"""End-to-end through the API with the real Module 2 generator and the fake LLM.

generate (working_capital_crunch, seed 42, foundation) -> schema -> sql -> python -> submit.
Skipped while Module 2 (``axiom.engine.procedural_world``) is not installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytest.importorskip(
    "axiom.engine.procedural_world",
    reason="Module 2 (axiom.engine.procedural_world) is not available yet; e2e world test skipped",
)
try:  # present but not importable (e.g. mid-edit by the Module 2 build): skip with the reason
    import axiom.engine.procedural_world  # noqa: F401
except Exception as _exc:  # noqa: BLE001
    pytest.skip(f"Module 2 is present but failed to import: {type(_exc).__name__}: {_exc}", allow_module_level=True)

from axiom.config import Settings, get_settings  # noqa: E402
from axiom.engine.llm_client import FakeTransport  # noqa: E402
from axiom.services import AppServices  # noqa: E402
from main import create_app  # noqa: E402
from tests.test_pedagogy_offline import adversary_reply  # noqa: E402
from tests.world_factory import load_fixture  # noqa: E402

API = "/api/v1"


@pytest.fixture()
def e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, AppServices]]:
    data = tmp_path / "data"
    monkeypatch.setenv("AXIOM_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = Settings(data_dir=data)
    settings.ensure_dirs()
    transport = FakeTransport({
        "grader": load_fixture("good_submission_working_capital")["fake_rubric"],
        "adversary": adversary_reply(),
    })
    services = AppServices(settings, llm_transport=transport, llm_sleep=lambda s: None)
    app = create_app(settings, services_factory=lambda: services, frontend_dist=tmp_path / "none")
    try:
        with TestClient(app) as client:
            r = client.put(f"{API}/settings/llm", json={"provider": "openai", "base_url": "https://api.openai.com/v1",
                                                          "api_key": "sk-test-e2e-0000", "fast_model": "m", "deep_model": "m"})
            assert r.status_code == 200
            yield client, services
    finally:
        get_settings.cache_clear()


def test_generate_schema_sql_submit(e2e: tuple[TestClient, AppServices]) -> None:
    client, services = e2e
    r = client.post(f"{API}/cases/generate", json={"archetype": "working_capital_crunch", "seed": 42, "difficulty": "foundation"})
    assert r.status_code == 200, r.text
    case = r.json()
    cid = case["case_id"]
    assert case["archetype"] == "working_capital_crunch" and case["difficulty"] == "foundation"
    assert "ground_truth" not in case and case["asks"] and case["status"] == "generated"
    assert (services.settings.cases_dir / cid / "company.duckdb").is_file()

    schema = client.get(f"{API}/cases/{cid}/schema").json()
    assert schema["tables"] and schema["total_rows"] > 0
    table = schema["tables"][0]["name"]

    q = client.post(f"{API}/sandbox/sql", json={"case_id": cid, "sql": f'SELECT count(*) AS n FROM "{table}"'}).json()
    assert q["error"] is None and q["rows"][0][0] == schema["tables"][0]["row_count"]
    assert q["column_types"] == ["BIGINT"]

    py = client.post(f"{API}/sandbox/python", json={"case_id": cid, "code": f"result = len(tables[{table!r}])"}).json()
    assert py["error"] is None and py["result_json"] == schema["tables"][0]["row_count"]

    world = services.world_service.load(cid)
    evidence: list[dict[str, Any]] = []
    for anomaly in world.ground_truth.anomalies:
        for ref in anomaly.reference_sql:
            res = client.post(f"{API}/sandbox/sql", json={"case_id": cid, "sql": ref.sql}).json()
            evidence.append({"name": ref.name, "sql": ref.sql, "result_preview": {"columns": res["columns"], "rows": res["rows"][:5]}})
    fx = load_fixture("good_submission_working_capital")
    sub = {**fx["submission"], "case_id": cid, "sql_evidence": evidence,
           "metrics": {m.key: m.value for m in world.ground_truth.expected_metrics}}
    g = client.post(f"{API}/cases/{cid}/submit", json=sub)
    assert g.status_code == 200, g.text
    grade = g.json()
    assert grade["deterministic"]["accuracy"] == 1.0
    assert grade["revealed"]["root_cause"] and "reference_sql" not in grade["revealed"]["anomalies"][0]
    assert client.get(f"{API}/cases/{cid}/session").json()["submission_id"] == grade["submission_id"]
