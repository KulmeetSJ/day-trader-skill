"""Unit and integration tests for BharatTrader AI trading engine adaptation (Phase 4).

Covers:
- trader-entry: Indian symbol ORB check, IST trading window, INR formatting
- trader-exit: Tick size rounding (Rs 0.05), IST flat_by (15:20), Trade-to-Trade warnings
- trader-budget: INR budget allocation, lot size multiples, T2T refusal, bracket orders
- trader-risk: Indian symbol regex, IST market hours, Indian holidays, T2T rejection, PDT exemption
- trader-timemachine: 75-bar regular / 72-bar CAS session grids, offline Indian replay, IST chart
- End-to-end integration: entry -> exit -> sizing -> risk check -> replay simulation
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load, FIX, ROOT

entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
tm_mod = load("trader-timemachine", "timemachine")
instruments_mod = load("trader-alerts", "instruments")
scan_mod = load("trader-scanner", "scan")
ticket_mod = load("trader-broker", "ticket")
alerts_mod = load("trader-alerts", "alerts")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")
RELIANCE_5M = os.path.join(BHARAT_FIXTURES, "RELIANCE-5m.json")
RELIANCE_60D = os.path.join(FIX, "trader-timemachine", "RELIANCE-60d5m.json.gz")


def run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestBharatEntryTimer(unittest.TestCase):
    def test_entry_indian_symbol_orb_check_triggered(self):
        # 2026-10-19 10:35 IST: bar 14 (10:25) has closed at Rs 2849.00 > range high Rs 2848.00
        code, out, err = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--setup", "orb",
            "--now", "2026-10-19 10:35",
            "--offline", RELIANCE_5M,
            "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE", out)
        self.assertIn("TRIGGERED", out)
        self.assertIn("2849.00", out)

    def test_entry_waiting_state(self):
        # 2026-10-19 09:35 IST: within opening range, waiting for breakout
        code, out, err = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--setup", "orb",
            "--now", "2026-10-19 09:35",
            "--offline", RELIANCE_5M,
            "--market", "IN"
        ])
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE", out)
        self.assertIn("WAITING", out)
        self.assertIn("Opening range 9:15-9:30", out)

    def test_entry_outside_market_hours_ist(self):
        # 08:30 IST is before NSE market open (09:15 IST)
        code, out, err = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--setup", "orb",
            "--now", "2026-10-19 08:30",
            "--offline", RELIANCE_5M,
            "--market", "IN"
        ])
        self.assertEqual(code, 1)
        self.assertIn("NOT OPEN YET", out)
        self.assertIn("9:15", out)

    def test_entry_market_profile_resolution(self):
        self.assertEqual(entry_mod.get_market_profile(sym="RELIANCE.NS"), "IN")
        self.assertEqual(entry_mod.get_market_profile(sym="BAJAJ-AUTO"), "IN")
        self.assertEqual(entry_mod.get_market_profile(sym="M&M"), "IN")
        self.assertEqual(entry_mod.get_market_profile(sym="TSLA"), "US")
        self.assertEqual(entry_mod.get_market_profile(sym="AAPL"), "US")


class TestBharatExitPlanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plan_path = os.path.join(self.tmp, "plan.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exit_plan_tick_size_rounding(self):
        # For Indian stocks, tick size is Rs 0.05.
        # Stop rounds down away from entry to tick multiple (floor_tick).
        # Target rounds half-up to tick multiple (round_tick).
        tr, tp = exit_mod.parse_target("2R")
        res = exit_mod.compute(
            entry=Decimal("2850.12"),
            stop_rule="or_mid",
            stop_param=None,
            target_rule=tr,
            target_param=tp,
            or_high=Decimal("2845.00"),
            or_low=Decimal("2835.00"),
            market="IN",
            tick_size=Decimal("0.05")
        )
        self.assertEqual(res["entry"], Decimal("2850.10"))
        self.assertEqual(res["stop"], Decimal("2840.00"))
        self.assertEqual(res["risk"], Decimal("10.10"))
        self.assertEqual(res["target"], Decimal("2870.30"))
        self.assertEqual(res["tick_size"], Decimal("0.05"))

    def test_exit_cli_plan_and_show(self):
        code, out, err = run_cli(exit_mod, [
            "plan",
            "--symbol", "RELIANCE",
            "--entry", "2850.00",
            "--stop", "pct:1.0",
            "--target", "2R",
            "--time", "09:35",
            "--market", "IN",
            "--write", self.plan_path
        ])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.plan_path))
        with open(self.plan_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["symbol"], "RELIANCE")
        self.assertEqual(saved["market"], "IN")
        self.assertEqual(saved["flat_by"], "15:20")
        self.assertEqual(saved["tick_size"], 0.05)

        # Test show command
        code_s, out_s, err_s = run_cli(exit_mod, ["show", "--plan", self.plan_path])
        self.assertEqual(code_s, 0)
        self.assertIn("RELIANCE", out_s)
        self.assertIn("15:20 IST", out_s)


class TestBharatBudgetAndSizing(unittest.TestCase):
    def test_shares_for_inr_budget_and_loss(self):
        # 100,000 INR budget / 2,850 entry = 35 shares
        sh = budget_mod.shares_for(100000.0, 2850.0, lot_size=1)
        self.assertEqual(sh, 35)

        # 2,000 INR max loss / 20 risk = 100 shares
        sh_loss = budget_mod.shares_for(2000.0, 20.0, lot_size=1)
        self.assertEqual(sh_loss, 100)

    def test_shares_for_lot_size_multiples(self):
        # Index / F&O derivative contract with lot_size = 50
        sh1 = budget_mod.shares_for(5000.0, 20.0, lot_size=50)
        self.assertEqual(sh1, 250)
        self.assertEqual(sh1 % 50, 0)

        # 5600 / 20 = 280 shares floored to nearest lot of 50 -> 250 shares
        sh2 = budget_mod.shares_for(5600.0, 20.0, lot_size=50)
        self.assertEqual(sh2, 250)

    def test_budget_trade_to_trade_refusal(self):
        plan = {
            "symbol": "SUZLON", "side": "buy", "entry": 50.00, "stop": 48.00, "target": 54.00,
            "risk_per_share": 2.00, "market": "IN", "setup": "orb", "entry_time": "09:35 AM", "flat_by": "15:20",
            "is_trade_to_trade": True
        }
        cfg = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 100000,
               "max_loss_per_trade_usd": 2000, "paper": True}
        res = budget_mod.size_trade(plan, cfg, today="2026-10-19")
        self.assertEqual(res["qty"], 0)
        self.assertIn("Trade-to-Trade", res["refusal"])
        self.assertIn("SEBI rules", res["refusal"])
        self.assertIsNone(budget_mod.bracket_order(res))

    def test_bracket_order_generation_inr(self):
        plan = {
            "symbol": "RELIANCE", "side": "buy", "entry": 2850.00, "stop": 2830.00, "target": 2890.00,
            "risk_per_share": 20.00, "market": "IN", "setup": "orb", "entry_time": "09:35 AM", "flat_by": "15:20"
        }
        cfg = {
            "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 100000,
            "max_loss_per_trade_usd": 2000, "min_order_usd": 100, "paper": True
        }
        res = budget_mod.size_trade(plan, cfg, today="2026-10-19")
        self.assertGreater(res["qty"], 0)
        self.assertEqual(res["currency"], "INR")
        self.assertEqual(res["market"], "IN")

        order = budget_mod.bracket_order(res)
        self.assertEqual(order["symbol"], "RELIANCE")
        self.assertEqual(order["currency"], "INR")
        self.assertEqual(order["market"], "IN")
        self.assertEqual(order["side"], "buy")
        self.assertIn("RELIANCE", order["why"])


class TestBharatPreTradeRisk(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 100000,
            "max_loss_per_trade_usd": 2000, "min_order_usd": 100, "paper": True,
            "require_stop": True, "min_reward_risk": 1.0, "no_entries_before": "09:30", "no_entries_after": "15:00",
            "max_trades_per_day": 3, "max_daily_loss_usd": 5000, "max_orders_per_day": 10
        }

    def test_risk_symbol_validation_indian(self):
        self.assertTrue(risk_mod.TICKER_IN.match("RELIANCE"))
        self.assertTrue(risk_mod.TICKER_IN.match("TCS"))
        self.assertTrue(risk_mod.TICKER_IN.match("M&M"))
        self.assertTrue(risk_mod.TICKER_IN.match("BAJAJ-AUTO"))
        self.assertTrue(risk_mod.TICKER_IN.match("L&TFH"))
        self.assertIsNone(risk_mod.TICKER_IN.match("invalid_symbol!"))

    def test_risk_ist_trading_window(self):
        # 10:30 IST on Monday 2026-10-19
        dt_ist = datetime.datetime(2026, 10, 19, 10, 30)
        fails, warns = risk_mod.window_check_in(dt_ist, self.cfg)
        self.assertEqual(len(fails), 0)

        # 08:30 IST is before open
        early_ist = datetime.datetime(2026, 10, 19, 8, 30)
        fails_e, _ = risk_mod.window_check_in(early_ist, self.cfg)
        self.assertTrue(any("market is closed" in f for f in fails_e))

        # 16:30 IST is after close
        late_ist = datetime.datetime(2026, 10, 19, 16, 30)
        fails_l, _ = risk_mod.window_check_in(late_ist, self.cfg)
        self.assertTrue(any("market is closed" in f for f in fails_l))

    def test_risk_indian_holiday_check(self):
        # Republic Day: 2026-01-26 (Monday) is an official Indian market holiday
        holiday_ist = datetime.datetime(2026, 1, 26, 10, 30)
        fails, warns = risk_mod.window_check_in(holiday_ist, self.cfg)
        self.assertTrue(any("holiday" in f.lower() for f in fails))

    def test_risk_trade_to_trade_rejection(self):
        order = {
            "symbol": "SUZLON", "side": "buy", "qty": "100", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "54.00", "stop_loss_stop_price": "48.00",
            "plan": {"entry": 50.0, "stop": 48.0, "target": 54.0, "risk_per_share": 2.0, "setup": "orb", "flat_by": "15:20", "market": "IN", "is_trade_to_trade": True},
            "market": "IN", "is_trade_to_trade": True
        }
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], self.cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("trade-to-trade" in r.lower() for r in res["results"][0]["reasons"]))

    def test_risk_pass_valid_indian_order(self):
        order = {
            "symbol": "RELIANCE", "side": "buy", "qty": "20", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "2890.00", "stop_loss_stop_price": "2830.00",
            "plan": {"entry": 2850.0, "stop": 2830.0, "target": 2890.0, "risk_per_share": 20.0, "setup": "orb", "flat_by": "15:20", "market": "IN"},
            "market": "IN", "currency": "INR"
        }
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], self.cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        self.assertEqual(res["results"][0]["reasons"], [])


class TestBharatTimeMachine(unittest.TestCase):
    def test_timemachine_session_grid_bars(self):
        # Regular NSE session: 75 bars
        self.assertEqual(tm_mod.SESSION_BARS_IN, 75)
        # CAS V1: 72 bars
        days = {
            "2026-10-15": [None] * 75,
            "2026-10-16": [None] * 72,
            "2026-10-17": [None] * 40 # partial
        }
        full, short = tm_mod.full_sessions(days, "2026-10-18", market="IN")
        self.assertEqual(len(full), 2)
        self.assertEqual(len(short), 1)

    def test_timemachine_replay_simulation_offline(self):
        code, out, err = run_cli(tm_mod, [
            "run",
            "--symbol", "RELIANCE",
            "--date", "2026-10-19",
            "--entry-time", "09:55",
            "--entry", "2840.00",
            "--stop", "2820.00",
            "--target", "2880.00",
            "--offline", RELIANCE_60D,
            "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE", out)
        self.assertIn("9:55 AM to 3:30 PM (67 five-minute bars)", out)
        self.assertIn("target first", out)
        self.assertIn("stop first", out)

    def test_timemachine_chart_ist_axis(self):
        code, out, err = run_cli(tm_mod, [
            "chart",
            "--symbol", "RELIANCE",
            "--date", "2026-10-19",
            "--entry-time", "09:55",
            "--entry", "2840.00",
            "--stop", "2820.00",
            "--target", "2880.00",
            "--offline", RELIANCE_60D,
            "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertIn("3:30", out)
        self.assertIn("9:55", out)
        self.assertIn("RELIANCE", out)


class TestBharatEndToEndPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plan_path = os.path.join(self.tmp, "plan.json")
        self.watchlist_path = os.path.join(self.tmp, "watchlist.json")
        self.entry_path = os.path.join(self.tmp, "entry.json")
        self.orders_path = os.path.join(self.tmp, "orders.json")
        self.config_path = os.path.join(self.tmp, "config.json")
        self.risk_path = os.path.join(self.tmp, "risk.json")
        self.broker_path = os.path.join(self.tmp, "broker.json")
        self.portfolio_path = os.path.join(self.tmp, "portfolio.json")
        self.alerts_path = os.path.join(self.tmp, "alerts.json")
        self.alerts_state_path = os.path.join(self.tmp, "alerts_state.json")
        self.prices_path = os.path.join(self.tmp, "prices.json")
        shutil.copy(os.path.join(ROOT, "trader-budget", "config.in.example.json"), self.config_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_complete_indian_trading_workflow(self):
        # Step 1: Entry timing check (triggered at 10:35 IST)
        code_e, out_e, _ = run_cli(entry_mod, [
            "check", "RELIANCE",
            "--setup", "orb",
            "--now", "2026-10-19 10:35",
            "--offline", RELIANCE_5M,
            "--market", "IN"
        ])
        self.assertEqual(code_e, 0)
        self.assertIn("TRIGGERED", out_e)

        # Step 2: Exit planner creates plan with tick size Rs 0.05 and 15:20 flat_by
        code_p, out_p, _ = run_cli(exit_mod, [
            "plan",
            "--symbol", "RELIANCE",
            "--entry", "2850.00",
            "--stop", "pct:0.7",
            "--target", "2R",
            "--date", "2026-10-19",
            "--time", "09:35",
            "--market", "IN",
            "--write", self.plan_path
        ])
        self.assertEqual(code_p, 0)
        with open(self.plan_path, encoding="utf-8") as f:
            plan = json.load(f)

        self.assertEqual(plan["symbol"], "RELIANCE")
        self.assertEqual(plan["market"], "IN")
        self.assertEqual(plan["flat_by"], "15:20")
        self.assertEqual(plan["tick_size"], 0.05)

        # Step 3: Position sizing in INR
        cfg = {
            "budget": 200000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 200000,
            "max_loss_per_trade_usd": 2500, "min_order_usd": 100, "paper": True,
            "require_stop": True, "min_reward_risk": 1.0, "no_entries_before": "09:30", "no_entries_after": "15:00",
            "max_trades_per_day": 3, "max_daily_loss_usd": 5000, "max_orders_per_day": 10
        }
        res_size = budget_mod.size_trade(plan, cfg, today="2026-10-19")
        self.assertGreater(res_size["qty"], 0)
        self.assertEqual(res_size["currency"], "INR")
        order = budget_mod.bracket_order(res_size)
        self.assertEqual(order["currency"], "INR")
        self.assertEqual(order["market"], "IN")

        # Step 4: Pre-trade risk verification
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res_risk = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res_risk["results"][0]["verdict"], "PASS")

        # Step 5: Historical Monte Carlo replay
        code_t, out_t, _ = run_cli(tm_mod, [
            "run",
            "--plan", self.plan_path,
            "--offline", RELIANCE_60D,
            "--market", "IN"
        ])
        self.assertEqual(code_t, 0)
        self.assertIn("10,000 replays", out_t)
        self.assertIn("target first", out_t)

    def test_full_lifecycle_scan_to_flatten(self):
        # 1. Scanner produces watchlist
        code, out, _ = run_cli(scan_mod, [
            "orb", "--market", "IN", "--offline", BHARAT_FIXTURES,
            "--now", "2026-10-19 10:35", "--out", self.watchlist_path
        ])
        self.assertEqual(code, 0)
        with open(self.watchlist_path, encoding="utf-8") as f:
            wl = json.load(f)
        self.assertEqual(wl["hits"][0]["symbol"], "RELIANCE")

        # 2. Entry analysis
        code, out, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", RELIANCE_5M,
            "--now", "2026-10-19 10:35", "--market", "IN", "--write", self.entry_path
        ])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED", out)
        with open(self.entry_path, encoding="utf-8") as f:
            ent = json.load(f)
        self.assertEqual(ent["symbol"], "RELIANCE")
        self.assertEqual(ent["status"], "triggered")

        # 3. Exit planner
        code, out, _ = run_cli(exit_mod, [
            "plan", "--from", self.entry_path, "--market", "IN", "--write", self.plan_path
        ])
        self.assertEqual(code, 0)
        with open(self.plan_path, encoding="utf-8") as f:
            plan = json.load(f)
        self.assertEqual(plan["symbol"], "RELIANCE")
        self.assertEqual(plan["flat_by"], "15:20")
        self.assertEqual(plan["tick_size"], 0.05)

        # 4. Position sizing
        code, out, _ = run_cli(budget_mod, [
            "--plan", self.plan_path, "--config", self.config_path,
            "--now", "2026-10-19 10:35", "--market", "IN", "--write", self.orders_path
        ])
        self.assertEqual(code, 0)
        with open(self.orders_path, encoding="utf-8") as f:
            ord_doc = json.load(f)
        order = ord_doc["orders"][0]
        self.assertEqual(order["symbol"], "RELIANCE")
        self.assertEqual(order["qty"], "26")
        self.assertEqual(order["market"], "IN")

        # 5. Risk check
        code, out, _ = run_cli(risk_mod, [
            "--orders", self.orders_path, "--config", self.config_path,
            "--now", "2026-10-19 10:35", "--market", "IN", "--out", self.risk_path
        ])
        self.assertEqual(code, 0)
        self.assertIn("PASS  #1 buy RELIANCE 26 shares", out)

        # 6. Ticket show
        code, out, _ = run_cli(ticket_mod, [
            "show", "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.config_path, "--state", self.broker_path,
            "--now", "2026-10-19 10:35", "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER account, Paper Broker (NSE/BSE)", out)
        self.assertIn("BUY 26 RELIANCE", out)
        self.assertIn("READY: trader-risk PASS", out)

        # 7. Ticket approve
        code, out, _ = run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy 26 RELIANCE",
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.config_path, "--state", self.broker_path,
            "--now", "2026-10-19 10:35", "--market", "IN"
        ])
        self.assertEqual(code, 0)
        self.assertIn("Recorded your yes for order 1", out)

        # 8. Ticket result & fill
        code, out, _ = run_cli(ticket_mod, [
            "result", "--index", "1", "--status", "placed",
            "--filled-qty", "26", "--filled-price", "2849.00",
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.config_path, "--portfolio", self.portfolio_path,
            "--state", self.broker_path, "--now", "2026-10-19 10:35", "--market", "IN"
        ])
        self.assertEqual(code, 0)
        with open(self.portfolio_path, encoding="utf-8") as f:
            pf = json.load(f)
        self.assertEqual(pf["cash"], 25926.0)
        self.assertEqual(pf["currency"], "INR")
        self.assertEqual(pf["positions"][0]["ticker"], "RELIANCE")
        self.assertEqual(pf["positions"][0]["shares"], 26.0)

        # 9. Alerts add from plan
        code, out, _ = run_cli(alerts_mod, [
            "add", "--from-plan", "--plan", self.plan_path, "--config", self.config_path,
            "--rules", self.alerts_path, "--state", self.alerts_state_path
        ])
        self.assertEqual(code, 0)
        with open(self.alerts_path, encoding="utf-8") as f:
            alerts_doc = json.load(f)
        self.assertEqual(len(alerts_doc["rules"]), 3)

        # 10. Alerts check mid-day
        code, out, _ = run_cli(alerts_mod, [
            "check", "--rules", self.alerts_path, "--state", self.alerts_state_path,
            "--prices-out", self.prices_path,
            "--now", "2026-10-19 10:40", "--market", "IN", "--offline", BHARAT_FIXTURES
        ])
        self.assertIn("NSE OPEN (10:40 IST", out)
        self.assertIn("Nothing hit", out)

        # 11. Alerts check flat-by
        code, out, _ = run_cli(alerts_mod, [
            "check", "--rules", self.alerts_path, "--state", self.alerts_state_path,
            "--prices-out", self.prices_path,
            "--now", "2026-10-19 15:20", "--market", "IN", "--offline", BHARAT_FIXTURES
        ])
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close: it is 15:20 IST", out)

        # 12. Flatten position
        code, out, _ = run_cli(ticket_mod, [
            "flatten", "--symbol", "RELIANCE", "--config", self.config_path,
            "--portfolio", self.portfolio_path, "--plan", self.plan_path,
            "--orders", self.orders_path, "--now", "2026-10-19 15:20"
        ])
        self.assertEqual(code, 0)
        self.assertIn("SELL 26 shares of RELIANCE", out)
        self.assertIn("(flat by 15:20 IST)", out)

        # 13. Risk check on flatten order
        code, out, _ = run_cli(risk_mod, [
            "--orders", self.orders_path, "--config", self.config_path,
            "--portfolio", self.portfolio_path, "--state", self.broker_path,
            "--now", "2026-10-19 15:20", "--market", "IN", "--out", self.risk_path
        ])
        self.assertEqual(code, 0)
        self.assertIn("PASS  #1 sell RELIANCE 26 shares, exit", out)

        # 14. Approve flatten order
        code, out, _ = run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes sell 26 RELIANCE",
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.config_path, "--state", self.broker_path,
            "--now", "2026-10-19 15:20"
        ])
        self.assertEqual(code, 0)

        # 15. Record fill on flatten
        code, out, _ = run_cli(ticket_mod, [
            "result", "--index", "1", "--status", "placed",
            "--filled-qty", "26", "--filled-price", "2863.00",
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.config_path, "--portfolio", self.portfolio_path,
            "--state", self.broker_path, "--now", "2026-10-19 15:20"
        ])
        self.assertEqual(code, 0)
        with open(self.portfolio_path, encoding="utf-8") as f:
            pf_final = json.load(f)
        self.assertEqual(pf_final["positions"], [])
        self.assertEqual(pf_final["cash"], 100364.0)

        # 16. Verify log
        code, out, _ = run_cli(ticket_mod, [
            "log", "--state", self.broker_path, "--config", self.config_path,
            "--now", "2026-10-19 15:20"
        ])
        self.assertEqual(code, 0)
        self.assertIn("2 placed today", out)


if __name__ == "__main__":
    unittest.main()
