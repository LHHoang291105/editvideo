"""Web contracts for the project-integrated RVC voice feature."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_app import create_app


class VoiceWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='voice web Việt ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / 'project.mp4'; self.video.write_bytes(b'fixture video')
        self.app = create_app(self.root)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.jobs = self.app.extensions['jobs']
        self.headers = {'X-CSRF-Token': self.app.extensions['csrf']}
        self.project = self.store.add(self.video)
        self.pid = self.project['id']
        self.base = f'/api/projects/{self.pid}'
        models = self.store.settings(self.pid).voice_models_dir
        models.mkdir(parents=True)
        self.model = models / 'cute_female.pth'; self.model.write_bytes(b'model')
        (models / 'cute_female.index').write_bytes(b'index')

    def post(self, path: str, data: dict[str, object]):
        return self.client.post(path, json=data, headers=self.headers)

    def select_model(self) -> None:
        response = self.post(self.base + '/voice/options', {'model_id': 'cute_female', 'preset': 'cute'})
        self.assertEqual(response.status_code, 200)

    def test_voice_public_data_redacts_paths_and_lists_safe_model_ids(self) -> None:
        self.select_model()
        data = self.client.get('/api/projects').json['projects'][0]
        self.assertEqual(data['voice']['models'], [{'id': 'cute_female', 'name': 'cute_female.pth', 'has_index': True}])
        self.assertEqual(data['voice']['options']['model_id'], 'cute_female')
        serialized = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(str(self.model.resolve()), serialized)
        self.assertNotIn(str(self.model.resolve()), self.store.registry.read_text(encoding='utf-8'))

    def test_rvc_preview_uses_fixed_project_paths_and_only_returns_safe_data(self) -> None:
        """An audition must use the selected server-side PTH and app-owned files."""
        self.select_model()
        settings = self.store.settings(self.pid)
        paths = self.store.paths(self.pid)
        expected_source = (settings.temp_dir / 'rvc_model_preview_source.mp3').resolve()
        expected_output = paths['rvc_preview'].resolve()
        expected_work_dir = (settings.temp_dir / 'voice_conversion' / 'audition').resolve()
        calls: dict[str, object] = {}

        def fake_source_preview(output: Path) -> None:
            self.assertEqual(output, expected_source)
            self.assertTrue(output.is_relative_to(self.root / 'projects' / self.pid / 'temp'))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'fixed source mp3')
            calls['source'] = output

        def fake_convert(request, *, audio_output: Path, video_output: Path | None,
                         work_dir: Path, settings):  # type: ignore[no-untyped-def]
            self.assertEqual(request.source, expected_source)
            self.assertEqual(request.model, self.model.resolve())
            self.assertEqual(request.index, self.model.with_suffix('.index').resolve())
            self.assertEqual(audio_output, expected_output)
            self.assertIsNone(video_output)
            self.assertEqual(work_dir, expected_work_dir)
            self.assertEqual(settings, self.store.settings(self.pid))
            self.assertTrue(audio_output.is_relative_to(self.root / 'projects' / self.pid / 'temp'))
            audio_output.parent.mkdir(parents=True, exist_ok=True)
            audio_output.write_bytes(b'rvc preview wav')
            calls['request'] = request

        with patch('web_app.create_edge_preview_sync', side_effect=fake_source_preview) as make_source, \
             patch('web_app.convert_voice', side_effect=fake_convert) as convert:
            response = self.post(self.base + '/voice/preview', {})

        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(
            response.json,
            {
                'model': {'id': 'cute_female', 'label': 'cute_female.pth'},
                'url': f'{self.base}/files/rvc_preview',
            },
        )
        self.assertEqual(make_source.call_count, 1)
        self.assertEqual(convert.call_count, 1)
        self.assertIn('source', calls)
        self.assertIn('request', calls)
        serialized = json.dumps(response.json, ensure_ascii=False)
        self.assertNotIn(str(self.model.resolve()), serialized)
        self.assertNotIn(str(expected_source), serialized)
        self.assertNotIn(str(self.model.with_suffix('.index').resolve()), serialized)

        rendered = self.client.get(response.json['url'])
        self.assertEqual(rendered.status_code, 200)
        self.assertEqual(rendered.data, b'rvc preview wav')
        rendered.close()

    def test_rvc_preview_requires_a_selected_model_before_calling_backends(self) -> None:
        """The audition endpoint cannot be used with a browser-supplied path."""
        with patch('web_app.create_edge_preview_sync') as make_source, \
             patch('web_app.convert_voice') as convert:
            response = self.post(self.base + '/voice/preview', {})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Chọn model RVC .pth', response.json['error'])
        make_source.assert_not_called()
        convert.assert_not_called()

    def test_voice_upload_is_confined_and_becomes_the_selected_source(self) -> None:
        response = self.client.post(
            self.base + '/voice/upload', headers=self.headers,
            data={'voice': (io.BytesIO(b'fake mp3'), 'ngoai.mp3')},
        )
        self.assertEqual(response.status_code, 200)
        voice = response.json['project']['voice']
        self.assertEqual(voice['options']['source_mode'], 'upload')
        self.assertEqual(voice['source']['name'], 'voice_source.mp3')
        self.assertTrue(voice['source']['exists'])
        source = self.store.voice_source_path(self.pid)
        self.assertTrue(source.is_relative_to(self.root / 'projects' / self.pid / 'input' / 'voice'))
        self.assertEqual(source.read_bytes(), b'fake mp3')
        bad = self.client.post(self.base + '/voice/upload', headers=self.headers,
                               data={'voice': (io.BytesIO(b'bad'), 'bad.exe')})
        self.assertEqual(bad.status_code, 400)

    def test_voice_job_receives_only_project_id_and_requires_model_source(self) -> None:
        self.select_model()
        with patch.object(self.jobs, 'start', return_value={'status': 'running'}) as start:
            response = self.post(self.base + '/jobs', {'action': 'voice'})
        self.assertEqual(response.status_code, 200)
        request = start.call_args.args[0]
        self.assertEqual(request['action'], 'voice')
        self.assertEqual(request['project'], self.pid)
        self.assertNotIn('model', request)
        self.assertNotIn(str(self.model.resolve()), json.dumps(request))
        self.assertEqual(self.post(self.base + '/voice/options', {'model_id': '../escape'}).status_code, 400)

    def test_project_voice_source_obeys_slow_review_but_uploaded_audio_does_not(self) -> None:
        self.select_model()
        slow = self.store.slow_render_path(self.pid); slow.write_bytes(b'slow')
        self.store.mark_slow_created(self.pid, .8)
        self.assertEqual(self.post(self.base + '/jobs', {'action': 'voice'}).status_code, 400)
        upload = self.client.post(self.base + '/voice/upload', headers=self.headers,
                                  data={'voice': (io.BytesIO(b'wav'), 'voice.wav')})
        self.assertEqual(upload.status_code, 200)
        with patch.object(self.jobs, 'start', return_value={'status': 'running'}) as start:
            response = self.post(self.base + '/jobs', {'action': 'voice'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[0]['action'], 'voice')

    def test_audio_only_result_ignores_retained_mp4_from_an_older_conversion(self) -> None:
        self.select_model()
        paths = self.store.paths(self.pid)
        audio = paths['voice_audio']; audio.write_bytes(b'first converted wav')
        old_video = paths['voice_video']; old_video.write_bytes(b'first converted mp4')
        self.store.mark_voice_completed(
            self.pid, source=self.video, model=self.model, audio=audio, video=old_video,
        )
        self.assertTrue(self.store.voice_status(self.pid)['result_ready'])

        uploaded = self.store.voice_upload_destination(self.pid, '.wav')
        uploaded.write_bytes(b'uploaded wav source')
        self.store.mark_voice_upload(self.pid, uploaded)
        audio.write_bytes(b'new audio-only conversion')
        self.store.mark_voice_completed(
            self.pid, source=uploaded, model=self.model, audio=audio, video=None,
        )

        voice = self.store.voice_status(self.pid)
        self.assertTrue(old_video.is_file(), 'old MP4 is retained until the user cleans it up')
        self.assertTrue(voice['result_ready'])
        self.assertTrue(voice['audio']['exists'])
        self.assertFalse(voice['video']['exists'])


if __name__ == '__main__':
    unittest.main()

