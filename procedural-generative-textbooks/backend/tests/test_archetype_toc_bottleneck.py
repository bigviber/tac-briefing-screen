"""Targeted checks for ``toc_bottleneck`` beyond the generic archetype suite."""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.pedagogy import simulate_transition
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "toc_bottleneck"
DIFFS = ("foundation", "practitioner", "principal")
SEEDS = (7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("toc")
    out = {}
    for d in DIFFS:
        for s in SEEDS:
            folder = root / f"{d}-{s}"
            out[(d, s)] = (generate_world(KEY, s, difficulty=d, out_dir=folder, pool=pool), folder)
    return out


def _metrics(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _anomaly(world: CaseWorld, aid: str):
    return next(a for a in world.ground_truth.anomalies if a.id == aid)


def _con(world: CaseWorld, folder: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)


@pytest.mark.parametrize("seed", SEEDS)
def test_naive_vs_true_constraint(worlds: dict, seed: int) -> None:
    """Run-time 'efficiency' points at the wrong station from practitioner up; foundation agrees."""
    for d in DIFFS:
        world, folder = worlds[(d, seed)]
        con = _con(world, folder)
        try:
            util = con.execute(_anomaly(world, "A1").reference_sql[0].sql).fetchdf()
        finally:
            con.close()
        true_seq = int(util.sort_values("utilization", ascending=False).iloc[0]["op_seq"])
        naive_seq = int(util.sort_values("run_only_utilization", ascending=False).iloc[0]["op_seq"])
        m = _metrics(world)
        assert true_seq == int(m["bottleneck_op_seq"])
        capex_seq = int(util["op_seq"].max())  # the finishing line (capex request) is last
        assert true_seq != capex_seq and m["rho_capex_station"] < 0.85 < m["rho_bottleneck"]
        if d == "foundation":
            assert naive_seq == true_seq
        else:
            assert naive_seq != true_seq and naive_seq < true_seq  # the upstream next-tightest station


@pytest.mark.parametrize("difficulty", DIFFS)
def test_recovered_magnitudes_match_parameters(worlds: dict, difficulty: str) -> None:
    world, folder = worlds[(difficulty, 7)]
    a2, a3, a4 = (_anomaly(world, k) for k in ("A2", "A3", "A4"))
    con = _con(world, folder)
    try:
        wc = con.execute("SELECT work_center_id, op_seq, capacity_units_per_hour FROM work_centers").fetchdf()
        b_seq = int(_metrics(world)["bottleneck_op_seq"])
        b_id = wc.loc[wc["op_seq"] == b_seq, "work_center_id"].iat[0]
        p8 = world.periods[-1]
        months = [f"{m:%Y-%m}" for m in pd.date_range(*[pd.Timestamp(x) for x in _bounds(p8)], freq="MS")]
        avail = con.execute(f"SELECT SUM(regular_hours + overtime_hours) * 60 FROM labor_hours WHERE work_center_id = '{b_id}' "
                            f"AND month IN ({','.join(repr(m) for m in months)})").fetchone()[0]
        setup_min = con.execute(f"SELECT SUM(setup_minutes_actual) FROM (SELECT DISTINCT log_id, setup_minutes_actual, start_at, work_center_id FROM production_log) "
                                f"WHERE work_center_id = '{b_id}' AND period_of(start_at) = '{p8}'").fetchone()[0]
        scrap = con.execute("SELECT SUM(quantity) FROM quality_events").fetchone()[0]
        shipped = con.execute("SELECT SUM(quantity_completed + quantity_scrapped) FROM work_orders WHERE status = 'closed'").fetchone()[0]
    finally:
        con.close()
    assert setup_min / avail == pytest.approx(a3.magnitude["setup_share_b"], abs=0.025)
    assert scrap / shipped == pytest.approx(a2.magnitude["scrap_after_b_pct"], abs=0.006)
    assert a2.magnitude["origin_upstream_share"] == pytest.approx(a2.magnitude["defect_origin_upstream_share"], abs=0.08)
    caps = wc.sort_values("capacity_units_per_hour")["capacity_units_per_hour"].to_numpy()
    assert caps[1] / caps[0] - 1 == pytest.approx(a4.magnitude["c_b2"] / a4.magnitude["c_b"] - 1, abs=0.01)
    assert _metrics(world)["migration_threshold_pct"] == pytest.approx(a4.magnitude["c_b2"] / a4.magnitude["c_b"] - 1, abs=0.002)


def _bounds(period: str) -> tuple[str, str]:
    from axiom.engine.finance import quarter_bounds

    s, e = quarter_bounds(period)
    return str(s), str(e)


@pytest.mark.parametrize("difficulty", DIFFS)
def test_littles_law_on_generated_data(worlds: dict, difficulty: str) -> None:
    """Time-average WIP over the last quarter ~= completion rate x average flow time (Little), and the
    queueing block in OperationsState satisfies L = lambda * W exactly."""
    world, folder = worlds[(difficulty, 2024)]
    q = world.operations.queueing
    assert q["L"] == pytest.approx(q["lambda"] * q["W"], rel=1e-9)
    assert q["Lq"] == pytest.approx(q["lambda"] * q["Wq"], rel=1e-9)
    con = _con(world, folder)
    try:
        wo = con.execute("SELECT work_order_id, quantity_planned, released_at, closed_at FROM work_orders").fetchdf()
    finally:
        con.close()
    s, e = (pd.Timestamp(x) for x in _bounds(world.periods[-1]))
    e = e + pd.Timedelta(days=1)
    days = pd.date_range(s, e - pd.Timedelta(days=1), freq="D") + pd.Timedelta(hours=12)
    rel = wo["released_at"].to_numpy()
    close = wo["closed_at"].fillna(pd.Timestamp("2100-01-01")).to_numpy()
    qty = wo["quantity_planned"].to_numpy()
    avg_wip = np.mean([qty[(rel <= d.to_datetime64()) & (close > d.to_datetime64())].sum() for d in days])
    done = wo[(wo["closed_at"] >= s) & (wo["closed_at"] < e)]
    thr_day = done["quantity_planned"].sum() / len(days)
    flow = ((done["closed_at"] - done["released_at"]).dt.total_seconds() / 86400 * done["quantity_planned"]).sum() / done["quantity_planned"].sum()
    assert avg_wip == pytest.approx(thr_day * flow, rel=0.30)
    # the ground-truth cycle time is Little's L/lambda at quarter end
    m = _metrics(world)
    assert m["cycle_time_days_p8"] > 5


def test_financial_trace_and_difficulty_ordering(worlds: dict) -> None:
    for s in SEEDS:
        mags = {}
        for d in DIFFS:
            world, _ = worlds[(d, s)]
            P = world.periods
            tb = {p: world.finance.trial_balance[p] for p in P}
            assert tb[P[-1]]["1210"] > 1.3 * tb[P[0]]["1210"]  # WIP build
            r = world.finance.statements.ratios
            assert r[P[-1]]["dio"] > r[P[0]]["dio"]
            assert r[P[-1]]["operating_margin"] < r[P[0]]["operating_margin"]
            m = _metrics(world)
            assert m["overtime_cost_p8_cents"] > 0
            pen = sum(e.lines[0].debit_cents for e in world.finance.journal if e.source == "penalties" and e.period == P[-1])
            assert pen >= 0
            mags[d] = (_anomaly(world, "A3").magnitude["setup_share_b"], _anomaly(world, "A2").magnitude["scrap_after_b_pct"],
                       world.database.total_rows, len(world.ground_truth.red_herrings))
        assert mags["foundation"][0] > mags["practitioner"][0] > mags["principal"][0]
        assert mags["foundation"][1] > mags["practitioner"][1] > mags["principal"][1]
        assert mags["foundation"][2] < mags["practitioner"][2] < mags["principal"][2]
        assert mags["foundation"][3] < mags["practitioner"][3] < mags["principal"][3]


def test_simulation_migration_caps_throughput(worlds: dict) -> None:
    world, _ = worlds[("practitioner", 7)]
    levers = world.ground_truth.decision_levers
    model = world.simulation.model
    k0 = model["kpis"]
    q1, _ = simulate_transition(world.simulation, levers, {"capacity_add_bottleneck_pct": 40})
    sim2 = world.simulation.model_copy(update={"history": [q1]})
    q2, _ = simulate_transition(sim2, levers, {"capacity_add_bottleneck_pct": 40})
    cap = model["migration"]["throughput_ceiling_at_b2_units_month"]
    assert q2.kpis["throughput_units_month"] <= cap + 1e-6
    assert q2.kpis["throughput_units_month"] < k0["throughput_units_month"] * 1.40  # gains cap below +40%
    assert q2.kpis["b2_utilization"] <= 1.0
    # the capex request moves cost, not throughput
    qf, _ = simulate_transition(world.simulation, levers, {"capacity_add_finishing_pct": 40})
    assert qf.kpis["throughput_units_month"] == pytest.approx(k0["throughput_units_month"] + model["anomaly_drag"].get("throughput_units_month", 0.0))
    # exploiting the constraint resolves the anomaly
    _qr, info = simulate_transition(world.simulation, levers, {"move_inspection_before_b": 1, "batch_size_b_multiplier": 3})
    assert info["resolved"]
