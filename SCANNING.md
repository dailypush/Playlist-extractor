# Scan local DJ recordings

## Duplicate handling and quick exports

The playlist groups the same normalized song title and artist into one row,
including explicit remix, mix, edit, version and remaster labels. Different
artists and meaningful title suffixes such as `(Part Two)` stay separate.
This is a song-level playlist: distinct remixes may share a row. The most
frequently detected title supplies the displayed title and ISRC; all returned
titles remain in `versions_detected`. Conflicting versions get `review_versions`
instead of an automatic confirmation. Each sample counts at most once per song.
The timestamped observations preserve the original responses for review.

Version-only changes no longer trigger extra transition samples. Baseline
coverage is unchanged, so this saves requests without widening the sampling gaps.
Rebuild exports from existing results without extracting samples or contacting Shazam:

```sh
.venv/bin/python scan_streams.py twitch-pyka --provider shazam --export-only
```

Use the same source and sampling settings as the original scan. This works on
partial checkpoints too and does not use any recognition quota.

## Shazam option

Install ShazamIO in a separate environment (Python 3.10+, tested here with 3.11):

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-shazam.txt
.venv/bin/python scan_streams.py twitch-pyka --provider shazam --max-requests 20 --delay 2
```

Shazam mode does not use ACRCloud credentials or quota. It uses ShazamIO's
unofficial API to submit locally generated audio fingerprints to Shazam.
Repeat the command to scan the next batch, or omit `--max-requests` for the full
recording. ACRCloud remains available with `--provider acrcloud` (the default).
Each provider uses separate checkpoints, so their results cannot be mixed.

Shazam does not supply an ACRCloud-style confidence score. Score fields stay
blank; `supported` means the same Shazam track appeared in at least two samples.
`confident_detections` counts only scored ACRCloud matches and remains zero for
Shazam. Single detections remain marked `review`. Repeat detections do not
establish that a particular remix/version is correct. Extra sampling checks
changes in grouped songs or no-matches. Requests have a 45-second timeout,
with no automatic HTTP retries; rerun after network or rate-limit errors.

`scan_streams.py` is the new scanner. The original `video_to_playlist.py` is
preserved. Python 3.9+ and FFmpeg (including ffprobe) are required; the new
scanner uses only Python's standard library, so the old requirements are unnecessary.

## Start with a recording

Estimate work without credentials, uploading audio, or making recognition requests:

```sh
python3 scan_streams.py "/path/to/recording.mp4" --dry-run
```

The scanner reads the existing `config.ini` `[secrets]` section with `ACCESS_KEY`
and `ACCESS_SECRET`. Alternatively set `ACRCLOUD_ACCESS_KEY` and
`ACRCLOUD_ACCESS_SECRET` in your environment. Set `ACRCLOUD_HOST` to your project's
recognition hostname if it differs from `identify-eu-west-1.acrcloud.com`.
Use an ACRCloud project with the ACRCloud Music database enabled.

Try a limited scan first (each request sends a short audio sample to ACRCloud
and uses your account's recognition quota):

```sh
python3 scan_streams.py "/path/to/recording.mp4" --max-requests 20
```

Scan an entire folder, including subfolders:

```sh
python3 scan_streams.py "/path/to/recordings" --output scan_results
```

Rerun the same command to resume. Completed samples are saved after every
response, including no-match responses. Network/API errors stop the run and
remain eligible for retry. An interruption between a response and saving its
checkpoint can cause that one request to repeat. Avoid running two scanners
against the same source/output simultaneously.

## Review the results

Each source gets its own folder under `scan_results`:

- `observations.csv`: all candidate matches in sample order, with timestamps,
  scores and explicit unmatched samples. Multiple returned matches are retained.
- `playlist.csv`: unique tracks in first-detected order, with first/last detection,
  evidence counts, best score, ISRC when available, and review status.
- `checkpoint.json`: cached recognition results for resuming and rebuilding exports.

`supported` means at least two samples scored 80 or above for that track.
Other tracks are marked `review`. Scores are provider scores, not probabilities;
even supported tracks can be wrong. First/last detection times are evidence
locations, **not song boundaries**. A song played again later is one playlist
row; its individual appearances remain visible in observations.

## Sampling and long recordings

The default is 12 seconds of mono audio every 45 seconds, extracted directly
with FFmpeg without decoding the full recording into memory. A five-hour file
gets 400 baseline requests. A second pass samples the midpoint between adjacent
baseline checks when their confident candidate sets differ or either is missing.
This adds at most 399 requests for a five-hour file. Extra checks help investigate
transitions and no-matches; they do not guarantee complete track coverage.

Use `--interval 30` for denser coverage or `--no-refine` for baseline checks only.
`--max-requests` caps new calls **per recording per invocation**, including extra
checks. `--delay` controls seconds between requests. Files run sequentially to
bound memory and API traffic. Clips shorter than the sample length are rejected;
the final fragment shorter than a full sample is skipped.

Changing source path, size, modification time, interval, sample length or host
creates a separate checkpoint. Changing `--min-score` reuses saved responses and
rebuilds review statuses, with additional refinement where necessary.

DJ overlaps, speech, muted sections, tempo/pitch changes, edits and tracks absent
from the recognition catalog can produce missing or incorrect matches. Validate
on a representative recording before processing the collection. This version
exports CSV files and does not download Twitch VODs or publish streaming playlists.

## Tests

```sh
python3 -m unittest -v test_scan_streams.py
```

The tests use fake recognition responses; they do not consume API quota or
measure recognition accuracy on real music.
