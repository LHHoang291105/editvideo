"""Optional real-model OCR smoke test on a generated Chinese hard-subtitle clip.

Run from the project folder: python tests/smoke_ocr.py [--ffprobe PATH].
This downloads the real PaddleOCR models on first use. It is deliberately not
part of unittest discovery and never substitutes simulated recognition output.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffprobe", help="Run complete extract_subtitles with this ffprobe executable")
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/msyh.ttc"))
    args = parser.parse_args()

    destination = PROJECT / "temp" / "ocr smoke 中文 tiếng Việt"
    destination.mkdir(parents=True, exist_ok=True)
    # PaddleX supports these documented/source-defined cache locations. Keep all
    # model/extension downloads in this project, not the user's profile.
    cache = PROJECT / "temp" / "model_cache"
    for variable, directory in {
        "PADDLE_PDX_CACHE_HOME": cache / "paddlex",
        "PADDLE_EXTENSION_DIR": cache / "extensions",
        "HF_HOME": cache / "huggingface",
        "MODELSCOPE_CACHE": cache / "modelscope",
    }.items():
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(directory)

    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    from common import read_srt, setup_logging, write_srt
    from config import Settings
    from extract_subtitles import _make_ocr, extract_subtitles, group_samples, iter_video_samples, normalize_text

    setup_logging()
    font = ImageFont.truetype(str(args.font), 40)
    video = destination / "Chinese 字幕 sample.mp4"
    srt = destination / "sample_zh.srt"
    expected = ["你好欢迎来到中国", "我们一起去修仙吧"]
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 24, (960, 540))
    if not writer.isOpened():
        raise RuntimeError("OpenCV cannot create the Unicode-path test MP4.")
    try:
        for frame_index in range(84):
            timestamp = frame_index / 24
            image = Image.new("RGB", (960, 540), "#17304a")
            draw = ImageDraw.Draw(image)
            draw.text((120, 80), "这里不是字幕", font=font, fill="white")
            text = ""
            if 0.5 <= timestamp < 1.5:
                text = "你好，欢迎来到中国"
            elif 2 <= timestamp < 3:
                text = "我们一起去修仙吧"
            if text:
                draw.text((480, 455), text, font=font, anchor="mm", fill="white", stroke_width=2, stroke_fill="black")
            writer.write(cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR))
    finally:
        writer.release()

    settings = Settings()
    if args.ffprobe:
        settings = replace(settings, ffprobe_bin=args.ffprobe)
        subtitles = extract_subtitles(video, srt, settings, overwrite=True)
        sample_count = None
    else:
        ocr = _make_ocr(settings)
        samples = list(iter_video_samples(video, settings, 3.5, ocr))
        subtitles = group_samples(samples, 3.5, settings)
        write_srt(srt, subtitles, overwrite=True)
        sample_count = len(samples)
        assert sample_count == 14, f"Expected 14 sampled frames, got {sample_count}"
    normalized = [normalize_text(subtitle.text) for subtitle in subtitles]
    assert normalized == expected, f"Unexpected real OCR output: {normalized!r}"
    assert [(sub.start, sub.end) for sub in subtitles] == [(0.5, 1.5), (2.0, 3.0)]
    assert read_srt(srt) == subtitles
    report = {
        "real_paddleocr": True,
        "unicode_video_path": str(video),
        "ffprobe_used": bool(args.ffprobe),
        "source_frames": 84,
        "ocr_samples": sample_count,
        "subtitles": [dict(id=sub.id, start=sub.start, end=sub.end, text=sub.text) for sub in subtitles],
    }
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
