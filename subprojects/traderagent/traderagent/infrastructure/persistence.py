"""
Atomic File Persistence & Ledger Repository (Infrastructure Adapter).
"""
import json
import os
import tempfile
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo
from traderagent.config import TraderConfig
from traderagent.infrastructure.experience_memory import ExperienceMemoryRepository

CET = ZoneInfo("Europe/Stockholm")


class StateRepository:
    """Crash-safe persistence for portfolio.json, risk_profile.json, experience_memory.json, trade_audit.jsonl, and pipeline_trace.jsonl."""

    def __init__(self, config: TraderConfig):
        self.config = config
        self.experience = ExperienceMemoryRepository(config.experience_memory_path)

    @staticmethod
    def load_json(path: str) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def save_json(path: str, data: dict[str, Any]) -> None:
        """Atomically write JSON via temp file + fsync + os.replace to prevent corruption."""
        dir_name = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def ensure_daily_rollover(self, portfolio: dict[str, Any]) -> dict[str, Any]:
        """Automatically reset daily trade counters and realized PnL at midnight CET."""
        today_cet = datetime.now(CET).strftime("%Y-%m-%d")
        stats = portfolio.setdefault("daily_stats", {})
        if stats.get("date") != today_cet:
            old_date = stats.get("date", "unknown")
            stats["date"] = today_cet
            stats["trades_executed_today"] = 0
            stats["realized_pnl_today_sek"] = 0.0
            stats.setdefault("last_stopout_epoch", 0)
            self.save_json(self.config.portfolio_path, portfolio)
            print(f"[Daily Rollover] Reset daily_stats from {old_date} -> {today_cet}")
        return portfolio

    def load_portfolio(self) -> dict[str, Any]:
        return self.ensure_daily_rollover(self.load_json(self.config.portfolio_path))

    def save_portfolio(self, portfolio: dict[str, Any]) -> None:
        self.save_json(self.config.portfolio_path, portfolio)

    def load_risk(self) -> dict[str, Any]:
        return self.load_json(self.config.risk_path)

    def append_audit_log(self, record: dict[str, Any]) -> None:
        with open(self.config.audit_log_path, "a", encoding="utf-8") as logf:
            logf.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_recent_audit_tail(self, n: int = 3, ticker: Optional[str] = None) -> list[dict[str, Any]]:
        if not os.path.exists(self.config.audit_log_path):
            return []
        try:
            with open(self.config.audit_log_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            records = [json.loads(line) for line in lines]
            if ticker:
                records = [r for r in records if r.get("ticker", "").upper() == ticker.upper()]
            return records[-n:]
        except Exception:
            return []

    def get_recent_traces(self, n: int = 3) -> list[dict[str, Any]]:
        if not os.path.exists(self.config.pipeline_trace_path):
            return []
        try:
            with open(self.config.pipeline_trace_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            return [json.loads(line) for line in lines[-n:]]
        except Exception:
            return []
