"""Read hardcoded Chinese subtitles from sampled lower video frames.

OCR timing is measured, not inferred by a language model. Boundaries are limited
by the sampling interval: a subtitle starts at its first visible sample and ends
at the first blank/different sample. OpenCV timestamps are used where available;
the warned frame/FPS fallback is only accurate for constant-frame-rate videos.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from common import Subtitle, read_srt, setup_logging, write_srt
from config import Settings, load_settings

LOGGER = logging.getLogger(__name__)

# The normal dialogue pipeline intentionally uses the compact PP-OCRv5 models
# so a full film remains practical to scan.  In-frame labels are often much
# smaller, vertical, outlined, or stylized; the larger Server pair is reserved
# for the explicit on-demand annotation recovery action in the web app.
ENHANCED_ANNOTATION_DET_MODEL = "PP-OCRv5_server_det"
ENHANCED_ANNOTATION_REC_MODEL = "PP-OCRv5_server_rec"


def enhanced_annotation_settings(settings: Settings) -> Settings:
    """Return stronger PaddleOCR settings for a selected annotation region.

    The caller keeps its current device, selected crop, sampling rate and
    confidence threshold.  This is deliberately not the default for dialogue
    OCR: Server models take longer and download more weights on their first
    use, while they substantially improve recovery of vertical/display text.
    """
    return replace(
        settings,
        ocr_det_model=ENHANCED_ANNOTATION_DET_MODEL,
        ocr_rec_model=ENHANCED_ANNOTATION_REC_MODEL,
    )


@dataclass(frozen=True)
class OCRSample:
    """A timestamp in seconds and text recognized in the cropped frame."""

    timestamp: float
    text: str
    confidence: float = 1.0


@dataclass(frozen=True)
class OCRRegion:
    """A normalized rectangular crop in the decoded source-video frame."""

    left: float
    top: float
    right: float
    bottom: float

    def validate(self) -> "OCRRegion":
        values = (self.left, self.top, self.right, self.bottom)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               or not math.isfinite(float(value)) for value in values):
            raise ValueError("Vùng OCR phải gồm các số hữu hạn từ 0 đến 1.")
        if not (0 <= self.left < self.right <= 1 and 0 <= self.top < self.bottom <= 1):
            raise ValueError("Vùng OCR phải có 0 <= trái < phải <= 1 và 0 <= trên < dưới <= 1.")
        return self

    @classmethod
    def subtitle_region(cls, settings: Settings) -> "OCRRegion":
        return cls(settings.roi_left, settings.roi_top, settings.roi_right, settings.roi_bottom).validate()

    @classmethod
    def annotation_region(cls, settings: Settings) -> "OCRRegion":
        return cls(
            settings.annotation_left, settings.annotation_top,
            settings.annotation_right, settings.annotation_bottom,
        ).validate()


def normalize_text(text: str) -> str:
    """Ignore punctuation/spacing for matching, preserving original display text."""
    return "".join(
        char.casefold()
        for char in unicodedata.normalize("NFKC", text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _edit_distance(left: str, right: str, limit: int) -> int:
    """Levenshtein distance, with a cheap early exit above the allowed limit."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_char in enumerate(right, 1):
        current = [row]
        for column, left_char in enumerate(left, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def texts_match(left: str, right: str, threshold: float = 0.84) -> bool:
    """Match small OCR errors conservatively, especially on short Chinese lines.

    Up to four characters require exact normalized equality: changing one Han
    character in a short answer often changes its meaning. Longer lines allow
    at most one edit (5-11 chars) or two edits (12+), and must meet threshold.
    """
    left_key, right_key = normalize_text(left), normalize_text(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shortest, longest = min(len(left_key), len(right_key)), max(len(left_key), len(right_key))
    if shortest <= 4:
        return False
    allowed = 1 if shortest < 12 else 2
    distance = _edit_distance(left_key, right_key, allowed)
    return distance <= allowed and (1.0 - distance / longest) >= threshold


@dataclass
class _TextVotes:
    count: int = 0
    score: float = 0.0


@dataclass
class _ActiveSubtitle:
    start: float
    last_seen: float
    anchor: str
    first_blank: float | None = None
    votes: dict[str, _TextVotes] = field(default_factory=dict)

    def add(self, sample: OCRSample) -> None:
        text = sample.text.strip()
        vote = self.votes.setdefault(text, _TextVotes())
        vote.count += 1
        vote.score += sample.confidence
        self.last_seen = sample.timestamp
        self.first_blank = None

    def representative(self) -> str:
        # Prefer the text observed most often; use confidence to break ties.
        return max(self.votes, key=lambda text: (self.votes[text].count, self.votes[text].score))


def group_samples(
    samples: Iterable[OCRSample], duration: float, settings: Settings
) -> list[Subtitle]:
    """Coalesce adjacent observations and tolerate brief recognition dropouts.

    Every new observation is compared to both the initial anchor and the voted
    representative, preventing a chain of small changes from merging sentences.
    A repeated sentence after a real blank gap receives a new subtitle ID.
    """
    if duration <= 0 or not math.isfinite(duration):
        raise ValueError("Video duration must be finite and positive.")
    output: list[Subtitle] = []
    active: _ActiveSubtitle | None = None
    previous_timestamp = -1.0

    def finish(end: float) -> None:
        nonlocal active
        if active is None:
            return
        end = min(duration, end)
        if end - active.start + 1e-9 >= settings.ocr_min_duration and end > active.start:
            output.append(Subtitle(len(output) + 1, active.start, end, active.representative()))
        active = None

    for sample in samples:
        timestamp = sample.timestamp
        if not math.isfinite(timestamp) or timestamp < 0 or timestamp < previous_timestamp:
            raise ValueError("OCR sample timestamps must be finite, nonnegative and ordered.")
        previous_timestamp = timestamp
        if timestamp >= duration:
            break
        text = sample.text.strip()
        if not normalize_text(text):
            if active is not None:
                if active.first_blank is None:
                    active.first_blank = timestamp
                if timestamp - active.first_blank > settings.ocr_max_gap + 1e-9:
                    finish(active.first_blank)
            continue

        if active is not None:
            gap_expired = (
                active.first_blank is not None
                and timestamp - active.first_blank > settings.ocr_max_gap + 1e-9
            )
            same = (
                not gap_expired
                and texts_match(active.anchor, text, settings.fuzzy_threshold)
                and texts_match(active.representative(), text, settings.fuzzy_threshold)
            )
            if same:
                active.add(sample)
                continue
            finish(active.first_blank if active.first_blank is not None else timestamp)

        active = _ActiveSubtitle(timestamp, timestamp, text)
        active.add(sample)

    if active is not None:
        # A final frame stays visible until EOF, including low-FPS and VFR clips.
        finish(active.first_blank if active.first_blank is not None else duration)
    return output


def configure_ocr_cache(settings: Settings) -> None:
    """Keep model/extension caches in the project unless explicitly overridden."""
    cache = settings.temp_dir / "model_cache"
    for variable, directory in {
        "PADDLE_PDX_CACHE_HOME": cache / "paddlex",
        "PADDLE_EXTENSION_DIR": cache / "extensions",
        "HF_HOME": cache / "huggingface",
        "MODELSCOPE_CACHE": cache / "modelscope",
    }.items():
        target = Path(os.environ.setdefault(variable, str(directory.resolve())))
        target.mkdir(parents=True, exist_ok=True)
    # Do not probe every model host on import. A missing model still downloads normally.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def _make_ocr(settings: Settings) -> Any:
    configure_ocr_cache(settings)
    from ocr_runtime import select_ocr_device
    device = select_ocr_device(settings)
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR/PaddlePaddle is missing. Activate .venv, then run "
            "python -m pip install -r requirements.txt. See README.md for Windows setup."
        ) from exc
    LOGGER.info(
        "Loading PaddleOCR on %s: detector=%s, recognizer=%s (first run downloads model files)...",
        settings.ocr_device,
        settings.ocr_det_model or settings.ocr_lang,
        settings.ocr_rec_model or settings.ocr_lang,
    )
    model_options: dict[str, Any]
    if settings.ocr_det_model and settings.ocr_rec_model:
        model_options = {
            "text_detection_model_name": settings.ocr_det_model,
            "text_recognition_model_name": settings.ocr_rec_model,
        }
    else:
        model_options = {"lang": settings.ocr_lang, "ocr_version": "PP-OCRv5"}
    try:
        return PaddleOCR(
            **model_options,
            device=device,
            # Paddle 3.3.1 on Windows can fail on PIR ArrayAttribute with oneDNN.
            # Plain CPU kernels are slower but work for this V1 compatibility target.
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_rec_score_thresh=settings.ocr_confidence,
        )
    except Exception as exc:
        raise RuntimeError(
            "PaddleOCR initialization failed. Check installed Paddle versions, model download "
            "access and OCR_DEVICE in .env. Original error: " + str(exc)
        ) from exc


def _parse_ocr_results(results: Iterable[Any], confidence_threshold: float) -> tuple[str, float]:
    """Read PaddleOCR 3.x result mappings and order boxes top-to-bottom/left-to-right."""
    boxes: list[tuple[float, float, float, str, float]] = []
    for result in results:
        if not isinstance(result, Mapping):
            # PaddleX Result is a dict subclass; reject incompatible API versions.
            raise RuntimeError("Unexpected PaddleOCR result. This project requires PaddleOCR 3.x.")
        payload = result.get("res", result)
        texts = payload.get("rec_texts", [])
        scores = payload.get("rec_scores", [])
        polygons = payload.get("rec_polys")
        if polygons is None:
            polygons = payload.get("dt_polys")
        for index, raw_text in enumerate(texts):
            score = float(scores[index]) if index < len(scores) else 0.0
            text = str(raw_text).strip()
            if score < confidence_threshold or not normalize_text(text):
                continue
            if polygons is not None and index < len(polygons):
                polygon = polygons[index]
                xs, ys = [float(point[0]) for point in polygon], [float(point[1]) for point in polygon]
                x, y, height = min(xs), (min(ys) + max(ys)) / 2, max(ys) - min(ys)
            else:
                x, y, height = 0.0, float(index * 100), 10.0
            boxes.append((y, x, max(1.0, height), text, score))
    if not boxes:
        return "", 0.0
    rows: list[list[tuple[float, float, float, str, float]]] = []
    for box in sorted(boxes, key=lambda item: (item[0], item[1])):
        if rows:
            row_center = sum(item[0] for item in rows[-1]) / len(rows[-1])
            row_height = sum(item[2] for item in rows[-1]) / len(rows[-1])
            if abs(box[0] - row_center) <= min(box[2], row_height) * 0.55:
                rows[-1].append(box)
                continue
        rows.append([box])
    lines = [" ".join(item[3] for item in sorted(row, key=lambda item: item[1])) for row in rows]
    character_count = sum(len(item[3]) for item in boxes)
    confidence = sum(item[4] * len(item[3]) for item in boxes) / character_count
    return "\n".join(lines), confidence


def iter_video_samples(
    video: Path, settings: Settings, duration: float, ocr: Any, *,
    region: OCRRegion | None = None, progress_name: str = "OCR",
) -> Iterator[OCRSample]:
    """Decode sequentially but retrieve/crop/recognize only selected video frames."""
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is missing. Run python -m pip install -r requirements.txt.") from exc
    region = (region or OCRRegion.subtitle_region(settings)).validate()
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Cannot open video with OpenCV: {video}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    interval = 1.0 / settings.sample_fps
    next_sample = 0.0
    last_timestamp = -1.0
    frame_index = -1
    sample_count = 0
    fallback = False
    last_progress = time.monotonic()
    try:
        while capture.grab():
            frame_index += 1
            timestamp = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
            if not fallback and (
                not math.isfinite(timestamp)
                or timestamp < 0
                or (frame_index > 0 and timestamp <= last_timestamp + 1e-9)
            ):
                fallback = True
                if not math.isfinite(fps) or fps <= 0:
                    raise RuntimeError("Video has neither usable timestamps nor FPS metadata.")
                LOGGER.warning(
                    "OpenCV timestamps unavailable/non-monotonic: using frame index / %.3f FPS. "
                    "This fallback can be inaccurate for VFR video; convert to CFR first.", fps,
                )
            if fallback:
                timestamp = frame_index / fps
            if timestamp < last_timestamp - 1e-6:
                raise RuntimeError("Video timing moved backwards; remux or convert the input to CFR first.")
            last_timestamp = timestamp
            if timestamp + 1e-8 < next_sample:
                continue
            # Advance to the next grid point without accumulating frame rounding drift.
            next_sample = (math.floor((timestamp + 1e-8) / interval) + 1) * interval
            if timestamp >= duration:
                break
            ok, frame = capture.retrieve()
            if not ok or frame is None:
                raise RuntimeError(f"Cannot decode sampled video frame at {timestamp:.3f}s.")
            height, width = frame.shape[:2]
            left, right = int(width * region.left), int(width * region.right)
            top, bottom = int(height * region.top), int(height * region.bottom)
            if right <= left or bottom <= top:
                raise ValueError("Vùng OCR trống; hãy khoanh lại vùng chữ trên video.")
            crop = frame[top:bottom, left:right].copy()
            try:
                text, confidence = _parse_ocr_results(ocr.predict(crop), settings.ocr_confidence)
            except Exception as exc:
                raise RuntimeError(f"OCR failed at {timestamp:.3f}s: {exc}") from exc
            sample_count += 1
            if sample_count == 1 or time.monotonic() - last_progress >= 5:
                LOGGER.info(
                    "%s %.1f%% | %.1f/%.1fs | %d sampled frames",
                    progress_name,
                    min(100.0, timestamp / duration * 100), timestamp, duration, sample_count,
                )
                last_progress = time.monotonic()
            yield OCRSample(timestamp, text, confidence)
        if frame_index < 0:
            raise RuntimeError("The video contains no decodable frames.")
        # Do not silently save truncated OCR after an early decoder failure.
        tolerance = max(2.0, interval * 2, 2 / fps if fps > 0 else 0)
        if last_timestamp + tolerance < duration:
            raise RuntimeError(
                f"Video decoder stopped at {last_timestamp:.3f}s, before duration {duration:.3f}s. "
                "Check whether the video is damaged."
            )
        LOGGER.info("OCR complete: %d sampled frames from %d decoded frames.", sample_count, frame_index + 1)
    finally:
        capture.release()


def extract_region_subtitles(
    video: Path, output_srt: Path, settings: Settings, region: OCRRegion, *,
    overwrite: bool = False, label: str = "phụ đề",
) -> list[Subtitle]:
    """OCR one chosen image region and atomically save its timed Chinese SRT."""
    video, output_srt = Path(video).resolve(), Path(output_srt).resolve()
    region = region.validate()
    if not video.is_file():
        raise FileNotFoundError(f"Input video does not exist: {video}")
    if output_srt == video:
        raise ValueError("Output SRT must not overwrite the input video.")
    if output_srt.exists() and not overwrite:
        LOGGER.info("Reusing existing Chinese SRT: %s", output_srt)
        return read_srt(output_srt)
    if not math.isfinite(settings.sample_fps) or settings.sample_fps <= 0:
        raise ValueError("SAMPLE_FPS must be positive.")
    from media import probe_media

    duration = probe_media(video, settings).duration
    ocr = _make_ocr(settings)
    samples = iter_video_samples(video, settings, duration, ocr, region=region, progress_name=f"OCR {label}")
    subtitles = group_samples(samples, duration, settings)
    if not subtitles:
        raise RuntimeError(
            f"Không tìm thấy chữ trong vùng {label}. Hãy khoanh lại vùng, kiểm tra OCR confidence "
            "và video đầu vào. SRT cũ không bị thay đổi."
        )
    write_srt(output_srt, subtitles, overwrite=overwrite)
    LOGGER.info("Đã lưu %d câu OCR %s: %s", len(subtitles), label, output_srt)
    return subtitles


def extract_subtitles(
    video: Path, output_srt: Path, settings: Settings, *, overwrite: bool = False
) -> list[Subtitle]:
    """OCR the configured lower dialogue ROI and save its Chinese SRT."""
    return extract_region_subtitles(
        video, output_srt, settings, OCRRegion.subtitle_region(settings), overwrite=overwrite,
        label="phụ đề dưới video",
    )


def main() -> int:
    # argparse prints --help before normal log setup; configure Unicode first.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Extract hardcoded Chinese subtitles using PaddleOCR.")
    parser.add_argument("video", type=Path, help="Input MP4 video")
    parser.add_argument("--output", type=Path, help="Chinese SRT output (default: output/<stem>_zh.srt)")
    parser.add_argument("--sample-fps", type=float, help="Frames per second sent to OCR (default from .env: 4)")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace the Chinese SRT")
    parser.add_argument("--env", type=Path, help="Load settings from this .env file")
    parser.add_argument("--verbose", action="store_true", help="Enable detailed logging")
    args = parser.parse_args()
    setup_logging(args.verbose)
    try:
        settings = load_settings(args.env)
        if args.sample_fps is not None:
            settings = replace(settings, sample_fps=args.sample_fps)
        output = args.output or settings.output_dir / f"{args.video.stem}_zh.srt"
        extract_subtitles(args.video, output, settings, overwrite=args.overwrite)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("OCR cancelled; existing SRT files are untouched.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
