"""Manual workflow must preserve edits and never call translation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import Subtitle, read_srt, write_srt
from config import Settings
import main as pipeline


class ManualTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="dịch tay 中文 ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.video = self.root / "phim.mp4"
        self.video.touch()
        self.settings = Settings(output_dir=self.root)
        self.zh = self.root / "phim_zh.srt"
        self.vi = self.root / "phim_vi.srt"
        self.draft = self.root / "phim_vi.draft.srt"
        self.cues = [Subtitle(7, 1.2, 3.5, "你好")]

    def run_mode(self, mode: str) -> int:
        with patch("main.load_settings", return_value=self.settings), patch(
            "translate_subtitles.translate_subtitles", side_effect=AssertionError("Must not translate")
        ), patch("main.prompt_api_key", side_effect=AssertionError("Must not ask for key")):
            return pipeline.main([str(self.video), mode])

    def test_manual_creates_draft_and_preserves_edits_on_rerun(self) -> None:
        write_srt(self.zh, self.cues)
        self.assertEqual(self.run_mode("--manual-translate"), 0)
        self.assertEqual(read_srt(self.draft), self.cues)
        self.assertFalse(self.vi.exists())
        edited = [Subtitle(7, 1.2, 3.5, "Xin chào")]
        write_srt(self.draft, edited, overwrite=True)
        self.assertEqual(self.run_mode("--manual-translate"), 0)
        self.assertEqual(read_srt(self.draft), edited)

    def test_manual_preserves_existing_vietnamese(self) -> None:
        write_srt(self.zh, self.cues)
        write_srt(self.vi, [Subtitle(7, 1.2, 3.5, "Xin chào")])
        before = self.vi.read_bytes()
        self.assertEqual(self.run_mode("--manual-translate"), 0)
        self.assertEqual(self.vi.read_bytes(), before)
        self.assertFalse(self.draft.exists())

    def test_dub_uses_manual_srt_without_chinese_source(self) -> None:
        write_srt(self.vi, [Subtitle(7, 1.2, 3.5, "Xin chào")])
        with patch("main.dub_video", return_value=0) as dub:
            self.assertEqual(self.run_mode("--dub-only"), 0)
        dub.assert_called_once_with(self.video, self.vi, self.settings, burn=False)
        self.assertFalse(self.zh.exists())

    def test_dub_missing_vietnamese_stops_without_api(self) -> None:
        with patch("main.dub_video") as dub:
            self.assertEqual(self.run_mode("--dub-only"), 1)
        dub.assert_not_called()
