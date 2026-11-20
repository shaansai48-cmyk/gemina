"""
Server-Sent Events (SSE) Broker & Background JSONL/Health Watcher (`src/web/sse.py`).
Supports Last-Event-ID replay over a bounded ring buffer, non-blocking per-subscriber queues,
15s keepalive heartbeats, PipelineTracer integration, and standalone file tailing.
"""
from collections import deque
import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Iterator, Optional

from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.tracing import PipelineTracer
from traderagent.config import TraderConfig
from traderagent.infrastructure.persistence import redact_secrets


class SSESubscription:
    """Iterator and context manager yielding formatted SSE frames for one client."""

    def __init__(
        self,
        broker: "SSEBroker",
        sub_queue: "queue.Queue[Optional[str]]",
        replay_frames: list[str],
        keepalive_sec: float = 15.0,
    ):
        self._broker = broker
        self._queue = sub_queue
        self._replay = deque(replay_frames)
        self._keepalive_sec = max(0.05, float(keepalive_sec))
        self._closed = False
        self._lock = threading.Lock()

    def __iter__(self) -> Iterator[str]:
        return self

    def __enter__(self) -> "SSESubscription":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        """Unregisters this subscription from the broker and wakes any waiting reader."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._broker._unregister_queue(self._queue)
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    def __next__(self) -> str:
        if self._replay:
            return self._replay.popleft()

        while True:
            if self._closed:
                raise StopIteration
            try:
                frame = self._queue.get(timeout=self._keepalive_sec)
            except queue.Empty:
                if self._closed:
                    raise StopIteration
                return ": keepalive\n\n"

            if frame is None:
                self.close()
                raise StopIteration
            return frame


class SSEBroker:
    """
    Thread-safe SSE event hub with a 250-event replay buffer and PipelineTracer subscription.
    """

    def __init__(
        self,
        buffer_size: int = 250,
        queue_maxsize: int = 128,
        keepalive_sec: float = 15.0,
        auto_subscribe_tracer: bool = True,
    ):
        self._buffer: deque[tuple[int, str, str]] = deque(maxlen=max(1, int(buffer_size)))
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._keepalive_sec = max(0.05, float(keepalive_sec))
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Optional[str]]] = []
        self._next_id: int = 0
        self._closed: bool = False
        self._tracer_hooked: bool = False
        self._recent_inproc_trace_ids: deque[str] = deque(maxlen=250)

        if auto_subscribe_tracer:
            self.start()

    @property
    def latest_event_id(self) -> int:
        with self._lock:
            return self._next_id

    def start(self) -> None:
        """Activates the broker and hooks PipelineTracer stage/completion events."""
        with self._lock:
            self._closed = False
            should_hook = not self._tracer_hooked
            self._tracer_hooked = True
        if should_hook:
            PipelineTracer.subscribe(self._on_pipeline_event)

    def stop(self) -> None:
        """Unhooks PipelineTracer without closing existing client connections."""
        with self._lock:
            should_unhook = self._tracer_hooked
            self._tracer_hooked = False
        if should_unhook:
            PipelineTracer.unsubscribe(self._on_pipeline_event)

    def _on_pipeline_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type not in ("trace.stage", "trace.completed"):
            return
        sanitized = redact_secrets(payload)
        if event_type == "trace.completed" and isinstance(sanitized, dict):
            tid = str(sanitized.get("trace_id", "")).strip()
            if tid:
                with self._lock:
                    self._recent_inproc_trace_ids.append(tid)
        self.publish(event_type, sanitized if isinstance(sanitized, dict) else {"data": sanitized})

    def was_trace_published_inproc(self, trace_id: str) -> bool:
        """Checks whether a completed trace ID was already emitted via in-process PipelineTracer."""
        tid = str(trace_id or "").strip()
        if not tid:
            return False
        with self._lock:
            return tid in self._recent_inproc_trace_ids

    @staticmethod
    def format_sse_frame(event_id: int, event_name: str, data_dict: dict[str, Any]) -> str:
        safe_event = str(event_name or "message").replace("\r", "").replace("\n", "")
        payload_json = json.dumps(data_dict, ensure_ascii=False, default=str)
        return f"id: {event_id}\nevent: {safe_event}\ndata: {payload_json}\n\n"

    def publish(self, event_name: str, data_dict: dict[str, Any]) -> int:
        """Redacts secrets, buffers the event for replay, and pushes to all live subscribers."""
        sanitized = redact_secrets(data_dict if isinstance(data_dict, dict) else {"value": data_dict})
        with self._lock:
            if self._closed:
                return self._next_id
            self._next_id += 1
            event_id = self._next_id
            frame = self.format_sse_frame(event_id, event_name, sanitized)
            self._buffer.append((event_id, event_name, frame))
            queues = list(self._subscribers)

        for q in queues:
            try:
                q.put_nowait(frame)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass
        return event_id

    def subscribe(self, last_event_id: int | str | None = 0) -> SSESubscription:
        """Registers a subscriber queue and replays buffered events newer than `last_event_id`."""
        try:
            since_id = max(0, int(last_event_id or 0))
        except (TypeError, ValueError):
            since_id = 0

        sub_queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=self._queue_maxsize)
        with self._lock:
            replay = [frame for (ev_id, _, frame) in self._buffer if ev_id > since_id]
            is_closed = self._closed
            if not is_closed:
                self._subscribers.append(sub_queue)

        if is_closed:
            try:
                sub_queue.put_nowait(None)
            except queue.Full:
                pass

        return SSESubscription(
            broker=self,
            sub_queue=sub_queue,
            replay_frames=replay,
            keepalive_sec=self._keepalive_sec,
        )

    def _unregister_queue(self, sub_queue: "queue.Queue[Optional[str]]") -> None:
        with self._lock:
            self._subscribers = [q for q in self._subscribers if q is not sub_queue]

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def close_all(self) -> None:
        """Unsubscribes from PipelineTracer and cleanly terminates all active SSE streams."""
        self.stop()
        with self._lock:
            self._closed = True
            queues = list(self._subscribers)
            self._subscribers.clear()

        for q in queues:
            try:
                q.put_nowait(None)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass


class _JsonlFileCursor:
    """Tracks incremental file position across appends, truncations, and rotations."""

    def __init__(self, path: str, start_at_end: bool = True):
        self.path = Path(path)
        self.inode: Optional[int] = None
        self.offset: int = 0
        self._partial: str = ""
        if start_at_end and self.path.is_file():
            try:
                st = self.path.stat()
                self.inode = st.st_ino
                self.offset = st.st_size
            except OSError:
                pass

    def read_new_records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            self.inode = None
            self.offset = 0
            self._partial = ""
            return []

        try:
            st = self.path.stat()
        except OSError:
            return []

        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.offset = 0
            self._partial = ""

        self.inode = st.st_ino
        if st.st_size == self.offset:
            return []

        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return []

        if not chunk:
            return []

        combined = self._partial + chunk
        lines = combined.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._partial = lines.pop()
        else:
            self._partial = ""

        records: list[dict[str, Any]] = []
        for raw_line in lines:
            s = raw_line.strip()
            if not s:
                continue
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    records.append(parsed)
            except Exception:
                continue
        return records


class JsonlTailWatcher:
    """
    Daemon background watcher that tails trade_audit.jsonl / pipeline_trace.jsonl,
    detects portfolio.json modifications, and publishes 5s health.tick SSE heartbeats.
    """

    def __init__(
        self,
        broker: SSEBroker,
        config: TraderConfig,
        poll_interval_sec: float = 0.5,
        health_interval_sec: float = 5.0,
        status_evaluator: Optional[Any] = None,
    ):
        self.broker = broker
        self.config = config
        self.poll_interval_sec = max(0.1, float(poll_interval_sec))
        self.health_interval_sec = max(0.5, float(health_interval_sec))
        self.status_evaluator = status_evaluator

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._audit_cursor = _JsonlFileCursor(config.audit_log_path, start_at_end=True)
        self._trace_cursor = _JsonlFileCursor(config.pipeline_trace_path, start_at_end=True)
        self._portfolio_sig = self._stat_signature(config.portfolio_path)
        self._last_health_ts: float = 0.0

    @staticmethod
    def _stat_signature(path: str) -> Optional[tuple[int, int, int]]:
        try:
            st = os.stat(path)
            return (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="gemina-jsonl-tail-watcher",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def poll_once(self) -> None:
        """Runs one non-blocking poll cycle across watched files and health ticks."""
        for rec in self._trace_cursor.read_new_records():
            tid = str(rec.get("trace_id", "")).strip()
            if tid and self.broker.was_trace_published_inproc(tid):
                continue
            self.broker.publish("trace.completed", redact_secrets(rec))

        for rec in self._audit_cursor.read_new_records():
            self.broker.publish("audit.new", redact_secrets(rec))

        curr_sig = self._stat_signature(self.config.portfolio_path)
        if curr_sig is not None and curr_sig != self._portfolio_sig:
            self._portfolio_sig = curr_sig
            try:
                with open(self.config.portfolio_path, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                if isinstance(pdata, dict):
                    self.broker.publish(
                        "portfolio.updated",
                        {
                            "source": "disk_watcher",
                            "active_focus_ticker": pdata.get("active_focus_ticker", ""),
                            "daily_stats": pdata.get("daily_stats", {}),
                            "cash_reserves": pdata.get("cash_reserves", {}),
                            "watchlist": pdata.get("watchlist", {}),
                        },
                    )
            except Exception:
                pass

        now = time.monotonic()
        if (now - self._last_health_ts) >= self.health_interval_sec:
            self._last_health_ts = now
            try:
                host = SystemDiagnostics.collect_host_metrics(
                    max_age_sec=15.0,
                    allow_stale=True,
                    force_refresh=False,
                )
                payload: dict[str, Any] = {
                    "ts": time.time(),
                    "diagnostics": redact_secrets(host),
                }
                if self.status_evaluator is not None:
                    try:
                        st = self.status_evaluator.evaluate()
                        payload["is_red"] = bool(st.get("is_red"))
                        payload["color"] = st.get("color", "")
                        payload["ticker_line"] = st.get("ticker_line", "")
                        payload["red_reasons"] = list(st.get("red_reasons", []))
                    except Exception:
                        pass
                self.broker.publish("health.tick", payload)
            except Exception:
                pass

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval_sec)
