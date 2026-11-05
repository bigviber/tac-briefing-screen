"""LLM transport for Axiom (ARCHITECTURE.md section 9).

Two wire formats are supported:

* **OpenAI chat-completions** for ``openrouter``, ``openai`` and ``local`` providers:
  ``POST {base_url}/chat/completions`` with ``Authorization: Bearer <key>``. OpenRouter also
  receives ``HTTP-Referer`` and ``X-Title`` headers.
* **Anthropic Messages** for ``anthropic``: ``POST {base_url}/v1/messages`` with ``x-api-key``
  and ``anthropic-version: 2023-06-01``; system prompts travel in the top-level ``system`` field.

Request building, response parsing, SSE parsing and JSON extraction are pure module-level
functions shared by the sync :class:`LLMClient` and the :class:`AsyncLLMClient`. Every call
records an :class:`~axiom.models.LLMUsageRecord` when a persistence handle is supplied.

:class:`FakeTransport` is an ``httpx.MockTransport`` that serves canned replies in either wire
format (including streaming SSE) so every pedagogy path can be tested with no network.

The API key is never logged and ``repr()`` masks it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from axiom.models import LLMSettings, LLMTestResult, LLMUsageRecord, mask_api_key

__all__ = [
    "Tier",
    "Message",
    "LLMResponse",
    "LLMError",
    "LLMConfigError",
    "LLMHTTPError",
    "LLMTransportError",
    "LLMOutputError",
    "PreparedRequest",
    "PRICE_TABLE",
    "estimate_cost",
    "wire_format",
    "build_request",
    "parse_completion",
    "SSEDecoder",
    "extract_json",
    "repair_messages",
    "LLMClient",
    "AsyncLLMClient",
    "FakeHTTPError",
    "FakeTransport",
]

logger = logging.getLogger("axiom.llm_client")

Tier = Literal["fast", "deep"]
Message = dict[str, str]
WireFormat = Literal["openai", "anthropic"]

ANTHROPIC_VERSION = "2023-06-01"
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
MAX_ATTEMPTS = 3
PURPOSE_HEADER = "X-Axiom-Purpose"
JSON_INSTRUCTION = (
    "Respond with ONLY a single valid JSON object. No prose before or after it, no markdown fences."
)


# --------------------------------------------------------------------------- errors


class LLMError(RuntimeError):
    """Base class for LLM failures."""


class LLMConfigError(LLMError):
    """The settings cannot produce a valid request (e.g. missing API key)."""


class LLMHTTPError(LLMError):
    """The provider returned a non-retryable (or exhausted) HTTP error."""

    def __init__(self, status_code: int, body: str, *, purpose: str = "") -> None:
        self.status_code = status_code
        self.body = body[:2000]
        self.purpose = purpose
        super().__init__(f"LLM HTTP {status_code} for purpose={purpose!r}: {self.body[:300]}")


class LLMTransportError(LLMError):
    """Network failure or timeout after all retries."""


class LLMOutputError(LLMError):
    """The model output could not be parsed/validated as JSON, even after one repair call."""

    def __init__(self, message: str, raw: str) -> None:
        self.raw = raw
        super().__init__(f"{message}; raw output (truncated): {raw[:500]!r}")


# --------------------------------------------------------------------------- response type


class LLMResponse(BaseModel):
    """Result of one (possibly repaired) completion."""

    text: str
    parsed: dict[str, Any] | None = None
    obj: Any = None
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd_est: float = 0.0
    estimated: bool = True
    purpose: str = ""
    repaired: bool = False
    raw: dict[str, Any] = {}


# --------------------------------------------------------------------------- pricing

#: USD per 1M tokens ``(input, output)``, matched by case-insensitive substring of the model
#: name, first match wins (so more specific keys come first). These are rough public list
#: prices used only for the usage panel's estimate.
PRICE_TABLE: list[tuple[str, tuple[float, float]]] = [
    ("gpt-4o-mini", (0.15, 0.60)),
    ("gpt-4o", (2.50, 10.00)),
    ("gpt-4.1-nano", (0.10, 0.40)),
    ("gpt-4.1-mini", (0.40, 1.60)),
    ("gpt-4.1", (2.00, 8.00)),
    ("o4-mini", (1.10, 4.40)),
    ("o3", (2.00, 8.00)),
    ("claude-opus", (5.00, 25.00)),
    ("claude-sonnet", (3.00, 15.00)),
    ("claude-haiku", (1.00, 5.00)),
    ("llama", (0.20, 0.60)),
    ("qwen", (0.20, 0.60)),
    ("deepseek", (0.27, 1.10)),
    ("gemini-2.5-pro", (1.25, 10.00)),
    ("gemini", (0.30, 2.50)),
]


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, *, provider: str = "") -> tuple[float, bool]:
    """Return ``(cost_usd, estimated)``. Unknown models cost 0 with ``estimated=False``;
    the ``local`` provider is free and therefore an exact (estimated) zero."""
    if provider == "local":
        return 0.0, True
    name = model.lower()
    for key, (p_in, p_out) in PRICE_TABLE:
        if key in name:
            return round((prompt_tokens * p_in + completion_tokens * p_out) / 1_000_000, 8), True
    return 0.0, False


# --------------------------------------------------------------------------- request building


@dataclass
class PreparedRequest:
    """A fully built HTTP request plus metadata needed to parse the response."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    wire: WireFormat
    model: str
    prefill: str = ""


def wire_format(settings: LLMSettings) -> WireFormat:
    """``anthropic`` for the native Messages API, ``openai`` for everything else."""
    return "anthropic" if settings.provider == "anthropic" else "openai"


def _endpoint(settings: LLMSettings) -> str:
    base = settings.base_url.rstrip("/")
    if wire_format(settings) == "anthropic":
        return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    return f"{base}/chat/completions"


def _normalize_messages(messages: Sequence[Mapping[str, Any]]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        role = str(m.get("role", "user"))
        if role not in ("system", "user", "assistant"):
            raise ValueError(f"unsupported message role {role!r}")
        out.append({"role": role, "content": str(m.get("content", ""))})
    return out


def build_request(
    settings: LLMSettings,
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool = False,
    json_schema: dict[str, Any] | None = None,
    stream: bool = False,
    purpose: str = "",
    prefill: bool = True,
) -> PreparedRequest:
    """Build the provider-specific request for a chat completion.

    JSON mode: OpenAI-compatible providers get ``response_format={"type": "json_object"}``
    (``json_schema`` when the provider is ``openai`` and a schema is given). Anthropic gets a
    JSON-only instruction appended to ``system`` (with the schema if given) and, when
    ``prefill`` is true, an assistant turn prefilled with ``{``.
    """
    msgs = _normalize_messages(messages)
    if settings.provider != "local" and not settings.api_key:
        raise LLMConfigError(f"no API key configured for provider {settings.provider!r}")
    wire = wire_format(settings)
    headers: dict[str, str] = {"Content-Type": "application/json", PURPOSE_HEADER: purpose or "unspecified"}
    if stream:
        headers["Accept"] = "text/event-stream"

    if wire == "anthropic":
        headers["x-api-key"] = settings.api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
        system_parts = [m["content"] for m in msgs if m["role"] == "system"]
        convo = [m for m in msgs if m["role"] != "system"]
        prefill_text = ""
        if json_mode:
            instr = JSON_INSTRUCTION
            if json_schema:
                instr += "\nThe object must conform to this JSON schema:\n" + json.dumps(json_schema, separators=(",", ":"))
            system_parts.append(instr)
            if prefill and convo and convo[-1]["role"] == "user":
                prefill_text = "{"
                convo = [*convo, {"role": "assistant", "content": prefill_text}]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "messages": convo,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if stream:
            body["stream"] = True
        return PreparedRequest(_endpoint(settings), headers, body, wire, model, prefill_text)

    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    if settings.provider == "openrouter":
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "Axiom"
    if json_mode and not any("json" in m["content"].lower() for m in msgs):
        # OpenAI's json_object mode requires the word "JSON" to appear in the messages.
        msgs = [{"role": "system", "content": JSON_INSTRUCTION}, *msgs]
    body = {
        "model": model,
        "messages": msgs,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    if json_mode:
        if settings.provider == "openai" and json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": _schema_name(purpose), "schema": json_schema, "strict": False},
            }
        else:
            body["response_format"] = {"type": "json_object"}
    if stream:
        body["stream"] = True
        if settings.provider in ("openai", "openrouter"):
            body["stream_options"] = {"include_usage": True}
    return PreparedRequest(_endpoint(settings), headers, body, wire, model)


def _schema_name(purpose: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", purpose or "response")[:64]
    return name or "response"


def parse_completion(wire: WireFormat, data: Mapping[str, Any], *, prefill: str = "") -> tuple[str, str, int, int]:
    """Extract ``(text, model, prompt_tokens, completion_tokens)`` from a non-streaming response."""
    if wire == "anthropic":
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, Mapping) and b.get("type", "text") == "text")
        usage = data.get("usage") or {}
        return (
            prefill + text,
            str(data.get("model", "")),
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )
    choices = data.get("choices") or []
    text = ""
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # some providers return content parts
            text = "".join(p.get("text", "") for p in content if isinstance(p, Mapping))
        else:
            text = str(content or "")
    usage = data.get("usage") or {}
    return text, str(data.get("model", "")), int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)


@dataclass
class SSEDecoder:
    """Incremental decoder for provider SSE streams.

    Feed it lines with :meth:`feed_line`; it returns text deltas and accumulates the model name
    and token usage from ``message_start``/``message_delta`` (Anthropic) or the final usage chunk
    (OpenAI with ``include_usage``).
    """

    wire: WireFormat
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    done: bool = False
    _data_lines: list[str] = field(default_factory=list)

    def feed_line(self, line: str) -> list[str]:
        """Consume one line; return zero or more text deltas."""
        line = line.rstrip("\r")
        if line == "":
            return self._flush()
        if line.startswith(":"):
            return []
        if line.startswith("data:"):
            self._data_lines.append(line[5:].lstrip(" "))
        return []

    def close(self) -> list[str]:
        """Flush any pending event at end of stream."""
        return self._flush()

    def _flush(self) -> list[str]:
        if not self._data_lines:
            return []
        payload = "\n".join(self._data_lines)
        self._data_lines = []
        if payload.strip() == "[DONE]":
            self.done = True
            return []
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("ignoring non-JSON SSE payload (%d chars)", len(payload))
            return []
        return self._handle(event)

    def _handle(self, event: Mapping[str, Any]) -> list[str]:
        if self.wire == "anthropic":
            etype = event.get("type")
            if etype == "message_start":
                msg = event.get("message") or {}
                self.model = str(msg.get("model", self.model))
                usage = msg.get("usage") or {}
                self.prompt_tokens = int(usage.get("input_tokens", 0) or 0)
                self.completion_tokens = int(usage.get("output_tokens", 0) or 0)
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                text = delta.get("text")
                if text:
                    return [str(text)]
            elif etype == "message_delta":
                usage = event.get("usage") or {}
                if "output_tokens" in usage:
                    self.completion_tokens = int(usage.get("output_tokens") or 0)
            elif etype == "message_stop":
                self.done = True
            elif etype == "error":
                raise LLMHTTPError(500, json.dumps(event)[:500], purpose="stream")
            return []
        if event.get("model"):
            self.model = str(event["model"])
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            self.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            self.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        out: list[str] = []
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                out.append(str(content))
        return out


# --------------------------------------------------------------------------- JSON extraction

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _loads_obj(text: str) -> dict[str, Any] | None:
    for candidate in (text, _TRAILING_COMMA_RE.sub(r"\1", text)):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every balanced ``{...}`` substring, scanning string literals correctly."""
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break


def extract_json(text: str) -> dict[str, Any] | None:
    """Robustly extract a JSON object: direct parse, then fenced block, then the first balanced
    ``{...}`` that parses. Trailing commas are tolerated. Returns ``None`` if nothing parses."""
    if not text:
        return None
    stripped = text.strip()
    obj = _loads_obj(stripped)
    if obj is not None:
        return obj
    for match in _FENCE_RE.finditer(stripped):
        obj = _loads_obj(match.group(1).strip())
        if obj is not None:
            return obj
    for candidate in _balanced_objects(stripped):
        obj = _loads_obj(candidate)
        if obj is not None:
            return obj
    return None


def _validate(parsed: dict[str, Any] | None, parse_as: type[BaseModel] | None) -> tuple[Any, str | None]:
    """Return ``(validated_obj_or_None, error_message_or_None)``."""
    if parsed is None:
        return None, "output did not contain a JSON object"
    if parse_as is None:
        return parsed, None
    try:
        return parse_as.model_validate(parsed), None
    except ValidationError as exc:
        return None, str(exc)[:1500]


def repair_messages(messages: Sequence[Mapping[str, Any]], previous: str, schema: dict[str, Any] | None, error: str | None) -> list[Message]:
    """Messages for the single JSON repair call."""
    system = [{"role": "system", "content": str(m.get("content", ""))} for m in messages if m.get("role") == "system"]
    schema_txt = json.dumps(schema, indent=None, separators=(",", ":")) if schema else "(a single JSON object)"
    user = (
        f"Return only valid JSON matching this schema: {schema_txt}\n"
        f"Problem with the previous attempt: {error or 'invalid JSON'}\n"
        f"Here was your previous output:\n{previous[:12000]}"
    )
    return [*system, {"role": "user", "content": user}]


# --------------------------------------------------------------------------- client core


class _ClientBase:
    """Shared configuration, request preparation and usage accounting."""

    def __init__(
        self,
        settings: LLMSettings,
        persistence: Any | None = None,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base: float = 0.5,
        anthropic_prefill: bool = True,
    ) -> None:
        self.settings = settings
        self.persistence = persistence
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = float(backoff_base)
        self.anthropic_prefill = anthropic_prefill

    def __repr__(self) -> str:
        s = self.settings
        return (
            f"{type(self).__name__}(provider={s.provider!r}, base_url={s.base_url!r}, "
            f"api_key={mask_api_key(s.api_key)!r}, fast_model={s.fast_model!r}, deep_model={s.deep_model!r})"
        )

    # ------------------------------------------------------------------ helpers

    def model_for(self, tier: Tier) -> str:
        """Model name for a tier."""
        return self.settings.deep_model if tier == "deep" else self.settings.fast_model

    def _prepare(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: Tier,
        json_mode: bool,
        json_schema: dict[str, Any] | None,
        temperature: float | None,
        max_tokens: int | None,
        purpose: str,
        stream: bool,
    ) -> PreparedRequest:
        temp = temperature if temperature is not None else (
            self.settings.temperature_deep if tier == "deep" else self.settings.temperature_fast
        )
        return build_request(
            self.settings,
            messages,
            model=self.model_for(tier),
            temperature=temp,
            max_tokens=max_tokens or self.settings.max_tokens,
            json_mode=json_mode,
            json_schema=json_schema,
            stream=stream,
            purpose=purpose,
            prefill=self.anthropic_prefill,
        )

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            ra = response.headers.get("retry-after")
            if ra:
                try:
                    return min(float(ra), 30.0)
                except ValueError:
                    pass
        return self.backoff_base * (2 ** (attempt - 1))

    def _record(self, purpose: str, model: str, pt: int, ct: int, latency_ms: float) -> tuple[float, bool]:
        cost, estimated = estimate_cost(model, pt, ct, provider=self.settings.provider)
        if self.persistence is not None:
            try:
                self.persistence.record_usage(
                    LLMUsageRecord(
                        model=model or "unknown", purpose=purpose or "unspecified", prompt_tokens=pt,
                        completion_tokens=ct, latency_ms=round(latency_ms, 2), cost_usd_est=cost, estimated=estimated,
                    )
                )
            except Exception:  # usage accounting must never break a completion
                logger.exception("failed to record LLM usage for purpose=%s", purpose)
        logger.info(
            "llm call purpose=%s model=%s prompt_tokens=%d completion_tokens=%d latency_ms=%.0f",
            purpose, model, pt, ct, latency_ms,
        )
        return cost, estimated

    @staticmethod
    def _is_prefill_rejection(exc: LLMHTTPError) -> bool:
        body = exc.body.lower()
        return exc.status_code == 400 and ("prefill" in body or "final assistant" in body or "assistant message" in body)

    def _build_response(
        self, prep: PreparedRequest, data: Mapping[str, Any], latency_ms: float, purpose: str
    ) -> LLMResponse:
        text, model, pt, ct = parse_completion(prep.wire, data, prefill=prep.prefill)
        model = model or prep.model
        cost, estimated = self._record(purpose, model, pt, ct, latency_ms)
        return LLMResponse(
            text=text, model=model, prompt_tokens=pt, completion_tokens=ct, latency_ms=round(latency_ms, 2),
            cost_usd_est=cost, estimated=estimated, purpose=purpose, raw=dict(data),
        )

    @staticmethod
    def _schema_for(json_schema: dict[str, Any] | None, parse_as: type[BaseModel] | None) -> dict[str, Any] | None:
        if json_schema is not None:
            return json_schema
        return parse_as.model_json_schema() if parse_as is not None else None


class LLMClient(_ClientBase):
    """Synchronous LLM client (httpx).

    Args:
        settings: provider configuration.
        persistence: optional :class:`~axiom.persistence.Persistence`; every call records usage.
        transport: optional ``httpx`` transport (e.g. :class:`FakeTransport`) for tests.
        sleep: injectable sleep for the retry backoff (tests pass a no-op).
    """

    def __init__(
        self,
        settings: LLMSettings,
        persistence: Any | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        anthropic_prefill: bool = True,
    ) -> None:
        super().__init__(settings, persistence, max_attempts=max_attempts, backoff_base=backoff_base, anthropic_prefill=anthropic_prefill)
        self._sleep = sleep
        self._http = httpx.Client(transport=transport, timeout=DEFAULT_TIMEOUT)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def _post(self, prep: PreparedRequest, purpose: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = self._http.post(prep.url, headers=prep.headers, json=prep.body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("llm transport error purpose=%s attempt=%d: %s", purpose, attempt, type(exc).__name__)
            else:
                if response.status_code < 400:
                    try:
                        return dict(response.json())
                    except ValueError as exc:
                        raise LLMHTTPError(response.status_code, response.text, purpose=purpose) from exc
                err = LLMHTTPError(response.status_code, response.text, purpose=purpose)
                if response.status_code not in RETRYABLE_STATUS:
                    raise err
                last_error = err
                logger.warning("llm retryable status purpose=%s attempt=%d status=%d", purpose, attempt, response.status_code)
            if attempt < self.max_attempts:
                self._sleep(self._backoff(attempt, response))
        if isinstance(last_error, LLMHTTPError):
            raise last_error
        raise LLMTransportError(f"LLM request failed after {self.max_attempts} attempts: {type(last_error).__name__}") from last_error

    def _raw_complete(self, messages: Sequence[Mapping[str, Any]], **kw: Any) -> LLMResponse:
        purpose = kw["purpose"]
        prep = self._prepare(messages, stream=False, **kw)
        t0 = time.perf_counter()
        try:
            data = self._post(prep, purpose)
        except LLMHTTPError as exc:
            if prep.prefill and self._is_prefill_rejection(exc):
                logger.info("provider rejected assistant prefill; retrying without it")
                self.anthropic_prefill = False
                prep = self._prepare(messages, stream=False, **kw)
                t0 = time.perf_counter()
                data = self._post(prep, purpose)
            else:
                raise
        return self._build_response(prep, data, (time.perf_counter() - t0) * 1000, purpose)

    # ------------------------------------------------------------------ public API

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: Tier = "fast",
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str,
        parse_as: type[BaseModel] | None = None,
        json_mode: bool | None = None,
    ) -> LLMResponse:
        """One chat completion.

        JSON mode is on when ``json_schema`` or ``parse_as`` is given (or ``json_mode=True``).
        In JSON mode the output is extracted robustly and validated against ``parse_as``; on
        failure one repair call is made; if that fails too :class:`LLMOutputError` is raised.
        """
        want_json = json_mode if json_mode is not None else (json_schema is not None or parse_as is not None)
        schema = self._schema_for(json_schema, parse_as) if want_json else None
        kw = dict(tier=tier, json_mode=want_json, json_schema=schema, temperature=temperature, max_tokens=max_tokens, purpose=purpose)
        resp = self._raw_complete(messages, **kw)
        if not want_json:
            return resp
        return self._finish_json(messages, resp, kw, parse_as)

    def _finish_json(self, messages: Sequence[Mapping[str, Any]], resp: LLMResponse, kw: dict[str, Any], parse_as: type[BaseModel] | None) -> LLMResponse:
        parsed = extract_json(resp.text)
        obj, err = _validate(parsed, parse_as)
        if err is None:
            resp.parsed, resp.obj = parsed, obj
            return resp
        logger.warning("llm output invalid for purpose=%s (%s); attempting one repair", kw["purpose"], err[:120])
        schema = self._schema_for(kw["json_schema"], parse_as)
        repair_kw = {**kw, "purpose": f"{kw['purpose']}:repair", "temperature": 0.0}
        fixed = self._raw_complete(repair_messages(messages, resp.text, schema, err), **repair_kw)
        parsed2 = extract_json(fixed.text)
        obj2, err2 = _validate(parsed2, parse_as)
        if err2 is not None:
            raise LLMOutputError(f"invalid JSON for purpose={kw['purpose']} after repair: {err2[:300]}", fixed.text)
        fixed.parsed, fixed.obj, fixed.repaired = parsed2, obj2, True
        fixed.purpose = kw["purpose"]
        return fixed

    def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: Tier = "fast",
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str,
        json_mode: bool | None = None,
    ) -> Iterator[str]:
        """Yield text deltas as they arrive (SSE). Usage is recorded when the stream ends.

        The generator's return value (``StopIteration.value``) is the final :class:`LLMResponse`.
        """
        want_json = json_mode if json_mode is not None else json_schema is not None
        prep = self._prepare(messages, tier=tier, json_mode=want_json, json_schema=json_schema,
                             temperature=temperature, max_tokens=max_tokens, purpose=purpose, stream=True)
        return self._stream_gen(prep, purpose)

    def _stream_gen(self, prep: PreparedRequest, purpose: str) -> Generator[str, None, LLMResponse]:
        t0 = time.perf_counter()
        decoder = SSEDecoder(prep.wire)
        parts: list[str] = []
        if prep.prefill:
            parts.append(prep.prefill)
            yield prep.prefill
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self._http.stream("POST", prep.url, headers=prep.headers, json=prep.body) as response:
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", "replace")
                        err = LLMHTTPError(response.status_code, body, purpose=purpose)
                        if response.status_code in RETRYABLE_STATUS and attempt < self.max_attempts:
                            self._sleep(self._backoff(attempt, response))
                            continue
                        raise err
                    for line in response.iter_lines():
                        for delta in decoder.feed_line(line):
                            parts.append(delta)
                            yield delta
                    for delta in decoder.close():
                        parts.append(delta)
                        yield delta
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if len(parts) > (1 if prep.prefill else 0) or attempt >= self.max_attempts:
                    raise LLMTransportError(f"stream failed: {type(exc).__name__}") from exc
                self._sleep(self._backoff(attempt, None))
        latency = (time.perf_counter() - t0) * 1000
        model = decoder.model or prep.model
        cost, estimated = self._record(purpose, model, decoder.prompt_tokens, decoder.completion_tokens, latency)
        return LLMResponse(
            text="".join(parts), model=model, prompt_tokens=decoder.prompt_tokens,
            completion_tokens=decoder.completion_tokens, latency_ms=round(latency, 2), cost_usd_est=cost,
            estimated=estimated, purpose=purpose,
        )

    def stream_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: Tier = "fast",
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str,
        parse_as: type[BaseModel] | None = None,
    ) -> Generator[str, None, Any]:
        """Yield text deltas, then return the parsed object (``parse_as`` instance or dict).

        Usage::

            gen = client.stream_json(msgs, purpose="dossier", parse_as=Model)
            try:
                while True:
                    send(next(gen))
            except StopIteration as stop:
                result = stop.value
        """
        schema = self._schema_for(json_schema, parse_as)
        resp: LLMResponse = yield from self.stream(
            messages, tier=tier, json_schema=schema, temperature=temperature, max_tokens=max_tokens,
            purpose=purpose, json_mode=True,
        )
        kw = dict(tier=tier, json_mode=True, json_schema=schema, temperature=temperature, max_tokens=max_tokens, purpose=purpose)
        final = self._finish_json(messages, resp, kw, parse_as)
        return final.obj if parse_as is not None else final.parsed

    def test_connection(self) -> LLMTestResult:
        """One tiny call to verify credentials and connectivity."""
        t0 = time.perf_counter()
        try:
            resp = self.complete(
                [{"role": "user", "content": "Reply with the single word: pong"}],
                tier="fast", max_tokens=8, temperature=0.0, purpose="settings_test",
            )
        except LLMError as exc:
            return LLMTestResult(ok=False, model=self.settings.fast_model, latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=str(exc)[:500])
        return LLMTestResult(ok=True, model=resp.model, latency_ms=resp.latency_ms, error=None)


class AsyncLLMClient(_ClientBase):
    """Asynchronous counterpart of :class:`LLMClient` sharing the same request builders."""

    def __init__(
        self,
        settings: LLMSettings,
        persistence: Any | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base: float = 0.5,
        sleep: Callable[[float], Any] | None = None,
        anthropic_prefill: bool = True,
    ) -> None:
        super().__init__(settings, persistence, max_attempts=max_attempts, backoff_base=backoff_base, anthropic_prefill=anthropic_prefill)
        self._sleep = sleep or asyncio.sleep
        self._http = httpx.AsyncClient(transport=transport, timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def _pause(self, seconds: float) -> None:
        result = self._sleep(seconds)
        if asyncio.iscoroutine(result):
            await result

    async def _post(self, prep: PreparedRequest, purpose: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = await self._http.post(prep.url, headers=prep.headers, json=prep.body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("llm transport error purpose=%s attempt=%d: %s", purpose, attempt, type(exc).__name__)
            else:
                if response.status_code < 400:
                    try:
                        return dict(response.json())
                    except ValueError as exc:
                        raise LLMHTTPError(response.status_code, response.text, purpose=purpose) from exc
                err = LLMHTTPError(response.status_code, response.text, purpose=purpose)
                if response.status_code not in RETRYABLE_STATUS:
                    raise err
                last_error = err
            if attempt < self.max_attempts:
                await self._pause(self._backoff(attempt, response))
        if isinstance(last_error, LLMHTTPError):
            raise last_error
        raise LLMTransportError(f"LLM request failed after {self.max_attempts} attempts: {type(last_error).__name__}") from last_error

    async def _raw_complete(self, messages: Sequence[Mapping[str, Any]], **kw: Any) -> LLMResponse:
        purpose = kw["purpose"]
        prep = self._prepare(messages, stream=False, **kw)
        t0 = time.perf_counter()
        try:
            data = await self._post(prep, purpose)
        except LLMHTTPError as exc:
            if prep.prefill and self._is_prefill_rejection(exc):
                self.anthropic_prefill = False
                prep = self._prepare(messages, stream=False, **kw)
                t0 = time.perf_counter()
                data = await self._post(prep, purpose)
            else:
                raise
        return self._build_response(prep, data, (time.perf_counter() - t0) * 1000, purpose)

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: Tier = "fast",
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str,
        parse_as: type[BaseModel] | None = None,
        json_mode: bool | None = None,
    ) -> LLMResponse:
        """Async version of :meth:`LLMClient.complete` (same JSON extraction + one repair)."""
        want_json = json_mode if json_mode is not None else (json_schema is not None or parse_as is not None)
        schema = self._schema_for(json_schema, parse_as) if want_json else None
        kw = dict(tier=tier, json_mode=want_json, json_schema=schema, temperature=temperature, max_tokens=max_tokens, purpose=purpose)
        resp = await self._raw_complete(messages, **kw)
        if not want_json:
            return resp
        parsed = extract_json(resp.text)
        obj, err = _validate(parsed, parse_as)
        if err is None:
            resp.parsed, resp.obj = parsed, obj
            return resp
        fixed = await self._raw_complete(
            repair_messages(messages, resp.text, schema, err), **{**kw, "purpose": f"{purpose}:repair", "temperature": 0.0}
        )
        parsed2 = extract_json(fixed.text)
        obj2, err2 = _validate(parsed2, parse_as)
        if err2 is not None:
            raise LLMOutputError(f"invalid JSON for purpose={purpose} after repair: {err2[:300]}", fixed.text)
        fixed.parsed, fixed.obj, fixed.repaired, fixed.purpose = parsed2, obj2, True, purpose
        return fixed

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: Tier = "fast",
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str,
        json_mode: bool | None = None,
    ) -> AsyncIterator[str]:
        """Async generator of text deltas; usage is recorded at the end of the stream."""
        want_json = json_mode if json_mode is not None else json_schema is not None
        prep = self._prepare(messages, tier=tier, json_mode=want_json, json_schema=json_schema,
                             temperature=temperature, max_tokens=max_tokens, purpose=purpose, stream=True)
        t0 = time.perf_counter()
        decoder = SSEDecoder(prep.wire)
        if prep.prefill:
            yield prep.prefill
        for attempt in range(1, self.max_attempts + 1):
            async with self._http.stream("POST", prep.url, headers=prep.headers, json=prep.body) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    if response.status_code in RETRYABLE_STATUS and attempt < self.max_attempts:
                        await self._pause(self._backoff(attempt, response))
                        continue
                    raise LLMHTTPError(response.status_code, body, purpose=purpose)
                async for line in response.aiter_lines():
                    for delta in decoder.feed_line(line):
                        yield delta
                for delta in decoder.close():
                    yield delta
                break
        self._record(purpose, decoder.model or prep.model, decoder.prompt_tokens, decoder.completion_tokens,
                     (time.perf_counter() - t0) * 1000)

    async def test_connection(self) -> LLMTestResult:
        """Async connectivity check."""
        t0 = time.perf_counter()
        try:
            resp = await self.complete(
                [{"role": "user", "content": "Reply with the single word: pong"}],
                tier="fast", max_tokens=8, temperature=0.0, purpose="settings_test",
            )
        except LLMError as exc:
            return LLMTestResult(ok=False, model=self.settings.fast_model, latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=str(exc)[:500])
        return LLMTestResult(ok=True, model=resp.model, latency_ms=resp.latency_ms)


# --------------------------------------------------------------------------- fake transport


@dataclass
class FakeHTTPError:
    """A canned HTTP error for :class:`FakeTransport` (e.g. ``FakeHTTPError(429)``)."""

    status_code: int
    body: str = '{"error": "fake"}'
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeTimeout:
    """A canned timeout for :class:`FakeTransport`."""

    message: str = "fake read timeout"


FakeReply = str | dict[str, Any] | FakeHTTPError | FakeTimeout
FakeSource = Sequence[FakeReply] | Mapping[str, FakeReply | Sequence[FakeReply]] | Callable[[str, list[Message]], FakeReply]


class FakeTransport(httpx.MockTransport):
    """Offline ``httpx`` transport that answers in OpenAI or Anthropic wire format.

    ``responses`` may be:

    * a list of replies, consumed in order;
    * a mapping ``purpose -> reply | list[reply]`` (the purpose is read from the
      ``X-Axiom-Purpose`` header; ``"grader:repair"`` falls back to the key ``"grader:repair"``
      then ``"grader"``);
    * a callable ``(purpose, messages) -> reply``.

    A reply is a ``str`` (raw model text), a ``dict`` (serialized as JSON text), a
    :class:`FakeHTTPError` or a :class:`FakeTimeout`. When the request has ``stream: true`` the
    reply is emitted as SSE chunks. An Anthropic assistant prefill (``{``) is stripped from the
    canned text so the client's re-attachment round-trips exactly. Every request is captured in
    :attr:`requests` as ``{"url", "headers", "body", "purpose", "messages"}``.
    """

    def __init__(self, responses: FakeSource, *, chunk_size: int = 16) -> None:
        self.source = responses
        self.chunk_size = max(1, chunk_size)
        self.requests: list[dict[str, Any]] = []
        self._index = 0
        self._per_purpose: dict[str, int] = {}
        super().__init__(self._handle)

    # ------------------------------------------------------------------ dispatch

    def _next_reply(self, purpose: str, messages: list[Message]) -> FakeReply:
        src = self.source
        if callable(src) and not isinstance(src, Mapping):
            return src(purpose, messages)
        if isinstance(src, Mapping):
            for key in (purpose, purpose.split(":")[0]):
                if key in src:
                    item = src[key]
                    if isinstance(item, (str, dict, FakeHTTPError, FakeTimeout)):
                        return item
                    seq = list(item)
                    i = self._per_purpose.get(key, 0)
                    self._per_purpose[key] = i + 1
                    if i >= len(seq):
                        return seq[-1]
                    return seq[i]
            raise AssertionError(f"FakeTransport has no reply for purpose {purpose!r}")
        seq = list(src)
        if self._index >= len(seq):
            raise AssertionError(f"FakeTransport exhausted after {len(seq)} replies (purpose={purpose!r})")
        reply = seq[self._index]
        self._index += 1
        return reply

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        wire: WireFormat = "anthropic" if request.url.path.endswith("/messages") else "openai"
        messages: list[Message] = []
        if wire == "anthropic" and body.get("system"):
            messages.append({"role": "system", "content": str(body["system"])})
        messages.extend({"role": m["role"], "content": str(m.get("content", ""))} for m in body.get("messages", []))
        purpose = request.headers.get(PURPOSE_HEADER, "")
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body, "purpose": purpose, "messages": messages})
        reply = self._next_reply(purpose, messages)
        if isinstance(reply, FakeTimeout):
            raise httpx.ReadTimeout(reply.message, request=request)
        if isinstance(reply, FakeHTTPError):
            return httpx.Response(reply.status_code, text=reply.body, headers=reply.headers)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        prefill = ""
        if wire == "anthropic" and messages and messages[-1]["role"] == "assistant":
            prefill = messages[-1]["content"]
            if text.lstrip().startswith(prefill):
                text = text.lstrip()[len(prefill):]
        model = str(body.get("model", "fake-model"))
        pt = sum(len(m["content"]) for m in messages) // 4 + 1
        ct = len(text) // 4 + 1
        if body.get("stream"):
            return httpx.Response(200, content=self._sse(wire, text, model, pt, ct).encode("utf-8"),
                                  headers={"content-type": "text/event-stream"})
        if wire == "anthropic":
            payload = {
                "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": pt, "output_tokens": ct},
            }
        else:
            payload = {
                "id": "chatcmpl-fake", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
            }
        return httpx.Response(200, json=payload)

    def _sse(self, wire: WireFormat, text: str, model: str, pt: int, ct: int) -> str:
        chunks = [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)] or [""]
        out: list[str] = []

        def emit(event: str | None, data: Any) -> None:
            if event:
                out.append(f"event: {event}")
            out.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
            out.append("")

        if wire == "anthropic":
            emit("message_start", {"type": "message_start", "message": {"id": "msg_fake", "model": model, "usage": {"input_tokens": pt, "output_tokens": 1}}})
            emit("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            out.append(": keep-alive")
            for c in chunks:
                emit("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": c}})
            emit("content_block_stop", {"type": "content_block_stop", "index": 0})
            emit("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": ct}})
            emit("message_stop", {"type": "message_stop"})
        else:
            emit(None, {"id": "c", "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            for c in chunks:
                emit(None, {"id": "c", "model": model, "choices": [{"index": 0, "delta": {"content": c}}]})
            emit(None, {"id": "c", "model": model, "choices": [], "usage": {"prompt_tokens": pt, "completion_tokens": ct}})
            emit(None, "[DONE]")
        return "\n".join(out) + "\n"
