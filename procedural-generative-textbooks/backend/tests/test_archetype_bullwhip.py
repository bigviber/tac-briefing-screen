"""Targeted checks for ``bullwhip`` beyond the generic archetype suite."""

from __future__ import annotations

from pathlib import Path

import datetime as dt
import statistics

import duckdb
import numpy as np
import pytest

from axiom.engine.finance import quarter_bounds
from axiom.engine.market_research import get_default_pool
from axiom.engine.operations import chen_bullwhip_bound
from axiom.engine.pedagogy import simulate_transition
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "bullwhip"
DIFFS = ("foundation", "practitioner", "principal")
SEEDS = (7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("bullwhip")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}") for d in DIFFS for s in SEEDS}


def _m(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _a(world: CaseWorld, aid: str):
    return next(a for a in world.ground_truth.anomalies if a.id == aid)


def _window(world: CaseWorld) -> tuple[dt.date, dt.date]:
    """The 52 weeks the bullwhip asks are defined on: from the first Monday of P5."""
    s5 = quarter_bounds(world.periods[4])[0]
    wa = s5 + dt.timedelta(days=(7 - s5.weekday()) % 7)
    return wa, wa + dt.timedelta(weeks=51)


@pytest.mark.parametrize("difficulty", DIFFS)
@pytest.mark.parametrize("seed", SEEDS)
def test_amplification_recovered_and_above_chen_bound(worlds: dict, difficulty: str, seed: int) -> None:
    world, folder = worlds[(difficulty, seed)]
    m = _m(world)
    a1 = _a(world, "A1")
    refs = {q.name: q.expected["checks"] for q in a1.reference_sql}
    checks = refs["bullwhip_ratio_by_sku_order_cycle"]
    assert checks["cycles"] == 13
    assert checks["median_amp_t2"] == pytest.approx(m["amplification_tier2"], rel=0.05)
    assert checks["median_amp_t3"] == pytest.approx(m["amplification_tier3"], rel=0.05)
    cv = refs["weekly_cv_company_level"]
    assert cv["weeks"] == 52
    assert (cv["cv_pos"], cv["cv_ro"], cv["cv_po"]) == pytest.approx((m["cv_pos_demand"], m["cv_retailer_orders"], m["cv_company_po"]), abs=0.01)
    assert m["cv_pos_demand"] < m["cv_retailer_orders"] < m["cv_company_po"]
    assert 0.03 < m["cv_pos_demand"] < 0.15  # stable shelf demand (weekly, all SKUs)
    # review P0 #4: the asks land in the literature range (2-10x per tier, <= ~12x cumulative)
    assert 2.0 <= m["amplification_tier2"] <= 10.0
    assert m["amplification_tier2"] < m["amplification_tier3"] <= 12.5
    assert 1.5 < a1.magnitude["company_stage_cv_ratio"] < 10.0
    # the zero-filled weekly per-SKU variance ratio is the trap, an order of magnitude above the ask
    trap = refs["weekly_zero_filled_variance_trap"]
    assert trap["median_weekly_var_ratio_t3"] > 4 * m["amplification_tier3"]
    assert trap["median_weekly_var_ratio_t3"] == pytest.approx(a1.magnitude["weekly_zero_filled_var_ratio_tier3"], rel=0.05)
    if difficulty != "foundation":
        assert any("zeros in the weeks between MOQ lots" in h for h in world.ground_truth.red_herrings)
    # Chen et al.: our weekly ordering stage amplifies at least 1 + 2L/p + 2L^2/p^2 (recomputed from the DB on the same
    # 52-week window; in the review-period model a week without a PO is an order of 0)
    p = int(m["forecast_window_p"])
    chen = chen_bullwhip_bound(3, p)
    assert m["chen_lower_bound_tier2"] == pytest.approx(chen)
    wa, wb = _window(world)
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        stage = con.execute(f"""
WITH o AS (SELECT DISTINCT order_id, sku, order_date, qty FROM orders),
g AS (SELECT sku, date_trunc('week', sale_date) wk FROM pos_sales WHERE sale_date BETWEEN DATE '{wa}' AND DATE '{wb}'),
ro AS (SELECT sku, date_trunc('week', order_date) wk, SUM(qty) q FROM o GROUP BY 1, 2),
po AS (SELECT sku, date_trunc('week', CAST(TRY_CAST(substr(CAST(order_date AS VARCHAR), 1, 19) AS TIMESTAMP) AS DATE)) wk, SUM(qty) q
       FROM purchase_orders GROUP BY 1, 2)
SELECT median(r) FROM (
  SELECT g.sku, var_samp(COALESCE(po.q, 0)) / var_samp(COALESCE(ro.q, 0)) r
  FROM g LEFT JOIN ro USING (sku, wk) LEFT JOIN po USING (sku, wk) GROUP BY 1)""").fetchone()[0]
        signal = con.execute("SELECT demand_signal FROM forecast_config ORDER BY effective_from DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()
    assert stage >= chen
    assert stage == pytest.approx(a1.magnitude["company_stage_ratio"], rel=0.05)
    assert a1.magnitude["company_stage_ratio"] >= a1.magnitude["chen_bound"]
    assert signal == "retailer_orders"  # the POS feed exists but is not the planning signal


def test_promo_moves_demand_in_time_and_supplier_is_stable(worlds: dict) -> None:
    for world, _ in worlds.values():
        a2 = _a(world, "A2").magnitude
        assert a2["promo_forward_buy_multiple"] > 1.3
        assert abs(a2["pos_12wk_change"]) < 0.06  # promo shifts demand in time; 12-week POS ~unchanged (sampling noise at 10 SKUs)
        sup = _a(world, "A3").reference_sql[0].expected["checks"]
        assert sup["lead_days_range"] < 1.0  # supplier lead time unchanged quarter to quarter


def test_financial_traces(worlds: dict) -> None:
    for world, _ in worlds.values():
        m = _m(world)
        P = world.periods
        assert m["expedite_cost_cents_p7_p8"] > 0 and m["writedown_cents_p8"] > 0
        inv8 = world.finance.trial_balance[P[-1]]["1220"]
        wd = _a(world, "A4").magnitude["writedown_pct"]
        assert m["writedown_cents_p8"] <= wd * inv8 * 1.05 and m["writedown_cents_p8"] >= 0.3 * wd * inv8
        assert world.finance.trial_balance[P[-1]]["1230"] == -m["writedown_cents_p8"]
        ratios = world.finance.statements.ratios
        assert max(ratios[p]["dio"] for p in P[4:]) > ratios[P[0]]["dio"]  # inventory swings up after the policy change
        # the POS-driven counterfactual holds less inventory than the actual policy
        sc = world.operations.supply_chain
        assert m["inventory_value_at_pos_based_policy_cents"] < sum(k["inventory_cents"] for k in world.operations.kpis_by_period.values()) / 8 * 1.5
        assert sc["bullwhip_ratio_by_tier"]["tier2_company_pos_vs_pos"] == pytest.approx(m["amplification_tier3"])


def test_working_capital_is_a_distributor(worlds: dict) -> None:
    """Review P1 #11: DIO in the consumer-goods distributor band; AP moves with receipts (EOM terms), no near-zero quarter."""
    for (d, s), (world, folder) in worlds.items():
        P = world.periods
        ratios = world.finance.statements.ratios
        dio = [ratios[p]["dio"] for p in P]
        assert 28 <= dio[0] <= 60 and 30 <= statistics.median(dio) <= 60, (d, s, dio)
        assert all(25 <= x <= 90 for x in dio), (d, s, dio)  # the glut quarter may run above the band: that swing is the case
        ap = [-world.finance.trial_balance[p]["2000"] for p in P]
        assert min(ap) >= 0.45 * statistics.median(ap), (d, s, ap)
        assert all(30 <= ratios[p]["dpo"] <= 80 for p in P), (d, s, [ratios[p]["dpo"] for p in P])
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            terms = con.execute("SELECT payment_terms_days FROM suppliers").fetchone()[0]
            bad = con.execute(f"""
SELECT COUNT(*) FROM supplier_shipments
WHERE paid_at IS NOT NULL AND CAST(paid_at AS DATE) <> last_day(CAST(received_at AS DATE)) + INTERVAL {terms} DAY""").fetchone()[0]
        finally:
            con.close()
        assert bad == 0  # net-N EOM: due N days after the end of the receipt month


def test_difficulty_ordering(worlds: dict) -> None:
    for s in SEEDS:
        t3 = [_m(worlds[(d, s)][0])["amplification_tier3"] for d in DIFFS]
        rows = [worlds[(d, s)][0].database.total_rows for d in DIFFS]
        assert t3[0] > t3[2]  # louder at foundation (fewer SKUs, tighter supplier capacity, longer MOQ)
        assert rows[0] < rows[1] < rows[2]
        assert [len(worlds[(d, s)][0].ground_truth.red_herrings) for d in DIFFS] == [1, 2, 3]
    principal = worlds[("principal", 7)][0]
    assert "tier3_supplier_production_vs_pos" in principal.operations.supply_chain["bullwhip_ratio_by_tier"]


def test_simulation_dual_source_is_a_trap(worlds: dict) -> None:
    world, _ = worlds[("practitioner", 7)]
    levers = world.ground_truth.decision_levers
    k0 = world.simulation.model["kpis"]
    q, info = simulate_transition(world.simulation, levers, {"dual_source": 1})
    assert not info["resolved"] and q.kpis["amplification_tier3"] == pytest.approx(k0["amplification_tier3"], abs=1e-3)  # KPIs are rounded
    assert q.kpis["cogs_cents"] > k0["cogs_cents"]
    q1, info = simulate_transition(world.simulation, levers, {"demand_signal_pos": 1, "forecast_window_p": 12})
    assert info["resolved"] and q1.kpis["amplification_tier3"] < k0["amplification_tier3"]
    sim2 = world.simulation.model_copy(update={"history": [q1]})
    q2, _ = simulate_transition(sim2, levers, {"demand_signal_pos": 1, "forecast_window_p": 12})
    assert q2.kpis["inventory_cents"] < q1.kpis["inventory_cents"]  # smoothing lowers inventory with a one-quarter lag
    assert np.isfinite(list(q2.kpis.values())).all()
