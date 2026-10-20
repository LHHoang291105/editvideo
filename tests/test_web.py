"""Web contracts: edit conflicts, cleanup confinement and job routing."""
from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from common import Subtitle, read_srt, write_srt
from web_app import create_app
from web_store import safe_path


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='web Việt 中文 ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(self.root)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.jobs = self.app.extensions['jobs']
        self.headers = {'X-CSRF-Token': self.app.extensions['csrf']}
        self.video = self.root / 'video 中文.mp4'
        self.video.write_bytes(b'test fixture video')
        p = self.store.add(self.video)
        self.pid = p['id']
        self.base = f'/api/projects/{self.pid}'
        self.paths = self.store.paths(self.pid)
        self.settings = self.store.settings(self.pid)

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def test_page_static_and_host_origin_csrf_protection(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('VietDub Studio', response.text)
        self.assertIn('frame-ancestors', response.headers['Content-Security-Policy'])
        with self.client.get('/static/app.js') as static:
            self.assertEqual(static.status_code, 200)
            self.assertEqual(static.headers['Cache-Control'], 'no-store')
            self.assertIn('copyChineseSrt', static.text)
            self.assertIn('copyAnnotationChineseSrt', static.text)
            self.assertIn('preloadAnnotationChineseSrt', static.text)
            self.assertIn('annotationCopyButton', static.text)
            self.assertIn('navigator.clipboard', static.text)
            self.assertIn("source-mode", static.text)
            self.assertIn("slow_speed", static.text)
            self.assertIn("beginAnnotationDraw", static.text)
            self.assertIn("annotation_ocr", static.text)
            self.assertIn("annotation_ocr_enhanced", static.text)
            self.assertIn("annotationCoverage", static.text)
            self.assertIn("annotation_burn", static.text)
            self.assertIn("Nhập SRT Việt từ GPT", static.text)
            self.assertIn("Đã dịch ${coverageDetails.translated}/${coverageDetails.total} câu", static.text)
        self.assertIn('id="copy-zh-srt"', response.text)
        self.assertIn('Tua chậm video Trung', response.text)
        self.assertIn('id="annotation-overlay"', response.text)
        self.assertIn('id="draw-annotation"', response.text)
        self.assertIn('id="annotation_font_size"', response.text)
        self.assertIn('id="annotation_display_y"', response.text)
        self.assertIn('id="import-annotation-srt"', response.text)
        self.assertIn('id="annotation-burn"', response.text)
        self.assertIn('id="annotation-ocr-enhanced"', response.text)
        self.assertIn('id="copy-annotation-zh"', response.text)
        # Every persisted setting used by app.js must have a corresponding DOM
        # control.  Missing hidden annotation coordinates previously aborted
        # project loading with "Cannot read properties of null (reading 'value')".
        option_match = re.search(r"const optionNames = \[(.*?)\];", static.text, re.DOTALL)
        self.assertIsNotNone(option_match)
        option_ids = set(re.findall(r"'([^']+)'", option_match.group(1)))
        document_ids = set(re.findall(r'\bid="([^"]+)"', response.text))
        self.assertFalse(option_ids - document_ids, option_ids - document_ids)
        # Literal DOM lookups cover buttons, status text and media controls that
        # are not persisted project options.  Keep this next to the option
        # assertion so a template/JS change cannot fail only in the browser.
        literal_ids = set(re.findall(r"\$\('([^']+)'\)", static.text))
        self.assertFalse(literal_ids - document_ids, literal_ids - document_ids)
        self.assertEqual(self.client.post('/api/projects', json={'path': str(self.video)}).status_code, 403)
        self.assertEqual(self.client.post('/api/projects', json={'path': str(self.video)},
                         headers={**self.headers, 'Origin':'https://other.example'}).status_code, 403)
        self.assertEqual(self.client.get('/', headers={'Host':'other.example'}).status_code, 400)

    def test_project_isolation_and_no_secret_serialization(self):
        other = self.store.add(self.video)
        self.assertNotEqual(self.settings.audio_dir, self.store.settings(other['id']).audio_dir)
        response = self.client.get('/api/projects')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('gemini_api_key', response.text)

    def test_upload_filename_cannot_escape_workspace(self):
        response = self.client.post('/api/upload', headers=self.headers,
                                    data={'video':(io.BytesIO(b'mp4 fixture'), '../../escape.mp4')})
        self.assertEqual(response.status_code, 200)
        path = Path(response.json['project']['video'])
        self.assertTrue(path.is_relative_to(self.root / 'projects'))
        self.assertEqual(path.name, 'video.mp4')

    def test_editor_uses_chinese_reference_and_preserves_ids_times(self):
        write_srt(self.paths['zh'], [Subtitle(7, 1.2, 3.5, '你好'), Subtitle(19, 4, 5, '标记')])
        data = self.client.get(self.base + '/subtitles').json
        self.assertEqual(data['rows'][0]['zh'], '你好')
        self.assertEqual(data['rows'][0]['text'], '')
        data['rows'][0]['text'] = 'Xin chào'
        response = self.post(self.base + '/subtitles', data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_srt(self.paths['vi']), [Subtitle(7, 1.2, 3.5, 'Xin chào')])
        self.assertEqual(len(read_srt(self.paths['zh'])), 2)
        # Same stale browser state may not replace newer edits.
        self.assertEqual(self.post(self.base + '/subtitles', data).status_code, 409)

    def test_edit_backup_and_invalid_input_preservation(self):
        write_srt(self.paths['vi'], [Subtitle(3, 1, 2, 'Lời trước')])
        before = self.paths['vi'].read_bytes()
        data = self.client.get(self.base + '/subtitles').json
        data['rows'][0]['end'] = 0
        self.assertEqual(self.post(self.base + '/subtitles', data).status_code, 400)
        self.assertEqual(self.paths['vi'].read_bytes(), before)
        data['rows'][0].update(end=2, text='Lời sau')
        self.assertEqual(self.post(self.base + '/subtitles', data).status_code, 200)
        self.assertEqual(next(self.settings.output_dir.glob('*.bak')).read_bytes(), before)

    def test_import_srt_filters_empty_and_rejects_malformed(self):
        response = self.post(self.base + '/import-srt', {'revision':'missing',
             'content':'1\n00:00:00,000 --> 00:00:01,000\n\n7\n00:00:02,000 --> 00:00:03,000\nChào'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_srt(self.paths['vi']), [Subtitle(7, 2, 3, 'Chào')])
        revision = response.json['revision']
        self.assertEqual(self.post(self.base + '/import-srt', {'revision':revision,'content':'bad'}).status_code, 400)
        self.assertEqual(read_srt(self.paths['vi'])[0].text, 'Chào')

    def test_file_download_range_and_no_arbitrary_file_access(self):
        response = self.client.get(self.base + '/files/source', headers={'Range':'bytes=0-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'test')
        response.close()
        write_srt(self.paths['zh'], [Subtitle(9, 1.2, 3.4, '你好，师父。')])
        chinese = self.client.get(self.base + '/files/zh')
        self.assertEqual(chinese.status_code, 200)
        self.assertEqual(chinese.data, self.paths['zh'].read_bytes())
        chinese.close()
        write_srt(self.paths['annotation_zh'], [Subtitle(3, 2.0, 4.0, '人物：张三')])
        annotation_chinese = self.client.get(self.base + '/files/annotation_zh')
        self.assertEqual(annotation_chinese.status_code, 200)
        self.assertEqual(annotation_chinese.data, self.paths['annotation_zh'].read_bytes())
        annotation_chinese.close()
        self.assertEqual(self.client.get(self.base + '/files/secret').status_code, 400)

    def test_save_final_dialog_copies_completed_render_to_chosen_name(self):
        self.paths['final'].write_bytes(b'completed render')
        destination = self.root / 'Exports' / 'tap_moi_vietdub.mp4'
        destination.parent.mkdir()
        with patch('web_app.choose_video_save_destination', return_value=destination) as choose:
            response = self.post(self.base + '/save-final-dialog', {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['cancelled'])
        self.assertEqual(response.json['name'], 'tap_moi_vietdub.mp4')
        self.assertEqual(Path(response.json['path']), destination.resolve())
        self.assertEqual(destination.read_bytes(), b'completed render')
        self.assertEqual(choose.call_args.args[0], self.video.parent)
        self.assertEqual(choose.call_args.args[1], 'video 中文_vietdub.mp4')

    def test_save_final_dialog_cancel_and_input_protection(self):
        self.paths['final'].write_bytes(b'completed render')
        with patch('web_app.choose_video_save_destination', return_value=None):
            response = self.post(self.base + '/save-final-dialog', {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['cancelled'])
        original = self.video.read_bytes()
        with patch('web_app.choose_video_save_destination', return_value=self.video):
            response = self.post(self.base + '/save-final-dialog', {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.video.read_bytes(), original)
        other = self.store.add(self.video)
        other_final = self.store.paths(other['id'])['final']
        other_final.write_bytes(b'other project render')
        with patch('web_app.choose_video_save_destination', return_value=other_final):
            response = self.post(self.base + '/save-final-dialog', {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(other_final.read_bytes(), b'other project render')

    def test_save_final_dialog_requires_a_completed_render(self):
        with patch('web_app.choose_video_save_destination') as choose:
            response = self.post(self.base + '/save-final-dialog', {})
        self.assertEqual(response.status_code, 400)
        choose.assert_not_called()

    def test_options_validate_and_voice_speed_does_not_touch_tts_rate(self):
        self.assertEqual(self.post(self.base + '/options', {
            'voice_speed': 1.1, 'tts_pitch': '+4Hz', 'blur_chinese_subtitles': True, 'mirror_video': True,
            'blur_left': .1, 'blur_top': .75, 'blur_right': .9, 'blur_bottom': .95,
            'blur_strength': 18,
        }).status_code, 200)
        settings = self.store.settings(self.pid)
        self.assertEqual(settings.voice_speed, 1.1)
        self.assertEqual(settings.tts_rate, '+0%')
        self.assertTrue(settings.blur_chinese_subtitles)
        self.assertTrue(settings.mirror_video)
        self.assertEqual(settings.blur_strength, 18)
        self.assertEqual(settings.tts_pitch, '+4Hz')
        self.assertEqual(self.post(self.base + '/options', {'voice_speed':0}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'tts_pitch': '+21Hz'}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'tts_pitch': '5'}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'mirror_video':'yes'}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'audio_dir':'C:\\'}).status_code, 400)

    def test_annotation_options_are_validated_and_changing_crop_archives_old_track(self):
        write_srt(self.paths['annotation_zh'], [Subtitle(1, 0, 1, '人物：张三')])
        write_srt(self.paths['annotation_vi'], [Subtitle(1, 0, 1, 'Nhân vật: Trương Tam')])
        self.paths['final'].write_bytes(b'final for old annotation location')

        response = self.post(self.base + '/options', {
            'annotation_ocr_enabled': True,
            'annotation_left': .10,
            'annotation_top': .14,
            'annotation_right': .62,
            'annotation_bottom': .44,
            'annotation_font_size': 42,
            'annotation_display_y': .12,
        })
        self.assertEqual(response.status_code, 200, response.json)
        settings = self.store.settings(self.pid)
        self.assertTrue(settings.annotation_ocr_enabled)
        self.assertEqual(settings.annotation_font_size, 42)
        self.assertEqual(settings.annotation_display_y, .12)
        self.assertEqual(
            (settings.annotation_left, settings.annotation_top, settings.annotation_right, settings.annotation_bottom),
            (.10, .14, .62, .44),
        )
        # Text/timing and the old final render belong to a different on-screen
        # rectangle. They are retained as backups rather than accidentally
        # burned into the new chosen location.
        self.assertFalse(self.paths['annotation_zh'].exists())
        self.assertFalse(self.paths['annotation_vi'].exists())
        self.assertFalse(self.paths['final'].exists())
        self.assertEqual(len(list(self.settings.output_dir.glob('*.annotation-region.*.bak'))), 3)
        public = self.client.get('/api/projects').json['projects'][0]
        self.assertIn('annotation_zh', public['files'])
        self.assertIn('annotation_vi', public['files'])
        self.assertEqual(self.post(self.base + '/options', {'annotation_left': .8}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'annotation_font_size': 101}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'annotation_font_size': 28.5}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'annotation_display_y': .04}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'annotation_display_y': .81}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'annotation_display_y': True}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'annotation_ocr_enabled': 'yes'}).status_code, 400)

    def test_annotation_job_routes_require_a_region_and_its_own_srt(self):
        with patch.object(self.jobs, 'start') as start:
            disabled = self.post(self.base + '/jobs', {'action': 'annotation_ocr'})
        self.assertEqual(disabled.status_code, 400)
        start.assert_not_called()

        with patch.object(self.jobs, 'start') as start:
            enhanced_disabled = self.post(self.base + '/jobs', {'action': 'annotation_ocr_enhanced'})
        self.assertEqual(enhanced_disabled.status_code, 400)
        start.assert_not_called()

        self.assertEqual(self.post(self.base + '/options', {'annotation_ocr_enabled': True}).status_code, 200)
        with patch.object(self.jobs, 'start') as start:
            missing = self.post(self.base + '/jobs', {'action': 'annotation_translate'})
        self.assertEqual(missing.status_code, 400)
        start.assert_not_called()

        with patch.object(self.jobs, 'start', return_value={'status': 'running'}) as start:
            ocr = self.post(self.base + '/jobs', {'action': 'annotation_ocr'})
        self.assertEqual(ocr.status_code, 200, ocr.json)
        self.assertEqual(start.call_args.args[0]['action'], 'annotation_ocr')

        with patch.object(self.jobs, 'start', return_value={'status': 'running'}) as start:
            enhanced = self.post(self.base + '/jobs', {'action': 'annotation_ocr_enhanced'})
        self.assertEqual(enhanced.status_code, 200, enhanced.json)
        self.assertEqual(start.call_args.args[0]['action'], 'annotation_ocr_enhanced')

        write_srt(self.paths['annotation_zh'], [Subtitle(1, 0, 1, '人物：张三')])
        with patch.object(self.jobs, 'start', return_value={'status': 'running'}) as start:
            translated = self.post(self.base + '/jobs', {
                'action': 'annotation_translate', 'api_key': 'annotation-secret',
            })
        self.assertEqual(translated.status_code, 200, translated.json)
        request = start.call_args.args[0]
        self.assertEqual(request['action'], 'annotation_translate')
        self.assertEqual(request['api_key'], 'annotation-secret')
        self.assertNotIn('annotation-secret', self.store.registry.read_text(encoding='utf-8'))

    def test_annotation_coverage_reports_missing_extra_and_shifted_cues(self):
        write_srt(self.paths['annotation_zh'], [
            Subtitle(1, 0, 1, '人物：张三'),
            Subtitle(2, 1, 2, '地点：山门'),
            Subtitle(3, 2, 3, '三天后'),
        ])
        write_srt(self.paths['annotation_vi'], [
            Subtitle(1, 0, 1, 'Nhân vật: Trương Tam'),
            # Same ID but a different timestamp cannot be said to cover the
            # OCR cue, even though FFmpeg would still render it somewhere.
            Subtitle(2, 1.2, 2.2, 'Địa điểm: Sơn môn'),
            Subtitle(4, 3, 4, 'Một câu thừa'),
        ])

        coverage = self.client.get('/api/projects').json['projects'][0]['annotation_coverage']
        self.assertTrue(coverage['available'])
        self.assertEqual(coverage['status'], 'ready')
        self.assertEqual(coverage['zh_count'], 3)
        self.assertEqual(coverage['vi_count'], 3)
        self.assertEqual(coverage['matched_count'], 1)
        self.assertEqual(coverage['missing'], 2)
        self.assertEqual(coverage['missing_count'], 1)
        self.assertEqual(coverage['missing_ids'], [3])
        self.assertEqual(coverage['extra_ids'], [4])
        self.assertEqual(coverage['timing_mismatch_count'], 1)
        self.assertEqual(coverage['timing_mismatches'][0]['id'], 2)
        self.assertFalse(coverage['complete'])
        # The browser receives diagnostics only, never translated/OCR text.
        self.assertNotIn('人物：张三', json.dumps(coverage, ensure_ascii=False))
        self.assertNotIn('Nhân vật: Trương Tam', json.dumps(coverage, ensure_ascii=False))

        write_srt(self.paths['annotation_vi'], [
            Subtitle(1, 0, 1, 'Nhân vật: Trương Tam'),
            Subtitle(2, 1, 2, 'Địa điểm: Sơn môn'),
            Subtitle(3, 2, 3, 'Ba ngày sau'),
        ], overwrite=True)
        complete = self.client.get('/api/projects').json['projects'][0]['annotation_coverage']
        self.assertTrue(complete['complete'])
        self.assertEqual((complete['translated'], complete['total'], complete['missing']), (3, 3, 0))

    def test_manual_annotation_import_returns_partial_coverage_warning(self):
        self.assertEqual(self.post(self.base + '/options', {'annotation_ocr_enabled': True}).status_code, 200)
        write_srt(self.paths['annotation_zh'], [
            Subtitle(1, 0, 1, '人物：张三'),
            Subtitle(2, 1, 2, '地点：山门'),
        ])
        response = self.post(self.base + '/import-annotation-srt', {
            'content': '1\n00:00:00,000 --> 00:00:01,000\nNhân vật: Trương Tam',
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['saved'], 1)
        self.assertIn('1 câu Trung chưa có bản Việt', response.json['warning'])
        coverage = response.json['annotation_coverage']
        self.assertEqual((coverage['translated'], coverage['total'], coverage['missing']), (1, 2, 1))
        self.assertEqual(coverage['missing_count'], 1)
        self.assertFalse(coverage['complete'])
        self.assertEqual(read_srt(self.paths['annotation_vi']), [
            Subtitle(1, 0, 1, 'Nhân vật: Trương Tam'),
        ])

    def test_manual_annotation_import_stays_outside_dialogue_track_and_backs_up_old_file(self):
        # A selected region is a prerequisite.  A free-form import must not
        # accidentally create an annotation layer for an unselected rectangle,
        # but it no longer needs a Chinese OCR SRT: an external video review
        # may create the Vietnamese annotation SRT directly.
        blocked = self.post(self.base + '/import-annotation-srt', {
            'content': '1\n00:00:00,000 --> 00:00:01,000\nNhân vật',
        })
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'annotation_ocr_enabled': True}).status_code, 200)
        first = self.post(self.base + '/import-annotation-srt', {
            'content': '7\n00:00:00,200 --> 00:00:01,100\nNhân vật: Trương Tam',
        })
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(read_srt(self.paths['annotation_vi']), [
            Subtitle(7, .2, 1.1, 'Nhân vật: Trương Tam'),
        ])
        self.assertFalse(self.paths['vi'].exists())
        direct_coverage = first.json['annotation_coverage']
        self.assertEqual(direct_coverage['status'], 'direct_vietnamese')
        self.assertFalse(direct_coverage['available'])
        self.assertEqual((direct_coverage['zh_count'], direct_coverage['vi_count']), (0, 1))

        before = self.paths['annotation_vi'].read_bytes()
        second = self.post(self.base + '/import-annotation-srt', {
            'content': '7\n00:00:00,200 --> 00:00:01,100\nTrương Tam',
        })
        self.assertEqual(second.status_code, 200, second.json)
        backups = list(self.settings.output_dir.glob('*_annotation_vi.srt.*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

        malformed = self.post(self.base + '/import-annotation-srt', {'content': 'không phải SRT'})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(read_srt(self.paths['annotation_vi'])[0].text, 'Trương Tam')

    def test_direct_annotation_burn_needs_only_vietnamese_srt_and_region(self):
        """A GPT/manual annotation track must not require OCR, TTS, or dialogue SRT."""
        self.assertEqual(self.post(self.base + '/options', {'annotation_ocr_enabled': True}).status_code, 200)
        with patch.object(self.jobs, 'start') as start:
            missing = self.post(self.base + '/jobs', {'action': 'annotation_burn'})
        self.assertEqual(missing.status_code, 400)
        start.assert_not_called()

        write_srt(self.paths['annotation_vi'], [
            Subtitle(17, 12.2, 14.3, 'Tên nhân vật: Trương Tam'),
        ])
        self.assertFalse(self.paths['annotation_zh'].exists())
        self.assertFalse(self.paths['vi'].exists())
        self.assertFalse(self.paths['speech'].exists())
        with patch.object(self.jobs, 'start', return_value={'status': 'running'}) as start:
            response = self.post(self.base + '/jobs', {'action': 'annotation_burn'})
        self.assertEqual(response.status_code, 200, response.json)
        request = start.call_args.args[0]
        self.assertEqual(request['action'], 'annotation_burn')
        self.assertFalse(request['burn'])
        self.assertEqual(request['api_key'], '')

    def test_logo_upload_is_confined_replaces_extension_and_is_required_before_render(self):
        first = self.client.post(
            self.base + '/logo/upload', headers=self.headers,
            data={'logo': (io.BytesIO(b'first image bytes'), '../../brand.png')},
        )
        self.assertEqual(first.status_code, 200, first.json)
        public = first.json['project']
        self.assertEqual(public['files']['logo'], {'exists': True, 'name': 'logo.png'})
        first_logo = self.store.paths(self.pid)['logo']
        self.assertTrue(first_logo.is_relative_to(self.root / 'projects' / self.pid / 'input' / 'logo'))
        self.assertEqual(first_logo.read_bytes(), b'first image bytes')
        self.assertEqual(self.store.get(self.pid)['logo_overlay'], {'upload_name': 'logo.png'})
        self.assertNotIn(str(first_logo.resolve()), json.dumps(public, ensure_ascii=False))

        # A failed replacement must preserve both the current metadata and the
        # previous bytes, rather than leaving a blank selected logo.
        rejected = self.client.post(
            self.base + '/logo/upload', headers=self.headers,
            data={'logo': (io.BytesIO(b''), 'new.webp')},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertTrue(first_logo.is_file())
        self.assertEqual(first_logo.read_bytes(), b'first image bytes')
        self.assertEqual(self.store.get(self.pid)['logo_overlay'], {'upload_name': 'logo.png'})

        settings = self.post(self.base + '/options', {
            'burn_logo': True,
            'logo_position': 'bottom-left',
            'logo_width_percent': 18.0,
            'logo_opacity': .75,
            'logo_margin_percent': 4.0,
        })
        self.assertEqual(settings.status_code, 200, settings.json)
        configured = self.store.settings(self.pid)
        self.assertTrue(configured.burn_logo)
        self.assertEqual(configured.logo_position, 'bottom-left')
        self.assertEqual(configured.logo_width_percent, 18.0)
        self.assertEqual(self.post(self.base + '/options', {'logo_position': 'center'}).status_code, 400)
        self.assertEqual(self.post(self.base + '/options', {'logo_opacity': 1.1}).status_code, 400)

        replacement = self.client.post(
            self.base + '/logo/upload', headers=self.headers,
            data={'logo': (io.BytesIO(b'new image bytes'), 'new.webp')},
        )
        self.assertEqual(replacement.status_code, 200, replacement.json)
        logo = self.store.paths(self.pid)['logo']
        self.assertEqual(logo.name, 'logo.webp')
        self.assertEqual(logo.read_bytes(), b'new image bytes')
        self.assertFalse(first_logo.exists())
        self.assertEqual(self.store.get(self.pid)['logo_overlay'], {'upload_name': 'logo.webp'})

        # The preflight is before jobs.start(), so a missing selected image
        # cannot overwrite an old final render.
        self.paths['final'].write_bytes(b'old final video')
        logo.unlink()
        with patch.object(self.jobs, 'start') as start:
            missing = self.post(self.base + '/jobs', {'action': 'all'})
        self.assertEqual(missing.status_code, 400)
        self.assertIn('file logo bị thiếu hoặc rỗng', missing.json['error'])
        start.assert_not_called()
        self.assertEqual(self.paths['final'].read_bytes(), b'old final video')

    def make_slow_review(self, speed: float = .8) -> Path:
        slow = self.store.slow_render_path(self.pid)
        slow.write_bytes(b'slow review fixture')
        self.store.mark_slow_created(self.pid, speed)
        return slow

    def test_slow_job_and_review_gate_use_the_approved_timeline(self):
        with patch.object(self.jobs, 'start', return_value={'status':'running'}) as start:
            response = self.post(self.base + '/jobs', {'action':'slow', 'slow_speed':.75})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[0]['action'], 'slow')
        self.assertEqual(start.call_args.args[0]['slow_speed'], .75)
        self.assertEqual(self.store.settings(self.pid).source_speed, .75)

        write_srt(self.paths['zh'], [Subtitle(1, 0, 1, '你好')])
        write_srt(self.paths['vi'], [Subtitle(1, 0, 1, 'Chào')])
        write_srt(self.paths['annotation_zh'], [Subtitle(1, 0, 1, '人物：张三')])
        write_srt(self.paths['annotation_vi'], [Subtitle(1, 0, 1, 'Nhân vật: Trương Tam')])
        self.paths['speech'].write_text('1\n00:00:00,000 --> 00:00:01,000\nChào\n', encoding='utf-8')
        self.paths['final'].write_bytes(b'old final')
        slow = self.make_slow_review(.75)
        pending = self.client.get('/api/projects').json['projects'][0]
        self.assertEqual(pending['source_state'], 'review_required')
        self.assertTrue(pending['files']['slow']['exists'])
        self.assertEqual(self.post(self.base + '/jobs', {'action':'ocr'}).status_code, 400)
        self.assertEqual(self.post(self.base + '/subtitles', {'rows': [], 'revision':'missing'}).status_code, 400)

        approved = self.post(self.base + '/source-mode', {'mode':'slow'})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json['project']['source_state'], 'approved')
        self.assertEqual(self.store.paths(self.pid)['source'], slow)
        self.assertFalse(self.paths['vi'].exists())
        self.assertFalse(self.paths['annotation_zh'].exists())
        self.assertFalse(self.paths['annotation_vi'].exists())
        self.assertTrue(list(self.settings.output_dir.glob('*.source-changed.*.bak')))
        with patch.object(self.jobs, 'start', return_value={'status':'running'}) as start:
            response = self.post(self.base + '/jobs', {'action':'ocr'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[0]['action'], 'ocr')

    def test_slow_file_route_and_return_to_original_are_confined(self):
        slow = self.make_slow_review()
        response = self.client.get(self.base + '/files/slow')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, slow.read_bytes())
        response.close()
        reset = self.post(self.base + '/source-mode', {'mode':'original'})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json['project']['source_state'], 'original')
        self.assertFalse(slow.exists())

    def test_job_request_requires_saved_subtitles_and_does_not_persist_key(self):
        self.assertEqual(self.post(self.base + '/jobs', {'action':'dub'}).status_code, 400)
        write_srt(self.paths['vi'], [Subtitle(1, 0, 1, 'Chào')])
        with patch.object(self.jobs, 'start', return_value={'status':'running'}) as start:
            result = self.post(self.base + '/jobs', {'action':'dub','burn':True,'api_key':'dummy-secret'})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(start.call_args.args[0]['burn'])
        self.assertNotIn('dummy-secret', self.store.registry.read_text(encoding='utf-8'))

    def test_running_job_blocks_mutations_and_cleanup(self):
        self.jobs.job = {'status':'running'}
        for url, payload in [('/options',{'voice_speed':1.2}), ('/cleanup/preview',{'categories':['temp']}),
                             ('/subtitles',{'rows':[],'revision':'missing'})]:
            self.assertEqual(self.post(self.base+url,payload).status_code, 400)

    def test_cleanup_preview_exact_delete_preserves_source_srt_models_and_code(self):
        temporary = self.settings.temp_dir / 'timeline.f32'
        temporary.write_bytes(b'1234')
        code = self.settings.temp_dir / 'keep.py'; code.write_text('pass')
        model = self.settings.temp_dir / 'model_cache'; model.mkdir(); (model/'weights.bin').write_bytes(b'weights')
        write_srt(self.paths['vi'], [Subtitle(1,0,1,'Chào')])
        audio = self.settings.audio_dir / '001.wav'; audio.write_bytes(b'fixture')
        result = self.post(self.base+'/cleanup/preview', {'categories':['temp','audio']}).json
        self.assertEqual(result['count'], 2)
        self.assertTrue(temporary.exists())
        response = self.post(self.base+'/cleanup/execute', {'token':result['token']})
        self.assertEqual(response.json['removed'], 2)
        for path in (self.video, self.paths['vi'], code, model/'weights.bin'):
            self.assertTrue(path.exists())
        self.assertEqual(self.post(self.base+'/cleanup/execute', {'token':result['token']}).status_code, 400)

    def test_cleanup_rejects_stale_preview_cross_project_and_path_escape(self):
        path=self.settings.temp_dir/'a.mp3';path.write_bytes(b'first')
        preview=self.store.preview_cleanup(self.pid,['temp'])
        path.write_bytes(b'changed bytes')
        with self.assertRaises(ValueError): self.store.execute_cleanup(self.pid,preview['token'])
        self.assertTrue(path.exists())
        preview=self.store.preview_cleanup(self.pid,['temp'])
        other=self.store.add(self.video)
        with self.assertRaises(ValueError): self.store.execute_cleanup(other['id'],preview['token'])
        with self.assertRaises(ValueError): safe_path(self.root/'..'/'outside.mp3',self.root)

    def test_cleanup_never_selects_input_even_if_named_like_output(self):
        self.store.get(self.pid)['video']=str(self.settings.output_dir/'final_video.mp4')
        path=Path(self.store.get(self.pid)['video']);path.write_bytes(b'original')
        self.assertEqual(self.store.cleanup_candidates(self.pid,['renders']), [])

    def test_legacy_connection_is_exclusive(self):
        self.store.add(self.video,legacy=True)
        with self.assertRaises(ValueError): self.store.add(self.video,legacy=True)


if __name__ == '__main__':
    unittest.main()
