# Twitch Playlist Extractor

Identify songs in local DJ recordings, review timestamped matches, and compare
recordings. Recognition uses ShazamIO; no API account or credentials are required. Scans resume from checkpoints
and reuse identical audio through a shared SQLite cache.

## Quick start

Launch the interactive terminal app after installation:

```sh
.venv/bin/python -m playlist_extractor ui
```

Select recordings, scan/resume with live progress, browse playlists, rebuild
exports, seed the cache, or generate reports from one menu. Settings include
source/output paths, sample spacing, request pacing and a request cap.
Ctrl+C during recognition pauses the scan and returns to the menu with results
saved. See [Terminal app](docs/TERMINAL.md) for local and Docker usage.

Requires Python 3.11+ and FFmpeg/ffprobe on PATH. On macOS, install FFmpeg with
`brew install ffmpeg` if needed.

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Estimate work without contacting a recognition service.
.venv/bin/python -m playlist_extractor scan twitch-pyka --dry-run

# Scan or resume. Requests are sequential with a three-second pause by default.
.venv/bin/python -m playlist_extractor scan twitch-pyka

# Rebuild exports and reports locally, without recognition requests.
.venv/bin/python -m playlist_extractor scan twitch-pyka --export-only
.venv/bin/python -m playlist_extractor report
```

Add `--max-requests 20` for a limited batch. Source can be a local file or folder.
ShazamIO is unofficial; scans stop on provider errors without automatic retries.
Shazam is the default and only active recognition provider. Existing local
config files are no longer read. Historical checkpoints remain available for reports.

The original `python scan_streams.py ...` and `python session_report.py ...`
commands still work. Optional installation with `pip install -e '.[shazam]'`
also provides `playlist-scan`, `playlist-report` and `playlist-ui` commands.

## Results

`scan_results/<recording>-<identity>/` holds `playlist.json`, `playlist.csv`, `observations.csv`,
and the resumable `checkpoint.json`. The playlist groups versions of a song,
retains alternate titles, and flags uncertainty. Timestamps indicate detections,
not precise song boundaries. A completed sampling run can still miss tracks.

`playlist.json` bundles the songs with the source MP4 path and filename, recording
metadata, sampling settings and result counts for use by other applications.

The existing full scan snapshot is in `scan_results/full-session/`. Reports go
to `scan_results/reports/`; each source file currently represents one recording,
not necessarily an entire Twitch broadcast. Session metadata and play-level
analytics are future work. The SQLite database currently stores recognition
cache entries, not the proposed sessions/songs/plays schema.

## Project layout

```text
playlist_extractor/   Scanner, cache, song grouping/exports, reports
 tests/              Offline regression tests
 docs/               Scanning, caching, reporting, and development guides
 legacy/             Archived prototype and old setup files
 twitch-pyka/        Local input recordings (ignored by Git)
 scan_results/       Checkpoints, CSV exports, cache and reports (ignored)
```

Recordings, results, credentials and virtual environments stay out of Git and
Docker build contexts. The legacy prototype is retained for reference and is
not used by the active commands.

- [Scanning guide](docs/SCANNING.md)
- [Cache and reporting design](docs/REPORTING_AND_CACHE.md)
- [Long batches and unattended Docker runs](docs/BATCHES.md)
- [Development and Docker](docs/DEVELOPMENT.md)

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests are offline and do not consume recognition quota.
