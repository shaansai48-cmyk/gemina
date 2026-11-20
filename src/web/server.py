"""
Local HTTP Server & CLI Entrypoint for the Gemina Web Command Center (`src/web/server.py`).
Wraps ThreadingHTTPServer with security validation, HTTP/1.1 chunked SSE streaming,
safe static asset serving, and `gemina-ui --host --port --no-tail`.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from traderagent.config import TraderConfig

from src.web.api import WebApiRouter
from src.web.concurrency import WebRuntimeBridge
from src.web.schemas import api_error
from src.web.security import (
    apply_security_headers,
    has_query_token,
    is_loopback_host,
    validate_ui_bind_security,
    verify_bearer_token,
    verify_host_and_origin,
)
from src.web.sse import JsonlTailWatcher, SSEBroker

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 1_048_576  # 1 MiB cap for JSON payloads

_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

_FALLBACK_INDEX_HTML = (
    "<!doctype html>"
    "<html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Gemina Web Command Center</title></head>"
    "<body><h1>Gemina Web Command Center</h1>"
    "<p>Backend online. Static UI assets directory is empty.</p></body></html>"
)


def _build_default_allowlists(host: str, port: int) -> tuple[set[str], set[str]]:
    base_hosts: set[str] = {"127.0.0.1", "localhost", "::1"}
    clean_host = str(host or "").strip().strip("[]")
    if clean_host and clean_host not in ("0.0.0.0", "::"):
        base_hosts.add(clean_host)
    if not is_loopback_host(clean_host):
        env_hosts = os.environ.get("GEMINA_UI_ALLOWED_HOSTS", "")
        for item in env_hosts.split(","):
            cleaned = item.strip().strip("[]")
            if cleaned:
                base_hosts.add(cleaned)
        for candidate in (socket.gethostname(), socket.getfqdn()):
            if candidate:
                host_name = candidate.strip()
                base_hosts.add(host_name)
                try:
                    for info in socket.getaddrinfo(host_name, None):
                        addr = str(info[4][0])
                        if addr and "%" not in addr:
                            base_hosts.add(addr)
                except OSError:
                    pass

    allowed: set[str] = set()
    for h in base_hosts:
        allowed.add(h)
        if port > 0:
            allowed.add(f"[{h}]:{port}" if ":" in h else f"{h}:{port}")
    return allowed, set(allowed)


class _GeminaThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        owner: "WebCommandCenterServer",
    ):
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        else:
            self.address_family = socket.AF_INET
        self.owner = owner
        super().__init__(server_address, RequestHandlerClass)


class _CommandCenterRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _GeminaThreadingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress default stderr HTTP logs containing URL paths/headers.
        return

    def _send_json(self, status: int, payload: dict[str, Any], *, is_api: bool = True) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        apply_security_headers(self, is_api=is_api)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(raw)
            self.wfile.flush()

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        *,
        is_api: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        apply_security_headers(self, is_api=is_api)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()

    def _read_request_body(self) -> bytes:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not str(raw_len).strip():
            return b""
        try:
            length = int(str(raw_len).strip())
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        if length < 0:
            raise ValueError("Negative Content-Length header.")
        if length > MAX_BODY_BYTES:
            raise OverflowError(
                f"Request body exceeds maximum size of {MAX_BODY_BYTES} bytes."
            )
        return self.rfile.read(length) if length > 0 else b""

    def _check_security_gates(self, clean_path: str) -> bool:
        owner = self.server.owner
        is_api = clean_path.startswith("/api/") or clean_path == "/api"

        ok, reason = verify_host_and_origin(
            self.command,
            self.headers,
            owner.allowed_hosts,
            owner.expected_origin_hosts,
        )
        if not ok:
            status, payload = api_error(403, "FORBIDDEN", reason)
            self._send_json(status, payload, is_api=is_api)
            return False

        if has_query_token(self.path):
            status, payload = api_error(
                401,
                "QUERY_TOKEN_REJECTED",
                "Authentication tokens in URL query parameters (?token=) are rejected.",
            )
            self._send_json(status, payload, is_api=is_api)
            return False

        if is_api and owner.ui_token:
            if not verify_bearer_token(
                self.headers, owner.ui_token, path_or_query=self.path
            ):
                status, payload = api_error(
                    401,
                    "UNAUTHORIZED",
                    "Missing or invalid Authorization: Bearer token.",
                )
                self._send_json(status, payload, is_api=True)
                return False

        return True

    def _write_http_chunk(self, data: bytes) -> None:
        if not data:
            return
        header = f"{len(data):x}\r\n".encode("ascii")
        self.wfile.write(header + data + b"\r\n")
        self.wfile.flush()

    def _handle_sse_stream(self) -> None:
        owner = self.server.owner
        last_event_id_raw = self.headers.get("Last-Event-ID")
        try:
            last_event_id = max(0, int(str(last_event_id_raw or "0").strip()))
        except ValueError:
            last_event_id = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        apply_security_headers(self, is_api=True)
        self.end_headers()
        self.close_connection = True

        with owner.sse_broker.subscribe(last_event_id=last_event_id) as sub:
            try:
                self._write_http_chunk(b": connected\n\n")
                for frame in sub:
                    if owner.is_shutting_down:
                        break
                    self._write_http_chunk(frame.encode("utf-8"))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

    def _serve_static(self, clean_path: str) -> None:
        raw_rel = unquote(clean_path)
        if "\x00" in raw_rel or "\\" in raw_rel:
            status, payload = api_error(403, "FORBIDDEN", "Invalid static path.")
            self._send_json(status, payload, is_api=False)
            return

        rel = raw_rel.lstrip("/")
        if not rel or rel == "index.html":
            rel = "index.html"
        elif rel.startswith("static/"):
            rel = rel[len("static/"):] or "index.html"

        segments = [seg for seg in rel.split("/") if seg]
        if any(seg in (".", "..") for seg in segments):
            status, payload = api_error(403, "FORBIDDEN", "Path traversal is forbidden.")
            self._send_json(status, payload, is_api=False)
            return

        static_root = self.server.owner.static_dir.resolve()
        candidate = (static_root / Path(*segments)).resolve() if segments else (static_root / "index.html").resolve()
        if not candidate.is_relative_to(static_root):
            status, payload = api_error(403, "FORBIDDEN", "Static path escapes static root.")
            self._send_json(status, payload, is_api=False)
            return

        if candidate.is_file():
            content_type = _MIME_TYPES.get(
                candidate.suffix.lower(), "application/octet-stream"
            )
            self._send_bytes(200, content_type, candidate.read_bytes(), is_api=False)
            return

        if rel == "index.html":
            self._send_bytes(
                200,
                "text/html; charset=utf-8",
                _FALLBACK_INDEX_HTML.encode("utf-8"),
                is_api=False,
            )
            return

        status, payload = api_error(404, "NOT_FOUND", f"Static asset not found: {clean_path}")
        self._send_json(status, payload, is_api=False)

    def _handle_any(self) -> None:
        parsed = urlsplit(self.path)
        clean_path = parsed.path or "/"
        if not self._check_security_gates(clean_path):
            return

        is_api = clean_path.startswith("/api/") or clean_path == "/api"
        if not is_api:
            if self.command in ("GET", "HEAD"):
                self._serve_static(clean_path)
            else:
                status, payload = api_error(405, "METHOD_NOT_ALLOWED", "Method not allowed.")
                self._send_json(status, payload, is_api=False)
            return

        if self.command == "GET" and clean_path == "/api/stream":
            self._handle_sse_stream()
            return

        try:
            body_bytes = self._read_request_body() if self.command in ("POST", "PUT", "PATCH") else b""
        except OverflowError as exc:
            status, payload = api_error(413, "PAYLOAD_TOO_LARGE", str(exc))
            self._send_json(status, payload, is_api=True)
            return
        except ValueError as exc:
            status, payload = api_error(400, "BAD_REQUEST", str(exc))
            self._send_json(status, payload, is_api=True)
            return

        query_params = parse_qs(parsed.query, keep_blank_values=True)
        status, payload = self.server.owner.api_router.dispatch(
            self.command, clean_path, query_params, body_bytes
        )
        self._send_json(status, payload, is_api=True)

    def do_GET(self) -> None:
        self._handle_any()

    def do_HEAD(self) -> None:
        self._handle_any()

    def do_POST(self) -> None:
        self._handle_any()

    def do_PUT(self) -> None:
        self._handle_any()

    def do_DELETE(self) -> None:
        self._handle_any()

    def do_PATCH(self) -> None:
        self._handle_any()

    def do_OPTIONS(self) -> None:
        self._handle_any()


class WebCommandCenterServer:
    """
    Coordinates HTTP serving, security gates, runtime bridge, SSE broker, and optional file tailing.
    """

    def __init__(
        self,
        config: Optional[TraderConfig] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        token: Optional[str] = None,
        enable_tail: bool = True,
        bridge: Optional[WebRuntimeBridge] = None,
        sse_broker: Optional[SSEBroker] = None,
        static_dir: Path = STATIC_DIR,
        allowed_hosts: Optional[set[str]] = None,
        expected_origin_hosts: Optional[set[str]] = None,
    ):
        self.config = config or TraderConfig.from_env()
        self.host = str(host if host is not None else self.config.ui_host).strip() or "127.0.0.1"
        self.requested_port = int(self.config.ui_port if port is None else port)
        self.ui_token = str(self.config.ui_token if token is None else token).strip()
        self.enable_tail = bool(enable_tail)
        self.static_dir = Path(static_dir)

        validate_ui_bind_security(self.host, self.ui_token)

        self.bridge = bridge or WebRuntimeBridge(config=self.config)
        self.sse_broker = sse_broker or SSEBroker()
        self.api_router = WebApiRouter(self.bridge, self.sse_broker)
        self.tail_watcher: Optional[JsonlTailWatcher] = (
            JsonlTailWatcher(
                broker=self.sse_broker,
                config=self.config,
                status_evaluator=self.bridge.status_evaluator,
            )
            if self.enable_tail
            else None
        )

        self._shutting_down = False
        self._thread: Optional[threading.Thread] = None
        self._httpd = _GeminaThreadingHTTPServer(
            (self.host, self.requested_port),
            _CommandCenterRequestHandler,
            owner=self,
        )
        self.server_port = int(self._httpd.server_address[1])

        default_allowed, default_origins = _build_default_allowlists(self.host, self.server_port)
        self.allowed_hosts = set(allowed_hosts) if allowed_hosts is not None else default_allowed
        self.expected_origin_hosts = (
            set(expected_origin_hosts) if expected_origin_hosts is not None else default_origins
        )

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def port(self) -> int:
        return self.server_port

    @property
    def base_url(self) -> str:
        formatted_host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{formatted_host}:{self.server_port}"

    def _start_background_services(self) -> None:
        self.sse_broker.start()
        if self.tail_watcher is not None:
            self.tail_watcher.start()

    def start_in_thread(self) -> threading.Thread:
        """Starts the HTTP server and background SSE services on a daemon thread."""
        self._start_background_services()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="gemina-web-http",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def serve_forever(self) -> None:
        """Runs the HTTP server blocking in the foreground until interrupted or shut down."""
        self._start_background_services()
        try:
            self._httpd.serve_forever(poll_interval=0.2)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Idempotently tears down SSE streams, tail watcher, HTTP server, and thread pool."""
        if self._shutting_down:
            return
        self._shutting_down = True

        if self.tail_watcher is not None:
            self.tail_watcher.stop()
        self.sse_broker.close_all()
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.bridge.shutdown(wait=True)

    def __enter__(self) -> "WebCommandCenterServer":
        self.start_in_thread()
        return self

    def __exit__(self, *args: Any) -> None:
        self.shutdown()


def create_server(
    host: Optional[str] = None,
    port: Optional[int] = None,
    *,
    enable_tail: bool = True,
    token: Optional[str] = None,
    config: Optional[TraderConfig] = None,
) -> WebCommandCenterServer:
    """Factory creating a configured WebCommandCenterServer instance."""
    return WebCommandCenterServer(
        config=config,
        host=host,
        port=port,
        token=token,
        enable_tail=enable_tail,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gemina Web Command Center — Local Portfolio, Charts, Trace Waterfall & AI Console"
    )
    parser.add_argument("--host", type=str, default=None, help="Bind host (default: GEMINA_UI_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: GEMINA_UI_PORT or 8765)")
    parser.add_argument(
        "--no-tail",
        action="store_true",
        help="Disable background JSONL file tailing & periodic health.tick publisher",
    )
    args = parser.parse_args()

    server = create_server(
        host=args.host,
        port=args.port,
        enable_tail=not args.no_tail,
    )
    print(f"Gemina Web Command Center listening on {server.base_url} (tail={not args.no_tail})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Gemina Web Command Center...")
        server.shutdown()


if __name__ == "__main__":
    main()
