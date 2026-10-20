"""Mix current SRT-aligned WAVs using bounded-memory, sample-accurate placement."""
from __future__ import annotations

import argparse
import logging
import math
import sys
import uuid
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from common import Subtitle, read_srt, setup_logging
from config import Settings, load_settings
from media import probe_media, run_ffmpeg
from tts_from_srt import AudioInfo, TTSProvider, audio_filename, validate_audio_manifest, warn_overlaps, wav_info

LOGGER = logging.getLogger(__name__)


def build_timeline(
    cues: list[Subtitle], infos: dict[int, AudioInfo], settings: Settings,
    duration: float, destination: Path,
) -> int:
    """Write mono little-endian float PCM; return its exact number of samples.

    Only one WAV and 65536 samples are read at a time. A disk-backed array avoids
    a multi-hour RAM buffer and thousands of FFmpeg inputs/Windows argv overflow.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Thiếu numpy. Chạy: python -m pip install -r requirements.txt") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration phải lớn hơn 0.")
    rate = settings.audio_sample_rate
    sample_count = math.ceil(duration * rate)
    # Extending a new file creates zeroed bytes. No full-array initialization in RAM.
    with destination.open("wb") as handle:
        handle.truncate(sample_count * 4)
    timeline = np.memmap(destination, dtype="<f4", mode="r+", shape=(sample_count,))
    try:
        for index, cue in enumerate(cues, 1):
            start = round(cue.start * rate)
            if start < 0:
                raise ValueError(f"Timestamp âm tại ID {cue.id}")
            end = start + infos[cue.id].frames
            LOGGER.info("Timeline [%d/%d] #%d: %.3fs -> %.3fs", index, len(cues), cue.id,
                        cue.start, cue.start + infos[cue.id].duration)
            if end > sample_count:
                LOGGER.warning("Audio #%d vượt cuối video; chỉ giữ phần trước %.3fs.", cue.id, duration)
            if start >= sample_count:
                continue
            cursor = start
            with wave.open(str(settings.audio_dir / audio_filename(cue.id)), "rb") as wav:
                while cursor < sample_count:
                    data = wav.readframes(min(65536, sample_count - cursor))
                    if not data:
                        break
                    samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
                    samples *= 1.0 / 32768.0
                    # Add, don't overwrite: explicitly preserve overlaps.
                    timeline[cursor:cursor + len(samples)] += samples
                    cursor += len(samples)
        timeline.flush()
    finally:
        # Windows cannot remove the TemporaryDirectory until the mapping closes.
        timeline._mmap.close()
    return sample_count


def mix_audio(
    video: Path, srt_path: Path, settings: Settings, *, output: Path | None = None,
    burn: bool = False, provider: TTSProvider | None = None, display_srt: Path | None = None,
    logo_path: Path | None = None, annotation_srt: Path | None = None,
) -> Path:
    video, srt_path = video.resolve(), srt_path.resolve()
    display_srt = (display_srt or srt_path).resolve()
    logo_path = logo_path.resolve() if logo_path else None
    annotation_srt = annotation_srt.resolve() if annotation_srt else None
    output = (output or settings.output_dir / "final_video.mp4").resolve()
    if output in (video, srt_path, display_srt, annotation_srt):
        raise ValueError("File xuất phải khác video và SRT đầu vào.")
    for label, volume in (("original_volume", settings.original_volume), ("voice_volume", settings.voice_volume)):
        if not math.isfinite(volume) or volume < 0:
            raise ValueError(f"{label} phải là số hữu hạn >= 0.")
    cues = read_srt(srt_path)
    if not cues:
        raise ValueError("SRT không có lời thoại để ghép.")
    if burn:
        read_srt(display_srt)
    if settings.burn_logo and logo_path is None:
        raise ValueError("Đã bật chèn logo nhưng chưa chọn ảnh logo.")
    if logo_path is not None and (not logo_path.is_file() or logo_path.stat().st_size == 0):
        raise FileNotFoundError("Không tìm thấy ảnh logo hợp lệ để chèn vào video.")
    if annotation_srt is not None:
        # Annotation cues are display-only.  Validate their SRT independently
        # rather than letting them enter the speech/TTS timeline above.
        read_srt(annotation_srt)
    info = probe_media(video, settings)
    infos = validate_audio_manifest(cues, settings, provider=provider)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.mp4")
    try:
        with TemporaryDirectory(prefix="mix_", dir=settings.temp_dir) as temporary:
            timeline_settings = settings
            if settings.voice_speed != 1.0:
                speed_dir = Path(temporary) / 'voice'
                speed_dir.mkdir()
                for index, cue in enumerate(cues, 1):
                    destination = speed_dir / audio_filename(cue.id)
                    LOGGER.info('Tăng tốc giọng Việt [%d/%d] #%d: %.2fx',
                                index, len(cues), cue.id, settings.voice_speed)
                    run_ffmpeg(['-i', str(settings.audio_dir / audio_filename(cue.id)),
                                '-af', f'atempo={settings.voice_speed}', '-ac', '1',
                                '-ar', str(settings.audio_sample_rate), '-c:a', 'pcm_s16le',
                                str(destination)], settings)
                    infos[cue.id] = wav_info(destination, settings.audio_sample_rate)
                timeline_settings = replace(settings, audio_dir=speed_dir)
            warn_overlaps(cues, infos)
            timeline = Path(temporary) / "timeline.f32"
            samples = build_timeline(cues, infos, timeline_settings, info.duration, timeline)
            duration = f"{info.duration:.9f}"
            voice = f"[1:a:0]volume={settings.voice_volume},atrim=end_sample={samples}[voice]"
            if info.has_audio:
                graph = (
                    voice + ";"
                    f"[0:a:0]volume={settings.original_volume},apad,atrim=duration={duration}[original];"
                    "[original][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                    "alimiter=limit=0.95:level=0:latency=1[mixed]"
                )
            else:
                LOGGER.info("Video không có track audio gốc; xuất riêng giọng Việt.")
                graph = voice + ";[voice]alimiter=limit=0.95:level=0:latency=1[mixed]"
            LOGGER.info("FFmpeg: ghép audio vào %s", output)
            run_ffmpeg([
                "-i", str(video), "-f", "f32le", "-ar", str(settings.audio_sample_rate),
                "-ac", "1", "-i", str(timeline), "-filter_complex", graph,
                "-map", "0:v:0", "-map", "[mixed]", "-c:v", "copy", "-c:a", "aac",
                "-b:a", "192k", "-t", duration, "-movflags", "+faststart", str(pending),
            ], settings)
        if (burn or settings.blur_chinese_subtitles or settings.mirror_video or logo_path is not None
                or annotation_srt is not None):
            from burn_subtitle import render_video
            render_video(
                pending, display_srt if burn else None, output, settings,
                burn_subtitles=burn, logo_path=logo_path, annotation_srt=annotation_srt,
            )
        else:
            pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    LOGGER.info("Đã tạo %s", output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ghép WAV theo start_time SRT, giữ volume audio gốc tùy chọn.")
    parser.add_argument("video", type=Path)
    parser.add_argument("srt", type=Path)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--audio-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--original-volume", type=float)
    parser.add_argument("--voice-volume", type=float)
    parser.add_argument("--voice-speed", type=float, help="Tốc độ riêng giọng Việt: 1.0–1.5, ví dụ 1.1")
    parser.add_argument("--burn-subtitles", action="store_true")
    parser.add_argument("--blur-chinese-subtitles", action="store_true", default=None,
                        help="Làm mờ dải phụ đề Trung trước khi xuất")
    parser.add_argument("--blur-strength", type=int, help="Độ mờ phụ đề Trung: 1–50")
    parser.add_argument("--mirror-video", action="store_true", default=None,
                        help="Lật ngang video kiểu gương; phụ đề Việt không bị ngược")
    parser.add_argument("--logo", type=Path, help="Ảnh logo PNG/JPG/WebP để chèn vào video")
    parser.add_argument("--logo-position", choices=("top-left", "top-right", "bottom-left", "bottom-right"))
    parser.add_argument("--logo-width-percent", type=float, help="Độ rộng logo theo %% chiều rộng video (2–50)")
    parser.add_argument("--logo-opacity", type=float, help="Độ trong suốt logo từ 0.05 đến 1")
    parser.add_argument("--logo-margin-percent", type=float, help="Lề logo theo %% cạnh video (0–20)")
    parser.add_argument("--annotation-srt", type=Path,
                        help="SRT Việt cho chú thích trong khung hình; không tạo giọng từ các câu này")
    parser.add_argument("--display-srt", type=Path, help="SRT chèn chữ riêng; đối số srt vẫn dùng cho timeline giọng")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    setup_logging("--verbose" in sys.argv[1:])
    args = build_parser().parse_args()
    try:
        settings = load_settings(args.env)
        changes = {}
        if args.audio_dir is not None:
            changes["audio_dir"] = args.audio_dir.resolve()
        if args.original_volume is not None:
            changes["original_volume"] = args.original_volume
        if args.voice_volume is not None:
            changes["voice_volume"] = args.voice_volume
        if args.voice_speed is not None:
            changes["voice_speed"] = args.voice_speed
        if args.blur_chinese_subtitles is not None:
            changes["blur_chinese_subtitles"] = args.blur_chinese_subtitles
        if args.blur_strength is not None:
            changes["blur_strength"] = args.blur_strength
        if args.mirror_video is not None:
            changes["mirror_video"] = args.mirror_video
        if args.logo_position is not None:
            changes["logo_position"] = args.logo_position
        if args.logo_width_percent is not None:
            changes["logo_width_percent"] = args.logo_width_percent
        if args.logo_opacity is not None:
            changes["logo_opacity"] = args.logo_opacity
        if args.logo_margin_percent is not None:
            changes["logo_margin_percent"] = args.logo_margin_percent
        if args.logo is not None:
            changes["burn_logo"] = True
        if changes:
            settings = replace(settings, **changes)
        mix_audio(args.video, args.srt, settings, output=args.output, burn=args.burn_subtitles,
                  display_srt=args.display_srt, logo_path=args.logo, annotation_srt=args.annotation_srt)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Đã dừng; file xuất trước đó được giữ lại.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
