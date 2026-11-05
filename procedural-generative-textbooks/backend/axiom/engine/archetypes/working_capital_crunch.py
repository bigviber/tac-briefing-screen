"""``working_capital_crunch`` - "Profitable and Broke" (archetypes.md section 1).

A fast-growing B2B goods seller (wholesale / EDI / portal channel of an e-commerce, retail or
manufacturing business) books record revenue and positive net income while cash drains into
receivables and inventory, suppliers get stretched and the revolver hits its limit.

Mechanics (all ordinary journal families; every invariant and every sub-ledger tie holds)
-----------------------------------------------------------------------------------------
* Warm-up quarter ``P0`` + ``P1..P8``; orders are generated vectorised for all 27 months
  (``units ~ (1+g)^(m/3) * season_m * LN(0, 0.05)``), 1-5 lines per order, Pareto(1.16) SKU
  popularity, unit cost = price * (1 - gm_sku), gm_sku ~ N(gm, 0.06).
* Invoices are issued at shipment (``invoice_date = shipped date``) on ``terms_days_base``
  (30/45); payment arrives ``terms + max(0, N(5, 12))`` days later.
* Purchasing is a periodic-review order-up-to rule per SKU (review every 3 months, staggered):
  ``order = demand(until next review) + cover(m) * demand(next review month) - on_hand``,
  received on day 1 of the month, so inventory never goes negative. ``cover`` is calibrated to
  ``dio_base``.
* Monthly close: payroll + opex accrued (paid next month), depreciation, allowance top-up,
  interest accrual (paid quarterly), tax at quarter end (paid next quarter), term-debt
  amortisation + reclass, revolver draw when cash < 1 month opex / repay when > 2 months.

Anomalies (window = last 2/3/4 periods for foundation/practitioner/principal)
* **A1 receivables stretch** - ``late_pay_customer_share`` of customers (biased 4:1 to the top 20%
  by revenue) get ``terms = base + dso_drift * ramp`` on new invoices plus ``N(10, 8)`` extra days.
* **A2 inventory build** - over-forecast purchasing: target cover ramps by ``dio_drift / 30.4``
  months across the window (equivalent to purchases ~ demand * (1+over_forecast)^k); no write-down.
* **A3 payables stretch** (practitioner/principal, last 2 periods) - once month-start cash <
  1.5x monthly opex, vendor payment lag = ``dpo_base + dpo_drift``; ~15% of suppliers shorten
  terms / go on credit hold (extra ``purchase_orders.status='on_hold'`` rows) and 5-10% of
  fast-moving SKUs are back-ordered in P8 (``order_items.backordered = 1``, shipment delayed).
* **A4 revolver at limit** (P8) - outcome of A1-A3.

Documented simplifications
* ``revolver_limit``: opening cash is calibrated (two-pass) so the P8 draw lands near the
  nominal ``U(0.10, 0.18) * revenue`` limit; the final limit is ``max(nominal, peak/0.995)``.
* Covenant: the seeded covenant type is kept unless only the other one is breached at P8, in
  which case the breached one is used (the case is about the breach).
* DSO benchmarks for e-commerce/retail reflect card-paid DTC sales; this company's B2B channel
  invoices on 30/45-day terms, so baseline DSO follows ``terms_days_base`` (the benchmark draw is
  recorded in params as ``dso_benchmark``).
* Schema: ``ecommerce_oms`` for every industry, extended with ``invoices`` and ``payments``.
* Quarter simulation is reduced-form (Appendix B.4): lever effects are linearised around P8:
  ``credit_terms_days`` moves DSO 1:1 (AR and cash by revenue/day) and revenue by
  ``+0.004 * R`` per day (``eps_terms``); ``early_pay_discount_pct`` collects ``0.25 * pct`` of AR
  at a cost of 1.5% of the collected amount; ``inventory_liquidation_pct`` turns that share of
  excess inventory into cash at 72% recovery (28% hits net income) and cuts DIO; ``purchase_cover_months``
  moves inventory by one month of COGS per unit (lag 1) and stockouts by -0.15/month;
  ``vendor_terms_days`` moves DPO 1:1 and AP/cash by COGS/day, stockouts +0.4pt/day (lag 1);
  ``revolver_draw_cents`` / ``equity_raise_cents`` add cash 1:1 (revolver interest at rate/4).
  ``anomaly_drag`` = the average quarterly P6-P8 drift (cash, DSO, DIO, CCC) while unresolved;
  ``resolved_if`` names the root-cause reductions (``<=`` conditions): customer terms back to within a quarter of the
  drift above the base terms, and purchasing cover back to about the pre-window DIO (both must hold: Module 3 has
  no OR-groups, so liquidation / early-pay discounts relieve cash but do not resolve the anomaly). ``bounds`` keep
  days in [0, 365], balances non-negative and the revolver within its limit.
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
    at_most,
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
from axiom.engine.finance import Ledger, SubledgerTie, batch_end_dates, cents, split_cents
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state, eoq, mm1, safety_stock, service_z, station_network
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

logger = logging.getLogger("axiom.archetypes.working_capital_crunch")

DAY = pd.Timedelta(days=1)
REGIONS = ("Northeast", "Southeast", "Midwest", "Southwest", "West", "Canada")
CATEGORIES = ("Home", "Outdoor", "Kitchen", "Apparel", "Tools", "Wellness", "Electronics", "Pet")
CARRIERS = (("UPS", "LTL"), ("FedEx Freight", "LTL"), ("XPO", "LTL"), ("Old Dominion", "LTL"), ("UPS", "Ground"))
COUNTRIES = ("US", "US", "MX", "CN", "VN", "IN", "CA")
PAYROLL_SPLIT = {"6000": 0.45, "6200": 0.55}
OPEX_SPLIT = {"6000": 0.35, "6200": 0.35, "6400": 0.30}


def _month_index(ts: pd.Series, start: dt.date) -> np.ndarray:
    t = pd.to_datetime(ts)
    return ((t.dt.year - start.year) * 12 + (t.dt.month - start.month)).to_numpy()


@register
class WorkingCapitalCrunch(Archetype):
    """Profitable and Broke: growth-funded working capital, terms creep and an over-forecast."""

    key = "working_capital_crunch"
    title = "Profitable and Broke"
    pillars = ("accounting_finance", "operations_strategy", "sql")
    industries = ("ecommerce", "manufacturing", "retail", "3pl")
    schema_template = "ecommerce_oms"
    summary = ("A fast-growing goods company posts record revenue and rising profits while its cash drains into "
               "receivables and inventory; the revolver is maxed and a covenant test looms.")
    root_cause_terms = ("cash conversion cycle", "payment terms", "over-forecast", "excess inventory")
    stage_options = ("growth",)  # the narrative is a growth story

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 1 (difficulty scales magnitudes, window, dirt, red herrings)."""
        ind = industry or rng.choice(("ecommerce", "manufacturing", "retail"))
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        lo, hi = pool.industry(ind).revenue_range_usd
        lo, hi = max(lo, 20e6), min(hi, 250e6)
        rev_usd = math.exp(rng.uniform(math.log(lo), math.log(max(hi, lo * 1.01))))

        def band(metric_key: str, q_lo: float, q_hi: float) -> float:
            return bk.sample_between(pool, rng.spawn(f"bench:{metric_key}"), f"{ind}.{metric_key}", ind, q_lo, q_hi)

        gm = float(np.clip(band("gross_margin", 0.25, 0.75), 0.22, 0.6))
        dio_base = float(np.clip(band("dio", 0.25, 0.50), 30, 110))
        dpo_base = float(np.clip(band("dpo", 0.25, 0.50), 22, 60))
        window = difficulty_pick(difficulty, 2, 3, 4)
        p = {
            "industry": ind,
            "business_model": {"ecommerce": "DTC brand with a fast-growing wholesale / B2B channel",
                               "manufacturing": "make-to-stock catalog manufacturer selling to distributors",
                               "retail": "specialty retailer with a wholesale / B2B channel",
                               "3pl": "wholesale distribution arm of a logistics group"}[ind],
            "revenue_ttm_cents": int(rev_usd * 100),
            "growth_qoq": float(rng.uniform(0.06, 0.14)),
            "gm": gm,
            "dso_benchmark": float(band("dso", 0.25, 0.50)),
            "dio_base": dio_base,
            "dpo_base": dpo_base,
            "terms_days_base": int(rng.choice((30, 45))),
            "dso_drift": magnitude(rng, difficulty, 8, 25),
            "dio_drift": magnitude(rng, difficulty, 15, 40),
            "dpo_drift": magnitude(rng, difficulty, 10, 30),
            "late_pay_customer_share": float(rng.uniform(0.25, 0.40)),
            "over_forecast": float(rng.uniform(0.04, 0.08)),
            "revolver_limit_pct_nominal": float(rng.uniform(0.10, 0.18)),
            "covenant_pref": str(rng.choice(("current_ratio", "debt_to_ebitda"))),
            "covenant_min_current_ratio": 1.20,
            "covenant_max_debt_ebitda": 3.0,
            "interest_rate": float(rng.uniform(0.09, 0.12)),
            "tax_rate": float(np.clip(band("effective_tax_rate", 0.5, 0.75), 0.15, 0.28)),
            "capex_pct": float(np.clip(band("capex_pct_revenue", 0.25, 0.5), 0.01, 0.06)),
            "allowance_pct": float(np.clip(band("allowance_pct_ar", 0.25, 0.5), 0.005, 0.04)),
            "op_margin": float(np.clip(band("operating_margin", 0.5, 0.75), 0.05, 0.11)),
            "cash_pct": float(np.clip(band("cash_pct_revenue", 0.25, 0.5), 0.04, 0.15)),
            "debt_to_ebitda_open": float(np.clip(band("debt_to_ebitda", 0.25, 0.50), 0.8, 2.2)),
            "freight_pct": float(rng.uniform(0.03, 0.06)),
            "freight_surcharge": float(rng.uniform(0.20, 0.35)) if difficulty != "foundation" else 0.0,
            "lead_time_days": float(np.clip(bk.sample(pool, rng.spawn("bench:lt"), "3pl.lead_time_ltl_days", "3pl"), 1, 10)),
            "supplier_lead_days": int(rng.integers(10, 41)),
            "review_months": 3,
            "ppe_life_years": 7,
            "window_periods": window,
            "payables_stretch": difficulty != "foundation",
            "decoy_capex_pct": 0.04 if difficulty == "principal" else 0.0,
            "supplier_hold_share": 0.15,
            "backorder_sku_share": float(rng.uniform(0.05, 0.10)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            # seasonality is kept below quarterly growth so revenue still rises every quarter (spec red herring 3)
            "season_amp": 0.5 if ind in ("ecommerce", "retail") else 0.2,
            "difficulty": difficulty,
        }
        budget = ROW_BUDGET[difficulty]
        p["n_skus"] = difficulty_pick(difficulty, 60, 120, 200)
        p["n_customers"] = difficulty_pick(difficulty, 150, 400, 800)
        p["n_suppliers"] = difficulty_pick(difficulty, 15, 30, 50)
        gl_rows = difficulty_pick(difficulty, 2700, 2900, 11000)
        fixed = p["n_skus"] * 21 + p["n_customers"] + p["n_suppliers"] + gl_rows + 120
        p["n_orders"] = int(max(500, (budget * difficulty_pick(difficulty, 0.93, 0.93, 0.86) - fixed) / 8.7))
        p["description"] = (f"{cap_first(p['business_model'])} growing roughly {p['growth_qoq'] * 100:.0f}% a quarter; "
                            f"sells to retailers and distributors on net-{p['terms_days_base']} terms.")
        return p

    # ------------------------------------------------------------------ baseline plan

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Master data and demand: customers, suppliers, products, orders and order lines (no anomaly)."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=False)  # base working-capital defaults: prepaid expenses (AP already comes from the purchasing / cost sub-ledger)
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        # --- customers
        C = p["n_customers"]
        w = 1.0 + g.pareto(1.16, C)
        order = np.argsort(-w, kind="stable")
        rank = np.empty(C, dtype=int)
        rank[order] = np.arange(C)
        seg = np.where(rank < C * 0.10, "enterprise", np.where(rank < C * 0.40, "midmarket", "smb"))
        cust_ids = np.array([f"C{i:05d}" for i in range(1, C + 1)])
        names = [f"{rng.company_name('retail')} #{i}" if i % 7 == 0 else rng.company_name("retail") for i in range(C)]
        customers = pd.DataFrame({
            "customer_id": cust_ids,
            "name": names,
            "segment": seg,
            "region": np.array(REGIONS)[g.integers(0, len(REGIONS), C)],
            "email": [f"ap@{n.split()[0].lower()}{i}.example.com" for i, n in enumerate(names)],
            "acquisition_channel": np.array(("trade_show", "inbound", "outbound", "marketplace"))[g.integers(0, 4, C)],
            "terms_days": np.full(C, p["terms_days_base"], dtype=np.int64),
            "credit_limit_cents": (np.round(w / w.max() * 400_000_00 + 2_000_000) // 100 * 100).astype(np.int64),
        })
        state.extras["cust_weight"] = w
        state.extras["cust_rank"] = rank
        # --- suppliers & products
        V, S = p["n_suppliers"], p["n_skus"]
        suppliers = pd.DataFrame({
            "supplier_id": [f"V{i:04d}" for i in range(1, V + 1)],
            "name": [rng.company_name("manufacturing") for _ in range(V)],
            "country": np.array(COUNTRIES)[g.integers(0, len(COUNTRIES), V)],
            "lead_time_days_quoted": (p["supplier_lead_days"] + g.integers(-5, 6, V)).clip(5, 60),
            "payment_terms_days": np.full(V, int(round(p["dpo_base"] / 15.0) * 15) or 30, dtype=np.int64),
            "deposit_pct": np.zeros(V),
            "credit_hold": np.zeros(V, dtype=bool),
            "terms_changed_at": pd.Series([pd.NaT] * V, dtype="datetime64[ns]"),
        })
        price_usd = np.clip(g.lognormal(math.log(60), 0.8, S), 8, 900)
        gm_sku = np.clip(g.normal(p["gm"], 0.06, S), 0.08, 0.8)
        pop = 1.0 + g.pareto(1.16, S)
        pop = pop / pop.sum()
        products = pd.DataFrame({
            "product_id": [f"P{i:05d}" for i in range(1, S + 1)],
            "sku": [f"SKU-{rng.integers(10000, 99999)}-{i}" for i in range(S)],
            "title": [f"{np.array(CATEGORIES)[i % len(CATEGORIES)]} item {i + 1}" for i in range(S)],
            "category": np.array(CATEGORIES)[g.integers(0, len(CATEGORIES), S)],
            "list_price_usd": price_usd,
            "gm_sku": gm_sku,
            "supplier_id": suppliers["supplier_id"].to_numpy()[g.integers(0, V, S)],
            "active": np.ones(S, dtype=bool),
            "introduced_at": pd.to_datetime(cal.start) - pd.to_timedelta(g.integers(30, 1500, S), unit="D"),
        })
        state.extras["popularity"] = pop
        # --- orders per month
        N = p["n_orders"]
        season = np.array([1.0 + p["season_amp"] * p["growth_qoq"] if m.month >= 10 else 1.0 for m in months])
        trend = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3.0)
        wts = trend * season * g.lognormal(0, 0.05, n_m)
        counts = np.floor(N * wts / wts.sum()).astype(int)
        counts[-1] += N - counts.sum()
        month_idx = np.repeat(np.arange(n_m), counts)
        mstart = pd.to_datetime(pd.Series(months))
        days_in = mstart.dt.days_in_month.to_numpy()
        day = (g.random(N) * days_in[month_idx]).astype(int)
        secs = g.integers(7 * 3600, 19 * 3600, N)
        order_ts = mstart.to_numpy()[month_idx] + pd.to_timedelta(day, unit="D").to_numpy() + pd.to_timedelta(secs, unit="s").to_numpy()
        sort = np.argsort(order_ts, kind="stable")
        order_ts = order_ts[sort]
        cust_p = w / w.sum()
        cust_idx = g.choice(C, size=N, p=cust_p)
        orders = pd.DataFrame({
            "order_id": [f"SO{i:06d}" for i in range(1, N + 1)],
            "customer_id": cust_ids[cust_idx],
            "order_ts": pd.to_datetime(order_ts),
            "channel": np.array(("wholesale", "edi", "portal"))[g.integers(0, 3, N)],
            "ship_region": customers["region"].to_numpy()[cust_idx],
            "sales_rep_id": np.array([f"R{k:03d}" for k in range(1, 13)])[g.integers(0, 12, N)],
        })
        orders["ship_days"] = g.integers(0, 3, N)
        orders["ship_secs"] = g.integers(0, 4 * 3600, N)
        ship_ts = orders["order_ts"] + pd.to_timedelta(orders["ship_days"], unit="D") + pd.to_timedelta(orders["ship_secs"], unit="s")
        ship_month = np.minimum(_month_index(ship_ts, cal.start), n_m - 1)
        # --- order lines
        k = 1 + np.minimum(g.poisson(1.2, N), 4)
        o_idx = np.repeat(np.arange(N), k)
        L = len(o_idx)
        sku_idx = g.choice(S, size=L, p=pop)
        last12 = month_idx[sort] >= n_m - 12
        aov_target = p["revenue_ttm_cents"] / 100 / max(1, last12.sum())
        mean_price = float((price_usd * pop).sum())
        qmean = max(1.0, aov_target / k.mean() / mean_price)
        qty = np.maximum(1, np.round(g.lognormal(math.log(qmean) - 0.125, 0.5, L))).astype(np.int64)
        # smooth monthly revenue onto the trend: T_m = trend * season * LN(0, 0.03), scaled to the TTM target
        line_month = ship_month[o_idx]  # smooth on the invoice (= ship) month
        act = np.bincount(line_month, weights=qty * price_usd[sku_idx], minlength=n_m)
        target = trend * season * g.lognormal(0, 0.03, n_m)
        target *= (p["revenue_ttm_cents"] / 100) / target[-12:].sum()
        factor = np.divide(target, act, out=np.ones(n_m), where=act > 0)
        qty = np.maximum(1, np.round(qty * factor[line_month])).astype(np.int64)
        items = pd.DataFrame({"order_idx": o_idx, "sku_idx": sku_idx, "quantity": qty})
        # price scale so realised TTM revenue ~= target
        raw = (qty * price_usd[sku_idx])[last12[o_idx]].sum()
        scale = (p["revenue_ttm_cents"] / 100) / raw if raw > 0 else 1.0
        list_cents = np.maximum(100, np.round(price_usd * scale * 100)).astype(np.int64)
        cost_cents = np.maximum(50, np.round(list_cents * (1 - gm_sku))).astype(np.int64)
        products["list_price_cents"] = list_cents
        products["unit_cost_cents"] = cost_cents
        state.frames.update({"customers": customers, "suppliers": suppliers, "products": products, "orders": orders})
        state.extras["items"] = items
        state.extras["months_n"] = n_m
        # policy
        # periodic review: top-20% SKUs by popularity monthly, the long tail every ``review_months``
        top = np.argsort(-pop, kind="stable")[: max(1, S // 5)]
        review = np.full(S, p["review_months"], dtype=np.int64)
        review[top] = 1
        state.extras["review"] = review
        # receipts land ~0.6 month before they are needed, which the base cover absorbs
        state.extras["cover_base"] = np.maximum(0.2, p["dio_base"] / 30.4 - (review - 1) / 2.0 - 0.6)
        state.extras["late_customers"] = np.zeros(C, dtype=bool)
        state.extras["held_suppliers"] = np.zeros(V, dtype=bool)
        state.extras["backorder_skus"] = np.zeros(S, dtype=bool)

    # ------------------------------------------------------------------ anomalies

    def _window(self, state: WorldState) -> tuple[int, int]:
        """(first window month index, number of window months) - window = last ``window_periods`` quarters."""
        n_m = state.extras["months_n"]
        wm = 3 * state.params["window_periods"]
        return n_m - wm, wm

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """A1 terms creep, A2 over-forecast cover ramp, A3 supplier stretch/holds/backorders, A4 outcome."""
        p = state.params
        rng = ctx.rng.spawn("anomaly")
        g = rng.generator
        C, V, S = p["n_customers"], p["n_suppliers"], p["n_skus"]
        w0, wm = self._window(state)
        win_periods = state.periods[-p["window_periods"]:]
        # A1: late customers biased 4:1 towards the top 20% by revenue
        rank = state.extras["cust_rank"]
        bias = np.where(rank < 0.2 * C, 4.0, 1.0)
        n_late = max(1, int(round(p["late_pay_customer_share"] * C)))
        late = rank < max(1, int(0.05 * C))  # key accounts (the "top-3 customer renewal") always get the extension
        rest = np.flatnonzero(~late)
        k = max(0, n_late - int(late.sum()))
        late[g.choice(rest, size=k, replace=False, p=bias[rest] / bias[rest].sum())] = True
        state.extras["late_customers"] = late
        # A3: held suppliers + backordered fast movers (last period)
        if p["payables_stretch"]:
            demand = np.bincount(state.extras["items"]["sku_idx"], weights=state.extras["items"]["quantity"], minlength=S)
            fast = np.argsort(-demand, kind="stable")[: max(4, S // 4)]
            n_bo = max(1, int(round(p["backorder_sku_share"] * len(fast))))
            bo = np.zeros(S, dtype=bool)
            bo[g.choice(fast, size=n_bo, replace=False)] = True
            sup_ids = state.frames["suppliers"]["supplier_id"].to_numpy()
            held = np.isin(sup_ids, state.frames["products"]["supplier_id"].to_numpy()[bo])
            extra = max(0, int(round(p["supplier_hold_share"] * V)) - int(held.sum()))
            if extra:
                cand = np.flatnonzero(~held)
                held[g.choice(cand, size=min(extra, len(cand)), replace=False)] = True
            state.extras["held_suppliers"] = held
            state.extras["backorder_skus"] = bo
        anomalies = [
            Anomaly(id="A1", kind="receivables_terms_creep", pillar="accounting_finance", title="Receivables stretch: terms creep at the largest customers",
                    mechanism=(f"{n_late} of {C} customers (biased 4:1 to the top 20% by revenue) were moved from net-{p['terms_days_base']} to up to "
                               f"net-{p['terms_days_base'] + round(p['dso_drift'])} on new invoices over {', '.join(win_periods)} and pay ~10 days later still; "
                               "sale_credit entries are unchanged, collection entries shift later."),
                    periods=win_periods, affected_accounts=["1100", "1000"], affected_tables=["invoices", "payments", "customers"],
                    magnitude={"dso_drift_days": round(p["dso_drift"], 3), "late_pay_customer_share": round(p["late_pay_customer_share"], 4)},
                    severity="high"),
            Anomaly(id="A2", kind="inventory_overbuild", pillar="operations_strategy", title="Inventory build from an over-forecast purchasing rule",
                    mechanism=(f"Purchasing forecast growth at roughly {(p['growth_qoq'] + p['over_forecast']) * 100:.0f}% a quarter vs "
                               f"{p['growth_qoq'] * 100:.0f}% actual, so target cover ramped by {p['dio_drift']:.0f} days of COGS across the window; "
                               "receipts outpace issues, no write-down taken."),
                    periods=win_periods, affected_accounts=["1220", "2000"], affected_tables=["purchase_orders", "inventory_movements"],
                    magnitude={"dio_drift_days": round(p["dio_drift"], 3), "over_forecast": round(p["over_forecast"], 4)}, severity="high"),
        ]
        if p["payables_stretch"]:
            a3_periods = state.periods[-2:]
            anomalies.append(Anomaly(
                id="A3", kind="stretched_payables", pillar="accounting_finance", title="Payables stretch and supplier response",
                mechanism=(f"Once month-start cash fell below 1.5x monthly opex, vendor payments slipped from ~{p['dpo_base']:.0f} to "
                           f"~{p['dpo_base'] + p['dpo_drift']:.0f} days; {int(state.extras['held_suppliers'].sum())} suppliers shortened terms or "
                           f"put the account on credit hold and {int(state.extras['backorder_skus'].sum())} fast-moving SKUs were back-ordered in {state.periods[-1]}."),
                periods=a3_periods, affected_accounts=["2000", "1000"], affected_tables=["purchase_orders", "suppliers", "order_items", "shipments"],
                magnitude={"dpo_drift_days": round(p["dpo_drift"], 3)}, severity="medium"))
        anomalies.append(Anomaly(
            id="A4", kind="revolver_at_limit", pillar="accounting_finance", title="Revolver at limit and covenant test",
            mechanism="Working-capital build is funded by revolver draws (cash < 1 month opex triggers a draw); by the last quarter the line is effectively fully drawn.",
            periods=[state.periods[-1]], affected_accounts=["2500", "1000", "7000"], affected_tables=["gl_lines", "gl_journal"],
            magnitude={}, severity="critical"))
        return anomalies

    # ------------------------------------------------------------------ derived frames

    def _derive(self, ctx: BuildContext, state: WorldState) -> None:
        """Order lines, shipments, invoices, customer payments and the purchasing plan from the plan."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        products, orders, customers = state.frames["products"], state.frames["orders"], state.frames["customers"]
        items = state.extras["items"]
        months = state.extras["months"]
        n_m = state.extras["months_n"]
        start = cal.start
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        w0, wm = self._window(state)
        N = len(orders)
        order_month = _month_index(orders["order_ts"], start)
        # backorders (A3, last period)
        bo_sku = state.extras["backorder_skus"]
        item_bo = bo_sku[items["sku_idx"].to_numpy()] & (order_month[items["order_idx"].to_numpy()] >= n_m - 3)
        order_bo = np.bincount(items["order_idx"].to_numpy(), weights=item_bo.astype(float), minlength=N) > 0
        delay = np.where(order_bo, g.integers(7, 22, N), 0)
        shipped = (orders["order_ts"] + pd.to_timedelta(orders["ship_days"].to_numpy() + delay, unit="D")
                   + pd.to_timedelta(orders["ship_secs"].to_numpy(), unit="s"))
        # lines
        sku = items["sku_idx"].to_numpy()
        lp = products["list_price_cents"].to_numpy()[sku]
        uc = products["unit_cost_cents"].to_numpy()[sku]
        qty = items["quantity"].to_numpy()
        line_total = qty * lp
        oi = pd.DataFrame({
            "order_item_id": [f"OI{i:07d}" for i in range(1, len(items) + 1)],
            "order_id": orders["order_id"].to_numpy()[items["order_idx"].to_numpy()],
            "product_id": products["product_id"].to_numpy()[sku],
            "quantity": qty, "unit_price_cents": lp, "unit_cost_cents": uc, "line_total_cents": line_total,
            "backordered": item_bo.astype(np.int64),
        })
        subtotal = np.bincount(items["order_idx"].to_numpy(), weights=line_total, minlength=N).astype(np.int64)
        cost = np.bincount(items["order_idx"].to_numpy(), weights=qty * uc, minlength=N).astype(np.int64)
        ship_month = _month_index(shipped, start)
        surcharge = np.where(ship_month >= n_m - 6, 1.0 + p["freight_surcharge"], 1.0)
        freight = np.round(subtotal * p["freight_pct"] * g.lognormal(-0.011, 0.15, N) * surcharge).astype(np.int64)
        # drop orders shipping after the as-of date (still open orders)
        in_scope = (shipped <= end_ts).to_numpy()
        status = np.where(in_scope, "fulfilled", "open")
        orders_out = orders.drop(columns=["ship_days", "ship_secs"]).copy()
        orders_out["status"] = status
        orders_out["subtotal_cents"] = subtotal
        orders_out["total_cents"] = subtotal
        orders_out["currency"] = "USD"
        orders_out["created_at"] = orders["order_ts"]
        orders_out["updated_at"] = shipped.where(in_scope, orders["order_ts"])
        shipments = pd.DataFrame({
            "shipment_id": [f"SH{i:06d}" for i in range(1, N + 1)],
            "order_id": orders["order_id"],
            "carrier": np.array([c for c, _ in CARRIERS])[g.integers(0, len(CARRIERS), N)],
            "service": "LTL",
            "shipped_at": shipped,
            "delivered_at": shipped + pd.to_timedelta(np.ceil(p["lead_time_days"] * g.lognormal(0, 0.3, N)), unit="D"),
            "warehouse_id": np.array(("WH-EAST", "WH-WEST", "WH-CENTRAL"))[g.integers(0, 3, N)],
            "freight_cost_cents": freight,
        })[in_scope].reset_index(drop=True)
        # invoices (one per shipped order) and terms
        cust_pos = pd.Index(customers["customer_id"]).get_indexer(orders["customer_id"])
        late = state.extras["late_customers"][cust_pos]
        ramp = np.clip((ship_month - w0 + 1) / max(1, wm), 0, 1) * (ship_month >= w0)
        ramp = np.ceil(ramp * p["window_periods"]) / p["window_periods"]  # steps per quarter
        terms = np.where(late & (ship_month >= w0), p["terms_days_base"] + np.round(p["dso_drift"] * ramp / 5) * 5, p["terms_days_base"]).astype(np.int64)
        base_delay = np.maximum(0, g.normal(5, 12, N))
        extra = np.where(late & (ship_month >= w0), np.maximum(0, g.normal(10, 8, N)), 0)
        inv_date = shipped.dt.normalize()
        paid_at = inv_date + pd.to_timedelta(terms + base_delay + extra, unit="D") + pd.to_timedelta(g.integers(8 * 3600, 18 * 3600, N), unit="s")
        paid = (paid_at <= end_ts).to_numpy()
        invoices = pd.DataFrame({
            "invoice_id": [f"INV{i:06d}" for i in range(1, N + 1)],
            "order_id": orders["order_id"],
            "customer_id": orders["customer_id"],
            "invoice_date": inv_date,
            "due_date": inv_date + pd.to_timedelta(terms, unit="D"),
            "terms_days": terms,
            "amount_cents": subtotal,
            "status": np.where(paid, "paid", "open"),
            "paid_at": paid_at.where(paid),
            "created_at": shipped,
            "updated_at": paid_at.where(paid, shipped),
            "ingest_id": np.ones(N, dtype=np.int64),
            "_cost": cost, "_freight": freight,
        })[in_scope].reset_index(drop=True)
        payments = invoices.loc[invoices["status"] == "paid", ["invoice_id", "customer_id", "paid_at", "amount_cents"]].copy()
        payments = payments.sort_values(["paid_at", "invoice_id"], kind="stable").reset_index(drop=True)
        payments.insert(0, "payment_id", [f"PMT{i:06d}" for i in range(1, len(payments) + 1)])
        payments["method"] = np.array(("ach", "wire", "check"))[g.integers(0, 3, len(payments))]
        # customer master terms (A1 visible as current terms)
        cust = customers.copy()
        if state.extras["late_customers"].any():
            cust.loc[state.extras["late_customers"], "terms_days"] = int(p["terms_days_base"] + round(p["dso_drift"] / 5) * 5)
        # --- purchasing plan (per SKU, monthly)
        S = len(products)
        ship_items_month = ship_month[items["order_idx"].to_numpy()]
        keep = in_scope[items["order_idx"].to_numpy()]
        D = np.zeros((S, n_m + 4))
        np.add.at(D, (sku[keep], ship_items_month[keep]), qty[keep])
        # extrapolate beyond the horizon (and the unshipped tail of the last month)
        tail = D[:, max(0, n_m - 3):n_m].mean(axis=1) * (1 + p["growth_qoq"] / 3)
        for j in range(n_m, n_m + 4):
            D[:, j] = tail
        r = state.extras["review"]
        phase = (np.arange(S) * 7) % r
        cover_base = state.extras["cover_base"]
        ramp_m = np.clip((np.arange(n_m + 1) - w0 + 1) / wm, 0, 1) if wm else np.zeros(n_m + 1)
        cover_add = (p["dio_drift"] / 30.4) * ramp_m
        state.extras["cover_by_month"] = float(np.average(cover_base, weights=D[:, :n_m].sum(axis=1) + 1e-9)) + cover_add[:n_m]
        on_hand = np.round(cover_base * D[:, 0]).astype(np.int64)
        opening_qty = on_hand.copy()
        po_rows = []
        # plan one month past the horizon: its stock arrives (early) inside the last month
        for m in range(n_m + 1):
            review = (m == 0) | ((m - phase) % r == 0)
            nxt = np.where(m == 0, np.where(phase == 0, r, phase), r)
            need = np.zeros(S)
            for j in range(int(r.max())):
                need += np.where(j < nxt, D[:, m + j], 0.0)
            cover_m = D[np.arange(S), m + nxt] * (cover_base + cover_add[m])
            q = np.where(review, np.maximum(0, np.ceil(need + cover_m - on_hand)), 0).astype(np.int64)
            for s in np.flatnonzero(q > 0):
                po_rows.append((m, int(s), int(q[s])))
            # D is integer-valued for in-horizon months, so on_hand stays exact and never negative
            on_hand = on_hand + q - np.round(D[:, m]).astype(np.int64)
        po = pd.DataFrame(po_rows, columns=["month", "sku_idx", "qty"])
        if len(po):
            po = po[~((po["month"] == n_m) & (po["qty"] <= 0))].reset_index(drop=True)
        mstart = pd.to_datetime(pd.Series(months + [(pd.Timestamp(months[-1]) + pd.offsets.MonthBegin(1)).date()]))
        # stock for plan month m arrives during the last ~3 weeks of month m-1 (month 0: on day 1), so
        # receipts (and hence payables) are spread over the month and stock is always on hand in time
        early = np.where(po["month"].to_numpy() == 0, 0, g.integers(1, 21, len(po)))
        received = pd.Series(mstart.to_numpy()[po["month"].to_numpy()] + np.timedelta64(6, "h")
                             - pd.to_timedelta(early, unit="D").to_numpy())
        lead = products["supplier_id"].map(state.frames["suppliers"].set_index("supplier_id")["lead_time_days_quoted"]).to_numpy()[po["sku_idx"].to_numpy()]
        ordered = received - pd.to_timedelta(lead + g.integers(0, 4, len(po)), unit="D")
        po_df = pd.DataFrame({
            "po_id": [f"PO{i:06d}" for i in range(1, len(po) + 1)],
            "supplier_id": products["supplier_id"].to_numpy()[po["sku_idx"].to_numpy()],
            "product_id": products["product_id"].to_numpy()[po["sku_idx"].to_numpy()],
            "ordered_at": ordered.dt.normalize(),
            "expected_at": received.dt.normalize(),
            "received_at": received,
            "quantity_ordered": po["qty"].to_numpy(),
            "quantity_received": po["qty"].to_numpy(),
            "unit_cost_cents": products["unit_cost_cents"].to_numpy()[po["sku_idx"].to_numpy()],
            "status": "received",
            "_month": po["month"].to_numpy(),
            "_rec_month": _month_index(received, start),
            "_sku": po["sku_idx"].to_numpy(),
        })
        po_df = po_df[po_df["received_at"] <= end_ts].reset_index(drop=True)
        po_df["amount_cents"] = po_df["quantity_received"] * po_df["unit_cost_cents"]
        po_df["_noise"] = g.normal(0, 3, len(po_df))
        state.extras["opening_qty"] = opening_qty
        state.extras["demand"] = D
        state.frames.update({"orders": orders_out, "order_items": oi, "shipments": shipments, "invoices": invoices,
                             "payments": payments, "customers": cust})
        state.extras["po_plan"] = po_df

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Derive frames, then post the books month by month (two passes to calibrate opening cash)."""
        self._derive(ctx, state)
        p = state.params
        first = self._run_books(ctx, state, cash_shift=0)
        target = p["revolver_limit_pct_nominal"] * p["revenue_ttm_cents"] * 0.98
        shift = int(first["revolver_end"] - target)
        floor = -int(first["opening_cash"] * 0.85)
        shift = max(shift, floor)
        final = self._run_books(ctx, state, cash_shift=shift)
        state.extras["books"] = final

    def _run_books(self, ctx: BuildContext, state: WorldState, cash_shift: int) -> dict[str, Any]:
        """Post every entry from P0 to P8 into a fresh ledger; returns facts used downstream."""
        p, cal = state.params, state.calendar
        L = self.fresh_ledger(state)
        state.ledger = L
        gran = GL_BATCH[p["difficulty"]]
        months = state.extras["months"]
        n_m = len(months)
        end_date = cal.end
        products = state.frames["products"]
        R = p["revenue_ttm_cents"]
        inv, pay, po = state.frames["invoices"], state.frames["payments"], state.extras["po_plan"].copy()
        # opening balances
        cost = products["unit_cost_cents"].to_numpy()
        inv0 = int((state.extras["opening_qty"] * cost).sum())
        ppe = cents(R * p["capex_pct"] * p["ppe_life_years"] * 0.55)
        accdep = cents(ppe * 0.45)
        ebitda = R * (p["op_margin"] + 0.02)
        debt = cents(ebitda * p["debt_to_ebitda_open"] / 100_000) * 100_000
        debt_cur = cents(debt * 0.2)
        cash0 = cents(R * p["cash_pct"] + R / 12 * 0.6) + cash_shift
        net = cash0 + inv0 + ppe - accdep - debt
        cs = cents(0.6 * net) if net > 0 else max(cents(0.25 * (cash0 + inv0 + ppe - accdep)), 1)
        ob = {"1000": cash0, "1220": inv0, "1500": ppe, "1510": -accdep, "2600": -debt_cur, "2610": -(debt - debt_cur),
              "3000": -cs, "3100": -(net - cs)}
        L.open_books(cal.opening_date, ob)
        # batches
        inv_b = inv.assign(b=batch_end_dates(inv["invoice_date"], gran)).groupby("b", sort=True).agg(
            amt=("amount_cents", "sum"), cost=("_cost", "sum"), freight=("_freight", "sum"), n=("invoice_id", "size"))
        pay_b = pay.assign(b=batch_end_dates(pay["paid_at"], gran)).groupby("b", sort=True).agg(amt=("amount_cents", "sum"), n=("payment_id", "size"))
        rec_b = po.assign(b=batch_end_dates(po["received_at"], gran)).groupby("b", sort=True).agg(amt=("amount_cents", "sum"), n=("po_id", "size"))
        month_rev = inv.groupby(_month_index(inv["invoice_date"], cal.start))["amount_cents"].sum().reindex(range(n_m), fill_value=0).to_numpy()
        po_month = po["_rec_month"].to_numpy()
        w0, _wm = self._window(state)
        a3_start = n_m - 6
        stretched = False
        trigger_month: int | None = None
        pay_dates = np.full(len(po), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
        prev_payroll = prev_accrued = 0
        tax_due = 0
        revolver = 0
        revolver_hist: list[int] = []
        cash_hist: list[int] = []
        opex_hist: list[int] = []
        nz = self.period_noise(ctx, state)  # review C2: seasonal + idiosyncratic quarter noise
        term_bal = debt
        amort_q = cents(debt * 0.05)
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            quarter_end = ms.month % 3 == 0
            run_rate = float(month_rev[max(0, m - 2): m + 1].mean()) if m > 0 else float(month_rev[0])
            run_rate = max(run_rate, R / 12 * 0.5)
            opex_total = cents(run_rate * (p["gm"] - p["freight_pct"] - p["op_margin"] - 0.015) * nz.ox(ms))
            opex_hist.append(opex_total)
            cash_start = L.closing_balances()["1000"]
            if p["payables_stretch"] and m >= a3_start and not stretched and (cash_start < 1.5 * opex_total or m >= n_m - 3):
                stretched = True
                trigger_month = m
            # schedule vendor payments for this month's receipts
            sel = po_month == m
            lag = p["dpo_base"] + po["_noise"].to_numpy()[sel] + (p["dpo_drift"] if stretched else 0.0)
            # whole seconds, so the GL month cut-off (< next day) and the sub-ledger cut-off (<= 23:59:59) agree (seed sweep:
            # seed 18 had a payment at 23:59:30 on the last day that the GL paid and the sub-ledger left open)
            pay_dates[sel] = (po["received_at"].to_numpy()[sel] + pd.to_timedelta(np.maximum(5, lag), unit="D").to_numpy()).astype("datetime64[s]").astype("datetime64[ns]")
            events: list[tuple[dt.date, int, Any]] = []
            if m > 0:
                pp, pa = prev_payroll, prev_accrued
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=pp: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=pa: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex and freight paid", source="opex")))
            if ms.month in (1, 4, 7, 10) and tax_due > 0:
                td = tax_due
                events.append((ms + dt.timedelta(days=14), 1, lambda d, a=td: L.pay_tax(d, a)))
                tax_due = 0
            month_sel = lambda df: df[(df.index >= pd.Timestamp(ms)) & (df.index <= pd.Timestamp(me))]  # noqa: E731
            for b, row in month_sel(rec_b).iterrows():
                dim = {"batch": f"purchasing:{b.date()}", "rows": str(int(row.n))}
                events.append((b.date(), 2, lambda d, a=int(row.amt), dm=dim: L.purchase_inventory(d, a, memo="Inventory receipts (batch)", dim=dm)))
            freight_m = 0
            for b, row in month_sel(inv_b).iterrows():
                dim = {"batch": f"sales:{b.date()}", "rows": str(int(row.n))}
                freight_m += int(row.freight)
                events.append((b.date(), 3, lambda d, a=int(row.amt), dm=dim: L.sell_on_credit(d, a, memo="Credit sales invoiced (batch)", dim=dm)))
                events.append((b.date(), 3, lambda d, a=int(row.cost), dm=dim: L.relieve_inventory_cogs(d, a, memo="COGS on shipments (batch)", dim=dm)))
                events.append((b.date(), 3, lambda d, a=int(row.freight), dm=dim: L.adjust(d, [("5030", a, 0, dm), ("2100", 0, a, dm)], "Carrier freight accrued (batch)", "fulfillment")))
            for b, row in month_sel(pay_b).iterrows():
                dim = {"batch": f"collections:{b.date()}", "rows": str(int(row.n))}
                events.append((b.date(), 4, lambda d, a=int(row.amt), dm=dim: L.collect_cash(d, a, memo="Customer payments (batch)", dim=dm)))
            due = (pay_dates >= np.datetime64(ms)) & (pay_dates < np.datetime64(me + dt.timedelta(days=1)))
            if due.any():
                dues = pd.DataFrame({"b": batch_end_dates(pd.Series(pay_dates[due]), gran), "amt": po["amount_cents"].to_numpy()[due]})
                for b, grp in dues.groupby("b", sort=True):
                    dim = {"batch": f"payables:{b.date()}", "rows": str(len(grp))}
                    events.append((b.date(), 5, lambda d, a=int(grp.amt.sum()), dm=dim: L.pay_ap(d, a, memo="Vendor payments (batch)", dim=dm)))
            capex = cents(run_rate * 12 * p["capex_pct"] / 12)
            if p["decoy_capex_pct"] and m == n_m - 6:
                capex += cents(R * p["decoy_capex_pct"])
                state.extras["decoy_capex_cents"] = cents(R * p["decoy_capex_pct"])
            events.append((ms + dt.timedelta(days=14), 6, lambda d, a=capex: L.capex(d, a, memo="Capital expenditure (warehouse racking)" if a > cents(R * 0.02) else "Capital expenditure")))
            payroll_total = cents(opex_total * 0.6)
            other_opex = opex_total - payroll_total
            pr_split = dict(zip(PAYROLL_SPLIT, _split(payroll_total, list(PAYROLL_SPLIT.values())), strict=True))
            ox_split = dict(zip(OPEX_SPLIT, _split(other_opex, list(OPEX_SPLIT.values())), strict=True))
            events.append((me, 7, lambda d, s=pr_split: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox_split: L.accrue_opex(d, s)))
            events.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / (p["ppe_life_years"] * 12)))))

            def provision(d: dt.date) -> None:
                bal = L.closing_balances()
                target = cents(bal["1100"] * p["allowance_pct"])
                L.bad_debt_provision(d, target + bal["1110"])

            events.append((me, 8, provision))
            events.append((me, 9, lambda d: L.accrue_interest(d, cents(revolver_box[0] * p["interest_rate"] / 12 + term_box[0] * (p["interest_rate"] - 0.01) / 12))))
            if quarter_end:
                events.append((me, 10, lambda d: L.pay_interest(d, -L.closing_balances()["2150"])))
                events.append((me, 10, lambda d: (L.repay(d, amort_q, debt_account="2600", memo="Term debt amortisation", source="financing"),
                                                  L.reclass(d, amort_q, "2610", "2600", memo="Reclass current portion of term debt"))))

                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))

                events.append((me, 11, tax))
            revolver_box = [revolver]
            term_box = [term_bal]
            events.sort(key=lambda e: (e[0], e[1]))
            for d, _prio, fn in events:
                if d > end_date:
                    continue
                fn(d)
            if quarter_end:
                term_bal -= amort_q
                tax_due = L.closing_balances()["2400"] * -1
            prev_payroll = payroll_total
            prev_accrued = other_opex + freight_m
            # revolver decision
            cash = L.closing_balances()["1000"]
            if cash < opex_total:
                draw = int(math.ceil((1.25 * opex_total - cash) / 1_000_000) * 1_000_000)
                L.borrow(me, draw, memo="Revolver draw")
                revolver += draw
            elif cash > 2 * opex_total and revolver > 0:
                rep = min(revolver, int(math.floor((cash - 1.5 * opex_total) / 1_000_000) * 1_000_000))
                if rep > 0:
                    L.repay(me, rep, memo="Revolver repayment")
                    revolver -= rep
            revolver_hist.append(revolver)
            cash_hist.append(L.closing_balances()["1000"])
        po = po.assign(paid_at=pd.Series(pay_dates).where(pd.Series(pay_dates) < pd.Timestamp(end_date) + pd.Timedelta(days=1)).to_numpy())
        state.extras["po_paid"] = po
        return {"revolver_end": revolver, "revolver_hist": revolver_hist, "cash_hist": cash_hist, "opex_hist": opex_hist,
                "opening_cash": cash0, "trigger_month": trigger_month, "inv0": inv0}

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Clean frames + the difficulty's dirt rules (2 / 4 / 6)."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("tables").generator
        po = state.extras["po_paid"]
        products = state.frames["products"]
        sup = state.frames["suppliers"].copy()
        held = state.extras["held_suppliers"]
        if held.any():
            p8_start = pd.Timestamp(cal.bounds(state.periods[-1])[0])
            sup.loc[held, "payment_terms_days"] = np.where(g.random(int(held.sum())) < 0.5, 0, 15)
            sup.loc[held, "deposit_pct"] = np.where(sup.loc[held, "payment_terms_days"].to_numpy() == 0, 30.0, 0.0)
            sup.loc[held, "credit_hold"] = True
            sup.loc[held, "terms_changed_at"] = p8_start + pd.to_timedelta(g.integers(3, 40, int(held.sum())), unit="D")
        sup["created_at"] = pd.Timestamp(cal.start) - pd.Timedelta(days=400)
        po_out = po[["po_id", "supplier_id", "product_id", "ordered_at", "expected_at", "received_at", "quantity_ordered",
                     "quantity_received", "unit_cost_cents", "amount_cents", "status", "paid_at"]].copy()
        po_out["due_date"] = (po_out["received_at"].dt.normalize()
                              + pd.to_timedelta(po_out["supplier_id"].map(state.frames["suppliers"].set_index("supplier_id")["payment_terms_days"]).to_numpy(), unit="D"))
        if held.any():
            held_ids = sup.loc[held, "supplier_id"].to_numpy()
            rows = []
            p8_months = state.extras["months"][-3:]
            k = 0
            for sid in held_ids:
                prods = products.loc[products["supplier_id"] == sid]
                if prods.empty:
                    continue
                for ms in p8_months[1:]:
                    pr = prods.iloc[int(g.integers(0, len(prods)))]
                    k += 1
                    q = int(g.integers(50, 400))
                    rows.append({"po_id": f"PH{k:05d}", "supplier_id": sid, "product_id": pr.product_id,
                                 "ordered_at": pd.Timestamp(ms) + pd.Timedelta(days=int(g.integers(0, 20))),
                                 "expected_at": pd.Timestamp(ms) + pd.Timedelta(days=45), "received_at": pd.NaT,
                                 "quantity_ordered": q, "quantity_received": 0, "unit_cost_cents": int(pr.unit_cost_cents),
                                 "amount_cents": q * int(pr.unit_cost_cents), "status": "on_hold", "paid_at": pd.NaT, "due_date": pd.NaT})
            if rows:
                po_out = pd.concat([po_out, pd.DataFrame(rows)], ignore_index=True)
        # inventory movements
        oi = state.frames["order_items"]
        ships = state.frames["shipments"].set_index("order_id")["shipped_at"]
        sold = oi[oi["order_id"].isin(ships.index)]
        mv_sale = pd.DataFrame({"product_id": sold["product_id"].to_numpy(), "movement_ts": ships.reindex(sold["order_id"]).to_numpy(),
                                "movement_type": "sale", "quantity": -sold["quantity"].to_numpy(), "unit_cost_cents": sold["unit_cost_cents"].to_numpy(),
                                "reference_id": sold["order_id"].to_numpy()})
        mv_rec = pd.DataFrame({"product_id": po["product_id"].to_numpy(), "movement_ts": po["received_at"].to_numpy(), "movement_type": "receipt",
                               "quantity": po["quantity_received"].to_numpy(), "unit_cost_cents": po["unit_cost_cents"].to_numpy(),
                               "reference_id": po["po_id"].to_numpy()})
        oq = state.extras["opening_qty"]
        nz = oq > 0
        mv_open = pd.DataFrame({"product_id": products["product_id"].to_numpy()[nz],
                                "movement_ts": pd.Timestamp(cal.opening_date) + pd.Timedelta(hours=23), "movement_type": "adjustment",
                                "quantity": oq[nz], "unit_cost_cents": products["unit_cost_cents"].to_numpy()[nz], "reference_id": "OPENING-BALANCE"})
        mv = pd.concat([mv_open, mv_rec, mv_sale], ignore_index=True)
        mv["movement_ts"] = pd.to_datetime(mv["movement_ts"])
        mv = mv.sort_values(["movement_ts", "movement_type", "product_id"], kind="stable").reset_index(drop=True)
        mv.insert(0, "movement_id", np.arange(1, len(mv) + 1, dtype=np.int64))
        mv.insert(2, "warehouse_id", np.array(("WH-EAST", "WH-WEST", "WH-CENTRAL"))[mv["movement_id"].to_numpy() % 3])
        state.frames["inventory_movements"] = mv
        state.frames["purchase_orders"] = po_out
        state.frames["suppliers"] = sup
        customers = state.frames["customers"].copy()
        first = state.frames["orders"].groupby("customer_id")["order_ts"].min()
        customers["first_order_at"] = customers["customer_id"].map(first)
        customers["created_at"] = customers["first_order_at"].fillna(pd.Timestamp(cal.start)) - pd.to_timedelta(g.integers(5, 300, len(customers)), unit="D")
        state.frames["customers"] = customers
        prod_out = products[["product_id", "sku", "title", "category", "unit_cost_cents", "list_price_cents", "supplier_id", "active", "introduced_at"]]
        invoices = state.frames["invoices"].drop(columns=["_cost", "_freight"])
        dirt_plan = [
            ("customers", "nulls:email:0.08"), ("customers", "case_mixed:region"), ("invoices", "dup_keys:0.02"),
            ("orders", "tz_mixed:order_ts"), ("customers", "whitespace:segment"), ("invoices", "late_arriving:updated_at"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        money = {"amount_cents": "BIGINT", "unit_cost_cents": "BIGINT", "list_price_cents": "BIGINT", "subtotal_cents": "BIGINT",
                 "total_cents": "BIGINT", "unit_price_cents": "BIGINT", "line_total_cents": "BIGINT", "freight_cost_cents": "BIGINT",
                 "credit_limit_cents": "BIGINT", "quantity": "INTEGER", "terms_days": "INTEGER"}
        builder.add_frame("customers", customers, primary_key=["customer_id"], foreign_keys=[], column_types=money, dirt=dirt.get("customers", ()),
                          descriptions={"segment": "enterprise | midmarket | smb (by revenue rank)", "terms_days": "current payment terms on the customer master"})
        builder.add_frame("suppliers", sup, primary_key=["supplier_id"], column_types={"payment_terms_days": "INTEGER", "deposit_pct": "DOUBLE"},
                          descriptions={"credit_hold": "supplier has put our account on credit hold", "terms_changed_at": "when the supplier last changed our terms"})
        builder.add_frame("products", prod_out, primary_key=["product_id"], column_types=money)
        builder.add_frame("orders", state.frames["orders"], primary_key=["order_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types=money, dirt=dirt.get("orders", ()), descriptions={"total_cents": "order value in cents (no tax: B2B resale)"})
        builder.add_frame("order_items", oi, column_types=money, descriptions={"backordered": "1 if the line could not ship from stock"})
        builder.add_frame("shipments", state.frames["shipments"], column_types=money)
        builder.add_frame("invoices", invoices, primary_key=["invoice_id"],
                          foreign_keys=[("order_id", "orders", "order_id"), ("customer_id", "customers", "customer_id")],
                          column_types={**money, "invoice_date": "DATE", "due_date": "DATE"}, dirt=dirt.get("invoices", ()),
                          descriptions={"ingest_id": "load sequence; >1 marks a re-ingested duplicate", "paid_at": "NULL while open",
                                        "invoice_date": "issued at shipment"},
                          description="Customer invoices (AR sub-ledger), one per shipped order.")
        builder.add_frame("payments", state.frames["payments"], primary_key=["payment_id"],
                          foreign_keys=[("invoice_id", "invoices", "invoice_id"), ("customer_id", "customers", "customer_id")],
                          column_types=money, description="Customer payments; each settles one invoice in full.")
        builder.add_frame("purchase_orders", po_out, column_types={**money, "ordered_at": "DATE", "expected_at": "DATE", "due_date": "DATE",
                                                                   "quantity_ordered": "INTEGER", "quantity_received": "INTEGER"},
                          descriptions={"paid_at": "vendor payment date (NULL = unpaid)", "status": "received | on_hold"})
        builder.add_frame("inventory_movements", mv, column_types={**money, "movement_id": "BIGINT"})

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, inventory, AP and revenue ties - all hold (nothing is 'wrong' in the books)."""
        cal = state.calendar
        inv = state.frames["invoices"]
        pay_at = inv["paid_at"]
        po = state.extras["po_paid"]
        products = state.frames["products"]
        D = state.extras
        ar, apd, invd, rev = {}, {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            issued = inv["invoice_date"] <= pd.Timestamp(e)
            open_ = pay_at.isna() | (pay_at > e_ts)
            ar[period] = int(inv.loc[issued & open_, "amount_cents"].sum())
            rec = po["received_at"] <= e_ts
            unpaid = po["paid_at"].isna() | (po["paid_at"] > e_ts)
            apd[period] = int(po.loc[rec & unpaid, "amount_cents"].sum())
            rev[period] = int(inv.loc[(inv["invoice_date"] >= pd.Timestamp(s)) & issued, "amount_cents"].sum())
            # inventory from movements (qty * cost)
            recq = po.loc[rec, "amount_cents"].sum()
            oi = state.frames["order_items"]
            ships = state.frames["shipments"].set_index("order_id")["shipped_at"]
            shipped_by = ships[ships <= e_ts].index
            sold = oi[oi["order_id"].isin(shipped_by)]
            invd[period] = int((D["opening_qty"] * products["unit_cost_cents"].to_numpy()).sum() + recq
                               - (sold["quantity"] * sold["unit_cost_cents"]).sum())
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Open invoices (amount, unpaid at period end) == GL 1100"),
            SubledgerTie("inventory_movements", invd, ["1200", "1210", "1220"], description="Sum(qty*unit_cost) of movements == GL inventory"),
            SubledgerTie("ap_open_purchase_orders", apd, ["2000"], description="Received, unpaid POs == GL 2000"),
            SubledgerTie("revenue_invoices", rev, ["4000"], basis="activity", description="Invoices issued in period == GL 4000 activity"),
        ]

    # ------------------------------------------------------------------ helpers for figures

    def _figs(self, state: WorldState) -> dict[str, Any]:
        """Period figures used by ground truth, dossier and simulation (cached)."""
        if "figs" in state.extras:
            return state.extras["figs"]
        st = state.statements
        assert st is not None
        L = state.ledger
        f: dict[str, Any] = {"ratios": st.ratios, "tb": {p: L.trial_balance(p) for p in state.periods}}
        prev = L.trial_balance(state.calendar.warmup_periods[-1])
        f["tb_prev"] = prev
        f["income"] = {i.period: i for i in st.income}
        f["cf"] = {c.period: c for c in st.cash_flow}
        f["bs"] = {b.period: b for b in st.balance}
        state.extras["figs"] = f
        return f

    def _covenant(self, state: WorldState) -> dict[str, Any]:
        p = state.params
        r8 = state.statements.ratios[state.periods[-1]]  # type: ignore[union-attr]
        # undefined ratios (None: no current liabilities / non-positive EBITDA with debt) -> covenant terms
        cr_v = r8["current_ratio"] if r8["current_ratio"] is not None else 99.0
        de_v = r8["debt_to_ebitda"] if r8["debt_to_ebitda"] is not None else 99.0
        cr_breach = cr_v < p["covenant_min_current_ratio"]
        de_breach = de_v > p["covenant_max_debt_ebitda"]
        kind = p["covenant_pref"]
        if kind == "current_ratio" and not cr_breach and de_breach:
            kind = "debt_to_ebitda"
        elif kind == "debt_to_ebitda" and not de_breach and cr_breach:
            kind = "current_ratio"
        breached = cr_breach if kind == "current_ratio" else de_breach
        value = cr_v if kind == "current_ratio" else de_v
        threshold = p["covenant_min_current_ratio"] if kind == "current_ratio" else p["covenant_max_debt_ebitda"]
        return {"type": kind, "breached": bool(breached), "value": float(value), "threshold": float(threshold)}

    def _revolver(self, state: WorldState) -> dict[str, float]:
        p = state.params
        drawn = -state.ledger.trial_balance(state.periods[-1])["2500"]
        peak = max(state.extras["books"]["revolver_hist"] + [drawn, 1])
        nominal = p["revolver_limit_pct_nominal"] * p["revenue_ttm_cents"]
        limit = max(nominal, peak / 0.995)
        limit = math.ceil(limit / 1_000_000) * 1_000_000
        return {"drawn": float(drawn), "limit": float(limit), "utilization": drawn / limit if limit else 0.0}

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Supply-chain policy, warehouse queueing and public KPIs by period."""
        p, cal = state.params, state.calendar
        f = self._figs(state)
        orders, oi = state.frames["orders"], state.frames["order_items"]
        kpis: dict[str, dict[str, float]] = {}
        oi_orders = orders.set_index("order_id")
        for period in state.periods:
            s, e = cal.bounds(period)
            sel = (orders["order_ts"] >= pd.Timestamp(s)) & (orders["order_ts"] < pd.Timestamp(e) + DAY)
            ids = orders.loc[sel, "order_id"]
            lines = oi[oi["order_id"].isin(ids)]
            inc = f["income"][period]
            r = f["ratios"][period]
            kpis[period] = {
                "revenue_cents": float(inc.revenue_cents), "gross_margin": r["gross_margin"], "net_income_cents": float(inc.net_income_cents),
                "operating_cash_flow_cents": float(f["cf"][period].operating_cents), "cash_cents": float(f["tb"][period]["1000"]),
                "orders": float(sel.sum()), "units_ordered": float(lines["quantity"].sum()),
                "avg_order_value_cents": float(oi_orders.loc[ids, "total_cents"].mean()) if len(ids) else 0.0,
                "backorder_line_rate": float(lines["backordered"].mean()) if len(lines) else 0.0,
                "inventory_turns_annualized": 365.0 / r["dio"] if r["dio"] else 0.0,
            }
        daily_orders = len(orders) / (len(state.extras["months"]) * 30.4)
        stations, summary = station_network(daily_orders / 8.0, [
            {"name": "pick", "capacity_per_hour": daily_orders / 8.0 / 0.72, "cs": 0.8},
            {"name": "pack", "capacity_per_hour": daily_orders / 8.0 / 0.81, "cs": 0.6},
            {"name": "ship_dock", "capacity_per_hour": daily_orders / 8.0 / 0.55, "cs": 1.0},
        ])
        q = mm1(daily_orders / 8.0, daily_orders / 8.0 / 0.81)
        D = state.extras["demand"]
        n_m = state.extras["months_n"]
        mean_d = D[:, :n_m].mean(axis=1)
        sd_d = D[:, :n_m].std(axis=1)
        unit_cost = state.frames["products"]["unit_cost_cents"].to_numpy()
        z = service_z(0.95)
        lt_months = (p["supplier_lead_days"] + p["lead_time_days"]) / 30.4
        top = int(np.argmax(mean_d))
        cover = state.extras["cover_by_month"]
        supply = {
            "lead_time_days": {"supplier_mean": p["supplier_lead_days"], "freight_ltl_mean": round(p["lead_time_days"], 2), "cv": 0.35},
            "policy": {"type": "periodic review order-up-to", "review_months": p["review_months"],
                       "target_cover_months_by_period": {per: float(cover[3 * (i + 1) + 2]) for i, per in enumerate(state.periods)}},
            "safety_stock_units_top_sku": safety_stock(z, float(sd_d[top]), lt_months),
            "eoq_units_top_sku": eoq(float(mean_d[top]) * 12, 25_000, max(1.0, unit_cost[top] * 0.25)),
            "receipts_to_issues_by_period": self._receipts_ratio(state),
        }
        unit = {"gross_margin": f["ratios"][state.periods[-1]]["gross_margin"],
                "arpa": kpis[state.periods[-1]]["avg_order_value_cents"], "cac": 0.0, "ltv": 0.0, "payback_months": 0.0, "churn_monthly": 0.0}
        return build_operations_state(stations=stations, queueing={**{k: v for k, v in q.items()}, **{"throughput": summary["throughput"]}},
                                      unit_economics=unit, supply_chain=supply,
                                      pricing={"terms_elasticity_revenue_per_day": 0.004, "list_price_policy": "annual list price, no discounts"},
                                      kpis_by_period=kpis)

    def _receipts_ratio(self, state: WorldState) -> dict[str, float]:
        mv = state.frames["inventory_movements"]
        out = {}
        for period in state.periods:
            s, e = state.calendar.bounds(period)
            sel = (mv["movement_ts"] >= pd.Timestamp(s)) & (mv["movement_ts"] < pd.Timestamp(e) + DAY)
            val = mv.loc[sel, "quantity"] * mv.loc[sel, "unit_cost_cents"]
            rec = float(val[mv.loc[sel, "movement_type"] == "receipt"].sum())
            iss = float(-val[mv.loc[sel, "movement_type"] == "sale"].sum())
            out[period] = rec / iss if iss else 0.0
        return out

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Nine expected metrics (archetypes.md section 1 table), reference SQL and evidence trail."""
        p, cal = state.params, state.calendar
        f = self._figs(state)
        P = state.periods
        P5, P6, P8 = P[4], P[5], P[7]
        ratios = f["ratios"]
        tb, tb_prev = f["tb"], f["tb_prev"]
        w_first = P[-p["window_periods"]]
        pre = [x for x in P if x < w_first]
        dio_base_real = float(np.mean([ratios[x]["dio"] for x in pre]))

        def bal(period: str, codes: tuple[str, ...]) -> int:
            return sum(tb[period][c] for c in codes)

        def prev_bal(period: str, codes: tuple[str, ...]) -> int:
            i = P.index(period)
            src = tb[P[i - 1]] if i > 0 else tb_prev
            return sum(src[c] for c in codes)

        inv_codes = ("1200", "1210", "1220", "1230")
        drag = sum((bal(x, ("1100",)) - prev_bal(x, ("1100",))) + (bal(x, inv_codes) - prev_bal(x, inv_codes))
                   - (-(bal(x, ("2000",))) + prev_bal(x, ("2000",))) for x in P[5:])
        cfo_ni = sum(f["cf"][x].operating_cents - f["income"][x].net_income_cents for x in P[5:])
        inv = state.frames["invoices"]
        s6 = pd.Timestamp(cal.bounds(P6)[0])
        e8 = pd.Timestamp(cal.bounds(P8)[1])
        win = inv[(inv["invoice_date"] >= s6) & (inv["invoice_date"] <= e8)]
        late_share = float(win.loc[win["terms_days"] > p["terms_days_base"], "amount_cents"].sum() / win["amount_cents"].sum())
        days8 = cal.days(P8)
        cogs8 = f["income"][P8].revenue_cents - f["income"][P8].gross_profit_cents
        inv8 = bal(P8, inv_codes)
        excess = inv8 - cogs8 / days8 * dio_base_real
        rv = self._revolver(state)
        cov = self._covenant(state)
        # runway: cash_P8 / avg monthly (opex + interest - CFO_ex_WC) over P6..P8
        burn_q = []
        for x in P[5:]:
            act = state.ledger.activity(x)
            opex = sum(act[c] for c in ("6000", "6010", "6100", "6200", "6210", "6300", "6310", "6400", "6500"))
            interest = act["7000"]
            cf = f["cf"][x]
            cfo_ex_wc = f["income"][x].net_income_cents + sum(
                ln.cents for ln in cf.lines if ln.caption in ("Depreciation & amortization", "Provision for doubtful accounts, net of write-offs",
                                                               "Inventory reserve and write-downs"))
            burn_q.append(opex + interest - cfo_ex_wc)
        burn_m = float(np.mean(burn_q)) / 3.0
        cash8 = tb[P8]["1000"]
        runway = min(60.0, cash8 / burn_m) if burn_m > 0 else 60.0
        drift_realized = ratios[P8]["dso"] - ratios[P5]["dso"]
        metrics = [
            metric("ccc_p8_days", ratios[P8]["ccc"], "days", f"DSO + DIO - DPO for {P8} from statements.ratios "
                   f"({ratios[P8]['dso']:.1f} + {ratios[P8]['dio']:.1f} - {ratios[P8]['dpo']:.1f}); days = days in quarter, AR net incl. allowance.",
                   pillar="accounting_finance", tol_abs=3.0, label="Cash conversion cycle, latest quarter"),
            metric("ccc_delta_p5_to_p8_days", ratios[P8]["ccc"] - ratios[P5]["ccc"], "days", f"CCC({P8}) - CCC({P5}).",
                   pillar="accounting_finance", tol_abs=4.0, label="Change in CCC over the last three quarters"),
            metric("delta_wc_cash_drag_p6_p8_cents", drag, "cents", f"Sum over {P6}..{P8} of (delta AR gross + delta inventory - delta AP) from the trial balance.",
                   pillar="accounting_finance", tol_rel=0.05, label="Working-capital cash drag, last three quarters"),
            metric("cfo_minus_ni_p6_p8_cents", cfo_ni, "cents", f"Sum over {P6}..{P8} of (CFO - net income) from the cash-flow statements.",
                   pillar="accounting_finance", tol_rel=0.05, label="Accrual gap (CFO minus net income)"),
            metric("late_terms_customer_revenue_share", late_share, "ratio",
                   f"Invoice revenue {P6}..{P8} on terms_days > {p['terms_days_base']} / total invoice revenue {P6}..{P8} (deduplicated invoices).",
                   pillar="sql", tol_abs=0.04, label="Revenue share on extended terms"),
            metric("excess_inventory_cents", excess, "cents",
                   f"Inventory({P8}) - COGS({P8})/days * DIO_base, DIO_base = mean DIO of {pre[0]}..{pre[-1]} = {dio_base_real:.1f} days.",
                   pillar="operations_strategy", tol_rel=0.10, label="Excess inventory vs baseline days"),
            metric("revolver_utilization_p8", rv["utilization"], "ratio", f"Revolver drawn at {P8} end (GL 2500) / committed limit ${rv['limit'] / 100:,.0f}.",
                   pillar="accounting_finance", tol_abs=0.03, label="Revolver utilization"),
            metric("covenant_breached", 1.0 if cov["breached"] else 0.0, "bool",
                   f"{cov['type']} at {P8} = {cov['value']:.2f} vs covenant {'min' if cov['type'] == 'current_ratio' else 'max'} {cov['threshold']:.2f} (1 = breached).",
                   pillar="accounting_finance", tol_abs=0.0, label="Covenant breached (1/0)"),
            metric("cash_runway_months", runway, "months",
                   f"Cash({P8}) / average monthly (opex + interest - CFO before working-capital changes) over {P6}..{P8}; capped at 60.",
                   pillar="accounting_finance", tol_abs=1.0, label="Cash runway"),
        ]
        # ---------------- reference SQL
        dedupe = "SELECT * FROM invoices QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1"
        sql_dso = f"""
WITH inv0 AS ({dedupe}),
inv AS (
  SELECT date_trunc('quarter', invoice_date) q, SUM(amount_cents) rev FROM inv0 GROUP BY 1),
ar AS (
  SELECT q, SUM(open_cents) ar FROM (
    SELECT date_trunc('quarter', i.invoice_date) q,
           i.amount_cents - COALESCE(SUM(p.amount_cents), 0) open_cents
    FROM inv0 i LEFT JOIN payments p ON p.invoice_id = i.invoice_id
      AND CAST(p.paid_at AS DATE) <= last_day_of_quarter(i.invoice_date)
    GROUP BY 1, i.invoice_id, i.amount_cents) GROUP BY 1)
SELECT period_of(inv.q) AS period, 91.25 * ar.ar / inv.rev AS dso_days FROM inv JOIN ar USING (q) ORDER BY 1"""

        def dso_checks(df: pd.DataFrame) -> dict[str, Any]:
            s = df.set_index("period")["dso_days"]
            w = [x for x in P if x >= w_first]
            vals = [float(s.get(x, float("nan"))) for x in w]
            mono = all(b >= a - 0.5 for a, b in zip(vals, vals[1:], strict=False))
            d = float(s.get(P8, 0.0) - s.get(P5, 0.0))
            return {f"dso_monotone_from_{w_first}": mono, "dso_p8_minus_p5": d, "meets_0_6_drift_floor": d >= 0.6 * p["dso_drift"]}

        q_dso = run_reference_query(con, "dso_by_quarter_subledger", sql_dso, signature=["qualify", "left join", "group by", "date_trunc("],
                                    metric_keys=["ccc_p8_days", "ccc_delta_p5_to_p8_days"], checks=dso_checks)
        sql_terms = f"""
SELECT TRIM(LOWER(c.segment)) AS segment, AVG(i.terms_days) AS avg_terms, SUM(i.amount_cents) AS rev,
       SUM(CASE WHEN i.terms_days > {p['terms_days_base']} THEN i.amount_cents ELSE 0 END) * 1.0 / SUM(i.amount_cents) AS extended_share
FROM ({dedupe}) i JOIN customers c USING (customer_id)
WHERE i.invoice_date >= DATE '{cal.bounds(P6)[0]}' AND i.invoice_date <= DATE '{cal.bounds(P8)[1]}'
GROUP BY 1 ORDER BY rev DESC"""
        q_terms = run_reference_query(con, "terms_creep_by_segment", sql_terms, signature=["join", "group by", "case when", "avg("],
                                      metric_keys=["late_terms_customer_revenue_share"],
                                      checks=lambda df: {"top_segment": str(df.iloc[0]["segment"]), "max_avg_terms": float(df["avg_terms"].max())})
        sql_inv = """
SELECT period_of(movement_ts) AS period,
       SUM(CASE WHEN movement_type = 'receipt' THEN quantity * unit_cost_cents ELSE 0 END) AS receipts_cents,
       -SUM(CASE WHEN movement_type = 'sale' THEN quantity * unit_cost_cents ELSE 0 END) AS issues_cents,
       SUM(CASE WHEN movement_type = 'receipt' THEN quantity * unit_cost_cents ELSE 0 END) * 1.0
         / NULLIF(-SUM(CASE WHEN movement_type = 'sale' THEN quantity * unit_cost_cents ELSE 0 END), 0) AS receipts_to_issues
FROM inventory_movements GROUP BY 1 ORDER BY 1"""
        q_inv = run_reference_query(con, "inventory_receipts_vs_issues", sql_inv, signature=["case when", "group by", "sum("],
                                    metric_keys=["excess_inventory_cents"],
                                    checks=lambda df: {"max_receipts_to_issues_in_window": float(df.loc[df["period"] >= w_first, "receipts_to_issues"].max())})
        sql_rev = """
WITH m AS (
  SELECT j.period, SUM(l.credit_cents - l.debit_cents) AS net_draw_cents
  FROM gl_lines l JOIN gl_journal j USING (entry_id)
  WHERE l.account_code = '2500' GROUP BY 1)
SELECT period, net_draw_cents, SUM(net_draw_cents) OVER (ORDER BY period) AS revolver_balance_cents FROM m ORDER BY period"""
        q_rev = run_reference_query(con, "revolver_running_balance", sql_rev, signature=["join", "group by", "over("],
                                    metric_keys=["revolver_utilization_p8", "covenant_breached", "cash_runway_months"],
                                    checks=lambda df: {"balance_last_cents": float(df["revolver_balance_cents"].iloc[-1]), "limit_cents": rv["limit"]})
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_dso, q_terms],
            "magnitude": {**anomalies["A1"].magnitude, "late_terms_customer_revenue_share": round(late_share, 6),
                          "dso_increase_p5_to_p8_days": round(drift_realized, 4)},
            "evidence_trail": [f"DSO rose from {ratios[P5]['dso']:.1f} days ({P5}) to {ratios[P8]['dso']:.1f} ({P8}).",
                               f"{late_share:.0%} of {P6}-{P8} invoice revenue carries terms above net-{p['terms_days_base']}, concentrated in large accounts."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_inv],
            "magnitude": {**anomalies["A2"].magnitude, "excess_inventory_cents": round(excess, 2), "dio_base_days": round(dio_base_real, 4)},
            "evidence_trail": [f"DIO rose from {dio_base_real:.1f} (pre-window mean) to {ratios[P8]['dio']:.1f} days.",
                               "inventory_movements: receipts outpace issues every quarter of the window."]})
        if "A3" in anomalies:
            sql_ap = """
SELECT period_of(received_at) AS period, COUNT(*) AS pos,
       AVG(date_diff('day', CAST(received_at AS DATE), CAST(paid_at AS DATE))) AS avg_days_to_pay,
       COUNT(*) FILTER (WHERE status = 'on_hold') AS on_hold_pos
FROM purchase_orders WHERE paid_at IS NOT NULL OR status = 'on_hold' GROUP BY 1 ORDER BY 1"""
            q_ap = run_reference_query(con, "supplier_payment_lag", sql_ap, signature=["date_diff(", "group by", "avg("],
                                       metric_keys=["ccc_p8_days"],
                                       checks=lambda df: {"max_avg_days_to_pay": float(df["avg_days_to_pay"].max())})
            anomalies["A3"] = anomalies["A3"].model_copy(update={
                "reference_sql": [q_ap],
                "evidence_trail": [f"DPO moved from {ratios[P5]['dpo']:.1f} to {ratios[P8]['dpo']:.1f} days.",
                                   "suppliers.credit_hold / shortened payment_terms_days and purchase_orders.status='on_hold' appear in the last quarter.",
                                   "order_items.backordered = 1 for fast movers in the last quarter."]})
        anomalies["A4"] = anomalies["A4"].model_copy(update={
            "reference_sql": [q_rev],
            "magnitude": {"revolver_utilization_p8": round(rv["utilization"], 6), "revolver_limit_cents": rv["limit"],
                          "covenant_breached": 1.0 if cov["breached"] else 0.0, "cash_runway_months": round(runway, 4)},
            "evidence_trail": [f"Revolver balance {rv['drawn'] / 100:,.0f} USD vs limit {rv['limit'] / 100:,.0f} USD.",
                               f"Covenant ({cov['type']}) at {P8}: {cov['value']:.2f} vs {cov['threshold']:.2f}."]})
        ordered = [anomalies[k] for k in ("A1", "A2", "A3", "A4") if k in anomalies]
        gm_dip = ratios[P5]["gross_margin"] - ratios[P8]["gross_margin"]
        herrings_all = [
            f"Gross margin is compressing - it dipped {gm_dip * 100:.1f} pts by {P8} on freight surcharges, roughly "
            f"{abs(gm_dip) * sum(f['income'][x].revenue_cents for x in P[5:]) / max(1, abs(drag)):.0%} of the working-capital drag.",
            f"Sales slowed - revenue is up {ratios[P8]['revenue_growth_yoy']:+.0%} year over year in {P8} "
            f"({', '.join(f'{ratios[x]['revenue_growth_qoq']:+.0%}' for x in P[5:])} q/q over {P6}-{P8}); growth is what consumes the working capital.",
            f"The warehouse capex did it - {state.extras.get('decoy_capex_cents', 0) / 100:,.0f} USD one-off in {P[6]}, small next to the working-capital build.",
        ]
        n_rh = p["n_red_herrings"]
        herrings = ([herrings_all[1]] if n_rh == 1 else herrings_all[:2] if n_rh == 2 else herrings_all)
        root = (f"Nothing in the books is wrong: the company is profitable on an accrual basis but its cash is trapped in working capital. "
                f"From {w_first} the largest customers were moved to longer payment terms (DSO {ratios[P5]['dso']:.0f} -> {ratios[P8]['dso']:.0f} days) "
                f"while an over-forecast purchasing rule built inventory (DIO {dio_base_real:.0f} -> {ratios[P8]['dio']:.0f} days), so the cash conversion cycle "
                f"lengthened by {ratios[P8]['ccc'] - ratios[P5]['ccc']:.0f} days and working capital absorbed {drag / 100:,.0f} USD over {P6}-{P8}. "
                + (f"Stretching suppliers (DPO {ratios[P5]['dpo']:.0f} -> {ratios[P8]['dpo']:.0f}) triggered credit holds and back-orders. " if "A3" in anomalies else "")
                + f"The gap was funded by the revolver, now {rv['utilization']:.0%} drawn; the {cov['type'].replace('_', ' ')} covenant is "
                + ("breached." if cov["breached"] else "not yet breached but has little headroom."))
        python_ref = (
            "import pandas as pd\n"
            "tb = pd.read_sql(\"SELECT j.period, l.account_code, SUM(l.debit_cents - l.credit_cents) AS net FROM gl_lines l JOIN gl_journal j USING (entry_id) GROUP BY 1, 2\", con)\n"
            "bal = tb.pivot_table(index='period', columns='account_code', values='net', aggfunc='sum').fillna(0).sort_index().cumsum()\n"
            "wc = bal.get('1100', 0) + bal[[c for c in ('1200', '1210', '1220', '1230') if c in bal]].sum(axis=1) + bal.get('2000', 0)\n"
            "print(wc.diff())  # quarterly working-capital build (cash drag)\n"
            "# 12-month cash forecast, three scenarios: collect at base terms / liquidate excess inventory at 70c / both\n"
        )
        return GroundTruth(
            root_cause=root, anomalies=ordered, red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q_dso.sql, q_terms.sql, q_inv.sql, q_rev.sql], python=[python_ref],
                memo_bullets=[
                    "Accrual profit is not cash: CFO trails net income because AR and inventory absorbed the growth.",
                    f"CCC lengthened {ratios[P8]['ccc'] - ratios[P5]['ccc']:.0f} days ({P5} -> {P8}); quantify the drag by component.",
                    "Terms creep is concentrated in the largest accounts - target them rather than a blanket credit policy.",
                    f"Excess inventory of ~{excess / 100:,.0f} USD vs baseline days: fix the forecast rule, liquidate slow movers.",
                    "Stretching suppliers has already triggered holds and back-orders; it is not a free lever.",
                    "Revolver near its limit: covenant risk is the near-term issue; refinance or raise only alongside the CCC fix.",
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
        names = rng.pick_names(4)
        growth = [f["ratios"][x]["revenue_growth_qoq"] for x in P[4:]]
        reveal = [
            f"Revenue has grown every quarter ({', '.join(f'{g:+.0%}' for g in growth)} q/q over {P[4]}-{P[7]}) and {P[7]} is a record quarter.",
            "Board materials call the latest quarter a 'record quarter' and show revenue and EBITDA only.",
            "The CFO notes the revolving credit line is 'temporarily fully drawn'.",
            "A supplier emailed about late payments on recent invoices.",
        ]
        if p["decoy_capex_pct"]:
            reveal.append(f"New warehouse racking was installed in {P[6]}.")
        conceal = [
            "cash conversion cycle", "extended payment terms for the largest customers (payment terms creep)",
            "the over-forecast purchasing rule", "covenant status and covenant test results",
            "excess inventory value", "A1", "A2", "A3", "A4",
        ]
        stakeholders = [
            Stakeholder(name=names[0], role="CEO", agenda="Keep the growth narrative intact for the board.", disposition="confident, defensive",
                        knows=["the revolver is maxed"], hides=["approved extended terms for the top accounts"]),
            Stakeholder(name=names[1], role="CFO", agenda="Refinance the revolver before the covenant test.", disposition="anxious",
                        knows=["the covenant test is next month"], hides=["inventory carries no write-down"]),
            Stakeholder(name=names[2], role="VP Sales", agenda="Hit quota.", disposition="combative",
                        knows=["gave 90-day terms to large accounts"], hides=["frames long terms as 'enterprise standard'"]),
            Stakeholder(name=names[3], role="Head of Supply Chain", agenda="Avoid stockouts at all costs.", disposition="cooperative",
                        knows=["the demand forecast was aggressive"], hides=["suppliers have put the account on hold"]),
        ]
        tl = [
            TimelineEvent(period=P[4], headline="Aggressive growth plan approved", detail="Board approves a Series B-style growth plan."),
            TimelineEvent(period=P[5], headline="Top-3 customer renews at larger volume", detail="Renewal announced; commercial terms not discussed."),
            TimelineEvent(period=P[6], headline="Revolver drawn to fund inventory for peak season", detail="Treasury draws on the line."),
            TimelineEvent(period=P[7], headline="Record revenue quarter", detail="Revenue and EBITDA at all-time highs."),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[6], headline="Freight surcharges hit", detail="Carriers add fuel and peak surcharges.", is_red_herring=True))
        if p["decoy_capex_pct"]:
            tl.append(TimelineEvent(period=P[5], headline="Warehouse expansion approved", detail="New racking for the central warehouse.", is_red_herring=True))
        if p["payables_stretch"]:
            tl.append(TimelineEvent(period=P[7], headline="Two suppliers move us to prepayment", detail="Supplier relations escalate."))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["board_deck_excerpt", "bank_covenant_letter_excerpt", "supplier_email", "ar_aging_export"],
                           architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Seven levers of the spec; defaults are the P8 status quo."""
        p = state.params
        f = self._figs(state)
        P8 = state.periods[-1]
        inv = state.frames["invoices"]
        s8 = pd.Timestamp(state.calendar.bounds(P8)[0])
        last = inv[inv["invoice_date"] >= s8]
        terms_now = float(np.clip(round((last["terms_days"] * last["amount_cents"]).sum() / max(1, last["amount_cents"].sum())), 15, 90))
        rv = self._revolver(state)
        cover_now = float(np.clip(f["ratios"][P8]["dio"] / 30.4, 0.5, 3.0))
        return [
            Lever(key="credit_terms_days", label="Customer credit terms", min=15, max=90, default=terms_now, unit="days",
                  effect="DSO follows terms; each day tightened below today costs ~0.4% of revenue (lost orders)."),
            Lever(key="early_pay_discount_pct", label="Early-payment discount", min=0, max=3, default=0, unit="%",
                  effect="Collects 0.25 x pct of AR early; costs the discount on the collected amount."),
            Lever(key="inventory_liquidation_pct", label="Liquidate excess inventory", min=0, max=60, default=0, unit="% of excess",
                  effect="Cash at ~72% recovery; the haircut is written down; DIO falls."),
            Lever(key="purchase_cover_months", label="Purchasing cover", min=0.5, max=3, default=round(cover_now, 2), unit="months",
                  effect="Sets future inventory; below 1 month stockouts rise ~15 pts per month cut."),
            Lever(key="vendor_terms_days", label="Days to pay suppliers", min=30, max=75,
                  default=float(np.clip(round(f["ratios"][P8]["dpo"]), 30, 75)), unit="days",
                  effect="Holds cash in AP; above ~50 days supplier holds and back-orders rise."),
            Lever(key="revolver_draw_cents", label="Revolver balance", min=0, max=rv["limit"], default=rv["drawn"], unit="cents",
                  effect="Cash now, interest later; covenant retested each quarter."),
            Lever(key="equity_raise_cents", label="Equity raise", min=0, max=round(0.2 * p["revenue_ttm_cents"]), default=0, unit="cents",
                  effect="Cash with dilution; does not fix the operating cause."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model (see module docstring for the linearisation)."""
        p = state.params
        f = self._figs(state)
        P = state.periods
        P8 = P[-1]
        r8 = f["ratios"][P8]
        tb8 = f["tb"][P8]
        inc8 = f["income"][P8]
        days = state.calendar.days(P8)
        rev = float(inc8.revenue_cents)
        cogs = float(inc8.revenue_cents - inc8.gross_profit_cents)
        ar = float(tb8["1100"] + tb8["1110"])
        inventory = float(sum(tb8[c] for c in ("1200", "1210", "1220", "1230")))
        gt_excess = max(0.0, inventory - cogs / days * float(np.mean([f["ratios"][x]["dio"] for x in P if x < P[-p["window_periods"]]])))
        rv = self._revolver(state)
        oi = state.frames["order_items"]
        orders = state.frames["orders"]
        s8 = pd.Timestamp(state.calendar.bounds(P8)[0])
        ids = orders.loc[orders["order_ts"] >= s8, "order_id"]
        stockout = float(oi.loc[oi["order_id"].isin(ids), "backordered"].mean()) if len(ids) else 0.0
        kpis = {
            "revenue_cents": rev, "net_income_cents": float(inc8.net_income_cents), "cash_cents": float(tb8["1000"]),
            "ar_cents": ar, "inventory_cents": inventory, "ap_cents": float(-tb8["2000"]), "revolver_cents": rv["drawn"],
            "dso_days": r8["dso"], "dio_days": r8["dio"], "dpo_days": r8["dpo"], "ccc_days": r8["ccc"],
            "current_ratio": r8["current_ratio"] if r8["current_ratio"] is not None else 99.0,
            "debt_to_ebitda": r8["debt_to_ebitda"] if r8["debt_to_ebitda"] is not None else 10.0, "stockout_rate": stockout,
            "operating_cash_flow_cents": float(f["cf"][P8].operating_cents),
        }
        rev_day, cogs_day = rev / days, cogs / days
        recovery = 0.72
        effects = [
            {"lever": "credit_terms_days", "kpi": "dso_days", "delta_per_unit": 1.0, "lag_periods": 0},
            {"lever": "credit_terms_days", "kpi": "ar_cents", "delta_per_unit": rev_day, "lag_periods": 0},
            {"lever": "credit_terms_days", "kpi": "cash_cents", "delta_per_unit": -rev_day, "lag_periods": 0},
            {"lever": "credit_terms_days", "kpi": "ccc_days", "delta_per_unit": 1.0, "lag_periods": 0},
            {"lever": "credit_terms_days", "kpi": "revenue_cents", "delta_per_unit": 0.004 * rev, "lag_periods": 1},
            {"lever": "early_pay_discount_pct", "kpi": "cash_cents", "delta_per_unit": 0.25 * ar * (1 - 0.015), "lag_periods": 0},
            {"lever": "early_pay_discount_pct", "kpi": "ar_cents", "delta_per_unit": -0.25 * ar, "lag_periods": 0},
            {"lever": "early_pay_discount_pct", "kpi": "dso_days", "delta_per_unit": -0.25 * r8["dso"], "lag_periods": 0},
            {"lever": "early_pay_discount_pct", "kpi": "net_income_cents", "delta_per_unit": -0.25 * ar * 0.015, "lag_periods": 0},
            {"lever": "inventory_liquidation_pct", "kpi": "cash_cents", "delta_per_unit": gt_excess / 100 * recovery, "lag_periods": 0},
            {"lever": "inventory_liquidation_pct", "kpi": "inventory_cents", "delta_per_unit": -gt_excess / 100, "lag_periods": 0},
            {"lever": "inventory_liquidation_pct", "kpi": "net_income_cents", "delta_per_unit": -gt_excess / 100 * (1 - recovery), "lag_periods": 0},
            {"lever": "inventory_liquidation_pct", "kpi": "dio_days", "delta_per_unit": -gt_excess / 100 / cogs_day if cogs_day else 0.0, "lag_periods": 0},
            {"lever": "purchase_cover_months", "kpi": "inventory_cents", "delta_per_unit": cogs / 3, "lag_periods": 1},
            {"lever": "purchase_cover_months", "kpi": "cash_cents", "delta_per_unit": -cogs / 3, "lag_periods": 1},
            {"lever": "purchase_cover_months", "kpi": "dio_days", "delta_per_unit": 30.4, "lag_periods": 1},
            {"lever": "purchase_cover_months", "kpi": "stockout_rate", "delta_per_unit": -0.15, "lag_periods": 1},
            {"lever": "vendor_terms_days", "kpi": "dpo_days", "delta_per_unit": 1.0, "lag_periods": 0},
            {"lever": "vendor_terms_days", "kpi": "ap_cents", "delta_per_unit": cogs_day, "lag_periods": 0},
            {"lever": "vendor_terms_days", "kpi": "cash_cents", "delta_per_unit": cogs_day, "lag_periods": 0},
            {"lever": "vendor_terms_days", "kpi": "ccc_days", "delta_per_unit": -1.0, "lag_periods": 0},
            {"lever": "vendor_terms_days", "kpi": "stockout_rate", "delta_per_unit": 0.004, "lag_periods": 1},
            {"lever": "revolver_draw_cents", "kpi": "cash_cents", "delta_per_unit": 1.0, "lag_periods": 0},
            {"lever": "revolver_draw_cents", "kpi": "revolver_cents", "delta_per_unit": 1.0, "lag_periods": 0},
            {"lever": "revolver_draw_cents", "kpi": "net_income_cents", "delta_per_unit": -p["interest_rate"] / 4, "lag_periods": 0},
            {"lever": "equity_raise_cents", "kpi": "cash_cents", "delta_per_unit": 1.0, "lag_periods": 0},
        ]
        n = len(P[5:])
        drag = {
            "cash_cents": -float(sum(f["income"][x].net_income_cents - f["cf"][x].operating_cents for x in P[5:])) / n,
            "dso_days": (r8["dso"] - f["ratios"][P[4]]["dso"]) / n,
            "dio_days": (r8["dio"] - f["ratios"][P[4]]["dio"]) / n,
            "ccc_days": (r8["ccc"] - f["ratios"][P[4]]["ccc"]) / n,
        }
        # root-cause fixes are reductions (review P1 #8): tighten customer terms back toward the base and cut the
        # purchasing cover back toward the pre-window DIO. Module 3 has no OR-groups, so both must hold.
        lv = {x.key: x for x in self.levers(ctx, state)}
        terms_now = float(lv["credit_terms_days"].default)
        terms_thr = float(min(terms_now - 1, p["terms_days_base"] + max(2.0, 0.25 * (terms_now - p["terms_days_base"]))))
        dio_pre = float(np.mean([f["ratios"][x]["dio"] for x in P if x < P[-p["window_periods"]]]))
        cover_now = float(lv["purchase_cover_months"].default)
        cover_thr = float(np.clip(min(cover_now - 0.25, dio_pre / 30.4 + 0.25), 0.5, 3.0))
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"credit_terms_days": at_most(round(terms_thr)), "purchase_cover_months": at_most(round(cover_thr, 2))},
                "bounds": {"dso_days": [0.0, 365.0], "dio_days": [0.0, 365.0], "dpo_days": [0.0, 365.0], "stockout_rate": [0.0, 1.0],
                           "ar_cents": [0.0, None], "inventory_cents": [0.0, None], "ap_cents": [0.0, None],
                           "revolver_cents": [0.0, float(self._revolver(state)["limit"])]}}


def _split(total: int, weights: list[float]) -> list[int]:
    return split_cents(total, weights)
