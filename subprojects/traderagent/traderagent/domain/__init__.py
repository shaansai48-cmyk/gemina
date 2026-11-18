"""
TraderAgent Domain Layer:
Contains pure quantitative models, fee-adjusted math, and deterministic circuit breakers.
"""
from traderagent.domain.models import AdversarialCritique, TradeDecision, TradeDecisionDraft
from traderagent.domain.quant_math import compute_quant_metrics, compute_verified_fee_adjusted_rr
from traderagent.domain.guardrails import enforce_hard_quant_guardrails

__all__ = [
    "TradeDecisionDraft",
    "AdversarialCritique",
    "TradeDecision",
    "compute_quant_metrics",
    "compute_verified_fee_adjusted_rr",
    "enforce_hard_quant_guardrails",
]
