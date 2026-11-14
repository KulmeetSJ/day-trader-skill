#!/usr/bin/env python3
"""
trader-options: Synthetic Options Simulation & Replay Engine.

SYNTHETIC OPTIONS SIMULATION:
Simulates intraday option strategy outcomes (BUY CE, BUY PE, SELL CE, SELL PE)
using underlying spot OHLCV bars and second-order Delta-Gamma Taylor expansion
calibrated to snapshot IV and Greeks.

LIMITATIONS & DISCLOSURE:
This is a synthetic mathematical approximation, NOT authentic historical options
tick-by-tick backtesting. Real historical options execution experiences dynamic IV
crush/expansion, time-decay non-linearities, bid/ask spread widening, and liquidity
slippage that cannot be captured from underlying spot candles alone.
"""

import datetime
import math
import os
import sys
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

for path in [
    os.path.join(BASE_DIR, "trader-alerts"),
    os.path.join(BASE_DIR, "trader-entry"),
    os.path.join(BASE_DIR, "trader-budget"),
    os.path.join(BASE_DIR, "trader-options"),
]:
    if path not in sys.path:
        sys.path.insert(0, path)

import data_in
import instruments
import size
from options import OptionsIntelligenceSpecialist, round_tick, STRATEGY_BUY_CE, STRATEGY_BUY_PE, STRATEGY_SELL_CE, STRATEGY_SELL_PE


class OptionsTimeMachineReplay:
    """
    Synthetic options simulation engine for intraday setups.
    
    DISCLOSURE: Approximates option premium trajectories from spot bars using
    snapshot Greeks. Not an authoritative historical option tick backtest.
    """

    def __init__(self, specialist: Optional[OptionsIntelligenceSpecialist] = None):
        self.specialist = specialist or OptionsIntelligenceSpecialist()

    def replay_session(
        self,
        symbol: str,
        strategy: str,
        bars: List[Dict[str, Any]],
        chain: Any,
        meta: Dict[str, Any],
        slippage_pct: float = 0.5,
        max_loss_budget: float = 2000.0,
    ) -> Dict[str, Any]:
        """
        Replay an intraday trading session bar-by-bar for an options setup.
        """
        lot_size = meta["lot_size"]
        strike_interval = meta.get("strike_interval", 50.0)
        is_seller = strategy in (STRATEGY_SELL_CE, STRATEGY_SELL_PE)
        is_put = "PE" in strategy

        if len(bars) < 5:
            return {
                "strategy": strategy,
                "status": "INSUFFICIENT_BARS",
                "simulation_model": "SYNTHETIC_OPTIONS_SIMULATION",
                "limitations_disclosure": (
                    "SYNTHETIC OPTIONS SIMULATION: Uses underlying spot candles with synthetic option premium "
                    "modeling based on snapshot Greeks and Taylor expansion. Authentic historical option "
                    "backtesting requires tick-level or 1-minute historical option chain archives including "
                    "real bid/ask quotes and historical IV."
                ),
                "trade_count": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "expectancy_inr": 0.0,
                "max_drawdown_inr": 0.0,
                "total_charges_inr": 0.0,
                "net_pnl_inr": 0.0,
                "trades": [],
                "data_coverage": f"{len(bars)} bars",
            }

        # Select Strike based on session open
        spot_open = float(bars[0]["o"])
        atm_strike = round(spot_open / strike_interval) * strike_interval
        strike = atm_strike
        option_type = "PE" if is_put else "CE"

        contract = chain.get_contract(strike, option_type) if chain else None
        base_prem = float(contract.get("last_price", 100.0)) if contract else 100.0
        greeks = (contract.get("greeks") or {}) if contract else {}
        delta = float(greeks.get("delta") or (0.50 if not is_put else -0.50))
        gamma = float(greeks.get("gamma") or 0.001)

        trades = []
        equity_curve = [0.0]
        current_trade = None

        # Simple intraday momentum / breakout condition
        for i in range(2, len(bars)):
            b = bars[i]
            prev_b = bars[i - 1]
            spot_c = float(b["c"])

            if current_trade is None:
                # Check for entry trigger
                triggered = False
                if strategy == STRATEGY_BUY_CE and spot_c > float(prev_b["h"]):
                    triggered = True
                    spot_entry = spot_c
                    spot_stop = float(prev_b["l"])
                    spot_target = spot_entry + (spot_entry - spot_stop) * 1.5
                elif strategy == STRATEGY_BUY_PE and spot_c < float(prev_b["l"]):
                    triggered = True
                    spot_entry = spot_c
                    spot_stop = float(prev_b["h"])
                    spot_target = spot_entry - (spot_stop - spot_entry) * 1.5
                elif strategy == STRATEGY_SELL_CE and spot_c < float(prev_b["l"]):
                    triggered = True
                    spot_entry = spot_c
                    spot_stop = float(prev_b["h"])
                    spot_target = spot_entry - (spot_stop - spot_entry) * 1.0
                elif strategy == STRATEGY_SELL_PE and spot_c > float(prev_b["h"]):
                    triggered = True
                    spot_entry = spot_c
                    spot_stop = float(prev_b["l"])
                    spot_target = spot_entry + (spot_entry - spot_stop) * 1.0

                if triggered:
                    # Model option entry premium
                    raw_entry = base_prem
                    # Apply slippage on entry
                    slip_entry = raw_entry * (1.0 + (slippage_pct / 100.0) if not is_seller else 1.0 - (slippage_pct / 100.0))
                    entry_prem = round_tick(slip_entry, 0.05)

                    levels = self.specialist.model_premium_levels(
                        entry_premium=entry_prem,
                        spot_entry=spot_entry,
                        spot_stop=spot_stop,
                        spot_t1=spot_target,
                        spot_t2=spot_target * 1.02,
                        delta=delta,
                        gamma=gamma,
                        option_type=option_type,
                        is_seller=is_seller,
                    )

                    if not levels:
                        continue

                    # Size lots to risk budget (Phase 1 Fix: Never force 1 lot if risk exceeds budget)
                    risk_per_share = abs(entry_prem - levels["stop_loss_premium"])
                    risk_lot = risk_per_share * lot_size
                    lots = int(max_loss_budget // risk_lot) if risk_lot > 0 else 0
                    if lots <= 0:
                        continue  # Skip trade: Risk per lot exceeds max loss budget
                    qty = lots * lot_size

                    current_trade = {
                        "entry_bar": i,
                        "entry_time": b.get("et") or f"bar_{i}",
                        "spot_entry": spot_entry,
                        "spot_stop": spot_stop,
                        "spot_target": spot_target,
                        "option_entry": entry_prem,
                        "option_stop": levels["stop_loss_premium"],
                        "option_target": levels["target_1_premium"],
                        "qty": qty,
                        "lots": lots,
                    }
            else:
                # Check for exit (target, stop, or flat-by end of session)
                exit_triggered = False
                exit_reason = None
                exit_prem = current_trade["option_entry"]

                # Target check
                if (not is_put and spot_c >= current_trade["spot_target"]) or (is_put and spot_c <= current_trade["spot_target"]):
                    exit_triggered = True
                    exit_reason = "TARGET_HIT"
                    raw_exit = current_trade["option_target"]
                # Stop check
                elif (not is_put and spot_c <= current_trade["spot_stop"]) or (is_put and spot_c >= current_trade["spot_stop"]):
                    exit_triggered = True
                    exit_reason = "STOP_LOSS_HIT"
                    raw_exit = current_trade["option_stop"]
                # End of session cutoff (last bar)
                elif i == len(bars) - 1:
                    exit_triggered = True
                    exit_reason = "FLAT_BY_SESSION_END"
                    dS = spot_c - current_trade["spot_entry"]
                    dP = (delta * dS) + (0.5 * gamma * (dS ** 2))
                    raw_exit = max(0.05, round_tick(current_trade["option_entry"] + dP, 0.05))

                if exit_triggered:
                    # Apply slippage on exit
                    slip_exit = raw_exit * (1.0 - (slippage_pct / 100.0) if not is_seller else 1.0 + (slippage_pct / 100.0))
                    exit_prem = round_tick(slip_exit, 0.05)

                    # Calculate PnL and statutory Indian charges
                    qty = current_trade["qty"]
                    if not is_seller:
                        gross_pnl = (exit_prem - current_trade["option_entry"]) * qty
                    else:
                        gross_pnl = (current_trade["option_entry"] - exit_prem) * qty

                    charges = size.calculate_indian_transaction_charges(
                        asset_type="OPTIONS",
                        side="sell" if is_seller else "buy",
                        entry_price=current_trade["option_entry"],
                        exit_price=exit_prem,
                        qty=qty,
                        trading_style="intraday",
                        strike=strike,
                        option_type=option_type,
                    )
                    tot_charges = charges.get("total", 0.0)
                    net_pnl = round(gross_pnl - tot_charges, 2)

                    current_trade.update({
                        "exit_bar": i,
                        "exit_time": b.get("et") or f"bar_{i}",
                        "exit_reason": exit_reason,
                        "option_exit": exit_prem,
                        "gross_pnl": round(gross_pnl, 2),
                        "total_charges": round(tot_charges, 2),
                        "net_pnl": net_pnl,
                        "is_win": net_pnl > 0,
                    })
                    trades.append(current_trade)
                    equity_curve.append(equity_curve[-1] + net_pnl)
                    current_trade = None

        # Summary statistics
        trade_count = len(trades)
        wins = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"]]
        win_rate = round((len(wins) / trade_count) * 100.0, 1) if trade_count > 0 else 0.0

        total_gross_profit = sum(t["gross_pnl"] for t in wins)
        total_gross_loss = abs(sum(t["gross_pnl"] for t in losses))
        profit_factor = round(total_gross_profit / total_gross_loss, 2) if total_gross_loss > 0 else (99.0 if total_gross_profit > 0 else 0.0)

        total_charges = round(sum(t["total_charges"] for t in trades), 2)
        total_net_pnl = round(sum(t["net_pnl"] for t in trades), 2)
        expectancy = round(total_net_pnl / trade_count, 2) if trade_count > 0 else 0.0

        # Drawdown calculation
        peak = 0.0
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd

        return {
            "symbol": symbol,
            "strategy": strategy,
            "strike": strike,
            "simulation_model": "SYNTHETIC_OPTIONS_SIMULATION",
            "limitations_disclosure": (
                "SYNTHETIC OPTIONS SIMULATION: Uses underlying spot candles with synthetic option premium "
                "modeling based on snapshot Greeks and Taylor expansion. Authentic historical option "
                "backtesting requires tick-level or 1-minute historical option chain archives including "
                "real bid/ask quotes and historical IV."
            ),
            "trade_count": trade_count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "expectancy_inr": expectancy,
            "max_drawdown_inr": round(max_dd, 2),
            "total_statutory_charges_inr": total_charges,
            "net_pnl_inr": total_net_pnl,
            "slippage_assumptions": f"{slippage_pct}% on entry and exit",
            "data_coverage": f"{len(bars)} completed intraday bars",
            "trades": trades,
        }

