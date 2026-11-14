"""Regression tests for market-aware currency labels, units, and JSON keys (US USD vs India INR).

Verifies that:
1. Indian workflow outputs (budget, risk, alerts, entry, exit, broker, timemachine, news)
   use INR, ₹, rupees/paise, and neutral JSON keys (amount, value, min_order,
   max_loss_per_trade) and never label INR values as value_usd / min_order_usd /
   max_loss_per_trade_usd / dollars / usd / $.
2. US workflow outputs preserve backward compatibility ($, USD, cents, value_usd,
   dollars, max_loss_per_trade_usd, Alpaca wording).
3. CLI help strings across scripts are market-neutral or explicitly market-qualified.
"""

import contextlib
import copy
import datetime
import io
import json
import os
import sys
import tempfile
import unittest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import FIX, ROOT, load

alerts_mod = load("trader-alerts", "alerts")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
broker_mod = load("trader-broker", "ticket")
news_mod = load("trader-news", "news")
tm_mod = load("trader-timemachine", "timemachine")
scan_mod = load("trader-scanner", "scan")


def _help_text(mod, subcmd=None):
    buf = io.StringIO()
    argv = [subcmd, "--help"] if subcmd else ["--help"]
    with contextlib.redirect_stdout(buf):
        try:
            mod.main(argv)
        except SystemExit:
            pass
    return buf.getvalue()


def _in_plan():
    return {
        "created_by": "trader-exit",
        "symbol": "RELIANCE.NS",
        "market": "IN",
        "currency": "INR",
        "side": "buy",
        "date": "2026-10-19",
        "setup": "orb",
        "entry": 1410.0,
        "entry_time": "10:15 AM",
        "entry_bar": 12,
        "interval": "5m",
        "stop": 1400.0,
        "stop_rule": "or_mid",
        "stop_label": "opening-range midpoint",
        "target": 1440.0,
        "target_r": 3.0,
        "target_label": "3R (three times the risk)",
        "risk_per_share": 10.0,
        "reward_risk": 3.0,
        "flat_by": "15:20",
        "tick_size": 0.05,
    }


def _in_cfg():
    return {
        "market": "IN",
        "currency": "INR",
        "exchange": "NSE",
        "budget": 100000,
        "cash_buffer_pct": 10,
        "max_position_pct": 75,
        "max_order_inr": 75000,
        "min_order_inr": 500,
        "max_loss_per_trade_inr": 2000,
        "max_daily_loss_inr": 5000,
        "max_trades_per_day": 3,
        "max_orders_per_day": 10,
        "min_reward_risk": 1.0,
        "require_stop": True,
        "no_entries_before": "09:30",
        "no_entries_after": "15:00",
        "flat_by": "15:20",
        "fractional": False,
        "paper": True,
        "blocked": [],
    }


def _us_plan():
    with open(os.path.join(FIX, "trader-budget", "plan-tsla.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _us_cfg():
    with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
        return json.load(fh)


class BudgetCurrencyTests(unittest.TestCase):
    def test_indian_plan_sizing_omits_usd_keys_and_formats_inr(self):
        cfg = _in_cfg()
        plan = _in_plan()
        s = budget_mod.size_trade(plan, cfg, today="2026-10-19", market="IN")
        self.assertEqual(s["market"], "IN")
        self.assertEqual(s["currency"], "INR")
        self.assertEqual(s["max_loss_per_trade"], 2000.0)
        self.assertNotIn("max_loss_per_trade_usd", s)
        for row in s["limits"]:
            self.assertIn("amount", row)
            self.assertNotIn("usd", row)
            self.assertNotIn("$", row["how"])
            self.assertIn("\u20b9", row["how"])

        order = budget_mod.bracket_order(s)
        self.assertEqual(order["market"], "IN")
        self.assertEqual(order["currency"], "INR")
        self.assertNotIn("$", order["why"])
        self.assertIn("\u20b9", order["why"])

        report = budget_mod.plan_report(s, order)
        self.assertNotIn("$", report)
        self.assertIn("\u20b9", report)
        self.assertIn("IST", report)

        blob = json.dumps({"sizing": s, "orders": [order]})
        self.assertNotIn("value_usd", blob)
        self.assertNotIn("max_loss_per_trade_usd", blob)
        self.assertNotIn('"usd"', blob)
        self.assertNotIn("$", blob)

    def test_indian_neutral_config_keys_override_usd_defaults(self):
        cfg = {
            "market": "IN",
            "currency": "INR",
            "budget": 100000,
            "cash_buffer_pct": 10,
            "max_position_pct": 75,
            "max_order": 50000,
            "min_order": 500,
            "max_loss_per_trade": 500,
            "min_reward_risk": 1.0,
        }
        s = budget_mod.size_trade(_in_plan(), cfg, today="2026-10-19", market="IN")
        self.assertEqual(s["max_loss_per_trade"], 500.0)
        self.assertEqual(s["qty"], 35)  # max order 50000 / 1410 = 35 (< max loss 500 / 10 = 50)
        self.assertEqual(s["bound_by"], "max order")

    def test_us_plan_sizing_preserves_usd_keys_and_neutral_aliases(self):
        s = budget_mod.size_trade(_us_plan(), _us_cfg(), today="2026-10-19")
        self.assertEqual(s["currency"], "USD")
        self.assertEqual(s["max_loss_per_trade"], 50.0)
        self.assertEqual(s["max_loss_per_trade_usd"], 50.0)
        for row in s["limits"]:
            self.assertIn("amount", row)
            self.assertIn("usd", row)
            self.assertIn("$", row["how"])
        order = budget_mod.bracket_order(s)
        self.assertIn("$", order["why"])
        self.assertEqual((order["market"], order["currency"], order["asset_type"]),
                         ("US", "USD", "EQUITY"))

    def test_weights_mode_india_vs_us(self):
        rows = [
            {"ticker": "RELIANCE.NS", "name": "Reliance", "weight": 60.0},
            {"ticker": "TCS.NS", "name": "TCS", "weight": 35.0},
            {"ticker": "SMALL.NS", "name": "Tiny", "weight": 5.0},
        ]
        cfg = {
            "market": "IN",
            "currency": "INR",
            "budget": 10000,
            "cash_buffer_pct": 10,
            "max_position_pct": 60,
            "min_order_inr": 600,
            "fractional": True,
        }
        r_in = budget_mod.size(rows, cfg, market="IN")
        self.assertEqual(r_in["market"], "IN")
        self.assertEqual(r_in["currency"], "INR")
        self.assertEqual(r_in["min_order"], 600.0)
        self.assertNotIn("min_order_usd", r_in)
        self.assertTrue(r_in["dropped"])
        self.assertIn("\u20b9", r_in["dropped"][0]["reason"])
        self.assertNotIn("$", r_in["dropped"][0]["reason"])
        for p in r_in["positions"]:
            self.assertIn("amount", p)
            self.assertNotIn("dollars", p)
        orders = budget_mod.orders_from(r_in)
        self.assertTrue(all(o.get("currency") == "INR" and o.get("market") == "IN" for o in orders))
        txt = budget_mod.report(r_in, orders)
        self.assertIn("amount", txt)
        self.assertNotIn("dollars", txt.lower())
        self.assertNotIn("$", txt)
        self.assertIn("\u20b9", txt)

        # US mode still preserves dollars & min_order_usd alongside neutral keys
        r_us = budget_mod.size(
            [{"ticker": "AAPL", "name": "Apple", "weight": 50.0},
             {"ticker": "KO", "name": "Coca-Cola", "weight": 50.0}],
            {"budget": 1000, "cash_buffer_pct": 10, "max_position_pct": 60, "min_order_usd": 5, "fractional": True},
        )
        self.assertEqual(r_us["currency"], "USD")
        self.assertEqual(r_us["min_order_usd"], 5.0)
        self.assertEqual(r_us["min_order"], 5.0)
        self.assertIn("dollars", r_us["positions"][0])
        self.assertIn("amount", r_us["positions"][0])
        txt_us = budget_mod.report(r_us, budget_mod.orders_from(r_us))
        self.assertIn("dollars", txt_us)
        self.assertIn("$", txt_us)


class RiskCurrencyTests(unittest.TestCase):
    def test_indian_risk_json_and_messages_use_inr_and_no_value_usd(self):
        cfg = _in_cfg()
        s = budget_mod.size_trade(_in_plan(), cfg, today="2026-10-19", market="IN")
        order = budget_mod.bracket_order(s)
        now_utc = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], cfg, market="IN", now=now_utc)
        self.assertEqual(res["pass"], 1)
        self.assertEqual(res["market"], "IN")
        self.assertEqual(res["currency"], "INR")
        r = res["results"][0]
        self.assertEqual(r["currency"], "INR")
        self.assertIsNotNone(r["value"])
        self.assertNotIn("value_usd", r)
        blob = json.dumps(res)
        self.assertNotIn("value_usd", blob)
        self.assertNotIn("$", blob)
        txt = risk_mod.report(res)
        self.assertNotIn("$", txt)
        self.assertIn("\u20b9", txt)
        self.assertIn("IST", txt)

    def test_indian_risk_block_messages_use_rupee_symbol(self):
        cfg = _in_cfg()
        cfg["max_loss_per_trade_inr"] = 100
        cfg["max_order_inr"] = 1000
        order = {
            "symbol": "RELIANCE.NS",
            "side": "buy",
            "qty": "10",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit_limit_price": "1430.00",
            "stop_loss_stop_price": "1380.00",
            "plan": {"entry": 1410.0, "stop": 1380.0, "target": 1430.0},
            "market": "IN",
            "currency": "INR",
        }
        now_utc = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        res = risk_mod.check([order], cfg, market="IN", now=now_utc)
        self.assertEqual(res["block"], 1)
        joined = " ".join(res["results"][0]["reasons"])
        self.assertIn("\u20b9", joined)
        self.assertNotIn("$", joined)

    def test_us_risk_preserves_value_usd_and_neutral_value(self):
        cfg = _us_cfg()
        s = budget_mod.size_trade(_us_plan(), cfg, today="2026-10-19")
        order = budget_mod.bracket_order(s)
        now_utc = risk_mod.parse_now("2026-10-19 10:15", market="US")
        res = risk_mod.check([order], cfg, market="US", now=now_utc)
        r = res["results"][0]
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["value"], 2913.44)
        self.assertEqual(r["value_usd"], 2913.44)
        self.assertIn("$", risk_mod.report(res))


class AlertsEntryExitBrokerNewsCurrencyTests(unittest.TestCase):
    def test_alerts_evaluate_formats_inr_for_indian_quotes_and_usd_for_us(self):
        now_utc = datetime.datetime(2026, 10, 19, 5, 0, 0)  # 10:30 IST
        in_status = {"market": "IN", "open": True, "date": "2026-10-19", "opens": "09:15", "closes": "15:30"}
        in_quotes = {"RELIANCE": {"price": 1425.50, "prev_close": 1400.0, "currency": "INR", "exchange": "NSE"}}
        rules = [
            {"id": "rel-target", "type": "target", "symbol": "RELIANCE", "price": 1420.0},
            {"id": "rel-pct", "type": "pct_move", "symbol": "RELIANCE", "pct": 1.0, "direction": "up"},
        ]
        rings, _ = alerts_mod.evaluate(rules, in_quotes, in_status, {}, now_utc)
        self.assertEqual(len(rings), 2)
        for ring in rings:
            self.assertIn("\u20b9", ring["message"])
            self.assertNotIn("$", ring["message"])

        us_status = {"open": True, "date": "2026-10-19", "opens": "09:30", "closes": "16:00"}
        us_quotes = {"AAPL": {"price": 341.27, "prev_close": 330.0, "currency": "USD"}}
        us_rules = [{"id": "aapl-above", "type": "above", "symbol": "AAPL", "price": 340.0}]
        us_rings, _ = alerts_mod.evaluate(us_rules, us_quotes, us_status, {}, datetime.datetime(2026, 10, 19, 14, 30, 0))
        self.assertIn("$341.27", us_rings[0]["message"])

    def test_entry_lag_note_uses_paise_for_india_and_cents_for_us(self):
        # Deterministic 4-bar ORB chart for 2026-10-19 where bar 4 breaks range high
        # and meta.regularMarketTime is 30s before bar 4 close so data_note fires.
        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        t_in = [
            int(datetime.datetime(2026, 10, 19, 9, 15, tzinfo=ist).timestamp()),
            int(datetime.datetime(2026, 10, 19, 9, 20, tzinfo=ist).timestamp()),
            int(datetime.datetime(2026, 10, 19, 9, 25, tzinfo=ist).timestamp()),
            int(datetime.datetime(2026, 10, 19, 9, 30, tzinfo=ist).timestamp()),
        ]
        in_chart = {
            "chart": {
                "result": [{
                    "meta": {
                        "symbol": "RELIANCE.NS",
                        "currency": "INR",
                        "exchangeTimezoneName": "Asia/Kolkata",
                        "dataGranularity": "5m",
                        "regularMarketTime": t_in[3] + 240,
                    },
                    "timestamp": t_in,
                    "indicators": {"quote": [{
                        "open": [1400.0, 1401.0, 1402.0, 1403.0],
                        "high": [1405.0, 1406.0, 1404.0, 1412.0],
                        "low": [1398.0, 1399.0, 1400.0, 1402.0],
                        "close": [1402.0, 1403.0, 1401.0, 1410.0],
                        "volume": [1000, 1200, 1100, 2000],
                    }]},
                }]
            }
        }
        et = datetime.timezone(datetime.timedelta(hours=-4))
        t_us = [
            int(datetime.datetime(2026, 10, 19, 9, 30, tzinfo=et).timestamp()),
            int(datetime.datetime(2026, 10, 19, 9, 35, tzinfo=et).timestamp()),
            int(datetime.datetime(2026, 10, 19, 9, 40, tzinfo=et).timestamp()),
            int(datetime.datetime(2026, 10, 19, 9, 45, tzinfo=et).timestamp()),
        ]
        us_chart = {
            "chart": {
                "result": [{
                    "meta": {
                        "symbol": "TSLA",
                        "currency": "USD",
                        "exchangeTimezoneName": "America/New_York",
                        "dataGranularity": "5m",
                        "regularMarketTime": t_us[3] + 240,
                    },
                    "timestamp": t_us,
                    "indicators": {"quote": [{
                        "open": [355.0, 356.0, 357.0, 358.0],
                        "high": [360.0, 362.0, 361.0, 366.0],
                        "low": [354.0, 355.0, 356.0, 357.0],
                        "close": [358.0, 359.0, 357.0, 365.0],
                        "volume": [1000, 1200, 1100, 2000],
                    }]},
                }]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            p_in = os.path.join(tmp, "RELIANCE.NS.json")
            p_us = os.path.join(tmp, "TSLA.json")
            with open(p_in, "w", encoding="utf-8") as fh:
                json.dump(in_chart, fh)
            with open(p_us, "w", encoding="utf-8") as fh:
                json.dump(us_chart, fh)

            buf_in = io.StringIO()
            with contextlib.redirect_stdout(buf_in):
                code_in = entry_mod.main([
                    "check", "RELIANCE.NS", "--offline", p_in, "--market", "IN",
                    "--now", "2026-10-19 09:36", "--json",
                ])
            self.assertEqual(code_in, 0)
            r_in = json.loads(buf_in.getvalue())
            self.assertEqual(r_in["status"], "triggered")
            self.assertIn("data_note", r_in)
            self.assertIn("paise", r_in["data_note"])
            self.assertNotIn("cent", r_in["data_note"])

            buf_us = io.StringIO()
            with contextlib.redirect_stdout(buf_us):
                code_us = entry_mod.main([
                    "check", "TSLA", "--offline", p_us, "--market", "US",
                    "--now", "2026-10-19 09:51", "--json",
                ])
            self.assertEqual(code_us, 0)
            r_us = json.loads(buf_us.getvalue())
            self.assertEqual(r_us["status"], "triggered")
            self.assertIn("data_note", r_us)
            self.assertIn("cent or two", r_us["data_note"])
            self.assertNotIn("paise", r_us["data_note"])

    def test_cli_persisted_indian_artifacts_and_error_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_p = os.path.join(tmp, "config.json")
            plan_p = os.path.join(tmp, "plan.json")
            orders_p = os.path.join(tmp, "orders.json")
            risk_p = os.path.join(tmp, "risk.json")
            with open(cfg_p, "w", encoding="utf-8") as fh:
                json.dump(_in_cfg(), fh)
            with open(plan_p, "w", encoding="utf-8") as fh:
                json.dump(_in_plan(), fh)

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = budget_mod.main([
                    "--plan", plan_p, "--config", cfg_p, "--market", "IN",
                    "--now", "2026-10-19 10:15", "--write", orders_p, "--json",
                ])
            self.assertEqual(code, 0, err.getvalue())
            with open(orders_p, encoding="utf-8") as fh:
                orders_raw = fh.read()
            orders_doc = json.loads(orders_raw)
            self.assertEqual(orders_doc["market"], "IN")
            self.assertEqual(orders_doc["currency"], "INR")
            self.assertNotIn("value_usd", orders_raw)
            self.assertNotIn("max_loss_per_trade_usd", orders_raw)
            self.assertNotIn("$", orders_raw)

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = risk_mod.main([
                    "--orders", orders_p, "--config", cfg_p, "--market", "IN",
                    "--now", "2026-10-19 10:16", "--out", risk_p, "--json",
                ])
            self.assertEqual(code, 0, err.getvalue())
            with open(risk_p, encoding="utf-8") as fh:
                risk_raw = fh.read()
            risk_doc = json.loads(risk_raw)
            self.assertEqual(risk_doc["market"], "IN")
            self.assertEqual(risk_doc["currency"], "INR")
            self.assertNotIn("value_usd", risk_raw)
            self.assertNotIn("$", risk_raw)

            # Verify alerts --at error message is market-neutral
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = alerts_mod.main(["add", "--at", "bad"])
            self.assertNotEqual(code, 0)
            self.assertIn("local market time", err.getvalue())
            self.assertNotIn("wants a New York time", err.getvalue())

    def test_timemachine_indian_json_records_currency_and_market(self):
        gz_path = os.path.join(FIX, "trader-timemachine", "RELIANCE-60d5m.json.gz")
        if not os.path.exists(gz_path):
            self.skipTest("RELIANCE-60d5m fixture missing")
        import gzip
        with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
            chart = json.load(fh)
        ts_list = chart["chart"]["result"][0]["timestamp"]
        # Pick the last full day inside the fixture and replay a small window
        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        last_dt = datetime.datetime.fromtimestamp(ts_list[-1], tz=ist)
        day_str = last_dt.date().isoformat()
        raw_plan = {
            "symbol": "RELIANCE.NS",
            "market": "IN",
            "side": "buy",
            "date": day_str,
            "entry": 1410.0,
            "stop": 1400.0,
            "target": 1430.0,
            "entry_time": "10:15 AM",
            "setup": "orb",
            "opening_range": {"minutes": 15},
            "interval": "5m",
        }
        plan = tm_mod.normalize_plan(raw_plan, market="IN")
        now_utc = datetime.datetime(last_dt.year, last_dt.month, last_dt.day, 10, 0, 0)
        doc = tm_mod.time_machine(
            json.dumps(chart), plan, now_utc, replays=100, seed=19,
            window=2, until=None, source="offline", simulated=False,
        )
        self.assertEqual(doc["market"], "IN")
        self.assertEqual(doc["currency"], "INR")

    def test_exit_supports_rupee_prefixes_and_nse_ticks(self):
        self.assertEqual(exit_mod.parse_stop("price:\u20b91400"), ("price", exit_mod.D("1400")))
        self.assertEqual(exit_mod.parse_stop("price:Rs.1400.25"), ("price", exit_mod.D("1400.25")))
        self.assertEqual(exit_mod.parse_target("price:\u20b91450"), ("price", exit_mod.D("1450")))
        c_in = exit_mod.compute(1410.0, "or_mid", None, "r", exit_mod.D("2"), or_high=1412.0, or_low=1400.03, market="IN")
        self.assertEqual(c_in["tick_size"], exit_mod.Decimal("0.05"))
        # 1406.015 floors to 1406.00 on 0.05 tick
        self.assertEqual(float(c_in["stop"]), 1406.0)
        with self.assertRaises(exit_mod.PlanError) as ctx:
            exit_mod.compute(1410.0, "price", exit_mod.D("1409.95"), "r", exit_mod.D("2"), market="IN")
        self.assertIn("\u20b9", str(ctx.exception))
        self.assertNotIn("$", str(ctx.exception))

    def test_broker_ticket_and_json_use_inr_and_paper_broker_label(self):
        cfg = _in_cfg()
        s = budget_mod.size_trade(_in_plan(), cfg, today="2026-10-19", market="IN")
        order = budget_mod.bracket_order(s)
        now_utc = risk_mod.parse_now("2026-10-19 10:15", market="IN")
        risk = risk_mod.check([order], cfg, market="IN", now=now_utc)
        risk["date"] = "2026-10-19"
        tjson = broker_mod.ticket_json(order, 1, cfg, risk, {"orders": []}, "2026-10-19", market="IN")
        self.assertEqual(tjson["market"], "IN")
        self.assertEqual(tjson["currency"], "INR")
        self.assertIn("\u20b9", tjson["plain"])
        self.assertNotIn("$", tjson["plain"])

        rendered, ready = broker_mod.render(order, 1, 1, cfg, risk, {"orders": []}, "2026-10-19", market="IN")
        self.assertTrue(ready)
        self.assertIn("Paper Broker (NSE/BSE)", rendered)
        self.assertNotIn("Alpaca places", rendered)
        self.assertNotIn("$", rendered)

    def test_news_parse_chart_defaults_indian_ticker_currency_to_inr(self):
        payload = json.dumps({
            "chart": {"result": [{"meta": {"regularMarketPrice": 1410.0, "chartPreviousClose": 1400.0}}]}
        })
        mv_in = news_mod.parse_chart(payload, "RELIANCE.NS")
        self.assertEqual(mv_in["currency"], "INR")
        mv_us = news_mod.parse_chart(payload, "TSLA")
        self.assertEqual(mv_us["currency"], "USD")

    def test_cli_help_strings_are_market_aware(self):
        budget_help = _help_text(budget_mod)
        self.assertNotIn("whole dollars", budget_help)
        risk_help = _help_text(risk_mod)
        self.assertIn("--market", risk_help)
        entry_help = _help_text(entry_mod, "check")
        self.assertIn("--market", entry_help)
        self.assertIn("market time", entry_help)
        alerts_help = _help_text(alerts_mod, "add")
        self.assertIn("IST", alerts_help)
        tm_help = _help_text(tm_mod, "run")
        self.assertIn("IST", tm_help)
        self.assertIn("--market", tm_help)
        scan_help = _help_text(scan_mod, "orb")
        self.assertIn("--market", scan_help)
        exit_help = _help_text(exit_mod, "plan")
        self.assertIn("IST", exit_help)
        self.assertIn("--market", exit_help)

    def test_alerts_readiness_accepts_neutral_limit_keys(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_p = os.path.join(td, "config.json")
            with open(cfg_p, "w", encoding="utf-8") as fh:
                json.dump({
                    "market": "IN",
                    "india_market_data_provider": "simulated",
                    "paper": True,
                    "budget": 100000,
                    "max_loss_per_trade": 1500,
                    "max_daily_loss": 3000,
                }, fh)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = alerts_mod.main([
                    "readiness", "--market", "IN", "--config", cfg_p,
                    "--instruments", os.path.join(td, "inst.json"),
                    "--now", "2026-10-19 10:15", "--json",
                ])
            self.assertEqual(code, 0, err.getvalue())
            doc = json.loads(out.getvalue())
            cfg_item = [it for it in doc["checks"] if it["check"] == "Configuration & Safety Limits"][0]
            self.assertEqual(cfg_item["status"], "OK")
            self.assertIn("\u20b91,500", cfg_item["detail"])
            self.assertIn("\u20b93,000", cfg_item["detail"])
            self.assertNotIn("Missing max_loss_per_trade", cfg_item["note"])
            self.assertNotIn("$", cfg_item["detail"])


if __name__ == "__main__":
    unittest.main()
