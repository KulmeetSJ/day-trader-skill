# Day Trader: Comprehensive Intraday Strategy Specification

## 1. Overview
The deterministic strategy engine evaluates actionable intraday setups across Indian (NSE/BSE) and US (NYSE/NASDAQ) markets.
Financial math, indicator calculations, invalidation levels, and risk checks are strictly deterministic -- AI orchestrates, surveys, and explains, but never invents financial figures.

---

## 2. The 10 Core Intraday Strategies

### 1. Breakout (`breakout` / `breakout_intraday`)
- **Premise**: Stock breaks above an identified intraday resistance level on surge volume with price above VWAP.
- **Criteria**: Price closes above highest high of the prior `lookback_bars` (default 12 bars).
- **Confirmations**: $\text{RVOL} \ge 1.5$, price holding above intraday VWAP, trigger candle close near high.
- **Stop Policy**: `breakout-invalidation` (placed just below the broken breakout level).
- **Target Policy**: Minimum 2.5R to 3.0R (authoritative net 2.0R floor enforced after Indian charges).
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`.
- **Ineligible Regimes**: `STRONG_BEARISH`, `SIDEWAYS`.

### 2. 20-Session Closing Price Breakout (`breakout_20d`)
- **Premise**: Daily breakout above the highest closing price of the prior 20 sessions.
- **Criteria**: Today's intraday price breaches highest close of prior 20 daily candles.
- **Confirmations**: Above 20-day high close, RVOL expansion, index trend alignment.
- **Stop Policy**: Breakout anchor price or ATR trailing buffer.
- **Target Policy**: 3.0R minimum.
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`.

### 3. Opening Range Breakout (`orb`)
- **Premise**: Momentum breakout from opening price discovery range (15m or 30m).
- **Criteria**: Candle closes strictly above the established Opening Range High (`OR-High`).
- **Confirmations**: Trigger bar volume $\ge 1.5\times$ average opening range volume, price > VWAP.
- **Stop Policy**: `or-mid` (midpoint of opening range) or `or-low`.
- **Target Policy**: Minimum 2.5R target based on risk from entry to stop.
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`.

### 4. VWAP Reclaim (`vwap_reclaim`)
- **Premise**: Early selloff or gap-down reclaims institutional VWAP benchmark on heavy volume.
- **Criteria**: Stock traded below VWAP for at least 2 bars, crosses above VWAP and closes above.
- **Confirmations**: Increasing volume delta / RVOL $\ge 1.3$, subsequent retest holds above VWAP.
- **Stop Policy**: Prior swing low or 1 tick below VWAP band.
- **Target Policy**: 2.5R to 3.0R.
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`, `SIDEWAYS`.

### 5. VWAP Rejection (`vwap_rejection`)
- **Premise**: Weak counter-trend rally reaches VWAP from below, encounters heavy selling aggressor volume.
- **Criteria**: Price tests underside of VWAP, fails to break through, and closes below prior candle low.
- **Confirmations**: Negative volume delta, declining market breadth, index below EMA20.
- **Stop Policy**: Just above VWAP line.
- **Target Policy**: 2.5R down to intraday lows.
- **Eligible Regimes**: `STRONG_BEARISH`, `BEARISH`.

### 6. EMA Alignment (`ema_alignment`)
- **Premise**: Strong directional trend where short, medium, and long EMAs stack in canonical order.
- **Criteria**: $\text{EMA}_9 > \text{EMA}_{21} > \text{EMA}_{50}$ with positive slope on all three.
- **Confirmations**: Price above $\text{EMA}_9$, RVOL $\ge 1.2$, RSI between 55 and 70.
- **Stop Policy**: $\text{EMA}_{21}$ trailing level or swing low.
- **Target Policy**: 2.5R minimum.
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`.

### 7. RSI Bullish Momentum (`rsi_momentum`)
- **Premise**: Momentum thrust where RSI crosses into the bullish power zone ($60-75$).
- **Criteria**: 14-period RSI crosses above 60 from sub-50 levels while price trades above VWAP.
- **Confirmations**: MACD histogram expansion, volume expansion.
- **Stop Policy**: 1.5 ATR trailing stop or VWAP anchor.
- **Target Policy**: 2.5R to 3.0R.
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`.

### 8. Trend Continuation (`trend_continuation` / `trend`)
- **Premise**: Established uptrend printing higher highs and higher lows consolidates and breaks out.
- **Criteria**: Price consolidates in tight flag/pennant for 3-6 bars and breaks out in trend direction.
- **Confirmations**: Declining volume during flag, volume explosion on continuation trigger candle.
- **Stop Policy**: Low of consolidation flag.
- **Target Policy**: 2.5R minimum.
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`.

### 9. Pullback (`pullback` / `vwap_pullback`)
- **Premise**: Leading stock in strong uptrend pulls back on light volume to test VWAP or 20 EMA.
- **Criteria**: Price touches VWAP/EMA20 support, prints a bullish reversal wick candle.
- **Confirmations**: Low pullback volume followed by bullish trigger bar with RVOL surge.
- **Stop Policy**: 1 tick below pullback pivot low.
- **Target Policy**: 2.5R to 3.0R.
- **Eligible Regimes**: `STRONG_BULLISH`, `BULLISH`.

### 10. Reversal (`reversal` / `exhaustion_reversal`)
- **Premise**: Climactic capitulation selloff hits major multi-day support with volume spike and divergence.
- **Criteria**: RSI < 25 on 5m, price prints high-volume hammer/absorption candle at key level.
- **Confirmations**: Bullish volume divergence, positive volume delta on reversal bar.
- **Stop Policy**: Low of exhaustion candle.
- **Target Policy**: Retest of falling VWAP (minimum 2.5R).
- **Eligible Regimes**: `BEARISH`, `SIDEWAYS`, `HIGH_VOLATILITY`.

---

## 3. Opportunity Scoring and Ranking Formula

When scanning universes, opportunities are ranked by a deterministic Composite Quality Score ($0 - 100$):

$$\text{Score} = S_{\text{trend}} + S_{\text{momentum}} + S_{\text{rvol}} + S_{\text{vwap}} + S_{\text{regime}} + S_{\text{rr}}$$

1. **Trend Alignment ($0 - 25$ pts)**: Full points if stock trend matches benchmark index trend (e.g. NIFTY/SPY).
2. **Momentum ($0 - 20$ pts)**: Based on RSI position ($55 - 70$) and positive MACD histogram slope.
3. **RVOL ($0 - 20$ pts)**:
   - $\text{RVOL} \ge 2.5$: 20 pts
   - $\text{RVOL} \ge 1.8$: 15 pts
   - $\text{RVOL} \ge 1.3$: 10 pts
   - $\text{RVOL} < 1.0$: 0 pts
4. **VWAP Relationship ($0 - 15$ pts)**: Price comfortably holding $0.2\% - 1.0\%$ above VWAP.
5. **Regime Alignment ($0 - 10$ pts)**: Setup is explicitly eligible in current market regime.
6. **Net Risk/Reward ($0 - 10$ pts)**: Bonus for clean net R:R $> 3.0$ after all transaction charges.
