"""
Traffic-Light System & Portfolio Health Evaluator (Infrastructure Service).
Produces a 1-line compact 🟢 GREEN status ticker, auto-expanding with full details ONLY when 🔴 RED.
"""
import time
from typing import Any, Optional
from gemina.core.diagnostics import SystemDiagnostics
from traderagent.domain.models import TradeDecision
from traderagent.infrastructure.apps_script_client import AppsScriptClient
from traderagent.infrastructure.persistence import StateRepository


class StatusEvaluator:
    """Evaluates live Linux/GPU commands and portfolio risk thresholds."""

    def __init__(self, repo: StateRepository, apps_script: AppsScriptClient):
        self.repo = repo
        self.apps_script = apps_script

    def evaluate(
        self,
        portfolio: Optional[dict[str, Any]] = None,
        risk: Optional[dict[str, Any]] = None,
        latest_telemetry: Optional[dict[str, Any]] = None,
        latest_decision: Optional[TradeDecision] = None,
    ) -> dict[str, Any]:
        if portfolio is None:
            portfolio = self.repo.load_portfolio()
        if risk is None:
            risk = self.repo.load_risk()

        focus = portfolio["active_focus_ticker"]
        asset = portfolio["watchlist"].get(focus, {})
        stats = portfolio["daily_stats"]

        if latest_telemetry is None:
            for entry in reversed(self.repo.get_recent_audit_tail(10)):
                if entry.get("ticker", "").upper() == focus.upper():
                    latest_telemetry = entry.get("telemetry")
                    if latest_decision is None and entry.get("decision"):
                        try:
                            latest_decision = TradeDecision.model_validate(entry["decision"])
                        except Exception:
                            pass
                    break

        host = SystemDiagnostics.collect_host_metrics()
        red_reasons: list[str] = list(host["red_alerts"])

        if not self.apps_script.last_ok:
            red_reasons.append(f"Apps Script Webhook error: `{self.apps_script.last_error}`")

        spot_str = "N/A"
        hard_stop = float(asset.get("hard_stop_price", 0.0))
        if latest_telemetry and "close" in latest_telemetry:
            spot = float(latest_telemetry["close"])
            cur_sym = "$" if asset.get("currency") == "USD" else ""
            spot_str = f"{cur_sym}{spot}"
            if hard_stop > 0 and (asset.get("regular_shares_held", 0) > 0 or asset.get("mini_units_held", 0) > 0):
                if spot <= hard_stop:
                    red_reasons.append(
                        f"STOP BREACH on `{focus}`: Close `{spot}` <= Hard Stop `{hard_stop}`!"
                    )
                elif "atr14" in latest_telemetry and (spot - hard_stop) <= 0.35 * float(latest_telemetry["atr14"]):
                    red_reasons.append(
                        f"NEAR HARD STOP on `{focus}`: Close `{spot}` is within 0.35 ATR of Stop `{hard_stop}`"
                    )

        if latest_decision and latest_decision.action != "HOLD":
            red_reasons.append(
                f"TRADE ACTION REQUIRED: *{latest_decision.action}* (`{latest_decision.quantity}`x `{latest_decision.instrument}`)"
            )

        realized_pnl = float(stats.get("realized_pnl_today_sek", 0.0))
        if realized_pnl <= float(risk.get("daily_loss_lockout_sek", -900.0)):
            red_reasons.append(
                f"DAILY LOSS LOCKOUT: Realized PnL `{realized_pnl} SEK` <= `{risk['daily_loss_lockout_sek']} SEK`"
            )

        mins_since_stop = (time.time() - float(stats.get("last_stopout_epoch", 0))) / 60.0
        if mins_since_stop < float(risk.get("cooldown_minutes_after_stopout", 45)):
            left = int(risk["cooldown_minutes_after_stopout"] - mins_since_stop)
            red_reasons.append(f"STOP-OUT COOLDOWN ACTIVE: `{left}m` remaining")

        is_red = len(red_reasons) > 0
        color = "🔴" if is_red else "🟢"
        gpu_temp = host["gpu_temp_c"]
        ollama_state = host["ollama_service"]
        sys_tag = "SYS:OK" if (ollama_state == "active" and self.apps_script.last_ok and gpu_temp < 85) else "SYS:WARN"
        pnl_sign = "+" if realized_pnl > 0 else ""

        ticker_line = (
            f"{color} `[{focus} {spot_str} | Stop {hard_stop} | "
            f"PnL {pnl_sign}{int(realized_pnl)} SEK | GPU {gpu_temp}°C | {sys_tag}]`"
        )

        expanded_block = ""
        if is_red:
            bullet_lines = "\n".join(f"• {reason}" for reason in red_reasons)
            expanded_block = (
                f"\n🚨 *RED STATUS DETAILS:*\n{bullet_lines}\n"
                f"• *Hardware:* GPU `{gpu_temp}°C` (`{host['gpu_vram_mb']} MB`, `{host['gpu_util_pct']}%`) | "
                f"RAM `{host['ram_used_pct']}%` | Disk `{host['disk_used_pct']}%` | Ollama `{ollama_state}`\n"
                f"• *Holdings ({focus}):* Shares `{asset.get('regular_shares_held', 0)}` @ `{asset.get('blended_avg_price', 0)}` | "
                f"Mini `{asset.get('mini_units_held', 0)}`u | Cash `{int(portfolio['cash_reserves']['available_tactical_mini_sek'])} SEK`"
            )

        return {
            "is_red": is_red,
            "color": color,
            "ticker_line": ticker_line,
            "expanded_block": expanded_block,
            "red_reasons": red_reasons,
            "diagnostics": {
                "gpu_temp_c": gpu_temp,
                "gpu_vram_mb": host["gpu_vram_mb"],
                "gpu_util_pct": host["gpu_util_pct"],
                "ram_used_pct": host["ram_used_pct"],
                "disk_used_pct": host["disk_used_pct"],
                "ollama_service": ollama_state,
                "apps_script_ok": self.apps_script.last_ok,
            },
        }

    def format_with_status_ticker(self, body: str, status_info: Optional[dict[str, Any]] = None) -> str:
        if status_info is None:
            status_info = self.evaluate()
        header = status_info["ticker_line"]
        if status_info["is_red"]:
            header += status_info["expanded_block"]
        if not body:
            return header
        return f"{header}\n\n{body}"
