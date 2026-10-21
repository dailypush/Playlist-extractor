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

    def test_process_snapshot_records_decoder_io_and_cpu(self):
        for pid, name, children in ((42, 'python', '43'), (43, 'ffmpeg', '')):
            folder = self.root / str(pid)
            (folder / 'task' / str(pid)).mkdir(parents=True)
            (folder / 'status').write_text(f'Name:\t{name}\nState:\tD (disk sleep)\nVmRSS:\t100 kB\n')
            (folder / 'wchan').write_text('folio_wait_bit_common')
            (folder / 'io').write_text('rchar: 25000000\nread_bytes: 13000000\n')
            (folder / 'stat').write_text(f'{pid} ({name} with spaces) ' + ' '.join(['D'] + ['0'] * 10 + ['273', '42']))
            (folder / 'task' / str(pid) / 'children').write_text(children)
        rows = diagnostics.process_info(42, proc=self.root)
        self.assertEqual([row['name'] for row in rows], ['python', 'ffmpeg'])
        self.assertEqual(rows[1]['cpu_ticks'], {'user': 273, 'system': 42})
        self.assertEqual(rows[1]['io']['rchar'], 25000000)
