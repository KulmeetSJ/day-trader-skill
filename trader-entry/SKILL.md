---
name: trader-entry
description: >-
  Intraday Entry Engine. Evaluates actionable setups at any session hour
  (09:45, 11:30, 14:00) on 1m/5m/15m/30m/1h bars across US and Indian
  (NSE/BSE, IST, tick-size normalized) markets. Supports breakout_intraday,
  vwap_reclaim, ema_alignment, orb, level, breakout_20d, ma_crossover, trend,
  rsi_momentum, and signal-only options_orb (BUY CE only; no option execution)
  without requiring opening-range
  bars for non-ORB setups. Distinguishes ENTRY CONFIRMED, ENTRY CANDIDATE,
  SETUP, and NO TRADE (EXTENDED anti-chasing, INVALIDATED, STALE_DATA),
  reports entry_zone, invalidation_level, stop/target previews, and writes
  trader/entry.json for /trader-exit. Long side only; never places an order.
argument-hint: "[TICKER] [--setup breakout_intraday|vwap_reclaim|ema_alignment|orb|level|breakout_20d|rsi_momentum|options_orb] [--interval 1m|5m|15m|30m|1h]"
---

# trader-entry

## Options strategy support

`options_orb` evaluates a bullish underlying opening-range breakout and can identify a listed **BUY CE** contract for analysis. BUY PE, SELL CE, and SELL PE are unsupported and must return `UNSUPPORTED_STRATEGY`. It does not currently validate fresh two-sided option quotes, execution costs, or premium-based stop/target levels, so an option signal is never an actionable option recommendation: report the underlying setup separately and return `NO TRADE / OPTION RISK UNAVAILABLE`. Never size or route an option order from the underlying's R:R.

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TSLA                  # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TSLA --write          # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TSLA --setup level --level 371.20
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --market IN --exchange NSE --setup breakout_20d
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TSLA --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch TSLA --until 15:30    # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                   # every hit in trader/watchlist.json
```

Flags on `check`, `watch`, and `watchlist`:
- `--setup`: `orb` (default), `level`, `breakout_20d`, `ma_crossover`,
  `trend`, `rsi_momentum`, or `options_orb`
- `--or-minutes N` (default `15`), `--interval 5m|1m` (default `5m`),
  `--level PRICE` (`--setup level`), `--min-volume-x FLOAT`
- `--market US|IN`, `--exchange NSE|BSE`, `--instruments PATH`,
  `--trading-style intraday|positional`
- `--option-strategy STRATEGY` (e.g. `long_call`, `long_put`),
  `--strike-selection ATM|ITM1|ITM2|OTM1|OTM2` (`options_orb`)
- `--offline PATH` (saved chart JSON `.json`/`.json.gz` or folder),
  `--daily PATH` (saved `1d` daily history JSON or folder for `breakout_20d`),
  `--now "2026-10-19 10:15"` (market local time, ET for US or IST for IN;
  clock-overridden runs are always marked non-live), `--config PATH`, `--json`
- `watch`: `--every SECS` (default `30`), `--until HH:MM`, `--out PATH`
  (with `--now` or `--offline`, replays bar by bar without sleeping)

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` / `no_entries_after` (`09:45`/`15:30` ET for US; `09:30`/`15:00` IST for India). Optional |
| `trader/state/instruments.json` | optional NSE/BSE cache for exchange resolution and authoritative INR tick size (India) |
| Yahoo / India data provider | today's bars (`5m` or `1m`) plus `1d` history for `breakout_20d`: Yahoo is free, unofficial, and can lag |
| `trader/entry.json` | with `check --write` or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range, data tier and optional `invalidation_level` / `option_contract` |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes:
  `9:30`, `9:35`, `9:40` ET bars (US) or `9:15`, `9:20`, `9:25` IST bars
  (India), or fifteen 1-minute bars. The level to beat is the range high.
- **Level.** A price you give, like a level from `/trader-scanner`.
- **20-day breakout (`breakout_20d`).** Derives the highest close among the
  20 completed sessions before the candidate/current session and
  requires a completed intraday bar closing strictly above that breakout
  level. A daily scanner hit never triggers without intraday confirmation;
  stale or missing daily history returns `no-data`. Also records
  `invalidation_level` for `/trader-exit`.
- **Intraday indicator setups.** `ma_crossover` (9-bar SMA crossing above
  21-bar SMA), `trend` (close > 9-bar SMA > 21-bar SMA), and `rsi_momentum`
  (14-bar RSI crossing at or above 60.0).
- **Simulated options ORB (`options_orb`).** Applies the ORB trigger on the
  underlying and selects an option contract via `data_in`; when live option
  chain data is absent, contracts come from the analytical Black-Scholes
  engine and are explicitly tagged `SIMULATED` (`is_simulated: true`).
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 10:10 bar closes at 10:15 AM).
  Trigger and level prices round using the authoritative `tick_size` from
  `--instruments` / chart metadata when present (preserving sub-paisa INR
  ticks such as `0.0025`, or standard `0.01`, `0.05`, `0.10`).
- **Only closed bars count.** A bar is closed once the clock passes its
  close and the feed has caught up. Live in-progress ticks are never counted.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The opening bar holds the
  opening auction, so 1.0 is already strict.
- **Time limits.** US: `09:45` to `15:30` ET by default (moved up to `12:30` on
  a `1:00 p.m.` early close). India (`--market IN`): `09:30` to `15:00` IST
  (with continuous close `15:30 IST`, or `15:15 IST` during CAS V1 for CAS
  eligible scrips). Positional setups (`--trading-style positional`) plan for
  multi-session delivery holds rather than same-day square-off.
- **Data provenance.** Output includes `data_tier` (`CURRENT`, `DELAYED`,
  `HISTORICAL`, `FIXTURE`, `SIMULATED`) and `is_live_signal`. Mocked clock
  (`--now`) or offline fixture runs are never marked live signals.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before open: not open yet. During the
  opening range: still forming. Weekends and exchange holidays: market closed.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/trader-scanner`. Check `/trader-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 15:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time, the price, the data tier, and that nothing
   was placed. If the trigger is more than a bar old, say that too: a late
   entry is a different trade.
5. Hand off to `/trader-exit` to set the stop and the target BEFORE anything
   is bought.

## Example: the reel

TSLA, Monday 2026-10-19, 5-minute bars. The opening range is 354.47 to
362.18. At 10:12 the 10:10 bar is still open, so it waits:

```
WAITING  last close $361.68 (10:05 bar), $0.50 under the range high $362.18 (-0.14%).
The 10:10 bar is still forming ($364.18 so far, closes 10:15 AM); it does not count until it closes.
```

At 10:15 the bar has closed:

```
TSLA  Mon 2026-10-19 10:15 ET  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:30-9:45: high $362.18, low $354.47 (3 of 3 bars).
TRIGGERED 10:15 AM at $364.18: the 10:10 bar (bar 8) closed above the range high $362.18 (+0.55%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan --from trader/entry.json).
```

With `--min-volume-x 1` the 10:10 bar is too thin (0.60x the range's volume)
and the trigger moves to 10:35 AM at 365.75 (the 10:30 bar, 7.98x).

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars (or configured Indian
  provider). Free feeds are unofficial and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. v2 plans longs only. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## Intraday Decision Engine (anytime evaluation)

`entry.py check` evaluates live or on-demand intraday market state at any time during an active session (e.g., `09:45`, `11:30`, `14:00`) using `1m`, `5m`, `15m`, `30m`, or `1h` bars:

- **Setups without opening-range prerequisites**: `breakout_intraday` (intraday swing high breakout via `--lookback-bars`), `vwap_reclaim` (session VWAP reclaim), `ema_alignment` (EMA9 > EMA21 > VWAP / rising EMA stack), `rsi_momentum` (`momentum`), alongside `orb`, `level`, `breakout_20d`, `ma_crossover`, `trend`, and `options_orb`.
- **Actionable decision states**: `ENTRY CONFIRMED`, `ENTRY CANDIDATE`, `SETUP`, or `NO TRADE` (`EXTENDED` anti-chasing protection beyond `entry_zone`, `INVALIDATED`, `STALE_DATA`, `NO_DATA`, `EXPIRED`, `MARKET_CLOSED`, `ANALYTICAL_ONLY_INDEX`).
- **Decision fields**: `entry_zone` (`low`/`high`), `invalidation_level`, `stop_preview`, `targets_preview`, `reward_risk`, `sizing_preview`, separated `candle_context` (`completed_candle`, `forming_candle`, `current_quote`), and opt-in repeated-run deduplication via `--state PATH` (`NEW` / `UPDATED` / `UNCHANGED`).
- **Evaluation mode**: Runs on-invocation against latest completed bars and fail-closes (`NO TRADE` / `STALE_DATA`) when data is stale or unavailable.

## Opt-in visualisations

Default output is text/JSON only. Only when the user explicitly requests a chart, render an entry/candlestick SVG via `../trader-alerts/visuals.py` from already-fetched bars and `trader/entry.json` (see `guides/trader-visuals.md`).

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/trader-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/trader-broker` from a background
watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
