"""
Stage 1 regression suite: Options Calculation Integrity.

Covers the forensic findings remediated in this stage, scoped narrowly to:
1. model_premium_levels()/generate_signal_contract() must never substitute 0.0 for an
   unavailable stop-loss or target premium (Zero Fabrication Policy).
2. Position sizing must never size risk from a missing/unavailable stop.
3. A model-unavailable candidate must never be misclassified as RR_BELOW_MINIMUM, and must
   never become actionable.
4. The theta time-decay conversion must use a calendar-day basis consistent with the theta
   unit actually produced by each data source (data_in.py black_scholes() and the Upstox
   passthrough), not the trading-session-hours constant used previously.

Out of scope (explicitly not touched by this stage, not re-tested here): spread payoff
architecture, naked-selling policy, configuration gates, liquidity thresholds, instrument
discovery, WebSocket lifecycle. Those are covered by tests/test_options_intelligence.py and
tests/test_banknifty_audit.py, which this stage does not modify.
"""

import datetime
import json
import os
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

for path in [
    os.path.join(BASE_DIR, "trader-alerts"),
    os.path.join(BASE_DIR, "trader-entry"),
    os.path.join(BASE_DIR, "trader-scanner"),
    os.path.join(BASE_DIR, "trader-budget"),
    os.path.join(BASE_DIR, "trader-options"),
]:
    if path not in sys.path:
        sys.path.insert(0, path)

import _load  # noqa: E402,F401
import calendar_in  # noqa: E402
import data_in  # noqa: E402
from options import OptionsIntelligenceSpecialist, STRATEGY_BUY_CE  # noqa: E402

SAMPLE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.sampleAnalyticsTokenPayload.sig123456"


@pytest.fixture
def specialist():
    return OptionsIntelligenceSpecialist()


# A fixed, deliberately extreme underlying move (small delta/gamma, large theta) that makes the
# Taylor-expansion-projected target premium fall at or below the entry premium -- the documented
# non-viable condition that model_premium_levels() must reject with None.
NON_VIABLE_GREEKS = {"delta": 0.02, "gamma": 0.0, "theta": -300.0}
CLOSED_MARKET_NOW = datetime.datetime(2026, 11, 14, 16, 30, tzinfo=datetime.timezone.utc)  # Saturday


def _unavailable_signal(specialist, strategy=STRATEGY_BUY_CE, option_type="CE"):
    """Build a signal contract whose premium model is guaranteed to be unavailable."""
    return specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=strategy,
        option_type=option_type,
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "greeks": dict(NON_VIABLE_GREEKS),
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=CLOSED_MARKET_NOW,
    )


# ---------------------------------------------------------------- 1. Premium model unavailable propagation

def test_failed_premium_model_propagates_none_not_zero(specialist):
    """A non-viable Delta/Gamma model must surface stop/target as None, never 0.0."""
    # First confirm the underlying model itself still returns None for this input (precondition).
    levels = specialist.model_premium_levels(
        entry_premium=137.75,
        spot_entry=25200.0,
        spot_stop=25050.0,
        spot_t1=25550.0,
        spot_t2=25750.0,
        option_type="CE",
        is_seller=False,
        **NON_VIABLE_GREEKS,
    )
    assert levels is None

    sig = _unavailable_signal(specialist)
    assert sig["stop_loss"] is None
    assert sig["target_1"] is None
    assert sig["target_2"] is None
    assert sig["expected_risk_reward_after_charges"] is None
    assert any(r.startswith("OPTION_RISK_UNAVAILABLE") for r in sig["rejection_reasons"])
    assert any(r.startswith("MODEL_UNAVAILABLE") for r in sig["rejection_reasons"])


# ---------------------------------------------------------------- 2. Missing stop cannot enter position sizing

def test_missing_stop_cannot_enter_position_sizing(specialist):
    """No lot, capital, or risk figure may be derived from a missing stop premium."""
    sig = _unavailable_signal(specialist)
    ps = sig["position_sizing"]
    assert ps["sizing_state"] == "MODEL_UNAVAILABLE"
    assert ps["lots"] == 0
    assert ps["final_permitted_lots"] == 0
    assert ps["risk_budget_permitted_lots"] == 0
    # Per-lot risk figures must be unavailable, never a number derived from entry premium
    # (the defect under test: substituting 0.0 made risk_per_lot == entry_premium * lot_size,
    # i.e. a fabricated "100% premium loss" risk estimate).
    assert ps["risk_per_lot_inr"] is None
    assert ps["net_risk_per_lot_inr"] is None
    # Maximum theoretical capital loss is independent of the stop model and must remain populated.
    assert sig["maximum_loss"] not in (None, "", "UNKNOWN")
    assert ps["maximum_possible_loss"] == sig["maximum_loss"]


# ---------------------------------------------------------------- 3. Human-readable N/A, not 0.0

def test_missing_rr_displays_na_not_zero_in_rationale(specialist):
    """The rationale string must read N/A (MODEL_UNAVAILABLE), never 'Net R:R: 0.0:1'."""
    sig = _unavailable_signal(specialist)
    assert "Net R:R: N/A (MODEL_UNAVAILABLE)" in sig["selection_rationale"]
    assert "0.0:1" not in sig["selection_rationale"]
    assert "N/A (premium stop/target model unavailable" in sig["invalidation_conditions"]


# ---------------------------------------------------------------- 4. No RR_BELOW_MINIMUM misclassification

def test_model_unavailable_does_not_generate_rr_below_minimum(specialist):
    """An unavailable model must not be relabeled as a computed, below-floor R:R."""
    sig = _unavailable_signal(specialist)
    assert not any(r.startswith("RR_BELOW_MINIMUM") for r in sig["rejection_reasons"])
    assert not any(r.startswith("ZERO_OR_NEGATIVE_RISK") for r in sig["rejection_reasons"])


# ---------------------------------------------------------------- 5. Never actionable

def test_model_unavailable_signal_is_never_actionable(specialist):
    sig = _unavailable_signal(specialist)
    assert sig["is_actionable"] is False
    assert sig["signal_state"] == "SIGNAL REJECTED"


# ---------------------------------------------------------------- 6. Calendar-day vs trading-session theta

def test_theta_decay_uses_calendar_day_not_trading_session_basis(specialist):
    """
    theta is a per-CALENDAR-day figure (confirmed convention of data_in.black_scholes()).
    Decay over a 2-hour hold must be theta * (2/24), not theta * (2/6.25) -- the previous,
    ~3.84x-inflated conversion that used the NSE trading-session length as the denominator.
    """
    levels = specialist.model_premium_levels(
        entry_premium=100.0,
        spot_entry=25000.0,
        spot_stop=24900.0,
        spot_t1=25200.0,
        spot_t2=25400.0,
        delta=0.50,
        gamma=0.001,
        theta=-24.0,
        holding_hours=2.0,
        option_type="CE",
        is_seller=False,
    )
    assert levels is not None
    expected_decay = round(24.0 * (2.0 / 24.0), 2)  # == 2.0
    assert levels["theta_decay_applied"] == expected_decay
    wrong_decay_old_basis = round(24.0 * (2.0 / 6.25), 2)  # == 7.68 -- must NOT match
    assert levels["theta_decay_applied"] != wrong_decay_old_basis


# ---------------------------------------------------------------- 7. Provider theta units normalized/declared

def test_black_scholes_theta_is_tagged_per_calendar_day():
    bs = data_in.black_scholes(25000.0, 25000.0, 15 / 365.0, option_type="CE")
    assert bs["theta_unit"] == "PER_CALENDAR_DAY"

    bs_expiry = data_in.black_scholes(25000.0, 25000.0, 0.0, option_type="CE")
    assert bs_expiry["theta_unit"] == "PER_CALENDAR_DAY"


def test_upstox_theta_unit_is_declared_as_unverified_assumption():
    """
    Upstox's public API documentation does not state theta's time unit. The data boundary must
    declare this explicitly (not silently assume parity with the internal Black-Scholes
    calendar-day convention) so any future consumer can see the assumption is unverified.
    """
    open_dt = datetime.datetime(2026, 10, 19, 11, 0, 0, tzinfo=calendar_in.IST)

    def fake_http(url, headers=None, timeout=15):
        if "/v2/option/chain" in url:
            return 200, json.dumps({
                "status": "success",
                "data": [
                    {
                        "expiry": "2026-10-27",
                        "pcr": 1.15,
                        "strike_price": 24500,
                        "underlying_key": "NSE_INDEX|Nifty 50",
                        "underlying_spot_price": 24515.0,
                        "call_options": {
                            "instrument_key": "NSE_FO|10003",
                            "market_data": {
                                "ltp": 132.5, "volume": 150000, "oi": 3100000, "prev_oi": 2950000,
                                "bid_price": 132.2, "ask_price": 132.8,
                            },
                            "option_greeks": {"delta": 0.52, "gamma": 0.0014, "theta": -12.0, "vega": 9.5, "iv": 13.2},
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|10004",
                            "market_data": {
                                "ltp": 108.0, "volume": 140000, "oi": 3400000, "prev_oi": 3300000,
                                "bid_price": 107.8, "ask_price": 108.2,
                            },
                            "option_greeks": {"delta": -0.48, "gamma": 0.0014, "theta": None, "vega": 9.5, "iv": 13.6},
                        },
                    },
                ],
            })
        raise AssertionError("Unexpected URL: %s" % url)

    prov = data_in.UpstoxMarketDataProvider(
        analytics_token=SAMPLE_TOKEN,
        http_get=fake_http,
        now_fn=lambda: open_dt,
    )
    chain = prov.get_option_chain("NIFTY", expiry="2026-10-27")
    ce = chain.get_contract(24500.0, "CE")
    pe = chain.get_contract(24500.0, "PE")

    # Present theta -> explicitly flagged as an unverified assumption (never silently equated
    # with the confirmed Black-Scholes calendar-day convention).
    assert ce["greeks"]["theta"] == -12.0
    assert ce["greeks"]["theta_unit"] == "PER_CALENDAR_DAY_ASSUMED_UNVERIFIED"

    # Absent theta -> no unit is fabricated for a value that doesn't exist.
    assert pe["greeks"]["theta"] is None
    assert pe["greeks"]["theta_unit"] is None
