# Day Trader: Pre-Release Safety & Operational Release Checklist

## 1. Safety Verification Gates

Before running Day Trader for controlled paper trading:

- [x] **Indian Paper-Only Enforcement**: Verify that `config/schema.py` rejects any configuration setting `paper: false` or a live broker for `market: "IN"`.
- [x] **Upstox Read-Only Enforcement**: Verify that `trader-alerts/data_in.py` and `upstox_feed.py` execute GET requests only and contain zero order execution endpoints.
- [x] **Mandatory Net 2R Enforcement**: Verify that `trader-risk/check.py` and `trader-budget/size.py` enforce `net_reward_risk >= 2.0` after all statutory transaction charges and tick rounding.
- [x] **Indian Statutory Cost Deduction**: Verify that STT, NSE exchange fees, SEBI charges, stamp duty, and GST are accurately modeled in position sizing.
- [x] **Authoritative Tick Size Resolution**: Verify that NSE/BSE orders use authoritative ₹0.05 tick steps and that sub-penny prices are rejected.
- [x] **No Naked Short Options**: Verify that `trader-risk/check.py` unconditionally blocks naked short options (undefined risk).
- [x] **Options Execution Block**: Verify that option order sizing in `trader-budget/size.py` raises `SizeError: options are not enabled`.
- [x] **Short Equity Execution Block**: Verify that short order sizing in `trader-budget/size.py` and exit planning in `trader-exit/exit.py` fail closed for `side: "sell"` entries.
- [x] **Truncated SHA-256 Order Fingerprint**: Verify that `order_fingerprint.py` uses truncated unkeyed SHA-256 (24 chars) and invalidates orders if parameters or policy snapshots change.
- [x] **Human Confirmation Gate**: Verify that `trader-broker/ticket.py` refuses approval without exact confirmation text (e.g. `yes buy 10 RELIANCE`).
- [x] **Fail-Closed on Stale/Future Data**: Verify that missing instrument caches, future-dated bars, or stale prices result in immediate rejection.
- [x] **Zero Secret Leakage**: Verify that `.env` and authentication tokens are never printed, persisted in orders, or committed to git.

---

## 2. Test Execution Verification

Execute the complete test suite to confirm baseline stability:

```bash
# 1. Run all unit and integration tests (target >= 728 passed)
python3 -m pytest -v

# 2. Run Centralized Configuration tests
python3 -m pytest tests/test_config.py tests/test_config_integration.py -v

# 3. Run Strategy Decoupling and VWAP Rejection tests
python3 -m pytest tests/test_entry_vwap_rejection.py tests/test_entry_setups_decoupled.py -v

# 4. Run Risk Engine & Safety tests
python3 -m pytest tests/test_risk.py tests/test_bharat_rules_2026.py -v

# 5. Run Upstox Read-Only Provider tests
python3 -m pytest tests/test_upstox_provider.py tests/test_pre_upstox_acceptance.py -v

# 6. Run System Integration & Operational Readiness tests (Scenarios A through H)
python3 -m pytest tests/test_system_integration_readiness.py -v
```

---

## 3. Configuration & CLI Sanity Checks

Verify CLI tools execute without error:

```bash
# Show active configuration profile for India
python3 config/loader.py show --market IN

# Validate configuration file
python3 config/loader.py validate --config config/config.in.json

# Pre-market readiness check
python3 trader-alerts/alerts.py readiness --market IN

# Inspect instrument master cache for Reliance
python3 trader-alerts/instruments.py inspect RELIANCE --exchange NSE
```
