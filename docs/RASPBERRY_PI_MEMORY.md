# DietPi memory headroom

The Pi Zero 2 W reported repeated `GFP_ATOMIC` page-allocation failures in
`brcmf_sdio_dataworker` while reading recordings over SMB. The Wi-Fi driver
could not allocate incoming packet buffers. Other Wi-Fi I/O errors and SMB
timeouts were also logged; memory pressure is a likely contributor, not proof
of the cause of every outage.

The target has approximately 463 MiB usable RAM. Its original settings were
`vm.min_free_kbytes=2740` and `vm.watermark_scale_factor=10`.
The deployment configuration in
`deploy/raspberry-pi/90-playlist-memory.conf` raises the minimum free-memory
target to 16 MiB and the watermark scale factor to 100 (1%). This makes
background reclamation start earlier, leaving more headroom for network bursts.
It trades some application/page-cache capacity for free-memory headroom.
Swap capacity alone does not ensure immediate packet-buffer allocations succeed.

These values are specific to this workload and machine. Linux documents these
controls in its [VM sysctl reference](https://docs.kernel.org/admin-guide/sysctl/vm.html).
Do not apply them indiscriminately to other hosts.

## Apply and observe

Back up the live values, test with `sysctl -w`, and observe normal scanning
before making the change persistent. Check `/proc/meminfo`, `/proc/vmstat`,
scanner progress, and the kernel journal for allocation failures, Wi-Fi errors,
SMB reconnects, and OOM events. A short clean observation does not establish
long-term stability.

After validating the target, install the configuration as
`/etc/sysctl.d/90-playlist-memory.conf` and apply it with:

```sh
sysctl -p /etc/sysctl.d/90-playlist-memory.conf
```

No reboot, remount, or scanner restart is required. Keep sequential scanning
and the existing network-error backoff enabled.

## Rollback on the current DietPi deployment

The original live settings and diagnostic snapshots are saved in
`/var/backups/playlist-memory-20260924/`. To restore the original behavior,
remove `/etc/sysctl.d/90-playlist-memory.conf`, then run:

```sh
sysctl -p /var/backups/playlist-memory-20260924/original.conf
```

The observation report is saved as `observation.json` in the same backup
directory. No recording, recognition cache, or checkpoint needs to be changed.

## Initial observation, 2026-09-24

A five-minute observation during normal sequential scanning completed with:

- 12 additional samples saved (baseline count 115 to 127).
- No new Wi-Fi/SMB errors, page-allocation warnings, or OOM kills.
- No direct-reclaim allocation stalls in the VM counters.
- Minimum sampled free memory of 35,728 KiB and available memory of 185,752 KiB
  (ten-second sampling; shorter dips may not be captured).
- 2,270 pages swapped out, approximately 8.9 MiB with the target's 4 KiB pages.

The settings were persisted after this observation. This is an initial workload
check, not a long-duration stability test or a controlled before/after benchmark.
