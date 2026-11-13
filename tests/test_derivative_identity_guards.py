"""Regression tests for caller-metadata and spot-index derivative bypasses."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load

symbols_in = _load.load("trader-alerts", "symbols_in")
instruments = _load.load("trader-alerts", "instruments")
ticket = _load.load("trader-broker", "ticket")
budget = _load.load("trader-budget", "size")
exit_mod = _load.load("trader-exit", "exit")
entry = _load.load("trader-entry", "entry")
risk = _load.load("trader-risk", "check")


def contract_cache(symbol):
    parsed = symbols_in.parse_derivative_symbol(symbol)
    exchange = "BSE" if parsed["underlying"] in ("SENSEX", "BANKEX") else "NSE"
    record = {
        "canonical_symbol": symbol,
        "symbol": symbol,
        "trading_symbol": symbol,
        "underlying": parsed["underlying"],
        "exchange": exchange,
        "expiry": parsed["expiry_date"],
        "strike": parsed["strike"],
        "option_type": parsed["option_type"],
        "instrument_type": parsed["option_type"] or "FUT",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "tick_size_source": "exchange_master",
        "lot_size": 25,
    }
    return {
        "effective_date": "2026-11-13",
        "instruments": {symbol: record},
        "instruments_by_exchange": {exchange: {symbol: record}},
    }


class DerivativeIdentityGuardTests(unittest.TestCase):
    def test_exact_listed_call_and_put_metadata_is_accepted(self):
        for symbol in ("NIFTY26NOV25000CE", "NIFTY26NOV25000PE"):
            meta = instruments.validate_listed_derivative_contract(
                symbol, cache_payload=contract_cache(symbol), exchange="NSE"
            )
            self.assertEqual(meta["canonical_symbol"], symbol)
            self.assertEqual(meta["option_type"], symbol[-2:])
            self.assertEqual(meta["exchange"], "NSE")

    def test_exact_listed_index_future_is_accepted(self):
        symbol = "NIFTY26NOVFUT"
        meta = instruments.validate_listed_derivative_contract(
            symbol, cache_payload=contract_cache(symbol), exchange="NSE"
        )
        self.assertEqual(meta["canonical_symbol"], symbol)
        self.assertEqual(meta["instrument_type"], "FUT")

    def test_listed_banknifty_and_sensex_options_are_accepted(self):
        for symbol, exchange in (("BANKNIFTY26NOV50000PE", "NSE"), ("SENSEX26N1982200CE", "BSE")):
            meta = instruments.validate_listed_derivative_contract(
                symbol, cache_payload=contract_cache(symbol), exchange=exchange
            )
            self.assertEqual(meta["canonical_symbol"], symbol)
            self.assertEqual(meta["exchange"], exchange)

    def test_missing_or_mismatched_exact_contract_metadata_is_rejected(self):
        symbol = "NIFTY26NOV25000CE"
        cache = contract_cache(symbol)
        cache["instruments_by_exchange"]["NSE"][symbol]["underlying"] = "BANKNIFTY"
        with self.assertRaises(instruments.InstrumentMetadataError):
            instruments.validate_listed_derivative_contract(symbol, cache_payload=cache)
        with self.assertRaises(instruments.InstrumentMetadataError):
            instruments.validate_listed_derivative_contract(symbol, cache_payload=contract_cache("NIFTY26NOV25000PE"))

    def test_spot_indices_rejected_even_when_caller_says_options(self):
        for symbol in ("NIFTY", "BANKNIFTY", "SENSEX"):
            with self.assertRaises(ticket.TicketError):
                ticket._assert_tradable_instrument(symbol, {"asset_type": "OPTIONS"})
            with self.assertRaises(budget.SizeError):
                budget.size_trade(
                    {"symbol": symbol, "asset_type": "OPTIONS", "market": "IN", "entry": 100,
                     "stop": 90, "target": 120, "date": "2026-11-13"},
                    {"market": "IN", "budget": 100000, "allow_options": True},
                    today="2026-11-13",
                )
            result = risk.check(
                [{"symbol": symbol, "side": "buy", "qty": "1", "type": "market",
                  "time_in_force": "day", "asset_type": "OPTIONS", "market": "IN"}],
                {"market": "IN", "allow_options": True, "paper": True, "budget": 100000},
                now=risk.parse_now("2026-11-13 10:15", market="IN"),
            )
            self.assertEqual(result["results"][0]["verdict"], "BLOCK")
            self.assertTrue(any("spot index" in reason.lower() for reason in result["results"][0]["reasons"]))

    def test_unsupported_option_strategy_is_explicit(self):
        result = {"market": "IN"}
        entry._attach_options_orb_contract(
            result, {"symbol": "NIFTY"}, None, None, None, "NSE",
            option_strategy="SELL CE",
        )
        self.assertTrue(result["options_unavailable"])
        self.assertEqual(result["option_contract_sub_state"], "UNSUPPORTED_STRATEGY")
        self.assertIn("BUY PE, SELL CE and SELL PE are unsupported", result["option_contract_unavailable_reason"])

    def test_exit_description_uses_spot_identity_over_options_label(self):
        text = "\n".join(exit_mod.describe_management({
            "symbol": "NIFTY", "market": "IN", "asset_type": "OPTIONS", "action": "EXIT",
            "reason_code": "INTRADAY_SQUARE_OFF", "current_price": 22000,
            "active_stop": None, "unrealized_pnl": None, "r_multiple": None,
            "trading_style": "intraday", "reasons": [],
        }))
        self.assertIn("analytical only", text)
        self.assertNotIn("ticket.py flatten", text)


if __name__ == "__main__":
    unittest.main()
