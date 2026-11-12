# Opt-in Charts and Visualisations Guide (`guides/trader-visuals.md`)

Shared visualisation standards and lightweight standard-library charting workflow for all `day-trader-skill` market, technical, portfolio, risk, fundamental, and catalyst outputs.

## Strict Core Rules

1. **Text and tables by default.** Never generate charts, plots, or images during normal workflows unless the user explicitly asks for a chart, plot, graph, diagram, or visual explanation.
2. **No decorative images.** Every generated visualisation must convey real numerical/event data relevant to the user's explicit request.
3. **Reuse already-fetched data.** Generate charts directly from existing JSON payloads or cached tool outputs (`trader/history.json`, `trader/index_scan.json`, `trader/entry.json`, `trader/plan.json`, `trader/timemachine.json`, `trader/portfolio.json`, `trader/state/risk.json`, `trader/state/broker.json`, `facts.py --json`, `news.py --json`, option-chain JSON). Avoid making extra API calls solely to draw a chart.
4. **Zero external dependencies.** `<SKILL_DIR>/trader-alerts/visuals.py` uses Python 3.8+ standard library only and produces clean, self-contained `.svg` charts without requiring `matplotlib`, `Pillow`, or network assets.
5. **Mandatory chart provenance.** Every market chart must clearly display:
   - **Data source / provider** (e.g., `upstox`, `yahoo`, `offline`, `sec-edgar`)
   - **Timestamp** (exact `last_data_timestamp` or `as_of` from the source payload)
   - **Timeframe / interval** (e.g., `1m`, `5m`, `15m`, `1h`, `1d`)
   - **Freshness / tier** (`CURRENT`, `DELAYED`, `HISTORICAL`, `FIXTURE`, or `SIMULATED`)
6. **Never fabricate data.** Never invent candles, prices, timestamps, volume, or indicators. If required series or volume are missing (for example, cash volume on spot Indian indices), omit VWAP/volume subplots honestly and state `"N/A"` rather than synthesizing numbers.

## Supported Visualisation Types

Run `<SKILL_DIR>/trader-alerts/visuals.py <subcommand> ... --out trader/<name>.svg` (or `--chart <path>` where supported):

1. **Candlestick + Indicators (`candlestick`)**
   - OHLC candlesticks with optional EMA overlays (`--ema 20,50`), VWAP (computed only when volume > 0), volume bars, support/resistance levels, breakout levels, and optional plan overlays (`--plan trader/plan.json`).
   - Inline shortcuts (opt-in only):
     - `alerts.py history NIFTY --market IN --interval 5m --range 5d --chart trader/nifty_5m.svg`
     - `scan.py indices --symbols NIFTY --interval 5m --ema 20,50 --chart trader/nifty_5m.svg`
   - Standalone from saved JSON (zero extra API calls):
     - `visuals.py candlestick --input trader/history.json --ema 20,50 --out trader/chart.svg`

2. **Entry / Exit Plan (`entry-exit`)**
   - Overlays entry trigger, stop-loss, R-multiple target(s), and recorded exit levels cleanly on real bars:
     - `visuals.py entry-exit --input trader/history.json --plan trader/plan.json --entry trader/entry.json --out trader/trade_plan.svg`

3. **Options Payoff, Greeks & IV (`options-payoff`)**
   - Plots expiration payoff P&L curve, breakeven point(s), spot price line, and Greeks/IV summary from option-chain or contract JSON:
     - `visuals.py options-payoff --input trader/option_payoff.json --out trader/payoff.svg`

4. **Portfolio Allocation & P&L (`portfolio`)**
   - Holdings allocation breakdown, cash vs invested capital, and realized/unrealized P&L from `trader/portfolio.json` (plus optional `trader/prices.json`):
     - `visuals.py portfolio --portfolio trader/portfolio.json --prices trader/prices.json --out trader/portfolio.svg`

5. **Risk Exposure & Drawdown (`risk-drawdown`)**
   - Per-trade max-loss exposure vs limits, cumulative P&L / drawdown vs daily loss cap from `trader/state/risk.json`, `trader/state/broker.json`, and `trader/config.json`:
     - `visuals.py risk-drawdown --risk trader/state/risk.json --broker trader/state/broker.json --config trader/config.json --out trader/risk.svg`

6. **Backtest Equity Fan & Distribution (`backtest`)**
   - Plots Time Machine Monte Carlo percentile bands (`p5`/`p25`/`median`/`p75`/`p95`) and historical replay return distribution directly from `trader/timemachine.json`:
     - `visuals.py backtest --input trader/timemachine.json --out trader/timemachine.svg`

7. **Fundamental Trends (`fundamentals`)**
   - Revenue, net profit, margins (`%`), and EPS across periods from `facts.py --json` output (US USD or Indian INR Crores):
     - `visuals.py fundamentals --input trader/facts.json --out trader/fundamentals.svg`

8. **News & Catalyst Timeline (`news-timeline`)**
   - Chronological event timeline grouped by relevance/category (`corporate`, `business`, `analyst`, `market`, `sec_8k`) from `news.py --json`:
     - `visuals.py news-timeline --input trader/news.json --out trader/news_timeline.svg`

