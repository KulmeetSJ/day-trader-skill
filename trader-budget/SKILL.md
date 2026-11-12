---
name: trader-budget
description: >-
  Budget Manager. Sizes every position to your budget and your max loss per
  trade. Reads the trade plan from trader-exit (entry, stop, target), finds
  whole shares (or Indian F&O lots) that fit inside four limits (max loss per
  trade, position cap, max order, cash after your buffer), enforces
  authoritative INR tick sizes, T2T delivery blocks, and Indian statutory
  charges (STT, NSE exchange fees, SEBI, stamp duty, GST + estimated
  brokerage), and writes ONE proposal to trader/orders.json (intraday bracket
  or Indian paper positional GTT OCO). A stop too far away for your max loss
  or missing Indian tick metadata is a plain refusal, not a smaller order.
  Nothing is placed. Use when the user says "how many shares", "size this
  trade", "how much should I buy", "I have $5,000", "I only want to risk $50
  a trade", "set my rules", "keep 10% in cash", or after /trader-exit writes
  a plan.
argument-hint: "[budget in USD or INR]"
---

# trader-budget

The Budget Manager turns a plan ("buy TSLA at 364.18, stop 358.32") into an
exact order ("buy 8 shares"), inside rules the user sets once. The stop
decides the size: the further away the stop, the fewer shares. It writes a
proposal only. trader-risk checks it and trader-broker asks the user yes or
no. Day trading loses most people money. Sizing to a max loss keeps each
loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --market IN --exchange NSE --instruments trader/state/instruments.json --write
python3 "${CLAUDE_SKILL_DIR}/size.py" --trading-style positional --market IN --write
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write        # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 364.00 --json  # a limit order at 364.00
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights AAPL=40,KO=60 --config split.json   # weights mode, below
```

Flags: `--plan`, `--config`, `--budget`, `--limit [PRICE]`, `--prices`,
`--portfolio`, `--market US|IN`, `--exchange NSE|BSE`,
`--trading-style intraday|positional`, `--instruments PATH`, `--now`,
`--write [PATH]` (`trader/orders.json`), `--json`; plus weights mode
(`--weights`, `--targets`, `--top`, `--whole-shares`, `--cents`).
A plan for another day is refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (`config.example.json` for US, `config.in.example.json` for India) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time, `tick_size`, `lot_size` (trader-exit writes it) |
| `trader/state/instruments.json` | NSE/BSE cache (used when present or passed via `--instruments` to resolve/verify INR `tick_size`, `is_trade_to_trade`, F&O `lot_size`) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed order (`bracket` intraday, or paper-only `gtt_oco` positional in India mode) written with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` (US) or
`config.in.example.json` (India) into `trader/config.json` and ask the user for
each value in plain words:

- **budget**: how much money this is for. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr** / **max_loss_per_trade_usd** (or neutral **max_loss_per_trade**): the most one trade may lose if the stop fills at the stop (default 50 USD / 2000 INR).
- **max_position_pct**, **max_order_inr** / **max_order_usd**, **min_order_inr** / **min_order_usd**: the biggest position (75% of the budget), biggest order and smallest order.
- The day limits trader-risk enforces: **max_daily_loss_inr** / **max_daily_loss_usd**, **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:45" / "15:30" ET for US; "09:30" / "15:00" IST for India) and **flat_by** ("15:55" ET for US; "15:20" IST for India).
- **blocked**: tickers they never want to trade.
- Keep **paper** true until they have watched it work on a paper account.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only (and whole F&O contract lots for Indian `OPTIONS`/`FUTURES`):
the size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | `max_loss_per_trade` (`_inr` or `_usd`) / risk a share (entry minus stop) |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order` (`_inr` or `_usd`) / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` |

The entry it sizes at: the limit price with `--limit` (rounded to the
authoritative `tick_size`). For a market order, the plan's entry, or the
price in `trader/prices.json` if it is fresh (under 15 minutes old) and higher.
A price that ran up since the plan means a bigger loss at the same stop, so
fewer shares. A lower price never shrinks the risk.

Indian-market sizing specifics (`--market IN`):

- **Authoritative tick size (fail closed).** Uses `trader/state/instruments.json`
  when present (or `--instruments`), else falls back only to an authoritative
  `tick_size` already recorded on `plan.json`. If `--instruments` does not
  exist, disagrees with the plan, or neither carries a valid `tick_size` for
  an Indian plan, sizing fails closed rather than falling back to `₹0.05`.
  Sub-paisa ticks (such as `0.0025`) keep full decimal precision.
- **Equity vs F&O lots.** Cash equity trades in single shares (`lot_size: 1`);
  Indian `OPTIONS`/`FUTURES` must size in exact multiples of `fo_lot_size`
  (`contracts = qty // lot_size`) and refuse if lot metadata is missing.
- **Trade-to-Trade (T2T) blocks.** Securities in series `BE`/`BZ`/`T` or
  marked `is_trade_to_trade: true` are refused for intraday day trading.
- **Intraday MIS vs Positional GTT OCO.** Intraday orders write `product: "MIS"`,
  `time_in_force: "day"`, `order_class: "bracket"`. Positional orders
  (`--trading-style positional` or positional plan) write Indian paper-only
  `product: "CNC"` (equity) or `"NRML"` (F&O), `time_in_force: "gtc"`,
  `order_class: "gtt_oco"`, and `multi_session_protection` (`persists_across_sessions: true`),
  refusing if `paper` is false or `broker` is not `"paper"`.
- **Transaction costs.** Computes effective-dated Indian statutory levies
  (STT per 2026-04-01 schedule, NSE transaction charges incl. IPFT per
  2026-03-01 schedule, SEBI Rs 10/crore, buy-side stamp duty, 18% GST) separately
  from estimated discount brokerage and prints the breakeven exit price.

It refuses, and writes no order, when: the size is zero shares/lots, the order
is under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the ticker is blocked or T2T intraday, or the plan is
a short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares (and F&O lots if
applicable), the order amount (in USD `$` or INR `₹` depending on market), the
max loss at the stop, and any Indian charge breakdown / breakeven level. Say
plainly that a stop is an order, not a promise: in a fast market it can fill
below the stop. Then hand over to `/trader-risk`.

## Example: the reel

TSLA on Monday 2026-10-19. Opening-range breakout: entry 364.18 at 10:15 AM,
stop 358.32 (the opening-range midpoint), target 370.04 (1R). Risk 5.86 a
share. The example config: $5,000 budget, 10% cash buffer, $50 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | $50 / 5.86 | **8 (decides it)** |
| position cap | 75% of $5,000 = $3,750 / 364.18 | 10 |
| max order | $4,000 / 364.18 | 10 |
| cash | $5,000 minus $500 = $4,500 / 364.18 | 12 |

8 shares = $2,913.44. Max loss at the stop: 8 x 5.86 = $46.88. It writes:

```json
{"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
 "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32",
 "why": "opening-range breakout on TSLA: 8 shares, bound by your $50 max loss per trade (8 x 5.86 = $46.88 at the stop)",
 "plan": {"entry": 364.18, "stop": 358.32, "target": 370.04, "risk_per_share": 5.86, "max_loss": 46.88,
          "setup": "orb", "entry_time": "10:15 AM", "flat_by": "15:55"}}
```

`why` and `plan` are notes for trader-risk and trader-broker. They are never
sent to Alpaca. With the stop at 300.00 instead, one share would risk 64.18,
more than the $50 max loss: no order. At 10:52 the real quote was 366.48, so
the same plan sizes to 6 shares at 8.16 a share, and reward:risk falls to
0.44: refused, because the move it was waiting for has mostly happened.

## Weights mode (kept from v1)

`--weights AAPL=22.04,KO=10.86` or `--targets FILE` (`targets.example.json`
here) splits a budget across a list by weight: the cash buffer comes off the
top, weights are renormalised to 100, any stock over `max_position_pct` is
pinned at the cap and its excess shared out, slices under the minimum order
are dropped, and amounts round to whole currency units (dollars or rupees, or
sub-units with `--cents`) so nothing is left idle by accident. With
`"max_position_pct": 25` and `"fractional": true`, the example file gives AAPL
$1,250 (capped), AXP $1,119, GOOGL $823, KO $708, BAC $600. With
`"fractional": false` (the example config) it buys whole shares and needs
prices (`--prices trader/prices.json`). These are plain buys with no stop:
trader-risk blocks them while `require_stop` is true and counts each one
against `max_trades_per_day`. Use a separate config (`--config`) for a long-term
split.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan, the prices or the instrument master. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes short positions.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
