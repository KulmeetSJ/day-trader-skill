"""Tests for VWAP Rejection bearish intraday strategy in trader-entry."""

import copy
import datetime
from datetime import datetime as dt, timedelta, timezone
import json
import unittest

from _load import load

entry = load("trader-entry", "entry")


def _et_utc(y, m, d, hh, mm):
    return dt(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)


def _ist_utc(y, m, d, hh, mm):
    return dt(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc)


def _make_v8_chart(sym, bars, interval="5m", market="US", currency="USD",
                   exchange="NYSE", provider="yahoo", data_tier="CURRENT",
                   is_live=True, prev_close=100.0):
    ts = [int(b["t"]) for b in bars]
    os_ = [b["o"] for b in bars]
    hs = [b["h"] for b in bars]
    ls = [b["l"] for b in bars]
    cs = [b["c"] for b in bars]
    vs = [b.get("v", 10000) for b in bars]
    gmtoff = 19800 if market == "IN" else -14400
    meta = {
        "symbol": sym,
        "currency": currency,
        "exchangeName": exchange,
        "instrumentType": "EQUITY",
        "dataGranularity": interval,
        "gmtoffset": gmtoff,
        "chartPreviousClose": prev_close,
        "previousClose": prev_close,
        "regularMarketPrice": cs[-1] if cs else prev_close,
    }
    doc = {
        "chart": {
            "result": [{
                "meta": meta,
                "timestamp": ts,
                "indicators": {
                    "quote": [{
                        "open": os_,
                        "high": hs,
                        "low": ls,
                        "close": cs,
                        "volume": vs,
                    }]
                },
            }],
            "error": None,
        }
    }
    ch = entry.parse_chart(json.dumps(doc), sym)
    ch["market"] = market
    ch["provider"] = provider
    ch["source"] = provider
    ch["data_tier"] = data_tier
    ch["provider_data_status"] = data_tier
    ch["is_live"] = is_live
    ch["is_offline"] = False
    return ch, doc


class TestVWAPRejection(unittest.TestCase):
    def test_vwap_rejection_triggers_bearish_signal(self):
        # 2026-10-19 US Session starting at 09:30 EDT
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        # Bar 0 (09:30-09:35): 100 -> 102 (high), VWAP ~ 101.0
        # Bar 1 (09:35-09:40): drops to 98.0, close 98.5 (below VWAP ~99.8)
        # Bar 2 (09:40-09:45): pushes up to test VWAP (high=99.9), rejects sharply down to close=97.0
        bars = [
            {"t": t0, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 10000},
            {"t": t0 + 300, "o": 100.5, "h": 100.8, "l": 98.0, "c": 98.5, "v": 10000},
            {"t": t0 + 600, "o": 98.5, "h": 99.9, "l": 96.8, "c": 97.0, "v": 15000},
        ]

        ch, _ = _make_v8_chart("TEST", bars, interval="5m", market="US")
        # Evaluate after bar 2 closes, at 09:46 EDT
        eval_time = _et_utc(2026, 10, 19, 9, 46)

        res = entry.evaluate(ch, eval_time, setup="vwap_rejection", interval="5m")

        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["setup"], "vwap_rejection")
        self.assertEqual(res["side"], "sell")
        self.assertIsNotNone(res["trigger"])
        self.assertEqual(res["trigger"]["bar_start"], "09:40")

        # Invalidation level should be rejection high or VWAP (above entry price 97.0)
        inv = res["invalidation_level"]
        self.assertIsNotNone(inv)
        self.assertGreaterEqual(inv, 97.0)
        self.assertEqual(res["stop_preview"], inv)

        # Risk per unit is inv - entry price
        self.assertGreater(res["risk_per_unit"], 0.0)

        # Targets preview should be placed below entry price (bearish short target)
        self.assertIsNotNone(res["targets_preview"])
        tp = res["targets_preview"]
        t1, t2 = tp["t1_1r"], tp["t2_2r"]
        self.assertLess(t1, 97.0)
        self.assertLess(t2, t1)
        self.assertGreaterEqual(res["reward_risk"], 2.0)

        # Verify entry document generation
        doc = entry.entry_doc(res, eval_time)
        self.assertEqual(doc["side"], "sell")
        self.assertEqual(doc["setup"], "vwap_rejection")

    def test_reclaim_above_vwap_does_not_trigger_rejection(self):
        # If price closes ABOVE VWAP, vwap_rejection must NOT trigger
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        # Bar 0 (09:30-09:35): 100 -> 101
        # Bar 1 (09:35-09:40): drops to 99.0
        # Bar 2 (09:40-09:45): reclaims and closes ABOVE VWAP at 103.0
        bars = [
            {"t": t0, "o": 100.0, "h": 101.5, "l": 99.5, "c": 101.0, "v": 10000},
            {"t": t0 + 300, "o": 100.5, "h": 100.5, "l": 98.5, "c": 99.0, "v": 10000},
            {"t": t0 + 600, "o": 99.0, "h": 103.5, "l": 99.0, "c": 103.0, "v": 15000},
        ]

        ch, _ = _make_v8_chart("TEST", bars, interval="5m", market="US")
        eval_time = _et_utc(2026, 10, 19, 9, 46)

        res = entry.evaluate(ch, eval_time, setup="vwap_rejection", interval="5m")

        self.assertNotEqual(res["status"], "triggered")
        self.assertIsNone(res["trigger"])

    def test_forming_bar_does_not_prematurely_trigger(self):
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        bars = [
            {"t": t0, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 10000},
            {"t": t0 + 300, "o": 100.5, "h": 100.8, "l": 98.0, "c": 98.5, "v": 10000},
            {"t": t0 + 600, "o": 98.5, "h": 99.9, "l": 96.8, "c": 97.0, "v": 15000},
        ]

        ch, _ = _make_v8_chart("TEST", bars, interval="5m", market="US")
        # Evaluated at 09:42 EDT, while bar 2 is still forming
        eval_time = _et_utc(2026, 10, 19, 9, 42)

        res = entry.evaluate(ch, eval_time, setup="vwap_rejection", interval="5m")

        self.assertEqual(res["status"], "waiting")
        self.assertIsNone(res["trigger"])

    def test_vwap_rejection_indian_market(self):
        # 2026-10-19 Indian NSE Session starting at 09:15 IST
        start_utc = _ist_utc(2026, 10, 19, 9, 15)
        t0 = int(start_utc.timestamp())

        bars = [
            {"t": t0, "o": 2500.0, "h": 2520.0, "l": 2490.0, "c": 2510.0, "v": 50000},
            {"t": t0 + 300, "o": 2505.0, "h": 2510.0, "l": 2470.0, "c": 2480.0, "v": 60000},
            {"t": t0 + 600, "o": 2480.0, "h": 2498.0, "l": 2440.0, "c": 2445.0, "v": 80000},
        ]

        ch, _ = _make_v8_chart("INFY", bars, interval="5m", market="IN", currency="INR", exchange="NSE")
        eval_time = _ist_utc(2026, 10, 19, 9, 31)

        res = entry.evaluate(ch, eval_time, setup="vwap_rejection", interval="5m", market="IN")

        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["setup"], "vwap_rejection")
        self.assertEqual(res["side"], "sell")
        self.assertEqual(res["market"], "IN")
        self.assertEqual(res["currency"], "INR")
        self.assertGreaterEqual(res["reward_risk"], 2.0)

    def test_vwap_rejection_sizing_preview(self):
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        bars = [
            {"t": t0, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 10000},
            {"t": t0 + 300, "o": 100.5, "h": 100.8, "l": 98.0, "c": 98.5, "v": 10000},
            {"t": t0 + 600, "o": 98.5, "h": 99.9, "l": 96.8, "c": 97.0, "v": 15000},
        ]

        ch, _ = _make_v8_chart("TEST", bars, interval="5m", market="US")
        eval_time = _et_utc(2026, 10, 19, 9, 46)

        cfg = dict(
            entry.DEFAULTS,
            max_loss_per_trade_usd=150.0,
            budget=20000.0,
            max_position_pct=50.0,
            min_reward_risk=2.0,
        )
        res = entry.evaluate(ch, eval_time, setup="vwap_rejection", interval="5m", cfg=cfg)

        self.assertEqual(res["status"], "triggered")
        sp = res.get("sizing_preview")
        self.assertIsNotNone(sp)
        self.assertGreater(sp["qty"], 0)
        expected_qty = int(150.0 // res["risk_per_unit"])
        self.assertEqual(sp["qty"], expected_qty)
        self.assertLessEqual(sp["estimated_max_loss"], 150.0)

    def test_vwap_rejection_anti_chasing(self):
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        bars = [
            {"t": t0, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 10000},
            {"t": t0 + 300, "o": 100.5, "h": 100.8, "l": 98.0, "c": 98.5, "v": 10000},
            {"t": t0 + 600, "o": 98.5, "h": 99.9, "l": 96.8, "c": 97.0, "v": 15000},
        ]

        ch, _ = _make_v8_chart("TEST", bars, interval="5m", market="US")
        # Current quote is dumped way below the entry zone (e.g. at 90.0 vs trigger at 97.0)
        ch["current_quote"] = {"price": 90.0, "time": "09:47:00"}
        eval_time = _et_utc(2026, 10, 19, 9, 47)

        cfg = dict(entry.DEFAULTS, max_loss_per_trade_usd=150.0, max_chase_pct=1.0)
        res = entry.evaluate(ch, eval_time, setup="vwap_rejection", interval="5m", cfg=cfg)

        self.assertEqual(res["decision"]["state"], "NO TRADE")
        self.assertEqual(res["decision"]["sub_state"], "EXTENDED")
        self.assertTrue(res["decision"]["chase_blocked"])
        self.assertIn("below entry zone lower bound", res["decision"]["no_trade_reason"])


if __name__ == "__main__":
    unittest.main()
