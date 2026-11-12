---
name: trader-news
description: News Monitor. Tracks breaking catalysts for US stocks (Yahoo Finance headlines, SEC EDGAR 8-K filings with item tags, and big daily moves) and Indian stocks (Google News India, Yahoo India RSS, and NSE/BSE price moves), categorizing corporate, business, analyst, market, and low-relevance items with deduplicated state and IST/ET timestamps.
---

# trader-news (The News Monitor)

Runs `news.py` to check what is moving stocks on your watchlist, active plan, and portfolio before entering or managing a trade. Pure Python standard library, zero third-party dependencies.

## Quick start

```
# US stocks (Yahoo Finance headlines + SEC EDGAR 8-K items + big price moves)
python3 <SKILL_DIR>/trader-news/news.py --tickers AAPL TSLA

# Indian stocks (Google News India + Yahoo India RSS + NSE/BSE price moves in IST)
python3 <SKILL_DIR>/trader-news/news.py --tickers RELIANCE TCS --exchange NSE

# Check everything currently in your watchlist, active plan, and portfolio
python3 <SKILL_DIR>/trader-news/news.py --watchlist trader/watchlist.json --plan trader/plan.json --portfolio trader/portfolio.json
```

Offline / reproducible replay mode:
```
python3 <SKILL_DIR>/trader-news/news.py --tickers AAPL --offline <FIXTURE_DIR> --now "2026-10-19T14:30:00Z" --dry-run
```

## Sources and flags

- **US tickers**:
  - **Yahoo Finance RSS headlines** (`feeds.finance.yahoo.com`).
  - **SEC EDGAR submissions API** (`data.sec.gov`): recent `8-K` (or `--forms` list) filings decoded into human item labels (`earnings`, `deal`, `CEO/board change`, `restatement`, `bankruptcy`, etc.). Online SEC queries require `SEC_USER_AGENT` env var, config `sec_user_agent`, or `--user-agent "Name you@example.com"`.
  - **Daily price moves**: Yahoo chart close vs prior close, flagging moves exceeding `--move-pct` (default `3.0%`).
- **Indian tickers** (`--exchange NSE|BSE`, `.NS`/`.BO`, or Indian config):
  - **Google News India RSS** (`news.google.com`, `hl=en-IN&gl=IN&ceid=IN:en`) and **Yahoo India RSS** (`<TICKER>.NS`), merged and deduplicated cleanly with timestamps formatted in **IST** (`Asia/Kolkata`).
  - Automatic relevance categorization into `corporate`, `business`, `analyst`, `market`, and `low_relevance`, subsidiary name matching, and a per-ticker Coverage Summary.
- Key flags:
  - `--tickers [TICKERS ...]` (defaults to reading `trader/watchlist.json`, `trader/plan.json`, and `trader/portfolio.json`)
  - `--portfolio PATH`, `--watchlist PATH`, `--plan PATH`, `--config PATH`
  - `--state PATH` (tracks seen items so repeat checks only show new stories; `--all` includes previously seen items; `--dry-run` avoids saving state)
  - `--hours N` (lookback window in hours, default `48`), `--forms 8-K`, `--no-sec`, `--no-moves`, `--move-pct PCT` (default `3.0`)
  - `--exchange NSE|BSE` (default `NSE` for Indian symbols)
  - `--include-quotes` (include automated price-tracker articles), `--research` (include `low_relevance` items), `--sort relevance|time`
  - `--offline DIR`, `--now ISO_TIME`, `--json`

## Presentation workflow

1. Run `news.py` on the watchlist hits from `trader-scanner`, active plan symbols, or user-requested tickers.
2. Lead with high-signal material catalysts: SEC `8-K` filings (`Item 2.02` earnings, `Item 1.01` material agreements, `Item 4.02` non-reliance, `Item 5.02` executive changes) and Indian `corporate` / `business` / `analyst` stories, plus any big-move flag.
3. Explicitly warn before an entry when fresh earnings releases, trading halts, restatements, M&A news, or dilution/offering announcements appear.
4. Preserve market timezones (`ET` for US, `IST` for India) and clearly note when `--offline` fixtures or `--now` replay mode is used.

## What it never does

It never generates trade signals, never overrides a risk limit, and never places a broker order.

## Safety

- Education and news aggregation only, not financial advice or a recommendation to buy or sell.
- Never fabricate headlines, filing dates, or SEC item numbers; report only what `news.py` outputs.
- Paper trading by default. Any trade idea prompted by news must still go through `trader-entry`, `trader-exit`, `trader-budget`, `trader-risk` PASS, and an explicit typed `yes` from the user in `trader-broker`.
- Never paste or ask for private API tokens in chat (`SEC_USER_AGENT` is a public fair-access contact header only).
