"""ProjectStore contracts for the reviewed slow-source stage."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from config import Settings
from web_store import ProjectStore


class SlowSourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='slow store Việt 中文 ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / 'nguồn 中文.mp4'
        self.video.write_bytes(b'original video bytes')
        self.store = ProjectStore(self.root)
        self.project_id = self.store.add(self.video)['id']
        self.paths = self.store.paths(self.project_id)

    def create_review_copy(self, speed: float = 0.8) -> Path:
        slow = self.store.slow_render_path(self.project_id)
        slow.write_bytes(f'slow at {speed}'.encode('utf-8'))
        self.store.mark_slow_created(self.project_id, speed)
        return slow

    def write_timeline_artifacts(self) -> dict[str, bytes]:
        values = {
            'zh': b'zh timing', 'vi': b'vi manual timing', 'speech': b'speech timing',
            'final': b'final render', 'overlap': b'old overlap timing',
        }
        self.paths['zh'].write_bytes(values['zh'])
        self.paths['vi'].write_bytes(values['vi'])
        self.paths['speech'].write_bytes(values['speech'])
        self.paths['final'].write_bytes(values['final'])
        (self.store.settings(self.project_id).audio_dir / 'overlap_warnings.json').write_bytes(values['overlap'])
        # Audio cache remains safe because its own manifest checks text+voice.
        (self.store.settings(self.project_id).audio_dir / '001.wav').write_bytes(b'wav cache')
        (self.store.settings(self.project_id).audio_dir / 'manifest.json').write_bytes(b'{}')
        return values

    def test_review_gate_keeps_original_immutable_until_approved(self) -> None:
        original = self.video.read_bytes()
        slow = self.create_review_copy(.8)

        public = self.store.public(self.project_id)
        self.assertEqual(public['source_state'], 'review_required')
        self.assertEqual(public['slow_speed'], .8)
        self.assertTrue(public['slow_ready'])
        self.assertEqual(self.store.paths(self.project_id)['original'], self.video.resolve())
        self.assertEqual(self.store.paths(self.project_id)['slow'], slow)
        self.assertEqual(self.store.paths(self.project_id)['source'], self.video.resolve())
        self.assertEqual(self.video.read_bytes(), original)

        self.store.activate_slow_source(self.project_id)
        self.assertEqual(self.store.public(self.project_id)['source_state'], 'approved')
        self.assertEqual(self.store.paths(self.project_id)['source'], slow)
        self.assertEqual(self.video.read_bytes(), original)
        with self.assertRaises(ValueError):
            self.store.slow_render_path(self.project_id)

    def test_approval_archives_time_dependent_artifacts_but_keeps_audio_cache(self) -> None:
        self.write_timeline_artifacts()
        self.create_review_copy()

        result = self.store.activate_slow_source(self.project_id)
        self.assertEqual(result['source_state'], 'approved')
        for key in ('zh', 'vi', 'speech', 'final'):
            self.assertFalse(self.paths[key].exists(), key)
        audio = self.store.settings(self.project_id).audio_dir
        self.assertFalse((audio / 'overlap_warnings.json').exists())
        self.assertTrue((audio / '001.wav').exists())
        self.assertTrue((audio / 'manifest.json').exists())
        backups = list(self.store.settings(self.project_id).output_dir.glob('*.source-changed.*.bak'))
        self.assertEqual(len(backups), 4)
        backed_bytes = {path.read_bytes() for path in backups}
        self.assertTrue({b'zh timing', b'vi manual timing', b'speech timing', b'final render'} <= backed_bytes)

    def test_rejecting_a_review_copy_preserves_original_timeline_artifacts(self) -> None:
        values = self.write_timeline_artifacts()
        slow = self.create_review_copy()

        result = self.store.activate_original_source(self.project_id)
        self.assertEqual(result['source_state'], 'original')
        self.assertEqual(self.store.paths(self.project_id)['source'], self.video.resolve())
        self.assertFalse(slow.exists())
        self.assertEqual(self.paths['vi'].read_bytes(), values['vi'])
        self.assertEqual(self.paths['final'].read_bytes(), values['final'])
        self.assertTrue(list(self.store.settings(self.project_id).output_dir.glob('*.slow-source.*.bak')))

    def test_switching_back_from_approved_archives_both_old_timeline_and_slow_video(self) -> None:
        self.create_review_copy()
        self.store.activate_slow_source(self.project_id)
        values = self.write_timeline_artifacts()

        self.store.activate_original_source(self.project_id)
        self.assertEqual(self.store.public(self.project_id)['source_state'], 'original')
        self.assertEqual(self.store.paths(self.project_id)['source'], self.video.resolve())
        self.assertFalse(self.paths['slow'].exists())
        self.assertFalse(self.paths['vi'].exists())
        backups = list(self.store.settings(self.project_id).output_dir.glob('*.bak'))
        self.assertIn(values['vi'], {path.read_bytes() for path in backups})
        self.assertIn(b'slow at 0.8', {path.read_bytes() for path in backups})

    def test_changed_source_invalidates_pending_review_and_never_selects_slow_file(self) -> None:
        slow = self.create_review_copy()
        self.video.write_bytes(b'changed original video bytes')
        # Some Windows filesystems have coarse timestamp granularity; make the
        # mutation explicit while retaining the same test path.
        stat = self.video.stat()
        os.utime(self.video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

        public = self.store.public(self.project_id)
        self.assertEqual(public['source_state'], 'original')
        self.assertIsNone(public['slow_speed'])
        self.assertIn('đã thay đổi', public['source_error'])
        self.assertEqual(self.store.paths(self.project_id)['source'], self.video.resolve())
        with self.assertRaises(ValueError):
            self.store.activate_slow_source(self.project_id)
        self.assertTrue(slow.exists())

    def test_reload_observes_metadata_persisted_by_a_separate_worker_store(self) -> None:
        worker = ProjectStore(self.root)
        slow = worker.slow_render_path(self.project_id)
        slow.write_bytes(b'slow worker output')
        worker.mark_slow_created(self.project_id, .75)

        # This store is deliberately stale until the web app calls reload().
        self.assertEqual(self.store.public(self.project_id)['source_state'], 'original')
        self.store.reload()
        self.assertEqual(self.store.public(self.project_id)['source_state'], 'review_required')
        self.assertEqual(self.store.public(self.project_id)['slow_speed'], .75)

    def test_source_speed_validation_is_shared_with_settings(self) -> None:
        self.store.update_options(self.project_id, {'source_speed': .25})
        self.assertEqual(self.store.settings(self.project_id).source_speed, .25)
        with self.assertRaises(ValueError):
            self.store.update_options(self.project_id, {'source_speed': .24})
        with self.assertRaises(ValueError):
            Settings(source_speed=1.01)

    def test_source_transition_never_archives_any_registered_input_video(self) -> None:
        # A user can later connect a generated file as another project's input.
        # Source transitions must preserve it just like cleanup does.
        final = self.paths['final']
        final.write_bytes(b'also a registered input')
        self.store.get(self.project_id)['video'] = str(final)
        slow = self.store.slow_render_path(self.project_id)
        slow.write_bytes(b'slow preview')
        self.store.mark_slow_created(self.project_id, .8)

        self.store.activate_slow_source(self.project_id)
        self.assertTrue(final.exists())
        self.assertEqual(final.read_bytes(), b'also a registered input')


if __name__ == '__main__':
    unittest.main()
