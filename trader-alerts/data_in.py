"""Indian market data infrastructure for NSE and BSE equities.

Provides modular market data fetching, symbol translation, OHLCV bar normalization,
data quality tier classification, and error handling for BharatTrader AI.

Python 3.8 compatible without third-party dependencies.
"""

import datetime
from decimal import Decimal, InvalidOperation
import gzip
import inspect
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Ensure module path resolves sibling modules in trader-alerts
_alerts_dir = os.path.dirname(os.path.abspath(__file__))
if _alerts_dir not in sys.path:
    sys.path.insert(0, _alerts_dir)

import env_loader
env_loader.load_project_dotenv()

parse_dotenv = env_loader.parse_dotenv
load_dotenv_file = env_loader.load_dotenv_file
load_project_dotenv = env_loader.load_project_dotenv
resolve_project_root = env_loader.resolve_project_root

import symbols_in
import calendar_in
import instruments
import upstox_feed

# User agent and provider constants
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; BharatTrader/1.0; +https://github.com/KulmeetSJ/day-trader-skill)"
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
DEFAULT_TIMEOUT = 10
DEFAULT_PAUSE = 0.3
DEFAULT_BACKOFF = 2.0


def _normalize_provider_tick_size(raw_tick, source_unit="INR", segment="", instrument_type=""):
    """Normalize raw tick_size to canonical INR (rupees) without inventing precision.

    Divides by 100 via Decimal ONLY when source_unit is 'paise'. Returns (tick_inr_or_none, status).
    Valid status values: 'ok', 'not_applicable_index', 'missing', 'invalid'.
    """
    seg_up = str(segment or "").strip().upper()
    type_up = str(instrument_type or "").strip().upper()
    is_index = seg_up.endswith("_INDEX") or type_up == "INDEX"

    if raw_tick is None or (isinstance(raw_tick, str) and not raw_tick.strip()):
        return None, ("not_applicable_index" if is_index else "missing")
    if isinstance(raw_tick, bool):
        return None, "invalid"
    try:
        dec = Decimal(str(raw_tick).strip())
    except (InvalidOperation, ValueError):
        return None, "invalid"
    if not dec.is_finite() or dec <= 0:
        return None, "invalid"

    unit = str(source_unit or "INR").strip().lower()
    if unit in ("paise", "paisa", "p"):
        dec = dec / Decimal("100")
    elif unit not in ("inr", "rupee", "rupees"):
        return None, "invalid"
    return float(dec), "ok"


class DataStatus:
    """Standardized market data freshness and availability status."""
    CURRENT = "CURRENT"
    DELAYED = "DELAYED"
    HISTORICAL = "HISTORICAL"
    FIXTURE = "FIXTURE"
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "UNAVAILABLE"


class DataTier:
    """Classification of market data quality, latency, and origin."""
    REAL_TIME = "REAL_TIME"    # Direct exchange/broker tick stream (un-delayed)
    DELAYED = "DELAYED"        # Delayed exchange feed (e.g. Yahoo Finance 15-minute delay)
    HISTORICAL = "HISTORICAL"  # Closed session completed bars (EOD or settled intraday)
    SIMULATED = "SIMULATED"    # Synthetic, mock, or backtest replay data
    FIXTURE = "FIXTURE"        # Local offline saved fixture snapshot
    UNAVAILABLE = "UNAVAILABLE"


# Standard capability keys across Indian market data providers
CAPABILITY_HISTORICAL_CANDLES = "historical_candles"
CAPABILITY_INTRADAY_CANDLES = "intraday_candles"
CAPABILITY_LIVE_QUOTES = "live_quotes"
CAPABILITY_STREAMING_DATA = "streaming_data"
CAPABILITY_OPTION_CHAINS = "option_chains"
CAPABILITY_OPEN_INTEREST = "open_interest"
CAPABILITY_INSTRUMENT_MASTERS = "instrument_masters"
CAPABILITY_MARKET_STATUS = "market_status"

ALL_CAPABILITIES = (
    CAPABILITY_HISTORICAL_CANDLES,
    CAPABILITY_INTRADAY_CANDLES,
    CAPABILITY_LIVE_QUOTES,
    CAPABILITY_STREAMING_DATA,
    CAPABILITY_OPTION_CHAINS,
    CAPABILITY_OPEN_INTEREST,
    CAPABILITY_INSTRUMENT_MASTERS,
    CAPABILITY_MARKET_STATUS,
)


class DataError(Exception):
    """Base exception for market data operations."""
    pass


class InvalidSymbolError(DataError):
    """Raised when an equity symbol format is invalid for the specified exchange."""
    pass


class SymbolNotFoundError(DataError):
    """Raised when a symbol is not found by the data provider (HTTP 404)."""
    pass


class RateLimitError(DataError):
    """Raised when data provider throttles requests (HTTP 429)."""
    pass


class ProviderConnectionError(DataError):
    """Raised when data provider cannot be reached (HTTP 5xx, timeout, network error)."""
    pass


class MissingDataError(DataError):
    """Raised when response contains no data or empty timestamps."""
    pass


class NotSupportedError(DataError):
    """Raised when the active market data provider does not support a capability."""

    def __init__(self, message, capability=None, provider=None):
        super().__init__(message)
        self.status = "NOT_SUPPORTED"
        self.capability = capability
        self.provider = provider

    def to_dict(self):
        return {
            "status": self.status,
            "capability": self.capability,
            "provider": self.provider,
            "error": str(self),
            "data_status": DataStatus.UNAVAILABLE,
            "is_live": False,
            "is_simulated": False,
        }


class ProviderConfigError(DataError):
    """Raised when provider selection or configuration is invalid."""
    pass


class AuthenticationConfigError(ProviderConfigError):
    """Raised when an authenticated provider is missing required credentials."""
    pass


def _normalize_exchange(exchange):
    exch = (exchange or "NSE").upper().strip()
    if exch in ("NSI", "NSE_EQ"):
        return "NSE"
    if exch in ("BOM", "BSE_EQ"):
        return "BSE"
    return exch


def _format_ist(ts):
    if ts is None:
        return ""
    try:
        dt_ist = datetime.datetime.fromtimestamp(int(ts), tz=calendar_in.IST)
        return dt_ist.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return ""


def _format_ist_iso(ts):
    if ts is None:
        return None
    try:
        dt_ist = datetime.datetime.fromtimestamp(int(ts), tz=calendar_in.IST)
        return dt_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    except Exception:
        return None


def _parse_cutoff_ts(now):
    """Parse an optional cutoff (datetime/date/str/epoch) into (dt_ist, epoch_ts)."""
    if now is None:
        return None, None
    if isinstance(now, (int, float)):
        dt_ist = datetime.datetime.fromtimestamp(int(now), tz=calendar_in.IST)
        return dt_ist, int(dt_ist.timestamp())
    if isinstance(now, datetime.date) and not isinstance(now, datetime.datetime):
        # Interpret a plain date cutoff as end-of-day IST
        dt_ist = datetime.datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=calendar_in.IST)
        return dt_ist, int(dt_ist.timestamp())
    dt_ist = calendar_in.parse_ist(now)
    return dt_ist, int(dt_ist.timestamp())


def _interval_to_seconds(interval):
    m = re.match(r"^(\d+)([smhd])$", str(interval or "5m").strip())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _is_daily_session_complete(session_date, cutoff_dt_ist, exchange="NSE"):
    """Return True if a daily bar for session_date is complete by cutoff_dt_ist."""
    if cutoff_dt_ist is None:
        return True
    cutoff_date = cutoff_dt_ist.date()
    if session_date < cutoff_date:
        return True
    if session_date > cutoff_date:
        return False
    hrs = calendar_in.get_segment_session_hours(
        calendar_in.MarketSegment.CASH_EQUITY, session_date, exchange=exchange
    )
    close_str = hrs.get("cas_close") or hrs.get("continuous_close") or "15:30"
    close_dt = calendar_in.parse_ist(
        "%s %s:00" % (session_date.strftime("%Y-%m-%d"), str(close_str)[:5])
    )
    return cutoff_dt_ist >= close_dt


def _sanitize_meta_as_of(meta, bars, cutoff_ts, interval_secs=None, daily=False):
    """Strip or clamp Yahoo meta quote fields so nothing after cutoff_ts leaks through."""
    m = dict(meta or {})
    if cutoff_ts is None:
        return m
    cutoff_ts = int(cutoff_ts)
    if not bars:
        for k in (
            "regularMarketPrice",
            "regularMarketTime",
            "regularMarketDayHigh",
            "regularMarketDayLow",
            "regularMarketVolume",
            "regularMarketOpen",
            "postMarketPrice",
            "preMarketPrice",
        ):
            m.pop(k, None)
        return m

    last = bars[-1]
    last_ts = last.get("ts") if last.get("ts") is not None else last.get("t")
    if last_ts is not None:
        last_ts = int(last_ts)
        last_close_ts = last_ts if daily else min(cutoff_ts, last_ts + int(interval_secs or 0))
    else:
        last_close_ts = None

    rmt = m.get("regularMarketTime")
    needs_sanitize = rmt is None
    if rmt is not None:
        try:
            needs_sanitize = int(rmt) > cutoff_ts
        except (TypeError, ValueError):
            needs_sanitize = True

    if needs_sanitize:
        m.pop("postMarketPrice", None)
        m.pop("preMarketPrice", None)
        if daily:
            m["regularMarketPrice"] = last.get("c")
            m["regularMarketOpen"] = last.get("o")
            m["regularMarketDayHigh"] = last.get("h")
            m["regularMarketDayLow"] = last.get("l")
            m["regularMarketVolume"] = last.get("v")
        else:
            last_date = last.get("date")
            day_bars = [b for b in bars if b.get("date") == last_date] if last_date else [last]
            if not day_bars:
                day_bars = [last]
            m["regularMarketOpen"] = day_bars[0].get("o")
            m["regularMarketDayHigh"] = max(b.get("h") for b in day_bars if b.get("h") is not None)
            m["regularMarketDayLow"] = min(b.get("l") for b in day_bars if b.get("l") is not None)
            m["regularMarketPrice"] = day_bars[-1].get("c")
            m["regularMarketVolume"] = sum(int(b.get("v") or 0) for b in day_bars)
        if last_close_ts is not None:
            m["regularMarketTime"] = last_close_ts
    return m


def _call_with_optional_now(fn, *args, now=None, **kwargs):
    """Invoke provider methods supporting optional `now` while remaining backward compatible."""
    if now is None:
        return fn(*args, **kwargs)
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        accepts_now = "now" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_now = False
    if accepts_now:
        return fn(*args, now=now, **kwargs)
    return fn(*args, **kwargs)


def _validate_quote_as_of(q, now, symbol=None):
    """Ensure a quote/LTP/OHLC payload is not timestamped after `now`."""
    if now is None or not isinstance(q, dict):
        return q
    if q.get("error") and q.get("data_status") == DataStatus.UNAVAILABLE:
        return q
    _, cutoff_ts = _parse_cutoff_ts(now)
    q_ts = q.get("timestamp")
    if q_ts is not None:
        try:
            q_ts_int = int(q_ts)
        except (TypeError, ValueError):
            q_ts_int = None
        if q_ts_int is not None and cutoff_ts is not None and q_ts_int > cutoff_ts:
            raise MissingDataError(
                "Quote timestamp %s is after simulated cutoff %s for %s"
                % (_format_ist(q_ts_int), _format_ist(cutoff_ts), symbol or q.get("symbol", ""))
            )
    return q


def _compute_freshness(timestamp, data_status, now=None):
    """Compute standardized freshness metadata from epoch timestamp and data_status."""
    now_dt = calendar_in.parse_ist(now) if now else calendar_in.now_ist()
    now_ts = int(now_dt.timestamp())
    age_secs = None
    if timestamp is not None:
        try:
            age_secs = max(0, int(now_ts - int(timestamp)))
        except Exception:
            age_secs = None

    if data_status == DataStatus.DELAYED:
        note = "Delayed unofficial feed (do not treat as real-time)"
    elif data_status == DataStatus.FIXTURE:
        note = "Local offline fixture (historical snapshot)"
    elif data_status == DataStatus.SIMULATED:
        note = "Simulated / theoretical model output"
    elif data_status == DataStatus.HISTORICAL:
        note = "Historical completed bars"
    elif data_status == DataStatus.CURRENT:
        note = "Current session quote or bar"
    else:
        note = "Data unavailable"

    return age_secs, {
        "status": data_status,
        "age_seconds": age_secs,
        "delayed": data_status == DataStatus.DELAYED,
        "note": note,
    }


def _standardize_payload(payload, provider_name, symbol=None, exchange="NSE",
                         data_status=DataStatus.DELAYED, tier=DataTier.DELAYED,
                         timestamp=None, is_live=False, is_simulated=False, now=None):
    """Attach standardized provider, symbol, IST timestamp, freshness, and status metadata."""
    exch = _normalize_exchange(exchange or payload.get("exchange") or "NSE")
    canon = symbol or payload.get("symbol") or payload.get("underlying") or ""
    ts = timestamp if timestamp is not None else payload.get("timestamp")
    ist_time = payload.get("ist_time") or payload.get("time") or (_format_ist(ts) if ts else "")
    age_secs, freshness = _compute_freshness(ts, data_status, now=now)

    out = dict(payload)
    out["provider"] = provider_name
    out["source"] = out.get("source") or provider_name
    out["exchange"] = exch
    if canon:
        if "symbol" in out or not out.get("underlying"):
            out["symbol"] = canon
        out["instrument_id"] = "%s:%s" % (exch, canon)
    out["timestamp"] = ts
    out["ist_time"] = ist_time
    if "time" not in out and ist_time:
        out["time"] = ist_time
    out["freshness_seconds"] = age_secs
    out["freshness"] = freshness
    out["data_status"] = data_status
    out["tier"] = out.get("tier") or tier
    out["is_live"] = bool(is_live)
    out["is_simulated"] = bool(is_simulated)
    out["is_real_time"] = bool(is_live)
    out["last_data_timestamp"] = out.get("last_data_timestamp") or (
        _format_ist_iso(ts) if ts is not None else None
    )
    if now is not None:
        _, cutoff_ts = _parse_cutoff_ts(now)
        if cutoff_ts is not None:
            out["simulated_at"] = _format_ist_iso(cutoff_ts)
    return out


class MarketDataProvider:
    """Abstract base interface for Indian market data providers."""

    provider_name = "base"
    requires_auth = False
    required_credentials = ()
    CAPABILITIES = {cap: False for cap in ALL_CAPABILITIES}

    @classmethod
    def capabilities(cls):
        caps = {cap: bool(cls.CAPABILITIES.get(cap, False)) for cap in ALL_CAPABILITIES}
        caps["provider"] = cls.provider_name
        caps["requires_auth"] = bool(cls.requires_auth)
        caps["required_credentials"] = list(cls.required_credentials)
        caps["not_supported"] = [cap for cap in ALL_CAPABILITIES if not caps[cap]]
        return caps

    def supports(self, capability):
        return bool(self.CAPABILITIES.get(capability, False))

    @classmethod
    def validate_config(cls, config=None, env=None, **kwargs):
        """Validate provider credentials and settings before activation."""
        if not cls.requires_auth or not cls.required_credentials:
            return True
        cfg = config if isinstance(config, dict) else {}
        creds_map = cfg.get("provider_credentials") or {}
        prov_creds = creds_map.get(cls.provider_name) if isinstance(creds_map, dict) else {}
        if not isinstance(prov_creds, dict):
            prov_creds = {}
        env_map = env if env is not None else os.environ
        missing = []
        for key in cls.required_credentials:
            val = (
                kwargs.get(key)
                or prov_creds.get(key)
                or cfg.get(key)
                or env_map.get("%s_%s" % (cls.provider_name.upper(), key.upper()))
                or env_map.get(key.upper())
            )
            if not val:
                missing.append(key)
        if missing:
            raise AuthenticationConfigError(
                "Provider '%s' requires credentials that are missing: %s"
                % (cls.provider_name, ", ".join(missing))
            )
        return True

    def not_supported_response(self, capability, message=None):
        msg = message or (
            "Capability '%s' is not supported by provider '%s'." % (capability, self.provider_name)
        )
        return {
            "status": "NOT_SUPPORTED",
            "capability": capability,
            "provider": self.provider_name,
            "error": msg,
            "data_status": DataStatus.UNAVAILABLE,
            "is_live": False,
            "is_simulated": False,
        }

    def get_quote(self, symbol, exchange="NSE", now=None):
        """Fetch current or delayed quote for a single symbol."""
        raise NotImplementedError

    def get_ltp(self, symbol, exchange="NSE", now=None):
        """Fetch standardized Last Traded Price payload for a single symbol."""
        q = _validate_quote_as_of(
            _call_with_optional_now(self.get_quote, symbol, exchange=exchange, now=now),
            now,
            symbol=symbol,
        )
        ltp = q.get("ltp") if q.get("ltp") is not None else q.get("price")
        if ltp is None:
            raise MissingDataError("No LTP available for %s" % symbol)
        canon = q.get("symbol") or symbols_in.normalize_symbol(symbol)
        exch = q.get("exchange") or _normalize_exchange(exchange)
        payload = {
            "symbol": canon,
            "exchange": exch,
            "currency": q.get("currency", "INR"),
            "ltp": float(ltp),
            "price": float(ltp),
            "timestamp": q.get("timestamp"),
            "ist_time": q.get("ist_time") or q.get("time", ""),
            "source": q.get("source", self.provider_name),
            "tier": q.get("tier", DataTier.DELAYED),
        }
        return _standardize_payload(
            payload,
            provider_name=q.get("provider", self.provider_name),
            symbol=canon,
            exchange=exch,
            data_status=q.get("data_status", DataStatus.DELAYED),
            tier=q.get("tier", DataTier.DELAYED),
            timestamp=q.get("timestamp"),
            is_live=q.get("is_live", False),
            is_simulated=q.get("is_simulated", False),
            now=now,
        )

    def get_ohlc(self, symbol, exchange="NSE", interval="1d", now=None):
        """Fetch standardized OHLC snapshot for a symbol."""
        q = _validate_quote_as_of(
            _call_with_optional_now(self.get_quote, symbol, exchange=exchange, now=now),
            now,
            symbol=symbol,
        )
        close_px = q.get("close") if q.get("close") is not None else q.get("price")
        canon = q.get("symbol") or symbols_in.normalize_symbol(symbol)
        exch = q.get("exchange") or _normalize_exchange(exchange)
        payload = {
            "symbol": canon,
            "exchange": exch,
            "interval": interval,
            "currency": q.get("currency", "INR"),
            "open": float(q["open"]) if q.get("open") is not None else None,
            "high": float(q["high"]) if q.get("high") is not None else None,
            "low": float(q["low"]) if q.get("low") is not None else None,
            "close": float(close_px) if close_px is not None else None,
            "prev_close": float(q["prev_close"]) if q.get("prev_close") is not None else None,
            "volume": int(q["volume"]) if q.get("volume") is not None else None,
            "timestamp": q.get("timestamp"),
            "ist_time": q.get("ist_time") or q.get("time", ""),
            "source": q.get("source", self.provider_name),
            "tier": q.get("tier", DataTier.DELAYED),
        }
        return _standardize_payload(
            payload,
            provider_name=q.get("provider", self.provider_name),
            symbol=canon,
            exchange=exch,
            data_status=q.get("data_status", DataStatus.DELAYED),
            tier=q.get("tier", DataTier.DELAYED),
            timestamp=q.get("timestamp"),
            is_live=q.get("is_live", False),
            is_simulated=q.get("is_simulated", False),
            now=now,
        )

    def get_quotes(self, symbols, exchange="NSE", now=None):
        """Fetch quotes for a collection of symbols with per-ticker error isolation."""
        quotes = {}
        for s in symbols:
            canon = symbols_in.normalize_symbol(s)
            try:
                quotes[canon] = _validate_quote_as_of(
                    _call_with_optional_now(self.get_quote, s, exchange=exchange, now=now),
                    now,
                    symbol=canon,
                )
            except DataError as e:
                quotes[canon] = _standardize_payload(
                    {
                        "symbol": canon,
                        "exchange": exchange,
                        "error": str(e),
                    },
                    provider_name=self.provider_name,
                    symbol=canon,
                    exchange=exchange,
                    data_status=DataStatus.UNAVAILABLE,
                    tier=DataTier.UNAVAILABLE,
                    timestamp=None,
                    is_live=False,
                    is_simulated=False,
                    now=now,
                )
        return quotes

    def get_historical_data(self, symbol, start=None, end=None, range_str="1y", interval="1d", exchange="NSE", now=None):
        """Fetch standardized historical OHLCV bars (daily default or intraday range)."""
        if interval in ("1d", "d", "daily"):
            res = self.get_daily_bars(symbol, range_str=range_str, exchange=exchange, now=now)
        else:
            res = self.get_intraday_data(symbol, interval=interval, range_str=range_str, exchange=exchange, now=now)
        if start or end:
            s_day = calendar_in._resolve_date(start) if start else None
            e_day = calendar_in._resolve_date(end) if end else None
            filtered = []
            for b in res.get("bars", []):
                b_day = calendar_in._resolve_date(b["date"]) if b.get("date") else None
                if s_day and (b_day is None or b_day < s_day):
                    continue
                if e_day and (b_day is None or b_day > e_day):
                    continue
                filtered.append(b)
            res = dict(res)
            res["bars"] = filtered
            if filtered:
                last_ts = filtered[-1].get("ts")
                res["timestamp"] = last_ts
                res["last_data_timestamp"] = _format_ist_iso(last_ts)
            else:
                res["timestamp"] = None
                res["last_data_timestamp"] = None
        return res

    def get_intraday_data(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        """Fetch standardized intraday OHLCV bars aligned to Indian market sessions."""
        return self.get_intraday_bars(
            symbol,
            interval=interval,
            range_str=range_str,
            exchange=exchange,
            now=now,
            is_cas_eligible=is_cas_eligible,
        )

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        """Fetch historical intraday OHLCV bars aligned to Indian market sessions."""
        raise NotImplementedError

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        """Fetch daily historical OHLCV bars for screening and technical indicators."""
        raise NotImplementedError

    def get_instruments(self, exchange="NSE", symbol=None):
        """Fetch standardized equity or derivatives instrument records where supported."""
        raise NotSupportedError(
            "Provider '%s' does not supply instrument masters." % self.provider_name,
            capability=CAPABILITY_INSTRUMENT_MASTERS,
            provider=self.provider_name,
        )

    def get_option_chain(self, symbol, expiry=None, exchange="NSE", spot_price=None):
        """Fetch or construct option chain for underlying symbol and expiry."""
        raise NotSupportedError(
            "Provider '%s' does not support option chains." % self.provider_name,
            capability=CAPABILITY_OPTION_CHAINS,
            provider=self.provider_name,
        )

    def get_derivatives_master(self, exchange="NSE"):
        """Fetch or construct derivatives master containing active contracts and lot sizes."""
        raise NotSupportedError(
            "Provider '%s' does not support derivatives instrument master." % self.provider_name,
            capability=CAPABILITY_INSTRUMENT_MASTERS,
            provider=self.provider_name,
        )

    def get_market_status(self, exchange="NSE", as_of=None):
        """Return standardized IST exchange market status using calendar_in."""
        exch = _normalize_exchange(exchange)
        now_dt = calendar_in.parse_ist(as_of) if as_of else calendar_in.now_ist()
        is_trading, day_status, day_reason = calendar_in.is_trading_day_in(now_dt, exchange=exch)
        phase, phase_reason = calendar_in.get_session_phase(now_dt, exchange=exch)
        is_open = bool(calendar_in.is_market_hours_in(now_dt, exchange=exch))
        last_td = calendar_in.get_last_trading_day(now_dt, exchange=exch)
        ts = int(now_dt.timestamp())
        if is_open:
            mkt_status = "OPEN"
        elif not is_trading:
            if day_reason == "weekend":
                mkt_status = "WEEKEND"
            elif "holiday" in str(day_reason or "").lower():
                mkt_status = "HOLIDAY"
            else:
                mkt_status = "CLOSED"
        elif phase in ("pre_open_entry", "order_matching", "buffer"):
            mkt_status = "PRE_OPEN"
        else:
            mkt_status = "CLOSED"
        payload = {
            "symbol": exch,
            "exchange": exch,
            "status": mkt_status,
            "calendar_verification": str(day_status or "").upper(),
            "is_open": is_open,
            "is_trading_day": bool(is_trading),
            "session": phase,
            "session_reason": phase_reason,
            "day_reason": day_reason,
            "last_trading_session": last_td.isoformat() if last_td else None,
            "as_of": now_dt.strftime("%Y-%m-%d %H:%M:%S IST"),
            "timestamp": ts,
            "ist_time": now_dt.strftime("%Y-%m-%d %H:%M:%S IST"),
            "live_supported": self.supports(CAPABILITY_LIVE_QUOTES),
            "capabilities": self.capabilities(),
        }
        return _standardize_payload(
            payload,
            provider_name=self.provider_name,
            symbol=exch,
            exchange=exch,
            data_status=DataStatus.CURRENT if is_open else DataStatus.HISTORICAL,
            tier=DataTier.DELAYED if self.provider_name == "yahoo" else DataTier.HISTORICAL,
            timestamp=ts,
            is_live=False,
            is_simulated=self.provider_name == "simulated",
            now=now_dt,
        )

    def get_batch_series(self, symbols, range_str="3mo", interval="1d", exchange="NSE", now=None, pause=0.0):
        """Fetch standardized scanner series dict for many symbols, isolating partial errors.

        Returns ({canon_symbol: series_dict}, {canon_symbol: error_message}).
        """
        data, failed = {}, {}
        for i, sym in enumerate(symbols):
            if i > 0 and pause:
                time.sleep(pause)
            canon = symbols_in.normalize_symbol(sym)
            try:
                if interval in ("1d", "d", "daily"):
                    payload = self.get_historical_data(canon, range_str=range_str, interval="1d", exchange=exchange, now=now)
                else:
                    payload = self.get_intraday_data(canon, interval=interval, range_str=range_str, exchange=exchange, now=now)
                data[canon] = to_scanner_series(payload)
            except Exception as e:
                failed[canon] = str(e)
        return data, failed


class YahooMarketDataProvider(MarketDataProvider):
    """Yahoo Finance provider for Indian equities.

    Retrieves quotes and historical bars with automatic multi-host fallback,
    polite backoff, rate-limit recovery, and explicit DELAYED tier labeling.
    """

    provider_name = "yahoo"
    requires_auth = False
    required_credentials = ()
    CAPABILITIES = {
        CAPABILITY_HISTORICAL_CANDLES: True,
        CAPABILITY_INTRADAY_CANDLES: True,
        CAPABILITY_LIVE_QUOTES: False,
        CAPABILITY_STREAMING_DATA: False,
        CAPABILITY_OPTION_CHAINS: False,
        CAPABILITY_OPEN_INTEREST: False,
        CAPABILITY_INSTRUMENT_MASTERS: False,
        CAPABILITY_MARKET_STATUS: True,
    }

    def __init__(self, hosts=YAHOO_HOSTS, user_agent=DEFAULT_USER_AGENT, timeout=DEFAULT_TIMEOUT,
                 pause=DEFAULT_PAUSE, backoff=DEFAULT_BACKOFF, http_get=None):
        self.hosts = hosts
        self.user_agent = user_agent
        self.timeout = timeout
        self.pause = pause
        self.backoff = backoff
        self._custom_http_get = http_get

    def _http_get(self, url):
        if self._custom_http_get is not None:
            return self._custom_http_get(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            code = resp.getcode()
            content = resp.read().decode("utf-8", "replace")
            return code, content

    def _fetch_chart_url(self, provider_symbol, params):
        query_string = urllib.parse.urlencode(params)
        last_error = None
        for n, host in enumerate(self.hosts):
            if n > 0 and self.pause:
                time.sleep(self.pause)
            for attempt in (0, 1):
                url = "https://%s/v8/finance/chart/%s?%s" % (host, provider_symbol, query_string)
                try:
                    code, body = self._http_get(url)
                    if code == 200:
                        return body
                    raise urllib.error.HTTPError(url, int(code), "HTTP %s" % code, {}, None)
                except urllib.error.HTTPError as e:
                    code = e.code
                    try:
                        e.close()
                    except Exception:
                        pass
                    last_error = e
                    if code == 404:
                        raise SymbolNotFoundError("Provider has no data for symbol '%s' (HTTP 404)" % provider_symbol)
                    if code == 429:
                        if attempt == 0:
                            time.sleep(self.backoff)
                            continue
                        break  # Fallback to secondary host on repeated 429
                    if code >= 500:
                        break  # Fallback to secondary host on 5xx server errors
                    raise ProviderConnectionError("Provider returned HTTP %d for symbol '%s'" % (code, provider_symbol))
                except (urllib.error.URLError, OSError) as e:
                    last_error = e
                    break
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
            raise RateLimitError("Rate limit exceeded on provider hosts (HTTP 429). Wait and retry.")
        raise ProviderConnectionError("Could not reach data provider: %s" % last_error)

    def _fetch_spark_url(self, provider_symbols, params):
        query_params = dict(params or {})
        query_params["symbols"] = ",".join(provider_symbols)
        query_string = urllib.parse.urlencode(query_params)
        last_error = None
        for n, host in enumerate(self.hosts):
            if n > 0 and self.pause:
                time.sleep(self.pause)
            for attempt in (0, 1):
                url = "https://%s/v7/finance/spark?%s" % (host, query_string)
                try:
                    code, body = self._http_get(url)
                    if code == 200:
                        return body
                    raise urllib.error.HTTPError(url, int(code), "HTTP %s" % code, {}, None)
                except urllib.error.HTTPError as e:
                    code = e.code
                    try:
                        e.close()
                    except Exception:
                        pass
                    last_error = e
                    if code == 429:
                        if attempt == 0:
                            time.sleep(self.backoff)
                            continue
                        break
                    if code >= 500:
                        break
                    raise ProviderConnectionError("Provider returned HTTP %d for spark batch" % code)
                except (urllib.error.URLError, OSError) as e:
                    last_error = e
                    break
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
            raise RateLimitError("Rate limit exceeded on provider hosts (HTTP 429). Wait and retry.")
        raise ProviderConnectionError("Could not reach data provider: %s" % last_error)

    def get_batch_series(self, symbols, range_str="3mo", interval="1d", exchange="NSE", now=None, pause=0.0):
        """Fetch batches of 20 via Yahoo Spark for scan speed, preserving partial failure isolation."""
        if len(symbols) <= 1 or type(self).get_daily_bars is not YahooMarketDataProvider.get_daily_bars or type(self).get_intraday_bars is not YahooMarketDataProvider.get_intraday_bars:
            return super().get_batch_series(symbols, range_str=range_str, interval=interval,
                                            exchange=exchange, now=now, pause=pause)
        data, failed = {}, {}
        batches = [symbols[i:i + 20] for i in range(0, len(symbols), 20)]
        exch = _normalize_exchange(exchange)
        for k, batch in enumerate(batches):
            if k > 0 and (pause or self.pause):
                time.sleep(pause if pause else self.pause)
            wanted = {}
            psym_list = []
            for s in batch:
                canon = symbols_in.normalize_symbol(s)
                psym = symbols_in.to_yahoo_symbol(canon, exchange=exch)
                wanted[psym.upper()] = canon
                psym_list.append((canon, psym))
            try:
                body = self._fetch_spark_url([p for _, p in psym_list], {"range": range_str, "interval": interval})
                doc = json.loads(body)
                sp = (doc or {}).get("spark")
                if not isinstance(sp, dict):
                    raise DataError("Unexpected Yahoo spark payload shape")
                got = {}
                for item in sp.get("result") or []:
                    resp = item.get("response") or []
                    if not resp:
                        continue
                    ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
                    canon = wanted.get(ysym.upper(), symbols_in.normalize_symbol(ysym))
                    q_ind = ((resp[0].get("indicators") or {}).get("quote") or [{}])[0] or {}
                    has_full_ohlc = all(q_ind.get(k) is not None for k in ("open", "high", "low"))
                    if has_full_ohlc:
                        single_text = json.dumps({"chart": {"result": [resp[0]], "error": None}})
                        if interval in ("1d", "d", "daily"):
                            norm = self.normalize_daily_bars(single_text, canon, exchange=exch, now=now)
                        else:
                            norm = self.normalize_intraday_bars(single_text, canon, exchange=exch, interval=interval, now=now)
                        got[canon] = to_scanner_series(norm)
                    else:
                        now_dt_spark, cutoff_ts_spark = _parse_cutoff_ts(now)
                        is_daily_spark = interval in ("1d", "d", "daily")
                        iv_secs_spark = None if is_daily_spark else (_interval_to_seconds(interval) or 300)
                        stamps = resp[0].get("timestamp") or []
                        closes_raw = q_ind.get("close") if isinstance(q_ind.get("close"), list) else []
                        vols_raw = q_ind.get("volume") if isinstance(q_ind.get("volume"), list) else None
                        t_out, c_out = [], []
                        v_out = [] if vols_raw is not None else None
                        for idx, raw_ts in enumerate(stamps):
                            if raw_ts is None:
                                continue
                            ts_int = int(raw_ts)
                            if now_dt_spark is not None:
                                if is_daily_spark:
                                    d_bar = datetime.datetime.fromtimestamp(ts_int, tz=calendar_in.IST).date()
                                    if not _is_daily_session_complete(d_bar, now_dt_spark, exchange=exch):
                                        continue
                                elif cutoff_ts_spark is not None and ts_int + iv_secs_spark > cutoff_ts_spark:
                                    continue
                            raw_c = closes_raw[idx] if idx < len(closes_raw) else None
                            t_out.append(ts_int)
                            c_out.append(float(raw_c) if raw_c is not None else None)
                            if v_out is not None:
                                raw_v = vols_raw[idx] if idx < len(vols_raw) else None
                                v_out.append(int(raw_v) if raw_v is not None else None)
                        meta = dict(resp[0].get("meta") or {})
                        non_null_c = [c for c in c_out if c is not None]
                        synth_bars = [
                            {
                                "ts": t_out[i],
                                "c": c_out[i],
                                "o": c_out[i],
                                "h": c_out[i],
                                "l": c_out[i],
                                "v": (v_out[i] if v_out is not None and v_out[i] is not None else 0),
                            }
                            for i in range(len(t_out))
                            if c_out[i] is not None
                        ]
                        if cutoff_ts_spark is not None:
                            meta = _sanitize_meta_as_of(
                                meta, synth_bars, cutoff_ts_spark,
                                interval_secs=iv_secs_spark, daily=is_daily_spark,
                            )
                        elif non_null_c and meta.get("regularMarketPrice") is None:
                            meta["regularMarketPrice"] = non_null_c[-1]
                        last_ts_spark = None
                        if synth_bars:
                            if is_daily_spark:
                                last_ts_spark = synth_bars[-1]["ts"]
                            elif cutoff_ts_spark is not None:
                                last_ts_spark = min(cutoff_ts_spark, synth_bars[-1]["ts"] + iv_secs_spark)
                            else:
                                last_ts_spark = synth_bars[-1]["ts"]
                        got[canon] = {
                            "t": t_out,
                            "c": c_out,
                            "o": None,
                            "h": None,
                            "l": None,
                            "v": v_out,
                            "meta": meta,
                            "provider": "yahoo",
                            "data_status": DataStatus.HISTORICAL,
                            "is_live": False,
                            "is_simulated": False,
                            "last_data_timestamp": _format_ist_iso(last_ts_spark),
                        }
                        if cutoff_ts_spark is not None:
                            got[canon]["simulated_at"] = _format_ist_iso(cutoff_ts_spark)
                err = sp.get("error")
                if err and not got:
                    raise DataError("Yahoo spark error: %s" % (err.get("description") or err.get("code") or err))
            except Exception as e:
                for canon, _ in psym_list:
                    failed[canon] = str(e)
                continue
            for canon, _ in psym_list:
                if canon in got:
                    data[canon] = got[canon]
                else:
                    failed[canon] = "no data from Yahoo"
        return data, failed

    def get_quote(self, symbol, exchange="NSE", now=None):
        canonical = symbols_in.normalize_symbol(symbol)
        provider_sym = symbols_in.to_yahoo_symbol(canonical, exchange=exchange)

        params = {"range": "1d", "interval": "1d"}
        text = self._fetch_chart_url(provider_sym, params)
        return self.normalize_quote(text, canonical, exchange=exchange, raw_symbol=provider_sym, now=now)

    def get_quotes(self, symbols, exchange="NSE", now=None):
        quotes = {}
        for i, s in enumerate(symbols):
            if i > 0 and self.pause:
                time.sleep(self.pause)
            canon = symbols_in.normalize_symbol(s)
            try:
                quotes[canon] = self.get_quote(s, exchange=exchange, now=now)
            except DataError as e:
                # Retain partial quotes while reporting errors per ticker
                quotes[canon] = _standardize_payload(
                    {
                        "symbol": canon,
                        "exchange": exchange,
                        "error": str(e),
                    },
                    provider_name="yahoo",
                    symbol=canon,
                    exchange=exchange,
                    data_status=DataStatus.UNAVAILABLE,
                    tier=DataTier.UNAVAILABLE,
                    timestamp=None,
                    is_live=False,
                    is_simulated=False,
                    now=now,
                )
        return quotes

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        canonical = symbols_in.normalize_symbol(symbol)
        provider_sym = symbols_in.to_yahoo_symbol(canonical, exchange=exchange)

        params = {"range": range_str, "interval": interval}
        text = self._fetch_chart_url(provider_sym, params)
        return self.normalize_intraday_bars(text, canonical, exchange=exchange, interval=interval,
                                            now=now, is_cas_eligible=is_cas_eligible)

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        canonical = symbols_in.normalize_symbol(symbol)
        provider_sym = symbols_in.to_yahoo_symbol(canonical, exchange=exchange)

        params = {"range": range_str, "interval": "1d"}
        text = self._fetch_chart_url(provider_sym, params)
        return self.normalize_daily_bars(text, canonical, exchange=exchange, now=now)

    def normalize_quote(self, text, symbol, exchange="NSE", raw_symbol=None, now=None):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise DataError("Malformed response JSON for %s: %s" % (symbol, e))

        chart = doc.get("chart") or {}
        if chart.get("error"):
            err = chart["error"]
            raise SymbolNotFoundError("Provider error for %s: %s" % (symbol, err.get("description", err)))

        results = chart.get("result") or []
        if not results:
            raise MissingDataError("Empty result payload for %s" % symbol)

        res = results[0]
        meta = dict(res.get("meta") or {})
        stamps = res.get("timestamp") or []
        q_ind = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}

        now_dt, cutoff_ts = _parse_cutoff_ts(now)
        rmt = meta.get("regularMarketTime")
        rmt_int = int(rmt) if rmt is not None else None
        exch = _normalize_exchange(exchange)

        price = None
        open_px = meta.get("regularMarketOpen")
        high_px = meta.get("regularMarketDayHigh")
        low_px = meta.get("regularMarketDayLow")
        vol_val = meta.get("regularMarketVolume")

        if cutoff_ts is not None:
            data_gran = meta.get("dataGranularity")
            iv_secs = _interval_to_seconds(data_gran) if data_gran else None
            if iv_secs is not None and iv_secs < 86400 and stamps:
                cols = [q_ind.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
                valid_bars = []
                for idx, ts in enumerate(stamps):
                    if ts is None:
                        continue
                    ts_int = int(ts)
                    if ts_int + iv_secs > cutoff_ts:
                        continue
                    o, h, l, c, v = [col[idx] if idx < len(col) else None for col in cols]
                    if None in (o, h, l, c):
                        continue
                    dt_bar = datetime.datetime.fromtimestamp(ts_int, tz=calendar_in.IST)
                    valid_bars.append({
                        "date": dt_bar.strftime("%Y-%m-%d"),
                        "o": float(o),
                        "h": float(h),
                        "l": float(l),
                        "c": float(c),
                        "v": int(v or 0),
                        "ts": ts_int,
                    })
                if not valid_bars:
                    raise MissingDataError(
                        "Insufficient quote data before simulated cutoff %s for %s"
                        % (_format_ist(cutoff_ts), symbol)
                    )
                if rmt_int is None or rmt_int > cutoff_ts:
                    last_date = valid_bars[-1]["date"]
                    day_bars = [b for b in valid_bars if b["date"] == last_date] or [valid_bars[-1]]
                    price = day_bars[-1]["c"]
                    open_px = day_bars[0]["o"]
                    high_px = max(b["h"] for b in day_bars)
                    low_px = min(b["l"] for b in day_bars)
                    vol_val = sum(b["v"] for b in day_bars)
                    rmt_int = min(cutoff_ts, valid_bars[-1]["ts"] + iv_secs)
            else:
                if rmt_int is not None:
                    if rmt_int > cutoff_ts:
                        raise MissingDataError(
                            "Quote timestamp %s is after simulated cutoff %s for %s"
                            % (_format_ist(rmt_int), _format_ist(cutoff_ts), symbol)
                        )
                elif stamps:
                    valid_ts = [int(t) for t in stamps if t is not None]
                    if not valid_ts or max(valid_ts) > cutoff_ts:
                        raise MissingDataError(
                            "Quote timestamp is after simulated cutoff %s for %s"
                            % (_format_ist(cutoff_ts), symbol)
                        )
                    last_d = datetime.datetime.fromtimestamp(valid_ts[-1], tz=calendar_in.IST).date()
                    if not _is_daily_session_complete(last_d, now_dt, exchange=exch):
                        raise MissingDataError(
                            "Daily quote session %s is incomplete at simulated cutoff %s for %s"
                            % (last_d.isoformat(), _format_ist(cutoff_ts), symbol)
                        )
                else:
                    raise MissingDataError(
                        "Quote has no timestamp before simulated cutoff %s for %s"
                        % (_format_ist(cutoff_ts), symbol)
                    )

        if price is None:
            price = meta.get("regularMarketPrice")
        if price is None:
            # Fallback to last close in indicators
            closes = [c for c in (q_ind.get("close") or []) if c is not None]
            if closes:
                price = closes[-1]

        if price is None:
            raise MissingDataError("No current or closing price available for %s" % symbol)

        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        change = None
        change_pct = None
        if prev_close and prev_close > 0:
            change = round(price - prev_close, 2)
            change_pct = round((price / prev_close - 1) * 100.0, 3)

        time_str = _format_ist(rmt_int) if rmt_int else ""
        payload = {
            "symbol": symbol,
            "raw_symbol": raw_symbol or symbol,
            "exchange": exch,
            "currency": meta.get("currency", "INR"),
            "price": float(price),
            "ltp": float(price),
            "close": float(price),
            "prev_close": float(prev_close) if prev_close else None,
            "change": change,
            "change_pct": change_pct,
            "open": float(open_px) if open_px is not None else None,
            "high": float(high_px) if high_px is not None else None,
            "low": float(low_px) if low_px is not None else None,
            "volume": int(vol_val) if vol_val is not None else None,
            "timestamp": rmt_int,
            "time": time_str,
            "source": "yahoo",
            "tier": DataTier.DELAYED,
        }
        return _standardize_payload(
            payload,
            provider_name="yahoo",
            symbol=symbol,
            exchange=exch,
            data_status=DataStatus.DELAYED,
            tier=DataTier.DELAYED,
            timestamp=rmt_int,
            is_live=False,
            is_simulated=False,
            now=now,
        )

    def normalize_intraday_bars(self, text, symbol, exchange="NSE", interval="5m", now=None, is_cas_eligible=False):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise DataError("Malformed response JSON for %s: %s" % (symbol, e))

        chart = doc.get("chart") or {}
        if chart.get("error"):
            err = chart["error"]
            raise SymbolNotFoundError("Provider error for %s: %s" % (symbol, err.get("description", err)))

        results = chart.get("result") or []
        if not results:
            raise MissingDataError("Empty result payload for %s" % symbol)

        res = results[0]
        meta = res.get("meta") or {}
        stamps = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
        cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]

        now_dt = calendar_in.parse_ist(now) if now else calendar_in.now_ist()
        now_ts = int(now_dt.timestamp())

        int_match = re.match(r"^(\d+)([smhd])$", interval)
        if not int_match:
            raise DataError("Unsupported interval: %s" % interval)
        val, unit = int(int_match.group(1)), int_match.group(2)
        unit_multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        interval_secs = val * unit_multiplier
        interval_mins = interval_secs // 60

        dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
        bars = []

        for i, ts in enumerate(stamps):
            if ts is None:
                dropped["no_price"] += 1
                continue
            ts = int(ts)
            o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
            if None in (o, h, l, c):
                dropped["no_price"] += 1
                continue

            if ts % interval_secs != 0:
                dropped["off_grid"] += 1
                continue

            if ts + interval_secs > now_ts:
                dropped["not_complete"] += 1
                continue

            dt_ist = datetime.datetime.fromtimestamp(ts, tz=calendar_in.IST)
            date_str = dt_ist.strftime("%Y-%m-%d")
            time_mins = dt_ist.hour * 60 + dt_ist.minute

            # Check continuous trading boundary
            open_min = calendar_in.NORMAL_OPEN_MIN
            rule_ver = calendar_in.get_cas_rule_version(date_str, exchange=exchange, is_cas_eligible=is_cas_eligible)
            if rule_ver == calendar_in.CASRuleVersion.CAS_V1:
                close_min = calendar_in.CAS_V1_CLOSE_MIN
            else:
                close_min = calendar_in.NORMAL_CLOSE_MIN

            if time_mins < open_min or time_mins >= close_min:
                dropped["outside_hours"] += 1
                continue

            slot = (time_mins - open_min) // max(1, interval_mins)
            bars.append({
                "date": date_str,
                "slot": slot,
                "start": dt_ist.strftime("%H:%M"),
                "ist_time": dt_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
                "o": round(float(o), 2),
                "h": round(float(h), 2),
                "l": round(float(l), 2),
                "c": round(float(c), 2),
                "v": int(v or 0),
                "ts": ts,
            })

        exch = _normalize_exchange(exchange)
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        cutoff_param = now_ts if now is not None else None
        meta = _sanitize_meta_as_of(
            meta, bars, cutoff_param if cutoff_param is not None else now_ts,
            interval_secs=interval_secs, daily=False,
        )
        if bars:
            last_ts = min(now_ts, bars[-1]["ts"] + interval_secs)
        else:
            last_ts = None
        payload = {
            "symbol": symbol,
            "exchange": exch,
            "interval": interval,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "tier": DataTier.HISTORICAL,
            "source": "yahoo",
            "bars": bars,
            "dropped": dropped,
            "meta": meta,
        }
        return _standardize_payload(
            payload,
            provider_name="yahoo",
            symbol=symbol,
            exchange=exch,
            data_status=DataStatus.HISTORICAL,
            tier=DataTier.HISTORICAL,
            timestamp=int(last_ts) if last_ts else None,
            is_live=False,
            is_simulated=False,
            now=now,
        )

    def normalize_daily_bars(self, text, symbol, exchange="NSE", now=None):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise DataError("Malformed response JSON for %s: %s" % (symbol, e))

        chart = doc.get("chart") or {}
        if chart.get("error"):
            err = chart["error"]
            raise SymbolNotFoundError("Provider error for %s: %s" % (symbol, err.get("description", err)))

        results = chart.get("result") or []
        if not results:
            raise MissingDataError("Empty result payload for %s" % symbol)

        res = results[0]
        meta = res.get("meta") or {}
        stamps = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
        cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]

        now_dt, now_ts = _parse_cutoff_ts(now) if now is not None else (
            calendar_in.now_ist(), int(calendar_in.now_ist().timestamp())
        )
        exch = _normalize_exchange(exchange)

        bars_by_date = {}
        for i, ts in enumerate(stamps):
            if ts is None:
                continue
            ts = int(ts)
            o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
            if None in (o, h, l, c):
                continue
            dt_ist = datetime.datetime.fromtimestamp(ts, tz=calendar_in.IST)
            d = dt_ist.date()
            if not _is_daily_session_complete(d, now_dt, exchange=exch):
                continue
            # Store completed daily bar
            bars_by_date[d] = {
                "date": d.isoformat(),
                "ist_time": dt_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
                "o": round(float(o), 2),
                "h": round(float(h), 2),
                "l": round(float(l), 2),
                "c": round(float(c), 2),
                "v": int(v or 0),
                "ts": ts,
            }

        sorted_bars = [bars_by_date[k] for k in sorted(bars_by_date.keys())]
        meta = _sanitize_meta_as_of(meta, sorted_bars, now_ts, daily=True)
        last_ts = min(now_ts, sorted_bars[-1]["ts"]) if sorted_bars else None
        payload = {
            "symbol": symbol,
            "exchange": exch,
            "interval": "1d",
            "tier": DataTier.HISTORICAL,
            "source": "yahoo",
            "bars": sorted_bars,
            "meta": meta,
        }
        return _standardize_payload(
            payload,
            provider_name="yahoo",
            symbol=symbol,
            exchange=exch,
            data_status=DataStatus.HISTORICAL,
            tier=DataTier.HISTORICAL,
            timestamp=int(last_ts) if last_ts else None,
            is_live=False,
            is_simulated=False,
            now=now,
        )

    def get_instruments(self, exchange="NSE", symbol=None):
        """Report capability limitation for remote instrument master download."""
        raise NotSupportedError(
            "Yahoo Finance does not provide official Indian equity or derivatives instrument masters. "
            "Use local instrument cache (instruments.py) or a broker adapter.",
            capability=CAPABILITY_INSTRUMENT_MASTERS,
            provider="yahoo",
        )

    def get_option_chain(self, symbol, expiry=None, exchange="NSE", spot_price=None):
        """Fetch option chain for Indian derivatives.

        Identifies provider capability limitation: Yahoo Finance does not provide
        reliable real-time option chains with open interest and Greeks for Indian equities/indices.
        """
        raise NotSupportedError(
            "Yahoo Finance does not provide reliable Indian options chains with live OI and Greeks. "
            "Use SimulatedMarketDataProvider (simulated tier) or official broker adapters for options trading.",
            capability=CAPABILITY_OPTION_CHAINS,
            provider="yahoo",
        )

    def get_derivatives_master(self, exchange="NSE"):
        """Fetch derivatives instrument master."""
        raise NotSupportedError(
            "Yahoo Finance does not provide Indian derivatives instrument master. "
            "Use authoritative NSE/BSE metadata files or local instrument cache.",
            capability=CAPABILITY_INSTRUMENT_MASTERS,
            provider="yahoo",
        )


class OfflineMarketDataProvider(MarketDataProvider):
    """Offline market data provider for tests, air-gapped environments, and replay.

    Reads saved JSON or JSON.GZ chart responses from a local folder or file mapping.
    """

    provider_name = "offline"
    requires_auth = False
    required_credentials = ()
    CAPABILITIES = {
        CAPABILITY_HISTORICAL_CANDLES: True,
        CAPABILITY_INTRADAY_CANDLES: True,
        CAPABILITY_LIVE_QUOTES: False,
        CAPABILITY_STREAMING_DATA: False,
        CAPABILITY_OPTION_CHAINS: True,
        CAPABILITY_OPEN_INTEREST: False,
        CAPABILITY_INSTRUMENT_MASTERS: True,
        CAPABILITY_MARKET_STATUS: True,
    }

    def __init__(self, base_path=None, tier=DataTier.HISTORICAL, config=None, env=None):
        cfg = config if isinstance(config, dict) else {}
        env_map = env if env is not None else os.environ
        resolved = base_path
        if resolved is None:
            for cand in (
                env_map.get("INDIA_OFFLINE_DIR"),
                cfg.get("offline_dir"),
                cfg.get("offline_path"),
                cfg.get("base_path"),
            ):
                if cand:
                    resolved = cand
                    break
        if resolved is None:
            pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for repo_fixtures in (
                os.path.join(pkg_root, "tests", "fixtures", "bharat"),
                os.path.join(pkg_root, "fixtures", "bharat"),
            ):
                if os.path.isdir(repo_fixtures):
                    resolved = repo_fixtures
                    break
        if not resolved or not os.path.exists(str(resolved)):
            raise ProviderConfigError(
                "Offline market data provider requires an existing base_path, offline_dir, "
                "or INDIA_OFFLINE_DIR directory; got %r" % (resolved,)
            )
        self.base_path = str(resolved)
        self.tier = tier
        self.parser = YahooMarketDataProvider()

    def _read_file(self, path):
        if not os.path.exists(path):
            raise SymbolNotFoundError("Offline file not found: %s" % path)
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()

    def _locate_file(self, symbol, suffix):
        if os.path.isfile(self.base_path):
            return self.base_path
        cand_names = (
            "%s-%s.json" % (symbol, suffix),
            "%s-%s.json.gz" % (symbol, suffix),
            "%s.json" % symbol,
            "%s.json.gz" % symbol,
        )
        for name in cand_names:
            p = os.path.join(self.base_path, name)
            if os.path.exists(p):
                return p
        raise SymbolNotFoundError("No offline chart file found for %s in %s" % (symbol, self.base_path))

    def get_quote(self, symbol, exchange="NSE", now=None):
        canonical = symbols_in.normalize_symbol(symbol)
        exch = _normalize_exchange(exchange)
        path = self._locate_file(canonical, "quote")
        text = self._read_file(path)
        res = self.parser.normalize_quote(text, canonical, exchange=exch, now=now)
        res["source"] = "offline"
        res["tier"] = self.tier
        return _standardize_payload(
            res,
            provider_name="offline",
            symbol=canonical,
            exchange=exch,
            data_status=DataStatus.FIXTURE,
            tier=self.tier,
            timestamp=res.get("timestamp"),
            is_live=False,
            is_simulated=False,
            now=now,
        )

    def get_quotes(self, symbols, exchange="NSE", now=None):
        exch = _normalize_exchange(exchange)
        quotes = {}
        for s in symbols:
            canon = symbols_in.normalize_symbol(s)
            try:
                quotes[canon] = self.get_quote(s, exchange=exch, now=now)
            except DataError as e:
                quotes[canon] = _standardize_payload(
                    {
                        "symbol": canon,
                        "exchange": exch,
                        "error": str(e),
                    },
                    provider_name="offline",
                    symbol=canon,
                    exchange=exch,
                    data_status=DataStatus.UNAVAILABLE,
                    tier=DataTier.UNAVAILABLE,
                    timestamp=None,
                    is_live=False,
                    is_simulated=False,
                    now=now,
                )
        return quotes

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        canonical = symbols_in.normalize_symbol(symbol)
        exch = _normalize_exchange(exchange)
        suffix = interval
        path = self._locate_file(canonical, suffix)
        text = self._read_file(path)
        res = self.parser.normalize_intraday_bars(text, canonical, exchange=exch, interval=interval,
                                                  now=now, is_cas_eligible=is_cas_eligible)
        res["source"] = "offline"
        res["tier"] = self.tier
        return _standardize_payload(
            res,
            provider_name="offline",
            symbol=canonical,
            exchange=exch,
            data_status=DataStatus.FIXTURE,
            tier=self.tier,
            timestamp=res.get("timestamp"),
            is_live=False,
            is_simulated=False,
            now=now,
        )

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        canonical = symbols_in.normalize_symbol(symbol)
        exch = _normalize_exchange(exchange)
        path = self._locate_file(canonical, "daily")
        text = self._read_file(path)
        res = self.parser.normalize_daily_bars(text, canonical, exchange=exch, now=now)
        res["source"] = "offline"
        res["tier"] = self.tier
        return _standardize_payload(
            res,
            provider_name="offline",
            symbol=canonical,
            exchange=exch,
            data_status=DataStatus.FIXTURE,
            tier=self.tier,
            timestamp=res.get("timestamp"),
            is_live=False,
            is_simulated=False,
            now=now,
        )

    def get_instruments(self, exchange="NSE", symbol=None):
        exch = _normalize_exchange(exchange)
        items = []
        raw_items = None
        if os.path.isdir(self.base_path):
            for fname in ("instruments.json", "instruments-%s.json" % exch.lower(), "nse_instruments.json"):
                cand = os.path.join(self.base_path, fname)
                if os.path.exists(cand):
                    try:
                        doc = json.loads(self._read_file(cand))
                    except ValueError as e:
                        raise DataError("Malformed instrument JSON in %s: %s" % (cand, e))
                    if isinstance(doc, dict):
                        if isinstance(doc.get("instruments"), list):
                            try:
                                flat_list = [
                                    instruments._normalize_cache_record_tick(
                                        r,
                                        cache_payload=doc,
                                        allow_none=True,
                                        sym_hint=(r.get("symbol") or r.get("canonical_symbol")),
                                    )
                                    for r in doc["instruments"]
                                    if isinstance(r, dict)
                                ]
                                by_exch_raw = doc.get("instruments_by_exchange")
                                if "instruments_by_exchange" in doc and by_exch_raw is not None:
                                    if not isinstance(by_exch_raw, dict):
                                        raise instruments.InstrumentMetadataError(
                                            "Malformed instrument cache: 'instruments_by_exchange' must be a dict"
                                        )
                                    ex_bucket = by_exch_raw.get(exch)
                                    if ex_bucket is None:
                                        raw_items = []
                                    elif isinstance(ex_bucket, dict):
                                        raw_items = [
                                            instruments._normalize_cache_record_tick(
                                                r, cache_payload=doc, allow_none=True, sym_hint=k
                                            )
                                            for k, r in ex_bucket.items()
                                        ]
                                    elif isinstance(ex_bucket, list):
                                        raw_items = [
                                            instruments._normalize_cache_record_tick(
                                                r,
                                                cache_payload=doc,
                                                allow_none=True,
                                                sym_hint=(r.get("symbol") or r.get("canonical_symbol")),
                                            )
                                            for r in ex_bucket
                                            if isinstance(r, dict)
                                        ]
                                    else:
                                        raise instruments.InstrumentMetadataError(
                                            "Malformed instrument cache: exchange bucket %r must be a dict" % (exch,)
                                        )
                                else:
                                    raw_items = flat_list
                            except instruments.InstrumentMetadataError as e:
                                raise DataError(str(e))
                        else:
                            try:
                                norm_doc = instruments.normalize_instrument_cache_payload(doc, allow_none_tick=True)
                            except instruments.InstrumentMetadataError as e:
                                raise DataError(str(e))
                            by_exch = norm_doc.get("instruments_by_exchange")
                            if isinstance(by_exch, dict):
                                ex_bucket = by_exch.get(exch)
                                raw_items = list(ex_bucket.values()) if isinstance(ex_bucket, dict) else []
                            else:
                                flat = norm_doc.get("instruments") or {}
                                raw_items = list(flat.values()) if isinstance(flat, dict) else flat
                    elif isinstance(doc, list):
                        try:
                            raw_items = [
                                instruments._normalize_cache_record_tick(r, cache_payload=None, allow_none=True)
                                for r in doc
                                if isinstance(r, dict)
                            ]
                        except instruments.InstrumentMetadataError as e:
                            raise DataError(str(e))
                    else:
                        raise DataError("Malformed instrument JSON root in %s" % cand)
                    break
        if raw_items is None and os.path.isdir(self.base_path):
            for fname in ("EQUITY_L.csv", "EQUITY_L_sample.csv"):
                cand = os.path.join(self.base_path, fname)
                if os.path.exists(cand):
                    try:
                        parsed = instruments.parse_equity_l_csv(self._read_file(cand), exchange=exch)
                        raw_items = list(parsed.values()) if isinstance(parsed, dict) else parsed
                    except Exception:
                        raw_items = None
                    break
        if raw_items is None and os.path.exists(instruments.DEFAULT_CACHE_PATH):
            try:
                cache_payload = instruments.load_instrument_cache(max_age_days=3650)
            except (FileNotFoundError, instruments.StaleMetadataError):
                cache_payload = None
            except instruments.InstrumentMetadataError as e:
                raise DataError(str(e))
            if cache_payload:
                by_exch = cache_payload.get("instruments_by_exchange")
                if isinstance(by_exch, dict):
                    ex_bucket = by_exch.get(exch)
                    raw_items = list(ex_bucket.values()) if isinstance(ex_bucket, dict) else []
                elif isinstance(cache_payload.get("instruments"), dict):
                    raw_items = list(cache_payload["instruments"].values())
        if raw_items is None:
            raw_items = list(instruments.INDEX_INSTRUMENTS.values())
        target_sym = symbols_in.normalize_symbol(symbol) if symbol else None
        for rec in (raw_items or []):
            if not isinstance(rec, dict):
                continue
            sym = symbols_in.normalize_symbol(rec.get("symbol") or rec.get("canonical_symbol") or "")
            raw_rec_exch = rec.get("exchange")
            if raw_rec_exch:
                rec_exch = _normalize_exchange(raw_rec_exch)
                if rec_exch != exch:
                    continue
            else:
                rec_exch = exch
            if target_sym and sym != target_sym:
                continue
            try:
                norm_rec = instruments._normalize_cache_record_tick(rec, cache_payload=None, allow_none=True, sym_hint=sym)
            except instruments.InstrumentMetadataError as e:
                raise DataError(str(e))
            tick_inr, tick_status = _normalize_provider_tick_size(
                norm_rec.get("tick_size"),
                source_unit=norm_rec["tick_size_unit"],
                segment=norm_rec.get("segment") or "",
                instrument_type=norm_rec.get("asset_type") or norm_rec.get("instrument_type") or "",
            )
            items.append({
                "symbol": sym,
                "exchange": rec_exch,
                "instrument_id": "%s:%s" % (rec_exch, sym),
                "name": rec.get("name", ""),
                "asset_type": rec.get("asset_type", "EQUITY"),
                "lot_size": int(rec.get("lot_size") or 1),
                "tick_size": tick_inr,
                "tick_size_unit": "INR",
                "tick_size_status": tick_status,
                "isin": rec.get("isin"),
            })
        return _standardize_payload(
            {
                "symbol": target_sym or exch,
                "exchange": exch,
                "instruments": items,
                "count": len(items),
                "source": "offline",
                "tier": self.tier,
            },
            provider_name="offline",
            symbol=target_sym or exch,
            exchange=exch,
            data_status=DataStatus.FIXTURE,
            tier=self.tier,
            timestamp=None,
            is_live=False,
            is_simulated=False,
        )

    def get_option_chain(self, symbol, expiry=None, exchange="NSE", spot_price=None):
        canonical = symbols_in.normalize_symbol(symbol)
        exch = _normalize_exchange(exchange)
        try:
            path = self._locate_file(canonical, "chain")
        except SymbolNotFoundError:
            try:
                path = self._locate_file(canonical, "options")
            except SymbolNotFoundError:
                path = self._locate_file(canonical, "option-chain")
        text = self._read_file(path)
        try:
            data = json.loads(text)
        except ValueError as e:
            raise DataError("Malformed option chain JSON in %s: %s" % (path, e))
        if isinstance(data, dict):
            data["source"] = "offline"
            data["tier"] = self.tier
            data = _standardize_payload(
                data,
                provider_name="offline",
                symbol=canonical,
                exchange=exch,
                data_status=DataStatus.FIXTURE,
                tier=self.tier,
                timestamp=data.get("timestamp"),
                is_live=False,
                is_simulated=False,
            )
        return data

    def get_derivatives_master(self, exchange="NSE"):
        try:
            path = self._locate_file("derivatives", "master")
        except SymbolNotFoundError:
            path = self._locate_file("fo_mktlots", "csv")
        return self._read_file(path)


# -----------------------------------------------------------------------------
# Options Pricing and Greeks Engine (Black-Scholes-Merton)
# -----------------------------------------------------------------------------

def norm_cdf(x):
    """Cumulative standard normal distribution function using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x):
    """Standard normal probability density function."""
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)


def black_scholes(spot, strike, t_years, rate=0.07, vol=0.18, option_type="CE"):
    """Compute European option price and analytical Greeks.

    - spot: Current price of underlying asset (S)
    - strike: Option strike price (K)
    - t_years: Time to expiry in years (e.g. 15/365.0)
    - rate: Annualized risk-free interest rate (e.g. 0.07 for 7%)
    - vol: Annualized implied volatility (e.g. 0.18 for 18%)
    - option_type: 'CE' for Call or 'PE' for Put

    Returns dict with keys:
    - price: Option theoretical premium
    - delta: Sensitivity to underlying price (+ for CE, - for PE)
    - gamma: Rate of change of delta per point move in underlying
    - theta: Daily time decay in premium (Rs / day)
    - vega: Sensitivity to 1% change in implied volatility
    - rho: Sensitivity to 1% change in interest rate
    """
    ot = str(option_type).upper().strip()
    s = float(spot)
    k = float(strike)
    t = max(float(t_years), 0.0)
    r = float(rate)
    v = max(float(vol), 0.001)

    if t <= 0.0001:
        # At or past expiry
        if ot == "CE":
            intrinsic = max(0.0, s - k)
            delta = 1.0 if s > k else 0.0
        else:
            intrinsic = max(0.0, k - s)
            delta = -1.0 if k > s else 0.0
        return {
            "price": max(0.05, round(intrinsic, 2)),
            "delta": round(delta, 4),
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "rho": 0.0,
        }

    v_sqrt_t = v * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * v * v) * t) / v_sqrt_t
    d2 = d1 - v_sqrt_t
    exp_rt = math.exp(-r * t)
    pdf_d1 = norm_pdf(d1)

    gamma = pdf_d1 / (s * v_sqrt_t)
    vega = (s * pdf_d1 * math.sqrt(t)) / 100.0  # per 1% vol

    if ot == "CE":
        price = s * norm_cdf(d1) - k * exp_rt * norm_cdf(d2)
        delta = norm_cdf(d1)
        theta = (- (s * pdf_d1 * v) / (2.0 * math.sqrt(t)) - r * k * exp_rt * norm_cdf(d2)) / 365.0
        rho = (k * t * exp_rt * norm_cdf(d2)) / 100.0
    else:
        price = k * exp_rt * norm_cdf(-d2) - s * norm_cdf(-d1)
        delta = norm_cdf(d1) - 1.0
        theta = (- (s * pdf_d1 * v) / (2.0 * math.sqrt(t)) + r * k * exp_rt * norm_cdf(-d2)) / 365.0
        rho = (-k * t * exp_rt * norm_cdf(-d2)) / 100.0

    return {
        "price": max(0.05, round(price, 2)),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 2),
        "vega": round(vega, 2),
        "rho": round(rho, 2),
    }


def calculate_iv(market_price, spot, strike, t_years, rate=0.07, option_type="CE", is_call=None):
    """Compute implied volatility using bisection solver."""
    target = float(market_price)
    if is_call is not None:
        ot = "CE" if is_call else "PE"
    else:
        ot = str(option_type).upper().strip()
    low_v = 0.01
    high_v = 4.0

    intrinsic = max(0.0, spot - strike) if ot == "CE" else max(0.0, strike - spot)
    if target <= intrinsic:
        return 0.05

    for _ in range(50):
        mid_v = (low_v + high_v) / 2.0
        calc = black_scholes(spot, strike, t_years, rate=rate, vol=mid_v, option_type=ot)["price"]
        diff = calc - target
        if abs(diff) < 0.01 or (high_v - low_v) < 0.001:
            return round(mid_v, 4)
        if diff > 0:
            high_v = mid_v
        else:
            low_v = mid_v
    return round(mid_v, 4)


class OptionChain:
    """Structured representation of an Indian equity or index option chain."""

    def __init__(self, underlying, underlying_price, expiry, strikes_data,
                 data_tier=DataTier.DELAYED, timestamp=None, exchange="NSE"):
        self.underlying = underlying
        self.underlying_price = float(underlying_price)
        self.expiry = str(expiry)
        self.data_tier = data_tier
        self.timestamp = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.exchange = exchange
        self.strikes_data = strikes_data  # dict of strike -> {'ce': ..., 'pe': ...}
        self.sorted_strikes = sorted(self.strikes_data.keys())

    @property
    def atm_strike(self):
        """Return the strike closest to the current underlying price."""
        if not self.sorted_strikes:
            return None
        return min(self.sorted_strikes, key=lambda k: abs(k - self.underlying_price))

    def get_strikes(self):
        return list(self.sorted_strikes)

    def get_contract(self, strike, option_type="CE"):
        k = float(strike)
        entry = self.strikes_data.get(k)
        if not entry:
            return None
        ot = option_type.lower()
        return entry.get(ot)

    def select_strike(self, strategy="ATM", option_type="CE", offset=0):
        """Select strike based on strategy: ATM, ITM1, ITM2, OTM1, OTM2, or offset."""
        atm = self.atm_strike
        if atm is None:
            return None
        idx = self.sorted_strikes.index(atm)
        strat = str(strategy).upper().strip()
        ot = str(option_type).upper().strip()

        if strat == "ATM":
            eff_offset = 0
        elif strat == "ITM1":
            eff_offset = -1 if ot == "CE" else 1
        elif strat == "ITM2":
            eff_offset = -2 if ot == "CE" else 2
        elif strat == "OTM1":
            eff_offset = 1 if ot == "CE" else -1
        elif strat == "OTM2":
            eff_offset = 2 if ot == "CE" else -2
        else:
            eff_offset = offset

        target_idx = max(0, min(len(self.sorted_strikes) - 1, idx + eff_offset))
        return self.sorted_strikes[target_idx]

    @property
    def pcr(self):
        """Calculate Put-Call Ratio (Total PE OI / Total CE OI)."""
        ce_oi = sum(s.get("ce", {}).get("open_interest", 0) for s in self.strikes_data.values())
        pe_oi = sum(s.get("pe", {}).get("open_interest", 0) for s in self.strikes_data.values())
        if ce_oi <= 0:
            return 1.0
        return round(float(pe_oi) / float(ce_oi), 3)

    def to_dict(self):
        is_sim = self.data_tier == DataTier.SIMULATED
        status = DataStatus.SIMULATED if is_sim else (
            DataStatus.FIXTURE if self.data_tier == DataTier.FIXTURE else DataStatus.DELAYED
        )
        return _standardize_payload(
            {
                "underlying": self.underlying,
                "underlying_price": self.underlying_price,
                "expiry": self.expiry,
                "exchange": self.exchange,
                "tier": self.data_tier,
                "timestamp": self.timestamp,
                "atm_strike": self.atm_strike,
                "pcr": self.pcr,
                "strikes": [
                    {
                        "strike": k,
                        "ce": self.strikes_data[k].get("ce"),
                        "pe": self.strikes_data[k].get("pe"),
                    }
                    for k in self.sorted_strikes
                ],
            },
            provider_name="simulated" if is_sim else "unknown",
            symbol=self.underlying,
            exchange=self.exchange,
            data_status=status,
            tier=self.data_tier,
            timestamp=None,
            is_live=False,
            is_simulated=is_sim,
        )


class SimulatedMarketDataProvider(MarketDataProvider):
    """Generates synthetic, fully consistent Indian market data and options chains.

    Useful for paper trading, scenario replay, and testing derivatives strategies.
    Explicitly labeled with DataTier.SIMULATED.
    """

    provider_name = "simulated"
    requires_auth = False
    required_credentials = ()
    CAPABILITIES = {
        CAPABILITY_HISTORICAL_CANDLES: True,
        CAPABILITY_INTRADAY_CANDLES: True,
        CAPABILITY_LIVE_QUOTES: False,
        CAPABILITY_STREAMING_DATA: False,
        CAPABILITY_OPTION_CHAINS: True,
        CAPABILITY_OPEN_INTEREST: False,
        CAPABILITY_INSTRUMENT_MASTERS: False,
        CAPABILITY_MARKET_STATUS: True,
    }

    def __init__(self, base_provider=None, default_iv=0.18, rate=0.07):
        self.base_provider = base_provider or YahooMarketDataProvider()
        self.default_iv = default_iv
        self.rate = rate

    def supports(self, capability):
        if capability == CAPABILITY_OPTION_CHAINS:
            return True
        if capability in (CAPABILITY_HISTORICAL_CANDLES, CAPABILITY_INTRADAY_CANDLES, CAPABILITY_INSTRUMENT_MASTERS):
            return self.base_provider.supports(capability)
        return bool(self.CAPABILITIES.get(capability, False))

    def get_quote(self, symbol, exchange="NSE", now=None):
        # If symbol is a derivative contract, resolve price using Black-Scholes
        if symbols_in.is_derivative_symbol(symbol):
            parsed = symbols_in.parse_derivative_symbol(symbol)
            underlying = parsed["underlying"]
            try:
                und_q = _validate_quote_as_of(
                    _call_with_optional_now(self.base_provider.get_quote, underlying, exchange=exchange, now=now),
                    now,
                    symbol=underlying,
                )
                spot = und_q["price"]
            except Exception:
                spot = 24000.0 if underlying in ("NIFTY", "^NSEI") else 2800.0

            now_dt, cutoff_ts = _parse_cutoff_ts(now) if now is not None else (
                calendar_in.now_ist(), int(calendar_in.now_ist().timestamp())
            )
            now_ts = cutoff_ts
            if parsed["asset_type"] == "FUTURES":
                price = round(spot * (1.0 + self.rate * (15.0 / 365.0)), 2)
                return _standardize_payload(
                    {
                        "symbol": parsed["canonical_symbol"],
                        "exchange": exchange,
                        "price": price,
                        "ltp": price,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "tier": DataTier.SIMULATED,
                        "source": "simulated",
                    },
                    provider_name="simulated",
                    symbol=parsed["canonical_symbol"],
                    exchange=exchange,
                    data_status=DataStatus.SIMULATED,
                    tier=DataTier.SIMULATED,
                    timestamp=now_ts,
                    is_live=False,
                    is_simulated=True,
                    now=now,
                )
            else:
                strike = parsed["strike"]
                opt_type = parsed["option_type"]
                exp_dt = calendar_in.get_next_expiry(
                    as_of_date=now_dt, underlying=underlying, exchange=exchange
                )
                days = max(1, calendar_in.days_to_expiry(exp_dt, as_of_date=now_dt, exchange=exchange))
                t_years = days / 365.0
                bs = black_scholes(spot, strike, t_years, rate=self.rate, vol=self.default_iv, option_type=opt_type)
                return _standardize_payload(
                    {
                        "symbol": parsed["canonical_symbol"],
                        "exchange": exchange,
                        "price": bs["price"],
                        "ltp": bs["price"],
                        "strike": strike,
                        "option_type": opt_type,
                        "expiry": exp_dt.strftime("%Y-%m-%d"),
                        "greeks": bs,
                        "tier": DataTier.SIMULATED,
                        "source": "simulated",
                    },
                    provider_name="simulated",
                    symbol=parsed["canonical_symbol"],
                    exchange=exchange,
                    data_status=DataStatus.SIMULATED,
                    tier=DataTier.SIMULATED,
                    timestamp=now_ts,
                    is_live=False,
                    is_simulated=True,
                    now=now,
                )

        return _validate_quote_as_of(
            _call_with_optional_now(self.base_provider.get_quote, symbol, exchange=exchange, now=now),
            now,
            symbol=symbol,
        )

    def get_quotes(self, symbols, exchange="NSE", now=None):
        return {symbols_in.normalize_symbol(s): self.get_quote(s, exchange=exchange, now=now) for s in symbols}

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        return self.base_provider.get_intraday_bars(symbol, interval=interval, range_str=range_str,
                                                    exchange=exchange, now=now, is_cas_eligible=is_cas_eligible)

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        return self.base_provider.get_daily_bars(symbol, range_str=range_str, exchange=exchange, now=now)

    def get_instruments(self, exchange="NSE", symbol=None):
        return self.base_provider.get_instruments(exchange=exchange, symbol=symbol)

    def get_option_chain(self, symbol, expiry=None, exchange="NSE", spot_price=None):
        canonical = symbols_in.normalize_symbol(symbol)
        if spot_price is not None:
            spot = float(spot_price)
        else:
            try:
                q = self.base_provider.get_quote(canonical, exchange=exchange)
                spot = float(q["price"])
            except Exception:
                spot = 24000.0 if canonical in ("NIFTY", "^NSEI") else 2800.0

        exp_dt = calendar_in.get_next_expiry(expiry or datetime.date.today(), underlying=canonical, exchange=exchange)
        days = max(1, calendar_in.days_to_expiry(exp_dt))
        t_years = days / 365.0

        step = instruments.get_strike_interval(canonical, spot_price=spot)
        base_atm = round(spot / step) * step

        strikes_data = {}
        for i in range(-10, 11):
            k = round(base_atm + i * step, 2)
            ce_bs = black_scholes(spot, k, t_years, rate=self.rate, vol=self.default_iv, option_type="CE")
            pe_bs = black_scholes(spot, k, t_years, rate=self.rate, vol=self.default_iv, option_type="PE")

            dist = abs(i)
            base_oi = max(5000, int(100000 / (1.0 + 0.3 * dist * dist)))

            strikes_data[k] = {
                "ce": {
                    "symbol": symbols_in.format_derivative_symbol(canonical, exp_dt, k, "CE"),
                    "last_price": ce_bs["price"],
                    "bid": max(0.05, round(ce_bs["price"] - 0.10, 2)),
                    "ask": round(ce_bs["price"] + 0.10, 2),
                    "volume": int(base_oi * 0.4),
                    "open_interest": base_oi,
                    "change_in_oi": int(base_oi * 0.05),
                    "iv": self.default_iv,
                    "greeks": ce_bs,
                    "tier": DataTier.SIMULATED,
                    "data_status": DataStatus.SIMULATED,
                    "is_simulated": True,
                    "is_live": False,
                },
                "pe": {
                    "symbol": symbols_in.format_derivative_symbol(canonical, exp_dt, k, "PE"),
                    "last_price": pe_bs["price"],
                    "bid": max(0.05, round(pe_bs["price"] - 0.10, 2)),
                    "ask": round(pe_bs["price"] + 0.10, 2),
                    "volume": int(base_oi * 0.35),
                    "open_interest": int(base_oi * 0.9),
                    "change_in_oi": int(base_oi * 0.04),
                    "iv": self.default_iv,
                    "greeks": pe_bs,
                    "tier": DataTier.SIMULATED,
                    "data_status": DataStatus.SIMULATED,
                    "is_simulated": True,
                    "is_live": False,
                }
            }

        return OptionChain(
            underlying=canonical,
            underlying_price=spot,
            expiry=exp_dt.strftime("%Y-%m-%d"),
            strikes_data=strikes_data,
            data_tier=DataTier.SIMULATED,
            exchange=exchange,
        )

    def get_derivatives_master(self, exchange="NSE"):
        return self.base_provider.get_derivatives_master(exchange=exchange)


# ---------------------------------------------------------------- Upstox Analytics Token Provider

_logger = logging.getLogger("day_trader.data_in")

UPSTOX_BASE_URL = "https://api.upstox.com"
UPSTOX_INSTRUMENTS_CDN_TEMPLATE = "https://assets.upstox.com/market-quote/instruments/exchange/%s.json.gz"

_UPSTOX_AUTH_ERROR_CODES = ("UDAPI100050", "UDAPI100051", "UDAPI100052", "UDAPI100016")
_UPSTOX_NOT_FOUND_CODES = ("UDAPI100011", "UDAPI100044")
_UPSTOX_RATE_LIMIT_CODES = ("UDAPI10005", "UDAPI100005")

def _build_upstox_index_keys():
    mapping = {}
    for canon, meta in getattr(symbols_in, "INDEX_MAP", {}).items():
        ukey = meta.get("upstox_key")
        if not ukey:
            continue
        mapping[canon.upper()] = ukey
        if meta.get("name"):
            mapping[str(meta["name"]).strip().upper()] = ukey
        if meta.get("yahoo"):
            mapping[str(meta["yahoo"]).strip().upper()] = ukey
        for alias in meta.get("aliases") or ():
            mapping[str(alias).strip().upper()] = ukey
    return mapping


_UPSTOX_INDEX_KEYS = _build_upstox_index_keys()


def _redact_upstox_secret(text, secret=None):
    return upstox_feed.redact_secret(text, secret=secret)


def _is_yahoo_fallback_enabled(config=None, env=None):
    """Return True only when Yahoo fallback is explicitly enabled in config or env."""
    cfg = _load_config_dict(config) if isinstance(config, (dict, str)) else (config if isinstance(config, dict) else {})
    env_map = env if env is not None else os.environ
    fb_prov = cfg.get("fallback_provider")
    if fb_prov is None:
        fb_prov = env_map.get("INDIA_MARKET_DATA_FALLBACK_PROVIDER")
    if fb_prov is not None and str(fb_prov).strip():
        fb_name = str(fb_prov).strip().lower()
        if fb_name != "yahoo":
            raise ProviderConfigError(
                "Unsupported fallback_provider: '%s'. Only 'yahoo' is supported." % fb_prov
            )
    else:
        fb_name = ""

    allow_flag = cfg.get("allow_yahoo_fallback")
    if allow_flag is None:
        env_flag = str(env_map.get("INDIA_MARKET_DATA_ALLOW_YAHOO_FALLBACK") or "").strip().lower()
        if env_flag in ("1", "true", "yes", "on"):
            allow_flag = True
        elif env_flag in ("0", "false", "no", "off"):
            allow_flag = False
    if allow_flag is False:
        return False
    return bool(allow_flag) or (fb_name == "yahoo")


def _parse_upstox_timestamp(val):
    """Parse an Upstox epoch-ms/epoch-s/ISO-8601 timestamp into UTC epoch seconds (int)."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        if num <= 0:
            return None
        if num > 1e14:
            return int(num / 1e6)
        if num > 1e11:
            return int(num / 1000.0)
        return int(num)
    raw = str(val).strip()
    if not raw:
        return None
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return _parse_upstox_timestamp(float(raw))
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=calendar_in.IST)
        return int(dt.timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(raw, fmt).replace(tzinfo=calendar_in.IST)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def _parse_range_days(range_str):
    raw = str(range_str or "1y").strip().lower()
    if raw == "max":
        return 3650
    m = re.match(r"^(\d+)\s*(d|wk|w|mo|m|y)$", raw)
    if not m:
        return 365
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return max(1, n)
    if unit in ("w", "wk"):
        return max(7, n * 7)
    if unit in ("mo", "m"):
        return max(30, n * 31)
    return max(365, n * 366)


def _to_upstox_interval_v3(interval):
    raw = str(interval or "1d").strip()
    low = raw.lower()
    mapping = {
        "1d": ("days", "1", 86400),
        "d": ("days", "1", 86400),
        "daily": ("days", "1", 86400),
        "1wk": ("weeks", "1", 604800),
        "1w": ("weeks", "1", 604800),
        "w": ("weeks", "1", 604800),
        "weekly": ("weeks", "1", 604800),
        "1mo": ("months", "1", 2592000),
        "mo": ("months", "1", 2592000),
        "monthly": ("months", "1", 2592000),
        "1m": ("minutes", "1", 60),
        "3m": ("minutes", "3", 180),
        "5m": ("minutes", "5", 300),
        "15m": ("minutes", "15", 900),
        "30m": ("minutes", "30", 1800),
        "60m": ("hours", "1", 3600),
        "1h": ("hours", "1", 3600),
    }
    if low in mapping:
        return mapping[low]
    m_min = re.match(r"^(\d+)m$", low)
    if m_min:
        val = int(m_min.group(1))
        if 1 <= val <= 300:
            return ("minutes", str(val), val * 60)
    m_hr = re.match(r"^(\d+)h$", low)
    if m_hr:
        val = int(m_hr.group(1))
        if 1 <= val <= 5:
            return ("hours", str(val), val * 3600)
    raise InvalidSymbolError("Unsupported Upstox V3 candle interval: '%s'" % interval)


def _iter_upstox_date_chunks(start_date, end_date, unit, unit_interval):
    """Yield (chunk_start, chunk_end) date windows complying with Upstox V3 calendar caps."""
    if start_date > end_date:
        return []
    iv_num = int(unit_interval)
    if unit == "minutes":
        # Upstox V3 caps: <=15m at 1 calendar month (28-day Feb -> 27d delta),
        # >15m at 1 quarter (Feb+Mar+Apr non-leap = 89 days -> 88d delta).
        max_days = 27 if iv_num <= 15 else 88
    elif unit == "hours":
        max_days = 88
    elif unit == "days":
        max_days = 3649
    else:
        return [(start_date, end_date)]
    chunks = []
    cur = start_date
    while cur <= end_date:
        chunk_end = min(end_date, cur + datetime.timedelta(days=max_days))
        chunks.append((cur, chunk_end))
        cur = chunk_end + datetime.timedelta(days=1)
    return chunks


class UpstoxOptionChain(OptionChain):
    """OptionChain preserving real Upstox provider, live/historical status, and Greeks."""

    def __init__(self, underlying, underlying_price, expiry, strikes_data,
                 data_tier=DataTier.REAL_TIME, data_status=DataStatus.CURRENT,
                 provider="upstox", is_live=True, timestamp=None, exchange="NSE",
                 pcr_override=None, underlying_key=None):
        OptionChain.__init__(
            self,
            underlying=underlying,
            underlying_price=underlying_price,
            expiry=expiry,
            strikes_data=strikes_data,
            data_tier=data_tier,
            timestamp=timestamp,
            exchange=exchange,
        )
        self.provider = str(provider or "upstox")
        self.data_status = str(data_status or DataStatus.CURRENT)
        self.is_live = bool(is_live)
        self.pcr_override = pcr_override
        self.underlying_key = underlying_key

    @property
    def pcr(self):
        calc = OptionChain.pcr.fget(self)
        if self.pcr_override is not None and not calc:
            return round(float(self.pcr_override), 3)
        return calc

    def to_dict(self):
        doc = OptionChain.to_dict(self)
        doc["provider"] = self.provider
        doc["source"] = self.provider
        doc["tier"] = self.data_tier
        doc["data_status"] = self.data_status
        doc["is_live"] = self.is_live
        doc["is_simulated"] = False
        if self.underlying_key:
            doc["underlying_key"] = self.underlying_key
        if isinstance(doc.get("meta"), dict):
            doc["meta"]["provider"] = self.provider
            doc["meta"]["source"] = self.provider
            doc["meta"]["tier"] = self.data_tier
            doc["meta"]["data_status"] = self.data_status
            doc["meta"]["is_live"] = self.is_live
            doc["meta"]["is_simulated"] = False
        return doc


class UpstoxMarketDataProvider(MarketDataProvider):
    """Read-only Upstox Analytics Token market-data provider for Indian equities/F&O.

    Supports Upstox API v2/v3 read-only GET endpoints and V3 WebSocket streaming:
    - Live quotes (/v3/market-quote/quotes, ltp, ohlc)
    - Historical & intraday candles (/v3/historical-candle/...) with auto-chunking
    - Option chains (/v2/option/chain), option contracts (/v2/option/contract),
      option Greeks (/v3/market-quote/option-greek), and Open Interest (OI)
    - Market status (/v2/market/status/{exchange})
    - Instrument & derivatives masters (NSE/BSE JSON masters + local ISIN fallback)
    - WebSocket Market Data Feed V3 (/v3/feed/market-data-feed/authorize + Protobuf)

    Strictly read-only: NO OAuth interactive login and NO order execution.
    """

    provider_name = "upstox"
    requires_auth = True
    required_credentials = ("analytics_token",)

    CAPABILITIES = {
        CAPABILITY_HISTORICAL_CANDLES: True,
        CAPABILITY_INTRADAY_CANDLES: True,
        CAPABILITY_LIVE_QUOTES: True,
        CAPABILITY_STREAMING_DATA: True,
        CAPABILITY_OPTION_CHAINS: True,
        CAPABILITY_OPEN_INTEREST: True,
        CAPABILITY_INSTRUMENT_MASTERS: True,
        CAPABILITY_MARKET_STATUS: True,
    }

    @classmethod
    def resolve_credentials(cls, config=None, env=None, analytics_token=None):
        """Resolve non-empty Upstox Analytics Token from explicit arg, config, or env."""
        if analytics_token is not None:
            cleaned = str(analytics_token).strip()
            return cleaned if cleaned else None
        cfg = _load_config_dict(config) if config is not None else {}
        creds_map = cfg.get("provider_credentials") or {}
        prov_creds = creds_map.get("upstox") if isinstance(creds_map, dict) else {}
        if not isinstance(prov_creds, dict):
            prov_creds = {}
        env_map = env if env is not None else os.environ
        candidates = (
            prov_creds.get("analytics_token"),
            cfg.get("upstox_analytics_token"),
            cfg.get("analytics_token"),
            env_map.get("UPSTOX_ANALYTICS_TOKEN"),
        )
        for val in candidates:
            if val is not None and str(val).strip():
                return str(val).strip()
        return None

    @classmethod
    def resolve_account_tier(cls, config=None, env=None, account_tier=None):
        """Resolve Upstox account tier ('standard' default, or opt-in 'plus')."""
        if account_tier is not None and str(account_tier).strip():
            raw = str(account_tier).strip()
        else:
            cfg = _load_config_dict(config) if config is not None else {}
            creds_map = cfg.get("provider_credentials") or {}
            prov_creds = creds_map.get("upstox") if isinstance(creds_map, dict) else {}
            if not isinstance(prov_creds, dict):
                prov_creds = {}
            env_map = env if env is not None else os.environ
            raw = None
            for val in (
                prov_creds.get("account_tier"),
                cfg.get("upstox_account_tier"),
                cfg.get("account_tier"),
                env_map.get("UPSTOX_ACCOUNT_TIER"),
            ):
                if val is not None and str(val).strip():
                    raw = str(val).strip()
                    break
        try:
            return upstox_feed.normalize_account_tier(raw)
        except ValueError as e:
            raise ProviderConfigError(str(e))

    @classmethod
    def validate_config(cls, config=None, env=None, analytics_token=None, account_tier=None, **_kwargs):
        """Ensure Upstox Analytics Token and account tier are valid before activating provider."""
        cfg = _load_config_dict(config) if config is not None else {}
        env_map = env if env is not None else os.environ
        _is_yahoo_fallback_enabled(cfg, env_map)
        cls.resolve_account_tier(config=cfg, env=env_map, account_tier=account_tier)
        token = cls.resolve_credentials(config=cfg, env=env_map, analytics_token=analytics_token)
        if not token:
            raise AuthenticationConfigError(
                "Upstox Analytics Token is missing. Set UPSTOX_ANALYTICS_TOKEN in your environment "
                "(or config provider_credentials.upstox.analytics_token) to use INDIA_MARKET_DATA_PROVIDER=upstox, "
                "or omit it to keep default free Yahoo delayed mode."
            )
        return True

    def __init__(
        self,
        analytics_token=None,
        config=None,
        env=None,
        http_get=None,
        ws_connect=None,
        timeout=DEFAULT_TIMEOUT,
        base_url=UPSTOX_BASE_URL,
        instruments_url_template=UPSTOX_INSTRUMENTS_CDN_TEMPLATE,
        allow_yahoo_fallback=None,
        fallback_factory=None,
        now_fn=None,
        account_tier=None,
    ):
        self._config = _load_config_dict(config) if config is not None else {}
        env_map = env if env is not None else os.environ
        self.account_tier = self.resolve_account_tier(
            config=self._config, env=env_map, account_tier=account_tier
        )
        token = self.resolve_credentials(config=self._config, env=env_map, analytics_token=analytics_token)
        if not token:
            raise AuthenticationConfigError(
                "Upstox Analytics Token is missing. Set UPSTOX_ANALYTICS_TOKEN or keep default Yahoo mode."
            )
        self._analytics_token = token
        if allow_yahoo_fallback is not None:
            self._allow_yahoo_fallback = bool(allow_yahoo_fallback)
        else:
            self._allow_yahoo_fallback = _is_yahoo_fallback_enabled(self._config, env_map)
        self._fallback_factory = fallback_factory or YahooMarketDataProvider
        self._http_get_fn = http_get
        self._ws_connect = ws_connect
        self._timeout = int(timeout)
        self._base_url = str(base_url or UPSTOX_BASE_URL).rstrip("/")
        self._instruments_url_template = str(instruments_url_template or UPSTOX_INSTRUMENTS_CDN_TEMPLATE)
        self._now_fn = now_fn

        self._instruments_by_exch = {}
        self._key_by_symbol = {}
        self._symbol_by_key = {}
        self._meta_by_key = {}
        self._active_ws_connections = [0]
        self._stream_quota = upstox_feed.UpstoxStreamQuota(account_tier=self.account_tier)

        custom_map = self._config.get("upstox_instruments") or self._config.get("symbol_map") or {}
        if isinstance(custom_map, dict):
            exch_default = _normalize_exchange(self._config.get("exchange") or "NSE")
            for raw_sym, ikey in custom_map.items():
                if raw_sym and ikey:
                    c_sym = symbols_in.normalize_symbol(str(raw_sym))
                    self._key_by_symbol[(exch_default, c_sym)] = str(ikey).strip()
                    self._symbol_by_key[str(ikey).strip()] = c_sym

    def stream_limits(self):
        """Return effective Upstox WebSocket V3 connection and subscription limits for configured tier."""
        return upstox_feed.get_tier_limits(self.account_tier)

    def __repr__(self):
        return "<UpstoxMarketDataProvider provider='upstox' tier='%s' readonly=True>" % self.account_tier

    __str__ = __repr__

    def _now_ist(self):
        if self._now_fn is not None:
            dt = self._now_fn()
            if dt.tzinfo is None:
                return dt.replace(tzinfo=calendar_in.IST)
            return dt.astimezone(calendar_in.IST)
        return calendar_in.now_ist()

    def _auth_headers(self):
        return {
            "Accept": "application/json",
            "Authorization": "Bearer %s" % self._analytics_token,
            "User-Agent": DEFAULT_USER_AGENT,
        }

    def _classify_upstox_error(self, status_code, body_text, url=""):
        clean_url = _redact_upstox_secret(url, self._analytics_token)
        clean_body = _redact_upstox_secret(body_text, self._analytics_token)
        err_codes = []
        err_msgs = []
        try:
            parsed = json.loads(body_text) if isinstance(body_text, str) else (
                body_text if isinstance(body_text, dict) else {}
            )
            if isinstance(parsed, dict):
                for e in (parsed.get("errors") or []):
                    if isinstance(e, dict):
                        code = str(e.get("errorCode") or "").strip()
                        msg = str(e.get("message") or "").strip()
                        if code:
                            err_codes.append(code)
                        if msg:
                            err_msgs.append(_redact_upstox_secret(msg, self._analytics_token))
                if not err_msgs and parsed.get("message"):
                    err_msgs.append(_redact_upstox_secret(parsed.get("message"), self._analytics_token))
        except Exception:
            pass

        combined_msg = "; ".join(err_msgs) or clean_body[:240] or ("HTTP %s" % status_code)
        combined_low = combined_msg.lower()
        if (
            status_code in (401, 403)
            or any(c in _UPSTOX_AUTH_ERROR_CODES for c in err_codes)
            or "invalid token" in combined_low
            or "token expired" in combined_low
            or "unauthorized" in combined_low
        ):
            raise AuthenticationConfigError(
                "Upstox authentication failed (%s): %s. Verify UPSTOX_ANALYTICS_TOKEN."
                % (", ".join(err_codes) or ("HTTP %s" % status_code), combined_msg)
            )
        if status_code == 429 or any(c in _UPSTOX_RATE_LIMIT_CODES for c in err_codes) or "rate limit" in combined_low:
            raise RateLimitError("Upstox rate limit exceeded (%s): %s" % (status_code, combined_msg))
        if (
            status_code == 404
            or any(c in _UPSTOX_NOT_FOUND_CODES for c in err_codes)
            or "invalid instrument" in combined_low
        ):
            raise SymbolNotFoundError("Upstox instrument/data not found (%s): %s" % (clean_url, combined_msg))
        if status_code is not None and int(status_code) >= 500:
            raise ProviderConnectionError("Upstox server error HTTP %s: %s" % (status_code, combined_msg))
        raise DataError("Upstox API error (%s): %s" % (", ".join(err_codes) or status_code, combined_msg))

    def _raw_http_get(self, url, headers=None, method="GET"):
        if str(method).upper() != "GET":
            raise ProviderConfigError(
                "UpstoxMarketDataProvider is strictly read-only; HTTP %s is forbidden." % method
            )
        hdrs = dict(headers or {})
        if self._http_get_fn is not None:
            resp = self._http_get_fn(url, headers=hdrs, timeout=self._timeout)
            if isinstance(resp, tuple) and len(resp) == 2:
                return int(resp[0]), resp[1]
            if hasattr(resp, "status_code") or hasattr(resp, "status"):
                code = getattr(resp, "status_code", None)
                if code is None:
                    code = getattr(resp, "status", 200)
                if hasattr(resp, "read"):
                    body = resp.read()
                elif hasattr(resp, "text"):
                    body = resp.text
                else:
                    body = resp.content
                return int(code), body
            return 200, resp

        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return int(getattr(resp, "status", 200)), resp.read()
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = str(e)
            self._classify_upstox_error(e.code, err_body, url=url)
        except urllib.error.URLError as e:
            raise ProviderConnectionError(
                "Upstox connection failed: %s" % _redact_upstox_secret(e.reason, self._analytics_token)
            )
        except OSError as e:
            raise ProviderConnectionError(
                "Upstox transport error: %s" % _redact_upstox_secret(e, self._analytics_token)
            )

    def _request_json(self, path_or_url, query_params=None, auth=True):
        url = path_or_url if str(path_or_url).startswith("http") else (self._base_url + path_or_url)
        if query_params:
            pairs = []
            for k, v in query_params.items():
                if v is not None:
                    pairs.append((k, str(v)))
            if pairs:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(pairs)

        headers = self._auth_headers() if auth else {"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT}
        status_code, body = self._raw_http_get(url, headers=headers, method="GET")
        if status_code >= 400:
            if isinstance(body, (bytes, bytearray)):
                body = bytes(body).decode("utf-8", errors="replace")
            self._classify_upstox_error(status_code, body, url=url)

        if isinstance(body, (dict, list)):
            data = body
        else:
            if isinstance(body, (bytes, bytearray)):
                raw_b = bytes(body)
                if raw_b[:2] == b"\x1f\x8b":
                    raw_b = gzip.decompress(raw_b)
                body_str = raw_b.decode("utf-8", errors="replace")
            else:
                body_str = str(body)
            try:
                data = json.loads(body_str)
            except ValueError as e:
                raise DataError("Malformed JSON from Upstox API: %s" % e)

        if isinstance(data, dict) and data.get("status") == "error":
            self._classify_upstox_error(status_code or 400, json.dumps(data), url=url)
        return data

    def _with_yahoo_fallback(self, fn_name, *args, **kwargs):
        if not self._allow_yahoo_fallback:
            raise AuthenticationConfigError(
                "Upstox authentication failed and Yahoo fallback is disabled by default."
            )
        _logger.warning("Upstox auth failed; delegating %s to explicit Yahoo DELAYED fallback.", fn_name)
        fb = self._fallback_factory()
        res = getattr(fb, fn_name)(*args, **kwargs)
        if isinstance(res, dict):
            out = dict(res)
            out["fallback_from"] = "upstox"
            out["fallback_reason"] = "upstox_auth_error"
            out["provider"] = "yahoo"
            out["source"] = "yahoo"
            out["is_live"] = False
            if str(out.get("tier") or "") == DataTier.REAL_TIME:
                out["tier"] = DataTier.DELAYED
            if isinstance(out.get("meta"), dict):
                out["meta"] = dict(out["meta"])
                out["meta"]["provider"] = "yahoo"
                out["meta"]["fallback_from"] = "upstox"
            return out
        return res

    def resolve_instrument_key(self, symbol, exchange="NSE"):
        """Resolve a ticker or instrument_key into (instrument_key, canonical_symbol, exchange)."""
        if not symbol or not str(symbol).strip():
            raise InvalidSymbolError("Empty symbol")
        raw = str(symbol).strip()
        exch = _normalize_exchange(exchange)
        raw_upper = raw.upper()
        if raw_upper.endswith(".NS"):
            exch = "NSE"
        elif raw_upper.endswith(".BO"):
            exch = "BSE"

        # Already an Upstox instrument key (e.g., NSE_EQ|INE002A01018, NSE_INDEX|Nifty 50, NSE_FO|12345)
        if "|" in raw:
            seg_part = raw.split("|", 1)[0].strip().upper()
            if seg_part.startswith("BSE"):
                exch = "BSE"
            elif seg_part.startswith("MCX"):
                exch = "MCX"
            elif seg_part.startswith("NSE") or seg_part.startswith("NFO"):
                exch = "NSE"
            mapped_sym = self._symbol_by_key.get(raw)
            if not mapped_sym:
                if seg_part.endswith("_INDEX"):
                    tail = raw.split("|", 1)[1].strip().upper()
                    rev = {
                        str(m["upstox_key"]).upper(): c
                        for c, m in getattr(symbols_in, "INDEX_MAP", {}).items()
                        if m.get("upstox_key")
                    }
                    mapped_sym = rev.get(raw.upper(), tail)
                else:
                    mapped_sym = raw
            return raw, mapped_sym, exch

        raw_spaces = " ".join(raw_upper.split())
        if raw_spaces in _UPSTOX_INDEX_KEYS:
            ikey = _UPSTOX_INDEX_KEYS[raw_spaces]
            idx_exch = "BSE" if ikey.startswith("BSE_") else "NSE"
            idx_canon = symbols_in.normalize_index_name(raw_spaces) or raw_spaces
            return ikey, idx_canon, idx_exch
        if (exch, raw_spaces) in self._key_by_symbol:
            ikey = self._key_by_symbol[(exch, raw_spaces)]
            idx_exch = "BSE" if ikey.startswith("BSE_") else exch
            return ikey, self._symbol_by_key.get(ikey, raw_spaces), idx_exch

        try:
            canon = symbols_in.normalize_symbol(raw)
        except Exception as e:
            if re.match(r"^(?:NIFTY|BSE|SENSEX|INDIA)[A-Z0-9 -]{1,30}$", raw_spaces):
                canon = raw_spaces
                if raw_spaces.startswith("BSE") or "SENSEX" in raw_spaces:
                    exch = "BSE"
            else:
                raise InvalidSymbolError("Invalid Indian market symbol '%s': %s" % (raw, e))
        if canon in _UPSTOX_INDEX_KEYS:
            ikey = _UPSTOX_INDEX_KEYS[canon]
            idx_exch = "BSE" if ikey.startswith("BSE_") else "NSE"
            return ikey, canon, idx_exch
        if raw_upper in _UPSTOX_INDEX_KEYS:
            ikey = _UPSTOX_INDEX_KEYS[raw_upper]
            idx_exch = "BSE" if ikey.startswith("BSE_") else "NSE"
            return ikey, canon, idx_exch

        if (exch, canon) in self._key_by_symbol:
            return self._key_by_symbol[(exch, canon)], canon, exch
        if ("NSE", canon) in self._key_by_symbol and not exchange:
            return self._key_by_symbol[("NSE", canon)], canon, "NSE"

        # Check instruments metadata / local ISIN directory without extra HTTP call
        info = None
        if hasattr(instruments, "get_instrument_info"):
            try:
                info = instruments.get_instrument_info(canon, exchange=exch)
            except TypeError:
                try:
                    info = instruments.get_instrument_info(canon)
                except Exception:
                    info = None
            except Exception:
                info = None
        elif self._http_get_fn is None and exch in ("NSE", "BSE") and hasattr(instruments, "resolve_instrument"):
            try:
                cand_info = instruments.resolve_instrument(canon, require_intraday=False, exchange=exch)
                if isinstance(cand_info, dict) and str(cand_info.get("exchange") or "NSE").upper() == exch:
                    info = cand_info
            except Exception:
                info = None
        if isinstance(info, dict):
            if info.get("upstox_key"):
                ikey = str(info["upstox_key"]).strip()
                self._key_by_symbol[(exch, canon)] = ikey
                self._symbol_by_key[ikey] = canon
                return ikey, canon, exch
            isin = info.get("isin") or info.get("ISIN")
            if isin and symbols_in.validate_isin(str(isin).strip()):
                seg = "BSE_EQ" if exch == "BSE" else "NSE_EQ"
                ikey = "%s|%s" % (seg, str(isin).strip().upper())
                self._key_by_symbol[(exch, canon)] = ikey
                self._symbol_by_key[ikey] = canon
                return ikey, canon, exch

        # Lazy-fetch Upstox official exchange JSON master
        if exch not in self._instruments_by_exch:
            try:
                self.get_instruments(exchange=exch)
            except AuthenticationConfigError:
                raise
            except Exception:
                pass
        if (exch, canon) in self._key_by_symbol:
            return self._key_by_symbol[(exch, canon)], canon, exch

        raise SymbolNotFoundError(
            "Could not resolve Upstox instrument_key for '%s' (%s). Supply 'NSE_EQ|<ISIN>' or populate "
            "Upstox instrument master / upstox_instruments mapping." % (symbol, exch)
        )

    def resolve_instrument_keys(self, symbols, exchange="NSE"):
        """Resolve multiple symbols into (resolved_keys_list, key_to_symbol_map)."""
        keys = []
        key_to_sym = {}
        for s in (symbols or []):
            ikey, canon, _ = self.resolve_instrument_key(s, exchange=exchange)
            keys.append(ikey)
            key_to_sym[ikey] = canon
        return keys, key_to_sym

    def _extract_quote_item(self, data_map, instrument_key, canon_symbol, exchange):
        if not isinstance(data_map, dict) or not data_map:
            return None
        if instrument_key in data_map:
            return data_map[instrument_key]
        seg = instrument_key.split("|", 1)[0] if "|" in instrument_key else ("BSE_EQ" if exchange == "BSE" else "NSE_EQ")
        colon_key = "%s:%s" % (seg, canon_symbol)
        if colon_key in data_map:
            return data_map[colon_key]

        ikey_low = instrument_key.lower()
        sym_up = str(canon_symbol).upper()
        for k, val in data_map.items():
            if not isinstance(val, dict):
                continue
            tok = str(val.get("instrument_token") or val.get("instrument_key") or "").strip().lower()
            if tok and tok == ikey_low:
                return val
            k_up = str(k).upper()
            if k_up == colon_key.upper() or k_up.endswith(":%s" % sym_up) or k_up.endswith("|%s" % sym_up):
                return val
            tail = instrument_key.split("|", 1)[1].upper() if "|" in instrument_key else sym_up
            if k_up.endswith(":%s" % tail) or k_up.endswith("|%s" % tail):
                return val

        # Guard against mismatched single-entry response returning another company
        if len(data_map) == 1:
            only_k, only_v = next(iter(data_map.items()))
            if isinstance(only_v, dict):
                tok = str(only_v.get("instrument_token") or only_v.get("instrument_key") or "").strip()
                if tok and tok.lower() != ikey_low:
                    raise DataError(
                        "Upstox returned quote for %s instead of requested %s (%s)"
                        % (tok, canon_symbol, instrument_key)
                    )
                if ":" in str(only_k):
                    ret_sym = str(only_k).split(":", 1)[1].upper()
                    if ret_sym != sym_up and ("|" not in instrument_key or ret_sym != instrument_key.split("|", 1)[1].upper()):
                        raise DataError(
                            "Upstox returned quote for %s instead of requested %s"
                            % (only_k, canon_symbol)
                        )
                return only_v
        return None

    def _normalize_quote_item(
        self,
        item,
        instrument_key,
        canon_symbol,
        exchange,
        now=None,
        mode="full",
        ohlc_interval="1d",
    ):
        if not isinstance(item, dict):
            raise MissingDataError("No quote data returned from Upstox for %s" % canon_symbol)
        last_price = item.get("last_price")
        if last_price is None:
            raise MissingDataError("Upstox quote missing last_price for %s" % canon_symbol)
        price = float(last_price)

        ohlc = item.get("ohlc") or item.get("live_ohlc") or {}
        if not isinstance(ohlc, dict):
            ohlc = {}
        prev_ohlc = item.get("prev_ohlc") or {}
        if not isinstance(prev_ohlc, dict):
            prev_ohlc = {}

        is_daily_ohlc = (str(ohlc_interval or "1d").strip().lower() == "1d")
        prev_close = None
        cands = [item.get("cp"), item.get("prev_close_price")]
        if mode != "ohlc" or is_daily_ohlc:
            cands.append(prev_ohlc.get("close"))
        for cand in cands:
            if cand is not None and float(cand) > 0:
                prev_close = float(cand)
                break
        if prev_close is None and item.get("net_change") is not None:
            prev_close = round(price - float(item["net_change"]), 4)
        if prev_close is None and is_daily_ohlc and mode != "ohlc" and ohlc.get("close") is not None and float(ohlc["close"]) > 0:
            prev_close = float(ohlc["close"])

        open_px = float(ohlc["open"]) if ohlc.get("open") is not None else None
        high_px = float(ohlc["high"]) if ohlc.get("high") is not None else None
        low_px = float(ohlc["low"]) if ohlc.get("low") is not None else None
        close_px = float(ohlc["close"]) if ohlc.get("close") is not None else price

        vol_val = item.get("volume")
        if vol_val is None:
            vol_val = ohlc.get("volume")
        volume = int(vol_val) if vol_val is not None else None

        q_ts = (
            _parse_upstox_timestamp(item.get("last_trade_time"))
            or _parse_upstox_timestamp(item.get("timestamp"))
            or _parse_upstox_timestamp(ohlc.get("ts"))
        )

        now_dt, cutoff_ts = _parse_cutoff_ts(now)
        ref_dt = now_dt if now_dt is not None else self._now_ist()
        if cutoff_ts is not None:
            if q_ts is None:
                raise MissingDataError(
                    "Upstox quote for %s has no timestamp before simulated cutoff %s"
                    % (canon_symbol, _format_ist(cutoff_ts))
                )
            if int(q_ts) > int(cutoff_ts):
                raise MissingDataError(
                    "Quote timestamp %s is after simulated cutoff %s for %s"
                    % (_format_ist(q_ts), _format_ist(cutoff_ts), canon_symbol)
                )
        elif q_ts is None:
            q_ts = int(ref_dt.timestamp())

        is_open = bool(calendar_in.is_market_hours_in(ref_dt, exchange=exchange))
        q_dt_ist = datetime.datetime.fromtimestamp(int(q_ts), tz=calendar_in.IST)
        same_session = (q_dt_ist.date() == ref_dt.date())
        if is_open and same_session and (int(ref_dt.timestamp()) - int(q_ts)) <= 600:
            data_status = DataStatus.CURRENT
            tier = DataTier.REAL_TIME
            is_live = True
        elif same_session and not is_open:
            data_status = DataStatus.HISTORICAL
            tier = DataTier.HISTORICAL
            is_live = False
        else:
            data_status = DataStatus.HISTORICAL
            tier = DataTier.HISTORICAL
            is_live = False

        age_secs, freshness = _compute_freshness(q_ts, data_status, now=now if now is not None else ref_dt)
        if age_secs is not None and age_secs > 600 and data_status == DataStatus.CURRENT:
            data_status = DataStatus.STALE
            is_live = False
            _, freshness = _compute_freshness(q_ts, data_status, now=now if now is not None else ref_dt)

        depth = item.get("depth") if isinstance(item.get("depth"), dict) else None
        best_bid = None
        best_ask = None
        if depth:
            buys = depth.get("buy") or []
            sells = depth.get("sell") or []
            if buys and isinstance(buys[0], dict):
                best_bid = buys[0].get("price")
            if sells and isinstance(sells[0], dict):
                best_ask = sells[0].get("price")

        payload = {
            "symbol": canon_symbol,
            "exchange": exchange,
            "instrument_key": instrument_key,
            "instrument_token": item.get("instrument_token") or instrument_key,
            "quote_mode": mode,
            "price": price,
            "last_price": price,
            "prev_close": prev_close,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": volume,
            "ltq": int(item["ltq"]) if item.get("ltq") is not None else None,
            "oi": float(item["oi"]) if item.get("oi") is not None else None,
            "prev_oi": float(item["previous_oi"]) if item.get("previous_oi") is not None else (
                float(item["prev_oi"]) if item.get("prev_oi") is not None else None
            ),
            "average_price": float(item["average_price"]) if item.get("average_price") is not None else None,
            "total_buy_quantity": item.get("total_buy_quantity"),
            "total_sell_quantity": item.get("total_sell_quantity"),
            "lower_circuit": item.get("lower_circuit_limit") or item.get("lower_circuit"),
            "upper_circuit": item.get("upper_circuit_limit") or item.get("upper_circuit"),
            "bid": best_bid,
            "ask": best_ask,
            "depth": depth,
            "timestamp": int(q_ts),
            "as_of": _format_ist(q_ts),
            "last_data_timestamp": _format_ist_iso(q_ts),
            "age_seconds": age_secs,
            "freshness": freshness,
            "meta": {
                "symbol": canon_symbol,
                "exchangeName": exchange,
                "instrument_key": instrument_key,
                "regularMarketPrice": price,
                "previousClose": prev_close,
                "regularMarketTime": int(q_ts),
            },
        }
        return _standardize_payload(
            payload,
            provider_name=self.provider_name,
            symbol=canon_symbol,
            exchange=exchange,
            data_status=data_status,
            tier=tier,
            is_live=is_live,
            is_simulated=False,
            now=now,
        )

    def get_quote(self, symbol, exchange="NSE", now=None):
        ikey, canon, exch = self.resolve_instrument_key(symbol, exchange=exchange)
        try:
            resp = self._request_json("/v3/market-quote/quotes", query_params={"instrument_key": ikey})
        except AuthenticationConfigError:
            if self._allow_yahoo_fallback:
                return self._with_yahoo_fallback("get_quote", symbol, exchange=exchange, now=now)
            raise
        data_map = (resp or {}).get("data") or {}
        item = self._extract_quote_item(data_map, ikey, canon, exch)
        return self._normalize_quote_item(item, ikey, canon, exch, now=now, mode="full")

    def get_ltp(self, symbol, exchange="NSE", now=None):
        ikey, canon, exch = self.resolve_instrument_key(symbol, exchange=exchange)
        try:
            resp = self._request_json("/v3/market-quote/ltp", query_params={"instrument_key": ikey})
        except AuthenticationConfigError:
            if self._allow_yahoo_fallback:
                return self._with_yahoo_fallback("get_ltp", symbol, exchange=exchange, now=now)
            raise
        data_map = (resp or {}).get("data") or {}
        item = self._extract_quote_item(data_map, ikey, canon, exch)
        return self._normalize_quote_item(item, ikey, canon, exch, now=now, mode="ltp")

    def get_ohlc(self, symbol, exchange="NSE", interval="1d", now=None):
        ikey, canon, exch = self.resolve_instrument_key(symbol, exchange=exchange)
        upstox_iv = {"1d": "1d", "1m": "I1", "30m": "I30"}.get(str(interval or "1d").lower(), interval)
        try:
            resp = self._request_json(
                "/v3/market-quote/ohlc",
                query_params={"instrument_key": ikey, "interval": upstox_iv},
            )
        except AuthenticationConfigError:
            if self._allow_yahoo_fallback:
                return self._with_yahoo_fallback("get_ohlc", symbol, exchange=exchange, interval=interval, now=now)
            raise
        data_map = (resp or {}).get("data") or {}
        item = self._extract_quote_item(data_map, ikey, canon, exch)
        out = self._normalize_quote_item(
            item, ikey, canon, exch, now=now, mode="ohlc", ohlc_interval=upstox_iv
        )
        out["interval"] = interval
        return out

    def get_quotes(self, symbols, exchange="NSE", mode="full", now=None):
        req_mode = str(mode or "full").strip().lower()
        endpoint = {
            "ltp": "/v3/market-quote/ltp",
            "ohlc": "/v3/market-quote/ohlc",
            "full": "/v3/market-quote/quotes",
        }.get(req_mode, "/v3/market-quote/quotes")

        resolved_entries = []
        out = {}
        for s in (symbols or []):
            try:
                raw_s = str(s or "").strip()
                ikey, canon, exch = self.resolve_instrument_key(raw_s, exchange=exchange)
                resolved_entries.append((ikey, canon, exch))
            except AuthenticationConfigError:
                raise
            except DataError as e:
                err_sym = str(s or "").strip().upper()
                try:
                    if "|" not in err_sym:
                        err_sym = symbols_in.normalize_symbol(err_sym)
                except Exception:
                    pass
                out[err_sym] = _standardize_payload(
                    {
                        "symbol": err_sym,
                        "exchange": _normalize_exchange(exchange),
                        "error": str(e),
                    },
                    provider_name=self.provider_name,
                    symbol=err_sym,
                    exchange=_normalize_exchange(exchange),
                    data_status=DataStatus.UNAVAILABLE,
                    tier=DataTier.UNAVAILABLE,
                    timestamp=None,
                    is_live=False,
                    is_simulated=False,
                    now=now,
                )

        batch_size = 500
        for idx in range(0, len(resolved_entries), batch_size):
            batch = resolved_entries[idx:idx + batch_size]
            keys_param = ",".join(b[0] for b in batch)
            qparams = {"instrument_key": keys_param}
            if req_mode == "ohlc":
                qparams["interval"] = "1d"
            try:
                resp = self._request_json(endpoint, query_params=qparams)
            except AuthenticationConfigError:
                if self._allow_yahoo_fallback:
                    return self._with_yahoo_fallback("get_quotes", symbols, exchange=exchange, now=now)
                raise
            data_map = (resp or {}).get("data") or {}
            for ikey, canon, exch in batch:
                try:
                    item = self._extract_quote_item(data_map, ikey, canon, exch)
                    if item is None:
                        raise DataError("No quote returned for %s:%s" % (exch, canon))
                    out[canon] = self._normalize_quote_item(item, ikey, canon, exch, now=now, mode=req_mode)
                except DataError as e:
                    out[canon] = _standardize_payload(
                        {
                            "symbol": canon,
                            "exchange": exch,
                            "error": str(e),
                        },
                        provider_name=self.provider_name,
                        symbol=canon,
                        exchange=exch,
                        data_status=DataStatus.UNAVAILABLE,
                        tier=DataTier.UNAVAILABLE,
                        timestamp=None,
                        is_live=False,
                        is_simulated=False,
                        now=now,
                    )
        return out

    def _parse_upstox_candles(self, raw_candles, unit):
        bars = []
        daily = (unit in ("days", "weeks", "months"))
        for row in (raw_candles or []):
            if isinstance(row, (list, tuple)):
                if len(row) < 5:
                    continue
                ts_raw, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
                v = row[5] if len(row) > 5 else 0
                oi = row[6] if len(row) > 6 else None
            elif isinstance(row, dict):
                ts_raw = row.get("ts") or row.get("timestamp") or row.get("date")
                o, h, l, c = row.get("open", row.get("o")), row.get("high", row.get("h")), row.get("low", row.get("l")), row.get("close", row.get("c"))
                v = row.get("volume", row.get("v", 0))
                oi = row.get("oi")
            else:
                continue
            if None in (ts_raw, o, h, l, c):
                continue
            ts_int = _parse_upstox_timestamp(ts_raw)
            if ts_int is None:
                continue
            dt_ist = datetime.datetime.fromtimestamp(int(ts_int), tz=calendar_in.IST)
            bar = {
                "ts": int(ts_int),
                "t": int(ts_int),
                "date": dt_ist.strftime("%Y-%m-%d"),
                "slot": dt_ist.strftime("%H:%M") if not daily else dt_ist.strftime("%Y-%m-%d"),
                "start": dt_ist.strftime("%H:%M") if not daily else dt_ist.strftime("%Y-%m-%d"),
                "ist_time": dt_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
                "o": float(o),
                "h": float(h),
                "l": float(l),
                "c": float(c),
                "v": int(v or 0),
            }
            if oi is not None:
                bar["oi"] = float(oi)
            bars.append(bar)
        return bars

    def get_historical_data(
        self,
        symbol,
        start=None,
        end=None,
        range_str="1y",
        interval="1d",
        exchange="NSE",
        now=None,
        include_intraday=False,
    ):
        ikey, canon, exch = self.resolve_instrument_key(symbol, exchange=exchange)
        unit, unit_iv, iv_secs = _to_upstox_interval_v3(interval)
        is_daily = unit in ("days", "weeks", "months")

        cutoff_dt, cutoff_ts = _parse_cutoff_ts(now)
        now_dt = self._now_ist()
        today_ist = now_dt.date()

        def _parse_d(val, default_d):
            if val is None:
                return default_d
            if isinstance(val, datetime.datetime):
                return val.astimezone(calendar_in.IST).date() if val.tzinfo else val.date()
            if isinstance(val, datetime.date):
                return val
            return datetime.date.fromisoformat(str(val)[:10])

        end_date = _parse_d(end, today_ist)
        if cutoff_dt is not None:
            end_date = min(end_date, cutoff_dt.date())
        default_start = end_date - datetime.timedelta(days=_parse_range_days(range_str))
        # Upstox V3 historical limits: minutes/hours from Jan 2022, days/weeks/months from Jan 2000.
        min_floor = datetime.date(2000, 1, 1) if is_daily else datetime.date(2022, 1, 1)
        start_date = max(min_floor, _parse_d(start, default_start))
        if end_date < start_date:
            raise MissingDataError(
                "Upstox %s range [%s, %s] has no bars inside API history bounds (>= %s)"
                % (interval, start_date.isoformat(), end_date.isoformat(), min_floor.isoformat())
            )

        encoded_key = urllib.parse.quote(ikey, safe="")
        by_ts = {}
        try:
            for c_start, c_end in _iter_upstox_date_chunks(start_date, end_date, unit, unit_iv):
                path = "/v3/historical-candle/%s/%s/%s/%s/%s" % (
                    encoded_key, unit, unit_iv, c_end.isoformat(), c_start.isoformat()
                )
                try:
                    resp = self._request_json(path)
                except SymbolNotFoundError:
                    if include_intraday and c_end >= today_ist:
                        continue
                    raise
                candles = ((resp or {}).get("data") or {}).get("candles") or []
                for bar in self._parse_upstox_candles(candles, unit):
                    by_ts[bar["ts"]] = bar

            if include_intraday and end_date >= today_ist:
                intra_path = "/v3/historical-candle/intraday/%s/%s/%s" % (encoded_key, unit, unit_iv)
                try:
                    resp_intra = self._request_json(intra_path)
                    candles_intra = ((resp_intra or {}).get("data") or {}).get("candles") or []
                    for bar in self._parse_upstox_candles(candles_intra, unit):
                        by_ts[bar["ts"]] = bar
                except SymbolNotFoundError:
                    pass
            elif is_daily and str(interval or "1d").lower() in ("1d", "d", "daily"):
                check_dt = cutoff_dt if cutoff_dt is not None else now_dt
                today_str = today_ist.isoformat()
                if (
                    start_date <= today_ist <= end_date
                    and not any(b.get("date") == today_str for b in by_ts.values())
                    and _is_daily_session_complete(today_ist, check_dt, exchange=exch)
                ):
                    intra_daily_path = "/v3/historical-candle/intraday/%s/days/1" % encoded_key
                    try:
                        resp_intra_d = self._request_json(intra_daily_path)
                        candles_intra_d = ((resp_intra_d or {}).get("data") or {}).get("candles") or []
                        for bar in self._parse_upstox_candles(candles_intra_d, "days"):
                            if bar.get("date") == today_str:
                                by_ts[bar["ts"]] = bar
                    except AuthenticationConfigError:
                        raise
                    except DataError:
                        pass
        except AuthenticationConfigError:
            if self._allow_yahoo_fallback:
                if is_daily:
                    return self._with_yahoo_fallback(
                        "get_daily_bars", symbol, range_str=range_str, exchange=exchange, now=now
                    )
                return self._with_yahoo_fallback(
                    "get_intraday_bars", symbol, interval=interval, range_str=range_str, exchange=exchange, now=now
                )
            raise

        bars = sorted(by_ts.values(), key=lambda b: b["ts"])
        check_dt = cutoff_dt if cutoff_dt is not None else now_dt
        if cutoff_ts is not None:
            filtered = []
            for b in bars:
                b_date = datetime.date.fromisoformat(b["date"])
                if is_daily:
                    if b_date > cutoff_dt.date():
                        continue
                    if b_date == cutoff_dt.date() and not _is_daily_session_complete(b_date, cutoff_dt, exchange=exch):
                        continue
                else:
                    if int(b["ts"]) + int(iv_secs) > int(cutoff_ts):
                        continue
                filtered.append(b)
            bars = filtered
        elif is_daily:
            bars = [
                b for b in bars
                if not (
                    b.get("date") == check_dt.date().isoformat()
                    and not _is_daily_session_complete(check_dt.date(), check_dt, exchange=exch)
                )
            ]

        if not bars:
            raise MissingDataError(
                "No Upstox %s bars available for %s%s"
                % (interval, canon, (" before simulated cutoff %s" % _format_ist(cutoff_ts)) if cutoff_ts else "")
            )

        last_bar = bars[-1]
        last_ts = int(last_bar["ts"]) if is_daily else (
            min(int(cutoff_ts), int(last_bar["ts"]) + int(iv_secs))
            if cutoff_ts is not None else int(last_bar["ts"]) + int(iv_secs)
        )
        prev_close = bars[-2]["c"] if len(bars) >= 2 else None
        if not is_daily and bars:
            distinct_dates = sorted(set(b["date"] for b in bars))
            if len(distinct_dates) >= 2:
                prev_day = distinct_dates[-2]
                prev_day_bars = [b for b in bars if b["date"] == prev_day]
                if prev_day_bars:
                    prev_close = prev_day_bars[-1]["c"]

        ref_dt = cutoff_dt if cutoff_dt is not None else now_dt
        is_open = bool(calendar_in.is_market_hours_in(ref_dt, exchange=exch))
        last_bar_date = datetime.date.fromisoformat(last_bar["date"])
        if not is_daily and is_open and last_bar_date == ref_dt.date() and (int(ref_dt.timestamp()) - last_ts) <= (iv_secs + 180):
            data_status = DataStatus.CURRENT
            tier = DataTier.REAL_TIME
            is_live = True
        else:
            data_status = DataStatus.HISTORICAL
            tier = DataTier.HISTORICAL
            is_live = False

        meta = {
            "symbol": canon,
            "exchangeName": exch,
            "instrument_key": ikey,
            "dataGranularity": interval,
            "regularMarketPrice": last_bar["c"],
            "regularMarketTime": last_ts,
            "previousClose": prev_close,
        }
        if cutoff_ts is not None:
            meta = _sanitize_meta_as_of(meta, bars, cutoff_ts, interval_secs=iv_secs, daily=is_daily)

        payload = {
            "symbol": canon,
            "exchange": exch,
            "instrument_key": ikey,
            "interval": interval,
            "range": range_str,
            "bars": bars,
            "count": len(bars),
            "last_price": last_bar["c"],
            "prev_close": prev_close,
            "timestamp": last_ts,
            "as_of": _format_ist(last_ts),
            "last_data_timestamp": _format_ist_iso(last_ts),
            "meta": meta,
        }
        return _standardize_payload(
            payload,
            provider_name=self.provider_name,
            symbol=canon,
            exchange=exch,
            data_status=data_status,
            tier=tier,
            is_live=is_live,
            is_simulated=False,
            now=now,
        )

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        return self.get_historical_data(
            symbol,
            range_str=range_str,
            interval="1d",
            exchange=exchange,
            now=now,
            include_intraday=False,
        )

    def get_intraday_data(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        return self.get_historical_data(
            symbol,
            range_str=range_str,
            interval=interval,
            exchange=exchange,
            now=now,
            include_intraday=True,
        )

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        return self.get_intraday_data(
            symbol,
            interval=interval,
            range_str=range_str,
            exchange=exchange,
            now=now,
            is_cas_eligible=is_cas_eligible,
        )

    def get_batch_series(self, symbols, range_str="3mo", interval="1d", exchange="NSE", now=None, pause=0.0):
        out = {}
        last_err = None
        is_daily = str(interval or "1d").lower() in ("1d", "d", "daily", "1wk", "1mo")
        for idx, sym in enumerate(symbols or []):
            canon = symbols_in.normalize_symbol(sym) if "|" not in str(sym) else str(sym)
            try:
                if is_daily:
                    pl = self.get_daily_bars(sym, range_str=range_str, exchange=exchange, now=now)
                else:
                    pl = self.get_intraday_data(sym, interval=interval, range_str=range_str, exchange=exchange, now=now)
                out[canon] = to_scanner_series(pl)
                if pl.get("fallback_from"):
                    out[canon]["fallback_from"] = pl["fallback_from"]
            except AuthenticationConfigError:
                raise
            except (SymbolNotFoundError, MissingDataError) as e:
                last_err = e
                continue
            if pause and idx + 1 < len(symbols):
                time.sleep(pause)
        if not out and symbols and last_err is not None:
            raise last_err
        return out

    def get_instruments(self, exchange="NSE", symbol=None, force_refresh=False):
        exch_req = str(exchange or "NSE").strip().upper()
        norm_exch = _normalize_exchange(exch_req)
        url_key = "complete" if exch_req in ("ALL", "COMPLETE") else norm_exch

        records = None if force_refresh else self._instruments_by_exch.get(url_key)
        if records is None:
            url = self._instruments_url_template % url_key
            # Public Upstox asset CDN requires no bearer token, though mock handlers accept either
            raw = self._request_json(url, auth=False)
            if isinstance(raw, dict):
                raw_list = raw.get("data") or raw.get("instruments") or []
            elif isinstance(raw, list):
                raw_list = raw
            else:
                raw_list = []

            records = []
            for rec in raw_list:
                if not isinstance(rec, dict):
                    continue
                ikey = str(rec.get("instrument_key") or "").strip()
                tsym = str(rec.get("trading_symbol") or rec.get("tradingsymbol") or "").strip()
                ex = _normalize_exchange(rec.get("exchange") or norm_exch)
                seg_str = rec.get("segment") or ""
                itype_str = rec.get("instrument_type") or ""
                # Upstox BOD JSON instrument master publishes tick_size in paise; normalize once to INR.
                if "tick_size_unit" in rec:
                    raw_unit = rec.get("tick_size_unit")
                    unit_low = str(raw_unit or "").strip().lower()
                    if not unit_low or unit_low not in ("inr", "rupee", "rupees", "paise", "paisa", "p"):
                        raise DataError(
                            "Unsupported tick_size_unit %r for Upstox instrument %r"
                            % (raw_unit, tsym or ikey)
                        )
                else:
                    raw_unit = "paise"
                tick_inr, tick_status = _normalize_provider_tick_size(
                    rec.get("tick_size"),
                    source_unit=raw_unit,
                    segment=seg_str,
                    instrument_type=itype_str,
                )
                item = {
                    "symbol": tsym,
                    "trading_symbol": tsym,
                    "name": rec.get("name") or rec.get("short_name") or tsym,
                    "exchange": ex,
                    "segment": seg_str,
                    "instrument_key": ikey,
                    "instrument_token": rec.get("exchange_token") or ikey,
                    "isin": rec.get("isin") or "",
                    "instrument_type": itype_str,
                    "tick_size": tick_inr,
                    "tick_size_unit": "INR",
                    "tick_size_status": tick_status,
                    "lot_size": rec.get("lot_size"),
                    "expiry": rec.get("expiry"),
                    "strike_price": rec.get("strike_price"),
                    "underlying_symbol": rec.get("underlying_symbol") or "",
                    "underlying_key": rec.get("underlying_key") or "",
                }
                records.append(item)
                if ikey:
                    self._meta_by_key[ikey] = item
                    if tsym:
                        self._symbol_by_key[ikey] = tsym
                        if symbols_in.is_derivative_symbol(tsym):
                            sym_norm = tsym.upper()
                        else:
                            try:
                                sym_norm = symbols_in.normalize_symbol(tsym)
                            except Exception:
                                sym_norm = tsym.upper()
                        # Prefer EQ / INDEX segments for canonical equity symbol resolution
                        if item["segment"] in ("NSE_EQ", "BSE_EQ", "NSE_INDEX", "BSE_INDEX") or (ex, sym_norm) not in self._key_by_symbol:
                            self._key_by_symbol[(ex, sym_norm)] = ikey
                        if item["segment"] in ("NSE_INDEX", "BSE_INDEX"):
                            for idx_alias in (tsym.upper(), str(item.get("name") or "").strip().upper(), ikey.split("|", 1)[-1].strip().upper()):
                                if idx_alias and (ex, idx_alias) not in self._key_by_symbol:
                                    self._key_by_symbol[(ex, idx_alias)] = ikey
                        exch_tok = str(rec.get("exchange_token") or "").strip()
                        if ex == "BSE" and exch_tok.isdigit() and item["segment"] in ("BSE_EQ", "BSE_INDEX"):
                            if (ex, exch_tok) not in self._key_by_symbol:
                                self._key_by_symbol[(ex, exch_tok)] = ikey
            self._instruments_by_exch[url_key] = records

        filtered = records
        if url_key != "complete":
            filtered = [r for r in filtered if (r.get("exchange") or norm_exch).upper() == norm_exch]
        if symbol:
            raw_sym = str(symbol).strip().upper()
            try:
                target = symbols_in.normalize_symbol(symbol) if "|" not in str(symbol) else raw_sym
            except Exception:
                target = raw_sym
            filtered = [
                r for r in filtered
                if (r.get("trading_symbol") or "").upper() == target
                or (r.get("symbol") or "").upper() == target
                or str(r.get("instrument_token") or "").strip().upper() == raw_sym
                or (r.get("instrument_key") or "").upper() == raw_sym
            ]
        return _standardize_payload(
            {
                "symbol": symbol or norm_exch,
                "exchange": norm_exch,
                "instruments": [dict(r) for r in filtered],
                "count": len(filtered),
            },
            provider_name=self.provider_name,
            symbol=symbol or norm_exch,
            exchange=norm_exch,
            data_status=DataStatus.CURRENT,
            tier=DataTier.HISTORICAL,
            is_live=False,
        )

    def get_instrument_metadata(self, symbol, exchange="NSE", require_tick=False):
        """Resolve an instrument's normalized metadata (canonical INR tick_size, lot_size, key)."""
        ikey, canon, exch = self.resolve_instrument_key(symbol, exchange=exchange)
        if ikey not in self._meta_by_key:
            self.get_instruments(exchange=exch)
        meta = dict(self._meta_by_key.get(ikey) or {"instrument_key": ikey, "symbol": canon, "exchange": exch})
        if require_tick and meta.get("tick_size") is None:
            raise DataError(
                "Missing or invalid tick_size for %s (%s): %s"
                % (symbol, exch, meta.get("tick_size_status") or "missing")
            )
        return meta

    def get_derivatives_master(self, exchange="NSE", underlying=None):
        norm_exch = _normalize_exchange(exchange)
        inst_doc = self.get_instruments(exchange=norm_exch)
        all_recs = inst_doc.get("instruments") or []
        fo_segs = ("NSE_FO", "BSE_FO", "MCX_FO", "NFO", "BFO")
        und_up = symbols_in.normalize_symbol(underlying) if underlying else None
        filtered = []
        for r in all_recs:
            seg = str(r.get("segment") or "").upper()
            itype = str(r.get("instrument_type") or "").upper()
            if seg not in fo_segs and itype not in ("FUT", "CE", "PE", "OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"):
                continue
            if und_up:
                u_sym = str(r.get("underlying_symbol") or "").upper()
                t_sym = str(r.get("trading_symbol") or "").upper()
                if u_sym != und_up and not t_sym.startswith(und_up):
                    continue
            filtered.append(r)
        return _standardize_payload(
            {
                "symbol": und_up or norm_exch,
                "exchange": norm_exch,
                "contracts": filtered,
                "count": len(filtered),
            },
            provider_name=self.provider_name,
            symbol=und_up or norm_exch,
            exchange=norm_exch,
            data_status=DataStatus.CURRENT,
            tier=DataTier.HISTORICAL,
            is_live=False,
        )

    def get_option_contracts(self, symbol, expiry=None, exchange="NSE"):
        ikey, canon, exch = self.resolve_instrument_key(symbol, exchange=exchange)
        params = {"instrument_key": ikey}
        if expiry:
            params["expiry_date"] = str(expiry)[:10]
        resp = self._request_json("/v2/option/contract", query_params=params)
        contracts = (resp or {}).get("data") or []
        expiries = sorted(set(str(c.get("expiry"))[:10] for c in contracts if isinstance(c, dict) and c.get("expiry")))
        return _standardize_payload(
            {
                "symbol": canon,
                "exchange": exch,
                "instrument_key": ikey,
                "contracts": contracts,
                "expiries": expiries,
                "count": len(contracts),
            },
            provider_name=self.provider_name,
            symbol=canon,
            exchange=exch,
            data_status=DataStatus.CURRENT,
            tier=DataTier.REAL_TIME,
            is_live=True,
        )

    def get_option_greeks(self, symbols, exchange="NSE"):
        """Fetch live option Greeks via Upstox V3 /v3/market-quote/option-greek (max 50 keys per call)."""
        resolved = []
        for s in (symbols or []):
            ikey, canon, exch = self.resolve_instrument_key(s, exchange=exchange)
            resolved.append((ikey, canon, exch))
        out = {}
        for idx in range(0, len(resolved), 50):
            batch = resolved[idx:idx + 50]
            resp = self._request_json(
                "/v3/market-quote/option-greek",
                query_params={"instrument_key": ",".join(b[0] for b in batch)},
            )
            data_map = (resp or {}).get("data") or {}
            for ikey, canon, exch in batch:
                item = self._extract_quote_item(data_map, ikey, canon, exch)
                if item is not None:
                    out[ikey] = item
        return out

    def get_option_chain(self, symbol, expiry=None, exchange="NSE", spot_price=None, now=None):
        ikey, canon, exch = self.resolve_instrument_key(symbol, exchange=exchange)
        cutoff_dt, _ = _parse_cutoff_ts(now)
        now_dt = self._now_ist()
        if cutoff_dt is not None and cutoff_dt.date() < now_dt.date():
            raise MissingDataError(
                "Upstox option chain provides current session snapshots only and cannot serve past date %s for %s"
                % (cutoff_dt.date().isoformat(), canon)
            )
        ref_dt = cutoff_dt if cutoff_dt is not None else now_dt
        exp_str = str(expiry)[:10] if expiry else None
        if not exp_str:
            exp_dt = calendar_in.get_next_expiry(as_of_date=ref_dt, underlying=canon)
            exp_str = exp_dt.isoformat() if isinstance(exp_dt, datetime.date) else str(exp_dt)[:10]

        resp = self._request_json(
            "/v2/option/chain",
            query_params={"instrument_key": ikey, "expiry_date": exp_str},
        )
        rows = (resp or {}).get("data") or []
        if not isinstance(rows, list) or not rows:
            raise MissingDataError("Upstox returned empty option chain for %s expiry %s" % (canon, exp_str))

        is_open = bool(calendar_in.is_market_hours_in(ref_dt, exchange=exch))
        tier = DataTier.REAL_TIME if is_open else DataTier.HISTORICAL
        data_status = DataStatus.CURRENT if is_open else DataStatus.HISTORICAL
        exp_compact = exp_str.replace("-", "")
        strikes_data = {}
        derived_spot = spot_price
        pcr_val = None
        returned_exp = exp_str

        for row in rows:
            if not isinstance(row, dict) or row.get("strike_price") is None:
                continue
            strike = float(row["strike_price"])
            if row.get("expiry"):
                returned_exp = str(row["expiry"])[:10]
                exp_compact = returned_exp.replace("-", "")
            if derived_spot is None and row.get("underlying_spot_price") is not None:
                spot_cand = float(row["underlying_spot_price"])
                if spot_cand > 0:
                    derived_spot = spot_cand
            if pcr_val is None and row.get("pcr") is not None:
                pcr_val = float(row["pcr"])

            strike_label = int(strike) if strike.is_integer() else strike
            sides = {}
            for api_side, leg in (("call_options", "CE"), ("put_options", "PE")):
                opt = row.get(api_side)
                if not isinstance(opt, dict):
                    continue
                mdata = opt.get("market_data") or {}
                gdata = opt.get("option_greeks") or {}
                opt_ikey = opt.get("instrument_key")
                mapped_sym = self._symbol_by_key.get(opt_ikey) if opt_ikey else None
                c_sym = mapped_sym or ("%s%s%s%s" % (canon, exp_compact, strike_label, leg))
                raw_oi = mdata.get("oi")
                oi_val = int(raw_oi) if raw_oi is not None else 0
                raw_prev_oi = mdata.get("prev_oi")
                if raw_prev_oi is None:
                    raw_prev_oi = mdata.get("previous_oi")
                prev_oi = int(raw_prev_oi) if raw_prev_oi is not None else None
                change_in_oi = (oi_val - prev_oi) if (raw_oi is not None and prev_oi is not None) else None
                ltp_val = mdata.get("ltp")
                if ltp_val is None:
                    ltp_val = mdata.get("close_price")
                greeks = {
                    "delta": gdata.get("delta"),
                    "gamma": gdata.get("gamma"),
                    "theta": gdata.get("theta"),
                    "vega": gdata.get("vega"),
                }
                if gdata.get("pop") is not None:
                    greeks["pop"] = gdata.get("pop")
                sides[leg.lower()] = {
                    "symbol": c_sym,
                    "instrument_key": opt_ikey,
                    "last_price": float(ltp_val) if ltp_val is not None else 0.0,
                    "close_price": float(mdata["close_price"]) if mdata.get("close_price") is not None else None,
                    "bid": float(mdata["bid_price"]) if mdata.get("bid_price") is not None else None,
                    "bid_qty": int(mdata["bid_qty"]) if mdata.get("bid_qty") is not None else None,
                    "ask": float(mdata["ask_price"]) if mdata.get("ask_price") is not None else None,
                    "ask_qty": int(mdata["ask_qty"]) if mdata.get("ask_qty") is not None else None,
                    "volume": int(mdata.get("volume") or 0),
                    "open_interest": oi_val,
                    "prev_oi": prev_oi,
                    "change_in_oi": change_in_oi,
                    "iv": float(gdata["iv"]) if gdata.get("iv") is not None else None,
                    "greeks": greeks,
                    "tier": tier,
                    "data_status": data_status,
                    "is_simulated": False,
                    "is_live": is_open,
                }
            if sides:
                strikes_data[strike] = sides

        if not strikes_data:
            raise MissingDataError("No usable option strikes returned from Upstox for %s" % canon)
        if derived_spot is None:
            try:
                derived_spot = self.get_ltp(canon, exchange=exch)["price"]
            except Exception:
                derived_spot = sorted(strikes_data.keys())[len(strikes_data) // 2]

        return UpstoxOptionChain(
            underlying=canon,
            underlying_price=float(derived_spot),
            expiry=returned_exp,
            strikes_data=strikes_data,
            data_tier=tier,
            data_status=data_status,
            provider=self.provider_name,
            is_live=is_open,
            timestamp=int(ref_dt.timestamp()),
            exchange=exch,
            pcr_override=pcr_val,
            underlying_key=ikey,
        )

    def get_market_status(self, exchange="NSE", as_of=None):
        exch = _normalize_exchange(exchange)
        base = MarketDataProvider.get_market_status(self, exchange=exch, as_of=as_of)
        if as_of is not None:
            return base
        try:
            resp = self._request_json("/v2/market/status/%s" % urllib.parse.quote(exch))
        except AuthenticationConfigError:
            if self._allow_yahoo_fallback:
                return self._with_yahoo_fallback("get_market_status", exchange=exchange, as_of=as_of)
            raise
        data = (resp or {}).get("data") or {}
        raw_status = str(data.get("status") or "").strip().upper()
        last_updated = _parse_upstox_timestamp(data.get("last_updated"))
        out = dict(base)
        if raw_status:
            out["raw_status"] = raw_status
            if "OPEN" in raw_status and "PRE" not in raw_status:
                out["is_open"] = True
                out["status"] = "OPEN"
            elif "PRE_OPEN" in raw_status or "PRE" in raw_status:
                out["is_open"] = False
                out["status"] = "PRE_OPEN"
            elif "CLOSING" in raw_status or "CLOSE" in raw_status:
                out["is_open"] = False
                out["status"] = "CLOSED"
        if data.get("cas_eligible_status") is not None:
            out["cas_eligible_status"] = data.get("cas_eligible_status")
        if last_updated is not None:
            out["timestamp"] = last_updated
            out["as_of"] = _format_ist(last_updated)
            out["last_data_timestamp"] = _format_ist_iso(last_updated)
        return _standardize_payload(
            out,
            provider_name=self.provider_name,
            symbol=exch,
            exchange=exch,
            data_status=DataStatus.CURRENT,
            tier=DataTier.REAL_TIME,
            is_live=True,
            is_simulated=False,
        )

    def create_streamer(
        self,
        on_message=None,
        on_status=None,
        stale_after_seconds=30.0,
        clock=None,
        sleep_fn=None,
    ):
        """Create a lifecycle-managed Upstox V3 WebSocket streamer authenticated with Analytics Token."""
        def _authorize_wss():
            try:
                resp = self._request_json("/v3/feed/market-data-feed/authorize")
            except AuthenticationConfigError as e:
                raise upstox_feed.UpstoxStreamAuthError(_redact_upstox_secret(e, self._analytics_token))
            data = (resp or {}).get("data") or {}
            uri = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
            if not uri:
                raise upstox_feed.UpstoxStreamError("Upstox authorize response missing authorized_redirect_uri")
            return str(uri).strip()

        return upstox_feed.UpstoxMarketDataStreamer(
            authorize_fn=_authorize_wss,
            ws_connect=self._ws_connect,
            on_message=on_message,
            on_status=on_status,
            stale_after_seconds=stale_after_seconds,
            clock=clock,
            sleep_fn=sleep_fn,
            active_connections_counter=self._active_ws_connections,
            account_tier=self.account_tier,
            quota=self._stream_quota,
        )


# ---------------------------------------------------------------- Provider Registry

_BUILTIN_PROVIDERS = {
    "yahoo": YahooMarketDataProvider,
    "offline": OfflineMarketDataProvider,
    "simulated": SimulatedMarketDataProvider,
    "upstox": UpstoxMarketDataProvider,
}
_REGISTRY = dict(_BUILTIN_PROVIDERS)
_CUSTOM_REGISTRY = sys.__dict__.setdefault("_DAY_TRADER_IN_CUSTOM_PROVIDERS", {})


def register_provider(name, provider_cls):
    """Register a new market data provider class."""
    key = str(name).lower().strip()
    _CUSTOM_REGISTRY[key] = provider_cls
    _REGISTRY[key] = provider_cls


def unregister_provider(name):
    """Remove a dynamically registered provider (useful in isolated tests)."""
    key = str(name).lower().strip()
    removed = _CUSTOM_REGISTRY.pop(key, None)
    if key in _BUILTIN_PROVIDERS:
        _REGISTRY[key] = _BUILTIN_PROVIDERS[key]
        return removed
    local_removed = _REGISTRY.pop(key, None)
    return removed if removed is not None else local_removed


def list_providers():
    """Return sorted names of registered Indian market data providers."""
    return sorted(set(_REGISTRY.keys()) | set(_CUSTOM_REGISTRY.keys()))


def _load_config_dict(config=None):
    if isinstance(config, dict):
        return config
    for path in (os.environ.get("TRADER_CONFIG"), os.path.join("trader", "config.json")):
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
    return {}


def resolve_provider_name(name=None, source=None, config=None):
    """Resolve active Indian market data provider name.

    Precedence:
    1. explicit name argument
    2. explicit source override
    3. INDIA_MARKET_DATA_PROVIDER environment variable
    4. config dict ('india_market_data_provider', 'data_provider', 'provider')
    5. BHARAT_DATA_PROVIDER legacy environment variable
    6. 'yahoo' safe default
    """
    if name:
        return str(name).lower().strip()
    if source:
        return str(source).lower().strip()
    env_primary = os.environ.get("INDIA_MARKET_DATA_PROVIDER")
    if env_primary and str(env_primary).strip():
        return str(env_primary).lower().strip()
    cfg = _load_config_dict(config)
    for key in ("india_market_data_provider", "INDIA_MARKET_DATA_PROVIDER", "data_provider", "provider"):
        val = cfg.get(key)
        if val and str(val).strip():
            return str(val).lower().strip()
    env_legacy = os.environ.get("BHARAT_DATA_PROVIDER")
    if env_legacy and str(env_legacy).strip():
        return str(env_legacy).lower().strip()
    return "yahoo"


def get_provider(name=None, source=None, config=None, **kwargs):
    """Factory to retrieve a configured market data provider instance."""
    resolved = resolve_provider_name(name=name, source=source, config=config)
    provider_cls = _CUSTOM_REGISTRY.get(resolved) or _REGISTRY.get(resolved)
    if provider_cls is None:
        raise ProviderConfigError(
            "Unknown market data provider: '%s'. Available: %s" % (resolved, list_providers())
        )
    cfg = _load_config_dict(config)
    try:
        if isinstance(provider_cls, type) and issubclass(provider_cls, UpstoxMarketDataProvider):
            provider_cls.validate_config(
                config=cfg,
                env=kwargs.get("env"),
                analytics_token=kwargs.get("analytics_token"),
                account_tier=kwargs.get("account_tier"),
            )
        elif "env" in kwargs:
            provider_cls.validate_config(config=cfg, env=kwargs.get("env"))
        else:
            provider_cls.validate_config(config=cfg)
    except AuthenticationConfigError as e:
        if resolved == "upstox" and _is_yahoo_fallback_enabled(cfg):
            _logger.warning(
                "Upstox credentials missing (%s); falling back to explicit Yahoo DELAYED provider.",
                _redact_upstox_secret(e),
            )
            fb = YahooMarketDataProvider()
            fb.fallback_from = "upstox"
            fb.fallback_reason = "missing_credentials"
            return fb
        raise
    if cfg and "config" not in kwargs:
        try:
            init_fn = provider_cls.__init__
            code_obj = getattr(init_fn, "__code__", None)
            if code_obj and "config" in code_obj.co_varnames[:code_obj.co_argcount + code_obj.co_kwonlyargcount]:
                kwargs["config"] = cfg
        except Exception:
            pass
    return provider_cls(**kwargs)


# ---------------------------------------------------------------- Engine Adapters & Technical Indicators

def to_scanner_series(daily_bars_payload):
    """Convert normalized daily bars into the series dict format expected by scan_daily/scan_orb."""
    bars = daily_bars_payload.get("bars", [])
    timestamps = [b["ts"] for b in bars]
    closes = [b["c"] for b in bars]
    opens = [b.get("o") for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    volumes = [b["v"] for b in bars]
    meta = dict(daily_bars_payload.get("meta") or {})
    if bars:
        meta.setdefault("regularMarketPrice", closes[-1])
        is_daily = daily_bars_payload.get("interval") in ("1d", "d", "daily", None)
        vol_fallback = volumes[-1] if is_daily else sum(v or 0 for v in volumes)
        meta.setdefault("regularMarketVolume", vol_fallback)
        meta.setdefault("regularMarketTime", timestamps[-1])
    out = {
        "t": timestamps,
        "c": closes,
        "o": opens,
        "h": highs,
        "l": lows,
        "v": volumes,
        "meta": meta,
        "provider": daily_bars_payload.get("provider", daily_bars_payload.get("source", "yahoo")),
        "data_status": daily_bars_payload.get("data_status", DataStatus.HISTORICAL),
        "is_live": bool(daily_bars_payload.get("is_live", False)),
        "is_simulated": bool(daily_bars_payload.get("is_simulated", False)),
        "last_data_timestamp": daily_bars_payload.get("last_data_timestamp") or (
            _format_ist_iso(timestamps[-1]) if timestamps else None
        ),
    }
    if daily_bars_payload.get("simulated_at") is not None:
        out["simulated_at"] = daily_bars_payload.get("simulated_at")
    return out


def to_entry_chart(intraday_payload):
    """Convert provider get_intraday_data() payload into trader-entry chart dict."""
    bars_out = []
    for b in intraday_payload.get("bars", []):
        t = b.get("t") if b.get("t") is not None else b.get("ts")
        if t is None:
            continue
        bars_out.append({
            "t": int(t),
            "o": float(b["o"]),
            "h": float(b["h"]),
            "l": float(b["l"]),
            "c": float(b["c"]),
            "v": int(b.get("v") or 0),
        })
    meta = dict(intraday_payload.get("meta") or {})
    prev_close = intraday_payload.get("prev_close")
    if prev_close is None:
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    data_time = intraday_payload.get("timestamp")
    if data_time is None and bars_out:
        iv = _interval_to_seconds(intraday_payload.get("interval") or "5m") or 0
        data_time = bars_out[-1]["t"] + iv
    if prev_close is not None:
        meta.setdefault("previousClose", prev_close)
    if data_time is not None:
        meta["regularMarketTime"] = data_time
    else:
        meta.pop("regularMarketTime", None)
    provider = intraday_payload.get("provider") or intraday_payload.get("source") or "yahoo"
    tier = intraday_payload.get("tier") or (
        DataTier.DELAYED if provider == "yahoo" else DataTier.HISTORICAL
    )
    out = {
        "symbol": intraday_payload.get("symbol", ""),
        "meta": meta,
        "bars": bars_out,
        "prev_close": prev_close,
        "data_time": data_time,
        "is_offline": provider == "offline",
        "provider": provider,
        "source": provider,
        "data_tier": tier,
        "provider_data_status": intraday_payload.get("data_status", DataStatus.HISTORICAL),
        "is_live": bool(intraday_payload.get("is_live", False)),
        "is_simulated": bool(intraday_payload.get("is_simulated", False)),
        "freshness_seconds": intraday_payload.get("freshness_seconds"),
        "last_data_timestamp": intraday_payload.get("last_data_timestamp") or (
            _format_ist_iso(data_time) if data_time is not None else None
        ),
    }
    if intraday_payload.get("simulated_at") is not None:
        out["simulated_at"] = intraday_payload.get("simulated_at")
    return out


def to_exit_chart(bars_payload):
    """Convert provider intraday/daily bars payload into trader-exit ATR bar format."""
    bars_out = []
    for b in bars_payload.get("bars", []):
        t = b.get("t") if b.get("t") is not None else b.get("ts")
        if t is None:
            continue
        bars_out.append({
            "t": int(t),
            "h": float(b["h"]),
            "l": float(b["l"]),
            "c": float(b["c"]),
        })
    data_time = bars_payload.get("timestamp")
    if data_time is None and bars_out:
        iv = _interval_to_seconds(bars_payload.get("interval") or "") or 0
        data_time = bars_out[-1]["t"] + (iv if iv < 86400 else 0)
    out = {
        "bars": bars_out,
        "data_time": data_time,
        "provider": bars_payload.get("provider") or bars_payload.get("source") or "yahoo",
        "data_status": bars_payload.get("data_status", DataStatus.HISTORICAL),
        "is_live": bool(bars_payload.get("is_live", False)),
        "is_simulated": bool(bars_payload.get("is_simulated", False)),
        "last_data_timestamp": bars_payload.get("last_data_timestamp") or (
            _format_ist_iso(data_time) if data_time is not None else None
        ),
    }
    if bars_payload.get("simulated_at") is not None:
        out["simulated_at"] = bars_payload.get("simulated_at")
    return out


def to_timemachine_bars(intraday_payload):
    """Convert provider intraday payload into normalized timemachine OHLCV bar tuples."""
    bars = []
    for b in intraday_payload.get("bars", []):
        t = b.get("t") if b.get("t") is not None else b.get("ts")
        if t is None:
            continue
        bars.append((
            int(t),
            float(b["o"]),
            float(b["h"]),
            float(b["l"]),
            float(b["c"]),
            int(b.get("v") or 0),
        ))
    return sorted(bars, key=lambda row: row[0])


def calculate_rsi(closes, period=14):
    """Calculate Relative Strength Index (RSI) using Wilder's smoothing."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def calculate_ma(closes, period=20):
    """Calculate simple moving average."""
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / float(period), 2)


def detect_setups(daily_bars):
    """Detect setup candidates on a series of daily bars (minimum 30 days).

    Supports:
    - breakout_20d: 20-day closing-high breakout
    - bull_flag: consolidation after strong run
    - reclaim_ma20: closed above 20-day average after dipping below
    - oversold: RSI below 30
    - oversold_bounce: RSI crossed above 30 with higher close
    """
    if len(daily_bars) < 30:
        return [], {}
    closes = [b["c"] for b in daily_bars]
    last = closes[-1]
    hi20 = max(closes[-21:-1])
    ma20 = sum(closes[-20:]) / 20.0
    ma20_prev = sum(closes[-21:-1]) / 20.0
    run = closes[-6] / closes[-16] - 1.0 if closes[-16] > 0 else 0.0
    rng5 = (max(closes[-5:]) - min(closes[-5:])) / last if last > 0 else 0.0
    r = calculate_rsi(closes)
    r_prev = calculate_rsi(closes[:-1])

    setups = []
    if last > hi20:
        setups.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        setups.append("bull_flag")
    if closes[-2] < ma20_prev and last > ma20:
        setups.append("reclaim_ma20")
    if r is not None and r < 30:
        setups.append("oversold")
    if r_prev is not None and r_prev < 30 and last > closes[-2]:
        setups.append("oversold_bounce")

    metrics = {
        "rsi": r,
        "rsi_prev": r_prev,
        "high20": round(hi20, 2),
        "ma20": round(ma20, 2),
        "run_pct": round(run * 100, 2),
        "range5_pct": round(rng5 * 100, 2),
    }
    return setups, metrics


# ---------------------------------------------------------------- Module-level convenience functions

def fetch_quote(symbol, exchange="NSE", provider=None, now=None):
    prov = provider or get_provider()
    q = _call_with_optional_now(prov.get_quote, symbol, exchange=exchange, now=now)
    return _validate_quote_as_of(q, now, symbol=symbol)


def fetch_ltp(symbol, exchange="NSE", provider=None, now=None):
    prov = provider or get_provider()
    q = _call_with_optional_now(prov.get_ltp, symbol, exchange=exchange, now=now)
    return _validate_quote_as_of(q, now, symbol=symbol)


def fetch_ohlc(symbol, exchange="NSE", interval="1d", provider=None, now=None):
    prov = provider or get_provider()
    q = _call_with_optional_now(prov.get_ohlc, symbol, exchange=exchange, interval=interval, now=now)
    return _validate_quote_as_of(q, now, symbol=symbol)


def fetch_quotes(symbols, exchange="NSE", provider=None, now=None):
    prov = provider or get_provider()
    quotes = _call_with_optional_now(prov.get_quotes, symbols, exchange=exchange, now=now)
    if now is not None and isinstance(quotes, dict):
        out = {}
        for k, v in quotes.items():
            try:
                out[k] = _validate_quote_as_of(v, now, symbol=k)
            except DataError as e:
                out[k] = _standardize_payload(
                    {"symbol": k, "exchange": exchange, "error": str(e)},
                    provider_name=getattr(prov, "provider_name", "unknown"),
                    symbol=k,
                    exchange=exchange,
                    data_status=DataStatus.UNAVAILABLE,
                    tier=DataTier.UNAVAILABLE,
                    timestamp=None,
                    is_live=False,
                    is_simulated=False,
                    now=now,
                )
        return out
    return quotes


def fetch_historical_data(symbol, start=None, end=None, range_str="1y", interval="1d", exchange="NSE", now=None, provider=None):
    prov = provider or get_provider()
    return prov.get_historical_data(symbol, start=start, end=end, range_str=range_str,
                                    interval=interval, exchange=exchange, now=now)


def fetch_intraday_data(symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False, provider=None):
    prov = provider or get_provider()
    return prov.get_intraday_data(symbol, interval=interval, range_str=range_str,
                                  exchange=exchange, now=now, is_cas_eligible=is_cas_eligible)


def fetch_intraday_bars(symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False, provider=None):
    prov = provider or get_provider()
    return prov.get_intraday_bars(symbol, interval=interval, range_str=range_str, exchange=exchange, now=now, is_cas_eligible=is_cas_eligible)


def fetch_daily_bars(symbol, range_str="1y", exchange="NSE", now=None, provider=None):
    prov = provider or get_provider()
    return prov.get_daily_bars(symbol, range_str=range_str, exchange=exchange, now=now)


def fetch_instruments(exchange="NSE", symbol=None, provider=None):
    prov = provider or get_provider()
    return prov.get_instruments(exchange=exchange, symbol=symbol)


def fetch_option_chain(symbol, expiry=None, exchange="NSE", provider=None, spot_price=None, config=None, now=None):
    if provider is not None:
        prov = provider
    else:
        resolved = resolve_provider_name(config=config)
        prov_cls = _CUSTOM_REGISTRY.get(resolved) or _REGISTRY.get(resolved)
        if prov_cls is not None and resolved != "simulated" and prov_cls.CAPABILITIES.get(CAPABILITY_OPTION_CHAINS):
            prov = get_provider(resolved, config=config)
            if not prov.supports(CAPABILITY_OPTION_CHAINS):
                return prov.get_option_chain(symbol, expiry=expiry, exchange=exchange, spot_price=spot_price)
        else:
            prov = get_provider("simulated")
    return _call_with_optional_now(
        prov.get_option_chain, symbol, expiry=expiry, exchange=exchange, spot_price=spot_price, now=now
    )


def fetch_market_status(exchange="NSE", as_of=None, provider=None):
    prov = provider or get_provider()
    return prov.get_market_status(exchange=exchange, as_of=as_of)

