"""
Pure Quantitative Pricing, Regime Classification, Fee-Adjusted R:R Math Auditor,
Bailey & Lopez de Prado Statistical Validation (PSR/DSR/Wilson CI),
Deterministic Strategy Candidate Builder, and Clean Telegram HTML Formatters.
"""
import html
import math
import re
from statistics import mean, stdev
from typing import Any, Optional
from traderagent.domain.models import TradeActionType, TradeDecision, TradeDecisionDraft


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

    # Mini Short financing level & theoretical price (uses configured short financing level or ~1.22 * spot fallback)
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
    er = round(min(1.0, max(0.0, float(telemetry.get("efficiency_ratio", 0.30)))), 2)
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
    Supports both LONG (`BUY_MINI_LONG`, `BUY_SHARES`) and SHORT (`BUY_MINI_SHORT`) setups,
    while marking exits (`SELL_*`) and underwater `HOLD` states as `rr_applicable=False`.
    Recomputes exact net Risk/Reward after Avanza FX drag (0.50% USD) on shares or
    certificate-equity spread (0.38% of Mini intrinsic equity `|spot - fin_lvl|`) on Mini Futures.
    """
    spot = float(telemetry["close"])
    action_str = str(draft.action)
    llm_rr = round(float(draft.net_risk_reward_ratio), 2)

    if action_str.startswith("SELL"):
        return {
            "direction": "EXIT",
            "spot": spot,
            "stop": float(draft.underlying_stop_price or 0.0),
            "target": 0.0,
            "llm_claimed_rr": llm_rr,
            "python_verified_net_rr": 0.0,
            "rr_discrepancy": 0.0,
            "rr_applicable": False,
            "math_hallucination_detected": False,
        }

    is_short = "SHORT" in action_str or "MINI S" in draft.instrument.upper()

    if is_short:
        draft_stop = float(draft.underlying_stop_price or 0.0)
        draft_tp = float(draft.take_profit_target or 0.0)
        stop = draft_stop if draft_stop > spot else round(spot * 1.02, 2)
        target = draft_tp if (0 < draft_tp < spot) else round(spot * 0.96, 2)
        gross_reward = max(spot - target, 0.0)
        gross_risk = max(stop - spot, 0.01)
    else:
        draft_stop = float(draft.underlying_stop_price or 0.0)
        draft_tp = float(draft.take_profit_target or 0.0)
        if action_str == "HOLD" and draft_stop >= spot:
            return {
                "direction": "LONG",
                "spot": spot,
                "stop": draft_stop,
                "target": draft_tp,
                "llm_claimed_rr": llm_rr,
                "python_verified_net_rr": 0.0,
                "rr_discrepancy": 0.0,
                "rr_applicable": False,
                "math_hallucination_detected": False,
            }

        asset_stop = float(asset.get("hard_stop_price") or 0.0)
        asset_tp = float(asset.get("take_profit_price") or 0.0)
        if 0 < draft_stop < spot:
            stop = draft_stop
        elif 0 < asset_stop < spot:
            stop = asset_stop
        else:
            stop = round(spot * 0.98, 2)

        if draft_tp > spot:
            target = draft_tp
        elif asset_tp > spot:
            target = asset_tp
        else:
            target = round(spot * 1.04, 2)

        gross_reward = max(target - spot, 0.0)
        gross_risk = max(spot - stop, 0.01)

    if "MINI" in action_str or "MINI" in draft.instrument.upper():
        spread_pct = float(risk.get("avanza_mini_spread_pct", 0.0038))
        if is_short:
            s_fin = float(asset.get("mini_short_financing_level") or round(spot * 1.22, 2))
            cert_equity = max(s_fin - spot, spot * 0.15)
        else:
            fin_lvl = float(asset.get("mini_financing_level", 0.0))
            cert_equity = max(spot - fin_lvl, spot * 0.15) if fin_lvl > 0 and spot > fin_lvl else (spot * 0.22)
        spread_cost = cert_equity * spread_pct
        net_reward = max(gross_reward - spread_cost, 0.0)
        net_risk = gross_risk + (spread_cost * 0.5)
    else:
        fx_drag = spot * float(risk.get("avanza_us_fx_roundtrip_pct", 0.005)) if asset.get("currency") == "USD" else 0.0
        net_reward = max(gross_reward - fx_drag, 0.0)
        net_risk = gross_risk + (fx_drag * 0.5)

    verified_rr = round(net_reward / net_risk, 2) if net_risk > 0 else 0.0
    discrepancy = round(abs(llm_rr - verified_rr), 2)
    return {
        "direction": "SHORT" if is_short else "LONG",
        "spot": spot,
        "stop": stop,
        "target": target,
        "llm_claimed_rr": llm_rr,
        "python_verified_net_rr": verified_rr,
        "rr_discrepancy": discrepancy,
        "rr_applicable": True,
        "math_hallucination_detected": discrepancy > 0.35,
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam's rational approximation for inverse standard normal CDF."""
    p = min(max(p, 1e-9), 1.0 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)


def compute_statistical_confidence(
    trade_pnls: list[float],
    num_trials: int = 8,
    benchmark_sr: float = 0.0,
) -> dict[str, Any]:
    """
    Bailey & Lopez de Prado Statistical Overfitting Safeguards:
      - Wilson 95% lower-bound Win Rate (penalizes small sample n)
      - Probabilistic Sharpe Ratio (PSR) adjusted for skewness & kurtosis
      - Deflated Sharpe Ratio (DSR) deflated for multiple strategy trials `num_trials`
      - Minimum Track Record Length (MinTRL) in number of trades
    """
    n = len(trade_pnls)
    if n < 3:
        wins = sum(1 for x in trade_pnls if x > 0)
        return {
            "n_trades": n,
            "raw_win_rate_pct": round((wins / n) * 100.0, 1) if n else 0.0,
            "wilson_95_lower_wr_pct": 0.0,
            "per_trade_sharpe": 0.0,
            "psr_pct": 0.0,
            "dsr_pct": 0.0,
            "min_trl_trades": 30,
            "statistically_significant": False,
        }

    wins = sum(1 for x in trade_pnls if x > 0)
    p_hat = wins / n
    z = 1.96
    denom = 1.0 + (z * z) / n
    center = p_hat + (z * z) / (2.0 * n)
    rad = z * math.sqrt((p_hat * (1.0 - p_hat) + (z * z) / (4.0 * n)) / n)
    wilson_low = round(max(0.0, (center - rad) / denom) * 100.0, 1)

    mu = mean(trade_pnls)
    sd = stdev(trade_pnls) if n > 1 else 1.0
    sr = (mu / sd) if sd > 0 else 0.0

    # Sample skewness and excess/raw kurtosis
    if sd > 0:
        m3 = sum(((x - mu) / sd) ** 3 for x in trade_pnls) / n
        m4 = sum(((x - mu) / sd) ** 4 for x in trade_pnls) / n
    else:
        m3, m4 = 0.0, 3.0

    # Expected maximum Sharpe under N independent trials (Bailey & Lopez de Prado)
    euler_gamma = 0.5772156649
    if num_trials > 1:
        sr_var = max(1.0 / max(n - 1, 1), 0.02)
        sr_std = math.sqrt(sr_var)
        sr0_deflated = sr_std * (
            (1.0 - euler_gamma) * _norm_ppf(1.0 - 1.0 / num_trials)
            + euler_gamma * _norm_ppf(1.0 - 1.0 / (num_trials * math.e))
        )
    else:
        sr0_deflated = benchmark_sr

    sr_denom = math.sqrt(max(1.0 - m3 * sr + ((m4 - 1.0) / 4.0) * (sr ** 2), 1e-6))
    psr = round(_norm_cdf(((sr - benchmark_sr) * math.sqrt(max(n - 1, 1))) / sr_denom) * 100.0, 1)
    dsr = round(_norm_cdf(((sr - sr0_deflated) * math.sqrt(max(n - 1, 1))) / sr_denom) * 100.0, 1)

    # MinTRL (95% confidence vs sr0=0.05)
    target_sr_gap = max(sr - 0.05, 0.05)
    min_trl = int(math.ceil(1.0 + (1.0 - m3 * sr + ((m4 - 1.0) / 4.0) * (sr ** 2)) * ((_norm_ppf(0.95) / target_sr_gap) ** 2)))

    return {
        "n_trades": n,
        "raw_win_rate_pct": round(p_hat * 100.0, 1),
        "wilson_95_lower_wr_pct": wilson_low,
        "per_trade_sharpe": round(sr, 3),
        "skewness": round(m3, 2),
        "kurtosis": round(m4, 2),
        "psr_pct": psr,
        "dsr_pct": dsr,
        "dsr_benchmark_sr0": round(sr0_deflated, 3),
        "min_trl_trades": min_trl,
        "statistically_significant": (dsr >= 90.0 and n >= min_trl),
    }


def _fmt_price(val: float, currency: str) -> str:
    """Formats a price cleanly with currency symbol."""
    return f"${val:.2f}" if currency == "USD" else f"{val:.2f} SEK"


def _sanitize_plain_text(raw: str) -> str:
    """Strips LaTeX macros, Markdown emphasis/backticks, and internal pipeline tokens before HTML escaping."""
    text = str(raw or "").strip()
    text = text.replace("$\\rightarrow$", "->").replace("\\rightarrow", "->")
    text = re.sub(r"\$\\[a-zA-Z]+\$", "->", text)
    text = text.replace("**", "").replace("`", "")
    # Scrub internal pipeline tokens that should never appear in user-facing alerts
    for token in (
        "REVISED_BY_CRITIC",
        "VETOED_BY_CRITIC",
        "VETOED_BY_GUARDRAIL",
        "MinTRL",
        "CSCV",
        "PBO",
        "DSR",
        "PSR",
        "L3a",
        "L3b",
        "L1",
        "L2",
        "L4",
        "ER",
    ):
        text = re.sub(rf"\b{re.escape(token)}\b", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return html.escape(text)


def format_avanza_order_instructions(
    decision: TradeDecision,
    telemetry: dict,
    asset: dict,
    metrics: dict,
    risk: dict,
    account_id: str = "ISK 5872428",
) -> str:
    """
    Generates a step-by-step Avanza order placement & hardware Stop-Loss guide in clean Telegram HTML
    whenever an active trade action (`BUY_*` or `SELL_*`) is recommended.
    """
    if decision.action == "HOLD" or decision.quantity <= 0:
        return ""

    spot = float(telemetry["close"])
    cur = str(asset.get("currency", "USD"))
    fx = float(metrics.get("fx_multiplier", 10.16 if cur == "USD" else 1.0))
    parity = float(asset.get("mini_parity", 10.0)) or 10.0
    esc_acct = html.escape(str(account_id))
    esc_ticker = html.escape(str(decision.ticker))
    esc_inst = html.escape(str(decision.instrument))
    esc_cur = html.escape(cur)

    if decision.action in ("BUY_MINI_LONG", "BUY_MINI_SHORT"):
        is_short = decision.action == "BUY_MINI_SHORT"
        fin_lvl = float(metrics.get("mini_short_financing_level", 0.0)) if is_short else float(asset.get("mini_financing_level", 0.0))
        fallback_sek = metrics.get("mini_short_theoretical_sek", 0.0) if is_short else metrics.get("mini_theoretical_sek", 0.0)
        entry_sek = float(decision.limit_price or fallback_sek)
        trig_sek = float(decision.avanza_stop_trigger_sek or 0.0)
        sell_sek = float(decision.avanza_stop_sell_sek or max(round(trig_sek - 0.20, 2), 0.0))
        tp_u = float(decision.take_profit_target)
        stop_u = float(decision.underlying_stop_price)

        if is_short:
            tp_sek = round(((fin_lvl - tp_u) * fx) / parity, 2) if fin_lvl > tp_u else round(entry_sek * 1.2, 2)
            risk_1r_u = max(stop_u - spot, 0.01)
            be_reach_u = round(spot - float(risk.get("breakeven_ratchet_r_mult", 0.85)) * risk_1r_u, 2)
            be_lock_u = round(spot - float(risk.get("breakeven_lock_r_mult", 0.15)) * risk_1r_u, 2)
            be_trig_sek = round(((fin_lvl - be_lock_u) * fx) / parity, 2) if fin_lvl > be_lock_u else entry_sek
        else:
            tp_sek = round(((tp_u - fin_lvl) * fx) / parity, 2) if fin_lvl > 0 else round(entry_sek * 1.2, 2)
            risk_1r_u = max(spot - stop_u, 0.01)
            be_reach_u = round(spot + float(risk.get("breakeven_ratchet_r_mult", 0.85)) * risk_1r_u, 2)
            be_lock_u = round(spot + float(risk.get("breakeven_lock_r_mult", 0.15)) * risk_1r_u, 2)
            be_trig_sek = round(((be_lock_u - fin_lvl) * fx) / parity, 2) if fin_lvl > 0 else entry_sek

        notional_sek = round(decision.quantity * entry_sek, 0)
        total_risk_sek = round(decision.quantity * max(entry_sek - sell_sek, 0.20), 0)
        courtage_note = "0 SEK courtage (&gt;= 1,000 SEK)" if notional_sek >= 1000 else "Note: &lt; 1,000 SEK incurs courtage"
        side_tag = "SHORT" if is_short else "LONG"

        return (
            f"<b>HOW TO PLACE THIS ORDER IN AVANZA ({esc_acct}):</b>\n"
            f"<b>1. Entry Order (Köp):</b>\n"
            f"• Search Instrument: <code>{esc_inst}</code>\n"
            f"• Action: <b>Köp</b> (Limit Order — never market order on ETPs)\n"
            f"• <b>Antal:</b> <code>{decision.quantity} st</code> (Total: ~{int(notional_sek):,} SEK | {courtage_note})\n"
            f"• <b>Kurs (Limit):</b> <code>{entry_sek:.2f} SEK</code> (1R Max Risk: ~{int(total_risk_sek):,} SEK)\n"
            f"<b>2. Immediately Attach Hardware Stop-Loss:</b>\n"
            f"• Open position -&gt; tap <b>Stop-Loss</b>:\n"
            f"• <b>Triggerkurs:</b> <code>&lt;= {trig_sek:.2f} SEK</code> ({esc_ticker} @ {stop_u:.2f} {esc_cur})\n"
            f"• <b>Säljkurs:</b> <code>{sell_sek:.2f} SEK</code> (0.20 SEK below trigger for Market Maker fill)\n"
            f"• <b>Antal:</b> <code>{decision.quantity} st</code> | <b>Giltighetstid:</b> 8 dagar\n"
            f"<b>3. Profit Management &amp; Sync:</b>\n"
            f"• <b>Breakeven Ratchet (+0.85R):</b> At {be_reach_u:.2f} {esc_cur}, raise Stop Triggerkurs to <code>{be_trig_sek:.2f} SEK</code> (+0.15R profit lock)\n"
            f"• <b>Take-Profit / Trail:</b> Target ~{tp_sek:.2f} SEK ({tp_u:.2f} {esc_cur}) or trail with intraday average until 20:25 CET\n"
            f"• <b>Sync Ledger:</b> <code>/fill_mini {esc_ticker} {decision.quantity} {entry_sek:.2f} {side_tag}</code>"
        )

    if decision.action in ("SELL_MINI_LONG", "SELL_MINI_SHORT"):
        is_short_exit = decision.action == "SELL_MINI_SHORT" or "MINI S" in decision.instrument.upper()
        fallback_px = metrics.get("mini_short_theoretical_sek", 0.0) if is_short_exit else metrics.get("mini_theoretical_sek", 0.0)
        exit_sek = float(decision.limit_price or fallback_px)
        return (
            f"<b>HOW TO EXIT THIS MINI IN AVANZA ({esc_acct}):</b>\n"
            f"• Open <code>{esc_inst}</code> -&gt; cancel any open Stop-Loss order first.\n"
            f"• Tap <b>Sälj</b> -&gt; <b>Antal:</b> <code>{decision.quantity} st</code> -&gt; <b>Kurs (Limit):</b> <code>{exit_sek:.2f} SEK</code> (match Market Maker bid).\n"
            f"• <b>Sync Ledger:</b> <code>/close_mini {esc_ticker} {exit_sek:.2f}</code>"
        )

    if decision.action == "SELL_SHARES":
        px = float(decision.limit_price or spot)
        fx_note = "Includes 0.25% Avanza FX conversion fee" if cur == "USD" else "0% FX fee (SEK)"
        return (
            f"<b>HOW TO EXIT SHARES IN AVANZA ({esc_acct}):</b>\n"
            f"• Search <code>{esc_ticker}</code> -&gt; tap <b>Sälj</b>.\n"
            f"• <b>Antal:</b> <code>{decision.quantity} st</code> | <b>Kurs (Limit):</b> <code>{px:.2f} {esc_cur}</code> ({fx_note}).\n"
            f"• <b>Sync Ledger:</b> <code>/close_shares {esc_ticker} {px:.2f}</code>"
        )

    if decision.action == "BUY_SHARES":
        px = float(decision.limit_price or spot)
        fx_note = "Includes 0.25% Avanza FX conversion fee" if cur == "USD" else "0% FX fee (SEK)"
        return (
            f"<b>HOW TO PLACE THIS SHARE ORDER IN AVANZA ({esc_acct}):</b>\n"
            f"• Search <code>{esc_ticker}</code> -&gt; tap <b>Köp</b>.\n"
            f"• <b>Antal:</b> <code>{decision.quantity} st</code> | <b>Kurs (Limit):</b> <code>{px:.2f} {esc_cur}</code> ({fx_note}).\n"
            f"• <b>Hard Stop-Loss:</b> <code>{decision.underlying_stop_price:.2f} {esc_cur}</code> | <b>Target:</b> <code>{decision.take_profit_target:.2f} {esc_cur}</code>.\n"
            f"• <b>Sync Ledger:</b> <code>/fill_shares {esc_ticker} {decision.quantity} {px:.2f}</code>"
        )

    return ""


def _translate_veto_reason(rationale: str, risk: dict) -> Optional[str]:
    """Maps internal guardrail veto/exit markers into plain-English explanations."""
    if "[EMA4 FAILURE EXIT]" in rationale:
        if "broke above" in rationale:
            return "Price broke above both short-term trend and the intraday average, triggering an automatic exit on your Short Mini."
        return "Price broke below both short-term trend and the intraday average, triggering an automatic protective exit."
    veto_map = {
        "DAILY QUOTA": "Today's maximum trade count is reached, so standing aside prevents overtrading.",
        "LOSS LOCKOUT": "Today's daily loss limit was hit, pausing any new risk entries until tomorrow.",
        "COOLDOWN": "A recent stop-out cooldown is still active to prevent rushing back into the market.",
        "MAX CONCURRENT MINIS": "You already hold an open leveraged Mini, keeping exposure limited to one Mini at a time.",
        "OPENING BELL GATE": "Standing aside during the noisy 15:30-15:45 CET opening bell volatility window.",
        "ETP CUTOFF": "Nordic exchange-traded product trading cutoff (20:30 CET) has passed for new leveraged entries.",
        "SUB-VWAP": "Price remains below the intraday average without a confirmed bounce off yesterday's low.",
        "ABOVE-VWAP SHORT": "Price remains above the intraday average without a confirmed rejection at yesterday's high.",
        "LOW ER CHOP": "Recent multi-day price action is choppy and range-bound, making momentum breakouts unreliable.",
        "LOW RVOL": "Trading volume is lighter than normal, lacking strong buyer or seller conviction.",
        "LOW R:R": f"Potential reward versus risk falls short of the minimum {risk.get('min_net_rr_ratio', 1.8)}:1 hurdle after fees.",
    }
    for key, msg in veto_map.items():
        if f"[VETO: {key}" in rationale:
            return msg
    return None


def build_plain_english_reasons(
    decision: TradeDecision,
    telemetry: dict,
    asset: dict,
    metrics: dict,
    risk: dict,
) -> list[str]:
    """
    Produces 2-3 concise, jargon-free plain-English bullet reasons explaining the trade verdict
    using price structure, volume/trend quality, and position/stop state.
    """
    cur = str(asset.get("currency", "USD"))
    spot = float(telemetry.get("close", 0.0))
    vwap = float(telemetry.get("vwap", spot))
    ema4 = float(telemetry.get("ema4", spot))
    rvol = float(telemetry.get("rvol", metrics.get("rvol", 1.0)))
    er = float(telemetry.get("efficiency_ratio", metrics.get("efficiency_ratio_5d", 0.30)))
    min_er = float(risk.get("min_trend_efficiency_ratio", 0.28))
    min_rvol = float(risk.get("min_rvol_for_breakout", 1.05))

    reasons: list[str] = []

    # 1. Price location relative to intraday average & short-term trend
    if spot > vwap and spot > ema4:
        reasons.append(
            f"Price ({_fmt_price(spot, cur)}) is holding above the intraday average ({_fmt_price(vwap, cur)}) "
            f"and short-term trend ({_fmt_price(ema4, cur)}), showing steady buyer control."
        )
    elif spot < vwap and spot < ema4:
        reasons.append(
            f"Price ({_fmt_price(spot, cur)}) is trading below both the intraday average ({_fmt_price(vwap, cur)}) "
            f"and short-term trend ({_fmt_price(ema4, cur)}), keeping sellers in control."
        )
    else:
        reasons.append(
            f"Price ({_fmt_price(spot, cur)}) is caught between short-term trend ({_fmt_price(ema4, cur)}) "
            f"and the intraday average ({_fmt_price(vwap, cur)}), showing mixed two-way movement."
        )

    # 2. Market session, volume, or multi-day trend clarity
    market_open = decision.market_open if decision.market_open is not None else telemetry.get("market_open")
    if market_open is False:
        reasons.append(
            "The exchange is currently closed, so this check uses the latest completed session rather than live order flow."
        )
    elif er < min_er:
        reasons.append(
            "Multi-day price action has been choppy and range-bound, which favors waiting over chasing breakouts."
        )
    elif rvol >= min_rvol:
        reasons.append(
            f"Trading volume is active ({rvol:.2f}x normal) alongside a clean multi-day directional trend."
        )
    else:
        reasons.append(
            f"Trading volume is relatively quiet ({rvol:.2f}x normal), lacking strong confirmation for a breakout."
        )

    # 3. Specific action, stop, ratchet, or veto reason
    veto_plain = _translate_veto_reason(decision.rationale or "", risk)
    held_shares = int(asset.get("regular_shares_held", 0))
    held_minis = int(asset.get("mini_units_held", 0))

    if decision.ratchet_triggered:
        reasons.append(
            f"Your open Mini hit +0.85R profit, locking in a raised protective stop at "
            f"{_fmt_price(decision.underlying_stop_price, cur)}."
        )
    elif veto_plain:
        reasons.append(veto_plain)
    elif decision.action == "SELL_SHARES":
        stop_px = float(asset.get("hard_stop_price") or decision.underlying_stop_price or spot)
        reasons.append(
            f"Price reached or fell below your protective stop ({_fmt_price(stop_px, cur)}), so exiting protects remaining capital."
        )
    elif decision.action.startswith("BUY"):
        max_risk_sek = int(float(risk.get("max_risk_per_trade_sek", 450.0)))
        rr_txt = (
            f" with {decision.net_risk_reward_ratio:.2f}:1 net reward-to-risk after fees"
            if (decision.rr_applicable and decision.net_risk_reward_ratio > 0)
            else ""
        )
        reasons.append(
            f"Position risk is capped around {max_risk_sek} SEK with a protective stop at "
            f"{_fmt_price(decision.underlying_stop_price, cur)}{rr_txt}."
        )
    elif held_minis > 0:
        reasons.append(
            f"Your open Mini ({held_minis} units) stays above exit levels with protection at "
            f"{_fmt_price(decision.underlying_stop_price, cur)}."
        )
    elif held_shares > 0:
        hard_stop = float(asset.get("hard_stop_price") or decision.underlying_stop_price or 0.0)
        avg_px = float(asset.get("blended_avg_price") or 0.0)
        stop_clause = f" above your protective stop ({_fmt_price(hard_stop, cur)})" if hard_stop > 0 else ""
        reasons.append(
            f"You hold {held_shares} shares (avg {_fmt_price(avg_px, cur)}){stop_clause}; no urgent move is needed."
        )
    else:
        reasons.append("Waiting for a clearer directional breakout or sweep reclaim with stronger volume before risking capital.")

    return reasons[:3]


def _build_mini_entry_candidate(
    side: str,
    ticker: str,
    telemetry: dict,
    asset: dict,
    risk: dict,
    metrics: dict,
    avail_cash_sek: float,
    regime: str,
    setup_note: str,
) -> Optional[TradeDecision]:
    """Builds and R:R-verifies a deterministic Mini Long or Mini Short candidate."""
    spot = float(telemetry["close"])
    low = float(telemetry.get("low", spot))
    high = float(telemetry.get("high", spot))
    pd_low = float(telemetry.get("pd_low", low))
    pd_high = float(telemetry.get("pd_high", high))
    vwap = float(telemetry.get("vwap", spot))
    atr14 = max(float(telemetry.get("atr14", spot * 0.01)), 0.01)
    cur = str(asset.get("currency", "USD"))
    fx = float(metrics.get("fx_multiplier", 10.16 if cur == "USD" else 1.0))
    parity = float(asset.get("mini_parity") or 10.0)
    stop_buf = float(risk.get("stop_buffer_below_pd_low_atr_mult", 0.20))
    max_risk_sek = float(risk.get("max_risk_per_trade_sek", 450.0))
    min_rr = float(risk.get("min_net_rr_ratio", 1.8))

    if side == "LONG":
        fin_lvl = float(asset.get("mini_financing_level") or 0.0)
        if fin_lvl <= 0 or spot <= fin_lvl:
            return None
        anchor = pd_low if (0 <= (spot - pd_low) <= 2.5 * atr14) else min(vwap, low)
        stop_u = round(min(anchor, low) - (stop_buf * atr14), 2)
        if stop_u >= spot:
            stop_u = round(spot - 1.2 * atr14, 2)
        risk_u = max(spot - stop_u, 0.05)
        target_u = round(spot + 2.2 * risk_u, 2)
        entry_sek = round(((spot - fin_lvl) * fx) / parity, 2)
        trig_sek = round(((stop_u - fin_lvl) * fx) / parity, 2)
        action: TradeActionType = "BUY_MINI_LONG"
        instrument = str(asset.get("mini_instrument") or f"MINI L {ticker} AVA")
    else:
        fin_lvl = float(metrics.get("mini_short_financing_level") or asset.get("mini_short_financing_level") or 0.0)
        if fin_lvl <= spot:
            return None
        anchor = pd_high if (0 <= (pd_high - spot) <= 2.5 * atr14) else max(vwap, high)
        stop_u = round(max(anchor, high) + (stop_buf * atr14), 2)
        if stop_u <= spot:
            stop_u = round(spot + 1.2 * atr14, 2)
        risk_u = max(stop_u - spot, 0.05)
        target_u = round(max(spot - 2.2 * risk_u, 0.01), 2)
        entry_sek = round(((fin_lvl - spot) * fx) / parity, 2)
        trig_sek = round(((fin_lvl - stop_u) * fx) / parity, 2)
        action = "BUY_MINI_SHORT"
        instrument = str(asset.get("mini_short_instrument") or f"MINI S {ticker} AVA")

    sell_sek = round(trig_sek - 0.20, 2)
    if entry_sek <= 0.5 or trig_sek <= 0.30:
        return None

    per_unit_risk = max(entry_sek - sell_sek, 0.25)
    qty = min(
        max(1, int(max_risk_sek / per_unit_risk)),
        max(1, int((avail_cash_sek * 0.95) / max(entry_sek, 1.0))),
    )
    draft = TradeDecisionDraft(
        ticker=ticker,
        market_regime=regime,  # type: ignore[arg-type]
        action=action,
        instrument=instrument,
        quantity=qty,
        limit_price=entry_sek,
        avanza_stop_trigger_sek=trig_sek,
        avanza_stop_sell_sek=sell_sek,
        underlying_stop_price=stop_u,
        take_profit_target=target_u,
        net_risk_reward_ratio=2.2,
        overnight_gap_warning=False,
        rationale=setup_note,
    )
    audit = compute_verified_fee_adjusted_rr(draft, telemetry, asset, risk)
    verified_rr = float(audit["python_verified_net_rr"])
    if verified_rr < min_rr:
        return None

    return TradeDecision(
        ticker=ticker,
        market_regime=regime,  # type: ignore[arg-type]
        action=action,
        instrument=instrument,
        quantity=qty,
        limit_price=entry_sek,
        avanza_stop_trigger_sek=trig_sek,
        avanza_stop_sell_sek=sell_sek,
        underlying_stop_price=stop_u,
        take_profit_target=target_u,
        net_risk_reward_ratio=verified_rr,
        overnight_gap_warning=False,
        rationale=setup_note,
        confidence_score=78,
        critic_verdict="APPROVED",
        critic_notes="Deterministic Python setup and fee-adjusted R:R verified.",
        rr_applicable=True,
        data_timestamp_cet=str(telemetry.get("data_timestamp_cet", "")),
        market_open=telemetry.get("market_open"),
    )


def build_deterministic_candidate(
    telemetry: dict,
    portfolio: dict,
    asset: dict,
    risk: dict,
    metrics: dict,
) -> TradeDecision:
    """
    Builds a fully deterministic Python TradeDecision candidate (verdict, stop, target,
    and risk-parity sizing) for live `/analyze` without relying on LLM numeric guesswork.
    """
    ticker = str(telemetry.get("ticker", "NVDA"))
    spot = float(telemetry["close"])
    low = float(telemetry.get("low", spot))
    high = float(telemetry.get("high", spot))
    ema4 = float(telemetry.get("ema4", spot))
    ema50 = float(telemetry.get("ema50", spot))
    vwap = float(telemetry.get("vwap", spot))
    or_high = float(telemetry.get("or_high", high))
    or_low = float(telemetry.get("or_low", low))
    pd_low = float(telemetry.get("pd_low", low))
    pd_high = float(telemetry.get("pd_high", high))
    atr14 = max(float(telemetry.get("atr14", spot * 0.01)), 0.01)
    rvol = float(telemetry.get("rvol", metrics.get("rvol", 1.0)))
    er = float(telemetry.get("efficiency_ratio", metrics.get("efficiency_ratio_5d", 0.30)))

    min_er = float(risk.get("min_trend_efficiency_ratio", 0.28))
    min_rvol = float(risk.get("min_rvol_for_breakout", 1.05))
    sweep_mult = float(risk.get("pd_low_sweep_exception_atr_mult", 0.35))
    ema4_mult = float(risk.get("ema4_failure_exit_atr_mult", 0.15))
    stop_buf = float(risk.get("stop_buffer_below_pd_low_atr_mult", 0.20))

    cur = str(asset.get("currency", "USD"))
    fx = float(metrics.get("fx_multiplier", 10.16 if cur == "USD" else 1.0))
    parity = float(asset.get("mini_parity") or 10.0)
    fin_long = float(asset.get("mini_financing_level") or 0.0)
    fin_short = float(metrics.get("mini_short_financing_level") or asset.get("mini_short_financing_level") or round(spot * 1.22, 2))
    hard_stop = float(asset.get("hard_stop_price") or 0.0)
    asset_tp = float(asset.get("take_profit_price") or 0.0)
    held_shares = int(asset.get("regular_shares_held", 0))
    held_minis = int(asset.get("mini_units_held", 0))
    is_held_short = held_minis > 0 and (
        str(asset.get("mini_side", "")).upper() == "SHORT"
        or "MINI S" in str(asset.get("mini_instrument", "")).upper()
    )
    avail_cash_sek = float(portfolio.get("cash_reserves", {}).get("available_tactical_mini_sek", 14548.0))
    market_open = telemetry.get("market_open")
    data_ts_cet = str(telemetry.get("data_timestamp_cet", ""))

    is_pd_low_sweep = (pd_low - atr14 <= low <= pd_low + sweep_mult * atr14) and (spot > ema4)
    is_pd_high_sweep = (pd_high - sweep_mult * atr14 <= high <= pd_high + atr14) and (spot < ema4)

    if er >= min_er:
        if spot > vwap and spot > ema4:
            regime = "Momentum_Breakout"
        elif spot < vwap and spot < ema4:
            regime = "Downtrend_Continuation"
        else:
            regime = "Volatile_Chop"
    elif is_pd_low_sweep or is_pd_high_sweep:
        regime = "Reversal_Base_Building"
    elif spot < vwap and spot < ema4 and spot < ema50:
        regime = "Downtrend_Continuation"
    else:
        regime = "Volatile_Chop"

    account_id = str(portfolio.get("account_id", "ISK 5872428"))
    decision: Optional[TradeDecision] = None

    # 1. Active Mini position exit check (symmetric EMA4 + VWAP failure)
    if held_minis > 0:
        if not is_held_short and spot < (ema4 - ema4_mult * atr14) and spot < vwap:
            inst = str(asset.get("mini_instrument") or f"MINI L {ticker} AVA")
            exit_sek = float(metrics.get("mini_theoretical_sek") or 0.0)
            decision = TradeDecision(
                ticker=ticker,
                market_regime=regime,  # type: ignore[arg-type]
                action="SELL_MINI_LONG",
                instrument=inst,
                quantity=held_minis,
                limit_price=exit_sek,
                avanza_stop_trigger_sek=None,
                avanza_stop_sell_sek=None,
                underlying_stop_price=round(spot, 2),
                take_profit_target=0.0,
                net_risk_reward_ratio=0.0,
                overnight_gap_warning=False,
                rationale=f"[EMA4 FAILURE EXIT] 15m close ({spot:.2f}) broke below both EMA4 ({ema4:.2f}) and VWAP ({vwap:.2f}).",
                confidence_score=85,
                critic_verdict="APPROVED",
                rr_applicable=False,
                data_timestamp_cet=data_ts_cet,
                market_open=market_open,
            )
        elif is_held_short and spot > (ema4 + ema4_mult * atr14) and spot > vwap:
            inst = str(asset.get("mini_short_instrument") or f"MINI S {ticker} AVA")
            exit_sek = float(metrics.get("mini_short_theoretical_sek") or 0.0)
            decision = TradeDecision(
                ticker=ticker,
                market_regime=regime,  # type: ignore[arg-type]
                action="SELL_MINI_SHORT",
                instrument=inst,
                quantity=held_minis,
                limit_price=exit_sek,
                avanza_stop_trigger_sek=None,
                avanza_stop_sell_sek=None,
                underlying_stop_price=round(spot, 2),
                take_profit_target=0.0,
                net_risk_reward_ratio=0.0,
                overnight_gap_warning=False,
                rationale=f"[EMA4 FAILURE EXIT] 15m close ({spot:.2f}) broke above both EMA4 ({ema4:.2f}) and VWAP ({vwap:.2f}).",
                confidence_score=85,
                critic_verdict="APPROVED",
                rr_applicable=False,
                data_timestamp_cet=data_ts_cet,
                market_open=market_open,
            )

    # 2. Held regular shares hard-stop breach check
    if decision is None and held_shares > 0 and held_minis == 0 and hard_stop > 0 and spot <= hard_stop:
        decision = TradeDecision(
            ticker=ticker,
            market_regime=regime,  # type: ignore[arg-type]
            action="SELL_SHARES",
            instrument=ticker,
            quantity=held_shares,
            limit_price=round(spot, 2),
            avanza_stop_trigger_sek=None,
            avanza_stop_sell_sek=None,
            underlying_stop_price=hard_stop,
            take_profit_target=0.0,
            net_risk_reward_ratio=0.0,
            overnight_gap_warning=False,
            rationale=f"Spot ({spot:.2f}) reached or breached configured hard stop ({hard_stop:.2f}).",
            confidence_score=90,
            critic_verdict="APPROVED",
            rr_applicable=False,
            data_timestamp_cet=data_ts_cet,
            market_open=market_open,
        )

    # 3. New leveraged Mini entry setup (only when flat on Minis and exchange is not explicitly closed)
    if decision is None and held_minis == 0 and market_open is not False:
        long_breakout = (er >= min_er and rvol >= min_rvol and spot > vwap and spot > ema4 and (spot > or_high or spot > ema50))
        short_breakout = (er >= min_er and rvol >= min_rvol and spot < vwap and spot < ema4 and (spot < or_low or spot < ema50))

        if long_breakout or is_pd_low_sweep:
            setup_note = (
                f"Bullish {'sweep reclaim' if is_pd_low_sweep and not long_breakout else 'trend continuation'} "
                f"above EMA4 ({ema4:.2f})"
            )
            decision = _build_mini_entry_candidate(
                side="LONG",
                ticker=ticker,
                telemetry=telemetry,
                asset=asset,
                risk=risk,
                metrics=metrics,
                avail_cash_sek=avail_cash_sek,
                regime="Reversal_Base_Building" if (is_pd_low_sweep and not long_breakout) else "Momentum_Breakout",
                setup_note=setup_note,
            )
        elif short_breakout or is_pd_high_sweep:
            setup_note = (
                f"Bearish {'sweep rejection' if is_pd_high_sweep and not short_breakout else 'downtrend continuation'} "
                f"below EMA4 ({ema4:.2f})"
            )
            decision = _build_mini_entry_candidate(
                side="SHORT",
                ticker=ticker,
                telemetry=telemetry,
                asset=asset,
                risk=risk,
                metrics=metrics,
                avail_cash_sek=avail_cash_sek,
                regime="Reversal_Base_Building" if (is_pd_high_sweep and not short_breakout) else "Downtrend_Continuation",
                setup_note=setup_note,
            )

    # 4. Deterministic HOLD fallback
    if decision is None:
        if held_minis > 0:
            inst = str((asset.get("mini_short_instrument") if is_held_short else asset.get("mini_instrument")) or ticker)
            if is_held_short:
                anchor = pd_high if (0 <= (pd_high - spot) <= 2.5 * atr14) else max(vwap, high)
                stop_u = round(max(anchor, high) + stop_buf * atr14, 2)
                target_u = round(max(spot - 2.5 * atr14, 0.01), 2)
                trig_sek = round(((fin_short - stop_u) * fx) / parity, 2) if fin_short > stop_u else None
            else:
                anchor = pd_low if (0 <= (spot - pd_low) <= 2.5 * atr14) else min(vwap, low)
                stop_u = round(min(anchor, low) - stop_buf * atr14, 2)
                target_u = asset_tp if asset_tp > spot else round(spot + 2.5 * atr14, 2)
                trig_sek = round(((stop_u - fin_long) * fx) / parity, 2) if fin_long > 0 and stop_u > fin_long else None
            sell_sek = round(trig_sek - 0.20, 2) if trig_sek is not None else None
            hold_qty = held_minis
        else:
            inst = ticker
            anchor = pd_low if (0 <= (spot - pd_low) <= 2.5 * atr14) else min(vwap, low)
            stop_u = hard_stop if (0 < hard_stop < spot) else round(min(anchor, low) - stop_buf * atr14, 2)
            target_u = asset_tp if asset_tp > spot else round(spot + 2.5 * atr14, 2)
            trig_sek = None
            sell_sek = None
            hold_qty = 0

        hold_draft = TradeDecisionDraft(
            ticker=ticker,
            market_regime=regime,  # type: ignore[arg-type]
            action="HOLD",
            instrument=inst,
            quantity=hold_qty,
            limit_price=None,
            avanza_stop_trigger_sek=trig_sek,
            avanza_stop_sell_sek=sell_sek,
            underlying_stop_price=stop_u,
            take_profit_target=target_u,
            net_risk_reward_ratio=0.0,
            overnight_gap_warning=False,
            rationale="No high-conviction entry criteria met; standing aside.",
        )
        rr_audit = compute_verified_fee_adjusted_rr(hold_draft, telemetry, asset, risk)
        hold_note = (
            "Exchange is currently closed; waiting for live market session."
            if market_open is False
            else "Setup criteria not met; waiting for clean trend/volume alignment."
        )
        decision = TradeDecision(
            ticker=ticker,
            market_regime=regime,  # type: ignore[arg-type]
            action="HOLD",
            instrument=inst,
            quantity=hold_qty,
            limit_price=None,
            avanza_stop_trigger_sek=trig_sek,
            avanza_stop_sell_sek=sell_sek,
            underlying_stop_price=stop_u,
            take_profit_target=target_u,
            net_risk_reward_ratio=float(rr_audit["python_verified_net_rr"]),
            overnight_gap_warning=False,
            rationale=hold_note,
            confidence_score=65 if market_open is False else 60,
            critic_verdict="APPROVED",
            critic_notes="",
            rr_applicable=bool(rr_audit.get("rr_applicable", True)),
            data_timestamp_cet=data_ts_cet,
            market_open=market_open,
        )

    decision.plain_reasons = build_plain_english_reasons(decision, telemetry, asset, metrics, risk)
    decision.avanza_execution_guide = format_avanza_order_instructions(
        decision=decision,
        telemetry=telemetry,
        asset=asset,
        metrics=metrics,
        risk=risk,
        account_id=account_id,
    )
    return decision


def _format_verdict_banner(decision: TradeDecision, asset: dict, metrics: dict) -> str:
    """Formats the bottom-line-first HTML verdict headline."""
    cur = str(asset.get("currency", "USD"))
    act = decision.action
    if act == "HOLD":
        return "⏸️ <b>WAIT / DO NOT TRADE (HOLD)</b>"
    if act == "BUY_MINI_LONG":
        px = float(decision.limit_price or metrics.get("mini_theoretical_sek", 0.0))
        est_sek = int(round(decision.quantity * px, 0))
        return f"🟢 <b>BUY MINI LONG — {decision.quantity} units (~{est_sek:,} SEK)</b>"
    if act == "BUY_MINI_SHORT":
        px = float(decision.limit_price or metrics.get("mini_short_theoretical_sek", 0.0))
        est_sek = int(round(decision.quantity * px, 0))
        return f"🔴 <b>BUY MINI SHORT — {decision.quantity} units (~{est_sek:,} SEK)</b>"
    if act == "BUY_SHARES":
        px = float(decision.limit_price or 0.0)
        est_val = round(decision.quantity * px, 2)
        disp = f"${est_val:,.2f}" if cur == "USD" else f"{int(round(est_val, 0)):,} SEK"
        return f"🟢 <b>BUY {decision.quantity} SHARES (~{disp})</b>"
    if act == "SELL_SHARES":
        return f"🚨 <b>SELL ALL {decision.quantity} SHARES</b>"
    if act == "SELL_MINI_LONG":
        return f"🚨 <b>SELL MINI LONG — {decision.quantity} units</b>"
    if act == "SELL_MINI_SHORT":
        return f"🚨 <b>SELL MINI SHORT — {decision.quantity} units</b>"
    return f"<b>{html.escape(str(act))}</b>"


def format_trade_alert_html(
    decision: TradeDecision,
    telemetry: dict,
    portfolio: dict,
    asset: dict,
    metrics: dict,
    risk: dict,
    source_label: str = "15m Signal",
    cash_override: Optional[float] = None,
) -> str:
    """
    Formats a plain-English, bottom-line-first Telegram HTML brief with escaped dynamic text,
    zero raw LaTeX, zero internal jargon tokens, and verdict-gated Avanza next steps.
    """
    cur = str(asset.get("currency", "USD"))
    spot = float(telemetry.get("close", 0.0))
    spot_disp = _fmt_price(spot, cur)
    esc_ticker = html.escape(str(decision.ticker))
    esc_src = _sanitize_plain_text(source_label or "15m Signal")

    chg = telemetry.get("day_change_pct")
    chg_txt = f" ({float(chg):+.2f}%)" if chg is not None else ""

    market_open = decision.market_open if decision.market_open is not None else telemetry.get("market_open")
    if market_open is True:
        mkt_state = "Market Open"
    elif market_open is False:
        mkt_state = "Market Closed"
    else:
        mkt_state = ""

    ts_raw = decision.data_timestamp_cet or telemetry.get("data_timestamp_cet") or telemetry.get("time") or ""
    ts_txt = html.escape(str(ts_raw))
    meta_parts = [p for p in (esc_src, mkt_state, ts_txt) if p]
    meta_line = " | ".join(meta_parts)

    verdict_banner = _format_verdict_banner(decision, asset, metrics)

    held_shares = int(asset.get("regular_shares_held", 0))
    held_minis = int(asset.get("mini_units_held", 0))
    pos_parts: list[str] = []
    if held_shares > 0:
        pos_parts.append(f"{held_shares} shares held")
    if held_minis > 0:
        pos_parts.append(f"{held_minis} Mini units held")
    pos_txt = ", ".join(pos_parts) if pos_parts else "No open position"

    cash_sek = float(
        cash_override
        if cash_override is not None
        else portfolio.get("cash_reserves", {}).get("available_tactical_mini_sek", 0.0)
    )
    rr_part = ""
    if decision.rr_applicable and decision.net_risk_reward_ratio > 0 and not decision.action.startswith("SELL"):
        rr_part = f" | Net R:R: <code>{decision.net_risk_reward_ratio:.2f}:1</code>"

    snapshot_line = (
        f"<b>{esc_ticker}</b>: <code>{spot_disp}</code>{html.escape(chg_txt)} | "
        f"{html.escape(pos_txt)} | Cash: <code>{int(round(cash_sek)):,} SEK</code>{rr_part}"
    )

    lines = [
        verdict_banner,
        f"<i>{meta_line}</i>",
        snapshot_line,
    ]

    if decision.catalyst_sentiment or decision.catalyst_summary:
        sent_label = _sanitize_plain_text(decision.catalyst_sentiment.replace("_", " ").title())
        summ_txt = _sanitize_plain_text(decision.catalyst_summary)
        cat_body = f"{sent_label}: {summ_txt}" if (sent_label and summ_txt) else (sent_label or summ_txt)
        if cat_body:
            lines.append(f"<b>News Context:</b> {cat_body}")

    reasons = decision.plain_reasons or build_plain_english_reasons(decision, telemetry, asset, metrics, risk)
    bullet_lines = "\n".join(f"• {_sanitize_plain_text(r)}" for r in reasons[:3] if str(r).strip())
    if bullet_lines:
        lines.append(f"<b>Why:</b>\n{bullet_lines}")

    # Verdict-gated Next Step: NEVER emit /fill_mini or entry order steps on HOLD
    if decision.action == "HOLD":
        if decision.ratchet_triggered and held_minis > 0 and decision.avanza_stop_trigger_sek:
            trig = float(decision.avanza_stop_trigger_sek)
            sell = float(decision.avanza_stop_sell_sek or round(trig - 0.20, 2))
            lines.append(
                "<b>Next Step (Raise Stop-Loss to Lock Profit):</b>\n"
                f"• Your open Mini hit +0.85R. Update Stop-Loss on <code>{html.escape(str(decision.instrument))}</code>:\n"
                f"• <b>Triggerkurs:</b> <code>{trig:.2f} SEK</code> | <b>Säljkurs:</b> <code>{sell:.2f} SEK</code>"
            )
        elif held_minis > 0:
            lines.append("<b>Next Step:</b> Hold your open Mini position and keep your existing Avanza Stop-Loss active.")
        elif held_shares > 0:
            hard_stop = float(asset.get("hard_stop_price") or decision.underlying_stop_price or 0.0)
            if hard_stop > 0:
                lines.append(
                    f"<b>Next Step:</b> No action needed. Hold shares and keep protective stop at <code>{_fmt_price(hard_stop, cur)}</code>."
                )
            else:
                lines.append("<b>Next Step:</b> No action needed right now.")
        else:
            lines.append("<b>Next Step:</b> No order to place. Wait for the next confirmed setup.")
    else:
        guide = decision.avanza_execution_guide or format_avanza_order_instructions(
            decision=decision,
            telemetry=telemetry,
            asset=asset,
            metrics=metrics,
            risk=risk,
            account_id=str(portfolio.get("account_id", "ISK 5872428")),
        )
        if guide:
            lines.append(guide)

    return "\n\n".join(lines)
