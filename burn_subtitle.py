"""Render optional subtitle blur/mirror effects and UTF-8 Vietnamese subtitles."""
from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import re
import subprocess
import sys
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from annotation_overlay import write_annotation_ass
from common import read_srt, setup_logging, write_srt
from config import Settings, load_settings
from extract_subtitles import OCRRegion
from media import find_binary, probe_media, run_ffmpeg

LOGGER = logging.getLogger(__name__)


def check_required_filters(settings: Settings, required: set[str]) -> None:
    """Fail early with a useful error if the installed FFmpeg lacks an effect."""
    ffmpeg = find_binary("ffmpeg", settings.ffmpeg_bin)
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"], capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Không kiểm tra được FFmpeg filters: {result.stderr.strip()}")
    # FFmpeg documents a unary video filter as ``V->V``, but overlay and
    # scale2ref take two video inputs (``VV->V`` / ``VV->VV``).  Looking for
    # the video-input/video-output shape covers both without matching an
    # unrelated help line.
    missing = [
        name for name in required
        if not re.search(rf"\b{re.escape(name)}\s+\S*V\S*->\S*V\b", result.stdout)
    ]
    if not missing:
        return
    if "subtitles" in missing:
        raise RuntimeError("FFmpeg này thiếu filter subtitles/libass. Cài bản FFmpeg full có libass để burn SRT.")
    raise RuntimeError(f"FFmpeg này thiếu filter cần thiết: {', '.join(sorted(missing))}.")


def check_subtitles_filter(settings: Settings) -> None:
    """Backward-compatible public check used by existing integrations."""
    check_required_filters(settings, {"subtitles"})


def _ratio(value: float) -> str:
    return f"{value:.6g}"


def _logo_position(settings: Settings) -> tuple[str, str]:
    """Return safe overlay coordinates for a configured, validated corner."""
    margin = _ratio(settings.logo_margin_percent / 100)
    positions = {
        "top-left": (f"main_w*{margin}", f"main_h*{margin}"),
        "top-right": (f"main_w-overlay_w-main_w*{margin}", f"main_h*{margin}"),
        "bottom-left": (f"main_w*{margin}", f"main_h-overlay_h-main_h*{margin}"),
        "bottom-right": (
            f"main_w-overlay_w-main_w*{margin}",
            f"main_h-overlay_h-main_h*{margin}",
        ),
    }
    # Settings validates this, but keep graph construction safe when a test or
    # integration supplies a Settings-like object directly.
    try:
        return positions[settings.logo_position]
    except KeyError as exc:
        raise ValueError("Vị trí logo không hợp lệ.") from exc


def build_video_filter(
    settings: Settings, subtitle_filter: str | None = None, *, logo_overlay: bool = False,
    annotation_filter: str | None = None,
) -> str:
    """Build: blur source subtitle band -> mirror -> logo -> Vietnamese subtitle.

    Blur coordinates refer to the original input video.  Mirroring afterwards
    keeps an asymmetric subtitle band attached to the same source pixels.  The
    Vietnamese SRT is deliberately the final filter so its text stays readable.
    The logo is a second FFmpeg input, therefore its Windows path never enters
    the filter graph and it is not mirrored together with the source video.
    """
    filters: list[str] = []
    current = "[0:v:0]"
    label = 0

    def next_label() -> str:
        nonlocal label
        label += 1
        return f"[video_{label}]"

    if settings.blur_chinese_subtitles:
        left, top = _ratio(settings.blur_left), _ratio(settings.blur_top)
        width = _ratio(settings.blur_right - settings.blur_left)
        height = _ratio(settings.blur_bottom - settings.blur_top)
        filters.append(f"{current}split=2[base][subtitle_region]")
        filters.append(
            "[subtitle_region]"
            f"crop=w=trunc(iw*{width}/2)*2:h=trunc(ih*{height}/2)*2:"
            f"x=trunc(iw*{left}/2)*2:y=trunc(ih*{top}/2)*2,"
            f"boxblur=luma_radius={settings.blur_strength}:luma_power=1:"
            f"chroma_radius={settings.blur_strength}:chroma_power=1[blurred_region]"
        )
        current = next_label()
        filters.append(
            f"[base][blurred_region]overlay=x=trunc(main_w*{left}/2)*2:"
            f"y=trunc(main_h*{top}/2)*2{current}"
        )

    if settings.mirror_video:
        target = next_label()
        filters.append(f"{current}hflip{target}")
        current = target

    if logo_overlay:
        width = _ratio(settings.logo_width_percent / 100)
        opacity = _ratio(settings.logo_opacity)
        x, y = _logo_position(settings)
        # scale2ref derives an even pixel width from the actual source frame;
        # it works for portrait and landscape video without probing dimensions
        # in Python.  [1:v:0] is the looping logo image, [current] is video.
        filters.append(
            f"[1:v:0]{current}scale2ref=w='trunc(ref_w*{width}/2)*2':h=-2"
            "[logo_scaled][logo_base]"
        )
        filters.append(f"[logo_scaled]format=rgba,colorchannelmixer=aa={opacity}[logo]")
        target = next_label()
        filters.append(
            f"[logo_base][logo]overlay=x={x}:y={y}:eof_action=pass:repeatlast=1{target}"
        )
        current = target

    if annotation_filter:
        # In-frame labels are positioned by their ASS tags.  They are added
        # after source blur/mirror and after a logo, but before normal bottom
        # dialogue so the ordinary subtitle remains the topmost text layer.
        target = next_label()
        filters.append(f"{current}{annotation_filter}{target}")
        current = target

    target = "[video]"
    if subtitle_filter:
        # Deliberately last: Vietnamese glyphs are never mirrored or blurred.
        filters.append(f"{current}{subtitle_filter}{target}")
    else:
        filters.append(f"{current}null{target}")
    return ";".join(filters)


def render_video(
    video: Path, srt_path: Path | None, output: Path, settings: Settings, *, burn_subtitles: bool,
    logo_path: Path | None = None, annotation_srt: Path | None = None,
) -> Path:
    """Apply selected visual effects, with subtitle burn optionally enabled."""
    video, output = video.resolve(), output.resolve()
    srt_path = srt_path.resolve() if srt_path else None
    logo_path = logo_path.resolve() if logo_path else None
    annotation_srt = annotation_srt.resolve() if annotation_srt else None
    if output in {video, srt_path, annotation_srt}:
        raise ValueError("File xuất phải khác video và SRT đầu vào.")
    if settings.burn_logo and logo_path is None:
        raise ValueError("Đã bật chèn logo nhưng chưa chọn ảnh logo.")
    if logo_path is not None and (not logo_path.is_file() or logo_path.stat().st_size == 0):
        raise FileNotFoundError("Không tìm thấy ảnh logo hợp lệ để chèn vào video.")
    if annotation_srt is not None and not annotation_srt.is_file():
        raise FileNotFoundError("Không tìm thấy SRT chú thích Việt để chèn vào video.")
    if not burn_subtitles and not (
        settings.blur_chinese_subtitles or settings.mirror_video or logo_path or annotation_srt
    ):
        raise ValueError("Chưa chọn hiệu ứng hình ảnh hoặc chèn phụ đề.")
    if burn_subtitles:
        if srt_path is None:
            raise ValueError("Cần SRT Việt khi chọn chèn phụ đề.")
        cues = read_srt(srt_path)
    else:
        cues = []
    if annotation_srt is not None:
        # Validate before FFmpeg starts, preserving the prior final video on a
        # malformed hand-edited annotation SRT.
        read_srt(annotation_srt)
    info = probe_media(video, settings)
    required: set[str] = set()
    if burn_subtitles or annotation_srt is not None:
        required.add("subtitles")
    if settings.blur_chinese_subtitles:
        required.add("boxblur")
    if settings.mirror_video:
        required.add("hflip")
    if logo_path is not None:
        required.update({"overlay", "scale2ref", "colorchannelmixer"})
    check_required_filters(settings, required)
    if burn_subtitles and (not settings.burn_font.strip() or any(c in settings.burn_font for c in "'\\:,;[]\n\r=")):
        raise ValueError("BURN_FONT chứa ký tự không được hỗ trợ trong FFmpeg filter.")
    if burn_subtitles and not 1 <= settings.burn_font_size <= 200:
        raise ValueError("BURN_FONT_SIZE phải trong khoảng 1..200.")
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.mp4")
    try:
        with TemporaryDirectory(prefix="render_", dir=settings.temp_dir) as temporary:
            directory = Path(temporary).resolve()
            subtitle_filter = None
            if burn_subtitles:
                # The filter sees only an ASCII relative filename, never D:\\, apostrophes,
                # spaces, brackets, or Unicode in a Windows absolute input path.
                write_srt(directory / "subtitles.srt", cues)
                style = (
                    f"FontName={settings.burn_font},FontSize={settings.burn_font_size},"
                    "Outline=2,Shadow=0,Alignment=2,MarginV=20"
                )
                subtitle_filter = f"subtitles=filename=subtitles.srt:charenc=UTF-8:force_style='{style}'"
            annotation_filter = None
            if annotation_srt is not None:
                # An ASCII filename in the current temp folder avoids all
                # Windows-path escaping issues inside the subtitles filter.
                annotation_region = OCRRegion.annotation_region(settings)
                if settings.mirror_video:
                    # The source pixels move when hflip runs.  The Vietnamese
                    # ASS is added afterwards so it stays readable, therefore
                    # its placement must follow the mirrored source location.
                    annotation_region = OCRRegion(
                        1 - annotation_region.right, annotation_region.top,
                        1 - annotation_region.left, annotation_region.bottom,
                    )
                write_annotation_ass(
                    annotation_srt, directory / "annotations.ass", annotation_region,
                    font_size=settings.annotation_font_size,
                    display_y=settings.annotation_display_y,
                )
                annotation_filter = "subtitles=filename=annotations.ass:charenc=UTF-8"
            graph = build_video_filter(
                settings, subtitle_filter, logo_overlay=logo_path is not None,
                annotation_filter=annotation_filter,
            )
            effects: list[str] = []
            if settings.blur_chinese_subtitles:
                effects.append("làm mờ phụ đề Trung")
            if settings.mirror_video:
                effects.append("lật gương video")
            if logo_path is not None:
                effects.append(f"chèn logo {logo_path.name}")
            if annotation_srt is not None:
                effects.append("chèn chú thích Việt trong khung hình")
            if burn_subtitles:
                effects.append("chèn phụ đề Việt")
            LOGGER.info("FFmpeg: %s", ", ".join(effects))
            command = ["-i", str(video)]
            if logo_path is not None:
                # The static image has to remain available for the entire
                # primary-video timeline.  -t below always bounds the result.
                command.extend(["-loop", "1", "-framerate", "30", "-i", str(logo_path)])
            command.extend([
                "-filter_complex", graph, "-map", "[video]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-t", f"{info.duration:.9f}", "-movflags", "+faststart", str(pending),
            ])
            run_ffmpeg(command, settings, cwd=directory)
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    return output


def burn_subtitle(
    video: Path, srt_path: Path, output: Path, settings: Settings, *, logo_path: Path | None = None,
    annotation_srt: Path | None = None,
) -> Path:
    return render_video(
        video, srt_path, output, settings, burn_subtitles=True, logo_path=logo_path,
        annotation_srt=annotation_srt,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Burn SRT tiếng Việt vào MP4 bằng FFmpeg/libass.")
    parser.add_argument("video", type=Path)
    parser.add_argument("srt", type=Path, nargs="?",
                        help="SRT thoại Việt để burn ở đáy video (tùy chọn nếu chỉ dùng --annotation-srt)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--blur-chinese-subtitles", action="store_true", default=None)
    parser.add_argument("--blur-strength", type=int)
    parser.add_argument("--mirror-video", action="store_true", default=None)
    parser.add_argument("--logo", type=Path, help="Ảnh logo PNG/JPG/WebP để chèn vào video")
    parser.add_argument("--logo-position", choices=("top-left", "top-right", "bottom-left", "bottom-right"))
    parser.add_argument("--logo-width-percent", type=float, help="Độ rộng logo theo %% chiều rộng video (2–50)")
    parser.add_argument("--logo-opacity", type=float, help="Độ trong suốt logo từ 0.05 đến 1")
    parser.add_argument("--logo-margin-percent", type=float, help="Lề logo theo %% cạnh video (0–20)")
    parser.add_argument("--annotation-srt", type=Path,
                        help="SRT Việt chú thích trong khung hình; có thể dùng riêng, không cần OCR Trung")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    setup_logging("--verbose" in sys.argv[1:])
    args = build_parser().parse_args()
    try:
        settings = load_settings(args.env)
        changes = {key: value for key, value in {
            "blur_chinese_subtitles": args.blur_chinese_subtitles,
            "blur_strength": args.blur_strength,
            "mirror_video": args.mirror_video,
            "logo_position": args.logo_position,
            "logo_width_percent": args.logo_width_percent,
            "logo_opacity": args.logo_opacity,
            "logo_margin_percent": args.logo_margin_percent,
        }.items() if value is not None}
        if args.logo is not None:
            changes["burn_logo"] = True
        if changes:
            settings = replace(settings, **changes)
        output = args.output or settings.output_dir / "final_video_burned.mp4"
        if args.srt is None and args.annotation_srt is None:
            raise ValueError("Cần SRT thoại hoặc --annotation-srt để chèn vào video.")
        if args.srt is None:
            render_video(
                args.video, None, output, settings, burn_subtitles=False,
                logo_path=args.logo, annotation_srt=args.annotation_srt,
            )
        else:
            burn_subtitle(
                args.video, args.srt, output, settings, logo_path=args.logo,
                annotation_srt=args.annotation_srt,
            )
        LOGGER.info("Đã tạo %s", output.resolve())
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Đã dừng; file xuất trước đó được giữ lại.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
