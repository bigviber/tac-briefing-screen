"""``abc_cost_distortion`` - "Peanut-Butter Overhead" (archetypes.md section 2).

A make-to-order manufacturer with three product families (high-volume simple, mid, low-volume
complex) costs products at standard with a single plant-wide overhead rate per direct-labour hour.
Prices are standard cost x a family markup, so the low-volume "premium" family *looks* like the
best line; reps are paid on reported margin and the mix drifts toward it. Its setups, moves and
engineering changes are far more expensive than its labour hours suggest: overhead actuals rise,
the under-absorbed variance grows every quarter and operating income falls while reported margins
look fine. No fraud: a measurement error the production data can correct (time-driven ABC).

Mechanics
---------
* 3 families x 12-40 SKUs; unit volume Dirichlet(8, 3, 1); routings 3 / 4 / 6 operations over 6-10
  work centres; setup minutes per operation (20, 45, 110) x U(0.8, 1.2); run minutes per unit grow
  with complexity; the finishing centre is the bottleneck and the high-volume family uses most of it.
* Monthly make-to-order: each SKU-month demand is produced in lots of a fixed family lot size (so
  setups scale with volume), one ``work_orders`` row per lot, one ``production_log`` row per operation
  (setup and run minutes, good and scrap quantity), ``quality_events`` (engineering changes /
  inspections) at a family-specific rate per work order; every work order ships to a customer
  (``sales_orders``) the same month and is invoiced at shipment on net-30/45.
* Standard cost = RM (``bom``) + run hours x labour rate + run hours x plant-wide rate, rate =
  budgeted overhead / budgeted DLH with overhead = ``overhead_pct_of_conversion`` of conversion cost,
  set on the warm-up quarter's volumes ("the standard was last reset before P1").
* Hidden TDABC cost: four pools (setups on setup minutes, material handling on operation moves,
  engineering on quality events, volume on run hours), pool rate = pool cost / practical capacity
  (``practical_capacity_pct`` of capacity is used, so only that share is assigned). The complexity
  pool size is solved on the warm-up drivers so the low-volume family's distortion
  ``(std - abc)/abc`` equals ``-distortion_target``; the remainder is the volume pool.
* Spending: complexity pools spend ``rate / util`` per driver unit actually used (crews and engineers
  are added as setups grow), the volume pool is fixed per month; the ``overhead_pools`` table reports
  cost, driver quantity and practical capacity per quarter.
* GL (monthly production batches, sales/collections in ``GL_BATCH``): RM purchases Dr 1200 / Cr 2000;
  issues Dr 1210 / Cr 1200; direct labour Dr 1210 / Cr 2110; overhead absorbed Dr 1210 / Cr 5020 at the
  standard rate x DLH; actual overhead Dr 5020 / Cr 2100 (and plant depreciation Cr 1510); completion
  Dr 1220 / Cr 1210 at standard x good units; scrap (the WIP residual) Dr 5040 / Cr 1210; COGS at
  standard Dr 5000 / Cr 1220; commissions on reported margin Dr 6010 / Cr 2110. The 5020 net activity
  of a quarter is the under/over-absorbed overhead.

Anomalies
---------
* **A1 cost distortion** (``cost_allocation_distortion``): the plant-wide DLH rate under-costs the
  low-volume family by ``distortion_target`` and over-costs the high-volume family.
* **A2 mix drift** (``incentive_mix_drift``): from P1 the low-volume family's revenue share rises +2pt
  a quarter because reps are paid on reported margin (``sales_reps.commission_basis``); the
  under-absorbed variance and operating income show it.
* **A3** (principal) **bottleneck economics** (``throughput_constraint``): per finishing (bottleneck)
  minute the low-volume family earns the most throughput - the answer is "reprice", not "drop".
* Red herrings: scrap rises slightly in the complex family; labour rate flat; +1pt discount creep on
  the high-volume family. All invariants and sub-ledger ties hold (no designed break).

Documented simplifications
* Industry ``manufacturing`` only (``manufacturing_mes`` schema, extended with ``sales_orders``
  payment columns, ``overhead_pools``, ``sales_reps``, ``customers``, ``suppliers`` /
  ``purchase_orders``); work orders complete in the month they start (no WIP across month ends).
* Family markups differ (high m+0.05, mid m+0.15, low m+0.25) so the premium family *appears* most
  profitable, as the spec narrative requires.
* Quarter simulation (reduced form, Appendix B.4): price levers use the spec elasticities (low -1.2,
  high -1.8) linearised at P8; ``discontinue_family_low`` removes revenue and the avoidable 60% of
  its activity cost; ``setup_reduction_pct`` saves setup-pool cost (lag 1) for a one-off 1.5% of
  revenue; ``commission_basis = 1`` (ABC) reverses the drift 1pt a quarter (the spec's sixth lever,
  ``min_batch_size_low``, is folded into the SMED program to keep five levers). ``resolved_if``: low-family price +10% and ABC commission basis.
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
    Archetype,
    BuildContext,
    WorldState,
    difficulty_pick,
    magnitude,
    metric,
    run_reference_query,
)
from axiom.engine.datagen import GL_BATCH, TableBuilder
from axiom.engine.finance import SubledgerTie, batch_end_dates, cents, cents_array, split_cents
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state, station_network
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

logger = logging.getLogger("axiom.archetypes.abc_cost_distortion")

EOD = pd.Timedelta(hours=23, minutes=59, seconds=59)
FAMILY_SETS = (("Standard valves", "Pumps", "Flow controllers"), ("Brackets", "Housings", "Precision actuators"),
               ("Fittings", "Manifolds", "Custom assemblies"))
FAMILY_KEYS = ("high", "mid", "low")
WORK_CENTERS = (("WC01", "Cutting"), ("WC02", "Machining"), ("WC03", "Assembly"), ("WC04", "Finishing"), ("WC05", "Inspection"),
                ("WC06", "Packaging"), ("WC07", "Welding"), ("WC08", "Testing"), ("WC09", "Painting"), ("WC10", "Kitting"))
BOTTLENECK = "WC04"
# family routing templates: (work centre, run minutes per unit range)
ROUTINGS = {
    "high": [("WC01", (0.4, 0.8)), ("WC04", (2.5, 3.5)), ("WC06", (0.3, 0.6))],
    "mid": [("WC01", (0.8, 1.5)), ("WC02", (2.0, 4.0)), ("WC04", (1.5, 2.5)), ("WC06", (0.5, 0.9))],
    "low": [("WC02", (1.2, 2.2)), ("WC07", (1.0, 2.0)), ("WC03", (1.5, 2.5)), ("WC04", (0.8, 1.4)), ("WC05", (0.8, 1.6)), ("WC08", (0.8, 1.6))],
}
RM_BASE = {"high": 6.0, "mid": 18.0, "low": 30.0}
LOT_MULT = {"high": 1.0, "mid": 1.0, "low": 0.5}  # the complex line runs in small batches (two lots per SKU-month)
SETUP_BASE = {"high": 20.0, "mid": 45.0, "low": 110.0}
QE_RATE = {"high": 0.03, "mid": 0.15, "low": 0.65}
POOL_SPLIT = {"setup": 0.5, "handling": 0.25, "engineering": 0.25}
POOL_DRIVER = {"setup": "setup_minutes", "handling": "operation_moves", "engineering": "quality_events", "volume": "run_hours"}
PAYROLL_SPLIT = {"6000": 0.45, "6100": 0.15, "6200": 0.40}
OPEX_SPLIT = {"6000": 0.35, "6200": 0.35, "6400": 0.30}


def _month_index(ts: pd.Series, start: dt.date) -> np.ndarray:
    t = pd.to_datetime(ts)
    return np.asarray((t.dt.year - start.year) * 12 + (t.dt.month - start.month))


@register
class AbcCostDistortion(Archetype):
    """Peanut-Butter Overhead: plant-wide labour-hour allocation hides what complexity costs."""

    key = "abc_cost_distortion"
    title = "Peanut-Butter Overhead"
    pillars = ("accounting_finance", "operations_strategy", "python")
    industries = ("manufacturing",)
    schema_template = "manufacturing_mes"
    summary = ("A manufacturer's 'premium' product line shows the best margins and record sales, yet operating income keeps "
               "falling and a manufacturing variance nobody can explain grows every quarter.")
    root_cause_terms = ("activity-based", "allocation base", "direct labor hours", "setup", "abc cost")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 2."""
        ind = industry or "manufacturing"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")

        def band(key: str, lo: float, hi: float) -> float:
            return bk.sample_between(pool, rng.spawn(f"bench:{key}"), key, ind, lo, hi)

        share = np.sort(rng.generator.dirichlet([8, 3, 1]))[::-1]
        share = np.clip(share, [0.55, 0.15, 0.04], [0.85, 0.35, 0.12])
        share = share / share.sum()
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": "make-to-order industrial components manufacturer selling to OEMs and distributors",
            "revenue_ttm_cents": int(math.exp(rng.uniform(math.log(25e6), math.log(80e6))) * 100),
            "families": list(FAMILY_SETS[int(rng.integers(0, len(FAMILY_SETS)))]),
            "volume_share": [float(x) for x in share],
            "overhead_pct_of_conversion": float(rng.uniform(0.45, 0.65)),
            "allocation_base": "direct_labor_hours",
            "setup_mult": [float(rng.uniform(0.8, 1.2)) for _ in range(3)],
            "markup": float(rng.uniform(0.25, 0.40)),
            "distortion_target": magnitude(rng, difficulty, 0.15, 0.35),
            "practical_capacity_pct": float(rng.uniform(0.80, 0.90)),
            "mix_drift_pts": 0.02,
            "growth_qoq": float(rng.uniform(0.0, 0.01)),
            "labor_rate_usd": float(rng.uniform(32, 45)),
            "scrap_rate": float(np.clip(band("manufacturing.scrap_rate", 0.4, 0.7), 0.01, 0.04)),
            "scrap_creep_low": 0.012,
            "discount_creep_high": 0.01,
            "commission_rate": float(rng.uniform(0.04, 0.07)),
            "op_margin": float(rng.uniform(0.10, 0.14)),
            "elasticity_low": -1.2, "elasticity_high": -1.8,
            "avoidable_share": 0.6,
            "tax_rate": float(np.clip(band("manufacturing.effective_tax_rate", 0.4, 0.7), 0.15, 0.26)),
            "dso_terms": int(rng.choice((30, 45))),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "throughput_twist": difficulty == "principal",
            "n_work_centers": int(rng.integers(8, 11)),
            "difficulty": difficulty,
        }
        p["skus_per_family"] = difficulty_pick(difficulty, 6, 15, 22)
        p["lot_factor"] = difficulty_pick(difficulty, 1.0, 0.9, 0.8)  # smaller lots -> more work orders at higher budgets
        p["description"] = (f"Make-to-order manufacturer of {', '.join(p['families']).lower()}; costs products at standard with a "
                            "plant-wide overhead rate.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Work centres, products (BOM, routings, standard cost), demand by SKU-month and the TDABC calibration."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        wcs = list(WORK_CENTERS[: p["n_work_centers"]])  # the first 8 carry the routings; 9-10 are idle support centres
        lr = p["labor_rate_usd"]
        # --- products
        k = p["skus_per_family"]
        rows, routes, bom = [], [], []
        for fi, fk in enumerate(FAMILY_KEYS):
            for j in range(k):
                pid = f"{fk[0].upper()}{fi + 1}-{j + 1:03d}"
                rm = RM_BASE[fk] * float(g.lognormal(0, 0.25))
                for seq, (wc, (lo, hi)) in enumerate(ROUTINGS[fk], start=1):
                    routes.append({"product_id": pid, "op_seq": seq * 10, "work_center_id": wc,
                                   "setup_minutes": round(SETUP_BASE[fk] * p["setup_mult"][fi] * float(g.uniform(0.85, 1.15)), 1),
                                   "run_minutes_per_unit": round(float(g.uniform(lo, hi)), 3)})
                n_comp = {"high": 2, "mid": 4, "low": 7}[fk]
                w = g.dirichlet(np.ones(n_comp))
                for c in range(n_comp):
                    bom.append({"product_id": pid, "component_id": f"RM-{fk[0].upper()}{int(g.integers(100, 999))}", "quantity_per": float(g.choice([1, 1, 2, 4])),
                                "unit_cost_usd": rm * w[c], "scrap_factor": round(float(g.uniform(0.0, 0.03)), 3)})
                rows.append({"product_id": pid, "family_key": fk, "family": p["families"][fi], "rm_usd": rm,
                             "complexity": {"high": "simple", "mid": "standard", "low": "complex"}[fk]})
        products = pd.DataFrame(rows)
        routings = pd.DataFrame(routes)
        routings.insert(0, "routing_id", [f"RT{i:05d}" for i in range(1, len(routings) + 1)])
        bomdf = pd.DataFrame(bom)
        # bom unit cost per line so that sum(qty_per * unit_cost) == rm per unit
        bomdf["unit_cost_usd"] = bomdf["unit_cost_usd"] / bomdf["quantity_per"]
        run_min = routings.groupby("product_id")["run_minutes_per_unit"].sum()
        setup_min = routings.groupby("product_id")["setup_minutes"].sum()
        n_ops = routings.groupby("product_id").size()
        bn_min = routings[routings["work_center_id"] == BOTTLENECK].set_index("product_id")["run_minutes_per_unit"]
        products["run_min"] = products["product_id"].map(run_min).to_numpy()
        products["setup_min_wo"] = products["product_id"].map(setup_min).to_numpy()
        products["n_ops"] = products["product_id"].map(n_ops).to_numpy()
        products["bottleneck_min"] = products["product_id"].map(bn_min).fillna(0.0).to_numpy()
        # --- plant-wide rate (standard): overhead = pct of conversion -> OH / DL = pct / (1 - pct)
        pct = p["overhead_pct_of_conversion"]
        rate_dlh_usd = lr * pct / (1 - pct)
        products["rm_cents"] = cents_array(products["rm_usd"] * 100)
        products["dl_cents"] = cents_array(products["run_min"] / 60 * lr * 100)
        products["oh_std_cents"] = cents_array(products["run_min"] / 60 * rate_dlh_usd * 100)
        products["standard_cost_cents"] = products["rm_cents"] + products["dl_cents"] + products["oh_std_cents"]
        mk = {"high": p["markup"] + 0.05, "mid": p["markup"] + 0.15, "low": p["markup"] + 0.25}
        products["list_price_cents"] = cents_array(products["standard_cost_cents"] * (1 + products["family_key"].map(mk)) / 100) * 100
        # --- demand: family revenue shares drift (low +2pt / quarter from P1), SKU weights Pareto within family
        fam_w = {}
        for fk in FAMILY_KEYS:
            sel = products["family_key"] == fk
            w = 1.0 + g.pareto(1.5, int(sel.sum()))
            fam_w[fk] = w / w.sum()
        vs = np.array(p["volume_share"])
        avg_price = products.groupby("family_key")["list_price_cents"].mean().reindex(FAMILY_KEYS).to_numpy() / 100
        rev_share0 = vs * avg_price / (vs * avg_price).sum()
        q_of_m = np.arange(n_m) // 3  # 0 = P0 (warm-up), 1 = P1 ...
        drift = np.where(q_of_m >= 1, (q_of_m - 0.5) * p["mix_drift_pts"], 0.0)
        share_m = np.zeros((n_m, 3))
        share_m[:, 2] = rev_share0[2] + drift
        share_m[:, 1] = rev_share0[1]
        share_m[:, 0] = 1 - share_m[:, 1] - share_m[:, 2]
        trend = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3) * g.lognormal(0, 0.02, n_m)
        R = p["revenue_ttm_cents"] / 100
        total_m = trend * R / trend[-12:].sum()
        demand = np.zeros((len(products), n_m))
        price = products["list_price_cents"].to_numpy() / 100
        for fi, fk in enumerate(FAMILY_KEYS):
            idx = np.flatnonzero(products["family_key"] == fk)
            fam_rev = total_m * share_m[:, fi]
            per_sku_rev = np.outer(fam_w[fk], fam_rev) * g.lognormal(0, 0.12, (len(idx), n_m))
            demand[idx] = np.maximum(0, np.round(per_sku_rev / price[idx, None]))
        products["lot_size"] = 0
        for fk in FAMILY_KEYS:
            idx = np.flatnonzero(products["family_key"] == fk)
            base = demand[idx, :3].mean(axis=1)
            products.loc[idx, "lot_size"] = np.maximum(1, np.round(np.maximum(base.mean() * 0.8, base) * p["lot_factor"] * LOT_MULT[fk])).astype(int)
        state.frames["work_centers"] = pd.DataFrame(wcs, columns=["work_center_id", "name"])
        state.extras.update({"products": products, "routings": routings, "bom": bomdf, "demand": demand, "rate_dlh_usd": rate_dlh_usd,
                             "share_target": share_m})

    # ------------------------------------------------------------------ anomalies (production, TDABC calibration)

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Work orders and production log from demand; solve the TDABC pools on warm-up drivers."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("production").generator
        products, routings, demand = state.extras["products"], state.extras["routings"], state.extras["demand"]
        months = state.extras["months"]
        n_m = len(months)
        P = state.periods
        # --- work orders: one per lot (setups scale with volume)
        S = len(products)
        lots = products["lot_size"].to_numpy()
        n_wo = np.ceil(demand / lots[:, None]).astype(int)
        si, mi = np.nonzero(n_wo)
        reps = n_wo[si, mi]
        wo_s = np.repeat(si, reps)
        wo_m = np.repeat(mi, reps)
        k_in = np.concatenate([np.arange(r) for r in reps])
        qty = np.floor(demand[wo_s, wo_m] / reps.repeat(reps)).astype(int) + (k_in < (demand[wo_s, wo_m] % reps.repeat(reps))).astype(int)
        keep = qty > 0
        wo_s, wo_m, qty = wo_s[keep], wo_m[keep], qty[keep]
        W = len(wo_s)
        fam = products["family_key"].to_numpy()[wo_s]
        # scrap: base rate, complex family creeps up from P5 (red herring)
        q_of = wo_m // 3
        scrap_rate = np.full(W, p["scrap_rate"]) + np.where((fam == "low") & (q_of >= 5), p["scrap_creep_low"] * (q_of - 4) / 4, 0.0)
        scrap = g.binomial(np.maximum(1, qty), np.clip(scrap_rate, 0, 0.5))
        started = qty + scrap  # good output == demand
        mstart = pd.to_datetime(pd.Series(months))
        days_in = mstart.dt.days_in_month.to_numpy()
        rel_day = (g.random(W) * (days_in[wo_m] - 15)).astype(int)  # every order closes within its production month
        released = mstart.to_numpy()[wo_m] + pd.to_timedelta(rel_day, unit="D").to_numpy() + np.timedelta64(6, "h")
        order = np.lexsort((wo_s, released))
        wo_s, wo_m, qty, scrap, started, released, fam = (a[order] for a in (wo_s, wo_m, qty, scrap, started, released, fam))
        wo_ids = np.array([f"WO{i:06d}" for i in range(1, W + 1)])
        # --- production log: one row per (work order, operation)
        r = routings.merge(products[["product_id"]].reset_index().rename(columns={"index": "s"}), on="product_id")
        r = r.sort_values(["s", "op_seq"], kind="stable")
        n_ops = products["n_ops"].to_numpy()[wo_s]
        log_wo = np.repeat(np.arange(W), n_ops)
        pos = np.concatenate([np.arange(x) for x in n_ops])
        offs = np.cumsum(np.concatenate([[0], products["n_ops"].to_numpy()]))[:-1]
        r_idx = offs[wo_s][log_wo] + pos  # row in r (ordered by s, op)
        r_arr = r.reset_index(drop=True)
        setup_act = np.round(r_arr["setup_minutes"].to_numpy()[r_idx] * g.lognormal(0, 0.12, len(log_wo)), 1)
        run_act = np.round(r_arr["run_minutes_per_unit"].to_numpy()[r_idx] * started[log_wo] * g.lognormal(0, 0.02, len(log_wo)), 1)
        # wall-clock: big lots run on parallel machines (at most two days per operation); operations run back to back
        wall = np.minimum(setup_act + run_act, 2880.0) + 30.0
        cum = np.cumsum(wall)
        first = np.r_[True, log_wo[1:] != log_wo[:-1]]
        grp_start = np.maximum.accumulate(np.where(first, np.arange(len(log_wo)), 0))
        before = cum - wall - (cum[grp_start] - wall[grp_start])
        start_ts = pd.to_datetime(released[log_wo]) + pd.to_timedelta(before, unit="m")
        end_ts = start_ts + pd.to_timedelta(wall - 30.0, unit="m")
        last_op = pos == n_ops[log_wo] - 1
        log = pd.DataFrame({
            "log_id": np.arange(1, len(log_wo) + 1, dtype=np.int64), "work_order_id": wo_ids[log_wo], "work_center_id": r_arr["work_center_id"].to_numpy()[r_idx],
            "op_seq": r_arr["op_seq"].to_numpy()[r_idx], "shift_date": start_ts.normalize(), "shift": np.where(start_ts.hour < 14, "A", "B"),
            "start_ts": start_ts, "end_ts": end_ts, "setup_minutes_actual": setup_act, "run_minutes_actual": run_act,
            "good_qty": np.where(last_op, qty[log_wo], started[log_wo]), "scrap_qty": np.where(last_op, scrap[log_wo], 0),
            "downtime_minutes": np.round(np.maximum(0, g.normal(4, 6, len(log_wo))), 0), "operator_id": np.array([f"OP{i:03d}" for i in range(1, 41)])[g.integers(0, 40, len(log_wo))],
        })
        # --- quality events / engineering changes (count driver), family-specific rate per work order
        n_qe = g.poisson(np.array([QE_RATE[f] for f in fam]) * np.where((fam == "low") & (q_of >= 5), 1.15, 1.0))
        qi = np.repeat(np.arange(W), n_qe)
        qe = pd.DataFrame({
            "event_id": [f"QE{i:05d}" for i in range(1, len(qi) + 1)], "work_order_id": wo_ids[qi],
            "work_center_id": np.where(fam[qi] == "low", "WC05", "WC02"),
            "detected_at": pd.to_datetime(released[qi]) + pd.to_timedelta(g.integers(2, 60, len(qi)), unit="h"),
            "defect_code": np.where(g.random(len(qi)) < 0.55, "ECO", np.array(("DIM", "FINISH", "LEAK", "TORQUE"))[g.integers(0, 4, len(qi))]),
            "quantity": np.maximum(1, g.poisson(2, len(qi))), "disposition": np.array(("rework", "use_as_is", "engineering_change"))[g.integers(0, 3, len(qi))],
            "engineering_hours": np.round(g.lognormal(math.log(3), 0.4, len(qi)), 1),
        })
        wo = pd.DataFrame({
            "work_order_id": wo_ids, "product_id": products["product_id"].to_numpy()[wo_s], "quantity_planned": qty, "quantity_completed": qty,
            "quantity_scrapped": scrap, "released_at": pd.to_datetime(released),
            "due_at": pd.to_datetime(released) + pd.Timedelta(days=10), "status": "closed", "priority": np.where(fam == "low", "expedite", "normal"),
            "_s": wo_s, "_m": wo_m, "_started": started,
        })
        wo["closed_at"] = log.groupby("work_order_id")["end_ts"].max().reindex(wo["work_order_id"]).to_numpy()
        # --- drivers per work order
        drv = log.assign(wo_idx=log_wo).groupby("wo_idx").agg(setup=("setup_minutes_actual", "sum"), run=("run_minutes_actual", "sum"), moves=("log_id", "size"))
        wo["_setup_min"] = drv["setup"].to_numpy()
        wo["_run_min"] = drv["run"].to_numpy()
        wo["_moves"] = drv["moves"].to_numpy()
        wo["_qe"] = np.bincount(qi, minlength=W)
        wo["_family"] = fam
        state.frames.update({"work_orders_full": wo, "production_log": log, "quality_events": qe})
        self._calibrate(state)
        # --- anomalies
        fams = p["families"]
        anomalies = [
            Anomaly(id="A1", kind="cost_allocation_distortion", pillar="accounting_finance", title=f"Plant-wide labour-hour rate mis-costs the {fams[2]} line",
                    mechanism=(f"Overhead is applied at one plant-wide rate per direct-labour hour; {fams[2]} (low volume, complex) consumes "
                               f"far more setup minutes, operation moves and engineering changes per unit than its labour hours imply, so its standard "
                               f"cost is ~{p['distortion_target']:.0%} below its activity-based cost while {fams[0]} is over-costed."),
                    periods=list(P), affected_accounts=["5000", "5020", "1220"], affected_tables=["products", "production_log", "quality_events", "work_orders", "overhead_pools"],
                    magnitude={"distortion_target": round(p["distortion_target"], 6)}, severity="high"),
            Anomaly(id="A2", kind="incentive_mix_drift", pillar="operations_strategy", title=f"Commission on reported margin drives the mix toward {fams[2]}",
                    mechanism=(f"Sales reps are paid on reported (standard-cost) margin; the {fams[2]} revenue share rises ~2pt every quarter from {P[0]}. "
                               "Overhead spending follows the activity, absorption follows labour hours, so the under-absorbed variance (5020) grows and "
                               "operating income falls while reported family margins look healthy."),
                    periods=list(P), affected_accounts=["4000", "5020", "6010"], affected_tables=["sales_orders", "sales_reps", "overhead_pools"],
                    magnitude={"mix_drift_pts_per_quarter": p["mix_drift_pts"]}, severity="high"),
        ]
        if p["throughput_twist"]:
            anomalies.append(Anomaly(id="A3", kind="throughput_constraint", pillar="operations_strategy", title="Finishing is the constraint - and the premium line barely uses it",
                                     mechanism=(f"{dict(WORK_CENTERS)[BOTTLENECK]} is the bottleneck; per bottleneck minute {fams[2]} earns the most throughput "
                                                f"(price - materials), so dropping it would free little constrained capacity: reprice rather than discontinue."),
                                     periods=list(P[4:]), affected_accounts=["4000"], affected_tables=["routings", "production_log", "work_centers"],
                                     magnitude={}, severity="medium"))
        return anomalies

    def _calibrate(self, state: WorldState) -> None:
        """Solve the complexity pool on the warm-up quarter so the low family's distortion equals the target."""
        p = state.params
        wo = state.frames["work_orders_full"]
        products = state.extras["products"]
        util = p["practical_capacity_pct"]
        lr = p["labor_rate_usd"] * 100
        rate = state.extras["rate_dlh_usd"] * 100
        base = wo[wo["_m"] < 3]
        tot = {"run": base["_run_min"].sum() / 60, "setup": base["_setup_min"].sum(), "handling": base["_moves"].sum(), "engineering": base["_qe"].sum()}
        oh_budget = rate * tot["run"]  # per quarter, cents
        lowb = base[base["_family"] == "low"]
        u_low = lowb["quantity_completed"].sum()
        rm_dl = (products["rm_cents"].to_numpy()[lowb["_s"]] * lowb["_started"]).sum() + lr * lowb["_run_min"].sum() / 60
        std_low = (products["standard_cost_cents"].to_numpy()[lowb["_s"]] * lowb["quantity_completed"]).sum()
        share_vol = lowb["_run_min"].sum() / 60 / tot["run"]
        share_cx = (POOL_SPLIT["setup"] * lowb["_setup_min"].sum() / tot["setup"] + POOL_SPLIT["handling"] * lowb["_moves"].sum() / tot["handling"]
                    + POOL_SPLIT["engineering"] * lowb["_qe"].sum() / max(1, tot["engineering"]))
        target_abc_low = std_low / (1 - p["distortion_target"])
        # abc_low_total(K) = rm_dl + util * ((OH - K) * share_vol + K * share_cx)
        A = rm_dl + util * oh_budget * share_vol
        B = util * (share_cx - share_vol)
        K = float(np.clip((target_abc_low - A) / B if B > 0 else 0.0, 0.0, 0.85 * oh_budget))
        pools_q = {"setup": POOL_SPLIT["setup"] * K, "handling": POOL_SPLIT["handling"] * K, "engineering": POOL_SPLIT["engineering"] * K, "volume": oh_budget - K}
        # spending rates: complexity pools spend per driver unit (cost/util per unit used); volume pool fixed per quarter
        spend_rate = {k: pools_q[k] / max(1.0, tot[k]) for k in ("setup", "handling", "engineering")}
        state.extras.update({"pools_q_budget": pools_q, "spend_rate": spend_rate, "budget_drivers": tot, "oh_budget_q": oh_budget, "K": K})

    # ------------------------------------------------------------------ ledger

    def _monthly_production(self, state: WorldState) -> pd.DataFrame:
        """Per-month production costs: RM issued, DL, absorbed OH, actual OH by pool, completions at standard, scrap."""
        p = state.params
        wo = state.frames["work_orders_full"]
        products = state.extras["products"]
        n_m = len(state.extras["months"])
        lr = p["labor_rate_usd"] * 100
        rate = state.extras["rate_dlh_usd"] * 100
        rm = products["rm_cents"].to_numpy()[wo["_s"]] * wo["_started"]
        dl = cents_array(wo["_run_min"] / 60 * lr)
        absorbed = cents_array(wo["_run_min"] / 60 * rate)
        std_good = products["standard_cost_cents"].to_numpy()[wo["_s"]] * wo["quantity_completed"]
        m = wo["_m"].to_numpy()

        def by_m(x: Any) -> np.ndarray:
            return np.bincount(m, weights=np.asarray(x, dtype=float), minlength=n_m)

        sr = state.extras["spend_rate"]
        vol_m = state.extras["pools_q_budget"]["volume"] / 3
        df = pd.DataFrame({
            "rm": by_m(rm), "dl": by_m(dl), "absorbed": by_m(absorbed), "std_good": by_m(std_good),
            "setup_min": by_m(wo["_setup_min"]), "moves": by_m(wo["_moves"]), "qe": by_m(wo["_qe"]), "run_h": by_m(wo["_run_min"] / 60),
        })
        df["oh_setup"] = cents_array(df["setup_min"] * sr["setup"])
        df["oh_handling"] = cents_array(df["moves"] * sr["handling"])
        df["oh_engineering"] = cents_array(df["qe"] * sr["engineering"])
        growth = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3)
        df["oh_volume"] = cents_array(vol_m * growth)
        df["oh_actual"] = df[["oh_setup", "oh_handling", "oh_engineering", "oh_volume"]].sum(axis=1)
        df["scrap"] = (df["rm"] + df["dl"] + df["absorbed"] - df["std_good"]).round().astype(np.int64)
        return df

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Sales orders from work orders, purchasing, then the monthly GL (production batches) and close."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("ledger").generator
        L = state.ledger
        gran = GL_BATCH[p["difficulty"]]
        months = state.extras["months"]
        n_m = len(months)
        end_date = cal.end
        end_ts = pd.Timestamp(end_date) + EOD
        wo = state.frames["work_orders_full"]
        products = state.extras["products"]
        P = state.periods
        # --- customers, reps, sales orders (one per work order; shipped at close, invoiced at shipment)
        n_c = difficulty_pick(p["difficulty"], 40, 80, 120)
        cw = 1.0 + g.pareto(1.3, n_c)
        customers = pd.DataFrame({"customer_id": [f"CU{i:04d}" for i in range(1, n_c + 1)], "name": [ctx.rng.spawn(f"cn{i}").company_name("manufacturing") for i in range(n_c)],
                                  "segment": np.where(np.argsort(np.argsort(-cw)) < n_c * 0.2, "oem", "distributor"),
                                  "payment_terms_days": p["dso_terms"]})
        reps = pd.DataFrame({"sales_rep_id": [f"SR{i:02d}" for i in range(1, 11)], "name": ctx.rng.spawn("reps").pick_names(10),
                             "commission_basis": "reported_margin", "commission_rate": round(p["commission_rate"], 4),
                             "basis_since": pd.Timestamp(cal.bounds(P[1])[0])})
        W = len(wo)
        shipped = pd.to_datetime(wo["closed_at"]) + pd.to_timedelta(g.integers(2, 20, W), unit="h")
        shipped = shipped.where(shipped <= end_ts, end_ts - pd.Timedelta(hours=2))
        fam = wo["_family"].to_numpy()
        q_of = wo["_m"].to_numpy() // 3
        disc = np.where(fam == "high", 0.02 + p["discount_creep_high"] * np.clip((q_of - 4) / 4, 0, 1), np.where(fam == "mid", 0.02, 0.0))
        price = products["list_price_cents"].to_numpy()[wo["_s"]]
        unit_net = np.round(price * (1 - disc)).astype(np.int64)
        amount = unit_net * wo["quantity_completed"].to_numpy()
        cust = g.choice(n_c, size=W, p=cw / cw.sum())
        pay_at = shipped.dt.normalize() + pd.to_timedelta(p["dso_terms"] + np.round(g.normal(4, 6, W)), unit="D") + pd.Timedelta(hours=11)
        so = pd.DataFrame({
            "sales_order_id": [f"SO{i:06d}" for i in range(1, W + 1)], "customer_id": customers["customer_id"].to_numpy()[cust],
            "product_id": wo["product_id"], "work_order_id": wo["work_order_id"], "quantity": wo["quantity_completed"], "unit_price_cents": unit_net,
            "list_price_cents": price, "discount_pct": np.round(disc, 4), "amount_cents": amount,
            "ordered_at": pd.to_datetime(wo["released_at"]) - pd.to_timedelta(g.integers(3, 25, W), unit="D"),
            "promised_at": pd.to_datetime(wo["due_at"]), "shipped_at": shipped, "invoiced_at": shipped.dt.normalize(),
            "paid_at": pay_at.where(pay_at <= end_ts), "sales_rep_id": reps["sales_rep_id"].to_numpy()[g.integers(0, 10, W)],
            "_std": products["standard_cost_cents"].to_numpy()[wo["_s"]] * wo["quantity_completed"].to_numpy(), "_family": fam,
        })
        prod = self._monthly_production(state)
        # --- RM purchasing: next month's RM need received mid-month, net-45 to suppliers
        V = 8
        sup = pd.DataFrame({"supplier_id": [f"SUP{i:02d}" for i in range(1, V + 1)], "name": [ctx.rng.spawn(f"sup{i}").company_name("manufacturing") for i in range(V)]})
        sw = g.dirichlet(np.ones(V) * 2)
        rm_need = prod["rm"].to_numpy()
        rm0 = int(rm_need[0] * 1.2)
        po_rows = []
        on_hand = rm0
        for m in range(n_m):
            target = rm_need[min(m + 1, n_m - 1)] * 1.1
            buy = max(0.0, target + rm_need[m] - on_hand)
            on_hand += buy - rm_need[m]
            for j, a in enumerate(split_cents(int(round(buy)), list(sw))):
                if a > 0:
                    po_rows.append((j, a, pd.Timestamp(months[m]) + pd.Timedelta(days=int(g.integers(0, 10)), hours=8)))
        po = pd.DataFrame(po_rows, columns=["j", "amount_cents", "received_at"]).sort_values(["received_at", "j"], kind="stable").reset_index(drop=True)
        po.insert(0, "po_id", [f"PO{i:05d}" for i in range(1, len(po) + 1)])
        po["supplier"] = sup["supplier_id"].to_numpy()[po["j"]]
        po["ordered_at"] = po["received_at"] - pd.to_timedelta(g.integers(10, 30, len(po)), unit="D")
        po["paid_at"] = (po["received_at"].dt.normalize() + pd.to_timedelta(45 + np.round(g.normal(0, 4, len(po))), unit="D") + pd.Timedelta(hours=10))
        po["paid_at"] = po["paid_at"].where(po["paid_at"] <= end_ts)
        po["status"] = "received"
        # FG opening stock: ~3 weeks of standard COGS per SKU (constant buffer; make-to-order ships what it makes)
        fg0_by_sku = np.round(state.extras["demand"][:, :3].mean(axis=1) * 0.7).astype(np.int64)
        fg0 = int((fg0_by_sku * products["standard_cost_cents"].to_numpy()).sum())
        state.extras.update({"so": so, "po": po, "fg0_by_sku": fg0_by_sku, "rm0": rm0, "prod": prod})
        state.frames.update({"customers": customers, "sales_reps": reps, "suppliers": sup})
        # --- opening balances
        R = p["revenue_ttm_cents"]
        # plant depreciation is part of the (fixed) volume pool: ~40% of it; PPE sized for a 10-year life
        dep_plant = cents(0.4 * state.extras["pools_q_budget"]["volume"] / 3)
        ppe = dep_plant * 120 + cents(R * 0.03)
        accdep = cents(ppe * 0.45)
        cash0 = cents(R * 0.06)
        debt = int(round(R * 0.12 / 100_000)) * 100_000
        net = cash0 + rm0 + fg0 + ppe - accdep - debt
        cs = cents(0.6 * net)
        L.open_books(cal.opening_date, {"1000": cash0, "1200": rm0, "1220": fg0, "1500": ppe, "1510": -accdep, "2610": -debt, "3000": -cs, "3100": -(net - cs)})
        events: list[tuple[dt.date, int, Any]] = []
        so_b = so.assign(b=batch_end_dates(so["invoiced_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), c=("_std", "sum"), n=("sales_order_id", "size"))
        paid = so[so["paid_at"].notna()]
        pay_b = paid.assign(b=batch_end_dates(paid["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("sales_order_id", "size"))
        rec_b = po.assign(b=batch_end_dates(po["received_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        po_paid = po[po["paid_at"].notna()]
        ap_b = po_paid.assign(b=batch_end_dates(po_paid["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        for b, r in so_b.iterrows():
            dim = {"batch": f"sales:{b.date()}", "rows": str(int(r.n))}
            events.append((b.date(), 3, lambda d, a=int(r.a), dm=dim: L.sell_on_credit(d, a, memo="Shipments invoiced (batch)", dim=dm)))
            events.append((b.date(), 3, lambda d, a=int(r.c), dm=dim: L.relieve_inventory_cogs(d, a, memo="COGS at standard (batch)", dim=dm)))
        for b, r in pay_b.iterrows():
            events.append((b.date(), 4, lambda d, a=int(r.a), dm={"batch": f"collections:{b.date()}", "rows": str(int(r.n))}: L.collect_cash(d, a, memo="Customer payments (batch)", dim=dm)))
        for b, r in rec_b.iterrows():
            events.append((b.date(), 1, lambda d, a=int(r.a), dm={"batch": f"purchasing:{b.date()}", "rows": str(int(r.n))}: L.purchase_inventory(
                d, a, inventory_account="1200", memo="Raw material receipts (batch)", dim=dm)))
        for b, r in ap_b.iterrows():
            events.append((b.date(), 5, lambda d, a=int(r.a), dm={"batch": f"payables:{b.date()}", "rows": str(int(r.n))}: L.pay_ap(d, a, memo="Supplier payments (batch)", dim=dm)))
        so_m = np.bincount(_month_index(so["invoiced_at"], cal.start), weights=so["amount_cents"], minlength=n_m)
        margin_m = np.bincount(_month_index(so["invoiced_at"], cal.start), weights=so["amount_cents"] - so["_std"], minlength=n_m)
        prev = {"payroll": 0, "opex": 0}
        R0 = float(so_m[:3].mean())
        # warm-up gross margin (standard COGS + scrap + variance, ~0 at the standard-setting volumes) sizes the SG&A base
        std0 = float(so.loc[_month_index(so["invoiced_at"], cal.start) < 3, "_std"].sum())
        gm0 = (so_m[:3].sum() - std0 - prod["scrap"].iloc[:3].sum() - (prod["oh_actual"] - prod["absorbed"]).iloc[:3].sum()) / so_m[:3].sum()
        sga_pct = max(0.08, gm0 - p["op_margin"] - 0.012)
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            row = prod.iloc[m]
            mdim = {"batch": f"production:{me}", "work_orders": str(int((wo['_m'] == m).sum()))}
            lines_wip = [("1210", int(row.rm), 0, mdim), ("1200", 0, int(row.rm), mdim)]
            events.append((me, 6, lambda d, ln=lines_wip: L.adjust(d, ln, "Raw materials issued to work orders", "production")))
            events.append((me, 6, lambda d, a=int(row.dl), dm=mdim: L.adjust(d, [("1210", a, 0, dm), ("2110", 0, a, dm)], "Direct labour charged to work orders", "production")))
            events.append((me, 6, lambda d, a=int(row.absorbed), dm=mdim: L.adjust(d, [("1210", a, 0, dm), ("5020", 0, a, dm)], "Overhead applied at the plant-wide DLH rate", "production")))
            oh_cash = int(row.oh_actual) - dep_plant
            events.append((me, 6, lambda d, a=oh_cash, dp=dep_plant, dm=mdim: L.adjust(d, [("5020", a + dp, 0, dm), ("2100", 0, a, dm), ("1510", 0, dp, dm)],
                                                                                       "Manufacturing overhead incurred (setups, handling, engineering, plant)", "overhead")))
            events.append((me, 7, lambda d, a=int(row.std_good), dm=mdim: L.adjust(d, [("1220", a, 0, dm), ("1210", 0, a, dm)], "Completed work orders to finished goods at standard", "production")))
            sc = int(row.scrap)
            sc_lines = [("5040", sc, 0, mdim), ("1210", 0, sc, mdim)] if sc >= 0 else [("1210", -sc, 0, mdim), ("5040", 0, -sc, mdim)]
            events.append((me, 7, lambda d, ln=sc_lines: L.adjust(d, ln, "Scrap and yield loss expensed (WIP residual)", "production")))
            if m > 0:
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["opex"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued overhead and opex paid", source="opex")))
            if ms.month in (1, 4, 7, 10) and m > 0:
                events.append((ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"])))
            # SG&A sized on the warm-up revenue (cost base does not follow the mix); commissions on reported margin
            sga = cents(R0 * (sga_pct - p["commission_rate"] * 0.3) * (1 + p["growth_qoq"]) ** (m / 3))
            payroll = cents(sga * 0.6)
            other = sga - payroll
            comm = cents(margin_m[m] * p["commission_rate"])
            pr = dict(zip(PAYROLL_SPLIT, split_cents(payroll, list(PAYROLL_SPLIT.values())), strict=True))
            pr["6010"] = comm
            ox = dict(zip(OPEX_SPLIT, split_cents(other, list(OPEX_SPLIT.values())), strict=True))
            events.append((me, 8, lambda d, s=pr: L.payroll(d, s, memo="Payroll and commissions accrual")))
            events.append((me, 8, lambda d, s=ox: L.accrue_opex(d, s)))
            events.append((me, 8, lambda d, a=cents(R0 * 0.004): L.depreciate(d, a, memo="Depreciation - office and IT")))
            events.append((ms + dt.timedelta(days=15), 2, lambda d, a=cents(R0 * 0.03): L.capex(d, a)))
            events.append((me, 9, lambda d, a=cents(debt * 0.075 / 12): L.accrue_interest(d, a)))
            if ms.month % 3 == 0:
                events.append((me, 10, lambda d: L.pay_interest(d, -L.closing_balances()["2150"])))

                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))

                events.append((me, 11, tax))
            prev = {"payroll": payroll + comm + int(row.dl), "opex": other + oh_cash}
        events.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in events:
            if d <= end_date:
                fn(d)

    # ------------------------------------------------------------------ tables

    def _pool_table(self, state: WorldState) -> pd.DataFrame:
        """``overhead_pools``: quarterly cost, driver quantity and practical capacity per activity pool."""
        cal = state.calendar
        prod = state.extras["prod"]
        util = state.params["practical_capacity_pct"]
        bud = state.extras["budget_drivers"]
        rows = []
        for qi, per in enumerate(cal.all_periods):
            sl = prod.iloc[3 * qi: 3 * qi + 3]
            vals = {"setup": (sl["oh_setup"].sum(), sl["setup_min"].sum()), "handling": (sl["oh_handling"].sum(), sl["moves"].sum()),
                    "engineering": (sl["oh_engineering"].sum(), sl["qe"].sum()), "volume": (sl["oh_volume"].sum(), sl["run_h"].sum())}
            for pool, (cost, qty) in vals.items():
                cap = qty / util if pool != "volume" else bud["run"] / util
                rows.append({"period": per, "pool": pool, "driver": POOL_DRIVER[pool], "cost_cents": int(cost), "driver_qty": round(float(qty), 2),
                             "practical_capacity_qty": round(float(cap), 2)})
        return pd.DataFrame(rows)

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Manufacturing MES tables + sales / cost-centre tables; dirt rules 2 / 4 / 6."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("tables").generator
        products = state.extras["products"]
        wc = state.frames["work_centers"].copy()
        log = state.frames["production_log"]
        hours = log.groupby("work_center_id")[["setup_minutes_actual", "run_minutes_actual"]].sum().sum(axis=1) / 60 / (len(state.extras["months"]) * 4.33)
        wc["capacity_hours_per_week"] = np.round(np.maximum(40, wc["work_center_id"].map(hours).fillna(20) / np.where(wc["work_center_id"] == BOTTLENECK, 0.93, 0.7)), 0)
        wc["hourly_rate_cents"] = int(round(p["labor_rate_usd"] * 100))
        wc["setup_minutes_avg"] = wc["work_center_id"].map(state.extras["routings"].groupby("work_center_id")["setup_minutes"].mean()).round(1)
        prods = pd.DataFrame({
            "product_id": products["product_id"], "part_number": [f"PN-{int(x):05d}" for x in g.integers(10000, 99999, len(products))],
            "family": products["family"], "complexity": products["complexity"], "standard_cost_cents": products["standard_cost_cents"],
            "list_price_cents": products["list_price_cents"], "rm_cost_cents": products["rm_cents"], "dl_cost_cents": products["dl_cents"],
            "overhead_std_cents": products["oh_std_cents"], "annual_volume_plan": np.round(state.extras["demand"][:, :3].sum(axis=1) * 4).astype(np.int64),
        })
        bom = state.extras["bom"].copy()
        bom.insert(0, "bom_line_id", [f"BL{i:05d}" for i in range(1, len(bom) + 1)])
        bom["unit_cost_cents"] = cents_array(bom.pop("unit_cost_usd") * 100)
        wo = state.frames["work_orders_full"].drop(columns=[c for c in state.frames["work_orders_full"].columns if c.startswith("_")])
        so = state.extras["so"].drop(columns=["_std", "_family"])
        po = state.extras["po"][["po_id", "supplier", "ordered_at", "received_at", "amount_cents", "status", "paid_at"]]
        # finished-goods snapshots: opening buffer is held constant (make-to-order), quarterly per SKU
        fgq = self._fg_qty(state)
        snaps = []
        for per in state.periods:
            e = pd.Timestamp(cal.bounds(per)[1])
            snaps.append(pd.DataFrame({"period": per, "item_id": products["product_id"], "item_type": "FG", "quantity_on_hand": fgq[per],
                                       "unit_cost_cents": products["standard_cost_cents"], "counted_at": e + pd.Timedelta(hours=18)}))
        inv = pd.concat(snaps, ignore_index=True)
        inv.insert(0, "snapshot_id", np.arange(1, len(inv) + 1, dtype=np.int64))
        pools = self._pool_table(state)
        dirt_plan = [
            ("production_log", "nulls:downtime_minutes:0.05"), ("quality_events", "case_mixed:defect_code"),
            ("customers", "whitespace:name"), ("sales_orders", "late_arriving:paid_at"),
            ("production_log", "tz_mixed:end_ts"), ("work_orders", "nulls:priority:0.08"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        so = so.assign(created_at=so["ordered_at"])
        money = {c: "BIGINT" for c in ("standard_cost_cents", "list_price_cents", "rm_cost_cents", "dl_cost_cents", "overhead_std_cents", "unit_cost_cents",
                                        "amount_cents", "unit_price_cents", "cost_cents", "hourly_rate_cents")}
        builder.add_frame("work_centers", wc, primary_key=["work_center_id"], foreign_keys=[], column_types=money,
                          descriptions={"capacity_hours_per_week": "available hours per week (practical)"})
        builder.add_frame("products", prods, primary_key=["product_id"], foreign_keys=[], column_types={**money, "annual_volume_plan": "BIGINT"},
                          descriptions={"standard_cost_cents": "RM + direct labour + overhead at the plant-wide DLH rate (last reset before the case period)",
                                        "list_price_cents": "standard cost x family markup"})
        builder.add_frame("bom", bom, primary_key=["bom_line_id"], foreign_keys=[("product_id", "products", "product_id")], column_types=money)
        builder.add_frame("routings", state.extras["routings"], primary_key=["routing_id"],
                          foreign_keys=[("product_id", "products", "product_id"), ("work_center_id", "work_centers", "work_center_id")])
        builder.add_frame("work_orders", wo, primary_key=["work_order_id"], foreign_keys=[("product_id", "products", "product_id")], dirt=dirt.get("work_orders", ()),
                          column_types={"quantity_planned": "INTEGER", "quantity_completed": "INTEGER", "quantity_scrapped": "INTEGER"})
        builder.add_frame("production_log", log, primary_key=["log_id"],
                          foreign_keys=[("work_order_id", "work_orders", "work_order_id"), ("work_center_id", "work_centers", "work_center_id")],
                          column_types={"log_id": "BIGINT", "good_qty": "INTEGER", "scrap_qty": "INTEGER", "shift_date": "DATE"}, dirt=dirt.get("production_log", ()),
                          descriptions={"setup_minutes_actual": "changeover minutes for this operation of the work order",
                                        "run_minutes_actual": "run minutes (direct labour) for this operation"})
        builder.add_frame("quality_events", state.frames["quality_events"], primary_key=["event_id"], foreign_keys=[("work_order_id", "work_orders", "work_order_id")],
                          dirt=dirt.get("quality_events", ()), descriptions={"defect_code": "ECO = engineering change order; others are inspection findings"})
        builder.add_frame("customers", state.frames["customers"], primary_key=["customer_id"], foreign_keys=[], dirt=dirt.get("customers", ()))
        builder.add_frame("sales_reps", state.frames["sales_reps"], primary_key=["sales_rep_id"], foreign_keys=[],
                          descriptions={"commission_basis": "what commissions are computed on", "basis_since": "plan effective date"})
        builder.add_frame("sales_orders", so, primary_key=["sales_order_id"],
                          foreign_keys=[("product_id", "products", "product_id"), ("customer_id", "customers", "customer_id"), ("sales_rep_id", "sales_reps", "sales_rep_id")],
                          column_types={**money, "quantity": "INTEGER"}, dirt=dirt.get("sales_orders", ()),
                          descriptions={"amount_cents": "invoiced amount (net of discount) - AR sub-ledger", "paid_at": "NULL while open"})
        builder.add_frame("suppliers", state.frames["suppliers"], primary_key=["supplier_id"], foreign_keys=[])
        builder.add_frame("purchase_orders", po, primary_key=["po_id"], foreign_keys=[("supplier", "suppliers", "supplier_id")], column_types=money)
        builder.add_frame("inventory", inv, primary_key=["snapshot_id"], foreign_keys=[("item_id", "products", "product_id")], column_types={**money, "quantity_on_hand": "BIGINT"})
        builder.add_frame("overhead_pools", pools, primary_key=["period", "pool"], foreign_keys=[], column_types=money,
                          description="Plant cost-centre report: activity pool cost, driver and practical capacity per quarter.")

    # ------------------------------------------------------------------ ties

    def _fg_qty(self, state: WorldState) -> dict[str, np.ndarray]:
        """Finished-goods quantity per SKU at each period end: opening buffer + completions (production month) - shipments."""
        if "fg_qty" in state.extras:
            return state.extras["fg_qty"]
        cal = state.calendar
        wo, so = state.frames["work_orders_full"], state.extras["so"]
        S = len(state.extras["products"])
        n_w = len(cal.warmup_periods)
        out = {}
        for i, per in enumerate(state.periods):
            e_ts = pd.Timestamp(cal.bounds(per)[1]) + EOD
            done = wo[wo["_m"] < 3 * (n_w + i + 1)]
            made = np.bincount(done["_s"], weights=done["quantity_completed"], minlength=S)
            shipped = so[so["invoiced_at"] <= e_ts]
            sold = np.bincount(state.extras["products"].reset_index().set_index("product_id").loc[shipped["product_id"], "index"].to_numpy(),
                               weights=shipped["quantity"], minlength=S)
            out[per] = (state.extras["fg0_by_sku"] + made - sold).astype(np.int64)
        state.extras["fg_qty"] = out
        return out

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR (sales orders), AP (purchase orders), FG (snapshots), RM, revenue and overhead pools tie (no designed break)."""
        cal = state.calendar
        so, po, prod = state.extras["so"], state.extras["po"], state.extras["prod"]
        products = state.extras["products"]
        fgq = self._fg_qty(state)
        ar, ap, rev, rm, fgd, oh = {}, {}, {}, {}, {}, {}
        n_w = len(cal.warmup_periods)
        for i, per in enumerate(state.periods):
            s, e = cal.bounds(per)
            e_ts = pd.Timestamp(e) + EOD
            inv_ok = so["invoiced_at"] <= e_ts
            ar[per] = int(so.loc[inv_ok & (so["paid_at"].isna() | (so["paid_at"] > e_ts)), "amount_cents"].sum())
            ap[per] = int(po.loc[(po["received_at"] <= e_ts) & (po["paid_at"].isna() | (po["paid_at"] > e_ts)), "amount_cents"].sum())
            rev[per] = int(so.loc[inv_ok & (so["invoiced_at"] >= pd.Timestamp(s)), "amount_cents"].sum())
            upto = 3 * (n_w + i + 1)
            rm[per] = int(state.extras["rm0"] + po.loc[po["received_at"] <= e_ts, "amount_cents"].sum() - prod["rm"].iloc[:upto].sum())
            fgd[per] = int((fgq[per] * products["standard_cost_cents"].to_numpy()).sum())
            sl = prod.iloc[upto - 3: upto]
            oh[per] = int(sl["oh_actual"].sum() - sl["absorbed"].sum())
        state.extras["underabsorbed"] = oh
        return [
            SubledgerTie("ar_open_sales_orders", ar, ["1100"], description="Invoiced, unpaid sales orders == GL 1100"),
            SubledgerTie("ap_open_purchase_orders", ap, ["2000"], description="Received, unpaid POs == GL 2000"),
            SubledgerTie("revenue_sales_orders", rev, ["4000"], basis="activity", description="Sales orders invoiced in period == GL 4000"),
            SubledgerTie("raw_materials", rm, ["1200"], description="Opening RM + receipts - issues to work orders == GL 1200"),
            SubledgerTie("finished_goods_snapshots", fgd, ["1220"], description="FG snapshot qty x standard cost == GL 1220 (+ 1210, empty at month end)"),
            SubledgerTie("overhead_pools_vs_5020", oh, ["5020"], basis="activity",
                         ledger_cents={per: state.ledger.activity(per)["5020"] for per in state.periods},
                         description="Overhead pools spend - overhead absorbed == 5020 net activity (under-absorbed variance)"),
        ]

    # ------------------------------------------------------------------ figures

    def _facts(self, state: WorldState) -> dict[str, Any]:
        """ABC unit costs, distortion, margins, mix and throughput computed from the generated data (cached)."""
        if "facts" in state.extras:
            return state.extras["facts"]
        p, cal = state.params, state.calendar
        P = state.periods
        wo = state.frames["work_orders_full"]
        products = state.extras["products"]
        so = state.extras["so"]
        pools = self._pool_table(state)
        rep = pools[pools["period"].isin(P)]
        rates = (rep.groupby("pool")["cost_cents"].sum() / rep.groupby("pool")["practical_capacity_qty"].sum()).to_dict()
        n_w = len(cal.warmup_periods)
        w = wo[wo["_m"] >= 3 * n_w].copy()
        lr = p["labor_rate_usd"] * 100
        w["rm"] = products["rm_cents"].to_numpy()[w["_s"]] * w["_started"]
        w["dl"] = w["_run_min"] / 60 * lr
        w["abc_oh"] = (w["_setup_min"] * rates["setup"] + w["_moves"] * rates["handling"] + w["_qe"] * rates["engineering"]
                       + w["_run_min"] / 60 * rates["volume"])
        w["std"] = products["standard_cost_cents"].to_numpy()[w["_s"]] * w["quantity_completed"]
        so_r = so[so["invoiced_at"] >= pd.Timestamp(cal.bounds(P[0])[0])]
        fam_rev = so_r.groupby("_family")["amount_cents"].sum()
        g = w.groupby("_family")
        units = g["quantity_completed"].sum()
        abc_unit = (g["rm"].sum() + g["dl"].sum() + g["abc_oh"].sum()) / units
        std_unit = g["std"].sum() / units
        price_unit = fam_rev / so_r.groupby("_family")["quantity"].sum()
        dist = (std_unit - abc_unit) / abc_unit
        shares = {}
        for per in P:
            s, e = cal.bounds(per)
            sel = (so["invoiced_at"] >= pd.Timestamp(s)) & (so["invoiced_at"] <= pd.Timestamp(e) + EOD)
            fr = so[sel].groupby("_family")["amount_cents"].sum()
            shares[per] = (fr / fr.sum()).reindex(FAMILY_KEYS).fillna(0.0).to_dict()
        true_margin = (price_unit - abc_unit) / price_unit
        reported_margin = (price_unit - std_unit) / price_unit
        st = state.statements
        inc8 = next(i for i in st.income if i.period == P[-1])  # type: ignore[union-attr]
        s8, e8 = cal.bounds(P[-1])
        rev8 = float(so.loc[(so["invoiced_at"] >= pd.Timestamp(s8)) & (so["invoiced_at"] <= pd.Timestamp(e8) + EOD), "amount_cents"].sum())
        oi_cf = inc8.operating_income_cents + sum((shares[P[0]][f] - shares[P[-1]][f]) * rev8 * true_margin[f] for f in FAMILY_KEYS)
        tot_run = w["_run_min"].sum() / 60
        tot_oh = rep["cost_cents"].sum()
        # throughput per bottleneck minute: (net price - RM) per unit / bottleneck run minutes per unit
        bn = products.groupby("family_key")["bottleneck_min"].mean()
        rm_unit = g["rm"].sum() / units
        tput = ((price_unit - rm_unit) / bn).to_dict()
        f = {"rates": rates, "abc_unit": abc_unit.to_dict(), "std_unit": std_unit.to_dict(), "price_unit": price_unit.to_dict(), "dist": dist.to_dict(),
             "true_margin": true_margin.to_dict(), "reported_margin": reported_margin.to_dict(), "shares": shares,
             "mix_shift": shares[P[-1]]["low"] - shares[P[0]]["low"], "oi_cf": float(oi_cf), "oi8": float(inc8.operating_income_cents),
             "plantwide_actual": tot_oh / tot_run, "plantwide_std": state.extras["rate_dlh_usd"] * 100, "underabs": state.extras["underabsorbed"],
             "tput": tput, "rev8": rev8,
             "setup_per_unit": (g["_setup_min"].sum() / units).to_dict(), "run_per_unit": (g["_run_min"].sum() / units).to_dict()}
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Work-centre stations (bottleneck = finishing), unit economics by family, KPIs by period."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        log = state.frames["production_log"]
        n_weeks = len(state.extras["months"]) * 4.33
        load = log.groupby("work_center_id")[["setup_minutes_actual", "run_minutes_actual"]].sum().sum(axis=1) / 60 / n_weeks
        specs = []
        for wcid, name in state.frames["work_centers"].itertuples(index=False):
            cap = max(40.0, float(load.get(wcid, 20.0)) / (0.93 if wcid == BOTTLENECK else 0.7))
            specs.append({"name": name, "capacity_per_hour": cap / 40.0, "cs": 1.0})
        lam = float(load.get(BOTTLENECK, 1.0)) / 40.0
        stations, summary = station_network(lam, specs)
        kpis = {}
        for per in state.periods:
            inc = next(i for i in state.statements.income if i.period == per)  # type: ignore[union-attr]
            kpis[per] = {"revenue_cents": float(inc.revenue_cents), "operating_income_cents": float(inc.operating_income_cents),
                         "underabsorbed_overhead_cents": float(f["underabs"][per]), "low_family_revenue_share": float(f["shares"][per]["low"]),
                         "gross_margin": state.statements.ratios[per]["gross_margin"]}  # type: ignore[union-attr]
        unit = {"gross_margin": state.statements.ratios[state.periods[-1]]["gross_margin"], "arpa": 0.0, "cac": 0.0, "ltv": 0.0, "payback_months": 0.0, "churn_monthly": 0.0}  # type: ignore[union-attr]
        return build_operations_state(stations=stations, queueing={"throughput": summary["throughput"]}, unit_economics=unit,
                                      supply_chain={"lot_sizes_by_family": {k: int(v) for k, v in state.extras["products"].groupby("family_key")["lot_size"].mean().round().items()}},
                                      pricing={"markup_by_family": {"high": p["markup"] + 0.05, "mid": p["markup"] + 0.15, "low": p["markup"] + 0.25},
                                               "elasticity": {"low": p["elasticity_low"], "high": p["elasticity_high"]}},
                                      kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 2 key table from the production data, cost-centre report and books; reference SQL on the DB."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        P = state.periods
        fams = p["families"]
        metrics = [
            metric("plantwide_rate_per_dlh_cents", f["plantwide_actual"], "cents/h",
                   f"Sum of overhead pool cost {P[0]}..{P[-1]} (overhead_pools) / sum of direct-labour (run) hours in production_log over the same quarters. "
                   f"(The stale standard rate in products is {f['plantwide_std']:.0f} cents/h.)", pillar="accounting_finance", tol_rel=0.03,
                   label="Plant-wide overhead rate per DLH (actual)"),
            metric("abc_unit_cost_family_low_cents", f["abc_unit"]["low"], "cents",
                   f"TDABC for {fams[2]}: (RM + direct labour + sum over pools of driver usage x pool rate) / good units, {P[0]}..{P[-1]}; pool rate = "
                   "pool cost / practical capacity (overhead_pools); drivers: setup minutes, operation moves (production_log rows), quality events, run hours.",
                   pillar="accounting_finance", tol_rel=0.05, label=f"Activity-based unit cost, {fams[2]}"),
            metric("abc_unit_cost_family_high_cents", f["abc_unit"]["high"], "cents", f"Same for {fams[0]}.", pillar="accounting_finance", tol_rel=0.05,
                   label=f"Activity-based unit cost, {fams[0]}"),
            metric("distortion_low_family_pct", f["dist"]["low"], "ratio", f"(standard unit cost - ABC unit cost) / ABC unit cost for {fams[2]} (negative = under-costed).",
                   pillar="accounting_finance", tol_abs=0.04, label=f"Cost distortion, {fams[2]}"),
            metric("true_margin_family_low_pct", f["true_margin"]["low"], "ratio", f"(average net price - ABC unit cost) / average net price, {fams[2]}.",
                   pillar="accounting_finance", tol_abs=0.03, label=f"True margin, {fams[2]}"),
            metric("reported_margin_family_low_pct", f["reported_margin"]["low"], "ratio", f"(average net price - standard unit cost) / average net price, {fams[2]}.",
                   pillar="accounting_finance", tol_abs=0.02, label=f"Reported margin, {fams[2]}"),
            metric("underabsorbed_overhead_p8_cents", f["underabs"][P[-1]], "cents", f"Net activity on 5020 in {P[-1]} (overhead incurred - overhead applied).",
                   pillar="accounting_finance", tol_rel=0.05, label="Under-absorbed overhead, latest quarter"),
            metric("mix_shift_low_family_pts", f["mix_shift"], "ratio", f"{fams[2]} share of invoiced revenue in {P[-1]} minus in {P[0]}.",
                   pillar="operations_strategy", tol_abs=0.02, label=f"Mix shift toward {fams[2]}"),
            metric("operating_income_at_p1_mix_cents", f["oi_cf"], "cents",
                   f"{P[-1]} operating income + sum over families of ({P[0]} share - {P[-1]} share) x {P[-1]} revenue x true (ABC) margin.",
                   pillar="operations_strategy", tol_rel=0.08, label="Operating income at the original mix (counterfactual)"),
        ]
        if p["throughput_twist"]:
            for fk, fn in (("low", fams[2]), ("high", fams[0])):
                metrics.append(metric(f"throughput_per_bottleneck_min_{fk}_cents", f["tput"][fk], "cents/min",
                                      f"(average net price - RM per unit) / {dict(WORK_CENTERS)[BOTTLENECK]} run minutes per unit (routings), {fn}.",
                                      pillar="operations_strategy", tol_rel=0.05, label=f"Throughput per bottleneck minute, {fn}"))
        sql_drivers = """
WITH wo AS (
  SELECT w.work_order_id, p.family, w.quantity_completed FROM work_orders w JOIN products p USING (product_id)),
lg AS (
  SELECT work_order_id, SUM(setup_minutes_actual) AS setup_min, SUM(run_minutes_actual) AS run_min, COUNT(*) AS moves
  FROM production_log GROUP BY 1),
qe AS (SELECT work_order_id, COUNT(*) AS events FROM quality_events GROUP BY 1)
SELECT wo.family, SUM(wo.quantity_completed) AS units,
       SUM(lg.setup_min) * 1.0 / SUM(wo.quantity_completed) AS setup_min_per_unit,
       SUM(lg.run_min) * 1.0 / SUM(wo.quantity_completed) AS run_min_per_unit,
       SUM(lg.moves) * 1.0 / SUM(wo.quantity_completed) AS moves_per_unit,
       SUM(COALESCE(qe.events, 0)) * 1.0 / SUM(wo.quantity_completed) AS events_per_unit
FROM wo JOIN lg USING (work_order_id) LEFT JOIN qe USING (work_order_id)
GROUP BY 1 ORDER BY setup_min_per_unit DESC"""

        def driver_checks(df: pd.DataFrame) -> dict[str, Any]:
            s = df.set_index("family")
            ratio = float(s.loc[fams[2], "setup_min_per_unit"] / s.loc[fams[0], "setup_min_per_unit"])
            return {"setup_per_unit_low_over_high": ratio, "low_ge_4x_high": ratio >= 4.0}

        q_drv = run_reference_query(con, "activity_drivers_per_unit_by_family", sql_drivers, signature=["with", "left join", "join", "group by", "coalesce("],
                                    metric_keys=["abc_unit_cost_family_low_cents", "distortion_low_family_pct"], checks=driver_checks)
        sql_abc = f"""
WITH rates AS (
  SELECT pool, SUM(cost_cents) * 1.0 / SUM(practical_capacity_qty) AS rate
  FROM overhead_pools WHERE period >= '{P[0]}' GROUP BY 1),
r AS (SELECT MAX(CASE WHEN pool = 'setup' THEN rate END) AS r_setup, MAX(CASE WHEN pool = 'handling' THEN rate END) AS r_move,
             MAX(CASE WHEN pool = 'engineering' THEN rate END) AS r_eng, MAX(CASE WHEN pool = 'volume' THEN rate END) AS r_vol FROM rates),
wo AS (
  SELECT w.work_order_id, p.family, w.quantity_completed, w.quantity_completed + w.quantity_scrapped AS started,
         p.rm_cost_cents, p.standard_cost_cents, p.list_price_cents
  FROM work_orders w JOIN products p USING (product_id) WHERE w.released_at >= DATE '{cal.bounds(P[0])[0]}'),
lg AS (SELECT work_order_id, SUM(setup_minutes_actual) AS setup_min, SUM(run_minutes_actual) AS run_min, COUNT(*) AS moves FROM production_log GROUP BY 1),
qe AS (SELECT work_order_id, COUNT(*) AS events FROM quality_events GROUP BY 1),
c AS (
  SELECT wo.family, SUM(wo.quantity_completed) AS units, SUM(wo.standard_cost_cents * wo.quantity_completed) AS std_total,
         SUM(wo.rm_cost_cents * wo.started + lg.run_min / 60.0 * {p['labor_rate_usd'] * 100:.4f}
             + lg.setup_min * r.r_setup + lg.moves * r.r_move + COALESCE(qe.events, 0) * r.r_eng + lg.run_min / 60.0 * r.r_vol) AS abc_total
  FROM wo JOIN lg USING (work_order_id) LEFT JOIN qe USING (work_order_id) CROSS JOIN r GROUP BY 1)
SELECT family, units, std_total * 1.0 / units AS std_unit_cents, abc_total * 1.0 / units AS abc_unit_cents,
       (std_total - abc_total) * 1.0 / abc_total AS distortion
FROM c ORDER BY distortion"""
        q_abc = run_reference_query(con, "tdabc_unit_cost_by_family", sql_abc, signature=["with", "left join", "join", "case when", "group by"],
                                    metric_keys=["abc_unit_cost_family_low_cents", "abc_unit_cost_family_high_cents", "distortion_low_family_pct"],
                                    checks=lambda df: {"distortion_low": float(df.set_index("family").loc[fams[2], "distortion"]),
                                                       "distortion_high": float(df.set_index("family").loc[fams[0], "distortion"])})
        sql_var = """
WITH v AS (
  SELECT j.period,
         SUM(CASE WHEN j.source = 'overhead' THEN l.debit_cents ELSE 0 END) AS overhead_incurred,
         SUM(CASE WHEN j.source = 'production' THEN l.credit_cents ELSE 0 END) AS overhead_applied
  FROM gl_lines l JOIN gl_journal j USING (entry_id) WHERE l.account_code = '5020' GROUP BY 1)
SELECT period, overhead_incurred, overhead_applied, overhead_incurred - overhead_applied AS underabsorbed,
       overhead_incurred - overhead_applied - LAG(overhead_incurred - overhead_applied) OVER (ORDER BY period) AS change_vs_prior
FROM v ORDER BY period"""
        q_var = run_reference_query(con, "underabsorbed_overhead_by_quarter", sql_var, signature=["over(", "lag(", "with", "join", "case when"],
                                    metric_keys=["underabsorbed_overhead_p8_cents"],
                                    checks=lambda df: {"underabsorbed_last": float(df["underabsorbed"].iloc[-1]),
                                                       "rising_from_p1": bool(df.set_index("period")["underabsorbed"].reindex(P).diff().dropna().gt(-0.02 * abs(df["underabsorbed"].iloc[-1])).all())})
        sql_mix = """
WITH q AS (
  SELECT period_of(s.invoiced_at) AS period, p.family, SUM(s.amount_cents) AS revenue,
         SUM(s.amount_cents - p.standard_cost_cents * s.quantity) AS reported_margin
  FROM sales_orders s JOIN products p USING (product_id) GROUP BY 1, 2),
sh AS (
  SELECT period, family, revenue, revenue * 1.0 / SUM(revenue) OVER (PARTITION BY period) AS revenue_share,
         reported_margin * 1.0 / revenue AS reported_margin_pct
  FROM q)
SELECT period, family, revenue, revenue_share, reported_margin_pct,
       revenue_share - LAG(revenue_share) OVER (PARTITION BY family ORDER BY period) AS share_change
FROM sh ORDER BY family, period"""
        q_mix = run_reference_query(con, "family_mix_and_reported_margin", sql_mix, signature=["over(", "partition by", "lag(", "with", "join"],
                                    metric_keys=["mix_shift_low_family_pts", "reported_margin_family_low_pct"],
                                    checks=lambda df: {"low_share_last": float(df[df["family"] == fams[2]]["revenue_share"].iloc[-1])})
        sql_comm = """
SELECT r.commission_basis, r.basis_since, COUNT(*) AS orders, SUM(s.amount_cents) AS revenue
FROM sales_orders s JOIN sales_reps r USING (sales_rep_id) GROUP BY 1, 2"""
        q_comm = run_reference_query(con, "commission_plan_basis", sql_comm, signature=["join", "group by", "count("], metric_keys=["mix_shift_low_family_pts"])
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_drv, q_abc],
            "magnitude": {**anomalies["A1"].magnitude, "distortion_low_family_pct": round(f["dist"]["low"], 6), "distortion_high_family_pct": round(f["dist"]["high"], 6)},
            "evidence_trail": [f"Setup minutes per unit: {fams[2]} {f['setup_per_unit']['low']:.2f} vs {fams[0]} {f['setup_per_unit']['high']:.3f} "
                               f"({f['setup_per_unit']['low'] / f['setup_per_unit']['high']:.0f}x).",
                               f"Re-costed with activity drivers, {fams[2]} costs {f['abc_unit']['low'] / 100:,.2f} USD vs a standard of {f['std_unit']['low'] / 100:,.2f}."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_mix, q_var, q_comm],
            "magnitude": {**anomalies["A2"].magnitude, "mix_shift_low_family_pts": round(f["mix_shift"], 6), "underabsorbed_overhead_p8_cents": float(f["underabs"][P[-1]])},
            "evidence_trail": [f"{fams[2]} revenue share {f['shares'][P[0]]['low']:.0%} -> {f['shares'][P[-1]]['low']:.0%}; reported margin "
                               f"{f['reported_margin']['low']:.0%}, the highest of the three families.",
                               f"Under-absorbed overhead {f['underabs'][P[0]] / 100:,.0f} -> {f['underabs'][P[-1]] / 100:,.0f} USD a quarter; operating income falls."]})
        if "A3" in anomalies:
            sql_tp = f"""
WITH bn AS (SELECT product_id, run_minutes_per_unit AS bn_min FROM routings WHERE work_center_id = '{BOTTLENECK}'),
so AS (SELECT product_id, SUM(amount_cents) AS revenue, SUM(quantity) AS units FROM sales_orders GROUP BY 1)
SELECT p.family, SUM(so.revenue - p.rm_cost_cents * so.units) * 1.0 / SUM(so.units * bn.bn_min) AS throughput_per_bn_minute,
       SUM(so.units * bn.bn_min) AS bottleneck_minutes
FROM so JOIN products p USING (product_id) JOIN bn USING (product_id) GROUP BY 1 ORDER BY 2 DESC"""
            q_tp = run_reference_query(con, "throughput_per_bottleneck_minute", sql_tp, signature=["with", "join", "group by", "sum("],
                                       metric_keys=["throughput_per_bottleneck_min_low_cents"], checks=lambda df: {"best_family": str(df.iloc[0]["family"])})
            sql_load = """
SELECT l.work_center_id, w.name, SUM(l.setup_minutes_actual + l.run_minutes_actual) / 60.0 AS hours,
       SUM(l.setup_minutes_actual + l.run_minutes_actual) / 60.0 / MAX(w.capacity_hours_per_week) AS weeks_of_capacity_used
FROM production_log l JOIN work_centers w USING (work_center_id) GROUP BY 1, 2 ORDER BY weeks_of_capacity_used DESC"""
            q_load = run_reference_query(con, "work_center_load", sql_load, signature=["join", "group by", "sum("], metric_keys=["throughput_per_bottleneck_min_low_cents"],
                                         checks=lambda df: {"most_loaded": str(df.iloc[0]["work_center_id"])})
            anomalies["A3"] = anomalies["A3"].model_copy(update={
                "reference_sql": [q_tp, q_load],
                "magnitude": {"throughput_per_bn_min_low_cents": round(f["tput"]["low"], 2), "throughput_per_bn_min_high_cents": round(f["tput"]["high"], 2)},
                "evidence_trail": [f"Finishing is the most loaded work centre; per finishing minute {fams[2]} earns {f['tput']['low'] / 100:,.2f} USD "
                                   f"vs {f['tput']['high'] / 100:,.2f} for {fams[0]}."]})
        ordered = [anomalies[k] for k in sorted(anomalies)]
        herrings_all = [
            f"Scrap is rising - it is, slightly, in {fams[2]} (5040), but it explains well under a point of margin; setups and engineering drive the variance.",
            "Labour rates went up - payroll per direct-labour hour is flat and run minutes per unit are unchanged.",
            f"Discounting on {fams[0]} - discount creep of about a point exists, but {fams[0]} is still the most profitable line on an ABC basis.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        root = (f"Overhead is allocated with a single plant-wide rate per direct-labour hour. {fams[2]} (low volume, complex) consumes "
                f"{f['setup_per_unit']['low'] / f['setup_per_unit']['high']:.0f}x the setup minutes per unit of {fams[0]} plus most engineering changes, "
                f"so its standard cost is {abs(f['dist']['low']):.0%} below its activity-based cost: reported margin {f['reported_margin']['low']:.0%} vs a true "
                f"margin of {f['true_margin']['low']:.0%}. Reps paid on reported margin shifted the mix +{f['mix_shift'] * 100:.0f}pt toward it, overhead "
                f"spending followed the activity while absorption followed labour hours, so the under-absorbed variance reached "
                f"{f['underabs'][P[-1]] / 100:,.0f} USD in {P[-1]} and operating income fell. At the {P[0]} mix the latest quarter would have earned "
                f"{f['oi_cf'] / 100:,.0f} USD instead of {f['oi8'] / 100:,.0f}."
                + (f" Finishing is the bottleneck and {fams[2]} earns the most per finishing minute: reprice it rather than drop it." if p["throughput_twist"] else ""))
        metrics = _with_hints(metrics, _HINTS)
        return GroundTruth(
            root_cause=root, anomalies=ordered, red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=list(dict.fromkeys(q.sql for a in ordered for q in a.reference_sql)),
                python=["import pandas as pd\n"
                        "pools = pd.read_sql('SELECT * FROM overhead_pools', con)\n"
                        "rate = pools.groupby('pool').cost_cents.sum() / pools.groupby('pool').practical_capacity_qty.sum()  # TDABC capacity cost rates\n"
                        "log = pd.read_sql('SELECT l.*, p.family FROM production_log l JOIN work_orders w USING (work_order_id) JOIN products p USING (product_id)', con)\n"
                        "# driver usage per family x rate -> ABC overhead; add RM + DL; divide by good units; margin waterfall; counterfactual mix\n"],
                memo_bullets=[
                    "Standard costs use one plant-wide labour-hour rate; setups, moves and engineering changes are not labour-hour driven.",
                    f"Time-driven ABC: capacity cost rate x time per activity; the data is already in production_log and quality_events.",
                    f"{fams[2]} is under-costed and underpriced; reprice it (+10-30%) before considering exit - only ~60% of its overhead is avoidable short-run.",
                    "Change the commission basis from reported margin to ABC margin; reduce setups (SMED) and batch the low-volume line.",
                    "The growing manufacturing variance is the accounting trace of the distortion, not a production problem.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, four stakeholders and the spec timeline."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        fams = p["families"]
        names = rng.pick_names(4)
        reveal = [
            f"Family P&L at standard cost: {fams[2]} reports a {f['reported_margin']['low']:.0%} margin, {fams[0]} {f['reported_margin']['high']:.0%}.",
            f"{fams[2]}'s share of revenue rose from {f['shares'][P[0]]['low']:.0%} to {f['shares'][P[-1]]['low']:.0%}.",
            f"CEO: 'Our premium line, {fams[2]}, is our best product.'",
            "The P&L shows a growing line labelled 'manufacturing variances'.",
            "Sales commissions are computed on reported margin.",
        ]
        conceal = ["allocation base is direct labor hours", "activity-based costing", "setup minutes and engineering changes per unit by family",
                   "the hidden abc cost table", "the counterfactual operating income at the original mix", "A1", "A2", *(["A3"] if p["throughput_twist"] else [])]
        stakeholders = [
            Stakeholder(name=names[0], role="COO", agenda=f"Win capex for a second finishing line for {fams[2]}.", disposition="driven",
                        knows=["changeovers are eating the plant"], hides=["setups doubled since the mix shifted"]),
            Stakeholder(name=names[1], role="Controller", agenda="A clean close.", disposition="careful",
                        knows=["the variances are unusual"], hides=["the standard costs were last reset three years ago"]),
            Stakeholder(name=names[2], role="VP Sales", agenda="Keep the margin-based commission plan.", disposition="confident",
                        knows=[f"{fams[2]} is easy to sell"], hides=[f"{fams[2]} sells easily because it is cheap for what it is"]),
            Stakeholder(name=names[3], role="Plant Manager", agenda="Hit the schedule.", disposition="frank when asked",
                        knows=["the real time each order takes on the floor"], hides=[]),
        ]
        tl = [
            TimelineEvent(period=P[1], headline="Commission plan moved to margin basis", detail="Reps now earn on reported margin."),
            TimelineEvent(period=P[2], headline="Premium line marketing push", detail=f"Campaign for {fams[2]}."),
            TimelineEvent(period=P[4], headline="Scrap reduction initiative", detail="Quality launches a scrap program.", is_red_herring=True),
            TimelineEvent(period=P[5], headline="Capex request for a second finishing line", detail="COO asks the board for capacity."),
            TimelineEvent(period=P[6], headline="Variance line flagged by auditors", detail="Management letter comment on manufacturing variances."),
            TimelineEvent(period=P[7], headline="Operating income miss despite record premium sales", detail="OI below plan."),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[3], headline="Union wage agreement signed", detail="Labour rates held flat for two years.", is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[6], headline=f"Price promotion on {fams[0]}", detail="Distributor discount increased by a point.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["family_pnl", "commission_plan_memo", "plant_capacity_chart", "auditor_management_letter_excerpt"],
                           architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Spec levers (categorical levers integer-coded); defaults are the status quo."""
        return [
            Lever(key="price_change_family_low_pct", label="Price change, low-volume family", min=-10, max=40, default=0, unit="%",
                  effect="Volume responds with elasticity ~-1.2; true margin improves."),
            Lever(key="price_change_family_high_pct", label="Price change, high-volume family", min=-15, max=10, default=0, unit="%",
                  effect="Volume responds with elasticity ~-1.8."),
            Lever(key="discontinue_family_low", label="Discontinue the low-volume family (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Removes its revenue and only the avoidable ~60% of its activity cost."),
            Lever(key="setup_reduction_pct", label="Setup reduction program (SMED)", min=0, max=50, default=0, unit="%",
                  effect="Cuts setup minutes and setup-pool cost (next quarter); one-off cost ~1.5% of revenue."),
            Lever(key="commission_basis", label="Commission basis (0 reported margin, 1 ABC margin)", min=0, max=1, default=0, unit="code",
                  effect="Mix drifts back ~1pt a quarter."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model linearised at P8 (module docstring)."""
        p = state.params
        f = self._facts(state)
        P = state.periods
        so = state.extras["so"]
        s8, e8 = state.calendar.bounds(P[-1])
        q8 = so[(so["invoiced_at"] >= pd.Timestamp(s8)) & (so["invoiced_at"] <= pd.Timestamp(e8) + EOD)]
        rev = {k: float(q8.loc[q8["_family"] == k, "amount_cents"].sum()) for k in FAMILY_KEYS}
        units = {k: float(q8.loc[q8["_family"] == k, "quantity"].sum()) for k in FAMILY_KEYS}
        abc_cost = {k: units[k] * f["abc_unit"][k] for k in FAMILY_KEYS}
        pools8 = self._pool_table(state)
        pools8 = pools8[pools8["period"] == P[-1]].set_index("pool")["cost_cents"]
        inc8 = next(i for i in state.statements.income if i.period == P[-1])  # type: ignore[union-attr]
        setup_min8 = float(state.extras["prod"]["setup_min"].iloc[-3:].sum())
        low_setup_share = float(f["setup_per_unit"]["low"] * units["low"] / max(1.0, setup_min8))
        kpis = {
            "revenue_cents": float(inc8.revenue_cents), "operating_income_cents": float(inc8.operating_income_cents),
            "underabsorbed_overhead_cents": float(f["underabs"][P[-1]]), "low_family_revenue_share": float(f["shares"][P[-1]]["low"]),
            "units_low": units["low"], "units_high": units["high"], "setup_minutes": setup_min8, "true_margin_low": float(f["true_margin"]["low"]),
            "fg_inventory_cents": float(state.ledger.trial_balance(P[-1])["1220"]), "cash_cents": float(state.ledger.trial_balance(P[-1])["1000"]),
        }
        el, eh = p["elasticity_low"], p["elasticity_high"]
        avoid = p["avoidable_share"]
        var_low = units["low"] * (state.extras["products"].query("family_key == 'low'")["rm_cents"].mean() + state.extras["products"].query("family_key == 'low'")["dl_cents"].mean())
        oh_low = abc_cost["low"] - var_low
        effects = [
            {"lever": "price_change_family_low_pct", "kpi": "units_low", "delta_per_unit": el * units["low"] / 100, "lag_periods": 0},
            {"lever": "price_change_family_low_pct", "kpi": "revenue_cents", "delta_per_unit": rev["low"] * (1 + el) / 100, "lag_periods": 0},
            {"lever": "price_change_family_low_pct", "kpi": "operating_income_cents", "delta_per_unit": (rev["low"] + el * (rev["low"] - abc_cost["low"])) / 100, "lag_periods": 0},
            {"lever": "price_change_family_low_pct", "kpi": "true_margin_low", "delta_per_unit": (1 - f["true_margin"]["low"]) / 100, "lag_periods": 0},
            {"lever": "price_change_family_high_pct", "kpi": "units_high", "delta_per_unit": eh * units["high"] / 100, "lag_periods": 0},
            {"lever": "price_change_family_high_pct", "kpi": "revenue_cents", "delta_per_unit": rev["high"] * (1 + eh) / 100, "lag_periods": 0},
            {"lever": "price_change_family_high_pct", "kpi": "operating_income_cents", "delta_per_unit": (rev["high"] + eh * (rev["high"] - abc_cost["high"])) / 100, "lag_periods": 0},
            {"lever": "discontinue_family_low", "kpi": "revenue_cents", "delta_per_unit": -rev["low"], "lag_periods": 0},
            {"lever": "discontinue_family_low", "kpi": "operating_income_cents", "delta_per_unit": -(rev["low"] - var_low - avoid * oh_low), "lag_periods": 0},
            {"lever": "discontinue_family_low", "kpi": "units_low", "delta_per_unit": -units["low"], "lag_periods": 0},
            {"lever": "discontinue_family_low", "kpi": "low_family_revenue_share", "delta_per_unit": -kpis["low_family_revenue_share"], "lag_periods": 0},
            {"lever": "setup_reduction_pct", "kpi": "setup_minutes", "delta_per_unit": -setup_min8 / 100, "lag_periods": 1},
            {"lever": "setup_reduction_pct", "kpi": "operating_income_cents", "delta_per_unit": float(pools8.get("setup", 0.0)) / 100, "lag_periods": 1},
            {"lever": "setup_reduction_pct", "kpi": "underabsorbed_overhead_cents", "delta_per_unit": -float(pools8.get("setup", 0.0)) / 100, "lag_periods": 1},
            {"lever": "setup_reduction_pct", "kpi": "cash_cents", "delta_per_unit": -0.015 * kpis["revenue_cents"] / 50, "lag_periods": 0},
            {"lever": "commission_basis", "kpi": "low_family_revenue_share", "delta_per_unit": -0.01, "lag_periods": 1},
            {"lever": "commission_basis", "kpi": "operating_income_cents", "delta_per_unit": 0.01 * f["rev8"] * (f["true_margin"]["high"] - f["true_margin"]["low"]), "lag_periods": 1},
        ]
        oi_drop = (f["oi8"] - float(next(i for i in state.statements.income if i.period == P[4]).operating_income_cents)) / 3  # type: ignore[union-attr]
        drag = {"low_family_revenue_share": p["mix_drift_pts"], "operating_income_cents": min(0.0, oi_drop),
                "underabsorbed_overhead_cents": max(0.0, (f["underabs"][P[-1]] - f["underabs"][P[4]]) / 3)}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"price_change_family_low_pct": 10.0, "commission_basis": 1.0},
                "bounds": {"low_family_revenue_share": [0.0, 1.0], "units_low": [0.0, None], "units_high": [0.0, None], "setup_minutes": [0.0, None]}}


def _with_hints(metrics: list[Any], hints: dict[str, str]) -> list[Any]:
    """Attach a learner-facing hint to every expected metric (``CasePublic.asks``); unknown keys get a generic hint."""
    out = []
    for m in metrics:
        h = m.hint or hints.get(m.key) or next((v for k, v in hints.items() if k.endswith("*") and m.key.startswith(k[:-1])), "")
        out.append(m.model_copy(update={"hint": h or f"Compute {m.label.lower()} from the case database ({m.unit})."}))
    return out

_HINTS: dict[str, str] = {'plantwide_rate_per_dlh_cents': 'Total overhead cost / total direct-labour (run) hours over the case quarters.', 'abc_unit_cost_family_*': 'Time-driven ABC: pool rate (cost / practical capacity) x driver usage, plus materials and labour, per good unit.', 'distortion_low_family_pct': '(standard unit cost - ABC unit cost) / ABC unit cost.', 'true_margin_family_low_pct': '(average net price - ABC unit cost) / average net price.', 'reported_margin_family_low_pct': '(average net price - standard unit cost) / average net price.', 'underabsorbed_overhead_p8_cents': 'Net activity on account 5020 in the latest quarter (incurred - applied).', 'mix_shift_low_family_pts': "Change in the family's share of invoiced revenue from the first to the latest quarter.", 'operating_income_at_p1_mix_cents': "Re-weight the latest quarter's revenue to the first quarter's family mix at true (ABC) margins.", 'throughput_per_bottleneck_min_*': '(net price - materials) per unit / bottleneck run minutes per unit.'}
