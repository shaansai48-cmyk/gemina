"""
End-to-End Message Lifecycle Tracer (Arrival -> Processing/Transformations -> Leaving).
Shared across all Gemina sub-agents.
"""
import json
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

CET = ZoneInfo("Europe/Stockholm")


class PipelineTracer:
    """
    Tracks a message from ARRIVAL -> each PROCESSING / TRANSFORMATION stage -> LEAVING.
    Writes both real-time structured stdout logs (for journalctl) and JSONL records.
    """

    def __init__(
        self,
        flow: str,
        source: str,
        prefix: str = "msg",
        trace_file_path: str = "pipeline_trace.jsonl",
    ):
        now = datetime.now(CET)
        short_id = uuid.uuid4().hex[:6]
        self.trace_id = f"{prefix}-{now.strftime('%H%M%S')}-{short_id}"
        self.flow = flow
        self.source = source
        self.trace_file_path = trace_file_path
        self.arrival_ts = now.isoformat(timespec="milliseconds")
        self.start_perf = time.perf_counter()
        self.last_stage_perf = self.start_perf
        self.leaving_ts: Optional[str] = None
        self.final_status: str = "IN_PROGRESS"
        self.stages: list[dict[str, Any]] = []

    def add_stage(
        self,
        stage: str,
        status: str = "OK",
        input_data: Any = None,
        transformation: str = "",
        output_data: Any = None,
        duration_ms: Optional[float] = None,
    ) -> None:
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

    def complete(self, final_status: str = "COMPLETED") -> dict[str, Any]:
        self.leaving_ts = datetime.now(CET).isoformat(timespec="milliseconds")
        self.final_status = final_status
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
            "stages": self.stages,
        }
        try:
            with open(self.trace_file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[Trace Write Error] {e}")

        print(
            f"[TRACE {self.trace_id}] [DONE] flow={self.flow} status={self.final_status} "
            f"arrival={self.arrival_ts} -> leaving={self.leaving_ts} (total={total_ms}ms, stages={len(self.stages)})"
        )
        return record
