"""Unit coverage for the pre-OCR slow-video renderer."""
from __future__ import annotations

import errno
from fractions import Fraction
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from config import Settings
from media import MediaInfo
from slow_video import (
    atempo_filter,
    probe_video_fps,
    probe_video_stream_duration,
    slow_video,
    video_encoder_args,
)


class SlowVideoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="slow video Việt 中文 ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "phim gốc 中文.mp4"
        self.source.write_bytes(b"source must remain unchanged")
        self.output = self.root / "output" / "phim_cham.mp4"
        self.settings = Settings(project_dir=self.root, output_dir=self.root / "output",
                                 audio_dir=self.root / "audio", temp_dir=self.root / "temp")

    def render(self, args: list[str], _settings: Settings) -> None:
        """Publish enough bytes for the mocked post-render FFprobe call."""
        Path(args[-1]).write_bytes(b"rendered")

    def test_atempo_filter_splits_rates_below_half(self) -> None:
        self.assertEqual(atempo_filter(0.25), "atempo=0.5,atempo=0.5")
        self.assertEqual(atempo_filter(0.3), "atempo=0.5,atempo=0.6")
        self.assertEqual(atempo_filter(0.5), "atempo=0.5")
        self.assertEqual(atempo_filter(0.8), "atempo=0.8")
        self.assertEqual(atempo_filter(1.0), "atempo=1")

    def test_atempo_filter_rejects_invalid_speeds(self) -> None:
        for speed in (0, 0.249, 1.001, float("nan"), True, "0.8"):
            with self.subTest(speed=speed):
                with self.assertRaises(ValueError):
                    atempo_filter(speed)  # type: ignore[arg-type]

    def test_audio_graph_uses_exact_fps_and_atempo_chain_then_publishes(self) -> None:
        source_info = MediaInfo(duration=3.2, has_audio=True)
        rendered_info = MediaInfo(duration=12.8, has_audio=True)
        source_before = self.source.read_bytes()
        with patch("slow_video.probe_media", side_effect=[source_info, rendered_info]), patch(
            "slow_video.probe_video_fps", return_value=Fraction(25, 1)
        ), patch("slow_video.probe_video_stream_duration", return_value=12.8), patch(
            "slow_video.run_ffmpeg", side_effect=self.render
        ) as render:
            result = slow_video(self.source, 0.25, self.output, self.settings)

        self.assertEqual(result, self.output.resolve())
        self.assertEqual(self.output.read_bytes(), b"rendered")
        self.assertEqual(self.source.read_bytes(), source_before)
        args = render.call_args.args[0]
        graph = args[args.index("-filter_complex") + 1]
        self.assertIn("setpts=(PTS-STARTPTS)/0.250000000", graph)
        self.assertNotIn("fps=fps=", graph)
        self.assertIn("atempo=0.5,atempo=0.5", graph)
        self.assertIn("apad=pad_dur=12.800000000", graph)
        self.assertIn("atrim=duration=12.800000000", graph)
        self.assertEqual(args[args.index("-t") + 1], "12.800000000")
        self.assertEqual(args[args.index("-fps_mode:v") + 1], "passthrough")
        self.assertIn("[audio]", args)
        self.assertEqual(args[args.index("-c:v") + 1], "h264_nvenc")
        self.assertIn("p5", args)
        self.assertIn("-cq", args)

    def test_silent_source_does_not_create_or_map_audio(self) -> None:
        source_info = MediaInfo(duration=5.0, has_audio=False)
        rendered_info = MediaInfo(duration=10.0, has_audio=False)
        with patch("slow_video.probe_media", side_effect=[source_info, rendered_info]), patch(
            "slow_video.probe_video_fps", return_value=Fraction(30000, 1001)
        ), patch("slow_video.probe_video_stream_duration", return_value=10.0), patch(
            "slow_video.run_ffmpeg", side_effect=self.render
        ) as render:
            slow_video(self.source, 0.5, self.output, self.settings)

        args = render.call_args.args[0]
        graph = args[args.index("-filter_complex") + 1]
        self.assertNotIn("fps=fps=", graph)
        self.assertNotIn("[0:a:0]", graph)
        self.assertNotIn("-c:a", args)
        self.assertNotIn("[audio]", args)

    def test_bad_render_does_not_replace_previous_output(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous good video")
        with patch("slow_video.probe_media", side_effect=[
            MediaInfo(duration=3.0, has_audio=True), MediaInfo(duration=1.0, has_audio=True),
        ]), patch("slow_video.probe_video_fps", return_value=Fraction(25, 1)), patch(
            "slow_video.probe_video_stream_duration", return_value=1.0
        ), patch(
            "slow_video.run_ffmpeg", side_effect=self.render
        ):
            with self.assertRaisesRegex(RuntimeError, "khớp"):
                slow_video(self.source, 0.5, self.output, self.settings)
        self.assertEqual(self.output.read_bytes(), b"previous good video")
        self.assertEqual(list(self.output.parent.glob(".*.mp4")), [])

    def test_rejects_full_audio_container_when_video_track_ends_early(self) -> None:
        """Padded audio must never make an incomplete visual render publish."""
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous good video")
        source_info = MediaInfo(duration=3.2, has_audio=True)
        rendered_info = MediaInfo(duration=12.8, has_audio=True)
        with patch("slow_video.probe_media", side_effect=[source_info, rendered_info]), patch(
            "slow_video.probe_video_fps", return_value=Fraction(25, 1)
        ), patch("slow_video.probe_video_stream_duration", return_value=2.0), patch(
            "slow_video.run_ffmpeg", side_effect=self.render
        ):
            with self.assertRaisesRegex(RuntimeError, "Track hình video tua chậm không khớp"):
                slow_video(self.source, 0.25, self.output, self.settings)
        self.assertEqual(self.output.read_bytes(), b"previous good video")
        self.assertEqual(list(self.output.parent.glob(".*.mp4")), [])

    def test_retries_transient_windows_publish_lock_then_keeps_atomic_publish(self) -> None:
        """Explorer/AV locks after FFmpeg exits should not fail the whole job."""
        source_info = MediaInfo(duration=3.0, has_audio=True)
        rendered_info = MediaInfo(duration=6.0, has_audio=True)
        real_replace = os.replace
        calls = 0

        def replace_after_two_locks(source: str | Path, target: str | Path) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                error = PermissionError(errno.EACCES, "Access is denied")
                error.winerror = 5  # type: ignore[attr-defined]
                raise error
            real_replace(source, target)

        with patch("slow_video.probe_media", side_effect=[source_info, rendered_info]), patch(
            "slow_video.probe_video_fps", return_value=Fraction(25, 1)
        ), patch("slow_video.probe_video_stream_duration", return_value=6.0), patch(
            "slow_video.run_ffmpeg", side_effect=self.render
        ), patch(
            "slow_video.os.replace", side_effect=replace_after_two_locks
        ), patch("slow_video.time.sleep") as sleep:
            result = slow_video(self.source, 0.5, self.output, self.settings)

        self.assertEqual(result, self.output.resolve())
        self.assertEqual(self.output.read_bytes(), b"rendered")
        self.assertEqual(calls, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5])

    def test_publish_lock_keeps_old_output_and_explains_how_to_recover(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"last completed slow video")
        source_info = MediaInfo(duration=3.0, has_audio=True)
        rendered_info = MediaInfo(duration=6.0, has_audio=True)

        def permanently_locked(_source: str | Path, _target: str | Path) -> None:
            error = PermissionError(errno.EACCES, "Access is denied")
            error.winerror = 5  # type: ignore[attr-defined]
            raise error

        with patch("slow_video.probe_media", side_effect=[source_info, rendered_info]), patch(
            "slow_video.probe_video_fps", return_value=Fraction(25, 1)
        ), patch("slow_video.probe_video_stream_duration", return_value=6.0), patch(
            "slow_video.run_ffmpeg", side_effect=self.render
        ), patch(
            "slow_video.os.replace", side_effect=permanently_locked
        ), patch("slow_video.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Bản video tua chậm hoàn thành trước đó vẫn được giữ nguyên"):
                slow_video(self.source, 0.5, self.output, self.settings)

        self.assertEqual(self.output.read_bytes(), b"last completed slow video")
        self.assertEqual(list(self.output.parent.glob(".*.mp4")), [])

    def test_rejects_malformed_destination_name_before_rendering(self) -> None:
        malformed = self.root / "output" / "video_slow" / ".mp4"
        with self.assertRaisesRegex(ValueError, "Tên file video tua chậm không hợp lệ"):
            slow_video(self.source, 0.8, malformed, self.settings)

    def test_rejects_source_as_output_and_non_mp4_target(self) -> None:
        with self.assertRaises(ValueError):
            slow_video(self.source, 0.8, self.source, self.settings)
        with self.assertRaises(ValueError):
            slow_video(self.source, 0.8, self.root / "slow.mkv", self.settings)
        other_source = self.root / "source.mkv"
        other_source.write_bytes(b"fixture")
        with self.assertRaises(ValueError):
            slow_video(other_source, 0.8, self.output, self.settings)

    def test_probe_video_fps_prefers_average_then_falls_back_to_rate(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0,
            stdout='{"streams":[{"avg_frame_rate":"30000/1001","r_frame_rate":"30/1"}]}', stderr="",
        )
        with patch("slow_video.find_binary", return_value="ffprobe"), patch(
            "slow_video.subprocess.run", return_value=completed
        ):
            self.assertEqual(probe_video_fps(self.source, self.settings), Fraction(30000, 1001))
        fallback = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0,
            stdout='{"streams":[{"avg_frame_rate":"0/0","r_frame_rate":"25/1"}]}', stderr="",
        )
        with patch("slow_video.find_binary", return_value="ffprobe"), patch(
            "slow_video.subprocess.run", return_value=fallback
        ):
            self.assertEqual(probe_video_fps(self.source, self.settings), Fraction(25, 1))

    def test_probe_video_stream_duration_requires_a_finite_video_duration(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0,
            stdout='{"streams":[{"duration":"12.800000"}]}', stderr="",
        )
        with patch("slow_video.find_binary", return_value="ffprobe"), patch(
            "slow_video.subprocess.run", return_value=completed
        ):
            self.assertEqual(probe_video_stream_duration(self.source, self.settings), 12.8)
        missing = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0, stdout='{"streams":[{}]}', stderr="",
        )
        with patch("slow_video.find_binary", return_value="ffprobe"), patch(
            "slow_video.subprocess.run", return_value=missing
        ):
            with self.assertRaisesRegex(RuntimeError, "Không lấy được thời lượng track hình"):
                probe_video_stream_duration(self.source, self.settings)

    def test_nvenc_is_default_and_cpu_is_explicit_fallback(self) -> None:
        self.assertEqual(video_encoder_args(self.settings)[:2], ["-c:v", "h264_nvenc"])
        cpu = Settings(project_dir=self.root, slow_video_encoder="libx264")
        self.assertEqual(video_encoder_args(cpu)[:2], ["-c:v", "libx264"])
        with self.assertRaises(ValueError):
            Settings(slow_video_encoder="hevc_nvenc")


if __name__ == "__main__":
    unittest.main()
