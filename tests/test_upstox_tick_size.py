"""Regression tests for canonical INR tick-size normalization and end-to-end precision.

Verifies:
1. Upstox BOD instrument-master paise -> INR conversion at provider boundary only.
2. No double conversion across cache persistence, lookup, and downstream tools.
3. Separate NSE vs BSE tick sizes retained (e.g. NSE RELIANCE Rs 0.10 vs BSE Rs 0.05).
4. Missing or invalid tick sizes handled explicitly (no silent Rs 0.05 invention).
5. Entry, exit, sizing, risk check, and ticket prices rounded and validated against canonical tick size.
6. US equities and legacy offline CSV fixtures remain backward compatible.
Python 3.8+ compatible.
"""

from decimal import Decimal
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("trader-alerts", "trader-entry", "trader-exit", "trader-budget", "trader-risk", "trader-broker"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check as risk_mod  # noqa: E402
import data_in  # noqa: E402
import entry as entry_mod  # noqa: E402
import exit as exit_mod  # noqa: E402
import instruments as inst_mod  # noqa: E402
import size as size_mod  # noqa: E402
import ticket as ticket_mod  # noqa: E402


SAMPLE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0IiwiZXhwIjoyMDAwMDAwMDAwfQ.sig"


def _run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def _ist_epoch(date_str, hh, mm):
    import datetime
    d = datetime.date.fromisoformat(date_str)
    t = datetime.time(hh, mm)
    dt_utc = datetime.datetime.combine(d, t) - datetime.timedelta(hours=5, minutes=30)
    return int(dt_utc.replace(tzinfo=datetime.timezone.utc).timestamp())


def _sample_upstox_records():
    """Real-format Upstox BOD JSON records (tick_size in paise, indices None)."""
    nse = [
        {
            "segment": "NSE_EQ",
            "name": "RELIANCE INDUSTRIES LTD",
            "exchange": "NSE",
            "isin": "INE002A01018",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE002A01018",
            "lot_size": 1,
            "exchange_token": "2885",
            "trading_symbol": "RELIANCE",
            "tick_size": 10.0,
        },
        {
            "segment": "NSE_EQ",
            "name": "VODAFONE IDEA LIMITED",
            "exchange": "NSE",
            "isin": "INE669E01016",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE669E01016",
            "lot_size": 1,
            "exchange_token": "14366",
            "trading_symbol": "IDEA",
            "tick_size": 1.0,
        },
        {
            "segment": "NSE_EQ",
            "name": "MRF LTD",
            "exchange": "NSE",
            "isin": "INE883A01011",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE883A01011",
            "lot_size": 1,
            "exchange_token": "2277",
            "trading_symbol": "MRF",
            "tick_size": 500.0,
        },
        {
            "segment": "NSE_FO",
            "name": "NIFTY",
            "exchange": "NSE",
            "instrument_type": "CE",
            "instrument_key": "NSE_FO|54321",
            "lot_size": 75,
            "exchange_token": "54321",
            "trading_symbol": "NIFTY26NOV27000CE",
            "underlying_symbol": "NIFTY",
            "strike_price": 27000.0,
            "expiry": 1795689000000,
            "tick_size": 5.0,
        },
        {
            "segment": "NCD_FO",
            "name": "USDINR",
            "exchange": "NSE",
            "instrument_type": "FUT",
            "instrument_key": "NCD_FO|99001",
            "lot_size": 1000,
            "exchange_token": "99001",
            "trading_symbol": "USDINR26NOVFUT",
            "underlying_symbol": "USDINR",
            "expiry": 1795689000000,
            "tick_size": 0.25,
        },
        {
            "segment": "NSE_INDEX",
            "name": "Nifty 50",
            "exchange": "NSE",
            "instrument_type": "INDEX",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "exchange_token": "Nifty 50",
            "trading_symbol": "NIFTY",
            "tick_size": None,
        },
    ]
    bse = [
        {
            "segment": "BSE_EQ",
            "name": "RELIANCE INDUSTRIES LTD",
            "exchange": "BSE",
            "isin": "INE002A01018",
            "instrument_type": "EQ",
            "instrument_key": "BSE_EQ|INE002A01018",
            "lot_size": 1,
            "exchange_token": "500325",
            "trading_symbol": "RELIANCE",
            "tick_size": 5.0,
        },
    ]
    return nse, bse


def _write_inr_cache(tmp_dir):
    nse_raw, bse_raw = _sample_upstox_records()

    def fake_http(url, headers=None, timeout=15):
        if "NSE.json.gz" in url:
            return 200, gzip.compress(json.dumps(nse_raw).encode("utf-8"))
        if "BSE.json.gz" in url:
            return 200, gzip.compress(json.dumps(bse_raw).encode("utf-8"))
        raise AssertionError("Unexpected URL: %s" % url)

    prov = data_in.UpstoxMarketDataProvider(analytics_token=SAMPLE_TOKEN, http_get=fake_http)
    nse_res = prov.get_instruments(exchange="NSE")
    bse_res = prov.get_instruments(exchange="BSE")
    combined = nse_res["instruments"] + bse_res["instruments"]
    payload = inst_mod.build_master_from_provider_records(combined, effective_date="2026-10-19")
    cache_path = os.path.join(tmp_dir, "instruments.json")
    inst_mod.save_instrument_cache(payload, cache_path=cache_path, history_dir=None)
    return cache_path, payload, prov


def _make_intraday_chart(tmp_dir, symbol="RELIANCE.NS", session_date="2026-10-19",
                         or_bars=None, trig_close=1335.44, trig_low=1334.02):
    bars_t = []
    bars_o = []
    bars_h = []
    bars_l = []
    bars_c = []
    bars_v = []

    raw_bars = or_bars or [
        (9, 15, 1330.00, 1334.00, 1328.02, 1332.00, 10000),
        (9, 20, 1332.00, 1335.03, 1331.01, 1334.10, 11000),
        (9, 25, 1334.10, 1334.90, 1332.50, 1334.50, 9000),
        (9, 30, 1334.50, 1336.00, trig_low, trig_close, 15000),
        (9, 35, trig_close, 1337.00, 1334.80, 1336.20, 12000),
    ]
    for hh, mm, o, h, l, c, v in raw_bars:
        bars_t.append(_ist_epoch(session_date, hh, mm))
        bars_o.append(o)
        bars_h.append(h)
        bars_l.append(l)
        bars_c.append(c)
        bars_v.append(v)

    chart = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "regularMarketPrice": bars_c[-1],
                        "chartPreviousClose": 1329.00,
                        "regularMarketTime": bars_t[-1] + 300,
                        "dataGranularity": "5m",
                    },
                    "timestamp": bars_t,
                    "indicators": {
                        "quote": [
                            {
                                "open": bars_o,
                                "high": bars_h,
                                "low": bars_l,
                                "close": bars_c,
                                "volume": bars_v,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    p = os.path.join(tmp_dir, "%s.json" % symbol.replace(".", "_"))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(chart, fh)
    return p


class TestUpstoxTickSizeNormalization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="upstox_tick_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_provider_boundary_converts_paise_to_inr_once(self):
        cache_path, payload, prov = _write_inr_cache(self.tmp)

        rel_nse = prov.get_instrument_metadata("RELIANCE", exchange="NSE", require_tick=True)
        rel_bse = prov.get_instrument_metadata("RELIANCE", exchange="BSE", require_tick=True)
        idea_nse = prov.get_instrument_metadata("IDEA", exchange="NSE", require_tick=True)
        mrf_nse = prov.get_instrument_metadata("MRF", exchange="NSE", require_tick=True)
        fo_opt = prov.get_instruments(exchange="NSE", symbol="NIFTY26NOV27000CE")["instruments"][0]
        cur_fut = prov.get_instruments(exchange="NSE", symbol="USDINR26NOVFUT")["instruments"][0]
        idx = prov.get_instruments(exchange="NSE", symbol="NIFTY")["instruments"][0]

        self.assertEqual(rel_nse["tick_size"], 0.1)
        self.assertEqual(rel_nse["tick_size_unit"], "INR")
        self.assertEqual(rel_bse["tick_size"], 0.05)
        self.assertEqual(idea_nse["tick_size"], 0.01)
        self.assertEqual(mrf_nse["tick_size"], 5.0)
        self.assertEqual(fo_opt["tick_size"], 0.05)
        self.assertEqual(cur_fut["tick_size"], 0.0025)
        self.assertIsNone(idx["tick_size"])
        self.assertEqual(idx["tick_size_status"], "not_applicable_index")

        # Repeated calls / cached lookups never double-convert
        rel_nse_again = prov.get_instruments(exchange="NSE", symbol="RELIANCE")["instruments"][0]
        self.assertEqual(rel_nse_again["tick_size"], 0.1)

        # Master cache lookup keeps NSE vs BSE distinct without double-converting
        reloaded = inst_mod.load_instrument_cache(cache_path, max_age_days=30, as_of_date="2026-10-19")
        r_nse = inst_mod.resolve_instrument("RELIANCE", reloaded, exchange="NSE")
        r_bse = inst_mod.resolve_instrument("RELIANCE", reloaded, exchange="BSE")
        r_bse_suffix = inst_mod.resolve_instrument("RELIANCE.BO", reloaded)
        self.assertEqual(r_nse["tick_size"], 0.1)
        self.assertEqual(r_bse["tick_size"], 0.05)
        self.assertEqual(r_bse_suffix["tick_size"], 0.05)

        # OfflineMarketDataProvider respects INR cache and exchange distinction without dividing by 100
        off = data_in.OfflineMarketDataProvider(self.tmp)
        off_nse = off.get_instruments(exchange="NSE", symbol="RELIANCE")["instruments"][0]
        off_bse = off.get_instruments(exchange="BSE", symbol="RELIANCE")["instruments"][0]
        self.assertEqual(off_nse["tick_size"], 0.1)
        self.assertEqual(off_bse["tick_size"], 0.05)

    def test_missing_or_invalid_tick_size_is_not_silently_defaulted(self):
        bad_records = [
            {
                "segment": "NSE_EQ",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "instrument_key": "NSE_EQ|BAD1",
                "trading_symbol": "MISSINGTICK",
                "lot_size": 1,
                "tick_size": None,
            },
            {
                "segment": "NSE_EQ",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "instrument_key": "NSE_EQ|BAD2",
                "trading_symbol": "ZEROTICK",
                "lot_size": 1,
                "tick_size": 0,
            },
            {
                "segment": "NSE_EQ",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "instrument_key": "NSE_EQ|BAD3",
                "trading_symbol": "NEGTICK",
                "lot_size": 1,
                "tick_size": -5.0,
            },
            {
                "segment": "NSE_EQ",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "instrument_key": "NSE_EQ|BAD4",
                "trading_symbol": "BOOLTICK",
                "lot_size": 1,
                "tick_size": True,
            },
        ]

        def fake_http(url, headers=None, timeout=15):
            return 200, gzip.compress(json.dumps(bad_records).encode("utf-8"))

        prov = data_in.UpstoxMarketDataProvider(analytics_token=SAMPLE_TOKEN, http_get=fake_http)
        res = prov.get_instruments(exchange="NSE")
        for rec in res["instruments"]:
            self.assertIsNone(rec["tick_size"])
            self.assertIn(rec["tick_size_status"], ("missing", "invalid"))

        for sym in ("MISSINGTICK", "ZEROTICK", "NEGTICK", "BOOLTICK"):
            with self.assertRaises(data_in.DataError):
                prov.get_instrument_metadata(sym, exchange="NSE", require_tick=True)

        payload = inst_mod.build_master_from_provider_records(res["instruments"], effective_date="2026-10-19")
        cache_path = os.path.join(self.tmp, "bad_instruments.json")
        inst_mod.save_instrument_cache(payload, cache_path=cache_path, history_dir=None)

        for sym in ("MISSINGTICK", "ZEROTICK", "NEGTICK", "BOOLTICK"):
            with self.assertRaises(inst_mod.TickResolutionError):
                inst_mod.resolve_instrument(sym, payload, exchange="NSE")

        # Offline provider also refuses to fabricate 0.05
        off = data_in.OfflineMarketDataProvider(os.path.dirname(cache_path))
        with open(os.path.join(self.tmp, "instruments.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        off_rec = off.get_instruments(exchange="NSE", symbol="MISSINGTICK")["instruments"][0]
        self.assertIsNone(off_rec["tick_size"])

        # entry, exit, size, and check all fail closed rather than substituting 0.05
        chart_path = _make_intraday_chart(self.tmp, symbol="MISSINGTICK.NS")
        cfg_path = os.path.join(self.tmp, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN", "budget": 50000, "max_loss_per_trade_inr": 1000}, fh)

        rc_e, _, err_e = _run_cli(entry_mod, [
            "check", "MISSINGTICK", "--market", "IN", "--offline", chart_path,
            "--instruments", cache_path, "--config", cfg_path, "--now", "2026-10-19 09:40",
        ])
        self.assertEqual(rc_e, 1)
        self.assertIn("tick", err_e.lower())

        rc_x, _, err_x = _run_cli(exit_mod, [
            "plan", "--symbol", "MISSINGTICK", "--entry", "1335.40", "--stop", "price:1320.00",
            "--market", "IN", "--instruments", cache_path, "--config", cfg_path, "--now", "2026-10-19 09:40",
        ])
        self.assertEqual(rc_x, 1)
        self.assertIn("tick", err_x.lower())

        bad_plan = {
            "symbol": "MISSINGTICK", "side": "buy", "date": "2026-10-19", "market": "IN",
            "entry": 1335.40, "stop": 1320.00, "target": 1366.20, "setup": "orb",
        }
        with self.assertRaises(size_mod.SizeError):
            size_mod.size_trade(bad_plan, {"market": "IN", "budget": 50000}, today="2026-10-19",
                                market="IN", instruments_path=cache_path)

        bad_order = {
            "symbol": "MISSINGTICK", "side": "buy", "qty": "1", "type": "limit",
            "time_in_force": "day", "order_class": "bracket", "limit_price": "1335.40",
            "stop_loss_stop_price": "1320.00", "take_profit_limit_price": "1366.20",
            "market": "IN",
        }
        r = risk_mod.check(
            [bad_order], {"market": "IN", "budget": 50000, "allow_off_hours": True},
            today="2026-10-19", market="IN", instruments_cache=payload,
        )
        self.assertEqual(r["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("tick_size" in reason for reason in r["results"][0]["reasons"]))

    def test_nse_vs_bse_boundary_rounding_and_full_pipeline_e2e(self):
        cache_path, _, _ = _write_inr_cache(self.tmp)
        cfg_path = os.path.join(self.tmp, "config.json")
        pf_path = os.path.join(self.tmp, "portfolio.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({
                "market": "IN",
                "broker": "paper",
                "paper": True,
                "budget": 100000,
                "cash_buffer_pct": 10,
                "max_position_pct": 75,
                "max_order_inr": 90000,
                "min_order_inr": 500,
                "max_loss_per_trade_inr": 2500,
                "max_daily_loss_inr": 5000,
                "allow_off_hours": True,
            }, fh)
        with open(pf_path, "w", encoding="utf-8") as fh:
            json.dump({"cash": 100000, "currency": "INR", "positions": []}, fh)

        # Same underlying RELIANCE on NSE (tick 0.10) vs BSE (tick 0.05) with 0.05 boundary values
        chart_nse = _make_intraday_chart(self.tmp, symbol="RELIANCE.NS", trig_close=1335.44)
        chart_bse = _make_intraday_chart(self.tmp, symbol="RELIANCE.BO", trig_close=1335.44)

        for label, exch, sym_arg, chart_p, exp_tick, exp_entry, exp_stop, exp_target in (
            ("nse", "NSE", "RELIANCE.NS", chart_nse, 0.10, 1335.40, 1331.50, 1343.20),
            ("bse", "BSE", "RELIANCE.BO", chart_bse, 0.05, 1335.45, 1331.50, 1343.35),
        ):
            ent_p = os.path.join(self.tmp, "entry_%s.json" % label)
            plan_p = os.path.join(self.tmp, "plan_%s.json" % label)
            ord_p = os.path.join(self.tmp, "orders_%s.json" % label)
            risk_p = os.path.join(self.tmp, "risk_%s.json" % label)
            state_p = os.path.join(self.tmp, "broker_%s.json" % label)

            # 1. trader-entry
            rc_e, _, err_e = _run_cli(entry_mod, [
                "check", sym_arg, "--market", "IN", "--exchange", exch,
                "--offline", chart_p, "--instruments", cache_path, "--config", cfg_path,
                "--now", "2026-10-19 09:40", "--write", ent_p,
            ])
            self.assertEqual(rc_e, 0, err_e)
            with open(ent_p, encoding="utf-8") as fh:
                edoc = json.load(fh)
            self.assertEqual(edoc["exchange"], exch)
            self.assertEqual(edoc["tick_size"], exp_tick)
            self.assertEqual(edoc["price"], exp_entry)

            # 2. trader-exit (or-mid: high=1335.03 -> 1335.00(NSE)/1335.05(BSE), low=1328.02 -> 1328.00)
            rc_x, _, err_x = _run_cli(exit_mod, [
                "plan", "--from", ent_p, "--market", "IN", "--exchange", exch,
                "--instruments", cache_path, "--config", cfg_path,
                "--now", "2026-10-19 09:40", "--write", plan_p,
            ])
            self.assertEqual(rc_x, 0, err_x)
            with open(plan_p, encoding="utf-8") as fh:
                pdoc = json.load(fh)
            self.assertEqual(pdoc["exchange"], exch)
            self.assertEqual(pdoc["tick_size"], exp_tick)
            self.assertEqual(pdoc["entry"], exp_entry)
            self.assertEqual(pdoc["stop"], exp_stop)
            self.assertEqual(pdoc["target"], exp_target)
            for px in (pdoc["entry"], pdoc["stop"], pdoc["target"],
                       pdoc["opening_range"]["high"], pdoc["opening_range"]["low"]):
                self.assertEqual(Decimal(str(px)) % Decimal(str(exp_tick)), Decimal(0))

            # 3. trader-budget (--limit passes unrounded boundary, rounded to canonical tick!)
            rc_s, _, err_s = _run_cli(size_mod, [
                "--plan", plan_p, "--config", cfg_path, "--portfolio", pf_path,
                "--market", "IN", "--exchange", exch, "--instruments", cache_path,
                "--limit", "1335.44", "--now", "2026-10-19 09:40", "--write", ord_p,
            ])
            self.assertEqual(rc_s, 0, err_s)
            with open(ord_p, encoding="utf-8") as fh:
                odoc = json.load(fh)
            order = odoc["orders"][0]
            self.assertEqual(order["exchange"], exch)
            self.assertEqual(order["tick_size"], exp_tick)
            self.assertEqual(float(order["limit_price"]), exp_entry)
            self.assertEqual(float(order["stop_loss_stop_price"]), exp_stop)
            self.assertEqual(float(order["take_profit_limit_price"]), exp_target)

            # 4. trader-risk
            rc_r, _, err_r = _run_cli(risk_mod, [
                "--orders", ord_p, "--config", cfg_path, "--portfolio", pf_path,
                "--market", "IN", "--exchange", exch, "--instruments", cache_path,
                "--now", "2026-10-19 09:40", "--out", risk_p, "--allow-off-hours",
            ])
            self.assertEqual(rc_r, 0, err_r)
            with open(risk_p, encoding="utf-8") as fh:
                rdoc = json.load(fh)
            self.assertEqual(rdoc["results"][0]["verdict"], "PASS")

            # 5. trader-broker ticket
            rc_t, out_t, err_t = _run_cli(ticket_mod, [
                "show", "--orders", ord_p, "--risk", risk_p, "--state", state_p,
                "--config", cfg_path, "--market", "IN", "--now", "2026-10-19 09:40", "--json",
            ])
            self.assertEqual(rc_t, 0, err_t)
            tdoc = json.loads(out_t)
            t0 = tdoc[0]
            self.assertTrue(t0["ready"])
            self.assertEqual(t0["exchange"], exch)
            self.assertEqual(t0["tick_size"], exp_tick)
            for k in ("limit_price", "stop_loss_stop_price", "take_profit_limit_price"):
                self.assertEqual(Decimal(str(t0["tool_input"][k])) % Decimal(str(exp_tick)), Decimal(0))

    def test_boundary_rounding_idea_1p_mrf_500p_and_offtick_risk_block(self):
        cache_path, payload, _ = _write_inr_cache(self.tmp)
        cfg = {"market": "IN", "budget": 500000, "max_loss_per_trade_inr": 25000, "allow_off_hours": True}

        # 1. IDEA (1 paisa = Rs 0.01): 10.13 must stay 10.13 (NOT coerced to 0.05)
        idea_plan = {
            "symbol": "IDEA", "side": "buy", "date": "2026-10-19", "market": "IN", "exchange": "NSE",
            "entry": 10.13, "stop": 9.87, "target": 10.65, "setup": "level", "tick_size": 0.01,
        }
        sized_idea = size_mod.size_trade(
            idea_plan, cfg, limit=10.13, today="2026-10-19", market="IN", instruments_path=cache_path
        )
        self.assertEqual(sized_idea["tick_size"], 0.01)
        self.assertEqual(sized_idea["entry"], 10.13)
        self.assertEqual(sized_idea["stop"], 9.87)
        self.assertEqual(sized_idea["target"], 10.65)
        ord_idea = size_mod.build_order(sized_idea, cfg=cfg)
        self.assertEqual(ord_idea["limit_price"], "10.13")

        # 2. MRF (500 paise = Rs 5.00): 149502.50 -> 149505.00, stop 148004.90 -> 148000.00 (floor)
        mrf_c = exit_mod.compute(149502.5, "price", 148004.9, "r", Decimal("2"), market="IN", tick_size=5.0)
        self.assertEqual(float(mrf_c["entry"]), 149505.0)
        self.assertEqual(float(mrf_c["stop"]), 148000.0)
        self.assertEqual(float(mrf_c["target"]), 152515.0)

        # 2b. Sub-paisa derivative tick (0.25 paise = Rs 0.0025): 4-decimal precision preserved end-to-end
        cur_c = exit_mod.compute(84.1024, "price", 84.0010, "r", Decimal("2"), market="IN", tick_size=0.0025)
        self.assertEqual(float(cur_c["entry"]), 84.1025)
        self.assertEqual(float(cur_c["stop"]), 84.0000)
        self.assertEqual(float(cur_c["target"]), 84.3075)
        cur_plan = {
            "symbol": "USDINR26NOVFUT", "side": "buy", "date": "2026-10-19", "market": "IN",
            "exchange": "NSE", "asset_type": "FUTURES", "lot_size": 1000,
            "entry": float(cur_c["entry"]), "stop": float(cur_c["stop"]), "target": float(cur_c["target"]),
            "setup": "level", "tick_size": 0.0025,
        }
        cur_cfg = {
            "market": "IN", "budget": 500000, "max_order_inr": 450000,
            "max_loss_per_trade_inr": 5000, "max_daily_loss_inr": 10000,
            "allow_off_hours": True, "allow_futures": True,
        }
        cur_s = size_mod.size_trade(cur_plan, cur_cfg, limit=84.1024, today="2026-10-19", market="IN", instruments_path=cache_path)
        self.assertEqual(cur_s["entry"], 84.1025)
        cur_ord = size_mod.build_order(cur_s, cfg=cur_cfg)
        self.assertEqual(cur_ord["limit_price"], "84.1025")
        self.assertEqual(cur_ord["stop_loss_stop_price"], "84.0000")
        self.assertEqual(cur_ord["take_profit_limit_price"], "84.3075")
        cur_chk = risk_mod.check([cur_ord], cur_cfg, today="2026-10-19", market="IN", instruments_cache=payload)
        self.assertEqual(cur_chk["results"][0]["verdict"], "PASS", cur_chk["results"][0].get("reasons"))
        cur_plain = ticket_mod.plain(cur_ord, market="IN")
        self.assertIn("84.1025", cur_plain)
        self.assertIn("84.3075", cur_plain)

        # 3. Off-tick order for NSE RELIANCE (tick 0.10): 1335.45 must BLOCK in risk check
        bad_tick_order = {
            "symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": "5", "type": "limit",
            "time_in_force": "day", "order_class": "bracket",
            "limit_price": "1335.45", "stop_loss_stop_price": "1331.50", "take_profit_limit_price": "1343.20",
            "market": "IN", "tick_size": 0.10,
            "plan": {"entry": 1335.45, "stop": 1331.50, "target": 1343.20, "tick_size": 0.10, "exchange": "NSE"},
        }
        chk = risk_mod.check(
            [bad_tick_order], cfg, today="2026-10-19", market="IN", instruments_cache=payload,
        )
        self.assertEqual(chk["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("not a valid multiple of tick size" in r for r in chk["results"][0]["reasons"]))

        # 4. Size refuses disagreement between plan.tick_size and exchange master tick_size
        mismatch_plan = {
            "symbol": "RELIANCE", "side": "buy", "date": "2026-10-19", "market": "IN", "exchange": "NSE",
            "entry": 1335.40, "stop": 1331.50, "target": 1343.20, "setup": "orb", "tick_size": 0.05,
        }
        with self.assertRaises(size_mod.SizeError):
            size_mod.size_trade(mismatch_plan, cfg, today="2026-10-19", market="IN", instruments_path=cache_path)

    def test_us_equity_and_offline_csv_backward_compatibility(self):
        # US equity continues using USD $0.01 cent rounding seamlessly
        us_c = exit_mod.compute(364.234, "pct", Decimal("2"), "r", Decimal("2"), market="US")
        self.assertEqual(us_c["tick_size"], Decimal("0.01"))
        self.assertEqual(float(us_c["entry"]), 364.23)

        us_plan = {
            "symbol": "TSLA", "side": "buy", "date": "2026-10-19", "market": "US",
            "entry": 364.23, "stop": 356.94, "target": 378.81, "setup": "orb",
        }
        us_cfg = {"market": "US", "budget": 10000, "max_loss_per_trade_usd": 100}
        us_s = size_mod.size_trade(us_plan, us_cfg, limit=364.23, today="2026-10-19", market="US")
        self.assertNotIn("tick_size", us_s)
        us_ord = size_mod.build_order(us_s, cfg=us_cfg)
        self.assertEqual(us_ord["limit_price"], "364.23")

        # Existing EQUITY_L_sample.csv offline fixture preserves Rs 0.05 fallback
        csv_path = os.path.join(ROOT, "tests", "fixtures", "bharat", "EQUITY_L_sample.csv")
        eq_recs = inst_mod.parse_equity_l_csv(csv_path, exchange="NSE")
        self.assertEqual(eq_recs["RELIANCE"]["tick_size"], 0.05)

    def test_breakout_20d_invalidation_and_reconciliation_use_canonical_tick(self):
        cache_path, _, _ = _write_inr_cache(self.tmp)
        daily_bars = []
        import datetime
        base_d = datetime.date(2026, 9, 1)
        added = 0
        d = base_d
        while added < 35:
            if d.weekday() < 5 and d.isoformat() != "2026-10-02":
                close_v = 1320.00 + added * 0.25
                if added == 34:
                    close_v = 1335.43  # triggers breakout above prior max cleanly
                daily_bars.append({
                    "t": _ist_epoch(d.isoformat(), 15, 30),
                    "o": close_v - 1.0,
                    "h": close_v + 0.5,
                    "l": close_v - 1.5,
                    "c": close_v,
                    "v": 100000,
                })
                added += 1
            d += datetime.timedelta(days=1)

        # Evaluate breakout_20d on 2026-10-21 with NSE RELIANCE (0.10) vs BSE RELIANCE (0.05)
        now_utc = entry_mod.parse_now("2026-10-21 10:00", market="IN")
        bars = [
            {
                "t": _ist_epoch("2026-10-21", 9, 15 + 5 * idx),
                "o": 1335.0,
                "h": 1336.0,
                "l": 1334.04 if idx == 3 else 1334.5,
                "c": 1335.64 if idx == 3 else 1334.8,
                "v": 15000,
            }
            for idx in range(5)
        ]
        for exch, sym_s, exp_tick, exp_inv, exp_conf in (
            ("NSE", "RELIANCE.NS", 0.10, 1334.00, 1335.60),
            ("BSE", "RELIANCE.BO", 0.05, 1334.00, 1335.65),
        ):
            ch = {
                "symbol": sym_s,
                "market": "IN",
                "exchange": exch,
                "meta": {"symbol": sym_s, "dataGranularity": "5m"},
                "bars": bars,
                "daily_bars": daily_bars,
                "prev_close": 1335.43,
                "data_time": _ist_epoch("2026-10-21", 9, 40),
                "is_offline": True,
                "provider": "offline",
                "data_tier": "FIXTURE",
                "is_live": False,
                "is_live_capable": False,
            }
            res = entry_mod.evaluate(
                ch, now_utc, setup="breakout_20d", market="IN",
                exchange=exch, instruments_path=cache_path, simulated=True,
            )
            self.assertEqual(res["status"], "triggered")
            self.assertEqual(res["tick_size"], exp_tick)
            self.assertEqual(res["confirmation_price"], exp_conf)
            self.assertEqual(res["invalidation_level"], exp_inv)
            self.assertEqual(Decimal(str(res["invalidation_level"])) % Decimal(str(exp_tick)), Decimal(0))
            self.assertEqual(Decimal(str(res["confirmation_price"])) % Decimal(str(exp_tick)), Decimal(0))

    # --- 12 Final Audit Correctness Regression Tests -----------------------

    def test_audit_01_legacy_csv_cache_without_unit_preserves_inr_compat(self):
        legacy_payload = {
            "metadata_version": "1.0",
            "effective_date": "2026-10-19",
            "generated_at": "2026-10-19T08:00:00+00:00",
            "exchange": "NSE",
            "count": 1,
            "instruments": {
                "RELIANCE": {
                    "canonical_symbol": "RELIANCE",
                    "exchange": "NSE",
                    "name": "Reliance Industries Limited",
                    "series": "EQ",
                    "isin": "INE002A01018",
                    "instrument_type": "EQUITY",
                    "instrument_identifier": "RELIANCE",
                    "tick_size": 0.05,
                    "lot_size": 1,
                    "expiry": None,
                    "is_trade_to_trade": False,
                }
            },
        }
        cache_path = os.path.join(self.tmp, "instruments.json")
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(legacy_payload, fh)

        loaded = inst_mod.load_instrument_cache(cache_path, max_age_days=1, as_of_date="2026-10-19")
        self.assertEqual(loaded["instruments"]["RELIANCE"]["tick_size"], 0.05)
        self.assertEqual(loaded["instruments"]["RELIANCE"]["tick_size_unit"], "INR")

        resolved = inst_mod.resolve_instrument("RELIANCE", loaded, exchange="NSE")
        self.assertEqual(resolved["tick_size"], 0.05)
        self.assertEqual(resolved["tick_size_unit"], "INR")

        off = data_in.OfflineMarketDataProvider(self.tmp)
        off_res = off.get_instruments(exchange="NSE", symbol="RELIANCE")
        self.assertEqual(off_res["instruments"][0]["tick_size"], 0.05)
        self.assertEqual(off_res["instruments"][0]["tick_size_unit"], "INR")

    def test_audit_02_unmarked_upstox_bod_cache_rejected_with_rebuild_guidance(self):
        # Even if raw tick_size looks like 0.05 or 10.0, unmarked Upstox/BOD caches must fail closed.
        for val in (10.0, 0.05):
            sub_dir = os.path.join(self.tmp, "bod_%s" % str(val).replace(".", "_"))
            os.makedirs(sub_dir, exist_ok=True)
            unmarked_bod = {
                "metadata_version": "1.0",
                "effective_date": "2026-10-19",
                "provider": "upstox",
                "instruments": {
                    "RELIANCE": {
                        "canonical_symbol": "RELIANCE",
                        "trading_symbol": "RELIANCE",
                        "exchange": "NSE",
                        "segment": "NSE_EQ",
                        "instrument_key": "NSE_EQ|INE002A01018",
                        "tick_size": val,
                        "lot_size": 1,
                    }
                },
            }
            p = os.path.join(sub_dir, "instruments.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(unmarked_bod, fh)

            with self.assertRaises(inst_mod.TickResolutionError) as ctx:
                inst_mod.load_instrument_cache(p, max_age_days=1, as_of_date="2026-10-19")
            self.assertIn("rebuild", str(ctx.exception).lower())

            off = data_in.OfflineMarketDataProvider(sub_dir)
            with self.assertRaises(data_in.DataError) as octx:
                off.get_instruments(exchange="NSE", symbol="RELIANCE")
            self.assertIn("rebuild", str(octx.exception).lower())

            with self.assertRaises(inst_mod.TickResolutionError):
                inst_mod.build_master_from_provider_records(
                    [unmarked_bod["instruments"]["RELIANCE"]], effective_date="2026-10-19"
                )

    def test_audit_03_ambiguous_unmarked_and_unknown_unit_caches_rejected(self):
        # Ambiguous unmarked record lacking CSV structural provenance (tested with both 0.05 and 10.0)
        for val in (0.05, 10.0):
            ambig_dir = os.path.join(self.tmp, "ambig_%s" % str(val).replace(".", "_"))
            os.makedirs(ambig_dir, exist_ok=True)
            ambig_cache = {
                "effective_date": "2026-10-19",
                "instruments": {
                    "TCS": {
                        "canonical_symbol": "TCS",
                        "exchange": "NSE",
                        "tick_size": val,
                        "lot_size": 1,
                    }
                },
            }
            p = os.path.join(ambig_dir, "instruments.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(ambig_cache, fh)
            with self.assertRaises(inst_mod.TickResolutionError):
                inst_mod.load_instrument_cache(p, max_age_days=1, as_of_date="2026-10-19")
            off = data_in.OfflineMarketDataProvider(ambig_dir)
            with self.assertRaises(data_in.DataError):
                off.get_instruments(exchange="NSE", symbol="TCS")

        # Explicit unknown tick_size_unit must fail explicitly in cache load & master build
        unknown_unit_rec = {
            "canonical_symbol": "TCS",
            "symbol": "TCS",
            "exchange": "NSE",
            "segment": "NSE_EQ",
            "instrument_key": "NSE_EQ|INE467B01029",
            "tick_size": 5.0,
            "tick_size_unit": "USD",
            "lot_size": 1,
        }
        with self.assertRaises(inst_mod.TickResolutionError):
            inst_mod.build_master_from_provider_records([unknown_unit_rec], effective_date="2026-10-19")
        bad_p = os.path.join(self.tmp, "unknown_unit.json")
        with open(bad_p, "w", encoding="utf-8") as fh:
            json.dump({"effective_date": "2026-10-19", "instruments": {"TCS": unknown_unit_rec}}, fh)
        with self.assertRaises(inst_mod.TickResolutionError):
            inst_mod.load_instrument_cache(bad_p, max_age_days=1, as_of_date="2026-10-19")

    def test_audit_04_fresh_subprocess_reload_no_double_conversion(self):
        # 1. Explicit tick_size_unit='paise' converts ONCE on build -> saved as 'INR'
        paise_rec = {
            "canonical_symbol": "RELIANCE",
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "segment": "NSE_EQ",
            "instrument_key": "NSE_EQ|INE002A01018",
            "tick_size": 10.0,
            "tick_size_unit": "paise",
            "lot_size": 1,
        }
        built = inst_mod.build_master_from_provider_records([paise_rec], effective_date="2026-10-19")
        self.assertEqual(built["instruments"]["RELIANCE"]["tick_size"], 0.10)
        self.assertEqual(built["instruments"]["RELIANCE"]["tick_size_unit"], "INR")

        cache_path = os.path.join(self.tmp, "reloaded_instruments.json")
        inst_mod.save_instrument_cache(built, cache_path=cache_path, history_dir=None)

        # Fresh Python subprocess reload -> save -> reload preserves 0.10 INR without double conversion
        code = (
            "import sys; sys.path.insert(0, %r); import instruments; "
            "c1 = instruments.load_instrument_cache(%r, max_age_days=30, as_of_date='2026-10-19'); "
            "r1 = instruments.resolve_instrument('RELIANCE', c1, exchange='NSE'); "
            "instruments.save_instrument_cache(c1, cache_path=%r, history_dir=None); "
            "c2 = instruments.load_instrument_cache(%r, max_age_days=30, as_of_date='2026-10-19'); "
            "r2 = instruments.resolve_instrument('RELIANCE', c2, exchange='NSE'); "
            "assert r1['tick_size'] == 0.10 and r1['tick_size_unit'] == 'INR'; "
            "assert r2['tick_size'] == 0.10 and r2['tick_size_unit'] == 'INR'; "
            "print('OK')"
        ) % (os.path.join(ROOT, "trader-alerts"), cache_path, cache_path, cache_path)
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout.strip(), "OK")

    def test_audit_05_entry_level_rounded_with_explicit_tick_metadata(self):
        cache_path, _, _ = _write_inr_cache(self.tmp)
        now_utc = entry_mod.parse_now("2026-10-19 09:40", market="IN")

        # Opening range high = 1335.03: NSE (0.10) rounds level -> 1335.00; BSE (0.05) -> 1335.05
        for sym_s, exch, exp_tick, exp_lvl in (("RELIANCE.NS", "NSE", 0.10, 1335.00), ("RELIANCE.BO", "BSE", 0.05, 1335.05)):
            ch_path = _make_intraday_chart(self.tmp, symbol=sym_s, trig_close=1335.44)
            ent_p = os.path.join(self.tmp, "audit05_%s.json" % exch.lower())
            rc, out, err = _run_cli(entry_mod, [
                "check", sym_s, "--setup", "orb", "--market", "IN", "--exchange", exch,
                "--offline", ch_path, "--instruments", cache_path,
                "--now", "2026-10-19 09:40", "--write", ent_p, "--json",
            ])
            self.assertEqual(rc, 0, err)
            res = json.loads(out)
            self.assertEqual(res["tick_size"], exp_tick)
            self.assertEqual(res["level"], exp_lvl)
            self.assertEqual(Decimal(str(res["level"])) % Decimal(str(exp_tick)), Decimal(0))
            with open(ent_p, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["level"], exp_lvl)
            self.assertEqual(Decimal(str(doc["level"])) % Decimal(str(exp_tick)), Decimal(0))

    def test_audit_06_closed_market_breakout_20d_uses_resolved_tick(self):
        cache_path, _, _ = _write_inr_cache(self.tmp)
        import datetime
        daily_bars = []
        d = datetime.date(2026, 9, 1)
        added = 0
        while added < 35:
            if d.weekday() < 5 and d.isoformat() != "2026-10-02":
                close_v = 1320.00 + added * 0.25 if added < 34 else 1335.43
                daily_bars.append({
                    "t": _ist_epoch(d.isoformat(), 15, 30),
                    "o": close_v - 1.0, "h": close_v + 0.5, "l": close_v - 1.5,
                    "c": close_v, "v": 100000,
                })
                added += 1
            d += datetime.timedelta(days=1)

        # Saturday 2026-10-24 (closed market): 1335.43 rounds to 1335.40 (NSE 0.10) vs 1335.45 (BSE 0.05)
        closed_now = entry_mod.parse_now("2026-10-24 11:00", market="IN")
        for sym_s, exch, exp_tick, exp_lvl, exp_inv in (
            ("RELIANCE.NS", "NSE", 0.10, 1335.40, 1335.30),
            ("RELIANCE.BO", "BSE", 0.05, 1335.45, 1335.40),
        ):
            ch = {
                "symbol": sym_s, "market": "IN", "exchange": exch,
                "meta": {"symbol": sym_s, "dataGranularity": "5m"},
                "bars": [], "daily_bars": daily_bars, "prev_close": 1335.43,
                "is_offline": True, "provider": "offline", "data_tier": "FIXTURE",
            }
            res = entry_mod.evaluate(
                ch, closed_now, setup="breakout_20d", market="IN",
                exchange=exch, instruments_path=cache_path, simulated=True,
            )
            self.assertEqual(res["tick_size"], exp_tick)
            self.assertEqual(res["level"], exp_lvl)
            self.assertEqual(res["breakout_level"], exp_lvl)
            self.assertEqual(Decimal(str(res["level"])) % Decimal(str(exp_tick)), Decimal(0))
            self.assertEqual(Decimal(str(res["invalidation_level"])) % Decimal(str(exp_tick)), Decimal(0))
            self.assertEqual(res["invalidation_level"], exp_inv)

    def test_audit_07_exit_entry_master_tick_and_exchange_disagreement_raises(self):
        cache_path, _, _ = _write_inr_cache(self.tmp)
        cfg_path = os.path.join(self.tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN", "budget": 100000, "paper": True}, fh)

        entry_p = os.path.join(self.tmp, "entry_bad_tick.json")
        with open(entry_p, "w", encoding="utf-8") as fh:
            json.dump({
                "created_by": "trader-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy",
                "date": "2026-10-19", "status": "triggered", "price": 1335.40, "level": 1335.00,
                "opening_range": {"high": 1335.00, "low": 1328.00, "minutes": 15},
                "market": "IN", "exchange": "NSE", "tick_size": 0.05,  # Disagrees with NSE RELIANCE 0.10
            }, fh)

        rc, _, err = _run_cli(exit_mod, [
            "plan", "--from", entry_p, "--market", "IN", "--exchange", "NSE",
            "--instruments", cache_path, "--config", cfg_path, "--now", "2026-10-19 09:40",
        ])
        self.assertEqual(rc, 1)
        self.assertIn("disagrees", err.lower())

        # CLI --exchange contradicting entry.json exchange also fails clearly
        entry_bse = os.path.join(self.tmp, "entry_bse.json")
        with open(entry_bse, "w", encoding="utf-8") as fh:
            json.dump({
                "created_by": "trader-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy",
                "date": "2026-10-19", "status": "triggered", "price": 1335.45, "level": 1335.05,
                "opening_range": {"high": 1335.05, "low": 1328.00, "minutes": 15},
                "market": "IN", "exchange": "BSE", "tick_size": 0.05,
            }, fh)
        rc2, _, err2 = _run_cli(exit_mod, [
            "plan", "--from", entry_bse, "--market", "IN", "--exchange", "NSE",
            "--instruments", cache_path, "--config", cfg_path, "--now", "2026-10-19 09:40",
        ])
        self.assertEqual(rc2, 1)
        self.assertIn("exchange", err2.lower())

    def test_audit_08_size_main_prices_note_respects_resolved_tick(self):
        # 1. Sub-paisa tick 0.0025: 84.1024 vs plan 84.1000 rounds to 84.1025 (> 84.1000),
        #    and formats both in 4 decimals instead of cent-rounding/collapsing to %.2f.
        cache_p = os.path.join(self.tmp, "subpaisa_inst.json")
        sub_cache = {
            "metadata_version": "1.0",
            "effective_date": "2026-10-19",
            "exchange": "NSE",
            "instruments": {
                "SUBPAISA": {
                    "canonical_symbol": "SUBPAISA",
                    "exchange": "NSE",
                    "series": "EQ",
                    "isin": "INE000Z01099",
                    "instrument_type": "EQUITY",
                    "instrument_identifier": "SUBPAISA",
                    "tick_size": 0.0025,
                    "tick_size_unit": "INR",
                    "lot_size": 1,
                    "is_trade_to_trade": False,
                }
            },
        }
        inst_mod.save_instrument_cache(sub_cache, cache_path=cache_p, history_dir=None)

        plan_p = os.path.join(self.tmp, "cur_plan.json")
        cfg_p = os.path.join(self.tmp, "cur_cfg.json")
        prices_p = os.path.join(self.tmp, "prices.json")
        pf_p = os.path.join(self.tmp, "pf.json")
        with open(plan_p, "w", encoding="utf-8") as fh:
            json.dump({
                "created_by": "trader-exit", "symbol": "SUBPAISA", "side": "buy",
                "date": "2026-10-19", "market": "IN", "exchange": "NSE", "setup": "orb",
                "entry": 84.1000, "stop": 84.0000, "target": 84.3000, "tick_size": 0.0025,
            }, fh)
        with open(cfg_p, "w", encoding="utf-8") as fh:
            json.dump({
                "market": "IN", "budget": 500000, "max_loss_per_trade_inr": 10000,
                "min_reward_risk": 0,
            }, fh)
        with open(prices_p, "w", encoding="utf-8") as fh:
            json.dump({
                "as_of": "2026-10-19T04:10:00Z",  # 09:40 IST
                "prices": {"SUBPAISA": 84.1024},
            }, fh)

        rc, out, err = _run_cli(size_mod, [
            "--plan", plan_p, "--config", cfg_p, "--portfolio", pf_p,
            "--prices", prices_p, "--instruments", cache_p,
            "--market", "IN", "--now", "2026-10-19 09:41", "--json",
        ])
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["sizing"]["entry"], 84.1025)
        self.assertTrue(doc["notes"], "Expected price-above-entry note for 4-decimal sub-paisa tick")
        self.assertIn("84.1025", doc["notes"][0])
        self.assertIn("84.1000", doc["notes"][0])

        # 2. NSE RELIANCE (tick 0.10): last 1335.42 rounds to 1335.40 == plan 1335.40 -> NO false note!
        inr_cache, _, _ = _write_inr_cache(self.tmp)
        rel_plan_p = os.path.join(self.tmp, "rel_plan.json")
        rel_prices_p = os.path.join(self.tmp, "rel_prices.json")
        with open(rel_plan_p, "w", encoding="utf-8") as fh:
            json.dump({
                "created_by": "trader-exit", "symbol": "RELIANCE", "side": "buy",
                "date": "2026-10-19", "market": "IN", "exchange": "NSE", "setup": "orb",
                "entry": 1335.40, "stop": 1331.50, "target": 1343.20, "tick_size": 0.10,
            }, fh)
        with open(rel_prices_p, "w", encoding="utf-8") as fh:
            json.dump({"as_of": "2026-10-19T04:10:00Z", "prices": {"RELIANCE": 1335.42}}, fh)
        rc_rel, out_rel, err_rel = _run_cli(size_mod, [
            "--plan", rel_plan_p, "--config", cfg_p, "--portfolio", pf_p,
            "--prices", rel_prices_p, "--instruments", inr_cache,
            "--market", "IN", "--exchange", "NSE", "--now", "2026-10-19 09:41", "--json",
        ])
        self.assertEqual(rc_rel, 0, err_rel)
        rel_doc = json.loads(out_rel)
        self.assertEqual(rel_doc["sizing"]["entry_from"], "plan")
        self.assertEqual(rel_doc["notes"], [])

    def test_audit_09_size_fails_closed_on_corrupt_unsafe_or_missing_tick(self):
        plan = {
            "symbol": "RELIANCE", "side": "buy", "date": "2026-10-19", "market": "IN",
            "exchange": "NSE", "entry": 1335.40, "stop": 1331.50, "target": 1343.20, "setup": "orb",
        }
        cfg = {"market": "IN", "budget": 100000, "max_loss_per_trade_inr": 2500}

        # 1. Corrupt JSON cache
        corrupt_p = os.path.join(self.tmp, "corrupt_size.json")
        with open(corrupt_p, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        with self.assertRaises(size_mod.SizeError):
            size_mod.size_trade(plan, cfg, today="2026-10-19", market="IN", instruments_path=corrupt_p)

        # 2. Unmarked Upstox/BOD unsafe cache
        unsafe_p = os.path.join(self.tmp, "unsafe_size.json")
        with open(unsafe_p, "w", encoding="utf-8") as fh:
            json.dump({
                "effective_date": "2026-10-19", "provider": "upstox",
                "instruments": {"RELIANCE": {"canonical_symbol": "RELIANCE", "exchange": "NSE", "segment": "NSE_EQ", "tick_size": 10.0, "lot_size": 1}},
            }, fh)
        with self.assertRaises(size_mod.SizeError):
            size_mod.size_trade(plan, cfg, today="2026-10-19", market="IN", instruments_path=unsafe_p)

        # 3. Exchange mismatch against NSE-only valid cache when BSE is requested
        cache_path, payload, _ = _write_inr_cache(self.tmp)
        nse_only = {k: v for k, v in payload.items() if k != "instruments_by_exchange"}
        nse_only["instruments"] = {k: v for k, v in payload["instruments"].items() if v.get("exchange") == "NSE"}
        nse_only_p = os.path.join(self.tmp, "nse_only.json")
        inst_mod.save_instrument_cache(nse_only, cache_path=nse_only_p, history_dir=None)
        bse_plan = dict(plan, exchange="BSE")
        with self.assertRaises(size_mod.SizeError):
            size_mod.size_trade(bse_plan, cfg, today="2026-10-19", market="IN", instruments_path=nse_only_p)

        # 4. Missing authoritative INR tick (no plan tick, nonexistent cache) must NOT fall back to 0.05
        orig_def = inst_mod.DEFAULT_CACHE_PATH
        try:
            inst_mod.DEFAULT_CACHE_PATH = os.path.join(self.tmp, "no_default.json")
            with self.assertRaises(size_mod.SizeError):
                size_mod.size_trade(plan, cfg, today="2026-10-19", market="IN")
        finally:
            inst_mod.DEFAULT_CACHE_PATH = orig_def

    def test_audit_10_risk_wrong_exchange_blocks_without_flat_fallback_and_corrupt_cache_blocks(self):
        _, payload, _ = _write_inr_cache(self.tmp)
        # NSE-only cache containing RELIANCE with tick=0.10; BSE order must BLOCK without flat lookup fallback
        nse_only = dict(payload)
        nse_only["instruments"] = {"RELIANCE": dict(payload["instruments_by_exchange"]["NSE"]["RELIANCE"])}
        nse_only["instruments_by_exchange"] = {"NSE": dict(payload["instruments_by_exchange"]["NSE"])}

        bse_order = {
            "symbol": "RELIANCE", "side": "buy", "qty": "5", "type": "limit", "time_in_force": "day",
            "order_class": "bracket", "limit_price": "1335.40",
            "stop_loss_stop_price": "1331.50", "take_profit_limit_price": "1343.20",
            "market": "IN", "exchange": "BSE",
        }
        r = risk_mod.check(
            [bse_order], {"market": "IN", "budget": 100000, "allow_off_hours": True},
            today="2026-10-19", market="IN", instruments_cache=nse_only,
        )
        self.assertEqual(r["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("tick_size" in reason and "BSE" in reason for reason in r["results"][0]["reasons"]))

        # Corrupt cache path fails closed with BLOCK reason in check()
        corrupt_p = os.path.join(self.tmp, "corrupt_risk.json")
        with open(corrupt_p, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        r_corrupt = risk_mod.check(
            [dict(bse_order, exchange="NSE")], {"market": "IN", "budget": 100000, "allow_off_hours": True},
            today="2026-10-19", market="IN", instruments_path=corrupt_p,
        )
        self.assertEqual(r_corrupt["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("instrument cache" in reason.lower() for reason in r_corrupt["results"][0]["reasons"]))

    def test_audit_11_risk_blocks_order_vs_plan_tick_mismatch(self):
        mismatched_order = {
            "symbol": "RELIANCE", "side": "buy", "qty": "5", "type": "limit", "time_in_force": "day",
            "order_class": "bracket", "limit_price": "1335.40",
            "stop_loss_stop_price": "1331.50", "take_profit_limit_price": "1343.20",
            "market": "IN", "tick_size": 0.10,
            "plan": {"entry": 1335.40, "stop": 1331.50, "target": 1343.20, "tick_size": 0.05},
        }
        orig_def = inst_mod.DEFAULT_CACHE_PATH
        try:
            inst_mod.DEFAULT_CACHE_PATH = os.path.join(self.tmp, "no_default_risk.json")
            r = risk_mod.check(
                [mismatched_order], {"market": "IN", "budget": 100000, "allow_off_hours": True},
                today="2026-10-19", market="IN",
            )
        finally:
            inst_mod.DEFAULT_CACHE_PATH = orig_def
        self.assertEqual(r["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("disagrees with plan tick_size" in reason for reason in r["results"][0]["reasons"]))

    def test_audit_12_ticket_plain_parts_preserves_4dec_subpaisa_precision(self):
        order_4dec = {
            "symbol": "USDINR26OCTFUT", "side": "buy", "qty": "1000", "type": "limit",
            "time_in_force": "day", "order_class": "bracket",
            "limit_price": "84.1000",
            "stop_loss_stop_price": "84.0000",
            "take_profit_limit_price": "84.3000",
            "market": "IN", "currency": "INR", "tick_size": 0.0025,
            "plan": {"entry": 84.1000, "stop": 84.0000, "target": 84.3000, "tick_size": 0.0025},
        }
        txt = ticket_mod.plain(order_4dec, market="IN")
        self.assertIn("limit 84.1000", txt)
        self.assertIn("stop-loss 84.0000", txt)
        self.assertIn("take-profit 84.3000", txt)
        self.assertIn("at 84.0000", txt)

    def test_review_01_size_cli_without_instruments_uses_plan_tick_when_default_cache_missing(self):
        plan = {
            "created_by": "trader-exit",
            "symbol": "RELIANCE",
            "side": "buy",
            "market": "IN",
            "exchange": "NSE",
            "currency": "INR",
            "date": "2026-10-19",
            "setup": "orb",
            "entry": 1335.40,
            "stop": 1331.50,
            "target": 1343.20,
            "tick_size": 0.10,
        }
        plan_p = os.path.join(self.tmp, "plan_in.json")
        cfg_p = os.path.join(self.tmp, "cfg_in.json")
        with open(plan_p, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        with open(cfg_p, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN", "budget": 100000}, fh)

        orig_def = inst_mod.DEFAULT_CACHE_PATH
        try:
            inst_mod.DEFAULT_CACHE_PATH = os.path.join(self.tmp, "nonexistent_default_cache.json")
            code, out, err = _run_cli(size_mod, [
                "--plan", plan_p, "--config", cfg_p, "--market", "IN",
                "--now", "2026-10-19 10:15", "--json",
            ])
            self.assertEqual(code, 0, err)
            doc = json.loads(out)
            self.assertEqual(len(doc["orders"]), 1)
            self.assertAlmostEqual(float(doc["orders"][0]["plan"]["tick_size"]), 0.10, places=4)

            # Explicit nonexistent --instruments must still fail closed
            code_bad, _, err_bad = _run_cli(size_mod, [
                "--plan", plan_p, "--config", cfg_p, "--market", "IN",
                "--now", "2026-10-19 10:15",
                "--instruments", os.path.join(self.tmp, "explicit_missing.json"),
            ])
            self.assertEqual(code_bad, 1)
            self.assertIn("not found", err_bad.lower())

            # Missing plan tick_size and missing default cache must NOT fall back to 0.05
            plan_no_tick = dict(plan)
            del plan_no_tick["tick_size"]
            plan_no_tick_p = os.path.join(self.tmp, "plan_no_tick.json")
            with open(plan_no_tick_p, "w", encoding="utf-8") as fh:
                json.dump(plan_no_tick, fh)
            code_nt, _, err_nt = _run_cli(size_mod, [
                "--plan", plan_no_tick_p, "--config", cfg_p, "--market", "IN",
                "--now", "2026-10-19 10:15",
            ])
            self.assertEqual(code_nt, 1)
            self.assertIn("tick_size", err_nt.lower())
        finally:
            inst_mod.DEFAULT_CACHE_PATH = orig_def

    def test_review_02_risk_market_exit_passes_when_symbol_missing_or_cache_stale(self):
        cfg = {"market": "IN", "budget": 100000, "allow_off_hours": True}
        port = {
            "cash": 50000.0,
            "positions": [{"symbol": "RELIANCE", "shares": 10, "lots": [{"shares": 10, "cost": 13000.0}]}],
        }
        prices = {"RELIANCE": 1350.0}
        mkt_exit = {
            "symbol": "RELIANCE", "side": "sell", "qty": "5",
            "type": "market", "time_in_force": "day", "market": "IN",
        }
        over_exit = {
            "symbol": "RELIANCE", "side": "sell", "qty": "20",
            "type": "market", "time_in_force": "day", "market": "IN",
        }
        limit_exit = {
            "symbol": "RELIANCE", "side": "sell", "qty": "5",
            "type": "limit", "limit_price": "1350.00", "time_in_force": "day", "market": "IN",
        }

        # (a) Valid cache missing RELIANCE: market exit PASSES; qty > held STILL BLOCKS as short; limit exit BLOCKS
        _, payload, _ = _write_inr_cache(self.tmp)
        tcs_only = dict(payload)
        tcs_rec = dict(payload["instruments"]["RELIANCE"], canonical_symbol="TCS", symbol="TCS")
        tcs_only["instruments"] = {"TCS": tcs_rec}
        tcs_only["instruments_by_exchange"] = {"NSE": {"TCS": dict(tcs_rec)}}

        res_mkt = risk_mod.check(
            [mkt_exit], cfg, portfolio=port, prices=prices,
            today="2026-10-19", market="IN", instruments_cache=tcs_only,
        )
        self.assertEqual(res_mkt["results"][0]["verdict"], "PASS", res_mkt["results"][0])

        res_over = risk_mod.check(
            [over_exit], cfg, portfolio=port, prices=prices,
            today="2026-10-19", market="IN", instruments_cache=tcs_only,
        )
        self.assertEqual(res_over["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("short" in r.lower() for r in res_over["results"][0]["reasons"]))

        res_lim_miss = risk_mod.check(
            [limit_exit], cfg, portfolio=port, prices=prices,
            today="2026-10-19", market="IN", instruments_cache=tcs_only,
        )
        self.assertEqual(res_lim_miss["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("tick_size" in r for r in res_lim_miss["results"][0]["reasons"]))

        # (b) Stale default instrument cache: market exit PASSES; limit exit BLOCKS
        stale_payload = dict(payload, effective_date="2020-01-01")
        stale_p = os.path.join(self.tmp, "stale_default_instruments.json")
        with open(stale_p, "w", encoding="utf-8") as fh:
            json.dump(stale_payload, fh)

        orig_def = inst_mod.DEFAULT_CACHE_PATH
        try:
            inst_mod.DEFAULT_CACHE_PATH = stale_p
            res_stale_mkt = risk_mod.check(
                [mkt_exit], cfg, portfolio=port, prices=prices,
                today="2026-10-19", market="IN",
            )
            self.assertEqual(res_stale_mkt["results"][0]["verdict"], "PASS", res_stale_mkt["results"][0])

            res_stale_lim = risk_mod.check(
                [limit_exit], cfg, portfolio=port, prices=prices,
                today="2026-10-19", market="IN",
            )
            self.assertEqual(res_stale_lim["results"][0]["verdict"], "BLOCK")
            self.assertTrue(any("tick_size" in r for r in res_stale_lim["results"][0]["reasons"]))

            # (c) Missing default instrument cache & no order/plan tick_size: market exit PASSES; limit exit BLOCKS
            inst_mod.DEFAULT_CACHE_PATH = os.path.join(self.tmp, "no_default_risk_exit.json")
            res_no_cache_mkt = risk_mod.check(
                [mkt_exit], cfg, portfolio=port, prices=prices,
                today="2026-10-19", market="IN",
            )
            self.assertEqual(res_no_cache_mkt["results"][0]["verdict"], "PASS", res_no_cache_mkt["results"][0])

            res_no_cache_lim = risk_mod.check(
                [limit_exit], cfg, portfolio=port, prices=prices,
                today="2026-10-19", market="IN",
            )
            self.assertEqual(res_no_cache_lim["results"][0]["verdict"], "BLOCK")
            self.assertTrue(any("tick_size" in r for r in res_no_cache_lim["results"][0]["reasons"]))
        finally:
            inst_mod.DEFAULT_CACHE_PATH = orig_def

    def test_review_03_offline_get_instruments_exchange_isolation_and_list_wrapper(self):
        # 1. instruments_by_exchange with NSE only must return 0 records for BSE (no flat fallback)
        dir_by_exch = os.path.join(self.tmp, "off_by_exch")
        os.makedirs(dir_by_exch, exist_ok=True)
        _, payload, _ = _write_inr_cache(dir_by_exch)
        nse_only = dict(payload)
        nse_only["instruments"] = {"RELIANCE": dict(payload["instruments_by_exchange"]["NSE"]["RELIANCE"])}
        nse_only["instruments_by_exchange"] = {"NSE": dict(payload["instruments_by_exchange"]["NSE"])}
        with open(os.path.join(dir_by_exch, "instruments.json"), "w", encoding="utf-8") as fh:
            json.dump(nse_only, fh)
        off_a = data_in.OfflineMarketDataProvider(dir_by_exch)
        self.assertGreater(off_a.get_instruments(exchange="NSE")["count"], 0)
        bse_a = off_a.get_instruments(exchange="BSE")
        self.assertEqual(bse_a["count"], 0)
        self.assertEqual(bse_a["instruments"], [])

        # 2. Flat cache with NSE-tagged record queried for BSE must return 0 records
        dir_flat = os.path.join(self.tmp, "off_flat")
        os.makedirs(dir_flat, exist_ok=True)
        flat_payload = {
            "effective_date": "2026-10-19",
            "instruments": {
                "RELIANCE": {
                    "symbol": "RELIANCE", "exchange": "NSE",
                    "tick_size": 0.10, "tick_size_unit": "INR",
                },
            },
        }
        with open(os.path.join(dir_flat, "instruments.json"), "w", encoding="utf-8") as fh:
            json.dump(flat_payload, fh)
        off_b = data_in.OfflineMarketDataProvider(dir_flat)
        self.assertEqual(off_b.get_instruments(exchange="NSE")["count"], 1)
        self.assertEqual(off_b.get_instruments(exchange="BSE")["count"], 0)

        # 3. {"instruments": [...]} list-under-dict keeps working with provenance & exchange isolation
        dir_list = os.path.join(self.tmp, "off_list_dict")
        os.makedirs(dir_list, exist_ok=True)
        list_doc = {
            "instruments": [
                {"symbol": "RELIANCE", "exchange": "NSE", "tick_size": 0.10, "tick_size_unit": "INR"},
            ],
        }
        with open(os.path.join(dir_list, "instruments.json"), "w", encoding="utf-8") as fh:
            json.dump(list_doc, fh)
        off_c = data_in.OfflineMarketDataProvider(dir_list)
        res_list_nse = off_c.get_instruments(exchange="NSE")
        self.assertEqual(res_list_nse["count"], 1)
        self.assertAlmostEqual(res_list_nse["instruments"][0]["tick_size"], 0.10, places=4)
        self.assertEqual(off_c.get_instruments(exchange="BSE")["count"], 0)

        # Unmarked Upstox list-under-dict still fails closed
        dir_bad = os.path.join(self.tmp, "off_list_bad")
        os.makedirs(dir_bad, exist_ok=True)
        with open(os.path.join(dir_bad, "instruments.json"), "w", encoding="utf-8") as fh:
            json.dump({"provider": "upstox", "instruments": [{"symbol": "RELIANCE", "exchange": "NSE", "tick_size": 10.0}]}, fh)
        with self.assertRaises(data_in.DataError):
            data_in.OfflineMarketDataProvider(dir_bad).get_instruments(exchange="NSE")

    def test_review_04_entry_cli_explicit_missing_instruments_fails_closed(self):
        chart_p = _make_intraday_chart(self.tmp, symbol="RELIANCE.NS", session_date="2026-10-19")
        cfg_p = os.path.join(self.tmp, "cfg_entry.json")
        with open(cfg_p, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN"}, fh)

        code_bad, _, err_bad = _run_cli(entry_mod, [
            "check", "RELIANCE.NS", "--offline", chart_p, "--config", cfg_p,
            "--market", "IN", "--now", "2026-10-19 09:40",
            "--instruments", os.path.join(self.tmp, "nonexistent_entry_inst.json"),
            "--json",
        ])
        self.assertEqual(code_bad, 1)
        self.assertIn("not found", err_bad.lower())

        # Omitting --instruments preserves normal behavior
        code_ok, out_ok, err_ok = _run_cli(entry_mod, [
            "check", "RELIANCE.NS", "--offline", chart_p, "--config", cfg_p,
            "--market", "IN", "--now", "2026-10-19 09:40",
            "--json",
        ])
        self.assertEqual(code_ok, 0, err_ok)
        res = json.loads(out_ok)
        self.assertEqual(res["symbol"], "RELIANCE.NS")

    def test_subpaisa_ticket_single_limit_stop_stop_limit_and_fill_preserve_4dec(self):
        base = {
            "symbol": "USDINR26OCTFUT", "side": "buy", "qty": "1",
            "time_in_force": "day", "market": "IN", "currency": "INR",
            "tick_size": 0.0025,
        }
        # Single limit (with and without explicit tick_size)
        lim = dict(base, type="limit", limit_price="84.1025")
        self.assertIn("limit order at ₹84.1025", ticket_mod.plain(lim, market="IN"))
        lim_no_tick = {
            "symbol": "USDINR26OCTFUT", "side": "buy", "qty": "1",
            "type": "limit", "limit_price": "84.1025", "time_in_force": "day", "market": "IN",
        }
        self.assertIn("limit order at ₹84.1025", ticket_mod.plain(lim_no_tick, market="IN"))

        # Authoritative 4-decimal tick preserves 4 decimals even when price ends in .10
        lim_zero = dict(base, type="limit", limit_price="84.10")
        self.assertIn("limit order at ₹84.1000", ticket_mod.plain(lim_zero, market="IN"))

        # Single stop & single stop_limit
        stp = dict(base, type="stop", stop_price="84.0025")
        self.assertIn("stop order at ₹84.0025", ticket_mod.plain(stp, market="IN"))
        stplim = dict(base, type="stop_limit", stop_price="84.0025", limit_price="84.1025")
        self.assertIn("stop ₹84.0025, limit ₹84.1025", ticket_mod.plain(stplim, market="IN"))

        # Ordinary 2-decimal instruments preserve concise formatting
        us_lim = {"symbol": "KO", "side": "sell", "qty": "1", "type": "limit", "limit_price": "95", "time_in_force": "gtc"}
        self.assertEqual(ticket_mod.plain(us_lim), "SELL 1 share of KO, limit order at $95, good until canceled")
        in_eq = {"symbol": "RELIANCE", "side": "buy", "qty": "2", "type": "limit", "limit_price": "1335.40",
                 "time_in_force": "day", "market": "IN", "currency": "INR", "tick_size": 0.10}
        self.assertIn("limit order at ₹1,335.40", ticket_mod.plain(in_eq, market="IN"))

        # cmd_fill & log_fill preserve 4-decimal sub-paisa fill prices
        pf_p = os.path.join(self.tmp, "pf_subpaisa.json")
        st_p = os.path.join(self.tmp, "broker_subpaisa.json")
        cfg_p = os.path.join(self.tmp, "cfg_subpaisa.json")
        with open(pf_p, "w", encoding="utf-8") as fh:
            json.dump({"cash": 100000.0, "currency": "INR", "positions": []}, fh)
        with open(cfg_p, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN"}, fh)
        code_f, out_f, err_f = _run_cli(ticket_mod, [
            "fill", "--symbol", "USDINR26OCTFUT", "--side", "buy", "--qty", "1", "--price", "84.1025",
            "--portfolio", pf_p, "--state", st_p, "--config", cfg_p, "--market", "IN", "--now", "2026-10-19 10:15",
        ])
        self.assertEqual(code_f, 0, err_f)
        self.assertIn("at ₹84.1025", out_f)
        with open(st_p, encoding="utf-8") as fh:
            st_doc = json.load(fh)
        self.assertIn("at 84.1025", st_doc["orders"][-1]["plain"])

    def test_subpaisa_risk_and_exit_reports_preserve_4dec_precision(self):
        # 1. Exit plan compute() errors & describe() preserve 4-decimal precision
        with self.assertRaises(exit_mod.PlanError) as ectx:
            exit_mod.compute(
                84.1025, "price", exit_mod.Decimal("84.1050"), "r", exit_mod.Decimal("2"),
                market="IN", tick_size=exit_mod.Decimal("0.0025"),
            )
        self.assertIn("₹84.1050", str(ectx.exception))
        self.assertIn("₹84.1025", str(ectx.exception))

        plan_4d = {
            "created_by": "trader-exit",
            "symbol": "USDINR26OCTFUT",
            "side": "buy",
            "market": "IN",
            "currency": "INR",
            "tick_size": 0.0025,
            "date": "2026-10-19",
            "setup": "orb",
            "interval": "5m",
            "entry": 84.1025,
            "stop": 84.0000,
            "stop_label": "your price",
            "target": 84.3075,
            "target_label": "2R (two times the risk)",
            "risk_per_share": 0.1025,
            "reward_risk": 2.0,
            "flat_by": "15:20",
        }
        exit_text = "\n".join(exit_mod.describe(plan_4d))
        self.assertIn("₹84.1025", exit_text)
        self.assertIn("₹84.0000", exit_text)
        self.assertIn("₹84.3075", exit_text)
        self.assertIn("Risk a share  ₹0.1025", exit_text)

        # Ordinary 2-decimal Indian plan retains 2 decimals
        plan_2d = dict(plan_4d, symbol="RELIANCE", tick_size=0.10, entry=1335.40, stop=1320.00, target=1366.20, risk_per_share=15.40)
        exit_2d = "\n".join(exit_mod.describe(plan_2d))
        self.assertIn("₹1,335.40", exit_2d)
        self.assertIn("₹1,320.00", exit_2d)

        # 2. Risk check & report preserve 4-decimal precision in entry_ref, info, warnings, and reasons
        cache_4d = {
            "provider": "upstox",
            "effective_date": "2026-10-19",
            "instruments": {
                "USDINR26OCTFUT": {
                    "canonical_symbol": "USDINR26OCTFUT", "exchange": "NSE", "asset_type": "FUTURES",
                    "tick_size": 0.0025, "tick_size_unit": "INR", "lot_size": 1000, "fo_lot_size": 1000,
                    "expiry_date": "2026-10-29",
                },
            },
        }
        order_4d = {
            "symbol": "USDINR26OCTFUT", "side": "buy", "qty": "1000", "type": "limit",
            "time_in_force": "day", "order_class": "bracket", "asset_type": "FUTURES",
            "lot_size": 1000, "expiry": "2026-10-29", "market": "IN", "currency": "INR",
            "tick_size": 0.0025,
            "limit_price": "84.1025", "stop_loss_stop_price": "84.0000", "take_profit_limit_price": "84.3075",
            "plan": {"entry": 84.1025, "stop": 83.9925, "target": 84.3075, "tick_size": 0.0025},
        }
        cfg_in = {
            "market": "IN", "budget": 500000, "max_order_inr": 500000,
            "max_loss_per_trade_inr": 5000, "allow_off_hours": True, "min_reward_risk": 5.0,
        }
        res_4d = risk_mod.check(
            [order_4d], cfg_in, portfolio={"cash": 500000.0, "positions": []},
            today="2026-10-19", market="IN", instruments_cache=cache_4d,
        )
        r0 = res_4d["results"][0]
        self.assertEqual(r0["entry_ref"], 84.1025)
        rep_4d = risk_mod.report(res_4d)
        self.assertIn("entry 84.1025 (the limit price), stop 84.0000, target 84.3075: risk 0.1025 a share", rep_4d)
        self.assertIn("bracket stop 84.0000 differs from the plan's stop 83.9925", rep_4d)
        self.assertIn("(target 84.3075, entry 84.1025, stop 84.0000)", rep_4d)

        # Bad stop >= entry error preserves 4-decimal prices
        bad_stop = dict(order_4d, stop_loss_stop_price="84.1050", plan={"entry": 84.1025, "stop": 84.1050, "target": 84.3075, "tick_size": 0.0025})
        res_bad = risk_mod.check(
            [bad_stop], cfg_in, portfolio={"cash": 500000.0, "positions": []},
            today="2026-10-19", market="IN", instruments_cache=cache_4d,
        )
        self.assertIn("stop 84.1050 must be below the entry 84.1025", " ".join(res_bad["results"][0]["reasons"]))


if __name__ == "__main__":
    unittest.main()
