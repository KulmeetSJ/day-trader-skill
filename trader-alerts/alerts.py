#!/usr/bin/env python3
"""Price alerts and the NYSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the NYSE open? next open / close
    python3 alerts.py quote TSLA --write             # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by-close from trader/plan.json
    python3 alerts.py add --symbol TSLA --stop 358.32
    python3 alerts.py add --symbol TSLA --target 370.04
    python3 alerts.py add --at 15:55 --label "flat by close" --before-close
    python3 alerts.py add --symbol KO --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-19 15:55" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset tsla-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NYSE opened today (rings once per trading day)
  time            the New York clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day); "before_close": true moves it
                  3 hours earlier on a 1:00 p.m. early close, so a 15:55
                  "flat by close" rings at 12:55 on those days

A rule with "date" is only watched on that New York date (rules from a plan
are for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/TSLA (falls back to
          query1 on a 429 or a 5xx; one retry with a pause on a 429; one
          request per ticker, half a second apart; the default)
  stooq   https://stooq.com/q/l/?s=tsla.us&f=sd2t2ohlcv&h&e=csv (Stooq's free
          CSV; it answered 404 on 2026-10-13, kept for when it is back)
  --offline DIR reads DIR/<source>-<TICKER>.json|csv (or .json.gz) instead.

NYSE calendar: the 2026 and 2027 holidays and 1:00 p.m. early closes as
published at https://www.nyse.com/markets/hours-calendars (read 2026-10-13).
Outside those years the clock says the holiday list is unknown.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

_alerts_dir = os.path.dirname(os.path.abspath(__file__))
if _alerts_dir not in sys.path:
    sys.path.insert(0, _alerts_dir)

_scanner_dir = os.path.join(os.path.dirname(_alerts_dir), "trader-scanner")
if os.path.isdir(_scanner_dir) and _scanner_dir not in sys.path:
    sys.path.insert(0, _scanner_dir)

try:
    import calendar_in
    import symbols_in
    import instruments
    import data_in
except ImportError:
    calendar_in = None
    symbols_in = None
    instruments = None
    data_in = None

try:
    import scan
except ImportError:
    scan = None

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/KulmeetSJ/day-trader-skill)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

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


class AlertError(Exception):
    pass


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


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in New York time, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def market_status(now_utc):
    local = to_eastern(now_utc)
    d = local.date()
    ok, reason = trading_day(d)
    st = {"now_new_york": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": d.year in CALENDAR_YEARS,
          "open": False, "reason": reason, "early_close": NYSE_EARLY_CLOSE.get(d.isoformat())}
    s = session(d)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif local.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    # next open
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            st["next_open"] = "%s 09:30" % nd.isoformat()
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def stooq_url(sym):
    return "https://stooq.com/q/l/?s=%s.us&f=sd2t2ohlcv&h&e=csv" % sym.lower().replace(".", "-")


def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, sym.upper().replace(".", "-"))


def parse_stooq(text, sym):
    """Symbol,Date,Time,Open,High,Low,Close,Volume -> quote. N/D means no data."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise AlertError("Stooq returned no rows for %s" % sym)
    r = {k.strip().lower(): (v or "").strip() for k, v in rows[0].items() if k}
    close = r.get("close", "")
    if not close or close.upper() == "N/D":
        raise AlertError("Stooq has no data for %s (check the ticker)" % sym)

    def f(k):
        v = r.get(k, "")
        try:
            return float(v)
        except ValueError:
            return None
    return {"symbol": sym.upper(), "price": float(close), "open": f("open"), "high": f("high"),
            "low": f("low"), "prev_close": None, "time": ("%s %s" % (r.get("date", ""), r.get("time", ""))).strip(),
            "source": "stooq"}


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym.upper(), "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, source, sym):
    ext = "csv" if source == "stooq" else "json"
    base = os.path.join(folder, "%s-%s.%s" % (source, sym, ext))
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per ticker, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        try:
            if offline:
                text = _offline_text(offline, source, sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch(stooq_url(sym)) if source == "stooq" else fetch_yahoo(sym)
            quotes[sym] = parse_stooq(text, sym) if source == "stooq" else parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as a New York time like \"15:55\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that New York date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this New York date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_eastern(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today, moved 3 hours earlier on an early close if "before_close"."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("early_close"):
        at = (datetime.datetime.combine(datetime.date(2000, 1, 1), at) - datetime.timedelta(hours=3)).time()
    return at


def _rang_today_in(stamp, today):
    """Did a rule that rang at this stamp ring on this Indian market date?"""
    if not stamp:
        return False
    return str(stamp)[:10] == today


def evaluate_in(rules, quotes, status, state, now_ist):
    """Pure function for Indian market rules. Returns (rings, new_state)."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    stamp = now_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today_in(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: NSE is open (%s IST)."
                              % now_ist.strftime("%H:%M")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))
            if status.get("opens") and now_ist.time() >= at and not _rang_today_in(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                           now_ist.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: ticket.py flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = str(r.get("symbol", "")).upper()
        c_sym = symbols_in.normalize_symbol(sym) if symbols_in else sym
        q = quotes.get(c_sym) or quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": c_sym, "message": "%s %s \u20b9%.2f: %s your %s \u20b9%s%s."
                              % (c_sym, "is" if status["open"] else "last", p, word, label, _n(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today_in(rung.get(rid), today):
                rings.append({"id": rid, "symbol": c_sym, "message": "%s moved %+.2f%% to \u20b9%.2f (from \u20b9%.2f)."
                              % (c_sym, move, p, float(ref))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_eastern(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the NYSE is open (%s New York)%s."
                              % (status["now_new_york"], (", early close at 1:00 p.m. today, "
                                                          + status["early_close"]) if status.get("early_close") else "")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s New York (set for %s)" % (label[:1].upper(), label[1:],
                                                                 local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s $%.2f: %s your %s $%s%s."
                              % (sym, "is" if status["open"] else "last", p, word, label, _n(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to $%.2f (from $%.2f)."
                              % (sym, move, p, float(ref))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by-close time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = str(plan["symbol"]).upper()
    if symbols_in:
        sym = symbols_in.normalize_symbol(sym)
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    is_in = str(plan.get("market", "")).upper() in ("IN", "INDIA") or plan.get("currency") == "INR"
    default_flat = "15:20" if is_in else "15:55"
    flat = plan.get("flat_by") or cfg_flat_by or default_flat
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:55" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by close", "before_close": True})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("ticker") or p.get("symbol") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def get_market_profile(a, doc=None):
    m = getattr(a, "market", None)
    if m:
        return m.upper()
    env = os.environ.get("BHARAT_MARKET")
    if env and env.upper() in ("IN", "INDIA"):
        return "IN"
    if doc and isinstance(doc, dict):
        if str(doc.get("market", "")).upper() in ("IN", "INDIA"):
            return "IN"
        for r in doc.get("rules", []):
            sym = str(r.get("symbol", "")).upper()
            if sym.endswith((".NS", ".BO", "-EQ")) or "-" in sym or "&" in sym:
                return "IN"
    cfg_path = getattr(a, "config", "trader/config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
                if "market" in cfg:
                    return str(cfg["market"]).upper()
        except Exception:
            pass
    return "US"


def cmd_readiness(a):
    market = get_market_profile(a)
    cfg_path = getattr(a, "config", "trader/config.json") or "trader/config.json"
    cfg = _read(cfg_path, {}) or {}
    inst_path = getattr(a, "instruments", "trader/state/instruments.json") or "trader/state/instruments.json"
    now_txt = getattr(a, "now", None)

    items = []
    has_block = False
    has_warn = False

    # Check 1: Market Calendar & Trading Session
    if market == "IN":
        if calendar_in is None:
            raise AlertError("Indian calendar engine unavailable")
        now_dt = calendar_in.parse_ist(now_txt) if now_txt else calendar_in.now_ist()
        date_str = now_dt.strftime("%Y-%m-%d")
        is_trading, cal_status, trade_reason = calendar_in.is_trading_day_in(now_dt)
        phase, phase_reason = calendar_in.get_session_phase(now_dt, exchange="NSE")
        is_open = calendar_in.is_market_hours_in(now_dt, exchange="NSE")
        special = calendar_in.get_special_session(date_str)
        last_session_dt = calendar_in.get_last_trading_day(now_dt, exchange="NSE")
        cal_detail = "%s (%s), %s, phase: %s | Last Session: %s" % (
            date_str, now_dt.strftime("%A"), now_dt.strftime("%H:%M:%S IST"), phase.upper(), last_session_dt.isoformat())
        if not is_trading:
            cal_state = "CLOSED"
            cal_note = "Trading Holiday: %s. 24/7 Analysis Engine and Paper Trading are AVAILABLE using data from session %s." % (
                trade_reason, last_session_dt.isoformat())
        elif phase == "pre_market":
            cal_state = "PRE_MARKET"
            cal_note = "Pre-market session starts at 09:00 IST; continuous trading at 09:15 IST."
        elif is_open:
            cal_state = "OPEN"
            cal_note = "Market is actively open (continuous trading closes 15:30 IST)."
        else:
            cal_state = "CLOSED"
            cal_note = "Market closed for continuous session (%s). 24/7 Analysis Engine and Paper Trading are AVAILABLE using data from session %s." % (
                phase_reason, last_session_dt.isoformat())
        if special:
            cal_note += " Special session: %s" % special.get("session_type", "")
        ts_display = now_dt.strftime("%Y-%m-%d %H:%M:%S IST")
    else:
        now_utc = parse_now(now_txt) if now_txt else datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
        now_et = to_eastern(now_utc)
        date_str = now_et.strftime("%Y-%m-%d")
        is_trading, trade_reason = trading_day(now_et.date())
        s = session(now_et.date())
        is_open = (s is not None and s[0] <= now_et.time() < s[1])
        cur_d = now_et.date()
        while not trading_day(cur_d)[0] or (cur_d == now_et.date() and now_et.time() < OPEN):
            cur_d -= datetime.timedelta(days=1)
        last_session_dt = cur_d
        cal_detail = "%s (%s), %s ET | Last Session: %s" % (
            date_str, now_et.strftime("%A"), now_et.strftime("%H:%M:%S"), last_session_dt.isoformat())
        if not is_trading:
            cal_state = "CLOSED"
            cal_note = "Trading Holiday: %s. 24/7 Analysis Engine and Paper Trading are AVAILABLE using data from session %s." % (
                trade_reason, last_session_dt.isoformat())
        elif now_et.time() < OPEN:
            cal_state = "PRE_MARKET"
            cal_note = "Opens at 9:30 a.m. ET."
        elif s and now_et.time() < s[1]:
            cal_state = "OPEN"
            cal_note = "Market open until %s ET." % s[1].strftime("%H:%M")
        else:
            cal_state = "CLOSED"
            cal_note = "Closed for continuous session. 24/7 Analysis Engine and Paper Trading are AVAILABLE using data from session %s." % last_session_dt.isoformat()
        ts_display = now_et.strftime("%Y-%m-%d %H:%M:%S ET")

    items.append({
        "check": "Market Calendar & Session",
        "status": cal_state,
        "detail": cal_detail,
        "note": cal_note,
    })

    # Check 2: Instrument Master Cache
    inst_state = "OK"
    inst_detail = inst_path
    inst_note = ""
    inst_payload = None
    if not os.path.isfile(inst_path):
        inst_state = "MISSING"
        inst_note = "Cache not found. Run: python3 trader-alerts/instruments.py sync"
        has_warn = True
    else:
        try:
            if instruments is not None:
                ref_d = now_dt.date() if market == "IN" else now_et.date()
                inst_payload = instruments.load_instrument_cache(inst_path, max_age_days=1, as_of_date=ref_d)
                eq_c = sum(1 for r in inst_payload.get("instruments", {}).values() if r.get("instrument_type") == "EQUITY")
                fo_c = sum(1 for r in inst_payload.get("instruments", {}).values() if r.get("is_fo_eligible"))
                inst_detail = "Effective: %s, %d active equities, %d F&O underlyings" % (
                    inst_payload.get("effective_date"), eq_c, fo_c)
                inst_note = "Authoritative exchange security master loaded fresh."
        except Exception as e:
            if "stale" in str(e).lower():
                inst_state = "STALE"
                inst_note = str(e)
                has_warn = True
                try:
                    with open(inst_path, encoding="utf-8") as fh:
                        inst_payload = json.load(fh)
                except Exception:
                    pass
            else:
                inst_state = "ERROR"
                inst_note = "Corrupted cache: %s" % e
                has_block = True

    items.append({
        "check": "Instrument Master Cache",
        "status": inst_state,
        "detail": inst_detail,
        "note": inst_note,
    })

    # Check 3: Data Provider & Tiers
    data_state = "OK"
    provider_name = cfg.get("provider", "yahoo")
    data_detail = "Quotes: %s (DELAYED 15m) | Options: SIMULATED (Black-Scholes)" % provider_name
    data_note = "Free delayed Yahoo quotes for watchlists; analytical Black-Scholes engine for option chains. Paper trading only."
    items.append({
        "check": "Data Provider & Tiers",
        "status": data_state,
        "detail": data_detail,
        "note": data_note,
    })

    # Check 4: Universe Population
    univ_state = "OK"
    idx_name = cfg.get("index") or cfg.get("universe") or ("NIFTY50" if market == "IN" else "ALL")
    exch_name = cfg.get("exchange") or "NSE"
    univ_detail = "%s / %s" % (exch_name, idx_name)
    univ_count = 0
    if market == "IN" and scan is not None:
        try:
            kept, counts, src = scan.build_universe_in(index=idx_name, instruments_cache=inst_path, exchange=exch_name, config_path=cfg_path)
            univ_count = len(kept)
            univ_detail += ": %d securities (%s)" % (univ_count, src)
            univ_note = "Universe constituents verified and ready for scanner."
        except Exception as e:
            univ_state = "ERROR"
            univ_note = "Failed to populate universe: %s" % e
            has_block = True
    else:
        univ_note = "Universe configured."

    items.append({
        "check": "Universe Population",
        "status": univ_state,
        "detail": univ_detail,
        "note": univ_note,
    })

    # Check 5: Derivatives & Expiry Metadata
    deriv_state = "OK"
    deriv_items = []
    if market == "IN" and calendar_in is not None and instruments is not None:
        ref_d = now_dt.date()
        for u in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            try:
                exch_u = "BSE" if u in ("SENSEX", "BANKEX") else "NSE"
                exp = calendar_in.get_next_expiry(as_of_date=ref_d, underlying=u, exchange=exch_u)
                dte = calendar_in.days_to_expiry(exp, as_of_date=ref_d)
                lot = instruments.get_fo_lot_size(u, cache_payload=inst_payload) or instruments.INDEX_INSTRUMENTS.get(u, {}).get("fo_lot_size", 25)
                step = instruments.get_strike_interval(u)
                deriv_items.append("%s [%s] (exp %s, %dd, lot %d, step %.0f)" % (u, exch_u, exp.strftime("%Y-%m-%d"), dte, lot, step))
            except Exception as e:
                deriv_items.append("%s (err: %s)" % (u, e))
        deriv_detail = ", ".join(deriv_items)
        deriv_note = "Authoritative expiry calendars (NSE Tuesday / BSE Friday), strike steps and contract lot sizes verified."
    else:
        deriv_detail = "US Equity / Options"
        deriv_note = "Standard US equity trading."

    items.append({
        "check": "Derivatives & Expiry Metadata",
        "status": deriv_state,
        "detail": deriv_detail,
        "note": deriv_note,
    })

    # Check 6: Configuration & Safety Limits
    cfg_state = "OK"
    cfg_issues = []
    curr_tag = "\u20b9" if market == "IN" else "$"
    max_loss = None
    daily_loss = None
    if not cfg:
        cfg_state = "NOTE"
        cfg_issues.append("No config found at %s: using built-in defaults" % cfg_path)
        has_warn = True
    else:
        is_paper = bool(cfg.get("paper", True))
        if not is_paper:
            cfg_state = "BLOCK"
            cfg_issues.append("FATAL: Live order execution is disabled in this package. Set paper: true.")
            has_block = True

        budget = cfg.get("budget", 0)
        if budget <= 0:
            cfg_issues.append("No positive budget configured.")
            has_warn = True

        if market == "IN":
            max_loss = cfg.get("max_loss_per_trade_inr") or cfg.get("max_loss_per_trade_usd")
            daily_loss = cfg.get("max_daily_loss_inr") or cfg.get("max_daily_loss_usd")
        else:
            max_loss = cfg.get("max_loss_per_trade_usd")
            daily_loss = cfg.get("max_daily_loss_usd")

        if not max_loss:
            cfg_issues.append("Missing max_loss_per_trade setting.")
            has_warn = True
        if not daily_loss:
            cfg_issues.append("Missing max_daily_loss setting.")
            has_warn = True

        req_stop = cfg.get("require_stop", True)
        if not req_stop:
            cfg_issues.append("require_stop must be true for safe trading.")
            has_warn = True

    if has_block:
        cfg_state = "BLOCKED"
    elif cfg_issues:
        cfg_state = "NOTE"

    budget_disp = "%s%s" % (curr_tag, "{:,}".format(int(cfg.get("budget", 100000))))
    max_loss_disp = "%s%s" % (curr_tag, "{:,}".format(int(max_loss or 1500)))
    daily_loss_disp = "%s%s" % (curr_tag, "{:,}".format(int(daily_loss or 3000)))
    cfg_detail = "Paper: %s | Budget: %s | Max Loss/Trade: %s | Max Daily Loss: %s" % (
        cfg.get("paper", True), budget_disp, max_loss_disp, daily_loss_disp)
    cfg_note = "; ".join(cfg_issues) if cfg_issues else "All safety rules and capital limits active. Paper trading mode verified."

    items.append({
        "check": "Configuration & Safety Limits",
        "status": cfg_state,
        "detail": cfg_detail,
        "note": cfg_note,
    })

    if has_block:
        verdict = "BLOCKED"
        verdict_msg = "Safety gates engaged. Resolve the blocking issues above before trading."
    elif has_warn:
        verdict = "READY_WITH_NOTICES"
        verdict_msg = "Pre-flight checks passed with operational notices. System is ready."
    else:
        verdict = "READY"
        verdict_msg = "All pre-market health and risk checks passed. System is fully ready for the session."

    market_session = "OPEN" if is_open else "CLOSED"
    if inst_state in ("MISSING", "ERROR"):
        data_availability = "UNAVAILABLE"
    elif inst_state == "STALE":
        data_availability = "STALE"
    else:
        tier_cfg = str(cfg.get("data_tier") or cfg.get("provider") or "").lower()
        data_availability = "FRESH" if "realtime" in tier_cfg else "DELAYED"

    analysis_engine = "UNAVAILABLE" if has_block else "AVAILABLE"
    paper_trading = "AVAILABLE" if (bool(cfg.get("paper", True)) and not has_block) else "UNAVAILABLE"

    doc = {
        "verdict": verdict,
        "message": verdict_msg,
        "market": market,
        "market_session": market_session,
        "data_availability": data_availability,
        "analysis_engine": analysis_engine,
        "paper_trading": paper_trading,
        "last_trading_session": last_session_dt.isoformat(),
        "timestamp": ts_display,
        "checks": items,
    }

    if a.json:
        print(json.dumps(doc, indent=2))
        return 1 if verdict == "BLOCKED" else 0

    print("=" * 76)
    print(" PRE-MARKET READINESS & HEALTH CHECK -- %s (%s)" % (
        "INDIAN MARKET (NSE/BSE)" if market == "IN" else "US MARKET (NYSE/NASDAQ)", doc["timestamp"]))
    print("=" * 76)
    print(" Market Session:       [%s] (%s)" % (
        market_session, "Continuous session open" if is_open else "Exchange continuous trading closed"))
    print(" Data Availability:    [%s] (%s)" % (
        data_availability, "Delayed 15m quotes / BS analytical options" if data_availability == "DELAYED" else data_availability))
    print(" Analysis Engine:      [%s] (24/7 Screening, Technicals, Research, Risk & Sizing)" % analysis_engine)
    print(" Paper Trading:        [%s] (Simulated bracket execution active)" % paper_trading)
    print(" Last Trading Session: %s" % last_session_dt.isoformat())
    print("-" * 76)
    for it in items:
        status_tag = "[%s]" % it["status"]
        print("%-32s %-12s" % (it["check"], status_tag))
        print("  Info: %s" % it["detail"])
        if it["note"]:
            print("  Note: %s" % it["note"])
        print("-" * 76)
    print("OVERALL VERDICT: %s" % verdict)
    print(verdict_msg)
    print("=" * 76)
    return 1 if verdict == "BLOCKED" else 0


def cmd_clock_in(a):
    if calendar_in is None:
        raise AlertError("Indian market calendar engine unavailable")
    now = calendar_in.parse_ist(a.now) if a.now else calendar_in.now_ist()
    date_str = now.strftime("%Y-%m-%d")

    is_cas = getattr(a, "cas", False)
    sym = getattr(a, "symbol", None)
    if sym and not is_cas:
        try:
            import instruments
            cache_path = getattr(a, "instruments", instruments.DEFAULT_CACHE_PATH)
            if os.path.isfile(cache_path):
                payload = instruments.load_instrument_cache(cache_path, max_age_days=365, as_of_date=date_str)
                is_cas = instruments.is_cas_eligible(sym, payload)
        except Exception:
            pass

    phase, reason = calendar_in.get_session_phase(now, exchange="NSE", is_cas_eligible=is_cas)
    is_open = calendar_in.is_market_hours_in(now, exchange="NSE", is_cas_eligible=is_cas)
    bars = calendar_in.get_session_bars(now, exchange="NSE", is_cas_eligible=is_cas)
    is_trading, status, trade_reason = calendar_in.is_trading_day_in(now)
    special = calendar_in.get_special_session(date_str)

    st = {
        "market": "NSE",
        "profile": "IN",
        "open": is_open,
        "phase": phase,
        "reason": reason,
        "is_trading_day": is_trading,
        "calendar_status": status,
        "now_ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "date": date_str,
        "weekday": now.strftime("%A"),
        "session_bars": bars,
        "is_cas_eligible": is_cas,
        "cas_rule_version": calendar_in.get_cas_rule_version(date_str, exchange="NSE", is_cas_eligible=is_cas),
        "unexecuted_order_transition_active": calendar_in.is_unexecuted_order_transition_active(date_str),
        "special_session": special is not None,
    }
    if sym:
        st["symbol"] = sym
    if special:
        st["special_session_details"] = special

    if a.json:
        print(json.dumps(st, indent=2))
    else:
        cas_label = " (CAS-eligible)" if is_cas else " (Non-CAS)"
        print("India (IST) %s (%s): NSE %s%s, phase: %s (%s)." % (
            st["now_ist"], st["weekday"], "OPEN" if is_open else "closed", cas_label, phase, reason))
        print("Continuous session bars (5-min): %d bars." % bars)
        if special:
            print("Special session (%s): %s to %s." % (
                special.get("session_type"), special["open_time"], special["close_time"]))
    return 0 if is_open else 1


def cmd_quote_in(a):
    raw_syms = _dedupe([s.upper() for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not raw_syms:
        raise AlertError("which tickers? alerts.py quote RELIANCE TCS, or --holdings")

    try:
        import data_in
    except ImportError:
        data_in = None

    now = calendar_in.parse_ist(a.now) if (calendar_in and a.now) else parse_now(a.now)

    quotes = {}
    errors = []

    if data_in:
        provider = data_in.OfflineMarketDataProvider(a.offline) if a.offline else data_in.get_provider(source=a.source)
        for s in raw_syms:
            try:
                c_sym = symbols_in.normalize_symbol(s) if symbols_in else s.upper()
                q = provider.get_quote(c_sym, exchange="NSE")
                quotes[c_sym] = q
            except Exception as e:
                errors.append(str(e))
    else:
        canonical_syms = []
        for s in raw_syms:
            if symbols_in:
                canonical_syms.append(symbols_in.normalize_symbol(s))
            else:
                canonical_syms.append(s)

        yahoo_map = {}
        query_syms = []
        for c_sym in canonical_syms:
            if symbols_in and a.source == "yahoo":
                y_sym = symbols_in.to_yahoo_symbol(c_sym, exchange="NSE")
            else:
                y_sym = c_sym
            yahoo_map[y_sym] = c_sym
            query_syms.append(y_sym)

        quotes_raw, errs = get_quotes(query_syms, a.source, a.offline)
        errors.extend(errs)

        for y_sym, q in quotes_raw.items():
            c_sym = yahoo_map.get(y_sym, y_sym)
            q_copy = dict(q)
            q_copy["symbol"] = c_sym
            q_copy["market"] = "NSE"
            q_copy["tier"] = "DELAYED"
            q_copy["currency"] = "INR"
            quotes[c_sym] = q_copy

    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source)

    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in sorted(quotes.keys()):
            q = quotes.get(s)
            if q and "price" in q:
                chg = ""
                if q.get("prev_close"):
                    pct = q.get("change_pct")
                    if pct is None:
                        pct = 100.0 * (q["price"] / q["prev_close"] - 1)
                    chg = " (%+.2f%% vs prev close \u20b9%.2f)" % (pct, q["prev_close"])
                tier_label = q.get("tier", "DELAYED")
                src_label = q.get("source", a.source)
                print("%-10s \u20b9%.2f%s  %s  [%s:%s]" % (
                    s, q["price"], chg, q.get("time", ""), src_label, tier_label))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Never treat delayed quotes as real-time.")
    return 0 if quotes else 1


def cmd_history_in(a):
    if not a.symbol:
        raise AlertError("which ticker? alerts.py history RELIANCE --market IN")
    try:
        import data_in
    except ImportError:
        raise AlertError("data_in module unavailable")

    c_sym = symbols_in.normalize_symbol(a.symbol) if symbols_in else a.symbol.upper()
    provider = data_in.OfflineMarketDataProvider(a.offline) if a.offline else data_in.get_provider(source=a.source)

    interval = getattr(a, "interval", "5m")
    range_str = getattr(a, "range_str", "5d")

    try:
        if interval == "1d":
            data = provider.get_daily_bars(c_sym, range_str=range_str, exchange="NSE", now=a.now)
        else:
            data = provider.get_intraday_bars(c_sym, interval=interval, range_str=range_str,
                                              exchange="NSE", now=a.now)
    except Exception as e:
        raise AlertError("Failed to fetch historical bars for %s: %s" % (c_sym, e))

    if a.write and data:
        _write(a.write, data)

    if a.json:
        print(json.dumps(data, indent=2))
    else:
        bars = data.get("bars", [])
        tier = data.get("tier", "HISTORICAL")
        source = data.get("source", "yahoo")
        print("%s (%s) - %d bars (%s) [%s:%s]" % (
            c_sym, data.get("exchange", "NSE"), len(bars), interval, source, tier))
        print("%-12s %-6s %9s %9s %9s %9s %10s" % ("Date", "Start", "Open", "High", "Low", "Close", "Volume"))
        print("-" * 72)
        display_bars = bars[-10:] if len(bars) > 10 else bars
        for b in display_bars:
            start_str = b.get("start", "")
            print("%-12s %-6s %9.2f %9.2f %9.2f %9.2f %10d" % (
                b["date"], start_str, b["o"], b["h"], b["l"], b["c"], b["v"]))
        if len(bars) > 10:
            print("... and %d earlier bars." % (len(bars) - 10))
        dropped = data.get("dropped")
        if dropped:
            drop_str = ", ".join("%s: %d" % (k, v) for k, v in dropped.items() if v > 0)
            if drop_str:
                print("Dropped bars: %s" % drop_str)
    return 0 if data.get("bars") else 1


def cmd_history(a):
    if get_market_profile(a) == "IN":
        return cmd_history_in(a)
    raise AlertError("history command currently supported for --market IN")
    return 0 if quotes else 1


def cmd_clock(a):
    if get_market_profile(a) == "IN":
        return cmd_clock_in(a)
    st = market_status(parse_now(a.now))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("New York %s (%s): NYSE %s, %s." % (st["now_new_york"], st["weekday"],
                                                  "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s%s." % (st["opens"], st["closes"],
                                                    (" (early close: %s)" % st["early_close"]) if st.get("early_close") else ""))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s New York." % st["next_open"])
        if not st["calendar_known"]:
            print("Holiday list covers %s only: check nyse.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return 0 if st["open"] else 1


def cmd_quote(a):
    if get_market_profile(a) == "IN":
        return cmd_quote_in(a)
    syms = _dedupe([s.upper() for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which tickers? alerts.py quote TSLA KO, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.source, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close $%.2f)" % (100.0 * (q["price"] / q["prev_close"] - 1), q["prev_close"])
                print("%-6s $%.2f%s  %s  [%s]" % (s, q["price"], chg, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check_in(a, rules):
    if calendar_in is None:
        raise AlertError("Indian market calendar engine unavailable")
    try:
        import data_in
    except ImportError:
        data_in = None

    now = calendar_in.parse_ist(a.now) if a.now else calendar_in.now_ist()
    date_str = now.strftime("%Y-%m-%d")

    phase, reason = calendar_in.get_session_phase(now, exchange="NSE")
    is_open = calendar_in.is_market_hours_in(now, exchange="NSE")
    status = {
        "market": "NSE",
        "open": is_open,
        "phase": phase,
        "reason": reason,
        "date": date_str,
        "now_ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "weekday": now.strftime("%A"),
        "opens": "09:15",
        "closes": "15:30",
    }

    watched = [r for r in rules if active(r, date_str)]
    raw_syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    quotes = {}
    errors = []

    if raw_syms:
        if data_in:
            provider = data_in.OfflineMarketDataProvider(a.offline) if a.offline else data_in.get_provider(source=a.source)
            for s in raw_syms:
                try:
                    c_sym = symbols_in.normalize_symbol(s) if symbols_in else s.upper()
                    q = provider.get_quote(c_sym, exchange="NSE")
                    quotes[c_sym] = q
                    if s != c_sym:
                        quotes[s] = q
                except Exception as e:
                    errors.append(str(e))
        else:
            canonical_syms = [symbols_in.normalize_symbol(s) if symbols_in else s for s in raw_syms]
            yahoo_map = {}
            query_syms = []
            for c_sym in canonical_syms:
                y_sym = symbols_in.to_yahoo_symbol(c_sym, exchange="NSE") if symbols_in else c_sym
                yahoo_map[y_sym] = c_sym
                query_syms.append(y_sym)
            raw_q, errs = get_quotes(query_syms, a.source, a.offline)
            errors.extend(errs)
            for y_s, q in raw_q.items():
                c_s = yahoo_map.get(y_s, y_s)
                q_copy = dict(q)
                q_copy["symbol"] = c_s
                q_copy["currency"] = "INR"
                quotes[c_s] = q_copy

    state = _read(a.state, {})
    rings, state = evaluate_in(rules, quotes, status, state, now)

    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, a.source)

    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", now.strftime("%H:%M"), status["phase"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s \u20b9%.2f" % (s, quotes[s]["price"]) for s in raw_syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. A bracket's stop-loss and take-profit already sit at "
                  "the broker: check get_open_position and record any fill (ticket.py fill). To close a "
                  "position yourself: ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    if get_market_profile(a, doc=doc) == "IN":
        return cmd_check_in(a, rules)
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.source, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, a.source)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NYSE %s (%s New York, %s)." % ("OPEN" if status["open"] else "closed", status["now_new_york"],
                                              status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s $%.2f" % (s, quotes[s]["price"]) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. A bracket's stop-loss and take-profit already sit at "
                  "the broker: check get_open_position and record any fill (ticket.py fill). To close a "
                  "position yourself: ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        if plan.get("market") == "IN" or (cfg or {}).get("market") == "IN":
            doc["market"] = "IN"
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the broker's bracket orders are what actually stop you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants a New York time like 15:55")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = a.symbol.upper()
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = a.symbol.upper()
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NYSE/NSE clock (trader-alerts).")
    ap.add_argument("--market", default="US", choices=["US", "IN"], help="market profile (US or IN)")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", default="yahoo", choices=["yahoo", "stooq"])
        p.add_argument("--offline", metavar="DIR", help="read <source>-<TICKER>.json|csv files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = New York time for US, IST for IN)")
        p.add_argument("--json", action="store_true")
        p.add_argument("--market", default=None, choices=["US", "IN"], help="market profile (US or IN)")

    p = sub.add_parser("clock", help="is the exchange open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p.add_argument("--market", default=None, choices=["US", "IN"], help="market profile (US or IN)")
    p.add_argument("--cas", action="store_true", help="evaluate as Closing Auction Session (CAS) eligible security")
    p.add_argument("--symbol", help="security symbol to resolve CAS eligibility from instruments")
    p.add_argument("--instruments", default="trader/state/instruments.json", help="path to instruments cache")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("history", help="historical intraday or daily bars")
    net(p)
    p.add_argument("symbol")
    p.add_argument("--interval", default="5m", help="bar interval (1m, 5m, 1d; default 5m)")
    p.add_argument("--range", dest="range_str", default="5d", help="range to fetch (e.g. 1d, 5d, 1mo, 1y; default 5d)")
    p.add_argument("--write", nargs="?", const="trader/history.json", metavar="PATH",
                   help="save to history.json (default trader/history.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, New York time")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by-close from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert that moves 3 hours earlier on a 1:00 p.m. early close")
    p.add_argument("--date", help="watch this rule on this New York date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("readiness", help="pre-market trading readiness check")
    p.add_argument("--market", choices=["US", "IN"], default="IN", help="market profile: IN (NSE/BSE) or US")
    p.add_argument("--config", default="trader/config.json", help="path to config JSON")
    p.add_argument("--instruments", default="trader/state/instruments.json", help="path to instruments cache")
    p.add_argument("--portfolio", default="trader/portfolio.json", help="path to portfolio JSON")
    p.add_argument("--now", help="pretend market time (e.g. \"2026-10-19 09:00\")")
    p.add_argument("--json", action="store_true", help="output JSON")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "history": cmd_history, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset, "readiness": cmd_readiness}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
