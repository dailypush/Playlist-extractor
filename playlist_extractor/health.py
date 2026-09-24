"""Bounded Linux health snapshots for the read-only dashboard."""
import argparse
from collections import Counter
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

DEFAULT_PATH = Path('/run/playlist-extractor-health/status.json')
JOURNAL_LIMIT = 200


def kernel_health():
    try:
        result = subprocess.run(
            ['journalctl', '-k', '-b', '--since', '15 minutes ago', '-p', 'warning',
             '-n', str(JOURNAL_LIMIT), '--no-pager', '-o', 'json'],
            capture_output=True, text=True, timeout=4)
        if result.returncode or 'permission' in result.stderr.lower() or 'not seeing' in result.stderr.lower():
            raise OSError(result.stderr.strip() or 'Kernel journal unavailable')
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        counts = Counter(memory=0, wifi=0, nas=0)
        recent = []
        for row in rows:
            message = str(row.get('MESSAGE', ''))
            lower = message.lower()
            kinds = []
            if any(s in lower for s in ('page allocation failure', 'out of memory', 'oom-kill')):
                kinds.append('memory')
            if 'brcmf' in lower or ('wlan' in lower and any(s in lower for s in ('fail', 'disconnect', 'error'))):
                kinds.append('wifi')
            if 'cifs' in lower or 'smb' in lower:
                kinds.append('nas')
            counts.update(kinds)
            if kinds:
                recent.append({'time': int(row['__REALTIME_TIMESTAMP']) / 1e6,
                               'message': message[:240]})
        return {'available': True, 'counts': dict(counts), 'recent': recent[-5:],
                'limited': len(rows) >= JOURNAL_LIMIT}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        return {'available': False, 'error': str(error)[:240]}


def collect(host, mount, interface='wlan0', proc=Path('/proc')):
    data = {'version': 1, 'updated_at': time.time(), 'errors': []}
    try:
        data['memory'] = {key: int(value.split()[0]) for key, value in
                          (line.split(':', 1) for line in (proc / 'meminfo').read_text().splitlines())
                         if key in ('MemTotal', 'MemAvailable', 'MemFree', 'SwapTotal', 'SwapFree')}
        data['reserve_kib'] = int((proc / 'sys/vm/min_free_kbytes').read_text())
        data['watermark_scale_factor'] = int((proc / 'sys/vm/watermark_scale_factor').read_text())
        data['load'] = os.getloadavg()[0]
    except (OSError, ValueError) as error:
        data['errors'].append('Memory: ' + str(error))
    try:
        row = next(line.split(':', 1)[1].split() for line in (proc / 'net/wireless').read_text().splitlines()
                   if line.split(':', 1)[0].strip() == interface)
        data['wifi'] = {'interface': interface, 'signal_dbm': float(row[2].rstrip('.'))}
    except (OSError, ValueError, IndexError, StopIteration):
        data['wifi'] = {'interface': interface, 'signal_dbm': None}
    nas = data['nas'] = {'host': host, 'port': 445, 'mounted': None, 'reachable': False}
    try:
        mounts = [line.split() for line in (proc / 'mounts').read_text().splitlines()]
        nas['mounted'] = any(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), row[1]) == str(mount)
                             and row[2] in ('cifs', 'smb3') for row in mounts)
    except (OSError, IndexError) as error:
        data['errors'].append('Mount: ' + str(error))
    started = time.monotonic()
    try:
        with socket.create_connection((host, 445), timeout=1):
            nas.update(reachable=True, latency_ms=round((time.monotonic() - started) * 1000))
    except OSError as error:
        nas['error'] = str(error)[:160]
    data['kernel'] = kernel_health()
    return data


def read(path=DEFAULT_PATH):
    try:
        data = json.loads(path.read_text())
        if data.get('version') != 1 or not isinstance(data.get('updated_at'), (float, int)):
            raise ValueError('Invalid health snapshot')
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def age(data, now=None):
    return int((time.time() if now is None else now) - data['updated_at'])


def mib(value):
    return '?' if value is None else str(round(value / 1024))


def summary(data, now=None):
    if not data:
        return 'HEALTH: unavailable | h details'
    seconds = age(data, now)
    if seconds > 90 or seconds < -60:
        return f'HEALTH: STALE ({seconds}s old) | h details'
    nas = data.get('nas', {})
    kernel = data.get('kernel', {})
    warnings = str(sum(kernel.get('counts', {}).values())) if kernel.get('available') else '?'
    if kernel.get('limited'):
        warnings += '+'
    state = 'reachable' if nas.get('reachable') else 'UNREACHABLE'
    if nas.get('mounted') is False:
        state = 'UNMOUNTED'
    elif nas.get('mounted') is None:
        state = 'mount unknown'
    return (f"HEALTH: RAM {mib(data.get('memory', {}).get('MemAvailable'))}MiB avail | "
            f'SMB {state} | 15m warnings {warnings} | h')


def details(data, now=None):
    if not data:
        return ['SYSTEM HEALTH', '', 'Health sampler unavailable.',
                'On DietPi: systemctl status playlist-extractor-health.timer',
                'The scanner and its saved progress are independent of this sampler.']
    memory, nas, kernel = (data.get(k, {}) for k in ('memory', 'nas', 'kernel'))
    total, free = memory.get('SwapTotal'), memory.get('SwapFree')
    used = total - free if total is not None and free is not None else None
    wifi = data.get('wifi', {})
    lines = ['SYSTEM HEALTH', summary(data, now),
             f'Updated {age(data, now)}s ago | collected every 30s', '',
             f"RAM: {mib(memory.get('MemAvailable'))} MiB available / {mib(memory.get('MemTotal'))} MiB total",
             f"Free RAM: {mib(memory.get('MemFree'))} MiB | reserve target: {mib(data.get('reserve_kib'))} MiB",
             f"Swap: {mib(used)} / {mib(total)} MiB used | load (1m): {data.get('load', '?')}",
             f"Wi-Fi: {wifi.get('interface', '?')} | signal: {wifi.get('signal_dbm', '?')} dBm", '',
             f"NAS: {nas.get('host', '?')}:445 | SMB port {'reachable' if nas.get('reachable') else 'UNREACHABLE'}",
             f"SMB mount: { {True: 'present', False: 'absent', None: 'unknown'}.get(nas.get('mounted')) }",
             'Port/mount checks do not prove the share can read files.', '']
    if nas.get('error'):
        lines.append('NAS check: ' + nas['error'])
    if kernel.get('available'):
        counts = kernel['counts']
        lines.append(f"Kernel warnings (last 15m): memory {counts['memory']} | Wi-Fi {counts['wifi']} | SMB {counts['nas']}")
        if kernel.get('limited'):
            lines.append('Counts cover only the latest 200 warning messages; more may exist.')
        lines.append('Recent relevant messages (counts are log messages, not outages):')
        for item in kernel.get('recent', []):
            lines.append(time.strftime('%H:%M:%S', time.localtime(item['time'])) + ' ' + item['message'])
        if not kernel.get('recent'):
            lines.append('No relevant kernel warnings in the checked window.')
    else:
        lines.append('Kernel warnings: unavailable (' + kernel.get('error', 'unknown reason') + ')')
    lines.extend(data.get('errors', []))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nas-host', required=True, type=lambda value: str(ipaddress.ip_address(value)),
                        help='NAS IP address (numeric, so probes cannot stall on DNS)')
    parser.add_argument('--mount', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)
    data = collect(args.nas_host, args.mount)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.chmod(0o644)
    temporary.replace(args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
