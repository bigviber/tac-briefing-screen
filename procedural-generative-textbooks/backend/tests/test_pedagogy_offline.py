"""Offline pedagogy tests (FakeTransport; synthetic worlds from tests/world_factory.py)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from axiom.engine.llm_client import FakeTransport, LLMClient
from axiom.engine.pedagogy import (
    ChapterLockedError,
    Pedagogy,
    SimulationLimitError,
    extract_numbers,
    leak_scan,
    next_period,
    number_matches,
    rung_for,
    simulate_transition,
    te_ceiling,
)
from axiom.models import CaseWorld, LLMSettings, Submission
from axiom.persistence import Persistence
from axiom.skill_graph import SkillGraph, bkt_update
from tests.world_factory import load_fixture, make_minimal_world

CONCEAL = "the 2024Q3 move to 90-day payment terms for the top-20 accounts"


# --------------------------------------------------------------------------- helpers


def dossier_payload(situation: str = "Cash fell to $1.2M at the end of 2024Q4 despite revenue growth.", body: str = "We need answers before the lender call.") -> dict[str, Any]:
    return {
        "title": "Growing Into a Cash Squeeze",
        "situation": situation,
        "company_background": "Kestrel Supply Co. is a regional B2B distributor of packaging supplies in Columbus, OH.",
        "stakeholder_artifacts": [
            {"stakeholder": "Dana Okafor", "role": "CFO", "format": "email", "subject": "Liquidity", "body": body},
            {"stakeholder": "Luis Ortiz", "role": "COO", "format": "slack", "subject": "", "body": "Inventory is flat; it is not the warehouse."},
        ],
        "timeline": [{"period": "2024Q4", "headline": "Warehouse expansion", "detail": "second shift"}],
        "the_ask": "Why is cash falling while revenue grows? The revolver covenant requires minimum liquidity.",
        "deliverable_spec": "- An executive memo\n- `dso_latest` (days)\n- SQL evidence",
        "data_room_index": [{"table": "invoices", "note": "one row per invoice"}, "customers"],
        "architecture_mermaid": None,
    }


def chapter_payload(node: str = "accounting_finance.working_capital") -> dict[str, Any]:
    return {
        "node_id": node, "title": "Working Capital from First Principles",
        "chapter_markdown": "## Why this exists\nCash and profit diverge. Revenue was 2,100,000,000 cents in 2024Q4.",
        "self_check": [{"question": "What is DSO?", "answer": "Receivables over revenue per day."}],
        "snippets": [{"language": "sql", "purpose": "dedup", "code": "SELECT 1"}],
    }


def adversary_reply(message: str = "Which period is your DSO from?") -> dict[str, Any]:
    return {"persona": "whoever", "message": message, "challenge_type": "clarify", "cites": ["dso_latest", "made_up_metric"],
            "concedes": True, "rung_advanced": True}


NARRATIVE = (
    "**Headline.** Collections recovered after the terms change.\n\n"
    "We cut terms to 45 days and offered a 1% early-pay discount. DSO fell from 68 to 38 days (-30). "
    "Cash held at $1.2M. Revenue will grow 17% next year.\n\n"
    "We watch DSO next quarter."
)


class Env:
    """Persistence + graph + fake-transport client + Pedagogy."""

    def __init__(self, replies: Any, provider: str = "openai") -> None:
        self.db = Persistence(":memory:")
        self.db.init_schema()
        self.graph = SkillGraph.load(persistence=self.db)
        self.transport = FakeTransport(replies)
        base = "https://api.anthropic.com" if provider == "anthropic" else "https://api.openai.com/v1"
        settings = LLMSettings(provider=provider, base_url=base, api_key="sk-test-0000", fast_model="gpt-4o-mini", deep_model="gpt-4o")  # type: ignore[arg-type]
        self.client = LLMClient(settings, self.db, transport=self.transport, sleep=lambda s: None)
        ids = iter(f"sub_{i}" for i in range(100))
        self.ped = Pedagogy(self.client, self.graph, self.db, id_factory=lambda: next(ids))

    def purposes(self) -> list[str]:
        return [r["purpose"] for r in self.transport.requests]


def fixture_submission(name: str, world: CaseWorld) -> tuple[Submission, dict[str, Any]]:
    fx = load_fixture(name)
    sub = Submission.model_validate({**fx["submission"], "case_id": world.case_id})
    expected = {m.key: m.value for m in world.ground_truth.expected_metrics}
    if name == "good_submission_working_capital":
        sub.metrics = dict(expected)  # harness: within tolerance
    elif name == "bad_submission_forensic_revenue":
        sub.metrics = {k: v * 1.35 for k, v in expected.items()}  # harness: deliberately outside tolerance
    return sub, fx


def wc_submission(world: CaseWorld, **metrics: float) -> Submission:
    sub, _ = fixture_submission("good_submission_working_capital", world)
    sub.metrics.update(metrics)
    return sub


# --------------------------------------------------------------------------- deterministic grade


def test_deterministic_metric_tolerances() -> None:
    env = Env({})
    world = make_minimal_world()
    ok = env.ped.grade_deterministic(world, wc_submission(world, dso_latest=70.0, cash_trapped_in_ar_cents=598_500_000))
    assert all(r.within_tolerance for r in ok.metric_results)
    assert ok.accuracy == 1.0 and ok.det_pass == 1.0 and ok.items_total == 4
    bad = env.ped.grade_deterministic(world, wc_submission(world, dso_latest=70.01, cash_trapped_in_ar_cents=600_000_000))
    res = {r.key: r for r in bad.metric_results}
    assert not res["dso_latest"].within_tolerance and not res["cash_trapped_in_ar_cents"].within_tolerance
    assert res["dso_baseline"].within_tolerance
    assert bad.accuracy == pytest.approx(1 / 3)
    missing = wc_submission(world)
    del missing.metrics["dso_baseline"]
    r = {m.key: m for m in env.ped.grade_deterministic(world, missing).metric_results}["dso_baseline"]
    assert r.submitted is None and not r.within_tolerance and r.derivation == "2024Q1 DSO"
    assert r.pillar == "accounting_finance" and r.anomaly_ids == ["A1"]


def test_query_signature_heuristic() -> None:
    env = Env({})
    world = make_minimal_world()
    det = env.ped.grade_deterministic(world, wc_submission(world))
    q = det.query_signature_matches[0]
    assert q.matched and q.method == "signature" and q.pillar == "accounting_finance"
    no_sql = wc_submission(world)
    no_sql.sql_evidence = []
    q2 = env.ped.grade_deterministic(world, no_sql).query_signature_matches[0]
    assert not q2.matched and "row_number()" in q2.detail


def test_query_execution_path_with_sql_runner() -> None:
    env = Env({})
    world = make_minimal_world()
    sub = wc_submission(world)  # fixture previews: first data row "2024Q1 | ... | ... | 41.2"
    calls: list[str] = []

    def runner(sql: str) -> dict[str, Any]:
        calls.append(sql)
        return {"columns": ["period", "dso_days"], "rows": [["2024Q1", 41.5]]}

    q = env.ped.grade_deterministic(world, sub, sql_runner=runner).query_signature_matches[0]
    assert calls and q.method == "execution" and q.matched  # 41.2 within 2% of 41.5
    q_bad = env.ped.grade_deterministic(world, sub, sql_runner=lambda s: {"rows": [[55.0]]}).query_signature_matches[0]
    assert q_bad.method == "execution" and not q_bad.matched

    def boom(sql: str) -> dict[str, Any]:
        raise RuntimeError("db offline")

    q_fb = env.ped.grade_deterministic(world, sub, sql_runner=boom).query_signature_matches[0]
    assert q_fb.method == "signature" and q_fb.matched


# --------------------------------------------------------------------------- full grade on fixtures


def test_good_working_capital_fixture() -> None:
    world = make_minimal_world("working_capital_crunch")
    sub, fx = fixture_submission("good_submission_working_capital", world)
    env = Env({"grader": fx["fake_rubric"], "adversary": adversary_reply()})
    priors = {n: env.graph.mastery(n) for n in world.skill_nodes}
    report = env.ped.grade(world, sub)
    assert report.deterministic.det_pass >= fx["expected_deterministic"]["det_pass_min"]
    rng = fx["expected_rubric_range"]
    assert rng["overall_min"] <= report.rubric.overall <= rng["overall_max"]
    assert report.rubric.misdiagnosis is rng["misdiagnosis"]
    assert report.rubric.overall == 8.9  # 0.35*9 + 0.35*9 + 0.15*8 + 0.15*9 = 8.85 -> half-up
    obs = {o.node: o.outcome for o in report.observations}
    assert obs == fx["expected_observations"]
    assert {u.node for u in report.skill_updates} == set(world.skill_nodes)
    assert all(u.after > priors[u.node] for u in report.skill_updates)
    assert report.revealed.root_cause == world.ground_truth.root_cause
    assert report.adversary_opening == "Which period is your DSO from?"
    assert env.purposes() == ["grader", "adversary"]
    stored = env.db.get_submission(report.submission_id)
    assert stored is not None and stored.grade is not None and stored.grade["rubric"]["overall"] == 8.9
    assert env.db.get_case(world.case_id).status == "graded"  # type: ignore[union-attr]


def test_bad_forensic_fixture() -> None:
    world = make_minimal_world("forensic_revenue")
    sub, fx = fixture_submission("bad_submission_forensic_revenue", world)
    env = Env({"grader": fx["fake_rubric"], "adversary": adversary_reply()})
    priors = {n: env.graph.mastery(n) for n in world.skill_nodes}
    report = env.ped.grade(world, sub)
    assert report.deterministic.det_pass <= fx["expected_deterministic"]["det_pass_max"]
    assert report.rubric.misdiagnosis is True
    assert fx["expected_rubric_range"]["overall_min"] <= report.rubric.overall <= fx["expected_rubric_range"]["overall_max"]
    assert report.rubric.ceiling_te == 3.0
    assert all(o.score <= fx["expected_node_score_max"] for o in report.observations)
    assert {o.node: o.outcome for o in report.observations} == fx["expected_observations"]
    for u in report.skill_updates:
        assert u.after == pytest.approx(bkt_update(priors[u.node], False, env.graph.params(u.node)), abs=1e-6)
        if env.graph.nodes[u.node].tier != "advanced":
            assert u.after < priors[u.node]
    # Spec note: for advanced nodes (p_init 0.10, T 0.10) a miss still raises p (fixed point above
    # the prior), so "p strictly decreases" in pedagogy.md section 8 holds only for core nodes.
    adv = next(u for u in report.skill_updates if u.node == "accounting_finance.forensic_detection")
    assert adv.after > adv.before


def test_good_outbox_fixture() -> None:
    world = make_minimal_world("outbox_pattern")
    sub, fx = fixture_submission("good_submission_outbox", world)
    env = Env({"grader": fx["fake_rubric"], "adversary": adversary_reply()})
    report = env.ped.grade(world, sub)
    det = report.deterministic
    assert det.det_pass >= fx["expected_deterministic"]["det_pass_min"]
    failed_af = [r for r in det.metric_results if r.pillar == "accounting_finance" and not r.within_tolerance]
    assert len(failed_af) == fx["expected_deterministic"]["tagged_failures"]["accounting_finance"]
    # ceiling: det_pass 5/6 -> 8; raw LLM 9 kept alongside the clamped 8
    assert report.rubric.raw_scores["technical_execution"] == 9
    assert report.rubric.dimensions["technical_execution"].score == 8
    assert report.rubric.overall == 8.0
    rng = fx["expected_rubric_range"]
    assert rng["overall_min"] <= report.rubric.overall <= rng["overall_max"]
    obs = {o.node: o for o in report.observations}
    for node in ("system_design.outbox_saga", "system_design.event_driven", "system_design.observability", "sql.reconciliation_queries"):
        assert obs[node].outcome == "correct"
    # Spec-literal: af score = 0.5*det_all + 0.3*bj + 0.2*te = 0.5*(5/6) + 0.24 + 0.16 = 0.8167.
    # The fixture expects the 0.50-0.65 borderline band, which the section 2.2 formula cannot
    # produce with det_pass >= 0.7 (reported as a contract gap).
    af = obs["accounting_finance.subledger_reconciliation"]
    assert af.score == pytest.approx(0.5 * 5 / 6 + 0.3 * 0.8 + 0.2 * 0.8, abs=1e-6)


def test_ceiling_applied_and_overall_recomputed() -> None:
    world = make_minimal_world()
    _, fx = fixture_submission("good_submission_working_capital", world)
    rubric = json.loads(json.dumps(fx["fake_rubric"]))
    rubric["overall"] = 9.9  # ignored by code
    env = Env({"grader": rubric, "adversary": adversary_reply()})
    sub = wc_submission(world, dso_latest=99.0)  # 2 of 3 metrics pass ...
    sub.sql_evidence = []  # ... and the reference query fails -> det_pass = 2/4
    report = env.ped.grade(world, sub)
    assert report.deterministic.det_pass == 0.5
    assert report.rubric.ceiling_te == 5.0
    assert report.rubric.raw_scores["technical_execution"] == 9
    assert report.rubric.dimensions["technical_execution"].score == 5
    assert report.rubric.llm_overall == 9.9
    assert report.rubric.overall == 7.5  # 0.35*5 + 0.35*9 + 0.15*8 + 0.15*9 = 7.45 -> half-up
    assert any("technical_execution 9 -> 5" in c for c in report.rubric.clamps)


def test_te_ceiling_table() -> None:
    assert [te_ceiling(x) for x in (1.0, 0.9, 0.89, 0.6, 0.59, 0.3, 0.29)] == [10, 10, 8, 8, 5, 5, 3]
    assert te_ceiling(0.0, items_total=0) == 10
    assert te_ceiling(0.95, difficulty="foundation", all_metrics_pass=False) == 8


def test_business_judgment_capped_when_critical_metric_fails() -> None:
    world = make_minimal_world("forensic_revenue")
    sub, fx = fixture_submission("bad_submission_forensic_revenue", world)
    rubric = json.loads(json.dumps(fx["fake_rubric"]))
    rubric["misdiagnosis"] = False
    rubric["dimensions"]["business_judgment"]["score"] = 9
    env = Env({"grader": rubric, "adversary": adversary_reply()})
    report = env.ped.grade(world, sub)
    assert report.rubric.dimensions["business_judgment"].score == 6
    assert report.rubric.raw_scores["business_judgment"] == 9


def test_grader_output_repaired() -> None:
    world = make_minimal_world()
    _, fx = fixture_submission("good_submission_working_capital", world)
    broken = dict(fx["fake_rubric"])
    del broken["next_drill_nodes"]
    dirty = "Here is my grade:\n```json\n" + json.dumps(broken) + "\n```\nLet me know!"
    env = Env({"grader": [dirty, fx["fake_rubric"]], "adversary": adversary_reply()})
    report = env.ped.grade(world, wc_submission(world))
    assert env.purposes()[:2] == ["grader", "grader:repair"]
    assert report.rubric.next_drill_nodes == ["accounting_finance.cash_flow_forecasting"]


def test_bkt_applied_only_once_per_case() -> None:
    world = make_minimal_world()
    _, fx = fixture_submission("good_submission_working_capital", world)
    env = Env({"grader": fx["fake_rubric"], "adversary": adversary_reply()})
    first = env.ped.grade(world, wc_submission(world))
    after_first = env.graph.mastery_all()
    second = env.ped.grade(world, wc_submission(world))
    assert first.counted_for_mastery and len(first.skill_updates) == 5
    assert not second.counted_for_mastery and second.skill_updates == []
    assert env.graph.mastery_all() == after_first
    assert second.observations  # still reported for feedback
    assert len(env.db.list_submissions(world.case_id)) == 2


# --------------------------------------------------------------------------- leak check


def test_leak_scan_flags() -> None:
    world = make_minimal_world()
    flags = leak_scan({"a": f"Analysts say {CONCEAL} hurt cash."}, world)
    assert any(f.kind == "conceal" for f in flags)
    para = leak_scan({"a": "In 2024Q3 the top-20 accounts were moved onto 90-day payment terms."}, world)
    assert [f.kind for f in para] == ["conceal_fuzzy"]
    for text in ("about $5.7M is tied up", "roughly $5,700,000 is tied up", "5.7 million dollars", "570,000,000 cents"):
        nums = [f for f in leak_scan({"a": text}, world) if f.kind == "number"]
        assert nums and nums[0].item.startswith("cash_trapped_in_ar_cents"), text
    assert not leak_scan({"a": "Cash fell to $1.2M in 2024Q4 and DSO matters."}, world)
    assert any(f.kind == "anomaly_id" for f in leak_scan({"a": "see anomaly A1"}, world))
    hides = leak_scan({"s[0].body": "Honestly, she approved the terms change."}, world, stakeholder_fields={"s[0].body": "Dana Okafor"})
    assert any(f.kind == "stakeholder_hides" for f in hides)


def test_leak_scan_percent_and_public_exemption() -> None:
    world = make_minimal_world("forensic_revenue")  # magnitude revenue_inflation_pct = 0.11
    flagged = leak_scan({"a": "revenue looks about 11% higher than it should"}, world)
    assert [f.kind for f in flagged] == ["number"]
    assert not leak_scan({"a": "revenue looks about 11% higher than it should"}, world, public_numbers=[0.11])
    vocab = leak_scan({"a": "The jump was driven by channel stuffing at quarter end."}, world)
    assert any(f.kind == "anomaly_vocab" for f in vocab)


def test_number_parsing_helpers() -> None:
    toks = extract_numbers("DSO rose from 41 to 68 days (+27) in 2024Q3; $1.2M, 3.8%, 50bps, 2025, A1")
    vals = [(t.value, t.is_pct, t.is_year) for t in toks]
    assert (41.0, False, False) in vals and (1.2e6, False, False) in vals and (3.8, True, False) in vals
    assert (0.5, True, False) in vals and (2025.0, False, True) in vals
    assert all(t.value != 3.0 for t in toks)  # 2024Q3 / A1 are not numbers
    tok = extract_numbers("$1.2M")[0]
    assert number_matches(tok, 123_456_700, unit="cents")
    assert not number_matches(extract_numbers("$1.5M")[0], 123_456_700, unit="cents")
    assert number_matches(extract_numbers("3.8%")[0], 0.038, unit="ratio")
    assert number_matches(extract_numbers("68.2")[0], 68.0)  # within 0.5%


def test_dossier_leak_triggers_regeneration() -> None:
    leaky = dossier_payload(situation=f"Cash is falling because of {CONCEAL}; about $5.7M is trapped.")
    env = Env({"case_synthesizer": [leaky, dossier_payload()]})
    world = make_minimal_world()
    dossier = env.ped.synthesize_dossier(world)
    assert env.purposes() == ["case_synthesizer", "case_synthesizer:regenerate"]
    retry_prompt = env.transport.requests[1]["messages"][-1]["content"]
    assert "DO NOT INCLUDE" in retry_prompt and "you leaked" in retry_prompt and "$5.7M" in retry_prompt
    assert not dossier.leak_redacted
    assert dossier.situation.startswith("Cash fell to $1.2M")
    meta = env.db.get_artifact(world.case_id, "dossier")["leak_check"]  # type: ignore[index]
    assert meta["attempts"] == 2 and meta["redactions"] == 0 and meta["first_attempt_flags"] >= 2


def test_dossier_redacted_when_still_leaking() -> None:
    leaky = dossier_payload(situation=f"Cash is falling because of {CONCEAL}; about $5,700,000 is trapped.",
                            body="I signed off, she approved the terms change, but keep that quiet.")
    env = Env({"case_synthesizer": [leaky, leaky]})
    world = make_minimal_world()
    dossier = env.ped.synthesize_dossier(world)
    assert dossier.leak_redacted
    assert "90-day" not in dossier.situation and "5,700,000" not in dossier.situation
    assert "[redacted]" in dossier.situation
    assert "approved the terms change" not in dossier.stakeholder_messages[0].body
    meta = env.db.get_artifact(world.case_id, "dossier")["leak_check"]  # type: ignore[index]
    assert meta["attempts"] == 2 and meta["redactions"] >= 3


def test_dossier_mapping_public_inputs_and_cache() -> None:
    env = Env({"case_synthesizer": [dossier_payload(), dossier_payload(situation="Second version.")]})
    world = make_minimal_world()
    d = env.ped.synthesize_dossier(world)
    assert d.stakeholder_messages[0].from_name == "Dana Okafor" and d.stakeholder_messages[1].channel == "slack"
    assert d.data_room_index == ["invoices — one row per invoice", "customers"]
    assert d.deliverables == ["An executive memo", "`dso_latest` (days)", "SQL evidence"]
    prompt = env.transport.requests[0]["messages"][-1]["content"]
    assert world.ground_truth.root_cause not in prompt  # dossier writer never sees ground truth
    assert "dso_latest" in prompt and "68.0" not in prompt.split("DELIVERABLE EXPECTED")[1]
    assert env.ped.synthesize_dossier(world) == d and len(env.transport.requests) == 1  # cached
    assert env.db.list_artifacts(world.case_id) == [("dossier", "")]
    assert env.db.get_case(world.case_id).status == "briefed"  # type: ignore[union-attr]
    forced = env.ped.synthesize_dossier(world, force=True)
    assert forced.situation == "Second version." and len(env.transport.requests) == 2


def test_dossier_streaming_anthropic() -> None:
    env = Env({"case_synthesizer": dossier_payload()}, provider="anthropic")
    world = make_minimal_world()
    deltas: list[str] = []
    d = env.ped.synthesize_dossier(world, stream=True, on_delta=deltas.append)
    assert len(deltas) > 5
    assert json.loads("".join(deltas))["title"] == d.title
    assert env.transport.requests[0]["body"]["stream"] is True


def test_textbook_cached_by_node_and_principal_lock() -> None:
    env = Env({"textbook_chapter": [chapter_payload(), chapter_payload("sql.running_balances")]})
    world = make_minimal_world()
    ch = env.ped.textbook_chapter(world, "accounting_finance.working_capital")
    assert ch.skill_node == "accounting_finance.working_capital" and ch.self_checks[0].question == "What is DSO?"
    prompt = env.transport.requests[0]["messages"][-1]["content"]
    assert "SKILL NODE: accounting_finance.working_capital" in prompt and "invoices" in prompt
    assert env.ped.textbook_chapter(world, "accounting_finance.working_capital") == ch
    env.ped.textbook_chapter(world, "sql.running_balances")
    assert len(env.transport.requests) == 2
    assert env.db.list_artifacts(world.case_id, "textbook") == [("textbook", "accounting_finance.working_capital"), ("textbook", "sql.running_balances")]
    with pytest.raises(KeyError):
        env.ped.textbook_chapter(world, "nope.node")
    principal = make_minimal_world(difficulty="principal", seed=2)
    with pytest.raises(ChapterLockedError):
        env.ped.textbook_chapter(principal, "accounting_finance.working_capital")


# --------------------------------------------------------------------------- adversary


@pytest.mark.parametrize(
    ("difficulty", "expected"),
    [
        ("practitioner", ["clarify", "probe_assumption", "counter_evidence", "second_order", "concede_or_escalate", "concede_or_escalate"]),
        ("foundation", ["clarify", "probe_assumption", "counter_evidence", "concede_or_escalate", "concede_or_escalate", "concede_or_escalate"]),
        ("principal", ["clarify", "probe_assumption", "counter_evidence", "second_order", "concede_or_escalate", "concede_or_escalate"]),
    ],
)
def test_adversary_escalation_by_turn_index(difficulty: str, expected: list[str]) -> None:
    world = make_minimal_world(difficulty=difficulty)
    _, fx = fixture_submission("good_submission_working_capital", world)
    env = Env({"grader": fx["fake_rubric"], "adversary": adversary_reply()})
    report = env.ped.grade(world, wc_submission(world))
    turns = [env.ped.adversary_turn(world, report.submission_id, f"reply {i}") for i in range(5)]
    opening = env.db.list_turns(report.submission_id)[0].payload
    types = [opening["challenge_type"]] + [t.challenge_type for t in turns]
    assert types == expected
    for t in turns:
        assert t.persona == "Dana Okafor"  # CFO stakeholder for an accounting_finance case
        assert t.cites == ["dso_latest"]  # unknown cite stripped
        assert t.concedes == (t.challenge_type == "concede_or_escalate")
    stored = env.db.list_turns(report.submission_id)
    assert [s.role for s in stored] == ["adversary"] + ["user", "adversary"] * 5
    assert turns[-1].turn_index == stored[-1].turn_index
    last_prompt = env.transport.requests[-1]["messages"][-1]["content"]
    assert "ANALYST'S LATEST REPLY: reply 4" in last_prompt and "reply 3" in last_prompt
    assert "adversary" in env.purposes()


def test_rung_for() -> None:
    assert [rung_for(i, 3) for i in range(5)] == [1, 2, 3, 5, 5]
    assert [rung_for(i, 5) for i in range(6)] == [1, 2, 3, 4, 5, 5]


# --------------------------------------------------------------------------- quarter simulation


def test_simulate_transition_math() -> None:
    world = make_minimal_world()
    levers = world.ground_truth.decision_levers
    q1, info1 = simulate_transition(world.simulation, levers, {"terms_days": 45, "early_pay_discount_pct": 0.01})
    # dso: 68 + 0.6*(45-90) - 300*0.01 = 38; revenue effect lagged one quarter; resolved -> no drag
    assert q1.kpis == {"dso": 38.0, "cash_cents": 120_000_000.0, "revenue_cents": 2_100_000_000.0}
    assert info1["resolved"] and q1.period == "2025Q1"
    world.simulation.history.append(q1)
    q2, _ = simulate_transition(world.simulation, levers, {"terms_days": 45, "early_pay_discount_pct": 0.01})
    # lagged elasticity: revenue * (1 + 0.05 * (45-90)/90) = 2.1e9 * 0.975
    assert q2.kpis["revenue_cents"] == pytest.approx(2_047_500_000.0)
    assert q2.kpis["dso"] == 38.0 and q2.period == "2025Q2"
    unresolved, info = simulate_transition(make_minimal_world().simulation, levers, {"terms_days": 60})
    assert not info["resolved"] and unresolved.kpis["cash_cents"] == 95_000_000.0
    assert unresolved.kpis["dso"] == pytest.approx(50.0)
    clamped, _ = simulate_transition(make_minimal_world().simulation, levers, {"terms_days": 10})
    assert clamped.decisions["terms_days"] == 30  # clamped to lever.min
    with pytest.raises(ValueError, match="unknown"):
        simulate_transition(world.simulation, levers, {"bogus": 1})
    assert next_period("2024Q4") == "2025Q1" and next_period("2024-11") == "2025-02"


def test_shock_and_limit() -> None:
    world = make_minimal_world()
    world.simulation.model["shocks"] = [{"quarter": 1, "kpi": "revenue_cents", "pct": -0.1, "headline": "big customer churns"}]
    q, info = simulate_transition(world.simulation, world.ground_truth.decision_levers, {}, max_quarters=1)
    assert info["shock"]["headline"] == "big customer churns" and q.kpis["revenue_cents"] == pytest.approx(1_890_000_000.0)
    world.simulation.history.append(q)
    with pytest.raises(SimulationLimitError):
        simulate_transition(world.simulation, world.ground_truth.decision_levers, {}, max_quarters=1)


def test_advance_quarter_narrative_and_persistence() -> None:
    world = make_minimal_world()
    _, fx = fixture_submission("good_submission_working_capital", world)
    env = Env({"grader": fx["fake_rubric"], "adversary": adversary_reply(),
               "quarter_narrator": {"narrative_markdown": NARRATIVE, "watch_items": [{"metric": "dso", "threshold": "50", "why": "x"}]}})
    report = env.ped.grade(world, wc_submission(world))
    res = env.ped.advance_quarter(world, report.submission_id, {"terms_days": 45, "early_pay_discount_pct": 0.01})
    narrative = res.quarter.narrative or ""
    assert "17%" not in narrative and res.stripped_sentences == ["Revenue will grow 17% next year."]
    assert "DSO fell from 68 to 38 days (-30)." in narrative and "Cash held at $1.2M." in narrative
    assert res.resolved and res.watch_items[0]["metric"] == "dso"
    assert world.simulation.history[-1] == res.quarter and world.simulation.current_period == "2025Q1"
    assert res.simulation.history == world.simulation.history
    assert env.db.list_sim_quarters(world.case_id, report.submission_id) == [res.quarter]
    prompt = env.transport.requests[-1]["messages"][-1]["content"]
    assert "PERIOD CLOSED: 2025Q1" in prompt and '"dso":38.0' in prompt
    env.ped.advance_quarter(world, report.submission_id, {"terms_days": 45})
    with pytest.raises(SimulationLimitError):  # practitioner: 2 quarters
        env.ped.advance_quarter(world, report.submission_id, {"terms_days": 45})
    with pytest.raises(KeyError):
        env.ped.advance_quarter(world, "sub_missing", {})


def test_check_narrative_numbers() -> None:
    text, stripped = Pedagogy.check_narrative_numbers("Revenue rose 5%. Margin hit 12.3 points.\n\n## Watch\nQ2 is next.", [100.0, 105.0, 0.05])
    assert stripped == ["Margin hit 12.3 points."]
    assert "Revenue rose 5%." in text and "## Watch" in text and "Q2 is next." in text


def test_usage_attributed_by_purpose() -> None:
    world = make_minimal_world()
    _, fx = fixture_submission("good_submission_working_capital", world)
    env = Env({"grader": fx["fake_rubric"], "adversary": adversary_reply(), "case_synthesizer": dossier_payload()})
    env.ped.synthesize_dossier(world)
    env.ped.grade(world, wc_submission(world))
    purposes = {u.purpose for u in env.db.list_usage()}
    assert purposes == {"case_synthesizer", "grader", "adversary"}
    models = {u.purpose: u.model for u in env.db.list_usage()}
    assert models["grader"] == "gpt-4o" and models["adversary"] == "gpt-4o-mini"
