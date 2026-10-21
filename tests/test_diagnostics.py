import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playlist_extractor import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_history_rotates_and_keeps_valid_json(self):
        for index in range(6):
            diagnostics.record(self.root, 'failure', detail='x' * 800000, index=index)
        logs = list((self.root / 'diagnostics').glob('batch.jsonl*'))
        self.assertGreater(len(logs), 1)
        self.assertLessEqual(len(logs), 4)
        for log in logs:
            self.assertLessEqual(log.stat().st_size, 2 * 1024 * 1024)
            for line in log.read_text().splitlines():
                self.assertEqual(json.loads(line)['event'], 'failure')
        current = self.root / 'diagnostics/batch.jsonl'
        self.assertEqual(json.loads(current.read_text().splitlines()[-1])['index'], 5)

    def test_snapshot_retains_last_step_error_health_and_process_wait(self):
        queue = self.root / 'queue.json'
        queue.write_text(json.dumps(dict(last_run=dict(current='video.mp4'), files={
            'video.mp4': dict(error='NAS read timeout', progress=dict(activity='Extracting audio', offset=2205))})))
        with patch.object(diagnostics.health, 'read', return_value={'updated_at': 123}), patch.object(diagnostics, 'process_info', return_value=[{'wchan': 'wait_woken'}]):
            row = diagnostics.snapshot(queue, 42)
        self.assertEqual(row['error'], 'NAS read timeout')
        self.assertEqual(row['progress']['offset'], 2205)
        self.assertEqual(row['health']['updated_at'], 123)
        self.assertEqual(row['processes'][0]['wchan'], 'wait_woken')

    def test_unwritable_history_does_not_stop_scanning(self):
        with patch.object(diagnostics.Path, 'mkdir', side_effect=OSError('disk full')):
            diagnostics.record(self.root, 'test')
