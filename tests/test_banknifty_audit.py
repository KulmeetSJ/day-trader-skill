"""Targeted correctness audit regression tests for BANKNIFTY and peer indices."""

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
data_in = _load.load("trader-alerts", "data_in")


def _ist_dt(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def _ist_ts(y, m, d, hh, mm):
    return int(_ist_dt(y, m, d, hh, mm).timestamp())


def _utc_from_ts(ts):
    return datetime.datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _sample_bod_records():
    """Construct minimal NSE BOD records matching Nov 2026 exchange specifications."""
    rows = [
        {
            "exchange": "NSE",
            "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty Bank",
            "trading_symbol": "BANKNIFTY",
            "name": "Nifty Bank",
            "instrument_type": "INDEX",
            "tick_size": 5,
            "lot_size": 1,
        },
        {
            "exchange": "NSE",
            "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY",
            "name": "Nifty 50",
            "instrument_type": "INDEX",
            "tick_size": 5,
            "lot_size": 1,
        },
        {
            "exchange": "NSE",
            "segment": "NSE_INDEX",
            "instrument_key": "NSE_INDEX|Nifty Fin Service",
            "trading_symbol": "FINNIFTY",
            "name": "Nifty Fin Service",
            "instrument_type": "INDEX",
            "tick_size": 5,
            "lot_size": 1,
        },
        # BANKNIFTY NFO: monthly expiry 2026-11-23 (Nov 24 Guru Nanak Jayanti holiday), lot 30, step 100
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "BANKNIFTY",
            "trading_symbol": "BANKNIFTY26NOV61300CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 11, 23, 23, 59).timestamp() * 1000),
            "strike_price": 61300.0,
            "lot_size": 30,
            "tick_size": 5,
        },
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "BANKNIFTY",
            "trading_symbol": "BANKNIFTY26NOV61400CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 11, 23, 23, 59).timestamp() * 1000),
            "strike_price": 61400.0,
            "lot_size": 30,
            "tick_size": 5,
        },
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "BANKNIFTY",
            "trading_symbol": "BANKNIFTY26DEC61300CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 12, 29, 23, 59).timestamp() * 1000),
            "strike_price": 61300.0,
            "lot_size": 30,
            "tick_size": 5,
        },
        # NIFTY NFO: weekly Tuesday 2026-11-17 and monthly 2026-11-23, lot 65, step 50
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "NIFTY",
            "trading_symbol": "NIFTY26111725300CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 11, 17, 23, 59).timestamp() * 1000),
            "strike_price": 25300.0,
            "lot_size": 65,
            "tick_size": 5,
        },
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "NIFTY",
            "trading_symbol": "NIFTY26111725350CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 11, 17, 23, 59).timestamp() * 1000),
            "strike_price": 25350.0,
            "lot_size": 65,
            "tick_size": 5,
        },
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "NIFTY",
            "trading_symbol": "NIFTY26NOV25300CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 11, 23, 23, 59).timestamp() * 1000),
            "strike_price": 25300.0,
            "lot_size": 65,
            "tick_size": 5,
        },
        # FINNIFTY NFO: monthly only, lot 60, step 50
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "FINNIFTY",
            "trading_symbol": "FINNIFTY26NOV27000CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 11, 23, 23, 59).timestamp() * 1000),
            "strike_price": 27000.0,
            "lot_size": 60,
            "tick_size": 5,
        },
        {
            "exchange": "NSE",
            "segment": "NSE_FO",
            "underlying_symbol": "FINNIFTY",
            "trading_symbol": "FINNIFTY26NOV27050CE",
            "instrument_type": "CE",
            "expiry": int(_ist_dt(2026, 11, 23, 23, 59).timestamp() * 1000),
            "strike_price": 27050.0,
            "lot_size": 60,
            "tick_size": 5,
        },
    ]
    for row in rows:
        row["tick_size_unit"] = "paise"
    return rows


def _make_5m_chart(sym, bars, prev_close, exchange="NSE"):
    return {
        "symbol": sym,
        "market": "IN",
        "exchange": exchange,
        "currency": "INR",
        "interval": "5m",
        "provider": "upstox",
        "data_tier": "HISTORICAL",
        "data_status": "delayed",
        "is_offline": True,
        "is_live": False,
        "prev_close": prev_close,
        "meta": {
            "symbol": sym,
            "dataGranularity": "5m",
            "exchangeName": exchange,
            "chartPreviousClose": prev_close,
            "previousClose": prev_close,
            "regularMarketPrice": bars[-1]["c"] if bars else prev_close,
        },
        "bars": list(bars),
    }


def _trading_days_before(end_date, count):
    out = []
    cur = end_date
    while len(out) < count:
        ok, _ = entry.trading_day(cur, market="IN")
        if ok:
            out.append(cur)
        cur -= datetime.timedelta(days=1)
    out.reverse()
    return out


class BankNiftyAuditTests(unittest.TestCase):

    def test_derivatives_metadata_uses_bod_first_and_separates_weekly_from_monthly(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "instruments.json")
            payload = instruments.build_master_from_provider_records(
                _sample_bod_records(),
                effective_date="2026-11-12",
                provider="upstox",
            )
            instruments.save_instrument_cache(payload, cache_path=cache_path, history_dir=tmp)

            with patch.object(instruments, "DEFAULT_CACHE_PATH", cache_path):
                bn = instruments.resolve_underlying_derivatives_meta("BANKNIFTY", as_of_date="2026-11-12")
                self.assertEqual(bn["lot_size"], 30)
                self.assertEqual(bn["expiry"], "2026-11-23")
                self.assertEqual(bn["expiry_schedule"], "monthly")
                self.assertEqual(bn["strike_interval"], 100.0)

                nf = instruments.resolve_underlying_derivatives_meta("NIFTY", as_of_date="2026-11-12")
                self.assertEqual(nf["lot_size"], 65)
                self.assertEqual(nf["expiry"], "2026-11-17")
                self.assertEqual(nf["expiry_schedule"], "weekly")
                self.assertEqual(nf["strike_interval"], 50.0)

                fn = instruments.resolve_underlying_derivatives_meta("FINNIFTY", as_of_date="2026-11-12")
                self.assertEqual(fn["lot_size"], 60)
                self.assertEqual(fn["expiry"], "2026-11-23")
                self.assertEqual(fn["expiry_schedule"], "monthly")
                self.assertEqual(fn["strike_interval"], 50.0)

                # Contract resolution ties expiry, lot size, and strike step together
                c_bn = instruments.resolve_derivative_contract("BANKNIFTY26NOV61300CE")
                self.assertEqual(c_bn["lot_size"], 30)
                self.assertEqual(c_bn["expiry"], "2026-11-23")
                self.assertEqual(c_bn["strike_interval"], 100.0)

                # Calendar fallback without cache still respects BANKNIFTY monthly + Nov 24 holiday
                self.assertEqual(
                    calendar_in.get_next_expiry(as_of_date="2026-11-12", underlying="BANKNIFTY").isoformat(),
                    "2026-11-23",
                )
                self.assertEqual(
                    calendar_in.get_next_expiry(as_of_date="2026-11-12", underlying="FINNIFTY").isoformat(),
                    "2026-11-23",
                )
                self.assertEqual(
                    calendar_in.get_next_expiry(as_of_date="2026-11-12", underlying="NIFTY").isoformat(),
                    "2026-11-17",
                )

                # Readiness Check 5 surfaces authoritative BOD metadata
                cfg_path = os.path.join(tmp, "config.json")
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump({"market": "IN", "paper_only": True, "budget": 100000, "max_loss": 1500, "daily_max_loss": 3000}, fh)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    alerts.main(["readiness", "--market", "IN", "--config", cfg_path, "--now", "2026-11-12 10:15", "--json"])
                rd = json.loads(buf.getvalue())
                chk5 = {c["check"]: c for c in rd["checks"]}["Derivatives & Expiry Metadata"]
                self.assertIn("BANKNIFTY [NSE] (exp 2026-11-23, 11d, lot 30, step 100)", chk5["detail"])
                self.assertIn("NIFTY [NSE] (exp 2026-11-17, 5d, lot 65, step 50)", chk5["detail"])

                # options_orb for BANKNIFTY and NIFTY resolve expiry, lot_size, and strike_interval together
                bn_bars = [
                    {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 61000.0, "h": 61195.5, "l": 60723.1, "c": 61100.0, "v": 1000}
                    for i in range(3)
                ] + [
                    {"t": _ist_ts(2026, 11, 12, 9, 30), "o": 61150.0, "h": 61260.0, "l": 61140.0, "c": 61252.95, "v": 1500},
                ]
                nf_bars = [
                    {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 25220.0, "h": 25341.6, "l": 25145.0, "c": 25300.0, "v": 1000}
                    for i in range(3)
                ] + [
                    {"t": _ist_ts(2026, 11, 12, 9, 30), "o": 25320.0, "h": 25360.0, "l": 25315.0, "c": 25352.0, "v": 1500},
                ]
                now_bn = _utc_from_ts(_ist_ts(2026, 11, 12, 9, 36))
                res_bn = entry.evaluate(_make_5m_chart("BANKNIFTY", bn_bars, 60792.4), now_bn, setup="options_orb", market="IN")
                self.assertTrue(res_bn["options_unavailable"])
                self.assertIsNone(res_bn["option_contract"])
                self.assertEqual(res_bn["decision_state"], "NO TRADE")
                self.assertIn("not present", res_bn["option_contract_unavailable_reason"].lower())

                res_nf = entry.evaluate(_make_5m_chart("NIFTY", nf_bars, 25255.05), now_bn, setup="options_orb", market="IN")
                self.assertTrue(res_nf["options_unavailable"])
                self.assertIsNone(res_nf["option_contract"])
                self.assertEqual(res_nf["decision_state"], "NO TRADE")

    def test_rsi_momentum_never_reuses_old_prev_close_as_entry_zone_or_invalidation(self):
        # Simulate BANKNIFTY intraday session with stale 5d chartPreviousClose=57773.65,
        # RSI crossing 60 at 11:05 close=61301.95 (low=61267.35), and final bar closing at 61518.50.
        bars = []
        # Build 22 earlier bars with gentle pullback so RSI < 60 before 11:05
        for i in range(22):
            ts = _ist_ts(2026, 11, 12, 9, 15) + i * 300
            px = 61050.0 + (i % 4) * 12.0 - (6.0 if i > 14 else 0.0)
            bars.append({"t": ts, "o": px - 15.0, "h": px + 20.0, "l": px - 25.0, "c": px, "v": 1000})
        # Push RSI sharply above 60 on bar 22..25
        for j, (close_p, low_p) in enumerate([(61120.0, 61080.0), (61200.0, 61110.0), (61301.95, 61267.35)]):
            ts = bars[-1]["t"] + 300
            bars.append({"t": ts, "o": close_p - 30.0, "h": close_p + 10.0, "l": low_p, "c": close_p, "v": 1200})
        # Stay extended upward to 61518.50
        for k in range(4):
            ts = bars[-1]["t"] + 300
            c_p = 61360.0 + k * 52.83
            if k == 3:
                c_p = 61518.50
            bars.append({"t": ts, "o": c_p - 20.0, "h": c_p + 10.0, "l": c_p - 25.0, "c": c_p, "v": 1100})

        now_utc = _utc_from_ts(bars[-1]["t"] + 310)
        # Replicate BANKNIFTY with stale 5d prev_close=57773.65
        ch_bn = _make_5m_chart("BANKNIFTY", bars, prev_close=57773.65)
        res_bn = entry.evaluate(ch_bn, now_utc, setup="rsi_momentum", market="IN")
        self.assertIsNotNone(res_bn.get("trigger"))
        trig_px = res_bn["trigger"]["close"]
        self.assertGreater(trig_px, 61000.0)
        # Zone MUST anchor to trigger close, NEVER to stale prev_close 57773.65
        self.assertGreater(res_bn["entry_zone"]["low"], 61000.0)
        self.assertEqual(res_bn["entry_zone"]["anchor"], trig_px)
        self.assertGreater(res_bn["invalidation_level"], 61000.0)
        self.assertNotAlmostEqual(res_bn["invalidation_level"], 57773.65, places=1)
        self.assertEqual(res_bn["decision_state"], "NO TRADE")
        self.assertFalse(res_bn["decision"]["actionable"])
        self.assertIsNone(res_bn["level"])

        # Same RSI setup on a tradable equity/contract symbol (e.g., RELIANCE) with far-below prev_close:
        # Must classify as NO TRADE (EXTENDED) while preserving trigger, not fabricate 80+ ATR garbage.
        ch_eq = _make_5m_chart("RELIANCE", bars, prev_close=57773.65)
        res_eq = entry.evaluate(ch_eq, now_utc, setup="rsi_momentum", market="IN")
        self.assertIsNotNone(res_eq.get("trigger"))
        self.assertGreater(res_eq["entry_zone"]["low"], 61000.0)
        self.assertEqual(res_eq["decision_state"], "NO TRADE")
        if res_eq["anti_chasing"]["extension_atr"] is not None:
            self.assertLess(res_eq["anti_chasing"]["extension_atr"], 15.0)

        # Defense-in-depth guard: if _assess_intraday_decision ever sees a detached lvl far below trig["c"],
        # it suppresses the detached entry_zone, keeps actionable=False, and preserves trigger.
        parsed_bars = [
            dict(b, et=_ist_dt(2026, 11, 12, 9, 15) + datetime.timedelta(minutes=5 * idx),
                 end=_ist_dt(2026, 11, 12, 9, 20) + datetime.timedelta(minutes=5 * idx), slot=idx)
            for idx, b in enumerate(bars)
        ]
        trig_sample = parsed_bars[-4]
        mock_res = {"status": "triggered", "phase": "open", "reason": "level breakout"}
        guarded = entry._assess_intraday_decision(
            mock_res, ch_eq, parsed_bars, parsed_bars, 57773.65, trig_sample, None, {"atr14": 45.0},
            parsed_bars[-1]["end"], datetime.date(2026, 11, 12), datetime.date(2026, 11, 12), 300,
            "IN", 0.05, True, entry.DEFAULTS_IN, "level", "CURRENT", "CURRENT", True, True, False,
            True, False, _ist_dt(2026, 11, 12, 15, 0),
        )
        self.assertIsNone(guarded["entry_zone"])
        self.assertEqual(guarded["decision_state"], "NO TRADE")
        self.assertEqual(guarded["sub_state"], "EXTENDED")
        self.assertFalse(guarded["decision"]["actionable"])

        # Expired / untriggered rsi_momentum with lvl=None must NOT TypeError on cutoff formatting
        flat_bars = [
            {"t": _ist_ts(2026, 11, 12, 9, 15) + i * 300, "o": 73400.0, "h": 73420.0, "l": 73380.0, "c": 73400.0 - (i % 3) * 10.0, "v": 1000}
            for i in range(72)
        ]
        ch_exp = _make_5m_chart("NIFTYNXT50", flat_bars, prev_close=72789.5)
        now_exp = _utc_from_ts(_ist_ts(2026, 11, 12, 16, 0))
        res_exp = entry.evaluate(ch_exp, now_exp, setup="rsi_momentum", market="IN")
        self.assertEqual(res_exp["status"], "expired")
        self.assertIsNone(res_exp["entry_zone"])
        self.assertEqual(res_exp["decision_state"], "NO TRADE")

    def test_breakout_20d_uses_prior_completed_closes_and_prevents_lookahead_for_banknifty_and_nifty(self):
        prior_days = _trading_days_before(datetime.date(2026, 11, 11), 34)
        # Case 1: BANKNIFTY - breakout reference is the highest close, not an intraday high.
        bn_daily = []
        for idx, d in enumerate(prior_days):
            if idx == len(prior_days) - 1:
                bn_daily.append({"t": _ist_ts(d.year, d.month, d.day, 9, 15), "o": 60500.0, "h": 61394.30, "l": 60400.0, "c": 60792.40, "v": 1000})
            else:
                base_c = 58800.0 + idx * 45.0
                bn_daily.append({"t": _ist_ts(d.year, d.month, d.day, 9, 15), "o": base_c - 50.0, "h": base_c + 120.0, "l": base_c - 100.0, "c": base_c, "v": 1000})

        # Add an eval-day (2026-11-12) daily bar with huge EOD high=62500 that must be EXCLUDED (no look-ahead)
        bn_daily_with_today = bn_daily + [
            {"t": _ist_ts(2026, 11, 12, 9, 15), "o": 60850.0, "h": 62500.0, "l": 60723.1, "c": 61518.5, "v": 1000},
        ]

        # The intraday close remains below the prior completed session's highest close -> waiting.
        bars_below = [
            {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 60650.0, "h": 60750.0, "l": 60600.0, "c": 60700.0, "v": 1000}
            for i in range(6)
        ]
        ch_wait = dict(_make_5m_chart("BANKNIFTY", bars_below, prev_close=60792.40), daily_bars=bn_daily_with_today)
        res_wait = entry.evaluate(
            ch_wait, _utc_from_ts(bars_below[-1]["t"] + 310),
            setup="breakout_20d", market="IN",
        )
        self.assertEqual(res_wait["breakout_level"], 60792.40)
        self.assertEqual(res_wait["level"], 60792.40)
        self.assertEqual(res_wait["daily_close"], 60792.40)
        self.assertEqual(res_wait["daily_close_date"], "2026-11-11")
        self.assertEqual(res_wait["status"], "waiting")
        self.assertIsNone(res_wait["trigger"])

        # A close above the highest completed daily close confirms the breakout.
        bars_brk = bars_below + [
            {"t": _ist_ts(2026, 11, 12, 9, 45), "o": 60780.0, "h": 60910.0, "l": 60767.20, "c": 60793.00, "v": 2000},
        ]
        ch_trig = dict(_make_5m_chart("BANKNIFTY", bars_brk, prev_close=60792.40), daily_bars=bn_daily_with_today)
        res_trig = entry.evaluate(
            ch_trig, _utc_from_ts(bars_brk[-1]["t"] + 310),
            setup="breakout_20d", market="IN",
        )
        self.assertEqual(res_trig["breakout_level"], 60792.40)
        self.assertEqual(res_trig["status"], "triggered")
        self.assertEqual(res_trig["confirmation_price"], 60793.00)
        # Spot indices may display the analytical trigger but cannot carry a trade plan.
        self.assertIsNone(res_trig["entry_reference"])
        self.assertIsNone(res_trig["stop_preview"])
        self.assertIsNone(res_trig["reward_risk"])

        # Case 2: NIFTY - a prior intraday high does not become the closing-high reference.
        nf_daily = []
        for idx, d in enumerate(prior_days):
            if idx == len(prior_days) - 3:
                nf_daily.append({"t": _ist_ts(d.year, d.month, d.day, 9, 15), "o": 25200.0, "h": 25424.35, "l": 25150.0, "c": 25240.0, "v": 1000})
            elif idx == len(prior_days) - 1:
                nf_daily.append({"t": _ist_ts(d.year, d.month, d.day, 9, 15), "o": 25200.0, "h": 25300.0, "l": 25180.0, "c": 25255.05, "v": 1000})
            else:
                base_c = 24950.0 + idx * 6.0
                nf_daily.append({"t": _ist_ts(d.year, d.month, d.day, 9, 15), "o": base_c - 10.0, "h": base_c + 25.0, "l": base_c - 20.0, "c": base_c, "v": 1000})

        nf_bars = [
            {"t": _ist_ts(2026, 11, 12, 9, 15 + 5 * i), "o": 25260.0, "h": 25310.0, "l": 25250.0, "c": 25299.35, "v": 1000}
            for i in range(6)
        ]
        ch_nf = dict(_make_5m_chart("NIFTY", nf_bars, prev_close=25255.05), daily_bars=nf_daily)
        res_nf = entry.evaluate(
            ch_nf, _utc_from_ts(nf_bars[-1]["t"] + 310),
            setup="breakout_20d", market="IN",
        )
        self.assertEqual(res_nf["breakout_level"], 25255.05)
        self.assertEqual(res_nf["status"], "triggered")
        self.assertIsNotNone(res_nf["trigger"])


if __name__ == "__main__":
    unittest.main()
