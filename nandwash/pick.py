"""Choose the chunks worth rewriting: candidates from a scan's latency map, confirmed by re-reading."""
from __future__ import annotations

from typing import Callable, Tuple

from . import report as rpt
from .device import DeviceInfo, DirectDevice
from . import targeted


def pick_slow(scan_report: str, factor: float = 1.8, max_chunks: int = 500, expect_serial: str = "",
              assume_ssd: bool = False, log: Callable[[str], None] = print,
              device: str = "") -> Tuple[dict, DeviceInfo]:
    """Read-only. Returns ({offsets, median_us, threshold_us, candidates}, DeviceInfo).
    `device` overrides the path recorded in the report (names can differ between boots); the serial
    must still match the report."""
    rep, lat = rpt.load(scan_report)
    info = DeviceInfo.probe(device or rep["device"])
    if info.serial != rep["serial"] or (expect_serial and info.serial != expect_serial):
        raise ValueError(f"serial mismatch: device is '{info.serial}', report is '{rep['serial']}'"
                         + (f", expected '{expect_serial}'" if expect_serial else ""))
    med = rpt.percentile(rpt._measured(lat), 50)
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
    return {"offsets": sorted(confirmed), "median_us": med, "threshold_us": thr, "candidates": len(cands),
            "chunk_size": cs}, info
