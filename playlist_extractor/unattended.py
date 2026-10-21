"""Run capped batches to completion with breaks and bounded error backoff."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback

from .scanner import save
from . import diagnostics


def queue_path(source, output):
    identity = dict(source=str(source.resolve()), interval=45, refine=True, version=1)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return output / 'batches' / key / 'queue.json'


def outcome(path, started, code):
    try:
        run = json.loads(path.read_text())['last_run']
        # A failed startup must not reuse the previous invocation's outcome.
        if run.get('started_at', 0) >= started:
            if run['status'] in ('complete', 'complete_with_errors'):
                return 'finished'
            if code == 0 and run['status'] == 'paused':
                return 'break'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 'error'


def retry_delay(failures):
    return min(6 * 3600, 3600 * 2 ** min(max(0, failures - 1), 3))


def wait_until(deadline, stop, heartbeat):
    # Pi clocks jump forward after boot/NTP. Recheck the persisted wall-clock
    # deadline instead of sleeping a stale duration calculated before sync.
    while not stop.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        heartbeat()
        if stop.wait(min(60, remaining)):
            return


def run(source, output, stop):
    output.mkdir(parents=True, exist_ok=True)
    status_path = output / 'unattended.json'
    state = dict(pid=os.getpid(), source=str(source), status='starting', failures=0)
    child = None
    def log(kind, **fields):
        diagnostics.record(output, kind, channel='unattended', **fields)

    def heartbeat():
        state['updated_at'] = time.time()
        save(status_path, state)
        log('heartbeat', state=state,
            **diagnostics.snapshot(queue_path(source, output), child.pid if child else None))

    log('service_started', source=str(source))
    try:
        previous = json.loads(status_path.read_text())
        if previous.get('source') == str(source):
            state['failures'] = previous.get('failures', 0)
            if previous.get('status') in ('waiting', 'stopped') and previous.get('next_run_at'):
                # Preserve a service backoff if the Pi reboots during the pause.
                remaining = max(0, previous.get('next_run_at', 0) - time.time())
                if remaining:
                    state.update(status='waiting', next_run_at=previous['next_run_at'], reason=previous.get('reason'),
                                 last_error=previous.get('last_error'))
                    save(status_path, state)
                    log('wait_restored', state=state, remaining_seconds=remaining)
                    print(f'Restored retry/break: {remaining / 60:.1f} minutes remaining; {state.get("reason")}', flush=True)
                    wait_until(state['next_run_at'], stop, heartbeat)
    except (OSError, ValueError, TypeError):
        pass
    try:
        while not stop.is_set():
            started = time.time()
            state.update(status='running', next_run_at=None, updated_at=started)
            save(status_path, state)
            command = [sys.executable, '-m', 'playlist_extractor', 'batch', str(source),
                       '--output', str(output), '--max-requests', '500']
            child = subprocess.Popen(command)
            log('worker_started', child_pid=child.pid, command=command)
            next_heartbeat = time.monotonic()
            while child.poll() is None:
                if time.monotonic() >= next_heartbeat:
                    heartbeat()
                    next_heartbeat = time.monotonic() + 60
                if stop.wait(1):
                    child.terminate()
                    child.wait(timeout=150)
                    break
            code = child.returncode
            child = None
            if stop.is_set():
                break
            result = outcome(queue_path(source, output), started, code)
            state['last_exit_code'] = code
            context = diagnostics.snapshot(queue_path(source, output))
            # Ignore queue errors from an earlier invocation (inventory can
            # fail before a new queue record is created).
            last_run = context['last_run']
            state['last_error'] = (last_run.get('error') or context.get('error')) if last_run.get('started_at', 0) >= started else None
            log('worker_exited', exit_code=code, result=result, **context)
            if result == 'finished':
                state.update(status='finished', next_run_at=None, failures=0)
                save(status_path, state)
                log('queue_finished', state=state)
                print('Unattended queue finished. Review skipped.jsonl for any failed recordings.', flush=True)
                return 0
            if result == 'break':
                state['failures'] = 0
                delay = 15 * 60
                reason = 'Batch allowance reached; scheduled break'
            else:
                state['failures'] += 1
                delay = retry_delay(state['failures'])
                reason = 'Service, network, or startup error; delayed retry'
            state.update(status='waiting', next_run_at=time.time() + delay, reason=reason)
            save(status_path, state)
            log('wait_started', delay_seconds=delay, state=state, **context)
            print(f'{reason}. Resuming in {delay // 60} minutes.', flush=True)
            wait_until(state['next_run_at'], stop, heartbeat)
        state.update(status='stopped')
        save(status_path, state)
        log('service_stopped', state=state)
        return 0
    except Exception as error:
        log('supervisor_failed', error=str(error), traceback=traceback.format_exc())
        raise
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=150)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    def terminate(*unused):
        stop.set()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    return run(args.source.expanduser().resolve(), args.output.expanduser().resolve(), stop)


if __name__ == '__main__':
    raise SystemExit(main())
