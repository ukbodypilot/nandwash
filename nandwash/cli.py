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


_ASSUME_SSD = False


def _probe(device: str) -> DeviceInfo:
    try:
        info = DeviceInfo.probe(device)
    except DeviceError as e:
        raise click.ClickException(str(e))
    if info.rotational and not _ASSUME_SSD:
        hint = ("\nUSB enclosures often misreport this. If it really is an SSD, run: nandwash --assume-ssd ..."
                if "/usb" in os.path.realpath(f"/sys/block/{info.name}") else "")
        raise click.ClickException(f"{device} is a rotational drive; nandwash only supports SSDs.{hint}")
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
@click.option("--assume-ssd", is_flag=True, help="Treat a drive that reports itself as rotational as an SSD "
              "(some USB enclosures misreport).")
def main(assume_ssd):
    """SSD NAND cell refresh utility for Linux."""
    global _ASSUME_SSD
    _ASSUME_SSD = assume_ssd


def _drive_table(ds, numbered: bool = False):
    from rich.table import Table
    from .drives import human
    t = Table(title="Drives")
    if numbered:
        t.add_column("#", justify="right")
    for c in ("Device", "Drive", "Serial", "Size", "Bus", "Mounted", "Can do"):
        t.add_column(c)
    for i, d in enumerate(ds, 1):
        mounted = ", ".join(f"{m}{'' if w else ' (ro)'}" for p in d.partitions for m, w in p.mounts[:1]) or "-"
        flag = " [red]SYSTEM[/]" if d.is_system else ""
        row = [d.path, d.title + (" [dim]removable[/]" if d.removable else "") + flag, d.serial or "?",
               human(d.size), d.transport + (" ROT" if d.rotational else ""), mounted,
               d.modes if d.eligible else f"[red]{d.modes}[/]"]
        t.add_row(*([str(i)] if numbered else []), *row)
    return t


def _choose_device(device):
    """Return DEVICE, or let the user pick one when it was omitted."""
    if device:
        return device
    from .drives import enumerate_drives
    ds = enumerate_drives()
    if not ds:
        raise click.ClickException("no drives found")
    if not sys.stdin.isatty():
        console.print(_drive_table(ds))
        raise click.ClickException("no DEVICE given. Pass one of the paths above (or run interactively to pick).")
    console.print(_drive_table(ds, numbered=True))
    for d in ds:
        for n in d.notes:
            console.print(f"[dim]{d.path}: {n}[/]")
    n = click.prompt("Which drive number", type=click.IntRange(1, len(ds)))
    d = ds[n - 1]
    if d.is_system:
        click.confirm(f"{d.path} is the SYSTEM disk. Continue?", abort=True)
    console.print(f"Using {d.path}  ({d.title}, serial {d.serial or '?'})")
    return d.path


@main.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def list_cmd(as_json):
    """List attached drives (SATA, NVMe, USB) and what nandwash can safely do with each."""
    from .drives import enumerate_drives
    ds = enumerate_drives()
    if as_json:
        import json
        click.echo(json.dumps([d.to_dict() for d in ds], indent=1))
        return
    console.print(_drive_table(ds))
    for d in ds:
        for n in d.notes:
            console.print(f"[dim]{d.path}: {n}[/]")


@main.command()
@click.argument("device", required=False, type=click.Path(exists=True))
def info(device):
    """Show device info and a SMART summary."""
    _require_root()
    device = _choose_device(device)
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
@click.argument("device", required=False, type=click.Path(exists=True))
@click.option("--resume", is_flag=True, help="Continue from saved progress.")
@click.option("--max-temp", default=70, show_default=True, help="Pause while drive is hotter than this (°C).")
@click.option("--hash", "hash_data", is_flag=True, help="SHA-256 all data read (integrity check; no --resume).")
def scan(device, resume, max_temp, hash_data):
    """Read-only sector health scan (O_DIRECT). Omit DEVICE to pick from a list."""
    _require_root()
    device = _choose_device(device)
    _execute(device, "scan", resume, max_temp, hash_data=hash_data)


@main.command()
@click.argument("device", required=False, type=click.Path(exists=True))
@click.option("--resume", is_flag=True, help="Continue from saved progress.")
@click.option("--max-temp", default=65, show_default=True, help="Pause while drive is hotter than this (°C).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--hash", "hash_data", is_flag=True, help="SHA-256 all data read (integrity check; no --resume).")
def refresh(device, resume, max_temp, yes, hash_data):
    """Refresh NAND by reading every sector and writing it back. WRITES to the device. Omit DEVICE to pick."""
    _require_root()
    device = _choose_device(device)
    if not yes:
        click.confirm(f"This rewrites every sector of {device}. Back up critical data first. Continue?", abort=True)
    _execute(device, "refresh", resume, max_temp, hash_data=hash_data)


@main.command("refresh-chunks")
@click.argument("device", type=click.Path(exists=True))
@click.argument("chunks_file", type=click.Path(exists=True))
@click.option("--allow-readonly-mounts", is_flag=True,
              help="Permit filesystems on the device that are mounted READ-ONLY (offline maintenance boot).")
@click.option("--report", type=click.Path(), help="Write a JSON result here.")
@click.option("--backup-dir", type=click.Path(file_okay=False), help="Save each chunk's original bytes here first.")
@click.option("--chunk-size", default=1048576, show_default=True)
@click.option("--expect-serial", help="Refuse unless the device's serial number matches (guards against renamed devices).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def refresh_chunks_cmd(device, chunks_file, allow_readonly_mounts, report, backup_dir, chunk_size, expect_serial, yes):
    """Rewrite ONLY the chunks listed in CHUNKS_FILE (JSON list of byte offsets, or {"offsets": [...]})."""
    import json
    from . import targeted
    _require_root()
    d = _probe(device)
    if expect_serial and d.serial != expect_serial:
        raise click.ClickException(f"serial mismatch: {device} is '{d.serial}', expected '{expect_serial}'; refusing.")
    data = json.load(open(chunks_file))
    offsets = data["offsets"] if isinstance(data, dict) else data
    held = targeted.holders_of(d)
    if held:
        raise click.ClickException(f"{device} is used by device-mapper/md layers ({', '.join(held)}); refusing.")
    mounts = targeted.mounts_of(d)
    rw = [m for m in mounts if m[2]]
    if rw:
        raise click.ClickException("refusing: mounted READ-WRITE: " + ", ".join(f"{s} on {t}" for s, t, _ in rw) +
                                   "\nUnmount, or boot a read-only maintenance environment.")
    if mounts and not allow_readonly_mounts:
        raise click.ClickException("device has read-only mounts (" + ", ".join(f"{s} on {t}" for s, t, _ in mounts) +
                                   "); pass --allow-readonly-mounts to proceed.")
    if not yes:
        click.confirm(f"Rewrite {len(offsets)} chunks of {device}?", abort=True)
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
    res = targeted.refresh_chunks(d.path, offsets, chunk_size, backup_dir, log=console.print)
    if report:
        with open(report, "w") as f:
            json.dump(res, f, indent=2)
    console.print(f"{res['chunks_ok']}/{res['chunks_requested']} chunks rewritten and verified; median read "
                  f"{res['median_before_ms']} -> {res['median_after_ms']} ms" +
                  (f"\n[red]STOPPED: {res['stopped']}[/]" if res["stopped"] else ""))
    if res["stopped"]:
        sys.exit(2)


@main.command("pick-slow")
@click.argument("scan_report", type=click.Path(exists=True))
@click.argument("out_file", type=click.Path())
@click.option("--factor", default=1.8, show_default=True, help="Candidate = latency above this multiple of the scan median.")
@click.option("--max-chunks", default=500, show_default=True, help="Safety cap on how many chunks to select (slowest first).")
@click.option("--expect-serial", help="Refuse unless the device's serial number matches.")
def pick_slow_cmd(scan_report, out_file, factor, max_chunks, expect_serial):
    """Select chunks that are persistently slow: candidates from a scan report, re-read 3x here (read-only)."""
    import json
    from .pick import pick_slow
    _require_root()
    try:
        res, _info = pick_slow(scan_report, factor, max_chunks, expect_serial or "")
    except (ValueError, DeviceError) as e:
        raise click.ClickException(str(e))
    json.dump(res, open(out_file, "w"), indent=1)
    console.print(f"median {res['median_us'] / 1000:.2f} ms, threshold {res['threshold_us'] / 1000:.2f} ms: "
                  f"{res['candidates']} candidates, {len(res['offsets'])} confirmed persistently slow")


@main.command("refresh-free")
@click.argument("device", required=False, type=click.Path(exists=True))
@click.option("--chunks", "chunks_file", type=click.Path(exists=True),
              help="JSON list of byte offsets (or {\"offsets\": [...]}) to consider.")
@click.option("--from-scan", "scan_report", type=click.Path(exists=True),
              help="Pick the persistently slow chunks from this scan report instead (re-read 3x).")
@click.option("--factor", default=1.8, show_default=True, help="With --from-scan: slow = above this multiple of the median.")
@click.option("--dry-run", is_flag=True, help="Only classify the chunks (free / used / outside ...); write nothing.")
@click.option("--max-chunks", default=200, show_default=True, help="Rewrite at most this many chunks per run.")
@click.option("--pause-ms", default=300, show_default=True, help="Pause between chunks so the system can breathe.")
@click.option("--max-freeze-ms", default=3000, show_default=True, help="Hard auto-thaw for the filesystem freeze.")
@click.option("--backup-dir", type=click.Path(file_okay=False), help="Save each rewritten chunk's bytes here (after the thaw).")
@click.option("--report", type=click.Path(), help="Write a JSON result here.")
@click.option("--expect-serial", help="Refuse unless the device's serial number matches.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def refresh_free_cmd(device, chunks_file, scan_report, factor, dry_run, max_chunks, pause_ms, max_freeze_ms,
                     backup_dir, report, expect_serial, yes):
    """Rewrite chunks that are FREE SPACE on a drive that is mounted read-write (ext4/xfs), live.

    Each chunk is checked against the kernel's free-space map with the filesystem frozen, rewritten with
    its own bytes, and verified. Chunks that hold file data or metadata are reported, never touched:
    those need `refresh-chunks` in the offline maintenance boot."""
    import json
    from . import livefree
    from .pick import pick_slow
    _require_root()
    if bool(chunks_file) == bool(scan_report):
        raise click.ClickException("give exactly one of --chunks or --from-scan")
    if scan_report and not device:
        device = json.load(open(scan_report)).get("device")
    device = _choose_device(device)
    d = _probe(device)
    if expect_serial and d.serial != expect_serial:
        raise click.ClickException(f"serial mismatch: {device} is '{d.serial}', expected '{expect_serial}'; refusing.")
    if chunks_file:
        data = json.load(open(chunks_file))
        offsets = data["offsets"] if isinstance(data, dict) else data
    else:
        try:
            picked, _ = pick_slow(scan_report, factor, 10_000, d.serial)
        except (ValueError, DeviceError) as e:
            raise click.ClickException(str(e))
        offsets = picked["offsets"]
        console.print(f"{len(offsets)} persistently slow chunks selected (> {picked['threshold_us'] / 1000:.2f} ms)")
    if not dry_run and not yes:
        click.confirm(f"Rewrite free-space chunks of {device} while it is mounted? Each chunk freezes the filesystem "
                      f"for ~100 ms.", abort=True)
    try:
        res = livefree.refresh_free(d.path, offsets, backup_dir=backup_dir, dry_run=dry_run, max_chunks=max_chunks,
                                    pause_s=pause_ms / 1000.0, max_freeze_s=max_freeze_ms / 1000.0, log=console.print)
    except ValueError as e:
        raise click.ClickException(str(e))
    meaning = {"free": "free space (rewritable live)", "used": "file data / metadata (needs offline boot)",
               "unmounted": "partition not mounted (use refresh-chunks)", "outside": "outside any partition (left alone)",
               "unsupported": "unsupported filesystem / swap (left alone)", "out-of-range": "bad offset"}
    for k, n in sorted(res["states"].items()):
        console.print(f"  {n:5d}  {meaning.get(k, k)}")
    if report:
        with open(report, "w") as f:
            json.dump(res, f, indent=2)
    if dry_run:
        console.print("[dim]dry run: nothing written[/]")
        return
    skipped = [r for r in res["results"] if r["status"] != "ok"]
    console.print(f"{res.get('chunks_ok', 0)}/{len(res['results'])} chunks rewritten and verified; median read "
                  f"{res.get('median_before_ms')} -> {res.get('median_after_ms')} ms; longest freeze {res.get('max_frozen_ms')} ms")
    for r in skipped:
        console.print(f"  [yellow]{r['offset']}: {r['status']}[/]")
    if res["stopped"]:
        console.print(f"[red]STOPPED: {res['stopped']}[/]")
        sys.exit(2)


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
