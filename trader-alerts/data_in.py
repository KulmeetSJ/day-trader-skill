"""Indian market data infrastructure for NSE and BSE equities.

Provides modular market data fetching, symbol translation, OHLCV bar normalization,
data quality tier classification, and error handling for BharatTrader AI.

Python 3.8 compatible without third-party dependencies.
"""

import datetime
import gzip
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


class DataTier:
    """Classification of market data quality, latency, and origin."""
    REAL_TIME = "REAL_TIME"    # Direct exchange/broker tick stream (un-delayed)
    DELAYED = "DELAYED"        # Delayed exchange feed (e.g. Yahoo Finance 15-minute delay)
    HISTORICAL = "HISTORICAL"  # Closed session completed bars (EOD or settled intraday)
    SIMULATED = "SIMULATED"    # Synthetic, mock, or backtest replay data


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


class MarketDataProvider:
    """Abstract base interface for Indian market data providers."""

    def get_quote(self, symbol, exchange="NSE"):
        """Fetch current or delayed quote for a single symbol."""
        raise NotImplementedError

    def get_quotes(self, symbols, exchange="NSE"):
        """Fetch quotes for a collection of symbols."""
        raise NotImplementedError

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        """Fetch historical intraday OHLCV bars aligned to Indian market sessions."""
        raise NotImplementedError

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        """Fetch daily historical OHLCV bars for screening and technical indicators."""
        raise NotImplementedError

    def get_option_chain(self, symbol, expiry=None, exchange="NSE"):
        """Fetch or construct option chain for underlying symbol and expiry."""
        raise NotImplementedError

    def get_derivatives_master(self, exchange="NSE"):
        """Fetch or construct derivatives master containing active contracts and lot sizes."""
        raise NotImplementedError


class YahooMarketDataProvider(MarketDataProvider):
    """Yahoo Finance provider for Indian equities.

    Retrieves quotes and historical bars with automatic multi-host fallback,
    polite backoff, rate-limit recovery, and explicit DELAYED tier labeling.
    """

    def __init__(self, hosts=YAHOO_HOSTS, user_agent=DEFAULT_USER_AGENT, timeout=DEFAULT_TIMEOUT,
                 pause=DEFAULT_PAUSE, backoff=DEFAULT_BACKOFF):
        self.hosts = hosts
        self.user_agent = user_agent
        self.timeout = timeout
        self.pause = pause
        self.backoff = backoff

    def _http_get(self, url):
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

    def get_quote(self, symbol, exchange="NSE"):
        canonical = symbols_in.normalize_symbol(symbol)
        provider_sym = symbols_in.to_yahoo_symbol(canonical, exchange=exchange)

        params = {"range": "1d", "interval": "1d"}
        text = self._fetch_chart_url(provider_sym, params)
        return self.normalize_quote(text, canonical, exchange=exchange, raw_symbol=provider_sym)

    def get_quotes(self, symbols, exchange="NSE"):
        quotes = {}
        for i, s in enumerate(symbols):
            if i > 0 and self.pause:
                time.sleep(self.pause)
            try:
                quotes[symbols_in.normalize_symbol(s)] = self.get_quote(s, exchange=exchange)
            except DataError as e:
                # Retain partial quotes while reporting errors per ticker
                quotes[symbols_in.normalize_symbol(s)] = {
                    "symbol": symbols_in.normalize_symbol(s),
                    "exchange": exchange,
                    "error": str(e),
                    "is_real_time": False,
                }
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

    def normalize_quote(self, text, symbol, exchange="NSE", raw_symbol=None):
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

        price = meta.get("regularMarketPrice")
        if price is None:
            # Fallback to last close in indicators
            q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
            closes = [c for c in (q.get("close") or []) if c is not None]
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

        rmt = meta.get("regularMarketTime")
        time_str = ""
        if rmt:
            dt_ist = datetime.datetime.fromtimestamp(rmt, tz=calendar_in.IST)
            time_str = dt_ist.strftime("%Y-%m-%d %H:%M:%S IST")

        return {
            "symbol": symbol,
            "raw_symbol": raw_symbol or symbol,
            "exchange": exchange,
            "currency": meta.get("currency", "INR"),
            "price": float(price),
            "prev_close": float(prev_close) if prev_close else None,
            "change": change,
            "change_pct": change_pct,
            "open": float(meta["regularMarketDayLow"]) if "regularMarketDayLow" in meta and False else meta.get("regularMarketOpen"),
            "high": float(meta["regularMarketDayHigh"]) if "regularMarketDayHigh" in meta else None,
            "low": float(meta["regularMarketDayLow"]) if "regularMarketDayLow" in meta else None,
            "volume": int(meta["regularMarketVolume"]) if meta.get("regularMarketVolume") is not None else None,
            "timestamp": int(rmt) if rmt else None,
            "time": time_str,
            "source": "yahoo",
            "tier": DataTier.DELAYED,
            "is_real_time": False,
        }

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
                "o": round(float(o), 2),
                "h": round(float(h), 2),
                "l": round(float(l), 2),
                "c": round(float(c), 2),
                "v": int(v or 0),
                "ts": ts,
            })

        return {
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval,
            "tier": DataTier.HISTORICAL,
            "source": "yahoo",
            "is_real_time": False,
            "bars": bars,
            "dropped": dropped,
            "meta": meta,
        }

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

        now_dt = calendar_in.parse_ist(now) if now else calendar_in.now_ist()
        today = now_dt.date()

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
            if d > today:
                continue
            # Store completed daily bar
            bars_by_date[d] = {
                "date": d.isoformat(),
                "o": round(float(o), 2),
                "h": round(float(h), 2),
                "l": round(float(l), 2),
                "c": round(float(c), 2),
                "v": int(v or 0),
                "ts": ts,
            }

        sorted_bars = [bars_by_date[k] for k in sorted(bars_by_date.keys())]
        return {
            "symbol": symbol,
            "exchange": exchange,
            "interval": "1d",
            "tier": DataTier.HISTORICAL,
            "source": "yahoo",
            "is_real_time": False,
            "bars": sorted_bars,
            "meta": meta,
        }

    def get_option_chain(self, symbol, expiry=None, exchange="NSE"):
        """Fetch option chain for Indian derivatives.

        Identifies provider capability limitation: Yahoo Finance does not provide
        reliable real-time option chains with open interest and Greeks for Indian equities/indices.
        """
        raise MissingDataError(
            "Yahoo Finance does not provide reliable Indian options chains with live OI and Greeks. "
            "Use SimulatedMarketDataProvider (simulated tier) or official broker adapters for options trading."
        )

    def get_derivatives_master(self, exchange="NSE"):
        """Fetch derivatives instrument master."""
        raise MissingDataError(
            "Yahoo Finance does not provide Indian derivatives instrument master. "
            "Use authoritative NSE/BSE metadata files or local instrument cache."
        )


class OfflineMarketDataProvider(MarketDataProvider):
    """Offline market data provider for tests, air-gapped environments, and replay.

    Reads saved JSON or JSON.GZ chart responses from a local folder or file mapping.
    """

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

    def get_quote(self, symbol, exchange="NSE"):
        canonical = symbols_in.normalize_symbol(symbol)
        path = self._locate_file(canonical, "quote")
        text = self._read_file(path)
        res = self.parser.normalize_quote(text, canonical, exchange=exchange)
        res["source"] = "offline"
        res["tier"] = self.tier
        return res

    def get_quotes(self, symbols, exchange="NSE"):
        quotes = {}
        for s in symbols:
            try:
                quotes[symbols_in.normalize_symbol(s)] = self.get_quote(s, exchange=exchange)
            except DataError as e:
                quotes[symbols_in.normalize_symbol(s)] = {
                    "symbol": symbols_in.normalize_symbol(s),
                    "exchange": exchange,
                    "error": str(e),
                    "is_real_time": False,
                }
        return quotes

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        canonical = symbols_in.normalize_symbol(symbol)
        suffix = interval
        path = self._locate_file(canonical, suffix)
        text = self._read_file(path)
        res = self.parser.normalize_intraday_bars(text, canonical, exchange=exchange, interval=interval,
                                                  now=now, is_cas_eligible=is_cas_eligible)
        res["source"] = "offline"
        res["tier"] = self.tier
        return res

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        canonical = symbols_in.normalize_symbol(symbol)
        path = self._locate_file(canonical, "daily")
        text = self._read_file(path)
        res = self.parser.normalize_daily_bars(text, canonical, exchange=exchange, now=now)
        res["source"] = "offline"
        res["tier"] = self.tier
        return res

    def get_option_chain(self, symbol, expiry=None, exchange="NSE"):
        canonical = symbols_in.normalize_symbol(symbol)
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
        return {
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
            ]
        }


class SimulatedMarketDataProvider(MarketDataProvider):
    """Generates synthetic, fully consistent Indian market data and options chains.

    Useful for paper trading, scenario replay, and testing derivatives strategies.
    Explicitly labeled with DataTier.SIMULATED.
    """

    def __init__(self, base_provider=None, default_iv=0.18, rate=0.07):
        self.base_provider = base_provider or YahooMarketDataProvider()
        self.default_iv = default_iv
        self.rate = rate

    def get_quote(self, symbol, exchange="NSE"):
        # If symbol is a derivative contract, resolve price using Black-Scholes
        if symbols_in.is_derivative_symbol(symbol):
            parsed = symbols_in.parse_derivative_symbol(symbol)
            underlying = parsed["underlying"]
            try:
                und_q = self.base_provider.get_quote(underlying, exchange=exchange)
                spot = und_q["price"]
            except Exception:
                spot = 24000.0 if underlying in ("NIFTY", "^NSEI") else 2800.0

            if parsed["asset_type"] == "FUTURES":
                price = round(spot * (1.0 + self.rate * (15.0 / 365.0)), 2)
                return {
                    "symbol": parsed["canonical_symbol"],
                    "exchange": exchange,
                    "price": price,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "tier": DataTier.SIMULATED,
                    "source": "simulated",
                    "is_real_time": False,
                }
            else:
                strike = parsed["strike"]
                opt_type = parsed["option_type"]
                exp_dt = calendar_in.get_next_expiry(underlying=underlying, exchange=exchange)
                days = max(1, calendar_in.days_to_expiry(exp_dt))
                t_years = days / 365.0
                bs = black_scholes(spot, strike, t_years, rate=self.rate, vol=self.default_iv, option_type=opt_type)
                return {
                    "symbol": parsed["canonical_symbol"],
                    "exchange": exchange,
                    "price": bs["price"],
                    "strike": strike,
                    "option_type": opt_type,
                    "expiry": exp_dt.strftime("%Y-%m-%d"),
                    "greeks": bs,
                    "tier": DataTier.SIMULATED,
                    "source": "simulated",
                    "is_real_time": False,
                }

        return self.base_provider.get_quote(symbol, exchange=exchange)

    def get_quotes(self, symbols, exchange="NSE"):
        return {s: self.get_quote(s, exchange=exchange) for s in symbols}

    def get_intraday_bars(self, symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False):
        return self.base_provider.get_intraday_bars(symbol, interval=interval, range_str=range_str,
                                                    exchange=exchange, now=now, is_cas_eligible=is_cas_eligible)

    def get_daily_bars(self, symbol, range_str="1y", exchange="NSE", now=None):
        return self.base_provider.get_daily_bars(symbol, range_str=range_str, exchange=exchange, now=now)

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

_REGISTRY = {
    "yahoo": YahooMarketDataProvider,
    "offline": OfflineMarketDataProvider,
    "simulated": SimulatedMarketDataProvider,
}


def register_provider(name, provider_cls):
    """Register a new market data provider class."""
    _REGISTRY[name.lower()] = provider_cls


def get_provider(name=None, source=None, **kwargs):
    """Factory to retrieve a configured market data provider instance.

    Checks environment variable BHARAT_DATA_PROVIDER (defaults to 'yahoo').
    """
    if name is None:
        name = source or os.environ.get("BHARAT_DATA_PROVIDER", "yahoo")
    name = str(name).lower()
    if name not in _REGISTRY:
        raise DataError("Unknown market data provider: '%s'. Available: %s" % (name, list(_REGISTRY.keys())))
    return _REGISTRY[name](**kwargs)


# ---------------------------------------------------------------- Engine Adapters & Technical Indicators

def to_scanner_series(daily_bars_payload):
    """Convert normalized daily bars into the series dict format expected by scan_daily."""
    bars = daily_bars_payload.get("bars", [])
    timestamps = [b["ts"] for b in bars]
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    volumes = [b["v"] for b in bars]
    meta = daily_bars_payload.get("meta") or {}
    if bars:
        meta["regularMarketPrice"] = closes[-1]
        meta["regularMarketVolume"] = volumes[-1]
    return {
        "t": timestamps,
        "c": closes,
        "h": highs,
        "l": lows,
        "v": volumes,
        "meta": meta,
    }


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

def fetch_quote(symbol, exchange="NSE", provider=None):
    prov = provider or get_provider()
    return prov.get_quote(symbol, exchange=exchange)


def fetch_quotes(symbols, exchange="NSE", provider=None):
    prov = provider or get_provider()
    return prov.get_quotes(symbols, exchange=exchange)


def fetch_intraday_bars(symbol, interval="5m", range_str="60d", exchange="NSE", now=None, is_cas_eligible=False, provider=None):
    prov = provider or get_provider()
    return prov.get_intraday_bars(symbol, interval=interval, range_str=range_str, exchange=exchange, now=now, is_cas_eligible=is_cas_eligible)


def fetch_daily_bars(symbol, range_str="1y", exchange="NSE", now=None, provider=None):
    prov = provider or get_provider()
    return prov.get_daily_bars(symbol, range_str=range_str, exchange=exchange, now=now)


def fetch_option_chain(symbol, expiry=None, exchange="NSE", provider=None, spot_price=None):
    prov = provider or get_provider("simulated")
    return prov.get_option_chain(symbol, expiry=expiry, exchange=exchange, spot_price=spot_price)

