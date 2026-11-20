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
# Tests must never pop up real notifications or speak (a failing test once showed a fake option
# alert on the desktop during market hours).
mw.notify = lambda *a, **k: None


class Args(object):
    config = None
    max_trigger_age = 10.0
    setups = mw.SETUPS
    no_short_signals = False
    stop = "swing"
    target_r = 2.2
    instruments = None


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
    w.market_ctx = {}
    w.equity_ledger_path = os.path.join(d, "eq_ledger.csv")
    w.options_ledger_path = os.path.join(d, "opt_ledger.csv")
    return w


def seed_today_call(w, qty, entry, sym="OLD"):
    """An equity call already alerted today (it ties up qty x entry of the capital cap)."""
    mw.append_row(w.csv_path, mw.CSV_FIELDS, {"date": mw.now_ist().strftime("%Y-%m-%d"), "kind": "equity",
                                               "symbol": sym, "qty": qty, "entry": entry})


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
    def write_day(self, w, net, minutes_ago=0, priced=None, unscored=()):
        import json
        now = mw.now_ist().replace(tzinfo=None) - datetime.timedelta(minutes=minutes_ago)
        d = {"date": now.strftime("%Y-%m-%d"), "as_of": now.strftime("%Y-%m-%d %H:%M"),
             "net_inr_worst_case": net, "unscored": list(unscored)}
        if priced is not None:
            d["net_inr"] = priced
        with open(w.day_path, "w") as fh:
            json.dump(d, fh)

    def test_unpriced_worst_case_blocks_but_does_not_latch(self):
        # 2026-11-19 09:36: five fresh calls had no price yet, each counted as a full stop-out
        # (-7838 worst case) while the priced total was 0. That must not halt the whole day.
        w = make_watcher()
        w.active = {"X": {}}
        self.write_day(w, -7838.0, priced=0.0, unscored=["COLPAL", "DABUR"])
        orig = mw.notify
        mw.notify = lambda *a, **k: self.fail("must not notify a halt")
        try:
            w.update_daily_loss()
        finally:
            mw.notify = orig
        self.assertIsNone(w.halted)
        self.assertFalse(os.path.exists(w.halt_path))
        self.assertTrue(w.loss_block().startswith("DAILY_LOSS_UNPRICED"))   # still fail closed this cycle
        self.assertIn("COLPAL", w.loss_block())
        self.write_day(w, 9377.0, priced=9377.0)    # priced next cycle: calls re-open
        w.update_daily_loss()
        self.assertIsNone(w.loss_block())

    def test_priced_loss_over_limit_still_latches(self):
        w = make_watcher()
        w.active = {"X": {}}
        self.write_day(w, -7000.0, priced=-5200.0, unscored=["Y"])
        orig = mw.notify
        mw.notify = lambda *a, **k: None
        try:
            w.update_daily_loss()
        finally:
            mw.notify = orig
        self.assertTrue(w.loss_block().startswith("DAILY_LOSS:"))
        self.assertTrue(w.load_halt())

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
               "_validated": True, "underlying_trigger": {"time": "13:35", "setup": "vwap_reclaim"},
               "current_premium": 160.5, "stop_loss": 143.3, "target_1": 175.9}

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


class TestCapitalCap(unittest.TestCase):
    """Rs 1,00,000 is the TOTAL capital at once (config budget), not per trade (user rule 2026-11-19).
    These run with the cap ENFORCED; it is off while testing (2026-11-20), see TestCapitalCapOff."""

    def setUp(self):
        self._enforce = mw.capital.ENFORCE
        mw.capital.ENFORCE = True

    def tearDown(self):
        mw.capital.ENFORCE = self._enforce

    def test_cap_is_config_budget(self):
        self.assertEqual(make_watcher().capital_cap, 100000.0)

    def test_option_that_does_not_fit_is_rejected(self):
        import json as _json
        w = make_watcher()
        seed_today_call(w, 950, 100.0)                  # Rs 95,000 already committed today
        w.args.options = ["NIFTY"]
        w.args.budget = 2000.0
        sig = {"contract": "NIFTY2026112325250CE", "strategy": "BUY_CE", "direction": "BULLISH",
               "_validated": True, "underlying_trigger": {"time": "13:35", "setup": "vwap_reclaim"},
               "current_premium": 160.5, "stop_loss": 143.3, "target_1": 175.9,
               "capital_margin_requirement": 12037.5, "position_sizing": {"lots": 1, "total_quantity": 75}}

        class R(object):
            returncode, stderr, stdout = 0, "", _json.dumps([sig])
        orig = mw.subprocess.run
        mw.subprocess.run = lambda *a, **k: R()
        try:
            w.check_options()
        finally:
            mw.subprocess.run = orig
        self.assertEqual([r["kind"] for r in rows(w.csv_path)], ["equity"])     # only the seeded call
        why = rows(w.rejected_path)[0]["reasons"]
        self.assertTrue(why.startswith("CAPITAL_CAP:"), why)
        self.assertIn("5000", why)

    def test_equity_is_sized_inside_the_free_capital(self):
        w = make_watcher()
        w.args.equities = True
        seed_today_call(w, 700, 100.0)                  # Rs 70,000 committed -> Rs 30,000 free
        now = mw.now_ist().strftime("%H:%M")
        hit = {"symbol": "NEW", "setup": "vwap_reclaim", "side": "buy", "decision_state": "ENTRY CONFIRMED",
               "actionable": True, "trigger": {"close_time": now, "close": 200.0},
               "decision": {"entry_reference": 200.0, "stop_loss": 196.0}}
        seen = {}

        def fake_size(plan, cfg, cash=None, **k):
            seen["cash"] = cash
            return {"qty": 0, "refusal": "too small for the test", "net_reward_risk": None}
        origs = (w.scan_equities, w.exit_plan, mw.size.size_trade, w.portfolio_cash)
        w.scan_equities = lambda: {"hits": [hit]}
        w.exit_plan = lambda *a, **k: {"entry": 200.0, "stop": 196.0, "target": 208.8}
        w.portfolio_cash = lambda: None
        mw.size.size_trade = fake_size
        try:
            w.check_equities()
        finally:
            w.scan_equities, w.exit_plan, mw.size.size_trade, w.portfolio_cash = origs
        self.assertAlmostEqual(seen["cash"], 30000.0)
        self.assertIn("30000 free of the Rs 100000 capital cap", rows(w.rejected_path)[0]["reasons"])

    def test_no_free_capital_rejects_before_sizing(self):
        w = make_watcher()
        seed_today_call(w, 1000, 100.0)                 # the whole Rs 1,00,000 is committed
        now = mw.now_ist().strftime("%H:%M")
        hit = {"symbol": "NEW", "setup": "vwap_reclaim", "side": "buy", "decision_state": "ENTRY CONFIRMED",
               "actionable": True, "trigger": {"close_time": now, "close": 200.0},
               "decision": {"entry_reference": 200.0, "stop_loss": 196.0}}
        origs = (w.scan_equities, w.exit_plan, mw.size.size_trade)
        w.scan_equities = lambda: {"hits": [hit]}
        w.exit_plan = lambda *a, **k: {"entry": 200.0, "stop": 196.0, "target": 208.8}
        mw.size.size_trade = lambda *a, **k: self.fail("must not size with no free capital")
        try:
            w.check_equities()
        finally:
            w.scan_equities, w.exit_plan, mw.size.size_trade = origs
        self.assertTrue(rows(w.rejected_path)[0]["reasons"].startswith("CAPITAL_CAP:"))


class TestCapitalCapOff(unittest.TestCase):
    """capital.ENFORCE False (testing stage, user 2026-11-20): the cap is reported, never a refusal."""

    def setUp(self):
        self._enforce = mw.capital.ENFORCE
        mw.capital.ENFORCE = False

    def tearDown(self):
        mw.capital.ENFORCE = self._enforce

    def _hit(self):
        now = mw.now_ist().strftime("%H:%M")
        return {"symbol": "NEW", "setup": "vwap_reclaim", "side": "buy", "decision_state": "ENTRY CONFIRMED",
                "actionable": True, "trigger": {"close_time": now, "close": 200.0},
                "decision": {"entry_reference": 200.0, "stop_loss": 196.0}}

    def test_full_cap_committed_still_sizes_with_the_whole_pool(self):
        w = make_watcher()
        w.args.equities = True
        seed_today_call(w, 1000, 100.0)                 # the whole Rs 1,00,000 is committed
        seen = {}

        def fake_size(plan, cfg, cash=None, **k):
            seen["cash"] = cash
            return {"qty": 0, "refusal": "too small for the test", "net_reward_risk": None}
        origs = (w.scan_equities, w.exit_plan, mw.size.size_trade, w.portfolio_cash)
        w.scan_equities = lambda: {"hits": [self._hit()]}
        w.exit_plan = lambda *a, **k: {"entry": 200.0, "stop": 196.0, "target": 208.8}
        w.portfolio_cash = lambda: None
        mw.size.size_trade = fake_size
        try:
            w.check_equities()
        finally:
            w.scan_equities, w.exit_plan, mw.size.size_trade, w.portfolio_cash = origs
        self.assertAlmostEqual(seen["cash"], 100000.0)
        why = rows(w.rejected_path)[0]["reasons"]
        self.assertFalse(why.startswith("CAPITAL_CAP"), why)
        self.assertNotIn("capital cap", why)

    def test_option_over_the_cap_is_alerted(self):
        import json as _json
        w = make_watcher()
        seed_today_call(w, 950, 100.0)                  # Rs 95,000 committed; the option needs Rs 12,037
        w.args.options = ["NIFTY"]
        w.args.budget = 2000.0
        sig = {"contract": "NIFTY2026112325250CE", "strategy": "BUY_CE", "direction": "BULLISH",
               "_validated": True, "underlying_trigger": {"time": "13:35", "setup": "vwap_reclaim"},
               "current_premium": 160.5, "stop_loss": 143.3, "target_1": 175.9,
               "capital_margin_requirement": 12037.5, "position_sizing": {"lots": 1, "total_quantity": 75}}

        class R(object):
            returncode, stderr, stdout = 0, "", _json.dumps([sig])
        orig, orig_n = mw.subprocess.run, mw.notify
        mw.subprocess.run = lambda *a, **k: R()
        mw.notify = lambda *a, **k: None
        try:
            w.check_options()
        finally:
            mw.subprocess.run, mw.notify = orig, orig_n
        self.assertEqual([r["kind"] for r in rows(w.csv_path)], ["equity", "options"])
        self.assertEqual(rows(w.rejected_path), [])


class TestAnalysisOnlySetups(unittest.TestCase):
    def test_listed_setup_is_analysis_only(self):
        w = make_watcher()
        self.assertIsNone(w.setup_block("ema_alignment"))   # default: every setup alerts
        w.args.analysis_only_setups = "ema_alignment"
        try:
            self.assertTrue(w.setup_block("ema_alignment").startswith("SETUP_OFF:"))
            self.assertIsNone(w.setup_block("vwap_reclaim"))
            self.assertIn("ema_alignment", w.scan_setups())   # still scanned, so it keeps being scored
        finally:
            del w.args.analysis_only_setups

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
        w.args.analysis_only_setups = "ema_alignment"
        sig = {"contract": "NIFTY2026112325250CE", "strategy": "BUY_CE", "direction": "BULLISH",
               "_validated": True, "underlying_trigger": {"time": "13:35", "setup": "ema_alignment"},
               "current_premium": 160.5, "stop_loss": 143.3, "target_1": 175.9}

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
        del w.args.analysis_only_setups


class TestIncompleteOptionSignal(unittest.TestCase):
    def test_validated_signal_without_numbers_is_rejected_not_alerted(self):
        import json as _json
        w = make_watcher()
        w.args.options = ["NIFTY"]
        w.args.budget = 2000.0
        sig = {"contract": "NIFTY2026112325250CE", "strategy": "BUY_CE", "direction": "BULLISH",
               "_validated": True, "underlying_trigger": {"time": "09:20", "setup": "vwap_reclaim"}}

        class R(object):
            returncode, stderr, stdout = 0, "", _json.dumps([sig])
        orig = mw.subprocess.run
        mw.subprocess.run = lambda *a, **k: R()
        try:
            w.check_options()
        finally:
            mw.subprocess.run = orig
        self.assertEqual(rows(w.csv_path), [])
        self.assertTrue(rows(w.rejected_path)[0]["reasons"].startswith("INCOMPLETE_SIGNAL:"))


class TestMarketContextLogged(unittest.TestCase):
    def test_rejected_row_carries_vix_and_nifty(self):
        w = make_watcher()
        w.market_ctx = {"india_vix": 15.57, "nifty_chg_pct": -0.16, "nifty_vs_open_pct": 0.2}
        w.reject({"kind": "equity", "symbol": "X", "reasons": "BUDGET: test"})
        r = rows(w.rejected_path)[0]
        self.assertEqual((r["india_vix"], r["nifty_chg_pct"], r["nifty_vs_open_pct"]), ("15.57", "-0.16", "0.2"))

    def test_context_failure_never_blocks(self):
        w = make_watcher()
        w.market_ctx = {}
        w.reject({"kind": "equity", "symbol": "X", "reasons": "BUDGET: test"})
        self.assertEqual(rows(w.rejected_path)[0]["india_vix"], "")


class TestScoreboardTotals(unittest.TestCase):
    def test_unpriced_call_counts_as_full_stop(self):
        import sys
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import scoreboard
        t = scoreboard.day_totals("2026-11-18", mw.now_ist(), 2, 1, -300.0, -300.0 - 1700.0, ["Y"])
        self.assertEqual(t["net_inr_worst_case"], -2000.0)
        self.assertEqual(t["unscored"], ["Y"])


class TestOptionsDedupeAcrossRestart(unittest.TestCase):
    def test_restart_does_not_realert_logged_option(self):
        import json as _json
        w = make_watcher()
        w.args.options = ["NIFTY"]
        w.args.budget = 2000.0
        sig = {"contract": "NIFTY2026112325250CE", "strategy": "BUY_CE", "direction": "BULLISH",
               "_validated": False, "underlying_trigger": {"time": "10:05", "setup": "vwap_reclaim"}}

        class R(object):
            returncode, stderr, stdout = 0, "", _json.dumps([sig])
        orig = mw.subprocess.run
        mw.subprocess.run = lambda *a, **k: R()
        try:
            w.check_options()
            self.assertEqual(len(rows(w.rejected_path)), 1)
            w.seen = set()                              # a restart: in-memory dedupe is gone ...
            w.seen.update(w.load_seen_options())        # ... and rebuilt from the CSVs
            w.check_options()
        finally:
            mw.subprocess.run = orig
        self.assertEqual(len(rows(w.rejected_path)), 1)


if __name__ == "__main__":
    unittest.main()
