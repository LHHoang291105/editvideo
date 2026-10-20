"""Explicit Paddle device validation; never silently fall back from GPU to CPU."""
from __future__ import annotations

import logging
import re

from config import Settings

LOGGER = logging.getLogger(__name__)


def select_ocr_device(settings: Settings) -> str:
    # Imported after configure_ocr_cache() by the caller.
    try:
        import paddle
    except ImportError as exc:
        raise RuntimeError("Thiếu PaddlePaddle. Cài requirements.txt cho GPU hoặc requirements-cpu.txt cho CPU.") from exc
    device = settings.ocr_device.lower().strip()
    if device == "cpu":
        paddle.set_device("cpu")
        LOGGER.info("OCR dùng CPU (OCR_DEVICE=cpu).")
        return "cpu"
    if not re.fullmatch(r"gpu(?::\d+)?", device):
        raise ValueError("OCR_DEVICE phải là cpu hoặc gpu:<số GPU>, ví dụ gpu:0.")
    if not paddle.is_compiled_with_cuda():
        raise RuntimeError(
            "OCR_DEVICE đang chọn GPU nhưng Paddle hiện tại là bản CPU. "
            "Gỡ paddlepaddle rồi cài requirements.txt; xem mục GPU trong README.md."
        )
    index = int(device.split(":", 1)[1]) if ":" in device else 0
    try:
        count = paddle.device.cuda.device_count()
        if index >= count:
            raise RuntimeError(f"Không thấy GPU số {index}; Paddle nhận {count} GPU. Kiểm tra driver NVIDIA bằng nvidia-smi.")
        selected = f"gpu:{index}"
        paddle.set_device(selected)
        name = paddle.device.cuda.get_device_name(index)
        LOGGER.info("OCR dùng %s — %s | Paddle %s | CUDA %s", selected, name, paddle.__version__, paddle.version.cuda())
        return selected
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Không khởi tạo được GPU cho Paddle ({type(exc).__name__}). Kiểm tra driver/CUDA theo README.md.") from exc
