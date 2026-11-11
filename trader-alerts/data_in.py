"""Indian market data infrastructure for NSE and BSE equities.

Provides modular market data fetching, symbol translation, OHLCV bar normalization,
data quality tier classification, and error handling for BharatTrader AI.

Python 3.8 compatible without third-party dependencies.
"""

import datetime
import gzip
import inspect
import json
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

import symbols_in
import calendar_in
import instruments

# User agent and provider constants
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; BharatTrader/1.0; +https://github.com/KulmeetSJ/day-trader-skill)"
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
DEFAULT_TIMEOUT = 10
DEFAULT_PAUSE = 0.3
DEFAULT_BACKOFF = 2.0


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
    def validate_config(cls, config=None, env=None):
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
                prov_creds.get(key)
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

    def __init__(self, base_path, tier=DataTier.HISTORICAL):
        self.base_path = base_path
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
                    doc = json.loads(self._read_file(cand))
                    raw_items = doc.get("instruments") if isinstance(doc, dict) else doc
                    break
        if raw_items is None and os.path.isdir(self.base_path):
            for fname in ("EQUITY_L.csv", "EQUITY_L_sample.csv"):
                cand = os.path.join(self.base_path, fname)
                if os.path.exists(cand):
                    try:
                        raw_items = instruments.parse_equity_l_csv(self._read_file(cand), exchange=exch)
                    except Exception:
                        raw_items = None
                    break
        if raw_items is None and os.path.exists(instruments.DEFAULT_CACHE_PATH):
            try:
                cache_payload = instruments.load_instrument_cache(max_age_days=3650)
                if cache_payload and cache_payload.get("instruments"):
                    raw_items = list(cache_payload["instruments"].values())
            except Exception:
                raw_items = None
        if raw_items is None:
            raw_items = list(instruments.INDEX_INSTRUMENTS.values())
        target_sym = symbols_in.normalize_symbol(symbol) if symbol else None
        for rec in (raw_items or []):
            if not isinstance(rec, dict):
                continue
            sym = symbols_in.normalize_symbol(rec.get("symbol") or "")
            rec_exch = _normalize_exchange(rec.get("exchange") or exch)
            if target_sym and sym != target_sym:
                continue
            items.append({
                "symbol": sym,
                "exchange": rec_exch,
                "instrument_id": "%s:%s" % (rec_exch, sym),
                "name": rec.get("name", ""),
                "asset_type": rec.get("asset_type", "EQUITY"),
                "lot_size": int(rec.get("lot_size") or 1),
                "tick_size": float(rec.get("tick_size") or 0.05),
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
        data = json.loads(text)
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


# ---------------------------------------------------------------- Provider Registry

_BUILTIN_PROVIDERS = {
    "yahoo": YahooMarketDataProvider,
    "offline": OfflineMarketDataProvider,
    "simulated": SimulatedMarketDataProvider,
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
    provider_cls.validate_config(config=cfg)
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


def fetch_option_chain(symbol, expiry=None, exchange="NSE", provider=None, spot_price=None):
    prov = provider or get_provider("simulated")
    return prov.get_option_chain(symbol, expiry=expiry, exchange=exchange, spot_price=spot_price)


def fetch_market_status(exchange="NSE", as_of=None, provider=None):
    prov = provider or get_provider()
    return prov.get_market_status(exchange=exchange, as_of=as_of)

