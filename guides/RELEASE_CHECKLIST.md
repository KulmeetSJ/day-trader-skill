# Day Trader: Pre-Release Validation & Safety Checklist

## 1. Safety Verification Gates

Before deploying or running Day Trader:
- [x] **Indian Paper-Only Enforcement**: Verify that `config/schema.py` rejects any configuration setting `paper: false` or a live broker for `market: "IN"`.
- [x] **Mandatory Net 2R Enforcement**: Verify that `trader-risk/check.py` and `trader-budget/size.py` enforce `net_reward_risk >= 2.0` after all statutory transaction charges and tick rounding.
- [x] **No Naked Short Options**: Verify that `trader-risk/check.py` unconditionally blocks naked short options (undefined risk).
- [x] **Defined-Risk Spreads Only**: Verify that all credit option orders include a valid protective leg.
- [x] **Cryptographic Provenance**: Verify that `order_fingerprint` hashes risk configuration and invalidates stale orders upon configuration change.
- [x] **Fail-Closed on Stale/Missing Data**: Verify that missing instrument caches, stale prices, or unclassified symbols result in immediate refusal.
- [x] **Zero Secret Leakage**: Verify that `.env` and authentication tokens are never printed, persisted in orders, or committed to git.

---

## 2. Test Execution Checklist

Run the complete test suite:

```bash
# 1. Run all unit and integration tests
python3 -m pytest

# 2. Run Centralized Configuration tests
python3 -m pytest tests/test_config.py

# 3. Run Volume and Order Flow tests
python3 -m pytest tests/test_volume_flow.py

# 4. Run Risk Engine tests
python3 -m pytest tests/test_risk.py

# 5. Run Repository Structure verification
python3 -m pytest tests/test_repo.py
```

---

## 3. Configuration & CLI Sanity Checks

Verify CLI tools execute without error:

```bash
# Show active configuration profile
python3 config/loader.py show --market IN

# Validate configuration file
python3 config/loader.py validate --config config/config.in.json

# Test volume flow engine
python3 -c "from trader-alerts import volume_flow" # via load
```

