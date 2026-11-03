"""Run reports and before/after analysis (latency map, SMART diff, write evidence)."""
from __future__ import annotations

import json
import os
import re
from array import array
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__
from .engine import EngineState
from .progress import STATE_DIR

REPORT_DIR = STATE_DIR / "reports"


# ---- latency statistics ---------------------------------------------------------------
def _measured(lat) -> list:
    return sorted(x for x in lat if x > 0)


def percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def latency_stats(lat) -> dict:
    v = _measured(lat)
    if not v:
        return {"chunks": 0}
    return {"chunks": len(v), "p50": percentile(v, 50), "p90": percentile(v, 90),
            "p99": percentile(v, 99), "p999": percentile(v, 99.9), "max": v[-1],
            "mean": sum(v) / len(v)}


def slowest(lat, chunk_size: int, n: int = 10) -> list:
    idx = sorted((i for i, x in enumerate(lat) if x > 0), key=lambda i: -lat[i])[:n]
    return [{"offset": i * chunk_size, "us": lat[i]} for i in idx]


# ---- write evidence ---------------------------------------------------------------------
_NAME_HINTS = [
    ("host writes", re.compile(r"(total_?lbas?_?written|lifetime_writes|host_writes|host_program)", re.I)),
    ("NAND/FTL programs", re.compile(r"(ftl_program|nand_writes|program_page|flash_writes|nand_?program)", re.I)),
    ("erase count", re.compile(r"(erase_?count|avg_?erase|wear_?leveling_?count|wear_range)", re.I)),
]


def attribute_diff(before, after) -> list:
    """Every attribute that exists in both snapshots: (id, name, before_raw, after_raw, delta)."""
    rows = []
    if not before or not after or not before.attributes or not after.attributes:
        return rows
    for aid in sorted(set(before.attributes) & set(after.attributes)):
        b, a = before.attributes[aid], after.attributes[aid]
        name = a.get("name") or b.get("name") or ""
        mask = 0xFFFFFFFF if re.search("msec", name, re.I) else 0xFF if re.search("temp|airflow", name, re.I) else None
        if mask:  # vendors pack extra fields (ms, min/max temps) into the high bits
            a = {**a, "raw": a["raw"] & mask}; b = {**b, "raw": b["raw"] & mask}
        try:
            d = a["raw"] - b["raw"]
        except (TypeError, KeyError):
            d = None
        rows.append((aid, a.get("name") or b.get("name") or "?", b.get("raw"), a.get("raw"), d))
    return rows


def write_evidence(before, after, written_bytes: int) -> list:
    """Did the drive itself register the rewrite? Returns human-readable lines."""
    rows = attribute_diff(before, after)
    if not rows:
        return ["No SMART attribute table: cannot confirm the drive registered the rewrite."]
    out, found, moved = [], False, False
    for label, rx in _NAME_HINTS:
        for aid, name, b, a, d in rows:
            if rx.search(name or "") and d is not None:
                found = True
                moved = moved or bool(d)
                out.append(f"{label}: {name} (ID {aid}) {b} -> {a} ({d:+d})")
    if not found:
        out.append("This drive exposes no recognisable write/erase counters; success can only be "
                   "judged from latency and ECC changes (see `nandwash compare`).")
    elif written_bytes and not moved:
        out.append("WARNING: counters did not move although data was written back; the drive may "
                   "have skipped identical writes, so cells may not have been refreshed.")
    return out


# ---- save / load ------------------------------------------------------------------------
def build(st: EngineState, info, interrupted: bool) -> dict:
    lat = st.latency_us if st.latency_us is not None else array("f")
    return {
        "nandwash": __version__, "created": datetime.now().isoformat(timespec="seconds"),
        "mode": st.mode, "serial": info.serial, "model": info.model, "device": info.path,
        "total_bytes": st.total_bytes, "chunk_size": st.chunk_size, "complete": st.finished and not interrupted,
        "good_bytes": st.good_bytes, "written_bytes": st.written_bytes,
        "error_sectors": st.error_sectors, "retry_count": st.retry_count,
        "elapsed_s": __import__("time").time() - st.start_time,
        "smart_before": st.smart_before.to_dict() if st.smart_before else None,
        "smart_after": (st.smart_after or st.smart).to_dict() if (st.smart_after or st.smart) else None,
        "sha256": st.sha256, "latency_stats": latency_stats(lat),
        "slowest": slowest(lat, st.chunk_size or 1),
    }


def save(rep: dict, lat) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", rep["serial"])
    stamp = rep["created"].replace(":", "").replace("-", "")
    # Names are built by appending, never with_suffix(): a serial with a "." would lose everything after it.
    base = f"{safe}-{rep['mode']}-{stamp}"
    name, n = base, 1
    while (REPORT_DIR / (name + ".json")).exists():  # never overwrite an earlier report
        n += 1
        name = f"{base}-{n}"
    rep["latency_file"] = name + ".lat"
    with open(REPORT_DIR / (name + ".json"), "w") as f:
        json.dump(rep, f, indent=2)
    with open(REPORT_DIR / (name + ".lat"), "wb") as f:
        lat.tofile(f)
    return REPORT_DIR / (name + ".json")


def load(path: str):
    p = Path(path)
    with open(p) as f:
        rep = json.load(f)
    lat = array("f")
    with open(p.with_name(rep["latency_file"]), "rb") as f:
        lat.fromfile(f, -(-rep["total_bytes"] // rep["chunk_size"]))
    return rep, lat


# ---- compare ----------------------------------------------------------------------------
def compare(ra: dict, la, rb: dict, lb) -> dict:
    """Pairwise analysis of two runs over the same drive/chunking."""
    if ra["serial"] != rb["serial"] or ra["total_bytes"] != rb["total_bytes"] or ra["chunk_size"] != rb["chunk_size"]:
        raise ValueError("reports are not comparable (different drive or chunk size)")
    va = _measured(la)
    med = percentile(va, 50)
    thr = max(med * 3, med + 1000)  # "slow" = >3x median and >1 ms over it
    slow_a = [i for i, x in enumerate(la) if x > thr and i < len(lb) and lb[i] > 0]
    res = {"threshold_us": thr, "median_a_us": med, "slow_in_a": len(slow_a),
           "slow_in_b_same_chunks": sum(1 for i in slow_a if lb[i] > thr),
           "slow_total_a": sum(1 for x in la if x > thr), "slow_total_b": sum(1 for x in lb if x > thr),
           "stats_a": latency_stats(la), "stats_b": latency_stats(lb)}
    if slow_a:
        ma = percentile(sorted(la[i] for i in slow_a), 50)
        mb = percentile(sorted(lb[i] for i in slow_a), 50)
        res["slow_region_median_a_us"], res["slow_region_median_b_us"] = ma, mb
    res["hash_match"] = (ra.get("sha256") == rb.get("sha256")) if ra.get("sha256") and rb.get("sha256") else None
    return res
