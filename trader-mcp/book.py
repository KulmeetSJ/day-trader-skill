#!/usr/bin/env python3
"""AI paper trading book -- the engine behind trader-mcp/server.py (MCP tools for an AI trader).

The AI analyses and DECIDES; this module EXECUTES on paper, and only what the deterministic engine
allows. It adds no financial logic of its own:
  * sizing     -> trader-budget/size.py size_trade() + build_order()   (same as the real pipeline)
  * risk gate  -> trader-risk/check.py check()                        (must PASS the exact order)
  * charges    -> trader-budget calculate_indian_transaction_charges()
  * option buys-> trader-options specialist signals that are SIGNAL VALIDATED + actionable
The AI can't change a stop, a limit or a verdict: it picks among PASSing trades and writes why.

Two SEPARATE pools, Rs 1,00,000 each (config `budget`; user rule 2026-11-19):
  * trading   -- intraday equity (flat by 15:20) and intraday option buys
  * investing -- multi-day equity (positional, up to `max_hold_days`)
Each pool keeps its own broker-style order records, so trader-risk's daily loss / trades-per-day
limits apply per pool.

PAPER ONLY. Indian trading is paper-only at the schema level and Upstox is read-only: nothing here
routes an order anywhere. Fills are simulated at the live quote with one tick of slippage.

State: trader/state/ai_book.json  {positions, orders, previews, journal}
"""

import datetime
import hashlib
import json
import os
import subprocess
import sys
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-budget", "trader-risk", "trader-options", "trader-timemachine", "config", "scripts"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import size  # noqa: E402  (trader-budget/size.py)
import check as risk  # noqa: E402  (trader-risk/check.py)
import capital  # noqa: E402  (scripts/capital.py: ENFORCE switch for the pool caps)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
BOOK_PATH = os.path.join(BASE_DIR, "trader", "state", "ai_book.json")
POOLS = {"trading": "intraday", "investing": "positional"}
PREVIEW_TTL_MIN = 10          # a preview must be placed within this many minutes, else re-preview
TICK = 0.05                   # NSE equity / option tick for the paper slippage
MAX_DRIFT_R = 0.25            # refuse a fill this far (in R) above the previewed entry: it is a new trade
FLAT_BY = datetime.time(15, 20)
MAX_HOLD_DAYS = 60
_LOCK = threading.Lock()


class BookError(Exception):
    """A refusal the AI should read and act on (not a crash)."""


def now_ist():
    return datetime.datetime.now(IST)


# ---------------------------------------------------------------- persistence

def load(path=None):
    path = path or BOOK_PATH
    if not os.path.exists(path):
        return {"positions": [], "orders": [], "previews": {}, "journal": []}
    with open(path, encoding="utf-8") as fh:
        b = json.load(fh)
    for k, v in (("positions", []), ("orders", []), ("previews", {}), ("journal", [])):
        b.setdefault(k, v)
    return b


def save(book, path=None):
    path = path or BOOK_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(book, fh, indent=1, default=str)
    os.replace(tmp, path)


class Book(object):
    """One paper account with two pools. `provider` and `clock` are injectable for tests."""

    def __init__(self, path=None, provider=None, cfg=None, clock=None, options_runner=None, instruments_path=None):
        self.path = path or BOOK_PATH
        self.instruments_path = instruments_path      # None = the default instrument master
        self._provider = provider
        self.cfg = cfg if cfg is not None else size.load_config(None, market="IN")
        self.clock = clock or now_ist
        self.options_runner = options_runner or run_options_worker

    # ------------------------------------------------------------ helpers
    @property
    def provider(self):
        if self._provider is None:
            import data_in
            self._provider = data_in.get_provider()
        return self._provider

    def cap(self, pool):
        if pool not in POOLS:
            raise BookError("pool must be 'trading' or 'investing', not %r" % pool)
        v = float(self.cfg.get("budget") or 0)
        if v <= 0:
            raise BookError("config has no positive 'budget' for the pool cap")
        return v

    def open_positions(self, book, pool=None):
        return [p for p in book["positions"] if p["status"] == "OPEN" and (pool is None or p["pool"] == pool)]

    def committed(self, book, pool):
        return sum(float(p["capital_inr"]) for p in self.open_positions(book, pool))

    def room(self, book, pool):
        return max(0.0, self.cap(pool) - self.committed(book, pool))

    def sizing_room(self, book, pool):
        """Cash a new trade may use: the pool's free capital, or (capital.ENFORCE off, testing stage)
        the whole pool cap, so open positions never refuse or shrink a new one. room() still reports
        the real free capital."""
        return self.room(book, pool) if capital.ENFORCE else self.cap(pool)

    def pool_state(self, book, pool):
        """trader-risk's broker-state view of ONE pool (its own daily loss / trades limits)."""
        return {"orders": [o for o in book["orders"] if o.get("pool") == pool]}

    def quote(self, symbol):
        q = self.provider.get_quote(symbol)
        px = q.get("price") or q.get("last_price")
        if px is None or float(px) <= 0:
            raise BookError("no price for %s from %s" % (symbol, q.get("provider") or "the provider"))
        return float(px), q.get("data_status") or q.get("tier") or "UNKNOWN"

    def journal(self, book, event, **kw):
        book["journal"].append(dict(kw, event=event, at=self.clock().strftime("%Y-%m-%d %H:%M:%S")))

    # ------------------------------------------------------------ equity
    def preview_equity(self, pool, symbol, stop, target, entry=None, setup="ai_discretionary"):
        """Size + risk-check a long equity trade. Returns the sized order, the trader-risk verdict and,
        on PASS, a preview_id to place within PREVIEW_TTL_MIN minutes. Nothing is recorded as a trade."""
        style = POOLS.get(pool)
        if style is None:
            raise BookError("pool must be 'trading' (intraday) or 'investing' (multi-day)")
        sym = str(symbol).strip().upper()
        price, tier = self.quote(sym)
        entry = float(entry) if entry is not None else price
        stop, target = float(stop), float(target)
        if not stop < entry < target:
            raise BookError("long only: need stop < entry < target (got stop %.2f, entry %.2f, target %.2f)"
                            % (stop, entry, target))
        with _LOCK:
            book = load(self.path)
            room = self.sizing_room(book, pool)
            if room <= 0:
                raise BookError("CAPITAL_CAP: the %s pool's Rs %.0f is fully committed" % (pool, self.cap(pool)))
            now = self.clock()
            plan = {"symbol": sym, "side": "buy", "setup": setup, "entry": entry, "stop": stop, "target": target,
                    "asset_type": "EQUITY", "trading_style": style, "market": "IN", "exchange": "NSE",
                    "currency": "INR", "provider": "ai_book", "data_status": tier,
                    "flat_by": "positional" if style == "positional" else "15:20"}
            try:
                s = size.size_trade(plan, self.cfg, cash=room, today=now.date(), market="IN",
                                    trading_style=style, exchange="NSE", instruments_path=self.instruments_path)
            except Exception as e:
                raise BookError("BUDGET: %s" % str(e).strip().splitlines()[-1])
            if s.get("refusal") or not s.get("qty"):
                raise BookError("BUDGET: %s" % (s.get("refusal") or "qty 0 within Rs %.0f free" % room))
            order = size.build_order(s, self.cfg)
            order["trading_style"] = style
            positions = [{"symbol": p["symbol"], "qty": p["qty"]} for p in self.open_positions(book, pool)
                         if p["kind"] == "equity"]
            now_utc = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            res = risk.check([order], self.cfg, portfolio={"cash": room, "positions": positions},
                             prices={sym: price}, state=self.pool_state(book, pool), now=now_utc,
                             price_times={sym: now_utc}, market="IN", instruments_path=self.instruments_path)
            r = res["results"][0]
            out = {"verdict": r["verdict"], "reasons": r["reasons"], "warnings": r["warnings"] + res["warnings"],
                   "pool": pool, "style": style, "symbol": sym, "qty": s["qty"], "entry": s["entry"],
                   "stop": s["stop"], "target": s["target"], "capital_inr": round(s["qty"] * s["entry"], 2),
                   "max_loss_inr": s.get("net_risk_including_costs"), "net_reward_risk": s.get("net_reward_risk"),
                   "est_charges_inr": (s.get("transaction_costs") or {}).get("total"),
                   "last_price": price, "data_status": tier, "pool_free_inr": round(self.room(book, pool), 2),
                   "capital_cap_enforced": bool(capital.ENFORCE)}
            if r["verdict"] == "PASS":
                pid = "EQ-" + hashlib.sha256(("%s|%s" % (r["fingerprint"], now.isoformat())).encode()).hexdigest()[:10]
                book["previews"][pid] = dict(out, kind="equity", fingerprint=r["fingerprint"], order=order,
                                             expires=(now + datetime.timedelta(minutes=PREVIEW_TTL_MIN)).isoformat())
                out["preview_id"] = pid
                save(book, self.path)
            return out

    # ------------------------------------------------------------ options (buy side, validated only)
    def preview_options(self, underlying, budget=None):
        """Run the trader-options specialist on one index/F&O stock and return its BUY_CE/BUY_PE signals.
        Only SIGNAL VALIDATED + actionable buys get a preview_id; the rest come back with their
        rejection reasons for the AI to read. Spreads/sells are never placeable here."""
        budget = float(budget or self.cfg.get("max_loss_per_trade_inr") or 2000.0)
        sigs = self.options_runner(str(underlying).strip().upper(), budget)
        out = []
        with _LOCK:
            book = load(self.path)
            now = self.clock()
            room = self.sizing_room(book, "trading")
            for s in sigs:
                strat = str(s.get("strategy") or "")
                ps = s.get("position_sizing") or {}
                row = {"strategy": strat, "contract": s.get("contract"), "underlying": s.get("underlying_instrument"),
                       "expiry": s.get("expiry"), "strike": s.get("strike"), "option_type": s.get("option_type"),
                       "premium": s.get("current_premium"), "stop": s.get("stop_loss"), "target": s.get("target_2"),
                       "target_1": s.get("target_1"), "lots": ps.get("lots"), "qty": ps.get("total_quantity"),
                       "capital_inr": s.get("capital_margin_requirement"), "max_loss_inr": s.get("maximum_loss"),
                       "net_reward_risk": s.get("expected_risk_reward_after_charges"),
                       "signal_state": s.get("signal_state"),
                       "reasons": [str(x)[:200] for x in (s.get("rejection_reasons") or [])][:6]}
                ok = bool(s.get("_validated")) and strat in ("BUY_CE", "BUY_PE")
                if ok and None in (row["premium"], row["stop"], row["target"], row["qty"], row["capital_inr"],
                                   row["max_loss_inr"]):
                    ok = False
                    row["reasons"].append("INCOMPLETE_SIGNAL: missing premium/stop/target/qty/capital")
                if ok and float(row["capital_inr"]) > room + 1e-6:
                    ok = False
                    row["reasons"].append("CAPITAL_CAP: needs Rs %.0f, trading pool has Rs %.0f free"
                                          % (float(row["capital_inr"]), room))
                if ok:
                    pid = "OP-" + hashlib.sha256(("%s|%s|%s" % (row["contract"], strat, now.isoformat())).encode()
                                                 ).hexdigest()[:10]
                    book["previews"][pid] = dict(row, kind="option", pool="trading", verdict="PASS",
                                                 expires=(now + datetime.timedelta(minutes=PREVIEW_TTL_MIN)).isoformat())
                    row["preview_id"] = pid
                    row["verdict"] = "PASS"
                else:
                    row["verdict"] = "BLOCK"
                out.append(row)
            save(book, self.path)
        return out

    def _option_limits(self, book, pv, today):
        """trader-risk's daily loss and trades/day limits applied to an option buy in the trading pool."""
        reasons = []
        recs = [o for o in self.pool_state(book, "trading")["orders"]
                if o.get("kind") == "entry" and o.get("date") == today]
        daily = float(self.cfg.get("max_daily_loss_inr") or 0)
        used = sum(float(o.get("max_loss") or 0) for o in recs)
        if daily and used + float(pv["max_loss_inr"]) > daily + 0.01:
            reasons.append("daily loss: Rs %.0f already at risk today + Rs %.0f > Rs %.0f limit"
                           % (used, float(pv["max_loss_inr"]), daily))
        cap_t = int(self.cfg.get("max_trades_per_day") or 0)
        if cap_t and len(recs) >= cap_t:
            reasons.append("trades: %d entries today, limit %d" % (len(recs), cap_t))
        per = float(self.cfg.get("max_loss_per_trade_inr") or 0)
        if per and float(pv["max_loss_inr"]) > per + 0.01:
            reasons.append("loss: Rs %.0f over the Rs %.0f per-trade limit" % (float(pv["max_loss_inr"]), per))
        cutoff = risk.hhmm(self.cfg.get("no_entries_after") or "15:00", "no_entries_after")
        if self.clock().time() > cutoff:
            reasons.append("window: after the %s no-new-entries cutoff" % cutoff.strftime("%H:%M"))
        return reasons

    # ------------------------------------------------------------ place / close
    def place(self, preview_id, rationale):
        """Paper-fill a PASSing preview at the live price (+1 tick). `rationale` is required: it is the
        AI's reason, kept in the journal next to the outcome so the AI's judgement can be scored."""
        if not rationale or len(str(rationale).strip()) < 10:
            raise BookError("rationale required: say in a sentence or two why you are taking this trade")
        with _LOCK:
            book = load(self.path)
            pv = book["previews"].get(preview_id)
            if pv is None:
                raise BookError("unknown preview_id %r (preview again)" % preview_id)
            now = self.clock()
            if now > datetime.datetime.fromisoformat(pv["expires"]):
                del book["previews"][preview_id]
                save(book, self.path)
                raise BookError("preview %s expired; prices move -- preview again" % preview_id)
            today = now.date().isoformat()
            if pv["kind"] == "equity":
                pos = self._fill_equity(book, pv, now)
            else:
                reasons = self._option_limits(book, pv, today)
                if reasons:
                    raise BookError("BLOCK: " + "; ".join(reasons))
                fill = round(float(pv["premium"]) + TICK, 2)
                pos = {"kind": "option", "pool": "trading", "style": "intraday", "symbol": pv["contract"],
                       "underlying": pv["underlying"], "expiry": pv["expiry"], "strike": pv["strike"],
                       "option_type": pv["option_type"], "strategy": pv["strategy"], "qty": int(pv["qty"]),
                       "entry": fill, "stop": float(pv["stop"]), "target": float(pv["target"]),
                       "capital_inr": round(fill * int(pv["qty"]), 2), "max_loss_inr": float(pv["max_loss_inr"])}
            pos.update(id="%s-%s-%d" % (today, pos["symbol"], len(book["positions"]) + 1), status="OPEN",
                       opened_at=now.strftime("%Y-%m-%d %H:%M:%S"), rationale=str(rationale).strip(),
                       preview_id=preview_id)
            book["positions"].append(pos)
            book["orders"].append({"pool": pos["pool"], "status": "placed", "kind": "entry", "side": "buy",
                                   "symbol": pos["symbol"], "qty": pos["qty"], "date": today,
                                   "at": now.isoformat(), "max_loss": pos["max_loss_inr"], "position_id": pos["id"]})
            del book["previews"][preview_id]
            self.journal(book, "OPEN", position_id=pos["id"], symbol=pos["symbol"], pool=pos["pool"],
                         entry=pos["entry"], stop=pos["stop"], target=pos["target"], qty=pos["qty"],
                         rationale=pos["rationale"])
            save(book, self.path)
            return pos

    def _fill_equity(self, book, pv, now):
        price, tier = self.quote(pv["symbol"])
        fill = round(price + TICK, 2)
        r = float(pv["entry"]) - float(pv["stop"])
        if fill <= float(pv["stop"]):
            raise BookError("price %.2f is already at/below the stop %.2f -- the setup failed" % (price, pv["stop"]))
        if fill > float(pv["entry"]) + MAX_DRIFT_R * r:
            raise BookError("price %.2f ran %.2fR past the previewed entry %.2f -- chasing; preview again"
                            % (price, (fill - float(pv["entry"])) / r, pv["entry"]))
        # The fill can't add risk beyond the per-trade limit (the drift guard above keeps it small).
        per = float(self.cfg.get("max_loss_per_trade_inr") or 0)
        if per and int(pv["qty"]) * (fill - float(pv["stop"])) > per + 0.01:
            raise BookError("at %.2f the loss to the stop would be Rs %.0f, over the Rs %.0f per-trade limit; preview again"
                            % (fill, int(pv["qty"]) * (fill - float(pv["stop"])), per))
        # Re-run trader-risk on the exact previewed order at its previewed price (the order's charge
        # estimate is tied to it): daily loss / trades / window / capital must STILL pass now -- other
        # trades may have been placed since the preview.
        pool = pv["pool"]
        positions = [{"symbol": p["symbol"], "qty": p["qty"]} for p in self.open_positions(book, pool)
                     if p["kind"] == "equity"]
        now_utc = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        res = risk.check([pv["order"]], self.cfg, portfolio={"cash": self.sizing_room(book, pool), "positions": positions},
                         prices={pv["symbol"]: float(pv["entry"])}, state=self.pool_state(book, pool), now=now_utc,
                         price_times={pv["symbol"]: now_utc}, market="IN", instruments_path=self.instruments_path)
        r0 = res["results"][0]
        if r0["verdict"] != "PASS":
            raise BookError("BLOCK at fill: " + "; ".join(r0["reasons"]))
        if r0["fingerprint"] != pv["fingerprint"]:
            raise BookError("order fingerprint changed since the preview (limits or config changed); preview again")
        qty = int(pv["qty"])
        return {"kind": "equity", "pool": pool, "style": pv["style"], "symbol": pv["symbol"], "qty": qty,
                "entry": fill, "stop": float(pv["stop"]), "target": float(pv["target"]),
                "capital_inr": round(fill * qty, 2), "max_loss_inr": pv["max_loss_inr"], "data_status": tier}

    def _charges(self, pos, exit_px, day):
        asset = "OPTIONS" if pos["kind"] == "option" else "EQUITY"
        c = size.calculate_indian_transaction_charges(asset, "buy", float(pos["entry"]), float(exit_px), int(pos["qty"]),
                                                      pos["style"], day)
        return float(c.get("total") if isinstance(c, dict) else (c or 0))

    def _close(self, book, pos, exit_px, reason, now):
        day = now.date().isoformat()
        gross = (float(exit_px) - float(pos["entry"])) * int(pos["qty"])
        net = gross - self._charges(pos, exit_px, day)
        risk_inr = (float(pos["entry"]) - float(pos["stop"])) * int(pos["qty"])
        pos.update(status="CLOSED", exit=round(float(exit_px), 2), exit_reason=reason,
                   closed_at=now.strftime("%Y-%m-%d %H:%M:%S"), gross_inr=round(gross, 2), net_inr=round(net, 2),
                   R=round(gross / risk_inr, 2) if risk_inr > 0 else None)
        book["orders"].append({"pool": pos["pool"], "status": "filled", "kind": "exit", "side": "sell",
                               "symbol": pos["symbol"], "qty": pos["qty"], "date": day, "at": now.isoformat(),
                               "pnl": round(net, 2), "position_id": pos["id"]})
        self.journal(book, "CLOSE", position_id=pos["id"], symbol=pos["symbol"], pool=pos["pool"], reason=reason,
                     exit=pos["exit"], net_inr=pos["net_inr"], R=pos["R"])
        return pos

    def option_price(self, pos):
        ch = self.provider.get_option_chain(pos["underlying"], expiry=pos["expiry"])
        ch = ch.to_dict() if hasattr(ch, "to_dict") else ch
        for s in ch.get("strikes") or []:
            if abs(float(s.get("strike") or 0) - float(pos["strike"])) < 1e-6:
                leg = s.get(str(pos["option_type"]).lower()) or {}
                px = leg.get("bid") or leg.get("last_price")
                if px:
                    return float(px)
        raise BookError("no chain price for %s" % pos["symbol"])

    def close(self, position_id, reason):
        """Close an open position at the live price (-1 tick). `reason` is required (journal)."""
        if not reason or len(str(reason).strip()) < 5:
            raise BookError("reason required for a discretionary exit")
        with _LOCK:
            book = load(self.path)
            pos = next((p for p in self.open_positions(book) if p["id"] == position_id), None)
            if pos is None:
                raise BookError("no open position %r" % position_id)
            px = self.option_price(pos) if pos["kind"] == "option" else self.quote(pos["symbol"])[0]
            out = self._close(book, pos, max(TICK, round(px - TICK, 2)), "AI: " + str(reason).strip(), self.clock())
            save(book, self.path)
            return out

    # ------------------------------------------------------------ deterministic exits
    def mark(self):
        """Walk every open position: stop / target from bars since entry (stop checked first), intraday
        flat by 15:20, positional time stop after MAX_HOLD_DAYS. Runs without the AI -- exits are rules,
        not opinions. Returns the list of positions it closed plus marks for the rest."""
        closed, marks = [], []
        with _LOCK:
            book = load(self.path)
            now = self.clock()
            for pos in self.open_positions(book):
                try:
                    hit = self._walk(pos, now)
                except Exception as e:
                    marks.append({"id": pos["id"], "symbol": pos["symbol"], "error": str(e)[:160]})
                    continue
                if hit:
                    closed.append(self._close(book, pos, hit[0], hit[1], now))
                else:
                    marks.append({"id": pos["id"], "symbol": pos["symbol"], "last": pos.get("last"),
                                  "open_R": pos.get("open_R")})
            save(book, self.path)
        return {"closed": closed, "open": marks}

    def _walk(self, pos, now):
        stop, target, entry = float(pos["stop"]), float(pos["target"]), float(pos["entry"])
        opened = datetime.datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S")
        if pos["kind"] == "option":
            px = self.option_price(pos)
            bars = [{"l": px, "h": px, "c": px, "o": px}]
        elif pos["style"] == "intraday":
            d = self.provider.get_intraday_data(pos["symbol"], interval="5m", range_str="1d")
            day = opened.date().isoformat()
            bars = [b for b in (d.get("bars") or []) if b.get("date") == day and b.get("start", "") >= opened.strftime("%H:%M")]
        else:
            d = self.provider.get_historical_data(pos["symbol"], range_str="3mo", interval="1d")
            bars = [b for b in (d.get("bars") or []) if b.get("date", "") > opened.date().isoformat()]
        for b in bars:
            if b.get("l") is not None and float(b["l"]) <= stop:
                o = b.get("o")
                return (min(stop, float(o)) if o is not None else stop) - TICK, "STOP"
            if b.get("h") is not None and float(b["h"]) >= target:
                return target - TICK, "TARGET"
        last = float(bars[-1]["c"]) if bars else None
        if last is not None:
            pos["last"] = last
            pos["open_R"] = round((last - entry) / (entry - stop), 2) if entry > stop else None
        if pos["style"] == "intraday" and (now.date() > opened.date() or now.time() >= FLAT_BY):
            if last is None:
                raise BookError("flat-by due but no price for %s" % pos["symbol"])
            return last - TICK, "FLAT_BY"
        if pos["style"] == "positional" and last is not None:
            held = len(bars)
            if held >= int(pos.get("max_hold_days") or MAX_HOLD_DAYS):
                return last - TICK, "TIME"
        return None

    # ------------------------------------------------------------ views
    def summary(self):
        book = load(self.path)
        today = self.clock().date().isoformat()
        out = {"paper_only": True, "pools": {}}
        for pool in POOLS:
            closed = [p for p in book["positions"] if p["pool"] == pool and p["status"] == "CLOSED"]
            today_net = sum(p["net_inr"] for p in closed if str(p.get("closed_at", ""))[:10] == today)
            entries_today = [o for o in book["orders"] if o.get("pool") == pool and o.get("kind") == "entry"
                             and o.get("date") == today]
            out["pools"][pool] = {
                "cap_inr": self.cap(pool), "committed_inr": round(self.committed(book, pool), 2),
                "free_inr": round(self.room(book, pool), 2),
                "realized_net_inr": round(sum(p["net_inr"] for p in closed), 2),
                "today_net_inr": round(today_net, 2), "entries_today": len(entries_today),
                "risk_at_stops_today_inr": round(sum(float(o.get("max_loss") or 0) for o in entries_today), 2),
                "trades_closed": len(closed), "wins": sum(1 for p in closed if p["net_inr"] > 0),
                "open": self.open_positions(book, pool)}
        out["limits"] = {k: self.cfg.get(k) for k in ("max_loss_per_trade_inr", "max_daily_loss_inr",
                                                       "max_trades_per_day", "min_reward_risk", "no_entries_after")}
        return out

    def journal_tail(self, n=20):
        return load(self.path)["journal"][-int(n):]


def run_options_worker(sym, budget):
    """The trader-options specialist in its own subprocess with a hard timeout (same isolation as
    scripts/market_watch.py). Returns its signal list, each flagged `_validated`."""
    cmd = [sys.executable, os.path.join(BASE_DIR, "scripts", "market_watch.py"), "--options-worker", sym,
           "--budget", str(budget)]
    out = subprocess.run(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, timeout=120)
    if out.returncode != 0:
        raise BookError("options analysis failed: %s" % (out.stderr.strip().splitlines() or ["exit %d" % out.returncode])[-1])
    return json.loads(out.stdout[out.stdout.index("["):])
