"""Server-sent-events bridge for the LLM endpoints (ARCHITECTURE.md sections 11 and 12.8).

The synchronous pedagogy call runs in a worker thread; its ``on_delta`` callback pushes text
into a queue that a sync generator (iterated by Starlette in its threadpool, so the event loop
never blocks) turns into ``data: {"delta": ...}`` events, then ``data: {"done": true,
"payload": ...}``. Failures after the stream started become ``data: {"error": "..."}``.

Errors that happen before the first event and have a precise HTTP meaning (404 / 409 / 428)
are raised as normal HTTP errors instead, so the client can react (e.g. open Settings).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from axiom.api.errors import error_detail, error_status

__all__ = ["DeltaCallback", "sse_event", "stream_or_call"]

logger = logging.getLogger("axiom.api.streaming")

DeltaCallback = Callable[[str], None]
_PRE_STREAM_STATUSES = {404, 409, 428}


def sse_event(obj: dict[str, Any]) -> str:
    """One SSE ``data:`` event."""
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


def _payload(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


async def stream_or_call(work: Callable[[DeltaCallback | None], Any], stream: bool) -> Any:
    """Run ``work`` and return its result, or stream it as SSE when ``stream`` is true.

    ``work(on_delta)`` is a blocking function; with ``stream=False`` it gets ``None`` and runs in
    the threadpool.
    """
    if not stream:
        return await run_in_threadpool(work, None)

    events: queue.Queue[tuple[str, Any]] = queue.Queue()

    def target() -> None:
        try:
            result = work(lambda delta: events.put(("delta", delta)))
        except Exception as exc:  # forwarded to the client as an SSE error / HTTP status
            events.put(("error", exc))
        else:
            events.put(("done", result))

    threading.Thread(target=target, name="axiom-sse-worker", daemon=True).start()
    first = await run_in_threadpool(events.get)
    if first[0] == "error" and error_status(first[1]) in _PRE_STREAM_STATUSES:
        raise first[1]

    def generate() -> Iterator[str]:
        item = first
        while True:
            kind, value = item
            if kind == "delta":
                if value:
                    yield sse_event({"delta": value})
            elif kind == "done":
                yield sse_event({"done": True, "payload": _payload(value)})
                return
            else:
                exc: BaseException = value
                logger.warning("stream failed: %s", type(exc).__name__)
                yield sse_event({"error": error_detail(exc)})
                return
            item = events.get()

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
