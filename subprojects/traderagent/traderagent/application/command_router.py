"""
Telegram Command & Free-Form Q&A Router (Application Use-Case Layer).
All responses use clean Telegram HTML (`parse_mode="HTML"`) with bottom-line-first
verdicts, strict numeric validation via LedgerService, and live /analyze via YahooFinanceClient.
"""
import html
import time
from pathlib import Path
from typing import Any, Optional

from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.tracing import PipelineTracer
from traderagent.agents.committee import QuantAgentCommittee
from traderagent.agents.qa_assistant import ConversationalQAAgent
from traderagent.application.ledger_service import (
    CommandInputError,
    LedgerService,
    _default_watchlist_asset,
)
from traderagent.config import TraderConfig
from traderagent.domain.models import (
    TICKER_ALIAS_MAP,
    normalize_ticker,
    resolve_focus_ticker,
)
from traderagent.domain.quant_math import (
    compute_quant_metrics,
    format_trade_alert_html,
)
from traderagent.infrastructure.market_data import MarketDataError, YahooFinanceClient
from traderagent.infrastructure.persistence import StateRepository
from traderagent.infrastructure.system_status import StatusEvaluator
from traderagent.infrastructure.telegram_adapter import TelegramGateway

__all__ = [
    "TICKER_ALIAS_MAP",
    "TelegramCommandRouter",
    "format_trace_summary_for_telegram",
    "resolve_focus_ticker",
]


def format_trace_summary_for_telegram(trace: dict[str, Any]) -> str:
    lines = [
        f"🔍 <b>Pipeline Trace</b> <code>{html.escape(str(trace['trace_id']))}</code>",
        f"• <b>Flow:</b> <code>{html.escape(str(trace['flow']))}</code> ({html.escape(str(trace['source']))})",
        f"• <b>Arrival:</b> <code>{html.escape(str(trace['arrival_ts']))}</code>",
        f"• <b>Leaving:</b> <code>{html.escape(str(trace['leaving_ts']))}</code>",
        f"• <b>Total Latency:</b> <code>{trace['total_duration_ms']} ms</code> | <b>Status:</b> <code>{html.escape(str(trace['final_status']))}</code>",
        "<b>Stages (Arrival -&gt; Multi-Agent Loop -&gt; Leaving):</b>",
    ]
    for s in trace.get("stages", []):
        lines.append(
            f"<code>{s['seq']}.</code> <b>{html.escape(str(s['stage']))}</b> "
            f"(<code>{s['duration_ms']}ms</code>, <code>{html.escape(str(s['status']))}</code>)\n"
            f"   -&gt; <i>{html.escape(str(s['transformation']))}</i>"
        )
    return "\n".join(lines)


class TelegramCommandRouter:
    """Dispatches Telegram slash commands, /analyze & /analyse, ledger actions, and free-form Q&A."""

    def __init__(
        self,
        config: TraderConfig,
        repo: StateRepository,
        status_evaluator: StatusEvaluator,
        telegram: TelegramGateway,
        qa_agent: ConversationalQAAgent,
        committee: Optional[QuantAgentCommittee] = None,
        market_data: Optional[YahooFinanceClient] = None,
        ledger: Optional[LedgerService] = None,
    ):
        self.config = config
        self._repo = repo
        self._ledger = ledger or LedgerService(repo)
        self.status_evaluator = status_evaluator
        self.telegram = telegram
        self.qa_agent = qa_agent
        self.committee = committee
        self.market_data = market_data or YahooFinanceClient()

    @property
    def repo(self) -> StateRepository:
        return self._repo

    @repo.setter
    def repo(self, value: StateRepository) -> None:
        self._repo = value
        if getattr(self, "_ledger", None) is not None:
            self._ledger.repo = value

    def _get_committee(self) -> QuantAgentCommittee:
        if self.committee is None:
            self.committee = QuantAgentCommittee(self.qa_agent.llm)
        return self.committee

    def _handle_analyze(
        self,
        parts: list[str],
        portfolio: dict[str, Any],
        tracer: Optional[PipelineTracer] = None,
    ) -> str:
        raw_args = parts[1:]
        lower_set = {a.lower() for a in raw_args}
        is_deep = "deep" in lower_set
        ticker_tokens = [a for a in raw_args if a.lower() not in ("deep", "quick", "fast")]

        explicit_raw = " ".join(ticker_tokens).strip() if ticker_tokens else None
        watchlist = portfolio.get("watchlist", {})
        if explicit_raw:
            norm = normalize_ticker(explicit_raw)
            target_ticker = norm if norm in watchlist else (norm or resolve_focus_ticker(portfolio))
        else:
            target_ticker = resolve_focus_ticker(portfolio, prefer_held_mini=True)

        asset = watchlist.get(target_ticker) or _default_watchlist_asset(target_ticker)
        risk = self.repo.load_risk()

        self.telegram.send_typing()
        stale_note = ""
        try:
            telemetry = self.market_data.fetch_15m_telemetry(target_ticker, asset=asset)
        except MarketDataError as exc:
            latest_map = (
                self.repo.get_latest_audit_by_ticker(max_scan_lines=300)
                if hasattr(self.repo, "get_latest_audit_by_ticker")
                else {}
            )
            fallback_row = latest_map.get(target_ticker) or {}
            fallback_tel = fallback_row.get("telemetry")
            if isinstance(fallback_tel, dict) and "close" in fallback_tel:
                telemetry = dict(fallback_tel)
                telemetry.setdefault("ticker", target_ticker)
                usd_sek, _ = self.market_data.fetch_usd_sek(default=10.16)
                telemetry.setdefault("usd_sek", usd_sek)
                stale_note = f"Live market feed unavailable ({exc}); using last webhook bar."
            else:
                return (
                    f"⚠️ Could not fetch live market data for <code>{html.escape(target_ticker)}</code>.\n"
                    f"• <b>Reason:</b> {html.escape(str(exc))}"
                )

        usd_sek = float(telemetry.get("usd_sek") or 10.16)
        metrics = compute_quant_metrics(telemetry, asset, usd_sek=usd_sek)
        ticker_exp = self.repo.experience.get_ticker_experience(target_ticker)

        decision = self._get_committee().evaluate_live_snapshot(
            telemetry=telemetry,
            portfolio=portfolio,
            asset=asset,
            risk=risk,
            metrics=metrics,
            ticker_experience=ticker_exp,
            allow_live_search=True,
            deep=is_deep,
            tracer=tracer,
        )
        msg = format_trade_alert_html(
            decision=decision,
            telemetry=telemetry,
            portfolio=portfolio,
            asset=asset,
            metrics=metrics,
            risk=risk,
            source_label="/analyze deep" if is_deep else "/analyze",
        )
        if stale_note:
            msg = f"<i>⚠️ {html.escape(stale_note)}</i>\n\n{msg}"
        return msg

    def execute_text_command(self, text: str, tracer: Optional[PipelineTracer] = None) -> tuple[str, Optional[dict[str, Any]]]:
        """
        Executes a command or free-form question and returns (response_body_html, optional_status_override).
        Used by Telegram polling, the Web UI API, and SubAgentPlugin.
        """
        text = text.strip()
        if not text.startswith("/"):
            if tracer:
                tracer.add_stage(
                    stage="ROUTE_PARSE",
                    status="OK",
                    input_data={"text": text},
                    transformation="Routed plain-text message to Q&A assistant",
                    output_data={"route": "general_qa"},
                )
            self.telegram.send_typing()
            return self.qa_agent.answer(text, tracer=tracer), None

        portfolio = self.repo.load_portfolio()
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]

        if tracer:
            tracer.add_stage(
                stage="ROUTE_PARSE",
                status="OK",
                input_data={"text": text},
                transformation=f"Parsed slash command '{cmd}' with {len(parts) - 1} arg(s)",
                output_data={"command": cmd, "args": parts[1:]},
            )

        if cmd in ("/help", "/start"):
            return (
                "🤖 <b>Gemina TraderAgent &amp; Quant Command Center</b>\n\n"
                "<b>Analysis &amp; Questions:</b>\n"
                "• <code>/analyze [TICKER] [deep]</code> (alias <code>/analyse</code>) — Live 15m chart verdict + news + Avanza next step.\n"
                "• Type <b>any message</b> (no <code>/</code>) or <code>/ask &lt;question&gt;</code> to ask the AI assistant.\n"
                "• <code>/clear</code> — Clear conversation memory.\n\n"
                "<b>Status &amp; Diagnostics:</b>\n"
                "• <code>/status</code> — Compact status line (auto-expands on 🔴 RED).\n"
                "• <code>/fullstatus</code> — Full portfolio, held positions &amp; GPU/RAM/service health.\n"
                "• <code>/watchlist</code> — Tracked tickers, hard stops &amp; Mini parameters.\n"
                "• <code>/trace [N]</code> — View last <code>N</code> pipeline execution traces.\n"
                "• <code>/experience [TICKER]</code> — 60-day backtest &amp; live stats dossier.\n\n"
                "<b>Portfolio &amp; Watchlist Controls:</b>\n"
                "• <code>/focus &lt;TICKER[,TICKER]&gt;</code> — Switch active watchlist focus.\n"
                "• <code>/add_ticker &lt;SYM&gt; &lt;USD|SEK&gt; &lt;STOP&gt; &lt;MINI_FIN&gt; [PARITY]</code>\n"
                "• <code>/fill_mini [TICKER] &lt;UNITS&gt; &lt;PRICE_SEK&gt; [LONG|SHORT]</code> | <code>/close_mini [TICKER] &lt;EXIT_SEK&gt;</code>\n"
                "• <code>/fill_shares [TICKER] &lt;QTY&gt; &lt;AVG_PRICE&gt;</code> | <code>/close_shares [TICKER] &lt;EXIT_PRICE&gt;</code>\n"
                "• <code>/set_stop [TICKER] &lt;STOP&gt; [TARGET]</code> | <code>/set_mini [TICKER] &lt;FIN_LVL&gt; [BARRIER] [PARITY]</code>\n\n"
                "<b>Declarative Agent Controls:</b>\n"
                "• <code>/reload</code> | <code>/sync</code> — Hot-reload <code>agents/*.md</code> &amp; <code>skills/*.md</code>."
            ), None

        if cmd == "/reload":
            from src.loader import load_agent
            from src.aistudio_sync import export_aistudio_bundle
            main_def = load_agent("main_agent.md")
            sub_def = load_agent("subagent.md")
            export_aistudio_bundle()
            if tracer:
                tracer.add_stage(
                    stage="DECLARATIVE_HOT_RELOAD",
                    status="OK",
                    input_data={"main": main_def.name, "sub": sub_def.name},
                    transformation="Hot-reloaded agents/*.md and skills/*.md from disk + refreshed AI Studio bundle",
                    output_data={"main_skills": main_def.skill_names, "sub_skills": sub_def.skill_names},
                )
            return (
                f"🔄 <b>Declarative Agents &amp; Skills Hot-Reloaded</b>\n"
                f"• <b>Main Agent:</b> <code>{html.escape(main_def.name)}</code> (<code>{html.escape(main_def.model)}</code>, skills: <code>{html.escape(str(main_def.skill_names))}</code>)\n"
                f"• <b>Sub-Agent:</b> <code>{html.escape(sub_def.name)}</code> (<code>{html.escape(sub_def.model)}</code>, skills: <code>{html.escape(str(sub_def.skill_names))}</code>)"
            ), None

        if cmd == "/sync":
            from src.loader import REPO_ROOT, load_agent
            from src.aistudio_sync import export_aistudio_bundle
            pull_out = SystemDiagnostics.run_cmd(["git", "-C", str(REPO_ROOT), "pull", "--ff-only"], timeout=15.0)
            rev_out = SystemDiagnostics.run_cmd(["git", "-C", str(REPO_ROOT), "log", "-1", "--oneline"], timeout=5.0)
            main_def = load_agent("main_agent.md")
            sub_def = load_agent("subagent.md")
            export_aistudio_bundle()
            if tracer:
                tracer.add_stage(
                    stage="GIT_REMOTE_SYNC",
                    status="OK",
                    input_data={"repo": str(REPO_ROOT)},
                    transformation=f"Executed git pull --ff-only ({pull_out[:80]}) & hot-reloaded personas",
                    output_data={"head": rev_out},
                )
            return (
                f"📦 <b>Git Sync &amp; Hot-Reload Complete</b>\n"
                f"• <b>Git Pull:</b> <code>{html.escape(pull_out)}</code>\n"
                f"• <b>HEAD Commit:</b> <code>{html.escape(rev_out)}</code>\n"
                f"• <b>Active Personas:</b> <code>{html.escape(main_def.name)}</code> &amp; <code>{html.escape(sub_def.name)}</code>"
            ), None

        if cmd == "/trace":
            count = min(max(int(parts[1]), 1), 5) if (len(parts) >= 2 and parts[1].isdigit()) else 1
            traces = self.repo.get_recent_traces(count)
            if not traces:
                return "ℹ️ No completed pipeline traces recorded yet.", None
            msg_blocks = [format_trace_summary_for_telegram(t) for t in traces]
            return "\n\n---\n\n".join(msg_blocks), None

        if cmd == "/status":
            st = self.status_evaluator.evaluate(portfolio=portfolio)
            return "", st

        if cmd in ("/fullstatus", "/sys"):
            st = self.status_evaluator.evaluate(portfolio=portfolio)
            diag = st["diagnostics"]
            focus_raw = str(portfolio["active_focus_ticker"])
            focus = resolve_focus_ticker(portfolio, prefer_held_mini=True)
            stats = portfolio["daily_stats"]
            cmds = SystemDiagnostics.run_all_diagnostic_commands()

            pos_lines: list[str] = []
            for sym, info in portfolio.get("watchlist", {}).items():
                sh = int(info.get("regular_shares_held", 0))
                mn = int(info.get("mini_units_held", 0))
                if sh > 0 or mn > 0 or sym == focus:
                    pos_lines.append(
                        f"• <b>{html.escape(sym)}:</b> Shares <code>{sh}</code> @ <code>{info['blended_avg_price']}</code> "
                        f"(Stop <code>{info['hard_stop_price']}</code> | TP <code>{info['take_profit_price']}</code>) | "
                        f"Mini <code>{mn}u</code>"
                    )

            body = (
                f"📋 <b>Full System &amp; Portfolio Report</b> (Focus: <code>{html.escape(focus_raw)}</code>)\n"
                + "\n".join(pos_lines)
                + "\n"
                + f"• <b>Tactical Cash:</b> <code>{portfolio['cash_reserves']['available_tactical_mini_sek']} SEK</code>\n"
                + f"• <b>Today ({html.escape(str(stats['date']))}):</b> <code>{stats['trades_executed_today']}</code> trades | Realized PnL: <code>{stats['realized_pnl_today_sek']} SEK</code>\n"
                + f"• <b>GPU:</b> <code>{diag['gpu_temp_c']}°C</code> | VRAM <code>{diag['gpu_vram_mb']} MB</code> | Util <code>{diag['gpu_util_pct']}%</code>\n"
                + f"• <b>Host:</b> RAM <code>{diag['ram_used_pct']}%</code> | Disk <code>{diag['disk_used_pct']}%</code> | Ollama <code>{html.escape(str(diag['ollama_service']))}</code>\n"
                + f"• <b>Uptime:</b> <code>{html.escape(str(cmds.get('uptime_load', 'N/A')))}</code>"
            )
            return body, st

        if cmd == "/ask":
            if len(parts) < 2:
                return "❓ Usage: <code>/ask &lt;your question&gt;</code>", None
            question = text.partition(" ")[2].strip()
            self.telegram.send_typing()
            return self.qa_agent.answer(question, tracer=tracer), None

        if cmd in ("/analyze", "/analyse"):
            return self._handle_analyze(parts, portfolio, tracer=tracer), None

        if cmd == "/clear":
            self.qa_agent.clear_history()
            return "🧹 <b>Cleared conversation history.</b>", None

        if cmd == "/watchlist":
            focus_raw = str(portfolio["active_focus_ticker"])
            focus_set = {normalize_ticker(x) for x in focus_raw.split(",") if x.strip()}
            lines = [f"🔭 <b>Watchlist</b> (Active Focus: <code>{html.escape(focus_raw)}</code>)"]
            for sym, info in portfolio["watchlist"].items():
                marker = "🎯" if normalize_ticker(sym) in focus_set else "•"
                lines.append(
                    f"{marker} <b>{html.escape(sym)}</b> ({html.escape(str(info['currency']))}): "
                    f"Shares <code>{info['regular_shares_held']}</code> @ <code>{info['blended_avg_price']}</code> | "
                    f"Stop <code>{info['hard_stop_price']}</code> / TP <code>{info['take_profit_price']}</code> | "
                    f"Mini <code>{info['mini_units_held']}u</code> (Fin <code>{info['mini_financing_level']}</code>, Bar <code>{info['mini_barrier']}</code>)"
                )
            return "\n".join(lines), None

        if cmd == "/focus":
            if len(parts) < 2:
                return "❓ Usage: <code>/focus &lt;TICKER[,TICKER]&gt;</code> (e.g. <code>/focus AAPL,NVDA</code>)", None
            try:
                res = self._ledger.switch_focus(" ".join(parts[1:]), tracer=tracer)
                return f"🎯 <b>Focus Ticker(s) Switched:</b> Now tracking <code>{html.escape(res['active_focus_ticker'])}</code>.", None
            except CommandInputError as exc:
                return f"⚠️ {html.escape(str(exc))}", None

        if cmd == "/add_ticker":
            if len(parts) < 5:
                return "❓ Usage: <code>/add_ticker &lt;SYM&gt; &lt;USD|SEK&gt; &lt;STOP&gt; &lt;MINI_FIN&gt; [PARITY]</code>", None
            try:
                res = self._ledger.add_ticker(
                    ticker=parts[1],
                    currency=parts[2],
                    hard_stop=parts[3],
                    financing_level=parts[4],
                    parity=parts[5] if len(parts) >= 6 else None,
                    tracer=tracer,
                )
                asset = res["asset"]
                return (
                    f"➕ <b>Added Ticker</b> <code>{html.escape(res['ticker'])}</code> "
                    f"({html.escape(str(asset['currency']))}, Stop: <code>{asset['hard_stop_price']}</code>, Mini Fin: <code>{asset['mini_financing_level']}</code>)."
                ), None
            except CommandInputError as exc:
                return f"⚠️ {html.escape(str(exc))}", None

        def _split_opt_ticker(args: list[str]) -> tuple[Optional[str], list[str]]:
            if not args:
                return None, args
            cand = normalize_ticker(args[0])
            if cand in portfolio.get("watchlist", {}):
                return cand, args[1:]
            return None, args

        if cmd == "/fill_mini":
            opt_ticker, rem = _split_opt_ticker(parts[1:])
            if len(rem) < 2:
                return (
                    "❓ Usage: <code>/fill_mini [TICKER] &lt;UNITS&gt; &lt;PRICE_SEK&gt; [LONG|SHORT]</code>\n"
                    "• <i>Example:</i> <code>/fill_mini AAPL 65 76.50 LONG</code>",
                    None,
                )
            if rem[1].upper() in ("LONG", "SHORT"):
                return (
                    "⚠️ Missing Mini unit fill price (<code>&lt;PRICE_SEK&gt;</code>). Please specify both quantity and SEK fill price.\n"
                    "• <b>Usage:</b> <code>/fill_mini [TICKER] &lt;UNITS&gt; &lt;PRICE_SEK&gt; [LONG|SHORT]</code>\n"
                    "• <i>Example:</i> <code>/fill_mini AAPL 65 76.50 LONG</code>",
                    None,
                )
            side_arg = rem[2].upper() if len(rem) >= 3 and rem[2].upper() in ("LONG", "SHORT") else "LONG"
            try:
                res = self._ledger.fill_mini(
                    ticker=opt_ticker,
                    units=rem[0],
                    price_sek=rem[1],
                    side=side_arg,
                    tracer=tracer,
                )
                return (
                    f"✅ Logged <code>{res['units']}</code> units of <code>{html.escape(res['ticker'])}</code> "
                    f"Mini ({html.escape(res['side'])}) @ <code>{res['price_sek']} SEK</code>. "
                    f"Cash left: <code>{res['new_cash_sek']} SEK</code>."
                ), None
            except CommandInputError as exc:
                return (
                    f"⚠️ {html.escape(str(exc))}\n"
                    f"• <b>Usage:</b> <code>/fill_mini [TICKER] &lt;UNITS&gt; &lt;PRICE_SEK&gt; [LONG|SHORT]</code>\n"
                    f"• <i>Example:</i> <code>/fill_mini AAPL 65 76.50 LONG</code>",
                    None,
                )

        if cmd == "/close_mini":
            opt_ticker, rem = _split_opt_ticker(parts[1:])
            if len(rem) != 1:
                return "❓ Usage: <code>/close_mini [TICKER] &lt;EXIT_SEK&gt;</code>", None
            try:
                res = self._ledger.close_mini(ticker=opt_ticker, exit_sek=rem[0], tracer=tracer)
                return (
                    f"🔒 Closed <code>{html.escape(res['ticker'])}</code> Mini @ <code>{res['exit_sek']} SEK</code> | "
                    f"Realized PnL: <code>{res['pnl_sek']} SEK</code> (Logged to Experience Memory)."
                ), None
            except CommandInputError as exc:
                return f"⚠️ {html.escape(str(exc))}", None

        if cmd == "/experience":
            raw_sym = " ".join(parts[1:]) if len(parts) >= 2 else resolve_focus_ticker(portfolio)
            target_sym = normalize_ticker(raw_sym)
            exp = self.repo.experience.get_ticker_experience(target_sym)
            rules_bullets = "\n".join(f"  • {html.escape(str(r))}" for r in exp.get("key_learned_rules", []))
            return (
                f"🧠 <b>Experience Memory Dossier (<code>{html.escape(target_sym)}</code>)</b>\n"
                f"• <b>Preferred Strategy:</b> <code>{html.escape(str(exp.get('preferred_regime_strategy')))}</code>\n"
                f"• <b>Verified Win Rate:</b> <code>{exp.get('verified_win_rate_pct')}%</code> | "
                f"<b>Profit Factor:</b> <code>{exp.get('verified_profit_factor')}</code>\n"
                f"• <b>60-Day Net Backtest PnL:</b> <code>{exp.get('net_backtest_pnl_sek'):+} SEK</code>\n"
                f"• <b>Learned Rules:</b>\n{rules_bullets}"
            ), None

        if cmd == "/fill_shares":
            opt_ticker, rem = _split_opt_ticker(parts[1:])
            if len(rem) != 2:
                return "❓ Usage: <code>/fill_shares [TICKER] &lt;QTY&gt; &lt;AVG_PRICE&gt;</code>", None
            try:
                res = self._ledger.fill_shares(ticker=opt_ticker, quantity=rem[0], avg_price=rem[1], tracer=tracer)
                return f"✅ Updated <code>{html.escape(res['ticker'])}</code> regular shares: <code>{res['quantity']}</code> @ <code>{res['avg_price']}</code>.", None
            except CommandInputError as exc:
                return f"⚠️ {html.escape(str(exc))}", None

        if cmd == "/close_shares":
            opt_ticker, rem = _split_opt_ticker(parts[1:])
            if len(rem) != 1:
                return "❓ Usage: <code>/close_shares [TICKER] &lt;EXIT_PRICE&gt;</code>", None
            try:
                res = self._ledger.close_shares(ticker=opt_ticker, exit_price=rem[0], tracer=tracer)
                return (
                    f"🔒 Closed <code>{res['quantity']}</code> <code>{html.escape(res['ticker'])}</code> shares @ "
                    f"<code>{res['exit_price']}</code> (Gross PnL: <code>{res['gross_pnl']} {html.escape(str(res['currency']))}</code>)."
                ), None
            except CommandInputError as exc:
                return f"⚠️ {html.escape(str(exc))}", None

        if cmd == "/set_stop":
            opt_ticker, rem = _split_opt_ticker(parts[1:])
            if not rem:
                return "❓ Usage: <code>/set_stop [TICKER] &lt;STOP&gt; [TARGET]</code>", None
            try:
                res = self._ledger.set_stop(
                    ticker=opt_ticker,
                    hard_stop_price=rem[0],
                    take_profit_price=rem[1] if len(rem) >= 2 else None,
                    tracer=tracer,
                )
                return f"🛡️ Updated <code>{html.escape(res['ticker'])}</code> Stop: <code>{res['hard_stop_price']}</code> | Target: <code>{res['take_profit_price']}</code>.", None
            except CommandInputError as exc:
                return f"⚠️ {html.escape(str(exc))}", None

        if cmd == "/set_mini":
            opt_ticker, rem = _split_opt_ticker(parts[1:])
            if not rem:
                return "❓ Usage: <code>/set_mini [TICKER] &lt;FIN_LVL&gt; [BARRIER] [PARITY]</code>", None
            try:
                res = self._ledger.set_mini_params(
                    ticker=opt_ticker,
                    financing_level=rem[0],
                    barrier=rem[1] if len(rem) >= 2 else None,
                    parity=rem[2] if len(rem) >= 3 else None,
                    tracer=tracer,
                )
                return (
                    f"⚙️ Updated <code>{html.escape(res['ticker'])}</code> Mini parameters: Fin <code>{res['mini_financing_level']}</code>, "
                    f"Barrier <code>{res['mini_barrier']}</code>, Parity <code>{res['mini_parity']}</code>."
                ), None
            except CommandInputError as exc:
                return f"⚠️ {html.escape(str(exc))}", None

        return "❓ Unknown command. Send <code>/help</code> to view all commands, or type any question directly.", None

    def _persist_update_offset(self, update_id: int) -> None:
        if hasattr(self.repo, "update_runtime_state"):
            def _mut(state: dict[str, Any]) -> None:
                if update_id > int(state.get("last_tg_update_id", 0) or 0):
                    state["last_tg_update_id"] = int(update_id)

            self.repo.update_runtime_state(_mut)

    def poll_and_handle(self, last_update_id: int) -> int:
        updates = self.telegram.fetch_updates(last_update_id)
        for upd in updates:
            last_update_id = int(upd["update_id"])
            self._persist_update_offset(last_update_id)

            msg = upd.get("message", {})
            incoming_chat_id = str(msg.get("chat", {}).get("id", ""))
            if not incoming_chat_id:
                continue

            text = msg.get("text", "").strip()
            tracer = PipelineTracer(
                flow="telegram_interaction",
                source=f"telegram_chat_{incoming_chat_id}",
                prefix=f"tg-{last_update_id}",
                trace_file_path=self.config.pipeline_trace_path,
            )
            tracer.add_stage(
                stage="INGEST_ARRIVAL",
                status="OK",
                input_data={"update_id": last_update_id, "chat_id": incoming_chat_id, "text": text},
                transformation="Received Telegram message via getUpdates long-poll",
                output_data={"authorized_chat": self.config.telegram_chat_id},
            )

            if not self.config.telegram_chat_id or not self.config.telegram_chat_id.lstrip("-").isdigit():
                self.telegram.persist_env_chat_id(incoming_chat_id)
                self.telegram.send_message(
                    f"🔗 <b>Telegram Linked!</b> Bound your numeric Chat ID (<code>{html.escape(incoming_chat_id)}</code>).\n"
                    f"Send <code>/help</code> to see commands or type any question directly.",
                    tracer=tracer,
                )
            elif incoming_chat_id != str(self.config.telegram_chat_id):
                tracer.add_stage(
                    stage="AUTH_GATE",
                    status="REJECTED",
                    input_data={"incoming_chat_id": incoming_chat_id},
                    transformation="Ignored unauthorized Telegram chat_id",
                    output_data={"expected": self.config.telegram_chat_id},
                )
                tracer.complete("REJECTED_UNAUTHORIZED")
                continue

            if not text and "document" not in msg:
                tracer.complete("IGNORED_EMPTY")
                continue

            if "document" in msg:
                doc = msg["document"]
                raw_fname = str(doc.get("file_name", ""))
                safe_fname = Path(raw_fname).name
                if safe_fname == raw_fname and safe_fname.endswith(".md") and not safe_fname.startswith("."):
                    from src.loader import DEFAULT_AGENTS_DIR, DEFAULT_SKILLS_DIR, parse_yaml_frontmatter
                    from src.aistudio_sync import export_aistudio_bundle
                    content = self.telegram.download_file_text(doc["file_id"])
                    meta, body = parse_yaml_frontmatter(content)
                    target_dir = DEFAULT_AGENTS_DIR if ("agent" in safe_fname or "role" in meta) else DEFAULT_SKILLS_DIR
                    target_path = (target_dir / safe_fname).resolve()
                    if target_path.parent != target_dir.resolve():
                        self.telegram.send_message("⚠️ Rejected unsafe file upload path.", tracer=tracer)
                        tracer.complete("REJECTED_UPLOAD_PATH")
                        continue
                    target_path.write_text(content, encoding="utf-8")
                    export_aistudio_bundle()
                    tracer.add_stage(
                        stage="DECLARATIVE_FILE_UPLOAD_PUSH",
                        status="OK",
                        input_data={"file_name": safe_fname, "bytes": len(content)},
                        transformation=f"Saved uploaded declarative Markdown to {target_path} & refreshed AI Studio bundle",
                        output_data={"metadata": meta, "body_chars": len(body)},
                    )
                    self.telegram.send_message(
                        f"📥 <b>Declarative File Pushed to Laptop</b>\n"
                        f"• <b>Saved to:</b> <code>{html.escape(str(target_path))}</code> (<code>{len(content)} bytes</code>)\n"
                        f"• <b>Status:</b> Hot-reloaded and active immediately.",
                        tracer=tracer,
                    )
                    tracer.complete("COMPLETED")
                    continue
                if raw_fname and safe_fname != raw_fname:
                    self.telegram.send_message("⚠️ Rejected unsafe file upload path.", tracer=tracer)
                    tracer.complete("REJECTED_UPLOAD_PATH")
                    continue
                if not text:
                    tracer.complete("IGNORED_UNSUPPORTED_DOCUMENT")
                    continue

            try:
                reply_body, st_override = self.execute_text_command(text, tracer=tracer)
                self.telegram.send_message(reply_body, include_ticker=True, status_info=st_override, tracer=tracer)
                tracer.complete("COMPLETED")
            except Exception as exc:
                tracer.add_stage(
                    stage="COMMAND_ERROR",
                    status="ERROR",
                    input_data={"text": text},
                    transformation=f"Handled exception without crashing daemon: {type(exc).__name__}: {exc}",
                    output_data={},
                )
                tracer.complete("ERROR_HANDLED")
                self.telegram.send_message(
                    f"⚠️ Command error (<code>{html.escape(type(exc).__name__)}</code>): "
                    f"<code>{html.escape(str(exc)[:180])}</code>\nSend <code>/help</code> to view valid syntax.",
                    tracer=tracer,
                )

        return last_update_id
