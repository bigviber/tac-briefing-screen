"""Targeted checks for ``replica_lag`` beyond the generic archetype suite."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "replica_lag"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("replica")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}")
            for d in DIFFICULTIES for s in SEEDS}


def _fetch(world: CaseWorld, folder: Path, sql: str) -> list[tuple]:
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _metrics(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _query(world: CaseWorld, name: str):
    return next(q for a in world.ground_truth.anomalies for q in a.reference_sql if q.name == name)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_oversells_are_stale_reads(worlds: dict, difficulty: str, seed: int) -> None:
    """Every oversold order reserved below zero on the primary after checkout saw stock on the replica/cache."""
    world, folder = worlds[(difficulty, seed)]
    m = _metrics(world)
    (neg,) = _fetch(world, folder, "SELECT COUNT(*) FROM inventory_events WHERE event_type = 'order_reserved' AND qty_after < 0")[0:1][0]
    assert neg == m["oversold_orders_count"] > 0
    checks = _query(world, "orders_after_primary_stockout").expected["checks"]
    assert checks["oversold_orders"] == m["oversold_orders_count"] and checks["share_with_positive_seen"] == 1.0
    # red herring: cycle counts agree with the primary's on_hand
    rows = _fetch(world, folder, "SELECT COUNT(*), COUNT(*) FILTER (WHERE counted_qty = system_on_hand), MAX(ABS(counted_qty - system_on_hand)) FROM inventory_counts")
    n, exact, worst = rows[0]
    assert exact / n >= 0.9 and worst <= 1


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_recovered_flash_lag_matches_sampled_parameter(worlds: dict, difficulty: str, seed: int) -> None:
    """Median lag in the biggest flash sale (after the 3-minute load ramp) ~ the sampled ``lag_under_load_s``."""
    world, folder = worlds[(difficulty, seed)]
    sampled = world.ground_truth.anomalies[0].magnitude["lag_under_load_s"]
    (med,) = _fetch(world, folder, """
        WITH last AS (SELECT * FROM flash_sales ORDER BY starts_at DESC LIMIT 1)
        SELECT quantile_cont(s.lag_ms, 0.5) / 1000.0 FROM replica_lag_samples s, last
        WHERE s.sampled_at >= last.starts_at + INTERVAL 3 MINUTE AND s.sampled_at < last.ends_at""")[0]
    assert sampled * 0.8 <= med <= sampled * 1.2 + 0.5, (med, sampled)
    lo, hi = {"foundation": (34.5, 45.0), "practitioner": (18.75, 29.25), "principal": (3.0, 13.5)}[difficulty]
    assert lo - 1e-6 <= sampled <= hi + 1e-6


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_lag_samples_show_oversell_correlation(worlds: dict, difficulty: str) -> None:
    for s in SEEDS:
        world, _ = worlds[(difficulty, s)]
        m = _metrics(world)
        rows = _query(world, "orders_after_primary_stockout").expected["rows"]
        cols = _query(world, "orders_after_primary_stockout").expected["columns"]
        lag_before = [r[cols.index("lag_p95_before_ms")] for r in rows if r[cols.index("lag_p95_before_ms")] is not None]
        if difficulty != "principal":
            # oversells happen during lag spikes: rank correlation positive, lag before an oversell >> steady state
            assert m["oversell_vs_lag_correlation"] > 0.1
            assert lag_before and sorted(lag_before)[len(lag_before) // 2] > 10 * m["lag_p50_ms"]
        else:
            # principal: the availability cache adds staleness that is not lag, diluting the correlation;
            # the nightly analytics job explains a large share of the off-sale oversells instead
            assert m["oversell_vs_lag_correlation"] > -0.1
            assert m["nightly_job_lag_attribution_share"] > 0.2 and m["lag_p99_nightly_ms"] > 60_000
        rate, target = m["oversell_rate_flash"], world.ground_truth.anomalies[0].magnitude["oversell_share_target"]
        if difficulty != "principal":
            assert 0.3 * target <= rate <= 3 * target, (rate, target)
        assert m["oversell_rate_flash"] > m["oversell_rate_baseline"] or difficulty == "principal"


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_designed_break_equals_cancellations(worlds: dict, difficulty: str) -> None:
    world, folder = worlds[(difficulty, 7)]
    checks = [c for c in world.finance.invariants if c.name.startswith("subledger:orders_gmv_vs_net_revenue")]
    assert len(checks) == 8 and all(c.passed for c in checks) and any("designed break" in c.detail for c in checks)
    q = _query(world, "oms_gmv_vs_gl_net_revenue")
    assert q.expected["checks"]["gap_equals_cancellations"] is True
    declared = {k[len("gmv_gap_"):]: v for k, v in world.ground_truth.anomalies[1].magnitude.items() if k.startswith("gmv_gap_")}
    got = {r[0]: r[3] for r in _fetch(world, folder, q.sql)}
    for period in world.periods:
        assert got[period] == declared[period]
    for name in ("inventory_receipts_less_shipments", "refund_liability_open_cancellations", "ap_open_purchase_orders"):
        ties = [c for c in world.finance.invariants if c.name.startswith(f"subledger:{name}")]
        assert len(ties) == 8 and all(c.passed and "designed break" not in c.detail for c in ties)


def test_cost_rollup_reproduced_by_sql(worlds: dict) -> None:
    for d in DIFFICULTIES:
        world, _ = worlds[(d, 2024)]
        m = _metrics(world)
        total = _query(world, "oversell_cost_rollup").expected["checks"]["total_cents"]
        assert total == pytest.approx(m["cancellation_cost_cents"], rel=1e-6)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_liquidator_holds_inventory_and_says_so(worlds: dict, difficulty: str) -> None:
    """Review P1 #12: the company is a liquidator / reseller (inventory, COGS, reseller margins), and the profile says so."""
    world, _ = worlds[(difficulty, 7)]
    bm = world.company.business_model.lower()
    assert "marketplace" not in bm and ("liquidat" in bm or "reselling" in bm or "resell" in bm)
    assert "marketplace" not in world.company.description.lower()
    for p in world.periods:
        assert 0.10 <= world.finance.statements.ratios[p]["gross_margin"] <= 0.35
    assert any(ln.caption == "Inventory, net" and ln.cents > 0 for bs in world.finance.statements.balance for ln in bs.lines)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_checkout_reads_cover_the_whole_window(worlds: dict, difficulty: str, seed: int) -> None:
    """QA: checkout_reads spans the same quarters as orders - primary reads before the read-path switch, replica (and the
    cache at principal) after it; every order line has exactly one read."""
    world, folder = worlds[(difficulty, seed)]
    reads = dict(_fetch(world, folder, "SELECT period_of(read_at), COUNT(*) FROM checkout_reads GROUP BY 1"))
    orders = {r[0] for r in _fetch(world, folder, "SELECT DISTINCT period_of(CAST(CAST(placed_at AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS TIMESTAMP)) FROM orders")}
    assert set(world.periods) <= set(reads) and set(world.periods) <= orders
    (n_reads, n_items), = _fetch(world, folder, "SELECT (SELECT COUNT(*) FROM checkout_reads), (SELECT COUNT(*) FROM order_items)")
    assert n_reads == n_items
    (switch,), = _fetch(world, folder, "SELECT changed_at FROM service_config WHERE key = 'inventory_read_path'")
    by_src = dict(_fetch(world, folder, f"""SELECT read_from, COUNT(*) FILTER (WHERE read_at < TIMESTAMP '{switch}')
                                            FROM checkout_reads GROUP BY 1"""))
    assert by_src.get("primary", 0) > 0 and by_src.get("replica", 0) == 0 and by_src.get("cache", 0) == 0
    # a primary read is exact: the regular SKUs never sell out, so no primary read saw zero
    (bad,), = _fetch(world, folder, "SELECT COUNT(*) FROM checkout_reads WHERE read_from = 'primary' AND available_qty_seen < requested_qty")
    assert bad == 0
    if difficulty == "principal":
        (cache_from,), = _fetch(world, folder, "SELECT changed_at FROM service_config WHERE key = 'availability_cache_ttl_s'")
        (early_cache,), = _fetch(world, folder, f"SELECT COUNT(*) FROM checkout_reads WHERE read_from = 'cache' AND read_at < TIMESTAMP '{cache_from}'")
        assert early_cache == 0
