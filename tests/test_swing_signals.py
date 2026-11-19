"""scripts/swing_signals.py + backtest_swing.py simulator (offline, synthetic daily bars)."""

import os
import random
import sys
import unittest

from _load import load  # noqa: F401  (skill folders on sys.path)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
import swing_signals as ss  # noqa: E402
import backtest_swing as bs  # noqa: E402


def mk(closes, vols=None, spread=0.004):
    """Daily bars from closes: open = previous close, high/low = close +- spread."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(c, prev) * (1 + spread), min(c, prev) * (1 - spread)
        out.append({"date": "D%05d" % i, "o": prev, "h": hi, "l": lo, "c": c,
                    "v": (vols[i] if vols else 1000.0)})
        prev = c
    return out


def lin(a, b, n):
    return [a + (b - a) * i / float(n) for i in range(1, n + 1)]


def wiggle(level, n, amp, period=6):
    import math
    return [level + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def last_signals(bars):
    A = ss.prep(bars)
    return dict(ss.signals_at(A, A["n"] - 1))


def with_breakout(closes, close, vol_mult=3.0):
    vols = [1000.0] * len(closes) + [1000.0 * vol_mult]
    return mk(closes + [close], vols)


class TestFormations(unittest.TestCase):
    def test_high52_breakout_needs_volume(self):
        base = wiggle(100, 300, 2)
        self.assertIn("high52_breakout", last_signals(with_breakout(base, 106)))
        self.assertNotIn("high52_breakout", last_signals(with_breakout(base, 106, vol_mult=1.0)))

    def test_high52_not_when_below_prior_high(self):
        base = wiggle(100, 300, 2) + [110] + wiggle(100, 20, 2)
        self.assertNotIn("high52_breakout", last_signals(with_breakout(base, 106)))

    def test_flat_base(self):
        base = lin(50, 100, 240) + wiggle(98, 30, 1.5)
        s = last_signals(with_breakout(base, 103))
        self.assertIn("flat_base", s)
        self.assertLess(s["flat_base"]["depth_pct"], 15)

    def test_flat_base_too_deep(self):
        base = lin(50, 100, 240) + wiggle(90, 30, 9)   # ~20 % range
        self.assertNotIn("flat_base", last_signals(with_breakout(base, 103)))

    def test_cup_handle(self):
        pre = wiggle(85, 300, 1)
        cup = lin(85, 100, 20) + lin(100, 75, 40) + lin(75, 99, 40)
        handle = lin(99, 94, 5) + lin(94, 97, 5)
        s = last_signals(with_breakout(pre + cup + handle, 101.5))
        self.assertIn("cup_handle", s)
        info = s["cup_handle"]
        self.assertTrue(12 <= info["cup_depth_pct"] <= 35)
        self.assertLess(info["stop"], info["pivot"])

    def test_cup_too_shallow_is_not_a_cup(self):
        pre = wiggle(85, 300, 1)
        cup = lin(85, 100, 20) + lin(100, 95, 40) + lin(95, 99, 40)   # 5 % dip
        handle = lin(99, 97, 5) + lin(97, 98, 5)
        self.assertNotIn("cup_handle", last_signals(with_breakout(pre + cup + handle, 101.5)))

    def test_double_bottom(self):
        pre = lin(120, 100, 60)
        w = lin(100, 80, 20) + lin(80, 90, 15) + lin(90, 80.5, 15) + lin(80.5, 89, 12)
        s = last_signals(with_breakout(pre + w, 91.5))
        self.assertIn("double_bottom", s)
        self.assertLess(s["double_bottom"]["stop"], 81)

    def test_double_bottom_lows_too_far_apart_in_price(self):
        pre = lin(120, 100, 60)
        w = lin(100, 80, 20) + lin(80, 90, 15) + lin(90, 70, 15) + lin(70, 89, 12)   # 2nd low 12 % lower
        self.assertNotIn("double_bottom", last_signals(with_breakout(pre + w, 91.5)))

    def test_bull_flag_v2(self):
        pre = wiggle(80, 260, 0.5)
        pole = lin(80, 100, 10)
        flag = lin(99, 94, 8)
        closes = pre + pole + flag
        vols = [1000.0] * len(pre) + [3000.0] * len(pole) + [800.0] * len(flag) + [5000.0]
        s = last_signals(mk(closes + [101.0], vols))
        self.assertIn("bull_flag_v2", s)

    def test_bull_flag_v2_deep_retrace_rejected(self):
        pre = wiggle(80, 260, 0.5)
        pole = lin(80, 100, 10)
        flag = lin(99, 86, 8)          # gives back > 50 % of the pole
        closes = pre + pole + flag
        vols = [1000.0] * len(pre) + [3000.0] * len(pole) + [800.0] * len(flag) + [5000.0]
        self.assertNotIn("bull_flag_v2", last_signals(mk(closes + [101.0], vols)))

    def test_ascending_triangle(self):
        pre = lin(60, 90, 240)
        tri = (lin(90, 100, 5) + lin(100, 92, 5) + lin(92, 100, 5) + lin(100, 94, 5)
               + lin(94, 100, 5) + lin(100, 96, 5) + lin(96, 99.5, 5))
        s = last_signals(with_breakout(pre + tri, 102))
        self.assertIn("ascending_triangle", s)

    def test_rs_leader_state(self):
        bars = mk(lin(50, 100, 300))
        A = ss.prep(bars)
        t = A["n"] - 1
        self.assertTrue(ss.rs_leader_state(A, t, 0.95))
        self.assertFalse(ss.rs_leader_state(A, t, 0.5))

    def test_rs_ranks(self):
        up = mk(lin(50, 100, 200))
        flat = mk([50.0] * 200)
        data = {"S%d" % i: (up if i < 2 else flat) for i in range(25)}
        r = ss.rs_ranks(data)
        d = up[-1]["date"]
        self.assertGreater(r["S0"][d], r["S10"][d])


class TestNoLookAhead(unittest.TestCase):
    def test_future_bars_never_change_past_signals(self):
        rng = random.Random(3)
        closes, vols, p = [], [], 100.0
        for i in range(700):
            p *= 1 + rng.gauss(0.0008, 0.018)
            closes.append(p)
            vols.append(1000.0 * (3.0 if rng.random() < 0.08 else rng.uniform(0.6, 1.2)))
        bars = mk(closes, vols)
        full = ss.scan_history("X", bars)
        self.assertTrue(full, "synthetic walk should produce some signals")
        for cut in (450, 560, 640):
            part = ss.scan_history("X", bars[:cut + 1])
            want = [r for r in full if r["date"] <= bars[cut]["date"]]
            self.assertEqual(part, want)


class TestSimulator(unittest.TestCase):
    def bars(self, rows):
        return [{"date": "D%d" % i, "o": o, "h": h, "l": l, "c": c, "v": 1} for i, (o, h, l, c) in enumerate(rows)]

    def test_gap_below_stop_exits_at_open(self):
        b = self.bars([(100, 101, 99, 100), (95, 96, 94, 95)])
        self.assertEqual(bs.simulate(b, 0, 100, 97, 106.6, 10), ("stop", 95, 1))

    def test_both_touched_counts_as_stop(self):
        b = self.bars([(100, 107, 96, 101)])
        self.assertEqual(bs.simulate(b, 0, 100, 97, 106.6, 10)[0], "stop")

    def test_target_and_gap_up(self):
        b = self.bars([(100, 101, 99, 100), (108, 109, 107, 108)])
        self.assertEqual(bs.simulate(b, 0, 100, 97, 106.6, 10), ("target", 108, 1))

    def test_time_exit(self):
        b = self.bars([(100, 101, 99, 100)] * 5)
        self.assertEqual(bs.simulate(b, 0, 100, 97, 106.6, 3), ("time", 100, 2))

    def test_plan_caps_stop_at_3_atr(self):
        b = mk(wiggle(100, 30, 1)) + mk([100.0, 100.0])
        p = bs.plan_for(b, len(b) - 2, sig_stop=50.0)
        self.assertGreaterEqual(p["stop"], p["entry"] - 3 * p["atr"] - 0.05)
        self.assertAlmostEqual(p["target"], round(p["entry"] + bs.TARGET_R * (p["entry"] - p["stop"]), 1), places=0)

    def test_pass_rule(self):
        good = {"n": 200, "avgR": 0.2, "edge_vs_random": 0.15, "years_pos%": 70.0}
        row = {"design": dict(good), "holdout": dict(good)}
        self.assertTrue(bs.passes(row))
        row["holdout"] = dict(good, edge_vs_random=0.05)
        self.assertFalse(bs.passes(row))
        row["holdout"] = dict(good, n=50)
        self.assertFalse(bs.passes(row))


class TestMultiDayFamilies(unittest.TestCase):
    def test_results_gap(self):
        base = wiggle(100, 80, 1)
        bars = mk(base, [1000.0] * 80)
        prev = bars[-1]["c"]
        bars.append({"date": "D99999", "o": prev * 1.05, "h": prev * 1.08, "l": prev * 1.045,
                     "c": prev * 1.075, "v": 4000.0})
        s = last_signals(bars)
        self.assertIn("results_gap", s)
        self.assertAlmostEqual(s["results_gap"]["stop"], round(prev * 1.045, 2), places=2)

    def test_results_gap_needs_volume_and_strong_close(self):
        base = wiggle(100, 80, 1)
        prev = mk(base)[-1]["c"]
        weak = mk(base, [1000.0] * 80) + [{"date": "D99999", "o": prev * 1.05, "h": prev * 1.10,
                                            "l": prev * 1.045, "c": prev * 1.05, "v": 4000.0}]
        self.assertNotIn("results_gap", last_signals(weak))          # closed in the bottom of its range
        quiet = mk(base, [1000.0] * 80) + [{"date": "D99999", "o": prev * 1.05, "h": prev * 1.08,
                                             "l": prev * 1.045, "c": prev * 1.075, "v": 1500.0}]
        self.assertNotIn("results_gap", last_signals(quiet))

    def test_pullback_uptrend(self):
        closes = lin(50, 100, 260) + [99.0, 97.5, 96.0]
        self.assertIn("pullback_uptrend", last_signals(mk(closes)))
        down = lin(100, 50, 260) + [49.0, 48.0, 47.0]
        self.assertNotIn("pullback_uptrend", last_signals(mk(down)))  # not in an uptrend

    def test_incomplete_time_exit_is_dropped(self):
        self.assertTrue(bs.incomplete("time", 105, 100, 20))
        self.assertFalse(bs.incomplete("time", 119, 100, 20))
        self.assertFalse(bs.incomplete("stop", 105, 100, 20))


if __name__ == "__main__":
    unittest.main()
