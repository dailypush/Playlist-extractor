# Long recording batches

Use a persistent batch for a large folder instead of launching parallel scans.
It runs one recording and one Shazam request at a time, retaining the shared
cache and each recording's original checkpoint/JSON/CSV outputs.

```sh
# Inventory total hours and maximum sampling work; no requests or queue writes.
.venv/bin/python -m playlist_extractor batch twitch-pyka --dry-run

# Process/resume, with a cap across the entire invocation (not per recording).
.venv/bin/python -m playlist_extractor batch twitch-pyka --max-requests 500

# Optional time budget, checked between processing steps.
.venv/bin/python -m playlist_extractor batch twitch-pyka --max-requests 500 --max-minutes 60

# Optional background audio preparation; Shazam requests stay sequential.
.venv/bin/python -m playlist_extractor batch twitch-pyka --threaded --max-requests 500
```

The default global cap is 500 new requests per run. Use `--max-requests 0` to
remove it deliberately. Default pacing is three seconds, with no automatic retries
or increase in concurrency. There is no guaranteed safe quota for unofficial
Shazam access. Requests already in flight may finish after a time limit; time
limits are cooperative, not hard process deadlines.
Media probing times out after 60 seconds and each audio extraction after 120
seconds. A timed-out file is marked failed so later recordings can continue;
use `--retry-failed` after checking the file or storage device.

The terminal menu's **8 — Run / resume batch** uses the configured source/output
paths, sample interval, pacing and refinement preference, and asks for the global
request cap. File numbers and queue summaries appear above live per-file progress.

## Optional threaded preparation

`--threaded` uses one background worker to extract and hash the next audio sample
while the main thread checks the cache, waits between requests, recognizes the
current sample and saves results. It keeps at most one sample ahead and processes
one recording at a time. Cache access, requests, progress events and checkpoint
writes remain on the main thread. The three-second request pause and shared
request/time budgets still apply; threading adds no concurrent Shazam requests.

Sequential preparation remains the default, including in Compose, for the
Raspberry Pi Zero 2 target. Enable threading in terminal Settings or launch with
`ui --threaded`. For Docker batches, append `--threaded` to the batch service's
command in compose.yaml. FFmpeg may use its own internal threads; this option
limits preparation jobs, not total OS threads. Performance on the Pi is unverified.

Changing this option reuses the same queue, checkpoints and recognition cache.
Dry runs, export-only and cache seeding do not start preparation workers. On pause
or error, pending preparation is discarded and background FFmpeg is stopped;
only completed recognitions are checkpointed. Some local extraction may therefore
be repeated on resume, without submitting unused prefetched samples to Shazam.
Prefetch errors are raised when that offset is reached, retaining earlier results.

## Queue and recovery

`scan_results/batches/<batch-id>/queue.json` stores recording paths, file metadata,
status, recent sample progress, output folders, and the last run's request/cache
counts. Files transition through pending, running, complete, paused or failed.
The queue uses a stable source-folder/settings identity. Request/time budgets
can change between invocations without creating another queue.

- Run the same command again to resume paused files and skip completed files.
- New files are discovered on each invocation. Changed file size/modification
  time resets that file's queue entry and selects its new scanner checkpoint.
- Unreadable/invalid files are marked failed and skipped. After fixing them,
  use `--retry-failed` to retry unchanged failed entries.
- Service, network or storage errors pause the batch immediately. Inspect the
  error and rerun later; it does not hammer the next file or retry automatically.
- Ctrl+C and SIGTERM pause gracefully. A hard kill/power loss can leave an entry
  marked running; it is treated as pending on the next invocation. Per-sample
  checkpoints and the shared cache limit repeated recognition after interruption.
- Completed entries are checked against their checkpoint and playlist.json.
  Missing, invalid or incomplete exports are revisited to rebuild output using
  saved samples. A damaged checkpoint requires review and is retained.

Each invocation reports completed/failed files, new requests and cache hits. A
normal request/time-budget pause exits 0 with queue status `paused`; errors exit 1,
and interruptions exit 130. Inspect queue status to distinguish completion from a
budget pause. Failed files require review before treating the batch as complete.

An OS file lock prevents scans, exports, cache seeding and batches from writing
to the same output simultaneously. Dry runs do not need the lock.
Do not run multiple output folders against Shazam to multiply throughput. Locking
is supported on macOS/Linux, including Docker. Reports can be rebuilt afterward
with `python -m playlist_extractor report`.

## Docker for unattended work

The Compose batch service has no automatic restart policy: a capped or failing
batch should stop instead of immediately beginning another request allowance.

```sh
docker compose --profile batch up --build -d batch
docker compose logs -f batch

# Gracefully pause; queued results persist on the host.
docker compose stop batch

# Resume the same command and saved queue.
docker compose start batch
```

Edit the batch service command in compose.yaml to change budgets. Both input and
results use the same mounts as the terminal service. Avoid running both services
on the same recordings simultaneously. Host paths and container paths select
different checkpoint/queue identities; choose one environment for ongoing work.
