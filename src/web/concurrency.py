"""
Thread-safe Concurrency & Runtime Bridge (`src/web/concurrency.py`).
Isolates persona locking, bounded background analysis execution (HTTP 429 on saturation),
Yahoo Finance market data client, and atomic LedgerService mutations.
"""
from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import Any, Callable, Optional, TypeVar

from gemina.core.llm_router import CognitiveLLMRouter
from traderagent.agents.committee import QuantAgentCommittee
from traderagent.agents.qa_assistant import ConversationalQAAgent
from traderagent.application.ledger_service import LedgerService
from traderagent.config import TraderConfig
from traderagent.infrastructure.apps_script_client import AppsScriptClient
from traderagent.infrastructure.market_data import YahooFinanceClient
from traderagent.infrastructure.persistence import StateRepository
from traderagent.infrastructure.system_status import StatusEvaluator
from traderagent.infrastructure.telegram_adapter import TelegramGateway

T = TypeVar("T")


class BusyError(RuntimeError):
    """Raised when the bounded analysis executor queue is saturated (maps to HTTP 429)."""


class WebRuntimeBridge:
    """
    Shared runtime container coordinating locks, bounded thread-pool execution,
    and TraderAgent domain/infrastructure services for the Web Command Center.
    """

    def __init__(
        self,
        config: Optional[TraderConfig] = None,
        repo: Optional[StateRepository] = None,
        market_data: Optional[YahooFinanceClient] = None,
        ledger: Optional[LedgerService] = None,
        llm_router: Optional[CognitiveLLMRouter] = None,
        committee: Optional[QuantAgentCommittee] = None,
        apps_script: Optional[AppsScriptClient] = None,
        status_evaluator: Optional[StatusEvaluator] = None,
        telegram: Optional[TelegramGateway] = None,
        max_workers: int = 2,
        max_inflight: int = 4,
    ):
        self.config = config or TraderConfig.from_env()
        self.repo = repo or StateRepository(self.config)
        self.market_data = market_data or YahooFinanceClient()
        self.ledger = ledger or LedgerService(self.repo)
        self.apps_script = apps_script or AppsScriptClient(self.config.apps_script_url)
        self.status_evaluator = status_evaluator or StatusEvaluator(self.repo, self.apps_script)
        self.telegram = telegram or TelegramGateway(self.config, self.status_evaluator)
        self.llm_router = llm_router or CognitiveLLMRouter(
            api_key=self.config.gemini_api_key,
            gemini_model=self.config.gemini_model,
            ollama_model=self.config.ollama_model,
        )
        self.committee = committee or QuantAgentCommittee(self.llm_router)

        self.persona_lock = threading.RLock()
        capped_workers = max(1, min(2, int(max_workers)))
        capped_inflight = max(capped_workers, min(4, int(max_inflight)))
        self.max_workers = capped_workers
        self.max_inflight = capped_inflight
        self._analysis_semaphore = threading.BoundedSemaphore(capped_inflight)
        self.analysis_executor = ThreadPoolExecutor(
            max_workers=capped_workers,
            thread_name_prefix="gemina-web-analysis",
        )
        self._chat_sessions: dict[str, ConversationalQAAgent] = {}
        self._shutdown = False

    def get_qa_agent(self, session_id: str = "web") -> ConversationalQAAgent:
        """Returns or creates an isolated ConversationalQAAgent for the given web session ID."""
        sid = str(session_id or "web").strip() or "web"
        with self.persona_lock:
            if sid not in self._chat_sessions:
                self._chat_sessions[sid] = ConversationalQAAgent(self.llm_router, self.repo)
            return self._chat_sessions[sid]

    def clear_chat_session(self, session_id: str = "web") -> None:
        """Clears conversation history for a web chat session under `persona_lock`."""
        sid = str(session_id or "web").strip() or "web"
        with self.persona_lock:
            agent = self._chat_sessions.get(sid)
            if agent is not None:
                agent.clear_history()

    def submit_analysis(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        """
        Submits a heavy analysis task to `analysis_executor` bounded by `max_workers + queue <= 4`.
        Raises `BusyError` (HTTP 429) immediately when capacity is full.
        """
        if self._shutdown:
            raise BusyError("Web runtime analysis executor is shutting down.")

        if not self._analysis_semaphore.acquire(blocking=False):
            raise BusyError(
                f"Analysis queue is busy (limit {self.max_inflight} concurrent/queued tasks). Retry shortly."
            )

        def _run_guarded() -> T:
            try:
                return fn(*args, **kwargs)
            finally:
                self._analysis_semaphore.release()

        try:
            return self.analysis_executor.submit(_run_guarded)
        except Exception as exc:
            self._analysis_semaphore.release()
            raise BusyError(f"Unable to enqueue analysis job: {exc}") from exc

    def shutdown(self, wait: bool = True) -> None:
        """Gracefully shuts down the bounded analysis thread pool."""
        self._shutdown = True
        self.analysis_executor.shutdown(wait=wait)
