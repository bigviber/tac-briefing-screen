"""Operations primitives (archetype_common.md section 7)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from axiom.engine.operations import (
    bullwhip_simulate,
    build_operations_state,
    chen_bullwhip_bound,
    constant_elasticity_demand,
    eoq,
    kingman_wq,
    lerner_markup,
    littles_law,
    mm1,
    optimal_price,
    order_up_to,
    pocket_price_waterfall,
    safety_stock,
    sbg_fit,
    sbg_generate_lifetimes,
    sbg_survival,
    sbg_survivor_counts,
    service_z,
    simple_retention_curve,
    station_network,
    unit_economics,
)
from axiom.engine.rng import SeededRNG


def test_littles_law_identities() -> None:
    assert littles_law(lam=4.0, W=2.5)["L"] == pytest.approx(10.0)
    assert littles_law(L=10.0, W=2.5)["lam"] == pytest.approx(4.0)
    assert littles_law(L=10.0, lam=4.0)["W"] == pytest.approx(2.5)
    q = mm1(0.7, 1.0)
    assert q["L"] == pytest.approx(q["lambda"] * q["W"])
    assert q["Lq"] == pytest.approx(q["lambda"] * q["Wq"])
    assert q["L"] == pytest.approx(0.7 / 0.3)  # M/M/1: L = rho/(1-rho)
    with pytest.raises(ValueError):
        littles_law(L=1.0)


def test_mm1_clamps_unstable() -> None:
    q = mm1(1.2, 1.0)
    assert q["rho"] == pytest.approx(0.98) and q["unstable"] == 1.0 and q["rho_raw"] == pytest.approx(1.2)


def test_kingman_equals_mm1_at_unit_cv() -> None:
    for lam, mu in ((0.5, 1.0), (3.0, 4.0), (0.9, 1.0)):
        assert kingman_wq(lam, mu, 1.0, 1.0) == pytest.approx(mm1(lam, mu)["Wq"])
    assert kingman_wq(0.8, 1.0, 0.5, 0.5) == pytest.approx(0.25 * mm1(0.8, 1.0)["Wq"])


def test_station_network_bottleneck() -> None:
    st, summary = station_network(10.0, [{"name": "a", "capacity_per_hour": 20}, {"name": "b", "capacity_per_hour": 11},
                                         {"name": "c", "capacity_per_hour": 15}])
    assert summary["bottleneck"] == "b" and summary["throughput"] == 10.0
    for s in st:
        assert s.wip == pytest.approx(min(10.0 / s.capacity_per_hour, 0.98) * s.capacity_per_hour * s.cycle_time)


def test_sbg_fit_recovers_parameters() -> None:
    alpha, beta = 0.8, 3.0
    lifetimes = sbg_generate_lifetimes(SeededRNG(11), alpha, beta, 5000, 24)
    fit = sbg_fit(sbg_survivor_counts(lifetimes, 24))
    assert abs(fit["alpha"] - alpha) / alpha < 0.10
    assert abs(fit["beta"] - beta) / beta < 0.10
    s = sbg_survival(alpha, beta, 12)
    assert s[0] == 1.0 and np.all(np.diff(s) < 0)


def test_sbg_fit_near_geometric_is_bounded_and_warning_free() -> None:
    """Homogeneous churn (geometric survivors): the unbounded MLE runs to alpha, beta -> inf; the shared
    fitter stays inside its bounds, emits no warnings and keeps the mean churn of the geometric MLE."""
    import warnings

    from axiom.engine.operations import SBG_PARAM_BOUNDS

    lo, hi = SBG_PARAM_BOUNDS
    cases = [np.round(1000 * 0.97 ** np.arange(13)).astype(int),  # exactly geometric, 3% churn
             np.round(250 * 0.9 ** np.arange(7)).astype(int)]
    g = SeededRNG(3).generator
    cases.append(sbg_survivor_counts(np.minimum(g.geometric(0.025, 4000) - 1, 12), 12))  # sampled, theta = 2.5%
    for i, surv in enumerate(cases):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with np.errstate(all="raise"):
                fit = sbg_fit(surv)
        a, b = fit["alpha"], fit["beta"]
        assert np.isfinite(a) and np.isfinite(b) and np.isfinite(fit["neg_log_likelihood"])
        assert lo <= a <= hi and lo <= b <= hi
        theta = (surv[0] - surv[-1]) / surv[:-1].sum()  # geometric MLE
        if i == 0:  # geometric to rounding: the fallback keeps the geometric MLE's mean churn
            assert fit["geometric_limit"] is True
            assert fit["theta_mean"] == pytest.approx(theta, rel=1e-9)
        assert fit["theta_mean"] == pytest.approx(theta, rel=0.05)
        proj = sbg_survival(a, b, 36)
        assert np.all(np.isfinite(proj)) and np.all((proj >= 0) & (proj <= 1)) and np.all(np.diff(proj) <= 0)
        assert proj[len(surv) - 1] == pytest.approx(surv[-1] / surv[0], rel=0.05)
    # heterogeneous data stays an interior fit
    het = sbg_fit(sbg_survivor_counts(sbg_generate_lifetimes(SeededRNG(11), 0.8, 3.0, 5000, 24), 24))
    assert het["geometric_limit"] is False and lo < het["alpha"] < hi and lo < het["beta"] < hi


def test_simple_retention_curve() -> None:
    r = simple_retention_curve(0.3, 0.9, 4)
    assert r == pytest.approx([1.0, 0.7, 0.63, 0.567, 0.5103])


def test_bullwhip_respects_chen_bound() -> None:
    res = bullwhip_simulate(SeededRNG(5), periods=6000, mean_demand=100, sd_demand=10, lead_times=[2, 3], p=4)
    for tier in res["tiers"]:
        assert tier["variance_ratio"] >= 0.95 * tier["chen_bound"]
    assert res["tiers"][-1]["cumulative_ratio"] >= 0.9 * res["tiers"][-1]["cumulative_bound"]
    assert chen_bullwhip_bound(2, 4) == pytest.approx(1 + 1 + 0.5)


def test_inventory_formulas() -> None:
    assert eoq(1200, 50, 6) == pytest.approx(math.sqrt(2 * 1200 * 50 / 6))
    z = service_z(0.95)
    assert z == pytest.approx(1.6449, abs=1e-3)
    assert safety_stock(z, 10, 4) == pytest.approx(z * 20)
    assert order_up_to(10, 2, 3, z, review=1) == pytest.approx(40 + z * 2 * 2)


def test_pricing_and_unit_economics() -> None:
    assert constant_elasticity_demand(100, 10, 11, -2.0) == pytest.approx(100 * 1.1 ** -2)
    assert lerner_markup(-4.0) == pytest.approx(0.25)
    assert optimal_price(6.0, -3.0) == pytest.approx(9.0)
    wf = pocket_price_waterfall(100, {"invoice_discount": 0.05, "rebate": 0.03, "freight": 2.0})
    assert wf["pocket_price"] == pytest.approx(90.0) and wf["leakage_pct"] == pytest.approx(0.10)
    ue = unit_economics(sm_spend_cents=1_000_000, new_customers=100, arpa_monthly_cents=1000, gross_margin=0.8, churn_monthly=0.02)
    assert ue["cac"] == 10_000 and ue["ltv"] == pytest.approx(40_000) and ue["payback_months"] == pytest.approx(12.5)


def test_build_operations_state_cleans_values() -> None:
    st = build_operations_state(queueing={"x": np.float64(float("nan")), "y": np.int64(3)}, supply_chain={"a": np.array([1.0, np.inf])},
                                kpis_by_period={"2024Q1": {"k": np.float32(1.5)}})
    assert st.queueing == {"x": 0.0, "y": 3.0} and st.supply_chain == {"a": [1.0, 0.0]} and st.kpis_by_period["2024Q1"]["k"] == 1.5
