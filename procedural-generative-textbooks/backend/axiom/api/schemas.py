"""API-layer response models that extend the shared models with ARCHITECTURE.md section 12.

The shared models in :mod:`axiom.models` are the module contract; the frontend asked for a few
extra fields (section 12) that only the API layer can fill (case status lives in the app DB,
asks are a projection of the ground truth, ...). Each view subclasses the shared model so it
still *is* the contract type, plus the additions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from axiom.models import (
    AdversaryTurn,
    Anomaly,
    CasePublic,
    GradeReport,
    Lever,
    LLMSettings,
    LLMSettingsPublic,
    Pillar,
    ReferenceSolution,
    SimQuarter,
    SkillGraphEdge,
    SkillGraphNode,
    SqlQueryResult,
    _Api,
)

__all__ = [
    "CaseStatus",
    "Ask",
    "CasePublicView",
    "SqlQueryResultView",
    "SkillGraphNodeView",
    "SkillGraphEdgeView",
    "SkillGraphView",
    "LLMSettingsView",
    "RevealedAnomaly",
    "RevealedTruthPublic",
    "GradeReportPublic",
    "ThreadMessage",
    "SessionState",
    "public_grade",
]

CaseStatus = Literal["generated", "briefed", "submitted", "graded"]


class Ask(_Api):
    """One metric the learner must fill in ``Submission.metrics`` (value withheld)."""

    key: str
    label: str
    unit: str = ""
    hint: str = ""


class CasePublicView(CasePublic):
    """``CasePublic`` + section 12.1/12.2 (``levers``, ``status``) + section 11 ``asks``."""

    asks: list[Ask] = Field(default_factory=list)
    levers: list[Lever] = Field(default_factory=list)
    status: CaseStatus = "generated"


class SqlQueryResultView(SqlQueryResult):
    """``SqlQueryResult`` + section 12.4 ``column_types`` (DuckDB type names)."""

    column_types: list[str] = Field(default_factory=list)


class SkillGraphNodeView(SkillGraphNode):
    """Skill graph node + section 12.5 ``summary`` and ``tier``."""

    summary: str = ""
    tier: str = "core"


class SkillGraphEdgeView(SkillGraphEdge):
    """Edge carrying both the backend (``src``/``dst``) and frontend (``source``/``target``) names."""

    source: str
    target: str


class SkillGraphView(_Api):
    """``GET /skills/graph`` with the section 12.5 node additions and per-pillar summary."""

    nodes: list[SkillGraphNodeView] = Field(default_factory=list)
    edges: list[SkillGraphEdgeView] = Field(default_factory=list)
    pillars: dict[str, dict[str, Any]] = Field(default_factory=dict)


class LLMSettingsView(LLMSettingsPublic):
    """Masked LLM settings: ``api_key`` and ``api_key_masked`` both hold the masked key."""

    api_key_masked: str = ""
    configured: bool = False

    @classmethod
    def build(cls, settings: LLMSettings) -> LLMSettingsView:
        """Masked view of ``settings`` (the raw key never leaves this function)."""
        base = LLMSettingsPublic.from_settings(settings)
        return cls(**base.model_dump(), api_key_masked=base.api_key, configured=settings.configured)


class RevealedAnomaly(_Api):
    """Public fields of an anomaly (section 12.6): no reference SQL, magnitudes or tables."""

    id: str
    kind: str
    pillar: Pillar
    title: str
    mechanism: str
    periods: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    evidence_trail: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, anomaly: Anomaly) -> RevealedAnomaly:
        """Project an :class:`~axiom.models.Anomaly`."""
        return cls.model_validate(anomaly.model_dump(mode="json"))


class RevealedTruthPublic(_Api):
    """Typed ``GradeReport.revealed`` (section 12.6)."""

    root_cause: str
    anomalies: list[RevealedAnomaly] = Field(default_factory=list)
    reference_solution: ReferenceSolution = Field(default_factory=ReferenceSolution)


class GradeReportPublic(GradeReport):
    """``GradeReport`` whose ``revealed.anomalies`` carry only the public anomaly fields."""

    revealed: RevealedTruthPublic  # type: ignore[assignment]


def public_grade(report: GradeReport | dict[str, Any]) -> GradeReportPublic:
    """Project a stored/full grade report to its API shape."""
    full = report if isinstance(report, GradeReport) else GradeReport.model_validate(report)
    data = full.model_dump(mode="json")
    data["revealed"] = RevealedTruthPublic(
        root_cause=full.revealed.root_cause,
        anomalies=[RevealedAnomaly.of(a) for a in full.revealed.anomalies],
        reference_solution=full.revealed.reference_solution,
    ).model_dump(mode="json")
    return GradeReportPublic.model_validate(data)


class ThreadMessage(_Api):
    """One message of the adversary conversation (user and adversary turns in order)."""

    role: Literal["user", "adversary", "system"]
    message: str
    turn_index: int
    persona: str | None = None
    created_at: str = ""


class SessionState(_Api):
    """``GET /cases/{id}/session`` (section 12.3): enough to restore the workbench on reload."""

    submission_id: str | None = None
    grade: GradeReportPublic | None = None
    adversary_turns: list[AdversaryTurn] = Field(default_factory=list)
    sim_history: list[SimQuarter] = Field(default_factory=list)
    thread: list[ThreadMessage] = Field(default_factory=list)
    status: CaseStatus = "generated"
