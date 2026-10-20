"""Regression tests for display-only OCR annotations burned through ASS."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from annotation_overlay import _ass_time, build_annotation_ass, write_annotation_ass
from common import Subtitle, read_srt, write_srt
from extract_subtitles import OCRRegion


class AnnotationOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.region = OCRRegion(.10, .20, .70, .50)

    def test_ass_document_preserves_timing_position_and_escapes_text(self) -> None:
        cues = [
            Subtitle(7, 1.2, 3.456, "Tên {nhân vật}\\nguồn\nDòng hai"),
        ]

        document = build_annotation_ass(cues, self.region, font_size=28)

        self.assertIn("PlayResX: 1000", document)
        self.assertIn("Style: Annotation,Arial,28,", document)
        self.assertIn(r"{\an5\pos(400,350)\clip(100,200,700,500)}", document)
        self.assertIn("Dialogue: 0,0:00:01.20,0:00:03.46,Annotation", document)
        # OCR/translation text may include braces, a literal backslash, or a
        # manually edited line break.  None may become ASS control syntax.
        self.assertIn(r"Tên \{nhân vật\}\\nguồn\NDòng hai", document)

    def test_display_y_raises_vietnamese_without_changing_ocr_crop(self) -> None:
        cues = [Subtitle(1, 0, 1, "Tên nhân vật rất dài bằng tiếng Việt")]

        document = build_annotation_ass(cues, self.region, font_size=28, display_y=.12)

        # X/width keep following the selected OCR rectangle, but the output
        # starts high at 12% and can use the full vertical frame.  \an8 makes
        # multi-line Vietnamese grow down rather than above the frame.
        self.assertIn(r"{\an8\pos(400,120)\clip(100,0,700,1000)}", document)
        self.assertNotIn(r"{\an5\pos(400,350)\clip(100,200,700,500)}", document)

    def test_ass_timestamps_and_input_bounds_are_checked(self) -> None:
        self.assertEqual(_ass_time(0), "0:00:00.00")
        self.assertEqual(_ass_time(3661.239), "1:01:01.24")
        with self.assertRaises(ValueError):
            _ass_time(-.01)
        with self.assertRaises(ValueError):
            build_annotation_ass([], self.region, font_size=11)
        with self.assertRaises(ValueError):
            build_annotation_ass([], OCRRegion(.8, .2, .1, .5))
        with self.assertRaises(ValueError):
            build_annotation_ass([], self.region, display_y=.04)
        with self.assertRaises(ValueError):
            build_annotation_ass([], self.region, display_y=.81)
        with self.assertRaises(ValueError):
            build_annotation_ass([], self.region, display_y=True)

    def test_write_annotation_ass_reads_utf8_srt_atomically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="annotation_ass_") as temporary:
            root = Path(temporary)
            source = root / "chú thích Việt.srt"
            destination = root / "annotations.ass"
            cues = [Subtitle(1, 0, 1, "Đệ tử của sư phụ")]
            write_srt(source, cues)

            result = write_annotation_ass(source, destination, self.region, display_y=.12)

            self.assertEqual(result, cues)
            self.assertTrue(destination.is_file())
            self.assertIn("Đệ tử của sư phụ", destination.read_text(encoding="utf-8"))
            self.assertEqual(read_srt(source), cues)
            self.assertIn(r"{\an8\pos(400,120)", destination.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
