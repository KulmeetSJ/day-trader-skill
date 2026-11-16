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

Out of scope (not touched in this stage): sell_ce_enabled/sell_pe_enabled (per
guides/CONFIGURATION_SPEC.md these gate not-yet-implemented defined-risk credit spreads, not the
naked SELL_CE/SELL_PE strategies that are actually generated today -- wiring them against naked
selling would be a misreading of the documented contract and would break the existing, correctly
designed SELL_CE/SELL_PE acceptance tests), spread payoff architecture, liquidity threshold
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
)

MOCK_OPEN = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.timezone.utc)  # Tuesday 10:30 IST


@pytest.fixture
def specialist():
    return OptionsIntelligenceSpecialist()


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
