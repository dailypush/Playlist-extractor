"""Per-invocation timings; kept separate from resumable recognition evidence."""
from contextlib import contextmanager
import resource
import sys
import time


class ScanMetrics:
    def __init__(self):
        self.started = time.perf_counter()
        self.started_at = time.time()
        self.seconds = dict(probe=0.0, extraction=0.0, preparation_wait=0.0,
                            recognition=0.0, pacing=0.0, cache=0.0,
                            checkpoint=0.0, export=0.0)
        self.prepared_samples = 0

    @contextmanager
    def measure(self, stage):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[stage] += time.perf_counter() - started

    def snapshot(self, *, requests, cache_hits, resumed):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return dict(schema_version=1, started_at=self.started_at,
                    elapsed_seconds=round(time.perf_counter() - self.started, 6),
                    seconds={key: round(value, 6) for key, value in self.seconds.items()},
                    prepared_samples=self.prepared_samples, requests=requests,
                    cache_hits=cache_hits, resumed_samples=resumed,
                    cache_hit_rate=cache_hits / (requests + cache_hits) if requests + cache_hits else None,
                    process_peak_rss_mib=round(peak / (1024 ** 2 if sys.platform == 'darwin' else 1024), 2))
