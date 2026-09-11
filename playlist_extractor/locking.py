"""Serialize writers to an output directory on macOS and Linux."""
from contextlib import contextmanager
import fcntl


@contextmanager
def output_lock(output):
    output.mkdir(parents=True, exist_ok=True)
    # Keep the existing filename so older batch processes share this lock.
    with (output / '.batch.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another scan or batch is using this output folder') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
