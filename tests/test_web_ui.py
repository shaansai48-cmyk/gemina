"""
End-to-End Integration, Auth, Security & SSE Tests for the Local Web Command Center (`tests/test_web_ui.py`).
All state is isolated inside TemporaryDirectory; no production data or .env files are modified.
"""
import copy
import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gemina.core.diagnostics import SystemDiagnostics
from gemina.core.tracing import PipelineTracer
from src.web.concurrency import WebRuntimeBridge
from src.web.security import resolve_safe_declarative_path, validate_ui_bind_security
from src.web.server import WebCommandCenterServer
from src.web.sse import SSEBroker
from traderagent.config import SUBPROJECT_ROOT, TraderConfig
from traderagent.domain.models import TradeDecision

DATA_DIR = SUBPROJECT_ROOT / "data"


def _make_isolated_cfg(tmp_dir: Path, *, ui_token: str = "secret-test-token") -> TraderConfig:
    shutil.copy2(DATA_DIR / "portfolio.json", tmp_dir / "portfolio.json")
    shutil.copy2(DATA_DIR / "risk_profile.json", tmp_dir / "risk_profile.json")
    shutil.copy2(DATA_DIR / "experience_memory.json", tmp_dir / "experience_memory.json")
    return TraderConfig(
        gemini_api_key="SUPER_SECRET_GEMINI_KEY",
        gemini_model="gemini-4-argon",
        ollama_model="qwen3:14b",
        telegram_bot_token="999:SUPER_SECRET_BOT_TOKEN",
        telegram_chat_id="123456789",
        apps_script_url="https://script.google.com/macros/s/SECRET_DEPLOYMENT/exec",
        portfolio_path=str(tmp_dir / "portfolio.json"),
        risk_path=str(tmp_dir / "risk_profile.json"),
        audit_log_path=str(tmp_dir / "trade_audit.jsonl"),
        pipeline_trace_path=str(tmp_dir / "pipeline_trace.jsonl"),
        experience_memory_path=str(tmp_dir / "experience_memory.json"),
        env_file_path=str(tmp_dir / ".env"),
        runtime_state_path=str(tmp_dir / "runtime_state.json"),
        ui_host="127.0.0.1",
        ui_port=0,
        ui_token=ui_token,
    )


class TestWebCommandCenter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.cfg = _make_isolated_cfg(self.tmp_dir)

        self.mock_market = MagicMock()
        self.mock_market.fetch_usd_sek.return_value = (10.25, "yahoo_live")
        self.sample_tel = {
            "ticker": "NVDA",
            "time": "2026-11-20T15:00:00Z",
            "data_timestamp_cet": "2026-11-20 16:00 CET",
            "market_open": True,
            "open": 149.0,
            "high": 152.5,
            "low": 148.5,
            "close": 151.8,
            "ema4": 150.9,
            "ema50": 149.5,
            "vwap": 150.2,
            "or_high": 151.2,
            "or_low": 148.8,
            "pd_low": 147.9,
            "pd_high": 153.5,
            "atr14": 2.1,
            "rsi14": 54.0,
            "rvol": 1.25,
            "efficiency_ratio": 0.36,
            "usd_sek": 10.25,
            "day_change_pct": 1.15,
            "recent_4_bars": [],
        }
        self.mock_market.fetch_15m_telemetry.return_value = copy.deepcopy(self.sample_tel)
        self.mock_market.fetch_chart_series.return_value = {
            "ticker": "NVDA",
            "yahoo_symbol": "NVDA",
            "currency": "USD",
            "source": "yahoo_live",
            "market_open": True,
            "data_timestamp_cet": "2026-11-20 16:00 CET",
            "usd_sek": 10.25,
            "bars": [
                {
                    "ts": 1795186800,
                    "time": "2026-11-20T15:00:00Z",
                    "open": 149.0,
                    "high": 152.5,
                    "low": 148.5,
                    "close": 151.8,
                    "volume": 1200000,
                    "ema4": 150.9,
                    "ema50": 149.5,
                    "vwap": 150.2,
                    "or_high": 151.2,
                    "or_low": 148.8,
                    "pd_low": 147.9,
                    "pd_high": 153.5,
                    "atr14": 2.1,
                    "rsi14": 54.0,
                    "rvol": 1.25,
                    "efficiency_ratio": 0.36,
                }
            ],
            "latest_telemetry": copy.deepcopy(self.sample_tel),
        }

        self.mock_apps = MagicMock()
        self.mock_apps.last_ok = True
        self.mock_apps.last_error = ""
        self.mock_apps.get_status.return_value = {"last_ok": True, "last_error": "", "url_configured": True}

        self.mock_telegram = MagicMock()
        self.mock_committee = MagicMock()
        self.mock_committee.evaluate_live_snapshot.return_value = TradeDecision(
            ticker="NVDA",
            market_regime="Momentum_Breakout",
            action="HOLD",
            instrument="NVDA",
            quantity=0,
            limit_price=None,
            avanza_stop_trigger_sek=None,
            avanza_stop_sell_sek=None,
            underlying_stop_price=148.5,
            take_profit_target=158.0,
            net_risk_reward_ratio=0.0,
            overnight_gap_warning=False,
            rationale="Waiting for confirmation.",
            plain_reasons=["Price is near resistance.", "Volume is steady."],
            confidence_score=65,
            critic_verdict="APPROVED",
            rr_applicable=False,
            data_timestamp_cet="2026-11-20 16:00 CET",
            market_open=True,
        )

        self.bridge = WebRuntimeBridge(
            config=self.cfg,
            market_data=self.mock_market,
            apps_script=self.mock_apps,
            telegram=self.mock_telegram,
            committee=self.mock_committee,
        )
        self.broker = SSEBroker(keepalive_sec=1.0)
        self.server = WebCommandCenterServer(
            config=self.cfg,
            host="127.0.0.1",
            port=0,
            token="secret-test-token",
            enable_tail=False,
            bridge=self.bridge,
            sse_broker=self.broker,
        )
        self.server.start_in_thread()

    def tearDown(self):
        self.server.shutdown()
        self._tmp.cleanup()

    def _req(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str | None = "secret-test-token",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], dict | str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5.0)
        req_headers: dict[str, str] = {}
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        raw_body = b""
        if body is not None:
            req_headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body).encode("utf-8")
        if headers:
            req_headers.update(headers)
        conn.request(method, path, body=raw_body, headers=req_headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        resp_hdrs = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        try:
            return resp.status, resp_hdrs, json.loads(data)
        except json.JSONDecodeError:
            return resp.status, resp_hdrs, data

    def test_security_gates_auth_csrf_dns_rebinding_csp_and_redaction(self):
        # 1. Fail-closed non-loopback without token
        with self.assertRaises(RuntimeError):
            validate_ui_bind_security("0.0.0.0", "")

        # 2. Missing Bearer token -> 401
        st, hdrs, _ = self._req("GET", "/api/portfolio", token=None)
        self.assertEqual(st, 401)
        self.assertEqual(hdrs.get("cache-control"), "no-store")
        self.assertEqual(hdrs.get("x-frame-options"), "DENY")
        self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
        self.assertIn("object-src 'none'", hdrs.get("content-security-policy", ""))

        # 3. Query string ?token= rejected
        st_qs, _, body_qs = self._req("GET", "/api/portfolio?token=secret-test-token", token="secret-test-token")
        self.assertEqual(st_qs, 401)
        self.assertEqual(body_qs["error"]["code"], "QUERY_TOKEN_REJECTED")

        # 4. Wrong Bearer token -> 401
        st_bad, _, _ = self._req("GET", "/api/portfolio", token="wrong-token")
        self.assertEqual(st_bad, 401)

        # 5. DNS-rebinding Host header spoofing -> 403
        st_host, _, _ = self._req(
            "GET",
            "/api/portfolio",
            headers={"Host": "evil.attacker.example"},
        )
        self.assertEqual(st_host, 403)

        # 6. Cross-origin CSRF Origin / Sec-Fetch-Site on POST -> 403
        st_csrf, _, _ = self._req(
            "POST",
            "/api/portfolio/focus",
            body={"tickers": "NVDA"},
            headers={"Origin": "http://evil.com"},
        )
        self.assertEqual(st_csrf, 403)

        st_sec_fetch, _, _ = self._req(
            "POST",
            "/api/portfolio/focus",
            body={"tickers": "NVDA"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(st_sec_fetch, 403)

        # 7. Safe path traversal guards
        st_trav, _, _ = self._req("GET", "/static/../../etc/passwd", token=None)
        self.assertIn(st_trav, (403, 404))
        with self.assertRaises(ValueError):
            resolve_safe_declarative_path("agents", "../secret.md")

        # 8. Static index HTML & JS served cleanly
        st_idx, idx_hdrs, idx_body = self._req("GET", "/", token=None)
        self.assertEqual(st_idx, 200)
        self.assertIn("text/html", idx_hdrs.get("content-type", ""))
        self.assertIn("<title>", str(idx_body))

        st_js, js_hdrs, _ = self._req("GET", "/static/js/app.js", token=None)
        self.assertEqual(st_js, 200)
        self.assertIn("javascript", js_hdrs.get("content-type", ""))

        # 9. Secret redaction in /api/health & /api/chart (inject leaked secret into audit first)
        self.bridge.repo.append_audit_log({
            "trace_id": "secret-check-1",
            "ts": "2026-11-20T15:00:00Z",
            "ticker": "NVDA",
            "telemetry": {"close": 151.0, "secret": "TOP_SECRET_SHOULD_NOT_LEAK"},
            "decision": {"action": "HOLD"},
        })
        with patch.object(
            SystemDiagnostics,
            "collect_host_metrics",
            return_value={
                "gpu_temp_c": 40, "gpu_vram_mb": "512/16384", "gpu_util_pct": 1,
                "ram_used_pct": 25, "disk_used_pct": 30, "ollama_service": "active", "red_alerts": [],
            },
        ), patch.object(
            SystemDiagnostics,
            "run_all_diagnostic_commands",
            return_value={"services": "active\nactive", "uptime_load": "up 1h"},
        ):
            st_h, _, body_h = self._req("GET", "/api/health")
            self.assertEqual(st_h, 200)
            dump_h = json.dumps(body_h)
            self.assertNotIn("SUPER_SECRET", dump_h)
            self.assertNotIn("SECRET_DEPLOYMENT", dump_h)
            self.assertNotIn("secret-test-token", dump_h)
            self.assertIn("diagnostics", body_h)
            self.assertIn("commands", body_h)

        st_c, _, body_c = self._req("GET", "/api/chart/NVDA?range=5d&interval=15m")
        self.assertEqual(st_c, 200)
        self.assertNotIn("TOP_SECRET_SHOULD_NOT_LEAK", json.dumps(body_c))

        # 10. Chart audit-log fallback returns 200 when MarketDataError is raised
        from traderagent.infrastructure.market_data import MarketDataError
        self.mock_market.fetch_chart_series.side_effect = MarketDataError("Yahoo 429")
        try:
            st_cfb, _, body_cfb = self._req("GET", "/api/chart/NVDA?range=5d&interval=15m")
            self.assertEqual(st_cfb, 200)
            self.assertEqual(body_cfb["source"], "audit_fallback")
            self.assertGreaterEqual(len(body_cfb["bars"]), 1)
        finally:
            self.mock_market.fetch_chart_series.side_effect = None

    def test_rest_routes_calculator_chat_analyze_traces_declarative_and_429(self):
        # 1. Portfolio & Live Calculator (risk_parity & fixed budget modes)
        st_p, _, p_body = self._req("GET", "/api/portfolio")
        self.assertEqual(st_p, 200)
        self.assertTrue(p_body["ok"])
        self.assertEqual(p_body["usd_sek"], 10.25)
        self.assertIn("latest_by_ticker", p_body)
        self.assertEqual(p_body["positions"]["NVDA"]["spot"], 151.8)

        st_calc, _, calc_body = self._req(
            "GET",
            "/api/portfolio/calculator?ticker=NVDA&side=LONG&risk_parity=1",
        )
        self.assertEqual(st_calc, 200)
        self.assertGreater(calc_body["units"], 0)
        self.assertIn("trigger_sek", calc_body)
        self.assertIn("sell_sek", calc_body)
        self.assertIn("zero_courtage", calc_body)

        # 2. Portfolio mutations via LedgerService (does NOT spam Telegram when mirror_telegram=false)
        st_f, _, f_body = self._req("POST", "/api/portfolio/focus", body={"tickers": ["NVDA", "MSFT"]})
        self.assertEqual(st_f, 200)
        self.assertEqual(f_body["portfolio"]["active_focus_ticker"], "NVDA,MSFT")
        self.mock_telegram.send_message.assert_not_called()

        st_s, _, _ = self._req(
            "POST",
            "/api/portfolio/stop",
            body={"ticker": "NVDA", "hard_stop_price": 149.0, "take_profit_price": 162.0},
        )
        self.assertEqual(st_s, 200)
        self.assertEqual(self.bridge.repo.load_portfolio()["watchlist"]["NVDA"]["hard_stop_price"], 149.0)

        st_fm, _, _ = self._req(
            "POST",
            "/api/portfolio/fill-mini",
            body={"ticker": "AAPL", "units": 20, "price_sek": 50.0, "side": "LONG"},
        )
        self.assertEqual(st_fm, 200)
        st_cm, _, _ = self._req(
            "POST",
            "/api/portfolio/close-mini",
            body={"ticker": "AAPL", "exit_sek": 55.0},
        )
        self.assertEqual(st_cm, 200)

        # Validation errors -> 400
        st_bad_side, _, _ = self._req("GET", "/api/portfolio/calculator?ticker=NVDA&side=INVALID")
        self.assertEqual(st_bad_side, 400)

        # 3. Chat & Clear (isolated mock LLM)
        with patch.object(
            self.bridge.llm_router,
            "generate_grounded_text",
            return_value=("Plain English answer for NVDA.", "gemini-4-argon", "OK"),
        ):
            st_ch, _, ch_body = self._req(
                "POST",
                "/api/chat",
                body={"message": "What is my NVDA stop?", "mode": "ask", "session_id": "test_sess"},
            )
            self.assertEqual(st_ch, 200)
            self.assertIn("Plain English answer", ch_body["answer"])

        st_cl, _, cl_body = self._req("POST", "/api/chat/clear", body={"session_id": "test_sess"})
        self.assertEqual(st_cl, 200)
        self.assertTrue(cl_body["cleared"])

        # 4. /api/analyze ("depth=quick" -> "brief") & HOLD HTML guard (no /fill_mini)
        st_an, _, an_body = self._req(
            "POST",
            "/api/analyze",
            body={"ticker": "NVDA", "depth": "quick", "allow_live_search": False},
        )
        self.assertEqual(st_an, 200)
        self.assertEqual(an_body["decision"]["action"], "HOLD")
        self.assertNotIn("/fill_mini", an_body["summary_html"])
        self.assertNotIn("$$", an_body["summary_html"])
        self.assertTrue(an_body["plain_reasons"])

        # Traces endpoint includes the trace generated by /api/analyze
        st_tr, _, tr_body = self._req("GET", f"/api/traces/{an_body['trace_id']}")
        self.assertEqual(st_tr, 200)
        self.assertEqual(tr_body["trace"]["trace_id"], an_body["trace_id"])

        # Benchmarks
        st_bm, _, bm_body = self._req("GET", "/api/benchmarks")
        self.assertEqual(st_bm, 200)
        self.assertIn("experience_memory", bm_body)

        # Declarative list & reload & invalid rollback check
        st_dec, _, dec_body = self._req("GET", "/api/declarative")
        self.assertEqual(st_dec, 200)
        self.assertGreaterEqual(len(dec_body["agents"]), 2)
        self.assertIn("raw_text", dec_body["agents"][0])

        # Invalid kind path -> 400
        st_bad_kind, _, _ = self._req(
            "PUT",
            "/api/declarative/unknown/foo.md",
            body={"content": "---\nname: x\n---\nhello"},
        )
        self.assertEqual(st_bad_kind, 400)

        # Declarative update atomic rollback check (mock load_agent failing -> prior bytes intact)
        from src.loader import DEFAULT_AGENTS_DIR
        main_md = DEFAULT_AGENTS_DIR / "main_agent.md"
        orig_main_md = main_md.read_text(encoding="utf-8")
        with patch("src.web.api.load_agent", side_effect=RuntimeError("broken frontmatter")):
            st_rb, _, body_rb = self._req(
                "PUT",
                "/api/declarative/agents/main_agent.md",
                body={"content": "---\nname: broken\n---\ninvalid"},
            )
            self.assertEqual(st_rb, 400)
            self.assertEqual(body_rb["error"]["code"], "INVALID_DECLARATIVE_CONTENT")
        self.assertEqual(main_md.read_text(encoding="utf-8"), orig_main_md)

        # 5. Bounded executor queue saturation -> HTTP 429
        acquired = []
        for _ in range(self.bridge.max_inflight):
            self.assertTrue(self.bridge._analysis_semaphore.acquire(blocking=False))
            acquired.append(True)
        try:
            st_429, _, body_429 = self._req(
                "POST",
                "/api/analyze",
                body={"ticker": "NVDA", "mode": "brief"},
            )
            self.assertEqual(st_429, 429)
            self.assertEqual(body_429["error"]["code"], "ANALYSIS_BUSY")
        finally:
            for _ in acquired:
                self.bridge._analysis_semaphore.release()

    def test_sse_live_stream_and_last_event_id_replay(self):
        # Publish event before connecting and verify live + Last-Event-ID replay over chunked HTTP/1.1
        ev1 = self.broker.publish("portfolio.updated", {"ticker": "NVDA", "seq": 1})
        ev2 = self.broker.publish("portfolio.updated", {"ticker": "AAPL", "seq": 2})

        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5.0)
        conn.request(
            "GET",
            "/api/stream",
            headers={
                "Authorization": "Bearer secret-test-token",
                "Last-Event-ID": str(ev1),
            },
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))

        # Read replayed ev2 plus live ev3
        self.broker.publish("bar.processed", {"ticker": "NVDA", "close": 152.1})
        collected = ""
        deadline = time.time() + 3.0
        while time.time() < deadline and ("bar.processed" not in collected or '"AAPL"' not in collected):
            line = resp.fp.readline().decode("utf-8", errors="replace")
            if not line:
                break
            collected += line
        conn.close()

        self.assertIn(f"id: {ev2}", collected)
        self.assertNotIn(f"id: {ev1}", collected)
        self.assertIn("event: bar.processed", collected)
        self.assertIn('"AAPL"', collected)


if __name__ == "__main__":
    unittest.main()
