#!/usr/bin/env python3
"""Total-capital hard caps for the paper utilities -- two SEPARATE pools (user rule, 2026-11-19):

  * TRADING   Rs 1,00,000: intraday calls (market_watch: equity + option buys) and option credit
              spreads (options_watch).
  * INVESTING Rs 1,00,000: multi-day equity holds (equity_calls ledger).
The AI paper account (trader-mcp/book.py) keeps its OWN two Rs 1,00,000 pools from its own positions;
these rule-based ledgers run beside it as benchmarks and never share or block its capital.

Each pool's cap is the config's `budget` (IN: Rs 1,00,000) -- the TOTAL committed at once in that pool,
not per trade. A tool asks `committed(pool, ...)` what is already tied up and only takes a new position
while committed + new <= cap. Profits are not reinvested yet (raise `budget` when that starts).

What counts as committed (deliberately conservative -- fail closed):
  * every call the intraday watcher alerted TODAY (equity: qty x entry; option buy: qty x premium),
    for the whole day, even after it hit its target or stop (we don't know it was actually exited);
  * every PENDING/OPEN row in the equity paper ledger: qty x entry (or x entry_ref before the fill);
  * every OPEN credit spread in the options paper ledger: its max_loss_inr (defined-risk spread;
    the broker's blocked margin is about the worst-case loss).
A row whose capital can't be read counts as the whole cap (nothing more fits until it's fixed).

It adds no sizing logic: the remaining room is handed to trader-budget's size_trade() as `cash`,
so the budget engine (cash buffer, position cap, max order) decides the quantity.
"""

import csv
import datetime
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(BASE_DIR, "trader", "state")
OPPORTUNITIES_CSV = os.path.join(STATE, "opportunities.csv")
EQUITY_LEDGER_CSV = os.path.join(STATE, "equity_ledger.csv")
OPTIONS_LEDGER_CSV = os.path.join(STATE, "options_ledger.csv")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


POOLS = ("trading", "investing")


def cap_inr(cfg, pool="trading"):
    """A pool's hard cap: the config's budget (same for both pools). Missing/invalid -> ValueError."""
    if pool not in POOLS:
        raise ValueError("unknown capital pool %r (want one of %s)" % (pool, ", ".join(POOLS)))
    v = float((cfg or {}).get("budget") or 0)
    if v <= 0:
        raise ValueError("config has no positive 'budget' for the capital cap")
    return v


def _rows(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def committed(pool="trading", cap=100000.0, today=None, opportunities=OPPORTUNITIES_CSV,
              equity_ledger=EQUITY_LEDGER_CSV, options_ledger=OPTIONS_LEDGER_CSV):
    """(total_inr, [(source, symbol, inr)]) of capital tied up right now in `pool`.
    trading: today's intraday calls + open option spreads.  investing: the equity ledger.
    Pass opportunities=None to leave out today's intraday calls (a position held past today's 15:20
    square-off doesn't share capital with them)."""
    if pool not in POOLS:
        raise ValueError("unknown capital pool %r" % pool)
    today = today or datetime.datetime.now(IST).strftime("%Y-%m-%d")
    parts = []
    if pool == "trading":
        for r in _rows(opportunities):
            if r.get("date") == today and r.get("kind") in ("equity", "options"):
                q, e = _num(r.get("qty")), _num(r.get("entry"))
                parts.append(("intraday_" + r["kind"], r.get("symbol"), q * e if None not in (q, e) else cap))
        for r in _rows(options_ledger):
            if r.get("status") == "OPEN":
                m = _num(r.get("max_loss_inr"))
                parts.append(("options_ledger", r.get("symbol"), abs(m) if m is not None else cap))
    else:
        for r in _rows(equity_ledger):
            if r.get("status") in ("PENDING", "OPEN"):
                q, e = _num(r.get("qty")), _num(r.get("entry")) or _num(r.get("entry_ref"))
                parts.append(("equity_ledger", r.get("symbol"), q * e if None not in (q, e) else cap))
    return sum(x[2] for x in parts), parts


def room(pool, cap, **kw):
    """Capital still free in `pool` under its cap (never negative)."""
    return max(0.0, cap - committed(pool, cap, **kw)[0])
