"""SMART polling via `smartctl -j`."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class SmartSnapshot:
    temperature: Optional[int] = None
    reallocated: Optional[int] = None       # ID 5
    pending_ecc: Optional[int] = None       # ID 197
    uncorrectable: Optional[int] = None     # ID 198
    power_on_hours: Optional[int] = None    # ID 9
    lifetime_remaining: Optional[int] = None  # ID 202 (Micron)
    # every ATA attribute: {id: {"name", "value", "raw"}} (vendor-specific, diffed generically)
    attributes: Optional[Dict[int, dict]] = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["attributes"] = {str(k): v for k, v in (self.attributes or {}).items()}
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["SmartSnapshot"]:
        if not d:
            return None
        snap = cls(**{k: d.get(k) for k in cls.__dataclass_fields__})
        snap.attributes = {int(k): v for k, v in (d.get("attributes") or {}).items()}
        return snap


def smartctl_available() -> bool:
    return shutil.which("smartctl") is not None


def poll_smart(device_path: str) -> Optional[SmartSnapshot]:
    if not smartctl_available():
        return None
    try:
        # smartctl uses exit-status bits for warnings; ignore returncode, parse JSON
        proc = subprocess.run(["smartctl", "-j", "-A", device_path],
                              capture_output=True, text=True, timeout=20)
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None

    snap = SmartSnapshot(attributes={})
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    for attr in table:
        aid = attr.get("id")
        raw = (attr.get("raw") or {}).get("value")
        if raw is None:
            continue
        snap.attributes[aid] = {"name": attr.get("name"), "value": attr.get("value"), "raw": raw}
        if aid == 194:
            snap.temperature = raw & 0xFF
        elif aid == 5:
            snap.reallocated = raw
        elif aid == 197:
            snap.pending_ecc = raw
        elif aid == 198:
            snap.uncorrectable = raw
        elif aid == 9:
            # some vendors pack milliseconds into the high bits ("Power_On_Hours_and_Msec")
            snap.power_on_hours = raw & 0xFFFFFFFF if raw > 0xFFFFFFFF else raw
        elif aid == 202:
            # Micron: normalised value = % lifetime remaining (100 - raw % used)
            snap.lifetime_remaining = attr.get("value")
    temp = (data.get("temperature") or {}).get("current")
    if temp is not None:
        snap.temperature = temp
    nvme = data.get("nvme_smart_health_information_log") or {}
    if nvme and snap.lifetime_remaining is None and "percentage_used" in nvme:
        snap.lifetime_remaining = 100 - nvme["percentage_used"]

    if all(getattr(snap, k) is None for k in snap.__dataclass_fields__ if k != "attributes") and not snap.attributes:
        return None
    return snap
