"""Operations primitives (archetype_common.md section 7). Every function is pure: same inputs ->
same outputs, randomness only through an explicit :class:`~axiom.engine.rng.SeededRNG`.

Formulas
--------
* Little's Law: ``L = lambda * W``.
* M/M/1 (per station): ``rho = lambda/mu``; ``Lq = rho^2/(1-rho)``; ``Wq = Lq/lambda``;
  ``W = Wq + 1/mu``; ``L = lambda*W``. ``rho >= 0.98`` is clamped to 0.98 and flagged unstable.
* Kingman (G/G/1): ``Wq ~= (rho/(1-rho)) * ((ca^2 + cs^2)/2) * (1/mu)`` (equals M/M/1 when
  ``ca = cs = 1``).
* Theory of constraints: bottleneck = argmax utilization; throughput = ``min(lambda, min_i c_i)``.
* Shifted beta-geometric (Fader & Hardie 2007): churn probability ``theta ~ Beta(alpha, beta)``;
  ``P(T=t) = B(alpha+1, beta+t-1)/B(alpha, beta)``; survival ``S(t) = B(alpha, beta+t)/B(alpha, beta)``.
* Simple cohort curve (common section 7): ``r(t) = (1-c_early) * s^(t-1)``.
* Bullwhip (Chen, Drezner, Ryan & Simchi-Levi 2000): with a ``p``-period moving-average forecast
  and order-up-to ``S_t = L*D_hat_t (+ z*sigma*sqrt(L))``, ``Var(O)/Var(D) >= 1 + 2L/p + 2L^2/p^2``
  per stage (``L`` = lead time + review period); for ``k`` uncoordinated stages the bound multiplies.
* EOQ ``Q* = sqrt(2DK/h)``; safety stock ``z * sigma_d * sqrt(L)``; order-up-to
  ``S = mu_d*(L+R) + z*sigma_d*sqrt(L+R)``.
* Constant elasticity demand ``Q(p) = Q0 (p/p0)^eps``; Lerner ``(p-c)/p = -1/eps``;
  profit-maximising price ``p* = c * eps/(1+eps)`` for ``eps < -1``.
* Unit economics: ``CAC = S&M/new_customers``; ``LTV = ARPA*GM/churn``; ``payback = CAC/(ARPA*GM)``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import special, stats

from axiom.engine.rng import SeededRNG
from axiom.models import Cohort, OperationsState, Station

__all__ = [
    "littles_law",
    "mm1",
    "kingman_wq",
    "station_network",
    "sbg_survival",
    "sbg_pmf",
    "sbg_generate_lifetimes",
    "sbg_survivor_counts",
    "sbg_fit",
    "simple_retention_curve",
    "cohort_from_curve",
    "chen_bullwhip_bound",
    "bullwhip_simulate",
    "eoq",
    "service_z",
    "safety_stock",
    "order_up_to",
    "constant_elasticity_demand",
    "lerner_markup",
    "optimal_price",
    "pocket_price_waterfall",
    "unit_economics",
    "build_operations_state",
]

logger = logging.getLogger("axiom.operations")

RHO_CLAMP = 0.98


# --------------------------------------------------------------------------- queueing


def littles_law(*, L: float | None = None, lam: float | None = None, W: float | None = None) -> dict[str, float]:
    """Solve ``L = lam * W`` for whichever of the three is ``None`` (exactly one must be)."""
    missing = [k for k, v in (("L", L), ("lam", lam), ("W", W)) if v is None]
    if len(missing) != 1:
        raise ValueError("littles_law needs exactly two of L, lam, W")
    if L is None:
        L = float(lam) * float(W)  # type: ignore[arg-type]
    elif lam is None:
        lam = float(L) / float(W) if W else 0.0  # type: ignore[arg-type]
    else:
        W = float(L) / float(lam) if lam else 0.0
    return {"L": float(L), "lam": float(lam), "W": float(W)}  # type: ignore[arg-type]


def mm1(lam: float, mu: float) -> dict[str, float]:
    """M/M/1 metrics: ``rho, Lq, Wq, W, L, unstable`` (``rho`` clamped at 0.98).

    ``Lq = rho^2/(1-rho)``; ``Wq = Lq/lam``; ``W = Wq + 1/mu``; ``L = lam*W``.
    """
    if mu <= 0:
        raise ValueError("service rate must be positive")
    rho_raw = lam / mu
    unstable = rho_raw >= RHO_CLAMP
    rho = min(rho_raw, RHO_CLAMP)
    lam_eff = rho * mu
    lq = rho * rho / (1 - rho)
    wq = lq / lam_eff if lam_eff > 0 else 0.0
    w = wq + 1.0 / mu
    return {"lambda": float(lam), "mu": float(mu), "rho": float(rho), "rho_raw": float(rho_raw), "Lq": float(lq),
            "Wq": float(wq), "W": float(w), "L": float(lam_eff * w), "unstable": float(unstable)}


def kingman_wq(lam: float, mu: float, ca: float = 1.0, cs: float = 1.0) -> float:
    """Kingman's G/G/1 waiting time ``Wq ~= (rho/(1-rho)) * ((ca^2+cs^2)/2) * (1/mu)`` (rho clamped)."""
    rho = min(lam / mu, RHO_CLAMP)
    return float((rho / (1 - rho)) * ((ca * ca + cs * cs) / 2.0) / mu)


def station_network(lam: float, stations: Sequence[Mapping[str, Any]], *, ca: float = 1.0) -> tuple[list[Station], dict[str, float | str]]:
    """Serial line of stations, each ``{"name", "capacity_per_hour", "cs"?}``.

    Per station: ``rho_i = lam/c_i``; waiting via Kingman with the station's service CV ``cs``
    (default 1 -> M/M/1); ``cycle_time = Wq + 1/c``; ``wip = lam_eff * cycle_time`` (Little).
    Returns ``(stations, summary)`` where summary has ``bottleneck``, ``throughput =
    min(lam, min c_i)``, ``total_wip`` and ``total_cycle_time``.
    """
    out: list[Station] = []
    total_wip = total_ct = 0.0
    for spec in stations:
        c = float(spec["capacity_per_hour"])
        cs = float(spec.get("cs", 1.0))
        rho = min(lam / c, RHO_CLAMP)
        wq = kingman_wq(lam, c, ca, cs)
        ct = wq + 1.0 / c
        wip = rho * c * ct
        total_wip += wip
        total_ct += ct
        out.append(Station(name=str(spec["name"]), capacity_per_hour=c, utilization=float(lam / c), wip=float(wip), cycle_time=float(ct)))
    bottleneck = max(out, key=lambda s: s.utilization).name if out else ""
    throughput = min([lam] + [s.capacity_per_hour for s in out])
    return out, {"bottleneck": bottleneck, "throughput": float(throughput), "total_wip": float(total_wip), "total_cycle_time": float(total_ct)}


# --------------------------------------------------------------------------- cohorts


def sbg_survival(alpha: float, beta: float, horizon: int) -> NDArray[np.float64]:
    """``S(t) = B(alpha, beta+t)/B(alpha, beta)`` for ``t = 0..horizon`` (``S(0) = 1``)."""
    t = np.arange(horizon + 1, dtype=float)
    return np.exp(special.betaln(alpha, beta + t) - special.betaln(alpha, beta))


def sbg_pmf(alpha: float, beta: float, horizon: int) -> NDArray[np.float64]:
    """``P(T=t) = B(alpha+1, beta+t-1)/B(alpha, beta)`` for ``t = 1..horizon``."""
    t = np.arange(1, horizon + 1, dtype=float)
    return np.exp(special.betaln(alpha + 1, beta + t - 1) - special.betaln(alpha, beta))


def sbg_generate_lifetimes(rng: SeededRNG, alpha: float, beta: float, n: int, horizon: int) -> NDArray[np.int64]:
    """Customer lifetimes (periods survived, censored at ``horizon``): ``theta_i ~ Beta(alpha, beta)``,
    churn period ``T_i ~ Geometric(theta_i)``; returns ``min(T_i - 1, horizon)`` (0 = churned at first renewal)."""
    g = rng.generator
    theta = g.beta(alpha, beta, n)
    t = g.geometric(np.clip(theta, 1e-12, 1.0))
    return np.minimum(t - 1, horizon).astype(np.int64)


def sbg_survivor_counts(lifetimes: ArrayLike, horizon: int) -> NDArray[np.int64]:
    """``n_t`` = customers still active after ``t`` periods, ``t = 0..horizon``."""
    lt = np.asarray(lifetimes)
    return np.array([(lt >= t).sum() for t in range(horizon + 1)], dtype=np.int64)


def sbg_fit(survivors: ArrayLike) -> dict[str, float]:
    """Maximum-likelihood ``(alpha, beta)`` from survivor counts ``n_0..n_T`` (scipy; ground-truth
    builder only). ``LL = sum_t (n_{t-1}-n_t) log P(T=t) + n_T log S(T)``."""
    from scipy import optimize

    n = np.asarray(survivors, dtype=float)
    horizon = len(n) - 1
    lost = n[:-1] - n[1:]

    def nll(x: NDArray[np.float64]) -> float:
        a, b = np.exp(x)
        p = sbg_pmf(a, b, horizon)
        s = sbg_survival(a, b, horizon)[-1]
        return -float(np.sum(lost * np.log(np.maximum(p, 1e-300))) + n[-1] * math.log(max(s, 1e-300)))

    best = None
    for x0 in ((0.0, 0.0), (math.log(0.5), math.log(3.0)), (math.log(2.0), math.log(10.0))):
        res = optimize.minimize(nll, np.array(x0), method="Nelder-Mead", options={"xatol": 1e-8, "fatol": 1e-8, "maxiter": 4000})
        if best is None or res.fun < best.fun:
            best = res
    a, b = np.exp(best.x)  # type: ignore[union-attr]
    return {"alpha": float(a), "beta": float(b), "neg_log_likelihood": float(best.fun)}  # type: ignore[union-attr]


def simple_retention_curve(c_early: float, s: float, horizon: int) -> list[float]:
    """``r(0)=1``, ``r(t) = (1-c_early) * s^(t-1)`` for ``t >= 1`` (common section 7)."""
    return [1.0] + [float((1 - c_early) * s ** (t - 1)) for t in range(1, horizon + 1)]


def cohort_from_curve(acquisition_period: str, size: int, retention: Sequence[float], arpa_cents: float, expansion: float = 0.0) -> Cohort:
    """Cohort with revenue ``size * r(t) * arpa * (1+expansion)^t`` (integer cents per period)."""
    rev = [int(round(size * r * arpa_cents * (1 + expansion) ** t)) for t, r in enumerate(retention)]
    return Cohort(acquisition_period=acquisition_period, size=int(size), retention=[float(r) for r in retention], revenue_cents=rev)


# --------------------------------------------------------------------------- supply chain


def chen_bullwhip_bound(lead_time: float, p: int) -> float:
    """Chen et al. (2000) lower bound ``1 + 2L/p + 2L^2/p^2`` for one stage."""
    return float(1 + 2 * lead_time / p + 2 * lead_time * lead_time / (p * p))


def bullwhip_simulate(rng: SeededRNG, *, periods: int, mean_demand: float, sd_demand: float, lead_times: Sequence[int],
                      p: int, z: float = 0.0, warmup: int | None = None) -> dict[str, Any]:
    """Multi-tier order-up-to simulation with moving-average forecasts.

    Tier ``k`` sees demand ``D_k`` (tier 0 = iid normal consumer demand; tier ``k+1`` sees tier
    ``k``'s orders), forecasts ``D_hat_t = mean(D_{t-p..t-1})`` and ``sigma_hat`` likewise, sets
    ``S_t = L_k*D_hat_t + z*sigma_hat*sqrt(L_k)`` and orders ``O_t = S_t - S_{t-1} + D_{t-1}``
    (negative orders allowed, as in Chen et al.). Returns per-tier variance ratios, the
    cumulative ratio and the Chen bound per tier / cumulative.
    """
    g = rng.generator
    w = warmup if warmup is not None else 2 * p + 5
    demand = g.normal(mean_demand, sd_demand, periods + w)
    series = [demand]
    tiers = []
    cumulative_bound = 1.0
    d = demand
    for lt in lead_times:
        n = len(d)
        s_prev = None
        orders = np.zeros(n)
        for t in range(p, n):
            window = d[t - p:t]
            s_t = lt * window.mean() + (z * window.std(ddof=1) * math.sqrt(lt) if z else 0.0)
            if s_prev is not None:
                orders[t] = s_t - s_prev + d[t - 1]
            else:
                orders[t] = d[t - 1]
            s_prev = s_t
        o = orders[w:]
        din = d[w:]
        ratio = float(np.var(o, ddof=1) / np.var(din, ddof=1))
        bound = chen_bullwhip_bound(lt, p)
        cumulative_bound *= bound
        tiers.append({"lead_time": int(lt), "variance_ratio": ratio, "chen_bound": bound,
                      "cumulative_ratio": float(np.var(o, ddof=1) / np.var(demand[w:], ddof=1)), "cumulative_bound": cumulative_bound})
        series.append(orders)
        d = orders
    return {"tiers": tiers, "p": p, "z": z, "periods": periods}


def eoq(annual_demand: float, order_cost: float, holding_cost_per_unit_year: float) -> float:
    """``Q* = sqrt(2*D*K/h)``."""
    if holding_cost_per_unit_year <= 0:
        raise ValueError("holding cost must be positive")
    return float(math.sqrt(2 * annual_demand * order_cost / holding_cost_per_unit_year))


def service_z(service_level: float) -> float:
    """Standard-normal quantile for a cycle service level."""
    return float(stats.norm.ppf(min(max(service_level, 1e-6), 1 - 1e-6)))


def safety_stock(z: float, sigma_d: float, lead_time: float) -> float:
    """``SS = z * sigma_d * sqrt(L)``."""
    return float(z * sigma_d * math.sqrt(max(lead_time, 0.0)))


def order_up_to(mean_d: float, sigma_d: float, lead_time: float, z: float, review: float = 1.0) -> float:
    """``S = mean_d*(L+R) + z*sigma_d*sqrt(L+R)``."""
    horizon = lead_time + review
    return float(mean_d * horizon + z * sigma_d * math.sqrt(max(horizon, 0.0)))


# --------------------------------------------------------------------------- pricing


def constant_elasticity_demand(q0: float, p0: float, p: float | NDArray[np.float64], eps: float) -> float | NDArray[np.float64]:
    """``Q(p) = Q0 * (p/p0)^eps`` (eps < 0)."""
    return q0 * (np.asarray(p, dtype=float) / p0) ** eps if isinstance(p, np.ndarray) else float(q0 * (p / p0) ** eps)


def lerner_markup(eps: float) -> float:
    """Profit-maximising margin ``(p-c)/p = -1/eps`` (defined for ``eps < -1``)."""
    if eps >= -1:
        raise ValueError("Lerner markup needs elastic demand (eps < -1)")
    return float(-1.0 / eps)


def optimal_price(unit_cost: float, eps: float) -> float:
    """``p* = c * eps / (1 + eps)`` for ``eps < -1``."""
    if eps >= -1:
        raise ValueError("optimal price needs eps < -1")
    return float(unit_cost * eps / (1 + eps))


def pocket_price_waterfall(list_price: float, deductions: Mapping[str, float]) -> dict[str, Any]:
    """Price waterfall from list price through ordered deductions (each a fraction of list price
    when ``<= 1``, else an absolute amount). Returns steps, pocket price and leakage share."""
    steps = [{"step": "list_price", "amount": float(list_price), "running": float(list_price)}]
    running = float(list_price)
    for name, v in deductions.items():
        amt = float(v) * list_price if abs(v) <= 1 else float(v)
        running -= amt
        steps.append({"step": name, "amount": -amt, "running": running})
    return {"steps": steps, "pocket_price": running, "leakage_pct": (list_price - running) / list_price if list_price else 0.0}


# --------------------------------------------------------------------------- unit economics


def unit_economics(*, sm_spend_cents: float, new_customers: float, arpa_monthly_cents: float, gross_margin: float,
                   churn_monthly: float) -> dict[str, float]:
    """``CAC = S&M/new``; ``LTV = ARPA*GM/churn``; ``payback_months = CAC/(ARPA*GM)``; ``ltv_to_cac``."""
    cac = sm_spend_cents / new_customers if new_customers else 0.0
    margin = arpa_monthly_cents * gross_margin
    ltv = margin / churn_monthly if churn_monthly > 0 else 0.0
    payback = cac / margin if margin > 0 else 0.0
    return {"cac": float(cac), "ltv": float(ltv), "payback_months": float(payback), "ltv_to_cac": float(ltv / cac) if cac else 0.0,
            "arpa": float(arpa_monthly_cents), "gross_margin": float(gross_margin), "churn_monthly": float(churn_monthly)}


# --------------------------------------------------------------------------- state assembly


def _clean(obj: Any) -> Any:
    """Recursively convert numpy scalars/arrays to Python and replace non-finite floats with 0.0."""
    if isinstance(obj, Mapping):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_clean(v) for v in obj.tolist()]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return round(f, 10) if math.isfinite(f) else 0.0
    return obj


def build_operations_state(
    *,
    stations: Sequence[Station] = (),
    queueing: Mapping[str, float] | None = None,
    unit_economics: Mapping[str, float] | None = None,
    cohorts: Sequence[Cohort] = (),
    supply_chain: Mapping[str, Any] | None = None,
    pricing: Mapping[str, Any] | None = None,
    kpis_by_period: Mapping[str, Mapping[str, float]] | None = None,
) -> OperationsState:
    """Assemble an :class:`OperationsState`, coercing numpy values and non-finite floats so the
    result serialises deterministically."""
    return OperationsState(
        stations=list(stations),
        queueing={k: float(v) for k, v in _clean(dict(queueing or {})).items()},
        unit_economics={k: float(v) for k, v in _clean(dict(unit_economics or {})).items()},
        cohorts=list(cohorts),
        supply_chain=_clean(dict(supply_chain or {})),
        pricing=_clean(dict(pricing or {})),
        kpis_by_period={p: {k: float(v) for k, v in _clean(dict(kp)).items()} for p, kp in (kpis_by_period or {}).items()},
    )
