#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check TSLA                          # opening-range breakout, 5-minute bars
    python3 entry.py check TSLA --now "2026-10-19 10:15" --offline TSLA.json
    python3 entry.py check TSLA --setup level --level 371.20    # first close above a price
    python3 entry.py check TSLA --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch TSLA --every 30 --until 15:30  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Setups (v2 plans longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 9:30, 9:35 and 9:40 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 9:30 bar carries the opening
         auction, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 10:10 bar closes at
10:15 AM) and its price is that close, to 0.01 (cent/paise). Only bars on
the interval grid count (a partial live tick, e.g. 10:49:45, is dropped) and
a bar counts only once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 15:30 ET
for US or 15:00 IST for IN; on a 1:00 p.m. NYSE early close the cutoff moves up
three hours with the close); no-data (the market is closed today or no bars).

trader/config.json (optional): "no_entries_before" (09:45 ET / 09:30 IST) and
"no_entries_after" (15:30 ET / 15:00 IST), market local time.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/TSLA?range=1d&interval=5m
  falls back to query1 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart JSON (.json or .json.gz) instead, or
  PATH/<TICKER>.json when PATH is a folder. Without --now, an offline file is
  read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

_alerts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "trader-alerts"))
if _alerts_dir not in sys.path:
    sys.path.insert(0, _alerts_dir)
_scanner_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "trader-scanner"))
if _scanner_dir not in sys.path:
    sys.path.insert(0, _scanner_dir)

# Cross-skill helpers live in ../trader-alerts and ../trader-scanner
try:
    import calendar_in  # type: ignore
    import symbols_in  # type: ignore
    import instruments  # type: ignore
    import data_in  # type: ignore
except ImportError:
    calendar_in = None
    symbols_in = None
    instruments = None
    data_in = None

try:
    import scan as scanner_mod  # type: ignore
except ImportError:
    scanner_mod = None

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/KulmeetSJ/day-trader-skill)"
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:45", "no_entries_after": "15:30"}
DEFAULTS_IN = {"no_entries_before": "09:30", "no_entries_after": "15:00"}
EPOCH = datetime.datetime(1970, 1, 1)
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
NSE_OPEN = datetime.time(9, 15)
NSE_CLOSE = datetime.time(15, 30)

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class EntryError(Exception):
    pass


def get_market_profile(a=None, sym=None, cfg=None):
    if a and getattr(a, "market", None):
        return a.market.upper()
    env = os.environ.get("BHARAT_MARKET")
    if env and env.upper() in ("IN", "INDIA"):
        return "IN"
    if cfg and isinstance(cfg, dict) and str(cfg.get("market", "")).upper() in ("IN", "INDIA"):
        return "IN"
    cfg_path = getattr(a, "config", "trader/config.json") if a else "trader/config.json"
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                c = json.load(fh)
                if c.get("market", "").upper() in ("IN", "INDIA"):
                    return "IN"
        except Exception:
            pass
    if sym:
        s = sym.upper()
        if s.endswith(".NS") or s.endswith(".BO") or s.endswith("-EQ") or "-" in s or "&" in s:
            return "IN"
    return "US"


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def to_local(utc, market="US"):
    if market == "IN":
        return utc.replace(tzinfo=None) + IST_OFFSET
    return to_eastern(utc)


def local_to_utc(local, market="US"):
    if market == "IN":
        return local.replace(tzinfo=None) - IST_OFFSET
    return eastern_to_utc(local)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text, market="US"):
    """--now -> naive UTC. "2026-10-19 10:15" (or ...T10:15, or just 10:15 for
    today) is local time; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if market == "IN":
        if t.upper().endswith(" IST"):
            t = t[:-4].strip()
        if re.match(r"^\d{1,2}:\d{2}$", t):
            t = "%s %s" % (to_local(utc_now(), market="IN").date().isoformat(), t.zfill(5))
        t = t.replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(t)
        except ValueError:
            raise EntryError('--now wants an IST time like "2026-10-19 10:15" (or ...Z for UTC)')
        if dt.tzinfo is None:
            return local_to_utc(dt, market="IN")
        return (dt - (dt.utcoffset() or datetime.timedelta(0))).replace(tzinfo=None)

    if t.upper().endswith(" ET"):
        t = t[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise EntryError('--now wants a New York time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - (dt.utcoffset() or datetime.timedelta(0))).replace(tzinfo=None)


def parse_hhmm(text, what, market="US"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    market_tz = "IST" if market == "IN" else "New York"
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants a %s time like 15:30, not %r" % (what, market_tz, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """10:10, 09:45: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    """9:45, 10:10: for sentences."""
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def trading_day(d, market="US"):
    """(is_trading_day, reason). Weekends and the market holiday list."""
    if market == "IN":
        if calendar_in:
            is_trade, _status, reason = calendar_in.is_trading_day_in(d)
            if not is_trade:
                return False, reason
            return True, ""
        if d.weekday() >= 5:
            return False, "weekend"
        return True, ""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session(d, market="US"):
    """(open_time, close_time) in local time, or None if closed all day."""
    ok, _ = trading_day(d, market=market)
    if not ok:
        return None
    if market == "IN":
        if calendar_in:
            sp = calendar_in.get_special_session(d.isoformat(), exchange="NSE")
            if sp and sp.get("open_time") and sp.get("close_time"):
                return (parse_hhmm(sp["open_time"], "special open", market="IN"),
                        parse_hhmm(sp["close_time"], "special close", market="IN"))
        return NSE_OPEN, NSE_CLOSE
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def next_open(d, market="US"):
    nd = d + datetime.timedelta(days=1)
    open_time = "09:15" if market == "IN" else "09:30"
    for _ in range(15):
        if session(nd, market=market):
            return "%s %s" % (nd.isoformat(), open_time)
        nd += datetime.timedelta(days=1)
    return None


def last_trading_session_date(local_dt, market="US"):
    """Most recent active or completed trading day for local_dt per exchange calendar."""
    if market == "IN" and calendar_in:
        return calendar_in.get_last_trading_day(local_dt, exchange="NSE")
    d = local_dt.date() if isinstance(local_dt, datetime.datetime) else local_dt
    for _ in range(30):
        if trading_day(d, market=market)[0]:
            return d
        d -= datetime.timedelta(days=1)
    return d


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    s = str(sym).upper()
    if s.endswith((".NS", ".BO")) or s.startswith("^"):
        return s
    return s.replace(".", "-")


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_symbol(sym), rng, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym, market="US"):
    if os.path.isdir(path):
        names = [
            sym + ".json", sym + ".json.gz",
            sym + "-5m.json", sym + "-5m.json.gz",
            yahoo_symbol(sym) + ".json", yahoo_symbol(sym) + ".json.gz",
        ]
        if market == "IN" and symbols_in:
            ysym = symbols_in.to_yahoo_symbol(sym)
            names.extend([ysym + ".json", ysym + ".json.gz", ysym + "-5m.json", ysym + "-5m.json.gz"])
        for name in names:
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def offline_daily_file(path, sym, market="US"):
    """Find an offline 1d daily history file for sym, or None if absent."""
    if not path:
        return None
    if os.path.isdir(path):
        names = [
            sym + "-daily.json", sym + "-daily.json.gz",
            yahoo_symbol(sym) + "-daily.json", yahoo_symbol(sym) + "-daily.json.gz",
        ]
        if market == "IN" and symbols_in:
            ysym = symbols_in.to_yahoo_symbol(sym)
            names.extend([ysym + "-daily.json", ysym + "-daily.json.gz"])
        for name in names:
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        return None
    if os.path.exists(path):
        return path
    return None


def _is_daily_chart(ch):
    """Detect whether a parsed chart JSON holds daily (1d+) bars rather than intraday bars."""
    meta = (ch or {}).get("meta") or {}
    gran = str(meta.get("dataGranularity") or "").lower()
    if gran in ("1d", "1wk", "1mo", "d", "daily"):
        return True
    if gran in ("1m", "2m", "5m", "15m", "30m", "60m", "1h"):
        return False
    bars = (ch or {}).get("bars") or []
    diffs = [
        int(bars[i]["t"]) - int(bars[i - 1]["t"])
        for i in range(1, len(bars))
        if bars[i].get("t") is not None and bars[i - 1].get("t") is not None
        and int(bars[i]["t"]) > int(bars[i - 1]["t"])
    ]
    if diffs:
        diffs_sorted = sorted(diffs)
        if diffs_sorted[len(diffs_sorted) // 2] >= 43200:
            return True
    return False


def _chart_symbol_matches(ch_sym, sym, market="US"):
    if market == "IN" and symbols_in:
        try:
            return symbols_in.normalize_symbol(ch_sym) == symbols_in.normalize_symbol(sym)
        except Exception:
            return ch_sym == yahoo_symbol(sym)
    return ch_sym == yahoo_symbol(sym)


_ORIG_YAHOO_GET = yahoo_get


def _to_ist_aware(now_utc):
    if now_utc is None:
        return None
    ist_tz = calendar_in.IST if calendar_in is not None else datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
    if getattr(now_utc, "tzinfo", None) is None:
        return now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(ist_tz)
    return now_utc.astimezone(ist_tz)


def _market_iso(utc_dt, market="US"):
    """Format naive UTC datetime into unambiguous market-local ISO-8601 timestamp."""
    if utc_dt is None:
        return None
    local = to_local(utc_dt, market=market)
    if market == "IN":
        return local.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    off_hours = eastern_offset(utc_dt)
    sign = "-" if off_hours < 0 else "+"
    return "%s%s%02d:00" % (
        local.strftime("%Y-%m-%dT%H:%M:%S"), sign, abs(int(off_hours))
    )


def _truncate_chart_as_of(ch, now_utc, interval, market="US"):
    """Truncate Indian chart bars and metadata so nothing after now_utc leaks in."""
    if ch is None or now_utc is None or market != "IN":
        return ch
    step = INTERVALS.get(interval, 300)
    cutoff_ts = ts_of(now_utc)
    bars = [
        b for b in (ch.get("bars") or [])
        if b.get("t") is not None and not (int(b["t"]) % step) and (int(b["t"]) + step <= cutoff_ts)
    ]
    ch["bars"] = bars
    dt = ch.get("data_time")
    if bars:
        latest_close = min(cutoff_ts, max(int(b["t"]) + step for b in bars))
        if dt is None or int(dt) > cutoff_ts:
            dt = latest_close
    else:
        if dt is None or int(dt) > cutoff_ts:
            dt = None
    ch["data_time"] = dt
    meta = ch.get("meta")
    if isinstance(meta, dict):
        if dt is not None:
            meta["regularMarketTime"] = dt
            last_date = to_local(utc_of(bars[-1]["t"]), market=market).date()
            day_bars = [b for b in bars if to_local(utc_of(b["t"]), market=market).date() == last_date] or [bars[-1]]
            meta["regularMarketOpen"] = day_bars[0]["o"]
            meta["regularMarketDayHigh"] = max(b["h"] for b in day_bars)
            meta["regularMarketDayLow"] = min(b["l"] for b in day_bars)
            meta["regularMarketPrice"] = day_bars[-1]["c"]
            meta["regularMarketVolume"] = sum(int(b.get("v") or 0) for b in day_bars)
        else:
            for k in (
                "regularMarketTime",
                "regularMarketPrice",
                "regularMarketDayHigh",
                "regularMarketDayLow",
                "regularMarketVolume",
                "regularMarketOpen",
                "postMarketPrice",
                "preMarketPrice",
            ):
                meta.pop(k, None)
    ch["last_data_timestamp"] = _market_iso(utc_of(dt), market=market) if dt is not None else None
    ch["simulated_at"] = _market_iso(now_utc, market=market)
    return ch


def _truncate_daily_chart_as_of(dch, now_utc, market="US"):
    """Truncate daily bars and metadata so no future or unfinished today daily bar leaks past now_utc."""
    if dch is None or now_utc is None:
        return dch
    cutoff_ts = ts_of(now_utc)
    local_now = to_local(now_utc, market=market)
    today_d = local_now.date()
    s_today = session(today_d, market=market)
    today_done = (s_today is None) or (local_now.time() >= s_today[1])
    kept = []
    for b in (dch.get("bars") or []):
        t = b.get("t")
        c = b.get("c")
        if t is None or c is None:
            continue
        ts = int(t)
        if ts > cutoff_ts:
            continue
        bd = to_local(utc_of(ts), market=market).date()
        if bd > today_d or (bd == today_d and not today_done):
            continue
        kept.append(b)
    dch["bars"] = kept
    dt = dch.get("data_time")
    if dt is not None and int(dt) > cutoff_ts:
        dt = max((int(b["t"]) for b in kept), default=None)
    elif dt is None and kept:
        dt = int(kept[-1]["t"])
    dch["data_time"] = dt
    meta = dch.get("meta")
    if isinstance(meta, dict):
        if kept and dt is not None:
            meta["regularMarketTime"] = dt
            meta["regularMarketPrice"] = kept[-1]["c"]
        else:
            for k in ("regularMarketTime", "regularMarketPrice", "regularMarketDayHigh",
                      "regularMarketDayLow", "regularMarketVolume", "regularMarketOpen"):
                meta.pop(k, None)
    dch["last_data_timestamp"] = _market_iso(utc_of(dt), market=market) if dt is not None else None
    dch["simulated_at"] = _market_iso(now_utc, market=market)
    return dch


def _parse_daily_file(path, sym):
    text = read_text(path)
    try:
        raw = json.loads(text)
    except (ValueError, TypeError) as e:
        raise EntryError("could not parse daily file %s: %s" % (path, e))
    if isinstance(raw, dict) and "chart" in raw:
        dch = parse_chart(text, sym)
    elif isinstance(raw, dict) and "bars" in raw:
        dch = dict(raw)
        dch.setdefault("symbol", sym)
        dch.setdefault("meta", {})
    else:
        raise EntryError("daily file %s has unexpected format" % path)
    dch["is_offline"] = True
    dch["provider"] = "offline"
    dch["source"] = "offline"
    dch["data_tier"] = "FIXTURE"
    dch["is_live"] = False
    dch["is_live_capable"] = False
    dch.setdefault("provider_data_status", "HISTORICAL")
    return dch


def _load_daily_chart(sym, offline=None, daily_path=None, market="US", cfg=None, now=None):
    """Return (daily_chart_dict, error_message) with look-ahead cutoff applied."""
    sym = sym.upper()
    dch = None
    if daily_path:
        dp = offline_daily_file(daily_path, sym, market=market)
        if not dp:
            return None, "no daily file for %s in %s" % (sym, daily_path)
        try:
            dch = _parse_daily_file(dp, sym)
        except EntryError as e:
            return None, str(e)
    elif offline:
        dp = offline_daily_file(offline, sym, market=market)
        if not dp:
            return None, "no offline daily file for %s in %s (want %s-daily.json)" % (sym, offline, sym)
        try:
            dch = _parse_daily_file(dp, sym)
        except EntryError as e:
            return None, str(e)
    elif market == "IN" and data_in is not None:
        try:
            prov = data_in.get_provider(config=cfg)
        except Exception as e:
            return None, "Market data provider error: %s" % e
        use_legacy_mock = (prov.provider_name == "yahoo" and yahoo_get is not _ORIG_YAHOO_GET)
        if use_legacy_mock:
            fetch_sym = symbols_in.to_yahoo_symbol(sym) if symbols_in else yahoo_symbol(sym)
            try:
                text = yahoo_get(chart_path(fetch_sym, "3mo", "1d"))
                dch = parse_chart(text, sym)
            except EntryError as e:
                return None, str(e)
            dch["is_offline"] = False
            dch["provider"] = "yahoo"
            dch["source"] = "yahoo"
            dch["data_tier"] = "DELAYED"
            dch["is_live"] = False
            dch["is_live_capable"] = False
        else:
            exch = (cfg or {}).get("exchange") or "NSE"
            try:
                payload = prov.get_historical_data(
                    sym,
                    range_str="3mo",
                    interval="1d",
                    exchange=exch,
                    now=_to_ist_aware(now),
                )
            except Exception as e:
                return None, str(e)
            if isinstance(payload, str):
                dch = parse_chart(payload, sym)
                dch["provider"] = prov.provider_name
                dch["source"] = prov.provider_name
                dch["data_tier"] = "DELAYED" if prov.provider_name == "yahoo" else "HISTORICAL"
                dch["is_live"] = False
            elif isinstance(payload, dict) and "chart" in payload:
                dch = parse_chart(json.dumps(payload), sym)
                dch["provider"] = prov.provider_name
                dch["source"] = prov.provider_name
                dch["data_tier"] = "DELAYED" if prov.provider_name == "yahoo" else "HISTORICAL"
                dch["is_live"] = False
            else:
                dch = data_in.to_entry_chart(payload)
                if isinstance(payload, dict) and payload.get("fallback_from"):
                    dch["fallback_from"] = payload.get("fallback_from")
            eff_d_prov = str(dch.get("provider") or prov.provider_name).lower()
            if eff_d_prov == "yahoo":
                dch["data_tier"] = "DELAYED"
                dch["is_live"] = False
                dch["is_live_capable"] = False
            else:
                dch["is_live_capable"] = bool(
                    prov.supports(data_in.CAPABILITY_LIVE_QUOTES)
                    and eff_d_prov not in ("yahoo", "offline", "simulated")
                )
    else:
        fetch_sym = symbols_in.to_yahoo_symbol(sym) if (market == "IN" and symbols_in) else yahoo_symbol(sym)
        try:
            text = yahoo_get(chart_path(fetch_sym, "3mo", "1d"))
            dch = parse_chart(text, sym)
        except EntryError as e:
            return None, str(e)
        dch["is_offline"] = False
        dch["provider"] = "yahoo"
        dch["source"] = "yahoo"
        dch["data_tier"] = "DELAYED"
        dch["is_live"] = False
        dch["is_live_capable"] = False

    if dch and not _chart_symbol_matches(dch.get("symbol") or sym, sym, market=market):
        return None, "the daily chart holds %s, not %s" % (dch.get("symbol"), sym)
    if dch and now is not None:
        _truncate_daily_chart_as_of(dch, now, market=market)
    return dch, None


def get_chart(sym, interval, day=None, offline=None, today=None, market="US", cfg=None, now=None,
              setup="orb", daily=None):
    """Bars for one ticker, from active Indian provider (or Yahoo) or a saved offline file."""
    sym = sym.upper()
    daily_only_file = False
    if offline:
        try:
            f = offline_file(offline, sym, market=market)
        except EntryError:
            if setup == "breakout_20d" and os.path.isdir(offline):
                df = offline_daily_file(offline, sym, market=market)
                if df:
                    f = df
                    daily_only_file = True
                else:
                    raise
            else:
                raise
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
        ch = parse_chart(text, sym)
        ch["is_offline"] = True
        ch["provider"] = "offline"
        ch["source"] = "offline"
        ch["data_tier"] = "FIXTURE"
        ch["is_live"] = False
        ch["is_live_capable"] = False
        if setup == "breakout_20d" and (daily_only_file or _is_daily_chart(ch)):
            dch = dict(ch)
            dch["bars"] = list(ch.get("bars") or [])
            dch["meta"] = dict(ch.get("meta") or {})
            if now is not None:
                _truncate_daily_chart_as_of(dch, now, market=market)
            ch["daily_bars"] = dch["bars"]
            ch["daily_meta"] = dch.get("meta")
            ch["daily_provider"] = "offline"
            ch["daily_data_tier"] = "FIXTURE"
            ch["daily_data_status"] = "HISTORICAL"
            ch["daily_is_live"] = False
            ch["daily_is_simulated"] = False
            ch["daily_last_data_timestamp"] = dch.get("last_data_timestamp")
            ch["daily_error"] = None
            ch["bars"] = []
            ch["data_time"] = None
            ch["last_data_timestamp"] = dch.get("last_data_timestamp")
            if now is not None:
                ch["simulated_at"] = _market_iso(now, market=market)
            if not _chart_symbol_matches(ch["symbol"], sym, market=market):
                raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
            return ch
        if market == "IN" and now is not None:
            _truncate_chart_as_of(ch, now, interval, market=market)
    elif market == "IN" and data_in is not None:
        try:
            prov = data_in.get_provider(config=cfg)
        except Exception as e:
            raise EntryError("Market data provider error: %s" % e)
        use_legacy_mock = (prov.provider_name == "yahoo" and yahoo_get is not _ORIG_YAHOO_GET)
        today = today or to_local(utc_now(), market=market).date()
        if prov.provider_name == "yahoo":
            rng = chart_range(day or today, today, interval)
        else:
            back = (today - (day or today)).days
            rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if use_legacy_mock:
            fetch_sym = symbols_in.to_yahoo_symbol(sym) if symbols_in else yahoo_symbol(sym)
            text = yahoo_get(chart_path(fetch_sym, rng, interval))
            ch = parse_chart(text, sym)
            ch["is_offline"] = False
            ch["provider"] = "yahoo"
            ch["source"] = "yahoo"
            ch["data_tier"] = "DELAYED"
            ch["is_live"] = False
            ch["is_live_capable"] = False
            if now is not None:
                _truncate_chart_as_of(ch, now, interval, market=market)
        else:
            exch = (cfg or {}).get("exchange") or "NSE"
            try:
                payload = prov.get_intraday_data(
                    sym,
                    interval=interval,
                    range_str=rng,
                    exchange=exch,
                    now=_to_ist_aware(now),
                )
            except Exception as e:
                if setup == "breakout_20d":
                    ch = {
                        "symbol": sym,
                        "meta": {},
                        "bars": [],
                        "prev_close": None,
                        "data_time": None,
                        "is_offline": (prov.provider_name == "offline"),
                        "provider": prov.provider_name,
                        "source": prov.provider_name,
                        "data_tier": "DELAYED" if prov.provider_name == "yahoo" else "HISTORICAL",
                        "provider_data_status": "UNAVAILABLE",
                        "is_live": False,
                        "is_live_capable": False,
                        "is_simulated": (prov.provider_name == "simulated"),
                        "intraday_error": str(e),
                    }
                    payload = None
                else:
                    raise EntryError(str(e))
            if payload is not None:
                ch = data_in.to_entry_chart(payload)
                if isinstance(payload, dict) and payload.get("fallback_from"):
                    ch["fallback_from"] = payload.get("fallback_from")
                eff_c_prov = str(ch.get("provider") or prov.provider_name).lower()
                ch["is_live_capable"] = bool(
                    prov.supports(data_in.CAPABILITY_LIVE_QUOTES)
                    and eff_c_prov not in ("yahoo", "offline", "simulated")
                )
    else:
        today = today or to_local(utc_now(), market=market).date()
        fetch_sym = symbols_in.to_yahoo_symbol(sym) if (market == "IN" and symbols_in) else yahoo_symbol(sym)
        text = yahoo_get(chart_path(fetch_sym, chart_range(day or today, today, interval), interval))
        ch = parse_chart(text, sym)
        ch["is_offline"] = False
        ch["provider"] = "yahoo"
        ch["source"] = "yahoo"
        ch["data_tier"] = "DELAYED"
        ch["is_live"] = False
        ch["is_live_capable"] = False
        if market == "IN" and now is not None:
            _truncate_chart_as_of(ch, now, interval, market=market)

    if not _chart_symbol_matches(ch["symbol"], sym, market=market):
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))

    if setup == "breakout_20d":
        dch, derr = _load_daily_chart(sym, offline=offline, daily_path=daily, market=market, cfg=cfg, now=now)
        if dch is not None:
            ch["daily_bars"] = dch.get("bars") or []
            ch["daily_meta"] = dch.get("meta") or {}
            ch["daily_provider"] = dch.get("provider")
            ch["daily_data_tier"] = dch.get("data_tier")
            ch["daily_data_status"] = dch.get("provider_data_status")
            ch["daily_is_live"] = bool(dch.get("is_live", False))
            ch["daily_is_simulated"] = bool(dch.get("is_simulated", False))
            ch["daily_last_data_timestamp"] = dch.get("last_data_timestamp")
            ch["daily_error"] = None
        else:
            ch["daily_bars"] = []
            ch["daily_error"] = derr
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path, market="US"):
    cfg = dict(DEFAULTS_IN if market == "IN" else DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
        if str(doc.get("market", "")).upper() == "IN":
            cfg["market"] = "IN"
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k, market=market)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def early_cutoff(day, t, close_dt):
    """A cutoff on an early-close day moves up with the close: 15:30 (30 minutes
    before a 16:00 close) becomes 12:30 before a 1:00 p.m. close. A cutoff that
    is already before the early close stays."""
    at = datetime.datetime.combine(day, t)
    if at < close_dt:
        return at
    moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
    return moved if moved.time() > OPEN else close_dt


def rsi_val(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def _tick_step(market="US"):
    return 0.05 if market == "IN" else 0.01


def _floor_tick(price, tick=0.05):
    from decimal import Decimal, ROUND_FLOOR
    d_price = Decimal(str(round(float(price), 4)))
    d_tick = Decimal(str(tick))
    floored = (d_price / d_tick).quantize(Decimal("1"), rounding=ROUND_FLOOR) * d_tick
    return round(float(floored), 2) + 0.0


def _compute_breakout_invalidation(breakout_level, trig_low, trig_close, market="US"):
    """Structural invalidation stop below broken level/trigger bar low, strictly below entry."""
    tick = _tick_step(market)
    anchor = float(breakout_level)
    if trig_low is not None:
        anchor = min(anchor, float(trig_low))
    inv = _floor_tick(anchor, tick=tick)
    entry_r = r2(trig_close)
    if entry_r is not None and inv >= entry_r:
        inv = _floor_tick(entry_r - tick, tick=tick)
    if inv <= 0:
        return None
    return inv


def _expected_prev_trading_day(eval_day, market="US"):
    d = eval_day - datetime.timedelta(days=1)
    for _ in range(30):
        ok_d, _ = trading_day(d, market=market)
        if ok_d:
            return d
        d = d - datetime.timedelta(days=1)
    return None


def _count_missed_trading_days(latest_date, eval_day, market="US"):
    if latest_date is None or latest_date >= eval_day:
        return 0
    missed = 0
    cur = latest_date + datetime.timedelta(days=1)
    steps = 0
    while cur < eval_day and steps < 90:
        ok_d, _ = trading_day(cur, market=market)
        if ok_d:
            missed += 1
        cur = cur + datetime.timedelta(days=1)
        steps += 1
    return missed


def _daily_closes_fallback(series, as_of_utc, market="US"):
    """Fallback daily_closes identical to scan.daily_closes if scan.py is unavailable."""
    now = to_local(as_of_utc, market=market)
    today_d = now.date()
    s = session(today_d, market=market)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series.get("t") or [], series.get("c") or []):
        if ts is None or c is None:
            continue
        d = to_local(utc_of(ts), market=market).date()
        if d > today_d or (d == today_d and not today_done):
            continue
        out[d] = float(c)
    return sorted(out.items())


def _find_20d_fallback(closes):
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    tags = ["breakout_20d"] if last > hi20 else []
    return tags, {"high20": hi20}


def _analyze_breakout_20d_baseline(chart, now_utc, eval_day, market="US"):
    """Compute prior-session 20d closing high entering eval_day, reusing trader-scanner logic."""
    min_daily = getattr(scanner_mod, "MIN_DAILY_BARS", 30) if scanner_mod else 30
    daily_err = chart.get("daily_error")
    raw_daily = chart.get("daily_bars")
    if daily_err and not raw_daily:
        return {"ok": False, "err_kind": "unavailable", "reason": "Insufficient historical daily data: %s" % daily_err,
                "breakout_level": None, "ref_invalidation": None, "closes_prior": [],
                "scanner_matched": False, "daily_close_date": None, "daily_close": None,
                "prior_high20": None, "missed_sessions": 0}
    raw_daily = raw_daily or []
    now_ts = ts_of(now_utc)
    ts_list = []
    c_list = []
    for b in raw_daily:
        if b.get("t") is None or b.get("c") is None:
            continue
        ts_val = int(b["t"])
        if ts_val <= now_ts:
            ts_list.append(ts_val)
            c_list.append(float(b["c"]))
    before_eval_utc = local_to_utc(datetime.datetime.combine(eval_day, datetime.time(0, 0)), market=market) - datetime.timedelta(seconds=1)
    as_of_daily = min(now_utc, before_eval_utc)
    series = {"t": ts_list, "c": c_list}
    if scanner_mod is not None:
        prior_bars = scanner_mod.daily_closes(series, as_of_daily, market=market)
    else:
        prior_bars = _daily_closes_fallback(series, as_of_daily, market=market)

    if not prior_bars:
        return {"ok": False, "err_kind": "insufficient",
                "reason": "Insufficient historical daily data before %s: need %d completed daily closes for breakout_20d, have 0" % (
                    eval_day.isoformat(), min_daily),
                "breakout_level": None, "ref_invalidation": None, "closes_prior": [],
                "scanner_matched": False, "daily_close_date": None, "daily_close": None,
                "prior_high20": None, "missed_sessions": 0}

    latest_daily_date = prior_bars[-1][0]
    latest_daily_close = prior_bars[-1][1]
    if len(prior_bars) < min_daily:
        return {"ok": False, "err_kind": "insufficient",
                "reason": "Insufficient historical daily data before %s: need %d completed daily closes for breakout_20d, have %d" % (
                    eval_day.isoformat(), min_daily, len(prior_bars)),
                "breakout_level": None, "ref_invalidation": None, "closes_prior": [c for _, c in prior_bars],
                "scanner_matched": False, "daily_close_date": latest_daily_date.isoformat(),
                "daily_close": r2(latest_daily_close), "prior_high20": None, "missed_sessions": 0}

    closes_prior = [c for _, c in prior_bars]
    if scanner_mod is not None:
        prior_tags, prior_nums = scanner_mod.find_setups(closes_prior)
        _, today_nums = scanner_mod.find_setups(closes_prior + [closes_prior[-1]])
    else:
        prior_tags, prior_nums = _find_20d_fallback(closes_prior)
        _, today_nums = _find_20d_fallback(closes_prior + [closes_prior[-1]])

    breakout_level = float(today_nums["high20"])
    tick = _tick_step(market)
    ref_inv = _floor_tick(breakout_level - tick, tick=tick)
    if ref_inv <= 0:
        ref_inv = r2(breakout_level)

    expected_prev = _expected_prev_trading_day(eval_day, market=market)
    missed = _count_missed_trading_days(latest_daily_date, eval_day, market=market)
    daily_prov_stale = (str(chart.get("daily_data_status") or "").upper() == "STALE")
    if (expected_prev is not None and latest_daily_date < expected_prev) or daily_prov_stale:
        return {"ok": False, "err_kind": "stale",
                "reason": "Stale daily history for breakout_20d: latest completed daily close is %s (%d trading session(s) missing before %s)" % (
                    latest_daily_date.isoformat(), max(1, missed), eval_day.isoformat()),
                "breakout_level": breakout_level, "ref_invalidation": ref_inv,
                "closes_prior": closes_prior,
                "scanner_matched": ("breakout_20d" in prior_tags),
                "daily_close_date": latest_daily_date.isoformat(),
                "daily_close": r2(latest_daily_close),
                "prior_high20": r2(prior_nums["high20"]),
                "missed_sessions": missed}

    return {"ok": True, "err_kind": None, "reason": "",
            "breakout_level": breakout_level, "ref_invalidation": ref_inv,
            "closes_prior": closes_prior,
            "scanner_matched": ("breakout_20d" in prior_tags),
            "daily_close_date": latest_daily_date.isoformat(),
            "daily_close": r2(latest_daily_close),
            "prior_high20": r2(prior_nums["high20"]),
            "missed_sessions": 0}


def _bar_confirms_breakout_20d(closes_prior, close_price, breakout_level):
    """Confirm an intraday bar close using scan.find_setups' breakout_20d rule."""
    if scanner_mod is not None and len(closes_prior) >= 21:
        tags, _ = scanner_mod.find_setups(closes_prior + [float(close_price)])
        return "breakout_20d" in tags
    return float(close_price) > float(breakout_level)


def _breakout_signal_freshness(status, data_tier, data_status, is_live_data, is_fixture,
                               is_simulated, is_historical, replay_full_session,
                               eval_day, day, minutes_since=None, phase=None):
    """Return explicit signal_freshness label for breakout_20d evaluations."""
    if status == "no-data":
        return "stale" if data_status == "stale" else "unavailable"
    if data_status == "stale":
        return "stale"
    if replay_full_session or eval_day < day or data_tier == "HISTORICAL":
        if status == "triggered":
            return "historical"
        return "expired" if status == "expired" else "historical"
    if status == "triggered" and minutes_since is not None and minutes_since > 30:
        return "stale"
    if status == "expired":
        return "fixture" if is_fixture else ("simulated" if is_simulated else "expired")
    if phase == "pre-open":
        return "fixture" if is_fixture else ("simulated" if is_simulated else "pending")
    if is_fixture:
        return "fixture"
    if is_simulated:
        return "simulated"
    if data_tier == "CURRENT" and is_live_data:
        return "current"
    if data_tier == "DELAYED":
        return "delayed"
    return str(data_status or "delayed").lower()


def _classify_market_data(chart, market, day, eval_day, local, open_dt, close_dt, now_ts, dt,
                          step, by_slot, ok, session_is_open, is_historical, replay_full_session,
                          is_fixture):
    """Determine data_tier, data_status, and is_live_data from provider capability and freshness."""
    is_simulated = bool(chart.get("is_simulated", False))
    provider_name = str(chart.get("provider") or ("offline" if is_fixture else "yahoo"))
    prov_lower = provider_name.lower()
    prov_status = str(chart.get("provider_data_status") or "").upper()
    prov_tier = str(chart.get("data_tier") or "").upper()
    provider_live_capable = (
        bool(chart.get("is_live_capable"))
        if "is_live_capable" in chart
        else (prov_lower not in ("yahoo", "offline", "simulated") and bool(chart.get("is_live")))
    ) and prov_lower not in ("yahoo", "offline", "simulated")
    provider_claims_live = (
        provider_live_capable
        and not is_fixture
        and not is_simulated
        and (chart.get("is_live") is not False)
        and prov_status in ("", "CURRENT")
        and prov_tier in ("", "CURRENT", "REAL_TIME")
    )

    dt_day = to_local(utc_of(dt), market=market).date() if dt is not None else None
    lag_sec = (now_ts - int(dt)) if dt is not None else None
    min_lag = 0 if market == "IN" else -5
    dt_is_fresh = (dt is not None and dt_day == day and lag_sec is not None and min_lag <= lag_sec <= 120)

    received_bars = [b for b in by_slot.values() if b["t"] <= now_ts]
    if eval_day == day and local >= open_dt + datetime.timedelta(seconds=step):
        if received_bars:
            latest_bar_end = max(b["t"] + step for b in received_bars)
            bars_lag = now_ts - latest_bar_end
            bars_are_fresh = bars_lag <= (step + 120)
        else:
            bars_lag = None
            bars_are_fresh = False
    else:
        bars_lag = None
        bars_are_fresh = True

    if is_fixture:
        return ("FIXTURE",
                "stale" if ((day - eval_day).days >= 1 or not session_is_open or prov_status == "STALE") else "fixture",
                False, provider_name, is_simulated)
    if is_simulated:
        return "SIMULATED", "simulated", False, provider_name, True
    if replay_full_session or eval_day < day or not ok:
        status = "stale" if ((day - eval_day).days >= 1 or prov_status == "STALE") else "delayed"
        return "HISTORICAL", status, False, provider_name, False
    if session_is_open:
        is_live_data = bool(provider_claims_live and dt_is_fresh and bars_are_fresh)
        if is_live_data:
            return "CURRENT", "current", True, provider_name, False
        stale_by_dt = (dt_day is not None and (day - dt_day).days >= 1) or (lag_sec is not None and lag_sec > 1800)
        stale_by_bars = (bars_lag is not None and bars_lag > 1800)
        if prov_status == "STALE" or stale_by_dt or stale_by_bars:
            return "DELAYED", "stale", False, provider_name, False
        return "DELAYED", "delayed", False, provider_name, False
    if local < open_dt:
        is_live_data = bool(provider_claims_live and dt_is_fresh)
        if is_live_data:
            return "CURRENT", "current", True, provider_name, False
        prior_dt = (dt_day is not None and (day - dt_day).days >= 1)
        return ("HISTORICAL" if prior_dt else "DELAYED",
                "stale" if (prior_dt or prov_status == "STALE") else "delayed",
                False, provider_name, False)
    prior_dt = (dt_day is not None and (day - dt_day).days >= 1)
    return ("HISTORICAL",
            "stale" if (prior_dt or prov_status == "STALE") else "delayed",
            False, provider_name, False)


def _mark_no_data(res, is_fixture, is_simulated, past_bar_dates, session_is_open):
    """Ensure missing/unavailable bar exits never claim live data or a live signal."""
    res["is_market_open"] = bool(session_is_open)
    res["is_live_data"] = False
    res["is_live_signal"] = False
    res["data_status"] = "stale" if past_bar_dates else ("simulated" if is_simulated else "unavailable")
    res["data_tier"] = "FIXTURE" if is_fixture else ("SIMULATED" if is_simulated else "HISTORICAL")
    return res


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, market=None, trading_style="intraday",
             option_strategy=None, strike_selection="ATM", simulated=False):
    """Pure function: the setup's status at now_utc from one session's bars."""
    market = market or chart.get("market") or "US"
    cfg = cfg or dict(DEFAULTS_IN if market == "IN" else DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    valid_setups = ("orb", "level", "breakout_20d", "ma_crossover", "trend", "rsi_momentum", "options_orb")
    if setup not in valid_setups:
        raise EntryError("--setup is orb, level, breakout_20d, ma_crossover, trend, rsi_momentum, or options_orb")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    local = to_local(now_utc, market=market)
    day = local.date()
    curr = "\u20b9" if market == "IN" else "$"
    exchange = "NSE" if market == "IN" else "NYSE"
    is_clock_override = bool(simulated or chart.get("clock_override"))
    is_sim_run = bool(simulated or chart.get("clock_override") or chart.get("simulated_at"))
    res = {"symbol": chart["symbol"], "setup": setup, "side": "buy", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "trading_style": trading_style,
           "prev_close": chart.get("prev_close"), "calendar_known": day.year in CALENDAR_YEARS if market == "US" else True,
           "market": market, "currency": "INR" if market == "IN" else "USD", "_bars": []}
    if is_clock_override:
        res["clock_override"] = True
    if is_sim_run:
        res["simulated_at"] = _market_iso(now_utc, market=market)
    now_ts = ts_of(now_utc)
    raw_bars = chart.get("bars", [])
    if market == "IN":
        chart_bars = [b for b in raw_bars if b.get("t") is not None and (int(b["t"]) + step <= now_ts)]
        permitted_dt = max(
            (min(now_ts, int(b["t"]) + step) for b in chart_bars if not (int(b["t"]) % step)),
            default=None,
        )
        dt = chart.get("data_time")
        if dt is not None and int(dt) > now_ts:
            dt = permitted_dt
        elif dt is None:
            dt = permitted_dt
    else:
        chart_bars = raw_bars
        dt = chart.get("data_time")
        permitted_dt = max(
            (
                min(now_ts, int(b["t"]) + step)
                for b in chart_bars
                if b.get("t") is not None and not (int(b["t"]) % step) and (int(b["t"]) + step <= now_ts)
            ),
            default=None,
        )
    res["data_time"] = to_local(utc_of(dt), market=market).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    if market == "IN" or is_sim_run:
        lts_ts = permitted_dt if permitted_dt is not None else (int(dt) if dt is not None and int(dt) <= now_ts else None)
    else:
        lts_ts = dt
    last_ts_iso = _market_iso(utc_of(lts_ts), market=market) if lts_ts is not None else None
    ok, why = trading_day(day, market=market)
    valid_bar_dates = sorted(list(set(to_local(utc_of(b["t"]), market=market).date()
                                      for b in chart_bars if not (b["t"] % step))))
    past_bar_dates = [d for d in valid_bar_dates if d <= day]
    is_fixture = bool(chart.get("is_offline"))
    last_td = last_trading_session_date(local, market=market)

    session_is_open = False
    is_historical = False
    replay_full_session = False
    eval_day = day

    s_today = session(day, market=market) if ok else None
    if ok and s_today is not None:
        o, c = s_today
        open_dt = datetime.datetime.combine(day, o)
        close_dt = datetime.datetime.combine(day, c)
        if open_dt <= local < close_dt:
            session_is_open = True
        elif local < open_dt:
            session_is_open = False
        else:
            is_historical = (market == "IN")
            replay_full_session = (market == "IN")
    else:
        if market == "IN" and past_bar_dates:
            eval_day = past_bar_dates[-1]
            is_historical = True
            replay_full_session = True
        else:
            res.update(status="no-data", phase="closed",
                       reason=("the %s is closed today (%s); last trading session was %s"
                               + (" and insufficient historical bars on or before %s (%s) were found in chart"
                                  % (day.isoformat(), res["now"]) if market == "IN" else ""))
                              % (exchange, why, last_td.isoformat()) if market == "IN"
                              else "the %s is closed today (%s)" % (exchange, why),
                       next_open=next_open(day, market=market),
                       is_market_open=False,
                       is_live_data=False,
                       is_live_signal=False,
                       is_historical_analysis=(market == "IN" or is_fixture),
                       is_fixture=is_fixture,
                       last_trading_session=last_td.isoformat(),
                       candle_session=None,
                       last_data_timestamp=last_ts_iso,
                       data_status="stale" if is_fixture else "unavailable",
                       data_tier="FIXTURE" if is_fixture else "HISTORICAL",
                       provider=chart.get("provider", "offline" if is_fixture else "yahoo"),
                       is_simulated=bool(chart.get("is_simulated", False)))
            res["_market_session_open"] = False
            res["_replay_full_session"] = True
            if setup == "breakout_20d":
                b20_ref = _analyze_breakout_20d_baseline(chart, now_utc, last_td, market=market)
                res["breakout_level"] = r2(b20_ref["breakout_level"]) if b20_ref.get("breakout_level") is not None else None
                res["level"] = res["breakout_level"]
                res["confirmation_price"] = None
                res["timestamp"] = None
                res["invalidation_level"] = b20_ref.get("ref_invalidation") if b20_ref.get("breakout_level") is not None else None
                res["scanner_breakout_matched"] = bool(b20_ref.get("scanner_matched"))
                if b20_ref.get("daily_close_date") is not None:
                    res["daily_close_date"] = b20_ref["daily_close_date"]
                if b20_ref.get("daily_close") is not None:
                    res["daily_close"] = b20_ref["daily_close"]
                res["signal_freshness"] = "stale" if (b20_ref.get("err_kind") == "stale" or is_fixture) else "unavailable"
            return res

    if eval_day != day:
        s_eval = session(eval_day, market=market)
        if s_eval:
            o, c = s_eval
            open_dt = datetime.datetime.combine(eval_day, o)
            close_dt = datetime.datetime.combine(eval_day, c)
        else:
            open_dt = datetime.datetime.combine(eval_day, NSE_OPEN if market == "IN" else OPEN)
            close_dt = datetime.datetime.combine(eval_day, NSE_CLOSE if market == "IN" else CLOSE)

    early = c != (NSE_CLOSE if market == "IN" else CLOSE)
    before = datetime.datetime.combine(eval_day, parse_hhmm(cfg["no_entries_before"], "no_entries_before", market=market))
    if str(trading_style).lower() == "positional":
        cutoff = close_dt - datetime.timedelta(minutes=5)
    else:
        cutoff = early_cutoff(eval_day, parse_hhmm(cfg["no_entries_after"], "no_entries_after", market=market), close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), early_close=NYSE_EARLY_CLOSE.get(eval_day.isoformat()) if market == "US" else None,
               no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart_bars:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        et = to_local(utc_of(b["t"]), market=market)
        if et.date() != eval_day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)

    data_tier, data_status, is_live_data, provider_name, is_simulated = _classify_market_data(
        chart, market, day, eval_day, local, open_dt, close_dt, now_ts, dt,
        step, by_slot, ok, session_is_open, is_historical, replay_full_session, is_fixture
    )
    if is_clock_override:
        # --now overrides the application clock: historical/test-clock signals must never
        # present as CURRENT live data or current actionable entry recommendations.
        if data_tier == "CURRENT":
            data_tier = "HISTORICAL"
            data_status = "stale" if (day - eval_day).days >= 1 else "historical"
        is_live_data = False
    res["date"] = eval_day.isoformat()
    res["is_market_open"] = bool(session_is_open)
    res["is_live_data"] = bool(is_live_data)
    res["is_live_signal"] = False
    res["is_historical_analysis"] = bool(
        is_historical or is_fixture or (is_clock_override and data_tier == "HISTORICAL")
    )
    res["is_fixture"] = is_fixture
    res["data_tier"] = data_tier
    res["last_trading_session"] = last_td.isoformat()
    res["candle_session"] = eval_day.isoformat()
    res["last_data_timestamp"] = last_ts_iso
    res["data_status"] = data_status
    res["is_simulated"] = is_simulated
    res["provider"] = provider_name
    res["_market_session_open"] = bool(session_is_open)
    res["_replay_full_session"] = replay_full_session

    b20 = None
    if setup == "breakout_20d":
        b20 = _analyze_breakout_20d_baseline(chart, now_utc, eval_day, market=market)
        res["breakout_level"] = r2(b20["breakout_level"]) if b20.get("breakout_level") is not None else None
        res["level"] = res["breakout_level"]
        res["confirmation_price"] = None
        res["timestamp"] = None
        res["invalidation_level"] = b20.get("ref_invalidation") if b20.get("breakout_level") is not None else None
        res["scanner_breakout_matched"] = bool(b20.get("scanner_matched"))
        if b20.get("daily_close_date") is not None:
            res["daily_close_date"] = b20["daily_close_date"]
        if b20.get("daily_close") is not None:
            res["daily_close"] = b20["daily_close"]
        if b20.get("prior_high20") is not None:
            res["prior_20d_high"] = b20["prior_high20"]
        if chart.get("intraday_error") and not by_slot:
            res.update(status="no-data", phase="open" if session_is_open else "closed",
                       reason="Insufficient intraday data for breakout_20d confirmation: %s" % chart["intraday_error"])
            _mark_no_data(res, is_fixture, is_simulated, past_bar_dates, session_is_open)
            res["signal_freshness"] = "unavailable"
            return res
        if not b20["ok"]:
            if b20["err_kind"] == "stale":
                res.update(status="no-data", phase="open" if session_is_open else "closed", reason=b20["reason"])
                res["is_market_open"] = bool(session_is_open)
                res["is_live_data"] = False
                res["is_live_signal"] = False
                res["data_status"] = "stale"
                res["data_tier"] = "FIXTURE" if is_fixture else ("SIMULATED" if is_simulated else (
                    "HISTORICAL" if (replay_full_session or eval_day < day or data_tier == "HISTORICAL") else "DELAYED"
                ))
                res["signal_freshness"] = "stale"
                return res
            res.update(status="no-data", phase="open" if session_is_open else "closed", reason=b20["reason"])
            res["breakout_level"] = None
            res["level"] = None
            res["invalidation_level"] = None
            _mark_no_data(res, is_fixture, is_simulated, past_bar_dates, session_is_open)
            res["signal_freshness"] = "unavailable"
            return res

    bars, pending = [], None
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if replay_full_session or end_ts <= now_ts:
            if replay_full_session or dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (to_local(utc_of(dt), market=market).strftime("%H:%M:%S"),
                                                                       short(b["et"])))
        elif market != "IN" and b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if not replay_full_session and local < open_dt:
        open_label = "9:15" if market == "IN" else "9:30"
        if setup == "breakout_20d" and b20 is not None:
            res["signal_freshness"] = _breakout_signal_freshness(
                "waiting", data_tier, data_status, is_live_data, is_fixture, is_simulated,
                is_historical, replay_full_session, eval_day, day, phase="pre-open"
            )
            if b20.get("scanner_matched"):
                res.update(phase="pre-open", reason=(
                    "the %s opens at %s; prior daily close %s%.2f (%s) matched breakout_20d - "
                    "awaiting completed intraday bar close above %s%.2f to confirm"
                    % (exchange, open_label, curr, b20["daily_close"], b20["daily_close_date"], curr, b20["breakout_level"])
                ))
            else:
                res.update(phase="pre-open", reason=(
                    "the %s opens at %s; awaiting completed intraday bar close above 20-day breakout level %s%.2f"
                    % (exchange, open_label, curr, b20["breakout_level"])
                ))
            return res
        res.update(phase="pre-open", reason="the %s opens at %s%s" % (
            exchange, open_label, " and the %d-minute range is set at %s" % (or_minutes, short(or_end)) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": (replay_full_session or local >= or_end), "avg_volume": int(round(avg_vol))}
    if setup in ("orb", "options_orb"):
        if not replay_full_session and local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            no_or_msg = (
                "Insufficient historical data before cutoff (%s): no opening-range bars available" % res["now"]
                if market == "IN" else "Yahoo sent no bars for the opening range"
            )
            res.update(status="no-data", phase="open" if session_is_open else "closed", reason=no_or_msg)
            return _mark_no_data(res, is_fixture, is_simulated, past_bar_dates, session_is_open)
        lvl = max(b["h"] for b in or_bars)
    elif setup == "level":
        lvl = float(level)
    elif setup == "breakout_20d":
        lvl = float(b20["breakout_level"])
    else:
        lvl = float(chart.get("prev_close") or (bars[0]["c"] if bars else 100.0))
    res["level"] = r2(lvl)

    first_slot = n_or if setup in ("orb", "options_orb") else 0
    trig, thin = None, []

    if setup in ("orb", "level", "options_orb"):
        for b in bars:
            if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
                continue
            if b["c"] > lvl:
                vx = (b["v"] / avg_vol) if avg_vol else None
                if min_volume_x and (vx is None or vx < min_volume_x):
                    thin.append(b)
                    continue
                trig = b
                break
    elif setup == "breakout_20d":
        closes_prior = b20["closes_prior"]
        for i, b in enumerate(bars):
            if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
                continue
            if _bar_confirms_breakout_20d(closes_prior, b["c"], lvl):
                ref_avg_vol = avg_vol
                if ref_avg_vol is None and i > 0:
                    ref_avg_vol = sum(x["v"] for x in bars[:i]) / float(i)
                vx = (b["v"] / ref_avg_vol) if ref_avg_vol else None
                if min_volume_x and (vx is None or vx < min_volume_x):
                    thin.append(b)
                    continue
                trig = b
                break
    elif setup == "ma_crossover":
        fast_n, slow_n = 9, 21
        for i in range(slow_n, len(bars)):
            b = bars[i]
            if b["end"] < before or b["end"] > cutoff:
                continue
            f_curr = sum(x["c"] for x in bars[i - fast_n + 1:i + 1]) / float(fast_n)
            f_prev = sum(x["c"] for x in bars[i - fast_n:i]) / float(fast_n)
            s_curr = sum(x["c"] for x in bars[i - slow_n + 1:i + 1]) / float(slow_n)
            s_prev = sum(x["c"] for x in bars[i - slow_n:i]) / float(slow_n)
            if f_prev <= s_prev and f_curr > s_curr:
                trig = b
                break
    elif setup == "trend":
        fast_n, slow_n = 9, 21
        for i in range(slow_n, len(bars)):
            b = bars[i]
            if b["end"] < before or b["end"] > cutoff:
                continue
            f_curr = sum(x["c"] for x in bars[i - fast_n + 1:i + 1]) / float(fast_n)
            s_curr = sum(x["c"] for x in bars[i - slow_n + 1:i + 1]) / float(slow_n)
            if b["c"] > f_curr > s_curr:
                trig = b
                break
    elif setup == "rsi_momentum":
        closes = [b["c"] for b in bars]
        for i in range(15, len(bars)):
            b = bars[i]
            if b["end"] < before or b["end"] > cutoff:
                continue
            r = rsi_val(closes[:i + 1])
            r_prev = rsi_val(closes[:i])
            if r is not None and r >= 60.0 and (r_prev is None or r_prev < 60.0):
                trig = b
                break

    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        trig_reason = (
            "the %s bar closed above 20-day breakout level %s%.2f at %s%.2f"
            % (short(trig["et"]), curr, lvl, curr, trig["c"])
            if setup == "breakout_20d"
            else "the %s bar closed above %s" % (short(trig["et"]), "the range high" if setup == "orb" else "the level")
        )
        res.update(status="triggered", phase="open" if session_is_open else "after-close",
                   reason=trig_reason)
        conf_iso = _market_iso(local_to_utc(trig["end"], market=market), market=market)
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if setup == "breakout_20d":
            res["confirmation_price"] = r2(trig["c"])
            res["timestamp"] = conf_iso
            res["invalidation_level"] = _compute_breakout_invalidation(lvl, trig.get("l"), trig["c"], market=market)
            res["signal_freshness"] = _breakout_signal_freshness(
                "triggered", data_tier, data_status, is_live_data, is_fixture, is_simulated,
                is_historical, replay_full_session, eval_day, day,
                minutes_since=res["minutes_since"], phase=res["phase"],
            )

        # Options-specific enrichment
        if setup == "options_orb" and data_in:
            try:
                sym_clean = chart["symbol"]
                if symbols_in:
                    try:
                        sym_clean = symbols_in.normalize_symbol(sym_clean)
                    except Exception:
                        pass
                exp_arg = None
                if calendar_in is not None:
                    try:
                        exp_arg = calendar_in.get_next_expiry(
                            as_of_date=_to_ist_aware(now_utc),
                            underlying=sym_clean,
                        ).strftime("%Y-%m-%d")
                    except Exception:
                        exp_arg = None
                chain_kw = {"expiry": exp_arg, "spot_price": trig["c"]}
                if chart.get("is_offline"):
                    chain_kw["provider"] = data_in.get_provider("simulated")
                else:
                    chain_kw["config"] = cfg
                    chain_kw["now"] = _to_ist_aware(now_utc)
                chain = data_in.fetch_option_chain(sym_clean, **chain_kw)
                sel_strike = chain.select_strike(strategy=strike_selection or "ATM", option_type="CE")
                contract = chain.get_contract(sel_strike, "CE")
                if contract:
                    fo_lot = None
                    if instruments:
                        try:
                            fo_lot = instruments.get_fo_lot_size(sym_clean)
                        except Exception:
                            pass
                    lot_sz = fo_lot or (25 if sym_clean in ("NIFTY", "^NSEI") else 1)
                    tier_val = str(contract.get("tier", "SIMULATED")).replace("DataTier.", "")
                    res["option_contract"] = {
                        "symbol": contract["symbol"],
                        "underlying": sym_clean,
                        "strike": sel_strike,
                        "option_type": "CE",
                        "expiry": chain.expiry,
                        "premium": contract["last_price"],
                        "lot_size": lot_sz,
                        "greeks": contract.get("greeks", {}),
                        "tier": tier_val,
                        "is_simulated": tier_val == "SIMULATED",
                    }
                    if tier_val == "SIMULATED":
                        res["is_simulated"] = True
                    res["trigger"]["option_symbol"] = contract["symbol"]
                    res["trigger"]["option_premium"] = contract["last_price"]
            except Exception:
                pass

        if dt and utc_of(dt) < local_to_utc(trig["end"], market=market):
            move_unit = "a few paise" if market == "IN" else "a cent or two"
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move %s"
                                % (to_local(utc_of(dt), market=market).strftime("%H:%M:%S"), move_unit))
        res["is_live_signal"] = bool(
            session_is_open
            and is_live_data
            and data_tier == "CURRENT"
            and not is_fixture
            and not is_clock_override
            and not bool(res.get("is_simulated"))
            and not is_historical
            and not replay_full_session
            and res.get("status") == "triggered"
            and not (setup == "breakout_20d" and res.get("signal_freshness") == "stale")
        )
        return res
    if not bars:
        if (setup == "breakout_20d" and not replay_full_session and session_is_open
                and local < open_dt + datetime.timedelta(seconds=step)
                and not chart.get("intraday_error")):
            res.update(
                status="waiting",
                phase="forming",
                reason="market open, awaiting first completed %s bar close above breakout level %s%.2f" % (
                    interval, curr, lvl),
            )
            res["signal_freshness"] = _breakout_signal_freshness(
                "waiting", data_tier, data_status, is_live_data, is_fixture, is_simulated,
                is_historical, replay_full_session, eval_day, day, phase="forming",
            )
            return res
        no_bars_msg = (
            "Insufficient historical data before cutoff (%s): 0 closed bars for %s"
            % (res["now"], eval_day.isoformat())
            if market == "IN" else ("Yahoo sent no bars for %s yet" % eval_day.isoformat())
        )
        res.update(status="no-data", phase="open" if session_is_open else "closed", reason=no_bars_msg)
        _mark_no_data(res, is_fixture, is_simulated, past_bar_dates, session_is_open)
        if setup == "breakout_20d":
            res["signal_freshness"] = "stale" if data_status == "stale" else "unavailable"
        return res
    min_ind_bars = 22 if setup in ("ma_crossover", "trend") else (16 if setup == "rsi_momentum" else 0)
    if market == "IN" and min_ind_bars and len(bars) < min_ind_bars:
        res.update(
            status="no-data",
            phase="open" if session_is_open else ("after-close" if not session_is_open else "after-cutoff"),
            reason="Insufficient historical data before cutoff (%s): need %d closed bars for %s, have %d"
                   % (res["now"], min_ind_bars, setup, len(bars)),
        )
        return _mark_no_data(res, is_fixture, is_simulated, past_bar_dates, session_is_open)
    if setup == "orb":
        what = "close above the range high %s%.2f" % (curr, lvl)
    elif setup == "breakout_20d":
        what = "confirm breakout_20d with a completed close above %s%.2f" % (curr, lvl)
    else:
        what = "close above %s%.2f" % (curr, lvl)
    if (replay_full_session or local >= cutoff) and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if not session_is_open else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (
                       what, short(cutoff), " (1:00 p.m. early close today)" if early else ""))
        if setup == "breakout_20d":
            res["signal_freshness"] = _breakout_signal_freshness(
                "expired", data_tier, data_status, is_live_data, is_fixture, is_simulated,
                is_historical, replay_full_session, eval_day, day, phase=res["phase"],
            )
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = ("the %s bar closed above at %s%.2f but on %.2fx the %s volume, under your %.2fx" % (
            short(last["et"]), curr, last["c"], last["v"] / avg_vol if avg_vol else 0,
            "baseline" if setup == "breakout_20d" else "range's", min_volume_x))
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = ("the %s bar closed above at %s%.2f, but no entries before %s (config)" % (
            short(last["et"]), curr, last["c"], short(before)))
    elif setup == "breakout_20d":
        gap = lvl - last["c"]
        if b20 and b20.get("scanner_matched"):
            res["reason"] = (
                "prior daily close %s%.2f (%s) matched breakout_20d, awaiting completed intraday close above %s%.2f "
                "(last close %s%.2f at %s, %s%.2f %s, %+.2f%%)"
                % (curr, b20["daily_close"], b20["daily_close_date"], curr, lvl,
                   curr, last["c"], short(last["et"]), curr, abs(gap),
                   "under" if gap >= 0 else "over", 100.0 * (last["c"] / lvl - 1))
            )
        else:
            res["reason"] = ("last close %s%.2f (%s bar), %s%.2f %s 20-day breakout level %s%.2f (%+.2f%%)" % (
                curr, last["c"], short(last["et"]), curr, abs(gap), "under" if gap >= 0 else "over",
                curr, lvl, 100.0 * (last["c"] / lvl - 1)))
    else:
        gap = lvl - last["c"]
        res["reason"] = ("last close %s%.2f (%s bar), %s%.2f %s %s (%+.2f%%)" % (
            curr, last["c"], short(last["et"]), curr, abs(gap), "under" if gap >= 0 else "over",
            ("the range high %s%.2f" % (curr, lvl)) if setup == "orb" else ("the level %s%.2f" % (curr, lvl)),
            100.0 * (last["c"] / lvl - 1)))
    if setup == "breakout_20d":
        res["signal_freshness"] = _breakout_signal_freshness(
            "waiting", data_tier, data_status, is_live_data, is_fixture, is_simulated,
            is_historical, replay_full_session, eval_day, day, phase=res["phase"],
        )
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    sym = res["symbol"]
    if (res.get("market") == "IN" or str(sym).endswith((".NS", ".BO"))) and symbols_in:
        try:
            sym = symbols_in.normalize_symbol(sym)
        except Exception:
            pass
    elif str(sym).endswith((".NS", ".BO")):
        sym = sym.split(".")[0]
    provider_name = res.get("provider") or "yahoo"
    doc_source = "yahoo" if (provider_name == "offline" or res.get("market") != "IN") else provider_name
    doc = {"created_by": "trader-entry", "symbol": sym, "setup": res["setup"], "side": "buy",
           "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
           "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
           "interval": res["interval"],
           "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
           "source": doc_source, "provider": provider_name, "checked_at": iso(now_utc), "reason": res.get("reason")}
    if res.get("market"):
        doc["market"] = res["market"]
    if res.get("currency"):
        doc["currency"] = res["currency"]
    if res.get("option_contract"):
        doc["option_contract"] = res["option_contract"]
        doc["asset_type"] = "OPTIONS"
    if res.get("trading_style"):
        doc["trading_style"] = res["trading_style"]
    if res.get("is_market_open") is not None:
        doc["is_market_open"] = bool(res.get("is_market_open", False))
    if res.get("is_live_data") is not None:
        doc["is_live_data"] = bool(res.get("is_live_data", False))
    if res.get("is_live_signal") is not None:
        doc["is_live_signal"] = bool(res.get("is_live_signal", False))
    if res.get("is_historical_analysis") is not None:
        doc["is_historical_analysis"] = bool(res.get("is_historical_analysis", False))
    if res.get("is_fixture") is not None:
        doc["is_fixture"] = bool(res.get("is_fixture", False))
    if res.get("data_tier"):
        doc["data_tier"] = res["data_tier"]
    if res.get("last_trading_session"):
        doc["last_trading_session"] = res["last_trading_session"]
    if res.get("candle_session"):
        doc["candle_session"] = res["candle_session"]
    if res.get("simulated_at"):
        doc["simulated_at"] = res["simulated_at"]
    if res.get("clock_override"):
        doc["clock_override"] = True
    doc["last_data_timestamp"] = res.get("last_data_timestamp")
    if res.get("data_status"):
        doc["data_status"] = res["data_status"]
    if res.get("is_simulated") is not None:
        doc["is_simulated"] = bool(res.get("is_simulated", False))
    if res.get("setup") == "breakout_20d" or "breakout_level" in res:
        doc["breakout_level"] = res.get("breakout_level")
        doc["confirmation_price"] = res.get("confirmation_price")
        doc["timestamp"] = res.get("timestamp")
        doc["invalidation_level"] = res.get("invalidation_level")
        doc["signal_freshness"] = res.get("signal_freshness")
        if res.get("daily_close_date") is not None:
            doc["daily_close_date"] = res["daily_close_date"]
        if res.get("scanner_breakout_matched") is not None:
            doc["scanner_breakout_matched"] = bool(res["scanner_breakout_matched"])
    return doc


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    curr = "\u20b9" if res.get("market") == "IN" else "$"
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    if res["setup"] == "breakout_20d":
        if res.get("breakout_level") is not None:
            return "20-day breakout (close above %s%.2f)" % (curr, res["breakout_level"])
        return "20-day breakout"
    return ("close above %s%.2f" % (curr, res["level"])) if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    market = res.get("market", "US")
    curr = "\u20b9" if market == "IN" else "$"
    tz_label = "IST" if market == "IN" else "ET"
    is_hist = bool(res.get("is_historical_analysis"))
    is_fix = bool(res.get("is_fixture"))
    is_clock_override = bool(res.get("clock_override"))
    closed_analysis = bool(res.get("_replay_full_session") or (is_hist and not res.get("_market_session_open") and res.get("phase") not in ("pre-open", "open", "forming")))
    tier = res.get("data_tier") or ("FIXTURE" if is_fix else ("HISTORICAL" if closed_analysis else "DELAYED"))
    sess_date = res.get("last_trading_session", res["date"])
    candle_sess = res.get("candle_session") or res["date"]
    prov_name = str(res.get("provider") or ("offline" if is_fix else "yahoo"))
    if closed_analysis:
        head = "%s  %s %s %s  %s on %s bars [HISTORICAL ANALYSIS -- Market CLOSED, Session: %s%s%s]" % (
            res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), tz_label, setup_name(res), bars,
            sess_date, ", Fixture" if is_fix else "", ", --now clock override" if is_clock_override else "")
    elif is_fix:
        head = "%s  %s %s %s  %s on %s bars [OFFLINE FIXTURE -- historical replay, NOT live]%s" % (
            res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), tz_label, setup_name(res), bars,
            " (--now clock override)" if is_clock_override else "")
    else:
        clock_suffix = " (application clock overridden via --now)" if is_clock_override else ""
        if is_clock_override and prov_name.lower() == "yahoo":
            prov_label = "Yahoo delayed provider, free, %s data%s - NOT live" % (tier, clock_suffix)
        elif is_clock_override:
            prov_label = "%s provider, %s data%s - NOT live" % (prov_name, tier, clock_suffix)
        elif res.get("is_live_data") and tier == "CURRENT":
            prov_label = "%s live provider, CURRENT data" % prov_name
        elif prov_name.lower() == "yahoo":
            prov_label = "Yahoo delayed provider, free, %s data - NOT live" % tier
        else:
            prov_label = "%s provider, %s data - NOT live" % (prov_name, tier)
        head = "%s  %s %s %s  %s on %s bars (%s)" % (
            res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), tz_label, setup_name(res), bars, prov_label)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s%.2f, low %s%.2f (%d of %d bars%s)." % (
            short(start), short(end), curr, orr["high"], curr, orr["low"], orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        if closed_analysis:
            out.append("[HISTORICAL ANALYSIS] TRIGGERED on session %s at %s%.2f: the %s bar (bar %d) closed above %s %s%.2f (%+.2f%%%s)." % (
                candle_sess, curr, t["price"], short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
                "the range high" if res["setup"] == "orb" else "the level", curr, res["level"], t["pct_over"],
                (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
            out.append("Market is CLOSED. Evaluated on historical candles from session %s (Data tier: %s, status: %s)." % (
                candle_sess, tier, str(res.get("data_status", "stale")).upper()))
            out.append("NOT a live-session signal. Orders evaluated outside market hours are for research and paper trading only. Nothing was placed.")
            out.append("Next: set the stop and target BEFORE you buy: /trader-exit "
                       "(exit.py plan --from trader/entry.json).")
        else:
            out.append("TRIGGERED %s at %s%.2f: the %s bar (bar %d) closed above %s %s%.2f (%+.2f%%%s)." % (
                t["time"], curr, t["price"], short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
                "the range high" if res["setup"] == "orb" else "the level", curr, res["level"], t["pct_over"],
                (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
            step = INTERVALS[res["interval"]] // 60
            if res.get("minutes_since", 0) > step and res.get("last"):
                out.append("That was %d minutes ago; the last close is %s%.2f (%s bar). A late entry is a different "
                           "trade: the price, the stop distance and the odds have moved." % (
                               res["minutes_since"], curr, res["last"]["close"], res["last"]["bar_start"]))
            if res.get("data_note"):
                out.append(res["data_note"] + ".")
            if is_fix:
                out.append("Offline fixture replay (Data tier: %s) -- NOT a live market signal. Nothing was placed.%s" % (
                    tier, " (Application clock overridden via --now.)" if is_clock_override else ""))
            elif not res.get("is_live_signal", False):
                d_stat = str(res.get("data_status", "delayed")).upper()
                if res.get("is_simulated"):
                    out.append("Simulated candidate evaluation (provider: %s, Data tier: %s, status: %s%s) -- NOT a live market signal. Nothing was placed." % (
                        prov_name, tier, d_stat, ", --now clock override" if is_clock_override else ""))
                elif is_clock_override and tier != "DELAYED":
                    out.append("HISTORICAL / TEST-CLOCK SIGNAL (--now %s, provider: %s, Data tier: %s, status: %s) -- NOT a current live entry signal. Nothing was placed." % (
                        res.get("simulated_at") or res.get("now") or "", prov_name, tier, d_stat))
                else:
                    out.append("CANDIDATE DETECTED ON DELAYED DATA (provider: %s, Data tier: %s, status: %s%s) -- NOT a live market signal. Nothing was placed." % (
                        prov_name, tier, d_stat, ", --now clock override" if is_clock_override else ""))
            elif res.get("minutes_since", 0) > step:
                out.append("Current live signal (%s provider, Data tier: CURRENT). Nothing was placed." % prov_name)
            else:
                out.append("This is the moment to get in, if you take the trade (%s live provider, Data tier: CURRENT live signal). Nothing was placed." % prov_name)
            out.append("Next: set the stop and target BEFORE you buy: /trader-exit "
                       "(exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        if not closed_analysis and not is_fix and not res.get("is_live_data", False) and res.get("phase") in ("open", "forming"):
            out.append("Evaluated using %s data (%s provider, status: %s -- NOT current live data)." % (
                tier, prov_name, str(res.get("data_status", "delayed")).upper()))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s%.2f so far, closes %s); it does not count until it closes." % (
                f["bar_start"].lstrip("0"), curr, f["price"], f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s %s." % (res["next_open"], "IST" if market == "IN" else "New York"))
    if res.get("early_close"):
        out.append("Early close today at 1:00 p.m. (%s): no new entries after %s." % (
            res["early_close"], res["no_entries_after"]))
    if res.get("setup") == "breakout_20d":
        bl = res.get("breakout_level")
        cp = res.get("confirmation_price")
        ts_val = res.get("timestamp")
        inv = res.get("invalidation_level")
        fresh = res.get("signal_freshness") or "unavailable"
        bl_text = ("%s%.2f" % (curr, bl)) if bl is not None else "unavailable"
        if res.get("daily_close_date"):
            bl_text += " (as of %s%s)" % (
                res["daily_close_date"],
                "; scanner breakout match" if res.get("scanner_breakout_matched") else "",
            )
        cp_text = ("%s%.2f" % (curr, cp)) if cp is not None else "none (unconfirmed)"
        ts_text = ts_val if ts_val else "none"
        inv_text = ("%s%.2f" % (curr, inv)) if inv is not None else "none"
        out.append("Breakout level: %s | Confirmation price: %s | Timestamp: %s" % (bl_text, cp_text, ts_text))
        out.append("Invalidation / stop level: %s | Signal freshness: %s" % (inv_text, fresh))
    if not res.get("calendar_known"):
        out.append("Holiday list covers %s only: check nyse.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    curr = "\u20b9" if res.get("market") == "IN" else "$"
    start = short(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s%.2f to %s%.2f" % (curr, min(x["l"] for x in seen), curr, max(x["h"] for x in seen))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s%.2f, low %s%.2f" % (curr, orr["high"], curr, orr["low"])
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s%.2f (%+.2f%%). Get in now if you take it: %s, %s%.2f" % (
            curr, lvl, t["pct_over"], t["time"], curr, t["price"])
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s%.2f" % (curr, lvl)
        if res["setup"] in ("level", "breakout_20d") or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x", market=res.get("market", "US"))):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s%.2f under %s%.2f" % (curr, lvl - b["c"], curr, lvl)
    return "%5s bar  closed %s%.2f  %s" % (start, curr, b["c"], note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    market = opts.get("market", "US")
    tz_label = "IST" if market == "IN" else "ET"
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_local(now, market=market)
        day = local.date()
        s = session(day, market=market)
        if not s:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), trading_day(day, market=market)[1]))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s %s  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), tz_label, "not open yet" if local < open_dt else "open, no bar closed yet",
                    short(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = local_to_utc(open_dt, market=market) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s %s  could not read bars: %s" % (local.strftime("%H:%M:%S"), tz_label, e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        def_cutoff = "15:00" if market == "IN" else "15:30"
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or def_cutoff, "cutoff", market=market))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("v2 plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup in ("orb", "options_orb", "breakout_20d") and a.level is not None:
        if a.setup == "breakout_20d":
            raise EntryError("--level is for --setup level (breakout_20d derives its level from 20-day daily closes)")
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    trading_style = getattr(a, "trading_style", None) or cfg.get("trading_style") or "intraday"
    option_strategy = getattr(a, "option_strategy", None) or cfg.get("option_strategy")
    strike_selection = getattr(a, "strike_selection", None) or cfg.get("strike_selection") or "ATM"
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg, "trading_style": trading_style,
            "option_strategy": option_strategy, "strike_selection": strike_selection}


def _load(a, sym, now, market="US", cfg=None, setup=None):
    """(chart, now). An offline file without --now is read as of its own time."""
    if cfg is None and hasattr(a, "config"):
        cfg = read_config(a.config, market=market)
    setup = setup or getattr(a, "setup", "orb")
    daily = getattr(a, "daily", None)
    real_today = to_local(utc_now(), market=market).date()
    has_explicit_now = now is not None
    if a.offline and not has_explicit_now:
        ch = get_chart(sym, a.interval, offline=a.offline, market=market, cfg=cfg, setup=setup, daily=daily)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    ch = get_chart(sym, a.interval, to_local(now, market=market).date(), a.offline, real_today,
                   market=market, cfg=cfg, now=now, setup=setup, daily=daily)
    if not has_explicit_now and not a.offline and isinstance(ch, dict):
        ch.pop("simulated_at", None)
    return ch, now


def cmd_check(a):
    sym = a.symbol.upper()
    market = get_market_profile(a, sym=sym)
    cfg = read_config(a.config, market=market)
    opts = _opts(a, cfg)
    opts["market"] = market
    chart, now = _load(a, sym, parse_now(a.now, market=market) if a.now else None, market=market, cfg=cfg, setup=a.setup)
    opts["simulated"] = bool(a.now)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    sym = a.symbol.upper()
    market = get_market_profile(a, sym=sym)
    cfg = read_config(a.config, market=market)
    opts = _opts(a, cfg)
    opts["market"] = market
    opts["simulated"] = bool(a.now)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now, market=market) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None, market=market, cfg=cfg, setup=a.setup)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_local(utc_now(), market=market).date()
    daily = getattr(a, "daily", None)

    def provider(now):
        ch = get_chart(sym, a.interval, to_local(now, market=market).date(), a.offline, real_today,
                       market=market, cfg=cfg, now=now, setup=a.setup, daily=daily)
        if not a.now and not a.offline and isinstance(ch, dict):
            ch.pop("simulated_at", None)
        return ch

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until", market=market)
        until = datetime.datetime.combine(to_local(now_fn(), market=market).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    market = get_market_profile(a)
    cfg = read_config(a.config, market=market)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <TICKER>.json files")
    if not getattr(a, "market", None) and str(doc.get("market", "")).upper() in ("IN", "INDIA"):
        market = "IN"

    now = parse_now(a.now, market=market) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        item_market = market if getattr(a, "market", None) else get_market_profile(a, sym=sym, cfg=cfg)
        o = dict(opts)
        o["market"] = item_market
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now, market=item_market, cfg=cfg, setup=a.setup)
            o["simulated"] = bool(a.now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-6s  error: %s" % (s, e))
                continue
            curr = "\u20b9" if (r and r.get("market") == "IN") else "$"
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % (
                s, status[:9], t.get("time") or "-", (curr + "%.2f" % t["price"]) if t else "-",
                (curr + "%.2f" % r["level"]) if r.get("level") is not None else "-",
                (curr + "%.2f" % last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        live_n = sum(1 for _, r, _ in rows if r and r.get("is_live_signal"))
        non_live_n = n - live_n
        print("%d of %d triggered. Pick one, then: entry.py check <TICKER> --write, and /trader-exit." % (n, len(rows)))
        if non_live_n > 0:
            tiers = sorted(set(str(r.get("data_tier", "DELAYED")) for _, r, _ in rows if r and r["status"] == "triggered" and not r.get("is_live_signal")))
            print("Note: %d triggered candidate(s) used non-live data (%s) -- NOT live market signals." % (non_live_n, "/".join(tiers)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", default="orb",
                       help="entry setup: orb, level, breakout_20d, ma_crossover, trend, rsi_momentum, or options_orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
        p.add_argument("--now", help='override the application clock to this market time for backtests/tests, e.g. "2026-10-19 10:15" (marks signals non-live)')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--daily", metavar="PATH", help="saved 1d daily history JSON (.json/.json.gz) or folder (--setup breakout_20d)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--market", choices=["US", "IN"], help="market profile (US or IN)")
        p.add_argument("--trading-style", choices=["intraday", "positional"], default=None,
                       help="trading style: intraday (default) or positional")
        p.add_argument("--option-strategy", default=None, help="option strategy: long_call, long_put, etc.")
        p.add_argument("--strike-selection", default="ATM", help="strike selection: ATM, ITM1, ITM2, OTM1, OTM2")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this market local time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
