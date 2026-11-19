"""
Domain Entities & Value Objects for Quantitative Trading Decisions.
Supports bidirectional trading (MINI L Longs, MINI S Shorts, and Regular Shares)
and step-by-step Avanza order placement instructions.
"""
from typing import Literal, Optional
from pydantic import BaseModel, Field

MarketRegimeType = Literal[
    "Reversal_Base_Building",
    "Momentum_Breakout",
    "Downtrend_Continuation",
    "Volatile_Chop",
]

TradeActionType = Literal[
    "HOLD",
    "BUY_SHARES",
    "SELL_SHARES",
    "BUY_MINI_LONG",
    "SELL_MINI_LONG",
    "BUY_MINI_SHORT",
    "SELL_MINI_SHORT",
]


class TradeDecisionDraft(BaseModel):
    """Level-2 Alpha Strategist raw proposal before Adversarial Critic & Math Audit."""
    ticker: str
    market_regime: MarketRegimeType
    action: TradeActionType
    instrument: str
    quantity: int
    limit_price: Optional[float]
    avanza_stop_trigger_sek: Optional[float] = Field(description="Avanza Stop-Loss Triggerkurs in SEK.")
    avanza_stop_sell_sek: Optional[float] = Field(description="Avanza Stop-Loss Säljkurs (0.20 SEK below trigger).")
    underlying_stop_price: float
    take_profit_target: float
    net_risk_reward_ratio: float
    overnight_gap_warning: bool
    rationale: str


class AdversarialCritique(BaseModel):
    """Level-3 Adversarial Red-Team Critic output that audits and refines Level-2's draft."""
    verdict: Literal["APPROVED", "REVISED_BY_CRITIC", "VETOED_BY_CRITIC"]
    confidence_score: int = Field(description="Calibrated confidence score 0-100 after adversarial stress test.")
    flaws_found: str = Field(description="Specific bull-trap, catalyst-divergence, or fee/R:R flaws identified.")
    refined_decision: TradeDecisionDraft


class TradeDecision(BaseModel):
    """Final multi-agent TradeDecision after L1 Catalyst -> L2 Strategist -> L3 Critic -> L4 Guardrails."""
    ticker: str
    market_regime: MarketRegimeType
    action: TradeActionType
    instrument: str
    quantity: int
    limit_price: Optional[float]
    avanza_stop_trigger_sek: Optional[float] = Field(description="Avanza Stop-Loss Triggerkurs in SEK.")
    avanza_stop_sell_sek: Optional[float] = Field(description="Avanza Stop-Loss Säljkurs (0.20 SEK below trigger).")
    underlying_stop_price: float
    take_profit_target: float
    net_risk_reward_ratio: float
    overnight_gap_warning: bool
    rationale: str
    confidence_score: int = 75
    critic_verdict: str = "APPROVED"
    critic_notes: str = ""
    avanza_execution_guide: str = ""
