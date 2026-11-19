#!/usr/bin/env python3
"""
trader-options: Dedicated Options Intelligence Specialist for Indian Markets.

Identifies, evaluates, models, ranks, and communicates high-quality options opportunities
(BUY CE, BUY PE, SELL CE, SELL PE, and Defined-Risk Spreads) for Indian indices
(NIFTY, BANKNIFTY, FINNIFTY) and F&O equities.

Strict Execution Boundary: Decision-support only; live broker execution disabled.
"""

import argparse
import datetime
import json
import math
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

# Workspace path setup for modular reuse
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

for path in [
    os.path.join(BASE_DIR, "trader-alerts"),
    os.path.join(BASE_DIR, "trader-entry"),
    os.path.join(BASE_DIR, "trader-scanner"),
    os.path.join(BASE_DIR, "trader-budget"),
]:
    if path not in sys.path:
        sys.path.insert(0, path)

import data_in
import instruments
import calendar_in
import entry
import scan
import size
from config.loader import load_config

# Signal Contract States
STATE_WATCHLIST_CANDIDATE = "WATCHLIST CANDIDATE"
STATE_SETUP_FORMING = "SETUP FORMING"
STATE_TRIGGER_CONFIRMED = "TRIGGER CONFIRMED"
STATE_SIGNAL_VALIDATED = "SIGNAL VALIDATED"
STATE_HISTORICAL_ANALYSIS = "HISTORICAL ANALYSIS"
STATE_SIGNAL_REJECTED = "SIGNAL REJECTED"
STATE_INVALIDATED_EXPIRED = "INVALIDATED / EXPIRED"

# Per-setup regime eligibility, transcribed from guides/STRATEGY_SPEC.md's "Eligible Regimes" /
# "Ineligible Regimes" columns. This was previously documented but never consulted anywhere in
# the codebase (scanner, entry, or options) -- the regime classification was computed and then
# discarded. Disclosed here as non-blocking context on every signal (never a rejection reason):
# a human who can flex budget or risk tolerance should see *why* regime looks unfavorable, not
# have the candidate hidden or auto-rejected over it. Bearish setup names (breakdown_intraday,
# orb_breakdown) aren't in the spec's table verbatim; they're the bearish mirror of breakout/orb
# and are mapped to that pair's bearish-regime equivalents.
SETUP_ELIGIBLE_REGIMES = {
    "breakout": ("STRONG_BULLISH", "BULLISH"),
    "breakout_intraday": ("STRONG_BULLISH", "BULLISH"),
    "breakout_20d": ("STRONG_BULLISH", "BULLISH"),
    "orb": ("STRONG_BULLISH", "BULLISH"),
    "breakdown_intraday": ("STRONG_BEARISH", "BEARISH"),
    "orb_breakdown": ("STRONG_BEARISH", "BEARISH"),
    "vwap_reclaim": ("STRONG_BULLISH", "BULLISH", "SIDEWAYS"),
    "vwap_rejection": ("STRONG_BEARISH", "BEARISH"),
    "ema_alignment": ("STRONG_BULLISH", "BULLISH"),
    "rsi_momentum": ("STRONG_BULLISH", "BULLISH"),
    "trend_continuation": ("STRONG_BULLISH", "BULLISH"),
    "trend": ("STRONG_BULLISH", "BULLISH"),
    "pullback": ("STRONG_BULLISH", "BULLISH"),
    "vwap_pullback": ("STRONG_BULLISH", "BULLISH"),
    "reversal": ("BEARISH", "SIDEWAYS", "HIGH_VOLATILITY"),
    "exhaustion_reversal": ("BEARISH", "SIDEWAYS", "HIGH_VOLATILITY"),
}


def _build_regime_context(setup_name: str, broad_regime: Optional[str]) -> Dict[str, Any]:
    """Non-blocking regime disclosure: never a rejection reason, always an explained context."""
    eligible = SETUP_ELIGIBLE_REGIMES.get(str(setup_name or "").lower())
    regime = broad_regime or "UNCERTAIN"
    if not eligible or regime == "UNCERTAIN":
        return {
            "broad_market_regime": regime,
            "setup_eligible_regimes": list(eligible) if eligible else None,
            "regime_aligned": None,
            "regime_note": (
                "Broad market regime could not be classified or this setup has no documented "
                "regime-eligibility mapping; shown without regime context."
            ),
        }
    aligned = regime in eligible
    note = (
        ("Broad market regime (%s) supports this '%s' setup." % (regime, setup_name))
        if aligned else
        (
            "Broad market regime is %s; '%s' is historically eligible only in %s. Not blocked by "
            "regime alone -- shown for your judgment, especially if you can flex budget or risk." %
            (regime, setup_name, "/".join(eligible))
        )
    )
    return {
        "broad_market_regime": regime,
        "setup_eligible_regimes": list(eligible),
        "regime_aligned": aligned,
        "regime_note": note,
    }


# Strategy Identifiers
STRATEGY_BUY_CE = "BUY_CE"
STRATEGY_BUY_PE = "BUY_PE"


def resolve_direction_conflict(bull, bear):
    """(bull, bear, note) when a bullish AND a bearish underlying setup both qualify.
    Only a setup whose trigger actually fired (status "triggered") can win; if both fired, the one
    on the later completed candle wins; otherwise neither does (no decisive trigger)."""
    def fired(x):
        return (x or {}).get("status") == "triggered"

    def when(x):
        return str(((x or {}).get("trigger") or {}).get("close_time") or "")
    bn, rn = (bull or {}).get("setup", "bullish"), (bear or {}).get("setup", "bearish")
    if fired(bull) and not fired(bear):
        return bull, None, "%s triggered; %s only %s" % (bn, rn, (bear or {}).get("status"))
    if fired(bear) and not fired(bull):
        return None, bear, "%s triggered; %s only %s" % (rn, bn, (bull or {}).get("status"))
    if fired(bull) and fired(bear) and when(bull) != when(bear):
        return (bull, None, "%s fired later" % bn) if when(bull) > when(bear) else (None, bear, "%s fired later" % rn)
    return None, None, "%s (%s) and %s (%s) with no decisive trigger" % (
        bn, (bull or {}).get("status"), rn, (bear or {}).get("status"))
STRATEGY_SELL_CE = "SELL_CE"
STRATEGY_SELL_PE = "SELL_PE"
STRATEGY_BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
STRATEGY_BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
STRATEGY_BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"
STRATEGY_BULL_PUT_SPREAD = "BULL_PUT_SPREAD"
STRATEGY_IRON_CONDOR = "IRON_CONDOR"

# Statutory Indian Charge Defaults & Risk Policy
DEFAULT_MAX_LOSS_INR = 2000.0
DEFAULT_MIN_REWARD_RISK = 2.0
# Stage 2 tightening: guides/OPTIONS_RISK_SPEC.md documents a stricter bar (OI >= 50,000, spread
# <= 1.0%) than was previously enforced here (no OI floor at all, 6% spread). These defaults are a
# deliberate interim tightening -- materially stricter than before without being calibrated
# against real NIFTY chain OI/spread distributions -- and are overridable via
# config["options_max_spread_pct"] / config["options_min_open_interest"]. Revisit against real
# chain data before relying on either the old or the fully doc-matching (50,000 / 1.0%) bar.
DEFAULT_SPREAD_TOLERANCE_PCT = 2.0  # max 2% bid-ask spread (was 6.0%; doc target is 1.0%)
DEFAULT_MIN_VOLUME = 500
DEFAULT_MIN_OPEN_INTEREST = 20000  # new floor (was none); doc target is 50,000
TRADING_HOURS_PER_DAY = 6.25

# Theta unit contract: every theta value consumed by model_premium_levels() MUST be expressed as
# decay PER CALENDAR DAY (a 24-hour day), not per trading session.
#   - trader-alerts/data_in.py black_scholes() theta is DERIVED on a /365.0 calendar-day basis
#     (verified by direct inspection of its formula) -- this is a CONFIRMED calendar-day figure.
#   - Upstox-sourced theta (data_in.py UpstoxMarketDataProvider.get_option_chain) is passed through
#     verbatim from the broker's option_greeks.theta field. Upstox's public API documentation does
#     not state a time unit for theta (checked Nov 2026). It is ASSUMED to follow the same
#     industry-standard per-calendar-day convention used by Black-Scholes and virtually all retail
#     options platforms (NSE option chain, Sensibull, Upstox's own UI) -- but this is a documented
#     ASSUMPTION, not a verified guarantee. See data_in.py's `theta_unit` field on each Greeks
#     payload for the per-source verification status.
# Using the 24-hour calendar-day basis here (instead of TRADING_HOURS_PER_DAY) is what makes the
# conversion from "hours held" to "fraction of theta's own time unit" dimensionally consistent.
THETA_TIME_BASIS_HOURS = 24.0

# DEF-01: Configurable Freshness Thresholds (seconds)
DEFAULT_MAX_QUOTE_AGE_SECONDS = 120.0       # 2 minutes for option quote
DEFAULT_MAX_CANDLE_AGE_SECONDS = 600.0      # 10 minutes for 5m underlying candle
DEFAULT_MAX_RETRIEVAL_AGE_SECONDS = 120.0   # 2 minutes for API response retrieval
DEFAULT_MAX_UNDERLYING_SPOT_AGE_SECONDS = 60.0  # 1 minute for live underlying spot quote


def round_tick(price: float, tick: float = 0.05) -> float:
    """Round price to the nearest tick step."""
    if price is None or math.isnan(price):
        return 0.0
    d_p = Decimal(str(price))
    d_t = Decimal(str(tick))
    rounded = (d_p / d_t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * d_t
    return float(rounded)


def _utc_now() -> datetime.datetime:
    """The engine's single wall-clock read (aware UTC), so replays and tests can pin it."""
    return datetime.datetime.now(datetime.timezone.utc)


def get_current_ist_time() -> datetime.datetime:
    """Current time in Indian Standard Time (IST)."""
    return _utc_now() + datetime.timedelta(hours=5, minutes=30)


def parse_timestamp(val: Any) -> Optional[datetime.datetime]:
    """
    Parse a timestamp (int/float UNIX, str ISO-8601, datetime) to timezone-aware UTC datetime.
    Returns None if missing or unparsable (no silent default to current time).
    """
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc)
        except Exception:
            return None
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            # Default naive datetimes to IST
            return val.replace(tzinfo=calendar_in.IST).astimezone(datetime.timezone.utc)
        return val.astimezone(datetime.timezone.utc)
    try:
        ist_dt = calendar_in.parse_ist(val)
        return ist_dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


class OptionsIntelligenceSpecialist:
    """The central engine for options intelligence and analytics."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        streamer: Optional[Any] = None,
        portfolio_path: Optional[str] = None,
        broker_state_path: Optional[str] = None,
    ):
        self.config = config or load_config(market="IN")
        self.streamer = streamer
        self.market = "IN"
        self.portfolio_path = portfolio_path
        self.broker_state_path = broker_state_path
        self.max_loss_budget = float(self.config.get("max_loss_per_trade_inr") or DEFAULT_MAX_LOSS_INR)
        self.min_reward_risk = float(self.config.get("min_reward_risk") or DEFAULT_MIN_REWARD_RISK)
        self.available_capital = float(self.config.get("budget") or self.config.get("capital_inr") or 100000.0)

    def fetch_normalized_chain(
        self,
        symbol: str,
        expiry: Optional[str] = None,
        exchange: Optional[str] = None,
        provider: Optional[str] = None,
        spot_price: Optional[float] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Optional[Any]:
        """Fetch and normalize option chain using authoritative Upstox or fallback provider."""
        canon = instruments.symbols_in.normalize_symbol(symbol) if hasattr(instruments, "symbols_in") else symbol
        exch = exchange or ("BSE" if canon in ("SENSEX", "BANKEX") else "NSE")
        try:
            chain = data_in.fetch_option_chain(
                canon,
                expiry=expiry,
                exchange=exch,
                provider=provider,
                spot_price=spot_price,
                config=self.config,
                now=now,
            )
            return chain
        except Exception:
            return None

    def validate_metadata(
        self,
        symbol: str,
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
        exchange: str = "NSE",
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Validate underlying derivatives metadata against authoritative BOD master."""
        try:
            meta = instruments.resolve_underlying_derivatives_meta(symbol, exchange=exchange)
        except Exception as e:
            return False, None, f"Symbol metadata error: {e}"

        if not meta or not meta.get("available") or not meta.get("is_fo_eligible"):
            return False, None, "Symbol is not eligible for F&O or derivatives metadata unavailable"

        lot_size = meta.get("lot_size")
        if not lot_size or lot_size <= 0:
            return False, None, "Authoritative lot size missing or zero"

        expiries = meta.get("expiries") or []
        if expiry is not None and expiry not in expiries:
            return False, None, f"Expiry {expiry} not in authoritative expiries list: {expiries[:5]}"

        strike_interval = meta.get("strike_interval")
        if strike is not None and strike_interval and strike_interval > 0:
            rem = (strike % strike_interval)
            if not math.isclose(rem, 0.0, abs_tol=1e-3) and not math.isclose(rem, strike_interval, abs_tol=1e-3):
                return False, None, f"Strike {strike} is not a valid interval of {strike_interval}"

        return True, meta, None

    def validate_liquidity(
        self,
        contract: Dict[str, Any],
        max_spread_pct: Optional[float] = None,
        min_volume: Optional[int] = None,
        min_open_interest: Optional[int] = None,
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Validate option liquidity, bid/ask presence, open interest, and spread tightness.

        Thresholds default to config overrides (`options_max_spread_pct`,
        `options_min_volume`, `options_min_open_interest`) when not passed explicitly, falling
        back to the module DEFAULT_* constants. See those constants' comments for the gap against
        guides/OPTIONS_RISK_SPEC.md's documented (stricter) bar.
        """
        if max_spread_pct is None:
            max_spread_pct = float(self.config.get("options_max_spread_pct", DEFAULT_SPREAD_TOLERANCE_PCT))
        if min_volume is None:
            min_volume = int(self.config.get("options_min_volume", DEFAULT_MIN_VOLUME))
        if min_open_interest is None:
            min_open_interest = int(self.config.get("options_min_open_interest", DEFAULT_MIN_OPEN_INTEREST))

        if not contract:
            return False, "Contract missing or null", {}

        bid = float(contract.get("bid") or 0.0)
        ask = float(contract.get("ask") or 0.0)
        ltp = float(contract.get("last_price") or 0.0)
        vol = int(contract.get("volume") or 0)
        oi = int(contract.get("open_interest") or 0)

        metrics = {
            "bid": bid,
            "ask": ask,
            "ltp": ltp,
            "volume": vol,
            "open_interest": oi,
            "spread": round(ask - bid, 2),
            "spread_pct": 0.0,
        }

        # 1. Zero quote check
        if bid <= 0.0:
            return False, "Zero or missing bid price (no buyers in book)", metrics
        if ask <= 0.0:
            return False, "Zero or missing ask price (no sellers in book)", metrics
        if ask < bid:
            return False, f"Inverted market quotes: bid ({bid}) > ask ({ask})", metrics

        # 2. Spread percentage check
        mid = (bid + ask) / 2.0
        spread_pct = round(((ask - bid) / mid) * 100.0, 2)
        metrics["spread_pct"] = spread_pct

        # For low premium options (< Rs 10), allow up to Rs 1.50 spread; otherwise check pct
        if mid >= 10.0 and spread_pct > max_spread_pct:
            return False, f"Bid/ask spread too wide ({spread_pct}% > allowed {max_spread_pct}%)", metrics
        elif mid < 10.0 and (ask - bid) > 1.50:
            return False, f"Bid/ask spread points too wide ({ask - bid:.2f} > 1.50 Rs)", metrics

        # 3. Minimum volume check
        if min_volume > 0 and vol < min_volume:
            return False, f"Contract volume ({vol}) below liquidity threshold ({min_volume})", metrics

        # 4. Minimum open interest check (previously absent entirely -- see DEFAULT_MIN_OPEN_INTEREST)
        if min_open_interest > 0 and oi < min_open_interest:
            return False, f"Open interest ({oi}) below liquidity threshold ({min_open_interest})", metrics

        return True, None, metrics

    def model_premium_levels(
        self,
        entry_premium: float,
        spot_entry: float,
        spot_stop: float,
        spot_t1: float,
        spot_t2: float,
        delta: float,
        gamma: float = 0.0,
        theta: float = 0.0,
        vega: float = 0.0,
        iv: float = 0.0,
        option_type: str = "CE",
        is_seller: bool = False,
        holding_hours: float = 2.0,
        slippage_ticks: int = 1,
        evaluation_time: Optional[datetime.datetime] = None,
    ) -> Optional[Dict[str, float]]:
        """
        Model option premium stops and targets dynamically derived from underlying technical levels.

        Accounts for:
        - Delta: First-order underlying sensitivity
        - Gamma: Second-order convexity
        - Theta: Time decay over holding period (derived from remaining session time or configured window)
        - Vega: Volatility shift sensitivity (conservative zero-benefit assumption at stop)
        - Slippage: 1 tick on entry and exit

        Theta unit contract: `theta` MUST be supplied as decay per CALENDAR day (see
        THETA_TIME_BASIS_HOURS above for the verification status of each data source). The
        holding-period fraction is computed against a 24-hour calendar day, not a 6.25-hour
        trading session, to stay dimensionally consistent with that convention.

        Zero Fabrication Policy: If the Taylor expansion or inputs yield non-viable premium levels
        (e.g. stop above entry for buyer or target below entry for buyer), this function returns None
        (OPTION RISK UNAVAILABLE). It never silently falls back to arbitrary percentages.
        """
        if entry_premium <= 0.05 or spot_entry <= 0:
            return None

        ot = str(option_type).upper()
        d_stop = spot_stop - spot_entry
        d_t1 = spot_t1 - spot_entry
        d_t2 = spot_t2 - spot_entry

        eff_holding_hours = holding_hours
        if evaluation_time is not None:
            try:
                now_ist = evaluation_time.astimezone(calendar_in.IST)
                session_end = now_ist.replace(hour=15, minute=15, second=0, microsecond=0)
                if now_ist < session_end:
                    mins_remaining = max(5.0, (session_end - now_ist).total_seconds() / 60.0)
                    eff_holding_hours = min(holding_hours, max(0.1, mins_remaining / 60.0))
            except Exception:
                pass

        # Theta decay over expected intraday holding window.
        # theta is a PER-CALENDAR-DAY figure (see THETA_TIME_BASIS_HOURS docstring above), so the
        # holding period must be expressed as a fraction of a 24-hour calendar day -- not a 6.25
        # hour trading session -- or the decay applied here is overstated by a ~3.8x factor
        # relative to what the theta value actually represents.
        time_fraction = eff_holding_hours / THETA_TIME_BASIS_HOURS
        theta_decay = abs(theta) * time_fraction

        # Conservative IV impact: No assumed IV expansion to cushion buyer's stop
        iv_impact_stop = 0.0
        iv_impact_target = 0.0

        # Second-order Taylor expansion: dP = Delta * dS + 0.5 * Gamma * (dS ^ 2)
        # Call: Delta > 0. When spot drops (d_stop < 0), Delta * d_stop < 0.
        # Put: Delta < 0. When spot rises (d_stop > 0), Delta * d_stop < 0.
        dp_stop_raw = (delta * d_stop) + (0.5 * gamma * (d_stop ** 2)) - theta_decay + iv_impact_stop
        dp_t1_raw = (delta * d_t1) + (0.5 * gamma * (d_t1 ** 2)) - theta_decay + iv_impact_target
        dp_t2_raw = (delta * d_t2) + (0.5 * gamma * (d_t2 ** 2)) - theta_decay + iv_impact_target

        slippage = slippage_ticks * 0.05

        if not is_seller:
            # Option Buyer:
            # Entry has buy slippage; exit has sell slippage
            exec_entry = entry_premium + slippage
            stop_prem = max(0.05, round_tick(entry_premium + dp_stop_raw - slippage, 0.05))
            t1_prem = max(0.05, round_tick(entry_premium + dp_t1_raw - slippage, 0.05))
            t2_prem = max(0.05, round_tick(entry_premium + dp_t2_raw - slippage, 0.05))

            # Strict Validation: Stop must be strictly below entry, targets strictly above entry
            if stop_prem >= exec_entry or t1_prem <= exec_entry or t2_prem <= t1_prem:
                return None
        else:
            # Option Seller:
            # Entry has sell slippage (lower credit); stop exit has buy slippage (higher debit)
            exec_entry = max(0.05, entry_premium - slippage)
            stop_prem = max(0.05, round_tick(entry_premium + dp_stop_raw + slippage, 0.05))
            t1_prem = max(0.05, round_tick(entry_premium + dp_t1_raw + slippage, 0.05))
            t2_prem = max(0.05, round_tick(entry_premium + dp_t2_raw + slippage, 0.05))

            # Strict Validation: Stop must be strictly above entry, targets strictly below entry
            if stop_prem <= exec_entry or t1_prem >= exec_entry or t2_prem >= t1_prem:
                return None

        return {
            "entry_premium": round_tick(exec_entry, 0.05),
            "stop_loss_premium": stop_prem,
            "target_1_premium": t1_prem,
            "target_2_premium": t2_prem,
            "delta_used": delta,
            "gamma_used": gamma,
            "theta_decay_applied": round(theta_decay, 2),
            "slippage_applied": slippage,
        }

    def calculate_charges_and_net_rr(
        self,
        strategy: str,
        entry_prem: float,
        stop_prem: float,
        t1_prem: float,
        qty: int,
        lot_size: int,
        strike: float,
        option_type: str = "CE",
        as_of_date: Optional[str] = None,
        t2_prem: Optional[float] = None,
        spread_strike: Optional[float] = None,
        spread_entry_prem: Optional[float] = None,
        target_rr_gate: str = "T2",
        primary_delta: Optional[float] = None,
        spread_delta: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute full statutory Indian charges, net PnL, and true Net Risk-to-Reward ratio across T1 and T2 targets.

        Zero Fabrication Policy: if `stop_prem` or `t1_prem` is None (the premium model could not
        produce a viable level), this returns an explicit MODEL_UNAVAILABLE result with every
        numeric field set to None. It never substitutes 0.0 or any other sentinel number for a
        missing premium -- callers must check `model_unavailable` (or the None fields) before
        using this result for risk sizing or R:R display.
        """
        if stop_prem is None or t1_prem is None:
            return {
                "model_unavailable": True,
                "statutory_charges_target": None,
                "statutory_charges_target_2": None,
                "statutory_charges_stop": None,
                "charges_breakdown": None,
                "gross_profit_t1": None,
                "net_profit_t1": None,
                "gross_profit_t2": None,
                "net_profit_t2": None,
                "gross_loss_stop": None,
                "net_loss_stop": None,
                "net_reward_risk": None,
                "net_reward_risk_t1": None,
                "net_reward_risk_t2": None,
            }
        is_seller = strategy in (STRATEGY_SELL_CE, STRATEGY_SELL_PE)
        is_spread = (
            strategy in (
                STRATEGY_BULL_CALL_SPREAD,
                STRATEGY_BEAR_PUT_SPREAD,
                STRATEGY_BEAR_CALL_SPREAD,
                STRATEGY_BULL_PUT_SPREAD,
            )
            and spread_strike is not None
            and spread_entry_prem is not None
        )

        eff_option_type = "PE" if ("PUT" in strategy or option_type == "PE") else "CE"

        charges_t1 = size.calculate_indian_transaction_charges(
            asset_type="OPTIONS",
            side="sell" if is_seller else "buy",
            entry_price=entry_prem,
            exit_price=t1_prem,
            qty=qty,
            trading_style="intraday",
            as_of_date=as_of_date,
            strike=strike,
            option_type=eff_option_type,
        )

        charges_stop = size.calculate_indian_transaction_charges(
            asset_type="OPTIONS",
            side="sell" if is_seller else "buy",
            entry_price=entry_prem,
            exit_price=stop_prem,
            qty=qty,
            trading_style="intraday",
            as_of_date=as_of_date,
            strike=strike,
            option_type=eff_option_type,
        )

        charges_t2 = (
            size.calculate_indian_transaction_charges(
                asset_type="OPTIONS",
                side="sell" if is_seller else "buy",
                entry_price=entry_prem,
                exit_price=t2_prem,
                qty=qty,
                trading_style="intraday",
                as_of_date=as_of_date,
                strike=strike,
                option_type=eff_option_type,
            )
            if t2_prem is not None
            else None
        )

        total_charges_t1 = charges_t1.get("total", 0.0)
        total_charges_stop = charges_stop.get("total", 0.0)
        total_charges_t2 = charges_t2.get("total", 0.0) if charges_t2 is not None else 0.0

        if is_spread:
            # Spread hedge leg charges
            hedge_side = "buy" if is_seller else "sell"
            hedge_charges_stop = size.calculate_indian_transaction_charges(
                asset_type="OPTIONS",
                side=hedge_side,
                entry_price=spread_entry_prem,
                exit_price=0.05,
                qty=qty,
                trading_style="intraday",
                as_of_date=as_of_date,
                strike=spread_strike,
                option_type=eff_option_type,
            )
            hedge_charges_t1 = size.calculate_indian_transaction_charges(
                asset_type="OPTIONS",
                side=hedge_side,
                entry_price=spread_entry_prem,
                exit_price=max(0.05, round(spread_entry_prem * 0.5, 2)),
                qty=qty,
                trading_style="intraday",
                as_of_date=as_of_date,
                strike=spread_strike,
                option_type=eff_option_type,
            )
            hedge_charges_t2 = size.calculate_indian_transaction_charges(
                asset_type="OPTIONS",
                side=hedge_side,
                entry_price=spread_entry_prem,
                exit_price=0.05,
                qty=qty,
                trading_style="intraday",
                as_of_date=as_of_date,
                strike=spread_strike,
                option_type=eff_option_type,
            )
            total_charges_stop += hedge_charges_stop.get("total", 0.0)
            total_charges_t1 += hedge_charges_t1.get("total", 0.0)
            total_charges_t2 += hedge_charges_t2.get("total", 0.0)

            width = abs(spread_strike - strike)
            net_debit = max(0.05, round(entry_prem - spread_entry_prem, 2))
            max_spread_profit = max(0.0, round((width - net_debit) * qty, 2))
            max_spread_loss = round(net_debit * qty, 2)

            if primary_delta is not None and spread_delta is not None and primary_delta != 0:
                net_delta = abs(primary_delta - spread_delta)
                delta_scale = max(0.1, min(1.0, net_delta / abs(primary_delta)))
            elif entry_prem > 0:
                delta_scale = min(1.0, max(0.1, net_debit / entry_prem))
            else:
                delta_scale = 0.5

            if not is_seller:
                p_profit_t1 = (t1_prem - entry_prem) * qty
                p_profit_t2 = (t2_prem - entry_prem) * qty if t2_prem is not None else p_profit_t1
                p_loss_stop = (entry_prem - stop_prem) * qty

                gross_profit_t1 = min(max_spread_profit, max(0.0, round(p_profit_t1 * delta_scale, 2)))
                gross_profit_t2 = min(max_spread_profit, max(0.0, round(p_profit_t2 * delta_scale, 2)))
                gross_loss_stop = min(max_spread_loss, max(0.05 * qty, round(p_loss_stop * delta_scale, 2)))
            else:
                p_profit_t1 = (entry_prem - t1_prem) * qty
                p_profit_t2 = (entry_prem - t2_prem) * qty if t2_prem is not None else p_profit_t1
                p_loss_stop = (stop_prem - entry_prem) * qty

                gross_profit_t1 = min(max_spread_profit, max(0.0, round(p_profit_t1 * delta_scale, 2)))
                gross_profit_t2 = min(max_spread_profit, max(0.0, round(p_profit_t2 * delta_scale, 2)))
                gross_loss_stop = min(max_spread_loss, max(0.05 * qty, round(p_loss_stop * delta_scale, 2)))
        else:
            if not is_seller:
                gross_profit_t1 = (t1_prem - entry_prem) * qty
                gross_profit_t2 = (t2_prem - entry_prem) * qty if t2_prem is not None else 0.0
                gross_loss_stop = (entry_prem - stop_prem) * qty
            else:
                gross_profit_t1 = (entry_prem - t1_prem) * qty
                gross_profit_t2 = (entry_prem - t2_prem) * qty if t2_prem is not None else 0.0
                gross_loss_stop = (stop_prem - entry_prem) * qty

        net_profit_t1 = max(0.0, gross_profit_t1 - total_charges_t1)
        net_profit_t2 = max(0.0, gross_profit_t2 - total_charges_t2) if t2_prem is not None else 0.0
        net_loss_stop = gross_loss_stop + total_charges_stop

        net_rr_t1 = round(net_profit_t1 / net_loss_stop, 2) if net_loss_stop > 0 else 0.0
        net_rr_t2 = round(net_profit_t2 / net_loss_stop, 2) if (net_loss_stop > 0 and t2_prem is not None) else 0.0

        if target_rr_gate == "T2" and t2_prem is not None:
            net_rr = net_rr_t2
        else:
            net_rr = net_rr_t1

        return {
            "statutory_charges_target": round(total_charges_t1, 2),
            "statutory_charges_target_2": round(total_charges_t2, 2),
            "statutory_charges_stop": round(total_charges_stop, 2),
            "charges_breakdown": {
                "brokerage_roundtrip": charges_t1.get("brokerage", 40.0),
                "stt": charges_t1.get("stt", 0.0),
                "exchange_charges": charges_t1.get("exchange_charges", 0.0),
                "sebi_charges": charges_t1.get("sebi_charges", 0.0),
                "stamp_duty": charges_t1.get("stamp_duty", 0.0),
                "gst": charges_t1.get("gst", 0.0),
            },
            "gross_profit_t1": round(gross_profit_t1, 2),
            "net_profit_t1": round(net_profit_t1, 2),
            "gross_profit_t2": round(gross_profit_t2, 2),
            "net_profit_t2": round(net_profit_t2, 2),
            "gross_loss_stop": round(gross_loss_stop, 2),
            "net_loss_stop": round(net_loss_stop, 2),
            "net_reward_risk": net_rr,
            "net_reward_risk_t1": net_rr_t1,
            "net_reward_risk_t2": net_rr_t2,
        }

    def evaluate_margin_and_capital(
        self,
        strategy: str,
        strike: float,
        lot_size: int,
        entry_prem: float,
        lots: int = 1,
        spread_strike: Optional[float] = None,
        spread_prem: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Calculate capital required, margin requirements, and tail risk.
        Clearly labels static notional margin as an unverified heuristic estimate.
        """
        qty = lots * lot_size
        notional_per_lot = strike * lot_size

        if strategy in (STRATEGY_BUY_CE, STRATEGY_BUY_PE):
            capital_required = round(entry_prem * qty, 2)
            capital_per_lot = round(entry_prem * lot_size, 2)
            # In option buying, max loss = 100% premium paid + statutory transaction costs (brokerage, STT, stamp duty, GST)
            try:
                chg_zero = size.calculate_indian_transaction_charges(
                    asset_type="OPTIONS",
                    side="buy",
                    entry_price=entry_prem,
                    exit_price=0.0,
                    qty=qty,
                    trading_style="intraday",
                    strike=strike,
                    option_type="CE" if strategy == STRATEGY_BUY_CE else "PE",
                )
                tot_chg = chg_zero.get("total", 0.0)
            except Exception:
                tot_chg = round(20.0 + (capital_required * 0.0006), 2)
            max_loss_val = round(capital_required + tot_chg, 2)
            max_loss_desc = f"Rs {max_loss_val:,.2f} (Premium Outlay Rs {capital_required:,.2f} + Transaction Charges Rs {tot_chg:,.2f})"
            return {
                "structure": "DEFINED_RISK",
                "capital_required": capital_required,
                "capital_per_lot": capital_per_lot,
                "margin_required": 0.0,
                "margin_per_lot": 0.0,
                "max_loss": max_loss_desc,
                "max_loss_value": max_loss_val,
                "premium_outlay": capital_required,
                "transaction_costs_at_zero": tot_chg,
                "margin_type": "EXACT_DEBIT_PAID",
                "margin_model": "EXACT_DEBIT_PAID (Premium × Quantity)",
                "formula": "Entry Premium × Lots × Lot Size + Applicable Transaction Charges",
                "assumptions": "Standard exchange option buying: 100% upfront premium payment plus round-trip statutory costs.",
                "is_authoritative": True,
                "broker_margin_disclaimer": "Exact capital outlay is determined by premium fill and statutory exchange charges.",
                "tail_risk": f"Limited to total premium paid plus statutory charges (Rs {max_loss_val:,.2f}). No margin calls possible.",
                "is_undefined_risk": False,
            }
        elif strategy == STRATEGY_SELL_CE:
            # Naked Call Selling: Unlimited risk
            margin_rate = 0.135
            span_exposure_margin_per_lot = round(notional_per_lot * margin_rate, 2)
            total_margin = round(span_exposure_margin_per_lot * lots, 2)
            premium_received = round(entry_prem * qty, 2)
            max_loss_desc = "THEORETICALLY UNLIMITED"
            tail_risk = (
                "CRITICAL: Naked Call selling carries THEORETICALLY UNLIMITED MAXIMUM LOSS. "
                "Extreme upside moves will trigger rapid margin calls and severe capital liquidation."
            )
            return {
                "structure": "UNDEFINED_RISK_NAKED",
                "capital_required": total_margin,
                "capital_per_lot": span_exposure_margin_per_lot,
                "margin_required": total_margin,
                "margin_per_lot": span_exposure_margin_per_lot,
                "premium_received": premium_received,
                "max_profit": premium_received,
                "max_loss": max_loss_desc,
                "margin_type": "HEURISTIC_MARGIN_ESTIMATE",
                "margin_model": "HEURISTIC_ESTIMATE (HEURISTIC_MARGIN_ESTIMATE: 13.5% of contract notional proxy)",
                "formula": "Strike × Lot Size × 13.5%",
                "assumptions": "Heuristic proxy (13.5% of contract notional). Unverified exchange SPAN clearing array. Does not include broker-specific margin multipliers.",
                "is_authoritative": False,
                "broker_margin_disclaimer": "Actual broker margin requirements may differ significantly based on clearing member risk rules, live exchange SPAN parameters, and market volatility.",
                "tail_risk": tail_risk,
                "is_undefined_risk": True,
            }
        elif strategy == STRATEGY_SELL_PE:
            # Naked Put Selling (DEF-02): Capped maximum loss at (Strike - Premium) * Qty
            margin_rate = 0.135
            span_exposure_margin_per_lot = round(notional_per_lot * margin_rate, 2)
            total_margin = round(span_exposure_margin_per_lot * lots, 2)
            premium_received = round(entry_prem * qty, 2)
            max_loss_val = round((strike - entry_prem) * qty, 2)
            max_loss_desc = f"Rs {max_loss_val:,.2f} (Capped at (strike - premium) if underlying collapses to 0)"
            tail_risk = (
                "SEVERE TAIL RISK: Naked Put selling carries SUBSTANTIAL MAXIMUM LOSS capped only by underlying "
                f"falling to 0 (Max loss: Rs {max_loss_val:,.2f}). Sharp market gaps will cause heavy capital drawdown."
            )
            return {
                "structure": "CAPPED_MAX_LOSS_NAKED",
                "capital_required": total_margin,
                "capital_per_lot": span_exposure_margin_per_lot,
                "margin_required": total_margin,
                "margin_per_lot": span_exposure_margin_per_lot,
                "premium_received": premium_received,
                "max_profit": premium_received,
                "max_loss": max_loss_desc,
                "max_loss_value": max_loss_val,
                "margin_type": "HEURISTIC_MARGIN_ESTIMATE",
                "margin_model": "HEURISTIC_ESTIMATE (HEURISTIC_MARGIN_ESTIMATE: 13.5% of contract notional proxy)",
                "formula": "Strike × Lot Size × 13.5%",
                "assumptions": "Heuristic proxy (13.5% of contract notional). Unverified exchange SPAN clearing array. Does not include broker-specific margin multipliers.",
                "is_authoritative": False,
                "broker_margin_disclaimer": "Actual broker margin requirements may differ significantly based on clearing member risk rules, live exchange SPAN parameters, and market volatility.",
                "tail_risk": tail_risk,
                "is_undefined_risk": False,
            }
        elif strategy in (
            STRATEGY_BULL_CALL_SPREAD,
            STRATEGY_BEAR_PUT_SPREAD,
            STRATEGY_BEAR_CALL_SPREAD,
            STRATEGY_BULL_PUT_SPREAD,
        ):
            if spread_strike is None or spread_prem is None:
                return {
                    "structure": "DEFINED_RISK_SPREAD",
                    "capital_required": 0.0,
                    "capital_per_lot": 0.0,
                    "margin_required": 0.0,
                    "margin_per_lot": 0.0,
                    "max_loss": "CALCULATION_UNAVAILABLE",
                    "tail_risk": "Missing spread leg",
                    "is_undefined_risk": False,
                }

            width = abs(spread_strike - strike)
            if strategy in (STRATEGY_BULL_CALL_SPREAD, STRATEGY_BEAR_PUT_SPREAD):
                # Debit Spread
                net_debit = max(0.0, entry_prem - spread_prem)
                max_loss = round(net_debit * qty, 2)
                max_profit = round((width - net_debit) * qty, 2)
                capital = max_loss
                margin = 0.0
            else:
                # Credit Spread (Hedged Option Selling)
                net_credit = max(0.0, entry_prem - spread_prem)
                max_profit = round(net_credit * qty, 2)
                max_loss = round((width - net_credit) * qty, 2)
                capital = max_loss
                margin = round(max_loss * 1.10, 2)

            return {
                "structure": "DEFINED_RISK_SPREAD",
                "capital_required": capital,
                "capital_per_lot": round(capital / max(1, lots), 2),
                "margin_required": margin,
                "margin_per_lot": round(margin / max(1, lots), 2),
                "max_loss": f"Rs {max_loss:,.2f}",
                "max_profit": f"Rs {max_profit:,.2f}",
                "strike_width": width,
                "margin_type": "SPREAD_CAPPED_MAX_LOSS",
                "margin_model": "SPREAD_CAPPED_MAX_LOSS (Hedged exchange margin benefit estimate)",
                "formula": "Net Debit × Quantity (Debit) or Strike Width × Quantity (Credit)",
                "assumptions": "Defined-risk structure with hedged exchange margin benefit estimate.",
                "is_authoritative": False,
                "broker_margin_disclaimer": "Actual broker margin may vary depending on execution sequence of individual legs and broker margin relief rules.",
                "tail_risk": f"Strictly defined and hedged. Max loss capped at Rs {max_loss:,.2f}.",
                "is_undefined_risk": False,
            }

        return {
            "structure": "CUSTOM",
            "capital_required": 0.0,
            "margin_required": 0.0,
            "max_loss": "UNKNOWN",
            "tail_risk": "Unspecified structure",
            "is_undefined_risk": False,
        }

    def validate_live_data_freshness(
        self,
        contract_data: Dict[str, Any],
        underlying_setup: Dict[str, Any],
        now_utc: datetime.datetime,
        max_quote_age_sec: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        max_candle_age_sec: float = DEFAULT_MAX_CANDLE_AGE_SECONDS,
        max_retrieval_age_sec: float = DEFAULT_MAX_RETRIEVAL_AGE_SECONDS,
    ) -> Tuple[bool, List[str], Dict[str, Any]]:
        """
        DEF-01: Genuine live-data freshness validation.
        A signal may become SIGNAL VALIDATED ONLY when ALL conditions hold:
        1. Indian market session is currently open (calendar_in.is_market_hours_in).
        2. Underlying candle timestamp is valid and within max_candle_age_sec.
        3. Option quote timestamp is valid and within max_quote_age_sec.
        4. Data retrieval timestamp is valid and within max_retrieval_age_sec.
        5. Data provenance is consistent with genuine real-time provider response.
        6. No missing or unparsable timestamps are silently replaced.
        """
        freshness_errors = []
        details = {}

        # 1. Market session check
        is_market_open = calendar_in.is_market_hours_in(now_utc, exchange="NSE")
        details["is_market_open"] = is_market_open
        if not is_market_open:
            freshness_errors.append("MARKET_SESSION_CLOSED: Indian market session (NSE) is currently closed")

        # 2. Underlying candle timestamp check (F-020: Prefer authoritative exchange timestamp)
        candle_ctx = underlying_setup.get("candle_context") or {}
        comp_candle = candle_ctx.get("completed_candle") or {}
        raw_candle_ts = (
            underlying_setup.get("candle_timestamp")
            or underlying_setup.get("last_data_timestamp")
            or comp_candle.get("last_data_timestamp")
            or underlying_setup.get("data_time")
            or comp_candle.get("bar_end")
            or comp_candle.get("time")
            or underlying_setup.get("candle_time")
        )
        session_date = (
            underlying_setup.get("candle_session")
            or candle_ctx.get("candle_session")
            or underlying_setup.get("session_date")
            or (comp_candle.get("bar_end", "").split("T")[0] if "T" in str(comp_candle.get("bar_end", "")) else None)
        )
        if raw_candle_ts and "T" not in str(raw_candle_ts):
            if session_date:
                time_part = str(raw_candle_ts).strip()
                if len(time_part) == 5:
                    time_part = f"{time_part}:00"
                raw_candle_ts = f"{session_date}T{time_part}+05:30"

        details["candle_timestamp_raw"] = raw_candle_ts
        if raw_candle_ts is None or raw_candle_ts == "":
            freshness_errors.append("MISSING_CANDLE_TIMESTAMP: Underlying candle timestamp is missing")
        else:
            candle_dt = parse_timestamp(raw_candle_ts)
            if candle_dt is None:
                freshness_errors.append(f"UNPARSABLE_CANDLE_TIMESTAMP: Could not parse candle timestamp '{raw_candle_ts}'")
            else:
                candle_age = (now_utc - candle_dt).total_seconds()
                details["candle_age_seconds"] = round(candle_age, 1)
                if candle_age > max_candle_age_sec:
                    freshness_errors.append(
                        f"STALE_UNDERLYING_CANDLE: Candle age {candle_age:.1f}s exceeds threshold {max_candle_age_sec}s (timestamp: {raw_candle_ts})"
                    )

        # 3. Option quote timestamp check
        raw_quote_ts = contract_data.get("quote_timestamp") or contract_data.get("timestamp")
        details["quote_timestamp_raw"] = raw_quote_ts
        if raw_quote_ts is None or raw_quote_ts == "":
            freshness_errors.append("MISSING_QUOTE_TIMESTAMP: Option quote timestamp is missing")
        else:
            quote_dt = parse_timestamp(raw_quote_ts)
            if quote_dt is None:
                freshness_errors.append(f"UNPARSABLE_QUOTE_TIMESTAMP: Could not parse quote timestamp '{raw_quote_ts}'")
            else:
                quote_age = (now_utc - quote_dt).total_seconds()
                details["quote_age_seconds"] = round(quote_age, 1)
                if quote_age > max_quote_age_sec:
                    freshness_errors.append(
                        f"STALE_OPTION_QUOTE: Quote age {quote_age:.1f}s exceeds threshold {max_quote_age_sec}s (timestamp: {raw_quote_ts})"
                    )

        # 4. Data retrieval timestamp check
        raw_retrieval_ts = contract_data.get("retrieval_timestamp") or contract_data.get("fetched_at")
        details["retrieval_timestamp_raw"] = raw_retrieval_ts
        if raw_retrieval_ts is None or raw_retrieval_ts == "":
            freshness_errors.append("MISSING_RETRIEVAL_TIMESTAMP: Option retrieval timestamp is missing")
        else:
            retrieval_dt = parse_timestamp(raw_retrieval_ts)
            if retrieval_dt is None:
                freshness_errors.append(f"UNPARSABLE_RETRIEVAL_TIMESTAMP: Could not parse retrieval timestamp '{raw_retrieval_ts}'")
            else:
                retrieval_age = (now_utc - retrieval_dt).total_seconds()
                details["retrieval_age_seconds"] = round(retrieval_age, 1)
                if retrieval_age > max_retrieval_age_sec:
                    freshness_errors.append(
                        f"STALE_RETRIEVAL_DATA: Retrieval age {retrieval_age:.1f}s exceeds threshold {max_retrieval_age_sec}s (timestamp: {raw_retrieval_ts})"
                    )

        # 5. Data provenance check
        provider = str(contract_data.get("provider", "")).lower()
        tier = str(contract_data.get("tier", "")).upper()
        is_live = bool(contract_data.get("is_live", False))
        is_sim = bool(contract_data.get("is_simulated", False))
        details["tier"] = tier
        details["is_live"] = is_live
        details["provider"] = provider

        if not is_live:
            freshness_errors.append("NOT_LIVE_FEED: contract is_live flag is False")
        if tier != "REAL_TIME":
            freshness_errors.append(f"INVALID_DATA_TIER: Data tier '{tier}' is not REAL_TIME")
        if is_sim:
            freshness_errors.append("SIMULATED_DATA: contract is flagged as simulated")

        is_fresh = len(freshness_errors) == 0
        return is_fresh, freshness_errors, details

    def validate_underlying_spot(
        self,
        spot_quote: Any,
        now_utc: datetime.datetime,
        max_age_seconds: float = DEFAULT_MAX_UNDERLYING_SPOT_AGE_SECONDS,
    ) -> Tuple[bool, Optional[float], Optional[str], Dict[str, Any]]:
        """Validate live underlying spot price, quote timestamp, retrieval age, and provenance."""
        details = {}
        if not spot_quote:
            return False, None, "MISSING_UNDERLYING_SPOT: Underlying spot quote payload is missing or empty", details

        if isinstance(spot_quote, (int, float)):
            price = float(spot_quote)
            if price <= 0.0:
                return False, None, f"INVALID_UNDERLYING_SPOT_PRICE: Non-positive underlying spot price ({price})", details
            return False, None, "UNVERIFIED_UNDERLYING_SPOT: Bare price provided without verifiable quote timestamp", details

        if not isinstance(spot_quote, dict):
            return False, None, "INVALID_UNDERLYING_SPOT_FORMAT: Spot quote must be a dictionary or numerical price", details

        raw_price = (
            spot_quote.get("price")
            or spot_quote.get("last_price")
            or spot_quote.get("spot")
            or spot_quote.get("underlying_price")
        )
        if raw_price is None:
            return False, None, "MISSING_UNDERLYING_SPOT_PRICE: Underlying spot quote does not contain a price", details

        try:
            price = float(raw_price)
            if price <= 0.0:
                return False, None, f"INVALID_UNDERLYING_SPOT_PRICE: Non-positive underlying spot price ({price})", details
        except (ValueError, TypeError):
            return False, None, f"INVALID_UNDERLYING_SPOT_PRICE: Unparseable price value '{raw_price}'", details

        details["price"] = price

        if spot_quote.get("is_request_timestamp_only") or spot_quote.get("is_unverified_rest"):
            return False, None, "UNDERLYING_SPOT_UNVERIFIED: REST option chain underlying spot does not carry an authoritative exchange quote timestamp; quote freshness cannot be established", details

        raw_ts = spot_quote.get("quote_timestamp") or spot_quote.get("exchange_timestamp")
        details["quote_timestamp_raw"] = raw_ts
        if raw_ts is None or raw_ts == "":
            return False, None, "UNDERLYING_SPOT_UNVERIFIED: Underlying spot quote exchange timestamp is missing", details

        ts_dt = parse_timestamp(raw_ts)
        if ts_dt is None:
            return False, None, f"UNPARSABLE_UNDERLYING_SPOT_TIMESTAMP: Could not parse spot timestamp '{raw_ts}'", details

        age = (now_utc - ts_dt).total_seconds()
        details["spot_age_seconds"] = round(age, 1)

        if age < -5.0:
            return False, None, f"FUTURE_UNDERLYING_SPOT_TIMESTAMP: Spot timestamp '{raw_ts}' is in the future ({age:.1f}s)", details

        if age > max_age_seconds:
            return False, None, f"STALE_UNDERLYING_SPOT: Spot age {age:.1f}s exceeds threshold {max_age_seconds}s (timestamp: {raw_ts})", details

        raw_retrieval = spot_quote.get("retrieval_timestamp") or spot_quote.get("fetched_at")
        if raw_retrieval:
            retrieval_dt = parse_timestamp(raw_retrieval)
            if retrieval_dt:
                ret_age = (now_utc - retrieval_dt).total_seconds()
                details["retrieval_age_seconds"] = round(ret_age, 1)
                if ret_age > max_age_seconds:
                    return False, None, f"STALE_UNDERLYING_SPOT_RETRIEVAL: Retrieval age {ret_age:.1f}s exceeds threshold {max_age_seconds}s", details

        return True, price, None, details

    def check_portfolio_risk(
        self,
        candidate_risk: float,
        capital_required: float,
        portfolio_path: Optional[str] = None,
        broker_state_path: Optional[str] = None,
        config_path: Optional[str] = None,
        now_utc: Optional[datetime.datetime] = None,
        is_live_check: bool = False,
        candidate_risk_budget: Optional[float] = None,
        allow_unverified_portfolio: bool = False,
    ) -> Tuple[bool, List[str]]:
        """Validate candidate against central trader-risk rules, portfolio state, and daily limits."""
        rejections = []
        try:
            import check as risk_check
        except ImportError:
            risk_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-risk")
            if risk_dir not in sys.path:
                sys.path.insert(0, risk_dir)
            try:
                import check as risk_check
            except Exception:
                risk_check = None

        if risk_check is None:
            if is_live_check:
                return False, ["PORTFOLIO_RISK_UNAVAILABLE: Central trader-risk module could not be imported"]
            return True, []

        now_dt = now_utc or _utc_now()
        today_iso = now_dt.astimezone(calendar_in.IST).strftime("%Y-%m-%d")

        cfg = risk_check.load_config(path=config_path, market="IN")
        p_path = portfolio_path or os.path.join(BASE_DIR, "trader", "portfolio.json")
        b_path = broker_state_path or os.path.join(BASE_DIR, "trader", "state", "broker.json")

        allow_unverified = bool(allow_unverified_portfolio or os.environ.get("ALLOW_UNVERIFIED_PORTFOLIO") == "1")

        try:
            portfolio = risk_check.read_json(p_path, "portfolio", required=False)
        except Exception as e:
            if is_live_check:
                return False, [f"PORTFOLIO_STATE_UNAVAILABLE: Central portfolio state file unreadable or corrupt: {e}"]
            portfolio = None

        try:
            broker_state = risk_check.read_json(b_path, "broker state", required=False)
        except Exception as e:
            if is_live_check:
                return False, [f"BROKER_STATE_UNAVAILABLE: Central broker state file unreadable or corrupt: {e}"]
            broker_state = None

        if is_live_check:
            if portfolio is None:
                if not allow_unverified:
                    rejections.append(
                        "PORTFOLIO_STATE_UNAVAILABLE: Central portfolio state file missing or unreadable; live execution rejected fail-closed by default"
                    )
                    return False, rejections
                else:
                    rejections.append(
                        "UNVERIFIED_PORTFOLIO_OVERRIDE: Central portfolio state missing; test override cannot produce live actionable signals"
                    )

            if broker_state is None:
                if not allow_unverified:
                    rejections.append(
                        "BROKER_STATE_UNAVAILABLE: Central broker state file missing or unreadable; live execution rejected fail-closed by default"
                    )
                    return False, rejections
                else:
                    rejections.append(
                        "UNVERIFIED_BROKER_STATE_OVERRIDE: Central broker state missing; test override cannot produce live actionable signals"
                    )

        # Daily loss limit check (N-1: Authoritative daily loss cap)
        max_daily_loss = cfg.get("max_daily_loss_inr") or cfg.get("max_daily_loss")
        if max_daily_loss:
            allowed_daily = float(max_daily_loss)
            used_loss_today = 0.0
            if broker_state:
                placed = risk_check.placed_today(broker_state, today_iso)
                used_loss_today = sum(float(r.get("max_loss") or 0.0) for r in placed)

            if used_loss_today >= allowed_daily:
                rejections.append(
                    f"DAILY_LOSS_LIMIT_EXHAUSTED: Daily risk used (Rs {used_loss_today:,.2f}) already meets or exceeds configured max daily loss limit (Rs {allowed_daily:,.2f})"
                )
            elif (used_loss_today + candidate_risk) > allowed_daily + 0.01:
                rejections.append(
                    f"DAILY_LOSS_LIMIT_EXCEEDED: Daily risk used (Rs {used_loss_today:,.2f}) + candidate risk (Rs {candidate_risk:,.2f}) "
                    f"exceeds configured max daily loss limit (Rs {allowed_daily:,.2f})"
                )

        # Per-trade risk budget check
        if candidate_risk_budget and candidate_risk > (float(candidate_risk_budget) + 0.01):
            rejections.append(
                f"TRADE_RISK_EXCEEDS_BUDGET: Candidate risk (Rs {candidate_risk:,.2f}) exceeds trade risk budget (Rs {float(candidate_risk_budget):,.2f})"
            )

        # Max open positions check
        max_positions = int(cfg.get("max_open_positions") or 5)
        if portfolio:
            positions = portfolio.get("positions", [])
            open_count = len([p for p in positions if float(p.get("shares") or p.get("qty") or 0.0) > 0])
            if open_count >= max_positions:
                rejections.append(
                    f"MAX_OPEN_POSITIONS_REACHED: Currently {open_count} open positions, which meets or exceeds limit of {max_positions}"
                )

        # Available cash check with buffer
        if portfolio and portfolio.get("cash") is not None:
            cash = float(portfolio.get("cash"))
            buffer_pct = float(cfg.get("cash_buffer_pct") or 10.0)
            avail_cash = max(0.0, cash * (1.0 - buffer_pct / 100.0))
            if capital_required > avail_cash + 0.01:
                rejections.append(
                    f"INSUFFICIENT_PORTFOLIO_CASH: Capital required (Rs {capital_required:,.2f}) exceeds available portfolio cash (Rs {avail_cash:,.2f}) after {buffer_pct}% buffer"
                )

        return len(rejections) == 0, rejections

    def generate_signal_contract(
        self,
        underlying: str,
        direction: str,
        strategy: str,
        option_type: str,
        strike: float,
        contract_data: Dict[str, Any],
        underlying_setup: Dict[str, Any],
        meta: Dict[str, Any],
        spread_contract: Optional[Dict[str, Any]] = None,
        max_loss_budget: Optional[float] = None,
        min_reward_risk: Optional[float] = None,
        available_capital: Optional[float] = None,
        now_utc: Optional[datetime.datetime] = None,
        portfolio_path: Optional[str] = None,
        broker_state_path: Optional[str] = None,
        underlying_spot_quote: Optional[Dict[str, Any]] = None,
        allow_unverified_portfolio: bool = False,
        target_rr_gate: Optional[str] = None,
        broad_regime: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build and rigorously validate the 20-field signal contract.

        Enforces Phase 1 Risk Gating:
        - If risk per lot > max_loss_budget: REJECT (lots = 0)
        - If net R:R < min_reward_risk: REJECT
        - If capital/margin > available_capital: REJECT
        - If data is historical or closed: assign HISTORICAL ANALYSIS (never SIGNAL VALIDATED)
        """
        budget = max_loss_budget if max_loss_budget is not None else self.max_loss_budget
        min_rr = min_reward_risk if min_reward_risk is not None else self.min_reward_risk
        capital_limit = available_capital if available_capital is not None else self.available_capital

        now_dt = now_utc if now_utc is not None else _utc_now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)

        is_seller = strategy in (STRATEGY_SELL_CE, STRATEGY_SELL_PE, STRATEGY_BEAR_CALL_SPREAD, STRATEGY_BULL_PUT_SPREAD)
        is_spread = spread_contract is not None
        lot_size = meta["lot_size"]
        expiry = contract_data.get("expiry") or meta["expiry"]
        contract_sym = contract_data.get("symbol", f"{underlying}_{strike}_{option_type}")

        bid = float(contract_data.get("bid") or 0.0)
        ask = float(contract_data.get("ask") or 0.0)
        ltp = float(contract_data.get("last_price") or 0.0)

        # Execution price reconciliation:
        # BUY CE/PE: use ask as conservative baseline
        # SELL CE/PE: use bid as conservative baseline
        if is_seller:
            baseline_quote = bid if bid > 0.0 else ltp
            baseline_type = "BID" if bid > 0.0 else "LTP_FALLBACK"
            slippage = -0.05
            reconciled_entry = max(0.05, round_tick(baseline_quote + slippage, 0.05))
        else:
            baseline_quote = ask if ask > 0.0 else ltp
            baseline_type = "ASK" if ask > 0.0 else "LTP_FALLBACK"
            slippage = 0.05
            reconciled_entry = round_tick(baseline_quote + slippage, 0.05)

        spread_points = round(ask - bid, 2)
        spread_mid = (bid + ask) / 2.0
        spread_pct = round((spread_points / spread_mid) * 100.0, 2) if spread_mid > 0 else 0.0

        execution_recon = {
            "order_side": "SELL" if is_seller else "BUY",
            "baseline_quote_price": baseline_quote,
            "baseline_quote_type": baseline_type,
            "slippage_applied": slippage,
            "execution_price": reconciled_entry,
            "bid": bid,
            "ask": ask,
            "spread_points": spread_points,
            "spread_pct": spread_pct,
            "quote_provenance": contract_data.get("quote_provenance", "UPSTOX_REST_SNAPSHOT"),
        }

        rejection_reasons = []

        if bid <= 0.0:
            rejection_reasons.append("INVALID_QUOTE: Zero or missing bid quote in order book")
        if ask <= 0.0:
            rejection_reasons.append("INVALID_QUOTE: Zero or missing ask quote in order book")
        if bid > 0.0 and ask > 0.0 and ask <= bid:
            rejection_reasons.append(f"INVALID_QUOTE: Crossed market quotes: bid ({bid}) >= ask ({ask})")

        # Execution-Price Conservatism: Spread & Depth Gating
        opt_spread_tolerance = float(self.config.get("max_option_bid_ask_spread_pct") or DEFAULT_SPREAD_TOLERANCE_PCT)
        if spread_mid >= 10.0 and spread_pct > opt_spread_tolerance:
            rejection_reasons.append(
                f"EXCESSIVE_SPREAD: Bid/ask spread ({spread_pct}%) exceeds maximum tolerance ({opt_spread_tolerance}%)"
            )
        elif spread_mid < 10.0 and spread_points > 1.50:
            rejection_reasons.append(
                f"EXCESSIVE_SPREAD: Bid/ask spread points (Rs {spread_points:.2f}) exceeds maximum tolerance (Rs 1.50)"
            )

        bid_qty = contract_data.get("bid_qty")
        ask_qty = contract_data.get("ask_qty")
        if bid_qty is not None and float(bid_qty) <= 0:
            rejection_reasons.append("INSUFFICIENT_DEPTH: Zero bid quantity available in order book")
        if ask_qty is not None and float(ask_qty) <= 0:
            rejection_reasons.append("INSUFFICIENT_DEPTH: Zero ask quantity available in order book")

        if spread_contract:
            s_bid = float(spread_contract.get("bid") or 0.0)
            s_ask = float(spread_contract.get("ask") or 0.0)
            s_points = round(s_ask - s_bid, 2)
            s_mid = (s_bid + s_ask) / 2.0 if (s_bid + s_ask) > 0 else 1.0
            s_pct = round((s_points / s_mid) * 100.0, 2)
            if s_mid >= 10.0 and s_pct > opt_spread_tolerance:
                rejection_reasons.append(
                    f"EXCESSIVE_SPREAD: Hedge leg bid/ask spread ({s_pct}%) exceeds maximum tolerance ({opt_spread_tolerance}%)"
                )
            elif s_mid < 10.0 and s_points > 1.50:
                rejection_reasons.append(
                    f"EXCESSIVE_SPREAD: Hedge leg bid/ask spread points (Rs {s_points:.2f}) exceeds maximum tolerance (Rs 1.50)"
                )
            s_bid_qty = spread_contract.get("bid_qty")
            s_ask_qty = spread_contract.get("ask_qty")
            if s_bid_qty is not None and float(s_bid_qty) <= 0:
                rejection_reasons.append("INSUFFICIENT_DEPTH: Zero bid quantity available in hedge leg order book")
            if s_ask_qty is not None and float(s_ask_qty) <= 0:
                rejection_reasons.append("INSUFFICIENT_DEPTH: Zero ask quantity available in hedge leg order book")

        u_dec = underlying_setup.get("decision") or {}
        u_trig = underlying_setup.get("trigger") or {}
        u_or = underlying_setup.get("opening_range") or {}
        u_last = underlying_setup.get("last") or {}

        spot_entry = float(
            u_dec.get("entry_reference")
            or underlying_setup.get("entry_reference")
            or underlying_setup.get("price")
            or u_trig.get("price")
            or u_last.get("close")
            or 0.0
        )
        spot_stop = float(
            u_dec.get("stop_loss")
            or u_dec.get("stop_preview")
            or underlying_setup.get("stop_loss")
            or underlying_setup.get("stop_preview")
            or u_dec.get("invalidation_level")
            or underlying_setup.get("invalidation_level")
            or (u_or.get("low") if direction in ("BULLISH", "BULLISH_OR_SIDEWAYS") else u_or.get("high"))
            or spot_entry
        )
        inv_level = float(
            u_dec.get("invalidation_level")
            or underlying_setup.get("invalidation_level")
            or spot_stop
        )
        spot_quote_candidate = (
            underlying_spot_quote
            or underlying_setup.get("underlying_spot_quote")
            or contract_data.get("underlying_spot_quote")
            or underlying_setup.get("live_spot_quote")
        )
        underlying_spot_details = {}
        if spot_quote_candidate is not None:
            v_ok, v_px, v_err, v_meta = self.validate_underlying_spot(spot_quote_candidate, now_utc=now_dt)
            underlying_spot_details = v_meta
            if v_ok and v_px is not None:
                current_spot = v_px
            else:
                rejection_reasons.append(f"UNDERLYING_SPOT_UNAVAILABLE: {v_err}")
                current_spot = float(v_meta.get("price") or spot_entry)
        else:
            current_spot = float(
                underlying_setup.get("current_spot")
                or underlying_setup.get("current_price")
                or u_dec.get("current_price")
                or u_last.get("close")
                or spot_entry
            )
        targets = (
            u_dec.get("targets")
            or u_dec.get("targets_preview")
            or underlying_setup.get("targets")
            or underlying_setup.get("targets_preview")
            or {}
        )
        risk_dist = abs(spot_entry - spot_stop)
        spot_t1 = float(
            targets.get("target_1_1r")
            or targets.get("t1_1r")
            or (spot_entry + 1.5 * risk_dist if direction in ("BULLISH", "BULLISH_OR_SIDEWAYS") else spot_entry - 1.5 * risk_dist)
            if risk_dist > 0 else 0.0
        )
        spot_t2 = float(
            targets.get("target_2_2r")
            or targets.get("t2_2r")
            or (spot_entry + 2.5 * risk_dist if direction in ("BULLISH", "BULLISH_OR_SIDEWAYS") else spot_entry - 2.5 * risk_dist)
            if risk_dist > 0 else 0.0
        )

        # Check invalidation level breach: Current spot breaching invalidation rejects active signal
        is_bullish = ("BULL" in direction or direction == "LONG")
        is_bearish = ("BEAR" in direction or direction == "SHORT")

        if is_bullish:
            if current_spot <= inv_level:
                rejection_reasons.append(
                    f"INVALIDATED_SETUP: Current underlying spot (Rs {current_spot:.2f}) has breached invalidation level (Rs {inv_level:.2f})"
                )
            elif spot_stop >= spot_entry and spot_entry > 0:
                rejection_reasons.append(
                    f"INVALID_RISK_PLAN: Inverted stop level (entry {spot_entry:.2f}, stop {spot_stop:.2f})"
                )
        elif is_bearish:
            if current_spot >= inv_level:
                rejection_reasons.append(
                    f"INVALIDATED_SETUP: Current underlying spot (Rs {current_spot:.2f}) has breached invalidation level (Rs {inv_level:.2f})"
                )
            elif spot_stop <= spot_entry and spot_entry > 0:
                rejection_reasons.append(
                    f"INVALID_RISK_PLAN: Inverted stop level (entry {spot_entry:.2f}, stop {spot_stop:.2f})"
                )

        # Check chase / extension limits
        if (
            u_dec.get("chase_blocked")
            or underlying_setup.get("anti_chasing", {}).get("chase_blocked")
            or underlying_setup.get("sub_state") == "EXTENDED"
            or u_dec.get("sub_state") == "EXTENDED"
        ):
            rejection_reasons.append("EXTENDED_SETUP: Underlying setup is extended past actionable entry zone (chase blocked)")

        entry_zone = u_dec.get("entry_zone") or underlying_setup.get("entry_zone")
        if isinstance(entry_zone, dict):
            z_high = float(entry_zone.get("high") or 0.0)
            z_low = float(entry_zone.get("low") or 0.0)
            if is_bullish and z_high > 0 and current_spot > z_high:
                if not any("EXTENDED_SETUP" in r for r in rejection_reasons):
                    rejection_reasons.append(
                        f"EXTENDED_SETUP: Current spot (Rs {current_spot:.2f}) is above entry zone upper bound (Rs {z_high:.2f})"
                    )
            elif is_bearish and z_low > 0 and current_spot < z_low:
                if not any("EXTENDED_SETUP" in r for r in rejection_reasons):
                    rejection_reasons.append(
                        f"EXTENDED_SETUP: Current spot (Rs {current_spot:.2f}) is below entry zone lower bound (Rs {z_low:.2f})"
                    )

        greeks = contract_data.get("greeks") or {}
        raw_delta = greeks.get("delta")
        raw_gamma = greeks.get("gamma")
        raw_theta = greeks.get("theta")
        raw_vega = greeks.get("vega")
        raw_iv = contract_data.get("iv") if contract_data.get("iv") is not None else greeks.get("iv")

        missing_greeks = []
        # Delta is mandatory across all option strategies
        if raw_delta is None:
            missing_greeks.append("Delta")

        # Gamma is mandatory by default for all strategies (second-order convexity).
        # Missing Gamma must reject with GREEKS_UNAVAILABLE and not silently default to synthetic 0.001.
        if raw_gamma is None:
            missing_greeks.append("Gamma")

        # Theta is mandatory for option sellers (STRATEGY_SELL_CE, STRATEGY_SELL_PE)
        # as time decay is the primary economic engine of short premium.
        # Strategy Exception: For directional option buyers (STRATEGY_BUY_CE, STRATEGY_BUY_PE),
        # positions are evaluated on rapid intraday momentum with instantaneous stop modeling;
        # Theta decay is not required by default (defaults to 0.0 assuming zero decay on stop).
        if is_seller and raw_theta is None:
            missing_greeks.append("Theta")

        # When require_all_greeks is configured True, all provider Greeks are strictly mandatory
        if self.config.get("require_all_greeks"):
            if raw_theta is None and "Theta" not in missing_greeks:
                missing_greeks.append("Theta")
            if raw_vega is None:
                missing_greeks.append("Vega")
            if raw_iv is None:
                missing_greeks.append("IV")

        # Vega & IV Strategy Exception:
        # Intraday directional breakout setups model stop and target levels from underlying
        # technical levels with zero IV expansion assumed (iv_impact_stop = 0.0, iv_impact_target = 0.0).
        # Thus, Vega and IV are not required by default for directional setups.

        if missing_greeks:
            rejection_reasons.append(
                f"GREEKS_UNAVAILABLE: Required Greek(s) missing or None from provider: {', '.join(missing_greeks)}"
            )
            delta = float(raw_delta) if raw_delta is not None else (0.50 if option_type == "CE" else -0.50)
            gamma = float(raw_gamma) if raw_gamma is not None else 0.0
            theta = float(raw_theta) if raw_theta is not None else 0.0
            vega = float(raw_vega) if raw_vega is not None else 0.0
            iv = float(raw_iv) if raw_iv is not None else 0.0
        else:
            # Preserve legitimate numerical zero values without truthiness coercion
            delta = float(raw_delta)
            gamma = float(raw_gamma)
            theta = float(raw_theta) if raw_theta is not None else 0.0
            vega = float(raw_vega) if raw_vega is not None else 0.0
            iv = float(raw_iv) if raw_iv is not None else 0.0

        # Model premium levels with zero fabrication using baseline quote and slippage
        prem_levels = self.model_premium_levels(
            entry_premium=baseline_quote,
            spot_entry=spot_entry,
            spot_stop=spot_stop,
            spot_t1=spot_t1,
            spot_t2=spot_t2,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            iv=iv,
            option_type=option_type,
            is_seller=is_seller,
            slippage_ticks=1,
            evaluation_time=now_dt,
        )

        premium_model_unavailable = not prem_levels
        if premium_model_unavailable:
            rejection_reasons.append("OPTION_RISK_UNAVAILABLE: Delta/Gamma model produced non-viable stop or target premiums")
            rejection_reasons.append(
                "MODEL_UNAVAILABLE: Net R:R, stop-loss, and target premiums cannot be modeled (N/A) for this candidate"
            )
            # Zero Fabrication Policy: an unavailable premium model must never be represented
            # by 0.0 (a concrete, misleading number). Only entry_premium (the observed quote,
            # independent of the Delta/Gamma projection) remains populated; stop/target are
            # explicit None sentinels that every downstream consumer must treat as "unavailable",
            # never as "zero risk" or "zero reward".
            prem_levels = {
                "entry_premium": round_tick(reconciled_entry, 0.05),
                "stop_loss_premium": None,
                "target_1_premium": None,
                "target_2_premium": None,
            }

        execution_recon["execution_price"] = prem_levels["entry_premium"]

        # Calculate per-lot risk & hedge leg reconciliation
        hedge_entry = 0.0
        spread_k = None
        spread_bid = 0.0
        spread_ask = 0.0
        if is_spread and spread_contract:
            # 1. Underlying and Expiry Match
            hedge_und = spread_contract.get("underlying")
            if hedge_und and hedge_und != underlying:
                rejection_reasons.append(
                    f"SPREAD_LEG_MISMATCH: Underlying mismatch between primary ({underlying}) and hedge ({hedge_und})"
                )
            hedge_exp = spread_contract.get("expiry")
            primary_exp = meta.get("expiry")
            if hedge_exp and primary_exp and str(hedge_exp)[:10] != str(primary_exp)[:10]:
                rejection_reasons.append(
                    f"SPREAD_LEG_MISMATCH: Expiry mismatch between primary ({primary_exp}) and hedge ({hedge_exp})"
                )

            # 2. Leg Collision Check
            primary_sym = contract_data.get("symbol")
            hedge_sym = spread_contract.get("symbol")
            if primary_sym and hedge_sym and primary_sym == hedge_sym:
                rejection_reasons.append(
                    f"SPREAD_LEG_COLLISION: Primary and hedge legs resolve to identical symbol ({primary_sym})"
                )
            spread_k = float(spread_contract.get("strike") or 0.0)
            if spread_k == strike:
                rejection_reasons.append(
                    f"SPREAD_LEG_COLLISION: Primary and hedge legs resolve to identical strike ({strike})"
                )

            # 3. Strategy Structure Check
            hedge_type = (spread_contract.get("option_type") or "CE").upper()
            if strategy == STRATEGY_BULL_CALL_SPREAD:
                if hedge_type != "CE" or spread_k <= strike:
                    rejection_reasons.append(
                        f"INVALID_SPREAD_STRUCTURE: Bull Call Spread requires hedge leg to be higher strike Call (got {spread_k} {hedge_type} vs {strike} CE)"
                    )
            elif strategy == STRATEGY_BEAR_PUT_SPREAD:
                if hedge_type != "PE" or spread_k >= strike:
                    rejection_reasons.append(
                        f"INVALID_SPREAD_STRUCTURE: Bear Put Spread requires hedge leg to be lower strike Put (got {spread_k} {hedge_type} vs {strike} PE)"
                    )
            elif strategy == STRATEGY_BEAR_CALL_SPREAD:
                # Credit spread: primary (strike) is the SOLD call; hedge must be a higher-strike
                # long call for genuine protection (same strike ordering as Bull Call Spread, just
                # with the sold/bought roles reversed).
                if hedge_type != "CE" or spread_k <= strike:
                    rejection_reasons.append(
                        f"INVALID_SPREAD_STRUCTURE: Bear Call Credit Spread requires hedge leg to be higher strike Call (got {spread_k} {hedge_type} vs {strike} CE)"
                    )
            elif strategy == STRATEGY_BULL_PUT_SPREAD:
                # Credit spread: primary (strike) is the SOLD put; hedge must be a lower-strike
                # long put for genuine protection.
                if hedge_type != "PE" or spread_k >= strike:
                    rejection_reasons.append(
                        f"INVALID_SPREAD_STRUCTURE: Bull Put Credit Spread requires hedge leg to be lower strike Put (got {spread_k} {hedge_type} vs {strike} PE)"
                    )

            # 4. Hedge Live Quote Freshness
            hedge_raw_ts = spread_contract.get("quote_timestamp") or spread_contract.get("timestamp") or contract_data.get("quote_timestamp")
            if not hedge_raw_ts:
                rejection_reasons.append("MISSING_HEDGE_TIMESTAMP: Hedge leg quote timestamp is missing")
            else:
                hedge_dt = parse_timestamp(hedge_raw_ts)
                if not hedge_dt:
                    rejection_reasons.append(f"UNPARSABLE_HEDGE_TIMESTAMP: Could not parse hedge timestamp '{hedge_raw_ts}'")
                else:
                    h_age = (now_dt - hedge_dt).total_seconds()
                    if h_age > DEFAULT_MAX_QUOTE_AGE_SECONDS:
                        rejection_reasons.append(
                            f"STALE_HEDGE_QUOTE: Hedge quote age {h_age:.1f}s exceeds threshold {DEFAULT_MAX_QUOTE_AGE_SECONDS}s (timestamp: {hedge_raw_ts})"
                        )
                    if h_age < -5.0:
                        rejection_reasons.append(
                            f"FUTURE_HEDGE_TIMESTAMP: Hedge timestamp '{hedge_raw_ts}' is in the future ({h_age:.1f}s)"
                        )

            # 5. Hedge Quotes & Depth
            spread_bid = float(spread_contract.get("bid") or 0.0)
            spread_ask = float(spread_contract.get("ask") or 0.0)
            spread_ltp = float(spread_contract.get("last_price") or 0.0)

            if spread_bid <= 0.0:
                rejection_reasons.append("ZERO_HEDGE_QUOTE: Zero or missing bid quote in spread hedge leg")
            if spread_ask <= 0.0:
                rejection_reasons.append("ZERO_HEDGE_QUOTE: Zero or missing ask quote in spread hedge leg")
            if spread_bid > 0.0 and spread_ask > 0.0 and spread_ask < spread_bid:
                rejection_reasons.append(
                    f"INVERTED_HEDGE_QUOTE: Crossed market quotes in spread hedge leg: bid ({spread_bid}) > ask ({spread_ask})"
                )

            hedge_depth_val = spread_contract.get("bid_qty") if not is_seller else spread_contract.get("ask_qty")
            if hedge_depth_val is None or int(hedge_depth_val) <= 0:
                rejection_reasons.append("INSUFFICIENT_HEDGE_DEPTH: Zero or missing order-book depth on spread hedge leg")

            # Hedge leg execution price reconciliation
            if not is_seller:
                hedge_base = spread_bid if spread_bid > 0.0 else spread_ltp
                hedge_entry = max(0.05, round_tick(hedge_base - 0.05, 0.05))
            else:
                hedge_base = spread_ask if spread_ask > 0.0 else spread_ltp
                hedge_entry = round_tick(hedge_base + 0.05, 0.05)

            width = abs(spread_k - strike) if spread_k is not None else 0.0
            if strategy in (STRATEGY_BULL_CALL_SPREAD, STRATEGY_BEAR_PUT_SPREAD):
                # Debit spread
                net_debit = max(0.05, round_tick(reconciled_entry - hedge_entry, 0.05))
                risk_per_share = net_debit
                risk_per_lot = round(net_debit * lot_size, 2)
            else:
                # Credit spread
                net_credit = max(0.05, round_tick(reconciled_entry - hedge_entry, 0.05))
                risk_per_share = max(0.0, width - net_credit)
                risk_per_lot = round(risk_per_share * lot_size, 2)
        elif prem_levels["stop_loss_premium"] is None:
            # Premium model unavailable: there is no validated stop to size risk from.
            risk_per_lot = None
        else:
            prem_risk_per_share = abs(prem_levels["entry_premium"] - prem_levels["stop_loss_premium"])
            risk_per_lot = round(prem_risk_per_share * lot_size, 2)

        # Gate on T2 (the underlying's 2R target). T1 is the 1R level: a 1R underlying move can never pay
        # 2R net on the option (same delta both ways, minus charges), so a T1 gate refused every buy.
        rr_gate = target_rr_gate or underlying_setup.get("target_rr_gate") or self.config.get("target_rr_gate") or "T2"
        hedge_delta_val = float(spread_contract.get("delta")) if (spread_contract and spread_contract.get("delta") is not None) else None

        # Position Sizing Logic (4-Tier Position Sizing)
        charges_1_lot = self.calculate_charges_and_net_rr(
            strategy=strategy,
            entry_prem=prem_levels["entry_premium"],
            stop_prem=prem_levels["stop_loss_premium"],
            t1_prem=prem_levels["target_1_premium"],
            t2_prem=prem_levels.get("target_2_premium"),
            qty=lot_size,
            lot_size=lot_size,
            strike=strike,
            option_type=option_type,
            spread_strike=spread_k if is_spread else None,
            spread_entry_prem=hedge_entry if is_spread else None,
            target_rr_gate=rr_gate,
            primary_delta=delta,
            spread_delta=hedge_delta_val,
        )
        net_risk_1_lot = charges_1_lot["net_loss_stop"]

        # 1-Lot Margin & Capital Requirement
        spread_k = spread_contract.get("strike") if spread_contract else None
        spread_p = spread_contract.get("last_price") if spread_contract else None
        margin_info_1_lot = self.evaluate_margin_and_capital(
            strategy=strategy,
            strike=strike,
            lot_size=lot_size,
            entry_prem=prem_levels["entry_premium"],
            lots=1,
            spread_strike=spread_k,
            spread_prem=spread_p,
        )
        capital_req_1_lot = margin_info_1_lot["capital_required"]
        margin_req_1_lot = margin_info_1_lot["margin_required"]

        # Tier 1: Theoretical Lots (constrained by both risk budget and authoritative max daily loss)
        effective_budget = budget
        eff_daily_limit = self.config.get("max_daily_loss_inr") or self.config.get("max_daily_loss")
        if eff_daily_limit:
            effective_budget = min(effective_budget, float(eff_daily_limit))

        theoretical_lots = int(effective_budget // net_risk_1_lot) if (net_risk_1_lot is not None and net_risk_1_lot > 0) else 0

        # Tier 2: Risk Budget Permitted Lots
        unavailable_sizing = premium_model_unavailable or risk_per_lot is None or net_risk_1_lot is None
        spread_max_loss_1_lot = (
            None
            if unavailable_sizing
            # risk_per_share already holds the correct per-share max loss for either spread type
            # (net_debit for Bull Call/Bear Put, max(0, width - net_credit) for Bear Call/Bull Put
            # credit spreads) -- it was a bug to reference net_debit directly here, since that
            # name is only ever bound in the debit-spread branch above and raised
            # UnboundLocalError the first time a credit spread actually reached this line.
            else (round((risk_per_share * lot_size) + charges_1_lot.get("statutory_charges_stop", 0.0), 2) if is_spread else net_risk_1_lot)
        )
        if unavailable_sizing:
            # The premium model could not produce a validated stop/target, so no risk-per-lot or
            # net-loss-at-stop figure exists to size against. Fail closed: zero lots, and do not
            # misclassify this as a computed "zero or negative risk" or "RR below minimum" --
            # the OPTION_RISK_UNAVAILABLE / MODEL_UNAVAILABLE reasons already recorded are the
            # precise, non-fabricated cause.
            risk_budget_permitted_lots = 0
        elif risk_per_lot <= 0.0 or net_risk_1_lot <= 0.0:
            risk_budget_permitted_lots = 0
            rejection_reasons.append("ZERO_OR_NEGATIVE_RISK: Trade risk per lot is zero or negative")
        elif (spread_max_loss_1_lot if is_spread else net_risk_1_lot) > budget:
            risk_budget_permitted_lots = 0
            eff_loss_chk = spread_max_loss_1_lot if is_spread else net_risk_1_lot
            rejection_reasons.append(
                f"RISK_EXCEEDS_BUDGET: Net risk per lot after charges (Rs {eff_loss_chk:.2f}) exceeds configured max loss budget (Rs {budget:.2f})"
            )
        elif net_risk_1_lot > effective_budget:
            risk_budget_permitted_lots = 0
            rejection_reasons.append(
                f"DAILY_LOSS_LIMIT_EXCEEDED: Net risk per lot after charges (Rs {net_risk_1_lot:.2f}) exceeds configured max daily loss limit (Rs {effective_budget:.2f})"
            )
        else:
            risk_budget_permitted_lots = max(1, theoretical_lots)
            while risk_budget_permitted_lots > 1:
                chk = self.calculate_charges_and_net_rr(
                    strategy=strategy,
                    entry_prem=prem_levels["entry_premium"],
                    stop_prem=prem_levels["stop_loss_premium"],
                    t1_prem=prem_levels["target_1_premium"],
                    t2_prem=prem_levels.get("target_2_premium"),
                    qty=risk_budget_permitted_lots * lot_size,
                    lot_size=lot_size,
                    strike=strike,
                    option_type=option_type,
                    spread_strike=spread_k if is_spread else None,
                    spread_entry_prem=hedge_entry if is_spread else None,
                    target_rr_gate=rr_gate,
                    primary_delta=delta,
                    spread_delta=hedge_delta_val,
                )
                if chk["net_loss_stop"] <= effective_budget:
                    break
                risk_budget_permitted_lots -= 1

        # Tier 3: Capital Permitted Lots (unconstrained division of available capital by margin/capital per lot)
        if capital_req_1_lot > 0:
            capital_permitted_lots = int(capital_limit // capital_req_1_lot)
        else:
            capital_permitted_lots = 0

        if capital_permitted_lots == 0:
            rejection_reasons.append(
                f"INSUFFICIENT_CAPITAL: Capital/margin required per lot (Rs {capital_req_1_lot:,.2f}) exceeds available capital (Rs {capital_limit:,.2f})"
            )

        # Available Order Book Depth Check
        # For BUY orders: capacity is constrained by ask_qty (sellers in book)
        # For SELL orders: capacity is constrained by bid_qty (buyers in book)
        if not is_seller:
            primary_depth = contract_data.get("ask_qty")
        else:
            primary_depth = contract_data.get("bid_qty")

        hedge_depth = None
        if is_spread and spread_contract:
            # In spreads, the hedge leg is opposite side of primary leg
            if not is_seller:
                hedge_depth = spread_contract.get("bid_qty")
            else:
                hedge_depth = spread_contract.get("ask_qty")

        avail_depth = None
        if primary_depth is not None and hedge_depth is not None:
            avail_depth = min(int(primary_depth), int(hedge_depth))
        elif primary_depth is not None:
            avail_depth = int(primary_depth)
        elif hedge_depth is not None:
            avail_depth = int(hedge_depth)

        depth_permitted_lots = None
        if avail_depth is not None:
            if avail_depth <= 0:
                depth_permitted_lots = 0
                rejection_reasons.append(
                    f"INSUFFICIENT_DEPTH: Order book depth ({avail_depth} shares) is zero or negative"
                )
            elif avail_depth < lot_size:
                depth_permitted_lots = 0
                rejection_reasons.append(
                    f"INSUFFICIENT_DEPTH: Available order book depth ({avail_depth} shares) is less than minimum 1 lot ({lot_size} shares)"
                )
            else:
                depth_permitted_lots = int(avail_depth // lot_size)

        # Tier 4: Final Permitted Lots
        # Sizing is constrained by the most restrictive tier: Risk Budget, Capital Limit, Order
        # Book Depth, and the configured max_option_lots policy cap.
        max_option_lots_cfg = self.config.get("max_option_lots")
        limits = [risk_budget_permitted_lots, capital_permitted_lots]
        if depth_permitted_lots is not None:
            limits.append(depth_permitted_lots)
        if max_option_lots_cfg is not None:
            try:
                limits.append(int(max_option_lots_cfg))
            except (TypeError, ValueError):
                pass
        final_permitted_lots = min(limits)
        lots = final_permitted_lots
        qty = lots * lot_size if lots > 0 else lot_size  # reference size for quote/charge calculations

        # Charges & Net R:R (computed on active quantity if lots > 0, else 1 lot reference)
        charges_info = self.calculate_charges_and_net_rr(
            strategy=strategy,
            entry_prem=prem_levels["entry_premium"],
            stop_prem=prem_levels["stop_loss_premium"],
            t1_prem=prem_levels["target_1_premium"],
            t2_prem=prem_levels.get("target_2_premium"),
            qty=qty,
            lot_size=lot_size,
            strike=strike,
            option_type=option_type,
            spread_strike=spread_k if is_spread else None,
            spread_entry_prem=hedge_entry if is_spread else None,
            target_rr_gate=rr_gate,
            primary_delta=delta,
            spread_delta=hedge_delta_val,
        )

        net_rr = charges_info["net_reward_risk"]
        if net_rr is not None and net_rr < min_rr:
            rejection_reasons.append(
                f"RR_BELOW_MINIMUM: Net R:R ({net_rr}:1) below configured minimum ({min_rr}:1)"
            )
        # net_rr is None only when the premium model was unavailable (premium_model_unavailable);
        # that case is already recorded via OPTION_RISK_UNAVAILABLE / MODEL_UNAVAILABLE above and
        # must never be re-classified as a computed RR_BELOW_MINIMUM result.

        # Margin & Capital Check evaluated for final lots (or 1 lot if 0 for reference)
        margin_info = self.evaluate_margin_and_capital(
            strategy=strategy,
            strike=strike,
            lot_size=lot_size,
            entry_prem=prem_levels["entry_premium"],
            lots=max(1, lots),
            spread_strike=spread_k,
            spread_prem=spread_p,
        )

        if lots > 0 and margin_info["capital_required"] > capital_limit:
            rejection_reasons.append(
                f"INSUFFICIENT_CAPITAL: Capital/margin required (Rs {margin_info['capital_required']:,.2f}) exceeds available capital (Rs {capital_limit:,.2f})"
            )

        if strategy in (STRATEGY_SELL_CE, STRATEGY_SELL_PE):
            # Structural, unconditional prohibition: naked (unhedged) single-leg option selling
            # is never approvable, mirroring trader-risk/check.py's explicit rule ("options
            # selling / naked short options are prohibited (undefined risk)"). This must NOT
            # depend on margin_info_1_lot["is_authoritative"] -- that check below is a separate,
            # additional reason and is kept for its own diagnostic value, but if it were ever
            # satisfied (e.g. a future authoritative SPAN margin integration), this prohibition
            # must still stand on its own so naked selling can never become approvable by accident.
            rejection_reasons.append(
                "NAKED_OPTION_PROHIBITED: Undefined/severe-tail-risk single-leg option selling "
                "is structurally prohibited for decision-support signals; only defined-risk "
                "hedged spread structures may ever be marked actionable"
            )
            if not margin_info_1_lot.get("is_authoritative"):
                rejection_reasons.append(
                    "AUTHORITATIVE_MARGIN_UNAVAILABLE: Exchange SPAN + Exposure margin data unavailable; "
                    "heuristic estimate cannot authorize live execution"
                )

        # Phase 3: Require authoritative, current instrument metadata
        if meta and meta.get("is_authoritative") is False:
            rejection_reasons.append(
                "AUTHORITATIVE_METADATA_UNAVAILABLE: Instrument metadata and lot size not authoritative from exchange BOD cache; live execution rejected"
            )

        # DEF-01: Genuine Live Data Freshness & Provenance Gating
        now_dt = now_utc if now_utc is not None else _utc_now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)

        # Expiry-day circuit breaker: mirrors trader-risk/check.py's rule for the order-execution
        # pipeline ("new derivative entries on expiry day blocked after 14:30 IST to avoid physical
        # delivery / gamma explosion"). trader-options had no equivalent -- it could recommend a
        # 0-DTE contract right up to the close, exactly when pin/gamma risk is worst.
        try:
            now_ist = now_dt.astimezone(calendar_in.IST)
            expiry_date = datetime.date.fromisoformat(str(expiry)[:10])
            if now_ist.date() == expiry_date and now_ist.time() >= datetime.time(14, 30):
                rejection_reasons.append(
                    "EXPIRY_DAY_CUTOFF: New entries on contract expiry day (%s) are blocked after "
                    "14:30 IST to avoid pin risk, gamma explosion, and illiquid expiry-day spreads"
                    % expiry_date.isoformat()
                )
        except (ValueError, TypeError):
            pass

        is_live_provenance, freshness_rejections, freshness_meta = self.validate_live_data_freshness(
            contract_data=contract_data,
            underlying_setup=underlying_setup,
            now_utc=now_dt,
        )

        # Phase 4: Central portfolio risk check
        p_path = portfolio_path or self.portfolio_path or os.path.join(BASE_DIR, "trader", "portfolio.json")
        b_path = broker_state_path or self.broker_state_path or os.path.join(BASE_DIR, "trader", "state", "broker.json")
        port_ok, port_rejections = self.check_portfolio_risk(
            # Unavailable premium model -> no stop-based risk figure exists; this candidate is
            # already fail-closed via OPTION_RISK_UNAVAILABLE/MODEL_UNAVAILABLE above regardless
            # of the portfolio check outcome, so contribute 0 (not a fabricated real number) to
            # the aggregate daily/weekly exposure counter.
            candidate_risk=charges_info.get("net_loss_stop") or 0.0,
            capital_required=margin_info.get("capital_required", 0.0),
            portfolio_path=p_path,
            broker_state_path=b_path,
            now_utc=now_dt,
            is_live_check=bool(freshness_meta.get("is_market_open", False)),
            candidate_risk_budget=budget,
            allow_unverified_portfolio=allow_unverified_portfolio,
        )
        if freshness_meta.get("is_market_open", False):
            if not port_ok:
                rejection_reasons.extend(port_rejections)

        # Preserve original raw timestamps for auditability without silent replacement
        raw_quote_ts = contract_data.get("quote_timestamp") or contract_data.get("timestamp")
        raw_retrieval_ts = contract_data.get("retrieval_timestamp") or contract_data.get("fetched_at")
        raw_candle_ts = freshness_meta.get("candle_timestamp_raw") or underlying_setup.get("candle_timestamp") or underlying_setup.get("last_data_timestamp")
        tier = contract_data.get("tier") or underlying_setup.get("data_tier") or "HISTORICAL"
        data_status = contract_data.get("data_status") or underlying_setup.get("data_status") or "HISTORICAL"
        is_live = bool(contract_data.get("is_live", False))

        is_risk_valid = len(rejection_reasons) == 0

        # Signal State Assignment
        if not is_risk_valid:
            signal_state = STATE_SIGNAL_REJECTED
            rejection_reasons.extend(freshness_rejections)
        elif not is_live_provenance:
            # Stale, off-hours, unparsable, or historical data cannot be validated for live execution
            signal_state = STATE_HISTORICAL_ANALYSIS
            rejection_reasons.extend(freshness_rejections)
        else:
            signal_state = STATE_SIGNAL_VALIDATED

        is_actionable = bool(is_risk_valid and is_live_provenance)
        is_approved = bool(signal_state == STATE_SIGNAL_VALIDATED and is_actionable)

        # Quote verifiability & executable status
        has_verifiable_quotes = bool(
            raw_quote_ts is not None and raw_quote_ts != "" and
            freshness_meta.get("is_market_open", False) and
            is_live_provenance
        )
        quote_classification = (
            "REAL_TIME_VERIFIED"
            if has_verifiable_quotes
            else "HISTORICAL_INDICATIVE"
        )
        execution_recon["is_executable"] = has_verifiable_quotes
        execution_recon["quote_classification"] = quote_classification
        execution_recon["quote_timestamp"] = raw_quote_ts
        execution_recon["retrieval_timestamp"] = raw_retrieval_ts
        execution_recon["executable_note"] = (
            "Verified live real-time market quote."
            if has_verifiable_quotes
            else "HISTORICAL INDICATIVE: Quote lacks verifiable live exchange quote timestamp or market session is closed. NOT executable."
        )

        # Construct spread legs structure if applicable (Phase 5 Fix)
        primary_option_type = (
            contract_data.get("option_type")
            or ("CE" if "CALL" in strategy or strategy in (STRATEGY_BUY_CE, STRATEGY_SELL_CE) else "PE")
        )
        legs_data = [
            {
                "leg_index": 1,
                "role": "PRIMARY",
                "side": "SELL" if is_seller else "BUY",
                "symbol": contract_sym,
                "strike": strike,
                "option_type": primary_option_type,
                "expiry": expiry,
                "entry_price": prem_levels["entry_premium"],
                "bid": bid,
                "ask": ask,
                "quantity": qty,
                "lot_size": lot_size,
                "delta": delta,
                "iv": iv,
                "quote_provenance": contract_data.get("quote_provenance", "UPSTOX_REST_SNAPSHOT"),
            }
        ]
        if spread_contract:
            legs_data.append({
                "leg_index": 2,
                "role": "HEDGE",
                "side": "BUY" if is_seller else "SELL",
                "symbol": spread_contract.get("symbol", f"{underlying}_{spread_k}_{spread_contract.get('option_type', option_type)}"),
                "strike": spread_k,
                "option_type": spread_contract.get("option_type", option_type),
                "expiry": spread_contract.get("expiry") or expiry,
                "entry_price": hedge_entry,
                "bid": spread_bid,
                "ask": spread_ask,
                "quantity": qty,
                "lot_size": lot_size,
                "delta": spread_contract.get("delta"),
                "iv": spread_contract.get("iv"),
                "quote_provenance": spread_contract.get("quote_provenance", contract_data.get("quote_provenance", "UPSTOX_REST_SNAPSHOT")),
            })

        # 3. Auditable Data Provenance
        data_prov = {
            "data_provider": str(contract_data.get("provider") or "upstox"),
            "quote_provenance": contract_data.get("quote_provenance", "UPSTOX_REST_SNAPSHOT"),
            "exchange_quote_timestamp": raw_quote_ts,
            "underlying_candle_timestamp": raw_candle_ts,
            "retrieval_timestamp": raw_retrieval_ts,
            "quote_age_seconds": freshness_meta.get("quote_age_seconds"),
            "candle_age_seconds": freshness_meta.get("candle_age_seconds"),
            "retrieval_age_seconds": freshness_meta.get("retrieval_age_seconds"),
            "data_tier": tier,
            "market_session_status": "OPEN" if freshness_meta.get("is_market_open") else "CLOSED",
            "freshness_validation_result": "PASSED" if is_live_provenance else "FAILED",
            "freshness_errors": freshness_rejections,
        }

        # 4. Underlying Setup Evidence
        strike_intv = meta.get("strike_interval") or 50.0
        atm_k = meta.get("atm_strike") or (round(spot_entry / strike_intv) * strike_intv)

        u_trig_dict = underlying_setup.get("trigger") or {}
        trig_price = float(
            u_trig_dict.get("price")
            or underlying_setup.get("trigger_price")
            or spot_entry
        )
        trig_time = str(u_trig_dict.get("time") or u_trig_dict.get("close_time") or "Current Bar")
        trig_reason = str(underlying_setup.get("reason") or u_trig_dict.get("reason") or "Underlying technical trigger")

        comp_candle = (underlying_setup.get("candle_context") or {}).get("completed_candle") or {}
        latest_candle_time = str(comp_candle.get("bar_end") or raw_candle_ts or "15:30")
        latest_candle_close = float(
            underlying_setup.get("current_spot")
            or underlying_setup.get("current_price")
            or u_dec.get("current_price")
            or u_last.get("close")
            or underlying_setup.get("spot_price")
            or spot_entry
        )

        # Distinguish historical trigger activation from current setup validity
        candle_date = (
            underlying_setup.get("candle_session")
            or underlying_setup.get("date")
            or (comp_candle.get("bar_end", "").split("T")[0] if "T" in str(comp_candle.get("bar_end", "")) else None)
        )
        now_ist_date = now_dt.astimezone(calendar_in.IST).strftime("%Y-%m-%d")

        is_stale_session = bool(candle_date and candle_date != now_ist_date)
        is_breached = bool(
            (latest_candle_close <= inv_level if is_bullish else latest_candle_close >= inv_level)
            if (latest_candle_close > 0 and inv_level > 0) else False
        ) or bool(
            (latest_candle_close <= spot_stop if is_bullish else latest_candle_close >= spot_stop)
            if (latest_candle_close > 0 and spot_stop > 0) else False
        )

        if is_breached or underlying_setup.get("is_invalidated") or u_dec.get("is_invalidated"):
            setup_validity = "INVALIDATED"
        elif is_stale_session or not freshness_meta.get("is_market_open", False):
            setup_validity = "HISTORICAL_TRIGGER_INACTIVE"
        else:
            setup_validity = "CURRENT_SESSION_ACTIVE"

        regime_context = _build_regime_context(underlying_setup.get("setup"), broad_regime)

        setup_evidence = {
            "setup_name": underlying_setup.get("setup") or "intraday_level",
            "directional_bias": direction,
            "trigger_price": trig_price,
            "trigger_time": trig_time,
            "trigger_reason": trig_reason,
            "candle_close": latest_candle_close,
            "current_spot": current_spot,
            "underlying_spot_details": underlying_spot_details,
            "latest_candle_time": latest_candle_time,
            "setup_validity_state": setup_validity,
            "entry_trigger": {
                "price": trig_price,
                "candle_close": latest_candle_close,
                "current_spot": current_spot,
                "time": trig_time,
                "latest_candle_time": latest_candle_time,
                "reason": trig_reason,
            },
            "invalidation_level": inv_level,
            "support_resistance_level": (
                underlying_setup.get("level")
                or (underlying_setup.get("opening_range") or {}).get("high" if "BULL" in direction else "low")
                or spot_stop
            ),
            "scanner_result": underlying_setup.get("status") or (underlying_setup.get("decision") or {}).get("state") or "TRIGGERED",
            "setup_timestamp": (
                raw_candle_ts
                or underlying_setup.get("candle_timestamp")
                or underlying_setup.get("last_data_timestamp")
                or trig_time
            ),
            "strike_selection_rationale": (
                f"Selected {strike:.1f} ({'ATM' if math.isclose(strike, atm_k, abs_tol=10.0) else 'OTM'}) {option_type} "
                f"with Delta {delta:+.2f} and IV {iv:.1f}% for optimal directional gearing in expiry {expiry}."
            ),
        }

        net_rr_display = f"{net_rr}:1" if net_rr is not None else "N/A (MODEL_UNAVAILABLE)"
        rationale = (
            f"Evaluated {strategy} on {underlying} ({strike} {option_type}, Delta: {delta:+.2f}, IV: {iv:.1f}%). "
            f"Net R:R: {net_rr_display}. "
            + (f"Approved ({signal_state})." if is_approved else f"REJECTED: {'; '.join(rejection_reasons)}.")
            + f" {regime_context['regime_note']}"
        )

        contract = {
            "underlying_instrument": underlying,
            "direction": direction,
            "strategy": strategy,
            "option_type": option_type,
            "contract": contract_sym,
            "expiry": expiry,
            "strike": strike,
            "current_premium": prem_levels["entry_premium"],
            "quote_timestamp": raw_quote_ts,
            "retrieval_timestamp": raw_retrieval_ts,
            "bid": bid,
            "ask": ask,
            "execution_reconciliation": execution_recon,
            "underlying_setup_evidence": setup_evidence,
            "data_provenance": data_prov,
            "underlying_trigger": {
                "price": trig_price,
                "current_spot": latest_candle_close,
                "setup": underlying_setup.get("setup"),
                "interval": underlying_setup.get("interval", "5m"),
                "time": trig_time,
                "latest_candle_time": latest_candle_time,
                "invalidation_level": spot_stop,
                "setup_validity_state": setup_validity,
            },
            "option_entry_range": {
                # Band width: never tighter than 2%, but widened to the option's own observed
                # bid/ask spread if that's wider -- a flat 2% band on an illiquid, wide-spread
                # option would look "violated" by completely normal bid/ask noise, which isn't
                # the signal this range is meant to catch (real price movement since generation).
                "min": round_tick(prem_levels["entry_premium"] * (1.0 - max(2.0, spread_pct) / 100.0), 0.05),
                "max": round_tick(prem_levels["entry_premium"] * (1.0 + max(2.0, spread_pct) / 100.0), 0.05),
                "ideal": prem_levels["entry_premium"],
                "band_pct": round(max(2.0, spread_pct), 2),
                "instruction": (
                    "If the live premium is outside this range when you're ready to act, skip "
                    "this trade -- the setup has moved and this recommendation no longer applies."
                ),
            },
            "stop_loss": prem_levels["stop_loss_premium"],
            "target_1": prem_levels["target_1_premium"],
            "target_2": prem_levels["target_2_premium"],
            "expected_risk_reward_after_charges": net_rr,
            "charges_summary": charges_info,
            "maximum_loss": margin_info["max_loss"],
            "capital_margin_requirement": margin_info["capital_required"],
            "margin_details": margin_info,
            "position_sizing": {
                "sizing_state": "MODEL_UNAVAILABLE" if unavailable_sizing else "COMPUTED",
                "lots": lots,
                "lot_size": lot_size,
                "total_quantity": lots * lot_size,
                "theoretical_lots": theoretical_lots,
                "risk_budget_permitted_lots": risk_budget_permitted_lots,
                "capital_permitted_lots": capital_permitted_lots,
                "depth_permitted_lots": depth_permitted_lots if depth_permitted_lots is not None else "UNCONSTRAINED",
                "available_depth_shares": avail_depth,
                "final_permitted_lots": final_permitted_lots,
                "capital_required_per_lot": capital_req_1_lot,
                "margin_required_per_lot": margin_req_1_lot,
                "risk_per_lot_inr": risk_per_lot,
                "net_risk_per_lot_inr": net_risk_1_lot,
                "budget_max_loss_inr": budget,
                "actual_net_risk_inr": charges_info["net_loss_stop"] if lots > 0 else 0.0,
                "stop_loss_risk_per_lot_inr": net_risk_1_lot,
                "total_stop_loss_risk_inr": charges_info["net_loss_stop"] if lots > 0 else 0.0,
                "maximum_possible_loss": margin_info["max_loss"],
                "maximum_possible_loss_value": margin_info.get("max_loss_value"),
                "maximum_theoretical_capital_loss_inr": margin_info.get("max_loss_value"),
                "stop_loss_risk_inr": charges_info["net_loss_stop"] if lots > 0 else 0.0,
                "stop_execution_guaranteed": False,
                "stop_execution_disclosure": (
                    "DISCLOSURE: Stop-loss order execution is NOT guaranteed. Orders are subject "
                    "to market liquidity, opening price gaps, and execution slippage. In an adverse gap "
                    "opening or fast market event, actual loss may exceed the stop risk and approach the "
                    "maximum theoretical capital loss."
                ),
                "risk_budget_policy_explanation": (
                    f"Position sized for stop-loss risk (Rs {charges_info['net_loss_stop'] if lots > 0 else 0.0:,.2f} fits within Rs {budget:,.0f} budget). "
                    f"Note: Maximum possible loss ({margin_info['max_loss']}) is substantially higher in adverse gap/worthless expiry scenarios. "
                    "Stop execution is NOT guaranteed; subject to gap risk and market liquidity."
                ),
            },
            "legs": legs_data,
            "confirmation_conditions": (
                f"Requires completed candle confirmation on {underlying_setup.get('interval', '5m')} bar "
                f"with minimum volume participation and option bid/ask spread <= {DEFAULT_SPREAD_TOLERANCE_PCT}%."
            ),
            "invalidation_conditions": (
                f"Underlying crosses invalidation level {spot_stop} or option premium drops below "
                + (
                    f"{prem_levels['stop_loss_premium']}."
                    if prem_levels["stop_loss_premium"] is not None
                    else "N/A (premium stop/target model unavailable -- see rejection_reasons)."
                )
            ),
            "data_provider": contract_data.get("provider", "upstox"),
            "data_quality": {
                "tier": tier,
                "data_status": data_status,
                "is_live": is_live,
                "is_simulated": contract_data.get("is_simulated", False),
            },
            "freshness_details": freshness_meta,
            "signal_expiry": "15:15 IST (End of Intraday Session)",
            "selection_rationale": rationale,
            "regime_context": regime_context,
            "signal_state": signal_state,
            "is_actionable": is_actionable,
            "rejection_reasons": rejection_reasons,
        }

        return contract

    def is_valid_session_setup(self, setup_res: Optional[Dict[str, Any]], now_utc: datetime.datetime) -> bool:
        """
        DEF-04: Reject stale session triggers.
        - decision_state=NO TRADE cannot be overridden by status=triggered.
        - Previous-session triggers cannot become active current-session setups.
        """
        if not setup_res:
            return False

        dec_state = setup_res.get("decision_state")
        sub_state = setup_res.get("sub_state")
        status = setup_res.get("status")

        # Non-index equity with decision_state=NO TRADE is an absolute refusal
        if dec_state == "NO TRADE":
            if sub_state != "ANALYTICAL_ONLY_INDEX":
                return False
            # For spot index, must have active triggered/waiting status
            if status not in ("triggered", "waiting"):
                return False
        elif dec_state not in ("ENTRY CONFIRMED", "ENTRY CANDIDATE", "SETUP"):
            return False

        # Current-session check: Trigger / candle must belong to current session if market is open
        now_dt = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=datetime.timezone.utc)
        now_ist_dt = now_dt.astimezone(calendar_in.IST)
        current_session_date = now_ist_dt.strftime("%Y-%m-%d")

        candle_ctx = setup_res.get("candle_context") or {}
        comp_candle = candle_ctx.get("completed_candle") or {}
        raw_date = (
            setup_res.get("candle_session")
            or setup_res.get("date")
            or (comp_candle.get("bar_end", "").split("T")[0] if "T" in str(comp_candle.get("bar_end", "")) else None)
        )

        # Fail-closed if setup relies on VWAP but VWAP is unavailable
        s_name = setup_res.get("setup") or ""
        if s_name in ("vwap_reclaim", "vwap_rejection"):
            if setup_res.get("indicators", {}).get("vwap_unavailable"):
                return False
            if setup_res.get("reason") and "session VWAP unavailable" in str(setup_res.get("reason")):
                return False

        is_market_open = calendar_in.is_market_hours_in(now_dt, exchange="NSE")
        if is_market_open:
            if setup_res.get("is_invalidated") or (setup_res.get("decision") or {}).get("is_invalidated"):
                return False
            if setup_res.get("anti_chasing", {}).get("chase_blocked") or (setup_res.get("decision") or {}).get("chase_blocked"):
                return False
            if sub_state in ("INVALIDATED", "EXTENDED", "EXPIRED", "STALE_DATA", "MARKET_CLOSED", "NO_DATA"):
                return False
            if raw_date and raw_date != current_session_date:
                return False  # Previous session trigger rejected in open market

        return True

    def resolve_near_month_future(
        self,
        symbol: str,
        exchange: Optional[str] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve the near-month futures contract for an index or F&O underlying.
        Inspects provider derivatives master / instruments first, then local cache.
        Returns dict with instrument_key, symbol, expiry, lot_size, or None if unresolvable.
        """
        canon = symbol.upper().strip()
        ref_dt = now or _utc_now()
        today_str = ref_dt.astimezone(calendar_in.IST).strftime("%Y-%m-%d")
        exch = exchange or ("BSE" if canon in ("SENSEX", "BANKEX") else "NSE")

        # 1. Try active market data provider or direct UpstoxMarketDataProvider (public CDN)
        # 1. Try active market data provider or Upstox public market data
        try:
            prov = None
            if hasattr(self, "provider") and self.provider:
                prov = self.provider
            else:
                try:
                    prov = data_in.get_provider(name="upstox")
                except Exception:
                    prov = data_in.UpstoxMarketDataProvider(analytics_token="public_read")
            if prov and hasattr(prov, "get_instruments"):
                inst_doc = prov.get_instruments(exchange=exch)
                recs = inst_doc.get("instruments") or []
                futs = []
                for r in recs:
                    if not isinstance(r, dict):
                        continue
                    itype = str(r.get("instrument_type") or "").upper()
                    if itype not in ("FUT", "FUTIDX", "FUTSTK"):
                        continue
                    u_sym = str(r.get("underlying_symbol") or r.get("name") or "").upper().strip()
                    t_sym = str(r.get("trading_symbol") or r.get("symbol") or "").upper().strip()
                    if u_sym == canon or t_sym.startswith(canon):
                        exp = r.get("expiry")
                        exp_str = None
                        if isinstance(exp, (int, float)) and exp > 1e11:
                            exp_dt = datetime.datetime.fromtimestamp(exp / 1000.0, tz=datetime.timezone.utc)
                            exp_str = exp_dt.astimezone(calendar_in.IST).strftime("%Y-%m-%d")
                        elif exp:
                            exp_str = str(exp)[:10]
                        if exp_str and exp_str >= today_str:
                            futs.append({
                                "instrument_key": r.get("instrument_key"),
                                "symbol": t_sym,
                                "expiry": exp_str,
                                "lot_size": r.get("lot_size"),
                                "raw_expiry": exp,
                            })
                if futs:
                    futs.sort(key=lambda x: x["expiry"])
                    return futs[0]
        except Exception:
            pass

        # 2. Try instruments.resolve_underlying_derivatives_meta
        try:
            meta = instruments.resolve_underlying_derivatives_meta(canon, exchange=exch)
            if meta and meta.get("expiry"):
                exp_date = str(meta.get("expiry"))[:10]
                if exp_date >= today_str:
                    fut_key = meta.get("future_instrument_key") or meta.get("futures_key")
                    if not fut_key:
                        try:
                            c = instruments.load_instrument_cache(max_age_days=30)
                            by_exch = c.get("instruments_by_exchange", {}).get(exch, {})
                            for k, r in by_exch.items():
                                if str(r.get("instrument_type") or "").upper() in ("FUT", "FUTIDX") and str(r.get("underlying") or "").upper() == canon:
                                    if str(r.get("expiry"))[:10] == exp_date:
                                        fut_key = r.get("instrument_key") or k
                                        break
                        except Exception:
                            fut_key = None
                    if fut_key:
                        return {
                            "instrument_key": fut_key,
                            "symbol": f"{canon}_{exp_date}_FUT",
                            "expiry": exp_date,
                            "lot_size": meta.get("lot_size"),
                        }
        except Exception:
            pass

        return None

    def get_futures_vwap(
        self,
        symbol: str,
        future_meta: Dict[str, Any],
        now: Optional[datetime.datetime] = None,
    ) -> Tuple[Optional[float], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Retrieve intraday bars for the near-month futures contract and compute volume-based VWAP.
        Returns (futures_vwap, vwap_info_dict, futures_chart_dict) or (None, None, None).
        Never fabricates volume or VWAP if bars or volume are missing.
        """
        fut_key = future_meta.get("instrument_key")
        if not fut_key:
            return None, None, None

        try:
            prov = None
            if hasattr(self, "provider") and self.provider:
                prov = self.provider
            else:
                try:
                    prov = data_in.get_provider(name="upstox")
                except Exception:
                    prov = data_in.UpstoxMarketDataProvider(analytics_token="public_read")
            bars_doc = prov.get_intraday_bars(fut_key, interval="5m", now=now)
            bars = (bars_doc or {}).get("bars") or []
            if not bars:
                return None, None, None

            # Verify genuine non-zero volume across bars
            total_vol = sum(float(b.get("v") or 0) for b in bars)
            if total_vol <= 0:
                return None, None, None

            import visuals
            vwap_series = visuals.compute_vwap(bars) if visuals is not None else entry._vwap_series(bars)
            if not vwap_series or vwap_series[-1] is None:
                return None, None, None

            fut_vwap = round(float(vwap_series[-1]), 2)
            last_fut_bar = bars[-1]
            fut_close = float(last_fut_bar["c"])

            info = {
                "future_instrument_key": fut_key,
                "future_symbol": future_meta.get("symbol"),
                "future_expiry": future_meta.get("expiry"),
                "futures_vwap": fut_vwap,
                "futures_last_price": fut_close,
                "futures_total_volume": int(total_vol),
                "source": "NEAR_MONTH_FUTURES_VOLUME",
            }
            fut_chart = {
                "symbol": future_meta.get("symbol") or f"{symbol}_FUT",
                "bars": bars,
                "meta": {"instrument_key": fut_key, "underlying": symbol},
                "data_tier": bars_doc.get("data_tier", "REAL_TIME"),
                "is_live": bars_doc.get("is_live", True),
            }
            return fut_vwap, info, fut_chart
        except Exception:
            return None, None, None

    def analyze_symbol_for_options(
        self,
        symbol: str,
        underlying_chart: Optional[Dict[str, Any]] = None,
        underlying_setup: Optional[Dict[str, Any]] = None,
        strategy_preference: Optional[str] = None,
        now_utc: Optional[datetime.datetime] = None,
        max_loss_budget: Optional[float] = None,
        min_reward_risk: Optional[float] = None,
        portfolio_path: Optional[str] = None,
        broker_state_path: Optional[str] = None,
        broad_regime: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Comprehensive dual-directional options analysis for a given symbol.

        Phase 2 Fix: Removed synthetic setup fabrication.
        If underlying_chart is None, fetches the real chart using entry.get_chart().
        If no real chart or no technical setup exists, returns NO TRADE.

        broad_regime: precomputed canonical market regime (scan.get_broad_market_regime()),
        passed in by callers (e.g. orchestrator.py) scanning multiple symbols so the 5-index
        regime fetch happens once per sweep, not once per symbol. If not supplied, this method
        computes it itself (single-symbol CLI usage) and degrades to "UNCERTAIN" on any failure
        -- regime is disclosed context, never a reason by itself to fail this call.
        """
        now = now_utc or _utc_now()
        budget = max_loss_budget if max_loss_budget is not None else self.max_loss_budget
        min_rr = min_reward_risk if min_reward_risk is not None else self.min_reward_risk
        p_path = portfolio_path or self.portfolio_path
        b_path = broker_state_path or self.broker_state_path

        if broad_regime is None:
            try:
                broad_regime = scan.get_broad_market_regime(market="IN", now=now).get("canonical_regime")
            except Exception:
                broad_regime = None
        broad_regime = broad_regime or "UNCERTAIN"

        meta_valid, meta, meta_err = self.validate_metadata(symbol)
        if not meta_valid:
            return {
                "symbol": symbol,
                "status": "NO_TRADE",
                "reason": f"INVALID_CONTRACT_METADATA: {meta_err}",
                "signals": [],
            }

        # 1. Obtain Real Underlying Chart (Phase 2 Fix: No synthetic fabrication)
        chart = underlying_chart
        if chart is None:
            try:
                chart = entry.get_chart(symbol, interval="5m", market="IN")
            except Exception as e:
                return {
                    "symbol": symbol,
                    "status": "NO_TRADE",
                    "reason": f"MISSING_UNDERLYING_CHART: Failed to load real OHLCV chart data ({e})",
                    "signals": [],
                }

        if not chart or not chart.get("bars") or len(chart["bars"]) < 5:
            return {
                "symbol": symbol,
                "status": "NO_TRADE",
                "reason": "MISSING_UNDERLYING_CHART: Insufficient real bars in chart",
                "signals": [],
            }

        # 2. Fetch Option Chain
        chain = self.fetch_normalized_chain(symbol, expiry=meta.get("expiry"), now=now)
        if not chain or not chain.strikes_data:
            return {
                "symbol": symbol,
                "status": "NO_TRADE",
                "reason": "STALE_OR_MISSING_DATA: Option chain unavailable from authorized provider",
                "signals": [],
            }

        spot_price = chain.underlying_price
        atm_strike = chain.atm_strike
        if atm_strike is None or spot_price <= 0:
            return {
                "symbol": symbol,
                "status": "NO_TRADE",
                "reason": "INVALID_PRICE_DATA: Missing spot price or ATM strike in chain",
                "signals": [],
            }

        # Resolve underlying key for websocket subscription and spot verification
        u_key = getattr(chain, "underlying_key", None)
        if not u_key and symbol:
            try:
                meta_info = instruments.INDEX_INSTRUMENTS.get(symbol.upper(), {})
                u_key = meta_info.get("upstox_key")
            except Exception:
                u_key = None

        # 2b. Integrate verified live WebSocket quotes if streamer is active
        ws_live_summary = None
        if self.streamer is not None:
            if getattr(self.streamer, "is_connected", False):
                try:
                    opt_keys = []
                    for s_k, legs in (chain.strikes_data or {}).items():
                        if isinstance(legs, dict):
                            for l_side in ("ce", "pe"):
                                c_info = legs.get(l_side)
                                if isinstance(c_info, dict) and c_info.get("instrument_key"):
                                    opt_keys.append(c_info["instrument_key"])
                    all_sub_keys = list(opt_keys)
                    if u_key and u_key not in all_sub_keys:
                        all_sub_keys.append(u_key)
                    if all_sub_keys:
                        unsub = [k for k in all_sub_keys if k not in self.streamer.active_keys()]
                        if unsub:
                            self.streamer.subscribe(unsub, mode="full")
                        if hasattr(self.streamer, "wait_for_quotes"):
                            self.streamer.wait_for_quotes(all_sub_keys, timeout=2.0, now_dt=now)
                        ws_live_summary = self.streamer.apply_live_quotes(chain, max_age_seconds=30.0, now_dt=now)
                except Exception as ws_err:
                    ws_live_summary = {"error": str(ws_err)}

        # Session status determination
        is_market_open = calendar_in.is_market_hours_in(now, exchange="NSE")
        chart_is_live = bool(chart.get("is_live", False)) and (str(chart.get("data_tier", "")).upper() == "REAL_TIME")
        session_status = "MARKET_OPEN (LIVE SESSION)" if (is_market_open and chart_is_live) else "MARKET_CLOSED (POST-MARKET REVIEW)"

        # 3. Evaluate Real Technical Setups on Chart Bars with Index Futures VWAP Support
        is_index_sym = symbol.upper() in instruments.INDEX_INSTRUMENTS or all(
            float(b.get("v") or 0) <= 0 for b in chart.get("bars", [])
        )
        fut_meta = None
        fut_vwap = None
        fut_vwap_info = None
        fut_chart = None

        if is_index_sym:
            fut_meta = self.resolve_near_month_future(symbol, now=now)
            if fut_meta:
                fut_vwap, fut_vwap_info, fut_chart = self.get_futures_vwap(symbol, fut_meta, now=now)

        bull_setups = ["vwap_reclaim", "breakout_intraday", "orb"]
        bear_setups = ["vwap_rejection", "breakdown_intraday", "orb_breakdown"]

        found_bull_setup = None
        found_bear_setup = None

        if underlying_setup is not None:
            if self.is_valid_session_setup(underlying_setup, now):
                s_side = underlying_setup.get("side", "").lower()
                s_name = underlying_setup.get("setup", "")
                if s_side == "buy" or s_name in bull_setups:
                    found_bull_setup = underlying_setup
                elif s_side == "sell" or s_name in bear_setups:
                    found_bear_setup = underlying_setup

        if not found_bull_setup and not found_bear_setup:
            valid_bulls = []
            for s_name in bull_setups:
                try:
                    if s_name in ("vwap_reclaim", "vwap_rejection") and is_index_sym and fut_vwap is None:
                        # Futures contract or volume unavailable; skip VWAP setups
                        continue
                    res_b = entry.evaluate(chart, now, setup=s_name, market="IN")
                    if is_index_sym and fut_vwap is not None and s_name in ("vwap_reclaim", "vwap_rejection"):
                        res_b = dict(res_b)
                        inds = dict(res_b.get("indicators") or {})
                        inds["vwap"] = fut_vwap
                        inds["vwap_unavailable"] = False
                        res_b["indicators"] = inds
                        res_b["futures_vwap_info"] = fut_vwap_info
                        if res_b.get("reason") and "session VWAP unavailable" in str(res_b.get("reason")):
                            res_b["reason"] = f"evaluated with near-month futures session VWAP ({fut_vwap:.2f})"
                    if self.is_valid_session_setup(res_b, now):
                        valid_bulls.append(res_b)
                except Exception:
                    continue
            if valid_bulls:
                valid_bulls.sort(key=lambda x: 0 if x.get("status") == "triggered" else 1)
                found_bull_setup = valid_bulls[0]

            valid_bears = []
            for s_name in bear_setups:
                try:
                    if s_name in ("vwap_reclaim", "vwap_rejection") and is_index_sym and fut_vwap is None:
                        # Futures contract or volume unavailable; skip VWAP setups
                        continue
                    res_bear = entry.evaluate(chart, now, setup=s_name, market="IN")
                    if is_index_sym and fut_vwap is not None and s_name in ("vwap_reclaim", "vwap_rejection"):
                        res_bear = dict(res_bear)
                        inds = dict(res_bear.get("indicators") or {})
                        inds["vwap"] = fut_vwap
                        inds["vwap_unavailable"] = False
                        res_bear["indicators"] = inds
                        res_bear["futures_vwap_info"] = fut_vwap_info
                        if res_bear.get("reason") and "session VWAP unavailable" in str(res_bear.get("reason")):
                            res_bear["reason"] = f"evaluated with near-month futures session VWAP ({fut_vwap:.2f})"
                    if self.is_valid_session_setup(res_bear, now):
                        valid_bears.append(res_bear)
                except Exception:
                    continue
            if valid_bears:
                valid_bears.sort(key=lambda x: 0 if x.get("status") == "triggered" else 1)
                found_bear_setup = valid_bears[0]

        # One trigger, one direction. A bullish and a bearish setup can both "qualify" at once (e.g.
        # the index sitting on VWAP: vwap_reclaim and vwap_rejection both waiting) -- that produced
        # mirror-image BUY_CE + BUY_PE pairs, a coin flip by construction (2026-11-18 logs). A PE is
        # fine on a bullish day, but it must come from a bearish trigger that actually fired.
        conflict_note = None
        if found_bull_setup and found_bear_setup:
            found_bull_setup, found_bear_setup, conflict_note = resolve_direction_conflict(
                found_bull_setup, found_bear_setup)
            if not found_bull_setup and not found_bear_setup:
                return {
                    "symbol": symbol,
                    "status": "NO_TRADE",
                    "reason": "CONFLICTING_DIRECTION: " + conflict_note,
                    "signals": [],
                    "session_status": session_status,
                }

        # If neither bullish nor bearish setups qualify on real chart
        if not found_bull_setup and not found_bear_setup:
            return {
                "symbol": symbol,
                "status": "NO_TRADE",
                "reason": "NO_QUALIFIED_INTRADAY_SETUP: Real chart price action does not meet strategy rules",
                "signals": [],
                "session_status": session_status,
            }

        signals = []

        chain_retrieval_ts = None
        raw_ts = getattr(chain, "timestamp", None)
        if raw_ts is not None:
            if isinstance(raw_ts, (int, float)):
                chain_retrieval_ts = datetime.datetime.fromtimestamp(raw_ts, tz=datetime.timezone.utc).isoformat()
            elif isinstance(raw_ts, str):
                chain_retrieval_ts = raw_ts
            elif isinstance(raw_ts, datetime.datetime):
                chain_retrieval_ts = raw_ts.isoformat()

        def _enrich_contract(c_dict):
            if not isinstance(c_dict, dict):
                return c_dict
            c = dict(c_dict)
            if c.get("retrieval_timestamp") is None and chain_retrieval_ts:
                c["retrieval_timestamp"] = chain_retrieval_ts
            if c.get("provider") is None and getattr(chain, "provider", None):
                c["provider"] = chain.provider
            return c

        # Construct verified underlying spot quote from streamer tick or chain
        underlying_spot_quote = None
        u_key = getattr(chain, "underlying_key", None)
        if not u_key and symbol:
            try:
                meta_info = instruments.INDEX_INSTRUMENTS.get(symbol.upper(), {})
                u_key = meta_info.get("upstox_key")
            except Exception:
                u_key = None

        is_cash_index = bool(
            symbol.upper() in instruments.INDEX_INSTRUMENTS
            or (u_key and ("INDEX" in str(u_key).upper() or str(u_key).startswith("NSE_INDEX|")))
        )

        if self.streamer is not None and u_key:
            require_depth = not is_cash_index
            live_u_tick, _ = self.streamer.get_live_quote(
                u_key,
                max_age_seconds=DEFAULT_MAX_UNDERLYING_SPOT_AGE_SECONDS,
                now_dt=now,
                require_depth=require_depth,
            )
            if live_u_tick and live_u_tick.get("last_price") is not None:
                try:
                    px_val = float(live_u_tick["last_price"])
                    if px_val > 0.0:
                        underlying_spot_quote = dict(live_u_tick)
                        underlying_spot_quote["price"] = px_val
                        exch_ts = (
                            live_u_tick.get("exchange_timestamp")
                            or live_u_tick.get("last_trade_time")
                            or live_u_tick.get("feed_time")
                            or live_u_tick.get("quote_timestamp")
                        )
                        underlying_spot_quote["quote_timestamp"] = exch_ts
                        underlying_spot_quote["exchange_timestamp"] = exch_ts
                        underlying_spot_quote["quote_provenance"] = "UPSTOX_WEBSOCKET_V2_LIVE"
                        underlying_spot_quote["is_live"] = True
                        underlying_spot_quote["tier"] = "REAL_TIME"
                        underlying_spot_quote["is_request_timestamp_only"] = False
                except (ValueError, TypeError):
                    underlying_spot_quote = None

        if underlying_spot_quote is None and getattr(chain, "underlying_price", None) is not None:
            chain_exch_ts = getattr(chain, "exchange_timestamp", None)
            underlying_spot_quote = {
                "price": float(chain.underlying_price),
                "quote_timestamp": chain_exch_ts,
                "retrieval_timestamp": chain_retrieval_ts,
                "tier": getattr(chain, "data_tier", "REAL_TIME"),
                "is_live": getattr(chain, "is_live", True),
                "provider": getattr(chain, "provider", "upstox"),
                "quote_provenance": "UPSTOX_REST_OPTION_CHAIN",
                "is_request_timestamp_only": (chain_exch_ts is None),
            }

        # --- A. Bullish Opportunities (BUY CE, SELL PE, BULL CALL SPREAD) ---
        if found_bull_setup:
            ce_strike = chain.select_strike("ATM", "CE") or atm_strike
            ce_contract = _enrich_contract(chain.get_contract(ce_strike, "CE"))
            # buy_ce_enabled gates BUY_CE analysis only; ce_contract remains available below for
            # the Bull Call Spread leg regardless of this flag (spreads are a separate, defined-
            # risk structure and are not documented as controlled by this toggle).
            if ce_contract and self.config.get("buy_ce_enabled", True):
                liq_ok, _, _ = self.validate_liquidity(ce_contract)
                if liq_ok:
                    sig_ce = self.generate_signal_contract(
                        underlying=symbol,
                        direction="BULLISH",
                        strategy=STRATEGY_BUY_CE,
                        option_type="CE",
                        strike=ce_strike,
                        contract_data=ce_contract,
                        underlying_setup=found_bull_setup,
                        meta=meta,
                        max_loss_budget=budget,
                        min_reward_risk=min_rr,
                        now_utc=now,
                        portfolio_path=p_path,
                        broker_state_path=b_path,
                        underlying_spot_quote=underlying_spot_quote,
                        broad_regime=broad_regime,
                    )
                    signals.append(sig_ce)

            # SELL PE
            otm_pe_strike = chain.select_strike("OTM1", "PE") or (atm_strike - meta.get("strike_interval", 50.0))
            otm_pe_contract = _enrich_contract(chain.get_contract(otm_pe_strike, "PE"))
            if otm_pe_contract:
                liq_ok, _, _ = self.validate_liquidity(otm_pe_contract)
                if liq_ok:
                    sig_sell_pe = self.generate_signal_contract(
                        underlying=symbol,
                        direction="BULLISH_OR_SIDEWAYS",
                        strategy=STRATEGY_SELL_PE,
                        option_type="PE",
                        strike=otm_pe_strike,
                        contract_data=otm_pe_contract,
                        underlying_setup=found_bull_setup,
                        meta=meta,
                        max_loss_budget=budget,
                        min_reward_risk=min_rr,
                        now_utc=now,
                        portfolio_path=p_path,
                        broker_state_path=b_path,
                        underlying_spot_quote=underlying_spot_quote,
                        broad_regime=broad_regime,
                    )
                    signals.append(sig_sell_pe)

            # BULL PUT CREDIT SPREAD (Sell OTM1 PE + Buy OTM2 PE for protection)
            # Gated by sell_pe_enabled (default False) -- this is the defined-risk, hedged
            # structure that config field was always documented as reserved for (see
            # guides/CONFIGURATION_SPEC.md: "Enable Defined-risk Credit Put Spreads"), previously
            # unimplemented. Unlike naked SELL_PE, this is NOT subject to NAKED_OPTION_PROHIBITED
            # (it has a genuine long protective leg) -- it inherits the same margin-authority
            # treatment as the existing BULL_CALL_SPREAD/BEAR_PUT_SPREAD debit spreads.
            if self.config.get("sell_pe_enabled", False) and otm_pe_contract:
                otm2_pe_strike_bps = chain.select_strike("OTM2", "PE")
                otm2_pe_contract_bps = _enrich_contract(chain.get_contract(otm2_pe_strike_bps, "PE")) if otm2_pe_strike_bps else None
                if otm2_pe_contract_bps:
                    l1, _, _ = self.validate_liquidity(otm_pe_contract)
                    l2, _, _ = self.validate_liquidity(otm2_pe_contract_bps)
                    if l1 and l2:
                        sig_bps_credit = self.generate_signal_contract(
                            underlying=symbol,
                            direction="BULLISH_OR_SIDEWAYS",
                            strategy=STRATEGY_BULL_PUT_SPREAD,
                            option_type="SPREAD",
                            strike=otm_pe_strike,
                            contract_data=otm_pe_contract,
                            underlying_setup=found_bull_setup,
                            meta=meta,
                            spread_contract={
                                "strike": otm2_pe_strike_bps,
                                "symbol": otm2_pe_contract_bps.get("symbol"),
                                "last_price": otm2_pe_contract_bps.get("last_price"),
                                "bid": otm2_pe_contract_bps.get("bid"),
                                "ask": otm2_pe_contract_bps.get("ask"),
                                "bid_qty": otm2_pe_contract_bps.get("bid_qty"),
                                "ask_qty": otm2_pe_contract_bps.get("ask_qty"),
                                "quote_timestamp": otm2_pe_contract_bps.get("quote_timestamp"),
                                "retrieval_timestamp": otm2_pe_contract_bps.get("retrieval_timestamp"),
                                "expiry": otm2_pe_contract_bps.get("expiry") or meta.get("expiry"),
                                "underlying": symbol,
                                "option_type": "PE",
                                "is_live": otm2_pe_contract_bps.get("is_live", True),
                                "tier": otm2_pe_contract_bps.get("tier", "REAL_TIME"),
                                "delta": (otm2_pe_contract_bps.get("greeks") or {}).get("delta"),
                                "iv": otm2_pe_contract_bps.get("iv"),
                            },
                            max_loss_budget=budget,
                            min_reward_risk=min_rr,
                            now_utc=now,
                            portfolio_path=p_path,
                            broker_state_path=b_path,
                            underlying_spot_quote=underlying_spot_quote,
                            broad_regime=broad_regime,
                        )
                        signals.append(sig_bps_credit)

            # BULL CALL SPREAD
            otm2_ce_strike = chain.select_strike("OTM2", "CE")
            otm2_ce_contract = _enrich_contract(chain.get_contract(otm2_ce_strike, "CE")) if otm2_ce_strike else None
            if ce_contract and otm2_ce_contract:
                l1, _, _ = self.validate_liquidity(ce_contract)
                l2, _, _ = self.validate_liquidity(otm2_ce_contract)
                if l1 and l2:
                    sig_bcs = self.generate_signal_contract(
                        underlying=symbol,
                        direction="BULLISH",
                        strategy=STRATEGY_BULL_CALL_SPREAD,
                        option_type="SPREAD",
                        strike=ce_strike,
                        contract_data=ce_contract,
                        underlying_setup=found_bull_setup,
                        meta=meta,
                        spread_contract={
                            "strike": otm2_ce_strike,
                            "symbol": otm2_ce_contract.get("symbol"),
                            "last_price": otm2_ce_contract.get("last_price"),
                            "bid": otm2_ce_contract.get("bid"),
                            "ask": otm2_ce_contract.get("ask"),
                            "bid_qty": otm2_ce_contract.get("bid_qty"),
                            "ask_qty": otm2_ce_contract.get("ask_qty"),
                            "quote_timestamp": otm2_ce_contract.get("quote_timestamp"),
                            "retrieval_timestamp": otm2_ce_contract.get("retrieval_timestamp"),
                            "expiry": otm2_ce_contract.get("expiry") or meta.get("expiry"),
                            "underlying": symbol,
                            "option_type": "CE",
                            "is_live": otm2_ce_contract.get("is_live", True),
                            "tier": otm2_ce_contract.get("tier", "REAL_TIME"),
                            "delta": (otm2_ce_contract.get("greeks") or {}).get("delta"),
                            "iv": otm2_ce_contract.get("iv"),
                        },
                        max_loss_budget=budget,
                        min_reward_risk=min_rr,
                        now_utc=now,
                        portfolio_path=p_path,
                        broker_state_path=b_path,
                        underlying_spot_quote=underlying_spot_quote,
                        broad_regime=broad_regime,
                    )
                    signals.append(sig_bcs)

        # --- B. Bearish Opportunities (BUY PE, SELL CE, BEAR PUT SPREAD) ---
        if found_bear_setup:
            pe_strike = chain.select_strike("ATM", "PE") or atm_strike
            pe_contract = _enrich_contract(chain.get_contract(pe_strike, "PE"))
            # buy_pe_enabled gates BUY_PE analysis only; pe_contract remains available below for
            # the Bear Put Spread leg regardless of this flag.
            if pe_contract and self.config.get("buy_pe_enabled", True):
                liq_ok, _, _ = self.validate_liquidity(pe_contract)
                if liq_ok:
                    sig_pe = self.generate_signal_contract(
                        underlying=symbol,
                        direction="BEARISH",
                        strategy=STRATEGY_BUY_PE,
                        option_type="PE",
                        strike=pe_strike,
                        contract_data=pe_contract,
                        underlying_setup=found_bear_setup,
                        meta=meta,
                        max_loss_budget=budget,
                        min_reward_risk=min_rr,
                        now_utc=now,
                        portfolio_path=p_path,
                        broker_state_path=b_path,
                        underlying_spot_quote=underlying_spot_quote,
                        broad_regime=broad_regime,
                    )
                    signals.append(sig_pe)

            # SELL CE
            otm_ce_strike = chain.select_strike("OTM1", "CE") or (atm_strike + meta.get("strike_interval", 50.0))
            otm_ce_contract = _enrich_contract(chain.get_contract(otm_ce_strike, "CE"))
            if otm_ce_contract:
                liq_ok, _, _ = self.validate_liquidity(otm_ce_contract)
                if liq_ok:
                    sig_sell_ce = self.generate_signal_contract(
                        underlying=symbol,
                        direction="BEARISH_OR_SIDEWAYS",
                        strategy=STRATEGY_SELL_CE,
                        option_type="CE",
                        strike=otm_ce_strike,
                        contract_data=otm_ce_contract,
                        underlying_setup=found_bear_setup,
                        meta=meta,
                        max_loss_budget=budget,
                        min_reward_risk=min_rr,
                        now_utc=now,
                        portfolio_path=p_path,
                        broker_state_path=b_path,
                        underlying_spot_quote=underlying_spot_quote,
                        broad_regime=broad_regime,
                    )
                    signals.append(sig_sell_ce)

            # BEAR CALL CREDIT SPREAD (Sell OTM1 CE + Buy OTM2 CE for protection)
            # Gated by sell_ce_enabled (default False) -- mirrors the Bull Put Credit Spread
            # above. Not subject to NAKED_OPTION_PROHIBITED (genuine protective long leg).
            if self.config.get("sell_ce_enabled", False) and otm_ce_contract:
                otm2_ce_strike_bcs = chain.select_strike("OTM2", "CE")
                otm2_ce_contract_bcs = _enrich_contract(chain.get_contract(otm2_ce_strike_bcs, "CE")) if otm2_ce_strike_bcs else None
                if otm2_ce_contract_bcs:
                    l1, _, _ = self.validate_liquidity(otm_ce_contract)
                    l2, _, _ = self.validate_liquidity(otm2_ce_contract_bcs)
                    if l1 and l2:
                        sig_bcs_credit = self.generate_signal_contract(
                            underlying=symbol,
                            direction="BEARISH_OR_SIDEWAYS",
                            strategy=STRATEGY_BEAR_CALL_SPREAD,
                            option_type="SPREAD",
                            strike=otm_ce_strike,
                            contract_data=otm_ce_contract,
                            underlying_setup=found_bear_setup,
                            meta=meta,
                            spread_contract={
                                "strike": otm2_ce_strike_bcs,
                                "symbol": otm2_ce_contract_bcs.get("symbol"),
                                "last_price": otm2_ce_contract_bcs.get("last_price"),
                                "bid": otm2_ce_contract_bcs.get("bid"),
                                "ask": otm2_ce_contract_bcs.get("ask"),
                                "bid_qty": otm2_ce_contract_bcs.get("bid_qty"),
                                "ask_qty": otm2_ce_contract_bcs.get("ask_qty"),
                                "quote_timestamp": otm2_ce_contract_bcs.get("quote_timestamp"),
                                "retrieval_timestamp": otm2_ce_contract_bcs.get("retrieval_timestamp"),
                                "expiry": otm2_ce_contract_bcs.get("expiry") or meta.get("expiry"),
                                "underlying": symbol,
                                "option_type": "CE",
                                "is_live": otm2_ce_contract_bcs.get("is_live", True),
                                "tier": otm2_ce_contract_bcs.get("tier", "REAL_TIME"),
                                "delta": (otm2_ce_contract_bcs.get("greeks") or {}).get("delta"),
                                "iv": otm2_ce_contract_bcs.get("iv"),
                            },
                            max_loss_budget=budget,
                            min_reward_risk=min_rr,
                            now_utc=now,
                            portfolio_path=p_path,
                            broker_state_path=b_path,
                            underlying_spot_quote=underlying_spot_quote,
                            broad_regime=broad_regime,
                        )
                        signals.append(sig_bcs_credit)

            # BEAR PUT SPREAD
            otm2_pe_strike = chain.select_strike("OTM2", "PE")
            otm2_pe_contract = _enrich_contract(chain.get_contract(otm2_pe_strike, "PE")) if otm2_pe_strike else None
            if pe_contract and otm2_pe_contract:
                l1, _, _ = self.validate_liquidity(pe_contract)
                l2, _, _ = self.validate_liquidity(otm2_pe_contract)
                if l1 and l2:
                    sig_bps = self.generate_signal_contract(
                        underlying=symbol,
                        direction="BEARISH",
                        strategy=STRATEGY_BEAR_PUT_SPREAD,
                        option_type="SPREAD",
                        strike=pe_strike,
                        contract_data=pe_contract,
                        underlying_setup=found_bear_setup,
                        meta=meta,
                        spread_contract={
                            "strike": otm2_pe_strike,
                            "symbol": otm2_pe_contract.get("symbol"),
                            "last_price": otm2_pe_contract.get("last_price"),
                            "bid": otm2_pe_contract.get("bid"),
                            "ask": otm2_pe_contract.get("ask"),
                            "bid_qty": otm2_pe_contract.get("bid_qty"),
                            "ask_qty": otm2_pe_contract.get("ask_qty"),
                            "quote_timestamp": otm2_pe_contract.get("quote_timestamp"),
                            "retrieval_timestamp": otm2_pe_contract.get("retrieval_timestamp"),
                            "expiry": otm2_pe_contract.get("expiry") or meta.get("expiry"),
                            "underlying": symbol,
                            "option_type": "PE",
                            "is_live": otm2_pe_contract.get("is_live", True),
                            "tier": otm2_pe_contract.get("tier", "REAL_TIME"),
                            "delta": (otm2_pe_contract.get("greeks") or {}).get("delta"),
                            "iv": otm2_pe_contract.get("iv"),
                        },
                        max_loss_budget=budget,
                        min_reward_risk=min_rr,
                        now_utc=now,
                        portfolio_path=p_path,
                        broker_state_path=b_path,
                        underlying_spot_quote=underlying_spot_quote,
                        broad_regime=broad_regime,
                    )
                    signals.append(sig_bps)

        if not signals:
            return {
                "symbol": symbol,
                "status": "NO_TRADE",
                "reason": "INSUFFICIENT_LIQUIDITY_OR_HIGH_SPREAD: Evaluated option strikes failed liquidity criteria",
                "signals": [],
            }

        is_market_open = calendar_in.is_market_hours_in(now, exchange="NSE")
        chart_is_live = bool(chart.get("is_live", False)) and (str(chart.get("data_tier", "")).upper() == "REAL_TIME")
        if not is_market_open or not chart_is_live:
            session_status = "MARKET_CLOSED (POST-MARKET REVIEW)"
        else:
            session_status = "MARKET_OPEN (LIVE SESSION)"

        return {
            "symbol": symbol,
            "status": "OPPORTUNITIES_IDENTIFIED",
            "pcr": chain.pcr,
            "atm_strike": atm_strike,
            "underlying_price": spot_price,
            "expiry": meta.get("expiry"),
            "session_status": session_status,
            "signals": signals,
        }

    def rank_opportunities(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Rank evaluated options opportunities by composite quality:
        Score = (Setup & R:R * 0.45) + (Risk Structure * 0.30) + (Data Tier * 0.25)
        De-duplicates overlapping or correlated contracts.
        """
        ranked = []
        seen_contracts = set()

        for sig in signals:
            c_key = f"{sig['underlying_instrument']}_{sig['strategy']}_{sig['strike']}"
            if c_key in seen_contracts:
                continue
            seen_contracts.add(c_key)

            rr = float(sig.get("expected_risk_reward_after_charges") or 0.0)
            margin_details = sig.get("margin_details") or {}
            is_valid = sig.get("is_actionable", False) or sig.get("signal_state") == STATE_HISTORICAL_ANALYSIS

            # Score components
            score_rr = min(5.0, max(0.0, rr)) / 5.0
            score_struct = 1.0 if not margin_details.get("is_undefined_risk") else 0.4

            dq = sig.get("data_quality") or {}
            score_tier = 1.0 if dq.get("is_live") else (0.7 if dq.get("tier") == "HISTORICAL" else 0.4)

            # Penalize rejected trades heavily
            penalty = 1.0 if is_valid else 0.1

            composite_score = round(((score_rr * 0.45) + (score_struct * 0.30) + (score_tier * 0.25)) * penalty, 3)
            sig["ranking_score"] = composite_score
            ranked.append(sig)

        ranked.sort(key=lambda s: s["ranking_score"], reverse=True)
        return ranked


# ---------------------------------------------------------------- CLI

def cmd_chain(args, specialist: OptionsIntelligenceSpecialist):
    """Fetch and display option chain."""
    sym = args.symbol.upper()
    chain = specialist.fetch_normalized_chain(sym, expiry=args.expiry)
    if not chain:
        print(f"Error: Unable to fetch option chain for {sym}.")
        return 1

    print(f"\n=================== OPTION CHAIN: {chain.underlying} ===================")
    print(f"Underlying Price: {chain.underlying_price} | Expiry: {chain.expiry}")
    print(f"ATM Strike: {chain.atm_strike} | PCR: {chain.pcr} | Tier: {chain.data_tier}")
    print("-" * 75)
    print(f"{'CE Bid':>8} {'CE Ask':>8} {'CE LTP':>8} {'Strike':^10} {'PE LTP':>8} {'PE Bid':>8} {'PE Ask':>8}")
    print("-" * 75)

    strikes = chain.get_strikes()
    atm = chain.atm_strike
    if atm in strikes:
        idx = strikes.index(atm)
        display_strikes = strikes[max(0, idx - 5): min(len(strikes), idx + 6)]
    else:
        display_strikes = strikes[:10]

    for k in display_strikes:
        ce = chain.get_contract(k, "CE") or {}
        pe = chain.get_contract(k, "PE") or {}
        star = "*" if k == atm else " "
        print(
            f"{ce.get('bid', 0):8.2f} {ce.get('ask', 0):8.2f} {ce.get('last_price', 0):8.2f} "
            f"{star}{k:^8.1f}{star} "
            f"{pe.get('last_price', 0):8.2f} {pe.get('bid', 0):8.2f} {pe.get('ask', 0):8.2f}"
        )
    print("-" * 75)
    print("Execution Boundary: Decision-support only. Live broker execution disabled.\n")
    return 0


def cmd_analyze(args, specialist: OptionsIntelligenceSpecialist):
    """Analyze underlying symbol and generate all valid options signals."""
    sym = args.symbol.upper()
    res = specialist.analyze_symbol_for_options(sym, max_loss_budget=args.budget)
    if res.get("status") == "NO_TRADE":
        print(f"\n[NO TRADE] {sym}: {res.get('reason')}\n")
        return 1

    session_tag = res.get("session_status", "POST-MARKET REVIEW")
    print(f"\n================ OPTIONS INTELLIGENCE: {sym} ================")
    print(f"Underlying Spot: {res['underlying_price']} | ATM Strike: {res['atm_strike']} | PCR: {res['pcr']}")
    print(f"Expiry: {res['expiry']} | Session Status: {session_tag}\n")

    ranked = specialist.rank_opportunities(res["signals"])
    for i, sig in enumerate(ranked[: args.limit], 1):
        status_tag = f"[{sig['signal_state']}]"
        recon = sig.get("execution_reconciliation") or {}
        prov = sig.get("data_provenance") or {}
        margin = sig.get("margin_details") or {}
        evidence = sig.get("underlying_setup_evidence") or {}

        print(f"--- Option Setup #{i}: {sig['strategy']} (Score: {sig['ranking_score']}) {status_tag} ---")
        print(f"Contract: {sig['contract']} (Strike: {sig['strike']}, Expiry: {sig['expiry']}, Lot Size: {sig['position_sizing']['lot_size']})")
        print(f"Direction: {sig['direction']} | Lots: {sig['position_sizing']['lots']} ({sig['position_sizing']['total_quantity']} qty)")

        # 1. Underlying setup evidence
        trig = evidence.get("entry_trigger") or {}
        print(f"Underlying Setup: {evidence.get('setup_name')} | Bias: {evidence.get('directional_bias')} | Scanner: {evidence.get('scanner_result')}")
        print(f"Trigger: Spot {trig.get('price')} @ {trig.get('time')} ({trig.get('reason')})")
        print(f"Invalidation: Spot {evidence.get('invalidation_level')} | Key S/R: Spot {evidence.get('support_resistance_level')}")
        print(f"Strike Rationale: {evidence.get('strike_selection_rationale')}")

        # 2. Execution price reconciliation
        print(f"Execution Entry: Rs {sig['current_premium']:.2f} (Conservative {recon.get('baseline_quote_type')}: Rs {recon.get('baseline_quote_price'):.2f}, Slippage: {recon.get('slippage_applied', 0):+.2f} | Bid: Rs {recon.get('bid', 0):.2f}, Ask: Rs {recon.get('ask', 0):.2f}, Spread: {recon.get('spread_pct', 0)}%)")
        entry_range = sig.get("option_entry_range") or {}
        print(f"VALID ENTRY RANGE: Rs {entry_range.get('min', 0):.2f} to Rs {entry_range.get('max', 0):.2f} (ideal Rs {entry_range.get('ideal', 0):.2f})")
        print(f"  -> {entry_range.get('instruction', '')}")

        # 3. Dynamic exits & net R:R
        # Zero Fabrication Policy: a premium level that the Delta/Gamma model could not validate
        # is None (MODEL_UNAVAILABLE), never a printed 0.0 -- render it explicitly as N/A.
        def _prem_or_na(v):
            return f"Rs {v:.2f}" if v is not None else "N/A"

        rr_val = sig.get("expected_risk_reward_after_charges")
        rr_or_na = f"{rr_val}:1" if rr_val is not None else "N/A (MODEL_UNAVAILABLE)"
        print(f"Stop Loss: {_prem_or_na(sig['stop_loss'])} | Target 1: {_prem_or_na(sig['target_1'])} | Target 2: {_prem_or_na(sig['target_2'])}")
        print(f"Net R:R: {rr_or_na} (Net of STT, NSE txn, SEBI, Stamp Duty, GST, Brokerage)")

        # 4. Risk reporting (Stop-loss risk vs Maximum theoretical capital loss)
        ps_info = sig.get("position_sizing") or {}
        net_risk_per_lot = ps_info.get("net_risk_per_lot_inr")
        net_risk_per_lot_str = f"Rs {net_risk_per_lot:,.2f}" if net_risk_per_lot is not None else "N/A"
        print(f"Defined Stop-Loss Risk: Rs {ps_info.get('total_stop_loss_risk_inr', 0):,.2f} ({net_risk_per_lot_str}/lot incl. charges; Budget: Rs {ps_info.get('budget_max_loss_inr', 0):,.2f})")
        print(f"Maximum Theoretical Capital Loss: {sig['maximum_loss']}")
        print("Execution Warning: Stop-loss order execution is NOT guaranteed. Subject to market liquidity, slippage, and opening price gaps.")

        # 5. Margin transparency
        print(f"Capital / Margin Required: Rs {sig['capital_margin_requirement']:,.2f} [{margin.get('margin_type', 'HEURISTIC_MARGIN_ESTIMATE')}]")
        print(f"Margin Model: {margin.get('formula')} -> {margin.get('assumptions')}")
        print(f"Broker Margin Disclaimer: {margin.get('broker_margin_disclaimer')}")
        print(f"Max Loss: {sig['maximum_loss']}")
        print(f"Tail Risk: {margin.get('tail_risk')}")

        # 6. Data provenance
        print(f"Data Provenance: Provider: {prov.get('data_provider')}, Tier: {prov.get('data_tier')}, Session: {prov.get('market_session_status')}, Freshness: {prov.get('freshness_validation_result')}")
        print(f"Timestamps: Quote: {prov.get('exchange_quote_timestamp')} (Age: {prov.get('quote_age_seconds')}s), Candle: {prov.get('underlying_candle_timestamp')} (Age: {prov.get('candle_age_seconds')}s), Retrieval: {prov.get('retrieval_timestamp')} (Age: {prov.get('retrieval_age_seconds')}s)")

        if sig.get("rejection_reasons"):
            print(f"Rejections: {'; '.join(sig['rejection_reasons'])}")
        print(f"Rationale: {sig['selection_rationale']}\n")

    return 0


def run_standalone_analysis(args, orchestrator_cls=None) -> int:
    """Execute standalone options analysis using shared orchestrator streamer lifecycle."""
    now_utc = _utc_now()
    is_open = calendar_in.is_market_hours_in(now_utc, exchange="NSE")

    if orchestrator_cls is None:
        from orchestrator import MarketIntelligenceOrchestrator
        orchestrator_cls = MarketIntelligenceOrchestrator

    with orchestrator_cls() as orch:
        # Check credentials and streamer availability during live market hours
        if is_open:
            if orch.streamer is None:
                print(
                    "\n[FAIL CLOSED] Live analysis requires an authenticated WebSocket streamer for real-time exchange timestamps.\n"
                    "Upstox credentials unavailable (set UPSTOX_ANALYTICS_TOKEN or config provider_credentials.upstox.analytics_token).\n"
                    "REST-only fallback cannot produce actionable live recommendations.\n"
                )
                return 1

            if not getattr(orch.streamer, "is_connected", False):
                try:
                    orch.streamer.connect()
                except Exception:
                    pass

            if not getattr(orch.streamer, "is_connected", False):
                print(
                    "\n[FAIL CLOSED] Live analysis requires an active WebSocket streamer connection.\n"
                    "Failed to establish live WebSocket connection to market feed.\n"
                )
                return 1

        if orch.streamer is not None and getattr(orch.specialist, "streamer", None) is None:
            orch.specialist.streamer = orch.streamer

        return cmd_analyze(args, orch.specialist)


def main(argv=None):
    ap = argparse.ArgumentParser(description="trader-options: Dedicated Options Intelligence Specialist")
    sub = ap.add_subparsers(dest="cmd")

    p_chain = sub.add_parser("chain", help="Fetch and inspect normalized option chain")
    p_chain.add_argument("--symbol", "-s", default="NIFTY", help="Underlying symbol (default NIFTY)")
    p_chain.add_argument("--expiry", default=None, help="Option contract expiry date (YYYY-MM-DD)")

    p_analyze = sub.add_parser("analyze", help="Analyze symbol for options buying and selling opportunities")
    p_analyze.add_argument("--symbol", "-s", default="NIFTY", help="Underlying symbol (default NIFTY)")
    p_analyze.add_argument("--budget", type=float, default=DEFAULT_MAX_LOSS_INR, help="Max loss risk budget in INR")
    p_analyze.add_argument("--limit", type=int, default=5, help="Max opportunities to display")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0

    if args.cmd == "chain":
        specialist = OptionsIntelligenceSpecialist()
        return cmd_chain(args, specialist)
    elif args.cmd == "analyze":
        return run_standalone_analysis(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
