#!/usr/bin/env python3
"""
60-Day (range=60d, 15m) 9-Stock Out-of-Sample Statistical Validation & CSCV/PBO/PSR/DSR Runner.

Replays all 9 watchlist tickers (NVDA, TSLA, AMD, GOOGL, AMZN, META, MSFT, AAPL, ERIC B)
Strictly Point-in-Time (PIT) with ZERO live Google Search or future leakage:
  - Causal Time-of-Day Normalized RVOL (`tod_rvol` vs trailing 10 days at same bar slot)
  - Causal 5-day Kaufman Efficiency Ratio (`er_5d` on prior completed daily closes [d-6 .. d-1])
  - Updated Guardrail Rule #6: Combined `EMA4 + VWAP Failure` exit (matching guardrails.py)
  - Bidirectional Avanza MINI L / MINI S +450 SEK 1R Risk-Parity sizing + +0.85R -> +0.15R BE Ratchet
  - Benchmarks 8 symmetric trials ({IMOM, ORB_SWEEP} x {UNCAPPED_TRAILING, CAPPED_2R_TP} x {GATED, UNGATED})
    computing Combinatorially Symmetric Cross-Validation (CSCV) PBO (`C(8,4)=70` splits),
    Probabilistic Sharpe Ratio (PSR), Deflated Sharpe Ratio (DSR, N=8), and Wilson 95% LB Win Rate.
  - Evaluates chronological global-portfolio execution enforcing `max_concurrent_leveraged_minis=1`.
"""
import itertools
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any
from zoneinfo import ZoneInfo

import requests
from traderagent.domain.models import TradeDecisionDraft
from traderagent.domain.quant_math import (
    compute_statistical_confidence,
    compute_verified_fee_adjusted_rr,
)

CET = ZoneInfo("Europe/Stockholm")
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BENCHMARK_DIR = DATA_DIR / "benchmarks"

UNIVERSE = [
    {"ticker": "NVDA", "yahoo": "NVDA", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "TSLA", "yahoo": "TSLA", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "AMD", "yahoo": "AMD", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "GOOGL", "yahoo": "GOOGL", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "AMZN", "yahoo": "AMZN", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "META", "yahoo": "META", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "MSFT", "yahoo": "MSFT", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "AAPL", "yahoo": "AAPL", "currency": "USD", "parity": 10.0, "exchange": "NASDAQ"},
    {"ticker": "ERIC B", "yahoo": "ERIC-B.ST", "currency": "SEK", "parity": 1.0, "exchange": "OMXSTO"},
]


def load_60d_15m_bars(spec: dict[str, Any]) -> tuple[list[dict], dict[str, list[dict]]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{spec['yahoo']}?interval=15m&range=60d"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    timestamps = res["timestamp"]
    q = res["indicators"]["quote"][0]

    bars: list[dict[str, Any]] = []
    for ts, o, h, l, c, v in zip(timestamps, q["open"], q["high"], q["low"], q["close"], q["volume"]):
        if o is None or h is None or l is None or c is None:
            continue
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        dt_cet = dt_utc.astimezone(CET)
        bars.append({
            "ts": int(ts),
            "iso": dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": dt_cet.strftime("%Y-%m-%d"),
            "hhmm_cet": dt_cet.strftime("%H:%M"),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(v or 0.0),
            "ticker": spec["ticker"],
        })

    k4 = 2.0 / 5.0
    k50 = 2.0 / 51.0
    ema4 = bars[0]["close"]
    ema50 = bars[0]["close"]
    trs: list[float] = []
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]

    for i, b in enumerate(bars):
        if i > 0:
            ema4 = (b["close"] * k4) + (ema4 * (1.0 - k4))
            ema50 = (b["close"] * k50) + (ema50 * (1.0 - k50))
            prev_c = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - prev_c), abs(b["low"] - prev_c))
            diff = b["close"] - prev_c
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        else:
            tr = b["high"] - b["low"]
        trs.append(tr)
        b["ema4"] = round(ema4, 4)
        b["ema50"] = round(ema50, 4)

    atr14 = sum(trs[:14]) / 14.0
    avg_g = sum(gains[1:15]) / 14.0
    avg_l = sum(losses[1:15]) / 14.0
    for i, b in enumerate(bars):
        if i >= 14:
            atr14 = ((atr14 * 13.0) + trs[i]) / 14.0
            avg_g = ((avg_g * 13.0) + gains[i]) / 14.0
            avg_l = ((avg_l * 13.0) + losses[i]) / 14.0
        rs = (avg_g / avg_l) if avg_l > 0 else 100.0
        b["atr14"] = round(max(atr14, 1e-4), 4)
        b["rsi14"] = round(100.0 - (100.0 / (1.0 + rs)), 2)

    bars_by_day: dict[str, list[dict]] = {}
    for b in bars:
        bars_by_day.setdefault(b["date"], []).append(b)

    dates = sorted(bars_by_day.keys())
    daily_closes = [bars_by_day[d][-1]["close"] for d in dates]
    slot_vols: dict[int, list[float]] = {}

    for d_idx, d in enumerate(dates):
        day_bars = bars_by_day[d]
        prev_bars = bars_by_day[dates[d_idx - 1]] if d_idx > 0 else day_bars
        pd_low = round(min(x["low"] for x in prev_bars), 2)
        pd_high = round(max(x["high"] for x in prev_bars), 2)
        or_high = round(day_bars[0]["high"], 2)
        or_low = round(day_bars[0]["low"], 2)

        if d_idx >= 6:
            d_net = abs(daily_closes[d_idx - 1] - daily_closes[d_idx - 6])
            d_path = sum(abs(daily_closes[k] - daily_closes[k - 1]) for k in range(d_idx - 5, d_idx))
            er_5d = round(min(1.0, (d_net / d_path)) if d_path > 0 else 0.30, 3)
        else:
            er_5d = 0.30

        cum_pv = 0.0
        cum_v = 0.0
        for idx, b in enumerate(day_bars):
            hist_slot = slot_vols.get(idx, [])
            avg_slot = mean(hist_slot[-10:]) if len(hist_slot) >= 3 else b["volume"]
            tod_rvol = round((b["volume"] / avg_slot) if avg_slot > 0 else 1.0, 2)
            slot_vols.setdefault(idx, []).append(b["volume"])

            hlc3 = (b["high"] + b["low"] + b["close"]) / 3.0
            cum_pv += hlc3 * b["volume"]
            cum_v += b["volume"]
            b["vwap"] = round((cum_pv / cum_v) if cum_v > 0 else b["close"], 4)
            b["or_high"] = or_high
            b["or_low"] = or_low
            b["pd_low"] = pd_low
            b["pd_high"] = pd_high
            b["efficiency_ratio"] = er_5d
            b["rvol"] = tod_rvol
            b["time"] = b["iso"]

    return bars, bars_by_day


def _evaluate_signal(
    idx: int,
    b: dict[str, Any],
    day_bars: list[dict[str, Any]],
    spec: dict[str, Any],
    risk: dict[str, Any],
    strategy_family: str,
    use_regime_gate: bool,
) -> dict[str, Any] | None:
    if idx < 1:
        return None
    cutoff_cet = risk["nordic_etp_cutoff_cet"] if spec["currency"] == "USD" else "17:15"
    if b["hhmm_cet"] >= cutoff_cet:
        return None

    min_er = float(risk.get("min_trend_efficiency_ratio", 0.28))
    min_rvol = float(risk.get("min_rvol_for_breakout", 1.05))
    if use_regime_gate and b["efficiency_ratio"] < min_er:
        return None

    spot = b["close"]
    ema4 = b["ema4"]
    vwap = b["vwap"]
    atr = b["atr14"]
    rvol = b["rvol"]

    if strategy_family == "IMOM":
        # Gao, Han, Li & Zhou (2018) Market Intraday Momentum continuation:
        # Afternoon session window after morning regime has established directional drive >= 1.2 ATR
        imom_lo, imom_hi = (22, 30) if spec["currency"] == "SEK" else (9, 16)
        if not (imom_lo <= idx <= imom_hi):
            return None
        if use_regime_gate and rvol < min_rvol:
            return None
        intraday_drive = (spot - day_bars[0]["open"]) / atr
        if intraday_drive >= 1.2 and spot > vwap and spot > ema4:
            return {"side": "LONG", "setup": "IMOM_AFTERNOON_LONG", "is_breakout": True}
        if intraday_drive <= -1.2 and spot < vwap and spot < ema4:
            return {"side": "SHORT", "setup": "IMOM_AFTERNOON_SHORT", "is_breakout": True}
        return None

    # Legacy ORB + VWAP Breakout (Zarattini & Aziz) baseline for head-to-head comparison
    if spot > b["or_high"] and spot > vwap and spot > ema4 and (not use_regime_gate or rvol >= min_rvol):
        return {"side": "LONG", "setup": "ORB_VWAP_BREAKOUT_LONG", "is_breakout": True}
    if spot < b["or_low"] and spot < vwap and spot < ema4 and (not use_regime_gate or rvol >= min_rvol):
        return {"side": "SHORT", "setup": "ORB_VWAP_BREAKDOWN_SHORT", "is_breakout": True}
    return None


def _build_entry_order(
    b: dict[str, Any],
    sig: dict[str, Any],
    spec: dict[str, Any],
    risk: dict[str, Any],
    fin_long: float,
    fin_short: float,
) -> dict[str, Any] | None:
    fx = 10.16 if spec["currency"] == "USD" else 1.0
    parity = spec["parity"]
    spot = b["close"]
    atr = b["atr14"]
    side = sig["side"]

    if side == "LONG":
        cand_stop = round(spot - 1.5 * atr, 2)
        risk_u = max(spot - cand_stop, 0.05)
        cand_target = round(spot + 2.0 * risk_u, 2)
        fin_lvl = fin_long
        mini_sek = ((spot - fin_lvl) * fx) / parity
        stop_sek = ((cand_stop - fin_lvl) * fx) / parity
        action_name = "BUY_MINI_LONG"
        inst_name = f"MINI L {spec['ticker']} AVA"
        asset_sim = {
            "currency": spec["currency"],
            "regular_shares_held": 0,
            "blended_avg_price": 0.0,
            "mini_financing_level": fin_lvl,
            "mini_parity": parity,
        }
    else:
        cand_stop = round(spot + 1.5 * atr, 2)
        risk_u = max(cand_stop - spot, 0.05)
        cand_target = round(spot - 2.0 * risk_u, 2)
        fin_lvl = fin_short
        mini_sek = ((fin_lvl - spot) * fx) / parity
        stop_sek = ((fin_lvl - cand_stop) * fx) / parity
        action_name = "BUY_MINI_SHORT"
        inst_name = f"MINI S {spec['ticker']} AVA"
        asset_sim = {
            "currency": spec["currency"],
            "regular_shares_held": 0,
            "blended_avg_price": 0.0,
            "mini_financing_level": fin_long,
            "mini_short_financing_level": fin_lvl,
            "mini_parity": parity,
        }

    if mini_sek <= 0.5:
        return None

    draft = TradeDecisionDraft(
        ticker=spec["ticker"],
        market_regime="Momentum_Breakout",
        action=action_name,
        instrument=inst_name,
        quantity=10,
        limit_price=round(mini_sek, 2),
        avanza_stop_trigger_sek=round(stop_sek, 2),
        avanza_stop_sell_sek=round(stop_sek - 0.2, 2),
        underlying_stop_price=cand_stop,
        take_profit_target=cand_target,
        net_risk_reward_ratio=2.0,
        overnight_gap_warning=False,
        rationale=sig["setup"],
    )
    rr_audit = compute_verified_fee_adjusted_rr(draft, b, asset_sim, risk)
    if rr_audit["python_verified_net_rr"] < risk["min_net_rr_ratio"]:
        return None

    max_risk_sek = float(risk.get("max_risk_per_trade_sek", 450.0))
    per_unit_risk = max((mini_sek - stop_sek) + 0.20, 0.25)
    units = min(max(1, int(max_risk_sek / per_unit_risk)), max(1, int((14548.0 * 0.95) / mini_sek)))
    return {
        "ticker": spec["ticker"],
        "side": side,
        "setup": sig["setup"],
        "fin_lvl": fin_lvl,
        "entry_spot": spot,
        "entry_mini_sek": mini_sek,
        "stop_u": cand_stop,
        "r_u": risk_u,
        "target_u": cand_target,
        "units": units,
        "ratcheted": False,
        "entry_time": b["hhmm_cet"],
    }


def _check_exit(
    pos: dict[str, Any],
    b: dict[str, Any],
    idx: int,
    total_day_bars: int,
    cutoff_cet: str,
    risk: dict[str, Any],
    exit_mode: str,
) -> tuple[str | None, float]:
    ratchet_r = float(risk.get("breakeven_ratchet_r_mult", 0.85))
    lock_r = float(risk.get("breakeven_lock_r_mult", 0.15))
    ema4_mult = float(risk["ema4_failure_exit_atr_mult"])
    spot = b["close"]
    ema4 = b["ema4"]
    vwap = b["vwap"]
    atr = b["atr14"]
    hhmm = b["hhmm_cet"]

    if pos["side"] == "LONG":
        if b["low"] <= pos["stop_u"]:
            ex_s = min(pos["stop_u"], b["open"]) if b["open"] < pos["stop_u"] else pos["stop_u"]
            return ("RATCHET_BE_STOP" if pos["ratcheted"] else "STRUCTURAL_STOP_HIT"), ex_s
        if not pos["ratcheted"] and max(b["high"], spot) >= pos["entry_spot"] + (ratchet_r * pos["r_u"]):
            be_stop = round(pos["entry_spot"] + (lock_r * pos["r_u"]), 4)
            if be_stop > pos["stop_u"]:
                pos["stop_u"] = be_stop
                pos["ratcheted"] = True
        if exit_mode == "CAPPED_2R_TP" and b["high"] >= pos["target_u"]:
            return "TAKE_PROFIT_2R", pos["target_u"]
        if spot < (ema4 - ema4_mult * atr) and spot < vwap:
            return "EMA4_VWAP_FAILURE_EXIT", spot
        if hhmm >= cutoff_cet or idx == total_day_bars - 1:
            return "EOD_ETP_CLOSE", spot
    else:
        if b["high"] >= pos["stop_u"]:
            ex_s = max(pos["stop_u"], b["open"]) if b["open"] > pos["stop_u"] else pos["stop_u"]
            return ("RATCHET_BE_STOP" if pos["ratcheted"] else "STRUCTURAL_STOP_HIT"), ex_s
        if not pos["ratcheted"] and min(b["low"], spot) <= pos["entry_spot"] - (ratchet_r * pos["r_u"]):
            be_stop = round(pos["entry_spot"] - (lock_r * pos["r_u"]), 4)
            if be_stop < pos["stop_u"]:
                pos["stop_u"] = be_stop
                pos["ratcheted"] = True
        if exit_mode == "CAPPED_2R_TP" and b["low"] <= pos["target_u"]:
            return "TAKE_PROFIT_2R", pos["target_u"]
        if spot > (ema4 + ema4_mult * atr) and spot > vwap:
            return "EMA4_VWAP_FAILURE_EXIT", spot
        if hhmm >= cutoff_cet or idx == total_day_bars - 1:
            return "EOD_ETP_CLOSE", spot

    return None, spot


def _settle_trade(pos: dict[str, Any], ex_s: float, exit_reason: str, d: str, exit_time: str, spec: dict[str, Any], risk: dict[str, Any]) -> dict[str, Any]:
    fx = 10.16 if spec["currency"] == "USD" else 1.0
    parity = spec["parity"]
    spread_pct = float(risk.get("avanza_mini_spread_pct", 0.0038))
    if pos["side"] == "LONG":
        exit_mini = ((ex_s - pos["fin_lvl"]) * fx) / parity
    else:
        exit_mini = ((pos["fin_lvl"] - ex_s) * fx) / parity
    spread_slip_u = max(pos["entry_mini_sek"] * spread_pct, 0.02) + 0.02
    net_pnl = round(((exit_mini - pos["entry_mini_sek"]) - spread_slip_u) * pos["units"], 2)
    if (pos["entry_mini_sek"] * pos["units"]) < 1000.0:
        net_pnl = round(net_pnl - 19.0, 2)
    return {
        "ticker": spec["ticker"],
        "date": d,
        "side": pos["side"],
        "setup": pos["setup"],
        "entry_time": pos["entry_time"],
        "exit_time": exit_time,
        "entry_spot": round(pos["entry_spot"], 2),
        "exit_spot": round(ex_s, 2),
        "units": pos["units"],
        "ratcheted": pos["ratcheted"],
        "exit_reason": exit_reason,
        "net_pnl_sek": net_pnl,
    }


def simulate_ticker_config(
    spec: dict[str, Any],
    bars_by_day: dict[str, list[dict]],
    risk: dict[str, Any],
    *,
    strategy_family: str,
    exit_mode: str,
    use_regime_gate: bool,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    dates = sorted(bars_by_day.keys())[10:]  # 10-day warmup for TOD RVOL & 5-day ER
    cutoff_cet = risk["nordic_etp_cutoff_cet"] if spec["currency"] == "USD" else "17:15"
    trades: list[dict[str, Any]] = []
    daily_pnl_map: dict[str, float] = {d: 0.0 for d in dates}

    for d in dates:
        day_bars = bars_by_day[d]
        if len(day_bars) < 5:
            continue
        day_open = day_bars[0]["open"]
        fin_long = round(day_open * 0.78, 2)
        fin_short = round(day_open * 1.25, 2)

        active_pos: dict[str, Any] | None = None
        trades_today = 0
        realized_today = 0.0
        last_stopout_ts = 0

        for idx, b in enumerate(day_bars):
            if active_pos is not None:
                reason, ex_s = _check_exit(active_pos, b, idx, len(day_bars), cutoff_cet, risk, exit_mode)
                if reason:
                    tr = _settle_trade(active_pos, ex_s, reason, d, b["hhmm_cet"], spec, risk)
                    trades.append(tr)
                    daily_pnl_map[d] = round(daily_pnl_map[d] + tr["net_pnl_sek"], 2)
                    realized_today = round(realized_today + tr["net_pnl_sek"], 2)
                    if tr["net_pnl_sek"] < 0:
                        last_stopout_ts = b["ts"]
                    active_pos = None
                continue

            if trades_today >= risk["max_daily_trades"] or realized_today <= risk["daily_loss_lockout_sek"]:
                continue
            if last_stopout_ts > 0 and ((b["ts"] - last_stopout_ts) / 60.0) < risk["cooldown_minutes_after_stopout"]:
                continue

            sig = _evaluate_signal(idx, b, day_bars, spec, risk, strategy_family, use_regime_gate)
            if not sig:
                continue
            order = _build_entry_order(b, sig, spec, risk, fin_long, fin_short)
            if not order:
                continue
            active_pos = order
            trades_today += 1

    return trades, daily_pnl_map


def compute_cscv_pbo(daily_pnl_matrix: list[list[float]], s_blocks: int = 8) -> dict[str, Any]:
    """
    Bailey, Borwein, Lopez de Prado & Zhu (2017) Combinatorially Symmetric Cross-Validation (CSCV).
    Splits T daily returns across N trials into `s_blocks` contiguous blocks and evaluates C(S, S/2) partitions.
    """
    t_days = len(daily_pnl_matrix)
    n_strats = len(daily_pnl_matrix[0]) if t_days else 0
    if t_days < s_blocks * 2 or n_strats < 2:
        return {"cscv_blocks": s_blocks, "cscv_combinations": 0, "pbo_pct": 50.0, "median_oos_logit": 0.0}

    block_size = t_days // s_blocks
    blocks = [daily_pnl_matrix[i * block_size : (i + 1) * block_size] for i in range(s_blocks)]
    half = s_blocks // 2
    logits: list[float] = []
    overfit_count = 0

    def _sharpe_vec(rows: list[list[float]]) -> list[float]:
        out: list[float] = []
        for k in range(n_strats):
            col = [r[k] for r in rows]
            mu = mean(col)
            sd = stdev(col) if len(col) > 1 else 0.0
            out.append((mu / sd) if sd > 0 else (1e-6 if mu > 0 else -1e-6 if mu < 0 else 0.0))
        return out

    combos = list(itertools.combinations(range(s_blocks), half))
    for is_idx in combos:
        oos_idx = [j for j in range(s_blocks) if j not in is_idx]
        is_sr = _sharpe_vec([row for j in is_idx for row in blocks[j]])
        oos_sr = _sharpe_vec([row for j in oos_idx for row in blocks[j]])
        best_is = max(range(n_strats), key=lambda k: is_sr[k])
        chosen_oos = oos_sr[best_is]
        rank = 1 + sum(1 for k in range(n_strats) if oos_sr[k] < chosen_oos)
        rel_w = min(max(rank / (n_strats + 1.0), 1e-4), 1.0 - 1e-4)
        logit = math.log(rel_w / (1.0 - rel_w))
        logits.append(logit)
        if logit <= 0.0:
            overfit_count += 1

    sorted_logits = sorted(logits)
    return {
        "cscv_blocks": s_blocks,
        "cscv_combinations": len(combos),
        "pbo_pct": round((overfit_count / len(combos)) * 100.0, 1),
        "median_oos_logit": round(sorted_logits[len(sorted_logits) // 2], 3),
        "mean_oos_logit": round(mean(logits), 3),
    }


def run_global_portfolio_sync_sim(
    all_bars_by_day: dict[str, dict[str, list[dict]]],
    risk: dict[str, Any],
    *,
    strategy_family: str,
    exit_mode: str,
    use_regime_gate: bool,
) -> dict[str, Any]:
    """Chronological global simulator enforcing `max_concurrent_leveraged_minis=1`."""
    all_dates = sorted({d for td in all_bars_by_day.values() for d in td})[10:]
    spec_by_ticker = {s["ticker"]: s for s in UNIVERSE}
    trades: list[dict[str, Any]] = []

    for d in all_dates:
        timeline: list[tuple[int, str, int, list[dict[str, Any]], dict[str, Any]]] = []
        day_fin: dict[str, tuple[float, float]] = {}
        for t_sym, t_days in all_bars_by_day.items():
            db = t_days.get(d, [])
            if len(db) < 5:
                continue
            day_open = db[0]["open"]
            day_fin[t_sym] = (round(day_open * 0.78, 2), round(day_open * 1.25, 2))
            for idx, b in enumerate(db):
                timeline.append((b["ts"], t_sym, idx, db, b))
        timeline.sort(key=lambda x: (x[0], x[1]))

        active_pos: dict[str, Any] | None = None
        trades_today = 0
        realized_today = 0.0
        last_stopout_ts = 0

        for ts, t_sym, idx, db, b in timeline:
            spec = spec_by_ticker[t_sym]
            cutoff_cet = risk["nordic_etp_cutoff_cet"] if spec["currency"] == "USD" else "17:15"
            fin_long, fin_short = day_fin[t_sym]

            if active_pos is not None and active_pos["ticker"] == t_sym:
                reason, ex_s = _check_exit(active_pos, b, idx, len(db), cutoff_cet, risk, exit_mode)
                if reason:
                    tr = _settle_trade(active_pos, ex_s, reason, d, b["hhmm_cet"], spec, risk)
                    trades.append(tr)
                    realized_today = round(realized_today + tr["net_pnl_sek"], 2)
                    if tr["net_pnl_sek"] < 0:
                        last_stopout_ts = ts
                    active_pos = None
                continue

            if active_pos is not None:
                continue
            if trades_today >= risk["max_daily_trades"] or realized_today <= risk["daily_loss_lockout_sek"]:
                continue
            if last_stopout_ts > 0 and ((ts - last_stopout_ts) / 60.0) < risk["cooldown_minutes_after_stopout"]:
                continue

            sig = _evaluate_signal(idx, b, db, spec, risk, strategy_family, use_regime_gate)
            if not sig:
                continue
            order = _build_entry_order(b, sig, spec, risk, fin_long, fin_short)
            if not order:
                continue
            active_pos = order
            trades_today += 1

    pnls = [t["net_pnl_sek"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = round(sum(wins), 2)
    gl = round(abs(sum(losses)), 2)
    return {
        "strategy_family": strategy_family,
        "exit_mode": exit_mode,
        "use_regime_gate": use_regime_gate,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "gross_profit_sek": gw,
        "gross_loss_sek": gl,
        "profit_factor": round(gw / gl, 2) if gl > 0 else 0.0,
        "net_pnl_sek": round(sum(pnls), 2),
        "statistical_confidence": compute_statistical_confidence(pnls, num_trials=8),
        "by_side": {
            "LONG": round(sum(t["net_pnl_sek"] for t in trades if t["side"] == "LONG"), 2),
            "SHORT": round(sum(t["net_pnl_sek"] for t in trades if t["side"] == "SHORT"), 2),
        },
    }


def main() -> None:
    risk = json.loads((DATA_DIR / "risk_profile.json").read_text(encoding="utf-8"))

    print("Fetching 60d 15m OHLCV datasets from Yahoo Finance across 9 tickers (PIT, allow_live_search=False)...")
    all_bars_by_day: dict[str, dict[str, list[dict]]] = {}
    bar_counts: dict[str, int] = {}
    for spec in UNIVERSE:
        bars, b_days = load_60d_15m_bars(spec)
        all_bars_by_day[spec["ticker"]] = b_days
        bar_counts[spec["ticker"]] = len(bars)
        print(f"  • {spec['ticker']:7s} ({spec['currency']}): {len(bars)} bars across {len(b_days)} days")

    all_dates = sorted({d for td in all_bars_by_day.values() for d in td})[10:]

    configs = [
        {"name": f"{sf}_{em}_{'GATED' if rg else 'UNGATED'}", "strategy_family": sf, "exit_mode": em, "use_regime_gate": rg}
        for sf in ("IMOM", "ORB_SWEEP")
        for em in ("UNCAPPED_TRAILING", "CAPPED_2R_TP")
        for rg in (True, False)
    ]

    daily_matrix: list[list[float]] = [[] for _ in all_dates]
    config_summaries: list[dict[str, Any]] = []
    best_per_ticker: dict[str, dict[str, Any]] = {}

    for c in configs:
        all_pnls: list[float] = []
        agg_daily: dict[str, float] = {d: 0.0 for d in all_dates}
        ticker_breakdown: dict[str, dict[str, Any]] = {}

        for spec in UNIVERSE:
            t_sym = spec["ticker"]
            trades, d_map = simulate_ticker_config(
                spec,
                all_bars_by_day[t_sym],
                risk,
                strategy_family=c["strategy_family"],
                exit_mode=c["exit_mode"],
                use_regime_gate=c["use_regime_gate"],
            )
            t_pnls = [t["net_pnl_sek"] for t in trades]
            all_pnls.extend(t_pnls)
            for d, pnl in d_map.items():
                if d in agg_daily:
                    agg_daily[d] += pnl
            wins = [p for p in t_pnls if p > 0]
            losses = [p for p in t_pnls if p <= 0]
            gw = round(sum(wins), 2)
            gl = round(abs(sum(losses)), 2)
            pf = round(gw / gl, 2) if gl > 0 else (round(gw / max(sum(t_pnls), 100.0), 2) if gw > 0 else 0.0)
            ticker_breakdown[t_sym] = {
                "trades": len(t_pnls),
                "wins": len(wins),
                "losses": len(losses),
                "net_pnl_sek": round(sum(t_pnls), 2),
                "profit_factor": pf,
                "stats": compute_statistical_confidence(t_pnls, num_trials=8),
                "long_pnl_sek": round(sum(t["net_pnl_sek"] for t in trades if t["side"] == "LONG"), 2),
                "short_pnl_sek": round(sum(t["net_pnl_sek"] for t in trades if t["side"] == "SHORT"), 2),
            }

        for row_i, d in enumerate(all_dates):
            daily_matrix[row_i].append(round(agg_daily[d], 2))

        wins_all = [p for p in all_pnls if p > 0]
        losses_all = [p for p in all_pnls if p <= 0]
        gw_all = round(sum(wins_all), 2)
        gl_all = round(abs(sum(losses_all)), 2)
        summary = {
            "config": c["name"],
            "strategy_family": c["strategy_family"],
            "exit_mode": c["exit_mode"],
            "use_regime_gate": c["use_regime_gate"],
            "total_trades": len(all_pnls),
            "wins": len(wins_all),
            "losses": len(losses_all),
            "gross_profit_sek": gw_all,
            "gross_loss_sek": gl_all,
            "profit_factor": round(gw_all / gl_all, 2) if gl_all > 0 else 0.0,
            "net_pnl_sek": round(sum(all_pnls), 2),
            "statistical_confidence": compute_statistical_confidence(all_pnls, num_trials=8),
            "per_ticker": ticker_breakdown,
        }
        config_summaries.append(summary)
        if c["name"] == "IMOM_UNCAPPED_TRAILING_GATED":
            best_per_ticker = ticker_breakdown

    cscv_result = compute_cscv_pbo(daily_matrix, s_blocks=8)
    global_imom_uncapped = run_global_portfolio_sync_sim(
        all_bars_by_day, risk, strategy_family="IMOM", exit_mode="UNCAPPED_TRAILING", use_regime_gate=True
    )
    global_imom_capped = run_global_portfolio_sync_sim(
        all_bars_by_day, risk, strategy_family="IMOM", exit_mode="CAPPED_2R_TP", use_regime_gate=True
    )
    global_orb_uncapped = run_global_portfolio_sync_sim(
        all_bars_by_day, risk, strategy_family="ORB_SWEEP", exit_mode="UNCAPPED_TRAILING", use_regime_gate=True
    )

    output = {
        "generated_at_cet": datetime.now(CET).isoformat(timespec="seconds"),
        "pit_constraints": {
            "allow_live_search": False,
            "data_window": "60d x 15m (Yahoo Finance), OOS days after 10-day warmup for TOD RVOL & ER",
            "oos_calendar_days": len(all_dates),
            "bar_counts": bar_counts,
            "spread_model": "0.38% of Mini certificate equity + 0.02 SEK slippage (+19 SEK courtage if <1,000 SEK)",
            "risk_parity_1r_sek": risk["max_risk_per_trade_sek"],
        },
        "cscv_overfit_analysis": cscv_result,
        "global_portfolio_max_1_mini": {
            "imom_uncapped_trailing_gated": global_imom_uncapped,
            "imom_capped_2r_tp_gated": global_imom_capped,
            "orb_uncapped_trailing_gated": global_orb_uncapped,
        },
        "trial_matrix_summary": [
            {k: v for k, v in s.items() if k != "per_ticker"} for s in config_summaries
        ],
        "recommended_config_per_ticker": best_per_ticker,
    }

    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BENCHMARK_DIR / "sixty_day_psr_dsr_validation.json"
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print("\n=== 8-TRIAL 60-DAY STATISTICAL MATRIX ===")
    for s in config_summaries:
        st = s["statistical_confidence"]
        print(
            f"{s['config']:34s} | n={s['total_trades']:3d} | WR={st['raw_win_rate_pct']:4.1f}% "
            f"(WLB={st['wilson_95_lower_wr_pct']:4.1f}%) | PF={s['profit_factor']:4.2f} | "
            f"Net={s['net_pnl_sek']:+9.1f} SEK | PSR={st['psr_pct']:5.1f}% | DSR={st['dsr_pct']:5.1f}%"
        )
    print("\n=== CSCV PBO (C(8,4)=70 splits) ===", cscv_result)
    print("\n=== GLOBAL PORTFOLIO (max_concurrent_leveraged_minis=1) ===")
    for k, v in output["global_portfolio_max_1_mini"].items():
        print(f"  {k:32s}:", {kk: vv for kk, vv in v.items() if kk != "statistical_confidence"}, v["statistical_confidence"])
    print("\n=== PER-TICKER (IMOM_UNCAPPED_TRAILING_GATED) ===")
    for sym, s in best_per_ticker.items():
        st = s["stats"]
        print(
            f"  {sym:7s}: n={s['trades']:2d} (W={s['wins']:2d}/L={s['losses']:2d}) | WR={st['raw_win_rate_pct']:4.1f}% "
            f"(WLB={st['wilson_95_lower_wr_pct']:4.1f}%) | PF={s['profit_factor']:4.2f} | "
            f"Net={s['net_pnl_sek']:+8.1f} SEK (L={s['long_pnl_sek']:+7.1f} / S={s['short_pnl_sek']:+7.1f})"
        )
    print(f"\nSaved full validation report to {out_path}")


if __name__ == "__main__":
    main()
