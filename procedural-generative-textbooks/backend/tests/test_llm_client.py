"""Offline tests for axiom.engine.llm_client (no network: FakeTransport only)."""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import BaseModel

from axiom.engine.llm_client import (
    AsyncLLMClient,
    FakeHTTPError,
    FakeTimeout,
    FakeTransport,
    LLMClient,
    LLMConfigError,
    LLMHTTPError,
    LLMOutputError,
    LLMTransportError,
    SSEDecoder,
    estimate_cost,
    extract_json,
)
from axiom.models import LLMSettings
from axiom.persistence import Persistence

KEY = "sk-secret-abcdef-9876"
MSGS = [{"role": "system", "content": "You are terse."}, {"role": "user", "content": "Say hi in JSON."}]


def settings(provider: str = "openai", **kw: object) -> LLMSettings:
    base = {"openai": "https://api.openai.com/v1", "openrouter": "https://openrouter.ai/api/v1",
            "anthropic": "https://api.anthropic.com", "local": "http://localhost:11434/v1"}[provider]
    data = dict(provider=provider, base_url=base, api_key=KEY, fast_model="gpt-4o-mini", deep_model="gpt-4o")
    if provider == "anthropic":
        data.update(fast_model="claude-haiku-4-5", deep_model="claude-sonnet-4-5")
    data.update(kw)
    return LLMSettings(**data)  # type: ignore[arg-type]


def make(provider: str, replies: object, persistence: Persistence | None = None, **kw: object) -> tuple[LLMClient, FakeTransport]:
    transport = FakeTransport(replies)  # type: ignore[arg-type]
    sleeps: list[float] = []
    client = LLMClient(settings(provider, **kw), persistence, transport=transport, sleep=sleeps.append)
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client, transport


@pytest.fixture()
def db() -> Persistence:
    p = Persistence(":memory:")
    p.init_schema()
    return p


class Greeting(BaseModel):
    greeting: str
    count: int


# --------------------------------------------------------------------------- wire formats


def test_openai_wire_format() -> None:
    client, t = make("openai", ["hello there"])
    resp = client.complete(MSGS, tier="deep", purpose="unit")
    req = t.requests[0]
    assert req["url"] == "https://api.openai.com/v1/chat/completions"
    assert req["headers"]["authorization"] == f"Bearer {KEY}"
    assert req["body"]["model"] == "gpt-4o"
    assert req["body"]["temperature"] == pytest.approx(0.2)  # deep tier temperature
    assert req["body"]["messages"][0] == {"role": "system", "content": "You are terse."}
    assert "response_format" not in req["body"]
    assert resp.text == "hello there"
    assert resp.model == "gpt-4o" and resp.prompt_tokens > 0 and resp.completion_tokens > 0


def test_openrouter_headers_and_json_object_mode() -> None:
    client, t = make("openrouter", [{"greeting": "hi", "count": 1}])
    resp = client.complete(MSGS, json_schema={"type": "object"}, purpose="unit")
    req = t.requests[0]
    assert req["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert req["headers"]["http-referer"] == "http://localhost"
    assert req["headers"]["x-title"] == "Axiom"
    assert req["body"]["response_format"] == {"type": "json_object"}
    assert resp.parsed == {"greeting": "hi", "count": 1}


def test_openai_json_schema_response_format() -> None:
    client, t = make("openai", [{"greeting": "hi", "count": 2}])
    resp = client.complete(MSGS, parse_as=Greeting, purpose="grader")
    rf = t.requests[0]["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "grader"
    assert rf["json_schema"]["schema"]["properties"]["count"]["type"] == "integer"
    assert isinstance(resp.obj, Greeting) and resp.obj.count == 2


def test_anthropic_wire_format_system_and_prefill() -> None:
    client, t = make("anthropic", ['{"greeting": "hey", "count": 3}'])
    resp = client.complete(MSGS, parse_as=Greeting, purpose="unit")
    req = t.requests[0]
    assert req["url"] == "https://api.anthropic.com/v1/messages"
    assert req["headers"]["x-api-key"] == KEY
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req["headers"]
    body = req["body"]
    assert body["system"].startswith("You are terse.")
    assert "ONLY a single valid JSON object" in body["system"]
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["messages"][-1] == {"role": "assistant", "content": "{"}
    assert body["model"] == "claude-haiku-4-5"
    assert resp.text.startswith("{") and resp.obj == Greeting(greeting="hey", count=3)


def test_anthropic_base_url_with_v1_suffix() -> None:
    client, t = make("anthropic", ["ok"], base_url="https://proxy.example/v1/")
    client.complete([{"role": "user", "content": "x"}], purpose="unit")
    assert t.requests[0]["url"] == "https://proxy.example/v1/messages"


def test_anthropic_prefill_rejected_falls_back() -> None:
    client, t = make("anthropic", [FakeHTTPError(400, '{"error": "assistant prefill is not supported"}'), '{"greeting": "a", "count": 1}'])
    resp = client.complete(MSGS, parse_as=Greeting, purpose="unit")
    assert resp.obj.count == 1
    assert t.requests[1]["body"]["messages"][-1]["role"] == "user"


def test_local_provider_needs_no_key_but_openai_does() -> None:
    client, t = make("local", ["fine"], api_key="")
    assert client.complete([{"role": "user", "content": "x"}], purpose="unit").text == "fine"
    assert "authorization" not in t.requests[0]["headers"]
    bad, _ = make("openai", ["x"], api_key="")
    with pytest.raises(LLMConfigError):
        bad.complete([{"role": "user", "content": "x"}], purpose="unit")


# --------------------------------------------------------------------------- JSON extraction + repair


@pytest.mark.parametrize(
    "text",
    [
        '{"greeting": "hi", "count": 1}',
        'Sure! Here you go:\n```json\n{"greeting": "hi", "count": 1}\n```\nHope that helps.',
        'Result: {"greeting": "hi", "count": 1} -- trailing prose with a } brace',
        '```\n{"greeting": "hi", "count": 1,}\n```',
        'noise {not json} then {"greeting": "hi", "count": 1}',
        '{"greeting": "a } inside a string", "count": 1}',
    ],
)
def test_extract_json_variants(text: str) -> None:
    obj = extract_json(text)
    assert obj is not None and obj["count"] == 1


def test_extract_json_none() -> None:
    assert extract_json("no json here") is None
    assert extract_json("") is None


def test_dirty_output_parsed_without_repair() -> None:
    client, t = make("openai", ['Here:\n```json\n{"greeting": "hi", "count": 5}\n```'])
    resp = client.complete(MSGS, parse_as=Greeting, purpose="unit")
    assert resp.obj.count == 5 and not resp.repaired and len(t.requests) == 1


def test_repair_retry_then_success(db: Persistence) -> None:
    client, t = make("openai", ['```json\n{"greeting": "hi"}\n```', '{"greeting": "hi", "count": 7}'], db)
    resp = client.complete(MSGS, parse_as=Greeting, purpose="grader")
    assert resp.repaired and resp.obj.count == 7
    assert [r["purpose"] for r in t.requests] == ["grader", "grader:repair"]
    repair_prompt = t.requests[1]["messages"][-1]["content"]
    assert repair_prompt.startswith("Return only valid JSON matching this schema")
    assert '{"greeting": "hi"}' in repair_prompt
    assert {u.purpose for u in db.list_usage()} == {"grader", "grader:repair"}


def test_repair_failure_raises_with_raw() -> None:
    client, _ = make("openai", ["not json at all", "still not json"])
    with pytest.raises(LLMOutputError) as exc:
        client.complete(MSGS, parse_as=Greeting, purpose="unit")
    assert exc.value.raw == "still not json"


# --------------------------------------------------------------------------- retries


def test_retry_on_429_then_success() -> None:
    client, t = make("openai", [FakeHTTPError(429, "slow down"), "ok"])
    assert client.complete(MSGS, purpose="unit").text == "ok"
    assert len(t.requests) == 2
    assert client.sleeps == [0.5]  # type: ignore[attr-defined]


def test_retry_honours_retry_after_and_5xx_and_timeout() -> None:
    client, t = make("openai", [FakeHTTPError(503, "x", {"retry-after": "2"}), FakeTimeout(), "ok"])
    assert client.complete(MSGS, purpose="unit").text == "ok"
    assert client.sleeps == [2.0, 1.0]  # type: ignore[attr-defined]
    assert len(t.requests) == 3


def test_retries_exhausted() -> None:
    client, _ = make("openai", [FakeHTTPError(500), FakeHTTPError(502), FakeHTTPError(503)])
    with pytest.raises(LLMHTTPError) as exc:
        client.complete(MSGS, purpose="unit")
    assert exc.value.status_code == 503
    client2, _ = make("openai", [FakeTimeout(), FakeTimeout(), FakeTimeout()])
    with pytest.raises(LLMTransportError):
        client2.complete(MSGS, purpose="unit")


def test_non_retryable_status_raises_immediately() -> None:
    client, t = make("openai", [FakeHTTPError(401, "bad key"), "never"])
    with pytest.raises(LLMHTTPError):
        client.complete(MSGS, purpose="unit")
    assert len(t.requests) == 1


# --------------------------------------------------------------------------- streaming


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_streaming_sse(provider: str, db: Persistence) -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 3
    client, t = make(provider, [text], db)
    gen = client.stream([{"role": "user", "content": "go"}], purpose="stream_test")
    deltas = []
    try:
        while True:
            deltas.append(next(gen))
    except StopIteration as stop:
        final = stop.value
    assert len(deltas) > 3
    assert "".join(deltas) == text
    assert t.requests[0]["body"]["stream"] is True
    assert final.text == text and final.completion_tokens > 0 and final.prompt_tokens > 0
    usage = db.list_usage()
    assert len(usage) == 1 and usage[0].purpose == "stream_test" and usage[0].completion_tokens == final.completion_tokens


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_stream_json(provider: str) -> None:
    client, _ = make(provider, ['{"greeting": "streamed", "count": 9}'])
    gen = client.stream_json([{"role": "user", "content": "go"}], purpose="sj", parse_as=Greeting)
    deltas = []
    try:
        while True:
            deltas.append(next(gen))
    except StopIteration as stop:
        result = stop.value
    assert "".join(deltas) == '{"greeting": "streamed", "count": 9}'
    assert result == Greeting(greeting="streamed", count=9)


def test_sse_decoder_handles_comments_and_done() -> None:
    dec = SSEDecoder("openai")
    out = []
    for line in [": ping", 'data: {"choices":[{"delta":{"content":"a"}}]}', "", 'data: {"choices":[{"delta":{"content":"b"}}]}', "", "data: [DONE]", ""]:
        out += dec.feed_line(line)
    assert out == ["a", "b"] and dec.done


def test_retry_on_429_for_stream() -> None:
    client, t = make("openai", [FakeHTTPError(429), "abc"])
    assert "".join(client.stream([{"role": "user", "content": "go"}], purpose="s")) == "abc"
    assert len(t.requests) == 2


# --------------------------------------------------------------------------- usage + cost


def test_usage_recorded_with_cost_estimate(db: Persistence) -> None:
    client, _ = make("openai", ["hi"], db)
    resp = client.complete(MSGS, tier="fast", purpose="dossier")
    rows = db.list_usage()
    assert len(rows) == 1
    row = rows[0]
    assert row.model == "gpt-4o-mini" and row.purpose == "dossier"
    expected = (resp.prompt_tokens * 0.15 + resp.completion_tokens * 0.60) / 1_000_000
    assert row.cost_usd_est == pytest.approx(expected)
    assert resp.estimated is True and resp.cost_usd_est == pytest.approx(expected)
    assert db.usage_summary().by_model["gpt-4o-mini"].calls == 1


def test_cost_table_lookup() -> None:
    assert estimate_cost("openai/gpt-4o-mini-2024", 1_000_000, 0) == (0.15, True)
    assert estimate_cost("gpt-4o", 1_000_000, 1_000_000) == (12.5, True)
    assert estimate_cost("anthropic/claude-sonnet-4.5", 1_000_000, 0)[0] == 3.0
    assert estimate_cost("meta-llama/llama-3.1-70b", 10, 10)[1] is True
    assert estimate_cost("mystery-model-x", 1000, 1000) == (0.0, False)
    assert estimate_cost("anything", 1000, 1000, provider="local") == (0.0, True)


def test_unknown_model_flagged_not_estimated(db: Persistence) -> None:
    client, _ = make("openai", ["hi"], db, fast_model="mystery-model")
    resp = client.complete(MSGS, purpose="unit")
    assert resp.cost_usd_est == 0.0 and resp.estimated is False
    assert db.list_usage()[0].cost_usd_est == 0.0


# --------------------------------------------------------------------------- secrets + connection test


def test_key_masked_in_repr_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    client, _ = make("openai", [FakeHTTPError(429), "ok"])
    r = repr(client)
    assert KEY not in r and "****9876" in r
    with caplog.at_level(logging.DEBUG):
        client.complete(MSGS, purpose="unit")
    assert KEY not in caplog.text


def test_test_connection_ok_and_failure() -> None:
    client, t = make("openai", ["pong"])
    res = client.test_connection()
    assert res.ok and res.model == "gpt-4o-mini" and res.error is None
    assert t.requests[0]["body"]["max_tokens"] == 8
    bad, _ = make("openai", [FakeHTTPError(401, "invalid api key")])
    res2 = bad.test_connection()
    assert not res2.ok and "401" in (res2.error or "") and KEY not in (res2.error or "")


def test_fake_transport_callable_and_mapping() -> None:
    seen = []

    def reply(purpose: str, messages: list[dict[str, str]]) -> str:
        seen.append((purpose, messages[-1]["content"]))
        return f"echo:{purpose}"

    client, _ = make("openai", reply)
    assert client.complete([{"role": "user", "content": "q"}], purpose="p1").text == "echo:p1"
    assert seen == [("p1", "q")]
    client2, _ = make("openai", {"a": ["one", "two"], "b": "bee"})
    assert [client2.complete(MSGS, purpose=p).text for p in ("a", "a", "b", "a:repair")] == ["one", "two", "bee", "two"]


# --------------------------------------------------------------------------- async


def test_async_client_complete_and_stream(db: Persistence) -> None:
    async def run() -> tuple[str, str]:
        transport = FakeTransport({"x": '{"greeting": "async", "count": 4}', "s": "streaming text here"})
        client = AsyncLLMClient(settings("anthropic"), db, transport=transport, sleep=lambda s: None)
        resp = await client.complete(MSGS, parse_as=Greeting, purpose="x")
        parts = [d async for d in client.stream([{"role": "user", "content": "go"}], purpose="s")]
        await client.aclose()
        assert KEY not in repr(client)
        return resp.obj.greeting, "".join(parts)

    greeting, streamed = asyncio.run(run())
    assert greeting == "async" and streamed == "streaming text here"
    assert {u.purpose for u in db.list_usage()} == {"x", "s"}


def test_async_retry_on_429() -> None:
    async def run() -> str:
        client = AsyncLLMClient(settings("openai"), transport=FakeTransport([FakeHTTPError(429), "ok"]), sleep=lambda s: None)
        resp = await client.complete(MSGS, purpose="unit")
        await client.aclose()
        return resp.text

    assert asyncio.run(run()) == "ok"
