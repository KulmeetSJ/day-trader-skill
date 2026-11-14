# Day Trader: Centralized Configuration Specification

## 1. Overview
The centralized configuration system provides an authoritative, schema-validated, provenance-tracked configuration mechanism for both Indian (NSE/BSE) and US (NYSE/NASDAQ) markets.

It eliminates scattered, ad-hoc dictionary lookups and guarantees non-negotiable safety rules:
1. **Paper-Only for Indian Markets**: Any attempt to set `paper: false` or a live broker for `market: "IN"` is rejected immediately at schema validation.
2. **Mandatory Net Reward-to-Risk $\ge 2.0$**: Any attempt to configure `min_reward_risk < 2.0` is clamped to $2.0$. For Indian trades, the 2.0 net R requirement is strictly enforced *after* all transaction costs, STT, exchange fees, SEBI charges, stamp duty, GST, and brokerage.
3. **Cryptographic Provenance**: Every loaded configuration is tagged with `_provenance` containing its source file, market profile, ISO timestamp, and a SHA-256 fingerprint hash. Any modification to risk parameters alters this hash, invalidating prior cached order fingerprints.

---

## 2. Configuration Profiles and Precedence

Configurations are resolved in strict precedence:
1. **Runtime Call Overrides**: Explicit kwargs or CLI flags passed to `load_config(overrides={...})`.
2. **Specified or Runtime Config File**: File path passed to `--config` or active runtime config at `trader/config.json`.
3. **Market Template File**: `config/config.in.json` for India or `config/config.us.json` for US.
4. **Authoritative Built-in Defaults**: `IN_DEFAULTS` or `US_DEFAULTS` defined in `config/schema.py`.

---

## 3. Schema Fields and Valid Ranges

### A. Capital and Budget Management
| Field | Type | Default (IN) | Default (US) | Range / Constraint | Description |
|---|---|---|---|---|---|
| `budget` | float | 100,000.0 | 25,000.0 | $\ge 0.0$ | Available trading capital (₹ or $) |
| `cash_buffer_pct` | float | 10.0 | 10.0 | $0.0 - 50.0$ | Percentage of budget kept idle in cash |
| `max_position_pct` | float | 75.0 | 75.0 | $1.0 - 100.0$ | Maximum allocation to any single stock |
| `max_order_inr` | float | 75,000.0 | N/A | $\ge 0.0$ | Maximum single order size in INR |
| `max_order_usd` | float | N/A | 4,000.0 | $\ge 0.0$ | Maximum single order size in USD |
| `min_order_inr` | float | 100.0 | N/A | $\ge 1.0$ | Minimum single order size in INR |
| `min_order_usd` | float | N/A | 5.0 | $\ge 1.0$ | Minimum single order size in USD |
| `max_portfolio_exposure_pct` | float | 80.0 | 80.0 | $1.0 - 100.0$ | Maximum total portfolio exposure |
| `max_open_positions` | int | 3 | 3 | $1 - 20$ | Maximum concurrent open positions |
| `max_trades_per_day` | int | 3 | 3 | $1 - 50$ | Maximum trades permitted per day |
| `max_orders_per_day` | int | 10 | 10 | $1 - 100$ | Maximum order actions per day |
| `max_capital_per_strategy_pct` | float | 40.0 | 40.0 | $1.0 - 100.0$ | Max capital allocated to one strategy |
| `max_sector_exposure_pct` | float | 35.0 | 35.0 | $1.0 - 100.0$ | Max portfolio capital in one sector |
| `max_correlated_exposure_pct` | float | 40.0 | 40.0 | $1.0 - 100.0$ | Max capital in correlated symbols |

### B. Risk Controls and Circuit Breakers
| Field | Type | Default (IN) | Default (US) | Range / Constraint | Description |
|---|---|---|---|---|---|
| `max_loss_per_trade_inr` | float | 2,000.0 | N/A | $\ge 1.0$ | Maximum loss permitted per trade in INR |
| `max_loss_per_trade_usd` | float | N/A | 50.0 | $\ge 1.0$ | Maximum loss permitted per trade in USD |
| `max_daily_loss_inr` | float | 5,000.0 | N/A | $\ge 1.0$ | Daily loss limit in INR (halts trading) |
| `max_daily_loss_usd` | float | N/A | 150.0 | $\ge 1.0$ | Daily loss limit in USD (halts trading) |
| `max_weekly_loss_inr` | float | 15,000.0 | N/A | $\ge 1.0$ | Weekly loss limit in INR |
| `max_weekly_loss_usd` | float | N/A | 450.0 | $\ge 1.0$ | Weekly loss limit in USD |
| `min_reward_risk` | float | 2.0 | 2.0 | Floor: **2.0** | Minimum Net R:R after costs & tick rounding |
| `require_stop` | bool | `true` | `true` | Mandatory `true` | Stop-loss is required on every order |
| `max_consecutive_losses` | int | 3 | 3 | $1 - 10$ | Halts trading after N consecutive losses |
| `max_portfolio_drawdown_pct` | float | 10.0 | 10.0 | $1.0 - 50.0$ | Maximum allowed portfolio drawdown |
| `daily_trading_stop` | bool | `true` | `true` | bool | Stop trading upon hitting daily loss |

### C. Market, Timings & Execution Preferences
| Field | Type | Default (IN) | Default (US) | Description |
|---|---|---|---|---|
| `market` | string | `"IN"` | `"US"` | Market identifier (`"IN"` or `"US"`) |
| `currency` | string | `"INR"` | `"USD"` | Trading currency |
| `exchange` | string | `"NSE"` | `"NASDAQ"` | Target exchange |
| `paper` | bool | `true` (locked) | `true` | Paper trading flag (must be true for IN) |
| `broker` | string | `"paper"` (locked) | `"alpaca"` | Broker route |
| `trading_style` | string | `"intraday"` | `"intraday"` | `"intraday"` or `"positional"` |
| `no_entries_before` | string | `"09:30"` | `"09:45"` | Local time before which entries are blocked |
| `no_entries_after` | string | `"15:00"` | `"15:30"` | Local time after which entries are blocked |
| `flat_by` | string | `"15:20"` | `"15:55"` | Local time intraday positions must be closed |

### D. Options Intelligence Settings
| Field | Type | Default | Description |
|---|---|---|---|
| `buy_ce_enabled` | bool | `true` | Enable Long Call (BUY CE) analysis |
| `buy_pe_enabled` | bool | `true` | Enable Long Put (BUY PE) analysis |
| `sell_ce_enabled` | bool | `false` | Enable Defined-risk Credit Call Spreads |
| `sell_pe_enabled` | bool | `false` | Enable Defined-risk Credit Put Spreads |
| `max_option_lots` | int | `2` | Maximum option contracts/lots per position |
| `strike_selection` | string | `"ATM"` | Strike selection (`"ATM"`, `"ITM1"`, `"OTM1"`) |
| `allowed_expiries` | list | `["current_week", "next_week", "monthly"]` | Permitted option expiry cycles |

---

## 4. CLI Usage

Display current active configuration:
```bash
python3 config/loader.py show --market IN
```

Validate a configuration file:
```bash
python3 config/loader.py validate --config trader/config.json
```

Initialize a safe, production-grade template without credentials:
```bash
python3 config/loader.py init --market IN --out trader/config.json
```

