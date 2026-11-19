"""scripts/backtest_spreads.py: credit-spread pricing, strike choice, stop and settlement (offline)."""

import datetime
import os
import sys
import unittest

from _load import load  # noqa: F401  (skill folders on sys.path)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
import backtest_spreads as bs  # noqa: E402


def row(date, o, h, l, c, iv=0.15, sma20=None):
    return {"date": date, "o": o, "h": h, "l": l, "c": c, "vix": iv * 100, "iv": iv, "sma20": sma20 or c}


class TestPricing(unittest.TestCase):
    def test_spread_value_bounds(self):
        # deep ITM at expiry is worth the full width; far OTM is worth nothing
        self.assertEqual(bs.spread_value(20000, 21000, 20950, 0.0, 0.15, "PE"), 50)
        self.assertEqual(bs.spread_value(22000, 21000, 20950, 0.0, 0.15, "PE"), 0)
        v = bs.spread_value(21000, 21000, 20950, 5 / 365.0, 0.15, "PE")
        self.assertTrue(0 < v < 50)

    def test_pick_strikes_out_of_the_money_and_hedged(self):
        sk, lk, d = bs.pick_strikes(25000, 4 / 365.0, 0.15, 0.20, 50, 2, "PE")
        self.assertLess(sk, 25000)
        self.assertEqual(sk - lk, 100)
        self.assertAlmostEqual(d, 0.20, delta=0.08)
        sk, lk, d = bs.pick_strikes(25000, 4 / 365.0, 0.15, 0.20, 50, 1, "CE")
        self.assertGreater(sk, 25000)
        self.assertEqual(lk - sk, 50)


class TestSimulate(unittest.TestCase):
    def setUp(self):
        self.exp = datetime.date(2026, 11, 24)

    def test_expires_worthless_keeps_credit_minus_charges(self):
        rows = [row("2026-11-20", 25000, 25050, 24950, 25000), row("2026-11-23", 25100, 25150, 25050, 25100),
                row("2026-11-24", 25200, 25250, 25150, 25200)]
        t = bs.simulate(rows, 0, self.exp, "PE", 0.20, 2, None, "NIFTY")
        self.assertEqual(t["outcome"], "expiry")
        self.assertEqual(t["exit_cost"], 0)
        self.assertGreater(t["net"], 0)
        self.assertAlmostEqual(t["net"], t["credit"] * 65 - t["charges"], delta=1)

    def test_loss_never_exceeds_width_minus_credit_plus_charges(self):
        rows = [row("2026-11-20", 25000, 25050, 24950, 25000), row("2026-11-23", 24000, 24050, 23900, 23950),
                row("2026-11-24", 23900, 23950, 23800, 23850)]
        t = bs.simulate(rows, 0, self.exp, "PE", 0.20, 2, None, "NIFTY")
        self.assertEqual(t["exit_cost"], 100)                      # settled at the full width
        self.assertGreaterEqual(t["net"], -t["max_loss"] - t["charges"] - 1)

    def test_stop_on_gap_open_exits_at_the_open(self):
        rows = [row("2026-11-20", 25000, 25050, 24950, 25000), row("2026-11-23", 24300, 24350, 24250, 24300),
                row("2026-11-24", 25500, 25550, 25450, 25500)]
        t = bs.simulate(rows, 0, self.exp, "PE", 0.20, 2, 2.0, "NIFTY")
        self.assertEqual(t["outcome"], "stop")
        self.assertEqual(t["exit"], "2026-11-23")                  # no credit for the later recovery
        self.assertLess(t["net"], 0)

    def test_unfinished_trade_is_skipped(self):
        rows = [row("2026-11-20", 25000, 25050, 24950, 25000), row("2026-11-23", 25100, 25150, 25050, 25100)]
        self.assertIsNone(bs.simulate(rows, 0, self.exp, "PE", 0.20, 2, None, "NIFTY"))


class TestTimeBasis(unittest.TestCase):
    def test_friday_to_tuesday(self):
        fri, tue = datetime.date(2026, 11, 20), datetime.date(2026, 11, 24)
        try:
            bs.TIME_BASIS = "calendar"
            self.assertAlmostEqual(bs.years(fri, tue), 4 / 365.0)
            bs.TIME_BASIS = "trading"
            self.assertAlmostEqual(bs.years(fri, tue), 2 / 252.0)
        finally:
            bs.TIME_BASIS = "calendar"


class TestPassRule(unittest.TestCase):
    def test_pass(self):
        good = {"n": 60, "avg": 100.0, "years_pos%": 70.0}
        self.assertTrue(bs.passes(good, good, good))
        self.assertFalse(bs.passes(good, dict(good, avg=-1.0), good))
        self.assertFalse(bs.passes(good, dict(good, n=30), good))
        self.assertFalse(bs.passes(good, good, dict(good, **{"years_pos%": 50.0})))


if __name__ == "__main__":
    unittest.main()
