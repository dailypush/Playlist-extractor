# Reporting and local recognition

## Available now

Every new scan consults `scan_results/recognition-cache.sqlite3` before sending
a sample to its provider. The key is a SHA-256 digest of decoded PCM audio and
its format. Container metadata and source filenames do not affect it. Provider
and recognizer version namespaces prevent accidental cross-provider reuse.
The database stores digests, results and timestamps, not audio or credentials.
Use `--cache PATH` to share a database across different output folders.

Successful matches persist. No-match results expire in the shared cache after
seven days; errors are never cached. Existing per-recording checkpoints still
skip all completed offsets (including old no-matches). Each newly completed
offset records whether its result came from the provider or the exact cache.
Per-run output reports external requests and cache hits. Local cache hits do
not consume `--max-requests`; the scanner stops at the next cache miss when
that limit is reached.

Seed the database from existing matched samples without contacting a provider:

```sh
.venv/bin/python scan_streams.py twitch-pyka --provider shazam --seed-cache
```

Seeding reuses saved audio digests where available, avoiding repeated decoding.
Older matched samples without digests are decoded once and their digests saved.
It does not replace an existing cached identification.

Generate reports without recognition requests:

```sh
python3 session_report.py
```

The reports contain recording coverage, candidate song frequency across
recordings, and pairwise playlist overlap. Currently a source file is one
recording, not necessarily an entire Twitch session. They use one checkpoint
per source path/size/modification-time combination (the one with most samples),
so replacing an MP4 does not hide the new recording behind an older, longer scan.
The recording CSV includes that file metadata. Recording IDs now incorporate
this metadata; path-only legacy checkpoints retain their old IDs. Reports include
partial/uncertain detections and cannot establish accurate play counts or true
absences. Touching or renaming a file can still create another recording identity;
stable Twitch session IDs remain necessary to reconcile such copies.

## Next: approximate acoustic fingerprints

**An exact PCM digest is not a song fingerprint.** A differently encoded copy,
a different excerpt, changed gain, DJ overlays, tempo or pitch adjustments will
usually miss this cache. Never skip a lookup merely because that song title has
appeared before: the current audio has to be identified first.

For approximate local recognition, benchmark [Panako](https://github.com/JorenSix/Panako),
which targets time/pitch transformations, against
[audfprint](https://github.com/dpwe/audfprint), which matches acoustic landmarks
with consistent time offsets. Neither is integrated yet. Both need an indexed
reference library; a known 12-second excerpt cannot match every other part of
the song. Expand references with clean, confidently identified excerpts and
retain recording, offset, provider ID and version ambiguity as provenance.

Start in shadow mode: compare local suggestions against service results without
skipping calls. Test wrong matches as well as missed matches, including different
remixes and tracks sharing samples. Only let well-supported local matches bypass
the provider after measuring reliability on multiple sessions. Do not automatically
promote uncertain transition audio into a reference for future song labels.

## Next: session patterns

Add stable Twitch session IDs and metadata (DJ, date, theme, split-file offsets),
reviewed song identities and time ranges before drawing session-level conclusions.
Then report recurring favorites, newly introduced tracks, songs frequently played
together, repeated transitions and changes by month/theme. Count appearances per
session, not recognition samples; distinguish partial coverage and unknown gaps.
