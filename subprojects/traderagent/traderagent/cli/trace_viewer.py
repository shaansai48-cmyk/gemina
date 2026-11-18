#!/usr/bin/env python3
"""
CLI Log Processor & Waterfall Visualizer for Gemina TraderAgent Pipeline Traces.
Reads pipeline_trace.jsonl and displays each message's journey from ARRIVAL -> TRANSFORMATIONS -> LEAVING.
"""
import argparse
import json
import os
import time
from traderagent.config import TraderConfig

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def color_for_status(status: str) -> str:
    s = status.upper()
    if any(k in s for k in ("RED", "ERR", "REJECT", "VETO", "MUTATED", "FALLBACK", "DEGRADED", "CORRECTED")):
        return RED if "ERR" in s or "RED" in s else YELLOW
    return GREEN


def render_trace(t: dict, show_io: bool = True) -> None:
    sc = color_for_status(t.get("final_status", ""))
    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════════════════════╗{RESET}")
    print(
        f"{BOLD}{CYAN}║{RESET} {BOLD}TRACE ID:{RESET} {t['trace_id']:<26} "
        f"{BOLD}FLOW:{RESET} {t['flow']:<22} "
        f"{sc}{BOLD}[{t['final_status']}]{RESET}"
    )
    print(
        f"{BOLD}{CYAN}║{RESET} {BOLD}ARRIVAL :{RESET} {t['arrival_ts']}  -->  "
        f"{BOLD}LEAVING:{RESET} {t['leaving_ts']}  "
        f"({BOLD}{t['total_duration_ms']} ms{RESET})"
    )
    print(f"{BOLD}{CYAN}╠══════════════════════════════════════════════════════════════════════════════╣{RESET}")

    stages = t.get("stages", [])
    for idx, s in enumerate(stages):
        is_last = idx == len(stages) - 1
        branch = "╚═▶" if is_last else "╠═▶"
        pipe = "   " if is_last else "║  "
        stc = color_for_status(s.get("status", "OK"))
        print(
            f"{CYAN}{branch}{RESET} {BOLD}[{s['seq']}/{len(stages)} {s['stage']}]{RESET} "
            f"({s['duration_ms']} ms) {stc}[{s['status']}]{RESET} @ {DIM}{s['ts']}{RESET}"
        )
        print(f"{CYAN}{pipe}{RESET}  {BOLD}Transform:{RESET} {s.get('transformation', '')}")
        if show_io:
            in_str = json.dumps(s.get("input"), ensure_ascii=False)
            out_str = json.dumps(s.get("output"), ensure_ascii=False)
            print(f"{CYAN}{pipe}{RESET}  {DIM}In :{RESET} {in_str}")
            print(f"{CYAN}{pipe}{RESET}  {DIM}Out:{RESET} {out_str}")
    print()


def main() -> None:
    cfg = TraderConfig.from_env()
    parser = argparse.ArgumentParser(description="Process & visualize message arrival-to-leaving pipeline traces.")
    parser.add_argument("-n", "--last", type=int, default=5, help="Number of recent traces to display (default: 5)")
    parser.add_argument("-f", "--follow", action="store_true", help="Follow new traces in real time")
    parser.add_argument("--flow", type=str, default="", help="Filter by flow substring (e.g. 15m_quant_bar or telegram)")
    parser.add_argument("--id", type=str, default="", help="Filter by trace_id substring")
    parser.add_argument("--file", type=str, default=cfg.pipeline_trace_path, help="Path to pipeline_trace.jsonl")
    parser.add_argument("--compact", action="store_true", help="Hide raw In/Out JSON payloads per stage")
    args = parser.parse_args()

    trace_file = args.file
    if not os.path.exists(trace_file):
        print(f"No trace file found at {trace_file} yet.")
        if not args.follow:
            return

    def matches(t: dict) -> bool:
        if args.flow and args.flow.lower() not in t.get("flow", "").lower():
            return False
        if args.id and args.id.lower() not in t.get("trace_id", "").lower():
            return False
        return True

    if os.path.exists(trace_file):
        with open(trace_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        traces = [json.loads(line) for line in lines if matches(json.loads(line))]
        for t in traces[-args.last:]:
            render_trace(t, show_io=not args.compact)

    if args.follow:
        print(f"{DIM}--- Following live traces in {trace_file} (Ctrl+C to stop) ---{RESET}\n")
        while not os.path.exists(trace_file):
            time.sleep(1)
        with open(trace_file, "r", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                if matches(t):
                    render_trace(t, show_io=not args.compact)


if __name__ == "__main__":
    main()
