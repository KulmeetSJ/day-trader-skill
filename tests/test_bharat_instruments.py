"""Unit tests for BharatTrader AI canonical symbols, instrument master cache, and tick resolution."""

import json
import os
import shutil
import sys
import tempfile
import unittest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load, FIX

sym_mod = load("trader-alerts", "symbols_in")
inst_mod = load("trader-alerts", "instruments")

SAMPLE_EQUITY_L = os.path.join(FIX, "bharat", "EQUITY_L_sample.csv")
SAMPLE_FO_LOTS = os.path.join(FIX, "bharat", "fo_mktlots_sample.csv")


class TestBharatSymbols(unittest.TestCase):
    def test_canonical_normalization(self):
        self.assertEqual(sym_mod.normalize_symbol("RELIANCE"), "RELIANCE")
        self.assertEqual(sym_mod.normalize_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(sym_mod.normalize_symbol("TCS.BO"), "TCS")
        self.assertEqual(sym_mod.normalize_symbol("m&m"), "M&M")
        self.assertEqual(sym_mod.normalize_symbol("BAJAJ-AUTO-EQ"), "BAJAJ-AUTO")
        self.assertEqual(sym_mod.normalize_symbol("NSE:INFY"), "INFY")
        self.assertEqual(sym_mod.normalize_symbol("BSE:HDFCBANK"), "HDFCBANK")

    def test_invalid_symbol_rejection(self):
        for bad in ("", "   ", "RELIANCE$%", "TCS.INVALID_EXT_TOO_LONG_FOR_REGEX", None, 12345):
            with self.assertRaises(sym_mod.SymbolValidationError):
                sym_mod.normalize_symbol(bad)

    def test_isin_validation(self):
        self.assertTrue(sym_mod.validate_isin("INE002A01018"))
        self.assertTrue(sym_mod.validate_isin("INE467B01029"))
        self.assertFalse(sym_mod.validate_isin("US0378331005"))  # US ISIN
        self.assertFalse(sym_mod.validate_isin("INVALID"))
        self.assertFalse(sym_mod.validate_isin(""))

    def test_yahoo_translation_isolation(self):
        self.assertEqual(sym_mod.to_yahoo_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(sym_mod.to_yahoo_symbol("TCS", exchange="BSE"), "TCS.BO")

        canonical, exch = sym_mod.from_yahoo_symbol("M&M.NS")
        self.assertEqual(canonical, "M&M")
        self.assertEqual(exch, "NSE")

        canonical_bse, exch_bse = sym_mod.from_yahoo_symbol("500325.BO")
        self.assertEqual(canonical_bse, "500325")
        self.assertEqual(exch_bse, "BSE")


class TestBharatInstruments(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.test_dir, "instruments.json")
        self.history_dir = os.path.join(self.test_dir, "history")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_parse_sample_fixtures_and_build_master(self):
        eq_records = inst_mod.parse_equity_l_csv(SAMPLE_EQUITY_L)
        self.assertIn("RELIANCE", eq_records)
        self.assertIn("M&M", eq_records)
        self.assertIn("IDEA", eq_records)

        # IDEA is Series BE (Trade-to-Trade)
        self.assertTrue(eq_records["IDEA"]["is_trade_to_trade"])
        # RELIANCE is Series EQ
        self.assertFalse(eq_records["RELIANCE"]["is_trade_to_trade"])

        fo_lots = inst_mod.parse_fo_lots_csv(SAMPLE_FO_LOTS)
        self.assertEqual(fo_lots["RELIANCE"], 250)
        self.assertEqual(fo_lots["TCS"], 175)

        master = inst_mod.build_instrument_master(
            eq_records,
            fo_lots=fo_lots,
            effective_date="2026-10-19",
            source_timestamp="2026-10-19T08:00:00Z"
        )
        self.assertEqual(master["effective_date"], "2026-10-19")
        self.assertIn("RELIANCE", master["instruments"])
        self.assertTrue(master["instruments"]["RELIANCE"]["is_fo_eligible"])
        self.assertEqual(master["instruments"]["RELIANCE"]["fo_lot_size"], 250)
        # CAS eligibility for F&O scrips on NSE
        self.assertTrue(master["instruments"]["RELIANCE"]["is_cas_eligible"])
        self.assertTrue(inst_mod.is_cas_eligible("RELIANCE", master))
        # Non-F&O scrip is not CAS-eligible
        self.assertFalse(master["instruments"]["IDEA"]["is_cas_eligible"])
        self.assertFalse(inst_mod.is_cas_eligible("IDEA", master))

    def test_atomic_cache_save_and_load(self):
        eq_records = inst_mod.parse_equity_l_csv(SAMPLE_EQUITY_L)
        master = inst_mod.build_instrument_master(eq_records, effective_date="2026-10-19")

        inst_mod.save_instrument_cache(master, cache_path=self.cache_file, history_dir=self.history_dir)
        self.assertTrue(os.path.isfile(self.cache_file))

        # Check history snapshot creation
        hist_file = os.path.join(self.history_dir, "instruments_20261019.json")
        self.assertTrue(os.path.isfile(hist_file))

        # Load fresh cache as of same date
        loaded = inst_mod.load_instrument_cache(self.cache_file, max_age_days=1, as_of_date="2026-10-19")
        self.assertEqual(loaded["effective_date"], "2026-10-19")
        self.assertIn("RELIANCE", loaded["instruments"])

    def test_fail_closed_on_stale_cache(self):
        eq_records = inst_mod.parse_equity_l_csv(SAMPLE_EQUITY_L)
        master = inst_mod.build_instrument_master(eq_records, effective_date="2026-10-10")
        inst_mod.save_instrument_cache(master, cache_path=self.cache_file, history_dir=None)

        # 9 days later (> 1 day max age) must raise StaleMetadataError
        with self.assertRaises(inst_mod.StaleMetadataError):
            inst_mod.load_instrument_cache(self.cache_file, max_age_days=1, as_of_date="2026-10-19")

    def test_fail_closed_on_missing_file_or_corrupt_json(self):
        with self.assertRaises(FileNotFoundError):
            inst_mod.load_instrument_cache("/nonexistent/instruments.json")

        with open(self.cache_file, "w", encoding="utf-8") as fh:
            fh.write("corrupted { json [")
        with self.assertRaises(inst_mod.InstrumentMetadataError):
            inst_mod.load_instrument_cache(self.cache_file)

    def test_resolve_instrument_and_fail_closed_rules(self):
        eq_records = inst_mod.parse_equity_l_csv(SAMPLE_EQUITY_L)
        fo_lots = inst_mod.parse_fo_lots_csv(SAMPLE_FO_LOTS)
        master = inst_mod.build_instrument_master(eq_records, fo_lots=fo_lots, effective_date="2026-10-19")

        # 1. Authoritative resolution of active stock
        rel = inst_mod.resolve_instrument("reliance.ns", master, require_intraday=True)
        self.assertEqual(rel["canonical_symbol"], "RELIANCE")
        self.assertEqual(rel["tick_size"], 0.05)
        self.assertEqual(rel["lot_size"], 1)
        self.assertEqual(rel["fo_lot_size"], 250)

        # 2. Trade-to-Trade exclusion for intraday trading (IDEA is Series BE)
        with self.assertRaises(inst_mod.InstrumentMetadataError) as ctx:
            inst_mod.resolve_instrument("IDEA", master, require_intraday=True)
        self.assertIn("Trade-to-Trade", str(ctx.exception))

        # Can resolve IDEA when intraday flag is False (e.g. delivery trade)
        idea = inst_mod.resolve_instrument("IDEA", master, require_intraday=False)
        self.assertEqual(idea["canonical_symbol"], "IDEA")

        # 3. Fail closed on unknown/unlisted symbol
        with self.assertRaises(inst_mod.InstrumentMetadataError):
            inst_mod.resolve_instrument("UNLISTED", master)

        # 4. Fail closed if tick size is missing or invalid
        bad_master = json.loads(json.dumps(master))
        bad_master["instruments"]["RELIANCE"]["tick_size"] = None
        with self.assertRaises(inst_mod.TickResolutionError):
            inst_mod.resolve_instrument("RELIANCE", bad_master)

        # 5. Fail closed if lot size is missing or 0
        bad_master["instruments"]["RELIANCE"]["tick_size"] = 0.05
        bad_master["instruments"]["RELIANCE"]["lot_size"] = 0
        with self.assertRaises(inst_mod.LotSizeResolutionError):
            inst_mod.resolve_instrument("RELIANCE", bad_master)


if __name__ == "__main__":
    unittest.main()
