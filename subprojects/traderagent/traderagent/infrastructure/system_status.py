"""
Multi-Ticker Traffic-Light System & Portfolio Health Evaluator (Infrastructure Service).
Monitors ALL focus tickers AND ALL held equity/Mini positions, binds spot telemetry strictly
by ticker (eliminating cross-ticker mismatches), and outputs clean Telegram HTML status headers.
"""
from datetime import datetime
import html
import time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from gemina.core.diagnostics import SystemDiagnostics
from traderagent.domain.models import TradeDecision, normalize_ticker, resolve_focus_ticker
from traderagent.infrastructure.apps_script_client import AppsScriptClient
from traderagent.infrastructure.persistence import StateRepository

CET = ZoneInfo("Europe/Stockholm")


class StatusEvaluator:
    """Evaluates cached host/webhook health and multi-ticker portfolio risk thresholds."""

    def __init__(self, repo: StateRepository, apps_script: AppsScriptClient):
        self.repo = repo
        self.apps_script = apps_script

    def _apps_script_status(self) -> dict[str, Any]:
        if hasattr(self.apps_script, "get_status") and callable(self.apps_script.get_status):
            res = self.apps_script.get_status()
            if isinstance(res, dict):
                return res
        return {
            "last_ok": bool(getattr(self.apps_script, "last_ok", True)),
            "last_error": str(getattr(self.apps_script, "last_error", "")),
            "last_poll_epoch": 0.0,
            "last_nonempty_epoch": 0.0,
            "last_bar_ts_by_ticker": {},
        }

    def evaluate(
        self,
        portfolio: Optional[dict[str, Any]] = None,
        risk: Optional[dict[str, Any]] = None,
        latest_telemetry: Optional[dict[str, Any]] = None,
        latest_decision: Optional[TradeDecision] = None,
        ticker: Optional[str] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        if portfolio is None:
            portfolio = self.repo.load_portfolio()
        if risk is None:
            risk = self.repo.load_risk()

        watchlist = portfolio.get("watchlist", {})
        stats = portfolio.get("daily_stats", {})
        focus_raw = str(portfolio.get("active_focus_ticker", "NVDA"))
        focus_list = [normalize_ticker(x) for x in focus_raw.split(",") if x.strip()]

        # Scan latest audit entry per ticker across bounded history
        latest_by_ticker: dict[str, tuple[Optional[dict[str, Any]], Optional[TradeDecision]]] = {}
        raw_audit_map = (
            self.repo.get_latest_audit_by_ticker(max_scan_lines=300)
            if hasattr(self.repo, "get_latest_audit_by_ticker")
            else {}
        )
        if not isinstance(raw_audit_map, dict):
            raw_audit_map = {}

        for sym, entry in raw_audit_map.items():
            dec_obj = None
            if entry.get("decision"):
                try:
                    dec_obj = TradeDecision.model_validate(entry["decision"])
                except Exception:
                    dec_obj = None
            latest_by_ticker[normalize_ticker(sym)] = (entry.get("telemetry"), dec_obj)

        telem_sym = ""
        if latest_telemetry is not None:
            telem_sym = normalize_ticker(ticker or str(latest_telemetry.get("ticker", "")))
            if telem_sym:
                prior_tel, prior_dec = latest_by_ticker.get(telem_sym, (None, None))
                latest_by_ticker[telem_sym] = (latest_telemetry, latest_decision or prior_dec)
        elif latest_decision is not None:
            telem_sym = normalize_ticker(ticker or latest_decision.ticker)
            if telem_sym:
                prior_tel, _ = latest_by_ticker.get(telem_sym, (None, None))
                latest_by_ticker[telem_sym] = (prior_tel, latest_decision)

        # Build deduplicated ordered monitored set: all focus tickers + all held positions
        monitored: list[str] = []
        for sym in focus_list + [
            sym
            for sym, a in watchlist.items()
            if int(a.get("regular_shares_held", 0)) > 0 or int(a.get("mini_units_held", 0)) > 0
        ]:
            norm = normalize_ticker(sym)
            if norm and norm not in monitored:
                monitored.append(norm)

        host = SystemDiagnostics.collect_host_metrics(
            max_age_sec=15.0,
            allow_stale=not force_refresh,
            force_refresh=force_refresh,
        )
        apps_st = self._apps_script_status()
        host_faults: list[str] = list(host.get("red_alerts", []))
        if not apps_st.get("last_ok", True):
            host_faults.append(f"Webhook error: {apps_st.get('last_error', '')}")

        trade_alerts: list[str] = []
        breached_tickers: list[str] = []

        for sym in monitored:
            w_asset = watchlist.get(sym, {})
            t_tel, t_dec = latest_by_ticker.get(sym, (None, None))
            shares_held = int(w_asset.get("regular_shares_held", 0))
            mini_held = int(w_asset.get("mini_units_held", 0))
            hard_stop = float(w_asset.get("hard_stop_price", 0.0))
            is_short_mini = mini_held > 0 and (
                str(w_asset.get("mini_side", "")).upper() == "SHORT"
                or "MINI S" in str(w_asset.get("mini_instrument", "")).upper()
            )

            if isinstance(t_tel, dict) and "close" in t_tel and (shares_held > 0 or mini_held > 0):
                spot = float(t_tel["close"])
                if shares_held > 0 or (mini_held > 0 and not is_short_mini):
                    if hard_stop > 0:
                        if spot <= hard_stop:
                            breached_tickers.append(sym)
                            trade_alerts.append(
                                f"Stop breached on {sym}: closed at ${spot:.2f} (stop ${hard_stop:.2f})"
                            )
                        elif "atr14" in t_tel and (spot - hard_stop) <= 0.35 * float(t_tel["atr14"]):
                            trade_alerts.append(
                                f"Near stop on {sym}: ${spot:.2f} within 0.35 ATR of ${hard_stop:.2f}"
                            )
                if is_short_mini:
                    short_bar = float(w_asset.get("mini_short_barrier", 0.0))
                    if short_bar > 0 and spot >= short_bar:
                        breached_tickers.append(sym)
                        trade_alerts.append(
                            f"Short barrier risk on {sym}: ${spot:.2f} >= barrier ${short_bar:.2f}"
                        )

            if t_dec and t_dec.action != "HOLD":
                # Ignore stale exit alerts after position was closed
                if t_dec.action == "SELL_SHARES" and shares_held <= 0:
                    continue
                if t_dec.action in ("SELL_MINI_LONG", "SELL_MINI_SHORT") and mini_held <= 0:
                    continue
                trade_alerts.append(f"{sym}: {t_dec.action} ({t_dec.quantity}x {t_dec.instrument})")

        realized_pnl = float(stats.get("realized_pnl_today_sek", 0.0))
        if realized_pnl <= float(risk.get("daily_loss_lockout_sek", -900.0)):
            trade_alerts.append(
                f"Daily loss limit hit ({realized_pnl:.0f} SEK <= {risk['daily_loss_lockout_sek']:.0f} SEK)"
            )

        mins_since_stop = (time.time() - float(stats.get("last_stopout_epoch", 0))) / 60.0
        if mins_since_stop < float(risk.get("cooldown_minutes_after_stopout", 45)):
            left = int(risk["cooldown_minutes_after_stopout"] - mins_since_stop)
            trade_alerts.append(f"Stop-out cooldown active: {left}m remaining")

        # Choose most informative subject ticker matching any explicit telemetry or breach
        display_ticker = ""
        if telem_sym:
            display_ticker = telem_sym
        elif breached_tickers:
            display_ticker = breached_tickers[0]
        else:
            for sym in monitored:
                if sym in latest_by_ticker and latest_by_ticker[sym][0] is not None:
                    display_ticker = sym
                    break
            if not display_ticker:
                display_ticker = resolve_focus_ticker(portfolio, prefer_held_shares=True)

        disp_asset = watchlist.get(display_ticker, {})
        disp_tel, disp_dec = latest_by_ticker.get(display_ticker, (None, None))
        disp_stop = float(disp_asset.get("hard_stop_price", 0.0))
        cur_sym = "$" if disp_asset.get("currency", "USD") == "USD" else ""
        if isinstance(disp_tel, dict) and "close" in disp_tel:
            spot_str = f"{cur_sym}{float(disp_tel['close']):.2f}"
        else:
            spot_str = "No live bar"

        # Check stale US webhook feed during open cash hours
        now_cet = datetime.now(CET)
        is_us_cash_open = now_cet.weekday() < 5 and ("15:45" <= now_cet.strftime("%H:%M") <= "21:45")
        feed_warn = False
        if is_us_cash_open and apps_st["last_poll_epoch"] > 0 and apps_st["last_nonempty_epoch"] > 0:
            if (time.time() - apps_st["last_nonempty_epoch"]) > 2100:
                feed_warn = True

        red_reasons = host_faults + trade_alerts
        is_red = len(red_reasons) > 0
        color = "🔴" if is_red else "🟢"
        gpu_temp = host["gpu_temp_c"]
        ollama_state = host["ollama_service"]
        has_host_issue = len(host_faults) > 0 or feed_warn
        sys_label = "System ISSUE" if len(host_faults) > 0 else ("Feed Delayed" if feed_warn else "System OK")
        sys_tag = "SYS:OK" if (ollama_state == "active" and apps_st["last_ok"] and gpu_temp < 85 and not feed_warn) else "SYS:WARN"
        pnl_sign = "+" if realized_pnl > 0 else ""

        stop_part = f" · Stop {cur_sym}{disp_stop:.2f}" if disp_stop > 0 else ""
        state_part = "<b>ACTION NEEDED</b>" if is_red else sys_label
        ticker_line = (
            f"{color} <b>{html.escape(display_ticker)}</b> {html.escape(spot_str)}{html.escape(stop_part)} "
            f"· Today {pnl_sign}{int(realized_pnl)} SEK · {state_part}"
        )

        expanded_block = ""
        if is_red:
            detail_lines = [f"• {html.escape(r)}" for r in red_reasons]
            if disp_asset:
                sh = int(disp_asset.get("regular_shares_held", 0))
                avg = float(disp_asset.get("blended_avg_price", 0.0))
                mn = int(disp_asset.get("mini_units_held", 0))
                cash = int(float(portfolio.get("cash_reserves", {}).get("available_tactical_mini_sek", 0.0)))
                detail_lines.append(
                    f"• Position ({html.escape(display_ticker)}): {sh} shares (avg {cur_sym}{avg:.2f}) · "
                    f"{mn} Mini units · Cash {cash:,} SEK"
                )
            if has_host_issue:
                detail_lines.append(
                    f"• Host: GPU {gpu_temp}C ({host['gpu_vram_mb']} MB, {host['gpu_util_pct']}%) · "
                    f"RAM {host['ram_used_pct']}% · Disk {host['disk_used_pct']}% · Ollama {html.escape(ollama_state)}"
                )
            expanded_block = "\n" + "\n".join(detail_lines)

        return {
            "is_red": is_red,
            "color": color,
            "subject_ticker": display_ticker,
            "monitored_tickers": monitored,
            "breached_tickers": breached_tickers,
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
                "apps_script_ok": apps_st["last_ok"],
                "apps_script_error": apps_st["last_error"],
                "sys_tag": sys_tag,
                "feed_delayed": feed_warn,
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
