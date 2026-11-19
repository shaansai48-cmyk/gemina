# Quantitative Trading Strategies, Capability Analysis & NVDA Sensitivity Benchmarks

All raw benchmark datasets are saved in:
* [subprojects/traderagent/data/benchmarks/nvda_2026_11_18_sensitivity.json](../subprojects/traderagent/data/benchmarks/nvda_2026_11_18_sensitivity.json)
* [subprojects/traderagent/data/benchmarks/strategy_capability_benchmark.json](../subprojects/traderagent/data/benchmarks/strategy_capability_benchmark.json)

Reproducible test runners:
* [subprojects/traderagent/tests/run_nvda_risk_sensitivity.py](../subprojects/traderagent/tests/run_nvda_risk_sensitivity.py)
* [subprojects/traderagent/tests/run_strategy_benchmark.py](../subprojects/traderagent/tests/run_strategy_benchmark.py)

---

## 1. Six Stock Trading Strategies to Test & Operate with `TraderAgent`

We evaluated 6 canonical quantitative stock/ETP trading strategies against the current `TraderAgent` architecture (`15m` Pine Script v6 telemetry + 4-Level Agentic Committee + Level-4 Python Guardrails):

| Strategy ID | Strategy Name & Mechanics | Current Setup Support Level | Benchmark Result (`run_strategy_benchmark.py`) | Why It Works / Architectural Analysis |
| :--- | :--- | :--- | :--- | :--- |
| **`STRAT_1`** | **15m Opening Range Breakout (ORB) + VWAP Momentum Continuation** | **100% Native Support** | `BUY_MINI_LONG` (`200u MINI L NVIDIA AVA 308`), Verified Net R:R **`1:2.76`**, Structural Stop `$149.32` (`31.40 / 31.20 SEK`) | Pine Script v6 tracks `or_high`, `or_low`, `vwap`, and `ema4`. Once the `15:30–15:45 CET` US Open blackout clears, a 15m close above `or_high` and `vwap` with `RSI14 > 55` passes all L3/L4 gates cleanly. |
| **`STRAT_2`** | **Previous Day Low (`pd_low`) Liquidity Sweep & EMA4 Reclaim (ICT Stop-Hunt Reversal)** | **100% Native Support** | `BUY_MINI_LONG` (`200u`), Verified Net R:R **`1:2.98`**, Structural Stop **`$149.32`** (`31.40 / 31.20 SEK`) | Specifically engineered into `guardrails.py`: even when price is below `VWAP` (`$151.35 < $152.00`), sweeping `pd_low` (`$149.65 <= $149.80 + 0.35*ATR`) and closing above `EMA4` (`$151.35 > $151.10`) unlocks the sub-VWAP exception while automatically anchoring the stop `0.20*ATR` below `pd_low`. |
| **`STRAT_3`** | **Intraday VWAP Trend Pullback with Automatic EMA4 Failure Trailing Exit** | **100% Native Support** | `HOLD` $\rightarrow$ Auto-Overridden to **`SELL_MINI_LONG`** (`200u`) when 15m close (`$150.70`) broke `< EMA4 - 0.15*ATR` (`$150.84`) | Combines trend entry above `VWAP` with an automated mechanical momentum exit (`ema4_failure_exit_atr_mult: 0.15`) that protects open Mini profits before price falls back to the hard stop. |
| **`STRAT_4`** | **Nordic OMX Zero-FX-Drag Breakout (`ERIC B` in SEK vs USD FX Drag)** | **100% Native Support** | `BUY_MINI_LONG` (`400u MINI L ERICSSON AVA`), Verified Net R:R **`1:2.27`**, Stop **`102.86 SEK`** (`20.86 / 20.66 SEK`) | Automatically detects `currency == "SEK"`, setting FX drag to `0.00%` (vs `0.50%` on USD stocks) and `fx_multiplier = 1.0`, making tighter intraday OMX Stockholm swings mathematically viable. |
| **`STRAT_5`** | **Pure Sub-VWAP Oversold RSI Knife-Catch (`RSI14 < 30` without `pd_low` Sweep)** | **Intentionally Vetoed on Leveraged Minis** | `BUY_MINI_LONG` $\rightarrow$ **`HOLD`** (`[VETO: SUB-VWAP]`) | Catching falling knives below `VWAP` in a leveraged financing-level product without a structural `pd_low` liquidity sweep + `EMA4` reclaim has poor expectancy. The setup intentionally vetoes this unless `require_close_above_vwap_for_mini` is set to `false`. |
| **`STRAT_6`** | **Unleveraged Core Share Dip Accumulation (`BUY_SHARES` Sub-VWAP)** | **100% Native Support** | `BUY_SHARES` (`5x NVDA @ $150.85`), Verified Net R:R **`1:4.73`**, Stop `$148.50`, Target `$164.50` | Because `require_close_above_vwap_for_mini` applies *only* to `BUY_MINI_LONG`, unleveraged `BUY_SHARES` accumulation is permitted below `VWAP` whenever fee-adjusted Net R:R `>= 1.8` (`4.73 >= 1.8`). |

---

## 2. Senior Architect Capability Gap Analysis (What Current Telemetry Supports vs. Future Extensions)

### What the Current Setup Excels At
1. **Long-Biased Momentum Breakouts (`ORB + VWAP`) & Structural Liquidity-Sweep Reversals (`pd_low + EMA4`)** on `15m` timeframes.
2. **Fee-Aware Nordic vs. US Execution:** Distinguishes `0.50%` USD FX round-trip drag on regular US shares vs. `0.38%` spread on SEK-denominated Avanza Mini Futures.
3. **Catalyst-Filtered Technicals:** Level-1 Google Search grounding prevents buying technical bounces into breaking bearish regulatory, earnings, or governance news.

### What Would Require Minor Schema / Pine Script Extensions if You Want to Test Additional Strategy Classes Later
1. **Short / Bear Strategies (`MINI S NVIDIA AVA`):**
   * *Current State:* `TradeActionType` supports `BUY_MINI_LONG` / `SELL_MINI_LONG` and `compute_quant_metrics` prices Mini Longs (`(spot - fin_lvl) * fx / parity`).
   * *To Unlock Bear Strategies:* Add `BUY_MINI_SHORT` / `SELL_MINI_SHORT`, add `pd_high` (Previous Day High) to the Pine Script v6 payload, and price Mini Shorts as `((fin_lvl - spot) * fx) / parity`.
2. **Volume-Confirmed Breakout Strategies (Relative Volume / `RVOL`):**
   * *Current State:* The Pine Script v6 JSON payload sends price, `ema4`, `vwap`, `or_high/low`, `pd_low`, `atr14`, and `rsi14`, without bar `volume` or `rvol` (`volume / ta.sma(volume, 20)`).
   * *To Unlock RVOL Strategies:* Add `"rvol": str.tostring(volume / ta.sma(volume, 20), "#.##")` to [tradingview_indicator.pine](../subprojects/traderagent/integrations/tradingview_indicator.pine) and a `min_rvol_for_breakout: 1.25` rule in `risk_profile.json`.
3. **Multi-Timeframe Trend Alignment (`1H` / `4H` EMA20 or Daily Trend Filter):**
   * *Current State:* `pd_low` is pulled from the Daily (`"D"`) timeframe via `request.security()`, while trend indicators (`ema4`, `vwap`, `rsi14`) are `15m`.
   * *To Unlock Multi-Timeframe Strategies:* Add `d_ema20` or `h1_vwap` via `request.security()` in Pine Script v6.

---

## 3. Saved 21-Scenario NVDA `risk_profile.json` Sensitivity Matrix (`2026-11-18`)

Tested against today's `NVDA` bar (`Close: $150.85`, `VWAP: $152.00`, `EMA4: $151.20`, `PD Low: $149.80`, `ATR14: $2.40`, `MINI L NVIDIA AVA 308` Fin `$118.415`, Parity `10`, `USD/SEK 10.16`):

| # | `risk_profile.json` Attribute | Value | Condition | Final Action | Underlying Stop | Mini Trigger / Sell (SEK) | Transformation / Guardrail Note |
| :- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 01 | `require_close_above_vwap_for_mini` | `true` | `$150.85 < $152.00 VWAP` | `HOLD` | `$149.75` | `31.83 / 31.63` | `[VETO: SUB-VWAP]` |
| 02 | `require_close_above_vwap_for_mini` | `false` | `$150.85 < $152.00 VWAP` | `BUY_MINI_LONG` | `$149.32` | `31.40 / 31.20` | Allowed; `[STOP FLOOR]` moved stop to `$149.32` |
| 03 | `pd_low_sweep_exception_atr_mult` | `0.15` | Low `$150.30` vs PD Low `$149.80`, Close `$151.35 > EMA4` | `HOLD` | `$149.75` | `31.83 / 31.63` | `$149.80 + 0.15*2.40 = $150.16 < $150.30` $\rightarrow$ `[VETO: SUB-VWAP]` |
| 04 | `pd_low_sweep_exception_atr_mult` | `0.35` | Low `$150.30` vs PD Low `$149.80`, Close `$151.35 > EMA4` | `BUY_MINI_LONG` | `$149.32` | `31.40 / 31.20` | `$149.80 + 0.35*2.40 = $150.64 >= $150.30` $\rightarrow$ Sweep Exception Passed |
| 05 | `stop_buffer_below_pd_low_atr_mult` | `0.10` | `PD Low $149.80 - 0.10*ATR` | `BUY_MINI_LONG` | `$149.56` | `31.64 / 31.44` | `[STOP FLOOR]` set `$0.24` below `pd_low` |
| 06 | `stop_buffer_below_pd_low_atr_mult` | `0.20` | `PD Low $149.80 - 0.20*ATR` | `BUY_MINI_LONG` | `$149.32` | `31.40 / 31.20` | `[STOP FLOOR]` set `$0.48` below `pd_low` |
| 07 | `stop_buffer_below_pd_low_atr_mult` | `0.50` | `PD Low $149.80 - 0.50*ATR` | `BUY_MINI_LONG` | `$148.60` | `30.67 / 30.47` | `[STOP FLOOR]` set `$1.20` below `pd_low` |
| 08 | `ema4_failure_exit_atr_mult` | `0.10` | Held `200u` Mini @ `$150.85` vs `EMA4 $151.20` | `SELL_MINI_LONG` | `$149.32` | `31.40 / 31.20` | `$150.85 < $150.96` $\rightarrow$ `[EMA4 FAILURE EXIT]` |
| 09 | `ema4_failure_exit_atr_mult` | `0.15` | Held `200u` Mini @ `$150.85` vs `EMA4 $151.20` | `HOLD` | `$149.32` | `31.40 / 31.20` | `$150.85 >= $150.84` (survived by `$0.01`) |
| 10 | `ema4_failure_exit_atr_mult` | `0.25` | Held `200u` Mini @ `$150.85` vs `EMA4 $151.20` | `HOLD` | `$149.32` | `31.40 / 31.20` | `$150.85 >= $150.60` |
| 11 | `min_net_rr_ratio` | `1.8` | Verified Net R:R `1:4.73` | `BUY_MINI_LONG` | `$149.32` | `31.40 / 31.20` | `4.73 >= 1.8` Passed |
| 12 | `min_net_rr_ratio` | `5.0` | Verified Net R:R `1:4.73` | `HOLD` | `$149.75` | `31.83 / 31.63` | `[VETO: LOW R:R] 4.73 < 5.0` |
| 13 | `avanza_us_fx_roundtrip_pct` | `0.00%` | Target `$155.50` / Stop `$148.50` @ `$150.85` | `R:R 1:1.98` | `$148.50` | `-` | Passes `1.8` R:R gate when `0%` FX fee |
| 14 | `avanza_us_fx_roundtrip_pct` | `0.50%` | Target `$155.50` / Stop `$148.50` @ `$150.85` | `R:R 1:1.43` | `$148.50` | `-` | `0.50%` USD FX fee drops R:R `< 1.8` |
| 15 | `avanza_us_fx_roundtrip_pct` | `2.50%` | Target `$155.50` / Stop `$148.50` @ `$150.85` | `R:R 1:0.21` | `$148.50` | `-` | High FX friction eliminates reward |
| 16–17 | `max_daily_trades` | `2` / `0` | `trades_executed_today = 0` | `BUY` / `HOLD` | `$149.32` | `31.40 / 31.20` | `[VETO: DAILY QUOTA]` at `0` |
| 18–19 | `daily_loss_lockout_sek` | `-900` / `-300` | `realized_pnl_today = -450 SEK` | `BUY` / `HOLD` | `$149.32` | `31.40 / 31.20` | `[VETO: LOSS LOCKOUT]` at `-300 SEK` |
| 20–21 | `cooldown_minutes_after_stopout` | `15m` / `45m` | Stop-out `25m ago` | `BUY` / `HOLD` | `$149.32` | `31.40 / 31.20` | `[VETO: COOLDOWN]` at `45m` (`19m` left) |
