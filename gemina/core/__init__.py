"""
Gemina Core Framework Layer:
Provides reusable agentic primitives, arrival-to-leaving pipeline tracing,
cloud-to-local LLM failover routing, and host/GPU diagnostic execution.
"""
from gemina.core.interfaces import AgentCapability, AgentRegistry, SubAgentPlugin
from gemina.core.tracing import PipelineTracer
from gemina.core.llm_router import CognitiveLLMRouter
from gemina.core.diagnostics import SystemDiagnostics

__all__ = [
    "AgentCapability",
    "AgentRegistry",
    "SubAgentPlugin",
    "PipelineTracer",
    "CognitiveLLMRouter",
    "SystemDiagnostics",
]
