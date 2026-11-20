"""
Telegram Bot API Gateway (Infrastructure Adapter).
Handles message delivery with clean Telegram HTML sanitization, chunking,
multi-chunk delivery verification, atomic env persistence, and strict update/file guards.
"""
import html
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Optional
import time
import requests

from gemina.core.tracing import PipelineTracer
from traderagent.config import TraderConfig
from traderagent.infrastructure.system_status import StatusEvaluator

_VALID_TG_TAG_RE = re.compile(
    r"(<(?:/)?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote)>|<a\s+href=[\"'][^\"']+[\"']>|</a>)",
    re.IGNORECASE,
)
_STATUS_LINE_RE = re.compile(r"^\s*[🟢🔴]\s*(?:`?\[[^\n]+\]`?|\*\*[^\n]+|<b>[^\n]+)\s*(?:\n+)?")


def strip_duplicate_status_header(text: str) -> str:
    """Removes accidental status header duplicated by an LLM at the top of response body."""
    return _STATUS_LINE_RE.sub("", text or "", count=1).strip()


def clean_plain_english_text(text: str) -> str:
    """Strips raw LaTeX delimiters/macros so plain-English text renders cleanly on mobile & web."""
    s = (text or "").strip()
    s = s.replace("$\\rightarrow$", "->").replace("\\rightarrow", "->").replace("\\to", "->")
    s = s.replace("\\times", "x").replace("\\cdot", "*")
    s = re.sub(r"\$\$(.*?)\$\$", lambda m: m.group(1).strip(), s, flags=re.DOTALL)
    s = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1) / (\2)", s)
    s = re.sub(r"\\(?:text|mathbf|mathrm|left|right)\{?([^{}]*)\}?", r"\1", s)
    s = s.replace("\\lfloor", "").replace("\\rfloor", "").replace("\\quad", " ")
    return s


def sanitize_telegram_html(text: str) -> str:
    """
    Converts mixed HTML + lightweight Markdown (if LLM emitted **bold** or `code`) into
    valid Telegram HTML while escaping raw `<`, `>`, `&` outside valid tags.
    """
    raw = clean_plain_english_text(text)

    # First escape plain segments around existing Telegram HTML tags
    tokens = _VALID_TG_TAG_RE.split(raw)
    out_parts: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if _VALID_TG_TAG_RE.fullmatch(tok):
            out_parts.append(tok)
        else:
            esc = html.escape(tok, quote=False)
            # Strip fenced code markers to <pre> and convert inline **bold**, ### headings, `code`
            esc = re.sub(r"```(?:[a-zA-Z0-9_+-]*)\n?(.*?)```", r"<pre>\1</pre>", esc, flags=re.DOTALL)
            esc = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", esc)
            esc = re.sub(r"^\s*#{1,4}\s+(.+)$", r"<b>\1</b>", esc, flags=re.MULTILINE)
            esc = re.sub(r"\*\*([^\n*]+?)\*\*", r"<b>\1</b>", esc)
            esc = re.sub(r"^\s*\*\s+", "• ", esc, flags=re.MULTILINE)
            out_parts.append(esc)

    # Normalize <strong>/<em> aliases to Telegram <b>/<i> and collapse excessive blank lines
    rendered = "".join(out_parts)
    rendered = rendered.replace("<strong>", "<b>").replace("</strong>", "</b>")
    rendered = rendered.replace("<em>", "<i>").replace("</em>", "</i>")
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def chunk_telegram_html(text: str, max_len: int = 3800) -> list[str]:
    """Splits long Telegram HTML on newline boundaries cleanly without breaking open tags."""
    if len(text) <= max_len:
        return [text] if text else [""]

    chunks: list[str] = []
    cur_lines: list[str] = []
    cur_len = 0
    for line in text.splitlines(keepends=True):
        if len(line) > max_len:
            if cur_lines:
                chunks.append("".join(cur_lines).rstrip())
                cur_lines, cur_len = [], 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i : i + max_len])
            continue
        if cur_len + len(line) > max_len and cur_lines:
            chunks.append("".join(cur_lines).rstrip())
            cur_lines, cur_len = [], 0
        cur_lines.append(line)
        cur_len += len(line)

    if cur_lines:
        chunks.append("".join(cur_lines).rstrip())
    return chunks or [""]


class TelegramGateway:
    """Manages inbound long-polling and outbound delivery to Telegram."""

    def __init__(self, config: TraderConfig, status_evaluator: StatusEvaluator):
        self.config = config
        self.status_evaluator = status_evaluator
        self._env_lock = threading.Lock()

    def persist_env_chat_id(self, new_chat_id: str) -> None:
        with self._env_lock:
            self.config.telegram_chat_id = str(new_chat_id)
            env_path = self.config.env_file_path
            lines: list[str] = []
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            found = False
            new_lines: list[str] = []
            for line in lines:
                if line.startswith("TELEGRAM_CHAT_ID="):
                    new_lines.append(f'TELEGRAM_CHAT_ID="{self.config.telegram_chat_id}"\n')
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f'TELEGRAM_CHAT_ID="{self.config.telegram_chat_id}"\n')

            env_dir = os.path.dirname(os.path.abspath(env_path)) or "."
            os.makedirs(env_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=env_dir, prefix=".tmp_env_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, env_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
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
        cleaned_body = strip_duplicate_status_header(text)
        if status_info is None and include_ticker:
            status_info = self.status_evaluator.evaluate()
        raw_final = self.status_evaluator.format_with_status_ticker(cleaned_body, status_info) if include_ticker else cleaned_body
        final_text = sanitize_telegram_html(raw_final)

        if tracer and include_ticker and status_info:
            tracer.add_stage(
                stage="STATUS_TICKER_TRANSFORM",
                status="RED_EXPANDED" if status_info["is_red"] else "GREEN_COMPACT",
                input_data={"body_chars": len(cleaned_body), "is_red": status_info["is_red"]},
                transformation=(
                    f"Prepended {status_info['color']} status ticker "
                    f"({'expanded with ' + str(len(status_info['red_reasons'])) + ' alerts' if status_info['is_red'] else '1-line compact'})"
                ),
                output_data={"final_chars": len(final_text), "ticker": status_info["ticker_line"]},
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        print("\n" + final_text + "\n")
        egress_info: dict[str, Any] = {
            "sent": False,
            "chunks": 0,
            "chars": len(final_text),
            "http_status": None,
            "mode": "html",
        }
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
        chunks = chunk_telegram_html(final_text, max_len=3800)
        all_ok = True
        for chunk in chunks:
            try:
                r = requests.post(
                    url,
                    json={
                        "chat_id": self.config.telegram_chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=8,
                )
                chunk_status = r.status_code
                if chunk_status != 200:
                    print(f"[Telegram HTML Fallback] HTTP {chunk_status}: {r.text[:160]}")
                    egress_info["mode"] = "plain_fallback"
                    plain_chunk = re.sub(r"<[^>]+>", "", html.unescape(chunk))
                    r2 = requests.post(
                        url,
                        json={"chat_id": self.config.telegram_chat_id, "text": plain_chunk},
                        timeout=8,
                    )
                    chunk_status = r2.status_code
                egress_info["http_status"] = chunk_status
                egress_info["chunks"] += 1
                all_ok = all_ok and (chunk_status == 200)
            except Exception as e:
                all_ok = False
                egress_info["error"] = str(e)
                print(f"[Telegram Send Error] {e}")

        egress_info["sent"] = all_ok and egress_info["chunks"] > 0
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
            r = requests.get(url, params={"offset": last_update_id + 1, "timeout": 2}, timeout=4)
            r.raise_for_status()
            resp = r.json()
            if not isinstance(resp, dict) or not resp.get("ok"):
                print(f"[Telegram Poll Error] API error: {resp}")
                return []
            return resp.get("result", [])
        except Exception as e:
            print(f"[Telegram Poll Error] {e}")
            return []

    def download_file_text(self, file_id: str, max_bytes: int = 262_144) -> str:
        """Downloads a small text/markdown file uploaded to Telegram chat."""
        info_url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/getFile"
        info_r = requests.get(info_url, params={"file_id": file_id}, timeout=5)
        info_r.raise_for_status()
        info_resp = info_r.json()
        if not info_resp.get("ok"):
            raise RuntimeError(f"Telegram getFile failed: {info_resp}")
        file_meta = info_resp["result"]
        if int(file_meta.get("file_size", 0)) > max_bytes:
            raise ValueError(f"Uploaded file exceeds {max_bytes} bytes limit.")
        file_path = file_meta["file_path"]
        dl_url = f"https://api.telegram.org/file/bot{self.config.telegram_bot_token}/{file_path}"
        dl_r = requests.get(dl_url, timeout=10)
        dl_r.raise_for_status()
        return dl_r.text
