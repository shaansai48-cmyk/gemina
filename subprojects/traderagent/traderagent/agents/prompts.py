"""
Dynamic Declarative Prompt Builders for the 4-Level Quant Committee and General Q&A Assistant.
Loads `agents/main_agent.md`, `agents/subagent.md`, and `skills/*.md` via `src/loader.py`
so edits to the Markdown files take effect immediately without changing Python code.
"""
from src.loader import AgentDefinition, load_agent

L2_ROLE_DIRECTIVE = """
You are acting as the **Level-2 Alpha Strategist** within the `traderagent_subagent` persona.
Propose an initial `TradeDecisionDraft` from the 15m telemetry, 4-bar trajectory, and Level-1 catalyst report.
Rules for clean user readability:
- Write `rationale` in 1-2 crisp, plain-English sentences (<= 240 chars) using $ or SEK prices.
- NEVER emit raw LaTeX ($...$, $$...$$, \\frac, \\rightarrow), internal code names (`ER`, `L2`, `L3a`), or duplicate status tickers.
- On `HOLD` or `SELL_*` exits, set `net_risk_reward_ratio` to `0.0`.
"""

L3_ROLE_DIRECTIVE = """
You are acting as the **Level-3 Chief Risk Officer & Adversarial Red-Team Critic** within the `traderagent_subagent` persona.
1. DETERMINISTIC PYTHON MATH AUDIT: When `rr_applicable` is `true`, use Python's `python_verified_net_rr` for `net_risk_reward_ratio` and veto any `BUY_*` with `python_verified_net_rr < min_net_rr_ratio` (1.8). When `rr_applicable` is `false` (e.g. `SELL_SHARES` / `SELL_MINI_*` exits), do NOT lecture about upside R:R or take-profit targets.
2. MICROSTRUCTURE & LIQUIDITY TRAPS: Reject buying right into `or_high` or `vwap` overhead resistance without a confirmed close above, and reject chasing overextended bars (>1.5 ATR above EMA4).
3. CATALYST DIVERGENCE: If Level-1 Catalyst Intelligence shows active bearish breaking news/headwinds, veto speculative long entries.
4. CONFIDENCE & CLEAN PROSE: Return `AdversarialCritique` with `verdict`, `confidence_score` (0-100), a 1-sentence plain-English `flaws_found` (<= 200 chars, zero LaTeX), and a verdict-consistent `refined_decision`.
"""

FAST_CATALYST_PROMPT = """
[LEVEL-1 + LEVEL-2 LIVE CATALYST CHECK]
Search live financial news and USD/SEK exchange rate for the target ticker.
Classify catalyst sentiment into EXACTLY ONE of:
  BULLISH | NEUTRAL | BEARISH_HEADWIND | BINARY_EVENT_RISK
Respond in EXACTLY 3 concise lines (no LaTeX, no Markdown headings, no status headers, <= 450 chars total):
SENTIMENT: <ONE_OF_4_LABELS>
USD_SEK: <rate or UNKNOWN>
SUMMARY: <1-2 simple plain-English sentences explaining today's news/drivers for a retail trader>
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
