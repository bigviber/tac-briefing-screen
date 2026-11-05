"""Shared pydantic models for Axiom (ARCHITECTURE.md sections 4 and 5).

World models (everything that goes into ``world.json``) forbid unknown fields so that a
drift between modules fails loudly. API request/response models ignore unknown fields so
that the frontend can evolve without breaking the backend.

Money is always integer cents. Percentages are floats in ``[0, 1]``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    # literals
    "Pillar",
    "Difficulty",
    "Distribution",
    "LLMProvider",
    # research tier
    "Benchmark",
    "IndustryProfile",
    # finance
    "Account",
    "JournalLine",
    "JournalEntry",
    "StatementLine",
    "IncomeStatement",
    "BalanceSheet",
    "CashFlowStatement",
    "FinancialStatements",
    "InvariantCheck",
    "FinanceState",
    # operations
    "Station",
    "Cohort",
    "OperationsState",
    # database
    "ColumnSpec",
    "TableSpec",
    "DatabaseSpec",
    # ground truth
    "ReferenceQuery",
    "Anomaly",
    "ExpectedMetric",
    "ReferenceSolution",
    "Lever",
    "GroundTruth",
    # dossier seed
    "Stakeholder",
    "TimelineEvent",
    "DossierSeed",
    "DossierSeedPublic",
    # simulation
    "SimQuarter",
    "SimulationState",
    # case
    "CompanyProfile",
    "CaseWorld",
    "CasePublic",
    "CaseSummary",
    "CaseRecord",
    # skills
    "SkillNode",
    "SkillEdge",
    "MasteryRecord",
    # LLM artifacts
    "StakeholderMessage",
    "Dossier",
    "SelfCheck",
    "TextbookChapter",
    "RubricDimension",
    "RubricGrade",
    "MetricResult",
    "QuerySignatureMatch",
    "DeterministicGrade",
    "AdversaryTurn",
    "AdversaryTurnRecord",
    # submissions and grading
    "SqlEvidence",
    "PythonEvidence",
    "Submission",
    "SubmissionRecord",
    "SkillUpdate",
    "RevealedTruth",
    "GradeReport",
    # LLM settings and usage
    "LLMSettings",
    "LLMSettingsPublic",
    "LLMUsageRecord",
    "ModelUsage",
    "LLMUsageSummary",
    "mask_api_key",
    # API request/response bodies
    "HealthResponse",
    "ArchetypeInfo",
    "GenerateCaseRequest",
    "DossierRequest",
    "TextbookRequest",
    "SqlQueryRequest",
    "SqlQueryResult",
    "PythonRunRequest",
    "PythonRunResult",
    "AdversaryRequest",
    "AdvanceRequest",
    "SkillGraphNode",
    "SkillGraphEdge",
    "SkillGraphResponse",
    "OkResponse",
    "LLMTestResult",
    "PromptTemplate",
]

Pillar = Literal["accounting_finance", "operations_strategy", "system_design", "sql", "python"]
Difficulty = Literal["foundation", "practitioner", "principal"]
Distribution = Literal["lognormal", "normal", "beta", "triangular", "empirical"]
LLMProvider = Literal["openrouter", "openai", "anthropic", "local"]


class _World(BaseModel):
    """Base for world/contract models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class _Api(BaseModel):
    """Base for API bodies: unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- research tier


class Benchmark(_World):
    """A source-cited percentile distribution for one metric."""

    key: str
    industry: str = "any"
    metric: str
    unit: str
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    distribution: Distribution = "empirical"
    source: str
    source_url: str | None = None
    as_of: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def _monotone(self) -> Benchmark:
        """Percentiles must be non-decreasing."""
        qs = [self.p10, self.p25, self.p50, self.p75, self.p90]
        if any(b < a for a, b in zip(qs, qs[1:], strict=False)):
            raise ValueError(f"benchmark {self.key!r}: percentiles must be non-decreasing, got {qs}")
        return self

    def percentiles(self) -> list[float]:
        """Return ``[p10, p25, p50, p75, p90]``."""
        return [self.p10, self.p25, self.p50, self.p75, self.p90]


class IndustryProfile(_World):
    """Industry-level context: business models, benchmarks, line items, schema template."""

    code: str
    name: str
    naics: str | None = None
    business_models: list[str] = Field(default_factory=list)
    revenue_range_usd: tuple[int, int] = (1_000_000, 100_000_000)
    benchmarks: dict[str, Benchmark] = Field(default_factory=dict)
    line_items: list[str] = Field(default_factory=list)
    schema_template: str = ""
    narrative_hooks: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- finance


class Account(_World):
    """Chart-of-accounts entry."""

    code: str
    name: str
    type: Literal["asset", "liability", "equity", "revenue", "expense", "contra_asset", "contra_revenue"]
    normal_balance: Literal["debit", "credit"]
    statement: Literal["BS", "IS"]
    section: str
    parent: str | None = None


class JournalLine(_World):
    """One side of a journal entry. Exactly one of debit/credit is normally non-zero."""

    account: str
    debit_cents: int = Field(default=0, ge=0)
    credit_cents: int = Field(default=0, ge=0)
    dimension: dict[str, str] = Field(default_factory=dict)


class JournalEntry(_World):
    """A balanced double-entry journal entry."""

    id: str
    date: dt.date
    period: str
    memo: str = ""
    source: str = "adjustment"
    lines: list[JournalLine]

    @model_validator(mode="after")
    def _balanced(self) -> JournalEntry:
        """Enforce sum(debits) == sum(credits)."""
        debits = sum(line.debit_cents for line in self.lines)
        credits = sum(line.credit_cents for line in self.lines)
        if debits != credits:
            raise ValueError(f"journal entry {self.id}: debits {debits} != credits {credits}")
        return self


class StatementLine(_World):
    """A caption on a financial statement."""

    caption: str
    account_codes: list[str] = Field(default_factory=list)
    cents: int
    level: int = 0


class IncomeStatement(_World):
    """Income statement for one period."""

    period: str
    lines: list[StatementLine] = Field(default_factory=list)
    revenue_cents: int
    gross_profit_cents: int
    operating_income_cents: int
    net_income_cents: int


class BalanceSheet(_World):
    """Balance sheet at the end of one period."""

    period: str
    lines: list[StatementLine] = Field(default_factory=list)
    total_assets_cents: int
    total_liabilities_cents: int
    total_equity_cents: int


class CashFlowStatement(_World):
    """Cash flow statement (indirect method) for one period."""

    period: str
    operating_cents: int
    investing_cents: int
    financing_cents: int
    net_change_cents: int
    beginning_cash_cents: int
    ending_cash_cents: int
    lines: list[StatementLine] = Field(default_factory=list)


class FinancialStatements(_World):
    """All statements across all periods plus derived ratios."""

    periods: list[str] = Field(default_factory=list)
    income: list[IncomeStatement] = Field(default_factory=list)
    balance: list[BalanceSheet] = Field(default_factory=list)
    cash_flow: list[CashFlowStatement] = Field(default_factory=list)
    ratios: dict[str, dict[str, float]] = Field(default_factory=dict)


class InvariantCheck(_World):
    """Result of one accounting invariant check."""

    name: str
    passed: bool
    detail: str = ""


class FinanceState(_World):
    """Complete ledger state of a generated company."""

    chart_of_accounts: list[Account] = Field(default_factory=list)
    journal: list[JournalEntry] = Field(default_factory=list)
    trial_balance: dict[str, dict[str, int]] = Field(default_factory=dict)
    statements: FinancialStatements = Field(default_factory=FinancialStatements)
    invariants: list[InvariantCheck] = Field(default_factory=list)


# --------------------------------------------------------------------------- operations


class Station(_World):
    """A process step for theory-of-constraints analysis."""

    name: str
    capacity_per_hour: float
    utilization: float = 0.0
    wip: float = 0.0
    cycle_time: float = 0.0


class Cohort(_World):
    """An acquisition cohort with retention and revenue curves (index = periods since acquisition)."""

    acquisition_period: str
    size: int
    retention: list[float] = Field(default_factory=list)
    revenue_cents: list[int] = Field(default_factory=list)


class OperationsState(_World):
    """Operational state: stations, queueing, unit economics, cohorts, supply chain, pricing."""

    stations: list[Station] = Field(default_factory=list)
    queueing: dict[str, float] = Field(default_factory=dict)
    unit_economics: dict[str, float] = Field(default_factory=dict)
    cohorts: list[Cohort] = Field(default_factory=list)
    supply_chain: dict[str, Any] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)
    kpis_by_period: dict[str, dict[str, float]] = Field(default_factory=dict)


# --------------------------------------------------------------------------- database


class ColumnSpec(_World):
    """A column in a generated table."""

    name: str
    type: str
    nullable: bool = True
    description: str = ""


class TableSpec(_World):
    """A generated relational table and the dirt applied to it."""

    name: str
    ddl: str
    columns: list[ColumnSpec] = Field(default_factory=list)
    row_count: int = 0
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[tuple[str, str, str]] = Field(default_factory=list)
    dirt: list[str] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


class DatabaseSpec(_World):
    """The case's DuckDB database."""

    engine: Literal["duckdb"] = "duckdb"
    path: str
    tables: list[TableSpec] = Field(default_factory=list)
    erd_mermaid: str = ""
    total_rows: int = 0


# --------------------------------------------------------------------------- ground truth


class ReferenceQuery(_World):
    """A query that exposes an anomaly, with its expected result signature."""

    name: str
    sql: str
    expected: dict[str, Any] = Field(default_factory=dict)
    tolerance: float = 0.02


class Anomaly(_World):
    """An injected anomaly (root cause component)."""

    id: str
    kind: str
    pillar: Pillar
    title: str
    mechanism: str
    periods: list[str] = Field(default_factory=list)
    affected_accounts: list[str] = Field(default_factory=list)
    affected_tables: list[str] = Field(default_factory=list)
    magnitude: dict[str, float] = Field(default_factory=dict)
    evidence_trail: list[str] = Field(default_factory=list)
    reference_sql: list[ReferenceQuery] = Field(default_factory=list)
    severity: Literal["low", "medium", "high", "critical"] = "medium"


class ExpectedMetric(_World):
    """A metric the learner is expected to compute, with tolerance."""

    key: str
    value: float
    unit: str
    tolerance_abs: float | None = None
    tolerance_rel: float | None = None
    derivation: str = ""


class ReferenceSolution(_World):
    """The model answer, revealed after grading."""

    sql: list[str] = Field(default_factory=list)
    python: list[str] = Field(default_factory=list)
    memo_bullets: list[str] = Field(default_factory=list)
    design_mermaid: str | None = None


class Lever(_World):
    """A decision lever in the quarter simulation."""

    key: str
    label: str
    min: float
    max: float
    default: float
    unit: str = ""
    effect: str = ""


class GroundTruth(_World):
    """The answer key. Never returned to the client before grading."""

    root_cause: str
    anomalies: list[Anomaly] = Field(default_factory=list)
    red_herrings: list[str] = Field(default_factory=list)
    expected_metrics: list[ExpectedMetric] = Field(default_factory=list)
    reference_solution: ReferenceSolution = Field(default_factory=ReferenceSolution)
    decision_levers: list[Lever] = Field(default_factory=list)


# --------------------------------------------------------------------------- dossier seed


class Stakeholder(_World):
    """A character in the case."""

    name: str
    role: str
    agenda: str = ""
    disposition: str = ""
    knows: list[str] = Field(default_factory=list)
    hides: list[str] = Field(default_factory=list)


class TimelineEvent(_World):
    """A dated event in the case narrative."""

    period: str
    headline: str
    detail: str = ""
    is_red_herring: bool = False


class DossierSeed(_World):
    """Facts handed to the dossier writer, with explicit reveal/conceal lists."""

    reveal: list[str] = Field(default_factory=list)
    conceal: list[str] = Field(default_factory=list)
    stakeholders: list[Stakeholder] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    architecture_mermaid: str | None = None


class DossierSeedPublic(_World):
    """``DossierSeed`` without ``conceal`` (what the client may see)."""

    reveal: list[str] = Field(default_factory=list)
    stakeholders: list[Stakeholder] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    architecture_mermaid: str | None = None


# --------------------------------------------------------------------------- simulation


class SimQuarter(_World):
    """One simulated quarter: decisions in, KPIs out."""

    period: str
    decisions: dict[str, float] = Field(default_factory=dict)
    kpis: dict[str, float] = Field(default_factory=dict)
    narrative: str | None = None


class SimulationState(_World):
    """State of the deterministic quarter simulator."""

    current_period: str
    history: list[SimQuarter] = Field(default_factory=list)
    model: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- case


class CompanyProfile(_World):
    """The fictional company."""

    name: str
    ticker_like: str
    industry_code: str
    business_model: str
    hq: str
    founded: int
    fiscal_year_end: str = "12-31"
    headcount: int
    stage: str
    revenue_ttm_cents: int
    description: str = ""


class _CaseCore(_World):
    """Fields shared by ``CaseWorld`` and ``CasePublic``."""

    case_id: str
    seed: int
    archetype: str
    version: str
    pillars: list[Pillar] = Field(default_factory=list)
    skill_nodes: list[str] = Field(default_factory=list)
    difficulty: Difficulty = "practitioner"
    company: CompanyProfile
    industry: IndustryProfile
    periods: list[str] = Field(default_factory=list)
    finance: FinanceState = Field(default_factory=FinanceState)
    operations: OperationsState = Field(default_factory=OperationsState)
    database: DatabaseSpec
    simulation: SimulationState
    generated_at: str = ""


class CaseWorld(_CaseCore):
    """The full generated world, ground truth included."""

    ground_truth: GroundTruth
    dossier_seed: DossierSeed

    def public_view(self) -> dict[str, Any]:
        """JSON-able dict with ``ground_truth`` and ``dossier_seed.conceal`` removed."""
        data = self.model_dump(mode="json", exclude={"ground_truth": True, "dossier_seed": {"conceal"}})
        return data

    def to_public(self) -> CasePublic:
        """Typed public view."""
        return CasePublic.model_validate(self.public_view())


class CasePublic(_CaseCore):
    """The public view of a case: no ground truth, no concealed facts."""

    dossier_seed: DossierSeedPublic


class CaseSummary(_Api):
    """List-row view of a case."""

    case_id: str
    archetype: str
    company_name: str
    industry: str
    difficulty: Difficulty
    pillars: list[Pillar] = Field(default_factory=list)
    created_at: str
    status: str = "generated"


class CaseRecord(_Api):
    """A row of the ``cases`` table."""

    case_id: str
    seed: int
    archetype: str
    difficulty: Difficulty
    company_name: str
    industry: str
    pillars: list[Pillar] = Field(default_factory=list)
    skill_nodes: list[str] = Field(default_factory=list)
    world_path: str
    db_path: str
    status: str = "generated"
    created_at: str

    def summary(self) -> CaseSummary:
        """Project to a ``CaseSummary``."""
        return CaseSummary(
            case_id=self.case_id,
            archetype=self.archetype,
            company_name=self.company_name,
            industry=self.industry,
            difficulty=self.difficulty,
            pillars=list(self.pillars),
            created_at=self.created_at,
            status=self.status,
        )


# --------------------------------------------------------------------------- skills


class SkillNode(_World):
    """A node of the skill DAG with Bayesian Knowledge Tracing parameters."""

    id: str
    pillar: Pillar
    title: str
    description: str = ""
    tier: str = "core"
    prereqs: list[str] = Field(default_factory=list)
    mental_models: list[str] = Field(default_factory=list)
    archetypes: list[str] = Field(default_factory=list)
    p_init: float = Field(default=0.2, ge=0, le=1)
    p_transit: float = Field(default=0.15, ge=0, le=1)
    p_slip: float = Field(default=0.1, ge=0, le=1)
    p_guess: float = Field(default=0.2, ge=0, le=1)

    @classmethod
    def from_dag_record(cls, record: dict[str, Any]) -> SkillNode:
        """Build from a ``skill_dag.json`` node: ``summary`` maps to ``description`` and the
        nested ``bkt`` dict is flattened into ``p_*`` fields; unknown keys are ignored."""
        data = dict(record)
        bkt = data.pop("bkt", None) or {}
        if "summary" in data and "description" not in data:
            data["description"] = data.pop("summary")
        data.update({k: v for k, v in bkt.items() if k in ("p_init", "p_transit", "p_slip", "p_guess")})
        return cls.model_validate({k: v for k, v in data.items() if k in cls.model_fields})


class SkillEdge(_World):
    """Prerequisite edge: ``src`` must be mastered before ``dst``."""

    src: str
    dst: str


class MasteryRecord(_Api):
    """A row of the ``mastery`` table."""

    node_id: str
    p_known: float = Field(ge=0, le=1)
    attempts: int = 0
    correct: int = 0
    last_seen: str | None = None


# --------------------------------------------------------------------------- LLM artifacts


class StakeholderMessage(_Api):
    """A message in the dossier from a stakeholder."""

    from_name: str
    role: str
    channel: str = "email"
    body: str


class Dossier(_Api):
    """The case briefing written by the LLM from the dossier seed."""

    title: str
    situation: str
    background_markdown: str = ""
    stakeholder_messages: list[StakeholderMessage] = Field(default_factory=list)
    the_ask: str
    deliverables: list[str] = Field(default_factory=list)
    data_room_index: list[str] = Field(default_factory=list)


class SelfCheck(_Api):
    """A self-check question in a textbook chapter."""

    question: str
    answer: str


class TextbookChapter(_Api):
    """A synthesized textbook chapter for one skill node."""

    skill_node: str
    title: str
    markdown: str
    self_checks: list[SelfCheck] = Field(default_factory=list)


class RubricDimension(_Api):
    """One rubric dimension score with evidence."""

    score: float = Field(ge=0, le=10)
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class RubricGrade(_Api):
    """LLM rubric grade across dimensions."""

    dimensions: dict[str, RubricDimension] = Field(default_factory=dict)
    overall: float = Field(default=0.0, ge=0, le=10)
    misdiagnosis: bool = False
    strongest_point: str = ""
    weakest_point: str = ""
    next_drill_nodes: list[str] = Field(default_factory=list)


class MetricResult(_Api):
    """Deterministic check of one submitted metric."""

    key: str
    submitted: float | None = None
    expected: float
    unit: str = ""
    within_tolerance: bool = False
    tolerance_abs: float | None = None
    tolerance_rel: float | None = None


class QuerySignatureMatch(_Api):
    """Whether submitted SQL evidence reproduced a reference query signature."""

    name: str
    matched: bool
    detail: str = ""


class DeterministicGrade(_Api):
    """Deterministic grading result."""

    metric_results: list[MetricResult] = Field(default_factory=list)
    query_signature_matches: list[QuerySignatureMatch] = Field(default_factory=list)
    accuracy: float = Field(default=0.0, ge=0, le=1)


class AdversaryTurn(_Api):
    """One Socratic adversary message."""

    persona: str
    message: str
    challenge_type: str = "probe"
    cites: list[str] = Field(default_factory=list)
    concedes: bool = False
    turn_index: int = 0


class AdversaryTurnRecord(_Api):
    """A row of the ``adversary_turns`` table (user or adversary)."""

    id: int | None = None
    submission_id: str
    turn_index: int
    role: Literal["user", "adversary", "system"]
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


# --------------------------------------------------------------------------- submissions


class SqlEvidence(_Api):
    """SQL evidence attached to a submission."""

    name: str
    sql: str
    result_preview: Any = None


class PythonEvidence(_Api):
    """Python evidence attached to a submission."""

    name: str
    code: str
    stdout_tail: str = ""
    result_json: Any = None


class Submission(_Api):
    """A learner's deliverable."""

    case_id: str
    memo_markdown: str = ""
    metrics: dict[str, float] = Field(default_factory=dict)
    sql_evidence: list[SqlEvidence] = Field(default_factory=list)
    python_evidence: list[PythonEvidence] = Field(default_factory=list)
    design_mermaid: str | None = None
    financial_model: dict[str, Any] | None = None


class SubmissionRecord(_Api):
    """A row of the ``submissions`` table."""

    submission_id: str
    case_id: str
    submitted: Submission
    grade: dict[str, Any] | None = None
    created_at: str = ""


class SkillUpdate(_Api):
    """Mastery change of one skill node caused by a grade."""

    node: str
    before: float
    after: float


class RevealedTruth(_Api):
    """Ground truth unlocked after grading."""

    root_cause: str
    anomalies: list[Anomaly] = Field(default_factory=list)
    reference_solution: ReferenceSolution = Field(default_factory=ReferenceSolution)


class GradeReport(_Api):
    """Combined deterministic + rubric grade."""

    submission_id: str
    deterministic: DeterministicGrade
    rubric: RubricGrade
    skill_updates: list[SkillUpdate] = Field(default_factory=list)
    revealed: RevealedTruth
    adversary_opening: str = ""


# --------------------------------------------------------------------------- LLM settings / usage


def mask_api_key(key: str) -> str:
    """Mask an API key, keeping only the last 4 characters."""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


class LLMSettings(_Api):
    """LLM provider configuration (stored only in the app DB)."""

    provider: LLMProvider = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    fast_model: str = "anthropic/claude-haiku-4.5"
    deep_model: str = "anthropic/claude-sonnet-4.5"
    temperature_fast: float = 0.7
    temperature_deep: float = 0.2
    max_tokens: int = 4096

    @property
    def configured(self) -> bool:
        """True when a key is present (or the provider is local)."""
        return bool(self.api_key) or self.provider == "local"


class LLMSettingsPublic(_Api):
    """``LLMSettings`` as returned by GET: API key masked to the last 4 characters."""

    provider: LLMProvider
    base_url: str
    api_key: str
    api_key_set: bool
    fast_model: str
    deep_model: str
    temperature_fast: float
    temperature_deep: float
    max_tokens: int

    @classmethod
    def from_settings(cls, settings: LLMSettings) -> LLMSettingsPublic:
        """Build the masked view."""
        data = settings.model_dump()
        data["api_key"] = mask_api_key(settings.api_key)
        data["api_key_set"] = bool(settings.api_key)
        return cls(**data)


class LLMUsageRecord(_Api):
    """A row of the ``llm_usage`` table."""

    id: int | None = None
    ts: str = ""
    model: str
    purpose: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd_est: float = 0.0


class ModelUsage(_Api):
    """Aggregated usage for one model."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    avg_latency_ms: float = 0.0
    cost_usd_est: float = 0.0


class LLMUsageSummary(_Api):
    """Usage aggregated across models."""

    by_model: dict[str, ModelUsage] = Field(default_factory=dict)
    total_tokens: int = 0
    total_cost_usd_est: float = 0.0


# --------------------------------------------------------------------------- API bodies


class HealthResponse(_Api):
    """GET /health."""

    status: str = "ok"
    version: str
    llm_configured: bool = False


class ArchetypeInfo(_Api):
    """GET /archetypes row."""

    key: str
    title: str
    pillars: list[Pillar] = Field(default_factory=list)
    skill_nodes: list[str] = Field(default_factory=list)
    difficulty_range: list[Difficulty] = Field(default_factory=list)
    summary: str = ""


class GenerateCaseRequest(_Api):
    """POST /cases/generate."""

    archetype: str | None = None
    seed: int | None = None
    difficulty: Difficulty | None = None
    target_skills: list[str] = Field(default_factory=list)


class DossierRequest(_Api):
    """POST /cases/{id}/dossier."""

    force: bool = False


class TextbookRequest(_Api):
    """POST /cases/{id}/textbook."""

    skill_node: str


class SqlQueryRequest(_Api):
    """POST /sandbox/sql."""

    case_id: str
    sql: str
    limit: int = Field(default=500, ge=1)


class SqlQueryResult(_Api):
    """Response of POST /sandbox/sql."""

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    elapsed_ms: float = 0.0
    truncated: bool = False
    error: str | None = None


class PythonRunRequest(_Api):
    """POST /sandbox/python."""

    case_id: str
    code: str
    timeout_s: int = Field(default=20, ge=1)


class PythonRunResult(_Api):
    """Response of POST /sandbox/python."""

    stdout: str = ""
    stderr: str = ""
    result_json: Any = None
    figures: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None


class AdversaryRequest(_Api):
    """POST /cases/{id}/adversary."""

    submission_id: str
    message: str


class AdvanceRequest(_Api):
    """POST /cases/{id}/advance."""

    submission_id: str
    decisions: dict[str, float] = Field(default_factory=dict)


class SkillGraphNode(_Api):
    """GET /skills/graph node."""

    id: str
    pillar: Pillar
    title: str
    mastery: float = 0.0
    prereqs: list[str] = Field(default_factory=list)
    attempts: int = 0


class SkillGraphEdge(_Api):
    """GET /skills/graph edge."""

    src: str
    dst: str


class SkillGraphResponse(_Api):
    """GET /skills/graph."""

    nodes: list[SkillGraphNode] = Field(default_factory=list)
    edges: list[SkillGraphEdge] = Field(default_factory=list)


class OkResponse(_Api):
    """Generic ``{ok}`` response."""

    ok: bool = True


class LLMTestResult(_Api):
    """POST /settings/llm/test."""

    ok: bool
    model: str = ""
    latency_ms: float = 0.0
    error: str | None = None


class PromptTemplate(_Api):
    """GET /settings/prompts row."""

    name: str
    template: str


for _model in list(globals().values()):
    if isinstance(_model, type) and issubclass(_model, BaseModel) and _model is not BaseModel:
        _model.model_rebuild()
del _model
