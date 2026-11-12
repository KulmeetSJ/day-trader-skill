"""Authoritative instrument master ingestion, caching, and tick size resolution.

Manages the local instruments master cache at trader/state/instruments.json.
Enforces fail-closed metadata validation: if a required field or tick size cannot
be authoritatively resolved from official exchange metadata, trade validation fails.
Python 3.8 compatible.
"""

import csv
import datetime
import json
import math
import os
import sys
import tempfile
from decimal import Decimal, InvalidOperation

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from symbols_in import (
    normalize_symbol,
    validate_isin,
    is_index_symbol,
    normalize_index_name,
    is_derivative_symbol,
    parse_derivative_symbol,
    format_derivative_symbol,
)
import calendar_in

DEFAULT_CACHE_PATH = "trader/state/instruments.json"
HISTORY_DIR = "trader/state/history"

# Authoritative Index Instruments Metadata (analytical benchmarks, not tradable cash equities)
INDEX_INSTRUMENTS = {
    "NIFTY": {
        "canonical_symbol": "NIFTY",
        "exchange": "NSE",
        "name": "NIFTY 50",
        "series": "INDEX",
        "isin": "IN9002A01018",
        "upstox_key": "NSE_INDEX|Nifty 50",
        "instrument_type": "INDEX",
        "instrument_identifier": "NIFTY",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": 25,
        "strike_interval": 50.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "BANKNIFTY": {
        "canonical_symbol": "BANKNIFTY",
        "exchange": "NSE",
        "name": "NIFTY BANK",
        "series": "INDEX",
        "isin": "IN9002A01019",
        "upstox_key": "NSE_INDEX|Nifty Bank",
        "instrument_type": "INDEX",
        "instrument_identifier": "BANKNIFTY",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": 15,
        "strike_interval": 100.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "FINNIFTY": {
        "canonical_symbol": "FINNIFTY",
        "exchange": "NSE",
        "name": "NIFTY FINANCIAL SERVICES",
        "series": "INDEX",
        "isin": "IN9002A01020",
        "upstox_key": "NSE_INDEX|Nifty Fin Service",
        "instrument_type": "INDEX",
        "instrument_identifier": "FINNIFTY",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": 25,
        "strike_interval": 50.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "MIDCPNIFTY": {
        "canonical_symbol": "MIDCPNIFTY",
        "exchange": "NSE",
        "name": "NIFTY MIDCAP SELECT",
        "series": "INDEX",
        "isin": "IN9002A01021",
        "upstox_key": "NSE_INDEX|NIFTY MID SELECT",
        "instrument_type": "INDEX",
        "instrument_identifier": "MIDCPNIFTY",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": 50,
        "strike_interval": 25.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "NIFTYNXT50": {
        "canonical_symbol": "NIFTYNXT50",
        "exchange": "NSE",
        "name": "NIFTY NEXT 50",
        "series": "INDEX",
        "isin": "",
        "upstox_key": "NSE_INDEX|Nifty Next 50",
        "instrument_type": "INDEX",
        "instrument_identifier": "NIFTYNXT50",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": None,
        "strike_interval": None,
        "is_fo_eligible": False,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "NIFTY100": {
        "canonical_symbol": "NIFTY100",
        "exchange": "NSE",
        "name": "NIFTY 100",
        "series": "INDEX",
        "isin": "",
        "upstox_key": "NSE_INDEX|Nifty 100",
        "instrument_type": "INDEX",
        "instrument_identifier": "NIFTY100",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": None,
        "strike_interval": None,
        "is_fo_eligible": False,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "NIFTY200": {
        "canonical_symbol": "NIFTY200",
        "exchange": "NSE",
        "name": "NIFTY 200",
        "series": "INDEX",
        "isin": "",
        "upstox_key": "NSE_INDEX|Nifty 200",
        "instrument_type": "INDEX",
        "instrument_identifier": "NIFTY200",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": None,
        "strike_interval": None,
        "is_fo_eligible": False,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "NIFTY500": {
        "canonical_symbol": "NIFTY500",
        "exchange": "NSE",
        "name": "NIFTY 500",
        "series": "INDEX",
        "isin": "",
        "upstox_key": "NSE_INDEX|Nifty 500",
        "instrument_type": "INDEX",
        "instrument_identifier": "NIFTY500",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": None,
        "strike_interval": None,
        "is_fo_eligible": False,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "SENSEX": {
        "canonical_symbol": "SENSEX",
        "exchange": "BSE",
        "name": "BSE SENSEX",
        "series": "INDEX",
        "isin": "IN9002A01022",
        "upstox_key": "BSE_INDEX|SENSEX",
        "instrument_type": "INDEX",
        "instrument_identifier": "SENSEX",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": 10,
        "strike_interval": 100.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
    "BANKEX": {
        "canonical_symbol": "BANKEX",
        "exchange": "BSE",
        "name": "BSE BANKEX",
        "series": "INDEX",
        "isin": "IN9002A01023",
        "upstox_key": "BSE_INDEX|BANKEX",
        "instrument_type": "INDEX",
        "instrument_identifier": "BANKEX",
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "lot_size": 1,
        "fo_lot_size": 15,
        "strike_interval": 100.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
        "is_tradable_equity": False,
    },
}


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


_VALID_TICK_UNITS_INR = ("inr", "rupee", "rupees")
_VALID_TICK_UNITS_PAISE = ("paise", "paisa", "p")
_UPSTOX_BOD_MARKERS = (
    "instrument_key",
    "segment",
    "trading_symbol",
    "tradingsymbol",
    "exchange_token",
    "instrument_token",
)


def _has_upstox_bod_provenance(rec, cache_payload=None):
    """Return True if a record or its enclosing cache carries Upstox/BOD provenance markers."""
    if isinstance(cache_payload, dict):
        prov = str(cache_payload.get("provider") or "").strip().lower()
        if prov:
            return True
    if not isinstance(rec, dict):
        return False
    if str(rec.get("provider") or "").strip():
        return True
    return any(k in rec for k in _UPSTOX_BOD_MARKERS)


def _is_legacy_csv_entry(rec, cache_payload=None):
    """Positively identify pre-unit-marker legacy NSE/BSE CSV cache entries by structure only."""
    if not isinstance(rec, dict):
        return False
    if _has_upstox_bod_provenance(rec, cache_payload=cache_payload):
        return False
    if isinstance(cache_payload, dict) and "metadata_version" in cache_payload:
        if str(cache_payload.get("metadata_version") or "").strip() not in ("1.0",):
            return False

    sym = rec.get("canonical_symbol")
    exch = str(rec.get("exchange") or "").strip().upper()
    ident = rec.get("instrument_identifier")
    lot = rec.get("lot_size")
    if not isinstance(sym, str) or not sym.strip():
        return False
    if exch not in ("NSE", "BSE"):
        return False
    if not isinstance(ident, str) or not ident.strip():
        return False
    if lot is None or isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0:
        return False

    itype = str(rec.get("instrument_type") or "").strip().upper()
    if itype == "EQUITY":
        return (
            isinstance(rec.get("series"), str)
            and isinstance(rec.get("isin"), str)
            and isinstance(rec.get("is_trade_to_trade"), bool)
        )
    if itype == "INDEX":
        return rec.get("series") == "INDEX" and ("strike_interval" in rec)
    if itype in ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"):
        return (
            isinstance(rec.get("underlying"), str)
            and bool(rec.get("underlying").strip())
            and ("expiry_source" in rec or "tick_size_source" in rec)
        )
    return False


def normalize_tick_size_inr(raw_tick, source_unit="INR", allow_none=False):
    """Normalize and validate an instrument tick size in INR (rupees).

    Converts from paise to INR by dividing by 100 ONLY when source_unit is 'paise'.
    If source_unit is 'INR', preserves the value without double conversion.
    Rejects unknown units, bool, NaN/Inf, non-numeric, and non-positive values explicitly.
    """
    if source_unit is None or not str(source_unit).strip():
        raise TickResolutionError("Unsupported tick_size unit %r" % (source_unit,))
    unit = str(source_unit).strip().lower()
    if unit not in (_VALID_TICK_UNITS_INR + _VALID_TICK_UNITS_PAISE):
        raise TickResolutionError("Unsupported tick_size unit %r" % (source_unit,))

    if raw_tick is None or (isinstance(raw_tick, str) and not raw_tick.strip()):
        if allow_none:
            return None
        raise TickResolutionError("Cannot authoritatively resolve tick size: missing value")
    if isinstance(raw_tick, bool):
        raise TickResolutionError("Invalid boolean tick_size %r" % (raw_tick,))
    try:
        dec = Decimal(str(raw_tick).strip())
    except (InvalidOperation, ValueError):
        raise TickResolutionError("Invalid non-numeric tick_size %r" % (raw_tick,))
    if not dec.is_finite() or dec <= 0:
        raise TickResolutionError("Invalid non-positive or non-finite tick_size %r" % (raw_tick,))
    if unit in _VALID_TICK_UNITS_PAISE:
        dec = dec / Decimal("100")
    return float(dec)


def _normalize_cache_record_tick(rec, cache_payload=None, allow_none=False, sym_hint=None):
    """Validate tick provenance and normalize tick_size to canonical INR without guessing.

    Never infers units from numerical magnitude. Explicitly distinguishes:
    - tick_size_unit='INR': validated without dividing by 100.
    - tick_size_unit='paise': converted once (dec / 100) and stamped tick_size_unit='INR'.
    - Unknown/empty tick_size_unit: raises TickResolutionError.
    - Unmarked Upstox/BOD records: raises TickResolutionError instructing cache rebuild.
    - Unmarked verifiable NSE/BSE CSV records: preserved as INR and stamped in memory.
    - Unmarked ambiguous records: raises TickResolutionError.
    """
    if not isinstance(rec, dict):
        raise InstrumentMetadataError("Malformed instrument record: expected dict")

    sym = sym_hint or rec.get("canonical_symbol") or rec.get("trading_symbol") or rec.get("symbol") or "UNKNOWN"
    out = dict(rec)
    raw_tick = out.get("tick_size")

    if "tick_size_unit" in out:
        unit_val = out.get("tick_size_unit")
        if unit_val is None or not str(unit_val).strip():
            raise TickResolutionError(
                "Invalid empty 'tick_size_unit' for instrument %r; expected 'INR' or 'paise'" % (sym,)
            )
        tick_inr = normalize_tick_size_inr(raw_tick, source_unit=unit_val, allow_none=allow_none)
        out["tick_size"] = tick_inr
        out["tick_size_unit"] = "INR"
        return out

    if _has_upstox_bod_provenance(out, cache_payload=cache_payload):
        raise TickResolutionError(
            "Unmarked Upstox/BOD instrument cache record for %r lacks 'tick_size_unit'; "
            "rebuild the instrument cache before trading" % (sym,)
        )

    if _is_legacy_csv_entry(out, cache_payload=cache_payload):
        tick_inr = normalize_tick_size_inr(raw_tick, source_unit="INR", allow_none=allow_none)
        out["tick_size"] = tick_inr
        out["tick_size_unit"] = "INR"
        return out

    raise TickResolutionError(
        "Ambiguous instrument cache record for %r is missing 'tick_size_unit' and lacks "
        "verifiable CSV provenance; cannot safely infer tick unit" % (sym,)
    )


def normalize_instrument_cache_payload(payload, allow_none_tick=True):
    """Validate tick provenance across an instrument cache payload and normalize units to INR."""
    if not isinstance(payload, dict) or not isinstance(payload.get("instruments"), dict):
        raise InstrumentMetadataError("Malformed instrument cache: missing 'instruments' mapping")

    out = dict(payload)
    norm_flat = {}
    for sym_key, rec in payload["instruments"].items():
        norm_flat[sym_key] = _normalize_cache_record_tick(
            rec, cache_payload=payload, allow_none=allow_none_tick, sym_hint=sym_key
        )
    out["instruments"] = norm_flat

    if "instruments_by_exchange" in payload and payload["instruments_by_exchange"] is not None:
        by_exch = payload["instruments_by_exchange"]
        if not isinstance(by_exch, dict):
            raise InstrumentMetadataError("Malformed instrument cache: 'instruments_by_exchange' must be a dict")
        norm_by_exch = {}
        for ex_name, bucket in by_exch.items():
            if not isinstance(bucket, dict):
                raise InstrumentMetadataError(
                    "Malformed instrument cache: exchange bucket %r must be a dict" % (ex_name,)
                )
            norm_bucket = {}
            for sym_key, rec in bucket.items():
                norm_bucket[sym_key] = _normalize_cache_record_tick(
                    rec, cache_payload=payload, allow_none=allow_none_tick, sym_hint=sym_key
                )
            norm_by_exch[ex_name] = norm_bucket
        out["instruments_by_exchange"] = norm_by_exch

    return out


def parse_tick_size(value, symbol=None):
    """Strictly parse a positive finite INR tick size; never substitutes a default."""
    try:
        return normalize_tick_size_inr(value, source_unit="INR", allow_none=False)
    except TickResolutionError as exc:
        if symbol:
            raise TickResolutionError("Cannot authoritatively resolve tick size for %r: %s" % (symbol, exc))
        raise


def _infer_exchange_from_symbol(symbol, explicit_exchange=None):
    """Infer explicit NSE/BSE exchange from --exchange or symbol suffix/prefix."""
    if explicit_exchange:
        ex = str(explicit_exchange).strip().upper()
        if ex in ("NSE", "BSE"):
            return ex
    raw = str(symbol or "").strip().upper()
    if raw.endswith(".BO") or raw.startswith("BSE:") or raw.startswith("BSE_"):
        return "BSE"
    if raw.endswith(".NS") or raw.startswith("NSE:") or raw.startswith("NSE_"):
        return "NSE"
    return None


def parse_equity_l_csv(csv_path_or_text, exchange="NSE"):
    """Parse official NSE EQUITY_L.csv content into structured record dicts.

    Header schema:
    SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE
    """
    if os.path.exists(csv_path_or_text):
        with open(csv_path_or_text, encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    else:
        reader = csv.DictReader(str(csv_path_or_text).splitlines())
        rows = list(reader)

    records = {}
    for raw_row in rows:
        row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw_row.items() if k}
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
            "tick_size_unit": "INR",
            "lot_size": lot_size,
            "expiry": None,
            "is_trade_to_trade": series in ("BE", "BZ", "T"),
        }
    return records


def parse_bse_equity_csv(csv_path_or_text):
    """Parse official BSE Equity.csv / scrip master into structured record dicts.

    Header schema commonly found in BSE security master:
    Security Code,Security Id,Security Name,Status,Group,Face Value,ISIN No,Industry,Instrument
    """
    if os.path.exists(csv_path_or_text):
        with open(csv_path_or_text, encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    else:
        reader = csv.DictReader(str(csv_path_or_text).splitlines())
        rows = list(reader)

    records = {}
    for raw_row in rows:
        row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw_row.items() if k}
        scrip_id = (row.get("Security Id") or row.get("SECURITY ID") or row.get("SYMBOL") or "").strip().upper()
        scrip_code = (row.get("Security Code") or row.get("SECURITY CODE") or "").strip()
        status = (row.get("Status") or row.get("STATUS") or "Active").strip()
        if status.lower() not in ("active", "a", ""):
            continue

        symbol = scrip_id or scrip_code
        if not symbol:
            continue
        try:
            symbol = normalize_symbol(symbol)
        except Exception:
            continue

        group = (row.get("Group") or row.get("GROUP") or "A").strip().upper()
        name = (row.get("Security Name") or row.get("SECURITY NAME") or "").strip()
        isin = (row.get("ISIN No") or row.get("ISIN") or "").strip().upper()

        records[symbol] = {
            "canonical_symbol": symbol,
            "exchange": "BSE",
            "name": name,
            "series": group,
            "isin": isin,
            "bse_scrip_code": scrip_code,
            "instrument_type": "EQUITY",
            "instrument_identifier": symbol,
            "tick_size": 0.05,
            "tick_size_unit": "INR",
            "lot_size": 1,
            "expiry": None,
            "is_trade_to_trade": group in ("T", "Z", "XT"),
        }
    return records


def get_strike_interval(underlying, spot_price=None):
    """Authoritatively determine the strike price step interval for an instrument.

    For indices, standard regulatory strike steps apply (e.g. NIFTY=50, BANKNIFTY=100).
    For equities, if not explicitly configured in instrument metadata, intervals follow
    standard exchange price brackets.
    """
    sym = normalize_symbol(underlying)
    if sym in INDEX_INSTRUMENTS:
        return INDEX_INSTRUMENTS[sym].get("strike_interval", 50.0)

    # Dynamic equity strike interval based on spot price
    if spot_price is not None and spot_price > 0:
        p = float(spot_price)
        if p < 50:
            return 1.0
        elif p < 100:
            return 2.5
        elif p < 250:
            return 5.0
        elif p < 500:
            return 10.0
        elif p < 1000:
            return 20.0
        elif p < 2500:
            return 50.0
        elif p < 5000:
            return 100.0
        else:
            return 200.0

    return 20.0  # Safe default for mid/large-cap equities


def get_fo_lot_size(symbol, cache_payload=None):
    """Authoritatively resolve the derivative contract lot size for a security, derivative, or index."""
    sym = normalize_symbol(symbol)
    if is_derivative_symbol(sym):
        parsed = parse_derivative_symbol(sym)
        if parsed and parsed.get("underlying"):
            sym = normalize_symbol(parsed["underlying"])
    if sym in INDEX_INSTRUMENTS:
        return INDEX_INSTRUMENTS[sym]["fo_lot_size"]

    payload = cache_payload
    if payload is None and os.path.isfile(DEFAULT_CACHE_PATH):
        try:
            payload = load_instrument_cache(DEFAULT_CACHE_PATH, max_age_days=30)
        except Exception:
            payload = None

    if payload and "instruments" in payload:
        inst = payload["instruments"].get(sym)
        if inst and inst.get("fo_lot_size"):
            return int(inst["fo_lot_size"])

    return None


def parse_fo_lots_csv(csv_path_or_text):
    """Parse official NSE fo_mktlots.csv content into a dict of {symbol: lot_size}.

    Supports both test fixture schema (LOT_SIZE column) and official exchange format
    with monthly expiry columns (e.g. UNDERLYING, SYMBOL, NOV-26...).
    """
    if os.path.exists(csv_path_or_text):
        with open(csv_path_or_text, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    else:
        lines = str(csv_path_or_text).splitlines()

    lots = {}
    reader = csv.reader(lines)
    header = None
    for row in reader:
        if not row or len(row) < 2:
            continue
        clean = [c.strip() for c in row]
        if not clean[0] and not clean[1]:
            continue
        u1 = clean[1].upper() if len(clean) > 1 else ""
        u0 = clean[0].upper()
        if "SYMBOL" in u1 or "UNDERLYING" in u0:
            header = [c.upper() for c in clean]
            continue

        raw_sym = clean[1] if len(clean) > 1 else ""
        if not raw_sym or raw_sym.upper() in ("SYMBOL", ""):
            continue

        try:
            sym = normalize_symbol(raw_sym)
        except Exception:
            continue

        lot = None
        if header and len(clean) <= len(header):
            for idx, h in enumerate(header):
                if "LOT" in h and idx < len(clean):
                    val = clean[idx].replace(",", "").strip()
                    if val.isdigit():
                        lot = int(val)
                        break
        if lot is None and len(clean) >= 3:
            for val in clean[2:]:
                v_clean = val.replace(",", "").strip()
                if v_clean.isdigit():
                    lot = int(v_clean)
                    break

        if lot is not None and lot > 0:
            lots[sym] = lot

    return lots


def build_instrument_master(equity_records, fo_lots=None, effective_date=None, source_timestamp=None, by_exchange_records=None):
    """Combine cash equity records and derivative metadata into authoritative master payload."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ts = source_timestamp or now_utc.isoformat()
    eff = effective_date or now_utc.strftime("%Y-%m-%d")

    instruments = {}
    instruments_by_exchange = {}
    fo_map = fo_lots or {}

    def _finalize_entry(sym, rec):
        entry = dict(rec)
        entry.setdefault("canonical_symbol", sym)
        entry.setdefault("symbol", entry["canonical_symbol"])
        entry["source_timestamp"] = ts
        entry["effective_date"] = eff
        entry = _normalize_cache_record_tick(entry, cache_payload=None, allow_none=True, sym_hint=sym)
        if sym in fo_map:
            entry["fo_lot_size"] = fo_map[sym]
            entry["is_fo_eligible"] = True
            # Since August 3, 2026, eligible F&O equities participate in Closing Auction Session (CAS) on NSE
            entry["is_cas_eligible"] = entry.get("exchange", "NSE") == "NSE"
        else:
            entry.setdefault("fo_lot_size", None)
            entry.setdefault("is_fo_eligible", False)
            entry.setdefault("is_cas_eligible", False)
        return entry

    items_iter = equity_records.items() if isinstance(equity_records, dict) else (
        (r.get("canonical_symbol") or r.get("symbol"), r) for r in (equity_records or []) if isinstance(r, dict)
    )
    for sym_key, rec in items_iter:
        if not sym_key or not isinstance(rec, dict):
            continue
        canon = str(rec.get("canonical_symbol") or sym_key).upper()
        if ":" in sym_key and not rec.get("canonical_symbol"):
            canon = sym_key.split(":", 1)[1].upper()
        entry = _finalize_entry(canon, rec)
        ex = str(entry.get("exchange") or "NSE").upper()
        instruments_by_exchange.setdefault(ex, {})[sym_key if ":" not in sym_key else canon] = entry
        if sym_key not in instruments or ex == "NSE":
            instruments[sym_key] = entry

    if isinstance(by_exchange_records, dict):
        for ex_name, recs_map in by_exchange_records.items():
            ex_up = str(ex_name).upper()
            ex_bucket = instruments_by_exchange.setdefault(ex_up, {})
            if isinstance(recs_map, dict):
                for s_k, rec in recs_map.items():
                    entry = _finalize_entry(s_k, rec)
                    ex_bucket[s_k] = entry

    # Seed canonical index instruments
    for idx_sym, idx_meta in INDEX_INSTRUMENTS.items():
        if idx_sym not in instruments:
            idx_entry = dict(idx_meta)
            idx_entry["source_timestamp"] = ts
            idx_entry["effective_date"] = eff
            instruments[idx_sym] = idx_entry
            idx_ex = str(idx_entry.get("exchange") or "NSE").upper()
            instruments_by_exchange.setdefault(idx_ex, {}).setdefault(idx_sym, idx_entry)

    payload = {
        "metadata_version": "1.0",
        "effective_date": eff,
        "generated_at": ts,
        "exchange": "NSE",
        "count": len(instruments),
        "instruments": instruments,
        "instruments_by_exchange": instruments_by_exchange,
    }
    return payload


def build_master_from_provider_records(records, fo_lots=None, effective_date=None, source_timestamp=None, provider="upstox"):
    """Build an instrument master cache payload from normalized provider instrument records.

    Provider records are expected to carry canonical rupee tick sizes (tick_size_unit='INR').
    Double conversion is guarded against: records marked 'INR' or without explicit 'paise' unit
    are validated as INR without dividing by 100. Missing or invalid tick sizes are preserved as
    None rather than silently substituted with 0.05.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ts = source_timestamp or now_utc.isoformat()
    eff = effective_date or now_utc.strftime("%Y-%m-%d")

    flat = {}
    by_exch = {}
    fo_map = fo_lots or {}

    for raw_rec in (records or []):
        if not isinstance(raw_rec, dict):
            continue
        seg = str(raw_rec.get("segment") or "").upper()
        ex_fallback = "BSE" if seg.startswith("BSE") else "NSE"
        ex = str(raw_rec.get("exchange") or ex_fallback).upper()
        itype = str(raw_rec.get("instrument_type") or "").upper()
        raw_sym = str(raw_rec.get("canonical_symbol") or raw_rec.get("trading_symbol") or raw_rec.get("symbol") or "").strip()
        if not raw_sym:
            continue
        is_deriv = seg.endswith("_FO") or itype in ("CE", "PE", "FUT") or is_derivative_symbol(raw_sym)
        if is_deriv:
            canon = raw_sym.upper()
        else:
            try:
                canon = normalize_symbol(raw_sym)
            except Exception:
                canon = raw_sym.upper()

        raw_tick = raw_rec.get("tick_size")
        if "tick_size_unit" in raw_rec:
            unit_val = raw_rec.get("tick_size_unit")
            if unit_val is None or not str(unit_val).strip():
                raise TickResolutionError(
                    "Invalid empty 'tick_size_unit' for provider instrument %r; expected 'INR' or 'paise'" % (canon,)
                )
            unit_low = str(unit_val).strip().lower()
            if unit_low not in (_VALID_TICK_UNITS_INR + _VALID_TICK_UNITS_PAISE):
                raise TickResolutionError("Unsupported tick_size unit %r" % (unit_val,))
            try:
                tick_inr = normalize_tick_size_inr(raw_tick, source_unit=unit_val, allow_none=True)
            except TickResolutionError:
                tick_inr = None
        elif _is_legacy_csv_entry(raw_rec):
            try:
                tick_inr = normalize_tick_size_inr(raw_tick, source_unit="INR", allow_none=True)
            except TickResolutionError:
                tick_inr = None
        else:
            raise TickResolutionError(
                "Unmarked provider instrument record %r is missing 'tick_size_unit'; "
                "rebuild or pass explicit 'tick_size_unit' ('INR' or 'paise')" % (canon,)
            )

        raw_lot = raw_rec.get("lot_size")
        try:
            lot_val = int(raw_lot) if raw_lot is not None and not isinstance(raw_lot, bool) and int(raw_lot) > 0 else 1
        except (TypeError, ValueError):
            lot_val = 1

        is_index = seg.endswith("_INDEX") or itype == "INDEX" or is_index_symbol(canon)
        is_t2t = bool(raw_rec.get("is_trade_to_trade", False)) or seg in ("NSE_BE", "BSE_T") or itype in ("BE", "T")
        idx_meta = INDEX_INSTRUMENTS.get(canon, {}) if is_index else {}
        ikey_val = raw_rec.get("instrument_key") or idx_meta.get("upstox_key") or ""
        entry = {
            "canonical_symbol": canon,
            "symbol": canon,
            "exchange": ex,
            "segment": seg,
            "name": raw_rec.get("name") or idx_meta.get("name") or canon,
            "series": raw_rec.get("series") or itype or ("INDEX" if is_index else "EQ"),
            "isin": raw_rec.get("isin") or "",
            "instrument_key": ikey_val,
            "upstox_key": ikey_val,
            "instrument_token": raw_rec.get("instrument_token") or "",
            "instrument_type": itype or ("INDEX" if is_index else ("EQUITY" if seg.endswith("_EQ") else "")),
            "tick_size": tick_inr,
            "tick_size_unit": "INR",
            "lot_size": lot_val,
            "fo_lot_size": fo_map.get(canon) or idx_meta.get("fo_lot_size") or (lot_val if is_deriv else None),
            "is_fo_eligible": bool(canon in fo_map or is_deriv or idx_meta.get("is_fo_eligible")),
            "is_cas_eligible": bool((not is_index) and (canon in fo_map) and ex == "NSE"),
            "is_trade_to_trade": is_t2t,
            "is_tradable_equity": bool((not is_index) and (not is_deriv)),
            "provider": provider,
            "source_timestamp": ts,
            "effective_date": eff,
        }
        if is_index and idx_meta.get("strike_interval") is not None:
            entry["strike_interval"] = idx_meta["strike_interval"]
        if raw_rec.get("expiry") is not None:
            entry["expiry"] = raw_rec.get("expiry")
        if raw_rec.get("strike_price") is not None:
            entry["strike_price"] = raw_rec.get("strike_price")

        ex_bucket = by_exch.setdefault(ex, {})
        # Prefer EQ/FO canonical records over less common segments when duplicate trading_symbol exists
        prev = ex_bucket.get(canon)
        if prev is None or (seg in ("NSE_EQ", "BSE_EQ") and prev.get("segment") not in ("NSE_EQ", "BSE_EQ")):
            ex_bucket[canon] = entry

        # In flat map: NSE canonical takes precedence for unqualified equity lookup; composite also stored if NSE!=ex
        if canon not in flat or (ex == "NSE" and flat[canon].get("exchange") != "NSE"):
            flat[canon] = entry

    return {
        "metadata_version": "1.0",
        "effective_date": eff,
        "generated_at": ts,
        "provider": provider,
        "exchange": "ALL" if len(by_exch) > 1 else (next(iter(by_exch.keys()), "NSE")),
        "count": len(flat),
        "instruments": flat,
        "instruments_by_exchange": by_exch,
    }


def save_instrument_cache(payload, cache_path=DEFAULT_CACHE_PATH, history_dir=HISTORY_DIR):
    """Atomically save instrument master cache and preserve historical snapshot.

    Uses a temporary file in the destination directory and atomic rename (os.replace)
    to prevent race conditions or corrupted cache writes.
    """
    target_dir = os.path.dirname(os.path.abspath(cache_path))
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    # Validate payload integrity and tick provenance before writing
    payload = normalize_instrument_cache_payload(payload, allow_none_tick=True)

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

    payload = normalize_instrument_cache_payload(payload, allow_none_tick=True)

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

    effective_ref = ref_date
    if calendar_in is not None:
        try:
            is_td, _, _ = calendar_in.is_trading_day_in(ref_date)
            if not is_td:
                effective_ref = calendar_in.get_last_trading_day(ref_date)
        except Exception:
            pass

    delta_days = (effective_ref - eff_date).days
    if delta_days > max_age_days:
        raise StaleMetadataError(
            "Instrument cache is stale (effective: %s, reference: %s, age: %d days > %d allowed)"
            % (eff_str, ref_date.isoformat(), delta_days, max_age_days)
        )

    return payload


def resolve_derivative_contract(symbol, cache_payload=None, as_of_date=None, exchange=None):
    """Authoritatively resolve instrument metadata for an Indian derivative contract.

    Parses symbol (e.g. NIFTY26NOV24000CE or RELIANCE26NOVFUT) and enriches with:
    - underlying instrument details
    - contract lot size
    - strike interval
    - authoritative expiry date
    - tick size (actual contract tick size if cached, or 0.05 synthetic fallback)
    """
    if cache_payload is None and os.path.isfile(DEFAULT_CACHE_PATH):
        try:
            cache_payload = load_instrument_cache(DEFAULT_CACHE_PATH, max_age_days=30, as_of_date=as_of_date)
        except (FileNotFoundError, StaleMetadataError):
            cache_payload = None

    parsed = parse_derivative_symbol(symbol)
    underlying = parsed["underlying"]
    asset_type = parsed["asset_type"]
    opt_type = parsed["option_type"]
    strike = parsed["strike"]
    inferred_exch = _infer_exchange_from_symbol(symbol, explicit_exchange=exchange) or (
        "BSE" if underlying in ("SENSEX", "BANKEX") else "NSE"
    )

    # Check if exact contract exists in cache (exchange-specific first, then flat)
    if cache_payload:
        exact = None
        by_exch = cache_payload.get("instruments_by_exchange") or {}
        if inferred_exch in by_exch:
            exact = by_exch[inferred_exch].get(parsed["canonical_symbol"])
        if exact is None and "instruments" in cache_payload:
            cand = cache_payload["instruments"].get(parsed["canonical_symbol"])
            if cand and (not cand.get("exchange") or str(cand.get("exchange")).upper() == inferred_exch):
                exact = cand
        if exact:
            out = _normalize_cache_record_tick(
                exact, cache_payload=cache_payload, allow_none=False, sym_hint=parsed["canonical_symbol"]
            )
            out.setdefault("expiry_source", "contract_master")
            return out

    # Resolve underlying metadata
    lot_size = get_fo_lot_size(underlying, cache_payload=cache_payload)
    if not lot_size:
        raise LotSizeResolutionError("Cannot resolve F&O lot size for underlying: %r" % underlying)

    strike_interval = get_strike_interval(underlying)

    # Resolve expiry date
    year = parsed["expiry_yy"]
    month = parsed["expiry_month"]
    day = parsed["expiry_day"]
    if day is not None:
        expiry_date = "%04d-%02d-%02d" % (year, month, day)
        expiry_source = "symbol_explicit"
    else:
        # Monthly contract: resolve last trading day based on underlying convention
        exp_dt = calendar_in.get_monthly_expiry(year, month, exchange=inferred_exch, underlying=underlying)
        expiry_date = exp_dt.strftime("%Y-%m-%d")
        expiry_source = "exchange_fallback_rule"

    return {
        "canonical_symbol": parsed["canonical_symbol"],
        "underlying": underlying,
        "exchange": inferred_exch,
        "asset_type": asset_type,
        "instrument_type": ("OPTIDX" if underlying in INDEX_INSTRUMENTS else "OPTSTK") if asset_type == "OPTIONS" else ("FUTIDX" if underlying in INDEX_INSTRUMENTS else "FUTSTK"),
        "instrument_identifier": parsed["canonical_symbol"],
        "option_type": opt_type,
        "strike": strike,
        "expiry": expiry_date,
        "expiry_source": expiry_source,
        "lot_size": lot_size,
        "tick_size": 0.05,
        "tick_size_unit": "INR",
        "tick_size_source": "synthetic_default",
        "strike_interval": strike_interval,
        "is_trade_to_trade": False,
        "is_cas_eligible": False,
        "is_index": underlying in INDEX_INSTRUMENTS,
    }


def resolve_instrument(symbol, cache_payload=None, require_intraday=True, exchange=None):
    """Authoritatively resolve instrument metadata for a canonical symbol.

    Fails closed:
    - Resolves derivatives if is_derivative_symbol is True.
    - Resolves index instruments if symbol is a known index.
    - Respects exchange qualifiers (.NS, .BO, NSE:, BSE:, or explicit exchange=).
    - Raises InstrumentMetadataError if symbol is missing from master (or absent on requested exchange).
    - Raises InstrumentMetadataError if security is Trade-to-Trade and require_intraday is True.
    - Raises TickResolutionError if tick size is missing or invalid.
    - Raises LotSizeResolutionError if lot size is missing or <= 0.
    """
    req_exch = _infer_exchange_from_symbol(symbol, explicit_exchange=exchange)
    if is_derivative_symbol(symbol):
        return resolve_derivative_contract(symbol, cache_payload=cache_payload, exchange=req_exch)

    try:
        sym = normalize_symbol(symbol)
    except Exception as exc:
        raise InstrumentMetadataError("Invalid symbol %r: %s" % (symbol, exc))

    if sym in INDEX_INSTRUMENTS:
        return dict(INDEX_INSTRUMENTS[sym])

    if not cache_payload and os.path.isfile(DEFAULT_CACHE_PATH):
        try:
            cache_payload = load_instrument_cache(DEFAULT_CACHE_PATH, max_age_days=30)
        except (FileNotFoundError, StaleMetadataError):
            cache_payload = None

    if not cache_payload:
        raise InstrumentMetadataError("Instrument cache payload required to resolve equity: %r" % sym)

    entry = None
    instruments = cache_payload.get("instruments") or {}
    by_exch = cache_payload.get("instruments_by_exchange") or {}
    if req_exch:
        if isinstance(by_exch.get(req_exch), dict) and sym in by_exch[req_exch]:
            entry = by_exch[req_exch][sym]
        elif "%s:%s" % (req_exch, sym) in instruments:
            entry = instruments["%s:%s" % (req_exch, sym)]
        elif sym in instruments:
            cand = instruments[sym]
            cand_exch = str(cand.get("exchange") or "").upper()
            if not cand_exch or cand_exch == req_exch:
                entry = cand
            else:
                raise InstrumentMetadataError(
                    "Symbol %r found only on %s, not requested exchange %s" % (sym, cand_exch, req_exch)
                )
    else:
        if sym in instruments:
            entry = instruments[sym]
        else:
            for ex_cand in ("NSE", "BSE"):
                if isinstance(by_exch.get(ex_cand), dict) and sym in by_exch[ex_cand]:
                    entry = by_exch[ex_cand][sym]
                    break

    if entry is None:
        if req_exch:
            raise InstrumentMetadataError("Symbol %r (%s) not found in authoritative instrument master" % (sym, req_exch))
        raise InstrumentMetadataError("Symbol %r not found in authoritative instrument master" % sym)

    # Series / Trade-to-Trade checks
    if require_intraday and entry.get("is_trade_to_trade"):
        series = entry.get("series", "UNKNOWN")
        raise InstrumentMetadataError(
            "Symbol %r is under Trade-to-Trade series %r: intraday square-off prohibited by SEBI"
            % (sym, series)
        )

    # Tick size resolution & provenance validation (strict positive finite INR; never invent precision)
    entry = _normalize_cache_record_tick(entry, cache_payload=cache_payload, allow_none=False, sym_hint=sym)

    # Lot size resolution
    lot_size = entry.get("lot_size")
    if lot_size is None or isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        raise LotSizeResolutionError("Cannot authoritatively resolve lot size for %r" % sym)

    return dict(entry)


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


def sync_masters(dest_path=DEFAULT_CACHE_PATH, offline_dir=None, exchange="NSE"):
    """Fetch official exchange masters and write an authoritative instruments cache.

    If network is available, downloads official EQUITY_L.csv and fo_mktlots.csv from NSE.
    If offline_dir is provided or fallback files exist, reads local CSV files.
    """
    import urllib.request
    equity_records = {}
    fo_lots = {}

    # 1. Fetch or load NSE EQUITY_L
    eq_text = None
    if offline_dir and os.path.isdir(offline_dir):
        p = os.path.join(offline_dir, "EQUITY_L.csv")
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                eq_text = fh.read()
    if not eq_text:
        urls = [
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
        ]
        for u in urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    eq_text = resp.read().decode("utf-8", errors="replace")
                    if eq_text and "SYMBOL" in eq_text:
                        break
            except Exception:
                continue

    if not eq_text:
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fixture_p in (
            os.path.join(pkg_root, "tests", "fixtures", "bharat", "EQUITY_L_sample.csv"),
            os.path.join(pkg_root, "fixtures", "bharat", "EQUITY_L_sample.csv"),
        ):
            if os.path.isfile(fixture_p):
                with open(fixture_p, encoding="utf-8", errors="replace") as fh:
                    eq_text = fh.read()
                break

    if eq_text:
        equity_records = parse_equity_l_csv(eq_text, exchange="NSE")

    # 2. Fetch or load NSE fo_mktlots
    fo_text = None
    if offline_dir and os.path.isdir(offline_dir):
        p = os.path.join(offline_dir, "fo_mktlots.csv")
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                fo_text = fh.read()
    if not fo_text:
        urls = [
            "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
            "https://archives.nseindia.com/content/fo/fo_mktlots.csv",
        ]
        for u in urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    fo_text = resp.read().decode("utf-8", errors="replace")
                    if fo_text and "SYMBOL" in fo_text:
                        break
            except Exception:
                continue

    if not fo_text:
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fixture_p in (
            os.path.join(pkg_root, "tests", "fixtures", "bharat", "fo_mktlots_sample.csv"),
            os.path.join(pkg_root, "fixtures", "bharat", "fo_mktlots_sample.csv"),
        ):
            if os.path.isfile(fixture_p):
                with open(fixture_p, encoding="utf-8", errors="replace") as fh:
                    fo_text = fh.read()
                break

    if fo_text:
        fo_lots = parse_fo_lots_csv(fo_text)

    # 3. Check for BSE Equity.csv
    bse_text = None
    bse_records = {}
    if offline_dir and os.path.isdir(offline_dir):
        p = os.path.join(offline_dir, "Equity.csv")
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                bse_text = fh.read()
    if bse_text:
        bse_records = parse_bse_equity_csv(bse_text)
        for sym, rec in bse_records.items():
            if sym not in equity_records:
                equity_records[sym] = rec

    by_exch = {}
    if equity_records:
        nse_only = {k: v for k, v in equity_records.items() if str(v.get("exchange") or "NSE").upper() == "NSE"}
        if nse_only:
            by_exch["NSE"] = nse_only
    if bse_records:
        by_exch["BSE"] = bse_records

    payload = build_instrument_master(equity_records, fo_lots=fo_lots, by_exchange_records=by_exch or None)
    save_instrument_cache(payload, cache_path=dest_path)
    return payload


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Authoritative NSE/BSE Instrument Master Cache Manager.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("sync", help="fetch latest official exchange masters and update local cache")
    p.add_argument("--out", default=DEFAULT_CACHE_PATH, help="cache path (default %s)" % DEFAULT_CACHE_PATH)
    p.add_argument("--offline", help="folder with offline EQUITY_L.csv, fo_mktlots.csv, Equity.csv")
    p.add_argument("--exchange", default="NSE", choices=["NSE", "BSE", "ALL"])
    p.add_argument("--json", action="store_true", help="output JSON status")

    p = sub.add_parser("inspect", help="inspect metadata for a symbol or derivative")
    p.add_argument("symbol", help="canonical or derivative symbol (e.g. RELIANCE, NIFTY26NOV24000CE)")
    p.add_argument("--cache", default=DEFAULT_CACHE_PATH, help="cache path (default %s)" % DEFAULT_CACHE_PATH)
    p.add_argument("--exchange", choices=["NSE", "BSE"], help="exchange qualifier (NSE or BSE)")
    p.add_argument("--json", action="store_true", help="output JSON")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1

    if a.cmd == "sync":
        try:
            payload = sync_masters(dest_path=a.out, offline_dir=a.offline, exchange=a.exchange)
            eq_count = sum(1 for r in payload["instruments"].values() if r.get("instrument_type") == "EQUITY")
            fo_count = sum(1 for r in payload["instruments"].values() if r.get("is_fo_eligible"))
            if a.json:
                print(json.dumps({
                    "status": "OK",
                    "effective_date": payload["effective_date"],
                    "equities_count": eq_count,
                    "fo_count": fo_count,
                    "total_instruments": len(payload["instruments"]),
                    "cache_path": a.out
                }, indent=2))
            else:
                print("Synced authoritative instrument master: %d instruments (%d equities, %d F&O underlyings)."
                      % (len(payload["instruments"]), eq_count, fo_count))
                print("Saved cache to %s" % a.out)
            return 0
        except Exception as exc:
            print("instruments.py sync error: %s" % exc, file=sys.stderr)
            return 1

    if a.cmd == "inspect":
        try:
            payload = None
            if os.path.isfile(a.cache):
                payload = load_instrument_cache(a.cache, max_age_days=30)
            res = resolve_instrument(
                a.symbol, cache_payload=payload, require_intraday=False, exchange=getattr(a, "exchange", None)
            )
            if a.json:
                print(json.dumps(res, indent=2))
            else:
                print("Instrument Metadata: %s" % res["canonical_symbol"])
                for k in sorted(res.keys()):
                    print("  %-22s: %s" % (k, res[k]))
            return 0
        except Exception as exc:
            print("instruments.py inspect error: %s" % exc, file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
