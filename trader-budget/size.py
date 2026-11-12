#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Default mode: sizes ONE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes one
bracket order (entry, stop-loss, take-profit) to trader/orders.json in the
shape of Alpaca's place_stock_order (or Indian paper broker bracket order). It
never places anything: trader-risk checks the order and trader-broker asks you
yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 364.00 --write           # a limit order at 364.00
    python3 size.py --now "2026-10-19 10:15" --json  # pin the clock (market time)

How many shares. Whole shares only (integer lot multiples for F&O):

    shares = floor of the smallest of
      max loss       max_loss_per_trade (_inr or _usd) / risk per share (entry minus stop)
      position cap   budget x max_position_pct / entry
      max order      max_order (_inr or _usd) / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

Weights mode (kept from v1, no longer the headline): split a budget across a
list of stocks by weight.

    python3 size.py --weights AAPL=22.04,AXP=17.14,GOOGL=12.62,KO=10.86,BAC=9.20
    python3 size.py --targets my-weights.json --budget 5000 --write
    python3 size.py --weights ... --whole-shares --prices trader/prices.json

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order (_inr or _usd) is dropped and its
                   weight shared out, so you never place a tiny order
  5. rounding      whole currency units (dollars or rupees) by default (--cents
                   for cents/paise); the rounding unit goes to the biggest
                   position that is not capped
  6. whole shares  with --whole-shares or "fractional": false; needs prices

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
import json
import math
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
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

# v2 defaults: the contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_usd": 4000,
    "min_order_usd": 5,
    "max_loss_per_trade_usd": 50,
    "max_daily_loss_usd": 150,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "fractional": False,
    "broker": "alpaca",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_futures": False,
    "futures_margin_pct": 20.0,
    "allow_margin": False,
    "allow_short": False,
    "pdt_guard": False,
}

PROVENANCE_KEYS = (
    "is_market_open", "is_live_data", "is_historical_analysis", "is_live_signal",
    "is_fixture", "data_tier", "last_trading_session", "last_data_timestamp",
    "data_status", "is_simulated", "provider", "signal_freshness",
    "simulated_at", "clock_override", "candle_session",
)


REGULATORY_SCHEDULES = {
    # Key is effective start date (YYYY-MM-DD)
    "2026-04-01": {
        "description": "Finance Act 2026 (STT revision) & NSE Circular 2026-03-01 (Txn charge revision)",
        "stt": {
            "equity_intraday_sell": 0.00025,       # 0.025% on sell
            "equity_delivery_buy": 0.001,           # 0.1% on buy
            "equity_delivery_sell": 0.001,          # 0.1% on sell
            "futures_sell": 0.0005,                 # 0.05% on sell (increased from 0.02%)
            "options_sell": 0.0015,                 # 0.15% on sell premium (increased from 0.1%)
            "options_exercised": 0.0015,            # 0.15% on intrinsic value
        },
        "nse_txn": {
            "cash": 307.0 / 1e7,                    # Rs 307 / crore per side = 0.0000307 (incl IPFT)
            "futures": 183.0 / 1e7,                 # Rs 183 / crore per side = 0.0000183 (incl IPFT)
            "options": 3553.0 / 1e7,                # Rs 3,553 / crore per side = 0.0003553 (incl IPFT)
        },
        "sebi": 10.0 / 1e7,                         # Rs 10 / crore per side = 0.000001
        "stamp_duty_buy": {
            "equity_intraday": 0.00003,             # 0.003%
            "equity_delivery": 0.00015,             # 0.015%
            "futures": 0.00002,                     # 0.002%
            "options": 0.00003,                     # 0.003%
        },
        "gst_rate": 0.18,
    },
    "2024-10-01": {
        "description": "Finance Act 2024 STT revision",
        "stt": {
            "equity_intraday_sell": 0.00025,
            "equity_delivery_buy": 0.001,
            "equity_delivery_sell": 0.001,
            "futures_sell": 0.0002,                 # 0.02% on sell
            "options_sell": 0.001,                  # 0.1% on sell premium
            "options_exercised": 0.00125,           # 0.125% on intrinsic value
        },
        "nse_txn": {
            "cash": 297.0 / 1e7,
            "futures": 173.0 / 1e7,
            "options": 3503.0 / 1e7,
        },
        "sebi": 10.0 / 1e7,
        "stamp_duty_buy": {
            "equity_intraday": 0.00003,
            "equity_delivery": 0.00015,
            "futures": 0.00002,
            "options": 0.00003,
        },
        "gst_rate": 0.18,
    },
}


def get_regulatory_schedule(as_of_date=None):
    """Retrieve regulatory transaction charge schedule for a given date."""
    if as_of_date is None:
        eff_keys = sorted(REGULATORY_SCHEDULES.keys(), reverse=True)
        return eff_keys[0], REGULATORY_SCHEDULES[eff_keys[0]]
    if isinstance(as_of_date, (datetime.datetime, datetime.date)):
        dt_str = as_of_date.strftime("%Y-%m-%d")
    else:
        dt_str = str(as_of_date).strip()[:10]
    for k in sorted(REGULATORY_SCHEDULES.keys(), reverse=True):
        if dt_str >= k:
            return k, REGULATORY_SCHEDULES[k]
    earliest_key = sorted(REGULATORY_SCHEDULES.keys())[0]
    return earliest_key, REGULATORY_SCHEDULES[earliest_key]


def calculate_indian_transaction_charges(
    asset_type="EQUITY",
    side="buy",
    entry_price=0.0,
    exit_price=0.0,
    qty=0,
    trading_style="intraday",
    as_of_date=None,
    exercised=False,
    intrinsic_value=None,
    spot_price=None,
    strike=None,
    option_type="CE",
):
    """Calculate statutory transaction charges and brokerage for Indian markets (NSE/BSE).
    
    Includes effective-date-based schedules (1 April 2026 STT rates and 1 March 2026 NSE rates).
    
    Statutory Levies:
    - STT (Securities Transaction Tax):
        Equity Intraday: 0.025% on sell turnover
        Equity Positional/Delivery: 0.1% on buy and sell turnover
        Futures: 0.05% on sell turnover (effective 2026-04-01)
        Options: 0.15% on sell premium turnover (effective 2026-04-01)
        Exercised Options: 0.15% on intrinsic value
    - Exchange Transaction Charges (NSE revised effective 2026-03-01, incl. IPFT):
        Cash: Rs 307 per crore per side (0.00307%)
        Futures: Rs 183 per crore per side (0.00183%)
        Options: Rs 3,553 per crore of premium per side (0.03553%)
    - SEBI Turnover Charges: Rs 10 per crore (0.0001% of turnover)
    - Stamp Duty (Buy side only):
        Equity Intraday: 0.003%
        Equity Positional: 0.015%
        Futures: 0.002%
        Options: 0.003%
    - GST: 18% on (Exchange Charges + SEBI Charges + Brokerage)
    
    Brokerage Estimate (Segregated from statutory levies):
    - Discount broker model: min(Rs 20, 0.03%) per order for intraday/futures, Rs 20 flat per order for options, Rs 40 per buy/sell order (Rs 80 round-trip) for positional/delivery equity.
    """
    entry_val = float(entry_price or 0.0) * float(qty or 0)
    exit_val = float(exit_price or entry_price or 0.0) * float(qty or 0)
    turnover = entry_val + exit_val
    asset = str(asset_type or "EQUITY").upper()
    style = str(trading_style or "intraday").lower()
    eff_date, schedule = get_regulatory_schedule(as_of_date)

    if turnover <= 0:
        empty_statutory = {
            "stt": 0.0, "exchange_charges": 0.0, "sebi_charges": 0.0,
            "stamp_duty": 0.0, "gst": 0.0, "total": 0.0,
            "exchange": "NSE", "effective_date": eff_date,
            "rates_source": "Statutory levies: STT, NSE Txn (incl IPFT), Stamp Duty, SEBI",
        }
        empty_brokerage = {
            "brokerage": 0.0, "gst": 0.0, "total": 0.0, "is_estimate": True,
            "model": "Standard discount broker model",
            "disclaimer": "Brokerage is an estimate and varies by broker. Not an official exchange charge.",
        }
        empty_pnl = {
            "gross_pnl": 0.0, "statutory_charges": 0.0, "brokerage_charges": 0.0,
            "total_charges": 0.0, "net_pnl": 0.0, "breakeven_exit_price": 0.0, "profit_margin_pct": 0.0,
        }
        return {
            "brokerage": 0.0, "stt": 0.0, "exchange_charges": 0.0,
            "sebi_charges": 0.0, "stamp_duty": 0.0, "gst": 0.0, "total": 0.0,
            "buy_value": 0.0, "sell_value": 0.0, "turnover": 0.0, "is_estimate": True,
            "effective_date": eff_date,
            "statutory": empty_statutory,
            "brokerage_estimate": empty_brokerage,
            "profitability_impact": empty_pnl,
        }

    # Brokerage (Estimated tariff, not official exchange charge)
    if asset == "OPTIONS":
        brokerage = 40.0  # Rs 20 on buy + Rs 20 on sell
    elif style == "positional" and asset == "EQUITY":
        brokerage = 80.0  # Rs 40 on buy + Rs 40 on sell (positional/delivery equity)
    else:
        brokerage = min(20.0, round(0.0003 * entry_val, 2)) + min(20.0, round(0.0003 * exit_val, 2))

    # STT (Securities Transaction Tax)
    if exercised:
        if intrinsic_value is not None:
            iv_unit = max(0.0, float(intrinsic_value))
        elif spot_price is not None and strike is not None:
            if str(option_type).upper() == "PE":
                iv_unit = max(0.0, float(strike) - float(spot_price))
            else:
                iv_unit = max(0.0, float(spot_price) - float(strike))
        else:
            iv_unit = max(0.0, float(exit_price or 0.0))
        tot_iv = iv_unit * float(qty or 0)
        stt = round(schedule["stt"]["options_exercised"] * tot_iv, 2)
    elif asset == "OPTIONS":
        stt = round(schedule["stt"]["options_sell"] * exit_val, 2)
    elif asset == "FUTURES":
        stt = round(schedule["stt"]["futures_sell"] * exit_val, 2)
    elif style == "positional":
        stt_buy = round(schedule["stt"]["equity_delivery_buy"] * entry_val, 2)
        stt_sell = round(schedule["stt"]["equity_delivery_sell"] * exit_val, 2)
        stt = round(stt_buy + stt_sell, 2)
    else:
        stt = round(schedule["stt"]["equity_intraday_sell"] * exit_val, 2)

    # Exchange charges (NSE circular effective 2026-03-01, incl IPFT)
    if asset == "OPTIONS":
        exch = round(schedule["nse_txn"]["options"] * turnover, 2)
    elif asset == "FUTURES":
        exch = round(schedule["nse_txn"]["futures"] * turnover, 2)
    else:
        exch = round(schedule["nse_txn"]["cash"] * turnover, 2)

    # SEBI turnover charges: Rs 10 / crore (0.0001% of turnover)
    sebi = round(schedule["sebi"] * turnover, 2)

    # Stamp duty: buy side only
    if asset == "OPTIONS":
        stamp = round(schedule["stamp_duty_buy"]["options"] * entry_val, 2)
    elif asset == "FUTURES":
        stamp = round(schedule["stamp_duty_buy"]["futures"] * entry_val, 2)
    elif style == "positional":
        stamp = round(schedule["stamp_duty_buy"]["equity_delivery"] * entry_val, 2)
    else:
        stamp = round(schedule["stamp_duty_buy"]["equity_intraday"] * entry_val, 2)

    # GST: 18% on services
    gst_statutory = round(schedule["gst_rate"] * (exch + sebi), 2)
    gst_brokerage = round(schedule["gst_rate"] * brokerage, 2)
    gst = round(schedule["gst_rate"] * (brokerage + exch + sebi), 2)

    statutory_total = round(stt + exch + sebi + stamp + gst_statutory, 2)
    brokerage_total = round(brokerage + gst_brokerage, 2)
    total_charges = round(brokerage + stt + exch + sebi + stamp + gst, 2)

    gross_pnl = round(exit_val - entry_val, 2)
    net_pnl = round(gross_pnl - total_charges, 2)
    breakeven_exit_price = round((entry_val + total_charges) / qty, 2) if qty > 0 else 0.0
    profit_margin_pct = round((net_pnl / entry_val) * 100, 2) if entry_val > 0 else 0.0

    statutory = {
        "stt": stt,
        "exchange_charges": exch,
        "sebi_charges": sebi,
        "stamp_duty": stamp,
        "gst": gst_statutory,
        "total": statutory_total,
        "exchange": "NSE",
        "effective_date": eff_date,
        "rates_source": "Statutory levies: STT (Finance Act), NSE Txn (Circular 2026), Stamp Duty, SEBI",
    }

    if style == "positional" and asset == "EQUITY":
        brokerage_model = "Positional/Delivery equity tariff (Rs 40 per buy/sell order)"
    elif asset == "OPTIONS":
        brokerage_model = "Standard discount broker model (Rs 20 flat per order)"
    else:
        brokerage_model = "Standard discount broker model (Rs 20 flat / max 0.03%)"

    brokerage_estimate = {
        "brokerage": brokerage,
        "gst": gst_brokerage,
        "total": brokerage_total,
        "is_estimate": True,
        "model": brokerage_model,
        "disclaimer": "Brokerage is an estimate and varies by broker. Not an official exchange charge.",
    }

    profitability_impact = {
        "gross_pnl": gross_pnl,
        "statutory_charges": statutory_total,
        "brokerage_charges": brokerage_total,
        "total_charges": total_charges,
        "net_pnl": net_pnl,
        "breakeven_exit_price": breakeven_exit_price,
        "profit_margin_pct": profit_margin_pct,
    }

    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_charges": exch,
        "sebi_charges": sebi,
        "stamp_duty": stamp,
        "gst": gst,
        "total": total_charges,
        "buy_value": round(entry_val, 2),
        "sell_value": round(exit_val, 2),
        "turnover": round(turnover, 2),
        "is_estimate": True,
        "effective_date": eff_date,
        "statutory": statutory,
        "brokerage_estimate": brokerage_estimate,
        "profitability_impact": profitability_impact,
    }
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


class SizeError(Exception):
    pass


def get_market_profile(a=None, plan=None, cfg=None):
    if a and getattr(a, "market", None):
        return a.market.upper()
    env = os.environ.get("BHARAT_MARKET")
    if env and env.upper() in ("IN", "INDIA"):
        return "IN"
    if plan and isinstance(plan, dict) and plan.get("market"):
        return str(plan["market"]).upper()
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
    sym = (plan or {}).get("symbol", "") if isinstance(plan, dict) else ""
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
    """ISO time -> naive UTC. A time without an offset is taken as local market time."""
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
        raise SizeError("--now wants a time like \"2026-10-19 10:15\" (%s) or ...Z (UTC)" % lbl)
    if dt.tzinfo is None:
        return local_to_utc(dt, market=market)
    return (dt - (dt.utcoffset() or datetime.timedelta(0))).replace(tzinfo=None)


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
    """"2026-10-19T14:52:13Z" or "2026-10-19 15:20:00 IST" -> naive UTC, or None."""
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


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": str(r["ticker"]).strip().upper(), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """AAPL=22.04,AXP=17.14 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants TICKER=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[str(t).upper()] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("timestamp", "last_data_timestamp", "time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("last_data_timestamp")) or parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES, allow_clock_skew=True):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    min_age = -60 if allow_clock_skew else 0
    return p, t, min_age <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def _cfg_money(raw_cfg, merged_cfg, base, market, default=None):
    raw = raw_cfg or {}
    keys = (base + "_inr", base, base + "_usd") if market == "IN" else (base + "_usd", base)
    for k in keys:
        if k in raw and raw.get(k) is not None:
            return raw.get(k)
    val = (merged_cfg or {}).get(base + "_usd")
    return val if val is not None else default


def _parse_tick(val, allow_none=False, context="tick_size"):
    """Validate and return positive finite float tick size, or None if allowed."""
    if val is None or (isinstance(val, str) and not val.strip()):
        if allow_none:
            return None
        raise SizeError("Missing tick_size in %s" % context)
    if isinstance(val, bool):
        raise SizeError("Invalid boolean tick_size %r in %s" % (val, context))
    try:
        dec = Decimal(str(val).strip())
    except (InvalidOperation, ValueError):
        raise SizeError("Invalid non-numeric tick_size %r in %s" % (val, context))
    if not dec.is_finite() or dec <= 0:
        raise SizeError("Invalid non-positive tick_size %r in %s" % (val, context))
    return float(dec)


def _tick_scale(tick=0.01):
    try:
        d = Decimal(str(tick))
        return max(2, -d.as_tuple().exponent)
    except Exception:
        return 2


def round_tick(x, tick=0.01):
    """Round a price to exact multiples of tick (half up)."""
    d_x = Decimal(str(float(x)))
    d_tick = Decimal(str(tick))
    q_fmt = Decimal("1").scaleb(-_tick_scale(tick))
    val = ((d_x / d_tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * d_tick).quantize(
        q_fmt, rounding=ROUND_HALF_UP
    )
    return float(val) + 0.0


def floor_tick(x, tick=0.01):
    """Floor a price down to exact multiples of tick (protective for stops)."""
    d_x = Decimal(str(float(x)))
    d_tick = Decimal(str(tick))
    q_fmt = Decimal("1").scaleb(-_tick_scale(tick))
    val = ((d_x / d_tick).quantize(Decimal("1"), rounding=ROUND_FLOOR) * d_tick).quantize(
        q_fmt, rounding=ROUND_HALF_UP
    )
    return float(val) + 0.0


def round_price(x):
    """Round a price to 0.01 sub-units (cent or paise, half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


round_cent = round_price


def shares_for(amount, per_share, lot_size=1):
    """Whole shares that fit in amount at per_share, in exact sub-units aligned to lot_size."""
    scale = Decimal("10000")
    u = int((Decimal(str(round(float(amount), 4))) * scale).to_integral_value(rounding=ROUND_HALF_UP))
    p = int((Decimal(str(round(float(per_share), 4))) * scale).to_integral_value(rounding=ROUND_HALF_UP))
    if p <= 0 or u <= 0:
        return 0
    raw = u // p
    if lot_size and lot_size > 1:
        return (raw // lot_size) * lot_size
    return raw


def _price(x, tick=0.01):
    """358.32 -> "358.32": format price cleanly to tick precision."""
    scale = _tick_scale(tick)
    return "%.*f" % (scale, round_tick(x, tick))


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13" (Alpaca wants these as strings)."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def money(x, market="US"):
    x = float(x)
    sym = "\u20b9" if market == "IN" else "$"
    if abs(x - round(x)) < 0.005:
        return sym + "{:,.0f}".format(round(x))
    return sym + "{:,.2f}".format(x)


# ---------------------------------------------------------------- plan mode (the default)

PAPER_MULTI_SESSION_BROKERS = ("paper", "paper_in", "simulated", "mock")


def validate_positional_support(cfg, market="IN", asset_type="EQUITY", stop=None, target=None):
    """Validate that configured paper broker/schema safely supports cross-session protection.

    Raises SizeError when the broker/schema cannot represent multi-session stop-loss and
    take-profit protection rather than generating a misleading intraday day/bracket order.
    """
    raw = cfg or {}
    eff_market = str(market or "US").upper()
    if eff_market != "IN":
        raise SizeError(
            "positional multi-session protection is currently supported only in Indian paper mode "
            "(market='IN', broker='paper'); %s %r broker schema cannot safely represent cross-session GTT OCO"
            % (eff_market, raw.get("broker", "alpaca"))
        )
    if not bool(raw.get("paper", True)):
        raise SizeError(
            "positional multi-session GTT OCO orders are supported in paper mode only (paper=true); "
            "refusing to generate a live positional order"
        )
    if raw.get("broker_supports_multi_session") is False or raw.get("supports_multi_session_protection") is False:
        raise SizeError(
            "configured broker/schema disables multi-session protection "
            "(broker_supports_multi_session=false); cannot generate a misleading positional order"
        )
    broker = str(raw.get("broker") or "paper").strip().lower()
    if broker not in PAPER_MULTI_SESSION_BROKERS:
        raise SizeError(
            "broker %r cannot safely represent Indian positional multi-session GTT OCO protection; "
            "use broker='paper' with paper=true" % broker
        )
    if stop is None or target is None or float(stop) <= 0 or float(target) <= 0:
        raise SizeError(
            "positional orders require valid stop-loss and take-profit levels to arm multi-session GTT OCO protection"
        )
    product = "CNC" if str(asset_type or "EQUITY").upper() == "EQUITY" else "NRML"
    return {
        "broker": broker,
        "paper": True,
        "product": product,
        "order_class": "gtt_oco",
        "time_in_force": "gtc",
    }


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None, market=None, instruments_path=None, trading_style=None, exchange=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  session date (YYYY-MM-DD); a plan for another day is refused"""
    raw_cfg = cfg or {}
    c = dict(DEFAULTS)
    c.update(raw_cfg)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    raw_sym = str(plan.get("symbol") or "").strip().upper()
    sym = raw_sym
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    if market is None:
        market = get_market_profile(plan=plan, cfg=c)
    req_exchange = exchange or plan.get("exchange") or c.get("exchange")
    if not req_exchange:
        if raw_sym.endswith(".BO") or raw_sym.startswith("BSE:"):
            req_exchange = "BSE"
        elif raw_sym.endswith(".NS") or raw_sym.startswith("NSE:"):
            req_exchange = "NSE"
    if req_exchange:
        req_exchange = str(req_exchange).strip().upper()
    if market == "IN" and symbols_in:
        try:
            sym = symbols_in.normalize_symbol(sym)
        except Exception:
            pass
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    
    asset_type = plan.get("asset_type")
    if not asset_type:
        if plan.get("option_contract"):
            asset_type = "OPTIONS"
        elif symbols_in and symbols_in.is_derivative_symbol(sym):
            deriv_meta = symbols_in.parse_derivative_symbol(sym)
            if deriv_meta:
                asset_type = "OPTIONS" if deriv_meta.get("instrument_type") == "OPT" else "FUTURES"
        elif market == "IN" and symbols_in and symbols_in.is_index_symbol(sym):
            asset_type = "INDEX"
        else:
            asset_type = "EQUITY"

    if str(asset_type).upper() == "INDEX" or (
        market == "IN" and symbols_in and symbols_in.is_index_symbol(sym)
        and not symbols_in.is_derivative_symbol(sym) and not plan.get("option_contract")
        and asset_type not in ("OPTIONS", "FUTURES")
    ):
        raise SizeError(
            "%s is a benchmark spot index (analytical only, not a directly tradable equity); "
            "size an index F&O derivative contract instead" % sym
        )

    if asset_type == "OPTIONS":
        if not c.get("allow_options"):
            raise SizeError("options are not enabled: set allow_options: true in trader/config.json")
    elif asset_type == "FUTURES":
        if c.get("allow_futures") is False:
            raise SizeError("futures are not enabled: set allow_futures: true in trader/config.json")

    if today and plan.get("date") and str(plan["date"]) != today:
        is_allowed_off_hours = False
        if market == "IN" and calendar_in:
            try:
                last_td = calendar_in.get_last_trading_day(today)
                is_trading, _, _ = calendar_in.is_trading_day_in(today)
                if (not is_trading and str(plan["date"]) == str(last_td)) or plan.get("is_historical_analysis") or plan.get("trading_style") == "positional":
                    is_allowed_off_hours = True
            except Exception:
                pass
        if not is_allowed_off_hours:
            tz_lbl = "IST" if market == "IN" else "New York"
            raise SizeError("the plan is for %s and today is %s in %s: make a new plan with trader-exit"
                            % (plan["date"], today, tz_lbl))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a bracket order needs entry, "
                            "stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    # Indian instrument metadata: load cache first so tick size drives price normalization
    inst_cache = None
    inst = None
    inst_tick = None
    resolved_exchange = req_exchange or ("NSE" if market == "IN" else None)
    explicit_inst = instruments_path is not None
    if market == "IN" and instruments:
        inst_path = instruments_path or getattr(instruments, "DEFAULT_CACHE_PATH", None)
        if explicit_inst and (not inst_path or not os.path.isfile(inst_path)):
            raise SizeError("Instrument cache not found at: %s" % inst_path)
        if inst_path and os.path.isfile(inst_path):
            try:
                inst_cache = instruments.load_instrument_cache(inst_path, max_age_days=30, as_of_date=today)
            except Exception as exc:
                raise SizeError("Failed to load authoritative instrument cache (%s): %s" % (inst_path, exc))
        if inst_cache is not None:
            instr_err_cls = getattr(instruments, "InstrumentMetadataError", Exception)
            try:
                inst = instruments.resolve_instrument(
                    raw_sym, cache_payload=inst_cache, require_intraday=False, exchange=req_exchange
                )
            except instr_err_cls as exc:
                raise SizeError(str(exc))
            if inst:
                inst_tick = _parse_tick(inst.get("tick_size"), allow_none=False, context="instrument master (%s)" % sym)
                if inst.get("exchange"):
                    resolved_exchange = str(inst["exchange"]).strip().upper()

    plan_tick = None
    if "tick_size" in plan:
        plan_tick = _parse_tick(plan.get("tick_size"), allow_none=False, context="plan (%s)" % sym)
    if inst_tick is not None and plan_tick is not None and Decimal(str(inst_tick)) != Decimal(str(plan_tick)):
        raise SizeError(
            "Plan tick_size %s disagrees with instrument master tick_size %s (%s) for %s"
            % (plan_tick, inst_tick, resolved_exchange, sym)
        )
    if inst_tick is not None:
        tick_size = inst_tick
    elif plan_tick is not None:
        tick_size = plan_tick
    elif market == "IN":
        raise SizeError(
            "Cannot authoritatively resolve tick_size for %s: provide a valid instrument cache or plan tick_size"
            % sym
        )
    else:
        tick_size = 0.01

    if limit is not None:
        ref, ref_from = round_tick(limit, tick_size), "limit"
    elif price is not None and round_tick(price, tick_size) > round_tick(entry_plan, tick_size):
        ref, ref_from = round_tick(price, tick_size), "price"
    else:
        ref, ref_from = round_tick(entry_plan, tick_size), "plan"
    stop, target = floor_tick(stop, tick_size), round_tick(target, tick_size)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop, tick_size), _price(ref, tick_size)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target, tick_size), _price(ref, tick_size)))
    risk = float(Decimal(str(ref)) - Decimal(str(stop)))
    reward = float(Decimal(str(target)) - Decimal(str(ref)))

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget, market=market), money(cash_buffer, market=market))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash, market=market), money(cash_buffer, market=market))
    available = max(round(available, 2), 0.0)

    lot_size = int(plan.get("lot_size") or 1)
    if asset_type == "EQUITY" and not (symbols_in and symbols_in.is_derivative_symbol(sym)):
        # Cash equity trades in shares (lot_size=1). F&O contract lot sizes must not apply to cash equity.
        lot_size = int(plan.get("equity_lot_size") or 1)
    elif lot_size <= 1 and (asset_type in ("OPTIONS", "FUTURES") or (symbols_in and symbols_in.is_derivative_symbol(sym))):
        if plan.get("option_contract") and plan["option_contract"].get("lot_size"):
            lot_size = int(plan["option_contract"]["lot_size"])
        elif market == "IN" and instruments:
            fo_lot = instruments.get_fo_lot_size(sym, cache_payload=inst_cache)
            if fo_lot:
                lot_size = int(fo_lot)
    if asset_type in ("OPTIONS", "FUTURES") and lot_size <= 1:
        raise SizeError("Cannot authoritatively resolve F&O lot size for derivative %r: missing or incomplete instrument metadata" % sym)
    if lot_size <= 0:
        lot_size = 1

    is_t2t = bool(plan.get("is_trade_to_trade"))
    series = plan.get("series", "")
    if not is_t2t and market == "IN" and instruments:
        if inst is None and isinstance(inst_cache, dict):
            try:
                canon_sym = symbols_in.normalize_symbol(sym) if symbols_in else sym
            except Exception:
                canon_sym = sym
            inst = (inst_cache.get("instruments") or {}).get(canon_sym)
        if inst:
            if inst.get("is_trade_to_trade"):
                is_t2t = True
            if not series and inst.get("series"):
                series = inst["series"]
    if is_t2t or (series and series.upper() in ("BE", "BZ", "T")):
        is_t2t = True

    def _limit_row(name, amt, per, shares, how):
        row = {"name": name, "amount": amt, "per": per, "shares": shares, "how": how}
        if market != "IN":
            row["usd"] = amt
        return row

    limits = []
    max_loss = _f(_cfg_money(raw_cfg, c, "max_loss_per_trade", market))
    if max_loss:
        limits.append(_limit_row("max loss", max_loss, risk, shares_for(max_loss, risk, lot_size=lot_size),
                                 "%s max loss / %s risk a share" % (money(max_loss, market=market), _price(risk, tick_size))))
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append(_limit_row("position cap", cap, ref, shares_for(cap, ref, lot_size=lot_size),
                                 "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget, market=market), money(cap, market=market), _price(ref, tick_size))))
    max_order = _f(_cfg_money(raw_cfg, c, "max_order", market))
    if max_order:
        limits.append(_limit_row("max order", max_order, ref, shares_for(max_order, ref, lot_size=lot_size),
                                 "%s / %s" % (money(max_order, market=market), _price(ref, tick_size))))
    limits.append(_limit_row("cash", available, ref, shares_for(available, ref, lot_size=lot_size),
                             "%s = %s / %s" % (cash_note, money(available, market=market), _price(ref, tick_size))))

    min_order_raw = _cfg_money(raw_cfg, c, "min_order", market, default=5)
    min_order = _f(min_order_raw) if min_order_raw is not None else 0
    if min_order is None:
        min_order = 0

    if is_t2t:
        qty = 0
        bound = ["trade-to-trade"]
        for l in limits:
            l["binds"] = False
        refusal = ("Trade-to-Trade: intraday day trading is prohibited on %s (SEBI rules: series %s requires gross delivery settlement)"
                   % (sym, series or "T2T"))
    else:
        qty = max(0, min(l["shares"] for l in limits))
        bound = [l["name"] for l in limits if l["shares"] == qty]
        for l in limits:
            l["binds"] = l["name"] == bound[0]
        refusal = None

    cost = round(qty * ref, 2)
    loss = round(qty * risk, 2)

    if not is_t2t:
        min_rr = _f(c.get("min_reward_risk"))
        if qty == 0:
            if bound[0] == "max loss" and max_loss is not None:
                if lot_size > 1:
                    one_lot_risk = risk * lot_size
                    refusal = ("the risk of one lot of %s (%d shares) is %s, more than your %s max loss per trade. "
                               "No order. Skip this trade, or plan a closer stop with trader-exit."
                               % (sym, lot_size, money(one_lot_risk, market=market), money(max_loss, market=market)))
                else:
                    refusal = ("one share risks %s (entry %s minus stop %s), more than your %s max loss per trade. "
                               "No order. Skip this trade, or plan a closer stop with trader-exit."
                               % (money(risk, market=market), _price(ref, tick_size), _price(stop, tick_size), money(max_loss, market=market)))
            else:
                unit_lbl = ("one lot of %s (%d shares)" % (sym, lot_size)) if lot_size > 1 else ("one share of %s" % sym)
                refusal = ("%s costs %s, more than your %s limit (%s). No order."
                           % (unit_lbl, money(ref, market=market), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
        elif min_rr and reward < min_rr * risk - EPS:
            refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                       "would block it. No order. Skip it, or make a new plan with trader-exit."
                       % (reward / float(risk), _price(target, tick_size), _price(ref, tick_size), _price(stop, tick_size), _num(c["min_reward_risk"])))
        elif cost < float(min_order) - 0.005:
            refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                       % (qty, "" if qty == 1 else "s", sym, money(cost, market=market), money(min_order, market=market)))

    plan_style = str(plan.get("trading_style") or "").strip().lower()
    plan_flat = str(plan.get("flat_by") or "").strip().lower()
    if trading_style is None and plan_style == "intraday" and plan_flat == "positional":
        raise SizeError(
            "conflicting plan metadata: trading_style='intraday' conflicts with flat_by='positional'; "
            "pass --trading-style explicitly or regenerate trader/plan.json"
        )
    raw_style = trading_style or plan_style or ("positional" if plan_flat == "positional" else None) or c.get("trading_style") or "intraday"
    resolved_style = str(raw_style).strip().lower()
    if resolved_style not in ("intraday", "positional"):
        raise SizeError("unsupported trading_style %r; use 'intraday' or 'positional'" % raw_style)
    if resolved_style == "positional":
        resolved_flat_by = "positional"
        pos_cap = validate_positional_support(c, market=market, asset_type=asset_type, stop=stop, target=target)
    else:
        resolved_flat_by = plan.get("flat_by") if (plan.get("flat_by") and plan_flat != "positional") else (c.get("flat_by") or ("15:20" if market == "IN" else "15:55"))
        pos_cap = None
    trading_style = resolved_style
    res = {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_tick(entry_plan, tick_size),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward / float(risk), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "currency": "INR" if market == "IN" else "USD",
        "max_loss_per_trade": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "max_loss": loss, "flat_by": resolved_flat_by,
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
        "asset_type": asset_type, "trading_style": trading_style,
    }
    if pos_cap:
        res["product"] = pos_cap["product"]
        res["broker"] = pos_cap["broker"]
        res["paper"] = pos_cap["paper"]
    elif market == "IN":
        res["product"] = "MIS"
        res["broker"] = str(c.get("broker") or "paper").strip().lower()
        res["paper"] = bool(c.get("paper", True))
    if "broker_supports_multi_session" in c or "supports_multi_session_protection" in c:
        res["broker_supports_multi_session"] = c.get("broker_supports_multi_session", c.get("supports_multi_session_protection"))
    if market != "IN":
        res["max_loss_per_trade_usd"] = max_loss
    if plan.get("option_contract"):
        res["option_contract"] = plan["option_contract"]
    if lot_size > 1:
        res["contracts"] = qty // lot_size
    if market == "IN":
        res["market"] = "IN"
        res["currency"] = "INR"
        res["lot_size"] = lot_size
        res["tick_size"] = tick_size
        if resolved_exchange:
            res["exchange"] = resolved_exchange
        if is_t2t:
            res["is_trade_to_trade"] = True
        res["transaction_costs"] = calculate_indian_transaction_charges(
            asset_type=asset_type,
            side="buy",
            entry_price=ref,
            exit_price=target,
            qty=qty,
            trading_style=trading_style,
            as_of_date=today,
        )
    for k in PROVENANCE_KEYS:
        if k in plan:
            res[k] = plan[k]
    return res


def build_order(s, cfg=None):
    """Build the proposed order for intraday bracket or Indian positional GTT OCO trading."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    market = s.get("market", "US")
    asset_type = str(s.get("asset_type") or "EQUITY").upper()
    style = str(s.get("trading_style") or ("positional" if str(s.get("flat_by") or "").lower() == "positional" else "intraday")).strip().lower()
    if style not in ("intraday", "positional"):
        raise SizeError("unsupported trading_style %r; use 'intraday' or 'positional'" % style)

    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    if s.get("tick_size") is not None:
        tick_sz = s["tick_size"]
    elif market == "IN":
        raise SizeError("Missing resolved tick_size for %s" % s.get("symbol"))
    else:
        tick_sz = 0.01
    max_loss_limit = s.get("max_loss_per_trade") if s.get("max_loss_per_trade") is not None else s.get("max_loss_per_trade_usd")
    max_loss_how = "%s max loss per trade" % money(max_loss_limit or 0, market=market)
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop)"
           % (setup, s["symbol"], s["qty"], {"max loss": max_loss_how,
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}.get(s["bound_by"], s["bound_by"]),
              s["qty"], _price(s["risk_per_share"], tick_sz), money(s["max_loss"], market=market)))

    if style == "positional":
        eff_cfg = cfg if cfg is not None else {
            "broker": s.get("broker", "paper" if market == "IN" else "alpaca"),
            "paper": s.get("paper", True),
            "broker_supports_multi_session": s.get("broker_supports_multi_session", True),
        }
        pos_spec = validate_positional_support(
            eff_cfg, market=market, asset_type=asset_type, stop=s.get("stop"), target=s.get("target")
        )
        product = s.get("product") or pos_spec["product"]
        protection = {
            "type": "gtt_oco",
            "product": product,
            "time_in_force": "gtc",
            "persists_across_sessions": True,
            "triggers": {
                "stop_loss": _price(s["stop"], tick_sz),
                "take_profit": _price(s["target"], tick_sz),
            },
            "note": (
                "Paper GTT OCO (%s): stop-loss %s and take-profit %s remain active across sessions "
                "until triggered, canceled, or manually exited."
                % (product, _price(s["stop"], tick_sz), _price(s["target"], tick_sz))
            ),
        }
        o = {
            "symbol": s["symbol"],
            "side": "buy",
            "qty": str(int(s["qty"])),
            "type": s["order_type"],
            "time_in_force": "gtc",
        }
        if s["order_type"] == "limit":
            o["limit_price"] = _price(s["entry"], tick_sz)
        o.update({
            "order_class": "gtt_oco",
            "product": product,
            "trading_style": "positional",
            "take_profit_limit_price": _price(s["target"], tick_sz),
            "stop_loss_stop_price": _price(s["stop"], tick_sz),
            "multi_session_protection": protection,
            "why": why,
            "plan": {
                "entry": s["entry"], "stop": s["stop"], "target": s["target"],
                "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"],
                "setup": s["setup"], "entry_time": s["entry_time"],
                "flat_by": "positional", "trading_style": "positional", "product": product,
            },
        })
    else:
        o = {"symbol": s["symbol"], "side": "buy", "qty": str(int(s["qty"])), "type": s["order_type"],
             "time_in_force": "day"}
        if s["order_type"] == "limit":
            o["limit_price"] = _price(s["entry"], tick_sz)
        o.update({
            "order_class": "bracket",
            "take_profit_limit_price": _price(s["target"], tick_sz),
            "stop_loss_stop_price": _price(s["stop"], tick_sz),
            "why": why,
            "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                     "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"], "setup": s["setup"],
                     "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
        })
        if market == "IN":
            o["trading_style"] = "intraday"
            o["product"] = s.get("product") or "MIS"
            o["plan"]["trading_style"] = "intraday"
            o["plan"]["product"] = o["product"]

    if s.get("asset_type") and s["asset_type"] != "EQUITY":
        o["asset_type"] = s["asset_type"]
        o["plan"]["asset_type"] = s["asset_type"]
    if s.get("lot_size") and s["lot_size"] > 1:
        o["lot_size"] = s["lot_size"]
        o["contracts"] = int(s["qty"]) // s["lot_size"]
        o["plan"]["lot_size"] = s["lot_size"]
        o["plan"]["contracts"] = o["contracts"]
    if s.get("option_contract"):
        o["option_contract"] = s["option_contract"]
        o["plan"]["option_contract"] = s["option_contract"]
    if s.get("transaction_costs"):
        o["transaction_costs"] = s["transaction_costs"]
        o["plan"]["transaction_costs"] = s["transaction_costs"]
    if market == "IN":
        o["market"] = "IN"
        o["currency"] = "INR"
        o["plan"]["market"] = "IN"
        if s.get("tick_size") is not None:
            o["tick_size"] = s["tick_size"]
            o["plan"]["tick_size"] = s["tick_size"]
        if s.get("exchange"):
            o["exchange"] = s["exchange"]
            o["plan"]["exchange"] = s["exchange"]
    for k in PROVENANCE_KEYS:
        if k in s:
            o[k] = s[k]
            o["plan"][k] = s[k]
    return o


def bracket_order(s, cfg=None):
    """Backward-compatible entry point returning the style-appropriate proposed order."""
    return build_order(s, cfg=cfg)


def plan_report(s, order, notes=()):
    lines = []
    market = s.get("market", "US")
    if s.get("tick_size") is not None:
        tick_sz = s["tick_size"]
    elif market == "IN":
        raise SizeError("Missing resolved tick_size for %s" % s.get("symbol"))
    else:
        tick_sz = 0.01
    tz_label = "IST" if market == "IN" else "New York"
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"], tick_sz),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"], tick_sz), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"], tick_sz),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"], tick_sz), src, _price(s["risk_per_share"], tick_sz),
                    _price(s["entry"], tick_sz), _price(s["stop"], tick_sz), s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-58s %3d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    if s.get("asset_type") in ("OPTIONS", "FUTURES") or order.get("contracts"):
        c_num = order.get("contracts", s["qty"] // s.get("lot_size", 1))
        lines.append("Size: %d contract%s (%d shares, lot size %d) of %s = %s at %s. Max loss at the stop: %d x %s = %s."
                     % (c_num, "" if c_num == 1 else "s", s["qty"], s.get("lot_size", 1), s["symbol"],
                        money(s["cost"], market=market), _price(s["entry"]), s["qty"],
                        _price(s["risk_per_share"]), money(s["max_loss"], market=market)))
    else:
        lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s = %s."
                     % (s["qty"], s["symbol"], money(s["cost"], market=market), _price(s["entry"]), s["qty"],
                        _price(s["risk_per_share"]), money(s["max_loss"], market=market)))
    if s.get("option_contract", {}).get("is_simulated") or order.get("tier") == "SIMULATED":
        lines.append("DATA NOTICE: Simulated option pricing (Black-Scholes model). NOT live market quotes.")
    if s.get("transaction_costs"):
        tc = s["transaction_costs"]
        lines.append("Est transaction charges: %s (STT %s, Exch %s, Stamp %s, SEBI %s, GST %s, Brokerage est %s; excl. DP/actual broker tariff)"
                     % (money(tc["total"], market=market), money(tc["stt"], market=market),
                        money(tc["exchange_charges"], market=market), money(tc["stamp_duty"], market=market),
                        money(tc["sebi_charges"], market=market), money(tc["gst"], market=market),
                        money(tc["brokerage"], market=market)))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    is_positional = (
        str(order.get("trading_style") or "").lower() == "positional"
        or str(order.get("order_class") or "").lower() == "gtt_oco"
        or str(s.get("trading_style") or "").lower() == "positional"
    )
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    if is_positional:
        prod = order.get("product") or s.get("product") or "CNC"
        tif = order.get("time_in_force", "gtc")
        lines.append("Proposed positional paper order (GTT OCO multi-session protection, nothing has been placed):")
        lines.append("  buy %s %s, %s, %s, %s, take-profit %s, stop-loss %s (GTT OCO across sessions)"
                     % (order["qty"], order["symbol"], how, tif, prod,
                        order["take_profit_limit_price"], order["stop_loss_stop_price"]))
        lines.append("Multi-session protection (%s GTT OCO): stop-loss %s and target %s remain active across "
                     "sessions (no intraday auto-square-off)."
                     % (prod, order["stop_loss_stop_price"], order["take_profit_limit_price"]))
    else:
        lines.append("Proposed bracket order (place_stock_order arguments, nothing has been placed):")
        lines.append("  buy %s %s, %s, %s, take-profit %s, stop-loss %s"
                     % (order["qty"], order["symbol"], how, order.get("time_in_force", "day"),
                        order["take_profit_limit_price"], order["stop_loss_stop_price"]))
        if s["flat_by"] and str(s["flat_by"]).lower() != "positional":
            lines.append("Flat by %s %s: anything still open then gets closed (trader-alerts rings, "
                         "trader-broker flatten)." % (s["flat_by"], tz_label))
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) currency amount per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: amount}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole currency units (unit 1) or sub-units (cents/paise, unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(allocations, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(allocations[t] / prices[t] + EPS)) if allocations[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = allocations[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, whole=None, cents=False, top=None, market=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict)."""
    cfg = cfg or {}
    if market is None:
        market = get_market_profile(None, None, cfg)
    curr = "\u20b9" if market == "IN" else "$"
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order_raw = _cfg_money(cfg, cfg, "min_order", market, default=5)
    min_order = float(min_order_raw) if min_order_raw is not None else 0.0
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    if whole is None:
        whole = not cfg.get("fractional", True)
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01 if (cents or whole) else 1.0

    if whole:
        missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
        if missing:
            raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                            "e.g. with trader-alerts: alerts.py quote %s --write)"
                            % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        amounts = round_to_unit(exact, capped, order, cap, unit)
        shares = None
        if whole:
            shares = whole_shares(amounts, prices, order, cap, sum(amounts.values()))
            amounts = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if amounts[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if whole and shares is not None and shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs %s%s, more than its slice; it cannot be bought in whole shares"
                      % (curr, _num(prices[worst["ticker"]])))
        else:
            reason = "its slice (%s%s) is under your %s%s minimum order" % (
                curr, _num(amounts[worst["ticker"]]), curr, _num(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
             "source_weight": r["source_weight"], "amount": amounts[t],
             "capped": t in capped}
        if market != "IN":
            p["dollars"] = amounts[t]
        if prices.get(t):
            p["price"] = prices[t]
            p["shares"] = shares[t] if (whole and shares is not None) else round(amounts[t] / prices[t], 4)
        positions.append(p)
    invested = round(sum(p["amount"] for p in positions), 2)
    out_res = {
        "market": market, "currency": "INR" if market == "IN" else "USD",
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order": min_order, "whole_shares": bool(whole),
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }
    if market != "IN":
        out_res["min_order_usd"] = min_order
    return out_res


def orders_from(result, source=""):
    """Weights mode: proposed buy orders, one per position, in place_stock_order shape."""
    out = []
    market = result.get("market", "US")
    for p in result["positions"]:
        amt = p.get("amount") if p.get("amount") is not None else p.get("dollars", 0)
        if amt <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"], market=market))
        o = {"symbol": p["ticker"], "side": "buy"}
        if result["whole_shares"]:
            o["qty"] = str(int(p["shares"]))
        else:
            o["notional"] = _num(amt)
        o.update({"type": "market", "time_in_force": "day", "why": why})
        if market == "IN":
            o["market"] = "IN"
            o["currency"] = "INR"
        out.append(o)
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    market = r.get("market", "US")
    amt_col = "amount" if market == "IN" else "dollars"
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (money(r["budget"], market=market), _num(r["cash_buffer_pct"]), money(r["cash_buffer"], market=market),
                    money(r["investable"], market=market), _num(r["max_position_pct"]), money(r["cap"], market=market)))
    lines.append("")
    has_price = any("price" in p for p in r["positions"])
    head = "%-7s %-22s %8s %11s" % ("ticker", "name", "weight", amt_col)
    if has_price:
        head += " %10s %10s" % ("price", "shares")
    lines.append(head + "  note")
    for p in r["positions"]:
        amt = p.get("amount") if p.get("amount") is not None else p.get("dollars", 0)
        row = "%-7s %-22s %7.2f%% %11s" % (p["ticker"], (p["name"] or "")[:22], p["weight"], money(amt, market=market))
        if has_price:
            row += " %10s %10s" % (money(p["price"], market=market) if "price" in p else "", p.get("shares", ""))
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-7s %-22s %8s %11s" % ("", "invested", "", money(r["invested"], market=market)))
    lines.append("%-7s %-22s %8s %11s" % ("", "cash buffer", "", money(r["cash_buffer"], market=market)))
    lines.append("%-7s %-22s %8s %11s" % ("", "left idle", "", money(r["idle"], market=market)))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: %s" % (money(r["idle"], market=market), (
            "whole shares do not divide evenly" if r["whole_shares"]
            else "every stock is at its cap; add stocks or raise max_position_pct")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (place_stock_order arguments, nothing has been placed):")
    for o in orders:
        amount = ("notional \"%s\"" % o["notional"]) if "notional" in o else ("qty \"%s\"" % o["qty"])
        lines.append("  %s %s %s, %s, %s" % (o["side"], o["symbol"], amount, o["type"], o["time_in_force"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None, simulated_at=None, last_data_timestamp=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    lts = last_data_timestamp
    if lts is None and orders:
        for o in orders:
            if isinstance(o, dict) and o.get("last_data_timestamp"):
                lts = o.get("last_data_timestamp")
                break
    sim_at = simulated_at
    if sim_at is None and orders:
        for o in orders:
            if isinstance(o, dict) and o.get("simulated_at"):
                sim_at = o.get("simulated_at")
                break
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    in_market = bool(cfg.get("market") == "IN" or any(isinstance(o, dict) and (o.get("market") == "IN" or o.get("currency") == "INR") for o in (orders or [])))
    if in_market:
        doc["market"] = "IN"
        doc["currency"] = "INR"
    if sim_at is not None:
        doc["simulated_at"] = sim_at
    if lts is not None:
        doc["last_data_timestamp"] = lts
    for k in PROVENANCE_KEYS:
        if k in ("simulated_at", "last_data_timestamp"):
            continue
        vals = []
        for o in (orders or []):
            if not isinstance(o, dict):
                continue
            val = None
            if k in o and o[k] is not None:
                val = o[k]
            elif isinstance(o.get("plan"), dict) and k in o["plan"] and o["plan"][k] is not None:
                val = o["plan"][k]
            if val is not None and all(v != val or type(v) is not type(val) for v in vals):
                vals.append(val)
        if len(vals) == 1:
            doc[k] = vals[0]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    market = getattr(a, "market", None) or get_market_profile(a, plan, cfg)
    explicit_now = bool(getattr(a, "now", None))
    now = parse_now(a.now, market=market)
    today = to_local(now, market=market).date().isoformat()
    simulated = bool(explicit_now or (isinstance(plan, dict) and plan.get("simulated_at")))

    if explicit_now and isinstance(plan, dict):
        plan_lts_raw = plan.get("last_data_timestamp")
        if plan_lts_raw:
            plan_lts = parse_utc(plan_lts_raw)
            if plan_lts is not None and plan_lts > now:
                raise SizeError(
                    "Plan data timestamp %s is after simulated time %s (look-ahead blocked)"
                    % (plan_lts_raw, _market_iso(now, market=market))
                )
        plan_date = str(plan.get("date") or "")
        if plan_date:
            if plan_date > today:
                raise SizeError(
                    "Plan date %s is after simulated date %s (look-ahead blocked)" % (plan_date, today)
                )
            if plan_date == today and plan.get("entry_time"):
                et_t = _parse_clock_time(plan.get("entry_time"))
                if et_t is not None:
                    et_utc = local_to_utc(
                        datetime.datetime.combine(datetime.date.fromisoformat(plan_date), et_t),
                        market=market,
                    )
                    if et_utc > now:
                        raise SizeError(
                            "Plan entry time %s on %s is after simulated time (look-ahead blocked)"
                            % (plan.get("entry_time"), plan_date)
                        )

    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 364.00")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price, used_price_t = [], None, None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    tz_lbl = "IST" if market == "IN" else "New York"
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now, allow_clock_skew=not explicit_now)
        if p is not None and fresh:
            price = p
            used_price_t = t
        elif p is not None:
            if t is not None and t > now:
                notes.append(
                    "%s in %s is from %s (after cutoff %s %s): not used (future quote ignored). Sized at the plan's entry."
                    % (sym, ppath, to_local(t, market=market).strftime("%Y-%m-%d %H:%M"),
                       to_local(now, market=market).strftime("%Y-%m-%d %H:%M"), tz_lbl)
                )
            else:
                notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                             % (sym, ppath, ("from " + to_local(t, market=market).strftime("%Y-%m-%d %H:%M") + " " + tz_lbl)
                                if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today, market=market,
                   instruments_path=getattr(a, "instruments", None),
                   trading_style=getattr(a, "trading_style", None),
                   exchange=getattr(a, "exchange", None))
    if price is not None and used_price_t is not None and s.get("entry_from") == "price":
        note_tick = s.get("tick_size") or 0.01
        notes.insert(0, "%s last %s (as of %s %s) is above the plan's entry %s, so the risk "
                     "is bigger: sized at %s." % (
                         sym, _price(price, note_tick),
                         to_local(used_price_t, market=market).strftime("%H:%M"),
                         tz_lbl,
                         _price(s["entry_plan"], note_tick),
                         _price(s["entry"], note_tick),
                     ))

    plan_lts_utc = parse_utc(plan.get("last_data_timestamp")) if isinstance(plan, dict) else None
    latest_data_utc = plan_lts_utc
    if used_price_t is not None and (latest_data_utc is None or used_price_t > latest_data_utc):
        latest_data_utc = used_price_t
    if latest_data_utc is not None:
        s["last_data_timestamp"] = _market_iso(latest_data_utc, market=market)
    sim_iso = _market_iso(now, market=market) if simulated else None
    if sim_iso is not None:
        s["simulated_at"] = sim_iso
    has_clock_override = bool(explicit_now or (isinstance(plan, dict) and plan.get("clock_override")))
    if has_clock_override:
        s["clock_override"] = True
        s["is_live_signal"] = False
        s["is_live_data"] = False
        if str(s.get("data_tier") or "").upper() == "CURRENT":
            s["data_tier"] = "HISTORICAL"
            s["is_historical_analysis"] = True

    order = build_order(s, cfg=cfg)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now, simulated_at=sim_iso, last_data_timestamp=s.get("last_data_timestamp"))
    if a.json:
        out_doc = {"sizing": s, "orders": [order] if order else [], "notes": notes}
        if sim_iso is not None:
            out_doc["simulated_at"] = sim_iso
        if has_clock_override:
            out_doc["clock_override"] = True
        if s.get("last_data_timestamp") is not None:
            out_doc["last_data_timestamp"] = s.get("last_data_timestamp")
        print(json.dumps(out_doc, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            kind_lbl = "positional GTT OCO order" if s.get("trading_style") == "positional" else "bracket order"
            print("Wrote 1 proposed %s to %s" % (kind_lbl, a.write))
    return 0 if order else 1


def run_weights(a, cfg):
    market = getattr(a, "market", None) or get_market_profile(a, None, cfg)
    now = parse_now(a.now, market=market)
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    prices = {}
    latest_ts = None
    if ppath:
        raw_pdoc = read_json(ppath, "prices")
        loaded = load_prices(ppath)
        is_price_doc = isinstance(raw_pdoc, dict) and isinstance(raw_pdoc.get("prices"), dict)
        for sym_k, px_v in loaded.items():
            if is_price_doc and a.now:
                pt = price_time(raw_pdoc, sym_k)
                if pt is not None:
                    if pt > now:
                        continue
                    if latest_ts is None or pt > latest_ts:
                        latest_ts = pt
            prices[sym_k] = px_v
    whole = True if a.whole_shares else None
    result = size(rows, cfg, a.budget, prices, whole, a.cents, a.top, market=market)
    orders = orders_from(result, source)
    sim_iso = _market_iso(now, market=market) if a.now else None
    lts_iso = _market_iso(latest_ts, market=market) if latest_ts is not None else None
    if a.write:
        write_orders(a.write, orders, cfg, source, now, simulated_at=sim_iso, last_data_timestamp=lts_iso)
    if a.json:
        out_doc = {"result": result, "orders": orders}
        if sim_iso is not None:
            out_doc["simulated_at"] = sim_iso
        if lts_iso is not None:
            out_doc["last_data_timestamp"] = lts_iso
        print(json.dumps(out_doc, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a day trade to your budget and max loss (trader-budget).")
    ap.add_argument("action", nargs="?", choices=["trade", "plan"], help=argparse.SUPPRESS)
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--market", choices=["US", "IN"], help="market profile: US (default) or IN")
    ap.add_argument("--exchange", choices=["NSE", "BSE"], help="exchange for Indian instruments (NSE or BSE)")
    ap.add_argument("--trading-style", choices=["intraday", "positional"],
                    help="override trading style (intraday bracket/day vs positional CNC/NRML GTT OCO)")
    ap.add_argument("--instruments", default=None, help="path to instruments cache (defaults to trader/state/instruments.json if present)")
    ap.add_argument("--now", help="override the application clock to this market time for backtests/tests, e.g. \"2026-10-19 10:15\" (marks signals non-live)")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: AAPL=22.04,AXP=17.14")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--whole-shares", action="store_true", help="whole shares only (needs --prices)")
    g.add_argument("--cents", action="store_true", help="size to the sub-unit (cent/paise) instead of whole units")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top or a.whole_shares or a.cents:
            raise SizeError("--top, --whole-shares and --cents work with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
