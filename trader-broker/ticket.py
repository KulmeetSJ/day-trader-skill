#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact US connector JSON or Indian paper-only simulation payload.
            A bracket order shows its stop-loss, take-profit and the max loss
            at the stop. An order is READY only if trader-risk passed THIS
            exact order today (matched by fingerprint) and nobody has decided
            on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records a US connector result or an Indian simulated paper result:
            placed (with the broker's order id), denied, or failed.
  fill      records a fill in trader/portfolio.json (cash and shares).
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by. It still goes through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 61e6...
    python3 ticket.py fill --symbol TSLA --side buy --qty 8 --price 364.20
    python3 ticket.py flatten --symbol TSLA --qty 8
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
New York dates; --now "2026-10-19 10:15" pins the clock.
"""

import argparse
import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
import re
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
_RISK_DIR = os.path.join(os.path.dirname(_DIR), "trader-risk")
if _RISK_DIR not in sys.path:
    sys.path.insert(0, _RISK_DIR)
_ALERT_DIR = os.path.join(os.path.dirname(_DIR), "trader-alerts")
if _ALERT_DIR not in sys.path:
    sys.path.insert(0, _ALERT_DIR)

# Cross-skill helpers live in ../trader-alerts (added to sys.path above)
try:
    import env_loader  # type: ignore
    env_loader.load_project_dotenv()
except ImportError:
    env_loader = None

try:
    import symbols_in  # type: ignore
    import calendar_in  # type: ignore
    import instruments  # type: ignore
except ImportError:
    symbols_in = None
    calendar_in = None
    instruments = None

import order_fingerprint  # type: ignore

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
PAPER_ORDER_FIELDS = ALPACA_FIELDS + (
    "market", "currency", "trading_style", "product", "multi_session_protection",
)
PAPER_MULTI_SESSION_BROKERS = ("paper", "paper_in", "simulated", "mock")
STRING_FIELDS = ("qty", "notional", "limit_price", "stop_price", "trail_price", "trail_percent",
                 "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TIF_WORDS = {"day": "good for today", "gtc": "good until canceled", "opg": "at the open",
             "cls": "at the close", "ioc": "immediate or cancel", "fok": "fill or kill"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
BUY_WORDS = {"buy", "buying", "bought", "purchase"}
SELL_WORDS = {"sell", "selling", "sold", "short"}
CONFIRM_WORDS = (
    {"yes", "y", "please", "pls", "thanks", "thank", "you",
     "place", "send", "submit", "execute", "confirm", "confirmed", "approve", "approved",
     "proceed", "do", "go", "ahead",
     "order", "trade", "ticket", "entry", "exit", "flatten", "close", "position",
     "market", "mkt", "limit", "lmt", "bracket", "target", "tp", "sl", "loss", "stoploss",
     "take", "profit", "max", "risk", "est", "charges", "about",
     "share", "shares", "sh", "contract", "contracts", "lot", "lots", "qty", "quantity",
     "notional", "amount", "price", "at", "for", "of", "on", "to", "in", "with", "by", "as",
     "and", "only", "this", "that", "the", "my", "a", "an", "it", "one", "now", "today",
     "day", "gtc", "opg", "cls", "ioc", "fok", "good", "til", "until", "canceled", "cancelled",
     "usd", "inr", "rs", "dollar", "dollars", "rupee", "rupees", "nse", "bse", "alpaca", "ibkr", "paper",
     "let's", "it's", "that's", "there's", "i'm", "i'll", "i'd", "we'll", "we're"}
    | BUY_WORDS | SELL_WORDS
)
UPPER_SAFE_WORDS = {"YES", "BUY", "SELL", "USD", "INR", "RS", "NSE", "BSE", "GTC", "OPG", "CLS", "IOC", "FOK", "IBKR"}
_REPLY_TOKEN_RE = re.compile(r"\^?[A-Za-z0-9']+(?:[.\-&:][A-Za-z0-9]+)*")


class TicketError(Exception):
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


def parse_now(text, market="US"):
    """ISO time -> naive UTC. A time without an offset is taken as market local time."""
    if not text:
        return utc_now()
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
        raise TicketError("--now wants a time like \"2026-10-19 10:15\" (%s) or ...Z (UTC)" % lbl)
    if dt.tzinfo is None:
        return local_to_utc(dt, market=market)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def _parse_utc(text):
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
            return eastern_to_utc(datetime.datetime.fromisoformat(s))
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


def _check_no_lookahead(items, now_utc, today, market, strict=False):
    """Validate that no plan/order/risk timestamp is after now_utc when strict (--now); return latest permitted ISO."""
    latest_utc = None
    latest_raw = None
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("last_data_timestamp",):
            raw = item.get(key)
            if raw:
                dt_utc = _parse_utc(raw)
                if dt_utc is not None:
                    if strict and dt_utc > now_utc:
                        raise TicketError(
                            "look-ahead blocked: %s %s is after simulated time %s"
                            % (key, raw, ny_iso(now_utc, market=market))
                        )
                    if dt_utc <= now_utc and (latest_utc is None or dt_utc > latest_utc):
                        latest_utc = dt_utc
                        latest_raw = raw
        plan = item.get("plan") if isinstance(item.get("plan"), dict) else item
        if isinstance(plan, dict) and strict:
            pdate = str(plan.get("date") or "")
            if pdate:
                if pdate > today:
                    raise TicketError(
                        "look-ahead blocked: plan date %s is after simulated date %s" % (pdate, today)
                    )
                if pdate == today and plan.get("entry_time"):
                    et_t = _parse_clock_time(plan.get("entry_time"))
                    if et_t is not None:
                        et_utc = local_to_utc(
                            datetime.datetime.combine(datetime.date.fromisoformat(pdate), et_t),
                            market=market,
                        )
                        if et_utc > now_utc:
                            raise TicketError(
                                "look-ahead blocked: entry time %s on %s is after simulated time %s"
                                % (plan.get("entry_time"), pdate, ny_iso(now_utc, market=market))
                            )
    return ny_iso(latest_utc, market=market) if latest_utc is not None else latest_raw


def get_market_profile(a=None, orders=None, cfg=None, risk=None):
    if a and getattr(a, "market", None):
        return a.market.upper()
    env = os.environ.get("BHARAT_MARKET")
    if env and env.upper() in ("IN", "INDIA"):
        return "IN"
    if risk and isinstance(risk, dict) and str(risk.get("market", "")).upper() in ("IN", "INDIA"):
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
    if orders and isinstance(orders, list):
        for o in orders:
            if not isinstance(o, dict):
                continue
            if o.get("market", "").upper() in ("IN", "INDIA") or o.get("currency", "").upper() == "INR":
                return "IN"
            if isinstance(o.get("plan"), dict) and o["plan"].get("market", "").upper() in ("IN", "INDIA"):
                return "IN"
            sym = str(o.get("symbol", "")).upper()
            if sym.endswith((".NS", ".BO", "-EQ")) or ("-" in sym or "&" in sym):
                return "IN"
    return "US"


def ny_iso(now_utc, market="US"):
    """naive UTC -> formatted wall clock ISO timestamp."""
    if market == "IN":
        return "%s+05:30" % to_local(now_utc, market="IN").replace(microsecond=0).isoformat()
    off = eastern_offset(now_utc)
    return "%s%+03d:00" % (to_eastern(now_utc).replace(microsecond=0).isoformat(), off)


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order, cfg=None, market=None, identity=None, risk_result=None):
    """Canonical fingerprint shared with trader-risk, including its validated identity."""
    if identity is None and isinstance(risk_result, dict):
        identity = risk_result.get("resolved_identity")
    fingerprint_cfg = dict(cfg or {})
    saved_policy = (risk_result or {}).get("risk_parameters") or {}
    saved_overrides = (risk_result or {}).get("fingerprint_overrides") or {}
    for key in ("allow_off_hours", "exchange"):
        if key in saved_overrides:
            fingerprint_cfg[key] = saved_overrides[key]
    if "allow_off_hours" not in fingerprint_cfg and "allow_off_hours" in saved_policy:
        fingerprint_cfg["allow_off_hours"] = saved_policy["allow_off_hours"]
    explicit_exchange = bool(
        isinstance(order, dict) and (
            order.get("exchange") or (isinstance(order.get("plan"), dict) and order["plan"].get("exchange"))
        )
    )
    if "exchange" not in fingerprint_cfg and explicit_exchange and "exchange" in saved_policy:
        fingerprint_cfg["exchange"] = saved_policy["exchange"]
    return order_fingerprint.fingerprint(order, config=fingerprint_cfg, market=market, identity=identity)


def _indian_paper_issue(market, cfg):
    """India is simulated-only; reject unsafe broker or account configuration."""
    if str(market or "").upper() != "IN":
        return None
    if not bool((cfg or {}).get("paper", True)):
        return "Indian orders require paper=true; live execution is not supported"
    broker = str((cfg or {}).get("broker") or "paper").strip().lower()
    if broker != "paper":
        return "Indian orders require broker='paper'; configured broker %r is not allowed" % broker
    return None


def _ensure_indian_paper(market, cfg):
    issue = _indian_paper_issue(market, cfg)
    if issue:
        raise TicketError(issue)


def _market_for_order(order, market=None, cfg=None):
    """Do not let a caller's US override reclassify an explicitly Indian order."""
    order = order if isinstance(order, dict) else {}
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
    markers = (
        order.get("market"), order.get("currency"), order.get("exchange"), order.get("product"),
        plan.get("market"), plan.get("currency"), plan.get("exchange"), plan.get("product"),
        (cfg or {}).get("market"), (cfg or {}).get("currency"),
    )
    if any(str(value or "").strip().upper() in ("IN", "INDIA", "INR", "NSE", "BSE", "MIS", "CNC", "NRML")
           for value in markers):
        return "IN"
    symbol = str(order.get("symbol") or "").strip().upper()
    if symbol.endswith((".NS", ".BO", "-EQ")) or symbol.startswith(("NSE:", "BSE:")):
        return "IN"
    if symbols_in and (symbols_in.is_index_symbol(symbol) or symbols_in.is_derivative_symbol(symbol)):
        return "IN"
    return str(market or get_market_profile(None, orders=[order], cfg=cfg)).upper()


def _external_identity_issue(order, risk_result, market):
    """Require explicit, risk-validated US identity before an external ticket can be emitted."""
    if str(market or "").upper() != "US":
        return None
    order = order if isinstance(order, dict) else {}
    identity = risk_result.get("resolved_identity") if isinstance(risk_result, dict) else None
    symbol = str(order.get("symbol") or "").strip().upper()
    asset_type = str(order.get("asset_type") or "").strip().upper()
    if str(order.get("market") or "").strip().upper() != "US":
        return "US external orders require explicit order market='US'"
    if str(order.get("currency") or "").strip().upper() != "USD":
        return "US external orders require explicit order currency='USD'"
    if asset_type not in ("EQUITY", "STOCK", "ETF"):
        return "US external orders require a validated equity or ETF asset_type"
    if not isinstance(identity, dict):
        return "US external orders require a resolved instrument identity from trader-risk"
    if identity.get("validation_status") != "RISK_VALIDATED_US_EQUITY":
        return "US external orders require a risk-validated US equity/ETF identity"
    expected = {
        "symbol": symbol,
        "market": "US",
        "currency": "USD",
        "asset_type": asset_type,
    }
    for key, value in expected.items():
        actual = identity.get(key)
        if key in ("symbol", "market", "currency", "asset_type"):
            actual = str(actual or "").strip().upper()
        if actual != value:
            return "US external order identity is missing or conflicts on %s" % key
    return None


def is_positional_gtt(order):
    return str(order.get("order_class") or "").strip().lower() == "gtt_oco"


def positional_order_issue(order, cfg=None, market=None):
    """Return an explicit reason if a positional entry order violates multi-session safety rules."""
    side = str(order.get("side") or "").strip().lower()
    oc = str(order.get("order_class") or "").strip().lower()
    plan = order.get("plan") or {}
    style = str(order.get("trading_style") or plan.get("trading_style") or "").strip().lower()
    flat_p = str(plan.get("flat_by") or "").strip().lower()
    is_pos = (style == "positional" or oc == "gtt_oco" or (flat_p == "positional" and side != "sell"))
    if not is_pos:
        return None
    if side == "sell" and oc != "gtt_oco":
        return None
    tif = str(order.get("time_in_force") or "").strip().lower()
    if tif == "day":
        return "positional order must not use intraday time_in_force='day'; use 'gtc' with gtt_oco"
    if tif != "gtc":
        return "positional order requires time_in_force='gtc', got %r" % tif
    if oc == "bracket":
        return "positional order must not inherit intraday order_class='bracket'; use 'gtt_oco'"
    if oc != "gtt_oco":
        return "positional order requires order_class='gtt_oco', got %r" % (oc or None)
    eff_market = str(market or order.get("market") or (cfg or {}).get("market") or "US").upper()
    if eff_market != "IN":
        return "positional GTT OCO orders are supported only in Indian paper mode (market='IN')"
    c = cfg or {}
    if not bool(c.get("paper", True)):
        return "positional GTT OCO orders are supported in paper mode only (paper=true)"
    if c.get("broker_supports_multi_session") is False or c.get("supports_multi_session_protection") is False:
        return "configured broker/schema disables multi-session protection"
    broker = str(c.get("broker") or ("paper" if eff_market == "IN" else "alpaca")).strip().lower()
    if broker not in PAPER_MULTI_SESSION_BROKERS:
        return "broker %r cannot safely represent multi-session GTT OCO protection; use broker='paper'" % broker
    product = str(order.get("product") or "").strip().upper()
    if product not in ("CNC", "NRML"):
        return "positional order must declare product 'CNC' or 'NRML', got %r" % order.get("product")
    prot = order.get("multi_session_protection")
    if not isinstance(prot, dict) or prot.get("persists_across_sessions") is not True:
        return "positional order is missing multi_session_protection with persists_across_sessions=true"
    if order.get("take_profit_limit_price") is None or order.get("stop_loss_stop_price") is None:
        return "positional GTT OCO order requires both stop_loss_stop_price and take_profit_limit_price"
    return None


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tool_input(order):
    """The exact arguments for place_stock_order (or paper GTT OCO payload for positional Indian orders)."""
    out = {}
    fields = PAPER_ORDER_FIELDS if is_positional_gtt(order) else ALPACA_FIELDS
    for k in fields:
        if k in order and order[k] is not None:
            out[k] = _str_num(order[k]) if k in STRING_FIELDS else order[k]
    return out


def _price_scale(v=None, tick=None):
    scale = 2
    if tick is not None:
        try:
            td = Decimal(str(tick).strip()).normalize()
            if td.is_finite() and td > 0 and td.as_tuple().exponent < 0:
                scale = max(scale, -td.as_tuple().exponent)
        except (InvalidOperation, ValueError, AttributeError):
            pass
    if v is not None:
        try:
            vd = Decimal(str(v).strip()).normalize()
            if vd.is_finite() and vd.as_tuple().exponent < 0:
                v_exp = -vd.as_tuple().exponent
                if v_exp <= 4:
                    scale = max(scale, v_exp)
        except (InvalidOperation, ValueError, AttributeError):
            pass
    return scale


def _money(v, market="US", tick=None):
    curr = "₹" if market == "IN" else "$"
    scale = _price_scale(v, tick=tick)
    try:
        d = Decimal(str(v).strip())
        q = Decimal("1").scaleb(-scale)
        s = "{:,f}".format(d.quantize(q, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, AttributeError):
        s = ("{:,." + str(scale) + "f}").format(float(v))
    return curr + (s[:-3] if (scale == 2 and s.endswith(".00")) else s)


def _p(v, tick=None):
    scale = _price_scale(v, tick=tick)
    try:
        d = Decimal(str(v).strip())
        q = Decimal("1").scaleb(-scale)
        return format(d.quantize(q, rounding=ROUND_HALF_UP), "f")
    except (InvalidOperation, ValueError, AttributeError):
        return "%.*f" % (scale, float(v))


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def max_loss(order, risk_result=None):
    """(amount lost, stop) if the stop fills exactly at the stop, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif isinstance(risk_result, dict) and _f(risk_result.get("max_loss")) is not None:
        return round(_f(risk_result["max_loss"]), 2), stop
    else:
        entry = _f((risk_result or {}).get("entry_ref")) or _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(Decimal(str(entry)) - Decimal(str(stop)))
    return round(float(Decimal(str(q)) * risk), 2), stop


def plain_parts(order, market=None, risk_result=None):
    """The ticket in plain words, one part per line."""
    if market is None:
        market = order.get("market") or ("IN" if order.get("currency") == "INR" else "US")
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    typ = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    contracts = order.get("contracts")
    plan_doc = order.get("plan") if isinstance(order.get("plan"), dict) else {}
    tick = order.get("tick_size") if order.get("tick_size") is not None else plan_doc.get("tick_size")
    if is_bracket(order) or is_positional_gtt(order):
        how = "limit %s" % _p(order["limit_price"], tick=tick) if typ == "limit" and order.get("limit_price") else typ
        if contracts:
            qty_desc = "%s (%s contract%s)" % (_str_num(order.get("qty")), contracts, "" if contracts == 1 else "s")
        else:
            qty_desc = _str_num(order.get("qty"))
        if is_positional_gtt(order):
            prod = order.get("product", "CNC")
            parts = ["%s %s %s, %s, %s, %s delivery (GTT OCO across sessions)" % (side, qty_desc, sym, how, tif, prod)]
        else:
            parts = ["%s %s %s, %s, %s" % (side, qty_desc, sym, how, tif)]
        if order.get("stop_loss_stop_price") is not None:
            leg = "stop-loss %s" % _p(order["stop_loss_stop_price"], tick=tick)
            if order.get("stop_loss_limit_price") is not None:
                leg += " (limit %s)" % _p(order["stop_loss_limit_price"], tick=tick)
            parts.append(leg)
        if order.get("take_profit_limit_price") is not None:
            parts.append("take-profit %s" % _p(order["take_profit_limit_price"], tick=tick))
        loss, stop = max_loss(order, risk_result)
        if loss is not None:
            parts.append("max loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                         % (_money(loss, market=market), _p(stop, tick=tick)))
        if order.get("transaction_costs"):
            tc = order["transaction_costs"]
            parts.append("est charges %s" % _money(tc["total"], market=market))
        return parts
    if order.get("notional") is not None:
        what = "%s of %s" % (_money(order["notional"], market=market), sym)
    else:
        q = _str_num(order.get("qty"))
        what = "%s share%s of %s" % (q, "" if q == "1" else "s", sym)
    if typ == "limit":
        how = "limit order at %s" % _money(order.get("limit_price"), market=market, tick=tick)
    elif typ == "stop":
        how = "stop order at %s" % _money(order.get("stop_price"), market=market, tick=tick)
    elif typ == "stop_limit":
        how = "stop %s, limit %s" % (
            _money(order.get("stop_price"), market=market, tick=tick),
            _money(order.get("limit_price"), market=market, tick=tick),
        )
    else:
        how = "%s order" % typ.replace("_", " ")
    return ["%s %s, %s, %s" % (side, what, how, tif)]


def plain(order, market=None, risk_result=None):
    """BUY $1,250 of AAPL, market order, good for today
    BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about $46.88 ..."""
    return " / ".join(plain_parts(order, market=market, risk_result=risk_result))


def amount_numbers(order):
    nums = []
    for k in ("notional", "qty", "limit_price", "stop_loss_stop_price", "take_profit_limit_price",
              "stop_loss_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def _exchange_tag(symbol):
    u = str(symbol or "").strip().upper()
    if u.startswith("NSE:") or u.endswith(".NS"):
        return "NSE"
    if u.startswith("BSE:") or u.endswith(".BO"):
        return "BSE"
    return None


def _strip_symbol_affixes(symbol):
    u = str(symbol or "").strip().upper()
    for pref in ("NSE:", "BSE:"):
        if u.startswith(pref):
            u = u[len(pref):]
            break
    for suf in (".NS", ".BO", "-EQ"):
        if u.endswith(suf) and len(u) > len(suf):
            u = u[:-len(suf)]
            break
    return u


def _norm_symbol(symbol):
    u = str(symbol or "").strip().upper()
    if not u:
        return ""
    if symbols_in is not None:
        try:
            return symbols_in.normalize_symbol(u)
        except Exception:
            pass
    return _strip_symbol_affixes(u)


def _symbol_matches(token, target_symbol):
    tok = str(token or "").strip().upper()
    sym = str(target_symbol or "").strip().upper()
    if not tok or not sym:
        return False
    if tok == sym:
        return True
    tok_exch = _exchange_tag(tok)
    sym_exch = _exchange_tag(sym)
    if tok_exch and sym_exch and tok_exch != sym_exch:
        return False
    return bool(_norm_symbol(tok) and _norm_symbol(tok) == _norm_symbol(sym))


def _explicit_qty_spans(text, target_symbol):
    """Find numeric strings in explicit order-quantity positions (e.g. 'buy 26 RELIANCE', '8 shares')."""
    spans = []
    qty_after_side = re.compile(
        r"\b(?:buy|buying|bought|purchase|sell|selling|sold|short)\s+(-?\d[\d,]*(?:\.\d+)?)(?![\w.])",
        re.I,
    )
    for m in qty_after_side.finditer(text):
        spans.append((m.start(1), m.end(1), m.group(1)))

    qty_before_unit = re.compile(
        r"(?<![A-Za-z0-9.$\u20b9-])(-?\d[\d,]*(?:\.\d+)?)\s+(?:shares?|sh|contracts?|lots?)\b",
        re.I,
    )
    for m in qty_before_unit.finditer(text):
        spans.append((m.start(1), m.end(1), m.group(1)))

    base_sym = _strip_symbol_affixes(target_symbol)
    if base_sym:
        esc = re.escape(base_sym)
        # Allow optional exchange prefix/suffix on the symbol in '8 TSLA' / '26 RELIANCE.NS'
        sym_pat = r"(?:(?:NSE|BSE):)?" + esc + r"(?:\.(?:NS|BO)|-EQ)?"
        qty_before_sym = re.compile(
            r"(?<![A-Za-z0-9.$\u20b9-])(-?\d[\d,]*(?:\.\d+)?)\s+" + sym_pat + r"(?![A-Za-z0-9])",
            re.I,
        )
        price_prefixes = {"at", "limit", "lmt", "stop", "target", "tp", "sl", "loss", "price", "rs", "inr", "usd"}
        for m in qty_before_sym.finditer(text):
            prefix = text[:m.start(1)].rstrip()
            if prefix.endswith(("@", "$", "\u20b9")):
                continue
            prev_words = re.findall(r"[a-z]+", prefix.lower())
            if prev_words and prev_words[-1] in price_prefixes:
                continue
            spans.append((m.start(1), m.end(1), m.group(1)))
    return spans


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"

    order_side = str(order.get("side") or "").strip().lower()
    reply_sides = set()
    if BUY_WORDS & set(words):
        reply_sides.add("buy")
    if SELL_WORDS & set(words):
        reply_sides.add("sell")
    if len(reply_sides) > 1:
        return False, "the reply names conflicting sides, but this order is %s" % plain(order)
    if reply_sides and order_side and reply_sides != {order_side}:
        return False, "the reply says %s, but this order is %s" % (next(iter(reply_sides)), plain(order))

    sym = str(order.get("symbol", "")).strip().upper()
    others = {str(s).strip().upper() for s in (batch_symbols or ()) if s and str(s).strip()} - ({sym} if sym else set())
    raw_has_lower = any(c.islower() for c in raw)
    sym_spans = []
    for m in _REPLY_TOKEN_RE.finditer(raw):
        tok = m.group(0)
        if not any(c.isalpha() for c in tok):
            continue
        sym_spans.append((m.start(), m.end()))
        if _symbol_matches(tok, sym):
            continue
        low = tok.lower()
        in_others = any(_symbol_matches(tok, o) for o in others)
        if in_others and (low not in CONFIRM_WORDS or tok.isupper()):
            return False, "the reply names %s, but this order is for %s" % (tok.upper(), sym)
        if low not in CONFIRM_WORDS or (raw_has_lower and tok.isupper() and tok.upper() not in UPPER_SAFE_WORDS):
            return False, "the reply names %s, but this order is for %s" % (tok.upper(), sym)

    chars = list(raw)
    for start, end in sym_spans:
        for i in range(start, end):
            chars[i] = " "
    masked = "".join(chars)
    nums = [float(n.replace(",", "")) for n in re.findall(r"(?<![A-Za-z0-9.])(-?\d[\d,]*(?:\.\d+)?)", masked)]
    if nums:
        want = amount_numbers(order)
        if any(not any(abs(n - w) < 0.005 for w in want) for n in nums):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order))
        order_qty = _f(order.get("qty"))
        if order_qty is not None:
            for _, _, q_str in _explicit_qty_spans(raw, sym):
                q_val = float(q_str.replace(",", ""))
                if abs(q_val - order_qty) >= 0.005:
                    return False, "the reply says qty %s, but this order is %s" % (_str_num(q_val), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in (state or {}).get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    """Legacy exact-fingerprint lookup API; kept stable for existing callers."""
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def _risk_order_verdict(risk, order, today, cfg=None, market=None):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)", None
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date"), None
    for r in risk.get("results", []):
        if str(r.get("symbol") or "").upper() != str(order.get("symbol") or "").upper():
            continue
        if str(r.get("side") or "").lower() != str(order.get("side") or "").lower():
            continue
        fp = fingerprint(order, cfg=cfg, market=market, risk_result=r)
        if r.get("fingerprint") == fp:
            return r, None, fp
    return None, "this exact order was not in the last risk check (it changed or is new, including risk context); run check.py again", None


def status_of(order, risk, state, today, cfg=None, market=None):
    """(ready, label, risk_result)"""
    if not isinstance(order, dict):
        return False, "BLOCKED: order must be a valid object", None
    market = _market_for_order(order, market, cfg)
    india_issue = _indian_paper_issue(market, cfg)
    if india_issue:
        return False, "BLOCKED: " + india_issue, None
    try:
        _assert_tradable_instrument(order.get("symbol"), order)
    except TicketError as exc:
        return False, "BLOCKED instrument check: " + str(exc), None
    issue = positional_order_issue(order, cfg=cfg, market=market)
    if issue:
        return False, "BLOCKED positional check: " + issue, None
    rv, why, fp = _risk_order_verdict(risk, order, today, cfg=cfg, market=market)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    identity_issue = _external_identity_issue(order, rv, market)
    if identity_issue:
        return False, "BLOCKED instrument identity: " + identity_issue, rv
    opt_entry = (
        str(order.get("asset_type") or "").upper() == "OPTIONS"
        or bool(order.get("option_contract"))
        or bool(symbols_in and symbols_in.is_derivative_symbol(str(order.get("symbol") or ""))
                and symbols_in.parse_derivative_symbol(str(order.get("symbol"))) .get("asset_type") == "OPTIONS")
    )
    if opt_entry and kind_of(order, rv) == "entry":
        return False, "BLOCKED: OPTION RISK UNAVAILABLE; option entry premium risk is not validated", rv
    if kind_of(order, rv) == "entry" and bool((cfg or {}).get("require_stop", True)):
        actual_stop = _f(order.get("stop_loss_stop_price"))
        risk_stop = _f(rv.get("stop"))
        if actual_stop is None:
            return False, "BLOCKED: risk PASS has no protective stop in the actual order", rv
        if risk_stop is None or Decimal(str(actual_stop)) != Decimal(str(risk_stop)):
            return False, "BLOCKED: risk stop does not match the protective stop in the actual order", rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            next_step = "paper result" if market == "IN" else "the place_stock_order call"
            return False, "APPROVED by you at %s, waiting for %s" % (rec.get("at"), next_step), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- signal provenance

SIGNAL_PROVENANCE_KEYS = (
    "provider", "data_tier", "data_status", "is_historical_analysis",
    "is_live_signal", "is_live_data", "is_fixture", "is_simulated",
    "is_market_open", "last_trading_session", "last_data_timestamp",
    "signal_freshness", "candle_session", "simulated_at", "clock_override",
)


def _is_market_open_at(now_utc, market="US"):
    """Return True if market continuous trading session is open at now_utc."""
    if now_utc is None:
        return True
    if str(market).upper() == "IN":
        local_ist = to_local(now_utc, market="IN")
        if calendar_in is not None:
            dt_ist = local_ist.replace(tzinfo=calendar_in.IST)
            return bool(calendar_in.is_market_hours_in(dt_ist, exchange="NSE"))
        if local_ist.weekday() >= 5:
            return False
        return datetime.time(9, 15) <= local_ist.time() < datetime.time(15, 30)
    local_et = to_eastern(now_utc)
    if local_et.weekday() >= 5:
        return False
    return datetime.time(9, 30) <= local_et.time() < datetime.time(16, 0)


def _meta_sources(order, doc=None, risk=None, rv=None):
    sources = []
    if isinstance(order, dict):
        sources.append(order)
        if isinstance(order.get("plan"), dict):
            sources.append(order["plan"])
    for extra in (rv, doc, risk):
        if isinstance(extra, dict):
            sources.append(extra)
    return sources


def _first_meta(key, sources):
    for src in sources:
        if key in src and src[key] is not None:
            return src[key]
    return None


def signal_provenance(order, doc=None, risk=None, rv=None, today=None, simulated_at=None,
                      last_data_timestamp=None, clock_override=None, now=None, market=None):
    """Extract and safely classify signal provenance (historical, delayed, fixture, simulated, live, or unverified).

    Delayed/Yahoo, historical, fixture, simulated, clock-overridden (--now), closed/stale,
    and missing/incomplete metadata are NEVER marked as live.
    """
    sources = _meta_sources(order, doc=doc, risk=risk, rv=rv)
    provider = _first_meta("provider", sources)
    if provider in (None, ""):
        for src in sources:
            src_val = src.get("source")
            if isinstance(src_val, str) and src_val.strip().lower() in ("yahoo", "offline", "simulated", "fixture"):
                src_l = src_val.strip().lower()
                provider = "offline" if src_l == "fixture" else src_l
                break
    provider = str(provider).strip() if provider not in (None, "") else None
    prov_l = provider.lower() if provider else ""

    raw_tier = _first_meta("data_tier", sources)
    tier_u = str(raw_tier).strip().upper() if raw_tier not in (None, "") else ""
    raw_status = _first_meta("data_status", sources)
    status_l = str(raw_status).strip().lower() if raw_status not in (None, "") else ""
    raw_hist = _first_meta("is_historical_analysis", sources)
    raw_live_sig = _first_meta("is_live_signal", sources)
    raw_live_data = _first_meta("is_live_data", sources)
    raw_fixture = _first_meta("is_fixture", sources)
    raw_sim = _first_meta("is_simulated", sources)
    opt_c = _first_meta("option_contract", sources)
    raw_market_open = _first_meta("is_market_open", sources)
    last_session = _first_meta("last_trading_session", sources)
    freshness = _first_meta("signal_freshness", sources)
    candle_session = _first_meta("candle_session", sources)
    sim_val = simulated_at or _first_meta("simulated_at", sources)
    lts_val = last_data_timestamp or _first_meta("last_data_timestamp", sources)
    raw_clock_override = _first_meta("clock_override", sources)

    fresh_l = str(freshness or "").strip().lower()
    is_yahoo = (prov_l == "yahoo")
    is_fixture = bool(raw_fixture) or (tier_u == "FIXTURE") or (prov_l == "offline")
    opt_sim = isinstance(opt_c, dict) and (
        bool(opt_c.get("is_simulated")) or str(opt_c.get("tier") or "").strip().upper() == "SIMULATED"
    )
    order_sim = isinstance(order, dict) and str(order.get("tier") or "").strip().upper() == "SIMULATED"
    is_simulated = bool(raw_sim) or (tier_u == "SIMULATED") or (prov_l == "simulated") or opt_sim or order_sim
    is_historical = bool(raw_hist) or (tier_u == "HISTORICAL") or (status_l == "historical") or (fresh_l == "historical")
    explicit_clock_override = bool(clock_override) or bool(raw_clock_override)
    is_clock_override = explicit_clock_override or bool(sim_val)

    session_date = str(candle_session or last_session or (str(lts_val)[:10] if lts_val else "") or "").strip()
    is_prior_session = bool(today and re.match(r"^\d{4}-\d{2}-\d{2}$", session_date) and session_date < today)
    closed_now = bool(now is not None and not _is_market_open_at(now, market=market or "US"))
    lts_utc = _parse_utc(lts_val) if (now is not None and lts_val) else None
    stale_now = bool(
        now is not None and lts_utc is not None
        and ((now - lts_utc).total_seconds() > 1800 or (now - lts_utc).total_seconds() < -5)
    )

    is_live_data_out = bool(raw_live_data) if raw_live_data is not None else None
    is_fixture_out = True if is_fixture else (bool(raw_fixture) if raw_fixture is not None else None)
    is_sim_out = True if is_simulated else (bool(raw_sim) if raw_sim is not None else None)

    if is_fixture:
        classification = "FIXTURE"
        data_tier = "FIXTURE"
        is_hist_out = bool(is_historical)
        is_live_sig = False
        if is_live_data_out is not None or raw_live_data:
            is_live_data_out = False
        warning = "FIXTURE SIGNAL (OFFLINE REPLAY) -- NOT A CURRENT ENTRY RECOMMENDATION"
    elif is_simulated:
        classification = "SIMULATED"
        data_tier = "SIMULATED"
        is_hist_out = bool(is_historical)
        is_live_sig = False
        if is_live_data_out is not None or raw_live_data:
            is_live_data_out = False
        warning = "SIMULATED SIGNAL -- SYNTHETIC / ANALYTICAL DATA ONLY, NOT A CURRENT ENTRY RECOMMENDATION"
    elif (
        is_historical
        or (is_prior_session and (tier_u == "CURRENT" or raw_live_sig is True))
        or (
            (tier_u == "CURRENT" or raw_live_sig is True)
            and prov_l not in ("", "unknown", "none", "yahoo", "offline", "simulated", "fixture")
            and (is_clock_override or closed_now or stale_now)
        )
    ):
        classification = "HISTORICAL"
        data_tier = "HISTORICAL"
        is_hist_out = True
        is_live_sig = False
        is_live_data_out = False
        if is_clock_override and not is_historical and not is_prior_session:
            warning = "HISTORICAL SIGNAL (CLOCK OVERRIDDEN BY --now) -- NOT A CURRENT ENTRY RECOMMENDATION"
        else:
            warning = "HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION"
    elif is_yahoo or tier_u == "DELAYED" or status_l == "delayed":
        classification = "DELAYED"
        data_tier = "DELAYED"
        is_hist_out = False
        is_live_sig = False
        is_live_data_out = False
        warning = "DELAYED SIGNAL (%s) -- DELAYED MARKET DATA, NOT A LIVE REAL-TIME ENTRY" % (provider or "delayed feed")
    elif (
        tier_u == "CURRENT"
        and raw_live_sig is True
        and prov_l not in ("", "unknown", "none", "yahoo", "offline", "simulated", "fixture")
        and raw_live_data is not False
        and raw_market_open is not False
        and not is_prior_session
        and not is_clock_override
        and not closed_now
        and not stale_now
    ):
        classification = "LIVE"
        data_tier = "CURRENT"
        is_hist_out = False
        is_live_sig = True
        if is_live_data_out is None:
            is_live_data_out = True
        warning = None
    else:
        classification = "UNVERIFIED"
        data_tier = tier_u if (tier_u and tier_u != "CURRENT") else "UNKNOWN"
        is_hist_out = bool(is_historical)
        is_live_sig = False
        if tier_u == "CURRENT" or is_yahoo:
            is_live_data_out = False
        warning = "UNVERIFIED SIGNAL PROVENANCE -- MISSING OR INCOMPLETE METADATA; NOT TREATED AS LIVE"

    return {
        "provider": provider,
        "data_tier": data_tier,
        "is_historical_analysis": is_hist_out,
        "is_live_signal": is_live_sig,
        "is_live_data": is_live_data_out,
        "is_fixture": is_fixture_out,
        "is_simulated": is_sim_out,
        "is_market_open": bool(raw_market_open) if raw_market_open is not None else None,
        "data_status": raw_status,
        "last_trading_session": last_session,
        "signal_freshness": freshness,
        "candle_session": candle_session,
        "simulated_at": sim_val,
        "clock_override": True if explicit_clock_override else None,
        "last_data_timestamp": lts_val,
        "signal_classification": classification,
        "signal_warning": warning,
    }


def _signal_summary_line(prov):
    details = [
        "tier=%s" % prov["data_tier"],
        "provider=%s" % (prov["provider"] or "unknown"),
        "live_signal=%s" % ("yes" if prov["is_live_signal"] else "no"),
    ]
    if prov.get("signal_freshness"):
        details.append("freshness=%s" % prov["signal_freshness"])
    if prov.get("last_trading_session"):
        details.append("session=%s" % prov["last_trading_session"])
    if prov.get("last_data_timestamp"):
        details.append("last_data=%s" % prov["last_data_timestamp"])
    if prov.get("simulated_at"):
        details.append("simulated_at=%s" % prov["simulated_at"])
    if prov.get("clock_override"):
        details.append("clock_override=--now")
    return "  signal [%s]: %s" % (prov["signal_classification"], ", ".join(details))


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today, market="US", doc=None,
           simulated_at=None, last_data_timestamp=None, clock_override=None, now=None):
    if not isinstance(order, dict):
        return "#%d: BLOCKED: order must be a valid object" % idx, False
    market = _market_for_order(order, market, cfg)
    ready, label, rv = status_of(order, risk, state, today, cfg=cfg, market=market)
    prov = signal_provenance(order, doc=doc, risk=risk, rv=rv, today=today,
                             simulated_at=simulated_at, last_data_timestamp=last_data_timestamp,
                             clock_override=clock_override, now=now, market=market)
    default_broker = "paper" if market == "IN" else "alpaca"
    broker = str(cfg.get("broker", default_broker)).lower()
    paper = cfg.get("paper", True)
    acct = "BLOCKED CONFIG" if market == "IN" and _indian_paper_issue(market, cfg) else ("PAPER account" if paper else "LIVE ACCOUNT, REAL MONEY")
    broker_name = "Paper Broker (NSE/BSE)" if market == "IN" else ("Interactive Brokers" if broker == "ibkr" else "Alpaca")
    lines = ["ORDER %d of %d, %s, %s" % (idx, total, acct, broker_name)]
    if prov.get("signal_warning"):
        lines.append("  WARNING: " + prov["signal_warning"])
    lines.append(_signal_summary_line(prov))
    for part in plain_parts(order, market=market, risk_result=rv):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    identity_issue = _external_identity_issue(order, rv, market)
    if market == "IN" and _indian_paper_issue(market, cfg):
        lines.append("  No order payload prepared: Indian orders require the paper broker configuration.")
    elif market == "IN" and is_positional_gtt(order):
        lines.append("  Paper broker positional GTT OCO payload (simulated CNC/NRML delivery; persists across sessions):")
        for l in json.dumps(tool_input(order), indent=2).splitlines():
            lines.append("    " + l)
        lines.append("  Paper broker arms GTT OCO stop-loss (%s) and take-profit (%s) across sessions under product %s."
                     % (order.get("stop_loss_stop_price"), order.get("take_profit_limit_price"),
                        order.get("product", "CNC")))
    elif market == "IN":
        lines.append("  Paper MIS simulation payload (not an external broker order):")
        for l in json.dumps(tool_input(order), indent=2).splitlines():
            lines.append("    " + l)
        lines.append("  Approval records the simulated paper order; use ticket.py result/fill to update the paper portfolio.")
    elif identity_issue:
        lines.append("  No external order payload prepared: " + identity_issue + ".")
    elif broker == "ibkr":
        lines.append("  Claude drafts this with the Interactive Brokers connector. IBKR sends it to your")
        lines.append("  IBKR account, and it only goes to the market after you approve it there.")
    else:
        lines.append("  Claude will call place_stock_order with exactly:")
        for l in json.dumps(tool_input(order), indent=2).splitlines():
            lines.append("    " + l)
        if is_bracket(order):
            if market == "IN":
                lines.append("  Paper broker records the entry, stop-loss and take-profit as linked bracket legs (paper mode).")
            else:
                lines.append("  Alpaca places the entry, then the stop-loss and the take-profit as two orders")
                lines.append("  that cancel each other: when one fills, the other is canceled.")
    if ready:
        if market == "IN":
            lines.append("  Reply \"yes\" to record this simulated Indian paper order, or \"no\" to skip it.")
        elif is_positional_gtt(order):
            lines.append("  Reply \"yes\" to record paper approval for this positional GTT OCO order, or \"no\" to skip it.")
            lines.append("  Simulated paper mode only: no live broker execution occurs.")
        else:
            lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
            if broker != "ibkr":
                lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, market="US", doc=None,
                simulated_at=None, last_data_timestamp=None, clock_override=None, now=None):
    if not isinstance(order, dict):
        return {
            "index": idx,
            "ready": False,
            "status": "BLOCKED: order must be a valid object",
            "plain": "INVALID ORDER",
            "fingerprint": "",
            "paper": True,
            "broker": "paper",
        }
    market = _market_for_order(order, market, cfg)
    ready, label, rv = status_of(order, risk, state, today, cfg=cfg, market=market)
    prov = signal_provenance(order, doc=doc, risk=risk, rv=rv, today=today,
                             simulated_at=simulated_at, last_data_timestamp=last_data_timestamp,
                             clock_override=clock_override, now=now, market=market)
    loss, stop = max_loss(order, rv)
    identity_issue = _external_identity_issue(order, rv, market)
    default_broker = "paper" if market == "IN" else "alpaca"
    res = {"index": idx, "ready": ready, "status": label, "plain": plain(order, market=market, risk_result=rv),
           "fingerprint": fingerprint(order, cfg=cfg, market=market, risk_result=rv), "paper": bool(cfg.get("paper", True)),
           "broker": cfg.get("broker", default_broker),
           "tool": (None if _indian_paper_issue(market, cfg) else ("paper_gtt_oco" if is_positional_gtt(order) else "paper_mis")) if market == "IN" else (None if identity_issue else "place_stock_order"),
           "tool_input": (None if _indian_paper_issue(market, cfg) else tool_input(order)) if market == "IN" else (None if identity_issue else tool_input(order)),
           "why": order.get("why", ""),
           "bracket": is_bracket(order), "max_loss": loss, "stop": stop,
           "data_tier": prov["data_tier"],
           "is_historical_analysis": prov["is_historical_analysis"],
           "is_live_signal": prov["is_live_signal"],
           "provider": prov["provider"],
           "signal_classification": prov["signal_classification"],
           "signal_warning": prov["signal_warning"]}
    if is_positional_gtt(order):
        res["positional"] = True
        res["trading_style"] = order.get("trading_style", "positional")
        res["product"] = order.get("product")
        res["multi_session_protection"] = order.get("multi_session_protection")
    if market == "IN":
        res["market"] = "IN"
        res["currency"] = "INR"
        tick_val = order.get("tick_size") if order.get("tick_size") is not None else (order.get("plan") or {}).get("tick_size")
        if tick_val is not None:
            res["tick_size"] = tick_val
        exch_val = order.get("exchange") or (order.get("plan") or {}).get("exchange")
        if exch_val:
            res["exchange"] = exch_val
    if order.get("asset_type"):
        res["asset_type"] = order["asset_type"]
    if order.get("contracts"):
        res["contracts"] = order["contracts"]
    if order.get("lot_size"):
        res["lot_size"] = order["lot_size"]
    if order.get("transaction_costs"):
        res["transaction_costs"] = order["transaction_costs"]
    for pk in ("is_live_data", "is_fixture", "is_simulated", "is_market_open",
               "data_status", "last_trading_session", "signal_freshness", "candle_session",
               "clock_override"):
        if prov.get(pk) is not None:
            res[pk] = prov[pk]
    if prov.get("simulated_at"):
        res["simulated_at"] = prov["simulated_at"]
    if prov.get("last_data_timestamp"):
        res["last_data_timestamp"] = prov["last_data_timestamp"]
    return res


# ---------------------------------------------------------------- fills -> portfolio

def _assert_tradable_instrument(symbol, doc=None):
    """Refuse direct equity/broker execution or paper fills on spot benchmark indices."""
    raw = str(symbol or "").strip().upper()
    norm = symbols_in.normalize_symbol(raw) if symbols_in else raw
    is_deriv = bool(symbols_in and symbols_in.is_derivative_symbol(norm))
    spot_index = bool(symbols_in and symbols_in.is_index_symbol(norm) and not is_deriv)
    if isinstance(doc, dict):
        declared_derivative = str(doc.get("asset_type") or "").upper() in ("OPTIONS", "FUTURES") or bool(doc.get("option_contract"))
        if spot_index or (declared_derivative and not is_deriv):
            raise TicketError(
                "%s is not a listed derivative contract; spot indices are analytical only and caller metadata cannot make them tradable" % norm
            )
    if spot_index:
        raise TicketError(
            "%s is a spot index (analytical only, not a directly tradable equity); "
            "use index F&O derivative contracts instead" % norm
        )
    if is_deriv:
        if not instruments:
            raise TicketError("authoritative instrument master is required for derivative tickets")
        expected = (doc or {}).get("option_contract") if isinstance(doc, dict) else None
        try:
            instruments.validate_listed_derivative_contract(raw, exchange=(doc or {}).get("exchange"), expected=expected)
        except Exception as exc:
            raise TicketError("derivative ticket validation failed: %s" % exc)


def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, market=None):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell)."""
    _assert_tradable_instrument(symbol)
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
        if market == "IN" or cfg.get("market") == "IN" or cfg.get("currency") == "INR":
            pf["currency"] = "INR"
    if market is None:
        market = "IN" if pf.get("currency") == "INR" or cfg.get("market") == "IN" else "US"
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    cost = round(qty * price, 2)
    positions = pf.get("positions")
    if positions is None:
        positions = []
        pf["positions"] = positions
    elif not isinstance(positions, list):
        raise TicketError("invalid portfolio %s: 'positions' must be a list" % portfolio_path)
    pos = None
    for p in positions:
        if isinstance(p, dict) and str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side not in ("buy", "sell"):
        raise TicketError("--side wants buy or sell, not %r" % side)
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        used_ids = set()
        for l in lots:
            m = re.match(r"^%s-(\d+)$" % re.escape(symbol), str(l.get("id", "")))
            if m:
                used_ids.add(int(m.group(1)))
        next_num = (max(used_ids) + 1) if used_ids else (len(lots) + 1)
        lots.append({"id": "%s-%d" % (symbol, next_num), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        relieved_cost = 0.0
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            lot_shares = float(lot["shares"])
            lot_cost = float(lot["cost"])
            if left + 1e-9 >= lot_shares:
                take = lot_shares
                take_cost = lot_cost
                lot["cost"] = 0.0
                lot["shares"] = 0.0
            else:
                take = left
                take_cost = round(lot_cost * (take / lot_shares), 2) if lot_shares else 0.0
                lot["cost"] = round(lot_cost - take_cost, 2)
                lot["shares"] = round(lot_shares - take, 6)
            relieved_cost = round(relieved_cost + take_cost, 2)
            left = round(left - take, 6)
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
        gross_pnl = round(cost - relieved_cost, 2)
        pf["last_trade_pnl"] = gross_pnl
        pf["realized_pnl"] = round(float(pf.get("realized_pnl", 0.0)) + gross_pnl, 2)
    write_json(portfolio_path, pf)
    return pf


def _apply_state_meta(state, sim_at=None, lts=None):
    if sim_at:
        state["simulated_at"] = sim_at
    if lts:
        state["last_data_timestamp"] = lts


def log_fill(state_path, now, today, symbol, side, qty, price, market="US", simulated_at=None, last_data_timestamp=None, tick=None, pnl=None):
    """A fill in the log, so trader-risk can count day trades (a buy and a sell the same day)."""
    state = load_state(state_path)
    entry = {
        "at": ny_iso(now, market=market), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
        "qty": _str_num(float(qty)), "price": float(price),
        "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price, tick=tick)),
    }
    if pnl is not None:
        entry["pnl"] = float(pnl)
        entry["realized_pnl"] = float(pnl)
    if market == "IN":
        entry["market"] = "IN"
        entry["currency"] = "INR"
    if simulated_at:
        entry["simulated_at"] = simulated_at
    if last_data_timestamp:
        entry["last_data_timestamp"] = last_data_timestamp
    _apply_state_meta(state, simulated_at, last_data_timestamp)
    state.setdefault("orders", []).append(entry)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a, market="US"):
    now = parse_now(getattr(a, "now", None), market=market)
    today = getattr(a, "today", None) or to_local(now, market=market).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-19")
    return now, today


def _meta_context(doc, orders, order, risk, now, market, explicit_now=False):
    items = [doc] if isinstance(doc, dict) else []
    if isinstance(risk, dict):
        items.append(risk)
    if isinstance(order, dict):
        items.append(order)
        if isinstance(order.get("plan"), dict):
            items.append(order["plan"])
    today = to_local(now, market=market).date().isoformat()
    lts = _check_no_lookahead(items, now, today, market=market, strict=explicit_now)
    is_sim = explicit_now or any(isinstance(x, dict) and x.get("simulated_at") for x in items)
    sim_at = ny_iso(now, market=market) if is_sim else None
    return sim_at, lts


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    market = get_market_profile(a, orders=orders, cfg=cfg, risk=risk)
    now, today = _clock(a, market=market)
    items = [doc] if isinstance(doc, dict) else []
    if isinstance(risk, dict):
        items.append(risk)
    for o in orders:
        if isinstance(o, dict):
            items.append(o)
            if isinstance(o.get("plan"), dict):
                items.append(o["plan"])
    _check_no_lookahead(items, now, today, market=market, strict=bool(getattr(a, "now", None)))
    return orders, cfg, risk, state, today, now, market


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    doc = read_json(a.orders, "orders file", required=False)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = []
        explicit_now = bool(getattr(a, "now", None))
        for i, o in picks:
            sim_at, lts = _meta_context(doc, orders, o, risk, now, market, explicit_now=explicit_now)
            out.append(ticket_json(o, i, cfg, risk, state, today, market=market, doc=doc,
                                   simulated_at=sim_at, last_data_timestamp=lts,
                                   clock_override=explicit_now, now=now))
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    explicit_now = bool(getattr(a, "now", None))
    for i, o in picks:
        sim_at, lts = _meta_context(doc, orders, o, risk, now, market, explicit_now=explicit_now)
        text, ready = render(o, i, len(orders), cfg, risk, state, today, market=market, doc=doc,
                             simulated_at=sim_at, last_data_timestamp=lts,
                             clock_override=explicit_now, now=now)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    order = _pick(orders, a.index)
    market = _market_for_order(order, market, cfg)
    _ensure_indian_paper(market, cfg)
    _assert_tradable_instrument(order.get("symbol", ""), doc=order)
    doc = read_json(a.orders, "orders file", required=False)
    sim_at, lts = _meta_context(doc, orders, order, risk, now, market, explicit_now=bool(getattr(a, "now", None)))
    ready, label, rv = status_of(order, risk, state, today, cfg=cfg, market=market)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    identity_issue = _external_identity_issue(order, rv, market)
    if identity_issue:
        raise TicketError("order %d cannot be routed externally: %s" % (a.index, identity_issue))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order, rv)
    explicit_now = bool(getattr(a, "now", None))
    prov = signal_provenance(order, doc=doc, risk=risk, rv=rv, today=today,
                             simulated_at=sim_at, last_data_timestamp=lts,
                             clock_override=explicit_now, now=now, market=market)
    default_broker = "paper" if market == "IN" else "alpaca"
    rec = {"at": ny_iso(now, market=market), "date": today, "index": a.index,
           "fingerprint": fingerprint(order, cfg=cfg, market=market, risk_result=rv),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order, market=market, risk_result=rv), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", default_broker),
           "data_tier": prov["data_tier"],
           "is_historical_analysis": prov["is_historical_analysis"],
           "is_live_signal": prov["is_live_signal"],
           "provider": prov["provider"],
           "signal_classification": prov["signal_classification"]}
    if market == "IN":
        rec["market"] = "IN"
        rec["currency"] = "INR"
    if sim_at:
        rec["simulated_at"] = sim_at
    if lts:
        rec["last_data_timestamp"] = lts
    _apply_state_meta(state, sim_at, lts)
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, market=market, risk_result=rv)))
    if market == "IN":
        print("Simulated Indian paper approval recorded. No external order tool was called or prepared.")
        print("Record the simulated paper result with ticket.py result --index %d --status placed, then ticket.py fill with the simulated fill." % a.index)
    elif str(cfg.get("broker", "alpaca")).lower() == "ibkr":
        print("Now draft it with the Interactive Brokers connector; approve it in your IBKR account.")
    elif is_positional_gtt(order):
        print("Positional paper GTT OCO payload (stop-loss and target remain active across sessions):")
        print(json.dumps(tool_input(order), indent=2))
        print("Then record fill/status: ticket.py result --index %d --status placed|denied|failed" % a.index)
    else:
        print("Now call place_stock_order with exactly:")
        print(json.dumps(tool_input(order), indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    order = _pick(orders, a.index)
    market = _market_for_order(order, market, cfg)
    doc = read_json(a.orders, "orders file", required=False)
    sim_at, lts = _meta_context(doc, orders, order, risk, now, market, explicit_now=bool(getattr(a, "now", None)))
    risk_result, _, risk_fp = _risk_order_verdict(risk, order, today, cfg=cfg, market=market)
    rec = {"at": ny_iso(now, market=market), "date": today, "index": a.index,
           "fingerprint": risk_fp or fingerprint(order, cfg=cfg, market=market),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order, market=market), "status": "declined", "reply": a.reply or "no"}
    if sim_at:
        rec["simulated_at"] = sim_at
    if lts:
        rec["last_data_timestamp"] = lts
    _apply_state_meta(state, sim_at, lts)
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, market=market)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    order = _pick(orders, a.index)
    market = _market_for_order(order, market, cfg)
    doc = read_json(a.orders, "orders file", required=False)
    sim_at, lts = _meta_context(doc, orders, order, risk, now, market, explicit_now=bool(getattr(a, "now", None)))
    _ensure_indian_paper(market, cfg)
    rv, risk_why, fp = _risk_order_verdict(risk, order, today, cfg=cfg, market=market)
    if fp is None:
        raise TicketError("the order no longer matches its risk PASS: %s" % risk_why)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    has_fq = a.filled_qty is not None
    has_fp = a.filled_price is not None
    if has_fq != has_fp:
        raise TicketError("--filled-qty and --filled-price must be supplied together")
    if has_fq:
        if a.status != "placed":
            raise TicketError("--filled-qty/--filled-price only apply when --status is placed")
        fq = float(a.filled_qty)
        fp_price = float(a.filled_price)
        if fq <= 0 or fp_price <= 0:
            raise TicketError("a fill needs a positive --filled-qty and --filled-price")
        max_qty = _f(rec.get("qty"))
        if max_qty is not None and fq > max_qty + 1e-9:
            raise TicketError("filled qty %s exceeds approved order qty %s" % (_str_num(fq), _str_num(max_qty)))
    new = {"at": ny_iso(now, market=market), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop", "market", "currency", "paper", "broker",
              "simulated_at", "last_data_timestamp"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if sim_at and "simulated_at" not in new:
        new["simulated_at"] = sim_at
    if lts and "last_data_timestamp" not in new:
        new["last_data_timestamp"] = lts
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    if a.status == "placed" and has_fq:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today, market=market)
        _apply_state_meta(state, new.get("simulated_at"), new.get("last_data_timestamp"))
        state["orders"].append(new)
        write_json(a.state, state)
        log_fill(
            a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price,
            market=market, simulated_at=new.get("simulated_at"), last_data_timestamp=new.get("last_data_timestamp"),
        )
        print("Recorded order %d as %s." % (a.index, a.status))
        print("Added the fill to %s." % a.portfolio)
    else:
        _apply_state_meta(state, new.get("simulated_at"), new.get("last_data_timestamp"))
        state["orders"].append(new)
        write_json(a.state, state)
        print("Recorded order %d as %s." % (a.index, a.status))
        if a.status == "placed":
            print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
                  % (rec["symbol"], rec["side"]))
            if rec.get("kind") == "entry" and (is_bracket(order) or is_positional_gtt(order)):
                if is_positional_gtt(order):
                    print("GTT OCO stop-loss and target triggers remain armed across sessions; record any exit sell fill with ticket.py fill.")
                else:
                    print("When the stop-loss or the take-profit fills later, record that sell the same way.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    market = get_market_profile(a, cfg=cfg)
    _ensure_indian_paper(market, cfg)
    now, today = _clock(a, market=market)
    date = a.date or today
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-19")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, market=market)
    sim_at = ny_iso(now, market=market) if getattr(a, "now", None) else None
    pnl = pf.get("last_trade_pnl") if a.side == "sell" else None
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price, market=market, simulated_at=sim_at, pnl=pnl)
    msg = ("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, a.symbol.upper(),
                                                                  _money(a.price, market=market), date, a.portfolio,
                                                                  _money(pf["cash"], market=market)))
    if a.side == "sell" and pf.get("last_trade_pnl") is not None:
        pnl = pf["last_trade_pnl"]
        sign = "+" if pnl >= 0 else "-"
        msg += " Gross P&L %s%s (realized %s%s, before charges)." % (
            sign, _money(abs(pnl), market=market),
            "+" if pf["realized_pnl"] >= 0 else "-", _money(abs(pf["realized_pnl"]), market=market))
    print(msg)
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = a.symbol.strip().upper()
    if symbols_in:
        sym = symbols_in.normalize_symbol(sym)
    _assert_tradable_instrument(sym)
    plan = read_json(a.plan, "plan", required=False) or {}
    market = get_market_profile(a, cfg=cfg, risk=plan)
    if market == "US" and (sym.endswith((".NS", ".BO", "-EQ")) or plan.get("market") == "IN"):
        market = "IN"
    _ensure_indian_paper(market, cfg)
    now, today = _clock(a, market=market)
    plan_for_sym = plan if (isinstance(plan, dict) and str(plan.get("symbol", "")).upper() == sym) else {}
    lts = _check_no_lookahead([plan_for_sym] if plan_for_sym else [], now, today, market=market, strict=bool(getattr(a, "now", None)))
    sim_at = ny_iso(now, market=market) if (getattr(a, "now", None) or (plan_for_sym and plan_for_sym.get("simulated_at"))) else None
    source = "the paper portfolio" if market == "IN" else "the broker, get_open_position"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty wants the number of shares you hold, like --qty 8")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        positions = pf.get("positions")
        if positions is None:
            positions = []
        if not isinstance(positions, list):
            raise TicketError("invalid portfolio %s: 'positions' must be a list" % a.portfolio)
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in positions
                  if isinstance(p, dict) and str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            if market == "IN":
                raise TicketError("nothing to flatten: no %s shares in paper portfolio %s" % (sym, a.portfolio))
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_open_position "
                              "%s) and pass --qty if you do hold some" % (sym, a.portfolio, sym))
    default_flat = "15:20" if market == "IN" else "15:55"
    time_label = "IST" if market == "IN" else "New York"
    plan_for_sym = plan if (isinstance(plan, dict) and str(plan.get("symbol", "")).upper() == sym) else {}
    flat_by = plan_for_sym.get("flat_by") or cfg.get("flat_by") or default_flat
    is_positional_plan = (str(plan_for_sym.get("trading_style") or "").lower() == "positional"
                          or str(flat_by).lower() == "positional")
    if is_positional_plan:
        why_text = "closes the %s positional position at market (cancel any open GTT OCO triggers first; qty from %s)" % (sym, source)
    else:
        why_text = "flat by %s %s: closes the %s day trade before the close (qty from %s)" % (flat_by, time_label, sym, source)
    order = {"symbol": sym, "side": "sell", "qty": _str_num(qty), "type": "market", "time_in_force": "day",
             "why": why_text}
    order["market"] = market
    order["currency"] = "INR" if market == "IN" else "USD"
    order["asset_type"] = str(plan_for_sym.get("asset_type") or "EQUITY").upper()
    if sim_at:
        order["simulated_at"] = sim_at
    if lts:
        order["last_data_timestamp"] = lts
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    if market == "IN":
        doc["market"] = "IN"
    if sim_at:
        doc["simulated_at"] = sim_at
    if lts:
        doc["last_data_timestamp"] = lts
    write_json(a.orders, doc)
    if is_positional_plan:
        print("Close %s: %s (positional exit)." % (sym, plain(order, market=market)))
    else:
        print("Flatten %s: %s (flat by %s %s)." % (sym, plain(order, market=market), flat_by, time_label))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_open_position %s before you say yes." % (source, sym))
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if market == "IN":
        print("  3. This is a simulated paper close only. Update the paper portfolio with ticket.py result/fill.")
        print("     No Indian broker or external order tool is available from this workflow.")
    else:
        print("  3. If the bracket's stop-loss and take-profit are still open, Alpaca holds these shares for them")
        print("     and rejects a new sell. Cancel them first: get_orders (status open, nested true), then")
        print("     cancel_order_by_id for each open leg (Needs approval). Then place_stock_order with the ticket.")
    return 0


def cmd_log(a):
    cfg = read_json(getattr(a, "config", "trader/config.json"), "config", required=False) or {}
    market = get_market_profile(a, cfg=cfg)
    state = load_state(a.state)
    now, today = _clock(a, market=market)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        r_market = "IN" if r.get("market") == "IN" or r.get("currency") == "INR" or "\u20b9" in str(r.get("plain", "")) else market
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss"], market=r_market)) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="override the application clock to this market local time for backtests/tests, e.g. \"2026-10-19 10:15\" (marks signals non-live)")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in market local)")
        p.add_argument("--market", choices=["US", "IN"], help="Market (US or IN)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record a US order result or Indian simulated paper result")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in market local)")
    p.add_argument("--now")
    p.add_argument("--market", choices=["US", "IN"], help="Market (US or IN)")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_open_position (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
