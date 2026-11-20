"""
TraderAgent — Multi-Ticker Quantitative Trading Sub-Agent for the Gemina Personal Agent Framework.
Can be run standalone or imported as a modular SubAgentPlugin into the top-level Gemina orchestrator.
"""
from traderagent.config import TraderConfig
from traderagent.domain.models import (
    TICKER_ALIAS_MAP,
    AdversarialCritique,
    TradeDecision,
    TradeDecisionDraft,
    normalize_ticker,
    resolve_focus_ticker,
)
from traderagent.domain.quant_math import (
    build_deterministic_candidate,
    build_plain_english_reasons,
    compute_quant_metrics,
    compute_statistical_confidence,
    compute_verified_fee_adjusted_rr,
    format_avanza_order_instructions,
    format_trade_alert_html,
)
from traderagent.domain.guardrails import enforce_hard_quant_guardrails
from traderagent.plugin import TraderAgentPlugin, create_trader_agent

__version__ = "1.0.0"
__all__ = [
    "TraderConfig",
    "TICKER_ALIAS_MAP",
    "normalize_ticker",
    "resolve_focus_ticker",
    "TradeDecisionDraft",
    "AdversarialCritique",
    "TradeDecision",
    "compute_quant_metrics",
    "compute_statistical_confidence",
    "compute_verified_fee_adjusted_rr",
    "format_avanza_order_instructions",
    "build_deterministic_candidate",
    "build_plain_english_reasons",
    "format_trade_alert_html",
    "enforce_hard_quant_guardrails",
    "TraderAgentPlugin",
    "create_trader_agent",
]
