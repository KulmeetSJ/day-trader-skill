import os
import sys
import unittest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load

vf = load("trader-alerts", "volume_flow")


class TestVolumeFlow(unittest.TestCase):
    def setUp(self):
        # 10 bars with steady volume 1000
        self.sample_bars = [
            {"o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 1000, "t": "2026-11-10 09:%02d" % (15 + i * 5)}
            for i in range(10)
        ]

    def test_rvol_rolling_lookback_no_lookahead(self):
        bars = list(self.sample_bars)
        # Bar 10 has a surge to 3000
        bars.append({"o": 101.0, "h": 105.0, "l": 101.0, "c": 104.5, "v": 3000, "t": "2026-11-10 10:05"})
        res = vf.compute_rvol(bars, target_idx=10, lookback=10)
        self.assertEqual(res["baseline_volume"], 1000)
        self.assertEqual(res["current_volume"], 3000)
        self.assertEqual(res["rvol"], 3.0)
        self.assertTrue(res["is_spike"])
        self.assertEqual(res["method"], "rolling_preceding_bars")

    def test_rvol_historical_time_of_day_bucket(self):
        bars = [{"o": 100, "h": 102, "l": 99, "c": 101, "v": 2500, "time": "09:15"}]
        hist_days = [
            [{"time": "09:15", "v": 1000}],
            [{"time": "09:15", "v": 1200}],
            [{"time": "09:15", "v": 800}],
        ]
        res = vf.compute_rvol(bars, target_idx=0, historical_session_bars=hist_days)
        self.assertEqual(res["method"], "historical_time_of_day")
        self.assertEqual(res["baseline_volume"], 1000)
        self.assertEqual(res["current_volume"], 2500)
        self.assertEqual(res["rvol"], 2.5)

    def test_volume_breakout_analysis(self):
        bars = list(self.sample_bars)
        bars.append({"o": 101.0, "h": 106.0, "l": 101.0, "c": 105.5, "v": 2000, "t": "2026-11-10 10:05"})
        analysis = vf.compute_volume_analysis(bars, target_idx=len(bars) - 1, rvol_threshold=1.5)
        self.assertEqual(analysis["status"], "OK")
        self.assertEqual(analysis["rvol"], 2.0)
        self.assertTrue(analysis["is_confirmed"])
        self.assertTrue(analysis["is_spike"])

    def test_volume_price_divergence(self):
        # Price goes 100 -> 102 -> 104 -> 106, but volume goes 2000 -> 1500 -> 1000 -> 500
        div_bars = [
            {"o": 100, "h": 101, "l": 99, "c": 100, "v": 2000},
            {"o": 100, "h": 103, "l": 100, "c": 102, "v": 1500},
            {"o": 102, "h": 105, "l": 102, "c": 104, "v": 1000},
            {"o": 104, "h": 107, "l": 104, "c": 106, "v": 500},
        ]
        analysis = vf.compute_volume_analysis(div_bars, target_idx=3)
        self.assertEqual(analysis["divergence"], "bearish_volume_divergence")
        self.assertEqual(analysis["volume_trend"], "decreasing")

    def test_order_flow_actual_vs_estimated_integrity(self):
        # 1. Actual trade ticks provided -> mode MUST be ACTUAL, is_actual MUST be True
        ticks = [
            {"qty": 500, "side": "BUY"},
            {"qty": 200, "side": "SELL"},
            {"qty": 300, "side": "BUY"},
        ]
        flow_actual = vf.compute_order_flow(self.sample_bars, ticks=ticks)
        self.assertEqual(flow_actual["aggressor_mode"], vf.AGGRESSOR_ACTUAL)
        self.assertTrue(flow_actual["is_actual"])
        self.assertFalse(flow_actual["is_estimated"])
        self.assertEqual(flow_actual["buy_volume"], 800)
        self.assertEqual(flow_actual["sell_volume"], 200)
        self.assertEqual(flow_actual["volume_delta"], 600)

        # 2. Only OHLCV candles, allow_estimation=True -> mode MUST be ESTIMATED
        flow_est = vf.compute_order_flow(self.sample_bars, ticks=None, allow_estimation=True)
        self.assertEqual(flow_est["aggressor_mode"], vf.AGGRESSOR_ESTIMATED)
        self.assertFalse(flow_est["is_actual"])
        self.assertTrue(flow_est["is_estimated"])
        self.assertIsNotNone(flow_est["volume_delta"])
        self.assertIn("Close Location Value", flow_est["estimation_method"])

        # 3. Only OHLCV candles, allow_estimation=False -> mode MUST be UNAVAILABLE
        flow_unavail = vf.compute_order_flow(self.sample_bars, ticks=None, allow_estimation=False)
        self.assertEqual(flow_unavail["aggressor_mode"], vf.AGGRESSOR_UNAVAILABLE)
        self.assertFalse(flow_unavail["is_actual"])
        self.assertFalse(flow_unavail["is_estimated"])
        self.assertIsNone(flow_unavail["buy_volume"])
        self.assertIsNone(flow_unavail["volume_delta"])

    def test_market_depth_and_spread(self):
        depth = {
            "buy": [{"price": 100.10, "qty": 1000}, {"price": 100.05, "qty": 1500}],
            "sell": [{"price": 100.20, "qty": 500}, {"price": 100.25, "qty": 500}],
        }
        res = vf.compute_market_depth_metrics(depth)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["best_bid"], 100.10)
        self.assertEqual(res["best_ask"], 100.20)
        self.assertEqual(res["bid_ask_spread"], 0.10)
        self.assertEqual(res["total_bid_qty"], 2500)
        self.assertEqual(res["total_ask_qty"], 1000)
        self.assertGreater(res["order_book_imbalance"], 0.4)
        self.assertEqual(res["liquidity_bias"], "BUY_HEAVY")


if __name__ == "__main__":
    unittest.main()
