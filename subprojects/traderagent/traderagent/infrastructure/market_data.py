"""
Live & Cached Yahoo Finance Market Data Adapter (`market_data.py`).
Fetches 15m OHLCV bars and USD/SEK FX rates via Yahoo Finance v8 chart endpoints,
computes Pine Script v6 compatible intraday technical telemetry, and provides
thread-safe TTL caching shared between `/analyze` and the Web Dashboard.
"""
import copy
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from traderagent.domain.models import normalize_ticker

CET = ZoneInfo("Europe/Stockholm")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class MarketDataError(RuntimeError):
    """Raised when live Yahoo Finance market data cannot be retrieved or parsed."""


class YahooFinanceClient:
    """Thread-safe Yahoo Finance v8 chart & FX client with shared TTL caches."""

    _cache_lock = threading.Lock()
    _series_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
    _fx_cache: Optional[tuple[float, float, str]] = None

    def __init__(
        self,
        timeout: float = 6.0,
        series_ttl_sec: float = 45.0,
        fx_ttl_sec: float = 60.0,
        session: Optional[requests.Session] = None,
    ):
        self.timeout = float(timeout)
        self.series_ttl_sec = float(series_ttl_sec)
        self.fx_ttl_sec = float(fx_ttl_sec)
        self._session = session

    @classmethod
    def clear_cache(cls) -> None:
        """Clears shared chart and FX caches (useful in deterministic tests)."""
        with cls._cache_lock:
            cls._series_cache.clear()
            cls._fx_cache = None

    @staticmethod
    def to_yahoo_symbol(ticker: str, exchange: str = "") -> str:
        """Maps canonical watchlist ticker (e.g. '$NVDA', 'ERIC B') to Yahoo Finance symbol."""
        raw = str(ticker or "").strip().lstrip("$").upper()
        if not raw:
            raise MarketDataError("Empty ticker symbol.")
        norm = normalize_ticker(raw)
        if norm == "ERIC B" or raw in ("ERIC B", "ERIC-B", "ERIC-B.ST", "ERIC_B"):
            return "ERIC-B.ST"
        if "." in raw or "=" in raw:
            return raw
        ex_upper = str(exchange or "").strip().upper()
        if ex_upper in ("OMXSTO", "STO", "OMX"):
            return f"{norm.replace(' ', '-')}.ST"
        if " " in norm:
            return f"{norm.replace(' ', '-')}.ST"
        return norm

    def _http_get_json(self, url: str) -> dict[str, Any]:
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        if self._session is not None:
            resp = self._session.get(url, headers=headers, timeout=self.timeout)
        else:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "chart" not in data:
            raise MarketDataError(f"Malformed Yahoo Finance response from {url}")
        return data

    def fetch_usd_sek(self, default: float = 10.16) -> tuple[float, str]:
        """
        Fetches live USD/SEK exchange rate with a 60s thread-safe TTL cache.
        Returns `(rate, source)` where `source` is `'yahoo_live'` or `'fallback'`.
        """
        now_mono = time.monotonic()
        if self.fx_ttl_sec > 0:
            with self._cache_lock:
                if self._fx_cache is not None:
                    cached_ts, cached_rate, cached_src = self._fx_cache
                    if (now_mono - cached_ts) < self.fx_ttl_sec:
                        return cached_rate, cached_src

        url = "https://query1.finance.yahoo.com/v8/finance/chart/USDSEK=X?interval=1d&range=5d"
        try:
            data = self._http_get_json(url)
            chart = data.get("chart") or {}
            if chart.get("error"):
                raise MarketDataError(str(chart["error"]))
            results = chart.get("result") or []
            if not results:
                raise MarketDataError("Empty USDSEK=X chart result")
            res = results[0]
            meta = res.get("meta") or {}
            sym = str(meta.get("symbol", "")).upper()
            if sym and "SEK" not in sym:
                raise MarketDataError(f"Unexpected FX symbol in response: {sym}")

            rate = meta.get("regularMarketPrice")
            if rate is None:
                closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                valid_closes = [float(c) for c in closes if c is not None]
                rate = valid_closes[-1] if valid_closes else None

            if rate is None:
                raise MarketDataError("Missing USD/SEK price")
            parsed_rate = round(float(rate), 4)
            if not (0.5 < parsed_rate < 100.0):
                raise MarketDataError(f"Out-of-range USD/SEK rate: {parsed_rate}")

            if self.fx_ttl_sec > 0:
                with self._cache_lock:
                    YahooFinanceClient._fx_cache = (time.monotonic(), parsed_rate, "yahoo_live")
            return parsed_rate, "yahoo_live"
        except Exception:
            return float(default), "fallback"

    @staticmethod
    def _resolve_exchange_tz(meta: dict[str, Any], yahoo_sym: str) -> ZoneInfo:
        tz_name = str(meta.get("exchangeTimezoneName") or "").strip()
        if not tz_name:
            tz_name = "Europe/Stockholm" if yahoo_sym.endswith(".ST") else "America/New_York"
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return CET

    @staticmethod
    def _infer_market_open(meta: dict[str, Any], ex_tz: ZoneInfo, yahoo_sym: str) -> bool:
        market_state = str(meta.get("marketState") or "").upper()
        if market_state:
            return market_state == "REGULAR"

        now_epoch = time.time()
        reg_period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        start_ts = reg_period.get("start")
        end_ts = reg_period.get("end")
        if start_ts is not None and end_ts is not None:
            try:
                return float(start_ts) <= now_epoch <= float(end_ts)
            except (TypeError, ValueError):
                pass

        now_local = datetime.now(tz=ex_tz)
        if now_local.weekday() >= 5:
            return False
        hhmm = now_local.strftime("%H:%M")
        if yahoo_sym.endswith(".ST") or ex_tz.key == "Europe/Stockholm":
            return "09:00" <= hhmm <= "17:30"
        return "09:30" <= hhmm <= "16:00"

    @staticmethod
    def _parse_bars(res: dict[str, Any], ex_tz: ZoneInfo, canonical_ticker: str) -> list[dict[str, Any]]:
        timestamps = res.get("timestamp") or []
        quote_list = (res.get("indicators") or {}).get("quote") or []
        if not timestamps or not quote_list:
            raise MarketDataError(f"No OHLCV bars returned for {canonical_ticker}")
        q = quote_list[0]
        opens = q.get("open") or []
        highs = q.get("high") or []
        lows = q.get("low") or []
        closes = q.get("close") or []
        volumes = q.get("volume") or [0.0] * len(timestamps)

        bars: list[dict[str, Any]] = []
        for ts, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes):
            if o is None or h is None or l is None or c is None:
                continue
            dt_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            dt_ex = dt_utc.astimezone(ex_tz)
            dt_cet = dt_utc.astimezone(CET)
            bars.append({
                "ts": int(ts),
                "time": dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timestamp_cet": dt_cet.strftime("%Y-%m-%d %H:%M %Z"),
                "date": dt_ex.strftime("%Y-%m-%d"),
                "hhmm_cet": dt_cet.strftime("%H:%M"),
                "open": round(float(o), 2),
                "high": round(float(h), 2),
                "low": round(float(l), 2),
                "close": round(float(c), 2),
                "volume": float(v or 0.0),
                "ticker": canonical_ticker,
            })

        if not bars:
            raise MarketDataError(f"All OHLCV bars were null/empty for {canonical_ticker}")
        bars.sort(key=lambda b: b["ts"])
        return bars

    @staticmethod
    def _compute_pine_indicators(
        bars: list[dict[str, Any]],
        chart_prev_close: Optional[float] = None,
        market_open: bool = False,
    ) -> dict[str, Any]:
        """Computes Pine v6 compatible EMA4/EMA50, Wilder ATR14/RSI14, session VWAP, OR/PD levels, 5d ER & causal RVOL."""
        n = len(bars)
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
            b["ema4"] = round(ema4, 2)
            b["ema50"] = round(ema50, 2)

            prior_vols = [bars[j]["volume"] for j in range(max(0, i - 20), i) if bars[j]["volume"] > 0]
            vol_sma = (sum(prior_vols) / len(prior_vols)) if prior_vols else b["volume"]
            if b["volume"] > 0 and vol_sma > 0:
                b["rvol"] = round(b["volume"] / vol_sma, 2)
            elif b["volume"] <= 0 and vol_sma > 0:
                b["rvol"] = 0.0
            else:
                b["rvol"] = 1.0

        # Wilder's ATR(14) & RSI(14)
        atr14 = trs[0]
        avg_g = 0.0
        avg_l = 0.0
        for i, b in enumerate(bars):
            if i == 0:
                atr14 = trs[0]
                rsi14 = 50.0
            elif i < 14:
                atr14 = sum(trs[: i + 1]) / float(i + 1)
                avg_g = sum(gains[1 : i + 1]) / float(i)
                avg_l = sum(losses[1 : i + 1]) / float(i)
                rsi14 = 50.0 if (avg_g == 0 and avg_l == 0) else (100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + (avg_g / avg_l))))
            elif i == 14:
                atr14 = ((atr14 * 13.0) + trs[i]) / 14.0
                avg_g = sum(gains[1:15]) / 14.0
                avg_l = sum(losses[1:15]) / 14.0
                rsi14 = 50.0 if (avg_g == 0 and avg_l == 0) else (100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + (avg_g / avg_l))))
            else:
                atr14 = ((atr14 * 13.0) + trs[i]) / 14.0
                avg_g = ((avg_g * 13.0) + gains[i]) / 14.0
                avg_l = ((avg_l * 13.0) + losses[i]) / 14.0
                rsi14 = 50.0 if (avg_g == 0 and avg_l == 0) else (100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + (avg_g / avg_l))))

            b["atr14"] = round(max(atr14, 0.01), 2)
            b["rsi14"] = round(rsi14, 2)

        # Daily grouping for session-reset VWAP, OR High/Low, PD High/Low/Close, and 5d Kaufman ER
        bars_by_day: dict[str, list[dict[str, Any]]] = {}
        for b in bars:
            bars_by_day.setdefault(b["date"], []).append(b)

        dates = sorted(bars_by_day.keys())
        daily_closes = [bars_by_day[d][-1]["close"] for d in dates]

        for d_idx, d in enumerate(dates):
            day_bars = bars_by_day[d]
            if d_idx > 0:
                prev_bars = bars_by_day[dates[d_idx - 1]]
                pd_low = round(min(x["low"] for x in prev_bars), 2)
                pd_high = round(max(x["high"] for x in prev_bars), 2)
                pd_close = round(prev_bars[-1]["close"], 2)
            else:
                pd_low = round(min(x["low"] for x in day_bars), 2)
                pd_high = round(max(x["high"] for x in day_bars), 2)
                pd_close = round(float(chart_prev_close), 2) if chart_prev_close else round(day_bars[0]["open"], 2)

            or_high = round(day_bars[0]["high"], 2)
            or_low = round(day_bars[0]["low"], 2)

            completed_closes = list(daily_closes[:d_idx])
            if len(completed_closes) < 6 and chart_prev_close and chart_prev_close > 0:
                completed_closes = [float(chart_prev_close)] + completed_closes
            if len(completed_closes) >= 2:
                win = completed_closes[-6:]
                d_net = abs(win[-1] - win[0])
                d_path = sum(abs(win[k] - win[k - 1]) for k in range(1, len(win)))
                er_5d = round(min(1.0, d_net / d_path) if d_path > 0 else 0.30, 3)
            else:
                er_5d = 0.30

            cum_pv = 0.0
            cum_v = 0.0
            for b in day_bars:
                hlc3 = (b["high"] + b["low"] + b["close"]) / 3.0
                cum_pv += hlc3 * b["volume"]
                cum_v += b["volume"]
                b["vwap"] = round((cum_pv / cum_v) if cum_v > 0 else b["close"], 2)
                b["or_high"] = or_high
                b["or_low"] = or_low
                b["pd_low"] = pd_low
                b["pd_high"] = pd_high
                b["pd_close"] = pd_close
                b["efficiency_ratio"] = er_5d

        latest_day = bars_by_day[dates[-1]]
        last = bars[-1]
        now_epoch = time.time()
        rvol_latest = last["rvol"]
        if n >= 2 and bars[-2]["volume"] > 0:
            if last["volume"] <= 0:
                rvol_latest = bars[-2]["rvol"]
            elif market_open and 0 <= (now_epoch - last["ts"]) < 840 and last["rvol"] < 0.30:
                rvol_latest = bars[-2]["rvol"]

        prev_close = last["pd_close"]
        day_change_pct = round(((last["close"] - prev_close) / prev_close) * 100.0, 2) if prev_close > 0 else 0.0
        session_high = round(max(b["high"] for b in latest_day), 2)
        session_low = round(min(b["low"] for b in latest_day), 2)

        return {
            "ticker": last["ticker"],
            "time": last["time"],
            "data_timestamp_cet": last["timestamp_cet"],
            "market_open": market_open,
            "open": last["open"],
            "high": last["high"],
            "low": last["low"],
            "close": last["close"],
            "ema4": last["ema4"],
            "ema50": last["ema50"],
            "vwap": last["vwap"],
            "or_high": last["or_high"],
            "or_low": last["or_low"],
            "pd_low": last["pd_low"],
            "pd_high": last["pd_high"],
            "pd_close": prev_close,
            "prev_close": prev_close,
            "atr14": last["atr14"],
            "rsi14": last["rsi14"],
            "rvol": rvol_latest,
            "efficiency_ratio": last["efficiency_ratio"],
            "day_change_pct": day_change_pct,
            "session_high": session_high,
            "session_low": session_low,
            "recent_4_bars": [
                {
                    "time": b["time"],
                    "close": b["close"],
                    "high": b["high"],
                    "low": b["low"],
                }
                for b in latest_day[-5:-1]
            ],
        }

    def fetch_15m_series(
        self,
        ticker: str,
        asset: Optional[dict[str, Any]] = None,
        range_str: str = "5d",
        interval: str = "15m",
    ) -> dict[str, Any]:
        """
        Fetches OHLCV chart data for `ticker` with a shared 45s thread-safe TTL cache,
        computes Pine v6 indicators, and returns normalized chart bars + `latest_telemetry`.
        """
        canonical_ticker = normalize_ticker(ticker) or str(ticker).strip().upper()
        exchange = str((asset or {}).get("exchange") or "")
        yahoo_sym = self.to_yahoo_symbol(canonical_ticker, exchange=exchange)
        cache_key = (yahoo_sym, range_str, interval)

        now_mono = time.monotonic()
        if self.series_ttl_sec > 0:
            with self._cache_lock:
                cached = self._series_cache.get(cache_key)
                if cached is not None and (now_mono - cached[0]) < self.series_ttl_sec:
                    return copy.deepcopy(cached[1])

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
            f"?interval={interval}&range={range_str}&includePrePost=false"
        )
        try:
            data = self._http_get_json(url)
            chart = data.get("chart") or {}
            if chart.get("error"):
                raise MarketDataError(f"Yahoo Finance error for {yahoo_sym}: {chart['error']}")
            results = chart.get("result") or []
            if not results:
                raise MarketDataError(f"No Yahoo Finance chart data returned for {yahoo_sym}")
            res = results[0]
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(f"Failed fetching Yahoo Finance data for {yahoo_sym}: {exc}") from exc

        meta = res.get("meta") or {}
        ex_tz = self._resolve_exchange_tz(meta, yahoo_sym)
        market_open = self._infer_market_open(meta, ex_tz, yahoo_sym)
        bars = self._parse_bars(res, ex_tz, canonical_ticker)

        chart_prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        parsed_prev = float(chart_prev_close) if chart_prev_close is not None else None
        latest_telemetry = self._compute_pine_indicators(
            bars=bars,
            chart_prev_close=parsed_prev,
            market_open=market_open,
        )

        usd_sek, _fx_src = self.fetch_usd_sek(default=10.16)
        currency = str((asset or {}).get("currency") or meta.get("currency") or ("SEK" if yahoo_sym.endswith(".ST") else "USD"))
        latest_telemetry["usd_sek"] = usd_sek
        latest_telemetry["source"] = "yahoo_live"

        payload: dict[str, Any] = {
            "ticker": canonical_ticker,
            "yahoo_symbol": yahoo_sym,
            "currency": currency,
            "source": "yahoo_live",
            "market_open": market_open,
            "data_timestamp_cet": latest_telemetry["data_timestamp_cet"],
            "usd_sek": usd_sek,
            "bars": bars,
            "latest_telemetry": latest_telemetry,
        }

        if self.series_ttl_sec > 0:
            with self._cache_lock:
                YahooFinanceClient._series_cache[cache_key] = (time.monotonic(), copy.deepcopy(payload))

        return payload

    def fetch_chart_series(
        self,
        ticker: str,
        asset: Optional[dict[str, Any]] = None,
        range_str: str = "5d",
        interval: str = "15m",
    ) -> dict[str, Any]:
        """Alias returning the full cached chart series payload for Web UI Canvas charts & `/analyze`."""
        return self.fetch_15m_series(ticker, asset=asset, range_str=range_str, interval=interval)

    def fetch_15m_telemetry(
        self,
        ticker: str,
        asset: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Returns the latest bar's standardized Pine v6 telemetry dictionary."""
        series = self.fetch_15m_series(ticker, asset=asset, range_str="5d", interval="15m")
        return copy.deepcopy(series["latest_telemetry"])
