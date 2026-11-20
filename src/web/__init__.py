"""
Gemina Local Web Command Center package (`src/web`).
Provides security primitives, SSE streaming, bounded concurrency, REST API routing,
and the local HTTP server (`gemina-ui`).
"""
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.web.server import WebCommandCenterServer, create_server, main

__all__ = [
    "WebCommandCenterServer",
    "create_server",
    "main",
]


def __getattr__(name: str) -> Any:
    """Lazily expose server entrypoints without eagerly loading runtime adapters."""
    if name in ("WebCommandCenterServer", "create_server", "main"):
        from src.web import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
