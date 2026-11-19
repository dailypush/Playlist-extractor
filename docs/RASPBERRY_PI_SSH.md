# SSH timeouts during long scans

The DietPi Zero 2 W has 463 MiB usable RAM and accesses recordings over onboard
Wi-Fi. A connection timeout can mean a network/driver failure or a host stall;
it does not by itself prove that the SSH daemon failed. This deployment uses
`dropbear.service`, not `ssh.service`.

## Observations on 2026-11-18

- Prior persisted diagnostics contain repeated media timeouts, intermittent
  loss of both router and NAS connectivity, and one automatic reboot request.
- FFmpeg reached 244,820 KiB RSS in one retained snapshot. Swap and direct
  reclaim counters continue to increase during scanning. These are evidence
  of memory pressure, not proof of the underlying Wi-Fi failure.
- The current boot has no OOM kills or recent Wi-Fi/SDIO kernel errors, Wi-Fi
  power saving is disabled, and the power-throttling flag is zero.
- The existing network recovery timer is enabled. The BCM2835 hardware
  watchdog was present but inactive (`RuntimeWatchdogUSec=0`).
- `/var/log` is tmpfs and the system journal only retained the current boot.
  Pre-NTP boot timestamps can be stale; use boot IDs and monotonic time when
  correlating incidents.

## Hardware watchdog

Install `deploy/raspberry-pi/90-playlist-watchdog.conf` in
`/etc/systemd/system.conf.d/`. `systemctl daemon-reexec` reloads the manager
configuration; it does not intentionally reboot or restart the scanner.
Verify the live values after applying:

```sh
systemctl show -p RuntimeWatchdogUSec
cat /sys/class/watchdog/watchdog0/state
cat /sys/class/watchdog/watchdog0/timeout
```

The configured interval is 15 seconds. Systemd services the hardware timer
at least every half interval. If it cannot keep doing so, the hardware may
reset the machine without a clean shutdown. This is independent of the
network recovery policy and its reboot budget. It cannot detect a Wi-Fi-only
failure while PID 1 remains responsive. An in-flight sample or recent unsynced
writes can be lost on a hardware reset; completed archives and the recognition
cache remain the recovery sources. Do not deliberately hang the host to test it.

## Bounded persistent journal

Install the journal configuration under `/etc/systemd/journald.conf.d/`,
`playlist-journal.tmpfiles` as `/etc/tmpfiles.d/playlist-journal.conf`, and
`playlist-journal.service` under `/etc/systemd/system/`. Check that
`/var/log/journal` is absent or already points to the intended location; do not
replace an unrelated existing journal directory.

The tmpfiles rule creates a link from `/var/log/journal` to
`/var/lib/playlist-system-journal` on disk. DietPi RAMlog preserves symlinks.
The service recreates/verifies the link after RAMlog at boot, then relinquishes
and flushes the journal so an earlier volatile-only boot flush is not reused.
Restart `systemd-journald`, reload systemd, and enable/start
`playlist-journal.service` after installing the files. Other `/var/log` contents
remain in RAM. Storage is bounded to 32 MiB (subject to active-file allocation),
with 8 MiB file limits, a 14-day retention limit, and 30-second synchronization.
The newest unsynced messages may still be lost during a hard reset.

```sh
journalctl --sync
journalctl --directory=/var/lib/playlist-system-journal --list-boots
journalctl -b -1 -k -p warning --no-pager
journalctl -b -1 -u dropbear -u playlist-extractor-recovery --no-pager
```

A healthy short observation does not validate multi-day stability. If failures
continue, inspect the previous boot's kernel and SSH logs before changing
memory/driver settings. A wired USB Ethernet connection can help isolate the
onboard Wi-Fi path; it requires appropriate hardware and separate setup.

## Rollback

The deployment backup is `/var/backups/playlist-ssh-recovery-20261118-01/`.
Remove only the newly installed manager drop-in and run `systemctl daemon-reexec`
to return the runtime watchdog to its prior disabled state. Verify the sysfs
watchdog state is inactive. Keep the existing network watchdog and its state.

For journal rollback, disable/stop `playlist-journal.service`, remove its unit
and the new journald/tmpfiles drop-ins, run `journalctl --relinquish-var`, remove
only the `/var/log/journal` symlink, then reload systemd and restart journald.
Retain the disk journal directory for inspection; no scan results are affected.

References: [systemd hardware watchdog](https://www.freedesktop.org/software/systemd/man/latest/systemd-system.conf.html),
[journal persistence](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html),
[Raspberry Pi Wi-Fi/SDIO issue](https://github.com/raspberrypi/linux/issues/5770).
