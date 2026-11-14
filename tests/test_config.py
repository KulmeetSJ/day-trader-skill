import json
import os
import subprocess
import sys
import tempfile
import pytest

from config.loader import load_config, get_config_hash
from config.schema import validate_config, ConfigError, MANDATORY_MIN_REWARD_RISK


def test_load_default_in_config():
    cfg = load_config(market="IN")
    assert cfg["market"] == "IN"
    assert cfg["currency"] == "INR"
    assert cfg["exchange"] == "NSE"
    assert cfg["paper"] is True
    assert cfg["broker"] == "paper"
    assert cfg["min_reward_risk"] >= 2.0
    assert "provenance" in cfg
    assert cfg["provenance"]["market"] == "IN"


def test_load_default_us_config():
    cfg = load_config(market="US")
    assert cfg["market"] == "US"
    assert cfg["currency"] == "USD"
    assert cfg["min_reward_risk"] >= 2.0
    assert "provenance" in cfg


def test_min_reward_risk_cannot_be_under_mandatory():
    # If user config provides reward_risk 1.5, it must be clamped or rejected
    cfg = validate_config({"market": "IN", "min_reward_risk": 1.2})
    assert cfg["min_reward_risk"] == MANDATORY_MIN_REWARD_RISK


def test_india_paper_only_enforced():
    # Attempting to set paper: False on market: IN must fail
    with pytest.raises(ConfigError, match="Indian market execution is strictly PAPER-ONLY"):
        validate_config({"market": "IN", "paper": False})

    with pytest.raises(ConfigError, match="Indian market broker must be 'paper'"):
        validate_config({"market": "IN", "broker": "alpaca"})


def test_config_hash_changes_on_overrides():
    cfg1 = load_config(market="IN", overrides={"budget": 50000.0})
    cfg2 = load_config(market="IN", overrides={"budget": 100000.0})
    assert cfg1["provenance"]["config_hash"] != cfg2["provenance"]["config_hash"]


def test_strategies_json_validity():
    strategies_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "strategies.json")
    assert os.path.isfile(strategies_path)
    with open(strategies_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    strategies = data.get("strategies", {})
    assert len(strategies) >= 10
    required_names = [
        "breakout", "breakout_20d", "orb", "vwap_reclaim", "vwap_rejection",
        "ema_alignment", "rsi_momentum", "trend_continuation", "pullback", "reversal"
    ]
    for name in required_names:
        assert name in strategies, f"Strategy {name} missing from strategies.json"
        s = strategies[name]
        assert "eligible_market_regimes" in s
        assert "ineligible_market_regimes" in s
        assert s["min_reward_risk"] >= 2.0


def test_cli_config_commands():
    loader_py = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "loader.py")
    # Test show
    res = subprocess.run([sys.executable, loader_py, "show", "--market", "IN"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "Active Configuration Profile: IN" in res.stdout

    # Test init and validate
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_cfg = os.path.join(tmpdir, "test_config.json")
        res_init = subprocess.run([sys.executable, loader_py, "init", "--market", "US", "--out", tmp_cfg], capture_output=True, text=True)
        assert res_init.returncode == 0
        assert os.path.isfile(tmp_cfg)

        res_val = subprocess.run([sys.executable, loader_py, "validate", "--config", tmp_cfg], capture_output=True, text=True)
        assert res_val.returncode == 0
        assert "PASS: Configuration" in res_val.stdout
