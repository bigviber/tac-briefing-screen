"""``forensic_revenue`` - "Quarter-End Magic" (archetypes.md section 4).

A B2B goods company (a manufacturer, or the wholesale channel of an e-commerce brand) sells to
8-25 wholesale distributors on net-45 terms (62-72% of revenue) and to a minority of small direct
accounts paid by card at checkout, so baseline DSO is ~35 days (review P1 #16).
Management raised guidance; organic growth is 2-5% a quarter. From the last quarters revenue is
pulled forward with one of the forensic_patterns.json mechanisms, so the "record quarter" is
partly borrowed from the next one.

Mechanics (ordinary journal families; every invariant holds)
------------------------------------------------------------
* Books start two warm-up quarters before ``P1`` (``P-1``, ``P0``) so that trailing-twelve-month
  Beneish indices exist for a clean comparison quarter (``P6`` vs ``P2``) as well as for ``P7``/``P8``.
* Revenue is recognised at shipment: ``invoice_date = ship date`` (``sale_credit`` Dr 1100 / Cr 4000),
  ``cogs_relief`` at shipment, freight accrued (5030 / 2100). Direct invoices are paid the same day by
  card (2.5% fee to 5050); wholesale invoices on net-45 plus a payment delay.
* Intra-quarter timing: invoice dates are drawn with a weight on the last 7 calendar days (5 business
  days) of every quarter so their baseline share is ``U(0.10, 0.14)`` of quarterly revenue.
* Returns: ``return_rate_base`` of orders return 20-40 days after shipment (a share of the order);
  a credit memo (Dr 4100 / Cr 1100) if the invoice is unpaid, else a cash refund (Dr 4100 / Cr 1000);
  goods are restocked at cost (Dr 1220 / Cr 5000). No returns reserve is carried (documented
  simplification), so stuffed returns hit the following quarter.
* Purchasing replenishes the month's cost of sales from 6-12 suppliers (Dr 1220 / Cr 2000), paid on
  supplier terms. Monthly close: payroll, opex accrual, depreciation, allowance, interest, tax,
  term-debt amortisation, revolver draws/repayments.

Anomalies (mechanism is a seeded choice; principal mixes two plus round-tripping)
----------------------------------------------------------------------------------
* ``channel_stuffing`` (P7, P8): extra wholesale orders worth ``stuffing_pct`` x baseline quarterly
  revenue in the last 7 days of the quarter for the top 3-5 distributors, +5-12pt "quarter-end
  promotion" discount, terms extended by 45-90 days. Window orders return at 2.5-4x the base rate
  20-40 days later (P7's returns land in P8) and the same distributors' regular P8 orders drop by
  ``U(0.5, 0.8)`` x the P7 stuffed amount.
* ``bill_and_hold`` (P8): extra orders invoiced in the last 10 days of P8 whose shipment row is
  ``status='awaiting_release'`` with ``shipped_at`` NULL; ``orders.notes`` carry hold remarks.
  Revenue is booked, COGS is not (GM spike); goods stay in inventory.
* ``premature_cutoff`` (P8): extra orders invoiced in the last 3 days of P8 that ship in the first
  5 days of P9 (the data extract was taken at close, so those shipment rows exist).
* ``round_tripping`` (principal, P5-P8): a "distributor" and a supplier share a tax id / address
  (dirt: case and whitespace); each month the company buys from the supplier and sells the same
  goods to the customer at ~1% margin, ``U(1, 2)%`` of revenue.
* Slow pay (all mechanisms): the distributors that took pulled-forward orders sit on extra stock (or on
  goods they never received) and pay their regular invoices due from P7 on ``slow_pay_days`` (15-25) late,
  so DSO climbs from ~35 to ~55 days by P8; the DSRI and the Beneish M-score of the anomalous quarters
  clear their thresholds while the clean comparison quarter stays below -1.78.
* ``understated_allowance`` (all difficulties): from the first anomalous quarter the allowance for
  doubtful accounts is held flat in dollars while receivables balloon (secondary signal).
* Designed sub-ledger break: ``revenue_vs_shipments`` - invoices recognised in a period whose goods
  had not shipped by period end; declared with the exact amount (zero for pure channel stuffing).

Documented simplifications
* Industries: ``manufacturing`` and ``ecommerce`` (wholesale channel); the SaaS variant of the spec
  is not implemented. Schema: ``ecommerce_oms`` extended with ``invoices``, ``payments``,
  ``suppliers.tax_id/address`` and ``customers.tax_id/address``.
* The Beneish M-score is computed on trailing-twelve-month flows against the same window a year
  earlier (``t`` vs ``t-4``), stocks at period end; the sequential-quarter ``m_score`` in
  ``statements.ratios`` is a different (quarter-on-quarter) reading.
* Base return rate ``U(2, 5)%`` (B2B goods), not the e-commerce DTC benchmark.
* Quarter simulation (reduced form): KPIs are P8 levels; ``continue_practice`` makes the next
  quarter need ``1.3x`` the pull-forward (escalation) and it collapses when distributor inventory
  hits its cap; ``restate`` removes the pull-forward from reported revenue and books a litigation
  reserve of ``U(1, 3)%`` of revenue; ``stop_quarter_end_promotions`` gives back the pull-forward
  plus the distributor over-inventory and normalises DSO over two quarters. ``resolved_if``:
  ``restate >= 1`` and ``stop_quarter_end_promotions >= 1``.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from axiom.engine import benchmark_keys as bk
from axiom.engine.archetypes import register
from axiom.engine.archetypes.base import (
    cap_first,
    Archetype,
    BuildContext,
    WorldState,
    difficulty_pick,
    magnitude,
    metric,
    run_reference_query,
)
from axiom.engine.datagen import GL_BATCH, ROW_BUDGET, TableBuilder
from axiom.engine.finance import (
    BENEISH_COEFFICIENTS,
    FiscalCalendar,
    Ledger,
    SubledgerTie,
    batch_end_dates,
    build_statements,
    cents,
    split_cents,
)
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state, station_network
from axiom.engine.rng import SeededRNG
from axiom.models import (
    Anomaly,
    Difficulty,
    DossierSeed,
    FinancialStatements,
    GroundTruth,
    Lever,
    OperationsState,
    ReferenceSolution,
    Stakeholder,
    TimelineEvent,
)

logger = logging.getLogger("axiom.archetypes.forensic_revenue")

DAY = pd.Timedelta(days=1)
EOD = pd.Timedelta(hours=23, minutes=59, seconds=59)
WINDOW_DAYS = 7  # last 7 calendar days = last 5 business days
CARD_FEE = 0.025
REGIONS = ("Northeast", "Southeast", "Midwest", "Southwest", "West", "Canada")
STREETS = ("Harbor Way", "Industrial Pkwy", "Commerce Dr", "Mill Rd", "Union St", "Depot Ave", "Gateway Blvd", "Foundry Ln")
CATEGORIES = {"manufacturing": ("Valves", "Fittings", "Pumps", "Controls", "Gaskets", "Actuators"),
              "ecommerce": ("Outdoor", "Kitchen", "Home", "Fitness", "Pet", "Garden")}
PAYROLL_SPLIT = {"6000": 0.40, "6100": 0.15, "6200": 0.45}
OPEX_SPLIT = {"6000": 0.40, "6200": 0.30, "6400": 0.30}
MECH_LABEL = {"channel_stuffing": "channel stuffing", "bill_and_hold": "bill-and-hold", "premature_cutoff": "premature cutoff"}
MECH_KIND = {"channel_stuffing": "channel_stuffing", "bill_and_hold": "bill_and_hold", "premature_cutoff": "premature_revenue_recognition"}


def _month_index(ts: pd.Series, start: dt.date) -> np.ndarray:
    t = pd.to_datetime(ts)
    return ((t.dt.year - start.year) * 12 + (t.dt.month - start.month)).to_numpy()


def _beneish(cur: dict[str, float], prev: dict[str, float]) -> dict[str, float]:
    """Beneish (1999) 8-variable model on TTM flows (``t`` vs ``t-4``); neutral 1.0 on zero denominators."""
    def r(a: float, b: float) -> float:
        return a / b if b else 0.0

    def idx(a: float, b: float) -> float:
        return a / b if b and a else 1.0

    dsri = idx(r(cur["ar"], cur["sales"]), r(prev["ar"], prev["sales"]))
    gmi = idx(r(prev["gp"], prev["sales"]), r(cur["gp"], cur["sales"]))
    aq_c = 1 - r(cur["ca"] + cur["ppe"], cur["ta"])
    aq_p = 1 - r(prev["ca"] + prev["ppe"], prev["ta"])
    aqi = aq_c / aq_p if abs(aq_p) > 1e-9 and abs(aq_c) > 1e-9 else 1.0
    sgi = idx(cur["sales"], prev["sales"])
    depi = idx(r(prev["dep"], prev["dep"] + prev["ppe"]), r(cur["dep"], cur["dep"] + cur["ppe"]))
    sgai = idx(r(cur["sga"], cur["sales"]), r(prev["sga"], prev["sales"]))
    tata = r(cur["ni"] - cur["cfo"], cur["ta"])
    lvgi = idx(r(cur["liab"], cur["ta"]), r(prev["liab"], prev["ta"]))
    c = BENEISH_COEFFICIENTS
    m = (c["intercept"] + c["dsri"] * dsri + c["gmi"] * gmi + c["aqi"] * aqi + c["sgi"] * sgi + c["depi"] * depi
         + c["sgai"] * sgai + c["tata"] * tata + c["lvgi"] * lvgi)
    return {"dsri": dsri, "gmi": gmi, "aqi": aqi, "sgi": sgi, "depi": depi, "sgai": sgai, "tata": tata, "lvgi": lvgi, "m_score": m}


@register
class ForensicRevenue(Archetype):
    """Quarter-End Magic: revenue pulled forward at quarter end, a record quarter that reverses."""

    key = "forensic_revenue"
    title = "Quarter-End Magic"
    pillars = ("accounting_finance", "sql", "python")
    industries = ("manufacturing", "ecommerce")
    schema_template = "ecommerce_oms"
    summary = ("A wholesale-heavy goods company reports a record quarter right after raising guidance; receivables "
               "grow faster than sales and the audit committee wants to know whether the revenue is real.")
    root_cause_terms = ("channel stuffing", "bill-and-hold", "premature cutoff", "pulled forward", "round-tripping")

    # ------------------------------------------------------------------ calendar

    def new_state(self, ctx: BuildContext, params: dict[str, Any]) -> WorldState:
        """Two warm-up quarters so TTM Beneish indices exist for a clean comparison quarter (P6 vs P2)."""
        return WorldState(params=params, calendar=FiscalCalendar.from_seed(ctx.seed, warmup=2), ledger=Ledger())

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 4 (difficulty scales magnitude, mechanisms, dirt, red herrings)."""
        ind = industry or rng.choice(self.industries)
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")

        def band(metric_key: str, q_lo: float, q_hi: float) -> float:
            return bk.sample_between(pool, rng.spawn(f"bench:{metric_key}"), f"{ind}.{metric_key}", ind, q_lo, q_hi)

        rev_usd = math.exp(rng.uniform(math.log(15e6), math.log(60e6)))
        if difficulty == "principal":
            mechs = ["channel_stuffing", str(rng.choice(("bill_and_hold", "premature_cutoff")))]
        else:
            mechs = [str(rng.choice(("channel_stuffing", "bill_and_hold", "premature_cutoff")))]
        stuffing = magnitude(rng, difficulty, 0.06, 0.18)
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": {"manufacturing": "industrial components manufacturer selling through distributors and direct accounts",
                               "ecommerce": "consumer-goods brand with a wholesale channel to distributors and a direct B2B web store"}[ind],
            "revenue_ttm_cents": int(rev_usd * 100),
            "gm": float(np.clip(band("gross_margin", 0.4, 0.7), 0.30, 0.42) if ind == "manufacturing" else np.clip(band("gross_margin", 0.3, 0.6), 0.38, 0.52)),
            "growth_qoq": float(rng.uniform(0.02, 0.05)),
            "guidance_qoq": float(rng.uniform(0.06, 0.09)),
            "direct_share": float(rng.uniform(0.28, 0.38)),  # review P1 #16: card-paying direct accounts are a minority
            "wholesale_terms": 45,  # distributors on net-45: baseline DSO ~35 days
            "dating_extra": int(rng.integers(30, 61)),
            "mechanisms": mechs,
            "stuffing_pct": stuffing,
            "second_pct": stuffing * float(rng.uniform(0.5, 0.8)),
            "round_trip": difficulty == "principal",
            "rt_pct": float(rng.uniform(0.01, 0.02)),
            "stuffed_terms_extra": int(rng.integers(60, 91)),
            "stuffed_discount_pt": float(rng.uniform(0.05, 0.12)),
            "return_rate_base": float(rng.uniform(0.02, 0.05)),
            "stuffed_return_mult": float(rng.uniform(2.5, 4.0)),
            "return_prob_base": 0.10,
            "base_last5_share": float(rng.uniform(0.10, 0.14)),
            "n_distributors": int(rng.integers(8, 26)),
            "n_stuffed": int(rng.integers(3, 6)),
            "order_drop": float(rng.uniform(0.5, 0.8)),
            "allowance_pct": float(np.clip(band("allowance_pct_ar", 0.25, 0.5), 0.01, 0.03)),
            "freight_pct": float(rng.uniform(0.02, 0.04)),
            "op_margin": float(rng.uniform(0.07, 0.11)),
            "tax_rate": float(np.clip(band("effective_tax_rate", 0.4, 0.7), 0.15, 0.26)),
            "capex_pct": float(np.clip(band("capex_pct_revenue", 0.25, 0.5), 0.015, 0.05)),
            "dio": float(np.clip(band("dio", 0.2, 0.4), 45, 80)),
            "dpo": float(np.clip(band("dpo", 0.25, 0.5), 30, 55)),
            "cash_pct": float(rng.uniform(0.04, 0.07)),
            "debt_to_ebitda": float(rng.uniform(0.8, 1.6)),
            "interest_rate": float(rng.uniform(0.07, 0.10)),
            "litigation_pct": float(rng.uniform(0.01, 0.03)),
            "inventory_cap_months": float(rng.uniform(2.0, 3.0)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
            "slow_pay_days": int(rng.integers(15, 26)),  # extra days the over-stocked distributors take to pay from P7
        }
        p["n_products"] = difficulty_pick(difficulty, 24, 40, 60)
        p["n_suppliers"] = difficulty_pick(difficulty, 6, 9, 12)
        p["n_direct"] = difficulty_pick(difficulty, 90, 200, 360)
        budget = ROW_BUDGET[difficulty]
        gl_rows = difficulty_pick(difficulty, 3300, 3400, 11500)
        fixed = gl_rows + p["n_products"] * 31 + p["n_suppliers"] * 62 + p["n_direct"] + 400
        p["n_orders"] = int(max(600, (budget * difficulty_pick(difficulty, 0.92, 0.92, 0.87) - fixed) / 5.9))
        p["description"] = (f"{cap_first(p['business_model'])}; roughly {(1 - p['direct_share']) * 100:.0f}% of revenue goes through "
                            f"{p['n_distributors']} distributors on net-{p['wholesale_terms']} terms.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Customers, suppliers, products and the baseline (healthy) order book for all 30 months."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=False)  # base working-capital defaults: prepaid expenses (AP already comes from the purchasing / cost sub-ledger)
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        ind = p["industry"]
        # --- customers: distributors + direct accounts
        nd, nx = p["n_distributors"], p["n_direct"]
        dw = 1.0 + g.pareto(1.3, nd)
        dw = np.sort(dw)[::-1]
        xw = 1.0 + g.pareto(2.0, nx)
        names = [rng.company_name("retail") + (" Supply" if i % 3 == 0 else " Distribution") for i in range(nd)] \
            + [rng.company_name(ind if ind in ("manufacturing", "ecommerce") else None) for _ in range(nx)]
        cid = [f"D{i:03d}" for i in range(1, nd + 1)] + [f"C{i:05d}" for i in range(1, nx + 1)]
        n_c = nd + nx
        customers = pd.DataFrame({
            "customer_id": cid,
            "name": names,
            "channel": ["wholesale"] * nd + ["direct"] * nx,
            "region": np.array(REGIONS)[g.integers(0, len(REGIONS), n_c)],
            "payment_terms_days": [p["wholesale_terms"]] * nd + [0] * nx,
            "credit_limit_cents": np.concatenate([np.round(dw / dw.max() * 600_000_000 / 100_000) * 100_000 + 50_000_000,
                                                  np.full(nx, 2_500_000)]).astype(np.int64),
            "tax_id": [f"US-{g.integers(10, 99)}-{g.integers(1_000_000, 9_999_999)}" for _ in range(n_c)],
            "address": [f"{g.integers(100, 9999)} {STREETS[int(g.integers(0, len(STREETS)))]}, {rng.city()}" for _ in range(n_c)],
            "email": [f"ap@{n.split()[0].lower()}{i}.example.com" for i, n in enumerate(names)],
            "sales_rep_id": np.array([f"R{k:02d}" for k in range(1, 9)])[g.integers(0, 8, n_c)],
        })
        # --- suppliers
        V = p["n_suppliers"]
        suppliers = pd.DataFrame({
            "supplier_id": [f"V{i:03d}" for i in range(1, V + 1)],
            "name": [rng.company_name("manufacturing") for _ in range(V)],
            "country": np.array(("US", "US", "MX", "CN", "VN", "CA"))[g.integers(0, 6, V)],
            "payment_terms_days": np.full(V, int(round(p["dpo"] / 15.0) * 15) or 30, dtype=np.int64),
            "tax_id": [f"US-{g.integers(10, 99)}-{g.integers(1_000_000, 9_999_999)}" for _ in range(V)],
            "address": [f"{g.integers(100, 9999)} {STREETS[int(g.integers(0, len(STREETS)))]}, {rng.city()}" for _ in range(V)],
        })
        state.extras["supplier_weight"] = 1.0 + g.pareto(1.5, V)
        # --- products
        S = p["n_products"]
        cats = CATEGORIES[ind]
        price_usd = np.clip(g.lognormal(math.log(180 if ind == "manufacturing" else 45), 0.7, S), 6, 4000)
        gm_sku = np.clip(g.normal(p["gm"], 0.05, S), 0.12, 0.7)
        pop = 1.0 + g.pareto(1.2, S)
        pop /= pop.sum()
        products = pd.DataFrame({
            "product_id": [f"P{i:04d}" for i in range(1, S + 1)],
            "sku": [f"{cats[i % len(cats)][:3].upper()}-{int(g.integers(1000, 9999))}-{i}" for i in range(S)],
            "title": [f"{cats[i % len(cats)]} model {i + 1}" for i in range(S)],
            "category": [cats[i % len(cats)] for i in range(S)],
            "list_price_cents": np.round(price_usd * 100).astype(np.int64),
            "supplier_id": suppliers["supplier_id"].to_numpy()[g.integers(0, V, S)],
            "active": True,
        })
        products["unit_cost_cents"] = np.maximum(50, np.round(products["list_price_cents"] * (1 - gm_sku))).astype(np.int64)
        state.extras["popularity"] = pop
        # --- daily revenue weights over the whole horizon: trend x quarter-end window weight
        start = pd.Timestamp(cal.start)
        end = pd.Timestamp(cal.end)
        days = pd.date_range(start, end, freq="D")
        qend = days + pd.offsets.QuarterEnd(0)
        in_win = np.asarray((qend - days).days < WINDOW_DAYS)
        b = p["base_last5_share"]
        w_win = (91 - WINDOW_DAYS) * b / (WINDOW_DAYS * (1 - b))
        m_idx = np.asarray((days.year - start.year) * 12 + (days.month - start.month))
        trend = (1 + p["growth_qoq"]) ** (m_idx / 3.0) * g.lognormal(0, 0.03, n_m)[m_idx]
        weekday = np.where(np.asarray(days.dayofweek) >= 5, 0.35, 1.0)
        dayw = trend * np.where(in_win, w_win, 1.0) * weekday
        # renormalise the window weight per quarter so the window share is exactly the target in expectation
        dq = pd.Series(dayw, index=days).groupby(days.to_period("Q"))
        win_sum = pd.Series(np.where(in_win, dayw, 0.0), index=days).groupby(days.to_period("Q")).transform("sum").to_numpy()
        tot_sum = dq.transform("sum").to_numpy()
        adj = np.where(in_win, b / np.maximum(win_sum / tot_sum, 1e-9), (1 - b) / np.maximum(1 - win_sum / tot_sum, 1e-9))
        dayw = dayw * adj
        # --- orders: count split direct / wholesale, order values lognormal
        N = p["n_orders"]
        n_direct = int(round(N * 0.5))
        n_whole = N - n_direct
        pday = dayw / dayw.sum()
        di = np.sort(g.choice(len(days), size=n_direct, p=pday))
        wi = np.sort(g.choice(len(days), size=n_whole, p=pday))
        R = p["revenue_ttm_cents"] / 100.0
        horizon_rev = R * float(dayw.sum() / dayw[-365:].sum())
        v_direct = horizon_rev * p["direct_share"] / n_direct
        v_whole = horizon_rev * (1 - p["direct_share"]) / n_whole
        dval = g.lognormal(math.log(v_direct) - 0.08, 0.4, n_direct)
        wcust = g.choice(nd, size=n_whole, p=dw / dw.sum())
        wval = g.lognormal(math.log(v_whole) - 0.125, 0.5, n_whole)
        xcust = g.choice(nx, size=n_direct, p=xw / xw.sum())
        orders = pd.DataFrame({
            "inv_date": np.concatenate([days.to_numpy()[di], days.to_numpy()[wi]]),
            "customer_idx": np.concatenate([nd + xcust, wcust]),
            "value_usd": np.concatenate([dval, wval]),
            "channel": ["direct"] * n_direct + ["wholesale"] * n_whole,
        })
        # calibrate values per (month, window, channel) cell to the monthly trend so the quarter-end window share and the
        # channel split hold per quarter (small samples would otherwise make quarterly revenue lumpy)
        allp = cal.all_periods
        mtarget = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3.0) * g.lognormal(0, 0.02, n_m)
        mtarget *= R / mtarget[-12:].sum()
        o_dates = pd.to_datetime(orders["inv_date"])
        o_m = _month_index(o_dates, cal.start)
        o_q = o_m // 3
        o_win = np.asarray((o_dates + pd.offsets.QuarterEnd(0) - o_dates).dt.days < WINDOW_DAYS)
        nw_days = np.array([(pd.Timestamp(months[m]) + pd.offsets.MonthEnd(0)).day - (WINDOW_DAYS if m % 3 == 2 else 0) for m in range(n_m)], dtype=float)
        vals = orders["value_usd"].to_numpy().copy()
        is_direct = (orders["channel"] == "direct").to_numpy()
        for q in range(len(allp)):
            ms = np.arange(3 * q, 3 * q + 3)
            T = mtarget[ms].sum()
            bq = float(np.clip(b * g.lognormal(0, 0.05), 0.09, 0.15))
            nw = T * (1 - bq) * mtarget[ms] * nw_days[ms] / (mtarget[ms] * nw_days[ms]).sum()
            cells = [(o_q == q) & o_win, *[(o_m == m) & ~o_win for m in ms]]
            for cell, tgt in zip(cells, [T * bq, *nw], strict=True):
                for ch_mask, share_ch in ((is_direct, p["direct_share"]), (~is_direct, 1 - p["direct_share"])):
                    sel = cell & ch_mask
                    tot = vals[sel].sum()
                    if tot > 0:
                        vals[sel] *= tgt * share_ch / tot
        orders["value_usd"] = vals
        orders["discount_pct"] = np.where(orders["channel"] == "wholesale", np.round(g.choice([0, 0, 0.02, 0.03, 0.05], size=N), 3), 0.0)
        orders["kind"] = "baseline"
        orders["pulled"] = False
        orders["terms_days"] = np.where(orders["channel"] == "wholesale", p["wholesale_terms"], 0)
        orders["notes"] = None
        orders["release_days"] = np.nan
        state.frames.update({"customers": customers, "suppliers": suppliers, "products": products})
        state.extras["orders_plan"] = orders
        state.extras["dist_weight"] = dw

    # ------------------------------------------------------------------ anomalies

    def _quarter_rev(self, orders: pd.DataFrame, cal: FiscalCalendar, period: str) -> float:
        s, e = cal.bounds(period)
        sel = (orders["inv_date"] >= pd.Timestamp(s)) & (orders["inv_date"] <= pd.Timestamp(e)) & (orders["kind"] == "baseline")
        return float(orders.loc[sel, "value_usd"].sum())

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Pull-forward orders by mechanism, the P8 order trough, round-trip pairs and the flat allowance."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("anomaly")
        g = rng.generator
        orders = state.extras["orders_plan"]
        P = state.periods
        nd = p["n_distributors"]
        top = np.arange(p["n_stuffed"])  # distributors are sorted by weight
        new_rows: list[pd.DataFrame] = []
        state.extras["pulled_by_period"] = {}
        mechs = p["mechanisms"]
        anomalies: list[Anomaly] = []
        aid = 0
        for k, mech in enumerate(mechs):
            pct = p["stuffing_pct"] if k == 0 else p["second_pct"]
            # channel stuffing runs in P7 and P8 at full size; bill-and-hold / cutoff start smaller in P7 (half size) and
            # escalate in P8 - the P7 tranche ships early in P8 (documented deviation: spec places them in P8 only)
            periods = [P[6], P[7]]
            for period in periods:
                base_q = self._quarter_rev(orders, cal, period)
                X = pct * base_q * (1.0 if mech == "channel_stuffing" or period == P[7] else 0.5)
                release = np.nan
                s, e = cal.bounds(period)
                e_ts = pd.Timestamp(e)
                if mech == "channel_stuffing":
                    n = int(g.integers(3, 6)) * len(top)
                    cust = g.choice(top, size=n, p=state.extras["dist_weight"][top] / state.extras["dist_weight"][top].sum())
                    day = e_ts - pd.to_timedelta(g.integers(0, WINDOW_DAYS, n), unit="D")
                    disc = np.round(np.minimum(0.25, g.choice([0, 0.02, 0.03], size=n) + p["stuffed_discount_pt"]), 3)
                    terms = p["wholesale_terms"] + p["stuffed_terms_extra"]
                    notes = np.where(g.random(n) < 0.7, "Q-end promotion: extended dating", "Q-end promotion")
                elif mech == "bill_and_hold":
                    n = int(g.integers(4, 9))
                    cust = g.choice(nd, size=n, p=state.extras["dist_weight"] / state.extras["dist_weight"].sum())
                    day = e_ts - pd.to_timedelta(g.integers(0, 10, n), unit="D")
                    disc = np.zeros(n)
                    terms = p["wholesale_terms"] + p["dating_extra"]
                    notes = np.where(g.random(n) < 0.6, "Customer requested hold - per sales", "Hold pending customer warehouse space - per sales")
                    release = np.nan if period == P[7] else 1.0
                else:  # premature_cutoff
                    n = int(g.integers(5, 11))
                    cust = g.choice(nd, size=n, p=state.extras["dist_weight"] / state.extras["dist_weight"].sum())
                    day = e_ts - pd.to_timedelta(g.integers(0, 3, n), unit="D")
                    disc = np.zeros(n)
                    terms = p["wholesale_terms"] + p["dating_extra"]
                    notes = np.full(n, None, dtype=object)
                w = g.lognormal(0, 0.3, n)
                vals = X * w / w.sum() / (1 - disc)  # list value so that net (after discount) sums to X
                new_rows.append(pd.DataFrame({
                    "inv_date": day.to_numpy(), "customer_idx": cust, "value_usd": vals, "channel": "wholesale",
                    "discount_pct": disc, "kind": mech, "pulled": True, "terms_days": terms, "notes": notes,
                    "release_days": g.integers(8, 45, n) * release if mech == "bill_and_hold" else np.full(n, np.nan),
                }))
            aid += 1
            kind = MECH_KIND[mech]
            mech_text = {
                "channel_stuffing": (f"Extra wholesale orders worth ~{pct:.1%} of quarterly revenue were booked in the last {WINDOW_DAYS} days of "
                                     f"{' and '.join(periods)} for the top {len(top)} distributors with a +{p['stuffed_discount_pt'] * 100:.0f}pt "
                                     f"'quarter-end promotion' discount and terms extended by {p['stuffed_terms_extra']} days; window orders return at "
                                     f"{p['stuffed_return_mult']:.1f}x the base rate and the same distributors' regular orders drop in {P[7]}."),
                "bill_and_hold": (f"Orders worth ~{pct:.1%} of {P[7]} revenue (~{pct / 2:.1%} of {P[6]} a quarter earlier) were invoiced in the last "
                                  f"10 days of the quarter on +{p['dating_extra']}-day dating but held in our warehouse; the {P[6]} goods were released "
                                  f"during {P[7]}, the {P[7]} goods are still shipments.status='awaiting_release' with shipped_at NULL. Revenue booked "
                                  "at invoice, COGS only at shipment."),
                "premature_cutoff": (f"Orders worth ~{pct:.1%} of {P[7]} revenue (~{pct / 2:.1%} of {P[6]}) were invoiced in the last 3 days of the "
                                     f"quarter on +{p['dating_extra']}-day dating but shipped in the first days of the next quarter; revenue recognised "
                                     "before control transferred."),
            }[mech]
            anomalies.append(Anomaly(
                id=f"A{aid}", kind=kind, pillar="accounting_finance", title={"channel_stuffing": "Quarter-end channel loading",
                                                                               "bill_and_hold": "Revenue on goods still in our warehouse",
                                                                               "premature_cutoff": "Shipments booked in the wrong quarter"}[mech],
                mechanism=mech_text, periods=periods, affected_accounts=["4000", "1100", "5000", "1220"],
                affected_tables=["invoices", "orders", "shipments", "returns", "customers"],
                magnitude={"pulled_forward_pct_of_quarter_revenue": round(pct, 6)}, severity="critical" if k == 0 else "high"))
            state.extras.setdefault("mech_ids", {})[mech] = f"A{aid}"
        if new_rows:
            orders = pd.concat([orders, *new_rows], ignore_index=True)
        # order trough: P8 regular orders of the P7-stuffed distributors drop by order_drop x X7
        if "channel_stuffing" in mechs:
            s7, e7 = cal.bounds(P[6])
            st7 = orders[(orders["kind"] == "channel_stuffing") & (orders["inv_date"] <= pd.Timestamp(e7))]
            x7 = float((st7["value_usd"] * (1 - st7["discount_pct"])).sum())
            s8, e8 = cal.bounds(P[7])
            cand = orders.index[(orders["kind"] == "baseline") & orders["customer_idx"].isin(top)
                                & (orders["inv_date"] >= pd.Timestamp(s8)) & (orders["inv_date"] <= pd.Timestamp(e8))].to_numpy()
            cand = cand[g.permutation(len(cand))]
            cum = np.cumsum(orders.loc[cand, "value_usd"].to_numpy())
            drop = cand[cum <= p["order_drop"] * x7]
            orders = orders.drop(index=drop)
            state.extras["dropped_value_usd"] = float(cum[len(drop) - 1]) if len(drop) else 0.0
        # round trip (principal): monthly sales to a related "distributor" mirrored by purchases from its twin supplier
        if p["round_trip"]:
            sup = state.frames["suppliers"]
            cust = state.frames["customers"]
            rt_name = rng.company_name("retail") + " Trading"
            rt_tax = f"US-{g.integers(10, 99)}-{g.integers(1_000_000, 9_999_999)}"
            rt_addr = f"{g.integers(100, 9999)} {STREETS[int(g.integers(0, len(STREETS)))]}, {rng.city()}"
            ci = len(cust)
            cust = pd.concat([cust, pd.DataFrame([{
                "customer_id": f"D{nd + 1:03d}", "name": rt_name, "channel": "wholesale", "region": str(rng.choice(REGIONS)),
                "payment_terms_days": p["wholesale_terms"], "credit_limit_cents": 300_000_000, "tax_id": rt_tax.lower() + " ", "address": rt_addr.upper(),
                "email": f"ap@{rt_name.split()[0].lower()}.example.com", "sales_rep_id": "R01"}])], ignore_index=True)
            vi = len(sup)
            sup = pd.concat([sup, pd.DataFrame([{
                "supplier_id": f"V{vi + 1:03d}", "name": rt_name.replace("Trading", "Sourcing"), "country": "US",
                "payment_terms_days": 15, "tax_id": rt_tax, "address": rt_addr}])], ignore_index=True)
            state.extras["supplier_weight"] = np.append(state.extras["supplier_weight"], 0.0)
            state.frames["customers"], state.frames["suppliers"] = cust, sup
            months = state.extras["months"]
            rows = []
            for m in range(len(months) - 12, len(months)):
                ms = pd.Timestamp(months[m])
                rev_m = float(orders.loc[(orders["kind"] == "baseline") & (orders["inv_date"] >= ms)
                                         & (orders["inv_date"] < ms + pd.offsets.MonthBegin(1)), "value_usd"].sum())
                rows.append({"inv_date": ms + pd.Timedelta(days=int(g.integers(12, 26))), "customer_idx": ci, "value_usd": p["rt_pct"] * rev_m,
                             "channel": "wholesale", "discount_pct": 0.0, "kind": "round_trip", "pulled": False,
                             "terms_days": p["wholesale_terms"], "notes": None, "release_days": np.nan})
            orders = pd.concat([orders, pd.DataFrame(rows)], ignore_index=True)
            state.extras["rt"] = {"customer_id": f"D{nd + 1:03d}", "supplier_id": f"V{vi + 1:03d}", "name": rt_name}
            aid += 1
            anomalies.append(Anomaly(
                id=f"A{aid}", kind="round_tripping", pillar="accounting_finance", title="Round-trip sales with a related party",
                mechanism=(f"{rt_name} appears both as a distributor and (as '{rt_name.replace('Trading', 'Sourcing')}') as a supplier with the same tax id "
                           f"and address; each month of the last year the company buys goods from the supplier and sells them to the customer at ~1% margin "
                           f"(~{p['rt_pct']:.1%} of revenue), cash going round in a circle."),
                periods=list(P[4:]), affected_accounts=["4000", "1220", "2000"], affected_tables=["customers", "suppliers", "purchase_orders", "invoices"],
                magnitude={"round_trip_pct_of_revenue": round(p["rt_pct"], 6)}, severity="high"))
            state.extras.setdefault("mech_ids", {})["round_trip"] = f"A{aid}"
        aid += 1
        first = min(a.periods[0] for a in anomalies)
        state.extras["allowance_freeze_period"] = first
        anomalies.append(Anomaly(
            id=f"A{aid}", kind="understated_allowance", pillar="accounting_finance", title="Receivables balloon while the allowance stays flat",
            mechanism=(f"From {first} the allowance for doubtful accounts is held at its prior dollar balance while gross receivables "
                       "(including extended-dating invoices) grow, so the allowance as a share of AR falls and bad-debt expense is understated."),
            periods=[x for x in P if x >= first], affected_accounts=["1110", "6210", "1100"], affected_tables=["gl_lines", "invoices", "payments"],
            magnitude={}, severity="medium"))
        state.extras["allowance_id"] = f"A{aid}"
        orders = orders.sort_values(["inv_date", "kind", "customer_idx"], kind="stable").reset_index(drop=True)
        state.extras["orders_plan"] = orders
        return anomalies

    # ------------------------------------------------------------------ derive frames

    def _derive(self, ctx: BuildContext, state: WorldState) -> None:
        """Orders, lines, shipments, invoices, payments, returns, purchase orders, inventory movements."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        plan = state.extras["orders_plan"]
        customers, products = state.frames["customers"], state.frames["products"]
        N = len(plan)
        S = len(products)
        end_ts = pd.Timestamp(cal.end) + EOD
        pop = state.extras["popularity"]
        lp = products["list_price_cents"].to_numpy()
        uc = products["unit_cost_cents"].to_numpy()
        # --- lines: 1-4 per order; round-trip orders use a dedicated resale kit SKU
        k = 1 + np.minimum(g.poisson(0.9, N), 3)
        rt = (plan["kind"] == "round_trip").to_numpy()
        k = np.where(rt, 1, k)
        o_idx = np.repeat(np.arange(N), k)
        sku = g.choice(S, size=len(o_idx), p=pop)
        share = g.dirichlet(np.ones(4), size=N)
        pos = np.concatenate([np.arange(x) for x in k])
        sh = np.where(np.arange(4)[None, :] < k[o_idx][:, None], share[o_idx], 0.0)
        line_share = share[o_idx, pos] / sh.sum(axis=1)
        target_cents = plan["value_usd"].to_numpy()[o_idx] * 100 * line_share
        unit_price = lp[sku].astype(np.int64)
        unit_cost = uc[sku].astype(np.int64)
        if rt.any():
            # resale kit: price fixed per line so that qty x price ~ target; cost = price / U(1.0, 1.02)
            rt_line = rt[o_idx]
            kit_price = 125_000
            unit_price = np.where(rt_line, kit_price, unit_price)
            unit_cost = np.where(rt_line, np.round(kit_price / g.uniform(1.0, 1.02, len(o_idx))).astype(np.int64), unit_cost)
        qty = np.maximum(1, np.round(target_cents / unit_price)).astype(np.int64)
        line_total = qty * unit_price
        subtotal = np.bincount(o_idx, weights=line_total, minlength=N).astype(np.int64)
        cost = np.bincount(o_idx, weights=qty * unit_cost, minlength=N).astype(np.int64)
        disc = plan["discount_pct"].to_numpy()
        discount = np.round(subtotal * disc).astype(np.int64)
        total = subtotal - discount
        inv_date = pd.to_datetime(plan["inv_date"]).dt.normalize()
        order_ts = inv_date - pd.to_timedelta(g.integers(0, 4, N), unit="D") + pd.to_timedelta(g.integers(7 * 3600, 18 * 3600, N), unit="s")
        order_ts = order_ts.where(order_ts >= pd.Timestamp(cal.start), inv_date + pd.Timedelta(hours=8))
        kind = plan["kind"].to_numpy()
        # --- shipments
        ship_ts = inv_date + pd.to_timedelta(g.integers(8 * 3600, 20 * 3600, N), unit="s")
        status = np.full(N, "delivered", dtype=object)
        held = kind == "bill_and_hold"
        cutoff = kind == "premature_cutoff"
        qend = inv_date + pd.offsets.QuarterEnd(0)
        if cutoff.any():
            ship_ts = ship_ts.where(~cutoff, qend + pd.to_timedelta(g.integers(1, 6, N), unit="D")
                                    + pd.to_timedelta(g.integers(8 * 3600, 18 * 3600, N), unit="s"))
        release = plan["release_days"].to_numpy()
        released = held & ~np.isnan(release)
        ship_ts = ship_ts.where(~held, qend + pd.to_timedelta(np.nan_to_num(release), unit="D") + pd.Timedelta(hours=10))
        ship_ts = ship_ts.where(~(held & ~released), pd.NaT)
        held = held & ~released
        status[held] = "awaiting_release"
        lead = np.ceil(g.lognormal(math.log(3), 0.4, N))
        # red herring made real: the new 3PL slowed deliveries during P7 onboarding (delivered_at, not shipped_at)
        s7, e7 = cal.bounds(state.periods[6])
        in_p7 = ((inv_date >= pd.Timestamp(s7)) & (inv_date <= pd.Timestamp(e7))).to_numpy()
        lead = np.where(in_p7, lead + g.integers(2, 5, N), lead)
        delivered = ship_ts + pd.to_timedelta(lead, unit="D")
        status = np.where((~held) & (delivered > end_ts), "in_transit", status)
        delivered = delivered.where(delivered <= end_ts)
        # --- invoices & payments
        cust_ids = customers["customer_id"].to_numpy()[plan["customer_idx"].to_numpy()]
        channel = plan["channel"].to_numpy()
        terms = plan["terms_days"].to_numpy().astype(np.int64)
        delay = np.where(channel == "direct", 0, np.maximum(-3, np.round(g.normal(3, 4, N))))
        # review P1 #16: distributors that took the pulled-forward orders sit on extra stock (or on goods they never
        # received) and slow-pay their regular invoices due from P7 on, so DSO climbs from ~35 to ~55 days
        gs = ctx.rng.spawn("slow_pay").generator
        pulled_plan = plan["pulled"].to_numpy(dtype=bool)
        slow_cust = np.isin(plan["customer_idx"].to_numpy(), np.unique(plan["customer_idx"].to_numpy()[pulled_plan]))
        due = inv_date + pd.to_timedelta(terms, unit="D")
        slow = slow_cust & ~pulled_plan & (channel == "wholesale") & (plan["kind"] != "round_trip").to_numpy() \
            & (due >= pd.Timestamp(cal.bounds(state.periods[6])[0])).to_numpy()
        delay = delay + np.where(slow, gs.integers(p["slow_pay_days"] - 7, p["slow_pay_days"] + 8, N), 0)
        state.extras["slow_pay_invoices"] = int(slow.sum())
        pay_ts = inv_date + pd.to_timedelta(terms + delay, unit="D") + pd.to_timedelta(g.integers(9 * 3600, 17 * 3600, N), unit="s")
        pay_ts = pay_ts.where(channel != "direct", ship_ts.fillna(inv_date) + pd.Timedelta(minutes=5))
        # --- returns
        # value return rate = probability x returned fraction: base q_b x f_b = return_rate_base; window (stuffed) orders return far more
        # often ("overstock") so that their value rate is stuffed_return_mult x the base rate
        stuffed = kind == "channel_stuffing"
        q_ret = np.where(stuffed, 0.7, p["return_prob_base"])
        f_mean = np.where(stuffed, p["return_rate_base"] * p["stuffed_return_mult"] / 0.7, p["return_rate_base"] / p["return_prob_base"])
        q_ret[rt] = 0.0
        returned = (g.random(N) < q_ret) & ~held
        ret_at = ship_ts + pd.to_timedelta(g.integers(20, 41, N), unit="D") + pd.to_timedelta(g.integers(9 * 3600, 17 * 3600, N), unit="s")
        returned &= (ret_at <= end_ts).fillna(False).to_numpy()
        frac = np.clip(f_mean * np.where(stuffed, g.uniform(0.8, 1.2, N), g.uniform(0.6, 1.4, N)), 0.02, 1.0)
        ret_amt = np.where(returned, np.round(total * frac), 0).astype(np.int64)
        ret_cost = np.where(returned, np.round(cost * frac), 0).astype(np.int64)
        credit_memo = returned & (ret_at < pay_ts).fillna(False).to_numpy()  # unpaid when returned -> credit memo against AR
        pay_amt = total - np.where(credit_memo, ret_amt, 0)
        paid = (pay_ts <= end_ts).to_numpy() & (pay_amt > 0)
        order_ids = np.array([f"SO{i:06d}" for i in range(1, N + 1)])
        orders = pd.DataFrame({
            "order_id": order_ids, "customer_id": cust_ids, "order_ts": order_ts, "channel": channel,
            "status": np.where(held, "billed_on_hold", np.where(status == "delivered", "fulfilled", "shipped")),
            "subtotal_cents": subtotal, "discount_pct": disc, "discount_cents": discount, "total_cents": total, "currency": "USD",
            "ship_region": customers["region"].to_numpy()[plan["customer_idx"].to_numpy()],
            "sales_rep_id": customers["sales_rep_id"].to_numpy()[plan["customer_idx"].to_numpy()],
            "notes": plan["notes"].to_numpy(), "created_at": order_ts,
            "_kind": kind, "_pulled": plan["pulled"].to_numpy(),
        })
        oi = pd.DataFrame({
            "order_item_id": [f"OI{i:07d}" for i in range(1, len(o_idx) + 1)], "order_id": order_ids[o_idx],
            "product_id": np.where(rt[o_idx], "P-KIT", products["product_id"].to_numpy()[sku]),
            "quantity": qty, "unit_price_cents": unit_price, "unit_cost_cents": unit_cost, "line_total_cents": line_total,
        })
        shipments = pd.DataFrame({
            "shipment_id": [f"SH{i:06d}" for i in range(1, N + 1)], "order_id": order_ids, "status": status,
            "carrier": np.array(("XPO", "Old Dominion", "UPS Freight", "Estes", "FedEx Freight"))[g.integers(0, 5, N)],
            "shipped_at": ship_ts, "delivered_at": delivered,
            "warehouse_id": np.array(("WH-EAST", "WH-WEST", "WH-CENTRAL"))[g.integers(0, 3, N)],
            "freight_cost_cents": np.round(total * p["freight_pct"] * g.lognormal(-0.01, 0.15, N)).astype(np.int64),
            "_cost": cost,
        })
        inv = pd.DataFrame({
            "invoice_id": [f"INV{i:06d}" for i in range(1, N + 1)], "order_id": order_ids, "customer_id": cust_ids,
            "invoice_date": inv_date, "due_date": inv_date + pd.to_timedelta(terms, unit="D"), "terms_days": terms,
            "amount_cents": total, "status": np.where(paid, "paid", "open"), "created_at": inv_date + pd.Timedelta(hours=20),
            "ingest_id": np.ones(N, dtype=np.int64), "_kind": kind, "_pulled": plan["pulled"].to_numpy(),
            "_cost": cost, "_channel": channel,
        })
        payments = pd.DataFrame({
            "invoice_id": inv["invoice_id"].to_numpy()[paid], "customer_id": cust_ids[paid], "paid_at": pay_ts[paid].to_numpy(),
            "amount_cents": pay_amt[paid], "method": np.where(channel[paid] == "direct", "card", np.where(g.random(int(paid.sum())) < 0.6, "ach", "wire")),
        })
        payments = payments.sort_values(["paid_at", "invoice_id"], kind="stable").reset_index(drop=True)
        payments.insert(0, "payment_id", [f"PMT{i:06d}" for i in range(1, len(payments) + 1)])
        ri = np.flatnonzero(returned)
        reasons_pool = np.array(("overstock", "damaged", "wrong_item", "not_as_described"))
        reasons = np.where(stuffed[ri], np.where(g.random(len(ri)) < 0.8, "overstock", "damaged"), reasons_pool[g.integers(1, 4, len(ri))])
        returns = pd.DataFrame({
            "order_id": order_ids[ri], "requested_at": (ret_at.iloc[ri] - pd.to_timedelta(g.integers(2, 8, len(ri)), unit="D")).to_numpy(),
            "received_at": ret_at.iloc[ri].to_numpy(), "reason": reasons, "refund_amount_cents": ret_amt[ri],
            "settlement": np.where(credit_memo[ri], "credit_memo", "refund"), "restock": True, "condition": "resaleable",
            "_cost": ret_cost[ri], "_invoice": inv["invoice_id"].to_numpy()[ri], "_stuffed": stuffed[ri],
        }).sort_values(["received_at", "order_id"], kind="stable").reset_index(drop=True)
        returns.insert(0, "return_id", [f"RT{i:05d}" for i in range(1, len(returns) + 1)])
        state.frames.update({"orders": orders, "order_items": oi, "shipments": shipments, "invoices": inv, "payments": payments, "returns": returns})
        self._purchasing(ctx, state)

    def _purchasing(self, ctx: BuildContext, state: WorldState) -> None:
        """Monthly POs per supplier replenishing shipped COGS (+ round-trip purchases), and inventory movements."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("purchasing").generator
        months = state.extras["months"]
        n_m = len(months)
        end_ts = pd.Timestamp(cal.end) + EOD
        sh = state.frames["shipments"]
        orders = state.frames["orders"]
        shipped = sh[sh["shipped_at"].notna() & (sh["shipped_at"] <= end_ts)]
        rt_orders = set(orders.loc[orders["_kind"] == "round_trip", "order_id"])
        regular = shipped[~shipped["order_id"].isin(rt_orders)]
        cogs_m = np.bincount(_month_index(regular["shipped_at"], cal.start), weights=regular["_cost"], minlength=n_m)
        sup = state.frames["suppliers"]
        sw = state.extras["supplier_weight"][: len(sup)]
        regular_sup = np.flatnonzero(sw > 0)
        ret = state.frames["returns"]
        restock_m = np.bincount(np.clip(_month_index(ret["received_at"], cal.start), 0, n_m - 1), weights=ret["_cost"], minlength=n_m)
        # order-up-to on value: receipts bring month-end stock back to ``dio`` days of trailing COGS
        inv0 = cents(cogs_m[:3].sum() / 91.0 * p["dio"])
        state.extras["inv0"] = inv0
        on_hand = float(inv0)
        rows = []
        for m in range(n_m):
            trailing = cogs_m[max(0, m - 2): m + 1].mean()
            target_end = trailing / 30.4 * p["dio"] * g.lognormal(0, 0.03)
            projected = on_hand - cogs_m[m] + restock_m[m]
            need = max(0.3 * cogs_m[m], target_end - projected)
            on_hand = projected + need
            parts = split_cents(int(need), list(sw[regular_sup] / sw[regular_sup].sum()))
            ms = pd.Timestamp(months[m])
            for j, amt in zip(regular_sup, parts, strict=True):
                if amt > 0:
                    rows.append((int(j), amt, ms + pd.Timedelta(days=int(g.integers(0, 12)), hours=int(g.integers(7, 16))), "regular"))
        if "rt" in state.extras:
            vj = len(sup) - 1
            rt_sh = shipped[shipped["order_id"].isin(rt_orders)]
            for _, r in rt_sh.iterrows():
                rows.append((vj, int(r["_cost"]), r["shipped_at"] - pd.Timedelta(days=int(g.integers(3, 9))), "round_trip"))
        po = pd.DataFrame(rows, columns=["sup_idx", "amount_cents", "received_at", "_kind"]).sort_values(["received_at", "sup_idx"], kind="stable").reset_index(drop=True)
        po.insert(0, "po_id", [f"PO{i:05d}" for i in range(1, len(po) + 1)])
        po["supplier_id"] = sup["supplier_id"].to_numpy()[po["sup_idx"].to_numpy()]
        terms = sup["payment_terms_days"].to_numpy()[po["sup_idx"].to_numpy()]
        po["ordered_at"] = (po["received_at"] - pd.to_timedelta(g.integers(10, 35, len(po)), unit="D")).dt.normalize()
        po["paid_at"] = po["received_at"].dt.normalize() + pd.to_timedelta(terms + np.round(g.normal(0, 4, len(po))), unit="D") + pd.Timedelta(hours=11)
        po["paid_at"] = po["paid_at"].where(po["paid_at"] <= end_ts)
        po["status"] = "received"
        po["description"] = np.where(po["_kind"] == "round_trip", "Component kits for resale", "Finished goods replenishment")
        state.frames["purchase_orders"] = po
        # inventory movements (value based): opening, receipts, monthly issues per product, restocks
        oi = state.frames["order_items"]
        shipped_oi = oi[oi["order_id"].isin(shipped["order_id"])].copy()
        shipped_oi["m"] = _month_index(shipped_oi["order_id"].map(shipped.set_index("order_id")["shipped_at"]), cal.start)
        shipped_oi["v"] = shipped_oi["quantity"] * shipped_oi["unit_cost_cents"]
        iss = shipped_oi.groupby(["m", "product_id"], sort=True).agg(q=("quantity", "sum"), v=("v", "sum")).reset_index()
        iss_ts = pd.to_datetime(pd.Series([months[m] for m in iss["m"]])) + pd.offsets.MonthEnd(0) + pd.Timedelta(hours=22)
        ret = state.frames["returns"]
        mv = pd.concat([
            pd.DataFrame({"product_id": [None], "movement_ts": [pd.Timestamp(cal.opening_date) + pd.Timedelta(hours=23)], "movement_type": ["opening"],
                          "quantity": [None], "value_cents": [state.extras["inv0"]], "reference_id": ["OPENING-BALANCE"]}),
            pd.DataFrame({"product_id": None, "movement_ts": po["received_at"], "movement_type": "receipt", "quantity": None,
                          "value_cents": po["amount_cents"], "reference_id": po["po_id"]}),
            pd.DataFrame({"product_id": iss["product_id"], "movement_ts": iss_ts.to_numpy(), "movement_type": "issue",
                          "quantity": -iss["q"], "value_cents": -iss["v"], "reference_id": "MONTHLY-SHIPMENTS"}),
            pd.DataFrame({"product_id": None, "movement_ts": ret["received_at"], "movement_type": "return_restock", "quantity": None,
                          "value_cents": ret["_cost"], "reference_id": ret["return_id"]}),
        ], ignore_index=True)
        mv["movement_ts"] = pd.to_datetime(mv["movement_ts"])
        mv = mv[mv["movement_ts"] <= end_ts].sort_values(["movement_ts", "movement_type"], kind="stable").reset_index(drop=True)
        mv.insert(0, "movement_id", np.arange(1, len(mv) + 1, dtype=np.int64))
        state.frames["inventory_movements"] = mv

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Derive the sub-ledgers, then post the GL month by month."""
        self._derive(ctx, state)
        p, cal = state.params, state.calendar
        L = state.ledger
        gran = GL_BATCH[p["difficulty"]]
        months = state.extras["months"]
        end_date = cal.end
        inv, pay, ret, po, sh = (state.frames[k] for k in ("invoices", "payments", "returns", "purchase_orders", "shipments"))
        R = p["revenue_ttm_cents"]
        # opening balance sheet (common section 3)
        inv0 = state.extras["inv0"]
        ppe = cents(R * p["capex_pct"] * 8 * 0.55)
        accdep = cents(ppe * 0.45)
        debt = int(round(R * (p["op_margin"] + 0.03) * p["debt_to_ebitda"] / 100_000)) * 100_000
        debt_cur = cents(debt * 0.2)
        cash0 = cents(R * p["cash_pct"])
        # receivables / payables of the warm-up quarter come from the generated rows; opening holds neither
        net = cash0 + inv0 + ppe - accdep - debt
        cs = cents(0.6 * net)
        L.open_books(cal.opening_date, {"1000": cash0, "1220": inv0, "1500": ppe, "1510": -accdep, "2600": -debt_cur,
                                        "2610": -(debt - debt_cur), "3000": -cs, "3100": -(net - cs)})
        sh_ok = sh[sh["shipped_at"].notna() & (sh["shipped_at"] <= pd.Timestamp(end_date) + EOD)]
        inv_b = inv.assign(b=batch_end_dates(inv["invoice_date"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("invoice_id", "size"))
        cogs_b = sh_ok.assign(b=batch_end_dates(sh_ok["shipped_at"], gran)).groupby("b", sort=True).agg(c=("_cost", "sum"), f=("freight_cost_cents", "sum"), n=("order_id", "size"))
        pay = pay.assign(card=(pay["method"] == "card"), fee=np.where(pay["method"] == "card", np.round(pay["amount_cents"] * CARD_FEE), 0).astype(np.int64))
        pay_b = pay.assign(b=batch_end_dates(pay["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), fee=("fee", "sum"), n=("payment_id", "size"))
        ret_b = ret.assign(b=batch_end_dates(ret["received_at"], gran)).groupby(["b", "settlement"], sort=True).agg(a=("refund_amount_cents", "sum"), c=("_cost", "sum"), n=("return_id", "size"))
        rec_b = po.assign(b=batch_end_dates(po["received_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        po_paid = po[po["paid_at"].notna()]
        ap_b = po_paid.assign(b=batch_end_dates(po_paid["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        base_inv = inv[~inv["_pulled"] & (inv["_kind"] != "round_trip")]
        rev_m = np.bincount(_month_index(base_inv["invoice_date"], cal.start), weights=base_inv["amount_cents"], minlength=len(months))
        freeze = state.extras["allowance_freeze_period"]
        events: list[tuple[dt.date, int, Any]] = []

        def month_rows(df: pd.DataFrame, ms: dt.date, me: dt.date) -> pd.DataFrame:
            lvl = df.index.get_level_values(0)
            return df[(lvl >= pd.Timestamp(ms)) & (lvl <= pd.Timestamp(me))]

        prev_payroll = prev_opex = 0
        tax_due = 0
        revolver = 0
        term = debt
        amort_q = cents(debt * 0.025)
        allowance_frozen_at: int | None = None
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            period = cal.labels([ms])[0]
            events = []
            if m > 0:
                pp, po_ = prev_payroll, prev_opex
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=pp: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=po_: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex and freight paid", source="opex")))
            if ms.month in (1, 4, 7, 10) and tax_due > 0:
                events.append((ms + dt.timedelta(days=14), 1, lambda d, a=tax_due: L.pay_tax(d, a)))
                tax_due = 0
            for b, r in month_rows(rec_b, ms, me).iterrows():
                dim = {"batch": f"purchasing:{b.date()}", "rows": str(int(r.n))}
                events.append((b.date(), 2, lambda d, a=int(r.a), dm=dim: L.purchase_inventory(d, a, memo="Inventory receipts (batch)", dim=dm)))
            freight_m = 0
            for b, r in month_rows(inv_b, ms, me).iterrows():
                dim = {"batch": f"sales:{b.date()}", "rows": str(int(r.n))}
                events.append((b.date(), 3, lambda d, a=int(r.a), dm=dim: L.sell_on_credit(d, a, memo="Sales invoiced (batch)", dim=dm)))
            for b, r in month_rows(cogs_b, ms, me).iterrows():
                dim = {"batch": f"shipments:{b.date()}", "rows": str(int(r.n))}
                freight_m += int(r.f)
                events.append((b.date(), 3, lambda d, a=int(r.c), dm=dim: L.relieve_inventory_cogs(d, a, memo="COGS on shipments (batch)", dim=dm)))
                events.append((b.date(), 3, lambda d, a=int(r.f), dm=dim: L.adjust(d, [("5030", a, 0, dm), ("2100", 0, a, dm)], "Carrier freight accrued (batch)", "fulfillment")))
            for b, r in month_rows(pay_b, ms, me).iterrows():
                dim = {"batch": f"collections:{b.date()}", "rows": str(int(r.n))}
                lines = [("1000", int(r.a - r.fee), 0, dim), ("5050", int(r.fee), 0, dim), ("1100", 0, int(r.a), dim)]
                events.append((b.date(), 4, lambda d, ln=lines: L.adjust(d, ln, "Customer payments (batch, card fees netted)", "collections")))
            for (b, kind), r in month_rows(ret_b, ms, me).iterrows():
                dim = {"batch": f"returns:{b.date()}", "rows": str(int(r.n))}
                acct = "1100" if kind == "credit_memo" else "1000"
                events.append((b.date(), 4, lambda d, a=int(r.a), c=int(r.c), ac=acct, dm=dim: L.returns_allowance(
                    d, a, settle_account=ac, restock_cost=c, memo="Returns: credit memos" if ac == "1100" else "Returns: refunds", dim=dm)))
            for b, r in month_rows(ap_b, ms, me).iterrows():
                dim = {"batch": f"payables:{b.date()}", "rows": str(int(r.n))}
                events.append((b.date(), 5, lambda d, a=int(r.a), dm=dim: L.pay_ap(d, a, memo="Vendor payments (batch)", dim=dm)))
            run_rate = float(rev_m[max(0, m - 2): m + 1].mean()) if m > 0 else float(rev_m[0])
            # opex sized so the baseline operating margin lands near ``op_margin`` after freight, card fees, returns and D&A
            opex = cents(run_rate * (p["gm"] - p["freight_pct"] - CARD_FEE * p["direct_share"] - 0.6 * p["return_rate_base"]
                                     - 0.55 * p["capex_pct"] - p["op_margin"]) * self.period_noise(ctx, state).ox(ms))  # review C2: opex noise
            payroll = cents(opex * 0.62)
            other = opex - payroll
            pr = dict(zip(PAYROLL_SPLIT, split_cents(payroll, list(PAYROLL_SPLIT.values())), strict=True))
            ox = dict(zip(OPEX_SPLIT, split_cents(other, list(OPEX_SPLIT.values())), strict=True))
            events.append((ms + dt.timedelta(days=14), 6, lambda d, a=cents(run_rate * 12 * p["capex_pct"] / 12): L.capex(d, a)))
            events.append((me, 7, lambda d, s=pr: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox: L.accrue_opex(d, s)))
            events.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 96))))
            frozen = period >= freeze

            def provision(d: dt.date, frozen: bool = frozen) -> None:
                nonlocal allowance_frozen_at
                bal = L.closing_balances()
                if frozen:
                    if allowance_frozen_at is None:
                        allowance_frozen_at = bal["1110"]
                    return
                L.bad_debt_provision(d, cents(bal["1100"] * p["allowance_pct"]) + bal["1110"])

            events.append((me, 8, provision))
            rv, tm = revolver, term
            events.append((me, 9, lambda d, a=cents(rv * p["interest_rate"] / 12 + tm * p["interest_rate"] / 12): L.accrue_interest(d, a)))
            if ms.month % 3 == 0:
                events.append((me, 10, lambda d: L.pay_interest(d, -L.closing_balances()["2150"])))
                events.append((me, 10, lambda d: (L.repay(d, amort_q, debt_account="2600", memo="Term debt amortisation"),
                                                  L.reclass(d, amort_q, "2610", "2600", memo="Reclass current portion of term debt"))))

                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))

                events.append((me, 11, tax))
            events.sort(key=lambda e: (e[0], e[1]))
            for d, _prio, fn in events:
                if d <= end_date:
                    fn(d)
            if ms.month % 3 == 0:
                term -= amort_q
                tax_due = -L.closing_balances()["2400"]
                # dividend policy: distribute cash above ~1.5x the target balance (keeps the balance sheet from piling up cash)
                cash_now = L.closing_balances()["1000"]
                target_cash = p["cash_pct"] * run_rate * 12
                if cash_now > 1.5 * target_cash and revolver == 0:
                    L.dividend(me, int((cash_now - target_cash) // 1_000_000 * 1_000_000), memo="Quarterly dividend")
            prev_payroll, prev_opex = payroll, other + freight_m
            cash = L.closing_balances()["1000"]
            floor = 0.6 * opex
            if cash < floor:
                draw = int(math.ceil((1.2 * opex - cash) / 1_000_000) * 1_000_000)
                L.borrow(me, draw, memo="Revolver draw")
                revolver += draw
            elif cash > 2.5 * opex and revolver > 0:
                rep = min(revolver, int(math.floor((cash - 1.5 * opex) / 1_000_000) * 1_000_000))
                if rep > 0:
                    L.repay(me, rep, memo="Revolver repayment")
                    revolver -= rep

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Clean frames + the difficulty's dirt rules (2 / 4 / 6)."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("tables").generator
        customers = state.frames["customers"].copy()
        first = state.frames["orders"].groupby("customer_id")["order_ts"].min()
        customers["created_at"] = customers["customer_id"].map(first).fillna(pd.Timestamp(cal.start)) - pd.to_timedelta(g.integers(30, 600, len(customers)), unit="D")
        sup = state.frames["suppliers"].copy()
        sup["created_at"] = pd.Timestamp(cal.start) - pd.to_timedelta(g.integers(100, 900, len(sup)), unit="D")
        if "rt" in state.extras:
            # the related pair is "newer": both onboarded within weeks of each other
            mid = pd.Timestamp(cal.bounds(state.periods[3])[1])
            customers.loc[customers["customer_id"] == state.extras["rt"]["customer_id"], "created_at"] = mid
            sup.loc[sup["supplier_id"] == state.extras["rt"]["supplier_id"], "created_at"] = mid - pd.Timedelta(days=11)
        products = state.frames["products"].copy()
        if "rt" in state.extras:
            products = pd.concat([products, pd.DataFrame([{"product_id": "P-KIT", "sku": "KIT-RESALE", "title": "Component kit (resale)",
                                                            "category": "Kits", "list_price_cents": 125_000, "supplier_id": state.extras["rt"]["supplier_id"],
                                                            "active": True, "unit_cost_cents": 123_800}])], ignore_index=True)
        orders = state.frames["orders"].drop(columns=["_kind", "_pulled"])
        orders["updated_at"] = orders["created_at"]
        inv = state.frames["invoices"].drop(columns=["_kind", "_pulled", "_cost", "_channel"])
        sh = state.frames["shipments"].drop(columns=["_cost"])
        ret = state.frames["returns"].drop(columns=["_cost", "_invoice", "_stuffed"])
        po = state.frames["purchase_orders"][["po_id", "supplier_id", "description", "ordered_at", "received_at", "amount_cents", "status", "paid_at"]]
        dirt_plan = [
            ("customers", "case_mixed:region"), ("orders", "nulls:sales_rep_id:0.06"),
            ("invoices", "dup_keys:0.01"), ("shipments", "tz_mixed:delivered_at"),
            ("customers", "whitespace:tax_id"), ("suppliers", "case_mixed:tax_id"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        money = {c: "BIGINT" for c in ("amount_cents", "subtotal_cents", "discount_cents", "total_cents", "unit_price_cents", "unit_cost_cents",
                                        "line_total_cents", "freight_cost_cents", "refund_amount_cents", "credit_limit_cents", "list_price_cents",
                                        "value_cents")}
        money.update({"quantity": "INTEGER", "terms_days": "INTEGER", "payment_terms_days": "INTEGER", "invoice_date": "DATE", "due_date": "DATE"})
        builder.add_frame("customers", customers, primary_key=["customer_id"], foreign_keys=[], column_types=money, dirt=dirt.get("customers", ()),
                          descriptions={"channel": "wholesale (distributor, net terms) | direct (card at checkout)", "tax_id": "EIN on the W-9 / credit application"},
                          description="Customers: wholesale distributors and direct accounts.")
        builder.add_frame("suppliers", sup, primary_key=["supplier_id"], foreign_keys=[], column_types=money, dirt=dirt.get("suppliers", ()),
                          descriptions={"tax_id": "EIN from the vendor master"})
        builder.add_frame("products", products, primary_key=["product_id"], foreign_keys=[("supplier_id", "suppliers", "supplier_id")], column_types=money)
        builder.add_frame("orders", orders, primary_key=["order_id"], foreign_keys=[("customer_id", "customers", "customer_id")], column_types=money,
                          dirt=dirt.get("orders", ()), descriptions={"discount_pct": "order-level discount", "notes": "free-text order remarks"})
        builder.add_frame("order_items", state.frames["order_items"], primary_key=["order_item_id"],
                          foreign_keys=[("order_id", "orders", "order_id"), ("product_id", "products", "product_id")], column_types=money)
        builder.add_frame("shipments", sh, primary_key=["shipment_id"], foreign_keys=[("order_id", "orders", "order_id")], column_types=money,
                          dirt=dirt.get("shipments", ()), descriptions={"status": "delivered | in_transit | awaiting_release",
                                                                       "shipped_at": "carrier pickup (revenue recognition point); NULL while held"},
                          description="Outbound shipments (extract taken at quarter close; may include the first days of the next quarter).")
        builder.add_frame("invoices", inv, primary_key=["invoice_id"], foreign_keys=[("order_id", "orders", "order_id"), ("customer_id", "customers", "customer_id")],
                          column_types=money, dirt=dirt.get("invoices", ()),
                          descriptions={"invoice_date": "invoice issued; revenue recognised on this date", "ingest_id": "load sequence; >1 marks a re-ingested duplicate",
                                        "amount_cents": "net of discount"},
                          description="Customer invoices (AR sub-ledger), one per order.")
        builder.add_frame("payments", state.frames["payments"], primary_key=["payment_id"],
                          foreign_keys=[("invoice_id", "invoices", "invoice_id"), ("customer_id", "customers", "customer_id")], column_types=money,
                          description="Customer payments (card payments settle at checkout).")
        builder.add_frame("returns", ret, primary_key=["return_id"], foreign_keys=[("order_id", "orders", "order_id")], column_types=money,
                          descriptions={"settlement": "credit_memo (against an open invoice) | refund (cash)"})
        builder.add_frame("purchase_orders", po, primary_key=["po_id"], foreign_keys=[("supplier_id", "suppliers", "supplier_id")],
                          column_types={**money, "ordered_at": "DATE"}, descriptions={"paid_at": "vendor payment (NULL = unpaid)"})
        builder.add_frame("inventory_movements", state.frames["inventory_movements"], primary_key=["movement_id"], column_types={**money, "movement_id": "BIGINT"},
                          descriptions={"value_cents": "signed movement value at standard cost", "movement_type": "opening | receipt | issue | return_restock"})

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, inventory, AP and revenue ties hold; revenue vs shipped goods breaks by design (held / cut-off invoices)."""
        cal = state.calendar
        inv, pay, ret, po, sh, mv = (state.frames[k] for k in ("invoices", "payments", "returns", "purchase_orders", "shipments", "inventory_movements"))
        ship_at = inv["order_id"].map(sh.set_index("order_id")["shipped_at"])
        ar, invd, apd, rev, shipped_rev, brk = {}, {}, {}, {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + EOD
            issued = inv["invoice_date"] <= pd.Timestamp(e)
            cm = ret[(ret["settlement"] == "credit_memo") & (ret["received_at"] <= e_ts)]["refund_amount_cents"].sum()
            ar[period] = int(inv.loc[issued, "amount_cents"].sum() - pay.loc[pay["paid_at"] <= e_ts, "amount_cents"].sum() - cm)
            invd[period] = int(mv.loc[mv["movement_ts"] <= e_ts, "value_cents"].sum())
            rec = po["received_at"] <= e_ts
            unpaid = po["paid_at"].isna() | (po["paid_at"] > e_ts)
            apd[period] = int(po.loc[rec & unpaid, "amount_cents"].sum())
            in_p = issued & (inv["invoice_date"] >= pd.Timestamp(s))
            rev[period] = int(inv.loc[in_p, "amount_cents"].sum())
            ok = ship_at.notna() & (ship_at <= e_ts)
            shipped_rev[period] = int(inv.loc[in_p & ok, "amount_cents"].sum())
            brk[period] = shipped_rev[period] - rev[period]
        state.extras["unshipped_by_period"] = {k: -v for k, v in brk.items()}
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Invoices - payments - credit memos == GL 1100"),
            SubledgerTie("inventory_movements", invd, ["1200", "1210", "1220"], description="Sum of movement values == GL inventory"),
            SubledgerTie("ap_open_purchase_orders", apd, ["2000"], description="Received, unpaid POs == GL 2000"),
            SubledgerTie("revenue_invoices", rev, ["4000"], basis="activity", description="Invoices issued in period == GL 4000 activity"),
            SubledgerTie("revenue_vs_shipments", shipped_rev, ["4000"], basis="activity", expected_break_cents={k: v for k, v in brk.items() if v},
                         description="Invoices whose goods shipped by period end vs GL revenue: breaks by invoiced-not-shipped (designed)"),
        ]

    # ------------------------------------------------------------------ figures

    def _figs(self, state: WorldState) -> dict[str, Any]:
        """Statements over warm-up + reporting quarters, TTM Beneish, DSO and window facts (cached)."""
        if "figs" in state.extras:
            return state.extras["figs"]
        p, cal = state.params, state.calendar
        L = state.ledger
        allp = cal.all_periods
        full: FinancialStatements = build_statements(L, allp)
        inc = {i.period: i for i in full.income}
        bs = {b.period: b for b in full.balance}
        cf = {c.period: c for c in full.cash_flow}
        tb = {x: L.trial_balance(x) for x in allp}

        def bsl(period: str, caption: str) -> int:
            return next(ln.cents for ln in bs[period].lines if ln.caption == caption)

        def ttm(i: int) -> dict[str, float]:
            qs = allp[i - 3: i + 1]
            per = allp[i]
            act = [L.activity(x) for x in qs]
            sales = sum(inc[x].revenue_cents for x in qs)
            return {
                "sales": float(sales), "gp": float(sum(inc[x].gross_profit_cents for x in qs)),
                "sga": float(sum(a["6000"] + a["6010"] + a["6200"] + a["6210"] + a["6400"] + a["6500"] for a in act)),
                "dep": float(sum(a["6300"] for a in act)), "ni": float(sum(inc[x].net_income_cents for x in qs)),
                "cfo": float(sum(cf[x].operating_cents for x in qs)), "ar": float(bsl(per, "Accounts receivable, net")),
                "ca": float(bsl(per, "Total current assets")), "ppe": float(bsl(per, "Property, plant and equipment, net")),
                "ta": float(bs[per].total_assets_cents), "liab": float(bs[per].total_liabilities_cents),
            }

        ben: dict[str, dict[str, float]] = {}
        for i in range(7, len(allp)):
            ben[allp[i]] = _beneish(ttm(i), ttm(i - 4))
        inv = state.frames["invoices"]
        qrev, qwin = {}, {}
        for x in allp:
            s, e = cal.bounds(x)
            sel = (inv["invoice_date"] >= pd.Timestamp(s)) & (inv["invoice_date"] <= pd.Timestamp(e))
            win = sel & (inv["invoice_date"] > pd.Timestamp(e) - pd.Timedelta(days=WINDOW_DAYS))
            qrev[x] = float(inv.loc[sel, "amount_cents"].sum())
            qwin[x] = float(inv.loc[win, "amount_cents"].sum())
        f = {"full": full, "inc": inc, "bs": bs, "cf": cf, "tb": tb, "ben": ben, "qrev": qrev, "qwin": qwin,
             "dso": {x: tb[x]["1100"] / qrev[x] * cal.days(x) if qrev[x] else 0.0 for x in allp}}
        state.extras["figs"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Fulfilment stations, unit economics and public KPIs by period."""
        p, cal = state.params, state.calendar
        f = self._figs(state)
        inv, ret = state.frames["invoices"], state.frames["returns"]
        kpis = {}
        for x in state.periods:
            s, e = cal.bounds(x)
            sel = (inv["invoice_date"] >= pd.Timestamp(s)) & (inv["invoice_date"] <= pd.Timestamp(e))
            rsel = (ret["received_at"] >= pd.Timestamp(s)) & (ret["received_at"] <= pd.Timestamp(e) + EOD)
            r = state.statements.ratios[x]  # type: ignore[union-attr]
            kpis[x] = {"revenue_cents": float(f["inc"][x].revenue_cents), "net_income_cents": float(f["inc"][x].net_income_cents),
                       "operating_cash_flow_cents": float(f["cf"][x].operating_cents), "orders": float(sel.sum()),
                       "wholesale_revenue_share": float(inv.loc[sel & (inv["_channel"] == "wholesale"), "amount_cents"].sum() / max(1, inv.loc[sel, "amount_cents"].sum())),
                       "returns_cents": float(ret.loc[rsel, "refund_amount_cents"].sum()), "dso": r["dso"], "gross_margin": r["gross_margin"],
                       "guidance_revenue_cents": float(f["inc"][state.periods[3]].revenue_cents * (1 + p["guidance_qoq"]) ** max(0, state.periods.index(x) - 3))}
        daily = len(inv) / (len(state.extras["months"]) * 30.4)
        stations, summary = station_network(daily / 8.0, [{"name": "pick", "capacity_per_hour": daily / 8.0 / 0.7, "cs": 0.8},
                                                          {"name": "pack", "capacity_per_hour": daily / 8.0 / 0.6, "cs": 0.6},
                                                          {"name": "ship_dock", "capacity_per_hour": daily / 8.0 / 0.5, "cs": 1.0}])
        r8 = state.statements.ratios[state.periods[-1]]  # type: ignore[union-attr]
        unit = {"gross_margin": r8["gross_margin"], "arpa": float(inv["amount_cents"].mean()), "cac": 0.0, "ltv": 0.0, "payback_months": 0.0, "churn_monthly": 0.0}
        return build_operations_state(stations=stations, queueing={"throughput": summary["throughput"]}, unit_economics=unit,
                                      supply_chain={"distributors": p["n_distributors"], "wholesale_terms_days": p["wholesale_terms"]},
                                      pricing={"base_discount_levels": [0, 0.02, 0.03, 0.05], "card_fee": CARD_FEE}, kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        p, cal = state.params, state.calendar
        f = self._figs(state)
        P = state.periods
        P6, P7, P8 = P[5], P[6], P[7]
        inv, ret = state.frames["invoices"], state.frames["returns"]
        s8, e8 = cal.bounds(P8)
        pulled = inv[inv["_pulled"]]
        pulled8 = pulled[(pulled["invoice_date"] >= pd.Timestamp(s8))]
        # earlier pulled-forward invoices whose goods shipped in P8 belong to P8 on a restated basis
        ship_at = pulled["order_id"].map(state.frames["shipments"].set_index("order_id")["shipped_at"])
        shifted_in = pulled[(pulled["invoice_date"] < pd.Timestamp(s8)) & (ship_at >= pd.Timestamp(s8)) & (ship_at <= pd.Timestamp(e8) + EOD)]
        sel8 = (inv["invoice_date"] >= pd.Timestamp(s8)) & (inv["invoice_date"] <= pd.Timestamp(e8))
        by_cust = inv[sel8 & (inv["_channel"] == "wholesale")].groupby("customer_id")["amount_cents"].sum().sort_values(ascending=False)
        top5 = float(by_cust.head(5).sum() / inv.loc[sel8, "amount_cents"].sum())
        share = {x: f["qwin"][x] / f["qrev"][x] for x in P}
        # window orders of P7 (stuffing) - return rate observed in P8
        s7, e7 = cal.bounds(P7)
        w7 = inv[(inv["invoice_date"] > pd.Timestamp(e7) - pd.Timedelta(days=WINDOW_DAYS)) & (inv["invoice_date"] <= pd.Timestamp(e7)) & (inv["_channel"] == "wholesale")]
        r_by_order = ret.groupby("order_id")["refund_amount_cents"].sum()
        ret7 = float(w7["order_id"].map(r_by_order).fillna(0).sum() / max(1, w7["amount_cents"].sum()))
        nonw = inv[(inv["invoice_date"] >= pd.Timestamp(s7)) & (inv["invoice_date"] <= pd.Timestamp(e7) - pd.Timedelta(days=WINDOW_DAYS)) & (inv["_channel"] == "wholesale")]
        ret_nonw = float(nonw["order_id"].map(r_by_order).fillna(0).sum() / max(1, nonw["amount_cents"].sum()))
        unshipped = state.extras["unshipped_by_period"]
        allp = cal.all_periods
        i8 = allp.index(P8)
        ta_avg = (f["bs"][allp[i8 - 4]].total_assets_cents + f["bs"][P8].total_assets_cents) / 2
        ni_ttm = sum(f["inc"][x].net_income_cents for x in allp[i8 - 3: i8 + 1])
        cfo_ttm = sum(f["cf"][x].operating_cents for x in allp[i8 - 3: i8 + 1])
        tb8, tb_prev = f["tb"][P8], f["tb"][allp[allp.index(state.extras["allowance_freeze_period"]) - 1]]
        out = {
            "share": share, "share_p8": share[P8], "share_base": float(np.mean([share[x] for x in P[:6]])),
            "pulled_total": int(pulled["amount_cents"].sum()), "pulled_p8": int(pulled8["amount_cents"].sum()),
            "shifted_into_p8": int(shifted_in["amount_cents"].sum()),
            "pulled_by_q": {x: int(pulled[(pulled["invoice_date"] >= pd.Timestamp(cal.bounds(x)[0])) & (pulled["invoice_date"] <= pd.Timestamp(cal.bounds(x)[1]))]["amount_cents"].sum()) for x in P},
            "rev_p8": int(f["inc"][P8].revenue_cents), "gross_inv_p8": int(f["qrev"][P8]),
            "dso_p8": f["dso"][P8], "dso_p6": f["dso"][P6], "top5_p8": top5,
            "ret_window_p7": ret7, "ret_nonwindow_p7": ret_nonw,
            "unshipped_p8": int(unshipped.get(P8, 0)),
            "m_p8": f["ben"][P8]["m_score"], "m_p7": f["ben"][P7]["m_score"], "m_clean": f["ben"][P6]["m_score"],
            "ben_p8": f["ben"][P8], "ben_clean": f["ben"][P6],
            "sloan_p8": (ni_ttm - cfo_ttm) / ta_avg if ta_avg else 0.0,
            "cfo_ni_p8": f["cf"][P8].operating_cents / f["inc"][P8].net_income_cents if f["inc"][P8].net_income_cents else 0.0,
            "allow_pct_p8": -tb8["1110"] / tb8["1100"] if tb8["1100"] else 0.0,
            "allow_pct_before": -tb_prev["1110"] / tb_prev["1100"] if tb_prev["1100"] else 0.0,
        }
        if "rt" in state.extras:
            rt = state.extras["rt"]
            rt_inv = inv[inv["customer_id"] == rt["customer_id"]]
            po = state.frames["purchase_orders"]
            out["rt_sales"] = int(rt_inv["amount_cents"].sum())
            out["rt_purchases"] = int(po.loc[po["supplier_id"] == rt["supplier_id"], "amount_cents"].sum())
        state.extras["facts"] = out
        return out

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 4 key table computed from the generated books and sub-ledgers; reference SQL on the dirty DB."""
        p, cal = state.params, state.calendar
        f = self._figs(state)
        fx = self._facts(state)
        P = state.periods
        P6, P7, P8 = P[5], P[6], P[7]
        mechs = p["mechanisms"]
        variant = [m for m in mechs if m != "channel_stuffing"]
        metrics = [
            metric("last5day_revenue_share_p8", fx["share_p8"], "ratio",
                   f"Invoice amount dated in the last {WINDOW_DAYS} calendar days (5 business days) of {P8} / all {P8} invoice amount (deduplicated invoices).",
                   pillar="sql", tol_abs=0.03, label="Quarter-end (last 5 business days) revenue share, latest quarter",
                   hint="Bucket invoices by quarter and flag invoice_date > last_day_of_quarter - 7 days."),
            metric("last5day_revenue_share_baseline", fx["share_base"], "ratio", f"Mean of the same share over {P[0]}..{P[5]}.",
                   pillar="sql", tol_abs=0.02, label="Quarter-end revenue share, historical baseline"),
            metric("pulled_forward_revenue_cents", fx["pulled_total"], "cents",
                   "Sum of invoice amounts that were pulled forward: " + ", ".join(
                       {"channel_stuffing": f"quarter-end window orders to the top distributors on extended dating in {P7}-{P8}",
                        "bill_and_hold": f"{P8} invoices on goods held in our warehouse (awaiting release)",
                        "premature_cutoff": f"{P8} invoices shipped after quarter end"}[m] for m in mechs) + ".",
                   pillar="accounting_finance", tol_rel=0.08, label="Revenue pulled forward"),
            metric("restated_revenue_p8_cents", fx["rev_p8"] - fx["pulled_p8"] + fx["shifted_into_p8"], "cents",
                   f"Reported {P8} net revenue (GL) {fx['rev_p8']} minus the {P8} pulled-forward invoices {fx['pulled_p8']} plus earlier "
                   f"pulled-forward invoices whose goods actually shipped in {P8} {fx['shifted_into_p8']}.",
                   pillar="accounting_finance", tol_rel=0.03, label="Restated revenue, latest quarter"),
            metric("dso_p8_days", fx["dso_p8"], "days", f"Open AR per the invoice sub-ledger (= GL 1100) at {P8} end / gross invoiced revenue in {P8} x {cal.days(P8)} days.",
                   pillar="accounting_finance", tol_abs=3.0, label="DSO, latest quarter"),
            metric("dso_p6_days", fx["dso_p6"], "days", f"Same for {P6}.", pillar="accounting_finance", tol_abs=3.0, label="DSO, two quarters earlier"),
            metric("top5_distributor_revenue_share_p8", fx["top5_p8"], "ratio", f"Top-5 wholesale customers' {P8} invoice amount / all {P8} invoice amount.",
                   pillar="sql", tol_abs=0.03, label="Top-5 distributor share of revenue"),
            metric("beneish_m_score_p8", fx["m_p8"], "score",
                   f"Beneish 8-variable M on trailing-twelve-month flows ending {P8} vs the TTM ending {cal.all_periods[cal.all_periods.index(P8) - 4]} "
                   f"(stocks at period end): DSRI {fx['ben_p8']['dsri']:.2f}, GMI {fx['ben_p8']['gmi']:.2f}, SGI {fx['ben_p8']['sgi']:.2f}, "
                   f"TATA {fx['ben_p8']['tata']:.3f}; threshold -1.78.", pillar="accounting_finance", tol_abs=0.25, label="Beneish M-score (TTM), latest quarter"),
            metric("beneish_m_score_clean_p6", fx["m_clean"], "score",
                   f"Same model for the TTM ending {P6} vs a year earlier (no pull-forward in the window): DSRI {fx['ben_clean']['dsri']:.2f}, "
                   f"GMI {fx['ben_clean']['gmi']:.2f}, SGI {fx['ben_clean']['sgi']:.2f}, TATA {fx['ben_clean']['tata']:.3f}, LVGI {fx['ben_clean']['lvgi']:.2f}.",
                   pillar="accounting_finance", tol_abs=0.25, label="Beneish M-score (TTM), comparison quarter"),
            metric("sloan_accrual_ratio_p8", fx["sloan_p8"], "ratio", f"(TTM net income - TTM CFO) / average total assets ({cal.all_periods[cal.all_periods.index(P8) - 4]} and {P8}).",
                   pillar="accounting_finance", tol_abs=0.02, label="Sloan accrual ratio (TTM)"),
            metric("cfo_to_ni_p8", fx["cfo_ni_p8"], "x", f"{P8} cash from operations / {P8} net income.", pillar="accounting_finance", tol_abs=0.1,
                   label="CFO / net income, latest quarter"),
        ]
        if "channel_stuffing" in mechs:
            metrics.append(metric("return_rate_window_orders_p7", fx["ret_window_p7"], "ratio",
                                  f"Returned amount (returns.refund_amount_cents) on wholesale orders invoiced in the last {WINDOW_DAYS} days of {P7} / their invoiced amount; "
                                  f"compare {fx['ret_nonwindow_p7']:.1%} on the rest of {P7}.", pillar="python", tol_abs=0.04, label="Return rate on quarter-end window orders"))
        if variant:
            metrics.append(metric("invoiced_not_shipped_p8_cents", fx["unshipped_p8"], "cents",
                                  f"{P8} invoices whose shipment has shipped_at NULL or after {cal.bounds(P8)[1]}.", pillar="sql", tol_rel=0.05,
                                  label="Invoiced but not shipped at quarter end"))
        if "rt" in state.extras:
            metrics.append(metric("round_trip_revenue_cents", fx["rt_sales"], "cents",
                                  "Invoices to the customer whose tax id matches a supplier's (after trimming / case-folding).",
                                  pillar="sql", tol_rel=0.05, label="Revenue from the related-party round trip"))
        # ---------------- reference SQL
        dedupe = "SELECT * FROM invoices QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1"
        sql_share = f"""
WITH inv AS ({dedupe}),
d AS (
  SELECT period_of(invoice_date) AS period, SUM(amount_cents) AS rev,
         SUM(CASE WHEN invoice_date > last_day_of_quarter(invoice_date) - INTERVAL {WINDOW_DAYS} DAY THEN amount_cents ELSE 0 END) AS rev_last5
  FROM inv GROUP BY 1)
SELECT period, rev, rev_last5 * 1.0 / rev AS last5_share,
       LAG(rev_last5 * 1.0 / rev) OVER (ORDER BY period) AS prev_share,
       LAG(rev_last5 * 1.0 / rev, 4) OVER (ORDER BY period) AS same_q_last_year
FROM d ORDER BY period"""

        def share_checks(df: pd.DataFrame) -> dict[str, Any]:
            s = df.set_index("period")["last5_share"]
            return {"share_p8": float(s.get(P8, 0.0)), "share_baseline": float(s.reindex(P[:6]).mean()),
                    "p8_vs_baseline_ratio": float(s.get(P8, 0.0) / max(1e-9, s.reindex(P[:6]).mean()))}

        q_share = run_reference_query(con, "quarter_end_loading_lag", sql_share, signature=["over(", "lag(", "qualify", "case when", "group by"],
                                      metric_keys=["last5day_revenue_share_p8", "last5day_revenue_share_baseline"], checks=share_checks)
        sql_conc = f"""
WITH inv AS ({dedupe}),
q AS (
  SELECT period_of(i.invoice_date) AS period, i.customer_id, SUM(i.amount_cents) AS rev
  FROM inv i JOIN customers c ON c.customer_id = i.customer_id
  WHERE TRIM(LOWER(c.channel)) = 'wholesale' GROUP BY 1, 2),
r AS (SELECT period, customer_id, rev, ROW_NUMBER() OVER (PARTITION BY period ORDER BY rev DESC) AS rk FROM q),
t AS (SELECT period_of(invoice_date) AS period, SUM(amount_cents) AS total FROM inv GROUP BY 1)
SELECT r.period, SUM(CASE WHEN rk <= 5 THEN rev ELSE 0 END) * 1.0 / MAX(t.total) AS top5_share
FROM r JOIN t USING (period) GROUP BY 1 ORDER BY 1"""
        q_conc = run_reference_query(con, "top5_distributor_share_by_quarter", sql_conc, signature=["row_number(", "partition by", "with", "join", "group by"],
                                     metric_keys=["top5_distributor_revenue_share_p8"],
                                     checks=lambda df: {"top5_share_p8": float(df.set_index("period")["top5_share"].get(P8, 0.0))})
        sql_unship = f"""
WITH inv AS ({dedupe})
SELECT period_of(i.invoice_date) AS period, COUNT(*) AS invoices, SUM(i.amount_cents) AS unshipped_cents
FROM inv i JOIN orders o USING (order_id)
LEFT JOIN shipments s ON s.order_id = o.order_id
WHERE s.shipped_at IS NULL OR CAST(s.shipped_at AS DATE) > last_day_of_quarter(i.invoice_date)
GROUP BY 1 ORDER BY 1"""
        sql_returns = f"""
WITH inv AS ({dedupe}),
w AS (
  SELECT i.order_id, period_of(i.invoice_date) AS period, i.amount_cents,
         CASE WHEN i.invoice_date > last_day_of_quarter(i.invoice_date) - INTERVAL {WINDOW_DAYS} DAY THEN 'window' ELSE 'rest' END AS bucket
  FROM inv i JOIN customers c ON c.customer_id = i.customer_id WHERE TRIM(LOWER(c.channel)) = 'wholesale'),
r AS (SELECT order_id, SUM(refund_amount_cents) AS returned FROM returns GROUP BY 1)
SELECT period, bucket, SUM(amount_cents) AS invoiced, SUM(COALESCE(returned, 0)) AS returned,
       SUM(COALESCE(returned, 0)) * 1.0 / SUM(amount_cents) AS return_rate
FROM w LEFT JOIN r USING (order_id) GROUP BY 1, 2 ORDER BY 1, 2"""
        sql_dso = """
WITH gross AS (
  SELECT j.period,
         SUM(CASE WHEN l.account_code = '1100' THEN l.debit_cents - l.credit_cents ELSE 0 END) AS d_ar,
         SUM(CASE WHEN l.account_code = '1110' THEN l.credit_cents - l.debit_cents ELSE 0 END) AS d_allow,
         SUM(CASE WHEN l.account_code = '4000' THEN l.credit_cents - l.debit_cents ELSE 0 END) AS invoiced
  FROM gl_lines l JOIN gl_journal j USING (entry_id) GROUP BY 1)
SELECT period, invoiced,
       SUM(d_ar) OVER (ORDER BY period) AS ar_gross,
       SUM(d_allow) OVER (ORDER BY period) AS allowance,
       SUM(d_allow) OVER (ORDER BY period) * 1.0 / SUM(d_ar) OVER (ORDER BY period) AS allowance_pct_ar,
       SUM(d_ar) OVER (ORDER BY period) * 1.0 / NULLIF(invoiced, 0) * 91 AS dso_days
FROM gross ORDER BY period"""
        sql_cfo = """
WITH flows AS (
  SELECT j.period,
         SUM(CASE WHEN l.account_code LIKE '4%' OR l.account_code LIKE '5%' OR l.account_code LIKE '6%'
                   OR l.account_code LIKE '7%' OR l.account_code = '8000' THEN l.credit_cents - l.debit_cents ELSE 0 END) AS net_income,
         SUM(CASE WHEN l.account_code = '1000' AND j.source NOT IN ('financing', 'capex', 'opening') THEN l.debit_cents - l.credit_cents ELSE 0 END) AS operating_cash
  FROM gl_lines l JOIN gl_journal j USING (entry_id) GROUP BY 1)
SELECT period, net_income, operating_cash,
       SUM(net_income) OVER (ORDER BY period ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS ni_ttm,
       SUM(operating_cash) OVER (ORDER BY period ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS cash_ttm
FROM flows ORDER BY period"""
        mech_ids = state.extras["mech_ids"]
        anomalies = {a.id: a for a in state.anomalies}
        q_dso = run_reference_query(con, "receivables_and_allowance_running_balance", sql_dso, signature=["over(", "with", "join", "case when", "group by"],
                                    metric_keys=["dso_p8_days", "dso_p6_days"],
                                    checks=lambda df: {"allowance_pct_ar_last": float(df["allowance_pct_ar"].iloc[-1]), "dso_last": float(df["dso_days"].iloc[-1])})
        q_cfo = run_reference_query(con, "net_income_vs_operating_cash_ttm", sql_cfo, signature=["over(", "with", "join", "case when", "group by"],
                                    metric_keys=["sloan_accrual_ratio_p8", "cfo_to_ni_p8", "beneish_m_score_p8"],
                                    checks=lambda df: {"ni_minus_cash_ttm_last": float(df["ni_ttm"].iloc[-1] - df["cash_ttm"].iloc[-1])})
        for k, mech in enumerate(mechs):
            aid = mech_ids[mech]
            if mech == "channel_stuffing":
                q_ret = run_reference_query(con, "return_rate_window_vs_rest", sql_returns, signature=["with", "left join", "case when", "coalesce(", "group by"],
                                            metric_keys=["return_rate_window_orders_p7"],
                                            checks=lambda df: {"window_rate_p7": float(df[(df["period"] == P7) & (df["bucket"] == "window")]["return_rate"].sum()),
                                                               "rest_rate_p7": float(df[(df["period"] == P7) & (df["bucket"] == "rest")]["return_rate"].sum())})
                refs = [q_share, q_conc, q_ret]
                trail = [f"Quarter-end share of revenue jumped to {fx['share'][P7]:.0%} ({P7}) and {fx['share_p8']:.0%} ({P8}) vs ~{fx['share_base']:.0%} historically.",
                         f"Top-5 distributors took {fx['top5_p8']:.0%} of {P8} revenue; window invoices carry {p['wholesale_terms'] + p['stuffed_terms_extra']}-day terms and a quarter-end discount.",
                         f"Window orders from {P7} came back at {fx['ret_window_p7']:.0%} vs {fx['ret_nonwindow_p7']:.0%} for the rest of the quarter; regular orders from the same distributors dropped in {P8}."]
                mag = {"pulled_forward_cents": float(fx["pulled_by_q"][P7] + fx["pulled_by_q"][P8]), "last5day_share_p8": round(fx["share_p8"], 6)}
            else:
                q_un = run_reference_query(con, "invoiced_not_shipped_by_quarter", sql_unship, signature=["qualify", "left join", "join", "group by", "sum("],
                                           metric_keys=["invoiced_not_shipped_p8_cents"],
                                           checks=lambda df: {"unshipped_p8": float(df.set_index("period")["unshipped_cents"].get(P8, 0.0))})
                refs = [q_un, q_share] if k == 0 else [q_un, q_conc]
                trail = [f"{P8} invoices worth {fx['unshipped_p8'] / 100:,.0f} USD have no shipment by quarter end"
                         + (" (shipments.status='awaiting_release', hold notes from sales)." if mech == "bill_and_hold" else " (they ship in the first days of the next quarter)."),
                         f"{P8} gross margin {state.statements.ratios[P8]['gross_margin']:.1%} vs {state.statements.ratios[P6]['gross_margin']:.1%} in {P6}: revenue booked without its COGS."]  # type: ignore[union-attr]
                mag = {"invoiced_not_shipped_p8_cents": float(fx["unshipped_p8"])}
            anomalies[aid] = anomalies[aid].model_copy(update={"reference_sql": refs, "evidence_trail": trail,
                                                               "magnitude": {**anomalies[aid].magnitude, **mag}})
        if "round_trip" in mech_ids:
            sql_rt = """
WITH c AS (SELECT customer_id, name, UPPER(TRIM(tax_id)) AS tax_key FROM customers),
s AS (SELECT supplier_id, name, UPPER(TRIM(tax_id)) AS tax_key FROM suppliers)
SELECT c.customer_id, c.name AS customer_name, s.supplier_id, s.name AS supplier_name, c.tax_key
FROM c JOIN s USING (tax_key)"""
            q_rt = run_reference_query(con, "customer_supplier_tax_id_match", sql_rt, signature=["with", "join"], metric_keys=["round_trip_revenue_cents"],
                                       checks=lambda df: {"matched_pairs": int(len(df))})
            sql_rt2 = f"""
WITH pair AS (
  SELECT c.customer_id, s.supplier_id FROM customers c JOIN suppliers s ON UPPER(TRIM(c.tax_id)) = UPPER(TRIM(s.tax_id))),
sales AS (
  SELECT date_trunc('month', i.invoice_date) AS month, SUM(i.amount_cents) AS sales_cents
  FROM ({dedupe}) i JOIN pair USING (customer_id) GROUP BY 1),
buys AS (
  SELECT date_trunc('month', p.received_at) AS month, SUM(p.amount_cents) AS purchases_cents
  FROM purchase_orders p JOIN pair USING (supplier_id) GROUP BY 1)
SELECT month, sales_cents, purchases_cents, sales_cents - purchases_cents AS margin_cents
FROM sales JOIN buys USING (month) ORDER BY month"""
            q_rt2 = run_reference_query(con, "round_trip_sales_vs_purchases_by_month", sql_rt2, signature=["with", "join", "date_trunc(", "group by"],
                                        metric_keys=["round_trip_revenue_cents"],
                                        checks=lambda df: {"months": int(len(df)), "margin_pct": float(df["margin_cents"].sum() / max(1, df["sales_cents"].sum()))})
            aid = mech_ids["round_trip"]
            anomalies[aid] = anomalies[aid].model_copy(update={
                "reference_sql": [q_rt, q_rt2],
                "magnitude": {**anomalies[aid].magnitude, "round_trip_revenue_cents": float(fx["rt_sales"]), "round_trip_purchases_cents": float(fx["rt_purchases"])},
                "evidence_trail": ["A distributor and a supplier share the same tax id and street address (after trimming / case-folding).",
                                   f"Every month the company buys from one and sells to the other at ~{(fx['rt_sales'] / max(1, fx['rt_purchases']) - 1):.1%} markup."]})
        al = state.extras["allowance_id"]
        anomalies[al] = anomalies[al].model_copy(update={
            "reference_sql": [q_dso, q_cfo],
            "magnitude": {"allowance_pct_ar_p8": round(fx["allow_pct_p8"], 6), "allowance_pct_ar_before": round(fx["allow_pct_before"], 6),
                          "sloan_accrual_ratio_p8": round(fx["sloan_p8"], 6), "beneish_m_score_p8": round(fx["m_p8"], 6)},
            "evidence_trail": [f"DSO rose from {fx['dso_p6']:.0f} days ({P6}) to {fx['dso_p8']:.0f} ({P8}) while the allowance stayed at the same dollar amount "
                               f"({fx['allow_pct_before']:.1%} -> {fx['allow_pct_p8']:.1%} of gross AR).",
                               f"TTM net income exceeds TTM operating cash flow; Sloan accrual ratio {fx['sloan_p8']:.3f}; Beneish M {fx['m_p8']:.2f} "
                               f"vs {fx['m_clean']:.2f} in {P6}."]})
        ordered = [anomalies[k] for k in sorted(anomalies, key=lambda s: int(s[1:]))]
        herrings_all = [
            "A new sales promotion drove real demand - discounts exist, but the volume sits with a handful of distributors in the last days of the quarter and reverses next quarter.",
            f"It is seasonality - the same quarter a year earlier had a quarter-end share of {fx['share'][P[3]]:.0%} vs {fx['share_p8']:.0%} now.",
            ("A logistics backlog at the new 3PL delayed shipments - the held orders say 'awaiting release' and carriers had capacity; the 3PL "
             "onboarding did slow some deliveries a few days, but delivered_at, not shipped_at, moved." if variant else
             "A logistics backlog at the new 3PL explains the returns - delivery times slipped a few days, but returns concentrate on window orders with reason 'overstock'."),
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        mech_txt = " and ".join(MECH_LABEL[m] for m in mechs)
        root = (f"Under pressure to meet raised guidance the company pulled revenue forward by {mech_txt}: {fx['pulled_total'] / 100:,.0f} USD of invoices "
                f"({fx['pulled_p8'] / max(1, fx['rev_p8']):.1%} of {P8} reported revenue) were recognised before an economic sale occurred. "
                f"Quarter-end loading jumped to {fx['share_p8']:.0%} of the quarter vs ~{fx['share_base']:.0%} historically, DSO rose from {fx['dso_p6']:.0f} to "
                f"{fx['dso_p8']:.0f} days while the allowance was held flat, and TTM accruals pushed the Beneish M-score to {fx['m_p8']:.2f} (vs {fx['m_clean']:.2f} in {P6}). "
                + (f"A related distributor/supplier pair adds round-tripped revenue of {fx.get('rt_sales', 0) / 100:,.0f} USD at near-zero margin. " if "rt" in state.extras else "")
                + f"Restated {P8} revenue is {(fx['rev_p8'] - fx['pulled_p8'] + fx['shifted_into_p8']) / 100:,.0f} USD; continuing the practice requires an ever larger pull-forward.")
        python_ref = (
            "import pandas as pd, numpy as np\n"
            "inv = pd.read_sql('SELECT * FROM invoices', con).sort_values('ingest_id').drop_duplicates('invoice_id')\n"
            "inv['q'] = pd.PeriodIndex(inv.invoice_date, freq='Q')\n"
            "qend = inv.q.dt.end_time.dt.normalize()\n"
            "inv['window'] = (qend - pd.to_datetime(inv.invoice_date)).dt.days < 7\n"
            "print(inv.groupby('q').apply(lambda d: d.loc[d.window, 'amount_cents'].sum() / d.amount_cents.sum()))\n"
            "# Beneish M (TTM vs TTM a year earlier) from the GL: DSRI, GMI, AQI, SGI, DEPI, SGAI, TATA, LVGI\n"
            "# M = -4.84 + .920*DSRI + .528*GMI + .404*AQI + .892*SGI + .115*DEPI - .172*SGAI + 4.679*TATA - .327*LVGI\n"
            "# Benford first-digit test on invoices.amount_cents; two-sample test of return rates window vs rest (scipy.stats)\n"
        )
        sql_refs = [q.sql for a in ordered for q in a.reference_sql]
        metrics = _with_hints(metrics, _HINTS)
        return GroundTruth(
            root_cause=root, anomalies=ordered, red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=list(dict.fromkeys(sql_refs)), python=[python_ref],
                memo_bullets=[
                    "Net income is growing faster than operating cash flow and receivables faster than revenue - the first-order warning sign.",
                    f"Quarter-end loading: last-5-business-day share {fx['share_p8']:.0%} vs ~{fx['share_base']:.0%} history; concentrated in a few distributors.",
                    "Transaction-level evidence (terms, discounts, hold notes / ship dates, post-quarter returns) is primary; M-score and Sloan corroborate.",
                    f"Restate: remove {fx['pulled_total'] / 100:,.0f} USD of pulled-forward revenue; true up the allowance.",
                    "Stopping the practice means a visible revenue air pocket next quarter; continuing it requires a bigger pull-forward each quarter.",
                    "Fix the incentive: quarter-end promotions and extended dating need finance approval; enforce ASC 606 bill-and-hold criteria and cutoff testing.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, four stakeholders and the spec timeline (red herrings flagged)."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._figs(state)
        fx = self._facts(state)
        names = rng.pick_names(4)
        r8 = state.statements.ratios[P[7]]  # type: ignore[union-attr]
        reveal = [
            f"Management raised guidance in {P[4]} to roughly {p['guidance_qoq']:.0%} growth a quarter.",
            f"{P[7]} is the best quarter in the company's history: revenue {f['inc'][P[7]].revenue_cents / 1e8:,.1f}M USD, up {r8['revenue_growth_yoy']:+.0%} year over year.",
            "Sales ran a quarter-end promotion for distributors.",
            f"The CFO describes DSO ({r8['dso']:.0f} days) as 'slightly up due to enterprise terms'.",
        ]
        if p["difficulty"] != "principal":
            reveal.append("The external auditor sent a question about year-end shipping documents.")
        conceal = [
            "channel stuffing", "bill-and-hold", "premature cutoff", "round-tripping", "revenue pulled forward from the next quarter",
            "the quarter-end window share of revenue", "distributor concentration in the last days of the quarter", "hold notes on orders",
            "the related-party customer and supplier", "allowance held flat",
            *[a.id for a in state.anomalies],
        ]
        stakeholders = [
            Stakeholder(name=names[0], role="CFO", agenda="Hit guidance and keep the stock up.", disposition="polished, defensive",
                        knows=["the quarter-end push", "terms on the big distributor orders"], hides=["extended dating and hold arrangements"]),
            Stakeholder(name=names[1], role="VP Sales", agenda="Earn the accelerator on the quarter.", disposition="combative",
                        knows=["organised the quarter-end promotion"], hides=["side letter granting return rights to distributors"]),
            Stakeholder(name=names[2], role="Controller", agenda="Get through the audit.", disposition="uneasy",
                        knows=["the quarter-end shipping documentation was aggressive"], hides=["will only tell if asked about specific invoices"]),
            Stakeholder(name=names[3], role="Audit Committee Chair", agenda="Assess whether the revenue is real before the 10-Q.", disposition="skeptical",
                        knows=["the stock fell after the earlier miss"], hides=[]),
        ]
        tl = [
            TimelineEvent(period=P[4], headline="Guidance raised", detail="Management guides to accelerating growth."),
            TimelineEvent(period=P[5], headline="Missed by 1%; stock -12%", detail="The quarter came in just short of guidance."),
            TimelineEvent(period=P[6], headline="Quarter-end promotion launched", detail="Distributor incentive program for the last weeks of the quarter."),
            TimelineEvent(period=P[7], headline="Record quarter", detail="Revenue and EPS above guidance."),
            TimelineEvent(period=P[7], headline="Distributor asks for a return authorization", detail="A large distributor requests an RA for slow-moving stock."),
        ]
        if p["difficulty"] != "principal":
            tl.append(TimelineEvent(period=P[7], headline="Auditor inquiry", detail="The audit team asks for shipping documents around quarter end."))
        tl.append(TimelineEvent(period=P[6], headline="New 3PL onboarding delays", detail="Delivery times slip a few days during the 3PL transition.", is_red_herring=True))
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[3], headline="Strong seasonal quarter last year", detail="Last year's same quarter was also strong.", is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[5], headline="Marketing launches a new product promotion", detail="Broad-based price promotion.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        artifacts = ["earnings_call_excerpt", "promotion_email", "ar_aging_summary", "board_deck_excerpt"]
        if any(m != "channel_stuffing" for m in p["mechanisms"]):
            artifacts.append("hold_request_letter")
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl, artifacts=artifacts, architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Spec levers; defaults are the status quo (practice continues, nothing restated)."""
        return [
            Lever(key="restate", label="Restate the pulled-forward quarters (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Removes the pull-forward from reported revenue; litigation reserve 1-3% of revenue; credibility recovers."),
            Lever(key="stop_quarter_end_promotions", label="Stop quarter-end promotions and extended dating (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Next quarter loses the pull-forward plus distributor over-inventory; DSO normalises over two quarters."),
            Lever(key="accept_returns_pct", label="Accept distributor returns", min=0, max=100, default=0, unit="%",
                  effect="Returns realised now (revenue and AR down) instead of later; distributor relationship improves."),
            Lever(key="allowance_true_up_pct", label="Allowance true-up", min=0, max=5, default=0, unit="% of AR", effect="Bad-debt expense now; allowance back to policy."),
            Lever(key="continue_practice", label="Keep pulling revenue forward (0/1)", min=0, max=1, default=1, unit="bool",
                  effect="Next quarter needs ~1.3x the pull-forward to show growth, until distributor inventory caps out."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model (module docstring)."""
        p = state.params
        f = self._figs(state)
        fx = self._facts(state)
        P8 = state.periods[-1]
        tb8 = f["tb"][P8]
        rev = float(f["inc"][P8].revenue_cents)
        pf = float(fx["pulled_p8"]) or float(fx["pulled_total"])
        gm = state.statements.ratios[P8]["gross_margin"]  # type: ignore[union-attr]
        ar = float(tb8["1100"])
        stuffed_open = float(state.frames["invoices"].query("_pulled")["amount_cents"].sum())
        overinv = p["order_drop"] * pf if "channel_stuffing" in p["mechanisms"] else 0.0
        kpis = {
            "reported_revenue_cents": rev, "economic_revenue_cents": rev - pf, "pulled_forward_cents": pf,
            "net_income_cents": float(f["inc"][P8].net_income_cents), "cash_cents": float(tb8["1000"]), "ar_cents": ar,
            "dso_days": fx["dso_p8"], "allowance_cents": float(-tb8["1110"]), "litigation_reserve_cents": 0.0,
            "credibility_index": 0.5, "stock_price_index": 100.0, "distributor_inventory_months": min(p["inventory_cap_months"], 1.0 + pf / max(1.0, rev / 3)),
        }
        dso_norm = (fx["dso_p8"] - fx["dso_p6"]) / 2
        effects = [
            {"lever": "restate", "kpi": "reported_revenue_cents", "delta_per_unit": -pf, "lag_periods": 0},
            {"lever": "restate", "kpi": "net_income_cents", "delta_per_unit": -(pf * gm + p["litigation_pct"] * rev), "lag_periods": 0},
            {"lever": "restate", "kpi": "litigation_reserve_cents", "delta_per_unit": p["litigation_pct"] * rev, "lag_periods": 0},
            {"lever": "restate", "kpi": "credibility_index", "delta_per_unit": 0.3, "lag_periods": 0},
            {"lever": "restate", "kpi": "stock_price_index", "delta_per_unit": -15.0, "lag_periods": 0},
            {"lever": "stop_quarter_end_promotions", "kpi": "reported_revenue_cents", "delta_per_unit": -(pf + overinv), "lag_periods": 0},
            {"lever": "stop_quarter_end_promotions", "kpi": "pulled_forward_cents", "delta_per_unit": -pf, "lag_periods": 0},
            {"lever": "stop_quarter_end_promotions", "kpi": "dso_days", "delta_per_unit": -dso_norm, "lag_periods": 0},
            {"lever": "stop_quarter_end_promotions", "kpi": "dso_days", "delta_per_unit": -dso_norm, "lag_periods": 1},
            {"lever": "stop_quarter_end_promotions", "kpi": "distributor_inventory_months", "delta_per_unit": -0.5, "lag_periods": 1},
            {"lever": "accept_returns_pct", "kpi": "reported_revenue_cents", "delta_per_unit": -stuffed_open * p["return_rate_base"] * p["stuffed_return_mult"] / 100, "lag_periods": 0},
            {"lever": "accept_returns_pct", "kpi": "ar_cents", "delta_per_unit": -stuffed_open / 100 * 0.5, "lag_periods": 0},
            {"lever": "accept_returns_pct", "kpi": "credibility_index", "delta_per_unit": 0.001, "lag_periods": 1},
            {"lever": "allowance_true_up_pct", "kpi": "allowance_cents", "delta_per_unit": ar / 100, "lag_periods": 0},
            {"lever": "allowance_true_up_pct", "kpi": "net_income_cents", "delta_per_unit": -ar / 100, "lag_periods": 0},
            {"lever": "continue_practice", "kpi": "pulled_forward_cents", "delta_per_unit": 0.3 * pf, "lag_periods": 0},
            {"lever": "continue_practice", "kpi": "reported_revenue_cents", "delta_per_unit": 0.3 * pf, "lag_periods": 0},
            {"lever": "continue_practice", "kpi": "credibility_index", "delta_per_unit": -0.2, "lag_periods": 0},
        ]
        drag = {"pulled_forward_cents": 0.3 * pf, "dso_days": dso_norm, "cash_cents": -0.3 * pf * (1 - gm),
                "distributor_inventory_months": 0.5, "credibility_index": -0.05}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"restate": 1.0, "stop_quarter_end_promotions": 1.0},
                "shocks": [{"quarter": 2, "kpi": "reported_revenue_cents", "pct": -0.25 if kpis["distributor_inventory_months"] >= p["inventory_cap_months"] else -0.05,
                            "headline": "Distributors stop ordering: channel inventory at its cap"}],
                "bounds": {"credibility_index": [0.0, 1.0], "dso_days": [0.0, 365.0], "pulled_forward_cents": [0.0, None]}}


def _with_hints(metrics: list[Any], hints: dict[str, str]) -> list[Any]:
    """Attach a learner-facing hint to every expected metric (``CasePublic.asks``); unknown keys get a generic hint."""
    out = []
    for m in metrics:
        h = m.hint or hints.get(m.key) or next((v for k, v in hints.items() if k.endswith("*") and m.key.startswith(k[:-1])), "")
        out.append(m.model_copy(update={"hint": h or f"Compute {m.label.lower()} from the case database ({m.unit})."}))
    return out

_HINTS: dict[str, str] = {'last5day_revenue_share_p8': 'Bucket deduplicated invoices by quarter; share of amount with invoice_date in the last 7 calendar days.', 'last5day_revenue_share_baseline': 'Average the same share over the first six reporting quarters.', 'pulled_forward_revenue_cents': 'Identify invoices that were not economic sales at quarter end (terms, notes, shipment status / dates) and sum them.', 'restated_revenue_p8_cents': 'Start from reported revenue for the latest quarter and move pulled-forward invoices to the quarter they belong to.', 'dso_p8_days': 'Open receivables at quarter end / invoiced revenue of the quarter x days in the quarter.', 'dso_p6_days': 'Same DSO definition, two quarters earlier.', 'top5_distributor_revenue_share_p8': 'Rank wholesale customers by latest-quarter invoice amount; top five / all invoices.', 'beneish_m_score_p8': 'Beneish 8-variable model on trailing-twelve-month flows vs the same window a year earlier (coefficients in the case notes).', 'beneish_m_score_clean_p6': 'Same model for an earlier, clean quarter as a comparison.', 'sloan_accrual_ratio_p8': '(TTM net income - TTM operating cash flow) / average total assets.', 'cfo_to_ni_p8': 'Latest-quarter operating cash flow divided by net income.', 'return_rate_window_orders_p7': 'Returned amount on quarter-end window orders / their invoiced amount; compare with the rest of the quarter.', 'invoiced_not_shipped_p8_cents': 'LEFT JOIN invoices to shipments; sum invoices without a shipment by quarter end.', 'round_trip_revenue_cents': "Match customers to suppliers on a normalised tax id; sum that customer's invoices."}
