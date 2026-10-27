import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from playlist_extractor import recovery


def observation(uptime=1000, **fields):
    return dict(boot_id='boot-a', uptime=uptime, gateway_ok=True, nas_ok=True,
                scanner={'eligible': True, 'marker': ['mix.mp4', 'Baseline', 10]},
                wifi_error_uptime=None, **fields)


class RecoveryTests(unittest.TestCase):
    def walk(self, state=None, start=1000, seconds=900, **changes):
        state = state or {}
        reason = None
        for uptime in range(start, start + seconds + 1, 60):
            obs = observation(uptime)
            obs.update(changes)
            state, reason = recovery.decide(state, obs)
        return state, reason

    def test_healthy_or_ordinary_scan_stall_does_not_reboot(self):
        state, reason = self.walk(seconds=1800)
        self.assertIsNone(reason)
        self.assertEqual(state['status'], 'monitoring')

    def test_both_local_targets_must_fail_for_full_threshold(self):
        state, reason = self.walk(seconds=840, gateway_ok=False, nas_ok=False)
        self.assertIsNone(reason)
        obs = observation(1900); obs.update(gateway_ok=False, nas_ok=False)
        state, reason = recovery.decide(state, obs)
        self.assertIn('Gateway and NAS', reason)
        for changes in ({'gateway_ok': False}, {'nas_ok': False},
                        {'gateway_ok': None, 'nas_ok': False}):
            self.assertIsNone(self.walk(seconds=1800, **changes)[1])

    def test_recovery_resets_continuous_failure(self):
        state, _ = self.walk(seconds=840, gateway_ok=False, nas_ok=False)
        state, reason = recovery.decide(state, observation(1900))
        self.assertIsNone(reason)
        self.assertNotIn('network_since', state)
        _, reason = self.walk(state, start=1960, seconds=840, gateway_ok=False, nas_ok=False)
        self.assertIsNone(reason)

    def test_boot_grace_and_missed_checks_do_not_count_as_observed_failure(self):
        state, reason = self.walk(start=0, seconds=540, gateway_ok=False, nas_ok=False)
        self.assertEqual(state['status'], 'boot_grace')
        self.assertIsNone(reason)
        state, _ = self.walk(start=1000, seconds=840, gateway_ok=False, nas_ok=False)
        obs = observation(4000); obs.update(gateway_ok=False, nas_ok=False)
        state, reason = recovery.decide(state, obs)
        self.assertIsNone(reason)
        self.assertEqual(state['network_since'], 4000)

    def test_stalled_scan_requires_driver_error_during_that_stall(self):
        state, reason = self.walk(seconds=900, wifi_error_uptime=1120)
        self.assertIn('No scan progress', reason)
        self.assertIsNone(self.walk(seconds=1800, wifi_error_uptime=999)[1])
        # The evidence remains latched when the journal window rolls forward.
        state, reason = recovery.decide(state, observation(1960))
        self.assertIn('No scan progress', reason)

    def test_saved_sample_or_file_change_clears_old_driver_incident(self):
        state, _ = self.walk(seconds=840, wifi_error_uptime=1120)
        obs = observation(1900)
        obs['scanner']['marker'][-1] = 11
        state, reason = recovery.decide(state, obs)
        self.assertIsNone(reason)
        self.assertNotIn('wifi_incident', state)
        _, reason = self.walk(state, start=1960, seconds=900,
                              scanner=obs['scanner'], wifi_error_uptime=1120)
        self.assertIsNone(reason)

    def test_scheduled_break_provider_errors_and_manual_stop_are_ineligible(self):
        for status in ('waiting', 'stopped', 'finished'):
            state, reason = self.walk(seconds=1800, wifi_error_uptime=1120,
                                      scanner={'eligible': False, 'status': status})
            self.assertIsNone(reason)
            self.assertNotIn('stall_since', state)

    def test_reboot_clears_incident_but_preserves_cooldown(self):
        state, _ = self.walk(seconds=900, gateway_ok=False, nas_ok=False)
        state['reboots'] = [{'at': state['clock'], 'reason': 'test'}]
        state['wifi_incident'] = 1120
        obs = observation(30); obs['boot_id'] = 'boot-b'
        state, reason = recovery.decide(state, obs)
        self.assertIsNone(reason)
        self.assertNotIn('wifi_incident', state)
        self.assertEqual(len(state['reboots']), 1)
        state, reason = self.walk(state, start=600, seconds=900, boot_id='boot-b',
                                  gateway_ok=False, nas_ok=False)
        self.assertIsNone(reason)
        self.assertEqual(state['status'], 'cooldown')

    def test_second_attempt_allowed_after_six_hours_then_rolling_limit(self):
        state, _ = self.walk(seconds=900, gateway_ok=False, nas_ok=False)
        first = state['clock']
        state['reboots'] = [{'at': first}]
        state, reason = self.walk(state, start=1900 + recovery.COOLDOWN,
                                  seconds=900, gateway_ok=False, nas_ok=False)
        self.assertIsNotNone(reason)
        state['reboots'].append({'at': state['clock']})
        state, reason = self.walk(state, start=int(state['uptime']) + recovery.COOLDOWN,
                                  seconds=900, gateway_ok=False, nas_ok=False)
        self.assertIsNone(reason)
        self.assertEqual(state['status'], 'reboot_limit')
        state, reason = self.walk(state, start=int(state['uptime']) + recovery.WINDOW,
                                  seconds=900, gateway_ok=False, nas_ok=False)
        self.assertIsNotNone(reason)

    def test_policy_does_not_depend_on_wall_clock_or_mutate_input(self):
        state, _ = self.walk(seconds=840, gateway_ok=False, nas_ok=False)
        original = copy.deepcopy(state)
        obs = observation(1900); obs.update(gateway_ok=False, nas_ok=False)
        with patch.object(recovery.time, 'time', side_effect=AssertionError('wall clock')):
            _, reason = recovery.decide(state, obs)
        self.assertIsNotNone(reason)
        self.assertEqual(state, original)

    def test_dry_run_never_writes_or_calls_reboot(self):
        state, reason = self.walk(gateway_ok=False, nas_ok=False)
        with patch.object(recovery, 'persist') as save, patch.object(recovery, 'command') as cmd:
            self.assertEqual(recovery.apply(state, reason, Path('/unused'), False), 'would_reboot')
        save.assert_not_called()
        cmd.assert_not_called()

    def test_attempt_is_durable_before_normal_reboot_command(self):
        state, reason = self.walk(gateway_ok=False, nas_ok=False)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            def reboot(args, timeout):
                saved = recovery.load_state(folder / 'state.json')
                self.assertEqual(len(saved['reboots']), 1)
                self.assertEqual(saved['status'], 'reboot_requested')
                self.assertEqual(args, ['systemctl', '--no-block', '--no-wall', 'reboot'])
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
            with patch.object(recovery, 'command', side_effect=reboot):
                recovery.apply(state, reason, folder, True)
            self.assertEqual((folder / 'state.json').stat().st_mode & 0o777, 0o600)

    def test_failed_state_write_prevents_reboot(self):
        state, reason = self.walk(gateway_ok=False, nas_ok=False)
        with patch.object(recovery, 'persist', side_effect=OSError('disk full')), patch.object(recovery, 'command') as cmd:
            with self.assertRaises(OSError):
                recovery.apply(state, reason, Path('/unused'), True)
        cmd.assert_not_called()

    def test_failed_reboot_command_consumes_budget(self):
        state, reason = self.walk(gateway_ok=False, nas_ok=False)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(recovery, 'command', return_value=subprocess.CompletedProcess([], 1, stdout='', stderr='denied')):
                with self.assertRaises(RuntimeError):
                    recovery.apply(state, reason, folder, True)
            saved = recovery.load_state(folder / 'state.json')
            self.assertEqual(len(saved['reboots']), 1)

    def test_corrupt_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'state.json'
            self.assertEqual(recovery.load_state(p), {})
            good, _ = self.walk()
            bad_states = [{}, [], dict(good, clock=float('nan')),
                          dict(good, reboots=[{'at': -1}]),
                          dict(good, reboots=[{'at': 10}, {'at': 1}]),
                          dict(good, network_since=-100)]
            for value in bad_states:
                with self.subTest(value=value):
                    p.write_text(json.dumps(value))
                    with self.assertRaises(ValueError):
                        recovery.load_state(p)

    def test_gateway_probe_errors_are_unknown(self):
        for code, expected in ((0, True), (1, False), (2, None)):
            with patch.object(recovery, 'command', return_value=subprocess.CompletedProcess([], code)):
                self.assertIs(recovery.gateway_probe('192.0.2.1'), expected)

    def test_scanner_state_classifies_breaks_and_storage_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); queue_dir = root / 'batches' / 'test'; queue_dir.mkdir(parents=True)
            queue = {'identity': {'source': '/nas'}, 'files': {'/nas/a.mp4': {'error': None}},
                     'last_run': {'current': '/nas/a.mp4'}}
            (queue_dir / 'queue.json').write_text(json.dumps(queue))
            for status, error, active, expected in (
                    ('running', None, True, True), ('waiting', None, True, False),
                    ('waiting', 'Shazam HTTP 429', True, False),
                    ('waiting', 'Media temporarily unavailable; retry later', True, True),
                    ('running', None, False, False)):
                (root / 'unattended.json').write_text(json.dumps({'source': '/nas', 'status': status, 'last_error': error}))
                with patch.object(recovery, 'command', return_value=subprocess.CompletedProcess([], 0, stdout='active' if active else 'inactive')):
                    self.assertEqual(recovery.scanner_state(root)['eligible'], expected)


if __name__ == '__main__':
    unittest.main()
