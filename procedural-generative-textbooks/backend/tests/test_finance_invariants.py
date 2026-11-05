"""Ledger engine, statements and invariants (archetype_common.md sections 2-6, ARCHITECTURE section 7)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from axiom.engine.finance import (
    CFS_MAP,
    FiscalCalendar,
    Ledger,
    LedgerError,
    OpeningBalanceInputs,
    SubledgerTie,
    WorldInvariantError,
    assert_invariants,
    build_statements,
    canonical_chart_of_accounts,
    cents,
    check_invariants,
    dollars_to_cents,
    opening_balances,
    split_cents,
)
from axiom.engine.rng import SeededRNG

CHART_CODES = [
    "1000", "1100", "1110", "1150", "1200", "1210", "1220", "1230", "1300", "1350", "1400", "1500", "1510", "1600", "1610",
    "1700", "1710", "1720", "1800", "2000", "2100", "2110", "2150", "2200", "2210", "2300", "2350", "2400", "2450", "2500",
    "2600", "2610", "2700", "3000", "3100", "3200", "4000", "4010", "4020", "4100", "4110", "4200", "5000", "5010", "5020",
    "5030", "5040", "5050", "6000", "6010", "6100", "6200", "6210", "6300", "6310", "6400", "6500", "7000", "7100", "7200", "8000",
]
D = dt.date(2024, 2, 15)


def test_chart_matches_spec_table() -> None:
    chart = canonical_chart_of_accounts()
    assert [a.code for a in chart] == CHART_CODES
    by = {a.code: a for a in chart}
    assert by["1110"].type == "contra_asset" and by["1110"].normal_balance == "credit"
    assert by["3200"].normal_balance == "debit" and by["4100"].type == "contra_revenue"
    assert all((a.statement == "IS") == (a.code[0] in "45678") for a in chart)
    assert set(CFS_MAP) == {a.code for a in chart if a.statement == "BS"} - {"1000"}


def test_rounding_half_away_from_zero() -> None:
    assert cents(2.5) == 3 and cents(-2.5) == -3 and cents(3.5) == 4 and cents(0.49) == 0
    assert dollars_to_cents(1.005) == 101 and dollars_to_cents(-0.125) == -13
    parts = split_cents(1000, [1, 1, 1])
    assert parts == [333, 333, 334] and sum(split_cents(99_999, [0.3, 0.45, 0.25])) == 99_999


def _families(L: Ledger) -> list:
    return [
        L.sell_on_credit(D, 10_000, tax_cents=800), L.sell_for_cash(D, 5_000, fee_cents=150), L.collect_cash(D, 4_000),
        L.recognize_deferred(D, 1_200), L.purchase_inventory(D, 7_000), L.relieve_inventory_cogs(D, 3_000),
        L.payroll(D, {"5010": 1_000, "6000": 2_000, "6100": 3_001}), L.accrue_opex(D, {"6200": 500, "6400": 700}),
        L.pay_ap(D, 2_000), L.capex(D, 9_000), L.depreciate(D, 150), L.borrow(D, 50_000), L.repay(D, 5_000),
        L.accrue_interest(D, 300), L.pay_interest(D, 300), L.provision_tax(D, 400), L.pay_tax(D, 400),
        L.returns_allowance(D, 600, restock_cost=250), L.bad_debt_provision(D, 120), L.bad_debt_provision(D, -20),
        L.write_off(D, 50), L.inventory_writedown(D, 75), L.chargeback(D, 900, fee_cents=1500), L.dividend(D, 1_000),
        L.equity_raise(D, 20_000), L.reclass(D, 2_500, "2610", "2600"), L.suspense(D, 333, "2000"),
        L.adjust(D, [("1300", 444, 0), ("2100", 0, 444)], "prepaid", "adjustment"),
    ]


def test_every_event_family_balances() -> None:
    L = Ledger()
    entries = _families(L)
    assert all(e is not None for e in entries)
    for e in entries:
        assert sum(ln.debit_cents for ln in e.lines) == sum(ln.credit_cents for ln in e.lines)
        assert all(ln.account in L.accounts for ln in e.lines)
    assert sum(L.closing_balances().values()) == 0
    assert L.sell_on_credit(D, 0) is None  # zero amounts post nothing
    assert len({e.id for e in L.entries}) == len(L.entries)


def test_ledger_rejects_bad_entries() -> None:
    L = Ledger()
    with pytest.raises(LedgerError):
        L.adjust(D, [("1000", 100, 0), ("9999", 0, 100)], "bad account")
    with pytest.raises(ValueError):
        L.adjust(D, [("1000", 100, 0), ("4000", 0, 99)], "unbalanced")
    with pytest.raises(LedgerError):
        L.open_books(D, {"1000": 100, "3000": -99})


def _healthy_ledger(seed: int) -> tuple[Ledger, FiscalCalendar]:
    """A random but healthy company: sales, collections, purchases, payroll, capex, debt, tax, dividends."""
    rng = SeededRNG(seed)
    cal = FiscalCalendar.from_seed(seed)
    L = Ledger()
    R = int(rng.uniform(5e8, 5e9))
    gm = rng.uniform(0.25, 0.6)
    ob = opening_balances(OpeningBalanceInputs(revenue_ttm_cents=R, gross_margin=gm, dso_days=rng.uniform(20, 60), dio_days=rng.uniform(20, 90),
                                               dpo_days=rng.uniform(20, 50), cash_pct_revenue=0.2, ebitda_ttm_cents=int(R * 0.12),
                                               debt_to_ebitda=rng.uniform(0.5, 2.5), inventory_split=(0.2, 0.1, 0.7)))
    L.open_books(cal.opening_date, ob)
    month_rev = R / 12
    for i, ms in enumerate(cal.month_starts()):
        g = (1.02 ** i) * rng.uniform(0.95, 1.05)
        rev = cents(month_rev * g)
        cogs = cents(rev * (1 - gm))
        d = ms + dt.timedelta(days=14)
        L.sell_on_credit(d, rev)
        L.purchase_inventory(d, cogs + cents(rng.uniform(-0.05, 0.05) * cogs), inventory_account="1220")
        L.relieve_inventory_cogs(d, cogs)
        L.collect_cash(d, cents(rev * rng.uniform(0.92, 1.02)))
        L.pay_ap(d, cents(cogs * rng.uniform(0.95, 1.03)))
        L.payroll(d, dict(zip(("5010", "6000", "6100", "6200"), split_cents(cents(rev * 0.18), [0.2, 0.3, 0.3, 0.2]), strict=True)))
        L.pay_ap(d, -L.closing_balances()["2110"], payable_account="2110")
        L.accrue_opex(d, {"6400": cents(rev * 0.03), "6200": cents(rev * 0.02)})
        L.pay_ap(d + dt.timedelta(days=5), cents(rev * 0.05), payable_account="2100")
        L.capex(d, cents(rev * 0.03))
        L.depreciate(d, cents(L.closing_balances()["1500"] / 84))
        L.bad_debt_provision(d, cents(L.closing_balances()["1100"] * 0.02) + L.closing_balances()["1110"])
        L.write_off(d, cents(rev * 0.001))
        L.accrue_interest(d, cents(-L.closing_balances()["2610"] * 0.08 / 12))
        if ms.month % 3 == 0:
            L.pay_interest(d, -L.closing_balances()["2150"])
            L.repay(d, cents(-L.closing_balances()["2600"] * 0.25), debt_account="2600")
            L.reclass(d, cents(-L.closing_balances()["2610"] * 0.05), "2610", "2600")
            act = L.activity(L.period_of(d))
            pretax = -sum(v for c, v in act.items() if c[0] in "4567")
            L.provision_tax(d, max(0, cents(pretax * 0.24)))
            L.dividend(d, cents(rev * 0.01))
        if ms.month % 3 == 1:
            L.pay_tax(d, -L.closing_balances()["2400"])
    return L, cal


@pytest.mark.parametrize("seed", range(20))
def test_statements_tie_on_random_healthy_ledgers(seed: int) -> None:
    L, cal = _healthy_ledger(seed)
    st = build_statements(L, cal.periods)
    assert len(st.periods) == 8
    checks = assert_invariants(L, st)
    names = {c.name.split("[")[0] for c in checks}
    assert {"bs_balances", "cfs_ties", "cfs_cfo_double_computation", "retained_earnings_rollforward", "trial_balance_balanced",
            "inventory_rollforward", "is_net_income_ties", "journal_entries_balanced"} <= names
    for b, cf, inc in zip(st.balance, st.cash_flow, st.income, strict=True):
        assert b.total_assets_cents == b.total_liabilities_cents + b.total_equity_cents
        assert cf.ending_cash_cents == L.trial_balance(b.period)["1000"]
        assert cf.operating_cents + cf.investing_cents + cf.financing_cents == cf.net_change_cents
        indirect = sum(ln.cents for ln in cf.lines if ln.level == 0 and ln.caption not in ("Beginning cash",)
                       and any(CFS_MAP.get(c, ("",))[0] == "operating" for c in ln.account_codes)) + inc.net_income_cents
        assert indirect == cf.operating_cents  # the CFO double computation, recomputed from the lines


def test_beneish_indices_sane_on_clean_company() -> None:
    L, cal = _healthy_ledger(3)
    st = build_statements(L, cal.periods)
    scores = []
    for p in cal.periods[1:]:
        r = st.ratios[p]
        for k in ("dsri", "gmi", "sgi", "sgai", "lvgi", "depi", "aqi"):
            assert 0.5 < r[k] < 2.0, (p, k, r[k])
        assert abs(r["tata"]) < 0.2
        scores.append(r["m_score"])
    assert np.median(scores) < -1.78  # a clean company should not look like a manipulator
    assert "m_score" not in st.ratios[cal.periods[0]]  # needs two periods


def test_ratios_present() -> None:
    L, cal = _healthy_ledger(5)
    r = build_statements(L, cal.periods).ratios[cal.periods[-1]]
    for k in ("dso", "dpo", "dio", "ccc", "gross_margin", "operating_margin", "net_margin", "current_ratio", "quick_ratio",
              "debt_to_ebitda", "interest_coverage", "accrual_ratio_sloan", "ar_growth_vs_revenue_growth", "allowance_pct_ar"):
        assert k in r and np.isfinite(r[k])
    assert r["ccc"] == pytest.approx(r["dso"] + r["dio"] - r["dpo"])


def test_subledger_tie_designed_break() -> None:
    L, cal = _healthy_ledger(1)
    st = build_statements(L, cal.periods)
    ar = {p: L.trial_balance(p)["1100"] for p in cal.periods}
    ok = SubledgerTie("ar", dict(ar), ["1100"])
    broken = SubledgerTie("ar_designed", {p: v + 12_345 for p, v in ar.items()}, ["1100"], expected_break_cents={p: 12_345 for p in ar})
    assert all(c.passed for c in check_invariants(L, st, [ok, broken]))
    wrong = SubledgerTie("ar_wrong", {p: v + 12_345 for p, v in ar.items()}, ["1100"], expected_break_cents={p: 12_000 for p in ar})
    with pytest.raises(WorldInvariantError) as exc:
        assert_invariants(L, st, [wrong])
    assert all(c.name.startswith("subledger:ar_wrong") for c in exc.value.failed)
    liab = SubledgerTie("ap", {p: -L.trial_balance(p)["2000"] for p in cal.periods}, ["2000"])  # credit-normal flip
    assert all(c.passed for c in check_invariants(L, st, [liab]) if c.name.startswith("subledger"))


def test_opening_balances_balance() -> None:
    ob = opening_balances(OpeningBalanceInputs(revenue_ttm_cents=10**10, gross_margin=0.4, dso_days=45, dio_days=60, dpo_days=30,
                                               ebitda_ttm_cents=10**9, debt_to_ebitda=2.0))
    assert sum(ob.values()) == 0 and ob["2600"] == -cents(2e9 * 0.2)
    distressed = opening_balances(OpeningBalanceInputs(revenue_ttm_cents=10**9, gross_margin=0.3, dpo_days=300, cash_pct_revenue=0.01))
    assert sum(distressed.values()) == 0 and distressed["3100"] > 0  # deficit goes to retained earnings


def test_calendar_from_seed() -> None:
    cal = FiscalCalendar.from_seed(42)
    assert cal.anchor == "2023Q3" and cal.periods[0] == "2021Q4" and cal.warmup_periods == ["2021Q3"]
    assert len(cal.month_starts()) == 27 and cal.opening_date == dt.date(2021, 6, 30)
