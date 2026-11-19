#!/usr/bin/env python3
"""Calls scoreboard -- how each of today's watcher calls is doing (paper, read-only).

Reads trader/state/opportunities.csv (written by scripts/market_watch.py), fetches today's
5m bars from the configured India provider, and walks each call bar by bar from the candle
after its trigger with trader-timemachine's own simulate_exit() -- the same rule the
backtest uses: a bar touching the stop counts as the stop (checked before the target), and
a call still open at flat-by (15:20) is closed at that bar's close.

It places nothing and changes nothing except the scoreboard text file it writes.

    python3 scripts/scoreboard.py                  # print today's board, write trader/state/scoreboard.txt
    python3 scripts/scoreboard.py --date 2026-11-18
"""

import argparse
import csv
import datetime
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-timemachine"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
CSV_PATH = os.path.join(BASE_DIR, "trader", "state", "opportunities.csv")
EVENTS_PATH = os.path.join(BASE_DIR, "trader", "state", "watch_events.jsonl")
REJECTED_PATH = os.path.join(BASE_DIR, "trader", "state", "rejected.csv")
OUT_PATH = os.path.join(BASE_DIR, "trader", "state", "scoreboard.txt")
# Machine-readable day total for scripts/market_watch.py's daily-loss stop.
DAY_PATH = os.path.join(BASE_DIR, "trader", "state", "scoreboard_day.json")
OPEN_MIN = 9 * 60 + 15
FLAT_BY = "15:20"
# Paper fills: you can't buy at the trigger candle's close -- the alert lands minutes later. So a call
# is filled at the OPEN of the first 5m bar starting at/after the alert, plus slippage; a stop that is
# gapped through fills at that bar's open; every market exit pays slippage. The target is a resting
# limit, so it fills at the target. 1R stays the PLANNED risk (entry - stop), so R is comparable.
TICK = 0.05
SLIP_TICKS = 1


def slot(hhmm):
    h, m = [int(x) for x in str(hhmm).split(":")[:2]]
    return (h * 60 + m - OPEN_MIN) // 5


def load_calls(date, path=CSV_PATH):
    """Today's equity calls, one per symbol (the first alert; repeats are the same trade)."""
    calls, seen = [], set()
    if not os.path.exists(path):
        return calls
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("date") != date or r.get("kind") != "equity" or r.get("symbol") in seen:
                continue
            seen.add(r["symbol"])
            calls.append(r)
    return calls


def load_charges(date, path=EVENTS_PATH):
    """{symbol: est. charges} from the watcher's event log -- the number trader-budget's
    size_trade() computed for that call (first alert per symbol)."""
    import json
    import re
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("kind") != "equity" or not str(ev.get("detected_at", "")).startswith(date):
                continue
            sym = (ev.get("hit") or {}).get("symbol")
            m = re.search(r"est\. charges ([0-9.]+)", ev.get("summary") or "")
            if sym and m and sym not in out:
                out[sym] = float(m.group(1))
    return out


def fill_slot(call):
    """First 5m bar that starts at/after the alert (never before the bar after the trigger)."""
    start = slot(call["trigger_time"])            # trigger bar closes at trigger_time -> next bar starts here
    at = call.get("alert_time") or call.get("time")
    if at:
        h, m = [int(x) for x in str(at).split(":")[:2]]
        mins = h * 60 + m - OPEN_MIN
        if len(str(at).split(":")) > 2 and int(str(at).split(":")[2]) > 0:
            mins += 1                             # 09:37:20 -> the 09:40 bar, never the one already running
        start = max(start, -(-mins // 5))
    return start


def score(call, bars, now_slot, side="buy", slip_ticks=None):
    """(status, exit_or_last_price, R, gross_pnl) for one call from today's {slot: bar}, with
    realistic paper fills (see TICK / SLIP_TICKS). Exit price is net of slippage.
    side "sell" scores a short (stop above entry, target below) -- analysis rows only."""
    import timemachine
    e, s, t = float(call["entry"]), float(call["stop"]), float(call["target"])
    qty = int(float(call.get("qty") or 0))
    slip = (SLIP_TICKS if slip_ticks is None else slip_ticks) * TICK
    long = side == "buy"
    sign = 1.0 if long else -1.0
    start = fill_slot(call)
    flat = slot(FLAT_BY) - 1                       # last bar that closes by 15:20
    last = min(now_slot, flat)
    while last >= start and last not in bars:
        last -= 1
    first = start
    while first <= last and first not in bars:
        first += 1
    if last < start or first > last:
        return "WAITING", None, None, None
    fill = float(bars[first].get("o", e)) + sign * slip
    oc, px, i = timemachine.simulate_exit(bars, first, last, fill, s, t, side)
    status = {"stop": "STOP HIT", "target": "TARGET HIT"}.get(oc)
    if status == "STOP HIT":
        o = bars[i].get("o")
        if o is not None and ((o < s) if long else (o > s)):
            px = float(o)                          # gapped through the stop: out at the open
        px -= sign * slip
    elif status is None:
        status = "CLOSED 15:20" if last == flat else "OPEN"
        px -= sign * slip
    return status, px, sign * (px - fill) / abs(e - s), sign * qty * (px - fill)


def load_rejected(date, path=REJECTED_PATH):
    """Today's rejected/short rows, one per (kind, symbol, direction, trigger)."""
    rows, seen = [], set()
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            k = (r.get("kind"), r.get("symbol"), r.get("direction"), r.get("trigger_time"))
            if r.get("date") != date or k in seen:
                continue
            seen.add(k)
            rows.append(r)
    return rows


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def underlying_of(contract):
    """NIFTY2026112325250CE -> NIFTY (leading letters)."""
    out = ""
    for ch in str(contract):
        if not ch.isalpha():
            break
        out += ch
    return out or str(contract)


def score_rejected(r, bars_for, now_slot):
    """(verdict, price_now, R_or_points, detail) for one rejected row.
    Equity rows with a full plan are walked bar by bar like a real call; options rows (and
    equity rows with no plan) are judged by direction only: did the underlying move the way
    the signal pointed since it was logged."""
    kind, direction = r.get("kind"), (r.get("direction") or "").upper()
    if kind == "options":
        und = underlying_of(r.get("symbol"))
        bars = bars_for(und)
        ref = _num(r.get("underlying_price"))
        if not bars or ref is None:
            return "NO DATA", None, None, und
        now_px = bars[max(bars)]["c"]
        pts = now_px - ref
        bullish = direction.startswith("BULL")
        right = (pts > 0) if bullish else (pts < 0)
        return ("RIGHT" if right else "WRONG") if pts else "FLAT", now_px, pts, "%s %+.1f pts" % (und, pts)
    bars = bars_for(r.get("symbol"))
    if not bars:
        return "NO DATA", None, None, ""
    e, st, tg = _num(r.get("entry")), _num(r.get("stop")), _num(r.get("target"))
    side = "sell" if direction == "SHORT" else "buy"
    if None not in (e, st, tg) and r.get("trigger_time"):
        status, px, R, _ = score(dict(r, qty=0), bars, now_slot, side=side)
        if R is None:
            return "WAITING", None, None, status
        verdict = "RIGHT" if (status == "TARGET HIT" or (status != "STOP HIT" and R > 0)) else "WRONG"
        return verdict, px, R, status
    if e is None:
        return "NO DATA", None, None, ""
    now_px = bars[max(bars)]["c"]
    mv = (now_px - e) if side == "buy" else (e - now_px)
    return ("RIGHT" if mv > 0 else "WRONG"), now_px, None, "direction only %+.2f" % mv


def build(date=None, provider=None, totals=None, on_totals=None):
    """Return the board text. If `totals` is a dict it is filled with the day's equity-call total
    (see day_totals) for the watcher's daily-loss stop. `on_totals(totals)` is called as soon as the
    alerted calls are scored -- before the slow rejected section -- so the daily-loss total is saved
    even if the rejected scoring is cut short by a timeout."""
    today = datetime.datetime.now(IST)
    date = date or today.strftime("%Y-%m-%d")
    calls = load_calls(date)
    import data_in
    p = provider or data_in.get_provider()
    cache = {}

    def bars_for(sym):
        if sym not in cache:
            try:
                d = p.get_intraday_data(sym, interval="5m", range_str="1d")
                cache[sym] = {slot(b["start"]): b for b in (d.get("bars") or [])
                              if b.get("date") == date and b.get("c") is not None}
            except Exception:
                cache[sym] = {}
        return cache[sym]
    # bars whose 5 minutes have finished, plus the forming one (gives the live price)
    now_slot = slot(today.strftime("%H:%M")) if date == today.strftime("%Y-%m-%d") else 999
    charges = load_charges(date)
    lines = ["Calls scoreboard %s (as of %s IST) -- paper only" % (date, today.strftime("%H:%M")), "",
             "%-11s %-6s %-18s %9s %9s %9s %5s %6s | %-12s %9s %7s %9s %9s"
             % ("SYMBOL", "ALERT", "SETUP", "ENTRY", "STOP", "TARGET", "QTY", "P(TGT)", "STATUS", "PRICE", "R",
                "GROSS Rs", "NET Rs")]
    tot_r, tot_pnl, tot_net, n = 0.0, 0.0, 0.0, 0
    unscored, worst_case, odds_seen = [], 0.0, []
    for c in calls:
        try:
            status, px, r, pnl = score(c, bars_for(c["symbol"]), now_slot)
        except Exception as ex:   # one bad symbol must not hide the rest
            status, px, r, pnl = "DATA ERROR", None, None, None
            c = dict(c, setup="%s (%s)" % (c.get("setup"), type(ex).__name__))
        chg = charges.get(c["symbol"])
        net = (pnl - chg) if (pnl is not None and chg is not None) else None
        lines.append("%-11s %-6s %-18s %9.2f %9.2f %9.2f %5s %6s | %-12s %9s %7s %9s %9s"
                     % (c["symbol"], c["alert_time"][:5], c["setup"][:18], float(c["entry"]), float(c["stop"]),
                        float(c["target"]), c.get("qty", ""), pct(c.get("p_target")), status,
                        "%.2f" % px if px is not None else "-", "%+.2f" % r if r is not None else "-",
                        "%+.0f" % pnl if pnl is not None else "-", "%+.0f" % net if net is not None else "-"))
        odds_seen.append((c.get("p_target"), c.get("p_stop"), status))
        if r is not None:
            tot_r, tot_pnl, n = tot_r + r, tot_pnl + pnl, n + 1
            tot_net += net if net is not None else pnl
            worst_case += net if net is not None else pnl
        else:
            # Can't price it: fail closed for the daily-loss total -- count it as a full stop-out
            # (trader-budget's own risk figure incl. charges for that call).
            unscored.append(c["symbol"])
            risk = _num(c.get("risk_inr_incl_costs"))
            if risk is None:
                e, st, q = _num(c.get("entry")), _num(c.get("stop")), _num(c.get("qty"))
                risk = abs(e - st) * q if None not in (e, st, q) else 0.0
            worst_case -= abs(risk)
    if totals is not None:
        totals.update(day_totals(date, today, len(calls), n, tot_net, worst_case, unscored))
        if on_totals is not None:
            on_totals(totals)
    lines += ["", "%d calls scored: total %+.2fR | gross %+.0f Rs | net %+.0f Rs after est. charges"
              % (n, tot_r, tot_pnl, tot_net),
              "R = move / (entry - stop); 1R = the loss if the stop is hit. Stop is checked before target in",
              "each bar (conservative). Net uses each call's charge estimate from trader-budget (watch_events.jsonl)."]
    lines += odds_check("Alerted calls", odds_seen)
    if not calls:
        lines = lines[:2] + ["No alerted calls yet today."]
    lines += rejected_section(date, bars_for, now_slot)
    return "\n".join(lines)


def pct(x):
    v = _num(x)
    return ("%.0f%%" % (100 * v)) if v is not None else "-"


def odds_check(label, items):
    """Did the odds hold up today? Expected target/stop hits (sum of the probabilities) vs actual,
    over calls that carried odds (scripts/probability.py). Calls still open count as neither yet."""
    rows = [(_num(pt), _num(ps), st) for pt, ps, st in items if _num(pt) is not None and _num(ps) is not None]
    if not rows:
        return []
    exp_t, exp_s = sum(r[0] for r in rows), sum(r[1] for r in rows)
    act_t = sum(1 for r in rows if "TARGET" in r[2])
    act_s = sum(1 for r in rows if "STOP" in r[2])
    return ["%s with odds: %d | targets expected %.1f, hit %d | stops expected %.1f, hit %d%s"
            % (label, len(rows), exp_t, act_t, exp_s, act_s,
               " (open calls can still change this)" if any(r[2] in ("OPEN", "WAITING") for r in rows) else "")]


def day_totals(date, now, calls, scored, net, worst_case, unscored):
    return {"date": date, "as_of": now.strftime("%Y-%m-%d %H:%M"), "calls": calls, "scored": scored,
            "net_inr": round(net, 2), "net_inr_worst_case": round(worst_case, 2), "unscored": unscored,
            "note": "equity calls marked to the latest 5m close, after est. charges; an unpriced call "
                    "counts as a full stop-out in net_inr_worst_case"}


def rejected_section(date, bars_for, now_slot):
    rows = load_rejected(date)
    out = ["", "=" * 100,
           "NOT ALERTED -- found but rejected, or bearish (long-only system). Scored for analysis only.",
           "  equity / equity_short: walked bar by bar like a real call (R).  options: did the INDEX move",
           "  the way the signal pointed since it was logged (points) -- option premium P&L is not modelled here.",
           ""]
    if not rows:
        return out + ["None logged yet today (trader/state/rejected.csv)."]
    scored = []
    for r in rows:
        try:
            v = score_rejected(r, bars_for, now_slot)
        except Exception as ex:
            v = ("ERROR", None, None, type(ex).__name__)
        scored.append((r, v))
    groups = {}
    for r, v in scored:
        g = groups.setdefault((r.get("kind"), r.get("direction")), [0, 0, 0])
        g[0] += 1
        g[1] += v[0] == "RIGHT"
        g[2] += v[0] == "WRONG"
    out.append("SUMMARY")
    for (k, d), (n, right, wrong) in sorted(groups.items()):
        out.append("  %-13s %-8s %3d logged | right %3d | wrong %3d" % (k, d, n, right, wrong))
    out += ["  " + x for x in odds_check("Rejected equity longs",
                                         [(r.get("p_target"), r.get("p_stop"), v[3] or "")
                                          for r, v in scored if r.get("kind") == "equity"])]
    out += ["", "%-6s %-12s %-8s %-24s %-26s %6s %9s %9s %9s | %-6s %9s %8s  %s"
            % ("TIME", "KIND", "DIR", "SYMBOL", "SETUP", "TRIG", "ENTRY", "STOP", "TARGET",
               "RESULT", "NOW", "R/PTS", "WHY REJECTED / DETAIL")]
    for r, (verdict, px, val, detail) in scored:
        f = lambda x: ("%.2f" % _num(x)) if _num(x) is not None else "-"
        out.append("%-6s %-12s %-8s %-24s %-26s %6s %9s %9s %9s | %-6s %9s %8s  %s"
                   % (r.get("time", "")[:5], r.get("kind", ""), r.get("direction", "")[:8], r.get("symbol", "")[:24],
                      r.get("setup", "")[:26], (r.get("trigger_time") or "")[:6], f(r.get("entry")), f(r.get("stop")),
                      f(r.get("target")), verdict, f(px), ("%+.2f" % val) if val is not None else "-",
                      ("%s | %s" % (detail, r.get("reasons", "")))[:160]))
    return out


def write(text, path=OUT_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today IST)")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    totals = {}
    save = write_day if (a.date is None and not a.no_write) else None
    text = build(a.date, totals=totals, on_totals=save)
    print(text)
    if not a.no_write:
        write(text)
    return 0


def write_day(totals, path=None):
    import json
    path = path or DAY_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(totals, fh, indent=1)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
