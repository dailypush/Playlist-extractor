# Development

Run commands from the repository root. The active package is `playlist_extractor`:

- `scanner.py`: media extraction, provider calls, resumable scan orchestration/CLI.
- `batch.py`: persistent queues, shared request/time budgets and recovery.
- `locking.py`: shared output lock for scan and batch commands.
- `prefetch.py`: optional single-worker sample preparation with bounded lookahead.
- `catalog.py`: song grouping, refinement planning, CSV exports.
- `cache.py`: provider-scoped exact-PCM recognition cache.
- `reports.py`: recording-level frequency and overlap reports.
- `terminal.py`: interactive workflow and live display consuming scanner events.

Scanner entry points accept an optional `event_handler` callback. Events include
messages, per-phase progress, completion and exported folder paths. The terminal
display refreshes independently while recognition remains sequential. CLI output
and checkpoint identities are unchanged when no callback is supplied.

The small root Python files preserve existing command/import compatibility.
Tests live in `tests/` and use provider mocks. The existing Shazam decoder version
and checkpoint identity fields must stay unchanged unless recognition semantics
change; reorganizing code should not cause a recording to be submitted again.

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m playlist_extractor scan twitch-pyka --dry-run
.venv/bin/python -m playlist_extractor scan twitch-pyka --export-only
.venv/bin/python -m playlist_extractor report
```

Export-only mode needs the source file metadata to locate its checkpoint, but
neither FFmpeg, provider credentials nor ShazamIO. Reports need only checkpoints.
Scan and batch commands reject concurrent writers to the same output folder.
Locks do not coordinate separate output folders sharing a cache. SQLite cache
transactions are independent from checkpoint writes; a completed cached request
can be reused if interrupted before checkpointing.

## Optional installation

`pip install -e '.[shazam]'` installs the package and Shazam dependencies. Plain
`pip install -e .` installs only standard-library functionality. Most development
can run directly from the repository without installing the package itself.

## Docker

```sh
docker build -f dockerfile -t playlist-extractor .
docker run --rm \
  -v "$PWD/twitch-pyka:/media:ro" \
  -v "$PWD/scan_results:/results" \
  playlist-extractor scan /media --output /results --dry-run
```

Remove `--dry-run` to recognize audio with Shazam. No credentials are needed.
Source paths inside Docker
are different from host paths and therefore use separate checkpoint identities;
choose one execution environment for a recording to avoid rescanning it.
The build context allowlist excludes recordings, credentials, results and legacy
files. The image uses Python 3.11 and FFmpeg; container verification requires a
running Docker daemon and network access for dependency installation.

## Raspberry Pi Zero 2 deployment target

The user may run long batches on a Raspberry Pi Zero 2. Keep resource use bounded
and preserve sequential processing as the default. The optional `--threaded`
mode uses one audio-preparation worker and at most one prefetched
sample, while keeping one recognition request in flight and one shared request
budget/pacing mechanism. Avoid parallel recordings, unbounded task submission
and loading whole recordings into memory. Coordinate cache/checkpoint writes
through the main processing thread.

Benchmark extraction, recognition, peak memory and cancellation on the target
before enabling prefetch by default. Account for FFmpeg's own threads and check
whether media timeouts need configuration for slower hardware. Validate the
chosen Pi OS architecture, ShazamIO/native dependency installation and Docker
image on the device before claiming Pi compatibility; that validation is pending.
