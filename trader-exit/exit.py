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
    import visuals  # type: ignore
except ImportError:
    calendar_in = None
    symbols_in = None
    instruments = None
    data_in = None
    visuals = None

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/KulmeetSJ/day-trader-skill)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
DEFAULT_EXIT_STATE = "trader/state/exit_decisions.json"
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
            sp = calendar_in.get_special_session(d.isoformat(), exchange="NSE")
            if sp and sp.get("open_time") and sp.get("close_time"):
                return (parse_hhmm(sp["open_time"], "special open", market="IN"),
                        parse_hhmm(sp["close_time"], "special close", market="IN"))
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


def _price_scale(x=None, tick=None):
    scale = 2
    if tick is not None:
        try:
            td = Decimal(str(tick).strip()).normalize()
            if td.is_finite() and td > 0 and td.as_tuple().exponent < 0:
                scale = max(scale, -td.as_tuple().exponent)
        except (InvalidOperation, ValueError, AttributeError):
            pass
    if x is not None:
        try:
            vd = Decimal(str(x).strip()).normalize()
            if vd.is_finite() and vd.as_tuple().exponent < 0:
                v_exp = -vd.as_tuple().exponent
                if v_exp <= 4:
                    scale = max(scale, v_exp)
        except (InvalidOperation, ValueError, AttributeError):
            pass
    return scale


def money(x, market="US", tick=None):
    curr = "\u20b9" if market == "IN" else "$"
    d = D(x)
    scale = _price_scale(d, tick=tick)
    q = Decimal("1").scaleb(-scale)
    return "%s%s" % (curr, format(d.quantize(q, rounding=ROUND_HALF_UP), ",f"))


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
    if tick_size is not None:
        if isinstance(tick_size, bool):
            raise PlanError("Invalid boolean tick_size %r" % (tick_size,))
        try:
            tick = Decimal(str(tick_size))
        except Exception:
            raise PlanError("Invalid tick_size %r" % (tick_size,))
        if not tick.is_finite() or tick <= 0:
            raise PlanError("Invalid non-positive tick_size %r" % (tick_size,))
    else:
        tick = TICK_IN if market == "IN" else CENT
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
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh, market=market, tick=tick), money(ol, market=market, tick=tick)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s%s" % (money(oh, market=market, tick=tick), money(ol, market=market, tick=tick), curr, num(raw))
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
        detail = "%s x (1 - %s%%) = %s%s" % (money(e, market=market, tick=tick), num(stop_param), curr, num(raw))
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
                        % (money(stop, market=market, tick=tick), label, money(e, market=market, tick=tick)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above %s0" % (money(stop, market=market, tick=tick), curr))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (money(risk, market=market, tick=tick), money(floor_risk.quantize(CENT, rounding=ROUND_HALF_UP), market=market), money(e, market=market, tick=tick)))
    if target_rule == "r":
        target = round_tick(e + target_param * risk, tick)
        t_label = r_label(target_param)
    else:
        target = round_tick(target_param, tick)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target, market=market, tick=tick), money(e, market=market, tick=tick)))
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


def load_chart(sym, interval, day, offline=None, market="US", cfg=None, now_utc=None, with_volume=False):
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
        rng = "5d" if (with_volume and back <= 0 and prov.provider_name != "offline") else (
            "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        )
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
            if with_volume:
                ch = data_in.to_entry_chart(payload)
                if now_utc is None and prov.supports(data_in.CAPABILITY_LIVE_QUOTES) and str(ch.get("provider") or "").lower() not in ("yahoo", "offline", "simulated"):
                    try:
                        q = prov.get_quote(sym, exchange=exch)
                        if isinstance(q, dict) and q.get("price") is not None:
                            ch["current_quote"] = q
                    except Exception:
                        pass
                return ch
            return data_in.to_exit_chart(payload)
    else:
        ref_utc = now_utc or utc_now()
        back = (to_local(ref_utc, market=market).date() - day).days
        rng = "5d" if (with_volume and back <= 0 and yahoo_get is _ORIG_YAHOO_GET) else (
            "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        )
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
        o_v, h, lo, v_v = at("open", i), at("high", i), at("low", i), at("volume", i)
        bar = {"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo), "c": float(c)}
        if with_volume:
            bar["o"] = float(c if o_v is None else o_v)
            bar["v"] = int(0 if v_v is None else v_v)
        bars.append(bar)
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
    out_ch = {"bars": bars, "data_time": dt_raw, "provider": "offline" if offline else "yahoo"}
    if offline:
        out_ch["is_offline"] = True
        out_ch["data_tier"] = "FIXTURE"
        out_ch["data_status"] = "FIXTURE"
        out_ch["is_live"] = False
    else:
        out_ch["data_tier"] = "DELAYED"
        out_ch["data_status"] = "DELAYED"
        out_ch["is_live"] = False
    return out_ch


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
        if isinstance(doc, dict):
            for k, v in doc.items():
                if k in DEFAULTS:
                    if v is not None:
                        cfg[k] = v
                elif v is not None:
                    cfg[k] = v
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
    raw_symbol = info["symbol"]
    doc_meta = info.get("doc") or {}
    cli_exch = getattr(a, "exchange", None)
    doc_exch = doc_meta.get("exchange") if isinstance(doc_meta, dict) else None
    if cli_exch and doc_exch and str(cli_exch).strip().upper() != str(doc_exch).strip().upper():
        raise PlanError(
            "CLI --exchange %s disagrees with entry document exchange %s for %s"
            % (str(cli_exch).strip().upper(), str(doc_exch).strip().upper(), info["symbol"])
        )
    req_exchange = cli_exch or doc_exch
    up_raw = str(raw_symbol or "").strip().upper()
    sym_exch = None
    if up_raw.endswith(".BO") or up_raw.startswith("BSE:"):
        sym_exch = "BSE"
    elif up_raw.endswith(".NS") or up_raw.startswith("NSE:"):
        sym_exch = "NSE"
    if sym_exch and req_exchange and sym_exch != str(req_exchange).strip().upper():
        raise PlanError(
            "Symbol exchange %s disagrees with requested exchange %s for %s"
            % (sym_exch, str(req_exchange).strip().upper(), raw_symbol)
        )
    if not req_exchange:
        req_exchange = sym_exch
    if market == "IN" and symbols_in:
        try:
            info["symbol"] = symbols_in.normalize_symbol(info["symbol"])
        except Exception:
            pass
    cfg_market = read_config(a.config, market=market) if market == "IN" else cfg
    if not req_exchange and market == "IN":
        req_exchange = cfg_market.get("exchange")
    if req_exchange:
        req_exchange = str(req_exchange).strip().upper()

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
    elif (
        market == "IN"
        and symbols_in
        and symbols_in.is_index_symbol(info["symbol"])
        and asset_type not in ("OPTIONS", "FUTURES")
        and not option_contract
    ):
        asset_type = "INDEX"
        idx_meta = symbols_in.get_index_metadata(info["symbol"])
        if idx_meta and not cli_exch and not doc_exch:
            req_exchange = idx_meta["exchange"]

    # Instrument metadata resolution (tick size, lot size, T2T)
    tick_size = None
    lot_size = 1
    resolved_exchange = req_exchange or ("NSE" if market == "IN" else None)
    if market == "IN":
        if str(asset_type).upper() == "INDEX":
            tick_size = TICK_IN
        doc_tick = doc_meta.get("tick_size") if isinstance(doc_meta, dict) and "tick_size" in doc_meta else None
        if isinstance(doc_meta, dict) and "tick_size" in doc_meta:
            if doc_tick is None or isinstance(doc_tick, bool):
                raise PlanError("Invalid tick_size %r in entry document for %s" % (doc_tick, info["symbol"]))
            try:
                doc_tick_dec = Decimal(str(doc_tick))
                if not doc_tick_dec.is_finite() or doc_tick_dec <= 0:
                    raise PlanError("Non-positive tick_size %r in entry document for %s" % (doc_tick, info["symbol"]))
            except PlanError:
                raise
            except Exception:
                raise PlanError("Invalid tick_size %r in entry document for %s" % (doc_tick, info["symbol"]))
            doc_tick = doc_tick_dec
        raw_inst_arg = getattr(a, "instruments", None)
        explicit_inst = raw_inst_arg is not None
        inst_path = raw_inst_arg or (getattr(instruments, "DEFAULT_CACHE_PATH", None) if instruments else None) or "trader/state/instruments.json"
        if explicit_inst and not os.path.exists(inst_path):
            raise PlanError("Instrument cache not found at: %s" % inst_path)
        if str(asset_type).upper() == "INDEX":
            tick_size = doc_tick or TICK_IN
        elif instruments and inst_path and os.path.exists(inst_path):
            instr_err = instruments.InstrumentMetadataError
            try:
                cache = instruments.load_instrument_cache(inst_path, max_age_days=30, as_of_date=info.get("date"))
                rec = instruments.resolve_instrument(
                    raw_symbol,
                    cache,
                    require_intraday=(str(trading_style).lower() == "intraday"),
                    exchange=req_exchange,
                )
                tick_size = Decimal(str(instruments.parse_tick_size(rec.get("tick_size"), symbol=info["symbol"])))
                if rec.get("exchange"):
                    rec_exch = str(rec["exchange"]).strip().upper()
                    if req_exchange and rec_exch != req_exchange:
                        raise PlanError(
                            "Resolved instrument exchange %s disagrees with requested exchange %s for %s"
                            % (rec_exch, req_exchange, info["symbol"])
                        )
                    resolved_exchange = rec_exch
                # Derivatives trade in F&O contract lots; cash equities trade in shares (lot_size=1)
                if asset_type in ("OPTIONS", "FUTURES"):
                    lot_size = rec.get("fo_lot_size") or rec.get("lot_size") or 1
                else:
                    lot_size = rec.get("lot_size") or 1
            except instr_err as exc:
                raise PlanError(str(exc))
            if doc_tick is not None and tick_size != doc_tick:
                raise PlanError(
                    "Entry document tick_size %s disagrees with instrument master tick_size %s (%s) for %s"
                    % (doc_tick, tick_size, resolved_exchange, info["symbol"])
                )
        elif doc_tick is not None:
            tick_size = doc_tick
        elif asset_type in ("OPTIONS", "FUTURES") and instruments:
            try:
                d_rec = instruments.resolve_derivative_contract(info["symbol"], exchange=req_exchange)
                tick_size = Decimal(str(instruments.parse_tick_size(d_rec.get("tick_size"), symbol=info["symbol"])))
                if d_rec.get("exchange"):
                    resolved_exchange = str(d_rec["exchange"]).strip().upper()
            except Exception as exc:
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
    if str(asset_type or "").upper() == "INDEX":
        plan["is_tradable_equity"] = False
        plan["analytical_only"] = True
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
        if resolved_exchange:
            plan["exchange"] = resolved_exchange
        if lot_size and lot_size > 1:
            plan["lot_size"] = lot_size
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    d = info.get("doc") or {}
    for k in ("is_market_open", "is_live_data", "is_historical_analysis", "is_live_signal",
              "is_fixture", "data_tier", "last_trading_session", "last_data_timestamp",
              "data_status", "is_simulated", "provider", "signal_freshness",
              "simulated_at", "clock_override", "candle_session"):
        if k in d:
            plan[k] = d[k]
    if chart_last_ts_iso is not None and not plan.get("last_data_timestamp"):
        plan["last_data_timestamp"] = chart_last_ts_iso
    if explicit_now or d.get("clock_override"):
        plan["clock_override"] = True
    if simulated:
        plan["simulated_at"] = _market_iso(now_utc, market=market)
    if chart_provider and "provider" not in plan:
        plan["provider"] = chart_provider
    if plan.get("clock_override"):
        plan["is_live_signal"] = False
        plan["is_live_data"] = False
        if str(plan.get("data_tier") or "").upper() == "CURRENT":
            plan["data_tier"] = "HISTORICAL"
            plan["is_historical_analysis"] = True
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
    if str(asset_type or "").upper() == "INDEX":
        warnings.append("Analytical plan for spot index (not a directly tradable equity); use index F&O derivatives for execution.")
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
    setup = {
        "orb": "opening-range breakout",
        "level": "close above a level",
        "breakout_20d": "20-day breakout",
        "ma_crossover": "moving-average crossover",
        "trend": "trend continuation",
        "rsi_momentum": "RSI momentum",
        "options_orb": "options opening-range breakout",
    }.get(plan["setup"], plan["setup"])
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
    p_tick = plan.get("tick_size")
    iv_labels = {"1m": "1-minute", "5m": "5-minute", "15m": "15-minute", "30m": "30-minute", "1h": "1-hour"}
    out = ["Exit plan: %s long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, iv_labels.get(plan["interval"], plan["interval"]))]
    out.append("  Entry         %s%s" % (money(plan["entry"], market=market, tick=p_tick), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"], market=market, tick=p_tick), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"], market=market, tick=p_tick), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (money(plan["risk_per_share"], market=market, tick=p_tick), pct))
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


# ---------------------------------------------------------------- intraday position manager

def _manage_ema(closes, period):
    if visuals is not None:
        return visuals.compute_ema(closes, period)
    n = int(period)
    if n <= 0 or not closes or len(closes) < n:
        return [None] * len(closes or [])
    vals = [None] * (n - 1)
    prev = sum(float(x) for x in closes[:n]) / float(n)
    vals.append(prev)
    k = 2.0 / float(n + 1)
    for c in closes[n:]:
        prev = (float(c) - prev) * k + prev
        vals.append(prev)
    return vals


def _manage_vwap(bars):
    if not bars:
        return None
    if visuals is not None:
        return visuals.compute_vwap(bars)
    tot_v = sum(float(b.get("v") or 0) for b in bars)
    if tot_v <= 0:
        return None
    out, cpv, cv = [], 0.0, 0.0
    for b in bars:
        v = float(b.get("v") or 0)
        tp = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        if v > 0:
            cpv += tp * v
            cv += v
        out.append((cpv / cv) if cv > 0 else None)
    return out


def _manage_rsi(closes, period=14):
    if visuals is not None:
        return visuals.compute_rsi(closes, period=period)
    return None


def _manage_tick(position, market="US", cfg=None, instruments_path=None, exchange=None):
    raw_tick = position.get("tick_size")
    if raw_tick is not None and not isinstance(raw_tick, bool):
        try:
            d = Decimal(str(raw_tick))
            if d.is_finite() and d > 0:
                return d
        except Exception:
            pass
    if market == "IN" and instruments is not None:
        inst_p = instruments_path or "trader/state/instruments.json"
        if os.path.exists(inst_p):
            try:
                master = instruments.load_master(inst_p)
                ex = exchange or position.get("exchange") or (cfg or {}).get("exchange") or "NSE"
                rec = instruments.resolve_instrument(position["symbol"], exchange=ex, master=master)
                if rec and rec.get("tick_size"):
                    return Decimal(str(rec["tick_size"]))
            except Exception:
                pass
    return TICK_IN if market == "IN" else CENT


def evaluate_position(position, chart, now_utc, interval="5m", market=None, cfg=None,
                      trail_atr_k=1.5, trail_activate_r=1.0, allow_partial=False,
                      partial_at_r=1.0, simulated=False, instruments_path=None):
    """Evaluate one open long position against current market conditions at now_utc.

    Returns dict with decision action:
      EXIT | PARTIAL EXIT | UPDATE TRAILING STOP | HOLD | NO_ACTION
    """
    if not isinstance(position, dict) or not position.get("symbol"):
        raise PlanError("position must contain 'symbol'")
    sym = str(position["symbol"]).upper()
    market = market or position.get("market") or ((cfg or {}).get("market")) or (
        "IN" if sym.endswith((".NS", ".BO", "-EQ")) else "US"
    )
    if str(market).upper() == "INDIA":
        market = "IN"
    cfg = cfg or dict(DEFAULTS_IN if market == "IN" else DEFAULTS)
    if interval not in INTERVALS:
        raise PlanError("--interval must be one of %s" % ", ".join(INTERVALS))
    step = INTERVALS[interval]
    tick = _manage_tick(position, market=market, cfg=cfg, instruments_path=instruments_path,
                        exchange=position.get("exchange"))
    local = to_local(now_utc, market=market)
    day = local.date()
    now_ts = int((now_utc.replace(tzinfo=None) - EPOCH).total_seconds())
    qty = float(position.get("shares") if position.get("shares") is not None else (position.get("qty") or 0))
    entry_px = float(position.get("avg_cost") if position.get("avg_cost") is not None else (position.get("entry") or 0))
    stop_px = position.get("stop")
    trailing_stop = position.get("trailing_stop")
    active_stop = None
    if stop_px is not None or trailing_stop is not None:
        cands = [float(x) for x in (stop_px, trailing_stop) if x is not None and float(x) > 0]
        active_stop = max(cands) if cands else None
    target_px = float(position["target"]) if position.get("target") is not None else None
    style = str(position.get("trading_style") or ("positional" if str(position.get("flat_by") or "").lower() == "positional" else cfg.get("trading_style") or "intraday")).lower()
    raw_asset = position.get("asset_type")
    if not raw_asset and market == "IN" and symbols_in and symbols_in.is_derivative_symbol(sym):
        dmeta = symbols_in.parse_derivative_symbol(sym)
        if dmeta:
            raw_asset = "OPTIONS" if dmeta.get("instrument_type") == "OPT" else "FUTURES"
    asset_type = str(raw_asset or "EQUITY").upper()
    lot_size = max(1, int(position.get("lot_size") or 1))
    expiry_str = position.get("expiry")
    flat_by_raw = position.get("flat_by")
    flat_by_time = None
    if style != "positional":
        fb_str = flat_by_raw if (flat_by_raw and str(flat_by_raw).lower() != "positional") else (
            cfg.get("flat_by") or (DEFAULTS_IN["flat_by"] if market == "IN" else DEFAULTS["flat_by"])
        )
        try:
            flat_by_time = parse_hhmm(fb_str, "flat_by", market=market)
        except PlanError:
            flat_by_time = parse_hhmm("15:20" if market == "IN" else "15:55", "flat_by", market=market)

    s = session(day, market=market)
    session_open = bool(s and s[0] <= local.time() < s[1])
    if style != "positional" and s and flat_by_time:
        close_dt = datetime.datetime.combine(day, s[1])
        fb_dt = datetime.datetime.combine(day, flat_by_time)
        if fb_dt >= close_dt:
            flat_by_time = (close_dt - datetime.timedelta(minutes=5)).time()

    # Extract completed intraday bars (up to now_ts) separated from forming bar and current quote
    raw_bars = (chart or {}).get("bars") or []
    completed_all = []
    session_bars_closed = []
    forming_bar = None
    for b in raw_bars:
        if b.get("t") is None or b.get("c") is None:
            continue
        t_val = int(b["t"])
        if t_val % step:
            continue
        et = to_local(utc_of(t_val), market=market)
        s_day = session(et.date(), market=market)
        if s_day and not (s_day[0] <= et.time() < s_day[1]):
            continue
        if t_val + step <= now_ts:
            nb = {
                "t": t_val, "et": et, "end": et + datetime.timedelta(seconds=step),
                "o": float(b.get("o") if b.get("o") is not None else b["c"]),
                "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"]),
                "v": float(b.get("v") or 0),
                "date": et.date().isoformat(),
            }
            completed_all.append(nb)
            if et.date() == day:
                session_bars_closed.append(nb)
        elif et.date() == day and t_val <= now_ts and forming_bar is None:
            forming_bar = {
                "bar_start": hm(et), "bar_end": hm(et + datetime.timedelta(seconds=step)),
                "close_so_far": float(b["c"]), "is_complete": False,
            }
    completed_all.sort(key=lambda x: x["t"])
    session_bars_closed.sort(key=lambda x: x["t"])

    q_raw = (chart or {}).get("current_quote")
    quote_px = None
    quote_ts = None
    if isinstance(q_raw, dict) and q_raw.get("price") is not None:
        q_t = q_raw.get("timestamp")
        if q_t is None or int(q_t) <= now_ts:
            quote_px = float(q_raw["price"])
            quote_ts = int(q_t) if q_t is not None else None

    last_closed = session_bars_closed[-1] if session_bars_closed else (completed_all[-1] if completed_all else None)
    curr_px = quote_px if quote_px is not None else (last_closed["c"] if last_closed else None)

    prov_name = str((chart or {}).get("provider") or "yahoo")
    d_tier = str((chart or {}).get("data_tier") or "DELAYED").upper()
    d_stat = str((chart or {}).get("provider_data_status") or (chart or {}).get("data_status") or d_tier).upper()
    is_fix = bool((chart or {}).get("is_offline"))
    is_sim = bool((chart or {}).get("is_simulated"))
    dt_val = (chart or {}).get("data_time")
    lag_sec = (now_ts - int(dt_val)) if dt_val is not None else (
        (now_ts - quote_ts) if quote_ts is not None else (
            (now_ts - (last_closed["t"] + step)) if last_closed else None
        )
    )
    is_stale = bool(
        session_open and not simulated and not is_fix and not is_sim
        and (d_stat == "STALE" or (lag_sec is not None and lag_sec > max(1800, step + 600)))
    )

    closes_all = [b["c"] for b in completed_all]
    ema9 = _manage_ema(closes_all, 9) if closes_all else []
    ema21 = _manage_ema(closes_all, 21) if closes_all else []
    vwap_s = _manage_vwap(session_bars_closed) if session_bars_closed else None
    rsi14 = _manage_rsi(closes_all, 14) if closes_all else None
    atr_val = (visuals.compute_atr(completed_all, 14) if (visuals is not None and len(completed_all) >= 15) else None)
    if atr_val is None and completed_all:
        atr_val, _ = atr_of(completed_all, ATR_BARS)

    ema9_v = ema9[-1] if ema9 else None
    ema21_v = ema21[-1] if ema21 else None
    vwap_v = vwap_s[-1] if vwap_s else None

    rps = (entry_px - float(stop_px)) if (stop_px is not None and entry_px > float(stop_px)) else (
        (entry_px - float(active_stop)) if (active_stop is not None and entry_px > float(active_stop)) else None
    )
    unrealized_pnl = round((curr_px - entry_px) * qty, 2) if (curr_px is not None and entry_px > 0 and qty > 0) else None
    r_mult = round((curr_px - entry_px) / rps, 2) if (curr_px is not None and rps and rps > 0) else None
    loss_cap_key = "max_loss_per_trade_inr" if market == "IN" else "max_loss_per_trade_usd"
    max_loss_cap = cfg.get(loss_cap_key) or cfg.get("max_loss_per_trade")

    action = "HOLD"
    reason_code = "TREND_INTACT"
    reasons = []
    recommended_stop = None
    exit_qty = None

    # 1. Hard session / expiry time constraints first (never depend on quote freshness)
    if style == "intraday" and s and flat_by_time:
        fb_dt = datetime.datetime.combine(day, flat_by_time)
        mins_to_flat = (fb_dt - local).total_seconds() / 60.0
        if mins_to_flat <= 5.0 and local.time() <= s[1]:
            action = "EXIT"
            reason_code = "INTRADAY_SQUARE_OFF"
            reasons.append("Intraday flat-by cutoff %s approached/reached (now %s)" % (hm(flat_by_time), hm(local)))
    if action == "HOLD" and asset_type in ("OPTIONS", "FUTURES") and expiry_str == day.isoformat() and s:
        exp_cutoff = parse_hhmm("15:15" if market == "IN" else "15:45", "expiry cutoff", market=market)
        if local.time() >= exp_cutoff:
            action = "EXIT"
            reason_code = "EXPIRY_APPROACHING"
            reasons.append("%s expires today (%s) and settlement cutoff %s reached" % (asset_type, expiry_str, hm(exp_cutoff)))

    # 2. Fail-closed on missing or stale data during open live session
    if action == "HOLD" and (curr_px is None or is_stale):
        action = "NO_ACTION"
        reason_code = "STALE_DATA" if is_stale else "NO_DATA"
        reasons.append(
            "Stale or unavailable intraday data (provider=%s, tier=%s, lag=%ss); manual check required before acting"
            % (prov_name, d_tier, lag_sec)
        )

    # 3. Stop-loss / trailing stop breach
    if action == "HOLD" and active_stop is not None and curr_px is not None:
        closed_stop_hit = bool(last_closed and (last_closed["c"] <= active_stop or last_closed["l"] <= active_stop))
        quote_stop_hit = bool(curr_px <= active_stop)
        if quote_stop_hit or closed_stop_hit:
            action = "EXIT"
            reason_code = "TRAILING_STOP_HIT" if (trailing_stop is not None and active_stop == float(trailing_stop) and (stop_px is None or float(trailing_stop) > float(stop_px))) else "STOP_HIT"
            reasons.append("Price breached active stop %.2f (current %.2f%s)" % (
                active_stop, curr_px, ", closed bar low %.2f" % last_closed["l"] if last_closed else ""
            ))

    # 4. Per-trade risk limit breach
    if action == "HOLD" and unrealized_pnl is not None and max_loss_cap and unrealized_pnl <= -abs(float(max_loss_cap)):
        action = "EXIT"
        reason_code = "RISK_LIMIT_BREACH"
        reasons.append("Unrealized loss %.2f exceeds max_loss_per_trade %.2f" % (unrealized_pnl, float(max_loss_cap)))

    # 5. Trend deterioration / VWAP breakdown on COMPLETED bars (any time of day)
    if action == "HOLD" and last_closed is not None and last_closed["date"] == day:
        bear_ema = (ema9_v is not None and ema21_v is not None and last_closed["c"] < ema21_v and ema9_v < ema21_v)
        below_vwap = (vwap_v is not None and last_closed["c"] < vwap_v)
        weak_rsi = (rsi14 is not None and rsi14 < 45.0)
        if (bear_ema and (below_vwap or vwap_v is None)) or (below_vwap and weak_rsi and len(session_bars_closed) >= 3):
            action = "EXIT"
            reason_code = "TREND_REVERSAL" if bear_ema else "VWAP_BREAKDOWN"
            reasons.append("Completed %s bar deteriorated: close %.2f (EMA9=%s, EMA21=%s, VWAP=%s, RSI=%s)" % (
                hm(last_closed["et"]), last_closed["c"],
                ("%.2f" % ema9_v) if ema9_v else "n/a",
                ("%.2f" % ema21_v) if ema21_v else "n/a",
                ("%.2f" % vwap_v) if vwap_v else "n/a",
                ("%.1f" % rsi14) if rsi14 else "n/a",
            ))

    # 6. Target & Partial Exit logic
    can_partial = bool(allow_partial and int(qty // lot_size) >= 2 and not position.get("partial_done"))
    if action == "HOLD" and target_px is not None and curr_px is not None:
        target_hit = (curr_px >= target_px) or bool(last_closed and last_closed["h"] >= target_px)
        if target_hit:
            bullish_run = bool(ema9_v and ema21_v and curr_px > ema9_v > ema21_v)
            if can_partial and bullish_run:
                half_lots = max(1, int(qty // lot_size) // 2)
                exit_qty = int(half_lots * lot_size)
                action = "PARTIAL EXIT"
                reason_code = "TARGET_PARTIAL_SCALE"
                cand_stop = float(floor_tick(entry_px, tick))
                if active_stop is None or cand_stop > active_stop:
                    recommended_stop = cand_stop
                reasons.append("Target %.2f reached while trend bullish; scale out %d units and raise stop to breakeven" % (
                    target_px, exit_qty))
            else:
                action = "EXIT"
                reason_code = "TARGET_HIT"
                reasons.append("Target %.2f achieved (current %.2f)" % (target_px, curr_px))

    if action == "HOLD" and can_partial and r_mult is not None and r_mult >= float(partial_at_r):
        half_lots = max(1, int(qty // lot_size) // 2)
        exit_qty = int(half_lots * lot_size)
        action = "PARTIAL EXIT"
        reason_code = "PROFIT_PARTIAL_SCALE"
        cand_stop = float(floor_tick(entry_px, tick))
        if active_stop is None or cand_stop > active_stop:
            recommended_stop = cand_stop
        reasons.append("Unrealized +%.2fR >= %.2fR; partial exit %d units and protect breakeven" % (
            r_mult, float(partial_at_r), exit_qty))

    # 7. Trailing stop advancement (strictly tighter / higher stop for long)
    if action == "HOLD" and curr_px is not None and r_mult is not None and r_mult >= float(trail_activate_r):
        trail_cands = []
        if entry_px > 0 and r_mult >= float(trail_activate_r):
            trail_cands.append(entry_px)
        if atr_val and atr_val > 0 and last_closed:
            trail_cands.append(last_closed["c"] - float(trail_atr_k) * float(atr_val))
        if ema21_v and ema21_v > 0 and curr_px > ema21_v:
            trail_cands.append(ema21_v - (0.1 * float(atr_val or 0)))
        valid_cands = [float(floor_tick(x, tick)) for x in trail_cands if x > 0 and x < curr_px - float(tick)]
        if valid_cands:
            best_trail = max(valid_cands)
            if active_stop is None or Decimal(str(best_trail)) >= Decimal(str(active_stop)) + tick:
                action = "UPDATE TRAILING STOP"
                reason_code = "TRAILING_STOP_ADVANCE"
                recommended_stop = best_trail
                reasons.append("Advance stop from %s to %.2f at +%.2fR" % (
                    ("%.2f" % active_stop) if active_stop else "none", best_trail, r_mult))

    if action == "HOLD" and not reasons:
        reasons.append("Position healthy above stop%s; trend intact" % (
            " (%.2f)" % active_stop if active_stop else ""))

    return {
        "symbol": sym,
        "market": market,
        "currency": "INR" if market == "IN" else "USD",
        "trading_style": style,
        "asset_type": asset_type,
        "action": action,
        "reason_code": reason_code,
        "reasons": reasons,
        "qty": qty,
        "exit_qty": exit_qty if action == "PARTIAL EXIT" else (qty if action == "EXIT" else 0),
        "entry": entry_px,
        "current_price": curr_px,
        "price_source": "current_quote" if quote_px is not None else ("completed_candle" if last_closed else "unavailable"),
        "active_stop": active_stop,
        "recommended_stop": recommended_stop,
        "target": target_px,
        "flat_by": "positional" if style == "positional" else (hm(flat_by_time) if flat_by_time else None),
        "unrealized_pnl": unrealized_pnl,
        "r_multiple": r_mult,
        "indicators": {
            "ema9": round(ema9_v, 4) if ema9_v else None,
            "ema21": round(ema21_v, 4) if ema21_v else None,
            "vwap": round(vwap_v, 4) if vwap_v else None,
            "rsi14": round(rsi14, 2) if rsi14 else None,
            "atr14": round(atr_val, 4) if atr_val else None,
        },
        "candle_context": {
            "completed_candle": {
                "bar_start": hm(last_closed["et"]), "bar_end": hm(last_closed["end"]),
                "close": last_closed["c"], "low": last_closed["l"], "high": last_closed["h"],
                "is_complete": True,
            } if last_closed else None,
            "forming_candle": forming_bar,
            "current_quote": {"price": quote_px, "timestamp": quote_ts} if quote_px is not None else None,
        },
        "data_tier": d_tier,
        "data_status": d_stat,
        "provider": prov_name,
        "evaluated_at": _market_iso(now_utc, market=market),
        "evaluation_mode": "on_invocation",
    }


def update_exit_state(state_path, evaluations, now_utc, market="US"):
    """Persist deduplicated exit decisions so repeated checks suppress duplicate HOLD/alert spam."""
    if not state_path:
        return evaluations
    state = {"positions": {}, "history": []}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, dict):
                    state["positions"] = dict(loaded.get("positions") or {})
                    state["history"] = list(loaded.get("history") or [])
        except Exception:
            pass
    stamp = _market_iso(now_utc, market=market)
    today = to_local(now_utc, market=market).date().isoformat()
    for ev in evaluations:
        sym = ev["symbol"]
        fp = "|".join(str(x) for x in (
            today, sym, ev.get("action"), ev.get("reason_code"),
            ev.get("recommended_stop"), ev.get("exit_qty"), ev.get("trading_style")
        ))
        prev = state["positions"].get(sym)
        if prev is None:
            chg = "NEW"
            dedup = False
        elif prev.get("fingerprint") == fp:
            chg = "UNCHANGED"
            dedup = True
        else:
            chg = "UPDATED"
            dedup = False
        state["positions"][sym] = {
            "fingerprint": fp, "action": ev.get("action"), "reason_code": ev.get("reason_code"),
            "active_stop": ev.get("recommended_stop") or ev.get("active_stop"),
            "last_evaluated_at": stamp,
        }
        if not dedup:
            state["history"].append({
                "at": stamp, "symbol": sym, "change": chg,
                "action": ev.get("action"), "reason_code": ev.get("reason_code"),
                "price": ev.get("current_price"),
            })
            state["history"] = state["history"][-200:]
        ev["signal_change"] = chg
        ev["deduplicated"] = dedup
    d = os.path.dirname(state_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")
    return evaluations


def _collect_open_positions(a, cfg, plan_doc=None):
    positions = []
    if getattr(a, "symbol", None):
        sym = a.symbol.upper()
        p = {
            "symbol": sym,
            "shares": float(getattr(a, "qty", None) or 1),
            "avg_cost": float(a.entry if getattr(a, "entry", None) is not None else ((plan_doc or {}).get("entry") or 0)),
            "stop": getattr(a, "stop_price", None) if getattr(a, "stop_price", None) is not None else (plan_doc or {}).get("stop"),
            "trailing_stop": getattr(a, "trailing_stop", None),
            "target": getattr(a, "target_price", None) if getattr(a, "target_price", None) is not None else (plan_doc or {}).get("target"),
            "trading_style": getattr(a, "trading_style", None) or (plan_doc or {}).get("trading_style") or cfg.get("trading_style") or "intraday",
            "asset_type": getattr(a, "asset_type", None) or (plan_doc or {}).get("asset_type") or "EQUITY",
            "expiry": getattr(a, "expiry", None) or ((plan_doc or {}).get("option_contract") or {}).get("expiry"),
            "exchange": getattr(a, "exchange", None) or (plan_doc or {}).get("exchange") or cfg.get("exchange"),
            "tick_size": (plan_doc or {}).get("tick_size"),
            "lot_size": (plan_doc or {}).get("lot_size") or 1,
            "flat_by": (plan_doc or {}).get("flat_by"),
        }
        positions.append(p)
        return positions
    pf_path = getattr(a, "portfolio", "trader/portfolio.json")
    if os.path.exists(pf_path):
        pf = read_json(pf_path, "portfolio")
        for pos in (pf.get("positions") or []) if isinstance(pf, dict) else []:
            sym = str(pos.get("symbol") or pos.get("ticker") or "").upper()
            if not sym or float(pos.get("shares") or 0) <= 0:
                continue
            merged = dict(pos)
            merged["symbol"] = sym
            if plan_doc and str(plan_doc.get("symbol") or "").upper() == sym:
                for k in ("stop", "target", "trading_style", "flat_by", "tick_size", "lot_size", "asset_type", "exchange"):
                    if merged.get(k) is None and plan_doc.get(k) is not None:
                        merged[k] = plan_doc[k]
                if not merged.get("expiry") and (plan_doc.get("option_contract") or {}).get("expiry"):
                    merged["expiry"] = plan_doc["option_contract"]["expiry"]
            positions.append(merged)
    return positions


def describe_management(ev):
    curr = "\u20b9" if ev.get("market") == "IN" else "$"
    chg = (" [%s]" % ev["signal_change"]) if ev.get("signal_change") else ""
    px_txt = ("%s%.2f" % (curr, ev["current_price"])) if ev.get("current_price") is not None else "unavailable"
    stp_txt = ("%s%.2f" % (curr, ev["active_stop"])) if ev.get("active_stop") is not None else "none"
    pnl_txt = ("%s%+.2f" % (curr, ev["unrealized_pnl"])) if ev.get("unrealized_pnl") is not None else "n/a"
    r_txt = ("%+.2fR" % ev["r_multiple"]) if ev.get("r_multiple") is not None else "n/a"
    lines = [
        "%s  %s (%s)%s | Current %s | Stop %s | P&L %s (%s) | Style %s" % (
            ev["symbol"], ev["action"], ev["reason_code"], chg,
            px_txt, stp_txt, pnl_txt, r_txt, ev.get("trading_style"),
        )
    ]
    for r in ev.get("reasons") or []:
        lines.append("  Reason: %s" % r)
    if ev.get("recommended_stop") is not None:
        lines.append("  New trailing stop: %s%.2f (never move stops further away)" % (curr, ev["recommended_stop"]))
    if ev["action"] == "EXIT":
        lines.append("  Next (paper gate): ticket.py flatten --symbol %s -> check.py -> yes -> ticket.approve" % ev["symbol"])
    elif ev["action"] == "PARTIAL EXIT":
        lines.append("  Next (paper gate): ticket.py flatten --symbol %s --qty %s -> check.py -> yes -> ticket.approve" % (
            ev["symbol"], int(ev.get("exit_qty") or 1)))
    return lines


def cmd_manage(a):
    cfg = read_config(a.config)
    market = get_market_profile(a, cfg=cfg)
    cfg = read_config(a.config, market=market)
    now_utc = parse_now(a.now, market=market)
    today = to_local(now_utc, market=market).date()
    plan_doc = read_json(a.plan, "plan") if (getattr(a, "plan", None) and os.path.exists(a.plan)) else None
    positions = _collect_open_positions(a, cfg, plan_doc=plan_doc)
    if not positions:
        if getattr(a, "json", False):
            print(json.dumps({"positions": [], "note": "No open positions found.", "evaluation_mode": "on_invocation"}, indent=2))
        else:
            print("No open positions found. Evaluated on-invocation (%s). Nothing was placed." % now_utc.strftime("%Y-%m-%d %H:%M:%SZ"))
        return 0
    results = []
    for pos in positions:
        p_market = get_market_profile(a, sym=pos["symbol"], doc=pos, cfg=cfg)
        ch = load_chart(
            pos["symbol"], a.interval, today, offline=a.offline, market=p_market,
            cfg=cfg, now_utc=now_utc if a.now else None, with_volume=True,
        )
        ev = evaluate_position(
            pos, ch, now_utc, interval=a.interval, market=p_market, cfg=cfg,
            trail_atr_k=a.trail_atr_k, trail_activate_r=a.trail_activate_r,
            allow_partial=bool(a.allow_partial), simulated=bool(a.now),
            instruments_path=getattr(a, "instruments", None),
        )
        results.append(ev)
    state_target = getattr(a, "state", None) or (DEFAULT_EXIT_STATE if getattr(a, "write", False) else None)
    if state_target and not getattr(a, "no_state", False):
        update_exit_state(state_target, results, now_utc, market=market)
    if getattr(a, "json", False):
        print(json.dumps({"positions": results, "evaluation_mode": "on_invocation"}, indent=2))
    else:
        print("Open position check (%s, evaluated on-invocation):" % _market_iso(now_utc, market=market))
        for ev in results:
            for line in describe_management(ev):
                print(line)
        print("Paper workflow only. Nothing was placed.")
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
    p.add_argument(
        "--setup",
        choices=["orb", "level", "breakout_20d", "ma_crossover", "trend", "rsi_momentum", "options_orb"],
    )
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:15 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["1m", "5m", "15m", "30m", "1h"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM market time (default: config flat_by, 15:55 ET or 15:20 IST)")
    p.add_argument("--asset-type", choices=["EQUITY", "FUTURES", "OPTIONS"], help="asset type")
    p.add_argument("--trading-style", choices=["intraday", "positional"], help="trading style")
    p.add_argument("--market", choices=["US", "IN"], help="market profile: US (default) or IN")
    p.add_argument("--exchange", choices=["NSE", "BSE"], help="exchange for Indian instruments (NSE or BSE)")
    p.add_argument("--instruments", default=None, help="path to instruments cache (default trader/state/instruments.json if present)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='override the application clock to this market time for backtests/tests, e.g. "2026-10-19 10:15" (marks signals non-live)')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--market", choices=["US", "IN"], help="market profile: US (default) or IN")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")

    for sub_name in ("manage", "check"):
        pm = sub.add_parser(sub_name, help="evaluate open positions for HOLD / EXIT / PARTIAL EXIT / UPDATE TRAILING STOP")
        pm.add_argument("--portfolio", default="trader/portfolio.json")
        pm.add_argument("--plan", default="trader/plan.json")
        pm.add_argument("--symbol", help="evaluate a single symbol")
        pm.add_argument("--qty", type=float, help="position size when evaluating ad-hoc --symbol")
        pm.add_argument("--entry", type=float, help="entry price override")
        pm.add_argument("--stop", dest="stop_price", type=float, help="stop price override")
        pm.add_argument("--trailing-stop", type=float, help="current trailing stop override")
        pm.add_argument("--target", dest="target_price", type=float, help="target price override")
        pm.add_argument("--trading-style", choices=["intraday", "positional"])
        pm.add_argument("--asset-type", choices=["EQUITY", "FUTURES", "OPTIONS"])
        pm.add_argument("--expiry", help="expiry date YYYY-MM-DD for derivatives")
        pm.add_argument("--interval", choices=["1m", "5m", "15m", "30m", "1h"], default="5m")
        pm.add_argument("--market", choices=["US", "IN"])
        pm.add_argument("--exchange", choices=["NSE", "BSE"])
        pm.add_argument("--instruments", default=None)
        pm.add_argument("--config", default="trader/config.json")
        pm.add_argument("--offline", metavar="PATH")
        pm.add_argument("--now")
        pm.add_argument("--trail-atr-k", type=float, default=1.5)
        pm.add_argument("--trail-activate-r", type=float, default=1.0)
        pm.add_argument("--allow-partial", action="store_true")
        pm.add_argument("--state", metavar="PATH")
        pm.add_argument("--no-state", action="store_true")
        pm.add_argument("--write", action="store_true")
        pm.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show, "manage": cmd_manage, "check": cmd_manage}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
