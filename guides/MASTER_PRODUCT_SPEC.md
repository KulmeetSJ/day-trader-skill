# Day Trader: Master Product Specification

## 1. System Overview & Architecture

Day Trader is an AI-orchestrated, deterministically verified intraday trading research, analysis, and paper trading system designed for both Indian (NSE/BSE) and US (NYSE/NASDAQ) equity markets.

### Architecture Decoupling
- **Deterministic Python Engine**: Solely responsible for mathematical indicators, position sizing, transaction cost estimation, circuit breakers, risk validation, and paper portfolio accounting.
- **AI Skill Orchestrator**: Handles user dialogue, contextual explanation, research synthesis, trade thesis articulation, and audit reporting. The AI is strictly prohibited from inventing financial metrics, fabricating market data, or overriding deterministic risk refusals.

```
       +-------------------------------------------------------------+
       |                   AI Agent Orchestration                    |
       |  (Dialogue, Research, Multi-Market Synthesis, Explanations) |
       +------------------------------+------------------------------+
                                      |
       +------------------------------v------------------------------+
       |              Deterministic Core Engine Architecture         |
       +-------------------------------------------------------------+
       | 1. Config Loader & Provenance  | 2. Market Clock & Calendar |
       | 3. Multi-Provider Market Data  | 4. Market Regime & Breadth |
       | 5. Volume & Order Flow Engine  | 6. 10 Intraday Strategies  |
       | 7. Exit & Trailing Stop Engine | 8. Statutory Costs & Net 2R|
       | 9. Pre-Trade Risk & Fingerprint| 10. Paper Broker & Journal |
       +-------------------------------------------------------------+
```

---

## 2. Supported Markets & Trading Rules

| Feature | Indian Market (NSE / BSE) | US Market (NYSE / NASDAQ) |
|---|---|---|
| **Execution Mode** | **STRICTLY PAPER-ONLY** | Paper by default; live Alpaca route guarded |
| **Trading Direction**| **LONG ONLY** (Short execution unsupported) | **LONG ONLY** (Short execution unsupported) |
| **Trading Currency** | Indian Rupee (INR / ₹) | US Dollar (USD / $) |
| **Tick Size Model** | Authoritative Master Tick (₹0.05 standard) | $0.01 standard |
| **Minimum Net R:R** | Mandatory $\ge 2.0$ after all statutory charges | Mandatory $\ge 2.0$ net floor |
| **Options Capabilities** | Analytical signal only (BUY CE options_orb); execution sizing disabled; BUY PE unsupported; defined-risk spreads unsupported; naked options prohibited | Analytical signal only; execution sizing disabled; naked options prohibited |
| **Lot Size Multiples** | Enforced integer F&O contract lot multiples | Standard 100-share contract units |
| **T2T Restrictions** | Trade-to-Trade (BE/BZ/T) intraday blocked | Day-trading pattern / margin rules |
| **Trading Windows** | Entries: 09:30 - 15:00 IST; Flat-by: 15:20 | Entries: 09:45 - 15:30 ET; Flat-by: 15:55 |
| **Statutory Levies** | STT, Exchange fees, SEBI charges, Stamp, GST | SEC & FINRA regulatory transaction fees |

---

## 3. Explicit Operational Boundaries & Limitations

1. **Long-Only Equity Execution**: The system plans and sizes long equity trades only. Short-side signals (`vwap_rejection`) are analytical only; downstream execution is fail-closed.
2. **Read-Only Market Data (Upstox)**: Upstox API keys and tokens provide market data, historical bars, and instrument master sync only. Zero order routing endpoints exist.
3. **Options Signal-Only Mode**: Call option alerts (`options_orb`) provide entry previews. Sizing and broker routing for options are disabled.
4. **TimeMachine Replay Scope**: Historical replay is implemented for the `orb` setup only. Monte Carlo simulates fixed barrier hit rates on historical bars rather than dynamic trailing exit logic.
5. **Standalone Research Tools**: `trader-analyst`, `trader-news`, and `trader-pelosi` operate as independent research CLIs and do not feed automated trade generation.
6. **Unkeyed Truncated SHA-256 Fingerprint**: Order hashes use the first 24 characters of a standard SHA-256 digest over normalized parameters and policy snapshots (not HMAC).
7. **Python 3.8 Target Compatibility**: Syntax compatibility is maintained targeting Python 3.8+, with runtime verification on Python 3.13.1.

---

## 4. Cryptographic Order Fingerprinting

Every proposed order is converted to canonical decimal string representation and hashed via SHA-256 with the active policy snapshot, creating an immutable 24-character `order_fingerprint`.
If any risk parameter, stop price, target price, or share quantity is altered, the fingerprint changes, immediately invalidating prior approvals.

---

## 5. End-to-End Workflow & 22-Item Standardized Analysis Report

An actionable intraday analysis delivers the complete 22-item standardized report:
1. **Symbol & Exchange**: Canonical ticker and trading venue (e.g. `NSE:RELIANCE`).
2. **Market Session & Status**: Current session state (Pre-open, Regular, Closing, Post-market, Holiday).
3. **Market Regime**: Broad index classification (`STRONG_BULLISH`, `BULLISH`, `SIDEWAYS`, `BEARISH`, `STRONG_BEARISH`, `HIGH_VOLATILITY`).
4. **Strategy Evaluated**: Evaluated strategy (e.g. `orb`, `vwap_reclaim`).
5. **Decision / State**: Explicit recommendation lifecycle status (`ENTRY CONFIRMED`, `ENTRY CANDIDATE`, `SETUP`, `NO TRADE`).
6. **Trigger Candle Time & Price**: Precise bar timestamp and closing trigger price.
7. **Entry Price & Entry Zone**: Planned execution level and anti-chasing boundary.
8. **Stop-Loss Price & Invalidation**: Technical invalidation anchor.
9. **Target Prices**: Target 1 (1R preview), Target 2 (2R Net minimum).
10. **Gross Reward/Risk**: Ratio prior to friction ($R_{\text{gross}} = \frac{T - E}{E - S}$).
11. **Estimated Transaction Costs & Taxes**: Roundtrip STT, exchange fees, SEBI, stamp duty, GST, brokerage.
12. **Net Reward/Risk**: Ratio after all friction ($R_{\text{net}} \ge 2.0$).
13. **Relative Volume (RVOL)**: Time-of-day volume multiplier.
14. **Volume Delta & CVD**: Actual (if trade ticks available) or Estimated (CLV model).
15. **VWAP Relationship**: Position relative to institutional VWAP anchor.
16. **Higher Timeframe Alignment**: Multi-timeframe trend confluence (15m/1h/Daily).
17. **Position Size**: Sized to risk limit in whole shares.
18. **Maximum Loss**: Currency loss at stop-loss execution.
19. **Key Risk Factors**: Imminent events, index divergence, spread width.
20. **Invalidation Conditions**: Specific price action nullifying the trade thesis.
21. **Order Fingerprint**: Cryptographic 24-character SHA-256 integrity hash.
22. **Execution Mode**: Explicit confirmation of `PAPER ONLY` status.
