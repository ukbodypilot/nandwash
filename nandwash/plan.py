"""Plans (PLAN A3/A4): a goal resolved into steps per drive, saved as TOML, re-validated before it runs.

A plan names drives by serial (+ model and size as a cross-check), never by /dev name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

PLAN_VERSION = 1
GOALS = ("check", "refresh", "refresh-full")
# step -> capability it needs (None = none, it writes nothing)
STEPS = {"scan": "check", "pick": "check", "live-free": "live", "rewrite-chunks": "offline-chunks",
         "queue-offline": None, "full-refresh": "full"}
WRITES = ("live-free", "rewrite-chunks", "full-refresh")
DEFAULT_MBPS = 300.0            # used for the estimate when the drive has never been scanned


@dataclass
class DrivePlan:
    serial: str
    model: str
    size: int
    id: str = ""
    name: str = ""              # /dev name when the plan was made: shown, never used to find the drive
    system: bool = False
    steps: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    est_s: Optional[float] = None


@dataclass
class Plan:
    goal: str
    drives: List[DrivePlan] = field(default_factory=list)
    refused: List[tuple] = field(default_factory=list)      # (drive label, why)

    @property
    def writes(self) -> bool:
        return any(s in WRITES for d in self.drives for s in d.steps)


def steps_for(d, goal: str) -> tuple:
    """(steps, notes) for one drive. An empty step list means the goal cannot be done on it (notes say why)."""
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal!r} (one of {', '.join(GOALS)})")
    if not d.can("check"):
        return [], list(d.caps["check"].reasons)
    if goal == "check":
        return ["scan", "pick"], []
    if goal == "refresh-full":
        if d.can("full"):
            return ["full-refresh"], []
        return [], list(d.caps["full"].reasons)
    if d.can("offline-chunks"):
        return ["scan", "pick", "rewrite-chunks"], list(d.caps["offline-chunks"].reasons)
    if d.can("live"):
        notes = list(d.caps["live"].reasons) + ["slow chunks holding data are queued for the offline boot"]
        return ["scan", "pick", "live-free", "queue-offline"], notes
    return ["scan", "pick"], ["nothing can be rewritten now: " + "; ".join(d.caps["live"].reasons +
                                                                          d.caps["offline-chunks"].reasons)]


def _last_scan_rate(serial: str) -> Optional[float]:
    """Bytes/s of this drive's newest complete scan, if any."""
    try:
        import json
        from .preboot import find_scan_report
        p = find_scan_report(serial)
        if not p:
            return None
        r = json.load(open(p))
        if r.get("complete") and r.get("elapsed_s"):
            return r["total_bytes"] / r["elapsed_s"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def estimate(d, steps: List[str], rate: Optional[float] = None) -> Optional[float]:
    rate = rate or DEFAULT_MBPS * 1e6
    t = 0.0
    if "scan" in steps:
        t += d.size / rate
    if "full-refresh" in steps:
        t += 3 * d.size / rate          # read + write + verify, writes are slower
    if "pick" in steps:
        t += 60                          # candidate re-reads: 8 s (sda) .. 40 s (mini sdb)
    if any(s in steps for s in ("live-free", "rewrite-chunks")):
        t += 180
    return t


def make(selection, goal: str, rate_of=_last_scan_rate) -> Plan:
    p = Plan(goal=goal)
    for d, why in selection.refused:
        p.refused.append((f"{d.name} {d.title} ({d.id})", why))
    for d in selection.drives:
        steps, notes = steps_for(d, goal)
        if not steps:
            p.refused.append((f"{d.name} {d.title} ({d.id})", "; ".join(notes)))
            continue
        p.drives.append(DrivePlan(serial=d.serial, model=d.model, size=d.size, id=d.id, name=d.name,
                                  system=d.is_system, steps=steps, notes=notes,
                                  est_s=estimate(d, steps, rate_of(d.serial) if rate_of else None)))
    return p


# ---------- TOML ----------
def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_toml(p: Plan) -> str:
    out = [f"nandwash_plan = {PLAN_VERSION}", f"goal = {_q(p.goal)}", ""]
    for r in p.refused:
        out.append(f"# refused: {r[0]}: {r[1]}")
    if p.refused:
        out.append("")
    for d in p.drives:
        out += ["[[drive]]", f"serial = {_q(d.serial)}", f"model  = {_q(d.model)}", f"size   = {d.size}",
                f"id     = {_q(d.id)}",
                "steps  = [" + ", ".join(_q(s) for s in d.steps) + "]",
                f"# was {d.name}{' (SYSTEM disk)' if d.system else ''}"
                + (f", about {d.est_s / 60:.0f} min" if d.est_s else "")]
        out += [f"# {n}" for n in d.notes]
        out.append("")
    return "\n".join(out)


def from_toml(text: str) -> Plan:
    try:
        import tomllib
    except ImportError as e:                    # Python < 3.11
        raise ValueError("plan files need Python >= 3.11 (tomllib)") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"not a valid plan file: {e}") from e
    if data.get("nandwash_plan") != PLAN_VERSION:
        raise ValueError(f"unsupported plan version {data.get('nandwash_plan')!r} (expected {PLAN_VERSION})")
    goal = data.get("goal")
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal!r}")
    p = Plan(goal=goal)
    for i, r in enumerate(data.get("drive", []), 1):
        for k in ("serial", "model", "size", "steps"):
            if k not in r:
                raise ValueError(f"drive #{i}: missing {k}")
        bad = [s for s in r["steps"] if s not in STEPS]
        if bad:
            raise ValueError(f"drive #{i} ({r['serial']}): unknown steps {bad} (known: {', '.join(STEPS)})")
        p.drives.append(DrivePlan(serial=r["serial"], model=r["model"], size=int(r["size"]), id=r.get("id", ""),
                                  steps=list(r["steps"])))
    return p


def load(path: str) -> Plan:
    with open(path) as f:
        return from_toml(f.read())


# ---------- re-validation (run PLAN) ----------
def validate(p: Plan, drives: list) -> tuple:
    """Match every planned drive again by serial and re-check each step against today's capabilities.
    Returns (pairs, problems): pairs = [(DrivePlan, Drive)] ready to run; problems = human-readable differences."""
    pairs, problems = [], []
    for dp in p.drives:
        hits = [d for d in drives if d.serial == dp.serial]
        label = f"{dp.serial} ({dp.model})"
        if not hits:
            problems.append(f"{label}: not attached now")
            continue
        if len(hits) > 1:
            problems.append(f"{label}: {len(hits)} attached drives report this serial; refusing to guess")
            continue
        d = hits[0]
        if d.model != dp.model or d.size != dp.size:
            problems.append(f"{label}: is now {d.model}, {d.size} bytes (plan: {dp.model}, {dp.size}); not the same drive")
            continue
        bad = []
        for i, s in enumerate(dp.steps):
            cap = STEPS[s]
            if cap and not d.can(cap):
                bad.append(f"{s} needs {cap}: " + "; ".join(d.caps[cap].reasons))
            if s in ("live-free", "rewrite-chunks") and "pick" not in dp.steps[:i]:
                bad.append(f"{s} needs a pick step before it")
        if bad:
            problems.append(f"{label} (now {d.name}): " + " | ".join(bad))
            continue
        dp.name, dp.id, dp.system = d.name, d.id, d.is_system
        pairs.append((dp, d))
    return pairs, problems


def human_s(s: Optional[float]) -> str:
    if s is None:
        return "?"
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"


__all__ = ["Plan", "DrivePlan", "GOALS", "STEPS", "make", "to_toml", "from_toml", "load", "validate", "steps_for",
           "human_s"]
