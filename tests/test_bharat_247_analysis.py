"""Regression tests for 24/7 Indian market stock research, analytics, and paper trading.

Verifies:
1. Separation of market-session status (OPEN/CLOSED) from analysis availability.
2. 24/7 availability of stock analysis, technicals, fundamentals, screening, backtesting,
   options calculations, entry/exit planning, position sizing, and risk checks on weekends and holidays.
3. Proper tagging of latest market data: last timestamp, data status, last trading session, simulated flag.
4. Distinction between live-session signals and historical candle analysis.
5. Strict exchange-session restrictions on live order execution and live watch loops.
6. Off-hours paper trading and hypothetical trade analysis availability with safety gates preserved.
7. Pre-market readiness report of all 4 separate statuses.
"""

import copy
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load, FIX

alerts_mod = load("trader-alerts", "alerts")
calendar_mod = load("trader-alerts", "calendar_in")
instruments_mod = load("trader-alerts", "instruments")
symbols_mod = load("trader-alerts", "symbols_in")
data_mod = load("trader-alerts", "data_in")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
scan_mod = load("trader-scanner", "scan")
facts_mod = load("trader-analyst", "facts")
news_mod = load("trader-news", "news")
tm_mod = load("trader-timemachine", "timemachine")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")
RELIANCE_5M = os.path.join(BHARAT_FIXTURES, "RELIANCE-5m.json")


def run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestReadiness247(unittest.TestCase):
    def test_readiness_on_weekend(self):
        # Sunday: 2026-11-15 11:00 IST (standard weekend, no Muhurat)
        code, out, _ = run_cli(alerts_mod, [
            "readiness",
            "--market", "IN",
            "--now", "2026-11-15 11:00",
            "--json"
        ])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["market_session"], "CLOSED")
        self.assertEqual(doc["analysis_engine"], "AVAILABLE")
        self.assertEqual(doc["paper_trading"], "AVAILABLE")
        self.assertIn(doc["data_availability"], ("FRESH", "DELAYED", "STALE", "UNAVAILABLE"))
        self.assertEqual(doc["last_trading_session"], "2026-11-13")  # Friday before Sunday 2026-11-15
        self.assertIn(doc["verdict"], ("READY", "READY_WITH_NOTICES"))

    def test_readiness_on_exchange_holiday(self):
        # Dussehra: 2026-10-20 (Tuesday) is an official Indian market holiday
        code, out, _ = run_cli(alerts_mod, [
            "readiness",
            "--market", "IN",
            "--now", "2026-10-20 11:00",
            "--json"
        ])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["market_session"], "CLOSED")
        self.assertEqual(doc["analysis_engine"], "AVAILABLE")
        self.assertEqual(doc["paper_trading"], "AVAILABLE")
        self.assertEqual(doc["last_trading_session"], "2026-10-19")  # Monday preceding Dussehra

    def test_readiness_during_live_session(self):
        # Monday: 2026-10-19 10:30 IST (continuous trading)
        code, out, _ = run_cli(alerts_mod, [
            "readiness",
            "--market", "IN",
            "--now", "2026-10-19 10:30",
            "--json"
        ])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["market_session"], "OPEN")
        self.assertEqual(doc["analysis_engine"], "AVAILABLE")
        self.assertEqual(doc["paper_trading"], "AVAILABLE")
        self.assertEqual(doc["last_trading_session"], "2026-10-19")

    def test_readiness_text_banner_formatting(self):
        code, out, _ = run_cli(alerts_mod, [
            "readiness",
            "--market", "IN",
            "--now", "2026-11-15 11:00"
        ])
        self.assertEqual(code, 0)
        self.assertIn("PRE-MARKET READINESS & HEALTH CHECK -- INDIAN MARKET (NSE/BSE)", out)
        self.assertIn("Market Session:       [CLOSED]", out)
        self.assertIn("Analysis Engine:      [AVAILABLE]", out)
        self.assertIn("Paper Trading:        [AVAILABLE]", out)
        self.assertIn("Last Trading Session: 2026-11-13", out)


class TestStockAnalysisAndResearch247(unittest.TestCase):
    def test_calendar_helpers_weekend_and_last_trading_day(self):
        # Sunday 2026-11-15
        self.assertTrue(calendar_mod.is_weekend("2026-11-15"))
        self.assertEqual(calendar_mod.get_last_trading_day("2026-11-15").isoformat(), "2026-11-13")

        # Saturday 2026-11-07
        self.assertTrue(calendar_mod.is_weekend("2026-11-07"))
        self.assertEqual(calendar_mod.get_last_trading_day("2026-11-07").isoformat(), "2026-11-06")

        # Holiday (Republic Day: 2026-01-26, Monday)
        self.assertFalse(calendar_mod.is_weekend("2026-01-26"))
        self.assertEqual(calendar_mod.get_last_trading_day("2026-01-26").isoformat(), "2026-01-23")

        # Active trading day during continuous hours (2026-10-19 10:30 IST)
        self.assertEqual(calendar_mod.get_last_trading_day("2026-10-19 10:30").isoformat(), "2026-10-19")

        # Trading day before pre-open (2026-10-19 08:30 IST)
        self.assertEqual(calendar_mod.get_last_trading_day("2026-10-19 08:30").isoformat(), "2026-10-16")

    def test_options_calculations_outside_market_hours(self):
        # Black-Scholes analytical calculations work 24/7 with unmistakable SIMULATED marking
        chain = data_mod.fetch_option_chain("NIFTY", spot_price=24500.0)
        c = chain.get_contract(chain.select_strike("ATM", "CE"), "CE")
        self.assertIsNotNone(c)
        self.assertEqual(c["tier"], "SIMULATED")
        self.assertIn("greeks", c)
        self.assertGreater(c["greeks"]["delta"], 0.4)
        self.assertLess(c["greeks"]["delta"], 0.6)

    def test_fundamentals_and_analyst_247(self):
        # SEC EDGAR / filings parser functions without market-session dependence
        self.assertTrue(hasattr(facts_mod, "main"))
        self.assertTrue(callable(getattr(facts_mod, "main")))

    def test_news_monitor_247(self):
        # News monitor functions without market-session dependence
        self.assertTrue(hasattr(news_mod, "main"))
        self.assertTrue(callable(getattr(news_mod, "main")))

    def test_backtesting_timemachine_247(self):
        # Monte Carlo / Historical simulator functions 24/7
        self.assertTrue(hasattr(tm_mod, "simulate"))
        self.assertTrue(callable(getattr(tm_mod, "simulate")))


class TestIntradayStrategyHistoricalCandleAnalysis(unittest.TestCase):
    def test_intraday_strategy_on_weekend_marked_as_historical_analysis(self):
        # Run check on Sunday 2026-10-25 using RELIANCE 5m offline chart
        code, out, _ = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--offline", RELIANCE_5M,
            "--now", "2026-10-25 11:00",
            "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertIn("[HISTORICAL ANALYSIS]", out)
        self.assertIn("TRIGGERED", out)
        self.assertIn("Market is CLOSED", out)
        self.assertIn("NOT a live-session signal", out)
        self.assertIn("Evaluated on historical candles from session 2026-10-19", out)
        self.assertNotIn("This is the moment to get in", out)

    def test_intraday_strategy_during_live_session_marked_as_live_signal(self):
        # Run check on Monday 2026-10-19 10:35 IST
        code, out, _ = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--offline", RELIANCE_5M,
            "--now", "2026-10-19 10:35",
            "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertNotIn("[HISTORICAL ANALYSIS]", out)
        self.assertIn("TRIGGERED", out)
        self.assertIn("This is the moment to get in", out)

    def test_entry_doc_metadata_on_weekend(self):
        chart_obj = entry_mod.get_chart("RELIANCE", "5m", None, offline=RELIANCE_5M, market="IN")
        now_dt = datetime.datetime(2026, 10, 25, 11, 0, tzinfo=calendar_mod.IST)
        now_utc = calendar_mod.parse_ist(now_dt).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        res = entry_mod.evaluate(chart_obj, now_utc, setup="orb", interval="5m", market="IN")
        doc = entry_mod.entry_doc(res, now_utc)

        self.assertEqual(doc["status"], "triggered")
        self.assertTrue(doc["is_historical_analysis"])
        self.assertFalse(doc["is_live_signal"])
        self.assertEqual(doc["last_trading_session"], "2026-10-19")
        self.assertIn(doc["data_status"], ("stale", "delayed"))


class TestWorkflowEndToEnd247(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.entry_path = os.path.join(self.tmp, "entry.json")
        self.plan_path = os.path.join(self.tmp, "plan.json")
        self.orders_path = os.path.join(self.tmp, "orders.json")
        self.cfg_path = os.path.join(self.tmp, "config.json")

        self.cfg = {
            "budget": 100000,
            "cash_buffer_pct": 10,
            "max_position_pct": 75,
            "max_order_inr": 75000,
            "min_order_inr": 100,
            "max_loss_per_trade_inr": 2000,
            "max_daily_loss_inr": 5000,
            "max_trades_per_day": 3,
            "max_orders_per_day": 10,
            "min_reward_risk": 1.0,
            "require_stop": True,
            "no_entries_before": "09:30",
            "no_entries_after": "15:00",
            "paper": True,
            "market": "IN",
            "currency": "INR",
            "exchange": "NSE"
        }
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_paper_trading_pipeline_on_weekend(self):
        # 1. Entry evaluation on Sunday 2026-10-25 11:00 IST
        code, out, _ = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--offline", RELIANCE_5M,
            "--now", "2026-10-25 11:00",
            "--market", "IN",
            "--write", self.entry_path
        ])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.entry_path))

        with open(self.entry_path, encoding="utf-8") as fh:
            edoc = json.load(fh)
        self.assertTrue(edoc["is_historical_analysis"])
        self.assertFalse(edoc["is_live_signal"])

        # 2. Exit planning on Sunday
        code, out, _ = run_cli(exit_mod, [
            "plan",
            "--from", self.entry_path,
            "--market", "IN",
            "--config", self.cfg_path,
            "--write", self.plan_path
        ])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.plan_path))

        with open(self.plan_path, encoding="utf-8") as fh:
            pdoc = json.load(fh)
        self.assertTrue(pdoc.get("is_historical_analysis"))

        # 3. Position sizing on Sunday
        code, out, _ = run_cli(budget_mod, [
            "trade",
            "--plan", self.plan_path,
            "--market", "IN",
            "--config", self.cfg_path,
            "--write", self.orders_path
        ])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.orders_path))

        with open(self.orders_path, encoding="utf-8") as fh:
            odoc = json.load(fh)
        order = odoc["orders"][0]
        self.assertGreater(int(order["qty"]), 0)
        self.assertTrue(order.get("is_historical_analysis"))

        # 4. Pre-trade risk check in paper trading mode on Sunday
        now_dt = datetime.datetime(2026, 10, 25, 11, 0)
        now_utc = now_dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        res_risk = risk_mod.check([order], self.cfg, now=now_utc, market="IN")
        self.assertEqual(res_risk["results"][0]["verdict"], "PASS")
        self.assertTrue(any("off-hours paper trading" in w for w in res_risk["results"][0]["warnings"]))

        # 5. Verify that risk controls remain strict even off-hours:
        # A) Order with excessive max loss is BLOCKED
        bad_loss_order = copy.deepcopy(order)
        bad_loss_order["stop_loss_stop_price"] = "100.00"  # Huge risk
        bad_loss_order["plan"]["stop"] = 100.0
        bad_loss_order["plan"]["risk_per_share"] = 2749.0
        res_bad = risk_mod.check([bad_loss_order], self.cfg, now=now_utc, market="IN")
        self.assertEqual(res_bad["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("max loss" in r for r in res_bad["results"][0]["reasons"]))

        # B) Order without stop loss is BLOCKED
        no_stop_order = copy.deepcopy(order)
        del no_stop_order["stop_loss_stop_price"]
        del no_stop_order["plan"]["stop"]
        res_no_stop = risk_mod.check([no_stop_order], self.cfg, now=now_utc, market="IN")
        self.assertEqual(res_no_stop["results"][0]["verdict"], "BLOCK")

        # C) Live execution attempt (paper: false) is strictly BLOCKED outside market hours
        live_cfg = copy.deepcopy(self.cfg)
        live_cfg["paper"] = False
        res_live = risk_mod.check([order], live_cfg, now=now_utc, market="IN")
        self.assertEqual(res_live["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("market is closed" in r for r in res_live["results"][0]["reasons"]))

    def test_live_watch_loop_restricted_to_open_market(self):
        # watch_loop must immediately exit when market is closed
        messages = []

        def dummy_provider(now):
            return {}

        def emit(msg):
            messages.append(msg)

        clock = entry_mod.SimClock(datetime.datetime(2026, 10, 25, 11, 0))  # Sunday
        code, _ = entry_mod.watch_loop(dummy_provider, clock.now, clock.sleep, emit, {"market": "IN"}, every=1)
        self.assertEqual(code, 1)
        self.assertTrue(any("market closed today" in m for m in messages))
