"""
Telegram Bot API Gateway (Infrastructure Adapter).
Handles message delivery, status ticker formatting, 4000-char chunking, Markdown-to-plain fallback, and auto-binding chat_id.
"""
import os
import time
from typing import Any, Optional
import requests
from gemina.core.tracing import PipelineTracer
from traderagent.config import TraderConfig
from traderagent.infrastructure.system_status import StatusEvaluator


class TelegramGateway:
    """Manages inbound long-polling and outbound delivery to Telegram."""

    def __init__(self, config: TraderConfig, status_evaluator: StatusEvaluator):
        self.config = config
        self.status_evaluator = status_evaluator

    def persist_env_chat_id(self, new_chat_id: str) -> None:
        self.config.telegram_chat_id = str(new_chat_id)
        env_path = self.config.env_file_path
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(env_path, "w", encoding="utf-8") as f:
                for line in lines:
                    if line.startswith("TELEGRAM_CHAT_ID="):
                        f.write(f'TELEGRAM_CHAT_ID="{self.config.telegram_chat_id}"\n')
                    else:
                        f.write(line)
        print(f"[Telegram] Auto-bound numeric TELEGRAM_CHAT_ID={self.config.telegram_chat_id}")

    def send_typing(self) -> None:
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendChatAction",
                    json={"chat_id": self.config.telegram_chat_id, "action": "typing"},
                    timeout=3,
                )
            except Exception:
                pass

    def send_message(
        self,
        text: str,
        include_ticker: bool = True,
        status_info: Optional[dict[str, Any]] = None,
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        if status_info is None and include_ticker:
            status_info = self.status_evaluator.evaluate()
        final_text = self.status_evaluator.format_with_status_ticker(text, status_info) if include_ticker else text

        if tracer and include_ticker and status_info:
            tracer.add_stage(
                stage="STATUS_TICKER_TRANSFORM",
                status="RED_EXPANDED" if status_info["is_red"] else "GREEN_COMPACT",
                input_data={"body_chars": len(text), "is_red": status_info["is_red"]},
                transformation=(
                    f"Prepended {status_info['color']} status ticker "
                    f"({'expanded with ' + str(len(status_info['red_reasons'])) + ' red alerts' if status_info['is_red'] else '1-line compact'})"
                ),
                output_data={"final_chars": len(final_text), "ticker": status_info["ticker_line"]},
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        print("\n" + final_text + "\n")
        egress_info = {"sent": False, "chunks": 0, "chars": len(final_text), "http_status": None, "mode": "markdown"}
        if not (self.config.telegram_bot_token and self.config.telegram_chat_id):
            if tracer:
                tracer.add_stage(
                    stage="EGRESS_LEAVING",
                    status="SKIPPED_NO_CREDENTIALS",
                    input_data={"chars": len(final_text)},
                    transformation="Printed to stdout only (Telegram credentials missing)",
                    output_data=egress_info,
                )
            return egress_info

        t_send = time.perf_counter()
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        chunks = [final_text[i:i + 4000] for i in range(0, max(len(final_text), 1), 4000)]
        for chunk in chunks:
            try:
                r = requests.post(
                    url,
                    json={"chat_id": self.config.telegram_chat_id, "text": chunk, "parse_mode": "Markdown"},
                    timeout=8,
                )
                egress_info["http_status"] = r.status_code
                if r.status_code != 200:
                    egress_info["mode"] = "plain_fallback"
                    r2 = requests.post(
                        url,
                        json={"chat_id": self.config.telegram_chat_id, "text": chunk},
                        timeout=8,
                    )
                    egress_info["http_status"] = r2.status_code
                egress_info["chunks"] += 1
                egress_info["sent"] = egress_info["http_status"] == 200
            except Exception as e:
                egress_info["error"] = str(e)
                print(f"[Telegram Send Error] {e}")

        if tracer:
            tracer.add_stage(
                stage="EGRESS_LEAVING",
                status="SENT" if egress_info["sent"] else "ERROR",
                input_data={"chat_id": self.config.telegram_chat_id, "chars": len(final_text)},
                transformation=f"Dispatched {egress_info['chunks']} chunk(s) to Telegram API ({egress_info['mode']})",
                output_data=egress_info,
                duration_ms=(time.perf_counter() - t_send) * 1000.0,
            )
        return egress_info

    def fetch_updates(self, last_update_id: int) -> list[dict[str, Any]]:
        if not self.config.telegram_bot_token:
            return []
        try:
            url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/getUpdates"
            resp = requests.get(url, params={"offset": last_update_id + 1, "timeout": 2}, timeout=4).json()
            return resp.get("result", [])
        except Exception as e:
            print(f"[Telegram Poll Error] {e}")
            return []
