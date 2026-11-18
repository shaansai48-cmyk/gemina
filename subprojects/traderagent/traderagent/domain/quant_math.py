"""
Pure Quantitative Pricing & Fee-Adjusted Risk/Reward Calculations (Domain Service).
"""
from typing import Any
from traderagent.domain.models import TradeDecisionDraft


def compute_quant_metrics(telemetry: dict, asset: dict, usd_sek: float = 10.16) -> dict[str, Any]:
    """Computes Avanza Mini Future theoretical SEK pricing, effective leverage, FX drag, and EMA4/VWAP deltas."""
    spot = float(telemetry["close"])
    fx_mult = float(telemetry.get("usd_sek") or usd_sek) if asset["currency"] == "USD" else 1.0
    fin_lvl = float(asset["mini_financing_level"])
    parity = float(asset["mini_parity"]) or 10.0

    mini_theo_sek = round(((spot - fin_lvl) * fx_mult) / parity, 2) if fin_lvl > 0 and spot > fin_lvl else 0.0
    mini_lev = round(spot / (spot - fin_lvl), 2) if fin_lvl > 0 and spot > fin_lvl else 0.0

    qty = asset["regular_shares_held"]
    avg = asset["blended_avg_price"]
    fx_drag_rate = 0.005 if asset["currency"] == "USD" else 0.0
    gross_pnl = (spot - avg) * qty
    fx_drag = ((spot + avg) * qty) * (fx_drag_rate / 2.0)
    net_shares_pnl_sek = round((gross_pnl - fx_drag) * fx_mult, 0) if qty > 0 else 0.0

    return {
        "currency": asset["currency"],
        "fx_multiplier": fx_mult,
        "mini_theoretical_sek": mini_theo_sek,
        "mini_effective_leverage": mini_lev,
        "regular_shares_net_unrealized_pnl_sek": net_shares_pnl_sek,
        "distance_to_ema4": round(spot - float(telemetry["ema4"]), 2),
        "distance_to_vwap": round(spot - float(telemetry["vwap"]), 2),
    }


def compute_verified_fee_adjusted_rr(
    draft: TradeDecisionDraft, telemetry: dict, asset: dict, risk: dict
) -> dict[str, Any]:
    """
    Deterministic Python Math Auditor (Level 3a):
    Recomputes exact net Risk/Reward after Avanza FX drag (0.50% USD) or Mini spread (0.38%),
    catching LLM arithmetic hallucinations before the Adversarial Critic runs.
    """
    spot = float(telemetry["close"])
    stop = float(draft.underlying_stop_price) if draft.underlying_stop_price > 0 else float(asset.get("hard_stop_price", spot * 0.98))
    target = float(draft.take_profit_target) if draft.take_profit_target > 0 else float(asset.get("take_profit_price", spot * 1.04))

    gross_reward = max(target - spot, 0.0)
    gross_risk = max(spot - stop, 0.01)

    if "MINI" in draft.action or "MINI" in draft.instrument.upper():
        spread_cost = spot * float(risk.get("avanza_mini_spread_pct", 0.0038))
        net_reward = max(gross_reward - spread_cost, 0.0)
        net_risk = gross_risk + (spread_cost * 0.5)
    else:
        fx_drag = spot * float(risk.get("avanza_us_fx_roundtrip_pct", 0.005)) if asset.get("currency") == "USD" else 0.0
        net_reward = max(gross_reward - fx_drag, 0.0)
        net_risk = gross_risk + (fx_drag * 0.5)

    verified_rr = round(net_reward / net_risk, 2) if net_risk > 0 else 0.0
    llm_rr = round(float(draft.net_risk_reward_ratio), 2)
    discrepancy = round(abs(llm_rr - verified_rr), 2)
    return {
        "spot": spot,
        "stop": stop,
        "target": target,
        "llm_claimed_rr": llm_rr,
        "python_verified_net_rr": verified_rr,
        "rr_discrepancy": discrepancy,
        "math_hallucination_detected": discrepancy > 0.35,
    }
