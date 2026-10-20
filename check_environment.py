"""Check installed dependencies and FFmpeg without making paid API calls."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys

from common import setup_logging
from config import load_settings
from media import find_binary

LOGGER = logging.getLogger(__name__)
DEPENDENCIES = {"numpy": "numpy", "cv2": "opencv-contrib-python", "dotenv": "python-dotenv",
                "google.genai": "google-genai", "edge_tts": "edge-tts", "paddleocr": "paddleocr"}


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Kiểm tra Python, imports, FFmpeg; không gọi Gemini/TTS.")
    parser.add_argument("--env", type=Path)
    args = parser.parse_args()
    failures = 0
    LOGGER.info("Python %s | %s | %s", platform.python_version(), platform.machine(), sys.executable)
    if sys.version_info < (3, 11) or sys.maxsize <= 2**32:
        LOGGER.error("Cần Python 3.11+ 64-bit.")
        failures += 1
    try:
        settings = load_settings(args.env)
        from extract_subtitles import configure_ocr_cache
        configure_ocr_cache(settings)
    except (OSError, ValueError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    # Import checking should not trigger PaddleX model-host connectivity probes.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    for module, package in DEPENDENCIES.items():
        try:
            importlib.import_module(module)
            LOGGER.info("OK %-24s %s", package, importlib.metadata.version(package))
        except Exception as exc:
            failures += 1
            LOGGER.error("FAIL %s: %s", package, exc)
    try:
        installed = []
        for package in ("paddlepaddle", "paddlepaddle-gpu"):
            try:
                installed.append((package, importlib.metadata.version(package)))
            except importlib.metadata.PackageNotFoundError:
                pass
        if len(installed) != 1:
            raise RuntimeError("Cần cài đúng một bản Paddle: CPU hoặc GPU; không cài cả hai vào cùng .venv.")
        from ocr_runtime import select_ocr_device
        device = select_ocr_device(settings)
        import paddle
        value = paddle.ones([2, 2], dtype="float32").sum()
        if float(value.numpy()) != 4.0:
            raise RuntimeError("Paddle không tính được tensor kiểm tra.")
        LOGGER.info("OK %s %s | tensor trên %s", *installed[0], value.place)
    except Exception as exc:
        failures += 1
        LOGGER.error("FAIL Paddle/device: %s", exc)
    try:
        for name, value in (("ffmpeg", settings.ffmpeg_bin), ("ffprobe", settings.ffprobe_bin)):
            binary = find_binary(name, value)
            checked = subprocess.run([binary, "-version"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", check=True, timeout=30)
            LOGGER.info("OK %s: %s", binary, checked.stdout.splitlines()[0])
        ffmpeg = find_binary("ffmpeg", settings.ffmpeg_bin)
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", check=True, timeout=30)
        if not any(" subtitles " in line for line in filters.stdout.splitlines()):
            LOGGER.warning("FFmpeg thiếu subtitles/libass: vẫn mix được nhưng không burn được SRT.")
        if not settings.gemini_api_key:
            LOGGER.warning("Chưa có GEMINI_API_KEY: CLI sẽ hỏi key khi cần dịch trong terminal tương tác; OCR/mix dùng được.")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        failures += 1
        LOGGER.error("%s", exc)
    LOGGER.info("Kiểm tra xong: %d lỗi. Model OCR chỉ tải khi chạy OCR; API chưa được gọi.", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
