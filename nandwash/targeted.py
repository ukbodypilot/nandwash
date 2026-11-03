"""Rewrite a specific list of chunks (e.g. the ones a scan flagged slow), with per-chunk verification."""
from __future__ import annotations

import hashlib
import os
import statistics
import time
from typing import Callable, Optional

from .device import DeviceInfo, DirectDevice


def mounts_of(info: DeviceInfo) -> list:
    """Every mount backed by this disk or its partitions: [(source, mountpoint, writable)].
    A mount counts as writable if its SUPERBLOCK is rw (a ro bind mount over a rw fs is still rw)."""
    devnums = set()
    base = f"/sys/block/{info.name}"
    for p in [base] + [os.path.join(base, d) for d in os.listdir(base) if d.startswith(info.name)]:
        try:
            devnums.add(open(os.path.join(p, "dev")).read().strip())
        except OSError:
            pass
    out = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, right = line.rstrip("\n").split(" - ", 1)
            lf, rf = left.split(), right.split()
            if lf[2] in devnums:
                out.append((rf[1], lf[4], "rw" in rf[2].split(",")))
    return out


def holders_of(info: DeviceInfo) -> list:
    """Device-mapper/LVM/md layers sitting on this disk (refuse: we cannot see what uses them)."""
    base = f"/sys/block/{info.name}"
    found = []
    for p in [base] + [os.path.join(base, d) for d in os.listdir(base) if d.startswith(info.name)]:
        try:
            found += os.listdir(os.path.join(p, "holders"))
        except OSError:
            pass
    return found


def _timed_read(dev, off, size, n=3):
    ts = []
    for _ in range(n):
        t = time.perf_counter_ns()
        dev.read(off, size)
        ts.append((time.perf_counter_ns() - t) / 1000.0)
    return statistics.median(ts) / 1000.0  # ms


def refresh_chunks(path: str, offsets: list, chunk_size: int = 1048576, backup_dir: Optional[str] = None,
                   log: Callable[[str], None] = print, should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """Read each chunk twice (must match), optionally back it up, write the same bytes back, then
    read back and compare. On a readback mismatch: restore from the in-memory copy and stop."""
    info = DeviceInfo.probe(path)
    results, stopped = [], None
    with DirectDevice(info.path, writable=True) as dev:
        for k, off in enumerate(sorted(set(offsets))):
            if should_stop and should_stop():
                stopped = "cancelled"
                log("cancelled between chunks")
                break
            if off % chunk_size or off < 0 or off + chunk_size > info.total_bytes:
                results.append({"offset": off, "status": "skipped: out of range/unaligned"})
                continue
            before = _timed_read(dev, off, chunk_size)
            d1 = bytes(dev.read(off, chunk_size))
            d2 = bytes(dev.read(off, chunk_size))
            if d1 != d2:
                results.append({"offset": off, "status": "skipped: two reads differ"})
                log(f"skip {off}: reads differ")
                continue
            if backup_dir:
                with open(os.path.join(backup_dir, f"chunk_{off}.bin"), "wb") as f:
                    f.write(d1)
            buf = dev.buffer(chunk_size)
            buf[:] = d1
            dev.write(off, buf)
            dev.sync()
            if bytes(dev.read(off, chunk_size)) != d1:
                buf[:] = d1
                dev.write(off, buf)
                dev.sync()
                stopped = f"readback mismatch at {off}; restored; stopped"
                results.append({"offset": off, "status": "MISMATCH"})
                log(stopped)
                break
            after = _timed_read(dev, off, chunk_size)
            results.append({"offset": off, "status": "ok", "before_ms": round(before, 2),
                            "after_ms": round(after, 2), "sha256": hashlib.sha256(d1).hexdigest()[:16]})
            if k % 10 == 0:
                log(f"{k + 1}/{len(offsets)} done")
    ok = [r for r in results if r["status"] == "ok"]
    return {"device": info.path, "serial": info.serial, "chunks_requested": len(offsets), "chunks_ok": len(ok),
            "stopped": stopped, "results": results,
            "median_before_ms": statistics.median(r["before_ms"] for r in ok) if ok else None,
            "median_after_ms": statistics.median(r["after_ms"] for r in ok) if ok else None}
