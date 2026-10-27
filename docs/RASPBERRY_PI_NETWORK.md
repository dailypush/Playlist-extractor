# DietPi Wi-Fi and NAS read caching

## Incident, 2026-10-27

The Pi Zero 2 W became unreachable over SSH until power-cycled. After reboot,
the kernel again reported `mmc1: Controller never released inhibit bit(s)` and
`brcmfmac` SDIO read errors within about three minutes of scanning. Wi-Fi power
saving was already disabled, the existing memory reserves were applied, and
the power-throttling flag was zero. These observations identify a failing
Wi-Fi/SDIO path but do not establish its underlying hardware or driver cause.
Similar failures are tracked in the open
[Raspberry Pi kernel issue #5770](https://github.com/raspberrypi/linux/issues/5770).

The scanner opens the recording again for each sample. The affected recording
is over 13 hours long, and the Pi repeatedly fetched large amounts of data from
the NAS. The original SMB mount negotiated 4 MiB reads and 8 MiB read-ahead.
Reducing repeated transfers and the read burst size is a workload mitigation;
it is not a repair to the Wi-Fi driver.

## Configuration

The Pi's kernel has `CONFIG_CIFS_FSCACHE=y` and `CONFIG_CACHEFILES=m`.
Debian's `cachefilesd` stores reusable file ranges in `/var/cache/fscache` on
the local SD card. Linux manages cache consistency and reclamation. Source
recordings remain on the read-only NAS mount, and source paths stay the same,
preserving recognition checkpoints.

1. Install `cachefilesd`, back up `/etc/cachefilesd.conf`, and install
   `deploy/raspberry-pi/cachefilesd.conf` there. Set `RUN=yes` in
   `/etc/default/cachefilesd`, then enable and start `cachefilesd`.
2. Add `rsize=65536,fsc` to the existing `/mnt/playlist-videos` CIFS entry in
   `/etc/fstab`, preserving its credentials-file reference, read-only access,
   ownership, and automount settings. Do not use `cache=loose`.
3. Install `deploy/raspberry-pi/nas-cache.conf` as
   `/etc/systemd/system/playlist-extractor.service.d/nas-cache.conf`, then run
   `systemctl daemon-reload`. This orders scanner startup after the cache.
4. Stop the scanner gracefully before remounting. With the systemd automounter,
   use its mount unit to stop/start the actual mount; do not race a manual
   `mount` against an automount trigger. Resume scanning after validating the
   active mount.

The active mount should report `rsize=65536,fsc` and remain read-only.
On this kernel, the resulting `/sys/class/bdi/cifs-*/read_ahead_kb` is `128`.
`/proc/fs/fscache/caches` should show an active cache, and
`/proc/fs/fscache/volumes` should include the NAS share. Cache read/write
counters are in `/proc/fs/fscache/stats`; NAS read bytes are in
`/proc/fs/cifs/Stats`. Compare repeated sample extractions and decoded audio
digests, then observe saved scan progress and new kernel warnings under load.

Caching uses SD-card space and writes. The configuration begins culling when
less than 85% of filesystem blocks are available, stops allocations below
80%, and stops culling above 90%. These are free-space thresholds, not a hard
cache-size quota. Do not manually edit files inside the managed cache.
NAS connectivity is still needed for uncached data and coherence checks.
The first uncached sample in the validation below took 72 seconds. Media
probing therefore allows 120 seconds (previously 60), matching the existing
sample-extraction bound, so opening a long recording has more headroom while
the cache fills. The existing recoverable timeout/backoff behavior remains.

## Sample validation

Two extractions of the same previously recognized 12-second sample, followed
by a third after evicting only the source recording's clean NAS pages from
RAM, all produced the same decoded-PCM digest as the saved checkpoint:

| Read | Duration | NAS data bytes |
| --- | ---: | ---: |
| First, cold cache | 72.43 s | 100,293,579 |
| Repeat | 16.40 s | 311,296 |
| After NAS page-cache eviction | 16.72 s | 0 |

FS-Cache read counters increased and reported no cache read/write failures.
These tests made no recognition requests. They demonstrate data reuse and
unchanged decoded audio for this sample, not long-term Wi-Fi reliability.

The subsequent 306-second observation during normal sequential scanning saved
11 new samples (20 to 31) and read 52.13 MiB from the NAS. All 11 health
snapshots reported the NAS reachable; minimum sampled available RAM was
136.9 MiB. The full kernel journal for that observation contained no new
Wi-Fi/SDIO, CIFS, allocation-failure, or OOM messages. Swap/reclaim counters
still increased, so this does not eliminate the Pi's limited RAM or prove
long-term driver stability. Evidence is in `cache-observation.json`,
`cache-sample-test.json`, and `validation-summary.json` in the backup folder.

The scanner and batch test suites passed (31 tests) after the probe-timeout
change. `systemd-analyze verify playlist-extractor.service` passed, and the
service was restarted gracefully to load the change.

## Rollback

Original settings and observations for this deployment are saved on the Pi at
`/var/backups/playlist-network-20261027/`. Stop the scanner, remove `fsc` and
`rsize=65536` from only this mount's fstab entry, remove the `nas-cache.conf`
service drop-in, reload systemd, and remount before restarting the scanner.
Then disable/stop `cachefilesd` if no other mount uses it. The cache can be
discarded after detaching it; it contains no authoritative scan results.
Do not restore the entire old fstab over unrelated later changes.
The probe-timeout change is independent of the mount. Its original source and
installed-package copies are saved as `scanner-0.py.original` and
`scanner-1.py.original` in the same backup directory; reverting that change
requires updating both copies and restarting the scanner gracefully.

The initial live read-ahead-only trial changed `8192` to `128` KiB without a
remount. Its original value is in `read_ahead_kb.original`; a fresh mount without
the smaller `rsize` returns to the negotiated default automatically.

References:

- [Linux CIFS local caching](https://www.kernel.org/doc/html/latest/admin-guide/cifs/usage.html)
- [CacheFiles free-space management](https://kernel.org/doc/html/v5.19/filesystems/caching/cachefiles.html)
