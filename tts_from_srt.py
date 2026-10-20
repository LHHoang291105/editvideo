"""Generate resumable, genuine PCM WAV speech from the current Vietnamese SRT.

Only cue text and explicit voice settings affect the speech cache. Editing a
timestamp therefore repositions the same recording without calling TTS again.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import unicodedata
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from common import Subtitle, atomic_write_text, read_srt, setup_logging
from config import Settings, load_settings
from media import find_binary, run_ffmpeg

LOGGER = logging.getLogger(__name__)
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

# Edge's public endpoint can intermittently close a websocket without sending
# audio.  Retrying one cue six times in a row turns that transient failure into
# a long stall and a burst of fresh requests.  These limits apply only to the
# typed Edge ``NoAudioReceived`` response; normal provider failures continue to
# use Settings.tts_retries.
EDGE_NO_AUDIO_LOGICAL_ATTEMPTS = 2
EDGE_NO_AUDIO_COOLDOWN_SECONDS = 30.0
EDGE_NO_AUDIO_MAX_COOLDOWN_SECONDS = 60.0


class TTSProvider(Protocol):
    """Future providers must write mono PCM signed-16 WAV at the configured rate."""

    name: str

    def options(self, settings: Settings) -> dict[str, Any]:
        """Return every provider setting which changes the generated audio."""
        ...

    async def synthesize(self, text: str, output_wav: Path, settings: Settings) -> None:
        ...


class EdgeTTSProvider:
    name = "edge-tts"

    def options(self, settings: Settings) -> dict[str, Any]:
        return {
            "voice": settings.tts_voice,
            "rate": settings.tts_rate,
            "volume": settings.tts_volume,
            "pitch": settings.tts_pitch,
        }

    async def synthesize(self, text: str, output_wav: Path, settings: Settings) -> None:
        try:
            import edge_tts
            from edge_tts.exceptions import NoAudioReceived
        except ImportError as exc:
            raise RuntimeError(
                "Thiếu edge-tts. Chạy: python -m pip install -r requirements.txt"
            ) from exc
        # Edge returns MP3 bytes. Renaming them .wav would create an invalid WAV.
        mp3_path = output_wav.with_suffix(".mp3")

        async def save_mp3(
            spoken_text: str, *, boundary: str = "SentenceBoundary",
        ) -> None:
            communicator = edge_tts.Communicate(
                spoken_text, settings.tts_voice, rate=settings.tts_rate,
                volume=settings.tts_volume, pitch=settings.tts_pitch, boundary=boundary,
                connect_timeout=15, receive_timeout=60,
            )
            await communicator.save(str(mp3_path))

        try:
            try:
                await save_mp3(text)
            except NoAudioReceived:
                mp3_path.unlink(missing_ok=True)
                LOGGER.info(
                    "Edge TTS không trả audio với SentenceBoundary; thử lại "
                    "nguyên văn với WordBoundary."
                )
                try:
                    # This changes only Edge's metadata request.  The SSML text,
                    # voice, rate, volume, and pitch remain exactly the same.
                    await save_mp3(text, boundary="WordBoundary")
                except NoAudioReceived:
                    fallback_text = edge_no_audio_fallback_text(text)
                    if fallback_text is None:
                        raise
                    # Some Edge voice endpoints return no audio for a bare
                    # sentence. The SRT remains unchanged; this only gives the
                    # service a natural sentence boundary for one final retry.
                    mp3_path.unlink(missing_ok=True)
                    LOGGER.info(
                        "Edge TTS vẫn không trả audio cho câu thiếu dấu kết thúc; "
                        "thử lại một lần với dấu chấm qua SentenceBoundary."
                    )
                    await save_mp3(fallback_text)
            run_ffmpeg([
                "-i", str(mp3_path), "-vn", "-ac", "1",
                "-ar", str(settings.audio_sample_rate), "-c:a", "pcm_s16le",
                str(output_wav),
            ], settings)
        finally:
            mp3_path.unlink(missing_ok=True)


def edge_no_audio_fallback_text(text: str) -> str | None:
    """Return a one-shot Edge-safe sentence only when no final punctuation exists.

    This fixes an intermittent Edge service quirk without changing the SRT or
    treating legitimate speech as disposable.  A final comma, full stop, quote
    after a full stop, etc. already counts as punctuation and is left alone.
    """
    stripped = text.rstrip()
    for character in reversed(stripped):
        category = unicodedata.category(character)
        if category in {"Pe", "Pf"}:
            # A closing bracket/quote can follow a real terminal punctuation.
            continue
        if category.startswith("P"):
            return None
        return f"{stripped}."
    return None


class _EdgeNoAudioExhausted(RuntimeError):
    """A real Edge no-audio response after its bounded logical attempts."""

    def __init__(self, cue_id: int, attempts: int, detail: str) -> None:
        self.cue_id = cue_id
        self.attempts = attempts
        self.detail = detail
        super().__init__(
            f"TTS thất bại ở ID {cue_id} sau {attempts} lần. "
            "Các WAV đã hoàn thành được giữ lại; chạy lại để tiếp tục. "
            f"Chi tiết: {detail}"
        )


def is_edge_no_audio_received(provider: TTSProvider, exc: Exception) -> bool:
    """Recognize only Edge's typed transient response, never a lookalike message."""
    if provider.name != EdgeTTSProvider.name:
        return False
    try:
        from edge_tts.exceptions import NoAudioReceived
    except ImportError:
        return False
    return isinstance(exc, NoAudioReceived)


@dataclass
class _EdgeNoAudioCircuit:
    """Rate-limit only typed Edge no-audio retries across one TTS run.

    A valid WAV closes the circuit.  The pause is intentionally process-local:
    it is safe to resume a later run from the manifest without persisting a
    transient endpoint condition in user data.
    """

    consecutive_failures: int = 0
    next_request_at: float = 0.0

    async def wait_before_request(self) -> None:
        delay = self.next_request_at - asyncio.get_running_loop().time()
        if delay <= 0:
            return
        LOGGER.warning(
            "Edge TTS tạm nghỉ %.0fs sau lỗi NoAudio liên tiếp; giữ các WAV đã tạo rồi thử lại.",
            delay,
        )
        await asyncio.sleep(delay)

    def record_no_audio(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures < EDGE_NO_AUDIO_LOGICAL_ATTEMPTS:
            return
        cooldown = min(
            EDGE_NO_AUDIO_MAX_COOLDOWN_SECONDS,
            EDGE_NO_AUDIO_COOLDOWN_SECONDS
            * (2 ** (self.consecutive_failures - EDGE_NO_AUDIO_LOGICAL_ATTEMPTS)),
        )
        now = asyncio.get_running_loop().time()
        self.next_request_at = max(self.next_request_at, now + cooldown)
        LOGGER.warning(
            "Edge TTS có %d lỗi NoAudio liên tiếp; ngừng request %.0fs trước lần thử tiếp theo.",
            self.consecutive_failures,
            cooldown,
        )

    def record_valid_wav(self) -> None:
        if self.consecutive_failures:
            LOGGER.info("Edge TTS đã nhận WAV hợp lệ; khôi phục nhịp gọi bình thường.")
        self.consecutive_failures = 0
        self.next_request_at = 0.0


@dataclass(frozen=True)
class AudioInfo:
    frames: int
    sample_rate: int

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


def wav_info(path: Path, expected_rate: int | None = None) -> AudioInfo:
    """Measure actual sample count and reject disguised MP3/truncated WAV files."""
    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise ValueError(f"WAV phải là mono PCM 16-bit: {path}")
            rate, frames = wav.getframerate(), wav.getnframes()
            if frames <= 0 or rate <= 0:
                raise ValueError(f"Audio rỗng: {path}")
            if expected_rate is not None and rate != expected_rate:
                raise ValueError(f"Sample rate không đúng ({rate} != {expected_rate}): {path}")
            # Check truncation with bounded memory, even if the header claims a long duration.
            actual_frames = 0
            while data := wav.readframes(65536):
                if len(data) % 2:
                    raise ValueError(f"Dữ liệu WAV bị hỏng: {path}")
                actual_frames += len(data) // 2
            if actual_frames != frames:
                raise ValueError(f"WAV bị thiếu dữ liệu: {path}")
        return AudioInfo(frames, rate)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Không đọc được WAV {path}: {exc}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def provider_signature(settings: Settings, provider: TTSProvider | None = None) -> dict[str, Any]:
    provider = provider or EdgeTTSProvider()
    return {
        "provider": provider.name,
        "options": provider.options(settings),
        "sample_rate": settings.audio_sample_rate,
        "format": "mono-pcm-s16le-wav",
    }


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": MANIFEST_VERSION, "entries": {}}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION
                or not isinstance(manifest.get("entries"), dict)):
            raise ValueError("version hoặc entries không hợp lệ")
        return manifest
    except (ValueError, OSError) as exc:
        raise RuntimeError(
            f"Manifest audio không hợp lệ: {path}. Chạy tts_from_srt.py với --force để tạo lại."
        ) from exc


def audio_filename(cue_id: int) -> str:
    return f"{cue_id:03d}.wav"


def cached_audio(
    cue: Subtitle, entry: Any, settings: Settings, signature: dict[str, Any],
) -> AudioInfo | None:
    if not isinstance(entry, dict):
        return None
    name = audio_filename(cue.id)
    if (entry.get("file") != name or entry.get("text_sha256") != text_digest(cue.text)
            or entry.get("signature") != signature):
        return None
    path = settings.audio_dir / name
    try:
        if sha256_file(path) != entry.get("wav_sha256"):
            return None
        info = wav_info(path, settings.audio_sample_rate)
        if entry.get("frames") != info.frames or entry.get("sample_rate") != info.sample_rate:
            return None
        return info
    except (OSError, ValueError):
        return None


def warn_overlaps(cues: list[Subtitle], infos: dict[int, AudioInfo]) -> list[dict[str, Any]]:
    """Use the next cue's START, never subtitle display end, for overlap checks."""
    warnings: list[dict[str, Any]] = []
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.id))
    for cue, following in zip(ordered, ordered[1:]):
        audio_end = cue.start + infos[cue.id].duration
        overlap = audio_end - following.start
        if overlap > 1e-9:
            item = {
                "id": cue.id, "next_id": following.id, "start": cue.start,
                "audio_end": audio_end, "next_start": following.start,
                "overlap_seconds": round(overlap, 6),
            }
            warnings.append(item)
            LOGGER.warning(
                "Overlap theo WAV đang dùng: #%s [%.3f -> %.3f] chồng #%s bắt đầu %.3f (%.3fs).",
                cue.id, cue.start, audio_end, following.id, following.start, overlap,
            )
    return warnings


def validate_audio_manifest(
    cues: list[Subtitle], settings: Settings, *, provider: TTSProvider | None = None,
) -> dict[int, AudioInfo]:
    """Refuse missing/stale audio so manual SRT edits cannot silently use old speech."""
    manifest = load_manifest(settings.audio_dir / MANIFEST_NAME)
    signature = provider_signature(settings, provider)
    infos: dict[int, AudioInfo] = {}
    invalid: list[int] = []
    for cue in cues:
        info = cached_audio(cue, manifest["entries"].get(str(cue.id)), settings, signature)
        if info is None:
            invalid.append(cue.id)
        else:
            infos[cue.id] = info
    if invalid:
        preview = ", ".join(map(str, invalid[:12]))
        raise RuntimeError(
            f"Audio thiếu, đã sửa, hoặc không khớp SRT/cấu hình ở ID: {preview}"
            f"{' ...' if len(invalid) > 12 else ''}. "
            "Hãy chạy lại tts_from_srt.py với SRT hiện tại trước khi ghép."
        )
    return infos


async def tts_from_srt(
    srt_path: Path, settings: Settings, *, force: bool = False,
    provider: TTSProvider | None = None,
) -> Path:
    srt_path = srt_path.resolve()
    cues = read_srt(srt_path)
    if not cues:
        raise ValueError(f"SRT không có lời thoại: {srt_path}")
    find_binary("ffmpeg", settings.ffmpeg_bin)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    provider = provider or EdgeTTSProvider()
    signature = provider_signature(settings, provider)
    manifest_path = settings.audio_dir / MANIFEST_NAME
    try:
        manifest = load_manifest(manifest_path)
    except RuntimeError:
        if not force:
            raise
        manifest = {"version": MANIFEST_VERSION, "entries": {}}
    manifest["source_srt"] = str(srt_path)
    manifest["source_srt_sha256"] = sha256_file(srt_path)
    infos: dict[int, AudioInfo] = {}
    generated_any = False
    edge_circuit = _EdgeNoAudioCircuit()

    async def create_and_checkpoint(
        cue: Subtitle, index: int, total: int, *, recovery: bool = False,
    ) -> AudioInfo:
        """Create one cue and atomically save its manifest entry on success only."""
        nonlocal generated_any
        if generated_any and settings.tts_request_interval > 0:
            await asyncio.sleep(settings.tts_request_interval)
        generated_any = True
        phase = "TTS phục hồi" if recovery else "TTS"
        LOGGER.info("%s [%d/%d] #%d: tạo giọng %s", phase, index, total, cue.id, provider.name)
        # Staging beside the destination makes replace atomic even on Windows drives.
        with TemporaryDirectory(prefix="tts_", dir=settings.audio_dir) as staging:
            pending = Path(staging) / audio_filename(cue.id)
            standard_attempts = max(1, settings.tts_retries)
            # The same small limit applies in the recovery pass.  It prevents a
            # single unavailable Edge endpoint from changing a deferred cue into
            # another six-attempt stall; a later program run can resume it from
            # the unchanged manifest.
            edge_attempt_limit = min(standard_attempts, EDGE_NO_AUDIO_LOGICAL_ATTEMPTS)
            edge_attempts = 0
            non_edge_attempts = 0
            info: AudioInfo | None = None
            while True:
                try:
                    pending.unlink(missing_ok=True)
                    if provider.name == EdgeTTSProvider.name:
                        await edge_circuit.wait_before_request()
                    await asyncio.wait_for(
                        provider.synthesize(cue.text, pending, settings),
                        timeout=settings.tts_timeout,
                    )
                    info = wav_info(pending, settings.audio_sample_rate)
                    if provider.name == EdgeTTSProvider.name:
                        # Reset only after the WAV passed validation, never merely
                        # after a websocket returns without raising.
                        edge_circuit.record_valid_wav()
                    break
                except Exception as exc:
                    detail = str(exc) or type(exc).__name__
                    if is_edge_no_audio_received(provider, exc):
                        edge_attempts += 1
                        edge_circuit.record_no_audio()
                        if edge_attempts >= edge_attempt_limit:
                            raise _EdgeNoAudioExhausted(cue.id, edge_attempts, detail) from exc
                        delay = min(
                            60.0,
                            settings.tts_retry_delay * (2 ** min(edge_attempts - 1, 10)),
                        )
                        LOGGER.warning(
                            "TTS #%d Edge không trả audio (lần %d/%d); chờ %.1fs rồi thử lại.",
                            cue.id,
                            edge_attempts,
                            edge_attempt_limit,
                            delay,
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                        continue

                    non_edge_attempts += 1
                    if non_edge_attempts >= standard_attempts:
                        detail = str(exc) or type(exc).__name__
                        raise RuntimeError(
                            f"TTS thất bại ở ID {cue.id} sau {standard_attempts} lần. "
                            "Các WAV đã hoàn thành được giữ lại; chạy lại để tiếp tục. "
                            f"Chi tiết: {detail}"
                        ) from exc
                    delay = min(
                        60.0,
                        settings.tts_retry_delay * (2 ** min(non_edge_attempts - 1, 10)),
                    )
                    LOGGER.warning("TTS #%d lỗi (lần %d/%d): %s. Chờ %.1fs rồi thử lại.",
                                   cue.id, non_edge_attempts, standard_attempts, detail, delay)
                    if delay > 0:
                        await asyncio.sleep(delay)
            assert info is not None
            entry = {
                "file": audio_filename(cue.id), "text_sha256": text_digest(cue.text),
                "signature": signature, "wav_sha256": sha256_file(pending),
                "frames": info.frames, "sample_rate": info.sample_rate,
                "duration": info.duration,
            }
            pending.replace(settings.audio_dir / audio_filename(cue.id))
            manifest["entries"][str(cue.id)] = entry
            # Checkpoint every successful cue; retrying a quota/network failure resumes.
            atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
        return info

    deferred: list[tuple[int, Subtitle, _EdgeNoAudioExhausted]] = []
    for index, cue in enumerate(cues, 1):
        entry = manifest["entries"].get(str(cue.id))
        info = None if force else cached_audio(cue, entry, settings, signature)
        if info is not None:
            LOGGER.info("TTS [%d/%d] #%d: dùng WAV đã kiểm tra", index, len(cues), cue.id)
            infos[cue.id] = info
            continue
        try:
            infos[cue.id] = await create_and_checkpoint(cue, index, len(cues))
        except _EdgeNoAudioExhausted as exc:
            # Do not create a silent placeholder or alter the existing manifest row.
            # The remaining cues can still checkpoint while Edge has time to recover.
            deferred.append((index, cue, exc))
            LOGGER.warning(
                "TTS #%d Edge không trả audio sau %d lần; tạm hoãn đến cuối hàng.",
                cue.id, exc.attempts,
            )

    if deferred:
        LOGGER.info("Phục hồi %d câu Edge TTS chưa có audio ở cuối hàng.", len(deferred))
        unresolved: list[_EdgeNoAudioExhausted] = []
        for recovery_index, (_, cue, _) in enumerate(deferred, 1):
            try:
                infos[cue.id] = await create_and_checkpoint(
                    cue, recovery_index, len(deferred), recovery=True,
                )
            except _EdgeNoAudioExhausted as exc:
                unresolved.append(exc)
                LOGGER.error("TTS #%d vẫn không trả audio trong lượt phục hồi.", cue.id)
        if unresolved:
            failed_ids = ", ".join(str(error.cue_id) for error in unresolved)
            details = "; ".join(f"#{error.cue_id}: {error.detail}" for error in unresolved)
            raise RuntimeError(
                f"TTS thất bại sau lượt phục hồi; Edge vẫn không trả audio cho ID: {failed_ids}. "
                "Các WAV đã hoàn thành được giữ lại; chạy lại để tiếp tục. "
                f"Chi tiết: {details}"
            ) from unresolved[0]

    overlaps = warn_overlaps(cues, infos)
    atomic_write_text(
        settings.audio_dir / "overlap_warnings.json",
        json.dumps(overlaps, ensure_ascii=False, indent=2),
    )
    # Also update provenance when the SRT was only re-timed and no WAV changed.
    atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    LOGGER.info("Hoàn thành %d WAV tại %s; %d overlap", len(cues), settings.audio_dir, len(overlaps))
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tạo Edge TTS WAV từ SRT tiếng Việt; tiếp tục theo manifest.")
    parser.add_argument("srt", type=Path)
    parser.add_argument("--env", type=Path, help="File .env tùy chọn")
    parser.add_argument("--audio-dir", type=Path)
    parser.add_argument("--force", action="store_true", help="Tạo lại toàn bộ WAV")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    setup_logging("--verbose" in sys.argv[1:])
    args = build_parser().parse_args()
    try:
        settings = load_settings(args.env)
        if args.audio_dir is not None:
            settings = replace(settings, audio_dir=args.audio_dir.resolve())
        asyncio.run(tts_from_srt(args.srt, settings, force=args.force))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Đã dừng; các WAV hoàn thành được giữ lại.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
