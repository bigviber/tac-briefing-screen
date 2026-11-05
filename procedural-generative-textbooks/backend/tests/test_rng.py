"""Tests for axiom.engine.rng."""

from __future__ import annotations

import datetime as dt
import hashlib

import numpy as np
import pytest
from scipy import stats

from axiom.engine.rng import (
    COMPANY_STEMS,
    COMPANY_SUFFIXES,
    FIRST_NAMES,
    LAST_NAMES,
    SeededRNG,
    benchmark_quantile,
    derive_seed,
    empirical_knots,
    fit_lognormal,
    hash_seed,
)
from axiom.models import Benchmark


def _bench(dist: str = "empirical", **kw: float) -> Benchmark:
    qs = {"p10": 10.0, "p25": 14.0, "p50": 20.0, "p75": 30.0, "p90": 45.0} | kw
    return Benchmark(key=f"t.{dist}", industry="any", metric="m", unit="x", distribution=dist, source="test", **qs)


def test_hash_seed_is_blake2b_and_stable() -> None:
    expected = int.from_bytes(hashlib.blake2b(b"axiom|forensic_revenue|42|1", digest_size=8).digest(), "big")
    assert hash_seed("forensic_revenue", 42, "1") == expected
    assert hash_seed("a", 1, "1") != hash_seed("a", 2, "1")
    assert hash_seed("a", 1, "1") != hash_seed("b", 1, "1")
    assert hash_seed("a", 1, "1") != hash_seed("a", 1, "2")


def test_same_seed_same_sequence() -> None:
    a = SeededRNG.for_world("cohort_collapse", 7, "1")
    b = SeededRNG.for_world("cohort_collapse", 7, "1")
    seq_a = [a.uniform(), a.normal(), a.integers(0, 1000), a.poisson(3.0), a.pareto(2.0), a.choice(["x", "y", "z"])]
    seq_b = [b.uniform(), b.normal(), b.integers(0, 1000), b.poisson(3.0), b.pareto(2.0), b.choice(["x", "y", "z"])]
    assert seq_a == seq_b
    np.testing.assert_array_equal(a.normal(size=50), b.normal(size=50))
    c = SeededRNG.for_world("cohort_collapse", 8, "1")
    assert c.uniform() != SeededRNG.for_world("cohort_collapse", 7, "1").uniform()


def test_spawn_independent_of_draw_order() -> None:
    root1 = SeededRNG.for_world("bullwhip", 3, "1")
    fin1, dat1 = root1.spawn("finance"), root1.spawn("datagen")
    b_first = dat1.uniform(size=20)

    root2 = SeededRNG.for_world("bullwhip", 3, "1")
    root2.uniform(size=1000)  # draws on the parent do not matter
    fin2 = root2.spawn("finance")
    fin2.normal(size=500)  # extra draws in stream A
    dat2 = root2.spawn("datagen")
    np.testing.assert_array_equal(dat2.uniform(size=20), b_first)

    # grandchildren are deterministic too, and names matter
    assert fin1.spawn("x").seed == root2.spawn("finance").spawn("x").seed
    assert fin1.spawn("x").seed != fin1.spawn("y").seed
    assert root1.spawn("finance").seed == derive_seed(root1.seed, "finance")


def test_empirical_quantile_monotone_and_hits_knots() -> None:
    b = _bench()
    u = np.linspace(0, 1, 2001)
    q = benchmark_quantile(b, u)
    assert np.all(np.diff(q) >= 0)
    for p, val in zip([0.10, 0.25, 0.50, 0.75, 0.90], b.percentiles(), strict=True):
        assert benchmark_quantile(b, p) == pytest.approx(val)
    lo = b.p10 - (b.p25 - b.p10)
    hi = b.p90 + (b.p90 - b.p75)
    assert q.min() >= lo - 1e-9 and q.max() <= hi + 1e-9
    probs, values = empirical_knots(*b.percentiles())
    assert values[0] == pytest.approx(lo) and values[-1] == pytest.approx(hi)
    assert list(probs) == [0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 1.0]


def test_empirical_samples_stay_in_tails() -> None:
    b = _bench()
    rng = SeededRNG(123)
    draws = rng.sample_benchmark_n(b, 5000)
    assert draws.min() >= 6.0 - 1e-9 and draws.max() <= 60.0 + 1e-9
    assert np.median(draws) == pytest.approx(20.0, rel=0.05)


def test_lognormal_fit_recovers_p50() -> None:
    mu, sigma = np.log(250.0), 0.6
    z = stats.norm.ppf([0.10, 0.50, 0.90])
    p10, p50, p90 = np.exp(mu + sigma * z)
    fmu, fsigma = fit_lognormal(p10, p50, p90)
    assert np.exp(fmu) == pytest.approx(p50, rel=0.01)
    assert fsigma == pytest.approx(sigma, rel=0.01)

    rng = SeededRNG(99)
    draws = rng.lognormal_from_percentiles(p10, p50, p90, size=40000)
    assert np.median(draws) == pytest.approx(p50, rel=0.01)

    zs = stats.norm.ppf([0.10, 0.25, 0.50, 0.75, 0.90])
    qs = np.exp(mu + sigma * zs)
    b = Benchmark(key="t.ln", metric="m", unit="usd", distribution="lognormal", source="test",
                  p10=qs[0], p25=qs[1], p50=qs[2], p75=qs[3], p90=qs[4])
    assert float(benchmark_quantile(b, 0.5)) == pytest.approx(p50, rel=0.01)
    assert float(np.median(rng.sample_benchmark_n(b, 40000))) == pytest.approx(p50, rel=0.01)


@pytest.mark.parametrize("dist", ["lognormal", "normal", "beta", "triangular", "empirical"])
def test_all_distributions_sample_finite_and_monotone(dist: str) -> None:
    if dist == "beta":
        b = _bench(dist, p10=0.2, p25=0.3, p50=0.4, p75=0.5, p90=0.6)
    else:
        b = _bench(dist, p10=10.0, p25=15.0, p50=20.0, p75=25.0, p90=30.0)
    q = benchmark_quantile(b, np.linspace(0.001, 0.999, 500))
    assert np.all(np.isfinite(q)) and np.all(np.diff(q) >= -1e-12)
    rng = SeededRNG(5)
    vals = [rng.sample_benchmark(b) for _ in range(200)]
    assert all(np.isfinite(vals))
    assert np.median(vals) == pytest.approx(b.p50, rel=0.15)


def test_unfittable_family_falls_back_to_empirical() -> None:
    b = _bench("lognormal", p10=-5.0, p25=-1.0, p50=0.0, p75=1.0, p90=5.0)
    assert float(benchmark_quantile(b, 0.5)) == pytest.approx(0.0)
    flat = _bench("normal", p10=3.0, p25=3.0, p50=3.0, p75=3.0, p90=3.0)
    assert SeededRNG(1).sample_benchmark(flat) == 3.0


def test_helpers() -> None:
    rng = SeededRNG(2024)
    betas = rng.beta_from_mean_sd(0.3, 0.1, size=20000)
    assert np.mean(betas) == pytest.approx(0.3, abs=0.01)
    assert np.std(betas) == pytest.approx(0.1, abs=0.01)
    tri = rng.triangular(1.0, 2.0, 4.0, size=1000)
    assert tri.min() >= 1.0 and tri.max() <= 4.0
    par = rng.pareto(1.5, xm=10.0, size=1000)
    assert par.min() >= 10.0
    counts = {"a": 0, "b": 0}
    for _ in range(4000):
        counts[rng.weighted_choice(["a", "b"], [0.9, 0.1])] += 1
    assert 0.86 < counts["a"] / 4000 < 0.94
    base = dt.date(2024, 3, 15)
    jitters = {rng.date_jitter(base, 5) for _ in range(300)}
    assert min(jitters) >= base - dt.timedelta(days=5) and max(jitters) <= base + dt.timedelta(days=5)
    assert all(d >= base for d in (rng.date_jitter(base, 3, forward_only=True) for _ in range(50)))
    assert sorted(rng.shuffled([1, 2, 3, 4])) == [1, 2, 3, 4]
    assert len(set(rng.sample(list(range(10)), 5))) == 5
    with pytest.raises(ValueError):
        rng.weighted_choice(["a"], [0.0])


def test_name_generation_deterministic_and_lists_large() -> None:
    assert len(set(FIRST_NAMES)) >= 80
    assert len(set(LAST_NAMES)) >= 80
    assert len(set(COMPANY_STEMS)) + len(set(COMPANY_SUFFIXES)) >= 60
    a, b = SeededRNG(11), SeededRNG(11)
    assert [a.company_name("saas") for _ in range(5)] == [b.company_name("saas") for _ in range(5)]
    names = SeededRNG(3).pick_names(50)
    assert len(set(names)) == 50
    assert SeededRNG.ticker_like("Kestrel Logistics") == "KEL"
    assert SeededRNG.ticker_like("Northwind Supply Chain") == "NSC"
    assert len(SeededRNG.ticker_like("Atlas")) >= 3
