#!/usr/bin/env python3
"""Upstox request pacing for each paper-stack process (2026-11-20).

data_in.UpstoxMarketDataProvider reads three opt-in env vars (defaults unchanged when unset):
  UPSTOX_MAX_RPS         per-process request-rate cap (also turns on one retry after a 429);
  UPSTOX_BATCH_WORKERS   parallel batch fetches;
  UPSTOX_HIST_CACHE_DIR  same-day cache of historical chunks that hold only past sessions.

Each process gets its own share of the API: the watcher scans hard during the session, the others are
light but overlap with it (the AI's tools, the 15:45 multi-day scan, the spread tracker). The shares add
up to well under the burst that tripped Upstox's Cloudflare edge (error 1015) on 2026-11-20.
"""

import datetime
import os
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "trader", "state", "cache", "upstox_hist")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

PROFILES = {
    "market_watch": {"UPSTOX_MAX_RPS": "5", "UPSTOX_BATCH_WORKERS": "4"},
    "full_board": {"UPSTOX_MAX_RPS": "2", "UPSTOX_BATCH_WORKERS": "2"},
    "ai_mcp": {"UPSTOX_MAX_RPS": "3", "UPSTOX_BATCH_WORKERS": "2"},
    "equity_calls": {"UPSTOX_MAX_RPS": "3", "UPSTOX_BATCH_WORKERS": "2"},
    "options_watch": {"UPSTOX_MAX_RPS": "2", "UPSTOX_BATCH_WORKERS": "1"},
}


def apply(profile, environ=None, today=None):
    """Set `profile`'s pacing in `environ` (default os.environ; an explicit value already set wins) and
    drop cache days other than today. Returns the environ."""
    env = os.environ if environ is None else environ
    for k, v in PROFILES[profile].items():
        env.setdefault(k, v)
    env.setdefault("UPSTOX_HIST_CACHE_DIR", CACHE_DIR)
    root = env.get("UPSTOX_HIST_CACHE_DIR")
    today = today or datetime.datetime.now(IST).date().isoformat()
    if root and os.path.isdir(root):
        for d in os.listdir(root):
            if d != today and len(d) == 10 and d[4] == "-":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return env
