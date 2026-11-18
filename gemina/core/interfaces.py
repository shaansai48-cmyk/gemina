"""
Gemina Agentic Framework Interfaces & Plugin Registry.
Allows subprojects (such as `traderagent`) to be imported and mounted as modular sub-agents.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
from gemina.core.tracing import PipelineTracer


@dataclass(frozen=True)
class AgentCapability:
    """Describes a capability or command exposed by a Gemina sub-agent."""
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)


class SubAgentPlugin(ABC):
    """
    Standard contract that every Gemina subproject must implement to be imported
    into the top-level Gemina Personal Agent orchestrator.
    """

    @property
    @abstractmethod
    def agent_id(self) -> str:
        """Unique identifier for the sub-agent (e.g., 'traderagent')."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable summary of the sub-agent's domain and role."""

    @abstractmethod
    def capabilities(self) -> list[AgentCapability]:
        """List of capabilities and slash commands handled by this sub-agent."""

    @abstractmethod
    def health_status(self) -> dict[str, Any]:
        """Return traffic-light health status (🟢 GREEN / 🔴 RED) and compact ticker."""

    @abstractmethod
    def handle_message(self, text: str, tracer: Optional[PipelineTracer] = None) -> str:
        """Process a user command or natural-language query routed to this sub-agent."""

    @abstractmethod
    def poll_external_events(self) -> int:
        """Run one non-blocking tick of external event ingestion (e.g., webhooks). Returns events processed."""


class AgentRegistry:
    """
    Top-level Gemina registry that mounts sub-agent plugins (like `traderagent`)
    and routes commands or queries across the personal agent ecosystem.
    """

    def __init__(self):
        self._agents: dict[str, SubAgentPlugin] = {}

    def register(self, plugin: SubAgentPlugin) -> None:
        self._agents[plugin.agent_id] = plugin

    def get(self, agent_id: str) -> Optional[SubAgentPlugin]:
        return self._agents.get(agent_id)

    def list_agents(self) -> list[SubAgentPlugin]:
        return list(self._agents.values())
