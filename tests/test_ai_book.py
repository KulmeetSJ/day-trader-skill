"""AI paper book + MCP server (trader-mcp). Hermetic: fake provider, fixed clock, temp book and
instrument master. Paper only: nothing here can route an order."""
import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load  # noqa: E402

book_mod = load("trader-mcp", "book")
server = load("trader-mcp", "server")
import test_upstox_tick_size as tick_t  # noqa: E402  (reuse its sample NSE/BSE instrument master)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
CFG = {"market": "IN", "paper": True, "broker": "paper", "budget": 100000.0, "max_loss_per_trade_inr": 2000.0,
       "max_daily_loss_inr": 5000.0, "max_trades_per_day": 3, "min_reward_risk": 2.0,
       "no_entries_after": "15:00", "cash_buffer_pct": 10.0, "max_position_pct": 75.0, "max_order_inr": 75000.0}


class FakeProvider(object):
    def __init__(self, price=1335.40):
        self.price = price
        self.bars = []

    def get_quote(self, symbol, exchange="NSE"):
        return {"symbol": symbol, "price": self.price, "data_status": "FIXTURE"}

    def get_intraday_data(self, symbol, interval="5m", range_str="1d", exchange="NSE"):
        return {"bars": self.bars}

    def get_historical_data(self, symbol, range_str="3mo", interval="1d"):
        return {"bars": self.bars}


class Clock(object):
    def __init__(self, hhmm="10:00", day=datetime.date(2026, 10, 21)):
        self.set(hhmm, day)

    def set(self, hhmm, day=None):
        h, m = map(int, hhmm.split(":"))
        self.t = datetime.datetime.combine(day or self.t.date(), datetime.time(h, m), tzinfo=IST)

    def __call__(self):
        return self.t


def bar(start, o, h, l, c, day="2026-10-21"):
    return {"date": day, "start": start, "o": o, "h": h, "l": l, "c": c}


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.inst_path, _, _ = tick_t._write_inr_cache(d)
        self.p = FakeProvider()
        self.clock = Clock()
        self.bk = book_mod.Book(path=os.path.join(d, "book.json"), provider=self.p, cfg=dict(CFG), clock=self.clock,
                                instruments_path=self.inst_path, options_runner=lambda s, b: self.signals)
        self.signals = []


class TestEquityFlow(Base):
    def test_preview_pass_place_then_target_exit(self):
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.assertEqual(pv["verdict"], "PASS", pv["reasons"])
        self.assertIn("preview_id", pv)
        self.assertLessEqual(pv["capital_inr"], 100000.0)
        pos = self.bk.place(pv["preview_id"], "Breakout over the opening range with NIFTY firm; stop under the base.")
        self.assertEqual((pos["status"], pos["pool"], pos["style"]), ("OPEN", "trading", "intraday"))
        self.assertAlmostEqual(pos["entry"], 1335.45)                     # +1 tick paper slippage
        self.p.bars = [bar("10:00", 1335.4, 1340, 1330, 1338), bar("10:05", 1338, 1377, 1337, 1375)]
        self.clock.set("10:10")
        res = self.bk.mark()
        self.assertEqual([c["exit_reason"] for c in res["closed"]], ["TARGET"])
        closed = res["closed"][0]
        self.assertGreater(closed["net_inr"], 0)
        self.assertLess(closed["net_inr"], closed["gross_inr"])          # charges deducted
        s = self.bk.summary()["pools"]["trading"]
        self.assertEqual((s["trades_closed"], s["wins"], s["committed_inr"]), (1, 1, 0))
        ev = [j["event"] for j in self.bk.journal_tail()]
        self.assertEqual(ev, ["OPEN", "CLOSE"])

    def test_stop_checked_before_target(self):
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.bk.place(pv["preview_id"], "Testing the stop path with a reasoned entry.")
        self.p.bars = [bar("10:00", 1335, 1380, 1315, 1330)]           # touches both: stop wins
        self.clock.set("10:05")
        c = self.bk.mark()["closed"][0]
        self.assertEqual(c["exit_reason"], "STOP")
        self.assertLess(c["net_inr"], 0)

    def test_intraday_flat_by_1520(self):
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.bk.place(pv["preview_id"], "Entry to test the 15:20 flat-by.")
        self.p.bars = [bar("10:05", 1335, 1340, 1330, 1338)]
        self.clock.set("15:21")
        self.assertEqual(self.bk.mark()["closed"][0]["exit_reason"], "FLAT_BY")

    def test_risk_block_is_final(self):
        # stop so far that Rs 2,000 max loss leaves R:R or sizing impossible -> refused, never resized
        with self.assertRaises(book_mod.BookError):
            self.bk.preview_equity("trading", "RELIANCE", stop=1000.0, target=1340.0)

    def test_window_block_after_cutoff(self):
        self.clock.set("15:10")
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.assertEqual(pv["verdict"], "BLOCK")
        self.assertNotIn("preview_id", pv)
        self.assertTrue(any("window" in r for r in pv["reasons"]), pv["reasons"])

    def test_rationale_required_and_preview_expires(self):
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        with self.assertRaises(book_mod.BookError):
            self.bk.place(pv["preview_id"], "")
        self.clock.set("10:11")
        with self.assertRaises(book_mod.BookError):
            self.bk.place(pv["preview_id"], "Valid rationale but too late now.")

    def test_chasing_fill_refused(self):
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.p.price = 1345.0                                            # +0.62R past the previewed entry
        with self.assertRaises(book_mod.BookError) as cm:
            self.bk.place(pv["preview_id"], "Price ran away but I still want it.")
        self.assertIn("chasing", str(cm.exception))

    def test_long_only(self):
        with self.assertRaises(book_mod.BookError):
            self.bk.preview_equity("trading", "RELIANCE", stop=1350.0, target=1300.0)

    def test_duplicate_blocked_and_pools_independent(self):
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.bk.place(pv["preview_id"], "First RELIANCE entry in the trading pool.")
        again = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.assertEqual(again["verdict"], "BLOCK")                     # trader-risk: duplicate same day
        self.assertTrue(any("duplicate" in r for r in again["reasons"]), again["reasons"])
        # the investing pool has its own Rs 1,00,000 and its own order records
        inv = self.bk.preview_equity("investing", "RELIANCE", stop=1290.0, target=1450.0)
        self.assertEqual(inv["pool_free_inr"], 100000.0)
        self.assertEqual(inv["verdict"], "PASS", inv["reasons"])

    def test_trades_per_day_limit(self):
        self.bk.cfg["max_trades_per_day"] = 1
        pv = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.bk.place(pv["preview_id"], "The one entry allowed today.")
        self.bk.close(self.bk.summary()["pools"]["trading"]["open"][0]["id"], "testing re-entry")
        again = self.bk.preview_equity("trading", "RELIANCE", stop=1320.0, target=1376.0)
        self.assertEqual(again["verdict"], "BLOCK")
        self.assertTrue(any("trades" in r for r in again["reasons"]), again["reasons"])


class TestOptions(Base):
    SIG = {"strategy": "BUY_CE", "_validated": True, "contract": "NIFTY26NOV27000CE", "underlying_instrument": "NIFTY",
           "expiry": "2026-11-24", "strike": 27000.0, "option_type": "CE", "current_premium": 100.0,
           "stop_loss": 90.0, "target_1": 110.0, "target_2": 125.0, "capital_margin_requirement": 6500.0,
           "maximum_loss": 700.0, "expected_risk_reward_after_charges": 2.2, "signal_state": "SIGNAL VALIDATED",
           "position_sizing": {"lots": 1, "total_quantity": 65}}

    def test_only_validated_buys_get_a_preview(self):
        self.signals = [dict(self.SIG), dict(self.SIG, strategy="SELL_CE"), dict(self.SIG, _validated=False)]
        rows = self.bk.preview_options("NIFTY")
        self.assertEqual([r["verdict"] for r in rows], ["PASS", "BLOCK", "BLOCK"])
        pos = self.bk.place(rows[0]["preview_id"], "NIFTY reclaimed VWAP with breadth; buying the ATM call.")
        self.assertEqual((pos["kind"], pos["qty"], pos["entry"]), ("option", 65, 100.05))

    def test_option_daily_loss_limit(self):
        self.signals = [dict(self.SIG, maximum_loss=1900.0)]
        for i in range(2):
            pid = self.bk.preview_options("NIFTY")[0]["preview_id"]
            self.bk.place(pid, "Option entry %d for the daily loss test." % i)
        pid = self.bk.preview_options("NIFTY")[0]["preview_id"]      # 3 x 1900 > 5000
        with self.assertRaises(book_mod.BookError) as cm:
            self.bk.place(pid, "Third option entry should be blocked.")
        self.assertIn("daily loss", str(cm.exception))


class TestServer(unittest.TestCase):
    def test_initialize_and_list(self):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "day-trader")
        names = [t["name"] for t in server.handle({"id": 2, "method": "tools/list"})["result"]["tools"]]
        for n in ("portfolio", "preview_equity_trade", "place_trade", "close_position", "get_signals"):
            self.assertIn(n, names)
        self.assertIsNone(server.handle({"method": "notifications/initialized"}))

    def test_refusal_is_a_tool_error_not_a_crash(self):
        orig = server._book

        class B(object):
            def place(self, pid, why):
                raise server.book_mod.BookError("unknown preview_id 'x'")
        server._book = lambda: B()
        try:
            r = server.handle({"id": 3, "method": "tools/call",
                               "params": {"name": "place_trade", "arguments": {"preview_id": "x", "rationale": "y"}}})
        finally:
            server._book = orig
        self.assertTrue(r["result"]["isError"])
        self.assertIn("REFUSED", r["result"]["content"][0]["text"])

    def test_no_live_order_tool(self):
        names = " ".join(t[0] for t in server.TOOLS)
        for bad in ("live", "broker", "upstox", "route"):
            self.assertNotIn(bad, names)


if __name__ == "__main__":
    unittest.main()
