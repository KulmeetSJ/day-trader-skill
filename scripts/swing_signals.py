#!/usr/bin/env python3
"""Daily-chart swing signals: the scanner's daily setups, classic chart formations, relative strength.

Research module for scripts/backtest_swing.py (and a future end-of-day scan). Long side only;
it detects, it never sizes or places anything. Stdlib only.

Point-in-time rule: a signal on day t uses bars 0..t only (day t's close is known, nothing after).
Swing highs/lows need W bars on each side to be confirmed, so a swing at day k is used only
once k <= t - W. tests/test_swing_signals.py checks that appending future bars never changes a
past day's signals.

BREAKOUT (shared by the formations): day t closes above the pattern's pivot while day t-1 had
not, on volume >= 1.5 x the average of the 50 days before t.

Formations (each docstring below is the exact rule):
  high52_breakout, flat_base, ascending_triangle, cup_handle, double_bottom, vcp, bull_flag_v2
Relative strength:
  rs_leader
Multi-day families (2026-11-19, for 20-60 day holds):
  results_gap      -- gap-up >= 4 % on >= 3x volume closing in the top third (how a results/news
                      surprise shows up in price; historical results dates aren't available)
  pullback_uptrend -- RSI(2) < 10 while close > 200-day average and 50-day > 200-day
Existing scanner daily setups (trader-scanner/scan.py find_setups, closes only):
  breakout_20d, bull_flag, reclaim_ma20, oversold, oversold_bounce
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_p = os.path.join(BASE_DIR, "trader-scanner")
if _p not in sys.path:
    sys.path.insert(0, _p)

VOL_MULT = 1.5
SWING_W = 3            # swing high/low = extreme of the 3 bars each side (strict on the left)
WARMUP = 260           # ~1 year: the 52-week and 200-day rules need it
COOLDOWN = 10          # the same setup on the same stock is reported again only after 10 bars
SCANNER_SETUPS = ("breakout_20d", "bull_flag", "reclaim_ma20", "oversold", "oversold_bounce")


# ---------------------------------------------------------------- precompute

class _Sparse(object):
    """O(1) range max/min with index (sparse table)."""

    def __init__(self, arr, cmp):
        self.cmp = cmp
        n = len(arr)
        self.arr = arr
        self.t = [list(range(n))]
        j = 1
        while (1 << j) <= n:
            prev, half = self.t[-1], 1 << (j - 1)
            row = []
            for i in range(n - (1 << j) + 1):
                a, b = prev[i], prev[i + half]
                row.append(a if cmp(arr[a], arr[b]) else b)
            self.t.append(row)
            j += 1

    def idx(self, lo, hi):
        """Index of the extreme of arr[lo..hi] inclusive (lo <= hi)."""
        k = (hi - lo + 1).bit_length() - 1
        a, b = self.t[k][lo], self.t[k][hi - (1 << k) + 1]
        return a if self.cmp(self.arr[a], self.arr[b]) else b

    def val(self, lo, hi):
        return self.arr[self.idx(lo, hi)]


def _sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def prep(bars):
    """bars: [{date,o,h,l,c,v}] oldest first -> arrays used by every detector."""
    A = {k: [float(b[k] or 0) for b in bars] for k in ("o", "h", "l", "c", "v")}
    A["date"] = [b["date"] for b in bars]
    n = len(bars)
    A["n"] = n
    A["sma50"], A["sma200"] = _sma(A["c"], 50), _sma(A["c"], 200)
    v50 = _sma(A["v"], 50)
    A["avgv50_prior"] = [None] + v50[:-1]          # average of the 50 days BEFORE t
    A["hmax"] = _Sparse(A["h"], lambda a, b: a >= b)
    A["lmin"] = _Sparse(A["l"], lambda a, b: a <= b)
    W = SWING_W
    # strictly above the W bars before, at least as high as the W bars after (a flat top counts once)
    A["swing_hi"] = [W <= k < n - W and A["h"][k] > max(A["h"][k - W:k]) and A["h"][k] >= max(A["h"][k + 1:k + W + 1])
                     for k in range(n)]
    A["swing_lo"] = [W <= k < n - W and A["l"][k] < min(A["l"][k - W:k]) and A["l"][k] <= min(A["l"][k + 1:k + W + 1])
                     for k in range(n)]
    trs, prev = [], None
    for k in range(n):
        tr = A["h"][k] - A["l"][k]
        if prev is not None:
            tr = max(tr, abs(A["h"][k] - prev), abs(A["l"][k] - prev))
        trs.append(tr)
        prev = A["c"][k]
    A["atr14"] = _sma(trs, 14)
    A["rsi2"] = _rsi(A["c"], 2)
    return A


def _rsi(closes, n):
    """Wilder RSI(n); None until n changes are known."""
    out = [None] * len(closes)
    up = dn = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            up += g / n
            dn += l / n
            if i < n:
                continue
        else:
            up = (up * (n - 1) + g) / n
            dn = (dn * (n - 1) + l) / n
        out[i] = 100.0 if dn == 0 else 100.0 - 100.0 / (1.0 + up / dn)
    return out


def _swings(A, key, lo, t):
    """Confirmed swing points in [lo, t - W] (a swing needs W later bars to be known)."""
    return [k for k in range(max(lo, SWING_W), t - SWING_W + 1) if A[key][k]]


def volume_ok(A, t):
    av = A["avgv50_prior"][t]
    return bool(av) and A["v"][t] >= VOL_MULT * av


def breakout(A, t, pivot):
    return pivot is not None and A["c"][t] > pivot >= A["c"][t - 1] and volume_ok(A, t)


def low10(A, t):
    return A["lmin"].val(max(0, t - 9), t)


def _r(x):
    return round(x, 2)


# ---------------------------------------------------------------- formations

def high52_breakout(A, t):
    """Close above the highest high of the prior 252 sessions (52 weeks), on volume.
    Stop: lowest low of the last 10 sessions."""
    if t < 253:
        return None
    pivot = A["hmax"].val(t - 252, t - 1)
    if not breakout(A, t, pivot):
        return None
    return {"pivot": _r(pivot), "stop": _r(low10(A, t))}


def flat_base(A, t, length=25, max_depth=0.15):
    """Flat base: the 25 sessions before t (5 weeks) trade within 15 % (highest high to lowest
    low), and price was above its 200-day average when the base started. Breakout above the base
    high on volume. Stop: the base low."""
    if t < length + 200:
        return None
    lo, hi_i = t - length, t - 1
    hi, low = A["hmax"].val(lo, hi_i), A["lmin"].val(lo, hi_i)
    if hi <= 0 or (hi - low) / hi >= max_depth:
        return None
    if A["sma200"][lo] is None or A["c"][lo] < A["sma200"][lo]:
        return None
    if not breakout(A, t, hi):
        return None
    return {"pivot": _r(hi), "stop": _r(low), "depth_pct": _r(100 * (hi - low) / hi)}


def ascending_triangle(A, t, window=40, tol=0.015):
    """Ascending triangle over the last 40 sessions: >= 2 confirmed swing highs within 1.5 % of the
    window's highest high (flat resistance, >= 5 bars apart), and >= 2 swing lows each higher than
    the one before (rising support). Breakout above the resistance on volume. Stop: last swing low."""
    if t < window + SWING_W + 1:
        return None
    lo = t - window
    res = A["hmax"].val(lo, t - 1)
    highs = [k for k in _swings(A, "swing_hi", lo, t - 1) if A["h"][k] >= res * (1 - tol)]
    if len(highs) < 2 or highs[-1] - highs[0] < 5:
        return None
    lows = _swings(A, "swing_lo", highs[0], t - 1)
    if len(lows) < 2:
        return None
    lv = [A["l"][k] for k in lows]
    if any(b <= a for a, b in zip(lv, lv[1:])):
        return None
    if not breakout(A, t, res):
        return None
    return {"pivot": _r(res), "stop": _r(lv[-1]), "touches": len(highs)}


def cup_handle(A, t):
    """Cup with handle (O'Neil, simplified):
      handle = the last H sessions before t (H in 5..20): depth (high - low) / high <= 12 %;
      cup    = from the left rim (highest high 35..325 sessions before the handle) to the handle
               start; depth from the left rim to the cup low 12-35 %; the right rim (highest high
               of the 10 sessions before the handle) within 5 % of the left rim;
               the cup low sits in the middle 80 % of the cup (not a straight slide);
      handle low stays in the upper half of the cup.
    Breakout above the handle high on volume. Stop: the handle low."""
    for H in (5, 10, 15, 20):
        s = t - H
        if s - 325 < 0:
            return None
        hh, hl = A["hmax"].val(s, t - 1), A["lmin"].val(s, t - 1)
        if hh <= 0 or (hh - hl) / hh > 0.12:
            continue
        if not breakout(A, t, hh):
            continue
        Li = A["hmax"].idx(s - 325, s - 35)
        lh = A["h"][Li]
        bi = A["lmin"].idx(Li, s - 1)
        bottom = A["l"][bi]
        depth = (lh - bottom) / lh if lh else 0
        if not 0.12 <= depth <= 0.35:
            continue
        span = s - Li
        if not (Li + 0.1 * span <= bi <= Li + 0.9 * span):
            continue
        rr = A["hmax"].val(max(Li + 1, s - 10), s - 1)
        if abs(rr - lh) / lh > 0.05:
            continue
        if hl <= bottom + 0.5 * (lh - bottom):
            continue
        return {"pivot": _r(hh), "stop": _r(hl), "cup_depth_pct": _r(100 * depth), "cup_bars": span,
                "handle_bars": H, "left_rim_date": A["date"][Li], "cup_low_date": A["date"][bi]}
    return None


def double_bottom(A, t, window=120):
    """Double bottom ("W") in the last 120 sessions: the last two confirmed swing lows are within
    3 % of each other and >= 10 sessions apart, and the highest high between them is >= 8 % above
    the higher low. Breakout above that middle peak on volume. Stop: the lower of the two lows."""
    lows = _swings(A, "swing_lo", t - window, t - 1)
    if len(lows) < 2:
        return None
    a, b = lows[-2], lows[-1]
    la, lb = A["l"][a], A["l"][b]
    if b - a < 10 or abs(la - lb) / min(la, lb) > 0.03:
        return None
    peak = A["hmax"].val(a, b)
    if peak < max(la, lb) * 1.08:
        return None
    if A["hmax"].val(b, t - 1) > peak:   # already broke out earlier: not a fresh W
        return None
    if not breakout(A, t, peak):
        return None
    return {"pivot": _r(peak), "stop": _r(min(la, lb)), "lows": [A["date"][a], A["date"][b]]}


def vcp(A, t, window=60):
    """Volatility contraction (Minervini, simplified), in a stage-2 trend (close > 200-day average
    > its value 20 sessions ago): in the last 60 sessions, 2-4 pullbacks (swing high -> next swing
    low), each shallower than the one before, the last under 10 %; the 10 sessions before t trade
    on below-average volume (< 0.8 x 50-day). Breakout above the last swing high on volume.
    Stop: the last swing low."""
    if t < 220 or A["sma200"][t] is None or A["sma200"][t - 20] is None:
        return None
    if not (A["c"][t] > A["sma200"][t] > A["sma200"][t - 20]):
        return None
    lo = t - window
    highs = _swings(A, "swing_hi", lo, t - 1)
    lows = _swings(A, "swing_lo", lo, t - 1)
    legs = []
    for hk in highs:
        nxt = [lk for lk in lows if lk > hk]
        if nxt:
            lk = nxt[0]
            legs.append((hk, lk, (A["h"][hk] - A["l"][lk]) / A["h"][hk]))
    # keep the latest leg per swing low, in time order
    legs = sorted({lk: (hk, lk, d) for hk, lk, d in legs}.values(), key=lambda x: x[1])[-4:]
    if len(legs) < 2:
        return None
    depths = [d for _, _, d in legs]
    if any(b >= a for a, b in zip(depths, depths[1:])) or depths[-1] >= 0.10:
        return None
    av = A["avgv50_prior"][t]
    if not av or sum(A["v"][t - 10:t]) / 10.0 >= 0.8 * av:
        return None
    last_hi = max(A["h"][hk] for hk, _, _ in legs[-1:])
    pivot = max(last_hi, A["hmax"].val(legs[-1][1], t - 1))
    if not breakout(A, t, pivot):
        return None
    return {"pivot": _r(pivot), "stop": _r(A["l"][legs[-1][1]]),
            "contractions_pct": [_r(100 * d) for d in depths]}


def bull_flag_v2(A, t):
    """Bull flag with highs/lows and volume: a pole that rises >= 15 % within <= 15 sessions to its
    top F; then a flag of 5-15 sessions after F that never trades above F's high, retraces <= 50 %
    of the pole, on lower average volume than the pole. Breakout above the flag high on volume.
    Stop: the flag low."""
    for F in range(t - 6, t - 17, -1):
        if F - 15 < 0:
            return None
        top = A["h"][F]
        if A["hmax"].val(F, t - 1) > top:
            continue
        bi = A["lmin"].idx(F - 15, F - 1)
        base = A["l"][bi]
        if base <= 0 or top / base - 1 < 0.15:
            continue
        flo = A["lmin"].val(F + 1, t - 1)
        if (top - flo) / (top - base) > 0.5:
            continue
        pole_v = sum(A["v"][bi:F + 1]) / float(F + 1 - bi)
        flag_v = sum(A["v"][F + 1:t]) / float(t - F - 1)
        if flag_v >= pole_v:
            continue
        fhi = A["hmax"].val(F + 1, t - 1)
        if not breakout(A, t, fhi):
            continue
        return {"pivot": _r(fhi), "stop": _r(flo), "pole_pct": _r(100 * (top / base - 1)), "flag_bars": t - F - 1}
    return None


def results_gap(A, t, gap=0.04, vol_mult=3.0):
    """Results/news-surprise proxy: day t opens above day t-1's high, closes >= 4 % above day t-1's
    close and in the top third of its own range, on volume >= 3x the 50 days before.
    Stop: day t's low (a gap that fills is a failed surprise)."""
    if t < 51:
        return None
    o, h, l, c, pc = A["o"][t], A["h"][t], A["l"][t], A["c"][t], A["c"][t - 1]
    av = A["avgv50_prior"][t]
    if not av or A["v"][t] < vol_mult * av or pc <= 0:
        return None
    if o <= A["h"][t - 1] or c / pc - 1 < gap or h <= l or (c - l) / (h - l) < 2.0 / 3:
        return None
    return {"stop": _r(l), "gap_pct": _r(100 * (c / pc - 1)), "vol_x": _r(A["v"][t] / av)}


def pullback_uptrend(A, t):
    """Short pullback inside an uptrend: RSI(2) < 10 while close > 200-day average and the 50-day
    average > the 200-day. No pattern stop (the 3 ATR cap applies)."""
    r, s50, s200 = A["rsi2"][t], A["sma50"][t], A["sma200"][t]
    if r is None or s50 is None or s200 is None:
        return None
    if r < 10 and A["c"][t] > s200 and s50 > s200:
        return {"stop": None, "rsi2": _r(r)}
    return None


FORMATIONS = (("high52_breakout", high52_breakout), ("flat_base", flat_base),
              ("ascending_triangle", ascending_triangle), ("cup_handle", cup_handle),
              ("double_bottom", double_bottom), ("vcp", vcp), ("bull_flag_v2", bull_flag_v2))


# ---------------------------------------------------------------- relative strength

def ret126(bars):
    """{date: 126-session (~6 month) return} for one stock."""
    out = {}
    for i in range(126, len(bars)):
        p0 = bars[i - 126]["c"]
        if p0:
            out[bars[i]["date"]] = bars[i]["c"] / p0 - 1
    return out


def rs_ranks(data):
    """{sym: {date: percentile 0..1 of the 6-month return among all stocks that day}}."""
    rets = {s: ret126(b) for s, b in data.items() if s != "NIFTY"}
    by_date = {}
    for s, d in rets.items():
        for dt, r in d.items():
            by_date.setdefault(dt, []).append((r, s))
    out = {s: {} for s in rets}
    for dt, lst in by_date.items():
        if len(lst) < 20:
            continue
        lst.sort()
        n = len(lst)
        for i, (_r6, s) in enumerate(lst):
            out[s][dt] = i / float(n - 1)
    return out


def rs_leader_state(A, t, pct):
    """6-month return in the top 10 % of the universe that day, close > 50-day > 200-day average,
    and within 10 % of the 52-week high."""
    if pct is None or t < 252 or A["sma200"][t] is None:
        return False
    hi = A["hmax"].val(t - 251, t)
    return (pct >= 0.9 and A["c"][t] > A["sma50"][t] > A["sma200"][t] and A["c"][t] >= 0.9 * hi)


# ---------------------------------------------------------------- regime

def regime_series(nifty):
    """{date: NIFTY close > its 200-day average}."""
    c = [b["c"] for b in nifty]
    s = _sma(c, 200)
    return {b["date"]: (s[i] is not None and c[i] > s[i]) for i, b in enumerate(nifty)}


_REG = {}


def regime_up_on(nifty, date):
    key = id(nifty)
    if key not in _REG:
        _REG.clear()
        _REG[key] = regime_series(nifty)
    return _REG[key].get(date)


# ---------------------------------------------------------------- scanning

def signals_at(A, t, rs_pct=None, rs_pct_prev=None):
    """All signals on day t: [(setup, info)]. info has pivot/stop where the setup defines them."""
    out = []
    pre = A["c"][t] > A["c"][t - 1] and volume_ok(A, t)   # every formation needs an up close on volume
    if pre:
        for name, fn in FORMATIONS:
            r = fn(A, t)
            if r:
                out.append((name, r))
    for name, fn in (("results_gap", results_gap), ("pullback_uptrend", pullback_uptrend)):
        r = fn(A, t)
        if r:
            out.append((name, r))
    if rs_leader_state(A, t, rs_pct) and not rs_leader_state(A, t - 1, rs_pct_prev):
        out.append(("rs_leader", {"stop": _r(low10(A, t)), "rs_pct": _r(rs_pct)}))
    import scan
    closes = A["c"][max(0, t - 59):t + 1]
    if len(closes) >= 30:
        tags, _nums = scan.find_setups(closes)
        for tag in tags:
            if tag in SCANNER_SETUPS:
                out.append((tag, {"stop": _r(low10(A, t))}))
    return out


def scan_history(sym, bars, nifty=None, rs_rank=None, start=WARMUP):
    """Every signal for one stock over its history, with a per-setup cooldown (a setup that stays
    true for days is one signal, not ten)."""
    A = prep(bars)
    reg = regime_series(nifty) if nifty else {}
    ranks = (rs_rank or {}).get(sym) or {}
    last = {}
    rows = []
    for t in range(max(start, 1), A["n"]):
        d = A["date"][t]
        for setup, info in signals_at(A, t, ranks.get(d), ranks.get(A["date"][t - 1])):
            if t - last.get(setup, -10 ** 9) < COOLDOWN:
                continue
            last[setup] = t
            rows.append(dict(info, sym=sym, date=d, setup=setup, close=_r(A["c"][t]),
                             regime_up=reg.get(d)))
    return rows
