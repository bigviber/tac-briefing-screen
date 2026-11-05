"""Targeted checks for ``pricing_elasticity`` beyond the generic archetype suite."""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.pedagogy import simulate_transition
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "pricing_elasticity"
DIFFS = ("foundation", "practitioner", "principal")
SEEDS = (7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("pricing")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}") for d in DIFFS for s in SEEDS}


def _m(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _a(world: CaseWorld, aid: str):
    return next(a for a in world.ground_truth.anomalies if a.id == aid)


def test_heterogeneous_elasticities_recovered(worlds: dict) -> None:
    for (d, _s), (world, _) in worlds.items():
        m = _m(world)
        mag = _a(world, "A1").magnitude
        est_s, est_l = m["elasticity_seg_price_shopper"], m["elasticity_seg_loyal_account"]
        assert mag["eps_price_shopper"] < -1.5 and mag["eps_loyal_account"] > -0.7  # the spec's spread
        assert est_s < est_l  # heterogeneity survives estimation
        assert est_s < -1.5
        if d == "practitioner":  # the regression recovers the sampled shopper elasticity
            assert est_s == pytest.approx(mag["eps_price_shopper"], abs=0.6)


@pytest.mark.parametrize("difficulty", DIFFS)
def test_pocket_price_waterfall_and_leakage(worlds: dict, difficulty: str) -> None:
    world, folder = worlds[(difficulty, 7)]
    m = _m(world)
    inc = _a(world, "A1").magnitude["price_change_pct"]
    a2 = _a(world, "A2").magnitude
    d0, d1 = a2["discount_before"], a2["discount_after"]
    assert m["discount_leakage_delta_pts"] == pytest.approx(d1 - d0, abs=0.01)
    assert m["pocket_price_change_pct_seg_price_shopper"] == pytest.approx((1 + inc) * (1 - d1) / (1 - d0) - 1, abs=0.02)
    assert m["pocket_price_change_pct_seg_loyal_account"] == pytest.approx(inc, abs=0.02)
    assert m["pocket_price_change_pct_seg_price_shopper"] < 0.75 * inc  # leakage ate a large share of the increase
    # the list-price PVM overstates the price effect relative to pocket prices
    checks = _a(world, "A1").reference_sql[1].expected["checks"]
    assert checks["list_price_variance"] > checks["price_variance"] > 0
    assert checks["price_variance"] == pytest.approx(m["price_variance_cents"], rel=0.02)
    # PVM identity on the generated lines: price + volume + mix = revenue change over common cells
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        P5, P7 = world.periods[4], world.periods[6]
        rev = con.execute(f"""
WITH oi AS (SELECT DISTINCT order_item_id, order_id, product_id, quantity, net_price_cents FROM order_items),
l AS (SELECT TRIM(c.segment) seg, p.category cat, period_of(CAST(substr(CAST(o.order_ts AS VARCHAR), 1, 19) AS TIMESTAMP)) per,
             oi.net_price_cents * oi.quantity rev
      FROM oi JOIN orders o USING (order_id) JOIN customers c USING (customer_id) JOIN products p USING (product_id)),
cell AS (SELECT seg, cat, SUM(CASE WHEN per = '{P5}' THEN rev END) r5, SUM(CASE WHEN per = '{P7}' THEN rev END) r7 FROM l GROUP BY 1, 2)
SELECT SUM(r7) - SUM(r5) FROM cell WHERE r5 > 0 AND r7 > 0""").fetchone()[0]
    finally:
        con.close()
    assert m["price_variance_cents"] + m["volume_variance_cents"] + m["mix_variance_cents"] == pytest.approx(rev, rel=0.01)


def test_financial_story(worlds: dict) -> None:
    for (d, _s), (world, _) in worlds.items():
        m = _m(world)
        k = world.operations.kpis_by_period
        P = world.periods
        assert k[P[6]]["gross_margin"] > k[P[4]]["gross_margin"]  # GM% up ...
        assert k[P[6]]["discounts_cents"] > k[P[4]]["discounts_cents"]  # leakage in contra revenue (4110)
        if d != "principal":
            assert m["contribution_change_cents"] < 0  # ... GM$ / contribution down
        p5_contrib = k[P[4]]["gross_profit_cents"]
        assert m["contribution_change_cents"] < 0.03 * p5_contrib  # principal (small increase): roughly flat at worst
        opt = m.get("optimal_price_change_seg_price_shopper_pct")
        if opt is not None:
            assert opt < 0  # Lerner: the elastic segment is priced above its optimum
        assert m["revenue_at_segmented_pricing_cents"] > 0


def test_difficulty_ordering(worlds: dict) -> None:
    for s in SEEDS:
        inc = [_a(worlds[(d, s)][0], "A1").magnitude["price_change_pct"] for d in DIFFS]
        rows = [worlds[(d, s)][0].database.total_rows for d in DIFFS]
        assert inc[0] > inc[1] > inc[2]
        assert rows[0] < rows[1] < rows[2]
        assert [len(worlds[(d, s)][0].ground_truth.red_herrings) for d in DIFFS] == [1, 2, 3]
    assert any(a.id == "A4" for a in worlds[("principal", 7)][0].ground_truth.anomalies)  # cannibalization at principal


def test_simulation_segment_response(worlds: dict) -> None:
    world, _ = worlds[("practitioner", 7)]
    levers = world.ground_truth.decision_levers
    k0 = world.simulation.model["kpis"]
    q1, info = simulate_transition(world.simulation, levers, {"price_change_shopper_pct": -10, "fence_feature": 1})
    assert info["resolved"]
    sim2 = world.simulation.model_copy(update={"history": [q1]})
    q2, _ = simulate_transition(sim2, levers, {"price_change_shopper_pct": -10, "fence_feature": 1})
    assert q1.kpis["units_price_shopper"] > k0["units_price_shopper"] and q2.kpis["units_price_shopper"] > q1.kpis["units_price_shopper"]  # partial adjustment
    ql, _ = simulate_transition(world.simulation, levers, {"price_change_loyal_pct": 10})
    assert ql.kpis["contribution_cents"] > k0["contribution_cents"] + world.simulation.model["anomaly_drag"]["contribution_cents"]
    assert ql.kpis["units_loyal_account"] < k0["units_loyal_account"]
    assert np.isfinite(list(q2.kpis.values())).all()


def test_dossier_reveal_is_consistent_and_leaves_work(worlds: dict) -> None:
    """Review P1 #15: the revenue wording follows the number; from practitioner the unit decline and the reps' discount
    requests are not handed over (principal shows them only inside red-herring timeline events)."""
    for (d, s), (world, _) in worlds.items():
        ds = world.dossier_seed
        rev_line = next(r for r in ds.reveal if r.startswith("Revenue is"))
        pct = float(rev_line.split("(")[1].split("%")[0]) / 100
        assert ("roughly flat" in rev_line) == (abs(pct) < 0.03), rev_line
        units_revealed = any(r.startswith("Units are") for r in ds.reveal)
        authority_revealed = any("discount authority" in r for r in ds.reveal)
        tl_text = " ".join(e.headline + " " + e.detail for e in ds.timeline if not e.is_red_herring)
        if d == "foundation":
            assert units_revealed and authority_revealed
        else:
            assert not units_revealed and not authority_revealed
            assert "units" not in tl_text.lower() and "discount" not in tl_text.lower()
            assert any("unit decline" in c for c in ds.conceal) and any("discount authority" in c for c in ds.conceal)
        if d == "principal":
            rh = [e for e in ds.timeline if e.is_red_herring]
            assert any("discount ceiling" in e.detail for e in rh) and any("units" in e.headline for e in rh)
        if d == "practitioner":
            assert "discount_authority_memo" not in ds.artifacts
