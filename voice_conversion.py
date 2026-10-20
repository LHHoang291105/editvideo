"""Offline RVC voice-conversion pipeline used by the VietDub Studio voice lab.

The main application deliberately does not import PyTorch or RVC.  RVC runs in
its own CUDA virtual environment through the upstream ``infer/cli.py`` entry
point, so the existing PaddleOCR environment remains stable.  This module only
uses standard Python plus the project's FFmpeg wrapper.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Final
import uuid

from config import Settings
from media import find_binary, run_ffmpeg


LOGGER = logging.getLogger(__name__)

SUPPORTED_INPUT_SUFFIXES: Final[frozenset[str]] = frozenset({".wav", ".mp3", ".mp4"})
RVC_MODEL_SUFFIX: Final[str] = ".pth"
RVC_INDEX_SUFFIX: Final[str] = ".index"


@dataclass(frozen=True)
class MediaDescription:
    """Audio/video capabilities required by the conversion and mux stages."""

    duration: float
    has_audio: bool
    has_video: bool


@dataclass(frozen=True)
class VoicePreset:
    """A conservative style profile for a supplied female RVC voice model.

    RVC changes the vocal identity.  ``pitch_semitones`` is passed to RVC's F0
    conditioning rather than applying a crude resample after conversion.  The
    values are intentionally small because an appropriate female model already
    supplies the important vocal-tract/formant character.
    """

    key: str
    label: str
    description: str
    pitch_semitones: int
    index_rate: float
    rms_mix_rate: float
    protect: float
    equalizer: str


VOICE_PRESETS: Final[dict[str, VoicePreset]] = {
    "cute": VoicePreset(
        key="cute",
        label="Nữ cute",
        description="Sáng, trẻ, nhẹ; tăng F0 vừa phải qua RMVPE/RVC.",
        pitch_semitones=2,
        index_rate=0.75,
        rms_mix_rate=0.38,
        protect=0.45,
        equalizer="equalizer=f=3400:t=q:w=1.1:g=1.5",
    ),
    "bright": VoicePreset(
        key="bright",
        label="Nữ sáng",
        description="Rõ và sáng hơn, hợp giọng nói ngắn hoặc video vui.",
        pitch_semitones=2,
        index_rate=0.70,
        rms_mix_rate=0.34,
        protect=0.42,
        equalizer="equalizer=f=3900:t=q:w=1.2:g=2.0",
    ),
    "soft": VoicePreset(
        key="soft",
        label="Nữ mềm",
        description="Mềm, ít chói, vẫn giữ nhịp điệu của câu nói gốc.",
        pitch_semitones=1,
        index_rate=0.78,
        rms_mix_rate=0.50,
        protect=0.48,
        equalizer="equalizer=f=3000:t=q:w=1.0:g=0.7",
    ),
}


@dataclass(frozen=True)
class VoiceConversionRequest:
    """Fully validated inputs for one offline RVC conversion."""

    source: Path
    model: Path
    index: Path | None
    preset: str = "cute"
    speaker_id: int | None = None
    pitch_semitones: int | None = None
    index_rate: float | None = None
    rms_mix_rate: float | None = None
    protect: float | None = None
    post_pitch_semitones: float = 0.0

    def __post_init__(self) -> None:
        source = Path(self.source).expanduser().resolve()
        model = Path(self.model).expanduser().resolve()
        index = Path(self.index).expanduser().resolve() if self.index else None
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "index", index)
        if source.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            raise ValueError("Nguồn giọng chỉ hỗ trợ WAV, MP3 hoặc MP4.")
        if not source.is_file():
            raise FileNotFoundError(f"Không tìm thấy file nguồn: {source}")
        if model.suffix.lower() != RVC_MODEL_SUFFIX or not model.is_file():
            raise ValueError("Chọn một model RVC .pth có thật trên máy.")
        if index is not None and (index.suffix.lower() != RVC_INDEX_SUFFIX or not index.is_file()):
            raise ValueError("File index RVC phải là .index có thật trên máy.")
        if self.preset not in VOICE_PRESETS:
            raise ValueError("Preset giọng không hợp lệ.")
        if self.speaker_id is not None and (type(self.speaker_id) is not int or not 0 <= self.speaker_id <= 109):
            raise ValueError("Speaker ID phải là số nguyên từ 0 đến 109.")
        if self.pitch_semitones is not None and (type(self.pitch_semitones) is not int or not -12 <= self.pitch_semitones <= 12):
            raise ValueError("Pitch RVC phải là số nguyên từ -12 đến 12 bán âm.")
        for name, value in (("index_rate", self.index_rate), ("rms_mix_rate", self.rms_mix_rate)):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1):
                raise ValueError(f"{name} phải từ 0 đến 1.")
        if self.protect is not None and (type(self.protect) not in (int, float) or not math.isfinite(float(self.protect)) or not 0 <= float(self.protect) <= 0.5):
            raise ValueError("protect phải từ 0 đến 0.5.")
        if type(self.post_pitch_semitones) not in (int, float) or not math.isfinite(float(self.post_pitch_semitones)) or not -2 <= float(self.post_pitch_semitones) <= 2:
            raise ValueError("Tinh chỉnh pitch hậu kỳ phải từ -2 đến 2 bán âm.")

    @property
    def profile(self) -> VoicePreset:
        return VOICE_PRESETS[self.preset]

    @property
    def effective_pitch(self) -> int:
        return self.profile.pitch_semitones if self.pitch_semitones is None else self.pitch_semitones

    @property
    def effective_index_rate(self) -> float:
        return self.profile.index_rate if self.index_rate is None else float(self.index_rate)

    @property
    def effective_rms_mix_rate(self) -> float:
        return self.profile.rms_mix_rate if self.rms_mix_rate is None else float(self.rms_mix_rate)

    @property
    def effective_protect(self) -> float:
        return self.profile.protect if self.protect is None else float(self.protect)


@dataclass(frozen=True)
class VoiceConversionResult:
    """Published output paths.  ``video`` is present only for MP4 input."""

    audio: Path
    video: Path | None
    input_duration: float
    output_duration: float
    used_gpu: bool


def preset_public_data() -> list[dict[str, object]]:
    """Return browser-safe preset metadata without exposing internal paths."""
    return [
        {
            "key": preset.key,
            "label": preset.label,
            "description": preset.description,
            "pitch": preset.pitch_semitones,
        }
        for preset in VOICE_PRESETS.values()
    ]


def rvc_paths(settings: Settings) -> tuple[Path, Path, Path]:
    """Return RVC root, isolated Python and upstream offline CLI location."""
    root = Path(settings.rvc_root).expanduser().resolve()
    python = Path(settings.rvc_python).expanduser().resolve()
    return root, python, root / "infer" / "cli.py"


def rvc_runtime_status(settings: Settings) -> dict[str, object]:
    """Diagnose only filesystem prerequisites; importing torch stays isolated."""
    root, python, cli = rvc_paths(settings)
    errors: list[str] = []
    if not python.is_file():
        errors.append("Chưa có Python runtime RVC. Chạy tools\\setup_rvc_gpu.ps1 một lần.")
    if not cli.is_file():
        errors.append("Chưa có mã nguồn RVC. Chạy tools\\setup_rvc_gpu.ps1 một lần.")
    hubert = root / "assets" / "hubert_base" / "pytorch_model.bin"
    rmvpe = root / "assets" / "rmvpe" / "rmvpe.pt"
    if root.is_dir() and not hubert.is_file():
        errors.append("RVC thiếu model Hubert Base cho inference.")
    if root.is_dir() and not rmvpe.is_file():
        errors.append("RVC thiếu assets/rmvpe/rmvpe.pt.")
    return {
        "ready": not errors,
        "gpu_device": settings.rvc_cuda_device,
        "root_name": root.name,
        "errors": errors,
    }


def verify_rvc_cuda(settings: Settings) -> str:
    """Require a real CUDA-capable torch in the isolated RVC environment.

    This check happens once per conversion worker, never in the browser's
    frequent project polling path.  It intentionally fails rather than letting
    upstream RVC quietly select CPU when the feature was requested as GPU.
    """
    status = rvc_runtime_status(settings)
    if not status["ready"]:
        raise RuntimeError(" ".join(str(item) for item in status["errors"]))
    root, python, _ = rvc_paths(settings)
    code = (
        "import json,sys,torch; "
        "available=torch.cuda.is_available(); "
        "count=torch.cuda.device_count() if available else 0; "
        "print(json.dumps({'available':available,'count':count," 
        "'torch':torch.__version__,'cuda':torch.version.cuda},ensure_ascii=False)); "
        "sys.exit(0 if available else 2)"
    )
    result = subprocess.run(
        [str(python), "-c", code], cwd=root, capture_output=True, encoding="utf-8", errors="replace",
        check=False, timeout=90,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1500:]
        raise RuntimeError(
            "RVC runtime không thấy CUDA GPU. Chạy lại tools\\setup_rvc_gpu.ps1 hoặc kiểm tra driver NVIDIA. "
            + detail
        )
    try:
        info = json.loads(result.stdout.strip().splitlines()[-1])
        count = int(info["count"])
    except (IndexError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("Không đọc được trạng thái CUDA của RVC runtime.") from exc
    if settings.rvc_cuda_device >= count:
        raise RuntimeError(
            f"RVC_CUDA_DEVICE={settings.rvc_cuda_device} không tồn tại; PyTorch chỉ thấy {count} GPU CUDA."
        )
    # ``torch.cuda.is_available()`` alone is not enough: upstream RVC applies
    # its own GPU-memory/compute-capability rule and otherwise silently uses
    # CPU.  Restrict the child to the requested adapter exactly as the real
    # inference subprocess does, then require its Config to select CUDA.
    rvc_code = (
        "import json,sys,torch; "
        "from configs.config import Config; "
        "config=Config(); device=str(config.device); "
        "gpu=device.startswith('cuda'); "
        "name=torch.cuda.get_device_name(0) if gpu else ''; "
        "print(json.dumps({'device':device,'name':name},ensure_ascii=False)); "
        "sys.exit(0 if gpu else 3)"
    )
    rvc_environment = {
        **os.environ,
        "CUDA_VISIBLE_DEVICES": str(settings.rvc_cuda_device),
        "PYTHONIOENCODING": "utf-8",
    }
    rvc_result = subprocess.run(
        [str(python), "-c", rvc_code], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=False, timeout=90, env=rvc_environment,
    )
    if rvc_result.returncode:
        detail = (rvc_result.stderr or rvc_result.stdout).strip()[-1500:]
        raise RuntimeError(
            "PyTorch thấy CUDA nhưng upstream RVC sẽ fallback về CPU trên GPU đã chọn. "
            "Cần GPU NVIDIA đáp ứng rule của RVC (ít nhất 4 GiB VRAM và SM 5.3). "
            + detail
        )
    try:
        rvc_info = json.loads(rvc_result.stdout.strip().splitlines()[-1])
        device = str(rvc_info["device"])
        name = str(rvc_info["name"]).strip()
    except (IndexError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("Không đọc được thiết bị CUDA mà upstream RVC đã chọn.") from exc
    if not device.startswith("cuda") or not name:
        raise RuntimeError("Upstream RVC không chọn được CUDA GPU đã cấu hình.")
    return name


def probe_voice_media(path: Path, settings: Settings) -> MediaDescription:
    """Read media streams without assuming the input contains a video stream."""
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError("Nguồn giọng chỉ hỗ trợ WAV, MP3 hoặc MP4.")
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file nguồn: {path}")
    binary = find_binary("ffprobe", settings.ffprobe_bin)
    result = subprocess.run(
        [binary, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"FFprobe không đọc được {path.name}: {result.stderr[-3000:]}")
    try:
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Không đọc được thời lượng audio của {path.name}.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"File nguồn không có thời lượng audio hợp lệ: {path.name}")
    stream_types = {str(stream.get("codec_type")) for stream in data.get("streams", [])}
    if "audio" not in stream_types:
        raise ValueError("File nguồn không có audio stream để đổi giọng.")
    return MediaDescription(duration=duration, has_audio=True, has_video="video" in stream_types)


def _pending_path(output: Path) -> Path:
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.with_name(f".{output.stem}.{uuid.uuid4().hex}.pending{output.suffix}")


def _publish(pending: Path, output: Path) -> Path:
    pending = Path(pending).resolve()
    output = Path(output).resolve()
    if not pending.is_file() or pending.stat().st_size == 0:
        raise RuntimeError(f"Không tạo được audio kết quả: {output.name}")
    os.replace(pending, output)
    return output


def _preprocess_audio(source: Path, output: Path, settings: Settings) -> Path:
    """Extract clean mono PCM while preserving speech timing and pauses."""
    pending = _pending_path(output)
    try:
        LOGGER.info("Voice Lab: tách và chuẩn hóa audio nguồn.")
        run_ffmpeg([
            "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000",
            "-af", "highpass=f=55,lowpass=f=16000", "-c:a", "pcm_s16le", str(pending),
        ], settings)
        return _publish(pending, output)
    finally:
        pending.unlink(missing_ok=True)


def _run_rvc(command: list[str], *, root: Path, device: int, settings: Settings) -> None:
    """Run upstream RVC, relay its output to the app job log, and retain context."""
    ffmpeg_directory = str(Path(find_binary("ffmpeg", settings.ffmpeg_bin)).parent)
    environment = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "CUDA_VISIBLE_DEVICES": str(device),
        # ``infer/cli.py`` is launched as a script.  Its imports use the
        # top-level ``infer`` package, so put the pinned RVC checkout itself
        # on the child path on Windows as well as using it for ``cwd``.
        "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        # Upstream RVC invokes FFmpeg internally for some model/audio paths;
        # the app's private FFmpeg is made visible without changing PATH for
        # the parent desktop process.
        "PATH": ffmpeg_directory + os.pathsep + os.environ.get("PATH", ""),
    }
    LOGGER.info("Voice Lab: RVC + RMVPE chạy trên CUDA GPU %d.", device)
    LOGGER.debug("RVC command: %r", command)
    process = subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    tail: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            cleaned = line.rstrip()
            if cleaned:
                LOGGER.info("RVC: %s", cleaned)
                tail.append(cleaned)
                if len(tail) > 60:
                    tail.pop(0)
        code = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    if code:
        detail = "\n".join(tail[-30:])
        raise RuntimeError(
            "RVC không chuyển đổi được audio. Kiểm tra model .pth, index .index, "
            f"CUDA/RMVPE và log RVC.\n{detail}"
        )


def rvc_command(request: VoiceConversionRequest, input_wav: Path, output_wav: Path, settings: Settings) -> list[str]:
    """Build the documented upstream CLI call with RMVPE, never shell text."""
    root, python, cli = rvc_paths(settings)
    if not python.is_file() or not cli.is_file():
        details = "; ".join(str(item) for item in rvc_runtime_status(settings)["errors"])
        raise RuntimeError(details or "Không tìm thấy RVC runtime.")
    command = [
        str(python), "-u", str(cli), "--model", str(request.model),
        "--input", str(input_wav), "--output", str(output_wav),
        "--pitch", str(request.effective_pitch), "--f0-method", "rmvpe",
        "--index-rate", f"{request.effective_index_rate:.3f}",
        "--rms-mix-rate", f"{request.effective_rms_mix_rate:.3f}",
        "--protect", f"{request.effective_protect:.3f}", "--format", "wav", "--overwrite",
    ]
    if request.index is not None:
        command.extend(["--index", str(request.index)])
    else:
        # Current upstream RVC intentionally errors if index-rate > 0 with no
        # matching index.  Make the behavior explicit and deterministic.
        command[command.index("--index-rate") + 1] = "0.000"
    if request.speaker_id is not None:
        command.extend(["--speaker-id", str(request.speaker_id)])
    return command


def _ffmpeg_supports_rubberband(settings: Settings) -> bool:
    binary = find_binary("ffmpeg", settings.ffmpeg_bin)
    result = subprocess.run([binary, "-hide_banner", "-filters"], capture_output=True,
                            encoding="utf-8", errors="replace", check=False, timeout=30)
    return result.returncode == 0 and "rubberband" in result.stdout.lower()


def _postprocess_filters(request: VoiceConversionRequest, settings: Settings) -> str:
    """Polish the RVC result without using a chipmunk-style resample.

    When an advanced fine tune is explicitly requested, FFmpeg's Rubber Band
    filter shifts pitch while preserving formants.  Normal presets do all F0
    work inside RVC/RMVPE and therefore do not depend on Rubber Band.
    """
    profile = request.profile
    filters = ["highpass=f=55", profile.equalizer]
    if abs(float(request.post_pitch_semitones)) > 0.001:
        if not _ffmpeg_supports_rubberband(settings):
            raise RuntimeError(
                "FFmpeg hiện tại không có filter Rubber Band. Đặt Tinh chỉnh F0 hậu kỳ về 0, "
                "hoặc dùng bản FFmpeg có librubberband."
            )
        ratio = 2 ** (float(request.post_pitch_semitones) / 12.0)
        filters.append(
            f"rubberband=pitch={ratio:.8f}:formant=preserved:transients=crisp:phase=laminar:window=long"
        )
    # A fixed one-pass target is predictable for a local editor.  The limiter
    # prevents an RVC peak from clipping during AAC muxing.
    filters.extend(["loudnorm=I=-16:LRA=11:TP=-1.5", "alimiter=limit=0.95"])
    return ",".join(filters)


def _postprocess_audio(converted: Path, output: Path, request: VoiceConversionRequest, settings: Settings) -> Path:
    pending = _pending_path(output)
    try:
        LOGGER.info("Voice Lab: hậu xử lý âm lượng và độ sáng giọng.")
        run_ffmpeg([
            "-i", str(converted), "-af", _postprocess_filters(request, settings),
            "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(pending),
        ], settings)
        return _publish(pending, output)
    finally:
        pending.unlink(missing_ok=True)


def _mux_voice_to_video(source: Path, voice_wav: Path, output: Path, duration: float, settings: Settings) -> Path:
    """Keep the source image and replace only its audio with converted voice."""
    pending = _pending_path(output)
    try:
        LOGGER.info("Voice Lab: ghép giọng đã đổi trở lại MP4.")
        target_duration = f"{duration:.6f}"
        run_ffmpeg([
            "-i", str(source), "-i", str(voice_wav),
            "-filter_complex", f"[1:a]apad=pad_dur={target_duration},atrim=duration={target_duration}[voice]",
            "-map", "0:v:0", "-map", "[voice]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(pending),
        ], settings)
        return _publish(pending, output)
    finally:
        pending.unlink(missing_ok=True)


def convert_voice(
    request: VoiceConversionRequest,
    *,
    audio_output: Path,
    video_output: Path | None,
    work_dir: Path,
    settings: Settings,
) -> VoiceConversionResult:
    """Run the full offline path: media -> RVC/RMVPE -> polish -> optional MP4.

    All intermediate files live in ``work_dir``.  Both the polished WAV and
    the optional MP4 are staged before either published output is replaced, so
    a mux failure cannot overwrite a previously completed WAV.
    """
    gpu_name = verify_rvc_cuda(settings)
    LOGGER.info("Voice Lab: xác nhận CUDA GPU %d: %s.", settings.rvc_cuda_device, gpu_name)
    description = probe_voice_media(request.source, settings)
    if video_output is not None and not description.has_video:
        raise ValueError("Chỉ nguồn MP4 có hình mới ghép được lại thành video.")
    work_dir = Path(work_dir).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    source_wav = work_dir / f"{token}_source.wav"
    rvc_wav = work_dir / f"{token}_rvc.wav"
    staged_audio = work_dir / f"{token}_post.wav"
    staged_video = work_dir / f"{token}_mux.mp4" if video_output is not None else None
    audio_target = Path(audio_output).expanduser().resolve()
    video_target = Path(video_output).expanduser().resolve() if video_output is not None else None
    audio_target.parent.mkdir(parents=True, exist_ok=True)
    if video_target is not None:
        video_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _preprocess_audio(request.source, source_wav, settings)
        command = rvc_command(request, source_wav, rvc_wav, settings)
        root, _, _ = rvc_paths(settings)
        _run_rvc(command, root=root, device=settings.rvc_cuda_device, settings=settings)
        if not rvc_wav.is_file() or rvc_wav.stat().st_size == 0:
            raise RuntimeError("RVC kết thúc nhưng không tạo WAV đầu ra.")
        _postprocess_audio(rvc_wav, staged_audio, request, settings)
        output_description = probe_voice_media(staged_audio, settings)
        if abs(output_description.duration - description.duration) > 0.20:
            LOGGER.warning(
                "Voice Lab: WAV kết quả lệch %.3fs so với nguồn (%.3fs -> %.3fs).",
                output_description.duration - description.duration, description.duration, output_description.duration,
            )
        if staged_video is not None:
            _mux_voice_to_video(request.source, staged_audio, staged_video, description.duration, settings)
        # Do not expose the new WAV until the optional MP4 has also been
        # rendered successfully.  ``os.replace`` makes each final publication
        # atomic on Windows, while the staging above protects old results from
        # ordinary RVC/FFmpeg failures.
        audio = _publish(staged_audio, audio_target)
        video = _publish(staged_video, video_target) if staged_video is not None and video_target is not None else None
        LOGGER.info("Voice Lab: hoàn tất preset %s.", request.profile.label)
        return VoiceConversionResult(
            audio=audio, video=video, input_duration=description.duration,
            output_duration=output_description.duration, used_gpu=True,
        )
    finally:
        source_wav.unlink(missing_ok=True)
        rvc_wav.unlink(missing_ok=True)
        staged_audio.unlink(missing_ok=True)
        if staged_video is not None:
            staged_video.unlink(missing_ok=True)
