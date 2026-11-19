#!/usr/bin/env python3
"""AI Trader -- wakes Claude (headless, with the day-trader MCP server) when there is something to decide.

Paper only. The AI decides; trader-mcp/book.py sizes, risk-checks (trader-risk must PASS), fills on paper
and exits on rules.

    python3 scripts/ai_trader.py run                  # stays up on trading days (see below)
    python3 scripts/ai_trader.py trigger --reason "…" # one AI decision now (for any external API/webhook)
    python3 scripts/ai_trader.py mark                 # apply automatic exits now (no AI)
    python3 scripts/ai_trader.py show                 # the AI paper account

`run`, every 2 minutes during the NSE session (and once after the close):
  * marks the book: stop / target / 15:20 flat-by / time stop -- deterministic, no AI;
  * wakes the AI when there is something NEW: an intraday alert or a fresh batch of refused setups
    (market_watch), an option spread call (options_watch), a new multi-day scan (equity_calls), or a
    scheduled review (09:35, 11:00, 13:30, and 15:50 for the investing pool);
  * caps AI runs (MAX_RUNS_PER_DAY, MIN_GAP_MIN apart) so a busy tape can't run up the bill.

    nohup caffeinate -i python3 scripts/ai_trader.py run >> trader/state/ai_trader.log 2>&1 &
"""

import argparse
import csv
import datetime
import json
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "trader-mcp"))
sys.path.insert(0, os.path.join(BASE_DIR, "trader-alerts"))
import book as book_mod  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
STATE = os.path.join(BASE_DIR, "trader", "state")
MCP_CONFIG = os.path.join(BASE_DIR, ".mcp.json")
SEEN_PATH = os.path.join(STATE, "ai_trader_seen.json")
REVIEWS = (datetime.time(9, 35), datetime.time(11, 0), datetime.time(13, 30), datetime.time(15, 50))
OPEN, CLOSE = datetime.time(9, 15), datetime.time(15, 30)
POLL_SECONDS = 120
MAX_RUNS_PER_DAY = 16
MIN_GAP_MIN = 10
AI_TIMEOUT_S = 900
REFUSED_BATCH = 25          # wake the AI when this many NEW refused setups have piled up

PROMPT = """You are the trader for a PAPER account (Indian markets, NSE). Use only the day-trader MCP tools.
Why you were woken: {reason}

Do this:
1. portfolio -> see both pools (trading: intraday; investing: multi-day), free capital, risk used today, limits.
2. market_status. If the market is closed, only review the investing pool / plan for tomorrow; no intraday trades.
3. get_signals (and get_quote / get_bars / get_news on anything you consider). Rule-based signals are leads;
   backtests found no fixed rule with a proven edge after charges, so your judgement is the point:
   trend and market context (NIFTY), where the stop really belongs (structure, not a fixed %), whether the
   target is realistic for the time left, liquidity, news/results risk.
4. For the best idea(s) only: preview_equity_trade (or preview_option_trades for an index view). If PASS and
   you still like it, place_trade with a specific rationale. A BLOCK is final -- don't fiddle the stop/target
   just to squeeze past the gate; a stop must sit where the setup is proven wrong.
5. Doing nothing is often right. Prefer 0-2 high-conviction trades over many marginal ones.
6. Finish with 3-6 lines: what you did and why, or why you passed.
"""


def now_ist():
    return datetime.datetime.now(IST)


def emit(line):
    sys.stdout.write("%s %s\n" % (now_ist().strftime("%Y-%m-%d %H:%M:%S"), line))
    sys.stdout.flush()


def load_seen():
    try:
        with open(SEEN_PATH, encoding="utf-8") as fh:
            s = json.load(fh)
    except (OSError, ValueError):
        s = {}
    today = now_ist().strftime("%Y-%m-%d")
    if s.get("date") != today:
        s = {"date": today, "alerts": 0, "refused": 0, "spread_calls": 0, "scan_date": s.get("scan_date"),
             "reviews": [], "runs": 0, "last_run": None}
    return s


def save_seen(s):
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh)
    os.replace(tmp, SEEN_PATH)


def count_rows(name, where):
    path = os.path.join(STATE, name)
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8", newline="") as fh:
        return sum(1 for r in csv.DictReader(fh) if where(r))


def latest_scan_date():
    path = os.path.join(STATE, "equity_calls.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        return max((r.get("scan_date") or "" for r in csv.DictReader(fh)), default=None)


def pending_reasons(seen, now):
    """What's new since the AI last looked. Updates `seen` in place."""
    today = now.strftime("%Y-%m-%d")
    reasons = []
    n = count_rows("opportunities.csv", lambda r: r.get("date") == today)
    if n > seen["alerts"]:
        reasons.append("%d new intraday alert(s) from the market watcher" % (n - seen["alerts"]))
        seen["alerts"] = n
    n = count_rows("rejected.csv", lambda r: r.get("date") == today and r.get("kind") == "equity")
    if n - seen["refused"] >= REFUSED_BATCH:
        reasons.append("%d new confirmed setups the fixed rules refused (your call whether any deserve a "
                       "different stop/target)" % (n - seen["refused"]))
        seen["refused"] = n
    n = count_rows("options_calls.csv", lambda r: str(r.get("time", "")).startswith(today) and r.get("status") == "CALL")
    if n > seen["spread_calls"]:
        reasons.append("new option credit-spread call")
        seen["spread_calls"] = n
    sd = latest_scan_date()
    if sd and sd != seen.get("scan_date"):
        reasons.append("new multi-day equity scan for %s (investing pool)" % sd)
        seen["scan_date"] = sd
    for t in REVIEWS:
        key = t.strftime("%H:%M")
        if now.time() >= t and key not in seen["reviews"] and (now - now.replace(hour=t.hour, minute=t.minute)).seconds < 1800:
            reasons.append("scheduled %s review" % key)
            seen["reviews"].append(key)
    return reasons


def run_ai(reason, dry=False):
    """One headless Claude session with only the day-trader MCP tools. Returns its final text."""
    cmd = ["claude", "-p", PROMPT.format(reason=reason), "--mcp-config", MCP_CONFIG, "--strict-mcp-config",
           "--allowedTools", "mcp__day-trader", "--output-format", "text"]
    if dry:
        return "DRY RUN: " + " ".join(cmd[:2]) + " ..."
    out = subprocess.run(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, timeout=AI_TIMEOUT_S)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "claude exit %d" % out.returncode).strip()[-400:])
    return out.stdout.strip()


def is_trading_day(d):
    try:
        import calendar_in
        return bool(calendar_in.is_trading_day_in(d)[0])
    except Exception:
        return d.weekday() < 5


def mark(bk):
    try:
        res = bk.mark()
    except Exception as e:
        emit("MARK ERROR %s" % e)
        return
    for p in res["closed"]:
        emit("EXIT %s %s %s at %.2f -> net Rs %+.0f (%sR)" % (p["pool"], p["symbol"], p["exit_reason"], p["exit"],
                                                            p["net_inr"], p["R"]))
    for m in res["open"]:
        if m.get("error"):
            emit("MARK %s: %s" % (m["symbol"], m["error"]))


def cmd_run(a):
    bk = book_mod.Book()
    emit("AI TRADER start: paper only; marks every %ds in session; AI runs max %d/day, %d min apart. Ctrl-C to stop."
         % (POLL_SECONDS, MAX_RUNS_PER_DAY, MIN_GAP_MIN))
    while True:
        now = now_ist()
        if is_trading_day(now.date()) and OPEN <= now.time() <= datetime.time(16, 0):
            if now.time() <= datetime.time(15, 35):
                mark(bk)
            seen = load_seen()
            reasons = pending_reasons(seen, now)
            last = datetime.datetime.fromisoformat(seen["last_run"]) if seen.get("last_run") else None
            gap_ok = last is None or (now - last).total_seconds() >= MIN_GAP_MIN * 60
            if reasons and seen["runs"] < MAX_RUNS_PER_DAY and gap_ok:
                seen["runs"] += 1
                seen["last_run"] = now.isoformat()
                save_seen(seen)
                why = "; ".join(reasons)
                emit("AI RUN %d: %s" % (seen["runs"], why))
                try:
                    emit("AI SAYS:\n" + run_ai(why, dry=a.dry_run))
                except Exception as e:
                    emit("AI ERROR %s" % str(e)[-300:])
            elif reasons:
                emit("AI deferred (%s): %s" % ("daily run cap" if seen["runs"] >= MAX_RUNS_PER_DAY else "too soon",
                                               "; ".join(reasons)))
                # not saved: the reasons stay pending and wake the AI once the gap/cap allows
            else:
                save_seen(seen)
        time.sleep(POLL_SECONDS)


def cmd_trigger(a):
    emit("AI TRIGGER: %s" % a.reason)
    print(run_ai(a.reason, dry=a.dry_run))
    return 0


def cmd_mark(a):
    mark(book_mod.Book())
    return 0


def cmd_show(a):
    print(json.dumps(book_mod.Book().summary(), indent=1, default=str))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="log what would wake the AI, don't call it")
    t = sub.add_parser("trigger")
    t.add_argument("--reason", required=True)
    t.add_argument("--dry-run", action="store_true")
    sub.add_parser("mark")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    os.chdir(BASE_DIR)
    return {"run": cmd_run, "trigger": cmd_trigger, "mark": cmd_mark, "show": cmd_show}.get(a.cmd, lambda _: ap.print_help())(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
