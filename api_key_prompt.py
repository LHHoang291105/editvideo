"""Interactive API key entry; credentials stay in memory for the current run."""
from __future__ import annotations

from dataclasses import replace
import getpass
import sys
import warnings

from config import Settings


def prompt_api_key(settings: Settings, *, force: bool = False) -> Settings:
    """Use the configured key or ask securely when an actual API call needs one."""
    if settings.gemini_api_key.strip() and not force:
        return settings
    if sys.stdin is None or not sys.stdin.isatty():
        raise RuntimeError(
            "Không thể nhập API key trong phiên không tương tác. "
            "Chạy trong PowerShell/terminal tương tác để nhập key, hoặc đặt GEMINI_API_KEY trong .env. "
            "SRT Trung và cache vẫn được giữ nguyên."
        )
    try:
        # Stop if getpass cannot hide input; never fall back to echoed stdin.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Nhập Gemini API key (ẩn ký tự, chỉ dùng cho lần chạy này): ").strip()
    except (EOFError, getpass.GetPassWarning):
        raise RuntimeError(
            "Không đọc được API key bằng chế độ nhập ẩn. "
            "Dùng PowerShell/terminal tương tác hoặc đặt GEMINI_API_KEY trong .env."
        ) from None
    if not key:
        raise RuntimeError("API key trống. Chạy lại và nhập key hoặc cấu hình GEMINI_API_KEY trong .env.")
    return replace(settings, gemini_api_key=key)
