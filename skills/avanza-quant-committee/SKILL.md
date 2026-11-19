---
name: avanza-quant-committee
description: "Use when evaluating 15m TradingView telemetry bars, running /analyze, computing Avanza ISK fees/Mini Future stops, or enforcing Level-4 quantitative circuit breakers."
version: "1.0.0"
---

# Avanza Quantitative Committee

## Workflow

1. **Consult per-ticker Experience Memory and classify regime.**
   - Load `ticker_experience_memory` from `experience_memory.json` and inspect `efficiency_ratio_5d` (`ER`), `rvol`, `dsr_pct`, and `wilson_95_lower_wr_pct`.
   - **`TRENDING_BREAKOUT_REGIME` (`ER >= 0.28` & `rvol >= 1.05`):** Prioritize Intraday Momentum (`IMOM`) afternoon continuations or high-RVOL trend moves (`BUY_MINI_LONG` `MINI L` above `vwap`, `ema4`, and `ema50`; `BUY_MINI_SHORT` `MINI S` below `vwap`, `ema4`, and `ema50`).
   - **`MEAN_REVERTING_SWEEP_REGIME` (`ER < 0.28`):** Python Level-4 blocks `Momentum_Breakout` (`[VETO: LOW ER CHOP]`); only enter on a bounded liquidity sweep and reclaim of `pd_low` + `ema4` (`BUY_MINI_LONG`) or `pd_high` rejection below `ema4` (`BUY_MINI_SHORT`).
   - **Done when:** the proposed setup matches the stock's verified `ER` regime, respects `max_concurrent_leveraged_minis` (`1`), and clears `rvol >= 1.05` for breakouts.

2. **Execute the 4-level focused agentic loop (`L1` -> `L2` -> `L3a/L3b` -> `L4`).**
   - **Level-1 (Catalyst Agent):** Classify live news sentiment (`BULLISH`, `NEUTRAL`, `BEARISH_HEADWIND`, `BINARY_EVENT_RISK`) and extract `USD/SEK` (`allow_live_search=False` during historical PIT replays).
   - **Level-2 (Alpha Strategist):** Propose a `TradeDecisionDraft` from 15m telemetry (`close`, `ema4`, `ema50`, `vwap`, `or_high`, `or_low`, `pd_low`, `pd_high`, `atr14`, `rsi14`, `rvol`, `efficiency_ratio`) and the 4-bar trajectory.
   - **Level-3a & 3b (Python Math Auditor + Adversarial Critic):** Recompute fee-adjusted `python_verified_net_rr` (deducting `0.50%` USD FX drag on shares or `0.38%` of Mini intrinsic certificate equity `|spot - fin_lvl|`), reject bull/bear traps or catalyst divergence, and calibrate `confidence_score` (`0-100`).
   - **Done when:** `verdict` (`APPROVED`, `REVISED_BY_CRITIC`, or `VETOED_BY_CRITIC`), `confidence_score`, and `python_verified_net_rr` are populated.

3. **Enforce Level-4 immutable Python circuit breakers.**
   - Apply `risk_profile.json` gates: `max_daily_trades` (`2`), `max_concurrent_leveraged_minis` (`1`), `daily_loss_lockout_sek` (`-900.0`), `cooldown_minutes_after_stopout` (`45`), US open blackout (`15:30–15:45 CET`), Nordic ETP cutoff (`20:30 CET`), sub-VWAP / above-VWAP short gate with bounded sweep exception, `[VETO: LOW ER CHOP]` (`ER < 0.28`), `[VETO: LOW RVOL]` (`< 1.05`), `min_net_rr_ratio` (`1.8`), structural `[STOP FLOOR]`, `max_risk_per_trade_sek` (`450.0`) risk-parity sizing, stateful `+0.85R -> +0.15R` breakeven ratchet, and symmetric `MINI L`/`MINI S` combined `EMA4 + VWAP` failure exit.
   - **Done when:** every `BUY_*` decision passes all Python guardrails, has `quantity` sized to `<= 450 SEK` 1R risk, and includes a step-by-step Avanza order placement guide (`Triggerkurs`, `Säljkurs` `0.20 SEK` below, and `/fill_mini` sync command).
