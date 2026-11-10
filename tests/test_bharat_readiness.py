"""Unit tests for BharatTrader AI Pre-Market Readiness Check and Configuration Matrix (Phase 5).

Covers:
- Pre-market readiness check CLI (alerts.py readiness --market IN)
- Market calendar, session phase, holiday, and special session handling
- Instrument master freshness and fail-closed validation
- Data provider tier tagging (DELAYED quotes, SIMULATED options)
- Expiry weekday resolution per underlying (NIFTY, FINNIFTY, SENSEX, BANKEX, MIDCPNIFTY)
- Fail-closed refusal on missing, stale or incomplete derivative metadata
- CLI compatibility across size.py trade, check.py positional orders, scan.py daily --strategy
"""

import io
import json
import os
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
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
scan_mod = load("trader-scanner", "scan")


def run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestBharatReadiness(unittest.TestCase):
    def test_readiness_cli_json_on_trading_day(self):
        code, out, err = run_cli(alerts_mod, [
            "readiness",
            "--market", "IN",
            "--now", "2026-10-19 09:05",
            "--json"
        ])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertIn("verdict", doc)
        self.assertIn("checks", doc)
        self.assertEqual(doc["market"], "IN")

        checks_by_name = {c["check"]: c for c in doc["checks"]}
        self.assertIn("Market Calendar & Session", checks_by_name)
        self.assertIn("Instrument Master Cache", checks_by_name)
        self.assertIn("Data Provider & Tiers", checks_by_name)
        self.assertIn("Universe Population", checks_by_name)
        self.assertIn("Derivatives & Expiry Metadata", checks_by_name)
        self.assertIn("Configuration & Safety Limits", checks_by_name)

        # 09:05 IST is pre-open
        cal_check = checks_by_name["Market Calendar & Session"]
        self.assertIn("PRE_OPEN", cal_check["detail"])

    def test_readiness_blocks_when_paper_trading_is_false(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({
                "paper": False,  # Live trading attempt
                "budget": 50000,
                "market": "IN",
            }, fh)
            cfg_path = fh.name

        try:
            code, out, _ = run_cli(alerts_mod, [
                "readiness",
                "--market", "IN",
                "--config", cfg_path,
                "--now", "2026-10-19 10:15",
                "--json"
            ])
            self.assertEqual(code, 1)  # Must exit 1 on block
            doc = json.loads(out)
            self.assertEqual(doc["verdict"], "BLOCKED")
            cfg_check = [c for c in doc["checks"] if c["check"] == "Configuration & Safety Limits"][0]
            self.assertIn(cfg_check["status"], ("BLOCK", "BLOCKED"))
            self.assertIn("Live order execution is disabled", cfg_check["note"])
        finally:
            if os.path.exists(cfg_path):
                os.unlink(cfg_path)

    def test_underlying_expiry_weekday_conventions(self):
        # Authoritative November 2026 NSE contract specifications:
        # All NSE index & stock derivatives expire on Tuesday (weekday = 1).
        # When Tuesday is a holiday (e.g. Dussehra 2026-10-20), rolls back to preceding trading day (Monday 2026-10-19).
        ref_holiday_week = "2026-10-19"  # Monday
        nifty_dussehra = calendar_mod.get_next_expiry(as_of_date=ref_holiday_week, underlying="NIFTY", exchange="NSE")
        self.assertEqual(nifty_dussehra.isoformat(), "2026-10-19")  # Rolled back from holiday Tuesday 2026-10-20
        self.assertEqual(nifty_dussehra.weekday(), 0)  # Monday (holiday adjusted)

        # On normal non-holiday week (e.g. as_of 2026-10-21):
        ref_normal = "2026-10-21"  # Wednesday
        nifty_exp = calendar_mod.get_next_expiry(as_of_date=ref_normal, underlying="NIFTY", exchange="NSE")
        self.assertEqual(nifty_exp.weekday(), 1)  # Tuesday
        self.assertEqual(nifty_exp.isoformat(), "2026-10-27")

        banknifty_exp = calendar_mod.get_next_expiry(as_of_date=ref_normal, underlying="BANKNIFTY", exchange="NSE")
        self.assertEqual(banknifty_exp.weekday(), 1)  # Tuesday
        self.assertEqual(banknifty_exp.isoformat(), "2026-10-27")

        fin_exp = calendar_mod.get_next_expiry(as_of_date=ref_normal, underlying="FINNIFTY", exchange="NSE")
        self.assertEqual(fin_exp.weekday(), 1)  # Tuesday
        self.assertEqual(fin_exp.isoformat(), "2026-10-27")

        mid_exp = calendar_mod.get_next_expiry(as_of_date=ref_normal, underlying="MIDCPNIFTY", exchange="NSE")
        self.assertEqual(mid_exp.weekday(), 1)  # Tuesday
        self.assertEqual(mid_exp.isoformat(), "2026-10-27")

        # BSE Independent Expiry Rules:
        # BSE SENSEX: Friday (4)
        sensex_exp = calendar_mod.get_next_expiry(as_of_date=ref_holiday_week, underlying="SENSEX", exchange="BSE")
        self.assertEqual(sensex_exp.weekday(), 4)  # Friday
        self.assertEqual(sensex_exp.isoformat(), "2026-10-23")

        # BSE BANKEX: Monday (0)
        bankex_exp = calendar_mod.get_next_expiry(as_of_date=ref_normal, underlying="BANKEX", exchange="BSE")
        self.assertEqual(bankex_exp.weekday(), 0)  # Monday
        self.assertEqual(bankex_exp.isoformat(), "2026-10-26")

    def test_fail_closed_derivative_refusal_on_missing_lot_size(self):
        # Derivative order where lot size cannot be authoritatively resolved
        order = {
            "symbol": "UNKNOWNCO26OCT100CE",
            "side": "buy",
            "qty": "1",  # Lot size unknown / <= 1
            "type": "limit",
            "limit_price": "10.00",
            "order_class": "bracket",
            "take_profit_limit_price": "20.00",
            "stop_loss_stop_price": "5.00",
            "asset_type": "OPTIONS",
            "lot_size": 1,
            "expiry": "2026-10-22",
            "time_in_force": "day",
            "market": "IN",
        }
        cfg = {
            "budget": 50000,
            "allow_options": True,
            "market": "IN",
        }
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("cannot authoritatively resolve contract lot size" in r for r in res["results"][0]["reasons"]))

    def test_fail_closed_derivative_refusal_on_missing_expiry(self):
        # Derivative order without expiry
        order = {
            "symbol": "NIFTY24000CE",
            "side": "buy",
            "qty": "25",
            "type": "limit",
            "limit_price": "120.00",
            "order_class": "bracket",
            "take_profit_limit_price": "168.00",
            "stop_loss_stop_price": "96.00",
            "asset_type": "OPTIONS",
            "lot_size": 25,
            "time_in_force": "day",
            "market": "IN",
        }
        cfg = {
            "budget": 50000,
            "allow_options": True,
            "market": "IN",
        }
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("missing contract expiry date" in r for r in res["results"][0]["reasons"]))

    def test_cli_size_trade_subcommand_compatibility(self):
        # Verify size.py accepts optional 'trade' argument from README examples
        plan = {
            "symbol": "RELIANCE",
            "side": "buy",
            "entry": 2850.00,
            "stop": 2800.00,
            "target": 2950.00,
            "market": "IN",
            "date": "2026-10-19",
            "setup": "orb"
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(plan, fh)
            plan_path = fh.name

        try:
            code, out, _ = run_cli(budget_mod, [
                "trade",  # positional action
                "--plan", plan_path,
                "--budget", "100000",
                "--market", "IN",
                "--now", "2026-10-19 10:15",
                "--json"
            ])
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertIn("orders", doc)
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["symbol"], "RELIANCE")
        finally:
            if os.path.exists(plan_path):
                os.unlink(plan_path)

    def test_cli_risk_check_positional_orders_argument(self):
        # Verify check.py accepts positional orders file argument: python3 check.py trader/orders.json
        orders = [{
            "symbol": "RELIANCE",
            "side": "buy",
            "qty": "10",
            "type": "limit",
            "limit_price": "2850.00",
            "order_class": "bracket",
            "take_profit_limit_price": "2950.00",
            "stop_loss_stop_price": "2800.00",
            "time_in_force": "day",
            "market": "IN",
        }]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(orders, fh)
            orders_path = fh.name

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({
                "budget": 100000,
                "max_order_inr": 50000,
                "max_loss_per_trade_inr": 1500,
                "max_daily_loss_inr": 3000,
                "market": "IN"
            }, fh)
            cfg_path = fh.name

        try:
            code, out, _ = run_cli(risk_mod, [
                orders_path,  # positional orders file
                "--config", cfg_path,
                "--market", "IN",
                "--now", "2026-10-19 10:15",
                "--no-write",
                "--json"
            ])
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(doc["block"], False)
            self.assertEqual(doc["results"][0]["verdict"], "PASS")
        finally:
            if os.path.exists(orders_path):
                os.unlink(orders_path)
            if os.path.exists(cfg_path):
                os.unlink(cfg_path)

    def test_cli_scan_daily_strategy_flag(self):
        # Verify scan.py daily accepts --strategy
        code, out, err = run_cli(scan_mod, [
            "daily",
            "--market", "IN",
            "--symbols", "RELIANCE,TCS,INFY",
            "--strategy", "breakout_20d",
            "--offline", os.path.join(FIX, "bharat"),
            "--no-write",
            "--json"
        ])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertIn("hits", doc)
        self.assertGreaterEqual(len(doc["hits"]), 1)
        self.assertEqual(doc["hits"][0]["symbol"], "INFY")


if __name__ == "__main__":
    unittest.main()
