#!/usr/bin/env python3
"""Positional defined-risk credit spreads (Bull Put / Bear Call) held to expiry.

Decision support only -- never routes an order. Builds ONE spread from a live option chain for a
rule that passed scripts/backtest_spreads.py (a model-based backtest), and validates it fail-closed:
missing bid/ask/delta, a non-positive credit, a hedge on the wrong side, a too-wide market, or a max
loss over the spread budget are all rejections, never guesses. Naked option selling is not built
here and stays prohibited (options.py NAKED_OPTION_PROHIBITED); every spread has its long hedge.
"""

import datetime
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-budget", "config"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

MAX_LEG_SPREAD_PCT = 12.0      # a leg whose bid-ask is wider than 12 % of its mid is too illiquid
STEPS = {"NIFTY": 50, "BANKNIFTY": 100}

# Rules that passed the backtest under BOTH time bases (2026-11-19, bt_cache/spreads_report*.json).
RULES = (
    {"name": "NIFTY monthly Bear Call", "symbol": "NIFTY", "expiry": "monthly", "days_before": 5,
     "side": "CE", "delta": 0.20, "width_steps": 2},
    {"name": "BANKNIFTY monthly trend spread", "symbol": "BANKNIFTY", "expiry": "monthly", "days_before": 10,
     "side": "trend", "delta": 0.20, "width_steps": 2},
)


class SpreadError(Exception):
    pass


# ---------------------------------------------------------------- calendar

def trading_days_until(today, expiry):
    """Trading sessions after `today` up to and including `expiry` (0 on expiry day)."""
    import calendar_in
    n, d = 0, today
    while d < expiry:
        d += datetime.timedelta(days=1)
        ok, _, _ = calendar_in.is_trading_day_in(d, exchange="NSE")
        if ok:
            n += 1
    return n


def next_monthly_expiry(today, symbol):
    import calendar_in
    y, m = today.year, today.month
    for _ in range(3):
        e = calendar_in.get_monthly_expiry(y, m, underlying=symbol)
        if e >= today:
            return e
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise SpreadError("no monthly expiry found after %s" % today)


def entry_expiry(today, rule):
    """The expiry this rule would trade from `today`: this month's if its entry day hasn't passed,
    otherwise next month's. Returns (expiry, entry_date)."""
    import calendar_in
    e = next_monthly_expiry(today, rule["symbol"])
    for _ in range(3):
        d, n = e, 0
        while n < rule["days_before"]:
            d -= datetime.timedelta(days=1)
            ok, _, _ = calendar_in.is_trading_day_in(d, exchange="NSE")
            if ok:
                n += 1
        if d >= today:
            return e, d
        e = next_monthly_expiry(e + datetime.timedelta(days=1), rule["symbol"])
    raise SpreadError("could not place the entry day for %s" % rule["name"])


# ---------------------------------------------------------------- spread building

def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _leg(strike_row, side):
    c = (strike_row or {}).get(side.lower()) or {}
    return {"symbol": c.get("symbol"), "instrument_key": c.get("instrument_key"), "bid": _num(c.get("bid")),
            "ask": _num(c.get("ask")), "ltp": _num(c.get("last_price")),
            "delta": _num((c.get("greeks") or {}).get("delta")), "iv": _num(c.get("iv")),
            "oi": _num(c.get("open_interest"))}


def pick_strikes(chain, side, delta_target, width_steps, step):
    """(short_strike, long_strike) from a chain dict: the out-of-the-money strike whose |delta| is
    closest to delta_target, hedged width_steps strikes further out. None if the chain can't say."""
    spot = _num(chain.get("underlying_price"))
    rows = {float(s["strike"]): s for s in chain.get("strikes") or [] if s.get("strike") is not None}
    if not spot or not rows:
        return None
    best = None
    for k, row in rows.items():
        if (side == "CE" and k <= spot) or (side == "PE" and k >= spot):
            continue
        d = _leg(row, side)["delta"]
        if d is None:
            continue
        gap = abs(abs(d) - delta_target)
        if best is None or gap < best[1]:
            best = (k, gap)
    if best is None:
        return None
    short_k = best[0]
    long_k = short_k + width_steps * step if side == "CE" else short_k - width_steps * step
    if long_k not in rows:
        return None
    return short_k, long_k


def build_spread(chain, side, delta_target, width_steps, lot, step, max_loss_budget, as_of=None):
    """A validated credit spread dict, or raise SpreadError (fail closed) with every reason."""
    import size
    if side not in ("CE", "PE"):
        raise SpreadError("side must be CE (Bear Call) or PE (Bull Put)")
    pk = pick_strikes(chain, side, delta_target, width_steps, step)
    if pk is None:
        raise SpreadError("chain has no usable out-of-the-money strike / hedge (missing deltas or strikes)")
    short_k, long_k = pk
    rows = {float(s["strike"]): s for s in chain["strikes"]}
    s_leg, l_leg = _leg(rows[short_k], side), _leg(rows[long_k], side)
    reasons = []
    if (side == "CE" and not long_k > short_k) or (side == "PE" and not long_k < short_k):
        reasons.append("HEDGE_WRONG_SIDE")
    for name, leg in (("short", s_leg), ("long", l_leg)):
        if leg["bid"] is None or leg["ask"] is None or leg["bid"] <= 0 or leg["ask"] <= 0:
            reasons.append("NO_QUOTE: %s leg %s has no live bid/ask" % (name, leg["symbol"]))
            continue
        mid = (leg["bid"] + leg["ask"]) / 2
        if (leg["ask"] - leg["bid"]) / mid * 100 > MAX_LEG_SPREAD_PCT:
            reasons.append("ILLIQUID: %s leg bid-ask %.1f%% of mid" % (name, (leg["ask"] - leg["bid"]) / mid * 100))
    if reasons:
        raise SpreadError("; ".join(reasons))
    credit = s_leg["bid"] - l_leg["ask"]                 # sell the short at the bid, buy the hedge at the ask
    width = abs(long_k - short_k)
    if credit <= 0:
        raise SpreadError("NO_CREDIT: short bid %.2f - hedge ask %.2f <= 0" % (s_leg["bid"], l_leg["ask"]))
    max_loss = (width - credit) * lot
    chg = 0.0
    for sell, buy in ((s_leg["bid"], 0.05), (l_leg["bid"], l_leg["ask"])):   # open + worst-case close
        c = size.calculate_indian_transaction_charges("OPTIONS", "buy", buy, sell, lot, "intraday",
                                                       (as_of or datetime.date.today()).isoformat())
        chg += c.get("total") if isinstance(c, dict) else float(c or 0)
    max_loss_net = max_loss + chg
    if max_loss_net > max_loss_budget:
        raise SpreadError("RISK_EXCEEDS_BUDGET: max loss Rs %.0f incl. charges > spread budget Rs %.0f"
                          % (max_loss_net, max_loss_budget))
    be = short_k + credit if side == "CE" else short_k - credit
    return {"strategy": "BEAR_CALL_SPREAD" if side == "CE" else "BULL_PUT_SPREAD", "side": side,
            "underlying_price": _num(chain.get("underlying_price")), "expiry": chain.get("expiry"),
            "short": dict(s_leg, strike=short_k, action="SELL"), "long": dict(l_leg, strike=long_k, action="BUY"),
            "credit": round(credit, 2), "width": width, "lot": lot,
            "max_profit_inr": round(credit * lot - chg, 0), "max_loss_inr": round(max_loss_net, 0),
            "charges_inr": round(chg, 0), "breakeven": round(be, 2),
            "margin_estimate_inr": round(max_loss * 1.10, 0), "margin_note": "HEURISTIC_MARGIN_ESTIMATE (max loss x 1.10)",
            "short_delta": s_leg["delta"]}


def settle_value(spot, short_k, long_k, side):
    """Spread value at expiry (what it costs to close): intrinsic, between 0 and the width."""
    if side == "CE":
        v = max(0.0, spot - short_k) - max(0.0, spot - long_k)
    else:
        v = max(0.0, short_k - spot) - max(0.0, long_k - spot)
    return max(0.0, min(v, abs(long_k - short_k)))


def trend_side(closes):
    """'PE' (Bull Put) when the last close is above its 20-day average, else 'CE' (Bear Call)."""
    if len(closes) < 20:
        raise SpreadError("need 20 daily closes for the trend side")
    return "PE" if closes[-1] > sum(closes[-20:]) / 20.0 else "CE"
