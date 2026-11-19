#!/usr/bin/env python3
"""Options Calls -- positional credit spreads held to expiry (separate from the equity utility).
Decision support and paper tracking only; never places orders.

    python3 scripts/options_watch.py run        # stays up: checks the rules at 15:10 IST and tracks at 15:40, every trading day
    python3 scripts/options_watch.py check      # check the rules now (alerts only on a rule's entry day)
    python3 scripts/options_watch.py preview    # what each rule would sell right now, whatever the day
    python3 scripts/options_watch.py track      # mark open paper spreads to the chain; settle at expiry
    python3 scripts/options_watch.py show       # schedule + paper ledger

Rules (trader-options/positional.py RULES) are the only variants that passed the credit-spread
backtest (scripts/backtest_spreads.py) under both time assumptions:
  * NIFTY monthly Bear Call: 5 trading days before the monthly expiry, sell the ~0.20-delta call,
    buy the call 2 strikes higher, hold to expiry.
  * BANKNIFTY monthly trend spread: 10 trading days before expiry, ~0.20 delta, 2 strikes wide;
    Bull Put if BANKNIFTY is above its 20-day average, else Bear Call.
That backtest prices options with a MODEL (Black-Scholes on India VIX), not real past quotes, so
every alert is labelled MODEL-VALIDATED (paper) and tracked at real chain prices to expiry --
the ledger is how we find out if the edge is real.

Risk: a spread may lose up to Rs 6,000 per lot (user-approved for spreads only, 2026-11-19), and
only ONE spread is open at a time (one worst case already exceeds the Rs 5,000 daily limit).
"""

import argparse
import csv
import datetime
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-budget", "trader-options", "config", "scripts"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import positional as pos  # noqa: E402  (trader-options/positional.py)
import capital  # noqa: E402  (scripts/capital.py: Rs 1,00,000 total-capital hard cap)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
STATE = os.path.join(BASE_DIR, "trader", "state")
CALLS_CSV = os.path.join(STATE, "options_calls.csv")
LEDGER_CSV = os.path.join(STATE, "options_ledger.csv")
REPORTS = (os.path.join(STATE, "bt_cache", "spreads_report.json"),
           os.path.join(STATE, "bt_cache", "spreads_report_trading.json"))
SPREAD_BUDGET = 6000.0
MAX_OPEN = 1
CHECK_AT, TRACK_AT = datetime.time(15, 10), datetime.time(15, 40)
LABEL = "MODEL-VALIDATED (paper)"
LEDGER_FIELDS = ("id", "rule", "symbol", "strategy", "side", "expiry", "opened", "short_strike", "long_strike",
                 "credit", "lot", "max_loss_inr", "status", "mark_cost", "mark_inr", "mark_at", "closed",
                 "settle_spot", "net_inr")
CALL_FIELDS = ("time", "rule", "symbol", "strategy", "expiry", "short", "long", "credit", "max_profit_inr",
               "max_loss_inr", "breakeven", "spot", "short_delta", "odds", "status", "reasons")


def now_ist():
    return datetime.datetime.now(IST)


def emit(line):
    print("%s %s" % (now_ist().strftime("%H:%M:%S"), line), flush=True)


def notify(title, msg):
    import subprocess
    try:
        subprocess.Popen(["osascript", "-e", 'display notification "%s" with title "%s"'
                          % (msg.replace('"', "'")[:220], title.replace('"', "'"))],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["say", "Options spread call"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------- odds

def odds_for(rule, reports=REPORTS):
    """The rule's backtest numbers (worse of the two time bases), or None."""
    want = {"sym": rule["symbol"], "expiry": rule["expiry"], "days_before": rule["days_before"],
            "side": rule["side"], "delta": rule["delta"], "width_steps": rule["width_steps"], "stop_x_credit": None}
    found = []
    for path in reports:
        try:
            with open(path) as fh:
                for r in json.load(fh):
                    if r["variant"] == want:
                        found.append(r)
        except (OSError, ValueError):
            continue
    if not found:
        return None
    w = min(found, key=lambda r: r["holdout"].get("avg") or 0)
    a, h = w["all"], w["holdout"]
    return {"win%": a["win%"], "avg": a["avg"], "holdout_avg": h["avg"], "worst": a["worst"],
            "avg_loss": a["avg_loss"], "n": a["n"], "max_dd": a["max_dd"]}


def odds_text(o):
    if not o:
        return "no backtest numbers"
    return ("backtest (model, %d months): wins %.0f%% | avg %+.0f/lot (2023+: %+.0f) | avg loss %+.0f | worst %+.0f | "
            "worst run %+.0f" % (o["n"], o["win%"], o["avg"], o["holdout_avg"], o["avg_loss"], o["worst"], o["max_dd"]))


# ---------------------------------------------------------------- csv

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, fields, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def append_call(row):
    rows = read_csv(CALLS_CSV) + [row]
    write_csv(CALLS_CSV, CALL_FIELDS, rows)


# ---------------------------------------------------------------- core

def provider():
    import data_in
    return data_in.get_provider()


def lot_of(symbol):
    import instruments
    lot = instruments.get_fo_lot_size(symbol)
    if not lot:
        raise pos.SpreadError("no authoritative lot size for %s" % symbol)
    return int(lot)


def side_for(rule, p):
    if rule["side"] in ("CE", "PE"):
        return rule["side"]
    d = p.get_historical_data(rule["symbol"], range_str="3mo", interval="1d")
    closes = [b["c"] for b in (d.get("bars") or []) if b.get("c") is not None]
    return pos.trend_side(closes)


def evaluate(rule, expiry, p=None, as_of=None):
    """Build the rule's spread from the live chain for `expiry`. Returns (spread|None, reasons)."""
    p = p or provider()
    try:
        side = side_for(rule, p)
        chain = p.get_option_chain(rule["symbol"], expiry=expiry.isoformat())
        ch = chain.to_dict() if hasattr(chain, "to_dict") else chain
        if str(ch.get("tier") or "").upper() not in ("REAL_TIME", "CURRENT", "LIVE"):
            raise pos.SpreadError("chain is %s, not live -- prices can't be trusted for a call" % ch.get("tier"))
        sp = pos.build_spread(ch, side, rule["delta"], rule["width_steps"], lot_of(rule["symbol"]),
                              pos.STEPS[rule["symbol"]], SPREAD_BUDGET, as_of=as_of)
        return sp, None
    except pos.SpreadError as e:
        return None, str(e)
    except Exception as e:
        return None, "DATA: %s: %s" % (type(e).__name__, str(e)[:160])


def open_rows(ledger):
    return [r for r in ledger if r.get("status") == "OPEN"]


def capital_free():
    """(free_inr, cap_inr) in the TRADING pool's hard cap. Today's intraday calls are left out: the
    spread is held past today's 15:20 square-off. Unreadable config/state -> (0, cap) (fail closed)."""
    import size
    cap = capital.cap_inr(size.load_config(None, market="IN"), "trading")
    try:
        used = capital.committed("trading", cap, opportunities=None, options_ledger=LEDGER_CSV)[0]
    except Exception:
        return 0.0, cap
    return max(0.0, cap - used), cap


def check(preview=False, today=None):
    today = today or now_ist().date()
    ledger = read_csv(LEDGER_CSV)
    p = None
    for rule in pos.RULES:
        expiry, entry_day = pos.entry_expiry(today, rule)
        o = odds_for(rule)
        if not preview and entry_day != today:
            emit("%s: next entry %s for the %s expiry (%d trading days before)." % (
                rule["name"], entry_day, expiry, rule["days_before"]))
            continue
        p = p or provider()
        sp, why = evaluate(rule, expiry, p, as_of=today)
        rid = "%s|%s" % (rule["name"], expiry)
        if sp is None:
            emit("%s %s: NO CALL -- %s" % (rule["name"], expiry, why))
            if not preview:
                append_call({"time": now_ist().strftime("%Y-%m-%d %H:%M"), "rule": rule["name"], "symbol": rule["symbol"],
                             "expiry": expiry, "status": "REJECTED", "reasons": why, "odds": odds_text(o)})
            continue
        text = summary(rule, sp, o)
        if preview:
            emit("PREVIEW (not a call: today is not the entry day) " + text)
            continue
        status, reason = "CALL", ""
        if any(r.get("id") == rid for r in ledger):
            status, reason = "DUPLICATE", "already in the ledger"
        elif len(open_rows(ledger)) >= MAX_OPEN:
            status, reason = "OPEN_RISK", "one spread already open (one worst case exceeds the Rs 5,000 daily limit)"
        else:
            free, cap = capital_free()
            if float(sp["max_loss_inr"]) > free + 1e-6:
                status, reason = "CAPITAL_CAP", ("needs Rs %.0f (max loss ~ blocked margin) but only Rs %.0f of the "
                                                 "Rs %.0f capital cap is free" % (float(sp["max_loss_inr"]), free, cap))
        append_call({"time": now_ist().strftime("%Y-%m-%d %H:%M"), "rule": rule["name"], "symbol": rule["symbol"],
                     "strategy": sp["strategy"], "expiry": expiry, "short": "SELL %s" % sp["short"]["symbol"],
                     "long": "BUY %s" % sp["long"]["symbol"], "credit": sp["credit"], "max_profit_inr": sp["max_profit_inr"],
                     "max_loss_inr": sp["max_loss_inr"], "breakeven": sp["breakeven"], "spot": sp["underlying_price"],
                     "short_delta": sp["short_delta"], "odds": odds_text(o), "status": status, "reasons": reason})
        if status != "CALL":
            emit("%s: %s -- %s | %s" % (rule["name"], status, reason, text))
            continue
        ledger.append({"id": rid, "rule": rule["name"], "symbol": rule["symbol"], "strategy": sp["strategy"],
                       "side": sp["side"], "expiry": expiry.isoformat(), "opened": now_ist().strftime("%Y-%m-%d %H:%M"),
                       "short_strike": sp["short"]["strike"], "long_strike": sp["long"]["strike"], "credit": sp["credit"],
                       "lot": sp["lot"], "max_loss_inr": sp["max_loss_inr"], "status": "OPEN"})
        write_csv(LEDGER_CSV, LEDGER_FIELDS, ledger)
        emit("OPTIONS CALL " + text)
        notify("%s spread call (%s)" % (rule["symbol"], LABEL),
               "SELL %s / BUY %s, credit %.2f, max loss Rs %.0f" % (sp["short"]["symbol"], sp["long"]["symbol"],
                                                                   sp["credit"], sp["max_loss_inr"]))
    return 0


def summary(rule, sp, o):
    name = "Bear Call Spread" if sp["side"] == "CE" else "Bull Put Spread"
    return ("[%s] %s %s exp %s | SELL %s (Δ%.2f) @ %.2f bid + BUY %s @ %.2f ask | credit %.2f x %d = Rs %.0f max profit "
            "(after charges) | max loss Rs %.0f | breakeven %.2f (spot %.2f) | hold to expiry | %s"
            % (LABEL, rule["symbol"], name, sp["expiry"], sp["short"]["symbol"], abs(sp["short_delta"] or 0),
               sp["short"]["bid"], sp["long"]["symbol"], sp["long"]["ask"], sp["credit"], sp["lot"], sp["max_profit_inr"],
               sp["max_loss_inr"], sp["breakeven"], sp["underlying_price"], odds_text(o)))


def track(today=None):
    """Mark open spreads to the live chain (cost to close = short ask - long bid); on/after expiry,
    settle at the index's expiry-day close (intrinsic)."""
    today = today or now_ist().date()
    ledger = read_csv(LEDGER_CSV)
    if not open_rows(ledger):
        emit("no open spreads")
        return 0
    p = provider()
    import size
    for r in open_rows(ledger):
        exp = datetime.date.fromisoformat(r["expiry"])
        sk, lk, side = float(r["short_strike"]), float(r["long_strike"]), r["side"]
        lot, credit = int(float(r["lot"])), float(r["credit"])
        try:
            if today >= exp:
                bars = p.get_historical_data(r["symbol"], range_str="1mo", interval="1d").get("bars") or []
                spot = next((b["c"] for b in bars if b.get("date") == r["expiry"]), None)
                if spot is None:
                    emit("%s: expiry-day close not available yet; will settle on the next run" % r["id"])
                    continue
                cost = pos.settle_value(float(spot), sk, lk, side)
                chg = 0.0
                for sell, buy in ((credit, max(cost, 0.05)), (0.05, 0.05)):
                    c = size.calculate_indian_transaction_charges("OPTIONS", "buy", buy, sell, lot, "intraday", r["expiry"])
                    chg += c.get("total") if isinstance(c, dict) else float(c or 0)
                net = (credit - cost) * lot - chg
                r.update(status="WIN" if net > 0 else "LOSS", closed=r["expiry"], settle_spot=spot, net_inr=round(net, 0),
                         mark_cost=round(cost, 2))
                emit("SETTLED %s at spot %.2f: spread worth %.2f -> net Rs %+.0f" % (r["id"], spot, cost, net))
            else:
                ch = p.get_option_chain(r["symbol"], expiry=r["expiry"]).to_dict()
                rows = {float(s["strike"]): s for s in ch.get("strikes") or []}
                s_leg, l_leg = pos._leg(rows.get(sk), side), pos._leg(rows.get(lk), side)
                if s_leg["ask"] is None or l_leg["bid"] is None:
                    emit("%s: no quotes to mark" % r["id"])
                    continue
                cost = max(0.0, s_leg["ask"] - l_leg["bid"])
                r.update(mark_cost=round(cost, 2), mark_inr=round((credit - cost) * lot, 0),
                         mark_at=now_ist().strftime("%Y-%m-%d %H:%M"))
                emit("%s: credit %.2f, cost to close now %.2f -> open P&L Rs %+.0f (spot %.2f)"
                     % (r["id"], credit, cost, (credit - cost) * lot, ch.get("underlying_price") or 0))
        except Exception as e:
            emit("%s: tracking error %s: %s" % (r["id"], type(e).__name__, str(e)[:120]))
    write_csv(LEDGER_CSV, LEDGER_FIELDS, ledger)
    return 0


def show(today=None):
    today = today or now_ist().date()
    print("OPTIONS CALLS -- positional credit spreads, %s, paper only. Spread budget Rs %.0f/lot, %d open at a time."
          % (LABEL, SPREAD_BUDGET, MAX_OPEN))
    for rule in pos.RULES:
        expiry, entry_day = pos.entry_expiry(today, rule)
        print("  %-32s next entry %s for expiry %s | %s" % (rule["name"], entry_day, expiry, odds_text(odds_for(rule))))
    ledger = read_csv(LEDGER_CSV)
    done = [r for r in ledger if r.get("status") in ("WIN", "LOSS")]
    print("PAPER LEDGER: %d open, %d settled, settled net Rs %+.0f" % (
        len(open_rows(ledger)), len(done), sum(float(r.get("net_inr") or 0) for r in done)))
    for r in ledger:
        print("  %-5s %-40s %s %s/%s credit %s max loss %s mark %s net %s" % (
            r["status"], r["id"], r["strategy"], r["short_strike"], r["long_strike"], r["credit"], r["max_loss_inr"],
            r.get("mark_inr") or "-", r.get("net_inr") or "-"))
    return 0


def run():
    import calendar_in
    emit("OPTIONS WATCH start: positional credit spreads (%s); rule check %s IST, tracking %s IST on trading days. "
         "Paper only; never places orders." % (LABEL, CHECK_AT.strftime("%H:%M"), TRACK_AT.strftime("%H:%M")))
    show()
    done = set()
    while True:
        n = now_ist()
        ok, _, _ = calendar_in.is_trading_day_in(n.date(), exchange="NSE")
        for name, at, fn in (("check", CHECK_AT, check), ("track", TRACK_AT, track)):
            key = (name, n.date())
            if ok and n.time() >= at and key not in done:
                done.add(key)
                try:
                    fn()
                except Exception as e:
                    emit("OPTIONS WATCH ERROR %s: %s: %s" % (name, type(e).__name__, str(e)[:160]))
        time.sleep(30)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("run", "check", "preview", "track", "show"))
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    if a.cmd == "run":
        return run()
    if a.cmd == "preview":
        return check(preview=True)
    return {"check": check, "track": track, "show": show}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
