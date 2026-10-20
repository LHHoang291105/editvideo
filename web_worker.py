"""Isolated pipeline worker. Request and API key arrive only through stdin."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys

from common import setup_logging
from web_store import ProjectStore


def main() -> int:
    setup_logging()
    logger = logging.getLogger('worker')
    try:
        request = json.load(sys.stdin)
        store = ProjectStore(Path(request['root']))
        project_id = request['project']
        settings = store.settings(project_id)
        # Share installed OCR weights, not audio or subtitle output, across projects.
        import os
        cache = store.root / 'temp' / 'model_cache'
        os.environ.setdefault('PADDLE_PDX_CACHE_HOME', str(cache / 'paddlex'))
        if request.get('api_key'):
            settings = replace(settings, gemini_api_key=request.pop('api_key'))
        paths = store.paths(project_id)
        action = request['action']
        if action == 'voice':
            # RVC runs in its own Python/CUDA environment.  The web worker
            # stays a small coordinator so PaddleOCR and torch never share an
            # interpreter or GPU dependency set.
            from voice_conversion import VoiceConversionRequest, convert_voice

            data = store.voice_request_data(project_id)
            conversion = VoiceConversionRequest(
                source=data['source'], model=data['model'], index=data['index'], preset=data['preset'],
                speaker_id=data['speaker_id'], pitch_semitones=data['pitch_semitones'],
                index_rate=data['index_rate'], rms_mix_rate=data['rms_mix_rate'], protect=data['protect'],
                post_pitch_semitones=float(data['post_pitch_semitones']),
            )
            logger.info('Bước đổi giọng: RVC + RMVPE GPU, preset %s.', conversion.profile.label)
            result = convert_voice(
                conversion,
                audio_output=paths['voice_audio'],
                video_output=paths['voice_video'] if conversion.source.suffix.lower() == '.mp4' else None,
                work_dir=settings.temp_dir / 'voice_conversion', settings=settings,
            )
            store.mark_voice_completed(
                project_id, source=conversion.source, model=conversion.model,
                audio=result.audio, video=result.video,
            )
            logger.info('Hoàn tất đổi giọng RVC trên GPU.')
            return 0
        if action == 'slow':
            from slow_video import slow_video

            speed = float(request.get('slow_speed', settings.source_speed))
            logger.info('Bước xem trước: tua chậm video Trung %.2fx.', speed)
            slow_video(paths['original'], speed, paths['slow'], settings)
            # The browser only unlocks OCR after the user explicitly approves
            # this newly rendered timeline in the review panel.
            store.mark_slow_created(project_id, speed)
        if action in ('ocr', 'all'):
            from extract_subtitles import extract_subtitles
            logger.info('Bước OCR: đọc vùng phụ đề trên video.')
            reocr = request.get('reocr') is True
            if reocr and paths['zh'].exists():
                import shutil
                from datetime import datetime
                backup = paths['zh'].with_name(paths['zh'].name + '.' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
                shutil.copy2(paths['zh'], backup)
                logger.info('Đã sao lưu SRT Trung trước khi OCR lại. Bản Việt được giữ nguyên.')
            extract_subtitles(paths['source'], paths['zh'], settings, overwrite=reocr)
        if action in ('annotation_ocr', 'annotation_ocr_enhanced'):
            from extract_subtitles import (
                OCRRegion, enhanced_annotation_settings, extract_region_subtitles,
            )
            enhanced = action == 'annotation_ocr_enhanced'
            if enhanced:
                # This explicit recovery path uses the larger OCR pair only
                # for the manually selected label crop.  It is intentionally
                # separate from the fast full-dialogue OCR setting.
                settings = enhanced_annotation_settings(settings)
                logger.info(
                    'Bước OCR tăng cường chú thích: PP-OCRv5 Server trên %s; '
                    'đọc chữ dọc/cách điệu chậm hơn OCR thường.', settings.ocr_device,
                )
            else:
                logger.info('Bước OCR chú thích: đọc vùng bạn đã khoanh trên khung hình.')
            # Enhanced OCR is a recovery operation, so it must replace the
            # incomplete Chinese track after saving a backup even if a caller
            # did not set reocr in a hand-built API request.
            reocr = request.get('reocr') is True or enhanced
            if reocr and paths['annotation_zh'].exists():
                import shutil
                from datetime import datetime
                backup = paths['annotation_zh'].with_name(
                    paths['annotation_zh'].name + '.'
                    + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak'
                )
                shutil.copy2(paths['annotation_zh'], backup)
                logger.info('Đã sao lưu SRT Trung chú thích trước khi OCR lại.')
            extract_region_subtitles(
                paths['source'], paths['annotation_zh'], settings, OCRRegion.annotation_region(settings),
                overwrite=reocr,
                label='chú thích trong khung hình (tăng cường)' if enhanced else 'chú thích trong khung hình',
            )
        if action in ('translate', 'all'):
            from translate_subtitles import translate_subtitles
            logger.info('Bước dịch: Gemini, giữ SRT Việt đã tồn tại.')
            translate_subtitles(paths['zh'], paths['vi'], settings)
        if action == 'annotation_translate':
            from translate_subtitles import translate_subtitles
            logger.info('Bước dịch chú thích: Gemini, giữ SRT chú thích Việt đã tồn tại.')
            translate_subtitles(paths['annotation_zh'], paths['annotation_vi'], settings)
        if action == 'annotation_burn':
            from burn_subtitle import render_video

            # This is deliberately independent of dialogue/TTS files: a
            # person can import an annotation-only Vietnamese SRT created by
            # an external video review, then burn it while preserving the
            # active source video's original audio.
            logo_path = store.logo_path(project_id) if settings.burn_logo else None
            logger.info('Bước chèn chú thích Việt trực tiếp: giữ audio gốc, không tạo giọng.')
            render_video(
                paths['source'], None, paths['final'], settings, burn_subtitles=False,
                logo_path=logo_path, annotation_srt=paths['annotation_vi'],
            )
        if action in ('prepare', 'tts', 'dub', 'all'):
            from prepare_tts_srt import prepare_tts_srt
            prepare_tts_srt(paths['vi'], max_gap=float(request.get('max_gap', 0.3)))
        if action in ('tts', 'dub', 'all'):
            from tts_from_srt import tts_from_srt
            asyncio.run(tts_from_srt(paths['speech'], settings))
        if action in ('mix', 'dub', 'all'):
            from mix_audio import mix_audio
            # The browser stores only logo choices, never a client path.  The
            # project store resolves the selected app-owned image again inside
            # this isolated worker before it reaches FFmpeg.
            logo_path = store.logo_path(project_id) if settings.burn_logo else None
            annotation_srt = (
                paths['annotation_vi']
                if settings.annotation_ocr_enabled and paths['annotation_vi'].is_file() else None
            )
            mix_audio(paths['source'], paths['speech'], settings,
                      burn=bool(request.get('burn')), display_srt=paths['vi'], logo_path=logo_path,
                      annotation_srt=annotation_srt)
        logger.info('Hoàn tất tác vụ.')
        return 0
    except Exception as exc:
        logging.getLogger('worker').error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
