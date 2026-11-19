"""
Telegram Command & Free-Form Q&A Router (Application Use-Case Layer).
"""
import time
from typing import Any, Optional
from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.tracing import PipelineTracer
from traderagent.agents.qa_assistant import ConversationalQAAgent
from traderagent.config import TraderConfig
from traderagent.infrastructure.persistence import StateRepository
from traderagent.infrastructure.system_status import StatusEvaluator
from traderagent.infrastructure.telegram_adapter import TelegramGateway


def format_trace_summary_for_telegram(trace: dict[str, Any]) -> str:
    lines = [
        f"🔍 *Pipeline Trace `{trace['trace_id']}`*",
        f"• *Flow:* `{trace['flow']}` ({trace['source']})",
        f"• *Arrival:* `{trace['arrival_ts']}`",
        f"• *Leaving:* `{trace['leaving_ts']}`",
        f"• *Total Latency:* `{trace['total_duration_ms']} ms` | *Status:* `{trace['final_status']}`",
        "*Stages (Arrival -> Multi-Agent Loop -> Leaving):*",
    ]
    for s in trace.get("stages", []):
        lines.append(
            f"`{s['seq']}.` *{s['stage']}* (`{s['duration_ms']}ms`, `{s['status']}`)\n"
            f"   ↳ _{s['transformation']}_"
        )
    return "\n".join(lines)


class TelegramCommandRouter:
    """Dispatches Telegram slash commands and general Gemini 4 Argon questions."""

    def __init__(
        self,
        config: TraderConfig,
        repo: StateRepository,
        status_evaluator: StatusEvaluator,
        telegram: TelegramGateway,
        qa_agent: ConversationalQAAgent,
    ):
        self.config = config
        self.repo = repo
        self.status_evaluator = status_evaluator
        self.telegram = telegram
        self.qa_agent = qa_agent

    def execute_text_command(self, text: str, tracer: Optional[PipelineTracer] = None) -> tuple[str, Optional[dict[str, Any]]]:
        """
        Executes a command or free-form question and returns (response_body, optional_status_override).
        Used both by Telegram polling and by the Gemina SubAgentPlugin interface.
        """
        text = text.strip()
        if not text.startswith("/"):
            if tracer:
                tracer.add_stage(
                    stage="ROUTE_PARSE",
                    status="OK",
                    input_data={"text": text},
                    transformation="Routed plain-text message to Gemini 4 Argon Q&A engine",
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
                "🤖 *Gemina TraderAgent & Multi-Agent Gemini Assistant*\n\n"
                "*Status, Diagnostics & Pipeline Tracing:*\n"
                "• `/status` — 1-line status ticker (auto-expands ONLY if status is 🔴 RED).\n"
                "• `/fullstatus` — Force full expanded portfolio + hardware/command report.\n"
                "• `/trace [N]` — View arrival-to-leaving multi-agent pipeline trace of last `N` messages.\n"
                "• `/watchlist` — List all tracked tickers, stops & Mini levels.\n\n"
                "*Remote Sync & Declarative Agent/Skill Push:*\n"
                "• `/sync` — Pull latest Git commits (`git pull --ff-only`), run tests & hot-reload agents/skills.\n"
                "• `/reload` — Hot-reload `agents/*.md` and `skills/*.md` from laptop disk.\n"
                "• *Upload `.md` file* — Attach `main_agent.md`, `subagent.md`, or any skill `.md` file in chat to update it live on the laptop.\n\n"
                "*General AI Q&A (Google Search + Live System Commands):*\n"
                "• Type *any message* (no `/` needed) or `/ask <question>` to ask Gemini 4 Argon anything.\n"
                "• `/analyze [TICKER]` — Immediate deep quant & live-news brief.\n"
                "• `/clear` — Clear conversation memory.\n\n"
                "*Trading & Watchlist Controls:*\n"
                "• `/focus <TICKER>` — Switch focus ticker (`NVDA`, `ERIC B`, `MSFT`).\n"
                "• `/add_ticker <SYM> <USD|SEK> <STOP> <MINI_FIN> [PARITY]`\n"
                "• `/fill_mini <UNITS> <PRICE_SEK>` | `/close_mini <EXIT_SEK>`\n"
                "• `/fill_shares <QTY> <AVG_PRICE>` | `/close_shares <EXIT_PRICE>`\n"
                "• `/set_stop <STOP> [TARGET]` | `/set_mini <FIN_LVL> [BARRIER] [PARITY]`"
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
                f"🔄 *Declarative Agents & Skills Hot-Reloaded!*\n"
                f"• *Main Agent:* `{main_def.name}` (`{main_def.model}`, `{len(main_def.compile_system_prompt())} chars`, skills: `{main_def.skill_names}`)\n"
                f"• *Sub-Agent:* `{sub_def.name}` (`{sub_def.model}`, `{len(sub_def.compile_system_prompt())} chars`, skills: `{sub_def.skill_names}`)"
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
                f"📦 *Git Sync & Hot-Reload Complete*\n"
                f"• *Git Pull:* `{pull_out}`\n"
                f"• *HEAD Commit:* `{rev_out}`\n"
                f"• *Active Personas:* `{main_def.name}` & `{sub_def.name}` (`{sub_def.model}`)"
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
            focus = portfolio["active_focus_ticker"]
            asset = portfolio["watchlist"][focus]
            stats = portfolio["daily_stats"]
            cmds = SystemDiagnostics.run_all_diagnostic_commands()
            body = (
                f"📋 *Full System & Portfolio Report* (Focus: `{focus}`)\n"
                f"• *Shares:* `{asset['regular_shares_held']}` @ `{asset['blended_avg_price']}` (Stop: `{asset['hard_stop_price']}` | TP: `{asset['take_profit_price']}`)\n"
                f"• *Mini:* `{asset['mini_units_held']}`u `{asset['mini_instrument']}` (Fin: `{asset['mini_financing_level']}` | Bar: `{asset['mini_barrier']}`)\n"
                f"• *Tactical Cash:* `{portfolio['cash_reserves']['available_tactical_mini_sek']} SEK`\n"
                f"• *Today ({stats['date']}):* `{stats['trades_executed_today']}` trades | Realized PnL: `{stats['realized_pnl_today_sek']} SEK`\n"
                f"• *GPU:* `{diag['gpu_temp_c']}°C` | VRAM `{diag['gpu_vram_mb']} MB` | Util `{diag['gpu_util_pct']}%`\n"
                f"• *Host:* RAM `{diag['ram_used_pct']}%` | Disk `{diag['disk_used_pct']}%` | Ollama `{diag['ollama_service']}`\n"
                f"• *Uptime:* `{cmds.get('uptime_load', 'N/A')}`"
            )
            return body, st

        if cmd == "/ask" and len(parts) >= 2:
            question = text.partition(" ")[2].strip()
            self.telegram.send_typing()
            return self.qa_agent.answer(question, tracer=tracer), None

        if cmd == "/analyze":
            target_ticker = " ".join(parts[1:]).upper().replace("_", " ") if len(parts) >= 2 else portfolio["active_focus_ticker"]
            q = (
                f"Perform a comprehensive live quantitative and catalyst analysis on {target_ticker}. "
                f"Check live price action, recent news/catalysts, USD/SEK if applicable, and evaluate how it "
                f"relates to our portfolio watchlist levels and Avanza Mini Future parameters."
            )
            self.telegram.send_typing()
            return self.qa_agent.answer(q, tracer=tracer, use_subagent_persona=True), None

        if cmd == "/clear":
            self.qa_agent.clear_history()
            return "🧹 *Cleared conversation history.*", None

        if cmd == "/watchlist":
            focus = portfolio["active_focus_ticker"]
            lines = [f"🔭 *Watchlist* (Active Focus: `{focus}`)"]
            for sym, info in portfolio["watchlist"].items():
                marker = "🎯" if sym == focus else "•"
                lines.append(
                    f"{marker} *{sym}* ({info['currency']}): "
                    f"Shares `{info['regular_shares_held']}` @ `{info['blended_avg_price']}` | "
                    f"Stop `{info['hard_stop_price']}` / TP `{info['take_profit_price']}` | "
                    f"Mini `{info['mini_units_held']}`u (Fin `{info['mini_financing_level']}`, Bar `{info['mini_barrier']}`)"
                )
            return "\n".join(lines), None

        if cmd == "/focus" and len(parts) >= 2:
            alias_map = {
                "NVIDIA": "NVDA",
                "TESLA": "TSLA",
                "MICROSOFT": "MSFT",
                "APPLE": "AAPL",
                "GOOGLE": "GOOGL",
                "ALPHABET": "GOOGL",
                "AMAZON": "AMZN",
                "ERICSSON": "ERIC B",
                "ERIC": "ERIC B",
            }
            old_focus = portfolio["active_focus_ticker"]
            raw_arg = " ".join(parts[1:]).upper().replace("_", " ")
            tickers = [alias_map.get(t.strip(), t.strip()) for t in raw_arg.split(",") if t.strip()]
            for t_sym in tickers:
                if t_sym not in portfolio["watchlist"]:
                    portfolio["watchlist"][t_sym] = {
                        "exchange": "NASDAQ",
                        "currency": "USD",
                        "regular_shares_held": 0,
                        "blended_avg_price": 0.0,
                        "hard_stop_price": 0.0,
                        "take_profit_price": 0.0,
                        "mini_instrument": f"MINI L {t_sym} AVA",
                        "mini_financing_level": 0.0,
                        "mini_barrier": 0.0,
                        "mini_parity": 10.0,
                        "mini_units_held": 0,
                        "mini_avg_sek": 0.0,
                    }
            new_ticker = ",".join(tickers)
            portfolio["active_focus_ticker"] = new_ticker
            self.repo.save_portfolio(portfolio)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"active_focus_ticker": old_focus},
                    transformation=f"Switched active_focus_ticker {old_focus} -> {new_ticker}",
                    output_data={"active_focus_ticker": new_ticker},
                )
            return f"🎯 *Focus Ticker(s) Switched:* Now tracking `{new_ticker}`.", None

        if cmd == "/add_ticker" and len(parts) >= 5:
            new_ticker = parts[1].upper().replace("_", " ")
            currency = parts[2].upper()
            hard_stop = float(parts[3].replace(",", "."))
            fin_level = float(parts[4].replace(",", "."))
            parity = float(parts[5].replace(",", ".")) if len(parts) >= 6 else (1.0 if currency == "SEK" else 10.0)
            portfolio["watchlist"][new_ticker] = {
                "exchange": "OMXSTO" if currency == "SEK" else "NASDAQ",
                "currency": currency,
                "regular_shares_held": 0,
                "blended_avg_price": 0.0,
                "hard_stop_price": hard_stop,
                "take_profit_price": 0.0,
                "mini_instrument": f"MINI L {new_ticker} AVA",
                "mini_financing_level": fin_level,
                "mini_barrier": round(fin_level * 1.08, 2),
                "mini_parity": parity,
                "mini_units_held": 0,
                "mini_avg_sek": 0.0,
            }
            self.repo.save_portfolio(portfolio)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"symbol": new_ticker},
                    transformation=f"Added {new_ticker} ({currency}) to portfolio watchlist",
                    output_data=portfolio["watchlist"][new_ticker],
                )
            return f"➕ *Added Ticker `{new_ticker}`* ({currency}, Stop: `{hard_stop}`, Mini Fin: `{fin_level}`).", None

        if cmd == "/fill_mini" and len(parts) == 3:
            units, price_sek = int(parts[1]), float(parts[2].replace(",", "."))
            focus = portfolio["active_focus_ticker"]
            old_cash = portfolio["cash_reserves"]["available_tactical_mini_sek"]
            portfolio["watchlist"][focus]["mini_units_held"] = units
            portfolio["watchlist"][focus]["mini_avg_sek"] = price_sek
            portfolio["cash_reserves"]["available_tactical_mini_sek"] = round(old_cash - (units * price_sek), 2)
            portfolio["daily_stats"]["trades_executed_today"] += 1
            self.repo.save_portfolio(portfolio)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"cash_sek": old_cash, "mini_units": 0},
                    transformation=f"Logged Mini buy: {units}u {focus} @ {price_sek} SEK",
                    output_data={"cash_sek": portfolio["cash_reserves"]["available_tactical_mini_sek"], "mini_units": units},
                )
            return f"✅ Logged `{units}` units of `{focus}` Mini @ `{price_sek} SEK`.", None

        if cmd == "/close_mini" and len(parts) == 2:
            exit_sek = float(parts[1].replace(",", "."))
            focus = portfolio["active_focus_ticker"]
            asset = portfolio["watchlist"][focus]
            units, entry_sek = asset["mini_units_held"], asset["mini_avg_sek"]
            pnl = round((exit_sek - entry_sek) * units, 2)
            portfolio["cash_reserves"]["available_tactical_mini_sek"] = round(
                portfolio["cash_reserves"]["available_tactical_mini_sek"] + (units * exit_sek), 2
            )
            portfolio["daily_stats"]["realized_pnl_today_sek"] = round(
                portfolio["daily_stats"]["realized_pnl_today_sek"] + pnl, 2
            )
            if pnl < 0:
                portfolio["daily_stats"]["last_stopout_epoch"] = int(time.time())
            asset["mini_units_held"] = 0
            asset["mini_avg_sek"] = 0.0
            self.repo.save_portfolio(portfolio)
            self.repo.experience.record_closed_trade(focus, "LIVE_AVANZA_MINI", pnl)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"mini_units": units, "entry_sek": entry_sek},
                    transformation=f"Closed {focus} Mini @ {exit_sek} SEK (PnL: {pnl} SEK) & updated experience_memory.json",
                    output_data={"realized_pnl_today_sek": portfolio["daily_stats"]["realized_pnl_today_sek"]},
                )
            return f"🔒 Closed `{focus}` Mini @ `{exit_sek} SEK` | Realized PnL: `{pnl} SEK` (Logged to Experience Memory).", None

        if cmd == "/experience":
            target_sym = " ".join(parts[1:]).upper().replace("_", " ") if len(parts) >= 2 else portfolio["active_focus_ticker"]
            exp = self.repo.experience.get_ticker_experience(target_sym)
            rules_bullets = "\n".join(f"  • {r}" for r in exp.get("key_learned_rules", []))
            return (
                f"🧠 *Experience Memory Dossier (`{target_sym}`)*\n"
                f"• *Preferred Strategy:* `{exp.get('preferred_regime_strategy')}`\n"
                f"• *Verified Win Rate:* `{exp.get('verified_win_rate_pct')}%` | *Profit Factor:* `{exp.get('verified_profit_factor')}`\n"
                f"• *22-Day Net Backtest PnL:* `{exp.get('net_backtest_pnl_sek'):+} SEK`\n"
                f"• *Learned Rules:*\n{rules_bullets}"
            ), None

        if cmd == "/fill_shares" and len(parts) == 3:
            qty, price = int(parts[1]), float(parts[2].replace(",", "."))
            focus = portfolio["active_focus_ticker"]
            portfolio["watchlist"][focus]["regular_shares_held"] = qty
            portfolio["watchlist"][focus]["blended_avg_price"] = price
            self.repo.save_portfolio(portfolio)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"ticker": focus},
                    transformation=f"Set regular_shares_held={qty} @ {price}",
                    output_data={"regular_shares_held": qty, "blended_avg_price": price},
                )
            return f"✅ Updated `{focus}` regular shares: `{qty}` @ `{price}`.", None

        if cmd == "/close_shares" and len(parts) == 2:
            exit_price = float(parts[1].replace(",", "."))
            focus = portfolio["active_focus_ticker"]
            asset = portfolio["watchlist"][focus]
            qty, avg = asset["regular_shares_held"], asset["blended_avg_price"]
            pnl_native = round((exit_price - avg) * qty, 2)
            asset["regular_shares_held"] = 0
            asset["blended_avg_price"] = 0.0
            self.repo.save_portfolio(portfolio)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"qty": qty, "avg": avg},
                    transformation=f"Closed {qty} shares of {focus} @ {exit_price}",
                    output_data={"gross_pnl": pnl_native},
                )
            return f"🔒 Closed `{qty}` `{focus}` shares @ `{exit_price}` (Gross PnL: `{pnl_native} {asset['currency']}`).", None

        if cmd == "/set_stop" and len(parts) >= 2:
            focus = portfolio["active_focus_ticker"]
            old_stop = portfolio["watchlist"][focus]["hard_stop_price"]
            stop_val = float(parts[1].replace(",", "."))
            portfolio["watchlist"][focus]["hard_stop_price"] = stop_val
            if len(parts) >= 3:
                portfolio["watchlist"][focus]["take_profit_price"] = float(parts[2].replace(",", "."))
            self.repo.save_portfolio(portfolio)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"hard_stop_price": old_stop},
                    transformation=f"Updated {focus} stop {old_stop} -> {stop_val}",
                    output_data={"hard_stop_price": stop_val, "take_profit_price": portfolio['watchlist'][focus]['take_profit_price']},
                )
            return f"🛡️ Updated `{focus}` Stop: `{stop_val}` | Target: `{portfolio['watchlist'][focus]['take_profit_price']}`.", None

        if cmd == "/set_mini" and len(parts) >= 2:
            focus = portfolio["active_focus_ticker"]
            fin_val = float(parts[1].replace(",", "."))
            portfolio["watchlist"][focus]["mini_financing_level"] = fin_val
            if len(parts) >= 3:
                portfolio["watchlist"][focus]["mini_barrier"] = float(parts[2].replace(",", "."))
            if len(parts) >= 4:
                portfolio["watchlist"][focus]["mini_parity"] = float(parts[3].replace(",", "."))
            self.repo.save_portfolio(portfolio)
            if tracer:
                tracer.add_stage(
                    stage="LEDGER_STATE_TRANSFORM",
                    status="OK",
                    input_data={"ticker": focus},
                    transformation=f"Updated Mini params for {focus}",
                    output_data={"fin": fin_val, "barrier": portfolio["watchlist"][focus]["mini_barrier"]},
                )
            return (
                f"⚙️ Updated `{focus}` Mini parameters: Fin `{fin_val}`, "
                f"Barrier `{portfolio['watchlist'][focus]['mini_barrier']}`, "
                f"Parity `{portfolio['watchlist'][focus]['mini_parity']}`."
            ), None

        return "❓ Unknown command. Send `/help` to view all commands, or just type your question without a `/`.", None

    def poll_and_handle(self, last_update_id: int) -> int:
        updates = self.telegram.fetch_updates(last_update_id)
        for upd in updates:
            last_update_id = upd["update_id"]
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
                    f"🔗 *Telegram Linked!* Bound your numeric Chat ID (`{incoming_chat_id}`).\n"
                    f"Send `/help` to see commands or type any question directly.",
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

            # Support direct upload of updated declarative .md agent/skill files via Telegram chat
            if "document" in msg:
                doc = msg["document"]
                fname = doc.get("file_name", "")
                if fname.endswith(".md"):
                    from src.loader import DEFAULT_AGENTS_DIR, DEFAULT_SKILLS_DIR, parse_yaml_frontmatter
                    from src.aistudio_sync import export_aistudio_bundle
                    content = self.telegram.download_file_text(doc["file_id"])
                    meta, body = parse_yaml_frontmatter(content)
                    target_dir = DEFAULT_AGENTS_DIR if ("agent" in fname or "role" in meta) else DEFAULT_SKILLS_DIR
                    target_path = target_dir / fname
                    target_path.write_text(content, encoding="utf-8")
                    export_aistudio_bundle()
                    tracer.add_stage(
                        stage="DECLARATIVE_FILE_UPLOAD_PUSH",
                        status="OK",
                        input_data={"file_name": fname, "bytes": len(content)},
                        transformation=f"Saved uploaded declarative Markdown to {target_path} & refreshed AI Studio bundle",
                        output_data={"metadata": meta, "body_chars": len(body)},
                    )
                    self.telegram.send_message(
                        f"📥 *Declarative File Pushed to Laptop!*\n"
                        f"• *Saved to:* `{target_path}` (`{len(content)} bytes`)\n"
                        f"• *Frontmatter:* `{meta}`\n"
                        f"• *Status:* Hot-reloaded and active immediately.",
                        tracer=tracer,
                    )
                    tracer.complete("COMPLETED")
                    continue

            reply_body, st_override = self.execute_text_command(text, tracer=tracer)
            self.telegram.send_message(reply_body, include_ticker=True, status_info=st_override, tracer=tracer)
            tracer.complete("COMPLETED")

        return last_update_id
