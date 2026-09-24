"""Persistent, sequential batches with a shared request budget and resumable files."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import time
import wave

from . import scanner
from .locking import output_lock as batch_lock
from .storage import load_state, retire_checkpoint


class BatchPause(Exception):
    pass


def failure_record(path, entry, error=None):
    cause = error
    while cause is not None and cause.__cause__ is not None:
        cause = cause.__cause__
    command = getattr(cause, 'cmd', None)
    program = Path(command[0] if isinstance(command, (list, tuple)) else str(command or '')).name
    stderr = getattr(cause, 'stderr', '') or ''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    return dict(time=datetime.now(timezone.utc).isoformat(), source=str(path),
                  category=program if program in ('ffmpeg', 'ffprobe') else 'media_validation',
                  error=str(error) if error is not None else entry.get('error', 'Previously failed'),
                  stderr=stderr[-8192:], command=command,
                  timeout_seconds=getattr(cause, 'timeout', None),
                  returncode=getattr(cause, 'returncode', None),
                  progress=entry.get('progress'), historical=error is None)


def retryable_media_failure(record):
    """A decoder's exit status alone cannot distinguish bad media from lost storage."""
    if record.get('timeout_seconds') is not None:
        return True
    stderr = record.get('stderr', '').casefold()
    return any(text in stderr for text in (
        'host is down', 'no route to host', 'network is unreachable',
        'network is down', 'connection timed out', 'connection reset',
        'connection refused', 'connection aborted', 'transport endpoint',
        'input/output error', 'i/o error', 'stale file handle',
        'resource temporarily unavailable', 'no such file or directory',
        'permission denied', 'operation timed out',
    ))


def log_failure(folder, path, entry, error=None, *, retryable=False):
    """Keep permanent skips and retryable failures in separate append-only logs."""
    record = failure_record(path, entry, error)
    filename = 'retryable.jsonl' if retryable else 'skipped.jsonl'
    with (folder / filename).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    entry['retryable_failure' if retryable else 'failure'] = record
    return record


def completed_output(entry, source, args):
    """Only skip a file when its checkpoint and export agree with this run."""
    if not entry.get('output'):
        return False
    folder = Path(entry['output'])
    try:
        state = load_state(folder)
        playlist = json.loads((folder / 'playlist.json').read_text())
        identity, sampling = state['identity'], state['sampling']
        exported, recognition = playlist['source'], playlist['recognition']
        complete = (
            identity['path'] == exported['path'] == str(source)
            and identity['size'] == exported['size_bytes'] == entry['signature']['size']
            and identity['mtime_ns'] == exported['mtime_ns'] == entry['signature']['mtime_ns']
            and identity['provider'] == recognition['provider'] == 'shazam'
            and identity['version'] == 2
            and identity['interval'] == recognition['sample_interval_seconds'] == args.interval
            and identity['sample_length'] == recognition['sample_length_seconds'] == 12
            and sampling['complete'] is True and recognition['sampling_complete'] is True
            and sampling['refinement_enabled'] == recognition['refinement_enabled'] == args.refine
            and playlist['schema_version'] == 2
            and playlist['scan_state'] == state
            and isinstance(playlist['playlist'], list)
            and playlist['summary']['samples_processed'] == len(state['results'])
        )
        if complete:
            retire_checkpoint(state, folder)
        return complete
    except (OSError, ValueError, KeyError, TypeError):
        return False


def discover(source):
    # Path.rglob/is_file may suppress filesystem errors, presenting an outage
    # as an empty or truncated queue. Abort inventory instead.
    def failed(error):
        raise error

    if stat.S_ISDIR(source.stat().st_mode):
        files = (Path(root) / name for root, _, names in os.walk(source, onerror=failed)
                 for name in names)
    else:
        files = [source]
    return sorted({p.resolve() for p in files if p.suffix.lower() in scanner.MEDIA
                   and stat.S_ISREG(p.stat().st_mode)})


def run(args, event_handler=None):
    def message(text):
        if event_handler:
            event_handler(dict(kind='message', text=text))
        else:
            print(text, flush=True)

    files = discover(args.source)
    if not files:
        raise ValueError('No supported recordings found')
    if args.dry_run:
        seconds, checks, bad = 0, 0, 0
        for path in files:
            try:
                length = scanner.duration(path)
                count = sum(length - offset >= 12 for offset in range(0, math.ceil(length), args.interval))
                if not count:
                    raise ValueError('Recording is shorter than one sample')
                seconds += length
                checks += count + (max(0, count - 1) if args.refine else 0)
                message(f'{path.name}: {scanner.timestamp(length)}, {count} baseline samples')
            except (ValueError, OSError, subprocess.CalledProcessError) as error:
                bad += 1
                message(f'Unreadable: {path.name} ({type(error).__name__})')
        message(f'{len(files)} files, {seconds / 3600:.2f} hours, up to {checks} samples before checkpoint/cache reuse; {bad} unreadable.')
        return 1 if bad else 0

    identity = dict(source=str(args.source.resolve()), interval=args.interval, refine=args.refine, version=1)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    folder = args.output / 'batches' / key
    folder.mkdir(parents=True, exist_ok=True)
    queue_path = folder / 'queue.json'
    queue = json.loads(queue_path.read_text()) if queue_path.exists() else dict(identity=identity, files={})
    if queue['identity'] != identity:
        raise ValueError('Batch queue settings do not match')
    for path in files:
        stat = path.stat()
        signature = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        entry = queue['files'].get(str(path))
        if entry and entry['status'] == 'failed' and not entry.get('failure'):
            log_failure(folder, path, entry)
        if entry is None or entry['signature'] != signature:
            queue['files'][str(path)] = dict(signature=signature, status='pending')
        elif entry['status'] == 'failed' and retryable_media_failure(entry.get('failure', {})):
            # Repair queues created before storage errors were distinguished from
            # invalid media. Retain the original failure and skipped audit log.
            entry['status'] = 'pending'
            message(f'Requeued previous storage failure/timeout: {path.name}')
        elif entry['status'] in ('running', 'paused') or (args.retry_failed and entry['status'] == 'failed'):
            entry['status'] = 'pending'
        elif entry['status'] == 'complete' and not completed_output(entry, path, args):
            entry['status'] = 'pending'
    queue['active_files'] = [str(p) for p in files]
    queue['last_run'] = dict(status='running', requests=0, cache_hits=0,
                            started_at=time.time(), max_requests=args.max_requests,
                            max_minutes=args.max_minutes, pid=os.getpid())
    scanner.save(queue_path, queue)
    message(f'Batch: {len(files)} recordings | queue: {queue_path} | request cap: {args.max_requests or "none"}')
    total_calls = total_hits = 0
    started = time.monotonic()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    exit_code = 0
    try:
        for index, path in enumerate(files, 1):
            entry = queue['files'][str(path)]
            if entry['status'] in ('complete', 'failed'):
                continue
            if (args.max_requests and total_calls >= args.max_requests) or (deadline and time.monotonic() >= deadline):
                raise BatchPause('Batch limit reached')
            if total_calls:
                time.sleep(args.delay)  # Preserve pacing across recording boundaries.
            message(f'File {index}/{len(files)}: {path.name}')
            entry.update(status='running', error=None, progress=None)
            queue['last_run'].update(current=str(path), current_index=index, updated_at=time.time())
            scanner.save(queue_path, queue)
            calls = hits = 0

            def handle(event):
                nonlocal calls, hits
                if event['kind'] == 'progress':
                    # Stop before extraction/pacing. An in-flight request is allowed
                    # to save its response before the next deadline check.
                    if deadline and time.monotonic() >= deadline and event['activity'] in ('Extracting audio', 'Recognizing audio'):
                        raise BatchPause('Batch time limit reached')
                    calls = max(calls, event['requests'])
                    hits = max(hits, event['cache_hits'])
                    entry['progress'] = {k: event[k] for k in ('phase', 'done', 'total', 'songs', 'activity', 'offset', 'resumed') if k in event}
                    queue['last_run'].update(requests=total_calls + calls, cache_hits=total_hits + hits,
                                             updated_at=time.time())
                    scanner.save(queue_path, queue)
                    if not event_handler and event['activity'] == 'Sample saved':
                        message(f"  {event['phase']} {event['done']}/{event['total']} | {event['songs']} songs | batch requests {total_calls + calls}")
                elif event['kind'] == 'finished':
                    entry['status'] = 'complete' if event['complete'] else 'paused'
                elif event['kind'] == 'exported':
                    entry['output'] = event['folder']
                elif event['kind'] == 'message' and not event_handler:
                    message(event['text'])
                if event_handler:
                    event_handler(event)

            scan_args = argparse.Namespace(output=args.output, cache=None, interval=args.interval,
                sample_length=12, min_score=80, delay=args.delay, refine=args.refine,
                threaded=args.threaded,
                max_requests=args.max_requests - total_calls if args.max_requests else None,
                dry_run=False, export_only=False, seed_cache=False)
            try:
                scanner.scan(path, scan_args, dict(provider='shazam', host='shazam'), event_handler=handle)
            except (subprocess.CalledProcessError, ValueError, wave.Error) as error:
                if retryable_media_failure(failure_record(path, entry, error)):
                    entry.update(status='paused', error=str(error))
                    log_failure(folder, path, entry, error, retryable=True)
                    message(f'Storage failure or media timeout; pausing queue at {path.name}. '
                            f'Saved progress retained; log: {folder / "retryable.jsonl"}')
                    raise RuntimeError('Media temporarily unavailable; retry this recording later') from error
                entry.update(status='failed', error=str(error))
                failure = log_failure(folder, path, entry, error)
                message(f"Skipped ({failure['category']}): {path.name}: {error}; log: {folder / 'skipped.jsonl'}")
                message(f'Skipping unreadable/invalid file: {path.name}. Use --retry-failed after fixing it.')
            except (KeyboardInterrupt, BatchPause):
                entry['status'] = 'paused'
                raise
            except Exception as error:
                entry.update(status='paused', error=str(error))
                raise  # Service/network/storage errors stop the entire queue.
            finally:
                total_calls += calls
                total_hits += hits
                queue['last_run'].update(requests=total_calls, cache_hits=total_hits)
                scanner.save(queue_path, queue)
            if entry['status'] == 'paused':
                raise BatchPause('Batch request limit reached')
        queue['last_run']['status'] = 'complete_with_errors' if any(queue['files'][str(p)]['status'] == 'failed' for p in files) else 'complete'
        exit_code = 1 if queue['last_run']['status'] == 'complete_with_errors' else 0
    except BatchPause as error:
        queue['last_run']['status'] = 'paused'
        message(f'{error}; rerun the same batch command to resume.')
    except KeyboardInterrupt:
        queue['last_run']['status'] = 'paused'
        message('Batch paused; saved samples and completed files will be reused.')
        exit_code = 130
    except Exception:
        queue['last_run']['status'] = 'paused'
        raise
    finally:
        queue['last_run']['elapsed_seconds'] = round(time.monotonic() - started, 2)
        queue['last_run']['updated_at'] = time.time()
        scanner.save(queue_path, queue)
        done = sum(queue['files'][str(p)]['status'] == 'complete' for p in files)
        failed = sum(queue['files'][str(p)]['status'] == 'failed' for p in files)
        message(f'Batch {queue["last_run"]["status"]}: {done}/{len(files)} complete, {failed} failed; {total_calls} requests, {total_hits} cache hits.')
    return exit_code


def main(argv=None, event_handler=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=Path('scan_results'))
    parser.add_argument('--interval', type=int, default=45)
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--max-requests', type=int, default=500, help='New requests across the whole batch per run; 0 disables the cap')
    parser.add_argument('--max-minutes', type=float, default=0, help='Pause after this many minutes; 0 disables')
    parser.add_argument('--no-refine', dest='refine', action='store_false')
    parser.add_argument('--threaded', action='store_true', help='Prepare one sample ahead in a background thread; recognition remains sequential')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    args.source = args.source.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if args.interval < 12 or args.max_requests < 0 or not math.isfinite(args.delay) or args.delay < 0 or not math.isfinite(args.max_minutes) or args.max_minutes < 0:
        parser.error('Interval must be >=12; request/time limits and delay must be finite and nonnegative')
    if not scanner.shutil.which('ffmpeg') or not scanner.shutil.which('ffprobe'):
        parser.error('Install FFmpeg and ffprobe first')
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(*unused):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.dry_run:
            return run(args, event_handler)
        with batch_lock(args.output):
            return run(args, event_handler)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        text = f'Batch stopped: {error}. Rerun to resume saved progress.'
        if event_handler:
            event_handler(dict(kind='message', text=text))
        else:
            print(text, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
