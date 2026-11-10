"""Unit tests for configurable Indian market universes and constituents (Phase 4).

Covers:
- NIFTY 50, NIFTY 100, NIFTY 200, NIFTY 500 constituents
- Sectoral and thematic indices: BANKNIFTY, FINNIFTY, SENSEX, BSE 100
- F&O universe and cash market equities
- Custom watchlists and config-driven symbol extraction
- Index normalization and BSE equity parsing
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

from _load import load

scan_mod = load("trader-scanner", "scan")
symbols_mod = load("trader-alerts", "symbols_in")
instruments_mod = load("trader-alerts", "instruments")


def run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestBharatUniverse(unittest.TestCase):
    def test_nifty_indices_constituents(self):
        u50_kept, _, _ = scan_mod.build_universe_in("NIFTY50")
        u50 = [s for s, _ in u50_kept]
        self.assertEqual(len(u50), 50)
        self.assertIn("RELIANCE", u50)
        self.assertIn("TCS", u50)
        self.assertIn("INFY", u50)
        self.assertIn("HDFCBANK", u50)

        u100_kept, _, _ = scan_mod.build_universe_in("NIFTY100")
        u100 = [s for s, _ in u100_kept]
        self.assertEqual(len(u100), 100)
        self.assertTrue(set(u50).issubset(set(u100)))

        u200_kept, _, _ = scan_mod.build_universe_in("NIFTY200")
        u200 = [s for s, _ in u200_kept]
        self.assertGreaterEqual(len(u200), 150)
        self.assertIn("LICI", u200)

        u500_kept, _, _ = scan_mod.build_universe_in("NIFTY500")
        u500 = [s for s, _ in u500_kept]
        self.assertGreaterEqual(len(u500), 150)

    def test_banking_and_financial_indices(self):
        bank_kept, _, _ = scan_mod.build_universe_in("BANKNIFTY")
        bank = [s for s, _ in bank_kept]
        self.assertEqual(len(bank), 12)
        self.assertIn("HDFCBANK", bank)
        self.assertIn("ICICIBANK", bank)
        self.assertIn("SBIN", bank)
        self.assertIn("KOTAKBANK", bank)

        fin_kept, _, _ = scan_mod.build_universe_in("FINNIFTY")
        fin = [s for s, _ in fin_kept]
        self.assertGreaterEqual(len(fin), 15)
        self.assertIn("HDFCBANK", fin)
        self.assertIn("BAJFINANCE", fin)
        self.assertIn("SBILIFE", fin)

    def test_bse_indices(self):
        sensex_kept, _, _ = scan_mod.build_universe_in("SENSEX")
        sensex = [s for s, _ in sensex_kept]
        self.assertEqual(len(sensex), 30)
        self.assertIn("RELIANCE", sensex)
        self.assertIn("TCS", sensex)
        self.assertIn("INFY", sensex)

        bse100_kept, _, _ = scan_mod.build_universe_in("BSE100")
        bse100 = [s for s, _ in bse100_kept]
        self.assertEqual(len(bse100), 100)

    def test_fno_universe(self):
        fno_kept, _, _ = scan_mod.build_universe_in("FNO")
        fno = [s for s, _ in fno_kept]
        self.assertGreaterEqual(len(fno), 100)
        self.assertIn("RELIANCE", fno)
        self.assertIn("INFY", fno)
        self.assertIn("TCS", fno)

    def test_custom_watchlist_from_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({
                "watchlist": ["TATAMOTORS", "MARUTI", "M&M"],
                "market": "IN"
            }, fh)
            cfg_path = fh.name

        try:
            custom_kept, _, _ = scan_mod.build_universe_in("CUSTOM", config_path=cfg_path)
            custom = [s for s, _ in custom_kept]
            self.assertEqual(custom, ["M&M", "MARUTI", "TATAMOTORS"])
        finally:
            if os.path.exists(cfg_path):
                os.unlink(cfg_path)

    def test_bse_csv_parsing(self):
        bse_csv_content = (
            "Security Code,Security Id,Security Name,Status,Group,Face Value,ISIN No,Industry,Instrument\n"
            "500325,RELIANCE,RELIANCE INDUSTRIES LTD.,Active,A,10,INE002A01018,Refineries,Equity\n"
            "532540,TCS,TATA CONSULTANCY SERVICES LTD.,Active,A,1,INE467B01029,IT Services,Equity\n"
            "500209,INFY,INFOSYS LTD.,Active,A,5,INE009A01021,IT Services,Equity\n"
            "999999,SUSP,SUSPENDED LTD.,Suspended,Z,10,INE999A01010,Other,Equity\n"
        )
        parsed = instruments_mod.parse_bse_equity_csv(bse_csv_content)
        self.assertIn("RELIANCE", parsed)
        self.assertIn("TCS", parsed)
        self.assertIn("INFY", parsed)
        self.assertNotIn("SUSP", parsed)  # Suspended skipped
        self.assertEqual(parsed["RELIANCE"]["exchange"], "BSE")
        self.assertEqual(parsed["RELIANCE"]["bse_scrip_code"], "500325")

    def test_index_normalization(self):
        self.assertEqual(symbols_mod.normalize_index_name("nifty"), "NIFTY")
        self.assertEqual(symbols_mod.normalize_index_name("NIFTY 50"), "NIFTY")
        self.assertEqual(symbols_mod.normalize_index_name("banknifty"), "BANKNIFTY")
        self.assertEqual(symbols_mod.normalize_index_name("NIFTY BANK"), "BANKNIFTY")
        self.assertEqual(symbols_mod.normalize_index_name("sensex"), "SENSEX")
        self.assertTrue(symbols_mod.is_index_symbol("^NSEI"))
        self.assertTrue(symbols_mod.is_index_symbol("NIFTY"))
        self.assertTrue(symbols_mod.is_index_symbol("BANKNIFTY"))
        self.assertFalse(symbols_mod.is_index_symbol("RELIANCE"))

    def test_universe_cli_command(self):
        with tempfile.TemporaryDirectory() as d:
            out_file = os.path.join(d, "universe.txt")
            code, out, _ = run_cli(scan_mod, [
                "universe",
                "--market", "IN",
                "--index", "BANKNIFTY",
                "--out", out_file
            ])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(out_file))
            with open(out_file) as fh:
                symbols = [line.strip().split("|")[0] for line in fh if line.strip()]
            self.assertEqual(len(symbols), 12)
            self.assertIn("HDFCBANK", symbols)


if __name__ == "__main__":
    unittest.main()
