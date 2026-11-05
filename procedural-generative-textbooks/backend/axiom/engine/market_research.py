"""Research tier: source-cited benchmark distributions and real-world reference material.

The corpus lives in ``axiom/engine/benchmarks/*.json`` (see ARCHITECTURE.md section 1).
Loading is tolerant: a missing or malformed file is logged and treated as empty, and each
record is validated with pydantic, with invalid records skipped and logged. Unknown keys
on a record are dropped (logged at debug level) so the corpus can carry extra annotations.

Optional live refresh: :class:`EdgarFramesAdapter` pulls SEC XBRL "frames" (keyless) and
turns cross-filer ratios into ``Benchmark`` percentiles, which
:meth:`BenchmarkPool.apply_live_overrides` merges in memory only.

CLI::

    python -m axiom.engine.market_research report
    python -m axiom.engine.market_research sample saas.gross_churn_monthly --seed 7
    python -m axiom.engine.market_research refresh --year 2024
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from scipy import stats

from axiom.engine.rng import SeededRNG, benchmark_quantile
from axiom.models import Benchmark, ColumnSpec, IndustryProfile, Pillar

__all__ = [
    "LineItem",
    "TemplateTable",
    "SchemaTemplate",
    "ForensicPattern",
    "EdgarRefreshSpec",
    "DEFAULT_EDGAR_SPECS",
    "default_refresh_year",
    "ArchetypeHint",
    "ARCHETYPE_HINTS",
    "BenchmarkPool",
    "EdgarFramesAdapter",
    "ResearchBrief",
    "get_default_pool",
    "main",
]

logger = logging.getLogger("axiom.market_research")

BENCHMARK_FILES: tuple[str, ...] = ("saas_metrics.json", "operations.json", "financial_ratios.json")
STATEMENTS: tuple[str, ...] = ("income_statement", "balance_sheet", "cash_flow")

M = TypeVar("M", bound=BaseModel)


# --------------------------------------------------------------------------- corpus record models


class LineItem(BaseModel):
    """A typical 10-K caption, optionally with its typical size as a % of revenue."""

    model_config = ConfigDict(extra="ignore")

    caption: str
    statement: Literal["income_statement", "balance_sheet", "cash_flow"]
    typical_pct_of_revenue: dict[str, float] | None = None
    basis: str | None = None  # pct_of_revenue | pct_of_assets | share_of_inventory
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None


class TemplateTable(BaseModel):
    """One table in a standard schema template."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    columns: list[ColumnSpec] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[tuple[str, str, str]] = Field(default_factory=list)
    typical_rows_per_month: int = 0

    @field_validator("columns", mode="before")
    @classmethod
    def _strip_column_extras(cls, value: Any) -> Any:
        """Drop unknown keys on column dicts (ColumnSpec forbids extras)."""
        if isinstance(value, list):
            allowed = set(ColumnSpec.model_fields)
            return [{k: v for k, v in c.items() if k in allowed} if isinstance(c, dict) else c for c in value]
        return value


class SchemaTemplate(BaseModel):
    """A standard relational schema (e.g. an order-to-cash ERP slice)."""

    model_config = ConfigDict(extra="ignore")

    key: str
    description: str = ""
    tables: list[TemplateTable] = Field(default_factory=list)

    def table(self, name: str) -> TemplateTable:
        """Look up a table by name."""
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(f"schema template {self.key!r} has no table {name!r}")


class ForensicPattern(BaseModel):
    """A documented anomaly/fraud/failure pattern with detection signatures and real cases."""

    model_config = ConfigDict(extra="ignore")

    key: str
    title: str
    pillar: Pillar
    description: str = ""
    signatures: list[Any] = Field(default_factory=list)
    typical_magnitude: dict[str, Any] = Field(default_factory=dict)
    detection: dict[str, Any] = Field(default_factory=dict)
    real_cases: list[Any] = Field(default_factory=list)
    sources: list[Any] = Field(default_factory=list)


class EdgarRefreshSpec(BaseModel):
    """How to compute one live benchmark from two XBRL concepts: ``multiplier * num / den``."""

    model_config = ConfigDict(extra="forbid")

    key: str
    industry: str = "any"
    metric: str
    unit: str = "ratio"
    numerator: str
    denominator: str
    numerator_instant: bool = False
    denominator_instant: bool = False
    multiplier: float = 1.0
    distribution: Literal["lognormal", "normal", "beta", "triangular", "empirical"] = "empirical"
    year: int | None = None


def default_refresh_year() -> int:
    """Most recent calendar year whose annual XBRL frames are reasonably complete."""
    return dt.date.today().year - 1


DEFAULT_EDGAR_SPECS: tuple[EdgarRefreshSpec, ...] = (
    EdgarRefreshSpec(
        key="edgar.any.gross_margin", metric="Gross margin (GrossProfit / Revenue)", numerator="GrossProfit",
        denominator="RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
    EdgarRefreshSpec(
        key="edgar.any.operating_margin", metric="Operating margin (OperatingIncomeLoss / Revenue)",
        numerator="OperatingIncomeLoss", denominator="RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
    EdgarRefreshSpec(
        key="edgar.any.dso_days", metric="Days sales outstanding (year-end AR / Revenue * 365)", unit="days",
        numerator="AccountsReceivableNetCurrent", numerator_instant=True,
        denominator="RevenueFromContractWithCustomerExcludingAssessedTax", multiplier=365.0,
    ),
    EdgarRefreshSpec(
        key="edgar.any.current_ratio", metric="Current ratio (AssetsCurrent / LiabilitiesCurrent)", unit="x",
        numerator="AssetsCurrent", numerator_instant=True, denominator="LiabilitiesCurrent", denominator_instant=True,
    ),
    EdgarRefreshSpec(
        key="edgar.any.dio_days", metric="Days inventory outstanding (year-end inventory / COGS * 365)", unit="days",
        numerator="InventoryNet", numerator_instant=True, denominator="CostOfGoodsAndServicesSold", multiplier=365.0,
    ),
)


class ArchetypeHint(BaseModel):
    """What research material an archetype needs: pillars and keyword hints."""

    model_config = ConfigDict(extra="forbid")

    pillars: list[Pillar]
    metric_keywords: list[str] = Field(default_factory=list)
    pattern_keywords: list[str] = Field(default_factory=list)
    schema_template: str | None = None


ARCHETYPE_HINTS: dict[str, ArchetypeHint] = {
    "working_capital_crunch": ArchetypeHint(
        pillars=["accounting_finance"],
        metric_keywords=["dso", "dpo", "dio", "ccc", "cash_conversion", "current_ratio", "quick", "receivable", "inventory", "payable", "gross_margin"],
        pattern_keywords=["working_capital", "receivable", "dso", "inventory", "payable", "cash"],
    ),
    "abc_cost_distortion": ArchetypeHint(
        pillars=["accounting_finance", "operations_strategy"],
        metric_keywords=["overhead", "gross_margin", "cogs", "cost", "sga", "opex"],
        pattern_keywords=["cost", "allocation", "overhead", "margin"],
    ),
    "ma_synergies": ArchetypeHint(
        pillars=["accounting_finance", "operations_strategy"],
        metric_keywords=["synerg", "sga", "opex", "gross_margin", "operating_margin", "ebitda", "goodwill"],
        pattern_keywords=["synerg", "integration", "goodwill", "acquisition", "merger"],
    ),
    "forensic_revenue": ArchetypeHint(
        pillars=["accounting_finance"],
        metric_keywords=["dso", "revenue", "returns", "deferred", "gross_margin", "receivable"],
        pattern_keywords=["revenue", "channel_stuffing", "round_trip", "bill_and_hold", "cookie_jar", "dso", "returns"],
    ),
    "toc_bottleneck": ArchetypeHint(
        pillars=["operations_strategy"],
        metric_keywords=["utilization", "throughput", "cycle", "oee", "capacity", "wip", "lead_time"],
        pattern_keywords=["bottleneck", "capacity", "throughput", "utilization"],
    ),
    "cohort_collapse": ArchetypeHint(
        pillars=["operations_strategy", "accounting_finance"],
        metric_keywords=["churn", "retention", "nrr", "grr", "ltv", "cac", "payback", "cohort", "arpa"],
        pattern_keywords=["churn", "cohort", "retention", "growth"],
    ),
    "bullwhip": ArchetypeHint(
        pillars=["operations_strategy"],
        metric_keywords=["inventory", "lead_time", "fill_rate", "order", "forecast", "turns", "stockout", "dio"],
        pattern_keywords=["bullwhip", "inventory", "forecast", "order"],
    ),
    "pricing_elasticity": ArchetypeHint(
        pillars=["operations_strategy"],
        metric_keywords=["price", "discount", "elasticity", "arpa", "arpu", "gross_margin", "conversion", "aov"],
        pattern_keywords=["pricing", "discount", "elasticity", "leakage"],
    ),
    "idempotent_webhooks": ArchetypeHint(
        pillars=["system_design"],
        metric_keywords=["webhook", "retry", "duplicate", "payment", "error_rate", "latency"],
        pattern_keywords=["webhook", "duplicate", "idempot", "retry", "double"],
    ),
    "outbox_pattern": ArchetypeHint(
        pillars=["system_design"],
        metric_keywords=["event", "latency", "error_rate", "consistency"],
        pattern_keywords=["outbox", "dual_write", "event", "consistency", "lost"],
    ),
    "replica_lag": ArchetypeHint(
        pillars=["system_design"],
        metric_keywords=["replica", "lag", "latency", "read"],
        pattern_keywords=["replica", "lag", "stale", "read_after_write"],
    ),
    "window_reconciliation": ArchetypeHint(
        pillars=["sql", "accounting_finance"],
        metric_keywords=["revenue", "dso", "receivable", "invoice", "payment"],
        pattern_keywords=["reconcil", "revenue", "invoice", "payment", "cutoff"],
    ),
    "cohort_matrix": ArchetypeHint(
        pillars=["sql", "operations_strategy"],
        metric_keywords=["churn", "retention", "cohort", "nrr", "grr"],
        pattern_keywords=["cohort", "retention", "churn"],
    ),
    "monte_carlo_cash": ArchetypeHint(
        pillars=["python", "accounting_finance"],
        metric_keywords=["cash", "burn", "runway", "dso", "collection", "churn", "growth"],
        pattern_keywords=["cash", "runway", "liquidity", "collection"],
    ),
}


# --------------------------------------------------------------------------- helpers


def _read_json(path: Path, issues: list[str]) -> Any | None:
    """Read a JSON file; on absence or parse failure log a warning, record the issue, return None."""
    if not path.exists():
        msg = f"{path.name}: missing (treated as empty)"
        logger.warning("research corpus %s", msg)
        issues.append(msg)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"{path.name}: unreadable ({exc}); treated as empty"
        logger.warning("research corpus %s", msg)
        issues.append(msg)
        return None


def _validate(model: type[M], record: Any, where: str, issues: list[str], *, strip_unknown: bool = True) -> M | None:
    """Validate one record; on failure log and record the issue, return None."""
    if not isinstance(record, dict):
        msg = f"{where}: expected an object, got {type(record).__name__}; skipped"
        logger.warning("research corpus %s", msg)
        issues.append(msg)
        return None
    data = record
    if strip_unknown:
        allowed = set(model.model_fields)
        unknown = sorted(set(record) - allowed)
        if unknown:
            logger.debug("%s: dropping unknown keys %s", where, unknown)
            data = {k: v for k, v in record.items() if k in allowed}
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"msg": str(exc)}
        msg = f"{where}: invalid record skipped ({first.get('loc', '')}: {first.get('msg', '')})"
        logger.warning("research corpus %s", msg)
        issues.append(msg)
        return None


def _nearest_correlation(matrix: NDArray[np.float64]) -> NDArray[np.float64]:
    """Project a symmetric matrix to the nearest PD correlation matrix (eigenvalue clipping)."""
    sym = (matrix + matrix.T) / 2.0
    vals, vecs = np.linalg.eigh(sym)
    vals = np.clip(vals, 1e-8, None)
    fixed = vecs @ np.diag(vals) @ vecs.T
    d = np.sqrt(np.diag(fixed))
    fixed = fixed / np.outer(d, d)
    np.fill_diagonal(fixed, 1.0)
    return fixed


# --------------------------------------------------------------------------- BenchmarkPool


class BenchmarkPool:
    """In-memory, validated research corpus with sampling helpers."""

    def __init__(
        self,
        benchmarks: Mapping[str, Benchmark] | None = None,
        industries: Mapping[str, IndustryProfile] | None = None,
        industry_benchmark_keys: Mapping[str, Sequence[str]] | None = None,
        line_items: Mapping[str, Mapping[str, Sequence[LineItem]]] | None = None,
        schema_templates: Mapping[str, SchemaTemplate] | None = None,
        forensic_patterns: Mapping[str, ForensicPattern] | None = None,
        meta: Mapping[str, Any] | None = None,
        load_issues: Sequence[str] | None = None,
        source_dir: Path | None = None,
    ) -> None:
        """Build a pool from already-validated parts. Use :meth:`load` to read the corpus."""
        self._benchmarks: dict[str, Benchmark] = dict(benchmarks or {})
        self._overrides: dict[str, Benchmark] = {}
        self._industries: dict[str, IndustryProfile] = dict(industries or {})
        self._industry_keys: dict[str, list[str]] = {k: list(v) for k, v in (industry_benchmark_keys or {}).items()}
        self._line_items: dict[str, dict[str, list[LineItem]]] = {
            code: {st: list(items) for st, items in by_st.items()} for code, by_st in (line_items or {}).items()
        }
        self._templates: dict[str, SchemaTemplate] = dict(schema_templates or {})
        self._patterns: dict[str, ForensicPattern] = dict(forensic_patterns or {})
        self.meta: dict[str, Any] = dict(meta or {})
        self.load_issues: list[str] = list(load_issues or [])
        self.source_dir: Path | None = source_dir
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, benchmarks_dir: str | Path | None = None) -> BenchmarkPool:
        """Load and validate the corpus from ``benchmarks_dir`` (default: ``Settings.benchmarks_dir``)."""
        if benchmarks_dir is None:
            from axiom.config import get_settings

            benchmarks_dir = get_settings().benchmarks_dir
        root = Path(benchmarks_dir)
        issues: list[str] = []
        meta: dict[str, Any] = {}
        benchmarks: dict[str, Benchmark] = {}

        def add_benchmark(record: Any, where: str) -> str | None:
            bench = _validate(Benchmark, record, where, issues)
            if bench is None:
                return None
            if bench.key in benchmarks:
                msg = f"{where}: duplicate benchmark key {bench.key!r}; first definition kept"
                logger.warning("research corpus %s", msg)
                issues.append(msg)
                return bench.key
            benchmarks[bench.key] = bench
            return bench.key

        for fname in BENCHMARK_FILES:
            doc = _read_json(root / fname, issues)
            if doc is None:
                continue
            if not isinstance(doc, dict):
                issues.append(f"{fname}: top level must be an object; ignored")
                logger.warning("research corpus %s: top level must be an object", fname)
                continue
            meta[fname] = doc.get("meta", {})
            records = doc.get("benchmarks", [])
            if not isinstance(records, list):
                issues.append(f"{fname}: 'benchmarks' must be a list; ignored")
                continue
            for i, rec in enumerate(records):
                add_benchmark(rec, f"{fname}[{i}]")

        # industries.json
        industries: dict[str, IndustryProfile] = {}
        industry_keys: dict[str, list[str]] = {}
        doc = _read_json(root / "industries.json", issues)
        if isinstance(doc, dict):
            meta["industries.json"] = doc.get("meta", {})
            for i, rec in enumerate(doc.get("benchmarks", []) or []):
                add_benchmark(rec, f"industries.json:benchmarks[{i}]")
            for i, rec in enumerate(doc.get("industries", []) or []):
                where = f"industries.json:industries[{i}]"
                if not isinstance(rec, dict):
                    issues.append(f"{where}: expected an object; skipped")
                    continue
                rec = dict(rec)
                declared = [k for k in (rec.pop("benchmark_keys", None) or []) if isinstance(k, str)]
                embedded = rec.pop("benchmarks", None)
                embedded_items: Iterable[Any] = []
                if isinstance(embedded, dict):
                    embedded_items = embedded.values()
                elif isinstance(embedded, list):
                    embedded_items = embedded
                for j, brec in enumerate(embedded_items):
                    key = add_benchmark(brec, f"{where}.benchmarks[{j}]")
                    if key and key not in declared:
                        declared.append(key)
                profile = _validate(IndustryProfile, rec, where, issues)
                if profile is None:
                    continue
                industries[profile.code] = profile
                industry_keys[profile.code] = declared
        elif doc is not None:
            issues.append("industries.json: top level must be an object; ignored")

        # line_items.json
        line_items: dict[str, dict[str, list[LineItem]]] = {}
        doc = _read_json(root / "line_items.json", issues)
        if isinstance(doc, dict):
            meta["line_items.json"] = doc.get("meta", {})
            by_industry = doc.get("by_industry", {})
            if isinstance(by_industry, dict):
                for code, statements in by_industry.items():
                    if not isinstance(statements, dict):
                        issues.append(f"line_items.json:{code}: expected an object; skipped")
                        continue
                    out: dict[str, list[LineItem]] = {}
                    for st in STATEMENTS:
                        items: list[LineItem] = []
                        for j, raw in enumerate(statements.get(st, []) or []):
                            rec = {"caption": raw} if isinstance(raw, str) else (dict(raw) if isinstance(raw, dict) else raw)
                            if isinstance(rec, dict):
                                rec["statement"] = st
                            item = _validate(LineItem, rec, f"line_items.json:{code}.{st}[{j}]", issues, strip_unknown=False)
                            if item is not None:
                                items.append(item)
                        out[st] = items
                    line_items[str(code)] = out

        # schema_templates.json
        templates: dict[str, SchemaTemplate] = {}
        doc = _read_json(root / "schema_templates.json", issues)
        if isinstance(doc, dict):
            meta["schema_templates.json"] = doc.get("meta", {})
            raw_templates = doc.get("templates", {})
            if isinstance(raw_templates, dict):
                for key, rec in raw_templates.items():
                    if isinstance(rec, dict):
                        rec = {**rec, "key": key}
                    tmpl = _validate(SchemaTemplate, rec, f"schema_templates.json:{key}", issues, strip_unknown=False)
                    if tmpl is not None:
                        templates[str(key)] = tmpl

        # forensic_patterns.json
        patterns: dict[str, ForensicPattern] = {}
        doc = _read_json(root / "forensic_patterns.json", issues)
        if isinstance(doc, dict):
            meta["forensic_patterns.json"] = doc.get("meta", {})
            for i, rec in enumerate(doc.get("patterns", []) or []):
                pat = _validate(ForensicPattern, rec, f"forensic_patterns.json[{i}]", issues, strip_unknown=False)
                if pat is not None:
                    patterns[pat.key] = pat

        pool = cls(
            benchmarks=benchmarks,
            industries=industries,
            industry_benchmark_keys=industry_keys,
            line_items=line_items,
            schema_templates=templates,
            forensic_patterns=patterns,
            meta=meta,
            load_issues=issues,
            source_dir=root,
        )
        logger.info(
            "research corpus loaded from %s: %d benchmarks, %d industries, %d templates, %d patterns, %d issues",
            root, len(benchmarks), len(industries), len(templates), len(patterns), len(issues),
        )
        return pool

    # ------------------------------------------------------------------ benchmarks

    def _all(self) -> dict[str, Benchmark]:
        merged = dict(self._benchmarks)
        merged.update(self._overrides)
        return merged

    def keys(self) -> list[str]:
        """All benchmark keys (corpus + live overrides), sorted."""
        return sorted(self._all())

    def __len__(self) -> int:
        return len(self._all())

    def has(self, key: str) -> bool:
        """True if a benchmark with ``key`` exists."""
        return key in self._overrides or key in self._benchmarks

    def get(self, key: str) -> Benchmark:
        """The benchmark for ``key`` (live override first). Raises ``KeyError`` if absent."""
        if key in self._overrides:
            return self._overrides[key]
        try:
            return self._benchmarks[key]
        except KeyError:
            raise KeyError(f"unknown benchmark {key!r}") from None

    def search(self, industry: str | None = None, metric_substring: str | None = None, *, include_any: bool = False) -> list[Benchmark]:
        """Benchmarks filtered by industry (exact; plus ``"any"`` if ``include_any``) and a
        case-insensitive substring of the key or metric. Sorted by key."""
        needle = metric_substring.lower() if metric_substring else None
        out: list[Benchmark] = []
        for key in self.keys():
            b = self.get(key)
            if industry is not None and not (b.industry == industry or (include_any and b.industry == "any")):
                continue
            if needle is not None and needle not in b.key.lower() and needle not in b.metric.lower():
                continue
            out.append(b)
        return out

    def sample(self, key: str, rng: SeededRNG) -> float:
        """One draw from benchmark ``key`` using ``rng`` (delegates to ``rng.sample_benchmark``)."""
        return rng.sample_benchmark(self.get(key))

    def sample_many(self, keys: Iterable[str], rng: SeededRNG) -> dict[str, float]:
        """Independent draws for several keys. Each key uses its own child stream
        (``rng.spawn("bench:<key>")``), so results do not depend on key order or set."""
        return {key: rng.spawn(f"bench:{key}").sample_benchmark(self.get(key)) for key in keys}

    def correlated_sample(
        self,
        keys: Sequence[str],
        rng: SeededRNG,
        corr_matrix: Sequence[Sequence[float]] | NDArray[np.float64],
        n: int | None = None,
    ) -> dict[str, float] | dict[str, NDArray[np.float64]]:
        """Gaussian-copula draws: correlated normals (Cholesky) mapped through each
        benchmark's quantile function. Returns floats if ``n`` is None, else arrays of length ``n``.
        A non-PD matrix is projected to the nearest correlation matrix (with a warning)."""
        k = len(keys)
        corr = np.asarray(corr_matrix, dtype=float)
        if corr.shape != (k, k):
            raise ValueError(f"corr_matrix must be {k}x{k}, got {corr.shape}")
        if not np.allclose(corr, corr.T, atol=1e-9):
            raise ValueError("corr_matrix must be symmetric")
        if not np.allclose(np.diag(corr), 1.0):
            raise ValueError("corr_matrix must have a unit diagonal")
        try:
            chol = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            logger.warning("correlated_sample: corr_matrix not positive definite; using nearest correlation matrix")
            chol = np.linalg.cholesky(_nearest_correlation(corr))
        benches = [self.get(key) for key in keys]
        m = 1 if n is None else int(n)
        z = rng.generator.standard_normal((m, k)) @ chol.T
        u = stats.norm.cdf(z)
        cols = {key: benchmark_quantile(b, u[:, i]) for i, (key, b) in enumerate(zip(keys, benches, strict=True))}
        if n is None:
            return {key: float(col[0]) for key, col in cols.items()}
        return cols

    # ------------------------------------------------------------------ industries and reference data

    def industry_codes(self) -> list[str]:
        """Codes of industries in the corpus, sorted."""
        return sorted(self._industries)

    def industry(self, code: str) -> IndustryProfile:
        """Industry profile with ``benchmarks`` resolved (declared keys that exist, plus every
        benchmark tagged with this industry) and ``line_items`` filled from ``line_items.json``
        if the profile has none. Raises ``KeyError`` for an unknown code."""
        if code not in self._industries:
            raise KeyError(f"unknown industry {code!r}")
        base = self._industries[code]
        resolved: dict[str, Benchmark] = {}
        missing: list[str] = []
        for key in self._industry_keys.get(code, []):
            if self.has(key):
                resolved[key] = self.get(key)
            else:
                missing.append(key)
        if missing:
            logger.debug("industry %s: declared benchmark keys missing from corpus: %s", code, missing)
        for b in self.search(industry=code):
            resolved.setdefault(b.key, b)
        line_items = list(base.line_items)
        if not line_items:
            by_st = self.line_items(code)
            line_items = [it.caption for st in ("income_statement", "balance_sheet") for it in by_st.get(st, [])]
        return base.model_copy(update={"benchmarks": dict(sorted(resolved.items())), "line_items": line_items}, deep=True)

    def industries(self) -> list[IndustryProfile]:
        """All industry profiles, resolved, sorted by code."""
        return [self.industry(code) for code in self.industry_codes()]

    def declared_keys(self, code: str) -> list[str]:
        """Benchmark keys an industry declares in ``industries.json`` (whether or not present)."""
        return list(self._industry_keys.get(code, []))

    def line_items(self, code: str) -> dict[str, list[LineItem]]:
        """Line items for an industry keyed by statement (falls back to ``"any"``; empty if none)."""
        by_st = self._line_items.get(code) or self._line_items.get("any") or {}
        return {st: list(by_st.get(st, [])) for st in STATEMENTS}

    def schema_template_keys(self) -> list[str]:
        """Available schema template keys, sorted."""
        return sorted(self._templates)

    def schema_template(self, key: str) -> SchemaTemplate:
        """Schema template by key. Raises ``KeyError`` if absent."""
        try:
            return self._templates[key]
        except KeyError:
            raise KeyError(f"unknown schema template {key!r}") from None

    def forensic_pattern_keys(self) -> list[str]:
        """Available forensic pattern keys, sorted."""
        return sorted(self._patterns)

    def forensic_pattern(self, key: str) -> ForensicPattern:
        """Forensic pattern by key. Raises ``KeyError`` if absent."""
        try:
            return self._patterns[key]
        except KeyError:
            raise KeyError(f"unknown forensic pattern {key!r}") from None

    def patterns_for_pillar(self, pillar: str) -> list[ForensicPattern]:
        """Forensic patterns tagged with ``pillar``, sorted by key."""
        return [self._patterns[k] for k in sorted(self._patterns) if self._patterns[k].pillar == pillar]

    def citations(self) -> list[dict[str, str | None]]:
        """``[{key, source, source_url}]`` for every benchmark, sorted by key."""
        return [{"key": b.key, "source": b.source, "source_url": b.source_url} for b in (self.get(k) for k in self.keys())]

    def coverage_report(self) -> dict[str, Any]:
        """Which benchmark keys exist per industry, plus corpus totals and load issues."""
        by_industry: dict[str, Any] = {}
        for code in self.industry_codes():
            declared = self._industry_keys.get(code, [])
            present = [k for k in declared if self.has(k)]
            tagged = [b.key for b in self.search(industry=code)]
            profile = self._industries[code]
            by_industry[code] = {
                "name": profile.name,
                "declared": len(declared),
                "present": sorted(set(present) | set(tagged)),
                "missing": [k for k in declared if not self.has(k)],
                "line_items": {st: len(v) for st, v in self.line_items(code).items()},
                "schema_template": profile.schema_template,
                "schema_template_present": profile.schema_template in self._templates,
            }
        tag_counts: dict[str, int] = {}
        for key in self.keys():
            ind = self.get(key).industry
            tag_counts[ind] = tag_counts.get(ind, 0) + 1
        pillar_counts: dict[str, int] = {}
        for p in self._patterns.values():
            pillar_counts[p.pillar] = pillar_counts.get(p.pillar, 0) + 1
        return {
            "source_dir": str(self.source_dir) if self.source_dir else None,
            "benchmarks_total": len(self),
            "live_overrides": sorted(self._overrides),
            "benchmarks_by_industry_tag": dict(sorted(tag_counts.items())),
            "industries": by_industry,
            "schema_templates": self.schema_template_keys(),
            "forensic_patterns_by_pillar": dict(sorted(pillar_counts.items())),
            "load_issues": list(self.load_issues),
        }

    # ------------------------------------------------------------------ live overrides

    def add_override(self, benchmark: Benchmark) -> None:
        """Register an in-memory override (never written to the corpus files)."""
        with self._lock:
            self._overrides[benchmark.key] = benchmark

    def clear_overrides(self) -> None:
        """Drop all live overrides."""
        with self._lock:
            self._overrides.clear()

    def apply_live_overrides(
        self, adapter: EdgarFramesAdapter, specs: Iterable[EdgarRefreshSpec], year: int | None = None
    ) -> list[Benchmark]:
        """Refresh benchmarks from EDGAR and merge them in memory (corpus files are never
        written). Each spec uses its own ``year``, else ``year``, else the last calendar year.
        Failed specs are skipped with a warning, leaving the corpus value in effect.
        Returns the benchmarks applied."""
        applied: list[Benchmark] = []
        for spec in specs:
            spec_year = spec.year or year or default_refresh_year()
            bench = adapter.refresh_from_spec(spec, spec_year)
            if bench is None:
                logger.warning("live override %s unavailable; keeping corpus value", spec.key)
                continue
            self.add_override(bench)
            applied.append(bench)
        return applied


# --------------------------------------------------------------------------- EDGAR adapter


class EdgarFramesAdapter:
    """Keyless SEC XBRL frames client with an on-disk JSON cache and a polite rate limit."""

    BASE_URL = "https://data.sec.gov/api/xbrl/frames"
    DEFAULT_USER_AGENT = "Axiom local research tool contact@example.com"

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
        max_requests_per_second: float = 5.0,
        timeout_s: float = 20.0,
        min_filers: int = 20,
    ) -> None:
        """Create the adapter. ``transport`` lets tests inject an ``httpx.MockTransport``."""
        if cache_dir is None:
            from axiom.config import get_settings

            cache_dir = get_settings().research_cache_dir
        assert cache_dir is not None
        self.cache_dir = Path(cache_dir)
        self.user_agent = user_agent or os.environ.get("AXIOM_SEC_USER_AGENT") or self.DEFAULT_USER_AGENT
        self.min_interval_s = 1.0 / max(max_requests_per_second, 0.1)
        self.min_filers = min_filers
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_s,
            headers={"User-Agent": self.user_agent, "Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
        )
        self._rate_lock = threading.Lock()
        self._last_request = 0.0
        self.network_requests = 0

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> EdgarFramesAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def frame_period(year: int, instant: bool = False) -> str:
        """``CY2024`` for duration concepts, ``CY2024Q4I`` for instant (balance-sheet) concepts."""
        return f"CY{year}Q4I" if instant else f"CY{year}"

    def frame_url(self, concept: str, period: str, unit: str = "USD") -> str:
        """URL of one XBRL frame."""
        return f"{self.BASE_URL}/us-gaap/{concept}/{unit}/{period}.json"

    def _cache_path(self, concept: str, period: str, unit: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{concept}_{unit}_{period}")
        return self.cache_dir / f"{safe}.json"

    def _throttle(self) -> None:
        with self._rate_lock:
            wait = self._last_request + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def fetch_frame(self, concept: str, period: str, unit: str = "USD") -> dict[str, Any] | None:
        """Frame JSON from cache or network. Returns None (with a warning) on any failure."""
        path = self._cache_path(concept, period, unit)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("EDGAR cache %s unreadable (%s); refetching", path.name, exc)
        url = self.frame_url(concept, period, unit)
        self._throttle()
        try:
            self.network_requests += 1
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("EDGAR fetch failed for %s: %s", url, exc)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            logger.warning("EDGAR frame %s has unexpected shape; ignored", url)
            return None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            logger.warning("EDGAR cache write failed for %s: %s", path, exc)
        return data

    @staticmethod
    def _values_by_cik(frame: Mapping[str, Any]) -> dict[int, float]:
        out: dict[int, float] = {}
        for row in frame.get("data", []):
            if not isinstance(row, dict):
                continue
            try:
                cik = int(row["cik"])
                val = float(row["val"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(val):
                out[cik] = val
        return out

    def ratio_values(
        self,
        concept_numerator: str,
        concept_denominator: str,
        year: int,
        *,
        numerator_instant: bool = False,
        denominator_instant: bool = False,
        multiplier: float = 1.0,
        ciks: Iterable[int] | None = None,
    ) -> NDArray[np.float64] | None:
        """Per-filer ``multiplier * numerator / denominator`` (denominator > 0), or None on failure."""
        num = self.fetch_frame(concept_numerator, self.frame_period(year, numerator_instant))
        den = self.fetch_frame(concept_denominator, self.frame_period(year, denominator_instant))
        if num is None or den is None:
            return None
        nv, dv = self._values_by_cik(num), self._values_by_cik(den)
        allowed = set(int(c) for c in ciks) if ciks is not None else None
        ratios = [
            multiplier * nv[c] / dv[c]
            for c in sorted(set(nv) & set(dv))
            if dv[c] > 0 and (allowed is None or c in allowed)
        ]
        arr = np.array([r for r in ratios if math.isfinite(r)], dtype=float)
        return arr

    def refresh_benchmark(
        self,
        concept_numerator: str,
        concept_denominator: str,
        year: int,
        key: str,
        industry: str = "any",
        *,
        metric: str | None = None,
        unit: str = "ratio",
        multiplier: float = 1.0,
        numerator_instant: bool = False,
        denominator_instant: bool = False,
        distribution: Literal["lognormal", "normal", "beta", "triangular", "empirical"] = "empirical",
        ciks: Iterable[int] | None = None,
    ) -> Benchmark | None:
        """Cross-filer p10..p90 of a concept ratio as a ``Benchmark`` (``source="SEC EDGAR XBRL frames"``).

        Returns None (with a warning) if the frames cannot be fetched or fewer than
        ``min_filers`` filers report both concepts.
        """
        values = self.ratio_values(
            concept_numerator, concept_denominator, year,
            numerator_instant=numerator_instant, denominator_instant=denominator_instant,
            multiplier=multiplier, ciks=ciks,
        )
        if values is None:
            return None
        if values.size < self.min_filers:
            logger.warning("EDGAR %s: only %d filers (< %d); not used", key, values.size, self.min_filers)
            return None
        p10, p25, p50, p75, p90 = (float(x) for x in np.percentile(values, [10, 25, 50, 75, 90]))
        url = self.frame_url(concept_numerator, self.frame_period(year, numerator_instant))
        try:
            return Benchmark(
                key=key, industry=industry,
                metric=metric or f"{concept_numerator} / {concept_denominator}",
                unit=unit, p10=p10, p25=p25, p50=p50, p75=p75, p90=p90,
                distribution=distribution,
                source="SEC EDGAR XBRL frames",
                source_url=url,
                as_of=str(year),
                notes=(
                    f"{values.size} filers; {multiplier:g} * {concept_numerator} "
                    f"({self.frame_period(year, numerator_instant)}) / {concept_denominator} "
                    f"({self.frame_period(year, denominator_instant)}); filers not filtered by SIC unless CIKs given."
                ),
            )
        except ValidationError as exc:
            logger.warning("EDGAR %s produced an invalid benchmark: %s", key, exc)
            return None

    def refresh_from_spec(self, spec: EdgarRefreshSpec, year: int, ciks: Iterable[int] | None = None) -> Benchmark | None:
        """:meth:`refresh_benchmark` driven by an :class:`EdgarRefreshSpec`."""
        return self.refresh_benchmark(
            spec.numerator, spec.denominator, year, spec.key, spec.industry,
            metric=spec.metric, unit=spec.unit, multiplier=spec.multiplier,
            numerator_instant=spec.numerator_instant, denominator_instant=spec.denominator_instant,
            distribution=spec.distribution, ciks=ciks,
        )


# --------------------------------------------------------------------------- ResearchBrief


class ResearchBrief:
    """Assembles the research material a world generator needs for one archetype."""

    @staticmethod
    def hint_for(archetype_key: str) -> ArchetypeHint:
        """Hints for a registered archetype; a generic all-pillar hint otherwise."""
        hint = ARCHETYPE_HINTS.get(archetype_key)
        if hint is None:
            logger.debug("no research hint for archetype %s; using generic", archetype_key)
            return ArchetypeHint(pillars=["accounting_finance", "operations_strategy", "system_design", "sql", "python"])
        return hint

    @staticmethod
    def _matches(text: str, keywords: Sequence[str]) -> bool:
        low = text.lower()
        return any(k.lower() in low for k in keywords)

    @classmethod
    def for_archetype(cls, archetype_key: str, industry_code: str, pool: BenchmarkPool) -> dict[str, Any]:
        """JSON-able brief: ``industry`` (resolved profile), ``benchmark_keys``, ``benchmarks``,
        ``line_items``, ``schema_template``, ``forensic_patterns``, ``citations``, ``missing``.

        Unknown industries degrade to a minimal profile built from benchmarks tagged with
        that code, so a partial corpus never blocks generation.
        """
        hint = cls.hint_for(archetype_key)
        try:
            profile = pool.industry(industry_code)
            missing = [k for k in pool.declared_keys(industry_code) if not pool.has(k)]
        except KeyError:
            logger.warning("industry %s not in corpus; using a minimal profile", industry_code)
            tagged = {b.key: b for b in pool.search(industry=industry_code)}
            by_st = pool.line_items(industry_code)
            profile = IndustryProfile(
                code=industry_code, name=industry_code.replace("_", " ").title(), benchmarks=tagged,
                line_items=[it.caption for st in ("income_statement", "balance_sheet") for it in by_st.get(st, [])],
            )
            missing = []

        keys = set(profile.benchmarks)
        if hint.metric_keywords:
            for b in pool.search(industry=industry_code, include_any=True):
                if cls._matches(f"{b.key} {b.metric}", hint.metric_keywords):
                    keys.add(b.key)
        benchmark_keys = sorted(keys)

        template_key = hint.schema_template or profile.schema_template
        template: dict[str, Any] | None = None
        if template_key:
            try:
                template = pool.schema_template(template_key).model_dump(mode="json")
            except KeyError:
                logger.warning("schema template %s not in corpus", template_key)
                missing.append(f"schema_template:{template_key}")

        pillar_patterns = [p for pillar in hint.pillars for p in pool.patterns_for_pillar(pillar)]
        if hint.pattern_keywords:
            matched = [
                p for p in pillar_patterns
                if cls._matches(f"{p.key} {p.title} {p.description}", hint.pattern_keywords)
            ]
            patterns = matched or pillar_patterns
        else:
            patterns = pillar_patterns
        seen: set[str] = set()
        unique_patterns: list[ForensicPattern] = []
        for pat in patterns:
            if pat.key not in seen:
                seen.add(pat.key)
                unique_patterns.append(pat)

        benchmarks = {k: pool.get(k).model_dump(mode="json") for k in benchmark_keys}
        return {
            "archetype": archetype_key,
            "industry_code": industry_code,
            "pillars": list(hint.pillars),
            "industry": profile.model_dump(mode="json"),
            "benchmark_keys": benchmark_keys,
            "benchmarks": benchmarks,
            "line_items": {st: [it.model_dump(mode="json") for it in items] for st, items in pool.line_items(industry_code).items()},
            "schema_template": template,
            "forensic_patterns": [p.model_dump(mode="json") for p in unique_patterns],
            "citations": [{"key": k, "source": benchmarks[k]["source"], "source_url": benchmarks[k]["source_url"]} for k in benchmark_keys],
            "missing": missing,
        }


# --------------------------------------------------------------------------- default pool + CLI

_default_pool: BenchmarkPool | None = None
_default_lock = threading.Lock()


def get_default_pool(reload: bool = False) -> BenchmarkPool:
    """Process-wide pool loaded from ``Settings.benchmarks_dir`` (cached)."""
    global _default_pool
    with _default_lock:
        if _default_pool is None or reload:
            _default_pool = BenchmarkPool.load()
        return _default_pool


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(prog="python -m axiom.engine.market_research", description="Axiom research tier")
    parser.add_argument("--dir", type=Path, default=None, help="benchmarks directory (default: Settings.benchmarks_dir)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log warnings while loading")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", help="print coverage report and citations")
    p_sample = sub.add_parser("sample", help="draw from a benchmark")
    p_sample.add_argument("key")
    p_sample.add_argument("--seed", type=int, default=0)
    p_sample.add_argument("-n", type=int, default=1, help="number of draws")
    p_refresh = sub.add_parser("refresh", help="refresh live benchmarks from SEC EDGAR XBRL frames (in memory)")
    p_refresh.add_argument("--year", type=int, required=True)
    p_refresh.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    pool = BenchmarkPool.load(args.dir)

    if args.command == "report":
        print(json.dumps({"coverage": pool.coverage_report(), "citations": pool.citations()}, indent=2))
        return 0
    if args.command == "sample":
        if not pool.has(args.key):
            print(f"unknown benchmark {args.key!r}", file=sys.stderr)
            return 2
        rng = SeededRNG(args.seed, name=f"cli:{args.seed}")
        bench = pool.get(args.key)
        draws = [pool.sample(args.key, rng) for _ in range(max(1, args.n))]
        print(json.dumps({"key": args.key, "seed": args.seed, "unit": bench.unit, "distribution": bench.distribution,
                          "percentiles": bench.percentiles(), "draws": draws}, indent=2))
        return 0
    if args.command == "refresh":
        with EdgarFramesAdapter(cache_dir=args.cache_dir) as adapter:
            applied = pool.apply_live_overrides(adapter, DEFAULT_EDGAR_SPECS, args.year)
        print(json.dumps({"year": args.year, "applied": [b.model_dump(mode="json") for b in applied],
                          "requested": [s.key for s in DEFAULT_EDGAR_SPECS]}, indent=2))
        return 0
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
