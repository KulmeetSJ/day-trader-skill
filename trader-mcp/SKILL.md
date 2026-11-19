---
name: trader-mcp
description: AI Trader desk over MCP. Runs the day-trader MCP server so Claude (or any MCP client) researches and PAPER-trades Indian equities and index option buys itself - quotes, bars, rule-based signals, news and results in; previews sized by trader-budget and gated by trader-risk out; place, close and automatic stop/target/flat-by exits on a paper book with two separate Rs 1,00,000 pools (trading = intraday, investing = multi-day). The AI decides and must write a rationale; it can never override a BLOCK or a limit. scripts/ai_trader.py wakes Claude headless when watchers emit new calls or on a schedule. Paper only, no broker routing. Use when asked to "let the AI trade", "start the AI trader", "AI paper account", "MCP trading server", "what did the AI trade", or "show the AI's positions".
---

# trader-mcp: AI Trader over MCP

The other skills are the engine: they scan, size, cost and risk-check. This skill hands the
**decisions** to an AI. The AI reads the market and the rule-based leads, picks the few trades it
believes in, and writes down why. The deterministic engine still owns every number and every gate.

> **Paper only.** Indian trading is paper-only at the schema level and the Upstox integration is
> read-only. Nothing in this skill routes an order to any broker. Education and tooling, not
> financial advice. A real-money order still needs trader-broker and your typed "yes".

## Pieces

| File | What it does |
|---|---|
| `trader-mcp/server.py` | Stdlib-only MCP server (JSON-RPC 2.0 over stdio). Registered in the repo's `.mcp.json` as `day-trader`. |
| `trader-mcp/book.py` | The AI paper book: preview, place, close, mark. Uses `size_trade`, `build_order` and `trader-risk/check.py` unchanged. |
| `scripts/ai_trader.py` | The trigger. Marks the book every 2 minutes in session and wakes Claude headless when there is something new to decide. |
| `trader/state/ai_book.json` | Positions, broker-style order records per pool, open previews, and the journal (rationale + outcome). |

## The two pools

| Pool | Style | Cap | Exits |
|---|---|---|---|
| `trading` | intraday equity, intraday option buys | Rs 1,00,000 committed at once | stop, target, flat by 15:20 |
| `investing` | multi-day equity (positional) | its own Rs 1,00,000 | stop, target, 60-session time stop |

The caps come from the config `budget`. Each pool keeps its own order records, so trader-risk's daily
loss (Rs 5,000 at the stops of today's entries), trades-per-day (3) and duplicate rules apply per pool.
The rule-based ledgers (equity_calls, options_watch) keep running beside this book as benchmarks; they
never share or block its capital.

## Tools

| Tool | Purpose |
|---|---|
| `market_status` | Is NSE open, holiday, IST time. |
| `get_quote`, `get_bars` | Live quote; 1m to 1h intraday bars or daily bars. |
| `get_signals` | Today's intraday alerts and refused setups, the latest multi-day ideas, option spread calls. Leads, not orders. |
| `get_news` | Company headlines (last N days) and the latest quarterly results. |
| `portfolio`, `journal` | Both pools, open positions, realized P&L, risk used today; past rationales and outcomes. |
| `preview_equity_trade` | pool, symbol, stop, target (entry defaults to live). Sized by trader-budget inside the pool's free capital, checked by trader-risk. PASS returns a `preview_id` valid 10 minutes. |
| `preview_option_trades` | Runs the trader-options specialist on an index or F&O stock. Only SIGNAL VALIDATED, actionable BUY_CE / BUY_PE signals get a `preview_id`. Sells and spreads are never placeable here. |
| `place_trade` | preview_id + rationale (required). Paper fill at the live price + 1 tick. Re-checked at fill time. |
| `close_position` | position_id + reason. Discretionary early exit at the live price - 1 tick. |
| `mark_positions` | Apply the automatic exits now. |

## What the AI can and cannot do

The AI can:
- choose which leads to act on, or none;
- choose the stop (where the setup is proven wrong) and the target, inside the hard rules;
- exit early with a reason.

The AI cannot:
- override a BLOCK, resize a refused trade, or change a limit (Rs 2,000 per trade, Rs 5,000 a day,
  3 entries a day, net reward:risk >= 2.0 after charges, 75% position cap, no entries after 15:00);
- go short, sell naked options, or open credit spreads;
- move a stop after entry (exits are rules, run by `mark` without the AI);
- chase: a fill more than 0.25R past the previewed entry is refused;
- place anything live.

## Running it

```bash
# Claude Code picks the server up from .mcp.json (approve the "day-trader" server once).
# Ask: "use the day-trader tools: review the signals and trade the trading pool if anything is worth it"

# Fully automatic: watchers produce leads, the AI trader decides
nohup caffeinate -i python3 scripts/market_watch.py --equities >> trader/state/watch.log 2>&1 &
nohup caffeinate -i python3 scripts/ai_trader.py run >> trader/state/ai_trader.log 2>&1 &

# One decision now (any external API, cron or webhook can call this)
python3 scripts/ai_trader.py trigger --reason "NIFTY broke above yesterday's high"

python3 scripts/ai_trader.py show     # the AI paper account
python3 scripts/ai_trader.py mark     # apply automatic exits now
```

`ai_trader.py run` wakes the AI on: new intraday alerts, every 25 newly refused setups, a new option
spread call, a new multi-day scan, and scheduled reviews at 09:35, 11:00, 13:30 and 15:50 IST. It runs
at most 16 AI sessions a day, at least 10 minutes apart. Each session is `claude -p` with only the
`day-trader` MCP tools allowed.

## Reading the results

Every entry stores the AI's rationale; every exit stores the outcome (net Rs after charges and R).
`journal` and `trader/state/ai_book.json` are the record to judge the AI on. Give it weeks of paper
trades before trusting it with anything: one good day proves nothing, and the backtests in this repo
found no fixed rule with an edge after charges.

## Safety notes

- Fail closed: no price, an expired preview, a changed order fingerprint or a stale instrument master
  is a refusal, never a guess.
- Previews are re-run through trader-risk at fill time, so trades placed in between count.
- Credentials are never logged; the server reads market data only.
- Not financial advice.
