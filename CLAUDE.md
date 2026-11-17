# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Day Trader is a pack of 12 Claude Code skills (`trader-*` folders) for day trading US (NYSE/NASDAQ) and Indian (NSE/BSE, "BharatTrader AI") equities/F&O. Each skill is a standalone stdlib-only Python CLI plus a `SKILL.md` that documents it for Claude. Skills chain together through JSON files written into a git-ignored `trader/` working directory:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close                                      (while open)
news / analyst / pelosi                                                      (standalone research, any time)
```

No trade is ever placed without the user typing an explicit "yes" to `trader-broker`, and `trader-risk` must `PASS` that exact order first. Indian-market trading is paper-only, long-only, at the schema level -- this is non-negotiable, not a default.

`dist/claude-chat/` is a built, standalone distribution bundle (zip + exploded copy) for Claude.ai (non-Code) chat. It's generated output -- don't hand-edit it; change the source `trader-*`/`guides` files and regenerate if asked.

## Commands

Run the full test suite (hermetic, offline, no network or API keys required):
```bash
python3 -m pytest tests/ -v
# or
python3 -m unittest discover -s tests -v
```

Run a single test file / test:
```bash
python3 -m pytest tests/test_risk.py -v
python3 -m pytest tests/test_risk.py::TestClassName::test_method_name -v
```

Config CLI (central config loader, shared by every skill):
```bash
python3 config/loader.py show --market IN
python3 config/loader.py validate --config trader/config.json
python3 config/loader.py init --market IN --out trader/config.json
```

Each skill is invoked directly as a script, e.g.:
```bash
python3 trader-scanner/scan.py orb --market IN --at 10:15
python3 trader-risk/check.py --orders trader/orders.json --config trader/config.json --market IN
```
See `README.md` for the full command reference (both US and IN examples) and `trader-*/SKILL.md` for each skill's exact flags and contract.

No build step, no package manager, no dependencies to install -- Python 3.8+ standard library only (verified at runtime on 3.13).

## Architecture

**Hyphenated package names -- the one thing that trips up normal imports.** Skill folders (`trader-risk`, `trader-budget`, etc.) are valid directories but not valid Python package names, so nothing imports them as `import trader_risk`. Instead:
- Scripts add sibling skill folders to `sys.path` at the top of the file (see the `BASE_DIR` / `sys.path.insert` block at the top of `trader-options/options.py` or any other entrypoint) and then do a plain `import data_in`, `import entry`, etc.
- Tests do the same via a shared helper, `tests/_load.py`: `load("trader-risk", "check")` dynamically imports `trader-risk/check.py` as a module. Every test file starts with calls like `check = load("trader-risk", "check")`.
- `pyrightconfig.json` lists every skill folder in `extraPaths` so the type checker resolves these same cross-folder imports.

**Deterministic engine vs. AI orchestration.** The Python code is the sole source of truth for math, indicators, position sizing, statutory costs, and risk gating -- it is deterministic and unit-tested. Claude (the skill layer / `SKILL.md` prose) handles dialogue, research synthesis, and explanation, and must never invent a number the Python didn't produce or override a `BLOCK`/fail-closed result. Keep this boundary when adding features: new financial logic goes in the Python tool, not in a `SKILL.md`.

**Config resolution precedence** (`config/loader.py` → `config/schema.py`): runtime call overrides → `--config` file or `trader/config.json` → market template (`config/config.in.json` / `config/config.us.json`) → built-in `IN_DEFAULTS`/`US_DEFAULTS` in `config/schema.py`. Every loaded config gets a `_provenance` block (source, market, SHA-256 config hash, timestamp) -- changing any risk parameter changes this hash and invalidates prior order fingerprints. Hard-locked rules enforced in `schema.py` validation (not just convention): `paper: true` for `market: "IN"`, `min_reward_risk` floored at 2.0.

**Provider-agnostic market data** (`trader-alerts/data_in.py`): all skills consume a `MarketDataProvider` interface (`get_quote`, `get_historical_data`, `get_intraday_data`, `get_option_chain`, `get_market_status`, ...) rather than binding to one vendor's schema. Providers: `yahoo` (default, no auth, `DELAYED` 15m), `upstox` (optional, read-only, needs `UPSTOX_ANALYTICS_TOKEN`), `offline`/`simulated` (fixtures/tests). Every payload is tagged with a `data_status`/`tier` (`CURRENT`, `DELAYED`, `HISTORICAL`, `FIXTURE`, `SIMULATED`, `UNAVAILABLE`) -- never fabricate or silently upgrade a tier. Add a new provider by subclassing `MarketDataProvider` and calling `data_in.register_provider(name, cls)`; existing skills pick it up through `INDIA_MARKET_DATA_PROVIDER` without changes.

**Order safety chain**: `trader-budget/size.py` sizes and writes `trader/orders.json` → `trader-risk/check.py` validates against every limit in config and computes a truncated SHA-256 `order_fingerprint` (not HMAC) over normalized order params + policy snapshot, writing `trader/risk.json` with `PASS`/`BLOCK` → `trader-broker/ticket.py` re-verifies the fingerprint matches, requires an explicit human-reply string (e.g. `"yes buy 26 RELIANCE"`), then records the fill in `trader/portfolio.json`. Any change to stop/target/qty/policy changes the fingerprint and invalidates a prior approval -- never try to "patch around" a fingerprint mismatch.

**Signal provenance threading**: fields like `data_tier`, `is_historical_analysis`, `is_live_signal`, `provider`, `signal_freshness` are set once in `trader-entry` and threaded unchanged through `trader-exit` → `trader-budget` → `trader-risk` → `trader-broker`, which prints explicit warnings (e.g. `WARNING: HISTORICAL SIGNAL`) rather than silently treating stale/offline data as live. Preserve this threading when touching any of these modules -- don't recompute provenance downstream.

**`--now` clock override**: most CLIs accept `--now "YYYY-MM-DD HH:MM"` (market-local time) for deterministic backtests/tests. Any run using it is marked `clock_override: true, is_live_signal: false` even on a live-capable provider -- this is a safety property, not a test-only convenience, so don't bypass it when wiring new code paths.

**Two independent options code paths -- don't conflate them.** (1) `trader-entry/entry.py`'s `options_orb` setup is narrow (BUY CE only) and feeds the main `scanner -> entry -> exit -> budget -> risk -> broker` execution pipeline, but execution sizing is disabled there regardless (`trader-budget/size.py: SizeError`), so it never reaches a placeable order. (2) `trader-options/` (`options.py`'s `OptionsIntelligenceSpecialist`, `orchestrator.py`, `replay.py`) is a separate, larger, standalone decision-support engine -- it analytically evaluates BUY_CE, BUY_PE, SELL_CE, SELL_PE, and two debit spreads, with its own Greeks/premium modeling, statutory charges, and 4-tier sizing. It **never** writes `trader/orders.json` and never calls `trader-budget/size.py`'s sizing path or `trader-risk/check.py`'s order validator (it reuses `check.py` only as a library for its own portfolio-risk re-check) -- its output is terminal, for a human to read. Naked SELL_CE/SELL_PE there are fully evaluated and disclosed but unconditionally blocked by path (2)'s own `NAKED_OPTION_PROHIBITED` check (independent of `trader-risk/check.py`'s rule, which path 2 never calls). `trader-options/SKILL.md` is the authoritative, kept-in-sync spec for path (2)'s actual per-strategy status -- prefer it over `guides/OPTIONS_RISK_SPEC.md`, which mostly documents path (1).

**Current hard operational boundaries** (see `guides/FEATURE_STATUS.md` §1 for the full authoritative list -- check it before assuming a capability exists):
- Long-only execution everywhere; short-side signals (`vwap_rejection`) are analytical only, blocked fail-closed downstream.
- Options execution (both paths): never happens. Path 1 (`options_orb`) is BUY-CE-analytical-only. Path 2 (`trader-options/`) is decision-support-only by design, regardless of strategy.
- Options config gates in `trader-options`: `buy_ce_enabled`/`buy_pe_enabled` (default `true`) and `sell_ce_enabled`/`sell_pe_enabled` (default `false` -- gate the Bear Call / Bull Put credit spreads, opt-in since their margin is a heuristic estimate) each suppress that strategy's generation; `max_option_lots` is a hard sizing cap; `max_correlated_exposure_pct` caps capital committed to the broad-index correlation group (all 5 major indices) per sweep. Iron Condor remains unimplemented (needs a 4-leg combined-margin model and a range-bound trigger condition neither exists today).
- `trader-timemachine` historical replay covers `orb` (`history_replay`) and the four intraday setups the scanner trades -- `breakout_intraday`, `vwap_reclaim`, `ema_alignment`, `rsi_momentum` -- via `setup_history_replay`, which re-runs `trader-entry`'s own `evaluate()` on past sessions with a horizon matched to the plan. Other setups get the Monte Carlo only.
- Upstox integration is strictly read-only (GET only) -- never add an order-routing call against it.

## Guides

`guides/` holds the detailed specs behind the README's summary -- consult before changing behavior they describe:
- `MASTER_PRODUCT_SPEC.md` -- architecture overview, market rules table, the 22-item standardized analysis report format.
- `FEATURE_STATUS.md` -- per-skill audit: exact I/O contracts, test coverage, and non-negotiable limitations (source of truth for "is X supported").
- `STRATEGY_SPEC.md` -- the intraday strategies, entry/stop/target rules, and the opportunity scoring formula.
- `CONFIGURATION_SPEC.md` -- full config schema field reference and precedence.
- `OPTIONS_RISK_SPEC.md` -- options state machine and per-strategy support status.
- `DATA_SOURCE_MATRIX.md` -- provider capability matrix and data tier/freshness policy.

## Conventions when editing

- New or changed financial logic (sizing, stops, targets, statutory costs, risk checks) needs matching test fixtures/assertions under `tests/` -- this repo's examples in the README are reproduced exactly by the test suite, so numbers must stay consistent.
- Preserve fail-closed behavior: raise a typed error (`ConfigError`, `SizeError`, `PlanError`, `TicketError`, `AuthenticationConfigError`, etc.) rather than guessing a default when required data/config is missing.
- Never log, print, or persist credentials (`UPSTOX_ANALYTICS_TOKEN`, etc.); `order_fingerprint.py` strips credentials and volatile `_provenance` fields before hashing -- keep that true for any new field you add to order payloads.
- Keep syntax compatible with Python 3.8 (no walrus-heavy/3.9+-only constructs) even though CI runs on 3.13.
