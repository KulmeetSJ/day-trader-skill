"""trader-options/positional.py + scripts/options_watch.py: spread building, fail-closed checks,
settlement, schedule, one-open-at-a-time ledger (offline, fixture chains)."""

import datetime
import os
import sys
import tempfile
import unittest

from _load import load  # noqa: F401  (skill folders on sys.path)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("trader-options", "scripts"):
    sys.path.insert(0, os.path.join(BASE, d))
import positional as pos  # noqa: E402
import options_watch as ow  # noqa: E402


def leg(sym, bid, ask, delta):
    return {"symbol": sym, "bid": bid, "ask": ask, "last_price": (bid + ask) / 2 if bid and ask else None,
            "greeks": {"delta": delta}, "iv": 13.0, "open_interest": 1000}


def chain(spot=25000.0):
    strikes = []
    for i, k in enumerate(range(24500, 25650, 50)):
        dc = max(0.02, 0.5 - (k - spot) / 1000.0)
        dp = -max(0.02, 0.5 - (spot - k) / 1000.0)
        prem_c, prem_p = max(1.0, 200 - (k - spot) * 0.3), max(1.0, 200 - (spot - k) * 0.3)
        strikes.append({"strike": float(k), "ce": leg("C%d" % k, prem_c - 0.5, prem_c + 0.5, dc),
                        "pe": leg("P%d" % k, prem_p - 0.5, prem_p + 0.5, dp)})
    return {"underlying_price": spot, "expiry": "2026-12-29", "tier": "REAL_TIME", "strikes": strikes}


class TestBuild(unittest.TestCase):
    def test_bear_call_picks_otm_strike_near_delta_and_hedges_higher(self):
        sp = pos.build_spread(chain(), "CE", 0.20, 2, 65, 50, 6000)
        self.assertEqual(sp["short"]["strike"], 25300.0)            # delta 0.20 in the fixture
        self.assertEqual(sp["long"]["strike"], 25400.0)
        self.assertAlmostEqual(sp["credit"], sp["short"]["bid"] - sp["long"]["ask"])
        self.assertAlmostEqual(sp["max_loss_inr"], (100 - sp["credit"]) * 65 + sp["charges_inr"], delta=1)
        self.assertGreater(sp["breakeven"], sp["short"]["strike"])

    def test_bull_put_hedges_lower(self):
        sp = pos.build_spread(chain(), "PE", 0.20, 2, 65, 50, 6000)
        self.assertLess(sp["short"]["strike"], 25000)
        self.assertEqual(sp["short"]["strike"] - sp["long"]["strike"], 100)

    def test_fail_closed(self):
        c = chain()
        for s in c["strikes"]:
            if s["strike"] == 25300.0:
                s["ce"]["bid"] = None
        with self.assertRaises(pos.SpreadError):
            pos.build_spread(c, "CE", 0.20, 2, 65, 50, 6000)       # no live bid on the short leg
        c = chain()
        for s in c["strikes"]:
            if s["strike"] == 25300.0:
                s["ce"]["bid"], s["ce"]["ask"] = 50.0, 80.0
        with self.assertRaises(pos.SpreadError):
            pos.build_spread(c, "CE", 0.20, 2, 65, 50, 6000)       # illiquid
        with self.assertRaises(pos.SpreadError):
            pos.build_spread(chain(), "CE", 0.20, 2, 65, 50, 3000)  # over the spread budget

    def test_missing_deltas_is_a_rejection(self):
        c = chain()
        for s in c["strikes"]:
            s["ce"]["greeks"] = {}
        with self.assertRaises(pos.SpreadError):
            pos.build_spread(c, "CE", 0.20, 2, 65, 50, 6000)


class TestSettle(unittest.TestCase):
    def test_settle_value(self):
        self.assertEqual(pos.settle_value(25000, 25300, 25400, "CE"), 0)
        self.assertEqual(pos.settle_value(25350, 25300, 25400, "CE"), 50)
        self.assertEqual(pos.settle_value(26000, 25300, 25400, "CE"), 100)
        self.assertEqual(pos.settle_value(24650, 24700, 24600, "PE"), 50)

    def test_trend_side(self):
        self.assertEqual(pos.trend_side([100.0] * 19 + [110.0]), "PE")
        self.assertEqual(pos.trend_side([100.0] * 19 + [90.0]), "CE")


class TestSchedule(unittest.TestCase):
    def test_entry_day_is_n_trading_days_before_expiry(self):
        rule = dict(pos.RULES[0])
        exp, entry = pos.entry_expiry(datetime.date(2026, 12, 1), rule)
        self.assertEqual(exp, datetime.date(2026, 12, 29))
        self.assertEqual(pos.trading_days_until(entry, exp), rule["days_before"])

    def test_rolls_to_next_month_after_entry_day(self):
        exp, entry = pos.entry_expiry(datetime.date(2026, 11, 19), dict(pos.RULES[0]))
        self.assertEqual(exp, datetime.date(2026, 12, 29))
        self.assertGreater(entry, datetime.date(2026, 11, 19))


class TestWatchLedger(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.orig = (ow.LEDGER_CSV, ow.CALLS_CSV, ow.evaluate, ow.notify, ow.provider)
        ow.LEDGER_CSV, ow.CALLS_CSV = os.path.join(d, "l.csv"), os.path.join(d, "c.csv")
        ow.notify = lambda *a, **k: None
        ow.provider = lambda: None
        self.orig_cf = ow.capital_free
        ow.capital_free = lambda: (100000.0, 100000.0)
        sp = pos.build_spread(chain(), "CE", 0.20, 2, 65, 50, 6000)
        ow.evaluate = lambda rule, expiry, p=None, as_of=None: (dict(sp, expiry=expiry.isoformat()), None)

    def tearDown(self):
        ow.LEDGER_CSV, ow.CALLS_CSV, ow.evaluate, ow.notify, ow.provider = self.orig
        ow.capital_free = self.orig_cf

    def test_one_open_spread_at_a_time(self):
        nifty, bank = pos.RULES
        _, e1 = pos.entry_expiry(datetime.date(2026, 12, 1), nifty)
        _, e2 = pos.entry_expiry(datetime.date(2026, 12, 1), bank)
        ow.check(today=e2)              # BANKNIFTY entry day: opens one
        ow.check(today=e1)              # NIFTY entry day: blocked, one already open
        led = ow.read_csv(ow.LEDGER_CSV)
        self.assertEqual(len(led), 1)
        calls = ow.read_csv(ow.CALLS_CSV)
        self.assertEqual([c["status"] for c in calls], ["CALL", "OPEN_RISK"])

    def test_capital_cap_blocks_spread_that_does_not_fit(self):
        orig = ow.capital_free
        ow.capital_free = lambda: (1000.0, 100000.0)     # Rs 99,000 already committed elsewhere
        try:
            _, e2 = pos.entry_expiry(datetime.date(2026, 12, 1), pos.RULES[1])
            ow.check(today=e2)
        finally:
            ow.capital_free = orig
        self.assertEqual(ow.read_csv(ow.LEDGER_CSV), [])
        self.assertEqual([c["status"] for c in ow.read_csv(ow.CALLS_CSV)], ["CAPITAL_CAP"])

    def test_not_entry_day_does_nothing(self):
        ow.check(today=datetime.date(2026, 12, 2))
        self.assertEqual(ow.read_csv(ow.LEDGER_CSV), [])


if __name__ == "__main__":
    unittest.main()
