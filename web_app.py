"""Loopback-only web application for the Windows video translation pipeline."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
import uuid
import webbrowser

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from common import Subtitle, normalize_subtitle_text, read_srt, setup_logging, validate_subtitles, write_srt
from config import PROJECT_DIR
from edge_tts_preview import create_edge_preview_sync, edge_voice
from voice_conversion import VoiceConversionRequest, convert_voice
from web_jobs import JobManager
from web_store import LOGO_UPLOAD_SUFFIXES, ProjectStore, revision, safe_path


MAX_LOGO_UPLOAD_BYTES = 25 * 1024 * 1024


def choose_video_save_destination(initial_directory: Path, initial_name: str) -> Path | None:
    """Open the native Windows Save As dialog and return a chosen MP4 path.

    The app is loopback-only, so a browser's ordinary download location is not
    a useful replacement for an explicit Windows destination chosen by the
    person at the computer.  Tk ships with the standard Windows Python
    installer and exposes the native Explorer-backed file dialog.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError(
            'Không mở được hộp Lưu thành của Windows. Hãy cài Python kèm Tcl/Tk.'
        ) from exc

    dialog_root = None
    try:
        dialog_root = tk.Tk()
        dialog_root.withdraw()
        dialog_root.attributes('-topmost', True)
        dialog_root.update_idletasks()
        selected = filedialog.asksaveasfilename(
            parent=dialog_root,
            title='Lưu video Việt thành',
            initialdir=str(initial_directory),
            initialfile=initial_name,
            defaultextension='.mp4',
            filetypes=[('Video MP4', '*.mp4')],
            confirmoverwrite=True,
        )
    except tk.TclError as exc:
        raise RuntimeError(
            'Không mở được hộp Lưu thành của Windows. Hãy chạy VietDub Studio '
            'trong phiên Windows đang đăng nhập.'
        ) from exc
    finally:
        if dialog_root is not None:
            try:
                dialog_root.destroy()
            except tk.TclError:
                pass

    if not selected:
        return None
    target = Path(selected).expanduser()
    if target.suffix.lower() != '.mp4':
        raise ValueError('Video xuất phải có đuôi .mp4.')
    return target


def copy_final_video(source: Path, target: Path) -> Path:
    """Atomically copy a completed render to the user-selected location."""
    source = source.resolve()
    target = target.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError('Video kết quả chưa được tạo.')
    if target.suffix.lower() != '.mp4':
        raise ValueError('Video xuất phải có đuôi .mp4.')
    if not target.parent.is_dir():
        raise ValueError('Thư mục đã chọn không còn tồn tại.')
    if target == source:
        raise ValueError('Đây đã là file video kết quả trong project.')
    if target.exists() and target.is_dir():
        raise ValueError('Hãy chọn tên một file MP4, không phải thư mục.')

    # Copy beside the final target first.  A failed copy never leaves a
    # half-written MP4 under the name the user chose.
    temporary = target.with_name(f'.{target.stem}.{uuid.uuid4().hex}.partial.mp4')
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def export_initial_directory(video_path: Path) -> Path:
    """Prefer the input video's folder, then the user's Videos folder."""
    source_folder = video_path.expanduser().parent
    if source_folder.is_dir():
        return source_folder
    videos = Path.home() / 'Videos'
    return videos if videos.is_dir() else Path.home()


def create_app(root: Path = PROJECT_DIR) -> Flask:
    app = Flask(__name__, static_folder='web/static', template_folder='web/templates')
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024**3, TRUSTED_HOSTS=['127.0.0.1', 'localhost', '[::1]'])
    store = ProjectStore(root)
    jobs = JobManager(root)
    csrf = secrets.token_urlsafe(32)
    save_dialog_lock = threading.Lock()
    app.extensions.update(store=store, jobs=jobs, csrf=csrf)

    def reload_store() -> None:
        """See state persisted by the isolated worker before serving a request."""
        store.reload()

    def require_reviewed_processing_source(project_id: str) -> None:
        """Do not let a pending/stale slow preview alter a different timeline."""
        status = store.source_status(project_id)
        if status['source_error']:
            raise ValueError(status['source_error'])
        if status['source_state'] == 'review_required':
            raise ValueError(
                'Hãy xem video tua chậm rồi bấm “Đã xem — dùng video này” '
                'trước khi OCR, sửa phụ đề, tạo giọng hoặc xuất video.'
            )

    @app.before_request
    def local_request():
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf):
                return jsonify(error='Phiên web không hợp lệ. Hãy tải lại trang.'), 403
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(error='Yêu cầu khác nguồn bị từ chối.'), 403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        # The local app is edited in place; avoid serving an old app.js after a restart.
        if request.path.startswith(('/api/', '/static/')) or request.path == '/':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, (ValueError, FileNotFoundError, RuntimeError)):
            return jsonify(error=str(exc)), 400
        if isinstance(exc, PermissionError):
            return jsonify(error='Không có quyền đọc/ghi file. Hãy đóng chương trình đang giữ file và chạy lại.'), 403
        if isinstance(exc, OSError):
            return jsonify(error='Không thao tác được file hoặc tiến trình. Kiểm tra quyền truy cập và dung lượng ổ đĩa.'), 500
        app.logger.error('Web request failed: %s', type(exc).__name__)
        return jsonify(error='Có lỗi nội bộ. Kiểm tra terminal chạy app.'), 500

    @app.get('/')
    def index():
        return render_template('index.html', csrf=csrf)

    @app.get('/api/projects')
    def projects():
        with jobs.lock:
            reload_store()
            return jsonify(projects=[store.public(p) for p in store.projects])

    @app.post('/api/projects')
    def add_project():
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict) or not isinstance(data.get('path'), str):
                raise ValueError('Nhập đường dẫn video.')
            project = store.add(Path(data['path'].strip().strip('"')), legacy=data.get('legacy') is True)
            return jsonify(project=store.public(project['id']))

    @app.post('/api/upload')
    def upload():
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            file = request.files.get('video')
            if not file or not file.filename or Path(file.filename).suffix.lower() != '.mp4':
                raise ValueError('Chọn file MP4, tối đa 8 GB. Video lớn nên dùng đường dẫn local.')
            project_id = uuid.uuid4().hex
            directory = safe_path(store.root / 'projects' / project_id / 'input', store.root)
            directory.mkdir(parents=True)
            destination = directory / 'video.mp4'
            file.save(destination)
            if destination.stat().st_size == 0:
                destination.unlink()
                raise ValueError('File video rỗng.')
            p = store.add(destination, project_id=project_id)
            p['name'] = Path(file.filename.replace('\\', '/')).stem[:150]
            store.persist()
            return jsonify(project=store.public(p['id']))

    @app.post('/api/projects/<project_id>/options')
    def options(project_id):
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('Tùy chọn không hợp lệ.')
            store.update_options(project_id, data)
            return jsonify(project=store.public(project_id))

    @app.post('/api/projects/<project_id>/voice/options')
    def voice_options(project_id):
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('Tùy chọn đổi giọng không hợp lệ.')
            store.update_voice_options(project_id, data)
            return jsonify(project=store.public(project_id))

    @app.post('/api/projects/<project_id>/voice/upload')
    def upload_voice_source(project_id):
        """Copy an audio/video source into this project before RVC reads it."""
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            file = request.files.get('voice')
            suffix = Path(file.filename).suffix.lower() if file and file.filename else ''
            if suffix not in {'.wav', '.mp3', '.mp4'}:
                raise ValueError('Chọn WAV, MP3 hoặc MP4 để đổi giọng.')
            destination = store.voice_upload_destination(project_id, suffix)
            pending = safe_path(destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.pending'), store.root)
            try:
                file.save(pending)
                if not pending.is_file() or pending.stat().st_size == 0:
                    raise ValueError('File nguồn đổi giọng rỗng.')
                os.replace(pending, destination)
                store.mark_voice_upload(project_id, destination)
            finally:
                pending.unlink(missing_ok=True)
            return jsonify(project=store.public(project_id))

    @app.post('/api/projects/<project_id>/logo/upload')
    def upload_logo(project_id):
        """Store a static overlay image under a server-owned project filename."""
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            file = request.files.get('logo')
            suffix = Path(file.filename).suffix.lower() if file and file.filename else ''
            if suffix not in LOGO_UPLOAD_SUFFIXES:
                raise ValueError('Chọn ảnh logo PNG, JPG, JPEG hoặc WebP.')
            # Reject an oversized multipart request before writing when the
            # browser provides a length.  The pending-file check below still
            # protects chunked requests whose length is not known upfront.
            if request.content_length is not None and request.content_length > MAX_LOGO_UPLOAD_BYTES + 1_024_000:
                raise ValueError('Ảnh logo tối đa 25 MB.')
            destination = store.logo_upload_destination(project_id, suffix)
            pending = safe_path(
                destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.pending'), store.root,
            )
            try:
                file.save(pending)
                if not pending.is_file() or pending.stat().st_size == 0:
                    raise ValueError('File logo rỗng.')
                if pending.stat().st_size > MAX_LOGO_UPLOAD_BYTES:
                    raise ValueError('Ảnh logo tối đa 25 MB.')
                # Commit the bytes first, then switch metadata.  A rejected or
                # failed upload therefore leaves the current selected logo in
                # place and never touches the old final video.
                os.replace(pending, destination)
                store.mark_logo_upload(project_id, destination)
            finally:
                pending.unlink(missing_ok=True)
            return jsonify(project=store.public(project_id))

    @app.post('/api/projects/<project_id>/tts-preview')
    def preview_edge_voice(project_id):
        """Create a sample for one actual Vietnamese Edge speaker."""
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            voice_id = data.get('voice') if isinstance(data, dict) else None
            if not isinstance(voice_id, str) or len(voice_id) > 100:
                raise ValueError('Giọng Edge không hợp lệ.')
            voice = edge_voice(voice_id)
            settings = store.settings(project_id)
            output = safe_path(store.paths(project_id)['tts_preview'], store.root)
            create_edge_preview_sync(output, voice_id=voice.id, pitch=settings.tts_pitch)
            return jsonify(
                voice={'id': voice.id, 'label': voice.label},
                url=f'/api/projects/{project_id}/files/tts_preview',
            )

    @app.post('/api/projects/<project_id>/voice/preview')
    def preview_rvc_model(project_id):
        """Render a real short RVC sample for the selected local ``.pth``.

        The request never accepts a model path or audio path from the browser.
        A fixed Vietnamese sample is generated inside this project and passed
        through the same CUDA/RMVPE conversion function as a normal job.  It
        lets the person hear an installed PTH before choosing an input video.
        """
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = store.voice_preview_request_data(project_id)
            settings = store.settings(project_id)
            paths = store.paths(project_id)
            source = safe_path(settings.temp_dir / 'rvc_model_preview_source.mp3', store.root)
            output = safe_path(paths['rvc_preview'], store.root)
            work_dir = safe_path(settings.temp_dir / 'voice_conversion' / 'audition', store.root)
            # Edge supplies only the neutral Vietnamese source sentence.  The
            # returned WAV is created by the selected offline RVC model.
            create_edge_preview_sync(source)
            conversion = VoiceConversionRequest(
                source=source,
                model=data['model'],
                index=data['index'],
                preset=data['preset'],
                speaker_id=data['speaker_id'],
                pitch_semitones=data['pitch_semitones'],
                index_rate=data['index_rate'],
                rms_mix_rate=data['rms_mix_rate'],
                protect=data['protect'],
                post_pitch_semitones=data['post_pitch_semitones'],
            )
            convert_voice(
                conversion,
                audio_output=output,
                video_output=None,
                work_dir=work_dir,
                settings=settings,
            )
            return jsonify(
                model={'id': data['model_id'], 'label': data['model_label']},
                url=f'/api/projects/{project_id}/files/rvc_preview',
            )

    @app.get('/api/projects/<project_id>/subtitles')
    def subtitles(project_id):
        reload_store()
        paths = store.paths(project_id)
        zh = read_srt(paths['zh']) if paths['zh'].exists() else []
        vi = read_srt(paths['vi']) if paths['vi'].exists() else []
        source = paths['vi'] if paths['vi'].exists() else paths['zh']
        by_id = {c.id: c.text for c in zh}
        rows = [{**asdict(c), 'zh': by_id.get(c.id, ''), 'text': c.text if paths['vi'].exists() else ''}
                for c in (vi if paths['vi'].exists() else zh)]
        return jsonify(rows=rows, revision=revision(source), vietnamese=paths['vi'].exists())

    def save_cues(project_id: str, cues: list[Subtitle], expected_revision: str):
        require_reviewed_processing_source(project_id)
        paths = store.paths(project_id)
        source = paths['vi'] if paths['vi'].exists() else paths['zh']
        if expected_revision != revision(source):
            return jsonify(error='SRT đã thay đổi ở nơi khác. Tải lại phụ đề trước khi lưu.'), 409
        validate_subtitles(cues)
        if not cues:
            raise ValueError('Cần ít nhất một câu có lời. Chưa ghi đè bản Việt.')
        target = paths['vi']
        if target.exists():
            backup = target.with_name(target.name + '.' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
            shutil.copy2(target, backup)
        write_srt(target, cues, overwrite=True)
        return jsonify(saved=len(cues), revision=revision(target))

    @app.post('/api/projects/<project_id>/subtitles')
    def save_subtitles(project_id):
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('Danh sách phụ đề không hợp lệ.')
            if not isinstance(data.get('rows'), list) or len(data['rows']) > 50000:
                raise ValueError('Danh sách phụ đề không hợp lệ.')
            cues = []
            for row in data['rows']:
                if not isinstance(row, dict) or not isinstance(row.get('text'), str):
                    raise ValueError('Nội dung phụ đề không hợp lệ.')
                if type(row.get('id')) is not int or type(row.get('start')) not in (int, float) or type(row.get('end')) not in (int, float):
                    raise ValueError('ID hoặc thời gian không hợp lệ.')
                text = normalize_subtitle_text(row['text'])
                if text:
                    cues.append(Subtitle(row['id'], row['start'], row['end'], text))
            return save_cues(project_id, cues, data.get('revision', ''))

    @app.post('/api/projects/<project_id>/import-srt')
    def import_srt(project_id):
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('SRT nhập vào không hợp lệ hoặc quá lớn.')
            content = data.get('content')
            if not isinstance(content, str) or len(content) > 5_000_000:
                raise ValueError('SRT nhập vào không hợp lệ hoặc quá lớn.')
            temporary = store.data / (uuid.uuid4().hex + '.srt')
            try:
                temporary.write_text(content, encoding='utf-8')
                return save_cues(project_id, read_srt(temporary), data.get('revision', ''))
            finally:
                temporary.unlink(missing_ok=True)

    @app.post('/api/projects/<project_id>/import-annotation-srt')
    def import_annotation_srt(project_id):
        """Explicitly replace the display-only Vietnamese annotation track.

        This mirrors normal manual SRT import, but intentionally writes only
        the separate annotation file.  It never reaches the dialogue editor,
        TTS manifest, or WAV timeline.  A person may supply this SRT from an
        external video review (for example, GPT's Vietnamese annotation
        output), so Chinese OCR is intentionally *not* a prerequisite.  The
        chosen in-frame region remains required because it defines where the
        display-only text is burned.
        """
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('SRT chú thích nhập vào không hợp lệ hoặc quá lớn.')
            content = data.get('content')
            if not isinstance(content, str) or len(content) > 5_000_000:
                raise ValueError('SRT chú thích nhập vào không hợp lệ hoặc quá lớn.')
            require_reviewed_processing_source(project_id)
            settings = store.settings(project_id)
            if not settings.annotation_ocr_enabled:
                raise ValueError('Hãy khoanh vùng chú thích trên video trước khi nhập SRT Việt.')
            paths = store.paths(project_id)
            temporary = safe_path(store.data / (uuid.uuid4().hex + '.annotation.srt'), store.root)
            try:
                temporary.write_text(content, encoding='utf-8')
                cues = read_srt(temporary)
                validate_subtitles(cues)
                if not cues:
                    raise ValueError('SRT chú thích không có câu có lời. Chưa thay bản Việt.')
                target = paths['annotation_vi']
                if target.exists():
                    backup = target.with_name(
                        target.name + '.' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak'
                    )
                    shutil.copy2(target, backup)
                write_srt(target, cues, overwrite=True)
                coverage = store.annotation_coverage(project_id)
                warning: str | None = None
                if coverage['available'] and not coverage['complete']:
                    missing = coverage['missing_count']
                    timing = coverage['timing_mismatch_count']
                    extra = coverage['extra_count']
                    parts: list[str] = []
                    if missing:
                        parts.append(f'{missing} câu Trung chưa có bản Việt nên sẽ không hiện')
                    elif coverage['coverage_gap_count']:
                        parts.append(f"{coverage['coverage_gap_count']} câu chưa khớp với OCR")
                    if timing:
                        parts.append(f'{timing} mục lệch timestamp và sẽ chèn theo mốc trong SRT Việt')
                    if extra:
                        parts.append(f'{extra} mục Việt không có cue Trung để đối chiếu')
                    if parts:
                        warning = '; '.join(parts) + '.'
                return jsonify(
                    saved=len(cues), revision=revision(target),
                    annotation_coverage=coverage, warning=warning,
                )
            finally:
                temporary.unlink(missing_ok=True)

    @app.post('/api/projects/<project_id>/jobs')
    def start_job(project_id):
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('Yêu cầu tác vụ không hợp lệ.')
            action = data.get('action')
            if action not in (
                'slow', 'ocr', 'translate', 'annotation_ocr', 'annotation_ocr_enhanced',
                'annotation_translate', 'annotation_burn',
                'prepare', 'tts', 'mix', 'dub', 'all', 'voice',
            ):
                raise ValueError('Tác vụ không hợp lệ.')
            voice_data: dict[str, object] | None = None
            if action == 'voice':
                voice_data = store.voice_request_data(project_id)
                if voice_data['source_mode'] == 'project':
                    require_reviewed_processing_source(project_id)
            elif action != 'slow':
                require_reviewed_processing_source(project_id)
            paths = store.paths(project_id)
            slow_speed: float | None = None
            if action == 'slow':
                status = store.source_status(project_id)
                if status['source_error']:
                    raise ValueError(status['source_error'])
                # The rendered preview may never overwrite the slow video
                # currently accepted as the processing source.
                store.slow_render_path(project_id)
                value = data.get('slow_speed', store.settings(project_id).source_speed)
                if type(value) not in (int, float):
                    raise ValueError('Tốc độ video phải là số.')
                # Use Settings as the one validation source and persist the
                # selected value for the next review render.
                slow_speed = float(value)
                store.update_options(project_id, {'source_speed': slow_speed})
                slow_speed = store.settings(project_id).source_speed
                paths = store.paths(project_id)
            if action in ('annotation_ocr', 'annotation_ocr_enhanced', 'annotation_translate', 'annotation_burn') and not store.settings(project_id).annotation_ocr_enabled:
                raise ValueError('Hãy bấm “Khoanh vùng trên video” để đặt vị trí chú thích trước.')
            required = {
                'translate': 'zh', 'annotation_translate': 'annotation_zh',
                'annotation_burn': 'annotation_vi',
                'prepare': 'vi', 'tts': 'vi', 'dub': 'vi', 'mix': 'speech',
            }
            if action in required and not paths[required[action]].exists():
                raise ValueError('Chưa có SRT cần thiết. Hãy hoàn tất bước trước và lưu phụ đề.')
            if action in ('mix', 'dub', 'all', 'annotation_burn') and store.settings(project_id).burn_logo:
                # Resolve only the app-owned image now, before a worker starts
                # or a previous final render can be replaced.
                store.logo_path(project_id)
            media_path = paths['original'] if action == 'slow' else paths['source']
            if action in ('slow', 'ocr', 'annotation_ocr', 'annotation_ocr_enhanced', 'annotation_burn', 'all', 'mix', 'dub') and not media_path.is_file():
                raise ValueError('Không tìm thấy video nguồn ở đường dẫn đã lưu.')
            key = data.get('api_key', '')
            if not isinstance(key, str) or len(key) > 2048:
                raise ValueError('API key không hợp lệ.')
            return jsonify(job=jobs.start({'root': str(store.root), 'project': project_id,
                                           'action': action, 'burn': data.get('burn') is True, 'api_key': key,
                                           'reocr': data.get('reocr') is True,
                                           **({'slow_speed': slow_speed} if slow_speed is not None else {})}))

    @app.post('/api/projects/<project_id>/source-mode')
    def set_source_mode(project_id):
        """Commit the human review decision for the source-video timeline."""
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            mode = data.get('mode') if isinstance(data, dict) else None
            if mode == 'slow':
                result = store.activate_slow_source(project_id)
            elif mode == 'original':
                result = store.activate_original_source(project_id)
            else:
                raise ValueError('Nguồn video phải là "slow" hoặc "original".')
            return jsonify(project=store.public(project_id), **result)

    @app.get('/api/job')
    def job():
        return jsonify(jobs.snapshot(int(request.args.get('after', '0'))))

    @app.post('/api/job/cancel')
    def cancel():
        jobs.cancel()
        return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/cleanup/preview')
    def preview_cleanup(project_id):
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('Chọn nhóm dữ liệu cần dọn.')
            return jsonify(store.preview_cleanup(project_id, data.get('categories', [])))

    @app.post('/api/projects/<project_id>/cleanup/execute')
    def execute_cleanup(project_id):
        with jobs.lock:
            jobs.require_idle()
            reload_store()
            data = request.get_json()
            if not isinstance(data, dict):
                raise ValueError('Bản xem trước không hợp lệ.')
            return jsonify(store.execute_cleanup(project_id, data.get('token', '')))

    @app.post('/api/projects/<project_id>/save-final-dialog')
    def save_final_dialog(project_id):
        """Let the user choose the visible filename and destination for a render."""
        if not save_dialog_lock.acquire(blocking=False):
            raise RuntimeError('Hộp Lưu video thành đang mở.')
        try:
            with jobs.lock:
                jobs.require_idle()
                reload_store()
                project = store.get(project_id)
                source = safe_path(store.paths(project_id)['final'], store.root)
                if not source.is_file():
                    raise FileNotFoundError('Video kết quả chưa được tạo.')
                target = choose_video_save_destination(
                    export_initial_directory(Path(project['video'])),
                    f"{Path(project['video']).stem}_vietdub.mp4",
                )
                if target is None:
                    return jsonify(cancelled=True)
                resolved_target = target.expanduser().resolve()
                protected_paths = {Path(item['video']).expanduser().resolve() for item in store.projects.values()}
                for item in store.projects.values():
                    project_paths = store.paths(item['id'])
                    protected_paths.update({
                        project_paths['final'].resolve(), project_paths['slow'].resolve(),
                        project_paths['source'].resolve(),
                    })
                if resolved_target in protected_paths:
                    raise ValueError(
                        'Không thể ghi đè video đầu vào hoặc bản kết quả của project. '
                        'Hãy chọn tên hoặc thư mục khác.'
                    )
                saved = copy_final_video(source, target)
                return jsonify(cancelled=False, name=saved.name, path=str(saved))
        finally:
            save_dialog_lock.release()

    @app.get('/api/projects/<project_id>/files/<kind>')
    def files(project_id, kind):
        reload_store()
        paths = store.paths(project_id)
        if kind not in paths:
            raise ValueError('Loại file không hợp lệ.')
        path = paths[kind]
        if not path.is_file():
            raise FileNotFoundError('File chưa được tạo.')
        original = paths['original']
        # The person explicitly registered the original local file, which may
        # live outside the workspace.  Every generated file, including an
        # approved slow source, stays inside the project and is confined.
        if path != original:
            safe_path(path, store.root)
        return send_file(path, as_attachment=request.args.get('download') == '1', conditional=True)

    @app.get('/api/projects/<project_id>/audio/<int:cue_id>')
    def audio(project_id, cue_id):
        path = safe_path(store.settings(project_id).audio_dir / f'{cue_id:03d}.wav', store.root)
        if not path.is_file():
            raise FileNotFoundError('Câu này chưa có WAV.')
        return send_file(path, conditional=True)

    return app


def main() -> None:
    # argparse writes --help before parsing; force UTF-8 first for Windows consoles.
    setup_logging()
    parser = argparse.ArgumentParser(description='Video Translator Studio — web local trên Windows')
    parser.add_argument('--port', type=int, default=7860)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Port phải từ 1024 đến 65535.')
    app = create_app()
    url = f'http://127.0.0.1:{args.port}'
    print(f'Mở {url} • Ctrl+C để tắt app', flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    from waitress import serve
    try:
        serve(app, host='127.0.0.1', port=args.port, threads=6, max_request_body_size=8 * 1024**3)
    finally:
        app.extensions['jobs'].cancel()


if __name__ == '__main__':
    main()
