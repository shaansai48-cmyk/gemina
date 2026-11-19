#!/usr/bin/env python3
"""
Fetches today's real 15m OHLCV chart data for Microsoft (MSFT), Apple (AAPL), and Tesla (TSLA),
computes the exact Pine Script v6 technical telemetry (EMA4, session VWAP, OR High/Low, PD Low, ATR14, RSI14),
and runs the 4-Level Declarative Agentic Quant Committee (L1 -> L2 -> L3a -> L3b -> L4) on each ticker.
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import requests
from gemina.core.tracing import PipelineTracer
from traderagent import compute_quant_metrics, create_trader_agent

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "data" / "benchmarks"


def fetch_15m_telemetry(symbol: str) -> dict:
    """Fetches 5d of 15m bars from Yahoo Finance and computes exact Pine Script v6 indicators."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=15m&range=5d"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
    res = r["chart"]["result"][0]
    timestamps = res["timestamp"]
    q = res["indicators"]["quote"][0]

    bars = []
    for ts, o, h, l, c, v in zip(timestamps, q["open"], q["high"], q["low"], q["close"], q["volume"]):
        if c is None or h is None or l is None or o is None:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        bars.append({
            "ts": ts,
            "iso": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": dt.strftime("%Y-%m-%d"),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(v or 0),
        })

    # Group bars by trading date
    dates = sorted(list({b["date"] for b in bars}))
    today_date = dates[-1]
    prev_date = dates[-2] if len(dates) >= 2 else today_date

    today_bars = [b for b in bars if b["date"] == today_date]
    prev_bars = [b for b in bars if b["date"] == prev_date]

    # 1. Previous Day Low (pd_low) & Previous Day High (pd_high)
    pd_low = min(b["low"] for b in prev_bars)
    pd_high = max(b["high"] for b in prev_bars)

    # 2. Opening Range High / Low (first 15m bar of today's session)
    or_high = today_bars[0]["high"]
    or_low = today_bars[0]["low"]

    # 3. Session VWAP (cumulative sum(hlc3 * vol) / sum(vol) for today)
    cum_pv = 0.0
    cum_vol = 0.0
    for b in today_bars:
        hlc3 = (b["high"] + b["low"] + b["close"]) / 3.0
        cum_pv += hlc3 * b["volume"]
        cum_vol += b["volume"]
    vwap = (cum_pv / cum_vol) if cum_vol > 0 else today_bars[-1]["close"]

    # 4. EMA 4 across the full 15m series
    k = 2.0 / (4.0 + 1.0)
    ema4 = bars[0]["close"]
    for b in bars[1:]:
        ema4 = (b["close"] * k) + (ema4 * (1.0 - k))

    # 5. Wilder's ATR 14 across the full 15m series
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["high"] - b["low"]
        else:
            prev_c = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - prev_c), abs(b["low"] - prev_c))
        trs.append(tr)
    atr14 = sum(trs[:14]) / 14.0
    for tr in trs[14:]:
        atr14 = ((atr14 * 13.0) + tr) / 14.0

    # 6. Wilder's RSI 14 across the full 15m series
    gains, losses = [], []
    for i in range(1, len(bars)):
        diff = bars[i]["close"] - bars[i - 1]["close"]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:14]) / 14.0
    avg_loss = sum(losses[:14]) / 14.0
    for g, ls in zip(gains[14:], losses[14:]):
        avg_gain = ((avg_gain * 13.0) + g) / 14.0
        avg_loss = ((avg_loss * 13.0) + ls) / 14.0
    rs = (avg_gain / avg_loss) if avg_loss > 0 else 100.0
    rsi14 = 100.0 - (100.0 / (1.0 + rs))

    last = today_bars[-1]
    session_high = max(b["high"] for b in today_bars)
    session_low = min(b["low"] for b in today_bars)
    prev_close = prev_bars[-1]["close"]
    day_change_pct = round(((last["close"] - prev_close) / prev_close) * 100.0, 2)

    return {
        "ticker": symbol,
        "time": last["iso"],
        "close": round(last["close"], 2),
        "low": round(last["low"], 2),
        "high": round(last["high"], 2),
        "session_low": round(session_low, 2),
        "session_high": round(session_high, 2),
        "prev_close": round(prev_close, 2),
        "day_change_pct": day_change_pct,
        "ema4": round(ema4, 2),
        "vwap": round(vwap, 2),
        "or_high": round(or_high, 2),
        "or_low": round(or_low, 2),
        "pd_low": round(pd_low, 2),
        "pd_high": round(pd_high, 2),
        "atr14": round(atr14, 2),
        "rsi14": round(rsi14, 2),
        "recent_4_bars": [
            {"time": b["iso"], "close": round(b["close"], 2), "high": round(b["high"], 2), "low": round(b["low"], 2)}
            for b in today_bars[-5:-1]
        ],
    }


def main():
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    plugin = create_trader_agent()
    daemon = plugin.daemon
    portfolio = daemon.repo.load_portfolio()
    risk = daemon.repo.load_risk()

    # Evaluate during active trading window (bypassing post-20:30 CET lockout so L4 evaluates pure chart structure)
    intraday_risk = copy.deepcopy(risk)
    intraday_risk["nordic_etp_cutoff_cet"] = "24:00"
    intraday_risk["us_open_blackout_start_cet"] = "00:00"
    intraday_risk["us_open_blackout_end_cet"] = "00:00"

    results = []
    for sym in ["MSFT", "AAPL", "TSLA"]:
        telem = fetch_15m_telemetry(sym)
        spot = telem["close"]
        atr = telem["atr14"]
        pd_low = telem["pd_low"]

        # Ensure watchlist entry has calibrated stop, target, and Avanza Mini Future parameters for current price level
        fin_lvl = round(spot * 0.78, 2)
        barrier = round(fin_lvl * 1.07, 2)
        hard_stop = round(min(pd_low - 0.20 * atr, spot - 1.5 * atr), 2)
        target_tp = round(max(telem["session_high"] + 1.5 * atr, spot + 3.2 * atr), 2)

        asset = {
            "exchange": "NASDAQ",
            "currency": "USD",
            "regular_shares_held": 0,
            "blended_avg_price": 0.0,
            "hard_stop_price": hard_stop,
            "take_profit_price": target_tp,
            "mini_instrument": f"MINI L {sym} AVA",
            "mini_financing_level": fin_lvl,
            "mini_barrier": barrier,
            "mini_parity": 10.0,
            "mini_units_held": 0,
            "mini_avg_sek": 0.0,
        }

        metrics = compute_quant_metrics(telem, asset)
        tracer = PipelineTracer(
            flow="multi_stock_chart_eval",
            source="yahoo_15m_live_chart",
            prefix=f"chart-{sym}",
            trace_file_path=daemon.config.pipeline_trace_path,
        )
        tracer.add_stage(
            stage="INGEST_ARRIVAL",
            status="LIVE_15M_CHART",
            input_data={"ticker": sym, "bar_ts": telem["time"], "close": spot, "day_chg_pct": telem["day_change_pct"]},
            transformation=f"Fetched 5d 15m chart from Yahoo Finance & computed Pine v6 telemetry for {sym}",
            output_data={
                "close": spot,
                "ema4": telem["ema4"],
                "vwap": telem["vwap"],
                "or_high": telem["or_high"],
                "pd_low": pd_low,
                "atr14": atr,
                "rsi14": telem["rsi14"],
            },
        )

        decision = daemon.committee.evaluate_bar(
            telemetry=telem,
            portfolio=portfolio,
            asset=asset,
            risk=intraday_risk,
            metrics=metrics,
            recent_ticker_bars=telem["recent_4_bars"],
            tracer=tracer,
        )
        trace_record = tracer.complete("COMPLETED")

        results.append({
            "ticker": sym,
            "trace_id": tracer.trace_id,
            "telemetry_15m": telem,
            "asset_config": asset,
            "computed_metrics": metrics,
            "final_decision": decision.model_dump(),
            "total_duration_ms": trace_record["total_duration_ms"],
        })

    out_path = BENCHMARK_DIR / "msft_aapl_tsla_today_analysis.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n✅ Saved MSFT, AAPL, TSLA chart evaluation to: {out_path}\n")

    for r in results:
        t = r["telemetry_15m"]
        d = r["final_decision"]
        m = r["computed_metrics"]
        print("=" * 95)
        print(
            f"📌 {r['ticker']} (15m Close: ${t['close']} | Day: {t['day_change_pct']:+}% | "
            f"Session Range: ${t['session_low']} - ${t['session_high']})"
        )
        print(
            f"   Telemetry : EMA4=${t['ema4']} | VWAP=${t['vwap']} | OR_High=${t['or_high']} | "
            f"OR_Low=${t['or_low']} | PD_Low=${t['pd_low']} | ATR14=${t['atr14']} | RSI14={t['rsi14']}"
        )
        print(
            f"   Mini Math : {r['asset_config']['mini_instrument']} | Theo={m['mini_theoretical_sek']} SEK | "
            f"Eff.Lev={m['mini_effective_leverage']}x | Dist_EMA4={m['distance_to_ema4']:+} | Dist_VWAP={m['distance_to_vwap']:+}"
        )
        print(
            f"   Verdict   : Action={d['action']} ({d['quantity']}x {d['instrument']}) | "
            f"Regime={d['market_regime']} | Confidence={d['confidence_score']}/100 ({d['critic_verdict']})"
        )
        print(
            f"   Risk/Stop : Underlying Stop=${d['underlying_stop_price']} | Target=${d['take_profit_target']} | "
            f"Verified Net R:R = 1:{d['net_risk_reward_ratio']} | Mini Stop={d['avanza_stop_trigger_sek']}/{d['avanza_stop_sell_sek']} SEK"
        )
        print(f"   L3 Critic : {d['critic_notes']}")
        print(f"   Rationale : {d['rationale']}")


if __name__ == "__main__":
    main()
