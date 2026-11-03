import tempfile
import unittest

from nandwash import history, results, state

ROW = {"date": "2026-11-03T14:02:00", "job": "j2", "device": "sda", "goal": "refresh", "complete": True,
       "median_ms": 2.21, "p99_ms": 3.1, "errors": 0, "noise": {"n": 200, "p5": 0.95, "p50": 1.0, "p95": 1.08,
                                                                "ok": True},
       "candidates": 4000, "confirmed": 1240, "severe": 300, "state": "slow-regions", "why": "1240 >= line 97",
       "slow_again": {"count": 14, "of": 1141, "offsets": [], "days_since_fix_min": 30.0, "days_since_fix_max": 30.0,
                      "again_days_median": 30.0},
       "smart_counters": {"5": 0, "197": 0},
       "outcome": {"outcome": "incomplete", "rewritten": 1141, "fixed": 1050, "fixed_pct": 92.0, "queued": 99,
                   "verified": 1141, "mismatch": 0, "median_before_ms": 4.3, "median_after_ms": 2.25,
                   "fixed_offsets": [0]},
       "next": "sudo nandwash preboot install /dev/sda --scan-report r.json"}

# The JSON schema (PLAN B6). Adding a key is fine; renaming or removing one breaks scripts: bump RESULT_VERSION.
KEYS = {"id", "alias", "serial", "model", "runs", "date", "goal", "state", "why", "outcome", "median_ms", "p99_ms",
        "noise", "candidates", "confirmed", "severe", "errors", "slow_again", "smart_counters", "next"}


class Results(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = state.VAR_DIR
        state.VAR_DIR = self.tmp.name
        history.add_run("nw-3f9a2c", "SER1", "Micron M600", {**ROW, "job": "j1", "confirmed": 1610,
                                                             "outcome": None, "slow_again": None})
        history.add_run("nw-3f9a2c", "SER1", "Micron M600", ROW)
        state.set_alias("data-m600", "nw-3f9a2c")

    def tearDown(self):
        state.VAR_DIR = self._old
        self.tmp.cleanup()

    def test_json_schema_is_pinned(self):
        j = results.to_json(history.all_drives())
        self.assertEqual(j["nandwash_result"], 1)
        d = j["drives"][0]
        self.assertTrue(KEYS <= set(d), KEYS - set(d))
        self.assertEqual((d["alias"], d["runs"], d["state"]), ("data-m600", 2, "slow-regions"))
        self.assertNotIn("fixed_offsets", d["outcome"])
        self.assertIn("preboot install", d["next"])
        self.assertIn("history", results.to_json(history.all_drives(), full=True)["drives"][0])

    def test_find_by_alias_id_serial_device(self):
        for sel in ("data-m600", "nw-3f9a2c", "id:nw-3f9a2c", "SER1", "sda"):
            self.assertEqual(results.find(sel)["id"], "nw-3f9a2c", sel)
        self.assertIsNone(results.find("nope"))

    def test_card(self):
        c = dict(results.card(history.load("nw-3f9a2c")))
        self.assertIn("1240 confirmed slow", c["STATE"])
        self.assertIn("1050/1141 fixed", c["OUTCOME"])
        self.assertIn("99 queued", c["OUTCOME"])
        self.assertIn("1141/1141", c["DATA"])
        self.assertIn("1610 -> 1240", c["HISTORY"])
        self.assertIn("14 of 1141 earlier fixes slow again", c["HISTORY"])
        self.assertIn("preboot install", c["NEXT"])
        self.assertIn("data-m600", c[""])

    def test_errors_card_says_back_up(self):
        history.add_run("nw-bad", "S2", "X", {**ROW, "job": "j9", "state": "errors",
                                              "why": "SMART 197 pending 0 -> 1", "outcome": None})
        c = dict(results.card(history.load("nw-bad")))
        self.assertIn("back up this drive now", c["NEXT"])
        self.assertIn("197", c["SMART"])

    def test_spark(self):
        self.assertEqual(results.spark([0, 5, 10]), "▁▄█")
        self.assertEqual(results.spark([3, 3]), "▁▁")
        self.assertEqual(results.spark([None]), "")


class Html(unittest.TestCase):
    def setUp(self):
        import json
        import os
        from array import array
        self.tmp = tempfile.TemporaryDirectory()
        self._old = state.VAR_DIR
        state.VAR_DIR = self.tmp.name
        d = self.tmp.name
        n, cs = 2000, 1048576
        lat = array("f", [2000.0] * n)
        for i in range(0, 60, 3):
            lat[1900 + i // 3] = 8000.0
        with open(os.path.join(d, "s.lat"), "wb") as f:
            lat.tofile(f)
        with open(os.path.join(d, "s.json"), "w") as f:
            json.dump({"latency_file": "s.lat", "total_bytes": n * cs, "chunk_size": cs}, f)
        with open(os.path.join(d, "slow.json"), "w") as f:
            json.dump({"offsets": [1900 * cs, 1901 * cs]}, f)
        history.add_run("nw-3f9a2c", "SECRETSERIAL", "Micron M600", {**ROW, "job": "j1", "outcome": None})
        history.add_run("nw-3f9a2c", "SECRETSERIAL", "Micron M600",
                        {**ROW, "scan_report": os.path.join(d, "s.json"), "slow_file": os.path.join(d, "slow.json"),
                         "device": "sdq", "next": "sudo nandwash preboot install /dev/sdq --scan-report /root/x.json"})

    def tearDown(self):
        state.VAR_DIR = self._old
        self.tmp.cleanup()

    def test_page_and_share_leaks_nothing(self):
        import socket
        from nandwash import html
        h = history.load("nw-3f9a2c")
        page = html.render(h)
        self.assertIn("<svg", page)
        self.assertIn("SECRETSERIAL", page)
        self.assertNotIn("<script", page)
        self.assertNotIn("http", page.replace("https://github.com", ""))     # no external assets
        shared = html.render(h, share=True)
        for secret in ("SECRETSERIAL", "sdq", "/root/", self.tmp.name, socket.gethostname()):
            self.assertNotIn(secret, shared, secret)
        self.assertIn("Micron M600", shared)
        self.assertIn("nw-3f9a2c", shared)


if __name__ == "__main__":
    unittest.main()
