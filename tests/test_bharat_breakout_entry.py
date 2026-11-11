"""Regression tests for Indian daily-scanner to entry-engine breakout_20d integration."""

import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for mod_dir in ("trader-entry", "trader-exit", "trader-scanner", "trader-alerts"):
    p = os.path.join(ROOT, mod_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)
import _load  # noqa: E402,F401

import data_in  # type: ignore  # noqa: E402
import entry  # type: ignore  # noqa: E402
import exit as exit_mod  # type: ignore  # noqa: E402
import scan  # type: ignore  # noqa: E402


IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)


def _ist_ts(year, month, day, hour, minute):
    utc_dt = datetime.datetime(year, month, day, hour, minute) - IST_OFFSET
    return int((utc_dt - EPOCH).total_seconds())


def _us_ts(year, month, day, hour, minute):
    # October 2026 is EDT (UTC-4)
    utc_dt = datetime.datetime(year, month, day, hour, minute) + datetime.timedelta(hours=4)
    return int((utc_dt - EPOCH).total_seconds())


def _trading_days_before(end_date, count, market="IN"):
    days = []
    cur = end_date
    while len(days) < count:
        ok, _ = entry.trading_day(cur, market=market)
        if ok:
            days.append(cur)
        cur = cur - datetime.timedelta(days=1)
    days.reverse()
    return days


def _make_daily_chart(sym, closes_by_date, market="IN", rmt=None, gran="1d"):
    ts_list, o_list, h_list, l_list, c_list, v_list = [], [], [], [], [], []
    hr, mn = (9, 15) if market == "IN" else (9, 30)
    for d, close in closes_by_date:
        ts = _ist_ts(d.year, d.month, d.day, hr, mn) if market == "IN" else _us_ts(d.year, d.month, d.day, hr, mn)
        ts_list.append(ts)
        o_list.append(round(close * 0.995, 2))
        h_list.append(round(close * 1.005, 2))
        l_list.append(round(close * 0.99, 2))
        c_list.append(float(close))
        v_list.append(2500000)
    ysym = ("%s.NS" % sym) if (market == "IN" and not sym.endswith((".NS", ".BO"))) else sym
    meta = {
        "symbol": ysym,
        "dataGranularity": gran,
        "regularMarketTime": rmt if rmt is not None else (ts_list[-1] if ts_list else None),
        "regularMarketPrice": c_list[-1] if c_list else None,
        "regularMarketVolume": v_list[-1] if v_list else None,
        "previousClose": c_list[-2] if len(c_list) >= 2 else (c_list[-1] if c_list else None),
    }
    return {
        "chart": {
            "result": [{
                "meta": meta,
                "timestamp": ts_list,
                "indicators": {
                    "quote": [{
                        "open": o_list,
                        "high": h_list,
                        "low": l_list,
                        "close": c_list,
                        "volume": v_list,
                    }]
                },
            }],
            "error": None,
        }
    }


def _make_intraday_chart(sym, sess_date, bar_specs, market="IN", rmt=None, prev_close=None):
    """bar_specs: list of (hh, mm, o, h, l, c, v)."""
    ts_list, o_list, h_list, l_list, c_list, v_list = [], [], [], [], [], []
    for hh, mm, o, h, lo, c, v in bar_specs:
        ts = _ist_ts(sess_date.year, sess_date.month, sess_date.day, hh, mm) if market == "IN" else _us_ts(
            sess_date.year, sess_date.month, sess_date.day, hh, mm
        )
        ts_list.append(ts)
        o_list.append(float(o))
        h_list.append(float(h))
        l_list.append(float(lo))
        c_list.append(float(c))
        v_list.append(int(v))
    ysym = ("%s.NS" % sym) if (market == "IN" and not sym.endswith((".NS", ".BO"))) else sym
    meta = {
        "symbol": ysym,
        "dataGranularity": "5m",
        "regularMarketTime": rmt if rmt is not None else (ts_list[-1] + 300 if ts_list else None),
        "regularMarketPrice": c_list[-1] if c_list else None,
        "regularMarketVolume": sum(v_list),
        "previousClose": prev_close if prev_close is not None else (o_list[0] if o_list else None),
    }
    return {
        "chart": {
            "result": [{
                "meta": meta,
                "timestamp": ts_list,
                "indicators": {
                    "quote": [{
                        "open": o_list,
                        "high": h_list,
                        "low": l_list,
                        "close": c_list,
                        "volume": v_list,
                    }]
                },
            }],
            "error": None,
        }
    }


def _sample_breakout_daily_closes(end_date=datetime.date(2026, 10, 16), count=35,
                                  prior_high=2820.0, last_close=2848.0, market="IN"):
    days = _trading_days_before(end_date, count, market=market)
    closes = []
    for i, d in enumerate(days):
        if i == len(days) - 1:
            closes.append((d, float(last_close)))
        elif i == len(days) - 6:
            closes.append((d, float(prior_high)))
        else:
            closes.append((d, round(prior_high - 60.0 + (i % 15) * 3.0, 2)))
    return closes


def _standard_session_bars_5m(bar_overrides=None):
    """Generate 09:15..10:40 IST 5m bars cleanly around 2840, applying overrides by (hh, mm)."""
    overrides = dict(bar_overrides or {})
    specs = []
    cur = datetime.datetime(2026, 10, 19, 9, 15)
    while cur <= datetime.datetime(2026, 10, 19, 10, 40):
        hh, mm = cur.hour, cur.minute
        if (hh, mm) in overrides:
            o, h, lo, c, v = overrides[(hh, mm)]
        else:
            o, h, lo, c, v = 2838.0, 2842.0, 2835.0, 2840.0, 10000
        specs.append((hh, mm, o, h, lo, c, v))
        cur += datetime.timedelta(minutes=5)
    return specs


class _LiveBreakoutMockProvider(data_in.MarketDataProvider):
    provider_name = "mock_kite_live"
    CAPABILITIES = {
        data_in.CAPABILITY_LIVE_QUOTES: True,
        data_in.CAPABILITY_HISTORICAL_CANDLES: True,
        data_in.CAPABILITY_INTRADAY_CANDLES: True,
    }

    def __init__(self, intraday_bars, daily_bars, intraday_dt, intraday_status="CURRENT",
                 intraday_tier="CURRENT", daily_status="HISTORICAL", fail_daily=False,
                 fail_intraday=False, is_simulated=False):
        self.intraday_bars = intraday_bars
        self.daily_bars = daily_bars
        self.intraday_dt = intraday_dt
        self.intraday_status = intraday_status
        self.intraday_tier = intraday_tier
        self.daily_status = daily_status
        self.fail_daily = fail_daily
        self.fail_intraday = fail_intraday
        self.is_simulated_flag = is_simulated
        if is_simulated:
            self.provider_name = "simulated"
            self.CAPABILITIES = {
                data_in.CAPABILITY_HISTORICAL_CANDLES: True,
                data_in.CAPABILITY_INTRADAY_CANDLES: True,
            }

    def get_quote(self, symbol, exchange="NSE", now=None):
        return {}

    def get_intraday_data(self, symbol, interval="5m", range_str="1d", exchange="NSE", now=None):
        if self.fail_intraday:
            raise data_in.MissingDataError("intraday feed down for %s" % symbol)
        dt = self.intraday_dt
        return {
            "symbol": symbol,
            "provider": self.provider_name,
            "source": self.provider_name,
            "tier": self.intraday_tier,
            "data_status": self.intraday_status,
            "is_live": (self.intraday_tier == "CURRENT" and not self.is_simulated_flag),
            "is_simulated": self.is_simulated_flag,
            "interval": interval,
            "timestamp": dt,
            "prev_close": 2848.0,
            "meta": {"symbol": "%s.NS" % symbol, "dataGranularity": interval, "regularMarketTime": dt},
            "bars": list(self.intraday_bars),
            "last_data_timestamp": entry._market_iso(entry.utc_of(dt), market="IN") if dt else None,
        }

    def get_historical_data(self, symbol, range_str="3mo", interval="1d", exchange="NSE", now=None):
        if self.fail_daily:
            raise data_in.MissingDataError("daily history unavailable for %s" % symbol)
        return {
            "symbol": symbol,
            "provider": self.provider_name,
            "source": self.provider_name,
            "tier": "HISTORICAL",
            "data_status": self.daily_status,
            "is_live": False,
            "is_simulated": self.is_simulated_flag,
            "interval": interval,
            "meta": {"symbol": "%s.NS" % symbol, "dataGranularity": "1d"},
            "bars": list(self.daily_bars),
        }


class Breakout20dTests(unittest.TestCase):

    def test_valid_breakout_integration_with_scanner_and_exit_handoff(self):
        """scan.py daily breakout_20d -> entry.py check breakout_20d -> exit.py plan."""
        daily_closes = _sample_breakout_daily_closes(
            end_date=datetime.date(2026, 10, 16), prior_high=2820.0, last_close=2848.0
        )
        # Confirmed breakout at 10:25-10:30 bar closing 2856.00 > 2848.00 (low 2846.50)
        specs = _standard_session_bars_5m({(10, 25): (2847.0, 2857.0, 2846.5, 2856.0, 22000)})

        with tempfile.TemporaryDirectory() as tmp:
            fix_dir = os.path.join(tmp, "fixtures")
            os.makedirs(fix_dir)
            with open(os.path.join(fix_dir, "RELIANCE-daily.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_daily_chart("RELIANCE", daily_closes), fh)
            with open(os.path.join(fix_dir, "RELIANCE-5m.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), specs, prev_close=2848.0), fh)

            watchlist_path = os.path.join(tmp, "watchlist.json")
            scan_out = io.StringIO()
            with contextlib.redirect_stdout(scan_out):
                rc_scan = scan.main([
                    "daily", "--market", "IN", "--strategy", "breakout_20d",
                    "--offline", fix_dir, "--now", "2026-10-19 09:00",
                    "--min-price", "0", "--min-volume", "0", "--out", watchlist_path, "--json",
                ])
            self.assertEqual(rc_scan, 0)
            with open(watchlist_path, encoding="utf-8") as fh:
                wdoc = json.load(fh)
            self.assertEqual([h["symbol"] for h in wdoc["hits"]], ["RELIANCE"])
            self.assertIn("breakout_20d", wdoc["hits"][0]["setups"])

            # Entry check at 10:35 IST using watchlist + offline dir
            entry_path = os.path.join(tmp, "entry.json")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", fix_dir, "--now", "2026-10-19 10:35",
                    "--write", entry_path, "--json",
                ])
            self.assertEqual(rc, 0)
            res = json.loads(buf.getvalue())
            self.assertEqual(res["status"], "triggered")
            self.assertEqual(res["setup"], "breakout_20d")
            self.assertEqual(res["breakout_level"], 2848.0)
            self.assertEqual(res["confirmation_price"], 2856.0)
            self.assertEqual(res["timestamp"], "2026-10-19T10:30:00+05:30")
            self.assertEqual(res["invalidation_level"], 2846.5)
            self.assertEqual(res["signal_freshness"], "fixture")
            self.assertTrue(res["scanner_breakout_matched"])
            self.assertFalse(res["is_live_signal"])
            self.assertFalse(res["is_live_data"])
            self.assertEqual(res["data_tier"], "FIXTURE")

            # Verify human-readable CLI output also displays all five fields
            text_buf = io.StringIO()
            with contextlib.redirect_stdout(text_buf):
                rc_txt = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", fix_dir, "--now", "2026-10-19 10:35",
                ])
            self.assertEqual(rc_txt, 0)
            txt = text_buf.getvalue()
            for needle in ("Breakout level:", "Confirmation price:", "Timestamp:", "Invalidation / stop level:", "Signal freshness:"):
                self.assertIn(needle, txt)

            # Verify entry_doc handoff to exit.py without needing manual --stop
            with open(entry_path, encoding="utf-8") as fh:
                edoc = json.load(fh)
            for k in ("breakout_level", "confirmation_price", "timestamp", "invalidation_level", "signal_freshness"):
                self.assertIn(k, edoc)
            plan_path = os.path.join(tmp, "plan.json")
            exit_buf = io.StringIO()
            with contextlib.redirect_stdout(exit_buf):
                rc_exit = exit_mod.main([
                    "plan", "--from", entry_path, "--market", "IN", "--now", "2026-10-19 10:35", "--write", plan_path
                ])
            self.assertEqual(rc_exit, 0)
            with open(plan_path, encoding="utf-8") as fh:
                pdoc = json.load(fh)
            self.assertEqual(pdoc["setup"], "breakout_20d")
            self.assertEqual(pdoc["entry"], 2856.0)
            self.assertEqual(pdoc["stop"], 2846.5)

            # Verify entry.py watchlist --setup breakout_20d also works cleanly
            wl_buf = io.StringIO()
            with contextlib.redirect_stdout(wl_buf):
                rc_wl = entry.main([
                    "watchlist", "--setup", "breakout_20d", "--market", "IN",
                    "--watchlist", watchlist_path, "--offline", fix_dir,
                    "--now", "2026-10-19 10:35", "--json",
                ])
            self.assertEqual(rc_wl, 0)
            wl_rows = json.loads(wl_buf.getvalue())
            self.assertEqual(len(wl_rows), 1)
            self.assertEqual(wl_rows[0]["result"]["status"], "triggered")

            # Verify watch replay works bar-by-bar for breakout_20d and writes trigger output
            watch_entry_path = os.path.join(tmp, "watch_entry.json")
            w_buf = io.StringIO()
            with contextlib.redirect_stdout(w_buf):
                rc_w = entry.main([
                    "watch", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", fix_dir, "--now", "2026-10-19 09:20", "--until", "11:00",
                    "--out", watch_entry_path,
                ])
            self.assertEqual(rc_w, 0)
            with open(watch_entry_path, encoding="utf-8") as fh:
                wdoc_out = json.load(fh)
            self.assertEqual(wdoc_out["setup"], "breakout_20d")
            self.assertEqual(wdoc_out["confirmation_price"], 2856.0)
            self.assertEqual(wdoc_out["invalidation_level"], 2846.5)

    def test_valid_breakout_via_explicit_daily_flag_without_prior_daily_breakout(self):
        """When prior session closed below 20d high, breaking max(prior 20 closes) intraday confirms."""
        daily_closes = _sample_breakout_daily_closes(
            end_date=datetime.date(2026, 10, 16), prior_high=2860.0, last_close=2845.0
        )
        specs = _standard_session_bars_5m({(10, 25): (2858.0, 2866.0, 2857.23, 2864.5, 18000)})

        with tempfile.TemporaryDirectory() as tmp:
            daily_file = os.path.join(tmp, "RELIANCE-daily.json")
            intraday_file = os.path.join(tmp, "RELIANCE-5m.json")
            with open(daily_file, "w", encoding="utf-8") as fh:
                json.dump(_make_daily_chart("RELIANCE", daily_closes), fh)
            with open(intraday_file, "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), specs), fh)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", intraday_file, "--daily", daily_file,
                    "--now", "2026-10-19 10:35", "--json",
                ])
            self.assertEqual(rc, 0)
            res = json.loads(buf.getvalue())
            self.assertEqual(res["status"], "triggered")
            self.assertFalse(res["scanner_breakout_matched"])
            self.assertEqual(res["breakout_level"], 2860.0)
            self.assertEqual(res["confirmation_price"], 2864.5)
            # min(2860.0, 2857.23) floored to 0.05 tick = 2857.20
            self.assertEqual(res["invalidation_level"], 2857.2)

    def test_unconfirmed_breakouts_never_auto_trigger(self):
        """Historical daily match stays waiting/expired/no-data until a completed bar closes > breakout_level."""
        daily_closes = _sample_breakout_daily_closes(
            end_date=datetime.date(2026, 10, 16), prior_high=2820.0, last_close=2848.0
        )
        # Bar 10:15 wicks to 2855.00 (> 2848) but closes at 2848.00 (== level, NOT above);
        # Bar 10:30 (closing at 10:35) closes 2854.00, which is still FORMING at --now 10:33!
        specs = _standard_session_bars_5m({
            (10, 15): (2844.0, 2855.0, 2843.0, 2848.0, 12000),
            (10, 30): (2848.0, 2856.0, 2847.5, 2854.0, 15000),
        })

        with tempfile.TemporaryDirectory() as tmp:
            fix_dir = os.path.join(tmp, "fixtures")
            os.makedirs(fix_dir)
            daily_path = os.path.join(fix_dir, "RELIANCE-daily.json")
            intraday_path = os.path.join(fix_dir, "RELIANCE-5m.json")
            with open(daily_path, "w", encoding="utf-8") as fh:
                json.dump(_make_daily_chart("RELIANCE", daily_closes), fh)
            with open(intraday_path, "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), specs), fh)

            # 1. Pre-open on Monday morning: historical scanner match MUST NOT trigger
            buf_pre = io.StringIO()
            with contextlib.redirect_stdout(buf_pre):
                rc_pre = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", fix_dir, "--now", "2026-10-19 08:45", "--json",
                ])
            self.assertEqual(rc_pre, 1)
            res_pre = json.loads(buf_pre.getvalue())
            self.assertEqual(res_pre["status"], "waiting")
            self.assertEqual(res_pre["phase"], "pre-open")
            self.assertTrue(res_pre["scanner_breakout_matched"])
            self.assertEqual(res_pre["breakout_level"], 2848.0)
            self.assertIsNone(res_pre["confirmation_price"])
            self.assertFalse(res_pre["is_live_signal"])

            # 2. Mid-session at 10:33: wick-only close == level (10:15) & forming bar (10:30->10:35) MUST NOT trigger
            buf_mid = io.StringIO()
            with contextlib.redirect_stdout(buf_mid):
                rc_mid = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", fix_dir, "--now", "2026-10-19 10:33", "--json",
                ])
            self.assertEqual(rc_mid, 1)
            res_mid = json.loads(buf_mid.getvalue())
            self.assertEqual(res_mid["status"], "waiting")
            self.assertIsNone(res_mid["confirmation_price"])
            self.assertIsNone(res_mid["timestamp"])
            self.assertFalse(res_mid["is_live_signal"])

            # 3. Daily-only offline file (no intraday confirmation bars) -> explicit no-data
            buf_daily_only = io.StringIO()
            with contextlib.redirect_stdout(buf_daily_only):
                rc_do = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", daily_path, "--now", "2026-10-19 10:35", "--json",
                ])
            self.assertEqual(rc_do, 1)
            res_do = json.loads(buf_daily_only.getvalue())
            self.assertEqual(res_do["status"], "no-data")
            self.assertIsNone(res_do["confirmation_price"])
            self.assertEqual(res_do["signal_freshness"], "unavailable")
            self.assertFalse(res_do["is_live_signal"])

            # 4. Thin volume rejected by --min-volume-x even above level
            buf_vol = io.StringIO()
            with contextlib.redirect_stdout(buf_vol):
                rc_vol = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", fix_dir, "--now", "2026-10-19 10:35",
                    "--min-volume-x", "2.0", "--json",
                ])
            self.assertEqual(rc_vol, 1)
            self.assertEqual(json.loads(buf_vol.getvalue())["status"], "waiting")

            # 5. Session past cutoff without any bar closing above 2848 -> expired
            specs_below = _standard_session_bars_5m()
            intraday_below = os.path.join(tmp, "RELIANCE-5m-below.json")
            with open(intraday_below, "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), specs_below), fh)
            buf_exp = io.StringIO()
            with contextlib.redirect_stdout(buf_exp):
                rc_exp = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", intraday_below, "--daily", daily_path,
                    "--now", "2026-10-19 15:10", "--json",
                ])
            self.assertEqual(rc_exp, 1)
            res_exp = json.loads(buf_exp.getvalue())
            self.assertEqual(res_exp["status"], "expired")
            self.assertIsNone(res_exp["confirmation_price"])
            self.assertFalse(res_exp["is_live_signal"])

    def test_signal_classification_and_stale_checks(self):
        """Yahoo=DELAYED, stale daily/intraday=stale, offline=FIXTURE, simulated=SIMULATED, live mock=CURRENT."""
        daily_closes = _sample_breakout_daily_closes(
            end_date=datetime.date(2026, 10, 16), prior_high=2820.0, last_close=2848.0
        )
        # Bar 10:25-10:30 confirms above 2848.0; bar 09:35-09:40 used in stale-late-trigger test
        specs_1030 = _standard_session_bars_5m({(10, 25): (2847.0, 2856.0, 2846.0, 2854.0, 20000)})
        specs_early = _standard_session_bars_5m({(9, 35): (2847.0, 2856.0, 2846.0, 2854.0, 20000)})

        def to_bars(specs):
            return [
                {"t": _ist_ts(2026, 10, 19, hh, mm), "o": o, "h": h, "l": lo, "c": c, "v": v}
                for hh, mm, o, h, lo, c, v in specs
            ]

        daily_bars = [
            {"t": _ist_ts(d.year, d.month, d.day, 9, 15), "o": c, "h": c, "l": c, "c": c, "v": 1000000}
            for d, c in daily_closes
        ]
        now_1035_ts = _ist_ts(2026, 10, 19, 10, 35)
        now_1035_utc = entry.parse_now("2026-10-19 10:35", market="IN")

        # 1. Yahoo delayed during open session -> DELAYED, is_live_signal=False, signal_freshness='delayed'
        daily_json_str = json.dumps(_make_daily_chart("RELIANCE", daily_closes))
        intraday_json_str = json.dumps(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), specs_1030, rmt=now_1035_ts))

        def fake_yahoo(url_path):
            if "interval=1d" in url_path:
                return daily_json_str
            return intraday_json_str

        with mock.patch.object(entry, "yahoo_get", side_effect=fake_yahoo):
            ch = entry.get_chart("RELIANCE", "5m", market="IN", cfg={"market": "IN", "data_provider": "yahoo"},
                                 now=now_1035_utc, setup="breakout_20d")
            res_y = entry.evaluate(ch, now_1035_utc, setup="breakout_20d", market="IN")
        self.assertEqual(res_y["provider"], "yahoo")
        self.assertEqual(res_y["data_tier"], "DELAYED")
        self.assertTrue(res_y["is_market_open"])
        self.assertFalse(res_y["is_live_data"])
        self.assertFalse(res_y["is_live_signal"])
        self.assertEqual(res_y["signal_freshness"], "delayed")
        self.assertEqual(res_y["status"], "triggered")

        # 2. Yahoo outside market hours (after close) -> HISTORICAL, is_live_signal=False, freshness='historical'
        now_1630_utc = entry.parse_now("2026-10-19 16:30", market="IN")
        with mock.patch.object(entry, "yahoo_get", side_effect=fake_yahoo):
            ch_ah = entry.get_chart("RELIANCE", "5m", market="IN", cfg={"market": "IN", "data_provider": "yahoo"},
                                    now=now_1630_utc, setup="breakout_20d")
            res_ah = entry.evaluate(ch_ah, now_1630_utc, setup="breakout_20d", market="IN")
        self.assertFalse(res_ah["is_market_open"])
        self.assertFalse(res_ah["is_live_signal"])
        self.assertEqual(res_ah["data_tier"], "HISTORICAL")
        self.assertEqual(res_ah["signal_freshness"], "historical")

        # 3. Genuine live CURRENT mock provider during open market (triggered 5m ago) -> CURRENT + is_live_signal=True
        live_prov = _LiveBreakoutMockProvider(to_bars(specs_1030), daily_bars, intraday_dt=now_1035_ts)
        with mock.patch.object(data_in, "get_provider", return_value=live_prov):
            ch_cur = entry.get_chart("RELIANCE", "5m", market="IN", cfg={"market": "IN"}, now=now_1035_utc, setup="breakout_20d")
            res_cur = entry.evaluate(ch_cur, now_1035_utc, setup="breakout_20d", market="IN")
        self.assertEqual(res_cur["data_tier"], "CURRENT")
        self.assertTrue(res_cur["is_market_open"])
        self.assertTrue(res_cur["is_live_data"])
        self.assertTrue(res_cur["is_live_signal"])
        self.assertEqual(res_cur["signal_freshness"], "current")

        # 4. Stale late trigger (> 30 mins after 09:40 confirmation bar close at 10:35) -> freshness='stale', NOT live
        stale_prov = _LiveBreakoutMockProvider(to_bars(specs_early), daily_bars, intraday_dt=now_1035_ts)
        with mock.patch.object(data_in, "get_provider", return_value=stale_prov):
            ch_old = entry.get_chart("RELIANCE", "5m", market="IN", cfg={"market": "IN"}, now=now_1035_utc, setup="breakout_20d")
            res_old = entry.evaluate(ch_old, now_1035_utc, setup="breakout_20d", market="IN")
        self.assertEqual(res_old["status"], "triggered")
        self.assertEqual(res_old["signal_freshness"], "stale")
        self.assertFalse(res_old["is_live_signal"])

        # 4b. Provider-reported STALE intraday feed -> DELAYED, data_status='stale', freshness='stale', NOT live
        stale_feed_prov = _LiveBreakoutMockProvider(
            to_bars(specs_1030), daily_bars, intraday_dt=now_1035_ts,
            intraday_status="STALE", intraday_tier="DELAYED"
        )
        with mock.patch.object(data_in, "get_provider", return_value=stale_feed_prov):
            ch_sf = entry.get_chart("RELIANCE", "5m", market="IN", cfg={"market": "IN"}, now=now_1035_utc, setup="breakout_20d")
            res_sf = entry.evaluate(ch_sf, now_1035_utc, setup="breakout_20d", market="IN")
        self.assertEqual(res_sf["data_tier"], "DELAYED")
        self.assertEqual(res_sf["data_status"], "stale")
        self.assertEqual(res_sf["signal_freshness"], "stale")
        self.assertFalse(res_sf["is_live_data"])
        self.assertFalse(res_sf["is_live_signal"])

        # 5. Simulated provider -> SIMULATED, is_live_signal=False, signal_freshness='simulated'
        sim_prov = _LiveBreakoutMockProvider(
            to_bars(specs_1030), daily_bars, intraday_dt=now_1035_ts,
            intraday_tier="SIMULATED", intraday_status="SIMULATED", is_simulated=True
        )
        with mock.patch.object(data_in, "get_provider", return_value=sim_prov):
            ch_sim = entry.get_chart("RELIANCE", "5m", market="IN", cfg={"market": "IN"}, now=now_1035_utc, setup="breakout_20d")
            res_sim = entry.evaluate(ch_sim, now_1035_utc, setup="breakout_20d", market="IN")
        self.assertEqual(res_sim["data_tier"], "SIMULATED")
        self.assertTrue(res_sim["is_simulated"])
        self.assertFalse(res_sim["is_live_signal"])
        self.assertEqual(res_sim["signal_freshness"], "simulated")

        # 6. Stale daily history (INFY-daily.json ending 2026-10-12 evaluated on 2026-10-19 -> 4 missed sessions)
        infy_daily_path = os.path.join(ROOT, "tests", "fixtures", "bharat", "INFY-daily.json")
        with tempfile.TemporaryDirectory() as tmp:
            infy_5m_path = os.path.join(tmp, "INFY-5m.json")
            with open(infy_5m_path, "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("INFY", datetime.date(2026, 10, 19), specs_1030), fh)
            buf_st = io.StringIO()
            with contextlib.redirect_stdout(buf_st):
                rc_st = entry.main([
                    "check", "INFY", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", infy_5m_path, "--daily", infy_daily_path,
                    "--now", "2026-10-19 10:35", "--json",
                ])
            self.assertEqual(rc_st, 1)
            res_st = json.loads(buf_st.getvalue())
            self.assertEqual(res_st["status"], "no-data")
            self.assertEqual(res_st["data_status"], "stale")
            self.assertEqual(res_st["signal_freshness"], "stale")
            self.assertFalse(res_st["is_live_signal"])

    def test_insufficient_and_missing_data_never_invent_signal(self):
        """Missing daily file, < 30 daily bars, or provider MissingDataError return explicit no-data."""
        specs = _standard_session_bars_5m({(10, 25): (2847.0, 2856.0, 2846.0, 2854.0, 20000)})
        now_1035_utc = entry.parse_now("2026-10-19 10:35", market="IN")

        # 1. RELIANCE-5m.json in tests/fixtures/bharat has no RELIANCE-daily.json -> explicit no-data
        bharat_dir = os.path.join(ROOT, "tests", "fixtures", "bharat")
        buf_miss = io.StringIO()
        with contextlib.redirect_stdout(buf_miss):
            rc_miss = entry.main([
                "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                "--offline", bharat_dir, "--now", "2026-10-19 10:35", "--json",
            ])
        self.assertEqual(rc_miss, 1)
        res_miss = json.loads(buf_miss.getvalue())
        self.assertEqual(res_miss["status"], "no-data")
        self.assertIsNone(res_miss["breakout_level"])
        self.assertIsNone(res_miss["confirmation_price"])
        self.assertEqual(res_miss["signal_freshness"], "unavailable")
        self.assertFalse(res_miss["is_live_signal"])

        # 2. Only 25 daily bars (< MIN_DAILY_BARS = 30) -> explicit insufficient-data result
        short_daily = _sample_breakout_daily_closes(end_date=datetime.date(2026, 10, 16), count=25)
        with tempfile.TemporaryDirectory() as tmp:
            dpath = os.path.join(tmp, "RELIANCE-daily.json")
            ipath = os.path.join(tmp, "RELIANCE-5m.json")
            with open(dpath, "w", encoding="utf-8") as fh:
                json.dump(_make_daily_chart("RELIANCE", short_daily), fh)
            with open(ipath, "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), specs), fh)
            buf_short = io.StringIO()
            with contextlib.redirect_stdout(buf_short):
                rc_short = entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", tmp, "--now", "2026-10-19 10:35", "--json",
                ])
            self.assertEqual(rc_short, 1)
            res_short = json.loads(buf_short.getvalue())
            self.assertEqual(res_short["status"], "no-data")
            self.assertIn("need 30", res_short["reason"])
            self.assertIsNone(res_short["breakout_level"])
            self.assertFalse(res_short["is_live_signal"])

        # 3. Provider raising MissingDataError on daily history -> explicit no-data
        intraday_bars = [{"t": _ist_ts(2026, 10, 19, hh, mm), "o": o, "h": h, "l": lo, "c": c, "v": v}
                         for hh, mm, o, h, lo, c, v in specs]
        fail_daily_prov = _LiveBreakoutMockProvider(
            intraday_bars, [], intraday_dt=_ist_ts(2026, 10, 19, 10, 35), fail_daily=True
        )
        with mock.patch.object(data_in, "get_provider", return_value=fail_daily_prov):
            ch_fd = entry.get_chart("RELIANCE", "5m", market="IN", cfg={"market": "IN"}, now=now_1035_utc, setup="breakout_20d")
            res_fd = entry.evaluate(ch_fd, now_1035_utc, setup="breakout_20d", market="IN")
        self.assertEqual(res_fd["status"], "no-data")
        self.assertEqual(res_fd["signal_freshness"], "unavailable")
        self.assertFalse(res_fd["is_live_signal"])

    def test_lookahead_future_daily_and_intraday_candles_cannot_change_result(self):
        """Adding future daily bars, same-day EOD bar, or future intraday bars leaves --now evaluation invariant."""
        base_daily = _sample_breakout_daily_closes(
            end_date=datetime.date(2026, 10, 16), prior_high=2820.0, last_close=2848.0
        )
        base_specs = _standard_session_bars_5m()

        # Future poisoned daily bars: same-day 2026-10-19 at 5000 AND future 2026-10-20/21 at 9999
        poisoned_daily = list(base_daily) + [
            (datetime.date(2026, 10, 19), 5000.0),
            (datetime.date(2026, 10, 20), 9999.0),
            (datetime.date(2026, 10, 21), 9999.0),
        ]
        # Future poisoned intraday breakout starting at 10:35 (bar closes 10:40, after --now 10:35)
        poisoned_specs = list(base_specs) + [
            (10, 45, 2900.0, 2999.0, 2899.0, 2995.0, 9999999),
            (11, 0, 2995.0, 3050.0, 2990.0, 3040.0, 9999999),
        ]
        # Override 10:30 bar close (which completes at 10:35 -> AT cutoff) vs 10:35 (closes at 10:40 -> AFTER cutoff)
        poisoned_specs = [
            (10, 35, 2900.0, 2999.0, 2895.0, 2990.0, 9999999) if (hh, mm) == (10, 35)
            else (10, 40, 2990.0, 3050.0, 2985.0, 3040.0, 9999999) if (hh, mm) == (10, 40)
            else (hh, mm, o, h, lo, c, v)
            for hh, mm, o, h, lo, c, v in poisoned_specs
        ]
        future_rmt = _ist_ts(2026, 10, 21, 15, 30)

        with tempfile.TemporaryDirectory() as tmp:
            clean_dir = os.path.join(tmp, "clean")
            poison_dir = os.path.join(tmp, "poison")
            os.makedirs(clean_dir)
            os.makedirs(poison_dir)

            # Truncate clean intraday strictly at <= 10:35 (last bar start 10:30)
            clean_specs = [s for s in base_specs if (s[0], s[1]) <= (10, 30)]
            with open(os.path.join(clean_dir, "RELIANCE-daily.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_daily_chart("RELIANCE", base_daily), fh)
            with open(os.path.join(clean_dir, "RELIANCE-5m.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), clean_specs), fh)

            with open(os.path.join(poison_dir, "RELIANCE-daily.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_daily_chart("RELIANCE", poisoned_daily, rmt=future_rmt), fh)
            with open(os.path.join(poison_dir, "RELIANCE-5m.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("RELIANCE", datetime.date(2026, 10, 19), poisoned_specs, rmt=future_rmt), fh)

            out_clean = io.StringIO()
            out_poison = io.StringIO()
            with contextlib.redirect_stdout(out_clean):
                entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", clean_dir, "--now", "2026-10-19 10:35", "--json",
                ])
            with contextlib.redirect_stdout(out_poison):
                entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", poison_dir, "--now", "2026-10-19 10:35", "--json",
                ])

            r_clean = json.loads(out_clean.getvalue())
            r_poison = json.loads(out_poison.getvalue())
            self.assertEqual(r_clean, r_poison)
            self.assertEqual(r_clean["status"], "waiting")
            self.assertEqual(r_clean["breakout_level"], 2848.0)
            self.assertEqual(r_clean["last_data_timestamp"], "2026-10-19T10:35:00+05:30")
            self.assertLessEqual(r_clean["last_data_timestamp"], r_clean["simulated_at"])

            # Post-close replay at 16:00 must STILL exclude same-day 2026-10-19 EOD close (5000.0) from entering baseline
            out_eod = io.StringIO()
            with contextlib.redirect_stdout(out_eod):
                entry.main([
                    "check", "RELIANCE", "--market", "IN", "--setup", "breakout_20d",
                    "--offline", poison_dir, "--now", "2026-10-19 16:00", "--json",
                ])
            r_eod = json.loads(out_eod.getvalue())
            self.assertEqual(r_eod["breakout_level"], 2848.0)
            self.assertEqual(r_eod["status"], "triggered")
            self.assertEqual(r_eod["confirmation_price"], 2990.0)

    def test_us_market_breakout_20d_and_cli_validation(self):
        """US --market US --setup breakout_20d works cleanly and --level with breakout_20d is rejected."""
        us_daily = _sample_breakout_daily_closes(
            end_date=datetime.date(2026, 10, 16), prior_high=360.0, last_close=365.0, market="US"
        )
        us_specs = [(9, 30 + 5 * i, 364.0, 366.0, 363.5, 364.5, 5000) for i in range(6)]
        us_specs[5] = (9, 55, 364.8, 367.5, 364.25, 367.1, 15000)

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "TSLA-daily.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_daily_chart("TSLA", us_daily, market="US"), fh)
            with open(os.path.join(tmp, "TSLA-5m.json"), "w", encoding="utf-8") as fh:
                json.dump(_make_intraday_chart("TSLA", datetime.date(2026, 10, 19), us_specs, market="US"), fh)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = entry.main([
                    "check", "TSLA", "--market", "US", "--setup", "breakout_20d",
                    "--offline", tmp, "--now", "2026-10-19 10:15", "--json",
                ])
            self.assertEqual(rc, 0)
            res = json.loads(buf.getvalue())
            self.assertEqual(res["currency"], "USD")
            self.assertEqual(res["breakout_level"], 365.0)
            self.assertEqual(res["confirmation_price"], 367.1)
            self.assertEqual(res["invalidation_level"], 364.25)

            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc_bad = entry.main([
                    "check", "TSLA", "--setup", "breakout_20d", "--level", "365.0", "--offline", tmp
                ])
            self.assertEqual(rc_bad, 1)
            self.assertIn("--level", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
