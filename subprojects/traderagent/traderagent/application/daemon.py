"""
Main Runtime Daemon & Webhook Bar Orchestrator for TraderAgent.
"""
import argparse
import time
from typing import Optional
from gemina.core.llm_router import CognitiveLLMRouter
from gemina.core.tracing import PipelineTracer
from traderagent.agents.committee import QuantAgentCommittee
from traderagent.agents.qa_assistant import ConversationalQAAgent
from traderagent.application.command_router import TelegramCommandRouter
from traderagent.config import TraderConfig
from traderagent.domain.quant_math import compute_quant_metrics
from traderagent.infrastructure.apps_script_client import AppsScriptClient
from traderagent.infrastructure.persistence import StateRepository
from traderagent.infrastructure.system_status import StatusEvaluator
from traderagent.infrastructure.telegram_adapter import TelegramGateway


class TraderAgentDaemon:
    """Cohesive runtime assembly wiring Domain, Agents, and Infrastructure layers."""

    def __init__(self, config: Optional[TraderConfig] = None):
        self.config = config or TraderConfig.from_env()
        self.repo = StateRepository(self.config)
        self.apps_script = AppsScriptClient(self.config.apps_script_url)
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
        )
        self.last_tg_update_id: int = 0
        self.last_apps_script_poll: float = 0.0

    def process_incoming_bars(self) -> int:
        """Polls Apps Script webhook and runs the 4-level agentic quant loop on unseen bars."""
        bars = self.apps_script.poll_bars()
        if not bars:
            return 0

        processed_count = 0
        portfolio = self.repo.load_portfolio()
        risk = self.repo.load_risk()
        seen_map = portfolio.setdefault("last_seen_ts_by_ticker", {})

        for ts, payload in bars:
            incoming_ticker = payload.get("ticker", "").upper().replace("_", " ")
            payload["ticker"] = incoming_ticker
            if not incoming_ticker:
                continue
            if seen_map.get(incoming_ticker) == ts:
                continue

            tracer = PipelineTracer(
                flow="15m_quant_bar",
                source="apps_script_webhook",
                prefix=f"bar-{incoming_ticker.replace(' ', '')}",
                trace_file_path=self.config.pipeline_trace_path,
            )
            prev_ts = seen_map.get(incoming_ticker)
            seen_map[incoming_ticker] = ts
            self.repo.save_portfolio(portfolio)

            tracer.add_stage(
                stage="INGEST_ARRIVAL",
                status="NEW_BAR",
                input_data={"ticker": incoming_ticker, "bar_ts": ts, "close": payload.get("close")},
                transformation=f"Deduplicated against last_seen_ts ({prev_ts} -> {ts})",
                output_data={"telemetry_keys": list(payload.keys())},
            )

            focus_list = [x.strip().upper() for x in portfolio["active_focus_ticker"].split(",") if x.strip()]
            focus_display = ",".join(focus_list)
            asset = portfolio["watchlist"].get(incoming_ticker)

            if asset and (
                incoming_ticker in focus_list
                or asset["regular_shares_held"] > 0
                or asset["mini_units_held"] > 0
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

                t_metrics = time.perf_counter()
                metrics = compute_quant_metrics(payload, asset)
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
                )

                if st["is_red"]:
                    guide_block = f"\n\n{decision.avanza_execution_guide}" if decision.avanza_execution_guide else ""
                    alert_body = (
                        f"📊 *{incoming_ticker} 15m Quant Signal* (Close: `{payload['close']}`)\n"
                        f"• *Regime:* `{decision.market_regime}` (`{metrics.get('regime_archetype', 'ADAPTIVE')}`)\n"
                        f"• *Action:* *{decision.action}* (`{decision.quantity}`x `{decision.instrument}`)\n"
                        f"• *Confidence:* `{decision.confidence_score}/100` (`{decision.critic_verdict}`)\n"
                        f"• *Limit:* `{decision.limit_price}` | *Target:* `{decision.take_profit_target}`\n"
                        f"• *Underlying Stop:* `{decision.underlying_stop_price}` | *Verified Net R:R:* `1 : {decision.net_risk_reward_ratio}`\n"
                        f"• *Mini Stop (Trigger/Sell):* `{decision.avanza_stop_trigger_sek}` / `{decision.avanza_stop_sell_sek} SEK`\n"
                        f"• *Critic Audit:* {decision.critic_notes}\n"
                        f"• *Rationale:* {decision.rationale}\n"
                        f"• *Trace:* `{tracer.trace_id}`"
                        f"{guide_block}"
                    )
                else:
                    alert_body = (
                        f"📊 *15m {incoming_ticker}:* `{decision.action}` | "
                        f"Conf `{decision.confidence_score}/100` (`{decision.critic_verdict}`) | "
                        f"`{decision.market_regime}` | R:R `1:{decision.net_risk_reward_ratio}` | "
                        f"Trace `{tracer.trace_id}`"
                    )
                self.telegram.send_message(alert_body, include_ticker=True, status_info=st, tracer=tracer)
                tracer.complete("COMPLETED")
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

    def run_forever(self, focus_override: Optional[str] = None) -> None:
        portfolio = self.repo.load_portfolio()
        if focus_override:
            portfolio["active_focus_ticker"] = focus_override.upper().replace("_", " ")
            self.repo.save_portfolio(portfolio)

        print(
            f"🚀 Gemina TraderAgent Daemon active (4-Level Agent Loop | Focus: {portfolio['active_focus_ticker']}, "
            f"Cloud: {self.config.gemini_model}, Local Fallback: {self.config.ollama_model}). "
            f"Send /help or any question in Telegram anytime."
        )

        while True:
            self.last_tg_update_id = self.command_router.poll_and_handle(self.last_tg_update_id)
            now = time.time()
            if now - self.last_apps_script_poll >= 8.0:
                self.last_apps_script_poll = now
                self.process_incoming_bars()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemina TraderAgent — Multi-Ticker Avanza Quant Copilot Daemon")
    parser.add_argument("--focus", type=str, help="Override active focus ticker (e.g. NVDA, 'ERIC B', MSFT)")
    args = parser.parse_args()
    daemon = TraderAgentDaemon()
    daemon.run_forever(focus_override=args.focus)


if __name__ == "__main__":
    main()
