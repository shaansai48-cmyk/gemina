"""
Domain Entities & Value Objects for Quantitative Trading Decisions.
Supports bidirectional trading (MINI L Longs, MINI S Shorts, and Regular Shares),
canonical ticker normalization/focus resolution, and step-by-step Avanza order instructions.
"""
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

TICKER_ALIAS_MAP: dict[str, str] = {
    "NVIDIA": "NVDA",
    "TESLA": "TSLA",
    "MICROSOFT": "MSFT",
    "APPLE": "AAPL",
    "GOOGLE": "GOOGL",
    "ALPHABET": "GOOGL",
    "AMAZON": "AMZN",
    "META": "META",
    "FACEBOOK": "META",
    "AMD": "AMD",
    "ERICSSON": "ERIC B",
    "ERICSSON B": "ERIC B",
    "ERIC": "ERIC B",
    "ERIC-B": "ERIC B",
    "ERIC-B.ST": "ERIC B",
    "ERIC B.ST": "ERIC B",
}


def normalize_ticker(raw: str) -> str:
    """Normalizes a raw ticker or company alias (e.g. '$nvda', 'ERIC_B', 'Ericsson') to canonical symbol."""
    cleaned = " ".join(str(raw or "").strip().lstrip("$").upper().replace("_", " ").split())
    if not cleaned:
        return ""
    return TICKER_ALIAS_MAP.get(cleaned, cleaned)


def resolve_focus_ticker(
    portfolio: dict[str, Any],
    explicit_ticker: Optional[str] = None,
    prefer_held_mini: bool = False,
    prefer_held_shares: bool = False,
) -> str:
    """Resolves a single watchlist ticker safely even when active_focus_ticker is comma-separated."""
    watchlist = portfolio.get("watchlist", {})
    if explicit_ticker:
        norm = normalize_ticker(explicit_ticker)
        if norm in watchlist:
            return norm

    focus_raw = str(portfolio.get("active_focus_ticker", "NVDA"))
    focus_list = [normalize_ticker(t) for t in focus_raw.split(",") if t.strip()]
    valid_focus = [t for t in focus_list if t in watchlist]

    if prefer_held_mini:
        for sym in valid_focus + list(watchlist.keys()):
            if int(watchlist.get(sym, {}).get("mini_units_held", 0)) > 0:
                return sym

    if prefer_held_shares:
        for sym in valid_focus + list(watchlist.keys()):
            if int(watchlist.get(sym, {}).get("regular_shares_held", 0)) > 0:
                return sym

    if valid_focus:
        return valid_focus[0]
    return next(iter(watchlist.keys()), "NVDA")


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
    plain_reasons: list[str] = Field(default_factory=list)
    catalyst_sentiment: str = ""
    catalyst_summary: str = ""
    rr_applicable: bool = True
    ratchet_triggered: bool = False
    data_timestamp_cet: str = ""
    market_open: Optional[bool] = None
