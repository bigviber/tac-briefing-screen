"""Spec benchmark keys -> corpus keys (archetypes.md Appendix A)."""

from __future__ import annotations

import pytest

from axiom.engine import benchmark_keys as bk
from axiom.engine.market_research import get_default_pool
from axiom.engine.rng import SeededRNG

# Appendix A keys with no corpus counterpart (documented): they resolve to FALLBACKS.
LEGIT_FALLBACKS = {
    "saas.interest_rate@saas", "ecommerce.interest_rate@ecommerce", "manufacturing.interest_rate@manufacturing",
    "3pl.interest_rate@3pl", "retail.interest_rate@retail", "fintech_marketplace.interest_rate@fintech_marketplace",
    "ecommerce.cash_pct_revenue@ecommerce", "manufacturing.cash_pct_revenue@manufacturing", "3pl.cash_pct_revenue@3pl",
    "retail.cash_pct_revenue@retail", "fintech_marketplace.cash_pct_revenue@fintech_marketplace",
    "ecommerce.rd_pct_revenue@ecommerce", "3pl.rd_pct_revenue@3pl", "retail.rd_pct_revenue@retail",
    "fintech_marketplace.allowance_pct_ar@fintech_marketplace",
    "3pl.lead_time_air_days_cv@3pl",
    "operations.elasticity_industrial@any",
    "operations.cohort_retention_ecommerce_m1@any", "operations.cohort_retention_ecommerce_m3@any",
    "operations.cohort_retention_ecommerce_m6@any",
    "any.wacc@any", "any.terminal_growth@any", "forensic.channel_stuffing_magnitude@any",
}


def test_every_appendix_a_key_resolves_to_corpus_or_documented_fallback() -> None:
    pool = get_default_pool()
    fell_back = set()
    for key, ind in bk.appendix_a_keys():
        res = bk.resolve_with_trace(pool, key, ind)
        if res.is_fallback:
            fell_back.add(f"{key}@{ind}")
            assert res.benchmark.source == "fallback default"
        else:
            assert pool.has(res.benchmark.key), key
    assert fell_back == LEGIT_FALLBACKS
    assert len(fell_back) <= 23  # was 26 before the supplier lead time / FTL CV / ecommerce m12 aliases; must not grow


@pytest.mark.parametrize(("key", "industry", "expected"), [
    ("saas.dso", "saas", "fin.saas.dso_days"),
    ("<ind>.gross_margin", "manufacturing", "fin.manufacturing.gross_margin"),
    ("manufacturing.oee", "manufacturing", "ops.oee.discrete_manufacturing"),
    ("3pl.lead_time_parcel_days", "3pl", "ops.lead_time_days.parcel_ground"),
    ("saas.cac_payback_months_smb", "saas", "saas.cac_payback_months.smb"),
    ("operations.safety_stock_service_level", "any", "ops.safety_stock_service_level"),
    ("dso", "retail", "fin.retail.dso_days"),
    ("fintech_marketplace.chargeback_rate", "fintech_marketplace", "ops.fintech.chargeback_rate"),
    ("operations.supplier_lead_time_days", "retail", "ops.manufacturing_lead_time_days"),
    ("operations.supplier_lead_time_days", "any", "ops.manufacturing_lead_time_days"),
    ("3pl.lead_time_ftl_days_cv", "3pl", "ops.lead_time_cv.ltl"),
    ("operations.cohort_retention_ecommerce_m12", "any", "ops.retention.ecommerce_dtc.repeat_rate_12m"),
])
def test_resolution_ladder(key: str, industry: str, expected: str) -> None:
    assert bk.resolve(get_default_pool(), key, industry).key == expected


def test_search_rung_and_explain() -> None:
    pool = get_default_pool()
    res = bk.resolve_with_trace(pool, "manufacturing.first_pass_yield", "manufacturing")
    assert res.rung in ("rewrite", "search") and res.benchmark.key == "ops.first_pass_yield"
    trace = bk.explain("3pl.lead_time_ltl_days", "3pl", pool)
    assert trace[0].startswith("parse:") and trace[-1] == "=> alias: ops.lead_time_days.ltl"
    with pytest.raises(KeyError):
        bk.resolve(pool, "saas.totally_unknown_metric_xyz", "saas")


def test_sampling_is_deterministic_and_banded() -> None:
    pool = get_default_pool()
    a = bk.sample(pool, SeededRNG(3), "saas.gross_margin", "saas")
    assert a == bk.sample(pool, SeededRNG(3), "saas.gross_margin", "saas")
    b = pool.get("fin.retail.dio_days")
    for s in range(20):
        v = bk.sample_between(pool, SeededRNG(s), "retail.dio", "retail", 0.25, 0.5)
        assert b.p25 - 1e-9 <= v <= b.p50 + 1e-9
