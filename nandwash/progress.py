"""Save/resume state in ~/.nandwash/<serial>.json."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

STATE_DIR = Path(os.path.expanduser("~")) / ".nandwash"


def state_path(serial: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", serial)
    return STATE_DIR / f"{safe}.json"


def save(serial: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
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
