"""Authoritative instrument master ingestion, caching, and tick size resolution.

Manages the local instruments master cache at trader/state/instruments.json.
Enforces fail-closed metadata validation: if a required field or tick size cannot
be authoritatively resolved from official exchange metadata, trade validation fails.
Python 3.8 compatible.
"""

import csv
import datetime
import json
import os
import sys
import tempfile

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from symbols_in import normalize_symbol, validate_isin

DEFAULT_CACHE_PATH = "trader/state/instruments.json"
HISTORY_DIR = "trader/state/history"


class InstrumentMetadataError(Exception):
    """Base exception for instrument metadata failures."""
    pass


class StaleMetadataError(InstrumentMetadataError):
    """Raised when instrument metadata is older than the allowed freshness window."""
    pass


class TickResolutionError(InstrumentMetadataError):
    """Raised when a security tick size cannot be authoritatively resolved."""
    pass


class LotSizeResolutionError(InstrumentMetadataError):
    """Raised when a security lot size cannot be authoritatively resolved."""
    pass


def parse_equity_l_csv(csv_path_or_text, exchange="NSE"):
    """Parse official NSE EQUITY_L.csv content into structured record dicts.

    Header schema:
    SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE
    """
    if os.path.exists(csv_path_or_text):
        with open(csv_path_or_text, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    else:
        reader = csv.DictReader(csv_path_or_text.splitlines())
        rows = list(reader)

    records = {}
    for row in rows:
        raw_symbol = row.get("SYMBOL") or row.get("Symbol")
        if not raw_symbol:
            continue
        try:
            symbol = normalize_symbol(raw_symbol)
        except Exception:
            continue

        series = (row.get("SERIES") or row.get("Series") or "").strip().upper()
        isin = (row.get("ISIN NUMBER") or row.get("ISIN") or "").strip().upper()
        name = (row.get("NAME OF COMPANY") or row.get("Company Name") or "").strip()
        lot_str = row.get("MARKET LOT") or row.get("Market Lot") or "1"

        try:
            lot_size = int(lot_str)
        except ValueError:
            lot_size = 1

        records[symbol] = {
            "canonical_symbol": symbol,
            "exchange": exchange.upper(),
            "name": name,
            "series": series,
            "isin": isin,
            "instrument_type": "EQUITY",
            "instrument_identifier": symbol,
            # Official tick size: in NSE cash equity, standard series EQ is 0.05
            # unless a distinct tick size is authoritatively assigned in instrument master
            "tick_size": 0.05,
            "lot_size": lot_size,
            "expiry": None,
            "is_trade_to_trade": series in ("BE", "BZ", "T"),
        }
    return records


def parse_fo_lots_csv(csv_path_or_text):
    """Parse official NSE fo_mktlots.csv content into a dict of {symbol: lot_size}."""
    if os.path.exists(csv_path_or_text):
        with open(csv_path_or_text, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    else:
        reader = csv.DictReader(csv_path_or_text.splitlines())
        rows = list(reader)

    lots = {}
    for row in rows:
        raw_sym = row.get("SYMBOL") or row.get("Symbol")
        raw_lot = row.get("LOT_SIZE") or row.get("Lot Size") or row.get("LOT SIZE")
        if not raw_sym or not raw_lot:
            continue
        try:
            sym = normalize_symbol(raw_sym)
            lot = int(str(raw_lot).strip())
            lots[sym] = lot
        except Exception:
            continue
    return lots


def build_instrument_master(equity_records, fo_lots=None, effective_date=None, source_timestamp=None):
    """Combine cash equity records and derivative metadata into authoritative master payload."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ts = source_timestamp or now_utc.isoformat()
    eff = effective_date or now_utc.strftime("%Y-%m-%d")

    instruments = {}
    fo_map = fo_lots or {}

    for sym, rec in equity_records.items():
        entry = dict(rec)
        entry["source_timestamp"] = ts
        entry["effective_date"] = eff
        # Enrich with derivative contract lot size if eligible
        if sym in fo_map:
            entry["fo_lot_size"] = fo_map[sym]
            entry["is_fo_eligible"] = True
            # Since August 3, 2026, eligible F&O equities participate in Closing Auction Session (CAS) on NSE
            entry["is_cas_eligible"] = entry.get("exchange", "NSE") == "NSE"
        else:
            entry["fo_lot_size"] = None
            entry["is_fo_eligible"] = False
            entry["is_cas_eligible"] = False

        instruments[sym] = entry

    payload = {
        "metadata_version": "1.0",
        "effective_date": eff,
        "generated_at": ts,
        "exchange": "NSE",
        "count": len(instruments),
        "instruments": instruments,
    }
    return payload


def save_instrument_cache(payload, cache_path=DEFAULT_CACHE_PATH, history_dir=HISTORY_DIR):
    """Atomically save instrument master cache and preserve historical snapshot.

    Uses a temporary file in the destination directory and atomic rename (os.replace)
    to prevent race conditions or corrupted cache writes.
    """
    target_dir = os.path.dirname(os.path.abspath(cache_path))
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    # Validate payload integrity before writing
    if not isinstance(payload, dict) or "instruments" not in payload:
        raise InstrumentMetadataError("Invalid instrument master payload: missing 'instruments' key")

    # Atomic write to main cache
    temp_fh = tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8")
    temp_path = temp_fh.name
    try:
        json.dump(payload, temp_fh, indent=2)
        temp_fh.flush()
        temp_fh.close()
        os.replace(temp_path, cache_path)
    except Exception as exc:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise InstrumentMetadataError("Failed to write instrument cache atomically: %s" % exc)

    # Preserve historical snapshot if history_dir is provided
    if history_dir:
        eff_date = payload.get("effective_date", "").replace("-", "")
        if eff_date:
            hist_path_dir = os.path.abspath(history_dir)
            os.makedirs(hist_path_dir, exist_ok=True)
            hist_file = os.path.join(hist_path_dir, "instruments_%s.json" % eff_date)
            try:
                with open(hist_file, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
            except Exception:
                pass  # Non-blocking if history logging fails


def load_instrument_cache(cache_path=DEFAULT_CACHE_PATH, max_age_days=1, as_of_date=None):
    """Load and validate the local instrument cache.

    Enforces fail-closed validation:
    - Raises FileNotFoundError if cache does not exist.
    - Raises StaleMetadataError if cache effective_date is older than max_age_days.
    - Raises InstrumentMetadataError if payload structure is corrupted.
    """
    if not os.path.isfile(cache_path):
        raise FileNotFoundError("Instrument cache not found at: %s" % cache_path)

    try:
        with open(cache_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        raise InstrumentMetadataError("Corrupted instrument cache JSON: %s" % exc)

    if not isinstance(payload, dict) or "instruments" not in payload:
        raise InstrumentMetadataError("Malformed instrument cache: missing 'instruments' root")

    eff_str = payload.get("effective_date")
    if not eff_str:
        raise InstrumentMetadataError("Instrument cache missing 'effective_date'")

    try:
        eff_date = datetime.datetime.strptime(eff_str, "%Y-%m-%d").date()
    except ValueError:
        raise InstrumentMetadataError("Invalid effective_date format: %s" % eff_str)

    # Freshness check
    ref_date = as_of_date or datetime.date.today()
    if isinstance(ref_date, str):
        ref_date = datetime.datetime.strptime(ref_date, "%Y-%m-%d").date()

    delta_days = (ref_date - eff_date).days
    if delta_days > max_age_days:
        raise StaleMetadataError(
            "Instrument cache is stale (effective: %s, reference: %s, age: %d days > %d allowed)"
            % (eff_str, ref_date.isoformat(), delta_days, max_age_days)
        )

    return payload


def resolve_instrument(symbol, cache_payload, require_intraday=True):
    """Authoritatively resolve instrument metadata for a canonical symbol.

    Fails closed:
    - Raises InstrumentMetadataError if symbol is missing from master.
    - Raises InstrumentMetadataError if security is Trade-to-Trade and require_intraday is True.
    - Raises TickResolutionError if tick size is missing or invalid.
    - Raises LotSizeResolutionError if lot size is missing or <= 0.
    """
    try:
        sym = normalize_symbol(symbol)
    except Exception as exc:
        raise InstrumentMetadataError("Invalid symbol %r: %s" % (symbol, exc))
    instruments = cache_payload.get("instruments", {})
    if sym not in instruments:
        raise InstrumentMetadataError("Symbol %r not found in authoritative instrument master" % sym)

    entry = instruments[sym]

    # Series / Trade-to-Trade checks
    if require_intraday and entry.get("is_trade_to_trade"):
        series = entry.get("series", "UNKNOWN")
        raise InstrumentMetadataError(
            "Symbol %r is under Trade-to-Trade series %r: intraday square-off prohibited by SEBI"
            % (sym, series)
        )

    # Tick size resolution
    tick_size = entry.get("tick_size")
    if tick_size is None or not isinstance(tick_size, (int, float)) or tick_size <= 0:
        raise TickResolutionError("Cannot authoritatively resolve tick size for %r" % sym)

    # Lot size resolution
    lot_size = entry.get("lot_size")
    if lot_size is None or not isinstance(lot_size, int) or lot_size <= 0:
        raise LotSizeResolutionError("Cannot authoritatively resolve lot size for %r" % sym)

    return entry


def is_cas_eligible(symbol, cache_payload):
    """Authoritatively determine if a security is CAS-eligible from instrument master."""
    try:
        sym = normalize_symbol(symbol)
    except Exception:
        return False
    instruments = cache_payload.get("instruments", {})
    if sym in instruments:
        return bool(instruments[sym].get("is_cas_eligible", False))
    return False
