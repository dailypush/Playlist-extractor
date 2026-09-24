import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from playlist_extractor import dashboard, health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = dict(version=1, updated_at=100, memory=dict(MemTotal=474816,
                         MemAvailable=200000, MemFree=40000, SwapTotal=100000, SwapFree=90000),
                         reserve_kib=16384, load=1.2,
                         nas=dict(host='192.0.2.1', mounted=True, reachable=True),
                         kernel=dict(available=True, counts=dict(memory=0, wifi=0, nas=0), recent=[]))

    def test_stale_and_unavailable_are_not_healthy(self):
        self.assertIn('STALE', health.summary(self.data, now=200))
        self.assertIn('unavailable', health.summary(None))
        self.data['kernel'] = {'available': False, 'error': 'permission denied'}
        self.assertIn('warnings ?', health.summary(self.data, now=110))
        self.assertIn('permission denied', '\n'.join(health.details(self.data, now=110)))
        self.data['nas']['reachable'] = False
        self.assertIn('UNREACHABLE', health.summary(self.data, now=110))
        self.data['nas']['mounted'] = False
        self.assertIn('UNMOUNTED', health.summary(self.data, now=110))

    def test_kernel_counts_relevant_messages_and_retains_timestamps(self):
        messages = ['page allocation failure: order:0', 'brcmfmac: RXHEADER FAILED',
                    'CIFS: server has not responded', 'unrelated warning']
        output = '\n'.join(json.dumps({'MESSAGE': m, '__REALTIME_TIMESTAMP': '100000000'}) for m in messages)
        result = subprocess.CompletedProcess([], 0, stdout=output, stderr='')
        with patch.object(health.subprocess, 'run', return_value=result) as run:
            result = health.kernel_health()
        self.assertEqual(result['counts'], {'memory': 1, 'wifi': 1, 'nas': 1})
        self.assertEqual(len(result['recent']), 3)
        self.assertEqual(result['recent'][0]['time'], 100)
        self.assertEqual(run.call_args.kwargs['timeout'], 4)
        self.assertIn('200', run.call_args.args[0])

    def test_kernel_permission_failure_and_timeout_report_unknown(self):
        with patch.object(health.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stdout='', stderr='Permission denied')):
            self.assertFalse(health.kernel_health()['available'])
        with patch.object(health.subprocess, 'run', side_effect=subprocess.TimeoutExpired('journalctl', 4)):
            self.assertFalse(health.kernel_health()['available'])

    def test_collector_uses_bounded_port_check_without_reading_share(self):
        (self.root / 'sys/vm').mkdir(parents=True)
        (self.root / 'net').mkdir()
        (self.root / 'meminfo').write_text('MemTotal: 474816 kB\nMemAvailable: 200000 kB\nMemFree: 40000 kB\nSwapTotal: 100000 kB\nSwapFree: 90000 kB\n')
        (self.root / 'sys/vm/min_free_kbytes').write_text('16384')
        (self.root / 'sys/vm/watermark_scale_factor').write_text('100')
        (self.root / 'net/wireless').write_text('wlan0: 0000 50. -62. -256 0 0\n')
        (self.root / 'mounts').write_text('//server/share /mnt/nas cifs ro 0 0\n')
        with patch.object(health.socket, 'create_connection', side_effect=socket.timeout('timed out')) as connect, patch.object(health, 'kernel_health', return_value={'available': False}):
            data = health.collect('192.0.2.1', Path('/mnt/nas'), proc=self.root)
        connect.assert_called_once_with(('192.0.2.1', 445), timeout=1)
        self.assertEqual(data['reserve_kib'], 16384)
        self.assertEqual(data['wifi']['signal_dbm'], -62)
        self.assertTrue(data['nas']['mounted'])
        self.assertFalse(data['nas']['reachable'])
        self.assertIn('timed out', data['nas']['error'])
        self.assertIn('do not prove', '\n'.join(health.details(data)))

    def test_snapshot_health_needs_no_queue_and_is_read_only(self):
        path = self.root / 'health.json'
        path.write_text(json.dumps(self.data))
        before = path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(dashboard.main(['--output', str(self.root), '--once', '--health', '--health-file', str(path)]), 0)
        self.assertIn('SYSTEM HEALTH', output.getvalue())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [path])
        path.write_text('{')
        self.assertIsNone(health.read(path))

    def test_h_opens_health_and_b_returns_without_scanner_actions(self):
        screen = Mock()
        screen.getmaxyx.return_value = (30, 100)
        screen.getch.side_effect = [ord('h'), ord('b'), ord('q')]
        with patch.object(dashboard.curses, 'curs_set'), patch.object(health, 'read', return_value=self.data):
            self.assertEqual(dashboard.show(screen, self.root), 0)
        drawn = [call.args[2] for call in screen.addnstr.call_args_list]
        self.assertIn('SYSTEM HEALTH', drawn)
        self.assertIn('h/b back | arrows scroll | q quit', drawn)
        self.assertFalse(list(self.root.iterdir()))

    def test_sampler_writes_readable_snapshot_atomically(self):
        path = self.root / 'run/status.json'
        with patch.object(health, 'collect', return_value=self.data):
            health.main(['--nas-host', '192.0.2.1', '--mount', '/mnt/nas', '--output', str(path)])
        self.assertEqual(health.read(path), self.data)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertFalse(path.with_suffix('.tmp').exists())
