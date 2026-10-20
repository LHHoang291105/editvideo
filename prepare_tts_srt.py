"""Derive a speech SRT while preserving the original display subtitles."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import logging
import math
from pathlib import Path
import shutil
import unicodedata

from common import Subtitle, read_srt, setup_logging, validate_subtitles, write_srt

LOGGER = logging.getLogger(__name__)


def comparison_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    # Preserve word boundaries, accents, internal punctuation and symbols.
    # Only ignore ordinary sentence-ending punctuation.
    return " ".join(text.split()).rstrip(".!?…。！？")


def merge_repeated_cues(cues: list[Subtitle], max_gap: float = 0.3) -> list[Subtitle]:
    if not math.isfinite(max_gap) or max_gap < 0:
        raise ValueError("Khoảng cách gộp phải là số hữu hạn không âm.")
    result: list[Subtitle] = []
    for cue in validate_subtitles(cues):
        key = comparison_text(cue.text)
        if (result and key and key == comparison_text(result[-1].text)
                and cue.start - result[-1].end <= max_gap + 1e-9):
            result[-1] = replace(result[-1], end=max(result[-1].end, cue.end))
        else:
            result.append(cue)
    return result


def prepare_tts_srt(source: Path, destination: Path | None = None, *, max_gap: float = 0.3) -> Path:
    source = source.expanduser().resolve()
    destination = (destination or source.with_name(source.stem + "_tts.srt")).expanduser().resolve()
    if source == destination:
        raise ValueError("SRT tạo giọng phải khác SRT hiển thị.")
    cues = read_srt(source)
    merged = merge_repeated_cues(cues, max_gap)
    if not merged:
        raise ValueError("SRT không có lời thoại để tạo giọng.")
    if destination.exists():
        try:
            unchanged = read_srt(destination) == merged
        except ValueError:
            unchanged = False
        if unchanged:
            LOGGER.info("SRT tạo giọng đã cập nhật: %s (%d câu)", destination, len(merged))
            return destination
        backup = destination.with_name(destination.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
        shutil.copy2(destination, backup)
        LOGGER.info("Sao lưu SRT tạo giọng cũ: %s", backup)
    write_srt(destination, merged, overwrite=True)
    LOGGER.info("SRT tạo giọng: %d -> %d câu, gộp %d mục lặp: %s. Giữ nguyên SRT hiển thị.",
                len(cues), len(merged), len(cues) - len(merged), destination)
    return destination


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Tạo SRT riêng cho TTS, gộp câu trùng liền nhau; giữ SRT hiển thị.")
    parser.add_argument("srt", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-gap", type=float, default=0.3)
    args = parser.parse_args()
    try:
        prepare_tts_srt(args.srt, args.output, max_gap=args.max_gap)
        return 0
    except (OSError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
