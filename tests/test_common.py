from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from common import Subtitle, atomic_write_text, format_timestamp, read_srt, write_srt
from config import PROJECT_DIR, Settings, load_settings
from media import probe_media


class CommonTests(unittest.TestCase):
    def test_empty_cues_and_blank_lines_are_filtered_without_retiming_or_file_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lọc trống 中文 ") as raw:
            path = Path(raw) / "phụ đề.srt"
            original = (
                "1\n00:00:00,000 --> 00:00:01,000\n\n"
                "7\n00:00:12,200 --> 00:00:14,300\n\nXin chào\n\nSư phụ!\n\n"
                "9\n00:00:15,000 --> 00:00:16,000\n \t\n\n"
                "20\n00:00:30,000 --> 00:00:31,000\n你好\n\n"
                "99\n00:00:40,000 --> 00:00:41,000"
            ).encode("utf-8")
            path.write_bytes(original)
            self.assertEqual(read_srt(path), [
                Subtitle(7, 12.2, 14.3, "Xin chào\nSư phụ!"),
                Subtitle(20, 30, 31, "你好"),
            ])
            self.assertEqual(path.read_bytes(), original)

    def test_empty_cues_do_not_hide_invalid_timestamps_or_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bad.srt"
            for text in (
                "1\n00:00:02,000 --> 00:00:01,000\n\n",
                "1\n00:70:00,000 --> 00:70:01,000\n\n",
                "1\n00:00:00,000 --> 00:00:01,000\n\n1\n00:00:02,000 --> 00:00:03,000\nLời thoại",
                "1\n00:00:00,000 --> 00:00:01,000\nLời thoại\n\n00:00:02,000 --> 00:00:03,000\nMất ID",
            ):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    read_srt(path)

    def test_all_empty_cues_produce_no_speech(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "empty.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8")
            self.assertEqual(read_srt(path), [])

    def test_zero_width_only_cues_are_filtered_before_tts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "invisible.srt"
            original = (
                "1\n00:00:00,000 --> 00:00:01,000\n\u200b\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nXin\u200b chào\n"
            )
            path.write_text(original, encoding="utf-8")
            self.assertEqual(read_srt(path), [Subtitle(2, 1, 2, "Xin chào")])
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            with self.assertRaises(ValueError):
                write_srt(path, [Subtitle(3, 2, 3, "\u200b")], overwrite=True)

    def test_unicode_srt_roundtrip_and_exclusive_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phim 中文 ") as raw:
            path = Path(raw) / "phụ đề.srt"
            rows = [Subtitle(7, 12.2, 14.3, "你好\nXin chào"), Subtitle(99, 14.8, 15.2, "sư phụ")]
            write_srt(path, rows)
            self.assertEqual(rows, read_srt(path))
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_srt(path, [Subtitle(1, 0, 1, "Thay đổi")])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path], list(path.parent.iterdir()))
            path.write_bytes(b"\xef\xbb\xbf" + original.replace(b"\n", b"\r\n"))
            self.assertEqual(rows, read_srt(path))

    def test_srt_invalid_ids_times_and_blank_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bad.srt"
            invalid = [
                [Subtitle(1, 0, 1, "a"), Subtitle(1, 1, 2, "b")],
                [Subtitle(1, 1, 2, "a"), Subtitle(2, 0, 1, "b")],
                [Subtitle(1, 0, 0, "a")], [Subtitle(1, float("nan"), 1, "a")],
                [Subtitle(1, 0, 1, "")], [Subtitle(1, 0, 1, "a\n\nb")],
            ]
            for rows in invalid:
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    write_srt(path, rows)
            self.assertFalse(path.exists())
            self.assertEqual(format_timestamp(3599.9996), "01:00:00,000")

    def test_config_paths_use_project_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cấu hình ") as raw:
            env = Path(raw) / "other.env"
            env.write_text("OUTPUT_DIR=output/phim Việt\nSAMPLE_FPS=6\nGEMINI_API_KEY=local\n", encoding="utf-8")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "environment"}, clear=True):
                settings = load_settings(env)
            self.assertEqual(settings.output_dir, PROJECT_DIR / "output" / "phim Việt")
            self.assertEqual(settings.sample_fps, 6)
            self.assertEqual(settings.gemini_api_key, "environment")

    def test_bad_settings_fail_early(self) -> None:
        for values in ({"sample_fps": 0}, {"roi_top": 1.1}, {"original_volume": -1},
                       {"blur_top": 1.1}, {"blur_strength": 0},
                       {"sample_fps": float("nan")}, {"tts_rate": "+25%"},
                       {"annotation_display_y": .04}, {"annotation_display_y": .81},
                       {"annotation_display_y": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Settings(), **values)

    def test_ffprobe_na_stream_duration_falls_back_to_container(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "video.mp4"
            path.touch()
            payload = {"streams": [{"codec_type": "video", "duration": "N/A"}, {"codec_type": "audio"}],
                       "format": {"duration": "15.5"}}
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
            with patch("media.find_binary", return_value="ffprobe"), patch("media.subprocess.run", return_value=result):
                info = probe_media(path, Settings())
            self.assertEqual(info.duration, 15.5)
            self.assertTrue(info.has_audio)

    def test_ffprobe_container_duration_wins_when_video_stream_is_truncated(self) -> None:
        """A long render can expose a shorter per-video-stream duration.

        FFmpeg's final ``out_time`` for the reported failure was
        ``03:44:14.375011`` (13454.375011 seconds), while FFprobe reported
        2107.300 seconds on the H.264 stream.  The MP4 container duration is
        the full render timeline and must be used for slow-video validation.
        """
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "video_slow.mp4"
            path.touch()
            payload = {
                "streams": [
                    {"codec_type": "video", "duration": "2107.300000"},
                    {"codec_type": "audio", "duration": "13454.375011"},
                ],
                "format": {"duration": "13454.375011"},
            }
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
            with patch("media.find_binary", return_value="ffprobe"), patch(
                "media.subprocess.run", return_value=result
            ):
                info = probe_media(path, Settings())

            self.assertAlmostEqual(info.duration, 13454.375011, places=6)
            self.assertTrue(info.has_audio)

    def test_ffprobe_invalid_container_duration_falls_back_to_video_stream(self) -> None:
        """Container-first probing must still support files without format duration."""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "video.mp4"
            path.touch()
            payload = {
                "streams": [{"codec_type": "video", "duration": "15.5"}],
                "format": {"duration": "N/A"},
            }
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
            with patch("media.find_binary", return_value="ffprobe"), patch(
                "media.subprocess.run", return_value=result
            ):
                info = probe_media(path, Settings())

            self.assertEqual(info.duration, 15.5)
            self.assertFalse(info.has_audio)

    def test_all_cli_help_from_other_working_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="CLI Việt 中文 ") as raw:
            for script in ("main.py", "extract_subtitles.py", "translate_subtitles.py", "tts_from_srt.py",
                           "mix_audio.py", "burn_subtitle.py", "check_environment.py", "prepare_tts_srt.py", "web_app.py"):
                result = subprocess.run([sys.executable, str(PROJECT_DIR / script), "--help"],
                                        cwd=raw, capture_output=True, encoding="utf-8", errors="replace")
                self.assertEqual(result.returncode, 0, msg=f"{script}: {result.stderr}")
                self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
