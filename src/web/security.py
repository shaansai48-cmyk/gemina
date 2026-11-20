"""
Security primitives for the Gemina Local Web Command Center.
Enforces bind-address safety, constant-time bearer authentication, DNS-rebinding Host checks,
cross-origin mutating-request blocking, strict HTTP security headers, and safe path resolution.
"""
from collections.abc import Mapping
import hmac
import ipaddress
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from src.loader import DEFAULT_AGENTS_DIR, DEFAULT_SKILLS_DIR

MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
STRICT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)
_QUERY_TOKEN_KEYS = frozenset({"token", "access_token", "auth", "bearer"})
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SAFE_MD_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.md)?$")


class SecurityError(ValueError):
    """Raised when a security policy or safe-path validation fails."""


def _get_header(headers: Any, name: str) -> str:
    """Reads a header case-insensitively from HTTPMessage or plain dict mappings."""
    if headers is None:
        return ""
    if isinstance(headers, Mapping) and not hasattr(headers, "get_all"):
        target = name.lower()
        for k, v in headers.items():
            if str(k).lower() == target:
                return str(v or "").strip()
        return ""
    try:
        val = headers.get(name)
        return str(val or "").strip()
    except Exception:
        return ""


def is_loopback_host(host: str) -> bool:
    """Returns True only for 'localhost' or loopback IP addresses (127.0.0.0/8, ::1)."""
    raw = str(host or "").strip()
    if not raw:
        return False
    if raw.lower() == "localhost":
        return True
    cleaned = raw.strip("[]")
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def validate_ui_bind_security(host: str, token: str) -> None:
    """Refuses binding the Web UI to a non-loopback address without GEMINA_UI_TOKEN."""
    if not is_loopback_host(host) and not str(token or "").strip():
        raise RuntimeError(
            "Refusing to bind Web UI to non-loopback address without GEMINA_UI_TOKEN."
        )


def has_query_token(path_or_query: str) -> bool:
    """Returns True when a URL path or query string supplies an authentication token parameter."""
    raw = str(path_or_query or "").strip()
    if not raw:
        return False
    query_str = urlsplit(raw).query if ("?" in raw or "/" in raw) else raw.lstrip("?")
    if not query_str:
        return False
    parsed = parse_qs(query_str, keep_blank_values=True)
    return any(str(k).lower() in _QUERY_TOKEN_KEYS for k in parsed.keys())


def verify_bearer_token(
    headers: Any,
    expected_token: str,
    *,
    path_or_query: str = "",
) -> bool:
    """
    Validates Authorization: Bearer <token> using constant-time hmac.compare_digest.
    Explicitly rejects ?token= query-string authentication.
    """
    if path_or_query and has_query_token(path_or_query):
        return False

    expected = str(expected_token or "").strip()
    if not expected:
        return False

    auth_header = _get_header(headers, "Authorization")
    if not auth_header:
        return False

    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False

    provided = parts[1].strip()
    if not provided:
        return False

    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _parse_host_port(value: str) -> tuple[str, int | None] | None:
    """Parses a Host header or authority string into (lowercase_hostname, optional_port)."""
    raw = str(value or "").strip()
    if not raw or any(ch in raw for ch in ("/", "\\", "@", " ", "\t", "\r", "\n")):
        return None

    bare_candidate = raw.strip("[]")
    try:
        ip_obj = ipaddress.ip_address(bare_candidate)
        return str(ip_obj).lower(), None
    except ValueError:
        pass

    try:
        parsed = urlsplit(f"//{raw}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    if not hostname:
        return None
    if port is not None and not (1 <= port <= 65535):
        return None
    return hostname.lower(), port


def _matches_host_allowlist(hostname: str, port: int | None, allowlist: set[str]) -> bool:
    """
    Checks (hostname, port) against allowlist entries.
    If the allowlist defines port-specific entries for `hostname`, `port` must match one when present.
    If the allowlist contains only bare hostnames for `hostname`, any port on `hostname` is accepted.
    """
    bare_allowed: set[str] = set()
    ported_allowed: dict[str, set[int]] = {}

    for entry in allowlist:
        parsed = _parse_host_port(entry)
        if parsed is None:
            continue
        a_host, a_port = parsed
        if a_port is None:
            bare_allowed.add(a_host)
        else:
            ported_allowed.setdefault(a_host, set()).add(a_port)

    if port is None:
        return hostname in bare_allowed

    if hostname in ported_allowed:
        return port in ported_allowed[hostname]
    return hostname in bare_allowed


def _validate_origin_url(url_value: str, expected_origin_hosts: set[str]) -> tuple[bool, str]:
    raw = str(url_value or "").strip()
    if not raw or raw.lower() == "null":
        return False, "Blocked null or empty Origin on mutating request."

    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        port = parsed.port
    except ValueError:
        return False, f"Malformed Origin/Referer header: {raw}"

    if scheme not in ("http", "https") or not hostname:
        return False, f"Unsupported Origin/Referer scheme or host: {raw}"

    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    # Match either explicit port or default port / bare host entry
    if _matches_host_allowlist(hostname, port, expected_origin_hosts):
        return True, "OK"
    if port is None and _matches_host_allowlist(hostname, effective_port, expected_origin_hosts):
        return True, "OK"
    return False, f"Cross-origin request blocked: {raw}"


def verify_host_and_origin(
    method: str,
    headers: Any,
    allowed_hosts: set[str],
    expected_origin_hosts: set[str],
) -> tuple[bool, str]:
    """
    Blocks DNS-rebinding Host spoofing on all requests and cross-origin
    Origin/Referer/Sec-Fetch-Site headers on mutating methods (POST/PUT/DELETE/PATCH).
    """
    host_header = _get_header(headers, "Host")
    parsed_host = _parse_host_port(host_header)
    if parsed_host is None:
        return False, "Missing or invalid Host header."

    hostname, port = parsed_host
    if not _matches_host_allowlist(hostname, port, allowed_hosts):
        return False, f"Disallowed Host header: {host_header}"

    if str(method or "").upper() in MUTATING_HTTP_METHODS:
        sec_fetch_site = _get_header(headers, "Sec-Fetch-Site")
        if sec_fetch_site and sec_fetch_site.lower() not in ("same-origin", "none"):
            return False, f"Cross-site Sec-Fetch-Site blocked: {sec_fetch_site}"

        origin = _get_header(headers, "Origin")
        if origin:
            return _validate_origin_url(origin, expected_origin_hosts)

        referer = _get_header(headers, "Referer")
        if referer:
            return _validate_origin_url(referer, expected_origin_hosts)

    return True, "OK"


def apply_security_headers(handler: Any, is_api: bool) -> None:
    """Applies strict CSP, framing, MIME-sniffing, referrer, and API cache-control headers."""
    handler.send_header("Content-Security-Policy", STRICT_CSP)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    if is_api:
        handler.send_header("Cache-Control", "no-store")


def resolve_safe_declarative_path(
    kind: str,
    filename: str,
    *,
    agents_dir: Path = DEFAULT_AGENTS_DIR,
    skills_dir: Path = DEFAULT_SKILLS_DIR,
) -> Path:
    """
    Resolves an agent or skill Markdown path safely under DEFAULT_AGENTS_DIR or DEFAULT_SKILLS_DIR.
    Allows only kind in ('agents', 'skills') and enforces basename + is_relative_to containment.
    """
    kind_norm = str(kind or "").strip().lower()
    if kind_norm not in ("agents", "skills"):
        raise SecurityError(f"Unsupported declarative kind: {kind!r}")

    raw_name = unquote(str(filename or "")).strip()
    if not raw_name or "\x00" in raw_name or "\\" in raw_name or raw_name.startswith("/"):
        raise SecurityError(f"Unsafe declarative filename: {filename!r}")
    if ".." in raw_name or ":" in raw_name:
        raise SecurityError(f"Path traversal forbidden in filename: {filename!r}")

    if kind_norm == "agents":
        if "/" in raw_name or not _SAFE_MD_FILENAME_RE.match(raw_name):
            raise SecurityError(f"Invalid agent filename: {filename!r}")
        fname = raw_name if raw_name.endswith(".md") else f"{raw_name}.md"
        base_dir = Path(agents_dir).resolve()
        target = (base_dir / fname).resolve()
        if not target.is_relative_to(base_dir) or target.parent != base_dir:
            raise SecurityError(f"Resolved agent path escapes base directory: {target}")
        return target

    # kind_norm == "skills"
    parts = raw_name.split("/")
    if len(parts) == 2:
        skill_name, leaf = parts[0], parts[1]
        if leaf != "SKILL.md" or not _SAFE_SEGMENT_RE.match(skill_name):
            raise SecurityError(f"Invalid skill path: {filename!r}")
    elif len(parts) == 1:
        leaf = parts[0]
        if not _SAFE_MD_FILENAME_RE.match(leaf):
            raise SecurityError(f"Invalid skill filename: {filename!r}")
        skill_name = leaf[:-3] if leaf.endswith(".md") else leaf
    else:
        raise SecurityError(f"Nested skill path not allowed: {filename!r}")

    base_dir = Path(skills_dir).resolve()
    hyphen_name = skill_name.replace("_", "-")
    if (base_dir / skill_name / "SKILL.md").is_file():
        chosen_dir = skill_name
    elif (base_dir / hyphen_name / "SKILL.md").is_file():
        chosen_dir = hyphen_name
    else:
        chosen_dir = skill_name

    target = (base_dir / chosen_dir / "SKILL.md").resolve()
    if not target.is_relative_to(base_dir) or target.parent.parent != base_dir:
        raise SecurityError(f"Resolved skill path escapes base directory: {target}")
    return target
