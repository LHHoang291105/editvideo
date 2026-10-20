"""Contracts for the offline RVC/RMVPE voice-conversion facade."""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from config import Settings
from voice_conversion import (
    MediaDescription,
    VoiceConversionRequest,
    _mux_voice_to_video,
    _postprocess_filters,
    _run_rvc,
    convert_voice,
    rvc_command,
    rvc_runtime_status,
    verify_rvc_cuda,
)


class VoiceConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='voice RVC Việt ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'rvc'
        (self.runtime / 'infer').mkdir(parents=True)
        (self.runtime / 'infer' / 'cli.py').write_text('print("fake")', encoding='utf-8')
        (self.runtime / 'assets' / 'hubert_base').mkdir(parents=True)
        (self.runtime / 'assets' / 'hubert_base' / 'pytorch_model.bin').write_bytes(b'hubert')
        (self.runtime / 'assets' / 'rmvpe').mkdir(parents=True)
        (self.runtime / 'assets' / 'rmvpe' / 'rmvpe.pt').write_bytes(b'rmvpe')
        self.models = self.root / 'models'; self.models.mkdir()
        self.model = self.models / 'cute.pth'; self.model.write_bytes(b'model')
        self.index = self.models / 'cute.index'; self.index.write_bytes(b'index')
        self.source = self.root / 'nguon.mp4'; self.source.write_bytes(b'input')
        self.settings = Settings(
            project_dir=self.root, rvc_root=self.runtime, rvc_python=Path(sys.executable),
            voice_models_dir=self.models, ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe',
        )

    def request(self, **changes: object) -> VoiceConversionRequest:
        values: dict[str, object] = {'source': self.source, 'model': self.model, 'index': self.index}
        values.update(changes)
        return VoiceConversionRequest(**values)  # type: ignore[arg-type]

    def test_current_rvc_cli_uses_rmvpe_and_rvc_pitch_not_crude_resampling(self) -> None:
        command = rvc_command(self.request(preset='cute'), self.root / 'pre.wav', self.root / 'rvc.wav', self.settings)
        self.assertEqual(command[0], sys.executable)
        self.assertIn('--f0-method', command)
        self.assertEqual(command[command.index('--f0-method') + 1], 'rmvpe')
        self.assertEqual(command[command.index('--pitch') + 1], '2')
        self.assertEqual(command[command.index('--index') + 1], str(self.index))
        self.assertNotIn('asetrate', ' '.join(command))
        no_index = rvc_command(self.request(index=None), self.root / 'pre.wav', self.root / 'rvc.wav', self.settings)
        self.assertEqual(no_index[no_index.index('--index-rate') + 1], '0.000')

    def test_rvc_child_receives_checkout_on_pythonpath(self) -> None:
        """Running infer/cli.py as a script still needs top-level infer imports."""
        class FinishedProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO('RVC started\n')

            def wait(self) -> int:
                return 0

            def kill(self) -> None:
                raise AssertionError('A successful process must not be killed.')

        with patch('voice_conversion.find_binary', return_value=str(self.root / 'ffmpeg.exe')), \
             patch('voice_conversion.subprocess.Popen', return_value=FinishedProcess()) as popen:
            _run_rvc(['python', 'infer/cli.py'], root=self.runtime, device=0, settings=self.settings)
        environment = popen.call_args.kwargs['env']
        self.assertEqual(environment['CUDA_VISIBLE_DEVICES'], '0')
        self.assertEqual(environment['PYTHONPATH'].split(os.pathsep)[0], str(self.runtime))

    def test_rvc_runtime_status_requires_isolated_python_cli_and_models(self) -> None:
        self.assertTrue(rvc_runtime_status(self.settings)['ready'])
        missing = Settings(project_dir=self.root, rvc_root=self.root / 'missing', rvc_python=self.root / 'missing.exe')
        status = rvc_runtime_status(missing)
        self.assertFalse(status['ready'])
        self.assertTrue(status['errors'])

    def test_cuda_verification_rejects_silent_cpu_fallback_and_reports_selected_gpu(self) -> None:
        cuda = subprocess.CompletedProcess(['python'], 0, stdout='{"available": true, "count": 1}\n', stderr='')
        rvc_cuda = subprocess.CompletedProcess(
            ['python'], 0, stdout='{"device": "cuda:0", "name": "NVIDIA Test GPU"}\n', stderr='',
        )
        with patch('voice_conversion.rvc_runtime_status', return_value={'ready': True, 'errors': []}), \
             patch('voice_conversion.subprocess.run', side_effect=[cuda, rvc_cuda]):
            self.assertEqual(verify_rvc_cuda(self.settings), 'NVIDIA Test GPU')
        cpu = subprocess.CompletedProcess(['python'], 2, stdout='{"available": false, "count": 0}\n', stderr='')
        with patch('voice_conversion.rvc_runtime_status', return_value={'ready': True, 'errors': []}), \
             patch('voice_conversion.subprocess.run', return_value=cpu):
            with self.assertRaisesRegex(RuntimeError, 'không thấy CUDA'):
                verify_rvc_cuda(self.settings)
        rvc_cpu = subprocess.CompletedProcess(['python'], 3, stdout='{"device": "cpu", "name": ""}\n', stderr='low VRAM')
        with patch('voice_conversion.rvc_runtime_status', return_value={'ready': True, 'errors': []}), \
             patch('voice_conversion.subprocess.run', side_effect=[cuda, rvc_cpu]):
            with self.assertRaisesRegex(RuntimeError, 'fallback về CPU'):
                verify_rvc_cuda(self.settings)

    def test_formant_preserving_rubberband_is_opt_in(self) -> None:
        ordinary = _postprocess_filters(self.request(post_pitch_semitones=0), self.settings)
        self.assertNotIn('rubberband=', ordinary)
        with patch('voice_conversion._ffmpeg_supports_rubberband', return_value=True):
            advanced = _postprocess_filters(self.request(post_pitch_semitones=1), self.settings)
        self.assertIn('rubberband=', advanced)
        self.assertIn('formant=preserved', advanced)

    def test_mux_keeps_video_stream_and_pads_voice_to_video_duration(self) -> None:
        source = self.root / 'source.mp4'; source.write_bytes(b'video')
        voice = self.root / 'voice.wav'; voice.write_bytes(b'wav')
        output = self.root / 'voice.mp4'

        def fake_ffmpeg(args: list[str], settings: Settings) -> None:
            Path(args[-1]).write_bytes(b'published video')
            self.command = args

        with patch('voice_conversion.run_ffmpeg', side_effect=fake_ffmpeg):
            _mux_voice_to_video(source, voice, output, 12.2, self.settings)
        self.assertEqual(output.read_bytes(), b'published video')
        self.assertIn('-c:v', self.command)
        self.assertEqual(self.command[self.command.index('-c:v') + 1], 'copy')
        graph = self.command[self.command.index('-filter_complex') + 1]
        self.assertIn('apad=pad_dur=12.200000', graph)
        self.assertIn('atrim=duration=12.200000', graph)

    def test_failure_preserves_old_published_audio_and_cleans_intermediate(self) -> None:
        audio = self.root / 'output.wav'; audio.write_bytes(b'old valid audio')
        work = self.root / 'temp'
        result_info = MediaDescription(duration=1.0, has_audio=True, has_video=False)
        with patch('voice_conversion.verify_rvc_cuda', return_value='Test GPU'), \
             patch('voice_conversion.probe_voice_media', return_value=result_info), \
             patch('voice_conversion._preprocess_audio') as preprocess, \
             patch('voice_conversion._run_rvc', side_effect=RuntimeError('RVC fail')):
            def make_source(_source: Path, output: Path, _settings: Settings) -> Path:
                output.write_bytes(b'prepared')
                return output
            preprocess.side_effect = make_source
            with self.assertRaisesRegex(RuntimeError, 'RVC fail'):
                convert_voice(self.request(), audio_output=audio, video_output=None, work_dir=work, settings=self.settings)
        self.assertEqual(audio.read_bytes(), b'old valid audio')
        self.assertEqual(list(work.glob('*.wav')), [])

    def test_mux_failure_keeps_old_published_wav_until_every_stage_succeeds(self) -> None:
        """An MP4 failure must not replace the valid WAV from an older run."""
        audio = self.root / 'output.wav'; audio.write_bytes(b'old valid audio')
        video = self.root / 'output.mp4'; video.write_bytes(b'old valid video')
        work = self.root / 'temp'
        info = MediaDescription(duration=1.0, has_audio=True, has_video=True)

        def make_file(_input: Path, output: Path, *_args: object) -> Path:
            output.write_bytes(b'staged wav')
            return output

        def run_rvc(command: list[str], **_kwargs: object) -> None:
            Path(command[command.index('--output') + 1]).write_bytes(b'rvc wav')

        with patch('voice_conversion.verify_rvc_cuda', return_value='Test GPU'), \
             patch('voice_conversion.probe_voice_media', side_effect=[info, info]), \
             patch('voice_conversion._preprocess_audio', side_effect=make_file), \
             patch('voice_conversion._run_rvc', side_effect=run_rvc), \
             patch('voice_conversion._postprocess_audio', side_effect=make_file), \
             patch('voice_conversion._mux_voice_to_video', side_effect=RuntimeError('mux failed')):
            with self.assertRaisesRegex(RuntimeError, 'mux failed'):
                convert_voice(self.request(), audio_output=audio, video_output=video, work_dir=work, settings=self.settings)

        self.assertEqual(audio.read_bytes(), b'old valid audio')
        self.assertEqual(video.read_bytes(), b'old valid video')
        self.assertEqual(list(work.glob('*.wav')), [])
        self.assertEqual(list(work.glob('*.mp4')), [])


if __name__ == '__main__':
    unittest.main()
