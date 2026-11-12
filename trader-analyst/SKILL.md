---
name: trader-analyst
description: The Earnings Analyst. Reads US quarterly/annual financials from SEC EDGAR XBRL companyfacts and 8-K/10-Q/10-K filings, or Indian NSE/BSE quarterly and annual results via Yahoo Finance TimeSeries/Fundamentals formatted in INR Crores (Rs Cr) across Indian FY quarters, computing YoY growth, margins, EPS, balance sheet, and cash flow metrics.
---

# trader-analyst (The Earnings Analyst)

Runs `facts.py` to summarize reported quarterly and annual fundamentals before holding across earnings or evaluating a stock's multi-quarter financial trend. Pure Python standard library, zero third-party dependencies.

## Quick start

```
# US tickers (SEC EDGAR XBRL companyfacts + 8-K/10-Q/10-K filing links)
python3 <SKILL_DIR>/trader-analyst/facts.py AAPL KO --quarters 6 --annual

# US lookup by CIK or offline SEC fixture folder
python3 <SKILL_DIR>/trader-analyst/facts.py --cik 0000320193 --offline <FIXTURE_DIR>

# Indian tickers (auto-detected via .NS/.BO, NSE:/BSE:, --exchange, or BHARAT_MARKET=IN; formats in Rs Cr)
python3 <SKILL_DIR>/trader-analyst/facts.py RELIANCE TCS INFY --exchange NSE --quarters 6 --annual
python3 <SKILL_DIR>/trader-analyst/facts.py RELIANCE --offline <FIXTURE_DIR> --json
```

## CLI flags

- `tickers`: one or more tickers (`AAPL KO` or `RELIANCE TCS INFY`).
- `--cik CIK`: resolve a single US SEC filer by CIK number instead of ticker lookup.
- `--exchange NSE|BSE`: exchange qualifier for Indian tickers (default `NSE`, maps to `.NS` / `.BO`).
- `--quarters N`: number of historical quarters to display with YoY comparisons (default `6`).
- `--annual`: append an annual financials summary table.
- `--no-filings`: omit SEC `8-K`, `10-Q`, and `10-K` filing links.
- `--metrics FILE`: merge standardized per-ticker summary metrics into a target JSON file (see `metrics.example.json`).
- `--user-agent "Name you@example.com"`: SEC EDGAR User-Agent header (else reads `SEC_USER_AGENT` env var or config).
- `--config PATH`: optional trader config JSON.
- `--offline DIR`: read local fixtures (`companyfacts`, `submissions`, `indian-<SYM>.json`) with zero network calls.
- `--json`: output structured JSON rather than formatted terminal tables.

## How US vs Indian earnings differ

- **US (`SEC EDGAR`)**: Reads structured XBRL `companyfacts` plus submission metadata. Computes trailing twelve months (TTM), same-quarter year-over-year revenue/EPS growth, gross/operating/net margins, balance-sheet cash/debt metrics, operating cash flow, and exact SEC filing dates/URLs.
- **India (`NSE` / `BSE`)**: Auto-detected from `.NS`/`.BO` suffixes, `NSE:`/`BSE:` prefixes, `--exchange`, `BHARAT_MARKET=IN`, built-in Indian flagship tickers, or offline `indian-<SYM>.json` fixtures. Queries Yahoo Finance fundamentals/timeseries, labels Indian fiscal-year quarters (`Q1-Q4 FYxx`, April-March fiscal calendar), and displays monetary figures in **INR Crores (`Rs Cr`)** with `[SOURCE | DATA | STATUS]` provenance.
- **Earnings call transcripts**: If the user pastes transcript text directly in chat, extract quantitative guidance changes, management commentary, and stated risks strictly from the pasted text.

## Output summary fields

Reports tabular quarterly income/margin metrics plus YoY % growth, diluted EPS, trailing 12 months (`TTM`), balance-sheet cash/debt snapshot, operating/free cash flow, and latest filing links when available.

## Presentation checklist

1. State the exact data source (`SEC EDGAR` or `Yahoo Finance India`), currency units (`$M`/`$B` or `Rs Cr`), and period end / filing dates.
2. Highlight revenue YoY growth, gross / operating / net margin expansion or compression, diluted EPS trend, and operating cash flow vs net income.
3. Call out balance-sheet leverage, cash runway, or upcoming earnings/filings risk if holding intraday or swinging across an expected report date.
4. For JSON automation pipelines, inspect `provenance` (`provider`, `data_status`, `is_live`, `last_data_timestamp`) returned by `--json` or `--metrics` (see `metrics.example.json` for schema fields).

## What it never does

It never forecasts future EPS or target prices, never rates a stock buy/sell, and never places a broker order.

## Safety

- Education and financial statement summary only, not financial advice or an investment recommendation.
- Always show exact report dates, currency units (`$M`/`$B` or `Rs Cr`), and `[SOURCE | DATA | STATUS]` provenance; report missing items as missing rather than estimating.
- Paper trading only for any downstream workflow: an earnings view never bypasses `trader-exit`, `trader-budget`, `trader-risk`, or explicit user typed `yes` confirmation in `trader-broker`.
- Never ask for private API credentials in chat (`SEC_USER_AGENT` is a public identification header only).
