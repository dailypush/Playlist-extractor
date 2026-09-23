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

from .scanner import save


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


def run(source, output, stop):
    output.mkdir(parents=True, exist_ok=True)
    status_path = output / 'unattended.json'
    state = dict(pid=os.getpid(), source=str(source), status='starting', failures=0)
    try:
        previous = json.loads(status_path.read_text())
        if previous.get('source') == str(source):
            state['failures'] = previous.get('failures', 0)
            if previous.get('status') == 'waiting':
                # Preserve a service backoff if the Pi reboots during the pause.
                remaining = max(0, previous.get('next_run_at', 0) - time.time())
                if remaining:
                    state.update(status='waiting', next_run_at=previous['next_run_at'], reason=previous.get('reason'))
                    save(status_path, state)
                    stop.wait(remaining)
    except (OSError, ValueError, TypeError):
        pass
    child = None
    try:
        while not stop.is_set():
            started = time.time()
            state.update(status='running', next_run_at=None, updated_at=started)
            save(status_path, state)
            command = [sys.executable, '-m', 'playlist_extractor', 'batch', str(source),
                       '--output', str(output), '--max-requests', '500']
            child = subprocess.Popen(command)
            while child.poll() is None:
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
            if result == 'finished':
                state.update(status='finished', next_run_at=None, failures=0)
                save(status_path, state)
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
            print(f'{reason}. Resuming in {delay // 60} minutes.', flush=True)
            stop.wait(delay)
        state.update(status='stopped', next_run_at=None)
        save(status_path, state)
        return 0
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
