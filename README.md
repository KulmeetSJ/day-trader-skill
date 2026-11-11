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
python3 trader-news/news.py --tickers RELIANCE TCS INFY --hours 48    # Indian corporate news in IST
python3 trader-entry/entry.py check RELIANCE --setup orb --market IN   # IST bar-by-bar entry timer
python3 trader-entry/entry.py check RELIANCE --setup breakout_20d --market IN  # 20d breakout + intraday confirmation
python3 trader-exit/exit.py plan --from trader/entry.json --market IN  # Tick size ₹0.05, flat_by 15:20 IST
python3 trader-timemachine/timemachine.py run --plan trader/plan.json --market IN  # 10k IST replays
python3 trader-budget/size.py --plan trader/plan.json --config trader/config.json --market IN  # INR sizing
python3 trader-risk/check.py --orders trader/orders.json --config trader/config.json --market IN  # Pre-trade gate
python3 trader-broker/ticket.py show --orders trader/orders.json --market IN  # Plain ticket (₹)
python3 trader-alerts/alerts.py clock --market IN                      # NSE market & CAS phase status
python3 trader-alerts/alerts.py quote RELIANCE TCS --market IN         # Delayed quotes (₹)
python3 trader-alerts/alerts.py check --market IN                      # Evaluate stop/target/flat-by
python3 trader-analyst/facts.py RELIANCE TCS INFY                      # Indian quarterly & TTM fundamentals
python3 trader-analyst/facts.py RELIANCE --annual                      # 4-year annual financials
python3 trader-analyst/facts.py RELIANCE --json                        # Full structured fundamentals JSON
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

### Pre-Market Readiness Checklist

Before trading any Indian market session, run the automated readiness check to verify market status, instrument masters, expiry calendars, data tiers, and safety gates:

```bash
# Pre-market health and readiness check for Indian markets (NSE/BSE)
python3 trader-alerts/alerts.py readiness --market IN

# JSON output mode for programmatic monitoring or automated sidecars
python3 trader-alerts/alerts.py readiness --market IN --json
```

The readiness command reports four independent statuses:
- **Market Session** (`OPEN` / `CLOSED`): Indicates whether the continuous exchange trading session (09:15-15:30 IST) is currently active.
- **Data Availability** (`FRESH` / `DELAYED` / `STALE` / `UNAVAILABLE`): Status of incoming quotes and cache freshness.
- **Analysis Engine** (`AVAILABLE` / `UNAVAILABLE`): Confirms that stock screening, technical indicators, fundamentals, and options analytics are operable.
- **Paper Trading** (`AVAILABLE` / `UNAVAILABLE`): Confirms that simulated bracket execution and risk gates are operable.
- **Last Trading Session**: The date of the most recent completed or active exchange trading session.

### 24/7 Research, Analytics & Paper Trading

All stock research, screening, and paper trading capabilities operate 24/7, including weekends, exchange holidays, and off-market hours:

1. **Available 24/7 Regardless of Market Hours**:
   - Fundamental analysis and filings (`trader-analyst/facts.py`)
   - Pre-market and daily technical screening (`trader-scanner/scan.py daily`)
   - News monitoring (`trader-news/news.py`)
   - Historical replay and Monte Carlo backtesting (`trader-timemachine/timemachine.py`)
   - Options chain generation and Black-Scholes Greeks (`trader-alerts/data_in.py`, marked `SIMULATED`)
   - Exit planning (`trader-exit/exit.py plan`)
   - Position sizing and bracket order generation (`trader-budget/size.py trade`)
   - Pre-trade risk validation in paper mode (`trader-risk/check.py`)

2. **Historical Candle Analysis vs. Live Signals**:
   - During live continuous trading (09:15-15:30 IST), strategies emit live entry signals (`TRIGGERED: This is the moment to get in`).
   - Outside continuous market hours (weekends, holidays, evenings), strategies analyze the latest completed session's historical bars. Signals are explicitly tagged `[HISTORICAL ANALYSIS]` with the last session date, data status, and notice that the market is closed. Stale data is never presented as a live signal.

3. **Exchange-Session Restrictions for Live Operations**:
   - Continuous polling loops (`entry.py watch`) halt immediately when the exchange is closed (`market closed today`).
   - Live order execution (`paper: false`) remains strictly blocked outside market hours. Paper trading and hypothetical trade analysis remain available off-hours with safety limits (stops, max loss, position cap) fully enforced.

**Pre-Market Runbook (Step-by-Step Pre-Flight):**
1. **Sync Exchange Instrument Masters**:
   Fetch current NSE and BSE equity listings and F&O contract lot sizes:
   ```bash
   python3 trader-alerts/instruments.py sync
   ```
2. **Verify Session Readiness**:
   Run `python3 trader-alerts/alerts.py readiness --market IN` and verify that all 6 core subsystem checks pass:
   - **Market Calendar & Session**: Confirms trading day vs holiday (e.g. Diwali Balipratipada) or special Muhurat trading sessions.
   - **Instrument Master Cache**: Confirms local cache freshness (< 1 day old) across 2,600+ equities and 220+ F&O underlyings.
   - **Data Provider & Tiers**: Confirms data quality classification: Quotes are `DELAYED` (15m), options are `SIMULATED`.
   - **Universe Population**: Verifies constituents for selected index (NIFTY50, NIFTY100, NIFTY500, FNO, SENSEX, ALL, or CUSTOM).
   - **Derivatives & Expiry Metadata**: Verifies upcoming expiry dates (Tuesday for NSE NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY and stock derivatives, Friday for BSE SENSEX, Monday for BSE BANKEX), strike steps, and lot sizes.
   - **Configuration & Safety Limits**: Confirms `paper: true` is active, positive capital budget, max loss per trade, and mandatory stop requirement.

3. **Inspect Derivative Contracts**:
   Verify authoritative contract resolution before market open:
   ```bash
   python3 trader-alerts/instruments.py inspect RELIANCE
   python3 trader-alerts/instruments.py inspect NIFTY26NOV24000CE
   ```

### Market Data Configuration

No Upstox account, login, or API key is required to use this skill. By default it runs out of the box with free delayed Yahoo Finance data (`yahoo`) and local fixture files (`offline`).

Set `INDIA_MARKET_DATA_PROVIDER` (or `india_market_data_provider` in config, or legacy `BHARAT_DATA_PROVIDER`) to choose the Indian market data source:

- `yahoo` (default): Free market data with automated NSE (`.NS`) and BSE (`.BO`) exchange suffix translation (`DELAYED` 15m, no credentials required).
- `upstox` (optional): Read-only live market data via an Upstox Analytics Token (`UPSTOX_ANALYTICS_TOKEN`). Supports V3 quotes (`quotes`, `ltp`, `ohlc`), V3 historical/intraday candles with automatic lookback chunking, live option chains with Open Interest and Greeks, exchange instrument masters, market status, and V3 WebSocket streaming (`trader-alerts/upstox_feed.py`). No OAuth login flow or live order placement is enabled.
- `offline`: Local JSON or JSON.GZ bar and quote files for offline analysis and deterministic backtesting.

#### Option 1: Yahoo-Only Setup (Default, No Credentials Needed)

Works immediately after cloning with no `.env` file or API keys:

```bash
cp trader-budget/config.in.example.json trader/config.json
python3 trader-alerts/alerts.py readiness --market IN
python3 trader-alerts/alerts.py quote RELIANCE --market IN
```

#### Option 2: Optional Upstox Setup Using `.env`

If you already have an Upstox read-only Analytics Token, create an untracked `.env` file in the project root (`.env` is listed in `.gitignore`):

```dotenv
INDIA_MARKET_DATA_PROVIDER=upstox
UPSTOX_ANALYTICS_TOKEN="<your-readonly-analytics-token>"
```

How `.env` loading works:
- Tools automatically load `.env` from the project root (never from arbitrary working directories) using Python's standard library only.
- Supports `KEY=value`, `'single-quoted'`, and `"double-quoted"` values. Does not run shell commands or expand variables, and never prints or logs secret values.
- Existing process environment variables always override values in `.env`.

Verify Upstox readiness and quotes:

```bash
python3 trader-alerts/alerts.py readiness --market IN
python3 trader-alerts/alerts.py quote RELIANCE --market IN
```

Switch back to free Yahoo data at any time without editing code:
- Override in your shell for one command (`INDIA_MARKET_DATA_PROVIDER=yahoo python3 trader-alerts/alerts.py readiness --market IN`), or
- Change `INDIA_MARKET_DATA_PROVIDER=yahoo` (or remove/comment out that line) in `.env`.

Notes on Upstox error handling:
- Never commit or paste `UPSTOX_ANALYTICS_TOKEN` into tracked files or logs. Credentials are never printed in CLI output or error messages.
- If `INDIA_MARKET_DATA_PROVIDER=upstox` is selected without a valid token, tools fail explicitly with `AuthenticationConfigError` and never fabricate data or silently fall back to Yahoo.
- To opt into explicit fallback on auth failure, set `"allow_yahoo_fallback": true` and `"fallback_provider": "yahoo"` in config (or `INDIA_MARKET_DATA_ALLOW_YAHOO_FALLBACK=1`). Fallback payloads remain strictly tagged `provider: "yahoo"`, `is_live: false`, and `DELAYED`.

### Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `BHARAT_MARKET` | Market mode (`IN` for India, default `US`) | `US` |
| `INDIA_MARKET_DATA_PROVIDER` | Market data provider (`yahoo`, `upstox`, `offline`) | `yahoo` |
| `UPSTOX_ANALYTICS_TOKEN` | Optional read-only Upstox Analytics Token (only when using `upstox`) | unset |
| `INDIA_MARKET_DATA_ALLOW_YAHOO_FALLBACK` | Explicitly allow fallback to delayed Yahoo when Upstox auth fails (`1`/`true`) | unset (`false`) |
| `BHARAT_DATA_PROVIDER` | Legacy alias for `INDIA_MARKET_DATA_PROVIDER` | `yahoo` |
| `BHARAT_INSTRUMENTS_CACHE` | Path to local instrument metadata cache | `trader/state/instruments.json` |

### Application Clock Override (`--now`)

Most CLI tools accept `--now "YYYY-MM-DD HH:MM"` (market local time: New York ET for US, IST for `--market IN`; or ISO with `Z`):
- **Purpose**: `--now` overrides the application clock for historical session replay, lookahead-free backtesting, and deterministic tests.
- **Safety distinction**: Any run using `--now` marks the evaluation as clock-overridden (`clock_override: true`, `is_live_signal: false`). Even when paired with a live-capable provider, clock-overridden signals are downgraded from `CURRENT` to `HISTORICAL` and are never presented as currently actionable live entry recommendations (`ticket.py show` displays a historical/clock-override warning rather than `LIVE`).
- **Paper workflows preserved**: Offline fixtures, `DELAYED` Yahoo research, and historical `--now` paper pipelines continue to run end-to-end through `exit.py`, `size.py`, `check.py` (with `paper: true`), and `ticket.py`.

### Data Quality Tiers

All market data is classified into explicit quality tiers:
- `DELAYED`: Delayed by 15 minutes (Yahoo Finance). Never treated or presented as real-time.
- `HISTORICAL`: Completed historical bars from closed trading sessions, or clock-overridden `--now` replays.
- `FIXTURE`: Local offline snapshot files (`--offline`).
- `SIMULATED`: Generated or synthetic backtest/derivatives data.
- `CURRENT` / `REAL_TIME`: Live un-delayed feeds (only on live-capable providers during an active session without `--now`).

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

4. Build Indian stock universe (`trader/universe.txt`) across NSE and BSE:

```bash
python3 trader-scanner/scan.py universe --market IN --index NIFTY50       # 50 Nifty constituents
python3 trader-scanner/scan.py universe --market IN --index NIFTY100      # 100 Nifty constituents
python3 trader-scanner/scan.py universe --market IN --index NIFTY200      # 200 Top Market-Cap constituents
python3 trader-scanner/scan.py universe --market IN --index NIFTY500      # 500 Broad-Market constituents
python3 trader-scanner/scan.py universe --market IN --index BANKNIFTY     # 12 Major Banking stocks
python3 trader-scanner/scan.py universe --market IN --index FINNIFTY     # 20 Financial Services stocks
python3 trader-scanner/scan.py universe --market IN --index SENSEX        # 30 BSE Sensex Bluechips
python3 trader-scanner/scan.py universe --market IN --index BSE100        # 100 BSE Large-Caps
python3 trader-scanner/scan.py universe --market IN --index FNO           # All F&O eligible underlying stocks
python3 trader-scanner/scan.py universe --market IN --index CASH          # All active listed cash equities
python3 trader-scanner/scan.py universe --market IN --index CUSTOM        # Custom watchlist from trader/config.json
```

5. Run daily setup scan (breakouts, flags, oversold, MA crossovers, RSI momentum):

```bash
python3 trader-scanner/scan.py daily --market IN
python3 trader-scanner/scan.py daily --market IN --strategy ma_crossover
python3 trader-scanner/scan.py daily --market IN --strategy rsi_momentum
```

6. Run opening-range breakout scan (15-min range 09:15-09:30 IST, first breakout at 09:35 IST):

```bash
python3 trader-scanner/scan.py orb --market IN
python3 trader-scanner/scan.py orb --market IN --at 10:15
```

7. Real-time entry timing check on Indian stocks and options:

```bash
# Equity ORB check
python3 trader-entry/entry.py check RELIANCE --setup orb --market IN

# Daily 20-day closing-high breakout with completed-candle intraday confirmation
python3 trader-entry/entry.py check RELIANCE --setup breakout_20d --market IN
python3 trader-entry/entry.py check INFY --setup breakout_20d --offline tests/fixtures/data_dir --now "2026-10-19 10:35" --market IN
python3 trader-entry/entry.py watchlist --setup breakout_20d --market IN --watchlist trader/watchlist.json

# Options ORB check: Triggers breakout on NIFTY index and selects ATM Call option
python3 trader-entry/entry.py check NIFTY --setup options_orb --strike-selection ATM --market IN

# Positional MA Crossover check
python3 trader-entry/entry.py check TCS --setup ma_crossover --trading-style positional --market IN
```

8. Exit plan generation with Indian tick sizes (Rs 0.05 default), options, and closing rules:

```bash
# Equity exit plan: stop at opening-range midpoint, 2R target, flat_by 15:20 IST
python3 trader-exit/exit.py plan --symbol RELIANCE --entry 2850.00 --stop or-mid --target 2R --market IN --write trader/plan.json

# Option contract exit plan: stop 20% under entry premium, 2R target
python3 trader-exit/exit.py plan --symbol NIFTY26OCT24000CE --entry 125.00 --stop pct:20 --target 2R --market IN --write trader/plan.json

# Positional trade plan: multi-day hold permitted (no 15:20 intraday cutoff)
# Inherited breakout_20d invalidation stops report stop_label="breakout invalidation"
python3 trader-exit/exit.py plan --symbol INFY --entry 1920.00 --stop pct:2 --target 2R --trading-style positional --market IN --write trader/plan.json
python3 trader-exit/exit.py show --plan trader/plan.json
```

9. Historical Monte Carlo replay on Indian session grid (75 bars regular / 72 bars CAS V1):

```bash
python3 trader-timemachine/timemachine.py run --plan trader/plan.json --market IN
python3 trader-timemachine/timemachine.py chart --plan trader/plan.json --market IN
```

10. Position sizing and capital allocation in INR with lot sizing, statutory charges, Trade-to-Trade checks, and style-aware order semantics:

- **Intraday orders (`--trading-style intraday`, e.g. `orb`)**: emit `time_in_force: "day"`, `order_class: "bracket"`, `product: "MIS"`, intraday `flat_by: "15:20"`, and intraday transaction charges.
- **Positional orders (`--trading-style positional`, e.g. `breakout_20d`)**: emit `time_in_force: "gtc"`, `order_class: "gtt_oco"`, `product: "CNC"` (`"NRML"` for F&O), `flat_by: "positional"`, delivery transaction charges (0.1% STT on both legs, 0.015% stamp, Rs 40 per buy/sell order / Rs 80 round-trip delivery brokerage), and explicit `multi_session_protection` (`persists_across_sessions: true`) so stop-loss and target triggers stay armed across sessions under Indian paper trading (`broker: "paper"`, `paper: true`). If the configured broker or schema cannot safely represent multi-session GTT OCO protection (for example `broker: "alpaca"`, `broker: "ibkr"`, live `paper: false`, or `broker_supports_multi_session: false`), `size.py`, `check.py`, and `ticket.py` fail clearly instead of issuing an expiring intraday order.

```bash
# Equity position sizing (single shares, T2T protection, statutory STT/GST calculation)
python3 trader-budget/size.py trade --plan trader/plan.json --config trader/config.json --market IN --write trader/orders.json

# Explicit positional override (CNC + GTC + GTT OCO multi-session protection + delivery charges)
python3 trader-budget/size.py trade --plan trader/plan.json --config trader/config.json --trading-style positional --market IN --write trader/orders.json

# Option position sizing (sizes in whole lots e.g. 25 shares/contract, caps max loss to premium outlay)
python3 trader-budget/size.py trade --plan trader/plan.json --config trader/config.json --market IN --write trader/orders.json
```

11. Pre-trade risk validation with Indian session window, SEBI Trade-to-Trade enforcement, lot multiples, and naked option rejection:

```bash
python3 trader-risk/check.py trader/orders.json --config trader/config.json --market IN
```

12. Broker ticket provenance and historical/delayed signal warnings (`ticket.py show` & `show --json`):
    - Signal provenance (`data_tier`, `is_historical_analysis`, `is_live_signal`, `provider`, `last_trading_session`, `signal_freshness`, `last_data_timestamp`, `simulated_at`) is propagated from `trader-entry` -> `trader-exit` -> `trader-budget` -> `trader-risk` -> `trader-broker`.
    - `ticket.py show` clearly distinguishes **HISTORICAL**, **DELAYED**, **FIXTURE**, **SIMULATED**, **LIVE**, and **UNVERIFIED** (missing/partial metadata) signals, printing prominent warnings such as `WARNING: HISTORICAL SIGNAL -- NOT A CURRENT ENTRY RECOMMENDATION` for historical analysis while still allowing valid paper workflows and keeping fingerprint + three-gate approvals intact.
    - Yahoo signals (even with missing/conflicting tier flags) and incomplete metadata are conservatively classified as non-live (`is_live_signal: false`).

```bash
python3 trader-broker/ticket.py show --orders trader/orders.json --risk trader/state/risk.json --config trader/config.json --market IN --json
```

13. Use the Python market data API:

```python
from data_in import fetch_quote, fetch_intraday_bars, fetch_option_chain, calculate_bs_greeks

# Fetch current delayed quote
quote = fetch_quote("RELIANCE", exchange="NSE")
print(quote["price"], quote["currency"], quote["tier"])

# Fetch 5-minute session bars
intraday = fetch_intraday_bars("TCS", interval="5m", range_str="5d", exchange="NSE")
print("Fetched %d continuous bars" % len(intraday["bars"]))

# Fetch Black-Scholes analytical option chain with Greeks and Open Interest
chain = fetch_option_chain("NIFTY", spot_price=24250.0, days_to_expiry=4)
atm = chain.atm_strike
print("ATM Strike:", atm, "Call Premium:", chain.calls[atm]["ltp"], "Delta:", chain.calls[atm]["delta"])
```

14. Fundamental analysis for Indian-listed companies (NSE & BSE):

```bash
# Analyze single Indian company (quarterly statements, margins, publication dates, TTM)
python3 trader-analyst/facts.py RELIANCE

# Compare multiple Indian companies
python3 trader-analyst/facts.py RELIANCE TCS INFY

# Include full 4-year annual financials table
python3 trader-analyst/facts.py RELIANCE --annual

# JSON output mode for programmatic consumption
python3 trader-analyst/facts.py RELIANCE --json

# Merge fundamental metrics into screener metrics file
python3 trader-analyst/facts.py RELIANCE TCS INFY --metrics trader/metrics.json

# Offline evaluation with saved fixtures (zero internet access required)
python3 trader-analyst/facts.py RELIANCE --offline tests/fixtures/trader-analyst/
```

### Indian Company Fundamental Analysis (`trader-analyst/facts.py`)

Provides comprehensive fundamental analysis for Indian-listed equities (NSE & BSE) using public exchange corporate disclosures:

- **Supported Metrics**:
  - **Quarterly Financials**: Revenue, Operating Profit (Operating Income / EBITDA), Net Profit, Diluted EPS, Gross Margin %, Operating Margin %, Net Margin %, YoY Growth % vs. same quarter a year earlier, and Official Publication Date.
  - **Annual Statements**: 4-year financial progression with annual Revenue, Operating Profit, Net Profit, Diluted EPS, Margins, and YoY Growth %.
  - **Trailing Twelve Months (TTM)**: Sum of latest 4 quarters for revenue, operating profit, net profit, and diluted EPS.
  - **Balance Sheet Metrics**: Reported Total Assets, Total Debt, Cash & Cash Equivalents, Stockholders' Equity, Debt-to-Equity Ratio, Current Ratio, Book Value per share, and Price-to-Book.
  - **Cash Flow Metrics**: Annual Operating Cash Flow and Free Cash Flow.
  - **Next Earnings Date**: Estimated date of the upcoming corporate quarterly results announcement.
- **Data Quality & Hygiene**:
  - **Clear Source Label**: Every report displays `[SOURCE: NSE/BSE Corporate Filings via Yahoo Finance | DATA: REPORTED | STATUS: FRESH]` along with the exact reporting period, filing publication date, and freshness timestamp.
  - **Fail-Closed Missing Values**: Missing data points remain `-` (`null` in JSON) and are never silently substituted with zero.
  - **No Fabrication**: Consensus estimates are clearly labeled as estimates; reported numbers strictly reflect exchange filings.
  - **Currency & Units**: Financial figures are displayed in Indian exchange standards (INR Crores, where 1 Cr = Rs 10,000,000, except per-share EPS in Rs) or US$ Millions for ADR filers (e.g. INFY).
- **Data Limitations**:
  - Service companies (IT consultancies) and banks that do not report traditional Cost of Goods Sold show gross margin as `-`.
  - Historical depth covers available public quarterly reporting history (typically 4-5 quarters and 4 fiscal years).

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

# 5. Budget Manager: Size bracket order to INR account budget, lot size, and max loss limits
python3 trader-budget/size.py --plan trader/plan.json --config trader/config.json --now "2026-10-19 10:35" --market IN --write trader/orders.json

# 6. Risk Manager: Pre-trade safety gate (max loss, trade counts, market hours, T2T, lot sizes)
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

### Indian Market Capability Matrix

| Asset Class / Instrument | Support Level | Execution Mode | Data Provider Tier | Capabilities & Limitations |
| :--- | :--- | :--- | :--- | :--- |
| **Cash Equities (NSE / BSE)** | Fully Supported | Paper Trading | `DELAYED` (Yahoo 15m) / `HISTORICAL` / `OFFLINE` | Complete support for all listed cash equities. Sectoral and broad indices (NIFTY 50 to 500, SENSEX, BSE 100). SEBI Trade-to-Trade (T2T) series BE/BZ/T intraday blocks enforced. Tick size Rs 0.05. |
| **Corporate News & Disclosures** | Fully Supported | Monitoring | `DELAYED` / `UNOFFICIAL` / `OFFLINE` | Public RSS syndication via Google News India corporate queries and Yahoo Finance RSS. Symbol normalization (RELIANCE, TCS.NS, BSE:INFY). Publisher attribution, source URLs, IST timestamps (+5:30), cross-feed deduplication, and automated price tracker filtering. |
| **Indices (NIFTY, BANKNIFTY, SENSEX)** | Fully Supported | Paper Trading | `DELAYED` (Yahoo 15m) / `HISTORICAL` | Spot price tracking, ORB calculation, setup scanning, and underlying spot reference for derivative contracts. |
| **Index Options (Long CE / PE)** | Fully Supported | Paper Trading | `SIMULATED` (Analytical BS) / `OFFLINE` | Contract naming (e.g. `NIFTY26OCT24000CE`), expiry calendar shifting for holidays, ATM/ITM/OTM strike selection, lot sizing (25 shares), analytical Black-Scholes Greeks (Delta, Gamma, Theta, Vega, Rho), IV solving, 14:30 IST expiry day cutoff. |
| **Stock Options (Long CE / PE)** | Fully Supported | Paper Trading | `SIMULATED` (Analytical BS) / `OFFLINE` | F&O underlying lookup, official lot sizes from instrument cache, strike ladder generation, defined-risk position sizing. |
| **Equity & Index Futures** | Fully Supported | Paper Trading | `SIMULATED` / `OFFLINE` | Margin requirements (~20% margin check), lot sizing, monthly Tuesday expiry calendar (NSE) / Friday (BSE), stop and target risk validation. |
| **Statutory Transaction Costs** | Fully Supported | Calculations | Analytical | Automated breakdown of STT (intraday vs delivery vs derivatives vs exercised), NSE exchange turnover fees (March 2026 revision), SEBI charges, Stamp Duty, and 18% GST. |
| **Naked Short Options** | Blocked by Design | N/A | N/A | Strictly blocked by risk manager. Undefined risk strategies are prohibited by default safety gates. |
| **Live Broker Execution** | Blocked by Design | N/A | N/A | Paper trading is the default and only execution mode. No live broker connector is connected to prevent accidental capital loss. |

### Indian Market Statutory Transaction Costs and Charges

Every trade sized by `/trader-budget` calculates statutory regulatory charges alongside broker commission models using effective-date-based schedules (`REGULATORY_SCHEDULES`):

| Component | Status | Intraday Equity | Delivery Equity | Equity / Index Futures | Options (Long CE/PE) | Exercised Options |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **STT (Securities Transaction Tax)** | Statutory Verified (Eff. 2026-04-01) | 0.025% on sell turnover | 0.1% on buy & sell turnover | 0.05% on sell turnover | 0.15% on sell premium turnover | 0.15% on intrinsic value |
| **Stamp Duty** | Statutory Verified | 0.003% on buy turnover | 0.015% on buy turnover | 0.002% on buy turnover | 0.003% on buy premium turnover | 0.003% on buy premium turnover |
| **NSE Exchange Charges** | Statutory Verified (Eff. 2026-03-01) | Rs 307 / cr (0.00307%) per side | Rs 307 / cr (0.00307%) per side | Rs 183 / cr (0.00183%) per side | Rs 3,553 / cr (0.03553%) per side of premium | Rs 3,553 / cr per side of premium |
| **SEBI Turnover Fees** | Statutory Verified | Rs 10 per crore (0.0001%) | Rs 10 per crore (0.0001%) | Rs 10 per crore (0.0001%) | Rs 10 per crore (0.0001%) | Rs 10 per crore (0.0001%) |
| **GST** | Statutory Verified | 18% on (Statutory Exch + SEBI) | 18% on (Statutory Exch + SEBI) | 18% on (Statutory Exch + SEBI) | 18% on (Statutory Exch + SEBI) | 18% on (Statutory Exch + SEBI) |
| **Brokerage (Estimate)** | Estimated Tariff | min(Rs 20, 0.03%) per order | Rs 40 flat per buy/sell order (Rs 80 round-trip) | min(Rs 20, 0.03%) per order | Rs 20 flat per order (Rs 40 round-trip) | Rs 20 flat per order |

> [!NOTE]
> **Brokerage Separation & Trade Profitability Impact**:
> Estimated discount broker commissions are kept strictly segregated in `brokerage_estimate` and labeled as non-official estimates, distinct from official government and exchange levies in `statutory`.
> The output includes `profitability_impact` containing gross P&L, total transaction drag, net P&L, and breakeven exit price.
>
> **Derivative Expiries (November 2026 Contract Specifications)**:
> - **NSE**: All index (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY) and stock derivatives expire on **Tuesday**. When Tuesday is an exchange holiday, contracts expire on the preceding trading day.
> - **BSE**: Contracts follow independent rules (SENSEX expires on **Friday**; BANKEX expires on **Monday**).

### Indian Corporate News Coverage (trader-news)

`/trader-news` monitors company-specific news, corporate announcements, and regulatory filings across Indian equities (NSE & BSE) and US stocks:

- **Symbol Normalization**: Accepts canonical symbols (`RELIANCE`, `TCS`, `INFY`, `HDFCBANK`), exchange suffixes (`.NS`, `.BO`), and prefixes (`NSE:`, `BSE:`), normalizing them automatically.
- **Company Relevance & Classification**: News is categorized into five structured buckets:
  - `corporate`: earnings, exchange filings, management changes, acquisitions, contracts, and material announcements.
  - `business`: products, partnerships, operational expansion, and commercial developments.
  - `analyst`: broker ratings, price targets, recommendations, and analyst estimates.
  - `market`: share-price commentary, market flows, and technical analysis.
  - `low_relevance`: incidental mentions, sporting/marathon sponsorships, CSR awards, or venue references.
- **Relevance Ranking & Filtering**: Material corporate developments and business expansions are prioritized by default. Low-relevance and incidental stories are excluded from standard output and can be inspected using `--research`. Sorting can be controlled via `--sort relevance` (default for Indian tickers) or `--sort time`.
- **Subsidiary & Group Detection**: Explicitly identifies subsidiaries and distinct business units (e.g. Reliance Consumer Products, Reliance Retail, Reliance Jio, Reliance New Energy, Reliance ADAG group, Tata Group Parent, Infosys BPM, EdgeVerve, HDFC Life/AMC/ERGO/Securities) rather than conflating them with the parent listed entity.
- **Sources & Attribution**: Fetches public Google News Indian corporate query RSS feeds alongside Yahoo Finance RSS feeds. Every headline clearly identifies the original reporting publisher (e.g. Livemint, The Economic Times, Moneycontrol, Reuters, TradingView) and preserves direct article URLs separate from publisher homepages.
- **Coverage Transparency**: Summarizes total relevant stories, category breakdowns, subsidiary touches, and feed status (`OK`, `EMPTY`, or `UNAVAILABLE`). An empty feed or zero stories is transparently reported and never fabricated.
- **IST Timestamps**: Formats all publication dates and intraday price moves in Indian Standard Time (`IST`, UTC+5:30), while preserving UTC datetimes internally for precise time-window filtering.
- **Noise Filtering (Price Trackers vs. Real News)**: Automated stock quote tracker pages (e.g. "Share Price Today - Live NSE/BSE") are distinguished from genuine corporate disclosures, earnings releases, and material developments.
- **Cross-Feed Deduplication**: Consolidates stories syndicated across multiple news outlets by matching normalized headlines and dates, merging ticker touchlists.
- **Multi-Ticker Support**: Supports multiple Indian and mixed US/India tickers in a single invocation (`python3 news.py --tickers RELIANCE TCS INFY`).
- **Data Limitations & Disclosures**: Public RSS feeds are unofficial and may experience syndication latency compared to real-time exchange circulars. Empty feed responses do not confirm the absence of material developments; material actions should be cross-verified directly on official exchange portals (NSE at `nseindia.com` or BSE at `bseindia.com`).

### Provider-Agnostic Indian Market-Data Architecture (trader-alerts/data_in.py)

All Indian trading workflows (`trader-scanner`, `trader-entry`, `trader-exit`, `trader-timemachine`, `trader-alerts`, and `trader-news` intraday moves) consume a provider-agnostic interface (`MarketDataProvider`) in [`trader-alerts/data_in.py`](trader-alerts/data_in.py) rather than binding directly to Yahoo schemas.

- **Standard Provider Interface**:
  - `get_quote(symbol, exchange="NSE")`: latest quote snapshot with canonical `symbol`, `exchange`, `instrument_id`, `ltp`/`price`, `prev_close`, `ist_time`, `freshness`, `data_status`, `is_live`, and `is_simulated`.
  - `get_ltp(symbol, exchange="NSE")` & `get_ohlc(symbol, exchange="NSE", interval="1d")`: standardized LTP and OHLC snapshots.
  - `get_historical_data(symbol, start=None, end=None, range_str="1y", interval="1d", exchange="NSE")` & `get_intraday_data(symbol, interval="5m", range_str="60d", exchange="NSE")`: IST-aligned OHLCV candle series converted cleanly for scanners, entry/exit calculators, and Time Machine replays.
  - `get_instruments(exchange="NSE", symbol=None)`: standardized equity/derivative instrument metadata (where supported).
  - `get_option_chain(symbol, expiry=None, exchange="NSE")` & `get_market_status(exchange="NSE", as_of=None)`: derivative chains (where supported) and IST exchange session status.
- **Selecting a Provider (`INDIA_MARKET_DATA_PROVIDER`)**:
  - Set `export INDIA_MARKET_DATA_PROVIDER=yahoo` (default when unset) or configure `"india_market_data_provider": "yahoo"` in `trader/config.json` (legacy `BHARAT_DATA_PROVIDER` and `"data_provider"` remain supported).
  - Switching to a future registered adapter (for example Groww once added) requires only changing `INDIA_MARKET_DATA_PROVIDER=groww` or updating config, without rewriting scanner, entry, exit, risk, or paper-trading logic.
  - Providers with `requires_auth = True` validate `required_credentials` from `config["provider_credentials"][provider]` or `<PROVIDER>_<CREDENTIAL>` env vars and fail fast with `AuthenticationConfigError`.

#### Provider Capability Matrix & Current Limitations

| Capability | `YahooMarketDataProvider` (`yahoo`, default) | `UpstoxMarketDataProvider` (`upstox`, optional) | `OfflineMarketDataProvider` (`offline`) | `SimulatedMarketDataProvider` (`simulated`) |
| :--- | :--- | :--- | :--- | :--- |
| `historical_candles` | Supported (`HISTORICAL`) | Supported (V3 candles, auto-chunked) | Supported (`FIXTURE`) | Delegated |
| `intraday_candles` | Supported (`5m`/`1m`, max ~60d/7d) | Supported (V3 historical + intraday) | Supported (`FIXTURE`) | Delegated |
| `live_quotes` | `NOT_SUPPORTED` (`DELAYED` 15m only) | Supported (`REAL_TIME` / `CURRENT` when open) | `NOT_SUPPORTED` (`FIXTURE`) | `NOT_SUPPORTED` |
| `streaming_data` | `NOT_SUPPORTED` | Supported (V3 WebSocket + stdlib Protobuf) | `NOT_SUPPORTED` | `NOT_SUPPORTED` |
| `option_chains` / `open_interest` | `NOT_SUPPORTED` (`NotSupportedError`) | Supported (`/v2/option/chain` + OI/Greeks) | Supported if saved fixture | `SIMULATED` (Black-Scholes; OI synthetic) |
| `instrument_masters` | `NOT_SUPPORTED` remote; local CSV cache in `instruments.py` | Supported (NSE/BSE gzip JSON masters) | Local fixture / cache | Delegated / `False` |
| `market_status` | Supported (IST calendar `calendar_in`) | Supported (`/v2/market/status` + `calendar_in`) | Supported (`calendar_in`) | Supported (`calendar_in`) |

- **Default Unauthenticated Behavior (Yahoo Finance)**: Anyone without an Upstox token runs on free delayed Yahoo Finance data (`data_status: DELAYED`, `is_live: False`). Failed or timed-out requests raise typed errors (`RateLimitError`, `SymbolNotFoundError`, `ProviderConnectionError`, `MissingDataError`) and never fabricate prices or treat outages as empty setup scans.
- **Optional Read-Only Upstox Feed**: Requires `INDIA_MARKET_DATA_PROVIDER=upstox` and `UPSTOX_ANALYTICS_TOKEN`. Account tier defaults strictly to **`standard`** (`UPSTOX_ACCOUNT_TIER=standard`: 2 concurrent WebSocket V3 connections; LTPC 5,000 individual / 2,000 combined; Option Greeks 3,000 individual / 2,000 combined; Full 2,000 individual / 1,500 combined; `full_d30` disabled). Opt into **`plus`** (`UPSTOX_ACCOUNT_TIER=plus` or `config["provider_credentials"]["upstox"]["account_tier"]`) only if your account holds Upstox Plus to enable 5 concurrent connections and `full_d30` (`50` keys per connection / `1,500` combined). Also enforces V3 historical lookback caps (`2022-01-01` floor for minutes/hours, calendar-safe month/quarter chunking), exponential backoff reconnects with fresh single-use authorization URLs, HTTP redirect following on WebSocket upgrade, and stale-tick detection (`stale_after_seconds`).
- **Simulated Options Safety**: Analytical Black-Scholes option pricing and chains remain isolated inside `SimulatedMarketDataProvider` and are always tagged `data_status: SIMULATED`, `is_simulated: True`, `is_live: False`.

#### Adding a Future Adapter (e.g. Groww, Zerodha, Dhan)

1. Subclass `data_in.MarketDataProvider`, set `provider_name`, `requires_auth = True`, `required_credentials`, and `CAPABILITIES`.
2. Implement `get_quote`, `get_daily_bars`, `get_intraday_bars`, and optionally `get_instruments` / `get_option_chain`, returning `_standardize_payload(...)` with IST timestamps and explicit `data_status` / `is_live` flags.
3. Register via `data_in.register_provider("groww", GrowwMarketDataProvider)` and activate with `INDIA_MARKET_DATA_PROVIDER=groww`.

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
