"""Exception -> HTTP status mapping (ARCHITECTURE.md section 5 error contract).

| Exception | Status |
|---|---|
| ``CaseNotFoundError`` / ``KeyError`` | 404 |
| ``ChapterLockedError``, ``SimulationLimitError``, ``CaseStateError`` | 409 |
| ``SandboxRejected`` | 400 |
| ``LLMConfigError`` | 428 (configure the LLM in Settings) |
| ``LLMHTTPError`` / other ``LLMError`` | 502 with the provider message |
| ``ModuleUnavailableError`` | 503 |

Messages never contain API keys: provider bodies are passed through :func:`scrub`.
"""

from __future__ import annotations

import logging
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from axiom.engine.llm_client import LLMConfigError, LLMError, LLMHTTPError
from axiom.engine.pedagogy import ChapterLockedError, SimulationLimitError
from axiom.engine.sandbox import SandboxRejected
from axiom.services import LLM_NOT_CONFIGURED, ModuleUnavailableError

__all__ = ["CaseStateError", "error_status", "error_detail", "scrub", "install_exception_handlers"]

logger = logging.getLogger("axiom.api.errors")

_KEY_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{6,}|sk_[A-Za-z0-9_\-]{6,}|[A-Za-z0-9_\-]{32,})\b")


class CaseStateError(RuntimeError):
    """The case is not in a state that allows the request (maps to 409)."""


def scrub(text: str) -> str:
    """Remove anything that looks like an API key from ``text``."""
    return _KEY_RE.sub("[redacted]", text)


def error_status(exc: BaseException) -> int | None:
    """HTTP status for a known exception type, else ``None``."""
    if isinstance(exc, (ChapterLockedError, SimulationLimitError, CaseStateError)):
        return 409
    if isinstance(exc, KeyError):
        return 404
    if isinstance(exc, SandboxRejected):
        return 400
    if isinstance(exc, LLMConfigError):
        return 428
    if isinstance(exc, LLMError):
        return 502
    if isinstance(exc, ModuleUnavailableError):
        return 503
    return None


def error_detail(exc: BaseException) -> str:
    """User-facing message for ``exc`` (provider message for LLM HTTP errors)."""
    if isinstance(exc, LLMConfigError):
        text = str(exc)
        return text if text == LLM_NOT_CONFIGURED else f"{LLM_NOT_CONFIGURED} ({scrub(text)})"
    if isinstance(exc, LLMHTTPError):
        return scrub(f"LLM provider returned HTTP {exc.status_code}: {exc.body[:500]}")
    if isinstance(exc, LLMError):
        return scrub(f"LLM error: {exc}")[:800]
    if isinstance(exc, KeyError):
        return str(exc.args[0]) if exc.args else "not found"
    return scrub(str(exc))[:800] or type(exc).__name__


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers for every mapped exception type."""

    async def _handler(request: Request, exc: Exception) -> JSONResponse:
        status = error_status(exc) or 500
        detail = error_detail(exc)
        log = logger.warning if status >= 500 else logger.info
        log("%s %s -> %d %s", request.method, request.url.path, status, type(exc).__name__)
        return JSONResponse(status_code=status, content={"detail": detail})

    for exc_type in (KeyError, ChapterLockedError, SimulationLimitError, CaseStateError, SandboxRejected,
                     LLMError, ModuleUnavailableError):
        app.add_exception_handler(exc_type, _handler)
