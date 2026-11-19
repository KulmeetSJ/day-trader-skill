"""scripts/equity_calls.py: labels, ledger cap, paper walk, news matching (offline)."""

import datetime
import os
import sys
import unittest

from _load import load  # noqa: F401  (skill folders on sys.path)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
import equity_calls as ec  # noqa: E402

IST = ec.IST


def b(date, o, h, l, c):
    return {"date": date, "o": o, "h": h, "l": l, "c": c, "v": 1000}


def idea(sym, risk, label="RESEARCH", qty=10, entry_ref=100.0):
    return {"scan_date": "2026-11-18", "symbol": sym, "setup": "bull_flag", "label": label, "stop": 95.0,
            "target": 112.5, "qty": qty, "entry_ref": entry_ref, "max_loss_inr": risk, "hold_days": 60}


class TestNews(unittest.TestCase):
    def test_company_match(self):
        self.assertFalse(ec.about_company("Gail Kim & TNA Lawsuit", "GAIL", "GAIL (India) Ltd."))
        self.assertTrue(ec.about_company("GAIL shares rise", "GAIL", "GAIL (India) Ltd."))
        self.assertTrue(ec.about_company("asian paints profit up", "ASIANPAINT", "Asian Paints Ltd."))
        self.assertEqual(ec.short_name("Mahindra & Mahindra Ltd."), "Mahindra")


class TestCompletedBar(unittest.TestCase):
    def test_drops_running_session_only(self):
        bars = [b("2026-11-18", 1, 1, 1, 1), b("2026-11-19", 1, 1, 1, 1)]
        during = datetime.datetime(2026, 11, 19, 11, 0, tzinfo=IST)
        after = datetime.datetime(2026, 11, 19, 16, 0, tzinfo=IST)
        self.assertEqual(len(ec.completed(bars, during)), 1)
        self.assertEqual(len(ec.completed(bars, after)), 2)


class TestOdds(unittest.TestCase):
    def test_label_only_from_a_passing_row(self):
        odds = {("vcp", 60, "any"): {"passes": False, "holdout": {"n": 50}},
                ("vcp", 40, "nifty_up"): {"passes": True, "holdout": {"n": 120}}}
        hold, row = ec.odds_for(odds, "vcp", True)
        self.assertEqual((hold, row["passes"]), (40, True))
        hold, row = ec.odds_for(odds, "vcp", False)
        self.assertEqual((hold, row["passes"]), (60, False))


class TestLedger(unittest.TestCase):
    def test_open_risk_cap_and_one_per_stock(self):
        ledger = [{"symbol": "A", "status": "OPEN", "max_loss_inr": "2000"}]
        ideas = [idea("A", 1500), idea("B", 2000), idea("C", 1500)]
        ec.add_to_ledger(ledger, ideas, cap=5000)
        self.assertEqual([i["ledger"] for i in ideas], ["ALREADY_OPEN", "ADDED", "OPEN_RISK"])
        self.assertEqual(ec.open_risk(ledger), 4000)

    def test_capital_cap_takes_ideas_only_while_they_fit(self):
        # Rs 1,00,000 cap with Rs 30,000 already tied up elsewhere -> Rs 70,000 free.
        ideas = [idea("A", 1000, qty=400, entry_ref=100.0),    # Rs 40,000: fits (30,000 left)
                 idea("B", 1000, qty=500, entry_ref=100.0),    # Rs 50,000: doesn't fit
                 idea("C", 1000, qty=250, entry_ref=100.0)]    # Rs 25,000: fits (5,000 left)
        ledger = ec.add_to_ledger([], ideas, cap=5000, capital_free=70000.0)
        self.assertEqual([i["ledger"] for i in ideas], ["ADDED", "CAPITAL_CAP", "ADDED"])
        self.assertEqual([r["entry_ref"] for r in ledger], [100.0, 100.0])   # capital is countable before the fill

    def row(self):
        return {"id": "x", "symbol": "X", "setup": "s", "label": "RESEARCH", "signal_date": "2026-11-18",
                "status": "PENDING", "stop": "95", "target": "112.5", "qty": "10", "max_loss_inr": "60", "max_hold": "3"}

    def test_fills_next_open_then_target(self):
        r = ec.walk(self.row(), [b("2026-11-18", 99, 101, 98, 100), b("2026-11-19", 100, 104, 99, 103),
                                 b("2026-11-20", 104, 113, 103, 112)])
        self.assertEqual((r["status"], r["entry"], r["exit_price"]), ("TARGET", 100, 112.5))
        self.assertGreater(float(r["net_inr"]), 0)

    def test_gap_below_stop_exits_at_open(self):
        r = ec.walk(self.row(), [b("2026-11-19", 100, 101, 99, 100), b("2026-11-20", 92, 93, 90, 91)])
        self.assertEqual((r["status"], r["exit_price"]), ("STOP", 92))

    def test_time_exit_after_max_hold(self):
        bars = [b("2026-11-%d" % d, 100, 101, 99, 100) for d in (19, 20, 23, 24)]
        r = ec.walk(self.row(), bars)
        self.assertEqual((r["status"], r["exit_date"]), ("TIME", "2026-11-23"))

    def test_still_open(self):
        r = ec.walk(self.row(), [b("2026-11-19", 100, 101, 99, 100)])
        self.assertEqual(r["status"], "OPEN")


if __name__ == "__main__":
    unittest.main()
