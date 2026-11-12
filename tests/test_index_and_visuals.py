#!/usr/bin/env python3
"""Tests for first-class Indian index scanning, non-equity safety guards, and opt-in SVG visuals."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("trader-alerts", "trader-scanner", "trader-entry", "trader-exit", "trader-budget", "trader-risk", "trader-broker"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import symbols_in  # type: ignore  # noqa: E402
import instruments  # type: ignore  # noqa: E402
import data_in  # type: ignore  # noqa: E402
import visuals  # type: ignore  # noqa: E402
import scan  # type: ignore  # noqa: E402
import entry  # type: ignore  # noqa: E402
import exit as exit_mod  # type: ignore  # noqa: E402
import size as size_mod  # type: ignore  # noqa: E402
import check as risk_check  # type: ignore  # noqa: E402
import ticket  # type: ignore  # noqa: E402


def _make_bars_doc(symbol="NIFTY", count=60, start_price=24000.0, step=15.0, volume=0,
                   interval="1d", exchange="NSE", provider="upstox", tier="CURRENT"):
    base_ts = 1760000000
    sec = 86400 if interval == "1d" else (300 if interval == "5m" else 900)
    bars = []
    px = start_price
    for i in range(count):
        o = round(px, 2)
        c = round(px + step, 2)
        h = round(max(o, c) + 8.0, 2)
        l = round(min(o, c) - 6.0, 2)
        ts = base_ts + i * sec
        bars.append({
            "t": ts,
            "date": "2026-10-%02d" % (1 + (i % 28)),
            "start": "09:%02d" % ((15 + i * 5) % 60),
            "o": o,
            "h": h,
            "l": l,
            "c": c,
            "v": volume,
        })
        px = c
    return {
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "provider": provider,
        "source": provider,
        "tier": tier,
        "data_tier": tier,
        "data_status": tier,
        "is_live": (tier == "CURRENT"),
        "last_data_timestamp": "2026-10-19 15:25 IST",
        "bars": bars,
    }


class TestIndexResolution(unittest.TestCase):
    EXPECTED_INDICES = {
        "NIFTY": ("NSE", "NSE_INDEX|Nifty 50", "^NSEI", True),
        "BANKNIFTY": ("NSE", "NSE_INDEX|Nifty Bank", "^NSEBANK", True),
        "FINNIFTY": ("NSE", "NSE_INDEX|Nifty Fin Service", "NIFTY_FIN_SERVICE.NS", True),
        "MIDCPNIFTY": ("NSE", "NSE_INDEX|NIFTY MID SELECT", "NIFTY_MID_SELECT.NS", True),
        "NIFTYNXT50": ("NSE", "NSE_INDEX|Nifty Next 50", "^NSMIDCP", True),
        "NIFTY100": ("NSE", "NSE_INDEX|Nifty 100", "^CNX100", False),
        "NIFTY200": ("NSE", "NSE_INDEX|Nifty 200", "^CNX200", False),
        "NIFTY500": ("NSE", "NSE_INDEX|Nifty 500", "^CRSLDX", False),
        "SENSEX": ("BSE", "BSE_INDEX|SENSEX", "^BSESN", True),
        "BANKEX": ("BSE", "BSE_INDEX|BANKEX", "BSE-BANK.BO", True),
    }

    def test_all_ten_benchmark_indices_metadata_and_aliases(self):
        self.assertEqual(set(symbols_in.BENCHMARK_INDICES), set(self.EXPECTED_INDICES.keys()))
        for canon, (exch, ukey, ysym, has_deriv) in self.EXPECTED_INDICES.items():
            meta = symbols_in.get_index_metadata(canon)
            self.assertIsNotNone(meta, canon)
            self.assertEqual(meta["exchange"], exch)
            self.assertEqual(meta["upstox_key"], ukey)
            self.assertEqual(meta["yahoo"], ysym)
            self.assertEqual(meta["has_derivatives"], has_deriv)
            self.assertEqual(symbols_in.to_yahoo_symbol(canon), ysym)
            self.assertTrue(symbols_in.is_index_symbol(canon))

        aliases = {
            "NIFTY 50": "NIFTY",
            "NIFTY BANK": "BANKNIFTY",
            "NIFTY FINANCIAL SERVICES": "FINNIFTY",
            "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
            "NIFTY NEXT 50": "NIFTYNXT50",
            "NIFTY 100": "NIFTY100",
            "NIFTY 200": "NIFTY200",
            "NIFTY 500": "NIFTY500",
            "BSE SENSEX": "SENSEX",
            "BSE BANKEX": "BANKEX",
        }
        for raw, expected in aliases.items():
            self.assertTrue(symbols_in.is_index_symbol(raw), raw)
            self.assertEqual(symbols_in.normalize_index_name(raw), expected)

    def test_upstox_key_resolution_and_bod_metadata(self):
        prov = data_in.UpstoxMarketDataProvider(analytics_token="dummy-token-for-unit-test")
        for canon, (exch, ukey, _, _) in self.EXPECTED_INDICES.items():
            resolved = prov.resolve_instrument_key(canon, exchange=exch)
            self.assertEqual(resolved[0], ukey)

        # Dynamic BOD index record resolution (e.g. NIFTY IT) and failure on unknown
        prov._instruments_by_exch["NSE"] = []
        prov._instruments_by_exch["BSE"] = []
        prov._key_by_symbol[("NSE", "NIFTY IT")] = "NSE_INDEX|Nifty IT"
        self.assertEqual(prov.resolve_instrument_key("NIFTY IT", exchange="NSE")[0], "NSE_INDEX|Nifty IT")
        with self.assertRaises(data_in.InvalidSymbolError):
            prov.resolve_instrument_key("UNKNOWN_FAKE_INDEX", exchange="NSE")
        with self.assertRaises(data_in.SymbolNotFoundError):
            prov.resolve_instrument_key("UNKNOWNINDEX", exchange="NSE")

    def test_instrument_master_marks_indices_non_tradable_equity(self):
        raw_records = [
            {
                "segment": "NSE_EQ",
                "exchange": "NSE",
                "trading_symbol": "RELIANCE",
                "name": "Reliance Industries Ltd.",
                "instrument_key": "NSE_EQ|INE002A01018",
                "tick_size": 5.0,
                "tick_size_unit": "paise",
                "lot_size": 1,
            },
            {
                "segment": "NSE_INDEX",
                "exchange": "NSE",
                "trading_symbol": "NIFTY",
                "name": "Nifty 50",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "tick_size": None,
                "tick_size_unit": "INR",
                "lot_size": None,
            },
            {
                "segment": "BSE_INDEX",
                "exchange": "BSE",
                "trading_symbol": "SENSEX",
                "name": "SENSEX",
                "instrument_key": "BSE_INDEX|SENSEX",
                "tick_size": None,
                "tick_size_unit": "INR",
                "lot_size": None,
            },
        ]
        master = instruments.build_master_from_provider_records(raw_records, provider="test")
        nse_inst = master["instruments_by_exchange"]["NSE"]
        bse_inst = master["instruments_by_exchange"]["BSE"]
        self.assertTrue(nse_inst["RELIANCE"]["is_tradable_equity"])
        self.assertFalse(nse_inst["NIFTY"]["is_tradable_equity"])
        self.assertEqual(nse_inst["NIFTY"]["instrument_type"], "INDEX")
        self.assertFalse(bse_inst["SENSEX"]["is_tradable_equity"])
        self.assertEqual(bse_inst["SENSEX"]["upstox_key"], "BSE_INDEX|SENSEX")


class _MockIndexProvider(object):
    provider_name = "upstox"

    def __init__(self, fail_syms=None, short_syms=None):
        self.fail_syms = set(fail_syms or [])
        self.short_syms = set(short_syms or [])
        self.calls = []

    def get_historical_data(self, symbol, range_str="3mo", interval="1d", exchange="NSE", now=None):
        self.calls.append(("1d", symbol, exchange))
        if symbol in self.fail_syms:
            raise data_in.MissingDataError("No historical data on upstox for %s" % symbol)
        cnt = 1 if symbol in self.short_syms else 60
        step = -12.0 if symbol in ("BANKNIFTY", "FINNIFTY") else 18.0
        return _make_bars_doc(symbol=symbol, count=cnt, start_price=24000.0, step=step,
                              volume=0, interval="1d", exchange=exchange, provider="upstox", tier="CURRENT")

    def get_intraday_data(self, symbol, interval="5m", range_str="5d", exchange="NSE", now=None):
        self.calls.append((interval, symbol, exchange))
        if symbol in self.fail_syms:
            raise data_in.MissingDataError("No intraday data on upstox for %s" % symbol)
        return _make_bars_doc(symbol=symbol, count=40, start_price=24500.0, step=5.0,
                              volume=0, interval=interval, exchange=exchange, provider="upstox", tier="CURRENT")

    def get_quote(self, symbol, exchange="NSE", now=None):
        return {"symbol": symbol, "price": 24800.0, "previous_close": 24600.0, "is_live": False}


class TestIndexScannerAndSafety(unittest.TestCase):
    def test_scan_indices_single_multi_and_opt_in_chart(self):
        prov = _MockIndexProvider(short_syms=["MIDCPNIFTY"])
        with tempfile.TemporaryDirectory() as tmp:
            out_json = os.path.join(tmp, "idx.json")
            svg_path = os.path.join(tmp, "chart.svg")
            buf = io.StringIO()
            with mock.patch.object(scan.data_in, "get_provider", return_value=prov):
                with redirect_stdout(buf):
                    rc = scan.main([
                        "indices",
                        "--symbols", "NIFTY 50,BANKNIFTY,SENSEX,MIDCPNIFTY",
                        "--interval", "1d",
                        "--out", out_json,
                        "--json",
                    ])
            self.assertEqual(rc, 0)
            # Opt-in rule: no --chart -> no SVG created
            self.assertFalse(os.path.exists(svg_path))
            doc = json.loads(buf.getvalue())
            self.assertEqual(doc["mode"], "indices")
            self.assertEqual(len(doc["indices"]), 4)
            by_sym = {r["symbol"]: r for r in doc["indices"]}
            self.assertEqual(by_sym["NIFTY"]["exchange"], "NSE")
            self.assertEqual(by_sym["SENSEX"]["exchange"], "BSE")
            self.assertEqual(by_sym["NIFTY"]["trend"], "uptrend")
            self.assertEqual(by_sym["NIFTY"]["ema_alignment"], "bullish_aligned")
            self.assertIsNone(by_sym["NIFTY"]["vwap"])
            self.assertIn("N/A", by_sym["NIFTY"]["vwap_note"])
            self.assertIsNotNone(by_sym["NIFTY"]["rsi_14"])
            self.assertEqual(by_sym["BANKNIFTY"]["trend"], "downtrend")
            self.assertEqual(by_sym["MIDCPNIFTY"]["status"], "limited")
            self.assertIsNone(by_sym["MIDCPNIFTY"]["rsi_14"])
            self.assertEqual(doc["breadth"]["regime"], "mixed_divergent")
            self.assertEqual(by_sym["SENSEX"]["is_tradable_equity"], False)

            # Now request explicit --chart and verify zero extra provider calls per bar
            calls_before = len(prov.calls)
            with mock.patch.object(scan.data_in, "get_provider", return_value=prov):
                with redirect_stdout(io.StringIO()):
                    rc2 = scan.main([
                        "indices", "--symbols", "NIFTY,BANKNIFTY", "--interval", "5m",
                        "--no-write", "--chart", svg_path, "--json",
                    ])
            self.assertEqual(rc2, 0)
            self.assertEqual(len(prov.calls) - calls_before, 2)  # 1 fetch per index, 0 extra chart fetches
            self.assertTrue(os.path.exists(svg_path))
            with open(svg_path, encoding="utf-8") as fh:
                svg = fh.read()
            self.assertIn("<svg", svg)
            self.assertIn("Source: upstox", svg)
            self.assertIn("Timeframe: 5m", svg)

    def test_scan_indices_provider_failure_no_silent_fallback(self):
        prov = _MockIndexProvider(fail_syms=["BANKEX"])
        with mock.patch.object(scan.data_in, "get_provider", return_value=prov):
            with redirect_stdout(io.StringIO()) as buf:
                rc = scan.main(["indices", "--symbols", "BANKEX", "--interval", "1d", "--no-write", "--json"])
        self.assertEqual(rc, 1)
        doc = json.loads(buf.getvalue())
        self.assertIn("BANKEX", doc["failed"])
        self.assertEqual(doc["indices"][0]["status"], "unavailable")

    def test_spot_indices_blocked_from_equity_sizing_risk_and_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            pf_path = os.path.join(tmp, "portfolio.json")
            orders_path = os.path.join(tmp, "orders.json")
            risk_path = os.path.join(tmp, "risk.json")
            state_path = os.path.join(tmp, "broker.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "market": "IN", "exchange": "NSE", "currency": "INR", "paper": True,
                    "budget": 100000, "max_risk_per_trade": 2000, "allow_futures": True, "allow_options": True,
                }, fh)

            # 1. size.py refuses spot index plan
            plan_spot = {
                "symbol": "NIFTY", "side": "buy", "asset_type": "INDEX", "date": "2026-10-19",
                "entry": 25000.0, "stop": 24900.0, "target": 25200.0, "market": "IN", "exchange": "NSE",
            }
            with self.assertRaises(size_mod.SizeError):
                size_mod.size_trade(plan_spot, {"budget": 100000, "max_risk_per_trade": 2000, "market": "IN"}, market="IN")

            # 2. risk/check.py blocks spot index equity order while allowing derivative
            spot_orders = {
                "created_by": "test", "market": "IN", "date": "2026-10-19",
                "orders": [{
                    "symbol": "NIFTY", "side": "buy", "qty": 1, "type": "limit",
                    "limit_price": 25000.0, "stop_loss": 24900.0, "take_profit": 25200.0,
                    "asset_type": "INDEX", "market": "IN", "exchange": "NSE", "tick_size": 0.05,
                }],
            }
            with open(orders_path, "w", encoding="utf-8") as fh:
                json.dump(spot_orders, fh)
            with redirect_stdout(io.StringIO()):
                rc = risk_check.main([
                    "--orders", orders_path, "--config", cfg_path, "--portfolio", pf_path,
                    "--out", risk_path, "--state", state_path, "--now", "2026-10-19 10:15",
                ])
            self.assertEqual(rc, 1)
            with open(risk_path, encoding="utf-8") as fh:
                rdoc = json.load(fh)
            self.assertEqual(rdoc["pass"], 0)
            self.assertEqual(rdoc["block"], 1)
            reasons = rdoc["results"][0]["reasons"]
            self.assertTrue(any("spot index" in r.lower() for r in reasons), reasons)

            # 3. ticket.py apply_fill refuses spot index equity fill, allows derivative F&O fill
            with self.assertRaises(ticket.TicketError):
                ticket.apply_fill(pf_path, {"budget": 100000, "market": "IN"}, "NIFTY", "buy", 1, 25000.0, "2026-10-19", market="IN")
            pf = ticket.apply_fill(pf_path, {"budget": 100000, "market": "IN"}, "NIFTY26NOVFUT", "buy", 25, 25050.0, "2026-10-19", market="IN")
            self.assertEqual(pf["positions"][0]["ticker"], "NIFTY26NOVFUT")

            # 4. entry.py & exit.py tag spot index as INDEX / analytical_only without crashing
            idx_bars = [
                {"t": 1760845500 + i * 300, "o": 24800.0 + i, "h": 24815.0 + i, "l": 24795.0 + i, "c": 24810.0 + i, "v": 0}
                for i in range(10)
            ]
            chart_doc = {"symbol": "NIFTY", "exchange": "NSE", "market": "IN", "asset_type": "INDEX", "bars": idx_bars}
            now_dt = entry.parse_now("2026-10-19 10:15", market="IN")
            ev = entry.evaluate(chart_doc, now_dt, setup="orb", or_minutes=15, interval="5m", market="IN")
            self.assertEqual(ev.get("asset_type"), "INDEX")
            self.assertFalse(ev.get("is_tradable_equity"))
            self.assertTrue(ev.get("analytical_only"))
            edoc = entry.entry_doc(ev, now_dt)
            self.assertEqual(edoc.get("asset_type"), "INDEX")
            self.assertFalse(edoc.get("is_tradable_equity"))

            entry_file = os.path.join(tmp, "entry_idx.json")
            plan_file = os.path.join(tmp, "plan_idx.json")
            edoc["status"] = "triggered"
            edoc["price"] = 24815.0
            edoc["time"] = "9:35 AM"
            edoc["opening_range"] = {"high": 24810.0, "low": 24795.0, "minutes": 15}
            with open(entry_file, "w", encoding="utf-8") as fh:
                json.dump(edoc, fh)
            with redirect_stdout(io.StringIO()):
                rc_exit = exit_mod.main([
                    "plan", "--from", entry_file, "--market", "IN", "--stop", "or-mid",
                    "--now", "2026-10-19 10:15", "--write", plan_file,
                ])
            self.assertEqual(rc_exit, 0)
            with open(plan_file, encoding="utf-8") as fh:
                pdoc = json.load(fh)
            self.assertEqual(pdoc.get("asset_type"), "INDEX")
            self.assertFalse(pdoc.get("is_tradable_equity"))
            self.assertTrue(pdoc.get("analytical_only"))


class TestVisualsOptInAndProvenance(unittest.TestCase):
    def test_candlestick_and_entry_exit_preserve_provenance_and_omit_zero_vwap(self):
        doc = _make_bars_doc(symbol="NIFTY", count=30, volume=0, interval="15m", provider="upstox", tier="CURRENT")
        svg = visuals.render_candlestick_svg(doc, ema_periods=(9, 20), show_vwap=True)
        self.assertIn("<svg", svg)
        self.assertIn("Source: upstox", svg)
        self.assertIn("Timeframe: 15m", svg)
        self.assertIn("CURRENT", svg)
        self.assertIn("2026-10-19 15:25 IST", svg)
        self.assertIn("VWAP: N/A (zero volume)", svg)
        self.assertNotIn("#c084fc", svg)  # spot index volume=0 must not plot VWAP line

        # With positive volume, VWAP is rendered
        doc_vol = _make_bars_doc(symbol="RELIANCE", count=30, volume=10000, interval="5m", provider="yahoo", tier="DELAYED")
        svg_ee = visuals.render_entry_exit_svg(doc_vol, plan={
            "entry": 24150.0, "stop": 24050.0, "target": 24350.0,
        })
        self.assertIn("#c084fc", svg_ee)
        self.assertIn("Entry", svg_ee)
        self.assertIn("Stop", svg_ee)
        self.assertIn("Target", svg_ee)
        self.assertIn("DELAYED", svg_ee)

        # Empty bars raises cleanly without inventing candles
        with self.assertRaises(visuals.VisualError):
            visuals.render_candlestick_svg({"symbol": "EMPTY", "bars": []})

    def test_all_other_chart_renderers(self):
        opt_svg = visuals.render_options_payoff_svg({
            "symbol": "NIFTY", "underlying_price": 24500.0, "provider": "upstox", "data_tier": "SIMULATED",
            "legs": [{"option_type": "CE", "side": "buy", "strike": 24500.0, "premium": 150.0, "qty": 75}],
            "greeks": {"delta": 0.52, "theta": -18.5, "iv": 0.142},
        })
        self.assertIn("Options Expiration Payoff", opt_svg)
        self.assertIn("SIMULATED", opt_svg)

        pf_svg = visuals.render_portfolio_svg(
            {"currency": "INR", "cash": 50000.0, "positions": [{"ticker": "RELIANCE", "shares": 10, "lots": [{"shares": 10, "cost": 28000.0}]}]},
            prices_doc={"quotes": {"RELIANCE": {"price": 2900.0}}, "provider": "upstox", "data_tier": "CURRENT"},
        )
        self.assertIn("Portfolio Allocation", pf_svg)
        self.assertIn("RELIANCE", pf_svg)

        risk_svg = visuals.render_risk_drawdown_svg(
            risk_doc={"market": "IN", "orders": [{"symbol": "RELIANCE", "qty": 10, "max_loss": 500.0}]},
            broker_doc={"fills": [{"date": "2026-10-19", "realized_pnl": 1200.0}, {"date": "2026-10-20", "realized_pnl": -400.0}]},
            config_doc={"budget": 100000.0, "max_daily_loss": 2000.0, "currency": "INR"},
        )
        self.assertIn("Risk", risk_svg)

        bt_svg = visuals.render_backtest_svg({
            "provider": "upstox",
            "data_tier": "HISTORICAL",
            "plan": {"symbol": "RELIANCE", "entry": 2850.0, "stop": 2830.0, "target": 2890.0},
            "mc": {
                "band_paths": 10000,
                "seed": 42,
                "target_pct": 54.0,
                "stop_pct": 38.0,
                "mean_r": 0.42,
                "pool_sessions": 60,
                "band_times": ["09:30", "12:00", "15:15"],
                "bands": [
                    [2835.0, 2845.0, 2850.0, 2858.0, 2870.0],
                    [2828.0, 2842.0, 2862.0, 2878.0, 2895.0],
                    [2830.0, 2850.0, 2875.0, 2890.0, 2910.0],
                ],
            },
        })
        self.assertIn("Time Machine Replay", bt_svg)

        fa_svg = visuals.render_fundamentals_svg({
            "symbol": "RELIANCE", "currency": "INR", "provider": "yahoo",
            "periods": [
                {"period": "Q1 FY26", "revenue": 220000, "net_income": 18000, "operating_margin": 14.5},
                {"period": "Q2 FY26", "revenue": 235000, "net_income": 19500, "operating_margin": 15.1},
            ],
        })
        self.assertIn("Fundamental Trends", fa_svg)

        news_svg = visuals.render_news_timeline_svg({
            "symbol": "RELIANCE", "provider": "news_in",
            "items": [
                {"published_at": "2026-10-19 09:30 IST", "category": "corporate", "title": "Q2 results beat estimates"},
                {"published_at": "2026-10-18 16:00 IST", "category": "analyst", "title": "Brokerage maintains Outperform"},
            ],
        })
        self.assertIn("Catalyst &amp; Filing Timeline", news_svg)
        self.assertIn("2026-10-19 09:30", news_svg)


if __name__ == "__main__":
    unittest.main()
