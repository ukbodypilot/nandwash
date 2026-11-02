"""Command line interface."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import click
from rich.console import Console
from rich.live import Live

from array import array

from . import __version__, progress as prog, report as rpt
from .device import DeviceError, DeviceInfo
from .engine import run as engine_run
from .smart import SmartSnapshot, poll_smart, smartctl_available
from .ui import Dashboard, final_report, human_bytes

console = Console()
SAVE_INTERVAL = 60.0


def _require_root() -> None:
    if os.geteuid() != 0:
        raise click.ClickException("nandwash must be run as root (use sudo).")


def _probe(device: str) -> DeviceInfo:
    try:
        info = DeviceInfo.probe(device)
    except DeviceError as e:
        raise click.ClickException(str(e))
    if info.rotational:
        raise click.ClickException(f"{device} is a rotational drive; nandwash only supports SSDs.")
    return info


def _mounted(info: DeviceInfo) -> list:
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt = line.split()[:2]
                if os.path.basename(dev).startswith(info.name) and dev.startswith("/dev/"):
                    out.append(f"{dev} on {mnt}")
    except OSError:
        pass
    return out


def _analysis(st, d, interrupted: bool) -> None:
    stats = rpt.latency_stats(st.latency_us)
    if stats.get("chunks"):
        console.print(f"Read latency per {st.chunk_size // 1024} KiB chunk (ms): "
                      f"p50 {stats['p50'] / 1000:.2f}  p90 {stats['p90'] / 1000:.2f}  "
                      f"p99 {stats['p99'] / 1000:.2f}  p99.9 {stats['p999'] / 1000:.2f}  max {stats['max'] / 1000:.2f}")
    if st.mode == "refresh" and not interrupted:
        console.print("[bold]Did the drive register the rewrite?[/]")
        for line in rpt.write_evidence(st.smart_before, st.smart_after or st.smart, st.written_bytes):
            console.print("  " + line)
    if st.sha256:
        console.print(f"SHA-256 of data read: {st.sha256}")
    rep = rpt.build(st, d, interrupted)
    path = rpt.save(rep, st.latency_us if st.latency_us is not None else array("f"))
    console.print(f"Report saved: {path}")
    if st.mode == "refresh" and not interrupted:
        console.print("To judge the effect, run a scan now and compare:  nandwash compare <before.json> <after.json>")


@click.group()
@click.version_option(__version__, prog_name="nandwash")
def main():
    """SSD NAND cell refresh utility for Linux."""


@main.command()
@click.argument("device", type=click.Path(exists=True))
def info(device):
    """Show device info and a SMART summary."""
    _require_root()
    d = _probe(device)
    console.print(f"[bold]{d.model}[/]  serial {d.serial}")
    console.print(f"  path {d.path}   size {human_bytes(d.total_bytes)}   physical block {d.physical_block_size} B")
    if not smartctl_available():
        console.print("[yellow]smartctl not found; install smartmontools for SMART data.[/]")
        return
    s = poll_smart(d.path)
    if s is None:
        console.print("[yellow]SMART data unavailable.[/]")
        return
    for k, v in s.to_dict().items():
        if k == "attributes":
            continue
        console.print(f"  {k.replace('_', ' '):20s} {'n/a' if v is None else v}")
    for aid, a in sorted((s.attributes or {}).items()):
        console.print(f"  [dim]{aid:3d} {a['name']:28s} value={a['value']} raw={a['raw']}[/]")


def _execute(device: str, mode: str, resume: bool, max_temp: int, chunk: int = 1048576,
             hash_data: bool = False) -> None:
    _require_root()
    if hash_data and resume:
        raise click.ClickException("--hash needs an uninterrupted whole-device pass; it cannot be used with --resume")
    d = _probe(device)
    state_in = None
    if resume:
        state_in = prog.load(d.serial)
        if not state_in:
            raise click.ClickException(f"no saved progress for serial {d.serial}")
        if state_in.get("serial") != d.serial:
            raise click.ClickException("saved progress belongs to a different drive (serial mismatch)")
        if state_in.get("mode") != mode:
            raise click.ClickException(f"saved progress is for '{state_in.get('mode')}' mode, not '{mode}'")
        if state_in.get("total_bytes") != d.total_bytes:
            raise click.ClickException("drive size differs from saved progress")
        chunk = state_in.get("chunk_size", chunk)

    if mode == "refresh":
        mounted = _mounted(d)
        if mounted:
            raise click.ClickException("refusing to refresh a device with mounted filesystems:\n  " + "\n  ".join(mounted))

    started_iso = (state_in or {}).get("start_time") or datetime.now().isoformat(timespec="seconds")
    dash = Dashboard(d)
    st = None
    interrupted = False
    last_save = time.time()

    def checkpoint(s, done=False):
        if s.latency_us is not None:
            prog.save_latency(d.serial, s.latency_us)
        prog.save(d.serial, {
            "device_path": d.path, "serial": d.serial, "mode": mode, "total_bytes": d.total_bytes,
            "current_offset": s.current_offset, "error_sectors": s.error_sectors, "chunk_size": chunk,
            "start_time": started_iso, "good_bytes": s.good_bytes, "written_bytes": s.written_bytes,
            "retry_count": s.retry_count,
            "smart_before": s.smart_before.to_dict() if s.smart_before else None,
        })

    lat = None
    if state_in:
        nchunks = -(-d.total_bytes // chunk)
        lat = prog.load_latency(d.serial, nchunks)
    gen = engine_run(d.path, mode, start_offset=(state_in or {}).get("current_offset", 0),
                     chunk_size=chunk, max_temp=max_temp, resume=state_in, info=d, latency=lat, hash_data=hash_data)
    try:
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            for st in gen:
                live.update(dash.render(st))
                if time.time() - last_save >= SAVE_INTERVAL:
                    checkpoint(st)
                    last_save = time.time()
    except KeyboardInterrupt:
        interrupted = True
    except OSError as e:
        console.print(f"[red]I/O failure: {e}[/]")
        interrupted = True

    if st is None:
        return
    if st.finished and not interrupted:
        prog.delete(d.serial)
        prog.delete_latency(d.serial)
        final_report(console, st)
        _analysis(st, d, interrupted=False)
    else:
        checkpoint(st)
        final_report(console, st, interrupted=True)
        _analysis(st, d, interrupted=True)
        console.print(f"Progress saved. Resume with: sudo nandwash {mode} {device} --resume")
        sys.exit(130 if interrupted else 1)


@main.command()
@click.argument("device", type=click.Path(exists=True))
@click.option("--resume", is_flag=True, help="Continue from saved progress.")
@click.option("--max-temp", default=70, show_default=True, help="Pause while drive is hotter than this (°C).")
@click.option("--hash", "hash_data", is_flag=True, help="SHA-256 all data read (integrity check; no --resume).")
def scan(device, resume, max_temp, hash_data):
    """Read-only sector health scan (O_DIRECT)."""
    _execute(device, "scan", resume, max_temp, hash_data=hash_data)


@main.command()
@click.argument("device", type=click.Path(exists=True))
@click.option("--resume", is_flag=True, help="Continue from saved progress.")
@click.option("--max-temp", default=65, show_default=True, help="Pause while drive is hotter than this (°C).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--hash", "hash_data", is_flag=True, help="SHA-256 all data read (integrity check; no --resume).")
def refresh(device, resume, max_temp, yes, hash_data):
    """Refresh NAND by reading every sector and writing it back. WRITES to the device."""
    if not yes:
        click.confirm(f"This rewrites every sector of {device}. Back up critical data first. Continue?", abort=True)
    _execute(device, "refresh", resume, max_temp, hash_data=hash_data)


@main.command()
@click.argument("before", type=click.Path(exists=True))
@click.argument("after", type=click.Path(exists=True))
def compare(before, after):
    """Compare two saved reports (e.g. scan before vs scan after a refresh)."""
    from rich.table import Table
    try:
        ra, la = rpt.load(before)
        rb, lb = rpt.load(after)
        r = rpt.compare(ra, la, rb, lb)
    except (OSError, ValueError, KeyError) as e:
        raise click.ClickException(str(e))
    console.print(f"[bold]{ra['model']}[/] ({ra['serial']})   A = {ra['mode']} {ra['created']}   B = {rb['mode']} {rb['created']}")
    t = Table(title=f"Read latency per {ra['chunk_size'] // 1024} KiB chunk (ms)")
    for c in ("", "A", "B", "change"):
        t.add_column(c, justify="right")
    for k, lab in (("p50", "median"), ("p90", "p90"), ("p99", "p99"), ("p999", "p99.9"), ("max", "max")):
        a, b = r["stats_a"].get(k, 0) / 1000, r["stats_b"].get(k, 0) / 1000
        t.add_row(lab, f"{a:.2f}", f"{b:.2f}", f"{(b - a) / a * 100:+.0f}%" if a else "")
    console.print(t)
    console.print(f"Slow chunks (> {r['threshold_us'] / 1000:.2f} ms, i.e. well above A's median {r['median_a_us'] / 1000:.2f} ms): "
                  f"A {r['slow_total_a']}, B {r['slow_total_b']}")
    if r["slow_in_a"]:
        console.print(f"Of the {r['slow_in_a']} chunks slow in A, {r['slow_in_b_same_chunks']} are still slow in B; "
                      f"their median went {r['slow_region_median_a_us'] / 1000:.2f} -> {r['slow_region_median_b_us'] / 1000:.2f} ms")
    else:
        console.print("[yellow]No slow chunks in A: nothing measurable for a refresh to improve here.[/]")
    if r["hash_match"] is not None:
        console.print("Data integrity: " + ("[green]SHA-256 identical[/]" if r["hash_match"] else "[bold red]SHA-256 DIFFERS (data changed!)[/]"))
    from .smart import SmartSnapshot
    sa, sb = SmartSnapshot.from_dict(ra["smart_before"]), SmartSnapshot.from_dict(rb["smart_after"])
    rows = [x for x in rpt.attribute_diff(sa, sb) if x[4]]
    if rows:
        st = Table(title="SMART attributes that changed between A (start) and B (end)")
        for c in ("ID", "Name", "A", "B", "Delta"):
            st.add_column(c)
        for aid, name, a, b, dlt in rows:
            st.add_row(str(aid), name, str(a), str(b), f"{dlt:+d}")
        console.print(st)
    console.print("[dim]Latency varies run to run: judge changes against a control (two scans with no refresh between them).[/]")


if __name__ == "__main__":
    main()
