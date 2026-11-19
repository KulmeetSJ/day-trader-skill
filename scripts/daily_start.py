#!/usr/bin/env python3
"""Daily auto-start for the paper-trading stack (macOS launchd).

    python3 scripts/daily_start.py start       # start whatever isn't running (no-op on non-trading days)
    python3 scripts/daily_start.py status      # which watchers are running
    python3 scripts/daily_start.py install     # launchd agent: run `start` at 09:00 IST Mon-Fri (+ at login)
    python3 scripts/daily_start.py uninstall

`start` launches, each under `caffeinate -i` with output appended to trader/state/<name>.log:
  market_watch  --equities   intraday leads; exits by itself at the 15:00 cutoff, so it is started daily
  options_watch run          credit-spread rules 15:10 / tracking 15:40 (long-running)
  equity_calls  run          multi-day scan + tracking 15:45 (long-running)
  ai_trader     run          marks the AI paper book and wakes Claude on new calls (long-running)
Any that is already running is left alone, so running `start` twice is safe. On an NSE holiday or a
weekend it does nothing (calendar_in). Paper only: none of these place orders anywhere.

launchd runs a missed 09:00 job when the Mac wakes, but a sleeping Mac at 09:15 misses the open. To wake
it automatically:  sudo pmset repeat wakeorpoweron MTWRF 08:55:00
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "trader-alerts"))
STATE = os.path.join(BASE_DIR, "trader", "state")
LABEL = "com.daytrader.dailystart"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# (name, script args, log file, start only before this IST time -- None = any time)
JOBS = [
    ("market_watch", ["scripts/market_watch.py", "--equities"], "watch.log", datetime.time(15, 0)),
    ("options_watch", ["scripts/options_watch.py", "run"], "options_watch.log", None),
    ("equity_calls", ["scripts/equity_calls.py", "run"], "equity_calls.log", None),
    ("ai_trader", ["scripts/ai_trader.py", "run"], "ai_trader.log", None),
]


def now_ist():
    return datetime.datetime.now(IST)


def log(msg):
    print("%s daily_start: %s" % (now_ist().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def is_trading_day(d):
    import calendar_in
    return bool(calendar_in.is_trading_day_in(d)[0])


def running(script):
    out = subprocess.run(["pgrep", "-f", "python[0-9.]* .*%s" % script], stdout=subprocess.PIPE,
                         universal_newlines=True)
    return [int(x) for x in out.stdout.split()]


def cmd_start(a):
    now = now_ist()
    try:
        trading = is_trading_day(now.date())
    except Exception as e:                       # fail closed: unknown calendar -> don't start
        log("can't read the NSE calendar (%s); not starting" % e)
        return 1
    if not trading and not a.force:
        log("%s is not an NSE trading day; nothing to start" % now.date())
        return 0
    os.makedirs(STATE, exist_ok=True)
    py = sys.executable
    for name, args, logname, until in JOBS:
        pids = running(args[0])
        if pids:
            log("%s already running (pid %s)" % (name, ",".join(map(str, pids))))
            continue
        if until is not None and now.time() >= until and not a.force:
            log("%s not started: past %s IST" % (name, until.strftime("%H:%M")))
            continue
        fh = open(os.path.join(STATE, logname), "a")
        p = subprocess.Popen(["caffeinate", "-i", py] + args, cwd=BASE_DIR, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        log("started %s (pid %d) -> trader/state/%s" % (name, p.pid, logname))
    return 0


def cmd_status(a):
    for name, args, logname, _ in JOBS:
        pids = running(args[0])
        print("%-14s %s" % (name, ("running pid " + ",".join(map(str, pids))) if pids else "NOT running"))
    print("launchd agent: %s" % ("installed (%s)" % PLIST if os.path.exists(PLIST) else "not installed"))
    return 0


def plist_text():
    py = sys.executable
    claude = shutil.which("claude")
    path_dirs = [os.path.dirname(py), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if claude:
        path_dirs.insert(0, os.path.dirname(claude))      # ai_trader runs `claude -p`
    seen, path = set(), []
    for d in path_dirs:
        if d not in seen:
            seen.add(d)
            path.append(d)
    days = "".join("""
        <dict><key>Weekday</key><integer>%d</integer><key>Hour</key><integer>9</integer>
              <key>Minute</key><integer>0</integer></dict>""" % d for d in range(1, 6))
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>%(label)s</string>
    <key>ProgramArguments</key>
    <array><string>%(py)s</string><string>%(script)s</string><string>start</string></array>
    <key>WorkingDirectory</key><string>%(base)s</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>%(path)s</string><key>HOME</key><string>%(home)s</string></dict>
    <key>StartCalendarInterval</key>
    <array>%(days)s
    </array>
    <key>RunAtLoad</key><true/>
    <key>AbandonProcessGroup</key><true/>
    <key>StandardOutPath</key><string>%(log)s</string>
    <key>StandardErrorPath</key><string>%(log)s</string>
</dict>
</plist>
""" % {"label": LABEL, "py": py, "script": os.path.abspath(__file__), "base": BASE_DIR, "path": ":".join(path),
       "home": os.path.expanduser("~"), "days": days, "log": os.path.join(STATE, "daily_start.log")}


def cmd_install(a):
    """Times are the Mac's local time: this machine runs on IST, so 09:00 local = 09:00 IST."""
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    uid = os.getuid()
    if os.path.exists(PLIST):
        subprocess.run(["launchctl", "bootout", "gui/%d" % uid, PLIST], stderr=subprocess.DEVNULL)
    with open(PLIST, "w") as fh:
        fh.write(plist_text())
    r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, PLIST], stderr=subprocess.PIPE,
                       universal_newlines=True)
    if r.returncode != 0:
        print("launchctl bootstrap failed: %s" % r.stderr.strip())
        return 1
    print("installed %s: runs `start` at 09:00 Mon-Fri and at login (holidays are skipped)" % PLIST)
    off = datetime.datetime.now().astimezone().utcoffset()
    if off != datetime.timedelta(hours=5, minutes=30):
        print("WARNING: this Mac's timezone is UTC%s, not IST -- 09:00 local is not 09:00 IST" % off)
    return 0


def cmd_uninstall(a):
    subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), PLIST], stderr=subprocess.DEVNULL)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("removed %s" % PLIST)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("start")
    s.add_argument("--force", action="store_true", help="start even on a non-trading day / after the cutoff")
    sub.add_parser("status")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    a = ap.parse_args(argv)
    fn = {"start": cmd_start, "status": cmd_status, "install": cmd_install, "uninstall": cmd_uninstall}.get(a.cmd)
    if fn is None:
        ap.print_help()
        return 2
    return fn(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
