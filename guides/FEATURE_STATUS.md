# Day Trader: Feature Status & Implementation Matrix

| Feature Area | Specific Capability | Status | Implementation Location | Test Coverage | Known Limitations |
|---|---|---|---|---|---|
| **Configuration** | Schema validation & type checking | Implemented | `config/schema.py` | `tests/test_config.py` | None |
| **Configuration** | Central loader & provenance hash | Implemented | `config/loader.py` | `tests/test_config.py` | None |
| **Configuration** | Paper-only India enforcement | Implemented | `config/schema.py` | `tests/test_config.py` | Cannot be overridden |
| **Configuration** | Mandatory Net 2R floor clamp | Implemented | `config/schema.py` | `tests/test_config.py` | Clamped to $\ge 2.0$ |
| **Configuration** | Strategy registry & regime rules | Implemented | `config/strategies.json` | `tests/test_config.py` | 10 core strategies |
| **Market Data** | Yahoo Finance provider | Implemented | `trader-alerts/data_in.py` | `tests/test_bharat_data.py` | ~15m delayed feed |
| **Market Data** | Upstox read-only provider | Implemented | `trader-alerts/data_in.py` | `tests/test_upstox_provider.py` | Token required |
| **Market Data** | Offline snapshot provider | Implemented | `trader-alerts/data_in.py` | `tests/test_bharat_data.py` | Fixed test snapshots |
| **Market Data** | Standardized payload metadata | Enhanced | `trader-alerts/data_in.py` | `tests/test_bharat_provider_abstraction.py` | None |
| **Market Data** | Capability matrix documentation | Implemented | `guides/DATA_SOURCE_MATRIX.md` | Doc verification | None |
| **Market Regime** | Index scanning (NIFTY/BANKNIFTY) | Implemented | `trader-scanner/scan.py` | `tests/test_index_and_visuals.py` | Analytical spot only |
| **Market Regime** | 6-regime classification | Enhanced | `trader-scanner/scan.py` | `tests/test_index_and_visuals.py` | Rule-based deterministic |
| **Market Regime** | Index divergence detection | Implemented | `trader-scanner/scan.py` | `tests/test_index_and_visuals.py` | Large-cap vs midcap |
| **Volume & Flow** | Relative Volume (RVOL) | Implemented | `trader-alerts/volume_flow.py` | `tests/test_volume_flow.py` | Avoids lookahead bias |
| **Volume & Flow** | Aggressor volume & CVD | Implemented | `trader-alerts/volume_flow.py` | `tests/test_volume_flow.py` | ACTUAL vs ESTIMATED |
| **Volume & Flow** | Volume-price divergence | Implemented | `trader-alerts/volume_flow.py` | `tests/test_volume_flow.py` | 4-bar divergence check |
| **Volume & Flow** | Market depth & book imbalance | Implemented | `trader-alerts/volume_flow.py` | `tests/test_volume_flow.py` | Requires L2 depth |
| **Strategy Engine**| 10 Intraday Strategies | Implemented | `trader-entry/entry.py` | `tests/test_entry.py` | Long bias default |
| **Strategy Engine**| Opportunity ranking formula | Implemented | `guides/STRATEGY_SPEC.md` | Specification | Deterministic composite |
| **Options Engine** | BUY CE & BUY PE (Long calls/puts)| Implemented | `trader-alerts/symbols_in.py` | `tests/test_bharat_derivatives.py` | Spot mapped strikes |
| **Options Engine** | Defined-risk Credit Spreads | Implemented | `trader-risk/check.py` | `tests/test_bharat_options_risk.py` | Protective leg required |
| **Options Engine** | Naked short options prohibition | Implemented | `trader-risk/check.py` | `tests/test_bharat_options_risk.py` | Blocked unconditionally |
| **Options Engine** | Option Greeks & IV solver | Implemented | `trader-alerts/data_in.py` | `tests/test_bharat_derivatives.py` | Black-Scholes analytic |
| **Options Engine** | 7-stage state machine | Implemented | `guides/OPTIONS_RISK_SPEC.md` | Specification | None |
| **Risk Engine** | Pre-trade order checks | Implemented | `trader-risk/check.py` | `tests/test_risk.py` | Fail-closed |
| **Risk Engine** | Cryptographic order fingerprint | Implemented | `trader-risk/order_fingerprint.py` | `tests/test_ticket_provenance.py` | SHA-256 canonical |
| **Risk Engine** | Consecutive losses circuit stop | Enhanced | `trader-risk/check.py` | `tests/test_risk.py` | Streak cooled off |
| **Risk Engine** | Weekly loss limit | Enhanced | `trader-risk/check.py` | `tests/test_risk.py` | Calendar week basis |
| **Risk Engine** | Sector exposure limit | Enhanced | `trader-risk/check.py` | `tests/test_risk.py` | 35% cap default |
| **Risk Engine** | Indian Net 2R cost deduction | Enhanced | `trader-risk/check.py` | `tests/test_bharat_rules_2026.py` | Statutory charges |
| **Replay & Backtest**| Monte Carlo 10,000 session replay | Implemented | `trader-timemachine/timemachine.py` | `tests/test_timemachine.py` | Seed-stable |
| **Replay & Backtest**| Historical replay metrics (PF/DD) | Enhanced | `trader-timemachine/timemachine.py` | `tests/test_timemachine.py` | Sharpe, Win Rate, PF |
| **Paper Broker** | FIFO lot accounting | Implemented | `trader-broker/ticket.py` | `tests/test_broker.py` | Paper portfolio |
| **Paper Broker** | Realized P&L log tracking | Enhanced | `trader-broker/ticket.py` | `tests/test_broker.py` | Preserved in state |

