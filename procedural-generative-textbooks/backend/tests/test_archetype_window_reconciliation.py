"""window_reconciliation: every designed break recovered by SQL, declared exactly, and the items net to the difference."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest

from axiom.engine.archetypes.base import run_reference_query
from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "window_reconciliation"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("recon")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}")
            for d in DIFFICULTIES for s in SEEDS}


def _m(world: CaseWorld) -> dict[str, float]:
    return {x.key: x.value for x in world.ground_truth.expected_metrics}


def _q(world: CaseWorld, name: str):
    return next(q for a in world.ground_truth.anomalies for q in a.reference_sql if q.name == name)


def _anomaly(world: CaseWorld, kind: str):
    return next(a for a in world.ground_truth.anomalies if a.kind == kind)


def _items(world: CaseWorld) -> dict[str, float]:
    """Itemised reconciling items (former asks, now ``checks`` on the roll-forward reference query, review P1 #14)."""
    chk = _q(world, "ar_rollforward_vs_gl_running_totals").expected["checks"]
    return {k: v for k, v in chk.items() if k.startswith("reconciling_item_")}


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_reconciling_items_net_to_the_difference_and_breaks_are_declared(worlds: dict, difficulty: str, seed: int) -> None:
    world, _ = worlds[(difficulty, seed)]
    m = _m(world)
    P8 = world.periods[-1]
    items = _items(world)
    chk = _q(world, "ar_rollforward_vs_gl_running_totals").expected["checks"]
    assert len(items) == {"foundation": 2, "practitioner": 3, "principal": 4}[difficulty]
    # GL - clean sub-ledger is fully explained by the itemised reconciling items (to the cent)
    assert sum(items.values()) == pytest.approx(m["unexplained_difference_cents"], abs=1)
    assert chk["reconciling_items_total_cents"] == pytest.approx(m["unexplained_difference_cents"], abs=1)
    assert m["gl_ar_close_p8_cents"] - m["subledger_ar_deduped_cents"] == pytest.approx(m["unexplained_difference_cents"], abs=1)
    assert chk["subledger_ar_naive_p8_cents"] - m["subledger_ar_deduped_cents"] == pytest.approx(m["duplicate_invoice_cents"], abs=1)
    # the invariant checker verified each designed break at its declared amount
    for tie, want in (("ar_subledger_deduped_vs_gl", -m["unexplained_difference_cents"]),
                      ("ar_subledger_naive_vs_gl", -m["unexplained_difference_cents"] + m["duplicate_invoice_cents"])):
        c = next(x for x in world.finance.invariants if x.name == f"subledger:{tie}[{P8}]")
        assert c.passed and "designed break" in c.detail and f"(expected {int(round(want))})" in c.detail
    # suspense (the interface clearing credit) holds exactly the GL-side omission
    om = next(a for a in world.ground_truth.anomalies if a.kind in ("credit_memo_unposted", "writeoff_one_sided"))
    assert m["suspense_balance_p8_cents"] > 0
    assert m["suspense_balance_p8_cents"] == pytest.approx(items[f"reconciling_item_{om.kind}_cents"], abs=1)
    assert om.reference_sql[0].expected["checks"][f"reconciling_item_{om.kind}_cents"] == pytest.approx(m["suspense_balance_p8_cents"], abs=1)
    assert all(c.passed for c in world.finance.invariants)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_sql_recovers_each_break(worlds: dict, difficulty: str) -> None:
    for seed in SEEDS:
        world, folder = worlds[(difficulty, seed)]
        m = _m(world)
        P8 = world.periods[-1]
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            dup_rows, dup_cents = con.execute(
                "SELECT COUNT(*), SUM(amount_cents) FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) AS rn FROM invoices) WHERE rn > 1"
            ).fetchone()
            assert dup_rows == _q(world, "duplicate_invoice_loads").expected["checks"]["duplicate_rows"] >= 3 and dup_cents == m["duplicate_invoice_cents"]
            # DISTINCT does not remove the reloaded rows (they differ in ingest_id / created_at) - socratic trap 5
            n_rows = con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            n_distinct = con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT * FROM invoices)").fetchone()[0]
            assert n_distinct == n_rows
            unapplied = con.execute("SELECT SUM(amount_cents) FROM payments WHERE invoice_id IS NULL").fetchone()[0]
            assert unapplied == m["unapplied_cash_cents"] == -_items(world)["reconciling_item_unapplied_cash_cents"]
            assert _q(world, "unapplied_cash_by_quarter").expected["checks"]["reconciling_item_unapplied_cash_cents"] == -unapplied
            susp = con.execute("SELECT SUM(credit_cents - debit_cents) FROM gl_lines "
                               "WHERE account_code = '2100' AND dimensions LIKE '%clearing=billing_interface%'").fetchone()[0]
            assert susp == m["suspense_balance_p8_cents"] > 0
            assert con.execute("SELECT COUNT(*) FROM gl_lines WHERE account_code = '1400'").fetchone()[0] == 0
            gl_ar = con.execute("SELECT SUM(debit_cents - credit_cents) FROM gl_lines WHERE account_code = '1100'").fetchone()[0]
            assert gl_ar == m["gl_ar_close_p8_cents"]
        finally:
            con.close()
        roll = _q(world, "ar_rollforward_vs_gl_running_totals").expected["checks"]
        assert roll["difference_p8"] == pytest.approx(m["unexplained_difference_cents"], abs=5)
        assert roll["subledger_close_p8"] == pytest.approx(m["subledger_ar_deduped_cents"], abs=5)
        assert roll["invoiced_p8"] > roll["collected_p8"] * 0.5 > 0
        if difficulty != "foundation":
            cut = _q(world, "invoices_missing_from_gl").expected["checks"]
            assert cut["cutoff_items_cents"] == cut["missing_p8_cents"] == -_items(world)["reconciling_item_cutoff_shift_cents"] > 0
            assert _q(world, "invoice_period_vs_gl_period").expected["checks"]["shifted_invoices"] >= 2
        if difficulty == "principal":
            assert m["reconciling_item_fx_rounding_cents"] == pytest.approx(_items(world)["reconciling_item_fx_rounding_cents"], abs=1)
            assert _q(world, "invoice_totals_by_currency").expected["checks"]["currencies"] >= 2
        assert P8 in {r[0] for r in _q(world, "ar_rollforward_vs_gl_running_totals").expected["rows"]}


@pytest.mark.parametrize("seed", SEEDS)
def test_break_sizes_match_sampled_magnitudes(worlds: dict, seed: int) -> None:
    for d in DIFFICULTIES:
        world, folder = worlds[(d, seed)]
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            ua = _anomaly(world, "unapplied_cash")
            target = ua.magnitude["target_cents"]
            amounts = [r[0] for r in con.execute("SELECT amount_cents FROM payments WHERE invoice_id IS NULL").fetchall()]
            # greedy selection: the items reach the sampled target and stop at the first item that crosses it (min 2 items)
            assert sum(amounts) >= target or len(amounts) == 2
            assert sum(amounts) - max(amounts) < target or len(amounts) == 2
            om = next(a for a in world.ground_truth.anomalies if a.kind in ("credit_memo_unposted", "writeoff_one_sided"))
            missing = om.reference_sql[0].expected["checks"]
            assert missing["amount_cents"] == om.magnitude["break_cents_p8"] * -1 and missing["items"] >= 1
            assert om.magnitude["break_cents_p8"] < 0
        finally:
            con.close()


def test_difficulty_scaling(worlds: dict) -> None:
    for s in SEEDS:
        kinds = {d: [a.kind for a in worlds[(d, s)][0].ground_truth.anomalies] for d in DIFFICULTIES}
        assert len(kinds["foundation"]) == 3 < len(kinds["practitioner"]) == 4 < len(kinds["principal"]) == 5
        assert "cutoff_shift" in kinds["practitioner"] and "fx_rounding" in kinds["principal"]
        mf = worlds[("foundation", s)][0].ground_truth.anomalies[1].magnitude["break_magnitude_pct_of_closing_ar"]
        mp = worlds[("principal", s)][0].ground_truth.anomalies[1].magnitude["break_magnitude_pct_of_closing_ar"]
        assert mf >= mp  # foundation draws the upper quartile of U(0.3%, 2.5%), principal the lower


def test_timezone_trap_moves_payments_across_quarters(worlds: dict) -> None:
    moved_total = 0
    for s in SEEDS:
        for d in ("practitioner", "principal"):
            world, folder = worlds[(d, s)]
            con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
            try:
                typ = con.execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'payments' AND column_name = 'paid_at'").fetchone()[0]
                assert typ == "VARCHAR"  # tz_mixed dirt applied
                moved = con.execute("""
                    SELECT COUNT(*) FROM payments
                    WHERE period_of(CAST(LEFT(paid_at, 10) AS DATE)) <> period_of(CAST(CAST(paid_at AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS DATE))""").fetchone()[0]
            finally:
                con.close()
            moved_total += moved
    assert moved_total > 0


def test_red_herrings_are_plausible(worlds: dict) -> None:
    for s in SEEDS:
        world, _ = worlds[("principal", s)]
        items = {k: abs(v) for k, v in _items(world).items()}
        # "FX explains it": FX differences exist but are small next to the structural items
        assert 0 < items["reconciling_item_fx_rounding_cents"] < 0.1 * sum(items.values())
        # "the GL is wrong": GL revenue equals the invoices it received, the breaks sit in the sub-ledger / interface
        rev_checks = [c for c in world.finance.invariants if c.name.startswith("subledger:revenue_invoices_vs_gl")]
        assert all(c.passed for c in rev_checks)
        assert all("designed break" not in c.detail for c in rev_checks[:6])


def test_aging_from_clean_open_items(worlds: dict) -> None:
    for (d, s), (world, _) in worlds.items():
        m = _m(world)
        aging = _q(world, "ar_aging_p8_deduplicated").expected["checks"]
        # open (positive) items of the deduplicated sub-ledger; credit balances from memos after payment are excluded
        assert sum(aging.values()) >= m["subledger_ar_deduped_cents"] * 0.98
        assert aging["aging_90_plus_p8_cents"] == pytest.approx(m["aging_bucket_90_plus_p8_cents"], rel=0.01)
        assert m["aging_bucket_90_plus_p8_cents"] > 0 and aging["aging_0_30_p8_cents"] > m["aging_bucket_90_plus_p8_cents"]


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_asks_are_few_and_the_rest_are_checks(worlds: dict, difficulty: str) -> None:
    """Review P1 #14: 7 asks (8 at principal, + the currency difference); the other figures live in reference-query checks."""
    want = {"gl_ar_close_p8_cents", "subledger_ar_deduped_cents", "unexplained_difference_cents", "duplicate_invoice_cents",
            "unapplied_cash_cents", "suspense_balance_p8_cents", "aging_bucket_90_plus_p8_cents"}
    if difficulty == "principal":
        want.add("reconciling_item_fx_rounding_cents")
    for seed in SEEDS:
        world, _ = worlds[(difficulty, seed)]
        assert set(_m(world)) == want
        keys = {k for a in world.ground_truth.anomalies for q in a.reference_sql for k in q.expected["metric_keys"]}
        assert keys == want  # every ask is linked to a reference query (grader signature matching)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_no_balance_sheet_line_is_negative(worlds: dict, difficulty: str) -> None:
    """The suspense credit sits in accrued liabilities (2100 clearing lines), not in 1400 inside the prepaid caption."""
    for seed in SEEDS:
        world, _ = worlds[(difficulty, seed)]
        for bs in world.finance.statements.balance:
            neg = [(ln.caption, ln.cents) for ln in bs.lines if ln.cents < 0 and "earnings" not in ln.caption.lower()]
            assert not neg, (seed, bs.period, neg)
        assert all(world.finance.trial_balance[p]["1400"] == 0 for p in world.periods)


_TYPED_INVOICE = re.compile(r"\b(?:(\w+)\.)?dimension_invoice_id\b")


def _regexp_variant(sql: str) -> str:
    """The pre-typed-column formulation: extract the invoice id from ``gl_lines.dimensions``."""
    return _TYPED_INVOICE.sub(lambda m: f"NULLIF(regexp_extract({m.group(1) + '.' if m.group(1) else ''}dimensions, "
                                        f"'invoice_id=([^;]+)', 1), '')", sql)


def test_typed_invoice_dimension_matches_regexp_extract(worlds: dict) -> None:
    """``gl_lines.dimension_invoice_id`` is a drop-in for parsing ``dimensions``: every reference query that
    uses the typed column returns exactly its recorded ``expected`` under the regexp formulation too."""
    checked = 0
    for (d, s), (world, folder) in worlds.items():
        assert any("regexp_extract(dimensions, 'invoice_id=" in b for b in world.ground_truth.reference_solution.memo_bullets)
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            typed_null = con.execute("SELECT COUNT(*) FROM gl_lines WHERE dimension_invoice_id IS DISTINCT FROM "
                                     "NULLIF(regexp_extract(dimensions, 'invoice_id=([^;]+)', 1), '')").fetchone()[0]
            assert typed_null == 0
            for q in {q.name: q for a in world.ground_truth.anomalies for q in a.reference_sql}.values():
                if "dimension_invoice_id" not in q.sql:
                    continue
                assert "invoice_id=(" not in q.sql
                alt = run_reference_query(con, q.name, _regexp_variant(q.sql), signature=q.expected["signature"],
                                          metric_keys=q.expected["metric_keys"]).expected
                for k in ("columns", "row_count", "rows"):
                    assert alt[k] == q.expected[k], (d, s, q.name, k)
                checked += 1
        finally:
            con.close()
    assert checked >= 9 + 6 * 2 + 3 * 1  # unapplied (all) + cutoff x2 (practitioner+) + fx (principal)
