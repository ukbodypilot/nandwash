"""One definition of slow, fixed, noise, drive state and refresh outcome (PLAN B1-B3).

Pure functions over latency arrays (scan, microseconds per chunk, 0 = unmeasured) and re-read times
(milliseconds). The I/O that produces re-reads lives in pick.py; everything that judges lives here, so
`check`, `refresh`, `compare` and `results` cannot drift apart.
"""
from __future__ import annotations

import random
from typing import Iterable, List, Optional, Sequence, Tuple

# Tunables. Calibrated on .140 sda and mini sdb, 2026-11-02/03 (PLAN B0, B3).
FACTOR = 1.8          # candidate / confirmed: > FACTOR x drive median
NORMAL = 1.5          # noise sample draws from chunks below NORMAL x median
NOISE_N = 200         # chunks in the noise sample
NOISE_MAX_P95 = 1.5   # re-read/scan ratio p95 above this: the drive is too noisy to judge
FIX_GAIN = 0.30       # fixed: after-read at least 30% faster than before ...
LINE_MIN = 20         # slow-regions: confirmed >= max(LINE_MIN, LINE_FRAC x chunks) ...
LINE_FRAC = 1e-4
SEVERE_X = 3.0        # ... or >= SEVERE_MIN confirmed chunks above SEVERE_X x median
SEVERE_MIN = 5
IMPROVED, PARTLY = 0.80, 0.20

# SMART counters that only grow on a failing drive (ATA ids). Any increase -> `errors`.
ERROR_ATTRS = {5: "reallocated", 187: "reported uncorrectable", 197: "pending", 198: "offline uncorrectable"}

STATES = ("errors", "inconclusive", "slow-regions", "healthy")
OUTCOMES = ("improved", "partly", "no-effect", "incomplete")


def pct(sorted_vals: Sequence[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def median_us(lat) -> float:
    return pct(sorted(x for x in lat if x > 0), 50)


def candidates(lat, med_us: float, whole: Optional[int] = None) -> List[int]:
    """Chunk indices > FACTOR x median, slowest first. `whole` = number of full-size chunks (the disk's
    last chunk is usually short and is never re-read or rewritten)."""
    thr = med_us * FACTOR
    n = len(lat) if whole is None else min(whole, len(lat))
    return sorted((i for i in range(n) if lat[i] > thr), key=lambda i: -lat[i])


def noise_sample(lat, med_us: float, n: int = NOISE_N, seed: str = "", whole: Optional[int] = None) -> List[int]:
    """`n` random normal chunks (measured, < NORMAL x median). Seeded, so a re-run picks the same ones."""
    lim = len(lat) if whole is None else min(whole, len(lat))
    pool = [i for i in range(lim) if 0 < lat[i] < med_us * NORMAL]
    rng = random.Random(seed)
    return sorted(rng.sample(pool, min(n, len(pool))))


def noise_band(scan_us: Iterable[float], reread_ms: Iterable[float]) -> dict:
    """Spread of re-read/scan ratios over the noise sample. A quiet drive is ~0.95..1.10."""
    r = sorted(b * 1000.0 / a for a, b in zip(scan_us, reread_ms) if a > 0)
    if not r:
        return {"n": 0, "p5": None, "p50": None, "p95": None, "ok": True}
    p95 = pct(r, 95)
    return {"n": len(r), "p5": round(pct(r, 5), 3), "p50": round(pct(r, 50), 3), "p95": round(p95, 3),
            "ok": p95 <= NOISE_MAX_P95}


def confirmed(reread_ms: float, med_us: float) -> bool:
    return reread_ms * 1000.0 > med_us * FACTOR


def is_fixed(before_ms: Optional[float], after_ms: Optional[float], med_us: float) -> bool:
    if before_ms is None or after_ms is None:
        return False
    return after_ms * 1000.0 <= med_us * FACTOR and after_ms <= before_ms * (1 - FIX_GAIN)


def line(nchunks: int) -> int:
    return max(LINE_MIN, int(nchunks * LINE_FRAC))


def severe_count(offsets: Iterable[int], lat, chunk_size: int, med_us: float) -> int:
    return sum(1 for off in offsets if off // chunk_size < len(lat) and lat[off // chunk_size] > SEVERE_X * med_us)


def _raw(smart: Optional[dict], aid: int) -> Optional[int]:
    a = ((smart or {}).get("attributes") or {}).get(str(aid)) or ((smart or {}).get("attributes") or {}).get(aid)
    try:
        return int(a["raw"]) if a else None
    except (KeyError, TypeError, ValueError):
        return None


def smart_counters(smart: Optional[dict]) -> dict:
    """{ata id (str): raw} for the error counters present on this drive."""
    out = {}
    for aid in ERROR_ATTRS:
        v = _raw(smart, aid)
        if v is not None:
            out[str(aid)] = v
    return out


def smart_increases(before: Optional[dict], after: Optional[dict]) -> List[str]:
    """Human lines for every error counter that went up from `before` to `after` (counter dicts)."""
    out = []
    for aid, name in ERROR_ATTRS.items():
        b, a = (before or {}).get(str(aid)), (after or {}).get(str(aid))
        if b is not None and a is not None and a > b:
            out.append(f"SMART {aid} {name} {b} -> {a}")
    return out


# 197 (pending) is a live count, not a total: it can go up during a read and back down once the drive re-reads or
# remaps the sector. A Micron M500 did 0 -> 1 -> 0 within minutes on two different read-only scans (mini sdb + sdd,
# 2026-11-03), with 5/187/198 flat and an empty error log. So 197 counts only if it is STILL up after the re-reads
# (and a short wait); the totals 5/187/198 count as soon as they move.
PENDING = "197"
PENDING_SETTLE_S = 120


def smart_rises(before: Optional[dict], after: Optional[dict], now: Optional[dict] = None) -> Tuple[List[str], List[str]]:
    """(error lines, notes). `after` = end of the scan, `now` = a read after the re-reads (None: not taken)."""
    errs = smart_increases(before, after)
    notes = []
    if now:
        for x in [x for x in errs if x.startswith(f"SMART {PENDING} ")]:
            b, n = (before or {}).get(PENDING), now.get(PENDING)
            if b is not None and n is not None and n <= b:
                errs.remove(x)
                notes.append(f"{x} during the scan, back to {n} after the re-reads (transient)")
    return errs, notes


def drive_state(*, complete: bool, error_sectors: int, smart_up: Sequence[str], noise: Optional[dict],
                confirmed_n: int, severe_n: int, nchunks: int) -> dict:
    """PLAN B3: the first matching row wins. Returns {state, why}."""
    if error_sectors or smart_up:
        why = ([f"{error_sectors} unreadable sector(s)"] if error_sectors else []) + list(smart_up)
        return {"state": "errors", "why": "; ".join(why)}
    if not complete:
        return {"state": "inconclusive", "why": "scan incomplete"}
    if noise and noise.get("n") and not noise.get("ok", True):
        return {"state": "inconclusive",
                "why": f"drive too noisy to judge: re-read/scan p95 {noise['p95']:.2f} > {NOISE_MAX_P95} "
                       "(thermal throttling, background GC or a busy disk)"}
    ln = line(nchunks)
    if confirmed_n >= ln or severe_n >= SEVERE_MIN:
        bits = []
        if confirmed_n >= ln:
            bits.append(f"{confirmed_n} confirmed slow >= line {ln}")
        if severe_n >= SEVERE_MIN:
            bits.append(f"{severe_n} above {SEVERE_X:g}x median")
        return {"state": "slow-regions", "why": "; ".join(bits)}
    return {"state": "healthy",
            "why": f"{confirmed_n} confirmed slow (line {ln}), {severe_n} above {SEVERE_X:g}x" if confirmed_n else ""}


def rewrite_rows(*results: Optional[dict]) -> List[dict]:
    """The per-chunk rows of refresh_free / refresh_chunks results, rewritten ones only."""
    rows = []
    for res in results:
        for r in (res or {}).get("results", []):
            if r.get("status") in ("ok", "MISMATCH"):
                rows.append(r)
    return rows


def outcome(rows: Sequence[dict], med_us: float, queued: int = 0) -> Optional[dict]:
    """PLAN B3 refresh outcome over rewritten chunks. None when nothing was rewritten or queued."""
    ok = [r for r in rows if r.get("status") == "ok"]
    mism = sum(1 for r in rows if r.get("status") == "MISMATCH")
    if not ok and not queued and not mism:
        return None
    fixed = [r for r in ok if is_fixed(r.get("before_ms"), r.get("after_ms"), med_us)]
    share = len(fixed) / len(ok) if ok else 0.0
    if queued:
        oc = "incomplete"
    elif share >= IMPROVED:
        oc = "improved"
    elif share >= PARTLY:
        oc = "partly"
    else:
        oc = "no-effect"
    b = sorted(r["before_ms"] for r in ok if r.get("before_ms") is not None)
    a = sorted(r["after_ms"] for r in ok if r.get("after_ms") is not None)
    return {"outcome": oc, "rewritten": len(ok), "fixed": len(fixed), "fixed_pct": round(100 * share, 1),
            "queued": queued, "verified": len(ok), "mismatch": mism,
            "median_before_ms": round(pct(b, 50), 2) if b else None,
            "median_after_ms": round(pct(a, 50), 2) if a else None,
            "fixed_offsets": sorted(r["offset"] for r in fixed)}


def exit_code(state: Optional[str], oc: Optional[str]) -> int:
    """PLAN B7: 0 healthy/improved, 1 slow-regions/partly/no-effect/incomplete, 2 inconclusive, 3 errors.
    The worst of state and outcome; a refresh that `improved` a slow-regions drive counts as 0."""
    if state == "errors":
        return 3
    if state == "inconclusive":
        return 2
    if oc == "improved":
        return 0
    if oc in ("partly", "no-effect", "incomplete") or state == "slow-regions":
        return 1
    return 0
