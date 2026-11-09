#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan thousands of charts for setups. Standard library only.

    python3 scan.py universe                          # Nasdaq Trader lists -> trader/universe.txt
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols TSLA,NVDA,AMD     # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:50 AM ET: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 AM ET
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-19 10:15" --min-price 0

universe  Downloads Nasdaq Trader's symbol directories (nasdaqlisted.txt and
          otherlisted.txt) and keeps US-listed common stocks, ordinary shares
          and ADRs. The rule, in order:
            1. not a test issue, not an ETF, not NextShares
            2. a plain ticker: 1 to 5 letters, or 1 to 4 letters plus one
               class letter after a dot (BRK.B); a .U / .W / .R suffix
               (units, warrants, rights) is out, and so is a $ preferred
            3. out, whatever else the name says: warrants; SPAC units and
               rights ("Units, each consisting of", "Rights to receive");
               notes, ETNs, debentures; a % coupon; "Depositary Shares" that
               are not American or Global (those carry a preferred)
            4. in: the name calls it common stock / shares / units, ordinary
               shares, shares of beneficial interest, ADS / ADR / American or
               Global depositary shares, or partnership / trust units
            5. out: the name still says unit, right, bond, preferred or preference
            6. in: everything else (plain names like "AMETEK, Inc.")

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (5) and regularMarketVolume >=
          --min-volume (1,000,000). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the session's close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session. Stage 1: spark closes for every
          symbol (1 day of 5-minute bars, 20 a request); a chart goes on only
          if a completed bar after the range closed at or above the highest
          close of the range (a necessary condition). Stage 2: each candidate's
          v8 chart (real highs and lows): range high = the highest high of the
          range bars, a hit = the first completed bar to CLOSE above it, at that
          bar's close time. Stage 2 fetches at most --max-confirm (200) charts,
          the most traded candidates first (price x volume so far), and says
          so when it stops short. A chart is usable only when every 5-minute
          bar from 9:30 to the cutoff is there (a missing bar is a halt or no
          trades). Floors: --min-price 5 (last close), --min-volume 0.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are New York (ET). --now pins the
clock ("2026-10-19 10:15", New York time; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with nasdaqlisted.txt and otherlisted.txt; daily and orb take a folder of
<T>.json v8 chart files or spark responses, or one file that is a spark
response, a v8 chart, or a bundle {"TSLA": <v8 json>, ...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
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

# Sibling module resolution for Indian market engine
_alerts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-alerts")
if os.path.isdir(_alerts_dir) and _alerts_dir not in sys.path:
    sys.path.insert(0, _alerts_dir)

try:
    import symbols_in
    import calendar_in
    import data_in
    import instruments
except ImportError:
    symbols_in = None
    calendar_in = None
    data_in = None
    instruments = None

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NASDAQ_FILES = ("nasdaqlisted.txt", "otherlisted.txt")
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

NSE_OPEN = datetime.time(9, 15)
NSE_CLOSE = datetime.time(15, 30)

NIFTY_50_CONSTITUENTS = [
    ("ADANIENT", "Adani Enterprises Ltd."),
    ("ADANIPORTS", "Adani Ports and Special Economic Zone Ltd."),
    ("APOLLOHOSP", "Apollo Hospitals Enterprise Ltd."),
    ("ASIANPAINT", "Asian Paints Ltd."),
    ("AXISBANK", "Axis Bank Ltd."),
    ("BAJAJ-AUTO", "Bajaj Auto Ltd."),
    ("BAJFINANCE", "Bajaj Finance Ltd."),
    ("BAJAJFINSV", "Bajaj Finserv Ltd."),
    ("BEL", "Bharat Electronics Ltd."),
    ("BPCL", "Bharat Petroleum Corporation Ltd."),
    ("BHARTIARTL", "Bharti Airtel Ltd."),
    ("BRITANNIA", "Britannia Industries Ltd."),
    ("CIPLA", "Cipla Ltd."),
    ("COALINDIA", "Coal India Ltd."),
    ("DRREDDY", "Dr. Reddy's Laboratories Ltd."),
    ("EICHERMOT", "Eicher Motors Ltd."),
    ("GRASIM", "Grasim Industries Ltd."),
    ("HCLTECH", "HCL Technologies Ltd."),
    ("HDFCBANK", "HDFC Bank Ltd."),
    ("HDFCLIFE", "HDFC Life Insurance Company Ltd."),
    ("HEROMOTOCO", "Hero MotoCorp Ltd."),
    ("HINDALCO", "Hindalco Industries Ltd."),
    ("HINDUNILVR", "Hindustan Unilever Ltd."),
    ("ICICIBANK", "ICICI Bank Ltd."),
    ("ITC", "ITC Ltd."),
    ("INDUSINDBK", "IndusInd Bank Ltd."),
    ("INFY", "Infosys Ltd."),
    ("JSWSTEEL", "JSW Steel Ltd."),
    ("KOTAKBANK", "Kotak Mahindra Bank Ltd."),
    ("LT", "Larsen & Toubro Ltd."),
    ("M&M", "Mahindra & Mahindra Ltd."),
    ("MARUTI", "Maruti Suzuki India Ltd."),
    ("NTPC", "NTPC Ltd."),
    ("NESTLEIND", "Nestle India Ltd."),
    ("ONGC", "Oil & Natural Gas Corporation Ltd."),
    ("POWERGRID", "Power Grid Corporation of India Ltd."),
    ("RELIANCE", "Reliance Industries Ltd."),
    ("SBILIFE", "SBI Life Insurance Company Ltd."),
    ("SHRIRAMFIN", "Shriram Finance Ltd."),
    ("SBIN", "State Bank of India"),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd."),
    ("TCS", "Tata Consultancy Services Ltd."),
    ("TATACONSUM", "Tata Consumer Products Ltd."),
    ("TATAMOTORS", "Tata Motors Ltd."),
    ("TATASTEEL", "Tata Steel Ltd."),
    ("TECHM", "Tech Mahindra Ltd."),
    ("TITAN", "Titan Company Ltd."),
    ("TRENT", "Trent Ltd."),
    ("ULTRACEMCO", "UltraTech Cement Ltd."),
    ("WIPRO", "Wipro Ltd."),
]

NIFTY_NEXT_50_CONSTITUENTS = [
    ("ABB", "ABB India Ltd."),
    ("ADANIENSOL", "Adani Energy Solutions Ltd."),
    ("ADANIGREEN", "Adani Green Energy Ltd."),
    ("ADANIPOWER", "Adani Power Ltd."),
    ("ATGL", "Adani Total Gas Ltd."),
    ("AMBUJACEM", "Ambuja Cements Ltd."),
    ("BANKBARODA", "Bank of Baroda"),
    ("BERGEPAINT", "Berger Paints India Ltd."),
    ("BOSCHLTD", "Bosch Ltd."),
    ("CANBK", "Canara Bank"),
    ("CHOLAFIN", "Cholamandalam Investment and Finance Company Ltd."),
    ("COLPAL", "Colgate-Palmolive (India) Ltd."),
    ("DLF", "DLF Ltd."),
    ("DABUR", "Dabur India Ltd."),
    ("DIVISLAB", "Divi's Laboratories Ltd."),
    ("GAIL", "GAIL (India) Ltd."),
    ("GODREJCP", "Godrej Consumer Products Ltd."),
    ("HAVELLS", "Havells India Ltd."),
    ("HAL", "Hindustan Aeronautics Ltd."),
    ("ICICIGI", "ICICI Lombard General Insurance Company Ltd."),
    ("ICICIPRULI", "ICICI Prudential Life Insurance Company Ltd."),
    ("IOC", "Indian Oil Corporation Ltd."),
    ("IRCTC", "Indian Railway Catering and Tourism Corporation Ltd."),
    ("IRFC", "Indian Railway Finance Corporation Ltd."),
    ("JINDALSTEL", "Jindal Steel & Power Ltd."),
    ("JIOFIN", "Jio Financial Services Ltd."),
    ("LICI", "Life Insurance Corporation of India"),
    ("LTIM", "LTIMindtree Ltd."),
    ("MOTHERSON", "Samvardhana Motherson International Ltd."),
    ("NAUKRI", "Info Edge (India) Ltd."),
    ("PIDILITIND", "Pidilite Industries Ltd."),
    ("PFC", "Power Finance Corporation Ltd."),
    ("PNB", "Punjab National Bank"),
    ("RECLTD", "REC Ltd."),
    ("SBICARD", "SBI Cards and Payment Services Ltd."),
    ("SRF", "SRF Ltd."),
    ("SIEMENS", "Siemens Ltd."),
    ("TVSMOTOR", "TVS Motor Company Ltd."),
    ("TATAPOWER", "Tata Power Company Ltd."),
    ("TORNTPOWER", "Torrent Power Ltd."),
    ("UNITDSPR", "United Spirits Ltd."),
    ("VBL", "Varun Beverages Ltd."),
    ("VEDL", "Vedanta Ltd."),
    ("ZYDUSLIFE", "Zydus Lifesciences Ltd."),
    ("BHEL", "Bharat Heavy Electricals Ltd."),
    ("CANFINHOME", "Can Fin Homes Ltd."),
    ("CONCOR", "Container Corporation of India Ltd."),
    ("ESCORTS", "Escorts Kubota Ltd."),
    ("IDFCFIRSTB", "IDFC First Bank Ltd."),
    ("MPHASIS", "Mphasis Ltd."),
]

NIFTY_100_CONSTITUENTS = NIFTY_50_CONSTITUENTS + NIFTY_NEXT_50_CONSTITUENTS

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

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


def get_market_profile(a=None):
    if a and getattr(a, "market", None):
        return a.market.upper()
    env = os.environ.get("BHARAT_MARKET")
    if env and env.upper() in ("IN", "INDIA"):
        return "IN"
    cfg_path = getattr(a, "config", "trader/config.json") if a else "trader/config.json"
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
                if cfg.get("market", "").upper() in ("IN", "INDIA"):
                    return "IN"
        except Exception:
            pass
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


def ts_to_eastern(ts):
    """Unix seconds -> naive New York time. Never uses the machine's time zone."""
    return to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)))


def to_local(utc, market="US"):
    if market == "IN":
        return utc.replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)
    return to_eastern(utc)


def local_to_utc(local, market="US"):
    if market == "IN":
        return local - datetime.timedelta(hours=5, minutes=30)
    return eastern_to_utc(local)


def ts_to_local(ts, market="US"):
    if market == "IN":
        return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts), hours=5, minutes=30)
    return ts_to_eastern(ts)


def parse_now(text, market="US"):
    """ISO time -> naive UTC. A time without an offset is taken as local market time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        tz_name = "IST" if market == "IN" else "New York"
        raise ScanError('--now wants a time like "2026-10-19 10:15" (%s) or ...Z (UTC)' % tz_name)
    if dt.tzinfo is None:
        return local_to_utc(dt, market=market)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at", market="US"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        tz_name = "IST" if market == "IN" else "New York"
        raise ScanError("%s wants a %s time like 10:15" % (flag, tz_name))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def trading_day(d, market="US"):
    """(is_trading_day, reason). Weekends and exchange holidays."""
    if market == "IN" and calendar_in is not None:
        ok, status, reason = calendar_in.is_trading_day_in(d)
        if not ok:
            return False, reason
        return True, ""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session(d, market="US"):
    """(open_time, close_time) in local market time, or None if closed all day."""
    ok, _ = trading_day(d, market=market)
    if not ok:
        return None
    if market == "IN":
        if calendar_in is not None:
            spec = calendar_in.get_special_session(d.isoformat() if hasattr(d, "isoformat") else str(d))
            if spec:
                parts_o = [int(p) for p in spec["open_time"].split(":")]
                parts_c = [int(p) for p in spec["close_time"].split(":")]
                return datetime.time(parts_o[0], parts_o[1]), datetime.time(parts_c[0], parts_c[1])
        return NSE_OPEN, NSE_CLOSE
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(sym, market="US"):
    if market == "IN":
        if symbols_in is not None:
            return symbols_in.to_yahoo_symbol(sym)
        return sym.upper() + ".NS"
    return sym.upper().replace(".", "-")


def from_yahoo(ysym, market="US"):
    if market == "IN":
        if symbols_in is not None:
            try:
                sym, _ = symbols_in.from_yahoo_symbol(ysym)
                return sym
            except Exception:
                pass
        s = ysym.upper()
        for suf in (".NS", ".BO"):
            if s.endswith(suf):
                return s[:-len(suf)]
        return s
    return ysym.upper().replace("-", ".")


def spark_path(symbols, rng, interval, market="US"):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(to_yahoo(s, market=market) for s in symbols), rng, interval)


def chart_path(sym, rng, interval, market="US"):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (to_yahoo(sym, market=market), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None, market="US"):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours (BRK-B -> BRK.B)."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), from_yahoo(ysym, market=market))
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _clean_stem(name, market="US"):
    stem = _stem(name)
    if market == "IN":
        for suf in ("-5m", "-daily", "-quote", "-live"):
            if stem.endswith(suf):
                stem = stem[:-len(suf)]
        for suf in (".NS", ".BO"):
            if stem.endswith(suf):
                stem = stem[:-len(suf)]
        if symbols_in is not None:
            try:
                return symbols_in.normalize_symbol(stem)
            except Exception:
                pass
        return stem.upper()
    return stem.upper().replace("-", ".")


def _absorb(doc, name_hint, data, errors, market="US"):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        for k, v in parse_spark(doc, wanted=None, market=market).items():
            if k not in data or len(v.get("t", [])) > len(data[k].get("t", [])):
                data[k] = v
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <TICKER>.json")
        if market == "IN":
            sym = from_yahoo(sym, market="IN")
        else:
            sym = sym.upper().replace("-", ".")
        try:
            parsed = parse_chart(doc, sym)
            if sym not in data or len(parsed.get("t", [])) > len(data[sym].get("t", [])):
                data[sym] = parsed
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors, market=market)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path, market="US"):
    """A folder of <T>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _clean_stem(f, market=market), data, errors, market=market)
            elif isinstance(doc, dict) and any(isinstance(v, dict) and ("chart" in v or "spark" in v) for v in doc.values()):
                _absorb(doc, "", data, errors, market=market)
        if not data and not errors:
            raise ScanError("no chart or spark files in %s" % path)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        hint = _clean_stem(os.path.basename(path), market=market)
        _absorb(doc, hint, data, errors, market=market)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

TICKER = re.compile(r"^[A-Z]{1,5}$|^[A-Z]{1,4}\.[A-Z]$")
ALWAYS_OUT = [
    ("warrant", re.compile(r"\bwarrants?\b")),
    ("unit or right", re.compile(r"\b(units?|rights?),? (each|that|entitling)\b|(?<!the )\brights? to\b")),
    ("note", re.compile(r"\b(notes?|etns?|debentures?)\b")),
    ("coupon", re.compile(r"%")),
    ("depositary", re.compile(r"(?<!american )(?<!global )\bdepositary shares?\b")),
]
COMMON = re.compile(
    r"\bcommon (stock|shares?|units?)\b|\bord(inary)? shares?\b|\bshares? of beneficial interest\b"
    r"|\b(american|global) deposit[ao]ry\b|\bads[s]?\b|\badrs?\b"
    r"|\b(limited partnership|partnership|trust) units?\b|\bl\.?p\.? units?\b"
    r"|\bunits? (of|representing)\b.*\b(limited partner|partnership|beneficial interest|limited liability)")
SOFT_OUT = [
    ("unit", re.compile(r"\bunits?\b")),
    ("right", re.compile(r"\brights?\b")),
    ("bond", re.compile(r"\bbonds?\b")),
    ("preferred", re.compile(r"\bpreferred\b|\bpreference\b")),
]


def classify(row):
    """One symbol-directory row -> 'keep' or the rule that drops it."""
    sym = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
    if (row.get("Test Issue") or "").strip() != "N":
        return "test issue"
    if (row.get("ETF") or "").strip() != "N":
        return "ETF"
    if (row.get("NextShares") or "N").strip() != "N":
        return "NextShares"
    if not TICKER.match(sym):
        return "not a plain ticker"
    if re.search(r"\.[UWR]$", sym):
        return "unit / warrant / right suffix"
    name = " ".join((row.get("Security Name") or "").lower().split())
    for tag, rx in ALWAYS_OUT:
        if rx.search(name):
            return tag
    if COMMON.search(name):
        return "keep"
    for tag, rx in SOFT_OUT:
        if rx.search(name):
            return tag
    return "keep"


def parse_symdir(text):
    """Nasdaq Trader pipe file -> (rows, file_created). The last line is 'File Creation Time: ...'."""
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if not lines or "|" not in lines[0]:
        raise ScanError("not a Nasdaq Trader symbol directory")
    hdr = [h.strip() for h in lines[0].split("|")]
    if "Security Name" not in hdr or not ({"Symbol", "ACT Symbol"} & set(hdr)):
        raise ScanError("not a Nasdaq Trader symbol directory (header %s)" % lines[0][:60])
    rows, created = [], None
    for ln in lines[1:]:
        if ln.startswith("File Creation Time"):
            m = re.search(r"(\d{2})(\d{2})(\d{4})(\d{2}):(\d{2})", ln)
            if m:
                created = "%s-%s-%s %s:%s" % (m.group(3), m.group(1), m.group(2), m.group(4), m.group(5))
            continue
        rows.append(dict(zip(hdr, ln.split("|"))))
    return rows, created


def build_universe(texts):
    """{filename: text} -> (kept [(SYM, name)], counts, created)."""
    kept, counts, created = {}, {}, None
    for fname in NASDAQ_FILES:
        if fname not in texts:
            raise ScanError("missing %s" % fname)
        rows, c = parse_symdir(texts[fname])
        created = created or c
        counts["rows"] = counts.get("rows", 0) + len(rows)
        for r in rows:
            why = classify(r)
            counts[why] = counts.get(why, 0) + 1
            if why == "keep":
                sym = (r.get("Symbol") or r.get("ACT Symbol")).strip()
                kept[sym] = " ".join((r.get("Security Name") or "").split())
    return sorted(kept.items()), counts, created


def build_universe_in(index="NIFTY50", instruments_cache=None, offline_path=None):
    """Build Indian stock universe for scanning.
    Supported indices:
    - NIFTY50: 50 prominent large-cap equities.
    - NIFTY100: 100 large-cap and mid-cap equities.
    - FNO: Available eligible F&O stocks.
    """
    idx = (index or "NIFTY50").strip().upper()
    counts = {}
    if idx == "NIFTY50":
        kept = sorted(NIFTY_50_CONSTITUENTS)
        counts["rows"] = len(kept)
        counts["keep"] = len(kept)
        return kept, counts, "NSE NIFTY 50 Index"
    elif idx == "NIFTY100":
        kept = sorted(NIFTY_100_CONSTITUENTS)
        counts["rows"] = len(kept)
        counts["keep"] = len(kept)
        return kept, counts, "NSE NIFTY 100 Index"
    elif idx in ("FNO", "F&O"):
        # Check offline path for EQUITY_L.csv and fo_mktlots.csv
        if offline_path and os.path.isdir(offline_path):
            eq_csv = os.path.join(offline_path, "EQUITY_L.csv")
            fo_csv = os.path.join(offline_path, "fo_mktlots.csv")
            if os.path.isfile(eq_csv) and instruments is not None:
                eq_records = instruments.parse_equity_l_csv(eq_csv)
                fo_lots = instruments.parse_fo_lots_csv(fo_csv) if os.path.isfile(fo_csv) else {}
                master = instruments.build_instrument_master(eq_records, fo_lots)
                kept = [(sym, rec.get("name", sym)) for sym, rec in sorted(master["instruments"].items())
                        if rec.get("is_fo_eligible")]
                counts["rows"] = len(eq_records)
                counts["keep"] = len(kept)
                return kept, counts, "NSE F&O Eligible List (Offline Files)"
        # Check instruments cache
        cache_path = instruments_cache or "trader/state/instruments.json"
        if os.path.isfile(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    doc = json.load(fh)
                insts = doc.get("instruments", {})
                kept = [(sym, rec.get("name", sym)) for sym, rec in sorted(insts.items())
                        if rec.get("is_fo_eligible")]
                if kept:
                    counts["rows"] = len(insts)
                    counts["keep"] = len(kept)
                    return kept, counts, "NSE F&O Master Cache (%s)" % cache_path
            except Exception:
                pass
        # Fallback to NIFTY 100 as base F&O set
        kept = sorted(NIFTY_100_CONSTITUENTS)
        counts["rows"] = len(kept)
        counts["keep"] = len(kept)
        return kept, counts, "NSE F&O Eligible (NIFTY 100 proxy)"
    else:
        raise ScanError("Unsupported Indian index %r: choose NIFTY50, NIFTY100, or FNO" % index)


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc, market="US"):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_local(now_utc, market=market)
    today = now.date()
    s = session(today, market=market)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_local(ts, market=market).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume, market="US"):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc, market=market)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most dollars traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k, market="US"):
    open_time = NSE_OPEN if market == "IN" else OPEN
    return datetime.datetime.combine(day, open_time) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, market="US"):
    """5-minute bars of `day` that are complete at `cutoff` (naive local market time),
    on the grid, with a close. Sorted, one per slot."""
    s = session(day, market=market)
    open_t = NSE_OPEN if market == "IN" else OPEN
    close_t = s[1] if s else (NSE_CLOSE if market == "IN" else CLOSE)
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        local_t = ts_to_local(ts, market=market)
        if local_t.date() != day or local_t.second or local_t.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if local_t.time() < open_t or local_t.time() >= close_t:
            continue  # pre-market, after hours, or closing auction print
        if local_t + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[local_t] = {"start": local_t, "c": float(c),
                        "h": None if not hs or hs[i] is None else float(hs[i]),
                        "l": None if not ls or ls[i] is None else float(ls[i]),
                        "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range, market="US"):
    """Every slot from open to the cutoff is there, and at least the range."""
    want = int((cutoff - slot(day, 0, market=market)).total_seconds() // (BAR_MIN * 60))
    s = session(day, market=market)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0, market=market)).total_seconds()
                             // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k, market=market) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, market="US"):
    """-> (day, cutoff naive local time) or raise ScanError with the plain message."""
    now = to_local(now_utc, market=market)
    day = explicit_day or now.date()
    tz_label = "IST" if market == "IN" else "ET"
    open_str = "09:15" if market == "IN" else "9:30"
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day, market=market)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day, market=market)
    range_end = slot(day, or_minutes // BAR_MIN, market=market)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s %s)." % (at.strftime("%H:%M"), clock_label(now), tz_label))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s %s on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), tz_label, day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s %s: the %d-minute opening range runs %s to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), tz_label, or_minutes, open_str, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s %s, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), tz_label, until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None, market="US"):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff, market=market)
        if not usable(bars, day, cutoff, n_range, market=market):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff, market=market)
            if not usable(full, day, cutoff, n_range, market=market):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None, market="US"):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s, market=market): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval, market=market)), wanted, market=market)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def money(x, market="US"):
    if market == "IN":
        return "\u20b9{:,.2f}".format(x)
    return "${:,.2f}".format(x)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    market = get_market_profile(a)
    if market == "IN":
        index = getattr(a, "index", None) or "NIFTY50"
        instruments_cache = getattr(a, "instruments", None) or "trader/state/instruments.json"
        kept, counts, created = build_universe_in(index=index, instruments_cache=instruments_cache,
                                                  offline_path=a.offline)
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(a.out, "w", encoding="utf-8") as fh:
                for sym, name in kept:
                    fh.write("%s|%s\n" % (sym, name))
        dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
        if a.json:
            print(json.dumps({"count": len(kept), "rows": counts.get("rows", len(kept)),
                              "index": index.upper(), "source": created,
                              "out": None if a.no_write else a.out,
                              "seconds": round(time.time() - t0, 1)}, indent=2))
        else:
            print("NSE/BSE Indian stock universe (%s): %d securities." % (index.upper(), len(kept)))
            if not a.no_write:
                print("Wrote %s (one SYMBOL|Name a line)." % a.out)
        return 0 if kept else 1

    texts = {}
    for k, fname in enumerate(NASDAQ_FILES):
        if a.offline:
            p = os.path.join(a.offline, fname)
            if not os.path.exists(p):
                raise ScanError("no %s in %s" % (fname, a.offline))
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts[fname] = fh.read()
        else:
            if k and a.pause:
                SLEEP(a.pause)
            try:
                texts[fname] = http_get(NASDAQ_URL + fname, timeout=60)
            except urllib.error.HTTPError as e:
                raise ScanError("nasdaqtrader.com answered HTTP %d for %s" % (e.code, fname))
            except (urllib.error.URLError, OSError) as e:
                raise ScanError("could not reach nasdaqtrader.com: %s" % getattr(e, "reason", e))
    kept, counts, created = build_universe(texts)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "files_created": created,
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("Nasdaq Trader symbol directories%s: %s rows." % (
            (", files of %s ET" % created) if created else "", "{:,}".format(counts.get("rows", 0))))
        print("Kept %s US-listed common stocks, ordinary shares and ADRs." % "{:,}".format(len(kept)))
        print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                      for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    market = get_market_profile(a)
    now = parse_now(a.now, market="IN") if market == "IN" else parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline, market=market)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a, market=market)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume, market=market)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "market": market,
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    local = to_local(now, market=market)
    tz_label = "IST" if market == "IN" else "ET"
    curr_sym = "\u20b9" if market == "IN" else "$"
    print("Daily scan, %s %s, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), tz_label, last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s%s+ and %s+ shares)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), curr_sym, _n(a.min_price),
                           "{:,}".format(a.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-6s %10s  RSI %5.1f  %s" % (h["symbol"], money(h["price"], market=market), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    market = get_market_profile(a)
    now = parse_now(a.now, market="IN") if market == "IN" else parse_now(a.now)
    at = (parse_hhmm(a.at, "--at", market="IN") if market == "IN" else parse_hhmm(a.at)) if a.at else None
    cfg = read_config(a.config)
    default_until = "15:00" if market == "IN" else "15:30"
    until_str = a.until or cfg.get("no_entries_after") or default_until
    until = parse_hhmm(until_str, "--until", market="IN") if market == "IN" else parse_hhmm(until_str, "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline, market=market)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_local(s["t"][-1], market=market).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, market=market)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        now_dt = parse_now(None, market="IN") if market == "IN" else parse_now(None)
        today = to_local(now_dt, market=market).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a, market=market)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN, market=market)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm, market=market)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    tz_label = "IST" if market == "IN" else "ET"
    as_of = "%s %s %s" % (day.isoformat(), cutoff.strftime("%H:%M"), tz_label)
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "market": market,
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    open_str = "09:15" if market == "IN" else "9:30"
    print("Opening-range breakout scan, %s %s %s: %d-minute range (%s to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), tz_label, a.or_minutes, open_str,
             clock_label(slot(day, a.or_minutes // BAR_MIN, market=market))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-6s %10s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], money(h["price"], market=market), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER and /trader-entry TICKER.")
    return 0 if hits else 1


def _p(x):
    return "{:,.2f}".format(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): daily setups and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download Nasdaq Trader's lists or generate NSE/BSE universe -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="DIR", help="a folder with symbol files")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")
    p.add_argument("--market", choices=["US", "IN"], help="market profile: US (Nasdaq/NYSE) or IN (NSE/BSE)")
    p.add_argument("--index", choices=["NIFTY50", "NIFTY100", "FNO"], default="NIFTY50",
                   help="Indian index constituent list: NIFTY50, NIFTY100, or FNO")
    p.add_argument("--instruments", default="trader/state/instruments.json",
                   help="path to authoritative instrument master cache")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price)
        p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" local, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")
        p.add_argument("--market", choices=["US", "IN"], help="market profile: US or IN")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 5.0, 1000000)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 5.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this local market time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 15:30/15:00)")
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
