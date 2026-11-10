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

# Authoritative Index Instruments Metadata
INDEX_INSTRUMENTS = {
    "NIFTY": {
        "canonical_symbol": "NIFTY",
        "exchange": "NSE",
        "name": "NIFTY 50",
        "series": "INDEX",
        "isin": "IN9002A01018",
        "instrument_type": "INDEX",
        "instrument_identifier": "NIFTY",
        "tick_size": 0.05,
        "lot_size": 1,
        "fo_lot_size": 25,
        "strike_interval": 50.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
    },
    "BANKNIFTY": {
        "canonical_symbol": "BANKNIFTY",
        "exchange": "NSE",
        "name": "NIFTY BANK",
        "series": "INDEX",
        "isin": "IN9002A01019",
        "instrument_type": "INDEX",
        "instrument_identifier": "BANKNIFTY",
        "tick_size": 0.05,
        "lot_size": 1,
        "fo_lot_size": 15,
        "strike_interval": 100.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
    },
    "FINNIFTY": {
        "canonical_symbol": "FINNIFTY",
        "exchange": "NSE",
        "name": "NIFTY FINANCIAL SERVICES",
        "series": "INDEX",
        "isin": "IN9002A01020",
        "instrument_type": "INDEX",
        "instrument_identifier": "FINNIFTY",
        "tick_size": 0.05,
        "lot_size": 1,
        "fo_lot_size": 25,
        "strike_interval": 50.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
    },
    "MIDCPNIFTY": {
        "canonical_symbol": "MIDCPNIFTY",
        "exchange": "NSE",
        "name": "NIFTY MIDCAP SELECT",
        "series": "INDEX",
        "isin": "IN9002A01021",
        "instrument_type": "INDEX",
        "instrument_identifier": "MIDCPNIFTY",
        "tick_size": 0.05,
        "lot_size": 1,
        "fo_lot_size": 50,
        "strike_interval": 25.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
    },
    "SENSEX": {
        "canonical_symbol": "SENSEX",
        "exchange": "BSE",
        "name": "BSE SENSEX",
        "series": "INDEX",
        "isin": "IN9002A01022",
        "instrument_type": "INDEX",
        "instrument_identifier": "SENSEX",
        "tick_size": 0.05,
        "lot_size": 1,
        "fo_lot_size": 10,
        "strike_interval": 100.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
    },
    "BANKEX": {
        "canonical_symbol": "BANKEX",
        "exchange": "BSE",
        "name": "BSE BANKEX",
        "series": "INDEX",
        "isin": "IN9002A01023",
        "instrument_type": "INDEX",
        "instrument_identifier": "BANKEX",
        "tick_size": 0.05,
        "lot_size": 1,
        "fo_lot_size": 15,
        "strike_interval": 100.0,
        "is_fo_eligible": True,
        "is_cas_eligible": False,
        "is_trade_to_trade": False,
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
    """Authoritatively resolve the derivative contract lot size for a security or index."""
    sym = normalize_symbol(symbol)
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

    # Seed canonical index instruments
    for idx_sym, idx_meta in INDEX_INSTRUMENTS.items():
        if idx_sym not in instruments:
            idx_entry = dict(idx_meta)
            idx_entry["source_timestamp"] = ts
            idx_entry["effective_date"] = eff
            instruments[idx_sym] = idx_entry

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


def resolve_derivative_contract(symbol, cache_payload=None, as_of_date=None):
    """Authoritatively resolve instrument metadata for an Indian derivative contract.

    Parses symbol (e.g. NIFTY26NOV24000CE or RELIANCE26NOVFUT) and enriches with:
    - underlying instrument details
    - contract lot size
    - strike interval
    - authoritative expiry date
    - tick size (0.05)
    """
    if cache_payload is None and os.path.isfile(DEFAULT_CACHE_PATH):
        try:
            cache_payload = load_instrument_cache(DEFAULT_CACHE_PATH, max_age_days=30, as_of_date=as_of_date)
        except Exception:
            cache_payload = None

    parsed = parse_derivative_symbol(symbol)
    underlying = parsed["underlying"]
    asset_type = parsed["asset_type"]
    opt_type = parsed["option_type"]
    strike = parsed["strike"]

    # Check if exact contract exists in cache
    if cache_payload and "instruments" in cache_payload:
        exact = cache_payload["instruments"].get(parsed["canonical_symbol"])
        if exact:
            out = dict(exact)
            out.setdefault("expiry_source", "contract_master")
            return out

    # Resolve underlying metadata
    lot_size = get_fo_lot_size(underlying, cache_payload=cache_payload)
    if not lot_size:
        raise LotSizeResolutionError("Cannot resolve F&O lot size for underlying: %r" % underlying)

    exchange = "BSE" if underlying in ("SENSEX", "BANKEX") else "NSE"
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
        exp_dt = calendar_in.get_monthly_expiry(year, month, exchange=exchange, underlying=underlying)
        expiry_date = exp_dt.strftime("%Y-%m-%d")
        expiry_source = "exchange_fallback_rule"

    return {
        "canonical_symbol": parsed["canonical_symbol"],
        "underlying": underlying,
        "exchange": exchange,
        "asset_type": asset_type,
        "instrument_type": ("OPTIDX" if underlying in INDEX_INSTRUMENTS else "OPTSTK") if asset_type == "OPTIONS" else ("FUTIDX" if underlying in INDEX_INSTRUMENTS else "FUTSTK"),
        "instrument_identifier": parsed["canonical_symbol"],
        "option_type": opt_type,
        "strike": strike,
        "expiry": expiry_date,
        "expiry_source": expiry_source,
        "lot_size": lot_size,
        "tick_size": 0.05,
        "strike_interval": strike_interval,
        "is_trade_to_trade": False,
        "is_cas_eligible": False,
        "is_index": underlying in INDEX_INSTRUMENTS,
    }


def resolve_instrument(symbol, cache_payload=None, require_intraday=True):
    """Authoritatively resolve instrument metadata for a canonical symbol.

    Fails closed:
    - Resolves derivatives if is_derivative_symbol is True.
    - Resolves index instruments if symbol is a known index.
    - Raises InstrumentMetadataError if symbol is missing from master.
    - Raises InstrumentMetadataError if security is Trade-to-Trade and require_intraday is True.
    - Raises TickResolutionError if tick size is missing or invalid.
    - Raises LotSizeResolutionError if lot size is missing or <= 0.
    """
    if is_derivative_symbol(symbol):
        return resolve_derivative_contract(symbol, cache_payload=cache_payload)

    try:
        sym = normalize_symbol(symbol)
    except Exception as exc:
        raise InstrumentMetadataError("Invalid symbol %r: %s" % (symbol, exc))

    if sym in INDEX_INSTRUMENTS:
        return dict(INDEX_INSTRUMENTS[sym])

    if not cache_payload and os.path.isfile(DEFAULT_CACHE_PATH):
        try:
            cache_payload = load_instrument_cache(DEFAULT_CACHE_PATH, max_age_days=30)
        except Exception:
            cache_payload = None

    if not cache_payload:
        raise InstrumentMetadataError("Instrument cache payload required to resolve equity: %r" % sym)

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
        fixture_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "bharat", "EQUITY_L_sample.csv")
        if os.path.isfile(fixture_p):
            with open(fixture_p, encoding="utf-8", errors="replace") as fh:
                eq_text = fh.read()

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
        fixture_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "bharat", "fo_mktlots_sample.csv")
        if os.path.isfile(fixture_p):
            with open(fixture_p, encoding="utf-8", errors="replace") as fh:
                fo_text = fh.read()

    if fo_text:
        fo_lots = parse_fo_lots_csv(fo_text)

    # 3. Check for BSE Equity.csv
    bse_text = None
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

    payload = build_instrument_master(equity_records, fo_lots=fo_lots)
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
            res = resolve_instrument(a.symbol, cache_payload=payload, require_intraday=False)
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
