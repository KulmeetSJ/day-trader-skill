"""
Acceptance Test Suite for Options Intelligence Specialist (trader-options).

Tests all 11 core product requirements and acceptance criteria (A through K):
A. Bullish NIFTY setup -> validated BUY CE signal.
B. Bearish NIFTY setup -> validated BUY PE signal.
C. Bearish/sideways setup -> SELL CE evaluation with tail-risk disclosure.
D. Bullish/sideways setup -> SELL PE evaluation with margin and exposure.
E. Defined-risk spread evaluation (Bull Call Spread).
F. Missing/stale option-chain data -> NO TRADE.
G. Insufficient liquidity or excessive bid/ask spread -> rejection.
H. Invalid contract, expiry or lot-size metadata -> rejection.
I. Complete statutory Indian risk calculation including charges and maximum loss.
J. Natural-language request orchestrating scanner, underlying analysis, and options specialist.
K. Multiple simultaneous opportunities ranked without duplicate recommendations.
"""

import datetime
import json
import math
import pytest
import sys
import os
import time

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

import _load
import calendar_in
import data_in
import instruments
import scan
import upstox_feed
from options import (
    OptionsIntelligenceSpecialist,
    STRATEGY_BUY_CE,
    STRATEGY_BUY_PE,
    STRATEGY_SELL_CE,
    STRATEGY_SELL_PE,
    STRATEGY_BULL_CALL_SPREAD,
    STATE_SIGNAL_VALIDATED,
)
from orchestrator import MarketIntelligenceOrchestrator
from replay import OptionsTimeMachineReplay


@pytest.fixture
def specialist():
    return OptionsIntelligenceSpecialist()


@pytest.fixture
def orchestrator():
    return MarketIntelligenceOrchestrator()


# ---------------------------------------------------------------- Test A: Bullish NIFTY -> BUY CE
def test_acceptance_criteria_a_bullish_nifty_buy_ce(specialist):
    """A. Bullish NIFTY setup resulting in a validated BUY CE signal."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"
    assert res["pcr"] > 0
    assert res["atm_strike"] > 0

    buy_ce_signals = [s for s in res["signals"] if s["strategy"] == STRATEGY_BUY_CE]
    assert len(buy_ce_signals) >= 1
    sig = buy_ce_signals[0]

    assert sig["underlying_instrument"] == "NIFTY"
    assert sig["direction"] == "BULLISH"
    assert sig["option_type"] == "CE"
    # Off-hours historical data must not produce live SIGNAL VALIDATED (Phase 3 gating)
    assert sig["signal_state"] in ("HISTORICAL ANALYSIS", "SIGNAL REJECTED")
    assert sig["position_sizing"]["lot_size"] == 65

    # With verified live data and adequate budget during open market session, verifies SIGNAL VALIDATED
    mock_open_market = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)  # Tuesday 10:30 IST
    live_sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:29:45+05:30",
            "retrieval_timestamp": "2026-11-17T10:29:50+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25240.0,
            "invalidation_level": 25100.0,
            "candle_timestamp": "2026-11-17T10:25:00+05:30",
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=mock_open_market,
    )
    assert live_sig["signal_state"] == STATE_SIGNAL_VALIDATED
    assert live_sig["is_actionable"] is True
    assert live_sig["current_premium"] > 0
    assert live_sig["stop_loss"] < live_sig["current_premium"]
    assert live_sig["target_1"] > live_sig["current_premium"]
    assert live_sig["expected_risk_reward_after_charges"] >= 2.0


# ---------------------------------------------------------------- Test B: Bearish NIFTY -> BUY PE
def test_acceptance_criteria_b_bearish_nifty_buy_pe(specialist):
    """B. Bearish NIFTY setup resulting in a validated BUY PE signal."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"

    buy_pe_signals = [s for s in res["signals"] if s["strategy"] == STRATEGY_BUY_PE]
    assert len(buy_pe_signals) >= 1
    sig = buy_pe_signals[0]

    assert sig["underlying_instrument"] == "NIFTY"
    assert sig["direction"] == "BEARISH"
    assert sig["option_type"] == "PE"
    assert sig["signal_state"] in ("HISTORICAL ANALYSIS", "SIGNAL REJECTED")
    assert sig["position_sizing"]["lot_size"] == 65

    # With verified live data and adequate budget during open market session, verifies SIGNAL VALIDATED
    mock_open_market = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    live_sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH",
        strategy=STRATEGY_BUY_PE,
        option_type="PE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150PE",
            "bid": 136.60,
            "ask": 137.60,
            "last_price": 137.60,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:29:45+05:30",
            "retrieval_timestamp": "2026-11-17T10:29:50+05:30",
            "greeks": {"delta": -0.49, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "price": 25140.0,
            "invalidation_level": 25280.0,
            "candle_timestamp": "2026-11-17T10:25:00+05:30",
            "targets": {"target_1_1r": 24830.0, "target_2_2r": 24630.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=mock_open_market,
    )
    assert live_sig["signal_state"] == STATE_SIGNAL_VALIDATED
    assert live_sig["is_actionable"] is True
    assert live_sig["current_premium"] > 0
    assert live_sig["stop_loss"] < live_sig["current_premium"]
    assert live_sig["target_1"] > live_sig["current_premium"]
    assert live_sig["expected_risk_reward_after_charges"] >= 2.0


# ---------------------------------------------------------------- Test C: Bearish/Sideways -> SELL CE
def test_acceptance_criteria_c_bearish_sideways_sell_ce(specialist):
    """C. Bearish/sideways setup resulting in a SELL CE evaluation."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"

    sell_ce_signals = [s for s in res["signals"] if s["strategy"] == STRATEGY_SELL_CE]
    assert len(sell_ce_signals) >= 1
    sig = sell_ce_signals[0]

    assert sig["underlying_instrument"] == "NIFTY"
    assert sig["strategy"] == STRATEGY_SELL_CE
    assert sig["maximum_loss"] == "THEORETICALLY UNLIMITED"
    assert sig["margin_details"]["is_undefined_risk"] is True
    assert "UNLIMITED" in sig["margin_details"]["tail_risk"]
    assert sig["capital_margin_requirement"] > 100000.0  # SPAN + Exposure Margin check


# ---------------------------------------------------------------- Test D: Bullish/Sideways -> SELL PE
def test_acceptance_criteria_d_bullish_sideways_sell_pe(specialist):
    """D. Bullish/sideways setup resulting in a SELL PE evaluation."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"

    sell_pe_signals = [s for s in res["signals"] if s["strategy"] == STRATEGY_SELL_PE]
    assert len(sell_pe_signals) >= 1
    sig = sell_pe_signals[0]

    assert sig["underlying_instrument"] == "NIFTY"
    assert sig["strategy"] == STRATEGY_SELL_PE
    assert sig["margin_details"]["is_undefined_risk"] is False
    assert sig["margin_details"]["structure"] == "CAPPED_MAX_LOSS_NAKED"
    assert sig["capital_margin_requirement"] > 100000.0
    assert "SUBSTANTIAL MAXIMUM LOSS" in sig["margin_details"]["tail_risk"]


# ---------------------------------------------------------------- Test E: Defined-Risk Spread
def test_acceptance_criteria_e_defined_risk_spread(specialist):
    """E. Defined-risk spread evaluation."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"

    spread_signals = [s for s in res["signals"] if s["strategy"] == STRATEGY_BULL_CALL_SPREAD]
    assert len(spread_signals) >= 1
    sig = spread_signals[0]

    assert sig["option_type"] == "SPREAD"
    assert sig["margin_details"]["is_undefined_risk"] is False
    assert sig["margin_details"]["structure"] == "DEFINED_RISK_SPREAD"
    assert "capped" in sig["margin_details"]["tail_risk"]


# ---------------------------------------------------------------- Test F: Missing/Stale Chain -> NO TRADE
def test_acceptance_criteria_f_missing_stale_chain_no_trade(specialist, monkeypatch):
    """F. Missing/stale option-chain data resulting in NO TRADE."""
    # Mock chain fetch to return None (simulating API failure or missing subscription)
    monkeypatch.setattr(specialist, "fetch_normalized_chain", lambda *args, **kwargs: None)

    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "NO_TRADE"
    assert "STALE_OR_MISSING_DATA" in res["reason"]
    assert len(res["signals"]) == 0


# ---------------------------------------------------------------- Test G: Liquidity & Spread Rejection
def test_acceptance_criteria_g_insufficient_liquidity_rejection(specialist):
    """G. Insufficient liquidity or excessive bid/ask spread resulting in rejection."""
    # Illiquid contract: zero bid
    contract_zero_bid = {"bid": 0.0, "ask": 10.0, "last_price": 5.0, "volume": 1000}
    ok, err, _ = specialist.validate_liquidity(contract_zero_bid)
    assert ok is False
    assert "Zero or missing bid" in err

    # Wide spread contract (15% spread > 6% limit)
    contract_wide_spread = {"bid": 100.0, "ask": 120.0, "last_price": 110.0, "volume": 5000}
    ok, err, _ = specialist.validate_liquidity(contract_wide_spread)
    assert ok is False
    assert "spread too wide" in err

    # Low volume contract (< 500 threshold)
    contract_low_vol = {"bid": 100.0, "ask": 102.0, "last_price": 101.0, "volume": 50}
    ok, err, _ = specialist.validate_liquidity(contract_low_vol, min_volume=500)
    assert ok is False
    assert "below liquidity threshold" in err


# ---------------------------------------------------------------- Test H: Invalid Metadata Rejection
def test_acceptance_criteria_h_invalid_metadata_rejection(specialist):
    """H. Invalid contract, expiry or lot-size metadata resulting in rejection."""
    # Non-F&O symbol (e.g. ZOMATO not in F&O)
    ok, meta, err = specialist.validate_metadata("ZOMATO")
    assert ok is False
    assert "not eligible for F&O" in err

    # Malformed symbol
    ok_sym, _, err_sym = specialist.validate_metadata("INVALID_TICKER_FORMAT")
    assert ok_sym is False
    assert "error" in err_sym.lower()

    # Invalid expiry date
    ok, meta, err = specialist.validate_metadata("NIFTY", expiry="1999-01-01")
    assert ok is False
    assert "not in authoritative expiries" in err

    # Invalid strike interval (e.g. NIFTY strike 25123.45 on a 50-point grid)
    ok, meta, err = specialist.validate_metadata("NIFTY", strike=25123.45)
    assert ok is False
    assert "not a valid interval" in err


# ---------------------------------------------------------------- Test I: Risk & Statutory Charges
def test_acceptance_criteria_i_risk_and_statutory_charges(specialist):
    """I. Risk calculation including charges and maximum loss."""
    res = specialist.calculate_charges_and_net_rr(
        strategy=STRATEGY_BUY_CE,
        entry_prem=100.0,
        stop_prem=80.0,
        t1_prem=150.0,
        qty=65,
        lot_size=65,
        strike=25000.0,
        option_type="CE",
    )

    charges = res["charges_breakdown"]
    assert charges["brokerage_roundtrip"] == 40.0
    assert charges["stt"] > 0.0
    assert charges["exchange_charges"] > 0.0
    assert charges["gst"] > 0.0
    assert res["net_profit_t1"] < res["gross_profit_t1"]
    assert res["net_loss_stop"] > res["gross_loss_stop"]
    assert res["net_reward_risk"] > 0.0


# ---------------------------------------------------------------- Test J: Natural Language Orchestrator
def test_acceptance_criteria_j_natural_language_orchestrator(orchestrator):
    """J. Natural-language request orchestrating scanner, underlying analysis and options specialist."""
    # Test broad scan query
    res_scan = orchestrator.run_market_intelligence("Scan the market for the best intraday opportunities.", top_n=3)
    assert res_scan["total_evaluated"] >= 3
    assert len(res_scan["shortlist"]) <= 3
    assert "# INTRADAY TRADING INTELLIGENCE REPORT" in res_scan["report_markdown"]
    assert "Human Trader Execution Checklist" in res_scan["report_markdown"]

    # Test focused option-selling query
    res_selling = orchestrator.run_market_intelligence("Find BANKNIFTY option-selling setups.", top_n=3)
    assert res_selling["total_evaluated"] == 1
    assert "BANKNIFTY" in res_selling["underlying_summaries"]
    for sig in res_selling["shortlist"]:
        assert "SELL" in sig["strategy"] or "SPREAD" in sig["strategy"]


# ---------------------------------------------------------------- Test K: Ranking & Deduplication
def test_acceptance_criteria_k_ranking_and_deduplication(specialist):
    """K. Multiple simultaneous opportunities ranked without duplicate recommendations."""
    # Create duplicate and distinct mock signals
    sig1 = {
        "underlying_instrument": "NIFTY",
        "strategy": STRATEGY_BUY_CE,
        "strike": 25000.0,
        "expected_risk_reward_after_charges": 2.5,
        "margin_details": {"is_undefined_risk": False},
        "data_quality": {"is_live": True},
    }
    sig1_duplicate = dict(sig1)
    sig2 = {
        "underlying_instrument": "BANKNIFTY",
        "strategy": STRATEGY_BUY_PE,
        "strike": 61000.0,
        "expected_risk_reward_after_charges": 1.5,
        "margin_details": {"is_undefined_risk": False},
        "data_quality": {"is_live": False, "tier": "HISTORICAL"},
    }
    sig3 = {
        "underlying_instrument": "NIFTY",
        "strategy": STRATEGY_SELL_CE,
        "strike": 25200.0,
        "expected_risk_reward_after_charges": 1.2,
        "margin_details": {"is_undefined_risk": True},
        "data_quality": {"is_live": False, "tier": "HISTORICAL"},
    }

    raw_signals = [sig1, sig1_duplicate, sig2, sig3]
    ranked = specialist.rank_opportunities(raw_signals)

    # Asserts
    assert len(ranked) == 3  # Deduplicated from 4 to 3
    assert ranked[0]["underlying_instrument"] == "NIFTY"
    assert ranked[0]["strategy"] == STRATEGY_BUY_CE  # Highest score due to 2.5 R:R and live tier
    assert ranked[0]["ranking_score"] > ranked[1]["ranking_score"]


# ---------------------------------------------------------------- Test L: Historical TimeMachine Replay
def test_options_timemachine_historical_validation(specialist):
    """Verifies strategy-specific historical replay for actual implemented options logic."""
    rep = OptionsTimeMachineReplay(specialist=specialist)
    chain = specialist.fetch_normalized_chain("NIFTY")
    meta = instruments.resolve_underlying_derivatives_meta("NIFTY")

    # Sample session bars
    bars = [
        {"o": 25100.0, "h": 25120.0, "l": 25090.0, "c": 25110.0, "v": 1000},
        {"o": 25110.0, "h": 25130.0, "l": 25100.0, "c": 25125.0, "v": 1200},
        {"o": 25125.0, "h": 25150.0, "l": 25120.0, "c": 25145.0, "v": 1500},
        {"o": 25145.0, "h": 25180.0, "l": 25140.0, "c": 25175.0, "v": 2000},
        {"o": 25175.0, "h": 25210.0, "l": 25170.0, "c": 25205.0, "v": 2500},
        {"o": 25205.0, "h": 25230.0, "l": 25200.0, "c": 25220.0, "v": 1800},
    ]

    res_ce = rep.replay_session("NIFTY", STRATEGY_BUY_CE, bars, chain, meta, slippage_pct=0.5)
    assert res_ce["strategy"] == STRATEGY_BUY_CE
    assert res_ce["trade_count"] >= 1
    assert "win_rate_pct" in res_ce
    assert "profit_factor" in res_ce
    assert "expectancy_inr" in res_ce
    assert "max_drawdown_inr" in res_ce
    assert "total_statutory_charges_inr" in res_ce
    assert "slippage_assumptions" in res_ce
    assert "data_coverage" in res_ce
    assert res_ce["total_statutory_charges_inr"] > 0.0

    res_pe = rep.replay_session("NIFTY", STRATEGY_BUY_PE, bars, chain, meta, slippage_pct=0.5)
    assert res_pe["strategy"] == STRATEGY_BUY_PE
    assert "win_rate_pct" in res_pe
    assert "slippage_assumptions" in res_pe


# ---------------------------------------------------------------- Phase 1-7 Forensic Regression Tests

def test_phase1_audit_sample_rejection_5164_risk_vs_2000_budget(specialist):
    """P0: ₹5,164 risk with ₹2,000 budget must be rejected (never force 1 lot)."""
    # Sample BUY CE from forensic audit: Entry 137.75, Stop 59.20, Lot size 65
    charges = specialist.calculate_charges_and_net_rr(
        STRATEGY_BUY_CE,
        entry_prem=137.75,
        stop_prem=59.20,
        t1_prem=262.0,
        qty=65,
        lot_size=65,
        strike=25150.0,
        option_type="CE",
    )
    assert charges["net_loss_stop"] == 5164.37  # Exact net risk after Indian charges

    # Evaluate contract with budget = 2000.0
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25088.0,
            "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=2000.0,
        min_reward_risk=2.0,
    )

    # Must be strictly rejected
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert sig["position_sizing"]["lots"] == 0
    assert any("RISK_EXCEEDS_BUDGET" in r for r in sig["rejection_reasons"])


def test_phase1_audit_sample_rejection_low_rr(specialist):
    """P0: 1.58R (or < 2.0R) with 2.0R minimum must be rejected."""
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25088.0,
            "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=10000.0,  # Adequate budget
        min_reward_risk=2.0,       # 2.0R required
    )
    # Target in underlying setup yields ~1.86R, which is below 2.0R
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("RR_BELOW_MINIMUM" in r for r in sig["rejection_reasons"])


def test_phase1_zero_risk_rejection(specialist):
    """P0: Zero or negative risk must fail closed and be rejected."""
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 137.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25200.0,  # Identical to entry: zero risk
            "targets": {"target_1_1r": 25300.0, "target_2_2r": 25400.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=5000.0,
        min_reward_risk=2.0,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["position_sizing"]["lots"] == 0


def test_phase2_missing_underlying_chart(specialist):
    """P0: Missing or invalid underlying chart data returns NO TRADE without fabricating setups."""
    empty_chart = {"symbol": "NIFTY", "bars": []}
    res = specialist.analyze_symbol_for_options("NIFTY", underlying_chart=empty_chart)
    assert res["status"] == "NO_TRADE"
    assert "MISSING_UNDERLYING_CHART" in res["reason"]
    assert len(res["signals"]) == 0


def test_phase3_historical_cannot_be_signal_validated(specialist):
    """P0: Historical/closed data can never produce SIGNAL VALIDATED for live execution."""
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "HISTORICAL",  # Not real time
            "is_live": False,      # Closed session
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=20000.0,
        min_reward_risk=1.5,
    )
    # Must be HISTORICAL ANALYSIS, NEVER SIGNAL VALIDATED
    assert sig["signal_state"] == "HISTORICAL ANALYSIS"
    assert sig["is_actionable"] is False


def test_phase4_premium_risk_model_accounts_for_theta_and_vega(specialist):
    """Phase 4: Premium levels calculate Theta decay and Vega shifts, returning None if invalid."""
    # Normal viable levels
    levels = specialist.model_premium_levels(
        entry_premium=100.0,
        spot_entry=25000.0,
        spot_stop=24900.0,
        spot_t1=25200.0,
        spot_t2=25400.0,
        delta=0.50,
        gamma=0.001,
        theta=-3.0,
        vega=12.0,
        holding_hours=2.0,
        option_type="CE",
        is_seller=False,
    )
    assert levels is not None
    assert levels["stop_loss_premium"] < levels["entry_premium"]
    assert levels["target_1_premium"] > levels["entry_premium"]
    assert levels["theta_decay_applied"] > 0.0

    # Non-viable levels (stop above entry for buyer) returns None, never arbitrary fallback
    invalid_levels = specialist.model_premium_levels(
        entry_premium=100.0,
        spot_entry=25000.0,
        spot_stop=25100.0,  # Invalid: stop is above entry for call buyer!
        spot_t1=24900.0,
        spot_t2=24800.0,
        delta=0.50,
        gamma=0.001,
        option_type="CE",
        is_seller=False,
    )
    assert invalid_levels is None


def test_phase5_selling_heuristic_margin_and_two_leg_spread(specialist):
    """Phase 5: Selling margin labeled HEURISTIC ESTIMATE; spreads have 2 explicit legs."""
    # Selling margin label
    m_info = specialist.evaluate_margin_and_capital(
        strategy=STRATEGY_SELL_CE,
        strike=25000.0,
        lot_size=65,
        entry_prem=100.0,
        lots=1,
    )
    assert "HEURISTIC_ESTIMATE" in m_info["margin_model"]
    assert m_info["is_undefined_risk"] is True

    # Two-leg spread payload
    spread_sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BULL_CALL_SPREAD,
        option_type="SPREAD",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        spread_contract={
            "strike": 25250.0,
            "symbol": "NIFTY2026111725250CE",
            "last_price": 85.0,
            "bid": 84.0,
            "ask": 85.0,
            "delta": 0.35,
            "iv": 12.5,
        },
        max_loss_budget=10000.0,
        min_reward_risk=1.5,
    )
    assert len(spread_sig["legs"]) == 2
    assert spread_sig["legs"][0]["role"] == "PRIMARY"
    assert spread_sig["legs"][0]["side"] == "BUY"
    assert spread_sig["legs"][1]["role"] == "HEDGE"
    assert spread_sig["legs"][1]["side"] == "SELL"
    assert spread_sig["legs"][1]["strike"] == 25250.0


def test_phase6_synthetic_replay_disclosures_and_sizing(specialist):
    """Phase 6: Replay is labeled SYNTHETIC OPTIONS SIMULATION and never forces 1 lot."""
    rep = OptionsTimeMachineReplay(specialist=specialist)
    chain = specialist.fetch_normalized_chain("NIFTY")
    meta = instruments.resolve_underlying_derivatives_meta("NIFTY")

    bars = [
        {"o": 25100.0, "h": 25120.0, "l": 25090.0, "c": 25110.0, "v": 1000},
        {"o": 25110.0, "h": 25130.0, "l": 25100.0, "c": 25125.0, "v": 1200},
        {"o": 25125.0, "h": 25150.0, "l": 25120.0, "c": 25145.0, "v": 1500},
        {"o": 25145.0, "h": 25180.0, "l": 25140.0, "c": 25175.0, "v": 2000},
        {"o": 25175.0, "h": 25210.0, "l": 25170.0, "c": 25205.0, "v": 2500},
        {"o": 25205.0, "h": 25230.0, "l": 25200.0, "c": 25220.0, "v": 1800},
    ]

    # Replay with tiny budget ($50) where risk per lot (~$1,000+) exceeds budget
    res = rep.replay_session("NIFTY", STRATEGY_BUY_CE, bars, chain, meta, max_loss_budget=50.0)
    assert res["simulation_model"] == "SYNTHETIC_OPTIONS_SIMULATION"
    assert "limitations_disclosure" in res
    # With $50 budget, all trades exceeding budget are skipped (trade_count == 0)
    assert res["trade_count"] == 0


# ---------------------------------------------------------------- DEF-01 to DEF-05 Forensic Regression Tests

def test_def01_market_closed_with_mocked_is_live(specialist):
    """DEF-01: Mocked is_live=True must never bypass market session check."""
    # Saturday evening: market closed
    closed_now = datetime.datetime(2026, 11, 14, 16, 30, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,  # Mocked live flag!
            "provider": "upstox",
            "quote_timestamp": "2026-11-14T22:00:00+05:30",
            "retrieval_timestamp": "2026-11-14T22:00:05+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "candle_timestamp": "2026-11-14T21:55:00+05:30",
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=closed_now,
    )
    # Must NOT be SIGNAL VALIDATED
    assert sig["signal_state"] == "HISTORICAL ANALYSIS"
    assert sig["is_actionable"] is False
    assert any("MARKET_SESSION_CLOSED" in r for r in sig["rejection_reasons"])


def test_def01_four_day_old_quote_rejected(specialist):
    """DEF-01: Stale 4-day-old quote timestamp must be rejected even during open session."""
    open_now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)  # Tuesday 10:30 IST
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-13T10:30:00+05:30",  # 4 days old!
            "retrieval_timestamp": "2026-11-17T10:29:55+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "candle_timestamp": "2026-11-17T10:25:00+05:30",
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=open_now,
    )
    assert sig["signal_state"] == "HISTORICAL ANALYSIS"
    assert sig["is_actionable"] is False
    assert any("STALE_OPTION_QUOTE" in r for r in sig["rejection_reasons"])


def test_def01_stale_underlying_candle_rejected(specialist):
    """DEF-01: Stale underlying candle must fail freshness validation."""
    open_now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:29:50+05:30",
            "retrieval_timestamp": "2026-11-17T10:29:55+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "candle_timestamp": "2026-11-17T09:30:00+05:30",  # 1 hour stale!
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=open_now,
    )
    assert sig["signal_state"] == "HISTORICAL ANALYSIS"
    assert sig["is_actionable"] is False
    assert any("STALE_UNDERLYING_CANDLE" in r for r in sig["rejection_reasons"])


def test_def01_missing_quote_and_retrieval_timestamps_rejected(specialist):
    """DEF-01: Missing quote or retrieval timestamps must never silently default to now."""
    open_now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig_no_quote = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            # quote_timestamp omitted!
            "retrieval_timestamp": "2026-11-17T10:29:55+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "candle_timestamp": "2026-11-17T10:25:00+05:30",
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=open_now,
    )
    assert sig_no_quote["signal_state"] == "HISTORICAL ANALYSIS"
    assert any("MISSING_QUOTE_TIMESTAMP" in r for r in sig_no_quote["rejection_reasons"])


def test_def01_valid_realtime_during_open_session_approved(specialist):
    """DEF-01: Genuine real-time data during open session passes all 6 freshness checks."""
    open_now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig_ok = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:29:50+05:30",
            "retrieval_timestamp": "2026-11-17T10:29:55+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "candle_timestamp": "2026-11-17T10:25:00+05:30",
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=open_now,
    )
    assert sig_ok["signal_state"] == "SIGNAL VALIDATED"
    assert sig_ok["is_actionable"] is True
    assert sig_ok["freshness_details"]["is_market_open"] is True


def test_def02_naked_sell_pe_correct_classification(specialist):
    """DEF-02: Naked SELL PE has capped maximum loss and must not be marked UNDEFINED_RISK_NAKED."""
    # SELL CE: Unlimited risk
    m_ce = specialist.evaluate_margin_and_capital(STRATEGY_SELL_CE, strike=25000.0, lot_size=65, entry_prem=100.0, lots=1)
    assert m_ce["is_undefined_risk"] is True
    assert m_ce["structure"] == "UNDEFINED_RISK_NAKED"
    assert m_ce["max_loss"] == "THEORETICALLY UNLIMITED"
    assert "UNLIMITED" in m_ce["tail_risk"]

    # SELL PE: Capped at (Strike - Premium) * Qty
    m_pe = specialist.evaluate_margin_and_capital(STRATEGY_SELL_PE, strike=25000.0, lot_size=65, entry_prem=100.0, lots=1)
    assert m_pe["is_undefined_risk"] is False
    assert m_pe["structure"] == "CAPPED_MAX_LOSS_NAKED"
    expected_max_loss = (25000.0 - 100.0) * 65  # 1,618,500.00
    assert m_pe["max_loss_value"] == expected_max_loss
    assert "SEVERE TAIL RISK" in m_pe["tail_risk"]
    assert "HEURISTIC_ESTIMATE" in m_pe["margin_model"]


def test_def03_actual_scanner_integration(orchestrator, monkeypatch):
    """DEF-03: Natural language request executes actual scan.scan_intraday pipeline."""
    called_scanner = {}

    def mock_scan_intraday(symbols, now_utc, strategies=None, **kwargs):
        called_scanner["symbols"] = symbols
        called_scanner["strategies"] = strategies
        return {
            "hits": [
                {
                    "symbol": "NIFTY",
                    "setup": "breakout_intraday",
                    "side": "buy",
                    "decision_state": "ENTRY CONFIRMED",
                    "price": 25200.0,
                    "trigger": {"price": 25200.0, "time": "10:30 AM"},
                    "decision": {
                        "entry_reference": 25200.0,
                        "stop_loss": 25050.0,
                        "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
                    },
                    "candle_context": {"completed_candle": {"bar_end": "2026-11-17T10:25:00+05:30"}},
                    "candle_session": "2026-11-17",
                }
            ],
            "evaluated_symbols": len(symbols),
        }

    monkeypatch.setattr(scan, "scan_intraday", mock_scan_intraday)
    res = orchestrator.run_market_intelligence("Scan the market for the best intraday opportunities.", top_n=3)

    assert "symbols" in called_scanner
    assert "NIFTY" in called_scanner["symbols"]
    assert res["total_evaluated"] >= 1


def test_def04_reject_stale_session_triggers(specialist):
    """DEF-04: Stale previous-session trigger or decision_state=NO TRADE cannot be accepted."""
    # 1. Non-index equity with decision_state=NO TRADE cannot be overridden by status=triggered
    equity_stale = {
        "symbol": "RELIANCE",
        "setup": "breakout_intraday",
        "decision_state": "NO TRADE",
        "status": "triggered",  # Stale trigger!
        "sub_state": "WAITING_CONFIRMATION",
    }
    open_now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    assert specialist.is_valid_session_setup(equity_stale, open_now) is False

    # 2. Previous session candle date rejected during open session
    prev_session_trigger = {
        "symbol": "INFY",
        "setup": "breakout_intraday",
        "decision_state": "ENTRY CONFIRMED",
        "date": "2026-11-13",  # 4 days ago!
    }
    assert specialist.is_valid_session_setup(prev_session_trigger, open_now) is False

    # 3. Current session valid trigger accepted
    current_trigger = {
        "symbol": "INFY",
        "setup": "breakout_intraday",
        "decision_state": "ENTRY CONFIRMED",
        "candle_session": "2026-11-17",  # Current session date
    }
    assert specialist.is_valid_session_setup(current_trigger, open_now) is True


def test_def05_preserve_per_lot_risk_when_executable_lots_zero(specialist):
    """DEF-05: When lots=0, position_sizing preserves risk_per_lot_inr and net_risk_per_lot_inr."""
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "HISTORICAL",
            "is_live": False,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25088.0,
            "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=2000.0,  # Exceeded by per-lot net risk (~₹4,405)
        min_reward_risk=2.0,
    )
    pos = sig["position_sizing"]
    assert pos["lots"] == 0
    assert pos["actual_net_risk_inr"] == 0.0
    # Must preserve per-lot risk fields even when lots = 0
    assert pos["risk_per_lot_inr"] > 0.0
    assert pos["net_risk_per_lot_inr"] > 0.0
    assert pos["net_risk_per_lot_inr"] > pos["risk_per_lot_inr"]  # Net includes statutory charges


# ---------------------------------------------------------------- Pipeline Audit & Correction Regression Tests

def test_pipeline_margin_transparency_heuristic_label(specialist):
    """Pipeline Audit 1: Naked option selling margin is explicitly labeled HEURISTIC_MARGIN_ESTIMATE."""
    # Test SELL CE
    m_ce = specialist.evaluate_margin_and_capital(
        strategy=STRATEGY_SELL_CE,
        strike=25200.0,
        lot_size=65,
        entry_prem=114.50,
        lots=1,
    )
    assert m_ce["margin_type"] == "HEURISTIC_MARGIN_ESTIMATE"
    assert "HEURISTIC_MARGIN_ESTIMATE" in m_ce["margin_model"]
    assert "Strike × Lot Size × 13.5%" in m_ce["formula"]
    assert m_ce["is_authoritative"] is False
    assert "broker margin" in m_ce["broker_margin_disclaimer"].lower()
    expected_notional_margin = round(25200.0 * 65 * 0.135, 2)
    assert m_ce["margin_required"] == expected_notional_margin

    # Test SELL PE
    m_pe = specialist.evaluate_margin_and_capital(
        strategy=STRATEGY_SELL_PE,
        strike=25100.0,
        lot_size=65,
        entry_prem=112.80,
        lots=1,
    )
    assert m_pe["margin_type"] == "HEURISTIC_MARGIN_ESTIMATE"
    assert "HEURISTIC_MARGIN_ESTIMATE" in m_pe["margin_model"]
    assert m_pe["is_authoritative"] is False
    assert "broker margin" in m_pe["broker_margin_disclaimer"].lower()

    # Test BUY CE (Exact debit paid)
    m_buy = specialist.evaluate_margin_and_capital(
        strategy=STRATEGY_BUY_CE,
        strike=25150.0,
        lot_size=65,
        entry_prem=137.75,
        lots=1,
    )
    assert m_buy["margin_type"] == "EXACT_DEBIT_PAID"
    assert m_buy["is_authoritative"] is True


def test_pipeline_execution_price_reconciliation(specialist):
    """Pipeline Audit 2: Proposed execution price is reconciled against option chain bid/ask."""
    # BUY CE uses Ask as baseline + slippage
    sig_buy = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25100.0,
            "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    recon_buy = sig_buy["execution_reconciliation"]
    assert recon_buy["baseline_quote_type"] == "ASK"
    assert recon_buy["baseline_quote_price"] == 137.75
    assert recon_buy["slippage_applied"] == 0.05
    assert recon_buy["execution_price"] == 137.80
    assert sig_buy["current_premium"] == 137.80

    # SELL PE uses Bid as baseline - slippage
    sig_sell = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH_OR_SIDEWAYS",
        strategy=STRATEGY_SELL_PE,
        option_type="PE",
        strike=25100.0,
        contract_data={
            "symbol": "NIFTY2026111725100PE",
            "bid": 112.80,
            "ask": 113.85,
            "last_price": 113.00,
            "tier": "REAL_TIME",
            "greeks": {"delta": -0.43, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25228.5,
            "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    recon_sell = sig_sell["execution_reconciliation"]
    assert recon_sell["baseline_quote_type"] == "BID"
    assert recon_sell["baseline_quote_price"] == 112.80
    assert recon_sell["slippage_applied"] == -0.05
    assert recon_sell["execution_price"] == 112.75
    assert sig_sell["current_premium"] == 112.75

    # Crossed quotes (bid > ask) are rejected
    sig_crossed = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.00,
            "ask": 135.00,  # Inverted/crossed
            "last_price": 137.00,
            "tier": "REAL_TIME",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25100.0,
            "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    assert any("Crossed market quotes" in r for r in sig_crossed["rejection_reasons"])


def test_pipeline_data_provenance_and_auditability(specialist):
    """Pipeline Audit 3: Every report signal contains complete auditable data provenance."""
    open_now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)  # 10:30 IST Tuesday
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:29:45+05:30",
            "retrieval_timestamp": "2026-11-17T10:29:50+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25100.0,
            "candle_timestamp": "2026-11-17T10:25:00+05:30",
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        now_utc=open_now,
    )
    prov = sig["data_provenance"]
    assert prov["data_provider"] == "upstox"
    assert prov["exchange_quote_timestamp"] == "2026-11-17T10:29:45+05:30"
    assert prov["underlying_candle_timestamp"] == "2026-11-17T10:25:00+05:30"
    assert prov["retrieval_timestamp"] == "2026-11-17T10:29:50+05:30"
    assert prov["market_session_status"] == "OPEN"
    assert prov["freshness_validation_result"] == "PASSED"
    assert prov["quote_age_seconds"] <= 120.0
    assert prov["candle_age_seconds"] <= 600.0


def test_pipeline_underlying_setup_evidence(specialist):
    """Pipeline Audit 4: Every strategy candidate displays full underlying setup evidence."""
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "last_price": 137.75,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25242.25,
            "invalidation_level": 25228.5,
            "level": 25230.35,
            "status": "triggered",
            "candle_timestamp": "2026-11-17T10:25:00+05:30",
            "trigger": {"price": 25242.25, "time": "10:50 AM", "reason": "Closed above resistance"},
            "targets": {"target_1_1r": 25450.0, "target_2_2r": 25650.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    ev = sig["underlying_setup_evidence"]
    assert ev["setup_name"] == "breakout_intraday"
    assert ev["directional_bias"] == "BULLISH"
    assert ev["entry_trigger"]["price"] == 25242.25
    assert ev["entry_trigger"]["time"] == "10:50 AM"
    assert ev["invalidation_level"] == 25228.5
    assert ev["support_resistance_level"] == 25230.35
    assert ev["scanner_result"] == "triggered"
    assert "25150.0" in ev["strike_selection_rationale"]
    assert "Delta +0.51" in ev["strike_selection_rationale"]


def test_pipeline_market_closed_session_labeling(specialist):
    """Pipeline Audit 5: Off-hours analysis is explicitly labeled MARKET_CLOSED and never live actionable."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert "MARKET_CLOSED" in res["session_status"]
    assert "POST-MARKET REVIEW" in res["session_status"]
    for sig in res["signals"]:
        assert sig["is_actionable"] is False
        assert sig["signal_state"] in ("HISTORICAL ANALYSIS", "SIGNAL REJECTED")


# ---------------------------------------------------------------- Forensic Dry Run Regression Tests

def test_forensic_bullish_scanner_hit_not_assigned_to_bearish_breakdown(specialist):
    """Forensic 1: Bullish scanner hits cannot be assigned to bearish breakdown signals without chart evidence."""
    bullish_scanner_hit = {
        "symbol": "NIFTY",
        "setup": "breakout_intraday",
        "side": "buy",
        "level": 25230.0,
        "trigger_price": 25245.0,
        "invalidation_level": 25200.0,
        "status": "triggered",
        "date": "2026-11-17",
        "candle_timestamp": "2026-11-17T10:15:00+05:30",
        "trigger": {"price": 25245.0, "time": "10:15 AM", "reason": "Closed above resistance"},
    }
    # Passing bullish hit to analyze_symbol_for_options:
    # If the chart does not independently exhibit a bear breakdown, bear setups must not adopt the bull evidence
    res = specialist.analyze_symbol_for_options("NIFTY", underlying_setup=bullish_scanner_hit)
    for sig in res.get("signals", []):
        if sig["direction"] in ("BEARISH", "BEARISH_OR_SIDEWAYS"):
            ev = sig.get("underlying_setup_evidence", {})
            # Must NOT use the bullish breakout evidence for a bearish strategy
            assert ev.get("setup_name") != "breakout_intraday"
            assert ev.get("directional_bias") != "BULLISH"


def test_forensic_underlying_spot_breaching_invalidation_rejected(specialist):
    """Forensic 2: An underlying spot breaching invalidation level must not produce an active signal."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    # Spot is 25,100, which has breached invalidation level 25,150 on a bullish setup
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 137.0,
            "ask": 137.5,
            "last_price": 137.5,
            "is_live": True,
            "tier": "L1_CONFIRMED",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.50, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "side": "buy",
            "price": 25100.0,  # Below invalidation
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        now_utc=mock_open,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    assert sig["is_actionable"] is False
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("INVALIDATED_SETUP" in r for r in sig["rejection_reasons"])


def test_forensic_universe_scan_coverage_matches_reported_counts():
    """Forensic 3: Total universe scan coverage must match reported instrument counts."""
    from orchestrator import MarketIntelligenceOrchestrator
    orch = MarketIntelligenceOrchestrator()
    res = orch.run_market_intelligence(query="NIFTY", top_n=1)
    assert res["total_evaluated"] == 1
    report = res["report_markdown"]
    assert "Evaluated **1** key instruments (core scan universe out of 230 eligible F&O instruments" in report
    assert "0.4% coverage" in report


def test_forensic_margin_exceeding_capital_results_in_zero_executable_lots(specialist):
    """Forensic 4: Margin per lot exceeding available capital must result in zero executable lots."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    # Available capital is Rs 1,00,000, but 1 lot of NIFTY SELL CE requires Rs 2,21,130
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH_OR_SIDEWAYS",
        strategy=STRATEGY_SELL_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 137.0,
            "ask": 137.5,
            "last_price": 137.0,
            "is_live": True,
            "tier": "L1_CONFIRMED",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.50, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "side": "sell",
            "price": 25150.0,
            "invalidation_level": 25200.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        now_utc=mock_open,
        available_capital=100000.0,  # Below 1-lot margin
        max_loss_budget=10000.0,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    ps = sig["position_sizing"]
    assert ps["capital_permitted_lots"] == 0
    assert ps["final_permitted_lots"] == 0
    assert ps["lots"] == 0
    assert ps["total_quantity"] == 0
    assert sig["is_actionable"] is False
    assert any("INSUFFICIENT_CAPITAL" in r for r in sig["rejection_reasons"])


def test_forensic_four_tier_position_sizing_breakdown(specialist):
    """Forensic 5: Distinguish theoretical lots, risk-permitted lots, capital-permitted lots, and final permitted lots."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH_OR_SIDEWAYS",
        strategy=STRATEGY_SELL_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 137.0,
            "ask": 137.5,
            "last_price": 137.0,
            "is_live": True,
            "tier": "L1_CONFIRMED",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.50, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "side": "sell",
            "price": 25150.0,
            "invalidation_level": 25160.0,  # Tight stop -> low net risk per lot
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        now_utc=mock_open,
        available_capital=500000.0,  # Allows 2 lots of margin (2 * 220,691 = 441,382 <= 500,000)
        max_loss_budget=2000.0,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    ps = sig["position_sizing"]
    assert "theoretical_lots" in ps
    assert "risk_budget_permitted_lots" in ps
    assert "capital_permitted_lots" in ps
    assert "final_permitted_lots" in ps
    assert ps["capital_permitted_lots"] == 2
    assert ps["final_permitted_lots"] == min(ps["risk_budget_permitted_lots"], ps["capital_permitted_lots"])
    assert ps["lots"] == ps["final_permitted_lots"]


def test_forensic_previous_session_triggers_fail_validation_in_open_session(specialist):
    """Forensic 6: Previous session triggers must fail validation during open market sessions."""
    # Market open on Tuesday Nov 17, 2026
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    old_session_setup = {
        "setup": "breakout_intraday",
        "side": "buy",
        "date": "2026-11-13",  # Friday setup tested during Tuesday session
        "status": "triggered",
        "decision_state": "ENTRY CONFIRMED",
    }
    assert specialist.is_valid_session_setup(old_session_setup, mock_open) is False


def test_quote_provenance_missing_timestamps_non_executable(specialist):
    """Forensic Audit 7: Quotes without verifiable timestamps must be classified as HISTORICAL_INDICATIVE and not executable."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH",
        strategy=STRATEGY_BUY_PE,
        option_type="PE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150PE",
            "bid": 136.60,
            "ask": 137.60,
            "last_price": 137.65,
            "is_live": True,
            "tier": "REAL_TIME",
            "quote_timestamp": None,  # Missing quote timestamp
            "retrieval_timestamp": "2026-11-17T10:30:00+05:30",
            "greeks": {"delta": -0.49, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "side": "sell",
            "price": 25149.35,
            "invalidation_level": 25161.85,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        now_utc=mock_open,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    recon = sig["execution_reconciliation"]
    assert recon["is_executable"] is False
    assert recon["quote_classification"] == "HISTORICAL_INDICATIVE"
    assert "NOT executable" in recon["executable_note"]
    assert sig["is_actionable"] is False
    assert any("MISSING_QUOTE_TIMESTAMP" in r for r in sig["rejection_reasons"])


def test_risk_reporting_stop_loss_vs_maximum_loss_distinction(specialist):
    """Forensic Audit 8: Distinguish stop-loss risk from maximum possible loss (including premium and transaction costs)."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH",
        strategy=STRATEGY_BUY_PE,
        option_type="PE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150PE",
            "bid": 136.60,
            "ask": 137.60,
            "last_price": 137.65,
            "is_live": True,
            "tier": "REAL_TIME",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": -0.49, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "side": "sell",
            "price": 25149.35,
            "invalidation_level": 25161.85,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        now_utc=mock_open,
        max_loss_budget=2000.0,
        available_capital=100000.0,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    ps = sig["position_sizing"]
    assert ps["lots"] >= 1
    assert ps["actual_net_risk_inr"] <= 2000.0  # Stop-loss risk fits inside budget
    assert ps["total_stop_loss_risk_inr"] == ps["actual_net_risk_inr"]
    # Maximum possible loss includes premium outlay + transaction charges.
    # Stage 2: max_option_lots (config default 2) is now enforced as a hard sizing cap (it was
    # previously dead configuration), so the uncapped ~4-lot figure this test originally pinned
    # (~Rs 26,841.75) is no longer reachable with the default config -- the thresholds below
    # reflect the now-correctly-capped ~2-lot outcome.
    assert "Premium Outlay" in sig["maximum_loss"]
    assert "Transaction Charges" in sig["maximum_loss"]
    assert ps["maximum_possible_loss_value"] > 15000.0
    # Sizing satisfies stop risk budget while maximum possible loss is still substantially higher
    assert ps["maximum_possible_loss_value"] > (8 * ps["budget_max_loss_inr"])
    assert "substantially higher" in ps["risk_budget_policy_explanation"]


def test_underlying_setup_candle_provenance_and_validity_state(specialist):
    """Forensic Audit 9: Trace breakdown_intraday evidence back to actual underlying candles and determine validity state."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"
    pe_sigs = [s for s in res["signals"] if s["strategy"] == STRATEGY_BUY_PE]
    assert len(pe_sigs) >= 1
    sig = pe_sigs[0]
    ev = sig["underlying_setup_evidence"]

    # Trigger price must match actual candle breakdown level (25149.35), not closing spot
    assert ev["setup_name"] == "breakdown_intraday"
    assert ev["trigger_price"] == 25149.35
    assert ev["trigger_time"] == "11:50 AM"
    assert "below intraday support" in ev["trigger_reason"]
    # Latest candle spot is closing bar (25144.30 @ 15:30)
    assert ev["current_spot"] == 25144.30
    assert "15:30" in ev["latest_candle_time"]
    assert ev["invalidation_level"] == 25161.85
    # Historical off-hours trigger must be marked HISTORICAL_TRIGGER_INACTIVE
    assert ev["setup_validity_state"] == "HISTORICAL_TRIGGER_INACTIVE"


# ---------------------------------------------------------------- WebSocket V2 Market Data Feed Tests


class _MockWS:
    def __init__(self, frames=None):
        self.frames = list(frames or [])
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout=None):
        if self.frames:
            return self.frames.pop(0)
        return None

    def close(self):
        self.closed = True


def _make_streamer(frames=None, ws_client=None):
    client = ws_client or _MockWS(frames=frames)
    s = upstox_feed.UpstoxMarketDataStreamer(
        authorize_fn=lambda: "wss://wsfeeder-api.upstox.com/market-data-feeder/v3",
        ws_connect=lambda u: client,
        stale_after_seconds=30.0,
    )
    s.connect()
    return s, client


def test_websocket_live_option_quote_success():
    """Verify live option tick parsing, exchange timestamping, and OptionChain enrichment."""
    streamer, _ = _make_streamer()
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    mock_now_utc = datetime.datetime(2026, 11, 17, 5, 0, 5, tzinfo=datetime.timezone.utc)
    ts_ms = int(mock_now_utc.timestamp() * 1000)

    frame = {
        "type": "live_feed",
        "currentTs": ts_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.50, "ltt": ts_ms, "ltq": 65, "cp": 137.65},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {
                            "bidAskQuote": [{"bidP": 145.00, "bidQ": 65, "askP": 146.00, "askQ": 130}]
                        },
                        "vtt": 50000,
                        "oi": 120000.0,
                        "optionGreeks": {"delta": 0.52, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
                    }
                },
            }
        },
    }
    streamer.handle_raw_message(frame)

    # Validate get_live_quote
    quote, err = streamer.get_live_quote("NSE_FO|45134", max_age_seconds=30.0, now_dt=mock_now_utc)
    assert err is None
    assert quote is not None
    assert quote["last_price"] == 145.50
    assert quote["bid"] == 145.00
    assert quote["ask"] == 146.00
    assert quote["volume"] == 50000
    assert quote["oi"] == 120000.0
    assert quote["quote_timestamp"] is not None
    assert quote["exchange_timestamp"] is not None
    assert quote["retrieval_timestamp"] is not None

    # Apply live quotes to option chain
    chain = data_in.OptionChain(
        "NIFTY",
        25150.0,
        "2026-11-17",
        {
            25150.0: {
                "ce": {
                    "symbol": "NIFTY2026111725150CE",
                    "instrument_key": "NSE_FO|45134",
                    "last_price": 137.65,
                    "bid": 136.60,
                    "ask": 137.60,
                }
            }
        },
    )
    res = streamer.apply_live_quotes(chain, max_age_seconds=30.0, now_dt=mock_now_utc)
    assert res["updated_contracts"] == 1
    assert res["failed_contracts"] == 0

    c = chain.strikes_data[25150.0]["ce"]
    assert c["last_price"] == 145.50
    assert c["bid"] == 145.00
    assert c["ask"] == 146.00
    assert c["quote_provenance"] == "UPSTOX_WEBSOCKET_V2_LIVE"
    assert c["is_live"] is True
    assert c["tier"] == "REAL_TIME"

    # Evaluate with OptionsIntelligenceSpecialist using this streamer during open session
    spec = OptionsIntelligenceSpecialist(streamer=streamer)
    sig = spec.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data=c,
        underlying_setup={
            "setup": "breakout_intraday",
            "side": "buy",
            "price": 25150.0,
            "invalidation_level": 25130.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        now_utc=mock_now_utc,
        max_loss_budget=2000.0,
        available_capital=100000.0,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    assert sig["execution_reconciliation"]["is_executable"] is True
    assert sig["execution_reconciliation"]["quote_classification"] == "REAL_TIME_VERIFIED"
    assert sig["execution_reconciliation"]["quote_provenance"] == "UPSTOX_WEBSOCKET_V2_LIVE"


def test_websocket_missing_exchange_timestamp_fails_closed():
    """Verify that feeds without exchange timestamps fail closed with MISSING_QUOTE_TIMESTAMP."""
    streamer, _ = _make_streamer()
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    # Feed message with no currentTs and no ltt
    frame = {
        "type": "live_feed",
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.50},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 145.00, "bidQ": 65, "askP": 146.00, "askQ": 130}]}
                    }
                },
            }
        },
    }
    streamer.handle_raw_message(frame)

    quote, err = streamer.get_live_quote("NSE_FO|45134", max_age_seconds=30.0)
    assert quote is None
    assert err == "MISSING_QUOTE_TIMESTAMP"


def test_websocket_stale_timestamp_fails_closed():
    """Verify that feeds with quote age > max_age_seconds fail closed with STALE_QUOTE_DATA."""
    streamer, _ = _make_streamer()
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    # Quote from 10 minutes ago
    ref_now = datetime.datetime(2026, 11, 17, 5, 10, 0, tzinfo=datetime.timezone.utc)
    old_ts_ms = int((ref_now - datetime.timedelta(minutes=10)).timestamp() * 1000)

    frame = {
        "type": "live_feed",
        "currentTs": old_ts_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.50, "ltt": old_ts_ms},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 145.00, "bidQ": 65, "askP": 146.00, "askQ": 130}]}
                    }
                },
            }
        },
    }
    streamer.handle_raw_message(frame)

    quote, err = streamer.get_live_quote("NSE_FO|45134", max_age_seconds=30.0, now_dt=ref_now)
    assert quote is None
    assert err == "STALE_QUOTE_DATA"


def test_websocket_future_timestamp_fails_closed():
    """Verify that feeds with exchange timestamps in the future fail closed."""
    streamer, _ = _make_streamer()
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    ref_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    future_ts_ms = int((ref_now + datetime.timedelta(minutes=5)).timestamp() * 1000)

    frame = {
        "type": "live_feed",
        "currentTs": future_ts_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.50, "ltt": future_ts_ms},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 145.00, "bidQ": 65, "askP": 146.00, "askQ": 130}]}
                    }
                },
            }
        },
    }
    streamer.handle_raw_message(frame)

    quote, err = streamer.get_live_quote("NSE_FO|45134", max_age_seconds=30.0, now_dt=ref_now)
    assert quote is None
    assert err == "FUTURE_QUOTE_TIMESTAMP"


def test_websocket_invalid_and_crossed_quotes_fail_closed():
    """Verify that crossed and zero quotes fail closed."""
    streamer, _ = _make_streamer()
    streamer.subscribe(["NSE_FO|45134", "NSE_FO|45136"], mode="full")

    now_utc = datetime.datetime(2026, 11, 17, 5, 0, 5, tzinfo=datetime.timezone.utc)
    ts_ms = int(now_utc.timestamp() * 1000)

    # Crossed market (bid 150 > ask 145)
    crossed_frame = {
        "type": "live_feed",
        "currentTs": ts_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 148.0, "ltt": ts_ms},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 150.00, "bidQ": 65, "askP": 145.00, "askQ": 130}]}
                    }
                },
            },
            "NSE_FO|45136": {
                "ltpc": {"ltp": 100.0, "ltt": ts_ms},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 0.0, "bidQ": 0, "askP": 100.0, "askQ": 65}]}
                    }
                },
            },
        },
    }
    streamer.handle_raw_message(crossed_frame)

    q1, err1 = streamer.get_live_quote("NSE_FO|45134", max_age_seconds=30.0, now_dt=now_utc)
    assert q1 is None
    assert err1 == "CROSSED_BID_ASK"

    q2, err2 = streamer.get_live_quote("NSE_FO|45136", max_age_seconds=30.0, now_dt=now_utc)
    assert q2 is None
    assert err2 == "INVALID_BID_ASK"


def test_websocket_disconnected_and_reconnection_behaviour():
    """Verify fail closed upon feed disconnection and recovery upon reconnection."""
    mock_ws = _MockWS()
    streamer, _ = _make_streamer(ws_client=mock_ws)
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    # While connected, quote is accessible
    now_utc = datetime.datetime(2026, 11, 17, 5, 0, 5, tzinfo=datetime.timezone.utc)
    ts_ms = int(now_utc.timestamp() * 1000)
    frame = {
        "type": "live_feed",
        "currentTs": ts_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.5, "ltt": ts_ms},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 145.0, "bidQ": 65, "askP": 146.0, "askQ": 65}]}
                    }
                },
            }
        },
    }
    streamer.handle_raw_message(frame)

    q, err = streamer.get_live_quote("NSE_FO|45134", now_dt=now_utc)
    assert q is not None

    # Simulate disconnect
    streamer.close()
    assert streamer.is_connected is False

    q_disconn, err_disconn = streamer.get_live_quote("NSE_FO|45134", now_dt=now_utc)
    assert q_disconn is None
    assert err_disconn == "WEBSOCKET_FEED_DISCONNECTED"

    # Reconnect
    streamer.reconnect(max_attempts=1)
    assert streamer.is_connected is True
    # Keys remain preserved
    assert "NSE_FO|45134" in streamer.active_keys()


# ---------------------------------------------------------------- Comprehensive Forensic Audit Regressions (F-001 through F-018)


def test_audit_f001_bullish_spot_past_invalidation_rejected(specialist):
    """F-001: Bullish setup where current spot has breached invalidation level must be rejected."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    # Entry trigger was 25200 with invalidation 25150, but current spot collapsed to 25140 (< 25150)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.52, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "current_spot": 25140.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=5000.0,
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("INVALIDATED_SETUP" in r for r in sig["rejection_reasons"])
    assert sig["underlying_setup_evidence"]["setup_validity_state"] == "INVALIDATED"


def test_audit_f001_bearish_spot_above_invalidation_rejected(specialist):
    """F-001: Bearish setup where current spot has rallied above stop must be rejected."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    # Entry was 25100 with stop 25150, but current spot rallied to 25160 (> 25150)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH",
        strategy=STRATEGY_BUY_PE,
        option_type="PE",
        strike=25100.0,
        contract_data={
            "symbol": "NIFTY2026111725100PE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": -0.48, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "price": 25100.0,
            "invalidation_level": 25150.0,
            "current_spot": 25160.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=5000.0,
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("INVALIDATED_SETUP" in r for r in sig["rejection_reasons"])
    assert sig["underlying_setup_evidence"]["setup_validity_state"] == "INVALIDATED"


def test_audit_f001_extended_above_chase_limit_rejected(specialist):
    """F-001: Setups flagged chase_blocked or past entry zone must reject as EXTENDED_SETUP."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.52, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25150.0,
            "invalidation_level": 25100.0,
            "current_spot": 25250.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "decision": {
                "chase_blocked": True,
                "sub_state": "EXTENDED",
                "entry_zone": {"low": 25145.0, "high": 25165.0},
            },
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=5000.0,
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("EXTENDED_SETUP" in r for r in sig["rejection_reasons"])


def test_audit_f007_invalidated_or_extended_index_setup_fails_in_live_market(specialist):
    """F-007: In an open live session, invalidated or extended index setups must fail closed in is_valid_session_setup."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    now_ist_str = "2026-11-17"

    # Case 1: Invalidated index setup
    inv_setup = {
        "symbol": "NIFTY",
        "status": "triggered",
        "decision_state": "NO TRADE",
        "sub_state": "ANALYTICAL_ONLY_INDEX",
        "date": now_ist_str,
        "is_invalidated": True,
    }
    assert specialist.is_valid_session_setup(inv_setup, mock_open) is False

    # Case 2: Chase blocked / extended index setup
    ext_setup = {
        "symbol": "BANKNIFTY",
        "status": "triggered",
        "decision_state": "NO TRADE",
        "sub_state": "ANALYTICAL_ONLY_INDEX",
        "date": now_ist_str,
        "decision": {"chase_blocked": True},
    }
    assert specialist.is_valid_session_setup(ext_setup, mock_open) is False


def test_audit_f010_contract_canonical_keys_reconciliation(specialist):
    """F-010: Verify options.py reads stop_loss and targets from decision payload without synthetic fallback."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.52, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "decision": {
                "entry_reference": 25200.0,
                "stop_loss": 25160.0,
                "invalidation_level": 25160.0,
                "current_price": 25205.0,
                "targets": {"target_1_1r": 25300.0, "target_2_2r": 25400.0},
            },
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=10000.0,
        min_reward_risk=2.0,
        now_utc=mock_open,
    )
    # Stop loss and targets in premium levels are modeled from exact canonical levels (25160 and 25300)
    assert sig["stop_loss"] < sig["current_premium"]
    assert sig["target_1"] > sig["current_premium"]
    assert sig["underlying_setup_evidence"]["invalidation_level"] == 25160.0


def test_audit_f003_missing_greeks_rejects_with_greeks_unavailable(specialist):
    """F-003: If required delta is missing/None, candidate must be rejected with GREEKS_UNAVAILABLE."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {},  # Missing delta
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("GREEKS_UNAVAILABLE" in r for r in sig["rejection_reasons"])


def test_audit_f003_zero_delta_and_zero_gamma_supported_without_truthiness_corruption(specialist):
    """F-003: Delta=0.0 or Gamma=0.0 must be preserved as legitimate numbers without fallback coercion."""
    res = specialist.model_premium_levels(
        entry_premium=100.0,
        spot_entry=25200.0,
        spot_stop=25150.0,
        spot_t1=25300.0,
        spot_t2=25400.0,
        delta=0.0,
        gamma=0.0,
        theta=0.0,
        vega=0.0,
        iv=15.0,
        option_type="CE",
        is_seller=False,
    )
    # With delta=0, gamma=0, premium does not move with spot
    assert res is None or res.get("delta_used") == 0.0


def test_audit_f005_f006_spread_strategy_two_leg_validation_and_payoff(specialist):
    """F-005 & F-006: Spread evaluates both legs with independent quotes and combined payoff."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BULL_CALL_SPREAD,
        option_type="SPREAD",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.52, "gamma": 0.001},
        },
        spread_contract={
            "strike": 25250.0,
            "symbol": "NIFTY2026111725250CE",
            "bid": 85.0,
            "ask": 86.0,
            "last_price": 85.5,
            "option_type": "CE",
            "quote_provenance": "UPSTOX_WEBSOCKET_V2_LIVE",
            "delta": 0.32,
            "iv": 14.5,
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0, "target_2_2r": 25400.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=10000.0,
        available_capital=100000.0,
        now_utc=mock_open,
    )
    # Check legs structure
    legs = sig.get("legs", [])
    assert len(legs) == 2
    assert legs[0]["role"] == "PRIMARY"
    assert legs[0]["option_type"] == "CE"
    assert legs[0]["side"] == "BUY"
    assert legs[1]["role"] == "HEDGE"
    assert legs[1]["option_type"] == "CE"
    assert legs[1]["side"] == "SELL"

    # Spread risk per lot: Net debit = entry1 (141.05) - entry2 (84.95) = 56.10
    ps = sig["position_sizing"]
    assert ps["risk_per_lot_inr"] > 0
    assert ps["risk_per_lot_inr"] < (100.0 * 65)  # Less than naked call risk


def test_audit_f005_spread_hedge_leg_crossed_or_zero_quotes_rejected(specialist):
    """F-005 & F-006: Spread must reject if hedge leg has zero or crossed quotes."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BULL_CALL_SPREAD,
        option_type="SPREAD",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.52, "gamma": 0.001},
        },
        spread_contract={
            "strike": 25250.0,
            "symbol": "NIFTY2026111725250CE",
            "bid": 90.0,
            "ask": 80.0,  # Crossed bid >= ask
            "last_price": 85.0,
            "option_type": "CE",
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("Crossed market quotes in spread hedge leg" in r for r in sig["rejection_reasons"])


def test_audit_f013_naked_selling_unauthoritative_margin_rejected(specialist):
    """F-013: Naked SELL CE / SELL PE must reject live execution when authoritative margin is unavailable."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH",
        strategy=STRATEGY_SELL_CE,
        option_type="CE",
        strike=25200.0,
        contract_data={
            "symbol": "NIFTY2026111725200CE",
            "bid": 110.0,
            "ask": 111.0,
            "last_price": 110.5,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.45, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "price": 25200.0,
            "invalidation_level": 25250.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25100.0, "target_2_2r": 25000.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=50000.0,
        available_capital=500000.0,
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("AUTHORITATIVE_MARGIN_UNAVAILABLE" in r for r in sig["rejection_reasons"])
    assert sig["margin_details"]["margin_type"] == "HEURISTIC_MARGIN_ESTIMATE"
    assert sig["margin_details"]["is_undefined_risk"] is True


def test_audit_f008_intent_parsing_word_boundaries_no_false_positives():
    """F-008: Regex word boundaries prevent false positive direction matches on words like open or setup."""
    orch = MarketIntelligenceOrchestrator()

    # Query with 'open' (contains 'pe') should NOT match BEARISH
    q1 = orch.detect_intent("What is the open interest on NIFTY?")
    assert q1["direction_bias"] is None
    assert q1["target_symbol"] == "NIFTY"

    # Query with 'setup' (contains 'up') should NOT match BULLISH
    q2 = orch.detect_intent("Show me the setup for BANKNIFTY")
    assert q2["direction_bias"] is None
    assert q2["target_symbol"] == "BANKNIFTY"

    # Exact 'pe' or 'put' matches BEARISH
    q3 = orch.detect_intent("Find best NIFTY PE options")
    assert q3["direction_bias"] == "BEARISH"

    # Exact 'ce' or 'call' matches BULLISH
    q4 = orch.detect_intent("Analyze CE calls on INFY")
    assert q4["direction_bias"] == "BULLISH"


def test_audit_f014_f018_dynamic_universe_reporting_and_scan_errors():
    """F-014 & F-018: Universe counts reported dynamically and scanner errors captured."""
    orch = MarketIntelligenceOrchestrator()
    res = orch.run_market_intelligence(query="NIFTY", top_n=2)

    assert "total_eligible" in res
    assert res["total_eligible"] >= 10
    assert "passed_to_scanner" in res
    assert "successfully_scanned" in res
    assert "coverage_pct" in res
    assert isinstance(res.get("scan_errors"), list)


def test_audit_websocket_ltpc_packet_without_depth_does_not_mark_quotes_live():
    """Verify that an LTPC tick without order book depth fails get_live_quote(require_depth=True)."""
    streamer, _ = _make_streamer()
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    mock_now_utc = datetime.datetime(2026, 11, 17, 5, 0, 5, tzinfo=datetime.timezone.utc)
    ts_ms = int(mock_now_utc.timestamp() * 1000)

    # LTPC message with ltp but no bid/ask quotes
    frame = {
        "type": "live_feed",
        "currentTs": ts_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.50, "ltt": ts_ms},
            }
        },
    }
    streamer.handle_raw_message(frame)

    # require_depth=True must reject missing bid/ask
    q, err = streamer.get_live_quote("NSE_FO|45134", require_depth=True, now_dt=mock_now_utc)
    assert q is None
    assert err == "MISSING_BID_ASK"

    # require_depth=False allows LTP-only lookup
    q_ltp, err_ltp = streamer.get_live_quote("NSE_FO|45134", require_depth=False, now_dt=mock_now_utc)
    assert q_ltp is not None
    assert err_ltp is None
    assert q_ltp["last_price"] == 145.50


def test_audit_websocket_wait_for_quotes_synchronous_receive():
    """Verify wait_for_quotes polls socket until all required ticks arrive."""
    mock_now_utc = datetime.datetime(2026, 11, 17, 5, 0, 5, tzinfo=datetime.timezone.utc)
    ts_ms = int(mock_now_utc.timestamp() * 1000)

    frame = {
        "type": "live_feed",
        "currentTs": ts_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.50, "ltt": ts_ms},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 145.00, "bidQ": 65, "askP": 146.00, "askQ": 65}]}
                    }
                },
            }
        },
    }
    # Mock WS with frame queued
    streamer, _ = _make_streamer(frames=[frame])
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    # wait_for_quotes polls and consumes the frame
    received = streamer.wait_for_quotes(["NSE_FO|45134"], timeout=1.0)
    assert received == 1
    assert "NSE_FO|45134" in streamer.last_ticks


def test_audit_upstox_strictly_read_only_boundary():
    """Verify Upstox integration and options modules contain zero order placement/modification methods."""
    import inspect
    from upstox_feed import UpstoxMarketDataStreamer
    from options import OptionsIntelligenceSpecialist

    forbidden_names = ["place_order", "modify_order", "cancel_order", "execute_order", "send_order"]

    for name in forbidden_names:
        assert not hasattr(UpstoxMarketDataStreamer, name), f"Streamer must not have {name}"
        assert not hasattr(OptionsIntelligenceSpecialist, name), f"Specialist must not have {name}"
        assert not hasattr(data_in.UpstoxMarketDataProvider, name), f"Provider must not have {name}"


def test_phase1_orchestrator_streamer_injection_and_lifecycle():
    """Phase 1: Verify Orchestrator injects streamer and cleanly shuts it down."""
    streamer, _ = _make_streamer()
    assert streamer.is_connected is True
    streamer.last_ticks["NSE_FO|12345"] = {"last_price": 100.0}

    with MarketIntelligenceOrchestrator(streamer=streamer) as orch:
        assert orch.specialist.streamer is streamer
        assert orch.streamer is streamer
        assert streamer.is_connected is True

    # After context exit, streamer should be closed and cached ticks cleared
    assert streamer.is_connected is False
    assert len(streamer.last_ticks) == 0


def test_phase1_ltpc_tick_does_not_refresh_stale_depth_timestamp():
    """Phase 1: Verify LTP-only ticks do not refresh depth timestamp, causing stale depth rejection."""
    streamer, _ = _make_streamer()
    streamer.subscribe(["NSE_FO|45134"], mode="full")

    t0 = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    t0_ms = int(t0.timestamp() * 1000)

    # Frame 1 at T0: Full depth tick
    frame1 = {
        "type": "live_feed",
        "currentTs": t0_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 145.0, "ltt": t0_ms},
                "fullFeed": {
                    "marketFF": {
                        "marketLevel": {"bidAskQuote": [{"bidP": 144.5, "bidQ": 50, "askP": 145.5, "askQ": 50}]}
                    }
                },
            }
        },
    }
    streamer.handle_raw_message(frame1)

    # Depth is fresh at T0 + 10s
    t_10s = t0 + datetime.timedelta(seconds=10)
    q1, err1 = streamer.get_live_quote("NSE_FO|45134", require_depth=True, max_age_seconds=120.0, now_dt=t_10s)
    assert err1 is None
    assert q1["bid"] == 144.5

    # Frame 2 at T0 + 130s: LTP-only tick (depth omitted)
    t_130s = t0 + datetime.timedelta(seconds=130)
    t_130s_ms = int(t_130s.timestamp() * 1000)
    frame2 = {
        "type": "live_feed",
        "currentTs": t_130s_ms,
        "feeds": {
            "NSE_FO|45134": {
                "ltpc": {"ltp": 146.0, "ltt": t_130s_ms},
            }
        },
    }
    streamer.handle_raw_message(frame2)

    # At T0 + 130s, last_price is updated to 146.0, but depth quote timestamp remained at T0
    # Therefore, require_depth=True must reject with STALE_QUOTE_DATA!
    q2, err2 = streamer.get_live_quote("NSE_FO|45134", require_depth=True, max_age_seconds=120.0, now_dt=t_130s)
    assert q2 is None
    assert err2 == "STALE_QUOTE_DATA"

    # But require_depth=False succeeds with updated LTP
    q_ltp, err_ltp = streamer.get_live_quote("NSE_FO|45134", require_depth=False, max_age_seconds=120.0, now_dt=t_130s)
    assert err_ltp is None
    assert q_ltp["last_price"] == 146.0


def test_phase2_seller_dp_stop_sign_and_slippage(specialist):
    """Phase 2: Seller premium levels have correct sign (no abs distortion) and conservative slippage."""
    entry_prem = 100.0
    spot_entry = 25000.0
    spot_stop = 25100.0  # Adverse for SELL CE (spot rises by 100 pts)
    spot_t1 = 24950.0    # Favorable for SELL CE (spot falls by 50 pts)
    spot_t2 = 24900.0    # More favorable for SELL CE (spot falls by 100 pts)

    res = specialist.model_premium_levels(
        entry_premium=entry_prem,
        spot_entry=spot_entry,
        spot_stop=spot_stop,
        spot_t1=spot_t1,
        spot_t2=spot_t2,
        delta=0.50,
        gamma=0.0,
        theta=0.0,
        vega=0.0,
        option_type="CE",
        is_seller=True,
        slippage_ticks=1,
    )
    assert res is not None
    assert res["entry_premium"] == 99.95  # 100.0 - 0.05 sell entry slippage
    assert res["stop_loss_premium"] == 150.05  # 100.0 + 50.0 + 0.05 buy stop slippage
    assert res["target_1_premium"] == 75.05  # 100.0 - 25.0 + 0.05 buy target slippage
    assert res["target_2_premium"] == 50.05  # 100.0 - 50.0 + 0.05 buy target slippage
    assert res["target_2_premium"] < res["target_1_premium"] < res["entry_premium"] < res["stop_loss_premium"]


def test_phase2_holding_time_session_clock(specialist):
    """Phase 2: Holding time derives dynamically from session cutoff 15:15 IST."""
    t_1415_ist = datetime.datetime(2026, 11, 17, 14, 15, tzinfo=calendar_in.IST)
    res_1h = specialist.model_premium_levels(
        entry_premium=100.0,
        spot_entry=25000.0,
        spot_stop=24900.0,
        spot_t1=25200.0,
        spot_t2=25300.0,
        delta=0.50,
        gamma=0.0,
        theta=-30.0,
        option_type="CE",
        is_seller=False,
        evaluation_time=t_1415_ist,
    )
    assert res_1h is not None
    # Theta is a per-calendar-day figure (Stage 1 fix): decay over 1 held hour is
    # theta * (1 / 24), not theta * (1 / 6.25) -- the holding-time-shrinking behavior under
    # test (1 hour remaining to the 15:15 IST cutoff) is otherwise unchanged.
    assert math.isclose(res_1h["theta_decay_applied"], 1.25, abs_tol=0.05)


def test_phase3_authoritative_bod_cache_enforcement():
    """Phase 3: Fallback lot size cannot satisfy require_authoritative."""
    lot_fallback = instruments.get_fo_lot_size(
        "NIFTY",
        cache_payload={"derivatives_meta": {}},
        require_authoritative=False,
    )
    assert lot_fallback == 25

    lot_auth = instruments.get_fo_lot_size(
        "NIFTY",
        cache_payload={"derivatives_meta": {}},
        require_authoritative=True,
    )
    assert lot_auth is None


def test_phase4_central_portfolio_risk_enforcement(specialist, tmp_path):
    """Phase 4: Enforce max daily loss and max open positions during live check."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    mock_port = tmp_path / "portfolio.json"
    mock_broker = tmp_path / "broker.json"

    mock_port.write_text(json.dumps({
        "cash": 100000.0,
        "positions": [{"symbol": f"POS_{i}", "qty": 10} for i in range(5)],
    }))
    mock_broker.write_text(json.dumps({"orders": []}))

    ok, rejections = specialist.check_portfolio_risk(
        candidate_risk=1000.0,
        capital_required=10000.0,
        portfolio_path=str(mock_port),
        broker_state_path=str(mock_broker),
        now_utc=mock_now,
        is_live_check=True,
    )
    assert ok is False
    assert any("MAX_OPEN_POSITIONS_REACHED" in r for r in rejections)


def test_phase5_orchestrator_shortlist_and_diagnostic_table(orchestrator):
    """Phase 5: Actionable shortlist excludes rejected candidates and includes diagnostic table."""
    sig_good = {
        "underlying_instrument": "NIFTY",
        "strategy": "BUY_CE",
        "strike": 25000.0,
        "signal_state": "SIGNAL VALIDATED",
        "is_actionable": True,
        "ranking_score": 90.0,
        "current_premium": 150.0,
        "stop_loss": 120.0,
        "target_1": 210.0,
        "target_2": 240.0,
        "expected_risk_reward_after_charges": 2.5,
        "maximum_loss": "Total premium outlay",
        "capital_margin_requirement": 9750.0,
        "direction": "BULLISH",
        "contract": "NIFTY26N1725000CE",
        "expiry": "2026-11-17",
        "position_sizing": {
            "lot_size": 65, "lots": 1, "total_quantity": 65,
            "net_risk_per_lot_inr": 1950.0, "capital_required_per_lot": 9750.0,
            "budget_max_loss_inr": 2000.0,
        },
        "selection_rationale": "High quality breakout setup",
    }
    sig_bad = {
        "underlying_instrument": "BANKNIFTY",
        "strategy": "SELL_PE",
        "strike": 52000.0,
        "signal_state": "SIGNAL REJECTED",
        "is_actionable": False,
        "rejection_reasons": ["AUTHORITATIVE_MARGIN_UNAVAILABLE: Heuristic estimate not permitted"],
        "ranking_score": 10.0,
        "direction": "BULLISH_OR_SIDEWAYS",
    }

    report = orchestrator._format_report(
        query="NIFTY and BANKNIFTY",
        session_label="LIVE SESSION",
        underlying_summaries={},
        shortlist=[sig_good],
        rejected_signals=[sig_bad],
        total_eligible=230,
        passed_to_scanner=2,
        successfully_scanned=2,
    )
    assert "Filtered & Rejected Candidates (Diagnostic Audit)" in report
    assert "AUTHORITATIVE_MARGIN_UNAVAILABLE" in report
    assert "Evaluated **2** key instruments (core scan universe out of 230" in report


# =====================================================================
# Targeted Hardening: Mandatory Greeks, CLI Lifecycle, Execution Safety
# =====================================================================

def test_hardening_missing_gamma_independently_rejects(specialist):
    """Hardening 1: Missing Gamma must reject with GREEKS_UNAVAILABLE and not default to synthetic 0.001."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51},  # Missing gamma
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("GREEKS_UNAVAILABLE" in r and "Gamma" in r for r in sig["rejection_reasons"])


def test_hardening_missing_delta_independently_rejects(specialist):
    """Hardening 1b: Missing Delta must independently reject with GREEKS_UNAVAILABLE."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"gamma": 0.001},  # Missing delta
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("GREEKS_UNAVAILABLE" in r and "Delta" in r for r in sig["rejection_reasons"])


def test_hardening_seller_missing_theta_independently_rejects(specialist):
    """Hardening 1c: Missing Theta on option selling must reject with GREEKS_UNAVAILABLE."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BEARISH_OR_SIDEWAYS",
        strategy=STRATEGY_SELL_CE,
        option_type="CE",
        strike=25200.0,
        contract_data={
            "symbol": "NIFTY2026111725200CE",
            "bid": 110.0,
            "ask": 111.0,
            "last_price": 110.5,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.45, "gamma": 0.001},  # Missing theta for seller
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "side": "sell",
            "price": 25200.0,
            "invalidation_level": 25250.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25100.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("GREEKS_UNAVAILABLE" in r and "Theta" in r for r in sig["rejection_reasons"])


def test_hardening_buyer_strategy_exception_documented_and_tested(specialist):
    """Hardening 1d: Directional buyers have documented strategy exception for Theta/Vega/IV."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},  # Delta and Gamma present, Theta/Vega/IV omitted
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0, "target_2_2r": 25400.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        max_loss_budget=15000.0,
        now_utc=mock_open,
    )
    # Strategy exception allows directional buyer without Theta/Vega/IV
    assert not any("GREEKS_UNAVAILABLE" in r for r in sig["rejection_reasons"])
    assert sig["signal_state"] == "SIGNAL VALIDATED"
    assert sig["is_actionable"] is True


def test_hardening_require_all_greeks_independently_rejects_missing_values():
    """Hardening 1e: With require_all_greeks enabled, missing Theta, Vega, or IV independently reject."""
    spec = OptionsIntelligenceSpecialist(config={"require_all_greeks": True})
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)

    # Missing Theta
    sig_no_theta = spec.generate_signal_contract(
        underlying="NIFTY", direction="BULLISH", strategy=STRATEGY_BUY_CE, option_type="CE", strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE", "bid": 140.0, "ask": 141.0, "last_price": 141.0,
            "tier": "REAL_TIME", "is_live": True, "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30", "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001, "vega": 10.0, "iv": 14.0},  # Missing Theta
        },
        underlying_setup={"setup": "breakout_intraday", "price": 25200.0, "invalidation_level": 25150.0, "candle_timestamp": "2026-11-17T10:30:00+05:30"},
        meta={"lot_size": 65, "expiry": "2026-11-17"}, now_utc=mock_open,
    )
    assert any("GREEKS_UNAVAILABLE" in r and "Theta" in r for r in sig_no_theta["rejection_reasons"])

    # Missing Vega
    sig_no_vega = spec.generate_signal_contract(
        underlying="NIFTY", direction="BULLISH", strategy=STRATEGY_BUY_CE, option_type="CE", strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE", "bid": 140.0, "ask": 141.0, "last_price": 141.0,
            "tier": "REAL_TIME", "is_live": True, "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30", "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "iv": 14.0},  # Missing Vega
        },
        underlying_setup={"setup": "breakout_intraday", "price": 25200.0, "invalidation_level": 25150.0, "candle_timestamp": "2026-11-17T10:30:00+05:30"},
        meta={"lot_size": 65, "expiry": "2026-11-17"}, now_utc=mock_open,
    )
    assert any("GREEKS_UNAVAILABLE" in r and "Vega" in r for r in sig_no_vega["rejection_reasons"])

    # Missing IV
    sig_no_iv = spec.generate_signal_contract(
        underlying="NIFTY", direction="BULLISH", strategy=STRATEGY_BUY_CE, option_type="CE", strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE", "bid": 140.0, "ask": 141.0, "last_price": 141.0,
            "tier": "REAL_TIME", "is_live": True, "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30", "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},  # Missing IV
        },
        underlying_setup={"setup": "breakout_intraday", "price": 25200.0, "invalidation_level": 25150.0, "candle_timestamp": "2026-11-17T10:30:00+05:30"},
        meta={"lot_size": 65, "expiry": "2026-11-17"}, now_utc=mock_open,
    )
    assert any("GREEKS_UNAVAILABLE" in r and "IV" in r for r in sig_no_iv["rejection_reasons"])


def test_hardening_legitimate_zero_greeks_preserved(specialist):
    """Hardening 1f: Zero values for Greeks (delta=0, gamma=0, theta=0) must be preserved without truthiness corruption."""
    res = specialist.model_premium_levels(
        entry_premium=100.0,
        spot_entry=25200.0,
        spot_stop=25150.0,
        spot_t1=25300.0,
        spot_t2=25400.0,
        delta=0.0,
        gamma=0.0,
        theta=0.0,
        vega=0.0,
        iv=0.0,
        option_type="CE",
        is_seller=False,
    )
    assert res is None or (res.get("delta_used") == 0.0 and res.get("gamma_used") == 0.0)


def test_hardening_cli_lifecycle_context_manager_cleanup():
    """Hardening 2: Context manager cleanly closes orchestrator and streamer on normal exit and exception."""
    class DummyStreamer:
        def __init__(self):
            self.is_connected = True
            self.closed = False
        def close(self):
            self.closed = True
            self.is_connected = False

    streamer = DummyStreamer()
    orch = MarketIntelligenceOrchestrator(streamer=streamer)

    # Test normal exit
    with orch as o:
        assert o.streamer.is_connected is True
    assert orch.streamer.closed is True
    assert orch.streamer.is_connected is False

    # Test exception exit
    streamer2 = DummyStreamer()
    orch2 = MarketIntelligenceOrchestrator(streamer=streamer2)
    with pytest.raises(RuntimeError):
        with orch2 as o:
            raise RuntimeError("Simulated pipeline failure")
    assert orch2.streamer.closed is True
    assert orch2.streamer.is_connected is False


def test_hardening_excessive_spread_rejection(specialist):
    """Hardening 3a: Wide bid-ask spreads (> 6.0%) must reject with EXCESSIVE_SPREAD."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)
    # Bid 100, Ask 120 -> Spread 20 / 110 = 18.18% > 6.0%
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 100.0,
            "ask": 120.0,
            "last_price": 110.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert sig["is_actionable"] is False
    assert any("EXCESSIVE_SPREAD" in r for r in sig["rejection_reasons"])


def test_hardening_insufficient_depth_rejection(specialist):
    """Hardening 3b: Zero bid or ask quantity in order book must reject with INSUFFICIENT_DEPTH."""
    mock_open = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)

    # Zero bid quantity
    sig_zero_bid_qty = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 0,  # Zero depth on bid
            "ask_qty": 500,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig_zero_bid_qty["signal_state"] == "SIGNAL REJECTED"
    assert any("INSUFFICIENT_DEPTH" in r for r in sig_zero_bid_qty["rejection_reasons"])

    # Zero ask quantity
    sig_zero_ask_qty = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 500,
            "ask_qty": 0,  # Zero depth on ask
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        now_utc=mock_open,
    )
    assert sig_zero_ask_qty["signal_state"] == "SIGNAL REJECTED"
    assert any("INSUFFICIENT_DEPTH" in r for r in sig_zero_ask_qty["rejection_reasons"])


# ==============================================================================
# REMEDIATION FORENSIC TESTS (FINDINGS 1 - 4)
# ==============================================================================


# ------------------------------------------------------------------------------
# 1. P1: FRESH UNDERLYING SPOT
# ------------------------------------------------------------------------------
def test_underlying_spot_missing_timestamp_rejects(specialist):
    """Underlying spot missing timestamp fails freshness validation fail-closed."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={
            "underlying_price": 25200.0,
            "quote_timestamp": None,
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
        },
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("UNDERLYING_SPOT_UNAVAILABLE" in r for r in sig["rejection_reasons"])


def test_underlying_spot_unparseable_timestamp_rejects(specialist):
    """Underlying spot with unparseable timestamp fails validation."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={
            "underlying_price": 25200.0,
            "quote_timestamp": "corrupt-iso-timestamp-string",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
        },
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("UNDERLYING_SPOT_UNAVAILABLE" in r for r in sig["rejection_reasons"])


def test_underlying_spot_stale_timestamp_rejects(specialist):
    """Underlying spot older than 60s max age threshold fails freshness validation."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    stale_ts = (mock_now - datetime.timedelta(seconds=75)).isoformat()
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": mock_now.isoformat(),
            "retrieval_timestamp": mock_now.isoformat(),
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": mock_now.isoformat(),
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={
            "underlying_price": 25200.0,
            "quote_timestamp": stale_ts,
            "retrieval_timestamp": mock_now.isoformat(),
        },
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("STALE" in r or "UNDERLYING_SPOT_UNAVAILABLE" in r for r in sig["rejection_reasons"])


def test_underlying_spot_future_timestamp_rejects(specialist):
    """Underlying spot timestamp with future drift > 5s fails freshness validation."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    future_ts = (mock_now + datetime.timedelta(seconds=20)).isoformat()
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": mock_now.isoformat(),
            "retrieval_timestamp": mock_now.isoformat(),
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": mock_now.isoformat(),
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={
            "underlying_price": 25200.0,
            "quote_timestamp": future_ts,
            "retrieval_timestamp": mock_now.isoformat(),
        },
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("FUTURE" in r or "UNDERLYING_SPOT_UNAVAILABLE" in r for r in sig["rejection_reasons"])


def test_underlying_spot_fresh_quote_validates(specialist):
    """Fresh validated underlying spot permits signal validation."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (mock_now - datetime.timedelta(seconds=5)).isoformat()
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
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
            "provider": "upstox",
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25240.0,
            "invalidation_level": 25100.0,
            "candle_timestamp": fresh_ts,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={
            "underlying_price": 25240.0,
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
        },
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL VALIDATED"
    assert sig["is_actionable"] is True
    assert sig["underlying_setup_evidence"]["current_spot"] == 25240.0
    assert sig["underlying_setup_evidence"]["candle_close"] == 25240.0


def test_candle_close_and_current_spot_disagree_invalidation_breach_rejects(specialist):
    """Candle close was above invalidation, but fresh live spot has crossed below invalidation."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (mock_now - datetime.timedelta(seconds=2)).isoformat()
    # Bullish setup: entry 25200, invalidation 25150
    # Candle close was 25200 (valid), but live spot dropped to 25140 (< 25150 invalidation)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 500,
            "ask_qty": 500,
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": fresh_ts,
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={
            "underlying_price": 25140.0,  # Below 25150 invalidation level
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
        },
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("INVALIDATED_SETUP" in r for r in sig["rejection_reasons"])


# ------------------------------------------------------------------------------
# 2. P1: ORDER-BOOK DEPTH VERSUS PERMITTED QUANTITY
# ------------------------------------------------------------------------------
def test_order_book_depth_below_one_lot_rejects(specialist):
    """Available depth less than 1 lot rejects trade fail-closed."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (mock_now - datetime.timedelta(seconds=2)).isoformat()
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 140.0,
            "ask": 141.0,
            "bid_qty": 500,
            "ask_qty": 20,  # Only 20 shares available, but lot size is 65
            "last_price": 141.0,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25150.0,
            "candle_timestamp": fresh_ts,
            "targets": {"target_1_1r": 25300.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25200.0, "quote_timestamp": fresh_ts},
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL REJECTED"
    assert any("INSUFFICIENT_DEPTH" in r for r in sig["rejection_reasons"])


def test_order_book_depth_caps_multi_lot_permitted_quantity(specialist):
    """Available depth for 1 lot caps quantity even if risk budget permits more lots."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (mock_now - datetime.timedelta(seconds=2)).isoformat()
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "bid_qty": 500,
            "ask_qty": 65,  # Exactly 1 lot (65 shares) available
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25240.0,
            "invalidation_level": 25100.0,
            "candle_timestamp": fresh_ts,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25240.0, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,  # Large budget that would otherwise permit multiple lots
        min_reward_risk=2.0,
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL VALIDATED"
    ps = sig["position_sizing"]
    assert ps["depth_permitted_lots"] == 1
    assert ps["final_permitted_lots"] == 1
    assert ps["total_quantity"] == 65


def test_order_book_depth_equal_and_above_proposed_quantity(specialist):
    """Depth equal to or above proposed quantity allows permitted lots without artificial truncation."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (mock_now - datetime.timedelta(seconds=2)).isoformat()
    # 2 lots permitted by depth (130 shares)
    sig_equal = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75,
            "ask": 137.75,
            "bid_qty": 500,
            "ask_qty": 130,  # Exactly 2 lots
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25240.0,
            "invalidation_level": 25100.0,
            "candle_timestamp": fresh_ts,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25240.0, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,
        min_reward_risk=2.0,
        now_utc=mock_now,
    )
    assert sig_equal["signal_state"] == "SIGNAL VALIDATED"
    assert sig_equal["position_sizing"]["depth_permitted_lots"] == 2

    # Ample depth (500 shares)
    sig_above = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
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
            "provider": "upstox",
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25240.0,
            "invalidation_level": 25100.0,
            "candle_timestamp": fresh_ts,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},
        underlying_spot_quote={"underlying_price": 25240.0, "quote_timestamp": fresh_ts},
        max_loss_budget=20000.0,
        min_reward_risk=2.0,
        now_utc=mock_now,
    )
    assert sig_above["signal_state"] == "SIGNAL VALIDATED"
    assert sig_above["position_sizing"]["depth_permitted_lots"] >= 2


def test_spread_hedge_leg_depth_constrains_quantity(specialist):
    """Hedge leg depth constrains spread quantity even when primary leg has large depth."""
    mock_now = datetime.datetime(2026, 11, 17, 5, 0, 0, tzinfo=datetime.timezone.utc)
    fresh_ts = (mock_now - datetime.timedelta(seconds=2)).isoformat()
    sig = specialist.generate_signal_contract(
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
            "ask_qty": 500,  # Ample depth on primary leg (500 shares)
            "last_price": 137.75,
            "tier": "REAL_TIME",
            "is_live": True,
            "provider": "upstox",
            "quote_timestamp": fresh_ts,
            "retrieval_timestamp": fresh_ts,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -2.0, "vega": 10.0},
        },
        spread_contract={
            "strike": 25250.0,
            "symbol": "NIFTY2026111725250CE",
            "last_price": 85.0,
            "bid": 84.0,
            "ask": 85.0,
            "bid_qty": 65,  # Only 1 lot (65 shares) depth on short hedge leg!
            "ask_qty": 500,
            "delta": 0.35,
            "iv": 12.5,
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
        max_loss_budget=20000.0,
        min_reward_risk=1.5,
        now_utc=mock_now,
    )
    assert sig["signal_state"] == "SIGNAL VALIDATED"
    ps = sig["position_sizing"]
    # Hedge depth of 65 shares must constrain sizing to 1 lot
    assert ps["depth_permitted_lots"] == 1
    assert ps["final_permitted_lots"] == 1


# ------------------------------------------------------------------------------
# 3. P2: STRICT PORTFOLIO RISK BY DEFAULT
# ------------------------------------------------------------------------------
def test_portfolio_risk_missing_file_fails_closed_by_default(specialist, tmp_path):
    """In live checks, missing portfolio file fails closed with PORTFOLIO_STATE_UNAVAILABLE."""
    non_existent = str(tmp_path / "non_existent_portfolio.json")
    valid_broker = str(tmp_path / "broker.json")
    with open(valid_broker, "w", encoding="utf-8") as f:
        json.dump({"orders": [], "positions": []}, f)

    is_valid, rejections = specialist.check_portfolio_risk(
        candidate_risk=2000.0,
        capital_required=10000.0,
        portfolio_path=non_existent,
        broker_state_path=valid_broker,
        is_live_check=True,
    )
    assert is_valid is False
    assert any("PORTFOLIO_STATE_UNAVAILABLE" in r for r in rejections)


def test_portfolio_risk_corrupt_json_fails_closed_by_default(specialist, tmp_path):
    """In live checks, corrupt JSON portfolio file fails closed with PORTFOLIO_STATE_UNAVAILABLE."""
    corrupt_port = str(tmp_path / "corrupt_portfolio.json")
    with open(corrupt_port, "w", encoding="utf-8") as f:
        f.write("{invalid_json_content:")

    valid_broker = str(tmp_path / "broker.json")
    with open(valid_broker, "w", encoding="utf-8") as f:
        json.dump({"orders": [], "positions": []}, f)

    is_valid, rejections = specialist.check_portfolio_risk(
        candidate_risk=2000.0,
        capital_required=10000.0,
        portfolio_path=corrupt_port,
        broker_state_path=valid_broker,
        is_live_check=True,
    )
    assert is_valid is False
    assert any("PORTFOLIO_STATE_UNAVAILABLE" in r for r in rejections)


def test_portfolio_risk_valid_empty_portfolio_passes(specialist, tmp_path):
    """Valid empty portfolio with adequate cash passes portfolio check."""
    port_file = str(tmp_path / "portfolio.json")
    with open(port_file, "w", encoding="utf-8") as f:
        json.dump({"cash": 500000.0, "positions": []}, f)

    broker_file = str(tmp_path / "broker.json")
    with open(broker_file, "w", encoding="utf-8") as f:
        json.dump({"orders": [], "positions": []}, f)

    is_valid, rejections = specialist.check_portfolio_risk(
        candidate_risk=2000.0,
        capital_required=10000.0,
        portfolio_path=port_file,
        broker_state_path=broker_file,
        is_live_check=True,
    )
    assert is_valid is True
    assert len(rejections) == 0


def test_portfolio_risk_valid_populated_portfolio_enforces_limits(specialist, tmp_path):
    """Populated portfolio exceeding max open positions limit rejects trade."""
    port_file = str(tmp_path / "portfolio.json")
    with open(port_file, "w", encoding="utf-8") as f:
        # 5 open positions
        positions = [{"symbol": f"SYM_{i}", "shares": 100} for i in range(5)]
        json.dump({"cash": 500000.0, "positions": positions}, f)

    broker_file = str(tmp_path / "broker.json")
    with open(broker_file, "w", encoding="utf-8") as f:
        json.dump({"orders": [], "positions": []}, f)

    is_valid, rejections = specialist.check_portfolio_risk(
        candidate_risk=2000.0,
        capital_required=10000.0,
        portfolio_path=port_file,
        broker_state_path=broker_file,
        is_live_check=True,
    )
    assert is_valid is False
    assert any("MAX_OPEN_POSITIONS_REACHED" in r for r in rejections)


def test_portfolio_risk_override_documented_and_tested(specialist, tmp_path, monkeypatch):
    """Explicit ALLOW_UNVERIFIED_PORTFOLIO=1 override allows missing portfolio but appends audit warning."""
    non_existent = str(tmp_path / "missing.json")
    monkeypatch.setenv("ALLOW_UNVERIFIED_PORTFOLIO", "1")

    is_valid, rejections = specialist.check_portfolio_risk(
        candidate_risk=2000.0,
        capital_required=10000.0,
        portfolio_path=non_existent,
        broker_state_path=non_existent,
        is_live_check=True,
    )
    # Rejection list records the explicit override reason
    assert any("UNVERIFIED_PORTFOLIO_OVERRIDE" in r for r in rejections)


# ------------------------------------------------------------------------------
# 4. P2: CONCURRENT SCAN PROTECTION
# ------------------------------------------------------------------------------
def test_scan_lock_acquisition_and_cleanup(tmp_path):
    """ScanLock acquires atomically, writes PID, and cleans up on exit."""
    from orchestrator import ScanLock, ScanLockError
    lock_file = str(tmp_path / "test_options_scan.lock")

    assert not os.path.exists(lock_file)
    with ScanLock(lock_path=lock_file) as lock:
        assert lock.is_acquired is True
        assert os.path.exists(lock_file)
        with open(lock_file, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        assert data["pid"] == os.getpid()
        assert "timestamp" in data

    assert not os.path.exists(lock_file)


def test_scan_lock_contention_fails_closed(tmp_path):
    """Simultaneous scans on same lock file trigger ScanLockError and return SCAN_BLOCKED."""
    from orchestrator import ScanLock, ScanLockError
    lock_file = str(tmp_path / "test_contention.lock")

    orch1 = MarketIntelligenceOrchestrator(lock_path=lock_file)
    orch2 = MarketIntelligenceOrchestrator(lock_path=lock_file)

    # First lock acquired
    lock1 = ScanLock(lock_path=lock_file)
    lock1.acquire()
    try:
        # Second lock attempt raises ScanLockError
        lock2 = ScanLock(lock_path=lock_file)
        with pytest.raises(ScanLockError) as exc_info:
            lock2.acquire()
        assert "CONCURRENT_SCAN_BLOCKED" in str(exc_info.value)

        # Orchestrator run fails closed safely
        res = orch2.run_market_intelligence(query="NIFTY")
        assert res["status"] == "SCAN_BLOCKED"
        assert "CONCURRENT_SCAN_BLOCKED" in res["reason"]
        assert res["shortlist"] == []
    finally:
        lock1.release()

    assert not os.path.exists(lock_file)


def test_scan_lock_stale_recovery_dead_pid(tmp_path):
    """Stale lock with non-existent dead PID is cleanly recovered."""
    from orchestrator import ScanLock
    lock_file = str(tmp_path / "stale_pid.lock")

    # Write a stale lock file with an absurd dead PID and old timestamp
    stale_payload = {
        "pid": 999999999,
        "timestamp": time.time() - 30.0,
        "acquired_at": "2026-11-17T09:00:00+00:00",
    }
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(stale_payload, f)

    # ScanLock should recognize dead PID and acquire successfully
    with ScanLock(lock_path=lock_file) as lock:
        assert lock.is_acquired is True
        with open(lock_file, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        assert data["pid"] == os.getpid()

    assert not os.path.exists(lock_file)


def test_scan_lock_exception_cleanup(tmp_path):
    """ScanLock context manager guarantees lockfile removal even on uncaught exceptions."""
    from orchestrator import ScanLock
    lock_file = str(tmp_path / "exception_test.lock")

    with pytest.raises(RuntimeError):
        with ScanLock(lock_path=lock_file):
            assert os.path.exists(lock_file)
            raise RuntimeError("Simulated crash during scan")

    # Lock must be released and file deleted despite the exception
    assert not os.path.exists(lock_file)







