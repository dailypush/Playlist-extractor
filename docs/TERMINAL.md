# Terminal app

Run from the project root after following the README installation steps:

```sh
.venv/bin/python -m playlist_extractor ui
```

Or use `.venv/bin/playlist-ui` after `pip install -e .`. No extra UI dependency
is needed. A terminal with at least 80 columns is recommended.

## Background scanner and system health

On DietPi, run `playlist-watch` (or select **9 — Watch background batch**).
The main dashboard includes a health line. Press **h** for memory, free-memory
reserve, swap usage, Wi-Fi signal, NAS SMB-port reachability, and kernel warnings
from the last 15 minutes. Press **h** or **b** to return; **q** closes the monitor
without stopping scanning. Arrows and Page Up/Down scroll the health view.

For a plain snapshot: `playlist-watch --once --health`.

The optional `playlist-extractor-health.timer` refreshes
`/run/playlist-extractor-health/status.json` every 30 seconds. The dashboard only
reads this file; it does not probe the NAS or need elevated journal permissions.
Snapshots older than 90 seconds are labeled **STALE**. Missing or inaccessible
kernel logs are labeled unavailable, not zero warnings. Counts are matching log
messages (up to the latest 200 kernel warnings), not distinct outages.
SMB-port reachability and a present mount do not guarantee successful file reads;
the batch's progress and automatic-retry countdown remain visible separately.

To install the sampler on the documented DietPi deployment, copy
`deploy/raspberry-pi/playlist-extractor-health.service` and `.timer` to
`/etc/systemd/system/`, run `systemctl daemon-reload`, then
`systemctl enable --now playlist-extractor-health.timer`. The service uses the
documented NAS IP; update `--nas-host` and `--mount` for a different deployment.
It runs with journal access, a read-only system filesystem and a writable runtime
directory. No scanner restart is necessary. Elsewhere, use `--health-file PATH`
to read an existing snapshot, or leave the optional sampler uninstalled.

## Workflow

1. Choose Settings if recordings or results are in another folder. Shazam is
   the UI default. Sample spacing defaults to 45 seconds, using 12-second clips,
   with three seconds between requests. A request cap of zero means unlimited.
2. Choose Estimate work to probe a recording without recognition requests.
3. Choose Scan / resume, then a recording (or all recordings). Existing matching
   checkpoints and exact-audio cache results are reused.
4. Watch progress by phase: baseline followed by optional refinement. Each phase
   has its own total. The screen shows elapsed time, sample position, current
   activity, candidate-song count, requests, cache hits and resumed samples.
   New matches appear above the progress display. The elapsed clock refreshes
   while a service response is pending; no completion-time estimate is invented.
5. Browse playlists from the menu. Results are paginated in groups of 20, with
   timestamps and review status. All CSVs remain available in the results folder.

Ctrl+C during a scan returns to the menu after the scanner exports completed
samples. Choose Scan / resume again to continue. A request in progress may have
reached the service before cancellation. Ctrl+C at a menu exits the app.
Provider errors stop the operation; they are not automatically retried.
Settings are in-memory for the current UI session; use command-line source,
output arguments to set launch defaults. Use the scan CLI
for options beyond the menu, such as a custom cache path or sample length.

Settings includes **Prepare the next sample in a background thread**, default off.
It applies to scans and batches, or can be enabled at launch with `ui --threaded`.
One worker prepares at most one sample ahead; Shazam requests retain their delay
and run one at a time. Disabling it again preserves saved progress. Settings changes
last only for the current UI session.

Other menu actions rebuild exports, seed the exact cache and generate recording
reports without recognition requests. Shazam is the only active provider;
there is no account or credential setup.

## Docker

The app needs an interactive terminal (`-it`), a read-only input mount and a
writable results mount. From the repository root:

```sh
docker compose run --rm --build playlist
```

The Compose service opens the menu with `/media` as input and `/results` as
persistent output. Stop it with `q`. Alternatively:

```sh
docker build -f dockerfile -t playlist-extractor:local .
docker run --rm -it \
  -v "$PWD/twitch-pyka:/media:ro" \
  -v "$PWD/scan_results:/results" \
  playlist-extractor:local ui --source /media --output /results
```

Host and container source paths produce different checkpoint identities.
Choose one environment for recognition of a recording to avoid redundant work.
The shared cache may reuse identical audio across those environments, but the
UI does not migrate checkpoint paths. Existing host playlists can still be
browsed from the mounted results folder.

For noninteractive jobs, use the existing `scan` and `report` commands. The UI
exits with a clear message when launched without an interactive terminal.
