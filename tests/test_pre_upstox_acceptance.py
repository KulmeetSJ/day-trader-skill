"""Pre-Upstox regression and end-to-end acceptance tests for Indian market workflows.

Covers the defects discovered during pre-Upstox validation as well as an isolated
end-to-end paper workflow using offline fixtures and mock HTTP seams:
- YahooMarketDataProvider handling of non-200 tuple responses from injected http_get
- Environment/config switching to OfflineMarketDataProvider without engine changes
- Exit planner direct CLI support for all entry setups (rsi_momentum, ma_crossover, trend, options_orb)
- Risk checker explicit order-level is_historical_analysis precedence over plan metadata
- Scanner filtered strategy hit counts matching emitted hits
- Isolated end-to-end Indian workflow through paper execution and flatten
"""

import datetime
import gzip
import io
import json
import os
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
budget_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")

BHARAT_FIXTURES = os.path.join(FIX, "bharat")
IN_CONFIG_SRC = os.path.join(ROOT, "trader-budget", "config.in.example.json")


def run_cli(mod, argv):
    """Run a CLI module's main(argv) and capture exit code, stdout, and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def make_daily_yahoo_doc(symbol, closes, volumes=None, end_date="2026-10-16", prev_close=None):
    """Build a synthetic Yahoo chart JSON payload ending on end_date (15:30 IST)."""
    end_d = datetime.date.fromisoformat(end_date)
    dates = []
    cur = end_d
    while len(dates) < len(closes):
        if cur.weekday() < 5:
            dates.append(cur)
        cur -= datetime.timedelta(days=1)
    dates.reverse()
    tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    stamps = [int(datetime.datetime(d.year, d.month, d.day, 15, 30, tzinfo=tz).timestamp()) for d in dates]
    vols = list(volumes) if volumes is not None else [3000000 for _ in closes]
    opens = [round(c * 0.995, 2) for c in closes]
    highs = [round(c * 1.008, 2) for c in closes]
    lows = [round(c * 0.990, 2) for c in closes]
    last_c = float(closes[-1])
    pc = float(prev_close) if prev_close is not None else float(closes[-2] if len(closes) > 1 else closes[-1])
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "currency": "INR",
                        "exchangeName": "NSI",
                        "regularMarketPrice": last_c,
                        "chartPreviousClose": pc,
                        "previousClose": pc,
                        "regularMarketVolume": int(vols[-1]),
                    },
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": [float(c) for c in closes],
                                "volume": vols,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


class TestPreUpstoxAcceptance(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("INDIA_MARKET_DATA_PROVIDER", "INDIA_OFFLINE_DIR", "BHARAT_DATA_PROVIDER")
        }
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_yahoo_tuple_http_errors_map_cleanly(self):
        """Injected http_get returning non-200 status tuples must raise typed DataErrors."""
        p429 = data_in.YahooMarketDataProvider(
            pause=0.0,
            backoff=0.0,
            http_get=lambda _u: (429, "Too Many Requests"),
        )
        with self.assertRaises(data_in.RateLimitError):
            p429.get_quote("RELIANCE", exchange="NSE")
        with self.assertRaises(data_in.RateLimitError):
            p429._fetch_spark_url(["RELIANCE.NS"], {"range": "3mo", "interval": "1d"})

        p404 = data_in.YahooMarketDataProvider(
            pause=0.0,
            backoff=0.0,
            http_get=lambda _u: (404, "Not Found"),
        )
        with self.assertRaises(data_in.SymbolNotFoundError):
            p404.get_quote("MISSING", exchange="NSE")

        p500 = data_in.YahooMarketDataProvider(
            pause=0.0,
            backoff=0.0,
            http_get=lambda _u: (500, "Internal Server Error"),
        )
        with self.assertRaises(data_in.ProviderConnectionError) as ctx500:
            p500.get_quote("RELIANCE", exchange="NSE")
        self.assertNotIn("None", str(ctx500.exception))

        p403 = data_in.YahooMarketDataProvider(
            pause=0.0,
            backoff=0.0,
            http_get=lambda _u: (403, "Forbidden"),
        )
        with self.assertRaises(data_in.ProviderConnectionError) as ctx403:
            p403.get_quote("RELIANCE", exchange="NSE")
        self.assertIn("403", str(ctx403.exception))

    def test_offline_provider_env_and_config_switching(self):
        """INDIA_MARKET_DATA_PROVIDER=offline resolves cleanly via config, env, or readiness."""
        prov_cfg = data_in.get_provider(
            config={"india_market_data_provider": "offline", "offline_dir": BHARAT_FIXTURES}
        )
        self.assertIsInstance(prov_cfg, data_in.OfflineMarketDataProvider)
        self.assertEqual(prov_cfg.base_path, BHARAT_FIXTURES)
        q = prov_cfg.get_quote("RELIANCE", exchange="NSE", now="2026-10-19 15:25:00")
        self.assertEqual(q["provider"], "offline")
        self.assertEqual(q["data_status"], data_in.DataStatus.FIXTURE)
        self.assertFalse(q["is_live"])

        os.environ["INDIA_MARKET_DATA_PROVIDER"] = "offline"
        os.environ["INDIA_OFFLINE_DIR"] = BHARAT_FIXTURES
        prov_env = data_in.get_provider()
        self.assertIsInstance(prov_env, data_in.OfflineMarketDataProvider)
        self.assertEqual(prov_env.base_path, BHARAT_FIXTURES)

        with tempfile.TemporaryDirectory() as tmp:
            bad_dir = os.path.join(tmp, "missing_fixtures")
            saved_offline_dir = os.environ.pop("INDIA_OFFLINE_DIR", None)
            try:
                with self.assertRaises(data_in.ProviderConfigError):
                    data_in.get_provider(
                        config={"india_market_data_provider": "offline", "offline_dir": bad_dir}
                    )
                os.environ["INDIA_OFFLINE_DIR"] = bad_dir
                with self.assertRaises(data_in.ProviderConfigError):
                    data_in.get_provider()
            finally:
                if saved_offline_dir is None:
                    os.environ.pop("INDIA_OFFLINE_DIR", None)
                else:
                    os.environ["INDIA_OFFLINE_DIR"] = saved_offline_dir

        os.environ["INDIA_OFFLINE_DIR"] = BHARAT_FIXTURES
        code, out, _ = run_cli(alerts_mod, [
            "readiness",
            "--market", "IN",
            "--config", IN_CONFIG_SRC,
            "--now", "2026-10-19 10:15:00",
            "--json",
        ])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["provider"], "offline")
        checks_by_name = {c["check"]: c for c in doc["checks"]}
        self.assertIn("FIXTURE/HISTORICAL", checks_by_name["Data Provider & Tiers"]["detail"])

    def test_exit_plan_direct_supports_all_entry_setups(self):
        """exit.py plan accepts all entry.py setups directly with explicit stop and refuses without stop."""
        with tempfile.TemporaryDirectory() as tmp:
            for idx, (setup_name, expected_phrase) in enumerate([
                ("rsi_momentum", "RSI momentum"),
                ("ma_crossover", "moving-average crossover"),
                ("trend", "trend continuation"),
                ("options_orb", "options opening-range breakout"),
            ], start=1):
                plan_path = os.path.join(tmp, "plan_%d.json" % idx)
                code, out, err = run_cli(exit_mod, [
                    "plan",
                    "--symbol", "RELIANCE",
                    "--entry", "1340.00",
                    "--setup", setup_name,
                    "--stop", "price:1324.40",
                    "--target", "2R",
                    "--market", "IN",
                    "--trading-style", "intraday",
                    "--config", IN_CONFIG_SRC,
                    "--now", "2026-10-19 10:15:00",
                    "--write", plan_path,
                    "--json",
                ])
                self.assertEqual(code, 0, msg="%s failed: %s" % (setup_name, err))
                plan = json.loads(out)
                self.assertEqual(plan["setup"], setup_name)
                lines = exit_mod.describe(plan)
                self.assertTrue(any(expected_phrase in line for line in lines), msg=str(lines))

                code_no_stop, _, err_no_stop = run_cli(exit_mod, [
                    "plan",
                    "--symbol", "RELIANCE",
                    "--entry", "1340.00",
                    "--setup", setup_name,
                    "--market", "IN",
                    "--config", IN_CONFIG_SRC,
                    "--now", "2026-10-19 10:15:00",
                ])
                self.assertEqual(code_no_stop, 1, msg=err_no_stop)

    def test_risk_order_false_historical_overrides_plan_true(self):
        """Explicit order['is_historical_analysis']=False must not allow off-hours bypass via plan metadata."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "cfg.json")
            with open(IN_CONFIG_SRC, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            cfg["paper"] = True
            cfg["allow_off_hours"] = False
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)

            port_path = os.path.join(tmp, "portfolio.json")
            with open(port_path, "w", encoding="utf-8") as fh:
                json.dump({"cash": 100000.0, "positions": []}, fh)

            orders_path = os.path.join(tmp, "orders.json")
            order_doc = {
                "market": "IN",
                "currency": "INR",
                "date": "2026-10-19",
                "orders": [
                    {
                        "market": "IN",
                        "currency": "INR",
                        "symbol": "RELIANCE",
                        "side": "buy",
                        "qty": 5,
                        "type": "market",
                        "order_class": "bracket",
                        "time_in_force": "day",
                        "take_profit_limit_price": "1371.20",
                        "stop_loss_stop_price": "1324.40",
                        "estimated_entry": 1340.0,
                        "stop_price": 1324.4,
                        "target_price": 1371.2,
                        "max_loss": 78.0,
                        "trading_style": "intraday",
                        "product": "MIS",
                        "provider": "yahoo",
                        "data_tier": "DELAYED",
                        "is_historical_analysis": False,
                        "plan": {
                            "entry": 1340.0,
                            "stop": 1324.4,
                            "target": 1371.2,
                            "is_historical_analysis": True,
                        },
                    }
                ],
            }
            with open(orders_path, "w", encoding="utf-8") as fh:
                json.dump(order_doc, fh)

            risk_path = os.path.join(tmp, "risk.json")
            broker_path = os.path.join(tmp, "broker.json")
            code, out, err = run_cli(risk_mod, [
                "--orders", orders_path,
                "--config", cfg_path,
                "--portfolio", port_path,
                "--state", broker_path,
                "--out", risk_path,
                "--market", "IN",
                "--now", "2026-10-19 20:00:00",
                "--json",
            ])
            self.assertEqual(code, 1, msg=err)
            res = json.loads(out)
            self.assertEqual(res["results"][0]["verdict"], "BLOCK")
            self.assertFalse(res["results"][0]["is_historical_analysis"])
            self.assertTrue(
                any("closed" in r.lower() or "outside" in r.lower() for r in res["results"][0]["reasons"])
            )

    def test_scan_daily_strategy_filter_updates_counts_hits(self):
        """scan.py daily --strategy filters counts['hits'] to match the emitted hits list."""
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = os.path.join(tmp, "daily")
            os.makedirs(daily_dir)
            # RELIANCE closes with a 20-day breakout (not oversold)
            closes_brk = [1250.0 + (i % 5) for i in range(25)] + [1280.0]
            with open(os.path.join(daily_dir, "RELIANCE.json"), "w", encoding="utf-8") as fh:
                json.dump(make_daily_yahoo_doc("RELIANCE.NS", closes_brk), fh)
            # TCS closes flat inside range (no hits)
            closes_flat = [3500.0 + (i % 3) for i in range(26)]
            with open(os.path.join(daily_dir, "TCS.json"), "w", encoding="utf-8") as fh:
                json.dump(make_daily_yahoo_doc("TCS.NS", closes_flat), fh)

            uni_path = os.path.join(tmp, "universe.txt")
            with open(uni_path, "w", encoding="utf-8") as fh:
                fh.write("RELIANCE|Reliance\nTCS|TCS\n")

            out_path = os.path.join(tmp, "watchlist.json")
            code, out, err = run_cli(scan_mod, [
                "daily",
                "--market", "IN",
                "--universe", uni_path,
                "--offline", daily_dir,
                "--strategy", "oversold",
                "--now", "2026-10-19 08:30:00",
                "--out", out_path,
                "--json",
            ])
            self.assertEqual(code, 1, msg=err)
            doc = json.loads(out)
            self.assertEqual(doc["hits"], [])
            self.assertEqual(doc["counts"]["hits"], len(doc["hits"]))

    def test_isolated_e2e_offline_intraday_lifecycle(self):
        """End-to-end paper workflow using offline fixtures: exit -> size -> check -> show -> approve -> partial fill -> flatten."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "cfg.json")
            with open(IN_CONFIG_SRC, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            cfg["paper"] = True
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)

            port_path = os.path.join(tmp, "portfolio.json")
            with open(port_path, "w", encoding="utf-8") as fh:
                json.dump({"cash": 100000.0, "positions": []}, fh)

            entry_path = os.path.join(tmp, "entry.json")
            entry_doc = {
                "symbol": "TCS",
                "setup": "orb",
                "status": "triggered",
                "side": "buy",
                "date": "2026-10-19",
                "interval": "5m",
                "or_minutes": 15,
                "opening_range": {"high": 3505.0, "low": 3480.0, "complete": True},
                "level": 3505.0,
                "price": 3510.0,
                "time": "09:45",
                "bar_index": 6,
                "trigger": {"price": 3510.0, "bar_start": "09:45", "time": "09:45", "bar": 6},
                "prev_close": 3485.0,
                "market": "IN",
                "currency": "INR",
                "trading_style": "intraday",
                "provider": "offline",
                "data_tier": "FIXTURE",
                "data_status": "fixture",
                "is_fixture": True,
                "is_historical_analysis": True,
                "is_live_data": False,
                "is_live_signal": False,
                "is_simulated": False,
                "last_data_timestamp": "2026-10-19T09:50:00+05:30",
            }
            with open(entry_path, "w", encoding="utf-8") as fh:
                json.dump(entry_doc, fh)

            plan_path = os.path.join(tmp, "plan.json")
            code, out, err = run_cli(exit_mod, [
                "plan",
                "--from", entry_path,
                "--target", "4R",
                "--config", cfg_path,
                "--market", "IN",
                "--trading-style", "intraday",
                "--now", "2026-10-19 09:55:00",
                "--write", plan_path,
                "--json",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))

            orders_path = os.path.join(tmp, "orders.json")
            code, out, err = run_cli(budget_mod, [
                "trade",
                "--plan", plan_path,
                "--config", cfg_path,
                "--portfolio", port_path,
                "--market", "IN",
                "--trading-style", "intraday",
                "--now", "2026-10-19 09:55:00",
                "--write", orders_path,
                "--json",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))
            sized = json.loads(out)
            first_order = sized["orders"][0]
            self.assertEqual(first_order["product"], "MIS")
            self.assertEqual(first_order["order_class"], "bracket")
            self.assertEqual(first_order["data_tier"], "FIXTURE")

            risk_path = os.path.join(tmp, "risk.json")
            broker_state = os.path.join(tmp, "broker.json")
            code, out, err = run_cli(risk_mod, [
                "--orders", orders_path,
                "--config", cfg_path,
                "--portfolio", port_path,
                "--state", broker_state,
                "--out", risk_path,
                "--market", "IN",
                "--now", "2026-10-19 09:55:00",
                "--json",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))
            risk_res = json.loads(out)
            self.assertEqual(risk_res["pass"], 1)

            code, out, err = run_cli(ticket_mod, [
                "show",
                "--orders", orders_path,
                "--config", cfg_path,
                "--risk", risk_path,
                "--state", broker_state,
                "--market", "IN",
                "--now", "2026-10-19 09:55:00",
                "--json",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))
            tickets = json.loads(out)
            self.assertTrue(tickets[0]["ready"])
            self.assertEqual(tickets[0]["data_tier"], "FIXTURE")
            self.assertTrue(tickets[0]["is_historical_analysis"])
            self.assertFalse(tickets[0]["is_live_signal"])

            code, out, err = run_cli(ticket_mod, [
                "approve",
                "--index", "1",
                "--reply", "yes paper buy",
                "--orders", orders_path,
                "--config", cfg_path,
                "--risk", risk_path,
                "--state", broker_state,
                "--market", "IN",
                "--now", "2026-10-19 09:55:00",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))

            code, out, err = run_cli(ticket_mod, [
                "result",
                "--index", "1",
                "--status", "placed",
                "--filled-qty", "3",
                "--filled-price", "3510.00",
                "--broker-id", "paper-tcs-1",
                "--orders", orders_path,
                "--config", cfg_path,
                "--risk", risk_path,
                "--state", broker_state,
                "--portfolio", port_path,
                "--market", "IN",
                "--now", "2026-10-19 09:56:00",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))

            # Duplicate placement result for same approved order must fail
            code_dup, _, _ = run_cli(ticket_mod, [
                "result",
                "--index", "1",
                "--status", "placed",
                "--filled-qty", "3",
                "--filled-price", "3510.00",
                "--orders", orders_path,
                "--config", cfg_path,
                "--risk", risk_path,
                "--state", broker_state,
                "--portfolio", port_path,
                "--market", "IN",
                "--now", "2026-10-19 09:56:30",
            ])
            self.assertEqual(code_dup, 1)

            with open(port_path, "r", encoding="utf-8") as fh:
                port_after_buy = json.load(fh)
            self.assertAlmostEqual(port_after_buy["cash"], 100000.0 - 3 * 3510.0, places=2)
            self.assertEqual(port_after_buy["positions"][0]["shares"], 3)

            flat_orders = os.path.join(tmp, "flatten_orders.json")
            code, out, err = run_cli(ticket_mod, [
                "flatten",
                "--symbol", "TCS",
                "--orders", flat_orders,
                "--portfolio", port_path,
                "--plan", plan_path,
                "--config", cfg_path,
                "--market", "IN",
                "--now", "2026-10-19 15:18:00",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))

            code, out, err = run_cli(risk_mod, [
                "--orders", flat_orders,
                "--config", cfg_path,
                "--portfolio", port_path,
                "--state", broker_state,
                "--out", risk_path,
                "--market", "IN",
                "--now", "2026-10-19 15:18:00",
                "--json",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))

            code, out, err = run_cli(ticket_mod, [
                "approve",
                "--index", "1",
                "--reply", "yes flatten TCS",
                "--orders", flat_orders,
                "--config", cfg_path,
                "--risk", risk_path,
                "--state", broker_state,
                "--market", "IN",
                "--now", "2026-10-19 15:18:00",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))

            code, out, err = run_cli(ticket_mod, [
                "result",
                "--index", "1",
                "--status", "placed",
                "--filled-qty", "3",
                "--filled-price", "3550.00",
                "--broker-id", "paper-tcs-flat-1",
                "--orders", flat_orders,
                "--config", cfg_path,
                "--risk", risk_path,
                "--state", broker_state,
                "--portfolio", port_path,
                "--market", "IN",
                "--now", "2026-10-19 15:18:10",
            ])
            self.assertEqual(code, 0, msg=(out + "\n" + err))

            with open(port_path, "r", encoding="utf-8") as fh:
                port_after_flat = json.load(fh)
            self.assertAlmostEqual(port_after_flat["cash"], 100120.0, places=2)
            self.assertEqual(port_after_flat["positions"], [])

    def test_upstox_cdn_spaced_currency_symbols_and_bse_scrip_codes(self):
        """Upstox CDN parsing skips/preserves spaced F&O/currency names and indexes BSE scrip codes."""
        nse_rows = [
            {
                "segment": "NCD_FO",
                "exchange": "NSE",
                "instrument_key": "NCD_FO|14222",
                "trading_symbol": "USDINR 101.25 PE 13 NOV 26",
                "instrument_type": "PE",
            },
            {
                "segment": "NSE_EQ",
                "exchange": "NSE",
                "instrument_key": "NSE_EQ|INE002A01018",
                "trading_symbol": "RELIANCE",
                "isin": "INE002A01018",
                "exchange_token": "2885",
                "instrument_type": "EQ",
            },
        ]
        bse_rows = [
            {
                "segment": "BFO",
                "exchange": "BSE",
                "instrument_key": "BSE_FO|88888",
                "trading_symbol": "SENSEX 71700 CE 26 NOV 26",
                "instrument_type": "CE",
            },
            {
                "segment": "BSE_EQ",
                "exchange": "BSE",
                "instrument_key": "BSE_EQ|INE002A01018",
                "trading_symbol": "RELIANCE",
                "isin": "INE002A01018",
                "exchange_token": "500325",
                "instrument_type": "A",
            },
        ]

        def fake_http(url, headers=None, timeout=15):
            if "NSE.json.gz" in url:
                return 200, gzip.compress(json.dumps(nse_rows).encode("utf-8"))
            if "BSE.json.gz" in url:
                return 200, gzip.compress(json.dumps(bse_rows).encode("utf-8"))
            if "/v3/market-quote/quotes" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "NSE_EQ:RELIANCE": {
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "last_price": 1335.4,
                            "volume": 1000,
                            "ohlc": {"open": 1330.0, "high": 1340.0, "low": 1325.0, "close": 1324.4},
                            "timestamp": "2026-10-19T15:25:00+05:30",
                        }
                    },
                })
            raise AssertionError("Unexpected URL: %s" % url)

        prov = data_in.UpstoxMarketDataProvider(analytics_token="tok-test-1234", http_get=fake_http)
        self.assertEqual(prov.resolve_instrument_key("RELIANCE", exchange="NSE")[0], "NSE_EQ|INE002A01018")
        self.assertEqual(prov.resolve_instrument_key("500325", exchange="BSE")[0], "BSE_EQ|INE002A01018")
        inst_bse = prov.get_instruments(exchange="BSE", symbol="500325")
        self.assertEqual(inst_bse["count"], 1)
        self.assertEqual(inst_bse["instruments"][0]["trading_symbol"], "RELIANCE")

        quotes = prov.get_quotes(["RELIANCE", "NOPEQTY"], exchange="NSE")
        self.assertEqual(quotes["RELIANCE"]["price"], 1335.4)
        self.assertIn("error", quotes["NOPEQTY"])
        self.assertEqual(quotes["NOPEQTY"]["data_status"], data_in.DataStatus.UNAVAILABLE)

    def test_upstox_daily_bars_post_close_intraday_daily_candle(self):
        """After market close, get_daily_bars fetches today's completed bar from intraday/days/1 if needed."""
        def fake_http(url, headers=None, timeout=15):
            if "/v3/historical-candle/intraday/" in url and "/days/1" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "candles": [
                            ["2026-10-19T00:00:00+05:30", 1320.0, 1340.0, 1315.0, 1335.4, 500000, 0],
                        ]
                    },
                })
            if "/v3/historical-candle/" in url and "/days/1/" in url:
                return 200, json.dumps({
                    "status": "success",
                    "data": {
                        "candles": [
                            ["2026-10-16T00:00:00+05:30", 1300.0, 1325.0, 1298.0, 1324.4, 400000, 0],
                        ]
                    },
                })
            raise AssertionError("Unexpected URL: %s" % url)

        tz_ist = data_in.calendar_in.IST
        open_dt = datetime.datetime(2026, 10, 19, 11, 0, 0, tzinfo=tz_ist)
        prov_open = data_in.UpstoxMarketDataProvider(
            analytics_token="tok-test-1234",
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=fake_http,
            now_fn=lambda: open_dt,
        )
        bars_open = prov_open.get_daily_bars("RELIANCE", range_str="1mo", now=open_dt)["bars"]
        self.assertEqual([b["date"] for b in bars_open], ["2026-10-16"])

        close_dt = datetime.datetime(2026, 10, 19, 16, 0, 0, tzinfo=tz_ist)
        prov_closed = data_in.UpstoxMarketDataProvider(
            analytics_token="tok-test-1234",
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018"}},
            http_get=fake_http,
            now_fn=lambda: close_dt,
        )
        bars_closed = prov_closed.get_daily_bars("RELIANCE", range_str="1mo", now=close_dt)["bars"]
        self.assertEqual([b["date"] for b in bars_closed], ["2026-10-16", "2026-10-19"])
        self.assertEqual(bars_closed[-1]["c"], 1335.4)

    def test_scan_fetch_in_provider_series_adapts_upstox_dict(self):
        """scan._fetch_in_provider_series adapts single-dict get_batch_series returns to (data, failed)."""
        def fake_http(url, headers=None, timeout=15):
            if "INE467B01029" in url:
                return 404, json.dumps({"status": "error", "errors": [{"errorCode": "UDAPI100011"}]})
            return 200, json.dumps({
                "status": "success",
                "data": {
                    "candles": [
                        ["2026-10-16T00:00:00+05:30", 1310.0, 1326.0, 1308.0, 1324.4, 350000, 0],
                    ]
                },
            })

        tz_ist = data_in.calendar_in.IST
        now_dt = datetime.datetime(2026, 10, 16, 16, 0, 0, tzinfo=tz_ist)
        prov = data_in.UpstoxMarketDataProvider(
            analytics_token="tok-test-1234",
            config={"upstox_instruments": {"RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|INE467B01029"}},
            http_get=fake_http,
            now_fn=lambda: now_dt,
        )
        data_map, failed = scan_mod._fetch_in_provider_series(
            prov, ["RELIANCE", "TCS"], "1mo", "1d", 0.0, exchange="NSE", now=now_dt
        )
        self.assertIn("RELIANCE", data_map)
        self.assertNotIn("TCS", data_map)
        self.assertIn("TCS", failed)


if __name__ == "__main__":
    unittest.main()
