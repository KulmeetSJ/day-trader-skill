"""Regression coverage for exit-management edge cases and plan reporting."""

import datetime
import unittest
from types import SimpleNamespace

import _load

exit_mod = _load.load("trader-exit", "exit")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def ist_utc(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(datetime.timezone.utc)


def bar_ts(y, m, d, hh, mm):
    return int((ist_utc(y, m, d, hh, mm) - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds())


class ExitFixesTests(unittest.TestCase):
    def test_intraday_square_off_fires_after_market_close(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 1, "avg_cost": 100, "trading_style": "intraday"},
            {"bars": [], "current_quote": {"price": 101.0, "timestamp": bar_ts(2026, 11, 13, 10, 0)},
             "provider": "offline", "data_tier": "FIXTURE"},
            ist_utc(2026, 11, 13, 15, 35), market="IN", simulated=True,
        )
        self.assertEqual((ev["action"], ev["reason_code"]), ("EXIT", "INTRADAY_SQUARE_OFF"))

    def test_prior_day_intraday_position_is_overdue(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 1, "avg_cost": 100, "entry_date": "2026-11-12",
             "trading_style": "intraday"},
            {"bars": [], "current_quote": {"price": 101.0, "timestamp": bar_ts(2026, 11, 13, 10, 0)},
             "provider": "offline", "data_tier": "FIXTURE"},
            ist_utc(2026, 11, 13, 10, 0), market="IN", simulated=True,
        )
        self.assertEqual((ev["action"], ev["reason_code"]), ("EXIT", "INTRADAY_CARRYOVER"))

    def test_plan_date_is_propagated_to_cli_position(self):
        args = SimpleNamespace(symbol="RELIANCE", qty=None, entry=None, stop_price=None,
                               trailing_stop=None, target_price=None, trading_style=None,
                               asset_type=None, expiry=None, exchange=None, entry_date=None)
        rows = exit_mod._collect_open_positions(args, {"trading_style": "intraday"},
                                                plan_doc={"symbol": "RELIANCE", "date": "2026-11-12"})
        self.assertEqual(rows[0]["entry_date"], "2026-11-12")

    def test_trend_check_compares_iso_bar_date_to_session_date(self):
        bars = []
        for i in range(30):
            hh, mm = 9, 15 + i * 5
            hh += mm // 60
            mm %= 60
            px = 200 - i
            bars.append({"t": bar_ts(2026, 11, 13, hh, mm), "o": px + 0.2, "h": px + 0.3,
                         "l": px - 0.3, "c": px, "v": 1000})
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 1, "avg_cost": 210, "trading_style": "positional"},
            {"bars": bars, "provider": "offline", "data_tier": "FIXTURE"},
            ist_utc(2026, 11, 13, 12, 0), market="IN", simulated=True,
        )
        self.assertIn(ev["reason_code"], ("TREND_REVERSAL", "VWAP_BREAKDOWN"))

    def test_spot_index_exit_does_not_suggest_equity_flatten_ticket(self):
        lines = exit_mod.describe_management({
            "symbol": "NIFTY", "market": "IN", "asset_type": "INDEX", "action": "EXIT",
            "reason_code": "INTRADAY_SQUARE_OFF", "current_price": 22000,
            "active_stop": None, "unrealized_pnl": None, "r_multiple": None,
            "trading_style": "intraday", "reasons": [],
        })
        text = "\n".join(lines)
        self.assertIn("analytical only", text)
        self.assertNotIn("ticket.py flatten", text)

    def test_indian_spot_index_is_auto_detected_during_management(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "NIFTY", "shares": 1, "avg_cost": 22000, "trading_style": "intraday"},
            {"bars": [], "provider": "offline", "data_tier": "FIXTURE"},
            ist_utc(2026, 11, 13, 15, 35), market="IN", simulated=True,
        )
        self.assertEqual(ev["asset_type"], "INDEX")

    def test_plan_report_includes_recommendation_and_probability_sections(self):
        plan = {
            "symbol": "TSLA", "date": "2026-11-13", "setup": "orb", "interval": "5m",
            "market": "US", "entry": 100.0, "stop": 98.0, "target": 104.0,
            "risk_per_share": 2.0, "reward_risk": 2.0, "flat_by": "15:55",
            "stop_label": "opening range midpoint", "target_label": "2R",
        }
        report = "\n".join(exit_mod.describe(plan))
        self.assertIn("TRADE RECOMMENDATION", report)
        self.assertIn("PROBABILITY: NOT AVAILABLE \u2014 INSUFFICIENT VALIDATED DATA", report)

    def test_option_premium_reward_risk_fails_closed_without_validated_quotes(self):
        result = exit_mod.option_premium_analysis(
            {"premium": 100.03, "greeks": {"delta": 0.5}},
            underlying_entry=100, underlying_stop=90, underlying_target=120, market="IN",
        )
        self.assertFalse(result["available"])
        self.assertIn("OPTION RISK UNAVAILABLE", result["reason"])

    def test_portfolio_entry_date_comes_from_oldest_remaining_lot(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "portfolio.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"positions": [{"ticker": "RELIANCE", "shares": 7, "lots": [
                    {"date": "2026-11-12", "shares": 2, "cost": 100},
                    {"date": "2026-11-13", "shares": 5, "cost": 101},
                    {"date": "2026-11-10", "shares": 0, "cost": 99},
                ]}]}, fh)
            args = SimpleNamespace(symbol=None, portfolio=path)
            rows = exit_mod._collect_open_positions(args, {"trading_style": "intraday"})
            self.assertEqual(rows[0]["entry_date"], "2026-11-12")

    def test_carryover_on_nontrading_day_is_pending_without_price(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 1, "avg_cost": 100, "entry_date": "2026-11-12",
             "trading_style": "intraday"},
            {"bars": [], "provider": "offline", "data_tier": "FIXTURE"},
            ist_utc(2026, 11, 24, 10, 0), market="IN", simulated=True,
        )
        self.assertEqual(ev["action"], "EXIT_PENDING_NEXT_SESSION")
        self.assertIsNone(ev["current_price"])
        self.assertEqual(ev["price_source"], "unavailable")

    def test_carryover_exits_at_first_open_session_with_fresh_price(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 1, "avg_cost": 100, "entry_date": "2026-11-23",
             "trading_style": "intraday"},
            {"bars": [], "current_quote": {"price": 101.0, "timestamp": bar_ts(2026, 11, 25, 9, 20)},
             "provider": "offline", "data_tier": "FIXTURE"},
            ist_utc(2026, 11, 25, 9, 20), market="IN", simulated=True,
        )
        self.assertEqual((ev["action"], ev["reason_code"]), ("EXIT", "INTRADAY_CARRYOVER"))
        self.assertEqual(ev["current_price"], 101.0)

    def test_positional_position_is_not_marked_as_intraday_carryover(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 1, "avg_cost": 100, "entry_date": "2026-11-12",
             "trading_style": "positional"},
            {"bars": [], "provider": "offline", "data_tier": "FIXTURE"},
            ist_utc(2026, 11, 24, 10, 0), market="IN", simulated=True,
        )
        self.assertNotEqual(ev["action"], "EXIT_PENDING_NEXT_SESSION")
        self.assertNotEqual(ev["reason_code"], "INTRADAY_CARRYOVER")

    def test_prior_session_quote_after_close_cannot_drive_price_actions(self):
        position = {
            "symbol": "RELIANCE", "shares": 10, "avg_cost": 100, "stop": 95,
            "target": 110, "trading_style": "positional",
        }
        ev = exit_mod.evaluate_position(
            position,
            {"bars": [], "current_quote": {"price": 90.0, "timestamp": bar_ts(2026, 11, 12, 15, 25)},
             "provider": "offline", "data_tier": "CURRENT"},
            ist_utc(2026, 11, 13, 15, 35), market="IN", simulated=True,
        )
        self.assertEqual((ev["action"], ev["reason_code"]), ("NO_ACTION", "STALE_DATA"))
        self.assertIsNone(ev["current_price"])
        self.assertEqual(ev["data_tier"], "STALE")
        self.assertEqual(ev["price_source"], "unavailable")

    def test_fresh_current_session_quote_can_drive_stop_exit(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 10, "avg_cost": 100, "stop": 95,
             "trading_style": "positional"},
            {"bars": [], "current_quote": {"price": 90.0, "timestamp": bar_ts(2026, 11, 13, 15, 25)},
             "provider": "offline", "data_tier": "CURRENT"},
            ist_utc(2026, 11, 13, 15, 35), market="IN", simulated=True,
        )
        self.assertEqual((ev["action"], ev["reason_code"]), ("EXIT", "STOP_HIT"))
        self.assertEqual(ev["current_price"], 90.0)

    def test_untimestamped_quote_is_not_used_for_price_dependent_action(self):
        ev = exit_mod.evaluate_position(
            {"symbol": "RELIANCE", "shares": 10, "avg_cost": 100, "stop": 95,
             "trading_style": "positional"},
            {"bars": [], "current_quote": {"price": 90.0},
             "provider": "offline", "data_tier": "CURRENT"},
            ist_utc(2026, 11, 13, 12, 0), market="IN", simulated=True,
        )
        self.assertEqual((ev["action"], ev["reason_code"]), ("NO_ACTION", "STALE_DATA"))
        self.assertIsNone(ev["current_price"])


if __name__ == "__main__":
    unittest.main()
