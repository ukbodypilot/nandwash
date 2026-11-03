"""Per-drive history (PLAN B5): /var/lib/nandwash/history/<short id>.json, one row per finished job drive.

The question it answers is whether fixed chunks stay fixed: every later check looks at the offsets fixed
in earlier runs and counts how many are confirmed slow again, and how long after the fix.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import List, Optional

from . import state

HISTORY_VERSION = 1


def history_dir() -> str:
    return os.path.join(state.VAR_DIR, "history")


def _path(drive_id: str) -> str:
    return os.path.join(history_dir(), re.sub(r"[^A-Za-z0-9._-]", "_", drive_id) + ".json")


def load(drive_id: str) -> dict:
    try:
        with open(_path(drive_id)) as f:
            h = json.load(f)
        if isinstance(h, dict) and isinstance(h.get("runs"), list):
            return h
    except (OSError, ValueError):
        pass
    return {"nandwash_history": HISTORY_VERSION, "id": drive_id, "runs": []}


def add_run(drive_id: str, serial: str, model: str, row: dict) -> dict:
    h = load(drive_id)
    h.update(id=drive_id, serial=serial, model=model)
    # A resumed job finishing the same drive twice must not add two rows.
    h["runs"] = [r for r in h["runs"] if r.get("job") != row.get("job") or not row.get("job")] + [row]
    # Date order: an offline boot is imported after the boot, possibly after a later run was already added.
    h["runs"].sort(key=lambda r: r.get("date") or "")
    os.makedirs(history_dir(), exist_ok=True)
    tmp = _path(drive_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, _path(drive_id))
    return h


def all_drives() -> List[dict]:
    out = []
    try:
        names = sorted(os.listdir(history_dir()))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json"):
            h = load(n[:-5])
            if h["runs"]:
                out.append(h)
    return out


def last_counters(h: dict) -> Optional[dict]:
    for r in reversed(h.get("runs", [])):
        if r.get("smart_counters"):
            return r["smart_counters"]
    return None


def _when(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def fixed_before(h: dict) -> dict:
    """{offset: date of its most recent fix} over all earlier runs."""
    out = {}
    for r in h.get("runs", []):
        for off in (r.get("outcome") or {}).get("fixed_offsets", []):
            out[off] = r.get("date")
    return out


def slow_again(h: dict, confirmed_offsets, now: Optional[datetime] = None) -> Optional[dict]:
    """How many earlier fixes are confirmed slow in this run. None when nothing was ever fixed."""
    fixed = fixed_before(h)
    if not fixed:
        return None
    now = now or datetime.now()
    again = sorted(o for o in confirmed_offsets if o in fixed)
    ages = [(now - d).total_seconds() / 86400 for d in (_when(fixed[o]) for o in fixed) if d]
    again_ages = [(now - d).total_seconds() / 86400 for d in (_when(fixed[o]) for o in again) if d]
    return {"count": len(again), "of": len(fixed), "offsets": again[:1000],
            "days_since_fix_min": round(min(ages), 1) if ages else None,
            "days_since_fix_max": round(max(ages), 1) if ages else None,
            "again_days_median": round(sorted(again_ages)[len(again_ages) // 2], 1) if again_ages else None}
