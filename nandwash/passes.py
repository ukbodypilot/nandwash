"""One whole-surface pass (scan or full refresh), resumable, with no UI (PLAN A5).

`cli._execute` (the old `scan`/`refresh` commands) and the job runner both drive passes through `run_pass`:
progress is checkpointed to progress.py every `save_interval` seconds, a report is saved at the end
(complete or not), and the caller sees every EngineState through `on_state`.
"""
from __future__ import annotations

import time
from array import array
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import progress as prog, report as rpt
from .engine import run as engine_run


class PassError(ValueError):
    pass


@dataclass
class PassResult:
    state: object = None            # last EngineState (None: the engine yielded nothing)
    finished: bool = False          # whole surface done
    interrupted: bool = False       # stopped by Ctrl+C or should_stop(): progress saved, resumable
    error: str = ""                 # I/O failure text; progress saved
    report: Optional[Path] = None


def saved_progress(serial: str, mode: str) -> Optional[dict]:
    s = prog.load(serial)
    return s if s and s.get("mode") == mode else None


def run_pass(info, mode: str, resume: bool = False, max_temp: int = 70, chunk: int = 1048576,
             hash_data: bool = False, on_state: Optional[Callable] = None,
             should_stop: Optional[Callable[[], bool]] = None, save_interval: float = 60.0) -> PassResult:
    if hash_data and resume:
        raise PassError("--hash needs an uninterrupted whole-device pass; it cannot be used with --resume")
    state_in = None
    if resume:
        state_in = prog.load(info.serial)
        if not state_in:
            raise PassError(f"no saved progress for serial {info.serial}")
        if state_in.get("serial") != info.serial:
            raise PassError("saved progress belongs to a different drive (serial mismatch)")
        if state_in.get("mode") != mode:
            raise PassError(f"saved progress is for '{state_in.get('mode')}' mode, not '{mode}'")
        if state_in.get("total_bytes") != info.total_bytes:
            raise PassError("drive size differs from saved progress")
        chunk = state_in.get("chunk_size", chunk)
    if mode == "refresh":
        from .targeted import mounts_of
        m = mounts_of(info)
        if m:
            raise PassError("refusing to refresh a device with mounted filesystems:\n  " +
                            "\n  ".join(f"{s} on {t}" for s, t, _ in m))

    started_iso = (state_in or {}).get("start_time") or datetime.now().isoformat(timespec="seconds")

    def checkpoint(s):
        if s.latency_us is not None:
            prog.save_latency(info.serial, s.latency_us)
        prog.save(info.serial, {
            "device_path": info.path, "serial": info.serial, "mode": mode, "total_bytes": info.total_bytes,
            "current_offset": s.current_offset, "error_sectors": s.error_sectors, "chunk_size": chunk,
            "start_time": started_iso, "good_bytes": s.good_bytes, "written_bytes": s.written_bytes,
            "retry_count": s.retry_count,
            "smart_before": s.smart_before.to_dict() if s.smart_before else None,
        })

    lat = prog.load_latency(info.serial, -(-info.total_bytes // chunk)) if state_in else None
    res = PassResult()
    last_save = time.time()
    gen = engine_run(info.path, mode, start_offset=(state_in or {}).get("current_offset", 0), chunk_size=chunk,
                     max_temp=max_temp, resume=state_in, info=info, latency=lat, hash_data=hash_data)
    try:
        for st in gen:
            res.state = st
            if on_state:
                on_state(st)
            if time.time() - last_save >= save_interval:
                checkpoint(st)
                last_save = time.time()
            if should_stop and not st.finished and should_stop():
                res.interrupted = True
                break
    except KeyboardInterrupt:
        res.interrupted = True
    except OSError as e:
        res.error = f"I/O failure: {e}"
    finally:
        gen.close()

    st = res.state
    if st is None:
        return res
    res.finished = bool(st.finished) and not res.interrupted and not res.error
    if res.finished:
        prog.delete(info.serial)
        prog.delete_latency(info.serial)
    else:
        checkpoint(st)
    rep = rpt.build(st, info, not res.finished)
    res.report = rpt.save(rep, st.latency_us if st.latency_us is not None else array("f"))
    return res
