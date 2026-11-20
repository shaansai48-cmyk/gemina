"""
Whitelisted Linux & GPU Diagnostic Command Executor for Gemina Agents.
Uses concurrent subprocess execution and a thread-safe stale-while-revalidate TTL cache
so RTX 3080 Ti D3cold wake latency (~2.1s) never blocks status tickers or UI SSE ticks.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import shutil
import subprocess
import threading
import time
from typing import Any, Optional


SAFE_DIAGNOSTIC_COMMANDS: dict[str, list[str]] = {
    "gpu": ["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total", "--format=csv,noheader"],
    "services": ["systemctl", "is-active", "nvda-copilot.service", "ollama.service"],
    "uptime_load": ["uptime"],
    "memory": ["free", "-h"],
    "disk": ["df", "-h", "/"],
    "recent_logs": ["journalctl", "-u", "nvda-copilot.service", "-n", "15", "--no-pager"],
    "ollama_models": ["ollama", "ps"],
}


class SystemDiagnostics:
    """Executes safe, read-only host & GPU telemetry commands."""

    _metrics_lock = threading.Lock()
    _cached_metrics: Optional[dict[str, Any]] = None
    _cached_metrics_ts: float = 0.0
    _refresh_in_flight: bool = False

    _cmds_lock = threading.Lock()
    _cached_cmds: Optional[dict[str, str]] = None
    _cached_cmds_ts: float = 0.0

    @staticmethod
    def run_cmd(cmd: list[str], timeout: float = 3.0) -> str:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            return (res.stdout or res.stderr or "").strip()
        except Exception as e:
            return f"err: {e}"

    @classmethod
    def invalidate_cache(cls) -> None:
        """Clears cached host metrics and diagnostic command outputs (mainly for tests)."""
        with cls._metrics_lock:
            cls._cached_metrics = None
            cls._cached_metrics_ts = 0.0
            cls._refresh_in_flight = False
        with cls._cmds_lock:
            cls._cached_cmds = None
            cls._cached_cmds_ts = 0.0

    @classmethod
    def run_all_diagnostic_commands(
        cls,
        max_age_sec: float = 5.0,
        force_refresh: bool = False,
    ) -> dict[str, str]:
        """Executes all whitelisted commands concurrently with a short TTL cache."""
        if not force_refresh and max_age_sec > 0:
            with cls._cmds_lock:
                if cls._cached_cmds is not None and (time.monotonic() - cls._cached_cmds_ts) <= max_age_sec:
                    return dict(cls._cached_cmds)

        with ThreadPoolExecutor(max_workers=len(SAFE_DIAGNOSTIC_COMMANDS)) as pool:
            futures = {name: pool.submit(cls.run_cmd, cmd) for name, cmd in SAFE_DIAGNOSTIC_COMMANDS.items()}
            results = {name: fut.result() for name, fut in futures.items()}

        with cls._cmds_lock:
            cls._cached_cmds = dict(results)
            cls._cached_cmds_ts = time.monotonic()
        return results

    @classmethod
    def _collect_host_metrics_uncached(cls) -> dict[str, Any]:
        red_alerts: list[str] = []

        gpu_cmd = [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        svc_cmd = ["systemctl", "is-active", "ollama.service"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            gpu_fut = pool.submit(cls.run_cmd, gpu_cmd)
            svc_fut = pool.submit(cls.run_cmd, svc_cmd)
            gpu_raw = gpu_fut.result()
            ollama_state = svc_fut.result()

        gpu_temp, gpu_mem_used, gpu_mem_total, gpu_util = 0, 0, 16384, 0
        if gpu_raw and not gpu_raw.startswith("err:"):
            try:
                parts = [p.strip() for p in gpu_raw.split(",")]
                gpu_temp = int(float(parts[0]))
                gpu_mem_used = int(float(parts[1]))
                gpu_mem_total = int(float(parts[2]))
                gpu_util = int(float(parts[3]))
            except Exception:
                pass
        else:
            red_alerts.append(f"GPU/NVIDIA driver check failed ({gpu_raw})")

        if gpu_temp >= 85:
            red_alerts.append(f"GPU temperature critical: {gpu_temp}C (>= 85C)")

        if ollama_state != "active":
            red_alerts.append(f"Local failover ollama.service is {ollama_state}")

        mem_pct = 0.0
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                meminfo = {}
                for line in f:
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    meminfo[k.strip()] = int(v.strip().split()[0])
                total_kb = meminfo.get("MemTotal", 1)
                avail_kb = meminfo.get("MemAvailable", total_kb)
                mem_pct = round((1.0 - (avail_kb / total_kb)) * 100.0, 1)
                if mem_pct >= 92.0:
                    red_alerts.append(f"System RAM critically high: {mem_pct}% used")
        except Exception:
            pass

        disk_pct = 0.0
        try:
            du = shutil.disk_usage("/")
            disk_pct = round((du.used / du.total) * 100.0, 1)
            if disk_pct >= 92.0:
                red_alerts.append(f"Root disk usage critically high: {disk_pct}% used")
        except Exception:
            pass

        return {
            "gpu_temp_c": gpu_temp,
            "gpu_mem_used_mb": gpu_mem_used,
            "gpu_mem_total_mb": gpu_mem_total,
            "gpu_vram_mb": f"{gpu_mem_used}/{gpu_mem_total}",
            "gpu_util_pct": gpu_util,
            "ram_used_pct": mem_pct,
            "disk_used_pct": disk_pct,
            "ollama_service": ollama_state,
            "red_alerts": red_alerts,
        }

    @classmethod
    def _spawn_background_refresh(cls) -> None:
        def _bg() -> None:
            try:
                fresh = cls._collect_host_metrics_uncached()
                with cls._metrics_lock:
                    cls._cached_metrics = fresh
                    cls._cached_metrics_ts = time.monotonic()
            finally:
                with cls._metrics_lock:
                    cls._refresh_in_flight = False

        t = threading.Thread(target=_bg, name="gemina-diag-refresh", daemon=True)
        t.start()

    @classmethod
    def warm_cache(cls) -> None:
        """Non-blocking background warmup of host/GPU telemetry at daemon startup."""
        with cls._metrics_lock:
            if cls._refresh_in_flight:
                return
            cls._refresh_in_flight = True
        cls._spawn_background_refresh()

    @classmethod
    def collect_host_metrics(
        cls,
        max_age_sec: float = 15.0,
        allow_stale: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Returns host metrics using stale-while-revalidate caching for sub-millisecond reads."""
        if not force_refresh:
            with cls._metrics_lock:
                if cls._cached_metrics is not None:
                    age = time.monotonic() - cls._cached_metrics_ts
                    if age <= max_age_sec:
                        return copy.deepcopy(cls._cached_metrics)
                    if allow_stale:
                        if not cls._refresh_in_flight:
                            cls._refresh_in_flight = True
                            cls._spawn_background_refresh()
                        return copy.deepcopy(cls._cached_metrics)

        fresh = cls._collect_host_metrics_uncached()
        with cls._metrics_lock:
            cls._cached_metrics = fresh
            cls._cached_metrics_ts = time.monotonic()
            cls._refresh_in_flight = False
        return copy.deepcopy(fresh)
