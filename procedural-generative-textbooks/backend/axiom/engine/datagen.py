"""Relational table builder, dirt injection and DuckDB writer (ARCHITECTURE.md section 8,
archetype_common.md section 8).

Workflow inside an archetype::

    tb = TableBuilder(ctx.rng.spawn("datagen"), pool.schema_template("ecommerce_oms"), difficulty=ctx.difficulty)
    tb.add_frame("customers", customers_df, dirt=["nulls:email:0.08", "case_mixed:region"])
    tb.add_generated("orders", lambda rng: make_orders(rng), primary_key=["order_id"])
    tb.add_ledger_tables(ledger)              # gl_accounts, gl_journal, gl_lines from the Ledger
    tables = tb.materialize()                 # runs generators, applies dirt (recorded per table)
    db_spec = tb.write(db_path, calendar)     # DuckDB + period_calendar + macros + TableSpecs + ERD

Dirt rules (declared per table, column names are table-local) - all deterministic, each driven by
its own child stream ``rng.spawn("dirt:<table>:<rule>")``:

=====================  ========================================================================
``nulls:<col>:<rate>``  set ``rate`` of the column to NULL
``dup_keys:<rate>``     duplicate ``rate`` of rows; copies carry ``ingest_id = 2`` (originals 1)
``tz_mixed:<col>``      timestamps rendered as text, ~half UTC ``+00:00``, rest local with offset
``format_mixed:<col>``  dates as ISO or US ``MM/DD/YYYY``; numbers as ``$1,234.56`` strings
``case_mixed:<col>``    random lower / UPPER / Title case
``whitespace:<col>``    leading / trailing / doubled spaces
``orphan_fk:<col>:<r>`` ``r`` of values replaced by ids that exist in no parent table
``skew:<col>:<alpha>``  numeric: multiplied by mean-1 Pareto(alpha) weights; categorical: Zipf
``late_arriving:<col>`` ~5% of rows get ``<col>`` = ``created_at - U(1h, 72h)``
``unit_mixed:<col>``    ~10% of rows divided by 100 (cents stored as dollars)
=====================  ========================================================================

Every case DB also gets ``period_calendar`` and the macros ``last_day_of_quarter(d)``,
``first_day_of_quarter(d)`` and ``period_of(d)`` (archetypes.md Appendix B.2).
Row budget (Appendix B.1): foundation ~10k, practitioner ~25k, principal <= 50k total rows.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from axiom.engine.finance import FiscalCalendar, Ledger
from axiom.engine.market_research import SchemaTemplate, TemplateTable
from axiom.engine.rng import SeededRNG
from axiom.models import ColumnSpec, DatabaseSpec, Difficulty, TableSpec

__all__ = [
    "ROW_BUDGET",
    "ROW_HARD_CAP",
    "GL_BATCH",
    "DIRT_RULE_NAMES",
    "RowBudgetError",
    "row_scale",
    "apply_dirt",
    "parse_rule",
    "write_duckdb",
    "erd_mermaid",
    "TableDef",
    "TableBuilder",
    "json_safe_records",
]

logger = logging.getLogger("axiom.datagen")

ROW_BUDGET: dict[str, int] = {"foundation": 10_000, "practitioner": 25_000, "principal": 50_000}
ROW_HARD_CAP: dict[str, int] = {"foundation": 13_000, "practitioner": 32_000, "principal": 50_000}
GL_BATCH: dict[str, str] = {"foundation": "weekly", "practitioner": "weekly", "principal": "daily"}
DIRT_RULE_NAMES: tuple[str, ...] = ("nulls", "dup_keys", "tz_mixed", "format_mixed", "case_mixed", "whitespace",
                                    "orphan_fk", "skew", "late_arriving", "unit_mixed")
_TZ_OFFSETS: tuple[tuple[str, int], ...] = (("-05:00", -300), ("-08:00", -480), ("+01:00", 60), ("-06:00", -360))

MACROS_SQL: tuple[str, ...] = (
    "CREATE OR REPLACE MACRO last_day_of_quarter(d) AS "
    "CAST(date_trunc('quarter', CAST(d AS DATE)) + INTERVAL 3 MONTH - INTERVAL 1 DAY AS DATE)",
    "CREATE OR REPLACE MACRO first_day_of_quarter(d) AS CAST(date_trunc('quarter', CAST(d AS DATE)) AS DATE)",
    "CREATE OR REPLACE MACRO period_of(d) AS CAST(year(CAST(d AS DATE)) AS VARCHAR) || 'Q' || CAST(quarter(CAST(d AS DATE)) AS VARCHAR)",
)


class RowBudgetError(RuntimeError):
    """The generated tables exceed the difficulty's row cap."""


def row_scale(difficulty: Difficulty | str) -> float:
    """Volume multiplier relative to the principal budget (foundation 0.2, practitioner 0.5, principal 1.0)."""
    return ROW_BUDGET[difficulty] / ROW_BUDGET["principal"]


# --------------------------------------------------------------------------- dirt


def parse_rule(rule: str) -> tuple[str, str | None, float | None]:
    """``"nulls:email:0.08" -> ("nulls", "email", 0.08)``; ``"dup_keys:0.02" -> ("dup_keys", None, 0.02)``."""
    parts = rule.split(":")
    name = parts[0]
    if name not in DIRT_RULE_NAMES:
        raise ValueError(f"unknown dirt rule {rule!r}")
    if name == "dup_keys":
        return name, None, float(parts[1]) if len(parts) > 1 else 0.02
    if len(parts) < 2:
        raise ValueError(f"dirt rule {rule!r} needs a column")
    rate = float(parts[2]) if len(parts) > 2 else None
    return name, parts[1], rate


def _pick(rng: SeededRNG, n: int, rate: float) -> np.ndarray:
    return rng.generator.random(n) < rate


def _fmt_ts(ts: pd.Series, offset_min: np.ndarray, labels: np.ndarray) -> pd.Series:
    shifted = ts + pd.to_timedelta(offset_min, unit="m")
    base = shifted.dt.strftime("%Y-%m-%d %H:%M:%S")
    return base.str.cat(pd.Series(labels, index=ts.index))


def apply_dirt(df: pd.DataFrame, rules: Sequence[str], rng: SeededRNG, *, table: str = "t") -> tuple[pd.DataFrame, list[str]]:
    """Apply dirt ``rules`` to a copy of ``df``; returns ``(dirty_df, applied_rules)``.

    Rules whose column is missing are skipped with a warning (and not recorded). Representation
    changes only - except ``dup_keys``, which deliberately duplicates rows (copies get
    ``ingest_id = 2``) - so money totals the GL depends on are untouched as long as the archetype
    does not point value-changing rules (``skew``/``unit_mixed``/``nulls``) at GL-tied columns.
    """
    out = df.copy()
    applied: list[str] = []
    for rule in rules:
        name, col, rate = parse_rule(rule)
        r = rng.spawn(f"dirt:{table}:{rule}")
        n = len(out)
        if n == 0:
            continue
        if col is not None and col not in out.columns:
            logger.warning("dirt %s on %s: column %r missing; skipped", rule, table, col)
            continue
        if name == "nulls":
            mask = _pick(r, n, rate if rate is not None else 0.05)
            s = out[col]
            if pd.api.types.is_bool_dtype(s):
                s = s.astype("boolean")
            elif pd.api.types.is_integer_dtype(s):
                s = s.astype("Int64")
            elif not (pd.api.types.is_float_dtype(s) or pd.api.types.is_datetime64_any_dtype(s)):
                s = s.astype(object)
            s = s.mask(pd.Series(mask, index=s.index))
            out[col] = s
        elif name == "dup_keys":
            k = max(1, int(round(n * (rate or 0.02))))
            idx = np.sort(r.generator.choice(n, size=min(k, n), replace=False))
            if "ingest_id" not in out.columns:
                out["ingest_id"] = np.ones(n, dtype=np.int64)
            dup = out.iloc[idx].copy()
            dup["ingest_id"] = 2
            if "updated_at" in dup.columns and pd.api.types.is_datetime64_any_dtype(dup["updated_at"]):
                dup["updated_at"] = dup["updated_at"] + pd.to_timedelta(r.generator.integers(5, 600, len(dup)), unit="m")
            order = np.concatenate([np.arange(n), idx + 0.5])
            out = pd.concat([out, dup], ignore_index=True)
            out = out.iloc[np.argsort(order, kind="stable")].reset_index(drop=True)
        elif name == "tz_mixed":
            ts = pd.to_datetime(out[col])
            local = r.generator.random(n) < 0.5
            choice = r.generator.integers(0, len(_TZ_OFFSETS), n)
            offs = np.where(local, np.array([o[1] for o in _TZ_OFFSETS])[choice], 0)
            labs = np.where(local, np.array([o[0] for o in _TZ_OFFSETS])[choice], "+00:00")
            txt = _fmt_ts(ts, offs, labs)
            out[col] = txt.where(ts.notna(), None).astype(object)
        elif name == "format_mixed":
            s = out[col]
            mask = r.generator.random(n) < 0.4
            if pd.api.types.is_datetime64_any_dtype(s):
                iso = s.dt.strftime("%Y-%m-%d %H:%M:%S") if (s.dt.normalize() != s).any() else s.dt.strftime("%Y-%m-%d")
                us = s.dt.strftime("%m/%d/%Y")
                out[col] = pd.Series(np.where(mask, us, iso), index=s.index).where(s.notna(), None).astype(object)
            elif pd.api.types.is_numeric_dtype(s):
                vals = s.astype(float)
                if col.endswith("_cents"):
                    vals = vals / 100.0
                money = vals.map(lambda v: f"${v:,.2f}" if pd.notna(v) else None)
                plain = s.map(lambda v: str(v) if pd.notna(v) else None)
                out[col] = pd.Series(np.where(mask, money, plain), index=s.index).astype(object)
            else:
                logger.warning("format_mixed on non-date/non-numeric %s.%s skipped", table, col)
                continue
        elif name == "case_mixed":
            s = out[col].astype(object)
            mode = r.generator.integers(0, 4, n)
            vals = [v if not isinstance(v, str) else (v.lower(), v.upper(), v.title(), v)[m] for v, m in zip(s.tolist(), mode, strict=True)]
            out[col] = pd.Series(vals, index=s.index, dtype=object)
        elif name == "whitespace":
            s = out[col].astype(object)
            mode = r.generator.integers(0, 5, n)
            vals = [v if not isinstance(v, str) else (" " + v, v + " ", "  " + v + " ", v.replace(" ", "  "), v)[m]
                    for v, m in zip(s.tolist(), mode, strict=True)]
            out[col] = pd.Series(vals, index=s.index, dtype=object)
        elif name == "orphan_fk":
            mask = _pick(r, n, rate if rate is not None else 0.02)
            s = out[col]
            if pd.api.types.is_numeric_dtype(s):
                repl = -(np.arange(n) + 1)
                out[col] = s.where(~mask, repl)
            else:
                repl = pd.Series([f"ORPHAN-{i:05d}" for i in range(n)], index=s.index)
                out[col] = s.astype(object).where(~mask, repl)
        elif name == "skew":
            alpha = rate if rate is not None else 1.16
            s = out[col]
            if pd.api.types.is_numeric_dtype(s):
                w = 1.0 + r.generator.pareto(alpha, n)
                w = w / w.mean()
                vals = s.astype(float) * w
                out[col] = np.round(vals).astype(s.dtype) if pd.api.types.is_integer_dtype(s) else vals
            else:
                cats = sorted({str(v) for v in s.dropna().tolist()})
                if not cats:
                    continue
                ranks = np.arange(1, len(cats) + 1, dtype=float)
                p = ranks ** (-alpha)
                p /= p.sum()
                pick = r.generator.choice(len(cats), size=n, p=p)
                out[col] = pd.Series(np.array(cats, dtype=object)[pick], index=s.index).where(s.notna(), None)
        elif name == "late_arriving":
            if "created_at" not in out.columns:
                logger.warning("late_arriving on %s needs created_at; skipped", table)
                continue
            mask = _pick(r, n, rate if rate is not None else 0.05)
            created = pd.to_datetime(out["created_at"])
            back = pd.to_timedelta(r.generator.integers(60, 72 * 60, n), unit="m")
            out[col] = pd.to_datetime(out[col]).where(~mask, created - back)
        elif name == "unit_mixed":
            mask = _pick(r, n, rate if rate is not None else 0.10)
            s = out[col].astype(float)
            out[col] = s.where(~mask, s / 100.0)
        applied.append(rule)
    return out, applied


# --------------------------------------------------------------------------- DuckDB


def json_safe_records(df: pd.DataFrame, limit: int = 5) -> list[dict[str, Any]]:
    """First ``limit`` rows as JSON-safe dicts (timestamps -> ISO strings, NaN -> None)."""
    rows: list[dict[str, Any]] = []
    for rec in df.head(limit).to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for k, v in rec.items():
            if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT or (not isinstance(v, (str, bytes, list, dict)) and pd.isna(v)):
                clean[str(k)] = None
            elif isinstance(v, pd.Timestamp):
                clean[str(k)] = v.isoformat(sep=" ")
            elif isinstance(v, (np.integer,)):
                clean[str(k)] = int(v)
            elif isinstance(v, (np.floating, float)):
                clean[str(k)] = float(v)
            elif isinstance(v, (np.bool_, bool)):
                clean[str(k)] = bool(v)
            elif hasattr(v, "isoformat"):
                clean[str(k)] = v.isoformat()
            else:
                clean[str(k)] = v if isinstance(v, (int, str)) else str(v)
        rows.append(clean)
    return rows


def _sql_type_ok(series: pd.Series, sql_type: str) -> bool:
    t = sql_type.upper()
    if t.startswith(("VARCHAR", "TEXT")):
        return True
    if pd.api.types.is_datetime64_any_dtype(series):
        return t.startswith(("DATE", "TIMESTAMP"))
    if pd.api.types.is_bool_dtype(series) or str(series.dtype) == "boolean":
        return t.startswith("BOOL")
    if pd.api.types.is_numeric_dtype(series):
        return t.startswith(("INT", "BIGINT", "DECIMAL", "DOUBLE", "FLOAT", "REAL", "NUMERIC", "SMALLINT", "HUGEINT"))
    return False


def _create_table(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame, types: Mapping[str, str]) -> None:
    con.register("__axiom_df", df)
    try:
        cols = []
        for c in df.columns:
            qc = f'"{c}"'
            t = types.get(c)
            if t and _sql_type_ok(df[c], t):
                cols.append(f"CAST({qc} AS {t}) AS {qc}")
            elif df[c].dtype == object or str(df[c].dtype).startswith(("str", "string")):
                cols.append(f"CAST({qc} AS VARCHAR) AS {qc}")
            else:
                cols.append(qc)
        con.execute(f'CREATE TABLE "{name}" AS SELECT {", ".join(cols)} FROM __axiom_df')
    finally:
        con.unregister("__axiom_df")


def write_duckdb(path: str | Path, tables: Mapping[str, pd.DataFrame], *, calendar: FiscalCalendar | None = None,
                 column_types: Mapping[str, Mapping[str, str]] | None = None) -> None:
    """Write ``tables`` to a fresh DuckDB file at ``path`` (replacing any existing file), plus
    ``period_calendar`` (when ``calendar`` is given) and the quarter macros."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".wal"):
        q = Path(str(p) + suffix)
        if q.exists():
            q.unlink()
    con = duckdb.connect(str(p))
    try:
        for stmt in MACROS_SQL:
            con.execute(stmt)
        if calendar is not None and "period_calendar" not in tables:
            _create_table(con, "period_calendar", calendar.calendar_frame(),
                          {"start_date": "DATE", "end_date": "DATE", "period_index": "INTEGER", "fiscal_year": "INTEGER",
                           "fiscal_quarter": "INTEGER", "days": "INTEGER", "is_reporting": "BOOLEAN"})
        for name, df in tables.items():
            _create_table(con, name, df, (column_types or {}).get(name, {}))
        con.execute("CHECKPOINT")
    finally:
        con.close()


def _mermaid_type(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", t.split("(")[0].replace(" ", "_")) or "VARCHAR"


def erd_mermaid(specs: Sequence[TableSpec]) -> str:
    """``erDiagram`` with every table, its columns (PK/FK marked) and FK relationships."""
    names = {s.name for s in specs}
    lines = ["erDiagram"]
    for s in specs:
        fk_cols = {fk[0] for fk in s.foreign_keys}
        lines.append(f"  {s.name} {{")
        for c in s.columns:
            mark = " PK" if c.name in s.primary_key else (" FK" if c.name in fk_cols else "")
            lines.append(f"    {_mermaid_type(c.type)} {c.name}{mark}")
        lines.append("  }")
    for s in specs:
        for col, ref_table, ref_col in s.foreign_keys:
            if ref_table in names:
                lines.append(f'  {ref_table} ||--o{{ {s.name} : "{col} -> {ref_col}"')
    return "\n".join(lines)


# --------------------------------------------------------------------------- builder


@dataclass
class TableDef:
    """A table registered with :class:`TableBuilder` (frame or generator plus metadata)."""

    name: str
    frame: pd.DataFrame | None = None
    generator: Callable[[SeededRNG], pd.DataFrame] | None = None
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)
    column_types: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    nullable: dict[str, bool] = field(default_factory=dict)
    dirt: list[str] = field(default_factory=list)
    description: str = ""
    applied_dirt: list[str] = field(default_factory=list)


class TableBuilder:
    """Collects tables, applies dirt deterministically, writes DuckDB and builds the
    :class:`~axiom.models.DatabaseSpec`.

    Metadata precedence per table: explicit arguments > ``schema_template`` table of the same name
    (PK, FKs, column descriptions/types/nullability) > inferred. Tables not in the template (e.g.
    ``invoices`` on ``ecommerce_oms``) are fine - archetypes extend templates.
    """

    def __init__(self, rng: SeededRNG, schema_template: SchemaTemplate | None, *, difficulty: Difficulty | str = "practitioner",
                 extra_tables: Sequence[TemplateTable] = ()) -> None:
        """Create a builder over ``schema_template`` (may be ``None``)."""
        self.rng = rng
        self.template = schema_template
        self.difficulty = str(difficulty)
        self._tmpl: dict[str, TemplateTable] = {t.name: t for t in (schema_template.tables if schema_template else [])}
        self._tmpl.update({t.name: t for t in extra_tables})
        self.tables: dict[str, TableDef] = {}
        self._materialized: dict[str, pd.DataFrame] | None = None

    # ------------------------------------------------------------------ registration

    def _register(self, name: str, *, frame: pd.DataFrame | None, generator: Callable[[SeededRNG], pd.DataFrame] | None,
                  primary_key: Sequence[str] | None, foreign_keys: Sequence[tuple[str, str, str]] | None,
                  column_types: Mapping[str, str] | None, descriptions: Mapping[str, str] | None,
                  dirt: Sequence[str], description: str | None) -> TableDef:
        if name in self.tables:
            raise ValueError(f"table {name!r} already registered")
        for rule in dirt:
            parse_rule(rule)
        t = self._tmpl.get(name)
        types = {c.name: c.type for c in t.columns} if t else {}
        types.update(column_types or {})
        descs = {c.name: c.description for c in t.columns if c.description} if t else {}
        descs.update(descriptions or {})
        td = TableDef(
            name=name, frame=frame, generator=generator,
            primary_key=list(primary_key) if primary_key is not None else (list(t.primary_key) if t else []),
            foreign_keys=[tuple(fk) for fk in foreign_keys] if foreign_keys is not None else ([tuple(fk) for fk in t.foreign_keys] if t else []),  # type: ignore[misc]
            column_types=types, descriptions=descs,
            nullable={c.name: c.nullable for c in t.columns} if t else {},
            dirt=list(dirt), description=description if description is not None else (t.description if t else ""),
        )
        self.tables[name] = td
        self._materialized = None
        return td

    def add_frame(self, name: str, df: pd.DataFrame, *, primary_key: Sequence[str] | None = None,
                  foreign_keys: Sequence[tuple[str, str, str]] | None = None, column_types: Mapping[str, str] | None = None,
                  descriptions: Mapping[str, str] | None = None, dirt: Sequence[str] = (), description: str | None = None) -> TableDef:
        """Register an already-built DataFrame."""
        return self._register(name, frame=df, generator=None, primary_key=primary_key, foreign_keys=foreign_keys,
                              column_types=column_types, descriptions=descriptions, dirt=dirt, description=description)

    def add_generated(self, name: str, generator: Callable[[SeededRNG], pd.DataFrame], *, primary_key: Sequence[str] | None = None,
                      foreign_keys: Sequence[tuple[str, str, str]] | None = None, column_types: Mapping[str, str] | None = None,
                      descriptions: Mapping[str, str] | None = None, dirt: Sequence[str] = (), description: str | None = None) -> TableDef:
        """Register a generator ``fn(rng) -> DataFrame``; it runs in :meth:`materialize` with its own
        child stream ``rng.spawn("table:<name>")``."""
        return self._register(name, frame=None, generator=generator, primary_key=primary_key, foreign_keys=foreign_keys,
                              column_types=column_types, descriptions=descriptions, dirt=dirt, description=description)

    def add_ledger_tables(self, ledger: Ledger, *, dirt_journal: Sequence[str] = ()) -> None:
        """Register ``gl_accounts``, ``gl_journal`` and ``gl_lines`` mirroring the ledger row-for-row
        (money in integer cents: ``debit_cents``/``credit_cents``)."""
        accounts = pd.DataFrame([{"account_code": a.code, "account_name": a.name, "account_type": a.type,
                                  "normal_balance": a.normal_balance, "statement": a.statement, "section": a.section,
                                  "parent_code": a.parent} for a in ledger.chart])
        journal, lines = ledger.entries_frame()
        self.add_frame("gl_accounts", accounts, primary_key=["account_code"], foreign_keys=[],
                       description="Chart of accounts (canonical).")
        self.add_frame("gl_journal", journal, primary_key=["entry_id"], foreign_keys=[], column_types={"entry_date": "DATE"},
                       descriptions={"source": "event family / batch source (sales, collections, purchasing, payroll, opening, ...)"},
                       dirt=dirt_journal, description="General-ledger journal headers; mirrors FinanceState.journal.")
        self.add_frame("gl_lines", lines, primary_key=["line_id"],
                       foreign_keys=[("entry_id", "gl_journal", "entry_id"), ("account_code", "gl_accounts", "account_code")],
                       column_types={"line_id": "BIGINT", "debit_cents": "BIGINT", "credit_cents": "BIGINT"},
                       descriptions={"debit_cents": "debit amount in cents", "credit_cents": "credit amount in cents",
                                     "dimension_invoice_id": "invoice id dimension of the line (NULL if none)",
                                     "dimension_sku": "SKU dimension of the line (NULL if none)",
                                     "dimensions": "all line dimensions as key=value;... (batch ids, row counts)"},
                       description="General-ledger lines; signed balance = SUM(debit_cents - credit_cents).")

    def add_dirt(self, name: str, rules: Sequence[str]) -> None:
        """Append dirt ``rules`` to a registered table (re-materialised on the next :meth:`materialize`). Each rule
        draws from its own ``dirt:<table>:<rule>`` stream, so earlier rules are unaffected."""
        for rule in rules:
            parse_rule(rule)
        self.tables[name].dirt.extend(rules)
        self._materialized = None

    def remove_dirt(self, name: str, rules: Sequence[str]) -> None:
        """Remove previously added ``rules`` from a table."""
        td = self.tables[name]
        td.dirt = [r for r in td.dirt if r not in set(rules)]
        self._materialized = None

    def clean_frame(self, name: str) -> pd.DataFrame:
        """The clean (pre-dirt) frame of a registered table."""
        td = self.tables[name]
        return td.frame if td.frame is not None else td.generator(self.rng.spawn(f"table:{name}"))  # type: ignore[misc]

    # ------------------------------------------------------------------ build

    def materialize(self) -> dict[str, pd.DataFrame]:
        """Run generators and apply each table's dirt (recorded in ``TableDef.applied_dirt``)."""
        if self._materialized is not None:
            return self._materialized
        out: dict[str, pd.DataFrame] = {}
        for name, td in self.tables.items():
            df = td.frame if td.frame is not None else td.generator(self.rng.spawn(f"table:{name}"))  # type: ignore[misc]
            if not isinstance(df, pd.DataFrame):
                raise TypeError(f"table {name}: generator must return a DataFrame")
            dirty, applied = apply_dirt(df, td.dirt, self.rng, table=name) if td.dirt else (df, [])
            td.applied_dirt = applied
            out[name] = dirty.reset_index(drop=True)
        self._materialized = out
        return out

    def total_rows(self, include_calendar: bool = True) -> int:
        """Total rows over materialized tables (+ ``period_calendar``)."""
        tables = self.materialize()
        return sum(len(df) for df in tables.values()) + (9 if include_calendar else 0)

    def check_budget(self, calendar_rows: int = 0) -> int:
        """Raise :class:`RowBudgetError` if total rows exceed the difficulty cap; returns the total."""
        total = sum(len(df) for df in self.materialize().values()) + calendar_rows
        cap = ROW_HARD_CAP.get(self.difficulty, ROW_HARD_CAP["principal"])
        if total > cap:
            raise RowBudgetError(f"{total} rows exceed the {self.difficulty} cap of {cap} "
                                 f"({', '.join(f'{k}={len(v)}' for k, v in self.materialize().items())})")
        return total

    def write(self, path: str | Path, calendar: FiscalCalendar, *, rel_path: str | None = None) -> DatabaseSpec:
        """Write the DuckDB file, then build ``TableSpec``s (DDL via ``DESCRIBE``), ERD and totals."""
        tables = self.materialize()
        cal_rows = len(calendar.calendar_frame())
        total = self.check_budget(cal_rows)
        types = {n: td.column_types for n, td in self.tables.items()}
        write_duckdb(path, tables, calendar=calendar, column_types=types)
        con = duckdb.connect(str(path), read_only=True)
        specs: list[TableSpec] = []
        try:
            all_names = ["period_calendar", *tables]
            for name in all_names:
                desc = con.execute(f'DESCRIBE "{name}"').fetchall()
                td = self.tables.get(name)
                df = tables[name] if name in tables else calendar.calendar_frame()
                cols = []
                for row in desc:
                    cname, ctype = str(row[0]), str(row[1])
                    nullable = bool(df[cname].isna().any()) if cname in df.columns else True
                    if td is not None and cname in td.nullable:
                        nullable = nullable or td.nullable[cname]
                    cols.append(ColumnSpec(name=cname, type=ctype, nullable=nullable,
                                           description=(td.descriptions.get(cname, "") if td else _CAL_DESC.get(cname, ""))))
                ddl = f'CREATE TABLE {name} (\n' + ",\n".join(f"  {c.name} {c.type}{'' if c.nullable else ' NOT NULL'}" for c in cols)
                pk = td.primary_key if td else ["period"]
                if pk:
                    ddl += f",\n  -- logical primary key: ({', '.join(pk)})"
                ddl += "\n);"
                specs.append(TableSpec(
                    name=name, ddl=ddl, columns=cols, row_count=len(df), primary_key=list(pk),
                    foreign_keys=[tuple(fk) for fk in (td.foreign_keys if td else [])],  # type: ignore[misc]
                    dirt=list(td.applied_dirt) if td else [], sample_rows=json_safe_records(df, 5),
                ))
        finally:
            con.close()
        return DatabaseSpec(engine="duckdb", path=rel_path or str(path), tables=specs, erd_mermaid=erd_mermaid(specs), total_rows=total)


_CAL_DESC = {
    "period": "quarter label YYYYQn", "period_index": "1..8 for reporting quarters P1..P8; <= 0 for warm-up history",
    "is_reporting": "true for P1..P8", "start_date": "first day of quarter", "end_date": "last day of quarter",
    "fiscal_year": "fiscal year (FYE December)", "fiscal_quarter": "1..4", "days": "days in quarter",
}
