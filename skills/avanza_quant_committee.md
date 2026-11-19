---
name: avanza_quant_committee
description: 4-Level Focused Agentic Quant Committee rules and deterministic Avanza ISK circuit-breaker constraints.
version: 1.0.0
---

# Avanza Quantitative Committee Skill

## 1. Multi-Level Focused Agentic Loop
Every 15m bar evaluation follows four specialized cognitive levels:
- **Level-1 (Catalyst & Macro Agent):** Classify live news sentiment into `BULLISH`, `NEUTRAL`, `BEARISH_HEADWIND`, or `BINARY_EVENT_RISK` and extract live `USD/SEK`.
- **Level-2 (Alpha Strategist):** Propose an initial `TradeDecisionDraft` from 15m price telemetry (`close`, `ema4`, `vwap`, `or_high`, `or_low`, `pd_low`, `atr14`, `rsi14`) and recent 4-bar trajectory.
- **Level-3a & 3b (Python Math Auditor + Adversarial Red-Team Critic):** Audit Level-2's draft against Python's deterministic fee-adjusted R:R (`python_verified_net_rr`), check for bull-traps or catalyst divergence, and calibrate `confidence_score` (`0-100`) and `verdict` (`APPROVED`, `REVISED_BY_CRITIC`, `VETOED_BY_CRITIC`).
- **Level-4 (Hard Python Circuit Breakers):** Enforce immutable limits from `risk_profile.json`:
  1. Max 2 trades/day (`[VETO: DAILY QUOTA]`)
  2. Daily loss lockout at `-900.0 SEK` (`[VETO: LOSS LOCKOUT]`)
  3. 45-minute cooldown after a stop-out (`[VETO: COOLDOWN]`)
  4. Opening Bell blackout `15:30–15:45 CET` (`[VETO: OPENING BELL GATE]`)
  5. Nordic ETP cutoff at `20:30 CET` (`[VETO: ETP CUTOFF]`)
  6. Close above 15m VWAP required for Mini Longs unless sweeping within `[pd_low - 1.0 * ATR14, pd_low + 0.35 * ATR14]` and reclaiming `EMA4` (`[VETO: SUB-VWAP]`)
  7. Minimum fee-adjusted Net R:R `>= 1.8` (`[VETO: LOW R:R]`), structural stop floor at `pd_low - 0.20 * ATR14` (or below `min(vwap, low) - 0.20 * ATR14` when breaking out `> 2.5 * ATR14` above `pd_low`) (`[STOP FLOOR]`), and immediate Mini exit if 15m close breaks below `EMA4 - 0.15 * ATR14` (`[EMA4 FAILURE EXIT]`).

## 2. High-Beta Stock Regime Filter (Validated on 22-Day TSLA 15m Walk-Forward Backtest)
For high-beta momentum stocks (`TSLA`, `NVDA`):
- **Opening-Range & Daily Higher-Low Alignment:** Prioritize `BUY_MINI_LONG` entries when the session's Opening Range shows a **Daily Higher-Low (`or_low > pd_low`)**, price is above both `15m VWAP` and `EMA4` (`RSI14 >= 52`), and price is not overextended (`(close - vwap) <= 0.85 * max(atr14, 0.45 * (or_high - or_low))`).
- **Volatility-Aware Stop Placement:** Because `atr14` at `15:45 CET` still includes 12 quiet late-session bars from the prior day, measure early-session effective volatility as `eff_vol = max(atr14, 0.45 * (or_high - or_low))` so stops are not placed inside normal opening noise.
