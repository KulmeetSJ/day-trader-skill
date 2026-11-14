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
from unittest.mock import patch

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

    def test_yahoo_delayed_data_during_open_session_never_live_signal(self):
        # Online Yahoo delayed provider fetch during open trading hours (2026-10-19 10:35 IST)
        with open(RELIANCE_5M, encoding="utf-8") as fh:
            raw_chart = json.load(fh)
        raw_text = json.dumps(raw_chart)
        with patch.object(entry_mod, "yahoo_get", return_value=raw_text):
            code, out, _ = run_cli(entry_mod, [
                "check", "RELIANCE",
                "--now", "2026-10-19 10:35",
                "--market", "IN"
            ])
        self.assertEqual(code, 0)
        self.assertNotIn("[HISTORICAL ANALYSIS]", out)
        self.assertNotIn("[OFFLINE FIXTURE", out)
        self.assertIn("Yahoo delayed provider", out)
        self.assertIn("TRIGGERED", out)
        self.assertIn("CANDIDATE DETECTED ON DELAYED DATA", out)
        self.assertIn("NOT a live market signal", out)
        self.assertNotIn("This is the moment to get in", out)

        with patch.object(entry_mod, "yahoo_get", return_value=raw_text):
            code_j, out_j, _ = run_cli(entry_mod, [
                "check", "RELIANCE",
                "--now", "2026-10-19 10:35",
                "--market", "IN",
                "--json"
            ])
        self.assertEqual(code_j, 0)
        doc_delayed = json.loads(out_j)
        self.assertTrue(doc_delayed["is_market_open"])
        self.assertFalse(doc_delayed["is_live_data"])
        self.assertFalse(doc_delayed["is_live_signal"])
        self.assertFalse(doc_delayed["is_fixture"])
        self.assertFalse(doc_delayed["is_historical_analysis"])
        self.assertEqual(doc_delayed["provider"], "yahoo")
        self.assertEqual(doc_delayed["data_tier"], "DELAYED")
        self.assertEqual(doc_delayed["data_status"], "delayed")

        # Even if Yahoo's regularMarketTime happens to be within seconds of now, Yahoo lacks
        # live quote capabilities and MUST stay DELAYED with is_live_data=False and is_live_signal=False.
        now_utc = entry_mod.parse_now("2026-10-19 10:35", market="IN")
        fresh_chart = copy.deepcopy(raw_chart)
        fresh_chart["chart"]["result"][0]["meta"]["regularMarketTime"] = entry_mod.ts_of(now_utc) - 30
        with patch.object(entry_mod, "yahoo_get", return_value=json.dumps(fresh_chart)):
            code_f, out_f, _ = run_cli(entry_mod, [
                "check", "RELIANCE",
                "--now", "2026-10-19 10:35",
                "--market", "IN",
                "--json"
            ])
        self.assertEqual(code_f, 0)
        doc_yahoo_fresh = json.loads(out_f)
        self.assertTrue(doc_yahoo_fresh["is_market_open"])
        self.assertFalse(doc_yahoo_fresh["is_live_data"])
        self.assertFalse(doc_yahoo_fresh["is_live_signal"])
        self.assertEqual(doc_yahoo_fresh["data_tier"], "DELAYED")
        self.assertEqual(doc_yahoo_fresh["data_status"], "delayed")

        # Useful delayed candidate analysis must still work end-to-end (exit -> size -> risk PASS)
        tmp = tempfile.mkdtemp()
        try:
            entry_p = os.path.join(tmp, "entry.json")
            plan_p = os.path.join(tmp, "plan.json")
            orders_p = os.path.join(tmp, "orders.json")
            cfg_p = os.path.join(tmp, "config.json")
            cfg = {
                "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75,
                "max_order_inr": 75000, "min_order_inr": 100, "max_loss_per_trade_inr": 2000,
                "max_daily_loss_inr": 5000, "max_trades_per_day": 3, "max_orders_per_day": 10,
                "min_reward_risk": 1.0, "require_stop": True, "no_entries_before": "09:30",
                "no_entries_after": "15:00", "paper": True, "market": "IN", "currency": "INR", "exchange": "NSE",
            }
            with open(cfg_p, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            with patch.object(entry_mod, "yahoo_get", return_value=raw_text):
                self.assertEqual(run_cli(entry_mod, [
                    "check", "RELIANCE", "--now", "2026-10-19 10:35", "--market", "IN", "--write", entry_p
                ])[0], 0)
            self.assertEqual(run_cli(exit_mod, [
                "plan", "--from", entry_p, "--target", "4R", "--market", "IN", "--config", cfg_p, "--write", plan_p
            ])[0], 0)
            self.assertEqual(run_cli(budget_mod, [
                "--now", "2026-10-19 10:35", "trade", "--plan", plan_p, "--market", "IN",
                "--config", cfg_p, "--write", orders_p
            ])[0], 0)
            with open(orders_p, encoding="utf-8") as fh:
                odoc = json.load(fh)
            order = odoc["orders"][0]
            self.assertGreater(int(order["qty"]), 0)
            self.assertTrue(order.get("is_market_open"))
            self.assertFalse(order.get("is_live_data"))
            self.assertFalse(order.get("is_live_signal"))
            self.assertEqual(order.get("data_tier"), "DELAYED")
            res_risk = risk_mod.check([order], cfg, now=now_utc, market="IN")
            self.assertEqual(res_risk["results"][0]["verdict"], "PASS")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_yahoo_historical_data_outside_market_hours(self):
        with open(RELIANCE_5M, encoding="utf-8") as fh:
            raw_text = fh.read()
        for now_str in ("2026-10-19 16:05", "2026-10-25 11:00", "2026-10-20 11:00"):
            with patch.object(entry_mod, "yahoo_get", return_value=raw_text):
                code, out, _ = run_cli(entry_mod, [
                    "check", "RELIANCE", "--now", now_str, "--market", "IN", "--json"
                ])
            self.assertEqual(code, 0, now_str)
            doc = json.loads(out)
            self.assertFalse(doc["is_market_open"], now_str)
            self.assertFalse(doc["is_live_data"], now_str)
            self.assertFalse(doc["is_live_signal"], now_str)
            self.assertTrue(doc["is_historical_analysis"], now_str)
            self.assertEqual(doc["data_tier"], "HISTORICAL", now_str)

    def test_offline_fixture_during_market_hours_never_live_signal(self):
        # Offline fixture replayed inside market hours (2026-10-19 10:35 IST) must NEVER claim to be live
        code, out, _ = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--offline", RELIANCE_5M,
            "--now", "2026-10-19 10:35",
            "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertIn("[OFFLINE FIXTURE -- historical replay, NOT live]", out)
        self.assertIn("NOT a live market signal", out)
        self.assertIn("TRIGGERED", out)
        self.assertNotIn("This is the moment to get in", out)

        code_j, out_j, _ = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--offline", RELIANCE_5M,
            "--now", "2026-10-19 10:35",
            "--market", "IN",
            "--json"
        ])
        self.assertEqual(code_j, 0)
        doc = json.loads(out_j)
        self.assertTrue(doc["is_market_open"])
        self.assertFalse(doc["is_live_data"])
        self.assertFalse(doc["is_live_signal"])
        self.assertTrue(doc["is_historical_analysis"])
        self.assertTrue(doc["is_fixture"])
        self.assertEqual(doc["data_tier"], "FIXTURE")
        self.assertEqual(doc["data_status"], "fixture")
        self.assertEqual(doc["last_trading_session"], "2026-10-19")

    def test_current_live_mock_provider_during_market_hours(self):
        with open(RELIANCE_5M, encoding="utf-8") as fh:
            raw_chart = json.load(fh)
        parsed = entry_mod.parse_chart(json.dumps(raw_chart), "RELIANCE.NS")
        full_bars = parsed["bars"]

        class LiveCurrentMockProvider(data_mod.MarketDataProvider):
            provider_name = "mock_live"
            requires_auth = False
            CAPABILITIES = {
                data_mod.CAPABILITY_INTRADAY_CANDLES: True,
                data_mod.CAPABILITY_LIVE_QUOTES: True,
                data_mod.CAPABILITY_HISTORICAL_CANDLES: True,
                data_mod.CAPABILITY_INSTRUMENT_MASTERS: True,
                data_mod.CAPABILITY_MARKET_STATUS: True,
            }
            mode = "current"

            def __init__(self, config=None):
                self.config = config or {}

            def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
                now_ts = int(now.timestamp()) if now is not None else entry_mod.ts_of(entry_mod.parse_now("2026-10-19 10:35", market="IN"))
                if self.mode == "missing":
                    bars = []
                    ts = None
                    status = data_mod.DataStatus.UNAVAILABLE
                    is_live = False
                elif self.mode == "yesterday_only":
                    bars = [dict(b, t=b["t"] - 3 * 86400) for b in full_bars]
                    ts = bars[-1]["t"]
                    status = data_mod.DataStatus.HISTORICAL
                    is_live = False
                elif self.mode == "stale_ts":
                    bars = [b for b in full_bars if b["t"] + 300 <= now_ts]
                    ts = now_ts - 3600
                    status = data_mod.DataStatus.CURRENT
                    is_live = True
                elif self.mode == "stale_bars":
                    # Quote claims current, yet intraday candles stopped arriving 60m ago
                    bars = [b for b in full_bars if b["t"] <= now_ts - 3900]
                    ts = now_ts - 10
                    status = data_mod.DataStatus.CURRENT
                    is_live = True
                else:
                    bars = [b for b in full_bars if b["t"] + 300 <= now_ts]
                    ts = now_ts - 15
                    status = data_mod.DataStatus.CURRENT
                    is_live = True
                return data_mod._standardize_payload(
                    {
                        "symbol": "RELIANCE",
                        "exchange": exchange,
                        "interval": interval,
                        "bars": bars,
                        "prev_close": parsed.get("prev_close"),
                        "meta": {"symbol": "RELIANCE.NS", "dataGranularity": interval, "regularMarketTime": ts},
                    },
                    provider_name=self.provider_name,
                    symbol="RELIANCE",
                    exchange=exchange,
                    data_status=status,
                    tier=data_mod.DataTier.REAL_TIME,
                    timestamp=ts,
                    is_live=is_live,
                    is_simulated=False,
                    now=now,
                )

        old_env = os.environ.get("INDIA_MARKET_DATA_PROVIDER")
        data_mod.register_provider("mock_live", LiveCurrentMockProvider)
        try:
            os.environ["INDIA_MARKET_DATA_PROVIDER"] = "mock_live"

            # 1. Triggered breakout on genuinely current live data during open market (application clock mocked, no --now)
            LiveCurrentMockProvider.mode = "current"
            now_1035 = entry_mod.parse_now("2026-10-19 10:35", market="IN")
            with patch.object(entry_mod, "utc_now", return_value=now_1035):
                code, out_txt, _ = run_cli(entry_mod, [
                    "check", "RELIANCE", "--market", "IN"
                ])
                self.assertEqual(code, 0)
                self.assertIn("mock_live live provider, CURRENT data", out_txt)
                self.assertIn("This is the moment to get in", out_txt)

                code_j, out_j, _ = run_cli(entry_mod, [
                    "check", "RELIANCE", "--market", "IN", "--json"
                ])
            self.assertEqual(code_j, 0)
            doc_live = json.loads(out_j)
            self.assertEqual(doc_live["provider"], "mock_live")
            self.assertTrue(doc_live["is_market_open"])
            self.assertTrue(doc_live["is_live_data"])
            self.assertTrue(doc_live["is_live_signal"])
            self.assertFalse(doc_live.get("clock_override", False))
            self.assertEqual(doc_live["data_tier"], "CURRENT")
            self.assertEqual(doc_live["data_status"], "current")

            # 1b. Same live provider with --now clock override: never currently actionable or live
            with patch.object(entry_mod, "utc_now", return_value=now_1035):
                code_now, out_now_txt, _ = run_cli(entry_mod, [
                    "check", "RELIANCE", "--now", "2026-10-19 10:35", "--market", "IN"
                ])
                _, out_now_j, _ = run_cli(entry_mod, [
                    "check", "RELIANCE", "--now", "2026-10-19 10:35", "--market", "IN", "--json"
                ])
            self.assertEqual(code_now, 0)
            self.assertNotIn("This is the moment to get in", out_now_txt)
            self.assertIn("NOT a current live entry signal", out_now_txt)
            doc_now = json.loads(out_now_j)
            self.assertTrue(doc_now["clock_override"])
            self.assertFalse(doc_now["is_live_data"])
            self.assertFalse(doc_now["is_live_signal"])
            self.assertTrue(doc_now["is_historical_analysis"])
            self.assertEqual(doc_now["data_tier"], "HISTORICAL")

            # 2. Watching (no breakout yet at 09:40 IST) on live data: is_live_data=True, is_live_signal=False
            now_0940 = entry_mod.parse_now("2026-10-19 09:40", market="IN")
            with patch.object(entry_mod, "utc_now", return_value=now_0940):
                code_w, out_w, _ = run_cli(entry_mod, [
                    "check", "RELIANCE", "--market", "IN", "--json"
                ])
            self.assertEqual(code_w, 1)
            doc_wait = json.loads(out_w)
            self.assertEqual(doc_wait["status"], "waiting")
            self.assertTrue(doc_wait["is_market_open"])
            self.assertTrue(doc_wait["is_live_data"])
            self.assertFalse(doc_wait["is_live_signal"])
            self.assertEqual(doc_wait["data_tier"], "CURRENT")

            # 3. Same live provider outside market hours (after close): never live
            now_1605 = entry_mod.parse_now("2026-10-19 16:05", market="IN")
            with patch.object(entry_mod, "utc_now", return_value=now_1605):
                code_c, out_c, _ = run_cli(entry_mod, [
                    "check", "RELIANCE", "--market", "IN", "--json"
                ])
            self.assertEqual(code_c, 0)
            doc_closed = json.loads(out_c)
            self.assertFalse(doc_closed["is_market_open"])
            self.assertFalse(doc_closed["is_live_data"])
            self.assertFalse(doc_closed["is_live_signal"])
            self.assertTrue(doc_closed["is_historical_analysis"])
            self.assertEqual(doc_closed["data_tier"], "HISTORICAL")

            # 4. Missing or stale data during open market hours: is_market_open stays True, never live
            for bad_mode, expected_status, expected_data_status, expected_tier in (
                ("missing", "no-data", "unavailable", "HISTORICAL"),
                ("yesterday_only", "no-data", "stale", "HISTORICAL"),
                ("stale_ts", "triggered", "stale", "DELAYED"),
                ("stale_bars", "waiting", "stale", "DELAYED"),
            ):
                LiveCurrentMockProvider.mode = bad_mode
                with patch.object(entry_mod, "utc_now", return_value=now_1035):
                    _, out_b, _ = run_cli(entry_mod, [
                        "check", "RELIANCE", "--market", "IN", "--json"
                    ])
                doc_bad = json.loads(out_b)
                self.assertEqual(doc_bad["status"], expected_status, bad_mode)
                self.assertTrue(doc_bad["is_market_open"], bad_mode)
                self.assertFalse(doc_bad["is_live_data"], bad_mode)
                self.assertFalse(doc_bad["is_live_signal"], bad_mode)
                self.assertEqual(doc_bad["data_status"], expected_data_status, bad_mode)
                self.assertEqual(doc_bad["data_tier"], expected_tier, bad_mode)
        finally:
            data_mod.unregister_provider("mock_live")
            if old_env is None:
                os.environ.pop("INDIA_MARKET_DATA_PROVIDER", None)
            else:
                os.environ["INDIA_MARKET_DATA_PROVIDER"] = old_env

    def test_entry_doc_metadata_on_weekend(self):
        chart_obj = entry_mod.get_chart("RELIANCE", "5m", None, offline=RELIANCE_5M, market="IN")
        now_dt = datetime.datetime(2026, 10, 25, 11, 0, tzinfo=calendar_mod.IST)
        now_utc = calendar_mod.parse_ist(now_dt).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        res = entry_mod.evaluate(chart_obj, now_utc, setup="orb", interval="5m", market="IN")
        doc = entry_mod.entry_doc(res, now_utc)

        self.assertEqual(doc["status"], "triggered")
        self.assertTrue(doc["is_historical_analysis"])
        self.assertFalse(doc["is_live_signal"])
        self.assertTrue(doc["is_fixture"])
        self.assertEqual(doc["data_tier"], "FIXTURE")
        self.assertEqual(doc["last_trading_session"], "2026-10-23")
        self.assertEqual(doc["candle_session"], "2026-10-19")
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
            "--target", "4R",
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


class TestHolidayAndSessionResolution(unittest.TestCase):
    """Regression tests for holiday, weekend, special session, and historical session resolution."""

    def test_republic_day_2026_01_26_never_reports_future_session(self):
        # RELIANCE_5M holds 2026-10-19 bars; on Republic Day 2026-01-26 it must NOT resolve to 2026-10-19
        code, out_json, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--market", "IN",
            "--offline", RELIANCE_5M, "--now", "2026-01-26 11:00", "--json"
        ])
        self.assertEqual(code, 1)
        doc = json.loads(out_json)
        self.assertEqual(doc["last_trading_session"], "2026-01-23")
        self.assertNotEqual(doc["last_trading_session"], "2026-10-19")
        self.assertIsNone(doc["candle_session"])
        self.assertEqual(doc["status"], "no-data")
        self.assertEqual(doc["phase"], "closed")
        self.assertFalse(doc["is_live_signal"])
        self.assertTrue(doc["is_historical_analysis"])
        self.assertEqual(doc["data_tier"], "FIXTURE")

        code_txt, out_txt, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--market", "IN",
            "--offline", RELIANCE_5M, "--now", "2026-01-26 11:00"
        ])
        self.assertEqual(code_txt, 1)
        self.assertIn("Session: 2026-01-23", out_txt)
        self.assertNotIn("Session: 2026-10-19", out_txt)

    def test_republic_day_2026_01_26_with_2026_01_23_historical_candles(self):
        with open(RELIANCE_5M, encoding="utf-8") as fh:
            raw = json.load(fh)
        shift_sec = (datetime.date(2026, 1, 23) - datetime.date(2026, 10, 19)).days * 86400
        r0 = raw["chart"]["result"][0]
        r0["timestamp"] = [t + shift_sec for t in r0["timestamp"]]
        r0["meta"]["regularMarketTime"] = r0["meta"]["regularMarketTime"] + shift_sec

        tmp = tempfile.mkdtemp()
        try:
            jan_path = os.path.join(tmp, "RELIANCE.json")
            with open(jan_path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh)
            code, out_json, _ = run_cli(entry_mod, [
                "check", "RELIANCE", "--setup", "orb", "--market", "IN",
                "--offline", jan_path, "--now", "2026-01-26 11:00", "--json"
            ])
            self.assertEqual(code, 0)
            doc = json.loads(out_json)
            self.assertEqual(doc["last_trading_session"], "2026-01-23")
            self.assertEqual(doc["candle_session"], "2026-01-23")
            self.assertEqual(doc["status"], "triggered")
            self.assertFalse(doc["is_live_signal"])
            self.assertTrue(doc["is_historical_analysis"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_weekend_holiday_and_muhurat_special_session_resolution(self):
        # Exchange holiday (Dussehra 2026-10-20 Tue) -> prior session 2026-10-19
        code_h, out_h, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--market", "IN",
            "--offline", RELIANCE_5M, "--now", "2026-10-20 11:00", "--json"
        ])
        self.assertEqual(code_h, 0)
        doc_h = json.loads(out_h)
        self.assertEqual(doc_h["last_trading_session"], "2026-10-19")
        self.assertEqual(doc_h["candle_session"], "2026-10-19")
        self.assertFalse(doc_h["is_live_signal"])

        # Weekend (Sunday 2026-10-25) -> calendar last trading session is Friday 2026-10-23
        code_w, out_w, _ = run_cli(alerts_mod, [
            "readiness", "--market", "IN", "--now", "2026-10-25 11:00", "--json"
        ])
        self.assertEqual(code_w, 0)
        self.assertEqual(json.loads(out_w)["last_trading_session"], "2026-10-23")

        # Special session (Muhurat Trading on Sunday 2026-11-08: 18:00 pre-open, 18:15-19:15 regular)
        self.assertEqual(calendar_mod.get_last_trading_day("2026-11-08 11:00").isoformat(), "2026-11-06")
        self.assertEqual(calendar_mod.get_last_trading_day("2026-11-08 18:30").isoformat(), "2026-11-08")
        self.assertEqual(entry_mod.session(datetime.date(2026, 11, 8), market="IN"),
                         (datetime.time(18, 15), datetime.time(19, 15)))

        code_m1, out_m1, _ = run_cli(alerts_mod, [
            "readiness", "--market", "IN", "--now", "2026-11-08 11:00", "--json"
        ])
        self.assertEqual(code_m1, 0)
        m1 = json.loads(out_m1)
        self.assertEqual(m1["market_session"], "CLOSED")
        self.assertEqual(m1["last_trading_session"], "2026-11-06")

        code_m2, out_m2, _ = run_cli(alerts_mod, [
            "readiness", "--market", "IN", "--now", "2026-11-08 18:30", "--json"
        ])
        self.assertEqual(code_m2, 0)
        m2 = json.loads(out_m2)
        self.assertEqual(m2["market_session"], "OPEN")
        self.assertEqual(m2["last_trading_session"], "2026-11-08")
