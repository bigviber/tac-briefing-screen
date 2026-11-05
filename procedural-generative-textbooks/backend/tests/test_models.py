"""Tests for axiom.models."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from pydantic import ValidationError

from axiom.models import (
    Anomaly,
    Benchmark,
    CasePublic,
    CaseWorld,
    CompanyProfile,
    DatabaseSpec,
    DossierSeed,
    ExpectedMetric,
    GroundTruth,
    IndustryProfile,
    JournalEntry,
    JournalLine,
    LLMSettings,
    LLMSettingsPublic,
    ReferenceQuery,
    RubricDimension,
    SimulationState,
    Stakeholder,
    TableSpec,
    mask_api_key,
)


def make_world() -> CaseWorld:
    bench = Benchmark(key="saas.dso_days", industry="saas", metric="DSO", unit="days",
                      p10=30, p25=40, p50=50, p75=60, p90=80, source="test")
    return CaseWorld(
        case_id="case_1", seed=7, archetype="forensic_revenue", version="1",
        pillars=["accounting_finance", "sql"], skill_nodes=["af.rev_rec"], difficulty="practitioner",
        company=CompanyProfile(name="Kestrel Software", ticker_like="KES", industry_code="saas",
                               business_model="subscription", hq="Austin, TX", founded=2015, headcount=210,
                               stage="growth", revenue_ttm_cents=4_200_000_000),
        industry=IndustryProfile(code="saas", name="SaaS", benchmarks={bench.key: bench}),
        periods=["2024Q1", "2024Q2"],
        database=DatabaseSpec(path="cases/case_1/company.duckdb", tables=[
            TableSpec(name="invoices", ddl="CREATE TABLE invoices (id VARCHAR)", row_count=10,
                      foreign_keys=[("customer_id", "customers", "customer_id")]),
        ]),
        ground_truth=GroundTruth(
            root_cause="Quarter-end channel stuffing by the EMEA sales team.",
            anomalies=[Anomaly(id="A1", kind="channel_stuffing", pillar="accounting_finance", title="Stuffing",
                               mechanism="pulled forward 11% of Q2 shipments",
                               reference_sql=[ReferenceQuery(name="q", sql="select 1", expected={"rows": 1})])],
            expected_metrics=[ExpectedMetric(key="dso_days", value=61.2, unit="days", tolerance_abs=1.0)],
        ),
        dossier_seed=DossierSeed(reveal=["DSO rose"], conceal=["channel stuffing", "A1"],
                                 stakeholders=[Stakeholder(name="Ana", role="CFO")]),
        simulation=SimulationState(current_period="2024Q2"),
        generated_at="2026-01-01T00:00:00Z",
    )


def test_public_view_strips_ground_truth_and_conceal() -> None:
    world = make_world()
    public = world.public_view()
    assert "ground_truth" not in public
    assert "conceal" not in public["dossier_seed"]
    assert public["dossier_seed"]["reveal"] == ["DSO rose"]
    text = json.dumps(public)
    assert "channel stuffing" not in text.lower() and "Quarter-end" not in text
    typed = world.to_public()
    assert isinstance(typed, CasePublic)
    assert typed.company.name == "Kestrel Software"
    assert not hasattr(typed, "ground_truth")
    # every other top-level field survives
    full = world.model_dump(mode="json")
    assert set(public) == set(full) - {"ground_truth"}


def test_case_world_json_round_trip() -> None:
    world = make_world()
    text = world.model_dump_json()
    again = CaseWorld.model_validate_json(text)
    assert again == world
    assert again.model_dump_json() == text
    assert again.database.tables[0].foreign_keys == [("customer_id", "customers", "customer_id")]


def test_world_models_forbid_extra() -> None:
    data = make_world().model_dump(mode="json")
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        CaseWorld.model_validate(data)
    pub = make_world().public_view()
    pub["ground_truth"] = {"root_cause": "x"}
    with pytest.raises(ValidationError):
        CasePublic.model_validate(pub)


def test_journal_entry_must_balance() -> None:
    ok = JournalEntry(id="J1", date=dt.date(2024, 1, 31), period="2024Q1", memo="sale", source="sales",
                      lines=[JournalLine(account="1100", debit_cents=500), JournalLine(account="4000", credit_cents=500)])
    assert ok.date == dt.date(2024, 1, 31)
    with pytest.raises(ValidationError):
        JournalEntry(id="J2", date=dt.date(2024, 1, 31), period="2024Q1", memo="bad", source="sales",
                     lines=[JournalLine(account="1100", debit_cents=500), JournalLine(account="4000", credit_cents=499)])
    with pytest.raises(ValidationError):
        JournalLine(account="1100", debit_cents=-1)


def test_benchmark_percentiles_must_be_monotone() -> None:
    with pytest.raises(ValidationError):
        Benchmark(key="k", metric="m", unit="x", p10=5, p25=4, p50=6, p75=7, p90=8, source="s")


def test_llm_settings_masking() -> None:
    assert mask_api_key("") == ""
    assert mask_api_key("abc") == "****"
    s = LLMSettings(api_key="sk-or-v1-abcdefgh1234")
    pub = LLMSettingsPublic.from_settings(s)
    assert pub.api_key == "****1234" and pub.api_key_set
    assert "abcdefgh" not in pub.model_dump_json()


def test_rubric_dimension_bounds() -> None:
    RubricDimension(score=10)
    with pytest.raises(ValidationError):
        RubricDimension(score=11)


def test_skill_node_from_dag_record() -> None:
    from axiom.models import SkillNode

    node = SkillNode.from_dag_record({
        "id": "sql.joins", "pillar": "sql", "title": "Joins", "tier": "foundation", "prereqs": [],
        "summary": "Inner and outer joins.", "mental_models": ["set algebra"], "archetypes": ["toc_bottleneck"],
        "bkt": {"p_init": 0.35, "p_transit": 0.25, "p_slip": 0.1, "p_guess": 0.15}, "future_field": 1,
    })
    assert node.description == "Inner and outer joins." and node.p_init == 0.35 and node.tier == "foundation"
