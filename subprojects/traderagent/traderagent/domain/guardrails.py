"""
Level-4 Immutable Python Quant Circuit Breakers (Domain Policy Engine).
Enforces all 7 hard trading rules deterministically after multi-agent LLM inference.
"""
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from gemina.core.tracing import PipelineTracer
from traderagent.domain.models import TradeDecision

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
    ema4 = float(telemetry["ema4"])
    vwap = float(telemetry["vwap"])
    atr14 = float(telemetry["atr14"])
    pd_low = float(telemetry["pd_low"])
    fin_lvl = float(asset["mini_financing_level"])
    parity = float(asset["mini_parity"]) or 10.0

    def _finish_guardrails() -> TradeDecision:
        if mutations and decision.action == "HOLD" and before_snapshot["action"] != "HOLD":
            decision.critic_verdict = "VETOED_BY_GUARDRAIL"
        after_snapshot = {
            "action": decision.action,
            "quantity": decision.quantity,
            "underlying_stop_price": decision.underlying_stop_price,
            "avanza_stop_trigger_sek": decision.avanza_stop_trigger_sek,
            "avanza_stop_sell_sek": decision.avanza_stop_sell_sek,
            "confidence_score": decision.confidence_score,
        }
        if tracer:
            tracer.add_stage(
                stage="L4_HARD_PYTHON_GUARDRAILS",
                status="MUTATED" if mutations else "PASSED_UNCHANGED",
                input_data=before_snapshot,
                transformation="; ".join(mutations) if mutations else "All 6 Python quant circuit-breakers passed without mutation",
                output_data=after_snapshot,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return decision

    # 1. Daily Quota, Loss Lockout & Stop-Out Cooldown
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

    # 2. Time-of-Day Blackout Windows
    if decision.action.startswith("BUY") and risk["us_open_blackout_start_cet"] <= hhmm < risk["us_open_blackout_end_cet"]:
        mutations.append(f"VETO OPENING BELL GATE ({decision.action} -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = "[VETO: OPENING BELL GATE] No entries during 15:30–15:45 CET stop-hunt window."
        return _finish_guardrails()

    if decision.action == "BUY_MINI_LONG" and hhmm >= risk["nordic_etp_cutoff_cet"]:
        mutations.append("VETO ETP CUTOFF (BUY_MINI_LONG -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = f"[VETO: ETP CUTOFF] Past {risk['nordic_etp_cutoff_cet']} CET; no new leveraged Mini entries."
        return _finish_guardrails()

    # 3. Hard VWAP Gate on Leveraged Mini Longs
    if decision.action == "BUY_MINI_LONG" and risk["require_close_above_vwap_for_mini"] and close < vwap:
        near_pd_low_sweep = (low <= (pd_low + risk["pd_low_sweep_exception_atr_mult"] * atr14)) and (close > ema4)
        if not near_pd_low_sweep:
            mutations.append(f"VETO SUB-VWAP ({close} < {vwap}, BUY_MINI_LONG -> HOLD)")
            decision.action, decision.quantity = "HOLD", 0
            decision.rationale = f"[VETO: SUB-VWAP] Close ({close}) < 15m VWAP ({vwap}) without pd_low ({pd_low}) sweep."
            return _finish_guardrails()

    # 4. Minimum Net Risk/Reward Gate
    if decision.action.startswith("BUY") and decision.net_risk_reward_ratio < risk["min_net_rr_ratio"]:
        mutations.append(f"VETO LOW R:R ({decision.net_risk_reward_ratio} < {risk['min_net_rr_ratio']}, {decision.action} -> HOLD)")
        decision.action, decision.quantity = "HOLD", 0
        decision.rationale = f"[VETO: LOW R:R] Net R:R (1:{decision.net_risk_reward_ratio}) < required 1:{risk['min_net_rr_ratio']}."
        return _finish_guardrails()

    # 5. Structural Stop-Loss Floor Below pd_low (Prevents Liquidity Sweep Stop-Outs)
    if (decision.action == "BUY_MINI_LONG" or asset["mini_units_held"] > 0) and fin_lvl > 0:
        structural_stop = round(pd_low - (risk["stop_buffer_below_pd_low_atr_mult"] * atr14), 2)
        if decision.underlying_stop_price > structural_stop:
            old_stop = decision.underlying_stop_price
            decision.underlying_stop_price = structural_stop
            trig_sek = round(((structural_stop - fin_lvl) * fx_mult) / parity, 2)
            decision.avanza_stop_trigger_sek = trig_sek
            decision.avanza_stop_sell_sek = round(trig_sek - 0.20, 2)
            mutations.append(f"STOP FLOOR ADJUSTED ({old_stop} -> {structural_stop}, Trigger {trig_sek} SEK)")
            decision.rationale += (
                f" | [STOP FLOOR] Moved stop below pd_low ({pd_low}) to {structural_stop} "
                f"(Trigger {trig_sek} / Sell {decision.avanza_stop_sell_sek} SEK)."
            )

    # 6. Auto-Exit Active Mini Long on EMA4 Failure
    if asset["mini_units_held"] > 0 and close < (ema4 - risk["ema4_failure_exit_atr_mult"] * atr14):
        mutations.append(f"EMA4 FAILURE OVERRIDE ({decision.action} -> SELL_MINI_LONG)")
        decision.action = "SELL_MINI_LONG"
        decision.instrument = asset["mini_instrument"]
        decision.quantity = asset["mini_units_held"]
        decision.rationale = (
            f"[EMA4 FAILURE EXIT] 15m close ({close}) broke below EMA4 ({ema4}). "
            f"Exit leveraged Mini immediately."
        )

    return _finish_guardrails()
