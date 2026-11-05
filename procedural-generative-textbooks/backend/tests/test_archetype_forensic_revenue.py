"""forensic_revenue: mechanism magnitudes recovered by SQL, designed revenue-vs-shipment break, Beneish separation."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from axiom.engine.finance import BENEISH_COEFFICIENTS
from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "forensic_revenue"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)
DEDUPE = "SELECT * FROM invoices QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1"


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("forensic")
    out = {}
    for d in DIFFICULTIES:
        for s in SEEDS:
            folder = root / f"{d}-{s}"
            out[(d, s)] = (generate_world(KEY, s, difficulty=d, out_dir=folder, pool=pool), folder)
    return out


def _m(world: CaseWorld) -> dict[str, float]:
    return {x.key: x.value for x in world.ground_truth.expected_metrics}


def _con(world: CaseWorld, folder: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)


def _mechanism(world: CaseWorld) -> list[str]:
    kinds = [a.kind for a in world.ground_truth.anomalies]
    return [k for k in kinds if k in ("channel_stuffing", "bill_and_hold", "premature_revenue_recognition")]


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_beneish_flags_anomalous_period_not_clean_period(worlds: dict, difficulty: str, seed: int) -> None:
    world, _ = worlds[(difficulty, seed)]
    m = _m(world)
    assert m["beneish_m_score_p8"] > -1.78, m["beneish_m_score_p8"]
    assert m["beneish_m_score_clean_p6"] < -1.78, m["beneish_m_score_clean_p6"]
    # independent recomputation of the P8 TTM M-score from the reported statements (P5..P8 vs P1..P4)
    st = world.finance.statements
    inc = {i.period: i for i in st.income}
    cf = {c.period: c for c in st.cash_flow}
    bs = {b.period: b for b in st.balance}
    P = world.periods

    def line(period: str, caption: str) -> int:
        return next(ln.cents for ln in bs[period].lines if ln.caption == caption)

    def ttm(qs: list[str]) -> dict[str, float]:
        end = qs[-1]
        sga = sum(next(ln.cents for ln in inc[x].lines if ln.caption == c) for x in qs for c in ("Sales & marketing", "General & administrative"))
        return {"sales": sum(inc[x].revenue_cents for x in qs), "gp": sum(inc[x].gross_profit_cents for x in qs), "sga": sga,
                "ni": sum(inc[x].net_income_cents for x in qs), "cfo": sum(cf[x].operating_cents for x in qs),
                "ar": line(end, "Accounts receivable, net"), "ca": line(end, "Total current assets"),
                "ppe": line(end, "Property, plant and equipment, net"), "ta": bs[end].total_assets_cents, "liab": bs[end].total_liabilities_cents}

    cur, prev = ttm(P[4:8]), ttm(P[0:4])
    dsri = (cur["ar"] / cur["sales"]) / (prev["ar"] / prev["sales"])
    sgi = cur["sales"] / prev["sales"]
    tata = (cur["ni"] - cur["cfo"]) / cur["ta"]
    assert dsri > 1.2 and sgi > 1.1 and tata > 0.03, (dsri, sgi, tata)  # spec correlations (manipulator side)
    gmi = (prev["gp"] / prev["sales"]) / (cur["gp"] / cur["sales"])
    sgai = (cur["sga"] / cur["sales"]) / (prev["sga"] / prev["sales"])
    lvgi = (cur["liab"] / cur["ta"]) / (prev["liab"] / prev["ta"])
    c = BENEISH_COEFFICIENTS
    # P8 depreciation of P1 needs P0 (not reported) - DEPI and AQI are ~1 in this archetype, use the neutral value
    m = (c["intercept"] + c["dsri"] * dsri + c["gmi"] * gmi + c["aqi"] + c["sgi"] * sgi + c["depi"] + c["sgai"] * sgai
         + c["tata"] * tata + c["lvgi"] * lvgi)
    assert abs(m - _m(world)["beneish_m_score_p8"]) < 0.1, m


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_sql_recovers_pulled_forward_magnitude(worlds: dict, difficulty: str) -> None:
    for seed in SEEDS:
        world, folder = worlds[(difficulty, seed)]
        m = _m(world)
        P = world.periods
        a1 = world.ground_truth.anomalies[0]
        pct = a1.magnitude["pulled_forward_pct_of_quarter_revenue"]
        con = _con(world, folder)
        try:
            if a1.kind == "channel_stuffing":
                # stuffed window orders carry extended dating; P7 has no order trough, so its baseline is intact
                rows = con.execute(f"""
                    WITH inv AS ({DEDUPE})
                    SELECT period_of(i.invoice_date) AS period,
                           SUM(CASE WHEN o.notes LIKE 'Q-end promotion%' THEN i.amount_cents ELSE 0 END) AS stuffed,
                           SUM(CASE WHEN i.terms_days <= 45 THEN i.amount_cents ELSE 0 END) AS base
                    FROM inv i JOIN orders o USING (order_id)
                    WHERE i.customer_id NOT IN (SELECT c.customer_id FROM customers c JOIN suppliers s
                                                ON UPPER(TRIM(c.tax_id)) = UPPER(TRIM(s.tax_id)))
                    GROUP BY 1""").fetchdf().set_index("period")
                assert rows.loc[P[6], "stuffed"] / rows.loc[P[6], "base"] == pytest.approx(pct, rel=0.05)
                assert rows.loc[P[6], "stuffed"] + rows.loc[P[7], "stuffed"] >= 0.99 * m["pulled_forward_revenue_cents"] * (1 if len(_mechanism(world)) == 1 else 0)
                assert all(rows.loc[x, "stuffed"] == 0 for x in P[:6])
            else:
                rows = con.execute(f"""
                    WITH inv AS ({DEDUPE})
                    SELECT period_of(i.invoice_date) AS period, SUM(i.amount_cents) AS unshipped
                    FROM inv i LEFT JOIN shipments s USING (order_id)
                    WHERE s.shipped_at IS NULL OR CAST(s.shipped_at AS DATE) > last_day_of_quarter(i.invoice_date)
                    GROUP BY 1""").fetchdf().set_index("period")["unshipped"]
                assert float(rows[P[7]]) == pytest.approx(m["invoiced_not_shipped_p8_cents"], abs=1)
                total8 = con.execute(f"WITH inv AS ({DEDUPE}) SELECT SUM(amount_cents) FROM inv WHERE period_of(invoice_date) = '{P[7]}'").fetchone()[0]
                assert float(rows[P[7]]) / (total8 - float(rows[P[7]])) == pytest.approx(pct, rel=0.08)
                assert set(rows.index) <= {P[6], P[7]}
            share = con.execute(f"""
                WITH inv AS ({DEDUPE})
                SELECT SUM(CASE WHEN invoice_date > last_day_of_quarter(invoice_date) - INTERVAL 7 DAY THEN amount_cents ELSE 0 END) * 1.0
                       / SUM(amount_cents) FROM inv WHERE period_of(invoice_date) = '{P[7]}'""").fetchone()[0]
            assert share == pytest.approx(m["last5day_revenue_share_p8"], abs=1e-6)
            assert m["last5day_revenue_share_p8"] > m["last5day_revenue_share_baseline"] + 0.03
        finally:
            con.close()


@pytest.mark.parametrize("seed", SEEDS)
def test_designed_revenue_vs_shipment_break_equals_declared_amount(worlds: dict, seed: int) -> None:
    for d in DIFFICULTIES:
        world, _ = worlds[(d, seed)]
        m = _m(world)
        P8 = world.periods[-1]
        checks = {c.name: c for c in world.finance.invariants if c.name.startswith("subledger:revenue_vs_shipments")}
        assert len(checks) == 8 and all(c.passed for c in checks.values())
        c8 = checks[f"subledger:revenue_vs_shipments[{P8}]"]
        if "invoiced_not_shipped_p8_cents" in m:
            assert "designed break" in c8.detail and f"(expected {-int(m['invoiced_not_shipped_p8_cents'])})" in c8.detail
        else:  # pure channel stuffing: goods shipped, the tie holds
            assert "designed break" not in c8.detail
        for p in world.periods[:6]:
            assert "designed break" not in checks[f"subledger:revenue_vs_shipments[{p}]"].detail
        # the AR sub-ledger itself always ties
        assert all(c.passed for c in world.finance.invariants if c.name.startswith("subledger:ar_open_invoices"))


def test_red_herrings_are_plausible(worlds: dict) -> None:
    for (d, s), (world, folder) in worlds.items():
        P = world.periods
        con = _con(world, folder)
        try:
            shares = con.execute(f"""
                WITH inv AS ({DEDUPE})
                SELECT period_of(invoice_date) AS period,
                       SUM(CASE WHEN invoice_date > last_day_of_quarter(invoice_date) - INTERVAL 7 DAY THEN amount_cents ELSE 0 END) * 1.0 / SUM(amount_cents) AS sh
                FROM inv GROUP BY 1""").fetchdf().set_index("period")["sh"]
            # "seasonality": a modest quarter-end bump exists every quarter (10-14% baseline), so the same quarter last year looks normal
            assert all(0.07 <= shares[p] <= 0.18 for p in P[:6]), shares.to_dict()
            assert shares[P[3]] < shares[P[7]]
            # "promotion": discounted wholesale orders exist throughout history, not only at quarter end
            n_disc = con.execute(f"SELECT COUNT(*) FROM orders WHERE discount_pct > 0 AND CAST(order_ts AS VARCHAR) < '{P[4][:4]}'").fetchone()[0]
            assert n_disc > 0
        finally:
            con.close()
        if world.ground_truth.anomalies[0].kind == "channel_stuffing":
            m = _m(world)
            a = next(x for x in world.ground_truth.anomalies if x.kind == "channel_stuffing")
            chk = next(q for q in a.reference_sql if q.name == "return_rate_window_vs_rest").expected["checks"]
            assert chk["window_rate_p7"] == pytest.approx(m["return_rate_window_orders_p7"], abs=1e-6)
            assert chk["window_rate_p7"] > chk["rest_rate_p7"], chk
            con = _con(world, folder)
            try:
                stuffed, rest = con.execute(f"""
                    WITH r AS (SELECT order_id, SUM(refund_amount_cents) AS ret FROM returns GROUP BY 1)
                    SELECT SUM(CASE WHEN o.notes LIKE 'Q-end promotion%' THEN COALESCE(r.ret, 0) END) * 1.0
                             / SUM(CASE WHEN o.notes LIKE 'Q-end promotion%' THEN o.total_cents END),
                           SUM(CASE WHEN o.notes IS NULL AND o.channel = 'wholesale' THEN COALESCE(r.ret, 0) END) * 1.0
                             / SUM(CASE WHEN o.notes IS NULL AND o.channel = 'wholesale' THEN o.total_cents END)
                    FROM orders o LEFT JOIN r USING (order_id) WHERE period_of(o.order_ts) = '{P[6]}'""").fetchone()
            finally:
                con.close()
            assert stuffed > 1.75 * rest, (stuffed, rest)  # stuffed orders come back at 2.5-4x the base rate (sampling noise)


def test_difficulty_ordering_and_principal_variants(worlds: dict) -> None:
    for s in SEEDS:
        f = worlds[("foundation", s)][0]
        pr = worlds[("principal", s)][0]
        pct_f = f.ground_truth.anomalies[0].magnitude["pulled_forward_pct_of_quarter_revenue"]
        pct_p = pr.ground_truth.anomalies[0].magnitude["pulled_forward_pct_of_quarter_revenue"]
        assert pct_f >= 0.15 and pct_p <= 0.09 and pct_f > pct_p  # D[p]: foundation upper quartile, principal lower quartile
        kinds_p = [a.kind for a in pr.ground_truth.anomalies]
        assert kinds_p[0] == "channel_stuffing" and kinds_p[1] in ("bill_and_hold", "premature_revenue_recognition")
        assert "round_tripping" in kinds_p and len(pr.ground_truth.anomalies) > len(f.ground_truth.anomalies)
        assert all(len(a.reference_sql) >= 2 for w in (f, pr) for a in w.ground_truth.anomalies)


def test_round_trip_pair_is_found_and_near_zero_margin(worlds: dict) -> None:
    for s in SEEDS:
        world, folder = worlds[("principal", s)]
        a = next(x for x in world.ground_truth.anomalies if x.kind == "round_tripping")
        q = next(r for r in a.reference_sql if r.name == "round_trip_sales_vs_purchases_by_month")
        assert q.expected["checks"]["months"] >= 10 and abs(q.expected["checks"]["margin_pct"]) < 0.03
        con = _con(world, folder)
        try:
            pairs = con.execute("SELECT COUNT(*) FROM customers c JOIN suppliers s ON UPPER(TRIM(c.tax_id)) = UPPER(TRIM(s.tax_id))").fetchone()[0]
            naive = con.execute("SELECT COUNT(*) FROM customers c JOIN suppliers s ON c.tax_id = s.tax_id").fetchone()[0]
        finally:
            con.close()
        assert pairs == 1 and naive == 0  # dirt (case / whitespace) hides the pair from a naive join


def test_allowance_held_flat_while_receivables_grow(worlds: dict) -> None:
    for (d, s), (world, _) in worlds.items():
        tb = world.finance.trial_balance
        P6, P8 = world.periods[5], world.periods[7]
        assert tb[P8]["1100"] > tb[P6]["1100"]
        allowance = next(a for a in world.ground_truth.anomalies if a.kind == "understated_allowance")
        assert allowance.magnitude["allowance_pct_ar_p8"] < allowance.magnitude["allowance_pct_ar_before"]
        assert tb[P8]["1110"] == tb[world.periods[6]]["1110"]  # not a cent of provision in the window


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_baseline_dso_is_wholesale_and_anomaly_lifts_it(worlds: dict, difficulty: str) -> None:
    """Review P1 #16: net-45 distributors with card-paying direct accounts as a minority give a baseline DSO of ~35 days;
    the pulled-forward quarters (plus slow-paying, over-stocked distributors) lift it to ~55."""
    for seed in SEEDS:
        world, _ = worlds[(difficulty, seed)]
        P = world.periods
        kp = world.operations.kpis_by_period
        base = [kp[p]["dso"] for p in P[:6]]
        assert 28 <= sum(base[:5]) / 5 <= 45, base
        assert all(24 <= x <= 48 for x in base), base
        assert 45 <= kp[P[7]]["dso"] <= 70 and kp[P[7]]["dso"] - sum(base[:5]) / 5 >= 10, (base, kp[P[7]]["dso"])
        m = _m(world)
        assert 45 <= m["dso_p8_days"] <= 70 and m["dso_p8_days"] > m["dso_p6_days"] + 10  # answer key (gross invoiced basis)
        assert "net-45" in world.company.description
