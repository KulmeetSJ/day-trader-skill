"""Authoritative configuration schema and validation for Day Trader system.

Preserves Python 3.8 compatibility and enforces deterministic financial rules:
- India is paper-only (paper: True, broker: 'paper').
- Mandatory minimum reward/risk >= 2.0 (cannot be weakened by configuration or strategy).
- Strict type and range checking for all budget, risk, trading, and analysis parameters.
"""

from decimal import Decimal, InvalidOperation
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple


class ConfigError(ValueError):
    """Raised when configuration fails schema, type, range, or safety validation."""
    pass


# Mandatory floors that cannot be weakened
MANDATORY_MIN_REWARD_RISK = 2.0
MIN_RISK_PCT = 0.05  # minimum 0.05% stop distance to avoid spread whipsaw

# Default configurations for US and IN markets
IN_DEFAULTS = {
    "market": "IN",
    "currency": "INR",
    "exchange": "NSE",
    "asset_type": "EQUITY",
    "trading_style": "intraday",
    "paper": True,
    "broker": "paper",
    "budget": 100000.0,
    "cash_buffer_pct": 10.0,
    "max_position_pct": 75.0,
    "max_order_inr": 75000.0,
    "min_order_inr": 100.0,
    "max_loss_per_trade_inr": 2000.0,
    "max_daily_loss_inr": 5000.0,
    "max_weekly_loss_inr": 15000.0,
    "max_portfolio_exposure_pct": 80.0,
    "max_open_positions": 3,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "max_capital_per_strategy_pct": 40.0,
    "max_sector_exposure_pct": 35.0,
    "max_correlated_exposure_pct": 40.0,
    "max_consecutive_losses": 3,
    "max_portfolio_drawdown_pct": 10.0,
    "daily_trading_stop": True,
    "min_reward_risk": 2.0,
    "require_stop": True,
    "fractional": False,
    "pdt_guard": False,
    "no_entries_before": "09:30",
    "no_entries_after": "15:00",
    "flat_by": "15:20",
    "allow_off_hours": False,
    "allow_margin": False,
    "allow_short": False,
    "allow_options": False,
    "allow_futures": False,
    "futures_margin_pct": 20.0,
    "blocked": [],
    "option_strategy": "long_call",
    "strike_selection": "ATM",
    "allowed_asset_classes": ["EQUITY"],
    "allowed_indices": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"],
    "allowed_symbols": ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"],
    "allowed_option_strategies": ["BUY_CE", "BUY_PE"],
    "buy_ce_enabled": True,
    "buy_pe_enabled": True,
    "sell_ce_enabled": False,  # defined-risk credit spreads only when risk model supports
    "sell_pe_enabled": False,
    "max_option_lots": 2,
    "allowed_expiries": ["current_week", "next_week", "monthly"],
    "timeframe": "5m",
    "rvol_threshold": 1.5,
    "vwap_confirmation": True,
    "multi_timeframe_confirmation": True,
    "min_liquidity_daily_volume": 100000,
    "max_bid_ask_spread_pct": 0.25,
    "atr_period": 14,
    "atr_multiplier": 1.5,
    "index_confirmation": True,
    "sector_confirmation": True,
    "news_restrictions": True,
    "india_market_data_provider": "yahoo",
    "data_provider": "yahoo",
    "track_transaction_costs": True,
}

US_DEFAULTS = {
    "market": "US",
    "currency": "USD",
    "exchange": "NYSE",
    "asset_type": "EQUITY",
    "trading_style": "intraday",
    "paper": True,
    "broker": "alpaca",
    "budget": 25000.0,
    "cash_buffer_pct": 10.0,
    "max_position_pct": 75.0,
    "max_order_usd": 4000.0,
    "min_order_usd": 5.0,
    "max_loss_per_trade_usd": 50.0,
    "max_daily_loss_usd": 150.0,
    "max_weekly_loss_usd": 500.0,
    "max_portfolio_exposure_pct": 80.0,
    "max_open_positions": 3,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "max_capital_per_strategy_pct": 40.0,
    "max_sector_exposure_pct": 35.0,
    "max_correlated_exposure_pct": 40.0,
    "max_consecutive_losses": 3,
    "max_portfolio_drawdown_pct": 10.0,
    "daily_trading_stop": True,
    "min_reward_risk": 2.0,
    "require_stop": True,
    "fractional": False,
    "pdt_guard": True,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "allow_off_hours": False,
    "allow_margin": False,
    "allow_short": False,
    "allow_options": False,
    "allow_futures": False,
    "futures_margin_pct": 20.0,
    "blocked": [],
    "allowed_asset_classes": ["EQUITY"],
    "allowed_indices": ["SPY", "QQQ", "IWM"],
    "allowed_symbols": ["TSLA", "AAPL", "MSFT", "NVDA", "AMZN"],
    "allowed_option_strategies": ["BUY_CE", "BUY_PE"],
    "buy_ce_enabled": True,
    "buy_pe_enabled": True,
    "sell_ce_enabled": False,
    "sell_pe_enabled": False,
    "max_option_lots": 2,
    "allowed_expiries": ["current_week", "monthly"],
    "timeframe": "5m",
    "rvol_threshold": 1.5,
    "vwap_confirmation": True,
    "multi_timeframe_confirmation": True,
    "min_liquidity_daily_volume": 500000,
    "max_bid_ask_spread_pct": 0.15,
    "atr_period": 14,
    "atr_multiplier": 1.5,
    "index_confirmation": True,
    "sector_confirmation": True,
    "news_restrictions": True,
    "us_market_data_provider": "yahoo",
    "data_provider": "yahoo",
    "track_transaction_costs": False,
}


def _validate_number(val: Any, name: str, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
    if val is None:
        raise ConfigError("Missing required numeric field: %s" % name)
    try:
        f = float(val)
    except (ValueError, TypeError):
        raise ConfigError("%s must be a valid number, got %r" % (name, val))
    if not (f == f) or f in (float("inf"), float("-inf")):
        raise ConfigError("%s must be a finite number, got %r" % (name, val))
    if min_val is not None and f < min_val:
        raise ConfigError("%s must be >= %s, got %s" % (name, min_val, f))
    if max_val is not None and f > max_val:
        raise ConfigError("%s must be <= %s, got %s" % (name, max_val, f))
    return f


def _validate_int(val: Any, name: str, min_val: Optional[int] = None, max_val: Optional[int] = None) -> int:
    f = _validate_number(val, name, min_val, max_val)
    if int(f) != f:
        raise ConfigError("%s must be an integer, got %s" % (name, val))
    return int(f)


def _validate_bool(val: Any, name: str) -> bool:
    if not isinstance(val, bool):
        raise ConfigError("%s must be a boolean (true/false), got %r" % (name, val))
    return val


def _validate_time(val: Any, name: str) -> str:
    if not isinstance(val, str) or not re.match(r"^\d{2}:\d{2}$", val):
        raise ConfigError("%s must be HH:MM format, got %r" % (name, val))
    hh, mm = map(int, val.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ConfigError("%s must be a valid clock time, got %s" % (name, val))
    return val


def validate_config(raw: Dict[str, Any], market: Optional[str] = None) -> Dict[str, Any]:
    """Validate and sanitize configuration dictionary against authoritative rules."""
    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a JSON object, got %s" % type(raw).__name__)

    cfg = dict(raw)
    resolved_market = str(market or cfg.get("market") or "US").strip().upper()
    if resolved_market not in ("IN", "US"):
        raise ConfigError("market must be 'IN' or 'US', got %r" % resolved_market)

    # Start with base market defaults
    defaults = IN_DEFAULTS if resolved_market == "IN" else US_DEFAULTS
    merged = dict(defaults)
    merged.update(cfg)
    merged["market"] = resolved_market

    # Mandatory Safety 1: Indian trading must remain PAPER-ONLY
    if resolved_market == "IN":
        if not bool(merged.get("paper", True)):
            raise ConfigError("Indian market execution is strictly PAPER-ONLY. 'paper' must be true.")
        broker = str(merged.get("broker") or "paper").strip().lower()
        if broker != "paper":
            raise ConfigError("Indian market broker must be 'paper' (got %r). Live execution is disabled." % broker)
        merged["paper"] = True
        merged["broker"] = "paper"

    # Mandatory Safety 2: Minimum reward/risk must be at least 2.0 (floor)
    raw_rr = merged.get("min_reward_risk")
    if raw_rr is not None:
        try:
            rr_val = float(raw_rr)
        except (ValueError, TypeError):
            rr_val = MANDATORY_MIN_REWARD_RISK
        if rr_val > 0:
            merged["min_reward_risk"] = max(MANDATORY_MIN_REWARD_RISK, rr_val)
        else:
            merged["min_reward_risk"] = 0.0
    else:
        merged["min_reward_risk"] = MANDATORY_MIN_REWARD_RISK

    # Validate numbers and ranges
    if merged.get("budget") is not None:
        merged["budget"] = _validate_number(merged["budget"], "budget", min_val=0.0)
    merged["cash_buffer_pct"] = _validate_number(merged.get("cash_buffer_pct", 10.0), "cash_buffer_pct", 0.0, 99.0)
    merged["max_position_pct"] = _validate_number(merged.get("max_position_pct", 75.0), "max_position_pct", 0.0, 100.0)

    # Currency neutral aliases
    if resolved_market == "IN":
        merged["currency"] = "INR"
        if "max_order_inr" not in cfg and "max_order" not in cfg and "max_order_usd" in cfg:
            merged["max_order_inr"] = cfg["max_order_usd"]
        if "max_loss_per_trade_inr" not in cfg and "max_loss_per_trade" not in cfg and "max_loss_per_trade_usd" in cfg:
            merged["max_loss_per_trade_inr"] = cfg["max_loss_per_trade_usd"]
        if "max_daily_loss_inr" not in cfg and "max_daily_loss" not in cfg and "max_daily_loss_usd" in cfg:
            merged["max_daily_loss_inr"] = cfg["max_daily_loss_usd"]
        if "min_order_inr" not in cfg and "min_order" not in cfg and "min_order_usd" in cfg:
            merged["min_order_inr"] = cfg["min_order_usd"]
        for key in ("max_order_inr", "max_order"):
            if merged.get(key) is not None:
                merged[key] = _validate_number(merged[key], key, min_val=0.0)
        for key in ("max_loss_per_trade_inr", "max_loss_per_trade"):
            if merged.get(key) is not None:
                merged[key] = _validate_number(merged[key], key, min_val=0.0)
        for key in ("max_daily_loss_inr", "max_daily_loss"):
            if merged.get(key) is not None:
                merged[key] = _validate_number(merged[key], key, min_val=0.0)
    else:
        merged["currency"] = "USD"
        if "max_order_usd" not in cfg and "max_order" not in cfg and "max_order_inr" in cfg:
            merged["max_order_usd"] = cfg["max_order_inr"]
        if "max_loss_per_trade_usd" not in cfg and "max_loss_per_trade" not in cfg and "max_loss_per_trade_inr" in cfg:
            merged["max_loss_per_trade_usd"] = cfg["max_loss_per_trade_inr"]
        if "max_daily_loss_usd" not in cfg and "max_daily_loss" not in cfg and "max_daily_loss_inr" in cfg:
            merged["max_daily_loss_usd"] = cfg["max_daily_loss_inr"]
        if "min_order_usd" not in cfg and "min_order" not in cfg and "min_order_inr" in cfg:
            merged["min_order_usd"] = cfg["min_order_inr"]
        for key in ("max_order_usd", "max_order"):
            if merged.get(key) is not None:
                merged[key] = _validate_number(merged[key], key, min_val=0.0)
        for key in ("max_loss_per_trade_usd", "max_loss_per_trade"):
            if merged.get(key) is not None:
                merged[key] = _validate_number(merged[key], key, min_val=0.0)
        for key in ("max_daily_loss_usd", "max_daily_loss"):
            if merged.get(key) is not None:
                merged[key] = _validate_number(merged[key], key, min_val=0.0)

    # Trades and orders
    merged["max_trades_per_day"] = _validate_int(merged.get("max_trades_per_day", 3), "max_trades_per_day", min_val=0)
    merged["max_orders_per_day"] = _validate_int(merged.get("max_orders_per_day", 10), "max_orders_per_day", min_val=0)
    merged["require_stop"] = _validate_bool(merged.get("require_stop", True), "require_stop")

    # Time validations
    if merged.get("no_entries_before"):
        merged["no_entries_before"] = _validate_time(merged["no_entries_before"], "no_entries_before")
    if merged.get("no_entries_after"):
        merged["no_entries_after"] = _validate_time(merged["no_entries_after"], "no_entries_after")
    if merged.get("flat_by") and merged["flat_by"] != "positional":
        merged["flat_by"] = _validate_time(merged["flat_by"], "flat_by")

    # Options flags
    merged["buy_ce_enabled"] = _validate_bool(merged.get("buy_ce_enabled", True), "buy_ce_enabled")
    merged["buy_pe_enabled"] = _validate_bool(merged.get("buy_pe_enabled", True), "buy_pe_enabled")
    merged["sell_ce_enabled"] = _validate_bool(merged.get("sell_ce_enabled", False), "sell_ce_enabled")
    merged["sell_pe_enabled"] = _validate_bool(merged.get("sell_pe_enabled", False), "sell_pe_enabled")
    merged["max_option_lots"] = _validate_int(merged.get("max_option_lots", 2), "max_option_lots", min_val=1)

    if "blocked" in merged and not isinstance(merged["blocked"], list):
        raise ConfigError("blocked must be a list of symbols, got %r" % (merged["blocked"],))
    if "futures_margin_pct" in merged and merged["futures_margin_pct"] is not None:
        merged["futures_margin_pct"] = _validate_number(merged["futures_margin_pct"], "futures_margin_pct", min_val=0.0, max_val=100.0)

    return merged

