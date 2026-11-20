---
name: avanza-quant-committee
description: "Use when evaluating 15m market telemetry, running /analyze, computing Avanza ISK fees or Mini Future stops, or enforcing Level-4 quantitative circuit breakers."
version: "1.0.0"
---

# Avanza Quantitative Committee

## Workflow

1. **Consult per-ticker Experience Memory and classify the market regime.**
   - Load `ticker_experience_memory` from `experience_memory.json` and inspect 5-day Kaufman trend efficiency (`efficiency_ratio_5d`), relative volume (`rvol`), empirical win rate, and learned rules.
   - **Trending Regime (`efficiency_ratio_5d >= 0.28` and `rvol >= 1.05`):** Prioritize afternoon Intraday Momentum (`IMOM`) continuations or high-volume trend moves (`BUY_MINI_LONG` `MINI L` above `vwap`, `ema4`, and `ema50`; `BUY_MINI_SHORT` `MINI S` below `vwap`, `ema4`, and `ema50`).
   - **Mean-Reverting Chop (`efficiency_ratio_5d < 0.28`):** Python guardrails block `Momentum_Breakout` entries (`[VETO: LOW ER CHOP]`); only enter on a bounded liquidity sweep and reclaim of `pd_low` + `ema4` (`BUY_MINI_LONG`) or `pd_high` rejection below `ema4` (`BUY_MINI_SHORT`).
   - **Done when:** the proposed setup matches the ticker's verified regime, respects `max_concurrent_leveraged_minis` (`1`), and clears `rvol >= 1.05` for breakouts.

2. **Execute the 4-stage internal evaluation pipeline.**
   - **Catalyst & FX Grounding:** Classify live news sentiment (`BULLISH`, `NEUTRAL`, `BEARISH_HEADWIND`, `BINARY_EVENT_RISK`) and retrieve `USD/SEK` (`allow_live_search=False` during historical point-in-time replays).
   - **Alpha Draft Proposal:** Propose a structured `TradeDecisionDraft` from 15m telemetry (`close`, `ema4`, `ema50`, `vwap`, `or_high`, `or_low`, `pd_low`, `pd_high`, `atr14`, `rsi14`, `rvol`, `efficiency_ratio`) and recent bar trajectory.
   - **Python Math Audit & Adversarial Critique:** Recompute fee-adjusted `python_verified_net_rr` (deducting `0.50%` USD FX drag on shares or `0.38%` of Mini intrinsic certificate equity `|spot - fin_lvl|`; mark `SELL_*` exits and underwater `HOLD` states as `rr_applicable=False`), reject bull/bear traps or catalyst divergence, and calibrate `confidence_score` (`0-100`).
   - **Done when:** `verdict`, `confidence_score`, `rr_applicable`, and `python_verified_net_rr` are populated.

3. **Enforce immutable Python circuit breakers.**
   - Apply `risk_profile.json` gates: `max_daily_trades` (`2`), `max_concurrent_leveraged_minis` (`1`), `daily_loss_lockout_sek` (`-900.0`), `cooldown_minutes_after_stopout` (`45`), US open blackout (`15:30-15:45 CET`), Nordic ETP cutoff (`20:30 CET`), sub-VWAP / above-VWAP short gate with bounded sweep exception, low trend-efficiency chop block, low relative-volume block, `min_net_rr_ratio` (`1.8`), structural stop floor, `max_risk_per_trade_sek` (`450.0`) risk-parity sizing, stateful `+0.85R -> +0.15R` breakeven ratchet, and symmetric `MINI L`/`MINI S` combined `EMA4 + VWAP` failure exit.
   - **Done when:** every active `BUY_*` decision passes all Python guardrails and sizes `quantity` to `<= 450 SEK` 1R risk.

4. **Format user-facing output in clean, verdict-gated plain English.**
   - Lead **bottom-line-first** (`Verdict -> 2-3 plain-English bullet reasons -> Verdict-gated Next Step`).
   - **Strictly forbid** raw LaTeX (`$\rightarrow$`, `$...$`), duplicate `🟢`/`🔴` status ticker lines, and internal jargon (`L1`, `L2`, `L3a`, `L3b`, `L4`, `ER`, `DSR`, `PSR`, `MinTRL`, `REVISED_BY_CRITIC`) in user-facing text.
   - **Verdict-gated Next Steps:**
     - On `HOLD`: **NEVER** print `/fill_mini` or entry order steps (only show updated `Triggerkurs` / `Säljkurs` if a breakeven ratchet fired on an open Mini).
     - On `SELL_SHARES`: show only `Sälj`, `Antal`, `Kurs (Limit)`, and `/close_shares <TICKER> <PRICE>` (no Hard Stop-Loss or Target lines).
     - On `BUY_MINI_LONG` / `BUY_MINI_SHORT`: provide `Triggerkurs`, `Säljkurs` (`0.20 SEK` below), and exact `/fill_mini <TICKER> <UNITS> <PRICE_SEK> <LONG|SHORT>` syntax.
   - **Done when:** the final message is jargon-free, valid Telegram HTML/plain text, and strictly matches the final verdict.
