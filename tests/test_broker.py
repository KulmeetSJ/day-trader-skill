import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-tsla.json")
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"
AAPL = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
BATCH = ["AAPL", "AXP", "GOOGL", "KO", "BAC"]
TSLA_CALL = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
TSLA_PLAIN = ("BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about "
              "$46.88 if the stop fills at 358.32 (it can fill worse in a fast market)")


def tsla(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=AAPL, batch=BATCH):
        return ticket.parse_yes(reply, order, batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes AAPL", "yes $1,250 of AAPL",
                  "yes 1250"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes AXP", "yes 1000", "yes but make it 1000", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 340"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        sell = {"symbol": "KO", "side": "sell", "qty": "3", "type": "market", "time_in_force": "day"}
        self.assertTrue(self.yes("yes sell 3 KO", sell))
        self.assertFalse(self.yes("yes sell 4", sell))

    def test_a_bracket_yes(self):
        o = tsla()
        for r in ("yes", "yes 8 TSLA", "yes 358.32", "yes max loss 46.88", "yes target 370.04"):
            self.assertTrue(self.yes(r, o, ["TSLA"]), r)
        for r in ("yes 9", "yes 10 TSLA", "yes but stop at 357", "yes, stop 358.32", "yes AAPL", "sure"):
            self.assertFalse(self.yes(r, o, ["TSLA", "AAPL"]), r)

    def test_ticker_side_and_qty_must_match_selected_order(self):
        rel = {
            "symbol": "RELIANCE", "side": "buy", "qty": "26", "type": "limit",
            "limit_price": "1326.00", "time_in_force": "day", "order_class": "bracket",
            "take_profit_limit_price": "1350.00", "stop_loss_stop_price": "1310.00",
            "market": "IN", "currency": "INR",
        }
        # Correct symbol, side, qty and valid short forms
        for r in ("yes", "y", "yes buy 26 RELIANCE", "yes buy 26 reliance", "yes 26 RELIANCE",
                  "yes buy 26 RELIANCE.NS", "yes buy 26 RELIANCE at 1326.00", "yes place it", "Yes, buy it"):
            self.assertTrue(self.yes(r, rel, ["RELIANCE"]), r)

        # Wrong ticker NOT in batch must be rejected (reported bug: BUY 26 RELIANCE vs 'yes buy 26 TCS')
        self.assertFalse(ticket.parse_yes("yes buy 26 TCS", rel)[0])
        self.assertTrue(ticket.parse_yes("yes buy 26 RELIANCE", rel)[0])
        for r in ("yes buy 26 TCS", "yes buy 26 tcs", "yes TCS", "yes INFY",
                  "yes buy 26 RELIANCE and TCS", "yes buy 26 IT", "yes buy 26 ON", "yes buy 26 RELIANCE.BO"):
            if r == "yes buy 26 RELIANCE.BO":
                rel_ns = dict(rel, symbol="RELIANCE.NS")
                self.assertFalse(self.yes(r, rel_ns, ["RELIANCE.NS"]), r)
            else:
                self.assertFalse(self.yes(r, rel, ["RELIANCE"]), r)

        # Wrong ticker present in batch must be rejected for the selected order
        for r in ("yes buy 26 TCS", "yes TCS", "yes tcs"):
            self.assertFalse(self.yes(r, rel, ["RELIANCE", "TCS"]), r)

        # Wrong side must be rejected
        for r in ("yes sell 26 RELIANCE", "yes sell 26", "yes sell it", "yes short 26 RELIANCE",
                  "yes buy or sell 26 RELIANCE"):
            self.assertFalse(self.yes(r, rel, ["RELIANCE"]), r)
        rel_sell = dict(rel, side="sell")
        for r in ("yes buy 26 RELIANCE", "Yes, buy it", "yes bought 26 RELIANCE"):
            self.assertFalse(self.yes(r, rel_sell, ["RELIANCE"]), r)
        self.assertTrue(self.yes("yes sell 26 RELIANCE", rel_sell, ["RELIANCE"]))

        # Wrong quantity or second mismatched number must be rejected
        for r in ("yes buy 25 RELIANCE", "yes buy 26 RELIANCE at 1400", "yes buy 25 RELIANCE at 1326",
                  "yes buy 1326 RELIANCE", "yes 1326 shares of RELIANCE", "yes buy -26 RELIANCE"):
            self.assertFalse(self.yes(r, rel, ["RELIANCE"]), r)
        self.assertFalse(self.yes("yes buy 358.32 TSLA", tsla(), ["TSLA"]))
        self.assertFalse(self.yes("yes buy 8 TSLA limit 999", tsla(), ["TSLA"]))


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_alpaca_call(self):
        o = dict(AAPL, why="from a list")
        self.assertEqual(ticket.tool_input(o), AAPL)
        self.assertEqual(ticket.tool_input({"symbol": "KO", "side": "buy", "qty": 3, "type": "limit",
                                            "limit_price": 89.5, "time_in_force": "day"}),
                         {"symbol": "KO", "side": "buy", "qty": "3", "type": "limit", "limit_price": "89.5",
                          "time_in_force": "day"})

    def test_the_bracket_call_leaves_out_why_and_plan(self):
        self.assertEqual(ticket.tool_input(tsla()), TSLA_CALL)
        self.assertEqual(ticket.fingerprint(tsla()), ticket.fingerprint(TSLA_CALL))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(AAPL), "BUY $1,250 of AAPL, market order, good for today")
        self.assertEqual(ticket.plain({"symbol": "KO", "side": "sell", "qty": "1", "type": "limit",
                                       "limit_price": "95", "time_in_force": "gtc"}),
                         "SELL 1 share of KO, limit order at $95, good until canceled")
        self.assertEqual(ticket.plain(tsla()), TSLA_PLAIN)
        lim = tsla(type="limit", limit_price="364.00")
        self.assertEqual(ticket.plain(lim).split(" / ")[0], "BUY 8 TSLA, limit 364.00, good for today")
        self.assertEqual(ticket.max_loss(lim), (45.44, 358.32))
        no_plan = dict(TSLA_CALL)
        self.assertEqual(ticket.max_loss(no_plan), (None, 358.32))


class DesktopConfig(unittest.TestCase):
    def test_example_is_paper_with_placeholder_keys(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            server = json.load(fh)["mcpServers"]["alpaca"]
        self.assertEqual((server["command"], server["args"]), ("uvx", ["alpaca-mcp-server"]))
        self.assertEqual(server["env"]["ALPACA_PAPER_TRADE"], "true")
        for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
            self.assertIn("NEVER_IN_CHAT", server["env"][k])


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_full_two_yes_flow(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER account, Alpaca", text)
        for part in TSLA_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn('"order_class": "bracket"', text)
        self.assertIn('"stop_loss_stop_price": "358.32"', text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertIn("Allow once", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("place_stock_order", text)
        self.assertEqual(json.loads(text[text.index("{"):text.rindex("}") + 1]), TSLA_CALL)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc123",
                                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf,
                                      now="2026-10-19 10:16"))
        self.assertEqual(code, 0)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"]), ("yes", "entry", 46.88, TODAY))
        self.assertEqual(appr["at"], "2026-10-19T10:15:00-04:00")
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["kind"]), ("abc123", 46.88, "entry"))
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 2086.4)
        self.assertEqual(pf["positions"][0]["shares"], 8)

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss $46.88", text)

        # trader-risk now counts it: a second TSLA buy today is a duplicate, and $46.88 is used
        res = check.check([tsla()], {"budget": 5000}, state=st, now=check.parse_now("2026-10-19 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 46.88))

    def test_flatten_by_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc",
                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf))
        late = "2026-10-19 15:55"
        code, text, _ = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 8 shares of TSLA, market order, good for today (flat by 15:55 New York)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancel_order_by_id", text)
        with open(self.orders) as fh:
            doc = json.load(fh)
        self.assertEqual(ticket.tool_input(doc["orders"][0]),
                         {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"})
        self.assertEqual(self.run_risk(now=late), 0)       # after the 15:30 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 8 TSLA", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "8",
                                   "--filled-price", "366.10", "--portfolio", self.pf, now="2026-10-19 15:56"))
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual((pf["cash"], pf["positions"]), (5015.2, []))
        self.assertEqual(check.day_trades(self.state_doc(), TODAY), (1, ["2026-10-19 TSLA"]))
        code, _, err = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "tsla", "--qty", "8", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against get_open_position", text)
        self.assertEqual(run(["flatten", "--symbol", "TSLA", "--qty", "0", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "9"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-16 10:15")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-19 09:40"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:40"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:45", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 09:40"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_ibkr_tickets_send_the_final_yes_to_ibkr(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["broker"] = "ibkr"
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("Interactive Brokers", text)
        self.assertIn("after you approve it there", text)
        self.assertNotIn("place_stock_order", text)

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "KO", "shares": 3, "lots": [
            {"id": "KO-1", "date": "2025-01-02", "shares": 2, "cost": 120.0},
            {"id": "KO-2", "date": "2026-01-02", "shares": 1, "cost": 70.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "2.5", "--price", "90",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 225.0)
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["KO-2"])
        self.assertEqual(lots[0]["shares"], 0.5)
        self.assertEqual(lots[0]["cost"], 35.0)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "5", "--price", "90",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


class IndianPortfolioAccountingTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "config.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.state = os.path.join(self.d, "broker.json")
        self.orders = os.path.join(self.d, "orders.json")
        self.risk = os.path.join(self.d, "risk.json")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"budget": 100000, "paper": True, "market": "IN", "currency": "INR",
                       "max_position_pct": 90, "max_order_usd": 95000, "max_loss_per_trade_usd": 2000,
                       "max_daily_loss_usd": 5000, "require_stop": True, "min_reward_risk": 1.0,
                       "no_entries_before": "09:30", "no_entries_after": "15:00"}, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_reliance_roundtrip_buy_56_sell_56_reconciles_pnl_and_cash(self):
        # BUY 56 RELIANCE @ Rs 1,326.00 (cost = Rs 74,256.00), then SELL 56 @ Rs 1,324.40 (proceeds = Rs 74,166.40)
        # Gross realized P&L before charges = Rs 74,166.40 - Rs 74,256.00 = -Rs 89.60
        # Cash = Rs 100,000 - Rs 74,256.00 + Rs 74,166.40 = Rs 99,910.40
        code1, out1, _ = run([
            "fill", "--symbol", "RELIANCE", "--side", "buy", "--qty", "56", "--price", "1326",
            "--date", "2026-10-19", "--now", "2026-10-19 10:35", "--market", "IN",
            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state
        ])
        self.assertEqual(code1, 0)
        with open(self.pf, encoding="utf-8") as fh:
            pf1 = json.load(fh)
        self.assertEqual(pf1["cash"], 25744.0)
        self.assertEqual(pf1["positions"][0]["shares"], 56.0)
        self.assertEqual(pf1["positions"][0]["lots"][0]["cost"], 74256.0)

        code2, out2, _ = run([
            "fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "56", "--price", "1324.40",
            "--date", "2026-10-19", "--now", "2026-10-19 15:20", "--market", "IN",
            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state
        ])
        self.assertEqual(code2, 0)
        self.assertIn("-₹89.60", out2)
        with open(self.pf, encoding="utf-8") as fh:
            pf2 = json.load(fh)
        self.assertEqual(pf2["cash"], 99910.4)
        self.assertEqual(pf2["realized_pnl"], -89.6)
        self.assertEqual(pf2["positions"], [])

    def test_partial_fills_multiple_positions_and_non_colliding_lot_ids(self):
        # Buy two RELIANCE lots and one TCS lot; relieve first RELIANCE lot completely + partial second
        for sym, side, qty, px, d in (
            ("RELIANCE", "buy", "30", "1300", "2026-10-15"),
            ("RELIANCE", "buy", "26", "1356", "2026-10-16"),
            ("TCS", "buy", "5", "4000", "2026-10-16"),
            ("RELIANCE", "sell", "40", "1340", "2026-10-19"),
        ):
            code, _, _ = run([
                "fill", "--symbol", sym, "--side", side, "--qty", qty, "--price", px,
                "--date", d, "--now", d + " 11:00", "--market", "IN",
                "--portfolio", self.pf, "--config", self.cfg, "--state", self.state
            ])
            self.assertEqual(code, 0)

        # Next RELIANCE buy must receive ID RELIANCE-3, not collide with RELIANCE-2
        code_b3, _, _ = run([
            "fill", "--symbol", "RELIANCE", "--side", "buy", "--qty", "4", " --price".strip(), "1350",
            "--date", "2026-10-19", "--now", "2026-10-19 12:00", "--market", "IN",
            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state
        ])
        self.assertEqual(code_b3, 0)
        with open(self.pf, encoding="utf-8") as fh:
            pf = json.load(fh)
        pos_map = {p["ticker"]: p for p in pf["positions"]}
        self.assertEqual(set(pos_map.keys()), {"RELIANCE", "TCS"})
        self.assertEqual(pos_map["RELIANCE"]["shares"], 20.0)
        self.assertEqual([l["id"] for l in pos_map["RELIANCE"]["lots"]], ["RELIANCE-2", "RELIANCE-3"])
        self.assertEqual(pos_map["TCS"]["shares"], 5.0)
        # Cash: 100000 - (30*1300 + 26*1356 + 5*4000) + 40*1340 - 4*1350
        # = 100000 - (39000 + 35256 + 20000) + 53600 - 5400 = 53944.0
        self.assertEqual(pf["cash"], 53944.0)

    def test_duplicate_result_and_atomic_fill_validation(self):
        order = {
            "symbol": "RELIANCE", "side": "buy", "qty": "56", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "1350.00", "stop_loss_stop_price": "1310.00",
            "market": "IN", "currency": "INR",
            "plan": {"symbol": "RELIANCE", "entry": 1326.0, "stop": 1310.0, "target": 1350.0,
                     "risk_per_share": 16.0, "reward_risk": 1.5, "max_loss": 896.0}
        }
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump({"orders": [order]}, fh)
        with redirect_stdout(io.StringIO()):
            r_code = check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                                 "--prices", os.path.join(self.d, "none"), "--state", self.state,
                                 "--now", "2026-10-19 10:35", "--market", "IN", "--out", self.risk])
        self.assertEqual(r_code, 0)
        c_app, _, _ = run(["approve", "--index", "1", "--reply", "yes", "--orders", self.orders,
                           "--config", self.cfg, "--risk", self.risk, "--state", self.state,
                           "--now", "2026-10-19 10:35", "--market", "IN"])
        self.assertEqual(c_app, 0)

        # Reject over-fill (> approved qty 56) before mutating state
        c_over, _, err_over = run(["result", "--index", "1", "--status", "placed", "--filled-qty", "60",
                                   "--filled-price", "1326", "--orders", self.orders, "--config", self.cfg,
                                   "--risk", self.risk, "--state", self.state, "--portfolio", self.pf,
                                   "--now", "2026-10-19 10:36", "--market", "IN"])
        self.assertEqual(c_over, 1)
        self.assertIn("exceeds approved order qty", err_over)

        # Record valid placement + fill
        c_ok, _, _ = run(["result", "--index", "1", "--status", "placed", "--filled-qty", "56",
                          "--filled-price", "1326", "--orders", self.orders, "--config", self.cfg,
                          "--risk", self.risk, "--state", self.state, "--portfolio", self.pf,
                          "--now", "2026-10-19 10:36", "--market", "IN"])
        self.assertEqual(c_ok, 0)

        # Duplicate result call for the same approved order must be rejected
        c_dup, _, err_dup = run(["result", "--index", "1", "--status", "placed", "--filled-qty", "56",
                                 "--filled-price", "1326", "--orders", self.orders, "--config", self.cfg,
                                 "--risk", self.risk, "--state", self.state, "--portfolio", self.pf,
                                 "--now", "2026-10-19 10:37", "--market", "IN"])
        self.assertEqual(c_dup, 1)
        self.assertIn("no yes recorded today", err_dup)
        with open(self.pf, encoding="utf-8") as fh:
            pf_after = json.load(fh)
        self.assertEqual(pf_after["cash"], 25744.0)

    def test_multi_order_batch_confirmation_and_fingerprint_gates(self):
        rel = {
            "symbol": "RELIANCE", "side": "buy", "qty": "26", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "1350.00", "stop_loss_stop_price": "1310.00",
            "market": "IN", "currency": "INR",
            "plan": {"symbol": "RELIANCE", "entry": 1326.0, "stop": 1310.0, "target": 1350.0,
                     "risk_per_share": 16.0, "reward_risk": 1.5, "max_loss": 416.0},
        }
        tcs = {
            "symbol": "TCS", "side": "buy", "qty": "10", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "3960.00", "stop_loss_stop_price": "3880.00",
            "market": "IN", "currency": "INR",
            "plan": {"symbol": "TCS", "entry": 3900.0, "stop": 3880.0, "target": 3960.0,
                     "risk_per_share": 20.0, "reward_risk": 3.0, "max_loss": 200.0},
        }
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump({"orders": [rel, tcs]}, fh)

        # 1. Missing risk verdict must still block approval
        c_norisk, _, err_norisk = run(["approve", "--index", "1", "--reply", "yes buy 26 RELIANCE",
                                       "--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                                       "--state", self.state, "--now", "2026-10-19 10:35", "--market", "IN"])
        self.assertEqual(c_norisk, 1)
        self.assertIn("not ready", err_norisk)

        # Run trader-risk over both orders
        with redirect_stdout(io.StringIO()):
            r_code = check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                                 "--prices", os.path.join(self.d, "none"), "--state", self.state,
                                 "--now", "2026-10-19 10:35", "--market", "IN", "--out", self.risk])
        self.assertEqual(r_code, 0)

        # 2. Tampered order fingerprint must still be rejected
        tampered = [dict(rel, symbol="TCS"), tcs]
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump({"orders": tampered}, fh)
        c_tamp, _, err_tamp = run(["approve", "--index", "1", "--reply", "yes buy 26 TCS",
                                   "--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                                   "--state", self.state, "--now", "2026-10-19 10:35", "--market", "IN"])
        self.assertEqual(c_tamp, 1)
        self.assertIn("was not in the last risk check", err_tamp)
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump({"orders": [rel, tcs]}, fh)

        # 3. Wrong ticker / side / qty on selected order #1 must be rejected without mutating state
        for bad_reply in ("yes buy 26 INFY", "yes buy 26 TCS", "yes sell 26 RELIANCE", "yes buy 25 RELIANCE"):
            c_bad, out_bad, _ = run(["approve", "--index", "1", "--reply", bad_reply,
                                     "--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                                     "--state", self.state, "--now", "2026-10-19 10:35", "--market", "IN"])
            self.assertEqual(c_bad, 1, bad_reply)
            self.assertIn("Not recorded", out_bad)
        self.assertFalse(os.path.exists(self.state))

        # 4. Approving order #1 approves ONLY order #1, leaving order #2 unapproved
        c_ok1, out_ok1, _ = run(["approve", "--index", "1", "--reply", "yes buy 26 RELIANCE",
                                 "--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                                 "--state", self.state, "--now", "2026-10-19 10:35", "--market", "IN"])
        self.assertEqual(c_ok1, 0)
        self.assertIn("BUY 26 RELIANCE", out_ok1)

        with open(self.state, encoding="utf-8") as fh:
            st1 = json.load(fh)
        self.assertEqual(len(st1["orders"]), 1)
        self.assertEqual(st1["orders"][0]["symbol"], "RELIANCE")
        self.assertEqual(st1["orders"][0]["fingerprint"], ticket.fingerprint(rel))

        # Order #2 cannot record placement/fill yet because confirmation covered only order #1
        c_res2, _, err_res2 = run(["result", "--index", "2", "--status", "placed", "--filled-qty", "10",
                                   "--filled-price", "3900", "--orders", self.orders, "--config", self.cfg,
                                   "--risk", self.risk, "--state", self.state, "--portfolio", self.pf,
                                   "--now", "2026-10-19 10:36", "--market", "IN"])
        self.assertEqual(c_res2, 1)
        self.assertIn("no yes recorded today", err_res2)

        # Approving order #2 explicitly covers order #2 independently
        c_ok2, out_ok2, _ = run(["approve", "--index", "2", "--reply", "yes buy 10 TCS",
                                 "--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                                 "--state", self.state, "--now", "2026-10-19 10:36", "--market", "IN"])
        self.assertEqual(c_ok2, 0)
        self.assertIn("BUY 10 TCS", out_ok2)


if __name__ == "__main__":
    unittest.main()
