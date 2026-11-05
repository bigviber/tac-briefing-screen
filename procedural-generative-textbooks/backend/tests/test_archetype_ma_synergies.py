"""ma_synergies: deal model vs realised data, goodwill from the PPA, impairment exposure, overlap churn via SQL."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from axiom.engine.archetypes.ma_synergies import dcf_value
from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "ma_synergies"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("ma")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}")
            for d in DIFFICULTIES for s in SEEDS}


def _m(world: CaseWorld) -> dict[str, float]:
    return {x.key: x.value for x in world.ground_truth.expected_metrics}


def _a(world: CaseWorld, kind: str):
    return next(a for a in world.ground_truth.anomalies if a.kind == kind)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_deal_economics_and_goodwill(worlds: dict, difficulty: str, seed: int) -> None:
    world, folder = worlds[(difficulty, seed)]
    m = _m(world)
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        ppa = dict(con.execute("SELECT line, amount_cents FROM purchase_price_allocation").fetchall())
        gl_gw = con.execute("SELECT SUM(debit_cents - credit_cents) FROM gl_lines WHERE account_code = '1700'").fetchone()[0]
        a = dict(con.execute("SELECT key, value FROM deal_assumptions").fetchall())
        tf = con.execute("SELECT period, revenue_cents, cogs_cents, standalone_opex_cents FROM target_financials ORDER BY period").fetchdf().set_index("period")
    finally:
        con.close()
    # goodwill = price - (net tangible assets + identified intangibles), exactly, in the PPA and in the GL
    net = ppa["acquired_receivables"] + ppa["acquired_ppe"] + ppa["assumed_contract_liabilities"]
    assert ppa["goodwill"] == ppa["purchase_price"] - net - ppa["customer_relationships_7y"] - ppa["developed_technology_5y"]
    assert gl_gw == ppa["goodwill"] == m["goodwill_cents"]
    assert ppa["cash_paid"] + ppa["acquisition_debt"] == ppa["purchase_price"]
    # the standalone DCF is recomputable from the data room (deal_assumptions + target_financials)
    P = world.periods
    ttm = tf.loc[P[2:6]]
    rev0 = float(ttm["revenue_cents"].sum())
    m0 = float((rev0 - ttm["cogs_cents"].sum() - ttm["standalone_opex_cents"].sum()) / rev0)
    dcf = dcf_value(rev0, float(a["target_growth_year1"]), float(a["terminal_growth"]), m0, float(a["long_run_fcf_margin"]), float(a["tax_rate"]), float(a["wacc"]))
    assert dcf["value"] == pytest.approx(m["standalone_target_dcf_cents"], rel=1e-6)
    assert int(a["standalone_value_cents"]) == pytest.approx(m["standalone_target_dcf_cents"], abs=1)
    # premium ~= PV(claimed synergies) x U(0.9, 1.1): NPV at claimed ~ 0, at achievable clearly negative
    premium = ppa["purchase_price"] - m["standalone_target_dcf_cents"]
    assert 0.85 <= premium / m["pv_claimed_synergies_cents"] <= 1.15
    assert abs(m["deal_npv_claimed_cents"]) <= 0.12 * m["pv_claimed_synergies_cents"]
    assert m["deal_npv_achievable_cents"] < 0 < m["pv_achievable_synergies_cents"] < m["pv_claimed_synergies_cents"]
    assert 0.85 <= m["breakeven_synergy_pct"] <= 1.15
    # impairment exposure exists and is bounded by goodwill; nothing has been booked
    assert 0 < m["goodwill_impairment_cents"] <= m["goodwill_cents"]
    assert world.finance.trial_balance[P[-1]]["1700"] == m["goodwill_cents"]


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_sql_recovers_synergy_shortfall_and_overlap_churn(worlds: dict, difficulty: str) -> None:
    for seed in SEEDS:
        world, folder = worlds[(difficulty, seed)]
        m = _m(world)
        a1 = _a(world, "synergy_shortfall")
        ach = a1.magnitude["cost_synergy_achievable_mult"]
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            P8 = world.periods[-1]
            budget, plan, actual = con.execute(
                "SELECT SUM(standalone_budget_cents), SUM(synergy_plan_cents), SUM(actual_cents) FROM opex_by_dept WHERE entity = 'target' AND period = ?", [P8]).fetchone()
            consolidated = con.execute("SELECT SUM(mrr_cents) * 12 FROM target_subscriptions WHERE cancel_reason = 'consolidated vendor'").fetchone()[0]
            naive_overlap = con.execute("""
                SELECT COUNT(*) FROM target_customers t JOIN customers c ON split_part(t.billing_email, '@', 2) = split_part(c.billing_email, '@', 2)""").fetchone()[0]
            norm_overlap = con.execute("""
                SELECT COUNT(DISTINCT t.customer_id) FROM target_customers t JOIN customers c
                ON lower(trim(split_part(t.billing_email, '@', 2))) = lower(trim(split_part(c.billing_email, '@', 2)))""").fetchone()[0]
        finally:
            con.close()
        # realised / planned cost synergy in P8 == the sampled achievable multiplier (cents rounding only)
        assert (budget - actual) / plan == pytest.approx(ach, abs=0.01)
        assert m["realized_cost_synergy_run_rate_p8_pct_of_plan"] == pytest.approx(ach, abs=0.01)
        assert consolidated == m["overlap_churn_arr_cents"] > 0
        a2 = _a(world, "customer_overlap_churn")
        assert a2.reference_sql[0].expected["checks"]["consolidated_arr_cents"] == consolidated
        assert norm_overlap >= 3
        if difficulty != "foundation":  # whitespace / case dirt on e-mails hides part of the overlap from a naive join
            assert naive_overlap < norm_overlap


def test_cancellations_cluster_after_close(worlds: dict) -> None:
    for (d, s), (world, folder) in worlds.items():
        P = world.periods
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            rows = con.execute("SELECT period_of(canceled_at), COUNT(*) FROM target_subscriptions WHERE cancel_reason = 'consolidated vendor' GROUP BY 1").fetchall()
        finally:
            con.close()
        assert rows and all(p in (P[6], P[7]) for p, _ in rows)  # "macro" red herring: the reason is explicit and post-close only


def test_integration_costs_overrun_and_recur(worlds: dict) -> None:
    for (d, s), (world, _) in worlds.items():
        q = next(r for r in _a(world, "synergy_shortfall").reference_sql if r.name == "integration_costs_in_gl")
        rows = {p: v for p, v in q.expected["rows"]}
        P = world.periods
        assert rows.get(P[6], 0) > 0 and rows.get(P[7], 0) > 0  # "one-time" costs hit both post-close quarters
        assert _a(world, "synergy_shortfall").magnitude["integration_overrun"] >= 1.1


def test_difficulty_ordering_and_revenue_quality(worlds: dict) -> None:
    for s in SEEDS:
        f, pr = worlds[("foundation", s)][0], worlds[("principal", s)][0]
        gap_f = 1 - _a(f, "synergy_shortfall").magnitude["cost_synergy_achievable_mult"]
        gap_p = 1 - _a(pr, "synergy_shortfall").magnitude["cost_synergy_achievable_mult"]
        assert gap_f > gap_p  # foundation: the widest shortfall; principal: subtle
        assert _a(f, "customer_overlap_churn").magnitude["overlap_churn_share"] >= _a(pr, "customer_overlap_churn").magnitude["overlap_churn_share"]
        kinds_p = [a.kind for a in pr.ground_truth.anomalies]
        assert "revenue_quality_mislabel" in kinds_p and len(kinds_p) == 4 and len(f.ground_truth.anomalies) == 3
        m = _m(pr)
        sampled = _a(pr, "revenue_quality_mislabel").magnitude["services_mislabelled_pct"]
        assert m["target_arr_overstated_pct"] == pytest.approx(sampled, abs=0.03)
