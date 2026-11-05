"""Prompt template tests: placeholders, brace doubling, verbatim constants, inspector output."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from axiom.engine.pedagogy import (
    ANTI_HALLUCINATION,
    PILLAR_HINTS,
    PROMPTS,
    RUBRIC_ANCHORS,
    list_prompts,
)

SPEC = Path(__file__).resolve().parents[2] / "docs" / "research" / "pedagogy.md"

DOCUMENTED = {
    "case_synthesizer": {"company_json", "narrative_hooks_json", "periods_json", "kpis_json", "reveal_json", "conceal_json",
                         "stakeholders_json", "timeline_json", "artifacts_json", "architecture_mermaid", "table_index_json",
                         "difficulty", "deliverable_spec"},
    "textbook_chapter": {"node_id", "node_title", "node_summary", "mental_models_json", "prereq_titles_json", "pillar",
                         "company_json", "periods_json", "kpis_json", "schema_excerpt_json", "sample_rows_json"},
    "grader": {"archetype", "difficulty", "root_cause", "anomalies_json", "red_herrings_json", "expected_metrics_json",
               "deterministic_grade_json", "ceiling_te", "memo_markdown", "metrics_json", "sql_evidence_json",
               "python_evidence_json", "design_mermaid", "financial_model_json", "candidate_nodes_json"},
    "adversary": {"persona_name", "persona_role", "company_name", "persona_agenda", "max_rung", "rung", "red_herrings_json",
                  "socratic_traps_json", "levers_json", "expected_metrics_json", "kpis_json", "overall", "misdiagnosis",
                  "weakest_point", "memo_markdown", "history_json", "latest_reply"},
    "quarter_narrator": {"company_name", "period", "decisions_json", "levers_json", "kpis_prev_json", "kpis_now_json",
                         "headlines_json", "shock_json", "anomaly_active"},
}


def test_all_five_templates_present() -> None:
    assert set(PROMPTS) == set(DOCUMENTED)
    assert {p.tier for p in PROMPTS.values()} <= {"fast", "deep"}
    assert PROMPTS["grader"].tier == "deep" and PROMPTS["adversary"].tier == "fast"


@pytest.mark.parametrize("name", sorted(DOCUMENTED))
def test_template_formats_with_documented_placeholders(name: str) -> None:
    prompt = PROMPTS[name]
    assert set(prompt.placeholders) == DOCUMENTED[name]
    values = {k: f"<{k}>" for k in prompt.placeholders}
    system, user = prompt.render(**values)
    assert system["role"] == "system" and user["role"] == "user"
    for k in prompt.placeholders:
        assert f"<{k}>" in system["content"] + user["content"]
    # no unformatted single-brace placeholders survive
    assert not re.search(r"(?<!\{)\{[a-z_]+\}(?!\})", system["content"] + user["content"])
    assert system["content"].endswith(ANTI_HALLUCINATION)


@pytest.mark.parametrize("name", sorted(DOCUMENTED))
def test_json_example_braces_doubled(name: str) -> None:
    """After formatting, the 'Return JSON:' example is valid JSON (so {{ }} were doubled)."""
    prompt = PROMPTS[name]
    _, user = prompt.render(**{k: "x" for k in prompt.placeholders})
    example = user["content"].split("Return JSON:", 1)[1].strip()
    parsed = json.loads(example)
    assert isinstance(parsed, dict) and parsed
    assert "{{" not in user["content"] and "}}" not in user["content"]


def test_missing_placeholder_raises() -> None:
    with pytest.raises(KeyError, match="missing placeholders"):
        PROMPTS["quarter_narrator"].render(company_name="x")


def test_grader_embeds_constants() -> None:
    system, _ = PROMPTS["grader"].render(**{k: "x" for k in PROMPTS["grader"].placeholders})
    assert RUBRIC_ANCHORS in system["content"] and PILLAR_HINTS in system["content"]
    assert RUBRIC_ANCHORS.startswith("**technical_execution**")
    assert "| system_design |" in PILLAR_HINTS
    assert set(PROMPTS["grader"].defaults) == {"rubric_anchors", "pillar_hints"}


def test_list_prompts_for_inspector() -> None:
    rows = list_prompts()
    assert [r.name for r in rows] == list(PROMPTS)
    grader = next(r for r in rows if r.name == "grader")
    assert RUBRIC_ANCHORS in grader.template and "{deterministic_grade_json}" in grader.template
    assert "{rubric_anchors}" not in grader.template
    for row in rows:
        assert "### SYSTEM" in row.template and "### USER" in row.template and "NUMERIC DISCIPLINE" in row.template


def test_schemas_are_json_schemas() -> None:
    for p in PROMPTS.values():
        assert p.json_schema.get("type") == "object" and p.json_schema.get("properties")
    assert {"dimensions", "misdiagnosis", "next_drill_nodes"} <= set(PROMPTS["grader"].json_schema["required"])


@pytest.mark.skipif(not SPEC.exists(), reason="docs/research/pedagogy.md not present")
def test_templates_verbatim_from_spec() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    assert ANTI_HALLUCINATION.strip() in spec
    assert RUBRIC_ANCHORS in spec and PILLAR_HINTS in spec
    for p in PROMPTS.values():
        system_body = p.system[: -len(ANTI_HALLUCINATION)]
        assert system_body.rstrip("\n") in spec, p.name
        assert p.user_template in spec, p.name
