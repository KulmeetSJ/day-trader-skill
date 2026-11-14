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
from unittest.mock import patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load

entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")

_IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
_EPOCH = datetime.datetime(1970, 1, 1)


def _ist_ts(y, m, d, hh, mm):
    return int((datetime.datetime(y, m, d, hh, mm) - _IST_OFFSET - _EPOCH).total_seconds())


def _run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def _yahoo_chart_payloads(sym="RELIANCE"):
    days = []
    cur = datetime.date(2026, 10, 16)
    while len(days) < 35:
        ok, _ = entry_mod.trading_day(cur, market="IN")
        if ok:
            days.append(cur)
        cur -= datetime.timedelta(days=1)
    days.reverse()
    closes = [1310.0 + (i % 5) * 2.0 for i in range(34)] + [1335.0]
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
                    "high": [float(c) for c in closes],
                    "low": [c * 0.99 for c in closes],
                    "close": closes,
                    "volume": [2500000 for _ in closes],
                }]},
            }],
            "error": None,
        }
    }
    bars = [
        (9, 15, 1320.0, 1326.0, 1318.0, 1324.0, 120000),
        (9, 20, 1324.0, 1328.0, 1321.0, 1326.0, 130000),
        (9, 25, 1326.0, 1330.0, 1324.0, 1329.0, 125000),
        (10, 30, 1334.0, 1344.0, 1332.0, 1340.0, 210000),
    ]
    b_ts = [_ist_ts(2026, 10, 19, hh, mm) for hh, mm, *_ in bars]
    intraday_doc = {
        "chart": {
            "result": [{
                "meta": {"symbol": "%s.NS" % sym, "dataGranularity": "5m",
                         "regularMarketTime": b_ts[-1] + 5 * 60, "regularMarketPrice": bars[-1][5],
                         "previousClose": 1335.0},
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
    return json.dumps(daily_doc), json.dumps(intraday_doc)


def _in_cfg():
    return {
        "market": "IN",
        "currency": "INR",
        "budget": 100000,
        "cash_buffer_pct": 10,
        "max_position_pct": 75,
        "max_order_inr": 75000,
        "max_loss_per_trade_inr": 2000,
        "max_daily_loss_inr": 5000,
        "max_trades_per_day": 3,
        "max_orders_per_day": 6,
        "min_reward_risk": 1.5,
        "broker": "paper",
        "paper": True,
        "trading_style": "positional",
        "flat_by": "positional",
        "no_entries_before": "09:15",
        "no_entries_after": "15:25",
    }


def _sample_order(sym="RELIANCE", qty="30", entry=1340.0, stop=1324.40, target=1410.00, **meta):
    o = {
        "symbol": sym,
        "side": "buy",
        "qty": str(qty),
        "type": "market",
        "time_in_force": "gtc",
        "order_class": "gtt_oco",
        "product": "CNC",
        "trading_style": "positional",
        "take_profit_limit_price": "%.2f" % target,
        "stop_loss_stop_price": "%.2f" % stop,
        "multi_session_protection": {
            "type": "gtt_oco",
            "product": "CNC",
            "time_in_force": "gtc",
            "persists_across_sessions": True,
            "triggers": {
                "stop_loss": "%.2f" % stop,
                "take_profit": "%.2f" % target,
            },
        },
        "take_profit": {"limit_price": "%.2f" % target},
        "stop_loss": {"stop_price": "%.2f" % stop},
        "plan": {
            "symbol": sym,
            "date": "2026-10-19",
            "entry": entry,
            "stop": stop,
            "target": target,
            "flat_by": "positional",
            "trading_style": "positional",
            "market": "IN",
        },
        "market": "IN",
        "currency": "INR",
    }
    c_target = budget_mod.calculate_indian_transaction_charges(
        asset_type="EQUITY", side="buy", entry_price=entry, exit_price=target,
        qty=float(qty), trading_style="positional", as_of_date="2026-10-19",
    )
    c_stop = budget_mod.calculate_indian_transaction_charges(
        asset_type="EQUITY", side="buy", entry_price=entry, exit_price=stop,
        qty=float(qty), trading_style="positional", as_of_date="2026-10-19",
    )
    o["transaction_costs"] = c_target
    o["transaction_costs_at_stop"] = c_stop
    o["plan"]["transaction_costs"] = c_target
    o["plan"]["transaction_costs_at_stop"] = c_stop
    for k, v in meta.items():
        o[k] = v
        o["plan"][k] = v
    return o


class TestTicketSignalProvenance(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="test_ticket_prov_")
        self.cfg_path = os.path.join(self.d, "config.json")
        self.pf_path = os.path.join(self.d, "portfolio.json")
        self.entry_path = os.path.join(self.d, "entry.json")
        self.plan_path = os.path.join(self.d, "plan.json")
        self.orders_path = os.path.join(self.d, "orders.json")
        self.risk_path = os.path.join(self.d, "risk.json")
        self.state_path = os.path.join(self.d, "broker.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(_in_cfg(), fh)
        with open(self.pf_path, "w", encoding="utf-8") as fh:
            json.dump({"cash": 100000, "currency": "INR", "positions": []}, fh)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run_show(self, order, today="2026-10-19", now_str="2026-10-19 11:00", cfg_override=None, doc_extra=None, use_clock_mock=False):
        cfg = copy.deepcopy(_in_cfg())
        if cfg_override:
            cfg.update(cfg_override)
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        doc = {"created_by": "test", "paper": True, "market": "IN", "currency": "INR", "orders": [order]}
        if doc_extra:
            doc.update(doc_extra)
        with open(self.orders_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        now_utc = risk_mod.parse_now(now_str, market="IN")
        r = risk_mod.check([order], cfg, market="IN", now=now_utc)
        with open(self.risk_path, "w", encoding="utf-8") as fh:
            json.dump(r, fh)

        base_args = [
            "show", "--orders", self.orders_path, "--config", self.cfg_path,
            "--risk", self.risk_path, "--state", self.state_path,
            "--market", "IN",
        ]
        if use_clock_mock:
            with patch.object(ticket_mod, "utc_now", return_value=now_utc), \
                 patch.object(risk_mod, "utc_now", return_value=now_utc):
                code_j, out_j, err_j = _run_cli(ticket_mod, base_args + ["--json"])
                self.assertEqual(code_j, 0, out_j + err_j)
                code_t, out_t, err_t = _run_cli(ticket_mod, base_args)
                self.assertEqual(code_t, 0, out_t + err_t)
        else:
            code_j, out_j, err_j = _run_cli(ticket_mod, base_args + ["--now", now_str, "--json"])
            self.assertEqual(code_j, 0, out_j + err_j)
            code_t, out_t, err_t = _run_cli(ticket_mod, base_args + ["--now", now_str])
            self.assertEqual(code_t, 0, out_t + err_t)
        return json.loads(out_j)[0], out_t

    def test_distinguishes_all_five_signal_classes(self):
        # 1. Historical signal (Yahoo closed session / replay)
        hist_o = _sample_order(
            provider="yahoo",
            data_tier="HISTORICAL",
            is_historical_analysis=True,
            is_live_signal=False,
            is_live_data=False,
            is_fixture=False,
            last_trading_session="2026-10-19",
            last_data_timestamp="2026-10-19T15:25:00+05:30",
            signal_freshness="historical",
        )
        tj, txt = self._run_show(hist_o, now_str="2026-10-19 18:15")
        self.assertEqual(tj["data_tier"], "HISTORICAL")
        self.assertIs(tj["is_historical_analysis"], True)
        self.assertIs(tj["is_live_signal"], False)
        self.assertEqual(tj["provider"], "yahoo")
        self.assertEqual(tj["signal_classification"], "HISTORICAL")
        self.assertEqual(tj["signal_warning"], "HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION")
        self.assertIn("WARNING: HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION", txt)
        self.assertIn("signal [HISTORICAL]: tier=HISTORICAL, provider=yahoo, live_signal=no", txt)

        # 2. Delayed signal (Yahoo active-session delayed feed)
        del_o = _sample_order(
            provider="yahoo",
            data_tier="DELAYED",
            is_historical_analysis=False,
            is_live_signal=False,
            is_live_data=False,
            is_market_open=True,
            last_trading_session="2026-10-19",
            last_data_timestamp="2026-10-19T10:45:00+05:30",
            signal_freshness="delayed",
        )
        tj, txt = self._run_show(del_o, now_str="2026-10-19 11:00")
        self.assertEqual(tj["data_tier"], "DELAYED")
        self.assertIs(tj["is_historical_analysis"], False)
        self.assertIs(tj["is_live_signal"], False)
        self.assertEqual(tj["provider"], "yahoo")
        self.assertEqual(tj["signal_classification"], "DELAYED")
        self.assertIn("DELAYED SIGNAL (yahoo)", tj["signal_warning"])
        self.assertIn("WARNING: DELAYED SIGNAL (yahoo)", txt)
        self.assertIn("signal [DELAYED]: tier=DELAYED, provider=yahoo, live_signal=no", txt)

        # 3. Fixture signal (offline deterministic replay)
        fix_o = _sample_order(
            provider="offline",
            data_tier="FIXTURE",
            is_fixture=True,
            is_historical_analysis=True,
            is_live_signal=False,
            last_trading_session="2026-10-19",
            last_data_timestamp="2026-10-19T10:35:00+05:30",
            signal_freshness="stale",
        )
        tj, txt = self._run_show(fix_o, now_str="2026-10-19 10:35")
        self.assertEqual(tj["data_tier"], "FIXTURE")
        self.assertIs(tj["is_fixture"], True)
        self.assertIs(tj["is_live_signal"], False)
        self.assertEqual(tj["provider"], "offline")
        self.assertEqual(tj["signal_classification"], "FIXTURE")
        self.assertIn("FIXTURE SIGNAL", tj["signal_warning"])
        self.assertIn("WARNING: FIXTURE SIGNAL", txt)
        self.assertIn("signal [FIXTURE]: tier=FIXTURE, provider=offline, live_signal=no", txt)

        # 4. Simulated signal (analytical synthetic data)
        sim_o = _sample_order(
            provider="simulated",
            data_tier="SIMULATED",
            is_simulated=True,
            is_historical_analysis=False,
            is_live_signal=False,
            last_trading_session="2026-10-19",
            last_data_timestamp="2026-10-19T10:55:00+05:30",
        )
        tj, txt = self._run_show(sim_o, now_str="2026-10-19 11:00")
        self.assertEqual(tj["data_tier"], "SIMULATED")
        self.assertIs(tj["is_simulated"], True)
        self.assertIs(tj["is_live_signal"], False)
        self.assertEqual(tj["provider"], "simulated")
        self.assertEqual(tj["signal_classification"], "SIMULATED")
        self.assertIn("SIMULATED SIGNAL", tj["signal_warning"])
        self.assertIn("WARNING: SIMULATED SIGNAL", txt)
        self.assertIn("signal [SIMULATED]: tier=SIMULATED, provider=simulated, live_signal=no", txt)

        # 5. Genuinely live signal (verified real-time broker provider, open market, real app clock without --now)
        live_o = _sample_order(
            provider="zerodha",
            data_tier="CURRENT",
            is_historical_analysis=False,
            is_live_signal=True,
            is_live_data=True,
            is_market_open=True,
            last_trading_session="2026-10-19",
            last_data_timestamp="2026-10-19T10:59:00+05:30",
            signal_freshness="current",
        )
        tj, txt = self._run_show(live_o, now_str="2026-10-19 11:00", use_clock_mock=True)
        self.assertEqual(tj["data_tier"], "CURRENT")
        self.assertIs(tj["is_historical_analysis"], False)
        self.assertIs(tj["is_live_signal"], True)
        self.assertEqual(tj["provider"], "zerodha")
        self.assertEqual(tj["signal_classification"], "LIVE")
        self.assertIsNone(tj["signal_warning"])
        self.assertNotIn("WARNING:", txt)
        self.assertIn("signal [LIVE]: tier=CURRENT, provider=zerodha, live_signal=yes", txt)

        # 6. Same order shown with --now clock override MUST NOT be labeled LIVE
        tj_now, txt_now = self._run_show(live_o, now_str="2026-10-19 11:00", use_clock_mock=False)
        self.assertEqual(tj_now["data_tier"], "HISTORICAL")
        self.assertIs(tj_now["is_historical_analysis"], True)
        self.assertIs(tj_now["is_live_signal"], False)
        self.assertIs(tj_now["clock_override"], True)
        self.assertEqual(tj_now["signal_classification"], "HISTORICAL")
        self.assertIn("CLOCK OVERRIDDEN BY --now", tj_now["signal_warning"])
        self.assertIn("WARNING: HISTORICAL SIGNAL (CLOCK OVERRIDDEN BY --now)", txt_now)

    def test_historical_yahoo_cannot_be_displayed_as_live(self):
        # Contradictory Yahoo + historical + forged CURRENT/is_live_signal=True -> forced HISTORICAL, not live
        forged_hist = _sample_order(
            provider="yahoo",
            data_tier="CURRENT",
            is_historical_analysis=True,
            is_live_signal=True,
            is_live_data=True,
            is_market_open=True,
            last_trading_session="2026-10-19",
            last_data_timestamp="2026-10-19T15:25:00+05:30",
        )
        tj, txt = self._run_show(forged_hist, now_str="2026-10-19 18:15")
        self.assertEqual(tj["data_tier"], "HISTORICAL")
        self.assertIs(tj["is_historical_analysis"], True)
        self.assertIs(tj["is_live_signal"], False)
        self.assertIs(tj["is_live_data"], False)
        self.assertEqual(tj["provider"], "yahoo")
        self.assertEqual(tj["signal_classification"], "HISTORICAL")
        self.assertIn("HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION", txt)

        # Forged Yahoo without is_historical_analysis claiming CURRENT/live -> forced DELAYED, never LIVE
        forged_yahoo = _sample_order(
            provider="yahoo",
            data_tier="CURRENT",
            is_historical_analysis=False,
            is_live_signal=True,
            is_live_data=True,
            last_trading_session="2026-10-19",
            last_data_timestamp="2026-10-19T10:55:00+05:30",
        )
        tj2, txt2 = self._run_show(forged_yahoo, now_str="2026-10-19 11:00")
        self.assertEqual(tj2["data_tier"], "DELAYED")
        self.assertIs(tj2["is_live_signal"], False)
        self.assertIs(tj2["is_live_data"], False)
        self.assertNotEqual(tj2["signal_classification"], "LIVE")
        self.assertIn("DELAYED SIGNAL (yahoo)", txt2)

        # Even a non-Yahoo provider carrying a prior-session candle date cannot display as LIVE
        stale_live = _sample_order(
            provider="zerodha",
            data_tier="CURRENT",
            is_historical_analysis=False,
            is_live_signal=True,
            is_live_data=True,
            last_trading_session="2026-10-16",
            last_data_timestamp="2026-10-16T15:25:00+05:30",
        )
        tj3, txt3 = self._run_show(stale_live, now_str="2026-10-19 11:00")
        self.assertEqual(tj3["data_tier"], "HISTORICAL")
        self.assertIs(tj3["is_historical_analysis"], True)
        self.assertIs(tj3["is_live_signal"], False)
        self.assertIn("HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION", txt3)

    def test_missing_and_partial_metadata_handled_safely(self):
        # 1. Order with zero provenance metadata -> never live, safe UNKNOWN/UNVERIFIED
        bare_o = _sample_order()
        tj, txt = self._run_show(bare_o, now_str="2026-10-19 11:00")
        self.assertEqual(tj["data_tier"], "UNKNOWN")
        self.assertIs(tj["is_historical_analysis"], False)
        self.assertIs(tj["is_live_signal"], False)
        self.assertIsNone(tj["provider"])
        self.assertEqual(tj["signal_classification"], "UNVERIFIED")
        self.assertIn("UNVERIFIED SIGNAL PROVENANCE", tj["signal_warning"])
        self.assertIn("WARNING: UNVERIFIED SIGNAL PROVENANCE", txt)

        # 2. Missing provider even with CURRENT / is_live_signal=True -> UNVERIFIED, never live
        no_prov = _sample_order(data_tier="CURRENT", is_live_signal=True, is_live_data=True)
        tj_np, _ = self._run_show(no_prov, now_str="2026-10-19 11:00")
        self.assertEqual(tj_np["data_tier"], "UNKNOWN")
        self.assertIs(tj_np["is_live_signal"], False)
        self.assertEqual(tj_np["signal_classification"], "UNVERIFIED")

        # 3. Provenance present only on order["plan"] is recovered and still passes off-hours risk check
        plan_only = _sample_order()
        plan_only["plan"].update({
            "provider": "yahoo",
            "data_tier": "HISTORICAL",
            "is_historical_analysis": True,
            "is_live_signal": False,
            "last_trading_session": "2026-10-19",
            "last_data_timestamp": "2026-10-19T15:25:00+05:30",
        })
        tj_plan, txt_plan = self._run_show(plan_only, now_str="2026-10-19 18:15")
        self.assertTrue(tj_plan["ready"])
        self.assertEqual(tj_plan["data_tier"], "HISTORICAL")
        self.assertIs(tj_plan["is_historical_analysis"], True)
        self.assertIs(tj_plan["is_live_signal"], False)
        self.assertEqual(tj_plan["provider"], "yahoo")
        self.assertIn("HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION", txt_plan)

        # 4. Provenance present only on orders.json top-level envelope is also recovered cleanly
        bare_for_doc = _sample_order()
        tj_doc, txt_doc = self._run_show(
            bare_for_doc,
            now_str="2026-10-19 11:00",
            doc_extra={
                "provider": "yahoo",
                "data_tier": "HISTORICAL",
                "is_historical_analysis": True,
                "is_live_signal": False,
                "last_trading_session": "2026-10-19",
                "last_data_timestamp": "2026-10-19T10:35:00+05:30",
            },
        )
        self.assertTrue(tj_doc["ready"])
        self.assertEqual(tj_doc["data_tier"], "HISTORICAL")
        self.assertIs(tj_doc["is_historical_analysis"], True)
        self.assertIs(tj_doc["is_live_signal"], False)
        self.assertEqual(tj_doc["provider"], "yahoo")
        self.assertIn("HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION", txt_doc)

    def test_end_to_end_yahoo_historical_pipeline_and_three_gate_approval(self):
        daily_raw, intra_raw = _yahoo_chart_payloads("RELIANCE")

        def fake_yahoo(url):
            if "interval=1d" in url:
                return daily_raw
            return intra_raw

        # Closed-market analysis at 2026-10-19 18:13 IST on mocked Yahoo data
        sim_now = "2026-10-19 18:13"
        with patch.object(entry_mod, "yahoo_get", side_effect=fake_yahoo):
            code_e, out_e, err_e = _run_cli(entry_mod, [
                "check", "RELIANCE", "--setup", "breakout_20d",
                "--market", "IN", "--trading-style", "positional",
                "--now", sim_now, "--write", self.entry_path, "--json",
            ])
        self.assertEqual(code_e, 0, out_e + err_e)
        entry_doc = json.loads(out_e)
        self.assertEqual(entry_doc["provider"], "yahoo")
        self.assertEqual(entry_doc["data_tier"], "HISTORICAL")
        self.assertIs(entry_doc["is_historical_analysis"], True)
        self.assertIs(entry_doc["is_live_signal"], False)

        # Exit -> Plan preserves provenance
        code_x, out_x, err_x = _run_cli(exit_mod, [
            "plan", "--from", self.entry_path, "--target", "4R", "--config", self.cfg_path,
            "--market", "IN", "--now", sim_now, "--write", self.plan_path, "--json",
        ])
        self.assertEqual(code_x, 0, out_x + err_x)
        with open(self.plan_path, encoding="utf-8") as fh:
            plan_doc = json.load(fh)
        self.assertEqual(plan_doc["data_tier"], "HISTORICAL")
        self.assertEqual(plan_doc["provider"], "yahoo")
        self.assertIs(plan_doc["is_historical_analysis"], True)
        self.assertIs(plan_doc["is_live_signal"], False)

        # Size -> Orders propagates provenance to order, plan, and orders.json envelope
        code_s, out_s, err_s = _run_cli(budget_mod, [
            "--plan", self.plan_path, "--config", self.cfg_path,
            "--portfolio", self.pf_path, "--market", "IN", "--now", sim_now,
            "--write", self.orders_path, "--json",
        ])
        self.assertEqual(code_s, 0, out_s + err_s)
        with open(self.orders_path, encoding="utf-8") as fh:
            orders_doc = json.load(fh)
        self.assertEqual(orders_doc["data_tier"], "HISTORICAL")
        self.assertEqual(orders_doc["provider"], "yahoo")
        self.assertIs(orders_doc["is_historical_analysis"], True)
        self.assertIs(orders_doc["is_live_signal"], False)
        order = orders_doc["orders"][0]
        self.assertEqual(order["data_tier"], "HISTORICAL")
        self.assertIs(order["is_live_signal"], False)

        # Risk -> valid historical paper analysis is NOT rejected solely for being historical
        code_r, out_r, err_r = _run_cli(risk_mod, [
            "--orders", self.orders_path, "--config", self.cfg_path,
            "--portfolio", self.pf_path, "--state", self.state_path,
            "--market", "IN", "--now", sim_now, "--out", self.risk_path, "--json",
        ])
        self.assertEqual(code_r, 0, out_r + err_r)
        risk_doc = json.loads(out_r)
        self.assertEqual(risk_doc["results"][0]["verdict"], "PASS")
        self.assertEqual(risk_doc["results"][0]["data_tier"], "HISTORICAL")

        # Ticket show --json & text expose full provenance and historical warning
        code_tj, out_tj, err_tj = _run_cli(ticket_mod, [
            "show", "--orders", self.orders_path, "--config", self.cfg_path,
            "--risk", self.risk_path, "--state", self.state_path,
            "--market", "IN", "--now", sim_now, "--json",
        ])
        self.assertEqual(code_tj, 0, out_tj + err_tj)
        tjson = json.loads(out_tj)[0]
        self.assertTrue(tjson["ready"])
        self.assertEqual(tjson["data_tier"], "HISTORICAL")
        self.assertIs(tjson["is_historical_analysis"], True)
        self.assertIs(tjson["is_live_signal"], False)
        self.assertEqual(tjson["provider"], "yahoo")
        self.assertEqual(tjson["signal_classification"], "HISTORICAL")
        self.assertEqual(tjson["signal_warning"], "HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION")
        self.assertEqual(tjson["last_data_timestamp"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(tjson["last_trading_session"], "2026-10-19")
        self.assertEqual(tjson["signal_freshness"], "historical")

        code_tt, out_tt, err_tt = _run_cli(ticket_mod, [
            "show", "--orders", self.orders_path, "--config", self.cfg_path,
            "--risk", self.risk_path, "--state", self.state_path,
            "--market", "IN", "--now", sim_now,
        ])
        self.assertEqual(code_tt, 0, out_tt + err_tt)
        self.assertIn("WARNING: HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION", out_tt)
        self.assertIn("signal [HISTORICAL]: tier=HISTORICAL, provider=yahoo, live_signal=no", out_tt)

        # Three-gate approval still requires exact ticker/side/qty & valid fingerprint
        qty = int(order["qty"])
        code_bad, _, _ = _run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy %d TCS" % qty,
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.cfg_path, "--state", self.state_path,
            "--market", "IN", "--now", sim_now,
        ])
        self.assertEqual(code_bad, 1)

        code_ok, out_ok, err_ok = _run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy %d RELIANCE" % qty,
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.cfg_path, "--state", self.state_path,
            "--market", "IN", "--now", sim_now,
        ])
        self.assertEqual(code_ok, 0, out_ok + err_ok)
        with open(self.state_path, encoding="utf-8") as fh:
            st = json.load(fh)
        rec = st["orders"][0]
        with open(self.risk_path, encoding="utf-8") as fh:
            checked_risk = json.load(fh)
        self.assertEqual(rec["fingerprint"], checked_risk["results"][0]["fingerprint"])
        self.assertEqual(rec["data_tier"], "HISTORICAL")
        self.assertIs(rec["is_historical_analysis"], True)
        self.assertIs(rec["is_live_signal"], False)
        self.assertEqual(rec["provider"], "yahoo")

        # Tampering the order after risk check breaks fingerprint and blocks ticket show/approve
        tampered = copy.deepcopy(order)
        tampered["qty"] = str(qty + 1)
        with open(self.orders_path, "w", encoding="utf-8") as fh:
            json.dump({"orders": [tampered]}, fh)
        code_tamp, _, err_tamp = _run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy %d RELIANCE" % (qty + 1),
            "--orders", self.orders_path, "--risk", self.risk_path,
            "--config", self.cfg_path, "--state", self.state_path,
            "--market", "IN", "--now", sim_now,
        ])
        self.assertEqual(code_tamp, 1)
        self.assertIn("was not in the last risk check", err_tamp)


if __name__ == "__main__":
    unittest.main()
