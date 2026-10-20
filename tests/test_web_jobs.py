from pathlib import Path
import tempfile
import time
import unittest

from web_jobs import JobManager


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='worker test ')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.jobs=JobManager(self.root)
        self.addCleanup(self.jobs.cancel)

    def wait_done(self):
        deadline=time.monotonic()+10
        while self.jobs.busy() and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertFalse(self.jobs.busy())

    def test_real_subprocess_redacts_key_and_tracks_completion(self):
        (self.root/'web_worker.py').write_text(
            "import json,sys\nr=json.load(sys.stdin)\nprint(r['api_key'],flush=True)\nprint('TTS [1/2] test',flush=True)\n",encoding='utf-8')
        self.jobs.start({'project':'test','action':'tts','api_key':'test-secret-never-real'})
        self.wait_done()
        data=self.jobs.snapshot()
        self.assertEqual(data['job']['status'],'completed')
        self.assertEqual(data['job']['progress'],50)
        self.assertNotIn('test-secret-never-real',str(data))
        self.assertIn('[API key]',str(data))

    def test_cancel_stops_worker_and_unlocks_mutations(self):
        (self.root/'web_worker.py').write_text(
            "import json,sys,time\njson.load(sys.stdin)\nprint('ready',flush=True)\ntime.sleep(30)\n",encoding='utf-8')
        self.jobs.start({'project':'test','action':'tts'})
        with self.assertRaises(ValueError):self.jobs.require_idle()
        self.jobs.cancel()
        self.wait_done()
        self.assertEqual(self.jobs.snapshot()['job']['status'],'cancelled')
