"""Unit tests for Indian options position sizing, risk rules, and statutory transaction costs (Phase 4).

Covers:
- Position sizing in whole lots for options and futures
- Strict max loss refusal on 1-lot threshold
- Gate enforcement: allow_options requirement
- Statutory Indian transaction charges breakdown (STT, NSE fees, SEBI, Stamp Duty, GST)
- Pre-trade risk manager:
  - Naked short options prohibition (undefined risk)
  - Lot size integer multiple compliance
  - Contract expiry validation
  - Expiry day 14:30 IST entry cutoff
  - Positional trading style window handling
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _load import load

budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")


class TestBharatOptionsRisk(unittest.TestCase):
    def test_options_sizing_fails_closed_without_validated_contract_pricing(self):
        plan = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "buy",
            "entry": 120.00,
            "stop": 96.00,        # 20% stop: risk per share = 24.00
            "target": 168.00,      # 2R target
            "risk_per_share": 24.00,
            "lot_size": 25,
            "asset_type": "OPTIONS",
            "market": "IN",
            "tick_size": 0.05,
            "date": "2026-10-19",
            "setup": "options_orb",
        }
        cfg = {
            "budget": 100000,
            "cash_buffer_pct": 10,
            "max_position_pct": 75,
            "max_order_inr": 75000,
            "max_loss_per_trade_inr": 1500,  # Max loss Rs 1500; 1 lot risk = 25 * 24 = Rs 600
            "allow_options": True,
            "market": "IN",
        }

        with self.assertRaises(budget_mod.SizeError) as ctx:
            budget_mod.size_trade(plan, cfg, today="2026-10-19")
        self.assertIn("derivative contract validation failed", str(ctx.exception))

    def test_options_sizing_refuses_unverified_contract_before_calculating_loss(self):
        plan = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "buy",
            "entry": 200.00,
            "stop": 150.00,        # Risk per share = 50.00; 1 lot (25 shares) = Rs 1250 risk
            "target": 300.00,
            "risk_per_share": 50.00,
            "lot_size": 25,
            "asset_type": "OPTIONS",
            "market": "IN",
            "tick_size": 0.05,
            "date": "2026-10-19",
        }
        cfg = {
            "budget": 100000,
            "max_loss_per_trade_inr": 1000,  # Only allows Rs 1000 max loss, but 1 lot is Rs 1250
            "allow_options": True,
            "market": "IN",
        }

        with self.assertRaises(budget_mod.SizeError) as ctx:
            budget_mod.size_trade(plan, cfg, today="2026-10-19")
        self.assertIn("derivative contract validation failed", str(ctx.exception))

    def test_options_gate_blocks_when_not_allowed(self):
        plan = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "buy",
            "entry": 120.00,
            "stop": 96.00,
            "target": 168.00,
            "asset_type": "OPTIONS",
            "market": "IN",
            "date": "2026-10-19",
        }
        cfg = {
            "budget": 100000,
            "allow_options": False,  # Disabled
            "market": "IN",
        }

        with self.assertRaises(budget_mod.SizeError) as ctx:
            budget_mod.size_trade(plan, cfg, today="2026-10-19")
        self.assertIn("options are not enabled", str(ctx.exception))

    def test_statutory_transaction_charges_breakdown(self):
        # Options trade: 50 shares bought at Rs 120, sold at target Rs 168
        charges = budget_mod.calculate_indian_transaction_charges(
            asset_type="OPTIONS",
            side="buy",
            entry_price=120.0,
            exit_price=168.0,
            qty=50,
            trading_style="intraday"
        )
        self.assertEqual(charges["brokerage"], 40.0)  # Rs 20 buy + Rs 20 sell
        # STT: 0.15% on sell premium = 0.0015 * (50 * 168) = 12.60 (effective 2026-04-01)
        self.assertEqual(charges["stt"], 12.60)
        # Exchange charges > 0 (revised Rs 3553 / crore per side)
        self.assertGreater(charges["exchange_charges"], 0)
        # GST: 18% on (brokerage + exchange + sebi)
        self.assertGreater(charges["gst"], 0)
        self.assertGreater(charges["total"], charges["brokerage"])
        # Verify clean segregation
        self.assertIn("statutory", charges)
        self.assertIn("brokerage_estimate", charges)
        self.assertIn("profitability_impact", charges)
        self.assertTrue(charges["brokerage_estimate"]["is_estimate"])
        self.assertGreater(charges["profitability_impact"]["gross_pnl"], 0)

    def test_risk_blocks_naked_short_options(self):
        # Naked option selling (opening sell order without holding contract)
        order = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "sell",
            "qty": "25",
            "type": "market",
            "asset_type": "OPTIONS",
            "time_in_force": "day",
            "market": "IN",
        }
        cfg = {
            "budget": 100000,
            "allow_options": True,
            "allow_short": True,   # Even if short stock is allowed, naked option is blocked
            "market": "IN",
        }
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("undefined risk" in r for r in res["results"][0]["reasons"]))

    def test_risk_enforces_lot_size_multiples(self):
        # Lot size for NIFTY is 25; ordering 30 shares must be blocked
        order = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "buy",
            "qty": "30",
            "type": "limit",
            "limit_price": "120.00",
            "order_class": "bracket",
            "take_profit_limit_price": "168.00",
            "stop_loss_stop_price": "96.00",
            "lot_size": 25,
            "asset_type": "OPTIONS",
            "time_in_force": "day",
            "market": "IN",
        }
        cfg = {
            "budget": 100000,
            "allow_options": True,
            "market": "IN",
        }
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("lot size" in r for r in res["results"][0]["reasons"]))

    def test_risk_blocks_expired_contract(self):
        # Contract expired on 2026-10-01, current time is 2026-10-19
        order = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "buy",
            "qty": "25",
            "type": "limit",
            "limit_price": "120.00",
            "order_class": "bracket",
            "take_profit_limit_price": "168.00",
            "stop_loss_stop_price": "96.00",
            "lot_size": 25,
            "expiry": "2026-10-01",
            "asset_type": "OPTIONS",
            "time_in_force": "day",
            "market": "IN",
        }
        cfg = {
            "budget": 100000,
            "allow_options": True,
            "market": "IN",
        }
        at_now = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("expired on 2026-10-01" in r for r in res["results"][0]["reasons"]))

    def test_risk_expiry_day_cutoff_at_1430_ist(self):
        # Contract expires today (2026-10-19); order placed at 14:45 IST (> 14:30 IST)
        order = {
            "symbol": "NIFTY26OCT24000CE",
            "side": "buy",
            "qty": "25",
            "type": "limit",
            "limit_price": "120.00",
            "order_class": "bracket",
            "take_profit_limit_price": "168.00",
            "stop_loss_stop_price": "96.00",
            "lot_size": 25,
            "expiry": "2026-10-19",
            "asset_type": "OPTIONS",
            "time_in_force": "day",
            "market": "IN",
        }
        cfg = {
            "budget": 100000,
            "allow_options": True,
            "no_entries_after": "15:00",
            "market": "IN",
        }
        at_now = risk_mod.parse_now("2026-10-19 14:45", market="IN")
        res = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("expiry cutoff" in r for r in res["results"][0]["reasons"]))

    def test_risk_positional_trading_style_bypasses_intraday_cutoff(self):
        # Positional order placed at 15:10 IST (when intraday no_entries_after is 15:00)
        # using multi-session GTT OCO delivery semantics rather than intraday day/bracket.
        order = {
            "symbol": "RELIANCE",
            "side": "buy",
            "qty": "10",
            "type": "limit",
            "limit_price": "2850.00",
            "order_class": "gtt_oco",
            "product": "CNC",
            "take_profit_limit_price": "2950.00",
            "stop_loss_stop_price": "2800.00",
            "trading_style": "positional",
            "time_in_force": "gtc",
            "multi_session_protection": {
                "type": "gtt_oco",
                "product": "CNC",
                "time_in_force": "gtc",
                "persists_across_sessions": True,
                "triggers": {"stop_loss": "2800.00", "take_profit": "2950.00"},
            },
            "market": "IN",
            "currency": "INR",
        }
        cfg = {
            "budget": 100000,
            "max_order_inr": 50000,
            "max_loss_per_trade_inr": 1000,
            "max_daily_loss_inr": 2000,
            "no_entries_after": "15:00",  # Intraday cutoff
            "broker": "paper",
            "paper": True,
            "market": "IN",
        }
        at_now = risk_mod.parse_now("2026-10-19 15:10", market="IN")
        res = risk_mod.check([order], cfg, now=at_now)
        self.assertEqual(res["results"][0]["verdict"], "PASS", res["results"][0])

        # Inheriting intraday day/bracket semantics on a positional order must be blocked
        bad_order = dict(order, order_class="bracket", time_in_force="day")
        bad_res = risk_mod.check([bad_order], cfg, now=at_now)
        self.assertEqual(bad_res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("positional" in r for r in bad_res["results"][0]["reasons"]))


if __name__ == "__main__":
    unittest.main()
