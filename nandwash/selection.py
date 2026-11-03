"""Drive selectors (PLAN A4): turn text like "all,-bus:usb" or "sda,id:nw-3f9a2c" into a list of drives.

Pure functions over `drives.Drive` objects, so they test without hardware.

    all                 every drive nandwash can at least check
    sda | /dev/sda      current name (the plan stores the serial, never the name)
    serial:XYZ          exact serial
    id:nw-3f9a2c        short ID (a bare nw-... works too)
    NAME                an alias from aliases.json
    model:*M500*        glob on the model (case-insensitive)
    bus:sata|nvme|usb|tb  transport; tb = behind a Thunderbolt dock
    -TERM               exclude what TERM matches

Terms apply left to right. A selector that starts with an exclusion starts from `all`.
System-disk rule: a WRITE goal reaches the system disk only when it is named explicitly
(name, serial, id or alias), never through `all`, `model:` or `bus:`.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

EXPLICIT = ("name", "serial", "id", "alias")


class SelectorError(ValueError):
    pass


@dataclass
class Selection:
    drives: list = field(default_factory=list)          # chosen, in drive order
    refused: List[Tuple[object, str]] = field(default_factory=list)   # (drive, why), shown in the plan preview
    explicit: set = field(default_factory=set)          # ids named explicitly


def parse(text: str) -> List[Tuple[bool, str, str]]:
    """'all,-bus:usb' -> [(False, 'all', ''), (True, 'bus', 'usb')]  (exclude?, kind, value)."""
    out = []
    for raw in (t.strip() for t in (text or "").split(",")):
        if not raw:
            continue
        neg = raw.startswith("-")
        t = raw[1:].strip() if neg else raw
        if not t:
            raise SelectorError(f"empty term in {text!r}")
        if t == "all":
            out.append((neg, "all", ""))
        elif ":" in t and t.split(":", 1)[0] in ("serial", "id", "model", "bus"):
            k, v = t.split(":", 1)
            if not v:
                raise SelectorError(f"'{t}' needs a value")
            out.append((neg, k, v))
        elif t.startswith("/dev/"):
            out.append((neg, "name", t[5:]))
        elif t.startswith("nw-"):
            out.append((neg, "id", t))
        else:
            out.append((neg, "word", t))          # a current name or an alias, decided against the drives
    if not out:
        raise SelectorError("no drives selected (empty selector)")
    return out


def _bus(d, v: str) -> bool:
    v = v.lower()
    if v in ("tb", "thunderbolt"):
        return bool(d.thunderbolt)
    return d.transport == v


def _match(d, kind: str, value: str, aliases: Dict[str, str]) -> bool:
    if kind == "all":
        return True
    if kind == "name":
        return d.name == value
    if kind == "serial":
        return d.serial == value
    if kind == "id":
        return d.id == value or (len(value) > 3 and d.id.startswith(value))
    if kind == "alias":
        return aliases.get(value) == d.id
    if kind == "model":
        return fnmatch.fnmatch(d.model.lower(), value.lower())
    if kind == "bus":
        return _bus(d, value)
    raise SelectorError(f"unknown selector kind {kind}")


def resolve(drives: list, text: str, aliases: Optional[Dict[str, str]] = None, write: bool = False) -> Selection:
    aliases = aliases or {}
    terms = parse(text)
    if terms[0][0]:
        terms.insert(0, (False, "all", ""))
    chosen: Dict[str, object] = {}
    explicit: set = set()
    for neg, kind, value in terms:
        if kind == "word":
            kind = "name" if any(d.name == value for d in drives) else "alias" if value in aliases else ""
            if not kind:
                raise SelectorError(f"'{value}' is neither a drive name nor an alias "
                                    f"(drives: {', '.join(d.name for d in drives) or 'none'})")
        hits = [d for d in drives if _match(d, kind, value, aliases)]
        if kind == "id" and len({d.id for d in hits}) > 1:
            raise SelectorError(f"id:{value} matches several drives: {', '.join(d.id for d in hits)}")
        if not hits and kind in EXPLICIT and not neg:
            raise SelectorError(f"no attached drive matches {kind}:{value}")
        for d in hits:
            if neg:
                chosen.pop(d.id, None)
                explicit.discard(d.id)
            else:
                chosen[d.id] = d
                if kind in EXPLICIT:
                    explicit.add(d.id)
    sel = Selection(explicit=explicit)
    for d in drives:
        if d.id not in chosen:
            continue
        if not d.can("check"):
            if d.id in explicit:
                sel.refused.append((d, "; ".join(d.caps["check"].reasons) or "not eligible"))
            continue                                # `all` quietly passes over disks there is nothing to do with
        if write and d.is_system and d.id not in explicit:
            sel.refused.append((d, "SYSTEM disk: a write goal needs it named explicitly (e.g. "
                                   f"{d.name} or serial:{d.serial})"))
            continue
        sel.drives.append(d)
    return sel
