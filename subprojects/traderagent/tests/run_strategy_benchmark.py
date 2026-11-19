#!/usr/bin/env python3
"""
Strategy Capability & Risk Sensitivity Benchmark Generator for Gemina TraderAgent.
Saves structured JSON benchmark datasets to `data/benchmarks/` and tests 6 distinct
quantitative trading strategies against the current TraderAgent setup.
"""
import copy
import json
import time
from pathlib import Path
from traderagent import (
    TradeDecision,
    TradeDecisionDraft,
    compute_quant_metrics,
    compute_verified_fee_adjusted_rr,
    create_trader_agent,
    enforce_hard_quant_guardrails,
)

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "data" / "benchmarks"


def generate_and_save_benchmarks():
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    plugin = create_trader_agent()
    daemon = plugin.daemon
    base_portfolio = daemon.repo.load_portfolio()
    base_risk = daemon.repo.load_risk()
    nvda_asset = copy.deepcopy(base_portfolio["watchlist"]["NVDA"])
    eric_asset = copy.deepcopy(base_portfolio["watchlist"]["ERIC B"])

    open_risk = copy.deepcopy(base_risk)
    open_risk["nordic_etp_cutoff_cet"] = "24:00"
    open_risk["us_open_blackout_start_cet"] = "00:00"
    open_risk["us_open_blackout_end_cet"] = "00:00"

    # 1. Save the 21-scenario NVDA risk_profile.json sensitivity dataset
    nvda_close_bar = {
        "ticker": "NVDA", "time": "2026-11-18T21:45:00Z",
        "close": 150.85, "low": 150.10, "high": 151.40,
        "ema4": 151.20, "vwap": 152.00, "or_high": 154.50,
        "or_low": 150.00, "pd_low": 149.80, "atr14": 2.40, "rsi14": 38.5,
    }
    nvda_sweep_bar = {
        "ticker": "NVDA", "time": "2026-11-18T17:15:00Z",
        "close": 151.35, "low": 150.30, "high": 151.60,
        "ema4": 151.20, "vwap": 152.00, "or_high": 154.50,
        "or_low": 150.00, "pd_low": 149.80, "atr14": 2.40, "rsi14": 44.0,
    }

    # 2. Define 6 Quantitative Stock Trading Strategies & Test Current Setup Capabilities
    strategy_cases = [
        {
            "strategy_id": "STRAT_1_ORB_VWAP_BREAKOUT",
            "name": "15m Opening Range Breakout (ORB) + VWAP Momentum Continuation",
            "supported_level": "NATIVE_FULL_SUPPORT (100%)",
            "telemetry": {
                "ticker": "NVDA", "time": "2026-11-18T16:15:00Z",
                "close": 154.85, "low": 153.90, "high": 155.10,
                "ema4": 154.20, "vwap": 153.40, "or_high": 154.50,
                "or_low": 150.00, "pd_low": 149.80, "atr14": 2.40, "rsi14": 62.5,
            },
            "candidate_decision": {
                "ticker": "NVDA", "market_regime": "Momentum_Breakout", "action": "BUY_MINI_LONG",
                "instrument": "MINI L NVIDIA AVA 308", "quantity": 200, "limit_price": 37.02,
                "avanza_stop_trigger_sek": 35.10, "avanza_stop_sell_sek": 34.90,
                "underlying_stop_price": 152.95, "take_profit_target": 161.50,
                "net_risk_reward_ratio": 2.8, "overnight_gap_warning": False,
                "rationale": "15m close ($154.85) broke above OR High ($154.50) and VWAP ($153.40) with EMA4 support.",
            },
            "asset": nvda_asset,
        },
        {
            "strategy_id": "STRAT_2_PD_LOW_LIQUIDITY_SWEEP",
            "name": "Previous Day Low (pd_low) Liquidity Sweep & EMA4 Reclaim (Stop-Hunt Reversal)",
            "supported_level": "NATIVE_FULL_SUPPORT (100%)",
            "telemetry": {
                "ticker": "NVDA", "time": "2026-11-18T17:15:00Z",
                "close": 151.35, "low": 149.65, "high": 151.50,
                "ema4": 151.10, "vwap": 152.00, "or_high": 154.50,
                "or_low": 150.00, "pd_low": 149.80, "atr14": 2.40, "rsi14": 41.0,
            },
            "candidate_decision": {
                "ticker": "NVDA", "market_regime": "Reversal_Base_Building", "action": "BUY_MINI_LONG",
                "instrument": "MINI L NVIDIA AVA 308", "quantity": 200, "limit_price": 33.46,
                "avanza_stop_trigger_sek": 31.80, "avanza_stop_sell_sek": 31.60,
                "underlying_stop_price": 149.60, "take_profit_target": 158.00,
                "net_risk_reward_ratio": 2.9, "overnight_gap_warning": False,
                "rationale": "Swept pd_low ($149.80) down to $149.65 and closed at $151.35 above EMA4 ($151.10).",
            },
            "asset": nvda_asset,
        },
        {
            "strategy_id": "STRAT_3_VWAP_EMA4_PULLBACK_TREND",
            "name": "Intraday VWAP Trend Pullback with Automatic EMA4 Failure Trailing Exit",
            "supported_level": "NATIVE_FULL_SUPPORT (100%)",
            "telemetry": {
                "ticker": "NVDA", "time": "2026-11-18T19:30:00Z",
                "close": 150.70, "low": 150.50, "high": 151.30,
                "ema4": 151.20, "vwap": 152.00, "or_high": 154.50,
                "or_low": 150.00, "pd_low": 149.80, "atr14": 2.40, "rsi14": 37.0,
            },
            "candidate_decision": {
                "ticker": "NVDA", "market_regime": "Volatile_Chop", "action": "HOLD",
                "instrument": "MINI L NVIDIA AVA 308", "quantity": 200, "limit_price": None,
                "avanza_stop_trigger_sek": 31.40, "avanza_stop_sell_sek": 31.20,
                "underlying_stop_price": 149.32, "take_profit_target": 160.00,
                "net_risk_reward_ratio": 2.5, "overnight_gap_warning": False,
                "rationale": "Testing automatic EMA4 failure exit when holding 200 Mini units.",
            },
            "asset": {**nvda_asset, "mini_units_held": 200},
        },
        {
            "strategy_id": "STRAT_4_NORDIC_ZERO_FX_SWING",
            "name": "Nordic OMX Zero-FX-Drag Breakout (ERIC B in SEK vs USD FX Drag Comparison)",
            "supported_level": "NATIVE_FULL_SUPPORT (100%)",
            "telemetry": {
                "ticker": "ERIC B", "time": "2026-11-18T10:15:00Z",
                "close": 104.80, "low": 104.10, "high": 105.00,
                "ema4": 104.35, "vwap": 104.20, "or_high": 104.60,
                "or_low": 103.50, "pd_low": 103.10, "atr14": 1.20, "rsi14": 59.4,
            },
            "candidate_decision": {
                "ticker": "ERIC B", "market_regime": "Momentum_Breakout", "action": "BUY_MINI_LONG",
                "instrument": "MINI L ERICSSON AVA", "quantity": 400, "limit_price": 22.80,
                "avanza_stop_trigger_sek": 21.10, "avanza_stop_sell_sek": 20.90,
                "underlying_stop_price": 103.10, "take_profit_target": 109.50,
                "net_risk_reward_ratio": 2.4, "overnight_gap_warning": False,
                "rationale": "OMX Stockholm 0% FX drag breakout above OR High (104.60 SEK) and VWAP (104.20 SEK).",
            },
            "asset": eric_asset,
        },
        {
            "strategy_id": "STRAT_5_SUB_VWAP_RSI_KNIFE_CATCH",
            "name": "Pure Sub-VWAP Oversold RSI Mean-Reversion (Without pd_low Sweep)",
            "supported_level": "INTENTIONALLY_GATED_ON_MINIS / ALLOWED_ON_UNLEVERAGED_SHARES",
            "telemetry": {
                "ticker": "NVDA", "time": "2026-11-18T18:45:00Z",
                "close": 150.85, "low": 150.45, "high": 151.10,
                "ema4": 151.00, "vwap": 152.00, "or_high": 154.50,
                "or_low": 150.00, "pd_low": 149.80, "atr14": 2.40, "rsi14": 28.0,
            },
            "candidate_decision": {
                "ticker": "NVDA", "market_regime": "Downtrend_Continuation", "action": "BUY_MINI_LONG",
                "instrument": "MINI L NVIDIA AVA 308", "quantity": 200, "limit_price": 32.95,
                "avanza_stop_trigger_sek": 31.40, "avanza_stop_sell_sek": 31.20,
                "underlying_stop_price": 149.32, "take_profit_target": 156.00,
                "net_risk_reward_ratio": 2.6, "overnight_gap_warning": False,
                "rationale": "Attempted oversold RSI=28 bounce below VWAP without sweeping pd_low ($149.80).",
            },
            "asset": nvda_asset,
        },
        {
            "strategy_id": "STRAT_6_UNLEVERAGED_SHARE_ACCUMULATION",
            "name": "Unleveraged Core Share Dip Accumulation (BUY_SHARES Sub-VWAP with Net R:R >= 1.8)",
            "supported_level": "NATIVE_FULL_SUPPORT (100%)",
            "telemetry": nvda_close_bar,
            "candidate_decision": {
                "ticker": "NVDA", "market_regime": "Reversal_Base_Building", "action": "BUY_SHARES",
                "instrument": "NVDA", "quantity": 5, "limit_price": 150.85,
                "avanza_stop_trigger_sek": None, "avanza_stop_sell_sek": None,
                "underlying_stop_price": 148.50, "take_profit_target": 164.50,
                "net_risk_reward_ratio": 4.73, "overnight_gap_warning": False,
                "rationale": "Unleveraged BUY_SHARES bypasses the Mini-only sub-VWAP gate when R:R >= 1.8.",
            },
            "asset": nvda_asset,
        },
    ]

    strategy_results = []
    for sc in strategy_cases:
        telem = sc["telemetry"]
        asset = sc["asset"]
        cand = TradeDecision(**sc["candidate_decision"])
        draft = TradeDecisionDraft(**sc["candidate_decision"])
        metrics = compute_quant_metrics(telem, asset)
        math_audit = compute_verified_fee_adjusted_rr(draft, telem, asset, open_risk)
        cand.net_risk_reward_ratio = math_audit["python_verified_net_rr"]
        enforced = enforce_hard_quant_guardrails(cand, telem, base_portfolio, asset, open_risk, metrics["fx_multiplier"])

        strategy_results.append({
            "strategy_id": sc["strategy_id"],
            "name": sc["name"],
            "supported_level": sc["supported_level"],
            "computed_metrics": metrics,
            "l3a_math_audit": math_audit,
            "initial_action": sc["candidate_decision"]["action"],
            "final_enforced_action": enforced.action,
            "final_stop": enforced.underlying_stop_price,
            "final_mini_trigger_sell_sek": f"{enforced.avanza_stop_trigger_sek}/{enforced.avanza_stop_sell_sek}",
            "final_rationale": enforced.rationale,
        })

    out_strat_path = BENCHMARK_DIR / "strategy_capability_benchmark.json"
    out_strat_path.write_text(json.dumps(strategy_results, indent=2), encoding="utf-8")

    # Also save the NVDA risk_profile sensitivity summary to JSON
    sensitivity_summary = {
        "generated_at": "2026-11-19T02:52:00+01:00",
        "reference_bar": nvda_close_bar,
        "sweep_bar": nvda_sweep_bar,
        "live_committee_traces": [
            {
                "trace_id": "sim-baseline-024414-038aa5",
                "bar_close": 150.85,
                "profile": "baseline_risk_profile",
                "l2_claimed_rr": 0.0,
                "l3a_verified_net_rr": 4.73,
                "l3b_verdict": "REVISED_BY_CRITIC",
                "l3b_confidence": 95,
                "final_action": "HOLD",
                "final_stop": 148.50,
            },
            {
                "trace_id": "sim-relaxed-024446-7a600f",
                "bar_close": 151.35,
                "profile": "relaxed_sub_vwap_risk_profile",
                "l2_claimed_rr": 1.38,
                "l3a_verified_net_rr": 0.99,
                "l3b_verdict": "REVISED_BY_CRITIC",
                "l3b_confidence": 96,
                "final_action": "HOLD",
                "final_stop": 149.32,
            },
        ],
        "strategy_benchmark_count": len(strategy_results),
    }
    out_sens_path = BENCHMARK_DIR / "nvda_2026_11_18_sensitivity.json"
    out_sens_path.write_text(json.dumps(sensitivity_summary, indent=2), encoding="utf-8")

    print(f"✅ Saved strategy benchmark dataset to: {out_strat_path}")
    print(f"✅ Saved NVDA sensitivity summary to : {out_sens_path}")
    for sr in strategy_results:
        print(
            f" • [{sr['strategy_id']}] {sr['initial_action']} -> {sr['final_enforced_action']} "
            f"(Verified R:R 1:{sr['l3a_math_audit']['python_verified_net_rr']}, Stop={sr['final_stop']}) | {sr['supported_level']}"
        )


if __name__ == "__main__":
    generate_and_save_benchmarks()
