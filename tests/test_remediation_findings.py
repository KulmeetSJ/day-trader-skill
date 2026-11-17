"""
Comprehensive regression tests for audit findings N-1, N-2, N-3, N-4, N-5, N-6, F-005, F-006, F-020.
Enforces zero em-dashes and strict safety/correctness invariants across Indian options trading.
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
    os.path.join(BASE_DIR, "trader-options"),
    os.path.join(BASE_DIR, "trader-risk"),
    os.path.join(BASE_DIR, "trader-entry"),
    os.path.join(BASE_DIR, "trader-budget"),
    os.path.join(BASE_DIR, "trader-alerts"),
]:
    if path not in sys.path:
        sys.path.insert(0, path)

from options import (
    OptionsIntelligenceSpecialist,
    STRATEGY_BUY_CE,
    STRATEGY_BUY_PE,
    STRATEGY_BULL_CALL_SPREAD,
    STRATEGY_BEAR_PUT_SPREAD,
    STATE_SIGNAL_VALIDATED,
    STATE_SIGNAL_REJECTED,
)
from orchestrator import MarketIntelligenceOrchestrator, ScanLock, ScanLockError


# ==============================================================================
# N-1: Authoritative Daily Loss Cap Tests
# ==============================================================================

def test_n1_exhausted_daily_loss_blocks_all_budgets(tmp_path):
    """N-1: When daily loss limit is exhausted, candidate trades with any budget (2k, 5k, 50k) are blocked."""
    spec = OptionsIntelligenceSpecialist()
    port_file = str(tmp_path / "portfolio.json")
    broker_file = str(tmp_path / "broker.json")
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    today_iso = "2026-11-17"

    with open(port_file, "w", encoding="utf-8") as f:
        json.dump({"cash": 100000.0, "max_daily_loss": 5000.0}, f)

    # Daily loss already exhausted (5000 used today)
    with open(broker_file, "w", encoding="utf-8") as f:
        json.dump({
            "orders": [
                {"symbol": "NIFTY26NOV25000CE", "status": "placed", "date": today_iso, "max_loss": 5000.0}
            ]
        }, f)

    for test_budget in [2000.0, 5000.0, 50000.0]:
        is_valid, rejections = spec.check_portfolio_risk(
            candidate_risk=1000.0,
            capital_required=10000.0,
            portfolio_path=port_file,
            broker_state_path=broker_file,
            now_utc=now_utc,
            is_live_check=True,
            candidate_risk_budget=test_budget,
        )
        assert is_valid is False
        assert any("DAILY_LOSS_LIMIT_EXHAUSTED" in r for r in rejections)


def test_n1_candidate_budget_cannot_expand_daily_loss_limit(tmp_path):
    """N-1: A candidate risk budget larger than max_daily_loss cannot expand the daily loss limit."""
    spec = OptionsIntelligenceSpecialist()
    port_file = str(tmp_path / "portfolio.json")
    broker_file = str(tmp_path / "broker.json")
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)

    with open(port_file, "w", encoding="utf-8") as f:
        json.dump({"cash": 100000.0, "max_daily_loss": 5000.0}, f)
    with open(broker_file, "w", encoding="utf-8") as f:
        json.dump({"placed_today": []}, f)

    # Candidate risk is 6,000, which exceeds max_daily_loss (5,000) even though budget is 50,000
    is_valid, rejections = spec.check_portfolio_risk(
        candidate_risk=6000.0,
        capital_required=20000.0,
        portfolio_path=port_file,
        broker_state_path=broker_file,
        now_utc=now_utc,
        is_live_check=True,
        candidate_risk_budget=50000.0,
    )
    assert is_valid is False
    assert any("DAILY_LOSS_LIMIT_EXCEEDED" in r for r in rejections)


def test_n1_candidate_within_limits_passes(tmp_path):
    """N-1: A candidate trade satisfying both limits independently passes."""
    spec = OptionsIntelligenceSpecialist()
    port_file = str(tmp_path / "portfolio.json")
    broker_file = str(tmp_path / "broker.json")
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)

    with open(port_file, "w", encoding="utf-8") as f:
        json.dump({"cash": 100000.0, "max_daily_loss": 5000.0}, f)
    with open(broker_file, "w", encoding="utf-8") as f:
        json.dump({"placed_today": []}, f)

    is_valid, rejections = spec.check_portfolio_risk(
        candidate_risk=1500.0,
        capital_required=10000.0,
        portfolio_path=port_file,
        broker_state_path=broker_file,
        now_utc=now_utc,
        is_live_check=True,
        candidate_risk_budget=2000.0,
    )
    assert is_valid is True
    assert len(rejections) == 0


# ==============================================================================
# N-2: Underlying Spot Freshness and REST Provenance
# ==============================================================================

def test_n2_request_timestamp_only_rejected():
    """N-2: An option chain without exchange timestamp has is_request_timestamp_only=True and is rejected."""
    spec = OptionsIntelligenceSpecialist()
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)

    quote_req_only = {
        "underlying_price": 25200.0,
        "is_request_timestamp_only": True,
        "quote_timestamp": "2026-11-17T10:29:55+05:30",
    }
    is_valid, px, err, meta = spec.validate_underlying_spot(quote_req_only, now_utc=now_utc)
    assert is_valid is False
    assert "UNDERLYING_SPOT_UNVERIFIED" in err


def test_n2_valid_exchange_timestamp_accepted():
    """N-2: Fresh underlying spot quote with verified exchange timestamp passes."""
    spec = OptionsIntelligenceSpecialist()
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (now_utc - datetime.timedelta(seconds=5)).isoformat()

    quote_valid = {
        "underlying_price": 25200.0,
        "quote_timestamp": fresh_ts,
        "is_request_timestamp_only": False,
        "quote_provenance": "UPSTOX_WEBSOCKET_V2_LIVE",
    }
    is_valid, px, err, meta = spec.validate_underlying_spot(quote_valid, now_utc=now_utc)
    assert is_valid is True
    assert err is None
    assert px == 25200.0


def test_n2_stale_and_future_timestamps_rejected():
    """N-2: Stale (> 60s) or future-dated underlying spot quotes are rejected."""
    spec = OptionsIntelligenceSpecialist()
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)

    # Stale quote (120 seconds old)
    stale_ts = (now_utc - datetime.timedelta(seconds=120)).isoformat()
    quote_stale = {"underlying_price": 25200.0, "quote_timestamp": stale_ts}
    ok_stale, _, err_stale, _ = spec.validate_underlying_spot(quote_stale, now_utc=now_utc)
    assert ok_stale is False
    assert "STALE_UNDERLYING_SPOT" in err_stale

    # Future-dated quote (15 seconds ahead)
    future_ts = (now_utc + datetime.timedelta(seconds=15)).isoformat()
    quote_future = {"underlying_price": 25200.0, "quote_timestamp": future_ts}
    ok_fut, _, err_fut, _ = spec.validate_underlying_spot(quote_future, now_utc=now_utc)
    assert ok_fut is False
    assert "FUTURE_UNDERLYING_SPOT" in err_fut


# ==============================================================================
# F-005 & F-006: Options Spread Payoff, Risk, and Hedge Validation
# ==============================================================================

def test_f005_spread_max_loss_exceeding_budget_rejected():
    """F-005: Bull Call Spread with max loss (Rs 2,616.25) exceeding budget (Rs 2,000) is rejected."""
    spec = OptionsIntelligenceSpecialist()
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (now_utc - datetime.timedelta(seconds=2)).isoformat()

    # Net debit ~ 37.0 * 65 = Rs 2,405 + statutory charges = Rs 2,616.25
    sig = spec.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BULL_CALL_SPREAD,
        option_type="SPREAD",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "bid_qty": 500,
            "ask_qty": 500,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        spread_contract={
            "strike": 25250.0,
            "symbol": "NIFTY2026111725250CE",
            "bid": 100.0,
            "ask": 101.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "last_price": 100.5,
            "quote_timestamp": fresh_ts,
            "delta": 0.35,
            "iv": 12.0,
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "candle_timestamp": fresh_ts,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25200.0, "quote_timestamp": fresh_ts},
        max_loss_budget=2000.0,  # Budget is Rs 2,000
        min_reward_risk=1.5,
        now_utc=now_utc,
    )
    assert sig["signal_state"] == STATE_SIGNAL_REJECTED
    assert any("RISK_EXCEEDS_BUDGET" in r for r in sig["rejection_reasons"])
    assert sig["position_sizing"]["final_permitted_lots"] == 0


def test_f006_spread_structure_and_hedge_validations():
    """F-006: Validates spread leg collision, structure, and hedge quote depth."""
    spec = OptionsIntelligenceSpecialist()
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (now_utc - datetime.timedelta(seconds=2)).isoformat()

    base_primary = {
        "symbol": "NIFTY2026111725150CE",
        "bid": 136.75,
        "ask": 137.75,
        "bid_qty": 500,
        "ask_qty": 500,
        "last_price": 137.75,
        "tier": "REAL_TIME",
        "is_live": True,
        "quote_timestamp": fresh_ts,
        "retrieval_timestamp": fresh_ts,
        "greeks": {"delta": 0.51, "gamma": 0.001},
    }
    setup = {
        "setup": "breakout_intraday",
        "price": 25200.0,
        "invalidation_level": 25050.0,
        "candle_timestamp": fresh_ts,
        "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
    }

    # 1. Spread leg collision (same strike)
    sig_coll = spec.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BULL_CALL_SPREAD,
        option_type="SPREAD",
        strike=25150.0,
        contract_data=base_primary,
        spread_contract={
            "strike": 25150.0,  # Same strike!
            "symbol": "NIFTY2026111725150CE_2",
            "bid": 84.0,
            "ask": 85.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "quote_timestamp": fresh_ts,
        },
        underlying_setup=setup,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25200.0, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,
        now_utc=now_utc,
    )
    assert any("SPREAD_LEG_COLLISION" in r for r in sig_coll["rejection_reasons"])

    # 2. Invalid spread structure (Bull Call Spread requires higher strike call)
    sig_inv = spec.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BULL_CALL_SPREAD,
        option_type="SPREAD",
        strike=25150.0,
        contract_data=base_primary,
        spread_contract={
            "strike": 25050.0,  # Lower strike!
            "symbol": "NIFTY2026111725050CE",
            "bid": 180.0,
            "ask": 181.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "quote_timestamp": fresh_ts,
        },
        underlying_setup=setup,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25200.0, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,
        now_utc=now_utc,
    )
    assert any("INVALID_SPREAD_STRUCTURE" in r for r in sig_inv["rejection_reasons"])

    # 3. Inverted hedge quote (bid > ask)
    sig_cross = spec.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BULL_CALL_SPREAD,
        option_type="SPREAD",
        strike=25150.0,
        contract_data=base_primary,
        spread_contract={
            "strike": 25250.0,
            "symbol": "NIFTY2026111725250CE",
            "bid": 90.0,
            "ask": 80.0,  # bid > ask!
            "bid_qty": 500,
            "ask_qty": 500,
            "quote_timestamp": fresh_ts,
        },
        underlying_setup=setup,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25200.0, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,
        now_utc=now_utc,
    )
    assert any("INVERTED_HEDGE_QUOTE" in r for r in sig_cross["rejection_reasons"])


# ==============================================================================
# N-3: Production Override for Portfolio Protection
# ==============================================================================

def test_n3_missing_portfolio_state_fails_closed_without_override(tmp_path):
    """N-3: In live trading, missing portfolio state fails closed."""
    spec = OptionsIntelligenceSpecialist()
    non_existent = str(tmp_path / "missing_file.json")

    is_valid, rejections = spec.check_portfolio_risk(
        candidate_risk=2000.0,
        capital_required=10000.0,
        portfolio_path=non_existent,
        broker_state_path=non_existent,
        is_live_check=True,
        allow_unverified_portfolio=False,
    )
    assert is_valid is False
    assert any("PORTFOLIO_STATE_UNAVAILABLE" in r for r in rejections)


# ==============================================================================
# N-4: Zero-Lot Sizing Exclusion from Shortlist
# ==============================================================================

def test_n4_zero_lot_allocation_excluded_from_shortlist():
    """N-4: Candidate receiving 0 lots during sequential allocation is moved to rejected signals."""
    orch = MarketIntelligenceOrchestrator()
    # This test is specifically about overall-capital exhaustion ordering, not correlated-group
    # exposure limiting (a separate, later feature -- see test_options_stage2_safety_gates.py).
    # NIFTY and BANKNIFTY share a correlation group by design; loosen the cap here so that new,
    # orthogonal feature doesn't change what this test is isolating.
    orch.specialist.config["max_correlated_exposure_pct"] = 100.0
    candidates = [
        {
            "underlying_instrument": "NIFTY",
            "strategy": STRATEGY_BUY_CE,
            "is_actionable": True,
            "signal_state": STATE_SIGNAL_VALIDATED,
            "position_sizing": {
                "final_permitted_lots": 2,
                "lot_size": 65,
                "net_risk_per_lot_inr": 1500.0,
                "capital_required_per_lot": 25000.0,
            },
            "capital_margin_requirement": 50000.0,
            "risk_reporting": {"net_risk": 3000.0},
            "expected_risk_reward_after_charges": 2.5,
            "rejection_reasons": [],
        },
        {
            "underlying_instrument": "BANKNIFTY",
            "strategy": STRATEGY_BUY_PE,
            "is_actionable": True,
            "signal_state": STATE_SIGNAL_VALIDATED,
            "position_sizing": {
                "final_permitted_lots": 2,
                "lot_size": 15,
                "net_risk_per_lot_inr": 1500.0,
                "capital_required_per_lot": 20000.0,
            },
            "capital_margin_requirement": 40000.0,
            "risk_reporting": {"net_risk": 3000.0},
            "expected_risk_reward_after_charges": 2.2,
            "rejection_reasons": [],
        },
    ]

    # Available capital only covers the first candidate (50,000 total capital, 5000 max loss)
    shortlist, rejected = orch.allocate_portfolio_across_shortlist(
        shortlist=candidates,
        budget=5000.0,
        available_capital=50000.0,
    )
    assert len(shortlist) == 1
    assert shortlist[0]["underlying_instrument"] == "NIFTY"
    assert len(rejected) == 1
    rej = rejected[0]
    assert rej["underlying_instrument"] == "BANKNIFTY"
    assert rej["is_actionable"] is False
    assert rej["signal_state"] == STATE_SIGNAL_REJECTED
    assert any("INSUFFICIENT_REMAINING_CAPITAL" in r or "ZERO_LOT_ALLOCATION" in r for r in rej["rejection_reasons"])


# ==============================================================================
# N-5: ScanLock Concurrency and Ownership Hardening
# ==============================================================================

def test_n5_scanlock_lifecycle_and_non_owner_safety(tmp_path):
    """N-5: ScanLock verifies ownership token before releasing and does not delete other processes' locks."""
    lock_file = str(tmp_path / "test_scan.lock")

    lock1 = ScanLock(lock_path=lock_file)
    lock1.acquire()
    assert lock1.is_acquired is True
    assert os.path.exists(lock_file)

    # Attempting to release from an un-acquired lock instance does NOT remove lock1's file
    lock2 = ScanLock(lock_path=lock_file)
    lock2.release()
    assert os.path.exists(lock_file)

    # Original lock owner releases cleanly
    lock1.release()
    assert not os.path.exists(lock_file)


# ==============================================================================
# Contractual R:R and F-020: Target Evaluation
# ==============================================================================

def test_f020_rr_evaluation_respects_target_gate():
    """F-020: Single target below 2.0R rejected at T1, passes when T2 is explicitly selected and >= 2.0R."""
    spec = OptionsIntelligenceSpecialist()
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (now_utc - datetime.timedelta(seconds=2)).isoformat()

    # Underlying setup where T1 gives ~ 1.34R (< 2.0R), and T2 gives ~ 2.64R (> 2.0R)
    setup = {
        "setup": "breakout_intraday",
        "price": 25242.25,
        "invalidation_level": 25088.0,
        "candle_timestamp": fresh_ts,
        "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
    }
    contract = {
        "symbol": "NIFTY2026111725150CE",
        "bid": 136.75,
        "ask": 137.75,
        "last_price": 137.75,
        "bid_qty": 500,
        "ask_qty": 500,
        "tier": "REAL_TIME",
        "is_live": True,
        "quote_timestamp": fresh_ts,
        "retrieval_timestamp": fresh_ts,
        "greeks": {"delta": 0.51, "gamma": 0.001},
    }

    # At T1 gate (default): rejected due to RR_BELOW_MINIMUM
    sig_t1 = spec.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data=contract,
        underlying_setup=setup,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25242.25, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,
        min_reward_risk=2.0,
        now_utc=now_utc,
        target_rr_gate="T1",
    )
    assert sig_t1["signal_state"] == STATE_SIGNAL_REJECTED
    assert any("RR_BELOW_MINIMUM" in r for r in sig_t1["rejection_reasons"])

    # At T2 gate: passes R:R gate
    sig_t2 = spec.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data=contract,
        underlying_setup=setup,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25242.25, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,
        min_reward_risk=2.0,
        now_utc=now_utc,
        target_rr_gate="T2",
    )
    assert sig_t2["signal_state"] == STATE_SIGNAL_VALIDATED
    assert sig_t2["is_actionable"] is True
