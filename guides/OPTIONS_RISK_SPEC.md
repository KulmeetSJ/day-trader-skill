# Day Trader: Options Risk and Intelligence Specification

## 0. Scope: Two Parallel Options Surfaces

This repository has two independent options code paths with different maturity. Sections 1-3 below
describe path (1), the narrow setup wired into the main order-execution pipeline. For path (2), see
`trader-options/SKILL.md` -- it is the authoritative spec for that engine's actual capabilities and
is kept in sync with its code; this document is not.

1. **`trader-entry/entry.py`'s `options_orb` setup**: BUY CE only, feeds `trader-exit` -> `trader-budget` ->
   `trader-risk` -> `trader-broker`. Execution sizing is disabled end-to-end in this path regardless
   of strategy (see item 2 below), so even BUY CE never reaches a placeable order here.
2. **`trader-options/` (`OptionsIntelligenceSpecialist`, `orchestrator.py`, `replay.py`)**: a separate,
   standalone decision-support engine. It analytically evaluates BUY_CE, BUY_PE, SELL_CE, SELL_PE, two
   defined-risk debit spreads (Bull Call Spread, Bear Put Spread), and two defined-risk credit spreads
   (Bear Call Credit Spread, Bull Put Credit Spread -- opt-in via `sell_ce_enabled`/`sell_pe_enabled`,
   default off) -- see `trader-options/SKILL.md` for exact status per strategy. It never writes to
   `trader/orders.json` and never calls
   `trader-budget/size.py`'s order-sizing path or `trader-risk/check.py`'s order validator -- it is
   terminal output for a human to read, not an input to the execution pipeline. Its own internal,
   unconditional `NAKED_OPTION_PROHIBITED` check (not `trader-risk/check.py`'s, which it never calls)
   is what prevents naked SELL_CE/SELL_PE from ever being marked actionable there.

## 1. Safety Principles & Non-Negotiables

1. **Indian Options are Strictly PAPER-ONLY**: Under no circumstances does an Indian option order route to live broker execution.
2. **Options Execution Status (path 1 only)**: All option execution sizing is currently disabled in `trader-budget/size.py` (`SizeError: OPTION RISK UNAVAILABLE`). BUY CE is supported as an analytical signal only (`options_orb` in `trader-entry/entry.py`). BUY PE is currently unsupported in `trader-entry/entry.py`. Defined-risk credit spreads are specification-only and currently unsupported for multi-leg order generation or execution sizing. **This item describes path 1 only** -- `trader-options/` (path 2) separately implements BUY_PE, SELL_CE, SELL_PE, and two debit spreads as analysis-only signals; see §0 above.
3. **Prohibition of Naked Short Options**: Undefined-risk option positions (naked short calls, naked short puts) are strictly prohibited and failed closed by `trader-risk/check.py`. Any single-leg short option (`side == "sell"`) is rejected unconditionally.
4. **Defined-Risk Spreads ONLY (Specification)**: When multi-leg spread execution is implemented in future releases, any short option position must be part of a defined-risk vertical credit spread with an explicit, verified long protective leg. Currently multi-leg spread execution is unsupported.
5. **Mandatory Net 2R After STT and Charges**: Indian options are subject to 0.125% STT on sell turnover and high statutory levies. The 2R requirement is calculated strictly on net premium after roundtrip levies.
6. **Contract Whole Lot Sizing**: All option orders must be sized in exact positive integer multiples of the exchange-mandated contract lot size (e.g., 25 for NIFTY, 15 for BANKNIFTY). Fractional option lots are rejected.
7. **Expiry Day Circuit Breaker**: New option entries are blocked after 14:30 IST on contract expiry day to protect against rapid pin risk and illiquid expiry spreads.

---

## 2. Recommendation State Machine

Every options analysis or proposal must resolve to one of seven explicit, unambiguous lifecycle states:

```
[ANALYSIS ONLY] --> [CANDIDATE] --> [TRADE ELIGIBLE] --> [RISK APPROVED] --> [PAPER APPROVED]
       |                 |                 |
       +-----------------+-----------------+--> [NO TRADE] / [UNSUPPORTED]
```

1. **`ANALYSIS ONLY`**:
   - Initial analytical stage. Option chain, Greeks, and PCR surveyed for spot sentiment.
   - No trade proposal or order generated.

2. **`CANDIDATE`**:
   - Technical setup confirmed on underlying (e.g. `options_orb` on NIFTY).
   - Candidate strikes identified (e.g. ATM CE). Contract quote and liquidity pending verification.

3. **`TRADE ELIGIBLE`**:
   - Verified active quote, adequate Open Interest, acceptable bid/ask spread, and valid contract lot size.
     This state machine was originally specified assuming OI $\ge 50,000$ / spread $\le 1.0\%$; `trader-options`'
     actual enforced defaults are OI $\ge 20,000$ / spread $\le 2.0\%$ (both config-overridable via
     `options_min_open_interest` / `options_max_spread_pct`), a deliberate interim tightening from a
     previous state with no OI floor and a 6% spread tolerance, pending calibration against real NIFTY
     chain data. Treat 50,000/1.0% as the long-term target, not the currently enforced bar.
   - Net Reward-to-Risk $\ge 2.0$ verified after Indian statutory charges.

4. **`RISK APPROVED`**:
   - Passed all pre-trade risk policy checks in `trader-risk/check.py`:
     - Budget and cash buffer compliance.
     - Single-trade loss limit and daily loss limit compliance.
     - Non-naked check (BUY leg or defined-risk vertical credit spread).
     - No PDT or account-level blocks.
     - Authoritative cryptographic order fingerprint generated.

5. **`PAPER APPROVED`**:
   - Explicit confirmation recorded by user in paper broker journal.
   - Paper execution simulated at realistic fill prices (inside bid-ask spread).

6. **`UNSUPPORTED`**:
   - Contract requested on an index or stock where options trading is not enabled in user configuration (`allow_options: false`), or instrument lacks options derivatives master metadata.

7. **`NO TRADE`**:
   - Stale data, illiquid strikes, excessive bid/ask spread, reward-to-risk $< 2.0$, or after cutoff time (e.g. after 14:30 IST on expiry day).

---

## 3. Option Strategies & Status

### A. Long Call (BUY CE) [Analytical Signal Only]
- **Status**: Analytical signal implemented via `options_orb` in `trader-entry/entry.py`. Order execution sizing disabled in `trader-budget/size.py`.
- **Outlook**: Bullish breakout or momentum continuation.
- **Strike Selection**: ATM (At-The-Money) or Delta $0.45 - 0.55$.
- **Risk**: Max loss strictly capped at premium paid $\times$ lot size.
- **Stop**: Technical invalidation on underlying mapped to premium stop (or fixed $20\%-25\%$ premium stop).
- **Target**: Minimum 2.0R Net reward.

### B. Long Put (BUY PE) [Unsupported in path 1; analytical signal in path 2]
- **Status**: Currently unsupported in `trader-entry/entry.py`'s `options_orb` (path 1 -- the execution-pipeline-connected setup; see §0). `trader-options/options.py` (path 2, analysis-only, never executes) implements BUY_PE as a full analytical signal, gated by config `buy_pe_enabled` (default `true`). See `trader-options/SKILL.md` §1.
- **Outlook**: Bearish breakdown or VWAP rejection.
- **Strike Selection**: ATM (At-The-Money) or Delta $-0.45$ to $-0.55$.
- **Risk**: Max loss strictly capped at premium paid $\times$ lot size.
- **Stop**: Invalidation above breakdown level or premium stop.

### C. Bear Call Credit Spread (SELL CE with Long Protective CE) [Unsupported for Execution in path 1; analytical signal in path 2]
- **Status**: Specification only / unsupported in `trader-entry`+`trader-risk`'s order-execution pipeline (path 1 -- naked short calls remain prohibited there regardless). `trader-options/options.py` (path 2, analysis-only, never executes) implements this as a full defined-risk analytical signal, gated by config `sell_ce_enabled` (default `false`, opt-in). Structurally validated before any signal is produced (hedge must be a higher-strike call); not subject to the naked-option prohibition since it has a genuine protective leg. Margin is a heuristic estimate, not authoritative. See `trader-options/SKILL.md` §1.
- **Outlook**: Bearish to Neutral.
- **Structure**:
  - Sell Strike $K_1$ (lower strike, higher premium).
  - Buy Strike $K_2$ (higher strike, lower premium, where $K_2 > K_1$).
- **Max Loss**: $(K_2 - K_1 - \text{Net Credit}) \times \text{Lot Size} + \text{Charges}$.
- **Safety Gate**: Long leg must be validated and executed simultaneously or prior to short leg.

### D. Bull Put Credit Spread (SELL PE with Long Protective PE) [Unsupported for Execution in path 1; analytical signal in path 2]
- **Status**: Specification only / unsupported in path 1 (naked short puts remain prohibited there). `trader-options/options.py` (path 2) implements this as a full defined-risk analytical signal, gated by config `sell_pe_enabled` (default `false`, opt-in), mirroring Bear Call Credit Spread above (hedge must be a lower-strike put). See `trader-options/SKILL.md` §1.
- **Outlook**: Bullish to Neutral.
- **Structure**:
  - Sell Strike $K_2$ (higher strike, higher premium).
  - Buy Strike $K_1$ (lower strike, lower premium, where $K_1 < K_2$).
- **Max Loss**: $(K_2 - K_1 - \text{Net Credit}) \times \text{Lot Size} + \text{Charges}$.
- **Safety Gate**: Long leg must be validated and executed simultaneously or prior to short leg.

