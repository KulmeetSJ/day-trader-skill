"""scripts/market_watch.py: rejected/short logging (offline; no scan, no network, no pop-ups)."""

import csv
import datetime
import importlib.util
import os
import tempfile
import unittest

from _load import load  # noqa: F401  (skill folders on sys.path)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("market_watch", os.path.join(BASE, "scripts", "market_watch.py"))
mw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mw)


class Args(object):
    config = None
    max_trigger_age = 10.0
    setups = mw.SETUPS
    no_short_signals = False


def make_watcher():
    w = mw.Watcher(Args())
    d = tempfile.mkdtemp()
    w.csv_path = os.path.join(d, "o.csv")
    w.rejected_path = os.path.join(d, "r.csv")
    w.events_path = os.path.join(d, "e.jsonl")
    w.active, w.active_short = {}, {}
    w.day_path = os.path.join(d, "day.json")
    w.halt_path = os.path.join(d, "halt.json")
    w.halted, w.loss_unknown = None, None
    return w


def rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def short_hit(sym="X", close_time=None, entry=100.0, stop=102.0, target=96.0):
    close_time = close_time or mw.now_ist().strftime("%H:%M")
    return {"symbol": sym, "setup": "breakdown_intraday", "side": "sell", "trigger": {"close_time": close_time, "close": entry},
            "decision": {"entry_reference": entry, "stop_loss": stop, "targets": {"t2_2r": target}}}


class TestWatcherLogging(unittest.TestCase):
    def test_scan_setups_include_shorts_unless_disabled(self):
        w = make_watcher()
        self.assertIn("breakdown_intraday", w.scan_setups())
        w.args.no_short_signals = True
        try:
            self.assertEqual(w.scan_setups(), mw.SETUPS)
        finally:
            w.args.no_short_signals = False

    def test_short_logged_once_per_range_never_alerted(self):
        w = make_watcher()
        today = mw.now_ist().date()
        w.log_short(short_hit(), today)
        w.log_short(dict(short_hit(), setup="vwap_rejection"), today)   # same trade, inside range
        r = rows(w.rejected_path)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["kind"], r[0]["direction"]), ("equity_short", "SHORT"))
        self.assertIn("LONG_ONLY", r[0]["reasons"])
        self.assertEqual(rows(w.csv_path), [])          # nothing in the alerts file
        self.assertEqual(w.opportunities, 0)

    def test_stale_short_not_logged(self):
        w = make_watcher()
        old = (mw.now_ist() - datetime.timedelta(minutes=30)).strftime("%H:%M")
        w.log_short(short_hit(close_time=old), mw.now_ist().date())
        self.assertEqual(rows(w.rejected_path), [])

    def test_rejected_options_go_to_rejected_csv(self):
        w = make_watcher()
        sig = {"strategy": "BUY_PE", "direction": "BEARISH", "contract": "NIFTY2026112325250PE", "_validated": False,
               "underlying_trigger": {"setup": "breakdown_intraday", "time": "9:40 AM", "current_spot": 25250.0},
               "current_premium": 120.0, "stop_loss": 90.0, "target_1": 180.0,
               "rejection_reasons": ["RR_BELOW_MINIMUM: Net R:R (0.87:1) below configured minimum (2.0:1)"],
               "position_sizing": {"lots": 1}}
        w.args.options = ["NIFTY"]
        w.args.budget = 2000.0

        class Out(object):
            returncode = 0
            stdout = __import__("json").dumps([sig])
            stderr = ""
        orig = mw.subprocess.run
        mw.subprocess.run = lambda *a, **k: Out()
        try:
            w.check_options()
        finally:
            mw.subprocess.run = orig
        r = rows(w.rejected_path)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["kind"], r[0]["direction"]), ("options", "BEARISH"))
        self.assertIn("RR_BELOW_MINIMUM", r[0]["reasons"])
        self.assertEqual(rows(w.csv_path), [])


class TestDailyLossStop(unittest.TestCase):
    def write_day(self, w, net, minutes_ago=0):
        import json
        now = mw.now_ist().replace(tzinfo=None) - datetime.timedelta(minutes=minutes_ago)
        with open(w.day_path, "w") as fh:
            json.dump({"date": now.strftime("%Y-%m-%d"), "as_of": now.strftime("%Y-%m-%d %H:%M"),
                       "net_inr_worst_case": net}, fh)

    def test_under_limit_allows_calls(self):
        w = make_watcher()
        w.active = {"X": {}}
        self.write_day(w, -4999.0)
        w.update_daily_loss()
        self.assertIsNone(w.loss_block())

    def test_limit_hit_latches_and_persists(self):
        w = make_watcher()
        w.active = {"X": {}}
        self.write_day(w, -5005.0)
        orig = mw.notify
        mw.notify = lambda *a, **k: None
        try:
            w.update_daily_loss()
        finally:
            mw.notify = orig
        self.assertTrue(w.loss_block().startswith("DAILY_LOSS:"))
        self.write_day(w, -1000.0)          # a bounce does not re-open the day
        w.update_daily_loss()
        self.assertTrue(w.loss_block().startswith("DAILY_LOSS:"))
        self.assertTrue(w.load_halt())      # a restart remembers it

    def test_unreadable_total_blocks_only_when_calls_exist(self):
        w = make_watcher()
        w.update_daily_loss()               # no calls today, no file: nothing to lose
        self.assertIsNone(w.loss_block())
        w.active = {"X": {}}
        w.update_daily_loss()
        self.assertTrue(w.loss_block().startswith("DAILY_LOSS_UNKNOWN"))
        self.write_day(w, -100.0, minutes_ago=30)   # stale total also fails closed
        w.update_daily_loss()
        self.assertTrue(w.loss_block().startswith("DAILY_LOSS_UNKNOWN"))

    def test_validated_option_is_rejected_when_halted(self):
        import json as _json
        w = make_watcher()
        w.args.options = ["NIFTY"]
        w.args.budget = 2000.0
        w.halted = {"date": "x", "at": "13:40", "net_inr": -5005.0, "limit_inr": 5000.0}
        sig = {"contract": "NIFTY2026112325250CE", "strategy": "BUY_CE", "direction": "BULLISH",
               "_validated": True, "underlying_trigger": {"time": "13:35", "setup": "vwap_reclaim"}}

        class R(object):
            returncode, stderr, stdout = 0, "", _json.dumps([sig])
        orig = mw.subprocess.run
        mw.subprocess.run = lambda *a, **k: R()
        try:
            w.check_options()
        finally:
            mw.subprocess.run = orig
        self.assertEqual(rows(w.csv_path), [])
        self.assertTrue(rows(w.rejected_path)[0]["reasons"].startswith("DAILY_LOSS:"))


class TestAnalysisOnlySetups(unittest.TestCase):
    def test_ema_alignment_is_off_by_default(self):
        w = make_watcher()
        self.assertTrue(w.setup_block("ema_alignment").startswith("SETUP_OFF:"))
        self.assertIsNone(w.setup_block("vwap_reclaim"))
        self.assertIn("ema_alignment", w.scan_setups())   # still scanned, so it keeps being scored

    def test_empty_list_alerts_all(self):
        w = make_watcher()
        w.args.analysis_only_setups = ""
        try:
            self.assertIsNone(w.setup_block("ema_alignment"))
        finally:
            del w.args.analysis_only_setups

    def test_validated_option_on_off_setup_is_rejected(self):
        import json as _json
        w = make_watcher()
        w.args.options = ["NIFTY"]
        w.args.budget = 2000.0
        sig = {"contract": "NIFTY2026112325250CE", "strategy": "BUY_CE", "direction": "BULLISH",
               "_validated": True, "underlying_trigger": {"time": "13:35", "setup": "ema_alignment"}}

        class R(object):
            returncode, stderr, stdout = 0, "", _json.dumps([sig])
        orig = mw.subprocess.run
        mw.subprocess.run = lambda *a, **k: R()
        try:
            w.check_options()
        finally:
            mw.subprocess.run = orig
        self.assertEqual(rows(w.csv_path), [])
        self.assertTrue(rows(w.rejected_path)[0]["reasons"].startswith("SETUP_OFF:"))


class TestScoreboardTotals(unittest.TestCase):
    def test_unpriced_call_counts_as_full_stop(self):
        import sys
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import scoreboard
        t = scoreboard.day_totals("2026-11-18", mw.now_ist(), 2, 1, -300.0, -300.0 - 1700.0, ["Y"])
        self.assertEqual(t["net_inr_worst_case"], -2000.0)
        self.assertEqual(t["unscored"], ["Y"])


if __name__ == "__main__":
    unittest.main()
