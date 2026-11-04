# Changelog

## Unreleased

- `preboot uninstall` now also removes `/etc/nandwash` when it is left empty (it kept the empty folder). A folder
  that still holds other files is left alone.

## 0.4.0 — 2026-11-03

Results: nandwash now says what state a drive is in and whether a refresh helped, and remembers it.

- **Health rules** (`health.py`): slow = above 1.8x the drive's median, in the scan *and* on a re-read. States
  `errors` > `inconclusive` > `slow-regions` > `healthy`. `slow-regions` = at least max(20, 0.01% of chunks)
  confirmed, or 5 or more above 3x (calibrated on five real drives, see docs/PLAN.md B3). Refresh outcomes
  `improved` / `partly` / `no-effect` / `incomplete`. A 200-chunk re-read noise band runs after every scan.
- **SMART 197 that clears is a note, not `errors`**: some drives raise *pending* by one during a heavy read and
  clear it seconds later. 197 is re-polled for up to 120 s after the re-reads; 5/187/198 count at once.
- **History per drive** (`/var/lib/nandwash/history/<id>.json`), including how many earlier fixes are slow again.
- `nandwash results [DRIVE] [--history] [--json]`: health card, one line per drive, versioned JSON schema.
- `nandwash report DRIVE --html [--share]`: one self-contained page (inline SVG histogram, disk map, history; no
  JS, light/dark). `--share` leaves out serial, device names, paths and hostname.
- Exit codes 0/1/2/3 (130 paused) and `run --on-result CMD`. Monthly read-only check timer in `contrib/`.
- **Offline boot results feed the history**: each `/var/lib/nandwash-maint/<ts>/` is imported once by the next
  root command, combined with the live refresh that queued it. Group/other-writable result files are refused.
- `preboot install` is real (was dry-run only): atomic install, GRUB entry written last, `preboot uninstall`.
  Proven on a real system disk (twice); the runner itself also on a second machine (installed by hand).
- The card says a refresh run's slow count is from *before* the rewrite, and how many are still slow after.
- `refresh-free` / `refresh-chunks` run by hand are recorded in the history too.
- Fixes: the offline runner ran with PID 1's umask 0, so its results were world-writable (now `umask 022`, and it
  saves `maint.conf` for the import); `--background` could return before the runner had taken the job; the
  background runner did not inherit `PYTHONDONTWRITEBYTECODE`.

## 0.3.0 — 2026-11-03 (not released separately)

From toolkit to application: pick drives and a goal, nandwash plans and runs it.

- Short drive IDs (`nw-xxxxxx`) from what the drive reports, never `/dev` names; aliases.
- Capabilities per drive with reasons; selectors (`all,-bus:usb,id:…,model:GLOB`).
- Goals `check` / `refresh` / `refresh-full`; `plan` (TOML), `run`, `status`, `resume`, `cancel`, `--background`
  under systemd-run. One drive at a time; the drive is re-identified and re-checked before every write step.
  The system disk needs the last 4 characters of its serial.
- `sudo nandwash` with no command: guided flow.
- State in `/var/lib/nandwash/`; reports from `~/.nandwash` copied once.
- Loop-device acceptance test (run, SIGKILL + resume, SIGTERM pause + resume, cancel).

## 0.2.0 — 2026-11-02 (not released separately)

Targeted refresh.

- Per-chunk read-latency map, full SMART diff, write-evidence counters, `--hash`, `compare`.
- `pick-slow` (confirm by re-reading), `refresh-chunks` (rewrite listed chunks, mount-safety checks).
- `refresh-free`: rewrite free-space chunks **live** on a mounted ext4/XFS, checked against the kernel's
  free-space map with the filesystem frozen.
- `list`: drive enumerator and picker (SATA/NVMe/USB/Thunderbolt).
- Offline maintenance boot runner (PID 1, read-only root, drive found by serial) and `preboot check`.

## 0.1.0

Whole-drive `scan` and `refresh` with resume, temperature throttling and bad-block isolation.
