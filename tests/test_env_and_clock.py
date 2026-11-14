import datetime
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import FIX, ROOT, load

env_loader = load("trader-alerts", "env_loader")
data_in = load("trader-alerts", "data_in")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")
size_mod = load("trader-budget", "size")
risk_mod = load("trader-risk", "check")
ticket_mod = load("trader-broker", "ticket")

RELIANCE_5M = os.path.join(FIX, "bharat", "RELIANCE-5m.json")


def _run_cli(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestSafeDotenvLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_dotenv_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_dotenv_quotes_escapes_comments_and_no_interpolation_or_shell_exec(self):
        marker = os.path.join(self.tmp, "pwned_marker")
        raw = "\ufeff# Top comment\r\n" \
              "  \r\n" \
              "PLAIN=value123\r\n" \
              "WITH_EQUALS=a=b=c==\n" \
              "HASH_TIGHT=abc#123\n" \
              "INLINE_COMMENT=clean_val # trailing comment\n" \
              "DOUBLE_Q=\"hello \\\"world\\\"\\nline2\\t$HOME ${PATH}\"\n" \
              "SINGLE_Q='literal \\\\n $HOME $(touch %s) `id` # not comment'\n" \
              "CMD_SUB=$(touch %s)\n" \
              "BACKTICK=`touch %s`\n" \
              "EMPTY=\n" \
              "export EXPORTED='exported_val'\n" \
              "INVALID LINE WITHOUT EQUALS\n" \
              "123INVALID=skip\n" \
              "UNCLOSED=\"oops\n" % (marker, marker, marker)

        parsed = env_loader.parse_dotenv(raw)
        self.assertEqual(parsed["PLAIN"], "value123")
        self.assertEqual(parsed["WITH_EQUALS"], "a=b=c==")
        self.assertEqual(parsed["HASH_TIGHT"], "abc#123")
        self.assertEqual(parsed["INLINE_COMMENT"], "clean_val")
        self.assertEqual(parsed["DOUBLE_Q"], 'hello "world"\nline2\t$HOME ${PATH}')
        self.assertIn("$(touch ", parsed["SINGLE_Q"])
        self.assertIn("$HOME", parsed["SINGLE_Q"])
        self.assertTrue(parsed["CMD_SUB"].startswith("$(touch "))
        self.assertTrue(parsed["BACKTICK"].startswith("`touch "))
        self.assertEqual(parsed["EMPTY"], "")
        self.assertEqual(parsed["EXPORTED"], "exported_val")
        self.assertNotIn("123INVALID", parsed)
        self.assertNotIn("UNCLOSED", parsed)
        self.assertFalse(os.path.exists(marker))

    def test_project_root_precedence_over_cwd_and_env_overrides_dotenv(self):
        proj = os.path.join(self.tmp, "proj")
        other_cwd = os.path.join(self.tmp, "other_cwd")
        os.makedirs(proj)
        os.makedirs(other_cwd)

        secret_tok = "tok_super_secret_do_not_print_987654"
        with open(os.path.join(proj, ".env"), "w", encoding="utf-8") as fh:
            fh.write("UPSTOX_ANALYTICS_TOKEN=\"%s\"\n" % secret_tok)
            fh.write("INDIA_MARKET_DATA_PROVIDER=upstox\n")
            fh.write("EXISTING_KEY=from_dotenv\n")
            fh.write("EMPTY_ENV_KEY=from_dotenv\n")
        with open(os.path.join(other_cwd, ".env"), "w", encoding="utf-8") as fh:
            fh.write("UPSTOX_ANALYTICS_TOKEN=wrong_cwd_token\n")
            fh.write("ONLY_IN_CWD=should_not_load\n")

        # Test that loading relative to proj ignores other_cwd even when cwd == other_cwd
        fake_env = {"EXISTING_KEY": "from_process_env", "EMPTY_ENV_KEY": ""}
        old_cwd = os.getcwd()
        out, err = io.StringIO(), io.StringIO()
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            os.chdir(other_cwd)
            with redirect_stdout(out), redirect_stderr(err):
                loaded_keys = env_loader.load_project_dotenv(project_root=proj, environ=fake_env)
        finally:
            os.chdir(old_cwd)
            root_logger.removeHandler(handler)

        self.assertEqual(fake_env["UPSTOX_ANALYTICS_TOKEN"], secret_tok)
        self.assertEqual(fake_env["INDIA_MARKET_DATA_PROVIDER"], "upstox")
        # Existing env vars (even empty string) MUST override .env
        self.assertEqual(fake_env["EXISTING_KEY"], "from_process_env")
        self.assertEqual(fake_env["EMPTY_ENV_KEY"], "")
        self.assertNotIn("ONLY_IN_CWD", fake_env)
        self.assertIn("UPSTOX_ANALYTICS_TOKEN", loaded_keys)
        self.assertNotIn("EXISTING_KEY", loaded_keys)
        # Never print or log secrets
        combined_io = out.getvalue() + err.getvalue() + log_buf.getvalue()
        self.assertNotIn(secret_tok, combined_io)

    def test_yahoo_default_without_credentials_and_upstox_explicit_error_when_missing(self):
        missing_root = os.path.join(self.tmp, "no_dotenv_here")
        os.makedirs(missing_root)
        env = {}
        loaded = env_loader.load_project_dotenv(project_root=missing_root, environ=env)
        self.assertEqual(loaded, ())

        clean_env = {
            "INDIA_MARKET_DATA_PROVIDER": "",
            "BHARAT_DATA_PROVIDER": "",
            "UPSTOX_ANALYTICS_TOKEN": "",
        }
        with patch.dict(os.environ, clean_env):
            prov = data_in.get_provider()
            self.assertEqual(prov.provider_name, "yahoo")
            self.assertIsInstance(prov, data_in.YahooMarketDataProvider)

            with patch.dict(os.environ, {"INDIA_MARKET_DATA_PROVIDER": "upstox", "UPSTOX_ANALYTICS_TOKEN": ""}):
                with self.assertRaises(data_in.AuthenticationConfigError) as ctx:
                    data_in.get_provider()
                self.assertIn("UPSTOX_ANALYTICS_TOKEN", str(ctx.exception))

    def test_subprocess_cli_env_overrides_dotenv_independent_of_cwd(self):
        # Build a minimal sandboxed mirror with trader-alerts so we can place a test .env without
        # touching the repository root or reading any developer .env file.
        mini_root = os.path.join(self.tmp, "mini_repo")
        mini_alerts = os.path.join(mini_root, "trader-alerts")
        outside_dir = os.path.join(self.tmp, "outside_cwd")
        os.makedirs(mini_alerts)
        os.makedirs(outside_dir)
        for fname in ("env_loader.py", "data_in.py", "symbols_in.py", "calendar_in.py", "instruments.py", "upstox_feed.py"):
            shutil.copy2(os.path.join(ROOT, "trader-alerts", fname), os.path.join(mini_alerts, fname))

        dotenv_tok = "dotenv_test_token_alpha_111"
        with open(os.path.join(mini_root, ".env"), "w", encoding="utf-8") as fh:
            fh.write("INDIA_MARKET_DATA_PROVIDER=upstox\n")
            fh.write('UPSTOX_ANALYTICS_TOKEN="%s"\n' % dotenv_tok)
        with open(os.path.join(outside_dir, ".env"), "w", encoding="utf-8") as fh:
            fh.write("INDIA_MARKET_DATA_PROVIDER=offline\n")
            fh.write("UPSTOX_ANALYTICS_TOKEN=outside_token_must_be_ignored\n")

        probe = (
            "import json, os, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import data_in; "
            "print(json.dumps({"
            "'provider': data_in.resolve_provider_name(), "
            "'tok': os.environ.get('UPSTOX_ANALYTICS_TOKEN', ''), "
            "'cls': data_in.get_provider().provider_name"
            "}))"
        )
        base_env = {k: v for k, v in os.environ.items() if k not in (
            "INDIA_MARKET_DATA_PROVIDER", "BHARAT_DATA_PROVIDER", "UPSTOX_ANALYTICS_TOKEN"
        )}

        # Case 1: no env vars set -> picks up mini_repo/.env despite running from outside_dir
        r1 = subprocess.run(
            [sys.executable, "-c", probe, mini_alerts],
            cwd=outside_dir, env=base_env, capture_output=True, text=True, check=True,
        )
        d1 = json.loads(r1.stdout)
        self.assertEqual(d1["provider"], "upstox")
        self.assertEqual(d1["tok"], dotenv_tok)
        self.assertEqual(d1["cls"], "upstox")

        # Case 2: exported env var overrides .env (including switching back to yahoo)
        override_env = dict(base_env)
        override_env["INDIA_MARKET_DATA_PROVIDER"] = "yahoo"
        override_env["UPSTOX_ANALYTICS_TOKEN"] = "exported_env_override_tok"
        r2 = subprocess.run(
            [sys.executable, "-c", probe, mini_alerts],
            cwd=outside_dir, env=override_env, capture_output=True, text=True, check=True,
        )
        d2 = json.loads(r2.stdout)
        self.assertEqual(d2["provider"], "yahoo")
        self.assertEqual(d2["tok"], "exported_env_override_tok")
        self.assertEqual(d2["cls"], "yahoo")

    def test_packaged_dist_resolves_enclosing_repo_dotenv_while_standalone_stays_default(self):
        mini_repo = os.path.join(self.tmp, "repo")
        repo_alerts = os.path.join(mini_repo, "trader-alerts")
        pkg_root = os.path.join(mini_repo, "dist", "claude-chat", "day-trader-skill-chat")
        pkg_alerts = os.path.join(pkg_root, "trader-alerts")
        standalone_parent = os.path.join(self.tmp, "standalone_parent")
        standalone_alerts = os.path.join(standalone_parent, "day-trader-skill-chat", "trader-alerts")
        outside_cwd = os.path.join(self.tmp, "outside_cwd")
        for d in (repo_alerts, pkg_alerts, standalone_alerts, outside_cwd):
            os.makedirs(d)

        for fname in ("env_loader.py", "data_in.py", "symbols_in.py", "calendar_in.py", "instruments.py", "upstox_feed.py"):
            shutil.copy2(os.path.join(ROOT, "trader-alerts", fname), os.path.join(repo_alerts, fname))
            shutil.copy2(os.path.join(ROOT, "trader-alerts", fname), os.path.join(pkg_alerts, fname))
            shutil.copy2(os.path.join(ROOT, "trader-alerts", fname), os.path.join(standalone_alerts, fname))

        repo_tok = "dotenv_pkg_tok_222"
        with open(os.path.join(mini_repo, ".env"), "w", encoding="utf-8") as fh:
            fh.write("INDIA_MARKET_DATA_PROVIDER=upstox\n")
            fh.write("UPSTOX_ANALYTICS_TOKEN=%s\n" % repo_tok)
        with open(os.path.join(outside_cwd, ".env"), "w", encoding="utf-8") as fh:
            fh.write("INDIA_MARKET_DATA_PROVIDER=offline\n")
            fh.write("UPSTOX_ANALYTICS_TOKEN=outside_cwd_decoy\n")
        with open(os.path.join(standalone_parent, ".env"), "w", encoding="utf-8") as fh:
            fh.write("INDIA_MARKET_DATA_PROVIDER=upstox\n")
            fh.write("UPSTOX_ANALYTICS_TOKEN=arbitrary_parent_decoy\n")

        probe = (
            "import json, os, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import data_in; "
            "print(json.dumps({"
            "'provider': data_in.resolve_provider_name(), "
            "'has_tok': bool(os.environ.get('UPSTOX_ANALYTICS_TOKEN')), "
            "'tok': os.environ.get('UPSTOX_ANALYTICS_TOKEN', ''), "
            "'cls': data_in.get_provider().provider_name"
            "}))"
        )
        base_env = {k: v for k, v in os.environ.items() if k not in (
            "INDIA_MARKET_DATA_PROVIDER", "BHARAT_DATA_PROVIDER", "UPSTOX_ANALYTICS_TOKEN"
        )}

        # In-repo dist/ package loads enclosing mini_repo/.env and ignores outside_cwd/.env
        r_pkg = subprocess.run(
            [sys.executable, "-c", probe, pkg_alerts],
            cwd=outside_cwd, env=base_env, capture_output=True, text=True, check=True,
        )
        d_pkg = json.loads(r_pkg.stdout)
        self.assertEqual(d_pkg["provider"], "upstox")
        self.assertEqual(d_pkg["tok"], repo_tok)
        self.assertEqual(d_pkg["cls"], "upstox")

        # Exported shell env still overrides enclosing repo .env
        override_env = dict(base_env)
        override_env["INDIA_MARKET_DATA_PROVIDER"] = "yahoo"
        r_ov = subprocess.run(
            [sys.executable, "-c", probe, pkg_alerts],
            cwd=outside_cwd, env=override_env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(r_ov.stdout)["cls"], "yahoo")

        # Standalone extraction never searches arbitrary parents and stays default yahoo
        r_solo = subprocess.run(
            [sys.executable, "-c", probe, standalone_alerts],
            cwd=outside_cwd, env=base_env, capture_output=True, text=True, check=True,
        )
        d_solo = json.loads(r_solo.stdout)
        self.assertEqual(d_solo["provider"], "yahoo")
        self.assertFalse(d_solo["has_tok"])
        self.assertEqual(d_solo["cls"], "yahoo")


class TestNowClockOverrideSemantics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_now_clock_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_now_on_live_capable_provider_downgrades_actionability_across_pipeline(self):
        with open(RELIANCE_5M, encoding="utf-8") as fh:
            parsed = entry_mod.parse_chart(fh.read(), "RELIANCE")
        full_bars = parsed["bars"]

        class LiveMock(data_in.MarketDataProvider):
            provider_name = "mock_upstox_live"
            CAPABILITIES = {
                data_in.CAPABILITY_HISTORICAL_CANDLES: True,
                data_in.CAPABILITY_INTRADAY_CANDLES: True,
                data_in.CAPABILITY_LIVE_QUOTES: True,
                data_in.CAPABILITY_MARKET_STATUS: True,
            }

            def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
                now_ts = int(now.timestamp()) if now is not None else entry_mod.ts_of(entry_mod.parse_now("2026-10-19 10:35", market="IN"))
                bars = [b for b in full_bars if b["t"] + 300 <= now_ts]
                ts = now_ts - 15
                return data_in._standardize_payload(
                    {
                        "symbol": "RELIANCE",
                        "exchange": exchange,
                        "interval": interval,
                        "bars": bars,
                        "prev_close": parsed.get("prev_close"),
                        "meta": {"symbol": "RELIANCE.NS", "dataGranularity": interval, "regularMarketTime": ts},
                    },
                    provider_name=self.provider_name,
                    symbol="RELIANCE",
                    exchange=exchange,
                    data_status=data_in.DataStatus.CURRENT,
                    tier=data_in.DataTier.REAL_TIME,
                    timestamp=ts,
                    is_live=True,
                    is_simulated=False,
                    now=now,
                )

        data_in.register_provider("mock_upstox_live", LiveMock)
        now_open = entry_mod.parse_now("2026-10-19 10:35", market="IN")
        try:
            with patch.dict(os.environ, {"INDIA_MARKET_DATA_PROVIDER": "mock_upstox_live"}):
                # A. Real open-market clock (no --now): genuine LIVE actionable signal
                with patch.object(entry_mod, "utc_now", return_value=now_open):
                    code_live, txt_live, _ = _run_cli(entry_mod, ["check", "RELIANCE", "--market", "IN"])
                    _, j_live, _ = _run_cli(entry_mod, ["check", "RELIANCE", "--market", "IN", "--json"])
                self.assertEqual(code_live, 0)
                self.assertIn("This is the moment to get in", txt_live)
                doc_live = json.loads(j_live)
                self.assertTrue(doc_live["is_live_signal"])
                self.assertEqual(doc_live["data_tier"], "CURRENT")
                self.assertFalse(doc_live.get("clock_override", False))

                # B. Same provider with --now clock override -> NOT actionable as live, full paper pipeline works
                entry_p = os.path.join(self.tmp, "entry.json")
                plan_p = os.path.join(self.tmp, "plan.json")
                orders_p = os.path.join(self.tmp, "orders.json")
                risk_p = os.path.join(self.tmp, "risk.json")
                state_p = os.path.join(self.tmp, "broker.json")
                cfg_p = os.path.join(self.tmp, "config.json")
                cfg = {
                    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75,
                    "max_order_inr": 75000, "min_order_inr": 100, "max_loss_per_trade_inr": 2000,
                    "max_daily_loss_inr": 5000, "max_trades_per_day": 3, "max_orders_per_day": 10,
                    "min_reward_risk": 1.0, "require_stop": True, "no_entries_before": "09:30",
                    "no_entries_after": "15:00", "paper": True, "market": "IN", "currency": "INR", "exchange": "NSE",
                }
                with open(cfg_p, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh)

                with patch.object(entry_mod, "utc_now", return_value=now_open):
                    code_now, txt_now, _ = _run_cli(entry_mod, [
                        "check", "RELIANCE", "--market", "IN", "--now", "2026-10-19 10:35", "--write", entry_p,
                    ])
                self.assertEqual(code_now, 0)
                self.assertNotIn("This is the moment to get in", txt_now)
                self.assertIn("NOT a current live entry signal", txt_now)

                with open(entry_p, encoding="utf-8") as fh:
                    edoc = json.load(fh)
                self.assertTrue(edoc["clock_override"])
                self.assertFalse(edoc["is_live_signal"])
                self.assertFalse(edoc["is_live_data"])
                self.assertTrue(edoc["is_historical_analysis"])
                self.assertEqual(edoc["data_tier"], "HISTORICAL")

                self.assertEqual(_run_cli(exit_mod, [
                    "plan", "--from", entry_p, "--target", "4R", "--market", "IN", "--config", cfg_p, "--write", plan_p,
                ])[0], 0)
                self.assertEqual(_run_cli(size_mod, [
                    "--plan", plan_p, "--market", "IN", "--config", cfg_p, "--now", "2026-10-19 10:35", "--write", orders_p,
                ])[0], 0)
                self.assertEqual(_run_cli(risk_mod, [
                    "--orders", orders_p, "--config", cfg_p, "--market", "IN", "--now", "2026-10-19 10:35", "--out", risk_p,
                ])[0], 0)
                with open(risk_p, encoding="utf-8") as fh:
                    rdoc = json.load(fh)
                self.assertEqual(rdoc["results"][0]["verdict"], "PASS")
                self.assertTrue(rdoc["results"][0]["clock_override"])

                # Non-paper config with clock override MUST be blocked
                live_cfg = dict(cfg, paper=False)
                with open(orders_p, encoding="utf-8") as fh:
                    orders_list = json.load(fh)["orders"]
                live_risk = risk_mod.check(
                    orders_list,
                    live_cfg, now=now_open, market="IN", simulated=True,
                )
                self.assertEqual(live_risk["results"][0]["verdict"], "BLOCK")

                code_t, out_tj, err_tj = _run_cli(ticket_mod, [
                    "show", "--orders", orders_p, "--risk", risk_p, "--config", cfg_p, "--state", state_p,
                    "--market", "IN", "--now", "2026-10-19 10:35", "--json",
                ])
                self.assertEqual(code_t, 0, out_tj + err_tj)
                tj = json.loads(out_tj)[0]
                self.assertTrue(tj["ready"])
                self.assertEqual(tj["data_tier"], "HISTORICAL")
                self.assertIs(tj["is_live_signal"], False)
                self.assertIs(tj["is_historical_analysis"], True)
                self.assertIs(tj["clock_override"], True)
                self.assertEqual(tj["signal_classification"], "HISTORICAL")
                self.assertIn("NOT A CURRENT ENTRY RECOMMENDATION", tj["signal_warning"])
        finally:
            data_in.unregister_provider("mock_upstox_live")

    def test_same_day_post_close_ticket_downgrades_live_signal(self):
        order = {
            "symbol": "RELIANCE",
            "side": "buy",
            "qty": "5",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": "1370.00"},
            "stop_loss": {"stop_price": "1340.00"},
            "market": "IN",
            "currency": "INR",
            "trading_style": "intraday",
            "provider": "upstox",
            "data_tier": "CURRENT",
            "is_historical_analysis": False,
            "is_live_signal": True,
            "is_live_data": True,
            "is_market_open": True,
            "last_trading_session": "2026-10-19",
            "last_data_timestamp": "2026-10-19T11:00:00+05:30",
        }
        closed_utc = ticket_mod.parse_now("2026-10-19 19:00", market="IN")
        prov = ticket_mod.signal_provenance(order, today="2026-10-19", now=closed_utc, market="IN")
        self.assertEqual(prov["signal_classification"], "HISTORICAL")
        self.assertEqual(prov["data_tier"], "HISTORICAL")
        self.assertIs(prov["is_live_signal"], False)
        self.assertIs(prov["is_historical_analysis"], True)
        self.assertIn("NOT A CURRENT ENTRY RECOMMENDATION", prov["signal_warning"])

