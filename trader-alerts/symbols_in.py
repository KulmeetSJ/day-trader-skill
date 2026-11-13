"""Canonical symbol handling and adapter translation for Indian equities.

Preserves clean canonical scrip identifiers internally (e.g. RELIANCE, M&M, BAJAJ-AUTO).
Exchange-specific suffixes (.NS, .BO) are strictly encapsulated within provider adapters.
Python 3.8 compatible.
"""

import datetime
import re

# Allowed characters in canonical Indian stock symbols: uppercase alphanumeric, &, -
SYMBOL_REGEX = re.compile(r"^[A-Z0-9&-]{1,25}$")
ISIN_REGEX = re.compile(r"^IN[A-Z0-9]{10}$")

KNOWN_SUFFIXES = (".NS", ".BO", "-EQ", ":NSE", ":BSE")

# Authoritative Indian benchmark indices (spot analytical instruments, not tradable cash equities)
BENCHMARK_INDICES = (
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "NIFTY100",
    "NIFTY200",
    "NIFTY500",
    "SENSEX",
    "BANKEX",
)

# Canonical index mappings, Upstox instrument keys (from BOD master), and Yahoo Finance tickers
INDEX_MAP = {
    "NIFTY": {
        "exchange": "NSE",
        "yahoo": "^NSEI",
        "upstox_key": "NSE_INDEX|Nifty 50",
        "name": "NIFTY 50",
        "has_derivatives": True,
        "aliases": ("NIFTY50", "NIFTY 50", "^NSEI", "NSE_INDEX|NIFTY 50"),
    },
    "BANKNIFTY": {
        "exchange": "NSE",
        "yahoo": "^NSEBANK",
        "upstox_key": "NSE_INDEX|Nifty Bank",
        "name": "NIFTY BANK",
        "has_derivatives": True,
        "aliases": ("NIFTYBANK", "NIFTY BANK", "^NSEBANK", "NSE_INDEX|NIFTY BANK"),
    },
    "FINNIFTY": {
        "exchange": "NSE",
        "yahoo": "NIFTY_FIN_SERVICE.NS",
        "upstox_key": "NSE_INDEX|Nifty Fin Service",
        "name": "NIFTY FINANCIAL SERVICES",
        "has_derivatives": True,
        "aliases": (
            "NIFTYFIN",
            "NIFTY FIN SERVICE",
            "NIFTY FINANCIAL SERVICES",
            "NIFTY_FIN_SERVICE.NS",
            "^CNXFIN",
            "NSE_INDEX|NIFTY FIN SERVICE",
        ),
    },
    "MIDCPNIFTY": {
        "exchange": "NSE",
        "yahoo": "NIFTY_MID_SELECT.NS",
        "upstox_key": "NSE_INDEX|NIFTY MID SELECT",
        "name": "NIFTY MIDCAP SELECT",
        "has_derivatives": True,
        "aliases": (
            "MIDCPNIFTY",
            "NIFTY MIDCAP SELECT",
            "NIFTY MID SELECT",
            "NIFTY_MID_SELECT.NS",
            "NSE_INDEX|NIFTY MID SELECT",
        ),
    },
    "NIFTYNXT50": {
        "exchange": "NSE",
        "yahoo": "^NSMIDCP",
        "upstox_key": "NSE_INDEX|Nifty Next 50",
        "name": "NIFTY NEXT 50",
        "has_derivatives": True,
        "aliases": (
            "NIFTYNXT50",
            "NIFTYNEXT50",
            "NIFTY NEXT 50",
            "NIFTY JUNIOR",
            "NIFTY JR",
            "^NSMIDCP",
            "NSE_INDEX|NIFTY NEXT 50",
        ),
    },
    "NIFTY100": {
        "exchange": "NSE",
        "yahoo": "^CNX100",
        "upstox_key": "NSE_INDEX|Nifty 100",
        "name": "NIFTY 100",
        "has_derivatives": False,
        "aliases": ("NIFTY100", "NIFTY 100", "^CNX100", "NSE_INDEX|NIFTY 100"),
    },
    "NIFTY200": {
        "exchange": "NSE",
        "yahoo": "^CNX200",
        "upstox_key": "NSE_INDEX|Nifty 200",
        "name": "NIFTY 200",
        "has_derivatives": False,
        "aliases": ("NIFTY200", "NIFTY 200", "^CNX200", "NSE_INDEX|NIFTY 200"),
    },
    "NIFTY500": {
        "exchange": "NSE",
        "yahoo": "^CRSLDX",
        "upstox_key": "NSE_INDEX|Nifty 500",
        "name": "NIFTY 500",
        "has_derivatives": False,
        "aliases": ("NIFTY500", "NIFTY 500", "^CRSLDX", "NSE_INDEX|NIFTY 500"),
    },
    "SENSEX": {
        "exchange": "BSE",
        "yahoo": "^BSESN",
        "upstox_key": "BSE_INDEX|SENSEX",
        "name": "BSE SENSEX",
        "has_derivatives": True,
        "aliases": ("BSESN", "^BSESN", "BSE SENSEX", "SENSEX 30", "BSE_INDEX|SENSEX"),
    },
    "BANKEX": {
        "exchange": "BSE",
        "yahoo": "BSE-BANK.BO",
        "upstox_key": "BSE_INDEX|BANKEX",
        "name": "BSE BANKEX",
        "has_derivatives": True,
        "aliases": ("BSEBANK", "BSE BANKEX", "BSE-BANK.BO", "BSE_INDEX|BANKEX"),
    },
    "INDIAVIX": {
        "exchange": "NSE",
        "yahoo": "^INDIAVIX",
        "upstox_key": "NSE_INDEX|India VIX",
        "name": "INDIA VIX",
        "has_derivatives": False,
        "aliases": ("INDIAVIX", "INDIA VIX", "^INDIAVIX", "NSE_INDEX|INDIA VIX"),
    },
}

# Derivatives Regex
FUT_REGEX = re.compile(r"^(?P<underlying>[A-Z0-9&-]+?)(?P<yy>\d{2})(?P<month>[A-Z]{3})FUT$")
OPT_REGEX = re.compile(r"^(?P<underlying>[A-Z0-9&-]+?)(?P<yy>\d{2})(?P<month>[A-Z]{3})(?P<strike>\d+(?:\.\d+)?)(?P<option_type>CE|PE)$")
OPT_WEEKLY_REGEX = re.compile(r"^(?P<underlying>[A-Z0-9&-]+?)(?P<yy>\d{2})(?P<month_code>[1-9OND])(?P<day>\d{2})(?P<strike>\d+(?:\.\d+)?)(?P<option_type>CE|PE)$")

MONTH_NAMES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
MONTH_CODES = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "O": 10, "N": 11, "D": 12
}
REVERSE_MONTH_CODES = {v: k for k, v in MONTH_CODES.items()}


class SymbolValidationError(ValueError):
    """Raised when an Indian equity symbol fails exchange symbology constraints."""
    pass


def normalize_index_name(raw_index):
    """Normalize index name or alias to canonical index key (e.g. 'NIFTY 50' -> 'NIFTY')."""
    if not raw_index or not isinstance(raw_index, str):
        return None
    clean = " ".join(raw_index.strip().upper().split())
    if ":" in clean:
        parts = clean.split(":", 1)
        if parts[0] in ("NSE", "BSE"):
            clean = parts[1].strip()
    if clean in INDEX_MAP:
        return clean
    for canon, meta in INDEX_MAP.items():
        if clean == meta["name"].upper() or clean == meta["yahoo"].upper():
            return canon
        if clean == str(meta.get("upstox_key") or "").upper():
            return canon
        for alias in meta["aliases"]:
            if clean == alias.upper():
                return canon
    return None


def is_index_symbol(symbol):
    """Return True if symbol corresponds to a known index."""
    return normalize_index_name(symbol) is not None


def get_index_metadata(symbol):
    """Return canonical index metadata copy for a known index, else None."""
    canon = normalize_index_name(symbol)
    if not canon:
        return None
    meta = dict(INDEX_MAP[canon])
    meta["canonical_symbol"] = canon
    return meta


def list_benchmark_indices(exchange=None):
    """Return list of canonical benchmark index symbols, optionally filtered by exchange."""
    ex = str(exchange or "").strip().upper()
    if not ex or ex == "ALL":
        return list(BENCHMARK_INDICES)
    return [sym for sym in BENCHMARK_INDICES if INDEX_MAP[sym]["exchange"] == ex]


def normalize_symbol(raw_symbol):
    """Normalize and sanitize a raw symbol string into its internal canonical form.

    Strips whitespace, converts to uppercase, and removes provider/exchange suffixes.
    Example: 'reliance.ns' -> 'RELIANCE', 'm&m' -> 'M&M', 'BAJAJ-AUTO-EQ' -> 'BAJAJ-AUTO'.
    Also normalizes index aliases: '^NSEI' -> 'NIFTY', '^BSESN' -> 'SENSEX'.
    """
    if not raw_symbol or not isinstance(raw_symbol, str):
        raise SymbolValidationError("Symbol must be a non-empty string")

    sym = raw_symbol.strip().upper()

    # Check for index alias first
    idx = normalize_index_name(sym)
    if idx:
        return idx

    # Remove known suffixes if present (using Python 3.8 compatible slice)
    for suffix in KNOWN_SUFFIXES:
        if sym.endswith(suffix):
            sym = sym[:-len(suffix)]
            break

    # If exchange prefix was provided, e.g. NSE:RELIANCE
    if ":" in sym:
        parts = sym.split(":")
        if len(parts) == 2 and parts[0] in ("NSE", "BSE"):
            sym = parts[1]

    sym = sym.strip()
    idx = normalize_index_name(sym)
    if idx:
        return idx

    if not SYMBOL_REGEX.match(sym):
        raise SymbolValidationError("Invalid Indian equity symbol format: %r" % raw_symbol)

    return sym


def validate_isin(isin):
    """Validate 12-character Indian ISIN string (e.g. INE002A01018)."""
    if not isin or not isinstance(isin, str):
        return False
    clean = isin.strip().upper()
    return bool(ISIN_REGEX.match(clean))


def to_yahoo_symbol(canonical_symbol, exchange="NSE"):
    """Convert canonical Indian symbol to Yahoo Finance format.

    Handles indices: 'NIFTY' -> '^NSEI', 'SENSEX' -> '^BSESN'.
    NSE equities use suffix '.NS' (e.g. 'RELIANCE.NS').
    BSE equities use suffix '.BO' (e.g. 'RELIANCE.BO').
    """
    clean = (canonical_symbol or "").strip()
    idx = normalize_index_name(clean)
    if idx:
        return INDEX_MAP[idx]["yahoo"]
    if clean.startswith("^"):
        return clean

    sym = normalize_symbol(canonical_symbol)
    exch = exchange.upper().strip()
    if exch == "BSE":
        return sym + ".BO"
    return sym + ".NS"


def from_yahoo_symbol(yahoo_symbol):
    """Convert a Yahoo Finance ticker back to canonical Indian symbol.

    Example: 'RELIANCE.NS' -> ('RELIANCE', 'NSE'), '^NSEI' -> ('NIFTY', 'NSE').
    """
    if not yahoo_symbol or not isinstance(yahoo_symbol, str):
        raise SymbolValidationError("Yahoo symbol must be a non-empty string")
    clean = yahoo_symbol.strip().upper()
    for canon, meta in INDEX_MAP.items():
        if clean == meta["yahoo"].upper():
            return canon, meta["exchange"]

    if clean.endswith(".BO"):
        return normalize_symbol(clean[:-3]), "BSE"
    if clean.endswith(".NS"):
        return normalize_symbol(clean[:-3]), "NSE"
    return normalize_symbol(clean), "NSE"


def is_derivative_symbol(symbol):
    """Check if symbol string represents an Indian futures or options contract."""
    if not symbol or not isinstance(symbol, str):
        return False
    clean = symbol.strip().upper()
    return bool(FUT_REGEX.match(clean) or OPT_REGEX.match(clean) or OPT_WEEKLY_REGEX.match(clean))


def parse_derivative_symbol(symbol):
    """Parse standard Indian derivative symbol into constituent parts.

    Supports:
    - Stock & Index Futures (e.g. 'NIFTY26NOVFUT', 'RELIANCE26NOVFUT')
    - Monthly Options (e.g. 'NIFTY26NOV24000CE', 'TCS26NOV3500PE')
    - Weekly Options (e.g. 'NIFTY26N1924000CE', 'BANKNIFTY26O2750000PE')

    Returns dict with keys:
    - canonical_symbol: str
    - underlying: str
    - asset_type: 'FUTURES' | 'OPTIONS'
    - expiry_yy: int (e.g. 26 -> 2026)
    - expiry_month: int (1-12)
    - expiry_month_str: str (e.g. 'NOV')
    - expiry_day: int or None
    - strike: float or None
    - option_type: 'CE' | 'PE' | None
    """
    if not symbol or not isinstance(symbol, str):
        raise SymbolValidationError("Symbol must be a non-empty string")
    clean = symbol.strip().upper()

    # 1. Futures
    m = FUT_REGEX.match(clean)
    if m:
        underlying = m.group("underlying")
        yy = int(m.group("yy")) + 2000
        month_str = m.group("month")
        if month_str not in MONTH_NAMES:
            raise SymbolValidationError("Invalid month in futures contract: %s" % month_str)
        month_num = MONTH_NAMES.index(month_str) + 1
        import calendar
        last_day = calendar.monthrange(yy, month_num)[1]
        target_weekday = 3 if underlying in ("SENSEX", "BANKEX") else 1
        offset = (datetime.date(yy, month_num, last_day).weekday() - target_weekday) % 7
        exp_date = "%04d-%02d-%02d" % (yy, month_num, last_day - offset)
        return {
            "canonical_symbol": clean,
            "underlying": underlying,
            "asset_type": "FUTURES",
            "instrument_type": "FUT",
            "expiry_yy": yy,
            "expiry_month": month_num,
            "expiry_month_str": month_str,
            "expiry_day": None,
            "expiry_date": exp_date,
            "strike": None,
            "option_type": None,
        }

    # 2. Monthly Options
    m = OPT_REGEX.match(clean)
    if m:
        underlying = m.group("underlying")
        yy = int(m.group("yy")) + 2000
        month_str = m.group("month")
        if month_str not in MONTH_NAMES:
            raise SymbolValidationError("Invalid month in options contract: %s" % month_str)
        month_num = MONTH_NAMES.index(month_str) + 1
        strike = float(m.group("strike"))
        option_type = m.group("option_type")
        import calendar
        last_day = calendar.monthrange(yy, month_num)[1]
        target_weekday = 3 if underlying in ("SENSEX", "BANKEX") else 1
        offset = (datetime.date(yy, month_num, last_day).weekday() - target_weekday) % 7
        exp_date = "%04d-%02d-%02d" % (yy, month_num, last_day - offset)
        return {
            "canonical_symbol": clean,
            "underlying": underlying,
            "asset_type": "OPTIONS",
            "instrument_type": "OPT",
            "expiry_yy": yy,
            "expiry_month": month_num,
            "expiry_month_str": month_str,
            "expiry_day": None,
            "expiry_date": exp_date,
            "strike": strike,
            "option_type": option_type,
        }

    # 3. Weekly Options
    m = OPT_WEEKLY_REGEX.match(clean)
    if m:
        underlying = m.group("underlying")
        yy = int(m.group("yy")) + 2000
        code = m.group("month_code")
        month_num = MONTH_CODES.get(code)
        if not month_num:
            raise SymbolValidationError("Invalid weekly month code: %s" % code)
        month_str = MONTH_NAMES[month_num - 1]
        day = int(m.group("day"))
        strike = float(m.group("strike"))
        option_type = m.group("option_type")
        exp_date = "%04d-%02d-%02d" % (yy, month_num, day)
        return {
            "canonical_symbol": clean,
            "underlying": underlying,
            "asset_type": "OPTIONS",
            "instrument_type": "OPT",
            "expiry_yy": yy,
            "expiry_month": month_num,
            "expiry_month_str": month_str,
            "expiry_day": day,
            "expiry_date": exp_date,
            "strike": strike,
            "option_type": option_type,
        }

    raise SymbolValidationError("Not a recognized Indian derivative contract: %s" % symbol)


def format_derivative_symbol(underlying, expiry_date, strike=None, option_type=None, is_weekly=False):
    """Format canonical NSE derivative contract symbol.

    Examples:
    - ('NIFTY', '2026-11-26', None, None) -> 'NIFTY26NOVFUT'
    - ('NIFTY', '2026-11-26', 24000, 'CE') -> 'NIFTY26NOV24000CE'
    - ('NIFTY', '2026-11-19', 24000, 'CE', is_weekly=True) -> 'NIFTY26N1924000CE'
    """
    u = normalize_symbol(underlying)
    if isinstance(expiry_date, str):
        dt = datetime.datetime.strptime(expiry_date, "%Y-%m-%d").date()
    elif isinstance(expiry_date, (datetime.datetime, datetime.date)):
        dt = expiry_date if isinstance(expiry_date, datetime.date) else expiry_date.date()
    else:
        raise ValueError("Invalid expiry date: %r" % expiry_date)

    yy = "%02d" % (dt.year % 100)
    month_str = MONTH_NAMES[dt.month - 1]

    if strike is None and option_type is None:
        # Futures contract
        return "%s%s%sFUT" % (u, yy, month_str)

    # Option contract
    ot = str(option_type).upper().strip()
    if ot not in ("CE", "PE"):
        raise ValueError("Option type must be 'CE' or 'PE'")

    strike_str = ("%d" % strike) if float(strike).is_integer() else ("%.2f" % strike)

    if is_weekly:
        m_code = REVERSE_MONTH_CODES.get(dt.month, str(dt.month))
        day_str = "%02d" % dt.day
        return "%s%s%s%s%s%s" % (u, yy, m_code, day_str, strike_str, ot)
    else:
        return "%s%s%s%s%s" % (u, yy, month_str, strike_str, ot)

