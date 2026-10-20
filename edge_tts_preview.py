"""One fixed Edge-TTS source utterance for optional RVC model auditions.

Microsoft's current Vietnamese Edge catalog exposes one female neural voice,
HoaiMy.  This helper deliberately does not invent "cute", "soft", or other
profiles from tiny pitch shifts.  It only creates the neutral source utterance
needed when someone later asks to audition a real, locally installed RVC model.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import uuid


EDGE_PREVIEW_TEXT = (
    "Xin chào, đây là câu mẫu tiếng Việt trong VietDub Studio. "
    "Bạn có thể nghe màu giọng của model RVC trước khi dùng cho video."
)
@dataclass(frozen=True)
class EdgeVoice:
    """One actual Vietnamese neural voice currently offered by Edge."""

    id: str
    label: str
    gender: str


EDGE_VOICES: tuple[EdgeVoice, ...] = (
    EdgeVoice("vi-VN-HoaiMyNeural", "Hoài My · Nữ", "Female"),
    EdgeVoice("vi-VN-NamMinhNeural", "Nam Minh · Nam", "Male"),
)
_VOICE_BY_ID = {voice.id: voice for voice in EDGE_VOICES}
EDGE_PREVIEW_VOICE = EDGE_VOICES[0].id
EDGE_PREVIEW_PITCH = "+0Hz"


def edge_voice_catalog() -> list[dict[str, str]]:
    """Return only real selectable Vietnamese Edge voice IDs for the browser."""
    return [
        {"id": voice.id, "label": voice.label, "gender": voice.gender}
        for voice in EDGE_VOICES
    ]


def edge_voice(voice_id: str) -> EdgeVoice:
    """Resolve an actual, finite Edge voice rather than a pseudo style name."""
    try:
        return _VOICE_BY_ID[voice_id]
    except KeyError as exc:
        raise ValueError("Giọng Edge không hợp lệ.") from exc


async def create_edge_preview(
    output: Path,
    *,
    voice_id: str = EDGE_PREVIEW_VOICE,
    pitch: str = EDGE_PREVIEW_PITCH,
) -> EdgeVoice:
    """Write a short sample for one actual Edge voice atomically.

    This audio has no relation to the project's TTS cache or final video.  It
    exists for an explicit Edge audition, or as a stable Vietnamese input for
    the separate RVC audition.  ``pitch`` is an advanced technical adjustment,
    not a choice of speaker.
    """
    voice = edge_voice(voice_id)
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "Thiếu edge-tts. Chạy: python -m pip install -r requirements.txt"
        ) from exc

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(f".{output.name}.{uuid.uuid4().hex}.pending.mp3")
    try:
        try:
            communicator = edge_tts.Communicate(
                EDGE_PREVIEW_TEXT,
                voice.id,
                rate="+0%",
                volume="+0%",
                pitch=pitch,
                boundary="WordBoundary",
                connect_timeout=15,
                receive_timeout=60,
            )
            await communicator.save(str(pending))
            if not pending.is_file() or pending.stat().st_size < 256:
                raise RuntimeError("Edge TTS không trả audio mẫu. Hãy thử lại sau.")
            os.replace(pending, output)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "Không tạo được audio mẫu từ Edge TTS. Kiểm tra Internet rồi thử lại."
            ) from exc
    finally:
        pending.unlink(missing_ok=True)
    return voice


def create_edge_preview_sync(
    output: Path,
    *,
    voice_id: str = EDGE_PREVIEW_VOICE,
    pitch: str = EDGE_PREVIEW_PITCH,
) -> EdgeVoice:
    """Synchronous bridge for the local Flask RVC-audition handler."""
    return asyncio.run(create_edge_preview(output, voice_id=voice_id, pitch=pitch))
