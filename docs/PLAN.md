# nandwash end-state plan: from toolkit to application

Status: draft 2026-11-03. Covers **A. picking drives** (decided enough to build) and **B. results** (outline;
to be settled next). Today nandwash is a set of expert commands (`scan`, `pick-slow`, `refresh-free`,
`refresh-chunks`, `preboot`, `compare`) that someone has to chain correctly. The end state: the user picks
**drives** and a **goal**; nandwash works out the steps per drive, shows the plan, runs it, and reports.

The expert commands stay. The new layer is glue over them, not a rewrite: `engine.run()` already yields an
`EngineState` per chunk, and `drives._judge()` already decides what each drive allows.

## Principles

1. **Read-only by default.** The default goal is `check`. Writing needs an explicit `refresh` goal and a confirmation.
2. **Identify drives by serial, never by `/dev` name.** Names move (the mini's Thunderbolt dock, reboots, hotplug).
   Each drive is re-identified right before it is touched.
3. **Always show the plan before running it**, including what is refused and why.
4. **One job per machine, one drive at a time.** Latency is the evidence; concurrent I/O on a shared controller corrupts it.
5. **Every long run survives a lost terminal** and can be resumed.
6. **No new heavy dependencies.** Keep it to `rich` + `click`; the interactive picker is a table plus typed selection, not a TUI framework.

---

## A. Picking drives

### A1. Drive identity

`DriveId = (serial, model, size_bytes)`. The serial comes from `device.read_serial` (sysfs → udev → VPD 0x80).
Add `wwid` (`/sys/block/X/device/wwid`) when present; it is more unique than the serial on some USB bridges.

- When a selected drive is touched, the `DriveId` is matched again. A mismatch skips that drive with a reason;
  it never falls back to the `/dev` name.
- USB: if two drives report the same serial (cheap bridges do), both are marked `ambiguous-id`, and writing to them is refused.

### A2. What each drive can do (capability)

Make `drives._judge()` produce a structured result instead of a mode string:

| capability | meaning | example reasons |
|---|---|---|
| `check` | can be scanned (read-only) | every non-rotational block device we can open |
| `live` | free-space chunks can be rewritten while mounted | rw-mounted ext4/xfs, kernel >= 5.10 |
| `offline-chunks` | chunks can be rewritten because nothing has it mounted rw | unmounted or ro-mounted |
| `offline-boot` | in-use chunks can be rewritten via the maintenance boot | `preboot check` passes for this drive |
| `none` | nothing | rotational, dm/md/LVM holder, ambiguous id |

Each capability carries `reasons: list[str]` for both yes and no ("live: no, NTFS has no free-space map").
The picker and `nandwash list` print these reasons. `--json` emits them for scripts.

### A3. Goals and how they resolve per drive

| goal | resolves to, per drive |
|---|---|
| `check` | scan → health verdict. Nothing is written. |
| `refresh` | scan → pick-slow → rewrite what can be rewritten now (`live` free chunks, or `offline-chunks` if unmounted) → rescan only the touched chunks → verdict. Chunks that hold data on a mounted fs are **queued** for the offline boot, never silently dropped. |
| `refresh --full` | whole-surface rewrite (today's `refresh`): unmounted drives only. |

The resolved per-drive steps are the **plan**. A plan row reads like:
`sdb  Micron M500  13080900A1B2  refresh → scan, pick, live-free (est. 4 chunks), 9 chunks queued for offline boot`.

### A4. Selecting drives

**Selector grammar** (CLI `--drives`, plan files, the interactive prompt; comma-separated, applied in order):

```
all                 every drive with capability >= check
sda                 by current name (resolved to its serial immediately; the plan stores the serial)
serial:13080900A1B2
model:*M500*        glob on model
bus:sata|nvme|usb   transport
-sdc  -bus:usb      exclude
```

**System-disk rule:** `check` may include the system disk via `all`. Any *write* goal on the system disk needs
it named explicitly (`sda` or `serial:`), not reached through `all` or a glob. The plan preview marks it `SYSTEM`.

**Interactive (`nandwash` with no arguments, on a tty):**
1. A numbered drive table: name, model, size, serial, bus (+TB), and each capability with a one-word reason.
2. Prompt: `Drives (e.g. 1,3-4 / all):`, then `Goal [check/refresh]:`.
3. The plan preview, then `Run? [y/N]`. For writes to the system disk, the user types the last 4 characters of its serial.

**Scripted:**
```
nandwash run --drives all --goal check
nandwash plan --drives "all,-bus:usb" --goal refresh > plan.toml   # review/edit, then:
nandwash run plan.toml
```

Plan file (TOML; needs `tomllib` → **bump `requires-python` to >=3.11**; 3.10 is EOL):
```toml
nandwash_plan = 1
goal = "refresh"
[[drive]]
serial = "13080900A1B2"
model  = "Micron_M500_MTFDDAK480MAV"
size   = 480103981056
steps  = ["scan", "pick", "live-free"]      # filled by `plan`; editing to a subset is allowed, adding is validated
```
`run plan.toml` re-resolves every drive by serial and re-checks its capabilities. If anything changed since the
plan was written (remounted, gone, now ambiguous), it shows the difference and stops unless `--yes`.

### A5. Jobs: running, progress, resume

- **State moves to `/var/lib/nandwash/`** (root-owned) instead of `~/.nandwash`. This removes the sudo-HOME
  confusion. `jobs/<id>/job.json` holds the plan, per-drive status and step; `reports/<serial>/…` holds results.
  On first run, existing reports from `~/.nandwash/reports` and `/root/.nandwash/reports` are imported.
- **Lock:** `/run/nandwash.lock` (flock). A second `run` refuses and points at `nandwash status`.
- **Foreground by default** with a live rich table, one row per drive: state (queued/scanning/picking/rewriting/
  done/skipped/failed), %, MB/s, ETA, temperature, slow chunks so far. The rest of the job (other drives, total ETA) shows below.
- **`--background`** runs the same job under `systemd-run --unit=nandwash-job-<id>` (falls back to a detached process
  without systemd). `nandwash status` attaches the live view to the running job from any terminal;
  `status --json` returns one snapshot for scripts. Progress lives in `job.json`, updated every few seconds:
  the job writes it and `status` reads it. No sockets, no daemon.
- **Ctrl+C / lost terminal in foreground:** the current drive pauses at a chunk boundary and the job becomes
  resumable. `nandwash resume [<id>]` continues it (builds on the existing per-drive `--resume`).
- `nandwash cancel` stops at the next chunk boundary. Nothing is left half-written: rewrite steps are per chunk, readback-verified.
- **Offline boot integration:** chunks queued for the offline boot are listed at the end of the job, along with
  `nandwash preboot …` as the next step. Results from the maintenance boot are imported into the job on the next normal boot
  (the first `nandwash` invocation, or the optional timer below).

### A6. CLI surface after this

```
nandwash                    interactive: pick → plan → run
nandwash list               drives + capabilities (+ --json)
nandwash plan  [--drives] [--goal]     print/save a plan, write nothing
nandwash run   [PLAN | --drives --goal] [--background] [--yes]
nandwash status [--json]    attach to / snapshot the running job
nandwash resume | cancel
nandwash results [DRIVE]    (part B)
nandwash preboot check|install       unchanged
expert: scan, refresh, refresh-chunks, refresh-free, pick-slow, compare, info
```

### A7. Implementation steps (A)

1. `drives.py`: a `Capability` dataclass; `_judge` fills it, and the old `modes` string is derived from it. Add `wwid` and `ambiguous-id`. Tests use fake sysfs trees.
2. `select.py`: selector parser + resolver (pure functions, unit-tested), including the system-disk rule.
3. `plan.py`: goal → per-drive steps, estimates (from the last scan if there is one), TOML read/write, the re-validation diff.
4. `jobs.py`: job dir, `job.json` schema, flock, step runner calling the existing functions; resumable.
5. `cli.py`: `plan`, `run`, `status`, `resume`, `cancel`, the interactive flow; `list` shows capabilities.
6. State-dir move + one-shot import of old reports.
7. Acceptance: a loop-device job of 3 drives (check + refresh) run foreground, then the same job `--background` with `status` attached
   from a second shell, killed mid-way and resumed. Run in CI without real hardware.

---

## B. Results (outline, to be settled next)

### B1. Per-drive health card

A verdict sentence first, then the evidence:

| verdict | rule (draft) |
|---|---|
| `healthy` | no chunk above the slow threshold, no read errors |
| `improved` | slow chunks found and rewritten; >= 80% now read within threshold; data verified identical |
| `unchanged` | rewritten, but latency did not move beyond the noise floor (the drive may skip identical writes) |
| `degraded` | read errors / reallocations increased, or slow chunks that rewriting did not fix are growing scan over scan |
| `inconclusive` | not enough data (no control scan, scan interrupted, noise floor unknown) |

Evidence shown: p50/p99/max latency, the slow-chunk count before → after, a latency histogram, the SMART attributes
that changed, write-evidence counters, data hashes match, and chunks still queued for the offline boot.

### B2. Noise floor

A verdict needs to know how noisy the drive's latency is. Options: (a) require a control scan pair once per drive;
(b) re-read a fixed sample of chunks N times and use their spread. **Lean: (b)**, since it is cheap and automatic.

### B3. History per serial

`reports/<serial>/index.json` holds one row per run (date, goal, p50, p99, slow count, errors, verdict).
`nandwash results sdb --history` shows the trend. The optional `nandwash-check.timer` (monthly `run --goal check
--drives all`) feeds it. Decay over time is the most useful thing this tool can show the community.

### B4. Output formats

- terminal card (`results`)
- `--json` (stable schema, versioned)
- `report --html`: one self-contained file with inline SVG charts and no external assets, for attaching to forum posts and issues

### B5. Open questions for B

- Exact slow threshold (today: pick 1.8x median; compare >3x median and >1 ms). One definition, used everywhere.
- Do we email/notify at all, or leave that to the timer's journal?
- Should results be anonymised by default in `--html` (serial hashed) for public sharing?

---

## Release cut

- **v0.3:** part A complete (check + live refresh + jobs/status/resume), state in `/var/lib/nandwash`, Python >= 3.11,
  CI with loop-device tests, `pipx install --global` documented.
- **v0.4:** part B (cards, history, HTML, timer); offline boot `preboot install` enabled for real once proven on .140 + the mini.
- **Before either:** NVMe test on real hardware (all testing so far has been SATA).
