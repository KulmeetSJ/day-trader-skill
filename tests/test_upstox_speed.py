"""Upstox speed knobs (2026-11-20): parallel get_batch_series and the same-day historical-candle cache.
Hermetic: mocked HTTP, fixed clock. Both knobs are opt-in env settings; default behaviour is unchanged."""
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-alerts"))
import _load  # noqa: E402,F401
import data_in  # type: ignore  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TOKEN = "eyJhbGciOiJIUzI1NiJ9.sampleAnalyticsTokenPayload.sig123456"
KEYS = {"RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|INE467B01029", "INFY": "NSE_EQ|INE009A01021"}
NOW = datetime.datetime(2026, 10, 21, 11, 0, tzinfo=IST)          # a Wednesday session, 11:00 IST


def daily(base):
    return [["2026-10-%02dT00:00:00+05:30" % d, base + d, base + d + 10, base + d - 5, base + d + 5, 100000, 0]
            for d in range(1, 21)]


def five_min(day, base, n):
    out = []
    t = datetime.datetime(2026, 10, day, 9, 15, tzinfo=IST)
    for i in range(n):
        ts = (t + datetime.timedelta(minutes=5 * i)).isoformat()
        out.append([ts, base + i, base + i + 1, base + i - 1, base + i + 0.5, 1000, 0])
    return out


class Recorder(object):
    def __init__(self, missing=("INE467B01029",), rate_limit=False):
        self.urls, self.missing, self.rate_limit = [], missing, rate_limit
        self.lock = threading.Lock()

    def __call__(self, url, headers=None, timeout=15):
        with self.lock:
            self.urls.append(url)
        if self.rate_limit:
            return 429, json.dumps({"status": "error", "errors": [{"message": "Rate limit"}]})
        if any(m in url for m in self.missing):
            return 404, json.dumps({"status": "error", "errors": [{"errorCode": "UDAPI100011",
                                                                   "message": "Invalid Instrument key"}]})
        base = 1000.0 if "INE002A01018" in url else 1500.0
        if "/intraday/" in url:
            candles = five_min(21, base, 21)                          # today 09:15-10:55
        elif "/minutes/" in url:
            candles = five_min(20, base, 75) + five_min(19, base, 75)  # past sessions only
        else:
            candles = daily(base)
        return 200, json.dumps({"status": "success", "data": {"candles": list(reversed(candles))}})


def provider(http, **env):
    e = {"UPSTOX_ANALYTICS_TOKEN": TOKEN}
    e.update(env)
    return data_in.UpstoxMarketDataProvider(analytics_token=TOKEN, config={"upstox_instruments": KEYS},
                                            env=e, http_get=http, now_fn=lambda: NOW)


class TestParallelBatch(unittest.TestCase):
    def test_default_is_sequential(self):
        self.assertEqual(provider(Recorder())._batch_workers, 1)
        self.assertIsNone(provider(Recorder())._hist_cache_dir)

    def test_parallel_matches_sequential(self):
        syms = ["RELIANCE", "TCS", "INFY"]
        seq = provider(Recorder()).get_batch_series(syms, range_str="1mo", interval="1d")
        par = provider(Recorder(), UPSTOX_BATCH_WORKERS="4").get_batch_series(syms, range_str="1mo", interval="1d")
        self.assertEqual(list(seq), list(par))                          # same symbols, same order
        self.assertNotIn("TCS", par)                                    # missing symbol skipped, not fatal
        for k in seq:
            self.assertEqual(seq[k]["c"], par[k]["c"])
            self.assertEqual(seq[k]["t"], par[k]["t"])

    def test_parallel_all_missing_raises(self):
        with self.assertRaises(data_in.SymbolNotFoundError):
            provider(Recorder(), UPSTOX_BATCH_WORKERS="4").get_batch_series(["TCS", "TCS"], range_str="1mo",
                                                                            interval="1d")

    def test_parallel_rate_limit_aborts_the_batch(self):
        orig = data_in.RATE_LIMIT_BACKOFF_S
        data_in.RATE_LIMIT_BACKOFF_S = 0.0
        try:
            with self.assertRaises(data_in.RateLimitError):        # still limited after the one retry
                provider(Recorder(rate_limit=True), UPSTOX_BATCH_WORKERS="4").get_batch_series(
                    ["RELIANCE", "INFY"], range_str="1mo", interval="1d")
        finally:
            data_in.RATE_LIMIT_BACKOFF_S = orig


class TestThrottle(unittest.TestCase):
    def test_max_rps_spaces_requests(self):
        import time
        rec = Recorder()
        p = provider(rec, UPSTOX_MAX_RPS="20", UPSTOX_BATCH_WORKERS="4")
        t0 = time.time()
        p.get_batch_series(["RELIANCE", "INFY", "RELIANCE", "INFY"], range_str="1mo", interval="1d")
        self.assertGreaterEqual(len(rec.urls), 2)
        self.assertGreaterEqual(time.time() - t0, (len(rec.urls) - 1) / 20.0 - 0.01)

    def test_one_retry_after_rate_limit(self):
        calls = {"n": 0}
        ok = Recorder()

        def flaky(url, headers=None, timeout=15):
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, json.dumps({"status": "error", "errors": [{"message": "Rate limit"}]})
            return ok(url, headers, timeout)
        orig = data_in.RATE_LIMIT_BACKOFF_S
        data_in.RATE_LIMIT_BACKOFF_S = 0.0
        try:
            out = provider(flaky, UPSTOX_BATCH_WORKERS="2").get_batch_series(["RELIANCE", "INFY"], range_str="1mo",
                                                                             interval="1d")
        finally:
            data_in.RATE_LIMIT_BACKOFF_S = orig
        self.assertEqual(sorted(out), ["INFY", "RELIANCE"])


class TestHistCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_past_sessions_cached_today_always_live(self):
        rec = Recorder()
        p = provider(rec, UPSTOX_HIST_CACHE_DIR=self.dir)
        first = p.get_intraday_data("RELIANCE", interval="5m", range_str="5d")
        n1 = len(rec.urls)
        self.assertEqual(sum("/intraday/" in u for u in rec.urls), 1)
        second = provider(rec, UPSTOX_HIST_CACHE_DIR=self.dir).get_intraday_data("RELIANCE", interval="5m",
                                                                                range_str="5d")
        new = rec.urls[n1:]
        self.assertEqual(len(new), 1)                                   # only today's intraday call
        self.assertIn("/intraday/", new[0])
        self.assertEqual([b["ts"] for b in first["bars"]], [b["ts"] for b in second["bars"]])
        self.assertTrue(any(b["date"] == "2026-10-21" for b in second["bars"]))

    def test_chunk_with_todays_bars_is_not_cached(self):
        p = provider(Recorder(), UPSTOX_HIST_CACHE_DIR=self.dir)
        today = NOW.date()
        p._hist_cache_put("/x", today, [{"ts": 1, "date": "2026-10-21", "c": 1.0}])
        self.assertIsNone(p._hist_cache_get("/x", today))
        p._hist_cache_put("/y", today, [{"ts": 1, "date": "2026-10-20", "c": 1.0}])
        self.assertEqual(p._hist_cache_get("/y", today), [{"ts": 1, "date": "2026-10-20", "c": 1.0}])
        self.assertIsNone(p._hist_cache_get("/y", today + datetime.timedelta(days=1)))   # day-scoped


if __name__ == "__main__":
    unittest.main()
