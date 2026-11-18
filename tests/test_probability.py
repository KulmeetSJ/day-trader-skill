"""scripts/probability.py: backtest odds table (offline, synthetic trades)."""

import os
import sys
import unittest

from _load import load  # noqa: F401  (skill folders on sys.path)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
import probability  # noqa: E402


def t(time, tatr, outcome, date="2026-10-01"):
    return {"time": time, "target_atr": tatr, "outcome": outcome, "date": date}


class TestProbability(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(probability.time_bucket("09:40"), "before 10:30")
        self.assertEqual(probability.time_bucket("10:30"), "10:30-12:30")
        self.assertEqual(probability.time_bucket("14:05"), "after 12:30")
        self.assertTrue(probability.dist_bucket(2.0, (3.0, 5.0)).startswith("near"))
        self.assertTrue(probability.dist_bucket(6.0, (3.0, 5.0)).startswith("far"))

    def test_counts_and_shrinkage(self):
        trades = [t("09:40", 2.0, "target")] * 10 + [t("09:40", 2.0, "stop")] * 10 \
            + [t("13:00", 2.0, "flat_red")] * 80
        m = probability.fit(trades, ("time",), (3.0, 5.0))
        self.assertAlmostEqual(m["base"]["target"], 0.10)
        early = m["table"]["before 10:30"]
        self.assertEqual(early["n"], 20)
        # raw 50% target, shrunk toward the 10% base with 30 pseudo-trades: (10 + 3) / 50
        self.assertAlmostEqual(early["p"]["target"], 13.0 / 50)
        self.assertAlmostEqual(sum(early["p"].values()), 1.0)

    def test_unseen_cell_falls_back_to_base_rate(self):
        m = probability.fit([t("09:40", 2.0, "stop"), t("09:45", 2.0, "flat_green")], ("time",), (3.0, 5.0))
        o = probability.predict(m, "14:00", 2.0)
        self.assertEqual(o["n"], 0)
        self.assertEqual(o["p"], m["base"])

    def test_odds_for_missing_inputs_gives_no_number(self):
        m = probability.fit([t("09:40", 2.0, "stop")], ("time", "dist"), (3.0, 5.0))
        self.assertIsNone(probability.odds_for(m, "09:40", 100, 104, None))
        self.assertIsNone(probability.odds_for(None, "09:40", 100, 104, 1.0))
        o = probability.odds_for(m, "09:40", 100, 104, 2.0)
        self.assertAlmostEqual(o["target_atr"], 2.0)
        self.assertIn("odds from 1 similar", probability.odds_text(o, dict(m, beats_base_rate_on_holdout=True)))
        self.assertIn("odds n/a", probability.odds_text(None, None))

    def test_brier_perfect_vs_wrong(self):
        m = probability.fit([t("09:40", 2.0, "stop")] * 5, (), (3.0, 5.0))
        self.assertAlmostEqual(probability.brier(m, [t("09:40", 2.0, "stop")]), 0.0)
        self.assertAlmostEqual(probability.brier(m, [t("09:40", 2.0, "target")]), 2.0)


class TestOddsCheckAndCsv(unittest.TestCase):
    def test_scoreboard_odds_check(self):
        import scoreboard
        lines = scoreboard.odds_check("X", [("0.1", "0.2", "TARGET HIT"), ("0.1", "0.3", "STOP HIT"),
                                            ("", "", "OPEN")])
        self.assertIn("with odds: 2 | targets expected 0.2, hit 1 | stops expected 0.5, hit 1", lines[0])
        self.assertEqual(scoreboard.odds_check("X", [("", "", "OPEN")]), [])

    def test_append_row_migrates_old_header(self):
        import csv
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location("market_watch", os.path.join(BASE, "scripts", "market_watch.py"))
        mw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mw)
        path = os.path.join(tempfile.mkdtemp(), "x.csv")
        with open(path, "w", newline="") as fh:
            fh.write("a,b\n1,2\n")
        mw.append_row(path, ("a", "b", "c"), {"a": 3, "b": 4, "c": 5})
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["a"] for r in rows], ["1", "3"])
        self.assertEqual((rows[0]["c"], rows[1]["c"]), ("", "5"))


if __name__ == "__main__":
    unittest.main()
