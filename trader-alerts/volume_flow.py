"""Volume and Order-Flow Intelligence Engine.

Provides deterministic calculations for:
- Total traded volume & relative volume (RVOL)
- Volume breakout confirmation & volume spikes
- Volume-price divergence & volume trend
- Aggressor volume & Cumulative Volume Delta (CVD) with strict ACTUAL vs ESTIMATED labeling
- Bid/ask spread, market depth, and order-book imbalance

Python 3.8 compatible without third-party dependencies.
"""

from decimal import Decimal, InvalidOperation
import math
from typing import Any, Dict, List, Optional, Tuple


AGGRESSOR_ACTUAL = "ACTUAL"
AGGRESSOR_ESTIMATED = "ESTIMATED"
AGGRESSOR_UNAVAILABLE = "UNAVAILABLE"


def compute_rvol(
    bars: List[Dict[str, Any]],
    target_idx: int,
    lookback: int = 20,
    historical_session_bars: Optional[List[List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Compute Relative Volume (RVOL) strictly avoiding current/future candle lookahead.

    If historical_session_bars (past completed days) are provided, compares against
    the historical average volume for the same intraday time-of-day bucket.
    Otherwise, uses the rolling lookback over completed preceding bars in current session.
    """
    if not bars or target_idx < 0 or target_idx >= len(bars):
        return {
            "rvol": None,
            "current_volume": None,
            "baseline_volume": None,
            "method": "insufficient_data",
            "is_spike": False,
        }

    target_bar = bars[target_idx]
    curr_v = float(target_bar.get("v") or target_bar.get("volume") or 0)

    # Method 1: Historical same-interval time-of-day matching
    if historical_session_bars:
        target_time = target_bar.get("time") or target_bar.get("t")
        same_bucket_vols = []
        for session in historical_session_bars:
            for b in session:
                b_time = b.get("time") or b.get("t")
                if b_time == target_time:
                    v = float(b.get("v") or b.get("volume") or 0)
                    if v > 0:
                        same_bucket_vols.append(v)
                    break
        if same_bucket_vols:
            avg_bucket = sum(same_bucket_vols) / float(len(same_bucket_vols))
            rvol = round(curr_v / avg_bucket, 2) if avg_bucket > 0 else 1.0
            return {
                "rvol": rvol,
                "current_volume": int(curr_v),
                "baseline_volume": int(round(avg_bucket)),
                "method": "historical_time_of_day",
                "samples": len(same_bucket_vols),
                "is_spike": bool(rvol >= 2.0),
            }

    # Method 2: Rolling completed bars strictly preceding target_idx
    start_idx = max(0, target_idx - int(lookback))
    prior_bars = bars[start_idx:target_idx]
    valid_vols = [
        float(b.get("v") or b.get("volume") or 0)
        for b in prior_bars
        if float(b.get("v") or b.get("volume") or 0) > 0
    ]

    if not valid_vols:
        # First candle of session or no prior volume data
        return {
            "rvol": 1.0 if curr_v > 0 else None,
            "current_volume": int(curr_v),
            "baseline_volume": int(curr_v) if curr_v > 0 else None,
            "method": "opening_bar_baseline",
            "is_spike": False,
        }

    avg_prior = sum(valid_vols) / float(len(valid_vols))
    rvol = round(curr_v / avg_prior, 2) if avg_prior > 0 else 1.0

    return {
        "rvol": rvol,
        "current_volume": int(curr_v),
        "baseline_volume": int(round(avg_prior)),
        "method": "rolling_preceding_bars",
        "lookback_used": len(valid_vols),
        "is_spike": bool(rvol >= 2.0),
    }


def compute_volume_analysis(
    bars: List[Dict[str, Any]],
    target_idx: Optional[int] = None,
    rvol_threshold: float = 1.5,
    spike_threshold: float = 2.0,
    lookback: int = 20,
) -> Dict[str, Any]:
    """Analyze volume metrics, breakout confirmation, trend, and divergence."""
    if not bars:
        return {
            "status": "UNAVAILABLE",
            "rvol": None,
            "is_confirmed": False,
            "is_spike": False,
            "volume_trend": "neutral",
            "divergence": "none",
        }

    idx = len(bars) - 1 if target_idx is None else target_idx
    rvol_data = compute_rvol(bars, idx, lookback=lookback)
    rvol = rvol_data.get("rvol")

    is_confirmed = bool(rvol is not None and rvol >= rvol_threshold)
    is_spike = bool(rvol is not None and rvol >= spike_threshold)

    # Volume trend over last 3-5 completed bars
    trend_window = min(5, idx + 1)
    sub = bars[max(0, idx - trend_window + 1):idx + 1]
    vols = [float(b.get("v") or b.get("volume") or 0) for b in sub]
    closes = [float(b.get("c") or b.get("close") or 0) for b in sub]

    vol_trend = "neutral"
    if len(vols) >= 3:
        if vols[-1] > vols[-2] > vols[-3]:
            vol_trend = "increasing"
        elif vols[-1] < vols[-2] < vols[-3]:
            vol_trend = "decreasing"

    # Divergence detection: Price moving higher while volume steadily declines
    divergence = "none"
    if len(closes) >= 4 and len(vols) >= 4:
        price_up = closes[-1] > closes[-3]
        vol_down = vols[-1] < vols[-3]
        price_down = closes[-1] < closes[-3]
        if price_up and vol_down:
            divergence = "bearish_volume_divergence"  # Price up on dying volume
        elif price_down and vol_down:
            divergence = "bullish_volume_divergence"  # Price down on drying volume

    return {
        "status": "OK",
        "current_volume": rvol_data.get("current_volume"),
        "baseline_volume": rvol_data.get("baseline_volume"),
        "rvol": rvol,
        "is_confirmed": is_confirmed,
        "is_spike": is_spike,
        "volume_trend": vol_trend,
        "divergence": divergence,
        "method": rvol_data.get("method"),
    }


def compute_order_flow(
    bars: List[Dict[str, Any]],
    ticks: Optional[List[Dict[str, Any]]] = None,
    allow_estimation: bool = True,
) -> Dict[str, Any]:
    """Compute aggressor volume and Cumulative Volume Delta (CVD).

    MANDATORY INTEGRITY REQUIREMENT:
    - If trade tick aggressor flags exist, outputs AGGRESSOR_ACTUAL.
    - If only OHLCV exists and allow_estimation is True, outputs AGGRESSOR_ESTIMATED using
      the Close Location Value (CLV) model.
    - If allow_estimation is False and no tick data exists, outputs AGGRESSOR_UNAVAILABLE.
    """
    if ticks:
        # Actual Trade-Level Aggressor Volume
        buy_vol = 0.0
        sell_vol = 0.0
        for t in ticks:
            qty = float(t.get("qty") or t.get("quantity") or t.get("v") or 0)
            side = str(t.get("side") or t.get("aggressor") or "").upper()
            if side in ("BUY", "B", "BID_AGRESSOR"):
                buy_vol += qty
            elif side in ("SELL", "S", "ASK_AGRESSOR"):
                sell_vol += qty
            else:
                # Undefined side in tick tape
                buy_vol += qty * 0.5
                sell_vol += qty * 0.5

        delta = buy_vol - sell_vol
        return {
            "aggressor_mode": AGGRESSOR_ACTUAL,
            "is_actual": True,
            "is_estimated": False,
            "buy_volume": int(round(buy_vol)),
            "sell_volume": int(round(sell_vol)),
            "volume_delta": int(round(delta)),
            "cumulative_volume_delta": int(round(delta)),
            "notes": "Computed from actual trade-level execution tape.",
        }

    if not allow_estimation or not bars:
        return {
            "aggressor_mode": AGGRESSOR_UNAVAILABLE,
            "is_actual": False,
            "is_estimated": False,
            "buy_volume": None,
            "sell_volume": None,
            "volume_delta": None,
            "cumulative_volume_delta": None,
            "notes": "Trade-level aggressor side feed unavailable. Estimation disabled.",
        }

    # Documented Estimation Model: Close Location Value (CLV)
    # CLV = ((Close - Low) - (High - Close)) / (High - Low)
    cvd_series = []
    running_cvd = 0.0
    last_buy = 0.0
    last_sell = 0.0
    last_delta = 0.0

    for b in bars:
        h = float(b.get("h") or b.get("high") or 0)
        l = float(b.get("l") or b.get("low") or 0)
        c = float(b.get("c") or b.get("close") or 0)
        v = float(b.get("v") or b.get("volume") or 0)

        rng = h - l
        if rng > 0:
            clv = ((c - l) - (h - c)) / rng  # range [-1.0, 1.0]
        else:
            clv = 0.0

        # Split volume by CLV weight
        buy_weight = (clv + 1.0) / 2.0  # range [0.0, 1.0]
        sell_weight = 1.0 - buy_weight

        b_vol = v * buy_weight
        s_vol = v * sell_weight
        d = b_vol - s_vol

        running_cvd += d
        cvd_series.append(running_cvd)
        last_buy = b_vol
        last_sell = s_vol
        last_delta = d

    return {
        "aggressor_mode": AGGRESSOR_ESTIMATED,
        "is_actual": False,
        "is_estimated": True,
        "estimation_method": "Close Location Value (CLV) Intrabar Pressure",
        "buy_volume": int(round(last_buy)),
        "sell_volume": int(round(last_sell)),
        "volume_delta": int(round(last_delta)),
        "cumulative_volume_delta": int(round(running_cvd)),
        "notes": "ESTIMATED from OHLCV price action. Never represent as actual exchange tape.",
    }


def compute_market_depth_metrics(depth: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute bid-ask spread and order-book imbalance from L2 market depth."""
    if not depth or not isinstance(depth, dict):
        return {
            "status": "UNAVAILABLE",
            "bid_ask_spread": None,
            "bid_ask_spread_pct": None,
            "order_book_imbalance": None,
            "total_bid_qty": None,
            "total_ask_qty": None,
        }

    buy_levels = depth.get("buy") or depth.get("bids") or []
    sell_levels = depth.get("sell") or depth.get("asks") or []

    if not buy_levels or not sell_levels:
        return {
            "status": "INCOMPLETE",
            "bid_ask_spread": None,
            "bid_ask_spread_pct": None,
            "order_book_imbalance": None,
            "total_bid_qty": None,
            "total_ask_qty": None,
        }

    best_bid = float(buy_levels[0].get("price") or 0)
    best_ask = float(sell_levels[0].get("price") or 0)

    spread = round(best_ask - best_bid, 4) if (best_ask and best_bid) else None
    mid = (best_ask + best_bid) / 2.0 if (best_ask and best_bid) else None
    spread_pct = round((spread / mid) * 100.0, 4) if (spread and mid) else None

    total_bid_qty = sum(float(lvl.get("quantity") or lvl.get("qty") or 0) for lvl in buy_levels)
    total_ask_qty = sum(float(lvl.get("quantity") or lvl.get("qty") or 0) for lvl in sell_levels)

    total_qty = total_bid_qty + total_ask_qty
    imbalance = (
        round((total_bid_qty - total_ask_qty) / total_qty, 3)
        if total_qty > 0
        else 0.0
    )

    return {
        "status": "OK",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_ask_spread": spread,
        "bid_ask_spread_pct": spread_pct,
        "total_bid_qty": int(total_bid_qty),
        "total_ask_qty": int(total_ask_qty),
        "order_book_imbalance": imbalance,
        "liquidity_bias": (
            "BUY_HEAVY" if imbalance > 0.2
            else ("SELL_HEAVY" if imbalance < -0.2 else "BALANCED")
        ),
    }

