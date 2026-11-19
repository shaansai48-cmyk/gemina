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

## 2. Regime-Matched Bidirectional Trading & Experience Memory (73.3% WR / 3.66 PF Benchmark)
- **Consult `ticker_experience_memory`:** Every evaluation includes the stock's empirical walk-forward dossier from `experience_memory.json`.
- **Kaufman 5-Day Efficiency Ratio (`efficiency_ratio_5d`) Regime Switch:**
  - **`TRENDING_BREAKOUT_REGIME` (`ER >= 0.28`):** Trade `BUY_MINI_LONG` (`MINI L`) on confirmed 15m closes above `or_high`, `vwap`, `ema4`, and `ema50` with `rvol >= 1.05` and `or_low > pd_low`. Trade `BUY_MINI_SHORT` (`MINI S`) on confirmed 15m closes below `or_low`, `vwap`, `ema4`, and `ema50` with `rvol >= 1.05` and `or_high < pd_high`.
  - **`MEAN_REVERTING_SWEEP_REGIME` (`ER < 0.28`):** Veto breakout chasing above `or_high`; only enter on a liquidity sweep and reclaim of `or_low`/`pd_low` + `vwap` (`BUY_MINI_LONG` when `close > ema50`) or failed sweep of `or_high`/`pd_high` + rejection below `vwap` (`BUY_MINI_SHORT` when `close < ema50`).
- **Deterministic Risk-Parity Sizing & `+0.85R` Breakeven Ratchet:**
  - Position size `quantity` is capped so `1R` stop risk $\le$ `450.0 SEK` (`max_risk_per_trade_sek`).
  - Once unrealized profit reaches `+0.85R`, ratchet the Avanza Stop-Loss `Triggerkurs` to lock in `+0.15R` profit.
- **Step-by-Step Avanza Order Guide:** Every active trade recommendation (`BUY_*` or `SELL_*`) automatically appends a step-by-step Avanza ISK order placement checklist (`Köp`/`Sälj`, `Antal`, limit `Kurs`, hardware Stop-Loss `Triggerkurs` and `Säljkurs`, `+0.85R` breakeven ratchet level, and Telegram `/fill_mini` sync command).
