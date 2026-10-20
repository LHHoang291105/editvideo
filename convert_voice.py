"""CLI facade for the same offline RVC + RMVPE voice-conversion engine as web."""
from __future__ import annotations

import argparse
from pathlib import Path

from common import setup_logging
from config import load_settings
from voice_conversion import VoiceConversionRequest, convert_voice, rvc_runtime_status


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description='Đổi WAV/MP3/MP4 thành giọng RVC nữ bằng RMVPE + CUDA (offline sau khi setup).',
    )
    result.add_argument('input', type=Path, help='File WAV, MP3 hoặc MP4 nguồn.')
    result.add_argument('--model', type=Path, required=True, help='Model RVC .pth có quyền sử dụng.')
    result.add_argument('--index', type=Path, help='Index RVC .index cùng model, nếu có.')
    result.add_argument('--preset', choices=('cute', 'bright', 'soft'), default='cute')
    result.add_argument('--output', type=Path, required=True, help='WAV đầu ra, hoặc MP4 khi input là MP4.')
    result.add_argument('--audio-only', action='store_true', help='Với MP4, chỉ tạo WAV ngay cả khi không mux video.')
    result.add_argument('--speaker-id', type=int)
    result.add_argument('--pitch', type=int, help='F0 shift trong RVC, -12 đến 12 bán âm.')
    result.add_argument('--post-pitch', type=float, default=0, help='F0 hậu kỳ -2 đến 2; cần FFmpeg có Rubber Band.')
    result.add_argument('--env', type=Path, help='File .env tùy chọn.')
    result.add_argument('--force', action='store_true', help='Cho phép thay output đã có sau khi job thành công.')
    result.add_argument('--verbose', action='store_true')
    return result


def main() -> int:
    # argparse writes --help before returning; configure UTF-8 first for the
    # default Windows PowerShell code page as the other project CLIs do.
    setup_logging()
    args = parser().parse_args()
    setup_logging(args.verbose)
    settings = load_settings(args.env)
    status = rvc_runtime_status(settings)
    if not status['ready']:
        parser().error(' '.join(str(item) for item in status['errors']))
    source = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.suffix.lower() not in ('.wav', '.mp4'):
        parser().error('--output phải có đuôi .wav hoặc .mp4.')
    if source == output:
        parser().error('Output không được trùng input.')
    if args.audio_only and output.suffix.lower() == '.mp4':
        parser().error('--audio-only cần output .wav.')
    if source.suffix.lower() != '.mp4' and output.suffix.lower() == '.mp4':
        parser().error('Chỉ input MP4 mới xuất MP4.')
    audio_output = output.with_suffix('.wav') if output.suffix.lower() == '.mp4' else output
    video_output = output if output.suffix.lower() == '.mp4' and not args.audio_only else None
    if not args.force:
        existing = [path for path in (audio_output, video_output) if path and path.exists()]
        if existing:
            parser().error(f'Output đã tồn tại: {existing[0]}. Thêm --force để thay sau khi xử lý xong.')
    request = VoiceConversionRequest(
        source=source, model=args.model, index=args.index, preset=args.preset,
        speaker_id=args.speaker_id, pitch_semitones=args.pitch,
        post_pitch_semitones=args.post_pitch,
    )
    result = convert_voice(
        request, audio_output=audio_output, video_output=video_output,
        work_dir=settings.temp_dir / 'voice_conversion_cli', settings=settings,
    )
    print(f'WAV: {result.audio}')
    if result.video:
        print(f'MP4: {result.video}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
