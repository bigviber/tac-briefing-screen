"""API tests: FastAPI ``TestClient`` + ``FakeTransport`` through a test ``AppServices`` on tmp dirs.

World generation is monkeypatched with ``make_minimal_world`` plus a small DuckDB, so these tests
do not depend on Module 2 (see ``test_e2e_world.py`` for the real generator).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

import axiom.services as services_mod
from axiom.config import Settings
from axiom.engine.llm_client import FakeHTTPError, FakeTransport
from axiom.models import ArchetypeInfo, CaseWorld
from axiom.services import AppServices
from main import create_app
from tests.test_pedagogy_offline import NARRATIVE, adversary_reply, chapter_payload, dossier_payload
from tests.world_factory import DAG, load_fixture, make_minimal_world

API = "/api/v1"
API_KEY = "sk-test-SECRETKEY-123456"


def fake_generate_world(archetype: str, seed: int, *, difficulty: str = "practitioner", industry: str | None = None,
                        out_dir: Path | None = None, pool: Any = None) -> CaseWorld:
    """Stand-in for Module 2: a minimal world plus a small company.duckdb in ``out_dir``."""
    assert out_dir is not None
    world = make_minimal_world(archetype, seed=seed, difficulty=difficulty)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(out_dir / "company.duckdb"))
    con.execute(
        "CREATE TABLE invoices AS SELECT 'INV-' || range AS invoice_id, 'C-' || (range % 7) AS customer_id, "
        "'2024-07-01' AS issued_at, (100000 + range * 10)::BIGINT AS amount_cents, "
        "CASE WHEN range % 3 = 0 THEN 90 ELSE 45 END AS terms_days, NULL::VARCHAR AS paid_at, "
        "'2024-07-02' AS updated_at FROM range(300)"
    )
    con.execute("CREATE TABLE customers AS SELECT 'C-' || range AS customer_id, 'Customer ' || range AS name, "
                "'smb' AS segment FROM range(7)")
    con.close()
    return world


class Env:
    """Test client + services + fake LLM replies (mutable by purpose)."""

    def __init__(self, client: TestClient, services: AppServices, transport: FakeTransport, replies: dict[str, Any]) -> None:
        self.client = client
        self.services = services
        self.transport = transport
        self.replies = replies

    def configure_llm(self) -> None:
        r = self.client.put(f"{API}/settings/llm", json={
            "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": API_KEY,
            "fast_model": "gpt-4o-mini", "deep_model": "gpt-4o",
        })
        assert r.status_code == 200, r.text

    def generate(self, archetype: str = "working_capital_crunch", seed: int = 7, difficulty: str = "practitioner") -> dict[str, Any]:
        r = self.client.post(f"{API}/cases/generate", json={"archetype": archetype, "seed": seed, "difficulty": difficulty})
        assert r.status_code == 200, r.text
        return r.json()

    def purposes(self) -> list[str]:
        return [q["purpose"] for q in self.transport.requests]

    def submit(self, case_id: str) -> dict[str, Any]:
        fx = load_fixture("good_submission_working_capital")
        world = self.services.world_service.load(case_id)
        body = {**fx["submission"], "case_id": "ignored", "metrics": {m.key: m.value for m in world.ground_truth.expected_metrics}}
        r = self.client.post(f"{API}/cases/{case_id}/submit", json=body)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_benchmarks_dir: Path) -> Iterator[Env]:
    settings = Settings(data_dir=tmp_path / "data", benchmarks_dir=fixture_benchmarks_dir, sandbox_timeout_s=20)
    settings.ensure_dirs()
    replies: dict[str, Any] = {
        "case_synthesizer": dossier_payload(),
        "textbook_chapter": chapter_payload(),
        "grader": load_fixture("good_submission_working_capital")["fake_rubric"],
        "adversary": adversary_reply(),
        "quarter_narrator": {"narrative_markdown": NARRATIVE, "watch_items": []},
        "settings_test": "pong",
    }
    transport = FakeTransport(replies)
    services = AppServices(settings, llm_transport=transport, llm_sleep=lambda s: None)
    monkeypatch.setattr(services_mod, "load_generate_world", lambda: fake_generate_world)
    app = create_app(settings, services_factory=lambda: services, frontend_dist=tmp_path / "no_frontend")
    with TestClient(app) as client:
        yield Env(client, services, transport, replies)


def sse_events(text: str) -> list[dict[str, Any]]:
    """Parse ``data:`` events of an SSE body."""
    return [json.loads(block[len("data: "):]) for block in text.strip().split("\n\n") if block.startswith("data: ")]


# --------------------------------------------------------------------------- health / settings


def test_health(env: Env) -> None:
    r = env.client.get(f"{API}/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"] and body["llm_configured"] is False
    env.configure_llm()
    assert env.client.get(f"{API}/health").json()["llm_configured"] is True


def test_settings_put_get_masked(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    unset = env.client.get(f"{API}/settings/llm").json()
    assert unset["configured"] is False and unset["api_key_masked"] == ""
    env.configure_llm()
    got = env.client.get(f"{API}/settings/llm")
    body = got.json()
    assert API_KEY not in got.text
    assert body["api_key_masked"] == "****3456" and body["api_key"] == "****3456"
    assert body["configured"] is True and body["provider"] == "openai" and body["api_key_set"] is True
    # Echoing the masked key (or leaving it blank) keeps the stored key.
    for echo in ("", "****3456"):
        r = env.client.put(f"{API}/settings/llm", json={**body, "api_key": echo, "fast_model": "gpt-4.1-mini"})
        assert r.status_code == 200 and API_KEY not in r.text
        stored = env.services.persistence.get_llm_settings()
        assert stored is not None and stored.api_key == API_KEY and stored.fast_model == "gpt-4.1-mini"
    assert all(API_KEY not in rec.getMessage() for rec in caplog.records)


def test_settings_test_usage_prompts(env: Env) -> None:
    r = env.client.post(f"{API}/settings/llm/test")
    assert r.json()["ok"] is False and "Settings" in r.json()["error"]
    env.configure_llm()
    ok = env.client.post(f"{API}/settings/llm/test").json()
    assert ok["ok"] is True and ok["error"] is None
    usage = env.client.get(f"{API}/settings/usage").json()
    assert usage["total_tokens"] > 0 and "gpt-4o-mini" in usage["by_model"]
    prompts = env.client.get(f"{API}/settings/prompts").json()
    assert {p["name"] for p in prompts} == {"case_synthesizer", "textbook_chapter", "grader", "adversary", "quarter_narrator"}
    assert all(p["template"] for p in prompts)


def test_llm_switch_applies_without_restart(env: Env) -> None:
    env.configure_llm()
    env.client.post(f"{API}/settings/llm/test")
    assert env.transport.requests[-1]["url"].startswith("https://api.openai.com/v1")
    env.client.put(f"{API}/settings/llm", json={"provider": "local", "base_url": "http://127.0.0.1:11434/v1", "api_key": "",
                                                 "fast_model": "llama3.1", "deep_model": "qwen2.5:32b"})
    env.client.post(f"{API}/settings/llm/test")
    assert env.transport.requests[-1]["url"].startswith("http://127.0.0.1:11434/v1")


# --------------------------------------------------------------------------- skills / archetypes


def test_skills_graph_and_reset(env: Env) -> None:
    g = env.client.get(f"{API}/skills/graph").json()
    assert len(g["nodes"]) == 65
    assert all(n["summary"] and n["tier"] for n in g["nodes"])
    assert {"id", "pillar", "title", "mastery", "prereqs", "attempts", "summary", "tier"} <= set(g["nodes"][0])
    e = g["edges"][0]
    assert e["src"] == e["source"] and e["dst"] == e["target"]
    assert set(g["pillars"]) == {"accounting_finance", "operations_strategy", "system_design", "sql", "python"}
    assert env.client.post(f"{API}/skills/reset").json() == {"ok": True}


def test_archetypes_endpoint(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    r = env.client.get(f"{API}/archetypes")
    assert r.status_code == 200 and isinstance(r.json(), list)
    monkeypatch.setattr(services_mod, "load_archetype_catalog", lambda: [ArchetypeInfo(key="k", title="K")])
    import axiom.api.routes_archetypes as ra

    monkeypatch.setattr(ra, "load_archetype_catalog", lambda: [ArchetypeInfo(key="k", title="K")])
    assert env.client.get(f"{API}/archetypes").json()[0]["key"] == "k"


# --------------------------------------------------------------------------- cases


def test_generate_get_list_schema(env: Env) -> None:
    case = env.generate()
    cid = case["case_id"]
    assert case["status"] == "generated"
    assert "ground_truth" not in case and "conceal" not in case["dossier_seed"]
    assert [a["key"] for a in case["asks"]] == ["dso_latest", "dso_baseline", "cash_trapped_in_ar_cents"]
    assert all(set(a) == {"key", "label", "unit", "hint"} for a in case["asks"])
    assert [lv["key"] for lv in case["levers"]] == ["terms_days", "early_pay_discount_pct"]
    text = json.dumps(case)
    assert "90-day payment terms for the top-20" not in text and "570000000" not in text
    assert (env.services.settings.cases_dir / cid / "world.json").is_file()
    assert (env.services.settings.cases_dir / cid / "company.duckdb").is_file()
    assert not [p for p in env.services.settings.cases_dir.iterdir() if p.name.startswith("_staging")]

    listed = env.client.get(f"{API}/cases").json()
    assert [c["case_id"] for c in listed] == [cid] and listed[0]["status"] == "generated"
    got = env.client.get(f"{API}/cases/{cid}").json()
    assert got["case_id"] == cid and got["asks"] == case["asks"]
    schema = env.client.get(f"{API}/cases/{cid}/schema").json()
    assert [t["name"] for t in schema["tables"]] == ["invoices", "customers"]


def test_generate_without_archetype_uses_skill_graph(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = ["working_capital_crunch", "forensic_revenue"]
    catalog = [ArchetypeInfo(key=k, title=k, skill_nodes=DAG["archetype_nodes"][k]) for k in keys]
    monkeypatch.setattr(services_mod, "load_archetype_catalog", lambda: catalog)
    r = env.client.post(f"{API}/cases/generate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archetype"] in keys and 0 <= body["seed"] < 2**31


def test_generate_module2_missing_is_503(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Any:
        raise services_mod.ModuleUnavailableError("the world generator (Module 2) is not available")

    monkeypatch.setattr(services_mod, "load_generate_world", missing)
    r = env.client.post(f"{API}/cases/generate", json={"archetype": "working_capital_crunch"})
    assert r.status_code == 503 and "Module 2" in r.json()["detail"]


def test_not_found_mappings(env: Env) -> None:
    for method, path, body in [
        ("get", "/cases/nope", None),
        ("get", "/cases/nope/schema", None),
        ("get", "/cases/nope/session", None),
        ("post", "/cases/nope/dossier", {}),
        ("post", "/sandbox/sql", {"case_id": "nope", "sql": "SELECT 1"}),
        ("post", "/sandbox/python", {"case_id": "nope", "code": "print(1)"}),
    ]:
        r = env.client.request(method.upper(), f"{API}{path}", json=body)
        assert r.status_code == 404, (path, r.status_code, r.text)
        assert "nope" in r.json()["detail"]


# --------------------------------------------------------------------------- sandbox endpoints


def test_sandbox_sql_endpoint(env: Env) -> None:
    cid = env.generate()["case_id"]
    r = env.client.post(f"{API}/sandbox/sql", json={"case_id": cid, "sql": "SELECT invoice_id, amount_cents, terms_days FROM invoices ORDER BY amount_cents", "limit": 5})
    body = r.json()
    assert r.status_code == 200 and body["error"] is None
    assert body["columns"] == ["invoice_id", "amount_cents", "terms_days"]
    assert body["column_types"] == ["VARCHAR", "BIGINT", "INTEGER"]
    assert body["row_count"] == 5 and body["truncated"] is True and body["rows"][0] == ["INV-0", 100000, 90]
    bad = env.client.post(f"{API}/sandbox/sql", json={"case_id": cid, "sql": "DROP TABLE invoices"})
    assert bad.status_code == 400 and "read-only" in bad.json()["detail"]
    multi = env.client.post(f"{API}/sandbox/sql", json={"case_id": cid, "sql": "SELECT 1; SELECT 2"})
    assert multi.status_code == 400
    err = env.client.post(f"{API}/sandbox/sql", json={"case_id": cid, "sql": "SELECT missing_col FROM invoices"}).json()
    assert err["error"] and err["rows"] == []


def test_sandbox_python_endpoint(env: Env) -> None:
    cid = env.generate()["case_id"]
    r = env.client.post(f"{API}/sandbox/python", json={
        "case_id": cid, "code": "inv = tables['invoices']\nprint(len(inv))\nresult = inv.groupby('terms_days').size().to_dict()",
    })
    body = r.json()
    assert r.status_code == 200 and body["error"] is None, body
    assert body["stdout"].strip() == "300" and body["result_json"] == {"45": 200, "90": 100}
    assert env.client.post(f"{API}/sandbox/python", json={"case_id": cid, "code": "  "}).status_code == 400


# --------------------------------------------------------------------------- LLM endpoints


def test_llm_not_configured_is_428(env: Env) -> None:
    cid = env.generate()["case_id"]
    for path, body in [("dossier", {}), ("textbook", {"skill_node": "accounting_finance.working_capital"})]:
        r = env.client.post(f"{API}/cases/{cid}/{path}", json=body)
        assert r.status_code == 428 and "Settings" in r.json()["detail"]
        s = env.client.post(f"{API}/cases/{cid}/{path}?stream=1", json=body)
        assert s.status_code == 428
    fx = load_fixture("good_submission_working_capital")
    r = env.client.post(f"{API}/cases/{cid}/submit", json={**fx["submission"], "case_id": cid})
    assert r.status_code == 428
    assert env.client.get(f"{API}/cases/{cid}").json()["status"] == "generated"
    assert env.transport.requests == []


def test_dossier_non_stream_and_cache(env: Env) -> None:
    env.configure_llm()
    cid = env.generate()["case_id"]
    r = env.client.post(f"{API}/cases/{cid}/dossier", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "Growing Into a Cash Squeeze" and d["stakeholder_messages"][0]["from_name"] == "Dana Okafor"
    assert env.client.post(f"{API}/cases/{cid}/dossier").json() == d  # body optional, served from cache
    assert env.purposes() == ["case_synthesizer"]
    assert env.client.get(f"{API}/cases/{cid}").json()["status"] == "briefed"


def test_dossier_stream(env: Env) -> None:
    env.configure_llm()
    cid = env.generate()["case_id"]
    r = env.client.post(f"{API}/cases/{cid}/dossier?stream=1", json={"force": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    deltas = "".join(e["delta"] for e in events if "delta" in e)
    assert len([e for e in events if "delta" in e]) > 1 and "Growing Into a Cash Squeeze" in deltas
    assert events[-1]["done"] is True and events[-1]["payload"]["title"] == "Growing Into a Cash Squeeze"
    assert env.transport.requests[0]["body"]["stream"] is True


def test_dossier_provider_error_502_and_sse_error(env: Env) -> None:
    env.configure_llm()
    cid = env.generate()["case_id"]
    env.replies["case_synthesizer"] = FakeHTTPError(400, '{"error": {"message": "model not found: gpt-9"}}')
    r = env.client.post(f"{API}/cases/{cid}/dossier", json={"force": True})
    assert r.status_code == 502 and "model not found: gpt-9" in r.json()["detail"]
    s = env.client.post(f"{API}/cases/{cid}/dossier?stream=1", json={"force": True})
    assert s.status_code == 200
    events = sse_events(s.text)
    assert len(events) == 1 and "model not found: gpt-9" in events[0]["error"]
    assert API_KEY not in s.text and API_KEY not in r.text


def test_sse_error_after_deltas(env: Env) -> None:
    env.configure_llm()
    cid = env.generate()["case_id"]
    env.replies["case_synthesizer"] = "this is not json at all, just prose " * 3
    s = env.client.post(f"{API}/cases/{cid}/dossier?stream=1", json={"force": True})
    events = sse_events(s.text)
    assert any("delta" in e for e in events)
    assert "error" in events[-1] and not any(e.get("done") for e in events)


def test_textbook_stream_and_409(env: Env) -> None:
    env.configure_llm()
    cid = env.generate()["case_id"]
    node = "accounting_finance.working_capital"
    r = env.client.post(f"{API}/cases/{cid}/textbook", json={"skill_node": node})
    assert r.status_code == 200 and r.json()["skill_node"] == node
    s = env.client.post(f"{API}/cases/{cid}/textbook?stream=1", json={"skill_node": node})
    events = sse_events(s.text)
    assert events[-1]["done"] and events[-1]["payload"]["title"] == "Working Capital from First Principles"
    unknown = env.client.post(f"{API}/cases/{cid}/textbook", json={"skill_node": "nope.node"})
    assert unknown.status_code == 404
    principal = env.generate(seed=99, difficulty="principal")["case_id"]
    locked = env.client.post(f"{API}/cases/{principal}/textbook", json={"skill_node": node})
    assert locked.status_code == 409
    assert env.client.post(f"{API}/cases/{principal}/textbook?stream=1", json={"skill_node": node}).status_code == 409


def test_submit_session_adversary_advance(env: Env) -> None:
    env.configure_llm()
    cid = env.generate()["case_id"]
    empty = env.client.get(f"{API}/cases/{cid}/session").json()
    assert empty == {"submission_id": None, "grade": None, "adversary_turns": [], "sim_history": [], "thread": [], "status": "generated"}

    grade = env.submit(cid)
    sid = grade["submission_id"]
    assert grade["deterministic"]["accuracy"] == 1.0
    assert grade["adversary_opening"] == adversary_reply()["message"]
    assert grade["skill_updates"]
    revealed = grade["revealed"]
    assert revealed["root_cause"].startswith("The 2024Q3 move")
    assert set(revealed["anomalies"][0]) == {"id", "kind", "pillar", "title", "mechanism", "periods", "severity", "evidence_trail"}
    assert revealed["reference_solution"]["memo_bullets"] == ["revert terms", "bridge with revolver"]
    assert env.client.get(f"{API}/cases/{cid}").json()["status"] == "graded"

    session = env.client.get(f"{API}/cases/{cid}/session").json()
    assert session["submission_id"] == sid and session["grade"]["submission_id"] == sid
    assert session["grade"]["revealed"] == revealed
    assert len(session["adversary_turns"]) == 1 and session["status"] == "graded"

    env.replies["adversary"] = adversary_reply("And what about the second shift?")
    turn = env.client.post(f"{API}/cases/{cid}/adversary", json={"submission_id": sid, "message": "DSO is 68 days in 2024Q4."})
    assert turn.status_code == 200, turn.text
    assert turn.json()["message"] == "And what about the second shift?"
    stream = env.client.post(f"{API}/cases/{cid}/adversary?stream=1", json={"submission_id": sid, "message": "Inventory is flat."})
    events = sse_events(stream.text)
    assert "".join(e.get("delta", "") for e in events) == "And what about the second shift?"
    assert events[-1]["payload"]["message"] == "And what about the second shift?"
    bad = env.client.post(f"{API}/cases/{cid}/adversary", json={"submission_id": "sub_missing", "message": "hi"})
    assert bad.status_code == 404

    session = env.client.get(f"{API}/cases/{cid}/session").json()
    assert len(session["adversary_turns"]) == 3
    assert [m["role"] for m in session["thread"]] == ["adversary", "user", "adversary", "user", "adversary"]
    assert session["thread"][1]["message"] == "DSO is 68 days in 2024Q4."

    q = env.client.post(f"{API}/cases/{cid}/advance", json={"submission_id": sid, "decisions": {"terms_days": 45, "early_pay_discount_pct": 0.01}})
    assert q.status_code == 200, q.text
    quarter = q.json()
    assert quarter["period"] == "2025Q1" and quarter["decisions"]["terms_days"] == 45 and quarter["kpis"]
    world = json.loads((env.services.settings.cases_dir / cid / "world.json").read_text())
    assert world["simulation"]["current_period"] == "2025Q1" and len(world["simulation"]["history"]) == 1
    assert env.client.get(f"{API}/cases/{cid}").json()["simulation"]["history"][0]["period"] == "2025Q1"
    assert env.client.get(f"{API}/cases/{cid}/session").json()["sim_history"][0]["period"] == "2025Q1"
    # practitioner cases allow two simulated quarters; the third is a 409.
    assert env.client.post(f"{API}/cases/{cid}/advance", json={"submission_id": sid, "decisions": {}}).status_code == 200
    over = env.client.post(f"{API}/cases/{cid}/advance", json={"submission_id": sid, "decisions": {}})
    assert over.status_code == 409


def test_submit_uses_reference_sql_runner(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.configure_llm()
    cid = env.generate()["case_id"]
    calls: list[str] = []
    from axiom.engine.sandbox import SQLSandbox

    original = SQLSandbox.run_reference

    def spy(self: SQLSandbox, sql: str) -> dict[str, Any]:
        calls.append(sql)
        return original(self, "SELECT avg(terms_days) AS t FROM invoices")

    monkeypatch.setattr(SQLSandbox, "run_reference", spy)
    fx = load_fixture("good_submission_working_capital")
    sub = {**fx["submission"], "sql_evidence": [{"name": "terms", "sql": "SELECT avg(terms_days) FROM invoices", "result_preview": {"rows": [[60.0]]}}]}
    grade = env.client.post(f"{API}/cases/{cid}/submit", json=sub).json()
    assert calls, "grader should execute reference SQL through the sandbox"
    match = grade["deterministic"]["query_signature_matches"][0]
    assert match["method"] == "execution" and match["matched"] is True


def test_spa_fallback(tmp_path: Path, fixture_benchmarks_dir: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>axiom</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    settings = Settings(data_dir=tmp_path / "data", benchmarks_dir=fixture_benchmarks_dir)
    app = create_app(settings, services_factory=lambda: AppServices(settings), frontend_dist=dist)
    with TestClient(app) as client:
        assert client.get("/").text == "<html>axiom</html>"
        assert client.get("/case/abc").text == "<html>axiom</html>"
        assert client.get("/assets/app.js").text == "console.log(1)"
        assert client.get("/api/v1/does-not-exist").status_code == 404
        assert client.get("/api/v1/health").json()["status"] == "ok"
