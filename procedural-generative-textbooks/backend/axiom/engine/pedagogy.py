"""LLM pedagogy layer (docs/research/pedagogy.md sections 4-8; ARCHITECTURE.md section 9).

Contents:

* The five prompt templates (``case_synthesizer``, ``textbook_chapter``, ``grader``,
  ``adversary``, ``quarter_narrator``) copied verbatim from pedagogy.md section 6, with
  :data:`RUBRIC_ANCHORS` and :data:`PILLAR_HINTS` embedded from constants, exposed through
  :data:`PROMPTS` and :func:`list_prompts` for the prompt inspector.
* :func:`leak_scan` / :func:`redact` implementing the section 7 leak check (concealed strings
  with 70%-token fuzzy windows, anomaly vocabulary near causal words, anomaly ids, ground-truth
  numbers in formatted variants with 0.5% tolerance, stakeholder ``hides``).
* :func:`simulate_transition`, the generic deterministic quarter-transition engine driven by
  ``world.simulation.model`` (format documented on the function).
* :class:`Pedagogy`, which orchestrates dossier/textbook synthesis (cached as case artifacts),
  deterministic + rubric grading with the code-side clamps of section 5.3, BKT application,
  the Socratic adversary ladder and quarter advancement.

The LLM never computes financial figures here: the deterministic grader decides numeric
correctness and the simulation computes KPIs; the narrator only narrates them, and any sentence
containing a number not present in the data is stripped.
"""

from __future__ import annotations

import json
import logging
import math
import re
import string
import uuid
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from axiom.engine.llm_client import LLMClient, LLMError, Message, Tier
from axiom.engine.rng import SeededRNG
from axiom.models import (
    AdvanceResult,
    AdversaryTurn,
    CaseRecord,
    CaseWorld,
    DeterministicGrade,
    Dossier,
    GradeReport,
    Lever,
    MetricResult,
    PromptTemplate as PromptTemplateRow,
    QuerySignatureMatch,
    RevealedTruth,
    RubricDimension,
    RubricGrade,
    SelfCheck,
    SimQuarter,
    SimulationState,
    StakeholderMessage,
    Submission,
    TextbookChapter,
)
from axiom.persistence import Persistence, utc_now_iso
from axiom.skill_graph import SkillGraph, metric_anomaly_ids, metric_pillar

__all__ = [
    "ANTI_HALLUCINATION",
    "RUBRIC_ANCHORS",
    "PILLAR_HINTS",
    "RUBRIC_WEIGHTS",
    "LADDER",
    "MAX_RUNG",
    "PERSONAS",
    "PromptTemplate",
    "PROMPTS",
    "list_prompts",
    "DossierLLM",
    "ChapterLLM",
    "GraderLLM",
    "AdversaryLLM",
    "NarratorLLM",
    "NumberToken",
    "extract_numbers",
    "number_matches",
    "LeakFlag",
    "leak_scan",
    "redact",
    "normalize_sql",
    "te_ceiling",
    "rubric_overall",
    "rung_for",
    "next_period",
    "simulate_transition",
    "SimulationLimitError",
    "ChapterLockedError",
    "Pedagogy",
]

logger = logging.getLogger("axiom.pedagogy")

# =========================================================================== prompt text
# Copied verbatim from docs/research/pedagogy.md (sections 5.1, 5.2 and 6). Do not retype.

ANTI_HALLUCINATION: str = """
NUMERIC DISCIPLINE
- Every number you state must appear verbatim in the data you were given, or be a simple
  arithmetic combination of such numbers that you show inline (e.g. "DSO rose from 41 to 68
  days (+27)").
- If you need a number you do not have, write "[not provided]" and do not estimate it.
- Never invent names, dates, tables, columns, customers or events that are not in the input.
- Output ONLY the JSON object requested. No prose before or after. No markdown fences.
"""

case_synthesizer_SYSTEM: str = """You are a Harvard Business School case writer with a forensic accountant's discipline. You
write immersive, executive-level case dossiers from a structured seed. Your dossier must let a
strong analyst discover the root cause from the data room; it must never state the root cause.

HARD RULES
1. REVEAL: every item in `reveal` must appear in the dossier, in substance.
2. CONCEAL: nothing in `conceal` may appear, be paraphrased, be hinted at by naming the
   anomaly mechanism, or be implied by a stakeholder "suspecting" it. Stakeholders may be
   wrong, worried, or defensive, but they do not diagnose.
3. Red-herring timeline events are presented with the same seriousness as real ones.
4. Stakeholders speak in their own voice (email, Slack, board-deck excerpt). Each one knows
   only what `knows` says and conceals what `hides` says; they may be evasive.
5. The ask is ambiguous at the stated difficulty: foundation names the area, practitioner
   names the symptom, principal asks a one-line board question.
6. Write in present tense, plain business English, no jargon the company would not use.
7. Length: 900–1400 words of prose across all sections.
"""

case_synthesizer_USER: str = """COMPANY
{company_json}

INDUSTRY NARRATIVE HOOKS
{narrative_hooks_json}

PERIODS: {periods_json}
HEADLINE KPIs BY PERIOD (public view, safe to quote)
{kpis_json}

DOSSIER SEED
reveal: {reveal_json}
conceal: {conceal_json}
stakeholders: {stakeholders_json}
timeline: {timeline_json}
artifacts requested: {artifacts_json}
architecture diagram (mermaid, may be null): {architecture_mermaid}

DATA ROOM (tables available to the analyst)
{table_index_json}

DIFFICULTY: {difficulty}
DELIVERABLE EXPECTED: {deliverable_spec}

Return JSON:
{{
  "title": "string (<= 12 words, no company name leak of the mechanism)",
  "situation": "markdown, 2-3 paragraphs: where the company is today and why this landed on the analyst's desk",
  "company_background": "markdown, 2 paragraphs grounded in the COMPANY and NARRATIVE HOOKS",
  "stakeholder_artifacts": [
    {{"stakeholder": "name", "role": "role", "format": "email|slack|board_deck|memo",
      "subject": "string", "body": "markdown in the stakeholder's voice, 80-200 words"}}
  ],
  "timeline": [{{"period": "2024Q3", "headline": "string", "detail": "string"}}],
  "the_ask": "markdown: the ambiguous question, scaled to DIFFICULTY",
  "deliverable_spec": "markdown bullet list of what the memo must contain (echo DELIVERABLE EXPECTED)",
  "data_room_index": [{{"table": "name", "note": "one line; at foundation difficulty describe what the table holds, at principal leave note empty"}}],
  "architecture_mermaid": "string or null, passed through unchanged"
}}"""

textbook_chapter_SYSTEM: str = """You are writing one chapter of a first-principles technical textbook. The chapter teaches ONE
skill node and uses the live case's own numbers as its worked example. You explain mechanisms,
not recipes. You assume a smart reader who has never seen the topic.

STRUCTURE (markdown, 1200-2000 words)
1. "Why this exists" — the problem the idea solves, in one paragraph.
2. "The model" — the core formula or pattern, derived not asserted. Use LaTeX ($...$) for math.
3. "Worked example from this case" — apply it to the numbers provided; show each step.
4. "Syntax patterns" — for SQL/Python nodes: 2-3 idiomatic snippets (DuckDB SQL / pandas) that
   run against the tables listed; for business nodes: the spreadsheet/ledger layout.
5. "Where people go wrong" — 3-5 named failure modes with how to detect them.
6. "Self-check" — exactly 3 questions; answers in a separate field, never in the chapter body.

RULES
- Do not mention the case's root cause or any anomaly; teach the skill, not the answer.
- Use only tables/columns from the provided schema.
- Every number in the worked example must come from the provided data.
"""

textbook_chapter_USER: str = """SKILL NODE: {node_id} — {node_title}
SUMMARY: {node_summary}
MENTAL MODELS TO CONVEY: {mental_models_json}
PREREQUISITES THE READER HAS: {prereq_titles_json}
PILLAR: {pillar}

CASE CONTEXT (public view)
company: {company_json}
periods: {periods_json}
relevant public KPIs: {kpis_json}
schema excerpt (tables relevant to this node): {schema_excerpt_json}
sample rows: {sample_rows_json}

Return JSON:
{{
  "node_id": "{node_id}",
  "title": "string",
  "chapter_markdown": "string",
  "self_check": [{{"question": "string", "answer": "string"}}],
  "snippets": [{{"language": "sql|python", "purpose": "string", "code": "string"}}]
}}"""

grader_SYSTEM: str = """You are an adversarial grader: a former Big Four forensic partner and a staff engineer sharing
one chair. You have the ground truth. The learner does not. You grade the submission against
the anchored rubric below. You are fair but unforgiving about unsupported claims.

PROCESS
1. Read the DETERMINISTIC GRADE first. It is authoritative for every number. Do not re-derive
   numbers; do not dispute it.
2. Decide `misdiagnosis`: true if the memo's stated root cause is not the GROUND TRUTH root
   cause or is one of the RED HERRINGS.
3. Score each dimension using the anchors. Quote the submission as evidence (exact substrings,
   <= 200 chars each). List gaps as concrete, checkable statements.
4. Name the strongest and weakest point in one sentence each.
5. Recommend 1-3 `next_drill_nodes` from the CANDIDATE NODES list only.

RUBRIC ANCHORS
{rubric_anchors}

PER-PILLAR HINTS
{pillar_hints}

RULES
- Evidence quotes must be verbatim substrings of the submission.
- Do not award technical_execution above the CEILING given.
- If the submission asserts a number the deterministic grade marked out of tolerance, cite it
  as a gap.
- Be specific: "did not deduplicate invoices before summing" beats "data quality issues".
"""

grader_USER: str = """CASE: {archetype} / {difficulty}
GROUND TRUTH
root_cause: {root_cause}
anomalies: {anomalies_json}
red_herrings: {red_herrings_json}
expected_metrics: {expected_metrics_json}

DETERMINISTIC GRADE
{deterministic_grade_json}
technical_execution CEILING: {ceiling_te}

SUBMISSION
memo_markdown:
{memo_markdown}
asserted metrics: {metrics_json}
sql evidence: {sql_evidence_json}
python evidence: {python_evidence_json}
design_mermaid: {design_mermaid}
financial_model: {financial_model_json}

CANDIDATE NODES: {candidate_nodes_json}

Return JSON:
{{
  "dimensions": {{
    "technical_execution": {{"score": 0, "evidence": ["string"], "gaps": ["string"]}},
    "business_judgment":   {{"score": 0, "evidence": ["string"], "gaps": ["string"]}},
    "communication":       {{"score": 0, "evidence": ["string"], "gaps": ["string"]}},
    "risk_awareness":      {{"score": 0, "evidence": ["string"], "gaps": ["string"]}}
  }},
  "overall": 0.0,
  "misdiagnosis": false,
  "stated_root_cause": "string: what the learner claimed, in one sentence",
  "strongest_point": "string",
  "weakest_point": "string",
  "next_drill_nodes": ["node.id"]
}}"""

adversary_SYSTEM: str = """You are {persona_name}, {persona_role} at {company_name}. You are reviewing an analyst's memo
in a live meeting. You are skeptical, busy, and fair. You have your own agenda: {persona_agenda}.
You know the data the analyst has. You do NOT know the ground truth and must never state it;
you only test the analyst's reasoning. You may be wrong on purpose when you push a red herring.

STYLE: 60-140 words per turn, first person, no bullet lists, one challenge per turn, end with a
question unless you are conceding. Cite specific numbers from the data when you challenge.

LADDER: follow the escalation ladder; your `challenge_type` must be the current rung unless the
analyst fully answered the previous rung, in which case advance. At the final rung either
concede (if the analyst's rebuttals were evidence-backed) or escalate the unresolved gap.
MAX RUNG FOR THIS DIFFICULTY: {max_rung}
"""

adversary_USER: str = """CURRENT RUNG: {rung}
GROUND-TRUTH MATERIAL FOR CHALLENGES (never reveal directly)
red_herrings: {red_herrings_json}
socratic_traps (challenge + correct rebuttal): {socratic_traps_json}
decision_levers: {levers_json}
expected_metrics (for citing numbers): {expected_metrics_json}
public KPIs: {kpis_json}

GRADE SUMMARY: overall {overall}, misdiagnosis {misdiagnosis}, weakest point: {weakest_point}

MEMO (analyst): {memo_markdown}
CONVERSATION SO FAR: {history_json}
ANALYST'S LATEST REPLY: {latest_reply}

Return JSON:
{{
  "persona": "{persona_name}",
  "message": "string",
  "challenge_type": "clarify|probe_assumption|counter_evidence|second_order|concede_or_escalate",
  "cites": ["metric keys from expected_metrics or public KPIs used in the message"],
  "concedes": false,
  "rung_advanced": true
}}"""

quarter_narrator_SYSTEM: str = """You are the CFO writing the one-page board update for the quarter that just closed. The
numbers were produced by a deterministic simulation; your job is to narrate them, attribute
changes to the decisions taken, and flag what the board should watch. You never add numbers.

STRUCTURE (markdown, 250-400 words): headline (one sentence), what we did (the decisions),
what happened (KPI deltas with before → after), what it cost / what it earned, what we watch
next quarter.
"""

quarter_narrator_USER: str = """COMPANY: {company_name}
PERIOD CLOSED: {period}
DECISIONS TAKEN (lever → value): {decisions_json}
LEVER DEFINITIONS: {levers_json}
KPIs PRIOR PERIOD: {kpis_prev_json}
KPIs THIS PERIOD: {kpis_now_json}
STATEMENT HEADLINES (revenue, gross profit, operating income, cash, DSO, DIO, DPO): {headlines_json}
SHOCK EVENT THIS PERIOD (may be null): {shock_json}
UNRESOLVED ANOMALY STILL ACTIVE: {anomaly_active}

Return JSON:
{{
  "narrative_markdown": "string",
  "watch_items": [{{"metric": "key", "threshold": "string", "why": "string"}}]
}}"""

RUBRIC_ANCHORS: str = """**technical_execution** — correctness and craft of the SQL/Python/model/diagram.

- **2** Code runs but answers a different question; joins fan out or aggregations double count;
  no handling of nulls/duplicates; metrics asserted without a derivation.
- **5** Core query/model is right in shape; at least one material data-quality issue missed
  (duplicates, timezone, units); derivation present but a step is wrong; no validation of row
  counts.
- **8** All asserted metrics reproducible from the evidence; data-quality issues found and
  handled explicitly; window/cohort/reconciliation logic idiomatic; at least one sanity check
  (row counts, tie-out to a control total).
- **10** Everything in 8 plus: the evidence is minimal and sufficient (no redundant queries),
  edge cases are named (right-censoring, cutoff boundaries), and a reviewer could rerun it
  unchanged.

**business_judgment** — did they find the root cause and reason about it like an operator?

- **2** Restates symptoms as causes; accepts a red herring; recommendation is generic ("improve
  collections").
- **5** Identifies the right area but not the mechanism; recommendation plausible but not tied
  to the quantified driver; no sizing of the impact.
- **8** Correct root cause with the mechanism explained; impact quantified in the company's
  own numbers; recommendation specific, sequenced, and owned by a named role.
- **10** Everything in 8 plus a quantified second-order consequence of the recommendation and
  an explicit statement of what would change their mind.

**communication** — can a CFO act on this in five minutes?

- **2** No clear answer in the first paragraph; numbers without units or periods; evidence not
  linked to claims.
- **5** Answer stated but buried; some claims linked to evidence; structure present but uneven.
- **8** Answer first, then evidence, then recommendation; every number has unit and period; each
  claim points at a query or model output.
- **10** Everything in 8 plus explicit confidence levels and the single chart/table a board
  would need.

**risk_awareness** — what could go wrong, and what did they rule out?

- **2** No alternatives considered; no mention of data limitations; recommendation ignores
  covenants/capacity/customers.
- **5** Mentions one alternative explanation or one risk, without evidence for ruling it out.
- **8** Rejects at least one red herring with evidence; names the operational or financial risk
  of the recommendation and a mitigation.
- **10** Everything in 8 plus a monitoring plan (which metric, which threshold, which cadence)
  and a pre-mortem of the recommendation."""

PILLAR_HINTS: str = """| Pillar | technical_execution looks for | business_judgment looks for |
|---|---|---|
| accounting_finance | statements tie; sub-ledger reconciled to GL; ratios computed with the right period denominators | accrual vs cash distinction; mechanism in journal-entry terms; covenant/liquidity impact |
| operations_strategy | Little's Law / queueing applied with consistent units; cohorts built correctly | constraint identified; second-order migration; unit-economics consequence |
| system_design | diagram shows the failure path and the fix; idempotency/outbox/replica reasoning specific | failure translated to P&L; blast radius; rollout risk |
| sql | correct joins/windows/cohorts; dedup and timezone handled; results reproducible | query results interpreted, not just shown |
| python | seeded, vectorised, validated; simulation reports percentiles | model assumptions tied to the case data |"""


# =========================================================================== templates


class _Partial(dict):  # type: ignore[type-arg]
    """``format_map`` helper that leaves unknown placeholders untouched."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fields(template: str) -> list[str]:
    out: list[str] = []
    for _, name, _, _ in string.Formatter().parse(template):
        if name and name not in out:
            out.append(name)
    return out


@dataclass(frozen=True)
class PromptTemplate:
    """A named prompt: ``system`` and ``user_template`` are ``str.format`` templates.

    ``defaults`` pre-fills constant placeholders (the grader's rubric anchors and pillar hints)
    so the inspector shows the exact text the model sees.
    """

    name: str
    system: str
    user_template: str
    json_schema: dict[str, Any]
    tier: Tier
    defaults: Mapping[str, str] = field(default_factory=dict)

    @property
    def placeholders(self) -> list[str]:
        """Placeholders the caller must supply (excluding ``defaults``)."""
        names = _fields(self.system) + [n for n in _fields(self.user_template) if n not in _fields(self.system)]
        return [n for n in names if n not in self.defaults]

    def render(self, **values: Any) -> list[Message]:
        """Format system and user parts; raises ``KeyError`` for a missing placeholder."""
        merged = {**self.defaults, **{k: v for k, v in values.items()}}
        missing = [n for n in self.placeholders if n not in merged]
        if missing:
            raise KeyError(f"prompt {self.name!r} missing placeholders: {missing}")
        return [
            {"role": "system", "content": self.system.format(**merged)},
            {"role": "user", "content": self.user_template.format(**merged)},
        ]

    def inspector_text(self) -> str:
        """System + user with constants embedded and call-time placeholders left visible."""
        system = self.system.format_map(_Partial(self.defaults))
        return f"### SYSTEM\n{system}\n### USER\n{self.user_template}"

    def to_public(self) -> PromptTemplateRow:
        """Row for ``GET /settings/prompts``."""
        return PromptTemplateRow(name=self.name, template=self.inspector_text())


# =========================================================================== LLM output schemas


class _LLMOut(BaseModel):
    model_config = ConfigDict(extra="ignore")


class StakeholderArtifactLLM(_LLMOut):
    """One stakeholder artifact in the synthesized dossier."""

    stakeholder: str
    role: str = ""
    format: str = "email"
    subject: str = ""
    body: str


class TimelineItemLLM(_LLMOut):
    """A timeline row in the synthesized dossier."""

    period: str
    headline: str
    detail: str = ""


class DataRoomEntryLLM(_LLMOut):
    """A data-room index row."""

    table: str
    note: str = ""


class DossierLLM(_LLMOut):
    """Output schema of ``case_synthesizer``."""

    title: str
    situation: str
    company_background: str = ""
    stakeholder_artifacts: list[StakeholderArtifactLLM] = Field(default_factory=list)
    timeline: list[TimelineItemLLM] = Field(default_factory=list)
    the_ask: str
    deliverable_spec: str = ""
    data_room_index: list[DataRoomEntryLLM] = Field(default_factory=list)
    architecture_mermaid: str | None = None

    @field_validator("data_room_index", mode="before")
    @classmethod
    def _coerce_index(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [{"table": v} if isinstance(v, str) else v for v in value]
        return value

    @field_validator("deliverable_spec", mode="before")
    @classmethod
    def _coerce_spec(cls, value: Any) -> Any:
        if isinstance(value, list):
            return "\n".join(f"- {v}" for v in value)
        return value


class SelfCheckLLM(_LLMOut):
    """A self-check question."""

    question: str
    answer: str


class ChapterLLM(_LLMOut):
    """Output schema of ``textbook_chapter``."""

    node_id: str = ""
    title: str
    chapter_markdown: str
    self_check: list[SelfCheckLLM] = Field(default_factory=list)
    snippets: list[dict[str, Any]] = Field(default_factory=list)


RUBRIC_DIMENSIONS: tuple[str, ...] = ("technical_execution", "business_judgment", "communication", "risk_awareness")


class GraderDimensionLLM(_LLMOut):
    """One rubric dimension from the grader."""

    score: float = Field(ge=0, le=10)
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class GraderLLM(_LLMOut):
    """Output schema of ``grader``: all four dimensions, ``misdiagnosis`` and
    ``next_drill_nodes`` are required (a missing key triggers the repair call)."""

    dimensions: dict[str, GraderDimensionLLM]
    overall: float | None = None
    misdiagnosis: bool
    stated_root_cause: str = ""
    strongest_point: str = ""
    weakest_point: str = ""
    next_drill_nodes: list[str]

    @field_validator("dimensions")
    @classmethod
    def _all_dims(cls, value: dict[str, GraderDimensionLLM]) -> dict[str, GraderDimensionLLM]:
        missing = [d for d in RUBRIC_DIMENSIONS if d not in value]
        if missing:
            raise ValueError(f"missing rubric dimensions: {missing}")
        return value


class AdversaryLLM(_LLMOut):
    """Output schema of ``adversary``."""

    persona: str = ""
    message: str
    challenge_type: str = ""
    cites: list[str] = Field(default_factory=list)
    concedes: bool = False
    rung_advanced: bool = True


class NarratorLLM(_LLMOut):
    """Output schema of ``quarter_narrator``."""

    narrative_markdown: str
    watch_items: list[dict[str, Any]] = Field(default_factory=list)


def _build_prompts() -> dict[str, PromptTemplate]:
    def sys(text: str) -> str:
        return text + ANTI_HALLUCINATION

    return {
        "case_synthesizer": PromptTemplate(
            "case_synthesizer", sys(case_synthesizer_SYSTEM), case_synthesizer_USER, DossierLLM.model_json_schema(), "deep"
        ),
        "textbook_chapter": PromptTemplate(
            "textbook_chapter", sys(textbook_chapter_SYSTEM), textbook_chapter_USER, ChapterLLM.model_json_schema(), "deep"
        ),
        "grader": PromptTemplate(
            "grader", sys(grader_SYSTEM), grader_USER, GraderLLM.model_json_schema(), "deep",
            defaults={"rubric_anchors": RUBRIC_ANCHORS, "pillar_hints": PILLAR_HINTS},
        ),
        "adversary": PromptTemplate(
            "adversary", sys(adversary_SYSTEM), adversary_USER, AdversaryLLM.model_json_schema(), "fast"
        ),
        "quarter_narrator": PromptTemplate(
            "quarter_narrator", sys(quarter_narrator_SYSTEM), quarter_narrator_USER, NarratorLLM.model_json_schema(), "fast"
        ),
    }


PROMPTS: dict[str, PromptTemplate] = _build_prompts()


def list_prompts() -> list[PromptTemplateRow]:
    """``GET /settings/prompts``: every template as the model sees it."""
    return [p.to_public() for p in PROMPTS.values()]


# =========================================================================== rubric math

RUBRIC_WEIGHTS: dict[str, float] = {
    "technical_execution": 0.35,
    "business_judgment": 0.35,
    "communication": 0.15,
    "risk_awareness": 0.15,
}


def te_ceiling(det_pass: float, *, items_total: int = 1, difficulty: str = "practitioner", all_metrics_pass: bool = True) -> float:
    """technical_execution ceiling from the deterministic pass rate (section 5.3).

    ``10 / 8 / 5 / 3`` for ``det_pass >= 0.9 / 0.6 / 0.3 / else``. With no deterministic items at
    all there is nothing to constrain (10). At foundation the ceiling is at most 8 unless every
    deterministic metric passed (section 4).
    """
    if items_total <= 0:
        ceiling = 10.0
    elif det_pass >= 0.9:
        ceiling = 10.0
    elif det_pass >= 0.6:
        ceiling = 8.0
    elif det_pass >= 0.3:
        ceiling = 5.0
    else:
        ceiling = 3.0
    if difficulty == "foundation" and not all_metrics_pass:
        ceiling = min(ceiling, 8.0)
    return ceiling


def rubric_overall(scores: Mapping[str, float]) -> float:
    """``0.35 te + 0.35 bj + 0.15 comm + 0.15 risk`` rounded half-up to one decimal."""
    raw = sum(w * float(scores.get(k, 0.0)) for k, w in RUBRIC_WEIGHTS.items())
    # format to 9 decimals first so float noise (7.449999...) does not defeat half-up rounding
    return float(Decimal(f"{raw:.9f}").quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


# =========================================================================== numbers

_NUM_RE = re.compile(
    r"(?<![\w.])(?P<cur>[$€£])?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"(?:\s?(?P<suf>%|percent\b|pct\b|bps\b|bn\b|mm\b|[kKmMbB](?![A-Za-z])|million\b|billion\b|thousand\b))?"
    r"(?![A-Za-z0-9_])"
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}


@dataclass(frozen=True)
class NumberToken:
    """A number found in prose, normalised to base units (``1.2M`` -> 1.2e6; ``11%`` -> 11, pct)."""

    value: float
    is_pct: bool
    sig: int
    start: int
    end: int
    raw: str
    is_year: bool = False


def _sig_digits(int_part: str, dec_part: str) -> int:
    digits = int_part.replace(",", "")
    if dec_part:
        all_digits = (digits + dec_part[1:]).lstrip("0")
        return max(1, len(all_digits))
    stripped = digits.lstrip("0").rstrip("0")
    return max(1, len(stripped))


def extract_numbers(text: str) -> list[NumberToken]:
    """All numbers in ``text`` with currency/percent/suffix handling. Period labels such as
    ``2024Q3`` and identifiers such as ``A1`` are not numbers."""
    out: list[NumberToken] = []
    for m in _NUM_RE.finditer(text):
        int_part, dec_part = m.group("int"), m.group("dec") or ""
        value = float(int_part.replace(",", "") + dec_part)
        suf = (m.group("suf") or "").lower()
        is_pct = suf in ("%", "percent", "pct", "bps")
        if suf == "bps":
            value /= 100.0
        elif suf in _SCALE:
            value *= _SCALE[suf]
        is_year = (not dec_part and not suf and not m.group("cur") and "," not in int_part and len(int_part) == 4
                   and 1900 <= int(int_part) <= 2100)
        out.append(NumberToken(value, is_pct, _sig_digits(int_part, dec_part), m.start(), m.end(), m.group(0), is_year))
    return out


def _round_sig(x: float, sig: int) -> float:
    if x == 0:
        return 0.0
    return round(x, sig - 1 - int(math.floor(math.log10(abs(x)))))


def _close(t: float, c: float, sig: int, rel_tol: float) -> bool:
    t, c = abs(t), abs(c)
    if c == 0:
        return t == 0
    if abs(t - c) <= rel_tol * c:
        return True
    if sig >= 2:
        rc = _round_sig(c, sig)
        return abs(rc - t) <= 1e-9 * max(1.0, abs(t))
    return False


def _is_cents(unit: str, key: str) -> bool:
    return "cent" in unit.lower() or key.lower().endswith("_cents")


def _is_ratio(unit: str, value: float) -> bool:
    u = unit.lower()
    return u in ("ratio", "pct", "%", "percent", "fraction", "share", "rate") or (u == "" and abs(value) <= 1.0)


def number_matches(token: NumberToken, value: float, *, unit: str = "", key: str = "", rel_tol: float = 0.005) -> bool:
    """Does a prose number denote ``value``? Compares as floats with ``rel_tol`` (0.5%) and also
    at the token's own significant-figure precision (``1.2M`` matches 1,234,567 dollars); cents
    values also match their dollar amount, ratios also match their percent form."""
    candidates: list[float] = []
    if token.is_pct:
        if _is_ratio(unit, value) or abs(value) <= 1.0:
            candidates.append(value * 100.0)
        if abs(value) > 1.0:
            candidates.append(value)
    else:
        candidates.append(value)
        if _is_cents(unit, key):
            candidates.append(value / 100.0)
    return any(_close(token.value, c, token.sig, rel_tol) for c in candidates)


# =========================================================================== leak check

_WORD_RE = re.compile(r"[a-z0-9]+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_CAUSAL = ("cause", "caused", "causes", "because", "due to", "driven by", "result of", "root cause", "stems from")
_STOP = {"that", "this", "with", "from", "were", "have", "been", "into", "than", "they", "their", "which", "when",
         "will", "would", "about", "after", "before", "over", "under", "only", "also", "some", "such"}


def _stem(word: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(word) > len(suf) + 3 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _content_tokens(text: str) -> list[str]:
    return list(dict.fromkeys(_stem(t) for t in _tokens(text) if len(t) >= 4 and t not in _STOP))


def _normalize(text: str) -> str:
    return " ".join(_tokens(text))


def _sentences(text: str) -> list[tuple[int, int]]:
    """Sentence spans ``(start, end)`` in ``text``."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _SENT_RE.finditer(text):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return spans


@dataclass
class LeakFlag:
    """One leak finding. ``spans`` are exact substrings of ``field`` to redact."""

    kind: Literal["conceal", "conceal_fuzzy", "anomaly_vocab", "anomaly_id", "number", "stakeholder_hides"]
    item: str
    field: str
    spans: list[str]
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-able form for ``case_artifacts.leak_check``."""
        return {"kind": self.kind, "item": self.item, "field": self.field, "snippet": self.snippet[:200]}


def _match_phrase(text: str, item: str, field_name: str, kind: str) -> list[LeakFlag]:
    """Exact (normalised) phrase match, else >= 70% of the item's content tokens inside any
    12-token window (items with >= 2 content tokens only)."""
    words = _tokens(item)
    if not words:
        return []
    flags: list[LeakFlag] = []
    norm_text, norm_item = f" {_normalize(text)} ", f" {' '.join(words)} "
    if norm_item in norm_text:
        pattern = re.compile(r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9]+".join(map(re.escape, words)) + r"(?![A-Za-z0-9])", re.I)
        spans = [m.group(0) for m in pattern.finditer(text)]
        if spans:
            flags.append(LeakFlag(kind, item, field_name, spans, spans[0]))  # type: ignore[arg-type]
            return flags
    content = _content_tokens(item)
    if len(content) < 2:
        return flags
    sentence_spans = _sentences(text)
    toks: list[tuple[str, int]] = []  # (stem, sentence index)
    for si, (a, b) in enumerate(sentence_spans):
        toks.extend((_stem(t), si) for t in _tokens(text[a:b]))
    need = math.ceil(0.7 * len(content))
    cset = set(content)
    hit_sentences: set[int] = set()
    for i in range(max(1, len(toks) - 11)):
        window = toks[i : i + 12]
        present = {t for t, _ in window if t in cset}
        if len(present) >= need:
            hit_sentences.update(si for t, si in window if t in cset)
    if hit_sentences:
        spans = [text[sentence_spans[si][0] : sentence_spans[si][1]] for si in sorted(hit_sentences)]
        flags.append(LeakFlag("conceal_fuzzy" if kind == "conceal" else kind, item, field_name, spans, spans[0]))  # type: ignore[arg-type]
    return flags


def _load_pattern_signatures() -> dict[str, list[str]]:
    """Optional ``forensic_patterns.json`` signatures (keyword lists per anomaly kind)."""
    path = Path(__file__).resolve().parent / "benchmarks" / "forensic_patterns.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("could not read %s", path)
        return {}
    patterns = data.get("patterns", data) if isinstance(data, dict) else {}
    out: dict[str, list[str]] = {}
    if isinstance(patterns, dict):
        for kind, spec in patterns.items():
            sigs = spec.get("signatures") if isinstance(spec, dict) else None
            if isinstance(sigs, list):
                out[kind] = [str(s) for s in sigs]
    return out


def _gt_numbers(world: CaseWorld) -> list[tuple[str, float, str]]:
    """Ground-truth numbers ``(key, value, unit)`` from expected metrics and anomaly magnitudes."""
    out = [(m.key, float(m.value), m.unit) for m in world.ground_truth.expected_metrics]
    for a in world.ground_truth.anomalies:
        out.extend((k, float(v), "") for k, v in a.magnitude.items())
    return out


def _quotable(value: float, unit: str, key: str, public: Sequence[float]) -> bool:
    cands = [abs(value)] + ([abs(value) / 100.0] if _is_cents(unit, key) else [])
    for p in public:
        for c in cands:
            if c != 0 and abs(abs(p) - c) <= 0.005 * c:
                return True
    return False


def leak_scan(
    fields: Mapping[str, str],
    world: CaseWorld,
    *,
    public_numbers: Sequence[float] = (),
    stakeholder_fields: Mapping[str, str] | None = None,
) -> list[LeakFlag]:
    """Section 7 leak check over named text fields.

    Args:
        fields: ``{field_path: text}`` to scan.
        world: the case (ground truth + dossier seed).
        public_numbers: numbers legitimately quotable (public KPIs); ground-truth values equal to
            one of them are not flagged.
        stakeholder_fields: ``{field_path: stakeholder_name}`` for artifact bodies; those fields
            are also scanned against that stakeholder's ``hides``.
    """
    flags: list[LeakFlag] = []
    gt = world.ground_truth
    conceal = [c for c in world.dossier_seed.conceal if c.strip()]
    signatures = _load_pattern_signatures()
    vocab: list[tuple[str, list[str]]] = []
    for a in gt.anomalies:
        words = [_stem(w) for w in re.split(r"[_\W]+", a.kind.lower()) if len(w) >= 4]
        for sig in signatures.get(a.kind, []):
            words.extend(_stem(w) for w in _tokens(sig) if len(w) >= 4 and w not in _STOP)
        vocab.append((a.kind, list(dict.fromkeys(words))))
    numbers = [(k, v, u) for k, v, u in _gt_numbers(world)
               if v != 0 and not (float(v).is_integer() and abs(v) <= 12) and not _quotable(v, u, k, public_numbers)]
    hides_by_name = {s.name: s.hides for s in world.dossier_seed.stakeholders}

    for fname, text in fields.items():
        if not text:
            continue
        for item in conceal:
            flags.extend(_match_phrase(text, item, fname, "conceal"))
        for a in gt.anomalies:
            if len(a.id) >= 2:
                spans = [m.group(0) for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(a.id) + r"(?![A-Za-z0-9])", text)]
                if spans:
                    flags.append(LeakFlag("anomaly_id", a.id, fname, spans, spans[0]))
        for a, (kind, words) in zip(gt.anomalies, vocab, strict=True):
            if not words:
                continue
            need = min(2, len(words))
            for s0, s1 in _sentences(text):
                sent = text[s0:s1]
                low = sent.lower()
                stems = {_stem(t) for t in _tokens(sent)}
                if sum(1 for w in words if w in stems) >= need and any(c in low for c in _CAUSAL):
                    flags.append(LeakFlag("anomaly_vocab", kind, fname, [sent], sent))
        for tok in extract_numbers(text):
            if tok.is_year:
                continue
            for key, value, unit in numbers:
                if number_matches(tok, value, unit=unit, key=key):
                    flags.append(LeakFlag("number", f"{key}={value}", fname, [tok.raw], tok.raw))
                    break
        if stakeholder_fields and fname in stakeholder_fields:
            for item in hides_by_name.get(stakeholder_fields[fname], []):
                flags.extend(_match_phrase(text, item, fname, "stakeholder_hides"))
    return flags


def redact(fields: dict[str, str], flags: Iterable[LeakFlag]) -> tuple[dict[str, str], int]:
    """Replace every flagged span with ``[redacted]``. Returns ``(new_fields, n_redactions)``."""
    out = dict(fields)
    n = 0
    for flag in flags:
        text = out.get(flag.field)
        if text is None:
            continue
        for span in sorted(set(flag.spans), key=len, reverse=True):
            if span and span in text:
                n += text.count(span)
                text = text.replace(span, "[redacted]")
        out[flag.field] = text
    return out, n


def _string_fields(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten every string leaf of a JSON-like object to ``{path: text}``."""
    out: dict[str, str] = {}
    if isinstance(obj, str):
        out[prefix] = obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            out.update(_string_fields(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_string_fields(v, f"{prefix}[{i}]"))
    return out


def _set_path(obj: Any, path: str, value: str) -> None:
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    cur = obj
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        key: Any = int(part[1:-1]) if part.startswith("[") else part
        if last:
            cur[key] = value
        else:
            cur = cur[key]


# =========================================================================== deterministic grading


def normalize_sql(sql: str) -> str:
    """Lowercase, collapse whitespace, and remove whitespace before ``(`` and inside parens edges."""
    s = re.sub(r"\s+", " ", sql.lower()).strip()
    s = re.sub(r"\s+\(", "(", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s


def _num(cell: Any) -> float | None:
    if isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return float(cell) if math.isfinite(float(cell)) else None
    if isinstance(cell, str):
        c = cell.strip().replace(",", "").replace("$", "").replace("%", "")
        try:
            v = float(c)
        except ValueError:
            return None
        return v if math.isfinite(v) else None
    return None


def _split_row(line: str) -> list[str]:
    for sep in ("|", "\t", ","):
        if sep in line:
            return [c.strip() for c in line.strip().strip("|").split(sep)]
    return line.split()


def _first_row_numbers(result: Any) -> list[float]:
    """Numeric cells of the first data row of a result/preview (dict with ``rows``, list of
    rows/dicts, or a pipe/CSV/TSV text table with a header line)."""
    row: Iterable[Any] | None = None
    if isinstance(result, Mapping):
        rows = result.get("rows")
        if isinstance(rows, list) and rows:
            row = rows[0].values() if isinstance(rows[0], Mapping) else rows[0]
        elif "first_row" in result and isinstance(result["first_row"], (list, Mapping)):
            fr = result["first_row"]
            row = fr.values() if isinstance(fr, Mapping) else fr
    elif isinstance(result, list) and result:
        first = result[0]
        row = first.values() if isinstance(first, Mapping) else (first if isinstance(first, (list, tuple)) else result)
    elif isinstance(result, str):
        lines = [ln for ln in result.splitlines() if ln.strip() and not set(ln.strip()) <= set("-|+= ")]
        for ln in lines[1:]:
            nums = [v for v in (_num(c) for c in _split_row(ln)) if v is not None]
            if nums:
                return nums
        return []
    if row is None:
        return []
    return [v for v in (_num(c) for c in row) if v is not None]


def _numbers_cover(reference: Sequence[float], user: Sequence[float], tol: float) -> bool:
    for r in reference:
        if not any((abs(u - r) <= tol * abs(r)) if r != 0 else abs(u) <= 1e-9 for u in user):
            return False
    return True


# =========================================================================== simulation engine


class SimulationLimitError(ValueError):
    """The case's quarter budget (by difficulty) is exhausted."""


class ChapterLockedError(PermissionError):
    """Principal difficulty: chapters unlock only after the first submission (productive failure)."""


QUARTERS_BY_DIFFICULTY: dict[str, int] = {"foundation": 1, "practitioner": 2, "principal": 4}


def next_period(period: str) -> str:
    """``2024Q4 -> 2025Q1``; ``2024-11 -> 2025-02`` (one quarter); otherwise ``<period>+1``."""
    m = re.fullmatch(r"(\d{4})\s*-?\s*Q([1-4])", period.strip(), re.I)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        return f"{y + (q == 4)}Q{1 if q == 4 else q + 1}"
    m = re.fullmatch(r"(\d{4})-(\d{2})", period.strip())
    if m:
        y, mo = int(m.group(1)), int(m.group(2)) + 3
        y += (mo - 1) // 12
        mo = (mo - 1) % 12 + 1
        return f"{y}-{mo:02d}"
    m = re.fullmatch(r"(.*)\+(\d+)", period)
    if m:
        return f"{m.group(1)}+{int(m.group(2)) + 1}"
    return f"{period}+1"


def _condition(value: float, spec: Any) -> bool:
    """``resolved_if`` condition: number -> ``>=``; ``"<=45"``; or ``{"op": "<=", "value": 45}``."""
    op, thr = ">=", spec
    if isinstance(spec, Mapping):
        op, thr = str(spec.get("op", ">=")), spec.get("value")
    elif isinstance(spec, str):
        m = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*", spec)
        if not m:
            raise ValueError(f"bad resolved_if condition {spec!r}")
        op, thr = m.group(1), float(m.group(2))
    t = float(thr)
    return {">=": value >= t, "<=": value <= t, ">": value > t, "<": value < t, "==": abs(value - t) < 1e-9}[op]


def simulate_transition(
    simulation: SimulationState,
    levers: Sequence[Lever],
    decisions: Mapping[str, float],
    *,
    actual_periods: Sequence[str] = (),
    max_quarters: int | None = None,
) -> tuple[SimQuarter, dict[str, Any]]:
    """Deterministic quarter transition. Pure: returns the new quarter (no narrative) and info.

    ``simulation.model`` format (Module 2 archetypes emit this)::

        {
          "kpis": {"dso": 68.0, "cash_cents": 1.2e9, ...},       # baseline KPI levels
          "lever_effects": [                                       # applied in order
            {"lever": "terms_days", "kpi": "dso", "delta_per_unit": 0.6, "lag_periods": 0},
            {"lever": "price_change_pct", "kpi": "revenue_cents", "elasticity": -1.4, "lag_periods": 1}
          ],
          "anomaly_drag": {"cash_cents": -2.5e7},                  # per-quarter delta while unresolved
          "resolved_if": {"terms_days": "<=45"},                   # all must hold; number means >=
          # optional:
          "shocks": [{"quarter": 2, "kpi": "revenue_cents", "pct": -0.05, "headline": "..."}],
          "bounds": {"dso": [0, 365]},
          "max_quarters": 2,
          "round": 4
        }

    Semantics. Let ``v_j`` be lever value in simulated quarter ``j`` (``v_0 = lever.default``) and
    ``dv_j = v_j - v_{j-1}``. Starting from the previous quarter's KPIs (the last history entry,
    else ``model["kpis"]``), for quarter ``t`` and each effect with lag ``L`` (only if
    ``t - L >= 1``): ``delta_per_unit`` adds ``d * dv_{t-L}``; ``elasticity`` multiplies by
    ``1 + e * dv_{t-L} / ref`` where ``ref = |v_{t-L-1}|`` (or the lever range if that is 0).
    Holding a lever constant therefore causes no further change: levers move KPI *levels*.
    Then, unless every ``resolved_if`` condition holds for the current decisions, each
    ``anomaly_drag`` delta is added. Then shocks for quarter ``t`` (``delta`` additive or ``pct``
    multiplicative), then ``bounds`` clamps, then rounding (default 4 decimals).

    Decisions are clamped to ``[lever.min, lever.max]``; missing levers take their default;
    unknown keys raise ``ValueError``.
    """
    model = simulation.model or {}
    lever_map = {lv.key: lv for lv in levers}
    unknown = [k for k in decisions if k not in lever_map]
    if unknown:
        raise ValueError(f"unknown decision levers: {unknown}")
    actual = set(actual_periods)
    simulated = [q for q in simulation.history if q.period not in actual]
    limit = model.get("max_quarters", max_quarters)
    if limit is not None and len(simulated) >= int(limit):
        raise SimulationLimitError(f"simulation already ran {len(simulated)} of {limit} quarters")
    t = len(simulated) + 1
    current = {k: float(min(lv.max, max(lv.min, decisions.get(k, lv.default)))) for k, lv in lever_map.items()}

    def value_at(lever: str, j: int) -> float:
        if j <= 0:
            return float(lever_map[lever].default)
        if j == t:
            return current[lever]
        return float(simulated[j - 1].decisions.get(lever, lever_map[lever].default))

    prev = dict(simulation.history[-1].kpis) if simulation.history else {}
    for k, v in (model.get("kpis") or {}).items():
        prev.setdefault(k, float(v))
    nxt = {k: float(v) for k, v in prev.items()}
    applied: list[dict[str, Any]] = []
    for eff in model.get("lever_effects") or []:
        lever, kpi = eff.get("lever"), eff.get("kpi")
        if lever not in lever_map or kpi is None:
            logger.warning("ignoring lever effect with unknown lever/kpi: %s", eff)
            continue
        lag = int(eff.get("lag_periods", 0) or 0)
        j = t - lag
        if j < 1:
            continue
        dv = value_at(lever, j) - value_at(lever, j - 1)
        if dv == 0:
            continue
        base = nxt.get(kpi, 0.0)
        if "elasticity" in eff:
            lv = lever_map[lever]
            ref = abs(value_at(lever, j - 1)) or abs(lv.max - lv.min) or 1.0
            nxt[kpi] = base * (1 + float(eff["elasticity"]) * dv / ref)
        else:
            nxt[kpi] = base + float(eff.get("delta_per_unit", 0.0)) * dv
        applied.append({"lever": lever, "kpi": kpi, "lag": lag, "dv": dv, "before": base, "after": nxt[kpi]})
    conds = model.get("resolved_if") or {}
    resolved = bool(conds) and all(
        _condition(current.get(lv, float(lever_map[lv].default) if lv in lever_map else 0.0), spec) for lv, spec in conds.items()
    )
    if not resolved:
        for kpi, delta in (model.get("anomaly_drag") or {}).items():
            nxt[kpi] = nxt.get(kpi, 0.0) + float(delta)
    shock_hit: dict[str, Any] | None = None
    for shock in model.get("shocks") or []:
        if int(shock.get("quarter", -1)) == t:
            kpi = shock.get("kpi")
            if kpi is None:
                continue
            if "pct" in shock:
                nxt[kpi] = nxt.get(kpi, 0.0) * (1 + float(shock["pct"]))
            else:
                nxt[kpi] = nxt.get(kpi, 0.0) + float(shock.get("delta", 0.0))
            shock_hit = dict(shock)
    for kpi, bounds in (model.get("bounds") or {}).items():
        if kpi in nxt and isinstance(bounds, (list, tuple)) and len(bounds) == 2:
            lo, hi = bounds
            if lo is not None:
                nxt[kpi] = max(float(lo), nxt[kpi])
            if hi is not None:
                nxt[kpi] = min(float(hi), nxt[kpi])
    digits = int(model.get("round", 4))
    nxt = {k: round(v, digits) for k, v in nxt.items()}
    base_period = simulation.history[-1].period if simulation.history else simulation.current_period
    period = next_period(base_period)
    quarter = SimQuarter(period=period, decisions=current, kpis=nxt, narrative=None)
    return quarter, {"prev": prev, "resolved": resolved, "shock": shock_hit, "effects": applied, "quarter_index": t}


# =========================================================================== adversary ladder

LADDER: tuple[str, ...] = ("clarify", "probe_assumption", "counter_evidence", "second_order", "concede_or_escalate")
MAX_RUNG: dict[str, int] = {"foundation": 3, "practitioner": 4, "principal": 5}
PERSONAS: dict[str, tuple[str, tuple[str, ...], str]] = {
    # pillar -> (role, role keywords to find a matching stakeholder, default agenda)
    "accounting_finance": ("CFO", ("cfo", "chief financial", "finance"), "protect liquidity and covenant headroom and avoid a restatement"),
    "operations_strategy": ("COO", ("coo", "chief operating", "operations"), "hit throughput and margin targets without new capex"),
    "system_design": ("VP of Engineering", ("vp of engineering", "vp engineering", "cto", "engineering"), "ship the roadmap without another incident or a rewrite"),
    "sql": ("Head of Data", ("head of data", "data", "analytics"), "keep the numbers the board sees reproducible and defensible"),
    "python": ("Head of Data", ("head of data", "data", "analytics"), "keep the numbers the board sees reproducible and defensible"),
}


def rung_for(adversary_index: int, max_rung: int) -> int:
    """Rung (1-5) of the adversary's ``adversary_index``-th turn (0 = opening). One rung per turn
    up to ``max_rung``; after that the adversary closes at rung 5 (``concede_or_escalate``)."""
    r = adversary_index + 1
    return r if r <= max_rung else len(LADDER)


# =========================================================================== orchestration


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def _bullets(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        m = re.match(r"\s*(?:[-*•]|\d+[.)])\s+(.*)", line)
        if m and m.group(1).strip():
            items.append(m.group(1).strip())
    return items or ([text.strip()] if text.strip() else [])


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


class Pedagogy:
    """Orchestrates every LLM-backed pedagogy path against a :class:`CaseWorld`.

    Args:
        client: the LLM client (use ``FakeTransport`` offline).
        graph: the skill graph (BKT updates on first graded submission per case).
        persistence: app DB (artifacts cache, submissions, adversary turns, sim quarters).
        id_factory: submission id generator (default ``sub_<12 hex>``).
    """

    def __init__(
        self,
        client: LLMClient,
        graph: SkillGraph,
        persistence: Persistence,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.client = client
        self.graph = graph
        self.persistence = persistence
        self._new_id = id_factory or (lambda: "sub_" + uuid.uuid4().hex[:12])

    # ------------------------------------------------------------------ context

    def _ensure_case(self, world: CaseWorld) -> None:
        if self.persistence.get_case(world.case_id) is None:
            self.persistence.upsert_case(
                CaseRecord(
                    case_id=world.case_id, seed=world.seed, archetype=world.archetype, difficulty=world.difficulty,
                    company_name=world.company.name, industry=world.industry.code, pillars=list(world.pillars),
                    skill_nodes=list(world.skill_nodes), world_path="", db_path=world.database.path,
                    status="generated", created_at=utc_now_iso(),
                )
            )

    @staticmethod
    def public_kpis(world: CaseWorld) -> dict[str, dict[str, float]]:
        """Headline KPIs by period that are safe to quote: ``operations.kpis_by_period`` plus
        statement headlines (revenue, gross profit, operating income, net income, ending cash)."""
        out: dict[str, dict[str, float]] = {p: {} for p in world.periods}
        for p, kpis in world.operations.kpis_by_period.items():
            out.setdefault(p, {}).update({k: float(v) for k, v in kpis.items()})
        for inc in world.finance.statements.income:
            out.setdefault(inc.period, {}).update({
                "revenue_cents": float(inc.revenue_cents), "gross_profit_cents": float(inc.gross_profit_cents),
                "operating_income_cents": float(inc.operating_income_cents), "net_income_cents": float(inc.net_income_cents),
            })
        for cf in world.finance.statements.cash_flow:
            out.setdefault(cf.period, {})["ending_cash_cents"] = float(cf.ending_cash_cents)
        return {p: v for p, v in out.items() if v}

    @classmethod
    def _public_numbers(cls, world: CaseWorld) -> list[float]:
        nums: list[float] = []
        for kpis in cls.public_kpis(world).values():
            for k, v in kpis.items():
                nums.append(v)
                if k.endswith("_cents"):
                    nums.append(v / 100.0)
        return nums

    @staticmethod
    def headlines(world: CaseWorld, period: str | None = None) -> dict[str, float]:
        """Statement headlines (revenue, gross profit, operating income, cash, DSO, DIO, DPO)."""
        st = world.finance.statements
        period = period or (world.periods[-1] if world.periods else None)
        out: dict[str, float] = {}
        inc = next((i for i in st.income if i.period == period), st.income[-1] if st.income else None)
        if inc is not None:
            out.update({"revenue_cents": inc.revenue_cents, "gross_profit_cents": inc.gross_profit_cents,
                        "operating_income_cents": inc.operating_income_cents})
        cf = next((c for c in st.cash_flow if c.period == period), st.cash_flow[-1] if st.cash_flow else None)
        if cf is not None:
            out["cash_cents"] = cf.ending_cash_cents
        ratios = st.ratios.get(period or "", {}) if period else {}
        for r in ("dso", "dio", "dpo"):
            if r in ratios:
                out[r] = float(ratios[r])
        return {k: float(v) for k, v in out.items()}

    def _call(
        self,
        name: str,
        messages: list[Message],
        parse_as: type[BaseModel],
        *,
        purpose: str | None = None,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> Any:
        prompt = PROMPTS[name]
        purpose = purpose or name
        if not stream:
            resp = self.client.complete(messages, tier=prompt.tier, json_schema=prompt.json_schema, purpose=purpose, parse_as=parse_as)
            return resp.obj
        gen: Generator[str, None, Any] = self.client.stream_json(
            messages, tier=prompt.tier, json_schema=prompt.json_schema, purpose=purpose, parse_as=parse_as
        )
        while True:
            try:
                delta = next(gen)
            except StopIteration as stop:
                return stop.value
            if on_delta is not None:
                on_delta(delta)

    # ------------------------------------------------------------------ leak-checked generation

    def _generate_checked(
        self,
        name: str,
        messages: list[Message],
        parse_as: type[BaseModel],
        world: CaseWorld,
        *,
        skip_fields: Sequence[str] = (),
        stakeholder_list_field: str | None = None,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Generate, leak-scan, regenerate once on a leak, then redact. Returns
        ``(payload_dict, leak_check_meta)``."""
        public = self._public_numbers(world)

        def scan(payload: dict[str, Any]) -> tuple[dict[str, str], list[LeakFlag]]:
            fields = {k: v for k, v in _string_fields(payload).items() if not any(k == s or k.startswith(s + "[") or k.startswith(s + ".") for s in skip_fields)}
            stake: dict[str, str] = {}
            if stakeholder_list_field:
                for i, art in enumerate(payload.get(stakeholder_list_field) or []):
                    if isinstance(art, Mapping):
                        stake[f"{stakeholder_list_field}[{i}].body"] = str(art.get("stakeholder", ""))
            return fields, leak_scan(fields, world, public_numbers=public, stakeholder_fields=stake)

        obj = self._call(name, messages, parse_as, stream=stream, on_delta=on_delta)
        payload = obj.model_dump(mode="json")
        fields, flags = scan(payload)
        meta: dict[str, Any] = {"attempts": 1, "redactions": 0, "flags": [f.as_dict() for f in flags]}
        if not flags:
            return payload, meta
        snippets = list(dict.fromkeys(f.snippet[:160] for f in flags))
        logger.warning("%s leaked %d item(s); regenerating once", name, len(flags))
        retry = [
            *messages[:-1],
            {"role": "user", "content": messages[-1]["content"]
             + "\n\nYour previous draft leaked concealed information; you leaked: " + _j(snippets)
             + "\nDO NOT INCLUDE: " + _j(snippets)},
        ]
        obj2 = self._call(name, retry, parse_as, purpose=f"{name}:regenerate", stream=stream, on_delta=on_delta)
        payload2 = obj2.model_dump(mode="json")
        fields2, flags2 = scan(payload2)
        meta = {"attempts": 2, "redactions": 0, "flags": [f.as_dict() for f in flags2], "first_attempt_flags": len(flags)}
        if flags2:
            redacted, n = redact(fields2, flags2)
            for path, text in redacted.items():
                if text != fields2[path]:
                    _set_path(payload2, path, text)
            meta["redactions"] = n
            logger.warning("%s still leaked after regeneration; redacted %d span(s)", name, n)
        return payload2, meta

    # ------------------------------------------------------------------ dossier

    def _deliverable_spec(self, world: CaseWorld) -> str:
        asks = [f"- `{m.key}` ({m.unit})" for m in world.ground_truth.expected_metrics]
        lines = [
            "An executive memo (answer first, then evidence, then recommendation with an owner).",
            "Root-cause diagnosis with the mechanism, quantified in the company's own numbers.",
            "SQL/Python evidence that reproduces every asserted metric.",
            "The following metrics, each with unit and period:",
            *asks,
        ]
        if world.dossier_seed.architecture_mermaid or "system_design" in world.pillars:
            lines.insert(3, "An architecture diagram (mermaid) of the failure path and the fix.")
        return "\n".join(f"- {ln}" if not ln.startswith("- ") else ln for ln in lines)

    @staticmethod
    def _table_index(world: CaseWorld) -> list[dict[str, Any]]:
        return [{"table": t.name, "columns": [c.name for c in t.columns], "row_count": t.row_count}
                for t in world.database.tables]

    def synthesize_dossier(
        self,
        world: CaseWorld,
        *,
        stream: bool = False,
        force: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> Dossier:
        """Write the case dossier from ``dossier_seed`` and public facts only, leak-checked and
        cached as artifact ``kind="dossier"``. ``stream=True`` forwards text deltas to ``on_delta``."""
        self._ensure_case(world)
        if not force:
            cached = self.persistence.get_artifact(world.case_id, "dossier", "")
            if cached is not None:
                return Dossier.model_validate(cached)
        seed = world.dossier_seed
        messages = PROMPTS["case_synthesizer"].render(
            company_json=_j(world.company.model_dump(mode="json")),
            narrative_hooks_json=_j(world.industry.narrative_hooks),
            periods_json=_j(world.periods),
            kpis_json=_j(self.public_kpis(world)),
            reveal_json=_j(seed.reveal),
            conceal_json=_j(seed.conceal),
            stakeholders_json=_j([s.model_dump(mode="json") for s in seed.stakeholders]),
            timeline_json=_j([t.model_dump(mode="json") for t in seed.timeline]),
            artifacts_json=_j(seed.artifacts),
            architecture_mermaid=seed.architecture_mermaid or "null",
            table_index_json=_j(self._table_index(world)),
            difficulty=world.difficulty,
            deliverable_spec=self._deliverable_spec(world),
        )
        payload, meta = self._generate_checked(
            "case_synthesizer", messages, DossierLLM, world, skip_fields=("architecture_mermaid",),
            stakeholder_list_field="stakeholder_artifacts", stream=stream, on_delta=on_delta,
        )
        raw = DossierLLM.model_validate(payload)
        dossier = Dossier(
            title=raw.title,
            situation=raw.situation,
            background_markdown=raw.company_background,
            stakeholder_messages=[
                StakeholderMessage(from_name=a.stakeholder, role=a.role, channel=a.format, subject=a.subject, body=a.body)
                for a in raw.stakeholder_artifacts
            ],
            the_ask=raw.the_ask,
            deliverables=_bullets(raw.deliverable_spec),
            data_room_index=[f"{e.table} — {e.note}" if e.note else e.table for e in raw.data_room_index],
            timeline=[t.model_dump() for t in raw.timeline],
            architecture_mermaid=seed.architecture_mermaid,
            leak_redacted=meta["redactions"] > 0,
        )
        self.persistence.save_artifact(world.case_id, "dossier", "", {**dossier.model_dump(mode="json"), "leak_check": meta})
        case = self.persistence.get_case(world.case_id)
        if case is not None and case.status == "generated":
            self.persistence.set_case_status(world.case_id, "briefed")
        return dossier

    # ------------------------------------------------------------------ textbook

    def textbook_chapter(
        self,
        world: CaseWorld,
        skill_node: str,
        *,
        stream: bool = False,
        force: bool = False,
        allow_locked: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> TextbookChapter:
        """One chapter for ``skill_node`` using the case's public numbers; leak-checked and cached
        as artifact ``kind="textbook", key=skill_node``. At principal difficulty chapters unlock
        only after the first submission unless ``allow_locked``."""
        if skill_node not in self.graph.nodes:
            raise KeyError(f"unknown skill node {skill_node!r}")
        self._ensure_case(world)
        if not force:
            cached = self.persistence.get_artifact(world.case_id, "textbook", skill_node)
            if cached is not None:
                return TextbookChapter.model_validate(cached)
        if world.difficulty == "principal" and not allow_locked and not self.persistence.list_submissions(world.case_id):
            raise ChapterLockedError("principal cases unlock chapters after the first submission")
        node = self.graph.nodes[skill_node]
        tables = world.database.tables[:8]
        messages = PROMPTS["textbook_chapter"].render(
            node_id=node.id,
            node_title=node.title,
            node_summary=node.description,
            mental_models_json=_j(node.mental_models),
            prereq_titles_json=_j([self.graph.nodes[p].title for p in self.graph.prereqs(node.id)]),
            pillar=node.pillar,
            company_json=_j(world.company.model_dump(mode="json")),
            periods_json=_j(world.periods),
            kpis_json=_j(self.public_kpis(world)),
            schema_excerpt_json=_j([{"table": t.name, "columns": [{"name": c.name, "type": c.type, "description": c.description} for c in t.columns]} for t in tables]),
            sample_rows_json=_j({t.name: t.sample_rows[:3] for t in tables[:3]}),
        )
        payload, meta = self._generate_checked("textbook_chapter", messages, ChapterLLM, world, skip_fields=("node_id",), stream=stream, on_delta=on_delta)
        raw = ChapterLLM.model_validate(payload)
        chapter = TextbookChapter(
            skill_node=skill_node, title=raw.title, markdown=raw.chapter_markdown,
            self_checks=[SelfCheck(question=s.question, answer=s.answer) for s in raw.self_check],
            snippets=raw.snippets, leak_redacted=meta["redactions"] > 0,
        )
        self.persistence.save_artifact(world.case_id, "textbook", skill_node, {**chapter.model_dump(mode="json"), "leak_check": meta})
        return chapter

    # ------------------------------------------------------------------ deterministic grade

    def grade_deterministic(
        self,
        world: CaseWorld,
        submission: Submission,
        sql_runner: Callable[[str], Any] | None = None,
    ) -> DeterministicGrade:
        """Authoritative numeric check.

        Metrics: pass if ``|s - e| <= tolerance_abs`` or ``|s - e| <= tolerance_rel * |e|`` (either
        declared tolerance suffices; with neither declared, 2% relative). Missing -> fail.

        Queries: offline, a reference query matches when one submitted SQL (normalised) contains
        every construct in ``ReferenceQuery.expected["signature"]``; queries without a signature
        are not evaluated. With ``sql_runner`` the reference SQL is executed and its first-row
        numeric cells must each be matched (within ``ReferenceQuery.tolerance``) by the first-row
        numeric cells of some submitted ``result_preview``; if that comparison is impossible
        (runner error, no previews, no numeric cells) the signature heuristic is used.
        """
        results: list[MetricResult] = []
        for em in world.ground_truth.expected_metrics:
            sub = submission.metrics.get(em.key)
            ok = False
            if sub is not None and math.isfinite(float(sub)):
                diff = abs(float(sub) - em.value)
                checks = []
                if em.tolerance_abs is not None:
                    checks.append(diff <= em.tolerance_abs + 1e-12)
                if em.tolerance_rel is not None:
                    checks.append(diff <= em.tolerance_rel * abs(em.value) + 1e-12)
                if not checks:
                    checks.append(diff <= 0.02 * abs(em.value) + 1e-12)
                ok = any(checks)
            results.append(MetricResult(
                key=em.key, submitted=float(sub) if sub is not None else None, expected=em.value, unit=em.unit,
                within_tolerance=ok, tolerance_abs=em.tolerance_abs, tolerance_rel=em.tolerance_rel,
                derivation=em.derivation, pillar=metric_pillar(em.key, world), anomaly_ids=metric_anomaly_ids(em.key, world),
            ))
        user_sql = [normalize_sql(e.sql) for e in submission.sql_evidence]
        previews = [_first_row_numbers(e.result_preview) for e in submission.sql_evidence if e.result_preview is not None]
        previews = [p for p in previews if p]
        matches: list[QuerySignatureMatch] = []
        for anomaly in world.ground_truth.anomalies:
            for q in anomaly.reference_sql:
                signature = [normalize_sql(str(s)) for s in (q.expected.get("signature") or [])]
                executed: QuerySignatureMatch | None = None
                if sql_runner is not None and previews:
                    try:
                        ref = _first_row_numbers(sql_runner(q.sql))
                    except Exception as exc:  # runner is user-supplied infrastructure
                        logger.warning("reference query %s failed: %s", q.name, exc)
                        ref = []
                    if ref:
                        ok = any(_numbers_cover(ref, p, q.tolerance) for p in previews)
                        executed = QuerySignatureMatch(
                            name=q.name, matched=ok, pillar=anomaly.pillar, anomaly_id=anomaly.id, method="execution",
                            detail=("first-row values reproduced" if ok else "no submitted result reproduced the reference first row")
                            + f" (tolerance {q.tolerance:.1%})",
                        )
                if executed is not None:
                    matches.append(executed)
                    continue
                if not signature:
                    matches.append(QuerySignatureMatch(name=q.name, matched=False, pillar=anomaly.pillar, anomaly_id=anomaly.id,
                                                       evaluated=False, detail="no signature declared; not evaluated offline"))
                    continue
                best_missing = signature
                for s in user_sql:
                    missing = [c for c in signature if c not in s]
                    if len(missing) < len(best_missing):
                        best_missing = missing
                    if not missing:
                        break
                ok = not best_missing and bool(user_sql)
                matches.append(QuerySignatureMatch(
                    name=q.name, matched=ok, pillar=anomaly.pillar, anomaly_id=anomaly.id, method="signature",
                    detail="all signature constructs present" if ok else f"missing constructs: {best_missing}",
                ))
        passed_metrics = sum(1 for r in results if r.within_tolerance)
        evaluated = [m for m in matches if m.evaluated]
        total = len(results) + len(evaluated)
        passed = passed_metrics + sum(1 for m in evaluated if m.matched)
        return DeterministicGrade(
            metric_results=results, query_signature_matches=matches,
            accuracy=round(passed_metrics / len(results), 6) if results else 0.0,
            det_pass=round(passed / total, 6) if total else 0.0, items_passed=passed, items_total=total,
        )

    # ------------------------------------------------------------------ full grade

    def _candidate_nodes(self, world: CaseWorld) -> list[str]:
        nodes = self.graph.case_nodes(world)
        out = list(nodes)
        for n in nodes:
            out.extend(p for p in self.graph.prereqs(n) if p not in out)
        return out

    @staticmethod
    def _submission_corpus(sub: Submission) -> str:
        parts = [sub.memo_markdown, sub.design_mermaid or "", _j(sub.metrics), _j(sub.financial_model or {})]
        for e in sub.sql_evidence:
            parts += [e.name, e.sql, e.result_preview if isinstance(e.result_preview, str) else _j(e.result_preview)]
        for e in sub.python_evidence:
            parts += [e.name, e.code, e.stdout_tail, _j(e.result_json)]
        return _ws("\n".join(parts))

    def grade(
        self,
        world: CaseWorld,
        submission: Submission,
        *,
        sql_runner: Callable[[str], Any] | None = None,
        submission_id: str | None = None,
    ) -> GradeReport:
        """Deterministic grade, LLM rubric (with ground truth), code-side clamps and ``overall``,
        BKT (first graded submission of the case only), reveal, adversary opening; persisted."""
        self._ensure_case(world)
        sid = submission_id or self._new_id()
        det = self.grade_deterministic(world, submission, sql_runner)
        all_metrics_pass = all(r.within_tolerance for r in det.metric_results)
        ceiling = te_ceiling(det.det_pass, items_total=det.items_total, difficulty=world.difficulty, all_metrics_pass=all_metrics_pass)
        gt = world.ground_truth
        candidates = self._candidate_nodes(world)
        messages = PROMPTS["grader"].render(
            archetype=world.archetype,
            difficulty=world.difficulty,
            root_cause=gt.root_cause,
            anomalies_json=_j([a.model_dump(mode="json") for a in gt.anomalies]),
            red_herrings_json=_j(gt.red_herrings),
            expected_metrics_json=_j([m.model_dump(mode="json") for m in gt.expected_metrics]),
            deterministic_grade_json=_j(det.model_dump(mode="json")),
            ceiling_te=f"{ceiling:g}",
            memo_markdown=submission.memo_markdown,
            metrics_json=_j(submission.metrics),
            sql_evidence_json=_j([e.model_dump(mode="json") for e in submission.sql_evidence]),
            python_evidence_json=_j([e.model_dump(mode="json") for e in submission.python_evidence]),
            design_mermaid=submission.design_mermaid or "null",
            financial_model_json=_j(submission.financial_model),
            candidate_nodes_json=_j(candidates),
        )
        if world.difficulty == "principal":
            messages[-1]["content"] += (
                "\n\nDIFFICULTY STRICTNESS (principal): business_judgment >= 8 requires a quantified "
                "second-order effect; risk_awareness >= 8 requires at least one red herring explicitly "
                "rejected with evidence."
            )
        raw: GraderLLM = self._call("grader", messages, GraderLLM)

        corpus = self._submission_corpus(submission)
        dims: dict[str, RubricDimension] = {}
        dropped = 0
        for name in RUBRIC_DIMENSIONS:
            d = raw.dimensions[name]
            evidence = [q for q in d.evidence if _ws(q) and _ws(q) in corpus]
            dropped += len(d.evidence) - len(evidence)
            dims[name] = RubricDimension(score=float(d.score), evidence=evidence, gaps=list(d.gaps))
        raw_scores = {k: float(v.score) for k, v in raw.dimensions.items() if k in RUBRIC_DIMENSIONS}
        clamps: list[str] = []
        te = dims["technical_execution"]
        if te.score > ceiling:
            clamps.append(f"technical_execution {te.score:g} -> {ceiling:g} (deterministic pass rate {det.det_pass:.2f})")
            te.score = ceiling
        critical = {a.id for a in gt.anomalies if a.severity == "critical"}
        crit_failed = any(not r.within_tolerance and set(r.anomaly_ids) & critical for r in det.metric_results)
        bj = dims["business_judgment"]
        if crit_failed and not raw.misdiagnosis and bj.score > 6:
            clamps.append(f"business_judgment {bj.score:g} -> 6 (a critical anomaly's expected metric failed)")
            bj.score = 6.0
        if dropped:
            clamps.append(f"dropped {dropped} evidence quote(s) not found verbatim in the submission")
        overall = rubric_overall({k: v.score for k, v in dims.items()})
        if raw.overall is not None and abs(raw.overall - overall) > 0.5:
            logger.info("grader overall drift: llm=%.1f code=%.1f", raw.overall, overall)
        drill = [n for n in raw.next_drill_nodes if n in candidates][:3]
        rubric = RubricGrade(
            dimensions=dims, overall=overall, misdiagnosis=raw.misdiagnosis, strongest_point=raw.strongest_point,
            weakest_point=raw.weakest_point, next_drill_nodes=drill, stated_root_cause=raw.stated_root_cause,
            raw_scores=raw_scores, ceiling_te=ceiling, llm_overall=raw.overall, clamps=clamps,
        )
        report = GradeReport(
            submission_id=sid, deterministic=det, rubric=rubric,
            revealed=RevealedTruth(root_cause=gt.root_cause, anomalies=list(gt.anomalies), reference_solution=gt.reference_solution),
        )
        report.observations = self.graph.observations_from_grade(report, world)
        first = not any(r.grade for r in self.persistence.list_submissions(world.case_id) if r.submission_id != sid)
        report.counted_for_mastery = first
        if first:
            report.skill_updates = self.graph.apply_grade(report, world)
        if not report.rubric.next_drill_nodes:
            report.rubric.next_drill_nodes = [o.node for o in sorted(report.observations, key=lambda o: o.score) if not o.correct][:3]

        self.persistence.save_submission(sid, submission)
        opening = self._adversary(world, sid, report, user_message=None)
        report.adversary_opening = opening.message
        self.persistence.set_submission_grade(sid, report.model_dump(mode="json"))
        self.persistence.set_case_status(world.case_id, "graded")
        return report

    # ------------------------------------------------------------------ adversary

    def persona(self, world: CaseWorld) -> dict[str, str]:
        """Persona by primary pillar (``world.pillars[0]``, else the most severe anomaly's pillar):
        a matching stakeholder from the seed if one exists, else a seeded generated name."""
        severity = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        pillar = world.pillars[0] if world.pillars else (
            max(world.ground_truth.anomalies, key=lambda a: severity[a.severity]).pillar if world.ground_truth.anomalies else "accounting_finance"
        )
        role, keywords, agenda = PERSONAS[pillar]
        for s in world.dossier_seed.stakeholders:
            if any(k in s.role.lower() for k in keywords):
                return {"name": s.name, "role": s.role, "agenda": s.agenda or agenda, "pillar": pillar}
        name = SeededRNG.for_world(world.archetype, world.seed, world.version).spawn("adversary_persona").person_name()
        return {"name": name, "role": role, "agenda": agenda, "pillar": pillar}

    def _known_cite_keys(self, world: CaseWorld) -> set[str]:
        keys = {m.key for m in world.ground_truth.expected_metrics}
        for kpis in self.public_kpis(world).values():
            keys.update(kpis)
        keys.update(lv.key for lv in world.ground_truth.decision_levers)
        return keys

    def _adversary(self, world: CaseWorld, sid: str, report: GradeReport, user_message: str | None) -> AdversaryTurn:
        turns = self.persistence.list_turns(sid)
        adv_turns = [t for t in turns if t.role == "adversary"]
        persona = dict(adv_turns[0].payload.get("persona_meta", {})) if adv_turns else {}
        if not persona:
            persona = self.persona(world)
        max_rung = MAX_RUNG.get(world.difficulty, 4)
        rung = rung_for(len(adv_turns), max_rung)
        challenge = LADDER[rung - 1]
        if user_message is not None:
            self.persistence.append_adversary_turn(sid, "user", {"message": user_message})
        history = [
            {"role": "analyst" if t.role == "user" else persona["name"], "message": t.payload.get("message", "")}
            for t in turns
        ]
        gt = world.ground_truth
        trail = next((a.evidence_trail for a in sorted(gt.anomalies, key=lambda a: a.severity != "critical")), [])
        traps = [{"challenge": rh, "correct_rebuttal": "; ".join(trail[:2]) or "[not provided]"} for rh in gt.red_herrings]
        traps += [{"challenge": f"{e.headline}: {e.detail}".strip(": "), "correct_rebuttal": "; ".join(trail[:2]) or "[not provided]"}
                  for e in world.dossier_seed.timeline if e.is_red_herring]
        submission = self.persistence.get_submission(sid)
        memo = submission.submitted.memo_markdown if submission else ""
        messages = PROMPTS["adversary"].render(
            persona_name=persona["name"], persona_role=persona["role"], company_name=world.company.name,
            persona_agenda=persona["agenda"], max_rung=str(max_rung), rung=f"{rung} ({challenge})",
            red_herrings_json=_j(gt.red_herrings), socratic_traps_json=_j(traps),
            levers_json=_j([lv.model_dump(mode="json") for lv in gt.decision_levers]),
            expected_metrics_json=_j([{"key": m.key, "value": m.value, "unit": m.unit} for m in gt.expected_metrics]),
            kpis_json=_j(self.public_kpis(world)), overall=f"{report.rubric.overall:g}",
            misdiagnosis=str(report.rubric.misdiagnosis).lower(), weakest_point=report.rubric.weakest_point,
            memo_markdown=memo, history_json=_j(history),
            latest_reply=user_message if user_message is not None else "(none yet: open the meeting)",
        )
        try:
            raw: AdversaryLLM = self._call("adversary", messages, AdversaryLLM)
            message, cites, concedes = raw.message, raw.cites, raw.concedes
        except LLMError as exc:
            if user_message is not None:
                raise
            logger.warning("adversary opening failed (%s); using deterministic opening", exc)
            message = ("Before we go further: state your conclusion precisely. Which metric, in which period, "
                       "supports it, and what number did you get?")
            cites, concedes = [], False
        known = self._known_cite_keys(world)
        turn = AdversaryTurn(
            persona=persona["name"], message=message, challenge_type=challenge,
            cites=[c for c in cites if c in known],
            concedes=bool(concedes) and challenge == "concede_or_escalate", turn_index=0, rung=rung,
        )
        rec = self.persistence.append_adversary_turn(sid, "adversary", {**turn.model_dump(mode="json"), "persona_meta": persona})
        turn.turn_index = rec.turn_index
        return turn

    def adversary_turn(self, world: CaseWorld, submission_id: str, user_message: str) -> AdversaryTurn:
        """Next Socratic turn: rung by adversary-turn index (one rung per turn up to the
        difficulty's max rung, then ``concede_or_escalate``); cites validated; persisted.
        Never updates BKT."""
        record = self.persistence.get_submission(submission_id)
        if record is None or record.case_id != world.case_id:
            raise KeyError(f"unknown submission {submission_id!r} for case {world.case_id!r}")
        if not record.grade:
            raise ValueError("submission has not been graded yet")
        report = GradeReport.model_validate(record.grade)
        return self._adversary(world, submission_id, report, user_message)

    # ------------------------------------------------------------------ quarter simulation

    @staticmethod
    def _allowed_numbers(*sources: Mapping[str, float] | Iterable[float]) -> list[float]:
        out: list[float] = []
        for src in sources:
            vals = src.values() if isinstance(src, Mapping) else src
            out.extend(float(v) for v in vals if v is not None)
        return out

    @staticmethod
    def check_narrative_numbers(text: str, allowed: Sequence[float]) -> tuple[str, list[str]]:
        """Strip every sentence containing a number not in ``allowed`` (0.5% / sig-fig match;
        cents values also match dollars, ratios also match percents). Years and integers 0-10
        are structural and always allowed. Returns ``(clean_text, stripped_sentences)``."""
        stripped: list[str] = []
        out_lines: list[str] = []
        for line in text.splitlines():
            kept: list[str] = []
            for a, b in _sentences(line):
                sent = line[a:b]
                bad = False
                for tok in extract_numbers(sent):
                    if tok.is_year or (not tok.is_pct and tok.value.is_integer() and tok.value <= 10 and "$" not in tok.raw):
                        continue
                    if not any(number_matches(tok, v, unit="ratio" if abs(v) <= 1 else "", key="")
                               or number_matches(tok, v, unit="cents") for v in allowed):
                        bad = True
                        break
                if bad:
                    stripped.append(sent.strip())
                else:
                    kept.append(sent)
            if kept or not line.strip():
                out_lines.append(" ".join(kept) if kept else line)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip(), stripped

    def advance_quarter(self, world: CaseWorld, submission_id: str, decisions: Mapping[str, float]) -> AdvanceResult:
        """Deterministic transition (:func:`simulate_transition`) then the narrator. Appends to
        ``world.simulation.history`` in place, persists the quarter and returns the updated
        :class:`SimulationState` for the API to rewrite ``world.json``."""
        record = self.persistence.get_submission(submission_id)
        if record is None or record.case_id != world.case_id:
            raise KeyError(f"unknown submission {submission_id!r} for case {world.case_id!r}")
        levers = world.ground_truth.decision_levers
        quarter, info = simulate_transition(
            world.simulation, levers, decisions, actual_periods=world.periods,
            max_quarters=QUARTERS_BY_DIFFICULTY.get(world.difficulty),
        )
        prev = info["prev"]
        now = quarter.kpis
        deltas = {k: now[k] - prev[k] for k in now if k in prev}
        pct = {k: (now[k] - prev[k]) / prev[k] for k in now if prev.get(k)}
        heads = self.headlines(world)
        shock = info["shock"]
        messages = PROMPTS["quarter_narrator"].render(
            company_name=world.company.name, period=quarter.period, decisions_json=_j(quarter.decisions),
            levers_json=_j([lv.model_dump(mode="json") for lv in levers]), kpis_prev_json=_j(prev),
            kpis_now_json=_j(now), headlines_json=_j(heads), shock_json=_j(shock),
            anomaly_active=str(not info["resolved"]).lower(),
        )
        watch: list[dict[str, Any]] = []
        stripped: list[str] = []
        try:
            raw: NarratorLLM = self._call("quarter_narrator", messages, NarratorLLM)
            lever_bounds = [x for lv in levers for x in (lv.min, lv.max, lv.default)]
            shock_nums = [float(v) for k, v in (shock or {}).items() if isinstance(v, (int, float)) and k != "quarter"]
            allowed = self._allowed_numbers(prev, now, deltas, pct, quarter.decisions, heads, lever_bounds, shock_nums)
            narrative, stripped = self.check_narrative_numbers(raw.narrative_markdown, allowed)
            watch = raw.watch_items
            if stripped:
                logger.warning("narrator: stripped %d sentence(s) with unsupported numbers", len(stripped))
        except LLMError as exc:
            logger.warning("quarter narrator failed (%s); storing quarter without narrative", exc)
            narrative = None
        quarter.narrative = narrative
        world.simulation.history.append(quarter)
        world.simulation.current_period = quarter.period
        self.persistence.save_sim_quarter(world.case_id, quarter, submission_id)
        return AdvanceResult(
            quarter=quarter, simulation=world.simulation.model_copy(deep=True), resolved=info["resolved"],
            shock=shock, watch_items=watch, stripped_sentences=stripped,
        )
