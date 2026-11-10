"""Unit tests for Indian derivatives, options math, and Black-Scholes Greeks (Phase 4).

Covers:
- Derivative symbol parsing and formatting (Futures, Monthly/Weekly Options)
- Expiry calendar rules (monthly last Thursday, weekly expiries, holiday shifting)
- Black-Scholes analytic pricing, Put-Call parity, and Greeks (Delta, Gamma, Theta, Vega, Rho)
- Implied Volatility (IV) bisection solver
- OptionChain strike ladder, ATM/ITM/OTM selection, and PCR calculation
"""

import datetime
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _load import load

symbols_mod = load("trader-alerts", "symbols_in")
calendar_mod = load("trader-alerts", "calendar_in")
data_mod = load("trader-alerts", "data_in")
instruments_mod = load("trader-alerts", "instruments")


class TestBharatDerivatives(unittest.TestCase):
    def test_derivative_symbol_parsing(self):
        # Monthly Index Option
        p1 = symbols_mod.parse_derivative_symbol("NIFTY26OCT24000CE")
        self.assertIsNotNone(p1)
        self.assertEqual(p1["underlying"], "NIFTY")
        self.assertEqual(p1["instrument_type"], "OPT")
        self.assertEqual(p1["asset_type"], "OPTIONS")
        self.assertEqual(p1["strike"], 24000.0)
        self.assertEqual(p1["option_type"], "CE")
        self.assertEqual(p1["expiry_date"], "2026-10-27")

        # Monthly Index Put
        p2 = symbols_mod.parse_derivative_symbol("BANKNIFTY26OCT52000PE")
        self.assertIsNotNone(p2)
        self.assertEqual(p2["underlying"], "BANKNIFTY")
        self.assertEqual(p2["instrument_type"], "OPT")
        self.assertEqual(p2["strike"], 52000.0)
        self.assertEqual(p2["option_type"], "PE")

        # Stock Option
        p3 = symbols_mod.parse_derivative_symbol("RELIANCE26OCT1400CE")
        self.assertIsNotNone(p3)
        self.assertEqual(p3["underlying"], "RELIANCE")
        self.assertEqual(p3["strike"], 1400.0)
        self.assertEqual(p3["option_type"], "CE")

        # Futures
        p4 = symbols_mod.parse_derivative_symbol("NIFTY26OCTFUT")
        self.assertIsNotNone(p4)
        self.assertEqual(p4["underlying"], "NIFTY")
        self.assertEqual(p4["instrument_type"], "FUT")
        self.assertEqual(p4["asset_type"], "FUTURES")
        self.assertIsNone(p4["strike"])
        self.assertIsNone(p4["option_type"])

    def test_derivative_symbol_formatting(self):
        exp = datetime.date(2026, 10, 27)
        sym_ce = symbols_mod.format_derivative_symbol("NIFTY", exp, strike=24500, option_type="CE")
        self.assertEqual(sym_ce, "NIFTY26OCT24500CE")

        sym_fut = symbols_mod.format_derivative_symbol("BANKNIFTY", exp)
        self.assertEqual(sym_fut, "BANKNIFTY26OCTFUT")

    def test_monthly_expiry_calendar_and_holiday_shift(self):
        # October 2026: Last Tuesday is 2026-10-27 (regular trading day)
        exp_oct = calendar_mod.get_monthly_expiry(2026, 10, exchange="NSE")
        self.assertEqual(exp_oct, datetime.date(2026, 10, 27))

        # Check if next expiry lands on an active trading day
        next_exp = calendar_mod.get_next_expiry(datetime.date(2026, 10, 19), exchange="NSE")
        self.assertGreaterEqual(next_exp, datetime.date(2026, 10, 19))
        self.assertTrue(calendar_mod.is_trading_day_in(next_exp)[0])

    def test_black_scholes_pricing_and_greeks(self):
        S = 24000.0
        K = 24000.0
        T = 30.0 / 365.0
        r = 0.07
        sigma = 0.15

        res_ce = data_mod.black_scholes(S, K, T, r, sigma, option_type="CE")
        res_pe = data_mod.black_scholes(S, K, T, r, sigma, option_type="PE")
        call_price = res_ce["price"]
        put_price = res_pe["price"]
        delta_call = res_ce["delta"]
        delta_put = res_pe["delta"]
        gamma = res_ce["gamma"]
        theta_call = res_ce["theta"]
        vega = res_ce["vega"]

        # Call & Put prices must be positive
        self.assertGreater(call_price, 0)
        self.assertGreater(put_price, 0)

        # Put-Call Parity: C - P = S - K * exp(-r * T)
        parity_diff = abs((call_price - put_price) - (S - K * math.exp(-r * T)))
        self.assertLess(parity_diff, 0.01)

        # ATM Call Delta ~ 0.5 (slightly > 0.5 with r > 0)
        self.assertTrue(0.48 < delta_call < 0.60)
        # Put Delta = Call Delta - 1
        self.assertAlmostEqual(delta_put, delta_call - 1.0, places=4)

        # Gamma must be positive for both
        self.assertGreater(gamma, 0)

        # Theta must be negative for long options (decay)
        self.assertLess(theta_call, 0)

        # Vega must be positive
        self.assertGreater(vega, 0)

    def test_implied_volatility_solver(self):
        S = 24000.0
        K = 24000.0
        T = 20.0 / 365.0
        r = 0.07
        known_sigma = 0.18

        price = data_mod.black_scholes(S, K, T, r, known_sigma, option_type="CE")["price"]
        solved_iv = data_mod.calculate_iv(price, S, K, T, r, is_call=True)
        self.assertAlmostEqual(solved_iv, known_sigma, places=3)

    def test_option_chain_generation_and_selection(self):
        spot = 24235.0
        chain = data_mod.fetch_option_chain("NIFTY", spot_price=spot)
        self.assertEqual(chain.underlying, "NIFTY")
        self.assertEqual(chain.data_tier, data_mod.DataTier.SIMULATED)

        # NIFTY strike interval is 50
        self.assertEqual(chain.atm_strike, 24250.0)
        self.assertIn(24250.0, chain.sorted_strikes)

        # Strike selection
        atm_strike = chain.select_strike(strategy="ATM", option_type="CE")
        self.assertEqual(atm_strike, 24250.0)
        atm_contract = chain.get_contract(atm_strike, option_type="CE")
        self.assertIsNotNone(atm_contract)
        self.assertGreater(atm_contract["last_price"], 0)
        self.assertIn("greeks", atm_contract)
        self.assertIn("open_interest", atm_contract)

        itm_strike = chain.select_strike(strategy="ITM1", option_type="CE")
        self.assertEqual(itm_strike, 24200.0)
        itm_contract = chain.get_contract(itm_strike, option_type="CE")
        self.assertGreater(itm_contract["greeks"]["delta"], atm_contract["greeks"]["delta"])

        otm_strike = chain.select_strike(strategy="OTM1", option_type="CE")
        self.assertEqual(otm_strike, 24300.0)
        otm_contract = chain.get_contract(otm_strike, option_type="CE")
        self.assertLess(otm_contract["greeks"]["delta"], atm_contract["greeks"]["delta"])

        # PCR must be positive
        self.assertGreater(chain.pcr, 0.1)


if __name__ == "__main__":
    unittest.main()
