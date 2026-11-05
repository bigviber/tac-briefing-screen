"""Targeted checks for ``cohort_matrix`` beyond the generic archetype suite."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "cohort_matrix"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)

# Mirrors the Python sandbox namespace (pd, np, duckdb, scipy, con, tables) in an isolated interpreter.
_RUNNER = textwrap.dedent('''
    import json, sys
    import numpy as np, pandas as pd, duckdb
    try:
        import scipy
    except ImportError:
        scipy = None
    db, code_path, out_path = sys.argv[1:4]
    con = duckdb.connect(db, read_only=True)
    names = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
    class Tables(dict):
        def __getitem__(self, k):
            if not dict.__contains__(self, k):
                dict.__setitem__(self, k, con.execute(f'SELECT * FROM "{k}"').df())
            return dict.__getitem__(self, k)
    ns = {"pd": pd, "np": np, "duckdb": duckdb, "scipy": scipy, "con": con, "tables": Tables(), "__name__": "__main__"}
    exec(compile(open(code_path).read(), "<reference>", "exec"), ns)
    json.dump(ns.get("result"), open(out_path, "w"), default=float)
''')


def run_isolated(code: str, db_path: Path, tmp: Path) -> dict:
    (tmp / "runner.py").write_text(_RUNNER)
    (tmp / "code.py").write_text(code)
    proc = subprocess.run([sys.executable, "-I", str(tmp / "runner.py"), str(db_path), str(tmp / "code.py"), str(tmp / "out.json")],
                          capture_output=True, text=True, check=False, timeout=120, cwd=tmp)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads((tmp / "out.json").read_text())


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("cohort")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}")
            for d in DIFFICULTIES for s in SEEDS}


def _df(world: CaseWorld, folder: Path, sql: str) -> pd.DataFrame:
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def _metrics(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _query(world: CaseWorld, name: str):
    return next(q for a in world.ground_truth.anomalies for q in a.reference_sql if q.name == name)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_matrix_is_lower_triangular_and_censoring_aware(worlds: dict, difficulty: str, seed: int) -> None:
    world, folder = worlds[(difficulty, seed)]
    tri = _df(world, folder, _query(world, "retention_triangle_long").sql)
    tri["cohort"] = pd.to_datetime(tri["cohort"])
    last = tri["cohort"].max()
    first = tri["cohort"].min()
    n_months = (last.year - first.year) * 12 + last.month - first.month + 1
    # installed base: the product launched 18-30 months before the books open (27 months of books incl. the warm-up)
    assert 27 + 18 <= n_months <= 27 + 30 and tri["cohort"].nunique() == n_months
    # no cell beyond the as-of month; k = 0 is always 100%; retention within [0, 1]
    month_no = tri["cohort"].dt.year * 12 + tri["cohort"].dt.month
    assert (month_no + tri["k"] <= last.year * 12 + last.month).all()
    assert (tri.loc[tri["k"] == 0, "retention"] == 1.0).all() and tri["retention"].between(0, 1).all()
    assert tri.groupby("cohort")["k"].max().max() == n_months - 1
    pivot = _df(world, folder, _query(world, "cohort_pivot_m1_m3_m6_m12").sql)
    assert pivot["m3"].isna().sum() == 3 and pivot["m6"].isna().sum() == 6 and pivot["m12"].isna().sum() == 12
    assert pivot["m1"].isna().sum() == 1


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_promo_cohort_is_the_anomaly(worlds: dict, difficulty: str, seed: int) -> None:
    world, folder = worlds[(difficulty, seed)]
    m = _metrics(world)
    promo = _df(world, folder, "SELECT strftime(starts_at, '%Y%m') AS m FROM promotions WHERE promo_code = 'LAUNCH50'")["m"].iloc[0]
    assert m["anomalous_cohort"] == float(promo)
    gap = m["baseline_m3_retention"] - m["anomalous_cohort_m3_retention"]
    assert gap > {"foundation": 0.12, "practitioner": 0.1, "principal": 0.06}[difficulty]
    worst = _query(world, "cohort_pivot_m1_m3_m6_m12").expected["checks"]["worst_m3_cohort"]
    assert worst.replace("-", "")[:6] == promo
    # the naive slide says young cohorts look better (censoring), which is the red herring
    assert _query(world, "naive_slide_active_today_share").expected["checks"]["last_vs_first_share"] > 0.2


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_naive_counts_and_reactivations(worlds: dict, difficulty: str) -> None:
    world, folder = worlds[(difficulty, 7)]
    m = _metrics(world)
    raw, customers = _df(world, folder, "SELECT (SELECT COUNT(*) FROM subscriptions), (SELECT COUNT(DISTINCT customer_id) FROM subscriptions)").iloc[0]
    assert m["naive_vs_correct_customer_count_delta"] == raw - customers > 0
    assert 0.003 < m["reactivation_share"] < 0.05
    late = _query(world, "late_arriving_cancellations").expected
    assert late["checks"]["not_recorded"] > 0
    if difficulty != "foundation":
        assert 0.05 < m["sbg_alpha"] < 5 and m["sbg_beta"] > m["sbg_alpha"]


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_python_reference_reproduces_sql_metrics(worlds: dict, difficulty: str, tmp_path: Path) -> None:
    """The pandas reference (dedupe, UTC, triangle, censoring-aware averages) agrees with the SQL ground truth."""
    world, folder = worlds[(difficulty, 2024)]
    m = _metrics(world)
    res = run_isolated(world.ground_truth.reference_solution.python[0], resolve_db_path(world, folder), tmp_path)
    assert res["anomalous_cohort"].replace("-", "") == str(int(m["anomalous_cohort"]))
    assert res["m3_anomalous"] == pytest.approx(m["anomalous_cohort_m3_retention"], abs=1e-6)
    assert res["m3_others"] == pytest.approx(m["baseline_m3_retention"], abs=1e-6)
    assert res["m6_weighted"] == pytest.approx(m["weighted_avg_m6_retention"], abs=1e-6)


def _mrr_revenue_by_quarter(world: CaseWorld, folder: Path) -> dict[str, float]:
    """Revenue rebuilt from the subscription table alone: every subscription bills its MRR (list price; LAUNCH50 halves the
    first three charges) on each monthly anniversary, recognised pro rata over the service month - i.e. Sigma active MRR
    per month, x 3 per quarter, with plan changes and proration."""
    subs = _df(world, folder, """
        SELECT subscription_id, CAST(started_at AS VARCHAR) AS started_at, ended_at, mrr_cents, promo_code
        FROM subscriptions QUALIFY ROW_NUMBER() OVER (PARTITION BY subscription_id ORDER BY ingest_id) = 1""")
    start = pd.to_datetime(subs["started_at"], utc=True, format="mixed").dt.tz_localize(None)
    asof = pd.Timestamp(world.periods[-1][:4] + "-" + str(int(world.periods[-1][-1]) * 3).zfill(2) + "-01") + pd.offsets.MonthBegin(1)
    end = pd.to_datetime(subs["ended_at"]).fillna(asof + pd.DateOffset(months=1))
    n = ((end.dt.year - start.dt.year) * 12 + end.dt.month - start.dt.month + 1).clip(lower=1).to_numpy()
    idx = np.repeat(np.arange(len(subs)), n)
    k = np.concatenate([np.arange(c) for c in n])
    s0 = start.iloc[idx].reset_index(drop=True)
    a = pd.Series([t + pd.DateOffset(months=int(j)) for t, j in zip(s0, k, strict=True)])
    b = pd.Series([t + pd.DateOffset(months=int(j) + 1) for t, j in zip(s0, k, strict=True)])
    keep = (a < asof) & (a < end.iloc[idx].reset_index(drop=True))
    price = subs["mrr_cents"].to_numpy()[idx].astype(float)
    promo = subs["promo_code"].notna().to_numpy()[idx] & (k < 3)
    net = np.where(promo, price - np.round(price * 0.5), price)
    out = {}
    for period in world.periods:
        y, q = int(period[:4]), int(period[-1])
        qs, qe = pd.Timestamp(year=y, month=3 * q - 2, day=1), pd.Timestamp(year=y, month=3 * q - 2, day=1) + pd.DateOffset(months=3)
        ov = (np.minimum(b, qe) - np.maximum(a, qs)).dt.total_seconds().clip(lower=0) / (b - a).dt.total_seconds()
        out[period] = float((net * ov * keep).sum())
    return out


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_revenue_is_derived_from_subscription_mrr(worlds: dict, difficulty: str) -> None:
    """Review P0 #1: quarterly revenue ties to the subscription table (Sigma active MRR, pro rata) within 0.5%, and growth is
    organic (new logos x ARPA net of churn), not a 6x independent growth path."""
    world, folder = worlds[(difficulty, 7)]
    mrr = _mrr_revenue_by_quarter(world, folder)
    inc = {i.period: i for i in world.finance.statements.income}
    for period, v in mrr.items():
        assert inc[period].revenue_cents == pytest.approx(v, rel=0.005), period
    growth = inc[world.periods[-1]].revenue_cents / inc[world.periods[0]].revenue_cents
    assert 1.2 < growth < 3.0
    # revenue per head follows the base benchmark, not a 12-person floor
    assert 120_000 < world.company.revenue_ttm_cents / 100 / world.company.headcount < 400_000


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_unit_economics_and_cohorts_from_the_books(worlds: dict, difficulty: str) -> None:
    """Review P2 #23: CAC from booked sales & marketing, payback and LTV populated; cohorts carry the real curves."""
    world, folder = worlds[(difficulty, 1)]
    ue = world.operations.unit_economics
    assert ue["cac"] > 0 and 3 < ue["payback_months"] < 30 and ue["ltv"] > ue["cac"]
    sm = sum(sum(ln.debit_cents - ln.credit_cents for ln in e.lines if ln.account == "6000")
             for e in world.finance.journal if world.periods[-4] <= f"{e.date.year}Q{(e.date.month - 1) // 3 + 1}")
    assert ue["sales_marketing_ttm_cents"] == pytest.approx(sm)
    assert ue["cac"] == pytest.approx(sm / ue["new_logos_ttm"])
    spend = _df(world, folder, "SELECT SUM(spend_cents) AS s FROM marketing_spend")["s"].iloc[0]
    assert 0.4 * sm < spend  # program spend is the non-payroll part of 6000 over the whole window
    cohorts = world.operations.cohorts
    assert len(cohorts) >= 27 and all(c.retention[0] == 1.0 for c in cohorts)
    tri = _df(world, folder, _query(world, "retention_triangle_long").sql)
    tri["cohort"] = pd.to_datetime(tri["cohort"]).dt.strftime("%Y-%m")
    c0 = cohorts[0]
    sql_curve = tri[tri["cohort"] == c0.acquisition_period].sort_values("k")["retention"].tolist()
    assert c0.retention[: len(sql_curve)] == pytest.approx(sql_curve, abs=1e-9)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_reveal_names_the_promo_month_only_at_foundation(worlds: dict, difficulty: str) -> None:
    """Review P2 #24: the practitioner / principal reveal says a promotion ran 'last <season>', never the month."""
    world, _ = worlds[(difficulty, 7)]
    pm = int(next(m.value for m in world.ground_truth.expected_metrics if m.key == "anomalous_cohort"))
    month = f"{pm // 100}-{pm % 100:02d}"
    reveal = " ".join(world.dossier_seed.reveal)
    month_name = pd.Timestamp(f"{month}-01").strftime("%B")
    if difficulty == "foundation":
        assert month in reveal
    else:
        assert month not in reveal and month_name not in reveal and "promotion" in reveal and "last " in reveal
