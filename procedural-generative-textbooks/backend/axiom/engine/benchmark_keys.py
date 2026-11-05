"""Logical benchmark keys -> corpus benchmarks.

The archetype spec (``docs/research/archetypes.md``) names benchmarks with *logical* keys such as
``saas.dso``, ``<ind>.gross_margin``, ``3pl.lead_time_ltl_days`` or
``operations.elasticity_apparel``. The research corpus uses its own, more specific naming
(``fin.saas.dso_days``, ``ops.lead_time_days.ltl``, ``ops.price_elasticity.apparel`` ...).
:func:`resolve` bridges the two with a documented, deterministic resolution ladder:

1. **exact** - the logical key itself exists in the pool.
2. **alias** - a hand-curated :data:`ALIASES` entry (semantic renames that no mechanical rewrite
   can find, e.g. ``rd_pct_revenue -> rnd_pct_revenue``, ``oee -> ops.oee.discrete_manufacturing``).
3. **rewrite** - candidate keys built mechanically, tried in order::

       fin.<ind>.<metric>, fin.<ind>.<metric>_days, ops.<metric>, ops.<ind>.<metric>,
       ops.<metric>.<ind>, saas.<metric>, saas.<metric>.<segment>, forensic.<metric>,
       any.<metric>, fin.any.<metric>

   ``<segment>`` is a trailing ``_smb`` / ``_midmarket`` / ``_enterprise`` / ``_early`` / ``_scale``
   suffix split off the metric (``cac_payback_months_smb -> saas.cac_payback_months.smb``).
4. **search** - ``pool.search(industry, token, include_any=True)`` for every metric token; a
   candidate qualifies only if *all* metric tokens occur among its key tokens; best = fewest extra
   tokens, then industry-specific over ``any``, then key order.
5. **fallback** - :data:`FALLBACKS`, conservative defaults with ``source="fallback default"``;
   a warning is logged once per (key, industry).

Logical keys may start with an industry code, ``<ind>`` (replaced by ``industry``),
``operations`` (cross-industry, i.e. ``ops``), ``forensic`` or ``any``; a bare metric
(``"dso"``) is read as ``<ind>.dso``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from axiom.engine.market_research import BenchmarkPool, get_default_pool
from axiom.engine.rng import SeededRNG, benchmark_quantile
from axiom.models import Benchmark

__all__ = [
    "ALIASES",
    "FALLBACKS",
    "KNOWN_INDUSTRIES",
    "Resolution",
    "resolve",
    "resolve_with_trace",
    "sample",
    "sample_between",
    "explain",
    "is_fallback",
]

logger = logging.getLogger("axiom.benchmark_keys")

KNOWN_INDUSTRIES: tuple[str, ...] = ("saas", "ecommerce", "manufacturing", "3pl", "retail", "fintech_marketplace")
_SCOPES: tuple[str, ...] = ("operations", "ops", "forensic", "any", "fin")
_SEGMENTS: tuple[str, ...] = ("smb", "midmarket", "enterprise", "early", "scale")
# industry code -> short code used inside ops.* keys of the corpus
_OPS_INDUSTRY: dict[str, str] = {"fintech_marketplace": "fintech"}

# (scope or industry, metric) -> corpus key. ``*`` matches any industry.
ALIASES: dict[tuple[str, str], str] = {
    ("*", "rd_pct_revenue"): "fin.{ind}.rnd_pct_revenue",
    ("saas", "sm_pct_revenue"): "saas.sales_marketing_pct_revenue",
    ("saas", "gross_churn_monthly_smb"): "saas.gross_logo_churn_monthly.smb",
    ("saas", "gross_churn_monthly_midmarket"): "saas.gross_logo_churn_monthly.midmarket",
    ("saas", "gross_churn_monthly_enterprise"): "saas.gross_logo_churn_monthly.enterprise",
    ("saas", "net_revenue_retention"): "saas.net_revenue_retention_annual",
    ("saas", "ltv_cac"): "saas.ltv_to_cac",
    ("saas", "arpa_monthly"): "saas.arpa_monthly_usd.midmarket",
    ("saas", "deferred_revenue_pct_of_arr"): "saas.deferred_revenue_pct_revenue",
    ("ecommerce", "inventory_turns"): "ops.inventory_turns.ecommerce_dtc",
    ("retail", "inventory_turns"): "ops.inventory_turns.retail_general",
    ("retail", "shrink_rate"): "fin.retail.inventory_shrink_pct_sales",
    ("ecommerce", "repeat_purchase_rate"): "ops.retention.ecommerce_dtc.repeat_rate_12m",
    ("ecommerce", "return_rate"): "fin.ecommerce.return_rate",
    ("retail", "return_rate"): "ops.ecommerce.return_rate.general",
    ("manufacturing", "oee"): "ops.oee.discrete_manufacturing",
    ("manufacturing", "bottleneck_utilization"): "ops.bottleneck_utilization",
    ("manufacturing", "scrap_rate"): "ops.scrap_rate",
    ("manufacturing", "setup_time_minutes"): "ops.changeover_minutes.generic",
    ("manufacturing", "overhead_pct_conversion"): "fin.manufacturing.overhead_pct_cogs",
    ("3pl", "lead_time_parcel_days"): "ops.lead_time_days.parcel_ground",
    ("3pl", "lead_time_ltl_days"): "ops.lead_time_days.ltl",
    ("3pl", "lead_time_ftl_days"): "ops.lead_time_days.ftl",
    ("3pl", "lead_time_ocean_days"): "ops.lead_time_days.ocean_door_to_door",
    ("3pl", "lead_time_air_days"): "ops.lead_time_days.air_freight",
    ("3pl", "lead_time_parcel_days_cv"): "ops.lead_time_cv.parcel",
    ("3pl", "lead_time_ltl_days_cv"): "ops.lead_time_cv.ltl",
    ("3pl", "lead_time_ocean_days_cv"): "ops.lead_time_cv.ocean",
    ("3pl", "otif"): "ops.otif.cpg_to_big_box",
    ("3pl", "fill_rate"): "ops.fill_rate.line",
    ("3pl", "pick_rate_lines_per_hour"): "ops.warehouse.lines_picked_per_labor_hour",
    ("ops", "bullwhip_ratio_empirical"): "ops.bullwhip_amplification_per_tier",
    ("ops", "elasticity_grocery"): "ops.price_elasticity.grocery_cpg",
    ("ops", "elasticity_apparel"): "ops.price_elasticity.apparel",
    ("ops", "elasticity_software"): "ops.price_elasticity.software_b2b",
    ("ops", "elasticity_durables"): "ops.price_elasticity.durables",
    ("ops", "cohort_retention_subscription_m1"): "ops.retention.subscription_b2c.m1",
    ("ops", "cohort_retention_subscription_m3"): "ops.retention.subscription_b2c.m3",
    ("ops", "cohort_retention_subscription_m6"): "ops.retention.subscription_b2c.m6",
    ("ops", "cohort_retention_subscription_m12"): "ops.retention.subscription_b2c.m12",
    ("fintech_marketplace", "take_rate"): "fin.fintech_marketplace.take_rate",
    ("fintech_marketplace", "chargeback_rate"): "ops.fintech.chargeback_rate",
    ("fintech_marketplace", "dispute_win_rate"): "ops.fintech.dispute_win_rate",
    ("*", "sloan_accrual_ratio"): "fin.{ind}.accrual_ratio_sloan",
}


def _fb(metric: str, unit: str, p10: float, p25: float, p50: float, p75: float, p90: float, notes: str) -> Benchmark:
    return Benchmark(
        key=f"fallback.{metric}", industry="any", metric=metric, unit=unit,
        p10=p10, p25=p25, p50=p50, p75=p75, p90=p90, distribution="empirical",
        source="fallback default", source_url=None, as_of="", notes=notes,
    )


# Metric name (after scope/industry/segment stripping) -> conservative cross-industry default.
FALLBACKS: dict[str, Benchmark] = {b.metric: b for b in (
    _fb("cash_pct_revenue", "ratio", 0.04, 0.07, 0.11, 0.17, 0.25, "Cash/revenue for private mid-market firms; wide prior."),
    _fb("interest_rate", "ratio", 0.06, 0.075, 0.09, 0.105, 0.12, "All-in floating rate on bank debt (SOFR + 250-600bp), 2023-2025."),
    _fb("revenue_growth_yoy", "ratio", -0.05, 0.03, 0.10, 0.22, 0.40, "Generic growth prior."),
    _fb("gross_margin", "ratio", 0.20, 0.30, 0.42, 0.58, 0.72, "Generic gross margin prior."),
    _fb("operating_margin", "ratio", -0.05, 0.02, 0.07, 0.12, 0.18, "Generic operating margin prior."),
    _fb("dso", "days", 5.0, 25.0, 42.0, 58.0, 75.0, "Generic DSO prior."),
    _fb("dio", "days", 0.0, 20.0, 55.0, 85.0, 130.0, "Generic DIO prior."),
    _fb("dpo", "days", 15.0, 30.0, 42.0, 55.0, 75.0, "Generic DPO prior."),
    _fb("current_ratio", "x", 0.9, 1.2, 1.5, 2.0, 2.8, "Generic liquidity prior."),
    _fb("debt_to_ebitda", "x", 0.0, 1.0, 2.2, 3.5, 5.0, "Generic leverage prior."),
    _fb("capex_pct_revenue", "ratio", 0.01, 0.02, 0.035, 0.06, 0.09, "Generic capex intensity."),
    _fb("sga_pct_revenue", "ratio", 0.08, 0.14, 0.22, 0.32, 0.45, "Generic SG&A intensity."),
    _fb("rd_pct_revenue", "ratio", 0.0, 0.01, 0.03, 0.08, 0.15, "Generic R&D intensity."),
    _fb("effective_tax_rate", "ratio", 0.0, 0.15, 0.22, 0.25, 0.28, "Statutory 21% plus state; NOLs pull it down."),
    _fb("allowance_pct_ar", "ratio", 0.005, 0.01, 0.02, 0.035, 0.06, "Generic allowance for doubtful accounts / gross AR."),
    _fb("return_rate", "ratio", 0.02, 0.04, 0.08, 0.15, 0.25, "Generic product return rate."),
    _fb("wacc", "ratio", 0.07, 0.08, 0.095, 0.11, 0.13, "US mid-cap WACC range (Damodaran, 2024 industry tables)."),
    _fb("terminal_growth", "ratio", 0.015, 0.02, 0.025, 0.03, 0.035, "Long-run nominal GDP-like growth."),
    _fb("supplier_lead_time_days", "days", 7.0, 14.0, 30.0, 45.0, 75.0, "Supplier quoted lead time (domestic to overseas)."),
    _fb("magic_number", "x", 0.3, 0.5, 0.8, 1.1, 1.5, "SaaS sales efficiency."),
    _fb("annual_prepay_discount", "ratio", 0.08, 0.12, 0.17, 0.2, 0.25, "Annual prepay discount."),
    _fb("channel_stuffing_magnitude", "ratio", 0.03, 0.05, 0.08, 0.12, 0.18, "Pulled-forward revenue / quarterly revenue in SEC AAERs (typical 5-12%)."),
    _fb("elasticity_industrial", "ratio", -1.6, -1.2, -0.8, -0.5, -0.3, "Industrial B2B price elasticity (inelastic)."),
    _fb("cohort_retention_ecommerce_m1", "ratio", 0.10, 0.15, 0.22, 0.30, 0.38, "Share of an e-commerce cohort repurchasing in month 1."),
    _fb("cohort_retention_ecommerce_m3", "ratio", 0.12, 0.18, 0.26, 0.34, 0.42, "Cumulative repeat within 3 months."),
    _fb("cohort_retention_ecommerce_m6", "ratio", 0.14, 0.20, 0.28, 0.36, 0.45, "Cumulative repeat within 6 months."),
    _fb("cohort_retention_ecommerce_m12", "ratio", 0.12, 0.17, 0.225, 0.30, 0.40, "Cumulative repeat within 12 months."),
    _fb("lead_time_ftl_days_cv", "ratio", 0.15, 0.2, 0.3, 0.4, 0.55, "FTL lead time CV."),
    _fb("lead_time_air_days_cv", "ratio", 0.1, 0.15, 0.25, 0.35, 0.5, "Air freight lead time CV."),
    _fb("dispute_fee_usd", "usd", 15.0, 15.0, 15.0, 20.0, 25.0, "Card network dispute fee."),
)}


class Resolution:
    """Outcome of :func:`resolve_with_trace`: the benchmark, the rung that matched, and the trace."""

    __slots__ = ("benchmark", "rung", "trace")

    def __init__(self, benchmark: Benchmark, rung: str, trace: list[str]) -> None:
        self.benchmark = benchmark
        self.rung = rung
        self.trace = trace

    @property
    def is_fallback(self) -> bool:
        """True when no corpus benchmark matched."""
        return self.rung == "fallback"


_warned: set[tuple[str, str]] = set()
_cache: dict[tuple[int, str, str], Resolution] = {}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[._]+", text.lower()) if t]


def _parse(logical_key: str, industry: str) -> tuple[str, str, str | None]:
    """Split a logical key into ``(scope, metric, segment)``.

    ``scope`` is an industry code or one of ``ops``/``forensic``/``any``/``fin``.
    """
    key = logical_key.strip().replace("<ind>", industry).replace("<industry>", industry)
    scope, metric = industry, key
    for prefix in sorted((*KNOWN_INDUSTRIES, *_SCOPES), key=len, reverse=True):
        if key.startswith(prefix + "."):
            scope, metric = prefix, key[len(prefix) + 1:]
            break
    if scope == "operations":
        scope = "ops"
    segment = None
    for seg in _SEGMENTS:
        if metric.endswith("_" + seg):
            segment = seg
            break
    return scope, metric, segment


def _alias_candidates(scope: str, metric: str, industry: str) -> list[str]:
    ind = scope if scope in KNOWN_INDUSTRIES else industry
    out = []
    for (sc, met), target in ALIASES.items():
        if met == metric and sc in (scope, "*"):
            out.append(target.format(ind=ind))
    return out


def _rewrite_candidates(scope: str, metric: str, segment: str | None, industry: str) -> list[str]:
    ind = scope if scope in KNOWN_INDUSTRIES else industry
    ops_ind = _OPS_INDUSTRY.get(ind, ind)
    base = metric[: -(len(segment) + 1)] if segment else metric
    cands = [
        f"fin.{ind}.{metric}", f"fin.{ind}.{metric}_days",
        f"ops.{metric}", f"ops.{ops_ind}.{metric}", f"ops.{metric}.{ops_ind}",
        f"saas.{metric}",
    ]
    if segment:
        cands.append(f"saas.{base}.{segment}")
    cands += [f"forensic.{metric}", f"any.{metric}", f"fin.any.{metric}"]
    if metric.endswith("_days"):
        cands.insert(2, f"fin.{ind}.{metric[:-5]}_days")
    seen: set[str] = set()
    return [c for c in cands if not (c in seen or seen.add(c))]


def _search(pool: BenchmarkPool, metric: str, industry: str) -> tuple[Benchmark | None, list[str]]:
    toks = _tokens(metric)
    if not toks:
        return None, []
    candidates: dict[str, Benchmark] = {}
    for tok in sorted(set(toks), key=lambda t: (-len(t), t)):
        for b in pool.search(industry, tok, include_any=True):
            candidates[b.key] = b
    scored: list[tuple[int, int, str, Benchmark]] = []
    for key, b in candidates.items():
        ktoks = set(_tokens(key))
        if not set(toks) <= ktoks:
            continue
        extra = len(ktoks - set(toks) - {"fin", "ops", "saas", "forensic", industry, _OPS_INDUSTRY.get(industry, industry)})
        scored.append((extra, 0 if b.industry == industry else 1, key, b))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return (scored[0][3] if scored else None), [s[2] for s in scored[:5]]


def resolve_with_trace(pool: BenchmarkPool, logical_key: str, industry: str) -> Resolution:
    """Resolve ``logical_key`` for ``industry`` and return the benchmark plus the ladder trace."""
    ck = (id(pool), logical_key, industry)
    if ck in _cache:
        return _cache[ck]
    trace: list[str] = []
    scope, metric, segment = _parse(logical_key, industry)
    trace.append(f"parse: scope={scope} metric={metric} segment={segment}")

    def done(b: Benchmark, rung: str) -> Resolution:
        trace.append(f"=> {rung}: {b.key}")
        res = Resolution(b, rung, trace)
        _cache[ck] = res
        return res

    exact = logical_key.replace("<ind>", industry)
    trace.append(f"exact: {exact}")
    if pool.has(exact):
        return done(pool.get(exact), "exact")
    for cand in _alias_candidates(scope, metric, industry):
        trace.append(f"alias: {cand}")
        if pool.has(cand):
            return done(pool.get(cand), "alias")
    for cand in _rewrite_candidates(scope, metric, segment, industry):
        trace.append(f"rewrite: {cand}")
        if pool.has(cand):
            return done(pool.get(cand), "rewrite")
    search_industry = scope if scope in KNOWN_INDUSTRIES else industry
    found, top = _search(pool, metric, search_industry)
    trace.append(f"search({search_industry}, {metric}): {top or 'no candidate with all tokens'}")
    if found is not None:
        return done(found, "search")
    fb = FALLBACKS.get(metric) or (FALLBACKS.get(metric[: -(len(segment) + 1)]) if segment else None)
    if fb is None:
        for name, b in FALLBACKS.items():
            if metric.startswith(name) or name.startswith(metric):
                fb = b
                break
    if fb is None:
        raise KeyError(f"benchmark {logical_key!r} ({industry}) unresolvable and no fallback for metric {metric!r}")
    if (logical_key, industry) not in _warned:
        _warned.add((logical_key, industry))
        logger.warning("benchmark %r for industry %r fell back to default %s", logical_key, industry, fb.key)
    return done(fb, "fallback")


def resolve(pool: BenchmarkPool, logical_key: str, industry: str) -> Benchmark:
    """Corpus benchmark for a spec-style logical key (see module docstring for the ladder)."""
    return resolve_with_trace(pool, logical_key, industry).benchmark


def is_fallback(pool: BenchmarkPool, logical_key: str, industry: str) -> bool:
    """True when ``logical_key`` only resolves to a :data:`FALLBACKS` default."""
    return resolve_with_trace(pool, logical_key, industry).is_fallback


def sample(pool: BenchmarkPool, rng: SeededRNG, logical_key: str, industry: str) -> float:
    """One draw (inverse CDF) from the resolved benchmark."""
    return rng.sample_benchmark(resolve(pool, logical_key, industry))


def sample_between(pool: BenchmarkPool, rng: SeededRNG, logical_key: str, industry: str, q_lo: float, q_hi: float) -> float:
    """Draw restricted to the quantile band ``[q_lo, q_hi]`` of the resolved benchmark.

    The spec's "draw p25-p50" is ``sample_between(..., 0.25, 0.50)``: ``u ~ U(q_lo, q_hi)``,
    value ``= Q(u)`` with ``Q`` the benchmark's quantile function.
    """
    lo, hi = sorted((float(q_lo), float(q_hi)))
    u = float(rng.uniform(lo, hi))
    return float(benchmark_quantile(resolve(pool, logical_key, industry), u))


def explain(logical_key: str, industry: str, pool: BenchmarkPool | None = None) -> list[str]:
    """The resolution ladder trace for ``logical_key`` (used by the CLI)."""
    return list(resolve_with_trace(pool or get_default_pool(), logical_key, industry).trace)


def appendix_a_keys() -> list[tuple[str, str]]:
    """``(logical_key, industry)`` pairs listed in archetypes.md Appendix A, expanded per industry."""
    per_ind = ["gross_margin", "operating_margin", "dso", "dio", "dpo", "current_ratio", "debt_to_ebitda",
               "capex_pct_revenue", "sga_pct_revenue", "rd_pct_revenue", "effective_tax_rate", "revenue_growth_yoy",
               "allowance_pct_ar", "cash_pct_revenue", "interest_rate"]
    out: list[tuple[str, str]] = []
    for ind in KNOWN_INDUSTRIES:
        out += [(f"{ind}.{m}", ind) for m in per_ind]
    out += [("ecommerce.return_rate", "ecommerce"), ("retail.return_rate", "retail")]
    saas = ["gross_churn_monthly_smb", "gross_churn_monthly_midmarket", "gross_churn_monthly_enterprise", "net_revenue_retention",
            "cac_payback_months", "ltv_cac", "sm_pct_revenue", "arpa_monthly", "deferred_revenue_pct_of_arr",
            "annual_prepay_discount", "magic_number", "rule_of_40"]
    out += [(f"saas.{m}", "saas") for m in saas]
    out += [("ecommerce.repeat_purchase_rate", "ecommerce"), ("ecommerce.inventory_turns", "ecommerce"),
            ("retail.inventory_turns", "retail"), ("retail.shrink_rate", "retail")]
    out += [(f"manufacturing.{m}", "manufacturing") for m in
            ("oee", "bottleneck_utilization", "scrap_rate", "setup_time_minutes", "overhead_pct_conversion")]
    for mode in ("parcel", "ltl", "ftl", "ocean", "air"):
        out += [(f"3pl.lead_time_{mode}_days", "3pl"), (f"3pl.lead_time_{mode}_days_cv", "3pl")]
    out += [("3pl.otif", "3pl"), ("3pl.fill_rate", "3pl"), ("3pl.pick_rate_lines_per_hour", "3pl")]
    ops = ["supplier_lead_time_days", "safety_stock_service_level", "bullwhip_ratio_empirical", "elasticity_grocery",
           "elasticity_apparel", "elasticity_software", "elasticity_durables", "elasticity_industrial"]
    ops += [f"cohort_retention_{k}_m{m}" for k in ("subscription", "ecommerce") for m in (1, 3, 6, 12)]
    out += [(f"operations.{m}", "any") for m in ops]
    out += [(f"fintech_marketplace.{m}", "fintech_marketplace") for m in ("take_rate", "chargeback_rate", "dispute_win_rate")]
    out += [("any.wacc", "any"), ("any.terminal_growth", "any"), ("forensic.channel_stuffing_magnitude", "any")]
    return out


def coverage(pool: BenchmarkPool, pairs: Iterable[tuple[str, str]] | None = None) -> dict[str, list[str]]:
    """``{rung: ["key@industry -> corpus_key", ...]}`` for Appendix A (or ``pairs``)."""
    out: dict[str, list[str]] = {}
    for key, ind in pairs if pairs is not None else appendix_a_keys():
        res = resolve_with_trace(pool, key, ind)
        out.setdefault(res.rung, []).append(f"{key}@{ind} -> {res.benchmark.key}")
    return out
