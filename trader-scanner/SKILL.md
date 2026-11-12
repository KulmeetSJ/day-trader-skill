---
name: trader-scanner
description: Chart Scanner. Scans US and Indian markets at any hour for actionable intraday setups via scan.py intraday (breakout_intraday, vwap_reclaim, ema_alignment, rsi_momentum, orb on 1m/5m/15m/30m/1h bars with entry_zone and anti-chasing), daily setups (20-day breakouts, bull flags, reclaim MA20, oversold, MA/trend/RSI), opening-range breakouts, and Indian benchmark indices (NIFTY, BANKNIFTY, FINNIFTY, SENSEX; analytical only). Supports Upstox/Yahoo/offline providers and writes trader/intraday_scan.json or trader/watchlist.json.
---

# trader-scanner (The Chart Scanner)

Runs `scan.py` to build a clean equity universe and scan daily closes or 5-minute opening-range breakouts (ORBs). Pure Python standard library, no third-party dependencies.

When the user asks to find setups, run a morning or pre-market scan, scan NIFTY50 / NSE / BSE or US stocks, or check opening-range breakouts, run the scanner and present only the setups found with exact price, volume, market, provider, and timestamp provenance.

## Setup

Run `scan.py` from `<SKILL_DIR>/trader-scanner/scan.py` (where `<SKILL_DIR>` is the directory containing the skill pack, and the working directory holds `trader/` outputs).

1. **US universe** (downloads Nasdaq Trader `nasdaqlisted.txt` and `otherlisted.txt` into `trader/universe.txt`; excludes test issues, ETFs, warrants, units, rights, preferreds):
   ```
   python3 <SKILL_DIR>/trader-scanner/scan.py universe
   ```
2. **Indian universe** (`--market IN`; supports `--index` with `NIFTY50`, `NIFTY100`, `NIFTY200`, `NIFTY500`, `BANKNIFTY`, `FINNIFTY`, `SENSEX`, `BSE100`, `INDICES`, `ALL`/`CASH`, `FNO`, or `CUSTOM` reading `symbols`/`watchlist` from `trader/config.json`, plus `--exchange NSE|BSE|ALL` and `--instruments trader/state/instruments.json`):
   ```
   python3 <SKILL_DIR>/trader-scanner/scan.py universe --market IN --index NIFTY50 --exchange NSE
   ```
3. **Daily pre-market setups** (from daily closes, default filter `$5+` price / `1M+` avg volume unless overridden):
   ```
   python3 <SKILL_DIR>/trader-scanner/scan.py daily --universe trader/universe.txt --show 25
   python3 <SKILL_DIR>/trader-scanner/scan.py daily --market IN --symbols RELIANCE,TCS,INFY --strategy breakout_20d
   ```
4. **Intraday Opening-Range Breakout (ORB)** (default `--or-minutes 15`, cutoff `--until` defaulting to config `no_entries_after` e.g. `15:30` ET / `15:00` IST):
   ```
   python3 <SKILL_DIR>/trader-scanner/scan.py orb --watchlist trader/watchlist.json
   python3 <SKILL_DIR>/trader-scanner/scan.py orb --market IN --symbols RELIANCE,HDFCBANK --or-minutes 15
   ```

To replay or test offline without network access:
```
python3 <SKILL_DIR>/trader-scanner/scan.py daily --offline <FIXTURE_DIR> --now "2026-10-19 08:30" --symbols AAPL,TSLA
python3 <SKILL_DIR>/trader-scanner/scan.py orb --market IN --offline <FIXTURE_DIR> --now "2026-10-19 10:15" --symbols RELIANCE,TCS
```

## Subcommands and flags

- `universe`: `--out` (`trader/universe.txt`), `--offline DIR`, `--no-write`, `--pause`, `--json`, `--market US|IN`, `--index INDEX`, `--exchange NSE|BSE|ALL`, `--instruments PATH`, `--config PATH`.
- `daily`: `--symbols SYMBOLS | --watchlist FILE`, `--universe FILE`, `--limit N`, `--min-price F`, `--min-volume F`, `--offline PATH`, `--now STR`, `--out FILE`, `--no-write`, `--pause S`, `--show N`, `--quiet`, `--json`, `--market US|IN`, `--strategy STRATEGY`, `--config FILE`.
- `orb`: all `daily` flags (`--min-volume` defaults to `0`), plus `--or-minutes N` (default `15`), `--max-confirm N` (default `200`, `0` = all), `--at HH:MM`, `--until HH:MM`.
- `indices`: first-class Indian market index analysis (`--symbols NIFTY,BANKNIFTY,...` or omit for all 10 major benchmarks: `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `NIFTYNXT50`, `NIFTY100`, `NIFTY200`, `NIFTY500`, `SENSEX`, `BANKEX`), `--exchange NSE|BSE`, `--interval 1m|5m|15m|1h|1d`, `--range 1d|5d|3mo|1y`, `--ema 20,50`, `--source`, `--offline`, `--now`, `--out trader/index_scan.json`, `--chart PATH` (opt-in SVG only when explicitly requested). Computes EMA alignment, VWAP (`N/A` on zero-volume spot indices), RSI(14), ATR/volatility %, support/resistance, 20-bar breakout levels, and multi-index breadth/regime summary.
  - Spot indices are analytical only (`is_tradable_equity: false`) and cannot be ordered or held as equities; use NSE/BSE index F&O contracts for execution workflows.
  - Provider note: `upstox` supports full intraday/daily history on all 10 benchmarks; Yahoo returns 1 daily bar only for `MIDCPNIFTY` (`NIFTY_MID_SELECT.NS`) and `BANKEX` (`BSE-BANK.BO`), which `scan.py indices` reports as `limited` without inventing bars or switching providers silently.
- Supported `--strategy` values:
  - Core setups: `breakout_20d`, `bull_flag`, `reclaim_ma20`, `oversold`, `oversold_bounce`.
  - Extended strategies: `ma_crossover` (fast/slow SMA from config `strategy_params`, default 9/21), `trend_following`, `rsi_momentum` (config `rsi_momentum_threshold`, default `60.0`).
- Writes `trader/watchlist.json` (`daily`/`orb`) or `trader/index_scan.json` (`indices`, unless `--no-write`), passing clean candidates and provider/data_status/last_data_timestamp metadata to `trader-news` and `trader-entry`.
- **Opt-in visualisations**: Default output is text/tables only. Only render SVG charts (`--chart PATH` or `../trader-alerts/visuals.py`) when the user explicitly requests a chart; see `guides/trader-visuals.md`.

## What each setup checks

Every rule is deterministic on completed OHLCV bars:
- `breakout_20d`: latest close exceeds the highest close of the prior 20 sessions.
- `bull_flag`: strong prior up-leg (`>=10%` over 20 bars into the flag high) followed by a tight, orderly 3-10 day consolidation (`<=8%` range) near highs.
- `reclaim_ma20`: yesterday's close was below the 20-day simple moving average, today's close is back above it.
- `oversold` / `oversold_bounce`: 14-period Wilder RSI `< 30`, or bouncing higher out of oversold.
- `ma_crossover` / `trend_following` / `rsi_momentum`: trend and momentum filters parameterized by `strategy_params` in config (`enable_extended_strategies`).
- `orb`: first completed 5-minute bar after the opening range (default 15 minutes from open: 9:30 ET for US, 9:15 IST for IN) that closes strictly above the opening-range high.

## Rules and provenance

- Always state market (`US` or `IN`), provider (`yahoo`, `upstox`, or offline fixture), `data_status`, `is_live`, and `last_data_timestamp`.
- Free Yahoo Finance data is unofficial and delayed (`~15m` US/India); live Indian feeds require an `UPSTOX_ANALYTICS_TOKEN` read-only data setup on the user's machine. Never claim real-time fills from delayed bars.
- Using `--now` switches the run to historical replay mode (`is_live=false`); report the simulated clock clearly.
- This scanner surfaces candidates only. Run `trader-news` (catalysts/8-K/announcements), `trader-entry` (1m/5m confirmation), `trader-exit` (stop/target plan), `trader-timemachine`, `trader-budget`, `trader-risk`, and explicit user `yes` in `trader-broker` before any paper order.

## What it never does

It never predicts prices, never recommends a buy or sell, never runs an unconfirmed trade, and never places a broker order.

## Safety

- Education and charting tools, not financial advice. A scanner hit is a pattern match, not a trade signal or recommendation.
- Paper trading only by default. Any downstream order still requires `trader-exit`, `trader-budget`, `trader-risk` PASS today, and the user's explicit typed `yes` in `trader-broker` (plus broker approval prompts if enabled).
- Upstox tokens or broker secrets belong in local environment variables only, never pasted into chat.

## Intraday Actionable Scanner (`scan.py intraday`)

Use `scan.py intraday` at any time during market hours to evaluate `breakout_intraday`, `vwap_reclaim`, `ema_alignment`, `rsi_momentum`, or `orb` across `1m|5m|15m|30m|1h` candles without requiring opening-range bars for non-ORB setups:

```
python3 <SKILL_DIR>/trader-scanner/scan.py intraday --market IN --watchlist trader/watchlist.json --setups breakout_intraday,vwap_reclaim,ema_alignment --interval 5m --state trader/state/scan_intraday.json --write
python3 <SKILL_DIR>/trader-scanner/scan.py intraday --market IN --indices --interval 5m
```

- Tags each hit with `decision_state` (`ENTRY CONFIRMED`, `ENTRY CANDIDATE`, `SETUP`, `NO TRADE`), `entry_zone`, `invalidation_level`, `stop_preview`, `targets_preview`, `reward_risk`, indicators (`ema_9`, `ema_21`, `vwap`, `rsi_14`, `atr`), and `signal_change` when `--state` is supplied.
- Indian spot indices (`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `SENSEX`) remain analytical only (`analytical_only: true`, `is_tradable_equity: false`).
