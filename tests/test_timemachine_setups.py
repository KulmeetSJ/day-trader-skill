"""Time Machine accuracy features: shared exit simulator, setup-aware history replay for
the intraday setups the scanner trades, flat-by horizon, and the blended prediction."""

import gzip
import json
import os
import unittest

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

RELIANCE = os.path.join(FIX, "trader-timemachine", "RELIANCE-60d5m.json.gz")
with gzip.open(RELIANCE, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = tm.parse_now("2026-10-19 10:35", market="IN")
ENTRY = 2744.28            # close of the 10:25-10:30 bar on 2026-10-19


def in_plan(setup="vwap_reclaim", **kw):
    risk = round(ENTRY * 0.006, 2)
    raw = {"symbol": "RELIANCE", "side": "buy", "date": "2026-10-19", "setup": setup, "entry": ENTRY,
           "stop": round(ENTRY - risk, 2), "target": round(ENTRY + 2.5 * risk, 2), "entry_time": "10:30",
           "market": "IN", "flat_by": "15:20", "stop_rule": "pct"}
    raw.update(kw)
    return tm.normalize_plan(raw, market="IN")


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


class SimulateExit(unittest.TestCase):
    def test_stop_checked_before_target_in_one_bar(self):
        D = [bar(100, 100, 100, 100), bar(100, 103, 98, 101)]
        self.assertEqual(tm.simulate_exit(D, 1, 1, 100, 99, 102), ("stop", 99, 1))

    def test_target_then_close(self):
        D = [bar(100, 100, 100, 100), bar(100, 101, 99.5, 100.5), bar(100.5, 102.5, 100, 102)]
        self.assertEqual(tm.simulate_exit(D, 1, 2, 100, 99, 102)[0], "target")
        self.assertEqual(tm.simulate_exit(D, 1, 1, 100, 99, 102), ("close", 100.5, 1))

    def test_breakeven_moves_the_stop_from_the_next_bar(self):
        # bar 1 reaches +1R (101) and dips to 100 in the same bar: still the original stop (no look-ahead)
        D = [bar(100, 100, 100, 100), bar(100, 101, 100, 100.8), bar(100.8, 100.9, 99.9, 100.2)]
        self.assertEqual(tm.simulate_exit(D, 1, 2, 100, 99, 103, breakeven_r=1.0), ("breakeven", 100, 2))
        self.assertEqual(tm.simulate_exit(D, 1, 2, 100, 99, 103)[0], "close")


class SetupHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = tm.time_machine(TEXT, in_plan(), NOW, replays=300, simulated=True)

    def test_intraday_setup_gets_its_own_history(self):
        h = self.doc["history"]
        self.assertEqual(h["rule"]["setup"], "vwap_reclaim")
        self.assertEqual(h["rule"]["stop_rule"], "pct")
        self.assertGreaterEqual(h["triggered"], tm.MIN_SETUP_TRADES)
        self.assertEqual(h["triggered"], h["target"] + h["stop"] + h["close"])

    def test_history_uses_only_earlier_sessions(self):
        self.assertTrue(all(t["date"] < "2026-10-19" for t in self.doc["history"]["trades"]))

    def test_history_trades_respect_the_rule(self):
        for t in self.doc["history"]["trades"]:
            risk = t["entry"] - t["stop"]
            self.assertGreater(risk, 0)
            self.assertAlmostEqual(risk / t["entry"], 0.006, delta=0.0002)
            self.assertAlmostEqual((t["target"] - t["entry"]) / risk, 2.5, delta=0.02)

    def test_intraday_plan_replays_to_flat_by(self):
        self.assertEqual(self.doc["mc"]["until"], "15:20")

    def test_prediction_is_the_monte_carlo_not_the_small_history(self):
        pr, mc = self.doc["prediction"], self.doc["mc"]
        self.assertEqual((pr["p_target"], pr["p_stop"], pr["p_neither"]), (mc["p_target"], mc["p_stop"], mc["p_neither"]))
        self.assertIn("Monte Carlo to 15:20", pr["source"])

    def test_report_describes_the_setup_history(self):
        text = "\n".join(tm.report(self.doc))
        self.assertIn("vwap_reclaim confirmed", text)
        self.assertIn("not used in the odds", text)

    def test_atr_and_swing_plans_replay_their_stop_distance_in_atr(self):
        atr = round(ENTRY * 0.006 / 1.5, 4)
        for rule_name in ("atr", "swing"):
            rule = tm.resolve_rule(in_plan(setup="breakout_intraday", stop_rule=rule_name, atr=atr), {})
            self.assertEqual(rule["stop_rule"], "atr")
            self.assertAlmostEqual(rule["stop_atr"], 1.5, places=2)
            self.assertEqual(rule["from_rule"], rule_name)

    def test_orb_history_is_unchanged(self):
        p = tm.normalize_plan({"symbol": "RELIANCE", "side": "buy", "date": "2026-10-19", "setup": "orb",
                               "entry": ENTRY, "stop": round(ENTRY - 10, 2), "target": round(ENTRY + 20, 2),
                               "entry_time": "10:30", "market": "IN"}, market="IN")
        rule = tm.resolve_rule(p, {})
        self.assertEqual(rule["setup"], "orb")


class Prediction(unittest.TestCase):
    MC = {"p_target": 0.2, "p_stop": 0.5, "p_neither": 0.3, "avg_R": -0.1, "r_at_target": 2.0, "until": "15:20"}

    def test_prediction_is_the_monte_carlo(self):
        pr = tm.prediction_of(self.MC)
        self.assertEqual((pr["p_target"], pr["p_stop"], pr["p_neither"], pr["avg_R"]), (0.2, 0.5, 0.3, -0.1))
        self.assertNotIn("avg_R_net", pr)

    def test_charges_lower_the_net_r(self):
        pr = tm.prediction_of(self.MC, cost_r=0.25)
        self.assertAlmostEqual(pr["avg_R_net"], -0.35, places=3)


if __name__ == "__main__":
    unittest.main()
