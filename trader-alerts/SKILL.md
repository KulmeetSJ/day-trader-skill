---
name: trader-alerts
description: Market Clock, Data, Instrument Master, Readiness, and Price Alerts. Handles US NYSE hours/holidays/early closes and Indian NSE/BSE/CAS trading sessions in ET or IST, checks readiness, syncs and inspects the Indian NSE/BSE/F&O instrument master with canonical INR tick sizes and exchange isolation, fetches quotes and history (Yahoo, Stooq, or read-only Upstox/offline providers), writes trader/prices.json, and evaluates one-shot price/time/plan alerts.
---

# trader-alerts (Market Clock, Indian Data, Instruments & Alerts)

Runs `alerts.py` and `instruments.py`, plus the shared Indian market infrastructure (`calendar_in.py`, `symbols_in.py`, `data_in.py`, `upstox_feed.py`, `env_loader.py`). Pure Python standard library.

Notice that in addition to monitoring stops/targets and writing `trader/prices.json`, `trader-alerts` houses the canonical market clock (`--market US|IN`), pre-flight readiness check, NSE/BSE/F&O instrument master cache, and read-only quote/history providers.

## Market readiness and clocks

```
# US market clock (NYSE hours, 2026-2027 holidays, 1:00 p.m. ET early closes)
python3 <SKILL_DIR>/trader-alerts/alerts.py clock --market US

# Indian NSE/BSE clock (IST; pre-open 09:00-09:08, market 09:15-15:30, post-close / CAS)
python3 <SKILL_DIR>/trader-alerts/alerts.py clock --market IN
python3 <SKILL_DIR>/trader-alerts/alerts.py clock --market IN --symbol RELIANCE --cas --instruments trader/state/instruments.json

# Pre-flight readiness check (config, calendar, instruments cache, data provider, portfolio)
python3 <SKILL_DIR>/trader-alerts/alerts.py readiness --market IN --config trader/config.json --instruments trader/state/instruments.json
```

## Instrument master (`instruments.py`, Indian markets)

Downloads and caches NSE (`EQUITY_L.csv`), F&O lot sizes (`fo_mktlots.csv`), BSE (`Equity.csv`), and Upstox BOD JSON masters with strict provenance and exchange isolation:

```
# Sync instrument master cache (online or from --offline folder)
python3 <SKILL_DIR>/trader-alerts/instruments.py sync --exchange ALL --out trader/state/instruments.json
python3 <SKILL_DIR>/trader-alerts/instruments.py sync --offline <BHARAT_FIXTURE_DIR> --exchange ALL --out trader/state/instruments.json

# Inspect canonical or derivative symbols with explicit exchange resolution
python3 <SKILL_DIR>/trader-alerts/instruments.py inspect RELIANCE --exchange NSE --cache trader/state/instruments.json
python3 <SKILL_DIR>/trader-alerts/instruments.py inspect RELIANCE --exchange BSE --cache trader/state/instruments.json
```

Key instrument invariants:
- Canonical `tick_size` is stored in rupees (`tick_size_unit: "INR"`). Raw Upstox BOD values (reported in paise, e.g. NSE `RELIANCE = 10.0` paise -> `0.10 INR`, BSE `RELIANCE = 5.0` paise -> `0.05 INR`) are converted once at ingestion, while NSE/BSE CSV parsers stamp `"INR"` directly. Unmarked BOD/ambiguous caches fail closed and tell you to re-sync.
- `instruments_by_exchange` keeps NSE and BSE records strictly isolated so exchange-specific tick sizes, ISINs, and CAS flags never overwrite one another.

## Quotes, history, and price alerts (`alerts.py`)

```
# Fetch quotes (writes trader/prices.json with --write for trader-budget & trader-risk)
python3 <SKILL_DIR>/trader-alerts/alerts.py quote AAPL TSLA --write
python3 <SKILL_DIR>/trader-alerts/alerts.py quote RELIANCE TCS --market IN --write

# Fetch historical bars (--market IN supports 1m, 5m, 1d via data_in / Upstox / Yahoo / offline)
python3 <SKILL_DIR>/trader-alerts/alerts.py history RELIANCE --market IN --interval 5m --range 5d --write trader/history.json

# Add alerts (all three from trader/plan.json: stop, target, and flat-by-close --before-close)
python3 <SKILL_DIR>/trader-alerts/alerts.py add --from-plan --plan trader/plan.json --config trader/config.json
python3 <SKILL_DIR>/trader-alerts/alerts.py add --symbol TSLA --stop 358.32 --target 370.04
python3 <SKILL_DIR>/trader-alerts/alerts.py add --opening-bell --market IN --label "NSE open"
python3 <SKILL_DIR>/trader-alerts/alerts.py add --at 15:10 --before-close --market IN --label "Flat by close"

# Check, list, or reset one-shot alerts
python3 <SKILL_DIR>/trader-alerts/alerts.py check --market IN --offline <FIXTURE_DIR> --now "2026-10-19 10:15"
python3 <SKILL_DIR>/trader-alerts/alerts.py list
python3 <SKILL_DIR>/trader-alerts/alerts.py reset <ALERT_ID>
```

Subcommands of `alerts.py`:
- `clock`: `--now`, `--json`, `--market US|IN`, `--cas`, `--symbol`, `--instruments`
- `readiness`: `--market US|IN`, `--config`, `--instruments`, `--portfolio`, `--now`, `--json`
- `quote`: `[symbols ...]`, `--holdings`, `--portfolio`, `--plan`, `--write [PATH]`, `--exchange NSE|BSE`, `--source`, `--config`, `--offline DIR`, `--now`, `--json`, `--market US|IN` (supports equities, F&O symbols, and Indian benchmark indices like `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `SENSEX`)
- `history`: `symbol`, `--exchange NSE|BSE`, `--interval 1m|5m|15m|1h|1d`, `--range 1d|5d|1mo|1y`, `--write [PATH]`, `--chart PATH` (opt-in SVG from fetched bars), `--source`, `--config`, `--offline DIR`, `--now`, `--json`, `--market US|IN`
- `visuals.py`: standalone opt-in SVG chart CLI (`candlestick`, `entry-exit`, `indices`, `options-payoff`, `portfolio`, `risk`, `backtest`, `fundamentals`, `news`) reusing saved JSON files; follow `guides/trader-visuals.md` and generate charts ONLY when explicitly requested.
- `check`: `--rules`, `--state`, `--prices-out`, `--no-write`, `--source`, `--config`, `--offline DIR`, `--now`, `--json`, `--market US|IN`
- `add`: `--rules`, `--state`, `--symbol`, (`--above` | `--below` | `--stop` | `--target` | `--pct` | `--opening-bell` | `--at HH:MM` | `--from-plan`), `--plan`, `--config`, `--label`, `--before-close`, `--date`, `--direction up|down`, `--rearm`, `--note`, `--id`, `--market US|IN`
- `list`: `--rules`, `--state`, `--market US|IN`
- `reset`: `id`, `--state`, `--market US|IN`

## Environment variables (read from process env or workspace `.env`)

- Market/provider selection: `BHARAT_MARKET`, `INDIA_MARKET_DATA_PROVIDER`, `BHARAT_DATA_PROVIDER`, `INDIA_MARKET_DATA_FALLBACK_PROVIDER`, `INDIA_MARKET_DATA_ALLOW_YAHOO_FALLBACK`, `INDIA_OFFLINE_DIR`, `TRADER_CONFIG`, `SEC_USER_AGENT`
- Upstox (strictly read-only market data): `UPSTOX_ANALYTICS_TOKEN`, `UPSTOX_ACCOUNT_TIER` (`standard` = max 2 WS connections, `plus` = max 5 WS connections)

## Rules and provenance

- Never run silently on simulated `--now` or `--offline` fixtures while reporting live status: inspect and report `provider`, `data_status`, `is_live`, and `last_data_timestamp`.
- Free Yahoo/Stooq quotes are unofficial and delayed (`~15m`). Do not claim real-time execution guarantees from delayed quotes.
- An alert firing is informational only -- it never auto-places an order.

## Safety

- Education and market monitoring tools, not financial advice.
- Upstox integration is strictly read-only for market data and never places orders. Order placement happens only in `trader-broker` after `trader-risk` PASS today and an explicit typed `yes` from the user.
- `rules_from_plan` (`alerts.py add --from-plan`) honors raised `trailing_stop` values when present in `trader/plan.json`, updating the active stop alert while keeping alert evaluation purely informational.
- Keep `UPSTOX_ANALYTICS_TOKEN` and all broker credentials in local `.env` or process environment variables only. Never paste or ask for tokens or secrets in chat.
