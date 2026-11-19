import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import capital  # noqa: E402


def write(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)


class TestCommitted(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.opp, self.eq, self.opt = (os.path.join(d, n) for n in ("o.csv", "e.csv", "x.csv"))
        write(self.opp, [
            {"date": "2026-11-19", "kind": "equity", "symbol": "COLPAL", "qty": "43", "entry": "1719.40"},
            {"date": "2026-11-19", "kind": "options", "symbol": "NIFTY..CE", "qty": "75", "entry": "160"},
            {"date": "2026-11-18", "kind": "equity", "symbol": "OLD", "qty": "100", "entry": "100"},   # other day
        ])
        write(self.eq, [
            {"symbol": "A", "status": "OPEN", "qty": "10", "entry": "500", "entry_ref": "490"},
            {"symbol": "B", "status": "PENDING", "qty": "20", "entry": "", "entry_ref": "100"},
            {"symbol": "C", "status": "TARGET", "qty": "99", "entry": "100", "entry_ref": "100"},     # closed
        ])
        write(self.opt, [{"symbol": "NIFTY", "status": "OPEN", "max_loss_inr": "5797"},
                         {"symbol": "NIFTY", "status": "WIN", "max_loss_inr": "5000"}])

    def kw(self, **over):
        k = dict(today="2026-11-19", opportunities=self.opp, equity_ledger=self.eq, options_ledger=self.opt)
        k.update(over)
        return k

    def test_trading_pool_is_intraday_plus_spreads(self):
        total, parts = capital.committed("trading", 100000.0, **self.kw())
        self.assertAlmostEqual(total, 43 * 1719.40 + 75 * 160 + 5797, places=2)
        self.assertEqual(len(parts), 3)

    def test_investing_pool_is_the_equity_ledger_only(self):
        total, _ = capital.committed("investing", 100000.0, **self.kw())
        self.assertAlmostEqual(total, 10 * 500 + 20 * 100)

    def test_pools_are_independent(self):
        # filling the trading pool leaves the investing pool's room untouched
        write(self.opp, [{"date": "2026-11-19", "kind": "equity", "symbol": "X", "qty": "1000", "entry": "100"}])
        self.assertEqual(capital.room("trading", 100000.0, **self.kw()), 0.0)
        self.assertAlmostEqual(capital.room("investing", 100000.0, **self.kw()), 100000.0 - 7000.0)

    def test_positional_view_leaves_out_intraday(self):
        total, _ = capital.committed("trading", 100000.0, **self.kw(opportunities=None))
        self.assertAlmostEqual(total, 5797)

    def test_unreadable_row_counts_as_whole_cap(self):
        write(self.eq, [{"symbol": "A", "status": "OPEN", "qty": "", "entry": "", "entry_ref": ""}])
        self.assertEqual(capital.committed("investing", 100000.0, **self.kw())[0], 100000.0)
        self.assertEqual(capital.room("investing", 100000.0, **self.kw()), 0.0)

    def test_cap_needs_a_budget_and_a_known_pool(self):
        self.assertEqual(capital.cap_inr({"budget": 100000.0}, "investing"), 100000.0)
        with self.assertRaises(ValueError):
            capital.cap_inr({})
        with self.assertRaises(ValueError):
            capital.cap_inr({"budget": 1.0}, "gambling")


if __name__ == "__main__":
    unittest.main()
