"""Targeted checks for ``cohort_collapse`` beyond the generic archetype suite."""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest

from axiom.engine.archetypes.base import condition_holds, satisfying_decisions
from axiom.engine.finance import quarter_bounds
from axiom.engine.market_research import get_default_pool
from axiom.engine.operations import sbg_survival
from axiom.engine.pedagogy import simulate_transition
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "cohort_collapse"
DIFFS = ("foundation", "practitioner", "principal")
SEEDS = (7, 2024, 5)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("cohort")
    out = {}
    for d in DIFFS:
        for s in SEEDS:
            folder = root / f"{d}-{s}"
            out[(d, s)] = (generate_world(KEY, s, difficulty=d, out_dir=folder, pool=pool), folder)
    return out


def _m(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _a(world: CaseWorld, aid: str):
    return next(a for a in world.ground_truth.anomalies if a.id == aid)


def _headline_change(world: CaseWorld) -> float:
    k = world.operations.kpis_by_period
    h = [k[p]["headline_logo_churn_monthly"] for p in world.periods]
    return float(np.mean(h[6:]) / np.mean(h[:4]) - 1)


def test_headline_flat_while_cohorts_collapse(worlds: dict) -> None:
    changes, ratios = [], []
    for (d, _s), (world, _) in worlds.items():
        m = _m(world)
        a1 = _a(world, "A1").magnitude
        assert m["retention_m3_post_cohorts"] < m["retention_m3_pre_cohorts"]
        assert m["ltv_cac_post"] < m["ltv_cac_pre"]
        changes.append(_headline_change(world))
        ratios.append(a1["hazard_marker"] / a1["hazard_control"] / a1["collapse_multiplier"])
        if d == "principal":  # largest samples: the board KPI moves little while the collapsing group churns ~multiplier x faster
            assert changes[-1] < 0.4
            assert 0.55 < ratios[-1] < 1.45
    mult = np.mean([_a(w, "A1").magnitude["collapse_multiplier"] for w, _ in worlds.values()])
    assert np.mean(changes) < 0.45 * (mult - 1)
    assert 0.7 < np.mean(ratios) < 1.3  # pooled recovery of the sampled multiplier


@pytest.mark.parametrize("difficulty", DIFFS)
def test_reference_sql_recovers_metrics_and_marker(worlds: dict, difficulty: str) -> None:
    world, folder = worlds[(difficulty, 7)]
    m = _m(world)
    a1 = _a(world, "A1")
    checks = a1.reference_sql[0].expected["checks"]
    assert checks["m3_pre_mean"] == pytest.approx(m["retention_m3_pre_cohorts"], abs=0.03)
    assert checks["m3_post_mean"] == pytest.approx(m["retention_m3_post_cohorts"], abs=0.03)
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        cause = int(m["collapse_cause_code"])
        p5 = world.periods[4]
        from axiom.engine.finance import quarter_bounds

        start = quarter_bounds(p5)[0]
        if cause == 1:
            share = con.execute(f"SELECT AVG(CASE WHEN TRIM(acquisition_channel) = 'paid_social' THEN 1.0 ELSE 0.0 END) FROM customers "
                                f"WHERE created_at >= TIMESTAMP '{start} 00:00:00'").fetchone()[0]
            assert share == pytest.approx(0.45, abs=0.08)
        elif cause == 3:
            share = con.execute(f"SELECT AVG(CASE WHEN s.discount_pct >= 30 THEN 1.0 ELSE 0.0 END) FROM subscriptions s JOIN customers c USING (customer_id) "
                                f"WHERE c.created_at >= TIMESTAMP '{start} 00:00:00' AND s.contract_months = 1").fetchone()[0]
            assert share == pytest.approx(0.50, abs=0.10)
        else:
            assert m["cause_marker_share"] == pytest.approx(0.5, abs=0.15)
        assert share if cause != 2 else True
        # annual-prepay mix shift (A3) recovered from SQL
        a3 = _a(world, "A3")
        c3 = a3.reference_sql[0].expected["checks"]
        assert c3["annual_share_first"] > c3["annual_share_last"]
    finally:
        con.close()


@pytest.mark.parametrize("difficulty", DIFFS)
def test_sbg_fit_recovers_survival(worlds: dict, difficulty: str) -> None:
    """The sBG fit in the ground truth reproduces the observed post-P5 survivor curve, and the projection runs off."""
    world, _ = worlds[(difficulty, 2024)]
    m = _m(world)
    deriv = next(x.derivation for x in world.ground_truth.expected_metrics if x.key == "mrr_projected_12m_cents")
    assert "a=" in deriv and "b=" in deriv
    k = world.operations.kpis_by_period
    assert 0 < m["mrr_projected_12m_cents"] < k[world.periods[-1]]["mrr_cents"]
    # cohort curves in OperationsState are monotone survival curves starting at 1
    for c in world.operations.cohorts:
        assert c.retention[0] == 1.0 and all(b <= a + 1e-12 for a, b in zip(c.retention, c.retention[1:], strict=False))
    # the sBG fit reproduces the pooled survivor shares it was fitted on (cohorts of the first six months from P5)
    mag = _a(world, "A1").magnitude
    emp = np.array([1.0] + [mag[f"post_survival_m{k}"] for k in range(1, 7)])
    alpha, beta = mag["sbg_post_alpha"], mag["sbg_post_beta"]
    fit = sbg_survival(alpha, beta, 6) if alpha + beta < 500 else (1 - alpha / (alpha + beta)) ** np.arange(7)
    tol = 0.10 if m["collapse_cause_code"] == 3 else 0.05  # the promo-end churn spike (discount cause) is not sBG-shaped
    assert np.max(np.abs(fit - emp)) < tol


def test_accounting_traces_and_difficulty_ordering(worlds: dict) -> None:
    for s in SEEDS:
        mags = {}
        for d in DIFFS:
            world, _ = worlds[(d, s)]
            P = world.periods
            tb = world.finance.trial_balance
            m = _m(world)
            assert tb[P[-1]]["1350"] > tb[P[3]]["1350"] > 0  # commission asset keeps growing
            assert m["overcapitalized_commissions_cents"] > 0
            assert -tb[P[-1]]["2200"] > 0  # deferred revenue exists
            q = world.operations.queueing
            assert q["L"] == pytest.approx(q["lambda"] * q["W"], rel=1e-6)
            mags[d] = (_a(world, "A1").magnitude["collapse_multiplier"], world.database.total_rows, len(world.ground_truth.red_herrings))
        assert mags["foundation"][0] > mags["practitioner"][0] > mags["principal"][0]
        assert mags["foundation"][1] < mags["practitioner"][1] < mags["principal"][1]
        assert mags["foundation"][2] < mags["practitioner"][2] < mags["principal"][2]


def test_simulation_only_cause_lever_resolves(worlds: dict) -> None:
    world, _ = worlds[("practitioner", 7)]
    levers = world.ground_truth.decision_levers
    model = world.simulation.model
    cause_lever = model["cause_lever"]
    lv = {x.key: x for x in levers}
    q_spend, info = simulate_transition(world.simulation, levers, {"marketing_spend_change_pct": -50})
    assert not info["resolved"] and q_spend.kpis["new_logos"] < model["kpis"]["new_logos"]
    fix = satisfying_decisions(model, levers)
    assert fix[cause_lever] != lv[cause_lever].default and fix["commission_amortization_months"] < lv["commission_amortization_months"].default
    q1, info = simulate_transition(world.simulation, levers, fix)
    assert info["resolved"]
    sim2 = world.simulation.model_copy(update={"history": [q1]})
    q2, _ = simulate_transition(sim2, levers, fix)
    assert q2.kpis["new_cohort_churn_monthly"] < model["kpis"]["new_cohort_churn_monthly"]


def test_funding_explains_the_cash(worlds: dict) -> None:
    """Review P1 #9: the cash pile is a preferred round closed in the warm-up quarter; venture debt drawn at P5 moves
    CFF and the debt line inside the case window, with interest expense from then on."""
    for (d, s), (world, _) in worlds.items():
        P = world.periods
        journal = world.finance.journal
        raises = [e for e in journal if e.source == "equity" and "preferred equity" in e.memo.lower()]
        assert raises and min(e.date for e in raises) < quarter_bounds(P[0])[0], (d, s)  # closed in the warm-up quarter
        draws = [e for e in journal if e.source == "financing" and "venture debt" in e.memo.lower()]
        assert len(draws) == 1 and world.finance.statements.cash_flow[4].financing_cents > 0
        tb = world.finance.trial_balance
        assert -tb[P[3]]["2610"] == 0 and -tb[P[4]]["2610"] > 0
        inc = {i.period: i for i in world.finance.statements.income}
        assert sum(world.finance.trial_balance[P[-1]].get(c, 0) for c in ("7000",)) > 0  # interest expense booked
        assert any("closed" in e.headline and e.period == P[0] for e in world.dossier_seed.timeline)
        assert inc[P[-1]].revenue_cents > 0


def test_reduction_levers_resolve_with_at_most(worlds: dict) -> None:
    """Paid-social share / promo depth / commission life are cut to fix the case (``<=``); the status quo never resolves."""
    for (d, s), (world, _) in worlds.items():
        model = world.simulation.model
        lv = {x.key: x for x in world.ground_truth.decision_levers}
        cond = model["resolved_if"]
        assert cond["commission_amortization_months"] == {"op": "<=", "value": 30.0}
        assert lv["commission_amortization_months"].default == 36
        cause = model["cause_lever"]
        if cause in ("paid_social_share_pct", "promo_depth_pct"):
            assert cond[cause]["op"] == "<=" and cond[cause]["value"] < lv[cause].default
        else:
            assert cause == "onboarding_investment_cents" and isinstance(cond[cause], float)
        assert not all(condition_holds(float(lv[k].default), v) for k, v in cond.items())
        q, info = simulate_transition(world.simulation, world.ground_truth.decision_levers, {})
        assert not info["resolved"]


def test_operations_cohorts_are_the_real_curves(worlds: dict) -> None:
    for (d, s), (world, _) in worlds.items():
        cohorts = world.operations.cohorts
        assert len(cohorts) == 8 and [c.acquisition_period for c in cohorts] == list(world.periods)
        early = [c for c in cohorts if len(c.retention) >= 4]
        assert len(early) >= 5
        for c in cohorts:
            assert c.size > 0 and 0 < c.retention[0] <= 1.0
            assert all(b <= a + 1e-9 for a, b in zip(c.retention, c.retention[1:], strict=False))  # survival is monotone
        m = _m(world)
        pre = [c.retention[3] for c in cohorts[:4] if len(c.retention) > 3]
        post = [c.retention[3] for c in cohorts[4:] if len(c.retention) > 3]
        assert post and np.mean(post) < np.mean(pre)  # the collapse shows in the operations cohorts too
        assert m["retention_m3_post_cohorts"] < m["retention_m3_pre_cohorts"]
