---
name: avanza-quant-committee
description: "Use when evaluating 15m TradingView telemetry bars, running /analyze, computing Avanza ISK fees/Mini Future stops, or enforcing Level-4 quantitative circuit breakers."
version: "1.0.0"
---

# Avanza Quantitative Committee

## Workflow

1. **Consult per-ticker Experience Memory and classify regime.**
   - Load `ticker_experience_memory` from `experience_memory.json` and inspect `efficiency_ratio_5d` (`ER`).
   - **`TRENDING_BREAKOUT_REGIME` (`ER >= 0.28`):** Trade `BUY_MINI_LONG` (`MINI L`) above `or_high`, `vwap`, `ema4`, and `ema50` (`rvol >= 1.05`, `or_low > pd_low`), or `BUY_MINI_SHORT` (`MINI S`) below `or_low`, `vwap`, `ema4`, and `ema50` (`rvol >= 1.05`, `or_high < pd_high`).
   - **`MEAN_REVERTING_SWEEP_REGIME` (`ER < 0.28`):** Reject breakout chasing above `or_high`; only enter on a liquidity sweep and reclaim of `or_low`/`pd_low` + `vwap` (`BUY_MINI_LONG` above `ema50`) or `or_high`/`pd_high` rejection below `vwap` (`BUY_MINI_SHORT` below `ema50`).
   - **Done when:** the proposed setup matches the stock's verified `ER` regime and `rvol >= 1.05` when breakout-trading.

2. **Execute the 4-level focused agentic loop (`L1` -> `L2` -> `L3a/L3b` -> `L4`).**
   - **Level-1 (Catalyst Agent):** Classify live news sentiment (`BULLISH`, `NEUTRAL`, `BEARISH_HEADWIND`, `BINARY_EVENT_RISK`) and extract `USD/SEK`.
   - **Level-2 (Alpha Strategist):** Propose a `TradeDecisionDraft` from 15m telemetry (`close`, `ema4`, `ema50`, `vwap`, `or_high`, `or_low`, `pd_low`, `pd_high`, `atr14`, `rsi14`, `rvol`) and the 4-bar trajectory.
   - **Level-3a & 3b (Python Math Auditor + Adversarial Critic):** Recompute fee-adjusted `python_verified_net_rr` (deducting `0.50%` USD FX drag or `0.38%` Mini spread), reject bull traps/catalyst divergence, and calibrate `confidence_score` (`0-100`).
   - **Done when:** `verdict` (`APPROVED`, `REVISED_BY_CRITIC`, or `VETOED_BY_CRITIC`), `confidence_score`, and `python_verified_net_rr` are populated.

3. **Enforce Level-4 immutable Python circuit breakers.**
   - Apply `risk_profile.json` gates: `max_daily_trades` (`2`), `daily_loss_lockout_sek` (`-900.0`), `cooldown_minutes_after_stopout` (`45`), US open blackout (`15:30–15:45 CET`), Nordic ETP cutoff (`20:30 CET`), sub-VWAP gate with bounded `[pd_low - 1.0*ATR14, pd_low + 0.35*ATR14]` sweep exception, `min_net_rr_ratio` (`1.8`), structural `[STOP FLOOR]`, `max_risk_per_trade_sek` (`450.0`) risk-parity sizing, `+0.85R` breakeven ratchet, and `[EMA4 FAILURE EXIT]` (`ema4 - 0.15*ATR14`).
   - **Done when:** every `BUY_*` decision passes all Python guardrails, has `quantity` sized to `<= 450 SEK` 1R risk, and includes a step-by-step Avanza order placement guide (`Triggerkurs` and `Säljkurs` `0.20 SEK` below).
