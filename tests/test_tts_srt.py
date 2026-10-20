from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from common import Subtitle, read_srt, write_srt
from config import Settings
from main import dub_video
from prepare_tts_srt import merge_repeated_cues, prepare_tts_srt


class SpeechSrtTests(unittest.TestCase):
    def test_merge_chain_keeps_first_id_text_start_and_latest_end(self) -> None:
        cues = [Subtitle(7, 12, 13, "Vâng, bệ hạ."), Subtitle(9, 13, 14, "Vâng, bệ hạ!"),
                Subtitle(11, 14.3, 15, "Vâng, bệ hạ"), Subtitle(20, 17, 18, "Vâng, bệ hạ.")]
        self.assertEqual(merge_repeated_cues(cues), [Subtitle(7, 12, 15, cues[0].text), cues[-1]])

    def test_different_words_accents_and_nonadjacent_repeats_are_preserved(self) -> None:
        cues = [Subtitle(1, 0, 1, "Có"), Subtitle(2, 1, 2, "Cò"),
                Subtitle(3, 2, 3, "Không"), Subtitle(4, 3, 4, "Có")]
        self.assertEqual(merge_repeated_cues(cues), cues)

    def test_source_untouched_and_generated_changes_backed_up(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hai SRT Việt 中文 ") as raw:
            source = Path(raw) / "phim_vi.srt"
            cues = [Subtitle(3, 0, 1, "Chào"), Subtitle(7, 1, 2, "Chào")]
            write_srt(source, cues)
            original = source.read_bytes()
            speech = prepare_tts_srt(source)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(read_srt(speech), [Subtitle(3, 0, 2, "Chào")])
            prepare_tts_srt(source)
            self.assertEqual(list(Path(raw).glob('*.bak')), [])
            speech.write_text("edited", encoding="utf-8")
            prepare_tts_srt(source)
            self.assertEqual(next(Path(raw).glob('*.bak')).read_text(encoding='utf-8'), "edited")
            with self.assertRaises(ValueError):
                prepare_tts_srt(source, source)

    def test_pipeline_uses_merged_speech_and_original_display(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "phim_vi.srt"
            write_srt(source, [Subtitle(1, 0, 1, "Chào"), Subtitle(2, 1, 2, "Chào")])
            settings = Settings(output_dir=root)
            with patch("tts_from_srt.tts_from_srt", new_callable=AsyncMock) as tts, patch(
                "mix_audio.mix_audio", return_value=root / "final.mp4"
            ) as mix:
                self.assertEqual(dub_video(root / 'video.mp4', source, settings, burn=True), 0)
            speech = source.with_name('phim_vi_tts.srt')
            tts.assert_awaited_once_with(speech, settings)
            mix.assert_called_once_with(root / 'video.mp4', speech, settings, burn=True, display_srt=source)

    def test_pipeline_forwards_display_only_annotation_without_tts_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "phim_vi.srt"
            annotation = root / "phim_annotation_vi.srt"
            write_srt(source, [Subtitle(1, 0, 1, "Chào")])
            write_srt(annotation, [Subtitle(99, 0, 1, "Tên nhân vật")])
            settings = Settings(output_dir=root)
            with patch("tts_from_srt.tts_from_srt", new_callable=AsyncMock) as tts, patch(
                "mix_audio.mix_audio", return_value=root / "final.mp4"
            ) as mix:
                self.assertEqual(
                    dub_video(root / "video.mp4", source, settings, burn=False, annotation_srt=annotation), 0,
                )

            speech = source.with_name("phim_vi_tts.srt")
            tts.assert_awaited_once_with(speech, settings)
            mix.assert_called_once_with(
                root / "video.mp4", speech, settings, burn=False, display_srt=source,
                annotation_srt=annotation,
            )
