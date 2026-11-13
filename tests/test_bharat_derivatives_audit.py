"""Regression tests for SENSEX, FINNIFTY, NIFTYNXT50, and general F&O contract resolution."""

import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timezone, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load

entry = _load.load("trader-entry", "entry")
alerts = _load.load("trader-alerts", "alerts")
calendar_in = _load.load("trader-alerts", "calendar_in")
instruments = _load.load("trader-alerts", "instruments")
symbols_in = _load.load("trader-alerts", "symbols_in")
data_in = _load.load("trader-alerts", "data_in")


def _ist_dt(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def _ist_ts(y, m, d, hh, mm):
    return int(_ist_dt(y, m, d, hh, mm).timestamp())


def _utc_from_ts(ts):
    return datetime.datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _make_5m_chart(symbol, bars, prev_close, exchange="NSE"):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "market": "IN",
        "currency": "INR",
        "prev_close": prev_close,
        "bars": bars,
        "is_live": False,
        "data_tier": "HISTORICAL",
        "data_status": "delayed",
        "provider": "yahoo",
    }


def _nov2026_bod_records(include_niftynxt50_fo=False):
    """Build synthetic BSE + NSE BOD master matching Nov 2026 specifications."""
    rows = [
        # Cash / Index records
        {
            "exchange": "BSE", "segment": "BSE_INDEX",
            "instrument_key": "BSE_INDEX|SENSEX", "trading_symbol": "SENSEX",
            "name": "BSE SENSEX", "instrument_type": "INDEX",
            "tick_size": 0.05, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty 50", "trading_symbol": "NIFTY",
            "name": "Nifty 50", "instrument_type": "INDEX",
            "tick_size": 0.05, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty Fin Service", "trading_symbol": "FINNIFTY",
            "name": "Nifty Fin Service", "instrument_type": "INDEX",
            "tick_size": 0.05, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty Bank", "trading_symbol": "BANKNIFTY",
            "name": "Nifty Bank", "instrument_type": "INDEX",
            "tick_size": 0.05, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|NIFTY MID SELECT", "trading_symbol": "MIDCPNIFTY",
            "name": "NIFTY MIDCAP SELECT", "instrument_type": "INDEX",
            "tick_size": 0.05, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty Next 50", "trading_symbol": "NIFTYNXT50",
            "name": "Nifty Next 50", "instrument_type": "INDEX",
            "tick_size": 0.05, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty 100", "trading_symbol": "NIFTY100",
            "name": "Nifty 100", "instrument_type": "INDEX",
            "tick_size": 0.05, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_EQ",
            "instrument_key": "NSE_EQ|INE002A01018", "trading_symbol": "RELIANCE",
            "name": "Reliance Industries Ltd", "instrument_type": "EQ",
            "tick_size": 0.10, "tick_size_unit": "INR", "lot_size": 1,
        },
        {
            "exchange": "NSE", "segment": "NSE_EQ",
            "instrument_key": "NSE_EQ|INE040H01021", "trading_symbol": "SUZLON",
            "name": "Suzlon Energy Ltd", "instrument_type": "EQ",
            "tick_size": 0.01, "tick_size_unit": "INR", "lot_size": 1,
        },
    ]

    # SENSEX BSE_FO Thursday weekly/monthly expiries: Nov 12, Nov 19, Nov 26
    for idx, (exp_d, exp_tag, is_wk) in enumerate([
        ("2026-11-12", "12 NOV 26", True),
        ("2026-11-19", "19 NOV 26", True),
        ("2026-11-26", "26 NOV 26", False),
    ]):
        for k_idx, strike in enumerate((82100.0, 82200.0, 82300.0)):
            rows.append({
                "exchange": "BSE", "segment": "BSE_FO",
                "instrument_key": "BSE_FO|%d" % (50000 + idx * 10 + k_idx),
                "underlying_symbol": "SENSEX",
                "trading_symbol": "SENSEX %d CE %s" % (int(strike), exp_tag),
                "expiry": exp_d, "weekly": is_wk,
                "instrument_type": "CE", "strike_price": strike,
                "lot_size": 20, "tick_size": 0.05, "tick_size_unit": "INR",
            })

    # FINNIFTY NSE_FO holiday-adjusted monthly expiries: Nov 23 (Nov 24 holiday), Dec 29
    for idx, (exp_d, exp_tag) in enumerate([("2026-11-23", "23 NOV 26"), ("2026-12-29", "29 DEC 26")]):
        for k_idx, strike in enumerate((27950.0, 28000.0, 28050.0)):
            rows.append({
                "exchange": "NSE", "segment": "NSE_FO",
                "instrument_key": "NSE_FO|%d" % (60000 + idx * 10 + k_idx),
                "underlying_symbol": "FINNIFTY",
                "trading_symbol": "FINNIFTY %d CE %s" % (int(strike), exp_tag),
                "expiry": exp_d, "weekly": False,
                "instrument_type": "CE", "strike_price": strike,
                "lot_size": 60, "tick_size": 0.05, "tick_size_unit": "INR",
            })

    # NIFTY NSE_FO weekly Tuesday expiries: Nov 17, Nov 23 (holiday-adjusted)
    for idx, (exp_d, exp_tag, is_wk) in enumerate([("2026-11-17", "17 NOV 26", True), ("2026-11-23", "23 NOV 26", False)]):
        for k_idx, strike in enumerate((25300.0, 25350.0, 25400.0)):
            rows.append({
                "exchange": "NSE", "segment": "NSE_FO",
                "instrument_key": "NSE_FO|%d" % (70000 + idx * 10 + k_idx),
                "underlying_symbol": "NIFTY",
                "trading_symbol": "NIFTY %d CE %s" % (int(strike), exp_tag),
                "expiry": exp_d, "weekly": is_wk,
                "instrument_type": "CE", "strike_price": strike,
                "lot_size": 65, "tick_size": 0.05, "tick_size_unit": "INR",
            })

    # BANKNIFTY & MIDCPNIFTY NSE_FO monthly
    for u, lot_sz, strikes, base_id in [
        ("BANKNIFTY", 30, (61200.0, 61300.0), 80000),
        ("MIDCPNIFTY", 120, (12275.0, 12300.0), 81000),
    ]:
        for k_idx, strike in enumerate(strikes):
            rows.append({
                "exchange": "NSE", "segment": "NSE_FO",
                "instrument_key": "NSE_FO|%d" % (base_id + k_idx),
                "underlying_symbol": u,
                "trading_symbol": "%s %d CE 23 NOV 26" % (u, int(strike)),
                "expiry": "2026-11-23", "weekly": False,
                "instrument_type": "CE", "strike_price": strike,
                "lot_size": lot_sz, "tick_size": 0.05, "tick_size_unit": "INR",
            })

    # RELIANCE NSE_FO monthly
    for k_idx, strike in enumerate((1380.0, 1400.0)):
        rows.append({
            "exchange": "NSE", "segment": "NSE_FO",
            "instrument_key": "NSE_FO|%d" % (82000 + k_idx),
            "underlying_symbol": "RELIANCE",
            "trading_symbol": "RELIANCE %d CE 23 NOV 26" % int(strike),
            "expiry": "2026-11-23", "weekly": False,
            "instrument_type": "CE", "strike_price": strike,
            "lot_size": 250, "tick_size": 0.05, "tick_size_unit": "INR",
        })

    if include_niftynxt50_fo:
        for k_idx, strike in enumerate((73700.0, 73800.0)):
            rows.append({
                "exchange": "NSE", "segment": "NSE_FO",
                "instrument_key": "NSE_FO|%d" % (83000 + k_idx),
                "underlying_symbol": "NIFTYNXT50",
                "trading_symbol": "NIFTYNXT50 %d CE 23 NOV 26" % int(strike),
                "expiry": "2026-11-23", "weekly": False,
                "instrument_type": "CE", "strike_price": strike,
                "lot_size": 25, "tick_size": 0.05, "tick_size_unit": "INR",
            })
    return rows


class DerivativesAuditTests(unittest.TestCase):

    def _write_bod_cache(self, tmp, include_niftynxt50_fo=False, effective_date="2026-11-12"):
        cache_path = os.path.join(tmp, "instruments.json")
        hist_dir = os.path.join(tmp, "history")
        payload = instruments.build_master_from_provider_records(
            _nov2026_bod_records(include_niftynxt50_fo=include_niftynxt50_fo),
            effective_date=effective_date,
            provider="upstox",
        )
        instruments.save_instrument_cache(payload, cache_path=cache_path, history_dir=hist_dir)
        return cache_path

    def _patch_cache(self, cache_path):
        stack = contextlib.ExitStack()
        seen = set()
        for inst_mod in (
            instruments,
            getattr(entry, "instruments", None),
            getattr(alerts, "instruments", None),
            getattr(data_in, "instruments", None),
        ):
            if inst_mod is not None and id(inst_mod) not in seen and hasattr(inst_mod, "DEFAULT_CACHE_PATH"):
                seen.add(id(inst_mod))
                stack.enter_context(patch.object(inst_mod, "DEFAULT_CACHE_PATH", cache_path))
        return stack

    def test_sensex_thursday_expiry_and_readiness_options_orb_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._write_bod_cache(tmp)
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"market": "IN", "paper_only": True, "budget": 100000, "max_loss": 1500, "daily_max_loss": 3000}, fh)

            with self._patch_cache(cache_path):
                # 1. Evaluation on 2026-11-12 (expiry day, 0d)
                meta_12 = instruments.resolve_underlying_derivatives_meta("SENSEX", exchange="BSE", as_of_date="2026-11-12")
                self.assertTrue(meta_12["available"])
                self.assertEqual(meta_12["expiry"], "2026-11-12")
                self.assertEqual(meta_12["lot_size"], 20)
                self.assertEqual(meta_12["strike_interval"], 100.0)
                self.assertTrue(calendar_in.is_expiry_day("2026-11-12", underlying="SENSEX", exchange="BSE"))
                self.assertEqual(calendar_in.days_to_expiry("2026-11-12", as_of_date="2026-11-12", exchange="BSE"), 0)

                # 2. Next valid BSE Thursday expiries: Nov 19, Nov 26 (no invalid Nov 27)
                meta_13 = instruments.resolve_underlying_derivatives_meta("SENSEX", exchange="BSE", as_of_date="2026-11-13")
                self.assertEqual(meta_13["expiry"], "2026-11-19")
                meta_20 = instruments.resolve_underlying_derivatives_meta("SENSEX", exchange="BSE", as_of_date="2026-11-20")
                self.assertEqual(meta_20["expiry"], "2026-11-26")

                # Monthly symbol parser/contract resolver must resolve Thu Nov 26, NOT Fri Nov 27
                parsed_mon = symbols_in.parse_derivative_symbol("SENSEX26NOV82200CE")
                self.assertEqual(parsed_mon["expiry_date"], "2026-11-26")
                c_mon = instruments.resolve_derivative_contract("SENSEX26NOV82200CE")
                self.assertEqual(c_mon["expiry"], "2026-11-26")
                self.assertEqual(c_mon["lot_size"], 20)

                # 3. Simulated provider refuses invalid Nov 27 expiry
                sim = data_in.SimulatedMarketDataProvider()
                with self.assertRaises(data_in.MissingDataError):
                    sim.get_option_chain("SENSEX", expiry="2026-11-27", exchange="BSE", spot_price=82200.0)

                # 4. Readiness Check 5 and options_orb on 2026-11-12 match identically
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    alerts.main(["readiness", "--market", "IN", "--config", cfg_path, "--now", "2026-11-12 10:15", "--json"])
                rd = json.loads(buf.getvalue())
                chk5 = {c["check"]: c for c in rd["checks"]}["Derivatives & Expiry Metadata"]
                self.assertIn("SENSEX [BSE] (exp 2026-11-12, 0d, lot 20, step 100)", chk5["detail"])

                sx_bars = [
                    {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 81900.0, "h": 82050.0, "l": 81850.0, "c": 82000.0, "v": 0}
                    for i in range(3)
                ] + [
                    {"t": _ist_ts(2026, 11, 12, 9, 30), "o": 82060.0, "h": 82230.0, "l": 82050.0, "c": 82210.0, "v": 0},
                ]
                now_12 = _utc_from_ts(_ist_ts(2026, 11, 12, 19, 56))
                ch_sx = _make_5m_chart("SENSEX", sx_bars, prev_close=81800.0, exchange="BSE")
                res_sx = entry.evaluate(ch_sx, now_12, setup="options_orb", market="IN", exchange="BSE")
                self.assertEqual(res_sx["status"], "triggered")
                oc = res_sx["option_contract"]
                self.assertIsNotNone(oc)
                self.assertEqual(oc["expiry"], "2026-11-12")
                self.assertNotEqual(oc["expiry"], "2026-11-27")
                self.assertEqual(oc["symbol"], "SENSEX26N1282200CE")
                self.assertEqual(oc["lot_size"], 20)
                self.assertEqual(oc["strike_interval"], 100.0)
                self.assertEqual(oc["tier"], "SIMULATED")
                self.assertTrue(oc["is_simulated"])
                self.assertEqual(res_sx["decision_state"], "NO TRADE")
                self.assertEqual(res_sx["sub_state"], "OPTION_RISK_UNAVAILABLE")
                self.assertIsNone(res_sx.get("reward_risk"))
                self.assertIn("underlying analytical levels", res_sx["underlying_technical_preview"]["interpretation"])

                # 5. Historical signal before BOD effective_date (2026-11-05 < 2026-11-12) must fail closed
                now_hist = _utc_from_ts(_ist_ts(2026, 11, 5, 10, 0))
                hist_bars = [
                    {"t": _ist_ts(2026, 11, 5, 9, 15 + 5 * i), "o": 81500.0, "h": 81600.0, "l": 81450.0, "c": 81550.0, "v": 0}
                    for i in range(3)
                ] + [
                    {"t": _ist_ts(2026, 11, 5, 9, 30), "o": 81610.0, "h": 81720.0, "l": 81600.0, "c": 81700.0, "v": 0},
                ]
                ch_hist = _make_5m_chart("SENSEX", hist_bars, prev_close=81400.0, exchange="BSE")
                res_hist = entry.evaluate(ch_hist, now_hist, setup="options_orb", market="IN", exchange="BSE")
                self.assertTrue(res_hist["options_unavailable"])
                self.assertEqual(res_hist["option_contract_status"], "UNAVAILABLE")
                self.assertIsNone(res_hist["option_contract"])
                self.assertEqual(res_hist["decision_state"], "NO TRADE")
                self.assertEqual(res_hist["sub_state"], "HISTORICAL_CONTRACT_UNAVAILABLE")

    def test_niftynxt50_without_bod_fo_fails_closed_while_spot_analysis_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._write_bod_cache(tmp, include_niftynxt50_fo=False)
            with self._patch_cache(cache_path):
                meta_nxt = instruments.resolve_underlying_derivatives_meta("NIFTYNXT50", exchange="NSE", as_of_date="2026-11-12")
                self.assertFalse(meta_nxt["available"])
                self.assertFalse(meta_nxt["is_fo_eligible"])
                self.assertIsNone(meta_nxt["expiry"])
                self.assertIsNone(meta_nxt["lot_size"])
                self.assertIsNone(meta_nxt["strike_interval"])
                self.assertIn("UNAVAILABLE", meta_nxt["unavailable_reason"])

                nxt_bars = [
                    {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 73400.0, "h": 73729.65, "l": 73311.15, "c": 73600.0, "v": 0}
                    for i in range(3)
                ] + [
                    {"t": _ist_ts(2026, 11, 12, 9, 30), "o": 73730.0, "h": 73790.0, "l": 73690.05, "c": 73757.65, "v": 0},
                ]
                now_nxt = _utc_from_ts(_ist_ts(2026, 11, 12, 9, 40))
                ch_nxt = _make_5m_chart("NIFTYNXT50", nxt_bars, prev_close=73250.0, exchange="NSE")

                # options_orb fails closed with UNAVAILABLE, zero synthetic contract/lot/strike/premium
                res_opt = entry.evaluate(ch_nxt, now_nxt, setup="options_orb", market="IN", exchange="NSE")
                self.assertTrue(res_opt["options_unavailable"])
                self.assertEqual(res_opt["option_contract_status"], "UNAVAILABLE")
                self.assertIsNone(res_opt["option_contract"])
                self.assertNotIn("option_symbol", res_opt.get("trigger") or {})
                self.assertEqual(res_opt["decision_state"], "NO TRADE")
                self.assertEqual(res_opt["sub_state"], "NO_LISTED_FO_CONTRACT")
                self.assertIn("UNAVAILABLE", "\n".join(entry.describe(res_opt)))

                with self.assertRaises(data_in.MissingDataError):
                    data_in.fetch_option_chain("NIFTYNXT50", spot_price=73757.65, exchange="NSE")

                # Spot technical analysis (orb) remains fully functional with ANALYTICAL_ONLY_INDEX
                res_spot = entry.evaluate(ch_nxt, now_nxt, setup="orb", market="IN", exchange="NSE")
                self.assertEqual(res_spot["status"], "triggered")
                self.assertEqual(res_spot["asset_type"], "INDEX")
                self.assertTrue(res_spot["analytical_only"])
                self.assertEqual(res_spot["decision_state"], "NO TRADE")
                self.assertEqual(res_spot["sub_state"], "ANALYTICAL_ONLY_INDEX")
                self.assertIsNotNone(res_spot["indicators"]["atr14"])

                # Preserved rsi_momentum expired lvl=None regression
                flat_bars = [
                    {"t": _ist_ts(2026, 11, 12, 9, 15) + i * 300, "o": 73400.0, "h": 73420.0, "l": 73380.0, "c": 73400.0 - (i % 3) * 10.0, "v": 0}
                    for i in range(72)
                ]
                ch_exp = _make_5m_chart("NIFTYNXT50", flat_bars, prev_close=72789.5, exchange="NSE")
                res_exp = entry.evaluate(ch_exp, _utc_from_ts(_ist_ts(2026, 11, 12, 16, 0)), setup="rsi_momentum", market="IN", exchange="NSE")
                self.assertEqual(res_exp["status"], "expired")
                self.assertEqual(res_exp["decision_state"], "NO TRADE")

    def test_finnifty_bod_consistency_with_holiday_adjusted_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._write_bod_cache(tmp)
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"market": "IN", "paper_only": True, "budget": 100000, "max_loss": 1500, "daily_max_loss": 3000}, fh)

            with self._patch_cache(cache_path):
                meta_fn = instruments.resolve_underlying_derivatives_meta("FINNIFTY", exchange="NSE", as_of_date="2026-11-12")
                self.assertTrue(meta_fn["available"])
                self.assertEqual(meta_fn["expiry"], "2026-11-23")  # Nov 24 Guru Nanak Jayanti holiday rollback
                self.assertEqual(meta_fn["lot_size"], 60)
                self.assertEqual(meta_fn["strike_interval"], 50.0)
                self.assertEqual(meta_fn["source"], "bod_master")

                c_fn = instruments.resolve_derivative_contract("FINNIFTY26NOV28000CE")
                self.assertEqual(c_fn["expiry"], "2026-11-23")
                self.assertEqual(c_fn["lot_size"], 60)
                self.assertEqual(c_fn["strike_interval"], 50.0)

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    alerts.main(["readiness", "--market", "IN", "--config", cfg_path, "--now", "2026-11-12 10:15", "--json"])
                rd = json.loads(buf.getvalue())
                chk5 = {c["check"]: c for c in rd["checks"]}["Derivatives & Expiry Metadata"]
                self.assertIn("FINNIFTY [NSE] (exp 2026-11-23, 11d, lot 60, step 50)", chk5["detail"])

                fn_bars = [
                    {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 27900.0, "h": 27980.0, "l": 27860.0, "c": 27950.0, "v": 0}
                    for i in range(3)
                ] + [
                    {"t": _ist_ts(2026, 11, 12, 9, 30), "o": 27980.0, "h": 28020.0, "l": 27970.0, "c": 28010.0, "v": 0},
                ]
                ch_fn = _make_5m_chart("FINNIFTY", fn_bars, prev_close=27850.0, exchange="NSE")
                res_fn = entry.evaluate(ch_fn, _utc_from_ts(_ist_ts(2026, 11, 12, 9, 36)), setup="options_orb", market="IN")
                oc = res_fn["option_contract"]
                self.assertIsNotNone(oc)
                self.assertEqual(oc["symbol"], "FINNIFTY26NOV28000CE")
                self.assertEqual(oc["expiry"], "2026-11-23")
                self.assertEqual(oc["lot_size"], 60)
                self.assertEqual(oc["strike_interval"], 50.0)
                self.assertEqual(res_fn["decision_state"], "NO TRADE")
                self.assertEqual(res_fn["sub_state"], "OPTION_RISK_UNAVAILABLE")
                self.assertFalse(res_fn["decision"]["actionable"])
                self.assertIsNone(res_fn.get("reward_risk"))
                rendered_fn = "\n".join(entry.describe(res_fn))
                self.assertIn("NO TRADE: OPTION RISK UNAVAILABLE", rendered_fn)
                self.assertNotIn("Targets preview:", rendered_fn)

    def test_general_non_fo_indices_and_equities_fail_closed_while_valid_fo_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._write_bod_cache(tmp, include_niftynxt50_fo=False)
            with self._patch_cache(cache_path):
                for non_fo_sym in ("NIFTY100", "NIFTY200", "NIFTY500", "SUZLON"):
                    meta = instruments.resolve_underlying_derivatives_meta(non_fo_sym, exchange="NSE", as_of_date="2026-11-12")
                    self.assertFalse(meta["available"], non_fo_sym)
                    self.assertEqual(meta["source"], "unavailable_no_listed_fo", non_fo_sym)

                    bars = [
                        {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000}
                        for i in range(3)
                    ] + [
                        {"t": _ist_ts(2026, 11, 12, 9, 30), "o": 101.0, "h": 102.5, "l": 100.8, "c": 102.0, "v": 1500},
                    ]
                    ch = _make_5m_chart(non_fo_sym, bars, prev_close=99.5, exchange="NSE")
                    res = entry.evaluate(ch, _utc_from_ts(_ist_ts(2026, 11, 12, 9, 36)), setup="options_orb", market="IN")
                    self.assertTrue(res["options_unavailable"], non_fo_sym)
                    self.assertIsNone(res["option_contract"], non_fo_sym)
                    self.assertEqual(res["decision_state"], "NO TRADE", non_fo_sym)

                # Valid listed F&O equity continues to work cleanly with BOD lot/expiry/step
                rel_bars = [
                    {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 1390.0, "h": 1398.0, "l": 1385.0, "c": 1395.0, "v": 10000}
                    for i in range(3)
                ] + [
                    {"t": _ist_ts(2026, 11, 12, 9, 30), "o": 1398.0, "h": 1406.0, "l": 1396.0, "c": 1403.0, "v": 15000},
                ]
                ch_rel = _make_5m_chart("RELIANCE", rel_bars, prev_close=1385.0, exchange="NSE")
                res_rel = entry.evaluate(ch_rel, _utc_from_ts(_ist_ts(2026, 11, 12, 9, 36)), setup="options_orb", market="IN")
                self.assertEqual(res_rel["status"], "triggered")
                self.assertEqual(res_rel["option_contract"]["lot_size"], 250)
                self.assertEqual(res_rel["option_contract"]["expiry"], "2026-11-23")
                self.assertTrue(res_rel["option_contract"]["is_simulated"])


if __name__ == "__main__":
    unittest.main()
