# nandwash

Find the slow, decaying regions of an SATA/NVMe SSD on Linux and rewrite just those, without reinstalling,
and prove whether it helped. Inspired by SpinRite's SSD refresh; MIT licensed.

```text
$ sudo nandwash results nw-c8b36a
nw-c8b36a  Micron_M500_MTFD  (sdb)   2026-11-03 14:32, check
STATE    healthy   13 confirmed slow of 678 candidates (3 above 3x), median 2.33 ms, noise 0.90-1.06
SMART    no error counters rose (187=0, 197=0, 198=0, 5=1196032)
         SMART 197 pending 0 -> 1 during the scan, back to 0 after the re-reads (transient)
HISTORY  169 -> 37 -> 29 -> 13 confirmed slow over 4 runs; 1 of 196 earlier fixes slow again
```

(The same drive that morning, before its first refresh: 445 chunks confirmed slow, reads up to 900 ms.)

## Why: NAND charge retention decay

NAND flash stores data as electrical charge trapped in cells. That charge slowly leaks away, faster at higher
temperatures and in cells that have seen more program/erase cycles. The controller then needs heavy ECC and
read-retry to recover the data: reads slow down, and eventually data can become uncorrectable.

This matters most for **old, lightly-written SSDs**. SMART wear indicators track writes, not calendar age, so a
12-year-old drive with few erase cycles looks healthy while the data that was never rewritten sits there decaying.
The symptom you *can* measure is read latency: decayed regions read several times slower than the rest of the drive.

## What it does

1. **Scan** (read-only): reads the whole drive with `O_DIRECT` (no page cache) and records the read time of every
   1 MiB chunk.
2. **Confirm**: chunks slower than 1.8x the drive's median are re-read; only those still slow count. Most scan
   outliers are one-off noise, and the re-read filters them out. The drive's own re-read noise is measured on every
   run, so a drive too noisy to judge is reported as `inconclusive` instead of guessed at.
3. **Rewrite** only the confirmed chunks with their own bytes (read twice, compare, back up, write, sync, read back,
   compare). The SSD's flash translation layer programs them into freshly erased cells.
   - Chunks in **free space** are rewritten **live**, on the mounted filesystem (ext4/XFS), by checking the kernel's
     free-space map with the filesystem briefly frozen (about 100 ms per chunk).
   - Chunks holding **file data or metadata** are queued for a one-shot **offline maintenance boot**: the machine
     boots once into a minimal read-only environment, rewrites them, and reboots back to normal (a few minutes).
4. **Judge and remember**: each chunk is timed before and after its rewrite; the drive gets a health state and
   the refresh an outcome, both kept in a per-drive history that tracks whether earlier fixes go slow again.

**Data that was not read cleanly is never written back**, and every rewrite is verified by reading it back.

## Install

nandwash is not on PyPI yet. Install it from GitHub, system-wide so `sudo` finds it:

```bash
sudo pipx install --global git+https://github.com/ukbodypilot/nandwash.git
# or, into a venv you own:
python3 -m venv /opt/nandwash/venv && /opt/nandwash/venv/bin/pip install git+https://github.com/ukbodypilot/nandwash.git
```

Needs Linux, Python 3.11+, root, and `smartctl` (smartmontools) for SMART data and temperature throttling
(without it nandwash still runs, but shows no SMART evidence). The live free-space mode needs Linux 5.10+.
For the offline boot the package must be installed on the root filesystem (a venv under `/opt` is fine;
`~/.local` on a separate `/home` is not).

## Quick start

```bash
sudo nandwash
```

With no command it lists the drives and what it can do on each (and why not, where it can't), asks which drives
and which goal, shows the plan and runs it. The goals:

| goal | does | writes |
|---|---|---|
| `check` | scan + confirm, gives each drive a state | nothing |
| `refresh` | `check`, then rewrites the confirmed slow chunks: free space live, the rest queued for the offline boot | slow chunks only |
| `refresh-full` | rewrites every readable chunk of an **unmounted** drive | the whole drive |

The typical cycle for one drive:

```bash
sudo nandwash run --drives sdb --goal check            # 1. ~20 min per 500 GB on SATA; read-only
sudo nandwash run --drives sdb --goal refresh          # 2. if the state is slow-regions
sudo nandwash preboot check /dev/sdb                   # 3. if chunks were queued for the offline boot
sudo nandwash preboot install /dev/sdb
sudo grub-reboot nandwash-maint && sudo reboot         #    one boot only; the next is normal again
sudo nandwash results sdb                              # 4. imports the boot's results; then
sudo nandwash preboot uninstall                        #    remove the boot entry
sudo nandwash run --drives sdb --goal check            # 5. in a few weeks: did the fixes hold?
```

Don't re-check straight after a refresh expecting zero: every fresh scan of a worn drive finds a few new
stragglers, and chasing them in circles achieves nothing. The number that matters is how many **earlier fixes
are slow again** after weeks, and the card shows it.

## Results

Every job ends with a verdict per drive:

- **State**:
  - `errors`: unreadable sectors, or a SMART error counter rose (5/187/198, or 197 *pending* still raised after the
    re-reads). Back up first, then look at `smartctl -a`.
  - `inconclusive`: scan incomplete, or the drive's re-read noise is too wide to judge. Re-check when idle and cool.
  - `slow-regions`: at least max(20, 0.01% of chunks) confirmed slow, **or** 5 or more confirmed above 3x the median.
  - `healthy`: otherwise.
- **Outcome** after a refresh: `improved` (at least 80% of the rewritten chunks fixed: back under 1.8x the median
  and at least 30% faster), `partly` (20-80%), `no-effect` (under 20%: the drive may skip identical writes; the
  write/program counters are shown), or `incomplete` (chunks holding data wait for the offline boot).

Some SSDs raise SMART 197 (pending sectors) by one during a heavy read scan and clear it within seconds. nandwash
re-polls it for up to 2 minutes after the re-reads; a rise that clears is shown as a note, not as `errors`.

```bash
nandwash results [DRIVE] [--history] [--json]     # health card per drive, or one line per drive
nandwash report DRIVE --html -o r.html [--share]  # one-file page: card, read-time histogram, disk map, history
```

`--share` leaves out the serial, device names, paths and hostname, so the page is safe to post. The JSON schema
is versioned (`nandwash_result: 1`); fields are only ever added.

`run` exits 0 (healthy/improved), 1 (slow-regions/partly/no-effect/incomplete), 2 (inconclusive), 3 (errors) or
130 (paused). `--on-result CMD` pipes the results JSON into your own command (mail, ntfy, Home Assistant...).
History is kept per drive in `/var/lib/nandwash/history/<id>.json`.

### Monthly check

```bash
sudo cp contrib/nandwash-check.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nandwash-check.timer
```

Read-only, AC power only. Exit 1 (slow regions) counts as success, so only `inconclusive`/`errors` fail the unit;
add `--on-result` to its `ExecStart` to be told either way. It deliberately uses no idle I/O class: queued reads
would look slow and pollute the latency evidence.

## Drives, plans and jobs

```bash
nandwash list [-v] [--json]                                   # drives, short IDs, what is possible + why not
nandwash plan --drives all,-bus:usb --goal refresh -o p.toml  # what would happen; writes nothing
sudo nandwash run p.toml                                      # or --drives ... --goal ...
sudo nandwash run --drives all --goal check --background      # under systemd-run; survives the terminal
nandwash status [--json]                                      # follow the running (or latest) job
sudo nandwash resume | cancel                                 # Ctrl+C pauses; even a kill is resumable
sudo nandwash alias sdc backup-ssd                            # name a drive; use the name in --drives
```

- Drives get a short ID (`nw-xxxxxx`) hashed from what the drive itself reports (WWID or serial, model, size),
  never from `/dev` names, which move between boots and ports.
- Selectors: `all`, `sda`, `id:nw-3f9a2c`, `serial:X`, an alias, `model:GLOB`, `bus:sata|nvme|usb|tb`; `-TERM` excludes.
- Jobs run one drive at a time (concurrent I/O on a shared controller would corrupt the latency evidence), and
  re-identify the drive and re-check what is allowed right before every write step.
- Writing to the **system disk** always asks for the last 4 characters of its serial, even with `--yes`.
- State lives in `/var/lib/nandwash/` (jobs, reports, progress, history, aliases). Reports from versions before
  0.3 in `~/.nandwash` are copied there once.

USB enclosures: the serial may belong to the bridge, SMART may need `smartctl -d sat` (tried automatically), and
some enclosures report an SSD as rotational: pass `nandwash --assume-ssd ...` once you are sure it is an SSD.
nandwash refuses spinning disks.

## Offline maintenance boot

Chunks that hold file data or metadata can only be rewritten while the filesystem is not in use. `preboot install`
writes a small runner (`/usr/local/sbin/nandwash-maint`), its config, a copy of the scan, and **one GRUB entry**
(in `/boot/grub/custom.cfg`; `grub.cfg`, kernels and initramfs are not touched). Nothing happens until you boot
that entry once with `grub-reboot`. In it, the runner is PID 1 with the root filesystem **read-only**:

1. finds the drive by serial (SCSI VPD page 0x80, no udev needed), and aborts touching nothing if that is ambiguous;
2. re-reads the scan's candidates and keeps the ones still slow;
3. rewrites and verifies them, timing each before and after;
4. saves `log`, `slow.json`, `report.json`, `maint.conf`, `dmesg.txt` to `/var/lib/nandwash-maint/<timestamp>/`
   and reboots into the normal system.

Every step has a time limit sized from the live scan, and the kernel gets `ro panic=30`, so a crash reboots back
to normal. A hardware watchdog is used if one is found; without one, `preboot check` warns that only the software
limits protect you.

```bash
sudo nandwash preboot check [DEVICE]             # can this machine do it? verdict + a reason per finding
sudo nandwash preboot install DEVICE --dry-run   # exactly which files / GRUB entry an install would write
```

The next root `nandwash results`/`report`/`run`/`status` imports each boot's results into the drive's history
once (goal `offline`), combined with the live refresh that queued them. Result files that anyone but root can
write are refused (they would be evidence nobody can trust); the message says how to fix the permissions.

Supported: GRUB with an ext2/3/4 or XFS root on a plain partition, nandwash installed on the root filesystem,
target drive on an internal bus. Anything else (systemd-boot, LUKS/LVM/RAID/btrfs/ZFS root, USB/Thunderbolt
targets) is reported as unsupported, with the reason.

## Expert commands

The goals above are built from these, which are still there for scripting and experiments:

```bash
sudo nandwash info    [/dev/sdX]                          # device info + SMART summary
sudo nandwash scan    [/dev/sdX] [--resume]               # read-only latency map -> /var/lib/nandwash/reports/
sudo nandwash pick-slow REPORT.json out.json              # confirm slow chunks by re-reading them
sudo nandwash refresh-free /dev/sdX --from-scan REPORT.json [--dry-run]   # rewrite free-space chunks, live
sudo nandwash refresh-chunks /dev/sdX chunks.json         # rewrite listed chunks (unmounted / read-only only)
sudo nandwash refresh [/dev/sdX] [--resume] [--max-temp 65]   # whole drive, must be unmounted
nandwash compare before.json after.json                   # did the slow chunks get faster? is the data unchanged?
```

Leave `/dev/sdX` off and a picker lists the attached drives (the system disk is flagged). `refresh-free` and
`refresh-chunks` run by hand are recorded in the drive's history too. `--dry-run` on `refresh-free` only
classifies the chunks (free / used / outside a filesystem) and writes nothing. Progress is saved to
`/var/lib/nandwash/progress/` and `--resume` continues; `--hash` SHA-256s everything read so `compare` can prove
the data is unchanged.

## Real-world results so far

All SATA, on Arch Linux. NVMe support is implemented but **not yet tested on real hardware**.

| drive | before | what was done | after |
|---|---|---|---|
| Kingston SV300 120 GB, system disk | median 2.3 ms; slow chunks at 2-2.7x | 335 chunks rewritten, then 60 metadata chunks offline (4.26 -> 2.18 ms) | slow chunks back to normal; untouched control chunks flat |
| Crucial M500 480 GB, system disk, 47,000 power-on hours | **445 confirmed slow** (`slow-regions`), tail to 900 ms | live 274 free + offline 169 used chunks; then 8 live + 29 offline stragglers | `healthy`, 13 confirmed; 195 of 196 fixes still fast the same day |
| Crucial M500 480 GB, M600 1 TB, T-FORCE 512 GB | 1, 0 and 1 confirmed | nothing needed | `healthy` |

What is **not** known yet: how long a fix lasts. That is what the history and the monthly check are for. If you run
nandwash on your drives, `nandwash report DRIVE --html --share` makes a page that is safe to post in an issue.

## Warning

**Refresh writes to the device.** A power cut or hardware fault mid-write can lose data; back up anything critical
first. Safeguards: whole-drive `refresh` refuses mounted drives; `refresh-free` only touches chunks the kernel's
free-space map says are free, re-checked with the filesystem frozen; `refresh-chunks` refuses read-write mounts and
device-mapper/md layers; every write is the chunk's own bytes, read twice and verified after; the system disk needs
its serial typed. Rotational drives and non-root runs are refused. Use at your own risk.

## Testing

```bash
python -m unittest discover -s tests          # unit tests, no root, no hardware (also run in CI)
sudo tests/acceptance_loop.sh "$(which nandwash)"   # jobs end to end on loop devices: run, kill, resume, cancel
sudo tests/live_loop_test.sh                  # refresh-free on a loop device while a writer churns the filesystem
```

Design notes and the reasoning behind every threshold are in [docs/PLAN.md](docs/PLAN.md); changes in
[CHANGELOG.md](CHANGELOG.md).

## Compared to SpinRite

nandwash is Linux-native, SSD-only (it will not touch spinning disks), targeted (it rewrites the slow chunks, not
the whole drive, and can do most of it live), and open source. It does not attempt data recovery of failing sectors.

## License

MIT
