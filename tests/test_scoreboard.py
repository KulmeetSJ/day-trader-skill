"""scripts/scoreboard.py: per-call status from today's bars (offline, synthetic bars)."""

import os
import sys
import unittest

from _load import load  # noqa: F401  (puts skill folders on sys.path like every other test)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "trader-timemachine"))
import scoreboard  # noqa: E402

CALL = {"symbol": "X", "trigger_time": "09:30", "entry": "100", "stop": "98", "target": "104.4", "qty": "10"}


def bar(h, l, c):
    return {"h": h, "l": l, "c": c}


class TestScore(unittest.TestCase):
    def test_slot(self):
        self.assertEqual(scoreboard.slot("09:15"), 0)
        self.assertEqual(scoreboard.slot("09:30"), 3)
        self.assertEqual(scoreboard.slot("15:20") - 1, 72)

    def test_open_call_marks_to_last_close(self):
        bars = {2: bar(100, 99, 100), 3: bar(101, 99.5, 101), 4: bar(101.5, 100.5, 101)}
        status, px, r, pnl = scoreboard.score(CALL, bars, now_slot=4)
        self.assertEqual(status, "OPEN")
        self.assertEqual(px, 101)
        self.assertAlmostEqual(r, 0.5)
        self.assertAlmostEqual(pnl, 10.0)

    def test_trigger_bar_itself_is_not_scored(self):
        # slot 2 is the trigger bar (09:25-09:30); its low under the stop must not count
        bars = {2: bar(100, 97, 100), 3: bar(100.5, 99.5, 100)}
        self.assertEqual(scoreboard.score(CALL, bars, now_slot=3)[0], "OPEN")

    def test_stop_hit_freezes_at_stop(self):
        bars = {3: bar(100.5, 97.9, 98.5), 4: bar(106, 98, 105)}
        status, px, r, pnl = scoreboard.score(CALL, bars, now_slot=4)
        self.assertEqual((status, px), ("STOP HIT", 98.0))
        self.assertAlmostEqual(r, -1.0)
        self.assertAlmostEqual(pnl, -20.0)

    def test_bar_touching_both_counts_as_stop(self):
        bars = {3: bar(105, 97, 101)}
        self.assertEqual(scoreboard.score(CALL, bars, now_slot=3)[0], "STOP HIT")

    def test_target_hit(self):
        bars = {3: bar(102, 99.5, 101), 4: bar(104.5, 101, 104)}
        status, px, r, _ = scoreboard.score(CALL, bars, now_slot=4)
        self.assertEqual((status, px), ("TARGET HIT", 104.4))
        self.assertAlmostEqual(r, 2.2)

    def test_closed_at_flat_by(self):
        bars = {i: bar(100.5, 99.5, 100.2) for i in range(3, 75)}
        status, px, _, _ = scoreboard.score(CALL, bars, now_slot=80)
        self.assertEqual((status, px), ("CLOSED 15:20", 100.2))

    def test_waiting_before_first_bar_after_trigger(self):
        self.assertEqual(scoreboard.score(CALL, {2: bar(100, 99, 100)}, now_slot=2)[0], "WAITING")


class TestRejected(unittest.TestCase):
    def test_short_stop_and_target(self):
        short = {"trigger_time": "09:30", "entry": "100", "stop": "102", "target": "96", "qty": "0"}
        st = scoreboard.score(short, {3: bar(102.5, 99, 101)}, now_slot=3, side="sell")
        self.assertEqual(st[0], "STOP HIT")
        self.assertAlmostEqual(st[2], -1.0)
        tg = scoreboard.score(short, {3: bar(100.5, 95.9, 96.5)}, now_slot=3, side="sell")
        self.assertEqual(tg[0], "TARGET HIT")
        self.assertAlmostEqual(tg[2], 2.0)
        op = scoreboard.score(short, {3: bar(100.5, 98.5, 99)}, now_slot=3, side="sell")
        self.assertEqual(op[0], "OPEN")
        self.assertAlmostEqual(op[2], 0.5)

    def test_underlying_of(self):
        self.assertEqual(scoreboard.underlying_of("NIFTY2026112325250CE"), "NIFTY")
        self.assertEqual(scoreboard.underlying_of("BANKNIFTY2026112362100PE"), "BANKNIFTY")

    def test_options_direction_verdict(self):
        bars = {5: bar(25300, 25200, 25250)}
        bf = lambda sym: bars if sym == "NIFTY" else {}
        bear = {"kind": "options", "direction": "BEARISH", "symbol": "NIFTY2026112325250PE", "underlying_price": "25300"}
        v = scoreboard.score_rejected(bear, bf, now_slot=5)
        self.assertEqual(v[0], "RIGHT")
        self.assertAlmostEqual(v[2], -50.0)
        bull = dict(bear, direction="BULLISH", symbol="NIFTY2026112325250CE")
        self.assertEqual(scoreboard.score_rejected(bull, bf, now_slot=5)[0], "WRONG")

    def test_equity_rejected_long_walks_plan(self):
        r = {"kind": "equity", "direction": "LONG", "symbol": "X", "trigger_time": "09:30",
             "entry": "100", "stop": "98", "target": "104.4"}
        bf = lambda sym: {3: bar(104.5, 99.5, 104)}
        v = scoreboard.score_rejected(r, bf, now_slot=3)
        self.assertEqual((v[0], v[3]), ("RIGHT", "TARGET HIT"))

    def test_equity_short_rejected_wrong_when_stopped(self):
        r = {"kind": "equity_short", "direction": "SHORT", "symbol": "X", "trigger_time": "09:30",
             "entry": "100", "stop": "102", "target": "96"}
        v = scoreboard.score_rejected(r, lambda sym: {3: bar(102.5, 99.5, 102)}, now_slot=3)
        self.assertEqual((v[0], v[3]), ("WRONG", "STOP HIT"))


if __name__ == "__main__":
    unittest.main()
