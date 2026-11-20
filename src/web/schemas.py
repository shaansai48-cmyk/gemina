"""
Pydantic v2 Request DTOs and Normalized API Error Builders for the Web Command Center.
"""
from typing import Any, Literal, Optional
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from traderagent.infrastructure.persistence import redact_secrets


def api_error(
    status: int,
    code: str,
    message: str,
    details: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Builds a normalized `(http_status, error_payload)` response tuple."""
    error_obj: dict[str, Any] = {
        "status": int(status),
        "code": str(code),
        "message": str(message),
    }
    if details is not None:
        error_obj["details"] = redact_secrets(details)
    return int(status), {"ok": False, "error": error_obj}


def format_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Extracts sanitized field validation diagnostics from a Pydantic ValidationError."""
    sanitized: list[dict[str, Any]] = []
    for err in exc.errors():
        sanitized.append(
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": str(err.get("msg", "Invalid input")),
                "type": str(err.get("type", "validation_error")),
            }
        )
    return sanitized


class _WebDTO(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class FocusRequest(_WebDTO):
    tickers: str | list[str] = Field(
        validation_alias=AliasChoices("tickers", "ticker", "focus")
    )
    mirror_telegram: bool = False

    @field_validator("tickers")
    @classmethod
    def _validate_tickers(cls, value: str | list[str]) -> str:
        if isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
            joined = ",".join(parts)
        else:
            parts = [part.strip() for part in str(value).split(",") if part.strip()]
            joined = ",".join(parts)
        if not joined:
            raise ValueError("At least one ticker must be provided.")
        return joined


class StopRequest(_WebDTO):
    ticker: Optional[str] = Field(default=None, max_length=24)
    hard_stop_price: float = Field(
        gt=0,
        validation_alias=AliasChoices("hard_stop_price", "stop_price", "stop"),
    )
    take_profit_price: Optional[float] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("take_profit_price", "target_price", "target"),
    )
    mirror_telegram: bool = False


class FillMiniRequest(_WebDTO):
    ticker: Optional[str] = Field(default=None, max_length=24)
    units: int = Field(
        gt=0,
        validation_alias=AliasChoices("units", "quantity", "qty"),
    )
    price_sek: float = Field(
        gt=0,
        validation_alias=AliasChoices("price_sek", "price", "entry_sek"),
    )
    side: Literal["LONG", "SHORT"] = "LONG"
    mirror_telegram: bool = False

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: Any) -> str:
        return str(value or "LONG").strip().upper()


class CloseMiniRequest(_WebDTO):
    ticker: Optional[str] = Field(default=None, max_length=24)
    exit_sek: float = Field(
        gt=0,
        validation_alias=AliasChoices("exit_sek", "exit_price", "price_sek", "price"),
    )
    mirror_telegram: bool = False


class FillSharesRequest(_WebDTO):
    ticker: Optional[str] = Field(default=None, max_length=24)
    quantity: int = Field(
        gt=0,
        validation_alias=AliasChoices("quantity", "qty", "shares"),
    )
    avg_price: float = Field(
        gt=0,
        validation_alias=AliasChoices("avg_price", "price"),
    )
    mirror_telegram: bool = False


class CloseSharesRequest(_WebDTO):
    ticker: Optional[str] = Field(default=None, max_length=24)
    exit_price: float = Field(
        gt=0,
        validation_alias=AliasChoices("exit_price", "price"),
    )
    mirror_telegram: bool = False


class MiniParamsRequest(_WebDTO):
    ticker: Optional[str] = Field(default=None, max_length=24)
    financing_level: float = Field(
        gt=0,
        validation_alias=AliasChoices(
            "financing_level", "fin_level", "mini_financing_level", "fin_lvl"
        ),
    )
    barrier: Optional[float] = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("barrier", "mini_barrier"),
    )
    parity: Optional[float] = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("parity", "mini_parity"),
    )
    side: Literal["LONG", "SHORT"] = "LONG"
    instrument: Optional[str] = Field(default=None, max_length=64)
    mirror_telegram: bool = False

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: Any) -> str:
        return str(value or "LONG").strip().upper()


class AddTickerRequest(_WebDTO):
    ticker: str = Field(
        min_length=1,
        max_length=24,
        validation_alias=AliasChoices("ticker", "symbol"),
    )
    currency: Literal["USD", "SEK"] = "USD"
    hard_stop_price: float = Field(
        default=0.0,
        ge=0,
        validation_alias=AliasChoices("hard_stop_price", "stop_price", "stop"),
    )
    mini_financing_level: float = Field(
        default=0.0,
        ge=0,
        validation_alias=AliasChoices(
            "mini_financing_level", "financing_level", "fin_level"
        ),
    )
    mini_parity: Optional[float] = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("mini_parity", "parity"),
    )
    take_profit_price: float = Field(
        default=0.0,
        ge=0,
        validation_alias=AliasChoices("take_profit_price", "target_price", "target"),
    )
    mirror_telegram: bool = False

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> str:
        return str(value or "USD").strip().upper()


class ChatRequest(_WebDTO):
    message: str = Field(
        min_length=1,
        max_length=8000,
        validation_alias=AliasChoices("message", "text", "prompt", "query"),
    )
    persona: Literal["main", "subagent"] = Field(
        default="main",
        validation_alias=AliasChoices("persona", "mode"),
    )
    session_id: str = Field(
        default="web",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    mirror_telegram: bool = False

    @field_validator("persona", mode="before")
    @classmethod
    def _normalize_persona(cls, value: Any) -> str:
        raw = str(value or "main").strip().lower()
        if raw in ("ask", "main", "general", "gemina"):
            return "main"
        if raw in ("quant", "subagent", "advisor"):
            return "subagent"
        return raw


class ChatClearRequest(_WebDTO):
    session_id: str = Field(
        default="web",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )


class AnalyzeRequest(_WebDTO):
    ticker: Optional[str] = Field(default=None, max_length=24)
    mode: Literal["committee", "brief"] = Field(
        default="committee",
        validation_alias=AliasChoices("mode", "depth"),
    )
    allow_live_search: bool = True
    persist_audit: bool = False

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> str:
        raw = str(value or "committee").strip().lower()
        if raw in ("quick", "fast", "brief"):
            return "brief"
        if raw in ("deep", "committee", "full"):
            return "committee"
        return raw


class DeclarativeUpdateRequest(BaseModel):
    """Preserves exact Markdown whitespace/indentation for edited agents and skills."""

    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=200_000)

    @field_validator("content")
    @classmethod
    def _require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Declarative Markdown content must not be empty.")
        return value
