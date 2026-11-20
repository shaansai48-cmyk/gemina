"""
Portfolio- & System-Aware General Conversational AI Assistant.
Thread-safe per-session conversation memory, keyword-gated Linux diagnostics (saving 2.1s GPU wake
latency on non-host questions), and clean plain-English answers with zero LaTeX/jargon bloat.
"""
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.llm_router import CognitiveLLMRouter
from gemina.core.tracing import PipelineTracer
from traderagent.agents.prompts import get_main_agent_definition, get_subagent_definition
from traderagent.infrastructure.persistence import StateRepository

CET = ZoneInfo("Europe/Stockholm")
_SYSTEM_QUERY_RE = re.compile(
    r"\b(gpu|cpu|ram|disk|memory|ollama|service|systemd|log|logs|host|system|uptime|crash|error|temp|vram|beach01|health|webhook)\b",
    re.IGNORECASE,
)


def _strip_latex_and_header(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^\s*[🟢🔴]\s*(?:`?\[[^\n]+\]`?|\*\*[^\n]+|<b>[^\n]+)\s*(?:\n+)?", "", s, count=1)
    s = s.replace("$\\rightarrow$", "->").replace("\\rightarrow", "->").replace("\\times", "x")
    s = re.sub(r"\$\$(.*?)\$\$", lambda m: m.group(1).strip(), s, flags=re.DOTALL)
    s = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1) / (\2)", s)
    s = re.sub(r"\\(?:text|mathbf|mathrm|left|right)\{?([^{}]*)\}?", r"\1", s)
    return s.strip()


def _compact_portfolio_snapshot(portfolio: dict[str, Any], risk: dict[str, Any]) -> dict[str, Any]:
    focus_raw = str(portfolio.get("active_focus_ticker", "NVDA"))
    focus_set = {x.strip().upper() for x in focus_raw.split(",") if x.strip()}
    watchlist = portfolio.get("watchlist", {})
    relevant = {
        sym: info
        for sym, info in watchlist.items()
        if sym.upper() in focus_set
        or int(info.get("regular_shares_held", 0)) > 0
        or int(info.get("mini_units_held", 0)) > 0
    }
    return {
        "account_id": portfolio.get("account_id", "ISK 5872428"),
        "active_focus_ticker": focus_raw,
        "daily_stats": portfolio.get("daily_stats", {}),
        "cash_reserves": portfolio.get("cash_reserves", {}),
        "active_or_held_positions": relevant,
        "all_watchlist_tickers": list(watchlist.keys()),
        "key_risk_limits": {
            "max_risk_per_trade_sek": risk.get("max_risk_per_trade_sek", 450.0),
            "max_daily_trades": risk.get("max_daily_trades", 2),
            "max_concurrent_leveraged_minis": risk.get("max_concurrent_leveraged_minis", 1),
            "daily_loss_lockout_sek": risk.get("daily_loss_lockout_sek", -900.0),
            "min_net_rr_ratio": risk.get("min_net_rr_ratio", 1.8),
            "us_open_blackout_cet": f"{risk.get('us_open_blackout_start_cet', '15:30')}-{risk.get('us_open_blackout_end_cet', '15:45')}",
            "nordic_etp_cutoff_cet": risk.get("nordic_etp_cutoff_cet", "20:30"),
        },
    }


class ConversationalQAAgent:
    """Answers general & trading questions using declarative Markdown agent personas + skills."""

    def __init__(self, llm_router: CognitiveLLMRouter, repo: StateRepository):
        self.llm = llm_router
        self.repo = repo
        self._history_lock = threading.Lock()
        self._histories: dict[str, deque[dict[str, str]]] = {"default": deque(maxlen=10)}
        self.chat_history = self._histories["default"]

    def clear_history(self, session_id: str = "default") -> None:
        with self._history_lock:
            self._histories.setdefault(session_id, deque(maxlen=10)).clear()

    def answer(
        self,
        user_query: str,
        tracer: Optional[PipelineTracer] = None,
        use_subagent_persona: bool = False,
        session_id: str = "default",
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
        needs_sys = bool(_SYSTEM_QUERY_RE.search(user_query or ""))
        cmd_outputs = SystemDiagnostics.run_all_diagnostic_commands(max_age_sec=5.0) if needs_sys else {}

        if tracer:
            tracer.add_stage(
                stage="CONTEXT_AND_DIAGNOSTIC_COMMANDS",
                status="OK",
                input_data={"query": user_query, "system_cmds_included": needs_sys},
                transformation=(
                    f"Loaded portfolio snapshot & {len(recent_signals)} signals"
                    + (f" + {len(cmd_outputs)} host diagnostic commands" if needs_sys else " (skipped host cmds for fast stock Q&A)")
                ),
                output_data={"commands_run": list(cmd_outputs.keys()), "focus_ticker": portfolio["active_focus_ticker"]},
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        with self._history_lock:
            hist = list(self._histories.setdefault(session_id, deque(maxlen=10)))
        history_str = "\n".join(f"User: {turn['user']}\nAssistant: {turn['assistant']}" for turn in hist)

        portfolio_compact = _compact_portfolio_snapshot(portfolio, risk)
        prompt_parts = [f"CURRENT TIME (CET): {datetime.now(CET).strftime('%Y-%m-%d %H:%M:%S %Z')}"]
        if cmd_outputs:
            prompt_parts.append(f"LIVE SYSTEM COMMAND OUTPUTS:\n{json.dumps(cmd_outputs, indent=2)}")
        prompt_parts.append(f"PORTFOLIO & RISK SUMMARY:\n{json.dumps(portfolio_compact, indent=2)}")
        prompt_parts.append(f"RECENT 15M SIGNALS:\n{json.dumps(recent_signals, indent=2)}")
        if history_str:
            prompt_parts.append(f"RECENT CONVERSATION HISTORY:\n{history_str}")
        prompt_parts.append(
            "RESPONSE STYLE: Give a direct plain-English answer first, then 2-4 crisp bullet points and 1 clear Next Step. "
            "No raw LaTeX equations ($...$, $$...$$, \\frac) and never invent `/fill_mini` syntax."
        )
        prompt_parts.append(f"USER QUESTION:\n{user_query}")
        prompt = "\n\n".join(prompt_parts)

        t_llm = time.perf_counter()
        raw_answer, model_used, llm_status = self.llm.generate_grounded_text(
            prompt=prompt,
            system_instruction=compiled_system_prompt,
            use_google_search=True,
            temperature=agent_def.temperature,
            allow_ollama_fallback=True,
        )
        answer = _strip_latex_and_header(raw_answer)
        if llm_status.startswith("FALLBACK"):
            answer += f"\n\n<i>(Answered via local {model_used} fallback)</i>"

        if tracer:
            tracer.add_stage(
                stage="LLM_REASONING_TRANSFORM",
                status=llm_status,
                input_data={"prompt_chars": len(prompt), "model": model_used, "persona": agent_def.name},
                transformation=f"Generated answer via {model_used} using persona '{agent_def.name}' + Google Search grounding",
                output_data={"answer_chars": len(answer), "preview": answer[:120]},
                duration_ms=(time.perf_counter() - t_llm) * 1000.0,
            )

        with self._history_lock:
            self._histories.setdefault(session_id, deque(maxlen=10)).append({"user": user_query, "assistant": answer[:1200]})
        return answer
