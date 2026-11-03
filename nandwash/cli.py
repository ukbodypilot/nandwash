"""Command line interface."""
from __future__ import annotations

import os
import sys
import time

import click
from rich.console import Console
from rich.live import Live

from array import array

from . import __version__, report as rpt
from .device import DeviceError, DeviceInfo
from .smart import poll_smart, smartctl_available
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


def _analysis(st, d, interrupted: bool, path=None) -> None:
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
    if path is None:
        path = rpt.save(rpt.build(st, d, interrupted), st.latency_us if st.latency_us is not None else array("f"))
    console.print(f"Report saved: {path}")
    if st.mode == "refresh" and not interrupted:
        console.print("To judge the effect, run a scan now and compare:  nandwash compare <before.json> <after.json>")


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="nandwash")
@click.option("--assume-ssd", is_flag=True, help="Treat a drive that reports itself as rotational as an SSD "
              "(some USB enclosures misreport).")
@click.pass_context
def main(ctx, assume_ssd):
    """SSD NAND cell refresh utility for Linux.

    Run with no command for the guided flow: pick drives, pick a goal, review the plan, run it.
    """
    global _ASSUME_SSD
    _ASSUME_SSD = assume_ssd
    if os.geteuid() == 0:
        from .state import import_legacy
        import_legacy(log=lambda m: console.print(f"[dim]{m}[/]", highlight=False))
        _import_offline()
    if ctx.invoked_subcommand is None:
        if not sys.stdin.isatty():
            click.echo(ctx.get_help())
            return
        _interactive()


def _import_offline() -> None:
    """Offline boot results -> history, once per boot directory (offline.py). Never fails the command."""
    if os.environ.get("NANDWASH_NO_IMPORT") == "1" and not os.environ.get("NANDWASH_MAINT_DIR"):
        return                      # the offline boot itself, and tests that did not ask for it
    try:
        from . import offline
        offline.import_all(log=lambda m: console.print(f"[dim]{m}[/]", highlight=False))
    except Exception as e:          # noqa: BLE001 -- a broken results dir must not stop `status` or `run`
        console.print(f"[dim]offline results not imported: {e}[/]", highlight=False)


def parse_numbers(text: str, n: int) -> list:
    """'1,3' / '2-4' / '' / 'all' -> sorted 0-based indices into a list of n. Raises ValueError."""
    text = (text or "").strip().lower()
    if text in ("", "all", "a"):
        return list(range(n))
    out = set()
    for part in text.replace(" ", ",").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if not (1 <= lo <= hi <= n):
            raise ValueError(f"{part}: pick numbers between 1 and {n}")
        out.update(range(lo - 1, hi))
    if not out:
        raise ValueError("nothing picked")
    return sorted(out)


_GOAL_MENU = (("check", "check: scan and find slow chunks. Read-only."),
              ("refresh", "refresh: check, then rewrite the slow chunks it can rewrite now (live or offline)"),
              ("refresh-full", "refresh-full: rewrite the whole surface of unmounted drives"))


def _interactive() -> None:
    from . import jobs, plan as pl, selection as sl
    from .drives import enumerate_drives
    from .state import load_aliases
    running = jobs.latest(lambda j: jobs.effective_state(j) == "running")
    if running:
        console.print(f"Job {running['id']} is running.")
        _job_summary(running)
        return
    paused = jobs.latest(jobs.resumable)
    if paused and click.confirm(f"Job {paused['id']} ({paused['goal']}) is {jobs.effective_state(paused)}. "
                                "Resume it?", default=True):
        _require_root()
        _run_job(paused, False, 70)
        return
    ds = [d for d in enumerate_drives() if d.can("check")]
    if not ds:
        raise click.ClickException("no drive nandwash can work on (see `nandwash list -v`)")
    console.print(_drive_table(ds, numbered=True, aliases=load_aliases()))
    _drive_reasons(ds)
    while True:
        try:
            picked = [ds[i] for i in parse_numbers(click.prompt("Drives (e.g. 1,3 or 2-4; Enter = all)",
                                                                default="", show_default=False), len(ds))]
            break
        except ValueError as e:
            console.print(f"[yellow]{e}[/]")
    for i, (_g, text) in enumerate(_GOAL_MENU, 1):
        console.print(f"  {i}. {text}")
    goal = _GOAL_MENU[click.prompt("Goal", type=click.IntRange(1, len(_GOAL_MENU)), default=1) - 1][0]
    # Picking by number names each drive explicitly, so a picked SYSTEM disk is in (and asks for its serial).
    sel = sl.resolve(ds, ",".join("id:" + d.id for d in picked), load_aliases(), write=goal != "check")
    p = pl.make(sel, goal)
    _show_plan(p)
    if not p.drives:
        return
    if os.geteuid() != 0:
        console.print("Starting needs root. Same plan: sudo nandwash run --drives "
                      + ",".join("id:" + d.id for d in p.drives) + f" --goal {goal}")
        return
    pairs, problems = pl.validate(p, enumerate_drives())
    if problems:
        raise click.ClickException("drives changed while you chose: " + "; ".join(problems))
    if p.writes:
        _confirm_writes(pairs, yes=False)
    elif not click.confirm("Start?", default=True):
        return
    bg = click.confirm("Run in the background (watch with `nandwash status`)?", default=False)
    _run_job(jobs.create(p), bg, 70)


_CAP_COLS = (("check", "check"), ("live", "live"), ("offline-chunks", "chunks"), ("full", "full"),
             ("offline-boot", "boot"))


def _mark(c) -> str:
    return "[green]yes[/]" if c.ok else "[yellow] ? [/]" if c.ok is None else "[dim] - [/]"


def _drive_table(ds, numbered: bool = False, aliases: dict | None = None):
    """Fits 80 columns: who the drive is and what it can do. Reasons go below the table (`_drive_reasons`)."""
    from rich.table import Table
    from .drives import human
    rev = {v: k for k, v in (aliases or {}).items()}
    from rich import box
    t = Table(title="Drives", box=box.SIMPLE_HEAD, pad_edge=False, padding=(0, 1))
    if numbered:
        t.add_column("#", justify="right")
    for c in ("Dev", "ID", "Drive", "Size", "Bus"):
        t.add_column(c, no_wrap=c != "Drive", overflow="fold", min_width=14 if c == "Drive" else None)
    t.add_column("Can do", overflow="fold")
    for i, d in enumerate(ds, 1):
        flag = " [red]SYSTEM[/]" if d.is_system else ""
        ident = d.id + (f"\n[cyan]{rev[d.id]}[/]" if d.id in rev else "")
        row = [d.name, ident, d.title + flag, human(d.size).replace(" ", ""),
               d.transport + ("+TB" if d.thunderbolt else "") + (" ROT" if d.rotational else "")]
        can = [label + ("?" if d.caps[k].ok is None else "") for k, label in _CAP_COLS
               if k in d.caps and d.caps[k].ok is not False]
        row.append(" ".join(can) if can else "[red]nothing[/]")
        t.add_row(*([str(i)] if numbered else []), *row)
    return t


def _drive_reasons(ds, verbose: bool = False) -> None:
    """One block per drive: serial, mounts, and why each capability is yes / no."""
    for d in ds:
        mounts = ", ".join(f"{m}{'' if w else ' (ro)'}" for p in d.partitions for m, w in p.mounts[:1]) or "not mounted"
        console.print(f"[bold]{d.name}[/] {d.id}  serial {d.serial or '?'}  {mounts}", highlight=False)
        for k, label in _CAP_COLS:
            c = d.caps.get(k)
            if c is None or (not verbose and not c.reasons):
                continue
            if not verbose and k != "check" and not d.can("check"):
                continue
            console.print(f"    {label:6s} {_mark(c)}  [dim]{'; '.join(c.reasons)}[/]", highlight=False)
        for n in d.notes:
            if verbose or "THUNDERBOLT" in n or "USB" in n:
                console.print(f"    [dim]note: {n}[/]", highlight=False)


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
    _drive_reasons(ds)
    n = click.prompt("Which drive number", type=click.IntRange(1, len(ds)))
    d = ds[n - 1]
    if d.is_system:
        click.confirm(f"{d.path} is the SYSTEM disk. Continue?", abort=True)
    console.print(f"Using {d.path}  ({d.title}, serial {d.serial or '?'})")
    return d.path


@main.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("-v", "--verbose", is_flag=True, help="Every reason and note, for yes and no.")
def list_cmd(as_json, verbose):
    """List attached drives and what nandwash can safely do with each, with the reason.

    \b
    check   scan + re-read, read-only
    live    rewrite free-space chunks while mounted (ext4/xfs)
    chunks  rewrite chosen chunks: nothing has the drive mounted read-write
    full    whole-surface rewrite: nothing mounted
    boot    rewrite in-use chunks in the offline maintenance boot (? = run `preboot check`)"""
    from .drives import enumerate_drives
    from .state import load_aliases
    ds = enumerate_drives()
    aliases = load_aliases()
    if as_json:
        import json
        rev = {v: k for k, v in aliases.items()}
        out = []
        for d in ds:
            x = d.to_dict()
            x["alias"] = rev.get(d.id)
            out.append(x)
        click.echo(json.dumps({"nandwash_list": 1, "drives": out}, indent=1))
        return
    console.print(_drive_table(ds, aliases=aliases))
    _drive_reasons(ds, verbose)


@main.command("alias")
@click.argument("drive", required=False)
@click.argument("name", required=False)
@click.option("--remove", "remove", metavar="NAME", help="Delete an alias.")
def alias_cmd(drive, name, remove):
    """Name a drive: `nandwash alias sda data-m600` (DRIVE is any selector naming one drive).
    With no arguments, list the aliases."""
    from . import selection as sl
    from .drives import enumerate_drives
    from .state import load_aliases, remove_alias, set_alias
    if remove:
        _require_root()
        console.print("removed" if remove_alias(remove) else f"no alias {remove!r}")
        return
    if not drive:
        for k, v in sorted(load_aliases().items()):
            console.print(f"{k:20s} {v}", highlight=False)
        return
    if not name:
        raise click.UsageError("give DRIVE and NAME")
    _require_root()
    try:
        s = sl.resolve(enumerate_drives(), drive, load_aliases())
        if len(s.drives) != 1:
            raise click.ClickException(f"{drive!r} selects {len(s.drives)} drives; name exactly one")
        set_alias(name, s.drives[0].id)
    except (sl.SelectorError, ValueError) as e:
        raise click.ClickException(str(e))
    console.print(f"{name} -> {s.drives[0].id} ({s.drives[0].name}, {s.drives[0].title})", highlight=False)


def _goal_opt(f):
    return click.option("--goal", type=click.Choice(["check", "refresh", "refresh-full"]), default="check",
                        show_default=True, help="check = read-only; refresh = rewrite what can be rewritten now; "
                        "refresh-full = whole-surface rewrite of unmounted drives.")(f)


def _make_plan(drives_sel: str, goal: str):
    from . import plan as pl, selection as sl
    from .drives import enumerate_drives
    from .state import load_aliases
    ds = enumerate_drives()
    try:
        s = sl.resolve(ds, drives_sel, load_aliases(), write=goal != "check")
    except sl.SelectorError as e:
        raise click.ClickException(str(e))
    return pl.make(s, goal)


def _show_plan(p) -> None:
    from rich.table import Table
    from . import plan as pl
    from .drives import human
    from rich import box
    if not p.drives:
        console.print(f"[bold]Plan: {p.goal}[/]: nothing to do on the selected drives.")
    t = Table(title=f"Plan: {p.goal}", box=box.SIMPLE_HEAD, pad_edge=False)
    for c in ("Dev", "ID", "Drive", "Steps", "Time"):
        t.add_column(c, overflow="fold", no_wrap=c in ("Dev", "ID", "Time"))
    for d in p.drives:
        t.add_row(d.name or "?", d.id, f"{d.model} {human(d.size)}" + (" [red]SYSTEM[/]" if d.system else ""),
                  " -> ".join(d.steps), pl.human_s(d.est_s))
    if p.drives:
        console.print(t)
    for d in p.drives:
        for n in d.notes:
            console.print(f"  [dim]{d.name}: {n}[/]", highlight=False)
    for label, why in p.refused:
        console.print(f"  [yellow]skipped[/] {label}: {why}", highlight=False)
    total = sum(d.est_s or 0 for d in p.drives)
    console.print(f"{len(p.drives)} drive(s), one at a time, about {pl.human_s(total)}. "
                  + ("[bold]This plan WRITES to the drives.[/]" if p.writes else "Read-only."))


@main.command("plan")
@click.option("--drives", "drives_sel", default="all", show_default=True,
              help="Selector: all, sda, serial:X, id:nw-..., an alias, model:GLOB, bus:sata|nvme|usb|tb; -TERM excludes.")
@_goal_opt
@click.option("-o", "--output", type=click.Path(dir_okay=False, writable=True),
              help="Save the plan as TOML (review/edit it, then `nandwash run FILE`).")
def plan_cmd(drives_sel, goal, output):
    """Show what a goal would do on each selected drive. Writes nothing to any drive."""
    from . import plan as pl
    p = _make_plan(drives_sel, goal)
    _show_plan(p)
    if output:
        with open(output, "w") as f:
            f.write(pl.to_toml(p))
        console.print(f"saved {output}")


# ---------- jobs (A5): run / status / resume / cancel ----------
def _job_table(job):
    from rich.table import Table
    from rich import box
    from . import plan as pl
    from .drives import human
    t = Table(title=f"job {job['id']}: {job['goal']} ({_jobs().effective_state(job)})", box=box.SIMPLE_HEAD,
              pad_edge=False)
    for c in ("Dev", "ID", "Drive", "Step", "State", "%", "MB/s", "ETA", "°C", "Slow"):
        t.add_column(c, no_wrap=c not in ("Drive", "State"), overflow="fold")
    for d in job["drives"]:
        pr, res = d.get("progress") or {}, d.get("results") or {}
        step = d["steps"][d["step"]] if d["step"] < len(d["steps"]) else "-"
        state = d["status"] + (f": {d['error']}" if d.get("error") else "")
        colour = {"done": "green", "failed": "red", "skipped": "yellow", "cancelled": "yellow"}.get(d["status"])
        slow = res.get("pick", {})
        t.add_row(d.get("name") or "?", d["id"], f"{d['model']} {human(d['size'])}", f"{d['step'] + 1 if step != '-' else len(d['steps'])}/{len(d['steps'])} {step}",
                  f"[{colour}]{state}[/]" if colour else state,
                  f"{pr['pct']:.1f}" if pr.get("pct") is not None else "",
                  f"{pr['mbps']:.0f}" if pr.get("mbps") else "", pl.human_s(pr["eta_s"]) if pr.get("eta_s") else "",
                  str(pr["temp"]) if pr.get("temp") is not None else "",
                  f"{slow['confirmed']} {slow['state']}" if slow else "")
    return t


def _jobs():
    from . import jobs
    return jobs


def _job_summary(job) -> None:
    console.print(_job_table(job))
    if job.get("message"):
        console.print(f"[dim]{job['message']}[/]", highlight=False)
    for d in job["drives"]:
        r = d.get("results") or {}
        for k in ("live_free", "rewrite_chunks"):
            if k in r and r[k].get("chunks_ok"):
                console.print(f"  {d['name']}: {k.replace('_', ' ')}: {r[k]['chunks_ok']} chunks, median read "
                              f"{r[k].get('median_before_ms')} -> {r[k].get('median_after_ms')} ms", highlight=False)
        if d.get("health") or r.get("outcome"):
            from .results import _c
            oc = r.get("outcome") or {}
            console.print(f"  {d['name']}: {_c(d.get('health'))}" + (f", refresh {_c(oc.get('outcome'))} "
                          f"({oc['fixed']}/{oc['rewritten']} fixed)" if oc else ""), highlight=False)
        q = r.get("queue_offline")
        if q and q.get("chunks"):
            console.print(f"  [bold]{d['name']}: {q['chunks']} slow chunks hold data and wait for the offline boot.[/]"
                          f"\n    next: {q['next']}", highlight=False)
    if _jobs().resumable(job):
        console.print(f"Resume with: sudo nandwash resume {job['id']}")


def _confirm_writes(pairs, yes: bool) -> None:
    """Write plans need a yes; a SYSTEM disk needs its serial's last 4 characters typed, even with --yes."""
    for dp, d in pairs:
        if any(s in ("live-free", "rewrite-chunks", "full-refresh") for s in dp.steps) and d.is_system:
            got = click.prompt(f"{d.name} ({d.model}, serial {d.serial}) is the SYSTEM disk. Type the last 4 "
                               "characters of its serial to allow writes to it")
            if got.strip() != d.serial[-4:]:
                raise click.ClickException("serial did not match; nothing done")
    if not yes:
        click.confirm("This plan WRITES to the drives above. Start?", abort=True)


def _run_job(job, background: bool, max_temp: int) -> None:
    jobs = _jobs()
    if background:
        import shutil
        import subprocess
        exe = [sys.executable, "-m", "nandwash", "_job-run", job["id"], "--max-temp", str(max_temp)]
        env = {k: os.environ[k] for k in ("NANDWASH_STATE", "NANDWASH_LOCK", "NANDWASH_TEST_LOOP", "NANDWASH_NO_IMPORT",
                                          "NANDWASH_MAINT_DIR", "PYTHONPATH", "HOME")
               if k in os.environ}
        if shutil.which("systemd-run"):
            cmd = ["systemd-run", f"--unit=nandwash-job-{job['id']}", "--collect", "--quiet",
                   "-p", f"StandardOutput=truncate:{jobs.job_dir(job['id'])}/runner.log", "-p", "StandardError=inherit"]
            cmd += [f"--setenv={k}={v}" for k, v in env.items()] + exe
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise click.ClickException(f"systemd-run failed: {r.stderr.strip()}")
            console.print(f"job {job['id']} running as unit nandwash-job-{job['id']}")
        else:
            with open(os.path.join(jobs.job_dir(job["id"]), "runner.log"), "w") as log:
                subprocess.Popen(exe, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            console.print(f"job {job['id']} running in the background (detached)")
        # Return only once the runner has taken the job: a `status` straight after must not see the old
        # paused/new state and exit at once (the acceptance test hit exactly that).
        import time
        before = jobs.effective_state(job)
        for _ in range(100):
            now = jobs.effective_state(jobs.load(job["id"]))
            if now == "running" or (now != before and now not in ("paused", "interrupted")):
                break
            time.sleep(0.1)
        else:
            console.print(f"[yellow]the job has not started after 10 s; see {jobs.job_dir(job['id'])}/runner.log[/]")
        console.print("Watch: nandwash status   Stop: sudo nandwash cancel")
        return
    with Live(_job_table(job), console=console, refresh_per_second=2, screen=False) as live:
        r = jobs.Runner(job, on_update=lambda j: live.update(_job_table(j)), max_temp=max_temp)
        try:
            r.run()
        except jobs.JobError as e:
            raise click.ClickException(str(e))
    _job_summary(job)
    sys.exit(jobs.exit_code(job))


@main.command("run")
@click.argument("plan_file", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option("--drives", "drives_sel", help="Selector (see `nandwash plan --help`); instead of PLAN_FILE.")
@_goal_opt
@click.option("--background", is_flag=True, help="Run under systemd-run; watch with `nandwash status`.")
@click.option("--yes", is_flag=True, help="Do not ask before writing (the SYSTEM disk still asks).")
@click.option("--max-temp", default=70, show_default=True, help="Pause while a drive is hotter than this (°C).")
@click.option("--on-result", metavar="CMD", help="Run CMD (shell) when the job ends, results JSON on stdin.")
def run_cmd(plan_file, drives_sel, goal, background, yes, max_temp, on_result):
    """Run a plan: a saved PLAN_FILE, or --drives/--goal made now. One drive at a time."""
    from . import plan as pl
    from .drives import enumerate_drives
    jobs = _jobs()
    _require_root()
    if plan_file and drives_sel:
        raise click.UsageError("give PLAN_FILE or --drives, not both")
    if plan_file:
        try:
            p = pl.load(plan_file)
        except (OSError, ValueError) as e:
            raise click.ClickException(str(e))
    else:
        p = _make_plan(drives_sel or "all", goal)
    pairs, problems = pl.validate(p, enumerate_drives())
    for x in problems:
        console.print(f"[yellow]cannot run[/] {x}", highlight=False)
    if plan_file and problems:
        raise click.ClickException("the plan no longer matches the attached drives; make a new one "
                                   "(`nandwash plan ... -o FILE`)")
    p.drives = [dp for dp, _ in pairs]
    _show_plan(p)
    if not p.drives:
        return
    busy = jobs.latest(lambda j: jobs.effective_state(j) == "running")
    if busy:
        raise click.ClickException(f"job {busy['id']} is running; see `nandwash status`")
    if p.writes:
        _confirm_writes(pairs, yes)
    job = jobs.create(p)
    if on_result:
        job["on_result"] = on_result
        jobs.save(job)
    _run_job(job, background, max_temp)


@main.command("_job-run", hidden=True)
@click.argument("job_id")
@click.option("--max-temp", default=70)
def job_run_cmd(job_id, max_temp):
    import signal
    jobs = _jobs()

    def _term(*_):                      # `systemctl stop` / `kill`: pause cleanly at the next chunk, like Ctrl+C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, _term)
    job = jobs.load(job_id)
    jobs.Runner(job, log=lambda s: print(s, flush=True), max_temp=max_temp).run()
    sys.exit(jobs.exit_code(job))     # a drive with errors fails the unit: visible in `systemctl --failed`


def _pick_job(job_id):
    jobs = _jobs()
    try:
        if job_id:
            return jobs.load(job_id)
    except jobs.JobError as e:
        raise click.ClickException(str(e))
    j = jobs.latest(lambda j: jobs.effective_state(j) == "running") or jobs.latest()
    if not j:
        raise click.ClickException("no jobs yet (start one with `nandwash run`)")
    return j


@main.command("status")
@click.argument("job_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="One snapshot of job.json for scripts.")
@click.option("--once", is_flag=True, help="Print once instead of following a running job.")
def status_cmd(job_id, as_json, once):
    """Show the running (or latest) job; follows it live until it ends or Ctrl+C."""
    import json
    jobs = _jobs()
    job = _pick_job(job_id)
    if as_json:
        job["effective_state"] = jobs.effective_state(job)
        click.echo(json.dumps(job, indent=1))
        return
    if once or jobs.effective_state(job) != "running":
        _job_summary(job)
        return
    try:
        with Live(_job_table(job), console=console, refresh_per_second=1, screen=False) as live:
            while jobs.effective_state(job) == "running":
                time.sleep(1)
                job = jobs.load(job["id"])
                live.update(_job_table(job))
    except KeyboardInterrupt:
        console.print("[dim]detached; the job keeps running[/]")
        return
    _job_summary(job)


@main.command("resume")
@click.argument("job_id", required=False)
@click.option("--background", is_flag=True)
@click.option("--max-temp", default=70, show_default=True)
def resume_cmd(job_id, background, max_temp):
    """Continue a paused or interrupted job where it stopped."""
    jobs = _jobs()
    _require_root()
    job = jobs.load(job_id) if job_id else jobs.latest(jobs.resumable)
    if not job:
        raise click.ClickException("no resumable job")
    if not jobs.resumable(job):
        raise click.ClickException(f"job {job['id']} is {jobs.effective_state(job)}; nothing to resume")
    try:
        os.unlink(os.path.join(jobs.job_dir(job["id"]), "cancel"))
    except OSError:
        pass
    _run_job(job, background, max_temp)


@main.command("cancel")
@click.argument("job_id", required=False)
def cancel_cmd(job_id):
    """Stop the running job at the next chunk boundary (a paused job is marked cancelled)."""
    jobs = _jobs()
    _require_root()
    job = _pick_job(job_id)
    st = jobs.effective_state(job)
    if st == "running":
        jobs.request_cancel(job["id"])
        console.print(f"cancel requested; job {job['id']} stops at the next chunk boundary")
    elif jobs.resumable(job):
        job["state"] = "cancelled"
        for d in job["drives"]:
            if d["status"] not in jobs.FINAL:
                d["status"] = "cancelled"
        jobs.save(job)
        console.print(f"job {job['id']} cancelled")
    else:
        console.print(f"job {job['id']} is already {st}")


# ---------- results (B4-B6) ----------
@main.command("results")
@click.argument("drive", required=False)
@click.option("--history", "show_history", is_flag=True, help="Every run of DRIVE, plus sparklines.")
@click.option("--json", "as_json", is_flag=True, help="Versioned JSON (`nandwash_result`: 1) for scripts.")
def results_cmd(drive, show_history, as_json):
    """Health card of DRIVE (id, alias, serial or device name), or one line per drive."""
    import json
    from rich.table import Table
    from rich import box
    from . import history as hist, results as res
    if os.geteuid() != 0:
        try:
            from . import offline
            n = len(offline.pending())
        except OSError:
            n = 0
        if n:
            console.print(f"[yellow]{n} offline boot result(s) not imported yet: run `sudo nandwash results` once[/]")
    if drive:
        h = res.find(drive)
        if not h:
            raise click.ClickException(f"no results for {drive!r} yet (run `sudo nandwash run --drives {drive} "
                                       "--goal check`)")
        hs = [h]
    else:
        if show_history:
            raise click.UsageError("--history needs a DRIVE")
        hs = hist.all_drives()
    if as_json:
        click.echo(json.dumps(res.to_json(hs, full=show_history), indent=1))
        return
    if not hs:
        console.print("No results yet. Start with: sudo nandwash   (or `sudo nandwash run --drives all --goal check`)")
        return
    if not drive:
        t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
        for c in ("ID", "Alias", "Model", "State", "Outcome", "Slow", "Last run"):
            t.add_column(c)
        for h in hs:
            t.add_row(*res.one_line(h))
        console.print(t)
        return
    h = hs[0]
    for label, text in res.card(h):
        console.print(f"{label:<8} {text}" if label else text, highlight=False)
    if show_history:
        t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
        for c in ("Date", "Goal", "State", "Median ms", "Cand.", "Slow", "Fixed", "Outcome", "Slow again"):
            t.add_column(c, no_wrap=c in ("Date", "Goal", "State"))
        for row in res.history_rows(h):
            t.add_row(*row)
        console.print(t)
        console.print(f"slow chunks  {res.spark([r.get('confirmed') for r in h['runs']])}\n"
                      f"median       {res.spark([r.get('median_ms') for r in h['runs']])}", highlight=False)


@main.command("report")
@click.argument("drive")
@click.option("--html", "as_html", is_flag=True, required=True, help="Self-contained HTML (the only format so far).")
@click.option("-o", "output", type=click.Path(dir_okay=False), help="Write here instead of stdout.")
@click.option("--share", is_flag=True, help="Safe to post: serial -> short id; no device names, paths, hostname.")
def report_cmd(drive, as_html, output, share):
    """One drive's latest result as an HTML page: card, read-time histogram, disk map, history."""
    from . import html as hp, results as res
    h = res.find(drive)
    if not h:
        raise click.ClickException(f"no results for {drive!r} yet")
    page = hp.render(h, share=share)
    if output:
        with open(output, "w") as f:
            f.write(page)
        console.print(f"wrote {output}" + (" (shareable: no serial, paths or hostname)" if share else ""))
    else:
        click.echo(page)


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
    from .passes import PassError, run_pass
    _require_root()
    d = _probe(device)
    dash = Dashboard(d)
    try:
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            res = run_pass(d, mode, resume=resume, max_temp=max_temp, chunk=chunk, hash_data=hash_data,
                           on_state=lambda s: live.update(dash.render(s)), save_interval=SAVE_INTERVAL)
    except PassError as e:
        raise click.ClickException(str(e))
    if res.error:
        console.print(f"[red]{res.error}[/]")
    st = res.state
    if st is None:
        return
    final_report(console, st, interrupted=not res.finished)
    _analysis(st, d, interrupted=not res.finished, path=res.report)
    if not res.finished:
        console.print(f"Progress saved. Resume with: sudo nandwash {mode} {device} --resume")
        sys.exit(130 if res.interrupted else 1)


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
@click.option("--device", default="", help="Device to re-read (default: the path recorded in the report). Serial must match.")
def pick_slow_cmd(scan_report, out_file, factor, max_chunks, expect_serial, device):
    """Select chunks that are persistently slow: candidates from a scan report, re-read 3x here (read-only)."""
    import json
    from .pick import pick_slow
    _require_root()
    try:
        res, _info = pick_slow(scan_report, factor, max_chunks, expect_serial or "", device=device)
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



@main.group()
def preboot():
    """The offline maintenance boot: check this machine, preview an install. (Nothing here writes anything.)"""


_ICON = {"ok": "[green]ok[/]", "warn": "[yellow]WARN[/]", "fail": "[red]FAIL[/]", "info": "[dim]info[/]"}


@preboot.command("check")
@click.argument("device", required=False, type=click.Path(exists=True))
@click.option("--from-scan", "scan_report", type=click.Path(exists=True, dir_okay=False),
              help="Live scan report to work from (default: the newest completed scan of DEVICE's serial).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def preboot_check(device, scan_report, as_json):
    """Can this machine run the maintenance boot? Read-only; prints a verdict and a reason per finding."""
    from . import preboot as pb
    rep = pb.check(device, scan_report=scan_report)
    if as_json:
        import json
        click.echo(json.dumps(rep.to_dict(), indent=1))
    else:
        for c in rep.checks:
            console.print(f"{_ICON[c.status]:>14}  [bold]{c.name}[/]: {c.detail}", highlight=False)
        col = {"supported": "green", "supported-with-warnings": "yellow", "unsupported": "red"}[rep.verdict]
        console.print(f"\nVerdict: [bold {col}]{rep.verdict}[/]")
    sys.exit(0 if not rep.failures else 2)


@preboot.command("install")
@click.argument("device", required=False, type=click.Path(exists=True))
@click.option("--from-scan", "scan_report", type=click.Path(exists=True, dir_okay=False),
              help="Live scan report to work from (default: the newest completed scan of DEVICE's serial).")
@click.option("--dry-run", is_flag=True, help="Show exactly what would be written, and write nothing.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def preboot_install(device, scan_report, dry_run, yes):
    """Install the one-shot maintenance boot for DEVICE (or preview it with --dry-run).

    Installing writes the runner, its config, a copy of the scan report and one GRUB entry; it does NOT
    reboot and does not select the entry. Boot it once with the command printed at the end."""
    from . import preboot as pb
    if not dry_run:
        _require_root()
    device = _choose_device(device)
    rep = pb.check(device, scan_report=scan_report)
    try:
        pl = pb.plan(rep)
    except ValueError as e:
        for c in rep.checks:
            if c.status in ("fail", "warn"):
                console.print(f"{_ICON[c.status]:>14}  [bold]{c.name}[/]: {c.detail}", highlight=False)
        raise click.ClickException(str(e))
    for c in rep.warnings:
        console.print(f"{_ICON['warn']:>14}  [bold]{c.name}[/]: {c.detail}", highlight=False)
    console.print("\n[bold]DRY RUN: nothing has been written.[/] An install would:\n" if dry_run else
                  "\n[bold]This install writes:[/]\n")
    for fl in pl["files"]:
        console.print(f"[bold]{fl['action']:>22}[/]  {fl['path']}  (mode {fl['mode']})", highlight=False)
        for ln in fl["content"].rstrip("\n").split("\n"):
            console.print(" " * 26 + ln, style="dim", highlight=False, markup=False)
    console.print(f"\n[bold]To run it later:[/] {pl['run']}")
    console.print("[bold]To undo:[/]")
    for u in pl["undo"]:
        console.print(f"  - {u}", highlight=False)
    console.print("[bold]Left untouched:[/] " + "; ".join(pl["untouched"]))
    if dry_run:
        return
    if not yes:
        click.confirm("Install the maintenance boot now? (nothing reboots)", abort=True)
    try:
        pb.apply(pl, log=lambda m: console.print(f"  {m}", highlight=False))
    except (OSError, ValueError) as e:
        raise click.ClickException(f"install failed part-way: {e}. Undo with `nandwash preboot uninstall`.")
    console.print(f"[green]Installed.[/] Nothing changes until you boot it once: [bold]{pl['run']}[/]")


@preboot.command("uninstall")
@click.option("--grub-dir", default="/boot/grub", show_default=True, type=click.Path(file_okay=False))
def preboot_uninstall(grub_dir):
    """Remove everything `preboot install` wrote. Results under /var/lib/nandwash-maint are kept."""
    from . import preboot as pb
    _require_root()
    pb.uninstall(grub_dir, log=lambda m: console.print(f"  {m}", highlight=False))
    console.print("[green]Uninstalled.[/]")


if __name__ == "__main__":
    main()
