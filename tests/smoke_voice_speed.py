"""Real FFmpeg integration: change voice speed, not video/original timing."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Subtitle, setup_logging, write_srt
from config import load_settings
from media import probe_media
from mix_audio import build_timeline, mix_audio
from tests.smoke_media import FFmpegToneTestProvider, make_video, assert_audio_at, video_frame
from tts_from_srt import tts_from_srt, sha256_file


def main():
    import numpy as np
    setup_logging()
    base = load_settings()
    root = Path(tempfile.mkdtemp(prefix='speed_smoke_', dir=base.temp_dir))
    settings = replace(base, audio_dir=root/'audio', temp_dir=root/'temp', output_dir=root,
                       voice_speed=1.1, tts_request_interval=0)
    source=root/'original.mp4'; speech=root/'speech.srt'; display=root/'display.srt'
    make_video(source,settings,has_audio=True)
    write_srt(speech,[Subtitle(7,12.2,14.3,'Sư phụ, đệ tử đã trở về.')])
    write_srt(display,[Subtitle(70,5,6,'Chữ hiển thị độc lập')])
    provider=FFmpegToneTestProvider()
    asyncio.run(tts_from_srt(speech,settings,provider=provider))
    original_hash=sha256_file(settings.audio_dir/'007.wav')
    measured={}

    def capture(cues,infos,current,duration,destination):
        result=build_timeline(cues,infos,current,duration,destination)
        samples=np.fromfile(destination,dtype='<f4')
        start=round(12.2*settings.audio_sample_rate)
        assert np.all(samples[:start]==0)
        assert np.any(samples[start:start+1000]!=0)
        assert 1.85<infos[7].duration<1.98
        measured['voice_duration']=infos[7].duration
        measured['start_sample']=start
        measured['timeline_samples']=result
        return result

    with patch('mix_audio.build_timeline',side_effect=capture):
        final=mix_audio(source,speech,settings,burn=True,display_srt=display,provider=provider)
    assert sha256_file(settings.audio_dir/'007.wav')==original_hash
    assert abs(probe_media(final,settings).duration-16)<.01
    assert assert_audio_at(final,1,2,settings)>.001
    assert video_frame(source,5.5,settings)!=video_frame(final,5.5,settings)
    report={'passed':True,'video_duration':16,'voice_speed':1.1,'original_wav_unchanged':True,
            'original_audio_present':True,'separate_display_burn':True,**measured}
    (root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report));print('Report:',root/'report.json')


if __name__=='__main__':
    main()
