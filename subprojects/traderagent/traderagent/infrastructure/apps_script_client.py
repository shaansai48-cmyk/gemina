"""
Google Apps Script Webhook Poller & Multi-Ticker Bar Extractor (Infrastructure Adapter).
Thread-safe status tracking, strict secret redaction on ingest, and dict/list bar normalization.
"""
import copy
import json
import math
import threading
import time
from typing import Any
import requests

_STRIPPED_KEYS = {"secret", "token", "api_key", "password", "authorization"}


class AppsScriptClient:
    """Polls Google Apps Script doGet() endpoint and normalizes single or multi-ticker bar buffers."""

    def __init__(self, url: str):
        self.url = url
        self._lock = threading.Lock()
        self.last_ok: bool = True
        self.last_error: str = ""
        self._warned_missing_doget: bool = False
        self.last_poll_epoch: float = 0.0
        self.last_nonempty_epoch: float = 0.0
        self.last_bar_ts_by_ticker: dict[str, str] = {}
        self.last_empty_poll_count: int = 0

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_ok": self.last_ok,
                "last_error": self.last_error,
                "last_poll_epoch": self.last_poll_epoch,
                "last_nonempty_epoch": self.last_nonempty_epoch,
                "last_bar_ts_by_ticker": dict(self.last_bar_ts_by_ticker),
                "last_empty_poll_count": self.last_empty_poll_count,
            }

    @staticmethod
    def _normalize_payload(raw_payload: Any, key_hint: str = "") -> dict[str, Any] | None:
        payload = raw_payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return None
        if not isinstance(payload, dict):
            return None

        cleaned = {k: v for k, v in payload.items() if str(k).lower() not in _STRIPPED_KEYS}
        raw_close = cleaned.get("close")
        try:
            close_val = float(raw_close)
            if not math.isfinite(close_val):
                return None
        except (TypeError, ValueError):
            return None

        if not str(cleaned.get("ticker", "")).strip() and key_hint and key_hint.upper() != "DEFAULT":
            cleaned["ticker"] = str(key_hint).strip().upper().replace("_", " ")
        return cleaned

    @classmethod
    def extract_bars_from_response(cls, r: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        bars: list[tuple[str, dict[str, Any]]] = []
        if not isinstance(r, dict):
            return bars

        raw_bars = r.get("bars")
        if isinstance(raw_bars, dict):
            for key, entry in raw_bars.items():
                if not isinstance(entry, dict) or not entry.get("timestamp"):
                    continue
                norm = cls._normalize_payload(entry.get("payload"), key_hint=str(key))
                if norm is not None:
                    bars.append((str(entry["timestamp"]), norm))
        elif isinstance(raw_bars, list):
            for entry in raw_bars:
                if not isinstance(entry, dict) or not entry.get("timestamp"):
                    continue
                norm = cls._normalize_payload(entry.get("payload"))
                if norm is not None:
                    bars.append((str(entry["timestamp"]), norm))
        elif r.get("timestamp"):
            norm = cls._normalize_payload(r.get("payload"))
            if norm is not None:
                bars.append((str(r["timestamp"]), norm))

        return bars

    def poll_bars(self) -> list[tuple[str, dict[str, Any]]]:
        now = time.time()
        try:
            resp = requests.get(self.url, timeout=5)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type and "doGet" in resp.text:
                with self._lock:
                    self.last_ok = False
                    self.last_error = "Missing doGet in Google Apps Script"
                    self.last_poll_epoch = now
                    warn = not self._warned_missing_doget
                    self._warned_missing_doget = True
                if warn:
                    print(
                        "[Apps Script Warning] Endpoint returned missing doGet error. "
                        "Add doGet(e) to your Google Apps Script and deploy a New Version."
                    )
                return []

            data = resp.json()
            if not isinstance(data, dict) or "error" in data or data.get("ok") is False:
                err_msg = str(data.get("error", "Invalid Apps Script JSON response")) if isinstance(data, dict) else "Invalid Apps Script payload"
                with self._lock:
                    self.last_ok = False
                    self.last_error = err_msg
                    self.last_poll_epoch = now
                return []

            bars = self.extract_bars_from_response(data)
            with self._lock:
                self._warned_missing_doget = False
                self.last_ok = True
                self.last_error = ""
                self.last_poll_epoch = now
                if bars:
                    self.last_nonempty_epoch = now
                    self.last_empty_poll_count = 0
                    for ts, payload in bars:
                        sym = str(payload.get("ticker", "")).strip().upper().replace("_", " ")
                        if sym:
                            self.last_bar_ts_by_ticker[sym] = ts
                else:
                    self.last_empty_poll_count += 1
            return bars
        except Exception as err:
            with self._lock:
                self.last_ok = False
                self.last_error = str(err)
                self.last_poll_epoch = now
            print(f"[Poll Error] {err}")
            return []
