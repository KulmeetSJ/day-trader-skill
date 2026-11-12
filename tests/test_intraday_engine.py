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

    def test_nifty50_chase_block_and_breakout_reconciliation(self):
        # Exact NIFTY50 chase-block figures: curr=25299.35, zone_high=25278.45, trigger=25266.90, atr14=13.7107
        ac = entry.compute_anti_chasing(
            25299.35,
            {"low": 25263.45, "high": 25278.45, "anchor": 25266.90},
            trig={"c": 25266.90, "l": 25251.10},
            atr_val=13.7107,
            px_source="current_quote",
            market="IN",
            eff_tick=0.05,
            explicit_tick=True,
            side="buy",
        )
        self.assertTrue(ac["chase_blocked"])
        self.assertEqual(ac["extension_atr"], 1.52)
        self.assertEqual(ac["extension_atr_from_trigger"], 2.37)
        self.assertEqual(ac["extension_pct_over_zone"], 0.08)

        # Full breakout_intraday evaluation where current price chases past entry_zone.high
        base_t = _ist_utc(2026, 11, 12, 9, 15)
        bars = _build_session_bars(base_t, 12, 300, base_price=25250.0, step_delta=0.5, spread=6.0)
        for b in bars:
            b["h"] = 25260.0
            b["l"] = 25245.0
            b["c"] = 25252.0
            b["v"] = 100000
        # Trigger bar closes cleanly above breakout level with volume confirmation
        t_trig = bars[-1]["t"] + 300
        trig_bar = {"t": t_trig, "o": 25255.0, "h": 25269.0, "l": 25251.10, "c": 25266.90, "v": 220000}
        # Next completed bar stays extended above zone high without breaking invalidation
        ext_bar = {"t": t_trig + 300, "o": 25270.0, "h": 25305.0, "l": 25268.0, "c": 25299.35, "v": 180000}
        now = datetime.fromtimestamp(ext_bar["t"] + 310, tz=timezone.utc)
        ch, _ = _make_v8_chart("NIFTY", bars + [trig_bar, ext_bar], interval="5m", market="IN", prev_close=25240.0)
        res = entry.evaluate(ch, now, setup="breakout_intraday", interval="5m", market="IN", lookback_bars=12)
        # NIFTY spot is analytical-only, while analytical chase-block & reference metrics must reconcile
        self.assertEqual(res["decision_state"], "NO TRADE")
        self.assertEqual(res["sub_state"], "ANALYTICAL_ONLY_INDEX")
        self.assertTrue(res["anti_chasing"]["chase_blocked"])
        self.assertIsNone(res["sizing_preview"])
        # Same bars on a tradable equity surface sub_state == EXTENDED explicitly
        ch_eq, _ = _make_v8_chart("RELIANCE", bars + [trig_bar, ext_bar], interval="5m", market="IN", prev_close=25240.0)
        res_eq = entry.evaluate(ch_eq, now, setup="breakout_intraday", interval="5m", market="IN", lookback_bars=12)
        self.assertEqual(res_eq["decision_state"], "NO TRADE")
        self.assertEqual(res_eq["sub_state"], "EXTENDED")
        # Displayed entry_reference must anchor to trigger close (not chased curr_px)
        self.assertEqual(res["entry_reference_source"], "trigger_close")
        self.assertEqual(res["entry_reference"], res["trigger"]["price"])
        # Displayed extension_atr matches distance above zone high, not trigger close
        atr14 = res["indicators"]["atr14"]
        ez_high = res["entry_zone"]["high"]
        expected_ext_atr = round((res["anti_chasing"]["current_price"] - ez_high) / atr14, 2)
        self.assertEqual(res["anti_chasing"]["extension_atr"], expected_ext_atr)
        # Displayed risk_per_unit, targets and R-multiple must algebraically reconcile
        rpu = res["risk_per_unit"]
        self.assertEqual(round(res["entry_reference"] - res["stop_preview"], 2), rpu)
        self.assertEqual(
            res["reward_risk"],
            round((res["targets_preview"]["t2_2r"] - res["entry_reference"]) / rpu, 2),
        )

    def test_nifty50_orb_invalidation_vs_stop_and_reconciliation(self):
        # Replicate NIFTY50 15m OR: low=25145.0, high=25341.6 -> or_mid=25243.30
        t0 = _ist_utc(2026, 11, 12, 9, 15)
        or_bars = [
            {"t": int(t0.timestamp()), "o": 25200.0, "h": 25260.0, "l": 25145.0, "c": 25220.0, "v": 150000},
            {"t": int(t0.timestamp()) + 300, "o": 25220.0, "h": 25310.0, "l": 25210.0, "c": 25295.0, "v": 160000},
            {"t": int(t0.timestamp()) + 600, "o": 25295.0, "h": 25341.6, "l": 25280.0, "c": 25325.0, "v": 170000},
        ]
        # Trigger closes at 25348.30 with low=25326.20 -> setup invalidation=25326.20, OR-mid stop=25243.30
        trig_bar = {"t": int(t0.timestamp()) + 900, "o": 25330.0, "h": 25352.0, "l": 25326.20, "c": 25348.30, "v": 200000}

        # Case A: Inside entry zone (at trigger close 25348.30)
        now_valid = datetime.fromtimestamp(trig_bar["t"] + 310, tz=timezone.utc)
        ch_ok, _ = _make_v8_chart("NIFTY", or_bars + [trig_bar], interval="5m", market="IN", prev_close=25190.0)
        res_ok = entry.evaluate(ch_ok, now_valid, setup="orb", or_minutes=15, interval="5m", market="IN")
        self.assertEqual(res_ok["invalidation_level"], 25326.20)
        self.assertEqual(res_ok["stop_preview"], 25243.30)
        self.assertEqual(res_ok["stop_preview_source"], "or_midpoint")
        self.assertNotEqual(res_ok["invalidation_level"], res_ok["stop_preview"])
        self.assertEqual(res_ok["entry_reference"], 25348.30)
        self.assertEqual(res_ok["risk_per_unit"], 105.0)
        self.assertEqual(res_ok["targets_preview"]["t1_1r"], 25453.30)
        self.assertEqual(res_ok["targets_preview"]["t2_2r"], 25558.30)
        self.assertEqual(res_ok["reward_risk"], 2.0)

        # Case B: Pullback below zone.low (25330.00) but strictly above invalidation (25326.20)
        pb_bar = {"t": trig_bar["t"] + 300, "o": 25345.0, "h": 25346.0, "l": 25328.0, "c": 25330.0, "v": 140000}
        ch_pb, _ = _make_v8_chart("NIFTY", or_bars + [trig_bar, pb_bar], interval="5m", market="IN", prev_close=25190.0)
        res_pb = entry.evaluate(ch_pb, datetime.fromtimestamp(pb_bar["t"] + 310, tz=timezone.utc),
                                setup="orb", or_minutes=15, interval="5m", market="IN")
        self.assertEqual(res_pb["sub_state"], "ANALYTICAL_ONLY_INDEX")
        self.assertFalse(res_pb["anti_chasing"]["chase_blocked"])
        self.assertEqual(res_pb["entry_reference"], 25330.0)
        self.assertEqual(res_pb["stop_preview"], 25243.30)
        self.assertEqual(res_pb["risk_per_unit"], 86.7)
        self.assertEqual(res_pb["targets_preview"]["t2_2r"], 25503.40)
        self.assertEqual(res_pb["reward_risk"], 2.0)

        # Same pullback bars on tradable equity surface ENTRY CONFIRMED / ACTIONABLE_IN_ZONE
        ch_pb_eq, _ = _make_v8_chart("RELIANCE", or_bars + [trig_bar, pb_bar], interval="5m", market="IN", prev_close=25190.0)
        res_pb_eq = entry.evaluate(ch_pb_eq, datetime.fromtimestamp(pb_bar["t"] + 310, tz=timezone.utc),
                                   setup="orb", or_minutes=15, interval="5m", market="IN")
        self.assertEqual(res_pb_eq["decision_state"], "ENTRY CONFIRMED")
        self.assertEqual(res_pb_eq["sub_state"], "ACTIONABLE_IN_ZONE")

        # Case C: Price drops to 25299.35 <= 25326.20 invalidation -> must NOT emit phantom 2.0R or T2 25411.45
        inv_bar = {"t": trig_bar["t"] + 300, "o": 25340.0, "h": 25342.0, "l": 25295.0, "c": 25299.35, "v": 180000}
        ch_inv, _ = _make_v8_chart("NIFTY", or_bars + [trig_bar, inv_bar], interval="5m", market="IN", prev_close=25190.0)
        res_inv = entry.evaluate(ch_inv, datetime.fromtimestamp(inv_bar["t"] + 310, tz=timezone.utc),
                                 setup="orb", or_minutes=15, interval="5m", market="IN")
        self.assertEqual(res_inv["decision_state"], "NO TRADE")
        self.assertIsNone(res_inv["entry_reference"])
        self.assertIsNone(res_inv["stop_preview"])
        self.assertIsNone(res_inv["risk_per_unit"])
        self.assertIsNone(res_inv["targets_preview"])
        self.assertIsNone(res_inv["reward_risk"])
        self.assertIsNone(res_inv["sizing_preview"])
        ch_inv_eq, _ = _make_v8_chart("RELIANCE", or_bars + [trig_bar, inv_bar], interval="5m", market="IN", prev_close=25190.0)
        res_inv_eq = entry.evaluate(ch_inv_eq, datetime.fromtimestamp(inv_bar["t"] + 310, tz=timezone.utc),
                                    setup="orb", or_minutes=15, interval="5m", market="IN")
        self.assertEqual(res_inv_eq["sub_state"], "INVALIDATED")

    def test_risk_reward_plan_long_short_positions_and_invalid_atr(self):
        # Long: non-tick entry/stop normalized cleanly, exact R-multiple reconciliation
        long_plan = entry.compute_risk_reward_plan(100.23, 98.11, side="buy", tick=0.05)
        self.assertIsNotNone(long_plan)
        self.assertEqual(long_plan["entry_reference"], 100.25)
        self.assertEqual(long_plan["stop_price"], 98.10)
        self.assertEqual(long_plan["risk_per_unit"], 2.15)
        self.assertEqual(long_plan["t1_1r"], 102.40)
        self.assertEqual(long_plan["t2_2r"], 104.55)
        self.assertEqual(long_plan["reward_risk"], 2.0)

        # Long invalid positions: stop >= entry or near-zero risk
        self.assertIsNone(entry.compute_risk_reward_plan(100.0, 100.0, side="buy", tick=0.05))
        self.assertIsNone(entry.compute_risk_reward_plan(100.0, 102.0, side="buy", tick=0.05))
        self.assertIsNone(entry.compute_risk_reward_plan(25000.0, 24995.0, side="buy", tick=0.05))

        # Short: stop ceiled above entry, targets downward
        short_plan = entry.compute_risk_reward_plan(250.00, 254.02, side="sell", tick=0.05)
        self.assertIsNotNone(short_plan)
        self.assertEqual(short_plan["entry_reference"], 250.00)
        self.assertEqual(short_plan["stop_price"], 254.05)
        self.assertEqual(short_plan["risk_per_unit"], 4.05)
        self.assertEqual(short_plan["t1_1r"], 245.95)
        self.assertEqual(short_plan["t2_2r"], 241.90)
        self.assertEqual(short_plan["reward_risk"], 2.0)
        self.assertIsNone(entry.compute_risk_reward_plan(250.0, 249.0, side="sell", tick=0.05))

        # Short anti-chasing: extended below zone.low vs inside zone
        zone = {"low": 248.0, "high": 250.5, "anchor": 250.0}
        ac_short_ext = entry.compute_anti_chasing(245.0, zone, trig={"c": 249.5}, atr_val=2.0, side="sell", eff_tick=0.05, explicit_tick=True)
        self.assertTrue(ac_short_ext["chase_blocked"])
        self.assertEqual(ac_short_ext["extension_atr"], 1.5)
        self.assertEqual(ac_short_ext["extension_atr_from_trigger"], 2.25)
        ac_short_ok = entry.compute_anti_chasing(249.0, zone, trig={"c": 249.5}, atr_val=2.0, side="sell", eff_tick=0.05, explicit_tick=True)
        self.assertFalse(ac_short_ok["chase_blocked"])
        self.assertIsNone(ac_short_ok["extension_atr"])

        # Missing / zero / negative / NaN / Inf ATR must fail safely without NaN propagation
        for bad_atr in (None, 0, 0.0, -5.0, float("nan"), float("inf"), float("-inf"), "bad"):
            ac_bad = entry.compute_anti_chasing(105.0, {"low": 99.0, "high": 101.0}, trig={"c": 100.0}, atr_val=bad_atr)
            self.assertTrue(ac_bad["chase_blocked"])
            self.assertIsNone(ac_bad["extension_atr"])
            self.assertEqual(ac_bad["extension_pct_over_zone"], 3.96)
            self.assertIsNone(entry.compute_risk_reward_plan(bad_atr, 95.0, side="buy"))
            self.assertIsNone(entry.compute_risk_reward_plan(100.0, bad_atr, side="buy"))


if __name__ == "__main__":
    unittest.main()
