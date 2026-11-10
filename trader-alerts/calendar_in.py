"""Indian market calendar, session schedule, and special sessions engine for NSE and BSE.

All calculations use Indian Standard Time (IST = UTC+05:30).
Python 3.8 compatible without third-party dependencies or zoneinfo.
"""

import datetime
import json
import os

# Indian Standard Time (UTC+05:30) fixed offset
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")

# Regulatory Effective Dates & Circular Citations
NSE_CAS_V1_EFFECTIVE_DATE = "2026-08-03"    # Original 20-min CAS (August 3, 2026)
NSE_CAS_EFFECTIVE_DATE = NSE_CAS_V1_EFFECTIVE_DATE  # Backward-compatibility alias
SEBI_CAS_V2_CIRCULAR = "SEBI/HO/47/11/11(3)2025-MRD-POD2/I/23843/2026"
SEBI_CAS_V2_CIRCULAR_DATE = "2026-10-15"
NSE_CAS_V2_EFFECTIVE_DATE = "2026-12-07"    # Revised 10-min CAS (December 7, 2026)
SEBI_UNEXECUTED_ORDER_TRANSITION_DATE = "2027-02-08"  # Unexecuted order transition (February 8, 2027)

# Continuous equity trading session constants (NSE/BSE)
NORMAL_OPEN_MIN = 9 * 60 + 15    # 09:15 IST (555 minutes from midnight)
NORMAL_CLOSE_MIN = 15 * 60 + 30  # 15:30 IST (Non-CAS, Pre-CAS, and CAS V2 continuous close)
CAS_V1_CLOSE_MIN = 15 * 60 + 15  # 15:15 IST (CAS V1 continuous close: 2026-08-03 to 2026-12-06)
CAS_CLOSE_MIN = CAS_V1_CLOSE_MIN  # Alias

NORMAL_SESSION_MINUTES = NORMAL_CLOSE_MIN - NORMAL_OPEN_MIN  # 375 minutes
CAS_V1_SESSION_MINUTES = CAS_V1_CLOSE_MIN - NORMAL_OPEN_MIN  # 360 minutes
DEFAULT_BAR_MINUTES = 5

NORMAL_SESSION_BARS = NORMAL_SESSION_MINUTES // DEFAULT_BAR_MINUTES  # 75 bars
CAS_V1_SESSION_BARS = CAS_V1_SESSION_MINUTES // DEFAULT_BAR_MINUTES  # 72 bars
CAS_SESSION_BARS = CAS_V1_SESSION_BARS  # Alias


class CASRuleVersion:
    PRE_CAS = "PRE_CAS"   # Prior to 2026-08-03: No CAS, 75 continuous bars (09:15-15:30)
    CAS_V1 = "CAS_V1"     # 2026-08-03 to 2026-12-06: 20-min CAS, 72 continuous bars (09:15-15:15)
    CAS_V2 = "CAS_V2"     # 2026-12-07 onward: 10-min CAS, 75 continuous bars (09:15-15:30)


class MarketSegment:
    CASH_EQUITY = "CASH_EQUITY"
    EQUITY_DERIVATIVES = "EQUITY_DERIVATIVES"
    SPECIAL_SESSION = "SPECIAL_SESSION"

# Verified NSE/BSE Equity Trading Holidays for 2025 (Official Exchange Circulars)
NSE_HOLIDAYS_2025 = {
    "2025-02-26": "Mahashivratri",
    "2025-03-14": "Holi",
    "2025-03-31": "Id-Ul-Fitr (Ramadan Eid)",
    "2025-04-10": "Mahavir Jayanti",
    "2025-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2025-04-18": "Good Friday",
    "2025-05-01": "Maharashtra Day",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Ganesh Chaturthi",
    "2025-10-02": "Mahatma Gandhi Jayanti / Dussehra",
    "2025-10-21": "Diwali Laxmi Pujan (Muhurat Trading in Evening)",
    "2025-10-22": "Diwali Balipratipada",
    "2025-11-05": "Prakash Gurpurb Sri Guru Nanak Dev Jayanti",
    "2025-12-25": "Christmas",
}

# Verified NSE/BSE Equity Trading Holidays for 2026 (Official Exchange Circulars)
NSE_HOLIDAYS_2026 = {
    "2026-01-26": "Republic Day",
    "2026-02-16": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-20": "Id-Ul-Fitr",
    "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-27": "Bakri Id",
    "2026-06-25": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat Trading in Evening)",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}

# Special sessions registry (Diwali Muhurat Trading, special live trading)
# Derived from official circulars with publication provenance.
OFFICIAL_SPECIAL_SESSIONS = {
    "2024-11-01": {
        "session_date": "2024-11-01",
        "exchange": "NSE",
        "open_time": "18:00",
        "close_time": "19:00",
        "pre_open_start": "17:45",
        "pre_open_end": "18:00",
        "session_type": "MUHURAT",
        "source": "NSE Circular Ref No: 110/2024",
        "publication_date": "2024-10-21",
    },
    "2025-10-21": {
        "session_date": "2025-10-21",
        "exchange": "NSE",
        "open_time": "18:15",
        "close_time": "19:15",
        "pre_open_start": "18:00",
        "pre_open_end": "18:08",
        "session_type": "MUHURAT",
        "source": "NSE Circular Ref No: 98/2025",
        "publication_date": "2025-10-06",
    },
    "2026-11-08": {
        "session_date": "2026-11-08",
        "exchange": "NSE",
        "open_time": "18:15",
        "close_time": "19:15",
        "pre_open_start": "18:00",
        "pre_open_end": "18:08",
        "session_type": "MUHURAT",
        "source": "NSE Circular Ref No: 104/2026",
        "publication_date": "2026-10-20",
    },
}


class CalendarStatus:
    VERIFIED = "verified"
    PROVISIONAL = "provisional"
    UNAVAILABLE = "unavailable"


def now_ist():
    """Return the current datetime localized to IST."""
    return datetime.datetime.now(IST)


def parse_ist(val):
    """Parse a datetime string (YYYY-MM-DD, YYYY-MM-DD HH:MM, ISO 8601) into an IST datetime."""
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=IST)
        return val.astimezone(IST)
    if isinstance(val, datetime.date):
        return datetime.datetime(val.year, val.month, val.day, tzinfo=IST)
    val = str(val).strip()
    if "T" in val:
        iso_str = val.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    parts = val.split()
    date_parts = [int(p) for p in parts[0].split("-")]
    if len(parts) == 1:
        return datetime.datetime(date_parts[0], date_parts[1], date_parts[2], tzinfo=IST)
    time_parts = [int(p) for p in parts[1].split(":")]
    h = time_parts[0]
    m = time_parts[1]
    s = time_parts[2] if len(time_parts) > 2 else 0
    return datetime.datetime(date_parts[0], date_parts[1], date_parts[2], h, m, s, tzinfo=IST)


def get_holiday_calendar(year, exchange="NSE"):
    """Return dict of {date_str: name} and calendar verification status for a given year."""
    if year == 2025:
        return dict(NSE_HOLIDAYS_2025), CalendarStatus.VERIFIED
    if year == 2026:
        return dict(NSE_HOLIDAYS_2026), CalendarStatus.VERIFIED
    # Future years without official gazetted circulars are marked provisional
    return {}, CalendarStatus.PROVISIONAL if year > 2026 else CalendarStatus.UNAVAILABLE


def get_special_session(date_str, exchange="NSE"):
    """Return special session dict if date has an officially announced session, else None."""
    return OFFICIAL_SPECIAL_SESSIONS.get(date_str)


def is_trading_day_in(dt, exchange="NSE"):
    """Check if dt falls on an Indian market trading day.

    Returns (is_trading, status, reason):
    - is_trading: bool
    - status: 'verified' or 'provisional'
    - reason: explanation if closed, or 'regular_session' / 'special_session'
    """
    dt_ist = parse_ist(dt)
    date_str = dt_ist.strftime("%Y-%m-%d")

    # Check for official special session (e.g. Muhurat trading held on a Sunday/holiday)
    special = get_special_session(date_str, exchange=exchange)
    if special:
        return True, CalendarStatus.VERIFIED, "special_session:" + special.get("session_type", "SPECIAL")

    # Standard weekends: Saturday (5), Sunday (6)
    if dt_ist.weekday() >= 5:
        return False, CalendarStatus.VERIFIED, "weekend"

    holidays, status = get_holiday_calendar(dt_ist.year, exchange=exchange)
    if date_str in holidays:
        return False, status, "holiday:" + holidays[date_str]

    return True, status, "regular_session"


def is_weekend(dt_or_date_str):
    """Return True if dt_or_date_str falls on Saturday (5) or Sunday (6)."""
    dt = parse_ist(dt_or_date_str)
    return dt.weekday() >= 5


def get_last_trading_day(as_of_date=None, exchange="NSE"):
    """Return the date of the most recent completed or active trading session.

    If as_of_date is during or after pre-market on a trading day (>= 09:00 IST),
    returns that day. If before 09:00 IST, on a weekend, or on an exchange holiday,
    walks backward to the preceding trading day.
    """
    if as_of_date is None:
        dt = now_ist()
    else:
        dt = parse_ist(as_of_date)

    cur_d = dt.date()
    is_td, _, _ = is_trading_day_in(cur_d, exchange=exchange)
    # If dt has time information (datetime), check if pre-open has begun
    has_time = isinstance(dt, datetime.datetime) and (dt.hour != 0 or dt.minute != 0 or dt.second != 0)
    if is_td and (not has_time or (dt.hour * 60 + dt.minute >= 9 * 60)):
        return cur_d

    cur_d -= datetime.timedelta(days=1)
    for _ in range(40):
        is_td, _, _ = is_trading_day_in(cur_d, exchange=exchange)
        if is_td:
            return cur_d
        cur_d -= datetime.timedelta(days=1)
    return cur_d


def get_cas_rule_version(dt_or_date_str, exchange="NSE", is_cas_eligible=False):
    """Determine applicable date-effective CAS regulatory regime.

    Regimes:
    - PRE_CAS: Prior to August 3, 2026 (or BSE / non-CAS scrips). Continuous trading 09:15-15:30.
    - CAS_V1: August 3, 2026 through December 6, 2026. 20-minute CAS (15:15-15:35).
    - CAS_V2: December 7, 2026 onward (SEBI Circular 15 Oct 2026). 10-minute CAS (15:30-15:40).
    """
    if not is_cas_eligible or exchange.upper() != "NSE":
        return CASRuleVersion.PRE_CAS
    if isinstance(dt_or_date_str, (datetime.datetime, datetime.date)):
        date_str = dt_or_date_str.strftime("%Y-%m-%d")
    else:
        date_str = str(dt_or_date_str)[:10]

    if date_str < NSE_CAS_V1_EFFECTIVE_DATE:
        return CASRuleVersion.PRE_CAS
    elif date_str < NSE_CAS_V2_EFFECTIVE_DATE:
        return CASRuleVersion.CAS_V1
    else:
        return CASRuleVersion.CAS_V2


def is_cas_active(dt_or_date_str, exchange="NSE", is_cas_eligible=False):
    """Check if Closing Auction Session (CAS) rules are active on the given date.

    CAS is effective since August 3, 2026 on NSE for eligible F&O scrips.
    Prior to August 3, 2026, or for non-CAS / BSE scrips, standard continuous session rules apply.
    """
    return get_cas_rule_version(dt_or_date_str, exchange=exchange, is_cas_eligible=is_cas_eligible) in (
        CASRuleVersion.CAS_V1,
        CASRuleVersion.CAS_V2,
    )


def is_unexecuted_order_transition_active(dt_or_date_str):
    """Check if SEBI unexecuted order automatic transition to CAS is active.

    Scheduled for February 8, 2027 per SEBI Circular HO/47/11/11(3)2025-MRD-POD2/I/23843/2026.
    Must not be applied prematurely before this date.
    """
    if isinstance(dt_or_date_str, (datetime.datetime, datetime.date)):
        date_str = dt_or_date_str.strftime("%Y-%m-%d")
    else:
        date_str = str(dt_or_date_str)[:10]
    return date_str >= SEBI_UNEXECUTED_ORDER_TRANSITION_DATE


def get_segment_session_hours(segment, dt_or_date_str, exchange="NSE", is_cas_eligible=False):
    """Return session structure and boundary hours for a given segment and date.

    Ensures clear segregation between Cash Equity, Equity Derivatives, and Special Sessions.
    """
    if isinstance(dt_or_date_str, (datetime.datetime, datetime.date)):
        date_str = dt_or_date_str.strftime("%Y-%m-%d")
    else:
        date_str = str(dt_or_date_str)[:10]

    special = get_special_session(date_str, exchange=exchange)
    if special:
        return {
            "segment": MarketSegment.SPECIAL_SESSION,
            "exchange": exchange,
            "date": date_str,
            "session_type": special.get("session_type", "SPECIAL"),
            "pre_open_start": special.get("pre_open_start"),
            "pre_open_end": special.get("pre_open_end"),
            "continuous_open": special.get("open_time"),
            "continuous_close": special.get("close_time"),
            "has_cas": False,
            "cas_rule_version": CASRuleVersion.PRE_CAS,
        }

    if segment == MarketSegment.EQUITY_DERIVATIVES:
        return {
            "segment": MarketSegment.EQUITY_DERIVATIVES,
            "exchange": exchange,
            "date": date_str,
            "pre_open_start": None,
            "pre_open_end": None,
            "continuous_open": "09:15",
            "continuous_close": "15:30",
            "has_cas": False,
            "cas_rule_version": CASRuleVersion.PRE_CAS,
            "settlement_computation": "15:30-15:40",
            "post_close": "15:40-16:00",
        }

    rule_ver = get_cas_rule_version(date_str, exchange=exchange, is_cas_eligible=is_cas_eligible)
    if rule_ver == CASRuleVersion.CAS_V2:
        return {
            "segment": MarketSegment.CASH_EQUITY,
            "exchange": exchange,
            "date": date_str,
            "pre_open_start": "09:00",
            "pre_open_end": "09:08",
            "continuous_open": "09:15",
            "continuous_close": "15:30",
            "has_cas": True,
            "cas_rule_version": CASRuleVersion.CAS_V2,
            "cas_open": "15:30",
            "cas_close": "15:40",
            "cas_matching": "15:40-15:45",
            "post_close": "15:50-16:00",
        }
    elif rule_ver == CASRuleVersion.CAS_V1:
        return {
            "segment": MarketSegment.CASH_EQUITY,
            "exchange": exchange,
            "date": date_str,
            "pre_open_start": "09:00",
            "pre_open_end": "09:08",
            "continuous_open": "09:15",
            "continuous_close": "15:15",
            "has_cas": True,
            "cas_rule_version": CASRuleVersion.CAS_V1,
            "cas_open": "15:15",
            "cas_close": "15:35",
            "cas_matching": "15:30-15:35",
            "post_close": "15:50-16:00",
        }
    else:
        return {
            "segment": MarketSegment.CASH_EQUITY,
            "exchange": exchange,
            "date": date_str,
            "pre_open_start": "09:00",
            "pre_open_end": "09:08",
            "continuous_open": "09:15",
            "continuous_close": "15:30",
            "has_cas": False,
            "cas_rule_version": CASRuleVersion.PRE_CAS,
            "closing_buffer": "15:30-15:40",
            "post_close": "15:40-16:00",
        }


def get_session_bars(dt_or_date_str, bar_minutes=DEFAULT_BAR_MINUTES, exchange="NSE", is_cas_eligible=False, segment=MarketSegment.CASH_EQUITY):
    """Calculate the exact number of continuous session bars for a given date.

    - Segment EQUITY_DERIVATIVES (09:15-15:30) = 75 five-minute bars (unaffected by CAS).
    - Segment CASH_EQUITY:
      - Non-CAS cash equities (09:15-15:30) = 75 five-minute bars.
      - CAS V1 (2026-08-03 to 2026-12-06): 72 five-minute bars (09:15-15:15).
      - CAS V2 (from 2026-12-07 per SEBI 15 Oct 2026 circular): 75 five-minute bars (09:15-15:30).
      - Historical backtests prior to 2026-08-03 = 75 five-minute bars.
    - Special sessions (e.g. 1-hour Muhurat) = dynamically calculated duration // bar_minutes (e.g. 12 bars).
    """
    if isinstance(dt_or_date_str, (datetime.datetime, datetime.date)):
        date_str = dt_or_date_str.strftime("%Y-%m-%d")
    else:
        date_str = str(dt_or_date_str)[:10]

    special = get_special_session(date_str, exchange=exchange)
    if special:
        o_h, o_m = [int(x) for x in special["open_time"].split(":")]
        c_h, c_m = [int(x) for x in special["close_time"].split(":")]
        duration_minutes = (c_h * 60 + c_m) - (o_h * 60 + o_m)
        return max(1, duration_minutes // bar_minutes)

    if segment == MarketSegment.EQUITY_DERIVATIVES:
        return NORMAL_SESSION_BARS

    rule_ver = get_cas_rule_version(date_str, exchange=exchange, is_cas_eligible=is_cas_eligible)
    if rule_ver == CASRuleVersion.CAS_V1:
        return CAS_V1_SESSION_BARS

    return NORMAL_SESSION_BARS


def get_session_phase(dt, exchange="NSE", is_cas_eligible=False, segment=MarketSegment.CASH_EQUITY):
    """Return current Indian market session phase with granular breakdown.

    Handles three distinct CAS eras:
    1. Pre-CAS (before 2026-08-03), BSE, and Non-CAS cash securities:
       - 09:00 - 09:08: pre_open_entry
       - 09:08 - 09:12: order_matching
       - 09:12 - 09:15: buffer
       - 09:15 - 15:30: regular (continuous trading, 75 bars)
       - 15:30 - 15:40: closing_buffer
       - 15:40 - 16:00: post_closing

    2. CAS V1 (2026-08-03 to 2026-12-06, NSE CAS-eligible):
       - 09:00 - 09:15: pre-open sequence
       - 09:15 - 15:15: regular (continuous trading, 72 bars)
       - 15:15 - 15:20: cas_transition (reference calculation)
       - 15:20 - 15:25: cas_order_entry_all (market and limit orders)
       - 15:25 - 15:28: cas_order_entry_limit_only (limit orders only)
       - 15:28 - 15:30: cas_order_entry_limit_only (random closure active)
       - 15:30 - 15:35: cas_matching (order matching)
       - 15:35 - 15:50: cas_buffer
       - 15:50 - 16:00: post_closing

    3. CAS V2 (from 2026-12-07 per SEBI Circular 15 Oct 2026):
       - 09:00 - 09:15: pre-open sequence
       - 09:15 - 15:30: regular (continuous trading, 75 bars)
       - 15:30 - 15:31: cas_transition (reference calculation, 1 min)
       - 15:31 - 15:36: cas_order_entry_all (market and limit orders, 5 min)
       - 15:36 - 15:38: cas_order_entry_limit_only (limit orders only, 2 min)
       - 15:38 - 15:40: cas_order_entry_limit_only (random closure active, 2 min)
       - 15:40 - 15:45: cas_matching (order matching)
       - 15:45 - 15:50: cas_buffer
       - 15:50 - 16:00: post_closing

    4. Equity Derivatives segment:
       - Continuous trading 09:15 - 15:30 (unaffected by cash CAS).
    """
    dt_ist = parse_ist(dt)
    date_str = dt_ist.strftime("%Y-%m-%d")
    special = get_special_session(date_str, exchange=exchange)

    # Special session schedule handling (e.g. Muhurat evening trading)
    if special:
        cur_min = dt_ist.hour * 60 + dt_ist.minute
        po_s_h, po_s_m = [int(x) for x in special["pre_open_start"].split(":")]
        po_e_h, po_e_m = [int(x) for x in special["pre_open_end"].split(":")]
        op_h, op_m = [int(x) for x in special["open_time"].split(":")]
        cl_h, cl_m = [int(x) for x in special["close_time"].split(":")]

        po_start = po_s_h * 60 + po_s_m
        po_end = po_e_h * 60 + po_e_m
        open_m = op_h * 60 + op_m
        close_m = cl_h * 60 + cl_m

        if cur_min < po_start:
            return "closed", "before_special_session"
        if po_start <= cur_min < po_end:
            return "pre_open_entry", "special_pre_open"
        if po_end <= cur_min < open_m:
            return "buffer", "special_buffer"
        if open_m <= cur_min < close_m:
            return "regular", "special_trading_session"
        return "closed", "after_special_session"

    # Regular day checks
    is_trading, status, reason = is_trading_day_in(dt_ist, exchange=exchange)
    if not is_trading:
        return "closed", reason

    cur_min = dt_ist.hour * 60 + dt_ist.minute

    # Equity Derivatives Segment (continuous 09:15-15:30, unaffected by CAS)
    if segment == MarketSegment.EQUITY_DERIVATIVES:
        if cur_min < 9 * 60 + 15:
            return "closed", "before_derivatives_open"
        if 9 * 60 + 15 <= cur_min < 15 * 60 + 30:
            return "regular", "continuous_trading"
        if 15 * 60 + 30 <= cur_min < 15 * 60 + 40:
            return "closing_buffer", "derivatives_settlement_price_computation"
        if 15 * 60 + 40 <= cur_min < 16 * 60:
            return "post_closing", "post_closing_session"
        return "closed", "after_hours"

    # Pre-open phases are common across cash equity segments
    if cur_min < 9 * 60:
        return "closed", "before_pre_open"
    if 9 * 60 <= cur_min < 9 * 60 + 8:
        return "pre_open_entry", "pre_open_order_entry"
    if 9 * 60 + 8 <= cur_min < 9 * 60 + 12:
        return "order_matching", "pre_open_order_matching"
    if 9 * 60 + 12 <= cur_min < 9 * 60 + 15:
        return "buffer", "pre_market_buffer"

    rule_ver = get_cas_rule_version(date_str, exchange=exchange, is_cas_eligible=is_cas_eligible)

    # 1. CAS V2: Revised 10-minute CAS regime (From 2026-12-07 per SEBI Circular 15 Oct 2026)
    if rule_ver == CASRuleVersion.CAS_V2:
        if 9 * 60 + 15 <= cur_min < 15 * 60 + 30:
            return "regular", "continuous_trading"
        if 15 * 60 + 30 <= cur_min < 15 * 60 + 31:
            return "cas_transition", "reference_price_calculation_and_transition"
        if 15 * 60 + 31 <= cur_min < 15 * 60 + 36:
            return "cas_order_entry_all", "market_and_limit_order_entry"
        if 15 * 60 + 36 <= cur_min < 15 * 60 + 38:
            return "cas_order_entry_limit_only", "limit_orders_only"
        if 15 * 60 + 38 <= cur_min < 15 * 60 + 40:
            return "cas_order_entry_limit_only", "limit_orders_only_random_closure_active"
        if 15 * 60 + 40 <= cur_min < 15 * 60 + 45:
            return "cas_matching", "order_matching_and_trade_confirmation"
        if 15 * 60 + 45 <= cur_min < 15 * 60 + 50:
            return "cas_buffer", "post_auction_transition"
        if 15 * 60 + 50 <= cur_min < 16 * 60:
            return "post_closing", "post_closing_session"
        return "closed", "after_hours"

    # 2. CAS V1: Original 20-minute CAS regime (2026-08-03 through 2026-12-06)
    if rule_ver == CASRuleVersion.CAS_V1:
        if 9 * 60 + 15 <= cur_min < 15 * 60 + 15:
            return "regular", "continuous_trading"
        if 15 * 60 + 15 <= cur_min < 15 * 60 + 20:
            return "cas_transition", "reference_price_calculation_and_transition"
        if 15 * 60 + 20 <= cur_min < 15 * 60 + 25:
            return "cas_order_entry_all", "market_and_limit_order_entry"
        if 15 * 60 + 25 <= cur_min < 15 * 60 + 28:
            return "cas_order_entry_limit_only", "limit_orders_only"
        if 15 * 60 + 28 <= cur_min < 15 * 60 + 30:
            return "cas_order_entry_limit_only", "limit_orders_only_random_closure_active"
        if 15 * 60 + 30 <= cur_min < 15 * 60 + 35:
            return "cas_matching", "order_matching_and_trade_confirmation"
        if 15 * 60 + 35 <= cur_min < 15 * 60 + 50:
            return "cas_buffer", "post_auction_transition"
        if 15 * 60 + 50 <= cur_min < 16 * 60:
            return "post_closing", "post_closing_session"
        return "closed", "after_hours"

    # 3. Standard continuous schedule (Pre-CAS, Non-CAS, or BSE)
    if 9 * 60 + 15 <= cur_min < 15 * 60 + 30:
        return "regular", "continuous_trading"
    if 15 * 60 + 30 <= cur_min < 15 * 60 + 40:
        return "closing_buffer", "closing_price_computation"
    if 15 * 60 + 40 <= cur_min < 16 * 60:
        return "post_closing", "post_closing_session"
    return "closed", "after_hours"


def is_market_hours_in(dt, exchange="NSE", is_cas_eligible=False, segment=MarketSegment.CASH_EQUITY):
    """Return True if dt is currently within the active continuous trading session."""
    phase, _ = get_session_phase(dt, exchange=exchange, is_cas_eligible=is_cas_eligible, segment=segment)
    return phase == "regular"


def get_cas_order_permissions(dt, exchange="NSE", is_cas_eligible=False):
    """Return granular order-entry permissions during CAS phases.

    Respects date-effective rules:
    - CAS V1 (2026-08-03 to 2026-12-06):
      - 15:15 - 15:20: Transition (No orders).
      - 15:20 - 15:25: Market and Limit orders allowed.
      - 15:25 - 15:30: Only Limit orders allowed (random closure 15:28 - 15:30).
      - 15:30 - 15:35: Order matching (No orders).

    - CAS V2 (From 2026-12-07 per SEBI circular dated 15 Oct 2026):
      - 15:30 - 15:31: Transition (No orders).
      - 15:31 - 15:36: Market and Limit orders allowed.
      - 15:36 - 15:40: Only Limit orders allowed (random closure 15:38 - 15:40).
      - 15:40 - 15:45: Order matching (No orders).
    """
    dt_ist = parse_ist(dt)
    date_str = dt_ist.strftime("%Y-%m-%d")
    rule_ver = get_cas_rule_version(date_str, exchange=exchange, is_cas_eligible=is_cas_eligible)

    if rule_ver == CASRuleVersion.PRE_CAS:
        return {
            "cas_active": False,
            "cas_rule_version": CASRuleVersion.PRE_CAS,
            "phase": None,
            "allow_market_orders": False,
            "allow_limit_orders": False,
            "allow_modifications": False,
            "allow_cancellations": False,
            "random_closure_active": False,
            "reason": "cas_not_active",
        }

    cur_min = dt_ist.hour * 60 + dt_ist.minute
    phase, reason = get_session_phase(dt_ist, exchange=exchange, is_cas_eligible=True)

    if rule_ver == CASRuleVersion.CAS_V2:
        if 15 * 60 + 31 <= cur_min < 15 * 60 + 36:
            return {
                "cas_active": True,
                "cas_rule_version": CASRuleVersion.CAS_V2,
                "phase": phase,
                "allow_market_orders": True,
                "allow_limit_orders": True,
                "allow_modifications": True,
                "allow_cancellations": True,
                "random_closure_active": False,
                "reason": reason,
            }
        elif 15 * 60 + 36 <= cur_min < 15 * 60 + 38:
            return {
                "cas_active": True,
                "cas_rule_version": CASRuleVersion.CAS_V2,
                "phase": phase,
                "allow_market_orders": False,
                "allow_limit_orders": True,
                "allow_modifications": True,
                "allow_cancellations": True,
                "random_closure_active": False,
                "reason": reason,
            }
        elif 15 * 60 + 38 <= cur_min < 15 * 60 + 40:
            return {
                "cas_active": True,
                "cas_rule_version": CASRuleVersion.CAS_V2,
                "phase": phase,
                "allow_market_orders": False,
                "allow_limit_orders": True,
                "allow_modifications": True,
                "allow_cancellations": True,
                "random_closure_active": True,
                "reason": reason,
            }
        else:
            return {
                "cas_active": True,
                "cas_rule_version": CASRuleVersion.CAS_V2,
                "phase": phase,
                "allow_market_orders": False,
                "allow_limit_orders": False,
                "allow_modifications": False,
                "allow_cancellations": False,
                "random_closure_active": False,
                "reason": reason,
            }

    # rule_ver == CASRuleVersion.CAS_V1
    if 15 * 60 + 20 <= cur_min < 15 * 60 + 25:
        return {
            "cas_active": True,
            "cas_rule_version": CASRuleVersion.CAS_V1,
            "phase": phase,
            "allow_market_orders": True,
            "allow_limit_orders": True,
            "allow_modifications": True,
            "allow_cancellations": True,
            "random_closure_active": False,
            "reason": reason,
        }
    elif 15 * 60 + 25 <= cur_min < 15 * 60 + 28:
        return {
            "cas_active": True,
            "cas_rule_version": CASRuleVersion.CAS_V1,
            "phase": phase,
            "allow_market_orders": False,
            "allow_limit_orders": True,
            "allow_modifications": True,
            "allow_cancellations": True,
            "random_closure_active": False,
            "reason": reason,
        }
    elif 15 * 60 + 28 <= cur_min < 15 * 60 + 30:
        return {
            "cas_active": True,
            "cas_rule_version": CASRuleVersion.CAS_V1,
            "phase": phase,
            "allow_market_orders": False,
            "allow_limit_orders": True,
            "allow_modifications": True,
            "allow_cancellations": True,
            "random_closure_active": True,
            "reason": reason,
        }
    else:
        return {
            "cas_active": True,
            "cas_rule_version": CASRuleVersion.CAS_V1,
            "phase": phase,
            "allow_market_orders": False,
            "allow_limit_orders": False,
            "allow_modifications": False,
            "allow_cancellations": False,
            "random_closure_active": False,
            "reason": reason,
        }


# -----------------------------------------------------------------------------
# Derivatives Expiry Calendar Engine
# -----------------------------------------------------------------------------

def _resolve_date(d):
    """Normalize string, datetime, or date to datetime.date object."""
    if d is None:
        return datetime.datetime.now(IST).date()
    if isinstance(d, str):
        return datetime.datetime.strptime(d.strip(), "%Y-%m-%d").date()
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    raise ValueError("Cannot resolve date: %r" % d)


def get_fallback_expiry_weekday(exchange="NSE", underlying=None):
    """Determine the fallback expiry weekday when not specified in contract masters.

    Authoritative Exchange Specifications (November 2026):
    - NSE: Tuesday (weekday = 1) for NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
      and individual stock derivatives.
    - BSE: SENSEX / general BSE contracts expire on Friday (weekday = 4);
      BSE BANKEX expires on Monday (weekday = 0).
    When the target expiry day is an exchange holiday, the expiry is adjusted
    to the preceding trading day.
    """
    u = str(underlying or "").strip().upper()
    exch = str(exchange or "NSE").strip().upper()
    if u == "BANKEX":
        return 0  # BSE BANKEX: Monday
    if u == "SENSEX" or exch == "BSE":
        return 4  # BSE SENSEX / general BSE: Friday
    # NSE authoritative specification: Tuesday for all index and stock derivatives
    return 1


def get_monthly_expiry(year, month, weekday=None, exchange="NSE", underlying=None):
    """Find the monthly derivative expiry date for a given year and month.

    Uses exchange and underlying fallback conventions when weekday is not explicitly passed:
    - NSE (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, Stocks): Tuesday (1)
    - BSE SENSEX: Friday (4)
    - BSE BANKEX: Monday (0)
    If the target expiry day is an exchange holiday, rolls backward to the preceding trading day.
    """
    if weekday is None:
        weekday = get_fallback_expiry_weekday(exchange=exchange, underlying=underlying)

    import calendar
    _, last_day = calendar.monthrange(year, month)
    curr = datetime.date(year, month, last_day)

    # Walk backward from month-end to find the last occurrence of the target weekday
    while curr.weekday() != weekday:
        curr -= datetime.timedelta(days=1)

    # If that day is a holiday/non-trading day, roll backward to the previous trading day
    while True:
        is_trading, _, _ = is_trading_day_in(curr, exchange=exchange)
        if is_trading:
            return curr
        curr -= datetime.timedelta(days=1)


def get_weekly_expiries(year, month, weekday=None, exchange="NSE", underlying=None):
    """Return all weekly expiry dates for a given month and year.

    Calculates each target weekday in the month, rolling backward if any falls on a holiday.
    """
    if weekday is None:
        weekday = get_fallback_expiry_weekday(exchange=exchange, underlying=underlying)

    import calendar
    _, last_day = calendar.monthrange(year, month)
    expiries = []
    seen = set()

    for day in range(1, last_day + 1):
        dt = datetime.date(year, month, day)
        if dt.weekday() == weekday:
            curr = dt
            while True:
                is_trading, _, _ = is_trading_day_in(curr, exchange=exchange)
                if is_trading:
                    break
                curr -= datetime.timedelta(days=1)
            if curr not in seen:
                seen.add(curr)
                expiries.append(curr)

    expiries.sort()
    return expiries


def get_next_expiry(as_of_date=None, expiry_type="weekly", underlying="NIFTY", exchange="NSE"):
    """Find the earliest active expiry date on or after as_of_date.

    - expiry_type: 'weekly' or 'monthly'
    - underlying: used to determine exchange convention (e.g. SENSEX -> Friday, NIFTY -> Tuesday)
    - exchange: 'NSE' or 'BSE'
    """
    as_of = _resolve_date(as_of_date)
    underlying_clean = str(underlying or "").strip().upper()
    weekday = get_fallback_expiry_weekday(exchange=exchange, underlying=underlying_clean)

    # Check the current month and subsequent months
    curr_year = as_of.year
    curr_month = as_of.month

    for _ in range(3):
        if expiry_type.lower() == "monthly":
            exp = get_monthly_expiry(curr_year, curr_month, weekday=weekday, exchange=exchange, underlying=underlying_clean)
            if exp >= as_of:
                return exp
        else:
            weeklies = get_weekly_expiries(curr_year, curr_month, weekday=weekday, exchange=exchange, underlying=underlying_clean)
            for exp in weeklies:
                if exp >= as_of:
                    return exp

        # Advance to next month
        if curr_month == 12:
            curr_year += 1
            curr_month = 1
        else:
            curr_month += 1

    raise RuntimeError("Could not resolve next expiry after: %s" % as_of)


def days_to_expiry(expiry_date, as_of_date=None, trading_days_only=False, exchange="NSE"):
    """Calculate days remaining until expiry.

    Returns non-negative integer (0 on expiry date, negative if expired).
    """
    exp = _resolve_date(expiry_date)
    as_of = _resolve_date(as_of_date)

    if not trading_days_only:
        return (exp - as_of).days

    if exp < as_of:
        return (exp - as_of).days

    # Count trading days
    count = 0
    curr = as_of
    while curr < exp:
        is_trading, _, _ = is_trading_day_in(curr, exchange=exchange)
        if is_trading:
            count += 1
        curr += datetime.timedelta(days=1)
    return count


def is_expiry_day(check_date=None, underlying="NIFTY", exchange="NSE"):
    """Return True if check_date is a contract expiry day for the given instrument."""
    dt = _resolve_date(check_date)
    next_exp = get_next_expiry(as_of_date=dt, expiry_type="weekly", underlying=underlying, exchange=exchange)
    return next_exp == dt

