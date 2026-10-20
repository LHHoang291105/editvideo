"""Offline regression tests for timing, resumability, and safe Windows paths.

The local test provider deliberately creates known PCM samples, solely to test
orchestration deterministically; production defaults always call Edge TTS.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from array import array
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from common import Subtitle, read_srt, write_srt
from config import Settings
from media import MediaInfo
from mix_audio import build_timeline, mix_audio
from tts_from_srt import (
    AudioInfo, EdgeTTSProvider, audio_filename, tts_from_srt,
    edge_no_audio_fallback_text, validate_audio_manifest, warn_overlaps, wav_info,
)


def write_pcm(path: Path, frames: int, *, value: int = 8192, rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array("h", [value] * frames).tobytes())


class LocalTestProvider(EdgeTTSProvider):
    """Same signature as Edge allows testing the normal mixer manifest validation."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def synthesize(self, text: str, output_wav: Path, settings: Settings) -> None:
        self.calls.append(text)
        if self.fail:
            output_wav.write_bytes(b"incomplete")
            raise RuntimeError("test transport failure")
        write_pcm(output_wav, 2400, rate=settings.audio_sample_rate)


class AudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="audio Việt 中文 ['test'] ")
        self.root = Path(self.temporary.name)
        self.settings = replace(
            Settings(), project_dir=self.root, output_dir=self.root / "output",
            audio_dir=self.root / "audio", temp_dir=self.root / "temp",
            tts_retries=1, tts_retry_delay=0, tts_request_interval=0,
        )
        self.settings.audio_dir.mkdir()
        self.settings.output_dir.mkdir()
        self.srt = self.root / "phụ đề 中文 ' [1].srt"
        self.cues = [Subtitle(3, 0.1, 0.12, "Xin chào."), Subtitle(17, 0.15, 0.18, "Sư phụ!")]
        write_srt(self.srt, self.cues)
        self.provider = LocalTestProvider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def generate(self, *, force: bool = False) -> Path:
        with patch("tts_from_srt.find_binary", return_value="ffmpeg"):
            return asyncio.run(tts_from_srt(self.srt, self.settings, force=force, provider=self.provider))

    def test_pcm_duration_and_id_filenames(self) -> None:
        self.generate()
        self.assertTrue((self.settings.audio_dir / "003.wav").is_file())
        self.assertTrue((self.settings.audio_dir / "017.wav").is_file())
        self.assertEqual(wav_info(self.settings.audio_dir / "003.wav").duration, 0.1)
        self.assertEqual(audio_filename(1234), "1234.wav")

    def test_deleting_cue_text_excludes_old_audio_from_tts_and_mixer_validation(self) -> None:
        self.generate()
        self.srt.write_text(
            "3\n00:00:00,100 --> 00:00:00,120\n\n"
            "17\n00:00:00,150 --> 00:00:00,180\nSư phụ!\n",
            encoding="utf-8",
        )
        self.generate()
        self.assertEqual(len(self.provider.calls), 2)
        cues = read_srt(self.srt)
        self.assertEqual([c.id for c in cues], [17])
        self.assertEqual(set(validate_audio_manifest(cues, self.settings)), {17})
        self.assertTrue((self.settings.audio_dir / "003.wav").exists())

    def test_retry_cleans_partial_audio_and_resumes_without_repeating_completed_cues(self) -> None:
        self.settings = replace(self.settings, tts_retries=6, tts_retry_delay=5)
        original = self.provider.synthesize
        failures = 0

        async def flaky(text: str, output: Path, settings: Settings) -> None:
            nonlocal failures
            self.assertFalse(output.exists())
            if text == self.cues[1].text and failures < 5:
                failures += 1
                output.write_bytes(b"partial audio")
                raise RuntimeError("No audio was received")
            await original(text, output, settings)

        with patch.object(self.provider, "synthesize", side_effect=flaky), patch(
            "tts_from_srt.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            self.generate()
            self.generate()
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [5, 10, 20, 40, 60])
        self.assertEqual(self.provider.calls, [cue.text for cue in self.cues])
        self.assertEqual(len(validate_audio_manifest(self.cues, self.settings)), 2)

    def test_exhausted_edge_no_audio_is_deferred_until_after_later_cues(self) -> None:
        from edge_tts.exceptions import NoAudioReceived

        cues = [
            *self.cues,
            Subtitle(29, 0.2, 0.24, "Câu sau vẫn phải được tạo."),
        ]
        write_srt(self.srt, cues, overwrite=True)
        calls: list[str] = []

        async def intermittent(text: str, output: Path, settings: Settings) -> None:
            calls.append(text)
            if text == cues[1].text and calls.count(text) == 1:
                raise NoAudioReceived()
            write_pcm(output, 2400, rate=settings.audio_sample_rate)

        with patch.object(self.provider, "synthesize", side_effect=intermittent):
            self.generate()

        self.assertEqual(calls, [cues[0].text, cues[1].text, cues[2].text, cues[1].text])
        self.assertTrue((self.settings.audio_dir / "029.wav").is_file())
        self.assertEqual(
            set(validate_audio_manifest(cues, self.settings, provider=self.provider)),
            {3, 17, 29},
        )

    def test_permanent_edge_no_audio_keeps_later_cue_checkpointed_before_final_error(self) -> None:
        from edge_tts.exceptions import NoAudioReceived

        cues = [
            *self.cues,
            Subtitle(29, 0.2, 0.24, "Câu sau vẫn phải được tạo."),
        ]
        write_srt(self.srt, cues, overwrite=True)
        calls: list[str] = []

        async def unavailable(text: str, output: Path, settings: Settings) -> None:
            calls.append(text)
            if text == cues[1].text:
                raise NoAudioReceived()
            write_pcm(output, 2400, rate=settings.audio_sample_rate)

        with patch.object(self.provider, "synthesize", side_effect=unavailable):
            with self.assertRaisesRegex(RuntimeError, "ID: 17"):
                self.generate()

        self.assertEqual(calls, [cues[0].text, cues[1].text, cues[2].text, cues[1].text])
        self.assertTrue((self.settings.audio_dir / "003.wav").is_file())
        self.assertFalse((self.settings.audio_dir / "017.wav").exists())
        self.assertTrue((self.settings.audio_dir / "029.wav").is_file())
        self.assertEqual(
            set(validate_audio_manifest([cues[0], cues[2]], self.settings, provider=self.provider)),
            {3, 29},
        )

    def test_edge_no_audio_circuit_bounds_main_attempts_and_resets_after_valid_wav(self) -> None:
        """A typed Edge outage is deferred, cooled down, then recovered without silent WAVs."""
        from edge_tts.exceptions import NoAudioReceived

        self.settings = replace(self.settings, tts_retries=6, tts_retry_delay=0)
        cues = [
            *self.cues,
            Subtitle(29, 0.2, 0.24, "Probe mở circuit."),
            Subtitle(42, 0.25, 0.29, "Câu lỗi sau khi đã phục hồi."),
            Subtitle(55, 0.3, 0.34, "Câu kế tiếp vẫn phải được tạo."),
        ]
        write_srt(self.srt, cues, overwrite=True)
        calls: list[str] = []
        counts: dict[str, int] = {}

        async def intermittent(text: str, output: Path, settings: Settings) -> None:
            calls.append(text)
            counts[text] = counts.get(text, 0) + 1
            # #17 opens at 30s. #29 then fails once, escalating to 60s,
            # before returning a valid WAV and resetting the circuit. #42
            # must consequently open at 30s again, not 60s.
            if text == cues[1].text and counts[text] <= 2:
                raise NoAudioReceived()
            if text == cues[2].text and counts[text] <= 1:
                raise NoAudioReceived()
            if text == cues[3].text and counts[text] <= 2:
                raise NoAudioReceived()
            write_pcm(output, 2400, rate=settings.audio_sample_rate)

        with patch.object(self.provider, "synthesize", side_effect=intermittent), patch(
            "tts_from_srt.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            self.generate()

        cooldowns = [call.args[0] for call in sleep.await_args_list]
        self.assertEqual(len(cooldowns), 3)
        for actual, expected in zip(cooldowns, (30.0, 60.0, 30.0)):
            self.assertAlmostEqual(actual, expected, delta=1.0)
        self.assertEqual(calls, [
            cues[0].text,
            cues[1].text, cues[1].text,
            cues[2].text, cues[2].text,
            cues[3].text, cues[3].text,
            cues[4].text,
            cues[1].text, cues[3].text,
        ])
        # Two main attempts, then one successful recovery attempt for each
        # deferred cue. No placeholder was ever written.
        self.assertEqual(counts[cues[1].text], 3)
        self.assertEqual(counts[cues[3].text], 3)
        self.assertEqual(
            set(validate_audio_manifest(cues, self.settings, provider=self.provider)),
            {3, 17, 29, 42, 55},
        )

    def test_non_edge_no_audio_error_still_fails_fast(self) -> None:
        cues = [
            *self.cues,
            Subtitle(29, 0.2, 0.24, "Câu sau không được chạy khi lỗi khác."),
        ]
        write_srt(self.srt, cues, overwrite=True)
        calls: list[str] = []

        async def non_edge_error(text: str, output: Path, settings: Settings) -> None:
            calls.append(text)
            if text == cues[1].text:
                raise RuntimeError("No audio was received")
            write_pcm(output, 2400, rate=settings.audio_sample_rate)

        with patch.object(self.provider, "synthesize", side_effect=non_edge_error):
            with self.assertRaisesRegex(RuntimeError, "TTS thất bại ở ID 17"):
                self.generate()

        self.assertEqual(calls, [cues[0].text, cues[1].text])
        self.assertFalse((self.settings.audio_dir / "029.wav").exists())

    def test_edge_no_audio_retries_exact_text_with_word_boundary(self) -> None:
        from edge_tts.exceptions import NoAudioReceived

        output = self.root / "fallback.wav"
        calls: list[tuple[str, str]] = []
        test_case = self

        class FakeCommunicator:
            def __init__(self, text: str, *args: object, **kwargs: object) -> None:
                self.text = text
                self.boundary = str(kwargs["boundary"])

            async def save(self, path: str) -> None:
                calls.append((self.text, self.boundary))
                if self.boundary == "SentenceBoundary":
                    Path(path).write_bytes(b"partial edge mp3")
                    raise NoAudioReceived()
                test_case.assertFalse(Path(path).exists())
                Path(path).write_bytes(b"fake edge mp3")

        def encode(args: list[str], settings: Settings) -> None:
            write_pcm(Path(args[-1]), 2400, rate=settings.audio_sample_rate)

        text = "Thái giám đến năm mươi tuổi"
        with patch("edge_tts.Communicate", FakeCommunicator), patch(
            "tts_from_srt.run_ffmpeg", side_effect=encode
        ):
            asyncio.run(EdgeTTSProvider().synthesize(text, output, self.settings))
        self.assertEqual(calls, [(text, "SentenceBoundary"), (text, "WordBoundary")])
        self.assertTrue(output.is_file())
        self.assertFalse(output.with_suffix(".mp3").exists())

    def test_edge_no_audio_uses_period_only_after_both_boundaries_fail(self) -> None:
        from edge_tts.exceptions import NoAudioReceived

        output = self.root / "period-fallback.wav"
        calls: list[tuple[str, str]] = []

        class FakeCommunicator:
            def __init__(self, text: str, *args: object, **kwargs: object) -> None:
                self.text = text
                self.boundary = str(kwargs["boundary"])

            async def save(self, path: str) -> None:
                calls.append((self.text, self.boundary))
                if len(calls) < 3:
                    raise NoAudioReceived()
                Path(path).write_bytes(b"fake edge mp3")

        def encode(args: list[str], settings: Settings) -> None:
            write_pcm(Path(args[-1]), 2400, rate=settings.audio_sample_rate)

        text = "Thái giám đến năm mươi tuổi"
        with patch("edge_tts.Communicate", FakeCommunicator), patch(
            "tts_from_srt.run_ffmpeg", side_effect=encode
        ):
            asyncio.run(EdgeTTSProvider().synthesize(text, output, self.settings))
        self.assertEqual(calls, [
            (text, "SentenceBoundary"),
            (text, "WordBoundary"),
            (f"{text}.", "SentenceBoundary"),
        ])

    def test_edge_no_audio_never_changes_a_sentence_with_punctuation(self) -> None:
        from edge_tts.exceptions import NoAudioReceived

        calls: list[tuple[str, str]] = []

        class FakeCommunicator:
            def __init__(self, text: str, *args: object, **kwargs: object) -> None:
                self.text = text
                self.boundary = str(kwargs["boundary"])

            async def save(self, path: str) -> None:
                calls.append((self.text, self.boundary))
                raise NoAudioReceived()

        text = "Thái giám đến năm mươi tuổi."
        with patch("edge_tts.Communicate", FakeCommunicator):
            with self.assertRaises(NoAudioReceived):
                asyncio.run(EdgeTTSProvider().synthesize(text, self.root / "no-audio.wav", self.settings))
        self.assertEqual(calls, [(text, "SentenceBoundary"), (text, "WordBoundary")])

    def test_edge_no_audio_fallback_only_applies_when_there_is_no_terminal_punctuation(self) -> None:
        self.assertEqual(
            edge_no_audio_fallback_text("Thái giám đến năm mươi tuổi"),
            "Thái giám đến năm mươi tuổi.",
        )
        self.assertIsNone(edge_no_audio_fallback_text("Xin chờ,"))
        self.assertIsNone(edge_no_audio_fallback_text('Ông ấy nói.\u201d'))
        self.assertIsNone(edge_no_audio_fallback_text("   \n\t"))

    def test_request_spacing_applies_only_to_new_audio(self) -> None:
        self.settings = replace(self.settings, tts_request_interval=1)
        with patch("tts_from_srt.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.generate()
            self.generate()
        sleep.assert_awaited_once_with(1)

    def test_hanging_provider_times_out_without_publishing_audio(self) -> None:
        self.settings = replace(self.settings, tts_timeout=0.01)

        async def hanging(text: str, output: Path, settings: Settings) -> None:
            await asyncio.Event().wait()

        with patch.object(self.provider, "synthesize", side_effect=hanging):
            with self.assertRaisesRegex(RuntimeError, "TTS thất bại"):
                self.generate()
        self.assertFalse((self.settings.audio_dir / "003.wav").exists())
        self.assertFalse((self.settings.audio_dir / "manifest.json").exists())

    def test_resume_and_manual_text_change_regenerate_only_changed_cue(self) -> None:
        self.generate()
        self.generate()
        self.assertEqual(len(self.provider.calls), 2)
        edited = [replace(self.cues[0], text="Ta tới đây."), self.cues[1]]
        write_srt(self.srt, edited, overwrite=True)
        with self.assertRaisesRegex(RuntimeError, "ID: 3"):
            validate_audio_manifest(edited, self.settings)
        self.generate()
        self.assertEqual(self.provider.calls, ["Xin chào.", "Sư phụ!", "Ta tới đây."])
        self.assertEqual(len(validate_audio_manifest(edited, self.settings)), 2)

    def test_retiming_reuses_audio_and_current_starts(self) -> None:
        self.generate()
        edited = [replace(cue, start=cue.start + 10, end=cue.end + 10) for cue in self.cues]
        write_srt(self.srt, edited, overwrite=True)
        self.generate()
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(len(validate_audio_manifest(read_srt(self.srt), self.settings)), 2)

    def test_missing_modified_wav_and_voice_setting_are_rejected(self) -> None:
        self.generate()
        with self.assertRaises(RuntimeError):
            validate_audio_manifest(self.cues, replace(self.settings, tts_voice="vi-VN-NamMinhNeural"))
        with self.assertRaises(RuntimeError):
            validate_audio_manifest(self.cues, replace(self.settings, tts_pitch="+5Hz"))
        write_pcm(self.settings.audio_dir / "003.wav", 2400, value=1)
        with self.assertRaisesRegex(RuntimeError, "ID: 3"):
            validate_audio_manifest(self.cues, self.settings)
        self.generate()
        (self.settings.audio_dir / "017.wav").unlink()
        with self.assertRaisesRegex(RuntimeError, "ID: 17"):
            validate_audio_manifest(self.cues, self.settings)

    def test_failure_preserves_previous_wav_and_manifest(self) -> None:
        manifest_path = self.generate()
        before_manifest = manifest_path.read_bytes()
        before_audio = (self.settings.audio_dir / "003.wav").read_bytes()
        self.provider.fail = True
        with self.assertRaisesRegex(RuntimeError, "TTS thất bại"):
            self.generate(force=True)
        self.assertEqual(manifest_path.read_bytes(), before_manifest)
        self.assertEqual((self.settings.audio_dir / "003.wav").read_bytes(), before_audio)

    def test_overlap_compares_next_start_and_reports_one_sample(self) -> None:
        cues = [Subtitle(1, 12.2, 12.3, "A"), Subtitle(2, 14.0, 15.0, "B")]
        report = warn_overlaps(cues, {1: AudioInfo(50400, 24000), 2: AudioInfo(2400, 24000)})
        self.assertAlmostEqual(report[0]["audio_end"], 14.3)
        self.assertAlmostEqual(report[0]["overlap_seconds"], 0.3)
        tiny = warn_overlaps(
            [Subtitle(1, 0, 1, "A"), Subtitle(2, 1, 2, "B")],
            {1: AudioInfo(24001, 24000), 2: AudioInfo(2400, 24000)},
        )
        self.assertEqual(len(tiny), 1)
        # Display end overlaps, but the actual first recording finishes before next start.
        no_overlap = warn_overlaps(
            [Subtitle(1, 0, 10, "A"), Subtitle(2, 1, 2, "B")],
            {1: AudioInfo(2400, 24000), 2: AudioInfo(2400, 24000)},
        )
        self.assertEqual(no_overlap, [])

    def test_timeline_exact_sample_positions_overlap_and_end_truncation(self) -> None:
        import numpy as np
        self.generate()
        infos = validate_audio_manifest(self.cues, self.settings)
        destination = self.root / "timeline.f32"
        count = build_timeline(self.cues, infos, self.settings, 0.22, destination)
        self.assertEqual(count, 5280)
        data = np.fromfile(destination, dtype="<f4")
        self.assertEqual(len(data), 5280)
        np.testing.assert_array_equal(data[:2400], 0)
        np.testing.assert_array_equal(data[2400:3600], 0.25)
        np.testing.assert_array_equal(data[3600:4800], 0.5)
        np.testing.assert_array_equal(data[4800:], 0.25)
        # File can be deleted immediately on Windows: the memmap was closed.
        destination.unlink()

    def test_invalid_wav_is_rejected(self) -> None:
        invalid = self.root / "bad.wav"
        invalid.write_bytes(b"This is not a WAV")
        with self.assertRaises(ValueError):
            wav_info(invalid)
        write_pcm(invalid, 100)
        invalid.write_bytes(invalid.read_bytes()[:-4])
        with self.assertRaisesRegex(ValueError, "thiếu dữ liệu"):
            wav_info(invalid)

    def test_mix_failure_preserves_existing_output(self) -> None:
        self.generate()
        output = self.settings.output_dir / "final_video.mp4"
        output.write_bytes(b"previous output")
        with patch("mix_audio.probe_media", return_value=MediaInfo(1.0, False)), patch(
            "mix_audio.run_ffmpeg", side_effect=RuntimeError("encode failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "encode failed"):
                mix_audio(self.root / "input.mp4", self.srt, self.settings, output=output)
        self.assertEqual(output.read_bytes(), b"previous output")
        self.assertEqual(list(output.parent.glob(".*.mp4")), [])

    def test_burn_uses_ascii_filter_path_and_absolute_media_paths(self) -> None:
        from burn_subtitle import burn_subtitle
        video = self.root / "phim ' 中文 [1].mp4"
        output = self.settings.output_dir / "kết quả.mp4"
        seen: list[list[str]] = []

        def encode(args: list[str], settings: Settings, *, cwd: Path | None = None) -> None:
            seen.append(args)
            self.assertIsNotNone(cwd)
            self.assertTrue((cwd / "subtitles.srt").is_file())
            self.assertEqual(read_srt(cwd / "subtitles.srt"), self.cues)
            self.assertTrue(Path(args[1]).is_absolute())
            graph = args[args.index("-filter_complex") + 1]
            self.assertIn("filename=subtitles.srt", graph)
            self.assertNotIn(str(self.root), graph)
            self.assertIn("[video]", args)
            Path(args[-1]).write_bytes(b"encoded video")

        with patch("burn_subtitle.probe_media", return_value=MediaInfo(1, False)), patch(
            "burn_subtitle.check_required_filters"
        ), patch("burn_subtitle.run_ffmpeg", side_effect=encode):
            result = burn_subtitle(video, self.srt, output, self.settings)
        self.assertEqual(result, output)
        self.assertEqual(output.read_bytes(), b"encoded video")
        self.assertEqual(len(seen), 1)

    def test_mixer_burns_display_srt_but_uses_speech_cues_for_timeline(self) -> None:
        self.generate()
        display = self.root / "hiển thị.srt"
        write_srt(display, [Subtitle(99, 0.4, 0.8, "Chữ hiển thị riêng")])

        def encode(args: list[str], settings: Settings) -> None:
            Path(args[-1]).write_bytes(b"mixed test fixture")

        with patch("mix_audio.probe_media", return_value=MediaInfo(1, False)), patch(
            "mix_audio.run_ffmpeg", side_effect=encode
        ), patch("mix_audio.build_timeline", wraps=build_timeline) as timeline, patch(
            "burn_subtitle.render_video"
        ) as burn:
            mix_audio(self.root / "input.mp4", self.srt, self.settings, burn=True, display_srt=display)
        self.assertEqual(timeline.call_args.args[0], self.cues)
        self.assertEqual(burn.call_args.args[1], display)
        self.assertTrue(burn.call_args.kwargs["burn_subtitles"])

    def test_mixer_renders_visual_effect_even_without_burn(self) -> None:
        self.generate()
        settings = replace(self.settings, mirror_video=True)

        def encode(args: list[str], current: Settings) -> None:
            Path(args[-1]).write_bytes(b"mixed test fixture")

        with patch("mix_audio.probe_media", return_value=MediaInfo(1, False)), patch(
            "mix_audio.run_ffmpeg", side_effect=encode
        ), patch("burn_subtitle.render_video") as render:
            mix_audio(self.root / "input.mp4", self.srt, settings, burn=False)
        self.assertIsNone(render.call_args.args[1])
        self.assertFalse(render.call_args.kwargs["burn_subtitles"])

    def test_mixer_renders_logo_even_without_burn_or_other_effects(self) -> None:
        self.generate()
        logo = self.root / "logo.png"
        logo.write_bytes(b"image fixture")
        settings = replace(self.settings, burn_logo=True)

        def encode(args: list[str], current: Settings) -> None:
            Path(args[-1]).write_bytes(b"mixed test fixture")

        with patch("mix_audio.probe_media", return_value=MediaInfo(1, False)), patch(
            "mix_audio.run_ffmpeg", side_effect=encode
        ), patch("burn_subtitle.render_video") as render:
            mix_audio(self.root / "input.mp4", self.srt, settings, burn=False, logo_path=logo)
        self.assertIsNone(render.call_args.args[1])
        self.assertFalse(render.call_args.kwargs["burn_subtitles"])
        self.assertEqual(render.call_args.kwargs["logo_path"], logo.resolve())

    def test_annotation_srt_is_display_only_and_never_enters_tts_timeline(self) -> None:
        self.generate()
        annotation = self.root / "chú thích giữa hình.srt"
        # Deliberately use an ID for which no WAV exists. If annotation cues
        # leak into the audio list, manifest validation/timeline construction
        # will fail instead of this render-only path succeeding.
        write_srt(annotation, [Subtitle(999, .2, .8, "Nhân vật: Trương Tam")])

        def encode(args: list[str], current: Settings) -> None:
            Path(args[-1]).write_bytes(b"mixed test fixture")

        with patch("mix_audio.probe_media", return_value=MediaInfo(1, False)), patch(
            "mix_audio.run_ffmpeg", side_effect=encode
        ), patch("mix_audio.build_timeline", wraps=build_timeline) as timeline, patch(
            "burn_subtitle.render_video"
        ) as render:
            mix_audio(self.root / "input.mp4", self.srt, self.settings, burn=False,
                      annotation_srt=annotation)

        self.assertEqual(timeline.call_args.args[0], self.cues)
        self.assertIsNone(render.call_args.args[1])
        self.assertFalse(render.call_args.kwargs["burn_subtitles"])
        self.assertEqual(render.call_args.kwargs["annotation_srt"], annotation.resolve())

    def test_enabled_logo_without_an_image_preserves_existing_output(self) -> None:
        self.generate()
        output = self.settings.output_dir / "final_video.mp4"
        output.write_bytes(b"previous output")
        with self.assertRaisesRegex(ValueError, "chưa chọn ảnh logo"):
            mix_audio(self.root / "input.mp4", self.srt, replace(self.settings, burn_logo=True), output=output)
        self.assertEqual(output.read_bytes(), b"previous output")

    def test_speed_changes_only_individual_voice_wavs_and_keeps_starts(self) -> None:
        self.generate()
        settings = replace(self.settings, voice_speed=1.1)
        before = (settings.audio_dir / '003.wav').read_bytes()
        calls = []

        def encode(args, current):
            calls.append(args)
            if '-af' in args:
                self.assertEqual(args[args.index('-af') + 1], 'atempo=1.1')
                write_pcm(Path(args[-1]), 2100)
            else:
                self.assertNotIn('atempo', ' '.join(args))
                Path(args[-1]).write_bytes(b'video fixture')

        with patch('mix_audio.probe_media', return_value=MediaInfo(1, True)), patch(
            'mix_audio.run_ffmpeg', side_effect=encode
        ), patch('mix_audio.build_timeline', wraps=build_timeline) as timeline:
            mix_audio(self.root/'input.mp4', self.srt, settings)
        self.assertEqual(len(calls), 3)
        self.assertEqual(timeline.call_args.args[0], self.cues)
        self.assertEqual(timeline.call_args.args[1][3].frames, 2100)
        self.assertEqual((settings.audio_dir/'003.wav').read_bytes(), before)

    def test_cli_help_is_utf8_even_with_windows_legacy_output_encoding(self) -> None:
        project = Path(__file__).resolve().parents[1]
        environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        for script in ("tts_from_srt.py", "mix_audio.py", "burn_subtitle.py"):
            with self.subTest(script=script):
                result = subprocess.run(
                    [sys.executable, str(project / script), "--help"], cwd=self.root,
                    capture_output=True, encoding="utf-8", env=environment, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--env", result.stdout)

    def test_missing_ffmpeg_cli_error_is_actionable(self) -> None:
        project = Path(__file__).resolve().parents[1]
        environment = {**os.environ, "FFMPEG_BIN": str(self.root / "missing-ffmpeg.exe")}
        result = subprocess.run(
            [sys.executable, str(project / "tts_from_srt.py"), str(self.srt)], cwd=self.root,
            capture_output=True, encoding="utf-8", env=environment, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("FFmpeg", result.stderr)
        self.assertIn("PATH", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
