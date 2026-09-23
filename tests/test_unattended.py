import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from playlist_extractor import unattended, dashboard


class UnattendedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.source = self.output / 'media'

    def test_outcome_rejects_stale_completion_and_handles_errors(self):
        path = unattended.queue_path(self.source, self.output)
        path.parent.mkdir(parents=True)
        for status, code, expected in [('complete', 0, 'finished'), ('complete_with_errors', 1, 'finished'), ('paused', 0, 'break'), ('paused', 1, 'error')]:
            path.write_text(json.dumps({'last_run': {'started_at': 20, 'status': status}}))
            self.assertEqual(unattended.outcome(path, 19, code), expected)
            self.assertEqual(unattended.outcome(path, 21, code), 'error')

    def test_backoff_is_capped(self):
        self.assertEqual([unattended.retry_delay(n) for n in (1, 2, 3, 4, 100)], [3600, 7200, 14400, 21600, 21600])

    def test_break_then_completion_and_no_repeat_of_failed_media(self):
        stop = Mock()
        stop.is_set.return_value = False
        child = Mock(returncode=0)
        child.poll.return_value = 0
        with patch.object(unattended.subprocess, 'Popen', return_value=child) as launch, patch.object(unattended, 'outcome', side_effect=['break', 'finished']):
            self.assertEqual(unattended.run(self.source, self.output, stop), 0)
        stop.wait.assert_called_once_with(900)
        self.assertEqual(launch.call_count, 2)
        self.assertNotIn('--retry-failed', launch.call_args[0][0])
        self.assertEqual(json.loads((self.output / 'unattended.json').read_text())['status'], 'finished')

    def test_errors_back_off_and_manual_stop_prevents_restart(self):
        stop = threading.Event()
        child = Mock(returncode=1)
        child.poll.return_value = 1
        waits = []
        def wait(seconds):
            waits.append(seconds)
            stop.set()
            return True
        with patch.object(stop, 'wait', side_effect=wait), patch.object(unattended.subprocess, 'Popen', return_value=child) as launch:
            self.assertEqual(unattended.run(self.source, self.output, stop), 0)
        self.assertEqual(waits, [3600])
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(json.loads((self.output / 'unattended.json').read_text())['status'], 'stopped')

    def test_dashboard_pause_countdown(self):
        (self.output / 'unattended.json').write_text(json.dumps(dict(status='waiting', pid=123, next_run_at=1000, reason='Scheduled break')))
        with patch.object(dashboard.os, 'kill'), patch.object(dashboard.time, 'time', return_value=100):
            self.assertIn('AUTO RESUME in 15 min', dashboard.automation_line(self.output))
