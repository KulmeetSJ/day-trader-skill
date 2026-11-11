#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact JSON Claude will send to Alpaca's place_stock_order tool.
            A bracket order shows its stop-loss, take-profit and the max loss
            at the stop. An order is READY only if trader-risk passed THIS
            exact order today (matched by fingerprint) and nobody has decided
            on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
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
except ImportError:
    symbols_in = None
    calendar_in = None

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
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


def parse_now(text, market="US"):
    """ISO time -> naive UTC. A time without an offset is taken as market local time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        lbl = "IST" if market == "IN" else "New York"
        raise TicketError("--now wants a time like \"2026-10-19 10:15\" (%s) or ...Z (UTC)" % lbl)
    if dt.tzinfo is None:
        return local_to_utc(dt, market=market)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


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

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ALPACA_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
    """The exact arguments for place_stock_order: Alpaca's parameter names,
    numbers as strings (its signature types qty, notional and prices as str).
    "why" and "plan" are ours and are never sent."""
    out = {}
    for k in ALPACA_FIELDS:
        if k in order and order[k] is not None:
            out[k] = _str_num(order[k]) if k in STRING_FIELDS else order[k]
    return out


def _money(v, market="US"):
    f = float(v)
    s = "{:,.2f}".format(f)
    curr = "₹" if market == "IN" else "$"
    return curr + (s[:-3] if s.endswith(".00") else s)


def _p(v):
    return "%.2f" % float(v)


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def max_loss(order):
    """(dollars, stop) lost if the stop fills exactly at the stop, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return round(q * int(round(risk * 100)) / 100.0, 2), stop


def plain_parts(order, market=None):
    """The ticket in plain words, one part per line."""
    if market is None:
        market = order.get("market") or ("IN" if order.get("currency") == "INR" else "US")
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    typ = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    contracts = order.get("contracts")
    if is_bracket(order):
        how = "limit %s" % _p(order["limit_price"]) if typ == "limit" and order.get("limit_price") else typ
        if contracts:
            qty_desc = "%s (%s contract%s)" % (_str_num(order.get("qty")), contracts, "" if contracts == 1 else "s")
        else:
            qty_desc = _str_num(order.get("qty"))
        parts = ["%s %s %s, %s, %s" % (side, qty_desc, sym, how, tif)]
        if order.get("stop_loss_stop_price") is not None:
            leg = "stop-loss %s" % _p(order["stop_loss_stop_price"])
            if order.get("stop_loss_limit_price") is not None:
                leg += " (limit %s)" % _p(order["stop_loss_limit_price"])
            parts.append(leg)
        if order.get("take_profit_limit_price") is not None:
            parts.append("take-profit %s" % _p(order["take_profit_limit_price"]))
        loss, stop = max_loss(order)
        if loss is not None:
            parts.append("max loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                         % (_money(loss, market=market), _p(stop)))
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
        how = "limit order at %s" % _money(order.get("limit_price"), market=market)
    elif typ == "stop":
        how = "stop order at %s" % _money(order.get("stop_price"), market=market)
    elif typ == "stop_limit":
        how = "stop %s, limit %s" % (_money(order.get("stop_price"), market=market), _money(order.get("limit_price"), market=market))
    else:
        how = "%s order" % typ.replace("_", " ")
    return ["%s %s, %s, %s" % (side, what, how, tif)]


def plain(order, market=None):
    """BUY $1,250 of AAPL, market order, good for today
    BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about $46.88 ..."""
    return " / ".join(plain_parts(order, market=market))


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
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the place_stock_order call" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today, market="US"):
    ready, label, rv = status_of(order, risk, state, today)
    broker = str(cfg.get("broker", "alpaca")).lower()
    paper = cfg.get("paper", True)
    acct = "PAPER account" if paper else "LIVE ACCOUNT, REAL MONEY"
    broker_name = "Interactive Brokers" if broker == "ibkr" else ("Paper Broker (NSE/BSE)" if market == "IN" else "Alpaca")
    lines = ["ORDER %d of %d, %s, %s" % (idx, total, acct, broker_name)]
    for part in plain_parts(order, market=market):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if broker == "ibkr":
        lines.append("  Claude drafts this with the Interactive Brokers connector. IBKR sends it to your")
        lines.append("  IBKR account, and it only goes to the market after you approve it there.")
    else:
        lines.append("  Claude will call place_stock_order with exactly:")
        for l in json.dumps(tool_input(order), indent=2).splitlines():
            lines.append("    " + l)
        if is_bracket(order):
            lines.append("  Alpaca places the entry, then the stop-loss and the take-profit as two orders")
            lines.append("  that cancel each other: when one fills, the other is canceled.")
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if broker != "ibkr":
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, market="US"):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order)
    res = {"index": idx, "ready": ready, "status": label, "plain": plain(order, market=market),
           "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
           "broker": cfg.get("broker", "alpaca"), "tool": "place_stock_order",
           "tool_input": tool_input(order), "why": order.get("why", ""),
           "bracket": is_bracket(order), "max_loss": loss, "stop": stop}
    if order.get("asset_type"):
        res["asset_type"] = order["asset_type"]
    if order.get("contracts"):
        res["contracts"] = order["contracts"]
    if order.get("lot_size"):
        res["lot_size"] = order["lot_size"]
    if order.get("transaction_costs"):
        res["transaction_costs"] = order["transaction_costs"]
    return res


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, market=None):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell)."""
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
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
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


def log_fill(state_path, now, today, symbol, side, qty, price, market="US"):
    """A fill in the log, so trader-risk can count day trades (a buy and a sell the same day)."""
    state = load_state(state_path)
    state.setdefault("orders", []).append({
        "at": ny_iso(now, market=market), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
        "qty": _str_num(float(qty)), "price": float(price),
        "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price))})
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a, market="US"):
    now = parse_now(getattr(a, "now", None), market=market)
    today = getattr(a, "today", None) or to_local(now, market=market).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-19")
    return now, today


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
    return orders, cfg, risk, state, today, now, market


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today, market=market) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, market=market)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    rec = {"at": ny_iso(now, market=market), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order, market=market), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "alpaca")}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, market=market)))
    if str(cfg.get("broker", "alpaca")).lower() == "ibkr":
        print("Now draft it with the Interactive Brokers connector; approve it in your IBKR account.")
    else:
        print("Now call place_stock_order with exactly:")
        print(json.dumps(tool_input(order), indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ny_iso(now, market=market), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order, market=market), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, market=market)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now, market = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
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
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    if a.status == "placed" and has_fq:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today, market=market)
        state["orders"].append(new)
        write_json(a.state, state)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, market=market)
        print("Recorded order %d as %s." % (a.index, a.status))
        print("Added the fill to %s." % a.portfolio)
    else:
        state["orders"].append(new)
        write_json(a.state, state)
        print("Recorded order %d as %s." % (a.index, a.status))
        if a.status == "placed":
            print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
                  % (rec["symbol"], rec["side"]))
            if rec.get("kind") == "entry" and is_bracket(order):
                print("When the stop-loss or the take-profit fills later, record that sell the same way.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    market = get_market_profile(a, cfg=cfg)
    now, today = _clock(a, market=market)
    date = a.date or today
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-19")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, market=market)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price, market=market)
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
    plan = read_json(a.plan, "plan", required=False) or {}
    market = get_market_profile(a, cfg=cfg, risk=plan)
    if market == "US" and (sym.endswith((".NS", ".BO", "-EQ")) or plan.get("market") == "IN"):
        market = "IN"
    now, today = _clock(a, market=market)
    source = "the broker, get_open_position"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty wants the number of shares you hold, like --qty 8")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_open_position "
                              "%s) and pass --qty if you do hold some" % (sym, a.portfolio, sym))
    default_flat = "15:20" if market == "IN" else "15:55"
    time_label = "IST" if market == "IN" else "New York"
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or default_flat
    why_text = "flat by %s %s: closes the %s day trade before the close (qty from %s)" % (flat_by, time_label, sym, source)
    order = {"symbol": sym, "side": "sell", "qty": _str_num(qty), "type": "market", "time_in_force": "day",
             "why": why_text}
    if market == "IN":
        order["market"] = "IN"
        order["currency"] = "INR"
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    if market == "IN":
        doc["market"] = "IN"
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s %s)." % (sym, plain(order, market=market), flat_by, time_label))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_open_position %s before you say yes." % (source, sym))
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
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
        r_market = "IN" if r.get("market") == "IN" or "₹" in str(r.get("plain", "")) else market
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
        p.add_argument("--now", help="pretend it is this market local time, e.g. \"2026-10-19 10:15\"")
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
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card")
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
