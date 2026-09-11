# Twitch Playlist Extractor

Identify songs in local DJ recordings, review timestamped matches, and compare
recordings. ShazamIO and ACRCloud are supported. Scans resume from checkpoints
and reuse identical audio through a shared SQLite cache.

## Quick start

Requires Python 3.11+ and FFmpeg/ffprobe on PATH. On macOS, install FFmpeg with
`brew install ffmpeg` if needed.

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Estimate work without contacting a recognition service.
.venv/bin/python -m playlist_extractor scan twitch-pyka --provider shazam --dry-run

# Scan or resume. Requests are sequential with a three-second pause by default.
.venv/bin/python -m playlist_extractor scan twitch-pyka --provider shazam

# Rebuild exports and reports locally, without recognition requests.
.venv/bin/python -m playlist_extractor scan twitch-pyka --provider shazam --export-only
.venv/bin/python -m playlist_extractor report
```

Add `--max-requests 20` for a limited batch. Source can be a local file or folder.
ShazamIO is unofficial; scans stop on provider errors without automatic retries.
For ACRCloud, use `--provider acrcloud` and configure environment variables or
copy `config.example.ini` to `config.ini` and enter your project credentials.
An existing config.ini should not be overwritten. ACRCloud remains the default
provider for compatibility; specify Shazam explicitly.

The original `python scan_streams.py ...` and `python session_report.py ...`
commands still work. Optional installation with `pip install -e '.[shazam]'`
also provides `playlist-scan` and `playlist-report` commands.

## Results

`scan_results/<recording>-<identity>/` holds `playlist.csv`, `observations.csv`,
and the resumable `checkpoint.json`. The playlist groups versions of a song,
retains alternate titles, and flags uncertainty. Timestamps indicate detections,
not precise song boundaries. A completed sampling run can still miss tracks.

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
- [Development and Docker](docs/DEVELOPMENT.md)

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests are offline and do not consume recognition quota.
