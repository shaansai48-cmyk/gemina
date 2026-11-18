"""
Google Apps Script Webhook Poller & Multi-Ticker Bar Extractor (Infrastructure Adapter).
"""
from typing import Any
import requests


class AppsScriptClient:
    """Polls Google Apps Script doGet() endpoint and normalizes single or multi-ticker bar buffers."""

    def __init__(self, url: str):
        self.url = url
        self.last_ok: bool = True
        self.last_error: str = ""
        self._warned_missing_doget: bool = False

    @staticmethod
    def extract_bars_from_response(r: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        bars: list[tuple[str, dict[str, Any]]] = []
        if not isinstance(r, dict):
            return bars

        if "bars" in r and isinstance(r["bars"], dict):
            for _, entry in r["bars"].items():
                if isinstance(entry, dict) and entry.get("timestamp") and isinstance(entry.get("payload"), dict):
                    bars.append((str(entry["timestamp"]), entry["payload"]))
        elif r.get("timestamp") and isinstance(r.get("payload"), dict):
            bars.append((str(r["timestamp"]), r["payload"]))

        return bars

    def poll_bars(self) -> list[tuple[str, dict[str, Any]]]:
        try:
            resp = requests.get(self.url, timeout=5)
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type and "doGet" in resp.text:
                self.last_ok = False
                self.last_error = "Missing doGet in Google Apps Script"
                if not self._warned_missing_doget:
                    print(
                        "[Apps Script Warning] Endpoint returned 'Det gick inte att hitta skriptfunktionen: doGet'. "
                        "Add doGet(e) to your Google Apps Script and deploy a New Version."
                    )
                    self._warned_missing_doget = True
                return []

            self._warned_missing_doget = False
            self.last_ok = True
            self.last_error = ""
            return self.extract_bars_from_response(resp.json())
        except Exception as err:
            self.last_ok = False
            self.last_error = str(err)
            print(f"[Poll Error] {err}")
            return []
