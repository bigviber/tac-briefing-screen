"""abc_cost_distortion: TDABC recovers the sampled distortion from the DB, variance and mix signatures, ToC twist."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "abc_cost_distortion"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("abc")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}")
            for d in DIFFICULTIES for s in SEEDS}


def _m(world: CaseWorld) -> dict[str, float]:
    return {x.key: x.value for x in world.ground_truth.expected_metrics}


def _q(world: CaseWorld, name: str):
    return next(q for a in world.ground_truth.anomalies for q in a.reference_sql if q.name == name)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_tdabc_from_db_recovers_sampled_distortion(worlds: dict, difficulty: str, seed: int) -> None:
    world, folder = worlds[(difficulty, seed)]
    m = _m(world)
    target = world.ground_truth.anomalies[0].magnitude["distortion_target"]
    q = _q(world, "tdabc_unit_cost_by_family")
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        df = con.execute(q.sql).fetchdf().set_index("family")
        fams = list(con.execute("SELECT DISTINCT family, complexity FROM products").fetchall())
    finally:
        con.close()
    low = next(f for f, c in fams if c == "complex")
    high = next(f for f, c in fams if c == "simple")
    # the reference SQL on the (dirty) DB reproduces the answer key, and both match the sampled target
    assert df.loc[low, "distortion"] == pytest.approx(m["distortion_low_family_pct"], abs=0.005)
    assert df.loc[low, "abc_unit_cents"] == pytest.approx(m["abc_unit_cost_family_low_cents"], rel=0.01)
    assert df.loc[high, "abc_unit_cents"] == pytest.approx(m["abc_unit_cost_family_high_cents"], rel=0.01)
    assert m["distortion_low_family_pct"] == pytest.approx(-target, abs=0.06)
    assert df.loc[high, "distortion"] > 0  # the high-volume line is over-costed
    # reported margin looks best on the premium line, true margin is far lower
    assert m["reported_margin_family_low_pct"] - m["true_margin_family_low_pct"] > 0.08


def test_setup_driver_signature_and_variance_trace(worlds: dict) -> None:
    for (d, s), (world, _) in worlds.items():
        chk = _q(world, "activity_drivers_per_unit_by_family").expected["checks"]
        assert chk["low_ge_4x_high"] and chk["setup_per_unit_low_over_high"] >= 4.0  # spec signature
        m = _m(world)
        P = world.periods
        var = _q(world, "underabsorbed_overhead_by_quarter").expected["checks"]
        assert var["underabsorbed_last"] == pytest.approx(m["underabsorbed_overhead_p8_cents"], abs=1)
        # under-absorbed overhead grows over the window while operating income falls
        kp = world.operations.kpis_by_period
        assert kp[P[-1]]["underabsorbed_overhead_cents"] > kp[P[0]]["underabsorbed_overhead_cents"]
        inc = {i.period: i for i in world.finance.statements.income}
        r = world.finance.statements.ratios
        assert r[P[-1]]["operating_margin"] < r[P[0]]["operating_margin"]
        assert m["operating_income_at_p1_mix_cents"] > inc[P[-1]].operating_income_cents
        # the overhead pool report ties to 5020 every quarter (sub-ledger tie, no designed break)
        ties = [c for c in world.finance.invariants if c.name.startswith("subledger:overhead_pools_vs_5020")]
        assert len(ties) == 8 and all(c.passed and "designed break" not in c.detail for c in ties)


def test_mix_drift_matches_plan(worlds: dict) -> None:
    for (d, s), (world, _) in worlds.items():
        m = _m(world)
        # +2pt a quarter from P1 to P8 (7 steps) = +14pt, within sampling noise
        assert 0.08 <= m["mix_shift_low_family_pts"] <= 0.20
        assert world.ground_truth.anomalies[1].magnitude["mix_drift_pts_per_quarter"] == 0.02


def test_red_herrings_are_plausible(worlds: dict) -> None:
    for s in SEEDS:
        world, folder = worlds[("principal", s)]
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            P = world.periods
            # scrap really rises in the complex family ...
            scrap = con.execute("""
                SELECT period_of(w.released_at) AS period, SUM(w.quantity_scrapped) * 1.0 / SUM(w.quantity_planned) AS scrap_rate
                FROM work_orders w JOIN products p USING (product_id) WHERE p.complexity = 'complex' GROUP BY 1""").fetchdf().set_index("period")["scrap_rate"]
            assert scrap[P[-1]] > scrap[P[0]]
            # ... but scrap expense (5040) is small next to the overhead variance
            scrap_cost = con.execute("SELECT SUM(l.debit_cents - l.credit_cents) FROM gl_lines l JOIN gl_journal j USING (entry_id) WHERE l.account_code = '5040' AND j.period = ?",
                                     [P[-1]]).fetchone()[0]
            assert scrap_cost < _m(world)["underabsorbed_overhead_p8_cents"]
            # labour rate flat: one hourly rate across work centres for the whole window
            assert con.execute("SELECT COUNT(DISTINCT hourly_rate_cents) FROM work_centers").fetchone()[0] == 1
            # discount creep on the high-volume family (~+1pt) exists
            disc = con.execute("""
                SELECT period_of(s.invoiced_at) AS period, AVG(s.discount_pct) AS d
                FROM sales_orders s JOIN products p USING (product_id) WHERE p.complexity = 'simple' GROUP BY 1""").fetchdf().set_index("period")["d"]
            assert 0.005 <= disc[P[-1]] - disc[P[0]] <= 0.015
        finally:
            con.close()


def test_difficulty_ordering_and_throughput_twist(worlds: dict) -> None:
    for s in SEEDS:
        f, pr = worlds[("foundation", s)][0], worlds[("principal", s)][0]
        tf = f.ground_truth.anomalies[0].magnitude["distortion_target"]
        tp = pr.ground_truth.anomalies[0].magnitude["distortion_target"]
        assert tf >= 0.30 and tp <= 0.20 and tf > tp
        assert abs(_m(f)["distortion_low_family_pct"]) > abs(_m(pr)["distortion_low_family_pct"])
        assert len(pr.ground_truth.anomalies) == 3 and len(f.ground_truth.anomalies) == 2
        m = _m(pr)
        # principal ToC twist: the "unprofitable" premium line earns the most per bottleneck minute
        assert m["throughput_per_bottleneck_min_low_cents"] > m["throughput_per_bottleneck_min_high_cents"]
        tp_q = _q(pr, "throughput_per_bottleneck_minute").expected["checks"]
        load = _q(pr, "work_center_load").expected["checks"]
        assert load["most_loaded"] == "WC04" and tp_q["best_family"] in {r[0] for r in _q(pr, "throughput_per_bottleneck_minute").expected["rows"][:1]}


def test_discontinue_trap_in_simulation(worlds: dict) -> None:
    world, _ = worlds[("practitioner", 7)]
    eff = {(e["lever"], e["kpi"]): e for e in world.simulation.model["lever_effects"]}
    # dropping the premium line removes its revenue but only ~60% of its activity cost: operating income falls
    assert eff[("discontinue_family_low", "operating_income_cents")]["delta_per_unit"] < 0
    # repricing it up improves operating income at the spec elasticity
    assert eff[("price_change_family_low_pct", "operating_income_cents")]["delta_per_unit"] > 0
