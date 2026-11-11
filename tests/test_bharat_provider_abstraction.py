"""Comprehensive tests for provider-agnostic Indian market-data abstraction."""

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load, FIX

data_in = load("trader-alerts", "data_in")
symbols_in = load("trader-alerts", "symbols_in")
calendar_in = load("trader-alerts", "calendar_in")
alerts_mod = load("trader-alerts", "alerts")
scan_mod = load("trader-scanner", "scan")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
tm_mod = load("trader-timemachine", "timemachine")
news_mod = load("trader-news", "news")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")


def _make_daily_bars(start_date, count, base_price=1400.0, breakout=False):
    bars = []
    cur = start_date
    produced = 0
    while produced < count:
        if cur.weekday() < 5 and calendar_in.is_trading_day_in(cur, exchange="NSE")[0]:
            dt_ist = datetime.datetime(cur.year, cur.month, cur.day, 15, 30, tzinfo=calendar_in.IST)
            ts = int(dt_ist.timestamp())
            close = base_price + (produced * 1.2)
            if breakout and produced == count - 1:
                close = base_price + (count * 3.0) + 40.0
            bars.append({
                "date": cur.isoformat(),
                "ist_time": dt_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
                "o": round(close - 5.0, 2),
                "h": round(close + 8.0, 2),
                "l": round(close - 9.0, 2),
                "c": round(close, 2),
                "v": 2500000,
                "ts": ts,
            })
            produced += 1
        cur += datetime.timedelta(days=1)
    return bars


def _make_intraday_sessions(end_date, num_sessions=25, breakout_on_last=True):
    """Build valid 75-bar NSE sessions (09:15..15:25 IST) across num_sessions trading days."""
    trading_days = []
    cur = end_date - datetime.timedelta(days=num_sessions * 2 + 10)
    while cur <= end_date:
        if cur.weekday() < 5 and calendar_in.is_trading_day_in(cur, exchange="NSE")[0]:
            trading_days.append(cur)
        cur += datetime.timedelta(days=1)
    trading_days = trading_days[-num_sessions:]
    bars = []
    for d_idx, d in enumerate(trading_days):
        base = 2800.0 + (d_idx % 5) * 4.0
        for slot in range(75):
            mins = 9 * 60 + 15 + slot * 5
            hh, mm = divmod(mins, 60)
            dt_ist = datetime.datetime(d.year, d.month, d.day, hh, mm, tzinfo=calendar_in.IST)
            ts = int(dt_ist.timestamp())
            if slot < 3:
                o, h, l, c = base, base + 8.0, base - 4.0, base + 2.0
                v = 100000
            elif slot == 3 and (breakout_on_last or d_idx < len(trading_days) - 1):
                o, h, l, c = base + 3.0, base + 24.0, base + 1.0, base + 20.0
                v = 180000
            else:
                drift = (slot % 7) * 1.5
                o, h, l, c = base + 10.0 + drift, base + 22.0 + drift, base + 5.0, base + 14.0 + drift
                v = 90000
            bars.append({
                "date": d.isoformat(),
                "slot": slot,
                "start": dt_ist.strftime("%H:%M"),
                "ist_time": dt_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
                "o": round(o, 2),
                "h": round(h, 2),
                "l": round(l, 2),
                "c": round(c, 2),
                "v": v,
                "ts": ts,
            })
    return bars, trading_days


class MockSecondProvider(data_in.MarketDataProvider):
    """Synthetic second provider used only in tests to verify clean swap-by-config."""

    provider_name = "mock_alt"
    requires_auth = False
    required_credentials = ()
    CAPABILITIES = {
        data_in.CAPABILITY_HISTORICAL_CANDLES: True,
        data_in.CAPABILITY_INTRADAY_CANDLES: True,
        data_in.CAPABILITY_LIVE_QUOTES: True,
        data_in.CAPABILITY_STREAMING_DATA: False,
        data_in.CAPABILITY_OPTION_CHAINS: False,
        data_in.CAPABILITY_OPEN_INTEREST: False,
        data_in.CAPABILITY_INSTRUMENT_MASTERS: True,
        data_in.CAPABILITY_MARKET_STATUS: True,
    }

    SHARED_FAIL_SYMBOLS = set()

    def __init__(self, config=None):
        self.config = config or {}
        self.calls = []
        end_d = datetime.date(2026, 10, 19)
        self._daily_reliance = _make_daily_bars(datetime.date(2026, 8, 1), 40, base_price=2800.0, breakout=True)
        self._daily_tcs = _make_daily_bars(datetime.date(2026, 8, 1), 40, base_price=3400.0, breakout=False)
        self._intraday, _ = _make_intraday_sessions(end_d, num_sessions=25, breakout_on_last=True)

    def get_quote(self, symbol, exchange="NSE"):
        self.calls.append(("get_quote", symbol, exchange))
        canon = symbols_in.normalize_symbol(symbol)
        if canon in self.SHARED_FAIL_SYMBOLS:
            raise data_in.ProviderConnectionError("Simulated error for %s" % canon)
        px = 2912.50 if canon == "RELIANCE" else 3485.00
        prev = 2850.00 if canon == "RELIANCE" else 3450.00
        ts = int(datetime.datetime(2026, 10, 19, 10, 30, tzinfo=calendar_in.IST).timestamp())
        return data_in._standardize_payload(
            {
                "symbol": canon,
                "exchange": exchange,
                "currency": "INR",
                "price": px,
                "ltp": px,
                "close": px,
                "prev_close": prev,
                "open": prev + 5.0,
                "high": px + 6.0,
                "low": prev - 4.0,
                "volume": 3200000,
                "change": round(px - prev, 2),
                "change_pct": round((px / prev - 1.0) * 100.0, 3),
                "source": self.provider_name,
                "tier": data_in.DataTier.REAL_TIME,
            },
            provider_name=self.provider_name,
            symbol=canon,
            exchange=exchange,
            data_status=data_in.DataStatus.CURRENT,
            tier=data_in.DataTier.REAL_TIME,
            timestamp=ts,
            is_live=True,
            is_simulated=False,
        )

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        self.calls.append(("get_daily_bars", symbol, exchange))
        canon = symbols_in.normalize_symbol(symbol)
        if canon in self.SHARED_FAIL_SYMBOLS:
            raise data_in.ProviderConnectionError("Simulated failure for %s" % canon)
        bars = self._daily_reliance if canon == "RELIANCE" else self._daily_tcs
        return data_in._standardize_payload(
            {
                "symbol": canon,
                "exchange": exchange,
                "interval": "1d",
                "bars": bars,
                "source": self.provider_name,
                "tier": data_in.DataTier.HISTORICAL,
                "meta": {"symbol": canon, "regularMarketTime": bars[-1]["ts"]},
            },
            provider_name=self.provider_name,
            symbol=canon,
            exchange=exchange,
            data_status=data_in.DataStatus.HISTORICAL,
            tier=data_in.DataTier.HISTORICAL,
            timestamp=bars[-1]["ts"],
            is_live=False,
            is_simulated=False,
            now=now,
        )

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        self.calls.append(("get_intraday_bars", symbol, interval, exchange))
        canon = symbols_in.normalize_symbol(symbol)
        if canon in self.SHARED_FAIL_SYMBOLS:
            raise data_in.ProviderConnectionError("Simulated intraday error for %s" % canon)
        return data_in._standardize_payload(
            {
                "symbol": canon,
                "exchange": exchange,
                "interval": interval,
                "bars": self._intraday,
                "prev_close": 2800.0,
                "source": self.provider_name,
                "tier": data_in.DataTier.REAL_TIME,
                "dropped": {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0},
                "meta": {"symbol": canon, "dataGranularity": interval, "regularMarketTime": self._intraday[-1]["ts"]},
            },
            provider_name=self.provider_name,
            symbol=canon,
            exchange=exchange,
            data_status=data_in.DataStatus.CURRENT,
            tier=data_in.DataTier.REAL_TIME,
            timestamp=self._intraday[-1]["ts"],
            is_live=True,
            is_simulated=False,
            now=now,
        )

    def get_instruments(self, exchange="NSE", symbol=None):
        self.calls.append(("get_instruments", exchange, symbol))
        records = [
            {
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "instrument_id": "NSE:RELIANCE",
                "name": "Reliance Industries Ltd",
                "asset_type": "EQUITY",
                "lot_size": 1,
                "tick_size": 0.05,
                "isin": "INE002A01018",
            },
            {
                "symbol": "TCS",
                "exchange": "NSE",
                "instrument_id": "NSE:TCS",
                "name": "Tata Consultancy Services Ltd",
                "asset_type": "EQUITY",
                "lot_size": 1,
                "tick_size": 0.05,
                "isin": "INE467B01029",
            },
        ]
        if symbol:
            canon = symbols_in.normalize_symbol(symbol)
            records = [r for r in records if r["symbol"] == canon]
        return data_in._standardize_payload(
            {"symbol": symbol or exchange, "exchange": exchange, "instruments": records, "count": len(records)},
            provider_name=self.provider_name,
            symbol=symbol or exchange,
            exchange=exchange,
            data_status=data_in.DataStatus.CURRENT,
            tier=data_in.DataTier.REAL_TIME,
            timestamp=int(datetime.datetime(2026, 10, 19, 9, 0, tzinfo=calendar_in.IST).timestamp()),
            is_live=False,
            is_simulated=False,
        )


class MockAuthProvider(MockSecondProvider):
    """Test provider requiring authentication credentials via config/env validation hook."""

    provider_name = "mock_auth"
    requires_auth = True
    required_credentials = ("api_key", "access_token")


class TestProviderAbstraction(unittest.TestCase):
    def setUp(self):
        MockSecondProvider.SHARED_FAIL_SYMBOLS = set()
        self._old_env = {
            k: os.environ.get(k)
            for k in ("INDIA_MARKET_DATA_PROVIDER", "BHARAT_DATA_PROVIDER", "MOCK_AUTH_API_KEY", "MOCK_AUTH_ACCESS_TOKEN")
        }
        os.environ.pop("INDIA_MARKET_DATA_PROVIDER", None)
        os.environ.pop("BHARAT_DATA_PROVIDER", None)
        os.environ.pop("MOCK_AUTH_API_KEY", None)
        os.environ.pop("MOCK_AUTH_ACCESS_TOKEN", None)
        data_in.register_provider("mock_alt", MockSecondProvider)
        data_in.register_provider("mock_auth", MockAuthProvider)

    def tearDown(self):
        MockSecondProvider.SHARED_FAIL_SYMBOLS = set()
        data_in.unregister_provider("mock_alt")
        data_in.unregister_provider("mock_auth")
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_standard_interface_methods_and_metadata(self):
        off = data_in.OfflineMarketDataProvider(BHARAT_FIXTURES)

        q = off.get_quote("RELIANCE.NS", exchange="NSE")
        self.assertEqual(q["provider"], "offline")
        self.assertEqual(q["symbol"], "RELIANCE")
        self.assertEqual(q["exchange"], "NSE")
        self.assertEqual(q["instrument_id"], "NSE:RELIANCE")
        self.assertEqual(q["data_status"], data_in.DataStatus.FIXTURE)
        self.assertFalse(q["is_live"])
        self.assertFalse(q["is_simulated"])
        self.assertIn("IST", q["ist_time"])
        self.assertIsInstance(q["freshness"], dict)

        ltp = off.get_ltp("RELIANCE", exchange="NSE")
        self.assertEqual(ltp["ltp"], q["price"])
        self.assertEqual(ltp["provider"], "offline")
        self.assertEqual(ltp["data_status"], data_in.DataStatus.FIXTURE)

        ohlc = off.get_ohlc("RELIANCE", exchange="NSE")
        self.assertEqual(ohlc["close"], q["price"])
        self.assertIn("open", ohlc)
        self.assertIn("high", ohlc)
        self.assertIn("low", ohlc)

        hist = off.get_historical_data("INFY", start="2026-09-01", end="2026-10-19", exchange="NSE", now="2026-10-19 16:00:00")
        self.assertGreater(len(hist["bars"]), 0)
        for b in hist["bars"]:
            self.assertGreaterEqual(b["date"], "2026-09-01")
            self.assertLessEqual(b["date"], "2026-10-19")
            self.assertIn("ist_time", b)
            self.assertIn("ts", b)

        intr = off.get_intraday_data("RELIANCE", interval="5m", exchange="NSE", now="2026-10-19 16:00:00")
        self.assertEqual(len(intr["bars"]), 75)
        self.assertEqual(intr["data_status"], data_in.DataStatus.FIXTURE)

        inst = off.get_instruments(exchange="NSE", symbol="NIFTY")
        self.assertEqual(inst["data_status"], data_in.DataStatus.FIXTURE)

        st = off.get_market_status(exchange="NSE", as_of="2026-10-18 12:00:00")
        self.assertFalse(st["is_open"])
        self.assertEqual(st["status"], "WEEKEND")
        self.assertEqual(st["provider"], "offline")

    def test_yahoo_quote_open_field_fix_and_error_integrity(self):
        with open(os.path.join(BHARAT_FIXTURES, "RELIANCE-quote.json"), "r", encoding="utf-8") as fh:
            raw = fh.read()
        doc = json.loads(raw)
        doc["chart"]["result"][0]["meta"]["regularMarketOpen"] = 2841.75
        doc["chart"]["result"][0]["meta"]["regularMarketDayLow"] = 2820.10

        y = data_in.YahooMarketDataProvider()
        q = y.normalize_quote(json.dumps(doc), "RELIANCE", exchange="NSE")
        self.assertEqual(q["open"], 2841.75)
        self.assertEqual(q["low"], 2820.10)
        self.assertEqual(q["data_status"], data_in.DataStatus.DELAYED)
        self.assertFalse(q["is_live"])
        self.assertFalse(q["is_simulated"])

        class FailingYahoo(data_in.YahooMarketDataProvider):
            def _http_get(self, url):
                raise urllib.error.URLError("offline socket disabled")

        fy = FailingYahoo(pause=0.0, backoff=0.0)
        with self.assertRaises(data_in.ProviderConnectionError):
            fy.get_quote("RELIANCE", exchange="NSE")

        quotes_map = fy.get_quotes(["RELIANCE"], exchange="NSE")
        self.assertEqual(quotes_map["RELIANCE"]["data_status"], data_in.DataStatus.UNAVAILABLE)
        self.assertFalse(quotes_map["RELIANCE"]["is_live"])
        self.assertNotIn("price", quotes_map["RELIANCE"])

    def test_provider_selection_precedence_and_auth_validation(self):
        self.assertEqual(data_in.get_provider().provider_name, "yahoo")

        os.environ["BHARAT_DATA_PROVIDER"] = "mock_alt"
        self.assertEqual(data_in.get_provider().provider_name, "mock_alt")

        # Config overrides legacy BHARAT_DATA_PROVIDER; INDIA_MARKET_DATA_PROVIDER overrides config
        os.environ["BHARAT_DATA_PROVIDER"] = "yahoo"
        self.assertEqual(data_in.get_provider(config={"india_market_data_provider": "mock_alt"}).provider_name, "mock_alt")

        os.environ["INDIA_MARKET_DATA_PROVIDER"] = "mock_alt"
        self.assertEqual(data_in.get_provider(config={"india_market_data_provider": "yahoo"}).provider_name, "mock_alt")

        with self.assertRaises(data_in.ProviderConfigError):
            data_in.get_provider("does_not_exist")

        with self.assertRaises(data_in.AuthenticationConfigError):
            data_in.get_provider("mock_auth")

        os.environ["MOCK_AUTH_API_KEY"] = "test-key"
        os.environ["MOCK_AUTH_ACCESS_TOKEN"] = "test-tok"
        prov_auth = data_in.get_provider("mock_auth")
        self.assertEqual(prov_auth.provider_name, "mock_auth")

    def test_capability_matrix_and_not_supported_handling(self):
        yahoo = data_in.YahooMarketDataProvider()
        caps = yahoo.capabilities()
        self.assertTrue(caps[data_in.CAPABILITY_HISTORICAL_CANDLES])
        self.assertTrue(caps[data_in.CAPABILITY_INTRADAY_CANDLES])
        self.assertTrue(caps[data_in.CAPABILITY_MARKET_STATUS])
        self.assertFalse(caps[data_in.CAPABILITY_LIVE_QUOTES])
        self.assertFalse(caps[data_in.CAPABILITY_STREAMING_DATA])
        self.assertFalse(caps[data_in.CAPABILITY_OPTION_CHAINS])
        self.assertFalse(caps[data_in.CAPABILITY_OPEN_INTEREST])
        self.assertFalse(caps[data_in.CAPABILITY_INSTRUMENT_MASTERS])

        with self.assertRaises(data_in.NotSupportedError) as ctx_opt:
            yahoo.get_option_chain("NIFTY", exchange="NSE")
        self.assertEqual(ctx_opt.exception.status, "NOT_SUPPORTED")
        self.assertEqual(ctx_opt.exception.capability, data_in.CAPABILITY_OPTION_CHAINS)
        self.assertEqual(ctx_opt.exception.to_dict()["status"], "NOT_SUPPORTED")

        with self.assertRaises(data_in.NotSupportedError) as ctx_inst:
            yahoo.get_instruments("NSE")
        self.assertEqual(ctx_inst.exception.status, "NOT_SUPPORTED")
        self.assertEqual(ctx_inst.exception.capability, data_in.CAPABILITY_INSTRUMENT_MASTERS)

        sim = data_in.SimulatedMarketDataProvider(base_provider=MockSecondProvider())
        chain = sim.get_option_chain("NIFTY", expiry="2026-10-27", exchange="NSE", spot_price=24500.0)
        chain_dict = chain.to_dict()
        self.assertEqual(chain_dict["tier"], data_in.DataTier.SIMULATED)
        self.assertEqual(chain_dict["data_status"], data_in.DataStatus.SIMULATED)
        self.assertTrue(chain_dict["is_simulated"])
        self.assertFalse(chain_dict["is_live"])

    def test_swap_provider_end_to_end_via_env_only(self):
        """Switching INDIA_MARKET_DATA_PROVIDER=mock_alt drives alerts, scanner, entry, exit, timemachine, news."""
        os.environ["INDIA_MARKET_DATA_PROVIDER"] = "mock_alt"

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"market": "IN", "exchange": "NSE", "symbols": ["RELIANCE", "TCS"]}, fh)

            # 1. Alerts quote + readiness
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = alerts_mod.main(["quote", "--market", "IN", "RELIANCE", "--config", cfg_path, "--json"])
            self.assertEqual(rc, 0)
            q_out = json.loads(buf.getvalue())
            self.assertEqual(q_out["provider"], "mock_alt")
            self.assertEqual(q_out["quotes"]["RELIANCE"]["price"], 2912.50)

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = alerts_mod.main([
                    "readiness", "--market", "IN", "--config", cfg_path,
                    "--instruments", os.path.join(tmp, "missing_inst.json"),
                    "--now", "2026-10-19 10:30", "--json",
                ])
            r_out = json.loads(buf.getvalue())
            self.assertEqual(r_out["provider"], "mock_alt")
            self.assertTrue(r_out["capabilities"]["live_quotes"])

            # 2. Daily + ORB scanner via INDIA_MARKET_DATA_PROVIDER=mock_alt
            wl_path = os.path.join(tmp, "watchlist.json")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                rc = scan_mod.main([
                    "daily", "--market", "IN", "--config", cfg_path,
                    "--symbols", "RELIANCE,TCS", "--now", "2026-10-19 16:00",
                    "--out", wl_path, "--json",
                ])
            self.assertEqual(rc, 0)
            wl_doc = json.loads(buf.getvalue())
            self.assertEqual(wl_doc["provider"], "mock_alt")
            self.assertEqual(wl_doc["source"], "mock_alt")
            self.assertGreaterEqual(len(wl_doc["hits"]), 1)

            orb_path = os.path.join(tmp, "orb.json")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                rc = scan_mod.main([
                    "orb", "--market", "IN", "--config", cfg_path,
                    "--symbols", "RELIANCE,TCS", "--now", "2026-10-19 10:30",
                    "--out", orb_path, "--json",
                ])
            self.assertEqual(rc, 0)
            orb_doc = json.loads(buf.getvalue())
            self.assertEqual(orb_doc["provider"], "mock_alt")
            self.assertGreaterEqual(len(orb_doc["hits"]), 1)

            # 3. Entry timer using mock_alt intraday bars
            entry_path = os.path.join(tmp, "entry.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = entry_mod.main([
                    "check", "RELIANCE", "--market", "IN", "--config", cfg_path,
                    "--now", "2026-10-19 10:30", "--write", entry_path, "--json",
                ])
            self.assertEqual(rc, 0)
            with open(entry_path, encoding="utf-8") as fh:
                entry_doc = json.load(fh)
            self.assertEqual(entry_doc["provider"], "mock_alt")
            self.assertEqual(entry_doc["source"], "mock_alt")
            self.assertEqual(entry_doc["status"], "triggered")

            # 4. Exit planner (ATR stop loads bars through mock_alt)
            plan_path = os.path.join(tmp, "plan.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = exit_mod.main([
                    "plan", "--from", entry_path, "--market", "IN",
                    "--config", cfg_path, "--stop", "atr:1.5",
                    "--now", "2026-10-19 10:30", "--write", plan_path,
                ])
            self.assertEqual(rc, 0)
            with open(plan_path, encoding="utf-8") as fh:
                plan_doc = json.load(fh)
            self.assertEqual(plan_doc["provider"], "mock_alt")
            self.assertIn("atr", plan_doc)

            # 5. Time Machine loads 25 sessions via mock_alt
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tm_mod.main([
                    "run", "--plan", plan_path, "--market", "IN",
                    "--config", cfg_path, "--now", "2026-10-19 10:30",
                    "--replays", "200", "--json",
                ])
            self.assertEqual(rc, 0)
            tm_doc = json.loads(buf.getvalue())
            self.assertEqual(tm_doc["provider"], "mock_alt")
            self.assertGreaterEqual(tm_doc["mc"]["pool_sessions"], 20)

            # 6. News collect_moves uses mock_alt for Indian tickers
            fetcher = news_mod.Fetcher(None, offline=None)
            errs = []
            moves = news_mod.collect_moves(fetcher, {"RELIANCE": None}, errs, exchange="NSE", config={"market": "IN"})
            self.assertEqual(len(moves), 1)
            self.assertEqual(moves[0]["provider"], "mock_alt")
            self.assertEqual(moves[0]["price"], 2912.50)

    def test_partial_and_total_provider_failures_never_masquerade_as_zero_hits(self):
        os.environ["INDIA_MARKET_DATA_PROVIDER"] = "mock_alt"
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"market": "IN", "exchange": "NSE"}, fh)

            # Partial failure: TCS fails, RELIANCE succeeds
            MockSecondProvider.SHARED_FAIL_SYMBOLS = {"TCS"}
            wl_partial = os.path.join(tmp, "wl_partial.json")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                rc = scan_mod.main([
                    "daily", "--market", "IN", "--config", cfg_path,
                    "--symbols", "RELIANCE,TCS", "--now", "2026-10-19 16:00",
                    "--out", wl_partial, "--json",
                ])
            self.assertEqual(rc, 0)
            partial_doc = json.loads(buf.getvalue())
            self.assertEqual(partial_doc["counts"]["fetched"], 1)
            self.assertEqual(partial_doc["failed"], 1)
            self.assertEqual(partial_doc["failed_count"], 1)
            self.assertEqual(len(partial_doc["hits"]), 1)

            # Complete provider outage: both fail -> data_status UNAVAILABLE and provider_error reported
            MockSecondProvider.SHARED_FAIL_SYMBOLS = {"RELIANCE", "TCS"}
            wl_fail = os.path.join(tmp, "wl_fail.json")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                rc = scan_mod.main([
                    "daily", "--market", "IN", "--config", cfg_path,
                    "--symbols", "RELIANCE,TCS", "--now", "2026-10-19 16:00",
                    "--out", wl_fail, "--json",
                ])
            self.assertEqual(rc, 1)
            fail_doc = json.loads(buf.getvalue())
            self.assertEqual(fail_doc["counts"]["fetched"], 0)
            self.assertEqual(fail_doc["failed"], 2)
            self.assertEqual(fail_doc["data_status"], "UNAVAILABLE")
            self.assertIn("provider_error", fail_doc)
            self.assertFalse(fail_doc["is_live"])

    def test_247_historical_analysis_and_non_live_fixture_labeling(self):
        # Weekend analysis works 24/7 cleanly via offline fixture and is NEVER marked live
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = entry_mod.main([
                "check", "RELIANCE", "--market", "IN",
                "--offline", BHARAT_FIXTURES,
                "--now", "2026-10-18 12:00",  # Sunday
                "--json",
            ])
        res_weekend = json.loads(buf.getvalue())
        self.assertTrue(res_weekend["is_historical_analysis"])
        self.assertFalse(res_weekend["is_live_signal"])
        self.assertTrue(res_weekend["is_fixture"])
        self.assertEqual(res_weekend["data_tier"], "FIXTURE")

        # Even during trading hours, offline fixture must never be marked live
        buf = io.StringIO()
        with redirect_stdout(buf):
            entry_mod.main([
                "check", "RELIANCE", "--market", "IN",
                "--offline", BHARAT_FIXTURES,
                "--now", "2026-10-19 10:30",
                "--json",
            ])
        res_market = json.loads(buf.getvalue())
        self.assertFalse(res_market["is_live_signal"])
        self.assertEqual(res_market["data_tier"], "FIXTURE")

    def test_bse_symbol_and_yahoo_http_fallbacks(self):
        with open(os.path.join(BHARAT_FIXTURES, "RELIANCE-quote.json"), "r", encoding="utf-8") as fh:
            quote_raw = fh.read()
        seen_urls = []

        class StubYahoo(data_in.YahooMarketDataProvider):
            def _http_get(self, url):
                seen_urls.append(url)
                if "NOTFOUND" in url:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
                if "query2" in url:
                    raise urllib.error.HTTPError(url, 503, "Unavailable", {}, io.BytesIO(b""))
                return 200, quote_raw

        p = StubYahoo(pause=0.0, backoff=0.0)
        q_bse = p.get_quote("500325", exchange="BSE")
        self.assertEqual(q_bse["exchange"], "BSE")
        self.assertEqual(q_bse["data_status"], data_in.DataStatus.DELAYED)
        self.assertFalse(q_bse["is_live"])
        self.assertTrue(any("500325.BO" in u for u in seen_urls))
        self.assertTrue(any("query2" in u for u in seen_urls))
        self.assertTrue(any("query1" in u for u in seen_urls))

        with self.assertRaises(data_in.SymbolNotFoundError):
            p.get_quote("NOTFOUND", exchange="NSE")

    def test_watch_loop_restricts_polling_to_market_sessions(self):
        # Sunday 2026-10-18 10:30 IST -> 05:00 UTC (naive UTC)
        sunday_utc = datetime.datetime(2026, 10, 18, 5, 0)
        clk = entry_mod.SimClock(sunday_utc)
        called = []
        emitted = []

        def fake_provider(now_utc):
            called.append(now_utc)
            raise AssertionError("Provider must not be polled when NSE is closed")

        code, res = entry_mod.watch_loop(
            fake_provider, clk.now, clk.sleep, emitted.append,
            {"market": "IN", "setup": "orb", "interval": "5m", "or_minutes": 15},
        )
        self.assertEqual(code, 1)
        self.assertIsNone(res)
        self.assertEqual(called, [])
        self.assertTrue(any("closed" in line.lower() for line in emitted))

    def test_yahoo_spark_closes_only_and_scan_universe_list_watchlist_precedence(self):
        # Regression 1: real Yahoo Spark /v7/finance/spark returns only `close` (no open/high/low/volume)
        stamps = [1791776700 + i * 86400 for i in range(40)]
        spark_doc = {
            "spark": {
                "result": [
                    {
                        "symbol": sym + ".NS",
                        "response": [{
                            "meta": {
                                "symbol": sym + ".NS",
                                "regularMarketPrice": 1350.0,
                                "regularMarketVolume": 5000000,
                            },
                            "timestamp": stamps,
                            "indicators": {"quote": [{"close": [1250.0 + i for i in range(40)]}]},
                        }],
                    }
                    for sym in ("RELIANCE", "TCS")
                ],
                "error": None,
            }
        }
        p = data_in.YahooMarketDataProvider(
            pause=0.0,
            backoff=0.0,
            http_get=lambda _url: (200, json.dumps(spark_doc)),
        )
        data, failed = p.get_batch_series(["RELIANCE", "TCS"], range_str="3mo", interval="1d", exchange="NSE")
        self.assertEqual(failed, {})
        self.assertEqual(len(data["RELIANCE"]["t"]), 40)
        self.assertEqual(len(data["RELIANCE"]["c"]), 40)
        self.assertIsNone(data["RELIANCE"]["o"])
        self.assertEqual(data["RELIANCE"]["meta"]["regularMarketVolume"], 5000000)
        self.assertEqual(data["RELIANCE"]["provider"], "yahoo")
        self.assertFalse(data["RELIANCE"]["is_live"])

        # Regression 2: config.in.example.json `watchlist` list and explicit `--universe` CLI precedence
        import types
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            uni_path = os.path.join(tmp, "universe.txt")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"watchlist": ["RELIANCE", "TCS"]}, fh)
            with open(uni_path, "w", encoding="utf-8") as fh:
                fh.write("INFY|Infosys Limited\nHDFCBANK|HDFC Bank\n")

            args_cfg = types.SimpleNamespace(symbols=None, watchlist=None, universe=None, config=cfg_path, limit=None)
            self.assertEqual(scan_mod.read_symbols(args_cfg), ["RELIANCE", "TCS"])

            args_uni = types.SimpleNamespace(symbols=None, watchlist=None, universe=uni_path, config=cfg_path, limit=None)
            self.assertEqual(scan_mod.read_symbols(args_uni), ["INFY", "HDFCBANK"])


if __name__ == "__main__":
    unittest.main()
