import tempfile
import unittest
from datetime import datetime

from nandwash import health as H, history, jobs, state
from test_jobs import FakeOps, JobsBase

CS = 1048576


class Rules(unittest.TestCase):
    """The B0/B3 numbers as fixtures."""

    def test_sda_and_mini_sdb_states(self):
        # .140 sda: 114,474 chunks, 5 confirmed, all 1.8-1.9x -> healthy
        st = H.drive_state(complete=True, error_sectors=0, smart_up=[], noise={"n": 200, "p95": 1.04, "ok": True},
                           confirmed_n=5, severe_n=0, nchunks=114_474)
        self.assertEqual(st["state"], "healthy")
        # mini sdb: ~457,862 chunks, 445 confirmed, 237 above 3x -> slow-regions (both reasons)
        st = H.drive_state(complete=True, error_sectors=0, smart_up=[], noise=None,
                           confirmed_n=445, severe_n=237, nchunks=457_862)
        self.assertEqual(st["state"], "slow-regions")
        self.assertIn("line 45", st["why"])
        # after its refresh: 3 left
        self.assertEqual(H.drive_state(complete=True, error_sectors=0, smart_up=[], noise=None, confirmed_n=3,
                                       severe_n=0, nchunks=457_862)["state"], "healthy")

    def test_order_errors_then_inconclusive(self):
        kw = dict(complete=False, error_sectors=0, smart_up=[], noise=None, confirmed_n=999, severe_n=99, nchunks=1000)
        self.assertEqual(H.drive_state(**kw)["state"], "inconclusive")
        self.assertEqual(H.drive_state(**{**kw, "smart_up": ["SMART 197 pending 0 -> 1"]})["state"], "errors")
        noisy = {**kw, "complete": True, "noise": H.noise_band([1000.0] * 100, [2.0] * 100)}
        self.assertEqual(H.drive_state(**noisy)["state"], "inconclusive")

    def test_noise_band(self):
        nb = H.noise_band([1000.0, 2000.0, 0.0], [1.0, 2.2])
        self.assertEqual((nb["n"], nb["ok"]), (2, True))
        self.assertAlmostEqual(nb["p50"], 1.05)

    def test_noise_sample_is_seeded_and_normal_only(self):
        lat = [1000.0] * 1000
        lat[7] = 5000.0
        lat[8] = 0.0
        a = H.noise_sample(lat, 1000.0, 50, seed="S")
        self.assertEqual(a, H.noise_sample(lat, 1000.0, 50, seed="S"))
        self.assertNotIn(7, a)
        self.assertNotIn(8, a)
        self.assertEqual(len(H.noise_sample(lat, 1000.0, 5000, seed="S")), 998)

    def test_fixed(self):
        med = 2300.0        # us; line 4.14 ms
        self.assertTrue(H.is_fixed(7.4, 2.4, med))
        self.assertFalse(H.is_fixed(7.4, 4.5, med))      # still above 1.8x
        self.assertFalse(H.is_fixed(3.0, 2.5, med))      # under the line but only 17% faster
        self.assertFalse(H.is_fixed(None, 2.0, med))

    def test_outcomes(self):
        med = 2000.0
        rows = lambda good, bad: ([{"offset": i, "status": "ok", "before_ms": 7, "after_ms": 2} for i in range(good)] +
                                  [{"offset": 100 + i, "status": "ok", "before_ms": 7, "after_ms": 6.9}
                                   for i in range(bad)])
        self.assertEqual(H.outcome(rows(9, 1), med)["outcome"], "improved")
        self.assertEqual(H.outcome(rows(5, 5), med)["outcome"], "partly")
        self.assertEqual(H.outcome(rows(1, 9), med)["outcome"], "no-effect")
        self.assertEqual(H.outcome(rows(9, 1), med, queued=3)["outcome"], "incomplete")
        self.assertIsNone(H.outcome([], med))
        o = H.outcome(rows(2, 1), med)
        self.assertEqual((o["fixed"], o["rewritten"], o["fixed_offsets"]), (2, 3, [0, 1]))

    def test_smart_increases(self):
        b = H.smart_counters({"attributes": {"5": {"raw": 0}, "197": {"raw": 0}, "9": {"raw": 1}}})
        a = H.smart_counters({"attributes": {"5": {"raw": 0}, "197": {"raw": 1}}})
        self.assertEqual(b, {"5": 0, "197": 0})
        self.assertEqual(H.smart_increases(b, a), ["SMART 197 pending 0 -> 1"])
        self.assertEqual(H.smart_increases(a, b), [])

    def test_exit_codes(self):
        self.assertEqual(H.exit_code("healthy", None), 0)
        self.assertEqual(H.exit_code("slow-regions", None), 1)
        self.assertEqual(H.exit_code("slow-regions", "improved"), 0)
        self.assertEqual(H.exit_code("slow-regions", "incomplete"), 1)
        self.assertEqual(H.exit_code("inconclusive", "improved"), 2)
        self.assertEqual(H.exit_code("errors", "improved"), 3)


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = state.VAR_DIR
        state.VAR_DIR = self.tmp.name

    def tearDown(self):
        state.VAR_DIR = self._old
        self.tmp.cleanup()

    def test_slow_again_and_dedup(self):
        history.add_run("nw-1", "S", "M", {"date": "2026-10-01T00:00:00", "job": "j1",
                                           "outcome": {"fixed_offsets": [0, CS, 2 * CS]}})
        history.add_run("nw-1", "S", "M", {"date": "2026-10-01T00:00:00", "job": "j1",
                                           "outcome": {"fixed_offsets": [0, CS, 2 * CS]}})
        h = history.load("nw-1")
        self.assertEqual(len(h["runs"]), 1)
        sa = history.slow_again(h, [CS, 9 * CS], now=datetime(2026, 10, 31))
        self.assertEqual((sa["count"], sa["of"], sa["offsets"], sa["again_days_median"]), (1, 3, [CS], 30.0))
        self.assertIsNone(history.slow_again(history.load("nw-2"), [CS]))


class JobResults(JobsBase):
    def test_check_then_refresh_then_check_writes_history(self):
        ops = FakeOps()
        self.runner(self.job("sdc", "refresh"), ops).run()          # sdc: unmounted -> rewrite-chunks
        h = history.load(self.S.tb.id)
        r = h["runs"][-1]
        self.assertEqual((r["state"], r["outcome"]["outcome"], r["outcome"]["fixed"]), ("healthy", "improved", 2))
        j = self.job("sdc", "check")
        self.runner(j, ops).run()                                   # same 2 chunks confirmed again
        j = jobs.load(j["id"])
        sa = j["drives"][0]["results"]["pick"]["slow_again"]
        self.assertEqual((sa["count"], sa["of"]), (2, 2))
        self.assertEqual(len(history.load(self.S.tb.id)["runs"]), 2)
        self.assertEqual(j["drives"][0]["exit"], 0)

    def test_live_refresh_is_incomplete_while_chunks_wait_for_the_boot(self):
        j = self.job("sda", "refresh")
        self.runner(j, FakeOps()).run()
        d = jobs.load(j["id"])["drives"][0]
        self.assertEqual(d["results"]["outcome"]["outcome"], "incomplete")
        self.assertEqual(d["results"]["outcome"]["queued"], 1)
        self.assertEqual(d["exit"], 1)

    def test_no_effect(self):
        ops = FakeOps()
        ops.after_ms = 6.9
        j = self.job("sdc", "refresh")
        self.runner(j, ops).run()
        self.assertEqual(jobs.load(j["id"])["drives"][0]["results"]["outcome"]["outcome"], "no-effect")


if __name__ == "__main__":
    unittest.main()
