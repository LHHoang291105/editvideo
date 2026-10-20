"""Regression tests for stale RVC renders after changing render settings."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from web_store import ProjectStore


class VoiceOptionStalenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='voice options Viet ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / 'input.mp4'
        self.video.write_bytes(b'video')
        self.store = ProjectStore(self.root)
        self.project = self.store.add(self.video)
        self.project_id = self.project['id']
        models = self.store.settings(self.project_id).voice_models_dir
        models.mkdir(parents=True)
        self.model = models / 'cute.pth'
        self.model.write_bytes(b'model')
        self.index = models / 'cute.index'
        self.index.write_bytes(b'first index')
        self.store.update_voice_options(self.project_id, {'model_id': 'cute', 'preset': 'cute'})
        self.audio = self.store.paths(self.project_id)['voice_audio']
        self.audio.write_bytes(b'converted voice')
        self.store.mark_voice_completed(
            self.project_id, source=self.video, model=self.model, audio=self.audio, video=None,
        )
        self.assertTrue(self.store.voice_status(self.project_id)['result_ready'])

    def test_preset_and_f0_changes_hide_old_render(self) -> None:
        self.store.update_voice_options(self.project_id, {'preset': 'soft', 'post_pitch_semitones': 1.0})

        voice = self.store.voice_status(self.project_id)
        self.assertFalse(voice['result_ready'])
        self.assertTrue(voice['result_stale'])
        self.assertFalse(voice['audio']['exists'])

    def test_replaced_matching_index_hides_old_render(self) -> None:
        # The .pth name is unchanged, but RVC uses a different retrieval index.
        self.index.write_bytes(b'replaced index with different content')

        voice = self.store.voice_status(self.project_id)
        self.assertFalse(voice['result_ready'])
        self.assertTrue(voice['result_stale'])


if __name__ == '__main__':
    unittest.main()
