"""Unit tests for BharatTrader AI Indian market time, session and calendar engine."""

import datetime
import os
import sys
import unittest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _load import load

cal = load("trader-alerts", "calendar_in")


class TestBharatTime(unittest.TestCase):
    def test_ist_timezone_and_now(self):
        now = cal.now_ist()
        self.assertIsNotNone(now.tzinfo)
        # Offset must be exactly +05:30 (19800 seconds)
        offset = now.utcoffset()
        self.assertEqual(offset.total_seconds(), 5.5 * 3600)
        self.assertEqual(now.tzname(), "IST")

    def test_parse_ist_formats(self):
        dt1 = cal.parse_ist("2026-10-19")
        self.assertEqual(dt1.year, 2026)
        self.assertEqual(dt1.month, 10)
        self.assertEqual(dt1.day, 19)
        self.assertEqual(dt1.hour, 0)
        self.assertEqual(dt1.tzinfo.utcoffset(dt1).total_seconds(), 19800)

        dt2 = cal.parse_ist("2026-10-19 09:15:00")
        self.assertEqual(dt2.hour, 9)
        self.assertEqual(dt2.minute, 15)

        dt3 = cal.parse_ist("2026-10-19T03:45:00Z")  # UTC 03:45 -> IST 09:15
        self.assertEqual(dt3.hour, 9)
        self.assertEqual(dt3.minute, 15)

    def test_verified_holidays_2025_and_2026(self):
        # Republic Day 2026 (Monday)
        is_trade, status, reason = cal.is_trading_day_in("2026-01-26")
        self.assertFalse(is_trade)
        self.assertEqual(status, cal.CalendarStatus.VERIFIED)
        self.assertIn("Republic Day", reason)

        # Mahashivratri 2026 (Monday)
        is_trade, status, reason = cal.is_trading_day_in("2026-02-16")
        self.assertFalse(is_trade)
        self.assertIn("Mahashivratri", reason)

        # Independence Day 2025 (Friday)
        is_trade, status, reason = cal.is_trading_day_in("2025-08-15")
        self.assertFalse(is_trade)
        self.assertIn("Independence Day", reason)

        # Normal Wednesday in 2026
        is_trade, status, reason = cal.is_trading_day_in("2026-01-28")
        self.assertTrue(is_trade)
        self.assertEqual(status, cal.CalendarStatus.VERIFIED)
        self.assertEqual(reason, "regular_session")

    def test_weekends_are_not_trading_days(self):
        # 2026-10-17 is Saturday, 2026-10-18 is Sunday
        is_sat, _, reason_sat = cal.is_trading_day_in("2026-10-17")
        self.assertFalse(is_sat)
        self.assertEqual(reason_sat, "weekend")

        is_sun, _, reason_sun = cal.is_trading_day_in("2026-10-18")
        self.assertFalse(is_sun)
        self.assertEqual(reason_sun, "weekend")

    def test_future_unannounced_year_is_provisional(self):
        # 2027 has no gazetted holiday circular yet
        holidays, status = cal.get_holiday_calendar(2027)
        self.assertEqual(status, cal.CalendarStatus.PROVISIONAL)
        self.assertEqual(len(holidays), 0)

        # Tuesday in 2027 returns provisional trading status
        is_trade, status, _ = cal.is_trading_day_in("2027-04-06")
        self.assertTrue(is_trade)
        self.assertEqual(status, cal.CalendarStatus.PROVISIONAL)

    def test_muhurat_trading_special_session_and_bars(self):
        # 2026-11-08 is Diwali Muhurat Trading (Sunday evening)
        is_trade, status, reason = cal.is_trading_day_in("2026-11-08")
        self.assertTrue(is_trade)
        self.assertEqual(status, cal.CalendarStatus.VERIFIED)
        self.assertEqual(reason, "special_session:MUHURAT")

        # Dynamic bar count for 1-hour Muhurat session must be exactly 12 bars (60 min / 5 min)
        bars_muhurat_2026 = cal.get_session_bars("2026-11-08")
        self.assertEqual(bars_muhurat_2026, 12)

        bars_muhurat_2025 = cal.get_session_bars("2025-10-21")
        self.assertEqual(bars_muhurat_2025, 12)

        # Standard trading day must be exactly 75 bars
        bars_regular = cal.get_session_bars("2026-10-19")
        self.assertEqual(bars_regular, 75)

    def test_granular_session_phases(self):
        # Regular trading day: 2026-10-19 (Monday)
        # 08:45 IST: before pre-open
        phase, _ = cal.get_session_phase("2026-10-19 08:45:00")
        self.assertEqual(phase, "closed")

        # 09:04 IST: pre-open order entry
        phase, _ = cal.get_session_phase("2026-10-19 09:04:00")
        self.assertEqual(phase, "pre_open_entry")

        # 09:10 IST: pre-open order matching
        phase, _ = cal.get_session_phase("2026-10-19 09:10:00")
        self.assertEqual(phase, "order_matching")

        # 09:13 IST: pre-market buffer
        phase, _ = cal.get_session_phase("2026-10-19 09:13:00")
        self.assertEqual(phase, "buffer")

        # 09:20 IST: regular continuous trading
        phase, _ = cal.get_session_phase("2026-10-19 09:20:00")
        self.assertEqual(phase, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-10-19 09:20:00"))

        # 14:00 IST: regular continuous trading
        phase, _ = cal.get_session_phase("2026-10-19 14:00:00")
        self.assertEqual(phase, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-10-19 14:00:00"))

        # 15:25 IST: continuous regular trading active for non-CAS securities
        phase, _ = cal.get_session_phase("2026-10-19 15:25:00")
        self.assertEqual(phase, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-10-19 15:25:00"))

        # 15:22 IST: CAS market & limit order entry window
        phase_cas_all, _ = cal.get_session_phase("2026-10-19 15:22:00", is_cas_eligible=True)
        self.assertEqual(phase_cas_all, "cas_order_entry_all")
        self.assertFalse(cal.is_market_hours_in("2026-10-19 15:22:00", is_cas_eligible=True))

        # 15:26 IST: CAS limit orders only window
        phase_cas_lim, _ = cal.get_session_phase("2026-10-19 15:26:00", is_cas_eligible=True)
        self.assertEqual(phase_cas_lim, "cas_order_entry_limit_only")
        self.assertFalse(cal.is_market_hours_in("2026-10-19 15:26:00", is_cas_eligible=True))

        # 15:35 IST: closing price computation buffer
        phase, _ = cal.get_session_phase("2026-10-19 15:35:00")
        self.assertEqual(phase, "closing_buffer")
        self.assertFalse(cal.is_market_hours_in("2026-10-19 15:35:00"))

        # 15:50 IST: post-closing session
        phase, _ = cal.get_session_phase("2026-10-19 15:50:00")
        self.assertEqual(phase, "post_closing")
        self.assertFalse(cal.is_market_hours_in("2026-10-19 15:50:00"))

        # 16:30 IST: after hours closed
        phase, _ = cal.get_session_phase("2026-10-19 16:30:00")
        self.assertEqual(phase, "closed")
        self.assertFalse(cal.is_market_hours_in("2026-10-19 16:30:00"))

    def test_muhurat_session_phases(self):
        # 2026-11-08: Muhurat open 18:15, close 19:15, pre-open 18:00 - 18:08
        phase_early, _ = cal.get_session_phase("2026-11-08 17:30:00")
        self.assertEqual(phase_early, "closed")

        phase_po, _ = cal.get_session_phase("2026-11-08 18:05:00")
        self.assertEqual(phase_po, "pre_open_entry")

        phase_reg, _ = cal.get_session_phase("2026-11-08 18:30:00")
        self.assertEqual(phase_reg, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-11-08 18:30:00"))

        phase_late, _ = cal.get_session_phase("2026-11-08 20:00:00")
        self.assertEqual(phase_late, "closed")

    def test_cli_clock_indian_market(self):
        import io
        import json
        from contextlib import redirect_stdout
        alerts = load("trader-alerts", "alerts")

        # 1. Normal trading day during market hours
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2026-10-19 10:15", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["open"])
        self.assertEqual(data["market"], "NSE")
        self.assertEqual(data["phase"], "regular")
        self.assertEqual(data["session_bars"], 75)

        # 2. Closed on Republic Day
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2026-01-26 10:15", "--json"])
        self.assertEqual(code, 1)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["open"])
        self.assertIn("Republic Day", data["reason"])

        # 3. Special session (Diwali Muhurat Trading 2026-11-08 at 18:30 IST)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2026-11-08 18:30", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["open"])
        self.assertEqual(data["session_bars"], 12)
        self.assertTrue(data["special_session"])

    def test_non_cas_security_session_and_75_bars(self):
        # Non-CAS security on NSE (or default): 09:15 to 15:30 IST = 75 bars
        bars = cal.get_session_bars("2026-08-10", exchange="NSE", is_cas_eligible=False)
        self.assertEqual(bars, 75)

        # 15:25 IST is still active continuous trading for non-CAS securities
        phase, reason = cal.get_session_phase("2026-08-10 15:25:00", exchange="NSE", is_cas_eligible=False)
        self.assertEqual(phase, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-08-10 15:25:00", exchange="NSE", is_cas_eligible=False))

        # 15:35 IST is closing buffer
        phase, _ = cal.get_session_phase("2026-08-10 15:35:00", exchange="NSE", is_cas_eligible=False)
        self.assertEqual(phase, "closing_buffer")
        self.assertFalse(cal.is_market_hours_in("2026-08-10 15:35:00", exchange="NSE", is_cas_eligible=False))

        # 15:45 IST is post-closing
        phase, _ = cal.get_session_phase("2026-08-10 15:45:00", exchange="NSE", is_cas_eligible=False)
        self.assertEqual(phase, "post_closing")

    def test_cas_eligible_security_session_and_72_bars(self):
        # CAS-eligible security on NSE post-August 3, 2026: 09:15 to 15:15 IST = 72 bars
        bars = cal.get_session_bars("2026-08-10", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(bars, 72)

        # 15:10 IST: Continuous regular trading active
        phase, _ = cal.get_session_phase("2026-08-10 15:10:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-08-10 15:10:00", exchange="NSE", is_cas_eligible=True))

        # 15:17 IST: CAS reference price calculation & transition
        phase, _ = cal.get_session_phase("2026-08-10 15:17:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_transition")
        self.assertFalse(cal.is_market_hours_in("2026-08-10 15:17:00", exchange="NSE", is_cas_eligible=True))

        # 15:22 IST: CAS order entry for market and limit orders
        phase, _ = cal.get_session_phase("2026-08-10 15:22:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_order_entry_all")
        self.assertFalse(cal.is_market_hours_in("2026-08-10 15:22:00", exchange="NSE", is_cas_eligible=True))

        # 15:26 IST: CAS order entry for limit orders only
        phase, _ = cal.get_session_phase("2026-08-10 15:26:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_order_entry_limit_only")
        self.assertFalse(cal.is_market_hours_in("2026-08-10 15:26:00", exchange="NSE", is_cas_eligible=True))

        # 15:29 IST: CAS limit orders only with random closure active
        phase, reason = cal.get_session_phase("2026-08-10 15:29:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_order_entry_limit_only")
        self.assertIn("random_closure", reason)

        # 15:32 IST: CAS order matching
        phase, _ = cal.get_session_phase("2026-08-10 15:32:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_matching")
        self.assertFalse(cal.is_market_hours_in("2026-08-10 15:32:00", exchange="NSE", is_cas_eligible=True))

        # 15:40 IST: CAS post-auction transition buffer
        phase, _ = cal.get_session_phase("2026-08-10 15:40:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_buffer")

        # 15:55 IST: Post-closing session
        phase, _ = cal.get_session_phase("2026-08-10 15:55:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "post_closing")

    def test_cas_order_entry_permissions_and_random_closure(self):
        # 1. 15:22 IST: Market + Limit allowed
        perms_22 = cal.get_cas_order_permissions("2026-08-10 15:22:00", exchange="NSE", is_cas_eligible=True)
        self.assertTrue(perms_22["cas_active"])
        self.assertEqual(perms_22["phase"], "cas_order_entry_all")
        self.assertTrue(perms_22["allow_market_orders"])
        self.assertTrue(perms_22["allow_limit_orders"])
        self.assertTrue(perms_22["allow_modifications"])
        self.assertTrue(perms_22["allow_cancellations"])
        self.assertFalse(perms_22["random_closure_active"])

        # 2. 15:26 IST: Limit only allowed, random closure not yet active
        perms_26 = cal.get_cas_order_permissions("2026-08-10 15:26:00", exchange="NSE", is_cas_eligible=True)
        self.assertTrue(perms_26["cas_active"])
        self.assertEqual(perms_26["phase"], "cas_order_entry_limit_only")
        self.assertFalse(perms_26["allow_market_orders"])
        self.assertTrue(perms_26["allow_limit_orders"])
        self.assertTrue(perms_26["allow_modifications"])
        self.assertTrue(perms_26["allow_cancellations"])
        self.assertFalse(perms_26["random_closure_active"])

        # 3. 15:29 IST: Limit only allowed, system-driven random closure active (15:28 - 15:30)
        perms_29 = cal.get_cas_order_permissions("2026-08-10 15:29:00", exchange="NSE", is_cas_eligible=True)
        self.assertTrue(perms_29["cas_active"])
        self.assertEqual(perms_29["phase"], "cas_order_entry_limit_only")
        self.assertFalse(perms_29["allow_market_orders"])
        self.assertTrue(perms_29["allow_limit_orders"])
        self.assertTrue(perms_29["random_closure_active"])

        # 4. 15:32 IST: Order matching (no order entry permitted)
        perms_32 = cal.get_cas_order_permissions("2026-08-10 15:32:00", exchange="NSE", is_cas_eligible=True)
        self.assertTrue(perms_32["cas_active"])
        self.assertEqual(perms_32["phase"], "cas_matching")
        self.assertFalse(perms_32["allow_market_orders"])
        self.assertFalse(perms_32["allow_limit_orders"])

        # 5. Non-CAS security post-Aug 3, 2026
        perms_non_cas = cal.get_cas_order_permissions("2026-08-10 15:22:00", exchange="NSE", is_cas_eligible=False)
        self.assertFalse(perms_non_cas["cas_active"])
        self.assertFalse(perms_non_cas["allow_market_orders"])

        # 6. Pre-Aug 3, 2026 date (e.g. 2025 backtest)
        perms_2025 = cal.get_cas_order_permissions("2025-10-15 15:22:00", exchange="NSE", is_cas_eligible=True)
        self.assertFalse(perms_2025["cas_active"])

    def test_historical_session_rules_before_and_after_august_3_2026(self):
        # 1. Backtest for 2025: CAS did not exist. Must yield 75 bars even if scrip is F&O eligible!
        bars_2025 = cal.get_session_bars("2025-10-15", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(bars_2025, 75)

        phase_2025, _ = cal.get_session_phase("2025-10-15 15:25:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase_2025, "regular")
        self.assertTrue(cal.is_market_hours_in("2025-10-15 15:25:00", exchange="NSE", is_cas_eligible=True))

        # 2. Date prior to August 3, 2026 (e.g. 2026-07-20)
        bars_pre_cas = cal.get_session_bars("2026-07-20", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(bars_pre_cas, 75)

        # 3. Exactly on effective date: August 3, 2026
        bars_effective = cal.get_session_bars("2026-08-03", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(bars_effective, 72)

        phase_effective, _ = cal.get_session_phase("2026-08-03 15:26:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase_effective, "cas_order_entry_limit_only")

    def test_bse_independent_schedule(self):
        # BSE schedule is independent: continuous trading 09:15-15:30 (75 bars)
        bars_bse = cal.get_session_bars("2026-08-10", exchange="BSE", is_cas_eligible=True)
        self.assertEqual(bars_bse, 75)

        phase_bse, _ = cal.get_session_phase("2026-08-10 15:25:00", exchange="BSE", is_cas_eligible=True)
        self.assertEqual(phase_bse, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-08-10 15:25:00", exchange="BSE", is_cas_eligible=True))

    def test_cli_clock_cas_and_symbol_resolution(self):
        import io
        import json
        from contextlib import redirect_stdout
        alerts = load("trader-alerts", "alerts")

        # 1. Post-August 3, 2026 with explicit --cas flag at 15:26 IST (limit only)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2026-08-10 15:26", "--cas", "--json"])
        self.assertEqual(code, 1)  # continuous market closed
        data = json.loads(buf.getvalue())
        self.assertFalse(data["open"])
        self.assertEqual(data["phase"], "cas_order_entry_limit_only")
        self.assertEqual(data["session_bars"], 72)
        self.assertTrue(data["is_cas_eligible"])

        # 2. Post-August 3, 2026 without --cas flag (non-CAS default) at 15:26 IST
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2026-08-10 15:26", "--json"])
        self.assertEqual(code, 0)  # non-CAS is open until 15:30
        data = json.loads(buf.getvalue())
        self.assertTrue(data["open"])
        self.assertEqual(data["phase"], "regular")
        self.assertEqual(data["session_bars"], 75)
        self.assertFalse(data["is_cas_eligible"])

    def test_sebi_cas_v2_and_three_eras(self):
        # 1. Circular citations and effective dates
        self.assertEqual(cal.SEBI_CAS_V2_CIRCULAR, "SEBI/HO/47/11/11(3)2025-MRD-POD2/I/23843/2026")
        self.assertEqual(cal.SEBI_CAS_V2_CIRCULAR_DATE, "2026-10-15")
        self.assertEqual(cal.NSE_CAS_V2_EFFECTIVE_DATE, "2026-12-07")
        self.assertEqual(cal.SEBI_UNEXECUTED_ORDER_TRANSITION_DATE, "2027-02-08")

        # 2. Era 1: PRE_CAS (before 2026-08-03)
        self.assertEqual(cal.get_cas_rule_version("2026-08-02", exchange="NSE", is_cas_eligible=True), cal.CASRuleVersion.PRE_CAS)
        self.assertEqual(cal.get_cas_rule_version("2025-10-15", exchange="NSE", is_cas_eligible=True), cal.CASRuleVersion.PRE_CAS)

        # 3. Era 2: CAS_V1 (2026-08-03 to 2026-12-06)
        self.assertEqual(cal.get_cas_rule_version("2026-08-03", exchange="NSE", is_cas_eligible=True), cal.CASRuleVersion.CAS_V1)
        self.assertEqual(cal.get_cas_rule_version("2026-10-19", exchange="NSE", is_cas_eligible=True), cal.CASRuleVersion.CAS_V1)
        self.assertEqual(cal.get_cas_rule_version("2026-12-06", exchange="NSE", is_cas_eligible=True), cal.CASRuleVersion.CAS_V1)

        # 4. Era 3: CAS_V2 (from 2026-12-07)
        self.assertEqual(cal.get_cas_rule_version("2026-12-07", exchange="NSE", is_cas_eligible=True), cal.CASRuleVersion.CAS_V2)
        self.assertEqual(cal.get_cas_rule_version("2027-01-15", exchange="NSE", is_cas_eligible=True), cal.CASRuleVersion.CAS_V2)

        # 5. Non-CAS or BSE always evaluates to PRE_CAS
        self.assertEqual(cal.get_cas_rule_version("2026-12-07", exchange="NSE", is_cas_eligible=False), cal.CASRuleVersion.PRE_CAS)
        self.assertEqual(cal.get_cas_rule_version("2026-12-07", exchange="BSE", is_cas_eligible=True), cal.CASRuleVersion.PRE_CAS)

    def test_cas_v2_continuous_trading_and_bars(self):
        # Under CAS V2 (effective 2026-12-07), continuous trading runs 09:15 to 15:30
        # Continuous bars are 75 for all cash securities!
        bars_cas = cal.get_session_bars("2026-12-08", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(bars_cas, 75)

        bars_non_cas = cal.get_session_bars("2026-12-08", exchange="NSE", is_cas_eligible=False)
        self.assertEqual(bars_non_cas, 75)

        # 15:25 IST is active continuous trading under CAS V2
        phase, _ = cal.get_session_phase("2026-12-08 15:25:00", exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-12-08 15:25:00", exchange="NSE", is_cas_eligible=True))

    def test_cas_v2_phases_and_permissions(self):
        date = "2026-12-08"

        # 15:30:30 IST: 1-minute CAS transition
        phase, reason = cal.get_session_phase("%s 15:30:30" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_transition")
        self.assertFalse(cal.is_market_hours_in("%s 15:30:30" % date, exchange="NSE", is_cas_eligible=True))

        perms_trans = cal.get_cas_order_permissions("%s 15:30:30" % date, exchange="NSE", is_cas_eligible=True)
        self.assertTrue(perms_trans["cas_active"])
        self.assertEqual(perms_trans["cas_rule_version"], cal.CASRuleVersion.CAS_V2)
        self.assertFalse(perms_trans["allow_market_orders"])
        self.assertFalse(perms_trans["allow_limit_orders"])

        # 15:33:00 IST: 5-minute Market + Limit order collection window (15:31 to 15:36)
        phase, _ = cal.get_session_phase("%s 15:33:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_order_entry_all")

        perms_all = cal.get_cas_order_permissions("%s 15:33:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertTrue(perms_all["allow_market_orders"])
        self.assertTrue(perms_all["allow_limit_orders"])
        self.assertTrue(perms_all["allow_modifications"])
        self.assertTrue(perms_all["allow_cancellations"])
        self.assertFalse(perms_all["random_closure_active"])

        # 15:37:00 IST: Limit orders only window (15:36 to 15:40) before random closure
        phase, _ = cal.get_session_phase("%s 15:37:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_order_entry_limit_only")

        perms_lim = cal.get_cas_order_permissions("%s 15:37:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertFalse(perms_lim["allow_market_orders"])
        self.assertTrue(perms_lim["allow_limit_orders"])
        self.assertFalse(perms_lim["random_closure_active"])

        # 15:39:00 IST: System-driven random closure window active (15:38 to 15:40)
        phase, reason = cal.get_session_phase("%s 15:39:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_order_entry_limit_only")
        self.assertIn("random_closure", reason)

        perms_rand = cal.get_cas_order_permissions("%s 15:39:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertFalse(perms_rand["allow_market_orders"])
        self.assertTrue(perms_rand["allow_limit_orders"])
        self.assertTrue(perms_rand["random_closure_active"])

        # 15:42:00 IST: Order matching immediately following collection closure (15:40 to 15:45)
        phase, _ = cal.get_session_phase("%s 15:42:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_matching")

        perms_match = cal.get_cas_order_permissions("%s 15:42:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertFalse(perms_match["allow_market_orders"])
        self.assertFalse(perms_match["allow_limit_orders"])

        # 15:47:00 IST: Post-auction buffer (15:45 to 15:50)
        phase, _ = cal.get_session_phase("%s 15:47:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "cas_buffer")

        # 15:55:00 IST: Post-closing session (15:50 to 16:00)
        phase, _ = cal.get_session_phase("%s 15:55:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "post_closing")

        # 16:05:00 IST: Closed after hours
        phase, _ = cal.get_session_phase("%s 16:05:00" % date, exchange="NSE", is_cas_eligible=True)
        self.assertEqual(phase, "closed")

    def test_boundary_dates_across_all_regimes(self):
        # Boundary 1: August 2, 2026 (Pre-CAS) vs August 3, 2026 (CAS V1)
        self.assertEqual(cal.get_cas_rule_version("2026-08-02", is_cas_eligible=True), cal.CASRuleVersion.PRE_CAS)
        self.assertEqual(cal.get_session_bars("2026-08-02", is_cas_eligible=True), 75)

        self.assertEqual(cal.get_cas_rule_version("2026-08-03", is_cas_eligible=True), cal.CASRuleVersion.CAS_V1)
        self.assertEqual(cal.get_session_bars("2026-08-03", is_cas_eligible=True), 72)

        # Boundary 2: December 6, 2026 (CAS V1) vs December 7, 2026 (CAS V2)
        self.assertEqual(cal.get_cas_rule_version("2026-12-06", is_cas_eligible=True), cal.CASRuleVersion.CAS_V1)
        self.assertEqual(cal.get_session_bars("2026-12-06", is_cas_eligible=True), 72)

        self.assertEqual(cal.get_cas_rule_version("2026-12-07", is_cas_eligible=True), cal.CASRuleVersion.CAS_V2)
        self.assertEqual(cal.get_session_bars("2026-12-07", is_cas_eligible=True), 75)

        # Boundary 3: February 7, 2027 (Transition Inactive) vs February 8, 2027 (Transition Active)
        self.assertFalse(cal.is_unexecuted_order_transition_active("2027-02-07"))
        self.assertTrue(cal.is_unexecuted_order_transition_active("2027-02-08"))
        self.assertTrue(cal.is_unexecuted_order_transition_active("2027-03-01"))

    def test_segment_independence(self):
        # 1. Equity derivatives continuous trading is 09:15 to 15:30 across all eras (75 bars)
        # Even during CAS V1 (e.g. 2026-08-10), derivatives continuous session is 75 bars
        bars_deriv = cal.get_session_bars("2026-08-10", segment=cal.MarketSegment.EQUITY_DERIVATIVES)
        self.assertEqual(bars_deriv, 75)

        # At 15:25 IST on 2026-08-10:
        # Cash CAS equity is in cas_order_entry_limit_only
        cash_phase, _ = cal.get_session_phase("2026-08-10 15:25:00", is_cas_eligible=True, segment=cal.MarketSegment.CASH_EQUITY)
        self.assertEqual(cash_phase, "cas_order_entry_limit_only")

        # But Derivatives is still in continuous regular trading!
        deriv_phase, _ = cal.get_session_phase("2026-08-10 15:25:00", segment=cal.MarketSegment.EQUITY_DERIVATIVES)
        self.assertEqual(deriv_phase, "regular")
        self.assertTrue(cal.is_market_hours_in("2026-08-10 15:25:00", segment=cal.MarketSegment.EQUITY_DERIVATIVES))

        # 2. get_segment_session_hours returns independent configurations
        hours_cash_v1 = cal.get_segment_session_hours(cal.MarketSegment.CASH_EQUITY, "2026-08-10", is_cas_eligible=True)
        self.assertEqual(hours_cash_v1["continuous_close"], "15:15")
        self.assertTrue(hours_cash_v1["has_cas"])
        self.assertEqual(hours_cash_v1["cas_rule_version"], cal.CASRuleVersion.CAS_V1)

        hours_cash_v2 = cal.get_segment_session_hours(cal.MarketSegment.CASH_EQUITY, "2026-12-08", is_cas_eligible=True)
        self.assertEqual(hours_cash_v2["continuous_close"], "15:30")
        self.assertTrue(hours_cash_v2["has_cas"])
        self.assertEqual(hours_cash_v2["cas_rule_version"], cal.CASRuleVersion.CAS_V2)

        hours_deriv = cal.get_segment_session_hours(cal.MarketSegment.EQUITY_DERIVATIVES, "2026-08-10")
        self.assertEqual(hours_deriv["continuous_close"], "15:30")
        self.assertFalse(hours_deriv["has_cas"])

        hours_special = cal.get_segment_session_hours(cal.MarketSegment.SPECIAL_SESSION, "2026-11-08")
        self.assertEqual(hours_special["segment"], cal.MarketSegment.SPECIAL_SESSION)
        self.assertEqual(hours_special["continuous_open"], "18:15")
        self.assertEqual(hours_special["continuous_close"], "19:15")

    def test_cli_clock_cas_v2_era(self):
        import io
        import json
        from contextlib import redirect_stdout
        alerts = load("trader-alerts", "alerts")

        # 1. Under CAS V2 at 15:25 IST: Continuous trading is OPEN (75 bars)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2026-12-08 15:25", "--cas", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["open"])
        self.assertEqual(data["phase"], "regular")
        self.assertEqual(data["session_bars"], 75)
        self.assertEqual(data["cas_rule_version"], "CAS_V2")
        self.assertFalse(data["unexecuted_order_transition_active"])

        # 2. Under CAS V2 at 15:33 IST: Continuous trading is CLOSED, CAS order entry all is active
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2026-12-08 15:33", "--cas", "--json"])
        self.assertEqual(code, 1)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["open"])
        self.assertEqual(data["phase"], "cas_order_entry_all")
        self.assertEqual(data["cas_rule_version"], "CAS_V2")

        # 3. Future date post-Feb 8, 2027 shows unexecuted_order_transition_active is True
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = alerts.main(["clock", "--market", "IN", "--now", "2027-02-10 10:00", "--cas", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["unexecuted_order_transition_active"])


if __name__ == "__main__":
    unittest.main()
