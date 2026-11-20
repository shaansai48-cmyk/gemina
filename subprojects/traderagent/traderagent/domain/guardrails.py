"""
Level-4 Immutable Python Quant Circuit Breakers, Concurrent Mini Limit, Efficiency-Ratio Regime Gate,
Deterministic Risk-Parity Position Sizer, Stateful +0.85R Breakeven Ratchet,
Symmetric MINI L/S EMA4 Exit, and Avanza Order Guide Attach (Domain Policy Engine).
"""
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from gemina.core.tracing import PipelineTracer
from traderagent.domain.models import TradeDecision
from traderagent.domain.quant_math import (
    build_plain_english_reasons,
    compute_quant_metrics,
    format_avanza_order_instructions,
)

CET = ZoneInfo("Europe/Stockholm")


def enforce_hard_quant_guardrails(
    decision: TradeDecision,
    telemetry: dict,
    portfolio: dict,
    asset: dict,
    risk: dict,
    fx_mult: float,
    tracer: Optional[PipelineTracer] = None,
) -> TradeDecision:
    t0 = time.perf_counter()
    before_snapshot = {
        "action": decision.action,
        "quantity": decision.quantity,
        "underlying_stop_price": decision.underlying_stop_price,
        "avanza_stop_trigger_sek": decision.avanza_stop_trigger_sek,
        "avanza_stop_sell_sek": decision.avanza_stop_sell_sek,
        "confidence_score": decision.confidence_score,
    }
    mutations: list[str] = []

    now_cet = datetime.now(CET)
    hhmm = now_cet.strftime("%H:%M")
    close = float(telemetry["close"])
    low = float(telemetry["low"])
    high = float(telemetry.get("high", close))
    ema4 = float(telemetry["ema4"])
    vwap = float(telemetry["vwap"])
    atr14 = float(telemetry["atr14"])
    pd_low = float(telemetry["pd_low"])
    pd_high = float(telemetry.get("pd_high", max(high, float(telemetry.get("or_high", close)))))
    rvol = float(telemetry.get("rvol", 1.10))
    fin_lvl = float(asset["mini_financing_level"])
    parity = float(asset["mini_parity"]) or 10.0
    metrics = compute_quant_metrics(telemetry, asset, usd_sek=fx_mult)

    held_minis = int(asset.get("mini_units_held", 0))
    is_held_short = held_minis > 0 and (
        str(asset.get("mini_side", "")).upper() == "SHORT"
        or "MINI S" in str(asset.get("mini_instrument", "")).upper()
    )

    def _finish_guardrails() -> TradeDecision:
        if mutations and decision.action == "HOLD" and before_snapshot["action"] != "HOLD":
            decision.critic_verdict = "VETOED_BY_GUARDRAIL"

        if decision.action.startswith("SELL"):
            decision.rr_applicable = False
            decision.net_risk_reward_ratio = 0.0

        # Always attach step-by-step Avanza order placement guide when an active order is recommended
        decision.avanza_execution_guide = format_avanza_order_instructions(
            decision=decision,
            telemetry=telemetry,
            asset=asset,
            metrics=metrics,
            risk=risk,
            account_id=portfolio.get("account_id", "ISK 5872428"),
        )
        decision.plain_reasons = build_plain_english_reasons(
            decision=decision,
            telemetry=telemetry,
            asset=asset,
            metrics=metrics,
            risk=risk,
        )

        after_snapshot = {
            "action": decision.action,
            "quantity": decision.quantity,
            "underlying_stop_price": decision.underlying_stop_price,
            "avanza_stop_trigger_sek": decision.avanza_stop_trigger_sek,
            "avanza_stop_sell_sek": decision.avanza_stop_sell_sek,
            "confidence_score": decision.confidence_score,
            "ratchet_triggered": decision.ratchet_triggered,
            "rr_applicable": decision.rr_applicable,
            "has_avanza_guide": bool(decision.avanza_execution_guide),
        }
        if tracer:
            tracer.add_stage(
                stage="L4_HARD_PYTHON_GUARDRAILS",
                status="MUTATED" if mutations else "PASSED_UNCHANGED",
                input_data=before_snapshot,
                transformation="; ".join(mutations) if mutations else "All Python quant circuit-breakers passed",
                output_data=after_snapshot,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return decision

    # 1. Daily Quota, Max Concurrent Minis, Loss Lockout & Stop-Out Cooldown
    stats = portfolio["daily_stats"]
    if decision.action.startswith("BUY"):
        if stats["trades_executed_today"] >= risk["max_daily_trades"]:
            mutations.append(f"VETO DAILY QUOTA ({decision.action} -> HOLD)")
            decision.action, decision.quantity = "HOLD", 0
            decision.rationale = f"[VETO: DAILY QUOTA] Max {risk['max_daily_trades']} trades/day reached."
            return _finish_guardrails()
        if stats["realized_pnl_today_sek"] <= risk["daily_loss_lockout_sek"]:
            mutations.append(f"VETO LOSS LOCKOUT ({decision.action} -> HOLD)")
            decision.action, decision.quantity = "HOLD", 0
            decision.rationale = f"[VETO: LOSS LOCKOUT] Daily realized PnL ({stats['realized_pnl_today_sek']} SEK) hit limit."
            return _finish_guardrails()
        mins_since_stop = (time.time() - stats["last_stopout_epoch"]) / 60.0
        if mins_since_stop < risk["cooldown_minutes_after_stopout"]:
            mutations.append(f"VETO COOLDOWN ({decision.action} -> HOLD)")
            decision.action, decision.quantity = "HOLD", 0
            decision.rationale = f"[VETO: COOLDOWN] {int(risk['cooldown_minutes_after_stopout'] - mins_since_stop)}m left after stop-out."
            return _finish_guardrails()

    if decision.action in ("BUY_MINI_LONG", "BUY_MINI_SHORT"):
        max_minis = int(risk.get("max_concurrent_leveraged_minis", 1))
        other_active_minis = sum(
            1
            for sym, w_asset in portfolio.get("watchlist", {}).items()
            if sym.upper() != decision.ticker.upper() and int(w_asset.get("mini_units_held", 0)) > 0
        )
        if other_active_minis >= max_minis:
            mutations.append(f"VETO MAX CONCURRENT MINIS ({other_active_minis} >= {max_minis})")
            decision.action, decision.quantity = "HOLD", 0
            decision.rationale = (
                f"[VETO: MAX CONCURRENT MINIS] Already holding {other_active_minis} active leveraged Mini "
                f"(limit: {max_minis})."
            )
            return _finish_guardrails()

    # 2. Time-of-Day Blackout Windows
    if decision.action.startswith("BUY") and risk["us_open_blackout_start_cet"] <= hhmm < risk["us_open_blackout_end_cet"]:
        mutations.append(f"VETO OPENING BELL GATE ({decision.action} -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = "[VETO: OPENING BELL GATE] No entries during 15:30–15:45 CET stop-hunt window."
        return _finish_guardrails()

    if decision.action in ("BUY_MINI_LONG", "BUY_MINI_SHORT") and hhmm >= risk["nordic_etp_cutoff_cet"]:
        mutations.append(f"VETO ETP CUTOFF ({decision.action} -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = f"[VETO: ETP CUTOFF] Past {risk['nordic_etp_cutoff_cet']} CET; no new leveraged Mini entries."
        return _finish_guardrails()

    # 3. Hard VWAP Gate, Efficiency-Ratio Regime Gate, and Scoped RVOL Breakout Gate
    is_pd_low_sweep = False
    is_pd_high_sweep = False

    if decision.action == "BUY_MINI_LONG" and risk["require_close_above_vwap_for_mini"] and close < vwap:
        sweep_floor = pd_low - (1.0 * atr14)
        sweep_ceiling = pd_low + (risk["pd_low_sweep_exception_atr_mult"] * atr14)
        is_pd_low_sweep = (sweep_floor <= low <= sweep_ceiling) and (close > ema4)
        if not is_pd_low_sweep:
            mutations.append(f"VETO SUB-VWAP ({close} < {vwap}, BUY_MINI_LONG -> HOLD)")
            decision.action, decision.quantity = "HOLD", 0
            decision.rationale = f"[VETO: SUB-VWAP] Close ({close}) < 15m VWAP ({vwap}) without pd_low ({pd_low}) sweep."
            return _finish_guardrails()

    if decision.action == "BUY_MINI_SHORT" and risk["require_close_above_vwap_for_mini"] and close > vwap:
        sweep_floor = pd_high - (risk["pd_low_sweep_exception_atr_mult"] * atr14)
        sweep_ceiling = pd_high + (1.0 * atr14)
        is_pd_high_sweep = (sweep_floor <= high <= sweep_ceiling) and (close < ema4)
        if not is_pd_high_sweep:
            mutations.append(f"VETO ABOVE-VWAP SHORT ({close} > {vwap}, BUY_MINI_SHORT -> HOLD)")
            decision.action, decision.quantity = "HOLD", 0
            decision.rationale = f"[VETO: ABOVE-VWAP SHORT] Close ({close}) > 15m VWAP ({vwap}) without pd_high ({pd_high}) rejection."
            return _finish_guardrails()

    is_sweep_entry = is_pd_low_sweep or is_pd_high_sweep
    min_er = float(risk.get("min_trend_efficiency_ratio", 0.28))
    if (
        "efficiency_ratio" in telemetry
        and decision.action in ("BUY_MINI_LONG", "BUY_MINI_SHORT")
        and decision.market_regime == "Momentum_Breakout"
        and not is_sweep_entry
        and float(telemetry["efficiency_ratio"]) < min_er
    ):
        er_val = float(telemetry["efficiency_ratio"])
        mutations.append(f"VETO LOW ER CHOP (ER={er_val} < {min_er}, {decision.action} -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = (
            f"[VETO: LOW ER CHOP] 5-day Efficiency Ratio ({er_val}) < {min_er}; "
            f"Momentum Breakouts blocked in Mean-Reverting regime."
        )
        return _finish_guardrails()

    # Enforce min_rvol_for_breakout on breakouts (not on quiet liquidity sweep reversals)
    min_rvol = float(risk.get("min_rvol_for_breakout", 1.05))
    if (
        "rvol" in telemetry
        and decision.action in ("BUY_MINI_LONG", "BUY_MINI_SHORT")
        and not is_sweep_entry
        and rvol < min_rvol
    ):
        mutations.append(f"VETO LOW RVOL ({rvol} < {min_rvol}, {decision.action} -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = f"[VETO: LOW RVOL] Relative Volume ({rvol}x) < required {min_rvol}x for breakout confirmation."
        return _finish_guardrails()

    # 4. Minimum Net Risk/Reward Gate
    if decision.action.startswith("BUY") and decision.net_risk_reward_ratio < risk["min_net_rr_ratio"]:
        mutations.append(f"VETO LOW R:R ({decision.net_risk_reward_ratio} < {risk['min_net_rr_ratio']}, {decision.action} -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = f"[VETO: LOW R:R] Net R:R (1:{decision.net_risk_reward_ratio}) < required 1:{risk['min_net_rr_ratio']}."
        return _finish_guardrails()

    # 5. Structural Stop-Loss Floor, +0.85R Breakeven Ratchet & Deterministic Risk-Parity Sizing (Max 450 SEK 1R risk)
    max_risk_sek = float(risk.get("max_risk_per_trade_sek", 450.0))
    avail_cash_sek = float(portfolio.get("cash_reserves", {}).get("available_tactical_mini_sek", 14548.0))
    ratchet_r = float(risk.get("breakeven_ratchet_r_mult", 0.85))
    lock_r = float(risk.get("breakeven_lock_r_mult", 0.15))

    if (decision.action == "BUY_MINI_LONG" or (held_minis > 0 and not is_held_short)) and fin_lvl > 0:
        anchor_support = pd_low if (0 <= (close - pd_low) <= 2.5 * atr14) else min(vwap, low)
        structural_stop = round(min(anchor_support, low) - (risk["stop_buffer_below_pd_low_atr_mult"] * atr14), 2)
        if decision.underlying_stop_price > structural_stop:
            old_stop = decision.underlying_stop_price
            decision.underlying_stop_price = structural_stop
            trig_sek = round(((structural_stop - fin_lvl) * fx_mult) / parity, 2)
            decision.avanza_stop_trigger_sek = trig_sek
            decision.avanza_stop_sell_sek = round(trig_sek - 0.20, 2)
            mutations.append(f"STOP FLOOR ADJUSTED ({old_stop} -> {structural_stop}, Trigger {trig_sek} SEK)")
            decision.rationale += (
                f" | [STOP FLOOR] Moved stop below support ({anchor_support}) to {structural_stop} "
                f"(Trigger {trig_sek} / Sell {decision.avanza_stop_sell_sek} SEK)."
            )

        # Stateful +0.85R Breakeven Stop Ratchet for open Mini Long
        if held_minis > 0:
            entry_u = float(asset.get("mini_entry_underlying_price", 0.0))
            risk_1r_u = float(asset.get("mini_initial_risk_r", 0.0))
            if entry_u > 0 and risk_1r_u > 0 and max(high, close) >= entry_u + (ratchet_r * risk_1r_u):
                ratchet_stop = round(entry_u + (lock_r * risk_1r_u), 2)
                if decision.underlying_stop_price < ratchet_stop:
                    decision.underlying_stop_price = ratchet_stop
                    trig_sek = round(((ratchet_stop - fin_lvl) * fx_mult) / parity, 2)
                    decision.avanza_stop_trigger_sek = trig_sek
                    decision.avanza_stop_sell_sek = round(trig_sek - 0.20, 2)
                    decision.ratchet_triggered = True
                    mutations.append(f"BREAKEVEN RATCHET +0.15R (Stop -> {ratchet_stop}, Trigger {trig_sek} SEK)")
                    decision.rationale += (
                        f" | [BREAKEVEN RATCHET +{lock_r}R] Reached +{ratchet_r}R; raise Avanza Stop to "
                        f"Trigger {trig_sek} / Sell {decision.avanza_stop_sell_sek} SEK."
                    )

        if decision.action == "BUY_MINI_LONG":
            entry_sek = float(decision.limit_price or metrics["mini_theoretical_sek"])
            decision.limit_price = entry_sek
            if not decision.avanza_stop_trigger_sek:
                trig_sek = round(((decision.underlying_stop_price - fin_lvl) * fx_mult) / parity, 2)
                decision.avanza_stop_trigger_sek = trig_sek
                decision.avanza_stop_sell_sek = round(trig_sek - 0.20, 2)
            per_unit_risk_sek = max(entry_sek - float(decision.avanza_stop_sell_sek), 0.25)
            risk_sized_qty = max(1, int(max_risk_sek / per_unit_risk_sek))
            cash_cap_qty = max(1, int((avail_cash_sek * 0.95) / max(entry_sek, 1.0)))
            optimal_qty = min(risk_sized_qty, cash_cap_qty)
            if decision.quantity <= 0 or decision.quantity > optimal_qty * 1.25:
                mutations.append(f"RISK-PARITY SIZED ({decision.quantity} -> {optimal_qty}u for {max_risk_sek} SEK 1R risk)")
                decision.quantity = optimal_qty

    if decision.action == "BUY_MINI_SHORT" or (held_minis > 0 and is_held_short):
        s_fin = float(metrics["mini_short_financing_level"])
        anchor_res = pd_high if (0 <= (pd_high - close) <= 2.5 * atr14) else max(vwap, high)
        structural_stop = round(max(anchor_res, high) + (risk["stop_buffer_below_pd_low_atr_mult"] * atr14), 2)
        if decision.underlying_stop_price < structural_stop:
            decision.underlying_stop_price = structural_stop

        if held_minis > 0:
            entry_u = float(asset.get("mini_entry_underlying_price", 0.0))
            risk_1r_u = float(asset.get("mini_initial_risk_r", 0.0))
            if entry_u > 0 and risk_1r_u > 0 and min(low, close) <= entry_u - (ratchet_r * risk_1r_u):
                ratchet_stop = round(entry_u - (lock_r * risk_1r_u), 2)
                if decision.underlying_stop_price > ratchet_stop:
                    decision.underlying_stop_price = ratchet_stop
                    trig_sek = round(((s_fin - ratchet_stop) * fx_mult) / parity, 2)
                    decision.avanza_stop_trigger_sek = trig_sek
                    decision.avanza_stop_sell_sek = round(trig_sek - 0.20, 2)
                    decision.ratchet_triggered = True
                    mutations.append(f"SHORT BREAKEVEN RATCHET +0.15R (Stop -> {ratchet_stop}, Trigger {trig_sek} SEK)")

        if decision.action == "BUY_MINI_SHORT":
            entry_sek = float(decision.limit_price or metrics["mini_short_theoretical_sek"])
            decision.limit_price = entry_sek
            trig_sek = round(((s_fin - decision.underlying_stop_price) * fx_mult) / parity, 2)
            decision.avanza_stop_trigger_sek = trig_sek
            decision.avanza_stop_sell_sek = round(trig_sek - 0.20, 2)
            per_unit_risk_sek = max(entry_sek - float(decision.avanza_stop_sell_sek), 0.25)
            optimal_qty = min(
                max(1, int(max_risk_sek / per_unit_risk_sek)),
                max(1, int((avail_cash_sek * 0.95) / max(entry_sek, 1.0))),
            )
            decision.quantity = optimal_qty
            if asset.get("mini_short_instrument"):
                decision.instrument = asset["mini_short_instrument"]
            elif "MINI L" in decision.instrument.upper():
                decision.instrument = decision.instrument.upper().replace("MINI L", "MINI S")

    # 6. Symmetric Auto-Exit on Active Mini Position (MINI L vs MINI S) on Combined EMA4 + VWAP Failure
    # Empirically validated over 60d (15m): requiring VWAP confirmation prevents premature chop on healthy pullbacks above VWAP.
    ema4_mult = float(risk["ema4_failure_exit_atr_mult"])
    if held_minis > 0:
        if not is_held_short and close < (ema4 - ema4_mult * atr14) and close < vwap:
            mutations.append(f"EMA4+VWAP FAILURE OVERRIDE ({decision.action} -> SELL_MINI_LONG)")
            decision.action = "SELL_MINI_LONG"
            decision.instrument = asset["mini_instrument"]
            decision.quantity = held_minis
            decision.rr_applicable = False
            decision.net_risk_reward_ratio = 0.0
            decision.rationale = (
                f"[EMA4 FAILURE EXIT] 15m close ({close}) broke below both EMA4 ({ema4}) and VWAP ({vwap}). "
                f"Exit leveraged Mini Long immediately."
            )
        elif is_held_short and close > (ema4 + ema4_mult * atr14) and close > vwap:
            mutations.append(f"SHORT EMA4+VWAP FAILURE OVERRIDE ({decision.action} -> SELL_MINI_SHORT)")
            decision.action = "SELL_MINI_SHORT"
            decision.instrument = asset.get("mini_short_instrument") or asset["mini_instrument"].replace("MINI L", "MINI S")
            decision.quantity = held_minis
            decision.rr_applicable = False
            decision.net_risk_reward_ratio = 0.0
            decision.rationale = (
                f"[EMA4 FAILURE EXIT] 15m close ({close}) broke above both EMA4 ({ema4}) and VWAP ({vwap}). "
                f"Exit leveraged Mini Short immediately."
            )

    return _finish_guardrails()
