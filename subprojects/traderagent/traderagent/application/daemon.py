"""
Main Runtime Daemon & Webhook Bar Orchestrator for TraderAgent.
Thread-safe, restart-idempotent (persists Telegram offset & exit-alert dedup in runtime_state.json),
and enriches partial TradingView webhooks with live Yahoo Finance telemetry when needed.
"""
import argparse
import html
import threading
import time
from typing import Any, Callable, Optional

from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.llm_router import CognitiveLLMRouter
from gemina.core.tracing import PipelineTracer
from traderagent.agents.committee import QuantAgentCommittee
from traderagent.agents.qa_assistant import ConversationalQAAgent
from traderagent.application.command_router import TelegramCommandRouter
from traderagent.config import TraderConfig
from traderagent.domain.models import normalize_ticker
from traderagent.domain.quant_math import compute_quant_metrics, format_trade_alert_html
from traderagent.infrastructure.apps_script_client import AppsScriptClient
from traderagent.infrastructure.market_data import YahooFinanceClient
from traderagent.infrastructure.persistence import StateRepository, redact_secrets
from traderagent.infrastructure.system_status import StatusEvaluator
from traderagent.infrastructure.telegram_adapter import TelegramGateway


class TraderAgentDaemon:
    """Cohesive runtime assembly wiring Domain, Agents, MarketData, and Infrastructure layers."""

    EXIT_DEDUP_COOLDOWN_SEC: float = 3600.0

    def __init__(self, config: Optional[TraderConfig] = None):
        self.config = config or TraderConfig.from_env()
        self.repo = StateRepository(self.config)
        self.apps_script = AppsScriptClient(self.config.apps_script_url)
        self.market_data = YahooFinanceClient()
        self.status_evaluator = StatusEvaluator(self.repo, self.apps_script)
        self.telegram = TelegramGateway(self.config, self.status_evaluator)
        self.llm_router = CognitiveLLMRouter(
            api_key=self.config.gemini_api_key,
            gemini_model=self.config.gemini_model,
            ollama_model=self.config.ollama_model,
        )
        self.committee = QuantAgentCommittee(self.llm_router)
        self.qa_agent = ConversationalQAAgent(self.llm_router, self.repo)
        self.command_router = TelegramCommandRouter(
            config=self.config,
            repo=self.repo,
            status_evaluator=self.status_evaluator,
            telegram=self.telegram,
            qa_agent=self.qa_agent,
            committee=self.committee,
            market_data=self.market_data,
        )
        rt = self.repo.load_runtime_state() if hasattr(self.repo, "load_runtime_state") else {}
        self.last_tg_update_id: int = int(rt.get("last_tg_update_id", 0) or 0)
        self.last_apps_script_poll: float = 0.0
        self._bar_lock = threading.Lock()
        self._listener_lock = threading.Lock()
        self._bar_listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_bar_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._listener_lock:
            if callback not in self._bar_listeners:
                self._bar_listeners.append(callback)

    def remove_bar_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._listener_lock:
            if callback in self._bar_listeners:
                self._bar_listeners.remove(callback)

    def _emit_bar_event(self, payload: dict[str, Any]) -> None:
        with self._listener_lock:
            listeners = list(self._bar_listeners)
        for cb in listeners:
            try:
                cb(payload)
            except Exception:
                pass

    def _trace_file_path(self) -> Optional[str]:
        repo_cfg = getattr(self.repo, "config", None)
        return getattr(repo_cfg, "pipeline_trace_path", self.config.pipeline_trace_path)

    def _enrich_partial_telemetry(self, ticker: str, payload: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        """Fills missing Pine telemetry fields (ema50, rvol, efficiency_ratio, etc.) without failing the bar."""
        clean = redact_secrets(dict(payload))
        clean["ticker"] = ticker
        needed = ("ema50", "rvol", "efficiency_ratio", "data_timestamp_cet", "market_open", "usd_sek")
        if all(k in clean and clean[k] is not None for k in needed):
            return clean
        try:
            live = self.market_data.fetch_15m_telemetry(ticker, asset=asset)
            for k in ("ema50", "rvol", "efficiency_ratio", "pd_high", "or_low", "data_timestamp_cet", "market_open", "usd_sek", "day_change_pct"):
                if clean.get(k) is None and k in live:
                    clean[k] = live[k]
        except Exception:
            pass
        return clean

    def _should_suppress_exit_alert(self, ticker: str, action: str, close: float, asset: dict[str, Any]) -> bool:
        if action not in ("SELL_SHARES", "SELL_MINI_LONG", "SELL_MINI_SHORT"):
            return False
        if not hasattr(self.repo, "update_runtime_state"):
            return False

        key = f"{ticker}:{action}"
        pos_qty = int(asset.get("regular_shares_held", 0)) if action == "SELL_SHARES" else int(asset.get("mini_units_held", 0))
        pos_stop = float(asset.get("hard_stop_price", 0.0))
        now = time.time()
        suppress_flag = False

        def _mut(state: dict[str, Any]) -> None:
            nonlocal suppress_flag
            dedup = state.setdefault("alert_dedup", {})
            prev = dedup.get(key)
            if isinstance(prev, dict):
                age = now - float(prev.get("epoch", 0.0))
                same_qty = int(prev.get("qty", -1)) == pos_qty
                same_stop = abs(float(prev.get("stop", -1.0)) - pos_stop) < 1e-6
                prev_close = float(prev.get("close", close) or close)
                pct_delta = abs(close - prev_close) / max(prev_close, 1e-6)
                if age < self.EXIT_DEDUP_COOLDOWN_SEC and same_qty and same_stop and pct_delta < 0.015:
                    suppress_flag = True
                    return
            dedup[key] = {
                "epoch": now,
                "close": round(close, 4),
                "qty": pos_qty,
                "stop": pos_stop,
            }

        self.repo.update_runtime_state(_mut)
        return suppress_flag

    def process_incoming_bars(self) -> int:
        """Polls Apps Script webhook and runs the 4-level agentic quant loop on unseen bars."""
        with self._bar_lock:
            bars = self.apps_script.poll_bars()
            if not bars:
                return 0

            processed_count = 0
            trace_path = self._trace_file_path()

            for ts, raw_payload in bars:
                payload = redact_secrets(dict(raw_payload))
                incoming_ticker = normalize_ticker(str(payload.get("ticker", "")))
                payload["ticker"] = incoming_ticker
                if not incoming_ticker:
                    continue

                already_seen = False
                prev_ts = None

                def _mark_seen(p: dict[str, Any]) -> None:
                    nonlocal already_seen, prev_ts
                    seen_map = p.setdefault("last_seen_ts_by_ticker", {})
                    prev_ts = seen_map.get(incoming_ticker)
                    if prev_ts == ts:
                        already_seen = True
                        return
                    seen_map[incoming_ticker] = ts

                portfolio, _ = self.repo.update_portfolio(_mark_seen)
                if already_seen:
                    continue

                risk = self.repo.load_risk()
                tracer = PipelineTracer(
                    flow="15m_quant_bar",
                    source="apps_script_webhook",
                    prefix=f"bar-{incoming_ticker.replace(' ', '')}",
                    trace_file_path=trace_path,
                )
                tracer.add_stage(
                    stage="INGEST_ARRIVAL",
                    status="NEW_BAR",
                    input_data={"ticker": incoming_ticker, "bar_ts": ts, "close": payload.get("close")},
                    transformation=f"Deduplicated against last_seen_ts ({prev_ts} -> {ts})",
                    output_data={"telemetry_keys": list(payload.keys())},
                )

                focus_list = [normalize_ticker(x) for x in str(portfolio["active_focus_ticker"]).split(",") if x.strip()]
                focus_display = ",".join(focus_list)
                asset = portfolio.get("watchlist", {}).get(incoming_ticker)

                if asset and (
                    incoming_ticker in focus_list
                    or int(asset.get("regular_shares_held", 0)) > 0
                    or int(asset.get("mini_units_held", 0)) > 0
                ):
                    tracer.add_stage(
                        stage="PORTFOLIO_FILTER",
                        status="ACCEPTED",
                        input_data={"incoming": incoming_ticker, "active_focus": focus_display},
                        transformation="Matched active focus ticker or held position",
                        output_data={
                            "shares_held": asset["regular_shares_held"],
                            "mini_held": asset["mini_units_held"],
                        },
                    )

                    payload = self._enrich_partial_telemetry(incoming_ticker, payload, asset)
                    t_metrics = time.perf_counter()
                    metrics = compute_quant_metrics(payload, asset, usd_sek=float(payload.get("usd_sek") or 10.16))
                    tracer.add_stage(
                        stage="QUANT_METRICS_TRANSFORM",
                        status="OK",
                        input_data={"close": payload.get("close"), "ema4": payload.get("ema4"), "vwap": payload.get("vwap")},
                        transformation="Computed Mini theoretical SEK price, effective leverage, FX drag & EMA4/VWAP deltas",
                        output_data=metrics,
                        duration_ms=(time.perf_counter() - t_metrics) * 1000.0,
                    )

                    recent_ticker_bars = [
                        {"ts": b.get("ts"), "close": b.get("telemetry", {}).get("close"), "action": b.get("decision", {}).get("action")}
                        for b in self.repo.get_recent_audit_tail(4, ticker=incoming_ticker)
                    ]
                    ticker_exp = self.repo.experience.get_ticker_experience(incoming_ticker)

                    decision = self.committee.evaluate_bar(
                        telemetry=payload,
                        portfolio=portfolio,
                        asset=asset,
                        risk=risk,
                        metrics=metrics,
                        recent_ticker_bars=recent_ticker_bars,
                        ticker_experience=ticker_exp,
                        tracer=tracer,
                    )

                    self.repo.append_audit_log({
                        "trace_id": tracer.trace_id,
                        "ts": ts,
                        "ticker": incoming_ticker,
                        "telemetry": payload,
                        "decision": decision.model_dump(),
                    })

                    st = self.status_evaluator.evaluate(
                        portfolio=portfolio,
                        risk=risk,
                        latest_telemetry=payload,
                        latest_decision=decision,
                        ticker=incoming_ticker,
                    )

                    suppress = self._should_suppress_exit_alert(
                        ticker=incoming_ticker,
                        action=decision.action,
                        close=float(payload.get("close", 0.0)),
                        asset=asset,
                    )
                    if suppress:
                        tracer.add_stage(
                            stage="ALERT_DEDUP",
                            status="SUPPRESSED_DUPLICATE_EXIT_ALERT",
                            input_data={"ticker": incoming_ticker, "action": decision.action},
                            transformation="Skipped duplicate exit alert within cooldown window",
                            output_data={"close": payload.get("close")},
                        )
                    else:
                        if st["is_red"] or decision.action != "HOLD" or decision.ratchet_triggered:
                            alert_body = format_trade_alert_html(
                                decision=decision,
                                telemetry=payload,
                                portfolio=portfolio,
                                asset=asset,
                                metrics=metrics,
                                risk=risk,
                                source_label="15m Signal",
                            )
                        else:
                            spot_val = float(payload.get("close", 0.0))
                            cur = str(asset.get("currency", "USD"))
                            spot_txt = f"${spot_val:.2f}" if cur == "USD" else f"{spot_val:.2f} SEK"
                            alert_body = (
                                f"⏸️ <b>{html.escape(incoming_ticker)} 15m Update: HOLD</b> ({html.escape(spot_txt)})\n"
                                f"<i>No setup triggered. Stand aside; no action needed.</i>"
                            )
                        self.telegram.send_message(alert_body, include_ticker=True, status_info=st, tracer=tracer)

                    tracer.complete("COMPLETED")
                    self._emit_bar_event({
                        "ticker": incoming_ticker,
                        "ts": ts,
                        "trace_id": tracer.trace_id,
                        "action": decision.action,
                        "close": payload.get("close"),
                        "is_red": st["is_red"],
                        "telemetry": payload,
                        "decision": decision.model_dump(),
                        "metrics": metrics,
                        "status": st,
                    })
                    processed_count += 1
                else:
                    tracer.add_stage(
                        stage="PORTFOLIO_FILTER",
                        status="IGNORED",
                        input_data={"incoming": incoming_ticker, "active_focus": focus_display},
                        transformation=f"Ignored {incoming_ticker} (not active focus and 0 units held)",
                        output_data={"action": "DROP"},
                    )
                    tracer.complete("IGNORED_UNFOCUSED_TICKER")

            return processed_count

    def run_forever(
        self,
        focus_override: Optional[str] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        if focus_override:
            norm_focus = ",".join(normalize_ticker(x) for x in focus_override.split(",") if x.strip())

            def _set_focus(p: dict[str, Any]) -> None:
                p["active_focus_ticker"] = norm_focus

            portfolio, _ = self.repo.update_portfolio(_set_focus)
        else:
            portfolio = self.repo.load_portfolio()

        SystemDiagnostics.warm_cache()
        print(
            f"🚀 Gemina TraderAgent Daemon active (4-Level Agent Loop | Focus: {portfolio['active_focus_ticker']}, "
            f"Cloud: {self.config.gemini_model}, Local Fallback: {self.config.ollama_model}). "
            f"Send /help or any question in Telegram anytime."
        )

        while not (stop_event and stop_event.is_set()):
            try:
                self.last_tg_update_id = self.command_router.poll_and_handle(self.last_tg_update_id)
            except Exception as exc:
                print(f"[Daemon Telegram Poll Error] {type(exc).__name__}: {exc}")
                if stop_event:
                    stop_event.wait(2.0)
                else:
                    time.sleep(2.0)

            now = time.time()
            if now - self.last_apps_script_poll >= 8.0:
                self.last_apps_script_poll = now
                try:
                    self.process_incoming_bars()
                except Exception as exc:
                    print(f"[Daemon Bar Poll Error] {type(exc).__name__}: {exc}")

            if stop_event:
                stop_event.wait(0.25)
            else:
                time.sleep(0.25)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemina TraderAgent — Multi-Ticker Avanza Quant Copilot Daemon")
    parser.add_argument("--focus", type=str, help="Override active focus ticker (e.g. NVDA, 'ERIC B', MSFT)")
    args = parser.parse_args()
    daemon = TraderAgentDaemon()
    daemon.run_forever(focus_override=args.focus)


if __name__ == "__main__":
    main()
