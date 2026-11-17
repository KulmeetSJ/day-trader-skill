"""Regression: indicator-only setups (rsi_momentum, ma_crossover, trend) have no price
level, so describing a TRIGGERED result must not format a None level (TypeError)."""

from datetime import datetime as dt, timedelta, timezone
import unittest

from _load import load
from test_entry_setups_decoupled import _make_v8_chart, _et_utc

entry = load("trader-entry", "entry")


def _rsi_cross_bars():
    t0 = int(_et_utc(2026, 10, 19, 9, 30).timestamp())
    closes = []
    px = 100.0
    for i in range(20):          # drift down: RSI well below 60
        px -= 0.3 if i % 3 else -0.1
        closes.append(px)
    for i in range(8):           # strong up-move: RSI crosses 60
        px += 0.8
        closes.append(px)
    bars = []
    prev = 100.0
    for i, c in enumerate(closes):
        bars.append({"t": t0 + 300 * i, "o": prev, "h": max(prev, c) + 0.05,
                     "l": min(prev, c) - 0.05, "c": c, "v": 10000})
        prev = c
    return bars


class IndicatorTriggerDescribe(unittest.TestCase):
    def test_rsi_momentum_triggered_describe_does_not_crash(self):
        ch, _ = _make_v8_chart("RSIX", _rsi_cross_bars(), interval="5m", market="US")
        res = entry.evaluate(ch, _et_utc(2026, 10, 19, 11, 55), setup="rsi_momentum", interval="5m")
        self.assertEqual(res["status"], "triggered")
        self.assertIsNone(res.get("level"))
        lines = entry.describe(res)
        text = "\n".join(lines) if isinstance(lines, list) else str(lines)
        self.assertIn("TRIGGERED", text)
        self.assertIn("RSI momentum", text)
        self.assertNotIn("None", text)

    def test_rsi_momentum_trigger_bar_line_does_not_crash(self):
        ch, _ = _make_v8_chart("RSIX", _rsi_cross_bars(), interval="5m", market="US")
        res = entry.evaluate(ch, _et_utc(2026, 10, 19, 11, 55), setup="rsi_momentum", interval="5m")
        trig_slot = res["trigger"]["bar_index"]
        bar = [b for b in res["_bars"] if b["slot"] == trig_slot][0]
        line = entry.bar_line(bar, res)
        self.assertIn("TRIGGERED", line)
        self.assertNotIn("None", line)


if __name__ == "__main__":
    unittest.main()
