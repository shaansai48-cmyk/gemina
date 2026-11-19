# TraderAgent (`gemina-traderagent`) — Comprehensive Reference Guide

**TraderAgent** is the quantitative trading subproject of the **Gemina** Personal Multi-Agent Framework. It manages an Avanza ISK multi-ticker portfolio (`NVDA`, `ERIC B`, `MSFT`, etc.) using a declarative Markdown agent/skill architecture (`agents/*.md`, `skills/*.md`), a **4-Level Focused Agentic Committee** (`gemini-4-argon` + Google Search grounding with 16GB VRAM `qwen2.5-coder:14b` Ollama failover), and **deterministic Python circuit breakers**.

---

## 1. Complete `risk_profile.json` Reference (Immutable Circuit-Breaker Controls)

File location: [data/risk_profile.json](data/risk_profile.json)

The LLM receives `risk_profile.json` as read-only context, and Python enforces every threshold mechanically in [guardrails.py](traderagent/domain/guardrails.py) (`L4_HARD_PYTHON_GUARDRAILS`) and [quant_math.py](traderagent/domain/quant_math.py) (`L3A_PYTHON_MATH_AUDITOR`) after inference:

| Attribute | Default | Type | Mathematical Formula / Rule Enforced | Purpose & Sensitivity Impact |
| :--- | :--- | :--- | :--- | :--- |
| `max_daily_trades` | `2` | `int` | Blocks any `BUY_*` if `daily_stats.trades_executed_today >= max_daily_trades` (`[VETO: DAILY QUOTA]`). | Prevents overtrading and commission/spread churn. Setting to `0` puts the agent in strict monitor/exit-only mode. |
| `max_concurrent_leveraged_minis` | `1` | `int` | Maximum simultaneous leveraged Avanza Mini Future positions across the watchlist. | Caps total portfolio leverage exposure so two correlated tech stocks don't double-lever simultaneously. |
| `cooldown_minutes_after_stopout` | `45` | `int` | Blocks any `BUY_*` if `(now_epoch - last_stopout_epoch) / 60 < 45` (`[VETO: COOLDOWN]`). | Prevents revenge trading right after a stop-out (`pnl < 0` on `/close_mini`). Lowering to `15` allows faster re-entry after a shakeout. |
| `daily_loss_lockout_sek` | `-900.0` | `float` | Blocks any `BUY_*` if `realized_pnl_today_sek <= -900.0` (`[VETO: LOSS LOCKOUT]`). | Hard daily circuit breaker (~6% of tactical cash). Protects against drawdown spirals on high-volatility trend days. |
| `min_net_rr_ratio` | `1.8` | `float` | Blocks any `BUY_*` if fee-adjusted `net_risk_reward_ratio < 1.8` (`[VETO: LOW R:R]`). | Ensures positive expectancy after deducting Avanza FX/spread friction. Raising to `3.0+` filters for major swing setups only. |
| `require_close_above_vwap_for_mini` | `true` | `bool` | Blocks `BUY_MINI_LONG` when `close < vwap`, **unless** the `pd_low` sweep exception triggers (`[VETO: SUB-VWAP]`). | Prevents buying leveraged Minis in intraday downtrends below institutional volume-weighted average price (`VWAP`). |
| `pd_low_sweep_exception_atr_mult` | `0.35` | `float` | Allows `BUY_MINI_LONG` below `VWAP` **only if** `low <= pd_low + (0.35 * atr14)` **and** `close > ema4`. | Captures institutional liquidity sweeps (stop-hunts) near Previous Day Low (`pd_low`) once price reclaims `EMA4`. |
| `stop_buffer_below_pd_low_atr_mult` | `0.20` | `float` | Forces `underlying_stop_price <= round(pd_low - (0.20 * atr14), 2)` (`[STOP FLOOR]`) and recomputes Mini SEK stops. | Prevents placing stops right on or above `pd_low` where retail stop hunts occur. At `0.20` on NVDA (`ATR $2.40`), moves stop `$0.48` below `pd_low` (`$149.32`). |
| `ema4_failure_exit_atr_mult` | `0.15` | `float` | If `mini_units_held > 0` and `close < ema4 - (0.15 * atr14)`, overrides action to `SELL_MINI_LONG` (`[EMA4 FAILURE EXIT]`). | Automatic momentum failure exit for leveraged Minis before price drops all the way to the hard structural stop. |
| `avanza_us_fx_roundtrip_pct` | `0.005` | `float` | Deducts `0.50%` (`0.25%` buy + `0.25%` sell valutaväxling) from gross reward on `USD` regular shares in Level-3a Math Auditor. | Prevents illusionary USD scalps where Avanza's currency exchange fee eats 25–40% of the profit target. (`0.0%` for `SEK` stocks like `ERIC B`). |
| `avanza_mini_spread_pct` | `0.0038` | `float` | Deducts `0.38%` market-maker bid-ask spread in Level-3a Math Auditor for Mini Futures. | Models real Avanza market-maker spread friction (`0 SEK` courtage for orders `>= 1,000 SEK`). |
| `nordic_etp_cutoff_cet` | `"20:30"` | `str` | Blocks `BUY_MINI_LONG` when `current_time_cet >= "20:30"` (`[VETO: ETP CUTOFF]`). | Protects against widened spreads or illiquidity near the Nordic ETP close and late-session overnight gap risk. |
| `us_open_blackout_start_cet` / `end_cet` | `"15:30"` / `"15:45"` | `str` | Blocks any `BUY_*` during `15:30 <= hhmm < 15:45 CET` (`[VETO: OPENING BELL GATE]`). | Avoids the first 15 minutes of NYSE/NASDAQ cash open where violent bidirectional stop-hunt wicks occur. |

---

## 2. Complete `portfolio.json` Reference (Ground-Truth State Ledger)

File location: [data/portfolio.json](data/portfolio.json)

All writes to `portfolio.json` use atomic `tempfile` + `os.fsync()` + `os.replace()` in [persistence.py](traderagent/infrastructure/persistence.py) so a crash or power cut can never corrupt the file.

### Top-Level Fields
* **`account_id`** (`str`): Avanza account identifier (`"ISK 5872428"`).
* **`active_focus_ticker`** (`str`): The currently focused ticker (`"NVDA"`, `"ERIC B"`, `"MSFT"`). Can be switched live via Telegram (`/focus <TICKER>`) or CLI (`--focus <TICKER>`). The daemon processes incoming 15m webhook bars for `active_focus_ticker` **plus** any ticker where `regular_shares_held > 0` or `mini_units_held > 0`.
* **`daily_stats`** (`object`):
  * `date` (`YYYY-MM-DD`): Current trading date in `Europe/Stockholm` (`CET`). Automatically rolls over at midnight CET (`ensure_daily_rollover()`), resetting `trades_executed_today` and `realized_pnl_today_sek` to `0`.
  * `trades_executed_today` (`int`): Incremented by `/fill_mini`. Checked against `max_daily_trades`.
  * `realized_pnl_today_sek` (`float`): Updated by `/close_mini`. Checked against `daily_loss_lockout_sek`.
  * `last_stopout_epoch` (`int`): Unix timestamp recorded whenever `/close_mini` realizes a negative PnL (`< 0 SEK`). Checked against `cooldown_minutes_after_stopout`.
* **`last_seen_ts_by_ticker`** (`dict[str, str]`): Per-ticker bar timestamp deduplication map (`{"NVDA": "2026-11-18T22:45:00Z"}`). Ensures simultaneous `:00, :15, :30, :45` candle closes across multiple stocks never collide and daemon restarts never re-fire old alerts.
* **`cash_reserves`** (`object`):
  * `available_tactical_mini_sek` (`float`): Liquid SEK available for tactical Mini Future entries (`14548.0 SEK`). Automatically debited on `/fill_mini` and credited on `/close_mini`.
  * `pending_fund_sale_sek` & `pending_fund_settlement_date`: Tracking for settling fund redemptions.

### Per-Ticker `watchlist.<TICKER>` Fields & Avanza Mini Future Pricing Formulas
* **`exchange` & `currency`**: e.g., `"NASDAQ"` / `"USD"` or `"OMXSTO"` / `"SEK"`.
* **`regular_shares_held` & `blended_avg_price`**: Core unleveraged share holdings (e.g., `16 NVDA` @ `$156.73`).
* **`hard_stop_price` & `take_profit_price`**: Underlying stock price stop and target levels (updated via `/set_stop <STOP> [TP]`).
* **`mini_instrument`**: Avanza Mini Future name (e.g., `"MINI L NVIDIA AVA 308"`).
* **`mini_financing_level` ($F$), `mini_barrier` ($B$), `mini_parity` ($P$)**:
  In [quant_math.py](traderagent/domain/quant_math.py) (`compute_quant_metrics()`), given underlying spot price $S$ and FX multiplier $\text{FX}$ (`USD/SEK` for USD stocks, `1.0` for SEK stocks):
  $$\text{Mini Theoretical Price (SEK)} = \frac{(S - F) \times \text{FX}}{P}$$
  $$\text{Effective Leverage} = \frac{S}{S - F}$$
  $$\text{Avanza Stop Triggerkurs (SEK)} = \frac{(\text{Stop}_{\text{underlying}} - F) \times \text{FX}}{P}$$
  $$\text{Avanza Stop Säljkurs (SEK)} = \text{Triggerkurs} - 0.20\text{ SEK}$$
* **`mini_units_held` & `mini_avg_sek`**: Active Mini Future position logged via `/fill_mini` and `/close_mini`.

---

## 3. Agent Functions & 4-Level Cognitive Pipeline

Every incoming 15m bar or Telegram message flows through a 10-stage observable pipeline tracked by `PipelineTracer` ([tracing.py](../../gemina/core/tracing.py)):

1. **Declarative Persona & Skill Loader ([src/loader.py](../../src/loader.py)):**
   Parses YAML frontmatter and Markdown `# Goal`, `# Backstory`, `# Instructions` from [agents/main_agent.md](../../agents/main_agent.md) and [agents/subagent.md](../../agents/subagent.md), dynamically injecting [skills/gemini_interaction.md](../../skills/gemini_interaction.md), [skills/avanza_quant_committee.md](../../skills/avanza_quant_committee.md), and [skills/system_diagnostics.md](../../skills/system_diagnostics.md).
2. **Level-1 Catalyst & Macro Agent (`QuantAgentCommittee.run_l1_catalyst_agent`):**
   Queries live Google Search via `gemini-4-argon` to classify breaking news into `BULLISH`, `NEUTRAL`, `BEARISH_HEADWIND`, or `BINARY_EVENT_RISK` and fetch live `USD/SEK`.
3. **Level-2 Alpha Strategist (`QuantAgentCommittee.run_l2_alpha_strategist`):**
   Evaluates 15m price microstructure (`close`, `ema4`, `vwap`, `or_high`, `or_low`, `pd_low`, `atr14`, `rsi14`) + last 4 bars of trajectory history to output a `TradeDecisionDraft`.
4. **Level-3a Deterministic Python Math Auditor (`compute_verified_fee_adjusted_rr`):**
   Recomputes exact net Risk/Reward after Avanza `0.50%` USD FX drag or `0.38%` Mini spread in `0.02 ms`, flagging `math_hallucination_detected = True` if Level-2's claimed R:R deviates by `> 0.35`.
5. **Level-3b Adversarial Red-Team Critic (`QuantAgentCommittee.run_l3_adversarial_critic`):**
   Audits Level-2's draft against Level-1's catalyst report and Level-3a's verified math, outputting a calibrated `confidence_score` (`0–100`), `verdict` (`APPROVED`, `REVISED_BY_CRITIC`, `VETOED_BY_CRITIC`), and refined decision.
6. **Level-4 Hard Python Guardrails (`enforce_hard_quant_guardrails`):**
   Mechanically enforces all 7 circuit-breaker rules from `risk_profile.json`.
7. **Traffic-Light Status Evaluator (`StatusEvaluator.evaluate`):**
   Executes whitelisted Linux/GPU commands (`nvidia-smi`, `systemctl`, RAM, Disk) and portfolio risk checks. Outputs a 1-line compact `🟢 GREEN` status ticker when healthy, and auto-expands with full diagnostic/risk details **only** when status is `🔴 RED`.

---

## 4. Stock Trading Strategies & Current Capability Analysis

See [docs/STRATEGIES_AND_BENCHMARKS.md](../../docs/STRATEGIES_AND_BENCHMARKS.md) and [data/benchmarks/strategy_capability_benchmark.json](data/benchmarks/strategy_capability_benchmark.json) for full benchmark outputs across 6 quantitative trading strategies.
