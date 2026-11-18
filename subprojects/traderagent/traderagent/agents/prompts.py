"""
Dynamic Declarative Prompt Builders for the 4-Level Quant Committee and General Q&A Assistant.
Loads `agents/main_agent.md`, `agents/subagent.md`, and `skills/*.md` via `src/loader.py`
so edits to the Markdown files take effect immediately without changing Python code.
"""
from src.loader import AgentDefinition, load_agent

L2_ROLE_DIRECTIVE = """
You are acting as the **Level-2 Alpha Strategist** within the `traderagent_subagent` persona.
Propose an initial `TradeDecisionDraft` from the 15m telemetry, 4-bar trajectory, and Level-1 catalyst report.
"""

L3_ROLE_DIRECTIVE = """
You are acting as the **Level-3 Chief Risk Officer & Adversarial Red-Team Critic** within the `traderagent_subagent` persona.
1. DETERMINISTIC PYTHON MATH AUDIT: You are given Python's exact fee-adjusted Net Risk/Reward calculation (`python_verified_net_rr`). If Level-2 claimed a different R:R than `python_verified_net_rr`, overwrite `net_risk_reward_ratio` with `python_verified_net_rr` and veto any BUY if `python_verified_net_rr < min_net_rr_ratio` (1.8).
2. MICROSTRUCTURE & LIQUIDITY TRAPS: Reject buying right into `or_high` or `vwap` overhead resistance without a confirmed close above, and reject chasing overextended bars (>1.5 ATR above EMA4).
3. CATALYST DIVERGENCE: If Level-1 Catalyst Intelligence shows active bearish breaking news/headwinds, veto speculative long entries.
4. CONFIDENCE CALIBRATION (0-100): Return your structured `AdversarialCritique` containing `verdict`, `confidence_score`, `flaws_found`, and the corrected `refined_decision`.
"""


def get_main_agent_definition() -> AgentDefinition:
    """Loads `agents/main_agent.md` with its attached `skills/*.md`."""
    return load_agent("main_agent.md", attach_skills=True)


def get_subagent_definition() -> AgentDefinition:
    """Loads `agents/subagent.md` with its attached `skills/*.md`."""
    return load_agent("subagent.md", attach_skills=True)


def build_chat_assistant_prompt() -> tuple[str, AgentDefinition]:
    agent_def = get_main_agent_definition()
    return agent_def.compile_system_prompt(), agent_def


def build_l2_strategist_prompt() -> tuple[str, AgentDefinition]:
    sub_def = get_subagent_definition()
    return sub_def.compile_system_prompt(extra_instructions=L2_ROLE_DIRECTIVE), sub_def


def build_l3_critic_prompt() -> tuple[str, AgentDefinition]:
    sub_def = get_subagent_definition()
    return sub_def.compile_system_prompt(extra_instructions=L3_ROLE_DIRECTIVE), sub_def
