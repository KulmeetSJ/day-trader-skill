#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the reel: stop 358.32, target 370.04
    python3 exit.py plan --symbol TSLA --entry 364.18 --or-high 362.18 --or-low 354.47
    python3 exit.py plan --stop atr:1.5 --offline TSLA.json # ATR needs the session's bars
    python3 exit.py plan --symbol NVDA --entry 190.25 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the tick: cent/0.01 in US, 0.05 rupee/paise on NSE/BSE, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round half up to the tick):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price (too tight to survive the spread), an
entry.json that has not triggered (unless you give --entry), a short (v2
plans longs only).

flat_by (default 15:55 ET for US / 15:20 IST for IN, or "flat_by" in
trader/config.json): out by then, win or lose. On a 1:00 p.m. NYSE early close
it moves up with the close (12:55). "min_reward_risk" in the config (default
1.0) only warns here; trader-risk enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
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
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

_alerts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "trader-alerts"))
if _alerts_dir not in sys.path:
    sys.path.insert(0, _alerts_dir)

# Cross-skill helpers live in ../trader-alerts (added to sys.path above)
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

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/KulmeetSJ/day-trader-skill)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:55", "min_reward_risk": 1.0}
DEFAULTS_IN = {"flat_by": "15:20", "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
CENT = Decimal("0.01")
TICK_IN = Decimal("0.05")
EPOCH = datetime.datetime(1970, 1, 1)
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
NSE_OPEN = datetime.time(9, 15)
NSE_CLOSE = datetime.time(15, 30)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

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
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class PlanError(Exception):
    pass


def get_market_profile(a=None, sym=None, doc=None, cfg=None):
    if a and getattr(a, "market", None):
        return a.market.upper()
    if doc and isinstance(doc, dict) and str(doc.get("market", "")).upper() in ("IN", "INDIA"):
        return "IN"
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
                if str(c.get("market", "")).upper() in ("IN", "INDIA"):
                    return "IN"
        except Exception:
            pass
    if sym:
        s = sym.upper()
        if s.endswith(".NS") or s.endswith(".BO") or s.endswith("-EQ"):
            return "IN"
    return "US"


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
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


def _market_iso(utc_dt, market="US"):
    if utc_dt is None:
        return None
    local = to_local(utc_dt, market=market)
    if market == "IN":
        return local.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    off = eastern_offset(utc_dt)
    sign = "-" if off < 0 else "+"
    return "%s%s%02d:00" % (local.strftime("%Y-%m-%dT%H:%M:%S"), sign, abs(off))


def _to_ist_aware(utc_dt):
    if utc_dt is None:
        return None
    ist_tz = calendar_in.IST if calendar_in is not None else datetime.timezone(IST_OFFSET, "IST")
    if getattr(utc_dt, "tzinfo", None) is None:
        return utc_dt.replace(tzinfo=datetime.timezone.utc).astimezone(ist_tz)
    return utc_dt.astimezone(ist_tz)


def _parse_ts_utc(val, market="US"):
    """Parse an ISO, '... IST', or epoch timestamp into naive UTC datetime."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return utc_of(int(val))
    t = str(val).strip()
    is_ist = False
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
        is_ist = True
    elif t.upper().endswith(" ET"):
        t = t[:-3].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return (dt - (dt.utcoffset() or datetime.timedelta(0))).replace(tzinfo=None)
    if is_ist or market == "IN":
        return dt - IST_OFFSET
    return eastern_to_utc(dt)


def local_to_utc(local, market="US"):
    if market == "IN":
        return local.replace(tzinfo=None) - IST_OFFSET
    return eastern_to_utc(local)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text, market="US"):
    """--now -> naive UTC. A time without an offset is local time."""
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
            raise PlanError('--now wants an IST time like "2026-10-19 10:15" (or ...Z for UTC)')
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
        raise PlanError('--now wants a New York time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - (dt.utcoffset() or datetime.timedelta(0))).replace(tzinfo=None)


def parse_hhmm(text, what, market="US"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    market_tz = "IST" if market == "IN" else "New York"
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants a %s time like 15:55, not %r" % (what, market_tz, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:15 AM, 10:15, 3:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def session(d, market="US"):
    """(open_time, close_time) in local time, or None if closed all day."""
    if market == "IN":
        if calendar_in:
            is_trade, _status, _reason = calendar_in.is_trading_day_in(d)
            if not is_trade:
                return None
            return NSE_OPEN, NSE_CLOSE
        if d.weekday() >= 5:
            return None
        return NSE_OPEN, NSE_CLOSE
    if d.weekday() >= 5 or d.isoformat() in NYSE_HOLIDAYS:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


# ---------------------------------------------------------------- money math (Decimal, to the cent/tick)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().lstrip("$").lstrip("\u20b9").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def floor_tick(x, tick=CENT):
    """Round down away from the entry to the nearest tick (0.01 in US, 0.05 on NSE)."""
    d = D(x)
    t = Decimal(str(tick))
    steps = (d / t).to_integral_value(rounding=ROUND_FLOOR)
    return (steps * t).quantize(t)


def round_tick(x, tick=CENT):
    """Round half up to the nearest tick (0.01 in US, 0.05 on NSE)."""
    d = D(x)
    t = Decimal(str(tick))
    steps = (d / t).to_integral_value(rounding=ROUND_HALF_UP)
    return (steps * t).quantize(t)


def cents(x, tick=CENT):
    """Round half up to the tick/cent."""
    return round_tick(x, tick)


def floor_cents(x, tick=CENT):
    """A long's stop: round down, away from the entry."""
    return floor_tick(x, tick)


def money(x, market="US"):
    curr = "\u20b9" if market == "IN" else "$"
    return "%s%s" % (curr, format(D(x).quantize(CENT), ",f"))


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    if t in ("breakout-invalidation", "breakout_invalidation", "invalidation"):
        return "breakout_invalidation", None
    m = re.match(r"^(price|pct|atr):\s*(?:\$|\u20b9|rs\.?\s*)?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*(?:\$|\u20b9|rs\.?\s*)?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, market="US", tick_size=None):
    """The stop, the target and the risk, in exact ticks. Raises PlanError with the reason."""
    tick = Decimal(str(tick_size)) if tick_size else (TICK_IN if market == "IN" else CENT)
    curr = "\u20b9" if market == "IN" else "$"
    e = round_tick(entry, tick)
    if e <= 0:
        raise PlanError("the entry price must be above %s0" % curr)
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = round_tick(or_high, tick), round_tick(or_low, tick)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh, market=market), money(ol, market=market)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s%s" % (money(oh, market=market), money(ol, market=market), curr, num(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "breakout_invalidation":
        if stop_param is None:
            raise PlanError("a breakout-invalidation stop requires an invalidation level from a breakout_20d entry")
        raw, label = stop_param, "breakout invalidation"
        detail = "inherited breakout_20d invalidation level %s%s" % (curr, num(D(stop_param)))
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s%s" % (money(e, market=market), num(stop_param), curr, num(raw))
    elif stop_rule == "atr":
        if atr is None or atr_bars is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR %s%s over the %d bars to the entry%s" % (
            curr, num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = floor_tick(raw, tick)
    if stop != D(raw):
        detail = (detail + ", rounded down" if detail else "%s%s rounded down" % (curr, num(raw)))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money(stop, market=market), label, money(e, market=market)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above %s0" % (money(stop, market=market), curr))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (money(risk, market=market), money(floor_risk.quantize(CENT, rounding=ROUND_HALF_UP), market=market), money(e, market=market)))
    if target_rule == "r":
        target = round_tick(e + target_param * risk, tick)
        t_label = r_label(target_param)
    else:
        target = round_tick(target_param, tick)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target, market=market), money(e, market=market)))
    rr = ((target - e) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick_size": tick}


# ---------------------------------------------------------------- bars for ATR (same reader as trader-entry)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
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
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


_ORIG_YAHOO_GET = yahoo_get


def load_chart(sym, interval, day, offline=None, market="US", cfg=None, now_utc=None):
    ysym = (symbols_in.to_yahoo_symbol(sym) if (market == "IN" and symbols_in) else sym.upper().replace(".", "-"))
    if offline:
        path = offline
        if os.path.isdir(offline):
            candidates = [
                os.path.join(offline, ysym + ".json"),
                os.path.join(offline, ysym + ".json.gz"),
                os.path.join(offline, sym.upper() + ".json"),
                os.path.join(offline, sym.upper() + ".json.gz"),
                os.path.join(offline, sym.upper() + "-5m.json"),
                os.path.join(offline, sym.upper() + "-5m.json.gz"),
            ]
            for p in candidates:
                if os.path.exists(p):
                    path = p
                    break
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    elif market == "IN" and data_in is not None:
        try:
            prov = data_in.get_provider(config=cfg)
        except Exception as e:
            raise PlanError("Market data provider error: %s" % e)
        use_legacy_mock = (prov.provider_name == "yahoo" and yahoo_get is not _ORIG_YAHOO_GET)
        ref_utc = now_utc or utc_now()
        back = (to_local(ref_utc, market=market).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if prov.provider_name == "yahoo" and interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        if use_legacy_mock:
            text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (ysym, rng, interval))
        else:
            exch = (cfg or {}).get("exchange") or "NSE"
            try:
                payload = prov.get_intraday_data(
                    sym,
                    interval=interval,
                    range_str=rng,
                    exchange=exch,
                    now=_to_ist_aware(now_utc) if now_utc is not None else None,
                )
            except Exception as e:
                raise PlanError(str(e))
            return data_in.to_exit_chart(payload)
    else:
        ref_utc = now_utc or utc_now()
        back = (to_local(ref_utc, market=market).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (ysym, rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    meta_sym = str(meta.get("symbol") or ysym).upper()
    matched = False
    if market == "IN" and symbols_in:
        try:
            matched = (symbols_in.normalize_symbol(meta_sym) == symbols_in.normalize_symbol(sym))
        except Exception:
            matched = (meta_sym == ysym)
    else:
        matched = (meta_sym == ysym)
    if not matched:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    dt_raw = meta.get("regularMarketTime")
    if now_utc is not None and market == "IN":
        step = INTERVALS.get(interval, 300)
        cutoff_ts = int((now_utc.replace(tzinfo=None) - EPOCH).total_seconds())
        bars = [b for b in bars if b["t"] + step <= cutoff_ts]
        if bars:
            latest_close = min(cutoff_ts, max(b["t"] + step for b in bars))
            dt_raw = latest_close if (dt_raw is None or int(dt_raw) > cutoff_ts) else int(dt_raw)
        elif dt_raw is not None and int(dt_raw) > cutoff_ts:
            dt_raw = None
    return {"bars": bars, "data_time": dt_raw, "provider": "offline" if offline else "yahoo"}


def session_bars(chart, day, interval, upto_slot=None, now_utc=None, market="US"):
    """Closed, on-grid bars of one session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day, market=market)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_local(now_utc, market=market) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        et = to_local(utc_of(b["t"]), market=market)
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        if local and et + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((et - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path, market="US"):
    cfg = dict(DEFAULTS_IN if market == "IN" else DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
        if str(doc.get("market", "")).upper() in ("IN", "INDIA"):
            cfg["market"] = "IN"
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time, market="US", trading_style="intraday"):
    """(HH:MM, note). The config's flat-by moves up with an early close."""
    if trading_style == "positional":
        return "positional", "positional trade: multi-day hold permitted"
    def_flat = DEFAULTS_IN["flat_by"] if market == "IN" else DEFAULTS["flat_by"]
    t = parse_hhmm(explicit or cfg.get("flat_by") or def_flat, "--flat-by" if explicit else "flat_by", market=market)
    note = ""
    s = session(day, market=market) if day else None
    if s:
        close_dt = datetime.datetime.combine(day, s[1])
        at = datetime.datetime.combine(day, t)
        if at >= close_dt:
            if explicit:
                raise PlanError("--flat-by %s is at or after the %s close on %s" % (hm(t), ampm(s[1]), day))
            if market == "US":
                moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
                if moved.time() <= OPEN:
                    raise PlanError("flat_by %s in the config is at or after the close" % hm(t))
                t = moved.time()
                note = "moved up for the 1:00 p.m. early close (%s)" % NYSE_EARLY_CLOSE.get(day.isoformat(), "")
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("v2 plans longs only: no stops above a short entry. Plan a buy instead.")
    style = getattr(a, "trading_style", None)
    asset_type = getattr(a, "asset_type", None)
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags", "doc": None,
                "trading_style": style or "intraday", "asset_type": asset_type}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: v2 plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path, "doc": doc,
                "trading_style": style or doc.get("trading_style") or "intraday",
                "asset_type": asset_type or doc.get("asset_type")}
        if doc.get("option_contract"):
            info["option_contract"] = doc["option_contract"]
            if not info.get("asset_type"):
                info["asset_type"] = "OPTIONS"
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now_utc=None):
    info = gather(a)
    cfg = read_config(a.config)
    market = get_market_profile(a, sym=info["symbol"], doc=info.get("doc"), cfg=cfg)
    explicit_now = bool(getattr(a, "now", None))
    if now_utc is None:
        now_utc = parse_now(getattr(a, "now", None), market=market)
    simulated = bool(explicit_now or (info.get("doc") or {}).get("simulated_at"))
    if market == "IN" and symbols_in:
        try:
            info["symbol"] = symbols_in.normalize_symbol(info["symbol"])
        except Exception:
            pass
    cfg_market = read_config(a.config, market=market) if market == "IN" else cfg

    trading_style = info.get("trading_style") or cfg_market.get("trading_style") or "intraday"
    asset_type = info.get("asset_type") or cfg_market.get("asset_type") or "EQUITY"
    option_contract = info.get("option_contract")

    # If symbol is a derivative symbol (e.g. NIFTY26MAR24000CE)
    if market == "IN" and symbols_in and symbols_in.is_derivative_symbol(info["symbol"]):
        deriv_meta = symbols_in.parse_derivative_symbol(info["symbol"])
        if deriv_meta:
            asset_type = "OPTIONS" if deriv_meta.get("instrument_type") == "OPT" else "FUTURES"
            if deriv_meta.get("instrument_type") == "OPT" and not option_contract:
                option_contract = {
                    "symbol": info["symbol"],
                    "underlying": deriv_meta.get("underlying"),
                    "expiry": deriv_meta.get("expiry_date"),
                    "strike": deriv_meta.get("strike"),
                    "option_type": deriv_meta.get("option_type")
                }

    # Instrument metadata resolution (tick size, lot size, T2T)
    tick_size = None
    lot_size = 1
    if market == "IN":
        inst_path = getattr(a, "instruments", "trader/state/instruments.json")
        if instruments and inst_path and os.path.exists(inst_path):
            instr_err = instruments.InstrumentMetadataError
            try:
                cache = instruments.load_instrument_cache(inst_path, max_age_days=30, as_of_date=info.get("date"))
                rec = instruments.resolve_instrument(info["symbol"], cache, require_intraday=True)
                tick_size = Decimal(str(rec.get("tick_size") or "0.05"))
                # Derivatives trade in F&O contract lots; cash equities trade in shares (lot_size=1)
                if asset_type in ("OPTIONS", "FUTURES"):
                    lot_size = rec.get("fo_lot_size") or rec.get("lot_size") or 1
                else:
                    lot_size = rec.get("lot_size") or 1
            except instr_err as exc:
                raise PlanError(str(exc))
        else:
            tick_size = TICK_IN

    if asset_type in ("OPTIONS", "FUTURES") and instruments:
        fo_lot = instruments.get_fo_lot_size(info["symbol"])
        if fo_lot:
            lot_size = fo_lot
            if option_contract:
                option_contract["lot_size"] = fo_lot

    stop_text = a.stop
    if not stop_text:
        if info["setup"] == "orb":
            stop_text = "or-mid"
        elif info["setup"] == "breakout_20d" and (info.get("doc") or {}).get("invalidation_level") is not None:
            stop_text = "breakout-invalidation"
        else:
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
    stop_rule, stop_param = parse_stop(stop_text)
    if stop_rule == "breakout_invalidation":
        inv = (info.get("doc") or {}).get("invalidation_level")
        if inv is None:
            raise PlanError("breakout-invalidation stop requires a breakout_20d entry with invalidation_level")
        stop_param = D(str(inv))
    target_rule, target_param = parse_target(a.target or "2R")
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_local(now_utc, market=market).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    sim_local = to_local(now_utc, market=market)
    if explicit_now:
        if day > sim_local.date():
            raise PlanError(
                "Plan date %s is after simulated time %s (look-ahead blocked)"
                % (day.isoformat(), sim_local.strftime("%Y-%m-%d %H:%M:%S"))
            )
        doc_in = info.get("doc") or {}
        if doc_in.get("last_data_timestamp"):
            doc_lts_utc = _parse_ts_utc(doc_in.get("last_data_timestamp"), market=market)
            if doc_lts_utc is not None and doc_lts_utc > now_utc:
                raise PlanError(
                    "Entry data timestamp %s is after simulated time %s (look-ahead blocked)"
                    % (doc_in.get("last_data_timestamp"), _market_iso(now_utc, market=market))
                )
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg_market, entry_time, market=market, trading_style=trading_style)
    if explicit_now and day == sim_local.date() and entry_time is not None:
        entry_utc = local_to_utc(datetime.datetime.combine(day, entry_time), market=market)
        if entry_utc > now_utc:
            raise PlanError(
                "Entry time %s on %s is after simulated time %s (look-ahead blocked)"
                % (ampm(entry_time), day.isoformat(), sim_local.strftime("%Y-%m-%d %H:%M:%S"))
            )
    atr = atr_n = None
    chart_provider = None
    chart_last_ts_iso = None
    if stop_rule == "atr":
        atr_now = now_utc if explicit_now else None
        chart = load_chart(info["symbol"], info["interval"], day, a.offline, market=market, cfg=cfg_market, now_utc=atr_now)
        chart_provider = chart.get("provider")
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], atr_now, market=market)
        if len(bars) < 3:
            raise PlanError(
                "an atr stop needs at least 3 closed %s bars of %s on %s%s; found %d"
                % (info["interval"], info["symbol"], day,
                   " by the cutoff (%s)" % sim_local.strftime("%Y-%m-%d %H:%M:%S") if explicit_now else " by the entry",
                   len(bars))
            )
        atr, atr_n = atr_of(bars)
        last_bar_end = utc_of(bars[-1]["t"] + INTERVALS[info["interval"]])
        if atr_now is not None and last_bar_end > atr_now:
            last_bar_end = atr_now
        chart_last_ts_iso = chart.get("last_data_timestamp") or _market_iso(last_bar_end, market=market)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, market=market, tick_size=tick_size)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(round_tick(info["or_high"], tick=c["tick_size"])),
               "low": float(round_tick(info["or_low"], tick=c["tick_size"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if asset_type:
        plan["asset_type"] = asset_type
    if option_contract:
        plan["option_contract"] = option_contract
        if not lot_size or lot_size == 1:
            lot_size = option_contract.get("lot_size", 1)
    if trading_style:
        plan["trading_style"] = trading_style
    if market == "IN":
        plan["market"] = "IN"
        plan["currency"] = "INR"
        plan["tick_size"] = float(c["tick_size"])
        if lot_size and lot_size > 1:
            plan["lot_size"] = lot_size
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    d = info.get("doc") or {}
    for k in ("is_market_open", "is_live_data", "is_historical_analysis", "is_live_signal",
              "is_fixture", "data_tier", "last_trading_session", "last_data_timestamp",
              "data_status", "is_simulated", "provider", "signal_freshness",
              "simulated_at", "candle_session"):
        if k in d:
            plan[k] = d[k]
    if chart_last_ts_iso is not None and not plan.get("last_data_timestamp"):
        plan["last_data_timestamp"] = chart_last_ts_iso
    if simulated:
        plan["simulated_at"] = _market_iso(now_utc, market=market)
    if chart_provider and "provider" not in plan:
        plan["provider"] = chart_provider
    if str(plan.get("provider") or "").lower() == "yahoo":
        plan["is_live_signal"] = False
        plan["is_live_data"] = False
        if "data_tier" not in plan or str(plan.get("data_tier") or "").upper() == "CURRENT":
            plan["data_tier"] = "HISTORICAL" if (simulated or plan.get("is_historical_analysis")) else "DELAYED"
    elif str(plan.get("provider") or "").lower() == "offline":
        plan["is_live_signal"] = False
        plan["is_live_data"] = False
        plan.setdefault("is_fixture", True)
        plan.setdefault("data_tier", "FIXTURE")
    warnings = []
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    mrr = cfg_market.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    today = to_local(now_utc, market=market).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level",
             "breakout_20d": "20-day breakout"}.get(plan["setup"], plan["setup"])
    market = plan.get("market", "US")
    tz_label = "IST" if market == "IN" else "ET"
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (money(plan["entry"], market=market), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"], market=market), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"], market=market), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (money(plan["risk_per_share"], market=market), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    if plan.get("option_contract"):
        opt = plan["option_contract"]
        out.append("  Option        %s %s %s, exp %s (lot %s)" % (
            opt.get("underlying", plan["symbol"]),
            opt.get("strike", ""),
            opt.get("option_type", ""),
            opt.get("expiry", ""),
            plan.get("lot_size", 1)
        ))
    if plan.get("flat_by") == "positional":
        out.append("  Hold          positional: multi-day hold permitted")
    else:
        out.append("  Flat by       %s %s, win or lose: no overnight hold" % (plan["flat_by"], tz_label))
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    plan, warnings = build_plan(a, None)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    market = getattr(a, "market", None) or plan.get("market") or (
        "IN" if plan.get("symbol", "").endswith((".NS", ".BO", "-EQ")) else get_market_profile(a, plan.get("symbol"))
    )
    today = to_local(parse_now(a.now), market=market).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level", "breakout_20d"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:15 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM market time (default: config flat_by, 15:55 ET or 15:20 IST)")
    p.add_argument("--asset-type", choices=["EQUITY", "FUTURES", "OPTIONS"], help="asset type")
    p.add_argument("--trading-style", choices=["intraday", "positional"], help="trading style")
    p.add_argument("--market", choices=["US", "IN"], help="market profile: US (default) or IN")
    p.add_argument("--instruments", default="trader/state/instruments.json", help="path to instruments cache")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this market time, e.g. "2026-10-19 10:15"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--market", choices=["US", "IN"], help="market profile: US (default) or IN")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
