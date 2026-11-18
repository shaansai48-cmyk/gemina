"""
Gemina — Top-Level Personal Multi-Agent Framework.
"""
from gemina.core.interfaces import AgentCapability, AgentRegistry, SubAgentPlugin
from gemina.core.tracing import PipelineTracer
from gemina.core.llm_router import CognitiveLLMRouter
from gemina.core.diagnostics import SystemDiagnostics

__version__ = "1.0.0"
__all__ = [
    "AgentCapability",
    "AgentRegistry",
    "SubAgentPlugin",
    "PipelineTracer",
    "CognitiveLLMRouter",
    "SystemDiagnostics",
]
