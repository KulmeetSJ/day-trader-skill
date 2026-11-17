# Day Trader: Comprehensive Feature Status & Specialist Skill Audit

This document provides the authoritative, evidence-based feature status for Day Trader, auditing all 12 specialist skills, their exact input/output contracts, dependencies, test coverage, and non-negotiable operational boundaries.

---

## 1. Global System Operational Limitations

The following constraints are non-negotiable across the entire Day Trader architecture:

1. **Indian Equity Trading is Strictly Long-Only Paper Trading**: Live execution in the Indian market (NSE/BSE) is hard-blocked at the schema level (`config/schema.py`). Downstream sizing and fill accounting support only long (`side: "buy"`) trades.
2. **Bearish VWAP Rejection is Analytical Only**: `trader-entry/entry.py` evaluates short-side geometry (`side: "sell"`), but downstream execution planning in `trader-exit/exit.py` and sizing in `trader-budget/size.py` reject short trades fail-closed.
3. **Options BUY CE is Analytical Only (path 1)**: `options_orb` in `trader-entry/entry.py` emits analytical call alerts. Option order sizing in `trader-budget/size.py` is hard-disabled (`SizeError: options are not enabled`). This item describes path 1 only -- see item 4 and §2.12 below for `trader-options` (path 2).
4. **Options BUY PE, SELL CE/PE, and Multi-Leg Spreads: two different surfaces, two different statuses**: In the `trader-entry`/`trader-exit`/`trader-budget`/`trader-risk` order-execution pipeline (path 1), BUY PE, option selling, and multi-leg spreads are unsupported/blocked, and naked short options are explicitly rejected by `trader-risk/check.py` (`side == "sell"` -> "options selling / naked short options are prohibited"). Separately, `trader-options/` (path 2, `OptionsIntelligenceSpecialist` -- see §2.12) is a standalone, non-execution-connected decision-support engine that **does** implement BUY_PE, SELL_CE, SELL_PE, and two debit spreads (Bull Call Spread, Bear Put Spread) as analytical signals. Naked SELL_CE/SELL_PE there are evaluated and fully disclosed but are unconditionally rejected by that engine's own `NAKED_OPTION_PROHIBITED` check (not `trader-risk/check.py`'s, which path 2 never calls) and can never be marked actionable. Credit spreads and Iron Condor remain unimplemented in both paths. See `trader-options/SKILL.md` for the authoritative per-strategy status.
5. **Upstox Integration is Strictly Read-Only**: Upstox REST endpoints and WebSocket V3 streams provide market data, live quotes, and security master files only. Zero order routing or execution endpoints exist.
6. **TimeMachine Historical Replay Covers ORB + 4 Intraday Setups**: `history_replay` evaluates Opening Range Breakouts (`orb`); `setup_history_replay` replays `breakout_intraday`, `vwap_reclaim`, `ema_alignment` and `rsi_momentum` through `trader-entry`'s `evaluate()` (same horizon as the plan, same stop distance in ATR units or percent; reported as context, not blended into the odds -- it calibrated worse than the Monte Carlo). Other setups (e.g. `bull_flag`, `level`) get the Monte Carlo only.
7. **Monte Carlo Uses Predefined Trade Boundaries**: `simulate()` in `trader-timemachine/timemachine.py` tests static barrier hit rates (`entry`, `stop`, `target`) across resampled historical bars. It does not execute dynamic trailing exit logic or live indicators.
8. **Analyst, News, and Pelosi are Independent Research Capabilities**: These tools provide standalone qualitative and fundamental intelligence via CLI. They do not participate in automated order generation or trade pipelines.
9. **Order Fingerprint Uses Truncated Unkeyed SHA-256 (Not HMAC)**: Order integrity verification uses the first 24 characters of an unkeyed SHA-256 hash computed over normalized order parameters and policy snapshots. No HMAC secret key is used.
10. **Python 3.8 Compatibility has Static Verification Only**: Core code maintains syntax and standard library compatibility targeting Python 3.8+, but active runtime testing is executed on Python 3.13.1.

---

## 2. Specialist Skills Audit (12 Skills)

### 2.1. `trader-alerts`
- **Primary Modules**: `alerts.py`, `calendar_in.py`, `instruments.py`, `symbols_in.py`, `data_in.py`, `upstox_feed.py`, `volume_flow.py`, `order_fingerprint.py`.
- **Actual Implemented Functionality**:
  - NYSE and NSE/BSE market clocks, session phases, holidays, and trading hours.
  - Authoritative Indian security master synchronization (`NSE_CM`, `BSE_CM`, `NSE_FO`).
  - Canonical NSE/BSE tick sizes (₹0.05), lot sizes, and ISIN resolution.
  - Multi-provider data ingestion (Upstox read-only, Yahoo Finance delayed, Offline snapshots).
  - Relative Volume (RVOL), volume delta, and institutional volume-flow indicators.
  - Truncated SHA-256 order fingerprint calculation.
  - Rule-based price, time, and plan alert monitoring.
- **Required Inputs**: Symbol, market (`IN` or `US`), exchange, optional config JSON.
- **Outputs**: Market status, delayed/real-time quotes, historical candles, alert triggers (`trader/prices.json`, `trader/alerts.json`).
- **Data Dependencies**: Exchange holiday calendars, Upstox API or Yahoo Finance endpoints, local cache (`trader/state/instruments.json`).
- **Configuration Dependencies**: `market`, `exchange`, `provider_credentials` (Upstox Analytics Token).
- **Supported Market Profiles**: US (NYSE/NASDAQ) and IN (NSE/BSE).
- **Operational Limitations**: Upstox token expires daily; Yahoo Finance is delayed ~15 minutes.
- **Unsupported Functionality**: Real-time tick streaming without valid Upstox credentials; order routing.
- **Failure Behaviour**: Fails closed with descriptive errors (`AlertError`, `MarketClockError`); falls back to Yahoo if enabled.
- **Test Coverage**: Tested independently via unit tests (`test_alerts.py`, `test_upstox_provider.py`, `test_bharat_data.py`, `test_volume_flow.py`).

### 2.2. `trader-scanner`
- **Primary Module**: `scan.py`.
- **Actual Implemented Functionality**:
  - Multi-timeframe screening across 1m, 5m, 15m, 30m, 1h, and daily bars.
  - Strategy scanning: ORB (5m/15m/30m), 20-day breakout, bull flag, reclaim MA20, oversold bounce, EMA alignment, RSI momentum.
  - Indian benchmark index scanning (NIFTY 50, BANK NIFTY, FIN NIFTY, SENSEX) and 6-state market regime classification.
- **Required Inputs**: Universe list or symbol list, market profile, timeframe interval.
- **Outputs**: Candidate watchlist JSON (`trader/intraday_scan.json`, `trader/watchlist.json`).
- **Data Dependencies**: Provider bars from `data_in.py`.
- **Configuration Dependencies**: Central loader `config.loader.load_config()`.
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Large universe scans are throttled to respect API rate limits.
- **Unsupported Functionality**: Scanning short breakout setups (scans for bullish breakouts above resistance only).
- **Failure Behaviour**: Logs missing charts, skips illiquid symbols, outputs empty candidate list if data fails.
- **Test Coverage**: Unit tested in `test_scanner.py`, `test_bharat_scan.py`, `test_system_integration_readiness.py`.

### 2.3. `trader-entry`
- **Primary Module**: `entry.py`.
- **Actual Implemented Functionality**:
  - Bar-by-bar entry timing on 1m, 5m, 15m, 30m, 1h bars.
  - 10 Intraday setups: `orb`, `breakout_intraday`, `vwap_reclaim`, `vwap_rejection`, `pullback`, `reversal`, `ema_alignment`, `rsi_momentum`, `ma_crossover`, `options_orb`.
  - Anti-chasing zone computation (`max_chase_pct`, `max_chase_atr`) returning `NO TRADE: EXTENDED`.
  - Stale and future timestamp detection returning `NO TRADE: STALE_DATA`.
  - Bearish VWAP rejection signal generation with inverted risk geometry.
- **Required Inputs**: Symbol, setup name, intraday bar series.
- **Outputs**: Trade entry recommendation (`trader/entry.json`).
- **Data Dependencies**: `data_in.py` candle context.
- **Configuration Dependencies**: Central loader `config.loader.load_config()`.
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Real-time evaluation requires continuous market data.
- **Unsupported Functionality**: Short execution planning; non-ORB options setups; PE options.
- **Failure Behaviour**: Emits `status: "waiting"` or `status: "no_trade"` fail-closed on invalid setups or stale data.
- **Test Coverage**: Tested in `test_entry.py`, `test_entry_vwap_rejection.py`, `test_entry_setups_decoupled.py`.

### 2.4. `trader-exit`
- **Primary Module**: `exit.py`.
- **Actual Implemented Functionality**:
  - Pre-entry protective stop and target planning (ATR, OR-mid, OR-low, percentage).
  - Enforces mandatory 2.0R gross and net reward-to-risk floor.
  - Position management: evaluates open positions against live bars for stop hit, target reach, or flat-by cutoff.
  - Authoritative tick-rounding for NSE/BSE (₹0.05) and US ($0.01).
- **Required Inputs**: `trader/entry.json` or CLI flags (`--symbol`, `--entry`, `--stop`, `--target`).
- **Outputs**: Trade plan (`trader/plan.json`), position management verdict (`trader/state/exit_decisions.json`).
- **Data Dependencies**: `data_in.py` bars for ATR and trailing evaluation.
- **Configuration Dependencies**: Central loader `config.loader.load_config()`.
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Supports long exits only.
- **Unsupported Functionality**: Planning stops for short sales (`PlanError: v2 plans longs only`); options premium stops.
- **Failure Behaviour**: Raises `PlanError` and refuses plan generation if R:R < 2.0 or stop is above entry.
- **Test Coverage**: Tested in `test_exit.py`, `test_exit_fixes.py`, `test_system_integration_readiness.py`.

### 2.5. `trader-budget`
- **Primary Module**: `size.py`.
- **Actual Implemented Functionality**:
  - Whole-share sizing constrained by 4 limits: max loss per trade, position cap, max order cap, remaining cash after buffer.
  - Indian statutory transaction charge calculation: STT, NSE exchange turnover fees, SEBI charges, stamp duty, GST (18%), brokerage.
  - Recalculates net reward/risk after friction, enforcing net 2.0R floor.
- **Required Inputs**: `trader/plan.json`, portfolio cash/positions (`trader/portfolio.json`), config rules.
- **Outputs**: Proposed order ticket (`trader/orders.json`).
- **Data Dependencies**: `instruments.py` for tick sizes and F&O lot sizes.
- **Configuration Dependencies**: Central loader `config.loader.load_config()`.
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Fractional shares unsupported in Indian market; sizing is long-only.
- **Unsupported Functionality**: Sizing short equity entries; sizing options contracts (`SizeError: options are not enabled`).
- **Failure Behaviour**: Raises `SizeError` and refuses sizing if risk exceeds budget or net R:R < 2.0.
- **Test Coverage**: Tested in `test_budget.py`, `test_bharat_lookahead.py`, `test_system_integration_readiness.py`.

### 2.6. `trader-risk`
- **Primary Module**: `check.py`.
- **Actual Implemented Functionality**:
  - Deterministic pre-trade order validation gate.
  - Verifies: required stop, max loss per trade, net 2R ratio, max daily/weekly loss, consecutive loss streak, T2T category, duplicate orders, market session entry window.
  - Computes canonical 24-character SHA-256 order fingerprint over order parameters and policy snapshot.
  - Generates verifiable audit verdicts (`trader/risk.json`).
- **Required Inputs**: `trader/orders.json`, active config, portfolio state.
- **Outputs**: Risk verdict JSON (`trader/risk.json`) with verdict `PASS` or `BLOCK`.
- **Data Dependencies**: `order_fingerprint.py`, `instruments.py`.
- **Configuration Dependencies**: Central loader `config.loader.load_config()`.
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Requires valid order JSON; blocks all unhedged or unpriced orders.
- **Unsupported Functionality**: Allowing short sales when `allow_short: false`; permitting naked short options.
- **Failure Behaviour**: Fails closed; returns exit code 1 and records explicit blocking reasons in JSON.
- **Test Coverage**: Tested in `test_risk.py`, `test_bharat_rules_2026.py`, `test_system_integration_readiness.py`.

### 2.7. `trader-broker`
- **Primary Module**: `ticket.py`.
- **Actual Implemented Functionality**:
  - Plain ticket rendering showing entry, stop, target, max loss, and transaction costs.
  - Verifies matching SHA-256 risk fingerprint before processing approvals.
  - Strict human approval parser: requires explicit confirmation text (e.g. `yes buy 10 RELIANCE`).
  - Records paper fills, updates portfolio cash and FIFO cost-basis share lots (`trader/portfolio.json`).
  - Generates flatten exit tickets to square off open day trades before flat-by cutoff.
- **Required Inputs**: `trader/orders.json`, `trader/risk.json`, user reply string.
- **Outputs**: Broker state (`trader/state/broker.json`), updated portfolio (`trader/portfolio.json`).
- **Data Dependencies**: `order_fingerprint.py`, `trader/state/broker.json`.
- **Configuration Dependencies**: Raw config dict + Risk PASS policy snapshot.
- **Supported Market Profiles**: US (paper + Alpaca live payload generation) and IN (strictly paper-only).
- **Operational Limitations**: Indian execution is paper-only; short sales cannot be filled in portfolio.
- **Unsupported Functionality**: Live broker order routing for Indian markets; automated unattended execution.
- **Failure Behaviour**: Throws `TicketError` and refuses approval if reply is ambiguous or fingerprint mismatches.
- **Test Coverage**: Tested in `test_broker.py`, `test_ticket_provenance.py`, `test_system_integration_readiness.py`.

### 2.8. `trader-timemachine`
- **Primary Module**: `timemachine.py`.
- **Actual Implemented Functionality**:
  - Monte Carlo simulation: runs 10,000 resampled intraday path replays to test barrier hit probabilities.
  - Historical replay: replays `orb` setups across past full sessions (75 bars for IN, 78 bars for US) without lookahead bias.
  - Calculates empirical win rate, profit factor, max drawdown, and expectancy.
- **Required Inputs**: `trader/plan.json` or CLI flags, 5m historical chart JSON/GZ.
- **Outputs**: Simulation summary (`trader/timemachine.json`).
- **Data Dependencies**: Historical OHLCV candle arrays.
- **Configuration Dependencies**: `_read_cfg_dict()` for optional parameters (`flat_by`).
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Historical replay is implemented for `orb` setup only; Monte Carlo uses static barrier boundaries.
- **Unsupported Functionality**: Dynamic trailing stop playback; non-ORB strategy historical replay.
- **Failure Behaviour**: Raises error on missing bars; returns `None` for historical replay on non-ORB setups.
- **Test Coverage**: Tested in `test_timemachine.py`, `test_bharat_lookahead.py`, `test_system_integration_readiness.py`.

### 2.9. `trader-analyst`
- **Primary Module**: `facts.py`.
- **Actual Implemented Functionality**:
  - Independent fundamental research CLI.
  - US: Pulls quarterly/annual balance sheet, income, and cash flow data from SEC EDGAR XBRL companyfacts API.
  - India: Ingests NSE/BSE fundamentals via Yahoo Finance TimeSeries, formatted in INR Crores (₹ Cr).
- **Required Inputs**: Ticker symbol, CIK, or exchange identifier via CLI.
- **Outputs**: Formatted financial summary table and JSON metrics (`metrics.json`).
- **Data Dependencies**: SEC EDGAR API (US) and Yahoo Finance fundamentals (IN).
- **Configuration Dependencies**: None (independent CLI flags).
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Subject to external upstream rate limits and data formatting changes.
- **Unsupported Functionality**: Real-time integration into intraday order generation.
- **Failure Behaviour**: Exits with error code on network failure or unrecognized ticker.
- **Test Coverage**: Unit tested in `test_analyst.py`. Verified via CLI `--help` in system integration suite.

### 2.10. `trader-news`
- **Primary Module**: `news.py`.
- **Actual Implemented Functionality**:
  - Independent catalyst and breaking news monitor CLI.
  - US: Fetches Yahoo Finance headlines and parses SEC EDGAR 8-K filings with item tags.
  - India: Aggregates Google News India RSS and Yahoo India business feeds.
  - Deduplicates items in local state (`trader/state/news_state.json`).
- **Required Inputs**: Ticker list, portfolio path, or watchlist path.
- **Outputs**: Formatted news items printed to stdout or JSON state.
- **Data Dependencies**: Public RSS feeds and SEC EDGAR feeds.
- **Configuration Dependencies**: None (independent CLI flags).
- **Supported Market Profiles**: US and IN.
- **Operational Limitations**: Free web RSS feeds may experience latency or formatting changes.
- **Unsupported Functionality**: Automated algorithmic sentiment execution.
- **Failure Behaviour**: Degrades gracefully; reports feed errors without terminating trading engine.
- **Test Coverage**: Unit tested in `test_news.py`. Verified via CLI `--help` in system integration suite.

### 2.11. `trader-pelosi`
- **Primary Module**: `ptr.py`.
- **Actual Implemented Functionality**:
  - Independent US Congressional STOCK Act Periodic Transaction Report (PTR) tracker CLI.
  - Downloads and parses House Clerk public disclosure XML indices and PDF filings.
  - Extracts transaction tables: owner, asset, ticker, transaction type, trade date, amount range.
- **Required Inputs**: Year, state path, or CLI flags (`index`, `new`, `parse`).
- **Outputs**: Extracted transaction reports.
- **Data Dependencies**: House Clerk disclosure website.
- **Configuration Dependencies**: None (independent CLI flags).
- **Supported Market Profiles**: US only.
- **Operational Limitations**: Relies on House Clerk filing publication schedule.
- **Unsupported Functionality**: Indian market disclosures; automated trade copying.
- **Failure Behaviour**: Handles missing files or invalid XML gracefully with error codes.
- **Test Coverage**: Unit tested in `test_pelosi.py`. Verified via CLI `--help` in system integration suite.

### 2.12. `trader-options`
- **Primary Modules**: `options.py` (`OptionsIntelligenceSpecialist`), `orchestrator.py` (`MarketIntelligenceOrchestrator`), `replay.py` (`OptionsTimeMachineReplay`).
- **Actual Implemented Functionality**:
  - Standalone, non-execution-connected options decision-support engine for Indian indices (NIFTY, BANKNIFTY, FINNIFTY) and F&O equities. Never writes `trader/orders.json`; never calls `trader-budget/size.py`'s order-sizing path or `trader-risk/check.py`'s order validator (it imports `check.py` only as a library, for its own internal portfolio-risk re-check).
  - Analytically evaluates and ranks BUY_CE, BUY_PE, SELL_CE, SELL_PE, two debit spreads (Bull Call Spread, Bear Put Spread), and two credit spreads (Bear Call Credit Spread, Bull Put Credit Spread) per underlying, with Black-Scholes/live Greeks, Delta-Gamma-Theta premium stop/target modeling, full Indian statutory charges, 4-tier position sizing, and portfolio-level sequential capital/risk allocation across a shortlist.
  - BUY_CE/BUY_PE are gated by config `buy_ce_enabled`/`buy_pe_enabled` (default `true`). SELL_CE/SELL_PE (naked) are always evaluated and fully disclosed (premium, margin, tail risk) but unconditionally rejected by an internal `NAKED_OPTION_PROHIBITED` check -- they can never be marked actionable. The Bear Call / Bull Put **credit spreads** are gated by `sell_ce_enabled`/`sell_pe_enabled` (default `false`, opt-in) and are genuinely actionable (not naked -- each has a structurally-validated protective long leg); their margin is a heuristic estimate (`max_loss x 1.10`), same disclosure pattern as the naked-sell margin estimate. Iron Condor (`STRATEGY_IRON_CONDOR`) remains a constant only -- building it needs a 4-leg combined-margin/P&L model (not just showing the two credit spreads side by side, which would understate the combined position's real margin efficiency) and a range-bound/non-directional trigger the scanner doesn't produce today.
  - Broad market regime (`trader-scanner/scan.py:get_broad_market_regime`) is disclosed on every signal as non-blocking context (`regime_context`: aligned/misaligned/unknown + plain-language reason) -- never a rejection reason, so a trade is never hidden or auto-killed by regime alone.
  - `max_option_lots` (config, default 2) and `max_correlated_exposure_pct` (config, default 40%, enforced in `orchestrator.py`'s sequential allocation -- all 5 major indices share one correlation bucket) are both hard ceilings on final position sizing.
  - Liquidity gate (`validate_liquidity`): open interest and bid/ask spread thresholds, config-overridable (`options_min_open_interest`, `options_max_spread_pct`; defaults 20,000 / 2.0%, an interim tightening from a prior state of no OI floor and 6% spread -- see `trader-options/SKILL.md` for the long-term 50,000/1.0% target).
  - `OptionsTimeMachineReplay` (`replay.py`): synthetic, explicitly-labeled `SYNTHETIC_OPTIONS_SIMULATION` backtest over real historical spot bars using the same Delta-Gamma premium model; not authentic tick-level options backtesting.
- **Required Inputs**: Symbol, option chain (via `trader-alerts/data_in.py`), underlying technical setup (via `trader-scanner/scan.py` or `trader-entry/entry.py`), config.
- **Outputs**: Signal contracts (in-memory/CLI/markdown report via `orchestrator.py`); never a file in the `trader/orders.json` execution chain.
- **Data Dependencies**: `trader-alerts/data_in.py` (`fetch_option_chain`: Upstox live chain if credentialed, else simulated Black-Scholes chain), `trader-alerts/instruments.py` (authoritative lot size/strike interval/expiry metadata).
- **Configuration Dependencies**: `buy_ce_enabled`, `buy_pe_enabled`, `sell_ce_enabled`, `sell_pe_enabled`, `max_option_lots`, `max_correlated_exposure_pct`, `options_min_open_interest`, `options_max_spread_pct`, `max_loss_per_trade_inr`/`max_daily_loss_inr`, `budget`.
- **Supported Market Profiles**: IN only.
- **Operational Limitations**: Spread net R:R (debit and credit alike) is derived by scaling the primary leg's own modeled premium move rather than independently projecting both legs' Greeks -- an approximation, not a full two-leg model (loss is still strictly capped by strike width). Theta is assumed per-calendar-day for all providers; confirmed for the internal Black-Scholes model, unverified against Upstox's own documentation for live broker Greeks (both paths are explicitly tagged with a `theta_unit` field so this assumption is auditable). Correlation limiting covers only the broad-index group and same-symbol repeats -- no sector-level grouping exists (no verified sector data in this repo).
- **Unsupported Functionality**: Any order execution; Iron Condor; approvable naked option selling.
- **Failure Behaviour**: Fails closed -- no synthetic underlying setups fabricated, no fabricated premium/R:R numbers (unavailable Delta/Gamma models propagate `None`/`N/A`, never `0.0`), stale/offline data downgrades a signal to `HISTORICAL ANALYSIS`/non-actionable rather than presenting it as live.
- **Test Coverage**: `test_options_intelligence.py`, `test_bharat_options_risk.py`, `test_options_premium_model_integrity.py`, `test_options_stage2_safety_gates.py`.

---

## 3. Operational Safety & Compliance Summary

| Requirement | Implementation Safeguard | Enforcing Code |
| :--- | :--- | :--- |
| **Paper-Only Indian Trading** | Live trading disabled in schema; tickets locked to paper | `config/schema.py:218`, `ticket.py:348` |
| **Mandatory 2.0R Net Floor** | Orders < 2.0R after statutory charges are refused | `size.py:1162`, `check.py:1396` |
| **Statutory Cost Modeling** | STT, NSE charges, SEBI fees, Stamp Duty, GST deducted | `size.py:1180-1250` |
| **Cryptographic Provenance** | Truncated SHA-256 fingerprint invalidates modified orders | `order_fingerprint.py:168-181` |
| **Human In The Loop** | Explicit text confirmation required before ticket processing | `ticket.py:698-765` |
| **Stale/Future Data Defense** | Timestamps in future or older than threshold fail closed | `data_in.py:1924`, `entry.py:1594` |
| **Read-Only Upstox** | Read-only GET requests only; zero order endpoints | `data_in.py:2494-2650` |
| **T2T Restrictions** | Trade-to-Trade delivery-only stocks blocked from day trading | `instruments.py:1260`, `check.py:1157` |
| **Naked Option Prohibition (order pipeline)** | Single-leg `side == "sell"` option orders rejected | `check.py:1295-1297` |
| **Naked Option Prohibition (trader-options analysis)** | SELL_CE/SELL_PE unconditionally non-actionable, independent of margin-authority status | `options.py` `generate_signal_contract` (`NAKED_OPTION_PROHIBITED`) |
| **Zero Fabrication (trader-options premiums)** | Unavailable Delta/Gamma premium model propagates `None`/`N/A`, never `0.0` | `options.py` `model_premium_levels`, `calculate_charges_and_net_rr` |
