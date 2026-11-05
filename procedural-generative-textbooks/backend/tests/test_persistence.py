"""Round-trip tests for axiom.persistence."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from axiom.models import (
    CaseRecord,
    LLMSettings,
    LLMUsageRecord,
    MasteryRecord,
    SimQuarter,
    SkillEdge,
    SkillNode,
    SqlEvidence,
    Submission,
)
from axiom.persistence import Persistence


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Persistence]:
    p = Persistence(tmp_path / "app" / "axiom.db")
    p.init_schema()
    yield p
    p.close()


def _case(case_id: str = "c1", created_at: str = "2026-01-01T00:00:00+00:00") -> CaseRecord:
    return CaseRecord(
        case_id=case_id, seed=42, archetype="forensic_revenue", difficulty="practitioner",
        company_name="Kestrel Logistics", industry="3pl", pillars=["accounting_finance", "sql"],
        skill_nodes=["af.revenue_recognition", "sql.window_functions"],
        world_path=f"cases/{case_id}/world.json", db_path=f"cases/{case_id}/company.duckdb",
        created_at=created_at,
    )


def test_connection_pragmas(db: Persistence) -> None:
    conn = db.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.row_factory is sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in ["skill_nodes", "skill_edges", "mastery", "cases", "case_artifacts", "submissions",
                 "adversary_turns", "sim_quarters", "llm_settings", "llm_usage"]:
        assert name in tables
    db.init_schema()  # idempotent


def test_skill_nodes_and_edges(db: Persistence) -> None:
    nodes = [
        SkillNode(id="sql.joins", pillar="sql", title="Joins"),
        SkillNode(id="sql.window_functions", pillar="sql", title="Window functions", prereqs=["sql.joins"], p_init=0.1),
    ]
    assert db.upsert_skill_nodes(nodes) == 2
    assert db.list_skill_nodes() == nodes
    db.upsert_skill_nodes([nodes[0].model_copy(update={"title": "SQL joins"})])
    assert db.list_skill_nodes()[0].title == "SQL joins"
    edges = [SkillEdge(src="sql.joins", dst="sql.window_functions")]
    db.replace_skill_edges(edges)
    assert db.list_skill_edges() == edges
    db.replace_skill_edges([])
    assert db.list_skill_edges() == []


def test_mastery(db: Persistence) -> None:
    db.upsert_mastery(MasteryRecord(node_id="a", p_known=0.3, attempts=1, correct=0))
    db.upsert_mastery(MasteryRecord(node_id="b", p_known=0.7, attempts=2, correct=2, last_seen="2026-02-02T00:00:00+00:00"))
    db.upsert_mastery(MasteryRecord(node_id="a", p_known=0.5, attempts=2, correct=1))
    allm = db.get_mastery_all()
    assert set(allm) == {"a", "b"}
    assert allm["a"].p_known == 0.5 and allm["a"].attempts == 2 and allm["a"].last_seen
    assert allm["b"].last_seen == "2026-02-02T00:00:00+00:00"
    assert db.get_mastery("b") == allm["b"]
    assert db.reset_mastery() == 2
    assert db.get_mastery_all() == {} and db.get_mastery("a") is None


def test_cases_and_artifacts(db: Persistence) -> None:
    db.upsert_case(_case("c1", "2026-01-01T00:00:00+00:00"))
    db.upsert_case(_case("c2", "2026-01-02T00:00:00+00:00"))
    got = db.get_case("c1")
    assert got == _case("c1", "2026-01-01T00:00:00+00:00")
    assert db.get_case("nope") is None
    summaries = db.list_cases()
    assert [s.case_id for s in summaries] == ["c2", "c1"]
    assert summaries[0].company_name == "Kestrel Logistics" and summaries[0].pillars == ["accounting_finance", "sql"]
    assert len(db.list_cases(limit=1)) == 1
    assert db.set_case_status("c1", "graded")
    assert db.get_case("c1").status == "graded"  # type: ignore[union-attr]

    payload = {"title": "Dossier", "nested": {"x": [1, 2, 3]}}
    db.save_artifact("c1", "dossier", "", payload)
    assert db.get_artifact("c1", "dossier") == payload
    db.save_artifact("c1", "dossier", "", {"title": "v2"})
    assert db.get_artifact("c1", "dossier") == {"title": "v2"}
    db.save_artifact("c1", "textbook", "sql.joins", {"markdown": "# Joins"})
    assert db.list_artifacts("c1") == [("dossier", ""), ("textbook", "sql.joins")]
    assert db.list_artifacts("c1", "textbook") == [("textbook", "sql.joins")]
    assert db.get_artifact("c1", "textbook", "missing") is None
    assert db.delete_artifact("c1", "textbook", "sql.joins")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_artifact("no_such_case", "dossier", "", {})


def test_submissions_turns_quarters_and_cascade(db: Persistence) -> None:
    db.upsert_case(_case())
    sub = Submission(case_id="c1", memo_markdown="Root cause: channel stuffing", metrics={"dso_days": 61.5},
                     sql_evidence=[SqlEvidence(name="q1", sql="select 1", result_preview=[[1]])])
    db.save_submission("s1", sub)
    rec = db.get_submission("s1")
    assert rec is not None and rec.submitted == sub and rec.grade is None and rec.case_id == "c1"
    db.set_submission_grade("s1", {"accuracy": 0.8})
    assert db.get_submission("s1").grade == {"accuracy": 0.8}  # type: ignore[union-attr]
    db.save_submission("s1", sub)  # re-save keeps grade
    assert db.get_submission("s1").grade == {"accuracy": 0.8}  # type: ignore[union-attr]
    assert [r.submission_id for r in db.list_submissions("c1")] == ["s1"]

    t0 = db.append_adversary_turn("s1", "adversary", {"message": "Why not seasonality?"})
    t1 = db.append_adversary_turn("s1", "user", {"message": "Because DSO..."})
    t5 = db.append_adversary_turn("s1", "adversary", {"message": "ok"}, turn_index=5)
    assert (t0.turn_index, t1.turn_index, t5.turn_index) == (0, 1, 5)
    turns = db.list_turns("s1")
    assert [t.role for t in turns] == ["adversary", "user", "adversary"]
    assert turns[1].payload == {"message": "Because DSO..."} and turns[0].id == t0.id

    q1 = SimQuarter(period="2025Q1", decisions={"price": 1.05}, kpis={"revenue": 1.2e6}, narrative="Up")
    q2 = SimQuarter(period="2025Q2", decisions={"price": 1.1}, kpis={"revenue": 1.25e6})
    db.save_sim_quarter("c1", q1, submission_id="s1")
    db.save_sim_quarter("c1", q2)
    assert db.list_sim_quarters("c1") == [q1, q2]
    assert db.list_sim_quarters("c1", submission_id="s1") == [q1]

    assert db.delete_case("c1")
    assert db.get_submission("s1") is None
    assert db.list_turns("s1") == [] and db.list_sim_quarters("c1") == []


def test_llm_settings_and_usage(db: Persistence) -> None:
    assert db.get_llm_settings() is None
    s = LLMSettings(provider="anthropic", base_url="https://api.anthropic.com", api_key="sk-secret-1234",
                    fast_model="f", deep_model="d")
    db.put_llm_settings(s)
    assert db.get_llm_settings() == s
    db.put_llm_settings(s.model_copy(update={"max_tokens": 2048}))
    assert db.get_llm_settings().max_tokens == 2048  # type: ignore[union-attr]
    assert db.connect().execute("SELECT COUNT(*) FROM llm_settings").fetchone()[0] == 1

    db.record_usage(LLMUsageRecord(model="m1", purpose="dossier", prompt_tokens=100, completion_tokens=50, latency_ms=200, cost_usd_est=0.01))
    db.record_usage(LLMUsageRecord(model="m1", purpose="grade", prompt_tokens=10, completion_tokens=5, latency_ms=100, cost_usd_est=0.002))
    db.record_usage(LLMUsageRecord(model="m2", purpose="adversary", prompt_tokens=1, completion_tokens=1))
    summary = db.usage_summary()
    assert summary.total_tokens == 167
    assert summary.total_cost_usd_est == pytest.approx(0.012)
    assert summary.by_model["m1"].calls == 2 and summary.by_model["m1"].avg_latency_ms == pytest.approx(150)
    assert summary.by_model["m2"].total_tokens == 2
    recent = db.list_usage(limit=2)
    assert [r.model for r in recent] == ["m2", "m1"] and recent[0].ts


def test_thread_safety(db: Persistence) -> None:
    def worker(i: int) -> None:
        for _ in range(25):
            db.record_usage(LLMUsageRecord(model=f"m{i}", purpose="t", prompt_tokens=1))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert db.usage_summary().total_tokens == 100
