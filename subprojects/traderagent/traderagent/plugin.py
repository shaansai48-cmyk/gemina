"""
Gemina SubAgentPlugin Implementation for TraderAgent.
Allows the top-level Gemina Personal Agent project (or any external Python project)
to import and mount `traderagent` as an agentic module.
"""
from typing import Any, Optional
from gemina.core.interfaces import AgentCapability, SubAgentPlugin
from gemina.core.tracing import PipelineTracer
from traderagent.application.daemon import TraderAgentDaemon
from traderagent.config import TraderConfig


class TraderAgentPlugin(SubAgentPlugin):
    """
    Adapter exposing TraderAgent as a pluggable Gemina Sub-Agent.
    Example usage from Gemina top-level orchestrator:
        from gemina import AgentRegistry
        from traderagent import create_trader_agent

        registry = AgentRegistry()
        registry.register(create_trader_agent())
    """

    def __init__(self, config: Optional[TraderConfig] = None):
        self.daemon = TraderAgentDaemon(config=config)

    @property
    def agent_id(self) -> str:
        return "traderagent"

    @property
    def description(self) -> str:
        return (
            "Multi-Ticker Avanza ISK Quantitative Trading Agent with 4-Level Focused Agentic Committee "
            "(L1 Catalyst -> L2 Strategist -> L3a Python Math Auditor + L3b Adversarial Critic -> L4 Guardrails)."
        )

    def capabilities(self) -> list[AgentCapability]:
        return [
            AgentCapability("status", "Compact 1-line traffic-light status ticker (expands on RED)", ["/status", "/fullstatus", "/sys"]),
            AgentCapability("trace", "Arrival-to-leaving multi-agent pipeline trace inspection", ["/trace"]),
            AgentCapability("watchlist", "Manage multi-ticker watchlist and active focus stock", ["/watchlist", "/focus", "/add_ticker"]),
            AgentCapability("execution_ledger", "Log Avanza Mini Future & share fills/closes", ["/fill_mini", "/close_mini", "/fill_shares", "/close_shares", "/set_stop", "/set_mini"]),
            AgentCapability("quant_analysis", "On-demand deep quant & live-news analysis via Gemini 4 Argon", ["/analyze", "/ask"]),
        ]

    def health_status(self) -> dict[str, Any]:
        return self.daemon.status_evaluator.evaluate()

    def handle_message(self, text: str, tracer: Optional[PipelineTracer] = None) -> str:
        own_tracer = False
        if tracer is None:
            tracer = PipelineTracer(
                flow="gemina_plugin_call",
                source="gemina_orchestrator",
                prefix="plugin-trader",
                trace_file_path=self.daemon.config.pipeline_trace_path,
            )
            own_tracer = True
        body, st = self.daemon.command_router.execute_text_command(text, tracer=tracer)
        formatted = self.daemon.status_evaluator.format_with_status_ticker(body, st)
        if own_tracer:
            tracer.complete("COMPLETED")
        return formatted

    def poll_external_events(self) -> int:
        self.daemon.last_tg_update_id = self.daemon.command_router.poll_and_handle(self.daemon.last_tg_update_id)
        return self.daemon.process_incoming_bars()


def create_trader_agent(config: Optional[TraderConfig] = None) -> TraderAgentPlugin:
    """Factory function to instantiate the TraderAgentPlugin."""
    return TraderAgentPlugin(config=config)
