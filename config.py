"""Configuration from project .env (environment variables take precedence)."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import math
import os
from pathlib import Path
import re

PROJECT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    project_dir: Path = PROJECT_DIR
    output_dir: Path = PROJECT_DIR / "output"
    audio_dir: Path = PROJECT_DIR / "audio"
    temp_dir: Path = PROJECT_DIR / "temp"
    glossary_path: Path = PROJECT_DIR / "glossary.json"
    # Playback speed for the optional Chinese-source review render.  It is kept
    # separate from voice_speed: this changes the video timeline before OCR,
    # while voice_speed changes only the synthesized Vietnamese WAVs at mix time.
    source_speed: float = 0.8
    # The default uses the NVIDIA encoder on the configured Windows GPU.  Keep
    # libx264 as an explicit fallback for machines without NVENC.
    slow_video_encoder: str = "h264_nvenc"
    # RVC lives in a separate CUDA environment.  Keeping it apart from the
    # PaddleOCR venv avoids torch/fairseq dependency conflicts in the web app.
    rvc_root: Path = PROJECT_DIR / ".tools" / "rvc"
    rvc_python: Path = PROJECT_DIR / ".tools" / "rvc-runtime" / "Scripts" / "python.exe"
    voice_models_dir: Path = PROJECT_DIR / "models" / "rvc"
    rvc_cuda_device: int = 0
    sample_fps: float = 4.0
    roi_left: float = 0.05
    roi_top: float = 0.65
    roi_right: float = 0.95
    roi_bottom: float = 0.98
    ocr_confidence: float = 0.6
    fuzzy_threshold: float = 0.84
    ocr_max_gap: float = 0.35
    ocr_min_duration: float = 0.2
    ocr_device: str = "gpu:0"
    ocr_det_model: str = "PP-OCRv5_mobile_det"
    ocr_rec_model: str = "PP-OCRv5_mobile_rec"
    ocr_lang: str = "ch"
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.6-flash"
    translation_batch_size: int = 40
    translation_context_size: int = 5
    translation_retries: int = 3
    translation_retry_delay: float = 5.0
    gemini_timeout: float = 120.0
    tts_voice: str = "vi-VN-HoaiMyNeural"
    tts_rate: str = "+0%"
    tts_volume: str = "+0%"
    # Optional Edge F0 adjustment.  It is not another speaker or voice model,
    # and changes neither video duration nor the later Vietnamese-only speed mix.
    tts_pitch: str = "+0Hz"
    tts_retries: int = 6
    tts_retry_delay: float = 5.0
    tts_request_interval: float = 1.0
    tts_timeout: float = 90.0
    audio_sample_rate: int = 24000
    original_volume: float = 0.15
    voice_volume: float = 1.0
    voice_speed: float = 1.0
    blur_chinese_subtitles: bool = False
    blur_left: float = 0.05
    blur_top: float = 0.80
    blur_right: float = 0.95
    blur_bottom: float = 0.96
    blur_strength: int = 14
    mirror_video: bool = False
    # Optional static image overlay for the final video.  The image itself is
    # kept per project by the web store; these values only describe how it is
    # rendered.
    burn_logo: bool = False
    logo_position: str = "top-right"
    logo_width_percent: float = 12.0
    logo_opacity: float = 0.85
    logo_margin_percent: float = 3.0
    # A second OCR region for in-frame labels such as character names.  It is
    # deliberately separate from the lower dialogue ROI and is never sent to
    # the TTS pipeline.
    annotation_ocr_enabled: bool = False
    annotation_left: float = 0.20
    annotation_top: float = 0.20
    annotation_right: float = 0.80
    annotation_bottom: float = 0.50
    annotation_font_size: int = 28
    # The OCR crop and the place where Vietnamese is rendered are separate.
    # This is the normalized distance from the top of the video for the
    # display-only Vietnamese label.  A small value intentionally keeps the
    # translation high and clear of the hard-coded Chinese text below.
    annotation_display_y: float = 0.12
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    burn_font: str = "Arial"
    burn_font_size: int = 24

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{item.name} phải là số hữu hạn.")
        if not 0.25 <= self.source_speed <= 1.0:
            raise ValueError("SOURCE_SPEED phải từ 0.25 đến 1.0.")
        if self.slow_video_encoder not in ("h264_nvenc", "libx264"):
            raise ValueError("SLOW_VIDEO_ENCODER phải là h264_nvenc hoặc libx264.")
        if self.rvc_cuda_device < 0 or self.rvc_cuda_device > 15:
            raise ValueError("RVC_CUDA_DEVICE phải là số GPU từ 0 đến 15.")
        if not 0 < self.sample_fps <= 60:
            raise ValueError("SAMPLE_FPS phải > 0 và <= 60.")
        if not (0 <= self.roi_left < self.roi_right <= 1 and 0 <= self.roi_top < self.roi_bottom <= 1):
            raise ValueError("ROI phải có 0 <= left < right <= 1 và 0 <= top < bottom <= 1.")
        if not (0 <= self.blur_left < self.blur_right <= 1 and 0 <= self.blur_top < self.blur_bottom <= 1):
            raise ValueError("Vùng làm mờ phải có 0 <= left < right <= 1 và 0 <= top < bottom <= 1.")
        if not (0 <= self.annotation_left < self.annotation_right <= 1
                and 0 <= self.annotation_top < self.annotation_bottom <= 1):
            raise ValueError("Vùng OCR chú thích phải có 0 <= left < right <= 1 và 0 <= top < bottom <= 1.")
        if (type(self.annotation_display_y) not in (int, float)
                or not math.isfinite(float(self.annotation_display_y))
                or not 0.05 <= float(self.annotation_display_y) <= 0.80):
            raise ValueError("ANNOTATION_DISPLAY_Y phải từ 0.05 đến 0.80 (tính từ phía trên video).")
        if not (0 <= self.ocr_confidence <= 1 and 0 <= self.fuzzy_threshold <= 1):
            raise ValueError("OCR_CONFIDENCE và FUZZY_THRESHOLD phải trong khoảng 0..1.")
        if self.ocr_max_gap < 0 or self.ocr_min_duration < 0:
            raise ValueError("OCR_MAX_GAP và OCR_MIN_DURATION không được âm.")
        if not 1 <= self.translation_batch_size <= 100:
            raise ValueError("TRANSLATION_BATCH_SIZE phải trong 1..100 (nên dùng 30..50).")
        if self.translation_context_size < 0 or self.translation_retries < 0 or self.tts_retries < 1:
            raise ValueError("Context/translation retries không âm; TTS_RETRIES phải >= 1.")
        if min(self.translation_retry_delay, self.tts_retry_delay) < 0 or self.gemini_timeout <= 0:
            raise ValueError("Retry delay không âm và GEMINI_TIMEOUT phải > 0.")
        if self.tts_request_interval < 0 or self.tts_timeout <= 0:
            raise ValueError("TTS_REQUEST_INTERVAL không âm và TTS_TIMEOUT phải > 0.")
        if self.audio_sample_rate not in (16000, 22050, 24000, 44100, 48000):
            raise ValueError("AUDIO_SAMPLE_RATE phải là 16000/22050/24000/44100/48000.")
        if min(self.original_volume, self.voice_volume) < 0 or self.burn_font_size < 1:
            raise ValueError("Volume không âm; BURN_FONT_SIZE phải > 0.")
        if self.tts_rate != "+0%":
            raise ValueError("Phiên bản 1 giữ TTS_RATE=+0%, không tăng tốc giọng đọc.")
        pitch = re.fullmatch(r"([+-])(\d{1,2})Hz", self.tts_pitch)
        if not pitch or int(pitch.group(2)) > 20:
            raise ValueError("TTS_PITCH phải có dạng +0Hz và nằm trong khoảng -20Hz đến +20Hz.")
        if not 1.0 <= self.voice_speed <= 1.5:
            raise ValueError("VOICE_SPEED phải từ 1.0 đến 1.5; chỉ áp dụng cho giọng Việt khi ghép.")
        if (type(self.blur_chinese_subtitles) is not bool or type(self.mirror_video) is not bool
                or type(self.burn_logo) is not bool or type(self.annotation_ocr_enabled) is not bool):
            raise ValueError(
                "BLUR_CHINESE_SUBTITLES, MIRROR_VIDEO, BURN_LOGO và ANNOTATION_OCR_ENABLED "
                "phải là true hoặc false."
            )
        if not 1 <= self.blur_strength <= 50:
            raise ValueError("BLUR_STRENGTH phải từ 1 đến 50.")
        if self.logo_position not in ('top-left', 'top-right', 'bottom-left', 'bottom-right'):
            raise ValueError("LOGO_POSITION phải là top-left/top-right/bottom-left/bottom-right.")
        for name, minimum, maximum in (
            ('logo_width_percent', 2.0, 50.0),
            ('logo_opacity', 0.05, 1.0),
            ('logo_margin_percent', 0.0, 20.0),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError(f"{name.upper()} phải là số hữu hạn.")
            if not minimum <= float(value) <= maximum:
                raise ValueError(f"{name.upper()} phải từ {minimum:g} đến {maximum:g}.")
        if type(self.annotation_font_size) is not int or not 12 <= self.annotation_font_size <= 100:
            raise ValueError("ANNOTATION_FONT_SIZE phải từ 12 đến 100.")


def load_settings(env_file: Path | None = None) -> Settings:
    env_path = Path(env_file).expanduser().resolve() if env_file else PROJECT_DIR / ".env"
    values: dict[str, str | None] = {}
    if env_file and not env_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file cấu hình: {env_path}")
    if env_path.is_file():
        try:
            from dotenv import dotenv_values
        except ImportError as exc:
            raise RuntimeError("Thiếu python-dotenv. Chạy: python -m pip install -r requirements.txt") from exc
        values.update(dotenv_values(env_path, encoding="utf-8-sig"))
    values.update(os.environ)
    default = Settings()
    kwargs: dict[str, object] = {}
    for item in fields(default):
        if item.name == "project_dir":
            continue
        raw = values.get(item.name.upper())
        if raw is None:
            continue
        old = getattr(default, item.name)
        try:
            if isinstance(old, Path):
                path = Path(raw).expanduser()
                kwargs[item.name] = (path if path.is_absolute() else PROJECT_DIR / path).resolve()
            elif isinstance(old, bool):
                normalized = str(raw).strip().lower()
                if normalized in ("1", "true", "yes", "on"):
                    kwargs[item.name] = True
                elif normalized in ("0", "false", "no", "off"):
                    kwargs[item.name] = False
                else:
                    raise ValueError("phải là true hoặc false")
            elif isinstance(old, int):
                kwargs[item.name] = int(raw)
            elif isinstance(old, float):
                kwargs[item.name] = float(raw)
            else:
                kwargs[item.name] = raw
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Giá trị không hợp lệ cho {item.name.upper()} trong cấu hình.") from exc
    return Settings(**kwargs)
