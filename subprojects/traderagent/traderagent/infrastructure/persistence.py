"""
Atomic, Thread-Safe & Cross-Process Locked Persistence Repository (Infrastructure Adapter).
"""
from collections import deque
from contextlib import contextmanager
import copy
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator, Optional, TypeVar
from zoneinfo import ZoneInfo

from traderagent.config import TraderConfig
from traderagent.infrastructure.experience_memory import ExperienceMemoryRepository

CET = ZoneInfo("Europe/Stockholm")
T = TypeVar("T")
SECRET_KEYS = {"secret", "api_key", "token", "password", "authorization"}


def redact_secrets(obj: Any) -> Any:
    """Recursively strips secret/credential keys from telemetry or trace payloads."""
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in SECRET_KEYS else redact_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    return obj


def _safe_parse_jsonl(lines: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                records.append(obj)
        except Exception:
            continue
    return records


class StateRepository:
    """
    Crash-safe persistence for portfolio.json, risk_profile.json, runtime_state.json,
    experience_memory.json, trade_audit.jsonl, and pipeline_trace.jsonl.
    """

    def __init__(self, config: TraderConfig):
        self.config = config
        self.experience = ExperienceMemoryRepository(config.experience_memory_path)
        self.runtime_state_path = (
            getattr(config, "runtime_state_path", "")
            or str(Path(config.portfolio_path).parent / "runtime_state.json")
        )
        self._lock_path = str(Path(config.portfolio_path).parent / ".state.lock")
        self._lock_tls = threading.local()
        self._portfolio_lock = threading.RLock()
        self._risk_lock = threading.RLock()
        self._runtime_lock = threading.RLock()
        self._audit_lock = threading.RLock()
        self._trace_lock = threading.RLock()

    @contextmanager
    def _advisory_flock(self) -> Iterator[None]:
        depth = getattr(self._lock_tls, "flock_depth", 0)
        if depth > 0:
            self._lock_tls.flock_depth = depth + 1
            try:
                yield
            finally:
                self._lock_tls.flock_depth = depth
            return
        os.makedirs(os.path.dirname(os.path.abspath(self._lock_path)) or ".", exist_ok=True)
        with open(self._lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._lock_tls.flock_depth = 1
            try:
                yield
            finally:
                self._lock_tls.flock_depth = 0
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def load_json(path: str) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def save_json(path: str, data: dict[str, Any]) -> None:
        """Atomically write JSON via temp file + fsync + os.replace to prevent corruption."""
        dir_name = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def ensure_daily_rollover(self, portfolio: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Resets daily trade counters and realized PnL at midnight CET. Returns (portfolio, changed)."""
        today_cet = datetime.now(CET).strftime("%Y-%m-%d")
        stats = portfolio.setdefault("daily_stats", {})
        if stats.get("date") != today_cet:
            old_date = stats.get("date", "unknown")
            stats["date"] = today_cet
            stats["trades_executed_today"] = 0
            stats["realized_pnl_today_sek"] = 0.0
            stats.setdefault("last_stopout_epoch", 0)
            print(f"[Daily Rollover] Reset daily_stats from {old_date} -> {today_cet}")
            return portfolio, True
        return portfolio, False

    def load_portfolio(self) -> dict[str, Any]:
        with self._portfolio_lock, self._advisory_flock():
            portfolio = self.load_json(self.config.portfolio_path)
            portfolio, rolled = self.ensure_daily_rollover(portfolio)
            if rolled:
                self.save_json(self.config.portfolio_path, portfolio)
            return copy.deepcopy(portfolio)

    def save_portfolio(self, portfolio: dict[str, Any]) -> None:
        with self._portfolio_lock, self._advisory_flock():
            self.save_json(self.config.portfolio_path, portfolio)

    def update_portfolio(self, mutator: Callable[[dict[str, Any]], T]) -> tuple[dict[str, Any], T]:
        """Atomically performs load -> rollover -> mutate -> save across threads & processes."""
        with self._portfolio_lock, self._advisory_flock():
            portfolio = self.load_json(self.config.portfolio_path)
            portfolio, _ = self.ensure_daily_rollover(portfolio)
            result = mutator(portfolio)
            self.save_json(self.config.portfolio_path, portfolio)
            return copy.deepcopy(portfolio), result

    def load_risk(self) -> dict[str, Any]:
        with self._risk_lock:
            return copy.deepcopy(self.load_json(self.config.risk_path))

    def _load_runtime_state_unlocked(self) -> dict[str, Any]:
        if not os.path.exists(self.runtime_state_path):
            return {"last_tg_update_id": 0, "alert_dedup": {}}
        try:
            data = self.load_json(self.runtime_state_path)
            if isinstance(data, dict):
                data.setdefault("last_tg_update_id", 0)
                data.setdefault("alert_dedup", {})
                return copy.deepcopy(data)
        except Exception:
            pass
        return {"last_tg_update_id": 0, "alert_dedup": {}}

    def load_runtime_state(self) -> dict[str, Any]:
        with self._runtime_lock, self._advisory_flock():
            return self._load_runtime_state_unlocked()

    def save_runtime_state(self, state: dict[str, Any]) -> None:
        with self._runtime_lock, self._advisory_flock():
            self.save_json(self.runtime_state_path, state)

    def update_runtime_state(self, mutator: Callable[[dict[str, Any]], T]) -> tuple[dict[str, Any], T]:
        with self._runtime_lock, self._advisory_flock():
            state = self._load_runtime_state_unlocked()
            result = mutator(state)
            self.save_json(self.runtime_state_path, state)
            return copy.deepcopy(state), result

    def append_audit_log(self, record: dict[str, Any]) -> None:
        sanitized = copy.deepcopy(record)
        if isinstance(sanitized.get("telemetry"), dict):
            sanitized["telemetry"] = {
                k: v for k, v in sanitized["telemetry"].items() if str(k).lower() not in SECRET_KEYS
            }
        with self._audit_lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.config.audit_log_path)) or ".", exist_ok=True)
            with open(self.config.audit_log_path, "a", encoding="utf-8") as logf:
                logf.write(json.dumps(sanitized, ensure_ascii=False) + "\n")
                logf.flush()

    def get_recent_audit_tail(self, n: int = 3, ticker: Optional[str] = None, max_scan_lines: int = 300) -> list[dict[str, Any]]:
        if not os.path.exists(self.config.audit_log_path):
            return []
        with self._audit_lock:
            try:
                with open(self.config.audit_log_path, "r", encoding="utf-8") as f:
                    lines = list(deque(f, maxlen=max(n * 10, max_scan_lines)))
            except Exception:
                return []
        records = _safe_parse_jsonl(lines)
        if ticker:
            target = ticker.strip().upper()
            records = [r for r in records if str(r.get("ticker", "")).strip().upper() == target]
        return redact_secrets(records[-n:])

    def get_latest_audit_by_ticker(self, max_scan_lines: int = 300) -> dict[str, dict[str, Any]]:
        """Returns the most recent audit record keyed by normalized uppercase ticker."""
        if not os.path.exists(self.config.audit_log_path):
            return {}
        with self._audit_lock:
            try:
                with open(self.config.audit_log_path, "r", encoding="utf-8") as f:
                    lines = list(deque(f, maxlen=max_scan_lines))
            except Exception:
                return {}
        by_ticker: dict[str, dict[str, Any]] = {}
        for rec in _safe_parse_jsonl(lines):
            sym = str(rec.get("ticker", "")).strip().upper()
            if sym:
                by_ticker[sym] = redact_secrets(rec)
        return by_ticker

    def get_recent_traces(self, n: int = 3, max_scan_lines: int = 300) -> list[dict[str, Any]]:
        if not os.path.exists(self.config.pipeline_trace_path):
            return []
        with self._trace_lock:
            try:
                with open(self.config.pipeline_trace_path, "r", encoding="utf-8") as f:
                    lines = list(deque(f, maxlen=max(n, max_scan_lines)))
            except Exception:
                return []
        records = _safe_parse_jsonl(lines)
        return redact_secrets(records[-n:])
