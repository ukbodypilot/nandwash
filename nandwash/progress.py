"""Save/resume state in /var/lib/nandwash/progress/<serial>.json (+ .lat)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

STATE_DIR: Optional[Path] = None      # override (tests); default: state.VAR_DIR/progress


def state_dir() -> Path:
    from . import state
    return Path(STATE_DIR) if STATE_DIR else Path(state.VAR_DIR) / "progress"


def state_path(serial: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", serial)
    return state_dir() / f"{safe}.json"


def save(serial: str, state: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    p = state_path(serial)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def load(serial: str) -> Optional[dict]:
    p = state_path(serial)
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete(serial: str) -> None:
    try:
        state_path(serial).unlink()
    except OSError:
        pass


# --- latency sidecar (float32 per chunk, microseconds; 0 = not measured) -------------------
def latency_path(serial: str) -> Path:
    return state_path(serial).with_suffix(".lat")


def save_latency(serial: str, arr) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    p = latency_path(serial)
    tmp = p.with_suffix(".lat.tmp")
    with open(tmp, "wb") as f:
        arr.tofile(f)
    os.replace(tmp, p)


def load_latency(serial: str, n: int):
    from array import array
    arr = array("f", [0.0]) * n
    try:
        with open(latency_path(serial), "rb") as f:
            raw = array("f")
            raw.fromfile(f, n)
            arr = raw
    except (OSError, EOFError):
        pass
    return arr


def delete_latency(serial: str) -> None:
    try:
        latency_path(serial).unlink()
    except OSError:
        pass
