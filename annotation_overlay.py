"""Create a positioned ASS overlay for OCR'd in-frame Chinese annotations.

Normal dialogue stays in SRT and is burned at the bottom.  This module turns
the separate, translated annotation SRT into an ASS script.  The selected
rectangle remains the OCR crop and horizontal label area; Vietnamese can be
raised independently so it stays clear and readable high on the video.  It is
a display-only artifact: callers never feed it to Edge TTS.
"""
from __future__ import annotations

import math
from pathlib import Path

from common import Subtitle, atomic_write_text, read_srt
from extract_subtitles import OCRRegion


PLAY_RESOLUTION = 1000


def _ass_time(seconds: float) -> str:
    """Format a finite non-negative timestamp using ASS centiseconds."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Timestamp chú thích không hợp lệ.")
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds_part, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds_part:02d}.{centiseconds:02d}"


def _escape_ass(text: str) -> str:
    """Escape user/OCR text so it cannot become ASS syntax."""
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", r"\N")
    )


def _wrap_text(text: str, *, max_characters: int) -> str:
    """Wrap Vietnamese words conservatively inside a bounded label region."""
    if "\n" in text:
        return "\n".join(_wrap_text(line, max_characters=max_characters) for line in text.split("\n"))
    words = text.split()
    if not words or len(text) <= max_characters:
        return text
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and len(candidate) > max_characters:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def _coordinates(region: OCRRegion) -> tuple[int, int, int, int, int, int]:
    region = region.validate()
    left = round(region.left * PLAY_RESOLUTION)
    top = round(region.top * PLAY_RESOLUTION)
    right = round(region.right * PLAY_RESOLUTION)
    bottom = round(region.bottom * PLAY_RESOLUTION)
    if right <= left or bottom <= top:
        raise ValueError("Vùng chèn chú thích trống.")
    return left, top, right, bottom, round((left + right) / 2), round((top + bottom) / 2)


def _display_coordinates(
    region: OCRRegion, display_y: float | None,
) -> tuple[int, int, int, int, int, int, str]:
    """Return clip/position coordinates for legacy or raised Vietnamese text.

    ``region`` always controls OCR and horizontal placement.  With a display
    position, only its vertical crop is released: the translated label can sit
    high on screen while OCR continues to inspect the exact selected rectangle.
    ``\\an8`` anchors the text's top edge at ``display_y`` so a two-line
    Vietnamese translation grows downward instead of disappearing above the
    frame.
    """
    left, top, right, bottom, x, legacy_y = _coordinates(region)
    if display_y is None:
        return left, top, right, bottom, x, legacy_y, r"\an5"
    if (type(display_y) not in (int, float) or not math.isfinite(float(display_y))
            or not 0.05 <= float(display_y) <= 0.80):
        raise ValueError("Vị trí dọc chú thích phải từ 0.05 đến 0.80.")
    y = round(float(display_y) * PLAY_RESOLUTION)
    return left, 0, right, PLAY_RESOLUTION, x, y, r"\an8"


def build_annotation_ass(
    cues: list[Subtitle], region: OCRRegion, *, font_size: int = 28,
    display_y: float | None = None,
) -> str:
    """Return a UTF-8 ASS document for cues placed inside ``region``.

    A fixed 1000×1000 virtual canvas lets normalized browser coordinates map
    directly to portrait and landscape source video.  ``\\clip`` prevents a
    long translation from spilling outside the location selected on the frame.
    ``display_y`` moves only Vietnamese output vertically; it does not change
    the OCR crop or timing.  ``None`` preserves the former centered-in-region
    output for direct callers.
    """
    if type(font_size) is not int or not 12 <= font_size <= 100:
        raise ValueError("Cỡ chữ chú thích phải từ 12 đến 100.")
    left, top, right, bottom, x, y, alignment = _display_coordinates(region, display_y)
    # A rough width-based word wrap prevents most short Vietnamese labels from
    # hitting the clip boundary while preserving explicit OCR line breaks.
    max_characters = max(8, int((right - left) / max(1, font_size * 0.52)))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_RESOLUTION}
PlayResY: {PLAY_RESOLUTION}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Annotation,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = [header]
    tag = rf"{{{alignment}\pos({x},{y})\clip({left},{top},{right},{bottom})}}"
    for cue in cues:
        text = _escape_ass(_wrap_text(cue.text, max_characters=max_characters))
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Annotation,,0,0,0,,{tag}{text}\n"
        )
    return "".join(lines)


def write_annotation_ass(
    source_srt: Path, destination: Path, region: OCRRegion, *, font_size: int = 28,
    display_y: float | None = None,
) -> list[Subtitle]:
    """Read translated annotation SRT and atomically write a temporary ASS file."""
    cues = read_srt(source_srt)
    if not cues:
        raise ValueError("SRT chú thích Việt không có câu để chèn.")
    atomic_write_text(
        destination, build_annotation_ass(cues, region, font_size=font_size, display_y=display_y),
        overwrite=True,
    )
    return cues
