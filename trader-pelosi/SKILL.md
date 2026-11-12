---
name: trader-pelosi
description: The Pelosi Tracker. Watches US Congressional Periodic Transaction Reports (PTRs) under the STOCK Act using the House Clerk public disclosure ZIP/XML index, alerts once per new filing, parses trade tables (owner, asset, ticker, transaction type, trade date, amount range, disclosure lag, options strike/expiry), and cross-references your tickers or portfolio.
---

# trader-pelosi (The Congressional PTR Tracker)

Runs `ptr.py` to track public US House Periodic Transaction Reports (PTRs) filed under the STOCK Act, parse trade disclosure tables cleanly, and highlight any overlap with symbols in your watchlist or `trader/portfolio.json`.

## Quick start

```
# List recent House PTR filings from the Clerk ZIP/XML index
python3 <SKILL_DIR>/trader-pelosi/ptr.py index --member Pelosi --since 2026-01-01

# Check for newly posted filings since last check (pass --read to download and parse PTR PDFs)
python3 <SKILL_DIR>/trader-pelosi/ptr.py new --member Pelosi --tickers AAPL NVDA --portfolio trader/portfolio.json
python3 <SKILL_DIR>/trader-pelosi/ptr.py new --offline <FIXTURE_ZIP_OR_XML> --today 2026-10-19 --dry-run

# Fetch and parse one specific House PTR PDF by document ID
python3 <SKILL_DIR>/trader-pelosi/ptr.py fetch 20026590 --year 2025 --filed 2025-01-17

# Parse an already-extracted plain-text PTR file (pure Python stdlib, no external binaries)
python3 <SKILL_DIR>/trader-pelosi/ptr.py parse <PTR_TEXT_FILE> --filed 2025-01-17
```

## Subcommands and flags

- `index`: `--year YYYY`, `--member NAME` (repeatable), `--type P` (default `P` = PTR, `''` for all), `--since YYYY-MM-DD`, `--offline FILE` (`<YEAR>FD.zip` or `.xml`), `--json`, `--limit N`.
- `new`: all `index` flags plus `--state PATH`, `--today YYYY-MM-DD` (reproducible dates), `--dry-run`, `--read` (downloads PDFs and calls `pdftotext`), `--pdf-dir DIR`, `--tickers [TICKERS ...]`, `--portfolio PATH`.
- `fetch`: `doc_id`, `--year YYYY`, `--filed YYYY-MM-DD`, `--pdf-dir DIR`, `--tickers [TICKERS ...]`, `--portfolio PATH`, `--json`.
- `parse`: `text_file`, `--filed YYYY-MM-DD`, `--tickers [TICKERS ...]`, `--portfolio PATH`, `--json`.

## Parsed output and limitations

- Extracts filer name, owner tag (`SP` spouse, `JT` joint, `DC` dependent child, or self), asset description, ticker symbol, transaction type (`Purchase`, `Sale`, `Exchange`), options terms (call/put, strike price, expiration date, underlying ticker), trade date, filing date, disclosure lag in days, and statutory dollar-amount brackets.
- Automatically flags overlap against `--tickers` and holdings in `trader/portfolio.json` (plus `HOLDINGS:` annotations).
- **US Senate disclosures**: Senate EFD (`efdsearch.senate.gov`) gates access behind an interactive acknowledgement / anti-bot prompt, so `ptr.py` never bypasses or scrapes it automatically; retrieve Senate filings manually if needed.
- **`pdftotext` dependency note**: `index`, `new` (without `--read`), and `parse` use pure Python stdlib and work anywhere. Downloading and converting raw PTR PDFs via `fetch` or `new --read` invokes external `pdftotext` (`poppler-utils`); if `pdftotext` is absent in the runtime container, state that limitation and use `parse` on extracted text or `--offline` fixtures.

## Output columns

Text output summarizes each filing row with:
- Filer & doc ID / filed date
- Owner, ticker, asset description, and options details
- Action, trade date, disclosure lag (days), amount bracket, and portfolio/watchlist flags

## Workflow tips

1. Run `index` first to verify whether new PTR document IDs appeared since the prior run, or pass `--offline` with a saved House Clerk `<YEAR>FD.zip` or `.xml` snapshot for offline demos and regression checks.
2. Use `new --read` (or `fetch` with a saved `doc_id`) to extract the table rows, or pass `--state trader/state/pelosi.json` to suppress duplicates across recurring runs.
3. When a filing touches a ticker in the user's watchlist or portfolio, surface the exact disclosure lag (`days late` vs trade date), transaction type (`Purchase`, `Sale`, `Exchange`), owner tag, options strike/expiry if any, and statutory dollar bracket so the user does not confuse a month-old report with fresh market flow.
4. Use `--json` when forwarding parsed rows to downstream research notes or cross-referencing against `trader-news` headlines.

## What it never does

It never recommends copying a politician's disclosure, never turns a filing into a trade signal, and never places a broker order.

## Safety

- Education and public disclosure monitoring only, not financial advice or a trade recommendation. STOCK Act disclosures can lag trades by up to 45 days and show broad dollar ranges rather than exact share quantities or rationales.
- Never place or prepare an order from a disclosure alone. Any trade plan must separately pass `trader-exit`, `trader-budget`, `trader-risk`, and an explicit user typed `yes` in `trader-broker` (paper trading by default).
- Never paste or request private credentials or API tokens in chat.
