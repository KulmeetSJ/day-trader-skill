#!/usr/bin/env python3
"""Day Trader MCP server -- lets an AI (Claude, or any MCP client) research and PAPER-trade.

Stdlib-only MCP over stdio (newline-delimited JSON-RPC 2.0, protocol 2024-11-05 compatible).
Register it with Claude Code via the repo's .mcp.json, or run:  python3 trader-mcp/server.py

Division of labour (CLAUDE.md "deterministic engine vs AI orchestration"):
  * the AI reads data, signals, news and results, decides WHETHER and WHAT to trade, and writes why;
  * the Python engine sizes, risk-checks (trader-risk must PASS), fills on paper and exits on rules.
The AI cannot change a limit, a stop after entry, or a BLOCK. PAPER ONLY -- nothing is routed to a
broker (Indian trading is paper-only by schema; Upstox is read-only).
"""

import csv
import json
import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import book as book_mod  # noqa: E402

PROTOCOL = "2024-11-05"
STATE = os.path.join(BASE_DIR, "trader", "state")

INSTRUCTIONS = (
    "Paper trading desk for Indian equities and index options (NSE). You are the trader: research with the "
    "read tools, then trade with preview_* -> place_trade. Two separate pools of Rs 1,00,000: 'trading' "
    "(intraday, flat by 15:20) and 'investing' (multi-day); during this testing stage the pool cap is reported "
    "but does not refuse trades (each trade is sized as if its pool were empty). Every trade is sized by the budget engine and must "
    "PASS trader-risk (Rs 2,000 max loss per trade, Rs 5,000 risk at stops per day per pool, 3 entries/day, "
    "net reward:risk >= 2 after charges, long only, no entries after 15:00). You cannot override a BLOCK -- "
    "pick another trade or do nothing. Doing nothing is a valid, often correct, decision. Exits at stop, "
    "target, 15:20 flat-by and the time stop are automatic. Always give a specific rationale; it is scored "
    "against the outcome.")


def _rows(path, n=None, where=None):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if where is None or where(r)]
    return rows[-n:] if n else rows


def _book():
    return book_mod.Book()


# ---------------------------------------------------------------- tools

def t_market_status(a):
    p = _book().provider
    return p.get_market_status(exchange="NSE")


def t_get_quote(a):
    q = _book().provider.get_quote(a["symbol"].upper())
    keep = ("symbol", "price", "prev_close", "open", "high", "low", "volume", "average_price", "bid", "ask",
            "lower_circuit", "upper_circuit", "data_status", "ist_time", "provider")
    return {k: q.get(k) for k in keep if k in q}


def t_get_bars(a):
    p = _book().provider
    interval = a.get("interval") or "5m"
    if interval == "1d":
        d = p.get_historical_data(a["symbol"].upper(), range_str=a.get("range") or "6mo", interval="1d")
    else:
        d = p.get_intraday_data(a["symbol"].upper(), interval=interval, range_str=a.get("range") or "1d")
    bars = d.get("bars") or []
    n = int(a.get("limit") or 120)
    return {"symbol": a["symbol"].upper(), "interval": interval, "data_status": d.get("data_status"),
            "bars": [{k: b.get(k) for k in ("date", "start", "o", "h", "l", "c", "v")} for b in bars[-n:]]}


def t_get_signals(a):
    """Today's machine calls: what the fixed-rule watchers saw. Input for the AI, not orders."""
    today = (a.get("date") or book_mod.now_ist().strftime("%Y-%m-%d"))
    out = {"date": today}
    out["intraday_alerts"] = _rows(os.path.join(STATE, "opportunities.csv"), where=lambda r: r.get("date") == today)
    rej = _rows(os.path.join(STATE, "rejected.csv"), where=lambda r: r.get("date") == today)
    out["intraday_refused_count"] = len(rej)
    out["intraday_refused_latest"] = rej[-int(a.get("refused_limit") or 15):]
    calls = _rows(os.path.join(STATE, "equity_calls.csv"))
    last = max((r["scan_date"] for r in calls), default=None)
    out["multi_day_ideas"] = [{k: r.get(k) for k in ("scan_date", "symbol", "setup", "label", "close", "stop", "target",
                                                     "qty", "net_rr", "p_target", "p_stop", "edge_vs_random", "news",
                                                     "results")}
                              for r in calls if r.get("scan_date") == last][:int(a.get("ideas_limit") or 25)]
    out["option_spread_calls"] = _rows(os.path.join(STATE, "options_calls.csv"), n=10)
    out["note"] = ("Rule-based signals. Backtests found no setup with a proven edge after charges -- treat each "
                   "as a lead to analyse, not a reason to trade.")
    return out


def t_get_news(a):
    sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
    import equity_calls as ec
    sym = a["symbol"].upper()
    names = ec.company_names()
    return {"symbol": sym, "news": ec.news_lines(sym, days=int(a.get("days") or 14), n=int(a.get("limit") or 6),
                                                 name=names.get(sym)),
            "results": ec.results_line(sym)}


def t_portfolio(a):
    return _book().summary()


def t_journal(a):
    return _book().journal_tail(int(a.get("limit") or 20))


def t_preview_equity(a):
    return _book().preview_equity(a["pool"], a["symbol"], a["stop"], a["target"], entry=a.get("entry"),
                                  setup=a.get("setup") or "ai_discretionary")


def t_preview_options(a):
    return _book().preview_options(a["underlying"], budget=a.get("max_loss_inr"))


def t_place(a):
    return _book().place(a["preview_id"], a.get("rationale"))


def t_close(a):
    return _book().close(a["position_id"], a.get("reason"))


def t_mark(a):
    return _book().mark()


def _s(props, req=()):
    return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}


S, N = {"type": "string"}, {"type": "number"}
TOOLS = [
    ("market_status", "Is NSE open now? Session, holiday and time in IST.", _s({}), t_market_status),
    ("get_quote", "Latest quote for an NSE stock or index (price, day range, circuits, data_status).",
     _s({"symbol": S}, ["symbol"]), t_get_quote),
    ("get_bars", "OHLCV bars. interval 1m/5m/15m/30m/1h (range e.g. '1d','5d') or 1d (range e.g. '6mo','1y').",
     _s({"symbol": S, "interval": S, "range": S, "limit": N}, ["symbol"]), t_get_bars),
    ("get_signals", "Today's rule-based leads: intraday alerts and refusals, the latest multi-day equity ideas, "
     "option spread calls. Leads to analyse, not orders.",
     _s({"date": S, "refused_limit": N, "ideas_limit": N}), t_get_signals),
    ("get_news", "Recent company news headlines and the latest quarterly results for one NSE stock.",
     _s({"symbol": S, "days": N, "limit": N}, ["symbol"]), t_get_news),
    ("portfolio", "The AI paper account: both pools' cap, committed and free capital, open positions, "
     "realized P&L, today's entries and risk used, and the hard limits.", _s({}), t_portfolio),
    ("journal", "Recent entries/exits with the rationale given and the outcome.", _s({"limit": N}), t_journal),
    ("preview_equity_trade", "Size and risk-check a LONG equity trade. pool 'trading' = intraday (flat by 15:20), "
     "'investing' = multi-day. Give stop and target (entry defaults to the live price). Returns qty, capital, "
     "max loss, net R:R after charges and the trader-risk verdict; PASS includes a preview_id valid 10 minutes.",
     _s({"pool": {"type": "string", "enum": ["trading", "investing"]}, "symbol": S, "stop": N, "target": N,
         "entry": N, "setup": S}, ["pool", "symbol", "stop", "target"]), t_preview_equity),
    ("preview_option_trades", "Run the options specialist (Greeks, premium stop/target, charges, sizing) on an "
     "index or F&O stock (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX...). Only validated BUY_CE/BUY_PE "
     "signals get a preview_id; the rest show why they were rejected. Takes up to ~2 minutes.",
     _s({"underlying": S, "max_loss_inr": N}, ["underlying"]), t_preview_options),
    ("place_trade", "Paper-execute a PASS preview at the live price (+1 tick slippage). rationale is required: "
     "the specific reason (setup, context, news, why now).",
     _s({"preview_id": S, "rationale": S}, ["preview_id", "rationale"]), t_place),
    ("close_position", "Exit an open position now at the live price (-1 tick). Stops/targets/flat-by happen "
     "automatically; use this only for a reasoned early exit.",
     _s({"position_id": S, "reason": S}, ["position_id", "reason"]), t_close),
    ("mark_positions", "Mark open positions to market and apply the automatic exits (stop, target, 15:20 "
     "flat-by, time stop).", _s({}), t_mark),
]
BY_NAME = {t[0]: t for t in TOOLS}


# ---------------------------------------------------------------- JSON-RPC

def handle(msg):
    """One request -> one response dict (None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None                                   # notification (e.g. notifications/initialized)
    if method == "initialize":
        return _ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "day-trader", "version": "0.1.0"},
                         "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        t = BY_NAME.get(name)
        if t is None:
            return _err(mid, -32602, "unknown tool %r" % name)
        try:
            res = t[3](args)
            return _ok(mid, {"content": [{"type": "text", "text": json.dumps(res, default=str, indent=1)}],
                             "isError": False})
        except book_mod.BookError as e:              # a refusal: tell the AI plainly, it is not a crash
            return _ok(mid, {"content": [{"type": "text", "text": "REFUSED: %s" % e}], "isError": True})
        except KeyError as e:
            return _ok(mid, {"content": [{"type": "text", "text": "missing argument %s" % e}], "isError": True})
        except Exception as e:
            sys.stderr.write(traceback.format_exc())
            return _ok(mid, {"content": [{"type": "text", "text": "ERROR %s: %s" % (type(e).__name__, e)}],
                             "isError": True})
    return _err(mid, -32601, "method not found: %s" % method)


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main():
    os.chdir(BASE_DIR)
    import upstox_speed                 # scripts/ is on sys.path via book.py
    upstox_speed.apply("ai_mcp")       # paced Upstox calls + same-day cache (429s on 2026-11-20)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = _err(None, -32700, "parse error")
        else:
            out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, default=str) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
