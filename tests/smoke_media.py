"""Opt-in integration checks using real FFmpeg and, with --edge, the real service.

Run: python tests/smoke_media.py --ffmpeg C:/ffmpeg/bin/ffmpeg.exe
                                  --ffprobe C:/ffmpeg/bin/ffprobe.exe [--edge]
Tone fixtures test deterministic media timing; they are never production TTS.
All generated media and the JSON report are kept under temp/smoke_media_*.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from common import Subtitle, atomic_write_text, setup_logging, write_srt
from config import Settings
from media import probe_media, run_ffmpeg
from mix_audio import build_timeline, mix_audio
from tts_from_srt import (
    TTSProvider, audio_filename, tts_from_srt, validate_audio_manifest, wav_info,
)

LOGGER = logging.getLogger(__name__)


class FFmpegToneTestProvider:
    """A deterministic fixture provider, used only by this integration test."""

    name = "smoke-test-tone"

    def options(self, settings: Settings) -> dict[str, Any]:
        return {"duration": 2.1, "frequency": 440}

    async def synthesize(self, text: str, output_wav: Path, settings: Settings) -> None:
        run_ffmpeg([
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={settings.audio_sample_rate}:duration=2.1",
            "-ac", "1", "-c:a", "pcm_s16le", str(output_wav),
        ], settings)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def video_frame(path: Path, at: float, settings: Settings) -> bytes:
    result = subprocess.run([
        settings.ffmpeg_bin, "-v", "error", "-ss", str(at), "-i", str(path),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], capture_output=True, check=False)
    check(result.returncode == 0, result.stderr.decode("utf-8", errors="replace"))
    check(len(result.stdout) == 320 * 180 * 3, "FFmpeg frame decode had unexpected size")
    return result.stdout


def make_video(path: Path, settings: Settings, *, has_audio: bool) -> None:
    args = ["-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=16"]
    if has_audio:
        args.extend(["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=16"])
    args.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    if has_audio:
        args.extend(["-c:a", "aac", "-b:a", "96k"])
    args.extend(["-t", "16", str(path)])
    run_ffmpeg(args, settings)


def assert_audio_at(path: Path, start: float, end: float, settings: Settings) -> float:
    import numpy as np
    result = subprocess.run([
        settings.ffmpeg_bin, "-v", "error", "-ss", str(start), "-t", str(end - start),
        "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "24000",
        "-f", "f32le", "pipe:1",
    ], capture_output=True, check=False)
    check(result.returncode == 0, "Cannot decode output audio")
    samples = np.frombuffer(result.stdout, dtype="<f4")
    check(len(samples) > 0, "No samples in requested output interval")
    return float(np.sqrt(np.mean(samples * samples)))


def run_offline(settings: Settings, root: Path) -> dict[str, Any]:
    import numpy as np
    silent = root / "phim im lặng 中文 [01] ' gốc.mp4"
    original = root / "phim có tiếng 中文 [02] ' gốc.mp4"
    srt = root / "phụ đề Việt [cổ trang] ' 中文.srt"
    cues = [
        Subtitle(1, 12.2, 12.5, "Sư phụ, đệ tử đã trở về."),
        Subtitle(2, 14.0, 14.5, "Hãy cẩn thận!"),
        Subtitle(3, 15.5, 16.0, "Chúng ta lên đường."),
    ]
    write_srt(srt, cues)
    make_video(silent, settings, has_audio=False)
    make_video(original, settings, has_audio=True)
    provider: TTSProvider = FFmpegToneTestProvider()
    asyncio.run(tts_from_srt(srt, settings, provider=provider))
    infos = validate_audio_manifest(cues, settings, provider=provider)
    check(infos[1].duration == 2.1, "Fixture audio must last exactly 2.1 seconds")
    timeline_path = root / "timeline exact [12.200].f32"
    count = build_timeline(cues, infos, settings, 16, timeline_path)
    timeline = np.fromfile(timeline_path, dtype="<f4")
    check(count == 16 * 24000, "Timeline duration mismatch")
    start_sample = round(12.2 * 24000)
    check(np.all(timeline[:start_sample] == 0), "Audio starts before SRT timestamp")
    # A sine starts with a zero sample, so nonzero energy begins one sample later.
    check(np.any(timeline[start_sample:start_sample + 100] != 0), "Missing sound at 12.200 seconds")
    import wave
    with wave.open(str(settings.audio_dir / audio_filename(1)), "rb") as wav:
        cue_samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
    np.testing.assert_array_equal(timeline[start_sample:14 * 24000], cue_samples[:14 * 24000 - start_sample])
    overlap_sample = 14 * 24000 + 37
    expected_overlap = cue_samples[overlap_sample - start_sample] + cue_samples[37]
    check(timeline[overlap_sample] == expected_overlap, "Overlap was overwritten instead of mixed")
    silent_output = mix_audio(silent, srt, settings, output=root / "final im lặng [1].mp4", provider=provider)
    original_output = mix_audio(original, srt, settings, output=root / "final có tiếng [2].mp4", provider=provider)
    burned_output = mix_audio(silent, srt, settings, output=root / "final burn Việt ' [3].mp4", burn=True, provider=provider)
    for output in (silent_output, original_output, burned_output):
        info = probe_media(output, settings)
        check(info.has_audio, f"No audio in {output.name}")
        check(abs(info.duration - 16.0) <= 0.001, f"Duration changed in {output.name}: {info.duration}")
    check(assert_audio_at(silent_output, 1, 2, settings) < 1e-5, "Unexpected speech before 12.2 seconds")
    check(assert_audio_at(silent_output, 12.3, 12.4, settings) > 0.01, "Missing TTS after SRT start")
    check(assert_audio_at(original_output, 1, 2, settings) > 0.001, "Original audio not preserved")
    check(video_frame(silent_output, 12.3, settings) != video_frame(burned_output, 12.3, settings),
          "Burned output has no visible subtitle pixels")
    check(video_frame(silent_output, 1.0, settings) == video_frame(burned_output, 1.0, settings),
          "Subtitle unexpectedly visible outside SRT interval")
    return {
        "status": "PASS", "video_duration": 16, "audio_duration": infos[1].duration,
        "first_start_seconds": 12.2, "first_start_sample": start_sample,
        "timeline_samples": count, "unicode_path": str(root),
        "outputs": [str(path) for path in (silent_output, original_output, burned_output)],
        "checks": ["exact PCM placement", "overlap sum", "end truncation", "silent source",
                   "original audio retained", "output duration", "burn visible only at cue time"],
    }


def run_edge(settings: Settings, root: Path) -> dict[str, Any]:
    edge_settings = replace(settings, audio_dir=root / "edge_audio", tts_retries=1)
    srt = root / "edge lời chào Việt.srt"
    write_srt(srt, [Subtitle(1, 0, 4, "Xin chào, chúc bạn một ngày tốt lành.")])
    asyncio.run(tts_from_srt(srt, edge_settings))
    wav = edge_settings.audio_dir / "001.wav"
    info = wav_info(wav, edge_settings.audio_sample_rate)
    check(info.duration > 0.1, "Edge TTS returned implausibly short audio")
    return {"status": "PASS", "provider": "real edge-tts", "audio": str(wav), "duration": info.duration}


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--edge", action="store_true", help="Also contact real Edge TTS; needs internet")
    parser.add_argument("--edge-only", action="store_true", help="Only the real Edge TTS check")
    args = parser.parse_args()
    temp_root = PROJECT / "temp"
    temp_root.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="smoke_media Việt 中文 [test] ' ", dir=temp_root))
    settings = replace(
        Settings(), project_dir=PROJECT, output_dir=root / "output", audio_dir=root / "audio",
        temp_dir=root / "work", ffmpeg_bin=str(args.ffmpeg.resolve()), ffprobe_bin=str(args.ffprobe.resolve()),
    )
    report: dict[str, Any] = {"root": str(root)}
    try:
        if not args.edge_only:
            report["media"] = run_offline(settings, root)
        if args.edge or args.edge_only:
            report["edge"] = run_edge(settings, root)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        LOGGER.error("Integration test failed: %s", exc)
        status = 1
    else:
        status = 0
    report_path = root / "report.json"
    atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {report_path}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
