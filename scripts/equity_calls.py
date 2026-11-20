#!/usr/bin/env python3
"""Equity Calls -- multi-day (up to ~3 months) long-only equity shortlist + paper ledger.
Paper only, long only; never places orders. Separate from the options watcher.

    python3 scripts/equity_calls.py scan     # after the close / weekends: shortlist + report + paper ledger
    python3 scripts/equity_calls.py track    # walk open paper calls with the latest daily bars
    python3 scripts/equity_calls.py show     # print the last report and the ledger
    python3 scripts/equity_calls.py run      # stays up: track + scan every trading day at 15:45 IST

What a line means
  * Setups come from scripts/swing_signals.py (chart formations, relative strength, results-gap
    surprises, pullbacks, the scanner's daily setups), detected on the last COMPLETED daily bar.
  * Plan: entry = next session's open (reference: last close), stop = the pattern's invalidation
    capped at 3 ATR, target = 2.5R, max hold 60 sessions. Sized by trader-budget size_trade
    (positional: Rs 2,000 max loss, the hard 2.0 net R:R floor, delivery charges).
  * Odds come from the 10-year backtest of that setup (scripts/backtest_swing.py,
    bt_cache/swing_report_2.5R.json), on the unseen 2023+ years.
  * Label: CALL only when that setup PASSED the backtest (beat random entries, net of charges, on
    design AND holdout years). Otherwise RESEARCH -- a shortlist for you to judge, not a call.
    (2026-11-19: no setup passes, so everything is RESEARCH.)
  * News (last 30 days) and the latest results are shown for context. They are NOT part of the
    odds -- they can't be backtested -- and never upgrade a label.
  * The paper ledger takes each new idea at the next open, as long as the open ideas' total
    worst-case loss stays within Rs 5,000 (OPEN_RISK otherwise) AND the INVESTING pool's capital stays
    within its own Rs 1,00,000 hard cap (separate from trading; CAPITAL_CAP otherwise -- see
    scripts/capital.py), and follows it to target / stop / 60 sessions.
"""

import argparse
import csv
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-scanner", "trader-budget", "trader-exit", "trader-news", "trader-analyst",
           "config", "scripts"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import swing_signals as sig  # noqa: E402
import capital  # noqa: E402  (scripts/capital.py: Rs 1,00,000 total-capital hard cap)
import upstox_speed  # noqa: E402  (per-process Upstox request pacing)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
STATE = os.path.join(BASE_DIR, "trader", "state")
UNIVERSE = os.path.join(BASE_DIR, "trader", "universe_nifty200.txt")
INSTRUMENTS = os.path.join(BASE_DIR, "trader", "state", "instruments.json")
ODDS_PATH = os.path.join(STATE, "bt_cache", "swing_report_2.5R.json")
CALLS_CSV = os.path.join(STATE, "equity_calls.csv")
REPORT_TXT = os.path.join(STATE, "equity_report.txt")
LEDGER_CSV = os.path.join(STATE, "equity_ledger.csv")
TARGET_R = 2.5
MAX_HOLD = 60
MAX_STOP_ATR = 3.0
OPEN_RISK_CAP = 5000.0
CONTEXT_TOP = 15            # news/results are fetched for the top N ideas only (they're slow)
CALL_FIELDS = ("scan_date", "symbol", "setup", "label", "close", "entry_ref", "stop", "target", "qty",
               "max_loss_inr", "net_rr", "hold_days", "p_target", "p_stop", "p_green", "avgR", "edge_vs_random",
               "odds_n", "regime_up", "ledger", "news", "results")
LEDGER_FIELDS = ("id", "symbol", "setup", "label", "signal_date", "status", "entry_date", "entry_ref", "entry", "stop", "target",
                 "qty", "max_loss_inr", "max_hold", "exit_date", "exit_price", "net_inr", "R")


def now_ist():
    return datetime.datetime.now(IST)


def tick(x, t=0.05):
    return round(round(x / t) * t, 2)


def floor_tick(x, t=0.05):
    import math
    return round(math.floor(x / t + 1e-9) * t, 2)


# ---------------------------------------------------------------- data

def universe(path=UNIVERSE):
    return [l.split("|")[0].strip() for l in open(path, encoding="utf-8") if l.strip()]


def company_names(path=UNIVERSE):
    out = {}
    for l in open(path, encoding="utf-8"):
        if "|" in l:
            s, n = l.strip().split("|", 1)
            out[s.strip()] = n.strip()
    return out


def short_name(name):
    """'GAIL (India) Ltd.' -> 'GAIL'; 'Asian Paints Ltd.' -> 'Asian Paints'."""
    import re
    n = re.sub(r"\(.*?\)", " ", name or "")
    n = re.sub(r"\b(Ltd|Limited|Ltd\.|Corporation|Corp|Company|Co|India|of)\b\.?", " ", n, flags=re.I)
    return " ".join(n.replace(".", " ").split()[:2]).rstrip(" &")


def about_company(title, sym, name):
    """Does a headline name this company? The ticker as an upper-case word, or the short company
    name (case-sensitive when the short name is an acronym like GAIL -- so 'Gail Kim' isn't GAIL)."""
    import re
    if re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(sym), title or ""):
        return True
    sn = short_name(name)
    if not sn or len(sn) < 3:
        return False
    flags = 0 if sn.isupper() else re.I
    return bool(re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(sn), title or "", flags))


def completed(bars, now=None):
    """Drop today's bar while the session is still running (it isn't a completed daily bar)."""
    now = now or now_ist()
    if bars and bars[-1]["date"] == now.strftime("%Y-%m-%d") and now.time() < datetime.time(15, 35):
        return bars[:-1]
    return bars


def fetch_daily(syms, provider=None, range_str="2y", workers=8):
    import data_in
    p = provider or data_in.get_provider()

    def one(s):
        try:
            d = p.get_historical_data(s, range_str=range_str, interval="1d")
            bars = [{"date": b["date"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or 0}
                    for b in (d.get("bars") or []) if None not in (b.get("o"), b.get("h"), b.get("l"), b.get("c"))]
            return s, completed(sorted(bars, key=lambda b: b["date"]))
        except Exception:
            return s, []
    with ThreadPoolExecutor(workers) as ex:
        return {s: b for s, b in ex.map(one, syms) if b}


# ---------------------------------------------------------------- odds

def load_odds(path=ODDS_PATH):
    try:
        with open(path) as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {(r["setup"], r["hold"], r["regime"]): r for r in rows}


def odds_for(odds, setup, regime_up):
    """(hold, row) for this setup: a passing hold if any (longest first), else MAX_HOLD; the
    'nifty_up' row when the market is in an uptrend, otherwise 'any'."""
    regime = "nifty_up" if regime_up else "any"
    for hold in (60, 40, 20):
        r = odds.get((setup, hold, regime))
        if r and r.get("passes"):
            return hold, r
    return MAX_HOLD, odds.get((setup, MAX_HOLD, regime)) or odds.get((setup, MAX_HOLD, "any"))


# ---------------------------------------------------------------- ideas

def plan(A, t, info):
    """Reference plan from day t's close (the real entry is the next open)."""
    e = A["c"][t]
    atr = A["atr14"][t]
    if not atr:
        return None
    floor = e - MAX_STOP_ATR * atr
    st = floor_tick(max(info.get("stop") if info.get("stop") is not None else floor, floor))
    if not 0 < st < e:
        return None
    return {"entry": e, "stop": st, "target": tick(e + TARGET_R * (e - st)), "atr": atr}


def size_it(sym, p, date, size_mod, cfg):
    try:
        r = size_mod.size_trade({"symbol": sym, "side": "buy", "entry": p["entry"], "stop": p["stop"],
                                 "target": p["target"], "asset_type": "EQUITY", "trading_style": "positional",
                                 "market": "IN", "exchange": "NSE", "currency": "INR", "flat_by": "positional"},
                                cfg, cash=capital.cap_inr(cfg, "investing"), today=date, market="IN", instruments_path=INSTRUMENTS,
                                trading_style="positional", exchange="NSE")
    except Exception as e:
        return None, "BUDGET: %s" % str(e).strip().splitlines()[-1][:120]
    q, nrr = r.get("qty") or 0, r.get("net_reward_risk")
    if r.get("refusal") or not q or nrr is None or nrr < 2.0:
        return None, "BUDGET: %s" % (r.get("refusal") or "net R:R %.2f < 2.00" % (nrr or 0))
    return r, None


def find_ideas(data, nifty, odds, size_mod=None, cfg=None):
    """[(idea dict)] for every setup on the last completed bar of each stock, sized and labelled."""
    if size_mod is None:
        import size as size_mod
        cfg = size_mod.load_config(None, market="IN")
    ranks = sig.rs_ranks(dict(data, NIFTY=nifty))
    reg = sig.regime_series(nifty) if nifty else {}
    ideas, refused = [], []
    for sym, bars in sorted(data.items()):
        if len(bars) < sig.WARMUP:
            continue
        A = sig.prep(bars)
        t = A["n"] - 1
        d = A["date"][t]
        rk = ranks.get(sym) or {}
        for setup, info in sig.signals_at(A, t, rk.get(d), rk.get(A["date"][t - 1])):
            p = plan(A, t, info)
            if p is None:
                continue
            s, why = size_it(sym, p, d, size_mod, cfg)
            if s is None:
                refused.append((sym, setup, why))
                continue
            hold, o = odds_for(odds, setup, reg.get(d))
            h = (o or {}).get("holdout") or {}
            ideas.append({
                "scan_date": d, "symbol": sym, "setup": setup, "close": A["c"][t],
                "label": "CALL" if (o or {}).get("passes") else "RESEARCH",
                "entry_ref": s.get("entry") or p["entry"], "stop": s.get("stop") or p["stop"],
                "target": s.get("target") or p["target"], "qty": s["qty"],
                "max_loss_inr": round(float(s.get("net_risk_including_costs") or s.get("max_loss") or 0), 0),
                "net_rr": round(s["net_reward_risk"], 2), "hold_days": hold,
                "p_target": h.get("target%"), "p_stop": h.get("stop%"), "p_green": h.get("win%"),
                "avgR": h.get("avgR"), "edge_vs_random": h.get("edge_vs_random"), "odds_n": h.get("n"),
                "regime_up": reg.get(d), "info": info})
    ideas.sort(key=lambda i: (i["label"] != "CALL", -(i["edge_vs_random"] if i["edge_vs_random"] is not None else -9),
                              -(i["avgR"] or -9)))
    return ideas, refused


# ---------------------------------------------------------------- context (display only)

def news_lines(sym, days=30, n=3, name=None):
    try:
        import news
        f = news.Fetcher(None)
        items = news.dedupe(news.tag(news.collect(f, {sym: {}}, {}, sec=False, exchange="NSE"), {sym: {}}))
        now = datetime.datetime.now(datetime.timezone.utc)
        sel = news.select(items, {}, now, hours=24 * days, show_all=True, sort_by="relevance")
        sel = [it for it in sel if about_company(it.get("title"), sym, name or sym)]
        return ["%s %s: %s" % (it["time"].astimezone(IST).strftime("%d %b") if it.get("time") else "?",
                               it.get("source") or "", (it.get("title") or "")[:110]) for it in sel[:n]]
    except Exception as e:
        return ["news unavailable (%s)" % type(e).__name__]


def results_line(sym):
    """Latest reported quarter; YoY from the most recent quarter that has a year-ago match."""
    try:
        import facts
        r = facts.analyse_indian(facts.Fetcher(None).get_indian(sym), quarters=6)
    except Exception as e:
        return "results unavailable (%s)" % type(e).__name__
    rows = r.get("rows") or []
    if not rows:
        return "no quarterly results found"
    cur = rows[0]
    yoy = next((x for x in rows if x.get("revenue_yoy") is not None or x.get("net_income_yoy") is not None), None)
    bits = ["latest quarter %s" % cur.get("end")]
    if cur.get("revenue") is not None:
        bits.append("revenue Rs %.0f Cr" % (cur["revenue"] / 1e7))
    if cur.get("net_income") is not None:
        bits.append("profit Rs %.0f Cr" % (cur["net_income"] / 1e7))
    if yoy:
        bits.append("YoY (q/e %s): revenue %s, profit %s" % (
            yoy.get("end"), _pct(yoy.get("revenue_yoy")), _pct(yoy.get("net_income_yoy"))))
        if (yoy.get("net_income_yoy") or 0) < 0:
            bits.append("PROFIT DOWN YoY")
    return " | ".join(bits)


def _pct(x):
    return "%+.0f%%" % x if x is not None else "n/a"


# ---------------------------------------------------------------- ledger

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


def open_risk(ledger):
    return sum(float(r.get("max_loss_inr") or 0) for r in ledger if r.get("status") in ("PENDING", "OPEN"))


def idea_capital(i):
    return float(i["qty"]) * float(i["entry_ref"])


def add_to_ledger(ledger, ideas, cap=OPEN_RISK_CAP, capital_free=None):
    """Take ideas in rank order while the open worst-case loss stays within `cap` and (when given) the
    idea's capital (qty x entry_ref) fits in `capital_free` -- the room left under the total-capital
    hard cap (scripts/capital.py); one per stock.
    Marks each idea's 'ledger' field: ADDED / ALREADY_OPEN / OPEN_RISK / CAPITAL_CAP."""
    held = {r["symbol"] for r in ledger if r.get("status") in ("PENDING", "OPEN")}
    risk = open_risk(ledger)
    for i in ideas:
        if i["symbol"] in held:
            i["ledger"] = "ALREADY_OPEN"
            continue
        if risk + i["max_loss_inr"] > cap:
            i["ledger"] = "OPEN_RISK"
            continue
        need = idea_capital(i) if capital_free is not None else 0.0
        if capital_free is not None and need > capital_free + 1e-6:
            i["ledger"] = "CAPITAL_CAP"
            continue
        ledger.append({"id": "%s-%s-%s" % (i["scan_date"], i["symbol"], i["setup"]), "symbol": i["symbol"],
                       "setup": i["setup"], "label": i["label"], "signal_date": i["scan_date"], "status": "PENDING",
                       "entry_ref": i["entry_ref"],
                       "stop": i["stop"], "target": i["target"], "qty": i["qty"], "max_loss_inr": i["max_loss_inr"],
                       "max_hold": i["hold_days"]})
        held.add(i["symbol"])
        risk += i["max_loss_inr"]
        if capital_free is not None:
            capital_free -= need
        i["ledger"] = "ADDED"
    return ledger


def walk(row, bars):
    """Advance one ledger row with daily bars (oldest first). PENDING fills at the first open after
    the signal day; then target / stop / max-hold like scripts/backtest_swing.simulate."""
    import size
    after = [b for b in bars if b["date"] > row["signal_date"]]
    if not after:
        return row
    st, tg, q = float(row["stop"]), float(row["target"]), int(float(row["qty"]))
    if row["status"] == "PENDING":
        row = dict(row, status="OPEN", entry_date=after[0]["date"], entry=after[0]["o"])
    if row["status"] != "OPEN":
        return row
    e = float(row["entry"])
    days = [b for b in after if b["date"] >= row["entry_date"]]
    hold = int(float(row.get("max_hold") or MAX_HOLD))
    out = None
    for k, b in enumerate(days[:hold]):
        if k > 0 and b["o"] <= st:
            out = ("STOP", b["o"], b["date"])
        elif b["l"] <= st:
            out = ("STOP", st, b["date"])
        elif k > 0 and b["o"] >= tg:
            out = ("TARGET", b["o"], b["date"])
        elif b["h"] >= tg:
            out = ("TARGET", tg, b["date"])
        if out:
            break
    if out is None and len(days) >= hold:
        out = ("TIME", days[hold - 1]["c"], days[hold - 1]["date"])
    if out is None:
        return row
    status, px, d = out
    chg = size.calculate_indian_transaction_charges("EQUITY", "buy", e, px, q, "positional", d)
    chg = chg.get("total") if isinstance(chg, dict) else float(chg or 0)
    net = (px - e) * q - chg
    return dict(row, status=status, exit_date=d, exit_price=round(px, 2), net_inr=round(net, 0),
                R=round(net / ((e - st) * q), 2) if e > st else "")


# ---------------------------------------------------------------- commands

def report_text(ideas, refused, scan_date, ledger):
    L = ["EQUITY IDEAS -- multi-day, long only, paper (scan of %s's close; generated %s IST)"
         % (scan_date, now_ist().strftime("%Y-%m-%d %H:%M")),
         "CALL = setup passed the 10-year backtest on unseen years. RESEARCH = it did not: your judgement, not a call.",
         "Odds = that setup's 2023+ backtest at the hold shown. News/results are context only (not in the odds).",
         "Each idea is sized on its own (Rs 2,000 max loss). The investing pool is hard-capped at Rs 1,00,000:",
         "you can hold only the few that fit together -- the paper ledger takes them in rank order until the cap.", ""]
    if not ideas:
        L.append("No setups on the last completed bar.")
    for n, i in enumerate(ideas[:CONTEXT_TOP], 1):
        L.append("%d. [%s] %s  %s  close %.2f%s" % (n, i["label"], i["symbol"], i["setup"], i["close"],
                                                     "" if i.get("regime_up") else "  (NIFTY below its 200-day average)"))
        L.append("   Plan: buy next open (ref %.2f) | stop %.2f | target %.2f (2.5R) | qty %s | capital Rs %.0f | "
                 "max loss Rs %.0f | net R:R %.2f | hold up to %d sessions | paper ledger: %s"
                 % (i["entry_ref"], i["stop"], i["target"], i["qty"], idea_capital(i), i["max_loss_inr"], i["net_rr"],
                    i["hold_days"], i.get("ledger", "-")))
        if i.get("odds_n"):
            L.append("   Backtest odds (2023+, n=%d): target %.0f%% | stop %.0f%% | ends green %.0f%% | avg %+.2fR | "
                     "vs random entries %+.2fR" % (i["odds_n"], i["p_target"], i["p_stop"], i["p_green"], i["avgR"],
                                                   i["edge_vs_random"] or 0))
        else:
            L.append("   Backtest odds: none for this setup at this hold")
        if i.get("results"):
            L.append("   Results: %s" % i["results"])
        for h in i.get("news") or []:
            L.append("   News: %s" % h)
        L.append("")
    if len(ideas) > CONTEXT_TOP:
        rest = ideas[CONTEXT_TOP:]
        L.append("%d more lower-ranked ideas (in trader/state/equity_calls.csv): %s"
                 % (len(rest), ", ".join("%s (%s)" % (i["symbol"], i["setup"]) for i in rest[:40])))
        L.append("")
    if refused:
        L.append("%d more setups refused by the budget gate (stop too far for Rs 2,000 or net R:R < 2.0 after charges)."
                 % len(refused))
    L += ["", ledger_text(ledger)]
    return "\n".join(L)


def ledger_text(ledger):
    if not ledger:
        return "Paper ledger: empty."
    op = [r for r in ledger if r.get("status") in ("PENDING", "OPEN")]
    done = [r for r in ledger if r.get("status") in ("TARGET", "STOP", "TIME")]
    net = sum(float(r.get("net_inr") or 0) for r in done)
    cap_used = sum(float(r.get("qty") or 0) * float(r.get("entry") or r.get("entry_ref") or 0) for r in op)
    L = ["PAPER LEDGER: %d open (capital Rs %.0f, worst-case Rs %.0f of Rs %.0f cap), %d closed, closed net Rs %+.0f"
         % (len(op), cap_used, open_risk(ledger), OPEN_RISK_CAP, len(done), net)]
    for r in op:
        L.append("  %-7s %-11s %-18s since %-10s entry %-8s stop %-8s target %-8s qty %s"
                 % (r["status"], r["symbol"], r["setup"], r.get("entry_date") or r["signal_date"], r.get("entry") or "next open",
                    r["stop"], r["target"], r["qty"]))
    for r in done[-10:]:
        L.append("  %-7s %-11s %-18s %s -> %s  Rs %+.0f (%sR)" % (r["status"], r["symbol"], r["setup"], r.get("entry_date"),
                                                              r.get("exit_date"), float(r.get("net_inr") or 0), r.get("R")))
    return "\n".join(L)


def notify(title, msg):
    import subprocess
    try:
        subprocess.Popen(["osascript", "-e", 'display notification "%s" with title "%s"'
                          % (msg.replace('"', "'")[:200], title.replace('"', "'"))],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def cmd_scan(a):
    syms = universe(a.universe)
    data = fetch_daily(syms + ["NIFTY"])
    nifty = data.pop("NIFTY", [])
    if not data:
        print("equity_calls: no daily data fetched (provider down or no token?)", file=sys.stderr)
        return 1
    ideas, refused = find_ideas(data, nifty, load_odds(a.odds))
    scan_date = max(b[-1]["date"] for b in data.values())
    ideas = [i for i in ideas if i["scan_date"] == scan_date]      # stale symbols don't count
    names = company_names(a.universe)
    for i in ideas[:CONTEXT_TOP]:
        i["news"] = news_lines(i["symbol"], name=names.get(i["symbol"])) or ["no company news in the last 30 days"]
        i["results"] = results_line(i["symbol"])
    ledger = read_csv(LEDGER_CSV)
    if not a.no_ledger:
        # Room in the INVESTING pool (its own Rs 1,00,000, separate from trading).
        import size as size_mod
        cap = capital.cap_inr(size_mod.load_config(None, market="IN"), "investing")
        free = capital.room("investing", cap) if capital.ENFORCE else None   # cap off while testing
        ledger = add_to_ledger(ledger, ideas, capital_free=free)
        write_csv(LEDGER_CSV, LEDGER_FIELDS, ledger)
    prev = [r for r in read_csv(CALLS_CSV) if r.get("scan_date") != scan_date]
    rows = prev + [dict(i, news=" || ".join(i.get("news") or []), results=i.get("results") or "") for i in ideas]
    write_csv(CALLS_CSV, CALL_FIELDS, rows)
    text = report_text(ideas, refused, scan_date, ledger)
    with open(REPORT_TXT, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    calls = sum(i["label"] == "CALL" for i in ideas)
    notify("Equity ideas %s" % scan_date, "%d ideas (%d CALL, %d RESEARCH). Report: trader/state/equity_report.txt"
           % (len(ideas), calls, len(ideas) - calls))
    return 0


def cmd_track(a):
    ledger = read_csv(LEDGER_CSV)
    live = sorted({r["symbol"] for r in ledger if r.get("status") in ("PENDING", "OPEN")})
    if live:
        data = fetch_daily(live, range_str="1y")
        ledger = [walk(r, data.get(r["symbol"]) or []) if r.get("status") in ("PENDING", "OPEN") else r
                  for r in ledger]
        write_csv(LEDGER_CSV, LEDGER_FIELDS, ledger)
    print(ledger_text(ledger))
    return 0


def cmd_show(a):
    if os.path.exists(REPORT_TXT):
        print(open(REPORT_TXT, encoding="utf-8").read())
    print(ledger_text(read_csv(LEDGER_CSV)))
    return 0


def cmd_run(a):
    import time
    import calendar_in
    print("%s EQUITY CALLS start: track + scan every trading day at 15:45 IST. Paper only; never places orders."
          % now_ist().strftime("%H:%M:%S"), flush=True)
    done = set()
    while True:
        n = now_ist()
        ok, _, _ = calendar_in.is_trading_day_in(n.date(), exchange="NSE")
        if ok and n.time() >= datetime.time(15, 45) and n.date() not in done:
            done.add(n.date())
            for fn in (cmd_track, cmd_scan):
                try:
                    fn(a)
                except Exception as e:
                    print("%s EQUITY CALLS ERROR %s: %s: %s" % (now_ist().strftime("%H:%M:%S"), fn.__name__,
                                                               type(e).__name__, str(e)[:160]), flush=True)
        time.sleep(60)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--universe", default=UNIVERSE)
    s.add_argument("--odds", default=ODDS_PATH)
    s.add_argument("--no-ledger", action="store_true", help="don't add ideas to the paper ledger")
    sub.add_parser("track")
    sub.add_parser("show")
    r = sub.add_parser("run")
    r.add_argument("--universe", default=UNIVERSE)
    r.add_argument("--odds", default=ODDS_PATH)
    r.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    if a.cmd in ("run", "scan"):
        upstox_speed.apply("equity_calls")
    return {"scan": cmd_scan, "track": cmd_track, "show": cmd_show, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
