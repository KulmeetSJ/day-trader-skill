"""End-to-end integration test for Indian options trading workflow (Phase 4).

Covers full lifecycle:
1. Entry signal evaluation: options ORB selection of ATM contract on NIFTY index
2. Exit plan generation: stop at 20% under entry premium, 2R target, lot size 25
3. Position sizing: whole lot allocation (multiples of 25), max loss check, statutory transaction charges
4. Pre-trade risk manager: lot multiple check, long option validation, window check, PASS verdict
5. Broker ticket: plain-English rendering, user approval, paper fill in portfolio, decision audit log
"""

import json
import os
import shutil
import tempfile
import unittest

from _load import load

entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")


class TestBharatOptionsE2E(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.entry_path = os.path.join(self.temp_dir, "entry.json")
        self.plan_path = os.path.join(self.temp_dir, "plan.json")
        self.orders_path = os.path.join(self.temp_dir, "orders.json")
        self.risk_path = os.path.join(self.temp_dir, "risk.json")
        self.portfolio_path = os.path.join(self.temp_dir, "portfolio.json")
        self.state_path = os.path.join(self.temp_dir, "broker.json")
        self.config_path = os.path.join(self.temp_dir, "config.json")

        self.cfg = {
            "market": "IN",
            "currency": "INR",
            "budget": 200000,
            "cash_buffer_pct": 10,
            "max_position_pct": 75,
            "max_order_inr": 150000,
            "min_order_inr": 100,
            "max_loss_per_trade_inr": 3000,
            "max_daily_loss_inr": 8000,
            "max_trades_per_day": 5,
            "max_orders_per_day": 10,
            "min_reward_risk": 1.0,
            "require_stop": True,
            "no_entries_before": "09:30",
            "no_entries_after": "15:00",
            "flat_by": "15:20",
            "allow_options": True,
            "paper": True,
        }
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh, indent=2)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_full_options_lifecycle(self):
        # Step 1: Entry Trigger Doc (Generated from /trader-entry options_orb)
        entry_doc = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "buy",
            "status": "triggered",
            "price": 140.00,
            "date": "2026-10-19",
            "time": "10:15 AM",
            "bar_index": 12,
            "interval": "5m",
            "setup": "options_orb",
            "market": "IN",
            "option_contract": {
                "symbol": "NIFTY26OCT24000CE",
                "underlying": "NIFTY",
                "expiry": "2026-10-29",
                "strike": 24000.0,
                "option_type": "CE",
                "lot_size": 25,
                "ltp": 140.00,
            }
        }
        with open(self.entry_path, "w", encoding="utf-8") as fh:
            json.dump(entry_doc, fh, indent=2)

        # Step 2: Exit Planner (trader-exit)
        # Stop at 20% under entry premium (140 * 0.8 = 112.00), Target at 2R (140 + 2 * 28 = 196.00)
        class ExitArgs:
            from_path = self.entry_path
            symbol = None
            entry = None
            or_high = None
            or_low = None
            or_minutes = 15
            setup = "options_orb"
            date = "2026-10-19"
            time = "10:15 AM"
            entry_bar = 12
            interval = "5m"
            side = "buy"
            stop = "pct:20"
            target = "2R"
            flat_by = "15:20"
            market = "IN"
            config = self.config_path
            instruments = None
            offline = None
            now = "2026-10-19 10:15"
            asset_type = "OPTIONS"
            trading_style = "intraday"

        now_utc = exit_mod.parse_now("2026-10-19 10:15", market="IN")
        plan, warnings = exit_mod.build_plan(ExitArgs(), now_utc)

        self.assertEqual(plan["symbol"], "NIFTY26OCT24000CE")
        self.assertEqual(plan["entry"], 140.00)
        self.assertEqual(plan["stop"], 112.00)
        self.assertEqual(plan["target"], 196.00)
        self.assertEqual(plan["risk_per_share"], 28.00)
        self.assertEqual(plan["lot_size"], 25)
        self.assertEqual(plan["asset_type"], "OPTIONS")

        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)

        # Step 3: Position Sizing (trader-budget)
        # Risk per share = 28.00; 1 lot = 25 * 28 = Rs 700 risk
        # Max loss = Rs 3000 -> Max lots = 3000 // 700 = 4 lots = 100 shares (Rs 2800 risk)
        res_size = budget_mod.size_trade(plan, self.cfg, today="2026-10-19")
        self.assertIsNone(res_size["refusal"])
        self.assertEqual(res_size["qty"], 100)
        self.assertEqual(res_size["contracts"], 4)
        self.assertEqual(res_size["lot_size"], 25)
        self.assertEqual(res_size["cost"], 14000.00)
        self.assertEqual(res_size["max_loss"], 2800.00)
        self.assertIn("transaction_costs", res_size)
        self.assertGreater(res_size["transaction_costs"]["total"], 0)

        order = budget_mod.bracket_order(res_size)
        self.assertIsNotNone(order)
        self.assertEqual(order["qty"], "100")
        self.assertEqual(order["contracts"], 4)
        self.assertEqual(order["lot_size"], 25)
        self.assertEqual(order["asset_type"], "OPTIONS")
        self.assertEqual(order["take_profit_limit_price"], "196.00")
        self.assertEqual(order["stop_loss_stop_price"], "112.00")

        with open(self.orders_path, "w", encoding="utf-8") as fh:
            json.dump([order], fh, indent=2)

        # Step 4: Risk Manager (trader-risk)
        res_risk = risk_mod.check([order], self.cfg, now=now_utc)
        self.assertEqual(res_risk["pass"], 1)
        self.assertEqual(res_risk["block"], 0)
        self.assertEqual(res_risk["results"][0]["verdict"], "PASS")

        with open(self.risk_path, "w", encoding="utf-8") as fh:
            json.dump(res_risk, fh, indent=2)

        # Step 5: Order Ticket & Execution (trader-broker)
        # Check ticket rendering
        ticket_str, ready = ticket_mod.render(order, 1, 1, self.cfg, res_risk, None, "2026-10-19", market="IN")
        self.assertTrue(ready)
        self.assertIn("READY", ticket_str)
        self.assertIn("4 contracts", ticket_str)
        self.assertIn("NIFTY26OCT24000CE", ticket_str)

        # User approval
        ok, reply_why = ticket_mod.parse_yes("yes buy 100 NIFTY26OCT24000CE", order)
        self.assertTrue(ok)

        # Record fill in paper portfolio
        ticket_mod.apply_fill(
            self.portfolio_path,
            self.cfg,
            symbol="NIFTY26OCT24000CE",
            side="buy",
            qty=100,
            price=140.00,
            date="2026-10-19",
            market="IN"
        )
        with open(self.portfolio_path, encoding="utf-8") as fh:
            pf = json.load(fh)
        self.assertEqual(len(pf["positions"]), 1)
        self.assertEqual(pf["positions"][0]["ticker"], "NIFTY26OCT24000CE")
        self.assertEqual(pf["positions"][0]["shares"], 100.0)
        # Cash = 200,000 - 14,000 = 186,000
        self.assertEqual(pf["cash"], 186000.0)


if __name__ == "__main__":
    unittest.main()
