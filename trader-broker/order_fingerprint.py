"""Canonical order and risk-context fingerprints shared by risk and broker.

Only hashes are persisted or displayed. Financial inputs are converted to
canonical decimal strings before deterministic JSON serialization.
"""

from decimal import Decimal, InvalidOperation
import hashlib
import json


POLICY_DEFAULTS = {
    "market": None,
    "exchange": None,
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_usd": 4000,
    "max_order": None,
    "max_order_inr": None,
    "min_order_usd": 5,
    "min_order": None,
    "min_order_inr": None,
    "max_loss_per_trade_usd": 50,
    "max_loss_per_trade": None,
    "max_loss_per_trade_inr": None,
    "max_daily_loss_usd": 150,
    "max_daily_loss": None,
    "max_daily_loss_inr": None,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 2.0,
    "require_stop": True,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "paper": True,
    "broker": None,
    "blocked": [],
    "allow_options": False,
    "allow_futures": True,
    "allow_margin": False,
    "allow_short": False,
    "allow_off_hours": False,
    "pdt_guard": False,
    "trading_style": None,
    "broker_supports_multi_session": None,
    "supports_multi_session_protection": None,
}

MINIMUM_REWARD_RISK = Decimal("2.0")


def effective_minimum_reward_risk(value):
    """Return the configured minimum without permitting a value below 2R."""
    try:
        requested = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return float(MINIMUM_REWARD_RISK)
    if not requested.is_finite():
        return float(MINIMUM_REWARD_RISK)
    return float(max(MINIMUM_REWARD_RISK, requested))

_IDENTITY_KEYS = (
    "symbol", "market", "exchange", "currency", "asset_type", "instrument_type", "segment",
    "instrument_key", "underlying", "expiry", "strike", "option_type",
    "lot_size", "tick_size", "tick_size_unit", "product", "currency",
)


def _decimal_text(value):
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None
    if not dec.is_finite():
        return None
    if dec == 0:
        return "0"
    text = format(dec.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical(value, key=None):
    if isinstance(value, dict):
        return {str(k): _canonical(v, str(k)) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        text = _decimal_text(value)
        return text if text is not None else str(value)
    if isinstance(value, str) and key in _NUMERIC_KEYS:
        text = _decimal_text(value)
        if text is not None:
            return text
    return value


_NUMERIC_KEYS = frozenset((
    "qty", "quantity", "notional", "entry", "entry_price", "entry_ref",
    "stop", "stop_price", "stop_loss_stop_price", "stop_loss_limit_price",
    "target", "target_price", "take_profit_limit_price", "limit_price",
    "trail_price", "trail_percent", "tick_size", "strike", "lot_size",
    "max_loss", "max_loss_per_trade", "max_daily_loss", "max_order",
    "min_order", "budget", "cash_buffer_pct", "max_position_pct",
    "min_reward_risk", "risk_per_share", "risk_per_unit", "value",
))


def policy_snapshot(config=None):
    """Return the single, shared set of risk-policy values used in fingerprints."""
    out = dict(POLICY_DEFAULTS)
    if isinstance(config, dict):
        out.update(_without_secrets(config))
    out["min_reward_risk"] = effective_minimum_reward_risk(out.get("min_reward_risk"))
    return _canonical(out)


def canonical_overrides(overrides=None):
    """Normalize invocation overrides without adding policy defaults."""
    return _canonical(_without_secrets(overrides or {}))


def _without_secrets(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            low = str(key).lower().replace("-", "_")
            if any(part in low for part in ("token", "secret", "password", "credential")):
                continue
            if low.endswith("_key") or low in ("key", "keys"):
                continue
            clean[str(key)] = _without_secrets(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_without_secrets(item) for item in value]
    return value


def resolved_identity(order, instrument=None):
    """Build a compact identity snapshot from validated master data and order data."""
    source = instrument if isinstance(instrument, dict) else {}
    plan = order.get("plan") if isinstance(order, dict) and isinstance(order.get("plan"), dict) else {}
    contract = order.get("option_contract") if isinstance(order, dict) and isinstance(order.get("option_contract"), dict) else {}
    result = {}
    for key in _IDENTITY_KEYS:
        value = source.get(key)
        if value is None:
            value = order.get(key) if isinstance(order, dict) else None
        if value is None:
            value = contract.get(key)
        if value is None:
            value = plan.get(key)
        if value is not None:
            result[key] = value
    # Keep derivative identity from the provider/master record when available.
    for key in ("option_contract", "derivative_identity", "contract_id"):
        value = source.get(key) or (order.get(key) if isinstance(order, dict) else None)
        if value is not None:
            result[key] = value
    return _canonical(result)


def fingerprint(order, config=None, market=None, identity=None):
    """Hash the full order, applicable policy, market, and resolved identity."""
    order_value = dict(order) if isinstance(order, dict) else order
    payload = {
        "schema": 1,
        "order": _canonical(order_value),
        "market": _canonical(market),
        "risk_parameters": policy_snapshot(config),
        "resolved_instrument_identity": _canonical(
            identity if identity is not None else resolved_identity(order_value)
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
