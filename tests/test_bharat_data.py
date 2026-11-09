"""Unit tests for BharatTrader AI Indian market data infrastructure (Phase 2)."""

import datetime
import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load, FIX

data_mod = load("trader-alerts", "data_in")
symbols_mod = load("trader-alerts", "symbols_in")
alerts_mod = load("trader-alerts", "alerts")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")


class TestBharatData(unittest.TestCase):
    def setUp(self):
        self.offline_provider = data_mod.OfflineMarketDataProvider(BHARAT_FIXTURES)

    def test_provider_registry_and_factory(self):
        # 1. Default provider is Yahoo
        prov = data_mod.get_provider()
        self.assertIsInstance(prov, data_mod.YahooMarketDataProvider)

        # 2. Explicit offline provider
        prov_off = data_mod.get_provider("offline", base_path=BHARAT_FIXTURES)
        self.assertIsInstance(prov_off, data_mod.OfflineMarketDataProvider)

        # 3. Environment variable override
        old_env = os.environ.get("BHARAT_DATA_PROVIDER")
        try:
            os.environ["BHARAT_DATA_PROVIDER"] = "offline"
            prov_env = data_mod.get_provider(base_path=BHARAT_FIXTURES)
            self.assertIsInstance(prov_env, data_mod.OfflineMarketDataProvider)
        finally:
            if old_env is not None:
                os.environ["BHARAT_DATA_PROVIDER"] = old_env
            else:
                os.environ.pop("BHARAT_DATA_PROVIDER", None)

        # 4. Unknown provider raises DataError
        with self.assertRaises(data_mod.DataError):
            data_mod.get_provider("unknown_provider")

    def test_symbol_validation_and_exchange_formatting(self):
        # Valid canonical symbols
        self.assertEqual(symbols_mod.normalize_symbol("reliance"), "RELIANCE")
        self.assertEqual(symbols_mod.normalize_symbol("tcs"), "TCS")
        self.assertEqual(symbols_mod.normalize_symbol("infy"), "INFY")
        self.assertEqual(symbols_mod.normalize_symbol("m&m"), "M&M")
        self.assertEqual(symbols_mod.normalize_symbol("bajaj-auto"), "BAJAJ-AUTO")

        # Provider ticker mapping for NSE and BSE
        self.assertEqual(symbols_mod.to_yahoo_symbol("RELIANCE", exchange="NSE"), "RELIANCE.NS")
        self.assertEqual(symbols_mod.to_yahoo_symbol("TCS", exchange="NSE"), "TCS.NS")
        self.assertEqual(symbols_mod.to_yahoo_symbol("500325", exchange="BSE"), "500325.BO")
        self.assertEqual(symbols_mod.to_yahoo_symbol("RELIANCE", exchange="BSE"), "RELIANCE.BO")

        # Invalid symbols
        with self.assertRaises(symbols_mod.SymbolValidationError):
            symbols_mod.normalize_symbol("BAD$SYM")

    def test_quote_normalization_and_tier_labeling(self):
        # RELIANCE quote
        q_rel = self.offline_provider.get_quote("RELIANCE", exchange="NSE")
        self.assertEqual(q_rel["symbol"], "RELIANCE")
        self.assertEqual(q_rel["exchange"], "NSE")
        self.assertEqual(q_rel["currency"], "INR")
        self.assertEqual(q_rel["price"], 2855.40)
        self.assertEqual(q_rel["prev_close"], 2835.00)
        self.assertEqual(q_rel["change"], 20.40)
        self.assertEqual(q_rel["change_pct"], 0.72)
        self.assertEqual(q_rel["volume"], 4521000)
        self.assertEqual(q_rel["high"], 2865.00)
        self.assertEqual(q_rel["low"], 2830.00)
        self.assertIn("IST", q_rel["time"])

        # Data tier checks: Never label delayed data as real-time!
        self.assertEqual(q_rel["tier"], data_mod.DataTier.HISTORICAL)
        self.assertFalse(q_rel["is_real_time"])

        # TCS quote
        q_tcs = self.offline_provider.get_quote("TCS", exchange="NSE")
        self.assertEqual(q_tcs["symbol"], "TCS")
        self.assertEqual(q_tcs["price"], 4120.50)
        self.assertEqual(q_tcs["prev_close"], 4100.00)
        self.assertEqual(q_tcs["change"], 20.50)
        self.assertEqual(q_tcs["change_pct"], 0.50)
        self.assertFalse(q_tcs["is_real_time"])

        # Batch quotes
        batch = self.offline_provider.get_quotes(["RELIANCE", "TCS"], exchange="NSE")
        self.assertIn("RELIANCE", batch)
        self.assertIn("TCS", batch)
        self.assertEqual(batch["RELIANCE"]["price"], 2855.40)
        self.assertEqual(batch["TCS"]["price"], 4120.50)

    def test_intraday_bars_normalization_and_session_alignment(self):
        bars_data = self.offline_provider.get_intraday_bars(
            "RELIANCE", interval="5m", range_str="1d", exchange="NSE", now="2026-10-19 16:00:00"
        )
        self.assertEqual(bars_data["symbol"], "RELIANCE")
        self.assertEqual(bars_data["exchange"], "NSE")
        self.assertEqual(bars_data["interval"], "5m")
        self.assertEqual(bars_data["tier"], data_mod.DataTier.HISTORICAL)
        self.assertFalse(bars_data["is_real_time"])

        bars = bars_data["bars"]
        # Exactly 75 regular 5-minute bars in continuous session 09:15 to 15:30
        self.assertEqual(len(bars), 75)

        # First bar: 09:15 IST
        first_bar = bars[0]
        self.assertEqual(first_bar["slot"], 0)
        self.assertEqual(first_bar["start"], "09:15")
        self.assertEqual(first_bar["date"], "2026-10-19")
        self.assertGreater(first_bar["o"], 0)
        self.assertGreater(first_bar["c"], 0)

        # Last bar: 15:25 IST
        last_bar = bars[-1]
        self.assertEqual(last_bar["slot"], 74)
        self.assertEqual(last_bar["start"], "15:25")

        # Edge cases dropped
        dropped = bars_data["dropped"]
        self.assertEqual(dropped["off_grid"], 1)       # 09:17:03 tick dropped
        self.assertEqual(dropped["outside_hours"], 1)  # 08:50 tick dropped
        self.assertEqual(dropped["no_price"], 1)       # bar with None dropped

    def test_cas_v1_intraday_bars_alignment(self):
        # In Era 2 (CAS V1: 2026-08-03 to 2026-12-06), CAS-eligible securities continuous trading ends at 15:15 (72 bars)
        # Using the fixture bars, evaluated with is_cas_eligible=True on date 2026-10-19:
        bars_data = self.offline_provider.get_intraday_bars(
            "RELIANCE", interval="5m", exchange="NSE", now="2026-10-19 16:00:00", is_cas_eligible=True
        )
        bars = bars_data["bars"]
        self.assertEqual(len(bars), 72)
        self.assertEqual(bars[-1]["slot"], 71)
        self.assertEqual(bars[-1]["start"], "15:10")
        # 3 bars between 15:15 and 15:30 were dropped as outside continuous trading hours
        self.assertEqual(bars_data["dropped"]["outside_hours"], 4)

    def test_daily_bars_and_technical_indicators(self):
        daily_data = self.offline_provider.get_daily_bars(
            "INFY", range_str="1y", exchange="NSE", now="2026-10-20"
        )
        bars = daily_data["bars"]
        self.assertEqual(len(bars), 35)
        self.assertEqual(bars[-1]["c"], 1950.0)

        # Test adapter to scanner series
        series = data_mod.to_scanner_series(daily_data)
        self.assertEqual(len(series["t"]), 35)
        self.assertEqual(len(series["c"]), 35)
        self.assertEqual(series["meta"]["regularMarketPrice"], 1950.0)

        # Test RSI calculation
        closes = [b["c"] for b in bars]
        rsi = data_mod.calculate_rsi(closes)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 50.0)  # Strong upward breakout should have bullish RSI

        # Test MA20 calculation
        ma20 = data_mod.calculate_ma(closes, period=20)
        self.assertIsNotNone(ma20)
        self.assertLess(ma20, 1950.0)

        # Test setup detection: 35th bar broke out above 20-day high!
        setups, metrics = data_mod.detect_setups(bars)
        self.assertIn("breakout_20d", setups)
        self.assertEqual(metrics["high20"], max(closes[-21:-1]))

    def test_error_handling_and_exceptions(self):
        # 1. Missing offline file raises SymbolNotFoundError
        with self.assertRaises(data_mod.SymbolNotFoundError):
            self.offline_provider.get_quote("NONEXISTENT", exchange="NSE")

        # 2. Malformed JSON
        with self.assertRaises(data_mod.DataError):
            self.offline_provider.parser.normalize_quote("not json", "RELIANCE")

        # 3. Provider error in payload
        err_json = '{"chart":{"result":null,"error":{"code":"Not Found","description":"Symbol not found"}}}'
        with self.assertRaises(data_mod.SymbolNotFoundError):
            self.offline_provider.parser.normalize_quote(err_json, "RELIANCE")

        # 4. Empty result
        empty_json = '{"chart":{"result":[],"error":null}}'
        with self.assertRaises(data_mod.MissingDataError):
            self.offline_provider.parser.normalize_quote(empty_json, "RELIANCE")

    def test_mock_network_failures_and_retries(self):
        # Mock network layer to verify 429 rate limit backoff and 5xx fallback
        calls = []

        class MockProvider(data_mod.YahooMarketDataProvider):
            def _http_get(self, url):
                calls.append(url)
                if "query2" in url:
                    # Query2 fails with 429
                    fp = io.BytesIO(b"Rate limited")
                    raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, fp)
                # Query1 succeeds
                with open(os.path.join(BHARAT_FIXTURES, "RELIANCE-quote.json")) as f:
                    return 200, f.read()

        prov = MockProvider(pause=0.01, backoff=0.01)
        res = prov.get_quote("RELIANCE", exchange="NSE")
        self.assertEqual(res["symbol"], "RELIANCE")
        self.assertEqual(res["price"], 2855.40)
        self.assertEqual(res["tier"], data_mod.DataTier.DELAYED)
        self.assertFalse(res["is_real_time"])

        # Verified that query2 was tried first, then fell back to query1
        self.assertTrue(any("query2" in u for u in calls))
        self.assertTrue(any("query1" in u for u in calls))

    def test_mock_persistent_rate_limit(self):
        class ThrottlingProvider(data_mod.YahooMarketDataProvider):
            def _http_get(self, url):
                fp = io.BytesIO(b"Rate limited")
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, fp)

        prov = ThrottlingProvider(pause=0.01, backoff=0.01)
        with self.assertRaises(data_mod.RateLimitError):
            prov.get_quote("RELIANCE", exchange="NSE")

    def test_cli_quote_in(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts_mod.main([
                "quote", "--market", "IN", "RELIANCE", "TCS",
                "--offline", BHARAT_FIXTURES, "--json"
            ])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("RELIANCE", data["quotes"])
        self.assertIn("TCS", data["quotes"])
        self.assertEqual(data["quotes"]["RELIANCE"]["price"], 2855.40)
        self.assertEqual(data["quotes"]["RELIANCE"]["tier"], data_mod.DataTier.HISTORICAL)
        self.assertFalse(data["quotes"]["RELIANCE"]["is_real_time"])

    def test_cli_quote_human_readable_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts_mod.main([
                "quote", "--market", "IN", "RELIANCE",
                "--offline", BHARAT_FIXTURES
            ])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("RELIANCE", out)
        self.assertIn("\u20b92855.40", out)  # Rupee sign
        self.assertIn("HISTORICAL", out)
        self.assertIn("Never treat delayed quotes as real-time", out)

    def test_cli_history_in(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts_mod.main([
                "history", "RELIANCE", "--market", "IN",
                "--offline", os.path.join(BHARAT_FIXTURES, "RELIANCE-5m.json"),
                "--now", "2026-10-19 16:00:00",
                "--json"
            ])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["symbol"], "RELIANCE")
        self.assertEqual(len(data["bars"]), 75)
        self.assertEqual(data["tier"], data_mod.DataTier.HISTORICAL)


if __name__ == "__main__":
    unittest.main()

