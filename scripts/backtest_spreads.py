#!/usr/bin/env python3
"""Credit-spread backtest (Bull Put / Bear Call, held to expiry) on NIFTY and BANKNIFTY -- research
only; decision support, never places orders. Naked selling is not modelled (it stays prohibited).

    python3 scripts/backtest_spreads.py fetch     # 10y daily NIFTY, BANKNIFTY, INDIA VIX -> bt_cache/index_10y.json
    python3 scripts/backtest_spreads.py report    # every variant: design vs holdout, per year, worst loss

MODEL, NOT HISTORICAL QUOTES (read before trusting any number):
  * Premiums come from Black-Scholes (trader-alerts/data_in.black_scholes) with IV = INDIA VIX for
    NIFTY; BANKNIFTY IV = VIX x (BANKNIFTY / NIFTY 60-day realised-vol ratio, known that day).
    Calls are priced at 0.92 x that IV (index call skew is below ATM). Puts use it flat, which
    UNDER-prices OTM puts -- conservative for put sellers.
  * VIX is a real implied vol and settlement uses the real index, so the gap between implied and
    realised volatility (what option sellers are paid for) is genuinely in the data.
  * Each leg pays slippage: max(0.5 point, 3 % of its premium) when opened and when closed early;
    expiry settles at intrinsic (no slippage). Full option charges from trader-budget per leg.
  * Daily bars only: a stop is checked against each day's adverse extreme (low for a put spread,
    high for a call spread) at that day's VIX; if the day OPENS beyond the stop it exits at the
    open's value. Intraday spikes that reverse by the close are therefore counted, not missed.
  * Expiry dates: trader-alerts/calendar_in (today's Tuesday convention, holidays rolled back) --
    a historical-weekday approximation. NIFTY weekly options exist only from 2019-02-11, so weekly
    tests start there; BANKNIFTY is tested monthly-only (its weeklies ended in 2024).
  * Lots: today's lot sizes for every year, so rupee P&L is per current lot.
"""

import argparse
import datetime
import json
import math
import os
import statistics
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-budget", "scripts"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

CACHE_DIR = os.path.join("trader", "state", "bt_cache")
INDEX_CACHE = os.path.join(CACHE_DIR, "index_10y.json")
HOLDOUT_FROM = "2023-01-01"
NIFTY_WEEKLY_FROM = "2019-02-11"
INDEX = {"NIFTY": {"step": 50, "lot": 65}, "BANKNIFTY": {"step": 100, "lot": 30}}
CALL_SKEW = 0.92
RATE = 0.065
SLIP_PTS, SLIP_PCT = 0.5, 0.03
BUDGETS = (2000.0, 3000.0)


# ---------------------------------------------------------------- pricing

def bs(spot, strike, t_years, vol, kind):
    import data_in
    return data_in.black_scholes(spot, strike, t_years, rate=RATE, vol=vol, option_type=kind)


def leg_price(spot, strike, t_years, vol, kind):
    if t_years <= 0:
        return max(0.0, (spot - strike) if kind == "CE" else (strike - spot))
    return bs(spot, strike, t_years, vol, kind)["price"]


def spread_value(spot, short_k, long_k, t_years, vol, kind):
    """Cost to buy the spread back (short leg minus long leg), floored at 0, capped at the width."""
    v = leg_price(spot, short_k, t_years, vol, kind) - leg_price(spot, long_k, t_years, vol, kind)
    return min(max(v, 0.0), abs(short_k - long_k))


def pick_strikes(spot, t_years, vol, delta_target, step, width_steps, kind):
    """Short strike: the strike (on the exchange step) whose |delta| is closest to delta_target,
    out of the money; hedge `width_steps` strikes further out."""
    atm = round(spot / step) * step
    best = None
    for i in range(1, 80):
        k = atm - i * step if kind == "PE" else atm + i * step
        if k <= 0:
            break
        d = abs(bs(spot, k, t_years, vol, kind)["delta"])
        if best is None or abs(d - delta_target) < abs(best[1] - delta_target):
            best = (k, d)
        if d < delta_target / 3:
            break
    if best is None:
        return None
    short_k = best[0]
    long_k = short_k - width_steps * step if kind == "PE" else short_k + width_steps * step
    return short_k, long_k, best[1]


def slip(premium):
    return max(SLIP_PTS, SLIP_PCT * premium)


def leg_charges(sell_px, buy_px, qty, date):
    """Round-trip option charges for one leg (trader-budget's schedule; STT on the sell side)."""
    import size
    c = size.calculate_indian_transaction_charges("OPTIONS", "buy", buy_px, sell_px, qty, "intraday", date)
    return c.get("total") if isinstance(c, dict) else float(c or 0)


# ---------------------------------------------------------------- data

def cmd_fetch(a):
    import data_in
    p = data_in.get_provider()
    out = {}
    for s in ("NIFTY", "BANKNIFTY", "INDIA VIX"):
        d = p.get_historical_data(s, range_str="10y", interval="1d")
        out[s] = [{"date": b["date"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
                  for b in (d.get("bars") or []) if None not in (b.get("o"), b.get("h"), b.get("l"), b.get("c"))]
        print(s, len(out[s]), out[s][0]["date"], out[s][-1]["date"])
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh)
    return 0


def realised_vol(closes, i, n=60):
    if i < n:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(i - n + 1, i + 1)]
    return statistics.pstdev(rets) * math.sqrt(252)


def build_series(data, sym):
    """Aligned daily rows {date, o, h, l, c, vix, iv, sma20} for one index (point-in-time IV)."""
    vix = {b["date"]: b["c"] for b in data["INDIA VIX"]}
    bars = [b for b in data[sym] if b["date"] in vix]
    nifty = {b["date"]: b["c"] for b in data["NIFTY"]}
    ncl = [b["c"] for b in data["NIFTY"]]
    nidx = {b["date"]: i for i, b in enumerate(data["NIFTY"])}
    cl = [b["c"] for b in bars]
    rows = []
    for i, b in enumerate(bars):
        iv = vix[b["date"]] / 100.0
        if sym != "NIFTY":
            rv_s = realised_vol(cl, i)
            j = nidx.get(b["date"])
            rv_n = realised_vol(ncl, j) if j is not None else None
            if not rv_s or not rv_n:
                continue
            iv *= rv_s / rv_n
        sma20 = statistics.mean(cl[i - 19:i + 1]) if i >= 19 else None
        rows.append(dict(b, vix=vix[b["date"]], iv=iv, sma20=sma20))
    return rows


def expiries_between(sym, start, end, weekly):
    import calendar_in
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        if weekly:
            out += calendar_in.get_weekly_expiries(y, m, underlying=sym)
        else:
            out.append(calendar_in.get_monthly_expiry(y, m, underlying=sym))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return [e for e in out if start <= e <= end]


# ---------------------------------------------------------------- simulation

TIME_BASIS = "calendar"     # or "trading": the weekend-decay check (see report notes)


def years(d0, d1):
    """Time from day d0's close to expiry d1's close. "calendar": days/365 (Black-Scholes as the
    VIX is quoted). "trading": weekdays/252 -- the market prices most weekend decay before Friday's
    close, so a Friday entry valued on calendar time overstates the premium a seller collects."""
    if TIME_BASIS == "trading":
        n, d = 0, d0
        while d < d1:
            d += datetime.timedelta(days=1)
            if d.weekday() < 5:
                n += 1
        return n / 252.0
    return max((d1 - d0).days, 0) / 365.0


def simulate(rows, i0, exp_date, kind, delta_target, width_steps, stop_mult, sym):
    """One credit spread opened at row i0's close, held to `exp_date` (settled at that day's close)
    or stopped when buying it back would cost stop_mult x the credit. Returns a trade dict or None."""
    meta = INDEX[sym]
    r0 = rows[i0]
    d0 = datetime.date.fromisoformat(r0["date"])
    t0 = years(d0, exp_date)
    vol0 = r0["iv"] * (CALL_SKEW if kind == "CE" else 1.0)
    pk = pick_strikes(r0["c"], t0, vol0, delta_target, meta["step"], width_steps, kind)
    if pk is None:
        return None
    short_k, long_k, delta = pk
    ps = leg_price(r0["c"], short_k, t0, vol0, kind)
    pl = leg_price(r0["c"], long_k, t0, vol0, kind)
    credit = (ps - slip(ps)) - (pl + slip(pl))          # sell at bid, buy at ask
    width = abs(short_k - long_k)
    if credit <= 0.5:                                     # nothing worth selling
        return None
    lot = meta["lot"]
    max_loss = (width - credit) * lot
    exit_cost, outcome, exit_date, legs_out = None, "expiry", None, None
    for r in rows[i0 + 1:]:
        d = datetime.date.fromisoformat(r["date"])
        if d > exp_date:
            break
        t = years(d, exp_date)
        vol = r["iv"] * (CALL_SKEW if kind == "CE" else 1.0)
        if d == exp_date:
            exit_cost = spread_value(r["c"], short_k, long_k, 0.0, vol, kind)
            legs_out = (leg_price(r["c"], short_k, 0.0, vol, kind), leg_price(r["c"], long_k, 0.0, vol, kind))
            exit_date = r["date"]
            break
        if stop_mult:
            stop_val = stop_mult * credit
            v_open = spread_value(r["o"], short_k, long_k, t, vol, kind)
            adverse = r["l"] if kind == "PE" else r["h"]
            v_adv = spread_value(adverse, short_k, long_k, t, vol, kind)
            if v_open >= stop_val or v_adv >= stop_val:
                px = r["o"] if v_open >= stop_val else adverse
                v = v_open if v_open >= stop_val else stop_val
                ls, ll = leg_price(px, short_k, t, vol, kind), leg_price(px, long_k, t, vol, kind)
                exit_cost = min(width, v + slip(ls) + slip(ll))    # buy short at ask, sell long at bid
                legs_out = (ls + slip(ls), max(ll - slip(ll), 0.0))
                outcome, exit_date = "stop", r["date"]
                break
    if exit_cost is None:
        return None                                       # expiry not in the data yet
    gross = (credit - exit_cost) * lot
    # per leg: short sold at bid then bought back; long bought at ask then sold (or settled).
    # A leg that expires worthless is still charged a round trip at Rs 0.05 (conservative).
    chg = (leg_charges(ps - slip(ps), max(legs_out[0], 0.05), lot, r0["date"])
           + leg_charges(max(legs_out[1], 0.05), pl + slip(pl), lot, r0["date"]))
    net = gross - chg
    return {"sym": sym, "kind": kind, "date": r0["date"], "expiry": exp_date.isoformat(), "exit": exit_date,
            "outcome": outcome, "spot": r0["c"], "short_k": short_k, "long_k": long_k, "delta": round(delta, 3),
            "credit": round(credit, 2), "exit_cost": round(exit_cost, 2), "max_loss": round(max_loss, 0),
            "net": round(net, 0), "charges": round(chg, 0), "vix": r0["vix"]}


def run_variant(rows, sym, weekly, days_before, kind_rule, delta_target, width_steps, stop_mult):
    by_date = {r["date"]: i for i, r in enumerate(rows)}
    dates = [datetime.date.fromisoformat(r["date"]) for r in rows]
    start = max(dates[0], datetime.date.fromisoformat(NIFTY_WEEKLY_FROM)) if (weekly and sym == "NIFTY") else dates[0]
    trades = []
    for exp in expiries_between(sym, start, dates[-1], weekly):
        # entry = the trading day `days_before` sessions before expiry (by index in the data)
        k = None
        for j in range(len(dates) - 1, -1, -1):
            if dates[j] <= exp:
                k = j
                break
        if k is None or dates[k] != exp:
            continue                                      # expiry day missing from data
        i0 = k - days_before
        if i0 < 20 or rows[i0]["sma20"] is None:
            continue
        if kind_rule == "trend":
            kinds = ["PE" if rows[i0]["c"] > rows[i0]["sma20"] else "CE"]
        elif kind_rule == "both":
            kinds = ["PE", "CE"]
        else:
            kinds = [kind_rule]
        for kind in kinds:
            t = simulate(rows, i0, exp, kind, delta_target, width_steps, stop_mult, sym)
            if t:
                trades.append(t)
    return trades


def summarize(ts):
    if not ts:
        return {"n": 0}
    net = [t["net"] for t in ts]
    wins, losses = [x for x in net if x > 0], [x for x in net if x <= 0]
    by_year, by_month = {}, {}
    for t in ts:
        by_year.setdefault(t["date"][:4], []).append(t["net"])
        by_month[t["expiry"][:7]] = by_month.get(t["expiry"][:7], 0.0) + t["net"]
    yr = {y: sum(v) for y, v in by_year.items() if len(v) >= 5}
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(ts, key=lambda x: x["expiry"]):
        eq += t["net"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": len(ts), "win%": 100.0 * len(wins) / len(ts), "avg": statistics.mean(net),
            "avg_win": statistics.mean(wins) if wins else 0.0, "avg_loss": statistics.mean(losses) if losses else 0.0,
            "worst": min(net), "worst_month": min(by_month.values()), "max_dd": dd, "total": sum(net),
            "years_pos%": (100.0 * sum(v > 0 for v in yr.values()) / len(yr)) if yr else None,
            "stop%": 100.0 * sum(t["outcome"] == "stop" for t in ts) / len(ts),
            "median_max_loss": statistics.median(t["max_loss"] for t in ts)}


def passes(design, holdout, all_):
    if not design.get("n") or not holdout.get("n") or holdout["n"] < 40:
        return False
    if design["avg"] <= 0 or holdout["avg"] <= 0:
        return False
    return (all_.get("years_pos%") or 0) >= 60.0


VARIANTS = []
for _sym, _weekly, _days in (("NIFTY", True, 1), ("NIFTY", True, 2), ("NIFTY", True, 4),
                             ("NIFTY", False, 5), ("NIFTY", False, 10), ("NIFTY", False, 15),
                             ("BANKNIFTY", False, 5), ("BANKNIFTY", False, 10), ("BANKNIFTY", False, 15)):
    for _rule in ("PE", "CE", "both", "trend"):
        for _delta in (0.15, 0.20, 0.30):
            for _w in (1, 2):
                for _stop in (None, 2.0):
                    VARIANTS.append((_sym, _weekly, _days, _rule, _delta, _w, _stop))


def _job(v):
    sym, weekly, days, rule, delta, w, stop = v
    rows = _W["rows"][sym]
    ts = run_variant(rows, sym, weekly, days, rule, delta, w, stop)
    out = {"variant": {"sym": sym, "expiry": "weekly" if weekly else "monthly", "days_before": days, "side": rule,
                       "delta": delta, "width_steps": w, "stop_x_credit": stop}}
    d = [t for t in ts if t["date"] < HOLDOUT_FROM]
    h = [t for t in ts if t["date"] >= HOLDOUT_FROM]
    out["design"], out["holdout"], out["all"] = summarize(d), summarize(h), summarize(ts)
    for b in BUDGETS:
        out["holdout_within_%d" % b] = summarize([t for t in h if t["max_loss"] <= b])
        out["design_within_%d" % b] = summarize([t for t in d if t["max_loss"] <= b])
    out["passes"] = passes(out["design"], out["holdout"], out["all"])
    out["passes_within"] = {str(int(b)): passes(out["design_within_%d" % b], out["holdout_within_%d" % b],
                                                summarize([t for t in ts if t["max_loss"] <= b]))
                            for b in BUDGETS}
    return out, ts


_W = {}


def _init(path, basis="calendar"):
    global TIME_BASIS
    TIME_BASIS = basis
    with open(path) as fh:
        data = json.load(fh)
    _W["rows"] = {s: build_series(data, s) for s in INDEX}


def cmd_report(a):
    import multiprocessing
    with multiprocessing.Pool(a.workers, _init, (a.cache, a.time_basis)) as pool:
        res = pool.map(_job, VARIANTS, chunksize=4)
    report = [r for r, _ in res]
    with open(a.json, "w") as fh:
        json.dump(report, fh, indent=1)
    best = sorted(res, key=lambda x: -(x[0]["holdout"].get("avg") or -1e9))[0]
    with open(a.trades, "w") as fh:
        for _, ts in res:
            for t in ts:
                fh.write(json.dumps(t) + "\n")
    print(report_text(report))
    return 0


def _f(p):
    if not p.get("n"):
        return "%-62s" % "n=0"
    return "%4d %5.1f%% %+7.0f %+7.0f %+8.0f %+8.0f %4s" % (
        p["n"], p["win%"], p["avg"], p["avg_win"], p["avg_loss"], p["worst"],
        "%.0f" % p["years_pos%"] if p.get("years_pos%") is not None else "-")


def report_text(report, top=40):
    L = ["Credit spreads held to expiry -- MODEL prices (Black-Scholes on INDIA VIX), real index settlement, "
         "slippage + full charges, per CURRENT lot (NIFTY 65, BANKNIFTY 30). Design < %s <= holdout." % HOLDOUT_FROM,
         "PASS = avg net > 0 on design AND holdout, holdout n >= 40, >= 60%% of years positive. Rs per lot.", "",
         "%-44s | %-50s | %-50s | %s" % ("variant", "DESIGN   n   win%%    avg     win    loss   worst yr+%",
                                         "HOLDOUT  n   win%%    avg     win    loss   worst yr+%",
                                         "PASS  (<=2k / <=3k)  medMaxLoss")]
    rows = sorted(report, key=lambda r: -(r["holdout"].get("avg") or -1e9))
    for r in rows[:top]:
        v = r["variant"]
        name = "%-9s %-7s d-%-2d %-5s Δ%.2f w%d %s" % (v["sym"], v["expiry"], v["days_before"], v["side"], v["delta"],
                                                       v["width_steps"], ("stop%gx" % v["stop_x_credit"]) if v["stop_x_credit"] else "hold")
        L.append("%-44s | %s | %s | %-4s (%s / %s)  %s" % (
            name, _f(r["design"]), _f(r["holdout"]), "PASS" if r["passes"] else "-",
            "Y" if r["passes_within"]["2000"] else "-", "Y" if r["passes_within"]["3000"] else "-",
            "%.0f" % r["all"].get("median_max_loss", 0) if r["all"].get("n") else "-"))
    n_pass = sum(r["passes"] for r in report)
    L += ["", "%d of %d variants pass; within the Rs 2,000 per-trade limit: %d; within Rs 3,000: %d."
          % (n_pass, len(report), sum(r["passes_within"]["2000"] for r in report),
             sum(r["passes_within"]["3000"] for r in report))]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", default=INDEX_CACHE)
    r = sub.add_parser("report")
    r.add_argument("--cache", default=INDEX_CACHE)
    r.add_argument("--json", default=os.path.join(CACHE_DIR, "spreads_report.json"))
    r.add_argument("--trades", default=os.path.join(CACHE_DIR, "spreads_trades.jsonl"))
    r.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    r.add_argument("--time-basis", choices=("calendar", "trading"), default="calendar",
                   help="option time to expiry: calendar days/365 or weekdays/252 (weekend-decay check)")
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    return {"fetch": cmd_fetch, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
