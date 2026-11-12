"""End-to-end intraday decision engine tests (entry, exit, scanner, alerts, safety)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import _load

entry = _load.load("trader-entry", "entry")
exit_mod = _load.load("trader-exit", "exit")
scan = _load.load("trader-scanner", "scan")
alerts = _load.load("trader-alerts", "alerts")
budget = _load.load("trader-budget", "size")
risk = _load.load("trader-risk", "check")
broker = _load.load("trader-broker", "ticket")


def _ist_utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc)


def _et_utc(y, m, d, hh, mm):
    # EDT (-04:00) for Oct dates in fixtures
    return datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)


def _make_v8_chart(sym, bars, interval="5m", market="IN", currency="INR",
                   exchange="NSE", provider="upstox", data_tier="CURRENT",
                   is_live=True, prev_close=100.0, quote=None, instrument_type="EQUITY"):
    ts = [int(b["t"]) for b in bars]
    os_ = [b["o"] for b in bars]
    hs = [b["h"] for b in bars]
    ls = [b["l"] for b in bars]
    cs = [b["c"] for b in bars]
    vs = [b.get("v", 10000) for b in bars]
    gmtoff = 19800 if market == "IN" else -14400
    meta = {
        "symbol": sym,
        "currency": currency,
        "exchangeName": exchange,
        "instrumentType": instrument_type,
        "dataGranularity": interval,
        "gmtoffset": gmtoff,
        "chartPreviousClose": prev_close,
        "previousClose": prev_close,
        "regularMarketPrice": cs[-1] if cs else prev_close,
    }
    doc = {
        "chart": {
            "result": [{
                "meta": meta,
                "timestamp": ts,
                "indicators": {
                    "quote": [{
                        "open": os_,
                        "high": hs,
                        "low": ls,
                        "close": cs,
                        "volume": vs,
                    }]
                },
            }],
            "error": None,
        }
    }
    ch = entry.parse_chart(json.dumps(doc), sym)
    ch["market"] = market
    ch["provider"] = provider
    ch["source"] = provider
    ch["data_tier"] = data_tier
    ch["provider_data_status"] = data_tier
    ch["is_live"] = is_live
    ch["is_offline"] = False
    if quote is not None:
        ch["current_quote"] = quote
    return ch, doc


def _build_session_bars(start_utc, count, step_sec, base_price=2800.0,
                        step_delta=1.0, spread=2.0, volume=15000):
    bars = []
    t0 = int(start_utc.timestamp())
    for i in range(count):
        px = round(base_price + i * step_delta, 2)
        bars.append({
            "t": t0 + i * step_sec,
            "o": round(px - 0.5, 2),
            "h": round(px + spread, 2),
            "l": round(px - spread, 2),
            "c": px,
            "v": volume,
        })
    return bars


class IntradayEngineTests(unittest.TestCase):

    def test_mid_morning_breakout_intraday_entry_confirmed(self):
        # Prior day warmup bars (2026-10-16) + morning bars up to 10:30 IST (2026-10-19)
        prev_day = _build_session_bars(_ist_utc(2026, 10, 16, 13, 30), 24, 300, base_price=2780.0, step_delta=0.6)
        today = _build_session_bars(_ist_utc(2026, 10, 19, 9, 15), 15, 300, base_price=2798.0, step_delta=0.4)
        # Keep consolidation below 2810 then cleanly break above 12-bar high on the 10:25 completed bar
        for b in today[:-1]:
            b["h"] = min(b["h"], 2808.0)
            b["c"] = min(b["c"], 2805.0)
            b["o"] = min(b["o"], 2804.0)
            b["l"] = min(b["l"], 2800.0)
            b["v"] = 12000
        # 10:25 bar (completed at 10:30 IST) breaks above 2805.20 cleanly inside entry zone with strong volume
        today[-1]["o"] = 2804.8
        today[-1]["h"] = 2809.0
        today[-1]["l"] = 2804.5
        today[-1]["c"] = 2808.2
        today[-1]["v"] = 36000
        # Forming 10:30 bar (incomplete at 10:31 IST)
        forming = {"t": today[-1]["t"] + 300, "o": 2808.2, "h": 2809.2, "l": 2807.8, "c": 2808.5, "v": 9000}
        now = _ist_utc(2026, 10, 19, 10, 31)
        ch, _ = _make_v8_chart(
            "RELIANCE", prev_day + today + [forming], interval="5m", market="IN",
            prev_close=2795.0,
            quote={"price": 2808.5, "timestamp": int(now.timestamp()), "provider": "upstox", "data_status": "CURRENT", "is_live": True},
        )
        cfg = dict(entry.DEFAULTS_IN)
        cfg.update({"budget": 100000.0, "max_loss": 2000.0, "max_order": 50000.0, "position_cap": 1.0})
        res = entry.evaluate(
            ch, now, setup="breakout_intraday", interval="5m", market="IN",
            min_volume_x=1.5, cfg=cfg, lookback_bars=12,
        )
        self.assertEqual(res["decision_state"], "ENTRY CONFIRMED")
        self.assertTrue(res["decision"]["actionable"])
        self.assertIsNotNone(res["decision"]["entry_zone"])
        self.assertLessEqual(res["decision"]["entry_zone"]["low"], res["decision"]["entry_zone"]["high"])
        self.assertIsNotNone(res["decision"]["invalidation_level"])
        self.assertIsNotNone(res["decision"]["stop_preview"])
        self.assertIsNotNone(res["decision"]["targets_preview"])
        self.assertGreater(res["decision"]["reward_risk"], 1.0)
        self.assertGreater(res["sizing_preview"]["qty"], 0)
        self.assertEqual(res["candle_context"]["completed_candle"]["close"], 2808.2)
        self.assertEqual(res["candle_context"]["forming_candle"]["close_so_far"], 2808.5)
        self.assertEqual(res["candle_context"]["current_quote"]["price"], 2808.5)
        self.assertEqual(res["decision"]["evaluation_mode"], "on_invocation")

    def test_late_session_1400_and_15m_without_opening_range_or_daily(self):
        # Bars ONLY from 11:30 to 14:00 (no 09:30/09:15 OR bars, no daily chart)
        bars_15m = _build_session_bars(_et_utc(2026, 10, 19, 10, 30), 14, 900, base_price=195.0, step_delta=0.35, spread=0.6)
        # Reclaim pattern on last two bars
        bars_15m[-3]["c"] = 197.8
        bars_15m[-2]["c"] = 199.6
        bars_15m[-2]["h"] = 200.1
        bars_15m[-1]["c"] = 200.6
        bars_15m[-1]["h"] = 201.0
        now = _et_utc(2026, 10, 19, 14, 1)  # 14:01 ET just after 13:45 bar closes
        ch, _ = _make_v8_chart("AAPL", bars_15m, interval="15m", market="US", currency="USD", exchange="NMS", prev_close=195.0)
        for strat in ("breakout_intraday", "vwap_reclaim", "ema_alignment"):
            res = entry.evaluate(ch, now, setup=strat, interval="15m", market="US", lookback_bars=8)
            self.assertNotIn("no opening range", (res.get("reason") or "").lower())
            self.assertIn(res["decision_state"], ("ENTRY CONFIRMED", "ENTRY CANDIDATE", "SETUP", "NO TRADE"))
        # Regression check: passing a 5m chart with --interval 15m still raises EntryError
        ch_5m, _ = _make_v8_chart("AAPL", bars_15m, interval="5m", market="US", currency="USD", exchange="NMS")
        with self.assertRaises(entry.EntryError):
            entry.evaluate(ch_5m, now, setup="breakout_intraday", interval="15m", market="US")

    def test_entry_candidate_and_anti_chasing_extended_and_invalidated(self):
        # Forming candle triggers before close -> ENTRY CANDIDATE (waiting)
        bars = _build_session_bars(_ist_utc(2026, 10, 19, 9, 15), 22, 300, base_price=500.0, step_delta=0.2, spread=0.8)
        for b in bars:
            b["h"] = 504.0
            b["l"] = 499.0
            b["c"] = 502.5
        forming = {"t": bars[-1]["t"] + 300, "o": 502.6, "h": 505.2, "l": 502.4, "c": 505.0, "v": 15000}
        now_cand = _ist_utc(2026, 10, 19, 11, 8)
        ch_cand, _ = _make_v8_chart("SBIN", bars + [forming], interval="5m", market="IN", prev_close=498.0)
        res_cand = entry.evaluate(ch_cand, now_cand, setup="breakout_intraday", interval="5m", market="IN", lookback_bars=12)
        self.assertEqual(res_cand["decision_state"], "ENTRY CANDIDATE")
        self.assertFalse(res_cand["decision"]["actionable"])

        # Extended anti-chasing: breakout happened at 505.0, price surged 6% away to 536.0
        ext_bars = list(bars)
        ext_bars.append({"t": bars[-1]["t"] + 300, "o": 503.0, "h": 506.0, "l": 502.8, "c": 505.5, "v": 30000})
        ext_bars.append({"t": bars[-1]["t"] + 600, "o": 505.5, "h": 525.0, "l": 505.0, "c": 524.0, "v": 40000})
        ext_bars.append({"t": bars[-1]["t"] + 900, "o": 524.0, "h": 537.0, "l": 523.5, "c": 536.0, "v": 35000})
        now_ext = datetime.fromtimestamp(ext_bars[-1]["t"] + 310, tz=timezone.utc)
        ch_ext, _ = _make_v8_chart("SBIN", ext_bars, interval="5m", market="IN", prev_close=498.0)
        res_ext = entry.evaluate(
            ch_ext, now_ext, setup="breakout_intraday", interval="5m", market="IN",
            lookback_bars=12, max_chase_pct=1.0, max_chase_atr=1.0,
        )
        self.assertEqual(res_ext["decision_state"], "NO TRADE")
        self.assertEqual(res_ext["sub_state"], "EXTENDED")
        self.assertTrue(res_ext["decision"]["chase_blocked"])
        self.assertFalse(res_ext["decision"]["actionable"])

        # Invalidated setup: broke level then reversed sharply below invalidation_level
        inv_bars = list(bars)
        inv_bars.append({"t": bars[-1]["t"] + 300, "o": 503.0, "h": 505.5, "l": 502.5, "c": 505.0, "v": 25000})
        inv_bars.append({"t": bars[-1]["t"] + 600, "o": 504.0, "h": 504.2, "l": 491.0, "c": 492.0, "v": 45000})
        now_inv = datetime.fromtimestamp(inv_bars[-1]["t"] + 310, tz=timezone.utc)
        ch_inv, _ = _make_v8_chart("SBIN", inv_bars, interval="5m", market="IN", prev_close=498.0)
        res_inv = entry.evaluate(ch_inv, now_inv, setup="breakout_intraday", interval="5m", market="IN", lookback_bars=12)
        self.assertEqual(res_inv["decision_state"], "NO TRADE")
        self.assertEqual(res_inv["sub_state"], "INVALIDATED")

    def test_mid_session_exit_stop_trail_partial_positional_and_expiry(self):
        # Base 5m series through 11:00 IST
        bars = _build_session_bars(_ist_utc(2026, 10, 19, 9, 15), 21, 300, base_price=1000.0, step_delta=0.5, spread=2.0)
        pos = {
            "symbol": "INFY", "market": "IN", "exchange": "NSE", "trading_style": "intraday",
            "entry": 1005.0, "stop": 995.0, "target": 1025.0, "qty": 20, "tick_size": 0.05,
        }

        # 1) Stop hit at 11:00 IST -> immediate EXIT (never waits until 15:20)
        stop_bars = list(bars)
        stop_bars[-1] = dict(stop_bars[-1], h=999.0, l=993.0, c=994.0)
        now_1100 = datetime.fromtimestamp(stop_bars[-1]["t"] + 305, tz=timezone.utc)
        ch_stop, _ = _make_v8_chart("INFY", stop_bars, interval="5m", market="IN", prev_close=1000.0)
        r_stop = exit_mod.evaluate_position(pos, ch_stop, now_1100, interval="5m", market="IN")
        self.assertEqual(r_stop["action"], "EXIT")
        self.assertEqual(r_stop["reason_code"], "STOP_HIT")
        self.assertEqual(r_stop["exit_qty"], 20)

        # 2) Healthy trend (+0.4R, above VWAP/EMA and stop) -> HOLD
        hold_bars = list(bars)
        hold_bars[-1] = dict(hold_bars[-1], c=1010.0, h=1011.0, l=1008.0)
        ch_hold, _ = _make_v8_chart("INFY", hold_bars, interval="5m", market="IN", prev_close=1000.0)
        r_hold = exit_mod.evaluate_position(pos, ch_hold, now_1100, interval="5m", market="IN")
        self.assertEqual(r_hold["action"], "HOLD")
        self.assertEqual(r_hold["exit_qty"], 0)

        # 3) >= 1R move in progress without hitting 2R target -> UPDATE TRAILING STOP (strictly raised)
        trail_bars = list(bars)
        for idx, offset in enumerate(range(-6, 0)):
            trail_bars[offset]["c"] = 1012.0 + idx * 1.4
            trail_bars[offset]["h"] = trail_bars[offset]["c"] + 1.0
            trail_bars[offset]["l"] = trail_bars[offset]["c"] - 0.8
        trail_bars[-1]["c"] = 1019.0
        trail_bars[-1]["h"] = 1020.0
        ch_trail, _ = _make_v8_chart("INFY", trail_bars, interval="5m", market="IN", prev_close=1000.0)
        r_trail = exit_mod.evaluate_position(pos, ch_trail, now_1100, interval="5m", market="IN", trail_activate_r=1.0)
        self.assertEqual(r_trail["action"], "UPDATE TRAILING STOP")
        self.assertGreater(r_trail["recommended_stop"], pos["stop"])

        # 4) Partial exit when allow_partial=True hits target with multi-share integer split
        part_bars = list(trail_bars)
        part_bars[-1] = dict(part_bars[-1], c=1026.0, h=1027.0, l=1024.0)
        ch_part, _ = _make_v8_chart("INFY", part_bars, interval="5m", market="IN", prev_close=1000.0)
        r_part = exit_mod.evaluate_position(pos, ch_part, now_1100, interval="5m", market="IN", allow_partial=True)
        self.assertEqual(r_part["action"], "PARTIAL EXIT")
        self.assertEqual(r_part["exit_qty"], 10)
        self.assertGreaterEqual(r_part["recommended_stop"], pos["entry"])

        # 5) Positional equity at 15:25 IST must NOT force evening square-off
        eod_bars = _build_session_bars(_ist_utc(2026, 10, 19, 14, 0), 17, 300, base_price=1007.0, step_delta=0.2)
        now_1525 = datetime.fromtimestamp(eod_bars[-1]["t"] + 305, tz=timezone.utc)
        pos_delivery = dict(pos, trading_style="positional")
        ch_eod, _ = _make_v8_chart("INFY", eod_bars, interval="5m", market="IN", prev_close=1000.0)
        r_deliv = exit_mod.evaluate_position(pos_delivery, ch_eod, now_1525, interval="5m", market="IN")
        self.assertEqual(r_deliv["action"], "HOLD")
        # Whereas intraday equity at 15:25 IST exits for flat_by (15:20 IST)
        r_intra_eod = exit_mod.evaluate_position(pos, ch_eod, now_1525, interval="5m", market="IN")
        self.assertEqual(r_intra_eod["action"], "EXIT")
        self.assertEqual(r_intra_eod["reason_code"], "INTRADAY_SQUARE_OFF")

        # 6) Expiry approaching on options/derivative position
        opt_pos = dict(pos, symbol="NIFTY26OCT24500CE", trading_style="positional", expiry="2026-10-19")
        r_exp = exit_mod.evaluate_position(opt_pos, ch_eod, now_1525, interval="5m", market="IN")
        self.assertEqual(r_exp["action"], "EXIT")
        self.assertEqual(r_exp["reason_code"], "EXPIRY_APPROACHING")

    def test_stale_data_fails_closed_for_both_entry_and_exit(self):
        bars = _build_session_bars(_ist_utc(2026, 10, 19, 9, 15), 20, 300, base_price=2800.0, step_delta=0.5)
        # Last bar was at ~10:50, evaluate 90 minutes later at 12:20 with no new data
        now_stale = datetime.fromtimestamp(bars[-1]["t"] + 5400, tz=timezone.utc)
        ch, _ = _make_v8_chart("RELIANCE", bars, interval="5m", market="IN", prev_close=2795.0)
        r_e = entry.evaluate(ch, now_stale, setup="breakout_intraday", interval="5m", market="IN")
        self.assertEqual(r_e["decision_state"], "NO TRADE")
        self.assertEqual(r_e["sub_state"], "STALE_DATA")
        pos = {"symbol": "RELIANCE", "market": "IN", "entry": 2800.0, "stop": 2785.0, "target": 2830.0, "qty": 10}
        r_x = exit_mod.evaluate_position(pos, ch, now_stale, interval="5m", market="IN")
        self.assertEqual(r_x["action"], "NO_ACTION")
        self.assertEqual(r_x["reason_code"], "STALE_DATA")

    def test_deduplication_across_entry_exit_and_scanner(self):
        bars = _build_session_bars(_ist_utc(2026, 10, 19, 9, 15), 16, 300, base_price=100.0, step_delta=0.2)
        for b in bars[:-1]:
            b["h"] = 103.0
            b["c"] = 102.0
        bars[-1] = {"t": bars[-1]["t"], "o": 102.5, "h": 105.0, "l": 102.3, "c": 104.5, "v": 30000}
        now = datetime.fromtimestamp(bars[-1]["t"] + 310, tz=timezone.utc)
        ch, _ = _make_v8_chart("TCS", bars, interval="5m", market="IN", prev_close=99.0)
        res = entry.evaluate(ch, now, setup="breakout_intraday", interval="5m", market="IN", lookback_bars=12)

        with tempfile.TemporaryDirectory() as td:
            e_state = os.path.join(td, "entry_state.json")
            entry.update_signal_state(e_state, "TCS", "breakout_intraday", "5m", res, now)
            self.assertEqual(res["signal_change"], "NEW")
            self.assertFalse(res["deduplicated"])
            entry.update_signal_state(e_state, "TCS", "breakout_intraday", "5m", res, now)
            self.assertEqual(res["signal_change"], "UNCHANGED")
            self.assertTrue(res["deduplicated"])

            x_state = os.path.join(td, "exit_state.json")
            pos = {"symbol": "TCS", "market": "IN", "entry": 101.0, "stop": 99.0, "target": 106.0, "qty": 10}
            xr = exit_mod.evaluate_position(pos, ch, now, interval="5m", market="IN")
            exit_mod.update_exit_state(x_state, [xr], now, market="IN")
            self.assertEqual(xr["signal_change"], "NEW")
            exit_mod.update_exit_state(x_state, [xr], now, market="IN")
            self.assertEqual(xr["signal_change"], "UNCHANGED")
            self.assertTrue(xr["deduplicated"])

    def test_scan_intraday_stocks_and_index_plus_alerts_trailing_stop(self):
        bars_eq = _build_session_bars(_ist_utc(2026, 10, 19, 9, 15), 22, 300, base_price=2800.0, step_delta=0.3)
        for b in bars_eq[:-1]:
            b["h"] = 2807.0
            b["c"] = 2804.0
        bars_eq[-1] = {"t": bars_eq[-1]["t"], "o": 2805.0, "h": 2812.0, "l": 2804.5, "c": 2811.0, "v": 40000}
        bars_idx = _build_session_bars(_ist_utc(2026, 10, 19, 9, 15), 22, 300, base_price=24500.0, step_delta=2.0, volume=0)
        bars_idx[-1] = {"t": bars_idx[-1]["t"], "o": 24535.0, "h": 24560.0, "l": 24530.0, "c": 24555.0, "v": 0}
        now = datetime.fromtimestamp(bars_eq[-1]["t"] + 310, tz=timezone.utc)

        _, doc_eq = _make_v8_chart("RELIANCE", bars_eq, interval="5m", market="IN", prev_close=2795.0)
        _, doc_idx = _make_v8_chart("NIFTY", bars_idx, interval="5m", market="IN", prev_close=24450.0, instrument_type="INDEX")
        offline = {
            "RELIANCE": scan.parse_chart(json.dumps(doc_eq), "RELIANCE"),
            "NIFTY": scan.parse_chart(json.dumps(doc_idx), "NIFTY"),
        }
        with tempfile.TemporaryDirectory() as td:
            s_state = os.path.join(td, "scan_state.json")
            out = scan.scan_intraday(
                ["RELIANCE", "NIFTY"], now,
                strategies=["breakout_intraday", "ema_alignment"],
                interval="5m", market="IN", offline=offline, state_path=s_state,
                include_all=True,
            )
            hits = out["hits"]
            self.assertTrue(any(h["symbol"] == "RELIANCE" and not h["analytical_only"] for h in hits))
            nifty_hits = [h for h in hits if h["symbol"] == "NIFTY"]
            self.assertTrue(nifty_hits)
            self.assertTrue(all(h["analytical_only"] and not h["is_tradable_equity"] for h in nifty_hits))
            self.assertEqual(hits[0]["signal_change"], "NEW")

            # Alerts rules_from_plan picks raised trailing_stop
            plan = {"symbol": "RELIANCE", "stop": 2795.0, "trailing_stop": 2804.0, "target": 2835.0}
            rules = alerts.rules_from_plan(plan)
            stop_rule = [r for r in rules if r["type"] == "stop"][0]
            self.assertEqual(stop_rule["price"], 2804.0)
            self.assertIn("trailing", stop_rule["note"].lower())

    def test_orb_timing_and_three_gate_safety_preserved(self):
        # Before ORB closes (09:22 IST for 15m ORB), ORB says waiting / forming while breakout_intraday still works on warmup bars
        warmup = _build_session_bars(_ist_utc(2026, 10, 16, 13, 45), 18, 300, base_price=150.0, step_delta=0.2)
        for b in warmup[:-1]:
            b["h"] = 153.5
            b["c"] = 152.5
        morning_one = {"t": int(_ist_utc(2026, 10, 19, 9, 15).timestamp()), "o": 152.6, "h": 154.5, "l": 152.4, "c": 154.2, "v": 25000}
        now_early = _ist_utc(2026, 10, 19, 9, 21)
        ch, _ = _make_v8_chart("TATASTEEL", warmup + [morning_one], interval="5m", market="IN", prev_close=151.0)
        res_orb = entry.evaluate(ch, now_early, setup="orb", or_minutes=15, interval="5m", market="IN")
        self.assertEqual(res_orb["decision_state"], "SETUP")
        self.assertFalse(res_orb["decision"]["actionable"])
        self.assertEqual(res_orb["status"], "waiting")

        # Spot index or unapproved order must still fail three-gate safety
        cfg = dict(risk.DEFAULTS, market="IN", paper=True)
        with tempfile.TemporaryDirectory() as td:
            orders_path = os.path.join(td, "orders.json")
            with open(orders_path, "w") as f:
                json.dump({
                    "created_by": "trader-budget", "market": "IN", "exchange": "NSE",
                    "orders": [{"symbol": "RELIANCE", "qty": 5, "entry": 2811.0, "stop": 2800.0, "target": 2833.0, "order_type": "MIS"}],
                }, f)
            # Without trader-risk PASS verdict, broker.risk_verdict blocks ticket generation
            orders_doc = {
                "created_by": "trader-budget", "market": "IN", "exchange": "NSE",
                "orders": [{"symbol": "RELIANCE", "qty": 5, "entry": 2808.2, "stop": 2800.0, "target": 2824.6, "order_type": "MIS"}],
            }
            verdict, why = broker.risk_verdict({"verdict": "BLOCK", "date": "2026-10-19"}, orders_doc, now_early)
            self.assertIsNone(verdict)
            self.assertTrue(bool(why))
            # With index order attempted, budget/risk blocks direct spot equity execution
            idx_plan = {"symbol": "NIFTY", "market": "IN", "exchange": "NSE", "entry": 24555.0, "stop": 24450.0, "target": 24765.0}
            with self.assertRaises(budget.SizeError):
                budget.size_trade(idx_plan, dict(budget.DEFAULTS, budget=100000.0, max_loss=2000.0, market="IN"), market="IN")


if __name__ == "__main__":
    unittest.main()
