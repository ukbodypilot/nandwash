"""`nandwash report DRIVE --html` (PLAN B6): one self-contained HTML file, inline SVG, no JavaScript,
no external assets. Light and dark follow the viewer's system setting.

`share=True` makes it safe to post: the serial becomes the short id, and device names, paths, the hostname
and mount points are left out. Model, size and the numbers stay (others with the same model want those).
"""
from __future__ import annotations

import html
import json
import math
import socket
from typing import List, Optional

from . import health, report as rpt
from .results import alias_of, next_step

W = 760          # chart width in SVG units; scales with the page


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _load_lat(row: dict):
    try:
        rep, lat = rpt.load(row["scan_report"])
        return rep, lat
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _offsets(path: Optional[str]) -> List[int]:
    try:
        with open(path) as f:
            return list(json.load(f).get("offsets", []))
    except (OSError, ValueError, TypeError):
        return []


def histogram_svg(lat, med_us: float) -> str:
    """Chunks by read time relative to the median, log x (0.5x..200x) and log y (count)."""
    worst = max((x / med_us for x in lat if x > 0), default=1.0)
    span = next(t for t in (10, 30, 100, 300, 1e9) if worst <= t)     # x axis ends just past the slowest chunk
    lo, hi, nb = math.log10(0.5), math.log10(span if span < 1e9 else worst * 1.1), 60
    counts = [0] * nb
    for x in lat:
        if x > 0:
            k = int((math.log10(max(x / med_us, 0.5)) - lo) / (hi - lo) * nb)
            counts[min(max(k, 0), nb - 1)] += 1
    top = max(counts) or 1
    H, base, left = 200, 150, 40
    bw = (W - left - 10) / nb
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="read-time histogram">']
    for i, c in enumerate(counts):
        if not c:
            continue
        h = (math.log10(c + 1) / math.log10(top + 1)) * (base - 10)
        x0 = 10 ** (lo + (hi - lo) * i / nb)
        cls = "bar-slow" if x0 >= health.FACTOR else "bar"
        parts.append(f'<rect class="{cls}" x="{left + i * bw:.1f}" y="{base - h:.1f}" width="{bw - 1:.1f}" '
                     f'height="{h:.1f}"><title>{x0:.2f}x: {c} chunks</title></rect>')
    ticks = [t for t in (0.5, 1, 1.8, 3, 10, 30, 100, 300, 1000) if math.log10(t) <= hi]
    for t in ticks:
        x = left + (math.log10(t) - lo) / (hi - lo) * (W - left - 10)
        cls = "line-slow" if t == health.FACTOR else "tick"
        parts.append(f'<line class="{cls}" x1="{x:.1f}" x2="{x:.1f}" y1="10" y2="{base}"/>'
                     f'<text class="lbl" x="{x:.1f}" y="{base + 16}" text-anchor="{"start" if t == 0.5 else "end" if t == ticks[-1] else "middle"}">'
                     f'{t:g}x</text>')
    parts.append(f'<text class="lbl" x="{left}" y="{H - 6}">read time relative to the median (log scale) · bar height: '
                 f'chunks (log scale, tallest {top:,}) · dashed: the {health.FACTOR:g}x slow line</text></svg>')
    return "".join(parts)


def diskmap_svg(nchunks: int, cs: int, cands, confirmed, fixed, total_bytes: int) -> str:
    """Where on the disk: candidates (faint), confirmed slow (strong), fixed (good colour)."""
    H, cols = 90, 380
    cw = (W - 20) / cols
    per = max(1, -(-nchunks // cols))
    def col(off):
        return min((off // cs) // per, cols - 1)
    cand_c, conf_c, fix_c = [0] * cols, [0] * cols, [0] * cols
    for i in cands:
        cand_c[min(i // per, cols - 1)] += 1
    for o in confirmed:
        conf_c[col(o)] += 1
    for o in fixed:
        fix_c[col(o)] += 1
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="slow chunks across the disk">',
             f'<rect class="track" x="10" y="10" width="{W - 20}" height="40"/>']
    for k in range(cols):
        x = 10 + k * cw
        if cand_c[k]:
            parts.append(f'<rect class="cand" x="{x:.1f}" y="10" width="{max(cw, 1):.1f}" height="40"/>')
        if fix_c[k]:
            parts.append(f'<rect class="fixed" x="{x:.1f}" y="10" width="{max(cw, 1.5):.1f}" height="20">'
                         f'<title>{fix_c[k]} fixed</title></rect>')
        if conf_c[k]:
            parts.append(f'<rect class="slow" x="{x:.1f}" y="30" width="{max(cw, 1.5):.1f}" height="20">'
                         f'<title>{conf_c[k]} confirmed slow</title></rect>')
    gb = total_bytes / 1e9
    for f in (0, 0.25, 0.5, 0.75, 1):
        x = 10 + f * (W - 20)
        anchor = "start" if f == 0 else "end" if f == 1 else "middle"
        parts.append(f'<text class="lbl" x="{x:.1f}" y="66" text-anchor="{anchor}">{gb * f:.0f} GB</text>')
    parts.append('<text class="lbl" x="10" y="84">top: fixed in this run · bottom: still confirmed slow · '
                 'shade: scan candidates</text></svg>')
    return "".join(parts)


def history_svg(runs: List[dict]) -> str:
    vals = [r.get("confirmed") for r in runs]
    if len([v for v in vals if v is not None]) < 2:
        return ""
    top = max(v for v in vals if v is not None) or 1
    H, base, left = 150, 110, 40
    bw = min(60, (W - left - 10) / len(runs))
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="confirmed slow chunks per run">']
    for i, (r, v) in enumerate(zip(runs, vals)):
        if v is None:
            continue
        h = max(1, v / top * (base - 20))
        x = left + i * bw
        parts.append(f'<rect class="hist" x="{x:.1f}" y="{base - h:.1f}" width="{bw - 6:.1f}" height="{h:.1f}">'
                     f'<title>{_e(r.get("date"))}: {v}</title></rect>'
                     f'<text class="lbl" x="{x + (bw - 6) / 2:.1f}" y="{base - h - 4:.1f}" text-anchor="middle">{v}</text>'
                     f'<text class="lbl" x="{x + (bw - 6) / 2:.1f}" y="{base + 14}" text-anchor="middle">'
                     f'{_e((r.get("date") or "")[5:10])}</text>')
    parts.append(f'<text class="lbl" x="{left}" y="{H - 6}">confirmed slow chunks per run</text></svg>')
    return "".join(parts)


CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e2dd;--bar:#7b8fa6;--slow:#c2410c;
--good:#15803d;--cand:rgba(194,65,12,.18);--track:#eeede8;--warn:#a16207;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--card:#1f1f1d;--line:#34332f;
--bar:#8ea3bb;--slow:#fb923c;--good:#4ade80;--cand:rgba(251,146,60,.22);--track:#2a2a27;--warn:#facc15;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:820px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:28px 0 8px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px}dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0}dt{color:var(--muted);
font-size:13px;padding-top:1px}dd{margin:0}.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-weight:600;
font-size:13px;border:1px solid currentColor}.s-healthy,.s-improved{color:var(--good)}.s-slow-regions,.s-partly,
.s-incomplete{color:var(--warn)}.s-errors,.s-no-effect{color:var(--bad)}.s-inconclusive{color:var(--muted)}
svg{width:100%;height:auto;display:block}.bar{fill:var(--bar)}.bar-slow{fill:var(--slow)}.tick{stroke:var(--line)}
.line-slow{stroke:var(--slow);stroke-dasharray:3 3}.lbl{fill:var(--muted);font-size:11px}.track{fill:var(--track)}
.cand{fill:var(--cand)}.slow{fill:var(--slow)}.fixed{fill:var(--good)}.hist{fill:var(--bar)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid
var(--line);white-space:nowrap}th{color:var(--muted);font-weight:600}.scroll{overflow-x:auto}code{font-size:13px;
overflow-wrap:anywhere}footer{margin-top:32px;color:var(--muted);font-size:12px}
"""


def render(h: dict, share: bool = False, aliases: Optional[dict] = None) -> str:
    r = h["runs"][-1]
    rep, lat = _load_lat(r)
    title = h["id"] if share else (alias_of(h["id"], aliases) or h["id"])
    st, oc = r.get("state"), r.get("outcome") or {}
    who = f"{_e(h.get('model'))}" + ("" if share else f" · serial {_e(h.get('serial'))} · {_e(r.get('device'))}"
                                     f" on {_e(socket.gethostname())}")
    size = f" · {rep['total_bytes'] / 1e9:.0f} GB" if rep else ""
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
           f'content="width=device-width,initial-scale=1"><title>nandwash {_e(title)}</title><style>{CSS}</style>'
           f'</head><body><main><h1>{_e(title)}</h1><p class="sub">{who}{size} · '
           f'{_e((r.get("date") or "")[:16].replace("T", " "))}, goal {_e(r.get("goal"))}</p>',
           '<div class="card"><dl>']
    nz = r.get("noise") or {}
    out.append(f'<dt>State</dt><dd><span class="pill s-{_e(st)}">{_e(st or "-")}</span> '
               + (f'{r["confirmed"]} confirmed slow of {r.get("candidates", 0)} candidates '
                  f'({r.get("severe", 0)} above 3x)' if r.get("confirmed") is not None else "")
               + (f'<br><small>{_e(r["why"])}</small>' if r.get("why") else "") + '</dd>')
    if r.get("median_ms"):
        out.append(f'<dt>Read time</dt><dd>median {r["median_ms"]:.2f} ms, p99 {r.get("p99_ms") or 0:.2f} ms'
                   + (f'; re-read noise {nz["p5"]:.2f}-{nz["p95"]:.2f}x' if nz.get("n") else "") + '</dd>')
    if oc:
        out.append(f'<dt>Outcome</dt><dd><span class="pill s-{_e(oc["outcome"])}">{_e(oc["outcome"])}</span> '
                   f'{oc["fixed"]}/{oc["rewritten"]} fixed ({oc["fixed_pct"]:.0f}%); median '
                   f'{oc.get("median_before_ms")} → {oc.get("median_after_ms")} ms'
                   + (f'; {oc["queued"]} wait for the offline boot' if oc.get("queued") else "")
                   + (f'<br><small>with the live refresh before it: <span class="s-{_e(cb["outcome"])}">'
                      f'{_e(cb["outcome"])}</span> {cb["fixed"]}/{cb["rewritten"]} fixed ({cb["live_fixed"]}/'
                      f'{cb["live_rewritten"]} live + {cb["offline_fixed"]}/{cb["offline_rewritten"]} offline)</small>'
                      if (cb := r.get("combined") or {}).get("outcome") else "") + '</dd>')
        out.append('<dt>Data</dt><dd>' + (f'<b class="s-errors">read-back MISMATCH on {oc["mismatch"]}</b>'
                                          if oc.get("mismatch") else
                                          f'read-back verified identical: {oc["verified"]}/{oc["verified"]}') + '</dd>')
    sc = r.get("smart_counters") or {}
    if sc:
        out.append('<dt>SMART</dt><dd>' + ", ".join(f'{_e(health.ERROR_ATTRS.get(int(k), k))} {v}'
                                                    for k, v in sorted(sc.items()))
                   + "".join(f'<br><span class="s-partly">{_e(n)}</span>' for n in r.get("smart_notes") or []) + '</dd>')
    sa = r.get("slow_again")
    if sa:
        out.append(f'<dt>Fixes held?</dt><dd>{sa["of"] - sa["count"]} of {sa["of"]} earlier fixes still fast'
                   + (f' ({sa["days_since_fix_min"]:.0f}-{sa["days_since_fix_max"]:.0f} days later)'
                      if sa.get("days_since_fix_max") is not None else "") + '</dd>')
    nxt = next_step(h, r)
    if nxt and not share:
        out.append(f'<dt>Next</dt><dd><code>{_e(nxt.replace("[bold red]", "").replace("[/]", ""))}</code></dd>')
    out.append('</dl></div>')

    if lat is not None and r.get("median_ms"):
        med = health.median_us(lat)
        cs = rep["chunk_size"]
        out.append('<h2>Read time per chunk</h2>' + histogram_svg(lat, med))
        cands = health.candidates(lat, med, rep["total_bytes"] // cs)
        out.append('<h2>Where on the disk</h2>' +
                   diskmap_svg(len(lat), cs, cands, _offsets(r.get("slow_file")), oc.get("fixed_offsets", [])
                               if isinstance(oc.get("fixed_offsets"), list) else [], rep["total_bytes"]))
    hs = history_svg(h["runs"])
    out.append('<h2>History</h2>' + hs + '<div class="scroll"><table><tr><th>Date</th><th>Goal</th><th>State</th>'
               '<th>Median ms</th><th>Candidates</th><th>Slow</th><th>Fixed</th><th>Outcome</th></tr>')
    for x in reversed(h["runs"]):
        o = x.get("outcome") or {}
        med = f'{x["median_ms"]:.2f}' if x.get("median_ms") else ""
        fx = f'{o["fixed"]}/{o["rewritten"]}' if o else ""
        out.append(f'<tr><td>{_e((x.get("date") or "")[:16].replace("T", " "))}</td><td>{_e(x.get("goal"))}</td>'
                   f'<td class="s-{_e(x.get("state"))}">{_e(x.get("state"))}</td><td>{med}</td>'
                   f'<td>{_e(x.get("candidates"))}</td><td>{_e(x.get("confirmed"))}</td><td>{fx}</td>'
                   f'<td class="s-{_e(o.get("outcome"))}">{_e(o.get("outcome", ""))}</td></tr>')
    out.append('</table></div>')
    out.append('<footer>nandwash: slow = read time above 1.8x the drive median, in the scan and again on a re-read. '
               'Fixed = back under that line and at least 30% faster after the rewrite.'
               + (' Shared report: serial, device names, paths and hostname removed.' if share else '')
               + '</footer></main></body></html>')
    return "".join(out)
