---
name: trader-timemachine
description: Time Machine. Replays every possible outcome of a trade plan before risking a dollar by running 10,000 Monte Carlo session simulations plus historical replay on every complete past session from the stock's 5-minute bars (78 bars for US ET, 75 bars for Indian IST), enforcing look-ahead checks and writing trader/timemachine.json for trader-budget.
---

# trader-timemachine (The Time Machine)

Runs `timemachine.py` to test a `trader-exit` plan against both historical sessions and a time-of-day matched Monte Carlo bootstrap (10,000 paths) before `trader-budget` sizes a single share. Pure Python standard library.

When the user asks "what are the odds?", "replay this trade", "run the time machine", or "simulate this setup", run `timemachine.py` and present the honest probabilities, historical count, and average `R` without overselling the verdict.

## Setup and commands

```
# Run against trader/plan.json (US default)
python3 <SKILL_DIR>/trader-timemachine/timemachine.py run --plan trader/plan.json --write trader/timemachine.json

# Run for an Indian plan (75 five-minute bars, 09:15-15:30 IST, via data_in provider)
python3 <SKILL_DIR>/trader-timemachine/timemachine.py run --plan trader/plan.json --market IN --write trader/timemachine.json

# Draw the text distribution chart from a saved run or fresh flags
python3 <SKILL_DIR>/trader-timemachine/timemachine.py chart --from trader/timemachine.json

# Offline / reproducible historical replay
python3 <SKILL_DIR>/trader-timemachine/timemachine.py run --plan trader/plan.json --offline <CHART_JSON_OR_GZ> --now "2026-10-19 10:15" --write trader/timemachine.json
```

## Subcommands and flags

- `run`: simulates the trade plan and optionally writes `trader/timemachine.json`.
- `chart`: renders the close-price distribution and simulated expectancy chart (`--from PATH` reads saved JSON without re-simulating).
- Plan/trade inputs: `--plan PATH` (`trader/plan.json`), or manual flags `--symbol`, `--side buy|sell`, `--date YYYY-MM-DD`, `--entry`, `--stop`, `--target`, `--entry-time "HH:MM AM/PM"`, `--setup orb`, `--or-minutes 15`, `--stop-rule auto|or_mid|or_low|or_high|pct`.
- Simulation controls: `--replays 10000`, `--seed 19`, `--window 2` (samples future 5-minute returns from `+-window` bars around the same time of day), `--until HH:MM` (defaults to `16:00` ET for US or `15:30` IST for India).
- Market/environment: `--market US|IN`, `--config PATH`, `--offline PATH` (`.json` or `.json.gz`), `--now STR`, `--json`, `--write [PATH]` (defaults to `trader/timemachine.json`).

## How the replay works

1. **Clean session builder**: Keeps only complete prior trading days -- `78` 5-minute bars (`09:30-16:00 ET`) for `--market US` and `75` 5-minute bars (`09:15-15:30 IST`) for `--market IN`. Automatically drops exchange holidays, short/special sessions (such as NYSE half-days or Indian Muhurat sessions), and incomplete gapped days.
2. **Look-ahead guard**: Validates `date`, `entry_time`, and `last_data_timestamp` against `--now` (or live market clock) so future bars never leak into a simulated entry.
3. **Monte Carlo bootstrap (10,000 sessions)**: Starts at `entry` and steps forward 5 minutes at a time until `--until`, drawing percentage returns from the matching time-of-day window across prior clean sessions and checking `stop` and `target` on each step.
4. **Historical replay**: Applies the exact setup rule across every clean prior day in the lookback window (excluding today) and reports past triggers, target-first vs stop-first vs close outcomes, and realized `R`.
   - `orb`: the opening-range rule with `--stop-rule`.
   - `breakout_intraday`, `vwap_reclaim`, `ema_alignment`, `rsi_momentum`: `trader-entry`'s own `evaluate()` on each past session (a fresh, actionable ENTRY CONFIRMED at its own bar close), triggers within +-90 minutes of the plan's entry (whole session if that gives fewer than 8), the plan's stop distance (risk / ATR(14) for `atr` and `swing` plans, otherwise the same percent) and target R, walked for the **same number of bars the plan has left** so a late entry is judged on a late-entry horizon.
5. **Intraday horizon**: an Indian plan with `flat_by` (trader-exit writes `15:20`) replays to that bar, not to the 15:30 close.
6. **Prediction**: the odds and the verdict come from the Monte Carlo (to flat-by for intraday India plans). The per-stock setup history is context only. On the 2026-09..11 NIFTY 200 holdout, its ~25 trades per stock predicted worse than the Monte Carlo at every blend weight tried, so it is not mixed in. For India the `prediction` block also reports `avg_R_net`: average R after estimated round-trip charges at the qty `trader-budget` would size. Lead with it, because charges are often 0.2-0.5R on small intraday risks.
   - History stops are replayed at the plan's own stop distance in ATR units (an `atr` or `swing` plan both become risk / ATR), so the past trades have the same geometry as this one.
7. **Summary metrics**: Prints `Target first %`, `Stop first %`, `Neither (held to close) %`, close percentiles (`p5`, `median`, `p95`), `Average R`, historical trade count, the prediction, and writes `trader/timemachine.json`.

Accuracy is measured, not assumed: `python3 scripts/backtest_intraday.py calibrate` scores the Time Machine's odds against what actually happened on held-out sessions (Brier score and reliability buckets).

## Interpreting results honestly

- Pay attention to both the bootstrap probabilities AND the number of historical sessions tested: a small clean-session count means noisy estimates.
- Lead with `avg_R_net` (after estimated Indian charges) when present; small intraday risks make charges worth 0.2-0.5R a trade.
- Look at `p5` vs `stop` to see gap/tail behavior near the close (`16:00 ET` or `15:30 IST`).
- When presenting results to the user, report:
  1. Target-first %, stop-first %, and close-out % from the 10,000 runs.
  2. Average `R` and close percentiles.
  3. Exact historical sample count and past hit rate.
  4. Whether `--now` or `--offline` was used (`historical` vs `live`).

## Opt-in visualisations

Default output is text/JSON only. Only when the user explicitly requests a chart, render equity curves or trade distributions via `../trader-alerts/visuals.py backtest --timemachine trader/timemachine.json` (see `guides/trader-visuals.md`).

## What it never does

It never predicts tomorrow's price, never recommends taking a trade, never overrides stop/risk limits, and never places a broker order.

## Safety

- Education and simulation tooling only, not financial advice. Past 5-minute bar distributions and Monte Carlo replays do not guarantee future performance or execution prices; live stops can slip through gaps.
- Never claim a positive simulated average `R` proves a strategy works in live markets. Always disclose sample size (number of clean sessions), spread/slippage assumptions, and `--now` / `is_live` status.
- Downstream sizing and order execution still require `trader-budget`, `trader-risk` PASS today, and the user's explicit typed `yes` in `trader-broker` (paper trading by default).
- Keep market data tokens in local environment variables only; never ask for API tokens in chat.
