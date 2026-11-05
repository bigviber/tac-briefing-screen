"""Double-entry ledger engine, statements and invariants (archetype_common.md sections 2-6).

Everything is integer **cents**. Signed trial-balance convention: ``balance = sum(debits) -
sum(credits)`` for every account, so credit-normal accounts carry negative balances; statements
flip signs for presentation.

Main pieces
-----------
* :func:`canonical_chart_of_accounts` - the canonical chart (common section 2; the table lists
  61 accounts, all of which are implemented; nothing is renumbered).
* :class:`FiscalCalendar` - 8 quarters ``P1..P8`` plus warm-up quarter(s) before ``P1``; anchor
  derived from the seed (``anchor_year = 2023 + seed % 3``, ``anchor_quarter = 1 + (seed // 3) % 4``).
* :class:`Ledger` - posts validated, balanced :class:`~axiom.models.JournalEntry` objects and
  exposes one helper per journal event family (common section 4). Helpers return the posted entry,
  or ``None`` when the amount is zero (nothing is posted).
* :func:`build_statements` - income statement, balance sheet, indirect cash-flow statement and
  ratios per period (common section 5).
* :func:`check_invariants` / :func:`assert_invariants` - every ARCHITECTURE.md section 7 check.

Conventions decided here (and binding for all archetypes)
---------------------------------------------------------
* **No closing entries are posted.** Income-statement accounts accumulate since inception; the
  balance sheet presents ``Retained earnings = -(3100 + 3200) - sum(IS balances)``. Quarterly
  statements are computed from period activity (common section 4 "close"), so the GL tables
  (``gl_lines``) can be summed per quarter without filtering closing entries.
* **Cash-flow statement (indirect)** is computed from the change in *every* non-cash balance-sheet
  account, each mapped to exactly one CFS line (:data:`CFS_MAP`). Non-cash add-backs use the
  contra/accumulated accounts (``-delta 1510/1610/1720`` for D&A, ``-delta 1110`` for the
  allowance net of write-offs, ``-delta 1230`` for the inventory reserve) so write-offs are never
  double counted. CFO is computed twice - indirectly and as the residual ``net_change - CFI - CFF``
  - and the two must agree to the cent (``cfs_cfo_double_computation``).
* **Rounding** is half away from zero (:func:`cents`), not banker's rounding; splits give the
  residual to the last bucket (:func:`split_cents`).
* **Warm-up quarter.** Archetypes start the books at the beginning of a warm-up quarter ``P0``
  (opening entry ``OB-0000`` dated the day before) and simulate it like any other quarter so that
  opening AR/AP/inventory exist as real sub-ledger rows. Statements are produced for ``P1..P8``.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike, NDArray

from axiom.models import (
    Account,
    BalanceSheet,
    CashFlowStatement,
    FinancialStatements,
    IncomeStatement,
    InvariantCheck,
    JournalEntry,
    JournalLine,
    StatementLine,
)

__all__ = [
    "cents",
    "cents_array",
    "dollars_to_cents",
    "split_cents",
    "canonical_chart_of_accounts",
    "chart_by_code",
    "FiscalCalendar",
    "quarter_label",
    "quarter_bounds",
    "Ledger",
    "LedgerError",
    "OpeningBalanceInputs",
    "opening_balances",
    "build_statements",
    "SubledgerTie",
    "check_invariants",
    "assert_invariants",
    "WorldInvariantError",
    "CFS_MAP",
    "IS_CAPTIONS",
    "BENEISH_COEFFICIENTS",
]

logger = logging.getLogger("axiom.finance")

LineSpec = JournalLine | tuple[str, int, int] | tuple[str, int, int, Mapping[str, str]] | Mapping[str, Any]


# --------------------------------------------------------------------------- money helpers


def cents(x: float | int | Decimal) -> int:
    """Round an amount already expressed in cents to an integer, half away from zero.

    ``cents(2.5) == 3``, ``cents(-2.5) == -3`` (not banker's rounding).
    """
    if isinstance(x, (int, np.integer)):
        return int(x)
    d = Decimal(str(float(x))) if not isinstance(x, Decimal) else x
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def dollars_to_cents(x: float | int | Decimal) -> int:
    """Dollars -> integer cents, half away from zero (``dollars_to_cents(1.005) == 101``)."""
    d = Decimal(str(x)) * 100
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def cents_array(a: ArrayLike) -> NDArray[np.int64]:
    """Vectorised :func:`cents`: ``sign(a) * floor(|a| + 0.5)`` as int64."""
    arr = np.asarray(a, dtype=float)
    return (np.sign(arr) * np.floor(np.abs(arr) + 0.5)).astype(np.int64)


def split_cents(total: int, weights: Sequence[float]) -> list[int]:
    """Split ``total`` cents proportionally to ``weights``; the last bucket takes the residual
    so that ``sum(result) == total`` exactly (common section 4, rounding rule)."""
    if not weights:
        raise ValueError("split_cents needs at least one weight")
    w = np.asarray(weights, dtype=float)
    s = float(w.sum())
    if s <= 0:
        w = np.ones_like(w)
        s = float(len(w))
    parts = [cents(total * float(x) / s) for x in w[:-1]]
    parts.append(int(total) - sum(parts))
    return parts


# --------------------------------------------------------------------------- chart of accounts

_CHART: tuple[tuple[str, str, str, str, str, str, str | None], ...] = (
    ("1000", "Cash and cash equivalents", "asset", "debit", "BS", "current_assets", None),
    ("1100", "Accounts receivable, gross", "asset", "debit", "BS", "current_assets", None),
    ("1110", "Allowance for doubtful accounts", "contra_asset", "credit", "BS", "current_assets", "1100"),
    ("1150", "Unbilled receivables (contract assets)", "asset", "debit", "BS", "current_assets", None),
    ("1200", "Inventory - raw materials", "asset", "debit", "BS", "current_assets", None),
    ("1210", "Inventory - work in process", "asset", "debit", "BS", "current_assets", None),
    ("1220", "Inventory - finished goods / merchandise", "asset", "debit", "BS", "current_assets", None),
    ("1230", "Inventory reserve (obsolescence / shrink)", "contra_asset", "credit", "BS", "current_assets", "1220"),
    ("1300", "Prepaid expenses", "asset", "debit", "BS", "current_assets", None),
    ("1350", "Deferred contract costs (capitalized commissions)", "asset", "debit", "BS", "current_assets", None),
    ("1400", "Other current assets", "asset", "debit", "BS", "current_assets", None),
    ("1500", "Property, plant and equipment, gross", "asset", "debit", "BS", "ppe", None),
    ("1510", "Accumulated depreciation", "contra_asset", "credit", "BS", "ppe", "1500"),
    ("1600", "Capitalized software, gross", "asset", "debit", "BS", "intangibles", None),
    ("1610", "Accumulated amortization - software", "contra_asset", "credit", "BS", "intangibles", "1600"),
    ("1700", "Goodwill", "asset", "debit", "BS", "intangibles", None),
    ("1710", "Acquired intangibles, gross", "asset", "debit", "BS", "intangibles", None),
    ("1720", "Accumulated amortization - intangibles", "contra_asset", "credit", "BS", "intangibles", "1710"),
    ("1800", "Deposits and other non-current assets", "asset", "debit", "BS", "other_noncurrent_assets", None),
    ("2000", "Accounts payable", "liability", "credit", "BS", "current_liabilities", None),
    ("2100", "Accrued expenses", "liability", "credit", "BS", "current_liabilities", None),
    ("2110", "Accrued payroll and benefits", "liability", "credit", "BS", "current_liabilities", None),
    ("2150", "Accrued interest", "liability", "credit", "BS", "current_liabilities", None),
    ("2200", "Deferred revenue, current", "liability", "credit", "BS", "current_liabilities", None),
    ("2210", "Deferred revenue, non-current", "liability", "credit", "BS", "noncurrent_liabilities", "2200"),
    ("2300", "Customer deposits", "liability", "credit", "BS", "current_liabilities", None),
    ("2350", "Sales tax payable", "liability", "credit", "BS", "current_liabilities", None),
    ("2400", "Income taxes payable", "liability", "credit", "BS", "current_liabilities", None),
    ("2450", "Refund liability (returns reserve)", "liability", "credit", "BS", "current_liabilities", None),
    ("2500", "Revolving line of credit", "liability", "credit", "BS", "current_liabilities", None),
    ("2600", "Term debt - current portion", "liability", "credit", "BS", "current_liabilities", None),
    ("2610", "Term debt - non-current", "liability", "credit", "BS", "noncurrent_liabilities", "2600"),
    ("2700", "Lease liabilities", "liability", "credit", "BS", "noncurrent_liabilities", None),
    ("3000", "Common stock and APIC", "equity", "credit", "BS", "equity", None),
    ("3100", "Retained earnings", "equity", "credit", "BS", "equity", None),
    ("3200", "Distributions / dividends", "equity", "debit", "BS", "equity", "3100"),
    ("4000", "Product / subscription revenue", "revenue", "credit", "IS", "revenue", None),
    ("4010", "Services revenue", "revenue", "credit", "IS", "revenue", None),
    ("4020", "Usage / transaction revenue", "revenue", "credit", "IS", "revenue", None),
    ("4100", "Sales returns and allowances", "contra_revenue", "debit", "IS", "revenue", "4000"),
    ("4110", "Discounts and rebates", "contra_revenue", "debit", "IS", "revenue", "4000"),
    ("4200", "Other income", "revenue", "credit", "IS", "other_income", None),
    ("5000", "COGS - materials / merchandise / hosting", "expense", "debit", "IS", "cogs", None),
    ("5010", "COGS - direct labor", "expense", "debit", "IS", "cogs", None),
    ("5020", "COGS - overhead (manufacturing / infrastructure)", "expense", "debit", "IS", "cogs", None),
    ("5030", "COGS - freight and fulfillment", "expense", "debit", "IS", "cogs", None),
    ("5040", "Inventory write-downs and shrink", "expense", "debit", "IS", "cogs", None),
    ("5050", "Payment processing fees", "expense", "debit", "IS", "cogs", None),
    ("6000", "Sales and marketing", "expense", "debit", "IS", "opex", None),
    ("6010", "Commissions expense", "expense", "debit", "IS", "opex", "6000"),
    ("6100", "Research and development", "expense", "debit", "IS", "opex", None),
    ("6200", "General and administrative", "expense", "debit", "IS", "opex", None),
    ("6210", "Bad debt expense", "expense", "debit", "IS", "opex", "6200"),
    ("6300", "Depreciation", "expense", "debit", "IS", "opex", None),
    ("6310", "Amortization", "expense", "debit", "IS", "opex", None),
    ("6400", "Occupancy and rent", "expense", "debit", "IS", "opex", None),
    ("6500", "Chargeback, dispute and claim losses", "expense", "debit", "IS", "opex", None),
    ("7000", "Interest expense", "expense", "debit", "IS", "interest", None),
    ("7100", "Interest income", "revenue", "credit", "IS", "interest", None),
    ("7200", "Other non-operating gains / (losses)", "expense", "debit", "IS", "other", None),
    ("8000", "Income tax expense", "expense", "debit", "IS", "tax", None),
)


def canonical_chart_of_accounts() -> list[Account]:
    """The canonical chart (archetype_common.md section 2), ordered by code."""
    return [
        Account(code=c, name=n, type=t, normal_balance=nb, statement=st, section=sec, parent=par)  # type: ignore[arg-type]
        for c, n, t, nb, st, sec, par in _CHART
    ]


def chart_by_code(chart: Iterable[Account] | None = None) -> dict[str, Account]:
    """``{code: Account}`` for ``chart`` (default: canonical)."""
    return {a.code: a for a in (chart if chart is not None else canonical_chart_of_accounts())}


IS_CODES: tuple[str, ...] = tuple(c for c, *_rest in _CHART if _rest[3] == "IS")
BS_CODES: tuple[str, ...] = tuple(c for c, *_rest in _CHART if _rest[3] == "BS")
REVENUE_CODES = ("4000", "4010", "4020", "4100", "4110")
COGS_CODES = ("5000", "5010", "5020", "5030", "5040", "5050")
OPEX_CODES = ("6000", "6010", "6100", "6200", "6210", "6300", "6310", "6400", "6500")
INVENTORY_CODES = ("1200", "1210", "1220")
DEBT_CODES = ("2500", "2600", "2610")

# Income statement captions: caption -> account codes (sign handled by section).
IS_CAPTIONS: dict[str, tuple[str, ...]] = {
    "Revenue": REVENUE_CODES,
    "Cost of revenue": COGS_CODES,
    "Sales & marketing": ("6000", "6010"),
    "Research & development": ("6100",),
    "General & administrative": ("6200", "6210", "6400", "6500"),
    "Depreciation & amortization": ("6300", "6310"),
    "Interest expense, net": ("7000", "7100"),
    "Other (income) / expense": ("4200", "7200"),
    "Income tax": ("8000",),
}

# Cash-flow statement map: every non-cash balance-sheet account -> (section, caption).
# The line amount is ``-delta(balance)`` (signed debit convention) summed over its accounts.
CFS_MAP: dict[str, tuple[Literal["operating", "investing", "financing"], str]] = {
    "1510": ("operating", "Depreciation & amortization"),
    "1610": ("operating", "Depreciation & amortization"),
    "1720": ("operating", "Depreciation & amortization"),
    "1110": ("operating", "Provision for doubtful accounts, net of write-offs"),
    "1230": ("operating", "Inventory reserve and write-downs"),
    "1100": ("operating", "(Increase) / decrease in accounts receivable"),
    "1150": ("operating", "(Increase) / decrease in unbilled receivables"),
    "1200": ("operating", "(Increase) / decrease in inventory"),
    "1210": ("operating", "(Increase) / decrease in inventory"),
    "1220": ("operating", "(Increase) / decrease in inventory"),
    "1300": ("operating", "(Increase) / decrease in prepaid and other current assets"),
    "1350": ("operating", "(Increase) / decrease in prepaid and other current assets"),
    "1400": ("operating", "(Increase) / decrease in prepaid and other current assets"),
    "2000": ("operating", "Increase / (decrease) in accounts payable"),
    "2100": ("operating", "Increase / (decrease) in accrued liabilities"),
    "2110": ("operating", "Increase / (decrease) in accrued liabilities"),
    "2150": ("operating", "Increase / (decrease) in accrued liabilities"),
    "2200": ("operating", "Increase / (decrease) in deferred revenue"),
    "2210": ("operating", "Increase / (decrease) in deferred revenue"),
    "2300": ("operating", "Increase / (decrease) in customer deposits and other liabilities"),
    "2350": ("operating", "Increase / (decrease) in customer deposits and other liabilities"),
    "2400": ("operating", "Increase / (decrease) in customer deposits and other liabilities"),
    "2450": ("operating", "Increase / (decrease) in customer deposits and other liabilities"),
    "1500": ("investing", "Capital expenditures"),
    "1600": ("investing", "Capitalized software"),
    "1700": ("investing", "Acquisitions, net"),
    "1710": ("investing", "Acquisitions, net"),
    "1800": ("investing", "Other investing"),
    "2500": ("financing", "Net revolver borrowings / (repayments)"),
    "2600": ("financing", "Term debt borrowings / (repayments)"),
    "2610": ("financing", "Term debt borrowings / (repayments)"),
    "2700": ("financing", "Lease liabilities"),
    "3000": ("financing", "Equity issued"),
    "3100": ("financing", "Other equity"),
    "3200": ("financing", "Dividends / distributions"),
}

BENEISH_COEFFICIENTS: dict[str, float] = {
    "intercept": -4.84, "dsri": 0.920, "gmi": 0.528, "aqi": 0.404, "sgi": 0.892,
    "depi": 0.115, "sgai": -0.172, "tata": 4.679, "lvgi": -0.327,
}


# --------------------------------------------------------------------------- calendar


def quarter_label(d: dt.date) -> str:
    """Calendar-quarter label ``YYYYQn`` (fiscal year end = December)."""
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(label: str) -> tuple[dt.date, dt.date]:
    """``(first_day, last_day)`` of quarter ``label``."""
    year, q = int(label[:4]), int(label[5:])
    start = dt.date(year, 3 * (q - 1) + 1, 1)
    end_month = 3 * q
    end = (dt.date(year + 1, 1, 1) if end_month == 12 else dt.date(year, end_month + 1, 1)) - dt.timedelta(days=1)
    return start, end


def _shift_quarter(label: str, k: int) -> str:
    year, q = int(label[:4]), int(label[5:])
    idx = year * 4 + (q - 1) + k
    return f"{idx // 4}Q{idx % 4 + 1}"


@dataclass(frozen=True)
class FiscalCalendar:
    """Quarterly fiscal calendar: ``n_periods`` reporting quarters ending at the anchor, preceded by
    ``warmup`` simulated-but-unreported quarters.

    ``anchor_year = 2023 + seed % 3``; ``anchor_quarter = 1 + (seed // 3) % 4`` (common section 1).
    """

    anchor_year: int
    anchor_quarter: int
    n_periods: int = 8
    warmup: int = 1

    @classmethod
    def from_seed(cls, seed: int, n_periods: int = 8, warmup: int = 1) -> FiscalCalendar:
        """Calendar whose last quarter is derived from ``seed``."""
        return cls(2023 + int(seed) % 3, 1 + (int(seed) // 3) % 4, n_periods, warmup)

    @property
    def anchor(self) -> str:
        """Label of the last (current) quarter."""
        return f"{self.anchor_year}Q{self.anchor_quarter}"

    @property
    def periods(self) -> list[str]:
        """Reporting quarters ``P1..Pn`` (oldest first)."""
        return [_shift_quarter(self.anchor, k) for k in range(-(self.n_periods - 1), 1)]

    @property
    def warmup_periods(self) -> list[str]:
        """Simulated quarters before ``P1`` (oldest first)."""
        return [_shift_quarter(self.periods[0], -k) for k in range(self.warmup, 0, -1)]

    @property
    def all_periods(self) -> list[str]:
        """Warm-up quarters followed by reporting quarters."""
        return self.warmup_periods + self.periods

    @property
    def start(self) -> dt.date:
        """First simulated day (start of the first warm-up quarter)."""
        return quarter_bounds(self.all_periods[0])[0]

    @property
    def end(self) -> dt.date:
        """Last day of the anchor quarter."""
        return quarter_bounds(self.anchor)[1]

    @property
    def opening_date(self) -> dt.date:
        """Date of the opening entry (the day before :attr:`start`)."""
        return self.start - dt.timedelta(days=1)

    def p(self, i: int) -> str:
        """Label of reporting period ``P<i>`` (1-based; ``p(0)`` is the last warm-up quarter)."""
        return self.all_periods[self.warmup + i - 1]

    def bounds(self, label: str) -> tuple[dt.date, dt.date]:
        """``(start, end)`` dates of quarter ``label``."""
        return quarter_bounds(label)

    def days(self, label: str) -> int:
        """Number of days in quarter ``label``."""
        s, e = quarter_bounds(label)
        return (e - s).days + 1

    def month_starts(self) -> list[dt.date]:
        """First day of every simulated month, oldest first."""
        out: list[dt.date] = []
        for label in self.all_periods:
            s, _ = quarter_bounds(label)
            out += [dt.date(s.year, s.month + k, 1) for k in range(3)]
        return out

    def labels(self, dates: pd.Series | pd.DatetimeIndex | ArrayLike) -> NDArray[np.str_]:
        """Vectorised :func:`quarter_label`."""
        ts = pd.DatetimeIndex(pd.to_datetime(np.asarray(dates)))
        return np.char.add(np.char.add(ts.year.astype(str).to_numpy(), "Q"), ((ts.month.to_numpy() - 1) // 3 + 1).astype(str))

    def calendar_frame(self) -> pd.DataFrame:
        """``period_calendar`` rows: one per quarter (warm-up quarters have ``period_index <= 0``)."""
        rows = []
        for i, label in enumerate(self.all_periods):
            s, e = quarter_bounds(label)
            rows.append({
                "period": label, "period_index": i - self.warmup + 1, "is_reporting": i >= self.warmup,
                "start_date": s, "end_date": e, "fiscal_year": int(label[:4]), "fiscal_quarter": int(label[5:]),
                "days": (e - s).days + 1,
            })
        df = pd.DataFrame(rows)
        df["start_date"] = pd.to_datetime(df["start_date"])
        df["end_date"] = pd.to_datetime(df["end_date"])
        return df


def batch_end_dates(dates: pd.Series, granularity: Literal["daily", "weekly", "monthly"]) -> pd.Series:
    """Posting date of the GL batch a transaction date falls in. Batches never cross a month end:

    * ``daily`` - the date itself;
    * ``weekly`` - buckets days 1-7, 8-14, 15-21, 22-end of each month, dated at bucket end;
    * ``monthly`` - month end.
    """
    d = pd.to_datetime(dates).dt.normalize()
    if granularity == "daily":
        return d
    month_end = d + pd.offsets.MonthEnd(0)
    if granularity == "monthly":
        return month_end
    k = np.minimum((d.dt.day.to_numpy() - 1) // 7, 3)
    bucket_end = d.dt.to_period("M").dt.to_timestamp() + pd.to_timedelta(k * 7 + 6, unit="D")
    return pd.Series(np.where(k == 3, month_end, bucket_end), index=d.index).astype("datetime64[ns]")


# --------------------------------------------------------------------------- ledger


class LedgerError(ValueError):
    """An entry could not be posted (unbalanced, unknown account, negative amount...)."""


class Ledger:
    """General ledger: validated balanced entries plus per-period activity.

    ``post`` validates: balanced (``sum debits == sum credits``), at least two lines, accounts in
    the chart, non-negative single-sided lines. Entry ids are sequential (``JE-000001``), so the
    ledger is deterministic given the order of calls.
    """

    def __init__(self, chart: Sequence[Account] | None = None, *, period_of: Callable[[dt.date], str] = quarter_label) -> None:
        """Create an empty ledger over ``chart`` (default: canonical chart)."""
        self.chart: list[Account] = list(chart) if chart is not None else canonical_chart_of_accounts()
        self.accounts: dict[str, Account] = chart_by_code(self.chart)
        self.period_of = period_of
        self.entries: list[JournalEntry] = []
        self._activity: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._seq = 0

    # ------------------------------------------------------------------ core

    def _coerce_line(self, spec: LineSpec) -> JournalLine:
        if isinstance(spec, JournalLine):
            return spec
        if isinstance(spec, Mapping):
            return JournalLine(**spec)
        account, debit, credit, *rest = spec
        dim = {str(k): str(v) for k, v in (rest[0] if rest else {}).items()}
        return JournalLine(account=str(account), debit_cents=int(debit), credit_cents=int(credit), dimension=dim)

    def post(self, entry: JournalEntry) -> JournalEntry:
        """Validate and post ``entry``. Raises :class:`LedgerError` if invalid."""
        if len(entry.lines) < 2:
            raise LedgerError(f"{entry.id}: an entry needs at least two lines")
        dr = cr = 0
        for line in entry.lines:
            if line.account not in self.accounts:
                raise LedgerError(f"{entry.id}: unknown account {line.account}")
            if line.debit_cents and line.credit_cents:
                raise LedgerError(f"{entry.id}: line on {line.account} has both debit and credit")
            dr += line.debit_cents
            cr += line.credit_cents
        if dr != cr:
            raise LedgerError(f"{entry.id}: debits {dr} != credits {cr}")
        act = self._activity[entry.period]
        for line in entry.lines:
            act[line.account] += line.debit_cents - line.credit_cents
        self.entries.append(entry)
        return entry

    def entry(
        self,
        date: dt.date,
        lines: Sequence[LineSpec],
        memo: str,
        source: str,
        *,
        entry_id: str | None = None,
        period: str | None = None,
    ) -> JournalEntry | None:
        """Build and post an entry; zero-amount lines are dropped and an all-zero entry posts
        nothing (returns ``None``)."""
        coerced = [ln for ln in (self._coerce_line(s) for s in lines) if ln.debit_cents or ln.credit_cents]
        if not coerced:
            return None
        for ln in coerced:
            if ln.debit_cents < 0 or ln.credit_cents < 0:
                raise LedgerError(f"negative amount on {ln.account}")
        if entry_id is None:
            self._seq += 1
            entry_id = f"JE-{self._seq:06d}"
        je = JournalEntry(id=entry_id, date=date, period=period or self.period_of(date), memo=memo, source=source, lines=coerced)
        return self.post(je)

    def adjust(self, date: dt.date, lines: Sequence[LineSpec], memo: str, source: str = "adjustment") -> JournalEntry | None:
        """Generic balanced entry (any lines). ``source`` defaults to ``"adjustment"``."""
        return self.entry(date, lines, memo, source)

    def _two(self, date: dt.date, debit: str, credit: str, amount: int, memo: str, source: str,
             dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        amount = int(amount)
        if amount == 0:
            return None
        if amount < 0:  # a negative amount reverses the template
            debit, credit, amount = credit, debit, -amount
        d = dict(dim or {})
        return self.entry(date, [(debit, amount, 0, d), (credit, 0, amount, d)], memo, source)

    # ------------------------------------------------------------------ event families (common section 4)

    def open_books(self, date: dt.date, balances: Mapping[str, int], *, memo: str = "Opening balance sheet") -> JournalEntry:
        """Post the opening entry ``OB-0000`` from signed (debit-positive) balances that sum to 0."""
        total = sum(int(v) for v in balances.values())
        if total != 0:
            raise LedgerError(f"opening balances do not net to zero (off by {total})")
        lines = [(code, int(v), 0) if v > 0 else (code, 0, -int(v)) for code, v in sorted(balances.items()) if v]
        je = self.entry(date, lines, memo, "opening", entry_id="OB-0000")
        assert je is not None
        return je

    def sell_on_credit(self, date: dt.date, amount: int, *, revenue_account: str = "4000", tax_cents: int = 0,
                       memo: str = "Credit sales", source: str = "sales", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``sale_credit``: Dr 1100 A+tax; Cr 4000/4010/4020 A; Cr 2350 tax."""
        d = dict(dim or {})
        return self.entry(date, [("1100", amount + tax_cents, 0, d), (revenue_account, 0, amount, d), ("2350", 0, tax_cents, d)], memo, source)

    def sell_for_cash(self, date: dt.date, amount: int, *, fee_cents: int = 0, revenue_account: str = "4000",
                      memo: str = "Cash sales", source: str = "sales", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``sale_cash``: Dr 1000 A; Cr revenue A; fee Dr 5050 / Cr 1000."""
        d = dict(dim or {})
        return self.entry(date, [("1000", amount - fee_cents, 0, d), ("5050", fee_cents, 0, d), (revenue_account, 0, amount, d)], memo, source)

    def collect_cash(self, date: dt.date, amount: int, *, from_account: str = "1100", memo: str = "Customer collections",
                     source: str = "collections", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``collection``: Dr 1000; Cr 1100 (or ``from_account``)."""
        return self._two(date, "1000", from_account, amount, memo, source, dim)

    def recognize_deferred(self, date: dt.date, amount: int, *, revenue_account: str = "4000", deferred_account: str = "2200",
                           memo: str = "Deferred revenue recognized", source: str = "revenue_recognition",
                           dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``deferred_revenue`` recognition: Dr 2200; Cr 4000."""
        return self._two(date, deferred_account, revenue_account, amount, memo, source, dim)

    def purchase_inventory(self, date: dt.date, amount: int, *, inventory_account: str = "1220", payable_account: str = "2000",
                           memo: str = "Inventory received", source: str = "purchasing",
                           dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``purchase_inventory``: Dr 1200/1220; Cr 2000."""
        return self._two(date, inventory_account, payable_account, amount, memo, source, dim)

    def relieve_inventory_cogs(self, date: dt.date, cost: int, *, inventory_account: str = "1220", cogs_account: str = "5000",
                               memo: str = "COGS relief", source: str = "fulfillment",
                               dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``cogs_relief``: Dr 5000; Cr 1220."""
        return self._two(date, cogs_account, inventory_account, cost, memo, source, dim)

    def payroll(self, date: dt.date, by_account: Mapping[str, int], *, liability_account: str = "2110",
                memo: str = "Payroll accrual", source: str = "payroll") -> JournalEntry | None:
        """``payroll``: Dr 5010/6000/6100/6200 by department; Cr 2110 total. Pay with :meth:`pay_ap`
        (``payable_account="2110"``)."""
        total = sum(int(v) for v in by_account.values())
        lines: list[LineSpec] = [(acct, int(v), 0, {"department": acct}) for acct, v in sorted(by_account.items())]
        lines.append((liability_account, 0, total))
        return self.entry(date, lines, memo, source)

    def accrue_opex(self, date: dt.date, by_account: Mapping[str, int], *, liability_account: str = "2100",
                    memo: str = "Operating expense accrual", source: str = "opex") -> JournalEntry | None:
        """``opex_accrual``: Dr 6000/6100/6200/6400 ...; Cr 2100."""
        total = sum(int(v) for v in by_account.values())
        lines: list[LineSpec] = [(acct, int(v), 0) for acct, v in sorted(by_account.items())]
        lines.append((liability_account, 0, total))
        return self.entry(date, lines, memo, source)

    def pay_ap(self, date: dt.date, amount: int, *, payable_account: str = "2000", memo: str = "Vendor payment",
               source: str = "payables", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``vendor_payment`` (any liability settlement): Dr 2000/2100/2110; Cr 1000."""
        return self._two(date, payable_account, "1000", amount, memo, source, dim)

    def capex(self, date: dt.date, amount: int, *, asset_account: str = "1500", funding_account: str = "1000",
              memo: str = "Capital expenditure", source: str = "capex", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``capex``: Dr 1500 (or 1600 for capitalized software); Cr 1000 (or 2000)."""
        return self._two(date, asset_account, funding_account, amount, memo, source, dim)

    def depreciate(self, date: dt.date, amount: int, *, expense_account: str = "6300", contra_account: str = "1510",
                   memo: str = "Depreciation", source: str = "depreciation") -> JournalEntry | None:
        """``depreciation``: Dr 6300; Cr 1510 (amortization: 6310 / 1610 or 1720)."""
        return self._two(date, expense_account, contra_account, amount, memo, source)

    def borrow(self, date: dt.date, amount: int, *, debt_account: str = "2500", memo: str = "Debt draw",
               source: str = "financing") -> JournalEntry | None:
        """``debt_draw``: Dr 1000; Cr 2500/2610."""
        return self._two(date, "1000", debt_account, amount, memo, source)

    def repay(self, date: dt.date, amount: int, *, debt_account: str = "2500", memo: str = "Debt repayment",
              source: str = "financing") -> JournalEntry | None:
        """``repay``: Dr 2500/2600; Cr 1000."""
        return self._two(date, debt_account, "1000", amount, memo, source)

    def accrue_interest(self, date: dt.date, amount: int, *, memo: str = "Interest accrual", source: str = "interest") -> JournalEntry | None:
        """``interest`` accrual: Dr 7000; Cr 2150."""
        return self._two(date, "7000", "2150", amount, memo, source)

    def pay_interest(self, date: dt.date, amount: int, *, memo: str = "Interest paid", source: str = "interest") -> JournalEntry | None:
        """``interest`` payment: Dr 2150; Cr 1000."""
        return self._two(date, "2150", "1000", amount, memo, source)

    def provision_tax(self, date: dt.date, amount: int, *, memo: str = "Income tax provision", source: str = "tax") -> JournalEntry | None:
        """``tax``: Dr 8000; Cr 2400 (a negative amount records a benefit)."""
        return self._two(date, "8000", "2400", amount, memo, source)

    def pay_tax(self, date: dt.date, amount: int, *, memo: str = "Income tax paid", source: str = "tax") -> JournalEntry | None:
        """Tax payment: Dr 2400; Cr 1000."""
        return self._two(date, "2400", "1000", amount, memo, source)

    def returns_allowance(self, date: dt.date, amount: int, *, settle_account: str = "1100", restock_cost: int = 0,
                          memo: str = "Sales return", source: str = "returns", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``return``: Dr 4100; Cr 1100 (unpaid) or 1000 (refund); restock Dr 1220; Cr 5000."""
        d = dict(dim or {})
        return self.entry(date, [("4100", amount, 0, d), (settle_account, 0, amount, d),
                                 ("1220", restock_cost, 0, d), ("5000", 0, restock_cost, d)], memo, source)

    def bad_debt_provision(self, date: dt.date, amount: int, *, memo: str = "Bad debt provision", source: str = "provision") -> JournalEntry | None:
        """``bad_debt_provision``: Dr 6210; Cr 1110 (negative = release: Dr 1110; Cr 6210)."""
        return self._two(date, "6210", "1110", amount, memo, source)

    def write_off(self, date: dt.date, amount: int, *, memo: str = "Receivable written off", source: str = "write_off",
                  dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``write_off``: Dr 1110; Cr 1100."""
        return self._two(date, "1110", "1100", amount, memo, source, dim)

    def inventory_writedown(self, date: dt.date, amount: int, *, reserve_account: str = "1230", memo: str = "Inventory write-down",
                            source: str = "inventory") -> JournalEntry | None:
        """``inventory_writedown``: Dr 5040; Cr 1230 (or 1220 for direct shrink)."""
        return self._two(date, "5040", reserve_account, amount, memo, source)

    def chargeback(self, date: dt.date, amount: int, *, fee_cents: int = 0, loss_account: str = "6500",
                   memo: str = "Chargeback lost", source: str = "disputes", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """``chargeback``: Dr 6500 (or another account bearing the loss) A; Dr 6500 fee; Cr 1000."""
        d = dict(dim or {})
        return self.entry(date, [(loss_account, amount, 0, d), ("6500", fee_cents, 0, d), ("1000", 0, amount + fee_cents, d)], memo, source)

    def dividend(self, date: dt.date, amount: int, *, memo: str = "Dividend paid", source: str = "equity") -> JournalEntry | None:
        """``dividend``: Dr 3200; Cr 1000."""
        return self._two(date, "3200", "1000", amount, memo, source)

    def equity_raise(self, date: dt.date, amount: int, *, memo: str = "Equity raise", source: str = "equity") -> JournalEntry | None:
        """Equity issuance: Dr 1000; Cr 3000."""
        return self._two(date, "1000", "3000", amount, memo, source)

    def reclass(self, date: dt.date, amount: int, debit_account: str, credit_account: str, *, memo: str = "Reclassification",
                source: str = "reclass") -> JournalEntry | None:
        """Reclass between accounts, e.g. term debt current portion: Dr 2610; Cr 2600."""
        return self._two(date, debit_account, credit_account, amount, memo, source)

    def suspense(self, date: dt.date, amount: int, counter_account: str, *, direction: Literal["debit", "credit"] = "debit",
                 memo: str = "Posted to suspense", source: str = "suspense", dim: Mapping[str, str] | None = None) -> JournalEntry | None:
        """Park an unexplained amount in suspense (account 1400 *Other current assets*; the chart has
        no dedicated suspense code). ``direction="debit"``: Dr 1400; Cr ``counter_account``."""
        if direction == "debit":
            return self._two(date, "1400", counter_account, amount, memo, source, dim)
        return self._two(date, counter_account, "1400", amount, memo, source, dim)

    # ------------------------------------------------------------------ queries

    @property
    def period_order(self) -> list[str]:
        """Periods with activity, sorted chronologically (labels sort lexicographically)."""
        return sorted(self._activity)

    def activity(self, period: str) -> dict[str, int]:
        """Net posting (debits - credits) per account in ``period`` (all chart codes, zeros included)."""
        act = self._activity.get(period, {})
        return {code: int(act.get(code, 0)) for code in self.accounts}

    def trial_balance(self, period: str) -> dict[str, int]:
        """Cumulative signed balances (debit-positive) at the end of ``period``."""
        out = {code: 0 for code in self.accounts}
        for p in self.period_order:
            if p > period:
                break
            for code, v in self._activity[p].items():
                out[code] += v
        return out

    def closing_balances(self) -> dict[str, int]:
        """Signed balances after every posted entry."""
        out = {code: 0 for code in self.accounts}
        for act in self._activity.values():
            for code, v in act.items():
                out[code] += v
        return out

    def balance(self, code: str, period: str | None = None) -> int:
        """Signed balance of one account (end of ``period`` or latest)."""
        return (self.trial_balance(period) if period else self.closing_balances())[code]

    def balance_as_of(self, codes: Iterable[str], date: dt.date) -> int:
        """Signed balance of ``codes`` after all entries dated on or before ``date``."""
        cs = set(codes)
        return sum(ln.debit_cents - ln.credit_cents for e in self.entries if e.date <= date for ln in e.lines if ln.account in cs)

    def sum_lines(self, period: str | None, accounts: Iterable[str], side: Literal["debit", "credit", "net"] = "net",
                  source_prefix: str | None = None) -> int:
        """Sum of lines on ``accounts`` (optionally one period / entries whose source starts with
        ``source_prefix``). ``net`` = debits - credits."""
        cs = set(accounts)
        total = 0
        for e in self.entries:
            if period is not None and e.period != period:
                continue
            if source_prefix is not None and not e.source.startswith(source_prefix):
                continue
            for ln in e.lines:
                if ln.account in cs:
                    total += ln.debit_cents if side == "debit" else (ln.credit_cents if side == "credit" else ln.debit_cents - ln.credit_cents)
        return total

    def entries_frame(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """``(gl_journal, gl_lines)`` DataFrames mirroring the journal row-for-row (money in cents).

        Typed ``dimension_<key>`` columns are extracted for ``customer_id``, ``product_id``, ``department``,
        ``invoice_id`` and ``sku`` (NULL when the line has no such dimension); ``dimensions`` keeps every
        dimension as ``key=value;...`` (sorted by key)."""
        j_rows = [(e.id, e.date, e.period, e.source, e.memo) for e in self.entries]
        journal = pd.DataFrame(j_rows, columns=["entry_id", "entry_date", "period", "source", "memo"])
        journal["entry_date"] = pd.to_datetime(journal["entry_date"])
        l_rows = []
        n = 0
        for e in self.entries:
            for ln in e.lines:
                n += 1
                dim = ln.dimension
                l_rows.append((n, e.id, ln.account, ln.debit_cents, ln.credit_cents, dim.get("customer_id"), dim.get("product_id"),
                               dim.get("department"), dim.get("invoice_id"), dim.get("sku"),
                               ";".join(f"{k}={v}" for k, v in sorted(dim.items())) or None))
        lines = pd.DataFrame(l_rows, columns=["line_id", "entry_id", "account_code", "debit_cents", "credit_cents",
                                              "dimension_customer_id", "dimension_product_id", "dimension_department",
                                              "dimension_invoice_id", "dimension_sku", "dimensions"])
        return journal, lines


# --------------------------------------------------------------------------- opening balance sheet


@dataclass
class OpeningBalanceInputs:
    """Inputs of the opening balance sheet (common section 3); every amount in cents or days."""

    revenue_ttm_cents: int
    gross_margin: float
    dso_days: float = 0.0
    dio_days: float = 0.0
    dpo_days: float = 0.0
    deferred_revenue_days: float = 0.0
    capex_pct_revenue: float = 0.03
    ppe_life_years: float = 7.0
    cash_pct_revenue: float = 0.12
    ebitda_ttm_cents: int = 0
    debt_to_ebitda: float = 0.0
    inventory_split: tuple[float, float, float] = (0.0, 0.0, 1.0)  # RM / WIP / FG


def opening_balances(inp: OpeningBalanceInputs) -> dict[str, int]:
    """Signed opening balances per common section 3 (debit-positive; sums to zero).

    ``AR0 = R*dso/365``; ``INV0 = R*(1-gm)*dio/365``; ``AP0 = R*(1-gm)*dpo/365``;
    ``DR0 = R*deferred_days/365``; ``PPE0 = R*capex%*life*0.55``; ``AccDep0 = 0.45*PPE0``;
    ``Cash0 = R*cash%``; ``Debt0 = EBITDA*debt_to_ebitda`` split 20% current / 80% non-current;
    equity plug: common stock 60% of net assets, retained earnings the rest (if net assets < 0,
    common stock = max(0.25*assets, 1) and retained earnings absorbs the deficit).
    """
    r = inp.revenue_ttm_cents
    ar = cents(r * inp.dso_days / 365)
    inv = cents(r * (1 - inp.gross_margin) * inp.dio_days / 365)
    ap = cents(r * (1 - inp.gross_margin) * inp.dpo_days / 365)
    dr = cents(r * inp.deferred_revenue_days / 365)
    ppe = cents(r * inp.capex_pct_revenue * inp.ppe_life_years * 0.55)
    accdep = cents(ppe * 0.45)
    cash = cents(r * inp.cash_pct_revenue)
    debt = cents(max(0, inp.ebitda_ttm_cents) * inp.debt_to_ebitda)
    debt_cur = cents(debt * 0.2)
    rm, wip, fg = split_cents(inv, list(inp.inventory_split))
    assets = cash + ar + inv + ppe - accdep
    liabilities = ap + dr + debt
    net = assets - liabilities
    if net >= 0:
        cs = cents(0.6 * net)
        re = net - cs
    else:
        cs = max(cents(0.25 * assets), 1)
        re = net - cs
    bal = {"1000": cash, "1100": ar, "1200": rm, "1210": wip, "1220": fg, "1500": ppe, "1510": -accdep,
           "2000": -ap, "2200": -dr, "2600": -debt_cur, "2610": -(debt - debt_cur), "3000": -cs, "3100": -re}
    assert sum(bal.values()) == 0
    return {k: v for k, v in bal.items() if v}


# --------------------------------------------------------------------------- statements


def _sum(d: Mapping[str, int], codes: Iterable[str]) -> int:
    return sum(int(d.get(c, 0)) for c in codes)


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b == 0 or not math.isfinite(a) or not math.isfinite(b):
        return default
    v = a / b
    return v if math.isfinite(v) else default


def _clip(v: float, lim: float = 999.0) -> float:
    return float(max(-lim, min(lim, v)))


@dataclass
class _PeriodFigures:
    """Raw figures of one period used by statements, ratios and invariants."""

    period: str
    days: int
    act: dict[str, int]
    tb: dict[str, int]
    tb_prev: dict[str, int]
    revenue: int = 0
    cogs: int = 0
    gross_profit: int = 0
    sm: int = 0
    rd: int = 0
    ga: int = 0
    da: int = 0
    opex: int = 0
    operating_income: int = 0
    interest_net: int = 0
    other: int = 0
    pretax: int = 0
    tax: int = 0
    net_income: int = 0
    cash: int = 0
    ar_gross: int = 0
    ar_net: int = 0
    inventory: int = 0
    other_current: int = 0
    tca: int = 0
    ppe_net: int = 0
    intangibles: int = 0
    other_nc: int = 0
    total_assets: int = 0
    ap: int = 0
    tcl: int = 0
    tncl: int = 0
    total_liabilities: int = 0
    common_stock: int = 0
    retained_earnings: int = 0
    total_equity: int = 0
    debt: int = 0
    cfo: int = 0
    cfo_residual: int = 0
    cfi: int = 0
    cff: int = 0
    net_change: int = 0
    begin_cash: int = 0
    dividends: int = 0
    cfs_lines: dict[str, tuple[str, int, list[str]]] = field(default_factory=dict)


def _figures(ledger: Ledger, period: str, prev_tb: dict[str, int]) -> _PeriodFigures:
    s, e = quarter_bounds(period)
    act = ledger.activity(period)
    tb = ledger.trial_balance(period)
    f = _PeriodFigures(period=period, days=(e - s).days + 1, act=act, tb=tb, tb_prev=prev_tb)
    f.revenue = -_sum(act, REVENUE_CODES)
    f.cogs = _sum(act, COGS_CODES)
    f.gross_profit = f.revenue - f.cogs
    f.sm = _sum(act, IS_CAPTIONS["Sales & marketing"])
    f.rd = _sum(act, IS_CAPTIONS["Research & development"])
    f.ga = _sum(act, IS_CAPTIONS["General & administrative"])
    f.da = _sum(act, IS_CAPTIONS["Depreciation & amortization"])
    f.opex = f.sm + f.rd + f.ga + f.da
    f.operating_income = f.gross_profit - f.opex
    f.interest_net = _sum(act, ("7000", "7100"))
    f.other = _sum(act, ("4200", "7200"))
    f.pretax = f.operating_income - f.interest_net - f.other
    f.tax = act["8000"]
    f.net_income = f.pretax - f.tax
    # balance sheet
    f.cash = tb["1000"]
    f.ar_gross = tb["1100"]
    f.ar_net = _sum(tb, ("1100", "1110", "1150"))
    f.inventory = _sum(tb, ("1200", "1210", "1220", "1230"))
    f.other_current = _sum(tb, ("1300", "1350", "1400"))
    f.tca = f.cash + f.ar_net + f.inventory + f.other_current
    f.ppe_net = _sum(tb, ("1500", "1510"))
    f.intangibles = _sum(tb, ("1600", "1610", "1700", "1710", "1720"))
    f.other_nc = tb["1800"]
    f.total_assets = f.tca + f.ppe_net + f.intangibles + f.other_nc
    f.ap = -tb["2000"]
    f.tcl = -_sum(tb, ("2000", "2100", "2110", "2150", "2200", "2300", "2350", "2400", "2450", "2500", "2600"))
    f.tncl = -_sum(tb, ("2210", "2610", "2700"))
    f.total_liabilities = f.tcl + f.tncl
    f.common_stock = -tb["3000"]
    f.retained_earnings = -_sum(tb, ("3100", "3200")) - _sum(tb, IS_CODES)
    f.total_equity = f.common_stock + f.retained_earnings
    f.debt = -_sum(tb, DEBT_CODES)
    f.dividends = act["3200"]
    # cash flow (indirect): -delta of every non-cash BS account
    lines: dict[str, tuple[str, int, list[str]]] = {}
    for code, (section, caption) in CFS_MAP.items():
        delta = tb[code] - prev_tb.get(code, 0)
        sec, amt, codes = lines.get(caption, (section, 0, []))
        lines[caption] = (sec, amt - delta, codes + [code])
    f.cfs_lines = lines
    f.cfi = sum(a for sec, a, _ in lines.values() if sec == "investing")
    f.cff = sum(a for sec, a, _ in lines.values() if sec == "financing")
    f.cfo = f.net_income + sum(a for sec, a, _ in lines.values() if sec == "operating")
    f.begin_cash = prev_tb.get("1000", 0)
    f.net_change = f.cash - f.begin_cash
    f.cfo_residual = f.net_change - f.cfi - f.cff
    return f


def _income_statement(f: _PeriodFigures) -> IncomeStatement:
    lines = [
        StatementLine(caption="Revenue", account_codes=list(REVENUE_CODES), cents=f.revenue),
        StatementLine(caption="Cost of revenue", account_codes=list(COGS_CODES), cents=f.cogs),
        StatementLine(caption="Gross profit", cents=f.gross_profit, level=1),
        StatementLine(caption="Sales & marketing", account_codes=list(IS_CAPTIONS["Sales & marketing"]), cents=f.sm),
        StatementLine(caption="Research & development", account_codes=list(IS_CAPTIONS["Research & development"]), cents=f.rd),
        StatementLine(caption="General & administrative", account_codes=list(IS_CAPTIONS["General & administrative"]), cents=f.ga),
        StatementLine(caption="Depreciation & amortization", account_codes=list(IS_CAPTIONS["Depreciation & amortization"]), cents=f.da),
        StatementLine(caption="Total operating expenses", account_codes=list(OPEX_CODES), cents=f.opex, level=1),
        StatementLine(caption="Operating income", cents=f.operating_income, level=1),
        StatementLine(caption="Interest expense, net", account_codes=["7000", "7100"], cents=f.interest_net),
        StatementLine(caption="Other (income) / expense", account_codes=["4200", "7200"], cents=f.other),
        StatementLine(caption="Pretax income", cents=f.pretax, level=1),
        StatementLine(caption="Income tax", account_codes=["8000"], cents=f.tax),
        StatementLine(caption="Net income", cents=f.net_income, level=2),
    ]
    return IncomeStatement(period=f.period, lines=lines, revenue_cents=f.revenue, gross_profit_cents=f.gross_profit,
                           operating_income_cents=f.operating_income, net_income_cents=f.net_income)


def _balance_sheet(f: _PeriodFigures) -> BalanceSheet:
    tb = f.tb

    def neg(codes: Sequence[str]) -> int:
        return -_sum(tb, codes)

    lines = [
        StatementLine(caption="Cash and cash equivalents", account_codes=["1000"], cents=f.cash),
        StatementLine(caption="Accounts receivable, net", account_codes=["1100", "1110", "1150"], cents=f.ar_net),
        StatementLine(caption="Inventory, net", account_codes=["1200", "1210", "1220", "1230"], cents=f.inventory),
        StatementLine(caption="Prepaid expenses and other current assets", account_codes=["1300", "1350", "1400"], cents=f.other_current),
        StatementLine(caption="Total current assets", cents=f.tca, level=1),
        StatementLine(caption="Property, plant and equipment, net", account_codes=["1500", "1510"], cents=f.ppe_net),
        StatementLine(caption="Goodwill and intangibles, net", account_codes=["1600", "1610", "1700", "1710", "1720"], cents=f.intangibles),
        StatementLine(caption="Other non-current assets", account_codes=["1800"], cents=f.other_nc),
        StatementLine(caption="Total assets", cents=f.total_assets, level=2),
        StatementLine(caption="Accounts payable", account_codes=["2000"], cents=f.ap),
        StatementLine(caption="Accrued liabilities", account_codes=["2100", "2110", "2150"], cents=neg(("2100", "2110", "2150"))),
        StatementLine(caption="Deferred revenue, current", account_codes=["2200"], cents=neg(("2200",))),
        StatementLine(caption="Customer deposits", account_codes=["2300"], cents=neg(("2300",))),
        StatementLine(caption="Taxes payable", account_codes=["2350", "2400"], cents=neg(("2350", "2400"))),
        StatementLine(caption="Refund liability", account_codes=["2450"], cents=neg(("2450",))),
        StatementLine(caption="Revolving line of credit", account_codes=["2500"], cents=neg(("2500",))),
        StatementLine(caption="Current portion of term debt", account_codes=["2600"], cents=neg(("2600",))),
        StatementLine(caption="Total current liabilities", cents=f.tcl, level=1),
        StatementLine(caption="Deferred revenue, non-current", account_codes=["2210"], cents=neg(("2210",))),
        StatementLine(caption="Term debt, non-current", account_codes=["2610"], cents=neg(("2610",))),
        StatementLine(caption="Lease liabilities", account_codes=["2700"], cents=neg(("2700",))),
        StatementLine(caption="Total liabilities", cents=f.total_liabilities, level=2),
        StatementLine(caption="Common stock and APIC", account_codes=["3000"], cents=f.common_stock),
        StatementLine(caption="Retained earnings", account_codes=["3100", "3200"], cents=f.retained_earnings),
        StatementLine(caption="Total equity", cents=f.total_equity, level=2),
        StatementLine(caption="Total liabilities and equity", cents=f.total_liabilities + f.total_equity, level=2),
    ]
    return BalanceSheet(period=f.period, lines=lines, total_assets_cents=f.total_assets,
                        total_liabilities_cents=f.total_liabilities, total_equity_cents=f.total_equity)


def _cash_flow(f: _PeriodFigures) -> CashFlowStatement:
    lines = [StatementLine(caption="Net income", cents=f.net_income)]
    for section, title, total in (("operating", "Net cash from operating activities", f.cfo),
                                  ("investing", "Net cash from investing activities", f.cfi),
                                  ("financing", "Net cash from financing activities", f.cff)):
        for caption, (sec, amt, codes) in f.cfs_lines.items():
            if sec == section:
                lines.append(StatementLine(caption=caption, account_codes=sorted(codes), cents=amt))
        lines.append(StatementLine(caption=title, cents=total, level=1))
    lines += [
        StatementLine(caption="Net change in cash", cents=f.net_change, level=2),
        StatementLine(caption="Beginning cash", account_codes=["1000"], cents=f.begin_cash),
        StatementLine(caption="Ending cash", account_codes=["1000"], cents=f.cash, level=2),
    ]
    return CashFlowStatement(period=f.period, operating_cents=f.cfo, investing_cents=f.cfi, financing_cents=f.cff,
                             net_change_cents=f.net_change, beginning_cash_cents=f.begin_cash, ending_cash_cents=f.cash, lines=lines)


def _ratios(figs: list[_PeriodFigures]) -> dict[str, dict[str, float]]:
    """Ratios per period (common section 5.4 plus Beneish indices vs the previous period)."""
    out: dict[str, dict[str, float]] = {}
    for i, f in enumerate(figs):
        prev = figs[i - 1] if i > 0 else None
        yoy = figs[i - 4] if i >= 4 else None
        ttm = figs[max(0, i - 3): i + 1]
        ebitda_ttm = sum(x.operating_income + x.da for x in ttm) * 4.0 / len(ttm)
        r: dict[str, float] = {
            "gross_margin": _safe_div(f.gross_profit, f.revenue),
            "operating_margin": _safe_div(f.operating_income, f.revenue),
            "net_margin": _safe_div(f.net_income, f.revenue),
            "dso": _safe_div(f.ar_net, f.revenue) * f.days,
            "dio": _safe_div(f.inventory, f.cogs) * f.days,
            "dpo": _safe_div(f.ap, f.cogs) * f.days,
            "current_ratio": _clip(_safe_div(f.tca, f.tcl, 999.0)),
            "quick_ratio": _clip(_safe_div(f.cash + f.ar_net, f.tcl, 999.0)),
            "debt_to_ebitda": _clip(_safe_div(f.debt, ebitda_ttm, 99.0) if ebitda_ttm > 0 else (0.0 if f.debt == 0 else 99.0)),
            "interest_coverage": _clip(_safe_div(f.operating_income, f.act["7000"], 999.0)),
            "accrual_ratio_sloan": _safe_div(f.net_income - f.cfo, ((prev.total_assets if prev else f.total_assets) + f.total_assets) / 2),
            "allowance_pct_ar": _safe_div(-f.tb["1110"], f.ar_gross),
            "unbilled_pct_ar": _safe_div(f.tb["1150"], f.ar_net),
            "returns_pct_sales": _safe_div(f.act["4100"], -_sum(f.act, ("4000", "4010", "4020"))),
            "capex_pct_revenue": _safe_div(f.tb["1500"] - f.tb_prev.get("1500", 0) + f.tb["1600"] - f.tb_prev.get("1600", 0), f.revenue),
            "deferred_revenue_to_revenue": _safe_div(-_sum(f.tb, ("2200", "2210")), f.revenue),
            "revenue_growth_qoq": _safe_div(f.revenue, prev.revenue) - 1 if prev and prev.revenue else 0.0,
            "revenue_growth_yoy": _safe_div(f.revenue, yoy.revenue) - 1 if yoy and yoy.revenue else 0.0,
            "ebitda_ttm_cents": float(ebitda_ttm),
        }
        r["ccc"] = r["dso"] + r["dio"] - r["dpo"]
        if prev is not None:
            ar_g = _safe_div(f.ar_net, prev.ar_net, 1.0) - 1
            rev_g = _safe_div(f.revenue, prev.revenue, 1.0) - 1
            r["ar_growth_vs_revenue_growth"] = ar_g - rev_g
            r.update(_beneish(f, prev))
        else:
            r["ar_growth_vs_revenue_growth"] = 0.0
        out[f.period] = {k: round(float(v), 10) for k, v in r.items()}
    return out


def _beneish(f: _PeriodFigures, p: _PeriodFigures) -> dict[str, float]:
    """Beneish (1999) 8-variable indices, period ``t`` vs ``t-1`` (sequential quarters).

    DSRI=(AR_t/S_t)/(AR_t-1/S_t-1); GMI=GM_t-1/GM_t; AQI=(1-(CA_t+PPE_t)/TA_t)/(1-(CA_t-1+PPE_t-1)/TA_t-1);
    SGI=S_t/S_t-1; DEPI=(Dep_t-1/(Dep_t-1+PPE_t-1))/(Dep_t/(Dep_t+PPE_t)); SGAI=(SGA_t/S_t)/(SGA_t-1/S_t-1);
    TATA=(NI_t-CFO_t)/TA_t; LVGI=((CL_t+LTD_t)/TA_t)/((CL_t-1+LTD_t-1)/TA_t-1);
    M = -4.84 + .920 DSRI + .528 GMI + .404 AQI + .892 SGI + .115 DEPI - .172 SGAI + 4.679 TATA - .327 LVGI.
    Undefined ratios (zero denominators) default to the neutral value 1.0 (0.0 for TATA).
    """
    def idx(num: float, den: float) -> float:
        v = _safe_div(num, den, 1.0)
        return _clip(v, 50.0)

    dsri = idx(_safe_div(f.ar_net, f.revenue), _safe_div(p.ar_net, p.revenue))
    gmi = idx(_safe_div(p.gross_profit, p.revenue), _safe_div(f.gross_profit, f.revenue))
    aq_t = 1 - _safe_div(f.tca + f.ppe_net, f.total_assets, 1.0)
    aq_p = 1 - _safe_div(p.tca + p.ppe_net, p.total_assets, 1.0)
    aqi = idx(aq_t, aq_p) if abs(aq_p) > 1e-9 and abs(aq_t) > 1e-9 else 1.0
    sgi = idx(f.revenue, p.revenue)
    dep_t, dep_p = f.act["6300"], p.act["6300"]
    depi = idx(_safe_div(dep_p, dep_p + p.ppe_net), _safe_div(dep_t, dep_t + f.ppe_net)) if dep_t and dep_p else 1.0
    sga_t, sga_p = f.sm + f.ga, p.sm + p.ga
    sgai = idx(_safe_div(sga_t, f.revenue), _safe_div(sga_p, p.revenue))
    tata = _clip(_safe_div(f.net_income - f.cfo, f.total_assets), 50.0)
    lev_t = _safe_div(f.tcl + f.tncl, f.total_assets)
    lev_p = _safe_div(p.tcl + p.tncl, p.total_assets)
    lvgi = idx(lev_t, lev_p)
    c = BENEISH_COEFFICIENTS
    m = (c["intercept"] + c["dsri"] * dsri + c["gmi"] * gmi + c["aqi"] * aqi + c["sgi"] * sgi + c["depi"] * depi
         + c["sgai"] * sgai + c["tata"] * tata + c["lvgi"] * lvgi)
    return {"dsri": dsri, "gmi": gmi, "aqi": aqi, "sgi": sgi, "depi": depi, "sgai": sgai, "tata": tata, "lvgi": lvgi, "m_score": m}


def _all_figures(ledger: Ledger, periods: Sequence[str]) -> list[_PeriodFigures]:
    if not periods:
        return []
    order = ledger.period_order
    prior = [p for p in order if p < periods[0]]
    prev_tb = ledger.trial_balance(prior[-1]) if prior else {c: 0 for c in ledger.accounts}
    figs: list[_PeriodFigures] = []
    for period in periods:
        f = _figures(ledger, period, prev_tb)
        figs.append(f)
        prev_tb = f.tb
    return figs


def build_statements(ledger: Ledger, periods: Sequence[str], chart: Sequence[Account] | None = None) -> FinancialStatements:
    """IS, BS, indirect CFS and ratios for ``periods`` (consecutive quarters, oldest first).

    The balance before ``periods[0]`` is the trial balance of the latest earlier period in the
    ledger (warm-up quarter / opening entry). ``chart`` defaults to the ledger's chart.
    """
    if chart is not None and {a.code for a in chart} != set(ledger.accounts):
        raise ValueError("chart does not match the ledger's chart")
    figs = _all_figures(ledger, periods)
    return FinancialStatements(
        periods=list(periods),
        income=[_income_statement(f) for f in figs],
        balance=[_balance_sheet(f) for f in figs],
        cash_flow=[_cash_flow(f) for f in figs],
        ratios=_ratios(figs),
    )


# --------------------------------------------------------------------------- invariants


class WorldInvariantError(RuntimeError):
    """A generated world violates an accounting invariant. This is a generator bug, not a case."""

    def __init__(self, failed: Sequence[InvariantCheck]) -> None:
        self.failed = list(failed)
        super().__init__("world invariants failed: " + "; ".join(f"{c.name}: {c.detail}" for c in self.failed[:8]))


@dataclass
class SubledgerTie:
    """A sub-ledger <-> GL tie, checked per period.

    ``subledger_cents[period]`` is the sub-ledger total in normal-balance terms (positive).
    The GL side is ``ledger_cents[period]`` when given, else the sum of ``accounts`` (``basis``
    ``"balance"`` = closing balance, ``"activity"`` = period activity), sign-flipped when the
    first account is credit-normal. The check passes when
    ``subledger - ledger == expected_break_cents.get(period, 0)`` within ``tolerance_cents``:
    archetypes that *design* a break declare it here so the check verifies the designed amount.
    """

    name: str
    subledger_cents: dict[str, int]
    accounts: list[str] = field(default_factory=list)
    basis: Literal["balance", "activity"] = "balance"
    ledger_cents: dict[str, int] | None = None
    expected_break_cents: dict[str, int] = field(default_factory=dict)
    tolerance_cents: int = 1
    description: str = ""

    def ledger_side(self, ledger: Ledger, period: str) -> int:
        """GL amount for ``period`` in normal-balance terms."""
        if self.ledger_cents is not None:
            return int(self.ledger_cents.get(period, 0))
        src = ledger.trial_balance(period) if self.basis == "balance" else ledger.activity(period)
        raw = _sum(src, self.accounts)
        if self.accounts and ledger.accounts[self.accounts[0]].normal_balance == "credit":
            raw = -raw
        return raw


def check_invariants(ledger: Ledger, statements: FinancialStatements, subledger_ties: Sequence[SubledgerTie] = ()) -> list[InvariantCheck]:
    """Every ARCHITECTURE.md section 7 invariant, one :class:`InvariantCheck` per check and period."""
    checks: list[InvariantCheck] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(InvariantCheck(name=name, passed=bool(ok), detail=detail))

    # entries
    bad = [e.id for e in ledger.entries if sum(ln.debit_cents for ln in e.lines) != sum(ln.credit_cents for ln in e.lines)]
    add("journal_entries_balanced", not bad, f"{len(ledger.entries)} entries; unbalanced: {bad[:5]}")
    unknown = sorted({ln.account for e in ledger.entries for ln in e.lines if ln.account not in ledger.accounts})
    add("journal_accounts_in_chart", not unknown, f"unknown accounts: {unknown}" if unknown else "all lines on chart accounts")
    nonbs = sorted(set(CFS_MAP) ^ (set(BS_CODES) - {"1000"}))
    add("cfs_mapping_complete", not nonbs, f"unmapped/extra BS accounts: {nonbs}" if nonbs else "every non-cash BS account mapped once")

    periods = statements.periods
    figs = {f.period: f for f in _all_figures(ledger, periods)}
    prev_ending: int | None = None
    prev_re: int | None = None
    order = ledger.period_order
    prior = [p for p in order if p < periods[0]] if periods else []
    if prior:
        f0 = ledger.trial_balance(prior[-1])
        prev_re = -_sum(f0, ("3100", "3200")) - _sum(f0, IS_CODES)
        prev_ending = f0["1000"]
    for i, period in enumerate(periods):
        f = figs[period]
        tb_total = sum(f.tb.values())
        add(f"trial_balance_balanced[{period}]", tb_total == 0, f"sum signed balances = {tb_total}")
        bs = statements.balance[i]
        diff = bs.total_assets_cents - (bs.total_liabilities_cents + bs.total_equity_cents)
        add(f"bs_balances[{period}]", diff == 0,
            f"assets {bs.total_assets_cents} - (liabilities {bs.total_liabilities_cents} + equity {bs.total_equity_cents}) = {diff}")
        inc = statements.income[i]
        ni_ledger = -_sum(f.act, IS_CODES)
        add(f"is_net_income_ties[{period}]", inc.net_income_cents == ni_ledger, f"IS NI {inc.net_income_cents} vs -sum(IS activity) {ni_ledger}")
        if prev_re is not None:
            want = prev_re + inc.net_income_cents - f.dividends
            add(f"retained_earnings_rollforward[{period}]", f.retained_earnings == want,
                f"RE {f.retained_earnings} vs RE_prev {prev_re} + NI {inc.net_income_cents} - div {f.dividends} = {want}")
        prev_re = f.retained_earnings
        cf = statements.cash_flow[i]
        ok_end = cf.ending_cash_cents == f.tb["1000"]
        ok_begin = prev_ending is None or cf.beginning_cash_cents == prev_ending
        ok_net = cf.beginning_cash_cents + cf.net_change_cents == cf.ending_cash_cents
        ok_sum = cf.operating_cents + cf.investing_cents + cf.financing_cents == cf.net_change_cents
        add(f"cfs_ties[{period}]", ok_end and ok_begin and ok_net and ok_sum,
            f"begin {cf.beginning_cash_cents} + net {cf.net_change_cents} = end {cf.ending_cash_cents}; BS cash {f.tb['1000']}; "
            f"prev end {prev_ending}; CFO+CFI+CFF={cf.operating_cents + cf.investing_cents + cf.financing_cents}")
        prev_ending = cf.ending_cash_cents
        add(f"cfs_cfo_double_computation[{period}]", f.cfo == f.cfo_residual,
            f"indirect CFO {f.cfo} vs residual (net change - CFI - CFF) {f.cfo_residual}")
        # inventory roll-forward from journal lines
        inv_codes = set(INVENTORY_CODES) | {"1230"}
        begin = _sum(f.tb_prev, inv_codes)
        purchases = cogs_relief = adjustments = 0
        for e in ledger.entries:
            if e.period != period:
                continue
            touches_cogs = any(ln.account == "5000" and ln.debit_cents for ln in e.lines)
            for ln in e.lines:
                if ln.account not in inv_codes:
                    continue
                if ln.debit_cents and (e.source.startswith("purchas") or e.source == "opening"):
                    purchases += ln.debit_cents
                elif ln.credit_cents and touches_cogs and ln.account in INVENTORY_CODES:
                    cogs_relief += ln.credit_cents
                else:
                    adjustments += ln.debit_cents - ln.credit_cents
        end = _sum(f.tb, inv_codes)
        add(f"inventory_rollforward[{period}]", begin + purchases - cogs_relief + adjustments == end and _sum(f.tb, INVENTORY_CODES) >= 0,
            f"begin {begin} + purchases {purchases} - COGS {cogs_relief} + adj {adjustments} = {begin + purchases - cogs_relief + adjustments}; end {end}")
    for tie in subledger_ties:
        for period in periods:
            if period not in tie.subledger_cents:
                continue
            sub = int(tie.subledger_cents[period])
            gl = tie.ledger_side(ledger, period)
            want = int(tie.expected_break_cents.get(period, 0))
            got = sub - gl
            label = "designed break" if want else "tie"
            add(f"subledger:{tie.name}[{period}]", abs(got - want) <= tie.tolerance_cents,
                f"{label}: sub-ledger {sub} - GL {gl} = {got} (expected {want})")
    return checks


def assert_invariants(ledger: Ledger, statements: FinancialStatements, subledger_ties: Sequence[SubledgerTie] = ()) -> list[InvariantCheck]:
    """Run :func:`check_invariants`; raise :class:`WorldInvariantError` if any check failed."""
    checks = check_invariants(ledger, statements, subledger_ties)
    failed = [c for c in checks if not c.passed]
    if failed:
        raise WorldInvariantError(failed)
    return checks
