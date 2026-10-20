"""Regression coverage for removing pseudo Edge voice profiles from projects."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from web_store import ProjectStore


class WebTtsVoiceMigrationTests(unittest.TestCase):
    def test_old_project_pitch_profile_is_forgotten_but_selected_speaker_stays(self) -> None:
        with tempfile.TemporaryDirectory(prefix='web tts migration Việt ') as raw:
            root = Path(raw)
            registry = root / 'web_data' / 'projects.json'
            registry.parent.mkdir()
            registry.write_text(json.dumps({
                'project': {
                    'options': {
                        'tts_voice': 'vi-VN-HoaiMyNeural',
                        'tts_pitch': '+7Hz',
                        'voice_speed': 1.1,
                    },
                },
            }), encoding='utf-8')

            store = ProjectStore(root)

            self.assertEqual(store.projects['project']['options']['tts_voice'], 'vi-VN-HoaiMyNeural')
            self.assertEqual(store.projects['project']['options']['voice_speed'], 1.1)
            self.assertNotIn('tts_pitch', store.projects['project']['options'])
            self.assertEqual(store.projects['project']['tts_pitch_ui_version'], 1)
            persisted = json.loads(registry.read_text(encoding='utf-8'))
            self.assertNotIn('tts_pitch', persisted['project']['options'])

    def test_explicit_slider_pitch_survives_a_later_store_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix='web tts explicit Việt ') as raw:
            root = Path(raw)
            video = root / 'source.mp4'
            video.write_bytes(b'fixture video')
            store = ProjectStore(root)
            project = store.add(video)
            store.update_options(project['id'], {'tts_pitch': '+5Hz'})

            restarted = ProjectStore(root)

            self.assertEqual(restarted.settings(project['id']).tts_pitch, '+5Hz')


if __name__ == '__main__':
    unittest.main()
