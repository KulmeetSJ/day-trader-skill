#!/usr/bin/env python3
"""Odds for a watcher call -- how often trades LIKE it hit the target in the backtest (research only).

The odds are counted, not modelled: every ENTRY CONFIRMED signal in the backtest cache
(scripts/backtest_intraday.py, ~37 NIFTY200 sessions) is replayed with the watcher's own rule
(trader-exit swing stop, 2.2R target, the real trader-budget size gate incl. the 2.0 net R:R
floor), and the outcome is recorded: TARGET, STOP, or still open at 15:20 (flat). A live call
is then looked up in a small table of similar past trades.

"Similar" = same time-of-day bucket and same target-distance bucket (how many ATRs the target
is above entry -- far targets are rarely reached). Each bucket's rate is shrunk toward the
overall rate so a thin bucket can't print an extreme number. The table is fitted on the
DESIGN sessions only and scored on the HOLDOUT sessions it never saw; the holdout score is
stored with the table and printed beside every probability.

    python3 scripts/probability.py build        # replay the cache, fit, write trader/state/prob_model.json
    python3 scripts/probability.py show         # print the table and its holdout check

Nothing here predicts direction from news or the market; it is a base rate for this exact
setup + stop + target rule. It places nothing.
"""

import argparse
import datetime
import json
import multiprocessing
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
import backtest_intraday as bt  # noqa: E402  (also puts the skill folders on sys.path)

MODEL_PATH = os.path.join("trader", "state", "prob_model.json")
STOP_RULE, TARGET_R = "swing", 2.2
PRIOR_N = 30.0            # shrinkage: a bucket needs ~30 trades before its own rate dominates
# Time-of-day buckets (entry time, IST) and target-distance buckets (target - entry, in ATRs).
TIME_EDGES = ("10:30", "12:30")                  # <10:30 | 10:30-12:30 | >=12:30
OUTCOMES = ("target", "stop", "flat_green", "flat_red")


def time_bucket(hhmm):
    hhmm = str(hhmm)[:5].zfill(5)
    if hhmm < TIME_EDGES[0]:
        return "before %s" % TIME_EDGES[0]
    if hhmm < TIME_EDGES[1]:
        return "%s-%s" % TIME_EDGES
    return "after %s" % TIME_EDGES[1]


def dist_bucket(target_atr, edges):
    if target_atr is None:
        return None
    if target_atr < edges[0]:
        return "near (<%.1f ATR)" % edges[0]
    if target_atr < edges[1]:
        return "mid (%.1f-%.1f ATR)" % tuple(edges)
    return "far (>=%.1f ATR)" % edges[1]


# ---------------------------------------------------------------- replay

def _job(s):
    import size
    import timemachine
    import exit as exit_mod
    bt._memo_instruments()
    D = bt._W["S"][s["sym"]][s["date"]]
    st = bt.stop_for(STOP_RULE, s, D)
    if st is None:
        return None
    e = s["entry"]
    tg = bt.tick(e + TARGET_R * (e - st))
    if "cfg" not in bt._W:
        bt._W["cfg"] = size.load_config(None, market="IN")
    try:
        r = size.size_trade({"symbol": s["sym"], "side": "buy", "entry": e, "stop": st, "target": tg,
                             "asset_type": "EQUITY", "trading_style": "intraday", "market": "IN", "exchange": "NSE",
                             "currency": "INR"}, bt._W["cfg"], cash=100000.0, today=s["date"], market="IN",
                            instruments_path=bt.INSTRUMENTS, trading_style="intraday", exchange="NSE")
    except Exception:
        return None
    q, nrr = r.get("qty") or 0, r.get("net_reward_risk")
    if r.get("refusal") or not q or nrr is None or nrr < 2.0:
        return None
    oc, px, _ = timemachine.simulate_exit(D, s["slot"] + 1, bt.FLAT_SLOT, e, r["stop"], r["target"], "buy")
    bars = [D[i] for i in range(0, s["slot"] + 1) if i in D]
    atr, _n = exit_mod.atr_of(bars)     # trader-exit's own session ATR (the live plan's "atr")
    chg = (r.get("transaction_costs") or {}).get("total") or 0.0
    pnl = q * (px - e) - chg
    if oc == "target":
        o = "target"
    elif oc == "stop":
        o = "stop"
    else:
        o = "flat_green" if pnl > 0 else "flat_red"
    return {"sym": s["sym"], "date": s["date"], "time": s["time"], "setup": s["setup"], "outcome": o,
            "target_atr": (r["target"] - e) / atr if atr else None, "R_net": pnl / (q * (e - r["stop"]))}


def _init(bars_path, signals_path):
    S, nifty, sigs, dates, cut = bt.load_context(bars_path, signals_path)
    bt._W.update(S=S)


def replay(bars_path, signals_path, workers):
    S, nifty, sigs, dates, cut = bt.load_context(bars_path, signals_path)
    del S
    with multiprocessing.Pool(workers, _init, (bars_path, signals_path)) as pool:
        trades = [t for t in pool.map(_job, sigs, chunksize=200) if t]
    # one trade per stock per day (the watcher alerts a stock once while its call is in range)
    seen, out = set(), []
    for t in sorted(trades, key=lambda x: (x["date"], x["time"])):
        k = (t["sym"], t["date"])
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out, cut


# ---------------------------------------------------------------- fit / score

def rates(trades):
    n = len(trades)
    return {o: (sum(1 for t in trades if t["outcome"] == o) / float(n)) if n else 0.0 for o in OUTCOMES}


def fit(trades, keys, edges):
    """Bucket rates, each shrunk toward the overall rate with PRIOR_N pseudo-trades."""
    base = rates(trades)
    cells = {}
    for t in trades:
        cells.setdefault(cell_key(t["time"], t["target_atr"], keys, edges), []).append(t)
    table = {}
    for k, ts in cells.items():
        r, n = rates(ts), len(ts)
        table[k] = {"n": n, "p": {o: (r[o] * n + base[o] * PRIOR_N) / (n + PRIOR_N) for o in OUTCOMES}}
    return {"keys": list(keys), "edges": list(edges), "base": base, "n": len(trades), "table": table}


def cell_key(hhmm, target_atr, keys, edges):
    parts = []
    if "time" in keys:
        parts.append(time_bucket(hhmm))
    if "dist" in keys:
        parts.append(dist_bucket(target_atr, edges) or "unknown")
    return " | ".join(parts) or "all"


def predict(model, hhmm, target_atr):
    k = cell_key(hhmm, target_atr, model["keys"], model["edges"])
    c = model["table"].get(k)
    if c is None:
        return {"cell": k, "n": 0, "p": dict(model["base"])}
    return {"cell": k, "n": c["n"], "p": dict(c["p"])}


def brier(model, trades):
    """Multi-class Brier score over (target, stop, flat): lower is better."""
    if not trades:
        return None
    tot = 0.0
    for t in trades:
        p = predict(model, t["time"], t["target_atr"])["p"]
        pf = p["flat_green"] + p["flat_red"]
        o = {"target": (1, 0, 0), "stop": (0, 1, 0)}.get(t["outcome"], (0, 0, 1))
        tot += (p["target"] - o[0]) ** 2 + (p["stop"] - o[1]) ** 2 + (pf - o[2]) ** 2
    return tot / len(trades)


def tertiles(xs):
    xs = sorted(x for x in xs if x is not None)
    return (round(xs[len(xs) // 3], 1), round(xs[2 * len(xs) // 3], 1)) if xs else (1.0, 2.0)


def cmd_build(a):
    trades, cut = replay(a.bars, a.signals, a.workers)
    design = [t for t in trades if t["date"] < cut]
    hold = [t for t in trades if t["date"] >= cut]
    edges = tertiles([t["target_atr"] for t in design])
    cands = {}
    for keys in ((), ("time",), ("dist",), ("time", "dist")):
        m = fit(design, keys, edges)
        cands[" + ".join(keys) or "base rate only"] = (m, brier(m, hold))
    base_score = cands["base rate only"][1]
    # Model choice is made on the DESIGN set (inner split: early design fits, late design scores),
    # so the holdout stays a clean test of whatever is picked.
    dcut = sorted({t["date"] for t in design})[int(len({t["date"] for t in design}) * 0.7)]
    d_fit, d_val = [t for t in design if t["date"] < dcut], [t for t in design if t["date"] >= dcut]
    inner = {name: brier(fit(d_fit, m["keys"], edges), d_val) for name, (m, _) in cands.items()}
    pick = min(inner, key=inner.get)
    model, score = cands[pick]
    hold_actual = rates(hold)
    model.update({
        "built": datetime.datetime.now(bt.IST).strftime("%Y-%m-%d %H:%M"),
        "rule": "%s stop, %.1fR target, trader-budget gate (net R:R >= 2.0), flat by 15:20" % (STOP_RULE, TARGET_R),
        "sessions": {"design": "%s..%s" % (design[0]["date"], design[-1]["date"]) if design else "",
                     "holdout": "%s..%s" % (hold[0]["date"], hold[-1]["date"]) if hold else ""},
        "n_design": len(design), "n_holdout": len(hold), "picked": pick,
        "holdout_brier": {name: round(s, 4) for name, (_, s) in cands.items()},
        "inner_brier": {name: round(s, 4) for name, s in inner.items()},
        "holdout_actual": hold_actual,
        # "Beats" needs a >= 1% relative Brier improvement on the holdout -- a smaller gain is noise.
        "beats_base_rate_on_holdout": bool(score < base_score * 0.99) if pick != "base rate only" else False,
    })
    model["holdout_reliability"] = reliability(model, hold)
    tmp = MODEL_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(model, fh, indent=1)
    os.replace(tmp, MODEL_PATH)
    print(show_text(model))
    return 0


def reliability(model, trades):
    """Per cell: what the table says vs what happened on the holdout."""
    out = {}
    for t in trades:
        c = predict(model, t["time"], t["target_atr"])
        r = out.setdefault(c["cell"], {"n": 0, "pred_target": c["p"]["target"], "target": 0, "stop": 0,
                                       "pred_stop": c["p"]["stop"]})
        r["n"] += 1
        r["target"] += t["outcome"] == "target"
        r["stop"] += t["outcome"] == "stop"
    return out


def load_model(path=MODEL_PATH):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def odds_for(model, entry_time, entry, target, atr):
    """Live lookup. Returns None (no number shown) if the model is missing or the inputs are."""
    if not model:
        return None
    try:
        e, tg, a = float(entry), float(target), float(atr)
    except (TypeError, ValueError):
        if "dist" in model["keys"]:
            return None
        e = tg = a = None
    tatr = (tg - e) / a if (a and e is not None) else None
    r = predict(model, entry_time, tatr)
    r["target_atr"] = tatr
    r["p_green"] = r["p"]["target"] + r["p"]["flat_green"]
    return r


def odds_text(o, model):
    if not o:
        return "odds n/a (no probability model; run scripts/probability.py build)"
    p = o["p"]
    note = "" if model.get("beats_base_rate_on_holdout") else " (no better than the overall base rate on unseen days)"
    return ("odds from %d similar backtest trades%s: target %.0f%% | stop %.0f%% | open at 15:20 %.0f%% "
            "(green %.0f%% overall)" % (o["n"], note, 100 * p["target"], 100 * p["stop"],
                                        100 * (p["flat_green"] + p["flat_red"]), 100 * o["p_green"]))


def show_text(m):
    L = ["Probability table (%s) -- built %s" % (m["rule"], m["built"]),
         "fitted on %d design trades (%s), tested on %d holdout trades (%s)"
         % (m["n_design"], m["sessions"]["design"], m["n_holdout"], m["sessions"]["holdout"]),
         "", "Holdout Brier (lower = better; model picked on design data only):"]
    for name, s in sorted(m["holdout_brier"].items(), key=lambda x: x[1]):
        L.append("  %-16s %.4f%s" % (name, s, "   <- used" if name == m["picked"] else ""))
    L.append("Beats the plain base rate on unseen sessions: %s" % ("YES" if m["beats_base_rate_on_holdout"] else "NO"))
    b, h = m["base"], m["holdout_actual"]
    L += ["", "Overall (design):  target %.1f%%  stop %.1f%%  open at 15:20 %.1f%% (green %.1f%%)"
          % (100 * b["target"], 100 * b["stop"], 100 * (b["flat_green"] + b["flat_red"]), 100 * b["flat_green"]),
          "Overall (holdout): target %.1f%%  stop %.1f%%  open at 15:20 %.1f%% (green %.1f%%)"
          % (100 * h["target"], 100 * h["stop"], 100 * (h["flat_green"] + h["flat_red"]), 100 * h["flat_green"]),
          "", "%-40s %5s %8s %8s | holdout: %5s %8s %8s" % ("bucket", "n", "P(tgt)", "P(stop)", "n", "tgt", "stop")]
    for k in sorted(m["table"]):
        c, r = m["table"][k], m["holdout_reliability"].get(k, {})
        hn = r.get("n", 0)
        L.append("%-40s %5d %7.1f%% %7.1f%% | %14d %7.1f%% %7.1f%%"
                 % (k, c["n"], 100 * c["p"]["target"], 100 * c["p"]["stop"], hn,
                    100.0 * r.get("target", 0) / hn if hn else 0, 100.0 * r.get("stop", 0) / hn if hn else 0))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--bars", default=bt.BARS)
    b.add_argument("--signals", default=bt.SIGNALS)
    b.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    sub.add_parser("show")
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    if a.cmd == "build":
        return cmd_build(a)
    m = load_model()
    if not m:
        print("no model yet: python3 scripts/probability.py build")
        return 1
    print(show_text(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
