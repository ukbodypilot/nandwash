"""Refresh chunks of a drive whose filesystem is MOUNTED READ-WRITE, touching only space the filesystem
reports as free.

Safety argument (every link is enforced in code):
  1. The chunk must lie wholly inside ONE partition holding a supported filesystem (ext4/xfs), mounted.
     Anything else (gaps, bootloader area, swap, unknown or unsupported fs, unmounted partitions) is skipped.
  2. The kernel (FS_IOC_GETFSMAP) must say the chunk's whole byte range is free.
  3. That answer is re-taken with the filesystem FROZEN, so nothing can allocate into the range between
     the check and the write. The freeze has a hard auto-thaw timer and is released after every chunk.
  4. The chunk is read twice (must match), written back with the SAME bytes, synced, and read back.
     Because the bytes are unchanged, even a wrong "free" verdict would destroy nothing.
No filesystem write happens while frozen (backups are written after the thaw, from memory).
"""
from __future__ import annotations

import hashlib
import os
import statistics
import time
from typing import Callable, Optional

from . import drives as drv
from .device import DeviceInfo, DirectDevice
from .fsmap import SUPPORTED_FS, Frozen, encode_dev, free_extents, is_free
from .targeted import _timed_read


def _partition_of(drive: drv.Drive, off: int, size: int):
    for p in drive.partitions:
        if p.start <= off and off + size <= p.start + p.size:
            return p
    return None


def _mount_of(part: drv.Partition) -> Optional[str]:
    """Shortest mountpoint: the least likely to be a container/bind view."""
    return min((m for m, _w in part.mounts), key=len) if part.mounts else None


def classify(drive: drv.Drive, offsets: list, chunk_size: int) -> list:
    """Unfrozen pre-filter. state: free | used | outside | unsupported | unmounted | out-of-range."""
    out, cache = [], {}
    for off in sorted(set(offsets)):
        row = {"offset": off}
        if off % chunk_size or off < 0 or off + chunk_size > drive.size:
            row.update(state="out-of-range", reason="unaligned or past the end of the device")
        else:
            p = _partition_of(drive, off, chunk_size)
            if p is None:
                row.update(state="outside", reason="not inside a single partition (partition table / bootloader / gap)")
            elif p.swap:
                row.update(state="unsupported", partition=p.name, reason="swap partition")
            elif not p.mounted:
                row.update(state="unmounted", partition=p.name,
                           reason="partition not mounted: use `nandwash refresh-chunks` (it is safe offline)")
            elif p.fstype not in SUPPORTED_FS:
                row.update(state="unsupported", partition=p.name, reason=f"filesystem '{p.fstype or 'unknown'}' has no free-space map")
            else:
                mp = _mount_of(p)
                if p.name not in cache:
                    maj, mino = (int(x) for x in p.devnum.split(":"))
                    try:
                        cache[p.name] = free_extents(mp, encode_dev(maj, mino))
                    except OSError as e:
                        cache[p.name] = e
                ext = cache[p.name]
                if isinstance(ext, OSError):
                    row.update(state="unsupported", partition=p.name, reason=f"free-space query failed: {ext}")
                elif is_free(ext, off - p.start, chunk_size):
                    row.update(state="free", partition=p.name, mountpoint=mp)
                else:
                    row.update(state="used", partition=p.name,
                               reason="holds file data or metadata: needs the offline maintenance boot")
        out.append(row)
    return out


def refresh_free(path: str, offsets: list, chunk_size: int = 1048576, backup_dir: Optional[str] = None,
                 dry_run: bool = False, max_chunks: int = 200, pause_s: float = 0.3, max_freeze_s: float = 3.0,
                 log: Callable[[str], None] = print) -> dict:
    info = DeviceInfo.probe(path)
    drive = drv.describe(info.name)
    if drive is None:
        raise ValueError(f"cannot describe {path}")
    if drive.holders:
        raise ValueError(f"{path} is used by device-mapper/md layers ({', '.join(sorted(set(drive.holders)))}); refusing")
    plan = classify(drive, offsets, chunk_size)
    counts: dict = {}
    for r in plan:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    res = {"device": info.path, "serial": info.serial, "dry_run": dry_run, "requested": len(plan), "states": counts,
           "plan": plan, "results": [], "stopped": None}
    todo = [r for r in plan if r["state"] == "free"][:max_chunks]
    if dry_run or not todo:
        return res

    parts = {p.name: p for p in drive.partitions}
    with DirectDevice(info.path, writable=True) as dev:
        for k, row in enumerate(todo):
            off, part = row["offset"], parts[row["partition"]]
            mp = row["mountpoint"]
            maj, mino = (int(x) for x in part.devnum.split(":"))
            r = {"offset": off, "partition": part.name}
            before = _timed_read(dev, off, chunk_size)
            buf = dev.buffer(chunk_size)
            try:
                with Frozen(mp, max_freeze_s) as fz:
                    # ---- nothing in here may write to the frozen filesystem ----
                    ext = free_extents(mp, encode_dev(maj, mino))
                    if not is_free(ext, off - part.start, chunk_size):
                        r["status"] = "skipped: no longer free"
                    else:
                        d1 = bytes(dev.read(off, chunk_size))
                        d2 = bytes(dev.read(off, chunk_size))
                        if d1 != d2:
                            r["status"] = "skipped: two reads differ"
                        elif fz.remaining() < 1.0:
                            r["status"] = "skipped: freeze budget too tight"
                        else:
                            buf[:] = d1
                            dev.write(off, buf)
                            dev.sync()
                            if bytes(dev.read(off, chunk_size)) != d1:
                                buf[:] = d1
                                dev.write(off, buf)
                                dev.sync()
                                r["status"] = "MISMATCH"
                            else:
                                r["status"] = "ok"
                                r["sha256"] = hashlib.sha256(d1).hexdigest()[:16]
                held = fz.held_s
                r["frozen_ms"] = round(held * 1000)
            except OSError as e:
                r["status"] = f"error: {e}"
            if r["status"] == "ok":
                if backup_dir:
                    os.makedirs(backup_dir, exist_ok=True)
                    with open(os.path.join(backup_dir, f"chunk_{off}.bin"), "wb") as f:
                        f.write(d1)
                r["before_ms"] = round(before, 2)
                r["after_ms"] = round(_timed_read(dev, off, chunk_size), 2)
            res["results"].append(r)
            if r["status"] == "MISMATCH" or r["status"].startswith("error"):
                res["stopped"] = f"{r['status']} at {off}; stopped"
                log(res["stopped"])
                break
            if (k + 1) % 10 == 0:
                log(f"{k + 1}/{len(todo)} done")
            time.sleep(pause_s)
    ok = [r for r in res["results"] if r["status"] == "ok"]
    res["chunks_ok"] = len(ok)
    res["median_before_ms"] = statistics.median(r["before_ms"] for r in ok) if ok else None
    res["median_after_ms"] = statistics.median(r["after_ms"] for r in ok) if ok else None
    res["max_frozen_ms"] = max((r.get("frozen_ms") or 0 for r in res["results"]), default=0)
    return res
