"""End-to-end through the API with the real Module 2 generator and the fake LLM.

generate (working_capital_crunch, seed 42, foundation) -> schema -> sql -> python -> submit.
Skipped while Module 2 (``axiom.engine.procedural_world``) is not installed.
"""

from __future__ import annotations

import json
import re
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


# --------------------------------------------------------------------------- leak audit


def _walk(obj: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """``(json_path, leaf)`` for every key and leaf of a JSON value (keys are yielded too)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}/<key>", k
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _number_forms(value: float, unit: str) -> set[str]:
    """Ways an expected metric could be printed; only forms with >= 4 significant digits (shorter
    ones collide with unrelated data such as benchmark percentiles)."""
    forms = {repr(value), f"{value:.4f}", f"{value:.3f}", f"{value:.2f}", f"{value:.1f}", f"{value:,.2f}", f"{value:,.0f}", f"{value:.0f}"}
    if unit == "cents":
        d = value / 100
        forms |= {f"{d:,.0f}", f"{d:.0f}", f"{d:,.2f}", f"{d:.2f}", f"{d / 1e6:.2f}M", f"{d / 1e3:,.0f}k"}
    if unit in ("ratio", "pct"):
        forms |= {f"{value * 100:.2f}%", f"{value * 100:.1f}%", f"{value * 100:.2f}", f"{value:.4f}"}
    return {f for f in forms if sum(c.isdigit() for c in f.lstrip("-0.")) >= 4}


def test_public_case_view_has_no_answer_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug 1 audit: the serialized ``CasePublicView`` of a real working_capital_crunch world (seed 42,
    foundation) contains no concealed string, anomaly id, 5-word root-cause fragment or expected
    metric value (as a number or formatted several ways), except inside ``asks`` keys/labels."""
    procedural_world = pytest.importorskip("axiom.engine.procedural_world")
    monkeypatch.setenv("AXIOM_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        settings = Settings(data_dir=tmp_path / "data")
        settings.ensure_dirs()
        world = procedural_world.generate_world("working_capital_crunch", 42, difficulty="foundation", out_dir=tmp_path / "w")
        services = AppServices(settings, llm_transport=FakeTransport({}), llm_sleep=lambda s: None)
        try:
            public = services.world_service.public(world).model_dump(mode="json")
        finally:
            services.close()
    finally:
        get_settings.cache_clear()

    assert world.dossier_seed.conceal and world.ground_truth.anomalies and world.ground_truth.expected_metrics
    asks = public.pop("asks")
    assert all(set(a) == {"key", "label", "unit", "hint"} for a in asks)
    for stakeholder in public["dossier_seed"]["stakeholders"]:
        assert "hides" not in stakeholder
    text = json.dumps(public)
    for word in ("ground_truth", "conceal", "hides", "reference_sql", "resolved_if", "anomaly_drag", "lever_effects",
                 "derivation", "root_cause", "red_herrings"):
        assert f'"{word}"' not in text, word

    leaks: list[str] = []
    ids = {a.id for a in world.ground_truth.anomalies} | {c for c in world.dossier_seed.conceal if re.fullmatch(r"[A-Z]{1,3}\d{1,3}", c)}
    phrases = [c.lower() for c in world.dossier_seed.conceal if c not in ids]
    words = re.findall(r"[a-z0-9]+(?:[-'.,%][a-z0-9]+)*", world.ground_truth.root_cause.lower())
    shingles = {" ".join(words[i:i + 5]) for i in range(len(words) - 4)}
    metrics = [m for m in world.ground_truth.expected_metrics if m.unit != "bool" and not float(m.value).is_integer()]
    for path, leaf in _walk(public):
        if isinstance(leaf, str):
            low = leaf.lower()
            norm = " ".join(re.findall(r"[a-z0-9]+(?:[-'.,%][a-z0-9]+)*", low))
            leaks += [f"{path}: conceal {p!r}" for p in phrases if p in low]
            leaks += [f"{path}: anomaly id {i!r}" for i in ids if re.search(rf"\b{re.escape(i)}\b", leaf)]
            leaks += [f"{path}: root-cause fragment {sh!r}" for sh in shingles if sh in norm]
        elif isinstance(leaf, (int, float)) and not isinstance(leaf, bool):
            leaks += [f"{path}: value of {m.key}" for m in metrics
                      if abs(float(leaf) - m.value) <= 1e-6 * max(1.0, abs(m.value))]
    for m in metrics:
        for form in _number_forms(m.value, m.unit):
            hit = re.search(rf"(?<![\d.]){re.escape(form)}(?!\d)", text)
            if hit:
                leaks.append(f"{m.key} printed as {form!r}: ...{text[max(0, hit.start() - 60):hit.end() + 10]}...")
    assert not leaks, "\n".join(leaks)
    # the levers stay public (section 12.1), with their effect texts
    assert [lv["key"] for lv in public["levers"]] == [lv.key for lv in world.ground_truth.decision_levers]
