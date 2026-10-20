"""Real FFmpeg regression test for mirror + lower subtitle blur rendering."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import burn_subtitle as burn_cli
from burn_subtitle import build_video_filter, render_video
from common import Subtitle, write_srt
from config import Settings
from media import MediaInfo


PROJECT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 320, 180


def build_effect_graph(
    subtitle_filter: str,
    *,
    mirror: bool = True,
    blur_left: float = 0.05,
    blur_top: float = 0.65,
    blur_right: float = 0.95,
    blur_bottom: float = 0.98,
    blur_radius: int = 12,
) -> str:
    """Build the actual production graph with a deterministic test ROI."""
    settings = replace(
        Settings(), blur_chinese_subtitles=True, mirror_video=mirror,
        blur_left=blur_left, blur_top=blur_top, blur_right=blur_right,
        blur_bottom=blur_bottom, blur_strength=blur_radius,
    )
    return build_video_filter(settings, subtitle_filter)


def _ffmpeg_path() -> str | None:
    configured = os.environ.get("FFMPEG_BIN")
    candidates = [
        configured,
        str(PROJECT / ".tools" / "ffmpeg" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffmpeg.exe"),
        shutil.which("ffmpeg"),
    ]
    return next((str(Path(item)) for item in candidates if item and Path(item).is_file()), None)


def _run(ffmpeg: str, args: list[str], *, cwd: Path) -> None:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-y", *args], cwd=cwd,
        capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))


def _frame(ffmpeg: str, path: Path, seconds: float, *, cwd: Path) -> bytes:
    result = subprocess.run(
        [
            ffmpeg, "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], cwd=cwd, capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    expected_size = WIDTH * HEIGHT * 3
    if len(result.stdout) != expected_size:
        raise AssertionError(f"Expected {expected_size} RGB bytes, got {len(result.stdout)}.")
    return result.stdout


def _pixel(frame: bytes, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * WIDTH + x) * 3
    return tuple(frame[offset:offset + 3])  # type: ignore[return-value]


def _white_mask(frame: bytes) -> bytes:
    """Threshold only the lower text area, where the source is solid black."""
    mask = bytearray()
    for y in range(130, HEIGHT):
        start = (y * WIDTH) * 3
        for x in range(WIDTH):
            red, green, blue = frame[start + x * 3:start + x * 3 + 3]
            mask.append(1 if min(red, green, blue) >= 160 else 0)
    return bytes(mask)


def _mask_distance(first: bytes, second: bytes) -> int:
    return sum(left != right for left, right in zip(first, second, strict=True))


def _high_frequency_energy(frame: bytes) -> int:
    """Sum horizontal differences over the patterned hard-subtitle test band."""
    total = 0
    for y in range(116, 126):
        for x in range(1, WIDTH):
            total += abs(frame[(y * WIDTH + x) * 3] - frame[(y * WIDTH + x - 1) * 3])
    return total


def _write_fixture_ppm(path: Path) -> None:
    """An asymmetric top marker plus a sharp lower band resembling hard text."""
    pixels = bytearray(WIDTH * HEIGHT * 3)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            red = green = blue = 0
            if 20 <= x < 80 and 20 <= y < 80:
                red = 255
            elif 240 <= x < 300 and 20 <= y < 80:
                blue = 255
            elif 104 <= y < 128:
                value = 255 if (x // 2 + y // 2) % 2 else 0
                red = green = blue = value
            offset = (y * WIDTH + x) * 3
            pixels[offset:offset + 3] = bytes((red, green, blue))
    path.write_bytes(f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + pixels)


def _write_asymmetric_logo_ppm(path: Path) -> None:
    """A red-left/green-right logo exposes accidental logo mirroring."""
    width, height = 60, 30
    pixels = bytearray()
    for _y in range(height):
        for x in range(width):
            pixels.extend((255, 0, 0) if x < width // 2 else (0, 255, 0))
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)


def _write_black_ppm(path: Path) -> None:
    """A uniform background makes an in-frame ASS label easy to locate."""
    path.write_bytes(f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + bytes(WIDTH * HEIGHT * 3))


def _bright_pixels_in_box(frame: bytes, *, left: int, top: int, right: int, bottom: int) -> int:
    """Count near-white subtitle pixels in a given source-frame rectangle."""
    count = 0
    for y in range(top, bottom):
        for x in range(left, right):
            red, green, blue = _pixel(frame, x, y)
            if min(red, green, blue) >= 160:
                count += 1
    return count


class FilterGraphConstructionTests(unittest.TestCase):
    def test_annotation_only_cli_dispatches_without_dialogue_srt(self) -> None:
        """The documented direct-GPT command must not need a dialogue SRT."""
        settings = Settings()
        with patch.object(sys, 'argv', [
            'burn_subtitle.py', 'source.mp4', '--annotation-srt', 'notes_vi.srt',
            '--output', 'annotated.mp4',
        ]), patch('burn_subtitle.load_settings', return_value=settings), patch(
            'burn_subtitle.render_video'
        ) as render:
            self.assertEqual(burn_cli.main(), 0)

        self.assertEqual(render.call_args.args[:3], (Path('source.mp4'), None, Path('annotated.mp4')))
        self.assertFalse(render.call_args.kwargs['burn_subtitles'])
        self.assertEqual(render.call_args.kwargs['annotation_srt'], Path('notes_vi.srt'))

    def test_graph_keeps_subtitle_filter_last(self) -> None:
        graph = build_effect_graph("subtitles=filename=subtitles.srt")
        self.assertLess(graph.index("boxblur"), graph.index("hflip"))
        self.assertLess(graph.index("hflip"), graph.index("subtitles="))
        self.assertTrue(graph.endswith("[video]"))

    def test_graph_places_logo_after_mirror_and_before_subtitles(self) -> None:
        settings = replace(
            Settings(), blur_chinese_subtitles=True, mirror_video=True, burn_logo=True,
            logo_position="top-right", logo_width_percent=12, logo_opacity=.7, logo_margin_percent=3,
        )
        graph = build_video_filter(settings, "subtitles=filename=subtitles.srt", logo_overlay=True)
        self.assertLess(graph.index("boxblur"), graph.index("hflip"))
        self.assertLess(graph.index("hflip"), graph.index("scale2ref"))
        self.assertLess(graph.index("scale2ref"), graph.index("[logo_base][logo]overlay"))
        self.assertLess(graph.index("[logo_base][logo]overlay"), graph.index("subtitles="))
        self.assertIn("[1:v:0]", graph)
        self.assertIn("colorchannelmixer=aa=0.7", graph)

    def test_graph_places_annotation_between_source_effects_and_dialogue(self) -> None:
        settings = replace(Settings(), blur_chinese_subtitles=True, mirror_video=True)
        graph = build_video_filter(
            settings,
            "subtitles=filename=dialogue.srt",
            annotation_filter="subtitles=filename=annotations.ass:charenc=UTF-8",
        )
        annotation_at = graph.index("annotations.ass")
        dialogue_at = graph.index("dialogue.srt")
        self.assertLess(graph.index("boxblur"), graph.index("hflip"))
        self.assertLess(graph.index("hflip"), annotation_at)
        self.assertLess(annotation_at, dialogue_at)
        self.assertTrue(graph.endswith("[video]"))

    def test_mirrored_annotation_region_tracks_the_flipped_source_pixels(self) -> None:
        """ASS is added after hflip, so only its placement must be mirrored."""
        with tempfile.TemporaryDirectory(prefix="annotation_mirror_") as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            annotation = root / "annotation.srt"
            output = root / "output.mp4"
            source.write_bytes(b"fixture")
            write_srt(annotation, [Subtitle(1, 0, 1, "Tên nhân vật")])
            settings = replace(
                Settings(), project_dir=root, temp_dir=root / "temp", mirror_video=True,
                annotation_left=.1, annotation_top=.2, annotation_right=.3, annotation_bottom=.4,
            )
            seen = {}

            def write_ass(source_srt, destination, region, *, font_size, display_y):
                seen["region"] = region
                seen["display_y"] = display_y
                destination.write_text("[Script Info]\n", encoding="utf-8")
                return [Subtitle(1, 0, 1, "Tên nhân vật")]

            def encode(args, _settings, *, cwd=None):
                Path(args[-1]).write_bytes(b"encoded")

            with patch("burn_subtitle.probe_media", return_value=MediaInfo(1, False)), patch(
                "burn_subtitle.check_required_filters"
            ), patch("burn_subtitle.write_annotation_ass", side_effect=write_ass), patch(
                "burn_subtitle.run_ffmpeg", side_effect=encode
            ):
                render_video(source, None, output, settings, burn_subtitles=False,
                             annotation_srt=annotation)

            self.assertEqual(seen["region"].left, .7)
            self.assertEqual(seen["region"].top, .2)
            self.assertEqual(seen["region"].right, .9)
            self.assertEqual(seen["region"].bottom, .4)
            self.assertEqual(seen["display_y"], .12)


class VideoEffectFilterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = _ffmpeg_path()
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg is not installed; skip real filter graph check.")
        filters = subprocess.run(
            [cls.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, check=False,
        )
        if filters.returncode or " subtitles " not in filters.stdout:
            raise unittest.SkipTest("FFmpeg has no subtitles/libass filter; skip real filter graph check.")

    def test_graph_mirrors_and_blurs_hard_text_before_readable_vietnamese_burn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video_effects_") as temporary:
            root = Path(temporary)
            fixture = root / "fixture.ppm"
            source = root / "source.mp4"
            correct = root / "correct.mp4"
            reference = root / "reference.mp4"
            wrong_order = root / "wrong_order.mp4"
            srt = root / "subtitles.srt"
            _write_fixture_ppm(fixture)
            srt.write_text("1\n00:00:00,000 --> 00:00:00,900\nF\n", encoding="utf-8")
            _run(self.ffmpeg, [
                "-loop", "1", "-framerate", "25", "-i", str(fixture), "-t", "1", "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p",
                str(source),
            ], cwd=root)
            subtitle_filter = (
                "subtitles=filename=subtitles.srt:charenc=UTF-8:force_style="
                "'FontName=Arial,FontSize=24,Outline=2,Shadow=0,Alignment=2,MarginV=20'"
            )
            ffprobe = Path(self.ffmpeg).with_name("ffprobe.exe" if self.ffmpeg.endswith(".exe") else "ffprobe")
            settings = replace(
                Settings(), project_dir=root, temp_dir=root / "temp", ffmpeg_bin=self.ffmpeg,
                ffprobe_bin=str(ffprobe), blur_chinese_subtitles=True, mirror_video=True,
                blur_left=.05, blur_top=.65, blur_right=.95, blur_bottom=.98, blur_strength=12,
            )
            render_video(source, srt, correct, settings, burn_subtitles=True)
            _run(self.ffmpeg, [
                "-i", str(source), "-vf", subtitle_filter, "-an", "-c:v", "libx264", "-preset",
                "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p", str(reference),
            ], cwd=root)
            # Deliberately incorrect order: this is the regression comparison.  It mirrors the F glyph.
            _run(self.ffmpeg, [
                "-i", str(source), "-filter_complex", f"[0:v]{subtitle_filter},hflip[v]", "-map", "[v]",
                "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p",
                str(wrong_order),
            ], cwd=root)

            without_subtitle = _frame(self.ffmpeg, correct, 0.96, cwd=root)
            with_subtitle = _frame(self.ffmpeg, correct, 0.40, cwd=root)
            reference_subtitle = _frame(self.ffmpeg, reference, 0.40, cwd=root)
            wrong_subtitle = _frame(self.ffmpeg, wrong_order, 0.40, cwd=root)
            source_frame = _frame(self.ffmpeg, source, 0.96, cwd=root)

            # Red marker started left and blue marker started right, so hflip swapped them.
            left = _pixel(without_subtitle, 50, 50)
            right = _pixel(without_subtitle, 270, 50)
            self.assertGreater(left[2], left[0] * 2, left)
            self.assertGreater(right[0], right[2] * 2, right)
            # The sharp lower pattern lost substantial high-frequency detail after boxblur.
            self.assertLess(_high_frequency_energy(without_subtitle), _high_frequency_energy(source_frame) // 4)

            # Text is present only while its SRT cue is active.
            self.assertGreater(sum(_white_mask(with_subtitle)), 10)
            self.assertEqual(sum(_white_mask(without_subtitle)), 0)
            # The final subtitle shape matches a normal burn much more closely than a mirrored burn.
            normal_distance = _mask_distance(_white_mask(with_subtitle), _white_mask(reference_subtitle))
            mirrored_distance = _mask_distance(_white_mask(wrong_subtitle), _white_mask(reference_subtitle))
            self.assertLess(normal_distance, mirrored_distance // 4)

    def test_logo_is_at_selected_corner_and_is_not_mirrored_with_video(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video_logo_") as temporary:
            root = Path(temporary)
            fixture = root / "fixture.ppm"
            logo = root / "logo.ppm"
            source = root / "source.mp4"
            output = root / "result.mp4"
            _write_fixture_ppm(fixture)
            _write_asymmetric_logo_ppm(logo)
            _run(self.ffmpeg, [
                "-loop", "1", "-framerate", "25", "-i", str(fixture), "-t", "1", "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p",
                str(source),
            ], cwd=root)
            ffprobe = Path(self.ffmpeg).with_name("ffprobe.exe" if self.ffmpeg.endswith(".exe") else "ffprobe")
            settings = replace(
                Settings(), project_dir=root, temp_dir=root / "temp", ffmpeg_bin=self.ffmpeg,
                ffprobe_bin=str(ffprobe), mirror_video=True, burn_logo=True,
                logo_position="top-right", logo_width_percent=18, logo_opacity=1.0,
                logo_margin_percent=3,
            )
            render_video(source, None, output, settings, burn_subtitles=False, logo_path=logo)
            frame = _frame(self.ffmpeg, output, .4, cwd=root)

            # The original source is mirrored, while red-left/green-right logo
            # remains in its own orientation at the selected top-right corner.
            left_logo = _pixel(frame, 270, 12)
            right_logo = _pixel(frame, 306, 12)
            self.assertGreater(left_logo[0], left_logo[1] * 2, left_logo)
            self.assertGreater(right_logo[1], right_logo[0] * 2, right_logo)

    def test_annotation_ass_is_burned_in_selected_region_and_only_for_its_cue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video_annotation_") as temporary:
            root = Path(temporary)
            fixture = root / "black.ppm"
            source = root / "source.mp4"
            annotation = root / "annotation_vi.srt"
            output = root / "result.mp4"
            _write_black_ppm(fixture)
            _run(self.ffmpeg, [
                "-loop", "1", "-framerate", "25", "-i", str(fixture), "-t", "1", "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p",
                str(source),
            ], cwd=root)
            write_srt(annotation, [Subtitle(1, .1, .8, "A")])
            ffprobe = Path(self.ffmpeg).with_name("ffprobe.exe" if self.ffmpeg.endswith(".exe") else "ffprobe")
            settings = replace(
                Settings(), project_dir=root, temp_dir=root / "temp", ffmpeg_bin=self.ffmpeg,
                ffprobe_bin=str(ffprobe), annotation_left=.10, annotation_top=.10,
                annotation_right=.60, annotation_bottom=.50, annotation_font_size=52,
            )
            render_video(source, None, output, settings, burn_subtitles=False, annotation_srt=annotation)

            active = _frame(self.ffmpeg, output, .4, cwd=root)
            inactive = _frame(self.ffmpeg, output, .95, cwd=root)
            # OCR rectangle maps to x=32..192 and y=18..90. The label is
            # centered there; a padded interior avoids text outline edges.
            active_inside = _bright_pixels_in_box(active, left=38, top=22, right=186, bottom=86)
            inactive_inside = _bright_pixels_in_box(inactive, left=38, top=22, right=186, bottom=86)
            outside = _bright_pixels_in_box(active, left=205, top=22, right=315, bottom=86)
            # A single "A" is intentionally short, so font rasterization and
            # H.264 chroma subsampling leave only a small white-pixel count.
            self.assertGreater(active_inside, 5)
            self.assertEqual(inactive_inside, 0)
            self.assertEqual(outside, 0)

if __name__ == "__main__":
    unittest.main()
