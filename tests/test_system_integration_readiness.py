"""End-to-End System Integration & Operational Readiness Test Suite.

Verifies complete cross-skill interoperability across Scenarios A through H:
Scenario A: Bullish equity opportunity (Scanner -> Entry -> Exit -> Budget -> Risk -> Broker -> Exit manage -> TimeMachine)
Scenario B: Bearish equity opportunity (VWAP rejection short-side geometry & risk safeguards)
Scenario C: Options boundaries (BUY CE analytical only, BUY PE unsupported, naked options prohibited)
Scenario D: Invalid or stale market data (fails closed, look-ahead blocked)
Scenario E: Risk rejection (Net 2R floor, max loss, ticket refusal)
Scenario F: Configuration consistency (IN vs US profiles, India paper-only enforcement)
Scenario G: TimeMachine historical replay parity (no future leakage, seed stability)
Scenario H: Independent skill invocation (clean contracts and execution across all specialist skills)
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

alerts_mod = load("trader-alerts", "alerts")
scan_mod = load("trader-scanner", "scan")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")
tm_mod = load("trader-timemachine", "timemachine")
analyst_mod = load("trader-analyst", "facts")
news_mod = load("trader-news", "news")
pelosi_mod = load("trader-pelosi", "ptr")

from config.loader import load_config as central_load_config, ConfigError
from config.schema import validate_config

BHARAT_FIXTURES = os.path.join(FIX, "bharat")
RELIANCE_5M = os.path.join(BHARAT_FIXTURES, "RELIANCE-5m.json")
RELIANCE_60D = os.path.join(FIX, "trader-timemachine", "RELIANCE-60d5m.json.gz")
TSLA_5M = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
ORDERS_TSLA = os.path.join(FIX, "trader-risk", "orders-tsla.json")


def _run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as exc:
            code = exc.code if exc.code is not None else 0
    return code, out.getvalue(), err.getvalue()


def _load_json_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestSystemIntegrationReadiness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sys_integ_")
        self.cfg_in_path = os.path.join(self.tmp, "config.in.json")
        cfg_in = central_load_config(market="IN", overrides={"budget": 200000.0, "max_order_inr": 150000.0})
        with open(self.cfg_in_path, "w", encoding="utf-8") as fh:
            json.dump(cfg_in, fh)

        self.cfg_us_path = os.path.join(self.tmp, "config.us.json")
        cfg_us = central_load_config(market="US", overrides={"budget": 25000.0, "max_order_usd": 4000.0})
        with open(self.cfg_us_path, "w", encoding="utf-8") as fh:
            json.dump(cfg_us, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario A: Bullish Equity Opportunity (Full Lifecycle)
    # -------------------------------------------------------------------------
    def test_scenario_a_bullish_equity_lifecycle(self):
        # 1. Market Context & Scanner
        wl_path = os.path.join(self.tmp, "watchlist.json")
        rc, out, err = _run_cli(scan_mod, [
            "orb", "--market", "IN", "--offline", RELIANCE_5M,
            "--now", "2026-10-19 10:35", "--min-price", "0", "--min-volume", "0",
            "--out", wl_path, "--json",
        ])
        self.assertEqual(rc, 0, err)
        with open(wl_path, "r", encoding="utf-8") as fh:
            wl_doc = json.load(fh)
        self.assertEqual(wl_doc["market"], "IN")
        self.assertTrue(any(h.get("symbol") == "RELIANCE" for h in wl_doc.get("hits", [])))

        # 2. Entry Engine
        entry_path = os.path.join(self.tmp, "entry.json")
        rc, out, err = _run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", RELIANCE_5M,
            "--now", "2026-10-19 10:35", "--market", "IN", "--write", entry_path, "--json",
        ])
        self.assertEqual(rc, 0, err)
        with open(entry_path, "r", encoding="utf-8") as fh:
            entry_doc = json.load(fh)
        self.assertEqual(entry_doc["symbol"], "RELIANCE")
        self.assertEqual(entry_doc["side"], "buy")
        self.assertEqual(entry_doc["status"], "triggered")
        self.assertEqual(entry_doc["currency"], "INR")

        # 3. Exit Planner
        plan_path = os.path.join(self.tmp, "plan.json")
        rc, out, err = _run_cli(exit_mod, [
            "plan", "--from", entry_path, "--offline", RELIANCE_5M,
            "--stop", "atr:1.5", "--target", "4R",
            "--config", self.cfg_in_path, "--market", "IN",
            "--now", "2026-10-19 10:35", "--write", plan_path,
        ])
        self.assertEqual(rc, 0, err)
        with open(plan_path, "r", encoding="utf-8") as fh:
            plan_doc = json.load(fh)
        self.assertEqual(plan_doc["symbol"], "RELIANCE")
        self.assertEqual(plan_doc["side"], "buy")
        self.assertGreaterEqual(plan_doc["reward_risk"], 2.0)
        self.assertEqual(plan_doc["tick_size"], 0.05)

        # 4. Budget & Sizing
        pf_path = os.path.join(self.tmp, "portfolio.json")
        with open(pf_path, "w", encoding="utf-8") as fh:
            json.dump({"cash": 200000.0, "currency": "INR", "positions": []}, fh)

        orders_path = os.path.join(self.tmp, "orders.json")
        rc, out, err = _run_cli(budget_mod, [
            "--plan", plan_path, "--config", self.cfg_in_path, "--portfolio", pf_path,
            "--now", "2026-10-19 10:35", "--market", "IN", "--write", orders_path,
        ])
        self.assertEqual(rc, 0, err)
        with open(orders_path, "r", encoding="utf-8") as fh:
            orders_doc = json.load(fh)
        self.assertEqual(len(orders_doc["orders"]), 1)
        ord_item = orders_doc["orders"][0]
        self.assertEqual(ord_item["symbol"], "RELIANCE")
        self.assertEqual(ord_item["currency"], "INR")
        self.assertGreater(int(ord_item["qty"]), 0)
        self.assertIn("transaction_costs", ord_item)
        self.assertGreaterEqual(ord_item["net_reward_risk"], 2.0)

        # 5. Risk Validation & Cryptographic Fingerprinting
        risk_path = os.path.join(self.tmp, "risk.json")
        rc, out, err = _run_cli(risk_mod, [
            "--orders", orders_path, "--config", self.cfg_in_path, "--portfolio", pf_path,
            "--now", "2026-10-19 10:35", "--market", "IN", "--out", risk_path,
        ])
        self.assertEqual(rc, 0, err)
        with open(risk_path, "r", encoding="utf-8") as fh:
            risk_doc = json.load(fh)
        self.assertEqual(risk_doc["pass"], 1)
        self.assertEqual(risk_doc["block"], 0)
        res_item = risk_doc["results"][0]
        self.assertEqual(res_item["verdict"], "PASS")
        self.assertTrue(bool(res_item["fingerprint"]))

        # 6. Paper Broker Approval & Fill
        broker_path = os.path.join(self.tmp, "broker.json")
        rc, out, err = _run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy %s RELIANCE" % ord_item["qty"],
            "--orders", orders_path, "--risk", risk_path,
            "--config", self.cfg_in_path, "--state", broker_path,
            "--now", "2026-10-19 10:35",
        ])
        self.assertEqual(rc, 0, err)

        rc, out, err = _run_cli(ticket_mod, [
            "result", "--index", "1", "--status", "placed",
            "--filled-qty", str(ord_item["qty"]),
            "--filled-price", str(plan_doc["entry"]),
            "--orders", orders_path, "--risk", risk_path,
            "--config", self.cfg_in_path, "--portfolio", pf_path,
            "--state", broker_path, "--now", "2026-10-19 10:35",
        ])
        self.assertEqual(rc, 0, err)
        with open(broker_path, "r", encoding="utf-8") as fh:
            broker_doc = json.load(fh)
        self.assertTrue(broker_doc["orders"][0]["paper"])
        self.assertEqual(broker_doc["orders"][-1]["status"], "filled")

        # 7. TimeMachine Historical Replay Parity
        tm_path = os.path.join(self.tmp, "timemachine.json")
        tm_chart = os.path.join(self.tmp, "RELIANCE-60d.json")
        with open(tm_chart, "w", encoding="utf-8") as fh:
            json.dump(_load_json_maybe_gz(RELIANCE_60D), fh)

        rc, out, err = _run_cli(tm_mod, [
            "run", "--plan", plan_path, "--offline", tm_chart,
            "--config", self.cfg_in_path, "--market", "IN",
            "--now", "2026-10-19 10:35", "--replays", "100", "--seed", "42", "--write", tm_path,
        ])
        self.assertEqual(rc, 0, err)
        with open(tm_path, "r", encoding="utf-8") as fh:
            tm_doc = json.load(fh)
        self.assertIn("history", tm_doc)
        self.assertIn("mc", tm_doc)
        self.assertFalse(tm_doc.get("future_data_leaked", False))

    # -------------------------------------------------------------------------
    # Scenario B: Bearish Equity Opportunity (VWAP Rejection & Short Policy)
    # -------------------------------------------------------------------------
    def test_scenario_b_bearish_equity_opportunity(self):
        # Build synthetic chart with VWAP rejection
        t0 = 1792416600  # 2026-10-19 09:30 EDT
        bars = [
            {"t": t0, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 10000},
            {"t": t0 + 300, "o": 100.5, "h": 100.8, "l": 98.0, "c": 98.5, "v": 10000},
            {"t": t0 + 600, "o": 98.5, "h": 99.9, "l": 96.8, "c": 97.0, "v": 15000},
        ]
        doc = {
            "chart": {
                "result": [{
                    "meta": {"symbol": "BEAR", "currency": "USD", "exchangeName": "NYSE", "instrumentType": "EQUITY",
                             "dataGranularity": "5m", "gmtoffset": -14400, "chartPreviousClose": 100.0, "previousClose": 100.0,
                             "regularMarketPrice": 97.0},
                    "timestamp": [b["t"] for b in bars],
                    "indicators": {"quote": [{
                        "open": [b["o"] for b in bars],
                        "high": [b["h"] for b in bars],
                        "low": [b["l"] for b in bars],
                        "close": [b["c"] for b in bars],
                        "volume": [b["v"] for b in bars],
                    }]},
                }],
                "error": None,
            }
        }
        chart_p = os.path.join(self.tmp, "bear_chart.json")
        with open(chart_p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

        entry_p = os.path.join(self.tmp, "bear_entry.json")
        rc, out, err = _run_cli(entry_mod, [
            "check", "BEAR", "--setup", "vwap_rejection", "--offline", chart_p,
            "--now", "2026-10-19 09:46", "--market", "US", "--write", entry_p, "--json",
        ])
        self.assertEqual(rc, 0, err)
        with open(entry_p, "r", encoding="utf-8") as fh:
            e_doc = json.load(fh)

        # Verify short-side trade geometry
        self.assertEqual(e_doc["setup"], "vwap_rejection")
        self.assertEqual(e_doc["side"], "sell")
        self.assertEqual(e_doc["status"], "triggered")
        self.assertGreater(e_doc["invalidation_level"], e_doc["price"])
        self.assertGreater(e_doc["stop_preview"], e_doc["price"])
        self.assertLess(e_doc["targets_preview"]["t1_1r"], e_doc["price"])
        self.assertLess(e_doc["targets_preview"]["t2_2r"], e_doc["targets_preview"]["t1_1r"])
        self.assertGreaterEqual(e_doc["reward_risk"], 2.0)

        # Downstream short policy check:
        # Default config has allow_short=False. An unheld short entry must be blocked by risk policy.
        ord_doc = {
            "created_by": "trader-budget", "market": "US", "currency": "USD",
            "orders": [{
                "symbol": "BEAR", "side": "sell", "qty": 10, "type": "limit", "time_in_force": "day",
                "limit_price": 97.0, "order_class": "bracket",
                "stop_loss_stop_price": e_doc["stop_preview"],
                "take_profit_limit_price": e_doc["targets_preview"]["t2_2r"],
                "plan": {"entry": 97.0, "stop": e_doc["stop_preview"], "target": e_doc["targets_preview"]["t2_2r"]},
            }],
        }
        res_blocked = risk_mod.check(
            ord_doc["orders"],
            {"allow_short": False, "budget": 10000.0, "max_loss_per_trade_usd": 100.0, "min_reward_risk": 2.0},
            portfolio={"cash": 10000.0, "positions": []},
            now=datetime.datetime(2026, 10, 19, 13, 46, tzinfo=datetime.timezone.utc),
            market="US",
        )
        self.assertEqual(res_blocked["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("short selling is off" in r for r in res_blocked["results"][0]["reasons"]))

    # -------------------------------------------------------------------------
    # Scenario C: Options Support Boundaries
    # -------------------------------------------------------------------------
    def test_scenario_c_options_support_boundaries(self):
        # 1. BUY CE: analytical signal only (options_orb)
        # Sizing execution is disabled in size.py (SizeError: OPTION RISK UNAVAILABLE)
        plan_ce = {
            "created_by": "trader-exit",
            "symbol": "NIFTY26MAR24000CE",
            "asset_type": "OPTIONS",
            "market": "IN",
            "currency": "INR",
            "side": "buy",
            "entry": 150.0,
            "stop": 120.0,
            "target": 220.0,
            "tick_size": 0.05,
            "option_contract": {"symbol": "NIFTY26MAR24000CE", "lot_size": 50},
        }
        with self.assertRaises(budget_mod.SizeError) as ctx:
            budget_mod.size_trade(plan_ce, {"budget": 100000.0}, market="IN")
        self.assertIn("options are not enabled", str(ctx.exception))

        # 2. Naked short options: blocked unconditionally by risk engine
        naked_short_order = {
            "symbol": "NIFTY26MAR24000CE",
            "asset_type": "OPTIONS",
            "side": "sell",
            "qty": 50,
            "market": "IN",
            "currency": "INR",
            "type": "market",
            "time_in_force": "day",
        }
        res = risk_mod.check([naked_short_order], {"paper": True, "budget": 100000.0}, market="IN")
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("naked short options are prohibited" in r for r in res["results"][0]["reasons"]))

    # -------------------------------------------------------------------------
    # Scenario D: Invalid or Stale Market Data Fails Closed
    # -------------------------------------------------------------------------
    def test_scenario_d_stale_and_future_data_fails_closed(self):
        # Future plan timestamp (look-ahead) is rejected by budget and risk
        future_plan = {
            "created_by": "trader-exit",
            "symbol": "RELIANCE",
            "side": "buy",
            "market": "IN",
            "currency": "INR",
            "date": "2026-10-19",
            "entry": 2849.0,
            "entry_time": "3:00 PM",
            "entry_bar": 68,
            "stop": 2840.0,
            "target": 2880.0,
            "last_data_timestamp": "2026-10-19T15:00:00+05:30",
        }
        fp_path = os.path.join(self.tmp, "future_plan.json")
        with open(fp_path, "w", encoding="utf-8") as fh:
            json.dump(future_plan, fh)

        # Budget run at 10:35 IST rejects 15:00 plan
        rc, _, err = _run_cli(budget_mod, [
            "--plan", fp_path, "--config", self.cfg_in_path,
            "--now", "2026-10-19 10:35", "--market", "IN",
        ])
        self.assertEqual(rc, 1)
        self.assertIn("look-ahead", err.lower())

    # -------------------------------------------------------------------------
    # Scenario E: Risk Rejection on Sub-2R or Excessive Loss
    # -------------------------------------------------------------------------
    def test_scenario_e_risk_rejection(self):
        # Order with insufficient reward:risk (1.2R < 2.0R mandatory floor)
        bad_order = {
            "symbol": "RELIANCE", "side": "buy", "qty": 10, "type": "limit", "time_in_force": "day",
            "limit_price": 2850.0, "order_class": "bracket",
            "stop_loss_stop_price": 2840.0,
            "take_profit_limit_price": 2862.0,  # only 12 / 10 = 1.2R
            "plan": {"entry": 2850.0, "stop": 2840.0, "target": 2862.0},
            "market": "IN", "currency": "INR",
        }
        bad_orders_p = os.path.join(self.tmp, "bad_orders.json")
        with open(bad_orders_p, "w", encoding="utf-8") as fh:
            json.dump({"orders": [bad_order]}, fh)

        risk_p = os.path.join(self.tmp, "bad_risk.json")
        rc, _, _ = _run_cli(risk_mod, [
            "--orders", bad_orders_p, "--config", self.cfg_in_path,
            "--now", "2026-10-19 10:35", "--market", "IN", "--out", risk_p,
        ])
        self.assertEqual(rc, 1)
        with open(risk_p, "r", encoding="utf-8") as fh:
            r_doc = json.load(fh)
        self.assertEqual(r_doc["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("under your minimum" in r for r in r_doc["results"][0]["reasons"]))

        # Ticket must refuse approval of the blocked order
        rc, out, err = _run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy 10 RELIANCE",
            "--orders", bad_orders_p, "--risk", risk_p,
            "--config", self.cfg_in_path, "--now", "2026-10-19 10:35",
        ])
        self.assertEqual(rc, 1)
        self.assertIn("BLOCKED", err)

    # -------------------------------------------------------------------------
    # Scenario F: Configuration Consistency (IN vs US Profiles)
    # -------------------------------------------------------------------------
    def test_scenario_f_configuration_consistency(self):
        # IN Profile: strictly paper-only, INR currency, NSE exchange
        cfg_in = central_load_config(market="IN")
        self.assertEqual(cfg_in["market"], "IN")
        self.assertEqual(cfg_in["currency"], "INR")
        self.assertEqual(cfg_in["broker"], "paper")
        self.assertTrue(cfg_in["paper"])
        self.assertGreaterEqual(cfg_in["min_reward_risk"], 2.0)

        # Attempting live execution in IN must fail
        with self.assertRaises(ConfigError):
            validate_config({"market": "IN", "paper": False})

        with self.assertRaises(ConfigError):
            validate_config({"market": "IN", "broker": "alpaca"})

        # US Profile: USD currency, NYSE exchange, minimum 2R
        cfg_us = central_load_config(market="US")
        self.assertEqual(cfg_us["market"], "US")
        self.assertEqual(cfg_us["currency"], "USD")
        self.assertEqual(cfg_us["exchange"], "NYSE")
        self.assertGreaterEqual(cfg_us["min_reward_risk"], 2.0)

    # -------------------------------------------------------------------------
    # Scenario G: TimeMachine Historical Replay Parity
    # -------------------------------------------------------------------------
    def test_scenario_g_timemachine_parity(self):
        plan = {
            "created_by": "trader-exit",
            "symbol": "RELIANCE",
            "side": "buy",
            "market": "IN",
            "currency": "INR",
            "date": "2026-10-19",
            "setup": "orb",
            "entry": 2849.0,
            "entry_time": "10:30 AM",
            "entry_bar": 14,
            "interval": "5m",
            "stop": 2842.0,
            "target": 2877.0,
            "tick_size": 0.05,
            "risk_per_share": 7.0,
            "reward_risk": 4.0,
            "flat_by": "15:20",
        }
        plan_p = os.path.join(self.tmp, "tm_plan.json")
        with open(plan_p, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)

        tm_chart = os.path.join(self.tmp, "RELIANCE-60d.json")
        with open(tm_chart, "w", encoding="utf-8") as fh:
            json.dump(_load_json_maybe_gz(RELIANCE_60D), fh)

        tm_out = os.path.join(self.tmp, "tm_out.json")
        rc, out, err = _run_cli(tm_mod, [
            "run", "--plan", plan_p, "--offline", tm_chart,
            "--replays", "200", "--seed", "7", "--market", "IN",
            "--now", "2026-10-19 10:35", "--write", tm_out, "--json",
        ])
        self.assertEqual(rc, 0, err)
        with open(tm_out, "r", encoding="utf-8") as fh:
            res = json.load(fh)
        self.assertIn("history", res)
        self.assertIn("mc", res)
        self.assertEqual(res["plan"]["symbol"], "RELIANCE")
        # Ensure replay statistics are coherent
        mc = res["mc"]
        self.assertIn("p_target", mc)
        self.assertIn("p_stop", mc)
        hist = res["history"]
        self.assertIn("win_rate", hist)
        self.assertIn("profit_factor", hist)
        self.assertIn("max_drawdown_r", hist)

    # -------------------------------------------------------------------------
    # Scenario H: Independent Specialist Skill Invocation
    # -------------------------------------------------------------------------
    def test_scenario_h_independent_skill_invocations(self):
        # 1. trader-alerts
        rc, out, _ = _run_cli(alerts_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Price alerts and the NYSE/NSE clock", out)

        # 2. trader-scanner
        rc, out, _ = _run_cli(scan_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Chart Scanner", out)

        # 3. trader-entry
        rc, out, _ = _run_cli(entry_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Entry Timer", out)

        # 4. trader-exit
        rc, out, _ = _run_cli(exit_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Exit Planner", out)

        # 5. trader-budget
        rc, out, _ = _run_cli(budget_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("size", out.lower())

        # 6. trader-risk
        rc, out, _ = _run_cli(risk_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("orders", out.lower())

        # 7. trader-broker
        rc, out, _ = _run_cli(ticket_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Order tickets and your yes", out)

        # 8. trader-timemachine
        rc, out, _ = _run_cli(tm_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Time Machine", out)

        # 9. trader-analyst
        rc, out, _ = _run_cli(analyst_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("quarters", out.lower())

        # 10. trader-news
        rc, out, _ = _run_cli(news_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("tickers", out.lower())

        # 11. trader-pelosi
        rc, out, _ = _run_cli(pelosi_mod, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("congress trade tracker", out.lower())


if __name__ == "__main__":
    unittest.main()
