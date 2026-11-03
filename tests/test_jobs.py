import os
import tempfile
import unittest
from types import SimpleNamespace

from nandwash import jobs, pick, plan as pl, selection as sel, state
from test_drives import Selectors, drive, part


class FakeOps:
    """Records calls; `pause_scans` = how many scan passes stop early (as Ctrl+C would)."""

    def __init__(self, pause_scans=0, slow=(0, 1048576), used=(1048576,)):
        self.calls, self.pause_scans, self.slow, self.used = [], pause_scans, list(slow), set(used)
        self.progress = set()

    def saved_progress(self, serial, mode):
        return {"mode": mode} if (serial, mode) in self.progress else None

    def run_pass(self, d, mode, resume=False, on_state=None, should_stop=None, **kw):
        self.calls.append(("pass", d.serial, mode, resume))
        st = SimpleNamespace(start_time=0, current_offset=d.size // 2, start_offset=0, total_bytes=d.size,
                             smart=None, error_sectors=[], throttled=False)
        on_state(st)
        if should_stop():
            return SimpleNamespace(finished=False, interrupted=True, error="", state=st, report=None)
        if self.pause_scans:
            self.pause_scans -= 1
            self.progress.add((d.serial, mode))
            return SimpleNamespace(finished=False, interrupted=True, error="", state=st, report=None)
        self.progress.discard((d.serial, mode))
        return SimpleNamespace(finished=True, interrupted=False, error="", state=st, report=f"/r/{d.serial}.json")

    def find_scan_report(self, serial):
        return None

    def pick(self, scan, d):
        self.calls.append(("pick", d.serial, scan))
        res = {"offsets": self.slow, "candidates": 5, "chunk_size": 1048576, "median_us": 1000.0}
        return res, pick.verdict(res, [3000.0] * 10, 10)

    def live_free(self, d, offsets, log, should_stop):
        self.calls.append(("live", d.serial, tuple(offsets)))
        plan = [{"offset": o, "state": "used" if o in self.used else "free"} for o in offsets]
        return {"plan": plan, "chunks_ok": sum(r["state"] == "free" for r in plan), "states": {}, "stopped": None}

    def rewrite(self, d, offsets, log, should_stop):
        self.calls.append(("rewrite", d.serial, tuple(offsets)))
        return {"chunks_ok": len(offsets), "chunks_requested": len(offsets), "median_before_ms": 7.0,
                "median_after_ms": 2.0, "stopped": None}


class Jobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = state.VAR_DIR, jobs.LOCK_PATH
        state.VAR_DIR = self.tmp.name
        jobs.LOCK_PATH = os.path.join(self.tmp.name, "lock")
        S = Selectors()
        S.setUp()
        self.S = S

    def tearDown(self):
        state.VAR_DIR, jobs.LOCK_PATH = self._old
        self.tmp.cleanup()

    def job(self, selector, goal):
        p = pl.make(sel.resolve(self.S.ds, selector, write=goal != "check"), goal, rate_of=None)
        return jobs.create(p)

    def runner(self, job, ops, ds=None):
        return jobs.Runner(job, ops=ops, enumerate_fn=lambda: ds if ds is not None else self.S.ds)

    def test_check_job(self):
        ops = FakeOps()
        j = self.job("sda,sdc", "check")
        self.assertEqual(self.runner(j, ops).run(), "done")
        j = jobs.load(j["id"])
        self.assertEqual([d["status"] for d in j["drives"]], ["done", "done"])
        self.assertEqual(j["drives"][0]["results"]["pick"]["confirmed"], 2)
        self.assertTrue(os.path.exists(j["drives"][0]["results"]["pick"]["file"]))
        self.assertFalse(any(c[0] in ("live", "rewrite") for c in ops.calls))
        self.assertIsNone(j["pid"])

    def test_refresh_live_and_offline(self):
        ops = FakeOps()
        j = self.job("sda,sdc", "refresh")
        self.runner(j, ops).run()
        self.assertIn(("live", "DATA", (0, 1048576)), ops.calls)
        self.assertIn(("rewrite", "TB", (0, 1048576)), ops.calls)
        a = j["drives"][0]["results"]
        self.assertEqual(a["offline_queue"], [1048576])
        self.assertEqual(a["queue_offline"]["chunks"], 1)
        self.assertIn("preboot install /dev/sda", a["queue_offline"]["next"])

    def test_pause_and_resume(self):
        ops = FakeOps(pause_scans=1)
        j = self.job("sda,sdc", "check")
        self.assertEqual(self.runner(j, ops).run(), "paused")
        j = jobs.load(j["id"])
        self.assertTrue(jobs.resumable(j))
        self.assertEqual(j["drives"][0]["status"], "paused")
        self.assertEqual(j["drives"][1]["status"], "queued")
        self.assertEqual(self.runner(j, ops).run(), "done")
        self.assertEqual([c for c in ops.calls if c[0] == "pass"],
                         [("pass", "DATA", "scan", False), ("pass", "DATA", "scan", True),
                          ("pass", "TB", "scan", False)])

    def test_resume_at_write_step_repicks(self):
        ops = FakeOps()
        j = self.job("sdc", "refresh")
        j["drives"][0]["step"] = 2                  # scan + pick done earlier, stopped before rewrite
        j["drives"][0]["status"] = "paused"
        j["drives"][0]["results"]["pick"] = {"file": self._slow_file(j), "confirmed": 1}
        jobs.save(j)
        self.assertEqual(self.runner(j, ops).run(), "done")
        self.assertEqual([c[0] for c in ops.calls], ["rewrite"])

    def _slow_file(self, j):
        path = os.path.join(jobs.job_dir(j["id"]), "x-slow.json")
        with open(path, "w") as f:
            f.write('{"offsets": [4096]}')
        return path

    def test_cancel(self):
        ops = FakeOps()
        j = self.job("sda,sdc", "check")
        jobs.request_cancel(j["id"])
        self.assertEqual(self.runner(j, ops).run(), "cancelled")
        self.assertFalse(jobs.cancel_requested(j["id"]))
        self.assertFalse(jobs.resumable(jobs.load(j["id"])))

    def test_vanished_drive_skipped_others_run(self):
        ops = FakeOps()
        j = self.job("sda,sdc", "check")
        self.runner(j, ops, ds=[self.S.tb]).run()
        self.assertEqual(j["drives"][0]["status"], "skipped")
        self.assertIn("not attached", j["drives"][0]["error"])
        self.assertEqual(j["drives"][1]["status"], "done")

    def test_write_rechecked_after_scan(self):
        j = self.job("sdc", "refresh")
        ops = FakeOps()
        mounted = drive("sdc", "TB", model="T-FORCE 512GB", tb=True, parts=[part("sdc1", "ext4", [("/x", True)])])
        calls = {"n": 0}

        def enum():                                 # validated unmounted, then mounted during the scan
            calls["n"] += 1
            return self.S.ds if calls["n"] == 1 else [mounted]
        jobs.Runner(j, ops=ops, enumerate_fn=enum).run()
        self.assertEqual(j["drives"][0]["status"], "failed")
        self.assertIn("no longer allowed", j["drives"][0]["error"])
        self.assertFalse(any(c[0] == "rewrite" for c in ops.calls))

    def test_lock_and_stale_running(self):
        with jobs.Lock():
            with self.assertRaises(jobs.JobError):
                with jobs.Lock():
                    pass
        j = self.job("sda", "check")
        j["state"], j["pid"] = "running", 2 ** 22 + 12345       # no such process
        self.assertEqual(jobs.effective_state(j), "interrupted")
        self.assertTrue(jobs.resumable(j))
        self.assertEqual(jobs.latest()["id"], j["id"])


class ReportNames(unittest.TestCase):
    def test_dotted_serial_keeps_its_name_and_never_overwrites(self):
        from array import array
        from pathlib import Path
        from nandwash import report as rpt
        with tempfile.TemporaryDirectory() as d:
            old, rpt.REPORT_DIR = rpt.REPORT_DIR, Path(d)
            try:
                rep = {"serial": "AB.12", "mode": "scan", "created": "2026-11-03T10:00:00", "total_bytes": 4, "chunk_size": 4}
                a = rpt.save(dict(rep), array("f", [1.0]))
                b = rpt.save(dict(rep), array("f", [2.0]))
            finally:
                rpt.REPORT_DIR = old
            self.assertEqual((a.name, b.name), ("AB.12-scan-20261103T100000.json", "AB.12-scan-20261103T100000-2.json"))
            self.assertEqual(rpt.load(str(b))[1][0], 2.0)


class Verdict(unittest.TestCase):
    def test_rule(self):
        cs, med = 1048576, 1000.0
        lat = [1000.0] * 1_000_000
        few = {"offsets": [i * cs for i in range(5)], "chunk_size": cs, "median_us": med}
        for i in range(5):
            lat[i] = 1900.0
        self.assertEqual(pick.verdict(few, lat, len(lat))["state"], "healthy")
        many = {"offsets": [i * cs for i in range(100)], "chunk_size": cs, "median_us": med}
        self.assertEqual(pick.verdict(many, lat, len(lat))["state"], "slow-regions")    # line = max(20, 100)
        for i in range(5):
            lat[i] = 3100.0
        v = pick.verdict(few, lat, len(lat))
        self.assertEqual((v["state"], v["severe"]), ("slow-regions", 5))


if __name__ == "__main__":
    unittest.main()
