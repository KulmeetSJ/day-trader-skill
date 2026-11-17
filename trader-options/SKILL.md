---
name: trader-options
description: Dedicated Options Intelligence Specialist for Indian Indices (NIFTY, BANKNIFTY, FINNIFTY) and F&O Equities. Evaluates, models, ranks, and communicates directional (BUY CE, BUY PE) and non-directional/premium-selling (SELL CE, SELL PE, Vertical Spreads) opportunities. Enforces authoritative Upstox BOD metadata, Black-Scholes Greeks, dynamic Delta/Gamma premium stops, full Indian statutory charges (STT, NSE, SEBI, stamp duty, GST + brokerage), and strict tail-risk disclosures. Decision-support only; live broker execution disabled.
---

# trader-options: Options Intelligence Specialist

The **trader-options** specialist acts as an expert "second pair of eyes" for a human intraday options trader in Indian markets (NSE/BSE). It continuously monitors, evaluates, ranks, and communicates actionable options setups during live market sessions.

> [!IMPORTANT]
> **Strict Execution Boundary**: This product is purely a decision-support and analytical intelligence system. The human trader independently decides whether and how to execute orders. Live broker order placement and automated order routing remain completely disabled.

---

## 1. Supported Options Strategies

Status key: **ACTIONABLE** = can reach `SIGNAL VALIDATED`/`is_actionable: true`. **ANALYSIS ONLY (never actionable)** = fully evaluated and disclosed, but structurally always rejected. **NOT IMPLEMENTED** = no code path generates this signal today; listed here only so a future contributor knows what's spec'd but not built.

1. **BUY CE (Long Call)** -- ACTIONABLE (gated by config `buy_ce_enabled`, default `true`):
   - **Market Regime**: Bullish breakout, session VWAP reclaim, EMA bullish alignment (EMA9 > EMA21), or RSI momentum (> 60).
   - **Strike Selection**: ATM or OTM1 call with tight bid-ask spread and robust volume.
   - **Risk Profile**: Strictly defined risk capped at premium paid.

2. **BUY PE (Long Put)** -- ACTIONABLE (gated by config `buy_pe_enabled`, default `true`):
   - **Market Regime**: Bearish breakdown, session VWAP rejection, EMA bearish alignment (EMA9 < EMA21), or RSI breakdown (< 40).
   - **Strike Selection**: ATM or OTM1 put with tight bid-ask spread and robust volume.
   - **Risk Profile**: Strictly defined risk capped at premium paid.

3. **SELL CE (Short Call / Premium Collection)** -- ANALYSIS ONLY, never actionable:
   - **Market Regime**: Bearish resistance rejection or range-bound market with capped upside.
   - **Strike Selection**: OTM1 or OTM2 call.
   - **Risk & Margin**: Naked short calls carry **theoretically unlimited maximum loss**. Requires full exchange initial margin (SPAN + Exposure Margin $\approx 12\% - 15\%$ of contract notional).
   - **Structurally prohibited**: unhedged single-leg option selling is unconditionally rejected (`NAKED_OPTION_PROHIBITED`), mirroring `trader-risk/check.py`'s rule for the order-execution path. This is evaluated and fully disclosed (premium, margin, tail risk) for the human trader's reference, but can never be marked actionable.

4. **SELL PE (Short Put / Premium Collection)** -- ANALYSIS ONLY, never actionable:
   - **Market Regime**: Bullish support bounce or range-bound market with strong floor.
   - **Strike Selection**: OTM1 or OTM2 put.
   - **Risk & Margin**: Naked short puts carry **substantial maximum loss** ($\text{Strike} \times \text{Lot Size}$). Requires full initial margin.
   - **Structurally prohibited**: same `NAKED_OPTION_PROHIBITED` rule as SELL CE above.

5. **Defined-Risk Debit Spreads** -- ACTIONABLE:
   - **Bull Call Spread (Debit)**: Buy ATM CE + Sell OTM2 CE. Capped upside, strictly capped max loss. Note: the spread's reward/R:R is currently derived by scaling the primary leg's own modeled premium move rather than independently projecting both legs' Greeks -- an approximation, not a full two-leg model.
   - **Bear Put Spread (Debit)**: Buy ATM PE + Sell OTM2 PE. Capped downside profit, strictly capped max loss. Same single-leg-derived R:R approximation as above.

6. **Defined-Risk Credit Spreads** -- ACTIONABLE, but **OFF by default**:
   - **Bear Call Credit Spread**: Sell OTM1 CE + Buy OTM2 CE. Gated by config `sell_ce_enabled` (default `false`). Not subject to `NAKED_OPTION_PROHIBITED` -- it has a genuine long protective leg, structurally validated (hedge must be a higher-strike call) before any signal is produced.
   - **Bull Put Credit Spread**: Sell OTM1 PE + Buy OTM2 PE. Gated by config `sell_pe_enabled` (default `false`). Same structural validation, mirrored (hedge must be a lower-strike put).
   - Margin for both is a **heuristic estimate** (`HEURISTIC_MARGIN_ESTIMATE`, `max_loss x 1.10`), same disclosure pattern as naked selling's margin estimate -- the margin shown here may not match what your broker actually requires. Turning these on means being comfortable with that estimate being approximate, not authoritative.

7. **Iron Condor** -- NOT IMPLEMENTED:
   - `STRATEGY_IRON_CONDOR` exists as a constant only. Building this properly needs two things this system doesn't have yet: a genuine 4-leg combined-margin/P&L model (simply showing the Bear Call + Bull Put spreads above side by side would understate the real position's margin efficiency, which is the whole point of combining them into one exchange position), and a non-directional/range-bound trigger condition -- today's scanner only detects directional (bullish/bearish) setups, never "sideways."

---

## 2. Complete Signal Contract

Every actionable signal generated by `trader-options` adheres to an unambiguous 20-field specification classified into five operational states:

### Operational States
1. `WATCHLIST CANDIDATE`: Potential setup identified; awaiting trigger level approach.
2. `SETUP FORMING`: Invalidation and entry zones active; awaiting candle close confirmation.
3. `TRIGGER CONFIRMED`: Underlying bar close confirms trigger level.
4. `SIGNAL VALIDATED`: All option chain liquidity, spread tolerance, contract validity, and risk criteria verified.
5. `INVALIDATED / EXPIRED`: Underlying invalidation breached or session cutoff passed.

### Required Fields
| Field | Description |
| :--- | :--- |
| `underlying_instrument` | Canonical symbol (`NIFTY`, `BANKNIFTY`, `RELIANCE`, etc.) |
| `direction` | `BULLISH`, `BEARISH`, or `RANGE_BOUND` |
| `strategy` | Strategy identifier (`BUY_CE`, `BUY_PE`, `SELL_CE`, `SELL_PE`, `BULL_CALL_SPREAD`, etc.) |
| `option_type` | `CE`, `PE`, or `SPREAD` |
| `contract` | Authoritative contract symbol (e.g. `NIFTY2026111725150CE`) |
| `expiry` | Authoritative weekly or monthly expiry date (`YYYY-MM-DD`) |
| `strike` | Strike price |
| `current_premium` | Current executable premium (Ask for buy, Bid for sell, or LTP) |
| `quote_timestamp` | Timestamp of current quote |
| `bid` / `ask` | Real-time bid and ask prices from order book |
| `underlying_trigger` | Spot price trigger, timeframe, and setup name |
| `option_entry_range` | Acceptable execution range (`min`, `max`, `ideal`) |
| `stop_loss` | Dynamic option stop loss modeled from underlying invalidation |
| `target_1` / `target_2` | Modeled option target premiums based on underlying R:R levels |
| `expected_risk_reward_after_charges` | Net R:R after deducting all Indian statutory taxes and brokerage |
| `maximum_loss` | True maximum loss (explicitly disclosed if theoretically unlimited) |
| `capital_margin_requirement` | Total capital or exchange margin required |
| `position_sizing` | Lot count, total quantity, and risk budget allocation |
| `confirmation_conditions` | Bar close and volume participation conditions |
| `invalidation_conditions` | Spot and premium invalidation levels |
| `data_provider` / `data_quality` | Provider (`upstox`), tier (`HISTORICAL`, `REAL_TIME`), and live status |
| `signal_expiry` | Enforced flat-by time (e.g. 15:15 IST) |
| `selection_rationale` | Plain-English reasoning for strike, strategy, and risk translation |

---

## 3. Dynamic Premium Stop & Target Modeling

Static percentage stops (e.g. arbitrary 20% premium stop) fail to account for underlying volatility and Greeks. `trader-options` derives premium targets and stops dynamically via Taylor series expansion:

$$\Delta S = S_{\text{level}} - S_0$$
$$\Delta P = \Delta \cdot \Delta S + \frac{1}{2} \Gamma (\Delta S)^2$$
$$\text{Premium}_{\text{level}} = \max\left(0.05, \text{round\_tick}(P_0 + \Delta P, 0.05)\right)$$

- For **Call Options** ($\Delta > 0$): Underlying decline ($\Delta S < 0$) causes premium contraction to $P_{\text{stop}}$. Underlying rise ($\Delta S > 0$) drives expansion to $P_{T1}$.
- For **Put Options** ($\Delta < 0$): Underlying rise ($\Delta S > 0$) causes premium contraction to $P_{\text{stop}}$. Underlying drop ($\Delta S < 0$) drives expansion to $P_{T1}$.
- For **Option Sellers**: Stops expand against the seller ($P_0 + |\Delta P|$) while targets represent premium decay ($P_0 - |\Delta P|$).

---

## 4. Statutory Indian Charges & Margin Engine

Every trade proposal evaluates the full schedule of Indian regulatory levies and estimated brokerage:

- **Brokerage**: ₹20 per executed order (₹40 round-trip).
- **STT (Securities Transaction Tax)**: 0.125% on option sale turnover.
- **NSE Exchange Turnover Charges**: 0.0505% on premium turnover.
- **SEBI Turnover Charges**: ₹10 per ₹1 Crore (0.0001% of turnover).
- **Stamp Duty**: 0.003% on buyer turnover.
- **GST**: 18% on (Brokerage + Exchange Charges + SEBI Charges).
- **Net R:R**:
  $$\text{Net R:R} = \frac{\text{Gross Profit}(T_1) - \text{Charges}(T_1)}{\text{Gross Loss}(\text{Stop}) + \text{Charges}(\text{Stop})}$$

### Margin & Risk Rules
- **Naked Option Selling Margin**: Labeled as `HEURISTIC_ESTIMATE (13.5% Notional; unverified exchange SPAN clearing array)`. Requires approximate initial margin: $\text{Strike} \times \text{Lot Size} \times 0.135$.
  - **Naked Call Selling**: Carries **THEORETICALLY UNLIMITED** tail risk. Flagged with `⚠️ UNDEFINED RISK (NAKED)`.
  - **Naked Put Selling**: Maximum loss is $(\text{Strike} - \text{Premium}) \times \text{Lot Size}$. Flagged with `⚠️ UNDEFINED RISK (NAKED)`.
- **Defined-Risk Vertical Spreads**: Capped at strike width or net debit. Evaluates complete two-leg structures (`leg_1` and `leg_2`) with individual symbols, strikes, bids, asks, deltas, and IV.

### Strict Risk Gating & Position Sizing Policy (P0 Rules)
- **Trade Refusal on Risk Exceedance**: If the net risk per lot after statutory charges exceeds the configured `max_loss_budget` (e.g. ₹5,164 risk vs ₹2,000 budget), the engine refuses the trade: `lots = 0`, status `SIGNAL REJECTED / RISK_EXCEEDS_BUDGET`. It never forces 1 lot.
- **Minimum 2.0R Net R:R Enforcement**: Any setup with `net_reward_risk < 2.0` (e.g. 1.58:1) is rejected with `RR_BELOW_MINIMUM`.
- **Available Capital & Margin Gating**: Total required capital/margin must not exceed available capital.
- **Data Provenance & Freshness Gating**: Only real-time live quotes during open market hours can produce `SIGNAL VALIDATED`. Historical or closed-session quotes produce `HISTORICAL ANALYSIS` or `SIGNAL REJECTED`.
- **Zero Fabrication**: No dummy setups are created if underlying chart data is missing. Missing chart returns `NO TRADE / MISSING_UNDERLYING_CHART`. Premium models never use arbitrary percentage stops/targets. When the Delta/Gamma model cannot produce a viable stop/target (`MODEL_UNAVAILABLE`), `stop_loss`/`target_1`/`target_2`/net R:R are reported as `None`/`N/A`, never as a fabricated `0.0` -- a `0.0` always means a genuinely computed value, never "unavailable."
- **Naked Option Prohibition**: SELL CE and SELL PE are evaluated and fully disclosed (premium, margin, tail risk) for reference, but are unconditionally rejected (`NAKED_OPTION_PROHIBITED`) and can never reach `SIGNAL VALIDATED` -- this does not depend on margin data being authoritative.
- **Policy Caps Honored**: `buy_ce_enabled` / `buy_pe_enabled` / `sell_ce_enabled` / `sell_pe_enabled` (config) each suppress that strategy's generation entirely when `false` (the two sell_* flags default `false` -- credit spreads are opt-in); `max_option_lots` (config, default 2) is enforced as a hard ceiling on `position_sizing.lots` regardless of what the risk-budget/capital/depth tiers alone would otherwise permit; `max_correlated_exposure_pct` (config, default 40%) caps how much capital can go into the broad-index correlation group (all 5 major indices) across a single sweep, checked independently of overall capital.

---

## 5. Synthetic Options Simulation (TimeMachine Replay)

> [!WARNING]
> **SIMULATION DISCLOSURE & LIMITATIONS**:
> The `trader-options` replay engine is labeled `SYNTHETIC OPTIONS SIMULATION`. It models option premium trajectories from underlying spot bars using second-order Delta-Gamma Taylor expansions calibrated to snapshot Greeks.
> It is **NOT** authentic historical options tick backtesting. Authentic historical options backtesting requires tick-level or 1-minute historical option chain archives including real bid/ask order book quotes and historical IV dynamics.

---

## 6. CLI & Orchestration

### Inspect Normalized Option Chain
```bash
python3 trader-options/options.py chain --symbol NIFTY
python3 trader-options/options.py chain --symbol BANKNIFTY
```

### Deep Dual-Directional Analysis
```bash
python3 trader-options/options.py analyze --symbol NIFTY --budget 2000
python3 trader-options/options.py analyze --symbol BANKNIFTY --budget 5000
```

### Natural Language Orchestration
```bash
python3 trader-options/orchestrator.py "Scan the market for the best intraday opportunities."
python3 trader-options/orchestrator.py "Find BANKNIFTY option-selling setups."
python3 trader-options/orchestrator.py "Find bearish F&O stock opportunities."
python3 trader-options/orchestrator.py "Compare the best three opportunities in the market."
```

