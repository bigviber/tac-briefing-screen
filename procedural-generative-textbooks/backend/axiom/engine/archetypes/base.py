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

import datetime as dt
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
from axiom.engine.finance import (
    FiscalCalendar,
    Ledger,
    SubledgerTie,
    WorkingCapitalPolicy,
    WorldInvariantError,
    assert_invariants,
    build_statements,
    cents,
)
from axiom.engine.market_research import BenchmarkPool
from axiom.engine import benchmark_keys as _bk
from axiom.engine.rng import COMPANY_STEMS, SeededRNG, excluding_name_stems
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
    "cap_first",
    "company_profile",
    "revenue_per_employee_usd",
    "founded_floor",
    "MIN_HEADCOUNT",
    "BS_CAPTION_RULES",
    "bs_caption_checks",
    "post_deferred_revenue_trueups",
    "DEFERRAL_SOURCE",
    "PeriodNoise",
    "at_most",
    "at_least",
    "condition_holds",
    "default_simulation_bounds",
    "satisfying_decisions",
    "SEASONAL_PROFILES",
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


def at_most(value: float) -> dict[str, Any]:
    """``resolved_if`` condition "lever <= value" (Module 3's ``simulate_transition`` accepts ``{"op": "<=", "value": x}``)."""
    return {"op": "<=", "value": round(float(value), 6)}


def at_least(value: float) -> float:
    """``resolved_if`` condition "lever >= value" (a bare number)."""
    return round(float(value), 6)


_COND_OPS = (">=", "<=", ">", "<", "==")


def _condition_ok(spec: Any) -> bool:
    if isinstance(spec, bool):
        return False
    if isinstance(spec, (int, float)):
        return True
    if isinstance(spec, str):
        return re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*", spec) is not None
    if isinstance(spec, Mapping):
        return spec.get("op", ">=") in _COND_OPS and isinstance(spec.get("value"), (int, float)) and not isinstance(spec.get("value"), bool)
    return False


def condition_holds(value: float, spec: Any) -> bool:
    """Evaluate one ``resolved_if`` condition (same semantics as ``pedagogy._condition``)."""
    op, thr = ">=", spec
    if isinstance(spec, Mapping):
        op, thr = str(spec.get("op", ">=")), spec.get("value")
    elif isinstance(spec, str):
        m = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*", spec)
        assert m is not None
        op, thr = m.group(1), float(m.group(2))
    t = float(thr)
    return {">=": value >= t, "<=": value <= t, ">": value > t, "<": value < t, "==": abs(value - t) < 1e-9}[op]


# KPI-name rules for default simulation bounds (review C3): first match wins; archetype bounds take precedence.
_DEFAULT_BOUND_RULES: tuple[tuple[str, tuple[float | None, float | None]], ...] = (
    # signed by nature: earnings, flows, differences, the cash conversion cycle; cash itself is left unbounded so a
    # simulated funding gap shows as negative cash instead of being clamped away
    (r"income|contribution|cash|difference|ebitda|equity|growth|change|delta|surplus|_net_|ccc", (None, None)),
    (r"margin", (-1.0, 1.0)),
    (r"(^p_|rate|share|retention|utilization|probability|ratio_of|fill)", (0.0, 1.0)),
    (r"_cents$|_days$|units|orders|count|customers|logos|events|tickets|rows|charges|minutes|wip|throughput|ms$|_q$|index", (0.0, None)),
)


def default_simulation_bounds(kpis: Mapping[str, float]) -> dict[str, list[float | None]]:
    """Bounds implied by KPI names (non-negative amounts / counts / days, rates and utilizations in ``[0, 1]``,
    margins in ``[-1, 1]``); a rule is skipped when the baseline value itself lies outside it."""
    out: dict[str, list[float | None]] = {}
    for k, v in kpis.items():
        for rx, (lo, hi) in _DEFAULT_BOUND_RULES:
            if re.search(rx, k):
                if (lo is None and hi is None) or (lo is not None and v < lo) or (hi is not None and v > hi):
                    break
                out[k] = [lo, hi]
                break
    return out


def validate_simulation_model(model: Mapping[str, Any], levers: Sequence[Lever]) -> None:
    """Check the reduced-form model contract consumed by the quarter simulator::

        {"kpis": {kpi: float}, "lever_effects": [{"lever", "kpi", "elasticity" | "delta_per_unit", "lag_periods"}],
         "anomaly_drag": {kpi: float}, "resolved_if": {lever: condition},
         "bounds": {kpi: [lo | None, hi | None]}, "shocks": [{"quarter", "kpi", "pct" | "delta", "headline"}]}

    Each effect carries exactly one of ``elasticity`` / ``delta_per_unit`` (the other key is omitted rather than
    ``null``); levers and KPIs must exist. A ``resolved_if`` condition is a number (``>=``), a string ``"<=45"`` or
    ``{"op": "<=", "value": 45}`` (:func:`at_most`); all conditions must hold (Module 3 has no OR-groups). The
    status-quo decisions must *not* resolve the anomaly and some decision within the lever ranges must. Baseline
    KPIs must lie inside their bounds.
    """
    missing = [k for k in SIM_MODEL_KEYS if k not in model]
    if missing:
        raise GroundTruthError(f"simulation model missing keys {missing}")
    kpis = model["kpis"]
    lever_map = {lv.key: lv for lv in levers}
    if not kpis or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in kpis.values()):
        raise GroundTruthError("simulation kpis must be a non-empty {name: float} map")
    for eff in model["lever_effects"]:
        if eff.get("lever") not in lever_map:
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
    conds = model["resolved_if"]
    for k, v in conds.items():
        if k not in lever_map or not _condition_ok(v):
            raise GroundTruthError(f"resolved_if must map known levers to conditions (number, '<=x', {{op, value}}), got {k!r}: {v!r}")
    if conds:
        if all(condition_holds(float(lever_map[k].default), v) for k, v in conds.items()):
            raise GroundTruthError("resolved_if already holds for the status-quo decisions")
        for k, v in conds.items():
            lv = lever_map[k]
            if not (condition_holds(float(lv.min), v) or condition_holds(float(lv.max), v)):
                raise GroundTruthError(f"resolved_if[{k}] = {v!r} cannot be met within [{lv.min}, {lv.max}]")
    for k, b in (model.get("bounds") or {}).items():
        if k not in kpis or not isinstance(b, (list, tuple)) or len(b) != 2:
            raise GroundTruthError(f"bounds must map known kpis to [lo, hi], got {k!r}: {b!r}")
        lo, hi = b
        if (lo is not None and kpis[k] < lo - 1e-9) or (hi is not None and kpis[k] > hi + 1e-9):
            raise GroundTruthError(f"baseline kpi {k} = {kpis[k]} outside its bounds {b}")
    for sh in model.get("shocks") or []:
        if sh.get("kpi") not in kpis or not isinstance(sh.get("quarter"), int) or (("pct" in sh) == ("delta" in sh)):
            raise GroundTruthError(f"bad shock {sh!r}")


def satisfying_decisions(model: Mapping[str, Any], levers: Sequence[Lever]) -> dict[str, float]:
    """Decisions that meet every ``resolved_if`` condition (lever max for ``>=``, min for ``<=``; others at default)."""
    out = {lv.key: float(lv.default) for lv in levers}
    for lv in levers:
        spec = (model.get("resolved_if") or {}).get(lv.key)
        if spec is None:
            continue
        out[lv.key] = float(lv.max) if condition_holds(float(lv.max), spec) else float(lv.min)
    return out


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


_PERIOD_LABEL = re.compile(r"^\d{4}Q[1-4]$")


def label_prior_periods(gt: GroundTruth, periods: Sequence[str]) -> None:
    """Review P2 #27: a reference output whose period column includes quarters before ``P1`` (the warm-up quarter
    the books open with) gets ``expected["prior_periods"]`` and a note, so nobody reads them as case quarters."""
    first = periods[0] if periods else ""
    for a in gt.anomalies:
        for q in a.reference_sql:
            cols = [str(c) for c in q.expected.get("columns", [])]
            rows = q.expected.get("rows", [])
            prior: set[str] = set()
            for j, c in enumerate(cols):
                if "period" not in c.lower() and "quarter" not in c.lower():
                    continue
                for r in rows:
                    v = r[j] if j < len(r) else None
                    if isinstance(v, str) and _PERIOD_LABEL.match(v) and v < first:
                        prior.add(v)
            if prior:
                q.expected["prior_periods"] = sorted(prior)
                q.expected["prior_period_note"] = (f"{', '.join(sorted(prior))} precede the case window ({first}..{periods[-1]}): "
                                                   "warm-up quarter(s) of the books, opening context only")


# --------------------------------------------------------------------------- principal dirt (review C8)


class _RecordingConnection:
    """Pass-through DuckDB connection that records every ``execute`` of the ground-truth hook, so the queries can be
    replayed after extra dirt is added and the answer key proven unchanged."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, *args: Any) -> Any:
        self.queries.append((str(sql), tuple(args)))
        return self._con.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)

    def snapshot(self) -> list[tuple[str, tuple[Any, ...], pd.DataFrame | None]]:
        out: list[tuple[str, tuple[Any, ...], pd.DataFrame | None]] = []
        for sql, args in self.queries:
            try:
                out.append((sql, args, self._con.execute(sql, *args).fetchdf()))
            except duckdb.Error:
                out.append((sql, args, None))
        return out


_DIRT_TARGET_PER_TABLE = 3
_TEXT_DIRT_HINTS = ("name", "email", "region", "status", "segment", "category", "channel", "city", "country", "reason", "method", "plan")


def _is_transactional(df: pd.DataFrame) -> bool:
    if len(df) < 100:
        return False
    return any(pd.api.types.is_datetime64_any_dtype(df[c]) or str(c).endswith(("_at", "_date", "_ts", "_time")) for c in df.columns)


def plan_principal_dirt(builder: TableBuilder, referenced: set[str]) -> dict[str, list[str]]:
    """Extra dirt so every transactional table carries 2-3 rules at principal, with ``orphan_fk`` and ``unit_mixed``
    present somewhere. Only columns that no ground-truth query, reference SQL or reference Python names are touched
    (so the answer key cannot move); keys, GL tables and already-dirtied columns are left alone. Deterministic."""
    plan: dict[str, list[str]] = {}
    placed = {"orphan_fk": False, "unit_mixed": False}
    for name, td in builder.tables.items():
        if name.startswith("gl_") or name == "period_calendar":
            continue
        df = builder.clean_frame(name)
        if not _is_transactional(df):
            continue
        need = _DIRT_TARGET_PER_TABLE - len(td.dirt)
        if need <= 0:
            continue
        dirty_cols = {parse_col for parse_col in (r.split(":")[1] if r.count(":") >= 1 else "" for r in td.dirt)}
        keys = set(td.primary_key) | {"ingest_id"}
        free = [c for c in df.columns if c not in keys and c not in dirty_cols and str(c).lower() not in referenced]
        fk_cols = [fk[0] for fk in td.foreign_keys]
        cands: list[str] = []
        for c in free:
            if str(c).endswith("_cents") and pd.api.types.is_numeric_dtype(df[c]) and df[c].notna().any():
                cands.append(f"unit_mixed:{c}")
        for c in free:
            if (c in fk_cols or str(c).endswith("_id")) and df[c].dtype == object:
                cands.append(f"orphan_fk:{c}:0.02")
        for c in free:
            if df[c].dtype == object and any(h in str(c).lower() for h in _TEXT_DIRT_HINTS) and df[c].map(lambda v: isinstance(v, str)).all():
                cands.append(f"case_mixed:{c}" if len(cands) % 2 == 0 else f"whitespace:{c}")
        for c in free:  # optional descriptive text fields only (never dates, amounts or ids)
            if df[c].dtype == object and not str(c).endswith("_id") and any(
                    h in str(c).lower() for h in (*_TEXT_DIRT_HINTS, "description", "notes", "memo", "subject", "title", "carrier",
                                                  "service", "owner", "version")):
                cands.append(f"nulls:{c}:0.03")
        chosen: list[str] = []
        used_cols: set[str] = set()
        # prefer a rule kind that has not been placed yet, then the candidate order above
        cands.sort(key=lambda r: (0 if r.split(":")[0] in placed and not placed[r.split(":")[0]] else 1))
        for r in cands:
            col = r.split(":")[1]
            if col in used_cols or len(chosen) >= need:
                continue
            chosen.append(r)
            used_cols.add(col)
            kind = r.split(":")[0]
            if kind in placed:
                placed[kind] = True
        if chosen:
            plan[name] = chosen
    return plan


def _canon(df: pd.DataFrame) -> pd.DataFrame:
    """Order-insensitive canonical form of a query result (queries without ORDER BY may return rows in any order)."""
    out = df.reset_index(drop=True).copy()
    for c in out.columns:  # float aggregates may differ in the last bits between runs (parallel summation order)
        if pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].map(lambda v: "nan" if v != v else f"{v:.9g}")
    out = out.astype(str)
    return out.sort_values(list(out.columns), kind="stable").reset_index(drop=True) if len(out.columns) else out


def _sql_words(texts: Sequence[str]) -> set[str]:
    return {w.lower() for t in texts for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", t or "")}


def add_principal_dirt(ctx: BuildContext, state: WorldState, builder: TableBuilder, gt: GroundTruth,
                       snapshot: Sequence[tuple[str, tuple[Any, ...], pd.DataFrame | None]]) -> None:
    """Apply :func:`plan_principal_dirt`, rewrite ``company.duckdb`` and replay every ground-truth query: if any result
    changed, the extra rules are withdrawn (the DB is rewritten without them, byte-for-byte the original data)."""
    texts = [sql for sql, _a, _df in snapshot]
    texts += [q.sql for a in gt.anomalies for q in a.reference_sql]
    texts += list(gt.reference_solution.sql) + list(gt.reference_solution.python)
    plan = plan_principal_dirt(builder, _sql_words(texts))
    if not plan:
        return
    for name, rules in plan.items():
        builder.add_dirt(name, rules)
    state.tables = builder.materialize()
    state.database = builder.write(ctx.db_path, state.calendar, rel_path=ctx.rel_db_path)
    for _attempt in range(len(plan) + 1):
        con = duckdb.connect(str(ctx.db_path), read_only=True)
        culprits: set[str] = set()
        try:
            for sql, args, before in snapshot:
                try:
                    after = con.execute(sql, *args).fetchdf()
                except duckdb.Error:
                    after = None
                same = (before is None) == (after is None) and (before is None or _canon(before).equals(_canon(after)))
                if not same:
                    words = _sql_words([sql])
                    culprits = {t for t in plan if t.lower() in words} or set(plan)
                    break
        finally:
            con.close()
        if not culprits:
            return
        logger.info("%s: principal dirt on %s changed a ground-truth query; withdrawn there: %s", ctx.case_id, sorted(culprits),
                    " ".join(sql.split())[:160])
        for name in culprits:
            builder.remove_dirt(name, plan.pop(name))
        state.tables = builder.materialize()
        state.database = builder.write(ctx.db_path, state.calendar, rel_path=ctx.rel_db_path)


# --------------------------------------------------------------------------- company profile (review C4)

MIN_HEADCOUNT = 8
FYE_MIX: tuple[tuple[str, float], ...] = (("12-31", 0.7), ("06-30", 0.1), ("09-30", 0.1), ("03-31", 0.1))
# revenue per employee (USD) by industry when the corpus has no key (review P1 #6): uniform [lo, hi]
REVENUE_PER_EMPLOYEE_USD: dict[str, tuple[float, float]] = {
    "3pl": (120_000, 180_000),            # kitting / contract warehousing (labour-heavy)
    "3pl_brokerage": (450_000, 750_000),  # asset-light freight brokerage: gross billings incl. purchased transport
    "retail": (300_000, 400_000),         # distribution / retail
    "ecommerce": (300_000, 400_000),
    "saas": (180_000, 250_000),           # used only if saas.headcount_per_1m_arr is missing
    "fintech_marketplace": (250_000, 400_000),
    "manufacturing": (200_000, 300_000),
}
# stage -> company age in years at the anchor year
STAGE_AGE_YEARS: dict[str, tuple[int, int]] = {"seed": (2, 6), "growth": (5, 16), "mature": (12, 36)}


def cap_first(text: str) -> str:
    """Upper-case the first character only (``str.capitalize`` lower-cases "B2B", "DTC", "API")."""
    return text[:1].upper() + text[1:] if text else text


def founded_floor(business_model: str, industry: str) -> int | None:
    """Earliest plausible founding year for a business model (review P2 #22): usage-based API, marketplace,
    flash-sale and AI businesses >= 2008; DTC / subscription-box brands >= 2005; SaaS >= 2000 (our addition:
    the delivery model did not exist earlier); everything else unbounded."""
    bm = business_model.lower()
    words = set(re.findall(r"[a-z0-9]+", bm))
    if words & {"usage", "api", "marketplace", "ai", "kafka", "flash"} or industry == "fintech_marketplace":
        return 2008
    if words & {"dtc"} or "subscription-box" in bm or "subscription box" in bm or "direct-to-consumer" in bm:
        return 2005
    if "saas" in words or industry == "saas":
        return 2000
    return None


def revenue_per_employee_usd(rng: SeededRNG, pool: BenchmarkPool, industry: str, business_model: str) -> float:
    """Revenue per employee: SaaS from the corpus ``saas.headcount_per_1m_arr`` (quantiles 0.15-0.40, i.e. about
    $180-260k), otherwise :data:`REVENUE_PER_EMPLOYEE_USD` (asset-light brokerage uses its own band)."""
    if industry == "saas" and pool.has("saas.headcount_per_1m_arr"):
        heads_per_m = _bk.sample_between(pool, rng, "saas.headcount_per_1m_arr", "saas", 0.15, 0.40)
        return 1_000_000.0 / max(heads_per_m, 1.0)
    key = "3pl_brokerage" if industry == "3pl" and "brokerage" in business_model.lower() else industry
    lo, hi = REVENUE_PER_EMPLOYEE_USD.get(key, (200_000, 350_000))
    return float(rng.uniform(lo, hi))


def _draw_stage(rng: SeededRNG, industry: IndustryProfile | None, revenue_cents: int, options: tuple[str, ...] | None) -> str:
    mix = dict(industry.stage_mix) if industry is not None and industry.stage_mix else {"growth": 1.0}
    if revenue_cents > 5_000_000 * 100:  # seed-stage companies have a few $M of revenue at most
        mix.pop("seed", None)
    if options:
        mix = {k: v for k, v in mix.items() if k in options} or {k: 1.0 for k in options}
    keys = sorted(k for k, v in mix.items() if v > 0) or ["growth"]
    return str(rng.weighted_choice(keys, [mix.get(k, 1.0) for k in keys]))


def company_profile(ctx: BuildContext, state: WorldState, *, stage_options: tuple[str, ...] | None = None) -> CompanyProfile:
    """The company profile (review C4). Name, HQ from the ``company`` stream (name first, so its stem is known before
    ``sample_params``); stage from ``industry.stage_mix`` (restricted to ``stage_options``; ``params["stage"]`` wins);
    fiscal year end from :data:`FYE_MIX` (statements stay on calendar quarters); founding year from the stage's age band,
    bounded by :func:`founded_floor`; revenue per employee by industry (``params["revenue_per_employee_usd"]`` wins),
    stored in ``state.extras`` so headcount is re-derived from TTM revenue after posting (floor :data:`MIN_HEADCOUNT`)."""
    rng = ctx.rng.spawn("company")
    ind = state.params["industry"]
    name = rng.company_name(ind if ind in ("saas", "ecommerce", "manufacturing", "3pl", "retail") else None, exclude=())
    hq = rng.city()
    rev = int(state.params.get("revenue_ttm_cents", 0))
    bm = str(state.params.get("business_model", ind))
    stage = str(state.params.get("stage") or _draw_stage(rng.spawn("stage"), state.industry, rev, stage_options))
    fye = str(rng.spawn("fye").weighted_choice([k for k, _ in FYE_MIX], [w for _, w in FYE_MIX]))
    lo_age, hi_age = STAGE_AGE_YEARS.get(stage, (6, 25))
    anchor = state.calendar.anchor_year
    lo_year, hi_year = anchor - hi_age, anchor - lo_age
    floor = founded_floor(bm, ind)
    if floor is not None:
        lo_year = max(lo_year, floor)
        hi_year = max(hi_year, lo_year)
    founded = int(rng.spawn("founded").integers(lo_year, hi_year + 1))
    per_head = float(state.params.get("revenue_per_employee_usd") or revenue_per_employee_usd(rng.spawn("per_head"), ctx.pool, ind, bm))
    state.extras["_revenue_per_employee_usd"] = per_head
    return CompanyProfile(
        name=name, ticker_like=SeededRNG.ticker_like(name), industry_code=ind, business_model=bm, hq=hq,
        founded=founded, fiscal_year_end=fye, headcount=max(MIN_HEADCOUNT, int(round(rev / 100 / per_head))), stage=stage,
        revenue_ttm_cents=rev, description=cap_first(str(state.params.get("description", ""))),
    )


# --------------------------------------------------------------------------- balance-sheet completeness (review C1)

# corpus caption regex (line_items.json, balance_sheet, basis pct_of_assets) -> our balance-sheet caption(s), or
# None with the reason the caption is not required of a generated company.
BS_CAPTION_RULES: tuple[tuple[str, tuple[str, ...] | None, str], ...] = (
    (r"^cash|restricted cash", None, "cash is always present"),
    (r"equity", None, "a total, always present"),
    (r"goodwill|intangible|customer relationships|capitalized internal-use software",
     None, "acquisition history / software-capitalisation election; not required of a company that made neither"),
    (r"lease", None, "ASC 842 right-of-use assets and lease liabilities are not modelled (rent is expensed in 6400)"),
    (r"debt|credit facility|borrowing", None, "capital-structure choice"),
    (r"loans held for investment", None, "a lending book exists only for lending business models"),
    (r"customer deposits", None, "deposits / progress billing are not part of the modelled order-to-cash flows (net terms on shipment)"),
    (r"receivable|contract assets", ("Accounts receivable, net",), ""),
    (r"inventor", ("Inventory, net",), ""),
    (r"prepaid|deferred commissions", ("Prepaid expenses and other current assets",), ""),
    (r"property", ("Property, plant and equipment, net",), ""),
    (r"accounts payable", ("Accounts payable",), ""),
    (r"funds payable|amounts due to customers", ("Customer deposits", "Accounts payable"), ""),
    (r"accrued", ("Accrued liabilities",), ""),
    (r"deferred revenue", ("Deferred revenue, current", "Deferred revenue, non-current"), ""),
)
BS_CAPTION_MIN_SHARE = 0.02
BS_CAPTIONS_REQUIRED = True  # promoted from warning level once all 14 archetypes passed (seed sweep + every industry)


def bs_caption_checks(pool: BenchmarkPool, industry: str, statements: FinancialStatements, *,
                      exemptions: Mapping[str, str] | None = None, required: bool = True) -> list[InvariantCheck]:
    """One ``bs_caption_present[<caption>]`` check per ``line_items.json`` balance-sheet caption of ``industry`` whose
    p50 share of assets exceeds 2%: the mapped caption(s) must be non-zero in at least one reporting period.
    ``exemptions`` maps a regex on the corpus caption (optionally ``"<industry>:<regex>"``) to the reason it does not
    apply to the archetype."""
    try:
        items = pool.line_items(industry).get("balance_sheet", [])
    except KeyError:
        return []
    by_caption: dict[str, list[int]] = {}
    for bs in statements.balance:
        for ln in bs.lines:
            by_caption.setdefault(ln.caption, []).append(int(ln.cents))
    out: list[InvariantCheck] = []
    for item in items:
        if item.basis != "pct_of_assets" or item.p50 is None or item.p50 <= BS_CAPTION_MIN_SHARE:
            continue
        cap = item.caption.lower()
        exempt = None
        for key, why in (exemptions or {}).items():  # "regex" or "industry:regex"
            ind_, _, rx = key.rpartition(":") if ":" in key else ("", "", key)
            if (not ind_ or ind_ == industry) and re.search(rx, cap):
                exempt = why
                break
        rule = next(((ours, why) for rx, ours, why in BS_CAPTION_RULES if re.search(rx, cap)), None)
        name = f"bs_caption_present[{item.caption}]"
        if exempt is not None or rule is None or rule[0] is None:
            reason = exempt if exempt is not None else (rule[1] if rule else "no mapped caption")
            out.append(InvariantCheck(name=name, passed=True, detail=f"exempt: {reason}", required=required))
            continue
        ours = rule[0]
        nonzero = [c for c in ours if any(v != 0 for v in by_caption.get(c, []))]
        out.append(InvariantCheck(name=name, passed=bool(nonzero), required=required,
                                  detail=(f"p50 {item.p50:.0%} of assets; carried in {nonzero}" if nonzero
                                          else f"p50 {item.p50:.0%} of assets but {list(ours)} is zero in every period")))
    return out


DEFERRAL_SOURCE = "revenue_deferral"


def post_deferred_revenue_trueups(state: WorldState, docs: pd.DataFrame, *, revenue_account: str,
                                  posted_col: str = "posted", start_col: str = "service_start", end_col: str = "service_end",
                                  amount_col: str = "amount_cents") -> dict[str, int]:
    """Quarter-end deferral of revenue billed in advance (review C1), for archetypes that credit revenue when they invoice.

    For each quarter end ``pe`` (warm-up included) the unearned part of the documents posted to the GL by ``pe``
    (``posted_col <= pe``) whose service period ``[start_col, end_col)`` runs past ``pe`` is moved to deferred revenue
    (``Dr revenue / Cr 2200``, source :data:`DEFERRAL_SOURCE`) and reversed on the first day of the next quarter
    (the usual auto-reversing close entry). Revenue is thereby recognised in the service period; AR is untouched.
    Returns the net revenue effect per period (credit-positive) so billing-basis revenue ties can exclude it."""
    cal, L = state.calendar, state.ledger
    posted = pd.to_datetime(docs[posted_col]).to_numpy()
    start = pd.to_datetime(docs[start_col]).to_numpy()
    end = pd.to_datetime(docs[end_col]).to_numpy()
    amt = docs[amount_col].to_numpy(dtype=float)
    span = np.maximum((end - start) / np.timedelta64(1, "D"), 1.0)
    effect: dict[str, int] = {}
    prev = 0
    for per in cal.all_periods:
        pe = cal.bounds(per)[1]
        cut = np.datetime64(pe) + np.timedelta64(1, "D")  # start of the next quarter
        open_ = (posted < cut) & (end > cut)
        frac = np.clip((end - np.maximum(start, cut)) / np.timedelta64(1, "D"), 0, None) / span
        unearned = cents(float((amt[open_] * frac[open_]).sum()))
        if unearned:
            L.entry(pe, [(revenue_account, unearned, 0, {}), ("2200", 0, unearned, {})],
                    "Deferred revenue: unearned portion of invoices billed in advance (auto-reversing)", DEFERRAL_SOURCE)
            nxt = pe + dt.timedelta(days=1)
            if nxt <= cal.end:
                L.entry(nxt, [("2200", unearned, 0, {}), (revenue_account, 0, unearned, {})],
                        "Reversal of prior quarter-end revenue deferral", DEFERRAL_SOURCE)
        effect[per] = prev - unearned
        prev = unearned
    return effect


# --------------------------------------------------------------------------- noise and seasonality (review C2)

# multiplicative volume profile by calendar quarter (Q1..Q4), drawn as lo + U * (hi - lo) per quarter, then normalised
# to a mean of 1 so TTM revenue targets hold: retail / e-commerce Q4 peak (+15-25%), manufacturing Q1 dip, SaaS Q4
# bookings, 3PL peak season, payments volume around the holidays.
SEASONAL_PROFILES: dict[str, tuple[tuple[float, float], ...]] = {
    "retail": ((-0.06, -0.02), (-0.02, 0.02), (-0.01, 0.03), (0.15, 0.25)),
    "ecommerce": ((-0.06, -0.02), (-0.03, 0.01), (-0.01, 0.03), (0.15, 0.25)),
    "manufacturing": ((-0.08, -0.04), (0.00, 0.03), (-0.02, 0.02), (0.00, 0.04)),
    "3pl": ((-0.05, -0.02), (-0.01, 0.02), (0.00, 0.03), (0.08, 0.14)),
    "saas": ((-0.03, -0.01), (-0.01, 0.01), (-0.01, 0.01), (0.03, 0.06)),  # revenue; bookings use bookings_q4
    "fintech_marketplace": ((-0.04, -0.01), (-0.01, 0.02), (-0.01, 0.02), (0.06, 0.10)),
}


@dataclass
class PeriodNoise:
    """Seeded per-quarter multipliers for the healthy plan (review C2), one ``noise`` sub-stream per world.

    * ``volume[q]``: seasonal profile (:data:`SEASONAL_PROFILES`) x idiosyncratic ``1 + N(0, 2%)`` clipped to +-4%;
    * ``gm_pts[q]``: additive gross-margin shock ``N(0, 1 pt)`` clipped to +-2 pts (a COGS-ratio shift);
    * ``opex[q]``: ``1 + N(0, 1.5%)`` clipped to +-3%;
    * ``bookings_q4``: SaaS-style Q4 bookings lift (+15-25%) for archetypes that model new-customer acquisition.

    Archetypes call :meth:`Archetype.period_noise` once and apply the factors in their own plan (demand intensity,
    unit cost or the COGS ratio, the monthly opex accrual); the draws come from one stream, so a world changes
    deterministically. Month helpers return the factor of the quarter containing the date."""

    volume: dict[str, float]
    gm_pts: dict[str, float]
    opex: dict[str, float]
    seasonal: dict[str, float]
    bookings_q4: float

    @staticmethod
    def _q(d: Any) -> str:
        ts = pd.Timestamp(d)
        return f"{ts.year}Q{(ts.month - 1) // 3 + 1}"

    def vol(self, d: Any) -> float:
        """Volume multiplier for the quarter containing ``d`` (1.0 outside the calendar)."""
        return self.volume.get(self._q(d), 1.0)

    def gm(self, d: Any) -> float:
        """Additive gross-margin shift (fraction) for the quarter containing ``d``."""
        return self.gm_pts.get(self._q(d), 0.0)

    def ox(self, d: Any) -> float:
        """Opex multiplier for the quarter containing ``d``."""
        return self.opex.get(self._q(d), 1.0)

    def bookings(self, d: Any) -> float:
        """New-customer acquisition multiplier (Q4 bookings lift, mean-normalised)."""
        q = (pd.Timestamp(d).month - 1) // 3
        return (1.0 + self.bookings_q4) / (1.0 + self.bookings_q4 / 4) if q == 3 else 1.0 / (1.0 + self.bookings_q4 / 4)

    def vol_array(self, dates: Any) -> np.ndarray:
        """Vectorised :meth:`vol` over datetimes."""
        idx = pd.DatetimeIndex(pd.to_datetime(dates))
        keys = [f"{y}Q{q}" for y, q in zip(idx.year, idx.quarter, strict=True)]
        return np.array([self.volume.get(k, 1.0) for k in keys], dtype=float)


def make_period_noise(rng: SeededRNG, industry: str, calendar: FiscalCalendar, *, extra_quarters: int = 8) -> PeriodNoise:
    """Draw :class:`PeriodNoise` for the warm-up + reporting quarters (and ``extra_quarters`` earlier, for archetypes
    whose history starts before the books open)."""
    g = rng.generator
    prof = SEASONAL_PROFILES.get(industry, SEASONAL_PROFILES["manufacturing"])
    season = np.array([lo + g.random() * (hi - lo) for lo, hi in prof])
    season = (1 + season) / float(np.mean(1 + season))
    first = calendar.all_periods[0]
    labels = [_shift_q(first, -k) for k in range(extra_quarters, 0, -1)] + list(calendar.all_periods)
    n = len(labels)
    eps_v = np.clip(g.normal(0, 0.02, n), -0.04, 0.04)
    eps_g = np.clip(g.normal(0, 0.01, n), -0.02, 0.02)
    eps_o = np.clip(g.normal(0, 0.015, n), -0.03, 0.03)
    vol = {q: float(season[int(q[-1]) - 1] * (1 + e)) for q, e in zip(labels, eps_v, strict=True)}
    return PeriodNoise(volume=vol, gm_pts={q: float(e) for q, e in zip(labels, eps_g, strict=True)},
                       opex={q: float(1 + e) for q, e in zip(labels, eps_o, strict=True)},
                       seasonal={f"Q{i + 1}": float(v) for i, v in enumerate(season)}, bookings_q4=float(g.uniform(0.15, 0.25)))


def _shift_q(label: str, k: int) -> str:
    y, q = int(label[:4]), int(label[5:])
    i = y * 4 + (q - 1) + k
    return f"{i // 4}Q{i % 4 + 1}"


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
    # stages the narrative allows (None: the industry's ``stage_mix``); e.g. a "fast-growing" story -> ("growth",)
    stage_options: ClassVar[tuple[str, ...] | None] = None
    # balance-sheet captions exempt from the ``bs_caption_present`` invariant: {corpus caption keyword: reason}
    bs_caption_exemptions: ClassVar[dict[str, str]] = {}
    # opt-in: a mature company returns surplus cash (a dividend every 3-5 quarters), see :meth:`post_capital_events`
    capital_events: ClassVar[bool] = False

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
        """Generic company profile (:func:`company_profile`). ``revenue_ttm_cents`` and ``headcount`` are
        refreshed from the ledger after posting (headcount = TTM revenue / revenue per employee)."""
        return company_profile(ctx, state, stage_options=self.stage_options)

    def enable_standard_working_capital(self, ctx: BuildContext, state: WorldState, *, vendor_ap: bool = True,
                                        prepaid: bool = True) -> WorkingCapitalPolicy:
        """Opt in to the base working-capital defaults (review P0 #3 / C1) before posting: vendor AP for 30-45% of
        the monthly accrued-expense run, paid on ``fin.<industry>.dpo_days`` terms (p25-p75; 30-45 days for
        fintech, whose corpus DPO is merchant payables), and a prepaid-expense balance (insurance and software
        prepayments, renewed every ~3 months and drawn down monthly, 4-8% of the run). Only for archetypes whose own flows do not
        already book these lines (an AP sub-ledger tied to 2000 must not opt in to ``vendor_ap``)."""
        rng = ctx.rng.spawn("working_capital")
        ind = state.params["industry"]
        if ind == "fintech_marketplace" or not ctx.pool.has(f"fin.{ind}.dpo_days"):
            dpo = float(rng.uniform(30, 45))
        else:
            dpo = _bk.sample_between(ctx.pool, rng.spawn("dpo"), f"fin.{ind}.dpo_days", ind, 0.25, 0.75)
        pol = WorkingCapitalPolicy(vendor_ap_share=float(rng.uniform(0.30, 0.45)) if vendor_ap else 0.0,
                                   vendor_dpo_days=int(round(dpo)),
                                   prepaid_share=float(rng.uniform(0.04, 0.08)) if prepaid else 0.0)
        state.ledger.wc_policy = pol
        state.extras["working_capital_policy"] = pol
        return pol

    def period_noise(self, ctx: BuildContext, state: WorldState) -> PeriodNoise:
        """The world's :class:`PeriodNoise` (drawn once from the ``noise`` stream, cached in ``state.extras``)."""
        if "period_noise" not in state.extras:
            state.extras["period_noise"] = make_period_noise(ctx.rng.spawn("noise"), state.params["industry"], state.calendar)
        return state.extras["period_noise"]

    def post_capital_events(self, ctx: BuildContext, state: WorldState) -> list[tuple[dt.date, int]]:
        """Review P1 #5 / #7: a mature company does not let cash pile up in a straight line. Every 3-5 quarters (seeded)
        it pays a dividend of 40-70% of the cash above ~one month of revenue, capped so cash never falls below that floor
        later in the window. Growth / seed companies keep their cash (they are investing). Posted after the archetype's
        books (only the cash and equity lines move)."""
        if state.company is None or state.company.stage != "mature":
            return []
        rng = ctx.rng.spawn("capital_events")
        L, cal = state.ledger, state.calendar
        rows = sorted((e.date, ln.debit_cents - ln.credit_cents) for e in L.entries for ln in e.lines if ln.account == "1000")
        dates = np.array([d.toordinal() for d, _ in rows])
        path = np.cumsum([v for _, v in rows])
        rev_q = abs(sum(L.activity(cal.periods[0])[c] for c in ("4000", "4010", "4020", "4100", "4110")))
        floor = rev_q // 3  # ~one month of revenue stays on hand
        out: list[tuple[dt.date, int]] = []
        k = int(rng.integers(1, 4))
        paid = 0
        while k < len(cal.periods):
            qe = cal.bounds(cal.periods[k])[1]
            later = path[dates >= qe.toordinal()] - paid
            at = path[dates <= qe.toordinal()][-1] - paid if (dates <= qe.toordinal()).any() else 0
            head = int(min(at, later.min() if len(later) else at)) - floor
            amt = int(head * float(rng.uniform(0.4, 0.7)) // 100_000 * 100_000)
            if amt > 0:
                L.dividend(qe, amt, memo="Dividend paid to shareholders")
                out.append((qe, amt))
                paid += amt
            k += int(rng.integers(3, 6))
        state.extras["capital_events"] = out
        return out

    @staticmethod
    def fresh_ledger(state: WorldState) -> Ledger:
        """A new empty ledger carrying the world's working-capital policy (for archetypes that re-post from scratch)."""
        L = Ledger()
        L.wc_policy = state.extras.get("working_capital_policy")
        return L

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
        # the company's name stem is the first draw of the "company" stream; exclude it from every
        # customer / supplier name drawn during this build (review P2 #19)
        own_stem = ctx.rng.spawn("company").choice(COMPANY_STEMS)
        with excluding_name_stems({own_stem}):
            return self._build(ctx)

    def _build(self, ctx: BuildContext) -> CaseWorld:
        params = self.sample_params(ctx.rng.spawn("params"), ctx.pool, ctx.difficulty, ctx.industry)
        if "industry" not in params:
            raise ValueError(f"{self.key}.sample_params must set 'industry'")
        state = self.new_state(ctx, params)
        state.industry = ctx.pool.industry(params["industry"])
        state.company = self.build_company(ctx, state)
        self.build_baseline(ctx, state)
        state.anomalies = list(self.inject_anomalies(ctx, state))
        self.post_ledger(ctx, state)
        state.ledger.flush_scheduled(state.calendar.end)
        if self.capital_events:
            self.post_capital_events(ctx, state)
        # TTM revenue from the books
        ttm = sum(-sum(state.ledger.activity(p)[c] for c in ("4000", "4010", "4020", "4100", "4110")) for p in state.periods[-4:])
        per_head = float(state.extras.get("_revenue_per_employee_usd", 0.0)) or 250_000.0
        state.company = state.company.model_copy(update={"revenue_ttm_cents": int(ttm),
                                                         "headcount": max(MIN_HEADCOUNT, int(round(ttm / 100 / per_head)))})

        statements = build_statements(state.ledger, state.periods)
        state.statements = statements
        ties = self.subledger_ties(ctx, state)
        state.invariants = assert_invariants(state.ledger, statements, ties)
        captions = bs_caption_checks(ctx.pool, params["industry"], statements, exemptions=self.bs_caption_exemptions,
                                     required=BS_CAPTIONS_REQUIRED)
        state.invariants += captions
        failed = [c for c in captions if c.required and not c.passed]
        if failed:
            raise WorldInvariantError(failed)

        template = ctx.pool.schema_template(self.schema_template) if self.schema_template in ctx.pool.schema_template_keys() else None
        builder = TableBuilder(ctx.rng.spawn("datagen"), template, difficulty=ctx.difficulty)
        self.build_tables(ctx, state, builder)
        builder.add_ledger_tables(state.ledger)
        state.tables = builder.materialize()
        state.database = builder.write(ctx.db_path, state.calendar, rel_path=ctx.rel_db_path)

        con = duckdb.connect(str(ctx.db_path), read_only=True)
        rec = _RecordingConnection(con)
        try:
            gt = self.build_ground_truth(ctx, state, rec)  # type: ignore[arg-type]
            snapshot = rec.snapshot() if ctx.difficulty == "principal" else []
        finally:
            con.close()
        if ctx.difficulty == "principal":
            add_principal_dirt(ctx, state, builder, gt, snapshot)
        levers = self.levers(ctx, state)
        if not gt.decision_levers:
            gt = gt.model_copy(update={"decision_levers": levers})
        self._validate_ground_truth(gt)
        label_prior_periods(gt, state.periods)
        dossier = self.build_dossier_seed(ctx, state)
        conceal_text = " ".join(dossier.conceal).lower()
        missing_terms = [t for t in self.root_cause_terms if t.lower() not in conceal_text]
        if missing_terms:
            raise GroundTruthError(f"dossier conceal list misses root-cause terms {missing_terms}")
        operations = self.build_operations(ctx, state)
        model = _round_floats(self.simulation_model(ctx, state))
        bounds = default_simulation_bounds(model["kpis"])
        bounds.update(model.get("bounds") or {})
        if bounds:
            model["bounds"] = {k: bounds[k] for k in sorted(bounds)}
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
