"""Archetype base class and build orchestration.

An archetype turns ``(seed, difficulty, industry)`` into a :class:`~axiom.models.CaseWorld`.
:meth:`Archetype.build` is a template method; subclasses implement the hooks, in this order:

====================  =========================================================================
``sample_params``     draw every parameter (``rng = ctx.rng.spawn("params")``); must set ``industry``
``build_company``     :class:`CompanyProfile` (default implementation provided)
``build_baseline``    healthy plan: master data, demand, policies (no anomaly yet)
``inject_anomalies``  modify the plan, return :class:`Anomaly` stubs (reference SQL filled later)
``post_ledger``       post the journal from the final plan (may be done in the two hooks above)
``build_tables``      register canonical (clean) DataFrames + dirt rules on the ``TableBuilder``
``subledger_ties``    :class:`SubledgerTie` list (designed breaks declared with their amount)
``build_operations``  :class:`OperationsState`
``build_ground_truth``expected metrics FROM the generated data, reference SQL run on the DuckDB
``build_dossier_seed``reveal / conceal / stakeholders / timeline / artifacts
``levers`` +          decision levers and the reduced-form quarter-simulation model
``simulation_model``
====================  =========================================================================

The base then: builds statements, runs every invariant (raising ``WorldInvariantError``), adds
the GL tables from the ledger, applies dirt, writes ``company.duckdb`` (only after invariants pass),
opens it read-only for the ground-truth hook, validates the ground truth and the simulation model,
and assembles the ``CaseWorld``. Randomness: one child stream per hook (``ctx.rng.spawn(<hook>)``),
so adding draws in one hook never shifts another.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import numpy as np
import pandas as pd

from axiom import __version__
from axiom.config import PACKAGE_DIR, Settings
from axiom.engine.datagen import TableBuilder
from axiom.engine.finance import FiscalCalendar, Ledger, SubledgerTie, assert_invariants, build_statements
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.rng import SeededRNG
from axiom.models import (
    Anomaly,
    ArchetypeInfo,
    CaseWorld,
    CompanyProfile,
    DatabaseSpec,
    Difficulty,
    DossierSeed,
    ExpectedMetric,
    FinanceState,
    FinancialStatements,
    GroundTruth,
    IndustryProfile,
    InvariantCheck,
    Lever,
    OperationsState,
    Pillar,
    ReferenceQuery,
    SimulationState,
)

__all__ = [
    "DIFFICULTIES",
    "BuildContext",
    "WorldState",
    "Archetype",
    "GroundTruthError",
    "magnitude",
    "difficulty_pick",
    "sql_signature",
    "run_reference_query",
    "validate_simulation_model",
    "metric",
    "archetype_skill_nodes",
]

logger = logging.getLogger("axiom.archetypes")

DIFFICULTIES: tuple[Difficulty, ...] = ("foundation", "practitioner", "principal")
SIM_MODEL_KEYS: tuple[str, ...] = ("kpis", "lever_effects", "anomaly_drag", "resolved_if")


class GroundTruthError(RuntimeError):
    """The ground truth an archetype produced is incomplete (a generator bug)."""


@lru_cache(maxsize=1)
def _skill_dag() -> dict[str, Any]:
    path = PACKAGE_DIR / "skill_dag.json"
    return json.loads(path.read_text(encoding="utf-8"))


def archetype_skill_nodes(key: str) -> list[str]:
    """Skill nodes of an archetype from ``skill_dag.json`` ``archetype_nodes`` (single source of truth)."""
    return list(_skill_dag().get("archetype_nodes", {}).get(key, []))


# --------------------------------------------------------------------------- context / state


@dataclass
class BuildContext:
    """Everything a build needs. ``rng`` is the world's root stream (``SeededRNG.for_world``)."""

    rng: SeededRNG
    pool: BenchmarkPool
    settings: Settings
    difficulty: Difficulty
    case_id: str
    out_dir: Path
    seed: int = 0
    industry: str | None = None
    version: str = __version__

    @property
    def db_path(self) -> Path:
        """Absolute path of ``company.duckdb``."""
        return Path(self.out_dir) / "company.duckdb"

    @property
    def rel_db_path(self) -> str:
        """``DatabaseSpec.path``: the logical location ``cases/<case_id>/company.duckdb`` relative to
        ``data/`` (ARCHITECTURE section 3). It never embeds the actual output directory, so
        ``world.json`` is byte-identical wherever it is written; the file itself always sits next to
        ``world.json`` (see ``procedural_world.resolve_db_path``)."""
        return f"cases/{self.case_id}/company.duckdb"


@dataclass
class WorldState:
    """Mutable scratch space shared by the hooks of one build."""

    params: dict[str, Any]
    calendar: FiscalCalendar
    ledger: Ledger
    industry: IndustryProfile | None = None
    company: CompanyProfile | None = None
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)  # canonical (clean) tables
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)  # as written (after dirt)
    anomalies: list[Anomaly] = field(default_factory=list)
    statements: FinancialStatements | None = None
    invariants: list[InvariantCheck] = field(default_factory=list)
    database: DatabaseSpec | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def periods(self) -> list[str]:
        """Reporting periods ``P1..P8``."""
        return self.calendar.periods

    def p(self, i: int) -> str:
        """Label of ``P<i>``."""
        return self.calendar.p(i)


# --------------------------------------------------------------------------- helpers


def magnitude(rng: SeededRNG, difficulty: str, lo: float, hi: float) -> float:
    """``D[p] * U(lo, hi)``: foundation draws from the upper quartile of ``[lo, hi]``, practitioner
    from the middle quarter around the median, principal from the lower quartile (common section 9)."""
    span = hi - lo
    band = {"foundation": (0.75, 1.0), "practitioner": (0.375, 0.625), "principal": (0.0, 0.25)}[difficulty]
    return float(rng.uniform(lo + band[0] * span, lo + band[1] * span))


def difficulty_pick(difficulty: str, foundation: Any, practitioner: Any, principal: Any) -> Any:
    """Return the value for ``difficulty``."""
    return {"foundation": foundation, "practitioner": practitioner, "principal": principal}[difficulty]


_SIG_PATTERNS: tuple[tuple[str, str], ...] = (
    ("with", r"^\s*with\b"),
    ("left join", r"\bleft\s+join\b"),
    ("join", r"\bjoin\b"),
    ("group by", r"\bgroup\s+by\b"),
    ("having", r"\bhaving\b"),
    ("over(", r"\bover\s*\("),
    ("partition by", r"\bpartition\s+by\b"),
    ("row_number(", r"\brow_number\s*\("),
    ("lag(", r"\blag\s*\("),
    ("qualify", r"\bqualify\b"),
    ("date_trunc(", r"\bdate_trunc\s*\("),
    ("date_diff(", r"\bdate_diff\s*\("),
    ("case when", r"\bcase\s+when\b"),
    ("coalesce(", r"\bcoalesce\s*\("),
    ("sum(", r"\bsum\s*\("),
    ("count(", r"\bcount\s*\("),
    ("avg(", r"\bavg\s*\("),
    ("distinct", r"\bdistinct\b"),
    ("union", r"\bunion\b"),
    ("filter(", r"\bfilter\s*\("),
)


def sql_signature(sql: str, *, limit: int = 5) -> list[str]:
    """Key SQL constructs present in ``sql`` (lower-case, matching ``pedagogy.normalize_sql``
    output) - at most ``limit``, most distinctive first (windows/CTEs before plain aggregates)."""
    s = sql.lower()
    found = [name for name, pat in _SIG_PATTERNS if re.search(pat, s, flags=re.MULTILINE)]
    if "left join" in found and "join" in found:
        found.remove("join")
    priority = ["over(", "partition by", "row_number(", "qualify", "lag(", "with", "left join", "join", "having",
                "group by", "date_trunc(", "date_diff(", "case when", "coalesce(", "filter(", "distinct", "union",
                "sum(", "count(", "avg("]
    found.sort(key=lambda c: priority.index(c))
    return found[:limit]


def _json_cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return None if f != f or f in (float("inf"), float("-inf")) else round(f, 6)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (list, tuple)):
        return [_json_cell(x) for x in v]
    return str(v)


def run_reference_query(
    con: duckdb.DuckDBPyConnection,
    name: str,
    sql: str,
    *,
    signature: Sequence[str] | None = None,
    metric_keys: Sequence[str] = (),
    checks: Callable[[pd.DataFrame], Mapping[str, Any]] | None = None,
    tolerance: float = 0.02,
    max_rows: int = 25,
) -> ReferenceQuery:
    """Execute ``sql`` on the case DB and build a :class:`ReferenceQuery` whose ``expected`` holds
    ``columns``, ``row_count``, the first ``max_rows`` ``rows``, ``signature`` (key constructs a
    correct answer uses), ``metric_keys`` (expected metrics this query supports) and ``checks``
    (archetype-specific properties computed on the result, e.g. ``dso_monotone_from_P6``).
    Raises :class:`GroundTruthError` if the query fails or returns no rows."""
    try:
        df = con.execute(sql).fetchdf()
    except duckdb.Error as exc:
        raise GroundTruthError(f"reference query {name!r} failed: {exc}\n{sql}") from exc
    if df.empty:
        raise GroundTruthError(f"reference query {name!r} returned no rows")
    rows = [[_json_cell(v) for v in rec] for rec in df.itertuples(index=False, name=None)][:max_rows]
    expected: dict[str, Any] = {
        "columns": [str(c) for c in df.columns],
        "row_count": int(len(df)),
        "rows": rows,
        "signature": list(signature) if signature is not None else sql_signature(sql),
        "metric_keys": list(metric_keys),
    }
    if checks is not None:
        expected["checks"] = {k: _json_cell(v) for k, v in checks(df).items()}
    return ReferenceQuery(name=name, sql=sql.strip(), expected=expected, tolerance=tolerance)


def metric(key: str, value: float, unit: str, derivation: str, *, pillar: Pillar | None = None, tol_abs: float | None = None,
           tol_rel: float | None = None, label: str = "", hint: str = "") -> ExpectedMetric:
    """Shorthand for :class:`ExpectedMetric` (value rounded to 6 decimals for stable JSON)."""
    v = float(value)
    return ExpectedMetric(key=key, value=round(v, 6), unit=unit, tolerance_abs=tol_abs, tolerance_rel=tol_rel,
                          derivation=derivation, pillar=pillar, label=label or key.replace("_", " "), hint=hint)


def validate_simulation_model(model: Mapping[str, Any], levers: Sequence[Lever]) -> None:
    """Check the reduced-form model contract consumed by the quarter simulator::

        {"kpis": {kpi: float}, "lever_effects": [{"lever", "kpi", "elasticity" | "delta_per_unit", "lag_periods"}],
         "anomaly_drag": {kpi: float}, "resolved_if": {lever: float}}

    Each effect carries exactly one of ``elasticity`` / ``delta_per_unit`` (the other key is
    omitted rather than ``null``); levers and KPIs must exist.
    """
    missing = [k for k in SIM_MODEL_KEYS if k not in model]
    if missing:
        raise GroundTruthError(f"simulation model missing keys {missing}")
    kpis = model["kpis"]
    lever_keys = {lv.key for lv in levers}
    if not kpis or not all(isinstance(v, (int, float)) for v in kpis.values()):
        raise GroundTruthError("simulation kpis must be a non-empty {name: float} map")
    for eff in model["lever_effects"]:
        if eff.get("lever") not in lever_keys:
            raise GroundTruthError(f"lever effect on unknown lever {eff.get('lever')!r}")
        if eff.get("kpi") not in kpis:
            raise GroundTruthError(f"lever effect on unknown kpi {eff.get('kpi')!r}")
        has_e = eff.get("elasticity") is not None
        has_d = eff.get("delta_per_unit") is not None
        if has_e == has_d:
            raise GroundTruthError(f"lever effect {eff} must set exactly one of elasticity/delta_per_unit")
        if not isinstance(eff.get("lag_periods", 0), int):
            raise GroundTruthError("lag_periods must be int")
    for k in model["anomaly_drag"]:
        if k not in kpis:
            raise GroundTruthError(f"anomaly_drag on unknown kpi {k!r}")
    for k, v in model["resolved_if"].items():
        if k not in lever_keys or not isinstance(v, (int, float)):
            raise GroundTruthError(f"resolved_if must map known levers to float thresholds, got {k!r}: {v!r}")


def _round_floats(obj: Any, nd: int = 6) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, nd) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return round(float(obj), nd)
    return obj


# --------------------------------------------------------------------------- the ABC


class Archetype(ABC):
    """Base class of every procedural archetype (see module docstring for the hook order)."""

    key: ClassVar[str]
    title: ClassVar[str]
    pillars: ClassVar[tuple[Pillar, ...]]
    industries: ClassVar[tuple[str, ...]]
    schema_template: ClassVar[str]
    difficulty_range: ClassVar[tuple[Difficulty, ...]] = DIFFICULTIES
    summary: ClassVar[str] = ""
    root_cause_terms: ClassVar[tuple[str, ...]] = ()  # must appear in DossierSeed.conceal

    # ------------------------------------------------------------------ catalog

    @classmethod
    def skill_nodes(cls) -> list[str]:
        """Skill nodes from ``skill_dag.json`` ``archetype_nodes``."""
        return archetype_skill_nodes(cls.key)

    @classmethod
    def info(cls) -> ArchetypeInfo:
        """Catalog row for ``GET /archetypes``."""
        return ArchetypeInfo(key=cls.key, title=cls.title, pillars=list(cls.pillars), skill_nodes=cls.skill_nodes(),
                             difficulty_range=list(cls.difficulty_range), summary=cls.summary)

    # ------------------------------------------------------------------ hooks

    @abstractmethod
    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Draw all parameters. Must include ``"industry"`` (one of :attr:`industries` unless forced)."""

    def build_company(self, ctx: BuildContext, state: WorldState) -> CompanyProfile:
        """Generic company profile; ``revenue_ttm_cents`` is refreshed from the ledger after posting."""
        rng = ctx.rng.spawn("company")
        ind = state.params["industry"]
        name = rng.company_name(ind if ind in ("saas", "ecommerce", "manufacturing", "3pl", "retail") else None)
        rev = int(state.params.get("revenue_ttm_cents", 0))
        per_head = float(state.params.get("revenue_per_employee_usd", rng.uniform(250_000, 550_000)))
        return CompanyProfile(
            name=name, ticker_like=SeededRNG.ticker_like(name), industry_code=ind,
            business_model=str(state.params.get("business_model", ind)), hq=rng.city(),
            founded=state.calendar.anchor_year - int(rng.integers(6, 26)), fiscal_year_end="12-31",
            headcount=max(12, int(rev / 100 / per_head)), stage=str(state.params.get("stage", "growth")),
            revenue_ttm_cents=rev, description=str(state.params.get("description", "")),
        )

    @abstractmethod
    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Build the healthy plan (and optionally post its ledger entries)."""

    @abstractmethod
    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Modify the plan; return anomalies (``reference_sql`` may be empty until ground truth)."""

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Post the journal from the final plan (default: nothing - already posted)."""

    @abstractmethod
    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Register clean DataFrames and their dirt rules (GL tables are added by the base)."""

    @abstractmethod
    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """Sub-ledger ties checked as invariants (designed breaks declared with their amount)."""

    @abstractmethod
    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Operations state (stations, queueing, unit economics, supply chain, KPIs by period)."""

    @abstractmethod
    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Answer key: metrics computed from the data, reference SQL executed on ``con``."""

    @abstractmethod
    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal/conceal lists, stakeholders, timeline, artifacts (and architecture diagram)."""

    @abstractmethod
    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Decision levers of the quarter simulation (defaults = status quo)."""

    @abstractmethod
    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form transition model (see :func:`validate_simulation_model`)."""

    # ------------------------------------------------------------------ orchestration

    def new_state(self, ctx: BuildContext, params: dict[str, Any]) -> WorldState:
        """Create the :class:`WorldState` (calendar from the seed, empty canonical ledger)."""
        calendar = FiscalCalendar.from_seed(ctx.seed)
        return WorldState(params=params, calendar=calendar, ledger=Ledger())

    def build(self, ctx: BuildContext) -> CaseWorld:
        """Run every hook, check invariants, write the DB and assemble the :class:`CaseWorld`."""
        if ctx.difficulty not in self.difficulty_range:
            raise ValueError(f"{self.key} does not support difficulty {ctx.difficulty!r}")
        params = self.sample_params(ctx.rng.spawn("params"), ctx.pool, ctx.difficulty, ctx.industry)
        if "industry" not in params:
            raise ValueError(f"{self.key}.sample_params must set 'industry'")
        state = self.new_state(ctx, params)
        state.industry = ctx.pool.industry(params["industry"])
        state.company = self.build_company(ctx, state)
        self.build_baseline(ctx, state)
        state.anomalies = list(self.inject_anomalies(ctx, state))
        self.post_ledger(ctx, state)
        # TTM revenue from the books
        ttm = sum(-sum(state.ledger.activity(p)[c] for c in ("4000", "4010", "4020", "4100", "4110")) for p in state.periods[-4:])
        state.company = state.company.model_copy(update={"revenue_ttm_cents": int(ttm)})

        statements = build_statements(state.ledger, state.periods)
        state.statements = statements
        ties = self.subledger_ties(ctx, state)
        state.invariants = assert_invariants(state.ledger, statements, ties)

        template = ctx.pool.schema_template(self.schema_template) if self.schema_template in ctx.pool.schema_template_keys() else None
        builder = TableBuilder(ctx.rng.spawn("datagen"), template, difficulty=ctx.difficulty)
        self.build_tables(ctx, state, builder)
        builder.add_ledger_tables(state.ledger)
        state.tables = builder.materialize()
        state.database = builder.write(ctx.db_path, state.calendar, rel_path=ctx.rel_db_path)

        con = duckdb.connect(str(ctx.db_path), read_only=True)
        try:
            gt = self.build_ground_truth(ctx, state, con)
        finally:
            con.close()
        levers = self.levers(ctx, state)
        if not gt.decision_levers:
            gt = gt.model_copy(update={"decision_levers": levers})
        self._validate_ground_truth(gt)
        dossier = self.build_dossier_seed(ctx, state)
        conceal_text = " ".join(dossier.conceal).lower()
        missing_terms = [t for t in self.root_cause_terms if t.lower() not in conceal_text]
        if missing_terms:
            raise GroundTruthError(f"dossier conceal list misses root-cause terms {missing_terms}")
        operations = self.build_operations(ctx, state)
        model = _round_floats(self.simulation_model(ctx, state))
        validate_simulation_model(model, gt.decision_levers)

        finance = FinanceState(
            chart_of_accounts=list(state.ledger.chart),
            journal=list(state.ledger.entries),
            trial_balance={p: state.ledger.trial_balance(p) for p in state.periods},
            statements=statements,
            invariants=state.invariants,
        )
        return CaseWorld(
            case_id=ctx.case_id, seed=ctx.seed, archetype=self.key, version=ctx.version,
            pillars=list(self.pillars), skill_nodes=self.skill_nodes(), difficulty=ctx.difficulty,
            company=state.company, industry=state.industry, periods=list(state.periods),
            finance=finance, operations=operations, database=state.database,
            ground_truth=gt, dossier_seed=dossier,
            simulation=SimulationState(current_period=state.periods[-1], history=[], model=model),
            generated_at="",
        )

    @staticmethod
    def _validate_ground_truth(gt: GroundTruth) -> None:
        if len(gt.expected_metrics) < 3:
            raise GroundTruthError("ground truth needs at least 3 expected metrics")
        for m in gt.expected_metrics:
            if not m.derivation:
                raise GroundTruthError(f"metric {m.key} has no derivation")
            if m.value != m.value:
                raise GroundTruthError(f"metric {m.key} is NaN")
        if not gt.anomalies:
            raise GroundTruthError("ground truth has no anomalies")
        for a in gt.anomalies:
            for q in a.reference_sql:
                if not q.expected:
                    raise GroundTruthError(f"reference query {q.name} has no expected signature")
        keys = [m.key for m in gt.expected_metrics]
        if len(set(keys)) != len(keys):
            raise GroundTruthError("duplicate expected metric keys")
