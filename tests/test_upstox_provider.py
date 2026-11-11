#!/usr/bin/env python3
"""Tests for optional Upstox Analytics Token provider and WebSocket V3 feed."""

import copy
import datetime
import gzip
import importlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERTS_DIR = os.path.join(ROOT, "trader-alerts")
ENTRY_DIR = os.path.join(ROOT, "trader-entry")
SCAN_DIR = os.path.join(ROOT, "trader-scanner")
TM_DIR = os.path.join(ROOT, "trader-timemachine")
BROKER_DIR = os.path.join(ROOT, "trader-broker")

for _d in (ALERTS_DIR, ENTRY_DIR, SCAN_DIR, TM_DIR, BROKER_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import calendar_in  # noqa: E402
import data_in  # noqa: E402
import upstox_feed  # noqa: E402


# ---------------------------------------------------------------- Protobuf Wire Test Encoder


def _pb_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_field_varint(num: int, val: int) -> bytes:
    return _pb_varint((num << 3) | 0) + _pb_varint(val)


def _pb_field_double(num: int, val: float) -> bytes:
    return _pb_varint((num << 3) | 1) + struct.pack("<d", float(val))


def _pb_field_bytes(num: int, data: bytes) -> bytes:
    return _pb_varint((num << 3) | 2) + _pb_varint(len(data)) + data


def _pb_field_str(num: int, text: str) -> bytes:
    return _pb_field_bytes(num, text.encode("utf-8"))


def _encode_ltpc(ltp, ltt, ltq, cp) -> bytes:
    return (
        _pb_field_double(1, ltp)
        + _pb_field_varint(2, ltt)
        + _pb_field_varint(3, ltq)
        + _pb_field_double(4, cp)
    )


def _encode_market_full_feed_packet(
    instrument_key="NSE_EQ|INE002A01018",
    ltp=1385.50,
    ltt=1760850000000,
    ltq=25,
    cp=1350.0,
    bid_p=1385.40,
    bid_q=100,
    ask_p=1385.60,
    ask_q=150,
    atp=1378.20,
    vtt=2500000,
    oi=450000.0,
    iv=18.5,
    tbq=800000.0,
    tsq=650000.0,
    delta=0.52,
    theta=-11.2,
    gamma=0.0012,
    vega=9.4,
    current_ts=1760850001000,
) -> bytes:
    ltpc_b = _encode_ltpc(ltp, ltt, ltq, cp)
    quote_b = (
        _pb_field_varint(1, bid_q)
        + _pb_field_double(2, bid_p)
        + _pb_field_varint(3, ask_q)
        + _pb_field_double(4, ask_p)
    )
    mlevel_b = _pb_field_bytes(1, quote_b)
    greeks_b = (
        _pb_field_double(1, delta)
        + _pb_field_double(2, theta)
        + _pb_field_double(3, gamma)
        + _pb_field_double(4, vega)
    )
    ohlc_entry = (
        _pb_field_str(1, "1d")
        + _pb_field_double(2, 1360.0)
        + _pb_field_double(3, 1390.0)
        + _pb_field_double(4, 1355.0)
        + _pb_field_double(5, ltp)
        + _pb_field_varint(6, vtt)
        + _pb_field_varint(7, ltt)
    )
    m_ohlc_b = _pb_field_bytes(1, ohlc_entry)
    mff_b = (
        _pb_field_bytes(1, ltpc_b)
        + _pb_field_bytes(2, mlevel_b)
        + _pb_field_bytes(3, greeks_b)
        + _pb_field_bytes(4, m_ohlc_b)
        + _pb_field_double(5, atp)
        + _pb_field_varint(6, vtt)
        + _pb_field_double(7, oi)
        + _pb_field_double(8, iv)
        + _pb_field_double(9, tbq)
        + _pb_field_double(10, tsq)
    )
    full_feed_b = _pb_field_bytes(1, mff_b)
    feed_b = _pb_field_bytes(2, full_feed_b) + _pb_field_varint(4, 1)
    map_entry_b = _pb_field_str(1, instrument_key) + _pb_field_bytes(2, feed_b)
    mkt_info_entry = _pb_field_str(1, "NSE_EQ") + _pb_field_varint(2, 0)
    mkt_info_b = _pb_field_bytes(1, mkt_info_entry)
    return (
        _pb_field_varint(1, 1)
        + _pb_field_bytes(2, map_entry_b)
        + _pb_field_varint(3, current_ts)
        + _pb_field_bytes(4, mkt_info_b)
    )


class _FakeWebSocket:
    def __init__(self, frames=None, fail_on_recv=False):
        self.frames = list(frames or [])
        self.fail_on_recv = fail_on_recv
        self.sent = []
        self.closed = False

    def send(self, data):
        if isinstance(data, (bytes, bytearray)):
            text = bytes(data).decode("utf-8")
        else:
            text = str(data)
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if self.fail_on_recv:
            raise OSError("Simulated socket drop")
        if self.frames:
            item = self.frames.pop(0)
            if item is None:
                self.closed = True
                return None
            return item
        return None

    def close(self):
        self.closed = True


# ---------------------------------------------------------------- Main Test Suite


class TestUpstoxProvider(unittest.TestCase):
    SAMPLE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.sampleAnalyticsTokenPayload.sig123456"

    def _clean_env(self):
        env = {}
        for k in list(os.environ.keys()):
            if k in (
                "INDIA_MARKET_DATA_PROVIDER",
                "BHARAT_DATA_PROVIDER",
                "UPSTOX_ANALYTICS_TOKEN",
                "INDIA_MARKET_DATA_ALLOW_YAHOO_FALLBACK",
                "INDIA_MARKET_DATA_FALLBACK_PROVIDER",
            ):
                env[k] = ""
        return env

    # 1. Default zero-credential UX preserved ----------------------------------

    def test_default_experience_works_without_upstox_or_credentials(self):
        patch_vals = {
            "INDIA_MARKET_DATA_PROVIDER": "",
            "BHARAT_DATA_PROVIDER": "",
            "UPSTOX_ANALYTICS_TOKEN": "",
        }
        with mock.patch.dict(os.environ, patch_vals):
            prov = data_in.get_provider()
            self.assertIsInstance(prov, data_in.YahooMarketDataProvider)
            self.assertEqual(prov.provider_name, "yahoo")
            self.assertFalse(prov.requires_auth)
            self.assertIn("upstox", data_in.list_providers())
            self.assertIn("yahoo", data_in.list_providers())
            self.assertIn("offline", data_in.list_providers())
            self.assertIn("simulated", data_in.list_providers())

            # Default option chain stays simulated Black-Scholes
            chain = data_in.fetch_option_chain("NIFTY", spot_price=24500.0)
            d = chain.to_dict()
            self.assertEqual(d["provider"], "simulated")
            self.assertTrue(d["is_simulated"])

            with open(os.path.join(ROOT, "trader-budget", "config.in.example.json"), encoding="utf-8") as fh:
                ex_cfg = json.load(fh)
            self.assertEqual(ex_cfg.get("india_market_data_provider"), "yahoo")

    # 2. Auth config, env resolution, and strict zero-leak guarantees ----------

    def test_missing_or_blank_token_raises_and_never_fabricates_data(self):
        for bad_tok in ("", "   ", "\t\n"):
            with mock.patch.dict(
                os.environ,
                {"INDIA_MARKET_DATA_PROVIDER": "upstox", "UPSTOX_ANALYTICS_TOKEN": bad_tok},
                clear=False,
            ):
                with self.assertRaises(data_in.AuthenticationConfigError) as ctx:
                    data_in.get_provider()
                self.assertIn("UPSTOX_ANALYTICS_TOKEN", str(ctx.exception))
                self.assertNotIn("yahoo", str(ctx.exception).lower().split("default")[0])

        with self.assertRaises(data_in.AuthenticationConfigError):
            data_in.UpstoxMarketDataProvider(analytics_token="   ")

    def test_token_resolution_modes_and_secret_redaction(self):
        # Env resolution
        with mock.patch.dict(
            os.environ,
            {"INDIA_MARKET_DATA_PROVIDER": "upstox", "UPSTOX_ANALYTICS_TOKEN": self.SAMPLE_TOKEN},
            clear=False,
        ):
            prov = data_in.get_provider()
            self.assertIsInstance(prov, data_in.UpstoxMarketDataProvider)
            self.assertNotIn(self.SAMPLE_TOKEN, repr(prov))
            self.assertNotIn(self.SAMPLE_TOKEN, str(prov))

        # Config nested provider_credentials resolution (without env var)
        cfg = {
            "india_market_data_provider": "upstox",
            "provider_credentials": {"upstox": {"analytics_token": self.SAMPLE_TOKEN}},
        }
        with mock.patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": ""}, clear=False):
            os.environ.pop("INDIA_MARKET_DATA_PROVIDER", None)
            prov2 = data_in.get_provider(config=cfg)
            self.assertIsInstance(prov2, data_in.UpstoxMarketDataProvider)
            self.assertEqual(prov2._analytics_token, self.SAMPLE_TOKEN)

        # HTTP 401 + echoed secret/Bearer in upstream message MUST be scrubbed
        def failing_http(url, headers=None, timeout=15):
            return (
                401,
                json.dumps({
                    "status": "error",
                    "errors": [{
                        "errorCode": "UDAPI100050",
                        "message": "Invalid token used to access API Authorization: Bearer %s" % self.SAMPLE_TOKEN,
                    }],
                }),
            )

        prov_err = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=failing_http,
        )
        with self.assertRaises(data_in.AuthenticationConfigError) as ctx:
            prov_err.get_quote("RELIANCE")
        msg = str(ctx.exception)
        self.assertNotIn(self.SAMPLE_TOKEN, msg)
        self.assertIn("UDAPI100050", msg)
        self.assertIn("[REDACTED]", msg)

    def test_read_only_enforcement_blocks_non_get(self):
        prov = data_in.UpstoxMarketDataProvider(analytics_token=self.SAMPLE_TOKEN)
        with self.assertRaises(data_in.ProviderConfigError):
            prov._raw_http_get("https://api.upstox.com/v2/order/place", method="POST")
        # No order placement methods exposed on UpstoxMarketDataProvider
        self.assertFalse(hasattr(prov, "place_order"))
        self.assertFalse(hasattr(prov, "modify_order"))
        self.assertFalse(hasattr(prov, "cancel_order"))

    # 3. Explicit Yahoo fallback vs strict failure -----------------------------

    def test_explicit_yahoo_fallback_never_masquerades_as_upstox(self):
        # Unconfigured: missing token fails explicitly
        with mock.patch.dict(
            os.environ,
            {
                "INDIA_MARKET_DATA_PROVIDER": "upstox",
                "UPSTOX_ANALYTICS_TOKEN": "",
                "INDIA_MARKET_DATA_ALLOW_YAHOO_FALLBACK": "",
            },
            clear=False,
        ):
            with self.assertRaises(data_in.AuthenticationConfigError):
                data_in.get_provider()

            # Explicit fallback on missing token returns real Yahoo provider (not Upstox)
            fb_prov = data_in.get_provider(config={
                "india_market_data_provider": "upstox",
                "allow_yahoo_fallback": True,
                "fallback_provider": "yahoo",
            })
            self.assertIsInstance(fb_prov, data_in.YahooMarketDataProvider)
            self.assertEqual(fb_prov.provider_name, "yahoo")
            self.assertEqual(getattr(fb_prov, "fallback_from", None), "upstox")

        # Invalid fallback provider name fails loudly
        with self.assertRaises(data_in.ProviderConfigError):
            data_in.get_provider(config={
                "india_market_data_provider": "upstox",
                "fallback_provider": "fake_source",
            })

        # Runtime 401 with allow_yahoo_fallback=True returns Yahoo DELAYED metadata
        def http_401(url, headers=None, timeout=15):
            return 401, json.dumps({"status": "error", "errors": [{"errorCode": "UDAPI100050", "message": "Invalid token"}]})

        sample_yahoo_quote = json.dumps({
            "chart": {
                "result": [{
                    "meta": {
                        "symbol": "RELIANCE.NS",
                        "exchangeName": "NSI",
                        "regularMarketPrice": 1348.5,
                        "chartPreviousClose": 1340.0,
                        "regularMarketTime": 1760850000,
                    },
                    "timestamp": [1760850000],
                    "indicators": {"quote": [{"open": [1341.0], "high": [1350.0], "low": [1339.0], "close": [1348.5], "volume": [1000]}]},
                }],
                "error": None,
            }
        })

        def fake_yahoo_factory():
            return data_in.YahooMarketDataProvider(http_get=lambda url: (200, sample_yahoo_quote))

        prov_fb = data_in.UpstoxMarketDataProvider(
            analytics_token="bad-token",
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=http_401,
            allow_yahoo_fallback=True,
            fallback_factory=fake_yahoo_factory,
        )
        q = prov_fb.get_quote("RELIANCE")
        self.assertEqual(q["provider"], "yahoo")
        self.assertEqual(q["source"], "yahoo")
        self.assertFalse(q["is_live"])
        self.assertNotEqual(q["tier"], data_in.DataTier.REAL_TIME)
        self.assertEqual(q.get("fallback_from"), "upstox")

    # 4. Capabilities & Instrument Master / Symbol Resolution ------------------

    def test_capabilities_and_instrument_master_resolution(self):
        master_records = [
            {
                "instrument_key": "NSE_EQ|INE002A01018",
                "exchange_token": "2885",
                "trading_symbol": "RELIANCE",
                "name": "RELIANCE INDUSTRIES LTD",
                "segment": "NSE_EQ",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "isin": "INE002A01018",
                "tick_size": 0.1,
                "lot_size": 1,
            },
            {
                "instrument_key": "BSE_EQ|INE002A01018",
                "exchange_token": "500325",
                "trading_symbol": "RELIANCE",
                "name": "RELIANCE INDUSTRIES LTD",
                "segment": "BSE_EQ",
                "exchange": "BSE",
                "instrument_type": "EQ",
                "isin": "INE002A01018",
                "tick_size": 0.05,
                "lot_size": 1,
            },
            {
                "instrument_key": "NSE_FO|54321",
                "exchange_token": "54321",
                "trading_symbol": "NIFTY26NOV24500CE",
                "name": "NIFTY",
                "segment": "NSE_FO",
                "exchange": "NSE",
                "instrument_type": "CE",
                "underlying_symbol": "NIFTY",
                "strike_price": 24500.0,
                "expiry": 1795689000000,
                "lot_size": 75,
                "tick_size": 0.05,
            },
        ]
        gz_payload = gzip.compress(json.dumps(master_records).encode("utf-8"))
        seen_urls = []

        def fake_http(url, headers=None, timeout=15):
            seen_urls.append((url, dict(headers or {})))
            if "NSE.json.gz" in url:
                return 200, gz_payload
            if "BSE.json.gz" in url:
                return 200, gzip.compress(json.dumps([r for r in master_records if r["exchange"] == "BSE"]).encode("utf-8"))
            raise AssertionError("Unexpected URL: %s" % url)

        prov = data_in.UpstoxMarketDataProvider(analytics_token=self.SAMPLE_TOKEN, http_get=fake_http)
        caps = prov.capabilities()
        for cap_key in (
            data_in.CAPABILITY_HISTORICAL_CANDLES,
            data_in.CAPABILITY_INTRADAY_CANDLES,
            data_in.CAPABILITY_LIVE_QUOTES,
            data_in.CAPABILITY_STREAMING_DATA,
            data_in.CAPABILITY_OPTION_CHAINS,
            data_in.CAPABILITY_OPEN_INTEREST,
            data_in.CAPABILITY_INSTRUMENT_MASTERS,
            data_in.CAPABILITY_MARKET_STATUS,
        ):
            self.assertTrue(caps[cap_key], "Expected True for capability %s" % cap_key)
        self.assertEqual(caps["not_supported"], [])

        # Index keys resolve directly without HTTP
        self.assertEqual(prov.resolve_instrument_key("NIFTY")[0], "NSE_INDEX|Nifty 50")
        self.assertEqual(prov.resolve_instrument_key("SENSEX", exchange="BSE")[0], "BSE_INDEX|SENSEX")
        self.assertEqual(prov.resolve_instrument_key("NSE_EQ|INE467B01029")[0], "NSE_EQ|INE467B01029")

        # Equity master fetch decodes gzip JSON cleanly
        inst_res = prov.get_instruments(exchange="NSE", symbol="RELIANCE")
        self.assertEqual(inst_res["count"], 1)
        self.assertEqual(inst_res["instruments"][0]["instrument_key"], "NSE_EQ|INE002A01018")
        # Public asset CDN request must not send Bearer auth header
        self.assertNotIn("Authorization", seen_urls[0][1])

        self.assertEqual(prov.resolve_instrument_key("RELIANCE.NS")[0], "NSE_EQ|INE002A01018")
        self.assertEqual(prov.resolve_instrument_key("RELIANCE.BO", exchange="BSE")[0], "BSE_EQ|INE002A01018")

        derivs = prov.get_derivatives_master(exchange="NSE", underlying="NIFTY")
        self.assertEqual(derivs["count"], 1)
        self.assertEqual(derivs["contracts"][0]["instrument_type"], "CE")

        with self.assertRaises(data_in.InvalidSymbolError):
            prov.resolve_instrument_key("DOESNOTEXIST_XYZ")
        with self.assertRaises(data_in.SymbolNotFoundError):
            prov.resolve_instrument_key("NOPEQTY")

    # 5. Live Quotes, LTP, OHLC, Batching, Mismatch & Look-Ahead ---------------

    def test_quotes_ltp_ohlc_batching_and_lookahead_guard(self):
        open_dt = datetime.datetime(2026, 10, 19, 10, 35, 0, tzinfo=calendar_in.IST)
        tick_ms = int((open_dt - datetime.timedelta(seconds=20)).timestamp() * 1000)

        calls = []

        def fake_http(url, headers=None, timeout=15):
            calls.append((url, dict(headers or {})))
            if "/v3/market-quote/quotes" in url:
                # Upstox often uses NSE_EQ:SYMBOL as dict key while instrument_token has |ISIN
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "NSE_EQ:RELIANCE": {
                            "last_price": 1352.40,
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "last_trade_time": str(tick_ms),
                            "volume": 1245000,
                            "average_price": 1345.10,
                            "oi": 0,
                            "net_change": 12.40,
                            "lower_circuit_limit": 1206.0,
                            "upper_circuit_limit": 1474.0,
                            "ohlc": {"open": 1342.0, "high": 1355.0, "low": 1338.0, "close": 1340.0},
                            "depth": {
                                "buy": [{"quantity": 500, "price": 1352.35, "orders": 4}],
                                "sell": [{"quantity": 300, "price": 1352.45, "orders": 3}],
                            },
                        }
                    },
                })
            if "/v3/market-quote/ltp" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "NSE_INDEX:Nifty 50": {
                            "last_price": 24510.25,
                            "instrument_token": "NSE_INDEX|Nifty 50",
                            "ltq": 0,
                            "volume": 0,
                            "cp": 24420.0,
                        }
                    },
                })
            if "/v3/market-quote/ohlc" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "NSE_EQ:RELIANCE": {
                            "last_price": 1352.40,
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "cp": 1340.0,
                            "live_ohlc": {
                                "open": 1342.0, "high": 1355.0, "low": 1338.0,
                                "close": 1352.40, "volume": 1245000, "ts": tick_ms,
                            },
                        }
                    },
                })
            raise AssertionError("Unexpected URL: %s" % url)

        prov = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=fake_http,
            now_fn=lambda: open_dt,
        )
        q = prov.get_quote("RELIANCE")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer %s" % self.SAMPLE_TOKEN)
        self.assertEqual(q["provider"], "upstox")
        self.assertEqual(q["tier"], data_in.DataTier.REAL_TIME)
        self.assertEqual(q["data_status"], data_in.DataStatus.CURRENT)
        self.assertTrue(q["is_live"])
        self.assertEqual(q["price"], 1352.40)
        self.assertAlmostEqual(q["prev_close"], 1340.0, places=2)
        self.assertEqual(q["bid"], 1352.35)
        self.assertEqual(q["ask"], 1352.45)

        ltp = prov.get_ltp("NIFTY")
        self.assertEqual(ltp["price"], 24510.25)
        self.assertEqual(ltp["prev_close"], 24420.0)
        self.assertEqual(ltp["quote_mode"], "ltp")

        ohlc = prov.get_ohlc("RELIANCE", interval="5m")
        self.assertEqual(ohlc["high"], 1355.0)
        self.assertEqual(ohlc["quote_mode"], "ohlc")

        # Look-ahead rejection when --now precedes quote timestamp
        early_now = open_dt - datetime.timedelta(minutes=5)
        with self.assertRaises(data_in.MissingDataError):
            prov.get_quote("RELIANCE", now=early_now)

        # Closed session quote accurately classifies as HISTORICAL, not live
        closed_dt = datetime.datetime(2026, 10, 19, 20, 0, 0, tzinfo=calendar_in.IST)
        q_closed = prov.get_quote("RELIANCE", now=closed_dt)
        self.assertEqual(q_closed["tier"], data_in.DataTier.HISTORICAL)
        self.assertFalse(q_closed["is_live"])

    def test_batch_quotes_splits_at_500_limit_and_guards_mismatch(self):
        batch_calls = []

        def fake_http(url, headers=None, timeout=15):
            batch_calls.append(url)
            if "mismatch_test" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {"NSE_EQ:TCS": {"last_price": 4000.0, "instrument_token": "NSE_EQ|INE467B01029"}},
                })
            # Extract keys from query string
            parsed = urllib.parse.urlparse(url)
            q = urllib.parse.parse_qs(parsed.query)
            keys = q["instrument_key"][0].split(",")
            self.assertLessEqual(len(keys), 500)
            data_map = {
                k: {"last_price": 100.0 + idx, "instrument_token": k, "timestamp": 1760850000000}
                for idx, k in enumerate(keys)
            }
            return 200, json.dumps({"status": "success", "data": data_map})

        import urllib.parse  # local import cleanly scoped
        prov = data_in.UpstoxMarketDataProvider(analytics_token=self.SAMPLE_TOKEN, http_get=fake_http)
        many_keys = ["NSE_EQ|INE%09d" % i for i in range(505)]
        res = prov.get_quotes(many_keys)
        self.assertEqual(len(res), 505)
        self.assertEqual(len(batch_calls), 2)

        prov_mismatch = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"INFY": "NSE_EQ|mismatch_test"}},
            http_get=fake_http,
        )
        with self.assertRaises(data_in.DataError):
            prov_mismatch.get_quote("INFY")

    # 6. Historical & Intraday Candles V3 + Auto-Chunking + Look-Ahead --------

    def test_v3_candles_auto_chunking_sorting_and_lookahead(self):
        candle_urls = []
        # 15m/5m range=60d spans >30 days so Upstox V3 MUST split into multiple chunks
        def fake_http(url, headers=None, timeout=15):
            candle_urls.append(url)
            if "/v3/historical-candle/intraday/" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "candles": [
                            # Newest-first, includes future bars at 10:40 and 10:45 after --now 10:35
                            ["2026-10-19T10:45:00+05:30", 1365.0, 1368.0, 1364.0, 1367.0, 5000, 1100],
                            ["2026-10-19T10:35:00+05:30", 1360.0, 1364.0, 1359.0, 1363.5, 4500, 1050],
                            ["2026-10-19T10:30:00+05:30", 1355.0, 1361.0, 1354.0, 1360.0, 4000, 1000],
                            ["2026-10-19T09:15:00+05:30", 1345.0, 1350.0, 1344.0, 1349.0, 8000, 950],
                        ]
                    },
                })
            if "/v3/historical-candle/" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "candles": [
                            ["2026-10-16T15:25:00+05:30", 1338.0, 1342.0, 1337.0, 1340.0, 3000, 900],
                            ["2026-09-10T09:15:00+05:30", 1300.0, 1305.0, 1298.0, 1302.0, 2000, 850],
                        ]
                    },
                })
            raise AssertionError("Unexpected URL: %s" % url)

        now_sim = datetime.datetime(2026, 10, 19, 10, 35, 0, tzinfo=calendar_in.IST)
        prov = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=fake_http,
            now_fn=lambda: now_sim,
        )
        pl = prov.get_intraday_data("RELIANCE", interval="5m", range_str="60d", now=now_sim)
        hist_urls = [u for u in candle_urls if "/v3/historical-candle/NSE_EQ" in u]
        # 60d > 29d chunk cap => multiple historical requests + 1 intraday request
        self.assertGreaterEqual(len(hist_urls), 2)
        # Verify Upstox V3 path order: .../minutes/5/{to_date}/{from_date}
        parts = hist_urls[0].split("/")
        to_d, from_d = parts[-2], parts[-1]
        self.assertLess(from_d, to_d)

        # Look-ahead stripped: 10:35+5m=10:40 > 10:35 MUST be excluded, 10:45 MUST be excluded!
        slots = [(b["date"], b["slot"]) for b in pl["bars"]]
        self.assertNotIn(("2026-10-19", "10:35"), slots)
        self.assertNotIn(("2026-10-19", "10:45"), slots)
        self.assertIn(("2026-10-19", "10:30"), slots)
        self.assertIn(("2026-10-19", "09:15"), slots)
        # Sorted strictly ascending oldest-first & OI preserved
        self.assertEqual(pl["bars"][0]["date"], "2026-09-10")
        self.assertEqual(pl["bars"][-1]["slot"], "10:30")
        self.assertEqual(pl["bars"][-1]["oi"], 1000.0)
        self.assertEqual(pl["prev_close"], 1340.0)
        self.assertTrue(pl["is_live"])
        self.assertEqual(pl["tier"], data_in.DataTier.REAL_TIME)
        self.assertEqual(pl["simulated_at"], "2026-10-19T10:35:00+05:30")

    # 7. Option Chains, Contracts, OI & Greeks -------------------------------

    def test_option_chain_contracts_and_greeks(self):
        open_dt = datetime.datetime(2026, 10, 19, 11, 0, 0, tzinfo=calendar_in.IST)

        def fake_http(url, headers=None, timeout=15):
            if "/v2/option/chain" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": [
                        {
                            "expiry": "2026-10-27",
                            "pcr": 1.15,
                            "strike_price": 24400,
                            "underlying_key": "NSE_INDEX|Nifty 50",
                            "underlying_spot_price": 24515.0,
                            "call_options": {
                                "instrument_key": "NSE_FO|10001",
                                "market_data": {"ltp": 195.0, "volume": 80000, "oi": 1500000, "prev_oi": 1400000, "bid_price": 194.5, "ask_price": 195.5, "close_price": 180.0},
                                "option_greeks": {"delta": 0.65, "gamma": 0.001, "theta": -10.5, "vega": 8.0, "iv": 13.8, "pop": 61.0},
                            },
                            "put_options": {
                                "instrument_key": "NSE_FO|10002",
                                "market_data": {"ltp": 62.0, "volume": 90000, "oi": 2200000, "prev_oi": 2000000, "bid_price": 61.8, "ask_price": 62.2},
                                "option_greeks": {"delta": -0.35, "gamma": 0.001, "theta": -9.2, "vega": 8.1, "iv": 14.1},
                            },
                        },
                        {
                            "expiry": "2026-10-27",
                            "pcr": 1.15,
                            "strike_price": 24500,
                            "underlying_key": "NSE_INDEX|Nifty 50",
                            "underlying_spot_price": 24515.0,
                            "call_options": {
                                "instrument_key": "NSE_FO|10003",
                                "market_data": {"ltp": 132.5, "volume": 150000, "oi": 3100000, "prev_oi": 2950000, "bid_price": 132.2, "ask_price": 132.8},
                                "option_greeks": {"delta": 0.52, "gamma": 0.0014, "theta": -12.0, "vega": 9.5, "iv": 13.2},
                            },
                            "put_options": {
                                "instrument_key": "NSE_FO|10004",
                                "market_data": {"ltp": 108.0, "volume": 140000, "oi": 3400000, "prev_oi": 3300000, "bid_price": 107.8, "ask_price": 108.2},
                                "option_greeks": {"delta": -0.48, "gamma": 0.0014, "theta": -11.5, "vega": 9.5, "iv": 13.6},
                            },
                        },
                    ],
                })
            if "/v2/option/contract" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": [{"expiry": "2026-10-27", "strike_price": 24500, "instrument_key": "NSE_FO|10003", "option_type": "CE"}],
                })
            if "/v3/market-quote/option-greek" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {"NSE_FO|10003": {"instrument_token": "NSE_FO|10003", "last_price": 132.5, "delta": 0.52, "iv": 0.132}},
                })
            raise AssertionError("Unexpected URL: %s" % url)

        prov = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            http_get=fake_http,
            now_fn=lambda: open_dt,
        )
        chain = prov.get_option_chain("NIFTY", expiry="2026-10-27")
        self.assertIsInstance(chain, data_in.UpstoxOptionChain)
        self.assertEqual(chain.atm_strike, 24500.0)
        ce = chain.get_contract(24500.0, "CE")
        self.assertEqual(ce["open_interest"], 3100000)
        self.assertEqual(ce["change_in_oi"], 150000)
        self.assertEqual(ce["iv"], 13.2)
        self.assertEqual(ce["greeks"]["delta"], 0.52)
        self.assertFalse(ce["is_simulated"])
        self.assertTrue(ce["is_live"])

        doc = chain.to_dict()
        self.assertEqual(doc["provider"], "upstox")
        self.assertEqual(doc["tier"], data_in.DataTier.REAL_TIME)
        self.assertTrue(doc["is_live"])
        self.assertFalse(doc["is_simulated"])
        self.assertAlmostEqual(doc["pcr"], (2200000 + 3400000) / (1500000 + 3100000), places=2)

        # Past date look-ahead guard on snapshot option chain
        with self.assertRaises(data_in.MissingDataError):
            prov.get_option_chain("NIFTY", now=open_dt - datetime.timedelta(days=2))

        contracts = prov.get_option_contracts("NIFTY")
        self.assertEqual(contracts["expiries"], ["2026-10-27"])
        greeks = prov.get_option_greeks(["NSE_FO|10003"])
        self.assertEqual(greeks["NSE_FO|10003"]["delta"], 0.52)

    # 8. Market Status ---------------------------------------------------------

    def test_market_status_live_and_historical(self):
        def fake_http(url, headers=None, timeout=15):
            return 200, json.dumps({
                "status": "success",
                "data": {
                    "exchange": "NSE",
                    "status": "NORMAL_OPEN",
                    "last_updated": 1760850000000,
                    "cas_eligible_status": False,
                },
            })

        prov = data_in.UpstoxMarketDataProvider(analytics_token=self.SAMPLE_TOKEN, http_get=fake_http)
        st = prov.get_market_status("NSE")
        self.assertEqual(st["provider"], "upstox")
        self.assertTrue(st["is_open"])
        self.assertEqual(st["status"], "OPEN")
        self.assertEqual(st["raw_status"], "NORMAL_OPEN")
        self.assertTrue(st["live_supported"])

        # Simulated past timestamp uses historical calendar without hitting live status
        st_hist = prov.get_market_status("NSE", as_of="2026-10-19 21:00:00")
        self.assertFalse(st_hist["is_open"])
        self.assertEqual(st_hist["status"], "CLOSED")

    # 9. WebSocket V3 Authorize, Protobuf Decode, Limits, Reconnect, Stale -----

    def test_websocket_protobuf_decode_limits_reconnect_and_stale_detection(self):
        pkt = _encode_market_full_feed_packet()
        decoded = upstox_feed.decode_protobuf_feed_response(pkt)
        self.assertEqual(decoded["type"], "live_feed")
        self.assertEqual(decoded["currentTs"], "1760850001000")
        mff = decoded["feeds"]["NSE_EQ|INE002A01018"]["fullFeed"]["marketFF"]
        self.assertAlmostEqual(mff["ltpc"]["ltp"], 1385.50, places=2)
        self.assertAlmostEqual(mff["atp"], 1378.20, places=2)
        self.assertEqual(mff["vtt"], 2500000)
        self.assertAlmostEqual(mff["oi"], 450000.0, places=1)
        self.assertAlmostEqual(mff["optionGreeks"]["delta"], 0.52, places=3)
        self.assertEqual(mff["marketLevel"]["bidAskQuote"][0]["bidP"], 1385.40)

        auth_calls = []
        ws_instances = []
        clock_val = [1000.0]
        sleeps = []

        def fake_http(url, headers=None, timeout=15):
            if "/v3/feed/market-data-feed/authorize" in url:
                idx = len(auth_calls) + 1
                auth_calls.append(url)
                return 200, json.dumps({
                    "status": "success",
                    "data": {"authorized_redirect_uri": "wss://ws.upstox.example/feed?one_time=%d" % idx},
                })
            raise AssertionError("Unexpected URL: %s" % url)

        def fake_ws_connect(url):
            if len(ws_instances) == 0:
                ws = _FakeWebSocket(frames=[pkt, None])
            else:
                ws = _FakeWebSocket(frames=[pkt])
            ws.url = url
            ws_instances.append(ws)
            return ws

        events = []
        statuses = []
        prov = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            http_get=fake_http,
            ws_connect=fake_ws_connect,
        )
        streamer = prov.create_streamer(
            on_message=events.append,
            on_status=statuses.append,
            stale_after_seconds=10.0,
            clock=lambda: clock_val[0],
            sleep_fn=sleeps.append,
        )
        streamer.connect()
        self.assertEqual(len(auth_calls), 1)
        streamer.subscribe(["NSE_EQ|INE002A01018"], mode="full")
        streamer.change_mode(["NSE_EQ|INE002A01018"], mode="ltpc")
        self.assertEqual(ws_instances[0].sent[0]["method"], "sub")
        self.assertEqual(ws_instances[0].sent[1]["method"], "change_mode")

        # Standard tier (default) disallows full_d30 even for 1 key
        exact_50 = ["NSE_EQ|INE%09d" % i for i in range(50)]
        with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
            streamer.subscribe(exact_50[:1], mode="full_d30")

        # Upstox Plus tier entitles full_d30 up to 50 keys, and rejects 51
        prov_plus = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            http_get=fake_http,
            ws_connect=fake_ws_connect,
            account_tier="plus",
        )
        streamer_plus = prov_plus.create_streamer()
        over_50 = ["NSE_EQ|INE%09d" % i for i in range(51)]
        with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
            streamer_plus.subscribe(over_50, mode="full_d30")
        streamer_plus.subscribe(exact_50, mode="full_d30")
        self.assertEqual(len(streamer_plus.active_keys("full_d30")), 50)
        streamer_plus.unsubscribe(exact_50)
        self.assertEqual(len(streamer_plus.active_keys("full_d30")), 0)

        # Consume binary Protobuf feed frame
        ev = streamer.poll_once()
        self.assertIsNotNone(ev)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(ev["feeds"]["NSE_EQ|INE002A01018"]["last_price"], 1385.50, places=2)
        self.assertEqual(ev["feeds"]["NSE_EQ|INE002A01018"]["volume"], 2500000)

        # Stale feed detection when clock advances without ticks
        self.assertFalse(streamer.is_stale())
        clock_val[0] = 1020.0
        self.assertTrue(streamer.is_stale())
        self.assertTrue(streamer.check_stale())
        self.assertTrue(any(s["state"] == "stale" for s in statuses))

        # Reconnect acquires a NEW single-use authorize() WSS URL & resubscribes active keys
        streamer.reconnect(max_attempts=3, initial_backoff=1.0)
        self.assertEqual(len(auth_calls), 2)
        self.assertNotEqual(ws_instances[0].url, ws_instances[1].url)
        self.assertEqual(ws_instances[1].sent[0]["method"], "sub")
        self.assertEqual(ws_instances[1].sent[0]["data"]["instrumentKeys"], ["NSE_EQ|INE002A01018"])
        ev2 = streamer.poll_once()
        self.assertIsNotNone(ev2)
        self.assertFalse(streamer.is_stale())
        streamer.close()

    def test_websocket_reconnect_backoff_schedule(self):
        attempts = [0]
        sleeps = []

        def fake_auth():
            return "wss://ws.upstox.example/feed?t=1"

        def flaky_connect(url):
            attempts[0] += 1
            if attempts[0] < 3:
                raise OSError("temporary network outage")
            return _FakeWebSocket()

        streamer = upstox_feed.UpstoxMarketDataStreamer(
            authorize_fn=fake_auth,
            ws_connect=flaky_connect,
            sleep_fn=sleeps.append,
        )
        streamer.reconnect(max_attempts=4, initial_backoff=1.0, backoff_factor=2.0, max_backoff=10.0)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertTrue(streamer.is_connected)
        streamer.close()

    def test_websocket_protobuf_index_first_level_and_json_frames(self):
        ltpc_idx = _encode_ltpc(24510.25, 1760850000000, 0, 24420.0)
        ohlc_idx = _pb_field_bytes(
            1,
            _pb_field_str(1, "1d") + _pb_field_double(2, 24450.0) + _pb_field_double(3, 24530.0)
            + _pb_field_double(4, 24410.0) + _pb_field_double(5, 24510.25)
            + _pb_field_varint(6, 0) + _pb_field_varint(7, 1760850000000),
        )
        iff_b = _pb_field_bytes(1, ltpc_idx) + _pb_field_bytes(2, ohlc_idx)
        ff_idx = _pb_field_bytes(2, iff_b)
        feed_idx = _pb_field_bytes(2, ff_idx)
        entry_idx = _pb_field_str(1, "NSE_INDEX|Nifty 50") + _pb_field_bytes(2, feed_idx)

        flwg_b = (
            _pb_field_bytes(1, _encode_ltpc(132.5, 1760850000000, 75, 120.0))
            + _pb_field_bytes(2, _pb_field_varint(1, 150) + _pb_field_double(2, 132.2) + _pb_field_varint(3, 150) + _pb_field_double(4, 132.8))
            + _pb_field_bytes(3, _pb_field_double(1, 0.52) + _pb_field_double(2, -12.0) + _pb_field_double(3, 0.0014) + _pb_field_double(4, 9.5))
            + _pb_field_varint(4, 150000)
            + _pb_field_double(5, 3100000.0)
            + _pb_field_double(6, 13.2)
        )
        entry_opt = _pb_field_str(1, "NSE_FO|10003") + _pb_field_bytes(2, _pb_field_bytes(3, flwg_b))

        pkt = _pb_field_varint(1, 1) + _pb_field_bytes(2, entry_idx) + _pb_field_bytes(2, entry_opt)
        norm = upstox_feed.normalize_feed_message(pkt)
        self.assertAlmostEqual(norm["feeds"]["NSE_INDEX|Nifty 50"]["last_price"], 24510.25, places=2)
        self.assertEqual(norm["feeds"]["NSE_INDEX|Nifty 50"]["ohlc"][0]["high"], 24530.0)
        self.assertEqual(norm["feeds"]["NSE_FO|10003"]["oi"], 3100000.0)
        self.assertEqual(norm["feeds"]["NSE_FO|10003"]["bid"], 132.2)
        self.assertAlmostEqual(norm["feeds"]["NSE_FO|10003"]["greeks"]["delta"], 0.52, places=3)

        # JSON text frame also normalizes seamlessly
        json_ev = upstox_feed.normalize_feed_message(json.dumps({
            "type": "live_feed",
            "currentTs": "1760850002000",
            "feeds": {"NSE_EQ|INE002A01018": {"ltpc": {"ltp": 1390.0, "ltt": 1760850002000, "ltq": 10, "cp": 1350.0}}},
        }))
        self.assertEqual(json_ev["feeds"]["NSE_EQ|INE002A01018"]["last_price"], 1390.0)

    def test_stdlib_websocket_rfc6455_framing_via_socketpair(self):
        import socket as _sock
        s_client, s_server = _sock.socketpair()
        try:
            ws = upstox_feed.StdlibWebSocketClient("wss://ws.upstox.example/feed")
            ws._sock = s_client
            ws.closed = False

            def _ws_frame(fin, opcode, data):
                b0 = (0x80 if fin else 0) | (opcode & 0x0F)
                n = len(data)
                if n < 126:
                    hdr = bytes([b0, n])
                else:
                    hdr = bytes([b0, 126]) + struct.pack("!H", n)
                return hdr + data

            # Send Ping (opcode 0x9) followed by fragmented Binary Protobuf message (0x2 + 0x0 with FIN)
            pkt = _encode_market_full_feed_packet()
            mid = len(pkt) // 2
            ping_frame = _ws_frame(True, 0x9, b"hi")
            frag1 = _ws_frame(False, 0x2, pkt[:mid])
            frag2 = _ws_frame(True, 0x0, pkt[mid:])
            s_server.sendall(ping_frame + frag1 + frag2)

            received = ws.recv(timeout=2.0)
            self.assertEqual(received, pkt)
            # Verify auto-Pong client-masked frame was written back to server socket
            s_server.settimeout(1.0)
            pong = s_server.recv(64)
            self.assertEqual(pong[0] & 0x0F, 0x0A)
            self.assertTrue(pong[1] & 0x80)  # masked per RFC 6455 client rule
            mask = pong[2:6]
            unmasked = bytes(b ^ mask[i & 3] for i, b in enumerate(pong[6:8]))
            self.assertEqual(unmasked, b"hi")
        finally:
            s_client.close()
            s_server.close()

    # 10. End-to-End Skill Integration (entry, timemachine, scan, ticket) -----

    def test_end_to_end_live_and_historical_provenance_with_upstox(self):
        import entry as entry_mod  # noqa: E402
        import ticket as ticket_mod  # noqa: E402
        import timemachine as tm_mod  # noqa: E402

        # Live open-session 5m ORB bars (opening range 09:15..09:30 < 1350, breakout at 09:30 close 1356)
        now_open = datetime.datetime(2026, 10, 19, 9, 36, 0, tzinfo=calendar_in.IST)
        live_bars = [
            ["2026-10-16T15:25:00+05:30", 1338.0, 1341.0, 1337.0, 1340.0, 5000, 0],
            ["2026-10-19T09:15:00+05:30", 1342.0, 1348.0, 1340.0, 1346.0, 9000, 0],
            ["2026-10-19T09:20:00+05:30", 1346.0, 1349.5, 1343.0, 1347.0, 8000, 0],
            ["2026-10-19T09:25:00+05:30", 1347.0, 1350.0, 1345.0, 1349.0, 8500, 0],
            ["2026-10-19T09:30:00+05:30", 1349.0, 1358.0, 1348.5, 1356.0, 22000, 0],
        ]

        def fake_http(url, headers=None, timeout=15):
            return 200, json.dumps({"status": "success", "data": {"candles": list(reversed(live_bars))}})

        prov = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=fake_http,
            now_fn=lambda: now_open,
        )
        pl = prov.get_intraday_data("RELIANCE", interval="5m", range_str="60d", now=now_open)
        ch = data_in.to_entry_chart(pl)
        ch["is_live_capable"] = True
        now_utc = now_open.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        ev = entry_mod.evaluate(ch, now_utc, setup="orb", or_minutes=15, interval="5m", market="IN")
        self.assertEqual(ev["status"], "triggered")
        self.assertEqual(ev["provider"], "upstox")
        self.assertEqual(ev["data_tier"], "CURRENT")
        self.assertTrue(ev["is_live_data"])
        self.assertTrue(ev["is_live_signal"])
        self.assertFalse(ev["is_historical_analysis"])

        # Ticket provenance from a live CURRENT Upstox signal must NOT flag HISTORICAL WARNING
        live_order = {
            "symbol": "RELIANCE",
            "side": "buy",
            "qty": "10",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit_limit_price": "1376.00",
            "stop_loss_stop_price": "1346.00",
            "market": "IN",
            "currency": "INR",
            "trading_style": "intraday",
            "last_data_timestamp": ev["last_data_timestamp"],
            "provider": "upstox",
            "data_tier": "CURRENT",
            "data_status": "current",
            "is_live_signal": True,
            "is_live_data": True,
            "is_historical_analysis": False,
        }
        prov_ticket = ticket_mod.signal_provenance(live_order)
        self.assertEqual(prov_ticket["provider"], "upstox")
        self.assertEqual(prov_ticket["data_tier"], "CURRENT")
        self.assertTrue(prov_ticket["is_live_signal"])
        self.assertIsNone(prov_ticket["signal_warning"])

        # Closed-session evaluation with same Upstox bars MUST downgrade to HISTORICAL
        now_closed = datetime.datetime(2026, 10, 19, 19, 0, 0, tzinfo=calendar_in.IST)
        pl_closed = prov.get_intraday_data("RELIANCE", interval="5m", range_str="60d", now=now_closed)
        ch_closed = data_in.to_entry_chart(pl_closed)
        ch_closed["is_live_capable"] = True
        ev_closed = entry_mod.evaluate(
            ch_closed,
            now_closed.astimezone(datetime.timezone.utc).replace(tzinfo=None),
            setup="orb", or_minutes=15, interval="5m", market="IN",
        )
        self.assertEqual(ev_closed["data_tier"], "HISTORICAL")
        self.assertFalse(ev_closed["is_live_signal"])
        self.assertTrue(ev_closed["is_historical_analysis"])

        # timemachine adapter cleanly ingests Upstox intraday payload dict
        meta_tm, bars_tm, dropped = tm_mod._bars_from_provider_payload(
            pl_closed, "RELIANCE", now_closed.astimezone(datetime.timezone.utc).replace(tzinfo=None), market="IN"
        )
        self.assertGreaterEqual(len(bars_tm), 4)
        self.assertEqual(sum(dropped.values()), 0)

        # exit.py adapter cleanly consumes Upstox payload
        ex_ch = data_in.to_exit_chart(pl)
        self.assertEqual(ex_ch["provider"], "upstox")
        self.assertEqual(ex_ch["data_status"], data_in.DataStatus.CURRENT)
        self.assertTrue(ex_ch["is_live"])

        # scanner batch series works and preserves Upstox provider & never swallows 401
        daily_bars = [
            ["2026-10-%02dT00:00:00+05:30" % d, 1300.0 + d, 1310.0 + d, 1295.0 + d, 1305.0 + d, 100000, 0]
            for d in range(1, 25)
        ]

        def daily_http(url, headers=None, timeout=15):
            if "INE467B01029" in url:
                return 404, json.dumps({"status": "error", "errors": [{"errorCode": "UDAPI100011", "message": "Invalid Instrument key"}]})
            return 200, json.dumps({"status": "success", "data": {"candles": list(reversed(daily_bars))}})

        prov_scan = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|INE467B01029"}},
            http_get=daily_http,
            now_fn=lambda: now_closed,
        )
        batch_res = prov_scan.get_batch_series(["RELIANCE", "TCS"], range_str="1mo", interval="1d")
        self.assertIn("RELIANCE", batch_res)
        self.assertNotIn("TCS", batch_res)
        self.assertEqual(batch_res["RELIANCE"]["provider"], "upstox")
        self.assertEqual(len(batch_res["RELIANCE"]["c"]), 24)
        with self.assertRaises(data_in.SymbolNotFoundError):
            prov_scan.get_batch_series(["TCS"], range_str="1mo", interval="1d")

        # HTTP error mapping checks: 429 RateLimitError, 502 ProviderConnectionError, 403 AuthError
        prov_err_codes = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=lambda u, headers=None, timeout=15: (429, '{"status":"error","errors":[{"message":"Rate limit"}]}'),
        )
        with self.assertRaises(data_in.RateLimitError):
            prov_err_codes.get_quote("RELIANCE")

        prov_502 = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=lambda u, headers=None, timeout=15: (502, "Bad Gateway"),
        )
        with self.assertRaises(data_in.ProviderConnectionError):
            prov_502.get_quote("RELIANCE")

        # Standard tier enforces 2 concurrent WebSocket connections by default
        std_streamers = []
        for idx in range(2):
            st_i = prov.create_streamer()
            st_i._authorize_fn = lambda i=idx: "wss://ws.upstox.example/s%d" % i
            st_i._ws_connect = lambda u: _FakeWebSocket()
            st_i.connect()
            std_streamers.append(st_i)
        st_3 = prov.create_streamer()
        st_3._authorize_fn = lambda: "wss://ws.upstox.example/s3"
        st_3._ws_connect = lambda u: _FakeWebSocket()
        with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
            st_3.connect()
        std_streamers[0].close()
        st_3.connect()
        st_3.close()
        std_streamers[1].close()

        # Plus tier enforces 5 concurrent WebSocket connections
        prov_plus_ws = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            account_tier="plus",
        )
        plus_streamers = []
        for idx in range(5):
            st_i = prov_plus_ws.create_streamer()
            st_i._authorize_fn = lambda i=idx: "wss://ws.upstox.example/p%d" % i
            st_i._ws_connect = lambda u: _FakeWebSocket()
            st_i.connect()
            plus_streamers.append(st_i)
        st_6 = prov_plus_ws.create_streamer()
        st_6._authorize_fn = lambda: "wss://ws.upstox.example/p6"
        st_6._ws_connect = lambda u: _FakeWebSocket()
        with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
            st_6.connect()
        for st_i in plus_streamers:
            st_i.close()

        # CLI alerts.py readiness fails explicitly with config guidance when upstox token missing
        cfg_flag = os.path.join(ROOT, "trader-budget", "config.in.example.json")
        proc = subprocess.run(
            [sys.executable, os.path.join(ALERTS_DIR, "alerts.py"), "readiness", "--market", "IN", "--config", cfg_flag, "--json"],
            env=dict(os.environ, INDIA_MARKET_DATA_PROVIDER="upstox", UPSTOX_ANALYTICS_TOKEN=""),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        readiness_doc = json.loads(proc.stdout)
        self.assertEqual(readiness_doc["verdict"], "BLOCKED")
        prov_check = [c for c in readiness_doc["checks"] if "Data Provider" in c["check"]][0]
        self.assertEqual(prov_check["status"], "ERROR")
        self.assertIn("upstox", prov_check["detail"].lower())
        self.assertIn("UPSTOX_ANALYTICS_TOKEN", prov_check["note"])

        # CLI alerts.py readiness succeeds when UPSTOX_ANALYTICS_TOKEN is configured (without exposing token)
        proc_ok = subprocess.run(
            [sys.executable, os.path.join(ALERTS_DIR, "alerts.py"), "readiness", "--market", "IN", "--config", cfg_flag, "--json"],
            env=dict(os.environ, INDIA_MARKET_DATA_PROVIDER="upstox", UPSTOX_ANALYTICS_TOKEN=self.SAMPLE_TOKEN),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
        )
        ok_doc = json.loads(proc_ok.stdout)
        ok_prov = [c for c in ok_doc["checks"] if "Data Provider" in c["check"]][0]
        self.assertEqual(ok_prov["status"], "OK")
        self.assertIn("upstox (LIVE/CURRENT)", ok_prov["detail"])
        self.assertIn("Tier: standard (max 2 WS)", ok_prov["detail"])
        self.assertIn("Options: upstox", ok_prov["detail"])
        self.assertTrue(ok_doc["capabilities"]["live_quotes"])
        self.assertTrue(ok_doc["capabilities"]["streaming_data"])
        self.assertNotIn(self.SAMPLE_TOKEN, proc_ok.stdout)

    # 11. Hardening: Tier Entitlements, Combined Limits, Redirects & Contracts

    def test_account_tier_configuration_and_entitlement_limits(self):
        # Default tier must be strictly 'standard' even when unset in config/env
        with mock.patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": self.SAMPLE_TOKEN}, clear=False):
            os.environ.pop("UPSTOX_ACCOUNT_TIER", None)
            prov_def = data_in.UpstoxMarketDataProvider()
            self.assertEqual(prov_def.account_tier, "standard")
            limits = prov_def.stream_limits()
            self.assertEqual(limits["max_connections"], 2)
            self.assertIsNone(limits["modes"]["full_d30"])
            self.assertEqual(limits["modes"]["ltpc"], {"individual": 5000, "combined": 2000, "per_connection": 5000})
            self.assertEqual(limits["modes"]["option_greeks"], {"individual": 3000, "combined": 2000, "per_connection": 3000})
            self.assertEqual(limits["modes"]["full"], {"individual": 2000, "combined": 1500, "per_connection": 2000})

        # Configurable via env or config provider_credentials.upstox.account_tier
        prov_env = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            env={"UPSTOX_ACCOUNT_TIER": "plus"},
        )
        self.assertEqual(prov_env.account_tier, "plus")
        self.assertEqual(prov_env.stream_limits()["max_connections"], 5)
        self.assertEqual(prov_env.stream_limits()["modes"]["full_d30"], {"individual": 50, "combined": 1500, "per_connection": 50})

        prov_cfg = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"provider_credentials": {"upstox": {"account_tier": "upstox_plus"}}},
        )
        self.assertEqual(prov_cfg.account_tier, "plus")

        with self.assertRaises(data_in.ProviderConfigError):
            data_in.UpstoxMarketDataProvider.validate_config(
                config={"provider_credentials": {"upstox": {"analytics_token": self.SAMPLE_TOKEN, "account_tier": "enterprise"}}}
            )

        # Standard single-mode individual limits: ltpc 5000, option_greeks 3000, full 2000
        def _keys(prefix, count):
            return ["NSE_EQ|%s%05d" % (prefix, i) for i in range(count)]

        for mode_name, max_ind in (("ltpc", 5000), ("option_greeks", 3000), ("full", 2000)):
            st = prov_def.create_streamer()
            st.subscribe(_keys(mode_name[:2].upper(), max_ind), mode=mode_name)
            with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
                st.subscribe(_keys("EX", 1), mode=mode_name)

        # Standard combined limits across single & multiple concurrent connections
        p_combo = data_in.UpstoxMarketDataProvider(analytics_token=self.SAMPLE_TOKEN)
        s1 = p_combo.create_streamer()
        s1._authorize_fn = lambda: "wss://ws.upstox.example/c1"
        s1._ws_connect = lambda u: _FakeWebSocket()
        s2 = p_combo.create_streamer()
        s2._authorize_fn = lambda: "wss://ws.upstox.example/c2"
        s2._ws_connect = lambda u: _FakeWebSocket()

        s1.connect()
        s2.connect()
        s1.subscribe(_keys("L", 1200), mode="ltpc")
        # Adding 800 option_greeks across s2 hits exact 2,000 combined limit -> OK
        s2.subscribe(_keys("G", 800), mode="option_greeks")
        # Adding 1 more option_greeks key exceeds 2,000 combined -> rejected cleanly
        with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
            s2.subscribe(["NSE_FO|EXTRA1"], mode="option_greeks")
        self.assertEqual(len(s2.active_keys("option_greeks")), 800)

        # Full + LTPC combined ceiling drops to 1,500
        s2.unsubscribe(_keys("G", 800))
        with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
            s2.subscribe(_keys("F", 301), mode="full")  # 1200 + 301 = 1501 > 1500
        s2.subscribe(_keys("F", 300), mode="full")      # 1200 + 300 = 1500 -> OK
        s1.close()
        s2.close()

        # Plus combined with full_d30 (<=50) + ltpc (<=1450) -> 1500 allowed, 1501 rejected
        p_plus = data_in.UpstoxMarketDataProvider(analytics_token=self.SAMPLE_TOKEN, account_tier="plus")
        sp1 = p_plus.create_streamer()
        sp1.subscribe(_keys("D", 50), mode="full_d30")
        sp1.subscribe(_keys("LP", 1450), mode="ltpc")
        with self.assertRaises(upstox_feed.UpstoxStreamLimitError):
            sp1.subscribe(["NSE_EQ|OVER1501"], mode="ltpc")

    def test_websocket_http_redirect_market_status_proto_and_eof_hardening(self):
        import hashlib as _hashlib
        import base64 as _b64
        import socket as _sock
        import threading as _threading

        # Real socketpair/TCP HTTP 302 redirect -> 101 WebSocket handshake
        srv = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        server_paths = []

        def _run_ws_server():
            # Connection 1: return 302 redirect to /v3/live
            c1, _ = srv.accept()
            req1 = c1.recv(4096).decode("ascii", errors="replace")
            server_paths.append(req1.splitlines()[0] if req1 else "")
            c1.sendall(
                (
                    "HTTP/1.1 302 Found\r\n"
                    "Location: ws://127.0.0.1:%d/v3/live?sig=ok\r\n"
                    "Content-Length: 0\r\n\r\n" % port
                ).encode("ascii")
            )
            c1.close()

            # Connection 2: complete 101 handshake
            c2, _ = srv.accept()
            req2 = c2.recv(4096).decode("ascii", errors="replace")
            server_paths.append(req2.splitlines()[0] if req2 else "")
            ws_key = ""
            for line in req2.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    ws_key = line.split(":", 1)[1].strip()
            accept_val = _b64.b64encode(
                _hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
            ).decode("ascii")
            c2.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: %s\r\n\r\n" % accept_val
                ).encode("ascii")
            )
            # Immediately close TCP without 0x08 close frame -> test EOF handling in poll_once
            c2.close()

        t = _threading.Thread(target=_run_ws_server, daemon=True)
        t.start()
        try:
            statuses = []
            clock_val = [100.0]
            streamer = upstox_feed.UpstoxMarketDataStreamer(
                authorize_fn=lambda: "ws://127.0.0.1:%d/init" % port,
                ws_connect=lambda u: upstox_feed.StdlibWebSocketClient(u, timeout=2.0).connect(),
                on_status=statuses.append,
                stale_after_seconds=5.0,
                clock=lambda: clock_val[0],
            )
            streamer.connect()
            self.assertTrue(streamer.is_connected)
            self.assertEqual(len(server_paths), 2)
            self.assertIn("/init", server_paths[0])
            self.assertIn("/v3/live?sig=ok", server_paths[1])

            # EOF from peer without close frame must disconnect cleanly and release quota
            ev = streamer.poll_once(timeout=1.0)
            self.assertIsNone(ev)
            self.assertFalse(streamer.is_connected)
            self.assertEqual(streamer._quota.connected_count(), 0)
            self.assertTrue(any(s["state"] == "disconnected" for s in statuses))
        finally:
            t.join(timeout=2.0)
            srv.close()

        # MarketStatus Protobuf enum numbering: PRE_OPEN_START=0 .. CLOSING_END=5
        seg_entries = [
            _pb_field_bytes(1, _pb_field_str(1, "NSE_EQ") + _pb_field_varint(2, 2)),
            _pb_field_bytes(1, _pb_field_str(1, "BSE_EQ") + _pb_field_varint(2, 0)),
            _pb_field_bytes(1, _pb_field_str(1, "NCD_FO") + _pb_field_varint(2, 3)),
            _pb_field_bytes(1, _pb_field_str(1, "MCX_FO") + _pb_field_varint(2, 5)),
        ]
        mkt_info_pkt = (
            _pb_field_varint(1, 2)
            + _pb_field_varint(3, 1760850005000)
            + _pb_field_bytes(4, b"".join(seg_entries))
        )
        decoded_mi = upstox_feed.decode_protobuf_feed_response(mkt_info_pkt)
        self.assertEqual(decoded_mi["type"], "market_info")
        self.assertEqual(decoded_mi["marketInfo"]["segmentStatus"]["NSE_EQ"], "NORMAL_OPEN")
        self.assertEqual(decoded_mi["marketInfo"]["segmentStatus"]["BSE_EQ"], "PRE_OPEN_START")
        self.assertEqual(decoded_mi["marketInfo"]["segmentStatus"]["NCD_FO"], "NORMAL_CLOSE")
        self.assertEqual(decoded_mi["marketInfo"]["segmentStatus"]["MCX_FO"], "CLOSING_END")

        # Heartbeat (no feeds) while subscribed must NOT mask stale tick detection
        clk = [10.0]
        st_stale = upstox_feed.UpstoxMarketDataStreamer(
            authorize_fn=lambda: "wss://ws.upstox.example/hb",
            ws_connect=lambda u: _FakeWebSocket(frames=[mkt_info_pkt, None]),
            stale_after_seconds=5.0,
            clock=lambda: clk[0],
        )
        st_stale.connect()
        st_stale.subscribe(["NSE_EQ|INE002A01018"], mode="ltpc")
        clk[0] = 14.0
        st_stale.poll_once()  # market_info only, no feeds -> last_message_at=14, last_tick_at=None
        clk[0] = 17.0          # 7s since connect(10), only 3s since heartbeat(14)
        self.assertTrue(st_stale.is_stale())
        health = st_stale.get_health()
        self.assertIsNone(health["seconds_since_last_tick"])
        self.assertEqual(health["seconds_since_last_message"], 3.0)
        st_stale.close()

    def test_api_contract_edge_cases_historical_bounds_ohlc_and_option_chain(self):
        # 1. Non-leap Feb 28-day month cap (minutes <= 15m -> 28d max inclusive chunk)
        #    and Feb+Mar+Apr 89-day quarter cap (1h -> 89d max inclusive chunk)
        chunks_5m = data_in._iter_upstox_date_chunks(
            datetime.date(2025, 2, 1), datetime.date(2025, 3, 5), "minutes", "5"
        )
        for c_start, c_end in chunks_5m:
            self.assertLessEqual((c_end - c_start).days + 1, 28)

        chunks_1h = data_in._iter_upstox_date_chunks(
            datetime.date(2025, 2, 1), datetime.date(2025, 6, 1), "hours", "1"
        )
        for c_start, c_end in chunks_1h:
            self.assertLessEqual((c_end - c_start).days + 1, 89)

        # 2. Zero/non-positive epoch timestamp ignored; intraday 1m OHLC does not misuse prev_ohlc
        self.assertIsNone(data_in._parse_upstox_timestamp(0))
        self.assertIsNone(data_in._parse_upstox_timestamp("0"))

        def fake_http(url, headers=None, timeout=15):
            if "/v3/market-quote/ohlc" in url:
                # Response without cp, but with prev_ohlc from prior 1m bar
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "NSE_EQ:RELIANCE": {
                            "last_price": 1352.0,
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "live_ohlc": {"open": 1351.0, "high": 1352.5, "low": 1350.5, "close": 1352.0, "ts": 1760850000000},
                            "prev_ohlc": {"open": 1350.0, "high": 1351.2, "low": 1349.8, "close": 1351.0, "ts": 1760849940000},
                        }
                    },
                })
            if "/v2/option/chain" in url:
                # Missing prev_oi and zero underlying_spot_price -> should preserve prev_oi=None & fallback spot via ltp
                return 200, json.dumps({
                    "status": "success",
                    "data": [{
                        "expiry": "2026-10-27",
                        "strike_price": 24500,
                        "underlying_spot_price": 0,
                        "call_options": {
                            "instrument_key": "NSE_FO|10003",
                            "market_data": {"ltp": 130.0, "volume": 5000, "oi": 250000},
                            "option_greeks": {"delta": 0.5, "iv": 13.0},
                        },
                    }],
                })
            if "/v3/market-quote/ltp" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {"NSE_INDEX:Nifty 50": {"last_price": 24520.0, "instrument_token": "NSE_INDEX|Nifty 50", "cp": 24400.0}},
                })
            raise AssertionError("Unexpected URL: %s" % url)

        open_dt = datetime.datetime(2026, 10, 19, 10, 35, 0, tzinfo=calendar_in.IST)
        prov = data_in.UpstoxMarketDataProvider(
            analytics_token=self.SAMPLE_TOKEN,
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=fake_http,
            now_fn=lambda: open_dt,
        )
        ohlc_1m = prov.get_ohlc("RELIANCE", interval="1m")
        self.assertIsNone(ohlc_1m["prev_close"])

        ohlc_1d = prov.get_ohlc("RELIANCE", interval="1d")
        self.assertEqual(ohlc_1d["prev_close"], 1351.0)

        # Intraday history prior to Jan 2022 fails explicitly with MissingDataError
        with self.assertRaises(data_in.MissingDataError):
            prov.get_historical_data("RELIANCE", interval="5m", start="2021-01-01", end="2021-06-01")
        with self.assertRaises(data_in.MissingDataError):
            prov.get_intraday_data("RELIANCE", interval="5m", now="2021-06-01 12:00:00")

        # Option chain preserves prev_oi=None & change_in_oi=None when omitted, and falls back from 0 spot
        chain = prov.get_option_chain("NIFTY", expiry="2026-10-27")
        self.assertEqual(chain.underlying_price, 24520.0)
        ce = chain.get_contract(24500.0, "CE")
        self.assertEqual(ce["open_interest"], 250000)
        self.assertIsNone(ce["prev_oi"])
        self.assertIsNone(ce["change_in_oi"])


if __name__ == "__main__":
    unittest.main()
