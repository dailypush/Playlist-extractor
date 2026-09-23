import json
from pathlib import Path
import tempfile
import unittest

from playlist_extractor import dashboard
from playlist_extractor.locking import output_lock


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.path = self.output / 'batches/test/queue.json'
        self.queue = dict(identity={'source': '/media'}, active_files=['a', 'b', 'c', 'd'],
            last_run=dict(status='running', requests=12, max_requests=500, started_at=100, updated_at=110),
            files={'a': {'status': 'complete'},
                   'b': {'status': 'running', 'progress': dict(phase='Baseline', done=3, total=10, songs=2, activity='Extracting audio', offset=135)},
                   'c': {'status': 'pending'},
                   'd': {'status': 'failed', 'error': 'decoder failed', 'failure': {'category': 'ffmpeg', 'stderr': 'Invalid data\x1b[31m'}},
                   'removed': {'status': 'pending'}})

    def test_live_budget_current_and_upcoming_exclude_removed_and_failed(self):
        lines, count = dashboard.render(self.queue, self.path, alive=True, now=120)
        text = '\n'.join(lines)
        self.assertIn('Requests 12/500', text)
        self.assertIn('1/4 complete | 1 skipped', text)
        self.assertIn('Extracting audio', text)
        self.assertIn('3/10', text)
        self.assertIn('[pending] c', text)
        self.assertNotIn('removed', text)
        self.assertEqual(count, 1)

    def test_dead_worker_and_failures_are_not_misrepresented(self):
        lines, _ = dashboard.render(self.queue, self.path, failures=True)
        text = '\n'.join(lines)
        self.assertIn('NOT RUNNING', text)
        self.assertIn('d [ffmpeg]', text)
        self.assertIn('Invalid data', text)
        self.assertNotIn('\x1b', text)
        self.assertIn('skipped.jsonl', text)
        self.assertFalse(dashboard.worker_alive(self.output, self.queue['last_run']))
        with output_lock(self.output):
            self.assertTrue(dashboard.worker_alive(self.output, self.queue['last_run']))
        self.assertFalse(dashboard.worker_alive(self.output, self.queue['last_run']))

    def test_legacy_queue_without_telemetry_still_renders(self):
        self.queue.pop('active_files')
        self.queue['last_run'] = {'status': 'paused'}
        self.queue['files']['b']['progress'] = None
        lines, _ = dashboard.render(self.queue, self.path)
        self.assertIn('No sample progress saved yet', lines)
        self.assertIn('unknown', '\n'.join(lines))

    def test_snapshot_is_read_only(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.queue))
        before = self.path.read_bytes()
        self.assertEqual(dashboard.main(['--output', str(self.output), '--once']), 0)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse((self.output / '.batch.lock').exists())
