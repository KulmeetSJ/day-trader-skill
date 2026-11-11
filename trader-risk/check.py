#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-19 10:15"        # pin the clock (market time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, both or neither of qty / notional,
                notional on anything but a market day order (Alpaca's rule),
                a limit order without a limit price, unknown type; a bracket
                that is not whole shares, not day / gtc, or misses a leg
  asset         options (OCC / NSE/BSE symbols) unless "allow_options": true;
                crypto and anything that is not a stock, ETF, or allowed derivative
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order, over max_order (min_order_inr/usd, max_order_inr/usd)
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) over max_loss_per_trade (_inr or _usd). The entry
                is the limit price for a limit order, else the HIGHER of the plan's
                entry and a fresh price (under 15 minutes old), so a stale plan
                cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss (_inr or _usd) (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (market
                time: New York ET for US, IST for IN), or the market is closed
                (weekend, exchange holiday, outside session hours)
  pdt           only with "pdt_guard": true: 3 day trades already in the last
                5 business days (a 4th would trip the old pattern-day-trader rule)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
_ALERT_DIR = os.path.join(os.path.dirname(_DIR), "trader-alerts")
if _ALERT_DIR not in sys.path:
    sys.path.insert(0, _ALERT_DIR)

# Cross-skill helpers live in ../trader-alerts (added to sys.path above)
try:
    import symbols_in  # type: ignore
    import calendar_in  # type: ignore
    import instruments  # type: ignore
except ImportError:
    symbols_in = None
    calendar_in = None
    instruments = None

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
    "min_order_usd": 5, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "pdt_guard": False,
}
ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
FINGERPRINT_FIELDS = ALPACA_FIELDS + (
    "market", "currency", "trading_style", "product", "multi_session_protection",
)
PAPER_MULTI_SESSION_BROKERS = ("paper", "paper_in", "simulated", "mock")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIFS = {"day", "gtc", "opg", "cls", "ioc", "fok"}
OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
TICKER = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
TICKER_IN = re.compile(r"^[A-Z0-9.\-&]{1,20}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

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

OPEN_IN = datetime.time(9, 15)
CLOSE_IN = datetime.time(15, 30)
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


class RiskError(Exception):
    pass


def get_market_profile(a=None, orders=None, cfg=None):
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
    if orders and isinstance(orders, list):
        for o in orders:
            if not isinstance(o, dict):
                continue
            if o.get("market", "").upper() in ("IN", "INDIA") or o.get("currency", "").upper() == "INR":
                return "IN"
            if isinstance(o.get("plan"), dict) and o["plan"].get("market", "").upper() in ("IN", "INDIA"):
                return "IN"
            sym = str(o.get("symbol", "")).upper()
            if sym.endswith((".NS", ".BO", "-EQ")):
                return "IN"
            if "-" in sym or "&" in sym:
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


def parse_now(text, market="US"):
    """ISO time -> naive UTC. A time without an offset is taken as market local time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
        market = "IN"
    elif t.upper().endswith(" ET"):
        t = t[:-3].strip()
        market = "US"
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        lbl = "IST" if market == "IN" else "New York"
        raise RiskError("--now wants a time like \"2026-10-19 10:15\" (%s) or ...Z (UTC)" % lbl)
    if dt.tzinfo is None:
        return local_to_utc(dt, market=market)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def _market_iso(utc_dt, market="US"):
    if utc_dt is None:
        return None
    local = to_local(utc_dt, market=market)
    if market == "IN":
        return local.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    off = eastern_offset(utc_dt)
    sign = "-" if off < 0 else "+"
    return "%s%s%02d:00" % (local.strftime("%Y-%m-%dT%H:%M:%S"), sign, abs(off))


def _parse_clock_time(text):
    if not text:
        return None
    t = str(text).strip().upper()
    for suffix in (" IST", " ET"):
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.datetime.strptime(t, fmt).time()
        except ValueError:
            continue
    return None


def parse_utc(text):
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return datetime.datetime.fromtimestamp(int(text), tz=datetime.timezone.utc).replace(tzinfo=None)
    s = str(text).strip()
    offset = None
    upper = s.upper()
    if upper.endswith(" IST"):
        s = s[:-4].strip()
        offset = IST_OFFSET
    elif upper.endswith(" ET"):
        s = s[:-3].strip()
        try:
            dt_naive = datetime.datetime.fromisoformat(s)
            return eastern_to_utc(dt_naive)
        except ValueError:
            return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - (dt.utcoffset() or datetime.timedelta(0))).replace(tzinfo=None)
    elif offset is not None:
        dt = dt - offset
    return dt


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def hhmm(value, key):
    """"09:45" -> time(9, 45); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be a New York time like \"09:45\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _shift(t, hours):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 1), t) - datetime.timedelta(hours=hours)
    return dt.time()


def window_check(now_et, c):
    """Reasons an entry cannot be placed now (New York time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_et.date(), now_et.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the holiday list covers %s only: check nyse.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s New York)" % (why, stamp)], warns
    close = EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE
    if t < OPEN or now_et.time() >= close:
        return ["window: the market is closed (%s New York; the session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if close == EARLY and after is not None:
        after = _shift(after, 3)
        warns.append("early close at 1:00 p.m. today (%s): no new entries after %s"
                     % (NYSE_EARLY_CLOSE[d.isoformat()], after.strftime("%H:%M")))
    if before is not None and t < before:
        return ["window: no new entries before %s New York (it is %s)" % (before.strftime("%H:%M"),
                                                                          t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s New York (it is %s)" % (after.strftime("%H:%M"),
                                                                         t.strftime("%H:%M"))], warns
    return [], warns


def window_check_in(now_ist, c, trading_style="intraday"):
    """Reasons an Indian entry cannot be placed now (IST). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None and trading_style != "positional":
        return [], []
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if d.weekday() >= 5:
        return ["window: the market is closed today (weekend, %s IST)" % stamp], warns
    if calendar_in:
        ok, _st, hname = calendar_in.is_trading_day_in(d)
        if not ok:
            return ["window: the market is closed today (NSE holiday: %s, %s IST)" % (hname or "holiday", stamp)], warns
    if t < OPEN_IN or now_ist.time() >= CLOSE_IN:
        return ["window: the market is closed (%s IST; the session is %s to %s)"
                % (stamp, OPEN_IN.strftime("%H:%M"), CLOSE_IN.strftime("%H:%M"))], warns
    if trading_style == "positional":
        return [], warns
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    return [], warns


def business_days(today, n=5):
    """The last n NYSE trading days up to and including today, newest first."""
    out, d = [], today
    for _ in range(40):
        if trading_day(d)[0]:
            out.append(d.isoformat())
            if len(out) == n:
                break
        d -= datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in FINGERPRINT_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def positional_order_issues(order, plan, cfg, market):
    """Validate positional order semantics and multi-session broker/schema capability."""
    issues = []
    style = str(order.get("trading_style") or plan.get("trading_style") or "").strip().lower()
    oc = str(order.get("order_class") or "").strip().lower()
    tif = str(order.get("time_in_force") or "").strip().lower()
    flat_p = str(plan.get("flat_by") or "").strip().lower()
    is_pos = (style == "positional" or oc == "gtt_oco" or flat_p == "positional")
    if not is_pos:
        return issues
    if tif == "day":
        issues.append("positional: positional order must not use intraday time_in_force='day'; use 'gtc' with gtt_oco")
    elif tif != "gtc":
        issues.append("positional: multi-session order requires time_in_force='gtc', got %r" % tif)
    if oc == "bracket":
        issues.append("positional: positional order must not inherit intraday order_class='bracket'; use 'gtt_oco'")
    elif oc != "gtt_oco":
        issues.append("positional: positional order requires order_class='gtt_oco' for multi-session protection, got %r" % (oc or None))
    if flat_p and flat_p != "positional":
        issues.append("positional: order has trading_style='positional' but intraday plan flat_by=%r" % plan.get("flat_by"))
    eff_market = str(market or "US").upper()
    if eff_market != "IN":
        issues.append("positional: multi-session GTT OCO orders are supported only in Indian paper mode (market='IN')")
    c = cfg or {}
    if not bool(c.get("paper", True)):
        issues.append("positional: multi-session GTT OCO orders are supported in paper mode only (paper=true)")
    if c.get("broker_supports_multi_session") is False or c.get("supports_multi_session_protection") is False:
        issues.append("positional: configured broker/schema disables multi-session protection")
    default_broker = "paper" if eff_market == "IN" else "alpaca"
    broker = str(c.get("broker") or default_broker).strip().lower()
    if broker not in PAPER_MULTI_SESSION_BROKERS:
        issues.append("positional: broker %r cannot safely represent multi-session GTT OCO protection; use broker='paper'" % broker)
    product = str(order.get("product") or "").strip().upper()
    if product not in ("CNC", "NRML"):
        issues.append("positional: expected delivery/carryforward product 'CNC' or 'NRML', got %r" % (order.get("product")))
    prot = order.get("multi_session_protection")
    if not isinstance(prot, dict) or prot.get("persists_across_sessions") is not True:
        issues.append("positional: order must declare multi_session_protection with persists_across_sessions=true")
    elif str(prot.get("time_in_force") or "").strip().lower() != "gtc":
        issues.append("positional: multi_session_protection must specify time_in_force='gtc'")
    if order.get("take_profit_limit_price") is None or order.get("stop_loss_stop_price") is None:
        issues.append("positional: GTT OCO order requires both stop_loss_stop_price and take_profit_limit_price")
    return issues


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _cfg_money(raw_cfg, merged_cfg, base, market, default=None):
    raw = raw_cfg or {}
    keys = (base + "_inr", base, base + "_usd") if market == "IN" else (base + "_usd", base)
    for k in keys:
        if k in raw and raw.get(k) is not None:
            return raw.get(k)
    val = (merged_cfg or {}).get(base + "_usd")
    return val if val is not None else default


def money(x, market="US"):
    sym = "\u20b9" if market == "IN" else "$"
    return sym + "{:,.2f}".format(float(x)).replace(".00", "")


def _sub_unit(x):
    """A price to 0.01 sub-units (cent or paise), half up (366.475 -> 366.48)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


_cent = _sub_unit


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def day_trades(state, today):
    """Day trades in the last 5 business days: a (day, symbol) with both a buy
    and a sell placed or filled that day. An entry placed today counts too: it
    is meant to be flat by the close. Returns (count, sorted list of "day SYM")."""
    days = set(business_days(datetime.date.fromisoformat(today)))
    sides = {}
    for rec in (state or {}).get("orders", []):
        if rec.get("status") not in ("placed", "filled"):
            continue
        d = rec_day(rec)
        if d not in days:
            continue
        key = (d, str(rec.get("symbol", "")).upper())
        sides.setdefault(key, set()).add(str(rec.get("side", "")).lower())
        if d == today and rec.get("status") == "placed" and rec_is_entry(rec):
            sides[key].add("pending")
    hits = sorted("%s %s" % k for k, s in sides.items()
                  if ("buy" in s and "sell" in s) or (k[0] == today and "pending" in s))
    return len(hits), hits


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None, market=None, simulated=False, source_doc=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its market date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh"""
    raw_cfg = cfg or {}
    c = dict(DEFAULTS)
    c.update(raw_cfg)
    if market is None:
        market = get_market_profile(orders=orders, cfg=c)
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None, market=market)
    now_local = to_local(now_utc, market=market)
    now_et = to_eastern(now_utc)
    today = today or now_local.date().isoformat()
    acct_prices = {
        k: v for k, v in prices.items()
        if price_times is None or price_times.get(k) is None or price_times[k] <= now_utc
    }
    acct, top_warn = account(c, portfolio, acct_prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    if market == "IN":
        win_reasons, win_warns = window_check_in(now_local, c)
        pdt_n, pdt_list = (0, [])
    else:
        win_reasons, win_warns = window_check(now_local, c)
        pdt_n, pdt_list = day_trades(state, today) if c.get("pdt_guard") else (0, [])
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)

    min_order_raw = _cfg_money(raw_cfg, c, "min_order", market, default=0)
    min_order = float(min_order_raw) if min_order_raw is not None else 0.0
    max_order = _f(_cfg_money(raw_cfg, c, "max_order", market))
    max_loss_trade = _f(_cfg_money(raw_cfg, c, "max_loss_per_trade", market))
    max_daily_loss = _f(_cfg_money(raw_cfg, c, "max_daily_loss", market))

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        min_age = 0 if (simulated or market == "IN") else -60
        return prices[sym] if min_age <= age <= FRESH_MINUTES * 60 else None

    is_sim_run = bool(simulated or (isinstance(source_doc, dict) and source_doc.get("simulated_at")))
    doc_lts_raw = (source_doc or {}).get("last_data_timestamp") if isinstance(source_doc, dict) else None
    doc_lts_utc = parse_utc(doc_lts_raw)
    permitted_data_times = []
    if doc_lts_utc is not None and doc_lts_utc <= now_utc:
        permitted_data_times.append(doc_lts_utc)

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        if o.get("simulated_at") or plan.get("simulated_at"):
            is_sim_run = True

        if simulated:
            o_lts_raw = o.get("last_data_timestamp") or plan.get("last_data_timestamp")
            o_lts_utc = parse_utc(o_lts_raw)
            if o_lts_utc is not None and o_lts_utc > now_utc:
                reasons.append(
                    "look-ahead: order data timestamp %s is after simulated time %s"
                    % (o_lts_raw, _market_iso(now_utc, market=market))
                )
            elif o_lts_utc is None and doc_lts_utc is not None and doc_lts_utc > now_utc:
                reasons.append(
                    "look-ahead: orders file data timestamp %s is after simulated time %s"
                    % (doc_lts_raw, _market_iso(now_utc, market=market))
                )
            elif o_lts_utc is not None:
                permitted_data_times.append(o_lts_utc)
            plan_date = str(plan.get("date") or "")
            if plan_date:
                if plan_date > today:
                    reasons.append(
                        "look-ahead: plan date %s is after simulated date %s" % (plan_date, today)
                    )
                elif plan_date == today and plan.get("entry_time"):
                    et_t = _parse_clock_time(plan.get("entry_time"))
                    if et_t is not None:
                        et_utc = local_to_utc(
                            datetime.datetime.combine(datetime.date.fromisoformat(plan_date), et_t),
                            market=market,
                        )
                        if et_utc > now_utc:
                            reasons.append(
                                "look-ahead: entry time %s on %s is after simulated time %s"
                                % (plan.get("entry_time"), plan_date, now_local.strftime("%Y-%m-%d %H:%M"))
                            )
        else:
            o_lts_utc = parse_utc(o.get("last_data_timestamp") or plan.get("last_data_timestamp"))
            if o_lts_utc is not None and o_lts_utc <= now_utc:
                permitted_data_times.append(o_lts_utc)
        oc = str(o.get("order_class") or "").strip().lower()
        is_gtt_oco = (oc == "gtt_oco")
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if (qty is None) == (notional is None):
            reasons.append("format: give exactly one of qty or notional")
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not isinstance(o.get(k), str):
                    warns.append("send %s as a string (\"%s\"), the way place_stock_order types it"
                                 % (k, o.get(k)))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r" % tif)
        if notional is not None and (typ != "market" or tif != "day"):
            reasons.append("format: a notional (currency amount) order must be a market order with time_in_force day")
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if o.get("extended_hours") and not (typ == "limit" and tif in ("day", "gtc")):
            reasons.append("format: extended hours needs a limit order with time_in_force day or gtc")
        if o.get("stop_loss_limit_price") is not None and o.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket or is_gtt_oco:
            order_kind = "gtt_oco" if is_gtt_oco else "bracket"
            if notional is not None:
                reasons.append("format: a %s order needs a whole-share qty, not a notional amount" % order_kind)
            elif _f(qty) is not None and abs(_f(qty) - round(_f(qty))) > 1e-9:
                reasons.append("format: a %s order takes whole shares only, got qty %r" % (order_kind, qty))
            if tif not in ("day", "gtc"):
                reasons.append("format: a %s order needs time_in_force day or gtc, got %r" % (order_kind, tif))
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: a %s order needs both take_profit_limit_price and "
                               "stop_loss_stop_price" % order_kind)
        elif oc or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you say yes"
                         % (oc or "advanced_instructions"))

        # asset
        is_opt = False
        is_fut = False
        deriv_meta = None
        if sym and OCC.match(sym.replace(" ", "")):
            is_opt = True
        elif sym and market == "IN" and symbols_in and symbols_in.is_derivative_symbol(sym):
            deriv_meta = symbols_in.parse_derivative_symbol(sym)
            if deriv_meta:
                if deriv_meta.get("instrument_type") == "OPT":
                    is_opt = True
                elif deriv_meta.get("instrument_type") == "FUT":
                    is_fut = True
        elif o.get("asset_type") == "OPTIONS" or o.get("option_contract"):
            is_opt = True
        elif o.get("asset_type") == "FUTURES":
            is_fut = True

        if is_opt:
            if not c.get("allow_options"):
                reasons.append("asset: %s is an option contract; options are off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("option order: options can lose 100% of what you pay")
        elif is_fut:
            if c.get("allow_futures") is False:
                reasons.append("asset: %s is a futures contract; futures are off "
                               "(\"allow_futures\": false)" % sym)
            else:
                warns.append("futures order: leveraged derivative instrument")
        elif sym:
            if market == "IN":
                if "/" in sym or not TICKER_IN.match(sym):
                    reasons.append("asset: %s does not look like an Indian stock ticker (NSE/BSE); "
                                    "this pack trades stocks and ETFs only" % sym)
            else:
                if "/" in sym or not TICKER.match(sym):
                    reasons.append("asset: %s does not look like a US stock or ETF ticker; "
                                    "this pack trades stocks and ETFs only" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        is_future_price = bool(
            price_times is not None and sym in prices
            and price_times.get(sym) is not None and price_times[sym] > now_utc
        )
        ref, ref_from = None, None
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "stop":
            ref, ref_from = _f(o.get("stop_price")), "stop price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
                if live is not None and price_times is not None and price_times.get(sym) is not None:
                    if price_times[sym] <= now_utc:
                        permitted_data_times.append(price_times[sym])
            elif sym in prices and not is_future_price:
                ref, ref_from = prices[sym], "stale price"
                if price_times is not None and price_times.get(sym) is not None and price_times[sym] <= now_utc:
                    permitted_data_times.append(price_times[sym])
        if live is None and sym in prices and price_times is not None and (
            (plan_entry and typ == "market") or is_future_price
        ):
            if is_future_price:
                warns.append(
                    "the %s price in trader/prices.json is from the future relative to cutoff "
                    "(older than %d minutes check failed): not used" % (sym, FRESH_MINUTES)
                )
            else:
                warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                             % (sym, FRESH_MINUTES))
        q = _f(qty)
        if notional is not None and _f(notional):
            value = _f(notional)
        elif q:
            if ref:
                value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry":
            reasons.extend(positional_order_issues(o, plan, c, market))
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # Trade-to-Trade (T2T) check for Indian equities
        if market == "IN" and kind == "entry":
            is_t2t = bool(o.get("is_trade_to_trade") or plan.get("is_trade_to_trade"))
            series = o.get("series") or plan.get("series") or ""
            if not is_t2t and instruments:
                try:
                    inst = instruments.get_instrument(sym)
                    if inst:
                        if inst.get("is_trade_to_trade"):
                            is_t2t = True
                        if not series and inst.get("series"):
                            series = inst["series"]
                except Exception:
                    pass
            if is_t2t or (series and str(series).upper() in ("BE", "BZ", "T")):
                later.append("trade-to-trade: intraday day trading is prohibited on %s (SEBI rules: series %s requires gross delivery settlement)"
                             % (sym, series or "T2T"))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < min_order - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value, market=market), money(min_order, market=market)))
            if max_order and value > max_order + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value, market=market), money(max_order, market=market)))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_amt = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (borrowed money)"
                             % (money(value, market=market), money(cash, market=market)))
            elif after_cash < buffer_amt - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0), market=market), _pct(c["cash_buffer_pct"]), money(buffer_amt, market=market)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after, market=market), 100.0 * pos_after / total, money(total, market=market),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if is_opt:
                reasons.append("options: options selling / naked short options are prohibited (undefined risk)")
            if q is not None:
                over = q > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # Lot size check
        lot_size = int(o.get("lot_size") or plan.get("lot_size") or 1)
        if lot_size <= 1 and market == "IN" and instruments and (is_opt or is_fut or (symbols_in and symbols_in.is_derivative_symbol(sym))):
            try:
                fo_lot = instruments.get_fo_lot_size(sym)
                if fo_lot and fo_lot > 1:
                    lot_size = int(fo_lot)
            except Exception:
                pass
        if market == "IN" and (is_opt or is_fut) and lot_size <= 1:
            reasons.append("metadata: cannot authoritatively resolve contract lot size for %s: missing or incomplete instrument metadata" % sym)
        elif lot_size > 1:
            q_val = _f(qty)
            if q_val is not None:
                q_int = int(round(q_val))
                if q_int % lot_size != 0 or q_int <= 0:
                    later.append("lot size: %s requires whole lot multiples of %d (got qty %s)" % (sym, lot_size, qty))

        # Expiry check for derivative contracts
        expiry_val = o.get("expiry") or plan.get("expiry") or (o.get("option_contract") or {}).get("expiry")
        if not expiry_val and deriv_meta:
            expiry_val = deriv_meta.get("expiry_date")
        if market == "IN" and (is_opt or is_fut) and not expiry_val:
            reasons.append("metadata: missing contract expiry date for derivative %s" % sym)
        elif expiry_val:
            try:
                exp_d = datetime.date.fromisoformat(str(expiry_val))
                if exp_d < now_local.date():
                    reasons.append("expiry: contract %s expired on %s" % (sym, exp_d.isoformat()))
                elif exp_d == now_local.date() and kind == "entry":
                    if now_local.time() > datetime.time(14, 30):
                        later.append("expiry cutoff: new derivative entries on expiry day (%s) are blocked after 14:30 IST to avoid physical delivery / gamma explosion" % exp_d.isoformat())
                    else:
                        warns.append("expiry day: contract %s expires today at 15:30 IST" % sym)
            except ValueError:
                pass

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window, pdt
        max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "the broker uses" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                 "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q if q is not None else ((value or 0) / ref)
                    max_loss = round(shares * risk_ps, 2)
                    cap = max_loss_trade
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, over your %s max loss "
                                     "per trade" % (_num(shares), _p(risk_ps), money(max_loss, market=market), money(cap, market=market)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = max_daily_loss
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used, market=market), money(max_loss, market=market), money(used + max_loss, market=market),
                                                       money(daily, market=market)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            order_style = str(o.get("trading_style") or plan.get("trading_style") or c.get("trading_style") or "").lower()
            if market == "IN" and order_style == "positional":
                order_win_reasons, _ = window_check_in(now_local, c, trading_style="positional")
            else:
                order_win_reasons = win_reasons
            is_paper = bool(c.get("paper", True))
            is_off_hours_allowed = is_paper and (bool(c.get("allow_off_hours")) or bool(o.get("is_historical_analysis")) or bool(plan.get("is_historical_analysis")))
            if is_off_hours_allowed and order_win_reasons:
                tz_label = "IST" if market == "IN" else "New York"
                stamp = now_local.strftime("%a %Y-%m-%d %H:%M")
                warns.append("off-hours paper trading analysis: market is closed (%s %s). Order evaluated for next trading session." % (stamp, tz_label))
            else:
                later.extend(order_win_reasons)
            if c.get("pdt_guard") and pdt_n >= 3:
                later.append("pdt: %d day trades in the last 5 business days (%s); a 4th trips the "
                             "pattern-day-trader rule your broker applies (\"pdt_guard\": true)"
                             % (pdt_n, ", ".join(pdt_list)))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (money(max_loss, market=market), (" (limit %s)" % money(max_loss_trade, market=market))
                               if max_loss_trade else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss, market=market), money(daily, market=market) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
        res_item = {
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": money(_f(notional) or 0, market=market) if notional is not None else ("%s shares" % qty),
            "currency": "INR" if market == "IN" else "USD",
            "value": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        }
        if market != "IN":
            res_item["value_usd"] = round(value, 2) if value is not None else None
        for pk in ("provider", "data_tier", "data_status", "is_historical_analysis", "is_live_signal",
                   "is_live_data", "is_fixture", "is_simulated", "is_market_open",
                   "last_trading_session", "last_data_timestamp", "signal_freshness",
                   "simulated_at", "candle_session"):
            if pk in o and o[pk] is not None:
                res_item[pk] = o[pk]
            elif isinstance(plan, dict) and pk in plan and plan[pk] is not None:
                res_item[pk] = plan[pk]
        results.append(res_item)

    res = {
        "date": today,
        "now_new_york": now_local.strftime("%Y-%m-%d %H:%M") if market == "US" else to_eastern(now_utc).strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2), "day_trades_5d": pdt_n if c.get("pdt_guard") else None},
    }
    if market == "IN":
        res["market"] = "IN"
        res["currency"] = "INR"
        res["now_ist"] = now_local.strftime("%Y-%m-%d %H:%M")
    if is_sim_run:
        res["simulated_at"] = _market_iso(now_utc, market=market)
    if permitted_data_times:
        res["last_data_timestamp"] = _market_iso(max(permitted_data_times), market=market)
    return res


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    market = res.get("market", "US")
    tz_label = "IST" if market == "IN" else "New York"
    now_stamp = res.get("now_ist") if market == "IN" else res.get("now_new_york")
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s %s. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], now_stamp, tz_label, money(res["cash_after"], market=market),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = (
            parse_utc(d.get("timestamp"))
            or parse_utc(d.get("last_data_timestamp"))
            or parse_utc(d.get("time"))
            or parse_utc(d.get("fetched"))
            or parse_utc(pdoc.get("last_data_timestamp"))
            or as_of
        )
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("orders_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--market", choices=["US", "IN"], help="market profile: US (default) or IN")
    ap.add_argument("--instruments", default="trader/state/instruments.json", help="path to instruments cache")
    ap.add_argument("--now", help="pretend it is this market time, e.g. \"2026-10-19 10:15\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in market local time)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--allow-off-hours", action="store_true", help="allow paper trade risk evaluation outside market hours")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        orders_file = a.orders_pos or a.orders
        doc = read_json(orders_file, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        if getattr(a, "allow_off_hours", False):
            cfg["allow_off_hours"] = True
        market = getattr(a, "market", None) or get_market_profile(a, orders, cfg)
        now = parse_now(a.now, market=market)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-19")
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(
            orders, cfg, portfolio, prices, state, a.today, now, times,
            market=market, simulated=bool(a.now), source_doc=doc if isinstance(doc, dict) else None,
        )
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            check_ts = now if a.now else datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None)
            rec["checked_at"] = check_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
