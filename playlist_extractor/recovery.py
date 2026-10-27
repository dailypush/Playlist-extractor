"""Conservative, persistent reboot policy for the DietPi network outage."""
import argparse
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import time

from . import diagnostics

FAILURE_SECONDS = 15 * 60
BOOT_GRACE = 10 * 60
MAX_CHECK_GAP = 150
COOLDOWN = 6 * 3600
WINDOW = 24 * 3600
MAX_REBOOTS = 2
WIFI_ERROR = re.compile(r'(?:brcmf.*(?:fail|error|timeout|timed out)|'
                        r'mmc1:.*(?:inhibit|error|timeout|timed out))', re.I)
STORAGE_ERROR = re.compile(r'host is down|media temporarily unavailable|'
                           r'(?:audio extraction|media probe).*timed out|'
                           r'input/output error|stale file handle', re.I)


def command(args, timeout=4):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def gateway_probe(host):
    try:
        result = command(['ping', '-n', '-c', '1', '-W', '2', host])
        # ping exit 1 means no reply; an execution/configuration error is unknown.
        return {0: True, 1: False}.get(result.returncode)
    except (OSError, subprocess.TimeoutExpired):
        return None


def nas_probe(host):
    try:
        with socket.create_connection((host, 445), timeout=2):
            return True
    except (TimeoutError, ConnectionError):
        return False
    except OSError as error:
        import errno
        return False if error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH) else None


def wifi_error_time():
    """Use bounded journal access, never a driver ioctl or the NAS mount."""
    try:
        result = command(['journalctl', '-k', '-b', '--since', '20 minutes ago',
                          '-n', '200', '--no-pager', '-o', 'json'])
        if result.returncode:
            return None
        events = [json.loads(line) for line in result.stdout.splitlines() if line]
        return max((int(row['__MONOTONIC_TIMESTAMP']) / 1e6 for row in events
                    if WIFI_ERROR.search(str(row.get('MESSAGE', '')))), default=None)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None


def scanner_state(output):
    try:
        active = command(['systemctl', 'is-active', 'playlist-extractor']).stdout.strip() == 'active'
    except (OSError, subprocess.TimeoutExpired):
        active = False
    automation = diagnostics.read_json(output / 'unattended.json')
    if not isinstance(automation, dict):
        return {'active': False}
    for path in sorted((output / 'batches').glob('*/queue.json')):
        queue = diagnostics.read_json(path)
        if not isinstance(queue, dict) or queue.get('identity', {}).get('source') != automation.get('source'):
            continue
        run = queue.get('last_run') or {}
        entry = queue.get('files', {}).get(run.get('current'), {})
        progress = entry.get('progress') or {}
        error = ' '.join(str(v or '') for v in
                         (automation.get('last_error'), run.get('error'), entry.get('error')))
        status = automation.get('status')
        # Provider errors and scheduled request breaks are not storage failures.
        eligible = active and (status == 'running' or
                              (status == 'waiting' and bool(STORAGE_ERROR.search(error))))
        return {'active': active, 'eligible': eligible, 'status': status,
                'marker': [run.get('current'), progress.get('phase'), progress.get('done')],
                'error': error[:1000]}
    return {'active': active, 'eligible': False}


def decide(previous, observation):
    """Pure policy. No reboot and no filesystem/network calls here.

    Cooldowns accrue measured uptime across boots, conservatively excluding
    powered-off time. Wall-clock/NTP jumps cannot reset the reboot budget.
    """
    state = copy.deepcopy(previous)
    uptime, boot = observation['uptime'], observation['boot_id']
    same_boot = state.get('boot_id') == boot
    old_uptime = state.get('uptime', uptime)
    delta = max(0, uptime - old_uptime) if same_boot else (uptime if state else 0)
    clock = state.get('clock', 0) + delta
    history = [event for event in state.get('reboots', []) if clock - event['at'] < WINDOW]
    continuous = same_boot and 0 <= uptime - old_uptime <= MAX_CHECK_GAP
    if not continuous:
        for key in ('network_since', 'stall_since', 'marker', 'wifi_incident'):
            state.pop(key, None)
    state.update(version=1, boot_id=boot, uptime=uptime, clock=clock, reboots=history)
    state['observation'] = observation
    reason = None
    if uptime < BOOT_GRACE:
        state.pop('network_since', None)
        state.pop('stall_since', None)
        state['status'] = 'boot_grace'
        return state, None

    # A NAS outage alone is not evidence that rebooting this Pi will help.
    if observation['gateway_ok'] is False and observation['nas_ok'] is False:
        state.setdefault('network_since', uptime)
        if uptime - state['network_since'] >= FAILURE_SECONDS:
            reason = 'Gateway and NAS unreachable for at least 15 minutes'
    else:
        state.pop('network_since', None)

    scan = observation.get('scanner', {})
    marker = scan.get('marker')
    if not scan.get('eligible') or not marker or marker[0] is None:
        state.pop('stall_since', None)
        state.pop('marker', None)
        state.pop('wifi_incident', None)
    elif marker != state.get('marker') or 'stall_since' not in state:
        state.update(marker=marker, stall_since=uptime)
        state.pop('wifi_incident', None)
    else:
        error_time = observation.get('wifi_error_uptime')
        if error_time is not None and state['stall_since'] <= error_time <= uptime:
            state['wifi_incident'] = error_time
        # Require a driver failure during this exact no-progress interval.
        if (uptime - state['stall_since'] >= FAILURE_SECONDS and
                state.get('wifi_incident', -1) >= state['stall_since']):
            reason = 'No scan progress for at least 15 minutes with a Wi-Fi/SDIO error'

    if reason:
        if history and clock - history[-1]['at'] < COOLDOWN:
            state['status'] = 'cooldown'
        elif len(history) >= MAX_REBOOTS:
            state['status'] = 'reboot_limit'
        else:
            state['status'] = 'reboot_due'
            return state, reason
    else:
        state['status'] = 'observing_failure' if 'network_since' in state else 'monitoring'
    return state, None


def load_state(path):
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    # Corrupt rate-limit state must fail closed, never reset the reboot budget.
    if not isinstance(state, dict) or state.get('version') != 1:
        raise ValueError('Invalid recovery state; automatic reboot disabled')
    for key in ('uptime', 'clock'):
        if not isinstance(state.get(key), (int, float)) or not math.isfinite(state[key]) or state[key] < 0:
            raise ValueError('Invalid recovery clock; automatic reboot disabled')
    if not isinstance(state.get('boot_id'), str) or not isinstance(state.get('reboots'), list):
        raise ValueError('Invalid recovery history; automatic reboot disabled')
    times = []
    for event in state['reboots']:
        value = event.get('at') if isinstance(event, dict) else None
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= state['clock']:
            raise ValueError('Invalid reboot timestamp; automatic reboot disabled')
        times.append(value)
    if times != sorted(times):
        raise ValueError('Unordered reboot history; automatic reboot disabled')
    for key in ('network_since', 'stall_since', 'wifi_incident'):
        if key in state and (not isinstance(state[key], (int, float)) or
                             not math.isfinite(state[key]) or not 0 <= state[key] <= state['uptime']):
            raise ValueError('Invalid recovery observation; automatic reboot disabled')
    return state


def persist(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        os.chmod(temporary, 0o600)
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply(state, reason, directory, armed):
    if not armed:
        return 'would_reboot' if reason else state['status']
    if reason:
        state['reboots'].append({'at': state['clock'], 'time': time.time(),
                                 'boot_id': state['boot_id'], 'reason': reason})
        state['status'] = 'reboot_requested'
    # A failed write prevents the reboot. Record even unsuccessful requests to
    # prevent a command failure from becoming a repeated reboot attempt loop.
    persist(directory / 'state.json', state)
    if reason:
        diagnostics.record(directory, 'reboot_requested', channel='recovery',
                           reason=reason, observation=state['observation'])
        # Normal systemd shutdown stops the scanner and preserves saved samples.
        result = command(['systemctl', '--no-block', '--no-wall', 'reboot'], timeout=5)
        if result.returncode:
            raise RuntimeError('Reboot request failed: ' + result.stderr[:300])
    return state['status']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', required=True, type=lambda s: str(ipaddress.IPv4Address(s)))
    parser.add_argument('--nas-host', required=True, type=lambda s: str(ipaddress.IPv4Address(s)))
    parser.add_argument('--output', type=Path, default=Path('/var/lib/playlist-extractor'))
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/playlist-extractor-recovery'))
    parser.add_argument('--allow-reboot', action='store_true', help='Persist checks and enable recovery; otherwise read-only')
    args = parser.parse_args(argv)
    previous = load_state(args.state_dir / 'state.json')
    observation = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                   'uptime': float(Path('/proc/uptime').read_text().split()[0]),
                   'gateway_ok': gateway_probe(args.gateway), 'nas_ok': nas_probe(args.nas_host),
                   'scanner': scanner_state(args.output), 'wifi_error_uptime': wifi_error_time()}
    state, reason = decide(previous, observation)
    status = apply(state, reason, args.state_dir, args.allow_reboot)
    if args.allow_reboot and (status != previous.get('status') or reason):
        diagnostics.record(args.state_dir, 'check', channel='recovery', status=status,
                           reason=reason, observation=observation)
    print(json.dumps({'status': status, 'reason': reason, 'armed': args.allow_reboot,
                      'observation': observation, 'recent_reboot_requests': len(state['reboots'])}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
