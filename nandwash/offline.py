"""Offline boot results -> drive history (PLAN B5).

`nandwash-maint` (the one-shot offline boot) leaves one directory per boot in /var/lib/nandwash-maint/<YYYYmmddTHHMMSS>/:
`log`, `slow.json` (its re-read of the staged live scan), `report.json` (refresh-chunks: every chunk timed before
and after its rewrite), `maint.conf` (from v0.4; which drive and which scan report). Nothing there knows about
history, because the boot runs with a read-only root and no state. The next root `nandwash results`/`report`/`run`/
`status` imports each directory once, as a run with goal `offline` (job id `offline-<ts>`, so a second import
replaces rather than adds).

The files are written by root at boot and read by root here, so a directory or file that anyone else can write is
refused: forged numbers would land in the history (and in "slow again" tracking) as if the drive had produced them.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import stat
from datetime import datetime
from typing import List, Optional, Tuple

from . import health, history as hist, report as rpt

MAINT_DIR = os.environ.get("NANDWASH_MAINT_DIR", "/var/lib/nandwash-maint")
_TS = re.compile(r"^\d{8}T\d{6}$")


def _unsafe(path: str) -> str:
    """Why `path` cannot be trusted ('' = fine): a symlink, owned by someone else, or writable by group/other."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"unreadable ({e.strerror})"
    if stat.S_ISLNK(st.st_mode):
        return "is a symlink"
    if st.st_uid not in (0, os.geteuid()):
        return f"owned by uid {st.st_uid}"
    if st.st_mode & 0o022:
        return f"writable by group/other (mode {stat.S_IMODE(st.st_mode):o})"
    return ""


def _json(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            j = json.load(f)
        return j if isinstance(j, dict) else None
    except (OSError, ValueError):
        return None


def _conf(path: str) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and re.fullmatch(r"[A-Z_]+", k):
                    out[k] = (shlex.split(v) or [""])[0]
    except (OSError, ValueError):
        pass
    return out


def boot_dirs(maint_dir: Optional[str] = None) -> List[str]:
    d = maint_dir or MAINT_DIR
    try:
        return [os.path.join(d, n) for n in sorted(os.listdir(d)) if _TS.match(n)]
    except OSError:
        return []


def read_boot(d: str) -> dict:
    """Everything one boot left behind. Raises ValueError when the directory must not be trusted."""
    bad = _unsafe(d) or next((f"{n}: {w}" for n in ("log", "slow.json", "report.json", "maint.conf")
                              for w in [_unsafe(os.path.join(d, n))] if os.path.lexists(os.path.join(d, n)) and w), "")
    if bad:
        raise ValueError(bad)
    try:
        with open(os.path.join(d, "log")) as f:
            log = f.read()
    except OSError:
        log = ""
    conf = _conf(os.path.join(d, "maint.conf"))
    fix = _json(os.path.join(d, "report.json"))
    m = re.search(r"start: serial=(\S*)", log)
    serial = (fix or {}).get("serial") or conf.get("SERIAL") or (m.group(1) if m else "")
    rc = re.search(r"pipeline done \(rc (\d+)\)", log)
    abort = re.search(r"ABORT: (.*)", log)
    return {"dir": d, "ts": os.path.basename(d), "serial": serial, "device": conf.get("DEVICE_HINT", ""),
            "scan_name": os.path.basename(conf.get("SCAN_REPORT", "")), "slow": _json(os.path.join(d, "slow.json")),
            "fix": fix, "rc": int(rc.group(1)) if rc else None, "abort": abort.group(1).strip() if abort else "",
            "finished": bool(rc)}


def _when(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%dT%H%M%S")


def _scan_for(b: dict) -> Optional[str]:
    """The live scan the boot worked from: the name staged in maint.conf, else (boots from before v0.4 saved
    maint.conf) the newest complete scan of this serial made before the boot."""
    rd = str(rpt.report_dir())
    if b["scan_name"] and os.path.exists(os.path.join(rd, b["scan_name"])):
        return os.path.join(rd, b["scan_name"])
    when = _when(b["ts"])
    best = None
    for p in sorted(glob.glob(os.path.join(rd, glob.escape(b["serial"]) + "-scan-*.json"))):
        m = re.search(r"-scan-(\d{8}T\d{6})\.json$", p)
        j = _json(p) if m and _when(m.group(1)) <= when else None
        if j and j.get("complete"):
            best = p
    return best


def to_row(b: dict, h: dict) -> dict:
    """One history row for one boot. `h` = the drive's history so far (for "slow again" and the combined outcome)."""
    from . import pick
    when = _when(b["ts"])
    slow, fix = b["slow"] or {}, b["fix"]
    med_us = slow.get("median_us") or 0.0
    offsets = slow.get("offsets") or []
    row = {"date": when.isoformat(timespec="seconds"), "job": f"offline-{b['ts']}", "device": os.path.basename(b["device"]),
           "goal": "offline", "steps": [s for s, have in (("pick-slow", b["slow"]), ("refresh-chunks", fix)) if have],
           "offline_dir": b["dir"], "complete": None, "median_ms": round(med_us / 1000, 3) if med_us else None,
           "p99_ms": None, "errors": None, "noise": None, "candidates": slow.get("candidates"),
           "confirmed": len(offsets) if b["slow"] else None, "severe": None, "state": None, "why": "",
           "slow_again": None, "smart_counters": {}, "smart_notes": [], "outcome": None, "next": ""}
    if b["abort"] or not b["slow"]:
        abort = b["abort"].replace(b["serial"], "<serial>") if b["serial"] else b["abort"]
        abort = re.sub(r"/dev/\S+?(?=[;)\s]|$)", "<device>", abort)      # keep `report --share` free of names
        row["why"] = ("offline boot: " + abort) if abort else (
            "offline boot did not finish" if not b["finished"] else "offline boot: the re-read step failed")
        row["next"] = f"see {os.path.join(b['dir'], 'log')}"
        return row
    src = _scan_for(b)
    row["scan_report"] = src
    if src:
        try:
            _rep, lat = rpt.load(src)
            # The live scan's own errors and SMART were judged by the live job; this row is about the re-read.
            v = pick.verdict(slow, lat, len(lat), rep={"complete": True})
            row.update(state=v["state"], why=v["why"], severe=v["severe"], complete=True)
        except (OSError, ValueError, KeyError):
            pass
    oc = health.outcome(health.rewrite_rows(fix), med_us) if fix and med_us else None
    if fix and fix.get("stopped"):
        row["why"] = "; ".join(x for x in (row["why"], f"refresh-chunks stopped: {fix['stopped']}") if x)
    row["outcome"] = oc
    row["slow_again"] = hist.slow_again(h, offsets, now=when)
    prev = next((r for r in reversed(h["runs"]) if r.get("job") != row["job"] and (r.get("date") or "") < row["date"]), None)
    poc = (prev or {}).get("outcome") or {}
    if poc.get("outcome") == "incomplete":       # the live refresh that queued these chunks: report the two together
        row["combined"] = health.combine(poc, oc, with_job=prev.get("job"))
    return row


def pending(maint_dir: Optional[str] = None) -> List[str]:
    """Boot directories not in any history yet (cheap: no parsing beyond the history files)."""
    done = {r.get("job") for h in hist.all_drives() for r in h["runs"]}
    return [d for d in boot_dirs(maint_dir) if f"offline-{os.path.basename(d)}" not in done]


def _drive_for(serial: str) -> Tuple[str, str]:
    """(short id, model) for a serial: from history first (the drive may be detached now), else attached drives."""
    for h in hist.all_drives():
        if h.get("serial") == serial:
            return h["id"], h.get("model", "")
    from . import drives
    for d in drives.enumerate_drives():
        if d.serial == serial:
            return d.id, d.model
    return "", ""


def import_all(maint_dir: Optional[str] = None, log=print) -> List[Tuple[str, dict]]:
    """Import every boot directory not imported yet. Returns [(drive id, row)]. Never raises for one bad directory."""
    out = []
    for d in pending(maint_dir):
        try:
            b = read_boot(d)
        except ValueError as e:
            log(f"offline results {d} not imported: {e}. Check the files, then `sudo chmod -R go-w {d}`"
                if "writable" in str(e) else f"offline results {d} not imported: {e}")
            continue
        if not b["serial"]:
            log(f"offline results {d} not imported: no drive serial recorded")
            continue
        did, model = _drive_for(b["serial"])
        if not did:
            log(f"offline results {d} not imported: drive serial {b['serial']} is not attached and has no history")
            continue
        row = to_row(b, hist.load(did))
        hist.add_run(did, b["serial"], model, row)
        out.append((did, row))
        oc = row.get("outcome") or {}
        log(f"imported offline boot {b['ts']} for {did}: " +
            (f"{oc['fixed']}/{oc['rewritten']} fixed ({oc['outcome']})" if oc else row.get("why") or "nothing rewritten"))
    return out
