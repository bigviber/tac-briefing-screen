"""Targeted checks for ``outbox_pattern`` beyond the generic archetype suite."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "outbox_pattern"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("outbox")
    out = {}
    for d in DIFFICULTIES:
        for s in SEEDS:
            folder = root / f"{d}-{s}"
            out[(d, s)] = (generate_world(KEY, s, difficulty=d, out_dir=folder, pool=pool), folder)
    return out


def _con(world: CaseWorld, folder: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)


def _metrics(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _query(world: CaseWorld, name: str):
    return next(q for a in world.ground_truth.anomalies for q in a.reference_sql if q.name == name)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_recovered_loss_share_matches_sampled_parameter(worlds: dict, difficulty: str, seed: int) -> None:
    """Lost-unit share recovered by an independent SQL anti-join == the sampled ``lost_event_share``."""
    world, folder = worlds[(difficulty, seed)]
    a1 = world.ground_truth.anomalies[0]
    sampled = a1.magnitude["lost_event_share"]
    window = a1.periods
    con = _con(world, folder)
    try:
        lost, total = con.execute(f"""
            WITH ue AS (SELECT * FROM usage_events QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY ingest_id) = 1)
            SELECT SUM(ue.quantity) FILTER (WHERE b.event_id IS NULL), SUM(ue.quantity)
            FROM ue LEFT JOIN (SELECT DISTINCT event_id FROM bus_messages) b USING (event_id)
            WHERE period_of(ue.occurred_at) IN ({", ".join(f"'{p}'" for p in window)})""").fetchone()
    finally:
        con.close()
    share = lost / total
    # targeted deploy losses hit the parameter per month; incident timeouts can only overshoot slightly
    assert sampled - 0.0015 <= share <= sampled + 0.006, (share, sampled)
    m = _metrics(world)
    assert m["lost_unit_share_window"] == pytest.approx(share, abs=1e-6)
    # the share is solved from the gross-margin drift target (1.6-2.7 pts by difficulty), see test_gm_drift_is_a_case_headline...
    assert 0.03 <= sampled <= 0.09


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_designed_break_equals_declared_amount(worlds: dict, difficulty: str) -> None:
    """usage_events vs billed usage: the invariant verifies the designed break, and the reference SQL
    run on the dirty DB recovers the same amount per quarter (zero before the pipeline went live)."""
    world, folder = worlds[(difficulty, 7)]
    checks = [c for c in world.finance.invariants if c.name.startswith("subledger:usage_events_vs_billed_usage")]
    assert len(checks) == 8 and all(c.passed for c in checks)
    assert sum("designed break" in c.detail for c in checks) >= 2
    declared = {k[4:]: v for k, v in world.ground_truth.anomalies[1].magnitude.items() if k.startswith("gap_")}
    q = _query(world, "metered_vs_billed_usage_by_quarter")
    con = _con(world, folder)
    try:
        rows = con.execute(q.sql).fetchall()
    finally:
        con.close()
    by_period = {r[0]: int(r[3]) for r in rows}
    for period in world.periods:
        assert by_period[period] == declared[period], period
    for period in world.periods[:4]:
        assert by_period[period] == 0  # legacy batch billed straight from usage_events
    assert by_period[world.periods[-1]] == _metrics(world)["usage_vs_billed_gap_cents_p8"]
    other = [c for c in world.finance.invariants if c.name.startswith(("subledger:ar_open_invoices", "subledger:usage_revenue_invoice_lines"))]
    assert other and all(c.passed and "designed break" not in c.detail for c in other)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_metrics_reproduced_by_reference_sql(worlds: dict, difficulty: str) -> None:
    world, _ = worlds[(difficulty, 2024)]
    m = _metrics(world)
    lost = _query(world, "lost_events_by_month").expected["checks"]
    assert lost["lost_events"] == m["lost_event_count"] and lost["lost_units"] == m["lost_units"]
    assert lost["unbilled_cents"] == m["unbilled_revenue_cents"]
    bb = _query(world, "recoverable_backbill_by_customer").expected["checks"]
    assert bb["recoverable_cents"] == m["recoverable_backbill_cents"] and bb["unbilled_cents"] == m["unbilled_revenue_cents"]
    assert 0 < m["recoverable_backbill_cents"] <= m["unbilled_revenue_cents"]
    assert m["gm_drift_pts"] < 0
    near = _query(world, "lost_events_near_deploys").expected["checks"]["lost_near_deploys"]
    assert near > 0  # deploy-time flushes are the dominant loss mode


def test_phantoms_only_at_principal_and_credited(worlds: dict) -> None:
    for s in SEEDS:
        assert "phantom_event_count" not in _metrics(worlds[("practitioner", s)][0])
        world, folder = worlds[("principal", s)]
        m = _metrics(world)
        ph = _query(world, "phantom_events_by_customer").expected["checks"]
        assert ph["phantom_events"] == m["phantom_event_count"] > 0 and ph["overbilled_cents"] == m["overbilled_cents"]
        con = _con(world, folder)
        try:
            credited = con.execute("SELECT SUM(amount_cents) FROM credits").fetchone()[0]
            outbox_used_for_usage = con.execute("SELECT COUNT(*) FROM outbox WHERE event_id IN (SELECT event_id FROM usage_events)").fetchone()[0]
        finally:
            con.close()
        assert 0 < credited <= m["overbilled_cents"]
        assert outbox_used_for_usage == 0  # the outbox exists but usage-service does not use it


def test_unit_infra_cost_is_flat(worlds: dict) -> None:
    """Red herring check: compute cost per metered unit only moves by the -2% cloud price change."""
    world, folder = worlds[("practitioner", 7)]
    con = _con(world, folder)
    try:
        rows = con.execute("SELECT unit_cost_microcents FROM infra_costs ORDER BY month").fetchall()
    finally:
        con.close()
    vals = sorted({r[0] for r in rows})
    assert len(vals) == 2 and vals[0] == pytest.approx(vals[1] * 0.98, rel=1e-4)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_gm_drift_is_a_case_headline_against_a_stated_baseline(worlds: dict, difficulty: str, seed: int) -> None:
    """Review: -0.8 pts was too small for a headline. The lost share is solved from a 1.6-2.7 pt drift target, so practitioner
    and principal show >= 1.5 pts (spec 1-3) and the derivation names the counterfactual baseline; the as-billed margin the
    CFO sees also slips from the pre-migration quarter to the latest one."""
    world, _ = worlds[(difficulty, seed)]
    m = _metrics(world)
    assert -3.5 < m["gm_drift_pts"] <= -1.5
    deriv = next(x for x in world.ground_truth.expected_metrics if x.key == "gm_drift_pts").derivation
    assert "Baseline:" in deriv and "every metered unit invoiced" in deriv and "not the pre-migration margin" in deriv
    r = world.finance.statements.ratios
    assert r[world.periods[-1]]["gross_margin"] < r[world.periods[3]]["gross_margin"]
    # a Kafka-era usage-API platform is not founded in the 1990s (base founded_floor for usage / API business models)
    assert world.company.founded >= 2008
