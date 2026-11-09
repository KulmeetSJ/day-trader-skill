"""Unit tests for BharatTrader AI Indian stock market scanning (Phase 3)."""

import datetime
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

scan = load("trader-scanner", "scan")
symbols_mod = load("trader-alerts", "symbols_in")
calendar_mod = load("trader-alerts", "calendar_in")
instruments_mod = load("trader-alerts", "instruments")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")


def run_scan(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestBharatScan(unittest.TestCase):
    def test_market_selection(self):
        # 1. Default without args or config
        self.assertEqual(scan.get_market_profile(), "US")

        # 2. Command-line flag --market IN
        class MockArgs:
            def __init__(self, market):
                self.market = market

        self.assertEqual(scan.get_market_profile(MockArgs("IN")), "IN")
        self.assertEqual(scan.get_market_profile(MockArgs("US")), "US")

        # 3. Environment variable BHARAT_MARKET
        old_env = os.environ.get("BHARAT_MARKET")
        try:
            os.environ["BHARAT_MARKET"] = "IN"
            self.assertEqual(scan.get_market_profile(), "IN")
            os.environ["BHARAT_MARKET"] = "INDIA"
            self.assertEqual(scan.get_market_profile(), "IN")
        finally:
            if old_env is None:
                os.environ.pop("BHARAT_MARKET", None)
            else:
                os.environ["BHARAT_MARKET"] = old_env

        # 4. Config file setting
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump({"market": "IN"}, fh)
            cfg_path = fh.name
        try:
            class MockCfgArgs:
                def __init__(self, cfg):
                    self.config = cfg
                    self.market = None
            self.assertEqual(scan.get_market_profile(MockCfgArgs(cfg_path)), "IN")
        finally:
            if os.path.exists(cfg_path):
                os.remove(cfg_path)

    def test_build_universe_in(self):
        # 1. NIFTY 50 (50 prominent blue-chip equities)
        kept, counts, src = scan.build_universe_in(index="NIFTY50")
        self.assertEqual(len(kept), 50)
        self.assertEqual(counts["rows"], 50)
        self.assertEqual(counts["keep"], 50)
        self.assertIn("NIFTY 50", src)
        syms_50 = [s for s, _ in kept]
        self.assertIn("RELIANCE", syms_50)
        self.assertIn("TCS", syms_50)
        self.assertIn("INFY", syms_50)
        self.assertIn("M&M", syms_50)
        self.assertIn("BAJAJ-AUTO", syms_50)

        # 2. NIFTY 100 (50 NIFTY 50 + 50 NIFTY NEXT 50)
        kept100, counts100, src100 = scan.build_universe_in(index="NIFTY100")
        self.assertEqual(len(kept100), 100)
        self.assertEqual(counts100["rows"], 100)
        self.assertEqual(counts100["keep"], 100)
        self.assertIn("NIFTY 100", src100)
        syms_100 = [s for s, _ in kept100]
        self.assertIn("HAL", syms_100)
        self.assertIn("VEDL", syms_100)
        self.assertIn("PIDILITIND", syms_100)

        # 3. FNO eligible from cache
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            sample_master = {
                "metadata_version": "1.0",
                "effective_date": "2026-10-19",
                "instruments": {
                    "RELIANCE": {"canonical_symbol": "RELIANCE", "name": "Reliance", "is_fo_eligible": True},
                    "TCS": {"canonical_symbol": "TCS", "name": "Tata CS", "is_fo_eligible": True},
                    "PENNY": {"canonical_symbol": "PENNY", "name": "Penny Stock", "is_fo_eligible": False},
                }
            }
            json.dump(sample_master, fh)
            cache_file = fh.name
        try:
            kept_fno, counts_fno, src_fno = scan.build_universe_in(index="FNO", instruments_cache=cache_file)
            self.assertEqual(len(kept_fno), 2)
            self.assertEqual([s for s, _ in kept_fno], ["RELIANCE", "TCS"])
            self.assertEqual(counts_fno["keep"], 2)
        finally:
            if os.path.exists(cache_file):
                os.remove(cache_file)

        # 4. Unknown index raises ScanError
        with self.assertRaises(scan.ScanError):
            scan.build_universe_in(index="INVALID_INDEX")

    def test_symbol_translation(self):
        # US market behavior remains unchanged
        self.assertEqual(scan.to_yahoo("BRK.B", market="US"), "BRK-B")
        self.assertEqual(scan.from_yahoo("BRK-B", market="US"), "BRK.B")

        # Indian market behavior
        self.assertEqual(scan.to_yahoo("RELIANCE", market="IN"), "RELIANCE.NS")
        self.assertEqual(scan.to_yahoo("M&M", market="IN"), "M&M.NS")
        self.assertEqual(scan.to_yahoo("BAJAJ-AUTO", market="IN"), "BAJAJ-AUTO.NS")

        self.assertEqual(scan.from_yahoo("RELIANCE.NS", market="IN"), "RELIANCE")
        self.assertEqual(scan.from_yahoo("M&M.NS", market="IN"), "M&M")
        self.assertEqual(scan.from_yahoo("500325.BO", market="IN"), "500325")

        # Path construction
        spark = scan.spark_path(["RELIANCE", "TCS"], "3mo", "1d", market="IN")
        self.assertIn("symbols=RELIANCE.NS,TCS.NS", spark)
        chart = scan.chart_path("INFY", "1d", "5m", market="IN")
        self.assertIn("/v8/finance/chart/INFY.NS", chart)

    def test_trading_calendar_and_session(self):
        # Weekend is closed
        sat = datetime.date(2026, 10, 24)
        is_open, reason = scan.trading_day(sat, market="IN")
        self.assertFalse(is_open)
        self.assertEqual(reason, "weekend")

        # Official 2026 NSE Holiday: Republic Day 2026-01-26
        rep_day = datetime.date(2026, 1, 26)
        is_open, reason = scan.trading_day(rep_day, market="IN")
        self.assertFalse(is_open)
        self.assertIn("Republic Day", reason)

        # Regular weekday session: 09:15 to 15:30 IST
        trading_day = datetime.date(2026, 10, 19)
        is_open, _ = scan.trading_day(trading_day, market="IN")
        self.assertTrue(is_open)
        sess = scan.session(trading_day, market="IN")
        self.assertIsNotNone(sess)
        self.assertEqual(sess[0], datetime.time(9, 15))
        self.assertEqual(sess[1], datetime.time(15, 30))

        # Special Muhurat session on Diwali (2026-11-08)
        muhurat = datetime.date(2026, 11, 8)
        is_open, _ = scan.trading_day(muhurat, market="IN")
        self.assertTrue(is_open)
        sess_muhurat = scan.session(muhurat, market="IN")
        self.assertEqual(sess_muhurat[0], datetime.time(18, 15))
        self.assertEqual(sess_muhurat[1], datetime.time(19, 15))

    def test_money_formatting(self):
        # Indian market: Rupee symbol
        self.assertEqual(scan.money(2842.50, market="IN"), "\u20b92,842.50")
        self.assertEqual(scan.money(100.0, market="IN"), "\u20b9100.00")

        # US market: Dollar symbol
        self.assertEqual(scan.money(2842.50, market="US"), "$2,842.50")
        self.assertEqual(scan.money(100.0, market="US"), "$100.00")

    def test_offline_daily_scan_json_and_human(self):
        # Scan offline folder containing INFY-daily.json and TCS-daily.json
        # INFY-daily.json has a 20-day breakout
        # TCS-daily.json has an oversold setup (RSI < 30)
        code, text, err = run_scan([
            "daily", "--market", "IN",
            "--offline", BHARAT_FIXTURES,
            "--now", "2026-10-19 16:00",
            "--min-price", "0",
            "--min-volume", "0",
            "--no-write",
            "--json"
        ])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mode"], "daily")
        self.assertEqual(doc["market"], "IN")
        self.assertEqual(doc["scanned"], 2)
        hits = doc["hits"]
        self.assertGreaterEqual(len(hits), 2)
        hit_syms = [h["symbol"] for h in hits]
        self.assertIn("INFY", hit_syms)
        self.assertIn("TCS", hit_syms)

        # Check setups
        infy_hit = next(h for h in hits if h["symbol"] == "INFY")
        self.assertIn("breakout_20d", infy_hit["setups"])
        tcs_hit = next(h for h in hits if h["symbol"] == "TCS")
        self.assertIn("oversold", tcs_hit["setups"])

        # Check human-readable output
        code_h, text_h, _ = run_scan([
            "daily", "--market", "IN",
            "--offline", BHARAT_FIXTURES,
            "--now", "2026-10-19 16:00",
            "--min-price", "0",
            "--min-volume", "0",
            "--no-write"
        ])
        self.assertEqual(code_h, 0)
        self.assertIn("IST", text_h)
        self.assertIn("\u20b9", text_h)
        self.assertIn("INFY", text_h)
        self.assertIn("TCS", text_h)

    def test_offline_orb_scan_json_and_human(self):
        # Scan RELIANCE-5m.json offline at 10:35 IST
        # Opening range (09:15-09:30) high is 2848.0
        # Breakout occurs with close > 2848.0
        code, text, err = run_scan([
            "orb", "--market", "IN",
            "--offline", os.path.join(BHARAT_FIXTURES, "RELIANCE-5m.json"),
            "--now", "2026-10-19 11:00",
            "--at", "10:35",
            "--min-price", "0",
            "--min-volume", "0",
            "--no-write",
            "--json"
        ])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mode"], "orb")
        self.assertEqual(doc["market"], "IN")
        self.assertEqual(doc["scanned"], 1)
        self.assertEqual(len(doc["hits"]), 1)
        hit = doc["hits"][0]
        self.assertEqual(hit["symbol"], "RELIANCE")
        self.assertEqual(hit["setups"], ["orb"])
        self.assertEqual(hit["or_high"], 2848.0)
        self.assertGreater(hit["price"], 2848.0)

        # Human-readable output
        code_h, text_h, _ = run_scan([
            "orb", "--market", "IN",
            "--offline", os.path.join(BHARAT_FIXTURES, "RELIANCE-5m.json"),
            "--now", "2026-10-19 11:00",
            "--at", "10:35",
            "--min-price", "0",
            "--min-volume", "0",
            "--no-write"
        ])
        self.assertEqual(code_h, 0)
        self.assertIn("IST", text_h)
        self.assertIn("09:15 to", text_h)
        self.assertIn("RELIANCE", text_h)
        self.assertIn("\u20b9", text_h)

    def test_cli_universe_generation(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as fh:
            uni_path = fh.name

        try:
            code, text, _ = run_scan([
                "universe", "--market", "IN",
                "--index", "NIFTY50",
                "--out", uni_path
            ])
            self.assertEqual(code, 0)
            self.assertIn("50 securities", text)
            with open(uni_path, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            self.assertEqual(len(lines), 50)
            for ln in lines:
                parts = ln.split("|")
                self.assertEqual(len(parts), 2)
                self.assertTrue(symbols_mod.normalize_symbol(parts[0]))
        finally:
            if os.path.exists(uni_path):
                os.remove(uni_path)

    def test_watchlist_output_format(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            watch_path = fh.name

        try:
            code, _, _ = run_scan([
                "daily", "--market", "IN",
                "--offline", BHARAT_FIXTURES,
                "--now", "2026-10-19 16:00",
                "--min-price", "0",
                "--min-volume", "0",
                "--out", watch_path
            ])
            self.assertEqual(code, 0)
            with open(watch_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["created_by"], "trader-scanner")
            self.assertEqual(doc["mode"], "daily")
            self.assertEqual(doc["market"], "IN")
            self.assertIn("hits", doc)
            for h in doc["hits"]:
                self.assertIn("symbol", h)
                self.assertIn("price", h)
                self.assertIn("setups", h)
        finally:
            if os.path.exists(watch_path):
                os.remove(watch_path)


if __name__ == "__main__":
    unittest.main()
