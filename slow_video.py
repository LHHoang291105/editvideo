"""Create a reviewable slowed copy of a source MP4 before OCR starts.

The original video is never modified.  Both the video and its first audio
track are slowed onto the same timeline, so OCR timestamps can later be used
unchanged for Vietnamese subtitles and dubbing.
"""
from __future__ import annotations

import argparse
import errno
from fractions import Fraction
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from common import setup_logging
from config import Settings, load_settings
from media import MediaInfo, find_binary, probe_media, run_ffmpeg


LOGGER = logging.getLogger(__name__)

MIN_SPEED = 0.25
MAX_SPEED = 1.0
DEFAULT_SPEED = 0.8
_FPS_RE = re.compile(r"^(\d+)/(\d+)$")
_INVALID_WINDOWS_FILENAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
})
_PUBLISH_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)


def _validate_speed(speed: float) -> float:
    """Return a finite supported playback rate, with a useful user error."""
    if not isinstance(speed, (int, float)) or isinstance(speed, bool):
        raise ValueError("Tốc độ video phải là số từ 0.25 đến 1.0.")
    value = float(speed)
    if not math.isfinite(value) or not MIN_SPEED <= value <= MAX_SPEED:
        raise ValueError("Tốc độ video phải từ 0.25 đến 1.0.")
    return value


def atempo_filter(speed: float) -> str:
    """Build an FFmpeg ``atempo`` chain for a 0.25–1.0 playback speed.

    FFmpeg accepts each atempo stage only from 0.5 to 100.  Splitting a slow
    rate keeps every stage valid: 0.25 is ``0.5 * 0.5`` and 0.30 is
    ``0.5 * 0.6``.  ``atempo`` preserves speech pitch, unlike changing a
    sample rate directly.
    """
    remaining = _validate_speed(speed)
    factors: list[float] = []
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.12g}" for factor in factors)


def _parse_frame_rate(value: object) -> Fraction | None:
    """Parse a trusted FFprobe frame-rate fraction without using it raw."""
    if not isinstance(value, str):
        return None
    match = _FPS_RE.fullmatch(value.strip())
    if match is None:
        return None
    numerator, denominator = (int(part) for part in match.groups())
    if numerator <= 0 or denominator <= 0:
        return None
    rate = Fraction(numerator, denominator)
    value_as_float = float(rate)
    if not math.isfinite(value_as_float) or not 1 <= value_as_float <= 240:
        return None
    return rate


def probe_video_fps(video: Path, settings: Settings) -> Fraction:
    """Return the primary video frame rate from FFprobe as an exact fraction."""
    video = Path(video).resolve()
    if not video.is_file():
        raise FileNotFoundError(f"Không tìm thấy video: {video}")
    binary = find_binary("ffprobe", settings.ffprobe_bin)
    result = subprocess.run(
        [
            binary, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "json", str(video),
        ],
        capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"FFprobe không đọc được FPS của {video.name}: {result.stderr[-3000:]}")
    try:
        streams = json.loads(result.stdout).get("streams", [])
        stream = streams[0]
        if not isinstance(stream, dict):
            raise TypeError("stream metadata is not an object")
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Không lấy được FPS video từ {video.name}.") from exc
    for field in ("avg_frame_rate", "r_frame_rate"):
        rate = _parse_frame_rate(stream.get(field))
        if rate is not None:
            return rate
    raise RuntimeError(f"FPS video không hợp lệ trong {video.name}.")


def probe_video_stream_duration(video: Path, settings: Settings) -> float:
    """Return the primary video-track duration without using audio/container time.

    A rendered MP4 can have a long audio track (or padded audio) while its
    visual track ended early.  ``probe_media`` deliberately returns the
    container timeline for general-purpose callers, so the slow-renderer uses
    this separate probe to make that failure impossible to publish.
    """
    video = Path(video).resolve()
    if not video.is_file():
        raise FileNotFoundError(f"Không tìm thấy video: {video}")
    binary = find_binary("ffprobe", settings.ffprobe_bin)
    result = subprocess.run(
        [
            binary, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=duration", "-of", "json", str(video),
        ],
        capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(
            f"FFprobe không đọc được thời lượng track hình của {video.name}: {result.stderr[-3000:]}"
        )
    try:
        streams = json.loads(result.stdout).get("streams", [])
        stream = streams[0]
        if not isinstance(stream, dict):
            raise TypeError("stream metadata is not an object")
        duration = float(stream.get("duration"))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration không hợp lệ")
        return duration
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Không lấy được thời lượng track hình từ {video.name}.") from exc


def _number(value: float) -> str:
    """Serialize only validated finite numeric values into an FFmpeg filter."""
    if not math.isfinite(value):
        raise ValueError("Thời lượng video không hợp lệ.")
    return f"{value:.9f}"


def _duration_tolerance(fps: Fraction) -> float:
    """Allow the unavoidable final-frame rounding of a video stream."""
    return max(0.05, 1.0 / float(fps) + 0.05)


def _validated_output_path(output: Path, video: Path) -> Path:
    """Normalize a safe slow-render destination before FFmpeg starts.

    A malformed path such as ``output/video_slow/.mp4`` has a filename of
    just ``.mp4``.  FFmpeg can still render its temporary file successfully,
    then Windows rejects the final rename.  Reject it early with a useful
    error instead of spending time on a render that cannot be published.
    """
    output = Path(output).expanduser().resolve(strict=False)
    name = output.name
    stem = output.stem.rstrip(" .")
    if (
        not name
        or name.casefold() == ".mp4"
        or not stem
        or any(character in _INVALID_WINDOWS_FILENAME_CHARS for character in name)
        or stem.upper() in _WINDOWS_RESERVED_BASENAMES
    ):
        raise ValueError(
            "Tên file video tua chậm không hợp lệ. Hãy chọn tên như video_slow.mp4, "
            "không để tên chỉ là .mp4."
        )
    if output.suffix.lower() != ".mp4":
        raise ValueError("Video tua chậm phải có đuôi .mp4.")
    if os.path.normcase(str(output)) == os.path.normcase(str(video)):
        raise ValueError("Video tua chậm phải khác video gốc.")
    if output.exists() and not output.is_file():
        raise ValueError(f"Đường dẫn video tua chậm đang là thư mục, không phải file: {output}")
    if output.parent.exists() and not output.parent.is_dir():
        raise ValueError(f"Thư mục xuất video tua chậm không hợp lệ: {output.parent}")
    return output


def _is_transient_publish_error(error: OSError) -> bool:
    """Recognize the Windows sharing/access errors worth retrying briefly."""
    return (
        getattr(error, "winerror", None) in {5, 32, 33}
        or error.errno in {errno.EACCES, errno.EBUSY, errno.EPERM}
    )


def _publish_pending_render(pending: Path, output: Path) -> None:
    """Atomically publish a verified temporary render with Windows retries.

    ``os.replace`` never removes a prior destination before it can replace it,
    so a failed publish leaves the user's last completed slow video intact.
    Windows Explorer, a video player, or antivirus can temporarily hold either
    file open right after FFmpeg exits; bounded backoff covers that case.
    """
    if not pending.is_file():
        raise RuntimeError(f"Không tìm thấy file tạm sau khi tua chậm: {pending}")
    had_previous_output = output.is_file()
    attempts = len(_PUBLISH_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            os.replace(pending, output)
            return
        except OSError as error:
            retry = _is_transient_publish_error(error) and attempt < attempts
            if retry:
                delay = _PUBLISH_RETRY_DELAYS[attempt - 1]
                LOGGER.warning(
                    "Windows đang bận file video tua chậm; thử lưu lại lần %s/%s sau %.2fs: %s",
                    attempt + 1, attempts, delay, error,
                )
                time.sleep(delay)
                continue
            preserved = (
                "Bản video tua chậm hoàn thành trước đó vẫn được giữ nguyên."
                if had_previous_output
                else "Không có video tua chậm mới nào được lưu."
            )
            raise RuntimeError(
                f"Không thể lưu video tua chậm vào {output}. "
                "Windows đang chặn hoặc khóa file này sau khi đã thử lại nhiều lần. "
                "Hãy đóng Explorer, trình phát video hoặc chương trình đang mở file rồi chạy lại. "
                f"{preserved}"
            ) from error


def _remove_pending_file(pending: Path) -> None:
    """Best-effort cleanup that must not hide the useful render/publish error."""
    try:
        pending.unlink(missing_ok=True)
    except OSError as error:
        LOGGER.warning("Không thể dọn file tạm tua chậm %s: %s", pending, error)


def video_encoder_args(settings: Settings) -> list[str]:
    """Return the selected H.264 encoding arguments for the review render.

    NVENC is the default on this Windows project.  Frame timing and audio
    filters still run inside FFmpeg, while the expensive H.264 encode runs on
    the NVIDIA GPU.  ``libx264`` remains an explicit portable fallback rather
    than a silent switch away from the user's selected GPU path.
    """
    if settings.slow_video_encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", "20", "-b:v", "0", "-pix_fmt", "yuv420p",
        ]
    if settings.slow_video_encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    # Settings validation normally reaches this first; keep the public helper
    # safe when callers construct a patched Settings-like object in a test.
    raise ValueError("SLOW_VIDEO_ENCODER phải là h264_nvenc hoặc libx264.")


def _validate_render(
    source: MediaInfo,
    rendered: MediaInfo,
    rendered_video_duration: float,
    target_duration: float,
    fps: Fraction,
) -> None:
    if source.has_audio != rendered.has_audio:
        expected = "có" if source.has_audio else "không có"
        actual = "có" if rendered.has_audio else "không có"
        raise RuntimeError(f"Video tua chậm phải {expected} audio gốc nhưng file tạm {actual} audio.")
    tolerance = _duration_tolerance(fps)
    if abs(rendered_video_duration - target_duration) > tolerance:
        raise RuntimeError(
            "Track hình video tua chậm không khớp: "
            f"dự kiến {target_duration:.3f}s, nhận {rendered_video_duration:.3f}s."
        )
    if abs(rendered.duration - target_duration) > tolerance:
        raise RuntimeError(
            "Thời lượng video tua chậm không khớp: "
            f"dự kiến {target_duration:.3f}s, nhận {rendered.duration:.3f}s."
        )


def slow_video(video: Path, speed: float, output: Path, settings: Settings) -> Path:
    """Render an MP4 slowed to ``speed`` and publish it atomically.

    The source remains untouched.  The generated file has H.264 video and,
    when the source has audio, a pitch-preserving AAC version of its first
    audio track.  ``output`` is only replaced after FFprobe verifies the
    resulting duration and audio-presence contract.
    """
    speed = _validate_speed(speed)
    video = Path(video).resolve()
    if video.suffix.lower() != ".mp4":
        raise ValueError("Video gốc phải là file MP4.")
    output = _validated_output_path(output, video)

    source_info = probe_media(video, settings)
    fps = probe_video_fps(video, settings)
    target_duration = source_info.duration / speed
    if not math.isfinite(target_duration) or target_duration <= 0:
        raise ValueError("Không xác định được thời lượng video tua chậm.")

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Không tạo được thư mục xuất video tua chậm {output.parent}: {exc}") from exc
    pending = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.mp4")
    speed_text = _number(speed)
    duration_text = _number(target_duration)
    # Keep every decoded frame and expand its timestamps.  Feeding this
    # into the fps filter used to force a CFR conversion; on some long HEVC
    # files it silently produced a short video track while padded audio made
    # the container look valid.  Passthrough below preserves the expanded
    # timing as VFR (normally 30 fps becomes 24 fps at 0.80x), which is the
    # correct slow motion without inventing or dropping frames.
    video_graph = f"[0:v:0]setpts=(PTS-STARTPTS)/{speed_text}[video]"
    try:
        if source_info.has_audio:
            audio_graph = (
                f"[0:a:0]asetpts=PTS-STARTPTS,{atempo_filter(speed)},"
                f"apad=pad_dur={duration_text},atrim=duration={duration_text}[audio]"
            )
            graph = f"{video_graph};{audio_graph}"
            maps = ["-map", "[video]", "-map", "[audio]", "-c:a", "aac", "-b:a", "192k"]
        else:
            graph = video_graph
            maps = ["-map", "[video]"]
        LOGGER.info(
            "FFmpeg: tua chậm video %.2fx bằng %s (%s → dự kiến %.3fs)",
            speed, settings.slow_video_encoder, video.name, target_duration,
        )
        run_ffmpeg([
            "-i", str(video), "-filter_complex", graph, *maps,
            "-map_metadata", "0", "-map_chapters", "0", *video_encoder_args(settings),
            "-fps_mode:v", "passthrough",
            "-t", duration_text, "-movflags", "+faststart", str(pending),
        ], settings)
        _validate_render(
            source_info,
            probe_media(pending, settings),
            probe_video_stream_duration(pending, settings),
            target_duration,
            fps,
        )
        _publish_pending_render(pending, output)
    finally:
        _remove_pending_file(pending)
    LOGGER.info("Đã tạo video tua chậm: %s", output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tạo MP4 tua chậm để xem trước trước khi OCR; video và audio gốc cùng chậm."
    )
    parser.add_argument("video", type=Path, help="Video MP4 gốc")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED,
                        help="Tốc độ 0.25–1.0, ví dụ 0.8 (mặc định: 0.8)")
    parser.add_argument("--output", type=Path, help="MP4 tua chậm; mặc định đặt trong OUTPUT_DIR")
    parser.add_argument("--env", type=Path)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    setup_logging("--verbose" in sys.argv[1:])
    args = build_parser().parse_args()
    try:
        settings = load_settings(args.env)
        output = args.output.resolve() if args.output else (
            settings.output_dir / f"{args.video.stem}_slow.mp4"
        ).resolve()
        slow_video(args.video, args.speed, output, settings)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Đã dừng; video tua chậm hoàn thành trước đó được giữ lại.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
