"""
End-to-End Message Lifecycle Tracer (Arrival -> Processing/Transformations -> Leaving).
Shared across all Gemina sub-agents with thread-safe JSONL persistence and live event hooks.
"""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Optional
import uuid
from zoneinfo import ZoneInfo

CET = ZoneInfo("Europe/Stockholm")


class PipelineTracer:
    """
    Tracks a message from ARRIVAL -> each PROCESSING / TRANSFORMATION stage -> LEAVING.
    Thread-safe across daemon + Web UI threads, supports optional trace file disablement for tests,
    and broadcasts stage/completion events to registered subscribers (SSE UI).
    """

    _file_locks_guard = threading.Lock()
    _file_locks: dict[str, threading.Lock] = {}

    _subscribers_lock = threading.Lock()
    _subscribers: list[Callable[[str, dict[str, Any]], None]] = []

    _active_lock = threading.Lock()
    _active_traces: dict[str, dict[str, Any]] = {}

    @classmethod
    def _lock_for_path(cls, path: str) -> threading.Lock:
        abs_path = os.path.abspath(path)
        with cls._file_locks_guard:
            if abs_path not in cls._file_locks:
                cls._file_locks[abs_path] = threading.Lock()
            return cls._file_locks[abs_path]

    @classmethod
    def subscribe(cls, callback: Callable[[str, dict[str, Any]], None]) -> None:
        with cls._subscribers_lock:
            if callback not in cls._subscribers:
                cls._subscribers.append(callback)

    @classmethod
    def unsubscribe(cls, callback: Callable[[str, dict[str, Any]], None]) -> None:
        with cls._subscribers_lock:
            cls._subscribers = [cb for cb in cls._subscribers if cb != callback]

    @classmethod
    def get_active_traces(cls) -> list[dict[str, Any]]:
        with cls._active_lock:
            return copy.deepcopy(list(cls._active_traces.values()))

    @classmethod
    def _emit_event(cls, event_type: str, payload: dict[str, Any]) -> None:
        with cls._subscribers_lock:
            callbacks = list(cls._subscribers)
        for cb in callbacks:
            try:
                cb(event_type, copy.deepcopy(payload))
            except Exception:
                pass

    def __init__(
        self,
        flow: str,
        source: str,
        prefix: str = "msg",
        trace_file_path: Optional[str] = "pipeline_trace.jsonl",
    ):
        now = datetime.now(CET)
        short_id = uuid.uuid4().hex[:6]
        self.trace_id = f"{prefix}-{now.strftime('%H%M%S')}-{short_id}"
        self.flow = flow
        self.source = source
        disable_disk = os.environ.get("GEMINA_DISABLE_TRACE_FILE") == "1"
        self.trace_file_path = None if (disable_disk or not trace_file_path) else str(trace_file_path)
        self.arrival_ts = now.isoformat(timespec="milliseconds")
        self.start_perf = time.perf_counter()
        self.last_stage_perf = self.start_perf
        self.leaving_ts: Optional[str] = None
        self.final_status: str = "IN_PROGRESS"
        self.stages: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._completed = False

        with self._active_lock:
            self._active_traces[self.trace_id] = {
                "trace_id": self.trace_id,
                "flow": self.flow,
                "source": self.source,
                "final_status": self.final_status,
                "arrival_ts": self.arrival_ts,
                "leaving_ts": None,
                "total_duration_ms": 0.0,
                "stage_count": 0,
                "stages": [],
            }

    def add_stage(
        self,
        stage: str,
        status: str = "OK",
        input_data: Any = None,
        transformation: str = "",
        output_data: Any = None,
        duration_ms: Optional[float] = None,
    ) -> None:
        with self._lock:
            now_perf = time.perf_counter()
            if duration_ms is None:
                duration_ms = round((now_perf - self.last_stage_perf) * 1000.0, 2)
            else:
                duration_ms = round(float(duration_ms), 2)
            self.last_stage_perf = now_perf

            seq = len(self.stages) + 1
            ts_iso = datetime.now(CET).isoformat(timespec="milliseconds")
            entry = {
                "seq": seq,
                "stage": stage,
                "ts": ts_iso,
                "duration_ms": duration_ms,
                "status": status,
                "input": input_data,
                "transformation": transformation,
                "output": output_data,
            }
            self.stages.append(entry)
            stage_copy = copy.deepcopy(entry)

        with self._active_lock:
            active = self._active_traces.get(self.trace_id)
            if active is not None:
                active["stages"].append(copy.deepcopy(stage_copy))
                active["stage_count"] = len(active["stages"])
                active["total_duration_ms"] = round((time.perf_counter() - self.start_perf) * 1000.0, 2)

        in_str = json.dumps(input_data, ensure_ascii=False) if input_data is not None else "-"
        out_str = json.dumps(output_data, ensure_ascii=False) if output_data is not None else "-"
        if len(in_str) > 180:
            in_str = in_str[:177] + "..."
        if len(out_str) > 180:
            out_str = out_str[:177] + "..."
        print(
            f"[TRACE {self.trace_id}] [{seq}:{stage}] ({duration_ms}ms) status={status} | "
            f"in={in_str} -> transform='{transformation}' -> out={out_str}"
        )

        self._emit_event(
            "trace.stage",
            {
                "trace_id": self.trace_id,
                "flow": self.flow,
                "source": self.source,
                "arrival_ts": self.arrival_ts,
                "stage": stage_copy,
            },
        )

    def complete(self, final_status: str = "COMPLETED") -> dict[str, Any]:
        with self._lock:
            if self._completed and self.leaving_ts is not None:
                return {
                    "trace_id": self.trace_id,
                    "flow": self.flow,
                    "source": self.source,
                    "final_status": self.final_status,
                    "arrival_ts": self.arrival_ts,
                    "leaving_ts": self.leaving_ts,
                    "total_duration_ms": round((time.perf_counter() - self.start_perf) * 1000.0, 2),
                    "stage_count": len(self.stages),
                    "stages": copy.deepcopy(self.stages),
                }

            self.leaving_ts = datetime.now(CET).isoformat(timespec="milliseconds")
            self.final_status = final_status
            self._completed = True
            total_ms = round((time.perf_counter() - self.start_perf) * 1000.0, 2)
            record = {
                "trace_id": self.trace_id,
                "flow": self.flow,
                "source": self.source,
                "final_status": self.final_status,
                "arrival_ts": self.arrival_ts,
                "leaving_ts": self.leaving_ts,
                "total_duration_ms": total_ms,
                "stage_count": len(self.stages),
                "stages": copy.deepcopy(self.stages),
            }

        with self._active_lock:
            self._active_traces.pop(self.trace_id, None)

        if self.trace_file_path:
            try:
                Path(self.trace_file_path).parent.mkdir(parents=True, exist_ok=True)
                file_lock = self._lock_for_path(self.trace_file_path)
                with file_lock:
                    with open(self.trace_file_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        f.flush()
            except Exception as e:
                print(f"[Trace Write Error] {e}")

        print(
            f"[TRACE {self.trace_id}] [DONE] flow={self.flow} status={self.final_status} "
            f"arrival={self.arrival_ts} -> leaving={self.leaving_ts} (total={total_ms}ms, stages={len(record['stages'])})"
        )
        self._emit_event("trace.completed", record)
        return record
