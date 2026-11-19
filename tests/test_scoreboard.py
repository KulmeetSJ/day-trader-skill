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


def score0(*a, **k):
    """Plan-level scoring (no slippage, bars without opens fill at the plan entry)."""
    return scoreboard.score(*a, slip_ticks=0, **k)


def obar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


class TestRealisticFills(unittest.TestCase):
    CALL = dict(CALL, alert_time="09:37:20")      # trigger bar closed 09:30, alert at 09:37 -> 09:40 bar

    def test_fills_at_open_of_first_bar_after_alert_plus_slippage(self):
        self.assertEqual(scoreboard.fill_slot(self.CALL), 5)
        bars = {3: obar(100, 103, 97, 101), 5: obar(101, 101.5, 100.5, 101)}   # slot 3 is before the alert
        status, px, r, pnl = scoreboard.score(self.CALL, bars, now_slot=5)
        self.assertEqual(status, "OPEN")
        self.assertAlmostEqual(px, 100.95)          # mark to close minus 1 tick
        self.assertAlmostEqual(pnl, 10 * (100.95 - 101.05))
        self.assertAlmostEqual(r, (100.95 - 101.05) / 2.0)

    def test_gap_through_stop_exits_at_open(self):
        bars = {5: obar(100, 100.2, 99.5, 99.8), 6: obar(97.0, 97.5, 96.5, 97.2)}
        status, px, r, _ = scoreboard.score(self.CALL, bars, now_slot=6)
        self.assertEqual(status, "STOP HIT")
        self.assertAlmostEqual(px, 96.95)
        self.assertLess(r, -1.0)

    def test_target_fills_at_limit_price(self):
        bars = {5: obar(100, 100.2, 99.5, 100), 6: obar(102, 104.6, 101.9, 104)}
        status, px, _, _ = scoreboard.score(self.CALL, bars, now_slot=6)
        self.assertEqual((status, px), ("TARGET HIT", 104.4))

    def test_waiting_until_fill_bar_exists(self):
        bars = {3: obar(100, 101, 99, 100)}
        self.assertEqual(scoreboard.score(self.CALL, bars, now_slot=4)[0], "WAITING")

    def test_rejected_rows_use_log_time(self):
        row = {"trigger_time": "09:30", "time": "09:43:05"}
        self.assertEqual(scoreboard.fill_slot(row), 6)


class TestScore(unittest.TestCase):
    def test_slot(self):
        self.assertEqual(scoreboard.slot("09:15"), 0)
        self.assertEqual(scoreboard.slot("09:30"), 3)
        self.assertEqual(scoreboard.slot("15:20") - 1, 72)

    def test_open_call_marks_to_last_close(self):
        bars = {2: bar(100, 99, 100), 3: bar(101, 99.5, 101), 4: bar(101.5, 100.5, 101)}
        status, px, r, pnl = score0(CALL, bars, now_slot=4)
        self.assertEqual(status, "OPEN")
        self.assertEqual(px, 101)
        self.assertAlmostEqual(r, 0.5)
        self.assertAlmostEqual(pnl, 10.0)

    def test_trigger_bar_itself_is_not_scored(self):
        # slot 2 is the trigger bar (09:25-09:30); its low under the stop must not count
        bars = {2: bar(100, 97, 100), 3: bar(100.5, 99.5, 100)}
        self.assertEqual(score0(CALL, bars, now_slot=3)[0], "OPEN")

    def test_stop_hit_freezes_at_stop(self):
        bars = {3: bar(100.5, 97.9, 98.5), 4: bar(106, 98, 105)}
        status, px, r, pnl = score0(CALL, bars, now_slot=4)
        self.assertEqual((status, px), ("STOP HIT", 98.0))
        self.assertAlmostEqual(r, -1.0)
        self.assertAlmostEqual(pnl, -20.0)

    def test_bar_touching_both_counts_as_stop(self):
        bars = {3: bar(105, 97, 101)}
        self.assertEqual(score0(CALL, bars, now_slot=3)[0], "STOP HIT")

    def test_target_hit(self):
        bars = {3: bar(102, 99.5, 101), 4: bar(104.5, 101, 104)}
        status, px, r, _ = score0(CALL, bars, now_slot=4)
        self.assertEqual((status, px), ("TARGET HIT", 104.4))
        self.assertAlmostEqual(r, 2.2)

    def test_closed_at_flat_by(self):
        bars = {i: bar(100.5, 99.5, 100.2) for i in range(3, 75)}
        status, px, _, _ = score0(CALL, bars, now_slot=80)
        self.assertEqual((status, px), ("CLOSED 15:20", 100.2))

    def test_waiting_before_first_bar_after_trigger(self):
        self.assertEqual(score0(CALL, {2: bar(100, 99, 100)}, now_slot=2)[0], "WAITING")


class TestRejected(unittest.TestCase):
    def test_short_stop_and_target(self):
        short = {"trigger_time": "09:30", "entry": "100", "stop": "102", "target": "96", "qty": "0"}
        st = scoreboard.score(short, {3: bar(102.5, 99, 101)}, now_slot=3, side="sell")
        self.assertEqual(st[0], "STOP HIT")
        # sold at 100 - 1 tick slippage = 99.95; stop bought back at 102 + 1 tick
        self.assertAlmostEqual(st[2], -(102.05 - 99.95) / 2)
        tg = scoreboard.score(short, {3: bar(100.5, 95.9, 96.5)}, now_slot=3, side="sell")
        self.assertEqual(tg[0], "TARGET HIT")
        self.assertAlmostEqual(tg[2], (99.95 - 96) / 2)     # target is a limit: no slippage
        op = scoreboard.score(short, {3: bar(100.5, 98.5, 99)}, now_slot=3, side="sell")
        self.assertEqual(op[0], "OPEN")
        self.assertAlmostEqual(op[2], (99.95 - 99.05) / 2)

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


class TestPendingFill(unittest.TestCase):
    CALL = dict(CALL, trigger_time="09:30", alert_time="09:32:37")   # fill bar = 09:35 (slot 4)

    def test_waiting_for_fill_bar_is_pending(self):
        self.assertTrue(scoreboard.pending_fill(self.CALL, {3: obar(100, 101, 99, 100)}, now_slot=4))

    def test_no_price_data_is_not_pending(self):
        self.assertFalse(scoreboard.pending_fill(self.CALL, {}, now_slot=4))   # fail closed

    def test_fill_bar_long_past_is_not_pending(self):
        self.assertFalse(scoreboard.pending_fill(self.CALL, {3: obar(100, 101, 99, 100)}, now_slot=9))

    def test_day_total_ignores_unfilled_calls(self):
        orig_calls, orig_rej = scoreboard.load_calls, scoreboard.rejected_section

        class P(object):
            def get_intraday_data(self, sym, interval="5m", range_str="1d"):
                return {"bars": [{"date": "2026-11-19", "start": "09:30", "o": 100, "h": 101,
                                  "l": 99, "c": 100}]}
        call = dict(self.CALL, symbol="X", date="2026-11-19", setup="s", risk_inr_incl_costs="1700")
        scoreboard.load_calls = lambda date: [call]
        scoreboard.rejected_section = lambda *a, **k: []
        orig_slot, orig_start = scoreboard.slot, None
        totals = {}
        try:
            import datetime as _dt
            real_now = scoreboard.datetime.datetime
            class FakeDT(real_now):
                @classmethod
                def now(cls, tz=None):
                    return real_now(2026, 11, 19, 9, 35, 25, tzinfo=tz)
            scoreboard.datetime.datetime = FakeDT
            try:
                scoreboard.build("2026-11-19", provider=P(), totals=totals)
            finally:
                scoreboard.datetime.datetime = real_now
        finally:
            scoreboard.load_calls, scoreboard.rejected_section = orig_calls, orig_rej
        self.assertEqual(totals["net_inr_worst_case"], 0.0)
        self.assertEqual(totals["unscored"], [])


class TestDayTotalsSavedEarly(unittest.TestCase):
    def test_totals_written_before_rejected_section(self):
        import json, tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "day.json")
        seen = []
        orig_rej, orig_calls = scoreboard.rejected_section, scoreboard.load_calls

        def boom(*a, **k):
            seen.append(os.path.exists(path))
            raise RuntimeError("timed out")   # what a watcher timeout looks like mid-run
        scoreboard.rejected_section, scoreboard.load_calls = boom, lambda date: []
        try:
            with self.assertRaises(RuntimeError):
                scoreboard.build("2026-11-19", provider=object(), totals={},
                                 on_totals=lambda t: scoreboard.write_day(t, path))
        finally:
            scoreboard.rejected_section, scoreboard.load_calls = orig_rej, orig_calls
        self.assertEqual(seen, [True])
        with open(path) as fh:
            self.assertEqual(json.load(fh)["net_inr_worst_case"], 0.0)


if __name__ == "__main__":
    unittest.main()
