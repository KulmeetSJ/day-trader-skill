"""End-to-end fail-closed checks for option execution without validated pricing."""

import json
import os
import shutil
import tempfile
import unittest

from _load import load

exit_mod = load("trader-exit", "exit")
budget_mod = load("trader-budget", "size")
instruments_mod = load("trader-alerts", "instruments")


class TestBharatOptionsE2E(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self._inst_orig = []
        missing = os.path.join(self.temp_dir, "missing_instruments.json")
        for module in {instruments_mod, getattr(exit_mod, "instruments", None), getattr(budget_mod, "instruments", None)}:
            if module is not None and hasattr(module, "DEFAULT_CACHE_PATH"):
                self._inst_orig.append((module, module.DEFAULT_CACHE_PATH))
                module.DEFAULT_CACHE_PATH = missing

    def tearDown(self):
        for module, original in self._inst_orig:
            module.DEFAULT_CACHE_PATH = original
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unverified_option_plan_is_rejected(self):
        entry_path = os.path.join(self.temp_dir, "entry.json")
        with open(entry_path, "w", encoding="utf-8") as fh:
            json.dump({
                "symbol": "NIFTY26OCT24000CE", "side": "buy", "status": "triggered",
                "price": 140.0, "date": "2026-10-19", "setup": "options_orb", "market": "IN",
                "asset_type": "OPTIONS", "option_contract": {
                    "symbol": "NIFTY26OCT24000CE", "underlying": "NIFTY", "expiry": "2026-10-29",
                    "strike": 24000, "option_type": "CE", "lot_size": 25, "premium": 140,
                },
            }, fh)

        class Args:
            from_path = entry_path
            symbol = None
            entry = None
            or_high = or_low = None
            or_minutes = 15
            setup = "options_orb"
            date = "2026-10-19"
            time = None
            entry_bar = None
            interval = "5m"
            side = "buy"
            stop = "pct:20"
            target = "2R"
            flat_by = "15:20"
            market = "IN"
            config = None
            instruments = None
            offline = None
            now = "2026-10-19 10:15"
            asset_type = "OPTIONS"
            trading_style = "intraday"

        with self.assertRaisesRegex(exit_mod.PlanError, "derivative contract validation failed"):
            exit_mod.build_plan(Args(), exit_mod.parse_now(Args.now, market="IN"))

    def test_option_sizing_is_blocked_without_premium_risk(self):
        with self.assertRaises(budget_mod.SizeError):
            budget_mod.size_trade(
                {"symbol": "NIFTY26NOV25000CE", "side": "buy", "asset_type": "OPTIONS",
                 "market": "IN", "entry": 100, "stop": 80, "target": 140,
                 "date": "2026-11-13", "option_premium_analysis": {"available": False}},
                {"market": "IN", "budget": 100000, "allow_options": True}, today="2026-11-13",
            )


if __name__ == "__main__":
    unittest.main()
