"""UTF-8 SRT I/O and safe file writes, shared by all pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable


@dataclass(frozen=True)
class Subtitle:
    id: int
    start: float
    end: float
    text: str


def setup_logging(verbose: bool = False) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    # HTTP debug logs may include request metadata; keep third-party logs quiet.
    for name in ("httpx", "httpcore", "google_genai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def atomic_write_text(path: Path, text: str, *, overwrite: bool = True) -> None:
    """Commit a complete UTF-8 file; exclusive publication protects edited SRTs."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temp, path)
        elif os.name == "nt":
            # Windows rename is exclusive, including on FAT/exFAT without hardlinks.
            os.rename(temp, path)
        else:
            # Same-directory hardlink publishes atomically and fails if destination exists.
            os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)


_TIMING = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})\s+-->\s+(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$")
# These Unicode format marks are invisible and have no spoken Vietnamese value.
# They are commonly introduced by copy/paste and make Edge return no audio.
_INVISIBLE_TEXT_TRANSLATION = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")


def normalize_subtitle_text(text: str) -> str:
    """Remove non-speaking invisible marks and normalize non-empty SRT lines."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE_TEXT_TRANSLATION)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())


def format_timestamp(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Timestamp phải là số giây hữu hạn không âm.")
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def validate_subtitles(subtitles: Iterable[Subtitle]) -> list[Subtitle]:
    result = list(subtitles)
    seen: set[int] = set()
    last_start = -1.0
    for sub in result:
        if type(sub.id) is not int or sub.id <= 0 or sub.id in seen:
            raise ValueError(f"ID subtitle phải nguyên dương, không trùng: {sub.id}")
        if not (math.isfinite(sub.start) and math.isfinite(sub.end)) or sub.start < 0 or sub.end <= sub.start:
            raise ValueError(f"Timing không hợp lệ tại subtitle {sub.id}.")
        if sub.start < last_start:
            raise ValueError(f"SRT phải xếp theo start_time: subtitle {sub.id}.")
        visible_text = sub.text.translate(_INVISIBLE_TEXT_TRANSLATION).strip()
        if not visible_text or re.search(r"\n\s*\n", visible_text):
            raise ValueError(f"Nội dung trống hoặc chứa dòng trống tại subtitle {sub.id}.")
        seen.add(sub.id)
        last_start = sub.start
    return result


def read_srt(path: Path) -> list[Subtitle]:
    """Read manual edits, ignoring empty cues without rewriting the source file.

    Cue headers delimit records so blank lines left after deleting a watermark
    do not detach the remaining dialogue from its original ID and timestamp.
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    headers = list(re.finditer(r"(?m)^[ \t]*(\d+)[ \t]*\n([^\n]*-->[^\n]*)(?:\n|$)", raw))
    if not headers or raw[:headers[0].start()].strip():
        raise ValueError(f"SRT không đúng định dạng trong {path}: thiếu ID hoặc timestamp đầu mục.")
    result: list[Subtitle] = []
    timing_rows: list[Subtitle] = []
    empty_ids: list[int] = []
    for index, header in enumerate(headers):
        cue_id = int(header.group(1))
        timing = header.group(2).strip()
        match = _TIMING.fullmatch(timing)
        if match is None:
            raise ValueError(f"Timestamp SRT không hợp lệ trong {path}: {timing}")
        body_end = headers[index + 1].start() if index + 1 < len(headers) else len(raw)
        body = raw[header.end():body_end]
        if "-->" in body:
            raise ValueError(f"SRT có timestamp thiếu ID hoặc sai định dạng sau mục {cue_id}: {path}")
        text = normalize_subtitle_text(body)
        numbers = [int(value) for value in match.groups()]
        start = numbers[0]*3600 + numbers[1]*60 + numbers[2] + numbers[3]/1000
        end = numbers[4]*3600 + numbers[5]*60 + numbers[6] + numbers[7]/1000
        # Validate every header, including empty cues: malformed timing and
        # duplicate IDs must not be silently discarded as "empty subtitles".
        timing_rows.append(Subtitle(cue_id, start, end, "timing validation"))
        if text:
            result.append(Subtitle(cue_id, start, end, text))
        else:
            empty_ids.append(cue_id)
    validate_subtitles(timing_rows)
    if empty_ids:
        logging.getLogger(__name__).info(
            "Tự bỏ qua %d mục phụ đề trống; còn %d mục có lời. Giữ ID/timestamp và khoảng nghỉ: %s",
            len(empty_ids), len(result), path,
        )
    return validate_subtitles(result)


def write_srt(path: Path, subtitles: Iterable[Subtitle], *, overwrite: bool = False) -> None:
    blocks: list[str] = []
    for sub in validate_subtitles(subtitles):
        if round(sub.end*1000) <= round(sub.start*1000):
            raise ValueError(f"Subtitle {sub.id} ngắn hơn độ phân giải SRT 1 ms.")
        text = normalize_subtitle_text(sub.text)
        blocks.append(f"{sub.id}\n{format_timestamp(sub.start)} --> {format_timestamp(sub.end)}\n{text}\n")
    atomic_write_text(Path(path), "\n".join(blocks), overwrite=overwrite)
