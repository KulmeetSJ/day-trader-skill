"""
Stage 2 regression suite: naked-option structural prohibition, options config-gate wiring,
and the max_option_lots sizing cap.

Covers:
1. NAKED_OPTION_PROHIBITED is unconditional for SELL_CE/SELL_PE -- it does not depend on
   margin_info_1_lot["is_authoritative"], so it cannot silently disappear if a future
   authoritative-margin integration makes that check pass.
2. buy_ce_enabled / buy_pe_enabled gate BUY_CE / BUY_PE generation in analyze_symbol_for_options
   (previously dead configuration).
3. max_option_lots is enforced as a hard final sizing cap (previously dead configuration).
4. Expiry-day circuit breaker, regime disclosure (non-blocking), correlated-exposure limits,
   and Bear Call / Bull Put credit spreads (gated by sell_ce_enabled/sell_pe_enabled, default
   False, wiring up the config fields that were previously declared but unimplemented).

Out of scope (not touched): Iron Condor (needs a 4-leg combined-margin model and a range-bound
trigger condition this system doesn't have yet -- see trader-options/SKILL.md), sector-level
correlation (no verified sector classification data exists in this repo), liquidity threshold
calibration against real chain data (see DEFAULT_MIN_OPEN_INTEREST / DEFAULT_SPREAD_TOLERANCE_PCT
comments in options.py for the interim-tightening rationale).
"""

import datetime
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
from options import (  # noqa: E402
    OptionsIntelligenceSpecialist,
    STRATEGY_BUY_CE,
    STRATEGY_BUY_PE,
    STRATEGY_SELL_CE,
    STRATEGY_SELL_PE,
    STRATEGY_BEAR_CALL_SPREAD,
    STRATEGY_BULL_PUT_SPREAD,
)
from orchestrator import MarketIntelligenceOrchestrator  # noqa: E402

MOCK_OPEN = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)  # Tuesday 10:30 IST


@pytest.fixture
def specialist():
    return OptionsIntelligenceSpecialist()


@pytest.fixture
def orchestrator():
    return MarketIntelligenceOrchestrator()


def _sell_ce_signal(specialist):
    return specialist.generate_signal_contract(
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
            "tier": "REAL_TIME",
            "quote_timestamp": "2026-11-17T10:30:00+05:30",
            "retrieval_timestamp": "2026-11-17T10:30:01+05:30",
            "greeks": {"delta": 0.50, "gamma": 0.001},
        },
        underlying_setup={
            "setup": "breakdown_intraday",
            "side": "sell",
            "price": 25150.0,
            "invalidation_level": 25160.0,
            "candle_timestamp": "2026-11-17T10:30:00+05:30",
        },
        now_utc=MOCK_OPEN,
        available_capital=500000.0,
        max_loss_budget=2000.0,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )


# ---------------------------------------------------------------- 1. Naked option prohibition

def test_naked_sell_ce_rejected_unconditionally(specialist):
    sig = _sell_ce_signal(specialist)
    assert any(r.startswith("NAKED_OPTION_PROHIBITED") for r in sig["rejection_reasons"])
    assert sig["is_actionable"] is False


def test_naked_sell_ce_still_rejected_even_if_margin_becomes_authoritative(specialist, monkeypatch):
    """
    The prohibition must not rely on margin_info["is_authoritative"] being False. Simulate a
    future authoritative-margin integration and confirm NAKED_OPTION_PROHIBITED still fires.
    """
    real_eval = specialist.evaluate_margin_and_capital

    def patched(*args, **kwargs):
        info = real_eval(*args, **kwargs)
        info["is_authoritative"] = True
        return info

    monkeypatch.setattr(specialist, "evaluate_margin_and_capital", patched)
    sig = _sell_ce_signal(specialist)
    assert any(r.startswith("NAKED_OPTION_PROHIBITED") for r in sig["rejection_reasons"])
    assert not any(r.startswith("AUTHORITATIVE_MARGIN_UNAVAILABLE") for r in sig["rejection_reasons"])
    assert sig["is_actionable"] is False


# ---------------------------------------------------------------- 2. Config gates for BUY_CE / BUY_PE

def test_buy_ce_disabled_suppresses_buy_ce_signal(specialist):
    specialist.config["buy_ce_enabled"] = False
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"
    assert not any(s["strategy"] == STRATEGY_BUY_CE for s in res["signals"])


def test_buy_ce_enabled_default_still_produces_buy_ce_signal(specialist):
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"
    assert any(s["strategy"] == STRATEGY_BUY_CE for s in res["signals"])


def test_buy_pe_disabled_suppresses_buy_pe_signal(specialist):
    specialist.config["buy_pe_enabled"] = False
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"
    assert not any(s["strategy"] == STRATEGY_BUY_PE for s in res["signals"])


# ---------------------------------------------------------------- 3. max_option_lots sizing cap

def test_max_option_lots_caps_final_sizing(specialist):
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
        now_utc=MOCK_OPEN,
        max_loss_budget=2000.0,
        available_capital=1000000.0,  # large enough that capital/risk tiers alone would exceed the cap
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    ps = sig["position_sizing"]
    # Without a lot cap, risk-budget/capital tiers alone would permit more than 2 lots here
    # (theoretical/risk_budget tiers comfortably exceed 2 given the available budget/capital).
    assert ps["theoretical_lots"] > 2 or ps["risk_budget_permitted_lots"] > 2 or ps["capital_permitted_lots"] > 2
    assert ps["lots"] <= 2
    assert ps["final_permitted_lots"] <= 2


def test_max_option_lots_cap_is_config_driven(specialist):
    specialist.config["max_option_lots"] = 1
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
        now_utc=MOCK_OPEN,
        max_loss_budget=2000.0,
        available_capital=1000000.0,
        meta={"lot_size": 65, "expiry": "2026-11-17"},
    )
    assert sig["position_sizing"]["lots"] <= 1


# ---------------------------------------------------------------- 4. Open interest liquidity floor

def test_open_interest_floor_rejects_illiquid_contract(specialist):
    ok, reason, metrics = specialist.validate_liquidity({
        "bid": 100.0, "ask": 100.5, "last_price": 100.2, "volume": 10000, "open_interest": 500,
    })
    assert ok is False
    assert "Open interest" in reason
    assert metrics["open_interest"] == 500


def test_open_interest_floor_accepts_liquid_contract(specialist):
    ok, reason, metrics = specialist.validate_liquidity({
        "bid": 100.0, "ask": 100.5, "last_price": 100.2, "volume": 10000, "open_interest": 25000,
    })
    assert ok is True
    assert reason is None


def test_open_interest_floor_is_config_driven(specialist):
    specialist.config["options_min_open_interest"] = 1000
    ok, _, _ = specialist.validate_liquidity({
        "bid": 100.0, "ask": 100.5, "last_price": 100.2, "volume": 10000, "open_interest": 5000,
    })
    assert ok is True


# ---------------------------------------------------------------- 5. Expiry-day circuit breaker

def test_expiry_day_after_1430_ist_blocks_new_entries(specialist):
    """Mirrors trader-risk/check.py's expiry-day cutoff, now enforced in trader-options too."""
    after_cutoff = datetime.datetime(2026, 11, 17, 9, 15, tzinfo=datetime.timezone.utc)  # 14:45 IST
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75, "ask": 137.75, "last_price": 137.75,
            "tier": "REAL_TIME", "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},  # expiry == now's date
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=after_cutoff,
    )
    assert any(r.startswith("EXPIRY_DAY_CUTOFF") for r in sig["rejection_reasons"])
    assert sig["is_actionable"] is False


def test_expiry_day_before_1430_ist_does_not_block(specialist):
    before_cutoff = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)  # 10:30 IST
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75, "ask": 137.75, "last_price": 137.75,
            "tier": "REAL_TIME", "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
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
        now_utc=before_cutoff,
    )
    assert not any(r.startswith("EXPIRY_DAY_CUTOFF") for r in sig["rejection_reasons"])


def test_non_expiry_day_after_1430_ist_does_not_block(specialist):
    """Sanity check: the cutoff is specific to the expiry date, not every day after 14:30 IST."""
    later_non_expiry = datetime.datetime(2026, 11, 16, 9, 15, tzinfo=datetime.timezone.utc)  # 14:45 IST, Mon
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75, "ask": 137.75, "last_price": 137.75,
            "tier": "REAL_TIME", "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-17"},  # expiry is tomorrow, not today
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=later_non_expiry,
    )
    assert not any(r.startswith("EXPIRY_DAY_CUTOFF") for r in sig["rejection_reasons"])


# ---------------------------------------------------------------- 6. Regime disclosure (non-blocking)

def test_regime_context_is_disclosed_and_never_blocking(specialist, monkeypatch):
    """A misaligned regime must be visible in regime_context but never added to rejection_reasons."""
    monkeypatch.setattr(specialist, "analyze_symbol_for_options", specialist.analyze_symbol_for_options)
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75, "ask": 137.75, "last_price": 137.75,
            "tier": "REAL_TIME", "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",  # bullish setup
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-23"},  # not expiry day -- isolates the regime test
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc),
        broad_regime="STRONG_BEARISH",  # explicitly misaligned with a bullish breakout setup
    )
    rc = sig["regime_context"]
    assert rc["broad_market_regime"] == "STRONG_BEARISH"
    assert rc["regime_aligned"] is False
    assert "STRONG_BEARISH" in rc["regime_note"]
    assert "breakout_intraday" in rc["regime_note"]
    # Never a blocking reason, regardless of alignment:
    assert not any("REGIME" in r for r in sig["rejection_reasons"])
    assert "STRONG_BEARISH" in sig["selection_rationale"]


def test_regime_context_aligned_case(specialist):
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75, "ask": 137.75, "last_price": 137.75,
            "tier": "REAL_TIME", "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-23"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc),
        broad_regime="BULLISH",
    )
    rc = sig["regime_context"]
    assert rc["regime_aligned"] is True


def test_regime_context_unknown_regime_is_disclosed_not_fabricated(specialist):
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 136.75, "ask": 137.75, "last_price": 137.75,
            "tier": "REAL_TIME", "is_live": True,
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-23"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc),
        broad_regime=None,  # unavailable -- must not be fabricated as aligned or misaligned
    )
    rc = sig["regime_context"]
    assert rc["regime_aligned"] is None
    assert rc["broad_market_regime"] == "UNCERTAIN"


# ---------------------------------------------------------------- 7. get_broad_market_regime() accepts a datetime

def test_get_broad_market_regime_accepts_datetime_now():
    """
    Regression: get_broad_market_regime(now=<datetime>) previously crashed with
    AttributeError('datetime.datetime' object has no attribute 'strip') because it routed
    a datetime through parse_now(), which only accepts strings/None. All real callers
    (orchestrator.py, analyze_symbol_for_options) pass an already-parsed datetime, not a string.
    """
    import scan
    now_utc = datetime.datetime(2026, 11, 16, 9, 10, tzinfo=datetime.timezone.utc)
    res = scan.get_broad_market_regime(market="IN", now=now_utc)
    assert res["canonical_regime"] in (
        "STRONG_BULLISH", "BULLISH", "SIDEWAYS", "BEARISH", "STRONG_BEARISH",
        "HIGH_VOLATILITY", "UNCERTAIN",
    )
    # A string --now value (CLI-style) must still work too.
    res_str = scan.get_broad_market_regime(market="IN", now="2026-11-16 14:40")
    assert res_str["canonical_regime"] in (
        "STRONG_BULLISH", "BULLISH", "SIDEWAYS", "BEARISH", "STRONG_BEARISH",
        "HIGH_VOLATILITY", "UNCERTAIN",
    )


# ---------------------------------------------------------------- 8. Correlated exposure limit

def _fake_sig(underlying, strategy, cap_req_per_lot, net_risk_per_lot=100.0, lots=5):
    return {
        "underlying_instrument": underlying,
        "strategy": strategy,
        "strike": 25000.0,
        "signal_state": "SIGNAL VALIDATED",
        "is_actionable": True,
        "ranking_score": 50.0,
        "direction": "BULLISH",
        "position_sizing": {
            "lot_size": 1,
            "net_risk_per_lot_inr": net_risk_per_lot,
            "capital_required_per_lot": cap_req_per_lot,
            "final_permitted_lots": lots,
        },
        "rejection_reasons": [],
    }


def test_correlated_indices_share_one_exposure_cap(orchestrator):
    """NIFTY and BANKNIFTY are both in the broad-index correlation group -- their capital
    requirements must be checked cumulatively against max_correlated_exposure_pct, not independently."""
    orchestrator.specialist.config["max_correlated_exposure_pct"] = 40.0
    available_capital = 100000.0  # 40% cap => Rs 40,000 shared across the whole index family
    sig_nifty = _fake_sig("NIFTY", "BUY_CE", cap_req_per_lot=15000.0, lots=5)      # wants 75,000
    sig_banknifty = _fake_sig("BANKNIFTY", "BUY_CE", cap_req_per_lot=15000.0, lots=5)  # wants 75,000

    shortlist, rejected = orchestrator.allocate_portfolio_across_shortlist(
        shortlist=[sig_nifty, sig_banknifty],
        budget=1_000_000.0,  # make risk-budget a non-binding constraint for this test
        available_capital=available_capital,
    )
    # NIFTY (evaluated first) gets capped at floor(40000/15000)=2 lots, consuming the whole group cap.
    assert sig_nifty["position_sizing"]["lots"] == 2
    assert sig_nifty in shortlist
    # BANKNIFTY has zero room left in the same correlation group -- rejected, not silently resized
    # to fit remaining *overall* capital (there's plenty of overall capital left; only the
    # correlated-group cap is exhausted).
    assert sig_banknifty in rejected
    assert any(r.startswith("MAX_CORRELATED_EXPOSURE_EXCEEDED") for r in sig_banknifty["rejection_reasons"])


def test_uncorrelated_symbols_do_not_share_exposure_cap(orchestrator):
    """Two unrelated stock symbols must not be treated as the same correlation group."""
    orchestrator.specialist.config["max_correlated_exposure_pct"] = 40.0
    available_capital = 100000.0
    sig_a = _fake_sig("RELIANCE", "BUY_CE", cap_req_per_lot=15000.0, lots=5)
    sig_b = _fake_sig("INFY", "BUY_CE", cap_req_per_lot=15000.0, lots=5)

    shortlist, rejected = orchestrator.allocate_portfolio_across_shortlist(
        shortlist=[sig_a, sig_b],
        budget=1_000_000.0,
        available_capital=available_capital,
    )
    # Each symbol gets its own 40%-of-capital allowance since they're different correlation
    # groups -- neither should be rejected purely for correlation (overall capital still binds
    # eventually, but not via MAX_CORRELATED_EXPOSURE_EXCEEDED for either).
    assert not any(r.startswith("MAX_CORRELATED_EXPOSURE_EXCEEDED") for r in sig_a.get("rejection_reasons", []))
    assert not any(r.startswith("MAX_CORRELATED_EXPOSURE_EXCEEDED") for r in sig_b.get("rejection_reasons", []))


def test_correlated_exposure_cap_is_config_driven(orchestrator):
    """A looser configured limit must permit more combined exposure in the same group."""
    orchestrator.specialist.config["max_correlated_exposure_pct"] = 90.0  # loosened
    available_capital = 100000.0
    sig_nifty = _fake_sig("NIFTY", "BUY_CE", cap_req_per_lot=15000.0, lots=5)
    sig_banknifty = _fake_sig("BANKNIFTY", "BUY_CE", cap_req_per_lot=15000.0, lots=5)

    shortlist, rejected = orchestrator.allocate_portfolio_across_shortlist(
        shortlist=[sig_nifty, sig_banknifty],
        budget=1_000_000.0,
        available_capital=available_capital,
    )
    # 90% of 100,000 = 90,000 shared; NIFTY takes floor(90000/15000)=6 but capped at its own
    # final_permitted_lots=5, leaving 90000-75000=15000 i.e. 1 lot room for BANKNIFTY.
    assert sig_nifty["position_sizing"]["lots"] == 5
    assert sig_banknifty["position_sizing"]["lots"] == 1
    assert sig_banknifty in shortlist


# ---------------------------------------------------------------- 9. Credit spreads (Bear Call / Bull Put)

def test_credit_spreads_off_by_default(specialist):
    """sell_ce_enabled/sell_pe_enabled default False -- credit spreads must not appear unasked."""
    res = specialist.analyze_symbol_for_options("NIFTY")
    assert res["status"] == "OPPORTUNITIES_IDENTIFIED"
    strategies = {s["strategy"] for s in res["signals"]}
    assert "BEAR_CALL_SPREAD" not in strategies
    assert "BULL_PUT_SPREAD" not in strategies


def test_bull_put_credit_spread_generated_when_enabled_with_correct_leg_structure(specialist):
    specialist.config["sell_pe_enabled"] = True
    res = specialist.analyze_symbol_for_options("NIFTY")
    bps = [s for s in res["signals"] if s["strategy"] == STRATEGY_BULL_PUT_SPREAD]
    assert len(bps) >= 1
    sig = bps[0]
    primary = next(l for l in sig["legs"] if l["role"] == "PRIMARY")
    hedge = next(l for l in sig["legs"] if l["role"] == "HEDGE")
    assert primary["side"] == "SELL" and primary["option_type"] == "PE"
    assert hedge["side"] == "BUY" and hedge["option_type"] == "PE"
    assert hedge["strike"] < primary["strike"]  # protective leg strictly below the sold strike
    assert sig["margin_details"]["structure"] == "DEFINED_RISK_SPREAD"
    # Must NOT be treated as naked -- it has a genuine protective leg.
    assert not any(r.startswith("NAKED_OPTION_PROHIBITED") for r in sig["rejection_reasons"])


def test_bear_call_credit_spread_generated_when_enabled_with_correct_leg_structure(specialist):
    specialist.config["sell_ce_enabled"] = True
    res = specialist.analyze_symbol_for_options("NIFTY")
    bcs = [s for s in res["signals"] if s["strategy"] == STRATEGY_BEAR_CALL_SPREAD]
    assert len(bcs) >= 1
    sig = bcs[0]
    primary = next(l for l in sig["legs"] if l["role"] == "PRIMARY")
    hedge = next(l for l in sig["legs"] if l["role"] == "HEDGE")
    assert primary["side"] == "SELL" and primary["option_type"] == "CE"
    assert hedge["side"] == "BUY" and hedge["option_type"] == "CE"
    assert hedge["strike"] > primary["strike"]  # protective leg strictly above the sold strike
    assert sig["margin_details"]["structure"] == "DEFINED_RISK_SPREAD"
    assert not any(r.startswith("NAKED_OPTION_PROHIBITED") for r in sig["rejection_reasons"])


def test_credit_spread_max_loss_and_max_profit_are_internally_consistent(specialist):
    """max_loss + max_profit must equal strike width x lot size (the defined-risk property)."""
    specialist.config["sell_pe_enabled"] = True
    res = specialist.analyze_symbol_for_options("NIFTY")
    bps = [s for s in res["signals"] if s["strategy"] == STRATEGY_BULL_PUT_SPREAD]
    assert len(bps) >= 1
    sig = bps[0]
    md = sig["margin_details"]
    width = abs(sig["legs"][0]["strike"] - sig["legs"][1]["strike"])
    lot_size = sig["legs"][0]["lot_size"]
    max_loss_val = float(md["max_loss"].replace("Rs ", "").replace(",", ""))
    max_profit_val = float(md["max_profit"].replace("Rs ", "").replace(",", ""))
    assert abs((max_loss_val + max_profit_val) - (width * lot_size)) < 0.01


# ---------------------------------------------------------------- 10. Entry-range band (chase protection)

def test_entry_range_defaults_to_2pct_band_on_tight_spread(specialist):
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 137.70, "ask": 137.80, "last_price": 137.75,  # tight spread (~0.07%)
            "is_live": True, "tier": "REAL_TIME",
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-23"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=MOCK_OPEN,
    )
    er = sig["option_entry_range"]
    assert er["band_pct"] == 2.0  # floor applies since spread is tighter than 2%
    assert er["min"] < er["ideal"] < er["max"]
    assert "skip this trade" in er["instruction"]


def test_entry_range_widens_to_match_a_wide_spread(specialist):
    sig = specialist.generate_signal_contract(
        underlying="NIFTY",
        direction="BULLISH",
        strategy=STRATEGY_BUY_CE,
        option_type="CE",
        strike=25150.0,
        contract_data={
            "symbol": "NIFTY2026111725150CE",
            "bid": 130.0, "ask": 145.0, "last_price": 137.5,  # ~10.9% spread -- illiquid
            "is_live": True, "tier": "REAL_TIME",
            "greeks": {"delta": 0.51, "gamma": 0.001, "theta": -3.0},
        },
        underlying_setup={
            "setup": "breakout_intraday",
            "price": 25200.0,
            "invalidation_level": 25050.0,
            "targets": {"target_1_1r": 25550.0, "target_2_2r": 25750.0},
        },
        meta={"lot_size": 65, "expiry": "2026-11-23"},
        max_loss_budget=15000.0,
        min_reward_risk=2.0,
        now_utc=MOCK_OPEN,
    )
    er = sig["option_entry_range"]
    # Band must widen to the observed spread (not stay flat at 2%), since a flat 2% band on an
    # illiquid option would look "violated" by its own normal bid/ask noise.
    assert er["band_pct"] > 2.0

    def _spread_pct(bid, ask):
        mid = (bid + ask) / 2.0
        return round(((ask - bid) / mid) * 100.0, 2)

    assert er["band_pct"] == _spread_pct(130.0, 145.0)
