"""Compare real CPU/GPU OCR on the SAME video, model, ROI and sample timestamps."""
from __future__ import annotations

import argparse
from dataclasses import replace
import gc
import json
from pathlib import Path
import statistics
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from common import setup_logging
from config import load_settings
from extract_subtitles import _make_ocr, group_samples, iter_video_samples
from media import probe_media


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    settings = load_settings()
    duration = probe_media(args.video, settings).duration
    report = {"video": str(args.video.resolve()), "video_duration": duration, "sample_fps": settings.sample_fps, "devices": {}}
    results = {}
    for device in ("cpu", "gpu:0"):
        current = replace(settings, ocr_device=device)
        start = time.perf_counter()
        ocr = _make_ocr(current)
        initialization = time.perf_counter() - start
        timings = []
        for run in range(args.repeats + 1):
            start = time.perf_counter()
            samples = list(iter_video_samples(args.video.resolve(), current, duration, ocr))
            cues = group_samples(samples, duration, current)
            elapsed = time.perf_counter() - start
            if run > 0:  # Exclude one warmup run for each device.
                timings.append(elapsed)
        results[device] = [(cue.id, cue.start, cue.end, cue.text) for cue in cues]
        report["devices"][device] = {"initialization_seconds": initialization, "run_seconds": timings,
                                     "median_seconds": statistics.median(timings), "samples": len(samples),
                                     "subtitles": results[device]}
        del ocr
        gc.collect()
    report["same_subtitles_and_timestamps"] = results["cpu"] == results["gpu:0"]
    report["speedup_excluding_model_load"] = report["devices"]["cpu"]["median_seconds"] / report["devices"]["gpu:0"]["median_seconds"]
    destination = settings.temp_dir / "ocr_gpu_benchmark.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["same_subtitles_and_timestamps"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
