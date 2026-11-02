"""Rich live TUI."""
from __future__ import annotations

import time

from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from . import __version__
from .device import DeviceInfo
from .engine import EngineState


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def fmt_dur(sec: float) -> str:
    if sec is None or sec != sec or sec == float("inf"):
        return "--:--:--"
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def temp_style(t) -> str:
    if t is None:
        return "dim"
    return "green" if t < 50 else "yellow" if t <= 65 else "red"


def speed_eta(st: EngineState):
    elapsed = time.time() - st.start_time
    done = st.current_offset - st.start_offset
    speed = done / elapsed if elapsed > 0 else 0.0
    remaining = st.total_bytes - st.current_offset
    eta = remaining / speed if speed > 0 else None
    return speed, elapsed, eta


def _val(v, suffix=""):
    return "n/a" if v is None else f"{v}{suffix}"


class Dashboard:
    def __init__(self, info: DeviceInfo):
        self.info = info
        self.progress = Progress(TextColumn("[bold]{task.percentage:>5.1f}%"), BarColumn(bar_width=None),
                                 expand=True)
        self.task = self.progress.add_task("", total=info.total_bytes)
        self.layout = Layout()
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="device", size=5),
            Layout(name="progress", size=5),
            Layout(name="stats", size=3),
            Layout(name="mid", size=8),
            Layout(name="log", minimum_size=6),
        )
        self.layout["mid"].split_row(Layout(name="smart"), Layout(name="results"))

    def render(self, st: EngineState) -> Layout:
        L = self.layout
        colour = "cyan" if st.mode == "scan" else "magenta"
        head = Text.assemble((f" nandwash v{__version__} ", "bold"), "  ",
                             (f" {st.mode.upper()} ", f"bold white on {colour}"))
        if st.throttled:
            head.append("  THROTTLED (temp)", style="bold red")
        L["header"].update(Panel(head))

        i = self.info
        L["device"].update(Panel(
            f"[bold]{i.model}[/]  serial {i.serial}\n{human_bytes(i.total_bytes)}  {st.device_path}",
            title="Device"))

        self.progress.update(self.task, completed=st.current_offset)
        lba = st.current_offset // 512
        L["progress"].update(Panel(Group(
            self.progress, Text(f"LBA {lba:,} / {st.total_bytes // 512:,}", justify="center")),
            title="Progress"))

        speed, elapsed, eta = speed_eta(st)
        L["stats"].update(Panel(Text(
            f"Speed {speed / 1e6:7.1f} MB/s    Elapsed {fmt_dur(elapsed)}    ETA {fmt_dur(eta)}",
            justify="center")))

        s = st.smart
        t = Table.grid(padding=(0, 2))
        if s is None:
            t.add_row("[dim]SMART unavailable (smartctl missing or failed)[/]")
        else:
            t.add_row("Temperature", Text(_val(s.temperature, " °C"), style=temp_style(s.temperature)))
            t.add_row("Lifetime left", _val(s.lifetime_remaining, "%"))
            t.add_row("Pending ECC", _val(s.pending_ecc))
            t.add_row("Reallocated", _val(s.reallocated))
        L["smart"].update(Panel(t, title="SMART"))

        r = Table.grid(padding=(0, 2))
        r.add_row("Good", human_bytes(st.good_bytes))
        r.add_row("Errors", Text(str(len(st.error_sectors)), style="red" if st.error_sectors else "green"))
        r.add_row("Retried", str(st.retry_count))
        if st.mode == "refresh":
            r.add_row("Written", human_bytes(st.written_bytes))
        L["results"].update(Panel(r, title="Results"))

        L["log"].update(Panel("\n".join(st.log_lines[-10:]), title="Log"))
        return L


def final_report(console: Console, st: EngineState, interrupted: bool = False) -> None:
    title = "Interrupted — partial results" if interrupted else "Final report"
    pct = 100 * st.current_offset / st.total_bytes if st.total_bytes else 0
    _, elapsed, _ = speed_eta(st)
    console.print(f"\n[bold]{title}[/]: {st.mode}, {pct:.1f}% of {human_bytes(st.total_bytes)} "
                  f"in {fmt_dur(elapsed)}")
    console.print(f"  read OK: {human_bytes(st.good_bytes)}   bad blocks: {len(st.error_sectors)}   "
                  f"retries: {st.retry_count}" +
                  (f"   written: {human_bytes(st.written_bytes)}" if st.mode == "refresh" else ""))
    for off in st.error_sectors[:20]:
        console.print(f"  [red]bad block @ byte {off} (LBA {off // 512})[/]")
    if len(st.error_sectors) > 20:
        console.print(f"  ... and {len(st.error_sectors) - 20} more")

    b, a = st.smart_before, st.smart_after or st.smart
    if b is None or a is None:
        console.print("[dim]No SMART comparison available.[/]")
        return
    tbl = Table(title="SMART before / after")
    for c in ("Attribute", "Before", "After", "Delta"):
        tbl.add_column(c, justify="right" if c != "Attribute" else "left")
    for label, key in (("Temperature (°C)", "temperature"), ("Reallocated (5)", "reallocated"),
                       ("Pending ECC (197)", "pending_ecc"), ("Uncorrectable (198)", "uncorrectable"),
                       ("Power-on hours (9)", "power_on_hours"), ("Lifetime left (202)", "lifetime_remaining")):
        x, y = getattr(b, key), getattr(a, key)
        d = "" if x is None or y is None else f"{y - x:+d}"
        style = "red" if key in ("reallocated", "pending_ecc", "uncorrectable") and d not in ("", "+0") else None
        tbl.add_row(label, _val(x), _val(y), Text(d, style=style) if style else d)
    console.print(tbl)
