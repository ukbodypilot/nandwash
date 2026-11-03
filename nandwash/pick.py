"""Choose the chunks worth rewriting: candidates from a scan's latency map, confirmed by re-reading."""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from . import health, report as rpt
from .device import DeviceInfo, DirectDevice
from . import targeted


def pick_slow(scan_report: str, factor: float = 1.8, max_chunks: int = 500, expect_serial: str = "",
              assume_ssd: bool = False, log: Callable[[str], None] = print,
              device: str = "", noise_n: int = 0) -> Tuple[dict, DeviceInfo]:
    """Read-only. Returns ({offsets, median_us, threshold_us, candidates}, DeviceInfo).
    `device` overrides the path recorded in the report (names can differ between boots); the serial
    must still match the report."""
    rep, lat = rpt.load(scan_report)
    info = DeviceInfo.probe(device or rep["device"])
    if info.serial != rep["serial"] or (expect_serial and info.serial != expect_serial):
        raise ValueError(f"serial mismatch: device is '{info.serial}', report is '{rep['serial']}'"
                         + (f", expected '{expect_serial}'" if expect_serial else ""))
    med = health.median_us(lat)
    thr = med * factor
    cs = rep["chunk_size"]
    # Only whole chunks: the disk's last chunk is usually shorter than chunk_size, and refresh-chunks
    # rewrites whole aligned chunks only (a full-size read there fails with a short read).
    whole = info.total_bytes // cs
    cands = sorted((i for i, x in enumerate(lat) if x > thr and i < whole), key=lambda i: -lat[i])
    confirmed = []
    with DirectDevice(info.path, writable=False) as dev:
        for i in cands:
            if len(confirmed) >= max_chunks:
                break
            if targeted._timed_read(dev, i * cs, cs) * 1000.0 > thr:
                confirmed.append(i * cs)
        noise = None
        if noise_n:     # PLAN B2: how far do re-reads of NORMAL chunks drift from the scan on this drive?
            sample = health.noise_sample(lat, med, noise_n, seed=info.serial, whole=whole)
            noise = health.noise_band([lat[i] for i in sample], [targeted._timed_read(dev, i * cs, cs) for i in sample])
    return {"offsets": sorted(confirmed), "median_us": med, "threshold_us": thr, "candidates": len(cands),
            "chunk_size": cs, "noise": noise}, info


def verdict(res: dict, lat, nchunks: int, rep: Optional[dict] = None, prev_counters: Optional[dict] = None) -> dict:
    """Drive state from a pick result + its scan report (PLAN B3, rules in health.drive_state).
    `prev_counters` = SMART error counters from this drive's previous run (history), so a counter that rose
    BETWEEN runs counts as well as one that rose during this scan."""
    rep = rep or {"complete": True}
    cs, med = res["chunk_size"], res["median_us"] or 1
    conf = res["offsets"]
    severe = health.severe_count(conf, lat, cs, med)
    before = health.smart_counters(rep.get("smart_before"))
    after = health.smart_counters(rep.get("smart_after")) or before
    up = health.smart_increases(before, after)
    if prev_counters:
        up += [x for x in health.smart_increases(prev_counters, before) if x not in up]
    st = health.drive_state(complete=bool(rep.get("complete")), error_sectors=len(rep.get("error_sectors") or []),
                            smart_up=up, noise=res.get("noise"), confirmed_n=len(conf), severe_n=severe,
                            nchunks=nchunks)
    return {**st, "confirmed": len(conf), "severe": severe, "chunks": nchunks, "line": health.line(nchunks),
            "share_pct": round(100.0 * len(conf) / nchunks, 4) if nchunks else 0.0,
            "median_ms": round(med / 1000.0, 3), "noise": res.get("noise"), "smart_counters": after}
