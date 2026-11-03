# nandwash

SSD NAND cell refresh utility for Linux, inspired by SpinRite's SSD refresh.

## Why: NAND charge retention decay

NAND flash stores data as electrical charge trapped in cells. That charge slowly leaks away over time, faster at higher temperatures and in cells that have seen more program/erase cycles. Eventually the controller needs heavy ECC and read-retry to recover the data, reads slow down, and finally data can become uncorrectable.

This matters most for **old, lightly-used SSDs**. A drive that is 12 years old but has only ~3,000 power-on hours has a low erase count, so SMART wear indicators look healthy. SMART tracks writes, not calendar age, so it says nothing about how long the data in a given cell has been sitting there decaying. Data that is never rewritten is never refreshed.

## How it works

1. **Reads every sector with `O_DIRECT`**, bypassing the page cache, so each read really hits the NAND. Slow or error-ridden regions show up and the controller's ECC is exercised.
2. In **refresh** mode, each cleanly-read chunk is **written back** to the same location. The SSD's flash translation layer (FTL) programs the data into freshly erased cells, resetting the charge.

Read errors are retried at physical-block (4096 B) granularity to isolate bad blocks. **Data that was not read cleanly is never written back.** Drive temperature is polled via SMART and the run pauses if it exceeds `--max-temp`.

## Install

```bash
pipx install nandwash      # or: pip install nandwash
```

Requires Python 3.10+, root (`sudo`), and optionally `smartctl` (smartmontools) for SMART features. Without it nandwash still works but shows no SMART data or temperature throttling.

## Usage

The quickest way in is `sudo nandwash` with no command. It lists the drives and what it can do on each,
asks which drives and which goal (`check` is read-only; `refresh` rewrites slow chunks), shows the plan,
and runs it.

```bash
nandwash list [-v] [--json]                              # drives, short IDs (nw-xxxxxx), what is possible + why
nandwash plan --drives all,-bus:usb --goal refresh -o p.toml   # what would happen; writes nothing
sudo nandwash run p.toml                                 # or: run --drives sda,id:nw-3f9a2c --goal check
sudo nandwash run --drives all --goal check --background # under systemd-run; survives the terminal
nandwash status [--json]                                 # follow the running (or latest) job
sudo nandwash resume | cancel                            # Ctrl+C / systemctl stop pause; a kill is resumable too
sudo nandwash alias sdc backup-tb                        # name a drive; use the name in --drives
```

Jobs run one drive at a time, check each drive again right before every write, and keep their state in
`/var/lib/nandwash/` (jobs, reports, progress, aliases). Reports from older versions in `~/.nandwash` are
copied there on the first run. Writing to the system disk always asks for the last 4 characters of its serial.

The single-drive commands are still there for experts:

```bash
sudo nandwash list                                      # every attached drive (SATA/NVMe/USB) + what is safe on it
sudo nandwash info    [/dev/sdX]                        # device info + SMART summary
sudo nandwash scan    [/dev/sdX] [--resume]             # read-only health scan
sudo nandwash refresh [/dev/sdX] [--resume] [--max-temp 65]  # read + write back (whole disk, must be unmounted)
```

Leave `/dev/sdX` off and an interactive picker lists the attached drives (the system disk is flagged). USB
enclosures: the serial may belong to the bridge, SMART may need `smartctl -d sat` (tried automatically), and some
enclosures report an SSD as rotational: pass `nandwash --assume-ssd ...` once you are sure it is an SSD.

### Fixing only what is slow

```bash
sudo nandwash scan /dev/sdX                                     # 1. latency map  -> /var/lib/nandwash/reports/
sudo nandwash refresh-free /dev/sdX --from-scan <report.json> --dry-run   # 2. what could be fixed live?
sudo nandwash refresh-free /dev/sdX --from-scan <report.json>             # 3. fix it, live
sudo nandwash scan /dev/sdX && nandwash compare <before.json> <after.json>  # 4. prove it
```

`refresh-free` works on a drive that is **mounted read-write** (ext4 or XFS). Per chunk it (1) asks the kernel's
free-space map (`FS_IOC_GETFSMAP`) whether the whole 1 MiB is free, (2) freezes the filesystem and asks again,
(3) reads it twice, writes the same bytes back, syncs and reads back, (4) thaws (about 100 ms). A hard auto-thaw timer
guards the freeze. Chunks holding file data or metadata are reported and never touched; for those use
`refresh-chunks` on an unmounted/read-only drive (see "Offline maintenance boot" below). Partition tables,
bootloader gaps, swap and other filesystems are always skipped. Needs Linux >= 5.10.

`tests/live_loop_test.sh` stress-tests this on a disposable loop device while a writer churns the filesystem.

With `sudo`, pipx-installed tools may not be on root's `PATH`; use `sudo $(which nandwash) ...`.

### Scan vs refresh

| | scan | refresh |
|---|---|---|
| Reads every sector (O_DIRECT) | yes | yes |
| Writes to the device | **no** | yes, clean chunks only |
| Triggers FTL relocation | no | yes |
| Use for | finding slow/bad regions | countering charge decay |

Progress is saved to `/var/lib/nandwash/progress/<serial>.json` every 60 s (10 s in jobs) and on Ctrl+C; use `--resume` to continue. The file is removed on successful completion. A before/after SMART comparison is printed at the end.

## Is it actually working?

Charge decay is invisible to the host (the drive's ECC hides it), so nandwash measures proxies and tells you plainly when it can't prove anything:

1. **Read latency map.** Every chunk's read time is recorded (`time.perf_counter_ns`; each run saves a report in `/var/lib/nandwash/reports/`). Decayed cells need ECC retries and read from slower. Run `scan`, `refresh`, `scan`, then `nandwash compare first.json last.json` to see whether the slow chunks got faster.
2. **Control run.** Latency is noisy, so do two scans with no refresh between them first; only differences bigger than that are meaningful.
3. **Did the drive rewrite anything?** After a refresh, nandwash diffs the drive's write/erase/program counters and warns if none moved (the drive may have skipped identical writes).
4. **ECC/SMART.** The full SMART attribute table is stored before/after and every changed counter is listed.
5. **Integrity.** `--hash` SHA-256s everything read; `compare` confirms the data is unchanged.

If the drive has no slow regions to begin with, there is nothing for a refresh to visibly improve, and `compare` says so. Note that under `sudo`, `~` is root's home unless your sudo keeps `HOME`; reports and resume state live there.

## Warning

**Refresh mode writes to the device.** A power cut or hardware fault mid-write can lose data. Back up anything critical first. nandwash refuses to refresh a device that has mounted filesystems, refuses rotational drives, and refuses to run without root. Use at your own risk.

## Compared to SpinRite

nandwash is inspired by SpinRite's approach but is Linux-native, SSD-focused (it will not touch spinning disks), and open source (MIT). It is a much smaller tool: it does not attempt data recovery of failing sectors.

## License

MIT

## Offline maintenance boot (preview)

Chunks that hold file data or metadata can only be rewritten with the filesystem not in use. The offline runner
(`nandwash/data/nandwash-maint`) boots the machine once into a minimal PID 1 with the root filesystem read-only,
re-confirms which chunks are persistently slow, rewrites them (timing each one before and after), saves a report
and reboots. The full scan is done **live** beforehand (it is read-only); scanning offline would keep the machine
down far longer. The offline time limits are sized from that scan, and the hardware watchdog deadline is derived from them.

```
sudo nandwash scan DEVICE                        # live, read-only: the latency map the offline boot works from
sudo nandwash preboot check  [DEVICE]            # can this machine do it? verdict + a reason per finding
sudo nandwash preboot install DEVICE --dry-run   # exactly which files / GRUB entry an install would write
```

The newest completed scan of the drive's serial is used, or pass `--from-scan REPORT`. It must be complete and
must match the drive's serial and size. It is copied to `/var/lib/nandwash-maint/input/`. Offline, the drive
is found by serial (device names can change between boots) using SCSI VPD page 0x80, which needs no udev.
The maintenance kernel gets `ro panic=30`, so a crash reboots back to the normal system.

Both are read-only. To install and run it once:

```bash
sudo nandwash preboot install DEVICE              # writes the runner, conf, staged scan and one GRUB entry; no reboot
sudo grub-reboot nandwash-maint && sudo reboot    # one-shot: the boot after it is the normal one again
ls /var/lib/nandwash-maint/                        # one folder per run: log, slow.json, report.json, dmesg.txt
sudo nandwash preboot uninstall                    # remove the entry and files (results are kept)
```

Supported: GRUB with an ext2/3/4 or XFS root
that is a plain partition, nandwash installed on the root filesystem, target drive on the internal bus. Anything
else (systemd-boot, LUKS/LVM/RAID/btrfs/ZFS root, USB/Thunderbolt targets) is reported as unsupported with the
reason. A missing hardware watchdog is a warning: the runner is then bounded by software time limits only.
