import copy
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import ROOT, load

entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")
alerts_mod = load("trader-alerts", "alerts")

US_PLAN_FIXTURE = os.path.join(ROOT, "tests", "fixtures", "trader-budget", "plan-tsla.json")
US_CFG_FIXTURE = os.path.join(ROOT, "trader-budget", "config.example.json")

_IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
_EPOCH = datetime.datetime(1970, 1, 1)


def _ist_ts(y, m, d, hh, mm):
    return int((datetime.datetime(y, m, d, hh, mm) - _IST_OFFSET - _EPOCH).total_seconds())


def _write_breakout_offline(dirpath, sym="INFY", confirm_at=(10, 30)):
    os.makedirs(dirpath, exist_ok=True)
    days = []
    cur = datetime.date(2026, 10, 16)
    while len(days) < 35:
        ok, _ = entry_mod.trading_day(cur, market="IN")
        if ok:
            days.append(cur)
        cur -= datetime.timedelta(days=1)
    days.reverse()
    closes = [1480.0 + (i % 5) * 2.0 for i in range(34)] + [1515.0]
    d_ts = [_ist_ts(d.year, d.month, d.day, 9, 15) for d in days]
    daily_doc = {
        "chart": {
            "result": [{
                "meta": {"symbol": "%s.NS" % sym, "dataGranularity": "1d",
                         "regularMarketTime": d_ts[-1], "regularMarketPrice": closes[-1],
                         "previousClose": closes[-2]},
                "timestamp": d_ts,
                "indicators": {"quote": [{
                    "open": [c * 0.995 for c in closes],
                    "high": [c * 1.005 for c in closes],
                    "low": [c * 0.99 for c in closes],
                    "close": closes,
                    "volume": [2500000 for _ in closes],
                }]},
            }],
            "error": None,
        }
    }
    ch, cm = confirm_at
    bars = [
        (9, 15, 1495.0, 1505.0, 1490.0, 1502.0, 120000),
        (9, 20, 1502.0, 1510.0, 1498.0, 1506.0, 140000),
        (9, 25, 1506.0, 1512.0, 1502.0, 1509.0, 130000),
        (ch, cm, 1514.0, 1528.0, 1512.0, 1525.0, 180000),
    ]
    b_ts = [_ist_ts(2026, 10, 19, hh, mm) for hh, mm, *_ in bars]
    rmt_ts = b_ts[-1] + 5 * 60
    intraday_doc = {
        "chart": {
            "result": [{
                "meta": {"symbol": "%s.NS" % sym, "dataGranularity": "5m",
                         "regularMarketTime": rmt_ts, "regularMarketPrice": bars[-1][5],
                         "previousClose": 1515.0},
                "timestamp": b_ts,
                "indicators": {"quote": [{
                    "open": [b[2] for b in bars],
                    "high": [b[3] for b in bars],
                    "low": [b[4] for b in bars],
                    "close": [b[5] for b in bars],
                    "volume": [b[6] for b in bars],
                }]},
            }],
            "error": None,
        }
    }
    with open(os.path.join(dirpath, "%s-daily.json" % sym), "w", encoding="utf-8") as fh:
        json.dump(daily_doc, fh)
    with open(os.path.join(dirpath, "%s-5m.json" % sym), "w", encoding="utf-8") as fh:
        json.dump(intraday_doc, fh)
    return dirpath


def run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def _in_cfg(**overrides):
    cfg = {
        "market": "IN",
        "currency": "INR",
        "exchange": "NSE",
        "asset_type": "EQUITY",
        "budget": 100000,
        "cash_buffer_pct": 10,
        "max_position_pct": 75,
        "max_order_inr": 75000,
        "min_order_inr": 100,
        "max_loss_per_trade_inr": 2500,
        "max_daily_loss_inr": 5000,
        "max_trades_per_day": 3,
        "max_orders_per_day": 10,
        "min_reward_risk": 1.0,
        "require_stop": True,
        "no_entries_before": "09:30",
        "no_entries_after": "15:00",
        "flat_by": "15:20",
        "fractional": False,
        "broker": "paper",
        "paper": True,
        "blocked": [],
        "allow_margin": False,
        "allow_short": False,
    }
    cfg.update(overrides)
    return cfg


def _in_plan(**overrides):
    plan = {
        "symbol": "INFY",
        "setup": "breakout_20d",
        "entry": 1525.00,
        "stop": 1465.00,
        "stop_rule": "breakout_invalidation",
        "stop_label": "breakout invalidation",
        "target": 1645.00,
        "target_rule": "r",
        "target_label": "2R",
        "risk_per_share": 60.00,
        "reward_risk": 2.0,
        "entry_time": "10:35 AM",
        "date": "2026-10-19",
        "flat_by": "positional",
        "trading_style": "positional",
        "market": "IN",
        "currency": "INR",
    }
    plan.update(overrides)
    return plan


class PositionalOrderSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.d, "config.json")
        self.pf_path = os.path.join(self.d, "portfolio.json")
        self.broker_state = os.path.join(self.d, "state", "broker.json")
        self.risk_path = os.path.join(self.d, "state", "risk.json")
        self.entry_path = os.path.join(self.d, "entry.json")
        self.plan_path = os.path.join(self.d, "plan.json")
        self.orders_path = os.path.join(self.d, "orders.json")
        self.alerts_rules = os.path.join(self.d, "alerts.json")
        self.alerts_state = os.path.join(self.d, "state", "alerts.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(_in_cfg(), fh)
        with open(self.pf_path, "w", encoding="utf-8") as fh:
            json.dump({"cash": 100000.0, "currency": "INR", "positions": []}, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_exit_breakout_invalidation_stop_label_vs_explicit_price(self):
        off_dir = _write_breakout_offline(os.path.join(self.d, "offline"), "INFY", confirm_at=(10, 25))
        code_e, out_e, err_e = run_cli(entry_mod, [
            "check", "INFY", "--setup", "breakout_20d",
            "--offline", off_dir,
            "--now", "2026-10-19 10:35", "--trading-style", "positional",
            "--market", "IN", "--write", self.entry_path,
        ])
        self.assertEqual(code_e, 0, out_e + err_e)
        with open(self.entry_path, encoding="utf-8") as fh:
            ed = json.load(fh)
        self.assertIsNotNone(ed.get("invalidation_level"))

        # Inherited breakout_20d invalidation stop must NOT be labeled "your price"
        code_p, out_p, err_p = run_cli(exit_mod, [
            "plan", "--from", self.entry_path, "--config", self.cfg_path,
            "--now", "2026-10-19 10:35", "--market", "IN", "--write", self.plan_path,
        ])
        self.assertEqual(code_p, 0, out_p + err_p)
        with open(self.plan_path, encoding="utf-8") as fh:
            pl = json.load(fh)
        self.assertEqual(pl["stop_rule"], "breakout_invalidation")
        self.assertEqual(pl["stop_label"], "breakout invalidation")
        self.assertEqual(pl["trading_style"], "positional")
        self.assertEqual(pl["flat_by"], "positional")
        self.assertIn("breakout invalidation", out_p)
        self.assertNotIn("your price", out_p)

        # Explicit --stop price:X continues to report "your price"
        explicit_plan = os.path.join(self.d, "explicit_plan.json")
        code_xp, out_xp, err_xp = run_cli(exit_mod, [
            "plan", "--from", self.entry_path, "--stop", "price:1450",
            "--config", self.cfg_path, "--now", "2026-10-19 10:35",
            "--market", "IN", "--write", explicit_plan,
        ])
        self.assertEqual(code_xp, 0, out_xp + err_xp)
        with open(explicit_plan, encoding="utf-8") as fh:
            xpl = json.load(fh)
        self.assertEqual(xpl["stop_rule"], "price")
        self.assertEqual(xpl["stop_label"], "your price")

        # Explicit --stop breakout-invalidation preserves breakout invalidation label
        inv_plan = os.path.join(self.d, "inv_plan.json")
        code_iv, out_iv, err_iv = run_cli(exit_mod, [
            "plan", "--from", self.entry_path, "--stop", "breakout-invalidation",
            "--config", self.cfg_path, "--now", "2026-10-19 10:35",
            "--market", "IN", "--write", inv_plan,
        ])
        self.assertEqual(code_iv, 0, out_iv + err_iv)
        with open(inv_plan, encoding="utf-8") as fh:
            ipl = json.load(fh)
        self.assertEqual(ipl["stop_rule"], "breakout_invalidation")
        self.assertEqual(ipl["stop_label"], "breakout invalidation")

    def test_intraday_vs_positional_order_semantics_and_transaction_costs(self):
        # 1. Intraday Indian order preserves day + bracket + MIS + intraday charges
        intraday_plan = _in_plan(setup="orb", trading_style="intraday", flat_by="15:20")
        cfg = _in_cfg()
        s_intra = budget_mod.size_trade(intraday_plan, cfg, today="2026-10-19")
        o_intra = budget_mod.build_order(s_intra, cfg=cfg)
        self.assertEqual(o_intra["trading_style"], "intraday")
        self.assertEqual(o_intra["time_in_force"], "day")
        self.assertEqual(o_intra["order_class"], "bracket")
        self.assertEqual(o_intra["product"], "MIS")
        self.assertEqual(o_intra["plan"]["flat_by"], "15:20")
        self.assertNotIn("multi_session_protection", o_intra)
        self.assertGreater(s_intra["transaction_costs"]["brokerage"], 0.0)
        rep_intra = budget_mod.plan_report(s_intra, o_intra)
        self.assertIn("Proposed bracket order", rep_intra)
        self.assertIn("Flat by 15:20 IST", rep_intra)

        # 2. Positional Indian order emits gtc + gtt_oco + CNC + multi_session_protection + delivery charges
        pos_plan = _in_plan()
        s_pos = budget_mod.size_trade(pos_plan, cfg, today="2026-10-19")
        o_pos = budget_mod.build_order(s_pos, cfg=cfg)
        self.assertEqual(s_pos["trading_style"], "positional")
        self.assertEqual(s_pos["flat_by"], "positional")
        self.assertEqual(o_pos["trading_style"], "positional")
        self.assertEqual(o_pos["time_in_force"], "gtc")
        self.assertEqual(o_pos["order_class"], "gtt_oco")
        self.assertEqual(o_pos["product"], "CNC")
        self.assertEqual(o_pos["plan"]["flat_by"], "positional")
        prot = o_pos["multi_session_protection"]
        self.assertEqual(prot["type"], "gtt_oco")
        self.assertEqual(prot["product"], "CNC")
        self.assertEqual(prot["time_in_force"], "gtc")
        self.assertTrue(prot["persists_across_sessions"])
        self.assertEqual(prot["triggers"]["stop_loss"], o_pos["stop_loss_stop_price"])
        self.assertEqual(prot["triggers"]["take_profit"], o_pos["take_profit_limit_price"])

        # Compare delivery vs intraday transaction costs for identical qty/prices
        self.assertGreater(s_intra["transaction_costs"]["brokerage"], 0.0)
        self.assertLessEqual(s_intra["transaction_costs"]["brokerage"], 40.0)
        self.assertEqual(s_pos["transaction_costs"]["brokerage"], 80.0)
        self.assertEqual(s_pos["transaction_costs"]["brokerage_estimate"]["gst"], 14.40)
        self.assertEqual(s_pos["transaction_costs"]["brokerage_estimate"]["total"], 94.40)
        self.assertGreater(s_pos["transaction_costs"]["stt"], s_intra["transaction_costs"]["stt"])
        self.assertGreater(s_pos["transaction_costs"]["stamp_duty"], s_intra["transaction_costs"]["stamp_duty"])

        rep_pos = budget_mod.plan_report(s_pos, o_pos)
        self.assertIn("Proposed positional paper order", rep_pos)
        self.assertIn("GTT OCO", rep_pos)
        self.assertNotIn("Flat by positional", rep_pos)

        # 3. Derivative positional uses NRML instead of CNC
        fut_cfg = _in_cfg(allow_futures=True, max_order_inr=500000, max_loss_per_trade_inr=10000, budget=500000)
        fut_plan = _in_plan(asset_type="FUTURES", lot_size=25)
        s_fut = budget_mod.size_trade(fut_plan, fut_cfg, today="2026-10-19")
        o_fut = budget_mod.build_order(s_fut, cfg=fut_cfg)
        self.assertEqual(o_fut["product"], "NRML")
        self.assertEqual(o_fut["multi_session_protection"]["product"], "NRML")

    def test_flat_by_positional_without_trading_style_does_not_inherit_day_bracket(self):
        # Config has trading_style='intraday', plan only sets flat_by='positional'
        cfg = _in_cfg(trading_style="intraday")
        plan = _in_plan()
        plan.pop("trading_style")
        s = budget_mod.size_trade(plan, cfg, today="2026-10-19")
        o = budget_mod.bracket_order(s)
        self.assertEqual(o["trading_style"], "positional")
        self.assertEqual(o["time_in_force"], "gtc")
        self.assertEqual(o["order_class"], "gtt_oco")

        # Conflicting trading_style='intraday' and flat_by='positional' without explicit override fails cleanly
        bad_plan = _in_plan(trading_style="intraday", flat_by="positional")
        with self.assertRaises(budget_mod.SizeError):
            budget_mod.size_trade(bad_plan, cfg, today="2026-10-19")

    def test_cli_trading_style_override_updates_charges_and_order_semantics(self):
        intraday_plan_path = os.path.join(self.d, "intra_plan.json")
        with open(intraday_plan_path, "w", encoding="utf-8") as fh:
            json.dump(_in_plan(setup="orb", trading_style="intraday", flat_by="15:20"), fh)

        code, out, err = run_cli(budget_mod, [
            "--plan", intraday_plan_path, "--config", self.cfg_path,
            "--portfolio", self.pf_path, "--now", "2026-10-19 10:35",
            "--market", "IN", "--trading-style", "positional",
            "--write", self.orders_path, "--json",
        ])
        self.assertEqual(code, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["sizing"]["trading_style"], "positional")
        self.assertEqual(doc["sizing"]["flat_by"], "positional")
        self.assertEqual(doc["sizing"]["transaction_costs"]["brokerage"], 80.0)
        order = doc["orders"][0]
        self.assertEqual(order["time_in_force"], "gtc")
        self.assertEqual(order["order_class"], "gtt_oco")
        self.assertEqual(order["product"], "CNC")

        # Overriding a positional plan back to intraday emits intraday day/bracket/MIS and intraday charges
        pos_plan_path = os.path.join(self.d, "pos_plan.json")
        with open(pos_plan_path, "w", encoding="utf-8") as fh:
            json.dump(_in_plan(), fh)
        code_in, out_in, err_in = run_cli(budget_mod, [
            "--plan", pos_plan_path, "--config", self.cfg_path,
            "--portfolio", self.pf_path, "--now", "2026-10-19 10:35",
            "--market", "IN", "--trading-style", "intraday", "--json",
        ])
        self.assertEqual(code_in, 0, out_in + err_in)
        doc_in = json.loads(out_in)
        self.assertEqual(doc_in["sizing"]["trading_style"], "intraday")
        self.assertEqual(doc_in["sizing"]["flat_by"], "15:20")
        self.assertGreater(doc_in["sizing"]["transaction_costs"]["brokerage"], 0.0)
        self.assertLessEqual(doc_in["sizing"]["transaction_costs"]["brokerage"], 40.0)
        self.assertEqual(doc_in["orders"][0]["time_in_force"], "day")
        self.assertEqual(doc_in["orders"][0]["order_class"], "bracket")
        self.assertEqual(doc_in["orders"][0]["product"], "MIS")

    def test_unsupported_broker_or_schema_fails_clearly(self):
        plan = _in_plan()
        for bad_cfg in (
            _in_cfg(broker="alpaca"),
            _in_cfg(broker="ibkr"),
            _in_cfg(paper=False),
            _in_cfg(broker_supports_multi_session=False),
        ):
            with self.assertRaises(budget_mod.SizeError):
                budget_mod.size_trade(plan, bad_cfg, today="2026-10-19")

        # US market positional request must fail rather than emitting intraday Alpaca bracket
        with self.assertRaises(budget_mod.SizeError):
            budget_mod.size_trade(
                _in_plan(symbol="TSLA", market="US", currency="USD"),
                {"budget": 5000, "paper": True, "broker": "alpaca", "market": "US"},
                today="2026-10-19",
            )

        # Risk and broker gates must also block manually crafted/tampered invalid positional orders
        valid_s = budget_mod.size_trade(plan, _in_cfg(), today="2026-10-19")
        valid_order = budget_mod.build_order(valid_s, cfg=_in_cfg())

        invalid_variants = [
            dict(valid_order, time_in_force="day"),
            dict(valid_order, order_class="bracket"),
            dict(valid_order, product="MIS"),
            dict(valid_order, multi_session_protection={"type": "gtt_oco", "persists_across_sessions": False}),
        ]
        now_dt = risk_mod.parse_now("2026-10-19 15:10", market="IN")
        for bad_order in invalid_variants:
            res = risk_mod.check([bad_order], _in_cfg(), portfolio={"cash": 100000.0, "positions": []}, now=now_dt)
            self.assertEqual(res["results"][0]["verdict"], "BLOCK", bad_order)
            ready, label, _ = ticket_mod.status_of(bad_order, res, {"orders": []}, "2026-10-19", cfg=_in_cfg(), market="IN")
            self.assertFalse(ready)
            self.assertIn("BLOCKED", label)

        # Incompatible broker in risk/ticket blocks even a properly shaped order
        for bad_cfg in (_in_cfg(broker="alpaca"), _in_cfg(paper=False), _in_cfg(broker_supports_multi_session=False)):
            res = risk_mod.check([valid_order], bad_cfg, portfolio={"cash": 100000.0, "positions": []}, now=now_dt)
            self.assertEqual(res["results"][0]["verdict"], "BLOCK")
            ready, _, _ = ticket_mod.status_of(valid_order, res, {"orders": []}, "2026-10-19", cfg=bad_cfg, market="IN")
            self.assertFalse(ready)

    def test_tampering_positional_fields_breaks_fingerprint_and_blocks_approval(self):
        s = budget_mod.size_trade(_in_plan(), _in_cfg(), today="2026-10-19")
        o = budget_mod.build_order(s, cfg=_in_cfg())
        with open(self.orders_path, "w", encoding="utf-8") as fh:
            json.dump({"orders": [o]}, fh)
        code_r, out_r, err_r = run_cli(risk_mod, [
            "--orders", self.orders_path, "--config", self.cfg_path,
            "--portfolio", self.pf_path, "--now", "2026-10-19 15:10",
            "--market", "IN", "--out", self.risk_path,
        ])
        self.assertEqual(code_r, 0, out_r + err_r)

        # Mutate product CNC -> NRML after risk pass; fingerprint must mismatch
        tampered = copy.deepcopy(o)
        tampered["product"] = "NRML"
        self.assertNotEqual(risk_mod.fingerprint(o), risk_mod.fingerprint(tampered))
        with open(self.orders_path, "w", encoding="utf-8") as fh:
            json.dump({"orders": [tampered]}, fh)
        code_a, _, err_a = run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy %s INFY" % o["qty"],
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.cfg_path, "--state", self.broker_state,
            "--now", "2026-10-19 15:10", "--market", "IN",
        ])
        self.assertEqual(code_a, 1)
        self.assertIn("was not in the last risk check", err_a)

    def test_full_end_to_end_positional_workflow_and_alerts(self):
        off_dir = _write_breakout_offline(os.path.join(self.d, "offline_e2e"), "INFY", confirm_at=(15, 5))
        # 1. Exit plan from breakout_20d entry fixture
        self.assertEqual(run_cli(entry_mod, [
            "check", "INFY", "--setup", "breakout_20d",
            "--offline", off_dir,
            "--now", "2026-10-19 15:10", "--trading-style", "positional",
            "--market", "IN", "--write", self.entry_path,
        ])[0], 0)
        self.assertEqual(run_cli(exit_mod, [
            "plan", "--from", self.entry_path, "--config", self.cfg_path,
            "--now", "2026-10-19 15:10", "--market", "IN", "--write", self.plan_path,
        ])[0], 0)

        # 2. Alerts from positional plan must arm multi-session stop & target without flat-by error
        code_al, out_al, err_al = run_cli(alerts_mod, [
            "add", "--from-plan", "--plan", self.plan_path, "--config", self.cfg_path,
            "--rules", self.alerts_rules, "--state", self.alerts_state,
        ])
        self.assertEqual(code_al, 0, out_al + err_al)
        with open(self.alerts_rules, encoding="utf-8") as fh:
            rules_doc = json.load(fh)
        self.assertEqual(sorted(r["id"] for r in rules_doc["rules"]), ["infy-plan-stop", "infy-plan-target"])
        self.assertTrue(all(r.get("multi_session") is True and "date" not in r for r in rules_doc["rules"]))

        # 3. Budget size positional CNC GTT OCO order
        code_sz, out_sz, err_sz = run_cli(budget_mod, [
            "--plan", self.plan_path, "--config", self.cfg_path,
            "--portfolio", self.pf_path, "--now", "2026-10-19 15:10",
            "--market", "IN", "--write", self.orders_path,
        ])
        self.assertEqual(code_sz, 0, out_sz + err_sz)
        self.assertIn("positional GTT OCO order", out_sz)
        self.assertNotIn("Flat by positional", out_sz)
        with open(self.orders_path, encoding="utf-8") as fh:
            odoc = json.load(fh)
        o = odoc["orders"][0]
        qty = int(o["qty"])
        self.assertEqual(o["time_in_force"], "gtc")
        self.assertEqual(o["order_class"], "gtt_oco")
        self.assertEqual(o["product"], "CNC")

        # 4. Risk check passes at 15:10 IST (after 15:00 intraday cutoff)
        code_rk, out_rk, err_rk = run_cli(risk_mod, [
            "--orders", self.orders_path, "--config", self.cfg_path,
            "--portfolio", self.pf_path, "--state", self.broker_state,
            "--now", "2026-10-19 15:10", "--market", "IN", "--out", self.risk_path,
        ])
        self.assertEqual(code_rk, 0, out_rk + err_rk)

        # 5. Ticket show, approve, result fill, and flatten ticket generation
        code_sh, out_sh, err_sh = run_cli(ticket_mod, [
            "show", "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.cfg_path, "--state", self.broker_state,
            "--now", "2026-10-19 15:10", "--market", "IN",
        ])
        self.assertEqual(code_sh, 0, out_sh + err_sh)
        self.assertIn("GTT OCO across sessions", out_sh)
        self.assertIn("CNC", out_sh)

        code_ap, out_ap, err_ap = run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy %d INFY" % qty,
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.cfg_path, "--state", self.broker_state,
            "--now", "2026-10-19 15:10", "--market", "IN",
        ])
        self.assertEqual(code_ap, 0, out_ap + err_ap)

        code_res, out_res, err_res = run_cli(ticket_mod, [
            "result", "--index", "1", "--status", "placed",
            "--filled-qty", str(qty), "--filled-price", str(o["plan"]["entry"]),
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.cfg_path, "--portfolio", self.pf_path,
            "--state", self.broker_state, "--now", "2026-10-19 15:10", "--market", "IN",
        ])
        self.assertEqual(code_res, 0, out_res + err_res)

        # Positional close via flatten on a subsequent session day works cleanly
        code_fl, out_fl, err_fl = run_cli(ticket_mod, [
            "flatten", "--symbol", "INFY", "--orders", self.orders_path,
            "--portfolio", self.pf_path, "--plan", self.plan_path,
            "--config", self.cfg_path, "--now", "2026-10-20 10:15", "--market", "IN",
        ])
        self.assertEqual(code_fl, 0, out_fl + err_fl)
        self.assertIn("positional exit", out_fl)
        self.assertNotIn("Flat by positional IST", out_fl)

    def test_us_compatibility_preserved(self):
        with open(US_PLAN_FIXTURE, encoding="utf-8") as fh:
            us_plan = json.load(fh)
        with open(US_CFG_FIXTURE, encoding="utf-8") as fh:
            us_cfg = json.load(fh)
        s = budget_mod.size_trade(us_plan, us_cfg, today="2026-10-19")
        o = budget_mod.bracket_order(s)
        self.assertEqual(o["time_in_force"], "day")
        self.assertEqual(o["order_class"], "bracket")
        self.assertNotIn("product", o)
        self.assertNotIn("multi_session_protection", o)


if __name__ == "__main__":
    unittest.main()
