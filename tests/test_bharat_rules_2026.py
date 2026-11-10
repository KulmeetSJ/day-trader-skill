"""Unit tests for November 2026 Indian market rules, regulatory schedules, and expiries.

Verifies:
1. NSE NIFTY expiry (Tuesday)
2. NSE BANKNIFTY expiry (Tuesday)
3. NSE FINNIFTY expiry (Tuesday)
4. NSE MIDCPNIFTY expiry (Tuesday)
5. NSE stock derivative expiry (Tuesday)
6. Holiday-adjusted expiry (roll backward to previous trading day)
7. BSE independent expiry rules (SENSEX Friday, BANKEX Monday)
8. STT calculations for equity intraday, equity delivery, futures, options, and exercised options
9. NSE transaction charges across cash, futures, and options segments (March 2026 revision)
10. Brokerage separation and charges impact on trade profitability
"""

import datetime
import os
import sys
import unittest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load

calendar_mod = load("trader-alerts", "calendar_in")
symbols_mod = load("trader-alerts", "symbols_in")
instruments_mod = load("trader-alerts", "instruments")
budget_mod = load("trader-budget", "size")


class TestBharatRules2026(unittest.TestCase):
    # 1. NSE NIFTY Expiry (Tuesday)
    def test_nse_nifty_expiry_tuesday(self):
        weekday = calendar_mod.get_fallback_expiry_weekday(exchange="NSE", underlying="NIFTY")
        self.assertEqual(weekday, 1)  # Tuesday

        # Normal non-holiday week: Wednesday 2026-10-21 -> Next expiry Tuesday 2026-10-27
        next_exp = calendar_mod.get_next_expiry(as_of_date="2026-10-21", underlying="NIFTY", exchange="NSE")
        self.assertEqual(next_exp.weekday(), 1)
        self.assertEqual(next_exp.isoformat(), "2026-10-27")

        # Monthly expiry for October 2026
        monthly_exp = calendar_mod.get_monthly_expiry(2026, 10, exchange="NSE", underlying="NIFTY")
        self.assertEqual(monthly_exp.weekday(), 1)
        self.assertEqual(monthly_exp.isoformat(), "2026-10-27")

    # 2. NSE BANKNIFTY Expiry (Tuesday)
    def test_nse_banknifty_expiry_tuesday(self):
        weekday = calendar_mod.get_fallback_expiry_weekday(exchange="NSE", underlying="BANKNIFTY")
        self.assertEqual(weekday, 1)  # Tuesday

        next_exp = calendar_mod.get_next_expiry(as_of_date="2026-10-21", underlying="BANKNIFTY", exchange="NSE")
        self.assertEqual(next_exp.weekday(), 1)
        self.assertEqual(next_exp.isoformat(), "2026-10-27")

        parsed = symbols_mod.parse_derivative_symbol("BANKNIFTY26OCT52000PE")
        self.assertEqual(parsed["expiry_date"], "2026-10-27")

    # 3. NSE FINNIFTY Expiry (Tuesday)
    def test_nse_finnifty_expiry_tuesday(self):
        weekday = calendar_mod.get_fallback_expiry_weekday(exchange="NSE", underlying="FINNIFTY")
        self.assertEqual(weekday, 1)  # Tuesday

        next_exp = calendar_mod.get_next_expiry(as_of_date="2026-10-21", underlying="FINNIFTY", exchange="NSE")
        self.assertEqual(next_exp.weekday(), 1)
        self.assertEqual(next_exp.isoformat(), "2026-10-27")

    # 4. NSE MIDCPNIFTY Expiry (Tuesday)
    def test_nse_midcpnifty_expiry_tuesday(self):
        weekday = calendar_mod.get_fallback_expiry_weekday(exchange="NSE", underlying="MIDCPNIFTY")
        self.assertEqual(weekday, 1)  # Tuesday

        next_exp = calendar_mod.get_next_expiry(as_of_date="2026-10-21", underlying="MIDCPNIFTY", exchange="NSE")
        self.assertEqual(next_exp.weekday(), 1)
        self.assertEqual(next_exp.isoformat(), "2026-10-27")

    # 5. NSE Stock Derivative Expiry (Tuesday)
    def test_nse_stock_derivative_expiry_tuesday(self):
        weekday_rel = calendar_mod.get_fallback_expiry_weekday(exchange="NSE", underlying="RELIANCE")
        weekday_infy = calendar_mod.get_fallback_expiry_weekday(exchange="NSE", underlying="INFY")
        self.assertEqual(weekday_rel, 1)   # Tuesday
        self.assertEqual(weekday_infy, 1)  # Tuesday

        cache = {"instruments": {"RELIANCE": {"fo_lot_size": 250}}}
        contract = instruments_mod.resolve_derivative_contract("RELIANCE26OCT1400CE", cache_payload=cache)
        self.assertEqual(contract["expiry"], "2026-10-27")
        self.assertEqual(contract["expiry_source"], "exchange_fallback_rule")
        self.assertEqual(contract["lot_size"], 250)

    # 6. Holiday-Adjusted Expiry (Rolls backward to previous trading day)
    def test_holiday_adjusted_expiry_roll_backward(self):
        # Tuesday 2026-10-20 is Dussehra (Exchange Holiday)
        # Contracts expiring on this week must roll back to Monday 2026-10-19
        ref_monday = "2026-10-19"
        exp_holiday = calendar_mod.get_next_expiry(as_of_date=ref_monday, underlying="NIFTY", exchange="NSE")
        self.assertEqual(exp_holiday.isoformat(), "2026-10-19")
        self.assertEqual(exp_holiday.weekday(), 0)  # Monday

        # Verify that 2026-10-20 is indeed marked as a non-trading holiday
        is_trading, status, reason = calendar_mod.is_trading_day_in(datetime.date(2026, 10, 20), exchange="NSE")
        self.assertFalse(is_trading)
        self.assertIn("Dussehra", reason)

    # 7. BSE Independent Expiry Rules (SENSEX Friday, BANKEX Monday)
    def test_bse_expiry_rules_independent(self):
        # SENSEX: Friday (weekday = 4)
        sensex_weekday = calendar_mod.get_fallback_expiry_weekday(exchange="BSE", underlying="SENSEX")
        self.assertEqual(sensex_weekday, 4)
        sensex_exp = calendar_mod.get_next_expiry(as_of_date="2026-10-19", underlying="SENSEX", exchange="BSE")
        self.assertEqual(sensex_exp.weekday(), 4)  # Friday
        self.assertEqual(sensex_exp.isoformat(), "2026-10-23")

        # BANKEX: Monday (weekday = 0)
        bankex_weekday = calendar_mod.get_fallback_expiry_weekday(exchange="BSE", underlying="BANKEX")
        self.assertEqual(bankex_weekday, 0)
        bankex_exp = calendar_mod.get_next_expiry(as_of_date="2026-10-21", underlying="BANKEX", exchange="BSE")
        self.assertEqual(bankex_exp.weekday(), 0)  # Monday
        self.assertEqual(bankex_exp.isoformat(), "2026-10-26")

        # General BSE fallback is Friday
        gen_bse = calendar_mod.get_fallback_expiry_weekday(exchange="BSE", underlying="UNKNOWN_BSE")
        self.assertEqual(gen_bse, 4)

    # 8. STT Calculations Across All Segments (Effective 1 April 2026)
    def test_stt_calculations_all_segments(self):
        # A. Equity Intraday: 0.025% on sell leg
        c_eq_intra = budget_mod.calculate_indian_transaction_charges(
            asset_type="EQUITY", entry_price=1000.0, exit_price=1000.0, qty=100, trading_style="intraday"
        )
        # Sell turnover = 100,000; STT = 0.00025 * 100,000 = 25.00
        self.assertEqual(c_eq_intra["stt"], 25.00)

        # B. Equity Delivery: 0.1% on buy and sell legs
        c_eq_deliv = budget_mod.calculate_indian_transaction_charges(
            asset_type="EQUITY", entry_price=1000.0, exit_price=1100.0, qty=100, trading_style="positional"
        )
        # Buy turnover = 100,000 (STT 100.00), Sell turnover = 110,000 (STT 110.00); Total STT = 210.00
        self.assertEqual(c_eq_deliv["stt"], 210.00)

        # C. Futures: 0.05% on sell turnover (revised from 0.02%)
        c_fut = budget_mod.calculate_indian_transaction_charges(
            asset_type="FUTURES", entry_price=5000.0, exit_price=5200.0, qty=100, trading_style="intraday"
        )
        # Sell turnover = 520,000; STT = 0.0005 * 520,000 = 260.00
        self.assertEqual(c_fut["stt"], 260.00)

        # D. Options (regular): 0.15% on sell premium (revised from 0.1%)
        c_opt = budget_mod.calculate_indian_transaction_charges(
            asset_type="OPTIONS", entry_price=100.0, exit_price=150.0, qty=100, trading_style="intraday"
        )
        # Sell premium = 15,000; STT = 0.0015 * 15,000 = 22.50
        self.assertEqual(c_opt["stt"], 22.50)

        # E. Exercised Options: 0.15% on intrinsic value
        # Call option exercised: Spot 24,200, Strike 24,000, Qty 50 -> Intrinsic value = 200 * 50 = 10,000
        c_opt_exercised = budget_mod.calculate_indian_transaction_charges(
            asset_type="OPTIONS", entry_price=100.0, exit_price=200.0, qty=50,
            exercised=True, spot_price=24200.0, strike=24000.0, option_type="CE"
        )
        # STT = 0.0015 * 10,000 = 15.00
        self.assertEqual(c_opt_exercised["stt"], 15.00)

        # Exercised Options with direct intrinsic value parameter
        c_opt_iv_direct = budget_mod.calculate_indian_transaction_charges(
            asset_type="OPTIONS", entry_price=50.0, exit_price=250.0, qty=100,
            exercised=True, intrinsic_value=250.0
        )
        # Total intrinsic value = 250 * 100 = 25,000; STT = 0.0015 * 25,000 = 37.50
        self.assertEqual(c_opt_iv_direct["stt"], 37.50)

    # 9. NSE Transaction Charges (Revision effective 1 March 2026 incl. IPFT)
    def test_nse_transaction_charges_segments(self):
        # A. Cash: Rs 307 per crore per side (0.0000307 of turnover)
        c_cash = budget_mod.calculate_indian_transaction_charges(
            asset_type="EQUITY", entry_price=500.0, exit_price=500.0, qty=1000, trading_style="intraday"
        )
        # Turnover = 1,000,000; Exch fee = 0.0000307 * 1,000,000 = 30.70
        self.assertEqual(c_cash["exchange_charges"], 30.70)

        # B. Equity Futures: Rs 183 per crore per side (0.0000183 of turnover)
        c_fut = budget_mod.calculate_indian_transaction_charges(
            asset_type="FUTURES", entry_price=2500.0, exit_price=2500.0, qty=2000, trading_style="intraday"
        )
        # Turnover = 10,000,000; Exch fee = 0.0000183 * 10,000,000 = 183.00
        self.assertEqual(c_fut["exchange_charges"], 183.00)

        # C. Equity Options: Rs 3,553 per crore of premium per side (0.0003553 of premium turnover)
        c_opt = budget_mod.calculate_indian_transaction_charges(
            asset_type="OPTIONS", entry_price=50.0, exit_price=50.0, qty=1000, trading_style="intraday"
        )
        # Premium turnover = 100,000; Exch fee = 0.0003553 * 100,000 = 35.53
        self.assertEqual(c_opt["exchange_charges"], 35.53)

    # 10. Brokerage Separation and Trade Profitability Impact
    def test_brokerage_separation_and_trade_profitability(self):
        # Trade: 100 shares bought at Rs 500, sold at Rs 505 (Gross profit Rs 500)
        c = budget_mod.calculate_indian_transaction_charges(
            asset_type="EQUITY", entry_price=500.0, exit_price=505.0, qty=100, trading_style="intraday"
        )

        # 1. Clear separation between statutory charges and estimated brokerage
        self.assertIn("statutory", c)
        self.assertIn("brokerage_estimate", c)
        self.assertIn("profitability_impact", c)

        brokerage_info = c["brokerage_estimate"]
        self.assertTrue(brokerage_info["is_estimate"])
        self.assertIn("not an official exchange charge", brokerage_info["disclaimer"].lower())

        statutory_info = c["statutory"]
        self.assertEqual(statutory_info["exchange"], "NSE")
        self.assertGreater(statutory_info["total"], 0)

        # Total charges = statutory total + brokerage total
        expected_total = round(statutory_info["total"] + brokerage_info["total"], 2)
        self.assertEqual(c["total"], expected_total)

        # 2. Effect on trade profitability
        pnl_info = c["profitability_impact"]
        # Gross P&L: (505 - 500) * 100 = 500.00
        self.assertEqual(pnl_info["gross_pnl"], 500.00)
        # Net P&L: Gross P&L - Total Charges
        expected_net_pnl = round(500.00 - c["total"], 2)
        self.assertEqual(pnl_info["net_pnl"], expected_net_pnl)
        self.assertLess(pnl_info["net_pnl"], pnl_info["gross_pnl"])

        # Breakeven price must be greater than entry price
        self.assertGreater(pnl_info["breakeven_exit_price"], 500.00)

        # Demonstrate small profit turned to loss by transaction costs
        c_small = budget_mod.calculate_indian_transaction_charges(
            asset_type="EQUITY", entry_price=500.0, exit_price=500.20, qty=100, trading_style="intraday"
        )
        # Gross profit = Rs 20, but charges > Rs 20 -> Net P&L is negative
        self.assertEqual(c_small["profitability_impact"]["gross_pnl"], 20.00)
        self.assertLess(c_small["profitability_impact"]["net_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
