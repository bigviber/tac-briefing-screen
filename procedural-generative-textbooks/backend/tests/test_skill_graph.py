"""Tests for axiom.skill_graph: DAG loading, BKT math, decay, observations, selection, ELO."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from axiom.engine.rng import SeededRNG
from axiom.models import (
    ArchetypeInfo,
    DeterministicGrade,
    GradeReport,
    MetricResult,
    QuerySignatureMatch,
    RevealedTruth,
    RubricDimension,
    RubricGrade,
    SkillEdge,
    SkillNode,
)
from axiom.persistence import Persistence
from axiom.skill_graph import (
    BKTParams,
    DAGError,
    EloTracker,
    SkillGraph,
    apply_decay,
    bkt_predict_correct,
    bkt_update,
    info_gain,
)
from tests.world_factory import make_minimal_world

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)
CORE = BKTParams(0.20, 0.15, 0.15, 0.12)


@pytest.fixture()
def db() -> Persistence:
    p = Persistence(":memory:")
    p.init_schema()
    return p


@pytest.fixture()
def graph(db: Persistence) -> SkillGraph:
    return SkillGraph.load(persistence=db, clock=lambda: NOW)


def catalog(graph: SkillGraph, keys: list[str] | None = None) -> list[ArchetypeInfo]:
    keys = keys or sorted(graph.archetype_map)
    return [ArchetypeInfo(key=k, title=k, skill_nodes=graph.archetype_nodes(k),
                          difficulty_range=["foundation", "practitioner", "principal"]) for k in keys]


def grade(te: float, bj: float, comm: float, risk: float, *, misdiagnosis: bool = False,
          metrics: list[tuple[str | None, bool]] = (), queries: list[tuple[str | None, bool]] = ()) -> GradeReport:
    dims = {"technical_execution": te, "business_judgment": bj, "communication": comm, "risk_awareness": risk}
    return GradeReport(
        submission_id="s1",
        deterministic=DeterministicGrade(
            metric_results=[MetricResult(key=f"m{i}", expected=1.0, within_tolerance=ok, pillar=p) for i, (p, ok) in enumerate(metrics)],
            query_signature_matches=[QuerySignatureMatch(name=f"q{i}", matched=ok, pillar=p) for i, (p, ok) in enumerate(queries)],
        ),
        rubric=RubricGrade(dimensions={k: RubricDimension(score=v) for k, v in dims.items()}, misdiagnosis=misdiagnosis),
        revealed=RevealedTruth(root_cause="x"),
    )


# --------------------------------------------------------------------------- DAG


def test_dag_loads_65_nodes_and_syncs(graph: SkillGraph, db: Persistence) -> None:
    assert len(graph.nodes) == 65
    assert len(graph.edges) == 73
    assert len(db.list_skill_nodes()) == 65 and len(db.list_skill_edges()) == 73
    assert {n.pillar for n in graph.nodes.values()} == {"accounting_finance", "operations_strategy", "system_design", "sql", "python"}
    assert graph.prereqs("accounting_finance.working_capital") == ["accounting_finance.accrual_vs_cash", "accounting_finance.three_statements"]
    assert graph.pillar_of("sql.window_functions") == "sql"
    assert graph.archetype_nodes("outbox_pattern")[0] == "system_design.outbox_saga"
    assert graph.nodes["accounting_finance.double_entry"].p_init == 0.35  # foundation preset


def test_dag_is_acyclic_topological(graph: SkillGraph) -> None:
    pos = {n: i for i, n in enumerate(graph.topological_order)}
    assert len(pos) == 65
    assert all(pos[e.src] < pos[e.dst] for e in graph.edges)


def test_cycle_and_degenerate_params_rejected() -> None:
    a = SkillNode(id="a", pillar="sql", title="A")
    b = SkillNode(id="b", pillar="sql", title="B")
    with pytest.raises(DAGError, match="cycle"):
        SkillGraph([a, b], [SkillEdge(src="a", dst="b"), SkillEdge(src="b", dst="a")])
    with pytest.raises(DAGError, match="unknown"):
        SkillGraph([a], [SkillEdge(src="a", dst="zzz")])
    bad = SkillNode(id="c", pillar="sql", title="C", p_slip=0.3, p_guess=0.45)
    SkillGraph([bad], [])  # S+G < 1 and both < 0.5: fine
    with pytest.raises(DAGError, match="degenerate"):
        SkillGraph([SkillNode(id="d", pillar="sql", title="D", p_guess=0.6)], [])


# --------------------------------------------------------------------------- BKT math


def test_bkt_update_hand_computed() -> None:
    # correct: post = .2*.85 / (.2*.85 + .8*.12) = .17/.266; next = post + (1-post)*.15
    post = 0.17 / 0.266
    assert bkt_update(0.2, True, CORE) == pytest.approx(post + (1 - post) * 0.15)
    assert bkt_update(0.2, True, CORE) == pytest.approx(0.693233, abs=1e-6)
    # incorrect: post = .2*.15 / (.03 + .8*.88) = .03/.734
    post = 0.03 / 0.734
    assert bkt_update(0.2, False, CORE) == pytest.approx(post + (1 - post) * 0.15)
    assert bkt_update(0.2, False, CORE) == pytest.approx(0.184741, abs=1e-6)
    # borderline: slip raised to min(.45, .15 + .15) = .30
    post = 0.06 / (0.06 + 0.704)
    assert bkt_update(0.2, False, CORE, slip_adjust=0.15) == pytest.approx(post + (1 - post) * 0.15)
    adv = BKTParams(0.10, 0.10, 0.20, 0.08)
    # slip cap: .20 + .30 -> capped at .45
    post = 0.1 * 0.45 / (0.1 * 0.45 + 0.9 * 0.92)
    assert bkt_update(0.1, False, adv, slip_adjust=0.30) == pytest.approx(post + (1 - post) * 0.10)
    # clamp
    assert bkt_update(0.99999, True, BKTParams(0.5, 0.9, 0.01, 0.01)) == pytest.approx(1 - 1e-4)
    assert bkt_predict_correct(0.2, CORE) == pytest.approx(0.2 * 0.85 + 0.8 * 0.12)


def test_info_gain_prefers_middle() -> None:
    assert info_gain(0.5, CORE) > info_gain(0.05, CORE)
    assert info_gain(0.5, CORE) > info_gain(0.97, CORE)
    assert info_gain(0.5, CORE) > 0


def test_graph_update_persists(graph: SkillGraph, db: Persistence) -> None:
    upd = graph.update("sql.joins", True)
    assert upd.before == pytest.approx(0.2) and upd.after == pytest.approx(0.693233, abs=1e-6)
    row = db.get_mastery("sql.joins")
    assert row is not None and row.attempts == 1 and row.correct == 1 and row.p_known == pytest.approx(upd.after)
    graph2 = SkillGraph.load(persistence=db, clock=lambda: NOW)
    assert graph2.mastery("sql.joins") == pytest.approx(upd.after)
    graph2.reset()
    assert db.get_mastery_all() == {} and graph2.mastery("sql.joins") == pytest.approx(0.2)


# --------------------------------------------------------------------------- decay


def test_decay_spec_example(db: Persistence) -> None:
    g = SkillGraph.load(persistence=db, clock=lambda: NOW)
    g.set_mastery("sql.joins", 0.9, last_seen=(NOW - dt.timedelta(days=60)).isoformat())
    g.set_mastery("sql.ctes", 0.9, last_seen=(NOW - dt.timedelta(days=10)).isoformat())
    g.set_mastery("sql.window_functions", 0.9, last_seen=(NOW - dt.timedelta(days=31)).isoformat())
    # 0.2 + 0.7 * max(0.5, 1 - 0.015 * 39) = 0.55
    assert g.mastery("sql.joins") == pytest.approx(0.55)
    assert g.mastery("sql.ctes") == pytest.approx(0.9)
    assert g.mastery("sql.window_functions") == pytest.approx(0.2 + 0.7 * (1 - 0.015 * 10))
    assert g.mastery("sql.joins", decayed=False) == pytest.approx(0.9)  # never persisted
    assert db.get_mastery("sql.joins").p_known == pytest.approx(0.9)  # type: ignore[union-attr]
    assert g.is_decayed("sql.joins") and not g.is_decayed("sql.ctes")
    assert g.decay(NOW + dt.timedelta(days=30))["sql.ctes"] < 0.9
    assert apply_decay(0.1, 0.2, NOW - dt.timedelta(days=100), NOW) == pytest.approx(0.15)  # toward the prior


# --------------------------------------------------------------------------- eligibility


def test_eligibility_respects_cross_pillar_prereqs(graph: SkillGraph) -> None:
    node = "operations_strategy.unit_economics"
    assert graph.prereqs(node) == ["accounting_finance.three_statements"]
    assert node not in graph.eligible_nodes()
    graph.set_mastery("accounting_finance.three_statements", 0.59)
    assert node not in graph.eligible_nodes()
    graph.set_mastery("accounting_finance.three_statements", 0.60)
    assert node in graph.eligible_nodes()
    assert "accounting_finance.double_entry" in graph.eligible_nodes()  # no prereqs
    # system_design.cost_of_architecture needs a system_design AND an accounting node
    target = "system_design.cost_of_architecture"
    graph.set_mastery("system_design.failure_mode_analysis", 0.9)
    assert target in graph.eligible_nodes()
    graph.set_mastery("accounting_finance.three_statements", 0.3)
    assert target not in graph.eligible_nodes()


# --------------------------------------------------------------------------- observations


def test_node_scores_follow_spec_table(graph: SkillGraph) -> None:
    world = make_minimal_world("outbox_pattern")  # system_design x3, sql, accounting_finance
    g = grade(8, 7, 6, 9, metrics=[("sql", True), ("sql", False), (None, True)], queries=[("system_design", True)])
    scores = graph.node_scores_from_grade(g, world)
    det_all = 3 / 4
    assert scores["sql.reconciliation_queries"] == pytest.approx(0.6 * 0.5 + 0.4 * 0.8)
    assert scores["accounting_finance.subledger_reconciliation"] == pytest.approx(0.5 * det_all + 0.3 * 0.7 + 0.2 * 0.8)
    assert scores["system_design.outbox_saga"] == pytest.approx(0.3 * det_all + 0.3 * 0.8 + 0.4 * 0.9)
    world_ops = make_minimal_world("toc_bottleneck")
    s2 = graph.node_scores_from_grade(g, world_ops)
    assert s2["operations_strategy.littles_law"] == pytest.approx(0.4 * det_all + 0.4 * 0.7 + 0.2 * 0.9)
    assert s2["python.visualization"] == pytest.approx(0.8)  # no python-tagged items -> te/10


def test_misdiagnosis_cap_and_borderline_band(graph: SkillGraph) -> None:
    world = make_minimal_world("working_capital_crunch")
    capped = graph.observations_from_grade(grade(10, 10, 10, 10, misdiagnosis=True, metrics=[(None, True)]), world)
    assert all(o.score <= 0.45 and o.outcome == "incorrect" and not o.correct for o in capped)
    # af: 0.5*0.5 + 0.3*0.6 + 0.2*0.6 = 0.55 -> borderline; sql/python: te = 0.6 -> borderline
    obs = {o.node: o for o in graph.observations_from_grade(grade(6, 6, 6, 6, metrics=[(None, True), (None, False)]), world)}
    af = obs["accounting_finance.working_capital"]
    assert af.score == pytest.approx(0.55) and af.outcome == "borderline" and af.slip_adjust == pytest.approx(0.15)
    assert obs["sql.running_balances"].outcome == "borderline"
    hi = {o.node: o for o in graph.observations_from_grade(grade(7, 7, 7, 7, metrics=[(None, True)]), world)}
    assert all(o.outcome == "correct" for o in hi.values())
    lo = graph.observations_from_grade(grade(4, 4, 4, 4, metrics=[(None, False)]), world)
    assert all(o.outcome == "incorrect" and o.slip_adjust == 0 for o in lo)


def test_apply_grade_uses_softened_slip_for_borderline(graph: SkillGraph) -> None:
    world = make_minimal_world("working_capital_crunch")
    updates = {u.node: u for u in graph.apply_grade(grade(6, 6, 6, 6, metrics=[(None, True), (None, False)]), world)}
    u = updates["accounting_finance.working_capital"]
    assert u.outcome == "borderline"
    assert u.after == pytest.approx(bkt_update(0.2, False, graph.params(u.node), slip_adjust=0.15), abs=1e-6)
    assert u.after > bkt_update(0.2, False, graph.params(u.node))
    assert len(updates) == 5


# --------------------------------------------------------------------------- selection


def _set_all(graph: SkillGraph, p: float) -> None:
    for nid in graph.nodes:
        graph.set_mastery(nid, p)


def test_select_case_prefers_lowest_mastery_eligible(graph: SkillGraph) -> None:
    _set_all(graph, 0.8)
    for nid in graph.archetype_nodes("forensic_revenue"):
        graph.set_mastery(nid, 0.25)
    pick = graph.select_case(catalog(graph), SeededRNG(7), recent_cases=[])
    assert pick["archetype"] == "forensic_revenue"
    # forensic_detection's prereq revenue_recognition is at 0.25 -> locked, so it is not usable
    assert "accounting_finance.forensic_detection" not in pick["skill_nodes"]
    assert len(pick["skill_nodes"]) == 4
    assert pick["difficulty"] == "foundation"
    assert "forensic_revenue" in pick["rationale"]


def test_select_case_deterministic_for_seed(graph: SkillGraph) -> None:
    _set_all(graph, 0.7)
    cat = catalog(graph)
    picks_a = [graph.select_case(cat, SeededRNG(s), recent_cases=[])["archetype"] for s in range(20)]
    picks_b = [graph.select_case(cat, SeededRNG(s), recent_cases=[])["archetype"] for s in range(20)]
    assert picks_a == picks_b
    # A different seed only changes tie-breaks: every pick is one of the top-scoring archetypes.
    cands = graph.select_case(cat, SeededRNG(0), recent_cases=[])["candidates"]
    top = {c["archetype"] for c in cands if abs(c["score"] - cands[0]["score"]) < 1e-6}
    assert set(picks_a) <= top


def test_select_case_tie_broken_by_rng(graph: SkillGraph) -> None:
    _set_all(graph, 0.7)
    twins = [ArchetypeInfo(key=k, title=k, skill_nodes=["sql.joins", "sql.ctes"]) for k in ("twin_a", "twin_b")]
    picks = {graph.select_case(twins, SeededRNG(s), recent_cases=[])["archetype"] for s in range(30)}
    assert picks == {"twin_a", "twin_b"}
    assert graph.select_case(twins, SeededRNG(3), recent_cases=[])["archetype"] == graph.select_case(twins, SeededRNG(3), recent_cases=[])["archetype"]


def test_recency_penalty(graph: SkillGraph) -> None:
    _set_all(graph, 0.8)
    for nid in graph.archetype_nodes("forensic_revenue"):
        graph.set_mastery(nid, 0.7)
    cat = catalog(graph)
    base = graph.select_case(cat, SeededRNG(1), recent_cases=[])
    assert base["archetype"] == "forensic_revenue"
    pen = graph.select_case(cat, SeededRNG(1), recent_cases=["forensic_revenue", "bullwhip"])
    assert pen["archetype"] != "forensic_revenue"
    before = {c["archetype"]: c["score"] for c in base["candidates"]}
    after = {c["archetype"]: c["score"] for c in pen["candidates"]}
    assert after["forensic_revenue"] == pytest.approx(before["forensic_revenue"] - 0.5)
    assert after["bullwhip"] == pytest.approx(before["bullwhip"] - 0.5)
    # only the last three recent archetypes are penalised
    old = graph.select_case(cat, SeededRNG(1), recent_cases=["forensic_revenue", "a", "b", "c"])
    assert old["archetype"] == "forensic_revenue"


def test_select_case_targets_difficulty_and_cold_start(graph: SkillGraph) -> None:
    cold = graph.select_case(catalog(graph), SeededRNG(1), recent_cases=[])
    assert all(n in graph.eligible_nodes() for n in cold["skill_nodes"])
    _set_all(graph, 0.7)
    t = graph.select_case(catalog(graph), SeededRNG(1), target_skills=["system_design.outbox_saga"], recent_cases=[])
    assert t["archetype"] == "outbox_pattern"
    d = graph.select_case(catalog(graph), SeededRNG(1), difficulty="principal", recent_cases=[])
    assert d["difficulty"] == "principal"
    narrow = [ArchetypeInfo(key="outbox_pattern", title="o", skill_nodes=graph.archetype_nodes("outbox_pattern"), difficulty_range=["foundation"])]
    assert graph.select_case(narrow, SeededRNG(1), difficulty="principal", recent_cases=[])["difficulty"] == "foundation"


# --------------------------------------------------------------------------- views + ELO


def test_pillar_summary(graph: SkillGraph) -> None:
    graph.set_mastery("sql.joins", 0.97)
    summary = graph.pillar_summary()
    assert set(summary) == {"accounting_finance", "operations_strategy", "system_design", "sql", "python"}
    assert sum(v["nodes"] for v in summary.values()) == 65
    assert summary["sql"]["mastered"] == 1
    assert 0 < summary["sql"]["mean_mastery"] < 1
    resp = graph.graph_response()
    assert len(resp.nodes) == 65 and len(resp.edges) == 73


def test_elo_tracker() -> None:
    elo = EloTracker()
    assert EloTracker.case_rating("principal", 3) == 1600
    e = EloTracker.expected(1200, 1300)
    assert e == pytest.approx(1 / (1 + 10 ** (100 / 400)))
    upd = elo.observe("sql.joins", 1.0, difficulty="practitioner")
    assert elo.rating("sql.joins") == pytest.approx(1200 + 40 * (1 - e))
    assert upd.after > upd.before
    for _ in range(4):
        elo.observe("sql.joins", 0.5)
    r = elo.rating("sql.joins")
    elo.observe("sql.joins", 1.0)  # sixth observation uses K = 20
    assert elo.rating("sql.joins") == pytest.approx(r + 20 * (1 - EloTracker.expected(r, 1300)))
    assert elo.mastery("unseen") == pytest.approx(1 / (1 + 10 ** (300 / 400)))
    assert math.isclose(EloTracker().mastery("x"), 1 / (1 + 10 ** 0.75))


def test_metric_pillar_rules() -> None:
    from axiom.skill_graph import metric_pillar

    world = make_minimal_world("outbox_pattern")
    assert metric_pillar("sql.unbilled_rows", world) == "sql"
    assert metric_pillar("af:ar_breaks", world) == "accounting_finance"
    assert metric_pillar("python_sim_p90", world) == "python"
    assert metric_pillar("unbilled_usage_cents", world) == "accounting_finance"  # A2 magnitude key
    assert metric_pillar("dlq_depth", world) is None  # two anomalies, not linked
    em = world.ground_truth.expected_metrics[-1]
    if "pillar" in type(em).model_fields:  # explicit tag (ExpectedMetric.pillar) wins
        em.pillar = "system_design"
        assert metric_pillar("dlq_depth", world) == "system_design"
