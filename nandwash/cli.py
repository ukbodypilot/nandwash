"""Command line interface."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import click
from rich.console import Console
from rich.live import Live

from . import __version__, progress as prog
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
        console.print(f"  {k.replace('_', ' '):20s} {'n/a' if v is None else v}")


def _execute(device: str, mode: str, resume: bool, max_temp: int, chunk: int = 1048576) -> None:
    _require_root()
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
        prog.save(d.serial, {
            "device_path": d.path, "serial": d.serial, "mode": mode, "total_bytes": d.total_bytes,
            "current_offset": s.current_offset, "error_sectors": s.error_sectors, "chunk_size": chunk,
            "start_time": started_iso, "good_bytes": s.good_bytes, "written_bytes": s.written_bytes,
            "retry_count": s.retry_count,
            "smart_before": s.smart_before.to_dict() if s.smart_before else None,
        })

    gen = engine_run(d.path, mode, start_offset=(state_in or {}).get("current_offset", 0),
                     chunk_size=chunk, max_temp=max_temp, resume=state_in, info=d)
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
        final_report(console, st)
    else:
        checkpoint(st)
        final_report(console, st, interrupted=True)
        console.print(f"Progress saved. Resume with: sudo nandwash {mode} {device} --resume")
        sys.exit(130 if interrupted else 1)


@main.command()
@click.argument("device", type=click.Path(exists=True))
@click.option("--resume", is_flag=True, help="Continue from saved progress.")
@click.option("--max-temp", default=70, show_default=True, help="Pause while drive is hotter than this (°C).")
def scan(device, resume, max_temp):
    """Read-only sector health scan (O_DIRECT)."""
    _execute(device, "scan", resume, max_temp)


@main.command()
@click.argument("device", type=click.Path(exists=True))
@click.option("--resume", is_flag=True, help="Continue from saved progress.")
@click.option("--max-temp", default=65, show_default=True, help="Pause while drive is hotter than this (°C).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def refresh(device, resume, max_temp, yes):
    """Refresh NAND by reading every sector and writing it back. WRITES to the device."""
    if not yes:
        click.confirm(f"This rewrites every sector of {device}. Back up critical data first. Continue?", abort=True)
    _execute(device, "refresh", resume, max_temp)


if __name__ == "__main__":
    main()
