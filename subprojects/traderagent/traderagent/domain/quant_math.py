"""
Pure Quantitative Pricing, Regime Classification, Fee-Adjusted R:R Math Auditor,
and Step-by-Step Avanza Order Placement Formatter (Domain Service).
"""
from typing import Any
from traderagent.domain.models import TradeDecision, TradeDecisionDraft


def compute_quant_metrics(telemetry: dict, asset: dict, usd_sek: float = 10.16) -> dict[str, Any]:
    """
    Computes Avanza Mini Future (MINI L & MINI S) theoretical SEK pricing, effective leverage,
    Opening-Range-adjusted effective volatility (`eff_vol`), Kaufman Efficiency Ratio regime classification,
    and Relative Volume (`rvol`).
    """
    spot = float(telemetry["close"])
    fx_mult = float(telemetry.get("usd_sek") or usd_sek) if asset["currency"] == "USD" else 1.0
    fin_lvl = float(asset["mini_financing_level"])
    parity = float(asset["mini_parity"]) or 10.0

    # Mini Long theoretical price & leverage
    mini_theo_sek = round(((spot - fin_lvl) * fx_mult) / parity, 2) if fin_lvl > 0 and spot > fin_lvl else 0.0
    mini_lev = round(spot / (spot - fin_lvl), 2) if fin_lvl > 0 and spot > fin_lvl else 0.0

    # Synthetic Mini Short financing level (~1.22 * spot for ~4.5x leverage if not explicitly configured)
    short_fin_lvl = float(asset.get("mini_short_financing_level") or round(spot * 1.22, 2))
    mini_short_theo_sek = (
        round(((short_fin_lvl - spot) * fx_mult) / parity, 2) if short_fin_lvl > spot else 0.0
    )

    qty = asset["regular_shares_held"]
    avg = asset["blended_avg_price"]
    fx_drag_rate = 0.005 if asset["currency"] == "USD" else 0.0
    gross_pnl = (spot - avg) * qty
    fx_drag = ((spot + avg) * qty) * (fx_drag_rate / 2.0)
    net_shares_pnl_sek = round((gross_pnl - fx_drag) * fx_mult, 0) if qty > 0 else 0.0

    or_high = float(telemetry.get("or_high", spot))
    or_low = float(telemetry.get("or_low", spot))
    or_range = round(max(or_high - or_low, 0.0), 2)
    atr14 = float(telemetry.get("atr14", 1.0))
    eff_vol = round(max(atr14, 0.45 * or_range), 2)
    ema50 = float(telemetry.get("ema50", telemetry.get("vwap", spot)))
    rvol = round(float(telemetry.get("rvol", 1.0)), 2)
    er = round(float(telemetry.get("efficiency_ratio", 0.30)), 2)
    regime_archetype = "TRENDING_BREAKOUT_REGIME" if er >= 0.28 else "MEAN_REVERTING_SWEEP_REGIME"

    return {
        "currency": asset["currency"],
        "fx_multiplier": fx_mult,
        "mini_theoretical_sek": mini_theo_sek,
        "mini_short_theoretical_sek": mini_short_theo_sek,
        "mini_short_financing_level": short_fin_lvl,
        "mini_effective_leverage": mini_lev,
        "regular_shares_net_unrealized_pnl_sek": net_shares_pnl_sek,
        "distance_to_ema4": round(spot - float(telemetry["ema4"]), 2),
        "distance_to_vwap": round(spot - float(telemetry["vwap"]), 2),
        "distance_to_ema50": round(spot - ema50, 2),
        "opening_range_width": or_range,
        "effective_volatility_atr": eff_vol,
        "rvol": rvol,
        "efficiency_ratio_5d": er,
        "regime_archetype": regime_archetype,
    }


def compute_verified_fee_adjusted_rr(
    draft: TradeDecisionDraft, telemetry: dict, asset: dict, risk: dict
) -> dict[str, Any]:
    """
    Deterministic Python Math Auditor (Level 3a):
    Supports both LONG (`BUY_MINI_LONG`, `BUY_SHARES`) and SHORT (`BUY_MINI_SHORT`) setups.
    Recomputes exact net Risk/Reward after Avanza FX drag (0.50% USD) or Mini spread (0.38%).
    """
    spot = float(telemetry["close"])
    is_short = "SHORT" in draft.action or "MINI S" in draft.instrument.upper()

    if is_short:
        stop = float(draft.underlying_stop_price) if draft.underlying_stop_price > spot else round(spot * 1.02, 2)
        target = float(draft.take_profit_target) if (0 < draft.take_profit_target < spot) else round(spot * 0.96, 2)
        gross_reward = max(spot - target, 0.0)
        gross_risk = max(stop - spot, 0.01)
    else:
        stop = float(draft.underlying_stop_price) if (0 < draft.underlying_stop_price < spot) else float(asset.get("hard_stop_price", spot * 0.98))
        target = float(draft.take_profit_target) if draft.take_profit_target > spot else float(asset.get("take_profit_price", spot * 1.04))
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
        "direction": "SHORT" if is_short else "LONG",
        "spot": spot,
        "stop": stop,
        "target": target,
        "llm_claimed_rr": llm_rr,
        "python_verified_net_rr": verified_rr,
        "rr_discrepancy": discrepancy,
        "math_hallucination_detected": discrepancy > 0.35,
    }


def format_avanza_order_instructions(
    decision: TradeDecision,
    telemetry: dict,
    asset: dict,
    metrics: dict,
    risk: dict,
    account_id: str = "ISK 5872428",
) -> str:
    """
    Generates a step-by-step Avanza order placement & hardware Stop-Loss guide whenever
    an active trade action (`BUY_*` or `SELL_*`) is recommended.
    """
    if decision.action == "HOLD" or decision.quantity <= 0:
        return ""

    spot = float(telemetry["close"])
    cur = asset.get("currency", "USD")
    fx = float(metrics.get("fx_multiplier", 10.16 if cur == "USD" else 1.0))
    parity = float(asset.get("mini_parity", 10.0)) or 10.0

    if decision.action in ("BUY_MINI_LONG", "BUY_MINI_SHORT"):
        is_short = decision.action == "BUY_MINI_SHORT"
        fin_lvl = float(metrics["mini_short_financing_level"]) if is_short else float(asset["mini_financing_level"])
        entry_sek = float(decision.limit_price or (metrics["mini_short_theoretical_sek"] if is_short else metrics["mini_theoretical_sek"]))
        trig_sek = float(decision.avanza_stop_trigger_sek or 0.0)
        sell_sek = float(decision.avanza_stop_sell_sek or 0.0)
        tp_u = float(decision.take_profit_target)
        stop_u = float(decision.underlying_stop_price)

        if is_short:
            tp_sek = round(((fin_lvl - tp_u) * fx) / parity, 2)
            risk_1r_u = max(stop_u - spot, 0.01)
            be_reach_u = round(spot - float(risk.get("breakeven_ratchet_r_mult", 0.85)) * risk_1r_u, 2)
            be_lock_u = round(spot - float(risk.get("breakeven_lock_r_mult", 0.15)) * risk_1r_u, 2)
            be_trig_sek = round(((fin_lvl - be_lock_u) * fx) / parity, 2)
        else:
            tp_sek = round(((tp_u - fin_lvl) * fx) / parity, 2)
            risk_1r_u = max(spot - stop_u, 0.01)
            be_reach_u = round(spot + float(risk.get("breakeven_ratchet_r_mult", 0.85)) * risk_1r_u, 2)
            be_lock_u = round(spot + float(risk.get("breakeven_lock_r_mult", 0.15)) * risk_1r_u, 2)
            be_trig_sek = round(((be_lock_u - fin_lvl) * fx) / parity, 2)

        notional_sek = round(decision.quantity * entry_sek, 0)
        total_risk_sek = round(decision.quantity * max(entry_sek - sell_sek, 0.20), 0)
        courtage_note = "0 SEK courtage (>= 1,000 SEK)" if notional_sek >= 1000 else "Note: < 1,000 SEK incurs courtage"

        return (
            f"🏦 *HOW TO PLACE THIS ORDER IN AVANZA ({account_id}):*\n"
            f"*1. Place Entry Order (Köp):*\n"
            f"   • Search Instrument: `{decision.instrument}`\n"
            f"   • Action: *Köp* (Limit Order — never use market order on ETPs)\n"
            f"   • Antal (Quantity): `{decision.quantity} st` (Total: `~{int(notional_sek)} SEK` | {courtage_note})\n"
            f"   • Kurs (Limit Price): `{entry_sek:.2f} SEK` (1R Max Risk: `~{int(total_risk_sek)} SEK`)\n"
            f"*2. Immediately Attach Avanza Hardware Stop-Loss:*\n"
            f"   • Open position $\\rightarrow$ tap *Stop-Loss*:\n"
            f"   • *Triggerkurs:* `<= {trig_sek:.2f} SEK` (Underlying `{decision.ticker}` @ `{stop_u}` {cur})\n"
            f"   • *Säljkurs:* `{sell_sek:.2f} SEK` (`0.20 SEK` below trigger so fast drops fill against Market Maker bid)\n"
            f"   • *Antal:* `{decision.quantity} st` | *Giltighetstid:* `8 dagar`\n"
            f"*3. Profit Management & Telegram Sync:*\n"
            f"   • *Breakeven Ratchet (+0.85R):* When `{decision.ticker}` reaches `{be_reach_u}` {cur}, raise Avanza Stop `Triggerkurs` to `{be_trig_sek:.2f} SEK` (`+0.15R` profit lock).\n"
            f"   • *Take-Profit Target:* Sell at `~{tp_sek:.2f} SEK` (Underlying `{tp_u}` {cur}).\n"
            f"   • *Sync Ledger:* Send `/fill_mini {decision.quantity} {entry_sek:.2f}` in this chat once filled."
        )

    if decision.action in ("SELL_MINI_LONG", "SELL_MINI_SHORT"):
        exit_sek = float(decision.limit_price or metrics["mini_theoretical_sek"])
        return (
            f"🏦 *HOW TO EXIT THIS MINI IN AVANZA ({account_id}):*\n"
            f"   • Open `{decision.instrument}` $\\rightarrow$ cancel any open Stop-Loss order first.\n"
            f"   • Tap *Sälj* $\\rightarrow$ *Antal:* `{decision.quantity} st` $\\rightarrow$ *Kurs:* `{exit_sek:.2f} SEK` (match Market Maker Köpkurs/bid).\n"
            f"   • Send `/close_mini {exit_sek:.2f}` in this chat once sold."
        )

    if decision.action in ("BUY_SHARES", "SELL_SHARES"):
        side_sv = "Köp" if decision.action == "BUY_SHARES" else "Sälj"
        px = float(decision.limit_price or spot)
        fx_note = "Includes 0.25% Avanza FX conversion fee" if cur == "USD" else "0% FX fee (SEK)"
        sync_cmd = f"/fill_shares {decision.quantity} {px:.2f}" if decision.action == "BUY_SHARES" else f"/close_shares {px:.2f}"
        return (
            f"🏦 *HOW TO PLACE THIS SHARE ORDER IN AVANZA ({account_id}):*\n"
            f"   • Search `{decision.ticker}` $\\rightarrow$ tap *{side_sv}*.\n"
            f"   • *Antal:* `{decision.quantity} st` | *Kurs (Limit):* `{px:.2f} {cur}` ({fx_note}).\n"
            f"   • *Hard Stop-Loss:* `{decision.underlying_stop_price} {cur}` | *Target:* `{decision.take_profit_target} {cur}`.\n"
            f"   • *Sync Ledger:* Send `{sync_cmd}` in this chat once filled."
        )

    return ""
