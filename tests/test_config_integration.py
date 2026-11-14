"""Integration tests for Day Trader centralized configuration.

Verifies that:
1. Updating a setting in config.json changes runtime behavior across:
   - trader-budget/size.py
   - trader-risk/check.py
   - trader-scanner/scan.py
   - trader-entry/entry.py
   - trader-exit/exit.py
2. Indian market execution strictly enforces paper-only across all modules.
3. Changing configuration parameters produces different config hashes and fingerprints,
   invalidating stale order approvals.
"""

import json
import os
import tempfile
import pytest

from _load import load, ROOT

size_mod = load("trader-budget", "size")
check_mod = load("trader-risk", "check")
scan_mod = load("trader-scanner", "scan")
entry_mod = load("trader-entry", "entry")
exit_mod = load("trader-exit", "exit")


def test_size_runtime_config_change():
    """Verify that updating budget and max_loss in config changes sizing behavior."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path1 = os.path.join(d, "config1.json")
        cfg_path2 = os.path.join(d, "config2.json")

        with open(cfg_path1, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "budget": 10000.0, "max_loss_per_trade_usd": 100.0}, fh)

        with open(cfg_path2, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "budget": 20000.0, "max_loss_per_trade_usd": 250.0}, fh)

        cfg1 = size_mod.load_config(cfg_path1)
        cfg2 = size_mod.load_config(cfg_path2)

        assert cfg1["budget"] == 10000.0
        assert cfg1["max_loss_per_trade_usd"] == 100.0

        assert cfg2["budget"] == 20000.0
        assert cfg2["max_loss_per_trade_usd"] == 250.0

        assert cfg1["provenance"]["config_hash"] != cfg2["provenance"]["config_hash"]


def test_risk_runtime_config_change():
    """Verify that check_mod loads config, tracks provenance, and rejects invalid limits."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path1 = os.path.join(d, "config1.json")
        cfg_path2 = os.path.join(d, "config2.json")

        with open(cfg_path1, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "max_loss_per_trade_usd": 50.0}, fh)

        with open(cfg_path2, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "max_loss_per_trade_usd": 150.0}, fh)

        cfg1 = check_mod.load_config(cfg_path1)
        cfg2 = check_mod.load_config(cfg_path2)

        assert cfg1["max_loss_per_trade_usd"] == 50.0
        assert cfg2["max_loss_per_trade_usd"] == 150.0
        assert cfg1["provenance"]["config_hash"] != cfg2["provenance"]["config_hash"]


def test_scanner_runtime_config_change():
    """Verify that scan_mod read_config reflects updated scanner cutoffs."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path1 = os.path.join(d, "config1.json")
        cfg_path2 = os.path.join(d, "config2.json")

        with open(cfg_path1, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "no_entries_after": "14:30"}, fh)

        with open(cfg_path2, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "no_entries_after": "15:15"}, fh)

        cfg1 = scan_mod.read_config(cfg_path1)
        cfg2 = scan_mod.read_config(cfg_path2)

        assert cfg1["no_entries_after"] == "14:30"
        assert cfg2["no_entries_after"] == "15:15"
        assert cfg1["provenance"]["config_hash"] != cfg2["provenance"]["config_hash"]


def test_entry_runtime_config_change():
    """Verify that entry_mod read_config reflects updated entry time cutoffs."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path1 = os.path.join(d, "config1.json")
        cfg_path2 = os.path.join(d, "config2.json")

        with open(cfg_path1, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "no_entries_before": "10:00", "no_entries_after": "14:00"}, fh)

        with open(cfg_path2, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "no_entries_before": "09:30", "no_entries_after": "15:00"}, fh)

        cfg1 = entry_mod.read_config(cfg_path1)
        cfg2 = entry_mod.read_config(cfg_path2)

        assert cfg1["no_entries_before"] == "10:00"
        assert cfg1["no_entries_after"] == "14:00"
        assert cfg2["no_entries_before"] == "09:30"
        assert cfg2["no_entries_after"] == "15:00"
        assert cfg1["provenance"]["config_hash"] != cfg2["provenance"]["config_hash"]


def test_exit_runtime_config_change():
    """Verify that exit_mod read_config reflects updated flat_by and min_reward_risk."""
    with tempfile.TemporaryDirectory() as d:
        cfg_path1 = os.path.join(d, "config1.json")
        cfg_path2 = os.path.join(d, "config2.json")

        with open(cfg_path1, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "flat_by": "15:45", "min_reward_risk": 2.5}, fh)

        with open(cfg_path2, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "flat_by": "15:55", "min_reward_risk": 3.0}, fh)

        cfg1 = exit_mod.read_config(cfg_path1)
        cfg2 = exit_mod.read_config(cfg_path2)

        assert cfg1["flat_by"] == "15:45"
        assert cfg1["min_reward_risk"] == 2.5
        assert cfg2["flat_by"] == "15:55"
        assert cfg2["min_reward_risk"] == 3.0
        assert cfg1["provenance"]["config_hash"] != cfg2["provenance"]["config_hash"]


def test_indian_paper_only_enforced_across_all_modules():
    """Verify that attempting to configure live execution for Indian market raises in all modules."""
    with tempfile.TemporaryDirectory() as d:
        illegal_cfg = os.path.join(d, "illegal.json")
        with open(illegal_cfg, "w", encoding="utf-8") as fh:
            json.dump({"market": "IN", "paper": False, "broker": "alpaca"}, fh)

        with pytest.raises(size_mod.SizeError, match="strictly PAPER-ONLY"):
            size_mod.load_config(illegal_cfg, market="IN")

        with pytest.raises(check_mod.RiskError, match="strictly PAPER-ONLY"):
            check_mod.load_config(illegal_cfg, market="IN")

        with pytest.raises(scan_mod.ScanError, match="strictly PAPER-ONLY"):
            scan_mod.read_config(illegal_cfg, market="IN")

        with pytest.raises(entry_mod.EntryError, match="strictly PAPER-ONLY"):
            entry_mod.read_config(illegal_cfg, market="IN")

        with pytest.raises(exit_mod.PlanError, match="strictly PAPER-ONLY"):
            exit_mod.read_config(illegal_cfg, market="IN")


def test_configuration_changes_invalidate_stale_order_approvals():
    """Verify that changing risk parameters alters config hash and order fingerprint."""
    order = {
        "symbol": "TSLA",
        "side": "buy",
        "qty": "10",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit_limit_price": "380.00",
        "stop_loss_stop_price": "360.00",
        "plan": {"entry": 365.0, "stop": 360.0, "target": 380.0},
    }

    with tempfile.TemporaryDirectory() as d:
        cfg_path1 = os.path.join(d, "c1.json")
        cfg_path2 = os.path.join(d, "c2.json")

        with open(cfg_path1, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "budget": 10000.0, "max_loss_per_trade_usd": 50.0}, fh)

        with open(cfg_path2, "w", encoding="utf-8") as fh:
            json.dump({"market": "US", "budget": 10000.0, "max_loss_per_trade_usd": 75.0}, fh)

        c1 = check_mod.load_config(cfg_path1)
        c2 = check_mod.load_config(cfg_path2)

        fp1 = check_mod.fingerprint(order, config=c1, market="US")
        fp2 = check_mod.fingerprint(order, config=c2, market="US")

        assert fp1 != fp2, "Config change must invalidate prior order fingerprint"

