"""Table builder, dirt rules and the DuckDB writer (ARCHITECTURE section 8)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from axiom.engine.datagen import (
    RowBudgetError,
    TableBuilder,
    apply_dirt,
    erd_mermaid,
    parse_rule,
    write_duckdb,
)
from axiom.engine.finance import FiscalCalendar, Ledger
from axiom.engine.market_research import get_default_pool
from axiom.engine.rng import SeededRNG

N = 400


def _frame() -> pd.DataFrame:
    g = np.random.default_rng(0)
    created = pd.Timestamp("2024-01-01") + pd.to_timedelta(g.integers(0, 90 * 86400, N), unit="s")
    return pd.DataFrame({
        "invoice_id": [f"I{i:04d}" for i in range(N)],
        "customer_id": [f"C{i % 40:03d}" for i in range(N)],
        "region": g.choice(["North East", "South", "West"], N),
        "email": [f"x{i}@example.com" for i in range(N)],
        "amount_cents": g.integers(1000, 100000, N),
        "qty": g.integers(1, 20, N),
        "created_at": created,
        "updated_at": created + pd.Timedelta(hours=5),
        "issued": created.normalize(),
    })


RULES = ["nulls:email:0.1", "dup_keys:0.05", "tz_mixed:created_at", "format_mixed:issued", "case_mixed:region", "whitespace:customer_id",
         "orphan_fk:customer_id:0.05", "skew:qty:1.5", "late_arriving:updated_at", "unit_mixed:amount_cents"]


@pytest.mark.parametrize("rule", RULES)
def test_each_dirt_rule_is_deterministic_and_recorded(rule: str) -> None:
    df = _frame()
    a, applied_a = apply_dirt(df, [rule], SeededRNG(9), table="t")
    b, applied_b = apply_dirt(df, [rule], SeededRNG(9), table="t")
    assert applied_a == applied_b == [rule]
    pd.testing.assert_frame_equal(a, b)
    c, _ = apply_dirt(df, [rule], SeededRNG(10), table="t")
    assert not a.equals(c)  # a different stream gives different dirt
    name, col, rate = parse_rule(rule)
    if name == "nulls":
        assert 0.04 < a["email"].isna().mean() < 0.2
    elif name == "dup_keys":
        assert len(a) == N + 20 and (a["ingest_id"] == 2).sum() == 20 and a["invoice_id"].duplicated().sum() == 20
    elif name == "tz_mixed":
        txt = a["created_at"]
        assert txt.str.endswith("+00:00").any() and txt.str.contains(r"[-+]0[5-8]:00$|\+01:00$").any()
        parsed = pd.to_datetime(txt, format="%Y-%m-%d %H:%M:%S%z", utc=True).dt.tz_convert(None)
        assert (parsed == df["created_at"]).all()  # same instants, different representation
    elif name == "format_mixed":
        assert a["issued"].str.match(r"\d{2}/\d{2}/\d{4}$").any() and a["issued"].str.match(r"\d{4}-\d{2}-\d{2}$").any()
    elif name == "case_mixed":
        assert a["region"].str.lower().nunique() == df["region"].nunique() < a["region"].nunique()
    elif name == "whitespace":
        assert (a["customer_id"] != a["customer_id"].str.strip()).any() and (a["customer_id"].str.strip() == df["customer_id"]).all()
    elif name == "orphan_fk":
        orphans = ~a["customer_id"].isin(df["customer_id"])
        assert 0 < orphans.mean() < 0.15
    elif name == "skew":
        assert a["qty"].max() > df["qty"].max()
    elif name == "late_arriving":
        assert (a["updated_at"] < a["created_at"]).any()
    elif name == "unit_mixed":
        assert (a["amount_cents"] < df["amount_cents"]).any()


def test_unknown_rule_rejected_and_missing_column_skipped() -> None:
    with pytest.raises(ValueError):
        parse_rule("explode:col")
    out, applied = apply_dirt(_frame(), ["nulls:nope:0.1"], SeededRNG(1))
    assert applied == [] and len(out) == N


def test_duckdb_written_with_calendar_and_macros(tmp_path: Path) -> None:
    cal = FiscalCalendar.from_seed(4)
    path = tmp_path / "company.duckdb"
    write_duckdb(path, {"t": _frame()}, calendar=cal, column_types={"t": {"issued": "DATE", "amount_cents": "BIGINT"}})
    con = duckdb.connect(str(path), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM period_calendar").fetchone()[0] == 9
        assert con.execute("SELECT last_day_of_quarter(DATE '2024-05-03')").fetchone()[0] == dt.date(2024, 6, 30)
        assert con.execute("SELECT period_of(TIMESTAMP '2024-11-30 10:00:00')").fetchone()[0] == "2024Q4"
        assert con.execute("SELECT first_day_of_quarter(DATE '2024-02-29')").fetchone()[0] == dt.date(2024, 1, 1)
        types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 't'").fetchall())
        assert types["issued"] == "DATE" and types["amount_cents"] == "BIGINT"
    finally:
        con.close()


def test_table_builder_specs_erd_and_gl(tmp_path: Path) -> None:
    pool = get_default_pool()
    cal = FiscalCalendar.from_seed(2)
    L = Ledger()
    L.open_books(cal.opening_date, {"1000": 500, "3000": -500})
    L.sell_on_credit(cal.start, 100)
    tb = TableBuilder(SeededRNG(3), pool.schema_template("ecommerce_oms"), difficulty="foundation")
    tb.add_frame("customers", pd.DataFrame({"customer_id": ["C1", "C2"], "region": ["West", "South"], "email": ["a@x", "b@x"],
                                            "created_at": pd.to_datetime(["2024-01-01", "2024-01-02"])}), dirt=["case_mixed:region"])
    tb.add_generated("orders", lambda rng: pd.DataFrame({"order_id": ["O1", "O2", "O3"], "customer_id": ["C1", "C2", "C1"],
                                                         "total_cents": rng.integers(1, 100, 3)}))
    tb.add_ledger_tables(L)
    db = tb.write(tmp_path / "company.duckdb", cal, rel_path="cases/x/company.duckdb")
    specs = {t.name: t for t in db.tables}
    assert set(specs) == {"period_calendar", "customers", "orders", "gl_accounts", "gl_journal", "gl_lines"}
    assert specs["orders"].foreign_keys == [("customer_id", "customers", "customer_id")]  # from the template
    assert specs["customers"].dirt == ["case_mixed:region"] and specs["orders"].primary_key == ["order_id"]
    assert specs["gl_lines"].row_count == 4 and "CREATE TABLE gl_lines" in specs["gl_lines"].ddl
    assert len(specs["orders"].sample_rows) == 3 and db.total_rows == sum(t.row_count for t in db.tables)
    assert db.erd_mermaid.startswith("erDiagram") and "customers ||--o{ orders" in db.erd_mermaid
    assert erd_mermaid(db.tables) == db.erd_mermaid
    con = duckdb.connect(str(tmp_path / "company.duckdb"), read_only=True)
    try:
        bal = con.execute("SELECT SUM(debit_cents - credit_cents) FROM gl_lines WHERE account_code = '1100'").fetchone()[0]
        assert bal == 100
    finally:
        con.close()


def test_row_budget_enforced(tmp_path: Path) -> None:
    tb = TableBuilder(SeededRNG(1), None, difficulty="foundation")
    tb.add_frame("big", pd.DataFrame({"x": np.arange(14_000)}))
    with pytest.raises(RowBudgetError):
        tb.write(tmp_path / "db.duckdb", FiscalCalendar.from_seed(1))
