#!/usr/bin/env python3
"""Market Watch -- background opportunity watcher for the Indian session (paper only).

Every 5-minute candle close (plus a short settle delay) during the IN session it:
  1. Runs `trader-scanner/scan.py intraday` (unchanged CLI) over the full NIFTY 200
     (trader/universe_nifty200.txt, built with `scan.py universe` if missing) from the
     first post-open candle, so early movers outside the daily-setup list are not missed.
  2. For each ENTRY CONFIRMED, actionable, long equity hit, builds a plan from the
     scanner's OWN computed entry/stop/2R target and runs it through
     `trader-budget/size.py: size_trade()` -- the same function, same config, same
     portfolio cash the real budget step uses. It qualifies only if size_trade returns
     no refusal, qty > 0 and net reward:risk after Indian charges >= the config minimum.
  3. Runs the trader-options specialist on the index list and qualifies only signals
     with signal_state == SIGNAL VALIDATED and is_actionable == True.

It adds NO financial logic of its own: every number it reports was produced by the
scanner, the budget engine or the options engine. It is a pre-filter that tells you
WHEN to run the real pipeline (entry -> exit -> timemachine -> budget -> risk -> broker).

It NEVER writes trader/orders.json, trader/plan.json or trader/entry.json, never calls
trader-risk or trader-broker, and never places or routes an order.

Output (one line per event, on stdout -- tail the log or stream it):
  OPPORTUNITY ...   a new qualifying setup (also a macOS notification + JSONL record)
  WATCH ...         start / half-hourly heartbeat / session end
  WATCH ERROR ...   a failing cycle step (rate-limited per distinct message)

Usage:
  python3 scripts/market_watch.py                 # run until the no-new-entries cutoff
  python3 scripts/market_watch.py --once          # one evaluation cycle, then exit
  nohup caffeinate -i python3 scripts/market_watch.py >> trader/state/watch.log 2>&1 &
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("trader-alerts", "trader-entry", "trader-scanner", "trader-budget", "trader-options"):
    _p = os.path.join(BASE_DIR, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import size  # noqa: E402  (trader-budget/size.py)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SETUPS = "breakout_intraday,vwap_reclaim,ema_alignment,rsi_momentum"
SESSION_OPEN = datetime.time(9, 15)
FIRST_CHECK = datetime.time(9, 20)
SETTLE_SECONDS = 25
HEARTBEAT_MINUTES = 30
ERROR_REPEAT_SECONDS = 3600
MAX_POPUPS_PER_CYCLE = 3
DEFAULT_UNIVERSE = os.path.join("trader", "universe_nifty200.txt")
EXIT_SETUPS = ("breakout_intraday", "vwap_reclaim", "ema_alignment", "rsi_momentum")
# Default exit rule, chosen on a 37-session NIFTY200 backtest (scripts/backtest_intraday.py, 2026-11-17):
# swing-low stop + 2.2R cut stop-outs from ~64% to ~14-22% vs 1.5xATR + 3R and lifted net expectancy
# from about -0.2R to about break-even. Re-run the backtest before changing these.
DEFAULT_STOP = "swing"
DEFAULT_TARGET_R = 2.2


def now_ist():
    return datetime.datetime.now(IST)


def emit(line):
    print("%s %s" % (now_ist().strftime("%H:%M:%S"), line), flush=True)


def notify(title, message, spoken="Trade opportunity"):
    """macOS alerts, none of which block the watcher loop:
    a spoken alert and an on-screen dialog (neither needs notification permission),
    plus a Notification Center banner (needs Script Editor notification permission)."""
    def _bg(cmd):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    _bg(["say", spoken])
    _bg(["osascript", "-e", "display alert %s message %s giving up after 600"
         % (json.dumps(title), json.dumps(message))])
    _bg(["osascript", "-e", 'display notification %s with title %s sound name "Glass"'
         % (json.dumps(message), json.dumps(title))])


def parse_hhmm(text, default):
    try:
        h, m = str(text).split(":")[:2]
        return datetime.time(int(h), int(m))
    except Exception:
        return default


class Watcher(object):
    def __init__(self, args):
        self.args = args
        self.cfg = size.load_config(args.config, market="IN")
        self.min_rr = float(self.cfg.get("min_reward_risk") or 2.0)
        self.cutoff = parse_hhmm(self.cfg.get("no_entries_after"), datetime.time(15, 0))
        self.seen = set()
        self.last_error = {}
        self.cycles = 0
        self.confirmed_rejected = 0
        self.opportunities = 0
        self.alerts_this_cycle = 0
        self.last_heartbeat = time.time()
        self.events_path = os.path.join(BASE_DIR, "trader", "state", "watch_events.jsonl")

    # ------------------------------------------------------------------ helpers
    def error(self, where, exc):
        msg = "%s: %s" % (where, str(exc).strip().splitlines()[-1][:300] if str(exc).strip() else type(exc).__name__)
        last = self.last_error.get(msg)
        if last is None or time.time() - last > ERROR_REPEAT_SECONDS:
            self.last_error[msg] = time.time()
            emit("WATCH ERROR " + msg)

    def record(self, kind, payload, summary):
        self.opportunities += 1
        payload = dict(payload, kind=kind, detected_at=now_ist().isoformat(), summary=summary)
        try:
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")
        except Exception as e:
            self.error("event log", e)
        emit("OPPORTUNITY " + summary)
        self.alerts_this_cycle += 1
        if self.alerts_this_cycle > MAX_POPUPS_PER_CYCLE:
            return  # logged above; don't stack voice/pop-ups (flood backstop)
        sym = (payload.get("hit") or {}).get("symbol") or ""
        notify("Trade opportunity (paper)", summary,
               spoken="Trade opportunity. %s %s." % (kind, sym) if sym else "Trade opportunity. %s." % kind)

    def portfolio_cash(self):
        path = os.path.join(BASE_DIR, "trader", "portfolio.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                pf = json.load(fh)
            return float(pf.get("cash")) if isinstance(pf, dict) and pf.get("cash") is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------ equities
    def scan_equities(self):
        cmd = [sys.executable, os.path.join(BASE_DIR, "trader-scanner", "scan.py"), "intraday",
               "--market", "IN"] + (["--universe", self.args.universe] if self.args.universe
                                    else ["--watchlist", self.args.watchlist]) + ["--setups", self.args.setups,
               "--interval", "5m", "--state", os.path.join("trader", "state", "watch_scan.json"),
               "--no-write", "--json"]
        out = subprocess.run(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=290)
        if out.returncode != 0:
            raise RuntimeError(out.stderr or out.stdout or "scan.py exit %d" % out.returncode)
        doc = json.loads(out.stdout[out.stdout.index("{"):])
        return doc

    def trigger_age_minutes(self, close_time):
        try:
            hh, mm = [int(x) for x in str(close_time).split(":")[:2]]
        except Exception:
            return None
        now = now_ist()
        trig = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return (now - trig).total_seconds() / 60.0

    def exit_plan(self, sym, entry, entry_time, stop_arg, setup=None):
        cmd = [sys.executable, os.path.join(BASE_DIR, "trader-exit", "exit.py"), "plan",
               "--symbol", sym, "--entry", str(entry), "--interval", "5m", "--market", "IN",
               "--exchange", "NSE", "--instruments", self.args.instruments,
               "--stop", stop_arg, "--target", "%sR" % self.args.target_r, "--json"]
        if entry_time:
            cmd[cmd.index("--interval"):cmd.index("--interval")] = ["--time", str(entry_time)]
        if setup in EXIT_SETUPS:   # so the plan (and the Time Machine) knows which setup to replay
            cmd += ["--setup", setup]
        try:
            out = subprocess.run(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeError("exit.py plan timed out after 60s")
        if out.returncode != 0 or "{" not in out.stdout:
            raise RuntimeError((out.stderr or out.stdout or "exit.py exit %d" % out.returncode).strip())
        return json.loads(out.stdout[out.stdout.index("{"):])

    def check_equities(self):
        doc = self.scan_equities()
        provider = doc.get("provider")
        cash = self.portfolio_cash()
        today = now_ist().date()
        for h in doc.get("hits") or []:
            if h.get("decision_state") != "ENTRY CONFIRMED" or not h.get("actionable"):
                continue
            if h.get("analytical_only") or not h.get("is_tradable_equity", True):
                continue
            if str(h.get("side") or "buy").lower() not in ("buy", "long"):
                continue
            d = h.get("decision") or {}
            trig = h.get("trigger") or {}
            key = "EQ|%s|%s|%s|%s" % (today, h.get("symbol"), h.get("setup"), trig.get("close_time"))
            if key in self.seen:
                continue
            # Freshness: only a trigger from the latest candle(s) is an opportunity. The scanner keeps
            # an old trigger "ENTRY CONFIRMED" whenever price drifts back into its zone; alerting on
            # those (e.g. a 09:30 trigger at 13:52) is a late entry -- a different trade.
            age = self.trigger_age_minutes(trig.get("close_time"))
            if age is None or age > self.args.max_trigger_age:
                self.seen.add(key)
                continue
            entry = d.get("entry_reference")
            if entry is None or d.get("stop_loss") is None:
                continue
            # Stop/target come from the real trader-exit CLI (exit.compute), with the same
            # --stop/--target flags you'd use in the pipeline -- the watcher computes nothing.
            stop_arg = ("price:%s" % d.get("stop_loss")) if self.args.stop == "scanner" else self.args.stop
            try:
                ex = self.exit_plan(h.get("symbol"), entry, trig.get("close_time"), stop_arg, h.get("setup"))
            except Exception as e:
                self.seen.add(key)
                self.confirmed_rejected += 1
                if "timed out" in str(e).lower():
                    self.error("exit plan %s" % h.get("symbol"), e)
                continue
            stop, target = ex.get("stop"), ex.get("target")
            plan = {
                "symbol": h.get("symbol"), "side": "buy", "setup": h.get("setup"),
                "entry": ex.get("entry", entry), "stop": stop, "target": target,
                "asset_type": "EQUITY", "trading_style": "intraday", "market": "IN",
                "exchange": "NSE", "currency": "INR", "interval": h.get("interval"),
                "provider": h.get("provider") or provider, "data_status": h.get("data_status"),
                "is_live_signal": h.get("is_live_signal"),
                "exit_cmd": "exit.py plan --stop %s --target %sR" % (stop_arg, self.args.target_r),
            }
            entry = plan["entry"]
            try:
                s = size.size_trade(plan, self.cfg, cash=cash, today=today, market="IN",
                                    instruments_path=self.args.instruments,
                                    trading_style="intraday", exchange="NSE")
            except Exception as e:  # SizeError and friends: a refusal, not a crash
                self.seen.add(key)
                self.confirmed_rejected += 1
                continue
            self.seen.add(key)
            net_rr = s.get("net_reward_risk")
            if s.get("refusal") or not s.get("qty") or net_rr is None or net_rr < self.min_rr - 1e-9:
                self.confirmed_rejected += 1
                continue
            costs = (s.get("transaction_costs") or {}).get("total")
            summary = ("EQUITY %s %s | trigger %s @ %.2f | entry %.2f stop %.2f target %.2f | qty %s | "
                       "net R:R %.2f after charges (est. charges %s) | %s %s"
                       % (h.get("symbol"), h.get("setup"), trig.get("close_time"), float(trig.get("close") or entry),
                          float(entry), float(stop), float(target), s.get("qty"), net_rr,
                          ("%.2f" % costs) if costs is not None else "n/a",
                          plan["provider"], plan["data_status"]))
            self.record("equity", {"hit": h, "sizing": {k: s.get(k) for k in
                        ("qty", "entry", "stop", "target", "net_reward_risk", "net_reward_after_costs",
                         "net_risk_including_costs", "bound_by", "max_loss_per_trade")}}, summary)

    # ------------------------------------------------------------------ options
    def check_options(self):
        if not self.args.options:
            return
        today = now_ist().date()
        for sym in self.args.options:
            # Each index runs in its own subprocess with a hard timeout: an in-process options
            # call hung the watcher silently once (2026-11-17 ~11:22), so isolate it.
            cmd = [sys.executable, os.path.abspath(__file__), "--options-worker", sym,
                   "--budget", str(self.args.budget)]
            try:
                out = subprocess.run(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True, timeout=90)
                if out.returncode != 0:
                    raise RuntimeError(out.stderr or "worker exit %d" % out.returncode)
                signals = json.loads(out.stdout[out.stdout.index("["):])
            except subprocess.TimeoutExpired:
                self.error("options %s" % sym, "timed out after 90s (skipped this cycle)")
                continue
            except Exception as e:
                self.error("options %s" % sym, e)
                continue
            for sig in signals:
                key = "OPT|%s|%s|%s|%s" % (today, sig.get("contract"), sig.get("strategy"),
                                           (sig.get("underlying_trigger") or {}).get("time")
                                           if isinstance(sig.get("underlying_trigger"), dict) else sig.get("underlying_trigger"))
                if key in self.seen:
                    continue
                self.seen.add(key)
                ps = sig.get("position_sizing") or {}
                summary = ("OPTIONS %s %s %s | premium %s | stop %s T1 %s | net R:R %s | lots %s | max loss %s "
                           "(decision-support only; options are never executed by this system)"
                           % (sym, sig.get("strategy"), sig.get("contract"), sig.get("current_premium"),
                              sig.get("stop_loss"), sig.get("target_1"),
                              sig.get("expected_risk_reward_after_charges"), ps.get("lots"),
                              sig.get("maximum_loss")))
                self.record("options", {"signal": sig}, summary)

    # ------------------------------------------------------------------ loop
    def cycle(self):
        self.cycles += 1
        self.alerts_this_cycle = 0
        try:
            self.check_equities()
        except Exception as e:
            self.error("equity scan", e)
        try:
            self.check_options()
        except Exception as e:
            self.error("options scan", e)

    def heartbeat(self, force=False):
        if force or time.time() - self.last_heartbeat >= HEARTBEAT_MINUTES * 60:
            self.last_heartbeat = time.time()
            emit("WATCH alive: %d cycles, %d opportunities, %d confirmed setups refused by budget/net R:R"
                 % (self.cycles, self.opportunities, self.confirmed_rejected))

    def next_wake(self, now):
        base = now.replace(second=0, microsecond=0)
        minute = (base.minute // 5 + 1) * 5
        nxt = base.replace(minute=0) + datetime.timedelta(minutes=minute)
        return nxt + datetime.timedelta(seconds=SETTLE_SECONDS)

    def run(self):
        emit("WATCH start: equities from %s (plan: stop %s, target %sR via trader-exit), options %s, "
             "budget Rs %.0f, min net R:R %.2f, cutoff %s IST. Paper only; never places orders."
             % (self.args.universe or self.args.watchlist, self.args.stop, self.args.target_r,
                ",".join(self.args.options) or "off", self.args.budget,
                self.min_rr, self.cutoff.strftime("%H:%M")))
        if self.args.once:
            self.cycle()
            self.heartbeat(force=True)
            return 0
        while True:
            now = now_ist()
            if now.weekday() >= 5 or now.time() >= self.cutoff:
                emit("WATCH end: %s IST is past the %s no-new-entries cutoff (or not a weekday). "
                     "%d cycles, %d opportunities." % (now.strftime("%H:%M"), self.cutoff.strftime("%H:%M"),
                                                      self.cycles, self.opportunities))
                return 0
            if now.time() >= FIRST_CHECK:
                self.cycle()
                self.heartbeat()
            wake = max(self.next_wake(now_ist()),
                       now_ist().replace(hour=FIRST_CHECK.hour, minute=FIRST_CHECK.minute, second=SETTLE_SECONDS, microsecond=0))
            time.sleep(max(1.0, (wake - now_ist()).total_seconds()))


def build_universe(path):
    """Write the NIFTY 200 universe file with the scanner's own `universe` command."""
    emit("WATCH building %s from the NIFTY200 index (scan.py universe)" % path)
    subprocess.run([sys.executable, os.path.join("trader-scanner", "scan.py"), "universe", "--market", "IN",
                    "--index", "NIFTY200", "--exchange", "NSE", "--out", path],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300, check=True)
    if not os.path.exists(path):
        raise SystemExit("could not build %s; pass --universe or --watchlist" % path)


def options_worker(sym, budget):
    """Child-process mode: print validated + actionable options signals for one symbol as JSON."""
    os.chdir(BASE_DIR)
    from options import OptionsIntelligenceSpecialist, STATE_SIGNAL_VALIDATED
    res = OptionsIntelligenceSpecialist().analyze_symbol_for_options(sym, max_loss_budget=budget)
    keep = [s for s in (res.get("signals") or [])
            if s.get("signal_state") == STATE_SIGNAL_VALIDATED and s.get("is_actionable")]
    sys.stdout.write(json.dumps(keep, default=str))
    sys.stdout.flush()
    os._exit(0)  # don't wait on any lingering feed threads


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--watchlist", default=None,
                    help="scan a watchlist JSON instead of the universe (e.g. trader/daily_scan.json)")
    ap.add_argument("--universe", default=DEFAULT_UNIVERSE,
                    help="SYMBOL|Name universe file (default %(default)s; built from NIFTY200 if missing)")
    ap.add_argument("--options", default="NIFTY,BANKNIFTY,FINNIFTY,MIDCPNIFTY,SENSEX",
                    help="comma-separated index/F&O symbols for trader-options ('' to disable)")
    ap.add_argument("--budget", type=float, default=2000.0, help="options max-loss budget (INR)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--instruments", default=os.path.join("trader", "state", "instruments.json"))
    ap.add_argument("--stop", default=DEFAULT_STOP,
                    help="trader-exit stop rule for the plan: swing[:N], 'scanner' (scanner's invalidation "
                         "level), atr:K, or pct:X (default %(default)s)")
    ap.add_argument("--target-r", type=float, default=DEFAULT_TARGET_R,
                    help="target in R for trader-exit (default %(default)s)")
    ap.add_argument("--max-trigger-age", type=float, default=10.0,
                    help="only alert on triggers whose candle closed within this many minutes (default 10)")
    ap.add_argument("--setups", default=SETUPS,
                    help="scanner setups to watch (default: %s)" % SETUPS)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--options-worker", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.options_worker:
        return options_worker(a.options_worker, a.budget)
    a.options = [s.strip().upper() for s in a.options.split(",") if s.strip()]
    os.chdir(BASE_DIR)
    if a.watchlist:
        a.universe = None
    elif not os.path.exists(a.universe):
        build_universe(a.universe)
    return Watcher(a).run()


if __name__ == "__main__":
    sys.exit(main())
