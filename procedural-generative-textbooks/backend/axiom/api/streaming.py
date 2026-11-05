"""Server-sent-events bridge for the LLM endpoints (ARCHITECTURE.md sections 11 and 12.8).

The synchronous pedagogy call runs in a worker thread; its ``on_delta`` callback pushes text
into a queue that an async generator turns into ``data: {"delta": ...}`` events, then
``data: {"done": true, "payload": ...}``. Failures after the stream started become
``data: {"error": "..."}``.

Errors that happen before the first event and have a precise HTTP meaning (404 / 409 / 428)
are raised as normal HTTP errors instead, so the client can react (e.g. open Settings).

Cancellation: when the client disconnects (polled with ``request.is_disconnected()`` while
waiting for the next event, and on generator close/cancel), a flag is set and the next
``on_delta`` call raises :class:`StreamCancelled` inside the worker. The pedagogy layer lets
exceptions from ``on_delta`` propagate, so the LLM stream is abandoned (its HTTP response is
closed) instead of running to completion for nobody.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from axiom.api.errors import error_detail, error_status

__all__ = ["DeltaCallback", "StreamCancelled", "sse_event", "stream_or_call"]

logger = logging.getLogger("axiom.api.streaming")

DeltaCallback = Callable[[str], None]
_PRE_STREAM_STATUSES = {404, 409, 428}
#: How often (seconds) the stream checks for a client disconnect while waiting for an event.
POLL_INTERVAL_S = 0.25


class StreamCancelled(Exception):
    """Raised from ``on_delta`` in the worker thread once the SSE client has gone away."""


class _Disconnectable(Protocol):
    async def is_disconnected(self) -> bool: ...


def sse_event(obj: dict[str, Any]) -> str:
    """One SSE ``data:`` event."""
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


def _payload(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


async def stream_or_call(
    work: Callable[[DeltaCallback | None], Any],
    stream: bool,
    request: _Disconnectable | None = None,
) -> Any:
    """Run ``work`` and return its result, or stream it as SSE when ``stream`` is true.

    ``work(on_delta)`` is a blocking function; with ``stream=False`` it gets ``None`` and runs in
    the threadpool. With ``stream=True`` it runs in a daemon thread; ``request`` (the Starlette
    request) is polled for a disconnect, which cancels the worker at its next delta.
    """
    if not stream:
        return await run_in_threadpool(work, None)

    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    cancelled = threading.Event()

    def on_delta(delta: str) -> None:
        if cancelled.is_set():
            raise StreamCancelled("client disconnected")
        events.put(("delta", delta))

    def target() -> None:
        try:
            result = work(on_delta)
        except StreamCancelled:
            logger.info("stream cancelled: client disconnected")
            events.put(("cancelled", None))
        except Exception as exc:  # forwarded to the client as an SSE error / HTTP status
            events.put(("error", exc))
        else:
            events.put(("done", result))

    worker = threading.Thread(target=target, name="axiom-sse-worker", daemon=True)
    worker.start()

    async def next_event() -> tuple[str, Any] | None:
        """The next queued event, or ``None`` once the client disconnected."""
        while True:
            try:
                return events.get_nowait()
            except queue.Empty:
                pass
            if request is not None and await request.is_disconnected():
                cancelled.set()
                return None
            try:
                return await run_in_threadpool(events.get, True, POLL_INTERVAL_S)
            except queue.Empty:
                continue

    try:
        first = await next_event()
    except BaseException:
        cancelled.set()
        raise
    if first is None:
        return StreamingResponse(iter(()), media_type="text/event-stream")
    if first[0] == "error" and error_status(first[1]) in _PRE_STREAM_STATUSES:
        raise first[1]

    async def generate() -> AsyncIterator[str]:
        item: tuple[str, Any] | None = first
        try:
            while item is not None:
                kind, value = item
                if kind == "delta":
                    if value:
                        yield sse_event({"delta": value})
                elif kind == "done":
                    yield sse_event({"done": True, "payload": _payload(value)})
                    return
                elif kind == "cancelled":
                    return
                else:
                    exc: BaseException = value
                    logger.warning("stream failed: %s", type(exc).__name__)
                    yield sse_event({"error": error_detail(exc)})
                    return
                item = await next_event()
        finally:  # normal end, client disconnect, or task cancellation / aclose()
            if worker.is_alive():
                cancelled.set()

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
