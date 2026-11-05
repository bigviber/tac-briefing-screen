"""Targeted checks for ``idempotent_webhooks`` beyond the generic archetype suite."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "idempotent_webhooks"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("webhooks")
    out = {}
    for d in DIFFICULTIES:
        for s in SEEDS:
            folder = root / f"{d}-{s}"
            out[(d, s)] = (generate_world(KEY, s, difficulty=d, out_dir=folder, pool=pool), folder)
    return out


def _df(world: CaseWorld, folder: Path, sql: str) -> pd.DataFrame:
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def _metrics(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _metric(world: CaseWorld, key: str):
    return next(m for m in world.ground_truth.expected_metrics if m.key == key)


def _query(world: CaseWorld, name: str):
    return next(q for a in world.ground_truth.anomalies for q in a.reference_sql if q.name == name)


def _windows(world: CaseWorld) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Incident windows as declared in the ``incident_window_count`` derivation ("YYYY-MM-DD HH:MM-HH:MM; ...")."""
    out = []
    for day, a, b in re.findall(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})-(\d{2}:\d{2})", _metric(world, "incident_window_count").derivation):
        start, end = pd.Timestamp(f"{day} {a}"), pd.Timestamp(f"{day} {b}")
        out.append((start, end if end >= start else end + pd.Timedelta(days=1)))
    return out


_DELIVERIES = """
SELECT event_id, attempt, status, CAST(CAST(sent_at AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS TIMESTAMP) AS sent_utc
FROM webhook_deliveries"""


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_duplicates_recovered_by_sql_match_the_sampled_timeout_rate(worlds: dict, difficulty: str, seed: int) -> None:
    """The reference SQL recovers exactly the duplicate charges; inside the incident windows the timeout share tracks the
    sampled ``p_timeout_window`` (binomial tolerance), and without dedupe every retried timeout becomes one duplicate."""
    world, folder = worlds[(difficulty, seed)]
    m = _metrics(world)
    a1 = next(a for a in world.ground_truth.anomalies if a.id == "A1")
    dups = _df(world, folder, _query(world, "duplicate_ledger_entries").sql)
    assert int((dups["n"] - 1).sum()) == m["duplicate_charge_count"] > 0
    assert int(dups["total"].sum() - (dups["total"] / dups["n"]).sum()) == pytest.approx(m["duplicate_charge_cents"], rel=1e-9)
    dl = _df(world, folder, _DELIVERIES)
    inw = np.zeros(len(dl), dtype=bool)
    for a, b in _windows(world):
        inw |= ((dl["sent_utc"] >= a) & (dl["sent_utc"] <= b)).to_numpy()
    share = float((dl.loc[inw, "status"] == "timeout").mean())
    assert share == pytest.approx(m["timeout_share_in_incidents"], abs=1e-6)
    p = a1.magnitude["p_timeout_window"]
    n = int(inw.sum())
    assert abs(share - p) < 4 * np.sqrt(p * (1 - p) / n) + 0.02
    timeouts = int((dl["status"] == "timeout").sum())
    if difficulty == "principal":
        # dedupe on event_id removes the charge.succeeded retries; only payment_intent.succeeded (re-issued event ids) duplicate
        assert 0 < m["duplicate_charge_count"] < timeouts
        assert m["dedupe_present_code"] == 1.0
    else:
        assert 0.9 * timeouts <= m["duplicate_charge_count"] <= timeouts
        assert m["dedupe_present_code"] == 0.0


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_incident_windows_count_and_concentration(worlds: dict, difficulty: str, seed: int) -> None:
    """The declared windows match ``incident_window_count`` (2 / 3 / 4 by difficulty), each holds timeouts, delivery density
    inside them is far above the background, and most post-migration timeouts fall inside or just after them."""
    world, folder = worlds[(difficulty, seed)]
    m = _metrics(world)
    wins = _windows(world)
    assert len(wins) == m["incident_window_count"] == {"foundation": 2, "practitioner": 3, "principal": 4}[difficulty]
    dl = _df(world, folder, _DELIVERIES)
    to = dl[dl["status"] == "timeout"]
    in_minutes = 0.0
    inw_all = np.zeros(len(dl), dtype=bool)
    for a, b in wins:
        assert ((to["sent_utc"] >= a) & (to["sent_utc"] <= b)).sum() >= 1
        inw_all |= ((dl["sent_utc"] >= a) & (dl["sent_utc"] <= b)).to_numpy()
        in_minutes += (b - a).total_seconds() / 60
    # the timeout rate inside the windows dwarfs the post-migration background rate
    rate_in = float((dl.loc[inw_all, "status"] == "timeout").mean())
    rate_out = float((dl.loc[~inw_all, "status"] == "timeout").mean())
    assert rate_in > 10 * rate_out
    first = dl[dl["attempt"] == 1]
    inw = np.zeros(len(first), dtype=bool)
    for a, b in wins:
        inw |= ((first["sent_utc"] >= a) & (first["sent_utc"] <= b)).to_numpy()
    span_min = (first["sent_utc"].max() - first["sent_utc"].min()).total_seconds() / 60
    density_in = inw.sum() / in_minutes
    density_out = (~inw).sum() / (span_min - in_minutes)
    assert density_in > 20 * density_out


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_refund_liability_ties_to_open_duplicates(worlds: dict, difficulty: str, seed: int) -> None:
    """GL refund liability (2450) at each quarter end == duplicate captures (payments_ledger, deduplicated) less duplicate
    refunds less lost duplicate_processing disputes, all recomputed from the data room."""
    world, folder = worlds[(difficulty, seed)]
    ch = _df(world, folder, """
        WITH pl AS (SELECT * FROM payments_ledger QUALIFY ROW_NUMBER() OVER (PARTITION BY ledger_entry_id ORDER BY ingest_id) = 1),
        c AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY payment_intent_id ORDER BY created_at, ledger_entry_id) AS rn
              FROM pl WHERE entry_type = 'charge')
        SELECT created_at AS t, amount_cents AS a FROM c WHERE rn > 1""")
    rf = _df(world, folder, "SELECT created_at AS t, amount_cents AS a FROM refunds WHERE reason = 'duplicate'")
    dp = _df(world, folder, "SELECT resolved_at AS t, amount_cents AS a FROM disputes WHERE reason = 'duplicate_processing' AND status = 'lost'")
    for frame in (ch, rf, dp):
        frame["t"] = pd.to_datetime(frame["t"])
    bs = {b.period: {ln.caption: ln.cents for ln in b.lines} for b in world.finance.statements.balance}
    seen_nonzero = False
    for period in world.periods:
        y, q = int(period[:4]), int(period[-1])
        end = pd.Timestamp(year=y, month=3 * q, day=1) + pd.offsets.MonthEnd(0) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        open_dups = int(ch.loc[ch["t"] <= end, "a"].sum() - rf.loc[rf["t"] <= end, "a"].sum() - dp.loc[dp["t"] <= end, "a"].sum())
        assert bs[period].get("Refund liability", 0) == open_dups, period
        seen_nonzero |= open_dups != 0
    assert seen_nonzero or difficulty == "foundation"


@pytest.mark.parametrize("seed", SEEDS)
def test_gross_margin_is_the_business_not_the_difficulty(worlds: dict, seed: int) -> None:
    """Review P1 #13: GM = 1 - (processor fees + payment operations) / take revenue is the same business at every difficulty
    (the pre-incident quarters agree within half a point), and the operating margin moves in a plausible band."""
    gms = {}
    for d in DIFFICULTIES:
        world, _ = worlds[(d, seed)]
        r = world.finance.statements.ratios
        gms[d] = float(np.mean([r[p]["gross_margin"] for p in world.periods[:4]]))
        ue = world.operations.unit_economics
        assert 0.12 <= ue["take_rate"] <= 0.15
        expected = 1 - ue["processing_fee_rate"] / ue["take_rate"] - ue["payment_ops_cost_share"]
        assert gms[d] == pytest.approx(expected, abs=0.02)
        inc = world.finance.statements.income
        opm = np.array([i.operating_income_cents / i.revenue_cents for i in inc])
        assert np.abs(np.diff(opm)).max() < 0.06 and opm.max() - opm.min() < 0.08
        assert (opm > -0.02).all()
    assert max(gms.values()) - min(gms.values()) < 0.005
    assert all(0.68 <= g <= 0.80 for g in gms.values())


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_balance_sheet_carries_payables_and_settlement_receivable(worlds: dict, difficulty: str) -> None:
    """Review P1 #13 / C1: vendor AP (incl. the processor's dispute-fee invoice), accrued payment-operations cost and the
    T+2 settlement receivable appear on the balance sheet."""
    world, _ = worlds[(difficulty, 7)]
    caps = {}
    for b in world.finance.statements.balance:
        for ln in b.lines:
            caps.setdefault(ln.caption, []).append(ln.cents)
    for cap in ("Accounts payable", "Accrued liabilities", "Accounts receivable, net", "Customer deposits"):
        assert any(v != 0 for v in caps.get(cap, [])), cap
