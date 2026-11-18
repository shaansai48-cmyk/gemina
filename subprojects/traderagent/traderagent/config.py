"""
Configuration & Environment Loader for TraderAgent.
Resolves data file paths and credentials cleanly whether run standalone or imported by Gemina.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

CET = ZoneInfo("Europe/Stockholm")
SUBPROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class TraderConfig:
    apps_script_url: str
    gemini_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    gemini_model: str
    ollama_model: str
    portfolio_path: str
    risk_path: str
    audit_log_path: str
    pipeline_trace_path: str
    env_file_path: str

    @classmethod
    def from_env(cls, env_path: str | Path | None = None, data_dir: str | Path | None = None) -> "TraderConfig":
        resolved_env = Path(env_path) if env_path else (SUBPROJECT_ROOT / ".env")
        if resolved_env.exists():
            load_dotenv(dotenv_path=resolved_env, override=False)
        else:
            load_dotenv(override=False)

        resolved_data = Path(data_dir) if data_dir else (SUBPROJECT_ROOT / "data")
        resolved_data.mkdir(parents=True, exist_ok=True)

        raw_model = os.environ.get("GEMINI_MODEL", "gemini-4-argon")
        gemini_model = "gemini-4-argon" if raw_model == "gemini-2.5-pro" else raw_model

        return cls(
            apps_script_url=os.environ.get("APPS_SCRIPT_URL", ""),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            gemini_model=gemini_model,
            ollama_model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b"),
            portfolio_path=str(resolved_data / "portfolio.json"),
            risk_path=str(resolved_data / "risk_profile.json"),
            audit_log_path=str(resolved_data / "trade_audit.jsonl"),
            pipeline_trace_path=str(resolved_data / "pipeline_trace.jsonl"),
            env_file_path=str(resolved_env),
        )
