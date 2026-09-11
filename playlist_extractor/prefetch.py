"""Optional single-worker preparation with only one item of lookahead."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event


class SamplePreparation:
    def __init__(self, positions, prepare, threaded=False):
        self.positions = iter(positions)
        self.prepare = prepare
        self.threaded = threaded
        self.stopped = Event()
        self.executor = None
        self.pending = None

    def __enter__(self):
        if self.threaded:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-prefetch')
        return self

    def __next__(self):
        if self.executor is None:
            return self.prepare(next(self.positions), None)
        if self.pending is None:
            self.pending = self.executor.submit(self.prepare, next(self.positions), self.stopped)
        result = self.pending.result()
        self.pending = None
        following = next(self.positions, None)
        if following is not None:
            self.pending = self.executor.submit(self.prepare, following, self.stopped)
        return result

    def __exit__(self, *unused):
        self.stopped.set()
        if self.pending is not None:
            self.pending.cancel()
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
