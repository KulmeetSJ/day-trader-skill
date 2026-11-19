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
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
import probability  # noqa: E402  (scripts/probability.py: backtest odds for a call)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SETUPS = "breakout_intraday,vwap_reclaim,ema_alignment,rsi_momentum"
# Bearish setups are scanned for ANALYSIS ONLY: equity execution is long-only (non-negotiable), so
# these are never alerted as trades -- they go to rejected.csv as "SHORT (not tradable)" so you can
# see what the bearish side would have done.
SHORT_SETUPS = "breakdown_intraday,vwap_rejection"
SESSION_OPEN = datetime.time(9, 15)
FIRST_CHECK = datetime.time(9, 20)
SETTLE_SECONDS = 25
HEARTBEAT_MINUTES = 30
ERROR_REPEAT_SECONDS = 3600
MAX_POPUPS_PER_CYCLE = 5
DEFAULT_UNIVERSE = os.path.join("trader", "universe_nifty200.txt")
EXIT_SETUPS = ("breakout_intraday", "vwap_reclaim", "ema_alignment", "rsi_momentum")
# Default exit rule, chosen on a 37-session NIFTY200 backtest (scripts/backtest_intraday.py, 2026-11-17):
# swing-low stop + 2.2R cut stop-outs from ~64% to ~14-22% vs 1.5xATR + 3R and lifted net expectancy
# from about -0.2R to about break-even. Re-run the backtest before changing these.
DEFAULT_STOP = "swing"
# Setups still scanned and scored in rejected.csv, but never alerted (comma-separated). Empty: none.
# (ema_alignment was here for a day; among calls that pass the budget gate it was no worse than the
# other setups in the 37-session backtest -- 2026-11-19 diagnosis -- so it is alerted again.)
ANALYSIS_ONLY_SETUPS = ""
DEFAULT_TARGET_R = 2.2
REJECT_FIELDS = ("date", "time", "kind", "direction", "symbol", "setup", "trigger_time", "underlying_price",
                 "entry", "stop", "target", "qty_or_lots", "net_rr", "reasons",
                 "p_target", "p_stop", "p_green", "odds_n", "india_vix", "nifty_chg_pct", "nifty_vs_open_pct")
CSV_FIELDS = ("date", "alert_time", "kind", "action", "symbol", "setup", "trigger_time", "trigger_price",
              "alert_delay_min", "last_close_at_alert", "entry", "stop", "target", "qty", "lots",
              "risk_inr_incl_costs", "net_rr", "provider", "p_target", "p_stop", "p_green", "odds_n",
              "india_vix", "nifty_chg_pct", "nifty_vs_open_pct")


def option_key(day, contract, strategy, trigger_time):
    return "OPT|%s|%s|%s|%s" % (day, contract, strategy, trigger_time)


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


def _last_line(exc):
    text = str(exc).strip()
    return (text.splitlines()[-1] if text else type(exc).__name__)[:200]


def append_row(path, fields, row):
    """Append a CSV row. If the file's header is older (fewer columns), rewrite it once with the
    new header first so old rows keep their values and new columns line up."""
    import csv
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh), None)
        if header and tuple(header) != tuple(fields):
            with open(path, encoding="utf-8", newline="") as fh:
                old = list(csv.DictReader(fh))
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
            os.replace(tmp, path)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def odds_fields(o):
    if not o:
        return {}
    return {"p_target": round(o["p"]["target"], 3), "p_stop": round(o["p"]["stop"], 3),
            "p_green": round(o["p_green"], 3), "odds_n": o["n"]}


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
        self.csv_path = os.path.join(BASE_DIR, "trader", "state", "opportunities.csv")
        self.rejected_path = os.path.join(BASE_DIR, "trader", "state", "rejected.csv")
        self.rejected = 0
        self.active_short = {}
        # symbol -> today's last equity call {entry, stop, target, time}. A new hit whose entry is still
        # inside that call's stop..target range is the same trade (another setup or a few points off),
        # so it is logged as a repeat, not alerted again. Seeded from the CSV so a restart remembers.
        self.active = self.load_active_calls()
        # Options signals already alerted or rejected today: a restart must not alert them again.
        self.seen.update(self.load_seen_options())
        # India VIX and NIFTY at the time of each call, logged with it (refreshed once a cycle) so the
        # VIX / market-regime leads from the 2026-11-19 diagnosis can be tested on new days.
        self.market_ctx = {}
        # Daily-loss stop: once today's alerted equity calls (marked to market by scripts/scoreboard.py,
        # after est. charges) are down max_daily_loss_inr, no more calls are alerted today. Latched for
        # the day in daily_halt.json so a restart or a bounce doesn't re-open it.
        self.max_daily_loss = float(self.cfg.get("max_daily_loss_inr") or 5000.0)
        self.day_path = os.path.join(BASE_DIR, "trader", "state", "scoreboard_day.json")
        self.halt_path = os.path.join(BASE_DIR, "trader", "state", "daily_halt.json")
        self.halted = self.load_halt()
        # Odds table from scripts/probability.py (backtest base rates). Missing -> "odds n/a", never invented.
        self.prob_model = probability.load_model(os.path.join(BASE_DIR, probability.MODEL_PATH))
        self.loss_unknown = None

    def load_active_calls(self):
        import csv
        out = {}
        today = now_ist().strftime("%Y-%m-%d")
        try:
            with open(self.csv_path, encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r.get("date") == today and r.get("kind") == "equity":
                        out[r["symbol"]] = {"entry": float(r["entry"]), "stop": float(r["stop"]),
                                            "target": float(r["target"]), "time": r.get("alert_time", "")[:5]}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return out

    def refresh_market_ctx(self):
        """{india_vix, nifty_chg_pct, nifty_vs_open_pct} from live quotes; blank on any failure
        (context for later analysis only -- never gates a call)."""
        ctx = {}
        try:
            import data_in
            p = data_in.get_provider()
            v = p.get_quote("INDIA VIX") or {}
            if v.get("price") is not None:
                ctx["india_vix"] = round(float(v["price"]), 2)
            n = p.get_quote("NIFTY") or {}
            px, prev, opn = n.get("price"), n.get("prev_close"), n.get("open")
            if px is not None and prev:
                ctx["nifty_chg_pct"] = round((float(px) - float(prev)) / float(prev) * 100, 2)
            if px is not None and opn:
                ctx["nifty_vs_open_pct"] = round((float(px) - float(opn)) / float(opn) * 100, 2)
        except Exception as e:
            self.error("market context", e)
        self.market_ctx = ctx

    def load_seen_options(self):
        """Today's options dedupe keys (same format as check_options) rebuilt from both CSVs."""
        import csv
        keys = set()
        today = now_ist().strftime("%Y-%m-%d")
        for path in (self.csv_path, self.rejected_path):
            try:
                with open(path, encoding="utf-8", newline="") as fh:
                    for r in csv.DictReader(fh):
                        if r.get("date") != today or r.get("kind") != "options":
                            continue
                        strat = (r.get("setup") or "").split(" ")[0]
                        keys.add(option_key(today, r.get("symbol"), strat, r.get("trigger_time") or None))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return keys

    # ------------------------------------------------------------------ helpers
    def load_halt(self):
        try:
            with open(self.halt_path, encoding="utf-8") as fh:
                h = json.load(fh)
            return h if h.get("date") == now_ist().strftime("%Y-%m-%d") else None
        except (OSError, ValueError, AttributeError):
            return None

    def update_daily_loss(self):
        """Read scoreboard_day.json; latch the halt when the day's net P&L reaches -max_daily_loss.
        If the total can't be read while calls are open today, block alerts this cycle (fail closed)."""
        self.loss_unknown = None
        if self.halted:
            return
        today = now_ist().strftime("%Y-%m-%d")
        try:
            with open(self.day_path, encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("date") != today:
                raise ValueError("scoreboard total is for %s, not today" % d.get("date"))
            as_of = datetime.datetime.strptime(d["as_of"], "%Y-%m-%d %H:%M")
            age = (now_ist().replace(tzinfo=None) - as_of).total_seconds() / 60.0
            if age > 15:
                raise ValueError("scoreboard total is %.0f min old" % age)
            net = float(d["net_inr_worst_case"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            if self.active:   # calls exist today but their P&L is unknown
                self.loss_unknown = "DAILY_LOSS_UNKNOWN: can't read today's P&L (%s); no new calls until it can" % e
            return
        if net <= -self.max_daily_loss + 1e-9:
            self.halted = {"date": today, "at": now_ist().strftime("%H:%M"), "net_inr": net,
                           "limit_inr": self.max_daily_loss}
            try:
                with open(self.halt_path, "w", encoding="utf-8") as fh:
                    json.dump(self.halted, fh)
            except OSError as e:
                self.error("daily halt file", e)
            msg = ("Daily loss limit hit: today's calls are at Rs %.0f (limit Rs %.0f). No more calls today; "
                   "still logging to rejected.csv." % (net, self.max_daily_loss))
            emit("WATCH HALT: " + msg)
            notify("STOP - daily loss limit (paper)", msg, spoken="Daily loss limit hit. No more trades today.")

    def loss_block(self):
        """Reason string if new calls must not be alerted, else None."""
        if self.halted:
            return ("DAILY_LOSS: today's calls reached Rs %.0f at %s (limit Rs %.0f); no new calls today"
                    % (self.halted["net_inr"], self.halted["at"], self.halted["limit_inr"]))
        return self.loss_unknown

    def error(self, where, exc):
        msg = "%s: %s" % (where, str(exc).strip().splitlines()[-1][:300] if str(exc).strip() else type(exc).__name__)
        last = self.last_error.get(msg)
        if last is None or time.time() - last > ERROR_REPEAT_SECONDS:
            self.last_error[msg] = time.time()
            emit("WATCH ERROR " + msg)

    def append_csv(self, row):
        """One human-readable row per opportunity (open in Numbers/Excel) -- survives dismissing the pop-up."""
        try:
            append_row(self.csv_path, CSV_FIELDS, row)
        except Exception as e:
            self.error("csv log", e)

    def setup_block(self, setup):
        off = [x.strip() for x in str(getattr(self.args, "analysis_only_setups", ANALYSIS_ONLY_SETUPS) or "").split(",")
               if x.strip()]
        if setup in off:
            return "SETUP_OFF: %s is analysis-only (no edge in backtest); logged for scoring, not alerted" % setup
        return None

    def reject(self, row):
        """A call that was found but NOT alerted -- with why -- so it can be scored later
        (scripts/scoreboard.py). Never a pop-up."""
        self.rejected += 1
        now = now_ist()
        row = dict(self.market_ctx, **row)
        row = dict(row, date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M:%S"))
        try:
            append_row(self.rejected_path, REJECT_FIELDS, row)
        except Exception as e:
            self.error("rejected log", e)

    def record(self, kind, payload, summary, title, spoken, row):
        self.opportunities += 1
        detected = now_ist()
        payload = dict(payload, kind=kind, detected_at=detected.isoformat(), summary=summary)
        try:
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")
        except Exception as e:
            self.error("event log", e)
        self.append_csv(dict(self.market_ctx, **dict(row, date=detected.strftime("%Y-%m-%d"),
                                                     alert_time=detected.strftime("%H:%M:%S"), kind=kind)))
        emit("OPPORTUNITY " + summary)
        self.alerts_this_cycle += 1
        if self.alerts_this_cycle > MAX_POPUPS_PER_CYCLE:
            return  # logged above; don't stack voice/pop-ups (flood backstop)
        notify(title, summary + "\n\nSaved to trader/state/opportunities.csv", spoken=spoken)

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
                                    else ["--watchlist", self.args.watchlist]) + ["--setups", self.scan_setups(),
               "--interval", "5m", "--state", os.path.join("trader", "state", "watch_scan.json"),
               "--no-write", "--json"]
        out = subprocess.run(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=290)
        if out.returncode != 0:
            raise RuntimeError(out.stderr or out.stdout or "scan.py exit %d" % out.returncode)
        doc = json.loads(out.stdout[out.stdout.index("{"):])
        return doc

    def scan_setups(self):
        return self.args.setups + ("" if self.args.no_short_signals else "," + SHORT_SETUPS)

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
                self.log_short(h, today)
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
            prev = self.active.get(h.get("symbol"))
            if prev and prev["stop"] < float(entry) < prev["target"]:
                self.seen.add(key)
                emit("WATCH repeat (no alert): %s %s @ %.2f is inside the %s call's range %.2f-%.2f"
                     % (h.get("symbol"), h.get("setup"), float(entry), prev["time"], prev["stop"], prev["target"]))
                continue
            # Stop/target come from the real trader-exit CLI (exit.compute), with the same
            # --stop/--target flags you'd use in the pipeline -- the watcher computes nothing.
            stop_arg = ("price:%s" % d.get("stop_loss")) if self.args.stop == "scanner" else self.args.stop
            base_row = {"kind": "equity", "direction": "LONG", "symbol": h.get("symbol"), "setup": h.get("setup"),
                        "trigger_time": trig.get("close_time"), "underlying_price": trig.get("close"),
                        "entry": entry}
            try:
                ex = self.exit_plan(h.get("symbol"), entry, trig.get("close_time"), stop_arg, h.get("setup"))
            except Exception as e:
                self.seen.add(key)
                self.confirmed_rejected += 1
                if "timed out" in str(e).lower():
                    self.error("exit plan %s" % h.get("symbol"), e)
                self.reject(dict(base_row, reasons="EXIT_PLAN: " + _last_line(e)))
                continue
            stop, target = ex.get("stop"), ex.get("target")
            odds = probability.odds_for(self.prob_model, trig.get("close_time"), ex.get("entry", entry), target,
                                        ex.get("atr"))
            base_row = dict(base_row, **odds_fields(odds))
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
                self.reject(dict(base_row, entry=entry, stop=stop, target=target, reasons="BUDGET: " + _last_line(e)))
                continue
            self.seen.add(key)
            net_rr = s.get("net_reward_risk")
            if s.get("refusal") or not s.get("qty") or net_rr is None or net_rr < self.min_rr - 1e-9:
                self.confirmed_rejected += 1
                why = ("BUDGET: %s" % s.get("refusal")) if s.get("refusal") else (
                    "BUDGET: qty 0" if not s.get("qty") else "NET_RR: %.2f after charges < %.2f" % (net_rr or 0, self.min_rr))
                self.reject(dict(base_row, entry=entry, stop=stop, target=target, qty_or_lots=s.get("qty"),
                                 net_rr=round(net_rr, 2) if net_rr is not None else "", reasons=why))
                continue
            blocked = self.setup_block(h.get("setup")) or self.loss_block()
            if blocked:
                self.reject(dict(base_row, entry=entry, stop=stop, target=target, qty_or_lots=s.get("qty"),
                                 net_rr=round(net_rr, 2), reasons=blocked))
                continue
            costs = (s.get("transaction_costs") or {}).get("total")
            # Latest COMPLETED 5m close the scanner saw (reported, not computed): if it is already
            # below the entry, the breakout has not followed through by the time you read this.
            last_close = ((h.get("candle_context") or {}).get("completed_candle") or {}).get("close")
            age_now = self.trigger_age_minutes(trig.get("close_time"))
            if last_close is not None:
                vs = (float(last_close) - float(entry)) / float(entry) * 100.0
                follow = "last 5m close %.2f (%+.2f%% vs entry%s)" % (
                    float(last_close), vs, ", BELOW ENTRY" if float(last_close) < float(entry) else "")
            else:
                follow = "last 5m close n/a"
            sym = h.get("symbol")
            summary = ("BUY EQUITY SHARES (long, not an option) %s %s | trigger %s @ %.2f | alerted %.0f min later, %s | "
                       "entry %.2f stop %.2f target %.2f | qty %s | net R:R %.2f after charges (est. charges %s) | %s %s"
                       % (sym, h.get("setup"), trig.get("close_time"), float(trig.get("close") or entry),
                          age_now or 0, follow,
                          float(entry), float(stop), float(target), s.get("qty"), net_rr,
                          ("%.2f" % costs) if costs is not None else "n/a",
                          plan["provider"], plan["data_status"]))
            summary += " | " + probability.odds_text(odds, self.prob_model)
            row = {"action": "BUY shares (long equity)", "symbol": sym, "setup": h.get("setup"),
                   "trigger_time": trig.get("close_time"), "trigger_price": trig.get("close"),
                   "alert_delay_min": ("%.1f" % age_now) if age_now is not None else "",
                   "last_close_at_alert": last_close, "entry": entry, "stop": stop, "target": target,
                   "qty": s.get("qty"), "risk_inr_incl_costs": s.get("net_risk_including_costs"),
                   "net_rr": round(net_rr, 2), "provider": plan["provider"]}
            row.update(odds_fields(odds))
            self.record("equity", {"hit": h, "sizing": {k: s.get(k) for k in
                        ("qty", "entry", "stop", "target", "net_reward_risk", "net_reward_after_costs",
                         "net_risk_including_costs", "bound_by", "max_loss_per_trade")}}, summary,
                        title="BUY %s shares - equity, not an option (paper)%s" % (
                            sym, (" - target odds %.0f%%" % (100 * odds["p"]["target"])) if odds else ""),
                        spoken="Buy %s shares. Equity, not an option." % sym, row=row)
            self.active[sym] = {"entry": float(entry), "stop": float(stop), "target": float(target),
                                "time": now_ist().strftime("%H:%M")}

    def log_short(self, h, today):
        """Bearish equity setup: analysis only (long-only execution). Uses the scanner's own
        entry/stop/2R target; fresh triggers only; one row per symbol while price is in range."""
        d = h.get("decision") or {}
        trig = h.get("trigger") or {}
        key = "SHORT|%s|%s|%s|%s" % (today, h.get("symbol"), h.get("setup"), trig.get("close_time"))
        if key in self.seen:
            return
        self.seen.add(key)
        age = self.trigger_age_minutes(trig.get("close_time"))
        entry, stop = d.get("entry_reference"), d.get("stop_loss")
        target = (d.get("targets") or {}).get("t2_2r") or d.get("target_2_2r")
        if age is None or age > self.args.max_trigger_age or None in (entry, stop, target):
            return
        prev = self.active_short.get(h.get("symbol"))
        if prev and prev["target"] < float(entry) < prev["stop"]:
            return
        self.active_short[h.get("symbol")] = {"stop": float(stop), "target": float(target)}
        self.reject({"kind": "equity_short", "direction": "SHORT", "symbol": h.get("symbol"), "setup": h.get("setup"),
                     "trigger_time": trig.get("close_time"), "underlying_price": trig.get("close"),
                     "entry": entry, "stop": stop, "target": target,
                     "reasons": "LONG_ONLY: short side is analysis only, never executed"})

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
                key = option_key(today, sig.get("contract"), sig.get("strategy"),
                                 (sig.get("underlying_trigger") or {}).get("time")
                                 if isinstance(sig.get("underlying_trigger"), dict) else sig.get("underlying_trigger"))
                if key in self.seen:
                    continue
                self.seen.add(key)
                ps = sig.get("position_sizing") or {}
                strat = str(sig.get("strategy") or "")
                _ut = sig.get("underlying_trigger") if isinstance(sig.get("underlying_trigger"), dict) else {}
                missing = [k for k in ("current_premium", "stop_loss", "target_1") if sig.get(k) is None]
                if sig.get("_validated") and missing:   # fail closed: never alert a call with no numbers
                    blocked = "INCOMPLETE_SIGNAL: validated but missing %s; not alerted" % ", ".join(missing)
                else:
                    blocked = (self.setup_block(_ut.get("setup")) or self.loss_block()) if sig.get("_validated") else None
                if not sig.get("_validated") or blocked:
                    ut = sig.get("underlying_trigger") if isinstance(sig.get("underlying_trigger"), dict) else {}
                    self.reject({"kind": "options", "direction": sig.get("direction") or "",
                                 "symbol": sig.get("contract") or sym, "setup": "%s (%s)" % (strat, ut.get("setup") or "?"),
                                 "trigger_time": ut.get("time"), "underlying_price": ut.get("current_spot") or ut.get("price"),
                                 "entry": sig.get("current_premium"), "stop": sig.get("stop_loss"),
                                 "target": sig.get("target_1"), "qty_or_lots": ps.get("lots"),
                                 "net_rr": sig.get("expected_risk_reward_after_charges"),
                                 "reasons": blocked or "; ".join(str(r)[:120] for r in (sig.get("rejection_reasons") or []))
                                 or sig.get("signal_state")})
                    continue
                summary = ("OPTION %s %s %s | premium %s | stop %s T1 %s | net R:R %s | lots %s | max loss %s "
                           "(decision-support only; options are never executed by this system)"
                           % (strat, sym, sig.get("contract"), sig.get("current_premium"),
                              sig.get("stop_loss"), sig.get("target_1"),
                              sig.get("expected_risk_reward_after_charges"), ps.get("lots"),
                              sig.get("maximum_loss")))
                row = {"action": "OPTION %s" % strat, "symbol": sig.get("contract") or sym, "setup": strat,
                       "trigger_time": (sig.get("underlying_trigger") or {}).get("time")
                       if isinstance(sig.get("underlying_trigger"), dict) else sig.get("underlying_trigger"),
                       "entry": sig.get("current_premium"), "stop": sig.get("stop_loss"),
                       "target": sig.get("target_1"), "lots": ps.get("lots"),
                       "risk_inr_incl_costs": sig.get("maximum_loss"),
                       "net_rr": sig.get("expected_risk_reward_after_charges")}
                self.record("options", {"signal": sig}, summary,
                            title="OPTION %s %s (decision-support only)" % (strat.replace("_", " "), sym),
                            spoken="Option signal. %s %s." % (sym, strat.replace("_", " ")), row=row)

    # ------------------------------------------------------------------ loop
    def cycle(self):
        self.cycles += 1
        self.alerts_this_cycle = 0
        t0 = time.time()
        # Scoreboard first: it writes today's P&L total that the daily-loss stop reads.
        self.refresh_scoreboard()
        self.update_daily_loss()
        self.refresh_market_ctx()
        try:
            if getattr(self.args, "equities", False):
                self.check_equities()
        except Exception as e:
            self.error("equity scan", e)
        t_eq = time.time() - t0
        try:
            self.check_options()
        except Exception as e:
            self.error("options scan", e)
        took = time.time() - t0
        if took > 300 - SETTLE_SECONDS:
            emit("WATCH SLOW: cycle took %.0fs (equities %.0fs, options %.0fs) -- the next 5m candle's "
                 "check is skipped, so alerts run late" % (took, t_eq, took - t_eq))

    def heartbeat(self, force=False):
        if force or time.time() - self.last_heartbeat >= HEARTBEAT_MINUTES * 60:
            self.last_heartbeat = time.time()
            emit("WATCH alive: %d cycles, %d opportunities, %d confirmed setups refused by budget/net R:R, "
                 "%d rejected/short calls logged to trader/state/rejected.csv%s"
                 % (self.cycles, self.opportunities, self.confirmed_rejected, self.rejected,
                    " | HALTED for the day (daily loss limit)" if self.halted else ""))

    def refresh_scoreboard(self):
        """Rewrite trader/state/scoreboard.txt (scripts/scoreboard.py) in its own process."""
        try:
            out = subprocess.run([sys.executable, os.path.join(BASE_DIR, "scripts", "scoreboard.py")],
                                 cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True, timeout=150)
            if out.returncode != 0:
                raise RuntimeError(out.stderr or "scoreboard exit %d" % out.returncode)
        except Exception as e:
            self.error("scoreboard", e)

    def next_wake(self, now):
        base = now.replace(second=0, microsecond=0)
        minute = (base.minute // 5 + 1) * 5
        nxt = base.replace(minute=0) + datetime.timedelta(minutes=minute)
        return nxt + datetime.timedelta(seconds=SETTLE_SECONDS)

    def run(self):
        emit("WATCH start: equities %s (plan: stop %s, target %sR via trader-exit), options %s, "
             "budget Rs %.0f, min net R:R %.2f, cutoff %s IST. Paper only; never places orders."
             % (("from %s" % (self.args.universe or self.args.watchlist)) if getattr(self.args, "equities", False)
                else "OFF (intraday equity alerts retired 2026-11-19; --equities to re-enable)", self.args.stop, self.args.target_r,
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
    """Child-process mode: print every options signal for one symbol as JSON, each flagged
    `_validated` (SIGNAL VALIDATED and actionable) or not, so the parent can alert the first
    kind and log the second with its rejection reasons.

    Uses the same MarketIntelligenceOrchestrator + Upstox WebSocket streamer lifecycle as
    `options.py analyze`: the streamer supplies the exchange timestamps the engine's freshness
    checks require (the REST option chain carries none, so without it every signal fails closed
    with UNDERLYING_SPOT_UNVERIFIED / MISSING_QUOTE_TIMESTAMP). If the streamer can't connect,
    analysis still runs and fails closed exactly as before -- the checks are never relaxed."""
    os.chdir(BASE_DIR)
    from options import STATE_SIGNAL_VALIDATED
    from orchestrator import MarketIntelligenceOrchestrator
    with MarketIntelligenceOrchestrator() as orch:
        if orch.streamer is not None and not getattr(orch.streamer, "is_connected", False):
            try:
                orch.streamer.connect()
            except Exception as e:
                sys.stderr.write("options streamer connect failed: %s\n" % _last_line(e))
        if orch.streamer is not None and getattr(orch.specialist, "streamer", None) is None:
            orch.specialist.streamer = orch.streamer
        res = orch.specialist.analyze_symbol_for_options(sym, max_loss_budget=budget)
    out = []
    for s in res.get("signals") or []:
        s = dict(s, _validated=bool(s.get("signal_state") == STATE_SIGNAL_VALIDATED and s.get("is_actionable")))
        out.append(s)
    sys.stdout.write(json.dumps(out, default=str))
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
    ap.add_argument("--analysis-only-setups", default=ANALYSIS_ONLY_SETUPS,
                    help="setups scanned and logged to rejected.csv but never alerted, comma-separated; "
                         "'' alerts all (default: %(default)s)")
    ap.add_argument("--equities", action="store_true",
                    help="also scan NIFTY200 for INTRADAY equity alerts (retired by default 2026-11-19: no edge "
                         "in the 37-session backtest; multi-day equity calls live in scripts/equity_calls.py)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-short-signals", action="store_true",
                    help="don't scan bearish setups for rejected.csv (they are never alerted either way)")
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
