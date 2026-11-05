"""``monte_carlo_cash`` - "Runway Under Uncertainty" (archetypes.md section 14).

An asset-light B2B services company (3PL / contract manufacturer / distributor) approved a 13-week
cash plan built on averages: mean days-late, mean weekly billings, stated pipeline probabilities,
payroll and supplier payments on schedule. Every week of the plan is positive. The data says
otherwise: collection delays are right-skewed (Gamma, shape ~2), the key customer (18-30% of
revenue) has paid 20-40 days late in two of the last eight quarters, the key supplier once demanded
prepayment, and payroll falls on the 15th and the last day of each month next to rent and debt
service. A Monte Carlo over those fitted inputs gives a material probability of running out of cash
and of breaching the revolver's minimum-liquidity covenant - exactly when the revolver is needed.

One simulation, three uses
--------------------------
``REF_DEFS`` (module constant) is the reference simulation: ``extract_inputs(con)`` reads everything
from the case DB (deduplicating payments, as the sandbox's learner must) and ``simulate(inp, cash0,
n, seed)`` runs ``n`` paths with ``numpy.random.default_rng(seed)`` in a fixed draw order. It is used
(1) at build time on an in-memory copy of the clean tables to *calibrate* the closing cash so that
``P(min weekly cash < 0) = D[p] in U(0.12, 0.35)``; (2) in ground-truth building, executed verbatim
(``REF_DEFS + REF_MAIN``) against the written DuckDB to produce every expected metric; and (3) as the
learner-facing ``reference_solution.python`` (sandbox names ``con`` / ``tables``). Seed ``SEED`` and
``N_PATHS = 20000`` are fixed, so a learner who runs the reference gets the grader's numbers exactly;
tolerances (abs 0.04 on probabilities, rel 0.10 on cash) cover a learner's own seed/model choices.

Model of the 13 weeks (horizon days 1..91 after the as-of date, week = (day - 1) // 7)
--------------------------------------------------------------------------------------
* Open invoices: collected at ``due + floor(Gamma(k, theta))`` days, ``(k, theta)`` by method of
  moments on days-late of non-key customers' paid invoices. Key customer: with probability
  ``p_tail`` (quarters with a > 20-days-late episode / 8) all its receipts slip by ``tail_extra``
  days (mean lateness in episodes minus normal lateness).
* New billings: weekly ``LN(mu, sigma)`` fitted to the last 26 weekly billing totals; the key share
  is due after the key terms, the rest after the average terms, each plus a Gamma delay.
* Pipeline: open deals closing in the horizon win with ``stated probability x calibration``
  (calibration = realised / stated win rate from ``pipeline_history``, principal only; 1 otherwise),
  invoiced at close on 30-day terms plus a Gamma delay.
* Outflows from the schedules: payroll, fixed costs (rent, insurance, software, tax), debt service,
  supplier POs on their due dates. With probability ``p_prepay`` (prepay requests / 8 quarters) the
  key supplier demands prepayment: its receipts of the next two weeks are paid a week before receipt.
* ``min_cash = cash0 + min_w cumsum(in - out)``; covenant breach = ``min_cash < min_liquidity``.
* Driver ranking (Sobol-lite first-order shares of Var(min_cash)): key-customer tail and supplier
  prepay by group means, billings / collection timing / pipeline by 20 quantile bins.

Books: invoices ``Dr 1100; Cr 4010`` (3PL) or ``4000``; collections; supplier bills ``Dr 5030 /
5000; Cr 2000`` paid on due dates; semi-monthly payroll paid on the 15th and the last day; rent on the
1st; insurance / software; term-debt service at quarter ends; tax paid mid-month after each quarter;
owner distributions at month ends (sized so the as-of cash equals the calibrated ``cash0`` while the
historical balance never goes negative - "we've always been fine"). AR, AP and revenue ties hold;
the flaw is the forecast artifact, not the books.

Simulation (reduced form, effects re-simulated with ``REF_DEFS`` at build time, 2,000 paths):
``early_pay_discount_big_customer_pct``, ``factoring_pct``, ``defer_vendor_days``,
``revolver_predraw_cents`` and ``payroll_timing`` move ``p_shortfall_13w`` / ``p_covenant_breach_13w`` /
``expected_min_cash_cents`` by their simulated finite differences; costs flow to net income.
``resolved_if``: a pre-arranged draw covering the p95 shortfall and >= 1% early-pay discount.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from axiom.engine.archetypes import register
from axiom.engine.archetypes.base import (
    Archetype,
    BuildContext,
    GroundTruthError,
    WorldState,
    difficulty_pick,
    magnitude,
    metric,
    run_reference_query,
)
from axiom.engine.datagen import GL_BATCH, TableBuilder
from axiom.engine.finance import Ledger, SubledgerTie, batch_end_dates, cents, split_cents
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state
from axiom.engine.rng import SeededRNG
from axiom.models import (
    Anomaly,
    Difficulty,
    DossierSeed,
    GroundTruth,
    Lever,
    OperationsState,
    ReferenceSolution,
    Stakeholder,
    TimelineEvent,
)

logger = logging.getLogger("axiom.archetypes.monte_carlo_cash")

SEED = 20_240_917
N_PATHS = 20_000
DRIVERS = ("key_customer_timing", "weekly_billings", "collection_timing", "pipeline", "supplier_prepay")
STORY = {
    "3pl": ("Asset-light 3PL and freight brokerage: bills shippers per load, pays carriers on 30-45 day terms", "4010", "5030",
            "Purchased transportation (carriers)"),
    "manufacturing": ("Contract manufacturer: bills OEM customers per shipment, buys materials on 30-45 day terms", "4000", "5000",
                      "Materials and subcontracting"),
    "ecommerce": ("B2B distributor for e-commerce brands: bills retailers per delivery, pays suppliers on 30-45 day terms", "4000", "5000",
                  "Merchandise (drop-shipped)"),
}

REF_DEFS = '''
import numpy as np
import pandas as pd

SEED = %(seed)d
N_PATHS = %(n_paths)d
H = 13            # weeks
HORIZON_DAYS = 91


def q(con, sql):
    return con.execute(sql).df()


def extract_inputs(con):
    """Every simulation input, fitted from the case database."""
    names = set(q(con, "SELECT table_name FROM information_schema.tables")["table_name"])
    asof = pd.Timestamp(q(con, "SELECT MAX(end_date) AS d FROM period_calendar")["d"].iloc[0]).normalize()

    def day(s):  # horizon day index: 1 = the first day after the as-of date
        return (pd.to_datetime(s).dt.normalize() - asof).dt.days.to_numpy().astype(float)

    inv = q(con, "SELECT invoice_id, customer_id, invoice_date, due_date, amount_cents FROM invoices")
    pay = q(con, """SELECT invoice_id, paid_at FROM payments
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY payment_id ORDER BY ingest_id) = 1""")
    inv = inv.merge(pay, on="invoice_id", how="left")
    inv["invoice_date"] = pd.to_datetime(inv["invoice_date"])
    inv["due_date"] = pd.to_datetime(inv["due_date"])
    ttm = inv[inv["invoice_date"] > asof - pd.Timedelta(days=365)].groupby("customer_id")["amount_cents"].sum()
    key = ttm.idxmax()
    key_share = float(ttm.max() / ttm.sum())
    paid = inv.dropna(subset=["paid_at"])
    late = (pd.to_datetime(paid["paid_at"]).dt.normalize() - paid["due_date"]).dt.days.astype(float).to_numpy()
    is_key = (paid["customer_id"] == key).to_numpy()
    base = late[~is_key]
    k, theta = base.mean() ** 2 / base.var(ddof=1), base.var(ddof=1) / base.mean()
    kl = late[is_key]
    tail = kl > 20
    tail_extra = float(kl[tail].mean() - kl[~tail].mean()) if tail.any() else 0.0
    p_tail = paid.loc[is_key].loc[tail, "due_date"].dt.to_period("Q").nunique() / 8.0
    recent = inv[inv["invoice_date"] > asof - pd.Timedelta(days=182)]
    wk = recent.groupby((asof - recent["invoice_date"]).dt.days // 7)["amount_cents"].sum()
    terms = (inv["due_date"] - inv["invoice_date"]).dt.days
    open_ = inv[inv["paid_at"].isna()]
    deals = q(con, "SELECT expected_close, stated_probability, amount_cents FROM sales_pipeline WHERE LOWER(TRIM(stage)) NOT IN ('won', 'lost')")
    deals = deals[(day(deals["expected_close"]) >= 1) & (day(deals["expected_close"]) <= HORIZON_DAYS)]
    calib = 1.0
    if "pipeline_history" in names:
        ph = q(con, "SELECT stated_probability, won FROM pipeline_history")
        calib = float(ph["won"].astype(float).mean() / ph["stated_probability"].mean())
    out = np.zeros(H)

    def add(days, amounts):
        w = ((days - 1) // 7).astype(int)
        ok = (days >= 1) & (w < H)
        np.add.at(out, w[ok], np.asarray(amounts, dtype=float)[ok])

    pr = q(con, "SELECT pay_date, amount_cents FROM payroll_schedule WHERE status = 'scheduled'")
    add(day(pr["pay_date"]), pr["amount_cents"])
    fc = q(con, "SELECT due_date, amount_cents FROM fixed_cost_schedule WHERE status = 'scheduled'")
    add(day(fc["due_date"]), fc["amount_cents"])
    ds = q(con, "SELECT payment_date, interest_cents + principal_cents AS amount_cents FROM debt_schedule WHERE status = 'scheduled'")
    add(day(ds["payment_date"]), ds["amount_cents"])
    st = q(con, "SELECT supplier_id, is_key_supplier, prepay_requests_24m FROM supplier_terms")
    key_sup = st.loc[st["is_key_supplier"].astype(bool), "supplier_id"].iloc[0]
    p_prepay = float(st.loc[st["is_key_supplier"].astype(bool), "prepay_requests_24m"].iloc[0]) / 8.0
    po = q(con, "SELECT supplier_id, expected_receipt, due_date, amount_cents FROM purchase_orders WHERE status IN ('received_unpaid', 'committed')")
    add(day(po["due_date"]), po["amount_cents"])
    # a prepayment demand covers the key supplier's receipts of the next two weeks (until credit is restored)
    kp = po[(po["supplier_id"] == key_sup) & (day(po["expected_receipt"]) >= 1) & (day(po["expected_receipt"]) <= 14)]
    shift = np.zeros(H)  # extra outflow per week if the key supplier demands prepayment
    for d_new, d_old, a in zip(np.maximum(1, day(kp["expected_receipt"]) - 7), day(kp["due_date"]), kp["amount_cents"]):
        w_new, w_old = int((d_new - 1) // 7), int((d_old - 1) // 7)
        if w_new < H:
            shift[w_new] += a
        if w_old < H:
            shift[w_old] -= a
    cov = float(q(con, "SELECT min_liquidity_cents FROM credit_agreement")["min_liquidity_cents"].iloc[0])
    return {
        "asof": asof, "k": float(k), "theta": float(theta), "key": key, "key_share": key_share,
        "tail_extra": tail_extra, "p_tail": float(p_tail), "mu": float(np.log(wk).mean()), "sigma": float(np.log(wk).std(ddof=1)),
        "weekly_mean": float(wk.mean()), "mean_late_all": float(late.mean()),
        "terms_key": float(terms[inv["customer_id"] == key].mean()), "terms_other": float(terms[inv["customer_id"] != key].mean()),
        "open_due": day(open_["due_date"]), "open_amt": open_["amount_cents"].to_numpy().astype(float),
        "open_key": (open_["customer_id"] == key).to_numpy(),
        "deal_close": day(deals["expected_close"]), "deal_p": deals["stated_probability"].to_numpy().astype(float) * calib,
        "deal_amt": deals["amount_cents"].to_numpy().astype(float), "calib": calib,
        "out": out, "prepay_shift": shift, "p_prepay": p_prepay, "cov_min": cov,
    }


def _bucket_fixed(days, amounts):
    """Weekly sums when every column has one fixed amount (open invoices): one masked mat-vec per week."""
    w = np.floor_divide(days - 1, 7)
    w = np.where((days >= 1) & (w < H), w, -1).astype(np.int8)
    a = np.asarray(amounts, dtype=np.float32)
    out = np.empty((days.shape[0], H))
    for k in range(H):
        out[:, k] = (w == k).astype(np.float32) @ a
    return out


def _bucket(days, amounts, n):
    """Sum ``amounts`` into (path, week) cells by horizon day; days outside 1..91 go to an overflow column."""
    w = np.floor_divide(days - 1, 7)
    w = np.where((days >= 1) & (w < H), w, H).astype(np.int64)
    idx = (np.arange(n, dtype=np.int64)[:, None] * (H + 1) + w).ravel()
    wts = np.broadcast_to(amounts, days.shape).ravel()
    return np.bincount(idx, weights=wts, minlength=n * (H + 1)).reshape(n, H + 1)[:, :H]


def simulate(inp, cash0, n=N_PATHS, seed=SEED):
    """Monte Carlo of weekly ending cash for 13 weeks; draws in a fixed order."""
    rng = np.random.default_rng(seed)
    k, th = inp["k"], inp["theta"]
    tail = rng.random(n) < inp["p_tail"]
    prepay = rng.random(n) < inp["p_prepay"]
    gamma = lambda size: np.floor(rng.standard_gamma(k, size=size, dtype=np.float32) * np.float32(th))  # noqa: E731
    d_open = gamma((n, len(inp["open_amt"])))
    d_new = gamma((n, H, 2))
    sales = rng.lognormal(inp["mu"], inp["sigma"], size=(n, H))
    d_pipe = gamma((n, len(inp["deal_amt"])))
    won = rng.random((n, len(inp["deal_amt"]))) < inp["deal_p"]
    slip = np.round(inp["tail_extra"]) * tail[:, None]
    collection = (d_open @ inp["open_amt"].astype(np.float32)) / max(1.0, inp["open_amt"].sum())  # amount-weighted delay
    days = d_open + inp["open_due"].astype(np.float32)[None, :]
    if inp["open_key"].any() and tail.any():
        days[tail] += np.float32(np.round(inp["tail_extra"])) * inp["open_key"].astype(np.float32)[None, :]
    inflow = _bucket_fixed(days, inp["open_amt"])
    bill_day = 7 * np.arange(H)[None, :] + 4
    inflow += _bucket(bill_day + np.round(inp["terms_other"]) + d_new[:, :, 0], sales * (1 - inp["key_share"]), n)
    inflow += _bucket(bill_day + np.round(inp["terms_key"]) + d_new[:, :, 1] + slip, sales * inp["key_share"], n)
    inflow += _bucket(inp["deal_close"][None, :] + 30 + d_pipe, won * inp["deal_amt"][None, :], n)
    outflow = inp["out"][None, :] + prepay[:, None] * inp["prepay_shift"][None, :]
    cash = cash0 + np.cumsum(inflow - outflow, axis=1)
    return {"cash": cash, "min": cash.min(axis=1), "argmin": cash.argmin(axis=1) + 1, "tail": tail, "prepay": prepay,
            "billings": sales.sum(axis=1), "collection": collection,
            "pipeline": (won * inp["deal_amt"]).sum(axis=1)}


def first_order_shares(sim):
    """Sobol-lite: Var(E[min_cash | X]) / Var(min_cash) per input (binary by group, continuous by 20 quantile bins)."""
    y = pd.Series(sim["min"])
    total = y.var(ddof=0)

    def share(groups):
        m = y.groupby(groups).transform("mean")
        return float(m.var(ddof=0) / total) if total > 0 else 0.0

    def bins(x):
        return pd.qcut(pd.Series(x).rank(method="first"), 20, labels=False)

    return {"key_customer_timing": share(sim["tail"]), "weekly_billings": share(bins(sim["billings"])),
            "collection_timing": share(bins(sim["collection"])), "pipeline": share(bins(sim["pipeline"])),
            "supplier_prepay": share(sim["prepay"])}


def deterministic_plan(inp, cash0):
    """Management's 13-week plan: averages everywhere, no tails."""
    out = np.zeros(H)

    def add(days, amounts):
        w = np.floor((np.asarray(days, dtype=float) - 1) / 7).astype(int)
        ok = (np.asarray(days) >= 1) & (w < H)
        np.add.at(out, w[ok], np.asarray(amounts, dtype=float)[ok])

    lat = round(inp["mean_late_all"])
    add(inp["open_due"] + lat, inp["open_amt"])
    bill_day = 7 * np.arange(H) + 4
    add(bill_day + round(inp["terms_other"]) + lat, np.full(H, inp["weekly_mean"] * (1 - inp["key_share"])))
    add(bill_day + round(inp["terms_key"]) + lat, np.full(H, inp["weekly_mean"] * inp["key_share"]))
    add(inp["deal_close"] + 30 + lat, inp["deal_p"] / max(inp["calib"], 1e-9) * inp["deal_amt"])
    inflow = out.copy()
    return {"inflow": inflow, "outflow": inp["out"].copy(), "ending_cash": cash0 + np.cumsum(inflow - inp["out"])}
'''

REF_MAIN = '''
inp = extract_inputs(con)
asof = inp["asof"]
cash0 = float(q(con, f"SELECT closing_balance_cents FROM cash_ledger WHERE balance_date = DATE '{asof:%Y-%m-%d}'")["closing_balance_cents"].iloc[0])
sim = simulate(inp, cash0)
shares = first_order_shares(sim)
mins = sim["min"]
plan = q(con, "SELECT MIN(ending_cash_cents) AS m FROM management_forecast")["m"].iloc[0]
result = {
    "cash0_cents": cash0,
    "p_shortfall_13w": float((mins < 0).mean()),
    "p_covenant_breach_13w": float((mins < inp["cov_min"]).mean()),
    "expected_min_cash_cents": float(mins.mean()),
    "p5_min_cash_cents": float(np.quantile(mins, 0.05)),
    "worst_week_index": int(np.bincount(sim["argmin"], minlength=H + 1).argmax()),
    "driver_shares": shares,
    "top_driver": max(shares, key=shares.get),
    "collection_delay_gamma_k": inp["k"], "collection_delay_gamma_theta": inp["theta"],
    "pipeline_calibration_factor": inp["calib"],
    "deterministic_forecast_min_cash_cents": float(plan),
    "cash_needed_for_p95_safety_cents": float(max(0.0, -np.quantile(mins, 0.05))),
    "weekly_p10_p50_p90": np.percentile(sim["cash"], [10, 50, 90], axis=0).round().tolist(),
}
print({k: v for k, v in result.items() if k != "weekly_p10_p50_p90"})
'''


def _ref_defs() -> str:
    return REF_DEFS % {"seed": SEED, "n_paths": N_PATHS}


def _load_ref() -> dict[str, Any]:
    ns: dict[str, Any] = {"__name__": "axiom_mc_ref"}
    exec(compile(_ref_defs(), "<monte_carlo_cash.REF_DEFS>", "exec"), ns)  # noqa: S102 - our own constant
    return ns


class _LazyTables(dict):
    """``tables[name]`` like the Python sandbox (loaded on first access)."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        super().__init__()
        self._con = con

    def __getitem__(self, key: str) -> pd.DataFrame:
        if not dict.__contains__(self, key):
            dict.__setitem__(self, key, self._con.execute(f'SELECT * FROM "{key}"').df())
        return dict.__getitem__(self, key)


@register
class MonteCarloCash(Archetype):
    """A 13-week cash plan on averages vs a Monte Carlo over fitted tails and covenant."""

    key = "monte_carlo_cash"
    title = "Runway Under Uncertainty"
    pillars = ("python", "accounting_finance", "operations_strategy")
    industries = ("3pl", "manufacturing", "ecommerce")
    schema_template = "ecommerce_oms"
    summary = ("The CFO's 13-week cash plan is positive every week; the treasurer is nervous about payroll weeks. "
               "Quantify the probability of a shortfall and of a covenant breach from the company's own data.")
    root_cause_terms = ("flaw of averages", "key customer", "monte carlo", "covenant")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 14."""
        ind = industry or "3pl"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        story, rev_acct, cogs_acct, cogs_label = STORY[ind]
        rev_usd = math.exp(rng.uniform(math.log(12e6), math.log(45e6)))
        p: dict[str, Any] = {
            "industry": ind, "business_model": story, "rev_account": rev_acct, "cogs_account": cogs_acct, "cogs_label": cogs_label,
            "revenue_ttm_cents": int(rev_usd * 100),
            "growth_qoq": float(rng.uniform(0.0, 0.04)),
            "billing_cv": float(rng.uniform(0.12, 0.25)),
            # thin-margin services economics: ~15-20% gross margin, ~3-8% operating margin
            "purchased_share": float(rng.uniform(0.64, 0.7)),  # supplier bills / revenue
            "labor_share": float(rng.uniform(0.18, 0.22)),     # payroll / revenue (70% direct labour)
            "fixed_share": float(rng.uniform(0.045, 0.06)),    # rent + other fixed / revenue
            "late_k": float(rng.uniform(1.6, 2.6)),
            "late_theta": float(rng.uniform(4.0, 8.0)),
            "key_share": float(rng.uniform(0.18, 0.30)),
            "key_terms": 45,
            "tail_extra_days": float(rng.uniform(22, 35)),
            "p_shortfall_target": magnitude(rng, difficulty, 0.12, 0.35),
            "covenant_mult": float(rng.uniform(0.5, 1.0)),
            "pipeline_calibration": float(rng.uniform(0.6, 0.7)) if difficulty == "principal" else 1.0,
            "interest_rate": float(rng.uniform(0.08, 0.11)),
            "tax_rate": float(rng.uniform(0.21, 0.26)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        p["n_customers"] = difficulty_pick(difficulty, 60, 140, 240)
        p["n_suppliers"] = difficulty_pick(difficulty, 8, 14, 20)

        # invoice volume: enough history to fit the delay distribution; open invoices drive simulation cost (N x open)
        p["n_invoices"] = difficulty_pick(difficulty, 2600, 5600, 8400)
        p["description"] = f"{story}; ~{p['n_customers']} customers, one of them a quarter of revenue."
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Customers (one key account), invoices, payment behaviour, suppliers, POs and fixed schedules."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        asof = pd.Timestamp(cal.end)
        state.extras["asof"] = asof
        start = pd.Timestamp(cal.start)
        # --- customers: Pareto tail + one key account holding key_share of billings
        C = p["n_customers"]
        w = 1.0 + g.pareto(1.3, C - 1)
        w = w / w.sum() * (1 - p["key_share"])
        w = np.concatenate([[p["key_share"]], w])
        names: list[str] = []
        while len(names) < C:
            nm = rng.company_name("retail" if p["industry"] != "manufacturing" else "manufacturing")
            if nm not in names:
                names.append(nm)
        cust = pd.DataFrame({
            "customer_id": [f"CU{i:04d}" for i in range(1, C + 1)], "name": names,
            "segment": np.where(np.arange(C) == 0, "key_account", np.where(w > np.quantile(w, 0.8), "large", "mid")),
            "payment_terms_days": np.where(np.arange(C) == 0, p["key_terms"], g.choice([30, 30, 45], C)).astype(np.int64),
            "region": np.array(("Northeast", "Southeast", "Midwest", "Texas", "West"))[g.integers(0, 5, C)],
            "credit_limit_cents": np.round(w * p["revenue_ttm_cents"] / 4 / 1e5).astype(np.int64) * 100_000 + 2_500_000,
            "created_at": start - pd.to_timedelta(g.integers(200, 3000, C), unit="D"),
        })
        # --- invoices: count per month ~ trend x season; key account invoices like everyone else (by weight)
        N = p["n_invoices"]
        trend = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3.0) * np.array([1.0 + 0.06 * math.sin(2 * math.pi * (m.month - 3) / 12) for m in months])
        counts = np.floor(N * trend / trend.sum()).astype(int)
        counts[-1] += N - counts.sum()
        midx = np.repeat(np.arange(n_m), counts)
        mstart = pd.to_datetime(pd.Series(months))
        days_in = mstart.dt.days_in_month.to_numpy()
        inv_date = pd.Series(mstart.to_numpy()[midx] + pd.to_timedelta((g.random(N) * days_in[midx]).astype(int), unit="D").to_numpy())
        # business days only
        inv_date = inv_date - pd.to_timedelta(np.maximum(0, inv_date.dt.dayofweek.to_numpy() - 4), unit="D")
        inv_date = inv_date.where(inv_date >= start, start)
        ci = g.choice(C, size=N, p=w / w.sum())
        amt = g.lognormal(0, 0.55, N)
        # scale so TTM billings ~ target revenue; weekly noise comes from counts and amounts
        ttm = (inv_date > asof - pd.Timedelta(days=365)).to_numpy()
        amt = amt / amt[ttm].sum() * p["revenue_ttm_cents"]
        amt = np.maximum(50_000, np.round(amt / 100) * 100).astype(np.int64)
        inv = pd.DataFrame({"_c": ci, "customer_id": cust["customer_id"].to_numpy()[ci], "invoice_date": inv_date.dt.normalize(), "amount_cents": amt})
        inv = inv.sort_values(["invoice_date", "_c"], kind="stable").reset_index(drop=True)
        inv["invoice_id"] = [f"INV{i:06d}" for i in range(1, len(inv) + 1)]
        inv["terms_days"] = cust["payment_terms_days"].to_numpy()[inv["_c"].to_numpy()]
        inv["due_date"] = inv["invoice_date"] + pd.to_timedelta(inv["terms_days"], unit="D")
        late = np.floor(g.gamma(p["late_k"], p["late_theta"], len(inv)))
        # key-account episodes: two quarters (P3 or P4, and P6) where invoices due in one month slip 22-35 days
        ep_periods = [state.p(int(g.integers(3, 5))), state.p(6)]
        episodes = []
        for per in ep_periods:
            s, _e = cal.bounds(per)
            m0 = pd.Timestamp(s) + pd.DateOffset(months=int(g.integers(0, 3)))
            episodes.append((m0, m0 + pd.offsets.MonthEnd(0)))
        is_key = (inv["_c"] == 0).to_numpy()
        in_ep = np.zeros(len(inv), dtype=bool)
        for a, b in episodes:
            in_ep |= ((inv["due_date"] >= a) & (inv["due_date"] <= b)).to_numpy()
        late = late + np.where(is_key & in_ep, np.round(p["tail_extra_days"] + g.normal(0, 2, len(inv))), 0)
        # keep normal lateness below the episode threshold for the key account so the tail is identifiable
        late = np.where(is_key & ~in_ep, np.minimum(late, 20), late)
        paid_at = inv["due_date"] + pd.to_timedelta(late, unit="D") + pd.to_timedelta(g.integers(9 * 3600, 17 * 3600, len(inv)), unit="s")
        inv["_paid_at_true"] = paid_at
        inv["paid_at"] = paid_at.where(paid_at <= asof + pd.Timedelta(hours=23, minutes=59))
        inv["status"] = np.where(inv["paid_at"].notna(), "paid", "open")
        inv["po_number"] = [f"PO-{g.integers(10000, 99999)}" for _ in range(len(inv))]
        state.extras.update({"episodes": episodes, "cust_w": w})
        state.frames.update({"customers": cust, "invoices": inv})
        self._suppliers_and_schedules(ctx, state)

    def _suppliers_and_schedules(self, ctx: BuildContext, state: WorldState) -> None:
        """Suppliers, historical + committed POs, payroll, fixed costs and debt schedules (history and the 13 forward weeks)."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("schedules").generator

        asof = state.extras["asof"]
        horizon_end = asof + pd.Timedelta(days=91)
        start = pd.Timestamp(cal.start)
        inv = state.frames["invoices"]
        S = p["n_suppliers"]
        tail_w = 1 + g.pareto(1.5, S - 1)
        sw = np.concatenate([[0.3], tail_w / tail_w.sum() * 0.7])
        sup = pd.DataFrame({"supplier_id": [f"SP{i:03d}" for i in range(1, S + 1)],
                            "name": [ctx.rng.spawn(f"sup{i}").company_name("3pl" if p["industry"] == "3pl" else "manufacturing") for i in range(S)],
                            "terms_days": np.where(np.arange(S) == 0, 30, g.choice([30, 45], S)).astype(np.int64),
                            "is_key_supplier": np.arange(S) == 0, "prepay_requests_24m": np.where(np.arange(S) == 0, 1, 0).astype(np.int64)})
        # weekly supplier bills: purchased share of the billings run-rate, per supplier
        weeks = pd.date_range(start + pd.Timedelta(days=(4 - start.dayofweek) % 7), horizon_end, freq="7D")
        bill_m = inv.groupby(inv["invoice_date"].dt.to_period("M"))["amount_cents"].sum()
        run = np.array([bill_m.get(pd.Timestamp(wk).to_period("M"), bill_m.iloc[-1]) for wk in weeks]) / 4.33
        rows = []
        for j, wk in enumerate(weeks):
            amt_total = run[j] * p["purchased_share"]
            for s_ in range(S):
                if s_ > 0 and g.random() < 0.35:  # small suppliers bill every other week or so
                    continue
                skip_adj = 1.0 if s_ == 0 else 1 / 0.65  # a bill from a small supplier covers ~1.5 weeks
                a = int(round(amt_total * sw[s_] * skip_adj * float(g.lognormal(0, 0.12)) / 100) * 100)
                rows.append((s_, wk, a))
        po = pd.DataFrame(rows, columns=["_s", "expected_receipt", "amount_cents"])
        po["supplier_id"] = sup["supplier_id"].to_numpy()[po["_s"].to_numpy()]
        po["ordered_at"] = po["expected_receipt"] - pd.to_timedelta(g.integers(7, 21, len(po)), unit="D")
        # receipts inside the 13-week horizon are covered by blanket POs / capacity commitments placed before the as-of date
        fwd = po["expected_receipt"] > asof
        po.loc[fwd, "ordered_at"] = np.minimum(po.loc[fwd, "ordered_at"], asof - pd.to_timedelta(g.integers(0, 10, int(fwd.sum())), unit="D"))
        po["due_date"] = po["expected_receipt"] + pd.to_timedelta(sup["terms_days"].to_numpy()[po["_s"].to_numpy()], unit="D")
        # the one historical prepayment: key supplier, P7
        p7s, p7e = cal.bounds(state.p(7))
        pre = po[(po["_s"] == 0) & (po["expected_receipt"] >= pd.Timestamp(p7s)) & (po["expected_receipt"] <= pd.Timestamp(p7e))].index[:2]
        po["prepaid"] = False
        po.loc[pre, "prepaid"] = True
        po.loc[pre, "due_date"] = po.loc[pre, "expected_receipt"] - pd.Timedelta(days=7)
        po["received_at"] = po["expected_receipt"].where(po["expected_receipt"] <= asof)
        po["paid_at"] = po["due_date"].where(po["due_date"] <= asof)
        po["status"] = np.where(po["paid_at"].notna(), "paid", np.where(po["received_at"].notna(), "received_unpaid", "committed"))
        po = po[po["ordered_at"] <= asof].sort_values(["expected_receipt", "_s"], kind="stable").reset_index(drop=True)
        po.insert(0, "po_id", [f"PO{i:06d}" for i in range(1, len(po) + 1)])
        # payroll: semi-monthly on the 15th and the last day (business-day adjusted backwards)
        pay_rows = []
        m_all = pd.date_range(start, horizon_end, freq="MS")
        for ms in m_all:
            mrev = float(bill_m.get(ms.to_period("M"), bill_m.iloc[-1]))
            amt = int(round(mrev * p["labor_share"] / 2 / 100) * 100)
            for d in (ms + pd.Timedelta(days=14), ms + pd.offsets.MonthEnd(0)):
                d = d - pd.Timedelta(days=max(0, d.dayofweek - 4))
                if d <= horizon_end:
                    pay_rows.append((d, amt))
        payroll = pd.DataFrame(pay_rows, columns=["pay_date", "amount_cents"])
        # fixed costs: rent on the 1st, software on the 5th, insurance quarterly, income tax mid-month after quarter end
        fx = []
        fixed_month = p["fixed_share"] * p["revenue_ttm_cents"] / 12
        for ms in m_all:
            fx.append((ms, "rent", int(round(fixed_month * 0.55 / 100) * 100)))
            fx.append((ms + pd.Timedelta(days=4), "software_and_utilities", int(round(fixed_month * 0.3 / 100) * 100)))
            if ms.month in (1, 4, 7, 10):
                fx.append((ms + pd.Timedelta(days=9), "insurance", int(round(fixed_month * 0.45 / 100) * 100)))
        fixed = pd.DataFrame([r for r in fx if r[0] <= horizon_end], columns=["due_date", "category", "amount_cents"])
        # term debt: quarterly interest + principal on the last day of each quarter
        debt0 = cents(p["revenue_ttm_cents"] * 0.12 / 100_000) * 100_000
        q_ends = pd.date_range(start, horizon_end, freq="QE")
        bal = debt0
        drows = []
        amort = cents(debt0 / 20)
        for qe in q_ends:
            interest = cents(bal * p["interest_rate"] / 4)
            drows.append((qe, interest, amort))
            bal -= amort
        debt = pd.DataFrame(drows, columns=["payment_date", "interest_cents", "principal_cents"])
        state.extras.update({"debt0": debt0, "sup_w": sw, "weeks": weeks})
        state.frames.update({"suppliers": sup, "purchase_orders": po, "payroll_schedule": payroll, "fixed_cost_schedule": fixed, "debt_schedule": debt})

    # ------------------------------------------------------------------ anomaly: the plan built on averages

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Sales pipeline (+ principal calibration history); the anomaly itself is the deterministic plan (built later)."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("anomaly").generator
        asof = state.extras["asof"]
        inv = state.frames["invoices"]
        avg_deal = float(inv["amount_cents"].mean()) * 18
        n_open = difficulty_pick(p["difficulty"], 40, 80, 120)
        close = asof + pd.to_timedelta(g.integers(3, 150, n_open), unit="D")
        stated = np.array([0.3, 0.5, 0.7, 0.9])[g.integers(0, 4, n_open)]
        deals = pd.DataFrame({"deal_id": [f"DL{i:04d}" for i in range(1, n_open + 1)],
                              "account_name": [f"Prospect {i}" for i in range(1, n_open + 1)],
                              "stage": np.where(stated >= 0.7, "negotiation", np.where(stated >= 0.5, "proposal", "qualified")),
                              "stated_probability": stated, "expected_close": close.normalize(),
                              "amount_cents": (np.round(g.lognormal(math.log(avg_deal), 0.6, n_open) / 1e4) * 1e4).astype(np.int64),
                              "owner": np.array(("A. Reyes", "J. Okafor", "M. Lindqvist", "S. Patel"))[g.integers(0, 4, n_open)]})
        state.frames["sales_pipeline"] = deals
        if p["pipeline_calibration"] < 1.0:
            nh = 360
            st = np.array([0.3, 0.5, 0.7, 0.9])[g.integers(0, 4, nh)]
            won = g.random(nh) < st * p["pipeline_calibration"]
            hist = pd.DataFrame({"deal_id": [f"DH{i:04d}" for i in range(1, nh + 1)], "stated_probability": st,
                                 "closed_at": (asof - pd.to_timedelta(g.integers(10, 700, nh), unit="D")).normalize(),
                                 "won": won, "amount_cents": (np.round(g.lognormal(math.log(avg_deal), 0.6, nh) / 1e4) * 1e4).astype(np.int64)})
            state.frames["pipeline_history"] = hist.sort_values(["closed_at", "deal_id"], kind="stable").reset_index(drop=True)
        ep = state.extras["episodes"]
        anomalies = [
            Anomaly(id="A1", kind="flaw_of_averages_cash_plan", pillar="python", title="13-week plan built on averages",
                    mechanism=("Management's plan collects every invoice at the mean days-late, books mean weekly billings and stated pipeline "
                               "probabilities, and ignores the key customer's late-payment tail and the key supplier's prepayment risk; "
                               "payroll on the 15th and month end lands next to rent, insurance, tax and debt service."),
                    periods=[state.periods[-1]], affected_accounts=["1000", "1100", "2000"],
                    affected_tables=["management_forecast", "invoices", "payments", "payroll_schedule", "purchase_orders"],
                    magnitude={"p_shortfall_target": round(p["p_shortfall_target"], 6)}, severity="critical"),
            Anomaly(id="A2", kind="key_customer_tail", pillar="accounting_finance", title="Key customer late-payment episodes",
                    mechanism=(f"The key account ({p['key_share']:.0%} of billings) paid invoices due in {ep[0][0]:%Y-%m} and {ep[1][0]:%Y-%m} "
                               f"~{p['tail_extra_days']:.0f} days later than usual; otherwise it pays within 20 days of due."),
                    periods=sorted({cal.labels([a])[0] for a, _ in ep}), affected_accounts=["1100", "1000"], affected_tables=["invoices", "payments"],
                    magnitude={"key_share": round(p["key_share"], 4), "tail_extra_days": round(p["tail_extra_days"], 3),
                               "late_gamma_k_param": round(p["late_k"], 4), "late_gamma_theta_param": round(p["late_theta"], 4)}, severity="high"),
            Anomaly(id="A3", kind="covenant_liquidity_trap", pillar="accounting_finance", title="Minimum-liquidity covenant blocks the revolver",
                    mechanism="The renewed revolver requires minimum liquidity; a draw is blocked in exactly the weeks the cash dips.",
                    periods=[state.periods[-2]], affected_accounts=["2500", "1000"], affected_tables=["credit_agreement", "cash_ledger"],
                    magnitude={"covenant_mult_of_payroll": round(p["covenant_mult"], 4)}, severity="high"),
        ]
        if p["pipeline_calibration"] < 1.0:
            anomalies.append(Anomaly(
                id="A4", kind="pipeline_optimism", pillar="operations_strategy", title="Pipeline probabilities are optimistic",
                mechanism=f"Deals marked 70% have historically closed at about {0.7 * p['pipeline_calibration']:.0%} (realised / stated ~{p['pipeline_calibration']:.2f}).",
                periods=[state.periods[-1]], affected_accounts=["4010"], affected_tables=["sales_pipeline", "pipeline_history"],
                magnitude={"pipeline_calibration": round(p["pipeline_calibration"], 4)}, severity="medium"))
        return anomalies

    # ------------------------------------------------------------------ calibration + ledger

    def _memory_db(self, state: WorldState, cash_ledger: pd.DataFrame | None = None) -> duckdb.DuckDBPyConnection:
        """In-memory DuckDB with the clean tables the reference simulation reads (calibration before the case DB exists)."""
        con = duckdb.connect()
        tabs = self._table_frames(state)
        tabs["period_calendar"] = state.calendar.calendar_frame()
        if cash_ledger is not None:
            tabs["cash_ledger"] = cash_ledger
        for name, df in tabs.items():
            con.register(f"__{name}", df)
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "__{name}"')
            con.unregister(f"__{name}")
        return con

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Calibrate the as-of cash with the reference simulation, build the plan, then post the books."""
        p = state.params
        # income tax accrued at the as-of date is paid inside the horizon; it does not depend on cash, so a
        # preliminary posting (no distributions) gives the scheduled amount
        L0 = Ledger()
        self._post_books(state, L0, {}, 0)
        state.extras["tax_due_asof"] = -int(L0.closing_balances()["2400"])
        payroll_amt = float(state.frames["payroll_schedule"]["amount_cents"].iloc[-1])
        cov_min = cents(p["covenant_mult"] * payroll_amt / 1e5) * 100_000
        state.extras["cov_min"] = cov_min
        ref = _load_ref()
        con = self._memory_db(state)
        try:
            inp = ref["extract_inputs"](con)
        finally:
            con.close()
        # 5,000 paths suffice to place the closing cash on the target quantile (the graded run uses N_PATHS)
        sim0 = ref["simulate"](inp, 0.0, 5000)
        cash0 = -float(np.quantile(sim0["min"], p["p_shortfall_target"]))
        plan0 = ref["deterministic_plan"](inp, 0.0)
        # the plan must look safe: positive every week (a quarter of a payroll run of headroom at its low point)
        need = 0.25 * payroll_amt - float(plan0["ending_cash"].min())
        if cash0 < need:
            logger.debug("monte_carlo_cash: raising cash0 %.0f -> %.0f so the deterministic plan stays positive", cash0, need)
            cash0 = need
        cash0 = cents(cash0 / 100) * 100
        state.extras.update({"cash0_target": cash0, "inputs_build": inp})
        self._post(ctx, state, cash0)
        plan = ref["deterministic_plan"](inp, float(cash0))
        asof = state.extras["asof"]
        wk_start = asof + pd.to_timedelta(1 + 7 * np.arange(13), unit="D")
        state.frames["management_forecast"] = pd.DataFrame({
            "week": np.arange(1, 14), "week_start": wk_start, "week_end": wk_start + pd.Timedelta(days=6),
            "collections_cents": np.round(plan["inflow"]).astype(np.int64), "disbursements_cents": np.round(plan["outflow"]).astype(np.int64),
            "net_cents": np.round(plan["inflow"] - plan["outflow"]).astype(np.int64),
            "ending_cash_cents": np.round(plan["ending_cash"]).astype(np.int64),
            "assumptions": "mean days-late on every invoice; mean weekly billings; stated pipeline probabilities; vendors on terms",
        })

    def _post_books(self, state: WorldState, L: Ledger, dist: dict[pd.Timestamp, int], cash_open: int) -> None:
        """Post every historical entry into ``L`` (opening cash ``cash_open``, owner distributions ``dist``)."""
        p, cal = state.params, state.calendar
        gran = GL_BATCH[p["difficulty"]]
        asof = state.extras["asof"]
        end_date = cal.end
        inv, po, payroll, fixed, debt = (state.frames[k] for k in ("invoices", "purchase_orders", "payroll_schedule", "fixed_cost_schedule", "debt_schedule"))
        hist = lambda df, col: df[df[col] <= asof]
        R = p["revenue_ttm_cents"]
        debt0 = state.extras["debt0"]
        ppe = cents(R * 0.05)
        accdep = cents(ppe * 0.4)
        months = state.extras["months"]
        debt_cur = cents(debt0 / 5)
        net = cash_open + ppe - accdep - debt0
        cs = cents(0.6 * net) if net > 0 else 1
        L.open_books(cal.opening_date, {"1000": cash_open, "1500": ppe, "1510": -accdep, "2600": -debt_cur, "2610": -(debt0 - debt_cur),
                                        "3000": -cs, "3100": -(net - cs)})
        ev: list[tuple[dt.date, int, Any]] = []
        ib = inv.assign(b=batch_end_dates(inv["invoice_date"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("invoice_id", "size"))
        for b, r in ib.iterrows():
            ev.append((b.date(), 2, lambda d, a=int(r.a), dm={"batch": f"billing:{b.date()}", "rows": str(int(r.n))}: L.sell_on_credit(
                d, a, revenue_account=p["rev_account"], memo="Customer invoices (batch)", dim=dm)))
        pd_ = inv.dropna(subset=["paid_at"])
        pb = pd_.assign(b=batch_end_dates(pd_["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("invoice_id", "size"))
        for b, r in pb.iterrows():
            ev.append((b.date(), 3, lambda d, a=int(r.a), dm={"batch": f"collections:{b.date()}", "rows": str(int(r.n))}: L.collect_cash(d, a, memo="Customer payments (batch)", dim=dm)))
        rec = hist(po.dropna(subset=["received_at"]), "received_at")
        rb = rec.assign(b=batch_end_dates(rec["received_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        for b, r in rb.iterrows():
            ev.append((b.date(), 2, lambda d, a=int(r.a), dm={"batch": f"supplier_bills:{b.date()}", "rows": str(int(r.n))}: L.adjust(
                d, [(p["cogs_account"], a, 0, dm), ("2000", 0, a, dm)], f"{p['cogs_label']} billed (batch)", "purchasing")))
        pp = po.dropna(subset=["paid_at"])
        # prepayments: paid before the bill arrives -> prepaid expense until receipt
        pre = pp[pp["prepaid"]]
        for r in pre.itertuples(index=False):
            ev.append((r.paid_at.date(), 1, lambda d, a=int(r.amount_cents), dm={"po_id": r.po_id}: L.adjust(d, [("1300", a, 0, dm), ("1000", 0, a, dm)], "Supplier prepayment", "payables")))
            ev.append((r.received_at.date(), 2, lambda d, a=int(r.amount_cents), dm={"po_id": r.po_id}: L.adjust(d, [("2000", a, 0, dm), ("1300", 0, a, dm)], "Prepayment applied to bill", "payables")))
        norm = pp[~pp["prepaid"]]
        vb = norm.assign(b=batch_end_dates(norm["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        for b, r in vb.iterrows():
            ev.append((b.date(), 4, lambda d, a=int(r.a), dm={"batch": f"vendor_payments:{b.date()}", "rows": str(int(r.n))}: L.pay_ap(d, a, memo="Supplier payments (batch)", dim=dm)))
        for r in hist(payroll, "pay_date").itertuples(index=False):
            split = dict(zip(("5010", "6000", "6200"), split_cents(int(r.amount_cents), [0.7, 0.12, 0.18]), strict=True))
            ev.append((r.pay_date.date(), 5, lambda d, s=split, a=int(r.amount_cents): (L.payroll(d, s), L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll"))))
        acct = {"rent": "6400", "software_and_utilities": "6200", "insurance": "6200"}
        for r in hist(fixed, "due_date").itertuples(index=False):
            if r.category == "income_tax":
                continue
            ev.append((r.due_date.date(), 5, lambda d, a=int(r.amount_cents), c=acct[r.category], m=r.category: L.adjust(d, [(c, a, 0), ("1000", 0, a)], f"{m.replace('_', ' ').capitalize()} paid", "opex")))
        for r in hist(debt, "payment_date").itertuples(index=False):
            ev.append((r.payment_date.date(), 9, lambda d, i=int(r.interest_cents), pr=int(r.principal_cents): (
                L.accrue_interest(d, i), L.pay_interest(d, i), L.repay(d, pr, debt_account="2600", memo="Term loan amortisation", source="financing"),
                L.reclass(d, pr, "2610", "2600", memo="Reclass current portion of term debt"))))
        tax_due: dict[str, int] = {}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            ev.append((ms + dt.timedelta(days=14), 6, lambda d, a=cents(R * 0.002): L.capex(d, a)))
            ev.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 84))))
            if ms.month % 3 == 0:
                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    amt = max(0, cents(pretax * p["tax_rate"]))
                    L.provision_tax(d, amt)
                    tax_due[str(d)] = amt
                ev.append((me, 11, tax))
            if ms.month in (1, 4, 7, 10) and m > 0:
                ev.append((ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"])))
        for d0, a in dist.items():
            ev.append((d0.date(), 12, lambda d, a=a: L.dividend(d, a, memo="Owner distribution")))
        ev.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in ev:
            if d <= end_date:
                fn(d)

    def _post(self, ctx: BuildContext, state: WorldState, cash0_target: int) -> None:
        """Post the books with owner distributions sized so cash at the as-of date == ``cash0_target`` (cash never negative)."""
        cal = state.calendar
        asof = state.extras["asof"]

        def cash_path(L: Ledger) -> pd.Series:
            rows = [(e.date, ln.debit_cents - ln.credit_cents) for e in L.entries for ln in e.lines if ln.account == "1000"]
            s = pd.DataFrame(rows, columns=["d", "v"]).groupby("d")["v"].sum()
            idx = pd.date_range(pd.Timestamp(cal.opening_date), asof, freq="D")
            s.index = pd.to_datetime(s.index)
            return s.reindex(idx, fill_value=0).cumsum()

        # pass 1: no distributions, opening cash such that the minimum historical balance is one payroll
        L1 = Ledger()
        self._post_books(state, L1, {}, 0)
        path = cash_path(L1)
        floor = float(state.frames["payroll_schedule"]["amount_cents"].iloc[0])
        cash_open = int(cents((floor - path.min()) / 100) * 100)
        excess = int(path.iloc[-1] + cash_open - cash0_target)
        # owner distributions on month ends from P1 to the as-of date (the as-of date always has headroom)
        q_ends = [pd.Timestamp(m) + pd.offsets.MonthEnd(0) for m in state.extras["months"][3:]]
        dist: dict[pd.Timestamp, int] = {}
        if excess > 0:
            bal = path + cash_open
            remaining = excess
            # distribute as early as the later headroom allows (owners sweep cash; balance never below zero afterwards)
            for qe in q_ends:
                head = float((bal[bal.index >= qe]).min())
                amt = int(max(0, min(remaining, math.floor(head))))
                if amt > 0:
                    dist[qe] = amt
                    bal = bal - np.where(bal.index >= qe, amt, 0)
                    remaining -= amt
            if remaining > 0:
                logger.debug("monte_carlo_cash: %s cents of excess cash could not be distributed safely", remaining)
        else:
            cash_open -= excess  # more opening cash so the as-of balance is exactly the target
        state.ledger = Ledger()
        self._post_books(state, state.ledger, dist, cash_open)
        state.extras["distributions"] = dist
        final = cash_path(state.ledger)
        state.extras["cash_path"] = final
        if final.min() < 0:
            raise GroundTruthError("monte_carlo_cash: historical cash went negative")

    # ------------------------------------------------------------------ tables

    def _table_frames(self, state: WorldState) -> dict[str, pd.DataFrame]:
        """Clean frames as written (before dirt); also used for the in-memory calibration DB."""
        p = state.params
        asof = state.extras["asof"]
        inv = state.frames["invoices"]
        invoices = inv[["invoice_id", "customer_id", "invoice_date", "due_date", "terms_days", "amount_cents", "status", "po_number"]].copy()
        pay = inv.dropna(subset=["paid_at"])
        payments = pd.DataFrame({"payment_id": [f"PM{i:06d}" for i in range(1, len(pay) + 1)], "invoice_id": pay["invoice_id"].to_numpy(),
                                 "customer_id": pay["customer_id"].to_numpy(), "paid_at": pay["paid_at"].to_numpy(),
                                 "amount_cents": pay["amount_cents"].to_numpy(), "method": "ach",
                                 "recorded_at": pay["paid_at"].to_numpy() + np.timedelta64(2, "h"), "ingest_id": 1})
        payments = payments.sort_values(["paid_at", "payment_id"], kind="stable").reset_index(drop=True)
        po = state.frames["purchase_orders"]
        pos = po[["po_id", "supplier_id", "ordered_at", "expected_receipt", "received_at", "due_date", "amount_cents", "status", "paid_at"]].copy()
        pay_s = state.frames["payroll_schedule"].copy()
        pay_s["status"] = np.where(pay_s["pay_date"] <= asof, "paid", "scheduled")
        pay_s.insert(0, "payroll_run_id", [f"PR{i:04d}" for i in range(1, len(pay_s) + 1)])
        fx = state.frames["fixed_cost_schedule"].copy()
        fx["status"] = np.where(fx["due_date"] <= asof, "paid", "scheduled")
        debt = state.frames["debt_schedule"].copy()
        debt["status"] = np.where(debt["payment_date"] <= asof, "paid", "scheduled")
        out = {"customers": state.frames["customers"], "invoices": invoices, "payments": payments, "purchase_orders": pos,
               "supplier_terms": state.frames["suppliers"], "payroll_schedule": pay_s, "fixed_cost_schedule": fx, "debt_schedule": debt,
               "sales_pipeline": state.frames["sales_pipeline"]}
        if "pipeline_history" in state.frames:
            out["pipeline_history"] = state.frames["pipeline_history"]
        cov = state.extras.get("cov_min")
        if cov is not None:
            out["credit_agreement"] = pd.DataFrame([{
                "facility": "Revolving credit facility", "lender": "First Regional Bank",
                "limit_cents": int(cents(p["revenue_ttm_cents"] * 0.08 / 1e5) * 1e5), "drawn_cents": 0,
                "min_liquidity_cents": int(cov), "covenant": "Unrestricted cash must stay at or above min_liquidity_cents at all times; "
                                                             "no draw may be made while in breach.",
                "renewed_at": pd.Timestamp(state.calendar.bounds(state.periods[-2])[0]) + pd.Timedelta(days=20),
                "maturity": pd.Timestamp(state.calendar.end) + pd.Timedelta(days=730)}])
        else:
            out["credit_agreement"] = pd.DataFrame([{"min_liquidity_cents": 0}])
        # income tax accrued at the as-of date, paid mid-month after the quarter (inside the horizon)
        due_tax = int(state.extras.get("tax_due_asof", 0))
        if due_tax > 0:
            row = pd.DataFrame([{"due_date": asof + pd.Timedelta(days=15), "category": "income_tax", "amount_cents": due_tax, "status": "scheduled"}])
            out["fixed_cost_schedule"] = pd.concat([fx, row], ignore_index=True)
        return out

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Receivables, payables, schedules, pipeline, cash ledger, management plan + 2/4/6 dirt rules."""
        p, cal = state.params, state.calendar
        tabs = self._table_frames(state)
        path = state.extras["cash_path"]
        daily = path.diff().fillna(path.iloc[0])
        cl = pd.DataFrame({"balance_date": path.index, "opening_balance_cents": (path - daily).astype(np.int64).to_numpy(),
                           "net_flow_cents": daily.astype(np.int64).to_numpy(), "closing_balance_cents": path.astype(np.int64).to_numpy()})
        cl = cl[cl["balance_date"] >= pd.Timestamp(cal.start)].reset_index(drop=True)
        cl["account"] = "operating"
        tabs["cash_ledger"] = cl
        tabs["management_forecast"] = state.frames["management_forecast"]
        dirt_plan = [
            ("customers", "whitespace:name"), ("payments", "dup_keys:0.01"),
            ("customers", "case_mixed:region"), ("invoices", "nulls:po_number:0.05"),
            ("sales_pipeline", "case_mixed:stage"), ("payments", "tz_mixed:recorded_at"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        money = {c: "BIGINT" for c in ("amount_cents", "credit_limit_cents", "interest_cents", "principal_cents", "opening_balance_cents",
                                         "net_flow_cents", "closing_balance_cents", "collections_cents", "disbursements_cents", "net_cents",
                                         "ending_cash_cents", "limit_cents", "drawn_cents", "min_liquidity_cents")}
        dates = {c: "DATE" for c in ("invoice_date", "due_date", "expected_receipt", "ordered_at", "pay_date", "payment_date", "expected_close",
                                      "balance_date", "week_start", "week_end", "closed_at", "renewed_at", "maturity")}
        types = {**money, **dates, "terms_days": "INTEGER", "payment_terms_days": "INTEGER", "prepay_requests_24m": "INTEGER", "week": "INTEGER"}
        desc = {
            "customers": "Customer master (one key account).",
            "invoices": "Customer invoices (AR sub-ledger); open invoices have status 'open'.",
            "payments": "Customer payments, one per invoice (paid in full).",
            "purchase_orders": "Supplier bills: history, received-unpaid and committed future receipts (status).",
            "supplier_terms": "Supplier master with payment terms and prepayment history.",
            "payroll_schedule": "Payroll runs: paid history and the scheduled runs.",
            "fixed_cost_schedule": "Rent, software, insurance and tax payments: paid history and scheduled.",
            "debt_schedule": "Term-loan service: quarterly interest + principal.",
            "sales_pipeline": "CRM opportunities with the sales team's stated probability.",
            "pipeline_history": "Closed opportunities: stated probability at the last forecast vs outcome.",
            "credit_agreement": "Revolver terms including the minimum-liquidity covenant.",
            "cash_ledger": "Daily bank balance of the operating account.",
            "management_forecast": "The approved 13-week cash plan (deterministic).",
        }
        pks = {"customers": ["customer_id"], "invoices": ["invoice_id"], "payments": ["payment_id"], "purchase_orders": ["po_id"],
               "supplier_terms": ["supplier_id"], "payroll_schedule": ["payroll_run_id"], "fixed_cost_schedule": [], "debt_schedule": ["payment_date"],
               "sales_pipeline": ["deal_id"], "pipeline_history": ["deal_id"], "credit_agreement": ["facility"], "cash_ledger": ["balance_date"],
               "management_forecast": ["week"]}
        fks = {"invoices": [("customer_id", "customers", "customer_id")], "payments": [("invoice_id", "invoices", "invoice_id")],
               "purchase_orders": [("supplier_id", "supplier_terms", "supplier_id")]}
        for name, df in tabs.items():
            builder.add_frame(name, df, primary_key=pks.get(name, []), foreign_keys=fks.get(name, []), column_types=types,
                              dirt=dirt.get(name, ()), description=desc.get(name, ""),
                              descriptions={"ingest_id": ">1 marks a re-ingested copy of the same payment"} if name == "payments" else None)

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, AP and revenue tie to the sub-ledgers (the books are right; the plan is the flaw)."""
        cal = state.calendar
        inv, po = state.frames["invoices"], state.frames["purchase_orders"]
        ar, ap, rev = {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            issued = inv["invoice_date"] <= e_ts
            ar[period] = int(inv.loc[issued & (inv["paid_at"].isna() | (inv["paid_at"] > e_ts)), "amount_cents"].sum())
            recd = po["received_at"].notna() & (po["received_at"] <= e_ts) & ~po["prepaid"]
            ap[period] = int(po.loc[recd & (po["paid_at"].isna() | (po["paid_at"] > e_ts)), "amount_cents"].sum())
            rev[period] = int(inv.loc[(inv["invoice_date"] >= pd.Timestamp(s)) & issued, "amount_cents"].sum())
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Open invoices == GL 1100"),
            SubledgerTie("ap_received_unpaid", ap, ["2000"], description="Received, unpaid supplier bills == GL 2000"),
            SubledgerTie("revenue_invoices", rev, [state.params["rev_account"]], basis="activity", description="Invoices issued in period == revenue"),
        ]

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Working-capital KPIs by period and the payment-delay model."""

        st = state.statements
        kpis = {}
        for period in state.periods:
            inc = next(i for i in st.income if i.period == period)  # type: ignore[union-attr]
            r = st.ratios[period]  # type: ignore[union-attr]
            kpis[period] = {"revenue_cents": float(inc.revenue_cents), "net_income_cents": float(inc.net_income_cents), "dso": r["dso"], "dpo": r["dpo"],
                            "cash_cents": float(state.ledger.trial_balance(period)["1000"]),
                            "min_daily_cash_cents": float(state.extras["cash_path"][state.extras["cash_path"].index.to_period("Q").astype(str) == period].min())}
        inp = state.extras["inputs_build"]
        return build_operations_state(
            unit_economics={"gross_margin": st.ratios[state.periods[-1]]["gross_margin"], "arpa": 0.0, "cac": 0.0, "ltv": 0.0,  # type: ignore[union-attr]
                            "payback_months": 0.0, "churn_monthly": 0.0},
            supply_chain={"collections": {"gamma_k": inp["k"], "gamma_theta": inp["theta"], "key_share": inp["key_share"]},
                          "supplier_terms_days": state.frames["suppliers"]["terms_days"].tolist()},
            kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def _run_reference(self, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        ns: dict[str, Any] = {"__name__": "__main__", "con": con, "tables": _LazyTables(con), "np": np, "pd": pd, "duckdb": duckdb, "print": lambda *a, **k: None}
        exec(compile(_ref_defs() + REF_MAIN, "<monte_carlo_cash.reference>", "exec"), ns)  # noqa: S102 - our own constant
        return ns["result"]

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Run the reference simulation on the written DB; metrics are its outputs (seed and N fixed)."""
        p = state.params
        res = self._run_reference(con)
        state.extras["ref_result"] = res

        asof = state.extras["asof"]
        drivers = {d: i for i, d in enumerate(DRIVERS)}
        sh = res["driver_shares"]
        metrics = [
            metric("p_shortfall_13w", res["p_shortfall_13w"], "probability",
                   f"Share of {N_PATHS:,} simulated paths (seed {SEED}) whose weekly ending cash drops below zero in the 13 weeks after {asof:%Y-%m-%d}.",
                   pillar="python", tol_abs=0.04, label="P(cash shortfall within 13 weeks)", hint="Simulate collections, billings, pipeline and outflows week by week."),
            metric("p_covenant_breach_13w", res["p_covenant_breach_13w"], "probability",
                   "Share of paths whose minimum weekly cash falls below credit_agreement.min_liquidity_cents.", pillar="accounting_finance", tol_abs=0.04,
                   label="P(covenant breach within 13 weeks)", hint="Compare each path's minimum cash with the covenant floor."),
            metric("expected_min_cash_cents", res["expected_min_cash_cents"], "cents", "Mean over paths of the minimum weekly ending cash.",
                   pillar="python", tol_rel=0.10, label="Expected minimum cash", hint="Average the per-path minimum weekly cash."),
            metric("p5_min_cash_cents", res["p5_min_cash_cents"], "cents", "5th percentile over paths of the minimum weekly ending cash.",
                   pillar="python", tol_rel=0.10, label="5th-percentile minimum cash", hint="5th percentile of the per-path minimum weekly cash."),
            metric("worst_week_index", res["worst_week_index"], "week", "Most frequent week (1-13) in which a path hits its minimum.",
                   pillar="operations_strategy", tol_abs=1.0, label="Tightest week", hint="In which week does a typical path bottom out?"),
            metric("top_driver_code", float(drivers[res["top_driver"]]), "code",
                   "Input with the largest first-order share of Var(min cash): " + ", ".join(f"{i} {d} ({sh[d]:.0%})" for d, i in drivers.items()) + ".",
                   pillar="operations_strategy", tol_abs=0.0, label="Dominant driver (0 key customer / 1 billings / 2 collection timing / 3 pipeline / 4 supplier prepay)", hint="Which input explains most of the variance of minimum cash?"),
            metric("collection_delay_gamma_k", res["collection_delay_gamma_k"], "shape",
                   "Gamma shape by method of moments (mean^2 / variance) of days paid after due date, non-key customers, deduplicated payments.",
                   pillar="python", tol_rel=0.25, label="Collection delay Gamma k", hint="Fit days paid after due date; the key customer behaves differently."),
            metric("collection_delay_gamma_theta", res["collection_delay_gamma_theta"], "days", "Gamma scale (variance / mean) of the same sample.",
                   pillar="python", tol_rel=0.25, label="Collection delay Gamma theta", hint="Same fit as k."),
            metric("deterministic_forecast_min_cash_cents", res["deterministic_forecast_min_cash_cents"], "cents",
                   "Lowest weekly ending cash in management_forecast (the approved plan).", pillar="accounting_finance", tol_abs=1.0,
                   label="Plan's minimum weekly cash", hint="Read it off the approved plan."),
            metric("cash_needed_for_p95_safety_cents", res["cash_needed_for_p95_safety_cents"], "cents",
                   "Extra liquidity so that P(min cash < 0) <= 5%: max(0, -p5 of minimum weekly cash).", pillar="accounting_finance", tol_rel=0.10,
                   label="Liquidity needed for 95% safety", hint="How much extra cash makes a shortfall a 1-in-20 event?"),
        ]
        if p["pipeline_calibration"] < 1.0:
            metrics.append(metric("pipeline_calibration_factor", res["pipeline_calibration_factor"], "ratio",
                                  "pipeline_history: realised win rate / mean stated probability.", pillar="operations_strategy", tol_abs=0.08,
                                  label="Pipeline calibration factor", hint="Realised win rate versus stated probability on closed deals."))
        sql_delay = """
WITH p AS (SELECT * FROM payments QUALIFY ROW_NUMBER() OVER (PARTITION BY payment_id ORDER BY ingest_id) = 1),
key AS (
  SELECT customer_id FROM invoices WHERE invoice_date > (SELECT MAX(end_date) FROM period_calendar) - INTERVAL 365 DAY
  GROUP BY 1 ORDER BY SUM(amount_cents) DESC LIMIT 1),
d AS (
  SELECT i.customer_id, i.customer_id IN (SELECT customer_id FROM key) AS is_key,
         date_diff('day', i.due_date, CAST(p.paid_at AS DATE)) AS days_late
  FROM p JOIN invoices i USING (invoice_id))
SELECT is_key, COUNT(*) AS paid_invoices, AVG(days_late) AS mean_late, var_samp(days_late) AS var_late,
       AVG(days_late) ^ 2 / var_samp(days_late) AS gamma_k, var_samp(days_late) / AVG(days_late) AS gamma_theta,
       quantile_cont(days_late, 0.5) AS p50, quantile_cont(days_late, 0.95) AS p95, MAX(days_late) AS max_late
FROM d GROUP BY 1 ORDER BY 1"""
        q_delay = run_reference_query(con, "collection_delay_fit_by_segment", sql_delay, signature=["qualify", "with", "join", "group by", "date_diff("],
                                      metric_keys=["collection_delay_gamma_k", "collection_delay_gamma_theta"],
                                      checks=lambda df: {"gamma_k_non_key": float(df.loc[~df["is_key"], "gamma_k"].iloc[0]),
                                                         "mean_late_all": float((df["mean_late"] * df["paid_invoices"]).sum() / df["paid_invoices"].sum())})
        sql_tail = """
WITH p AS (SELECT * FROM payments QUALIFY ROW_NUMBER() OVER (PARTITION BY payment_id ORDER BY ingest_id) = 1),
key AS (
  SELECT customer_id FROM invoices WHERE invoice_date > (SELECT MAX(end_date) FROM period_calendar) - INTERVAL 365 DAY
  GROUP BY 1 ORDER BY SUM(amount_cents) DESC LIMIT 1)
SELECT period_of(i.due_date) AS due_quarter, COUNT(*) AS invoices,
       AVG(date_diff('day', i.due_date, CAST(p.paid_at AS DATE))) AS avg_days_late,
       COUNT(*) FILTER (WHERE date_diff('day', i.due_date, CAST(p.paid_at AS DATE)) > 20) AS paid_20d_late
FROM p JOIN invoices i USING (invoice_id)
WHERE i.customer_id IN (SELECT customer_id FROM key)
GROUP BY 1 ORDER BY 1"""
        q_tail = run_reference_query(con, "key_customer_lateness_by_quarter", sql_tail, signature=["qualify", "with", "join", "group by", "filter("],
                                     metric_keys=["top_driver_code", "p_shortfall_13w"],
                                     checks=lambda df: {"quarters_with_episode": int((df["paid_20d_late"] > 0).sum())})
        sql_week = f"""
WITH sched AS (
  SELECT pay_date AS d, 'payroll' AS kind, amount_cents FROM payroll_schedule WHERE status = 'scheduled'
  UNION ALL SELECT due_date, category, amount_cents FROM fixed_cost_schedule WHERE status = 'scheduled'
  UNION ALL SELECT payment_date, 'debt_service', interest_cents + principal_cents FROM debt_schedule WHERE status = 'scheduled'
  UNION ALL SELECT due_date, 'suppliers', amount_cents FROM purchase_orders WHERE status IN ('received_unpaid', 'committed'))
SELECT (date_diff('day', DATE '{asof:%Y-%m-%d}', d) - 1) // 7 + 1 AS week,
       SUM(amount_cents) AS scheduled_outflows_cents,
       SUM(amount_cents) FILTER (WHERE kind = 'payroll') AS payroll_cents,
       COUNT(*) FILTER (WHERE kind IN ('rent', 'insurance', 'income_tax', 'debt_service')) AS lumpy_items
FROM sched
WHERE d > DATE '{asof:%Y-%m-%d}' AND d <= DATE '{asof:%Y-%m-%d}' + INTERVAL 91 DAY
GROUP BY 1 ORDER BY 1"""
        q_week = run_reference_query(con, "scheduled_outflows_by_week", sql_week, signature=["with", "union", "group by", "filter(", "sum("],
                                     metric_keys=["worst_week_index"], checks=lambda df: {"max_outflow_week": int(df.loc[df["scheduled_outflows_cents"].idxmax(), "week"])})
        sql_plan = """
SELECT week, week_start, collections_cents, disbursements_cents, ending_cash_cents,
       MIN(ending_cash_cents) OVER () AS plan_min_cents,
       (SELECT min_liquidity_cents FROM credit_agreement) AS covenant_min_cents
FROM management_forecast ORDER BY week"""
        q_plan = run_reference_query(con, "management_plan_vs_covenant", sql_plan, signature=["over("],
                                     metric_keys=["deterministic_forecast_min_cash_cents"],
                                     checks=lambda df: {"plan_min_cents": float(df["ending_cash_cents"].min()),
                                                        "all_weeks_positive": bool((df["ending_cash_cents"] > 0).all())})
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_plan, q_week],
            "magnitude": {**anomalies["A1"].magnitude, "p_shortfall_13w": round(res["p_shortfall_13w"], 6),
                          "deterministic_forecast_min_cash_cents": float(res["deterministic_forecast_min_cash_cents"])},
            "evidence_trail": ["The approved plan is positive every week.",
                               f"Simulated with fitted inputs, P(shortfall) = {res['p_shortfall_13w']:.0%}; tightest week {res['worst_week_index']}."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_delay, q_tail],
            "magnitude": {**anomalies["A2"].magnitude, "collection_delay_gamma_k": round(res["collection_delay_gamma_k"], 6),
                          "key_customer_variance_share": round(sh["key_customer_timing"], 6)},
            "evidence_trail": ["Days paid after due date are right-skewed (Gamma).",
                               "The key account had two quarters with invoices paid > 20 days late."]})
        sql_cov = f"""
SELECT c.min_liquidity_cents, c.limit_cents,
       (SELECT closing_balance_cents FROM cash_ledger WHERE balance_date = DATE '{asof:%Y-%m-%d}') AS cash_today_cents,
       (SELECT MIN(closing_balance_cents) FROM cash_ledger WHERE balance_date > DATE '{asof:%Y-%m-%d}' - INTERVAL 90 DAY) AS min_cash_last_90d_cents
FROM credit_agreement c"""
        q_cov = run_reference_query(con, "covenant_headroom_today", sql_cov, signature=["min("], metric_keys=["p_covenant_breach_13w"],
                                    checks=lambda df: {"headroom_cents": float(df["cash_today_cents"].iloc[0] - df["min_liquidity_cents"].iloc[0])})
        anomalies["A3"] = anomalies["A3"].model_copy(update={
            "reference_sql": [q_cov, q_plan],
            "magnitude": {**anomalies["A3"].magnitude, "p_covenant_breach_13w": round(res["p_covenant_breach_13w"], 6)},
            "evidence_trail": ["credit_agreement: no draw while below min_liquidity_cents.",
                               f"P(covenant breach) = {res['p_covenant_breach_13w']:.0%} - the revolver is unavailable in those paths."]})
        if "A4" in anomalies:
            sql_cal = """
SELECT stated_probability, COUNT(*) AS deals, AVG(CASE WHEN won THEN 1.0 ELSE 0.0 END) AS realised_win_rate,
       AVG(CASE WHEN won THEN 1.0 ELSE 0.0 END) / stated_probability AS calibration
FROM pipeline_history GROUP BY 1 ORDER BY 1"""
            q_cal = run_reference_query(con, "pipeline_calibration_by_bucket", sql_cal, signature=["group by", "case when", "avg("],
                                        metric_keys=["pipeline_calibration_factor"],
                                        checks=lambda df: {"overall": float((df["realised_win_rate"] * df["deals"]).sum() / (df["stated_probability"] * df["deals"]).sum())})
            sql_open = """
SELECT LOWER(TRIM(stage)) AS stage, COUNT(*) AS deals, SUM(amount_cents) AS amount_cents,
       SUM(amount_cents * stated_probability) AS weighted_cents
FROM sales_pipeline GROUP BY 1 ORDER BY 1"""
            q_open = run_reference_query(con, "open_pipeline_by_stage", sql_open, signature=["group by", "sum(", "count("], metric_keys=["pipeline_calibration_factor"])
            anomalies["A4"] = anomalies["A4"].model_copy(update={"reference_sql": [q_cal, q_open],
                                                                 "magnitude": {**anomalies["A4"].magnitude, "pipeline_calibration_factor": round(res["pipeline_calibration_factor"], 6)},
                                                                 "evidence_trail": ["Closed deals marked 70% won far less often."]})
        herrings_all = [
            "Average DSO is fine, so we're fine - the mean is not the quantile that kills you; the key account's tail drives the risk.",
            "The revolver covers it - the minimum-liquidity covenant blocks a draw in exactly the paths where cash dips.",
            "The payroll date change caused the problem - it moved little; the alignment of payroll, rent and debt service with the key account's receipts did.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        top = res["top_driver"].replace("_", " ")
        root = (f"The approved 13-week plan applies the flaw of averages: every invoice collected at the mean days-late, mean weekly billings, "
                f"stated pipeline probabilities, vendors on schedule. Its lowest week is {res['deterministic_forecast_min_cash_cents'] / 100:,.0f} USD. "
                f"Fitting the company's own data - Gamma collection delays (k {res['collection_delay_gamma_k']:.2f}, theta {res['collection_delay_gamma_theta']:.1f}), "
                f"the key customer's late-payment episodes, lognormal weekly billings, the key supplier's prepayment risk"
                + (f", pipeline calibration {res['pipeline_calibration_factor']:.2f}" if p["pipeline_calibration"] < 1.0 else "")
                + f" - a Monte Carlo puts P(cash < 0 within 13 weeks) at {res['p_shortfall_13w']:.0%} and P(covenant breach) at {res['p_covenant_breach_13w']:.0%}, "
                f"tightest in week {res['worst_week_index']}; the dominant driver is {top}. The covenant disables the revolver in those paths, so liquidity "
                f"of ~{res['cash_needed_for_p95_safety_cents'] / 100:,.0f} USD must be pre-arranged (pre-draw, factoring, early-pay discount for the key customer).")
        return GroundTruth(
            root_cause=root, anomalies=[anomalies[k] for k in ("A1", "A2", "A3", "A4") if k in anomalies], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q_delay.sql, q_tail.sql, q_week.sql, q_plan.sql],
                python=["# Reference Monte Carlo (sandbox: `con` and `tables` are provided). Seed and path count are fixed so the\n"
                        "# result reproduces the graded numbers exactly; tolerances (abs 0.04 / rel 0.10) cover your own modelling choices.\n"
                        + _ref_defs() + REF_MAIN],
                memo_bullets=[
                    "The plan is positive every week only because it plans on averages; the risk lives in the tails.",
                    f"P(shortfall within 13 weeks) = {res['p_shortfall_13w']:.0%}; P(covenant breach) = {res['p_covenant_breach_13w']:.0%}; week {res['worst_week_index']} is the tightest.",
                    f"Dominant driver: {top}. Act on the key customer first (early-pay discount, collection plan).",
                    "The revolver is not a buffer when the covenant blocks it: pre-draw or arrange factoring now.",
                    f"Liquidity needed for 95% safety: ~{res['cash_needed_for_p95_safety_cents'] / 100:,.0f} USD; compare with the cost of each lever.",
                    "More paths do not fix model error - the input model (key-customer tail) matters more than sample size.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, stakeholders, spec timeline (payroll-date change flagged)."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        names = rng.pick_names(4)
        plan = state.frames["management_forecast"]
        ep = state.extras["episodes"]
        reveal = [
            f"The approved 13-week plan never drops below {plan['ending_cash_cents'].min() / 100:,.0f} USD.",
            "CFO: 'We've always been fine - cash has never gone negative.'",
            f"The key account is about {p['key_share']:.0%} of billings.",
            "A revolving credit facility was renewed last quarter.",
            f"Open pipeline: {len(state.frames['sales_pipeline'])} deals with stated probabilities.",
        ]
        conceal = [
            "the flaw of averages in the plan: mean days-late on every invoice and no tails",
            "collection-delay distribution parameters (Gamma fit)", "the key customer late-payment tail probability",
            "monte carlo P(shortfall), P(covenant breach) and the driver ranking",
            "the covenant blocks the revolver draw in the stressed paths", "A1", "A2", "A3",
        ]
        if p["pipeline_calibration"] < 1.0:
            conceal += ["pipeline calibration factor (stated vs realised win rates)", "A4"]
        stakeholders = [
            Stakeholder(name=names[0], role="CFO", agenda="Defend the approved plan in front of the bank.", disposition="confident",
                        knows=["the plan uses average DSO"], hides=["has not modelled the key account paying late"]),
            Stakeholder(name=names[1], role="Treasurer", agenda="Get a liquidity buffer before the tight weeks.", disposition="nervous",
                        knows=["payroll weeks are tight", "the key account paid late twice"], hides=[]),
            Stakeholder(name=names[2], role="Head of Sales", agenda="Hit the pipeline number.", disposition="optimistic",
                        knows=["pipeline probabilities"], hides=["probabilities are rarely revisited"]),
            Stakeholder(name=names[3], role="Bank Relationship Manager", agenda="Covenant compliance.", disposition="formal",
                        knows=["minimum-liquidity covenant terms"], hides=[]),
        ]
        tl = [
            TimelineEvent(period=cal.labels([ep[1][0]])[0], headline="Key account paid ~4 weeks late", detail="Collections escalated to the CFO."),
            TimelineEvent(period=P[6], headline="Key supplier asked for prepayment", detail="Two bills paid a week before delivery."),
            TimelineEvent(period=P[6], headline="Revolver renewed with a liquidity covenant", detail="Minimum liquidity tested continuously."),
            TimelineEvent(period=P[7], headline="13-week plan approved", detail="Positive every week."),
            TimelineEvent(period=P[7], headline="Payroll dates moved to the 15th / month end", detail="Minor admin change.", is_red_herring=True),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[5], headline="DSO benchmark review", detail="Average DSO in line with peers.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["13_week_cash_plan", "covenant_excerpt", "pipeline_report", "ar_aging"], architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Liquidity levers of the spec (defaults = status quo)."""
        res = state.extras["ref_result"]
        limit = float(cents(state.params["revenue_ttm_cents"] * 0.08 / 1e5) * 1e5)
        return [
            Lever(key="early_pay_discount_big_customer_pct", label="Early-pay discount for the key customer", min=0, max=3, default=0, unit="%",
                  effect="Key-customer receipts ~10 days earlier at 2%; tail removed; costs the discount."),
            Lever(key="factoring_pct", label="Factor open receivables", min=0, max=50, default=0, unit="% of AR", effect="Cash now at a 3% fee."),
            Lever(key="defer_vendor_days", label="Defer supplier payments", min=0, max=30, default=0, unit="days", effect="Outflows later; supplier risk."),
            Lever(key="revolver_predraw_cents", label="Pre-draw on the revolver", min=0, max=max(limit, 1.25 * res["cash_needed_for_p95_safety_cents"]), default=0,
                  unit="cents", effect="Cash now, interest cost; avoids the covenant trap."),
            Lever(key="payroll_timing", label="Payroll timing (0 semi-monthly / 1 monthly)", min=0, max=1, default=0, unit="code",
                  effect="One payroll at month end: fewer but deeper dips."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Finite-difference lever effects from re-running the reference simulation (2,000 paths, same seed)."""
        p = state.params
        res = state.extras["ref_result"]
        ref = _load_ref()
        inp = dict(state.extras["inputs_build"])
        cash0 = float(res["cash0_cents"])
        n = 2000

        def stats(i: dict[str, Any], c0: float) -> tuple[float, float, float]:
            s = ref["simulate"](i, c0, n)
            return float((s["min"] < 0).mean()), float((s["min"] < i["cov_min"]).mean()), float(s["min"].mean())

        base = stats(inp, cash0)
        # early-pay discount 2%: key receipts 10 days earlier and no tail
        epd = dict(inp, open_due=inp["open_due"] - 10 * inp["open_key"], terms_key=inp["terms_key"] - 10, p_tail=0.0)
        s_epd = stats(epd, cash0)
        ar = float(inp["open_amt"].sum())
        fac = dict(inp, open_amt=inp["open_amt"] * 0.75)
        s_fac = stats(fac, cash0 + 0.25 * ar * 0.97)
        s_def = stats(dict(inp, out=np.concatenate([np.zeros(2), inp["out"][:-2]]) + np.concatenate([np.zeros(11), inp["out"][-2:].sum() * np.array([0.5, 0.5])])), cash0)
        need = max(1.0, res["cash_needed_for_p95_safety_cents"])
        s_rev = stats(inp, cash0 + need)
        pay = state.frames["payroll_schedule"]
        mono = inp["out"].copy()
        asof = state.extras["asof"]
        sched = pay[pay["pay_date"] > asof]
        for d, a in zip(sched["pay_date"], sched["amount_cents"], strict=True):
            if d.day < 20:  # the 15th run moves to month end
                w_old = int(((d - asof).days - 1) // 7)
                w_new = int((((d + pd.offsets.MonthEnd(0)) - asof).days - 1) // 7)
                if w_old < 13:
                    mono[w_old] -= a
                if w_new < 13:
                    mono[w_new] += a
        s_pay = stats(dict(inp, out=mono), cash0)
        key_q_rev = inp["weekly_mean"] * 13 * inp["key_share"]
        kpis = {"p_shortfall_13w": res["p_shortfall_13w"], "p_covenant_breach_13w": res["p_covenant_breach_13w"],
                "expected_min_cash_cents": res["expected_min_cash_cents"], "cash_cents": cash0, "financing_cost_cents": 0.0,
                "net_income_cents": float(next(i for i in state.statements.income if i.period == state.periods[-1]).net_income_cents)}  # type: ignore[union-attr]
        eff: list[dict[str, Any]] = []

        def add(lever: str, per_unit: float, s: tuple[float, float, float], cost: float, cash: float = 0.0) -> None:
            for kpi, i in (("p_shortfall_13w", 0), ("p_covenant_breach_13w", 1), ("expected_min_cash_cents", 2)):
                eff.append({"lever": lever, "kpi": kpi, "delta_per_unit": (s[i] - base[i]) / per_unit, "lag_periods": 0})
            if cost:
                eff.append({"lever": lever, "kpi": "financing_cost_cents", "delta_per_unit": cost / per_unit, "lag_periods": 0})
                eff.append({"lever": lever, "kpi": "net_income_cents", "delta_per_unit": -cost * (1 - p["tax_rate"]) / per_unit, "lag_periods": 0})
            if cash:
                eff.append({"lever": lever, "kpi": "cash_cents", "delta_per_unit": cash / per_unit, "lag_periods": 0})

        add("early_pay_discount_big_customer_pct", 2.0, s_epd, 0.02 * key_q_rev)
        add("factoring_pct", 25.0, s_fac, 0.25 * ar * 0.03, 0.25 * ar * 0.97)
        add("defer_vendor_days", 15.0, s_def, 0.0)
        add("revolver_predraw_cents", need, s_rev, need * p["interest_rate"] / 4, need)
        add("payroll_timing", 1.0, s_pay, 0.0)
        drag = {"p_shortfall_13w": 0.02, "p_covenant_breach_13w": 0.02}
        bounds = {"p_shortfall_13w": [0.0, 1.0], "p_covenant_breach_13w": [0.0, 1.0]}
        return {"kpis": kpis, "lever_effects": eff, "anomaly_drag": drag, "bounds": bounds,
                "resolved_if": {"revolver_predraw_cents": float(round(need)), "early_pay_discount_big_customer_pct": 1.0}}
