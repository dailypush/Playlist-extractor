"""Small, rotating diagnostic histories stored alongside durable scan results."""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time

from . import health


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def process_info(pid, proc=Path('/proc')):
    """Inspect only the worker and its children; never touch the NAS mount."""
    rows = []
    pending = [str(pid)] if pid else []
    while pending and len(rows) < 16:
        current = pending.pop(0)
        folder = proc / current
        try:
            status = dict(line.split(':', 1) for line in (folder / 'status').read_text().splitlines() if ':' in line)
            rows.append(dict(pid=int(current), name=status['Name'].strip(),
                             state=status['State'].strip(), wchan=(folder / 'wchan').read_text().strip()))
            pending.extend((folder / 'task' / current / 'children').read_text().split())
        except (OSError, KeyError, ValueError):
            continue
    return rows


def snapshot(queue_path, child_pid=None):
    queue = read_json(queue_path)
    run = queue.get('last_run', {})
    current = run.get('current')
    entry = queue.get('files', {}).get(current, {})
    return dict(last_run=run, source=current, progress=entry.get('progress'),
                error=entry.get('error'), processes=process_info(child_pid),
                health=health.read())


def record(output, kind, *, channel='batch', **fields):
    """Logging failures must never interrupt recognition or its checkpoints."""
    try:
        folder = output / 'diagnostics'
        folder.mkdir(parents=True, exist_ok=True)
        row = dict(time=datetime.now(timezone.utc).isoformat(), monotonic=time.monotonic(),
                   pid=os.getpid(), event=kind, **fields)
        try:
            row['boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            pass
        # Separate writers avoid cross-process rotation races. At most 8 MiB
        # per channel, including three backups; no media or credentials logged.
        handler = RotatingFileHandler(folder / f'{channel}.jsonl', maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding='utf-8', delay=True)
        try:
            handler.handle(logging.LogRecord('diagnostics', logging.INFO, '', 0,
                                             json.dumps(row, ensure_ascii=False), (), None))
        finally:
            handler.close()
    except (OSError, ValueError, TypeError):
        pass
