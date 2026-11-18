#!/usr/bin/env python3
"""
Declarative Telegram Bot & Gemini API Event Handler (`src/bot.py`).
Loads environment secrets via `python-dotenv`, parses markdown agent personas
(`agents/main_agent.md` and `agents/subagent.md`) and modular skills (`skills/*.md`)
via `src/loader.py`, and routes incoming Telegram messages and 15m TradingView bars
through the declarative personas before executing Gemini API calls.
"""
import argparse
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

from gemina.core.tracing import PipelineTracer
from src.loader import DeclarativeLoader
from traderagent.application.daemon import TraderAgentDaemon
from traderagent.config import SUBPROJECT_ROOT, TraderConfig

# 1. Securely load environment variables via python-dotenv
ENV_PATH = SUBPROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH, override=False)
else:
    load_dotenv(override=False)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")


class DeclarativeTelegramBot:
    """
    Routes incoming Telegram messages and webhook events through declarative Markdown
    agent configurations (`agents/main_agent.md` and `agents/subagent.md`) and skills (`skills/*.md`).
    """

    def __init__(
        self,
        config: Optional[TraderConfig] = None,
        loader: Optional[DeclarativeLoader] = None,
    ):
        self.loader = loader or DeclarativeLoader()
        self.main_agent_def = self.loader.load_agent("main_agent.md")
        self.subagent_def = self.loader.load_agent("subagent.md")

        self.config = config or TraderConfig.from_env()
        # Synchronize model selection from declarative markdown frontmatter if not overridden
        if self.main_agent_def.model:
            self.config.gemini_model = self.main_agent_def.model
        if self.main_agent_def.fallback_model:
            self.config.ollama_model = self.main_agent_def.fallback_model

        self.daemon = TraderAgentDaemon(config=self.config)

    def reload_personas(self) -> tuple[str, str]:
        """Hot-reloads `agents/main_agent.md`, `agents/subagent.md`, and `skills/*.md` from disk."""
        self.main_agent_def = self.loader.load_agent("main_agent.md")
        self.subagent_def = self.loader.load_agent("subagent.md")
        return self.main_agent_def.compile_system_prompt(), self.subagent_def.compile_system_prompt()

    def handle_incoming_message(self, text: str, tracer: Optional[PipelineTracer] = None) -> str:
        """
        Routes a user message through `agents/main_agent.md` (for general/system Q&A)
        or `agents/subagent.md` (for `/analyze` and portfolio commands) with skills injected.
        """
        self.reload_personas()
        is_subagent_task = text.strip().lower().startswith(("/analyze", "/focus", "/fill_", "/close_", "/set_", "/watchlist"))
        active_persona = self.subagent_def if is_subagent_task else self.main_agent_def

        if tracer:
            tracer.add_stage(
                stage="DECLARATIVE_AGENT_SKILL_LOAD",
                status="OK",
                input_data={"agent_md": Path(active_persona.source_path).name, "skills": active_persona.skill_names},
                transformation=(
                    f"Loaded persona '{active_persona.name}' ({active_persona.role}) "
                    f"with {len(active_persona.loaded_skills)} skill(s) -> model={active_persona.model}"
                ),
                output_data={
                    "compiled_prompt_chars": len(active_persona.compile_system_prompt()),
                    "model": active_persona.model,
                    "temperature": active_persona.temperature,
                },
            )

        body, st = self.daemon.command_router.execute_text_command(text, tracer=tracer)
        return self.daemon.status_evaluator.format_with_status_ticker(body, st)

    def run(self, focus_override: Optional[str] = None) -> None:
        print(
            f"📑 Declarative Markdown Agents Loaded:\n"
            f"  • Main Agent : {self.main_agent_def.name} ({self.main_agent_def.source_path}) "
            f"| Model: {self.main_agent_def.model} | Skills: {self.main_agent_def.skill_names}\n"
            f"  • Sub-Agent  : {self.subagent_def.name} ({self.subagent_def.source_path}) "
            f"| Model: {self.subagent_def.model} | Skills: {self.subagent_def.skill_names}"
        )
        self.daemon.run_forever(focus_override=focus_override)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemina Declarative Markdown Telegram Bot & Quant Daemon")
    parser.add_argument("--focus", type=str, help="Override active focus ticker (e.g. NVDA, 'ERIC B', MSFT)")
    args = parser.parse_args()
    bot = DeclarativeTelegramBot()
    bot.run(focus_override=args.focus)


if __name__ == "__main__":
    main()
