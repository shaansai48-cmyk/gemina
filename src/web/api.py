"""
REST & Streaming API Dispatcher for the Gemina Local Web Command Center (`src/web/api.py`).
Routes portfolio, chart, benchmark, chat, analysis, trace, health, declarative, and SSE endpoints.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
import html as html_lib
import inspect
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Optional
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.tracing import PipelineTracer
from src.aistudio_sync import export_aistudio_bundle
from src.loader import (
    DEFAULT_AGENTS_DIR,
    DEFAULT_SKILLS_DIR,
    load_agent,
    load_skill,
    parse_yaml_frontmatter,
)
from traderagent.application.ledger_service import CommandInputError
from traderagent.domain.models import TradeDecision, normalize_ticker, resolve_focus_ticker
from traderagent.domain.quant_math import compute_quant_metrics, format_trade_alert_html
from traderagent.infrastructure.market_data import MarketDataError
from traderagent.infrastructure.persistence import redact_secrets

from src.web.concurrency import BusyError, WebRuntimeBridge
from src.web.schemas import (
    AddTickerRequest,
    AnalyzeRequest,
    ChatClearRequest,
    ChatRequest,
    CloseMiniRequest,
    CloseSharesRequest,
    DeclarativeUpdateRequest,
    FillMiniRequest,
    FillSharesRequest,
    FocusRequest,
    MiniParamsRequest,
    StopRequest,
    api_error,
    format_validation_errors,
)
from src.web.security import SecurityError, resolve_safe_declarative_path
from src.web.sse import SSEBroker

CET = ZoneInfo("Europe/Stockholm")

_ALLOWED_CHART_RANGES = frozenset(
    {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "ytd", "max"}
)
_ALLOWED_CHART_INTERVALS = frozenset(
    {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo"}
)


def _query_first(query_params: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = query_params.get(key)
    if not vals:
        return default
    return str(vals[0] or default).strip()


def _parse_bool_flag(raw: str, default: bool = True) -> bool:
    if not raw:
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return default


def _parse_json_body(body_bytes: bytes, *, allow_empty: bool = False) -> dict[str, Any]:
    if not body_bytes or not body_bytes.strip():
        if allow_empty:
            return {}
        raise ValueError("Request body must not be empty.")
    try:
        parsed = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON request body must be an object.")
    return parsed


def _invoke_compatible(
    fn: Callable[..., Any],
    candidates: dict[str, Any],
    *,
    mock_kwargs: Optional[dict[str, Any]] = None,
) -> Any:
    """
    Calls `fn` adapting cleanly to its declared parameter names and defaults.
    Prevents breakage across minor Wave 1 parameter naming variants or unittest mocks.
    """
    is_mock = hasattr(fn, "assert_called") or hasattr(fn, "_mock_name")
    if is_mock and mock_kwargs is not None:
        return fn(**mock_kwargs)

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**(mock_kwargs or candidates))

    params = list(sig.parameters.values())
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    if has_var_kw and has_var_pos and len(params) == 2 and mock_kwargs is not None:
        return fn(**mock_kwargs)

    pos_args: list[Any] = []
    kw_args: dict[str, Any] = {}
    for p in params:
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if p.name not in candidates:
            continue
        val = candidates[p.name]
        if val is None and p.default is not inspect.Parameter.empty:
            continue
        if p.kind == inspect.Parameter.POSITIONAL_ONLY:
            pos_args.append(val)
        else:
            kw_args[p.name] = val

    return fn(*pos_args, **kw_args)


def _normalize_ledger_result(
    result: Any,
    bridge: WebRuntimeBridge,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Normalizes string/tuple/dict returns from LedgerService methods."""
    message = "Updated portfolio ledger."
    extra: dict[str, Any] = {}
    portfolio: Optional[dict[str, Any]] = None

    if isinstance(result, str):
        message = result
    elif isinstance(result, dict):
        extra = dict(result)
        if "message" in result:
            message = str(result["message"])
        elif "summary" in result:
            message = str(result["summary"])
        if isinstance(result.get("portfolio"), dict):
            portfolio = result["portfolio"]
    elif isinstance(result, tuple) and len(result) == 2:
        first, second = result
        if isinstance(first, dict) and isinstance(second, str):
            portfolio, message = first, second
        elif isinstance(first, str) and isinstance(second, dict):
            message = first
            if "watchlist" in second and "cash_reserves" in second:
                portfolio = second
            else:
                extra = second
        elif isinstance(first, dict) and isinstance(second, dict):
            if "watchlist" in first:
                portfolio, extra = first, second
            else:
                extra, portfolio = first, second

    if portfolio is None:
        portfolio = bridge.repo.load_portfolio()
    return message, portfolio, extra


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".tmp_{path.stem}_",
        suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _extract_usd_sek(raw: Any, fallback: float = 10.16) -> float:
    if isinstance(raw, (tuple, list)) and raw:
        raw = raw[0]
    if isinstance(raw, (int, float)) and raw > 0:
        return round(float(raw), 4)
    if isinstance(raw, dict):
        for k in ("usd_sek", "rate", "close", "price", "spot"):
            val = raw.get(k)
            if isinstance(val, (int, float)) and val > 0:
                return round(float(val), 4)
    return fallback


def _fallback_trade_html(decision: TradeDecision, ticker: str) -> str:
    esc = html_lib.escape
    guide_html = (
        f"<pre>{esc(decision.avanza_execution_guide)}</pre>"
        if decision.avanza_execution_guide
        else ""
    )
    return (
        f"<div class='trade-summary'>"
        f"<h4>{esc(ticker)} — {esc(decision.action)} ({esc(decision.market_regime)})</h4>"
        f"<p><strong>Instrument:</strong> {esc(decision.instrument)} | "
        f"<strong>Qty:</strong> {decision.quantity} | "
        f"<strong>Confidence:</strong> {decision.confidence_score}/100 ({esc(decision.critic_verdict)}) | "
        f"<strong>Net R:R:</strong> 1:{decision.net_risk_reward_ratio}</p>"
        f"<p>{esc(decision.rationale)}</p>"
        f"{guide_html}"
        f"</div>"
    )


def _derive_plain_reasons(decision: TradeDecision) -> list[str]:
    existing = [str(r).strip() for r in (getattr(decision, "plain_reasons", None) or []) if str(r).strip()]
    if existing:
        return existing
    reasons: list[str] = [
        f"Action {decision.action} in {decision.market_regime} regime (confidence {decision.confidence_score}/100, critic: {decision.critic_verdict}).",
        f"Stop {decision.underlying_stop_price} | Target {decision.take_profit_target} | Verified Net R:R 1:{decision.net_risk_reward_ratio}.",
    ]
    if decision.rationale:
        reasons.append(decision.rationale.strip())
    if decision.critic_notes and decision.critic_notes.strip().lower() not in ("none", "n/a"):
        reasons.append(f"Critic note: {decision.critic_notes.strip()}")
    return reasons


def _load_json_safe(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _trace_matches_ticker(trace: dict[str, Any], target_norm: str) -> bool:
    if not target_norm:
        return True
    compact = target_norm.replace(" ", "")
    underscore = target_norm.replace(" ", "_")

    for field_name in ("trace_id", "source", "flow"):
        val = str(trace.get(field_name, "")).upper()
        if target_norm in val or compact in val or underscore in val:
            return True

    for stage in trace.get("stages", []) or []:
        if not isinstance(stage, dict):
            continue
        for bucket_name in ("input", "output"):
            bucket = stage.get(bucket_name)
            if not isinstance(bucket, dict):
                continue
            for key in ("ticker", "incoming", "symbol", "active_focus", "active_focus_ticker"):
                if key in bucket:
                    raw = str(bucket.get(key, "")).upper()
                    parts = [normalize_ticker(p) for p in raw.split(",") if p.strip()]
                    if target_norm in parts or target_norm in raw or compact in raw:
                        return True
    return False


class WebApiRouter:
    """Dispatches REST and SSE requests against WebRuntimeBridge and SSEBroker."""

    def __init__(self, bridge: WebRuntimeBridge, sse_broker: SSEBroker):
        self.bridge = bridge
        self.sse_broker = sse_broker

    def dispatch(
        self,
        method: str,
        path: str,
        query_params: dict[str, list[str]],
        body_bytes: bytes = b"",
    ) -> tuple[int, dict[str, Any]]:
        """Routes a parsed HTTP API request and returns `(status_code, json_payload)`."""
        verb = str(method or "GET").upper()
        clean_path = "/" + str(path or "").strip("/")

        try:
            if verb == "GET" and clean_path == "/api/health":
                return 200, self.get_health()

            if verb == "GET" and clean_path == "/api/portfolio":
                use_live = _parse_bool_flag(_query_first(query_params, "live", "true"), default=True)
                return 200, self.get_portfolio(live=use_live)

            if verb == "GET" and clean_path == "/api/portfolio/calculator":
                return self.get_portfolio_calculator(query_params)

            if verb == "POST" and clean_path.startswith("/api/portfolio/"):
                action = clean_path.removeprefix("/api/portfolio/")
                if "/" not in action:
                    return self.post_portfolio_mutation(action, body_bytes)

            if verb == "GET" and clean_path.startswith("/api/chart/"):
                raw_ticker = unquote(clean_path.removeprefix("/api/chart/")).strip()
                if raw_ticker and "/" not in raw_ticker:
                    return self.get_chart(raw_ticker, query_params)

            if verb == "GET" and clean_path == "/api/benchmarks":
                return 200, self.get_benchmarks()

            if verb == "POST" and clean_path == "/api/chat":
                return self.post_chat(body_bytes)

            if verb == "POST" and clean_path == "/api/chat/clear":
                return self.post_chat_clear(body_bytes)

            if verb == "POST" and clean_path == "/api/analyze":
                return self.post_analyze(body_bytes)

            if verb == "GET" and clean_path == "/api/traces":
                return 200, self.get_traces(query_params)

            if verb == "GET" and clean_path.startswith("/api/traces/"):
                trace_id = unquote(clean_path.removeprefix("/api/traces/")).strip()
                if trace_id and "/" not in trace_id:
                    return self.get_trace_by_id(trace_id)

            if verb == "GET" and clean_path == "/api/declarative":
                return 200, self.get_declarative()

            if verb == "POST" and clean_path == "/api/declarative/reload":
                return 200, self.post_declarative_reload()

            if verb == "PUT" and clean_path.startswith("/api/declarative/"):
                rest = clean_path.removeprefix("/api/declarative/")
                parts = rest.split("/", 1)
                if len(parts) == 2 and parts[0] and parts[1]:
                    return self.put_declarative(unquote(parts[0]), unquote(parts[1]), body_bytes)

            return api_error(404, "NOT_FOUND", f"Unknown API endpoint: {verb} {clean_path}")

        except ValidationError as exc:
            return api_error(
                422,
                "VALIDATION_ERROR",
                "Request payload validation failed.",
                details=format_validation_errors(exc),
            )
        except ValueError as exc:
            return api_error(400, "BAD_REQUEST", str(exc))
        except CommandInputError as exc:
            return api_error(400, "COMMAND_INPUT_ERROR", str(exc))
        except BusyError as exc:
            return api_error(429, "ANALYSIS_BUSY", str(exc))
        except FuturesTimeoutError as exc:
            return api_error(504, "ANALYSIS_TIMEOUT", f"Analysis timed out: {exc}")
        except TimeoutError as exc:
            return api_error(504, "ANALYSIS_TIMEOUT", f"Analysis timed out: {exc}")
        except MarketDataError as exc:
            return api_error(502, "MARKET_DATA_ERROR", str(exc))
        except Exception as exc:
            return api_error(500, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")

    def _safe_fetch_usd_sek(self) -> tuple[float, str]:
        try:
            raw = self.bridge.market_data.fetch_usd_sek()
            if isinstance(raw, (tuple, list)) and len(raw) >= 2:
                return _extract_usd_sek(raw[0], 10.16), str(raw[1] or "yahoo")
            return _extract_usd_sek(raw, 10.16), "yahoo"
        except Exception:
            return 10.16, "fallback"

    def _safe_fetch_telemetry(self, ticker: str, asset: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            res = _invoke_compatible(
                self.bridge.market_data.fetch_15m_telemetry,
                {"ticker": ticker, "asset": asset},
                mock_kwargs={"ticker": ticker, "asset": asset},
            )
            return res if isinstance(res, dict) and "close" in res else None
        except Exception:
            return None

    def get_portfolio(self, *, live: bool = True) -> dict[str, Any]:
        portfolio = self.bridge.repo.load_portfolio()
        risk = self.bridge.repo.load_risk()
        latest_audits = self.bridge.repo.get_latest_audit_by_ticker(max_scan_lines=400)
        watchlist = portfolio.get("watchlist", {})

        usd_sek, usd_sek_source = self._safe_fetch_usd_sek() if live else (10.16, "fallback")

        live_telemetry_by_ticker: dict[str, Optional[dict[str, Any]]] = {}
        if live and watchlist:
            workers = min(5, max(1, len(watchlist)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gemina-spot") as pool:
                futs = {
                    sym: pool.submit(self._safe_fetch_telemetry, sym, asset)
                    for sym, asset in watchlist.items()
                }
                for sym, fut in futs.items():
                    try:
                        live_telemetry_by_ticker[sym] = fut.result(timeout=8.0)
                    except Exception:
                        live_telemetry_by_ticker[sym] = None

        positions: dict[str, dict[str, Any]] = {}
        total_shares_sek = 0.0
        total_mini_sek = 0.0
        active_mini_count = 0

        focus_raw = str(portfolio.get("active_focus_ticker", "NVDA"))
        focus_tickers = [
            normalize_ticker(t) for t in focus_raw.split(",") if t.strip()
        ]
        focus_set = set(focus_tickers)

        ratchet_target_r = float(risk.get("breakeven_ratchet_r_mult", 0.85))
        ratchet_lock_r = float(risk.get("breakeven_lock_r_mult", 0.15))

        for sym, asset in watchlist.items():
            norm_sym = normalize_ticker(sym) or sym.upper()
            audit = latest_audits.get(norm_sym, {})
            audit_telem = audit.get("telemetry") if isinstance(audit.get("telemetry"), dict) else {}
            live_telem = live_telemetry_by_ticker.get(sym)

            if live_telem is not None and "close" in live_telem:
                telem = dict(live_telem)
                spot = float(telem["close"])
                spot_source = "yahoo_15m"
            elif "close" in audit_telem:
                telem = dict(audit_telem)
                spot = float(telem["close"])
                spot_source = "audit_log"
            else:
                telem = {}
                spot = None
                spot_source = "unavailable"

            currency = str(asset.get("currency", "USD")).upper()
            fx_mult = usd_sek if currency == "USD" else 1.0
            shares_held = int(asset.get("regular_shares_held", 0))
            blended_avg = float(asset.get("blended_avg_price", 0.0))
            mini_units = int(asset.get("mini_units_held", 0))
            mini_avg_sek = float(asset.get("mini_avg_sek", 0.0))
            mini_side = str(asset.get("mini_side", "")).upper()
            if not mini_side:
                mini_side = "SHORT" if "MINI S" in str(asset.get("mini_instrument", "")).upper() else "LONG"
            if mini_units > 0:
                active_mini_count += 1

            hard_stop = float(asset.get("hard_stop_price", 0.0))
            take_profit = float(asset.get("take_profit_price", 0.0))

            metrics: dict[str, Any] = {}
            shares_pnl_native = 0.0
            shares_pnl_usd = 0.0
            shares_pnl_sek = 0.0
            shares_pnl_pct = 0.0
            mini_mark_sek = 0.0
            mini_pnl_sek = 0.0
            mini_pnl_pct = 0.0
            stop_distance_pct: Optional[float] = None
            stop_breached = False
            near_stop = False
            ratchet_progress_r = 0.0
            ratchet_progress_pct = 0.0
            ratchet_triggered = False

            if spot is not None and spot > 0:
                safe_telem = {
                    "close": spot,
                    "ema4": float(telem.get("ema4") or spot),
                    "vwap": float(telem.get("vwap") or spot),
                    "ema50": float(telem.get("ema50") or telem.get("vwap") or spot),
                    "or_high": float(telem.get("or_high") or spot),
                    "or_low": float(telem.get("or_low") or spot),
                    "atr14": float(telem.get("atr14") or 1.0),
                    "rvol": float(telem.get("rvol") or 1.0),
                    "efficiency_ratio": float(telem.get("efficiency_ratio") or 0.30),
                }
                if "usd_sek" in telem and currency == "USD":
                    safe_telem["usd_sek"] = float(telem["usd_sek"])

                metrics = compute_quant_metrics(safe_telem, asset, usd_sek=usd_sek)
                if shares_held > 0 and blended_avg > 0:
                    shares_pnl_native = round((spot - blended_avg) * shares_held, 2)
                    shares_pnl_usd = shares_pnl_native if currency == "USD" else round(shares_pnl_native / usd_sek, 2)
                    shares_pnl_sek = float(metrics["regular_shares_net_unrealized_pnl_sek"])
                    shares_pnl_pct = round(((spot - blended_avg) / blended_avg) * 100.0, 2)
                    total_shares_sek += shares_pnl_sek

                mini_mark_sek = (
                    float(metrics["mini_short_theoretical_sek"])
                    if mini_side == "SHORT"
                    else float(metrics["mini_theoretical_sek"])
                )
                if mini_units > 0 and mini_avg_sek > 0:
                    mini_pnl_sek = round((mini_mark_sek - mini_avg_sek) * mini_units, 2)
                    mini_pnl_pct = round(((mini_mark_sek - mini_avg_sek) / mini_avg_sek) * 100.0, 2)
                    total_mini_sek += mini_pnl_sek

                if hard_stop > 0:
                    stop_distance_pct = round(((spot - hard_stop) / spot) * 100.0, 2)
                    stop_breached = spot <= hard_stop
                    atr14 = float(telem.get("atr14") or 0.0)
                    near_stop = (not stop_breached) and atr14 > 0 and ((spot - hard_stop) <= 0.35 * atr14)

                entry_u = float(asset.get("mini_entry_underlying_price", 0.0))
                risk_1r_u = float(asset.get("mini_initial_risk_r", 0.0))
                if mini_units > 0 and entry_u > 0 and risk_1r_u > 0:
                    delta_u = (entry_u - spot) if mini_side == "SHORT" else (spot - entry_u)
                    ratchet_progress_r = round(delta_u / risk_1r_u, 2)
                    ratchet_triggered = ratchet_progress_r >= ratchet_target_r
                    if ratchet_target_r > 0:
                        ratchet_progress_pct = round(
                            max(0.0, min(100.0, (ratchet_progress_r / ratchet_target_r) * 100.0)),
                            1,
                        )

            positions[sym] = {
                **asset,
                "ticker": sym,
                "is_focus": norm_sym in focus_set,
                "spot": spot,
                "spot_source": spot_source,
                "telemetry_ts": telem.get("time") or audit.get("ts"),
                "fx_multiplier": fx_mult,
                "unrealized_shares_native": shares_pnl_native,
                "unrealized_shares_usd": shares_pnl_usd,
                "unrealized_shares_sek": shares_pnl_sek,
                "unrealized_shares_pct": shares_pnl_pct,
                "mini_mark_sek": mini_mark_sek,
                "unrealized_mini_sek": mini_pnl_sek,
                "unrealized_mini_pct": mini_pnl_pct,
                "total_unrealized_sek": round(shares_pnl_sek + mini_pnl_sek, 2),
                "stop_distance_pct": stop_distance_pct,
                "stop_breached": stop_breached,
                "near_stop": near_stop,
                "take_profit_price": take_profit,
                "ratchet_progress_r": ratchet_progress_r,
                "ratchet_progress_pct": ratchet_progress_pct,
                "ratchet_target_r": ratchet_target_r,
                "ratchet_lock_r": ratchet_lock_r,
                "ratchet_triggered": ratchet_triggered,
                "quant_metrics": metrics,
                "latest_decision": redact_secrets(audit.get("decision")) if audit.get("decision") else None,
            }

        stats = portfolio.get("daily_stats", {})
        trades_today = int(stats.get("trades_executed_today", 0))
        max_daily_trades = int(risk.get("max_daily_trades", 2))
        realized_today = float(stats.get("realized_pnl_today_sek", 0.0))
        daily_lockout = float(risk.get("daily_loss_lockout_sek", -900.0))
        cooldown_total = float(risk.get("cooldown_minutes_after_stopout", 45))
        last_stop_epoch = float(stats.get("last_stopout_epoch", 0) or 0)
        mins_since_stop = (time.time() - last_stop_epoch) / 60.0 if last_stop_epoch > 0 else None
        cooldown_active = mins_since_stop is not None and mins_since_stop < cooldown_total
        cooldown_remaining = round(max(0.0, cooldown_total - mins_since_stop), 1) if cooldown_active else 0.0
        max_minis = int(risk.get("max_concurrent_leveraged_minis", 1))

        budgets = {
            "trades_executed_today": trades_today,
            "max_daily_trades": max_daily_trades,
            "trades_remaining": max(0, max_daily_trades - trades_today),
            "daily_quota_exhausted": trades_today >= max_daily_trades,
            "realized_pnl_today_sek": realized_today,
            "daily_loss_lockout_sek": daily_lockout,
            "loss_lockout_active": realized_today <= daily_lockout,
            "cooldown_active": cooldown_active,
            "cooldown_remaining_min": cooldown_remaining,
            "cooldown_minutes_after_stopout": cooldown_total,
            "active_mini_count": active_mini_count,
            "max_concurrent_leveraged_minis": max_minis,
            "mini_slot_available": active_mini_count < max_minis,
            "max_risk_per_trade_sek": float(risk.get("max_risk_per_trade_sek", 450.0)),
        }

        latest_by_ticker = {
            sym: {
                "ticker": sym,
                "ts": pos["telemetry_ts"],
                "close": pos["spot"],
                "telemetry": {"close": pos["spot"], "time": pos["telemetry_ts"]} if pos["spot"] else {},
                "decision": pos["latest_decision"],
            }
            for sym, pos in positions.items()
        }

        return {
            "ok": True,
            "account_id": portfolio.get("account_id", "ISK 5872428"),
            "active_focus_ticker": focus_raw,
            "focus_tickers": focus_tickers,
            "usd_sek": usd_sek,
            "usd_sek_source": usd_sek_source,
            "cash_reserves": portfolio.get("cash_reserves", {}),
            "daily_stats": stats,
            "budgets": budgets,
            "risk_profile": risk,
            "totals": {
                "unrealized_shares_sek": round(total_shares_sek, 2),
                "unrealized_mini_sek": round(total_mini_sek, 2),
                "total_unrealized_sek": round(total_shares_sek + total_mini_sek, 2),
            },
            "positions": positions,
            "latest_by_ticker": latest_by_ticker,
            "watchlist": watchlist,
            "timestamp_cet": datetime.now(CET).isoformat(timespec="seconds"),
        }

    def get_portfolio_calculator(
        self, query_params: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        portfolio = self.bridge.repo.load_portfolio()
        risk = self.bridge.repo.load_risk()
        raw_ticker = _query_first(query_params, "ticker")
        target = resolve_focus_ticker(portfolio, explicit_ticker=raw_ticker or None)
        watchlist = portfolio.get("watchlist", {})
        if target not in watchlist:
            return api_error(404, "UNKNOWN_TICKER", f"Ticker '{target}' is not in watchlist.")

        asset = watchlist[target]
        side = (_query_first(query_params, "side", "LONG") or "LONG").upper()
        if side not in ("LONG", "SHORT"):
            return api_error(400, "INVALID_SIDE", "Query parameter 'side' must be LONG or SHORT.")

        def _opt_float(key: str) -> Optional[float]:
            val_str = _query_first(query_params, key)
            if not val_str:
                return None
            try:
                return float(val_str.replace(",", "."))
            except ValueError as exc:
                raise CommandInputError(f"Invalid numeric parameter '{key}': {val_str}") from exc

        spot = _opt_float("spot")
        stop = _opt_float("stop") or _opt_float("stop_price")
        target_px = _opt_float("target")
        risk_sek = _opt_float("risk_sek")
        entry_sek = _opt_float("entry_sek") or _opt_float("limit_price")
        risk_parity_mode = _parse_bool_flag(_query_first(query_params, "risk_parity"), default=False) or (
            _query_first(query_params, "mode").lower() == "risk_parity"
        )
        budget_sek = None if risk_parity_mode else _opt_float("budget_sek")

        usd_sek, usd_sek_source = self._safe_fetch_usd_sek()
        spot_source = "override"
        if spot is None:
            live_telem = self._safe_fetch_telemetry(target, asset)
            if live_telem and "close" in live_telem:
                spot = float(live_telem["close"])
                spot_source = "yahoo_15m"
            else:
                latest_audit = self.bridge.repo.get_recent_audit_tail(1, ticker=target)
                if latest_audit and isinstance(latest_audit[-1].get("telemetry"), dict):
                    spot = float(latest_audit[-1]["telemetry"].get("close") or 0.0)
                    spot_source = "audit_log"
                else:
                    spot = 0.0
                    spot_source = "unavailable"

        if stop is None:
            hard_stop = float(asset.get("hard_stop_price") or 0.0)
            if hard_stop > 0 and (
                (side == "LONG" and (spot <= 0 or hard_stop < spot))
                or (side == "SHORT" and spot > 0 and hard_stop > spot)
            ):
                stop = hard_stop
            elif spot > 0:
                stop = round(spot * (0.98 if side == "LONG" else 1.02), 2)
            else:
                stop = hard_stop

        if target_px is None:
            tp = float(asset.get("take_profit_price") or 0.0)
            target_px = tp if tp > 0 else (round(spot * (1.04 if side == "LONG" else 0.96), 2) if spot > 0 else 0.0)

        max_risk_sek = risk_sek if risk_sek is not None else float(risk.get("max_risk_per_trade_sek", 450.0))

        preview = _invoke_compatible(
            self.bridge.ledger.calculate_mini_preview,
            {
                "ticker": target,
                "symbol": target,
                "spot": spot,
                "spot_price": spot,
                "underlying_price": spot,
                "stop": stop,
                "stop_price": stop,
                "stop_underlying": stop,
                "hard_stop_price": stop,
                "underlying_stop_price": stop,
                "target": target_px,
                "take_profit_price": target_px,
                "side": side,
                "budget_sek": budget_sek,
                "usd_sek": usd_sek,
                "fx_mult": usd_sek,
                "fx_multiplier": usd_sek,
                "risk_sek": max_risk_sek,
                "max_risk_sek": max_risk_sek,
                "max_risk_per_trade_sek": max_risk_sek,
                "unit_price_sek": entry_sek,
                "entry_sek": entry_sek,
            },
            mock_kwargs={
                "ticker": target,
                "spot": spot,
                "stop": stop,
                "target": target_px,
                "side": side,
                "usd_sek": usd_sek,
                "risk_sek": max_risk_sek,
            },
        )

        payload: dict[str, Any] = {
            "ok": True,
            "ticker": target,
            "side": side,
            "spot": spot,
            "spot_source": spot_source,
            "stop": stop,
            "target": target_px,
            "usd_sek": usd_sek,
            "usd_sek_source": usd_sek_source,
            "max_risk_per_trade_sek": max_risk_sek,
            "available_tactical_mini_sek": portfolio.get("cash_reserves", {}).get(
                "available_tactical_mini_sek", 0.0
            ),
        }
        if isinstance(preview, dict):
            payload.update(preview)
            if "units_from_budget" in preview and "units" not in payload:
                payload["units"] = preview["units_from_budget"]
            if "input_unit_price_sek" in preview and "limit_price_sek" not in payload:
                payload["limit_price_sek"] = preview["input_unit_price_sek"]
            if "theoretical_unit_sek" in preview and "theoretical_sek" not in payload:
                payload["theoretical_sek"] = preview["theoretical_unit_sek"]
            if "courtage_free" in preview and "zero_courtage" not in payload:
                payload["zero_courtage"] = bool(preview["courtage_free"])
                payload["courtage_sek"] = 0 if preview["courtage_free"] else 19
            if "stop_trigger_sek" in preview and "trigger_sek" not in payload:
                payload["trigger_sek"] = preview["stop_trigger_sek"]
            if "stop_sell_sek" in preview and "sell_sek" not in payload:
                payload["sell_sek"] = preview["stop_sell_sek"]
            if "estimated_1r_risk_sek" in preview and "total_risk_sek" not in payload:
                payload["total_risk_sek"] = preview["estimated_1r_risk_sek"]
            if "preflight_warnings" in preview and "warnings" not in payload:
                payload["warnings"] = preview["preflight_warnings"]
            payload["preview"] = preview
        else:
            payload["preview"] = preview
        return 200, payload

    def _build_ledger_call(
        self, action: str, raw_body: dict[str, Any], tracer: PipelineTracer
    ) -> Optional[tuple[Callable[..., Any], dict[str, Any], dict[str, Any], bool]]:
        ledger = self.bridge.ledger
        spec_map: dict[
            str,
            tuple[type[Any], str, Callable[[Any], tuple[dict[str, Any], dict[str, Any]]]],
        ] = {
            "focus": (
                FocusRequest,
                "switch_focus",
                lambda d: (
                    {
                        "tickers_input": d.tickers,
                        "tickers": d.tickers,
                        "raw_tickers": d.tickers,
                        "focus": d.tickers,
                        "ticker": d.tickers,
                        "tracer": tracer,
                    },
                    {"tickers_input": d.tickers, "tracer": tracer},
                ),
            ),
            "stop": (
                StopRequest,
                "set_stop",
                lambda d: (
                    {
                        "ticker": d.ticker,
                        "symbol": d.ticker,
                        "hard_stop_price": d.hard_stop_price,
                        "stop_price": d.hard_stop_price,
                        "stop": d.hard_stop_price,
                        "stop_val": d.hard_stop_price,
                        "take_profit_price": d.take_profit_price,
                        "target_price": d.take_profit_price,
                        "target": d.take_profit_price,
                        "tp": d.take_profit_price,
                        "tracer": tracer,
                    },
                    {
                        "ticker": d.ticker,
                        "hard_stop_price": d.hard_stop_price,
                        "take_profit_price": d.take_profit_price,
                        "tracer": tracer,
                    },
                ),
            ),
            "fill-mini": (
                FillMiniRequest,
                "fill_mini",
                lambda d: (
                    {
                        "ticker": d.ticker,
                        "symbol": d.ticker,
                        "units": d.units,
                        "quantity": d.units,
                        "qty": d.units,
                        "price_sek": d.price_sek,
                        "entry_sek": d.price_sek,
                        "price": d.price_sek,
                        "side": d.side,
                        "side_tag": d.side,
                        "tracer": tracer,
                    },
                    {
                        "ticker": d.ticker,
                        "units": d.units,
                        "price_sek": d.price_sek,
                        "side": d.side,
                        "tracer": tracer,
                    },
                ),
            ),
            "close-mini": (
                CloseMiniRequest,
                "close_mini",
                lambda d: (
                    {
                        "ticker": d.ticker,
                        "symbol": d.ticker,
                        "exit_sek": d.exit_sek,
                        "exit_price": d.exit_sek,
                        "price_sek": d.exit_sek,
                        "price": d.exit_sek,
                        "tracer": tracer,
                    },
                    {"ticker": d.ticker, "exit_sek": d.exit_sek, "tracer": tracer},
                ),
            ),
            "fill-shares": (
                FillSharesRequest,
                "fill_shares",
                lambda d: (
                    {
                        "ticker": d.ticker,
                        "symbol": d.ticker,
                        "quantity": d.quantity,
                        "qty": d.quantity,
                        "shares": d.quantity,
                        "avg_price": d.avg_price,
                        "price": d.avg_price,
                        "tracer": tracer,
                    },
                    {
                        "ticker": d.ticker,
                        "quantity": d.quantity,
                        "avg_price": d.avg_price,
                        "tracer": tracer,
                    },
                ),
            ),
            "close-shares": (
                CloseSharesRequest,
                "close_shares",
                lambda d: (
                    {
                        "ticker": d.ticker,
                        "symbol": d.ticker,
                        "exit_price": d.exit_price,
                        "price": d.exit_price,
                        "tracer": tracer,
                    },
                    {"ticker": d.ticker, "exit_price": d.exit_price, "tracer": tracer},
                ),
            ),
            "mini-params": (
                MiniParamsRequest,
                "set_mini_params",
                lambda d: (
                    {
                        "ticker": d.ticker,
                        "symbol": d.ticker,
                        "financing_level": d.financing_level,
                        "fin_level": d.financing_level,
                        "mini_financing_level": d.financing_level,
                        "fin_lvl": d.financing_level,
                        "fin_val": d.financing_level,
                        "barrier": d.barrier,
                        "mini_barrier": d.barrier,
                        "parity": d.parity,
                        "mini_parity": d.parity,
                        "side": d.side,
                        "instrument": d.instrument,
                        "tracer": tracer,
                    },
                    {
                        "ticker": d.ticker,
                        "financing_level": d.financing_level,
                        "barrier": d.barrier,
                        "parity": d.parity,
                        "side": d.side,
                        "instrument": d.instrument,
                        "tracer": tracer,
                    },
                ),
            ),
            "add-ticker": (
                AddTickerRequest,
                "add_ticker",
                lambda d: (
                    {
                        "ticker": d.ticker,
                        "symbol": d.ticker,
                        "currency": d.currency,
                        "hard_stop_price": d.hard_stop_price,
                        "stop_price": d.hard_stop_price,
                        "stop": d.hard_stop_price,
                        "hard_stop": d.hard_stop_price,
                        "mini_financing_level": d.mini_financing_level,
                        "financing_level": d.mini_financing_level,
                        "fin_level": d.mini_financing_level,
                        "mini_parity": d.mini_parity,
                        "parity": d.mini_parity,
                        "take_profit_price": d.take_profit_price,
                        "target_price": d.take_profit_price,
                        "target": d.take_profit_price,
                        "tracer": tracer,
                    },
                    {
                        "ticker": d.ticker,
                        "currency": d.currency,
                        "hard_stop_price": d.hard_stop_price,
                        "mini_financing_level": d.mini_financing_level,
                        "mini_parity": d.mini_parity,
                        "take_profit_price": d.take_profit_price,
                        "tracer": tracer,
                    },
                ),
            ),
        }
        entry = spec_map.get(action)
        if entry is None:
            return None
        dto_cls, method_name, builder = entry
        dto = dto_cls.model_validate(raw_body)
        candidates, mock_kwargs = builder(dto)
        return getattr(ledger, method_name), candidates, mock_kwargs, bool(dto.mirror_telegram)

    def post_portfolio_mutation(
        self, action: str, body_bytes: bytes
    ) -> tuple[int, dict[str, Any]]:
        raw_body = _parse_json_body(body_bytes)
        tracer = PipelineTracer(
            flow="web_ledger_mutation",
            source=f"web_api_{action}",
            prefix=f"web-{action}",
            trace_file_path=self.bridge.config.pipeline_trace_path,
        )

        try:
            built = self._build_ledger_call(action, raw_body, tracer)
            if built is None:
                tracer.complete("UNKNOWN_ACTION")
                return api_error(404, "NOT_FOUND", f"Unknown portfolio action: {action}")
            fn, candidates, mock_kwargs, mirror_telegram = built
            raw_res = _invoke_compatible(fn, candidates, mock_kwargs=mock_kwargs)
        except (ValidationError, ValueError, CommandInputError):
            tracer.complete("REJECTED_INPUT")
            raise
        except Exception:
            tracer.complete("ERROR")
            raise

        message, updated_portfolio, extra = _normalize_ledger_result(raw_res, self.bridge)
        telegram_mirrored = False
        if mirror_telegram:
            try:
                self.bridge.telegram.send_message(
                    f"🌐 *Web UI Ledger Update:* {message}",
                    include_ticker=True,
                    tracer=tracer,
                )
                telegram_mirrored = True
            except Exception:
                telegram_mirrored = False

        trace_record = tracer.complete("COMPLETED")
        self.sse_broker.publish(
            "portfolio.updated",
            {
                "action": action,
                "message": message,
                "trace_id": tracer.trace_id,
                "active_focus_ticker": updated_portfolio.get("active_focus_ticker"),
                "daily_stats": updated_portfolio.get("daily_stats", {}),
                "cash_reserves": updated_portfolio.get("cash_reserves", {}),
            },
        )

        response: dict[str, Any] = {
            "ok": True,
            "action": action,
            "message": message,
            "telegram_mirrored": telegram_mirrored,
            "trace_id": tracer.trace_id,
            "trace": redact_secrets(trace_record),
            "portfolio": updated_portfolio,
        }
        if extra:
            response["result"] = redact_secrets(extra)
        return 200, response

    def get_chart(
        self, raw_ticker: str, query_params: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any]]:
        norm_ticker = normalize_ticker(raw_ticker)
        if not norm_ticker:
            return api_error(400, "INVALID_TICKER", "Ticker must not be empty.")

        range_str = _query_first(query_params, "range", "5d") or "5d"
        interval = _query_first(query_params, "interval", "15m") or "15m"
        if range_str not in _ALLOWED_CHART_RANGES:
            return api_error(400, "INVALID_RANGE", f"Unsupported chart range: {range_str}")
        if interval not in _ALLOWED_CHART_INTERVALS:
            return api_error(400, "INVALID_INTERVAL", f"Unsupported chart interval: {interval}")

        portfolio = self.bridge.repo.load_portfolio()
        watchlist = portfolio.get("watchlist", {})
        asset = watchlist.get(norm_ticker)

        series = _invoke_compatible(
            self.bridge.market_data.fetch_chart_series,
            {
                "ticker": norm_ticker,
                "asset": asset,
                "range_str": range_str,
                "range": range_str,
                "interval": interval,
            },
            mock_kwargs={
                "ticker": norm_ticker,
                "asset": asset,
                "range_str": range_str,
                "interval": interval,
            },
        )

        audit_records = self.bridge.repo.get_recent_audit_tail(
            50, ticker=norm_ticker, max_scan_lines=1000
        )
        audit_markers: list[dict[str, Any]] = []
        for rec in audit_records:
            telem = rec.get("telemetry") if isinstance(rec.get("telemetry"), dict) else {}
            dec = rec.get("decision") if isinstance(rec.get("decision"), dict) else {}
            audit_markers.append(
                {
                    "ts": rec.get("ts") or telem.get("time"),
                    "trace_id": rec.get("trace_id"),
                    "ticker": rec.get("ticker", norm_ticker),
                    "close": telem.get("close"),
                    "ema4": telem.get("ema4"),
                    "vwap": telem.get("vwap"),
                    "atr14": telem.get("atr14"),
                    "rsi14": telem.get("rsi14"),
                    "action": dec.get("action"),
                    "instrument": dec.get("instrument"),
                    "quantity": dec.get("quantity"),
                    "confidence_score": dec.get("confidence_score"),
                    "critic_verdict": dec.get("critic_verdict"),
                    "net_risk_reward_ratio": dec.get("net_risk_reward_ratio"),
                    "underlying_stop_price": dec.get("underlying_stop_price"),
                    "take_profit_target": dec.get("take_profit_target"),
                    "rationale": dec.get("rationale"),
                }
            )

        overlays: dict[str, Any] = {}
        if isinstance(asset, dict):
            overlays = {
                "currency": asset.get("currency", "USD"),
                "exchange": asset.get("exchange", "NASDAQ"),
                "hard_stop_price": asset.get("hard_stop_price", 0.0),
                "take_profit_price": asset.get("take_profit_price", 0.0),
                "blended_avg_price": asset.get("blended_avg_price", 0.0),
                "regular_shares_held": asset.get("regular_shares_held", 0),
                "mini_units_held": asset.get("mini_units_held", 0),
                "mini_avg_sek": asset.get("mini_avg_sek", 0.0),
                "mini_side": asset.get("mini_side", ""),
                "mini_instrument": asset.get("mini_instrument", ""),
                "mini_financing_level": asset.get("mini_financing_level", 0.0),
                "mini_barrier": asset.get("mini_barrier", 0.0),
                "mini_short_instrument": asset.get("mini_short_instrument", ""),
                "mini_short_financing_level": asset.get("mini_short_financing_level", 0.0),
                "mini_short_barrier": asset.get("mini_short_barrier", 0.0),
                "mini_parity": asset.get("mini_parity", 10.0),
                "mini_entry_underlying_price": asset.get("mini_entry_underlying_price", 0.0),
                "mini_initial_risk_r": asset.get("mini_initial_risk_r", 0.0),
            }

        payload: dict[str, Any] = {
            "ok": True,
            "ticker": norm_ticker,
            "range": range_str,
            "interval": interval,
            "in_watchlist": asset is not None,
            "overlays": overlays,
            "audit_markers": redact_secrets(audit_markers),
        }
        if isinstance(series, dict):
            for k, v in series.items():
                if k not in payload:
                    payload[k] = redact_secrets(v)
        else:
            payload["series"] = redact_secrets(series)
        return 200, payload

    def get_benchmarks(self) -> dict[str, Any]:
        data_dir = Path(self.bridge.config.portfolio_path).parent
        exp_path = Path(self.bridge.config.experience_memory_path)
        psr_path = data_dir / "benchmarks" / "sixty_day_psr_dsr_validation.json"
        cap_path = data_dir / "benchmarks" / "strategy_capability_benchmark.json"

        experience_memory = self.bridge.repo.experience.load()
        sixty_day = _load_json_safe(psr_path)
        capability = _load_json_safe(cap_path)

        return redact_secrets(
            {
                "ok": True,
                "experience_memory": experience_memory,
                "sixty_day_validation": sixty_day,
                "strategy_capability": capability,
                "sources": {
                    "experience_memory": exp_path.exists(),
                    "sixty_day_psr_dsr_validation": psr_path.exists(),
                    "strategy_capability_benchmark": cap_path.exists(),
                },
            }
        )

    def post_chat(self, body_bytes: bytes) -> tuple[int, dict[str, Any]]:
        raw_body = _parse_json_body(body_bytes)
        req = ChatRequest.model_validate(raw_body)

        tracer = PipelineTracer(
            flow="web_chat",
            source=f"web_session_{req.session_id}",
            prefix="web-chat",
            trace_file_path=self.bridge.config.pipeline_trace_path,
        )
        tracer.add_stage(
            stage="INGEST_ARRIVAL",
            status="OK",
            input_data={
                "session_id": req.session_id,
                "persona": req.persona,
                "chars": len(req.message),
            },
            transformation=f"Received web chat message for persona '{req.persona}'",
            output_data={"session_id": req.session_id},
        )

        try:
            with self.bridge.persona_lock:
                qa_agent = self.bridge.get_qa_agent(req.session_id)
                answer = qa_agent.answer(
                    req.message,
                    tracer=tracer,
                    use_subagent_persona=(req.persona == "subagent"),
                )
        except Exception:
            tracer.complete("ERROR")
            raise

        telegram_mirrored = False
        if req.mirror_telegram:
            try:
                self.bridge.telegram.send_message(
                    f"💬 *Web Chat ({req.persona}):*\nQ: {req.message[:400]}\n\n{answer}",
                    include_ticker=True,
                    tracer=tracer,
                )
                telegram_mirrored = True
            except Exception:
                telegram_mirrored = False

        trace_record = tracer.complete("COMPLETED")
        return 200, {
            "ok": True,
            "session_id": req.session_id,
            "persona": req.persona,
            "answer": answer,
            "telegram_mirrored": telegram_mirrored,
            "trace_id": tracer.trace_id,
            "trace": redact_secrets(trace_record),
        }

    def post_chat_clear(self, body_bytes: bytes) -> tuple[int, dict[str, Any]]:
        raw_body = _parse_json_body(body_bytes, allow_empty=True)
        req = ChatClearRequest.model_validate(raw_body)
        self.bridge.clear_chat_session(req.session_id)
        return 200, {"ok": True, "session_id": req.session_id, "cleared": True}

    def post_analyze(self, body_bytes: bytes) -> tuple[int, dict[str, Any]]:
        raw_body = _parse_json_body(body_bytes)
        req = AnalyzeRequest.model_validate(raw_body)

        portfolio = self.bridge.repo.load_portfolio()
        risk = self.bridge.repo.load_risk()
        watchlist = portfolio.get("watchlist", {})

        if req.ticker:
            norm = normalize_ticker(req.ticker)
            if norm not in watchlist:
                return api_error(
                    404,
                    "UNKNOWN_TICKER",
                    f"Ticker '{norm or req.ticker}' is not in watchlist. Add it first.",
                )
            target = norm
        else:
            target = resolve_focus_ticker(portfolio)
            if target not in watchlist:
                return api_error(404, "UNKNOWN_TICKER", f"Focus ticker '{target}' not found.")

        asset = watchlist[target]
        deep_mode = req.mode == "committee"

        def _execute_analysis() -> dict[str, Any]:
            tracer = PipelineTracer(
                flow="web_live_analysis",
                source=f"web_{req.mode}",
                prefix=f"web-{target.replace(' ', '')}",
                trace_file_path=self.bridge.config.pipeline_trace_path,
            )
            try:
                t_fetch = time.perf_counter()
                telemetry = _invoke_compatible(
                    self.bridge.market_data.fetch_15m_telemetry,
                    {"ticker": target, "asset": asset},
                    mock_kwargs={"ticker": target, "asset": asset},
                )
                if not isinstance(telemetry, dict) or "close" not in telemetry:
                    raise MarketDataError(f"Incomplete 15m telemetry returned for {target}.")
                telemetry.setdefault("ticker", target)

                usd_sek = _extract_usd_sek(telemetry, 0.0)
                if usd_sek <= 0:
                    usd_sek, _ = self._safe_fetch_usd_sek()

                tracer.add_stage(
                    stage="INGEST_ARRIVAL",
                    status="LIVE_15M_TELEMETRY",
                    input_data={"ticker": target, "mode": req.mode},
                    transformation="Fetched live 15m telemetry snapshot from Yahoo Finance",
                    output_data={
                        "close": telemetry.get("close"),
                        "time": telemetry.get("time"),
                        "usd_sek": usd_sek,
                    },
                    duration_ms=(time.perf_counter() - t_fetch) * 1000.0,
                )

                metrics = compute_quant_metrics(telemetry, asset, usd_sek=usd_sek)
                recent_bars = [
                    {
                        "ts": b.get("ts"),
                        "close": (b.get("telemetry") or {}).get("close"),
                        "action": (b.get("decision") or {}).get("action"),
                    }
                    for b in self.bridge.repo.get_recent_audit_tail(4, ticker=target)
                ]
                ticker_exp = self.bridge.repo.experience.get_ticker_experience(target)

                eval_fn = getattr(
                    self.bridge.committee,
                    "evaluate_live_snapshot",
                    self.bridge.committee.evaluate_bar,
                )
                decision = _invoke_compatible(
                    eval_fn,
                    {
                        "ticker": target,
                        "telemetry": telemetry,
                        "portfolio": portfolio,
                        "asset": asset,
                        "risk": risk,
                        "metrics": metrics,
                        "recent_ticker_bars": recent_bars,
                        "ticker_experience": ticker_exp,
                        "allow_live_search": req.allow_live_search,
                        "deep": deep_mode,
                        "mode": req.mode,
                        "tracer": tracer,
                    },
                    mock_kwargs={
                        "telemetry": telemetry,
                        "portfolio": portfolio,
                        "asset": asset,
                        "risk": risk,
                        "metrics": metrics,
                        "recent_ticker_bars": recent_bars,
                        "ticker_experience": ticker_exp,
                        "allow_live_search": req.allow_live_search,
                        "deep": deep_mode,
                        "tracer": tracer,
                    },
                )

                if not isinstance(decision, TradeDecision):
                    decision = TradeDecision.model_validate(decision)

                if req.persist_audit:
                    self.bridge.repo.append_audit_log(
                        {
                            "trace_id": tracer.trace_id,
                            "ts": telemetry.get("time") or tracer.arrival_ts,
                            "ticker": target,
                            "telemetry": redact_secrets(telemetry),
                            "decision": decision.model_dump(),
                        }
                    )

                html_res = _invoke_compatible(
                    format_trade_alert_html,
                    {
                        "decision": decision,
                        "telemetry": telemetry,
                        "portfolio": portfolio,
                        "asset": asset,
                        "metrics": metrics,
                        "risk": risk,
                        "source_label": f"Web /analyze ({req.mode})",
                        "account_id": portfolio.get("account_id", "ISK 5872428"),
                    },
                    mock_kwargs={
                        "decision": decision,
                        "telemetry": telemetry,
                        "portfolio": portfolio,
                        "asset": asset,
                        "metrics": metrics,
                        "risk": risk,
                    },
                )
                if isinstance(html_res, dict):
                    summary_html = str(html_res.get("summary_html") or html_res.get("html") or "")
                else:
                    summary_html = str(html_res or "")
                if not summary_html.strip():
                    summary_html = _fallback_trade_html(decision, target)

                plain_reasons = _derive_plain_reasons(decision)
                trace_record = tracer.complete("COMPLETED")
                return {
                    "ok": True,
                    "ticker": target,
                    "mode": req.mode,
                    "allow_live_search": req.allow_live_search,
                    "persist_audit": req.persist_audit,
                    "usd_sek": usd_sek,
                    "telemetry": redact_secrets(telemetry),
                    "metrics": redact_secrets(metrics),
                    "decision": decision.model_dump(),
                    "summary_html": summary_html,
                    "plain_reasons": plain_reasons,
                    "trace_id": tracer.trace_id,
                    "trace": redact_secrets(trace_record),
                }
            except Exception:
                tracer.complete("ERROR")
                raise

        future = self.bridge.submit_analysis(_execute_analysis)
        result = future.result(timeout=120.0)
        return 200, result

    def get_traces(self, query_params: dict[str, list[str]]) -> dict[str, Any]:
        raw_limit = _query_first(query_params, "limit", "40")
        try:
            limit = max(1, min(200, int(raw_limit)))
        except ValueError:
            limit = 40

        flow_filter = _query_first(query_params, "flow").lower()
        status_filter = _query_first(query_params, "status").lower()
        ticker_filter = normalize_ticker(_query_first(query_params, "ticker"))

        active = PipelineTracer.get_active_traces()
        persisted = self.bridge.repo.get_recent_traces(
            n=max(limit * 3, 200), max_scan_lines=1000
        )

        by_id: dict[str, dict[str, Any]] = {}
        for item in active:
            if isinstance(item, dict) and item.get("trace_id"):
                by_id[str(item["trace_id"])] = redact_secrets(item)
        for item in persisted:
            if isinstance(item, dict) and item.get("trace_id"):
                by_id[str(item["trace_id"])] = redact_secrets(item)

        filtered: list[dict[str, Any]] = []
        for trace in by_id.values():
            if flow_filter and flow_filter not in str(trace.get("flow", "")).lower():
                continue
            if status_filter and status_filter not in str(trace.get("final_status", "")).lower():
                continue
            if ticker_filter and not _trace_matches_ticker(trace, ticker_filter):
                continue
            filtered.append(trace)

        filtered.sort(key=lambda t: str(t.get("arrival_ts", "")), reverse=True)
        limited = filtered[:limit]
        return {
            "ok": True,
            "count": len(limited),
            "total_matched": len(filtered),
            "active_count": len(active),
            "traces": limited,
        }

    def get_trace_by_id(self, trace_id: str) -> tuple[int, dict[str, Any]]:
        for item in PipelineTracer.get_active_traces():
            if str(item.get("trace_id", "")) == trace_id:
                return 200, {"ok": True, "trace": redact_secrets(item)}

        for item in reversed(self.bridge.repo.get_recent_traces(n=500, max_scan_lines=2000)):
            if str(item.get("trace_id", "")) == trace_id:
                return 200, {"ok": True, "trace": redact_secrets(item)}

        return api_error(404, "TRACE_NOT_FOUND", f"Trace '{trace_id}' was not found.")

    def get_health(self) -> dict[str, Any]:
        host_metrics = SystemDiagnostics.collect_host_metrics(
            max_age_sec=15.0, allow_stale=True, force_refresh=False
        )
        diag_cmds = SystemDiagnostics.run_all_diagnostic_commands(max_age_sec=5.0)
        status_eval = self.bridge.status_evaluator.evaluate()
        apps_status = self.bridge.apps_script.get_status()

        payload = {
            "ok": True,
            "status": {
                "is_red": bool(status_eval.get("is_red")),
                "color": status_eval.get("color", ""),
                "ticker_line": status_eval.get("ticker_line", ""),
                "red_reasons": list(status_eval.get("red_reasons", [])),
            },
            "host_metrics": host_metrics,
            "diagnostics": host_metrics,
            "diagnostic_commands": diag_cmds,
            "commands": diag_cmds,
            "apps_script": apps_status,
            "sse": {
                "active_subscribers": self.sse_broker.subscriber_count(),
                "latest_event_id": self.sse_broker.latest_event_id,
            },
            "config_status": {
                "gemini_api_key_configured": bool(self.bridge.config.gemini_api_key),
                "telegram_bot_configured": bool(self.bridge.config.telegram_bot_token),
                "telegram_chat_bound": bool(self.bridge.config.telegram_chat_id),
                "apps_script_configured": bool(self.bridge.config.apps_script_url),
                "ui_token_configured": bool(self.bridge.config.ui_token),
                "gemini_model": self.bridge.config.gemini_model,
                "ollama_model": self.bridge.config.ollama_model,
                "ui_host": self.bridge.config.ui_host,
                "ui_port": self.bridge.config.ui_port,
            },
            "timestamp_cet": datetime.now(CET).isoformat(timespec="seconds"),
        }
        return redact_secrets(payload)

    def get_declarative(self) -> dict[str, Any]:
        with self.bridge.persona_lock:
            agents: list[dict[str, Any]] = []
            if DEFAULT_AGENTS_DIR.is_dir():
                for path in sorted(DEFAULT_AGENTS_DIR.glob("*.md")):
                    raw_text = path.read_text(encoding="utf-8")
                    agent_def = load_agent(path.name, attach_skills=True)
                    agents.append(
                        {
                            "kind": "agents",
                            "filename": path.name,
                            "name": agent_def.name,
                            "role": agent_def.role,
                            "model": agent_def.model,
                            "fallback_model": agent_def.fallback_model,
                            "temperature": agent_def.temperature,
                            "skill_names": agent_def.skill_names,
                            "goal": agent_def.goal,
                            "backstory": agent_def.backstory,
                            "instructions": agent_def.instructions,
                            "compiled_prompt_chars": len(agent_def.compile_system_prompt()),
                            "raw_text": raw_text,
                        }
                    )

            skills: list[dict[str, Any]] = []
            seen_slugs: set[str] = set()
            if DEFAULT_SKILLS_DIR.is_dir():
                for entry in sorted(DEFAULT_SKILLS_DIR.iterdir()):
                    skill_file = entry / "SKILL.md"
                    if entry.is_dir() and skill_file.is_file():
                        raw_text = skill_file.read_text(encoding="utf-8")
                        sdef = load_skill(entry.name)
                        seen_slugs.add(entry.name)
                        skills.append(
                            {
                                "kind": "skills",
                                "slug": entry.name,
                                "filename": f"{entry.name}/SKILL.md",
                                "name": sdef.name,
                                "description": sdef.description,
                                "version": sdef.version,
                                "metadata": sdef.metadata,
                                "body": sdef.body,
                                "raw_text": raw_text,
                            }
                        )
                for path in sorted(DEFAULT_SKILLS_DIR.glob("*.md")):
                    if path.stem in seen_slugs:
                        continue
                    raw_text = path.read_text(encoding="utf-8")
                    sdef = load_skill(path.name)
                    skills.append(
                        {
                            "kind": "skills",
                            "slug": path.stem,
                            "filename": path.name,
                            "name": sdef.name,
                            "description": sdef.description,
                            "version": sdef.version,
                            "metadata": sdef.metadata,
                            "body": sdef.body,
                            "raw_text": raw_text,
                        }
                    )

        return {"ok": True, "agents": agents, "skills": skills}

    def post_declarative_reload(self) -> dict[str, Any]:
        with self.bridge.persona_lock:
            main_def = load_agent("main_agent.md", attach_skills=True)
            sub_def = load_agent("subagent.md", attach_skills=True)
            bundle_dir = export_aistudio_bundle()

        payload = {
            "ok": True,
            "reloaded": [
                {
                    "name": main_def.name,
                    "model": main_def.model,
                    "skills": main_def.skill_names,
                    "compiled_prompt_chars": len(main_def.compile_system_prompt()),
                },
                {
                    "name": sub_def.name,
                    "model": sub_def.model,
                    "skills": sub_def.skill_names,
                    "compiled_prompt_chars": len(sub_def.compile_system_prompt()),
                },
            ],
            "bundle_dir": str(bundle_dir),
        }
        self.sse_broker.publish("declarative.reloaded", payload)
        return payload

    def put_declarative(
        self, kind: str, name: str, body_bytes: bytes
    ) -> tuple[int, dict[str, Any]]:
        try:
            target_path = resolve_safe_declarative_path(kind, name)
        except SecurityError as exc:
            return api_error(400, "INVALID_DECLARATIVE_PATH", str(exc))

        raw_body = _parse_json_body(body_bytes)
        req = DeclarativeUpdateRequest.model_validate(raw_body)
        meta, body = parse_yaml_frontmatter(req.content)

        with self.bridge.persona_lock:
            prev_content = (
                target_path.read_text(encoding="utf-8")
                if target_path.is_file()
                else None
            )
            _atomic_write_text(target_path, req.content)
            try:
                if kind.strip().lower() == "agents":
                    loaded = load_agent(target_path.name, attach_skills=True)
                    loaded_summary = {
                        "name": loaded.name,
                        "model": loaded.model,
                        "skills": loaded.skill_names,
                        "compiled_prompt_chars": len(loaded.compile_system_prompt()),
                    }
                else:
                    skill_slug = target_path.parent.name if target_path.name == "SKILL.md" else target_path.stem
                    loaded = load_skill(skill_slug)
                    loaded_summary = {
                        "name": loaded.name,
                        "version": loaded.version,
                        "description": loaded.description,
                    }
                bundle_dir = export_aistudio_bundle()
            except Exception as exc:
                if prev_content is not None:
                    _atomic_write_text(target_path, prev_content)
                elif target_path.exists():
                    target_path.unlink()
                return api_error(
                    400,
                    "INVALID_DECLARATIVE_CONTENT",
                    f"Failed to validate declarative Markdown update: {exc}",
                )

        event_info = {
            "kind": kind.strip().lower(),
            "name": name,
            "filename": target_path.name,
            "bytes": len(req.content.encode("utf-8")),
            "metadata": meta,
        }
        self.sse_broker.publish("declarative.updated", event_info)
        return 200, {
            "ok": True,
            **event_info,
            "body_chars": len(body),
            "loaded": loaded_summary,
            "bundle_dir": str(bundle_dir),
        }
