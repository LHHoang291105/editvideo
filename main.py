"""Chinese hardsub video -> Chinese SRT -> Vietnamese SRT -> dubbed MP4."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import logging
from pathlib import Path
import sys
from typing import Sequence

from common import read_srt, setup_logging, write_srt
from config import Settings, load_settings
from api_key_prompt import prompt_api_key

LOGGER = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    setup_logging("--verbose" in arguments)
    parser = argparse.ArgumentParser(description="Dịch và lồng tiếng video Trung có phụ đề dính bằng PaddleOCR/Gemini/Edge TTS.")
    parser.add_argument("video", type=Path, help="Video MP4 đầu vào")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ocr-only", action="store_true", help="Chỉ xuất SRT tiếng Trung")
    mode.add_argument("--translate", action="store_true", help="OCR nếu cần, dịch và dừng ở SRT Việt để sửa tay")
    mode.add_argument("--manual-translate", action="store_true", help="OCR nếu cần, tạo SRT nháp để tự dịch rồi dừng; không gọi Gemini")
    mode.add_argument("--dub-only", action="store_true", help="Tạo giọng và ghép từ SRT Việt đã dịch; không OCR, không gọi Gemini")
    parser.add_argument("--burn-subtitles", action="store_true", help="Burn SRT Việt lên video kết quả")
    parser.add_argument("--sample-fps", type=float, help="Số frame OCR/giây (mặc định 4)")
    parser.add_argument("--output-dir", type=Path, help="Thư mục SRT/video đầu ra")
    parser.add_argument("--audio-dir", type=Path, help="Thư mục WAV và manifest")
    parser.add_argument("--original-volume", type=float, help="Âm lượng gốc: 0=tắt, 1=nguyên bản")
    parser.add_argument("--voice-volume", type=float, help="Âm lượng giọng Việt: mặc định 1")
    parser.add_argument("--voice-speed", type=float, help="Tốc độ riêng giọng Việt khi ghép: 1.0–1.5")
    parser.add_argument("--blur-chinese-subtitles", action="store_true", default=None,
                        help="Làm mờ dải phụ đề Trung trước khi chèn phụ đề Việt")
    parser.add_argument("--mirror-video", action="store_true", default=None,
                        help="Lật ngang video kiểu gương; phụ đề Việt vẫn đọc bình thường")
    parser.add_argument("--annotation-srt", type=Path,
                        help="SRT Việt cho chú thích trong khung hình; chỉ chèn chữ, không tạo giọng")
    parser.add_argument("--blur-strength", type=int, help="Độ mờ phụ đề Trung: 1–50, mặc định 14")
    parser.add_argument("--reocr", action="store_true", help="Chủ động OCR lại SRT Trung; vẫn giữ SRT Việt hiện có")
    parser.add_argument("--env", type=Path, help="File .env tùy chọn")
    parser.add_argument("--gemini-model", help="Model Gemini dùng cho lần chạy này, ưu tiên hơn .env")
    parser.add_argument("--ask-api-key", action="store_true", help="Nhập Gemini API key bằng chế độ ẩn khi cần dịch, thay key trong .env cho lần chạy này")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(arguments)
    try:
        settings = load_settings(args.env)
        overrides = {}
        for key in ("sample_fps", "original_volume", "voice_volume", "voice_speed",
                    "blur_chinese_subtitles", "mirror_video", "blur_strength",
                    "output_dir", "audio_dir", "gemini_model"):
            value = getattr(args, key)
            if value is not None:
                overrides[key] = value.resolve() if isinstance(value, Path) else value
        settings = replace(settings, **overrides)
        video = args.video.expanduser().resolve()
        if not video.is_file():
            raise FileNotFoundError(f"Không tìm thấy video: {video}")
        if video.suffix.lower() != ".mp4":
            raise ValueError("Phiên bản này nhận video .mp4.")
        zh = settings.output_dir / f"{video.stem}_zh.srt"
        vi = settings.output_dir / f"{video.stem}_vi.srt"
        default_annotation = settings.output_dir / f"{video.stem}_annotation_vi.srt"
        annotation_srt = (
            args.annotation_srt.expanduser().resolve() if args.annotation_srt is not None
            else default_annotation if settings.annotation_ocr_enabled and default_annotation.is_file() else None
        )
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        if args.dub_only:
            if not vi.is_file():
                raise FileNotFoundError(f"Chưa có SRT Việt đã dịch: {vi}. Dịch bản nháp rồi lưu thành file này (UTF-8).")
            read_srt(vi)
            dub_options = {"burn": args.burn_subtitles}
            if annotation_srt is not None:
                dub_options["annotation_srt"] = annotation_srt
            return dub_video(video, vi, settings, **dub_options)
        if zh.exists() and not args.reocr:
            LOGGER.info("Dùng SRT Trung hiện có: %s (dùng --reocr nếu đổi video hoặc ROI)", zh)
            read_srt(zh)
        else:
            from extract_subtitles import extract_subtitles
            LOGGER.info("Bước 1/4: OCR vùng phụ đề dưới video.")
            extract_subtitles(video, zh, settings, overwrite=args.reocr)
        if args.ocr_only:
            LOGGER.info("Hoàn tất OCR: %s", zh)
            return 0
        if args.manual_translate:
            if vi.exists():
                LOGGER.info("Đã có SRT Việt, giữ nguyên để bạn sửa: %s", vi)
            else:
                draft = vi.with_name(f"{video.stem}_vi.draft.srt")
                if not draft.exists():
                    write_srt(draft, read_srt(zh))
                LOGGER.info("Bản nháp để dịch tay (hiện chứa lời Trung): %s", draft)
                LOGGER.info("Thay lời Trung bằng lời Việt, giữ ID/timestamp, lưu UTF-8 thành: %s", vi)
            LOGGER.info("Dịch xong chạy lại với --dub-only để tạo giọng và ghép video.")
            return 0
        from translate_subtitles import translate_subtitles
        LOGGER.info("Bước 2/4: Dịch SRT Trung sang Việt (giữ bản Việt đã tồn tại).")
        if args.reocr and vi.exists():
            LOGGER.warning("Đã OCR lại nhưng SRT Việt vẫn giữ nguyên; hãy kiểm tra ID/nội dung/timing bản Việt trước khi lồng tiếng.")
        translate_subtitles(
            zh, vi, settings,
            key_provider=lambda current: prompt_api_key(current, force=args.ask_api_key),
        )
        if args.translate:
            LOGGER.info("Đã có SRT Việt để sửa tay: %s", vi)
            return 0
        dub_options = {"burn": args.burn_subtitles}
        if annotation_srt is not None:
            dub_options["annotation_srt"] = annotation_srt
        return dub_video(video, vi, settings, **dub_options)
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        LOGGER.info("Các SRT đã xuất vẫn còn. Sửa lỗi rồi chạy lại để tiếp tục.")
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Đã dừng. Các SRT và audio đã lưu được giữ nguyên.")
        return 130


def dub_video(
    video: Path, vi: Path, settings: Settings, *, burn: bool = False,
    annotation_srt: Path | None = None,
) -> int:
    """Use the supplied SRT directly, without translation or OCR."""
    from tts_from_srt import tts_from_srt
    from mix_audio import mix_audio
    from prepare_tts_srt import prepare_tts_srt
    speech_srt = prepare_tts_srt(vi)
    LOGGER.info("Tạo giọng Việt từ SRT đã gộp câu lặp: %s", speech_srt)
    asyncio.run(tts_from_srt(speech_srt, settings))
    LOGGER.info("Đặt WAV vào đúng start_time SRT và ghép video.")
    mix_options = {"burn": burn, "display_srt": vi}
    if annotation_srt is not None:
        mix_options["annotation_srt"] = annotation_srt
    final = mix_audio(video, speech_srt, settings, **mix_options)
    LOGGER.info("Hoàn tất: %s | Phụ đề: %s", final, vi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
