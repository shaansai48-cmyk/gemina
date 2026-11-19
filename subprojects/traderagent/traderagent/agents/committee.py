"""
4-Level Focused Agentic Quant Committee (Driven by Declarative `agents/subagent.md` & `skills/*.md`):
  L1: Catalyst & Macro Search Grounding Agent
  L2: Alpha Strategist & Setup Proposer Agent (with multi-bar trajectory memory)
  L3a: Deterministic Python Fee-Adjusted R:R Math Auditor
  L3b: Adversarial Red-Team Critic & Self-Correction Loop (calibrates confidence 0-100)
  L4: Hard Deterministic Python Circuit Breakers
"""
import json
import time
from pathlib import Path
from typing import Any, Optional
from gemina.core.llm_router import CognitiveLLMRouter
from gemina.core.tracing import PipelineTracer
from traderagent.agents.prompts import (
    build_l2_strategist_prompt,
    build_l3_critic_prompt,
    get_subagent_definition,
)
from traderagent.domain.guardrails import enforce_hard_quant_guardrails
from traderagent.domain.models import AdversarialCritique, TradeDecision, TradeDecisionDraft
from traderagent.domain.quant_math import compute_verified_fee_adjusted_rr


class QuantAgentCommittee:
    """Orchestrates the 4-level focused agentic loop using declarative Markdown personas and skills."""

    def __init__(self, llm_router: CognitiveLLMRouter):
        self.llm = llm_router

    def run_l1_catalyst_agent(
        self,
        ticker: str,
        system_instruction: str,
        bar_time: str = "",
        tracer: Optional[PipelineTracer] = None,
    ) -> str:
        t0 = time.perf_counter()
        date_clause = f" as of date/time {bar_time[:10]}" if bar_time else ""
        prompt = (
            f"[LEVEL-1 CATALYST & MACRO AGENT]\n"
            f"Search breaking financial news, intraday catalysts, analyst/earnings updates for {ticker}{date_clause}, "
            f"and the USD/SEK exchange rate. Output:\n"
            f"1. Catalyst Sentiment: [BULLISH / NEUTRAL / BEARISH_HEADWIND / BINARY_EVENT_RISK]\n"
            f"2. Key Drivers (2 concise bullets)\n"
            f"3. Live USD/SEK rate."
        )
        try:
            text, model_used, _ = self.llm.generate_grounded_text(
                prompt=prompt,
                system_instruction=system_instruction,
                use_google_search=True,
                temperature=0.1,
                allow_ollama_fallback=False,
            )
            if tracer:
                tracer.add_stage(
                    stage="L1_CATALYST_AGENT",
                    status="OK",
                    input_data={"ticker": ticker, "bar_date": bar_time[:10], "model": model_used},
                    transformation=f"Level-1 Agent synthesized live catalyst sentiment & USD/SEK via Google Search ({bar_time[:10] or 'live'})",
                    output_data={"summary_chars": len(text), "preview": text[:140]},
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            return text
        except Exception as e:
            print(f"[L1 Catalyst Warning] Continuing with pure telemetry: {e}")
            if tracer:
                tracer.add_stage(
                    stage="L1_CATALYST_AGENT",
                    status="DEGRADED_NO_SEARCH",
                    input_data={"ticker": ticker},
                    transformation=f"L1 Search failed ({type(e).__name__}); proceeding with price telemetry",
                    output_data={"error": str(e)[:120]},
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            return "Live search unavailable; rely strictly on price/volume telemetry."

    def run_l2_alpha_strategist(
        self,
        context_blob: str,
        live_news_context: str,
        ticker: str,
        tracer: Optional[PipelineTracer] = None,
    ) -> TradeDecisionDraft:
        t0 = time.perf_counter()
        l2_system_prompt, sub_def = build_l2_strategist_prompt()
        user_payload = (
            f"[LEVEL-2 ALPHA STRATEGIST]\n"
            f"Propose an initial TradeDecisionDraft for the 15m bar close on {ticker}.\n\n"
            f"LEVEL-1 CATALYST & FX INTELLIGENCE:\n{live_news_context}\n\n"
            f"TELEMETRY, MULTI-BAR HISTORY & PORTFOLIO PAYLOAD:\n{context_blob}"
        )
        draft, model_used, status = self.llm.generate_structured(
            prompt=user_payload,
            system_instruction=l2_system_prompt,
            schema=TradeDecisionDraft,
            temperature=sub_def.temperature,
        )
        if tracer:
            tracer.add_stage(
                stage="L2_ALPHA_PROPOSER",
                status=status,
                input_data={"model": model_used, "persona": sub_def.name, "context_bytes": len(context_blob)},
                transformation=f"Level-2 Strategist proposed {draft.action} ({draft.market_regime}, claimed R:R 1:{draft.net_risk_reward_ratio})",
                output_data={
                    "proposed_action": draft.action,
                    "regime": draft.market_regime,
                    "proposed_stop": draft.underlying_stop_price,
                    "claimed_rr": draft.net_risk_reward_ratio,
                },
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return draft

    def run_l3_adversarial_critic(
        self,
        draft: TradeDecisionDraft,
        math_audit: dict[str, Any],
        context_blob: str,
        live_news_context: str,
        tracer: Optional[PipelineTracer] = None,
    ) -> AdversarialCritique:
        t0 = time.perf_counter()
        l3_system_prompt, sub_def = build_l3_critic_prompt()
        critic_input = (
            f"[LEVEL-3 ADVERSARIAL RED-TEAM CRITIC & RISK AUDITOR]\n"
            f"Audit and stress-test the Level-2 Alpha Strategist's draft decision.\n\n"
            f"DETERMINISTIC PYTHON MATH AUDIT (GROUND TRUTH):\n{json.dumps(math_audit, indent=2)}\n\n"
            f"LEVEL-2 DRAFT PROPOSAL:\n{draft.model_dump_json(indent=2)}\n\n"
            f"LEVEL-1 CATALYST INTELLIGENCE:\n{live_news_context}\n\n"
            f"RAW TELEMETRY & PORTFOLIO CONTEXT:\n{context_blob}"
        )
        critique, model_used, raw_status = self.llm.generate_structured(
            prompt=critic_input,
            system_instruction=l3_system_prompt,
            schema=AdversarialCritique,
            temperature=sub_def.temperature,
        )
        status = critique.verdict if raw_status == "OK" else f"{critique.verdict}_OLLAMA"
        critique.refined_decision.net_risk_reward_ratio = math_audit["python_verified_net_rr"]

        if tracer:
            tracer.add_stage(
                stage="L3_ADVERSARIAL_CRITIC_LOOP",
                status=status,
                input_data={
                    "model": model_used,
                    "persona": sub_def.name,
                    "l2_action": draft.action,
                    "l2_rr": math_audit["llm_claimed_rr"],
                    "python_rr": math_audit["python_verified_net_rr"],
                },
                transformation=(
                    f"Critic verdict={critique.verdict} (confidence={critique.confidence_score}/100): "
                    f"{draft.action} -> {critique.refined_decision.action} | Flaws: {critique.flaws_found[:100]}"
                ),
                output_data={
                    "verdict": critique.verdict,
                    "confidence_score": critique.confidence_score,
                    "final_action": critique.refined_decision.action,
                    "verified_rr": critique.refined_decision.net_risk_reward_ratio,
                },
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return critique

    def evaluate_bar(
        self,
        telemetry: dict,
        portfolio: dict,
        asset: dict,
        risk: dict,
        metrics: dict,
        recent_ticker_bars: list[dict],
        ticker_experience: Optional[dict[str, Any]] = None,
        tracer: Optional[PipelineTracer] = None,
    ) -> TradeDecision:
        # Load declarative subagent persona (`agents/subagent.md`) & skills (`skills/*.md`)
        t_load = time.perf_counter()
        sub_def = get_subagent_definition()
        base_system_prompt = sub_def.compile_system_prompt()
        if tracer:
            tracer.add_stage(
                stage="DECLARATIVE_AGENT_SKILL_LOAD",
                status="OK",
                input_data={"agent_md": Path(sub_def.source_path).name, "skills": sub_def.skill_names},
                transformation=(
                    f"Loaded declarative subagent '{sub_def.name}' ({sub_def.role}) "
                    f"with {len(sub_def.loaded_skills)} skill(s) & empirical experience memory"
                ),
                output_data={
                    "compiled_prompt_chars": len(base_system_prompt),
                    "model": sub_def.model,
                    "temperature": sub_def.temperature,
                },
                duration_ms=(time.perf_counter() - t_load) * 1000.0,
            )

        context_blob = json.dumps({
            "active_ticker": telemetry["ticker"],
            "telemetry_15m": telemetry,
            "recent_15m_trajectory": recent_ticker_bars,
            "computed_metrics": metrics,
            "ticker_experience_memory": ticker_experience or {},
            "asset_state": asset,
            "daily_stats": portfolio["daily_stats"],
            "available_cash_sek": portfolio["cash_reserves"]["available_tactical_mini_sek"],
            "immutable_risk_limits": risk,
        }, indent=2)

        # Level 1: Catalyst & Macro Search Grounding Agent
        live_news_context = self.run_l1_catalyst_agent(
            telemetry["ticker"],
            system_instruction=base_system_prompt,
            bar_time=str(telemetry.get("time", "")),
            tracer=tracer,
        )

        # Level 2: Alpha Strategist Proposal Agent
        draft = self.run_l2_alpha_strategist(context_blob, live_news_context, telemetry["ticker"], tracer=tracer)

        # Level 3a: Deterministic Python Math Auditor
        t_math = time.perf_counter()
        math_audit = compute_verified_fee_adjusted_rr(draft, telemetry, asset, risk)
        if tracer:
            tracer.add_stage(
                stage="L3A_PYTHON_MATH_AUDITOR",
                status="MATH_CORRECTED" if math_audit["math_hallucination_detected"] else "MATH_VERIFIED",
                input_data={"l2_claimed_rr": math_audit["llm_claimed_rr"], "spot": math_audit["spot"]},
                transformation=(
                    f"Verified fee-adjusted Net R:R = 1:{math_audit['python_verified_net_rr']} "
                    f"(L2 claimed 1:{math_audit['llm_claimed_rr']}, delta={math_audit['rr_discrepancy']})"
                ),
                output_data=math_audit,
                duration_ms=(time.perf_counter() - t_math) * 1000.0,
            )

        # Level 3b: Adversarial Red-Team Critic Loop
        critique = self.run_l3_adversarial_critic(
            draft, math_audit, context_blob, live_news_context, tracer=tracer
        )

        ref = critique.refined_decision
        decision = TradeDecision(
            ticker=ref.ticker,
            market_regime=ref.market_regime,
            action=ref.action,
            instrument=ref.instrument,
            quantity=ref.quantity,
            limit_price=ref.limit_price,
            avanza_stop_trigger_sek=ref.avanza_stop_trigger_sek,
            avanza_stop_sell_sek=ref.avanza_stop_sell_sek,
            underlying_stop_price=ref.underlying_stop_price,
            take_profit_target=ref.take_profit_target,
            net_risk_reward_ratio=ref.net_risk_reward_ratio,
            overnight_gap_warning=ref.overnight_gap_warning,
            rationale=ref.rationale,
            confidence_score=max(0, min(100, critique.confidence_score)),
            critic_verdict=critique.verdict,
            critic_notes=critique.flaws_found,
        )

        # Level 4: Hard Deterministic Python Circuit Breakers
        return enforce_hard_quant_guardrails(
            decision, telemetry, portfolio, asset, risk, metrics["fx_multiplier"], tracer=tracer
        )
