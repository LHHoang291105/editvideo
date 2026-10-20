"""Grouping regression tests require no OCR model or network connection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import Settings
from common import Subtitle
from extract_subtitles import (
    ENHANCED_ANNOTATION_DET_MODEL, ENHANCED_ANNOTATION_REC_MODEL,
    OCRRegion, OCRSample, _parse_ocr_results, enhanced_annotation_settings,
    extract_region_subtitles, group_samples, texts_match,
)


class SubtitleGroupingTests(unittest.TestCase):
    def test_final_subtitle_covers_long_last_frame_until_video_end(self) -> None:
        cues = group_samples([OCRSample(0.0, "最后一句"), OCRSample(1.0, "最后一句")], 2.0, self.settings)
        self.assertEqual([(cue.start, cue.end) for cue in cues], [(0.0, 2.0)])

    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            sample_fps=4.0, fuzzy_threshold=0.84, ocr_max_gap=0.35, ocr_min_duration=0.2
        )

    def test_repeated_text_uses_sample_boundaries_and_majority_vote(self) -> None:
        samples = [
            OCRSample(0, ""),
            OCRSample(0.25, "我们今天一起去修仙吧", 0.9),
            OCRSample(0.5, "我们今天一起去修仙吧", 0.9),
            OCRSample(0.75, "我们今天一起去修仟吧", 0.7),
            OCRSample(1, "另一句话"),
            OCRSample(1.25, ""),
        ]
        result = group_samples(samples, 1.5, self.settings)
        self.assertEqual(len(result), 2)
        self.assertEqual((result[0].start, result[0].end), (0.25, 1.0))
        self.assertEqual(result[0].text, "我们今天一起去修仙吧")
        self.assertEqual((result[1].id, result[1].start, result[1].end), (2, 1.0, 1.25))

    def test_one_missed_sample_is_bridged_but_long_gap_is_not(self) -> None:
        samples = [
            OCRSample(0, "你好"), OCRSample(0.25, ""), OCRSample(0.5, "你好"),
            OCRSample(0.75, ""), OCRSample(1, ""), OCRSample(1.25, ""),
            OCRSample(1.5, "你好"),
        ]
        result = group_samples(samples, 1.75, self.settings)
        self.assertEqual([(s.start, s.end) for s in result], [(0, 0.75), (1.5, 1.75)])

    def test_short_different_answers_are_not_fuzzy_merged(self) -> None:
        self.assertFalse(texts_match("是我", "是他"))
        result = group_samples([OCRSample(0, "是我"), OCRSample(0.25, "是他")], 0.5, self.settings)
        self.assertEqual(len(result), 2)

    def test_fuzzy_chain_cannot_drift_into_another_sentence(self) -> None:
        samples = [
            OCRSample(0, "一二三四五六七八九十"),
            OCRSample(0.25, "甲二三四五六七八九十"),
            OCRSample(0.5, "甲乙三四五六七八九十"),
        ]
        result = group_samples(samples, 0.75, self.settings)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].end, 0.5)

    def test_two_errors_in_long_line_can_match(self) -> None:
        self.assertTrue(texts_match("我们今天一起去山上寻找长生不老之药", "我们今天一齐去山上寻找长生不老之葯"))
        self.assertTrue(texts_match("你 好！", "你好。"))

    def test_last_subtitle_clips_to_real_video_duration(self) -> None:
        result = group_samples([OCRSample(0.75, "结束"), OCRSample(1.0, "结束")], 1.1, self.settings)
        self.assertEqual(result[-1].end, 1.1)

    def test_invalid_or_backward_timestamps_rejected(self) -> None:
        for samples in [[OCRSample(-1, "你好")], [OCRSample(float("nan"), "你好")],
                        [OCRSample(0.5, "你好"), OCRSample(0.25, "你好")]]:
            with self.assertRaises(ValueError):
                group_samples(samples, 2, self.settings)

    def test_paddle_v3_results_sorted_and_low_confidence_removed(self) -> None:
        results = [{
            "rec_texts": ["右边", "左边", "下一行", "噪音"],
            "rec_scores": [0.9, 0.95, 0.8, 0.2],
            "rec_polys": [
                [[60, 11], [90, 11], [90, 21], [60, 21]],
                [[10, 10], [40, 10], [40, 20], [10, 20]],
                [[10, 40], [50, 40], [50, 50], [10, 50]],
                [[10, 0], [20, 0], [20, 10], [10, 10]],
            ],
        }]
        text, score = _parse_ocr_results(results, 0.6)
        self.assertEqual(text, "左边 右边\n下一行")
        self.assertGreater(score, 0.8)

    def test_region_uses_normalized_settings_and_rejects_invalid_bounds(self) -> None:
        settings = replace(
            Settings(), roi_left=.05, roi_top=.70, roi_right=.95, roi_bottom=.98,
            annotation_left=.12, annotation_top=.18, annotation_right=.64, annotation_bottom=.42,
        )
        self.assertEqual(OCRRegion.subtitle_region(settings), OCRRegion(.05, .70, .95, .98))
        self.assertEqual(OCRRegion.annotation_region(settings), OCRRegion(.12, .18, .64, .42))
        for region in (
            OCRRegion(.6, .1, .4, .5),
            OCRRegion(.1, -.1, .4, .5),
            OCRRegion(.1, .1, 1.1, .5),
            OCRRegion(float("nan"), .1, .4, .5),
            OCRRegion(True, .1, .4, .5),
        ):
            with self.subTest(region=region), self.assertRaises(ValueError):
                region.validate()

    def test_enhanced_annotation_settings_use_server_models_without_mutating_base(self) -> None:
        """The slow, stronger annotation mode must keep the selected GPU/ROI."""
        base = replace(
            Settings(),
            ocr_device="gpu:0",
            annotation_left=.12,
            annotation_top=.18,
            annotation_right=.64,
            annotation_bottom=.42,
        )

        enhanced = enhanced_annotation_settings(base)

        self.assertIsNot(enhanced, base)
        self.assertEqual(enhanced.ocr_det_model, ENHANCED_ANNOTATION_DET_MODEL)
        self.assertEqual(enhanced.ocr_rec_model, ENHANCED_ANNOTATION_REC_MODEL)
        self.assertEqual(enhanced.ocr_device, "gpu:0")
        self.assertEqual(OCRRegion.annotation_region(enhanced), OCRRegion(.12, .18, .64, .42))
        self.assertEqual(base.ocr_det_model, "PP-OCRv5_mobile_det")
        self.assertEqual(base.ocr_rec_model, "PP-OCRv5_mobile_rec")

    def test_extract_custom_region_passes_only_that_crop_to_sampling(self) -> None:
        """The middle-frame annotation path must not silently revert to lower ROI."""
        with tempfile.TemporaryDirectory(prefix="ocr_annotation_") as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            output = root / "video_annotation_zh.srt"
            video.write_bytes(b"video fixture")
            settings = replace(
                Settings(), project_dir=root, output_dir=root / "output", temp_dir=root / "temp",
                ocr_min_duration=.1,
            )
            region = OCRRegion(.18, .22, .72, .46)
            samples = iter([OCRSample(0, "人物：张三"), OCRSample(.25, "人物：张三")])
            with patch("media.probe_media", return_value=SimpleNamespace(duration=.5)), patch(
                "extract_subtitles._make_ocr", return_value=object()
            ), patch("extract_subtitles.iter_video_samples", return_value=samples) as sample_video:
                cues = extract_region_subtitles(video, output, settings, region, label="chú thích giữa hình")

            self.assertEqual(cues, [Subtitle(1, 0, .5, "人物：张三")])
            self.assertEqual(sample_video.call_args.kwargs["region"], region)
            self.assertEqual(sample_video.call_args.kwargs["progress_name"], "OCR chú thích giữa hình")
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
