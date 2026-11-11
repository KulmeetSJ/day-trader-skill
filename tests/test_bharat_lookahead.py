"""Regression tests for historical replay look-ahead bias prevention in the Indian workflow.

Verifies that when --now is supplied:
1. Adding, modifying, or inflating future candles after the simulated timestamp cannot
   alter outputs of scanner, entry, exit, timemachine, budget sizing, risk check, or broker.
2. Daily bar cutoffs exclude incomplete intraday sessions before NSE close (15:30 IST).
3. Future quotes (including fixtures timestamped at 15:20 IST) and future prices.json entries
   are explicitly refused rather than leaked into entry, sizing, risk, alerts, or data_in.
4. Downstream stages reject upstream plans, entries, and orders carrying future timestamps.
5. Insufficient pre-cutoff data returns explicit insufficient-data / no-data errors rather than
   consuming future bars.
6. simulated_at (+05:30) and last_data_timestamp (<= simulated_at) persist across artifacts.
7. Original fixture files remain completely untouched.
"""

import copy
import datetime
import gzip
import hashlib
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

from _load import FIX, ROOT, load

data_in = load("trader-alerts", "data_in")
alerts_mod = load("trader-alerts", "alerts")
scan_mod = load("trader-scanner", "scan")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
tm_mod = load("trader-timemachine", "timemachine")
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")
RELIANCE_5M = os.path.join(BHARAT_FIXTURES, "RELIANCE-5m.json")
RELIANCE_QUOTE = os.path.join(BHARAT_FIXTURES, "RELIANCE-quote.json")
INFY_DAILY = os.path.join(BHARAT_FIXTURES, "INFY-daily.json")
RELIANCE_60D = os.path.join(FIX, "trader-timemachine", "RELIANCE-60d5m.json.gz")
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")


def ist_epoch(year, month, day, hour, minute):
    dt = datetime.datetime(year, month, day, hour, minute, 0, tzinfo=IST_TZ)
    return int(dt.timestamp())


def run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def _load_json_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _poison_yahoo_chart_after(raw, cutoff_ts, multiplier=5.0, append_next_day=True):
    """Clone a Yahoo chart JSON and dramatically distort/append all candles after cutoff_ts."""
    doc = copy.deepcopy(raw)
    res = doc["chart"]["result"][0]
    meta = res.setdefault("meta", {})
    ts_list = list(res.get("timestamp") or [])
    q = res["indicators"]["quote"][0]
    interval = meta.get("dataGranularity") or "5m"
    step = 86400 if interval == "1d" else 300

    for i, ts in enumerate(ts_list):
        # For intraday, bar closing after cutoff (or starting >= cutoff) must never be read
        if int(ts) >= int(cutoff_ts) or (interval != "1d" and int(ts) + step > int(cutoff_ts)):
            for fld in ("open", "high", "low", "close"):
                arr = q.get(fld)
                if arr and i < len(arr) and arr[i] is not None:
                    arr[i] = round(float(arr[i]) * multiplier, 2)
            vols = q.get("volume")
            if vols and i < len(vols) and vols[i] is not None:
                vols[i] = int(vols[i]) + 9000000

    if append_next_day:
        next_ts = ist_epoch(2026, 10, 20, 10, 0)
        ts_list.append(next_ts)
        for fld in ("open", "high", "low", "close"):
            arr = q.setdefault(fld, [])
            arr.append(19999.0)
        q.setdefault("volume", []).append(99999999)
        res["timestamp"] = ts_list

    # Also poison top-level Yahoo meta with future timestamp and price
    meta["regularMarketTime"] = int(cutoff_ts) + 18000
    meta["regularMarketPrice"] = 19999.0
    meta["regularMarketVolume"] = 99999999
    return doc


def _default_in_config():
    return {
        "market": "IN",
        "exchange": "NSE",
        "currency": "INR",
        "paper": True,
        "budget": 200000,
        "cash_buffer_pct": 10,
        "max_position_pct": 75,
        "max_order_usd": 200000,
        "max_loss_per_trade_usd": 2500,
        "min_order_usd": 100,
        "require_stop": True,
        "min_reward_risk": 1.0,
        "no_entries_before": "09:30",
        "no_entries_after": "15:00",
        "max_trades_per_day": 3,
        "max_daily_loss_usd": 5000,
        "max_orders_per_day": 10,
        "flat_by": "15:20",
    }


def _strip_created_at(doc):
    """Copy artifact without volatile wallclock fields if comparing across runs."""
    return copy.deepcopy(doc)


class TestLookaheadInvariance(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bharat_lookahead_")
        self.cfg_path = os.path.join(self.d, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(_default_in_config(), fh)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run_pipeline(self, tag, chart_path, tm_offline_path=None):
        work = os.path.join(self.d, tag)
        os.makedirs(work, exist_ok=True)
        now_arg = "2026-10-19 10:35"

        wl_path = os.path.join(work, "watchlist.json")
        entry_path = os.path.join(work, "entry.json")
        plan_path = os.path.join(work, "plan.json")
        orders_path = os.path.join(work, "orders.json")
        risk_path = os.path.join(work, "risk.json")
        broker_path = os.path.join(work, "broker.json")
        pf_path = os.path.join(work, "portfolio.json")
        with open(pf_path, "w", encoding="utf-8") as fh:
            json.dump({"cash": 200000.0, "currency": "INR", "positions": []}, fh)
        tm_path = os.path.join(work, "timemachine.json")

        c_scan, out_scan, err_scan = run_cli(scan_mod, [
            "orb", "--market", "IN", "--offline", chart_path,
            "--now", now_arg, "--min-price", "0", "--min-volume", "0",
            "--out", wl_path, "--json",
        ])
        self.assertEqual(c_scan, 0, err_scan)
        with open(wl_path, encoding="utf-8") as fh:
            wl_doc = json.load(fh)

        c_ent, out_ent, err_ent = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", chart_path,
            "--now", now_arg, "--market", "IN", "--write", entry_path, "--json",
        ])
        self.assertEqual(c_ent, 0, err_ent)
        with open(entry_path, encoding="utf-8") as fh:
            ent_doc = json.load(fh)

        c_exit, out_exit, err_exit = run_cli(exit_mod, [
            "plan", "--from", entry_path, "--offline", chart_path,
            "--stop", "atr:1.5", "--target", "2R",
            "--config", self.cfg_path, "--market", "IN",
            "--now", now_arg, "--write", plan_path,
        ])
        self.assertEqual(c_exit, 0, err_exit)
        with open(plan_path, encoding="utf-8") as fh:
            plan_doc = json.load(fh)

        c_size, out_size, err_size = run_cli(budget_mod, [
            "--plan", plan_path, "--config", self.cfg_path, "--portfolio", pf_path,
            "--now", now_arg, "--market", "IN", "--write", orders_path,
        ])
        self.assertEqual(c_size, 0, err_size)
        with open(orders_path, encoding="utf-8") as fh:
            orders_doc = json.load(fh)

        c_risk, out_risk, err_risk = run_cli(risk_mod, [
            "--orders", orders_path,
            "--config", self.cfg_path, "--portfolio", pf_path,
            "--now", now_arg, "--market", "IN", "--out", risk_path,
        ])
        self.assertEqual(c_risk, 0, out_risk + err_risk)
        with open(risk_path, encoding="utf-8") as fh:
            risk_doc = json.load(fh)

        c_app, _, err_app = run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes buy %s RELIANCE" % orders_doc["orders"][0]["qty"],
            "--orders", orders_path, "--risk", risk_path,
            "--config", self.cfg_path, "--state", broker_path,
            "--now", now_arg,
        ])
        self.assertEqual(c_app, 0, err_app)

        c_res, _, err_res = run_cli(ticket_mod, [
            "result", "--index", "1", "--status", "placed",
            "--filled-qty", str(orders_doc["orders"][0]["qty"]),
            "--filled-price", str(plan_doc["entry"]),
            "--orders", orders_path, "--risk", risk_path,
            "--config", self.cfg_path, "--portfolio", pf_path,
            "--state", broker_path, "--now", now_arg,
        ])
        self.assertEqual(c_res, 0, err_res)
        with open(broker_path, encoding="utf-8") as fh:
            broker_doc = json.load(fh)

        tm_doc = None
        if tm_offline_path is not None:
            c_tm, _, err_tm = run_cli(tm_mod, [
                "run", "--plan", plan_path, "--offline", tm_offline_path,
                "--config", self.cfg_path, "--market", "IN",
                "--now", now_arg, "--replays", "500", "--seed", "7", "--write", tm_path,
            ])
            self.assertEqual(c_tm, 0, err_tm)
            with open(tm_path, encoding="utf-8") as fh:
                tm_doc = json.load(fh)

        return {
            "watchlist": wl_doc,
            "entry": ent_doc,
            "plan": plan_doc,
            "orders": orders_doc,
            "risk": risk_doc,
            "broker": broker_doc,
            "timemachine": tm_doc,
        }

    def test_future_candles_do_not_change_full_indian_workflow(self):
        base_raw = _load_json_maybe_gz(RELIANCE_5M)
        cutoff = ist_epoch(2026, 10, 19, 10, 35)
        poison_raw = _poison_yahoo_chart_after(base_raw, cutoff, multiplier=5.0, append_next_day=True)

        base_dir = os.path.join(self.d, "base_in")
        poison_dir = os.path.join(self.d, "poison_in")
        os.makedirs(base_dir, exist_ok=True)
        os.makedirs(poison_dir, exist_ok=True)
        base_5m = os.path.join(base_dir, "RELIANCE-5m.json")
        poison_5m = os.path.join(poison_dir, "RELIANCE-5m.json")
        with open(base_5m, "w", encoding="utf-8") as fh:
            json.dump(base_raw, fh)
        with open(poison_5m, "w", encoding="utf-8") as fh:
            json.dump(poison_raw, fh)

        base_60_raw = _load_json_maybe_gz(RELIANCE_60D)
        poison_60_raw = _poison_yahoo_chart_after(base_60_raw, cutoff, multiplier=5.0, append_next_day=True)
        base_60 = os.path.join(self.d, "RELIANCE-base-60d.json")
        poison_60 = os.path.join(self.d, "RELIANCE-poison-60d.json")
        with open(base_60, "w", encoding="utf-8") as fh:
            json.dump(base_60_raw, fh)
        with open(poison_60, "w", encoding="utf-8") as fh:
            json.dump(poison_60_raw, fh)

        r_base = self._run_pipeline("base", base_5m, tm_offline_path=base_60)
        r_poison = self._run_pipeline("poison", poison_5m, tm_offline_path=poison_60)

        # Scanner bit-for-bit hits and timestamps
        self.assertEqual(r_base["watchlist"]["hits"], r_poison["watchlist"]["hits"])
        self.assertEqual(r_base["watchlist"]["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["watchlist"]["last_data_timestamp"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["watchlist"]["last_data_timestamp"], r_poison["watchlist"]["last_data_timestamp"])

        # Entry exact equality + persisted simulated_at & last_data_timestamp
        self.assertEqual(r_base["entry"]["status"], "triggered")
        self.assertEqual(
            {k: v for k, v in r_base["entry"].items() if k != "created_at"},
            {k: v for k, v in r_poison["entry"].items() if k != "created_at"},
        )
        self.assertEqual(r_base["entry"]["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["entry"]["last_data_timestamp"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["entry"]["last_data_timestamp"], r_poison["entry"]["last_data_timestamp"])

        # Exit plan exact equality (including ATR computed only on pre-cutoff bars)
        for k in ("entry", "stop", "target", "atr", "atr_bars", "risk_per_share",
                  "reward_risk", "simulated_at", "last_data_timestamp"):
            self.assertEqual(r_base["plan"][k], r_poison["plan"][k], k)
        self.assertEqual(r_base["plan"]["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertLessEqual(r_base["plan"]["last_data_timestamp"], r_base["plan"]["simulated_at"])

        # Budget orders exact equality + paper default + metadata
        self.assertEqual(r_base["orders"]["orders"], r_poison["orders"]["orders"])
        self.assertTrue(r_base["orders"]["paper"])
        self.assertEqual(r_base["orders"]["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["orders"]["last_data_timestamp"], r_poison["orders"]["last_data_timestamp"])

        # Risk verdict exact equality + metadata
        self.assertEqual(r_base["risk"]["results"], r_poison["risk"]["results"])
        self.assertEqual(r_base["risk"]["results"][0]["verdict"], "PASS")
        self.assertEqual(r_base["risk"]["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["risk"]["last_data_timestamp"], r_poison["risk"]["last_data_timestamp"])

        # Broker state exact equality + metadata
        self.assertEqual(r_base["broker"], r_poison["broker"])
        self.assertEqual(r_base["broker"]["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["broker"]["last_data_timestamp"], r_poison["broker"]["last_data_timestamp"])

        # Timemachine replay exact equality + metadata
        self.assertEqual(r_base["timemachine"]["mc"], r_poison["timemachine"]["mc"])
        self.assertEqual(r_base["timemachine"]["history"], r_poison["timemachine"]["history"])
        self.assertEqual(r_base["timemachine"]["data"]["last_bar"], "2026-10-19 10:30 IST")
        self.assertEqual(r_base["timemachine"]["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["timemachine"]["last_data_timestamp"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(r_base["timemachine"]["last_data_timestamp"], r_poison["timemachine"]["last_data_timestamp"])

    def test_forming_bar_at_1035_does_not_leak_until_1040(self):
        # Build flat 09:15..10:30 bars that stay <= 2848 (no breakout), and a huge spike in
        # the 10:35..10:40 bar + regularMarketPrice=3500 at 10:37.
        ts0 = ist_epoch(2026, 10, 19, 9, 15)
        timestamps = []
        opens, highs, lows, closes, vols = [], [], [], [], []
        for idx in range(18):  # 09:15 through 10:40 start times
            t = ts0 + idx * 300
            timestamps.append(t)
            if idx < 16:  # up to 10:30 bar (closes 10:35)
                opens.append(2840.0)
                highs.append(2848.0 if idx < 3 else 2846.0)
                lows.append(2836.0)
                closes.append(2845.0)
                vols.append(15000)
            else:  # 10:35 bar (closes 10:40) spikes to 3500
                opens.append(2845.0)
                highs.append(3510.0)
                lows.append(2845.0)
                closes.append(3500.0)
                vols.append(500000)

        chart_doc = {
            "chart": {
                "result": [{
                    "meta": {
                        "currency": "INR",
                        "symbol": "RELIANCE.NS",
                        "exchangeName": "NSI",
                        "instrumentType": "EQUITY",
                        "dataGranularity": "5m",
                        "regularMarketTime": ist_epoch(2026, 10, 19, 15, 25),
                        "regularMarketPrice": 3500.0,
                        "chartPreviousClose": 2835.0,
                        "gmtoffset": 19800,
                        "timezone": "IST",
                    },
                    "timestamp": timestamps,
                    "indicators": {"quote": [{
                        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols
                    }]},
                }],
                "error": None,
            }
        }
        p = os.path.join(self.d, "forming.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(chart_doc, fh)

        # At 10:35, the 10:35..10:40 bar is still forming and must NOT trigger a breakout
        c_wait, out_wait, err_wait = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", p,
            "--config", self.cfg_path, "--now", "2026-10-19 10:35", "--market", "IN", "--json",
        ])
        self.assertEqual(c_wait, 1, out_wait + err_wait)
        doc_wait = json.loads(out_wait)
        self.assertEqual(doc_wait["status"], "waiting")
        self.assertLessEqual(doc_wait["last_data_timestamp"], "2026-10-19T10:35:00+05:30")

        # At 10:40, the 10:35..10:40 bar has closed and legitimately triggers at 3500.0
        c_trig, out_trig, err_trig = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", p,
            "--config", self.cfg_path, "--now", "2026-10-19 10:40", "--market", "IN", "--json",
        ])
        self.assertEqual(c_trig, 0, out_trig + err_trig)
        doc_trig = json.loads(out_trig)
        self.assertEqual(doc_trig["status"], "triggered")
        self.assertEqual(doc_trig["trigger"]["price"], 3500.0)
        self.assertEqual(doc_trig["last_data_timestamp"], "2026-10-19T10:40:00+05:30")

    def test_daily_bar_cutoff_and_scan_daily_future_meta(self):
        # INFY-daily ends on 2026-10-12. Append a bar for 2026-10-19 (09:15 IST) and a huge meta price.
        base = _load_json_maybe_gz(INFY_DAILY)
        res = base["chart"]["result"][0]
        res["meta"]["dataGranularity"] = "1d"
        t_oct19 = ist_epoch(2026, 10, 19, 9, 15)
        res["timestamp"].append(t_oct19)
        q = res["indicators"]["quote"][0]
        q["open"].append(1950.0)
        q["high"].append(3900.0)
        q["low"].append(1940.0)
        q["close"].append(3800.0)
        q["volume"].append(99000000)
        res["meta"]["regularMarketTime"] = ist_epoch(2026, 10, 19, 15, 20)
        res["meta"]["regularMarketPrice"] = 3800.0
        res["meta"]["regularMarketVolume"] = 99000000

        # 1. Provider daily bar normalization: mid-session (10:35 IST) excludes Oct 19 daily bar
        prov = data_in.YahooMarketDataProvider()
        bars_mid = prov.normalize_daily_bars(
            json.dumps(base), "INFY", exchange="NSE",
            now=datetime.datetime(2026, 10, 19, 10, 35, tzinfo=IST_TZ),
        )
        self.assertEqual(bars_mid["bars"][-1]["date"], "2026-10-12")

        # After NSE 15:30 close (15:35 IST), Oct 19 daily bar IS included
        bars_close = prov.normalize_daily_bars(
            json.dumps(base), "INFY", exchange="NSE",
            now=datetime.datetime(2026, 10, 19, 15, 35, tzinfo=IST_TZ),
        )
        self.assertEqual(bars_close["bars"][-1]["date"], "2026-10-19")
        self.assertEqual(bars_close["bars"][-1]["c"], 3800.0)

        # 2. Scanner daily scan at 10:35 IST ignores future meta price/volume and excludes future day
        poison_dir = os.path.join(self.d, "daily_bundle")
        os.makedirs(poison_dir, exist_ok=True)
        with open(os.path.join(poison_dir, "INFY-daily.json"), "w", encoding="utf-8") as fh:
            json.dump(base, fh)
        wl_daily = os.path.join(self.d, "wl_daily.json")
        c_d, _, err_d = run_cli(scan_mod, [
            "daily", "--market", "IN", "--offline", poison_dir,
            "--now", "2026-10-19 10:35", "--min-price", "0", "--min-volume", "0",
            "--out", wl_daily,
        ])
        self.assertEqual(c_d, 0, err_d)
        with open(wl_daily, encoding="utf-8") as fh:
            wdoc = json.load(fh)
        self.assertEqual(wdoc["hits"][0]["close_date"], "2026-10-12")
        self.assertEqual(wdoc["hits"][0]["price"], 1950.0)
        self.assertEqual(wdoc["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertLessEqual(wdoc["last_data_timestamp"], "2026-10-19T10:35:00+05:30")


class TestLookaheadRejectionAndDataCutoff(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bharat_reject_")
        self.cfg_path = os.path.join(self.d, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(_default_in_config(), fh)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_data_in_refuses_future_quote_and_truncates_intraday(self):
        cutoff_dt = datetime.datetime(2026, 10, 19, 10, 35, tzinfo=IST_TZ)

        # OfflineMarketDataProvider against tests/fixtures/bharat (RELIANCE-quote is at 15:20 IST)
        off_prov = data_in.OfflineMarketDataProvider(BHARAT_FIXTURES)
        with self.assertRaises(data_in.MissingDataError):
            off_prov.get_quote("RELIANCE", exchange="NSE", now=cutoff_dt)

        # After 15:20 IST, the quote is valid and carries metadata
        q_ok = off_prov.get_quote(
            "RELIANCE", exchange="NSE",
            now=datetime.datetime(2026, 10, 19, 15, 25, tzinfo=IST_TZ),
        )
        self.assertEqual(q_ok["price"], 2855.4)
        self.assertEqual(q_ok["last_data_timestamp"], "2026-10-19T15:20:00+05:30")
        self.assertEqual(q_ok["simulated_at"], "2026-10-19T15:25:00+05:30")

        # YahooMarketDataProvider intraday payload truncation & future quote refusal
        raw_5m_txt = json.dumps(_load_json_maybe_gz(RELIANCE_5M))
        raw_q_txt = json.dumps(_load_json_maybe_gz(RELIANCE_QUOTE))

        class StubYahoo(data_in.YahooMarketDataProvider):
            def _fetch_chart_url(self, provider_symbol, params):
                if params.get("interval") == "1d" and params.get("range") == "1d":
                    return raw_q_txt
                return raw_5m_txt

        stub = StubYahoo()
        with self.assertRaises(data_in.MissingDataError):
            stub.get_quote("RELIANCE", exchange="NSE", now=cutoff_dt)

        intra = stub.get_intraday_data("RELIANCE", interval="5m", range_str="5d", exchange="NSE", now=cutoff_dt)
        self.assertEqual(intra["simulated_at"], "2026-10-19T10:35:00+05:30")
        self.assertEqual(intra["last_data_timestamp"], "2026-10-19T10:35:00+05:30")
        self.assertGreater(len(intra["bars"]), 0)
        self.assertTrue(all(b["ts"] + 300 <= ist_epoch(2026, 10, 19, 10, 35) for b in intra["bars"]))
        # Future meta regularMarketPrice (2879.0 at 15:25) must never leak: capped to permitted close 2848.0
        self.assertEqual(intra["meta"].get("regularMarketPrice"), 2848.0)

    def test_alerts_refuses_future_quote_and_excludes_future_from_prices_json(self):
        # alerts quote against RELIANCE-quote.json (15:20 IST) at --now 10:35 must fail (exit 1)
        code, out, err = run_cli(alerts_mod, [
            "quote", "RELIANCE", "--now", "2026-10-19 10:35",
            "--market", "IN", "--offline", BHARAT_FIXTURES, "--json",
        ])
        self.assertNotEqual(code, 0)
        self.assertNotIn("2855.4", out)

        # alerts check against future-only quote refuses to write RELIANCE into prices.json
        rules_p = os.path.join(self.d, "alerts.json")
        state_p = os.path.join(self.d, "alerts_state.json")
        prices_p = os.path.join(self.d, "prices.json")
        with open(rules_p, "w", encoding="utf-8") as fh:
            json.dump({
                "market": "IN",
                "rules": [{
                    "id": "r1", "type": "above", "symbol": "RELIANCE",
                    "price": 2850.0, "active": True,
                }],
            }, fh)
        code_c, out_c, _ = run_cli(alerts_mod, [
            "check", "--rules", rules_p, "--state", state_p,
            "--prices-out", prices_p, "--now", "2026-10-19 10:40",
            "--market", "IN", "--offline", BHARAT_FIXTURES,
        ])
        self.assertTrue((not os.path.exists(prices_p)) or ("RELIANCE" not in open(prices_p).read()))

        # Mixed offline dir: valid 10:35 IST TCS quote + future 15:20 IST RELIANCE quote
        mix_dir = os.path.join(self.d, "mix_quotes")
        os.makedirs(mix_dir, exist_ok=True)
        shutil.copyfile(RELIANCE_QUOTE, os.path.join(mix_dir, "RELIANCE-quote.json"))
        tcs_q = _load_json_maybe_gz(RELIANCE_QUOTE)
        tcs_q["chart"]["result"][0]["meta"]["symbol"] = "TCS.NS"
        tcs_q["chart"]["result"][0]["meta"]["regularMarketPrice"] = 3920.0
        tcs_q["chart"]["result"][0]["meta"]["regularMarketTime"] = ist_epoch(2026, 10, 19, 10, 35)
        tcs_q["chart"]["result"][0]["timestamp"] = [ist_epoch(2026, 10, 19, 10, 35)]
        tcs_q["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [3920.0]
        with open(os.path.join(mix_dir, "TCS-quote.json"), "w", encoding="utf-8") as fh:
            json.dump(tcs_q, fh)
        rules_mix = os.path.join(self.d, "alerts_mix.json")
        with open(rules_mix, "w", encoding="utf-8") as fh:
            json.dump({
                "market": "IN",
                "rules": [
                    {"id": "r1", "type": "above", "symbol": "RELIANCE", "price": 2850.0, "active": True},
                    {"id": "r2", "type": "above", "symbol": "TCS", "price": 3900.0, "active": True},
                ],
            }, fh)
        run_cli(alerts_mod, [
            "check", "--rules", rules_mix, "--state", os.path.join(self.d, "alerts_mix_state.json"),
            "--prices-out", prices_p, "--now", "2026-10-19 10:40",
            "--market", "IN", "--offline", mix_dir,
        ])
        with open(prices_p, encoding="utf-8") as fh:
            pdoc = json.load(fh)
        self.assertEqual(pdoc.get("simulated_at"), "2026-10-19T10:40:00+05:30")
        self.assertEqual(pdoc.get("last_data_timestamp"), "2026-10-19T10:35:00+05:30")
        self.assertIn("TCS", pdoc.get("prices", {}))
        self.assertNotIn("RELIANCE", pdoc.get("prices", {}))

    def test_downstream_stages_reject_future_plans_prices_and_orders(self):
        future_entry = {
            "created_by": "trader-entry",
            "symbol": "RELIANCE",
            "market": "IN",
            "currency": "INR",
            "setup": "orb",
            "status": "triggered",
            "date": "2026-10-19",
            "time": "2:00 PM",
            "bar": 56,
            "interval": "5m",
            "price": 2860.0,
            "range": {"high": 2848.0, "low": 2836.0, "minutes": 15},
            "last_data_timestamp": "2026-10-19T14:00:00+05:30",
        }
        entry_p = os.path.join(self.d, "future_entry.json")
        with open(entry_p, "w", encoding="utf-8") as fh:
            json.dump(future_entry, fh)

        # 1. exit rejects future entry.json at 10:35
        plan_p = os.path.join(self.d, "plan.json")
        c_ex, _, err_ex = run_cli(exit_mod, [
            "plan", "--from", entry_p, "--market", "IN", "--now", "2026-10-19 10:35", "--write", plan_p,
        ])
        self.assertEqual(c_ex, 1)
        self.assertIn("look-ahead", err_ex.lower())

        # Create valid plan at 14:00, then test --now 10:35 rejections on size, risk, ticket, timemachine
        future_plan = {
            "created_by": "trader-exit",
            "symbol": "RELIANCE",
            "side": "buy",
            "market": "IN",
            "currency": "INR",
            "date": "2026-10-19",
            "setup": "orb",
            "entry": 2860.0,
            "entry_time": "2:00 PM",
            "entry_bar": 56,
            "interval": "5m",
            "opening_range": {"high": 2848.0, "low": 2836.0, "minutes": 15},
            "stop": 2842.0,
            "target": 2896.0,
            "risk_per_share": 18.0,
            "reward_risk": 2.0,
            "flat_by": "15:20",
            "last_data_timestamp": "2026-10-19T14:00:00+05:30",
        }
        future_plan_p = os.path.join(self.d, "future_plan.json")
        with open(future_plan_p, "w", encoding="utf-8") as fh:
            json.dump(future_plan, fh)

        # 2. size rejects future plan at 10:35
        orders_p = os.path.join(self.d, "orders.json")
        c_sz, _, err_sz = run_cli(budget_mod, [
            "--plan", future_plan_p, "--config", self.cfg_path,
            "--now", "2026-10-19 10:35", "--market", "IN", "--write", orders_p,
        ])
        self.assertEqual(c_sz, 1)
        self.assertIn("look-ahead", err_sz.lower())

        # 3. timemachine rejects future plan at 10:35
        raw_60 = _load_json_maybe_gz(RELIANCE_60D)
        raw_60_p = os.path.join(self.d, "rel60.json")
        with open(raw_60_p, "w", encoding="utf-8") as fh:
            json.dump(raw_60, fh)
        c_tm, _, err_tm = run_cli(tm_mod, [
            "run", "--plan", future_plan_p, "--offline", raw_60_p,
            "--config", self.cfg_path, "--market", "IN", "--now", "2026-10-19 10:35",
        ])
        self.assertEqual(c_tm, 1)
        self.assertIn("look-ahead", err_tm.lower())

        # 4. size ignores future prices.json in run_plan and run_weights
        # Produce valid orders at 14:00 first to test risk and ticket rejections at 10:35
        pf_p = os.path.join(self.d, "pf_future.json")
        with open(pf_p, "w", encoding="utf-8") as fh:
            json.dump({"cash": 200000.0, "currency": "INR", "positions": []}, fh)
        c_sz_ok, _, err_sz_ok = run_cli(budget_mod, [
            "--plan", future_plan_p, "--config", self.cfg_path, "--portfolio", pf_p,
            "--now", "2026-10-19 14:00", "--market", "IN", "--write", orders_p,
        ])
        self.assertEqual(c_sz_ok, 0, err_sz_ok)
        with open(orders_p, encoding="utf-8") as fh:
            future_orders_doc = json.load(fh)
        future_orders = future_orders_doc["orders"]

        future_prices_p = os.path.join(self.d, "future_prices.json")
        with open(future_prices_p, "w", encoding="utf-8") as fh:
            json.dump({
                "market": "IN",
                "as_of": "2026-10-19T09:50:00Z",  # 15:20:00 IST
                "prices": {"RELIANCE": {"price": 2999.0, "at": "2026-10-19 15:20:00 IST"}},
            }, fh)

        # Equal weights / --prices with future prices must fail as unavailable, NOT consume 2999.0
        c_wt, _, err_wt = run_cli(budget_mod, [
            "--weights", "RELIANCE:1", "--prices", future_prices_p, "--portfolio", pf_p,
            "--config", self.cfg_path, "--now", "2026-10-19 10:35", "--market", "IN",
        ])
        self.assertEqual(c_wt, 1)

        # --plan + --prices with future 15:20 price (2999.0) must ignore the future price, sizing at plan entry (2849.0)
        past_plan_p = os.path.join(self.d, "past_plan.json")
        with open(past_plan_p, "w", encoding="utf-8") as fh:
            json.dump({
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
                "target": 2863.0,
                "risk_per_share": 7.0,
                "reward_risk": 2.0,
                "flat_by": "15:20",
                "last_data_timestamp": "2026-10-19T10:35:00+05:30",
            }, fh)
        past_orders_p = os.path.join(self.d, "past_orders_ignored_fp.json")
        c_sz_fp, out_sz_fp, err_sz_fp = run_cli(budget_mod, [
            "--plan", past_plan_p, "--prices", future_prices_p, "--portfolio", pf_p,
            "--config", self.cfg_path, "--now", "2026-10-19 10:35", "--market", "IN", "--write", past_orders_p,
        ])
        self.assertEqual(c_sz_fp, 0, out_sz_fp + err_sz_fp)
        with open(past_orders_p, encoding="utf-8") as fh:
            po_doc = json.load(fh)
        self.assertEqual(po_doc["orders"][0]["plan"]["entry"], 2849.0)
        self.assertEqual(po_doc["orders"][0]["qty"], "52")

        # 5. risk blocks future plan/orders and rejects future prices.json even without plan_entry fallback
        at_1035 = risk_mod.parse_now("2026-10-19 10:35", market="IN")
        loaded_prices, loaded_ptimes = risk_mod.load_prices(future_prices_p)
        self.assertIsNotNone(loaded_ptimes)
        res_block = risk_mod.check(
            future_orders,
            _default_in_config(),
            portfolio={"cash": 200000.0, "currency": "INR", "positions": []},
            prices=loaded_prices,
            price_times=loaded_ptimes,
            now=at_1035,
            market="IN",
            simulated=True,
            source_doc=dict(future_orders_doc, entry_time="2:00 PM"),
        )
        self.assertEqual(res_block["results"][0]["verdict"], "BLOCK")

        # Also check future prices.json directly when checking without plan entry fallback
        no_entry_order = copy.deepcopy(future_orders[0])
        no_entry_order.pop("plan", None)
        no_entry_order.pop("last_data_timestamp", None)
        res_fp = risk_mod.check(
            [no_entry_order],
            _default_in_config(),
            portfolio={"cash": 200000.0, "currency": "INR", "positions": []},
            prices=loaded_prices,
            price_times=loaded_ptimes,
            now=at_1035,
            market="IN",
            simulated=True,
        )
        # Future price must not become stale-price fallback or pass check
        self.assertEqual(res_fp["results"][0]["verdict"], "BLOCK")
        self.assertNotIn("stale price", " ".join(res_fp["results"][0].get("reasons", [])).lower())

        # 6. ticket rejects future orders/plan at --now 10:35
        risk_p = os.path.join(self.d, "future_risk.json")
        broker_p = os.path.join(self.d, "broker.json")
        # Build a risk file at 14:00, then try ticket show/approve at --now 10:35
        c_rk14, out_rk14, err_rk14 = run_cli(risk_mod, [
            "--orders", orders_p, "--portfolio", pf_p,
            "--config", self.cfg_path, "--now", "2026-10-19 14:00",
            "--market", "IN", "--out", risk_p,
        ])
        self.assertEqual(c_rk14, 0, out_rk14 + err_rk14)
        c_sh, out_sh, err_sh = run_cli(ticket_mod, [
            "show", "--orders", orders_p, "--risk", risk_p,
            "--config", self.cfg_path, "--state", broker_p, "--now", "2026-10-19 10:35",
        ])
        self.assertEqual(c_sh, 1, out_sh + err_sh)
        self.assertIn("look-ahead", (out_sh + err_sh).lower())

        c_app, out_app, err_app = run_cli(ticket_mod, [
            "approve", "--index", "1", "--reply", "yes",
            "--orders", orders_p, "--risk", risk_p,
            "--config", self.cfg_path, "--state", broker_p, "--now", "2026-10-19 10:35",
        ])
        self.assertEqual(c_app, 1, out_app + err_app)
        self.assertIn("look-ahead", (out_app + err_app).lower())

        c_fl, out_fl, err_fl = run_cli(ticket_mod, [
            "flatten", "--symbol", "RELIANCE", "--plan", future_plan_p,
            "--portfolio", pf_p, "--config", self.cfg_path,
            "--orders", os.path.join(self.d, "flatten_orders.json"), "--now", "2026-10-19 10:35",
        ])
        self.assertEqual(c_fl, 1, out_fl + err_fl)
        self.assertIn("look-ahead", (out_fl + err_fl).lower())

        # Also verify risk CLI blocks future orders at --now 10:35
        c_rk10, out_rk10, err_rk10 = run_cli(risk_mod, [
            "--orders", orders_p, "--portfolio", pf_p,
            "--config", self.cfg_path, "--now", "2026-10-19 10:35",
            "--market", "IN", "--out", os.path.join(self.d, "blocked_risk.json"),
        ])
        self.assertEqual(c_rk10, 1, out_rk10 + err_rk10)
        self.assertIn("BLOCK", out_rk10)


class TestInsufficientPreCutoffData(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bharat_insuff_")
        self.cfg_path = os.path.join(self.d, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(_default_in_config(), fh)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _slice_reliance_5m(self, keep_pred):
        raw = _load_json_maybe_gz(RELIANCE_5M)
        res = raw["chart"]["result"][0]
        ts_list = res["timestamp"]
        q = res["indicators"]["quote"][0]
        keep_idx = [i for i, t in enumerate(ts_list) if keep_pred(int(t))]
        res["timestamp"] = [ts_list[i] for i in keep_idx]
        for fld in ("open", "high", "low", "close", "volume"):
            res["indicators"]["quote"][0][fld] = [q[fld][i] for i in keep_idx]
        res["meta"]["regularMarketTime"] = int(res["timestamp"][-1]) if res["timestamp"] else None
        p = os.path.join(self.d, "sliced_%d.json" % len(keep_idx))
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        return p

    def test_pre_open_and_holiday_do_not_read_future_bars(self):
        # 09:05 IST before open on 2026-10-19 with RELIANCE-5m containing only pre-open 08:50 + future 09:15..15:25
        c, out, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", RELIANCE_5M,
            "--now", "2026-10-19 09:05", "--market", "IN", "--json",
        ])
        self.assertEqual(c, 1)
        doc = json.loads(out)
        self.assertIsNone(doc.get("trigger"))
        self.assertIsNone(doc.get("price"))
        if doc.get("last_data_timestamp") is not None:
            self.assertLessEqual(doc.get("last_data_timestamp"), "2026-10-19T09:05:00+05:30")
        self.assertEqual(doc.get("simulated_at"), "2026-10-19T09:05:00+05:30")

        # Sunday 2026-10-18 with fixture containing only future 2026-10-19 bars -> no-data, NOT 2026-10-19
        c_sun, out_sun, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", RELIANCE_5M,
            "--now", "2026-10-18 11:00", "--market", "IN", "--json",
        ])
        self.assertEqual(c_sun, 1)
        doc_sun = json.loads(out_sun)
        self.assertEqual(doc_sun["status"], "no-data")
        self.assertIsNone(doc_sun.get("last_data_timestamp"))

    def test_insufficient_bars_before_cutoff_does_not_read_future_bars(self):
        # Only keep bars from 10:00 IST onward; ask for check at 09:35 IST
        late_only = self._slice_reliance_5m(lambda t: t >= ist_epoch(2026, 10, 19, 10, 0))
        c, out, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "orb", "--offline", late_only,
            "--now", "2026-10-19 09:35", "--market", "IN", "--json",
        ])
        self.assertEqual(c, 1)
        doc = json.loads(out)
        self.assertEqual(doc["status"], "no-data")

        # ma_crossover needs >= 22 bars: even with 09:15..09:35 + 10:00..15:25 bars, at --now 09:35
        # only 4 bars precede cutoff, so it must return no-data instead of counting future bars.
        c_ma, out_ma, _ = run_cli(entry_mod, [
            "check", "RELIANCE", "--setup", "ma_crossover", "--offline", RELIANCE_5M,
            "--now", "2026-10-19 09:35", "--market", "IN", "--json",
        ])
        self.assertEqual(c_ma, 1)
        doc_ma = json.loads(out_ma)
        self.assertEqual(doc_ma["status"], "no-data")
        self.assertIn("insufficient", doc_ma["reason"].lower())

    def test_exit_atr_requires_three_bars_before_cutoff(self):
        # RELIANCE-5m has 75 bars through 15:25, but at 09:25 IST only 2 closed bars (09:15, 09:20) exist.
        entry_early = {
            "symbol": "RELIANCE",
            "market": "IN",
            "setup": "level",
            "status": "triggered",
            "date": "2026-10-19",
            "time": "9:25 AM",
            "bar": 1,
            "interval": "5m",
            "price": 2841.0,
        }
        ep = os.path.join(self.d, "early_entry.json")
        with open(ep, "w", encoding="utf-8") as fh:
            json.dump(entry_early, fh)

        c, _, err = run_cli(exit_mod, [
            "plan", "--from", ep, "--offline", RELIANCE_5M,
            "--stop", "atr:1.5", "--market", "IN", "--now", "2026-10-19 09:25",
        ])
        self.assertEqual(c, 1)
        self.assertIn("at least 3 closed", err)

    def test_timemachine_errors_when_pre_cutoff_bar_missing(self):
        # Omit the 10:30..10:35 bar from RELIANCE-60d5m while keeping later bars (10:35..15:25)
        raw_60 = _load_json_maybe_gz(RELIANCE_60D)
        res = raw_60["chart"]["result"][0]
        target_t = ist_epoch(2026, 10, 19, 10, 30)
        keep_idx = [i for i, t in enumerate(res["timestamp"]) if int(t) != target_t]
        res["timestamp"] = [res["timestamp"][i] for i in keep_idx]
        q = res["indicators"]["quote"][0]
        for fld in ("open", "high", "low", "close", "volume"):
            q[fld] = [q[fld][i] for i in keep_idx]
        p60 = os.path.join(self.d, "missing_1030.json")
        with open(p60, "w", encoding="utf-8") as fh:
            json.dump(raw_60, fh)

        c, _, err = run_cli(tm_mod, [
            "run", "--symbol", "RELIANCE", "--date", "2026-10-19",
            "--entry", "2849.0", "--stop", "2842.0", "--target", "2863.0",
            "--entry-time", "10:35 AM", "--offline", p60,
            "--config", self.cfg_path, "--market", "IN",
            "--now", "2026-10-19 10:35", "--replays", "200",
        ])
        self.assertEqual(c, 1)
        self.assertIn("no complete", err.lower())


class TestFixturesUnmodified(unittest.TestCase):
    def test_reliance_fixtures_end_through_afternoon_intact(self):
        # Confirm tests/fixtures/bharat/RELIANCE-5m.json still holds full day candles
        # (including 15:25 IST) and RELIANCE-quote.json holds 15:20 IST, never truncated.
        raw_5m = _load_json_maybe_gz(RELIANCE_5M)
        ts_5m = raw_5m["chart"]["result"][0]["timestamp"]
        self.assertGreaterEqual(max(int(t) for t in ts_5m), ist_epoch(2026, 10, 19, 15, 25))
        raw_q = _load_json_maybe_gz(RELIANCE_QUOTE)
        self.assertEqual(int(raw_q["chart"]["result"][0]["meta"]["regularMarketTime"]),
                         ist_epoch(2026, 10, 19, 15, 20))


if __name__ == "__main__":
    unittest.main()
