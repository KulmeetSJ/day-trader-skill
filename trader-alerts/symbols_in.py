"""Canonical symbol handling and adapter translation for Indian equities.

Preserves clean canonical scrip identifiers internally (e.g. RELIANCE, M&M, BAJAJ-AUTO).
Exchange-specific suffixes (.NS, .BO) are strictly encapsulated within provider adapters.
Python 3.8 compatible.
"""

import re

# Allowed characters in canonical Indian stock symbols: uppercase alphanumeric, &, -
SYMBOL_REGEX = re.compile(r"^[A-Z0-9&-]{1,20}$")
ISIN_REGEX = re.compile(r"^IN[A-Z0-9]{10}$")

KNOWN_SUFFIXES = (".NS", ".BO", "-EQ", ":NSE", ":BSE")


class SymbolValidationError(ValueError):
    """Raised when an Indian equity symbol fails exchange symbology constraints."""
    pass


def normalize_symbol(raw_symbol):
    """Normalize and sanitize a raw symbol string into its internal canonical form.

    Strips whitespace, converts to uppercase, and removes provider/exchange suffixes.
    Example: 'reliance.ns' -> 'RELIANCE', 'm&m' -> 'M&M', 'BAJAJ-AUTO-EQ' -> 'BAJAJ-AUTO'.
    """
    if not raw_symbol or not isinstance(raw_symbol, str):
        raise SymbolValidationError("Symbol must be a non-empty string")

    sym = raw_symbol.strip().upper()

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

    NSE equities use suffix '.NS' (e.g. 'RELIANCE.NS').
    BSE equities use suffix '.BO' (e.g. 'RELIANCE.BO').
    """
    sym = normalize_symbol(canonical_symbol)
    exch = exchange.upper().strip()
    if exch == "BSE":
        return sym + ".BO"
    return sym + ".NS"


def from_yahoo_symbol(yahoo_symbol):
    """Convert a Yahoo Finance ticker back to canonical Indian symbol.

    Example: 'RELIANCE.NS' -> ('RELIANCE', 'NSE'), '500325.BO' -> ('500325', 'BSE').
    """
    if not yahoo_symbol or not isinstance(yahoo_symbol, str):
        raise SymbolValidationError("Yahoo symbol must be a non-empty string")
    clean = yahoo_symbol.strip().upper()
    if clean.endswith(".BO"):
        return normalize_symbol(clean[:-3]), "BSE"
    if clean.endswith(".NS"):
        return normalize_symbol(clean[:-3]), "NSE"
    return normalize_symbol(clean), "NSE"

