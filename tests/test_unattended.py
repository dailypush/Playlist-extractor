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
        with patch.object(unattended.subprocess, 'Popen', return_value=child) as launch, patch.object(unattended, 'outcome', side_effect=['break', 'finished']), patch.object(unattended, 'wait_until') as wait:
            self.assertEqual(unattended.run(self.source, self.output, stop), 0)
        self.assertEqual(wait.call_count, 1)
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
        self.assertEqual(waits, [60])
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(json.loads((self.output / 'unattended.json').read_text())['status'], 'stopped')
        events = [json.loads(line) for line in (self.output / 'diagnostics/unattended.jsonl').read_text().splitlines()]
        self.assertEqual(next(e for e in events if e['event'] == 'wait_started')['delay_seconds'], 3600)
        self.assertIn('next_run_at', json.loads((self.output / 'unattended.json').read_text()))

    def test_wait_rechecks_deadline_after_clock_sync(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        heartbeat = Mock()
        with patch.object(unattended.time, 'time', side_effect=[100, 4000]):
            unattended.wait_until(3700, stop, heartbeat)
        stop.wait.assert_called_once_with(60)
        heartbeat.assert_called_once()

    def test_restart_preserves_wait_and_logs_it_without_starting_worker(self):
        (self.output / 'unattended.json').write_text(json.dumps(dict(
            source=str(self.source), status='waiting', failures=6,
            next_run_at=unattended.time.time() + 3600, reason='Network failure')))
        stop = threading.Event()
        def wait(seconds):
            stop.set()
            return True
        with patch.object(stop, 'wait', side_effect=wait), patch.object(unattended.subprocess, 'Popen') as launch:
            unattended.run(self.source, self.output, stop)
        launch.assert_not_called()
        state = json.loads((self.output / 'unattended.json').read_text())
        self.assertEqual(state['failures'], 6)
        self.assertIsNotNone(state['next_run_at'])
        events = [json.loads(line)['event'] for line in (self.output / 'diagnostics/unattended.jsonl').read_text().splitlines()]
        self.assertIn('wait_restored', events)

    def test_dashboard_pause_countdown(self):
        (self.output / 'unattended.json').write_text(json.dumps(dict(status='waiting', pid=123, next_run_at=1000, reason='Scheduled break')))
        with patch.object(dashboard.os, 'kill'), patch.object(dashboard.time, 'time', return_value=100):
            self.assertIn('AUTO RESUME in 15 min', dashboard.automation_line(self.output))
