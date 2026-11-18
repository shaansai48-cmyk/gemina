"""
Whitelisted Linux & GPU Diagnostic Command Executor for Gemina Agents.
"""
import shutil
import subprocess
from typing import Any


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

    @staticmethod
    def run_cmd(cmd: list[str], timeout: float = 3.0) -> str:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            return (res.stdout or res.stderr or "").strip()
        except Exception as e:
            return f"err: {e}"

    @classmethod
    def run_all_diagnostic_commands(cls) -> dict[str, str]:
        return {name: cls.run_cmd(cmd) for name, cmd in SAFE_DIAGNOSTIC_COMMANDS.items()}

    @classmethod
    def collect_host_metrics(cls) -> dict[str, Any]:
        red_alerts: list[str] = []

        gpu_raw = cls.run_cmd([
            "nvidia-smi",
            "--query-gpu=temperature.gpu,memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits"
        ])
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
            red_alerts.append(f"GPU/NVIDIA driver check failed (`{gpu_raw}`)")

        if gpu_temp >= 85:
            red_alerts.append(f"GPU temperature critical: `{gpu_temp}°C` (>= 85°C)")

        ollama_state = cls.run_cmd(["systemctl", "is-active", "ollama.service"])
        if ollama_state != "active":
            red_alerts.append(f"Local failover `ollama.service` is `{ollama_state}`")

        mem_pct = 0.0
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                meminfo = {}
                for line in f:
                    k, v = line.split(":", 1)
                    meminfo[k.strip()] = int(v.strip().split()[0])
                total_kb = meminfo.get("MemTotal", 1)
                avail_kb = meminfo.get("MemAvailable", total_kb)
                mem_pct = round((1.0 - (avail_kb / total_kb)) * 100.0, 1)
                if mem_pct >= 92.0:
                    red_alerts.append(f"System RAM critically high: `{mem_pct}%` used")
        except Exception:
            pass

        disk_pct = 0.0
        try:
            du = shutil.disk_usage("/")
            disk_pct = round((du.used / du.total) * 100.0, 1)
            if disk_pct >= 92.0:
                red_alerts.append(f"Root disk usage critically high: `{disk_pct}%` used")
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
