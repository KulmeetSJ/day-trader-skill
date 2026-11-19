#!/usr/bin/env python3
"""Swing (multi-day) backtest harness -- research only, paper/long-only, never places orders.

Tests the daily-chart signals in scripts/swing_signals.py (the scanner's existing daily
setups, classic chart formations, relative strength) over ~10 years of NIFTY200 daily bars.

    python3 scripts/backtest_swing.py fetch       # 10y daily OHLCV, NIFTY200 + NIFTY -> bt_cache/daily_10y.json
    python3 scripts/backtest_swing.py signals     # every signal on every day -> bt_cache/swing_signals.jsonl
    python3 scripts/backtest_swing.py report      # setups x hold x regime: design vs holdout vs random entries

Honest-fill rules:
  * A signal is known only after day t's close; entry is day t+1's OPEN.
  * Stop = the pattern's invalidation level, but never wider than 3 x ATR(14) (trader-exit's atr_of).
  * Target = 2.2R from the actual entry. Exit at the close of the Nth trading day if neither is hit.
  * A day that opens below the stop exits at that open (gap); a day touching both = stop.
  * Every trade is sized by the real trader-budget size_trade(trading_style="positional") -- Rs 2,000
    max loss, position caps, the hard 2.0 net R:R floor -- and pays positional Indian charges
    (calculate_indian_transaction_charges: 0.1% STT both sides, stamp, exchange, GST, Rs 80 brokerage).

A setup counts only if it beats RANDOM entries (same stocks, same years, same stop width,
target and hold) on the DESIGN years and again on the unseen HOLDOUT years. Today's NIFTY200
list is used for every year (survivorship bias); the random baseline carries the same bias,
which is why the edge over random -- not the raw average -- is the number that matters.
"""

import argparse
import datetime
import json
import multiprocessing
import os
import random
import statistics
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-scanner", "trader-budget", "trader-exit", "scripts"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import swing_signals as sig  # noqa: E402

CACHE_DIR = os.path.join("trader", "state", "bt_cache")
DAILY = os.path.join(CACHE_DIR, "daily_10y.json")
SIGNALS = os.path.join(CACHE_DIR, "swing_signals.jsonl")
TRADES = os.path.join(CACHE_DIR, "swing_trades.jsonl")
UNIVERSE = os.path.join("trader", "universe_nifty200.txt")
INSTRUMENTS = os.path.join("trader", "state", "instruments.json")
HOLDOUT_FROM = "2023-01-01"
TARGET_R = 2.5          # 2.2R nets ~1.9 after positional charges and fails the hard 2.0 floor
MAX_STOP_ATR = 3.0
HOLDS = (20, 40, 60)     # up to ~3 months (2026-11-19: multi-day equity calls)
RANDOM_PER_SIGNAL = 2
SEED = 7


# ---------------------------------------------------------------- data

def cmd_fetch(a):
    import data_in
    p = data_in.get_provider()
    syms = [l.split("|")[0].strip() for l in open(a.universe) if l.strip()]
    if "NIFTY" not in syms:
        syms.append("NIFTY")
    os.makedirs(CACHE_DIR, exist_ok=True)
    out, failed = {}, {}
    for i, s in enumerate(syms):
        try:
            d = p.get_historical_data(s, range_str=a.range, interval="1d")
            bars = [{"date": b["date"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or 0}
                    for b in (d.get("bars") or []) if None not in (b.get("o"), b.get("h"), b.get("l"), b.get("c"))]
            if not bars:
                raise ValueError("no bars")
            out[s] = sorted(bars, key=lambda b: b["date"])
        except Exception as e:
            failed[s] = str(e)[:100]
        if (i + 1) % 20 == 0:
            print("fetched %d/%d" % (i + 1, len(syms)), flush=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh)
    n = [len(v) for v in out.values()]
    print("wrote %s: %d symbols, bars per symbol median %d (min %d), failed %d %s"
          % (a.out, len(out), statistics.median(n) if n else 0, min(n) if n else 0, len(failed),
             list(failed.items())[:5]))
    return 0


def load_daily(path=DAILY):
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- signals

_W = {}


def _sig_init(path):
    data = load_daily(path)
    _W["data"] = data
    _W["nifty"] = data.get("NIFTY") or []
    _W["rs_rank"] = sig.rs_ranks(data)


def _signals_for(sym):
    bars = _W["data"][sym]
    return sig.scan_history(sym, bars, _W["nifty"], _W["rs_rank"])


def cmd_signals(a):
    data = load_daily(a.daily)
    syms = [s for s in data if s != "NIFTY"]
    del data
    with multiprocessing.Pool(a.workers, _sig_init, (a.daily,)) as pool:
        res = pool.map(_signals_for, syms, chunksize=4)
    n = 0
    with open(a.out, "w") as fh:
        for rows in res:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
                n += 1
    counts = {}
    for rows in res:
        for r in rows:
            counts[r["setup"]] = counts.get(r["setup"], 0) + 1
    print("wrote %s: %d signals" % (a.out, n))
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print("  %-20s %6d" % (k, v))
    return 0


# ---------------------------------------------------------------- simulation

def tick(x, t=0.05):
    return round(round(x / t) * t, 2)


def floor_tick(x, t=0.05):
    import math
    return round(math.floor(x / t + 1e-9) * t, 2)


def simulate(bars, i_entry, entry, stop, target, hold):
    """Walk daily bars from the entry day (index i_entry, filled at its open).
    Returns (outcome, exit_price, exit_index). Gap below stop -> exit at that open;
    a day touching both -> stop; neither within `hold` days -> close of the last day."""
    last = min(len(bars) - 1, i_entry + hold - 1)
    for k in range(i_entry, last + 1):
        b = bars[k]
        if k > i_entry and b["o"] <= stop:
            return "stop", b["o"], k
        if b["l"] <= stop:
            return "stop", stop, k
        if k > i_entry and b["o"] >= target:
            return "target", b["o"], k
        if b["h"] >= target:
            return "target", target, k
    return "time", bars[last]["c"], last


def incomplete(outcome, k, i_entry, hold):
    """A time exit before the full hold means the data ran out (a recent signal): not a result."""
    return outcome == "time" and k < i_entry + hold - 1


def plan_for(bars, i_signal, sig_stop):
    """Entry next open, stop = min-width of (pattern stop, entry - 3 ATR), target 2.2R. None if invalid."""
    import exit as exit_mod
    if i_signal + 1 >= len(bars):
        return None
    e = bars[i_signal + 1]["o"]
    atr, _n = exit_mod.atr_of(bars[max(0, i_signal - 13):i_signal + 1], 14)
    floor = e - MAX_STOP_ATR * atr
    st = floor_tick(max(sig_stop if sig_stop is not None else floor, floor))
    if not 0 < st < e:
        return None
    return {"entry": e, "stop": st, "target": tick(e + TARGET_R * (e - st)), "atr": atr}


def size_and_charge(sym, date, p, exit_px, size_mod, cfg):
    """Real positional sizing gate; returns (qty, net_R) or None when the budget refuses."""
    try:
        r = size_mod.size_trade({"symbol": sym, "side": "buy", "entry": p["entry"], "stop": p["stop"],
                                 "target": p["target"], "asset_type": "EQUITY", "trading_style": "positional",
                                 "market": "IN", "exchange": "NSE", "currency": "INR", "flat_by": "positional"},
                                cfg, cash=100000.0, today=date, market="IN", instruments_path=INSTRUMENTS,
                                trading_style="positional", exchange="NSE")
    except Exception:
        return None
    q, nrr = r.get("qty") or 0, r.get("net_reward_risk")
    if r.get("refusal") or not q or nrr is None or nrr < 2.0:
        return None
    e, st = r.get("entry") or p["entry"], r.get("stop") or p["stop"]
    chg = size_mod.calculate_indian_transaction_charges("EQUITY", "buy", e, exit_px, q, "positional", date)
    chg = chg.get("total") if isinstance(chg, dict) else float(chg or 0)
    return q, ((exit_px - e) * q - chg) / ((e - st) * q), chg


def _trade_init(daily_path, target_r=TARGET_R):
    import size
    _W["data"] = load_daily(daily_path)
    _W["size"] = size
    _W["cfg"] = size.load_config(None, market="IN")
    global TARGET_R
    TARGET_R = target_r
    import backtest_intraday
    backtest_intraday._memo_instruments()   # parse the instrument master once per worker


def _trade_job(s):
    bars = _W["data"][s["sym"]]
    idx = {b["date"]: i for i, b in enumerate(bars)}
    i = idx.get(s["date"])
    if i is None:
        return []
    out = []
    rng = random.Random("%s|%s|%s|%d" % (SEED, s["sym"], s["date"], hash(s["setup"]) % 997))
    p = plan_for(bars, i, s.get("stop"))
    if p is None:
        return []
    width_atr = (p["entry"] - p["stop"]) / p["atr"] if p["atr"] else None
    for hold in HOLDS:
        oc, px, k = simulate(bars, i + 1, p["entry"], p["stop"], p["target"], hold)
        if incomplete(oc, k, i + 1, hold):
            continue
        sz = size_and_charge(s["sym"], bars[i + 1]["date"], p, px, _W["size"], _W["cfg"])
        if sz is None:
            out.append({"kind": "refused", "setup": s["setup"], "hold": hold, "date": s["date"], "sym": s["sym"]})
            continue
        q, R, chg = sz
        out.append({"kind": "signal", "setup": s["setup"], "hold": hold, "date": s["date"], "sym": s["sym"],
                    "outcome": oc, "R": R, "days": k - i, "regime_up": s.get("regime_up"),
                    "width_atr": width_atr})
    # Random baseline: same stock, a random day within +-1 year, same stop width in ATRs.
    for _ in range(RANDOM_PER_SIGNAL):
        j = rng.randint(max(30, i - 250), min(len(bars) - 2, i + 250))
        if j + 1 >= len(bars) or width_atr is None:
            continue
        import exit as exit_mod
        atr, _n = exit_mod.atr_of(bars[max(0, j - 13):j + 1], 14)
        e = bars[j + 1]["o"]
        st = floor_tick(e - width_atr * atr)
        if not 0 < st < e:
            continue
        rp = {"entry": e, "stop": st, "target": tick(e + TARGET_R * (e - st)), "atr": atr}
        for hold in HOLDS:
            oc, px, k = simulate(bars, j + 1, e, st, rp["target"], hold)
            if incomplete(oc, k, j + 1, hold):
                continue
            sz = size_and_charge(s["sym"], bars[j + 1]["date"], rp, px, _W["size"], _W["cfg"])
            if sz is None:
                continue
            out.append({"kind": "random", "setup": s["setup"], "hold": hold, "date": bars[j]["date"],
                        "sym": s["sym"], "outcome": oc, "R": sz[1], "days": k - j,
                        "regime_up": sig.regime_up_on(_W["data"].get("NIFTY") or [], bars[j]["date"])})
    return out


def summarize(ts):
    if not ts:
        return {"n": 0}
    n = len(ts)
    R = [t["R"] for t in ts]
    by_year = {}
    for t in ts:
        by_year.setdefault(t["date"][:4], []).append(t["R"])
    yr = {y: statistics.mean(v) for y, v in by_year.items() if len(v) >= 10}
    return {"n": n, "win%": 100.0 * sum(r > 0 for r in R) / n,
            "target%": 100.0 * sum(t["outcome"] == "target" for t in ts) / n,
            "stop%": 100.0 * sum(t["outcome"] == "stop" for t in ts) / n,
            "avgR": statistics.mean(R), "days": statistics.mean(t["days"] for t in ts),
            "years_pos%": (100.0 * sum(v > 0 for v in yr.values()) / len(yr)) if yr else None,
            "worst_year": min(yr.items(), key=lambda x: x[1]) if yr else None}


def cmd_report(a):
    sigs = [json.loads(l) for l in open(a.signals) if l.strip()]
    if a.only:
        sigs = [s for s in sigs if a.only in s["setup"]]
    global TARGET_R
    TARGET_R = a.target_r
    if a.trades == TRADES:
        a.trades = TRADES.replace(".jsonl", "_%sR.jsonl" % a.target_r)
    if not os.path.exists(a.trades) or a.rebuild:
        with multiprocessing.Pool(a.workers, _trade_init, (a.daily, a.target_r)) as pool:
            rows = [r for rs in pool.imap_unordered(_trade_job, sigs, chunksize=50) for r in rs]
        with open(a.trades, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    else:
        rows = [json.loads(l) for l in open(a.trades) if l.strip()]
    report = build_report(rows)
    print(report_text(report))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(report, fh, indent=1)
    return 0


def build_report(rows):
    setups = sorted({r["setup"] for r in rows})
    out = []
    for st in setups:
        for hold in HOLDS:
            for regime in ("any", "nifty_up"):
                def sel(r, kind, part):
                    if r["setup"] != st or r["hold"] != hold or r["kind"] != kind:
                        return False
                    if regime == "nifty_up" and not r.get("regime_up"):
                        return False
                    return (r["date"] < HOLDOUT_FROM) if part == "design" else (r["date"] >= HOLDOUT_FROM)
                row = {"setup": st, "hold": hold, "regime": regime}
                for part in ("design", "holdout"):
                    s = summarize([r for r in rows if sel(r, "signal", part)])
                    b = summarize([r for r in rows if sel(r, "random", part)])
                    s["edge_vs_random"] = (s["avgR"] - b["avgR"]) if s.get("n") and b.get("n") else None
                    s["random_avgR"] = b.get("avgR")
                    row[part] = s
                refused = sum(1 for r in rows if r["setup"] == st and r["hold"] == hold and r["kind"] == "refused")
                row["refused"] = refused
                row["passes"] = passes(row)
                out.append(row)
    return out


def passes(row):
    d, h = row["design"], row["holdout"]
    if not d.get("n") or not h.get("n") or h["n"] < 100:
        return False
    for p in (d, h):
        if p["avgR"] <= 0 or p.get("edge_vs_random") is None or p["edge_vs_random"] < 0.1:
            return False
    yrs = [p.get("years_pos%") for p in (d, h) if p.get("years_pos%") is not None]
    return bool(yrs) and min(yrs) >= 60.0


def report_text(report):
    L = ["Swing backtest: entry next open, stop = pattern (<= %.0f ATR), target %.1fR, positional charges, "
         "real size_trade gate. Design < %s <= holdout." % (MAX_STOP_ATR, TARGET_R, HOLDOUT_FROM),
         "edge = avg net R minus random entries (same stocks/years/stop width). PASS = avgR>0 and edge>=0.1 on "
         "BOTH, holdout n>=100, >=60% of years positive.", ""]
    hdr = "%-20s %4s %-8s | %-46s | %-46s | %s" % ("setup", "hold", "regime", "DESIGN  n  win%  tgt%  stop%  avgR  edge",
                                                   "HOLDOUT n  win%  tgt%  stop%  avgR  edge", "verdict")
    L.append(hdr)

    def f(p):
        if not p.get("n"):
            return "%-46s" % "n=0"
        return "%5d %5.1f %5.1f %5.1f %+6.3f %+6.3f" % (p["n"], p["win%"], p["target%"], p["stop%"], p["avgR"],
                                                         p["edge_vs_random"] if p.get("edge_vs_random") is not None else 0)
    for r in sorted(report, key=lambda r: (-(r["holdout"].get("edge_vs_random") or -9), r["setup"])):
        L.append("%-20s %4d %-8s | %-46s | %-46s | %s" % (r["setup"], r["hold"], r["regime"], "        " + f(r["design"]),
                                                       "        " + f(r["holdout"]), "PASS" if r["passes"] else "-"))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = max(1, (os.cpu_count() or 2) - 1)
    f = sub.add_parser("fetch")
    f.add_argument("--universe", default=UNIVERSE)
    f.add_argument("--range", default="10y")
    f.add_argument("--out", default=DAILY)
    s = sub.add_parser("signals")
    s.add_argument("--daily", default=DAILY)
    s.add_argument("--out", default=SIGNALS)
    s.add_argument("--workers", type=int, default=w)
    r = sub.add_parser("report")
    r.add_argument("--daily", default=DAILY)
    r.add_argument("--signals", default=SIGNALS)
    r.add_argument("--trades", default=TRADES)
    r.add_argument("--rebuild", action="store_true", help="re-simulate trades even if the trades cache exists")
    r.add_argument("--only", default=None, help="only setups containing this text")
    r.add_argument("--json", default=None)
    r.add_argument("--target-r", type=float, default=TARGET_R, help="target in R (default %(default)s)")
    r.add_argument("--workers", type=int, default=w)
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    return {"fetch": cmd_fetch, "signals": cmd_signals, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
