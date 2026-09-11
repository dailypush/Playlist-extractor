# Terminal app

Run from the project root after following the README installation steps:

```sh
.venv/bin/python -m playlist_extractor ui
```

Or use `.venv/bin/playlist-ui` after `pip install -e .`. No extra UI dependency
is needed. A terminal with at least 80 columns is recommended.

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
