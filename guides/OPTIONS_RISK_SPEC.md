# Day Trader: Options Risk and Intelligence Specification

## 1. Safety Principles & Non-Negotiables

1. **Indian Options are Strictly PAPER-ONLY**: Under no circumstances does an Indian option order route to live broker execution.
2. **Prohibition of Naked Short Options**: Undefined-risk option positions (naked short calls, naked short puts) are strictly prohibited and failed closed by `trader-risk/check.py`.
3. **Defined-Risk Spreads ONLY**: Any short option position must be part of a defined-risk vertical credit spread with an explicit, verified long protective leg.
4. **Mandatory Net 2R After STT and Charges**: Indian options are subject to 0.125% STT on sell turnover and high statutory levies. The 2R requirement is calculated strictly on net premium after roundtrip levies.
5. **Contract Whole Lot Sizing**: All option orders must be sized in exact positive integer multiples of the exchange-mandated contract lot size (e.g., 25 for NIFTY, 15 for BANKNIFTY). Fractional option lots are rejected.
6. **Expiry Day Circuit Breaker**: New option entries are blocked after 14:30 IST on contract expiry day to protect against rapid pin risk and illiquid expiry spreads.

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
   - Verified active quote, adequate Open Interest (OI $\ge 50,000$), bid/ask spread $\le 1.0\%$, and valid contract lot size.
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

## 3. Allowed Option Strategies

### A. Long Call (BUY CE)
- **Outlook**: Bullish breakout or momentum continuation.
- **Strike Selection**: ATM (At-The-Money) or Delta $0.45 - 0.55$.
- **Risk**: Max loss strictly capped at premium paid $\times$ lot size.
- **Stop**: Technical invalidation on underlying mapped to premium stop (or fixed $20\%-25\%$ premium stop).
- **Target**: Minimum 2.0R Net reward.

### B. Long Put (BUY PE)
- **Outlook**: Bearish breakdown or VWAP rejection.
- **Strike Selection**: ATM (At-The-Money) or Delta $-0.45$ to $-0.55$.
- **Risk**: Max loss strictly capped at premium paid $\times$ lot size.
- **Stop**: Invalidation above breakdown level or premium stop.

### C. Bear Call Credit Spread (SELL CE with Long Protective CE)
- **Outlook**: Bearish to Neutral.
- **Structure**:
  - Sell Strike $K_1$ (lower strike, higher premium).
  - Buy Strike $K_2$ (higher strike, lower premium, where $K_2 > K_1$).
- **Max Loss**: $(K_2 - K_1 - \text{Net Credit}) \times \text{Lot Size} + \text{Charges}$.
- **Safety Gate**: Long leg must be validated and executed simultaneously or prior to short leg.

### D. Bull Put Credit Spread (SELL PE with Long Protective PE)
- **Outlook**: Bullish to Neutral.
- **Structure**:
  - Sell Strike $K_2$ (higher strike, higher premium).
  - Buy Strike $K_1$ (lower strike, lower premium, where $K_1 < K_2$).
- **Max Loss**: $(K_2 - K_1 - \text{Net Credit}) \times \text{Lot Size} + \text{Charges}$.
- **Safety Gate**: Long leg must be validated and executed simultaneously or prior to short leg.

