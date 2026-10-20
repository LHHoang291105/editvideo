"""Opt-in live web integration: OCR -> edited SRT -> Edge TTS -> FFmpeg.

Uses only the short OCR test fixture and an isolated web project. No Gemini.
Run with the web server already listening on localhost:7860.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from config import PROJECT_DIR


def main():
    base='http://127.0.0.1:7860'
    html=urllib.request.urlopen(base,timeout=10).read().decode('utf-8')
    token=re.search(r'name="csrf-token" content="([^"]+)"',html)[1]

    def request(path,data=None):
        req=urllib.request.Request(base+path, data=json.dumps(data).encode() if data is not None else None,
                                  headers={'Content-Type':'application/json','X-CSRF-Token':token})
        with urllib.request.urlopen(req,timeout=30) as response:
            return json.load(response)

    source=PROJECT_DIR/'temp'/'ocr smoke 中文 tiếng Việt'/'Chinese 字幕 sample.mp4'
    project=request('/api/projects',{'path':str(source)})['project']
    prefix='/api/projects/'+project['id']
    request(prefix+'/options',{'voice_speed':1.1})

    def run(action):
        job=request(prefix+'/jobs',{'action':action,'burn':True})['job']
        deadline=time.monotonic()+240
        cursor=0
        while time.monotonic()<deadline:
            data=request('/api/job?after='+str(cursor));cursor=data['cursor']
            for line in data['logs']:print(line['text'],flush=True)
            if data['job']['id']!=job['id']:raise RuntimeError('Another job replaced smoke job.')
            if data['job']['status'] not in ('running','stopping'):
                assert data['job']['status']=='completed',data['job']
                return
            time.sleep(1)
        request('/api/job/cancel',{})
        raise TimeoutError(action)

    run('ocr')
    data=request(prefix+'/subtitles')
    assert len(data['rows'])==2
    for row,text in zip(data['rows'],['Xin chào.','Cùng tu luyện.']):row['text']=text
    request(prefix+'/subtitles',data)
    run('dub')
    files=request('/api/projects')['projects']
    project=next(p for p in files if p['id']==project['id'])
    assert all(project['files'][k]['exists'] for k in ('zh','vi','speech','final'))
    req=urllib.request.Request(base+prefix+'/files/final',headers={'Range':'bytes=0-63'})
    with urllib.request.urlopen(req,timeout=10) as response:
        assert response.status==206 and len(response.read())==64
    report={'passed':True,'project_id':project['id'],'source':str(source),'voice_speed':1.1,
            'actual_gpu_ocr':True,'actual_edge_tts':True,'actual_burn_and_mix':True,'http_range':True}
    path=PROJECT_DIR/'temp'/'web_pipeline_smoke.json'
    path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('WEB_PIPELINE_PASS',json.dumps(report),flush=True)


if __name__=='__main__':main()
