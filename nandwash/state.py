"""Machine-wide state (PLAN A5): /var/lib/nandwash, root-owned. Aliases live here; jobs and reports follow."""
from __future__ import annotations

import json
import os
import re

VAR_DIR = os.environ.get("NANDWASH_STATE", "/var/lib/nandwash")
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")
RESERVED = ("all",)


def _alias_path() -> str:
    return os.path.join(VAR_DIR, "aliases.json")


def load_aliases() -> dict:
    """{alias: short id}. Missing or unreadable file = no aliases (reading never needs root)."""
    try:
        with open(_alias_path()) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def set_alias(alias: str, drive_id: str) -> dict:
    if not ALIAS_RE.match(alias) or alias in RESERVED or alias.startswith("nw-") or ":" in alias:
        raise ValueError(f"bad alias {alias!r}: start with a letter; letters, digits, . _ - only; "
                         "not 'all', not nw-...")
    a = load_aliases()
    a[alias] = drive_id
    _save(a)
    return a


def remove_alias(alias: str) -> bool:
    a = load_aliases()
    if a.pop(alias, None) is None:
        return False
    _save(a)
    return True


def _save(a: dict) -> None:
    os.makedirs(VAR_DIR, exist_ok=True)
    tmp = _alias_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(a, f, indent=1, sort_keys=True)
    os.replace(tmp, _alias_path())
