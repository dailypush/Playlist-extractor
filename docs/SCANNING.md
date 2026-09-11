# Scan local DJ recordings

The scanner uses ShazamIO, with Python 3.11+ and FFmpeg/ffprobe. No API key,
account or config file is required. Install with `pip install -r requirements.txt`.

```sh
# Interactive terminal
.venv/bin/python -m playlist_extractor ui

# Estimate work without contacting Shazam
.venv/bin/python scan_streams.py twitch-pyka --dry-run

# Limited batch, or omit the cap to scan the full recording
.venv/bin/python scan_streams.py twitch-pyka --max-requests 20

# Rebuild exports from saved results without recognition or FFmpeg
.venv/bin/python scan_streams.py twitch-pyka --export-only
```

Source can be one recording or a folder searched recursively. Rerun the same
command to resume. The previous `--provider shazam` argument is accepted for
compatibility but is no longer needed. Other providers and `--config` are not
supported. Historical checkpoints and local config files are preserved; config
files are not read by the active scanner.

## Sampling and pacing

Samples are 12 seconds of mono audio every 45 seconds, extracted directly with
FFmpeg without decoding the full recording into memory. A five-hour recording
gets 400 baseline checks. A second pass samples midpoints when adjacent grouped
songs differ or either sample is unmatched, adding at most 399 checks.

Requests are sequential with a three-second pause by default. Use `--delay` to
adjust pacing, `--interval` for sampling density, or `--no-refine` for baseline
checks only. `--max-requests` caps new requests per recording per invocation.
Checkpoint reuse and exact-cache hits do not consume that limit.

Add `--threaded` to prepare one sample ahead in a single background worker while
recognition remains sequential. The option defaults off and does not change
checkpoint identity or the request delay. It also works with the `batch` command;
see [threaded preparation](BATCHES.md#optional-threaded-preparation) for resource
limits and cancellation behavior.

ShazamIO uses an unofficial service interface and submits locally generated
fingerprints. Requests have a 45-second timeout and no automatic HTTP retries.
Service errors stop the operation; completed samples are retained. Ctrl+C also
keeps saved results. Avoid concurrent scans of the same source/output folder.

## Review files

Each source gets a folder under `scan_results` with:

- `playlist.json`: playlist plus its source recording path, filename, file size,
  modification time, known duration, scan settings and summary counts. Each song
  includes numeric detection timestamps, review status and alternate versions.
- `playlist.csv`: one row per normalized song/artist, first/last detections,
  evidence counts, alternate version titles, ISRC when supplied and review status.
- `observations.csv`: individual sample timestamps, original candidate matches
  and explicit unmatched samples.
- `checkpoint.json`: working recognition results and progress, kept while a scan
  is incomplete. After completion its metadata is verified in `playlist.json`
  before this redundant working file is removed.

JSON is generated automatically on scan completion, pause/error export, and
`--export-only`. `schema_version` identifies the export format. Missing metadata
is `null`; scores and counts retain their numeric types, and versions/timestamps
are arrays. Source paths refer to the environment that performed the scan (host
or container); the file itself is not embedded. Paths can become stale if files
are moved. File size and modification time are provenance, not a content hash.

Schema version **2** adds `scan_state`, containing the original checkpoint's
identity, sampling settings, all per-offset results (including no-matches), and
provenance with exact-audio digests and provider/cache origins. This preserves
provider track IDs, scores, ISRCs and any returned URLs without flattening them
into the grouped playlist. Completed JSONs can independently support resume,
cache seeding and regeneration of CSVs. Reports can read them without the MP4.

Older checkpoints still load normally. Rebuild exports to migrate them without
recognition calls: `python -m playlist_extractor scan twitch-pyka --export-only`.
Completed checkpoints are removed only after the new JSON is successfully
written and its embedded state verified. Incomplete checkpoints remain; a failed
export retains its working checkpoint. Schema-1 exports without their original
checkpoint cannot restore the missing raw evidence. Back up completed JSONs;
they become the authoritative per-video records. The shared recognition cache
and batch queue remain separate because they cover multiple recordings.

Explicit remix/mix/edit/version labels are grouped; distinct artists and meaningful
suffixes such as `(Part Two)` remain separate. The most frequently detected title
supplies the displayed title and ISRC. Alternate titles remain in `versions_detected`.
Version-only changes do not trigger extra refinement checks.

Shazam scores stay blank. `supported` means at least two detections;
`review` means isolated evidence; `review_versions` flags conflicting versions.
These are not manual confirmations. The legacy `confident_detections` column is
retained for export compatibility and remains zero for unscored Shazam matches.

Detection times are not exact song boundaries. Repeat plays of the same song
share one playlist row, with their evidence retained in observations. Sampling
can miss short appearances, edits, overlaps or songs absent from the catalog.
The tail after the last scheduled sample can remain unsampled.

Changing source path, size, modification time, interval or sample length creates
a separate checkpoint. Existing Shazam checkpoint identities and cache namespaces
are unchanged by the removal of other providers.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use mocked recognition responses and do not consume service requests.
