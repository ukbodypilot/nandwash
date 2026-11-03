# nandwash end-state plan: from toolkit to application

Status: draft 2026-11-03. Covers **A. picking drives** and **B. results**, both settled enough to build. Today nandwash is a set of expert commands (`scan`, `pick-slow`, `refresh-free`,
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

**Identity** is built only from what the drive itself reports, which survives reboots, port changes and reformatting:
`(wwid or serial, model, size_bytes)`. The serial comes from `device.read_serial` (sysfs → udev → VPD 0x80), and
`wwid` (`/sys/block/X/device/wwid`) is used when present.

**Short ID for people:** `nw-` + the first 6 hex characters of a SHA-256 of that tuple, e.g. `nw-3f9a2c`.
- It is used in tables, selectors (`id:nw-3f9a2c`), plan files and report filenames.
- It stands in for the serial in shareable reports (answers B5's anonymisation question).
- On a collision, the ID is lengthened.

**Aliases:** `nandwash alias nw-3f9a2c data-m600` saves a user-chosen name in `/var/lib/nandwash/aliases.json`.
Selectors accept the alias too.

**Never part of the identity** (all are shown as the drive's *current location* instead):
- the `/dev/sdX` name: it changes between boots, so history and plans would break
- filesystem labels and UUIDs: they change on reformat, a dd clone copies them, and a disk has several or none

Rules:
- When a selected drive is touched, its identity is matched again. A mismatch skips that drive with a reason;
  it never falls back to the `/dev` name.
- **Same-serial drives (cheap USB bridges):** first tie-break on the GPT disk GUID (it changes only on repartition),
  then on the physical port (`/dev/disk/by-path`). If **only the port** tells them apart, the drives are marked
  `ambiguous-id`. Reads are allowed; writes are refused, because swapping two identical enclosures between ports
  would silently swap the IDs.

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
id:nw-3f9a2c  data-m600    short ID or alias (A1)
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

## B. Results

### B0. What the data says (sda on .140, Kingston SV300 120 GB, 114,474 x 1 MiB chunks)

Two complete scans 33 min apart, nothing written to the drive in between:

| | scan A | scan B | slow in BOTH |
|---|---|---|---|
| > 1.8x median | 1021 | 965 | **392** |
| > 3x median | 10 | 9 | **0** |
| > 5x median | 1 | 2 | **0** |

- Normal chunks (A < 1.5x median) vary **±10%** between scans (B/A ratio p5 0.95, p50 1.01, p95 1.10). The drive median moved 1.4%.
- The extreme outliers were all transient; the persistently slow chunks are the mild ones. **One scan proves nothing per
  chunk: "slow" must mean slow on a re-read.** This validates pick-slow's re-read confirmation and argues against the
  old `compare` rule (>3x and >1 ms), which on this drive selects only noise.
- The first offline run rewrote 60 chunks: 41 were above 1.8x median before; all 60 were below it after, and 58 also dropped by >30%.
  That is what "improved" looks like.

### B1. One definition of slow, used everywhere

- **median** = the drive's p50 chunk latency in this scan.
- **candidate** = a chunk > **1.8x median** in the scan.
- **slow (confirmed)** = a candidate whose re-read (median of 3 reads, taken after the scan) is still > 1.8x median.
  This is what `pick-slow` does today. It moves into a shared `health.py` that `check`, `refresh`, `compare` and `results` all call.
- **fixed** = a rewritten chunk whose after-read is <= 1.8x median **and** at least 30% faster than its before-read.
- The noise band (B2) gates all of this: if the drive's own noise is wider than the threshold, the verdict is `inconclusive`, not a guess.

Consequence: the `check` goal = scan + candidate re-reads. Re-reading is read-only and cheap
(sda: ~1000 candidates x 3 reads x 2 ms = 6 s).

### B2. Noise band: measured every run, automatically

After the scan, re-read **200 random normal chunks** (< 1.5x median) 3 times each and record the spread of
re-read/scan ratios (p5..p95). On sda that is about 0.95..1.10. This costs ~1-2 s, so no control-scan pair is needed.
- If p95 > 1.5 (the drive is too noisy for a 1.8x threshold to mean anything), the verdict is `inconclusive`
  and the card says why (thermal throttling, background GC, a busy system disk).
- Stored with the report, so the history shows whether a drive is getting noisier.

### B3. Verdicts: two separate questions

**Drive state** (every run, including `check`). The first matching row wins:

| state | rule |
|---|---|
| `errors` | any unreadable sector, or SMART reallocated / pending / uncorrectable counts increased since the previous run. Card leads with **back up now**. |
| `inconclusive` | scan incomplete, or noise band too wide (B2) |
| `slow-regions` | confirmed slow >= **max(20, 0.01% of chunks)**, **or** >= **5** confirmed chunks above **3x median**: refresh recommended (card states how many, how slow, and where) |
| `healthy` | otherwise |

`healthy` with a few mild slow chunks shows them as a note ("5 chunks at 1.8-1.9x: within noise of the line"), no advice.

The rule was settled on 2026-11-03 from the data below. The first draft (> 0.1%) put both drives in `healthy`,
although they differ by 25x. With the rule above: sda (5 chunks, all 1.8-1.9x) is `healthy`; mini sdb (445 chunks,
237 above 3x) is `slow-regions`. Both thresholds are config values; re-check them as more drives are measured.

Calibration data, re-read confirmation (pick-slow, median of 3, isolated re-reads unbiased: ratio p50 0.98-0.99,
p95 1.03-1.04, 0/200 false positives on both drives):

| drive | candidates | confirmed | share | notes |
|---|---|---|---|---|
| .140 sda, Kingston SV300 120 GB | 965 | 5 | 0.004% | 392 were slow in BOTH scans of 2026-11-02; only ~64 rewritten since, yet 1 of the 392 is still slow. Cause unverified |
| mini sdb, Crucial M500 480 GB | 4786 | 445 | 0.097% | confirmed scan latency p50 7.6 ms, max 36.7 ms; every >100x read was transient |

- mini sdb by band: 1.8-3x 208/2151 confirmed, 3-5x 134/1370, 5-10x 102/1000, 10-100x 1/253, >100x 0/12.
- mini sdb confirmed chunks by filesystem state (GETFSMAP, read-only): **275 free** (rewritable live), **170 used**
  (150 file data, 1 group metadata, 19 partial). 255 of the 445 lie past 460 GB on a 7%-used disk; 190 of those are free.
- mini sdb refresh, 2026-11-03: live `refresh-free` rewrote the 274 free chunks (273 `fixed`, median 7.41 -> 2.42 ms,
  longest freeze 268 ms); the one-shot offline boot then re-confirmed 169 and rewrote them (168 `fixed`, 7.55 -> 2.47 ms,
  ~3 min away from the network). Re-check against the same scan: **445 -> 3 confirmed**, state `slow-regions` -> `healthy`,
  outcome `improved` (441/443 fixed). SMART 5/187/196/197/198 unchanged.
- Lessons: (1) a scan goes stale within hours, so confirm right after the scan, never from an old report;
  (2) the per-boot 500-chunk cap is redundant with the time budget; (3) the state rule needs count + severity,
  not one percentage (settled above).

**Refresh outcome** (only after a refresh):

| outcome | rule |
|---|---|
| `improved` | >= 80% of rewritten chunks `fixed` |
| `partly` | 20-80% `fixed` |
| `no-effect` | < 20% `fixed`. The card says the drive may skip identical writes, and shows the write-evidence counters. |
| `incomplete` | chunks still queued for the offline boot, listed with the next command to run |

Data integrity is reported separately and always: either "read-back verified identical: N/N", or a MISMATCH that stops the job.

### B4. The health card (terminal: `nandwash results [DRIVE]`)

```
nw-3f9a2c  data-m600  Micron M600 1 TB  (sda, sata)            2026-11-03 14:02, refresh
STATE    slow-regions   1240 confirmed slow chunks (0.13%), median 2.21 ms, noise ±8%
OUTCOME  improved       1141/1240 fixed (92%); slow median 4.30 -> 2.25 ms; 99 queued for offline boot
DATA     read-back verified identical: 1141/1141
SMART    no error counters changed; host writes +1141 MiB, NAND writes +1160 MiB
HISTORY  slow chunks 1610 -> 1240 -> 99 over 3 runs; 14 of 1141 earlier fixes slow again after 30 days
NEXT     sudo nandwash preboot install nw-3f9a2c --dry-run
```
`results` with no argument prints one line per drive (id, alias, state, outcome, date of last run).

### B5. History per drive, and the metric that matters

`/var/lib/nandwash/reports/<id>/index.json` holds one row per run: date, goal, median, p99, noise band,
candidates, confirmed slow, fixed, errors, state, outcome, and the offsets of fixed chunks.

**The key metric is whether fixed chunks stay fixed.** On every later scan, check the previously fixed offsets
and report how many are slow again, and after how many days. That answers the real-world question "how often
should I refresh?". Almost nothing else tells users this, and it's the most useful data we can give back.

`nandwash results DRIVE --history` shows a table plus a terminal sparkline of confirmed-slow count and median.
The optional `nandwash-check.timer` (monthly `run --goal check --drives all`) feeds it.

### B6. Output formats

- **Terminal card** (B4).
- **`--json`**: versioned schema (`"nandwash_result": 1`), the same fields as the card plus the raw numbers. Scripts and the timer use it.
- **`nandwash report DRIVE --html -o file.html`**: one self-contained file with inline SVG (latency histogram
  before/after, slow-chunk map across the disk, history chart), no JavaScript, no external assets.
  - `--share`: replaces the serial with the short ID, drops hostname, mountpoints and filesystem labels,
    and keeps model and firmware (useful to others comparing the same drive model). **Off by default**,
    since a report on your own machine should show the serial.

### B7. Notifications: none built in

No email or chat integrations: each one is a dependency plus credentials to manage.
- `nandwash run` exits with a status code: 0 healthy/improved, 1 slow-regions/partly/no-effect, 2 inconclusive, 3 errors.
- The timer's unit therefore fails visibly in `systemctl --failed` and the journal when a drive has errors.
- `--on-result CMD` runs a user command with the JSON on stdin, so anyone can wire up their own mail, ntfy or Home Assistant.

### B8. Implementation steps (B)

1. `health.py`: candidates, confirm, noise band, fixed, the state and outcome rules (pure functions over latency
   arrays + re-read results; unit-tested on the sda numbers above as fixtures).
2. Wire it into `check`/`refresh` jobs; the 200-chunk noise sample runs after every scan.
3. `index.json` per drive; "slow again" tracking of earlier fixed offsets.
4. `results` (card + one-line list + `--history`), `--json` schema + a test that pins it.
5. `report --html` with inline SVG, plus `--share`.
6. Exit codes, `--on-result`, `nandwash-check.timer` + service shipped in `contrib/`.
7. ~~Calibrate the `slow-regions` line~~ done 2026-11-03 (B3); re-check on the mini's other two drives.

---

## Release cut

- **v0.3:** part A complete (check + live refresh + jobs/status/resume), state in `/var/lib/nandwash`, Python >= 3.11,
  CI with loop-device tests, `pipx install --global` documented.
- **v0.4:** part B (cards, history, HTML, timer); offline boot `preboot install` enabled for real once proven on .140 + the mini.
- **Before either:** NVMe test on real hardware (all testing so far has been SATA).
