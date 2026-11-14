# Day Trader: Monday Indian Equity Controlled Paper-Trading Runbook

**Target Market**: Indian Equities (NSE / BSE)  
**Execution Mode**: **STRICTLY PAPER TRADING** (`paper: true`)  
**Trading Session Date**: Monday, November 16, 2026 (or next active trading session)  
**Session Hours (IST)**:
- Pre-Open: 09:00 - 09:15 IST
- Regular Trading: 09:15 - 15:30 IST
- Strategy Entry Window: 09:30 - 15:00 IST
- Mandatory Day Trade Flat-By: 15:20 IST

---

## Non-Negotiable Operational Rules

1. **Paper-Only Execution**: Under no circumstances should live order execution be attempted for Indian markets.
2. **Read-Only Data Provider**: Upstox feeds are strictly read-only for market data.
3. **Long-Only Equity Trading**: Only buy orders (`side: "buy"`) may be planned and sized. Short sales are unsupported and blocked.
4. **Mandatory Net 2.0R**: No order may be sized or approved if net reward/risk after statutory charges is below 2.0.
5. **Human Approval Gate**: Every order ticket requires explicit manual confirmation text (`yes buy <QTY> <SYMBOL>`).
6. **No Stale or Future Data**: If data timestamps are delayed past threshold or in the future, the engine must fail closed.

---

## Phase A: Pre-Market Preparation (08:30 - 09:15 IST)

*These checks are performed before the exchange opens and do not require live incoming tick data.*

### A.1. Verify Python Environment & Configuration
```bash
# 1. Verify that active configuration is set to India and strictly paper-only
python3 config/loader.py show --market IN

# Expected Output:
#   market: IN
#   currency: INR
#   broker: paper
#   paper: true
#   min_reward_risk: 2.0

# 2. Validate configuration file
python3 config/loader.py validate --config config/config.in.json
```
*Failure Action*: If `paper` is false or validation fails, stop immediately. Reinitialize via `python3 config/loader.py init --market IN --out trader/config.json`.

### A.2. Market Calendar & Readiness Health Check
```bash
# Verify market clock, holiday calendar, and session phases
python3 trader-alerts/alerts.py clock --market IN

# Run automated pre-flight readiness audit
python3 trader-alerts/alerts.py readiness --market IN
```
*Expected Output*: `OVERALL VERDICT: READY` or `READY_WITH_NOTICES`. Confirms instrument master loaded, calendar active, and paper trading available.

### A.3. Instrument Master & Security Checks
```bash
# Inspect symbol metadata, tick size, and T2T category for focus watchlist
python3 trader-alerts/instruments.py inspect RELIANCE --exchange NSE
python3 trader-alerts/instruments.py inspect TCS --exchange NSE
python3 trader-alerts/instruments.py inspect INFY --exchange NSE
```
*Expected Output*:
- `tick_size: 0.05`
- `lot_size: 1`
- `is_trade_to_trade: False`
*Failure Action*: If `is_trade_to_trade` is `True`, remove the symbol from intraday day-trading watchlist immediately.

### A.4. Pre-Market Index & Breadth Analysis
```bash
# Analyze benchmark indices on daily bars to determine macro regime
python3 trader-scanner/scan.py indices --exchange NSE --interval 1d
```
*Expected Output*: Outputs trend, 20/50 EMA, RSI, and regime (`STRONG_BULLISH`, `BULLISH`, `SIDEWAYS`, `BEARISH`, `STRONG_BEARISH`, `HIGH_VOLATILITY`).

---

## Phase B: Market Open & Entry Evaluation (09:30 - 11:30 IST)

*Live market data begins flowing at 09:15 IST. Intraday entries commence at 09:30 IST after the initial opening range forms.*

### B.1. Verify Market Hours & Live Data Flow
```bash
# Check that market phase is OPEN
python3 trader-alerts/alerts.py clock --market IN
# Expected Output: "NSE OPEN (Non-CAS), phase: open" (Exit code 0)
```

### B.2. Scan for Intraday Setups (5-minute Candles)
```bash
# Scan universe on 5m bars for ORB, VWAP reclaim, and breakout setups
python3 trader-scanner/scan.py intraday --market IN --interval 5m --setups orb,vwap_reclaim,breakout_intraday --out trader/watchlist.json
```
*Expected Output*: `trader/watchlist.json` populated with candidate hits.

### B.3. Evaluate Candidate Entry Bar-by-Bar
```bash
# Evaluate specific symbol setup (e.g. RELIANCE ORB)
python3 trader-entry/entry.py check RELIANCE --setup orb --interval 5m --market IN --exchange NSE --write trader/entry.json
```
*Interpretation*:
- `status: "triggered"` $\rightarrow$ Proceed to Exit Planning.
- `status: "waiting"` $\rightarrow$ Setup forming; do not enter.
- `d_state: "NO TRADE"` (`EXTENDED`) $\rightarrow$ Price chased past entry zone; reject trade.
- `d_state: "NO TRADE"` (`STALE_DATA`) $\rightarrow$ Feed stale or missing; reject trade.

### B.4. Plan Protective Stop & Minimum 2R Target
```bash
# Build exit plan with ATR or OR-mid stop and mandatory 2R target
python3 trader-exit/exit.py plan --from trader/entry.json --market IN --exchange NSE --stop atr:1.5 --target 2R --write trader/plan.json
```
*Expected Output*: `trader/plan.json` with `reward_risk >= 2.0`, `stop < entry`, `target > entry`, `tick_size: 0.05`.

### B.5. Size Order to Budget & Statutory Costs
```bash
# Calculate whole-share size and deduct statutory charges (STT, NSE, SEBI, GST, Stamp Duty)
python3 trader-budget/size.py --plan trader/plan.json --market IN --exchange NSE --portfolio trader/portfolio.json --write trader/orders.json
```
*Expected Output*: `trader/orders.json` containing whole shares, estimated friction costs, and `net_reward_risk >= 2.0`.

### B.6. Pre-Trade Deterministic Risk Validation
```bash
# Run mandatory risk check and generate cryptographic order fingerprint
python3 trader-risk/check.py --orders trader/orders.json --portfolio trader/portfolio.json --market IN --exchange NSE --out trader/risk.json
```
*Expected Output*: Exit code 0, `verdict: PASS`, 24-character SHA-256 fingerprint recorded.
*Failure Action*: If `verdict: BLOCK`, do not proceed. Review blocking reasons in `trader/risk.json`.

### B.7. Human Confirmation & Paper Ticket Execution
```bash
# 1. Render plain order ticket showing risk PASS proof
python3 trader-broker/ticket.py show --orders trader/orders.json --risk trader/risk.json

# 2. Record explicit human confirmation (exact side, qty, and symbol required)
python3 trader-broker/ticket.py approve --index 1 --reply "yes buy <QTY> <SYMBOL>" --orders trader/orders.json --risk trader/risk.json --state trader/state/broker.json

# 3. Record paper fill in portfolio
python3 trader-broker/ticket.py result --index 1 --status placed --filled-qty <QTY> --filled-price <PRICE> --orders trader/orders.json --risk trader/risk.json --portfolio trader/portfolio.json --state trader/state/broker.json
```
*Expected Output*: Order status updated to `filled` in `trader/state/broker.json`; cash and share lot updated in `trader/portfolio.json`.

---

## Phase C: Mid-Session Position Management (11:30 - 15:00 IST)

### C.1. Position Monitoring & Trailing Stops
```bash
# Evaluate open positions against latest market bars
python3 trader-exit/exit.py manage --market IN --exchange NSE --interval 5m --portfolio trader/portfolio.json
```
*Possible Decisions*:
- `HOLD`: Position within normal fluctuation; maintain stop.
- `UPDATE TRAILING STOP`: Price moved in favor; adjust protective stop level.
- `PARTIAL EXIT`: Target 1 reached; scale out partial lot.
- `EXIT`: Stop triggered, target achieved, or invalidation reached.

### C.2. Executing Position Flattening
```bash
# If EXIT is signaled, generate exit sell order
python3 trader-broker/ticket.py flatten --symbol <SYMBOL> --market IN

# Pass exit order through risk gate (unpriced market exit bypasses day limit)
python3 trader-risk/check.py --orders trader/orders.json --portfolio trader/portfolio.json --market IN --out trader/risk.json

# Approve and record exit fill
python3 trader-broker/ticket.py approve --index 1 --reply "yes sell <QTY> <SYMBOL>" --orders trader/orders.json --risk trader/risk.json --state trader/state/broker.json
python3 trader-broker/ticket.py result --index 1 --status placed --filled-qty <QTY> --filled-price <PRICE> --orders trader/orders.json --risk trader/risk.json --portfolio trader/portfolio.json --state trader/state/broker.json
```

---

## Phase D: Market Close & Session Reconciliation (15:20 - 15:45 IST)

*Indian intraday day trades must be flattened prior to 15:20 IST.*

### D.1. Mandatory Day Trade Flat-By Enforcement
```bash
# Check for any lingering intraday positions
python3 trader-exit/exit.py manage --market IN --portfolio trader/portfolio.json
```
*Rule*: Any open intraday day trade must be flattened immediately at market before 15:25 IST.

### D.2. Reconcile Paper Portfolio & Journal State
```bash
# Review today's paper trading decisions and fill history
python3 trader-broker/ticket.py log

# Inspect portfolio cash balance and realized P&L
cat trader/portfolio.json
```

### D.3. Post-Session Error & Risk Review
- Review `trader/risk.json` for any blocked attempts.
- Verify that `trader/portfolio.json` cash matches initial budget plus net realized P&L.
- Archive session state:
  ```bash
  mkdir -p trader/archive/$(date +%Y-%m-%d)
  cp trader/*.json trader/archive/$(date +%Y-%m-%d)/ 2>/dev/null || true
  ```

