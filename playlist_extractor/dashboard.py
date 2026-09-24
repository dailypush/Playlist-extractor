"""Read-only live batch dashboard; closing it never stops the scanner."""
import argparse
from collections import Counter
import curses
import fcntl
import json
import os
from pathlib import Path
import sys
import time

from .catalog import timestamp
from . import browser, health


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def worker_alive(output, run):
    if run.get('status') != 'running':
        return False
    try:
        if run.get('pid'):
            os.kill(run['pid'], 0)
        with (output / '.batch.lock').open('r') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    return False


def queue_paths(output):
    return sorted((output / 'batches').glob('*/queue.json'))


def load_queue(path):
    data = json.loads(path.read_text())
    if not isinstance(data.get('files'), dict) or not isinstance(data.get('last_run', {}), dict):
        raise ValueError('Invalid queue structure')
    return data


def matching_paths(output, source):
    paths = queue_paths(output)
    if source is None:
        return paths
    matched = []
    for path in paths:
        try:
            if load_queue(path).get('identity', {}).get('source') == str(source):
                matched.append(path)
        except (OSError, ValueError, TypeError):
            continue
    return matched


def automation_line(output):
    try:
        state = json.loads((output / 'unattended.json').read_text())
        if state['status'] == 'waiting':
            os.kill(state['pid'], 0)
            minutes = max(0, int((state['next_run_at'] - time.time() + 59) // 60))
            return f"AUTO RESUME in {minutes} min | {state.get('reason', 'Scheduled break')}"
        if state['status'] == 'running':
            os.kill(state['pid'], 0)
            return 'UNATTENDED: automatic resume enabled | 15-minute breaks between batches'
        return f"UNATTENDED: {state['status']}"
    except (OSError, ValueError, KeyError, TypeError):
        return ''


def render(queue, path, alive=False, failures=False, scroll=0, now=None):
    """Return plain display lines, also used by --once and offline tests."""
    now = time.time() if now is None else now
    run = queue.get('last_run', {})
    entries = queue['files']
    paths = queue.get('active_files', sorted(entries))
    items = [(p, entries[p]) for p in paths if p in entries]
    counts = Counter(e.get('status', 'pending') for _, e in items)
    state = run.get('status', 'unknown')
    if state == 'running' and not alive:
        state = 'NOT RUNNING (saved progress; resume batch to continue)'
    done = counts['complete']
    total = len(items)
    filled = int(24 * done / total) if total else 0
    cap = run.get('max_requests')
    budget = 'unlimited' if cap == 0 else str(cap) if cap is not None else 'unknown'
    elapsed = max(0, now - run['started_at']) if alive and run.get('started_at') else run.get('elapsed_seconds', 0)
    lines = ['PLAYLIST EXTRACTOR  |  LIVE BATCH MONITOR',
             f"Batch {path.parent.name} | {state}",
             f"Source: {queue.get('identity', {}).get('source', 'unknown')}",
             f"[{'#' * filled}{'-' * (24 - filled)}] {done}/{total} complete | {counts['failed']} skipped | {counts['pending'] + counts['paused']} waiting",
             f"Requests {run.get('requests', 0)}/{budget} | Cache hits {run.get('cache_hits', 0)} | Elapsed {timestamp(elapsed)}"]
    if run.get('updated_at'):
        lines.append(f"Last activity {int(max(0, now - run['updated_at']))}s ago (updates at processing steps)")
    current = next(((p, e) for p, e in items if e.get('status') == 'running'), None)
    if current is None and run.get('current') in entries:
        current = (run['current'], entries[run['current']])
    lines += ['', 'CURRENT RECORDING' if alive else 'LAST / PAUSED RECORDING']
    if current:
        p, e = current
        lines.append(f"{Path(p).name} [{e.get('status')}]")
        progress = e.get('progress') or {}
        if progress:
            lines.append(f"{progress.get('phase', '')}: {progress.get('done', 0)}/{progress.get('total', 0)} samples | {progress.get('songs', 0)} songs | {progress.get('activity', 'Saved progress')}")
            lines.append(f"Audio offset {timestamp(progress.get('offset', 0))} | {progress.get('resumed', 0)} reused samples")
        else:
            lines.append('Probing recording / preparing scan' if alive else 'No sample progress saved yet')
    else:
        lines.append('No active recording')
    if failures:
        rows = [(p, e) for p, e in items if e.get('status') == 'failed']
        lines += ['', f'SKIPPED RECORDINGS ({len(rows)}) | showing from {min(scroll + 1, len(rows))}']
        for p, e in rows[scroll:]:
            failure = e.get('failure') or {}
            lines.append(f"{Path(p).name} [{failure.get('category', 'previous failure')}]")
            lines.append('  ' + clean(failure.get('stderr') or e.get('error') or 'No error detail recorded'))
        if not rows:
            lines.append('No skipped recordings in this queue.')
        lines += ['', f"Audit log: {path.parent / 'skipped.jsonl'}", 'After fixing files, use batch --retry-failed to retry them.']
    else:
        rows = [(p, e) for p, e in items if e.get('status') in ('pending', 'paused', 'running') and (not alive or not current or p != current[0])]
        lines += ['', f'UP NEXT / NEXT RUN ({len(rows)}) | showing from {min(scroll + 1, len(rows))}']
        lines += [f"{i + scroll + 1:3}. [{e.get('status')}] {Path(p).name}" for i, (p, e) in enumerate(rows[scroll:])]
        if not rows:
            lines.append('No waiting recordings.')
    return [clean(line) for line in lines], len(rows)


def show(screen, output, source=None, health_path=health.DEFAULT_PATH, health_view=False):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(1000)
    selected = None
    failures = False
    scroll = 0
    health_scroll = 0
    while True:
        paths = matching_paths(output, source)
        if selected not in paths:
            selected = max(paths, key=lambda p: p.stat().st_mtime) if paths else None
        count = 0
        try:
            if selected:
                queue = load_queue(selected)
                lines, count = render(queue, selected, worker_alive(output, queue.get('last_run', {})), failures, scroll)
            else:
                lines = ['PLAYLIST EXTRACTOR', '', 'No batch queue yet. Waiting for the scanner...', str(output)]
        except (OSError, ValueError, TypeError, KeyError) as error:
            lines = ['Waiting for readable queue data...', clean(error)]
        snapshot = health.read(health_path)
        if health_view:
            lines = health.details(snapshot)
            count = len(lines)
            lines = lines[health_scroll:]
        else:
            lines.insert(min(5, len(lines)), health.summary(snapshot))
        screen.erase()
        height, width = screen.getmaxyx()
        for y, line in enumerate(lines[:max(0, height - 2)]):
            try:
                screen.addnstr(y, 0, clean(line), max(0, width - 1), curses.A_BOLD if y == 0 else curses.A_NORMAL)
            except curses.error:
                pass
        auto = automation_line(output)
        if auto:
            try:
                screen.addnstr(max(0, height - 2), 0, clean(auto), max(0, width - 1), curses.A_BOLD)
            except curses.error:
                pass
        footer = ('h/b back | arrows scroll | q quit' if health_view else
                  'h health | p playlists | d database | f skipped | arrows | Tab batch | q quit')
        try:
            screen.addnstr(max(0, height - 1), 0, footer, max(0, width - 1), curses.A_REVERSE)
        except curses.error:
            pass
        screen.refresh()
        key = screen.getch()
        if key in (ord('q'), ord('Q'), 27):
            return 0
        if key in (ord('h'), ord('H')) or (health_view and key == ord('b')):
            health_view, health_scroll = not health_view, 0
        elif health_view:
            step = max(1, height - 4) if key in (curses.KEY_NPAGE, curses.KEY_PPAGE) else 1
            if key in (curses.KEY_DOWN, ord('j'), curses.KEY_NPAGE):
                health_scroll = min(max(0, count - 1), health_scroll + step)
            elif key in (curses.KEY_UP, ord('k'), curses.KEY_PPAGE):
                health_scroll = max(0, health_scroll - step)
        elif key in (ord('p'), ord('d')):
            browser.show(screen, output, database=key == ord('d'))
        elif key in (ord('f'), ord('F')):
            failures, scroll = not failures, 0
        elif key in (curses.KEY_DOWN, ord('j')):
            scroll = min(max(0, count - 1), scroll + 1)
        elif key in (curses.KEY_UP, ord('k')):
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_NPAGE:
            scroll = min(max(0, count - 1), scroll + max(1, height - 15))
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - max(1, height - 15))
        elif key == 9 and paths:
            selected, scroll = paths[(paths.index(selected) + 1) % len(paths)], 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('scan_results'))
    parser.add_argument('--source', type=Path, help='Only show queues for this source')
    parser.add_argument('--once', action='store_true', help='Print a snapshot without a terminal')
    parser.add_argument('--failed', action='store_true', help='Show skipped files in the snapshot')
    parser.add_argument('--health', action='store_true', help='Open system health or print it with --once')
    parser.add_argument('--health-file', type=Path, default=health.DEFAULT_PATH, help='Health sampler snapshot')
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    source = args.source.expanduser().resolve() if args.source else None
    try:
        if args.once:
            if args.health:
                print('\n'.join(clean(line) for line in health.details(health.read(args.health_file))))
                return 0
            paths = matching_paths(output, source)
            if not paths:
                print('No batch queue yet.')
                return 0
            path = max(paths, key=lambda p: p.stat().st_mtime)
            queue = load_queue(path)
            lines, _ = render(queue, path, worker_alive(output, queue.get('last_run', {})), args.failed)
            lines.insert(5, health.summary(health.read(args.health_file)))
            print('\n'.join(lines))
            auto = automation_line(output)
            if auto:
                print(auto)
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error('Dashboard needs a terminal; use --once for a snapshot.')
        return curses.wrapper(show, output, source, args.health_file, args.health)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, curses.error) as error:
        print(f'Dashboard: {clean(error)}', file=sys.stderr)
        return 1
