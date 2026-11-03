import json
import os
import tempfile
import unittest
from unittest import mock
from array import array

from nandwash import history, offline, results, state

CS = 1048576
SER = "13080900A1B2"
N = 2000                                   # chunks in the fake scan


def _w(path, obj, mode=0o644):
    with open(path, "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))
    os.chmod(path, mode)


class Offline(unittest.TestCase):
    """The mini sdb story of 2026-11-03: a live refresh queues the used chunks, the offline boot rewrites them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = state.VAR_DIR
        state.VAR_DIR = os.path.join(self.tmp.name, "var")
        self.maint = os.path.join(self.tmp.name, "maint")
        rd = os.path.join(state.VAR_DIR, "reports")
        os.makedirs(rd)
        os.makedirs(self.maint)
        lat = array("f", [2300.0] * N)
        for i in range(100, 130):
            lat[i] = 7500.0
        with open(os.path.join(rd, f"{SER}-scan-20261103T091245.lat"), "wb") as f:
            lat.tofile(f)
        _w(os.path.join(rd, f"{SER}-scan-20261103T091245.json"),
           {"serial": SER, "complete": True, "latency_file": f"{SER}-scan-20261103T091245.lat",
            "total_bytes": N * CS, "chunk_size": CS})
        history.add_run("nw-9e1c2a", SER, "Micron_M500", {
            "date": "2026-11-03T09:50:00", "job": "j-live", "goal": "refresh", "state": "slow-regions",
            "confirmed": 445, "outcome": {"outcome": "incomplete", "rewritten": 274, "fixed": 273, "fixed_pct": 99.6,
                                          "queued": 169, "verified": 274, "mismatch": 0,
                                          "median_before_ms": 7.41, "median_after_ms": 2.42,
                                          "fixed_offsets": [5 * CS]},
            "next": "sudo nandwash preboot install /dev/sdb"})

    def tearDown(self):
        state.VAR_DIR = self._old
        self.tmp.cleanup()

    def boot(self, ts="20261103T100417", conf=True, fixed=29, abort="", mode=0o644, dmode=0o755):
        d = os.path.join(self.maint, ts)
        os.makedirs(d)
        offs = [i * CS for i in range(100, 130)]
        if abort:
            _w(os.path.join(d, "log"), f"[nandwash-maint] start: serial={SER} pick=464s\n"
                                       f"[nandwash-maint] ABORT: no unique disk with serial '{SER}' (installed for "
                                       f"/dev/sdb); nothing touched\n[nandwash-maint] pipeline done (rc 3)\n", mode)
        else:
            _w(os.path.join(d, "log"), f"[nandwash-maint] start: serial={SER} pick=464s\n"
                                       "[nandwash-maint] pipeline done (rc 0)\n", mode)
            _w(os.path.join(d, "slow.json"), {"offsets": offs, "median_us": 2300.0, "threshold_us": 4140.0,
                                              "candidates": 30, "chunk_size": CS}, mode)
            _w(os.path.join(d, "report.json"), {
                "device": "/dev/sdb", "serial": SER, "chunks_requested": 30, "chunks_ok": 30, "stopped": None,
                "results": [{"offset": o, "status": "ok", "before_ms": 7.5, "after_ms": 2.4 if k < fixed else 7.4}
                            for k, o in enumerate(offs)]}, mode)
        if conf:
            _w(os.path.join(d, "maint.conf"), f"SERIAL='{SER}'\nDEVICE_HINT=/dev/sdb\n"
                                              f"SCAN_REPORT='/var/lib/nandwash-maint/input/{SER}-scan-20261103T091245.json'\n",
                                              mode)
        os.chmod(d, dmode)
        return d

    def run_import(self):
        msgs = []
        return offline.import_all(self.maint, log=msgs.append), msgs

    def test_boot_becomes_an_offline_run_with_the_combined_outcome(self):
        self.boot()
        got, msgs = self.run_import()
        self.assertEqual(len(got), 1, msgs)
        did, row = got[0]
        self.assertEqual((did, row["goal"], row["job"]), ("nw-9e1c2a", "offline", "offline-20261103T100417"))
        self.assertEqual((row["outcome"]["outcome"], row["outcome"]["fixed"], row["outcome"]["rewritten"]),
                         ("improved", 29, 30))
        self.assertEqual(row["state"], "slow-regions")           # 30 confirmed on 2000 chunks, line 20
        self.assertEqual(row["date"], "2026-11-03T10:04:17")
        cb = row["combined"]
        self.assertEqual((cb["outcome"], cb["fixed"], cb["rewritten"], cb["with_job"]), ("improved", 302, 304, "j-live"))
        self.assertTrue(row["scan_report"].endswith(f"{SER}-scan-20261103T091245.json"))
        h = history.load("nw-9e1c2a")
        c = dict(results.card(h))
        self.assertIn("29/30 fixed", c["OUTCOME"])
        self.assertIn("302/304", c["OUTCOME"])
        self.assertNotIn("preboot install", c.get("NEXT", ""))   # the queued chunks are done now
        self.assertEqual(self.run_import()[0], [])               # once only
        self.assertEqual(len(history.load("nw-9e1c2a")["runs"]), 2)
        self.assertEqual(offline.pending(self.maint), [])

    def test_boot_from_before_v04_finds_its_scan_by_serial_and_time(self):
        self.boot(conf=False)
        (_, row), = self.run_import()[0]
        self.assertTrue(row["scan_report"].endswith("20261103T091245.json"))
        self.assertEqual(row["state"], "slow-regions")

    def test_world_writable_results_are_refused(self):
        d = self.boot(mode=0o666, dmode=0o777)                   # what PID 1's umask 0 made on the mini
        got, msgs = self.run_import()
        self.assertEqual(got, [])
        self.assertIn("writable by group/other", msgs[0])
        self.assertIn(f"chmod -R go-w {d}", msgs[0])
        self.assertEqual(len(offline.pending(self.maint)), 1)     # still pending: imported after the chmod
        for root, dirs, files in os.walk(d):
            for n in dirs + files:
                os.chmod(os.path.join(root, n), 0o755 if n in dirs else 0o644)
        os.chmod(d, 0o755)
        self.assertEqual(len(self.run_import()[0]), 1)

    def test_symlinked_file_is_refused(self):
        d = self.boot()
        os.remove(os.path.join(d, "report.json"))
        os.symlink("/etc/hostname", os.path.join(d, "report.json"))
        self.assertIn("symlink", self.run_import()[1][0])

    def test_aborted_boot_is_recorded_without_names(self):
        self.boot(abort="x")
        (_, row), = self.run_import()[0]
        self.assertIsNone(row["outcome"])
        self.assertIn("no unique disk", row["why"])
        self.assertNotIn(SER, row["why"])
        self.assertNotIn("/dev/sdb", row["why"])
        self.assertIn("log", results.next_step(history.load("nw-9e1c2a"), row))

    def test_unknown_drive_is_left_pending(self):
        os.remove(history._path("nw-9e1c2a"))
        self.boot()
        with mock.patch.object(offline, "_drive_for", return_value=("", "")):
            got, msgs = self.run_import()
        self.assertEqual(got, [])
        self.assertIn("not attached", msgs[0])

    def test_history_stays_in_date_order(self):
        history.add_run("nw-9e1c2a", SER, "Micron_M500", {"date": "2026-11-03T12:00:00", "job": "j-check",
                                                          "goal": "check", "state": "healthy", "confirmed": 3})
        self.boot()
        self.run_import()
        self.assertEqual([r["job"] for r in history.load("nw-9e1c2a")["runs"]],
                         ["j-live", "offline-20261103T100417", "j-check"])


if __name__ == "__main__":
    unittest.main()
