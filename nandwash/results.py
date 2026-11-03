"""What `nandwash results` shows (PLAN B4-B6): the health card, the one-line list, history, the JSON schema.

Everything here reads history rows (history.py) and returns plain strings/dicts; cli.py only prints them.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from . import history as hist, state

RESULT_VERSION = 1
SPARK = "▁▂▃▄▅▆▇█"
COLOUR = {"healthy": "green", "improved": "green", "slow-regions": "yellow", "partly": "yellow",
          "incomplete": "yellow", "no-effect": "red", "inconclusive": "magenta", "errors": "bold red"}


def alias_of(drive_id: str, aliases: Optional[dict] = None) -> str:
    aliases = state.load_aliases() if aliases is None else aliases
    return next((a for a, i in sorted(aliases.items()) if i == drive_id), "")


def find(selector: str, aliases: Optional[dict] = None) -> Optional[dict]:
    """A drive's history by short id, alias, serial, or device name of its last run."""
    aliases = state.load_aliases() if aliases is None else aliases
    want = aliases.get(selector, selector)
    want = want[3:] if want.startswith("id:") else want
    for h in hist.all_drives():
        if want in (h.get("id"), h.get("serial")) or want == (h["runs"][-1].get("device") or "\0"):
            return h
    return None


def spark(vals: List[Optional[float]]) -> str:
    v = [x for x in vals if x is not None]
    if not v:
        return ""
    lo, hi = min(v), max(v)
    return "".join(" " if x is None else SPARK[0 if hi == lo else int((x - lo) / (hi - lo) * (len(SPARK) - 1))]
                   for x in vals)


def _c(word: Optional[str]) -> str:
    if not word:
        return "-"
    return f"[{COLOUR[word]}]{word}[/]" if word in COLOUR else word


def card(h: dict, aliases: Optional[dict] = None) -> List[Tuple[str, str]]:
    """[(label, rich text)] for the newest run of one drive."""
    r = h["runs"][-1]
    rows: List[Tuple[str, str]] = []
    al = alias_of(h["id"], aliases)
    head = f"{h['id']}  {al + '  ' if al else ''}{h.get('model', '')}  ({r.get('device') or 'serial ' + h.get('serial', '')})"
    rows.append(("", f"[bold]{head}[/]   {r.get('date', '')[:16].replace('T', ' ')}, {r.get('goal', '')}"))
    nz = r.get("noise") or {}
    noise = (f", noise {nz['p5']:.2f}-{nz['p95']:.2f}" if nz.get("n") else "")
    med = f", median {r['median_ms']:.2f} ms" if r.get("median_ms") else ""
    if r.get("confirmed") is not None:
        detail = f"{r['confirmed']} confirmed slow of {r.get('candidates', 0)} candidates ({r.get('severe', 0)} above 3x){med}{noise}"
    else:
        detail = f"no re-reads in this run{med}"
    rows.append(("STATE", f"{_c(r.get('state'))}   {detail}" + (f"\n         {r['why']}" if r.get("why") and
                                                                    r.get("state") in ("errors", "inconclusive") else "")))
    oc = r.get("outcome")
    if oc:
        txt = (f"{_c(oc['outcome'])}   {oc['fixed']}/{oc['rewritten']} fixed ({oc['fixed_pct']:.0f}%); "
               f"read median {oc.get('median_before_ms')} -> {oc.get('median_after_ms')} ms")
        if oc.get("queued"):
            txt += f"; [bold]{oc['queued']} queued for the offline boot[/]"
        cb = r.get("combined")
        if cb and cb.get("outcome"):
            txt += (f"\n         with the live refresh before it: {_c(cb['outcome'])} {cb['fixed']}/{cb['rewritten']} "
                    f"fixed ({cb['live_fixed']}/{cb['live_rewritten']} live + {cb['offline_fixed']}/"
                    f"{cb['offline_rewritten']} offline)")
        rows.append(("OUTCOME", txt))
        if oc.get("mismatch"):
            rows.append(("DATA", f"[bold red]READ-BACK MISMATCH on {oc['mismatch']} chunk(s)[/]: the job stopped there"))
        elif oc.get("verified"):
            rows.append(("DATA", f"read-back verified identical: {oc['verified']}/{oc['verified']}"))
    sc = r.get("smart_counters") or {}
    if r.get("state") == "errors":
        rows.append(("SMART", f"[red]{r.get('why', '')}[/]"))
    elif sc:
        rows.append(("SMART", "no error counters rose (" + ", ".join(f"{k}={v}" for k, v in sorted(sc.items())) + ")"
                     + "".join(f"\n         [yellow]{n}[/]" for n in r.get("smart_notes") or [])))
    sa = r.get("slow_again")
    runs = h["runs"]
    if len(runs) > 1 or sa:
        conf = [x.get("confirmed") for x in runs]
        line = " -> ".join("?" if c is None else str(c) for c in conf[-6:]) + f" confirmed slow over {len(runs)} runs"
        if sa:
            line += (f"; {sa['count']} of {sa['of']} earlier fixes slow again"
                     + (f" ({sa['days_since_fix_min']:.0f}-{sa['days_since_fix_max']:.0f} days after the fix)"
                        if sa.get("days_since_fix_max") is not None else ""))
        rows.append(("HISTORY", line))
    nxt = next_step(h, r)
    if nxt:
        rows.append(("NEXT", nxt))
    return rows


def next_step(h: dict, r: dict) -> str:
    st, oc = r.get("state"), (r.get("outcome") or {}).get("outcome")
    ref = alias_of(h["id"]) or h["id"]
    if st == "errors":
        return "[bold red]back up this drive now[/]; then look at `smartctl -a` before any refresh"
    if r.get("goal") == "offline" and not r.get("state") and r.get("next"):
        return r["next"]          # the boot aborted or failed: its log says why
    if oc == "incomplete" and r.get("next"):
        return r["next"]
    if st == "inconclusive":
        return f"sudo nandwash run --drives {ref} --goal check   (when the system is idle and the drive is cool)"
    if st == "slow-regions" and not oc:
        return f"sudo nandwash run --drives {ref} --goal refresh"
    if oc == "no-effect":
        return "this drive may skip identical writes; see the write-evidence counters in the rewrite report"
    return ""


def one_line(h: dict, aliases: Optional[dict] = None) -> Tuple[str, ...]:
    r = h["runs"][-1]
    oc = (r.get("outcome") or {}).get("outcome")
    return (h["id"], alias_of(h["id"], aliases), h.get("model", ""), _c(r.get("state")), _c(oc) if oc else "",
            str(r.get("confirmed", "")), r.get("date", "")[:10])


def history_rows(h: dict) -> List[Tuple[str, ...]]:
    out = []
    for r in h["runs"]:
        oc = r.get("outcome") or {}
        sa = r.get("slow_again") or {}
        out.append((r.get("date", "")[:16].replace("T", " "), r.get("goal", ""), _c(r.get("state")),
                    f"{r['median_ms']:.2f}" if r.get("median_ms") else "", str(r.get("candidates", "")),
                    str(r.get("confirmed", "")), f"{oc['fixed']}/{oc['rewritten']}" if oc else "",
                    _c(oc.get("outcome")) if oc else "", f"{sa['count']}/{sa['of']}" if sa else ""))
    return out


def to_json(hs: List[dict], aliases: Optional[dict] = None, full: bool = False) -> dict:
    """PLAN B6 schema. Pinned by tests/test_results.py: add fields, never rename or remove them."""
    out = []
    for h in hs:
        r = h["runs"][-1]
        oc = dict(r.get("outcome") or {}) or None
        if oc:
            oc.pop("fixed_offsets", None)
        out.append({"id": h["id"], "alias": alias_of(h["id"], aliases), "serial": h.get("serial", ""),
                    "model": h.get("model", ""), "runs": len(h["runs"]), "date": r.get("date"), "goal": r.get("goal"),
                    "state": r.get("state"), "why": r.get("why", ""), "outcome": oc,
                    "median_ms": r.get("median_ms"), "p99_ms": r.get("p99_ms"), "noise": r.get("noise"),
                    "candidates": r.get("candidates"), "confirmed": r.get("confirmed"), "severe": r.get("severe"),
                    "errors": r.get("errors"), "slow_again": r.get("slow_again"),
                    "smart_counters": r.get("smart_counters"), "smart_notes": r.get("smart_notes") or [],
                    "combined": r.get("combined"), "next": next_step(h, r).replace("[bold red]", "")
                    .replace("[/]", ""), **({"history": h["runs"]} if full else {})})
    return {"nandwash_result": RESULT_VERSION, "drives": out}
