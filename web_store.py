"""Local project registry and preview-based, bounded cleanup."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any
import uuid

from common import Subtitle, atomic_write_text, read_srt
from config import PROJECT_DIR, Settings, load_settings
from edge_tts_preview import edge_voice_catalog
from voice_conversion import SUPPORTED_INPUT_SUFFIXES, VOICE_PRESETS, rvc_runtime_status

OPTION_NAMES = (
    'source_speed', 'sample_fps', 'roi_left', 'roi_top', 'roi_right', 'roi_bottom', 'ocr_device',
    'tts_voice', 'tts_pitch', 'original_volume', 'voice_volume', 'voice_speed', 'gemini_model',
    'blur_chinese_subtitles', 'blur_left', 'blur_top', 'blur_right', 'blur_bottom',
    'blur_strength', 'mirror_video',
    'burn_logo', 'logo_position', 'logo_width_percent', 'logo_opacity', 'logo_margin_percent',
    'annotation_ocr_enabled', 'annotation_left', 'annotation_top', 'annotation_right',
    'annotation_bottom', 'annotation_font_size', 'annotation_display_y',
)

BOOLEAN_OPTIONS = {'blur_chinese_subtitles', 'mirror_video', 'burn_logo', 'annotation_ocr_enabled'}
SLOW_METADATA_KEY = 'slow_source'
VOICE_METADATA_KEY = 'voice_conversion'
LOGO_METADATA_KEY = 'logo_overlay'
LOGO_UPLOAD_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.webp'})
SOURCE_STATES = frozenset({'original', 'review_required', 'approved'})
VOICE_SOURCE_MODES = frozenset({'project', 'upload'})
# Older builds presented tiny HoaiMy pitch shifts as distinct female voices.
# Remove that hidden project setting exactly once.  A later explicit slider
# selection is protected by the version marker and remains project-local.
TTS_PITCH_UI_VERSION = 1
TTS_PITCH_UI_VERSION_KEY = 'tts_pitch_ui_version'
LEGACY_TTS_PROFILE_OPTIONS = frozenset({'tts_pitch'})

# A subtitle project can legally contain tens of thousands of cues.  The
# browser needs enough IDs to point a person at the problem, but returning a
# complete list on every project refresh is needlessly expensive and makes the
# public API a poor place to expose an entire SRT index.  Counts remain exact;
# only diagnostic samples are bounded.
ANNOTATION_COVERAGE_DETAIL_LIMIT = 100


def _cue_timing_millis(cue: Subtitle) -> tuple[int, int]:
    """Compare SRT timings at their persisted millisecond precision."""
    return round(cue.start * 1000), round(cue.end * 1000)


def _coverage_timing_detail(chinese: Subtitle, vietnamese: Subtitle) -> dict[str, Any]:
    """Return timing-only diagnostics; never expose subtitle text or paths."""
    return {
        'id': chinese.id,
        'zh': {'start': chinese.start, 'end': chinese.end},
        'vi': {'start': vietnamese.start, 'end': vietnamese.end},
    }


def annotation_translation_coverage(
    chinese: list[Subtitle],
    vietnamese: list[Subtitle],
    *,
    source_exists: bool,
    translation_exists: bool,
    available: bool,
    status: str,
) -> dict[str, Any]:
    """Describe whether a Vietnamese annotation track can cover its OCR track.

    IDs are the stable link between OCR and manually translated SRTs.  A cue is
    counted as translated only when both its ID *and* persisted SRT timestamps
    match.  That keeps a shifted/manual cue visible as a repair target instead
    of silently claiming it will render at the OCR moment.

    This helper deliberately receives already-read cues so callers can retain
    useful counts even if the other file is absent.  It is also used by the
    import endpoint after its intentional file replacement, without changing
    any cue text, IDs, or timings.
    """
    chinese_by_id = {cue.id: cue for cue in chinese}
    vietnamese_by_id = {cue.id: cue for cue in vietnamese}

    missing_ids = [cue.id for cue in chinese if cue.id not in vietnamese_by_id]
    extra_ids = [cue.id for cue in vietnamese if cue.id not in chinese_by_id]
    timing_mismatches = [
        _coverage_timing_detail(chinese_cue, vietnamese_by_id[chinese_cue.id])
        for chinese_cue in chinese
        if chinese_cue.id in vietnamese_by_id
        and _cue_timing_millis(chinese_cue) != _cue_timing_millis(vietnamese_by_id[chinese_cue.id])
    ]
    matched_count = sum(
        1 for cue in chinese
        if cue.id in vietnamese_by_id
        and _cue_timing_millis(cue) == _cue_timing_millis(vietnamese_by_id[cue.id])
    )
    coverage_gap_count = len(chinese) - matched_count
    complete = bool(
        available and chinese and not missing_ids and not extra_ids and not timing_mismatches
    )

    def sample(values: list[Any]) -> list[Any]:
        return values[:ANNOTATION_COVERAGE_DETAIL_LIMIT]

    return {
        # Simple aliases make the status easy to render while keeping the
        # explicit counts below unambiguous.  ``missing`` includes a timing
        # mismatch because neither situation can safely cover the OCR cue.
        'total': len(chinese),
        'translated': matched_count,
        'missing': coverage_gap_count,
        'zh_count': len(chinese),
        'vi_count': len(vietnamese),
        'matched_count': matched_count,
        'coverage_gap_count': coverage_gap_count,
        'missing_count': len(missing_ids),
        'extra_count': len(extra_ids),
        'timing_mismatch_count': len(timing_mismatches),
        'missing_ids': sample(missing_ids),
        'extra_ids': sample(extra_ids),
        'timing_mismatches': sample(timing_mismatches),
        'detail_limit': ANNOTATION_COVERAGE_DETAIL_LIMIT,
        'missing_ids_truncated': len(missing_ids) > ANNOTATION_COVERAGE_DETAIL_LIMIT,
        'extra_ids_truncated': len(extra_ids) > ANNOTATION_COVERAGE_DETAIL_LIMIT,
        'timing_mismatches_truncated': len(timing_mismatches) > ANNOTATION_COVERAGE_DETAIL_LIMIT,
        'source_exists': source_exists,
        'translation_exists': translation_exists,
        'available': available,
        'empty': not chinese,
        'complete': complete,
        'status': status,
    }


def safe_path(path: Path, root: Path) -> Path:
    """Reject traversal and all symlinks/junctions before a file operation."""
    root = root.absolute()
    path = path.absolute()
    try:
        path.relative_to(root)
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except ValueError:
        raise ValueError('Đường dẫn nằm ngoài thư mục project.') from None
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Không thao tác qua symlink/junction.')
        if part == root:
            break
    return resolved


class ProjectStore:
    def __init__(self, root: Path = PROJECT_DIR) -> None:
        self.root = root.resolve()
        self.data = self.root / 'web_data'
        self.registry = self.data / 'projects.json'
        self.data.mkdir(exist_ok=True)
        self.previews: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        if self.registry.exists():
            self.projects = json.loads(self.registry.read_text(encoding='utf-8'))
            self._migrate_legacy_tts_profiles()

    def _migrate_legacy_tts_profiles(self) -> None:
        """Forget prior hidden pseudo-style pitch selections once per project."""
        changed = False
        for project in self.projects.values():
            if not isinstance(project, dict):
                continue
            if project.get(TTS_PITCH_UI_VERSION_KEY) == TTS_PITCH_UI_VERSION:
                continue
            options = project.get('options')
            if isinstance(options, dict):
                for key in LEGACY_TTS_PROFILE_OPTIONS:
                    if key in options:
                        options.pop(key)
                        changed = True
            project[TTS_PITCH_UI_VERSION_KEY] = TTS_PITCH_UI_VERSION
            changed = True
        if changed:
            self.persist()

    def persist(self) -> None:
        atomic_write_text(self.registry, json.dumps(self.projects, ensure_ascii=False, indent=2))

    def reload(self) -> None:
        """Refresh the registry after an isolated worker has persisted metadata.

        The web worker owns a separate ProjectStore process.  Reloading is
        intentionally explicit: callers that hold an in-memory project while
        editing it must not have their changes replaced unexpectedly.
        """
        if not self.registry.exists():
            self.projects = {}
            return
        loaded = json.loads(self.registry.read_text(encoding='utf-8'))
        if not isinstance(loaded, dict):
            raise ValueError('Registry project không hợp lệ.')
        self.projects = loaded

    def get(self, project_id: str) -> dict[str, Any]:
        if project_id not in self.projects:
            raise ValueError('Không tìm thấy project.')
        return self.projects[project_id]

    def _base(self, project: dict[str, Any]) -> Path:
        return safe_path(Path(project['base']), self.root)

    @staticmethod
    def _identity(path: Path) -> dict[str, Any]:
        """A cheap, stable-enough identity for a local media artifact.

        Hashing an eight-gigabyte source on every page refresh would make the
        review gate unusable.  The resolved path, size and nanosecond mtime
        detect normal replacement/edit operations without reading the video.
        """
        path = path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'Không tìm thấy video: {path}')
        info = path.stat()
        return {'path': str(path), 'size': info.st_size, 'mtime_ns': info.st_mtime_ns}

    @staticmethod
    def _same_identity(expected: Any, path: Path) -> bool:
        if not isinstance(expected, dict):
            return False
        try:
            actual = ProjectStore._identity(path)
        except OSError:
            return False
        return all(expected.get(key) == actual[key] for key in ('path', 'size', 'mtime_ns'))

    def _original_path(self, project: dict[str, Any]) -> Path:
        return Path(project['video']).expanduser().resolve()

    def _slow_path(self, project: dict[str, Any]) -> Path:
        base = self._base(project)
        stem = Path(project['video']).stem
        return safe_path(base / 'output' / f'{stem}_slow.mp4', self.root)

    def _voice_metadata(self, project: dict[str, Any]) -> dict[str, Any]:
        value = project.get(VOICE_METADATA_KEY)
        return value if isinstance(value, dict) else {}

    def _logo_metadata(self, project: dict[str, Any]) -> dict[str, Any]:
        value = project.get(LOGO_METADATA_KEY)
        return value if isinstance(value, dict) else {}

    def _default_logo_path(self, project: dict[str, Any]) -> Path:
        """Return a confined placeholder path when no logo has been selected."""
        return safe_path(self._base(project) / 'input' / 'logo' / 'logo.png', self.root)

    def _logo_upload_path(self, project: dict[str, Any], name: str) -> Path:
        """Resolve one app-owned logo image without accepting a client path."""
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError('Tên file logo không hợp lệ.')
        suffix = Path(name).suffix.lower()
        if suffix not in LOGO_UPLOAD_SUFFIXES:
            raise ValueError('Logo chỉ hỗ trợ PNG, JPG, JPEG hoặc WebP.')
        return safe_path(self._base(project) / 'input' / 'logo' / name, self.root)

    def _selected_logo_path(self, project: dict[str, Any]) -> Path:
        """Return a safe candidate for public file state without raising on old metadata."""
        name = self._logo_metadata(project).get('upload_name')
        if not isinstance(name, str):
            return self._default_logo_path(project)
        try:
            return self._logo_upload_path(project, name)
        except ValueError:
            # Metadata is local but may have been written by an older build or
            # hand-edited.  Keep the public response safe and let logo_path()
            # provide the actionable error when the overlay is enabled.
            return self._default_logo_path(project)

    def logo_upload_destination(self, project_id: str, suffix: str) -> Path:
        """Return the one permitted destination for a browser-uploaded logo."""
        project = self.get(project_id)
        suffix = str(suffix).lower()
        if suffix not in LOGO_UPLOAD_SUFFIXES:
            raise ValueError('Chỉ tải lên logo PNG, JPG, JPEG hoặc WebP.')
        path = self._logo_upload_path(project, f'logo{suffix}')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def logo_path(self, project_id: str) -> Path:
        """Return the selected logo or explain why an enabled overlay cannot render."""
        project = self.get(project_id)
        name = self._logo_metadata(project).get('upload_name')
        try:
            path = self._logo_upload_path(project, name) if isinstance(name, str) else None
        except ValueError:
            path = None
        if self.settings(project_id).burn_logo:
            if path is None:
                raise ValueError('Đã bật chèn logo nhưng chưa có file logo hợp lệ. Hãy tải ảnh PNG/JPG/WebP.')
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError('Đã bật chèn logo nhưng file logo bị thiếu hoặc rỗng. Hãy tải lại ảnh logo.')
        return path if path is not None else self._default_logo_path(project)

    def mark_logo_upload(self, project_id: str, destination: Path) -> None:
        """Select a completed app-owned logo and remove the prior extension safely."""
        project = self.get(project_id)
        destination = Path(destination).resolve()
        expected = self._logo_upload_path(project, destination.name)
        if destination != expected or not destination.is_file() or destination.stat().st_size == 0:
            raise ValueError('File logo không hợp lệ.')
        metadata = self._logo_metadata(project).copy()
        previous_name = metadata.get('upload_name')
        metadata['upload_name'] = destination.name
        project[LOGO_METADATA_KEY] = metadata
        self.persist()
        # If the type changed (for example PNG -> WebP), the old app-owned
        # image is no longer selected.  Never derive this path from a browser
        # filename or remove anything outside this project input directory.
        if isinstance(previous_name, str) and previous_name != destination.name:
            try:
                previous = self._logo_upload_path(project, previous_name)
            except ValueError:
                return
            if previous.is_file():
                previous.unlink()

    def _voice_upload_path(self, project: dict[str, Any], name: str) -> Path:
        """Resolve one app-owned optional WAV/MP3/MP4 input without traversal."""
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError('Tên file nguồn đổi giọng không hợp lệ.')
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_INPUT_SUFFIXES:
            raise ValueError('Nguồn đổi giọng chỉ hỗ trợ WAV, MP3 hoặc MP4.')
        return safe_path(self._base(project) / 'input' / 'voice' / name, self.root)

    def voice_upload_destination(self, project_id: str, suffix: str) -> Path:
        """Return the sole safe destination for a browser-uploaded voice source."""
        project = self.get(project_id)
        suffix = str(suffix).lower()
        if suffix not in SUPPORTED_INPUT_SUFFIXES:
            raise ValueError('Chỉ tải lên WAV, MP3 hoặc MP4 để đổi giọng.')
        path = self._voice_upload_path(project, f'voice_source{suffix}')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def voice_source_path(self, project_id: str) -> Path:
        """Return the selected source.  Project video keeps the review contract."""
        project = self.get(project_id)
        metadata = self._voice_metadata(project)
        mode = metadata.get('source_mode', 'project')
        if mode == 'project':
            return self.paths(project_id)['source']
        if mode != 'upload':
            raise ValueError('Nguồn đổi giọng không hợp lệ.')
        return self._voice_upload_path(project, metadata.get('upload_name', ''))

    def voice_model_catalog(self, project_id: str) -> dict[str, dict[str, Any]]:
        """List only app-configured local RVC models; clients never send paths."""
        models_root = Path(self.settings(project_id).voice_models_dir).expanduser().resolve()
        if not models_root.is_dir():
            return {}
        catalog: dict[str, dict[str, Any]] = {}
        for model in sorted(models_root.glob('*.pth'), key=lambda item: item.name.casefold()):
            if model.is_symlink() or not model.is_file() or not model.stem or model.stem in catalog:
                continue
            index = model.with_suffix('.index')
            catalog[model.stem] = {
                'id': model.stem,
                'name': model.name,
                'path': model.resolve(),
                'index': index.resolve() if index.is_file() and not index.is_symlink() else None,
            }
        return catalog

    @staticmethod
    def _voice_defaults() -> dict[str, Any]:
        return {
            'source_mode': 'project', 'model_id': '', 'preset': 'cute', 'speaker_id': None,
            'pitch_semitones': None, 'index_rate': None, 'rms_mix_rate': None,
            'protect': None, 'post_pitch_semitones': 0.0,
        }

    def voice_options(self, project_id: str) -> dict[str, Any]:
        project = self.get(project_id)
        options = self._voice_defaults()
        metadata = self._voice_metadata(project)
        for key in options:
            if key in metadata:
                options[key] = metadata[key]
        return options

    def _voice_result_options(
        self,
        project_id: str,
        catalog: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Capture every setting that changes an RVC render.

        A model path alone is insufficient to identify a render: a different
        preset, speaker, F0 setting or index file can produce a different
        voice from the same ``.pth``.  Store only JSON-safe values and file
        identities, so ``voice_status`` can hide an old render after any of
        those inputs changes.
        """
        options = self.voice_options(project_id)
        catalog = self.voice_model_catalog(project_id) if catalog is None else catalog
        item = catalog.get(options['model_id']) if isinstance(options['model_id'], str) else None
        index = item.get('index') if item else None
        return {
            'source_mode': options['source_mode'],
            'model_id': options['model_id'],
            'preset': options['preset'],
            'speaker_id': options['speaker_id'],
            'pitch_semitones': options['pitch_semitones'],
            'index_rate': options['index_rate'],
            'rms_mix_rate': options['rms_mix_rate'],
            'protect': options['protect'],
            'post_pitch_semitones': options['post_pitch_semitones'],
            'index': self._identity(index) if isinstance(index, Path) and index.is_file() else None,
        }

    def update_voice_options(self, project_id: str, values: dict[str, Any]) -> None:
        """Persist bounded style choices and a model *ID*, never a client path."""
        if not isinstance(values, dict):
            raise ValueError('Tùy chọn đổi giọng không hợp lệ.')
        allowed = set(self._voice_defaults())
        if set(values) - allowed:
            raise ValueError('Tùy chọn đổi giọng không được hỗ trợ.')
        options = self.voice_options(project_id)
        options.update(values)
        if options['source_mode'] not in VOICE_SOURCE_MODES:
            raise ValueError('Nguồn đổi giọng phải là video project hoặc file đã tải lên.')
        model_id = options['model_id']
        if not isinstance(model_id, str) or len(model_id) > 150:
            raise ValueError('Model RVC không hợp lệ.')
        if model_id and model_id not in self.voice_model_catalog(project_id):
            raise ValueError('Không tìm thấy model RVC đã chọn trong models\\rvc.')
        if options['preset'] not in VOICE_PRESETS:
            raise ValueError('Preset giọng không hợp lệ.')
        speaker_id = options['speaker_id']
        if speaker_id is not None and (type(speaker_id) is not int or not 0 <= speaker_id <= 109):
            raise ValueError('Speaker ID phải là số nguyên từ 0 đến 109.')
        pitch = options['pitch_semitones']
        if pitch is not None and (type(pitch) is not int or not -12 <= pitch <= 12):
            raise ValueError('Pitch RVC phải là số nguyên từ -12 đến 12.')
        for key in ('index_rate', 'rms_mix_rate'):
            value = options[key]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1):
                raise ValueError(f'{key} phải từ 0 đến 1.')
        protect = options['protect']
        if protect is not None and (type(protect) not in (int, float) or not math.isfinite(float(protect)) or not 0 <= float(protect) <= .5):
            raise ValueError('protect phải từ 0 đến 0.5.')
        post_pitch = options['post_pitch_semitones']
        if type(post_pitch) not in (int, float) or not math.isfinite(float(post_pitch)) or not -2 <= float(post_pitch) <= 2:
            raise ValueError('Tinh chỉnh F0 hậu kỳ phải từ -2 đến 2.')
        project = self.get(project_id)
        metadata = self._voice_metadata(project).copy()
        metadata.update(options)
        project[VOICE_METADATA_KEY] = metadata
        self.persist()

    def mark_voice_upload(self, project_id: str, destination: Path) -> None:
        """Select a successfully uploaded app-owned source and retain settings."""
        project = self.get(project_id)
        destination = Path(destination).resolve()
        expected = self._voice_upload_path(project, destination.name)
        if destination != expected or not destination.is_file() or destination.stat().st_size == 0:
            raise ValueError('File nguồn đổi giọng không hợp lệ.')
        metadata = self._voice_metadata(project).copy()
        previous_name = metadata.get('upload_name')
        metadata.update({'source_mode': 'upload', 'upload_name': destination.name})
        project[VOICE_METADATA_KEY] = metadata
        self.persist()
        if isinstance(previous_name, str) and previous_name != destination.name:
            previous = self._voice_upload_path(project, previous_name)
            if previous.is_file():
                previous.unlink()

    def voice_request_data(self, project_id: str) -> dict[str, Any]:
        """Resolve trusted files and options for the isolated conversion worker."""
        options = self.voice_options(project_id)
        source = self.voice_source_path(project_id)
        if not source.is_file():
            raise FileNotFoundError('Chưa chọn file nguồn đổi giọng.')
        model_id = options['model_id']
        catalog = self.voice_model_catalog(project_id)
        if not model_id or model_id not in catalog:
            raise ValueError('Chọn model RVC .pth trong thư mục models\\rvc trước khi chạy.')
        model = catalog[model_id]
        return {**options, 'source': source, 'model': model['path'], 'index': model['index']}

    def voice_preview_request_data(self, project_id: str) -> dict[str, Any]:
        """Resolve a selected model for the fixed, app-owned RVC audition.

        Unlike ``voice_request_data`` this intentionally has no user audio
        source.  The loopback handler creates a short fixed Vietnamese sample
        inside the project, which lets someone hear a model before selecting
        a video or uploading a file.
        """
        options = self.voice_options(project_id)
        model_id = options['model_id']
        catalog = self.voice_model_catalog(project_id)
        if not model_id or model_id not in catalog:
            raise ValueError('Chọn model RVC .pth trước khi nghe thử.')
        model = catalog[model_id]
        return {
            **options,
            'model': model['path'],
            'index': model['index'],
            'model_id': model['id'],
            'model_label': model['name'],
        }

    def voice_status(self, project_id: str) -> dict[str, Any]:
        """Return safe UI state; absolute source/model locations stay server-side."""
        project = self.get(project_id)
        metadata = self._voice_metadata(project)
        options = self.voice_options(project_id)
        try:
            source = self.voice_source_path(project_id)
            source_info = {'name': source.name, 'exists': source.is_file()}
        except ValueError:
            source_info = {'name': '', 'exists': False}
        paths = self.paths(project_id)
        result = metadata.get('result') if isinstance(metadata.get('result'), dict) else {}
        audio = paths['voice_audio']
        video = paths['voice_video']
        expected_source = result.get('source')
        expected_model = result.get('model')
        expected_video = result.get('video')
        # An audio-only conversion may follow an MP4 conversion in the same
        # project.  The old MP4 is deliberately retained until cleanup, but
        # it must not make the newly published WAV appear stale or downloadable.
        expects_video = expected_video is not None
        model_id = options['model_id']
        catalog = self.voice_model_catalog(project_id)
        model = catalog.get(model_id)
        valid = bool(
            result and source_info['exists'] and model and audio.is_file()
            and self._same_identity(expected_source, self.voice_source_path(project_id))
            and self._same_identity(expected_model, model['path'])
            and result.get('voice_options') == self._voice_result_options(project_id, catalog)
            and self._same_identity(result.get('audio'), audio)
            and (
                not expects_video
                or (isinstance(expected_video, dict) and video.is_file()
                    and self._same_identity(expected_video, video))
            )
        )
        return {
            'options': options,
            'source': source_info,
            'models': [{'id': item['id'], 'name': item['name'], 'has_index': item['index'] is not None}
                       for item in catalog.values()],
            'runtime': rvc_runtime_status(self.settings(project_id)),
            'result_ready': valid,
            'result_stale': bool(result) and not valid,
            'audio': {'exists': valid and audio.is_file(), 'name': audio.name},
            'video': {'exists': valid and expects_video and video.is_file(), 'name': video.name},
        }

    def mark_voice_completed(self, project_id: str, *, source: Path, model: Path, audio: Path, video: Path | None) -> None:
        """Bind output to exact source/model identities so stale results are hidden."""
        project = self.get(project_id)
        metadata = self._voice_metadata(project).copy()
        result = {
            'source': self._identity(source), 'model': self._identity(model),
            'audio': self._identity(audio), 'video': self._identity(video) if video else None,
            'voice_options': self._voice_result_options(project_id),
            'completed': time.time(),
        }
        metadata['result'] = result
        project[VOICE_METADATA_KEY] = metadata
        self.persist()
        # A local, human-inspectable record without leaking absolute external
        # input/model paths through the browser API.
        paths = self.paths(project_id)
        manifest = {
            'version': 1, 'source_name': Path(source).name, 'model_name': Path(model).name,
            'audio_name': Path(audio).name, 'video_name': Path(video).name if video else None,
            'options': self.voice_options(project_id), 'completed': result['completed'],
        }
        atomic_write_text(paths['voice_manifest'], json.dumps(manifest, ensure_ascii=False, indent=2))

    def _registered_sources(self) -> set[Path]:
        """All user-selected inputs, including another project's input."""
        sources = {Path(item['video']).expanduser().resolve() for item in self.projects.values()}
        for project in self.projects.values():
            metadata = self._voice_metadata(project)
            if metadata.get('source_mode') != 'upload':
                continue
            try:
                source = self._voice_upload_path(project, metadata.get('upload_name', ''))
            except ValueError:
                continue
            if source.is_file():
                sources.add(source)
        return sources

    def _slow_metadata(self, project: dict[str, Any]) -> dict[str, Any] | None:
        value = project.get(SLOW_METADATA_KEY)
        return value if isinstance(value, dict) else None

    def _slow_metadata_valid(self, project: dict[str, Any], metadata: dict[str, Any] | None = None) -> bool:
        metadata = metadata or self._slow_metadata(project)
        if not metadata or metadata.get('state') not in SOURCE_STATES - {'original'}:
            return False
        speed = metadata.get('speed')
        if type(speed) not in (int, float) or not math.isfinite(float(speed)) or not 0.25 <= float(speed) <= 1.0:
            return False
        return (self._same_identity(metadata.get('original'), self._original_path(project))
                and self._same_identity(metadata.get('slow'), self._slow_path(project)))

    def source_status(self, project_id: str) -> dict[str, Any]:
        """Return the review state without ever trusting a stale slow render."""
        project = self.get(project_id)
        metadata = self._slow_metadata(project)
        if not metadata:
            return {'source_state': 'original', 'slow_speed': None, 'slow_ready': False, 'source_error': None}
        if self._slow_metadata_valid(project, metadata):
            return {
                'source_state': metadata['state'],
                'slow_speed': float(metadata['speed']),
                'slow_ready': True,
                'source_error': None,
            }
        state = metadata.get('state')
        message = ('Bản tua chậm hoặc video gốc đã thay đổi sau khi tạo. '
                   'Hãy tạo lại bản tua chậm trước khi OCR/lồng tiếng.')
        # A stale approved artifact must never silently become the processing
        # source.  Existing outputs are left untouched for the user to inspect.
        return {
            'source_state': 'original', 'slow_speed': None, 'slow_ready': False,
            'source_error': message if state in {'review_required', 'approved'} else None,
        }

    def slow_render_path(self, project_id: str) -> Path:
        """Return the one permitted preview destination.

        An approved slow file is the active timeline.  Re-rendering over it
        would detach existing SRT timing from its source, so the caller must
        explicitly switch back to the original source first.
        """
        project = self.get(project_id)
        metadata = self._slow_metadata(project)
        if metadata and metadata.get('state') == 'approved':
            raise ValueError(
                'Bản tua chậm đang là nguồn đã duyệt. Hãy chọn dùng video gốc '
                'trước khi tạo bản tua chậm mới; các SRT/video cũ sẽ được sao lưu.'
            )
        path = self._slow_path(project)
        if path.exists() and path.resolve() in self._registered_sources():
            raise ValueError('Bản tua chậm này đang được một project khác dùng làm video đầu vào.')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def mark_slow_created(self, project_id: str, speed: float | None = None) -> dict[str, Any]:
        """Record a successfully rendered slow preview and require human review."""
        project = self.get(project_id)
        metadata = self._slow_metadata(project)
        if metadata and metadata.get('state') == 'approved':
            raise ValueError('Không thể thay bản tua chậm đã duyệt khi chưa chuyển về video gốc.')
        configured = self.settings(project_id).source_speed if speed is None else speed
        if type(configured) not in (int, float):
            raise ValueError('Tốc độ video phải là số.')
        # Reuse Settings validation so web, CLI and worker keep one range.
        speed = replace(self.settings(project_id), source_speed=float(configured)).source_speed
        slow = self.slow_render_path(project_id)
        if not slow.is_file() or slow.stat().st_size == 0:
            raise FileNotFoundError('Chưa tạo được file video tua chậm để xem lại.')
        project[SLOW_METADATA_KEY] = {
            'version': 1,
            'state': 'review_required',
            'speed': speed,
            'original': self._identity(self._original_path(project)),
            'slow': self._identity(slow),
            'created': time.time(),
        }
        self.persist()
        return self.source_status(project_id)

    def add(self, video: Path, *, legacy: bool = False, project_id: str | None = None) -> dict[str, Any]:
        video = video.expanduser().resolve()
        if not video.is_file() or video.suffix.lower() != '.mp4':
            raise ValueError('Chọn đường dẫn tới một file MP4 có thật trên máy.')
        if legacy and any(p['legacy'] for p in self.projects.values()):
            raise ValueError('Bộ dữ liệu CLI đã được kết nối. Mỗi bộ audio CLI chỉ kết nối một lần.')
        project_id = project_id or uuid.uuid4().hex
        if not project_id.isalnum() or project_id in self.projects:
            raise ValueError('ID project không hợp lệ.')
        base = self.root if legacy else self.root / 'projects' / project_id
        for directory in ('output', 'audio', 'temp'):
            safe_path(base / directory, self.root).mkdir(parents=True, exist_ok=True)
        self.projects[project_id] = {
            'id': project_id, 'name': video.stem, 'video': str(video), 'legacy': legacy,
            'base': str(base), 'created': time.time(), 'options': {},
            TTS_PITCH_UI_VERSION_KEY: TTS_PITCH_UI_VERSION,
        }
        self.persist()
        return self.projects[project_id]

    def settings(self, project_id: str) -> Settings:
        project = self.get(project_id)
        base = self._base(project)
        defaults = (load_settings() if self.root == PROJECT_DIR else Settings(
            project_dir=self.root,
            rvc_root=self.root / '.tools' / 'rvc',
            rvc_python=self.root / '.tools' / 'rvc-runtime' / 'Scripts' / 'python.exe',
            voice_models_dir=self.root / 'models' / 'rvc',
        ))
        settings = replace(defaults, output_dir=base / 'output', audio_dir=base / 'audio',
                           temp_dir=base / 'temp', **project['options'])
        return settings

    def paths(self, project_id: str) -> dict[str, Path]:
        p = self.get(project_id)
        settings = self.settings(project_id)
        stem = Path(p['video']).stem
        original = self._original_path(p)
        slow = self._slow_path(p)
        source = slow if self.source_status(project_id)['source_state'] == 'approved' else original
        logo = self._selected_logo_path(p)
        return {
            # `original` is immutable project input.  `source` is the exact
            # timeline subsequent stages must use after the review decision.
            'original': original, 'slow': slow, 'source': source,
            'zh': settings.output_dir / f'{stem}_zh.srt',
            'vi': settings.output_dir / f'{stem}_vi.srt',
            'speech': settings.output_dir / f'{stem}_vi_tts.srt',
            # In-frame labels have their own SRT pair so they are never fed
            # into the dialogue TTS timeline.
            'annotation_zh': settings.output_dir / f'{stem}_annotation_zh.srt',
            'annotation_vi': settings.output_dir / f'{stem}_annotation_vi.srt',
            'final': settings.output_dir / 'final_video.mp4',
            # A short, replaceable actual-Edge audition.  It never affects
            # the WAV cache or final video and can be removed with temp data.
            'tts_preview': settings.temp_dir / 'tts_voice_preview.mp3',
            # This is an actual RVC result from a fixed short Vietnamese
            # sample.  It is temporary and never replaces the user's audio.
            'rvc_preview': settings.temp_dir / 'rvc_model_preview.wav',
            # Voice conversion deliberately stays separate from Edge-TTS WAV
            # cache and the translated-video final render.
            'voice_audio': settings.output_dir / 'voice_cute.wav',
            'voice_video': settings.output_dir / 'voice_cute.mp4',
            'voice_manifest': settings.output_dir / 'voice_cute_manifest.json',
            # A selected logo is a private project input.  Its safe basename
            # and existence are exposed, never its absolute path or metadata.
            'logo': logo,
        }

    def annotation_coverage(self, project_id: str) -> dict[str, Any]:
        """Return bounded, text-free annotation translation diagnostics.

        Project cards are rendered even while a person is editing a manual SRT
        outside this app.  A malformed/locked subtitle file must therefore not
        take down ``/api/projects`` or expose a local absolute path in an error
        response.  Counts that can still be read are retained and ``status``
        explains which side needs attention.
        """
        paths = self.paths(project_id)
        zh_path = paths['annotation_zh']
        vi_path = paths['annotation_vi']
        try:
            zh_exists = zh_path.is_file()
        except OSError:
            zh_exists = False
        try:
            vi_exists = vi_path.is_file()
        except OSError:
            vi_exists = False

        chinese: list[Subtitle] = []
        vietnamese: list[Subtitle] = []
        zh_readable = True
        vi_readable = True
        if zh_exists:
            try:
                chinese = read_srt(zh_path)
            except (OSError, UnicodeError, ValueError):
                zh_readable = False
        if vi_exists:
            try:
                vietnamese = read_srt(vi_path)
            except (OSError, UnicodeError, ValueError):
                vi_readable = False

        if not vi_readable:
            status = 'invalid_translation'
        elif not zh_exists:
            # A Vietnamese annotation SRT may come directly from a manual or
            # GPT-assisted video review.  It is renderable without an OCR
            # source track, but there is deliberately no Chinese cue list to
            # compare IDs/timestamps against.
            status = 'direct_vietnamese' if vi_exists else 'missing_source'
        elif not zh_readable:
            status = 'invalid_source'
        elif not vi_exists:
            status = 'missing_translation'
        elif not chinese:
            status = 'empty_source'
        else:
            status = 'ready'

        return annotation_translation_coverage(
            chinese,
            vietnamese,
            source_exists=zh_exists,
            translation_exists=vi_exists,
            available=zh_exists and zh_readable and vi_readable,
            status=status,
        )

    def public(self, project_id: str) -> dict[str, Any]:
        p = self.get(project_id)
        settings = self.settings(project_id)
        status = self.source_status(project_id)
        # Do not leak implementation metadata into the browser.  The public
        # fields are enough for it to show the review gate and selected speed.
        project = {key: value for key, value in p.items()
                   if key not in (SLOW_METADATA_KEY, VOICE_METADATA_KEY, LOGO_METADATA_KEY,
                                  TTS_PITCH_UI_VERSION_KEY)}
        return {
            **project,
            'options': {k: getattr(settings, k) for k in OPTION_NAMES},
            'edge_voices': edge_voice_catalog(),
            'files': {k: {'exists': v.is_file(), 'name': v.name} for k, v in self.paths(project_id).items()},
            # This status contains only counts, IDs and timestamps.  It is
            # intentionally separate from the normal dialogue SRT, because
            # frame annotations do not enter TTS.
            'annotation_coverage': self.annotation_coverage(project_id),
            'voice': self.voice_status(project_id),
            **status,
        }

    def _archive_paths(self, paths: list[Path], *, reason: str) -> list[Path]:
        """Move selected generated files to collision-free .bak names.

        A source transition is a user-visible decision.  Do not delete manual
        Vietnamese subtitle work merely because its timestamps belong to the
        prior video timeline.  A short rollback keeps the old files in place if
        Windows refuses one of the moves midway through the operation.
        """
        moves: list[tuple[Path, Path]] = []
        stamp = time.strftime('%Y%m%d-%H%M%S')
        for path in paths:
            path = safe_path(path, self.root)
            if not path.is_file():
                continue
            destination = safe_path(
                path.with_name(f'{path.name}.{reason}.{stamp}-{uuid.uuid4().hex}.bak'), self.root,
            )
            moves.append((path, destination))
        moved: list[tuple[Path, Path]] = []
        try:
            for source, destination in moves:
                source.replace(destination)
                moved.append((source, destination))
        except OSError as exc:
            for source, destination in reversed(moved):
                try:
                    destination.replace(source)
                except OSError:
                    # The original error is still the useful one; a failed
                    # rollback leaves only explicitly named backup files.
                    pass
            raise RuntimeError('Không sao lưu được dữ liệu cũ trước khi đổi nguồn video.') from exc
        return [destination for _, destination in moved]

    def _archive_timeline_artifacts(self, project_id: str) -> list[Path]:
        """Archive only artifacts whose IDs/timestamps depend on video speed.

        WAV content is intentionally retained: it is keyed by subtitle text,
        voice and format in its manifest, and is placed at new SRT timestamps
        during a later mix.  Gemini cache includes source-SRT bytes, so a new
        OCR timeline cannot accidentally consume an old translation response.
        """
        paths = self.paths(project_id)
        overlap = safe_path(self.settings(project_id).audio_dir / 'overlap_warnings.json', self.root)
        protected = self._registered_sources()
        return self._archive_paths(
            [path for path in (
                paths['zh'], paths['vi'], paths['speech'], paths['annotation_zh'], paths['annotation_vi'],
                paths['final'], overlap,
                paths['voice_audio'], paths['voice_video'], paths['voice_manifest'],
            )
             if path.resolve() not in protected],
            reason='source-changed',
        )

    def activate_slow_source(self, project_id: str) -> dict[str, Any]:
        """Accept the reviewed slow timeline and invalidate dependent outputs."""
        project = self.get(project_id)
        metadata = self._slow_metadata(project)
        if not metadata or metadata.get('state') != 'review_required':
            raise ValueError('Chưa có bản tua chậm chờ duyệt.')
        if not self._slow_metadata_valid(project, metadata):
            raise ValueError(
                'Bản tua chậm hoặc video gốc đã thay đổi. Hãy tạo lại bản tua chậm rồi xem lại trước khi dùng.'
            )
        archived = self._archive_timeline_artifacts(project_id)
        metadata['state'] = 'approved'
        metadata['approved'] = time.time()
        self.persist()
        return {'archived': [str(path.relative_to(self.root)) for path in archived], **self.source_status(project_id)}

    def activate_original_source(self, project_id: str) -> dict[str, Any]:
        """Return to the original timeline, preserving prior generated work as backups."""
        project = self.get(project_id)
        metadata = self._slow_metadata(project)
        if not metadata:
            return {'archived': [], **self.source_status(project_id)}

        # An approved slow timeline has already produced timing-dependent
        # artifacts.  A review candidate has not, so rejecting it leaves the
        # original project's SRT/WAV/final artifacts available.
        archived: list[Path] = []
        if metadata.get('state') == 'approved':
            archived.extend(self._archive_timeline_artifacts(project_id))
        slow = self._slow_path(project)
        if slow.resolve() not in self._registered_sources():
            archived.extend(self._archive_paths([slow], reason='slow-source'))
        project.pop(SLOW_METADATA_KEY, None)
        self.persist()
        return {'archived': [str(path.relative_to(self.root)) for path in archived], **self.source_status(project_id)}

    def update_options(self, project_id: str, options: dict[str, Any]) -> None:
        if set(options) - set(OPTION_NAMES):
            raise ValueError('Tùy chọn không được hỗ trợ.')
        for key, value in options.items():
            if key in ('ocr_device', 'tts_voice', 'tts_pitch', 'gemini_model', 'logo_position'):
                if not isinstance(value, str) or not value.strip() or len(value) > 150:
                    raise ValueError(f'{key} không hợp lệ.')
                if key == 'tts_pitch':
                    matched = re.fullmatch(r'([+-])(\d{1,2})Hz', value)
                    if not matched or int(matched.group(2)) > 20:
                        raise ValueError('Cao độ Edge trên web phải từ -20Hz đến +20Hz.')
            elif key in BOOLEAN_OPTIONS:
                if type(value) is not bool:
                    raise ValueError(f'{key} phải là true hoặc false.')
            elif key == 'annotation_font_size':
                if type(value) is not int:
                    raise ValueError('annotation_font_size phải là số nguyên.')
            elif type(value) not in (int, float):
                raise ValueError(f'{key} phải là số.')
        current_settings = self.settings(project_id)
        candidate_settings = replace(current_settings, **options)  # Validate before saving.
        region_keys = {'annotation_left', 'annotation_top', 'annotation_right', 'annotation_bottom'}
        if region_keys.intersection(options):
            current_region = (
                current_settings.annotation_left, current_settings.annotation_top,
                current_settings.annotation_right, current_settings.annotation_bottom,
            )
            candidate_region = (
                candidate_settings.annotation_left, candidate_settings.annotation_top,
                candidate_settings.annotation_right, candidate_settings.annotation_bottom,
            )
            if current_region != candidate_region:
                # A human has explicitly chosen a different part of the frame.
                # Timings/text from the old crop cannot be rendered safely at
                # the new location, so retain them as named backups instead of
                # silently mixing them into the next export.  The existing
                # final video also contains the old on-frame position, so hide
                # it from the current pipeline by archiving it too when this
                # project has actually used an annotation track.
                paths = self.paths(project_id)
                protected = self._registered_sources()
                annotation_paths = [paths['annotation_zh'], paths['annotation_vi']]
                had_annotation = current_settings.annotation_ocr_enabled or any(
                    path.is_file() for path in annotation_paths
                )
                self._archive_paths(
                    [path for path in (*annotation_paths, *( (paths['final'],) if had_annotation else () ))
                     if path.resolve() not in protected],
                    reason='annotation-region',
                )
        project = self.get(project_id)
        project['options'].update(options)
        if 'tts_pitch' in options:
            project[TTS_PITCH_UI_VERSION_KEY] = TTS_PITCH_UI_VERSION
        self.persist()

    def cleanup_candidates(self, project_id: str, categories: list[str]) -> list[dict[str, Any]]:
        if not categories or set(categories) - {'temp', 'audio', 'renders', 'backups', 'translation_cache', 'voice_conversion'}:
            raise ValueError('Chọn nhóm dữ liệu cần dọn.')
        settings = self.settings(project_id)
        candidates: set[Path] = set()
        if 'audio' in categories:
            candidates.update(settings.audio_dir.glob('*.wav'))
            candidates.update(settings.audio_dir / name for name in ('manifest.json', 'overlap_warnings.json'))
        if 'renders' in categories:
            candidates.update(settings.output_dir.glob('final_video*.mp4'))
        if 'voice_conversion' in categories:
            candidates.update(path for path in (
                self.paths(project_id)['voice_audio'], self.paths(project_id)['voice_video'],
                self.paths(project_id)['voice_manifest'],
            ) if path.is_file())
            voice_temp = safe_path(settings.temp_dir / 'voice_conversion', self.root)
            if voice_temp.is_dir():
                candidates.update(voice_temp.rglob('*'))
        if 'backups' in categories:
            candidates.update(settings.output_dir.glob('*.bak'))
            candidates.update(settings.audio_dir.glob('*.bak'))
        if 'temp' in categories:
            # Never traverse model caches, source scripts, or dependency folders.
            for pattern in ('*.f32', '*.mp3', '*.wav', '*.mp4'):
                candidates.update(settings.temp_dir.glob(pattern))
            for directory in settings.temp_dir.iterdir():
                if directory.name.startswith(('mix_', 'burn_')):
                    safe_path(directory, self.root)
                    candidates.update(directory.rglob('*'))
            for directory in settings.audio_dir.glob('tts_*'):
                safe_path(directory, self.root)
                candidates.update(directory.rglob('*'))
        if 'translation_cache' in categories:
            cache = settings.temp_dir / 'translation_cache'
            if cache.exists():
                safe_path(cache, self.root)
                candidates.update(cache.rglob('*.json'))
        result = []
        sources = self._registered_sources()
        for candidate in sorted(candidates):
            path = safe_path(candidate, self.root)
            if not path.is_file() or path in sources:
                continue
            if path.suffix.lower() in ('.srt', '.py', '.env'):
                continue
            info = path.stat()
            result.append({'path': str(path.relative_to(self.root)), 'size': info.st_size,
                           'mtime': info.st_mtime_ns})
        return result

    def preview_cleanup(self, project_id: str, categories: list[str]) -> dict[str, Any]:
        files = self.cleanup_candidates(project_id, categories)
        self.previews = {k: v for k, v in self.previews.items() if v['expires'] > time.time()}
        token = secrets.token_urlsafe(24)
        self.previews[token] = {'project': project_id, 'files': files, 'expires': time.time() + 300}
        return {'token': token, 'files': files, 'bytes': sum(f['size'] for f in files), 'count': len(files)}

    def execute_cleanup(self, project_id: str, token: str) -> dict[str, Any]:
        preview = self.previews.pop(token, None)
        if not preview or preview['project'] != project_id or preview['expires'] < time.time():
            raise ValueError('Bản xem trước hết hạn. Hãy xem trước lại.')
        paths = []
        for item in preview['files']:
            path = safe_path(self.root / item['path'], self.root)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns) != (item['size'], item['mtime']):
                raise ValueError('File đã thay đổi kể từ lúc xem trước. Hãy xem trước lại.')
            paths.append(path)
        # Individual verified files only; never recursive deletion or arbitrary paths.
        removed, errors = 0, []
        for path, item in zip(paths, preview['files']):
            try:
                checked = safe_path(path, self.root)
                info = checked.stat()
                if (info.st_size, info.st_mtime_ns) != (item['size'], item['mtime']):
                    errors.append(str(path.relative_to(self.root)))
                    continue
                checked.unlink()
                removed += 1
            except OSError:
                errors.append(str(path.relative_to(self.root)))
        return {'removed': removed, 'errors': errors}


def revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'missing'
