"""Authoritative configuration loader for Day Trader.

Provides central resolution, provenance tracking, schema validation, and CLI commands:
    python3 config/loader.py show --market IN
    python3 config/loader.py validate --config trader/config.json
    python3 config/loader.py init --market IN --out trader/config.json
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
from typing import Any, Dict, Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from schema import (
    ConfigError,
    IN_DEFAULTS,
    US_DEFAULTS,
    validate_config,
    MANDATORY_MIN_REWARD_RISK,
)

CONFIG_DIR = _DIR
DEFAULT_RUNTIME_CONFIG = os.path.join(os.path.dirname(_DIR), "trader", "config.json")


def get_config_hash(cfg: Dict[str, Any]) -> str:
    """Calculate deterministic cryptographic SHA-256 hash of configuration."""
    filtered = {
        k: v for k, v in cfg.items()
        if k not in ("_provenance", "provenance")
    }
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_config(
    path: Optional[str] = None,
    market: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    must_exist: bool = False,
) -> Dict[str, Any]:
    """Load, merge, and validate configuration with full provenance.

    Precedence order:
    1. Caller overrides
    2. Specified or detected config file (path or trader/config.json)
    3. Market profile template (config.in.json or config.us.json)
    4. Authoritative built-in defaults
    """
    raw_cfg = {}
    resolved_source = "defaults"

    target_path = path or DEFAULT_RUNTIME_CONFIG
    if target_path and os.path.isfile(target_path):
        try:
            with open(target_path, "r", encoding="utf-8") as fh:
                raw_cfg = json.load(fh)
            resolved_source = os.path.abspath(target_path)
        except Exception as e:
            raise ConfigError("Failed to parse config file %s: %s" % (target_path, e))
    elif must_exist:
        raise ConfigError("Config file not found: %s" % target_path)
    else:
        # Check if market-specific template exists in config/
        mkt = str(market or raw_cfg.get("market") or "US").strip().upper()
        tpl_name = "config.in.json" if mkt == "IN" else "config.us.json"
        tpl_path = os.path.join(CONFIG_DIR, tpl_name)
        if os.path.isfile(tpl_path):
            try:
                with open(tpl_path, "r", encoding="utf-8") as fh:
                    raw_cfg = json.load(fh)
                resolved_source = tpl_path
            except Exception:
                pass

    if overrides:
        raw_cfg.update(overrides)

    # Validate and resolve
    validated = validate_config(raw_cfg, market=market)

    cfg_hash = get_config_hash(validated)

    # Add metadata and provenance
    prov = {
        "source": resolved_source,
        "market": validated["market"],
        "config_hash": cfg_hash,
        "loaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "minimum_reward_risk_enforced": MANDATORY_MIN_REWARD_RISK,
        "is_paper": validated.get("paper", True),
    }
    validated["_provenance"] = prov
    validated["provenance"] = prov

    return validated


def cmd_show(args) -> int:
    cfg = load_config(path=args.config, market=args.market)
    if getattr(args, "json", False):
        print(json.dumps(cfg, indent=2))
        return 0

    prov = cfg.get("_provenance", {})
    print("=" * 65)
    print("  DAY TRADER ACTIVE CONFIGURATION")
    print("=" * 65)
    print("  Source:         %s" % prov.get("source"))
    print("  Active Configuration Profile: %s" % cfg.get("market"))
    print("  Currency:       %s" % cfg.get("currency"))
    print("  Mode:           %s (%s)" % ("PAPER" if cfg.get("paper") else "LIVE", cfg.get("broker")))
    print("  Budget:         %s" % (("₹" if cfg.get("market") == "IN" else "$") + str(cfg.get("budget"))))
    print("  Min R:R:        %.2f (mandatory floor: %.2f)" % (cfg.get("min_reward_risk", 2.0), MANDATORY_MIN_REWARD_RISK))
    print("  Max Loss/Trade: %s" % (("₹" if cfg.get("market") == "IN" else "$") + str(cfg.get("max_loss_per_trade_inr") or cfg.get("max_loss_per_trade_usd") or cfg.get("max_loss_per_trade"))))
    print("  Trading Style:  %s (Flat by: %s)" % (cfg.get("trading_style"), cfg.get("flat_by")))
    print("  Options:        BUY_CE=%s, BUY_PE=%s, SELL_CE=%s, SELL_PE=%s (max %s lots)" % (
        cfg.get("buy_ce_enabled"), cfg.get("buy_pe_enabled"), cfg.get("sell_ce_enabled"), cfg.get("sell_pe_enabled"), cfg.get("max_option_lots")
    ))
    print("  Data Provider:  %s" % (cfg.get("india_market_data_provider") if cfg.get("market") == "IN" else cfg.get("us_market_data_provider")))
    print("=" * 65)
    return 0


def cmd_validate(args) -> int:
    try:
        cfg = load_config(path=args.config, market=args.market, must_exist=True)
        print("PASS: Configuration in %s is valid." % cfg["_provenance"]["source"])
        return 0
    except ConfigError as err:
        sys.stderr.write("CONFIG ERROR: %s\n" % err)
        return 1


def cmd_init(args) -> int:
    mkt = str(args.market or "IN").strip().upper()
    defaults = IN_DEFAULTS if mkt == "IN" else US_DEFAULTS
    out_path = args.out or ("trader/config.in.json" if mkt == "IN" else "trader/config.us.json")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(defaults, fh, indent=2)
    print("Created safe default configuration for %s at %s" % (mkt, out_path))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Day Trader Configuration Manager")
    sub = parser.add_subparsers(dest="cmd")

    p_show = sub.add_parser("show", help="Display active configuration")
    p_show.add_argument("--config", help="Path to config JSON")
    p_show.add_argument("--market", choices=["IN", "US"], help="Market profile")
    p_show.add_argument("--json", action="store_true", help="Output as raw JSON")

    p_val = sub.add_parser("validate", help="Validate a configuration file")
    p_val.add_argument("--config", required=True, help="Path to config JSON")
    p_val.add_argument("--market", choices=["IN", "US"], help="Market profile")

    p_init = sub.add_parser("init", help="Initialize a safe example configuration")
    p_init.add_argument("--market", choices=["IN", "US"], default="IN", help="Market profile")
    p_init.add_argument("--out", help="Output file path")

    args = parser.parse_args(argv)
    if args.cmd == "show":
        return cmd_show(args)
    elif args.cmd == "validate":
        return cmd_validate(args)
    elif args.cmd == "init":
        return cmd_init(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
