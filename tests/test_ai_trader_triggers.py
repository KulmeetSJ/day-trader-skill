"""ai_trader wake-up rules (2026-11-20) and per-process Upstox pacing. Hermetic: temp state dir, fixed clock."""
import csv
import datetime
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ai_trader  # noqa: E402
import upstox_speed  # noqa: E402

IST = ai_trader.IST
OPP = ["date", "time", "kind", "symbol", "setup", "entry", "stop", "target"]
REJ = ["date", "time", "kind", "symbol", "net_rr", "reasons"]


def at(h, m):
    return datetime.datetime(2026, 11, 23, h, m, tzinfo=IST)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.orig = ai_trader.STATE
        ai_trader.STATE = self.dir
        self.seen = {"date": "2026-11-23", "alerts": 0, "refused": 0, "refused_at": None, "spread_calls": 0,
                     "scan_date": None, "reviews": ["09:35"], "runs": 0, "last_run": None}

    def tearDown(self):
        ai_trader.STATE = self.orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, fields, rows):
        with open(os.path.join(self.dir, name), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def rejected(self, n, rr, kind="equity", sym="ABC"):
        return [{"date": "2026-11-23", "time": "10:00", "kind": kind, "symbol": "%s%d" % (sym, i),
                 "net_rr": rr, "reasons": "BUDGET"} for i in range(n)]


class TestAlerts(Base):
    def test_alert_reason_names_the_call_and_says_take_it(self):
        self.write("opportunities.csv", OPP, [{"date": "2026-11-23", "time": "09:33", "kind": "equity",
                                               "symbol": "ANGELONE", "setup": "ema_alignment", "entry": "337.7",
                                               "stop": "331.0", "target": "352.45"}])
        r = ai_trader.pending_reasons(self.seen, at(9, 40))
        self.assertEqual(len(r), 1)
        self.assertIn("take each unless a concrete reason says no", r[0])
        self.assertIn("ANGELONE ema_alignment entry 337.7 stop 331.0 target 352.45", r[0])
        self.assertEqual(ai_trader.pending_reasons(self.seen, at(9, 42)), [])     # not repeated

    def test_prompt_tells_ai_to_take_alerts(self):
        self.assertIn("TAKE each one", ai_trader.PROMPT)
        self.assertIn("is not a reason to skip an alert", ai_trader.PROMPT)


class TestRefused(Base):
    def test_far_from_gate_refusals_never_wake(self):
        self.write("rejected.csv", REJ, self.rejected(60, "1.5") + self.rejected(30, "2.5", kind="equity_short"))
        self.assertEqual(ai_trader.pending_reasons(self.seen, at(10, 5)), [])

    def test_near_gate_wakes_at_most_hourly(self):
        self.write("rejected.csv", REJ, self.rejected(2, "1.93"))
        r = ai_trader.pending_reasons(self.seen, at(10, 5))
        self.assertEqual(len(r), 1)
        self.assertIn("just under the net R:R gate", r[0])
        self.write("rejected.csv", REJ, self.rejected(5, "1.93"))
        self.assertEqual(ai_trader.pending_reasons(self.seen, at(10, 40)), [])     # inside the hour
        self.assertEqual(len(ai_trader.pending_reasons(self.seen, at(11, 6))), 2)  # + 11:00 review

    def test_runs_kept_for_reviews_ahead(self):
        self.write("rejected.csv", REJ, self.rejected(2, "1.95"))
        self.seen["runs"] = ai_trader.MAX_RUNS_PER_DAY - 3       # 11:00, 13:30, 15:50 still ahead
        self.assertEqual(ai_trader.pending_reasons(self.seen, at(10, 5)), [])
        self.seen["runs"] = ai_trader.MAX_RUNS_PER_DAY - 4
        self.assertEqual(len(ai_trader.pending_reasons(self.seen, at(10, 5))), 1)

    def test_reviews_left(self):
        self.assertEqual(ai_trader.reviews_left(self.seen, at(10, 0)), 3)
        self.assertEqual(ai_trader.reviews_left(self.seen, at(15, 51)), 0)


class TestPacing(unittest.TestCase):
    def test_profiles_set_defaults_but_explicit_values_win(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "2026-11-20"))
            os.makedirs(os.path.join(d, "2026-11-23"))
            env = upstox_speed.apply("ai_mcp", {"UPSTOX_HIST_CACHE_DIR": d, "UPSTOX_MAX_RPS": "1"}, today="2026-11-23")
            self.assertEqual(env["UPSTOX_MAX_RPS"], "1")
            self.assertEqual(env["UPSTOX_BATCH_WORKERS"], "2")
            self.assertEqual(sorted(os.listdir(d)), ["2026-11-23"])            # old cache day dropped
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_shares_stay_under_the_burst(self):
        self.assertLessEqual(sum(float(p["UPSTOX_MAX_RPS"]) for p in upstox_speed.PROFILES.values()), 15)


if __name__ == "__main__":
    unittest.main()
