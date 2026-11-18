"""
Portfolio- & System-Aware General Conversational AI Assistant.
Dynamically loads `agents/main_agent.md` (or `agents/subagent.md`) and `skills/*.md` via `src/loader.py`.
"""
import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.llm_router import CognitiveLLMRouter
from gemina.core.tracing import PipelineTracer
from traderagent.agents.prompts import get_main_agent_definition, get_subagent_definition
from traderagent.infrastructure.persistence import StateRepository

CET = ZoneInfo("Europe/Stockholm")


class ConversationalQAAgent:
    """Answers general & trading questions using declarative Markdown agent personas + skills."""

    def __init__(self, llm_router: CognitiveLLMRouter, repo: StateRepository):
        self.llm = llm_router
        self.repo = repo
        self.chat_history: deque[dict[str, str]] = deque(maxlen=10)

    def clear_history(self) -> None:
        self.chat_history.clear()

    def answer(
        self,
        user_query: str,
        tracer: Optional[PipelineTracer] = None,
        use_subagent_persona: bool = False,
    ) -> str:
        t_load = time.perf_counter()
        agent_def = get_subagent_definition() if use_subagent_persona else get_main_agent_definition()
        compiled_system_prompt = agent_def.compile_system_prompt()

        if tracer:
            tracer.add_stage(
                stage="DECLARATIVE_AGENT_SKILL_LOAD",
                status="OK",
                input_data={
                    "agent_md": Path(agent_def.source_path).name,
                    "skills": agent_def.skill_names,
                },
                transformation=(
                    f"Loaded declarative persona '{agent_def.name}' ({agent_def.role}) "
                    f"and injected {len(agent_def.loaded_skills)} skill(s) into system prompt"
                ),
                output_data={
                    "compiled_prompt_chars": len(compiled_system_prompt),
                    "model": agent_def.model,
                    "temperature": agent_def.temperature,
                },
                duration_ms=(time.perf_counter() - t_load) * 1000.0,
            )

        t0 = time.perf_counter()
        portfolio = self.repo.load_portfolio()
        risk = self.repo.load_risk()
        recent_signals = self.repo.get_recent_audit_tail(3)
        cmd_outputs = SystemDiagnostics.run_all_diagnostic_commands()

        if tracer:
            tracer.add_stage(
                stage="CONTEXT_AND_DIAGNOSTIC_COMMANDS",
                status="OK",
                input_data={"query": user_query},
                transformation=f"Executed {len(cmd_outputs)} Linux diagnostic commands + loaded portfolio & last {len(recent_signals)} audit signals",
                output_data={"commands_run": list(cmd_outputs.keys()), "focus_ticker": portfolio["active_focus_ticker"]},
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        history_str = "\n".join(
            f"User: {turn['user']}\nAssistant: {turn['assistant']}" for turn in self.chat_history
        )
        prompt = (
            f"CURRENT TIME (CET): {datetime.now(CET).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"LIVE SYSTEM COMMAND OUTPUTS (executed on Ubuntu laptop):\n{json.dumps(cmd_outputs, indent=2)}\n\n"
            f"LIVE PORTFOLIO STATE:\n{json.dumps(portfolio, indent=2)}\n\n"
            f"RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
            f"RECENT 15M SIGNALS:\n{json.dumps(recent_signals, indent=2)}\n\n"
        )
        if history_str:
            prompt += f"RECENT CONVERSATION HISTORY:\n{history_str}\n\n"
        prompt += f"USER QUESTION:\n{user_query}"

        t_llm = time.perf_counter()
        answer, model_used, llm_status = self.llm.generate_grounded_text(
            prompt=prompt,
            system_instruction=compiled_system_prompt,
            use_google_search=True,
            temperature=agent_def.temperature,
            allow_ollama_fallback=True,
        )
        if llm_status.startswith("FALLBACK"):
            answer += f"\n\n_(Answered via local {model_used} fallback)_"

        if tracer:
            tracer.add_stage(
                stage="LLM_REASONING_TRANSFORM",
                status=llm_status,
                input_data={"prompt_chars": len(prompt), "model": model_used, "persona": agent_def.name},
                transformation=f"Generated answer via {model_used} using persona '{agent_def.name}' + Google Search grounding",
                output_data={"answer_chars": len(answer), "preview": answer[:120]},
                duration_ms=(time.perf_counter() - t_llm) * 1000.0,
            )

        self.chat_history.append({"user": user_query, "assistant": answer[:1500]})
        return answer
