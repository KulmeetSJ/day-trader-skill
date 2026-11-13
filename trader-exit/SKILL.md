---
name: trader-exit
description: >-
  Exit & Position Manager. Plans pre-entry stops/targets (or-mid, or-low,
  breakout-invalidation, price, pct, ATR; US 0.01 USD and IN authoritative
  INR tick sizes; 2R/1R/target) and evaluates open positions at any session
  hour via exit.py manage/check. Outputs HOLD, UPDATE TRAILING STOP, PARTIAL
  EXIT, EXIT (stop/VWAP/trend/flat-by/expiry), or fail-closed NO_ACTION on
  stale/missing data. Respects intraday flat-by cutoffs without forcing
  evening square-off on positional delivery holds. Writes trader/plan.json for
  /trader-timemachine, /trader-budget, and /trader-alerts. Long side only.
argument-hint: "[plan|manage] [--stop ...] [--target 2R|1R|price:X] [--interval 1m|5m|15m|30m|1h]"
---

# trader-exit

Intraday portfolio lots use their oldest remaining lot date to detect overnight carryover. A carryover on a holiday, weekend, or outside an open session is `EXIT_PENDING_NEXT_SESSION` with no exit price or executable ticket; at the first open session, only fresh available market data can produce an `EXIT`. Positional holdings are exempt from this rule.

For options, underlying technical levels are analytical only. The tool does not have a validated premium valuation path with fresh bid/ask data, authoritative contract ticks, liquidity and charges; it therefore reports `OPTION RISK UNAVAILABLE` and `NO TRADE`, never a delta-based premium stop/target or option R:R.

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Day trading loses most people money,
and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --market IN --exchange NSE --instruments trader/state/instruments.json --entry 2950.10 --or-high 2950 --or-low 2920 --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --trading-style positional --market IN --exchange NSE --symbol RELIANCE --entry 2950.10 --stop pct:2 --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol TSLA --entry 364.18 --or-high 362.18 --or-low 354.47
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by HH:MM` (`15:55` ET / `15:20` IST), `--trading-style intraday|positional`,
`--asset-type EQUITY|FUTURES|OPTIONS`, `--market US|IN`, `--exchange NSE|BSE`,
`--instruments PATH`, `--setup NAME`, `--date YYYY-MM-DD`, `--time`,
`--entry-bar N`, `--interval 5m|1m`, `--entry PRICE` (plan ahead on waiting
entry.json), `--offline PATH` (bars for `atr:K`), `--now "2026-10-19 10:15"`,
`--config PATH`, `--json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, time, bar, opening range, exchange, `tick_size`, `invalidation_level` |
| `trader/config.json` | `flat_by` (`15:55` ET / `15:20` IST) and `min_reward_risk` (`1.0`: noted here, trader-risk enforces it) |
| `trader/state/instruments.json` | NSE/BSE master cache for exchange-isolated INR `tick_size` and F&O `lot_size` (India) |
| Yahoo / India provider | only for an `atr:K` stop: session bars up to the entry bar (Yahoo is free and unofficial) |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by, `tick_size`, `lot_size`, `exchange`, `trading_style` |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `breakout-invalidation` | structural invalidation level recorded by `entry.py --setup breakout_20d` (default for `breakout_20d`) |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Tick rounding and precision.** In US mode, the tick size is `$0.01`. In
  India mode (`--market IN`), the stop rounds away from the entry (down for a
  long) and the target rounds half-up to the symbol's authoritative INR
  `tick_size` from the entry document or `--instruments` cache (per exchange
  `NSE` or `BSE`; falls back to NSE `₹0.05` only if no cache or entry tick
  is supplied on a cash equity without `--instruments`). Sub-paisa ticks such
  as `0.0025` keep 4 decimal places in rendered reports and plan fields;
  standard ticks such as `0.05` or `0.10` keep 2 decimals. Conflicting
  `--exchange` or `tick_size` metadata fails closed.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price: too tight to survive the
  spread, normal wiggle would stop you out. An `entry.json` that has not
  triggered (unless you give `--entry`). A short: v2 plans longs only.
- **Flat by and positional mode.** Intraday plans default to `15:55` ET (US,
  moved up to `12:55` on a `1:00 p.m.` early close) or `15:20` IST (India),
  refusing entries at or after `flat_by`. Passing `--trading-style positional`
  sets `flat_by: "positional"` for multi-day delivery holds without an
  end-of-day square-off cutoff.
- **Asset types and F&O lots.** Cash equities use `lot_size: 1`. When
  planning Indian `OPTIONS` or `FUTURES` contracts, `--instruments` resolves
  derivative metadata and `fo_lot_size`.

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout or `breakout_20d`
   unless the user names another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk and flat by (plus
   exchange and tick size in India mode).
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: the reel

TSLA, Monday 2026-10-19. The entry is 364.18 at 10:15 AM (the 10:10 bar,
bar 8) and the opening range is 354.47 to 362.18. `exit.py plan --stop
or-mid --target 1R --write`:

```
Exit plan: TSLA long, Mon 2026-10-19, opening-range breakout on 5-minute bars
  Entry         $364.18 at 10:15 AM (bar 8)
  Stop          $358.32  opening-range midpoint: ($362.18 + $354.47) / 2 = $358.325, rounded down
  Target        $370.04  1R (one times the risk)
  Risk a share  $5.86 (1.61% of the entry)
  Reward:risk   1.0
  Flat by       15:55 ET, win or lose: no overnight hold
```

The default 2R target would be 364.18 + 2 x 5.86 = 375.90. trader-budget then
sizes it: a $50 max loss per trade buys floor(50 / 5.86) = 8 shares.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. v2 plans longs only.

## Intraday Position & Exit Management (`exit.py manage` / `check`)

Evaluate open trades at any hour during an active session against live or latest intraday candles (`1m|5m|15m|30m|1h`), active stops, VWAP/EMA deterioration, risk targets, and session/expiry constraints:

```
python3 <SKILL_DIR>/trader-exit/exit.py manage --plan trader/plan.json --config trader/config.json
python3 <SKILL_DIR>/trader-exit/exit.py manage --symbol RELIANCE --entry 2855.40 --stop 2835.00 --target 2896.20 --qty 26 --market IN --interval 5m --state trader/state/exit_state.json
```

- **Action taxonomy**: `HOLD`, `UPDATE TRAILING STOP`, `PARTIAL EXIT`, `EXIT`, or fail-closed `NO_ACTION` (`STALE_DATA` / `NO_DATA`).
- **Trading style & expiry**: Intraday trades enforce `flat_by` cutoffs (`15:55` ET / `15:20` IST or CAS V1 cutoff); positional equity trades (`--trading-style positional`) do not force evening square-off; derivative contracts warn/exit approaching expiry.
- **On-invocation & deduplicated**: Checks current market conditions whenever invoked and optionally tracks repeat-run transitions (`NEW` / `UPDATED` / `UNCHANGED`) via `--state`.

## Opt-in visualisations

Default output is text/JSON only. Only when the user explicitly requests a chart, render entry/stop/target levels via `../trader-alerts/visuals.py entry-exit` using `trader/plan.json` and fetched bars (see `guides/trader-visuals.md`).

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
