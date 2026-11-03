"""Jobs (PLAN A5): a validated plan run one drive at a time, resumable, observable through job.json.

    /var/lib/nandwash/jobs/<id>/job.json   plan + per-drive status/step/progress/results, rewritten atomically
    /var/lib/nandwash/jobs/<id>/cancel     flag file: the runner stops at the next chunk boundary
    /run/nandwash.lock                     flock held by the one running job

No daemon, no sockets: the runner writes job.json, `status` reads it.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from datetime import datetime
from typing import Callable, List, Optional

from . import health, history as hist, plan as pl, state

JOB_VERSION = 1
LOCK_PATH = os.environ.get("NANDWASH_LOCK", "/run/nandwash.lock")
WRITE_EVERY_S = 2.0
CHECKPOINT_S = 10.0              # pass progress saved this often: a SIGKILL loses at most this much
# step -> drive status shown while it runs
_STATUS = {"scan": "scanning", "pick": "picking", "live-free": "rewriting (live)", "rewrite-chunks": "rewriting",
           "queue-offline": "queueing", "full-refresh": "refreshing"}
FINAL = ("done", "skipped", "failed", "cancelled")


class JobError(RuntimeError):
    pass


def jobs_dir() -> str:
    return os.path.join(state.VAR_DIR, "jobs")


def job_dir(job_id: str) -> str:
    if not job_id or "/" in job_id or job_id.startswith("."):
        raise JobError(f"bad job id {job_id!r}")
    return os.path.join(jobs_dir(), job_id)


def new_id(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create(p: pl.Plan, job_id: Optional[str] = None) -> dict:
    jid = job_id or new_id()
    job = {"nandwash_job": JOB_VERSION, "id": jid, "goal": p.goal, "created": _now(), "updated": _now(),
           "state": "queued", "pid": None, "message": "",
           "refused": [list(r) for r in p.refused],
           "drives": [{"serial": d.serial, "model": d.model, "size": d.size, "id": d.id, "name": d.name,
                       "system": d.system, "steps": list(d.steps), "notes": list(d.notes), "est_s": d.est_s,
                       "step": 0, "status": "queued", "progress": {}, "results": {}, "error": ""}
                      for d in p.drives]}
    os.makedirs(job_dir(jid), mode=0o755, exist_ok=False)
    with open(os.path.join(job_dir(jid), "plan.toml"), "w") as f:
        f.write(pl.to_toml(p))
    save(job)
    return job


def save(job: dict) -> None:
    job["updated"] = _now()
    path = os.path.join(job_dir(job["id"]), "job.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def load(job_id: str) -> dict:
    try:
        with open(os.path.join(job_dir(job_id), "job.json")) as f:
            job = json.load(f)
    except FileNotFoundError:
        raise JobError(f"no job {job_id}")
    except (OSError, ValueError) as e:
        raise JobError(f"job {job_id}: unreadable job.json ({e})")
    if job.get("nandwash_job") != JOB_VERSION:
        raise JobError(f"job {job_id}: unsupported version {job.get('nandwash_job')!r}")
    return job


def list_ids() -> List[str]:
    try:
        return sorted(x for x in os.listdir(jobs_dir()) if os.path.isfile(os.path.join(jobs_dir(), x, "job.json")))
    except OSError:
        return []


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def effective_state(job: dict) -> str:
    """A job that says running but whose process is gone was killed (power cut, SIGKILL): resumable."""
    if job["state"] == "running" and not _alive(job.get("pid")):
        return "interrupted"
    return job["state"]


def resumable(job: dict) -> bool:
    return effective_state(job) in ("paused", "interrupted", "queued") and \
        any(d["status"] not in FINAL for d in job["drives"])


def latest(pred: Optional[Callable[[dict], bool]] = None) -> Optional[dict]:
    for jid in reversed(list_ids()):
        try:
            j = load(jid)
        except JobError:
            continue
        if pred is None or pred(j):
            return j
    return None


def request_cancel(job_id: str) -> None:
    open(os.path.join(job_dir(job_id), "cancel"), "w").close()


def cancel_requested(job_id: str) -> bool:
    return os.path.exists(os.path.join(job_dir(job_id), "cancel"))


class Lock:
    """Machine-wide: one job (or one write command) at a time."""

    def __init__(self, path: Optional[str] = None):
        self.path, self.f = path or LOCK_PATH, None

    def __enter__(self):
        self.f = open(self.path, "a+")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.seek(0)
            who = self.f.read().strip()
            self.f.close()
            raise JobError("another nandwash job is running" + (f" ({who})" if who else "") +
                           "; see `nandwash status`")
        self.f.seek(0)
        self.f.truncate()
        self.f.write(f"pid {os.getpid()}")
        self.f.flush()
        return self

    def __exit__(self, *exc):
        try:
            self.f.truncate(0)
        finally:
            self.f.close()


# ---------------------------------------------------------------------------------------------- runner
class _Stop(Exception):
    def __init__(self, how: str):
        self.how = how          # "paused" | "cancelled"


class Runner:
    """Runs (or resumes) one job in this process. `on_update(job)` is called after every job.json write."""

    def __init__(self, job: dict, on_update: Optional[Callable[[dict], None]] = None,
                 log: Callable[[str], None] = lambda s: None, max_temp: int = 70,
                 enumerate_fn=None, ops=None):
        from .drives import enumerate_drives
        self.job, self.on_update, self.log, self.max_temp = job, on_update, log, max_temp
        self.enumerate = enumerate_fn or enumerate_drives
        self.ops = ops or Ops()
        self._last_write = 0.0
        self.dir = job_dir(job["id"])

    # -- job.json
    def write(self, force: bool = True) -> None:
        if not force and time.time() - self._last_write < WRITE_EVERY_S:
            return
        save(self.job)
        self._last_write = time.time()
        if self.on_update:
            self.on_update(self.job)

    def note(self, msg: str) -> None:
        self.job["message"] = msg
        self.log(msg)

    def should_stop(self) -> bool:
        return cancel_requested(self.job["id"])

    # -- whole job
    def run(self) -> str:
        job = self.job
        with Lock():
            job["state"], job["pid"] = "running", os.getpid()
            job.pop("finished", None)
            self.write()
            try:
                self._run_drives()
                errs = [d for d in job["drives"] if d["status"] == "failed"]
                job["state"] = "failed" if errs and len(errs) == len(job["drives"]) else "done"
                self.note("finished" + (f" with {len(errs)} failed drive(s)" if errs else ""))
            except _Stop as s:
                job["state"] = s.how
                self.note("cancelled" if s.how == "cancelled" else "paused: `nandwash resume` continues")
            except KeyboardInterrupt:
                job["state"] = "paused"
                self.note("paused: `nandwash resume` continues")
            finally:
                if job["state"] in ("done", "failed", "cancelled"):
                    job["finished"] = _now()
                    try:
                        os.unlink(os.path.join(self.dir, "cancel"))
                    except OSError:
                        pass
                job["pid"] = None
                self.write()
        if job["state"] in ("done", "failed") and job.get("on_result"):
            self._on_result()
        return job["state"]

    def _on_result(self) -> None:
        """PLAN B7: the user's own command gets the results JSON on stdin. Its failure is logged, never fatal."""
        import subprocess
        try:
            data = json.dumps(result_json(self.job))
            r = subprocess.run(self.job["on_result"], shell=True, input=data, text=True, capture_output=True,
                               timeout=300)
            self.log(f"on-result exited {r.returncode}" + (f": {r.stderr.strip()[-300:]}" if r.returncode else ""))
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"on-result failed: {e}")

    def _run_drives(self) -> None:
        job = self.job
        todo = [jd for jd in job["drives"] if jd["status"] not in FINAL]
        if not todo:
            return
        p = pl.Plan(goal=job["goal"], drives=[pl.DrivePlan(serial=jd["serial"], model=jd["model"], size=jd["size"],
                                                            id=jd["id"], steps=jd["steps"][jd["step"]:])
                                               for jd in todo])
        # A resumed drive may start at a write step; its pick ran earlier in this job.
        for dp, jd in zip(p.drives, todo):
            if jd["step"] and "pick" in jd["steps"][:jd["step"]]:
                dp.steps = ["pick"] + dp.steps
        pairs, problems = pl.validate(p, self.enumerate())
        ok = {dp.serial for dp, _ in pairs}
        for jd in todo:
            if jd["serial"] not in ok:
                why = next((x for x in problems if x.startswith(jd["serial"])), "not valid now")
                jd["status"], jd["error"] = "skipped", why
                self.log(f"skip {jd['serial']}: {why}")
        self.write()
        drive_of = {dp.serial: d for dp, d in pairs}
        for jd in todo:
            if jd["serial"] in drive_of:
                if self.should_stop():
                    raise _Stop("cancelled")
                self._run_drive(jd, drive_of[jd["serial"]])

    # -- one drive
    def _run_drive(self, jd: dict, d) -> None:
        jd["name"] = d.name
        while jd["step"] < len(jd["steps"]):
            step = jd["steps"][jd["step"]]
            if pl.STEPS[step] and step in pl.WRITES:
                d = self._recheck(jd, d, pl.STEPS[step])
                if d is None:
                    return
            jd["status"] = _STATUS[step]
            jd["progress"] = {}
            self.note(f"{d.name}: {step}")
            self.write()
            try:
                getattr(self, "_step_" + step.replace("-", "_"))(jd, d)
            except _Stop as s:
                jd["status"] = "paused" if s.how == "paused" else "cancelled"
                self.write()
                raise
            except KeyboardInterrupt:
                jd["status"] = "paused"
                raise
            except Exception as e:          # one drive failing never takes the job's other drives with it
                jd["status"], jd["error"] = "failed", f"{step}: {e}"
                self.log(f"{d.name} {step} failed: {e}")
                self.write()
                return
            jd["step"] += 1
            self.write()
        jd["status"], jd["progress"] = "done", {}
        try:
            self._finish(jd, d)
        except Exception as e:              # the work is done; a history problem must not fail the drive
            self.log(f"{d.name}: could not record the result: {e}")
        self.write()

    def _finish(self, jd: dict, d) -> None:
        """PLAN B3/B5: the drive's state + refresh outcome, and one history row."""
        r = jd["results"]
        pk = r.get("pick") or {}
        med_us = (pk.get("median_ms") or 0) * 1000.0
        rows = []
        for key in ("live_free", "rewrite_chunks"):
            f = (r.get(key) or {}).get("file")
            if f:
                with open(f) as fh:
                    rows += health.rewrite_rows(json.load(fh))
        oc = health.outcome(rows, med_us, queued=len(r.get("offline_queue") or [])) if med_us else None
        if oc:
            r["outcome"] = {k: v for k, v in oc.items() if k != "fixed_offsets"}
        scan = {}
        src = (r.get("scan") or r.get("full_refresh") or {}).get("report") or pk.get("scan")
        if src:
            try:
                with open(src) as fh:
                    scan = json.load(fh)
            except (OSError, ValueError):
                pass
        ls = scan.get("latency_stats") or {}
        state_ = pk.get("state")
        if not pk and scan:                  # refresh-full: no re-reads, only errors can be judged
            n = len(scan.get("error_sectors") or [])
            state_ = "errors" if n else None
        row = {"date": _now(), "job": self.job["id"], "device": d.name, "goal": self.job["goal"], "steps": jd["steps"],
               "scan_report": src, "slow_file": pk.get("file"), "complete": scan.get("complete"),
               "median_ms": round(ls["p50"] / 1000, 3) if ls.get("p50") else None,
               "p99_ms": round(ls["p99"] / 1000, 3) if ls.get("p99") else None,
               "errors": len(scan.get("error_sectors") or []), "noise": pk.get("noise"),
               "candidates": pk.get("candidates"), "confirmed": pk.get("confirmed"), "severe": pk.get("severe"),
               "state": state_, "why": pk.get("why", ""), "slow_again": pk.get("slow_again"),
               "smart_counters": pk.get("smart_counters") or health.smart_counters(scan.get("smart_after")),
               "smart_notes": pk.get("smart_notes") or [],
               "outcome": oc, "next": (r.get("queue_offline") or {}).get("next", "")}
        jd["health"] = state_
        jd["exit"] = health.exit_code(state_, (oc or {}).get("outcome"))
        hist.add_run(jd["id"] or jd["serial"], jd["serial"], jd["model"], row)

    def _recheck(self, jd: dict, d, cap: str):
        """Right before a write: the scan may have taken hours, so look at the drive again."""
        now = [x for x in self.enumerate() if x.serial == jd["serial"]]
        if len(now) != 1 or now[0].model != jd["model"] or now[0].size != jd["size"]:
            jd["status"], jd["error"] = "failed", "drive changed or vanished before the write step"
        elif not now[0].can(cap):
            jd["status"], jd["error"] = "failed", f"no longer allowed ({cap}): " + "; ".join(now[0].caps[cap].reasons)
        else:
            return now[0]
        self.log(f"{jd['serial']}: {jd['error']}")
        self.write()
        return None

    def _pass(self, jd: dict, d, mode: str) -> dict:
        def on_state(st):
            el = max(time.time() - st.start_time, 1e-6)
            done = st.current_offset - st.start_offset
            rate = done / el
            jd["progress"] = {"pct": round(100.0 * st.current_offset / st.total_bytes, 2) if st.total_bytes else 0,
                              "mbps": round(rate / 1e6, 1),
                              "eta_s": round((st.total_bytes - st.current_offset) / rate) if rate > 0 else None,
                              "temp": st.smart.temperature if st.smart else None,
                              "errors": len(st.error_sectors), "throttled": st.throttled}
            self.write(force=False)
        # Resume only a pass THIS job started (and whose progress file is still there).
        resume = bool(jd["results"].get(mode + "_started")) and self.ops.saved_progress(d.serial, mode) is not None
        jd["results"][mode + "_started"] = True
        res = self.ops.run_pass(d, mode, resume=resume, max_temp=self.max_temp, on_state=on_state,
                                should_stop=self.should_stop, save_interval=CHECKPOINT_S)
        if res.error:
            raise RuntimeError(res.error)
        if not res.finished:
            raise _Stop("cancelled" if self.should_stop() else "paused")
        jd["results"].pop(mode + "_started", None)
        return {"report": str(res.report), "errors": len(res.state.error_sectors),
                "elapsed_s": round(time.time() - res.state.start_time)}

    def _out(self, jd: dict, name: str, data) -> str:
        path = os.path.join(self.dir, f"{jd['id'] or jd['serial']}-{name}.json")
        with open(path, "w") as f:
            json.dump(data, f, indent=1)
        return path

    def _step_scan(self, jd, d):
        jd["results"]["scan"] = self._pass(jd, d, "scan")

    def _step_full_refresh(self, jd, d):
        jd["results"]["full_refresh"] = self._pass(jd, d, "refresh")

    def _step_pick(self, jd, d):
        scan = jd["results"].get("scan", {}).get("report") or self.ops.find_scan_report(d.serial)
        if not scan:
            raise RuntimeError("no scan report to pick from")
        h = hist.load(jd["id"] or jd["serial"])
        res, verdict = self.ops.pick(scan, d, prev_counters=hist.last_counters(h))
        again = hist.slow_again(h, res["offsets"])
        jd["results"]["pick"] = {"file": self._out(jd, "slow", res), "scan": scan, "candidates": res["candidates"],
                                 **verdict, "slow_again": again}
        self.log(f"{d.name}: {verdict['confirmed']} confirmed slow ({verdict['severe']} above 3x): {verdict['state']}"
                 + (f"; {again['count']} of {again['of']} earlier fixes slow again" if again else ""))

    def _offsets(self, jd) -> list:
        with open(jd["results"]["pick"]["file"]) as f:
            return json.load(f)["offsets"]

    def _step_live_free(self, jd, d):
        offs = self._offsets(jd)
        if not offs:
            jd["results"]["live_free"] = {"chunks_ok": 0, "skipped": "nothing to rewrite"}
            return
        res = self.ops.live_free(d, offs, self.log, self.should_stop)
        if res.get("stopped") == "cancelled":
            raise _Stop("cancelled")
        used = [r["offset"] for r in res.get("plan", []) if r["state"] != "free"]
        jd["results"]["live_free"] = {"file": self._out(jd, "live-free", res), "chunks_ok": res.get("chunks_ok", 0),
                                      "states": res.get("states", {}), "median_before_ms": res.get("median_before_ms"),
                                      "median_after_ms": res.get("median_after_ms"),
                                      "max_frozen_ms": res.get("max_frozen_ms"), "stopped": res.get("stopped")}
        jd["results"]["offline_queue"] = used
        if res.get("stopped"):
            raise RuntimeError(res["stopped"])

    def _step_queue_offline(self, jd, d):
        q = jd["results"].get("offline_queue", [])
        scan = jd["results"].get("pick", {}).get("scan", "")
        jd["results"]["queue_offline"] = {
            "chunks": len(q),
            "next": (f"sudo nandwash preboot check {d.path} && sudo nandwash preboot install {d.path} "
                     f"--scan-report {scan}") if q else ""}

    def _step_rewrite_chunks(self, jd, d):
        offs = self._offsets(jd)
        if not offs:
            jd["results"]["rewrite_chunks"] = {"chunks_ok": 0, "skipped": "nothing to rewrite"}
            return
        res = self.ops.rewrite(d, offs, self.log, self.should_stop)
        if res["stopped"] == "cancelled":
            raise _Stop("cancelled")
        jd["results"]["rewrite_chunks"] = {"file": self._out(jd, "rewrite", res), "chunks_ok": res["chunks_ok"],
                                           "requested": res["chunks_requested"],
                                           "median_before_ms": res["median_before_ms"],
                                           "median_after_ms": res["median_after_ms"], "stopped": res["stopped"]}
        if res["stopped"]:
            raise RuntimeError(res["stopped"])


def exit_code(job: dict) -> int:
    """PLAN B7 for a whole job: the worst drive. 130 paused, otherwise 0/1/2/3 (health.exit_code);
    a drive that failed or was skipped counts as at least 1."""
    st = effective_state(job)
    if st in ("paused", "interrupted"):
        return 130
    worst = 0
    for d in job["drives"]:
        worst = max(worst, d.get("exit") or 0, 1 if d["status"] in ("failed", "skipped", "cancelled") else 0)
    return worst


def result_json(job: dict) -> dict:
    from . import results
    hs = [hist.load(d["id"] or d["serial"]) for d in job["drives"] if d["status"] == "done"]
    out = results.to_json([h for h in hs if h["runs"]])
    out.update(job=job["id"], job_state=job["state"], exit=exit_code(job))
    return out


class Ops:
    """The real work, behind one seam so the runner tests without hardware."""

    def saved_progress(self, serial, mode):
        from .passes import saved_progress
        return saved_progress(serial, mode)

    def run_pass(self, d, mode, **kw):
        from .passes import run_pass
        return run_pass(self._guard(d), mode, **kw)

    def find_scan_report(self, serial):
        from .preboot import find_scan_report
        return find_scan_report(serial)

    def pick(self, scan, d, prev_counters=None):
        from . import pick as pk, report as rpt
        res, info = pk.pick_slow(scan, max_chunks=10 ** 9, expect_serial=d.serial, device=d.path, log=lambda s: None,
                                 noise_n=health.NOISE_N)
        rep, lat = rpt.load(scan)
        return res, pk.verdict(res, lat, len(lat), rep=rep, prev_counters=prev_counters)

    @staticmethod
    def _guard(d):
        """Names move between enumerate and write: the device must still carry the serial."""
        from .device import DeviceInfo
        info = DeviceInfo.probe(d.path)
        if info.serial != d.serial or info.total_bytes != d.size:
            raise RuntimeError(f"{d.path} is now serial {info.serial} ({info.total_bytes} bytes), expected "
                               f"{d.serial}; refusing")
        return info

    def live_free(self, d, offsets, log, should_stop):
        from . import livefree
        self._guard(d)
        return livefree.refresh_free(d.path, offsets, log=log, max_chunks=10 ** 9, should_stop=should_stop)

    def rewrite(self, d, offsets, log, should_stop):
        from . import targeted
        info = self._guard(d)
        held = targeted.holders_of(info)
        rw = [m for m in targeted.mounts_of(info) if m[2]]
        if held or rw:
            raise RuntimeError("drive is in use now: " + ", ".join(held + [f"{s} on {t}" for s, t, _ in rw]))
        return targeted.refresh_chunks(d.path, offsets, log=log, should_stop=should_stop)


__all__ = ["create", "load", "save", "list_ids", "latest", "Runner", "Lock", "JobError", "request_cancel",
           "effective_state", "resumable", "new_id"]
