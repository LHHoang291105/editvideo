"""FFmpeg wrappers: argument arrays, UTF-8 errors, no shell interpolation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import shutil
import subprocess
import time

from config import Settings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_audio: bool


def find_binary(name: str, configured: str | None = None) -> str:
    candidate = configured or name
    found = shutil.which(candidate)
    if not found:
        raise RuntimeError(
            f"Không tìm thấy {name}. Cài FFmpeg có cả ffmpeg.exe và ffprobe.exe, "
            f"thêm thư mục bin vào PATH rồi mở lại terminal, hoặc đặt {name.upper()}_BIN "
            "trong .env bằng đường dẫn đầy đủ. Xem README.md."
        )
    return str(Path(found).resolve())


def probe_media(path: Path, settings: Settings) -> MediaInfo:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy media: {path}")
    binary = find_binary("ffprobe", settings.ffprobe_bin)
    result = subprocess.run(
        [binary, "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration", "-of", "json", str(path)],
        capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"FFprobe không đọc được {path.name}: {result.stderr[-3000:]}")
    try:
        data = json.loads(result.stdout)
        videos = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"]
        if not videos:
            raise ValueError("File không có video stream.")
        # The MP4 container duration is the playable timeline shared by its
        # tracks, and is the right default for general media operations such
        # as mixing or burning subtitles.  Fall back to the primary video
        # stream only when the container does not provide one.  Renderers
        # that need to prove the visual track itself is complete perform a
        # separate video-stream validation.
        duration = 0.0
        for candidate in (data.get("format", {}).get("duration"), videos[0].get("duration")):
            try:
                parsed = float(candidate)
            except (ValueError, TypeError):
                continue
            if math.isfinite(parsed) and parsed > 0:
                duration = parsed
                break
        if duration <= 0:
            raise ValueError("Không xác định được thời lượng video.")
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Metadata video không hợp lệ: {path.name}: {exc}") from exc
    return MediaInfo(duration, any(s.get("codec_type") == "audio" for s in data.get("streams", [])))


def run_ffmpeg(args: list[str], settings: Settings, *, cwd: Path | None = None) -> None:
    binary = find_binary("ffmpeg", settings.ffmpeg_bin)
    command = [binary, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *map(str, args)]
    # Disk-backed stderr avoids pipe deadlock and unbounded RAM during long encodes.
    import tempfile
    with tempfile.TemporaryFile(mode="w+b") as errors:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=errors,
                                   encoding="utf-8", errors="replace")
        last_update = time.monotonic()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if line.startswith("out_time=") and time.monotonic() - last_update >= 10:
                    LOGGER.info("FFmpeg: đã xử lý %s", line.strip().split("=", 1)[1])
                    last_update = time.monotonic()
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
        if code:
            errors.seek(0, 2)
            errors.seek(max(0, errors.tell() - 6000))
            detail = errors.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"FFmpeg thất bại (mã {code}):\n{detail}")
