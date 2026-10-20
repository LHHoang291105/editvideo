"""Contracts for real Vietnamese Edge voice choices and their audition."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import Settings
from edge_tts_preview import (
    EDGE_VOICES,
    create_edge_preview_sync,
    edge_voice,
    edge_voice_catalog,
)
from web_app import create_app


class EdgeTtsPreviewTests(unittest.TestCase):
    def test_catalog_has_actual_vietnamese_female_and_male_voices_only(self) -> None:
        catalog = edge_voice_catalog()
        self.assertEqual([item['id'] for item in catalog], [voice.id for voice in EDGE_VOICES])
        self.assertEqual(catalog[0]['label'], 'Hoài My · Nữ')
        self.assertEqual(catalog[1]['label'], 'Nam Minh · Nam')
        with self.assertRaisesRegex(ValueError, 'Giọng Edge'):
            edge_voice('../cute')
        # Pitch remains a technical .env option, not a browser profile.
        self.assertEqual(replace(Settings(), tts_pitch='-20Hz').tts_pitch, '-20Hz')
        with self.assertRaises(ValueError):
            replace(Settings(), tts_pitch='+21Hz')

    def test_preview_uses_selected_actual_voice_and_replaces_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix='edge preview Việt ') as raw:
            output = Path(raw) / 'preview.mp3'
            output.write_bytes(b'old sample')
            calls: list[tuple[str, dict[str, object]]] = []

            class FakeCommunicator:
                def __init__(self, _text: str, voice: str, **kwargs: object) -> None:
                    calls.append((voice, kwargs))

                async def save(self, path: str) -> None:
                    Path(path).write_bytes(b'a' * 1024)

            with patch('edge_tts.Communicate', FakeCommunicator):
                voice = create_edge_preview_sync(
                    output, voice_id='vi-VN-NamMinhNeural', pitch='+0Hz',
                )
            self.assertEqual(voice.id, 'vi-VN-NamMinhNeural')
            self.assertEqual(calls[0][0], 'vi-VN-NamMinhNeural')
            self.assertEqual(calls[0][1]['pitch'], '+0Hz')
            self.assertEqual(calls[0][1]['boundary'], 'WordBoundary')
            self.assertEqual(output.read_bytes(), b'a' * 1024)

            class BrokenCommunicator:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                async def save(self, _path: str) -> None:
                    raise OSError('network offline')

            with patch('edge_tts.Communicate', BrokenCommunicator):
                with self.assertRaisesRegex(RuntimeError, 'Không tạo được'):
                    create_edge_preview_sync(output)
            self.assertEqual(output.read_bytes(), b'a' * 1024)

    def test_loopback_endpoint_uses_selected_voice_and_fixed_project_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix='edge web Việt ') as raw:
            root = Path(raw)
            video = root / 'source.mp4'
            video.write_bytes(b'video')
            app = create_app(root)
            store = app.extensions['store']
            project = store.add(video)
            store.update_options(project['id'], {'tts_pitch': '+4Hz'})
            path = store.paths(project['id'])['tts_preview']

            def fake_preview(output: Path, *, voice_id: str, pitch: str):
                self.assertEqual(output, path)
                self.assertEqual(voice_id, 'vi-VN-NamMinhNeural')
                self.assertEqual(pitch, '+4Hz')
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b'preview mp3')
                return edge_voice(voice_id)

            client = app.test_client()
            headers = {'X-CSRF-Token': app.extensions['csrf']}
            with patch('web_app.create_edge_preview_sync', side_effect=fake_preview):
                response = client.post(
                    f'/api/projects/{project["id"]}/tts-preview',
                    json={'voice': 'vi-VN-NamMinhNeural'}, headers=headers,
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['voice']['id'], 'vi-VN-NamMinhNeural')
            rendered = client.get(response.json['url'])
            self.assertEqual(rendered.status_code, 200)
            self.assertEqual(rendered.data, b'preview mp3')
            rendered.close()
            self.assertEqual(
                client.post(
                    f'/api/projects/{project["id"]}/tts-preview',
                    json={'voice': '../cute'}, headers=headers,
                ).status_code,
                400,
            )


if __name__ == '__main__':
    unittest.main()
