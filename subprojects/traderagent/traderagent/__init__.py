"""
TraderAgent — Multi-Ticker Quantitative Trading Sub-Agent for the Gemina Personal Agent Framework.
Can be run standalone or imported as a modular SubAgentPlugin into the top-level Gemina orchestrator.
"""
from traderagent.config import TraderConfig
from traderagent.domain.models import AdversarialCritique, TradeDecision, TradeDecisionDraft
from traderagent.domain.quant_math import (
    compute_quant_metrics,
    compute_statistical_confidence,
    compute_verified_fee_adjusted_rr,
    format_avanza_order_instructions,
)
from traderagent.domain.guardrails import enforce_hard_quant_guardrails
from traderagent.plugin import TraderAgentPlugin, create_trader_agent

__version__ = "1.0.0"
__all__ = [
    "TraderConfig",
    "TradeDecisionDraft",
    "AdversarialCritique",
    "TradeDecision",
    "compute_quant_metrics",
    "compute_statistical_confidence",
    "compute_verified_fee_adjusted_rr",
    "format_avanza_order_instructions",
    "enforce_hard_quant_guardrails",
    "TraderAgentPlugin",
    "create_trader_agent",
]
