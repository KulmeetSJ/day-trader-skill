"""Regression tests for QA-discovered defects across entry, exit, scanner, risk, budget, broker, alerts, and data providers."""

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import FIX, load

data_mod = load("trader-alerts", "data_in")
inst_mod = load("trader-alerts", "instruments")
alerts_mod = load("trader-alerts", "alerts")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
scan_mod = load("trader-scanner", "scan")
size_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")


def _run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestQARegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qa_reg_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Bugs 1 & 2: entry.read_config, entry.cmd_watchlist, exit.read_config ----

    def test_entry_and_exit_read_config_preserve_provider_and_style_keys(self):
        cfg_path = os.path.join(self.tmp, "config.json")
        cfg_doc = {
            "market": "IN",
            "currency": "INR",
            "exchange": "BSE",
            "india_market_data_provider": "offline",
            "offline_dir": BHARAT_FIXTURES,
            "trading_style": "positional",
            "allow_yahoo_fallback": True,
            "provider_credentials": {"upstox": {"analytics_token": "tok_dummy"}},
        }
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg_doc, fh)

        ecfg = entry_mod.read_config(cfg_path, market="IN")
        self.assertEqual(ecfg["india_market_data_provider"], "offline")
        self.assertEqual(ecfg["exchange"], "BSE")
        self.assertEqual(ecfg["offline_dir"], BHARAT_FIXTURES)
        self.assertEqual(ecfg["trading_style"], "positional")
        self.assertTrue(ecfg["allow_yahoo_fallback"])
        self.assertIn("upstox", ecfg["provider_credentials"])

        xcfg = exit_mod.read_config(cfg_path, market="IN")
        self.assertEqual(xcfg["india_market_data_provider"], "offline")
        self.assertEqual(xcfg["exchange"], "BSE")
        self.assertEqual(xcfg["offline_dir"], BHARAT_FIXTURES)
        self.assertEqual(xcfg["trading_style"], "positional")

    def test_entry_cmd_watchlist_infers_in_market_before_loading_defaults(self):
        wl_path = os.path.join(self.tmp, "watchlist.json")
        with open(wl_path, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN", "hits": [{"symbol": "RELIANCE"}]}, fh)
        cfg_path = os.path.join(self.tmp, "missing_config.json")

        rc, out, err = _run_cli(
            entry_mod,
            [
                "watchlist",
                "--watchlist", wl_path,
                "--config", cfg_path,
                "--offline", BHARAT_FIXTURES,
                "--now", "2026-10-19 10:35",
                "--json",
            ],
        )
        self.assertEqual(rc, 0, msg=err)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        res = rows[0]["result"]
        self.assertEqual(res["market"], "IN")
        # 2026-10-19 10:35 IST must use IN defaults rather than US ET defaults
        self.assertEqual(res["status"], "triggered")

    # Bug 3: scan.py universe respects config universe/index/exchange ----------

    def test_scan_universe_respects_config_index_and_cli_override(self):
        cfg_path = os.path.join(self.tmp, "scan_cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN", "universe": "BANKNIFTY", "exchange": "NSE"}, fh)

        out_path = os.path.join(self.tmp, "universe.txt")
        rc, out, err = _run_cli(
            scan_mod,
            ["universe", "--market", "IN", "--config", cfg_path, "--out", out_path, "--json"],
        )
        self.assertEqual(rc, 0, msg=err)
        doc = json.loads(out)
        self.assertEqual(doc["index"], "BANKNIFTY")
        self.assertEqual(doc["exchange"], "NSE")

        # Explicit --index flag overrides config value
        rc2, out2, _ = _run_cli(
            scan_mod,
            ["universe", "--market", "IN", "--config", cfg_path, "--index", "NIFTY50", "--no-write", "--json"],
        )
        self.assertEqual(rc2, 0)
        self.assertEqual(json.loads(out2)["index"], "NIFTY50")

    # Bugs 4 & 5: check.py T2T --instruments wiring & positional orders_pos ---

    def test_risk_check_uses_instruments_flag_for_t2t_and_records_positional_orders_path(self):
        inst_path = os.path.join(self.tmp, "instruments.json")
        with open(inst_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "effective_date": "2026-10-19",
                    "instruments": {
                        "T2TSTOCK": {
                            "symbol": "T2TSTOCK",
                            "exchange": "NSE",
                            "series": "BE",
                            "is_trade_to_trade": True,
                            "tick_size": 0.05,
                            "tick_size_unit": "INR",
                            "lot_size": 1,
                        }
                    },
                },
                fh,
            )

        orders_path = os.path.join(self.tmp, "custom_orders.json")
        orders_doc = {
            "market": "IN",
            "currency": "INR",
            "orders": [
                {
                    "symbol": "T2TSTOCK",
                    "side": "buy",
                    "qty": "10",
                    "type": "limit",
                    "limit_price": "100.00",
                    "order_class": "bracket",
                    "time_in_force": "day",
                    "product_type": "MIS",
                    "take_profit_limit_price": "110.00",
                    "stop_loss_stop_price": "95.00",
                    "market": "IN",
                    "currency": "INR",
                }
            ],
        }
        with open(orders_path, "w", encoding="utf-8") as fh:
            json.dump(orders_doc, fh)

        cfg_path = os.path.join(self.tmp, "in_cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "market": "IN",
                    "currency": "INR",
                    "budget": 100000,
                    "max_order_inr": 50000,
                    "max_loss_per_trade_inr": 2000,
                    "max_daily_loss_inr": 5000,
                    "paper": True,
                },
                fh,
            )

        risk_out = os.path.join(self.tmp, "risk.json")
        rc, out, err = _run_cli(
            risk_mod,
            [
                orders_path,
                "--config", cfg_path,
                "--instruments", inst_path,
                "--out", risk_out,
                "--now", "2026-10-19 10:15",
                "--json",
            ],
        )
        self.assertEqual(rc, 1, msg=err)
        res = json.loads(out)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(
            any("trade-to-trade" in r for r in res["results"][0]["reasons"]),
            msg="Expected T2T block, got reasons=%r" % (res["results"][0]["reasons"],),
        )
        with open(risk_out, encoding="utf-8") as fh:
            recorded = json.load(fh)
        self.assertEqual(recorded["orders_file"], orders_path)

        # Positional empty-orders file reports the actual positional path in stderr
        empty_orders = os.path.join(self.tmp, "empty_orders.json")
        with open(empty_orders, "w", encoding="utf-8") as fh:
            json.dump({"orders": []}, fh)
        rc_empty, _, err_empty = _run_cli(risk_mod, [empty_orders, "--no-write"])
        self.assertEqual(rc_empty, 1)
        self.assertIn(empty_orders, err_empty)

    # Bugs 6 & 7: Upstox.get_ohlc parameter order & get_quotes missing item ---

    def test_upstox_get_ohlc_positional_args_and_get_quotes_missing_symbol(self):
        captured = []

        def fake_http(url, headers=None, timeout=15):
            captured.append(url)
            if "/v3/market-quote/ohlc" in url:
                return (
                    200,
                    json.dumps(
                        {
                            "status": "success",
                            "data": {
                                "NSE_EQ:RELIANCE": {
                                    "instrument_token": "NSE_EQ|INE002A01018",
                                    "last_price": 1350.0,
                                    "timestamp": 1792386900000,
                                    "live_ohlc": {"open": 1340.0, "high": 1355.0, "low": 1338.0, "close": 1350.0, "volume": 10000},
                                }
                            },
                        }
                    ),
                )
            if "/market-quote/quotes" in url:
                return (
                    200,
                    json.dumps(
                        {
                            "status": "success",
                            "data": {
                                "NSE_EQ:RELIANCE": {
                                    "instrument_token": "NSE_EQ|INE002A01018",
                                    "symbol": "RELIANCE",
                                    "last_price": 1350.0,
                                    "timestamp": 1792386900000,
                                    "ohlc": {"open": 1340.0, "high": 1355.0, "low": 1338.0, "close": 1350.0},
                                },
                                "NSE_EQ:EXTRA": {
                                    "instrument_token": "NSE_EQ|EXTRA",
                                    "last_price": 100.0,
                                },
                            },
                        }
                    ),
                )
            return 404, "{}"

        cache = {
            "RELIANCE": {"instrument_key": "NSE_EQ|INE002A01018", "exchange": "NSE", "trading_symbol": "RELIANCE"},
            "TCS": {"instrument_key": "NSE_EQ|INE467B01029", "exchange": "NSE", "trading_symbol": "TCS"},
        }
        prov = data_mod.UpstoxMarketDataProvider(analytics_token="tok_dummy", http_get=fake_http)
        prov._key_by_symbol = {("NSE", k): v["instrument_key"] for k, v in cache.items()}
        prov._symbol_by_key = {v["instrument_key"]: k for k, v in cache.items()}

        ohlc = prov.get_ohlc("RELIANCE", "NSE", "30m")
        self.assertEqual(ohlc["symbol"], "RELIANCE")
        self.assertEqual(ohlc["interval"], "30m")
        self.assertTrue(any("interval=I30" in u for u in captured), msg=captured)

        quotes = prov.get_quotes(["RELIANCE", "TCS"])
        self.assertIn("RELIANCE", quotes)
        self.assertIn("TCS", quotes)
        self.assertEqual(quotes["TCS"]["data_status"], data_mod.DataStatus.UNAVAILABLE)
        self.assertFalse(quotes["TCS"]["is_live"])

    # Bug 8: Offline provider malformed JSON raises DataError + dict cache ----

    def test_offline_provider_corrupt_json_raises_data_error_and_reads_dict_instruments(self):
        off_dir = os.path.join(self.tmp, "offline")
        os.makedirs(off_dir)
        with open(os.path.join(off_dir, "NIFTY-chain.json"), "w", encoding="utf-8") as fh:
            fh.write("{corrupt json")
        prov = data_mod.OfflineMarketDataProvider(off_dir)
        with self.assertRaises(data_mod.DataError):
            prov.get_option_chain("NIFTY")

        inst_path = os.path.join(off_dir, "instruments.json")
        with open(inst_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "instruments": {
                        "RELIANCE": {
                            "symbol": "RELIANCE",
                            "exchange": "NSE",
                            "name": "Reliance",
                            "lot_size": 1,
                            "tick_size": 0.05,
                            "tick_size_unit": "INR",
                        }
                    }
                },
                fh,
            )
        res = prov.get_instruments("NSE", symbol="RELIANCE")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["instruments"][0]["symbol"], "RELIANCE")

        with open(inst_path, "w", encoding="utf-8") as fh:
            fh.write("{broken instruments")
        with self.assertRaises(data_mod.DataError):
            prov.get_instruments("NSE")

    # Bug 9: ticket.py non-list portfolio positions raises TicketError --------

    def test_ticket_apply_fill_and_flatten_reject_non_list_positions(self):
        pf_path = os.path.join(self.tmp, "portfolio.json")
        with open(pf_path, "w", encoding="utf-8") as fh:
            json.dump({"cash": 100000, "currency": "INR", "positions": {}}, fh)
        with self.assertRaises(ticket_mod.TicketError):
            ticket_mod.apply_fill(pf_path, {"market": "IN"}, "RELIANCE", "buy", 1, 1350.0, "2026-10-19", market="IN")

        rc, _, err = _run_cli(
            ticket_mod,
            [
                "flatten",
                "--symbol", "RELIANCE",
                "--portfolio", pf_path,
                "--plan", os.path.join(self.tmp, "missing_plan.json"),
                "--orders", os.path.join(self.tmp, "orders.json"),
                "--config", os.path.join(self.tmp, "missing_cfg.json"),
                "--market", "IN",
                "--now", "2026-10-19 10:15",
            ],
        )
        self.assertEqual(rc, 1)
        self.assertIn("positions", err)

    # Bug 10: alerts.py --market IN before or after subcommand ----------------

    def test_alerts_market_flag_works_before_and_after_subcommand(self):
        for argv in (
            ["--market", "IN", "clock", "--now", "2026-10-16 10:15", "--json"],
            ["clock", "--market", "IN", "--now", "2026-10-16 10:15", "--json"],
        ):
            rc, out, err = _run_cli(alerts_mod, argv)
            self.assertEqual(rc, 0, msg=err)
            doc = json.loads(out)
            self.assertEqual(doc["market"], "NSE")
            self.assertEqual(doc["profile"], "IN")
            self.assertIn("now_ist", doc)

        rc_q, out_q, err_q = _run_cli(
            alerts_mod,
            ["--market", "IN", "quote", "RELIANCE", "--offline", BHARAT_FIXTURES, "--now", "2026-10-19 15:35", "--json"],
        )
        self.assertEqual(rc_q, 0, msg=err_q + "\n" + out_q)
        qdoc = json.loads(out_q)
        self.assertIn("RELIANCE", qdoc["quotes"])

        # Omitting --market on `check` must still allow rules.json `"market": "IN"` inference
        rules_path = os.path.join(self.tmp, "rules.json")
        with open(rules_path, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN", "rules": [{"id": "rel-above", "symbol": "RELIANCE", "type": "above", "price": 1000.0}]}, fh)
        state_path = os.path.join(self.tmp, "alerts_state.json")
        prices_path = os.path.join(self.tmp, "prices.json")
        rc_c, out_c, err_c = _run_cli(
            alerts_mod,
            ["check", "--rules", rules_path, "--state", state_path, "--prices-out", prices_path,
             "--offline", BHARAT_FIXTURES, "--now", "2026-10-19 15:35", "--json"],
        )
        self.assertEqual(rc_c, 0, msg=err_c + "\n" + out_c)
        cdoc = json.loads(out_c)
        self.assertEqual(cdoc["market"]["market"], "NSE")
        self.assertIn("rings", cdoc)

    # Bug 11: F&O derivative contract lot_size resolution via instruments -----

    def test_fo_lot_size_resolves_derivative_contracts_and_size_trade_cache(self):
        self.assertEqual(inst_mod.get_fo_lot_size("NIFTY26NOV24000CE"), 25)
        self.assertIsNone(inst_mod.get_fo_lot_size("UNKNOWNCO26OCT100CE"))

        inst_path = os.path.join(self.tmp, "inst_master.json")
        with open(inst_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "effective_date": "2026-10-19",
                    "instruments": {
                        "RELIANCE": {
                            "symbol": "RELIANCE",
                            "exchange": "NSE",
                            "series": "EQ",
                            "is_trade_to_trade": False,
                            "is_fo_eligible": True,
                            "fo_lot_size": 250,
                            "tick_size": 0.05,
                            "tick_size_unit": "INR",
                            "lot_size": 1,
                        }
                    },
                },
                fh,
            )

        plan = {
            "symbol": "RELIANCE26NOV1400CE",
            "side": "buy",
            "entry": 20.0,
            "stop": 16.0,
            "target": 28.0,
            "asset_type": "OPTIONS",
            "market": "IN",
            "date": "2026-10-19",
            "expiry": "2026-11-26",
        }
        cfg = {
            "market": "IN",
            "currency": "INR",
            "budget": 500000,
            "cash_buffer_pct": 10,
            "max_position_pct": 50,
            "max_order_inr": 250000,
            "max_loss_per_trade_inr": 1200,
            "allow_options": True,
        }
        sized = size_mod.size_trade(plan, cfg, today="2026-10-19", market="IN", instruments_path=inst_path)
        self.assertEqual(sized["lot_size"], 250)
        self.assertEqual(sized["qty"], 250)
        self.assertEqual(sized["contracts"], 1)


if __name__ == "__main__":
    unittest.main()
