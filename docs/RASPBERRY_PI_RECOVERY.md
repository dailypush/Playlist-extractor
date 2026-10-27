# Automatic recovery from sustained Wi-Fi failures

The DietPi deployment runs `playlist-extractor-recovery.timer` once per minute,
starting ten minutes after boot. Its service uses the separate
`playlist_extractor.recovery` module. It never opens a recording or reads the
NAS filesystem while checking recovery; it probes numeric LAN addresses and
reads local scanner state and the kernel journal.

## When it reboots

Either condition must persist for at least 15 minutes of observed checks:

- Both the router's ping probe and the NAS SMB port are unreachable.
- The active scanner makes no saved-sample/phase/recording progress, and a
  Wi-Fi/SDIO driver error occurred during that same stalled interval. A scanner
  waiting after a storage failure qualifies; scheduled breaks, finished or
  manually stopped scans, and waits after recognition-service failures do not.

A NAS outage alone, an old warning preceding recent progress, a missing probe
tool, or a stale health snapshot does not independently trigger a reboot.
The watchdog uses its own bounded probes instead of trusting the health
sampler. Gaps over 150 seconds between watchdog checks reset the observed
failure interval. The first ten minutes of boot are a grace period.

An eligible failure requests a normal `systemctl --no-block --no-wall reboot`.
Systemd stops the scanner normally, using its existing shutdown timeout, and
the boot-enabled scanner resumes saved checkpoints after reboot. An in-flight
sample can be retried. This does not force a power cycle or guarantee recovery
from a kernel/SD-card hang that prevents userspace or shutdown from running.

## Reboot limits and evidence

- Minimum six hours between automatic reboot requests.
- At most two requests in a rolling 24-hour window.
- These limits use accumulated measured uptime across boots, conservatively
  excluding unobserved powered-off time. NTP/timezone/wall-clock jumps cannot
  reset the budget, and time spent powered off can make the limit stricter.
- Each request is persisted and fsynced before calling systemctl. Even a failed
  reboot command consumes a request. Invalid state or a failed state write
  prevents automatic reboot.

Root-only state is stored at
`/var/lib/playlist-extractor-recovery/state.json`. Rotating event logs are in
`/var/lib/playlist-extractor-recovery/diagnostics/recovery.jsonl`; the scanner
continues keeping its existing diagnostics. No recognition data is deleted.

## Inspect or disable

```sh
systemctl list-timers playlist-extractor-recovery.timer
journalctl -u playlist-extractor-recovery.service -n 10 --no-pager
cat /var/lib/playlist-extractor-recovery/state.json

# Read-only check: does not persist counters or request reboot.
cd /opt/playlist-extractor
.venv/bin/python -m playlist_extractor.recovery \
  --gateway 192.168.0.1 --nas-host 192.168.0.109

# Disable future automatic checks and stop any check currently running.
systemctl disable --now playlist-extractor-recovery.timer
systemctl stop playlist-extractor-recovery.service
```

Disabling the watchdog does not undo a reboot already requested from systemd.
Preserve `state.json` when disabling/re-enabling it so the reboot budget remains
intact. Change the service's gateway/NAS arguments if LAN addresses change.

## Install and validate

Install the recovery module with the app, and copy
`deploy/raspberry-pi/playlist-extractor-recovery.service` and `.timer` into
`/etc/systemd/system/`. Validate with `systemd-analyze verify`, run the read-only
check above, then run `systemctl daemon-reload` and
`systemctl enable --now playlist-extractor-recovery.timer`. `--allow-reboot`
in the service explicitly arms the policy. The service creates and protects
its own state directory, independently of the scanner account.

Unit tests simulate persistent/recovered outages, progress, normal breaks,
clock changes, reboot cooldowns, budget exhaustion, corrupt state, and failed
persistence/reboot commands. Reboot calls are mocked in tests; validation does
not disconnect Wi-Fi or deliberately reboot a healthy Pi.

Deployment validation on 2026-10-27: all 17 recovery tests passed locally and
on the Pi, the service/timer passed `systemd-analyze verify`, and a live read-only
check reported both LAN probes healthy with the scanner running. The timer
was then enabled. Deployment staging/backups are under
`/var/backups/playlist-recovery-20261027/` on the Pi.
