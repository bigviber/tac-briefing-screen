/**
 * Server-sent-events helper for the `?stream=1` endpoints (§11 Streaming).
 * Chunks arrive as `data: {"delta": "..."}` followed by `data: {"done": true, "payload": <final>}`.
 * If the server answers with anything other than `text/event-stream`, the body is parsed as
 * the plain (non-stream) JSON payload, so callers always get the final object back.
 */

export interface StreamEvent<T> {
  delta?: string;
  done?: boolean;
  payload?: T;
  error?: string;
}

export type DeltaHandler = (delta: string, accumulated: string) => void;

/** Split an SSE byte stream into the JSON objects carried by its `data:` lines. */
export async function* parseEventStream<T>(body: ReadableStream<Uint8Array>): AsyncGenerator<StreamEvent<T>> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    let sep = buffer.search(/\r?\n\r?\n/);
    while (sep >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep).replace(/^\r?\n\r?\n/, "");
      const data = block
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (data && data !== "[DONE]") {
        // JSON boundary: the stream payload shape is validated by the caller's generic.
        yield JSON.parse(data) as StreamEvent<T>;
      }
      sep = buffer.search(/\r?\n\r?\n/);
    }
    if (done) break;
  }
}

/** Drain a stream of events: forward deltas, resolve with the final payload. */
export async function consumeStream<T>(events: AsyncIterable<StreamEvent<T>>, onDelta?: DeltaHandler): Promise<T> {
  let acc = "";
  for await (const ev of events) {
    if (ev.error) throw new Error(ev.error);
    if (typeof ev.delta === "string") {
      acc += ev.delta;
      onDelta?.(ev.delta, acc);
    }
    if (ev.done) {
      if (ev.payload === undefined) throw new Error("Stream finished without a payload");
      return ev.payload;
    }
  }
  throw new Error("Stream ended before the final payload");
}

/** Interpret a fetch Response as SSE when possible, otherwise as a JSON body. */
export async function readStreamOrJson<T>(res: Response, onDelta?: DeltaHandler): Promise<T> {
  const ct = res.headers.get("content-type") ?? "";
  if (!ct.includes("text/event-stream") || !res.body) {
    // JSON boundary: non-stream fallback.
    return (await res.json()) as T;
  }
  return consumeStream(parseEventStream<T>(res.body), onDelta);
}
