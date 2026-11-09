# The Day Trader skill

11 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar.

Now with **BharatTrader AI**: full native support for **Indian stock markets (NSE / BSE in INR)** alongside **US stock markets (NYSE / NASDAQ in USD)**.

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts, Nasdaq Trader's symbol lists, NSE/BSE
security masters, SEC EDGAR, and regulatory disclosures.

One scans thousands of charts for setups across US and Indian indices (NIFTY 50,
NIFTY 100, F&O). Another tracks breaking news on every stock you watch. One
watches a setup bar by bar in market hours (EST or IST) and tells you the moment
to get in, and another sets your stop and your target before you do. And the
craziest one is the Time Machine: before you risk a dollar, it replays the
rest of the session 10,000 times from the stock's own recent bars, plus every
past day the same setup fired, and tells you the honest odds.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2, now with full Indian
> stock market (NSE/BSE) integration.

## Install

Paste this into Claude:

```
https://github.com/KulmeetSJ/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add KulmeetSJ/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/KulmeetSJ/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install. Standard library only.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans thousands of charts (US equities or Indian NIFTY 50 / NIFTY 100 / F&O) for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks breaking news and corporate disclosures on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar in market hours (EST or IST) and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, rounded to exchange tick sizes (e.g. ₹0.05), with session flat_by rules (15:55 ET / 15:20 IST). |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk capital: 10,000 simulated sessions over historical 5-minute bars (including Indian 75-bar regular / 72-bar CAS grids). |
| `/trader-risk` | Risk Manager | Pre-trade safety gate checking loss limits, market hours, holiday calendars, and SEBI Trade-to-Trade restrictions. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget ($ USD or ₹ INR) and max loss per trade, respecting lot sizes and capital caps. |
| `/trader-broker` | Order Executor | Renders plain-English tickets, paper trading by default, requiring your explicit yes before anything is placed. |
| `/trader-alerts` | Price Alerts | Rings when a price, stop, target, or closing time (15:20 IST / 15:55 ET) hits. |
| `/trader-analyst` | Earnings Analyst | Reads corporate filings and financial reports and sums up what changed. |
| `/trader-pelosi` | Smart Money / Disclosures | Reads institutional, insider and regulatory trading disclosures. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close                                       (while it is open)
news / analyst / pelosi                                                        (on a schedule)
```

### Example 1: US Market Trade (Tesla on NASDAQ)

1. **`/trader-scanner`** at 10:15 AM ET: of the 231 charts in the reel's
   scan, 42 had closed above their 15-minute opening range. TSLA is one: range 354.47 to 362.18,
   the 10:10 candle closed at 364.18, 0.55 % over the top.
2. **`/trader-news`**: the morning's TSLA headlines, in New York time
   ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", 8:04,
   tagged earnings and deal). Tesla reports this week: a day trade is out
   before the close, so it is not exposed to the report.
3. **`/trader-entry`** watched TSLA bar by bar. At 10:12 it said waiting (the
   10:10 bar was still forming). At 10:15 it said triggered: the first
   5-minute close above the range, 364.18.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 358.32, so 5.86 a share at risk; target one times that risk,
   370.04; flat by 3:55 PM either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from TSLA's own candles at that time of day over the
   previous 59 sessions (seed 19, so anyone can repeat it): target first
   29.4 %, stop first 25.5 %, still open at the close 45.1 %. It also re-ran
   the same rule on those 59 real sessions: it fired 33 times, 13 hit the
   target, 15 the stop, 5 neither. Its verdict: close to a coin flip. That is
   the point of looking before you risk a dollar.
6. **`/trader-budget`** with a $5,000 account and a $50 max loss per trade:
   50 / 5.86 = 8 shares ($2,913.44). If the stop fills at 358.32, the loss is
   $46.88. It writes one bracket order: buy 8 TSLA, stop-loss 358.32,
   take-profit 370.04.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, position
   size and cash. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. Claude calls your brokerage connector (Alpaca's MCP
   server, paper account by default), and Claude's own "Allow once / Deny"
   card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target and at 3:55 PM to
   flatten whatever is still open.

### Example 2: Indian Market Trade (Reliance Industries on NSE)

1. **`/trader-scanner`** at 10:35 AM IST (`--market IN`): scans NIFTY constituents.
   RELIANCE triggers an opening-range breakout above its 15-minute opening range
   (09:15-09:30 range ₹2,836.00 to ₹2,848.00; the 10:25 bar closed at ₹2,849.00, +0.04% over).
2. **`/trader-entry`** verifies breakout trigger at 10:30 AM IST at ₹2,849.00.
3. **`/trader-exit`** plans the trade with official tick size ₹0.05: stop at
   opening-range midpoint ₹2,842.00 (risk ₹7.00/share), target 2R at ₹2,863.00,
   flat by 15:20 IST ahead of the closing auction session.
4. **`/trader-timemachine`** replays 10,000 simulations over historical IST
   5-minute bars: draws from RELIANCE's own sessions on the Indian 60-bar intraday grid.
5. **`/trader-budget`** with a ₹1,00,000 account and ₹2,000 max loss sizes the trade:
   position cap (75% = ₹75,000) binds at 26 shares (₹74,074). Max loss at stop is ₹182.
   Generates bracket order with INR pricing.
6. **`/trader-risk`** verifies pre-trade rules: IST entry window (09:30-15:00 IST),
   NSE holiday calendar, non-T2T equity series verification, and loss limits. PASS.
7. **`/trader-broker`** renders the ticket in plain English (`ORDER 1 of 1, PAPER account, Paper Broker (NSE/BSE)`).
   User types "yes buy 26 RELIANCE". Order executed in paper portfolio (`trader/portfolio.json`).
8. **`/trader-alerts`** monitors the position: stop (₹2,842.00), target (₹2,863.00),
   and rings at 15:20 IST: `ALERT Flat by close: it is 15:20 IST`.
9. **`/trader-broker flatten`** generates sell ticket for 26 shares, passes risk,
   and closes the position before 15:30 IST market close.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
# US Stock Market (default)
python3 trader-scanner/scan.py universe                                # ~5,900 US stocks, Nasdaq Trader
python3 trader-scanner/scan.py daily                                   # pre-market setups, whole universe
python3 trader-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 trader-news/news.py --tickers TSLA --hours 12                  # headlines, 8-Ks, today's move
python3 trader-entry/entry.py check TSLA --setup orb                   # waiting / triggered / expired
python3 trader-entry/entry.py watch TSLA --every 30                    # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py TSLA                                   # quarters, growth, EPS
python3 trader-pelosi/ptr.py index --member pelosi                     # her PTRs this year

# Indian Stock Market (NSE / BSE)
python3 trader-scanner/scan.py universe --market IN --index NIFTY50   # NIFTY 50 universe
python3 trader-scanner/scan.py orb --market IN --at 10:35             # Indian ORB scan
python3 trader-entry/entry.py check RELIANCE --setup orb --market IN   # IST bar-by-bar entry timer
python3 trader-exit/exit.py plan --from trader/entry.json --market IN  # Tick size ₹0.05, flat_by 15:20 IST
python3 trader-timemachine/timemachine.py run --plan trader/plan.json --market IN  # 10k IST replays
python3 trader-budget/size.py --plan trader/plan.json --config trader/config.json --market IN  # INR sizing
python3 trader-risk/check.py --orders trader/orders.json --config trader/config.json --market IN  # Pre-trade gate
python3 trader-broker/ticket.py show --orders trader/orders.json --market IN  # Plain ticket (₹)
python3 trader-alerts/alerts.py clock --market IN                      # NSE market & CAS phase status
python3 trader-alerts/alerts.py quote RELIANCE TCS --market IN         # Delayed quotes (₹)
python3 trader-alerts/alerts.py check --market IN                      # Evaluate stop/target/flat-by
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: TSLA's 5-minute bars
for the 60 days to October 19 2026, RELIANCE's 5-minute bars on NSE, the
10:15 AM opening-range scan, Yahoo's TSLA headlines that morning, the House
Clerk's 2026 index, real PTR text, and real XBRL facts. They reproduce every
number in the examples above.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your brokerage tool to "Needs approval" in Claude
(Customize, Connectors, tool permissions), never "Always allow", so Claude's
own "Allow once / Deny" card is a second check. No options, margin, short
selling or leverage unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json` and
`config.in.example.json`. Keys live in environment variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own recent 5-minute bars. The future can do things past sessions
never did: an earnings report, a halt, a headline. Treat its odds as a
reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price. The max loss shown is the loss at
the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. The scanner and the entry
timer read 1-minute or 5-minute bars, not every tick. Check anything that
matters against your broker before you act on it.

**Day-trading rules & regulatory frameworks:**
- **US Markets**: FINRA removed the $25,000 pattern-day-trader minimum on June 4, 2026 (Regulatory Notice 26-10), and brokers may phase the change in until October 20, 2027. If yours still applies the rule, set `"pdt_guard": true` and `/trader-risk` counts your day trades.
- **Indian Markets (SEBI)**: The US PDT rule does not apply in India. However, SEBI Trade-to-Trade (T2T / BE / BZ series) regulations strictly prohibit intraday squaring-off for securities transferred to the trade-for-trade segment. `/trader-risk` inspects instrument metadata and automatically blocks any intraday buy/sell proposal for T2T stocks.
- **NSE Closing Auction Session (CAS)**: Eligible F&O stocks participate in the Closing Auction Session starting at 15:15 IST. Day trades should be flattened before 15:20 IST (`"flat_by": "15:20"`).

**Taxes.** Day-trading gains are short-term gains, and frequent trading makes
wash sales likely. In India, speculative intraday equity trading is taxed as business income under Section 43(5) of the Income Tax Act. Nothing here is tax advice.

## BharatTrader AI (Indian Market Support)

BharatTrader AI adapts the trading tools for the Indian equity and derivatives market (NSE and BSE).

### Market Data Configuration

Set the environment variable `BHARAT_DATA_PROVIDER` to choose the data source:

- `yahoo` (default): Free market data with automated NSE (`.NS`) and BSE (`.BO`) exchange suffix translation.
- `offline`: Local JSON or JSON.GZ bar and quote files for offline analysis and deterministic backtesting.

### Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `BHARAT_MARKET` | Market mode (`IN` for India, default `US`) | `US` |
| `BHARAT_DATA_PROVIDER` | Market data provider (`yahoo`, `offline`) | `yahoo` |
| `BHARAT_INSTRUMENTS_CACHE` | Path to local instrument metadata cache | `trader/state/instruments.json` |

### Data Quality Tiers

All market data is classified into explicit quality tiers:
- `DELAYED`: Delayed by 15 minutes (Yahoo Finance). Never treated or presented as real-time.
- `HISTORICAL`: Completed historical bars from closed trading sessions.
- `SIMULATED`: Generated or replayed backtest data.
- `REAL_TIME`: Real-time un-delayed feeds (reserved for authenticated broker connections).

### Usage Examples

1. Check Indian market session status and Closing Auction Session (CAS) phase:

```bash
python3 trader-alerts/alerts.py clock --market IN
```

2. Fetch delayed quotes for NSE securities:

```bash
python3 trader-alerts/alerts.py quote RELIANCE TCS INFY --market IN
```

3. Fetch historical 5-minute intraday bars aligned to IST trading hours (09:15-15:30 IST):

```bash
python3 trader-alerts/alerts.py history RELIANCE --market IN --interval 5m --range 5d
```

4. Build Indian stock universe (`trader/universe.txt`):

```bash
python3 trader-scanner/scan.py universe --market IN --index NIFTY50   # 50 Nifty constituents
python3 trader-scanner/scan.py universe --market IN --index NIFTY100  # 100 Nifty constituents
python3 trader-scanner/scan.py universe --market IN --index FNO       # F&O eligible stocks
```

5. Run pre-market daily setup scan (breakouts, flags, oversold) on Indian stocks:

```bash
python3 trader-scanner/scan.py daily --market IN
```

6. Run opening-range breakout scan (15-min range 09:15-09:30 IST, first breakout at 09:35 IST):

```bash
python3 trader-scanner/scan.py orb --market IN
python3 trader-scanner/scan.py orb --market IN --at 10:15
```

7. Real-time entry timing check on Indian stocks (IST market hours, 15m ORB):

```bash
python3 trader-entry/entry.py check RELIANCE --setup orb --market IN
```

8. Exit plan generation with Indian tick sizes (Rs 0.05 default) and IST closing rules (flat_by 15:20 IST):

```bash
python3 trader-exit/exit.py plan --symbol RELIANCE --entry 2850.00 --stop or-mid --target 2R --market IN --write trader/plan.json
python3 trader-exit/exit.py show --plan trader/plan.json
```

9. Historical Monte Carlo replay on Indian session grid (75 bars regular / 72 bars CAS V1):

```bash
python3 trader-timemachine/timemachine.py run --plan trader/plan.json --market IN
python3 trader-timemachine/timemachine.py chart --plan trader/plan.json --market IN
```

10. Position sizing and capital allocation in INR with lot sizing and Trade-to-Trade checks:

```bash
python3 trader-budget/size.py trade --plan trader/plan.json --config trader/config.json --market IN --write trader/orders.json
```

11. Pre-trade risk validation with Indian session window, SEBI Trade-to-Trade enforcement, and PDT exemption:

```bash
python3 trader-risk/check.py trader/orders.json --config trader/config.json --market IN
```

12. Use the Python market data API:

```python
from data_in import fetch_quote, fetch_intraday_bars, detect_setups

# Fetch current delayed quote
quote = fetch_quote("RELIANCE", exchange="NSE")
print(quote["price"], quote["currency"], quote["tier"])

# Fetch 5-minute session bars
intraday = fetch_intraday_bars("TCS", interval="5m", range_str="5d", exchange="NSE")
print("Fetched %d continuous bars" % len(intraday["bars"]))
```

### Complete End-to-End Indian Paper-Trading Walkthrough

Run the full pipeline offline or with live data, from opening-range breakout scan through risk validation, order tickets, alerts tracking, and session flattening:

```bash
# Setup Indian trading configuration
cp trader-budget/config.in.example.json trader/config.json

# 1. Chart Scanner: Scan for opening-range breakouts (15-min range 09:15-09:30 IST)
python3 trader-scanner/scan.py orb --market IN --offline tests/fixtures/bharat --now "2026-10-19 10:35" --out trader/watchlist.json

# 2. Entry Timer: Verify breakout trigger on RELIANCE at 10:30 AM IST (bar 14 close > range high)
python3 trader-entry/entry.py check RELIANCE --setup orb --offline tests/fixtures/bharat/RELIANCE-5m.json --now "2026-10-19 10:35" --market IN --write trader/entry.json

# 3. Exit Planner: Set stop-loss, 2R target and 15:20 IST closing rule with Rs 0.05 tick size
python3 trader-exit/exit.py plan --from trader/entry.json --market IN --write trader/plan.json

# 4. Time Machine: 10,000-session Monte Carlo simulation over historical IST session bars
python3 trader-timemachine/timemachine.py run --plan trader/plan.json --offline tests/fixtures/trader-timemachine/RELIANCE-60d5m.json.gz --market IN

# 5. Budget Manager: Size bracket order to INR account budget and max loss limits
python3 trader-budget/size.py --plan trader/plan.json --config trader/config.json --now "2026-10-19 10:35" --market IN --write trader/orders.json

# 6. Risk Manager: Pre-trade safety gate (max loss, trade counts, market hours, T2T)
python3 trader-risk/check.py --orders trader/orders.json --config trader/config.json --now "2026-10-19 10:35" --market IN --out trader/state/risk.json

# 7. Order Executor: Render plain-English ticket and confirm order is READY
python3 trader-broker/ticket.py show --orders trader/orders.json --risk trader/state/risk.json --config trader/config.json --now "2026-10-19 10:35" --market IN

# 8. User Approval: Record explicit yes for the entry order
python3 trader-broker/ticket.py approve --index 1 --reply "yes buy 26 RELIANCE" --orders trader/orders.json --risk trader/state/risk.json --config trader/config.json --now "2026-10-19 10:35" --market IN

# 9. Broker Fill: Record paper execution in trader/portfolio.json
python3 trader-broker/ticket.py result --index 1 --status placed --filled-qty 26 --filled-price 2849.00 --orders trader/orders.json --risk trader/state/risk.json --config trader/config.json --portfolio trader/portfolio.json --state trader/state/broker.json --now "2026-10-19 10:35" --market IN

# 10. Price Alerts: Arm stop, target, and flat-by-close rules
python3 trader-alerts/alerts.py add --from-plan --plan trader/plan.json --config trader/config.json

# 11. Alert Monitor: Check prices mid-session
python3 trader-alerts/alerts.py check --now "2026-10-19 10:40" --market IN --offline tests/fixtures/bharat

# 12. Flat-by Alert: Time alert fires at 15:20 IST before closing auction
python3 trader-alerts/alerts.py check --now "2026-10-19 15:20" --market IN --offline tests/fixtures/bharat

# 13. Session Flatten: Build sell ticket to close open positions before market close
python3 trader-broker/ticket.py flatten --symbol RELIANCE --config trader/config.json --portfolio trader/portfolio.json --plan trader/plan.json --orders trader/orders.json --now "2026-10-19 15:20"

# 14. Exit Risk Check: Risk manager approves flatten exit order
python3 trader-risk/check.py --orders trader/orders.json --config trader/config.json --portfolio trader/portfolio.json --state trader/state/broker.json --now "2026-10-19 15:20" --market IN --out trader/state/risk.json

# 15. Approve Exit: Record user yes for the sell ticket
python3 trader-broker/ticket.py approve --index 1 --reply "yes sell 26 RELIANCE" --orders trader/orders.json --risk trader/state/risk.json --config trader/config.json --now "2026-10-19 15:20"

# 16. Record Exit Fill: Position flattened, cash and P&L updated
python3 trader-broker/ticket.py result --index 1 --status placed --filled-qty 26 --filled-price 2863.00 --orders trader/orders.json --risk trader/state/risk.json --config trader/config.json --portfolio trader/portfolio.json --state trader/state/broker.json --now "2026-10-19 15:20"

# 17. Decision Log: Inspect complete audit trail of today's trades
python3 trader-broker/ticket.py log --state trader/state/broker.json --config trader/config.json --now "2026-10-19 15:20"
```

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-pelosi/      SKILL.md, ptr.py
tests/              unittest suite + offline fixtures
```

MIT. Made by [Jake Schincariol](https://opusjake.ai).
