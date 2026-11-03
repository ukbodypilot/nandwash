"""Machine-wide state (PLAN A5): /var/lib/nandwash, root-owned.

    aliases.json   reports/   progress/   jobs/<id>/

Before v0.3 reports and progress lived in ~/.nandwash of whoever ran sudo (usually /root, sometimes the
user's home): `import_legacy` copies them here once. Reading needs no root; writing does."""
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


def legacy_dirs() -> list:
    """The pre-v0.3 ~/.nandwash directories that may hold reports/progress, most likely first."""
    out = [os.path.expanduser("~/.nandwash"), "/root/.nandwash"]
    su = os.environ.get("SUDO_USER")
    if su:
        try:
            import pwd
            out.append(os.path.join(pwd.getpwnam(su).pw_dir, ".nandwash"))
        except KeyError:
            pass
    return list(dict.fromkeys(out))


def import_legacy(log=lambda s: None) -> int:
    """Copy old reports and progress into VAR_DIR once (marker file). Copies, never moves: the old files stay
    until the user deletes them. Returns how many files were copied; any failure (read-only root in the
    maintenance boot, not root) is silent and retried next time."""
    import shutil
    marker = os.path.join(VAR_DIR, ".imported-v0.3")
    if os.path.exists(marker) or os.environ.get("NANDWASH_NO_IMPORT") == "1":
        return 0
    n = 0
    try:
        for src in legacy_dirs():
            for sub, dst in (("reports", "reports"), ("", "progress")):
                d = os.path.join(src, sub) if sub else src
                if not os.path.isdir(d):
                    continue
                os.makedirs(os.path.join(VAR_DIR, dst), exist_ok=True)
                for f in sorted(os.listdir(d)):
                    if not (f.endswith(".json") or f.endswith(".lat")) or not os.path.isfile(os.path.join(d, f)):
                        continue
                    t = os.path.join(VAR_DIR, dst, f)
                    if not os.path.exists(t):
                        shutil.copy2(os.path.join(d, f), t)
                        n += 1
        with open(marker, "w") as f:
            f.write("imported from: " + ", ".join(legacy_dirs()) + "\n")
    except OSError:
        return n
    if n:
        log(f"imported {n} files from the old ~/.nandwash into {VAR_DIR}")
    return n
