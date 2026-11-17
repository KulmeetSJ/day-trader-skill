#!/usr/bin/env python3
"""Intraday backtest harness (research only, paper/long-only, never places orders).

Replays the live scanner's own signal logic (trader-entry `evaluate`, the function
`scan.py intraday` calls) over ~40 past sessions, then tests exit and filter rules
on those signals. Every trade is gated by the real `trader-budget/size.py:
size_trade()`, so qty, the 2.0 net R:R floor and Indian charges are the live ones.

    python3 scripts/backtest_intraday.py fetch                # 60d of 5m bars, NIFTY200 + NIFTY
    python3 scripts/backtest_intraday.py signals              # every ENTRY CONFIRMED signal
    python3 scripts/backtest_intraday.py report               # variants: design vs holdout
    python3 scripts/backtest_intraday.py calibrate            # Time Machine odds vs reality

Sessions are split by date: the oldest ~70 % are the DESIGN set and the newest
~30 % the HOLDOUT. A rule only counts as better if it is better on the holdout.
Fills are idealized (entry at the trigger bar's close, stop/target at their price);
a bar that touches both stop and target counts as the stop.
"""

import argparse
import datetime
import json
import multiprocessing
import os
import statistics
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-entry", "trader-scanner", "trader-budget", "trader-timemachine", "trader-exit"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
CACHE_DIR = os.path.join("trader", "state", "bt_cache")
BARS = os.path.join(CACHE_DIR, "bars_60d.json")
SIGNALS = os.path.join(CACHE_DIR, "signals.jsonl")
UNIVERSE = os.path.join("trader", "universe_nifty200.txt")
INSTRUMENTS = os.path.join("trader", "state", "instruments.json")
SETUPS = ("breakout_intraday", "vwap_reclaim", "ema_alignment", "rsi_momentum")
OPEN_MIN = 9 * 60 + 15
FLAT_SLOT = (15 * 60 + 20 - OPEN_MIN) // 5 - 1   # last bar that closes by 15:20 (slot 72)
WARMUP_SESSIONS = 3
HOLDOUT_FRAC = 0.3


def ist(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST)


def slot_of(ts):
    d = ist(ts)
    return (d.hour * 60 + d.minute - OPEN_MIN) // 5


# ---------------------------------------------------------------- data

def load_bars(path=BARS):
    with open(path) as fh:
        return json.load(fh)


def sessions(series):
    """{date_iso: {slot: bar}} for one series (only bars inside 09:15-15:30)."""
    out = {}
    for i, t in enumerate(series.get("t") or []):
        if t is None or series["c"][i] is None:
            continue
        sl = slot_of(t)
        if not 0 <= sl < 75:
            continue
        out.setdefault(ist(t).date().isoformat(), {})[sl] = {
            "t": int(t), "o": series["o"][i], "h": series["h"][i], "l": series["l"][i], "c": series["c"][i],
            "v": (series.get("v") or [0] * len(series["t"]))[i] or 0}
    return out


def slice_series(series, keep_dates):
    keep = set(keep_dates)
    idx = [i for i, t in enumerate(series["t"]) if t is not None and ist(t).date().isoformat() in keep]
    out = {k: [series[k][i] for i in idx] for k in ("t", "o", "h", "l", "c", "v") if k in series}
    for k in series:
        if k not in out:
            out[k] = series[k]
    return out


def cmd_fetch(a):
    import scan
    import size
    cfg = size.load_config(None, market="IN")
    prov = scan._get_in_provider(cfg)
    syms = [l.split("|")[0].strip() for l in open(a.universe) if l.strip()]
    if "NIFTY" not in syms:
        syms.append("NIFTY")
    os.makedirs(CACHE_DIR, exist_ok=True)
    out, failed = {}, {}
    for k in range(0, len(syms), 20):
        chunk = syms[k:k + 20]
        got, bad = scan._fetch_in_provider_series(prov, chunk, a.range, "5m", 0, exchange="NSE", now=None)
        for s, v in got.items():
            out[s] = {kk: v[kk] for kk in ("t", "o", "h", "l", "c", "v") if kk in v}
        failed.update(bad or {})
        print("fetched %d/%d" % (min(k + 20, len(syms)), len(syms)), flush=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh)
    days = sorted({d for s in out.values() for d in sessions(s)})
    print("wrote %s: %d symbols, %d sessions (%s .. %s), failed %d"
          % (a.out, len(out), len(days), days[0] if days else "-", days[-1] if days else "-", len(failed)))
    return 0


# ---------------------------------------------------------------- signals

_W = {}


def _worker_init(bars_path):
    import entry
    import scan
    _W["entry"], _W["scan"] = entry, scan
    _W["cfg"] = entry.read_config(None, market="IN")
    _W["bars"] = load_bars(bars_path)


def _signals_for(sym):
    entry, scan, cfg = _W["entry"], _W["scan"], _W["cfg"]
    series = _W["bars"][sym]
    days = sorted(sessions(series))
    rows = []
    for i in range(WARMUP_SESSIONS, len(days)):
        D = days[i]
        sub = slice_series(series, days[i - WARMUP_SESSIONS:i + 1])
        try:
            chart = scan._series_to_entry_chart(entry, sym, sub, market="IN", interval="5m", is_offline=True)
        except Exception:
            continue
        d = datetime.date.fromisoformat(D)
        seen = set()
        for step in range(1, 70):            # bar closes 09:20 .. 15:00
            m = OPEN_MIN + 5 * step
            now = datetime.datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=IST).astimezone(datetime.timezone.utc)
            for st in SETUPS:
                try:
                    res = entry.evaluate(chart, now, setup=st, interval="5m", cfg=cfg, market="IN",
                                         trading_style="intraday", exchange="NSE", instruments_path=INSTRUMENTS)
                except Exception:
                    continue
                dec = res.get("decision") or {}
                if res.get("decision_state") != "ENTRY CONFIRMED" or not dec.get("actionable"):
                    continue
                tr = res.get("trigger") or {}
                key = (st, tr.get("close_time"))
                if key in seen or tr.get("close_time") != "%02d:%02d" % (m // 60, m % 60):
                    continue                     # only fresh triggers, seen at their own bar close
                seen.add(key)
                ind = res.get("indicators") or {}
                rows.append({"sym": sym, "date": D, "setup": st, "time": tr.get("close_time"),
                             "slot": step - 1, "entry": dec.get("entry_reference"), "scanner_stop": dec.get("stop_loss"),
                             "atr": ind.get("atr14"), "vwap": ind.get("vwap"), "ema9": ind.get("ema9"),
                             "ema21": ind.get("ema21"), "ema50": ind.get("ema50"), "rsi": ind.get("rsi14")})
    return rows


def cmd_signals(a):
    bars = load_bars(a.bars)
    syms = sorted(s for s in bars if s != "NIFTY")
    if a.limit:
        syms = syms[:a.limit]
    t0 = time.time()
    n = 0
    with open(a.out, "w") as fh, multiprocessing.Pool(a.workers, _worker_init, (a.bars,)) as pool:
        for k, rows in enumerate(pool.imap_unordered(_signals_for, syms)):
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            n += len(rows)
            if (k + 1) % 20 == 0:
                print("%d/%d symbols, %d signals, %.0fs" % (k + 1, len(syms), n, time.time() - t0), flush=True)
    print("wrote %s: %d signals from %d symbols in %.0fs" % (a.out, n, len(syms), time.time() - t0))
    return 0


# ---------------------------------------------------------------- variants

def tick(x, t=0.05):
    return round(round(x / t) * t, 2)


def floor_tick(x, t=0.05):
    import math
    return round(math.floor(x / t + 1e-9) * t, 2)


def stop_for(rule, sig, D):
    """Stop price for one signal under a stop rule; None when the rule has no valid stop."""
    e, atr = sig["entry"], sig.get("atr") or 0
    if rule == "scanner":
        s = sig["scanner_stop"]
    elif rule.startswith("atr"):
        s = e - float(rule[3:]) * atr if atr else None
    elif rule == "vwap":
        s = (sig["vwap"] - 0.25 * atr) if sig.get("vwap") and atr else None
    elif rule == "swing":
        # trader-exit's exact `--stop swing` rule: lowest low of the entry bar and the 5 before
        # it, minus 0.1 x trader-exit's session ATR(14)
        import exit as exit_mod
        bars = [D[i] for i in range(0, sig["slot"] + 1) if i in D]
        if len(bars) < 3:
            return None
        a, _n = exit_mod.atr_of(bars)
        s = min(b["l"] for b in bars[-exit_mod.SWING_BARS:]) - float(exit_mod.SWING_BUFFER_ATR) * a
    else:
        raise ValueError(rule)
    if s is None:
        return None
    s = floor_tick(s)
    return s if 0 < s < e else None


def nifty_up(nifty_sessions, date, slot):
    """NIFTY above its session open and its EMA20 of 5m closes at the signal bar."""
    D = nifty_sessions.get(date) or {}
    if slot not in D or 0 not in D:
        return None
    closes = []
    for d in sorted(nifty_sessions):
        if d > date:
            break
        for sl in sorted(nifty_sessions[d]):
            if d == date and sl > slot:
                break
            closes.append(nifty_sessions[d][sl]["c"])
    k = 2.0 / 21
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    c = D[slot]["c"]
    return c > D[0]["o"] and c > ema


FILTERS = {
    "none": lambda s, ctx: True,
    "after0945": lambda s, ctx: s["time"] >= "09:45",
    "trend": lambda s, ctx: bool(s.get("vwap") and s.get("ema21") and s["entry"] > s["vwap"] and s["entry"] > s["ema21"]
                                and (s.get("ema9") or 0) > s["ema21"]),
    "nifty_up": lambda s, ctx: bool(ctx["nifty"]),
}


def run_variant(sigs, ctx_of, D_of, size_mod, cfg, stop_rule, target_r, filters, breakeven_r=None, cash=100000.0):
    trades = []
    for s in sigs:
        ctx = ctx_of(s)
        if not all(FILTERS[f](s, ctx) for f in filters):
            continue
        D = D_of(s)
        st = stop_for(stop_rule, s, D)
        if st is None:
            continue
        e = s["entry"]
        tg = tick(e + target_r * (e - st))
        plan = {"symbol": s["sym"], "side": "buy", "entry": e, "stop": st, "target": tg, "asset_type": "EQUITY",
                "trading_style": "intraday", "market": "IN", "exchange": "NSE", "currency": "INR"}
        try:
            r = size_mod.size_trade(plan, cfg, cash=cash, today=s["date"], market="IN", instruments_path=INSTRUMENTS,
                                    trading_style="intraday", exchange="NSE")
        except Exception:
            continue
        q, nrr = r.get("qty") or 0, r.get("net_reward_risk")
        if r.get("refusal") or not q or nrr is None or nrr < 2.0:
            continue
        import timemachine
        oc, px, _ = timemachine.simulate_exit(D, s["slot"] + 1, FLAT_SLOT, e, r["stop"], r["target"], "buy",
                                              breakeven_r=breakeven_r)
        risk = e - r["stop"]
        chg = (r.get("transaction_costs") or {}).get("total") or 0.0
        pnl = q * (px - e) - chg
        trades.append({"sym": s["sym"], "date": s["date"], "time": s["time"], "setup": s["setup"], "outcome": oc,
                       "R_gross": (px - e) / risk, "R_net": pnl / (q * risk), "pnl": pnl, "qty": q})
    return trades


def portfolio(trades, max_trades=3, max_daily_loss=5000.0):
    """What a human taking alerts in time order could do: first signal per stock,
    at most 3 trades a day, stop for the day after the daily loss limit."""
    by_day = {}
    for t in sorted(trades, key=lambda x: (x["date"], x["time"])):
        by_day.setdefault(t["date"], []).append(t)
    taken = []
    for d, ts in by_day.items():
        seen, loss, n = set(), 0.0, 0
        for t in ts:
            if n >= max_trades or -loss >= max_daily_loss:
                break
            if t["sym"] in seen:
                continue
            seen.add(t["sym"])
            taken.append(t)
            n += 1
            loss += min(0.0, t["pnl"])
    return taken


def summarize(trades):
    if not trades:
        return {"n": 0}
    n = len(trades)
    cnt = lambda o: sum(1 for t in trades if t["outcome"] == o)
    days = {}
    for t in trades:
        days[t["date"]] = days.get(t["date"], 0.0) + t["pnl"]
    return {"n": n, "stop%": 100.0 * cnt("stop") / n, "target%": 100.0 * cnt("target") / n,
            "be%": 100.0 * cnt("breakeven") / n, "flat%": 100.0 * cnt("close") / n,
            "win%": 100.0 * sum(1 for t in trades if t["pnl"] > 0) / n,
            "avgR_net": statistics.mean(t["R_net"] for t in trades), "pnl": sum(t["pnl"] for t in trades),
            "worst_day": min(days.values()), "days": len(days)}


def _memo_instruments():
    """Research-process only: parse the instrument master once (size_trade re-reads it per call)."""
    import instruments
    orig = instruments.load_instrument_cache
    memo = {}

    def cached(path=instruments.DEFAULT_CACHE_PATH, max_age_days=1, as_of_date=None):
        key = (path, max_age_days)
        if key not in memo:
            memo[key] = orig(path, max_age_days=3650, as_of_date=None)
        return memo[key]
    instruments.load_instrument_cache = cached


def load_context(bars_path, signals_path):
    bars = load_bars(bars_path)
    sigs = [json.loads(l) for l in open(signals_path) if l.strip()]
    S = {s: sessions(v) for s, v in bars.items()}
    nifty = S.get("NIFTY") or {}
    dates = sorted({s["date"] for s in sigs})
    cut = dates[int(len(dates) * (1 - HOLDOUT_FRAC))] if dates else None
    return S, nifty, sigs, dates, cut


VARIANTS = [
    # (name, stop rule, target R, filters, breakeven R)
    ("baseline atr1.5 3R", "atr1.5", 3.0, (), None),
    ("scanner stop 2.2R", "scanner", 2.2, (), None),
    ("atr2.5 2.2R", "atr2.5", 2.2, (), None),
    ("atr2.5 3R", "atr2.5", 3.0, (), None),
    ("atr3 2.2R", "atr3", 2.2, (), None),
    ("atr4 2.2R", "atr4", 2.2, (), None),
    ("vwap stop 2.2R", "vwap", 2.2, (), None),
    ("swing stop 2.2R", "swing", 2.2, (), None),
    ("atr1.5 3R +after0945", "atr1.5", 3.0, ("after0945",), None),
    ("atr1.5 3R +trend", "atr1.5", 3.0, ("trend",), None),
    ("atr1.5 3R +nifty_up", "atr1.5", 3.0, ("nifty_up",), None),
    ("atr1.5 3R +BE1R", "atr1.5", 3.0, (), 1.0),
    ("atr3 2.2R +after0945", "atr3", 2.2, ("after0945",), None),
    ("atr3 2.2R +trend", "atr3", 2.2, ("trend",), None),
    ("atr3 2.2R +nifty_up", "atr3", 2.2, ("nifty_up",), None),
    ("atr3 2.2R +trend+nifty", "atr3", 2.2, ("trend", "nifty_up"), None),
    ("atr3 2.2R +BE1R", "atr3", 2.2, (), 1.0),
    ("atr3 2.2R +all", "atr3", 2.2, ("after0945", "trend", "nifty_up"), None),
    ("atr4 2.2R +trend+nifty", "atr4", 2.2, ("trend", "nifty_up"), None),
    ("atr4 2.2R +all", "atr4", 2.2, ("after0945", "trend", "nifty_up"), None),
    ("swing 2.2R +trend+nifty", "swing", 2.2, ("trend", "nifty_up"), None),
    ("vwap 2.2R +trend+nifty", "vwap", 2.2, ("trend", "nifty_up"), None),
    ("swing stop 2.5R", "swing", 2.5, (), None),
    ("swing stop 3R", "swing", 3.0, (), None),
    ("swing 2.2R +after0945", "swing", 2.2, ("after0945",), None),
    ("swing 2.2R +BE1R", "swing", 2.2, (), 1.0),
    ("swing 2.2R +trend", "swing", 2.2, ("trend",), None),
    ("atr3 2.5R", "atr3", 2.5, (), None),
]


def _variant_job(args):
    name, stop_rule, target_r, filters, be = args
    import size
    _memo_instruments()
    S, nifty, sigs, cut = _W["S"], _W["nifty"], _W["sigs"], _W["cut"]
    cfg = size.load_config(None, market="IN")
    ctx_of = lambda s: {"nifty": nifty_up(nifty, s["date"], s["slot"])} if "nifty_up" in filters else {}
    D_of = lambda s: S[s["sym"]][s["date"]]
    tr = run_variant(sigs, ctx_of, D_of, size, cfg, stop_rule, target_r, filters, be)
    out = {"name": name}
    for part, sel in (("design", lambda t: t["date"] < cut), ("holdout", lambda t: t["date"] >= cut)):
        ts = [t for t in tr if sel(t)]
        out[part] = summarize(ts)
        out[part + "_portfolio"] = summarize(portfolio(ts))
    out["by_setup_holdout"] = {st: summarize([t for t in tr if t["date"] >= cut and t["setup"] == st]) for st in SETUPS}
    return out


def _report_init(bars_path, signals_path):
    S, nifty, sigs, dates, cut = load_context(bars_path, signals_path)
    _W.update(S=S, nifty=nifty, sigs=sigs, cut=cut)


def fmt(m):
    if not m.get("n"):
        return "  n=0"
    return ("n=%4d stop %4.1f%% tgt %4.1f%% be %4.1f%% flat %4.1f%% win %4.1f%% avgR(net) %+.3f  P&L %+9.0f  worst day %+7.0f"
            % (m["n"], m["stop%"], m["target%"], m["be%"], m["flat%"], m["win%"], m["avgR_net"], m["pnl"], m["worst_day"]))


def cmd_report(a):
    S, nifty, sigs, dates, cut = load_context(a.bars, a.signals)
    print("%d signals over %d sessions; design %s..%s, holdout %s..%s"
          % (len(sigs), len(dates), dates[0], [d for d in dates if d < cut][-1], cut, dates[-1]))
    with multiprocessing.Pool(a.workers, _report_init, (a.bars, a.signals)) as pool:
        res = pool.map(_variant_job, VARIANTS)
    for r in res:
        print("\n== %s" % r["name"])
        for part in ("design", "holdout", "design_portfolio", "holdout_portfolio"):
            print("  %-18s %s" % (part, fmt(r[part])))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1)
        print("\nwrote %s" % a.json)
    return 0


# ---------------------------------------------------------------- calibration of the Time Machine

def _tm_payload(series, sym):
    bars = [{"t": t, "o": series["o"][i], "h": series["h"][i], "l": series["l"][i], "c": series["c"][i],
             "v": (series.get("v") or [0])[i] if series.get("v") else 0}
            for i, t in enumerate(series["t"]) if t is not None and series["c"][i] is not None]
    return {"symbol": sym, "bars": bars, "provider": "offline", "data_status": "HISTORICAL",
            "meta": {"symbol": sym, "dataGranularity": "5m"}}


def _calib_job(s):
    import size
    import timemachine
    _memo_instruments()
    S, bars_raw = _W["S"], _W["bars_raw"]
    stop_rule, target_r = _W["stop_rule"], _W["target_r"]
    D = S[s["sym"]][s["date"]]
    st = stop_for(stop_rule, s, D)
    if st is None:
        return None
    e = s["entry"]
    tg = tick(e + target_r * (e - st))
    if "size_cfg" not in _W:
        _W["size_cfg"] = size.load_config(None, market="IN")
    try:   # only trades the budget gate passes: those are the ones the Time Machine is asked about
        r = size.size_trade({"symbol": s["sym"], "side": "buy", "entry": e, "stop": st, "target": tg,
                             "asset_type": "EQUITY", "trading_style": "intraday", "market": "IN", "exchange": "NSE",
                             "currency": "INR"}, _W["size_cfg"], cash=100000.0, today=s["date"], market="IN",
                            instruments_path=INSTRUMENTS, trading_style="intraday", exchange="NSE")
    except Exception:
        return None
    if r.get("refusal") or not r.get("qty") or (r.get("net_reward_risk") or 0) < 2.0:
        return None
    st, tg = r["stop"], r["target"]
    oc, px, _ = timemachine.simulate_exit(D, s["slot"] + 1, FLAT_SLOT, e, st, tg, "buy")
    close_min = OPEN_MIN + 5 * (s["slot"] + 1)
    import exit as exit_mod
    bars = [D[i] for i in range(0, s["slot"] + 1) if i in D]
    atr, _n = exit_mod.atr_of(bars)
    rule_fields = {"stop_rule": "atr", "atr": atr} if stop_rule.startswith("atr") else (
        {"stop_rule": "swing", "atr": atr, "swing_bars": exit_mod.SWING_BARS} if stop_rule == "swing" else
        {"stop_rule": "pct"})
    base = {"symbol": s["sym"], "side": "buy", "date": s["date"], "entry": e, "stop": st, "target": tg,
            "entry_time": "%02d:%02d" % (close_min // 60, close_min % 60), "market": "IN"}
    old_plan = dict(base, setup="level")                                    # the old TM: MC only, to 15:30
    new_plan = dict(base, setup=s["setup"], flat_by="15:20", **rule_fields)  # setup history, to flat-by
    now = timemachine.parse_now("%s %02d:%02d" % (s["date"], close_min // 60, close_min % 60), market="IN")
    payload = _tm_payload(bars_raw[s["sym"]], s["sym"])
    try:
        old = timemachine.time_machine(payload, timemachine.normalize_plan(old_plan, "calibrate", market="IN"), now,
                                       replays=_W["replays"], source="offline", simulated=True)
        new = timemachine.time_machine(payload, timemachine.normalize_plan(new_plan, "calibrate", market="IN"), now,
                                       replays=_W["replays"], source="offline", simulated=True)
    except Exception as ex:
        return {"error": str(ex)[:120]}
    h = new.get("history") or {}
    return {"outcome": oc, "R": (px - e) / (e - st),
            "mc": {k: old["mc"][k] for k in ("p_target", "p_stop", "p_neither", "avg_R")},
            "mc_new": {k: new["mc"][k] for k in ("p_target", "p_stop", "p_neither", "avg_R")},
            "hist": {k: h.get(k) for k in ("triggered", "target", "stop", "close", "avg_R", "avg_R_net")},
            "pred": new.get("prediction")}


def _calib_init(bars_path, signals_path, stop_rule, target_r, replays, tm_kwargs):
    S, nifty, sigs, dates, cut = load_context(bars_path, signals_path)
    _W.update(S=S, bars_raw=load_bars(bars_path), stop_rule=stop_rule, target_r=target_r, replays=replays,
              tm_kwargs=tm_kwargs)


def brier(rows, key):
    """Multi-class Brier score over (target, stop, neither) for the given probability source."""
    tot = 0.0
    for r in rows:
        p = r[key]
        o = {"target": (1, 0, 0), "stop": (0, 1, 0)}.get(r["outcome"], (0, 0, 1))
        tot += (p[0] - o[0]) ** 2 + (p[1] - o[1]) ** 2 + (p[2] - o[2]) ** 2
    return tot / len(rows)


def cmd_calibrate(a):
    S, nifty, sigs, dates, cut = load_context(a.bars, a.signals)
    hold = [s for s in sigs if s["date"] >= cut]
    if a.setup:
        hold = [s for s in hold if s["setup"] == a.setup]
    step = max(1, len(hold) // (a.sample * a.oversample))
    sample = hold[::step][:a.sample * a.oversample]
    tm_kwargs = json.loads(a.tm_kwargs) if a.tm_kwargs else {}
    with multiprocessing.Pool(a.workers, _calib_init,
                              (a.bars, a.signals, a.stop, a.target_r, a.replays, tm_kwargs)) as pool:
        rows = [r for r in pool.map(_calib_job, sample) if r]
    errs = [r for r in rows if "error" in r]
    rows = [r for r in rows if "error" not in r]
    print("calibration on %d holdout signals (%d errors%s), rule %s %.1fR"
          % (len(rows), len(errs), (": " + errs[0]["error"]) if errs else "", a.stop, a.target_r))
    if not rows:
        return 1
    act = {o: 100.0 * sum(1 for r in rows if r["outcome"] == o) / len(rows) for o in ("target", "stop", "close")}
    print("actual:           target %5.1f%%  stop %5.1f%%  neither %5.1f%%  avg R %+.3f"
          % (act["target"], act["stop"], act["close"], statistics.mean(r["R"] for r in rows)))
    for r in rows:
        r["p_mc"] = (r["mc"]["p_target"], r["mc"]["p_stop"], r["mc"]["p_neither"])
        pr = r.get("pred") or {}
        r["p_tm"] = (pr.get("p_target", r["p_mc"][0]), pr.get("p_stop", r["p_mc"][1]), pr.get("p_neither", r["p_mc"][2]))
    for key, label in (("p_mc", "Old TM (MC)"), ("p_tm", "New TM")):
        m = [statistics.mean(r[key][i] for r in rows) * 100 for i in range(3)]
        print("%-16s target %5.1f%%  stop %5.1f%%  neither %5.1f%%  Brier %.4f"
              % (label + ":", m[0], m[1], m[2], brier(rows, key)))
    # reliability of p_stop
    for key in ("p_mc", "p_tm"):
        print("reliability (%s, stop):" % key)
        for lo in (0.0, 0.3, 0.45, 0.6, 0.75):
            hi = lo + (0.3 if lo == 0 else 0.15) if lo < 0.75 else 1.01
            b = [r for r in rows if lo <= r[key][1] < hi]
            if b:
                print("   predicted %.2f-%.2f: n=%4d actual stop %.0f%%" % (
                    lo, hi, len(b), 100.0 * sum(1 for r in b if r["outcome"] == "stop") / len(b)))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(rows, fh)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--universe", default=UNIVERSE)
    f.add_argument("--range", default="60d")
    f.add_argument("--out", default=BARS)
    s = sub.add_parser("signals")
    s.add_argument("--bars", default=BARS)
    s.add_argument("--out", default=SIGNALS)
    s.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    s.add_argument("--limit", type=int, default=0)
    r = sub.add_parser("report")
    r.add_argument("--bars", default=BARS)
    r.add_argument("--signals", default=SIGNALS)
    r.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    r.add_argument("--json", default=None)
    c = sub.add_parser("calibrate")
    c.add_argument("--bars", default=BARS)
    c.add_argument("--signals", default=SIGNALS)
    c.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    c.add_argument("--stop", default="atr1.5")
    c.add_argument("--target-r", type=float, default=3.0)
    c.add_argument("--sample", type=int, default=400)
    c.add_argument("--oversample", type=int, default=1, help="draw sample x N signals before the budget gate")
    c.add_argument("--replays", type=int, default=2000)
    c.add_argument("--setup", default=None)
    c.add_argument("--tm-kwargs", default=None, help=argparse.SUPPRESS)
    c.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    return {"fetch": cmd_fetch, "signals": cmd_signals, "report": cmd_report, "calibrate": cmd_calibrate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
