"""Tests demonstrating that pullback and reversal setups are decoupled from
vwap_reclaim and rsi_momentum with distinct trigger conditions, invalidation rules,
and decision states.
"""

from datetime import datetime as dt, timedelta, timezone
import json
import unittest

from _load import load

entry = load("trader-entry", "entry")


def _et_utc(y, m, d, hh, mm):
    return dt(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)


def _make_v8_chart(sym, bars, interval="5m", market="US", currency="USD",
                   exchange="NYSE", provider="yahoo", data_tier="CURRENT",
                   is_live=True, prev_close=100.0):
    ts = [int(b["t"]) for b in bars]
    os_ = [b["o"] for b in bars]
    hs = [b["h"] for b in bars]
    ls = [b["l"] for b in bars]
    cs = [b["c"] for b in bars]
    vs = [b.get("v", 10000) for b in bars]
    meta = {
        "symbol": sym,
        "currency": currency,
        "exchangeName": exchange,
        "instrumentType": "EQUITY",
        "dataGranularity": interval,
        "gmtoffset": -14400,
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


class TestSetupsDecoupled(unittest.TestCase):
    def test_setup_names_are_distinct(self):
        # Verify setup_name formats distinctly
        r_reclaim = {"setup": "vwap_reclaim", "level": 105.50, "market": "US"}
        r_pullback = {"setup": "pullback", "level": 104.20, "market": "US"}
        r_reversal = {"setup": "reversal", "level": 101.00, "market": "US"}
        r_rsi = {"setup": "rsi_momentum", "level": None, "market": "US"}
        r_rejection = {"setup": "vwap_rejection", "level": 105.50, "market": "US"}

        self.assertIn("VWAP reclaim", entry.setup_name(r_reclaim))
        self.assertIn("Pullback to support", entry.setup_name(r_pullback))
        self.assertIn("Exhaustion reversal", entry.setup_name(r_reversal))
        self.assertIn("RSI momentum", entry.setup_name(r_rsi))
        self.assertIn("VWAP rejection", entry.setup_name(r_rejection))

    def test_pullback_triggers_without_triggering_reversal(self):
        # Build an uptrend series (>= 22 bars so EMA21 is fully established):
        # EMA9 > EMA21, steady rise, then a shallow dip to support and a bullish hold.
        # This is a classic pullback, but NOT an exhaustion reversal (no 3 consecutive down candles, no oversold RSI).
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        bars = []
        px = 100.0
        for i in range(22):
            px += 0.5
            bars.append({"t": t0 + i * 300, "o": px - 0.2, "h": px + 0.4, "l": px - 0.3, "c": px, "v": 10000})

        # Bar 22 (11:20): dips to EMA9/support and holds with strong bullish close
        bars.append({"t": t0 + 22 * 300, "o": 109.8, "h": 111.2, "l": 109.4, "c": 111.0, "v": 15000})

        ch, _ = _make_v8_chart("BULL", bars, interval="5m", market="US")
        eval_time = _et_utc(2026, 10, 19, 11, 30)

        res_pb = entry.evaluate(ch, eval_time, setup="pullback", interval="5m")
        res_rev = entry.evaluate(ch, eval_time, setup="reversal", interval="5m")

        self.assertEqual(res_pb["status"], "triggered")
        self.assertEqual(res_pb["setup"], "pullback")
        self.assertEqual(res_pb["invalidation_rule"], "pullback_pivot_low")
        self.assertIn("completed bullish pullback holding support", res_pb["reason"])
        self.assertIsNotNone(res_pb["stop_preview"])
        self.assertLess(res_pb["stop_preview"], res_pb["trigger"]["price"])
        self.assertGreaterEqual(res_pb["reward_risk"], 2.0)

        # Reversal must NOT trigger on this mild uptrend pullback
        self.assertNotEqual(res_rev["status"], "triggered")

    def test_reversal_triggers_on_exhaustion_without_triggering_pullback(self):
        # Build a downtrend/sell-off series:
        # Multiple strong consecutive down bars (exhaustion) followed by a sharp bullish reversal bar.
        # Here EMA9 < EMA21 (downtrend), so pullback setup (which requires uptrend EMA9 > EMA21) must FAIL.
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        # 6 bars dropping
        bars = [
            {"t": t0, "o": 110.0, "h": 110.2, "l": 108.5, "c": 108.8, "v": 10000},
            {"t": t0 + 300, "o": 108.8, "h": 109.0, "l": 106.5, "c": 106.8, "v": 12000},
            {"t": t0 + 600, "o": 106.8, "h": 107.0, "l": 104.5, "c": 104.8, "v": 15000},
            # Bar 3: down
            {"t": t0 + 900, "o": 104.8, "h": 105.0, "l": 102.5, "c": 102.8, "v": 18000},
            # Bar 4: down
            {"t": t0 + 1200, "o": 102.8, "h": 103.0, "l": 100.5, "c": 100.8, "v": 20000},
            # Bar 5: sharp selloff climax bar
            {"t": t0 + 1500, "o": 100.8, "h": 101.0, "l": 98.0, "c": 98.2, "v": 35000},
            # Bar 6: strong bullish engulfing reversal bar reclaiming above prior bar
            {"t": t0 + 1800, "o": 98.4, "h": 101.5, "l": 98.2, "c": 101.2, "v": 28000},
        ]

        ch, _ = _make_v8_chart("BEAR", bars, interval="5m", market="US")
        eval_time = _et_utc(2026, 10, 19, 10, 6)

        res_rev = entry.evaluate(ch, eval_time, setup="reversal", interval="5m")
        res_pb = entry.evaluate(ch, eval_time, setup="pullback", interval="5m")

        self.assertEqual(res_rev["status"], "triggered")
        self.assertEqual(res_rev["setup"], "reversal")
        self.assertEqual(res_rev["invalidation_rule"], "exhaustion_low")
        self.assertIn("confirmed bullish exhaustion reversal", res_rev["reason"])
        self.assertIsNotNone(res_rev["stop_preview"])
        self.assertLess(res_rev["stop_preview"], res_rev["trigger"]["price"])
        self.assertGreaterEqual(res_rev["reward_risk"], 2.0)

        # Pullback must NOT trigger because EMAs are in a steep downtrend
        self.assertNotEqual(res_pb["status"], "triggered")

    def test_vwap_reclaim_distinct_from_pullback_and_rsi_momentum(self):
        # A simple VWAP cross from below with RSI still < 60
        start_utc = _et_utc(2026, 10, 19, 9, 30)
        t0 = int(start_utc.timestamp())

        # Bar 0 (09:30): 100.0, high 101.0, low 99.0, close 100.5, VWAP ~ 100.2
        # Bar 1 (09:35): dips below VWAP: open 100.2, high 100.4, low 99.0, close 99.5, VWAP ~ 99.9
        # Bar 2 (09:40): reclaims VWAP: open 99.5, high 101.5, low 99.4, close 101.2 (crosses VWAP)
        bars = [
            {"t": t0, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 10000},
            {"t": t0 + 300, "o": 100.2, "h": 100.4, "l": 99.0, "c": 99.5, "v": 10000},
            {"t": t0 + 600, "o": 99.5, "h": 101.5, "l": 99.4, "c": 101.2, "v": 12000},
        ]

        ch, _ = _make_v8_chart("CROSS", bars, interval="5m", market="US")
        eval_time = _et_utc(2026, 10, 19, 9, 46)

        res_rec = entry.evaluate(ch, eval_time, setup="vwap_reclaim", interval="5m")
        res_rsi = entry.evaluate(ch, eval_time, setup="rsi_momentum", interval="5m")

        self.assertEqual(res_rec["status"], "triggered")
        self.assertEqual(res_rec["setup"], "vwap_reclaim")
        self.assertIn("reclaimed session VWAP", res_rec["reason"])

        # RSI momentum should not be triggered (RSI requires 14+ bars for full calculation or >= 60)
        self.assertNotEqual(res_rsi["status"], "triggered")


if __name__ == "__main__":
    unittest.main()
