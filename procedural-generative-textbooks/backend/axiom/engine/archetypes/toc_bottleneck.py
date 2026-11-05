"""``toc_bottleneck`` - "Herbie Moves" (archetypes.md section 5).

A capacity-constrained plant (discrete manufacturer, or the contract-packaging / kitting arm of a
3PL) runs push release and measures every work center on local utilization. Orders exceed the
capacity of one station (the constraint ``b``), so shipments are flat while WIP piles up in front
of ``b``, overtime is authorised at quarter ends, late-delivery penalties hit revenue - and
management's capex request targets the finishing line because that is where the shipping office
sees a queue at month end.

Mechanics (fluid model + per-work-order timeline; every invariant and sub-ledger tie holds)
-------------------------------------------------------------------------------------------
* Serial line of ``n_stations`` (5 / 6 / 7-8 by difficulty), rated capacities ``c_i`` in units per
  regular hour (``hours_per_month`` = 320, two shifts). ``c_b`` is the constraint; the next-tightest
  station ``b2`` sits *upstream* of ``b`` (so after ``b`` is elevated the constraint migrates to
  ``b2``: ``migration_threshold = c_b2/c_b - 1``). Other stations run at 55-78% utilization;
  finishing (last) below 85%.
* Demand (release rate) ``lambda_m = lambda_final * (0.93 + 0.07 m/26) * LN(0, 0.03)`` units/hour,
  ``lambda_final = c_b * U(0.95, 1.05)``. Fluid model at ``b`` per month:
  ``cap_m = c_b * (H*(0.985 - s_m) + OT_m*(1 - s_m))``, ``O_m = min(Q_{m-1} + R_m, cap_m)``,
  ``Q_m = Q_{m-1} + R_m - O_m``. ``s_m`` = setup share of ``b`` (``s0`` 3-5%; the A3 policy raises it
  to ``setup_share_b`` from P3, when the local-efficiency KPI is introduced). Quarter-end overtime
  ``OT_m`` clears ``ot_clear`` (35-55%) of the quarter's backlog increase (<= 30% of H), so WIP
  still climbs and overtime is the "expediting" cost trace (5010, source ``overtime``).
* Work orders: ``N_WO`` sized to the row budget, split across months proportionally to ``R_m``.
  Each WO flows through the upstream stations with Kingman waits; at ``b`` departures follow the
  inverse of the cumulative fluid output curve (exact FIFO for a fluid queue), so the queue in front
  of ``b`` is real in the data; downstream stations add Kingman waits (finishing gets a month-end
  surge x3-5: the red herring). Final inspection is at the end of the line ("that's where the
  gauge is"): ``scrap_after_b_pct`` of units are scrapped *after* consuming constraint time; 50-70%
  of those defects originate upstream of ``b`` (``quality_events.origin_work_center_id``).
* Naive vs true constraint (practitioner+): the efficiency dashboard ranks stations by *run*
  utilization (``run_minutes / available``). Because ``b`` loses 10-18% of its time to setups and
  ``b2`` processes the full push release, ``lambda/c_b2`` (0.89-0.94) exceeds ``b``'s run share
  (~0.82-0.85) while ``b``'s true utilization (setup + run) is ~0.98: the naive ranking points at
  ``b2``. At foundation ``lambda/c_b2`` is 0.66-0.74, so both rankings agree. The measurement
  window is the trailing four quarters (P5-P8), i.e. after the KPI change.
* GL (absorption costing): RM purchases Dr 1200/Cr 2000; release Dr 1210 (RM + upstream
  conversion), Cr 1200, Cr 5020 (overhead applied); completion/shipment Dr 5000 standard cost of good
  units, Dr 5040 WIP cost of scrapped units, Cr 1210, Cr 5020 downstream conversion applied; sale Dr
  1100/Cr 4000; late penalties Dr 4110/Cr 1100 (credit memos); freight + expedite freight 5030;
  direct labour (regular 5010 ``payroll``, overtime premium 5010 ``overtime``), actual overhead 5020.
  WIP (1210) rises every month the queue at ``b`` grows. Ties: AR (open invoices net of credit memos),
  WIP (open work orders at standard WIP cost), AP (unpaid RM receipts), revenue.

Documented simplifications
* Schema ``manufacturing_mes`` for both industries (3PL = contract packaging/kitting stations).
* ``production_log`` stores machine time: ``end_at - start_at = setup_minutes_actual +
  run_minutes_actual``; utilization SQL sums those minutes over ``labor_hours`` (regular + overtime).
* ``c_b2/c_b`` is derived from the naive-vs-true calibration (4-18% at practitioner+, 28-60% at
  foundation) rather than drawn independently from ``U(0.86,0.94)/U(0.70,0.82)``.
* Levers are 5 (spec lists 6): the subcontracting lever is folded into ``capacity_add_bottleneck_pct``.

Simulation (reduced form): ``capacity_add_bottleneck_pct`` adds ``cap_b/100`` units/month per
point with ``bounds`` capping throughput at ``min(demand, b2 ceiling)`` (constraint migration) and
``b2_utilization`` rising to 1.0; ``capacity_add_finishing_pct`` only adds depreciation (the trap);
``move_inspection_before_b`` cuts scrap-after-b by 70% and recovers those constraint minutes;
``batch_size_b_multiplier`` recovers ``s_b/4`` of capacity per unit (setup share / multiplier,
linearised) with a small WIP/cycle-time penalty; ``release_policy_dbr`` collapses WIP to a
one-week buffer and cuts cycle time by 60% with a one-quarter lag. ``anomaly_drag`` = average
quarterly P6-P8 drift of WIP, overtime, cycle time and late share; ``resolved_if`` needs inspection
moved and batch multiplier >= 2.
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
    Ledger,
    SubledgerTie,
    batch_end_dates,
    cents,
    split_cents,
)
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state, kingman_wq, littles_law, mm1
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
    Station,
    TimelineEvent,
)

logger = logging.getLogger("axiom.archetypes.toc_bottleneck")

STATIONS: dict[str, tuple[str, ...]] = {
    "manufacturing": ("Saw & Cut", "CNC Machining", "Deburr", "Heat Treat", "Weld", "Paint", "Assembly", "Finishing & Pack"),
    "3pl": ("Receiving", "Kitting", "Bundling", "Labeling", "Shrink Wrap", "Palletize", "QA Audit", "Pack-out"),
}
FAMILIES: dict[str, tuple[str, ...]] = {
    "manufacturing": ("Brackets", "Housings", "Shafts"),
    "3pl": ("Retail kits", "Subscription boxes", "Promo bundles"),
}
DEFECTS: dict[str, tuple[str, ...]] = {
    "origin": ("DIM-OOT", "BURR", "SURF-FINISH"),
    "other": ("HANDLING", "LABEL", "COSMETIC"),
}
H_MONTH = 320.0  # regular hours per month (two shifts)
UTIL_TARGET = 0.985  # busy share of b when saturated (minor starvation / breakdowns)
DAY_H = 24.0


def _split_int(total: int, weights: np.ndarray) -> np.ndarray:
    """Largest-remainder split of an integer total (every part >= 1 when total >= len)."""
    w = np.asarray(weights, dtype=float)
    k = len(w)
    base = np.ones(k, dtype=np.int64) if total >= k else np.zeros(k, dtype=np.int64)
    rest = int(total) - int(base.sum())
    raw = rest * w / w.sum()
    fl = np.floor(raw).astype(np.int64)
    rem = rest - int(fl.sum())
    order = np.argsort(-(raw - fl), kind="stable")
    fl[order[:rem]] += 1
    return base + fl


@register
class TocBottleneck(Archetype):
    """Herbie Moves: push release + local-efficiency KPIs in a constrained plant."""

    key = "toc_bottleneck"
    title = "Herbie Moves"
    pillars = ("operations_strategy", "accounting_finance", "python")
    industries = ("manufacturing", "3pl")
    schema_template = "manufacturing_mes"
    summary = ("Shipments are flat while WIP, overtime and late-delivery penalties climb; the plant manager wants capex "
               "for the finishing line, but is that really where the constraint is?")
    root_cause_terms = ("true constraint", "setup", "scrap after the constraint", "migration threshold")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 5 (see module docstring for the calibration)."""
        ind = industry or ("manufacturing" if rng.random() < 0.75 else "3pl")
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        lo, hi = pool.industry(ind).revenue_range_usd
        lo, hi = max(lo, 25e6), min(hi, 160e6)
        rev_usd = math.exp(rng.uniform(math.log(lo), math.log(max(hi, lo * 1.01))))

        def band(metric_key: str, q_lo: float, q_hi: float, ind_: str = ind) -> float:
            return bk.sample_between(pool, rng.spawn(f"bench:{metric_key}"), f"{ind_}.{metric_key}", ind_, q_lo, q_hi)

        n_st = difficulty_pick(difficulty, 5, 6, int(rng.integers(7, 9)))
        setup_share_b = magnitude(rng, difficulty, 0.10, 0.18)
        u_b = float(rng.uniform(0.95, 1.03))  # lambda_final / c_b
        if difficulty == "foundation":
            r2 = float(rng.uniform(0.66, 0.74))  # lambda / c_b2: naive ranking agrees with the true one
        else:
            r2 = float(rng.uniform(0.89, min(0.94, u_b - 0.04)))  # b2 run share exceeds b's run share
        c_b = float(rng.uniform(8.0, 24.0))
        lam_final = c_b * u_b
        gm = float(np.clip(band("gross_margin", 0.3, 0.7, "manufacturing"), 0.26, 0.42))
        rm_share = float(rng.uniform(0.36, 0.46))
        freight = float(rng.uniform(0.015, 0.03))
        p = {
            "industry": ind,
            "business_model": {"manufacturing": "make-to-order precision components manufacturer",
                               "3pl": "contract packaging and kitting operation of a logistics group"}[ind],
            "revenue_ttm_cents": int(rev_usd * 100),
            "n_stations": n_st,
            "n_families": difficulty_pick(difficulty, 2, 2, 3),
            "hours_per_month": H_MONTH,
            "c_b": c_b,
            "lambda_final": lam_final,
            "u_b": u_b,
            "r2": r2,
            "c_b2": lam_final / r2,
            "cv_arrival": float(rng.uniform(0.5, 1.2)),
            "cv_service": float(rng.uniform(0.5, 1.2)),
            "setup_share_base": float(rng.uniform(0.03, 0.05)),
            "setup_share_b": setup_share_b,
            "setup_min_b": float(np.clip(bk.sample_between(pool, rng.spawn("bench:setup"), "manufacturing.setup_time_minutes",
                                                          "manufacturing", 0.3, 0.55), 25, 90)),
            "scrap_after_b_pct": magnitude(rng, difficulty, 0.02, 0.06),
            "defect_origin_upstream_share": float(rng.uniform(0.5, 0.7)),
            "ot_clear": float(rng.uniform(0.55, 0.75)),
            "ot_premium": float(rng.uniform(1.3, 1.5)),
            "late_penalty_pct_week": float(rng.uniform(0.01, 0.03)),
            "quote_buffer": float(rng.uniform(1.15, 1.35)),
            "gm": gm,
            "rm_share": rm_share,
            "freight_pct": freight,
            "conv_share": max(0.08, 1 - gm - rm_share - freight),
            "op_margin": float(rng.uniform(0.07, 0.11)),
            "wage_inflation_month": float(rng.uniform(0.002, 0.004)),
            "tax_rate": float(np.clip(band("effective_tax_rate", 0.4, 0.7, "manufacturing"), 0.15, 0.26)),
            "capex_pct": float(np.clip(band("capex_pct_revenue", 0.25, 0.5, "manufacturing"), 0.015, 0.05)),
            "interest_rate": float(rng.uniform(0.07, 0.10)),
            "terms_days": int(rng.choice((30, 45))),
            "dpo_days": float(rng.uniform(30, 50)),
            "kpi_month": 3 + 6,  # local-efficiency KPI introduced at the start of P3
            "capex_request_pct": float(rng.uniform(0.25, 0.40)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        budget = ROW_BUDGET[difficulty]
        gl_rows = difficulty_pick(difficulty, 2600, 2900, 11500)
        per_wo = n_st + 3.0  # production_log ops + work order + sales order + quality event (most WOs scrap something)
        p["n_work_orders"] = int(max(400, (budget * difficulty_pick(difficulty, 0.90, 0.90, 0.88) - gl_rows - 400) / per_wo))
        p["n_customers"] = difficulty_pick(difficulty, 25, 40, 60)
        p["description"] = (f"{cap_first(p['business_model'])} running a {n_st}-step serial line on two shifts; "
                            "work centers are measured on local utilization and work is released as orders arrive.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Stations, rated capacities, product families, routings and customers."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=False)  # base working-capital defaults: prepaid expenses (AP already comes from the purchasing / cost sub-ledger)
        p = state.params
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        n = p["n_stations"]
        names = STATIONS[p["industry"]]
        middle = list(range(2, len(names) - 1))
        keep = sorted(g.choice(middle, size=n - 3, replace=False).tolist())
        idx = [0, 1] + keep + [len(names) - 1]
        st_names = [names[i] for i in idx]
        b = int(g.integers(2, n - 1))  # never first/second or last
        b2 = int(g.integers(1, b))  # upstream: sees the full push release
        origin = 1  # machining / kitting: where most defects are created
        lam = p["lambda_final"]
        down_rate = p["c_b"] * (UTIL_TARGET - p["setup_share_b"]) * 1.05
        # every other station is looser than b2 (so b2 is where the constraint migrates) and loaded <= ~80%
        cap = np.zeros(n)
        for i in range(n):
            load = lam if i < b else down_rate
            if i == b:
                cap[i] = p["c_b"]
            elif i == b2:
                cap[i] = p["c_b2"]
            elif i == n - 1:
                cap[i] = max(p["c_b2"] * g.uniform(1.06, 1.25), load / g.uniform(0.70, 0.80))
            else:
                cap[i] = max(p["c_b2"] * g.uniform(1.08, 1.45), load / g.uniform(0.55, 0.78))
        setup = np.where(np.arange(n) == b, p["setup_min_b"], g.uniform(4, 10, n))
        rate = g.uniform(38, 62, n) * 100  # loaded labour + machine rate per hour (cents)
        wc = pd.DataFrame({
            "work_center_id": [f"WC{10 * (i + 1)}" for i in range(n)],
            "op_seq": [10 * (i + 1) for i in range(n)],
            "name": st_names,
            "capacity_units_per_hour": np.round(cap, 3),
            "hours_per_month": p["hours_per_month"],
            "crew_size": g.integers(3, 9, n),
            "cost_per_hour_cents": np.round(rate).astype(np.int64),
            "setup_minutes_avg": np.round(setup, 1),
            "efficiency_target": 0.95,
        })
        fam_names = FAMILIES[p["industry"]][: p["n_families"]]
        F = len(fam_names)
        mix = g.dirichlet(np.full(F, 6.0))
        rel_price = g.uniform(0.75, 1.35, F)
        spread = difficulty_pick(p["difficulty"], 0.12, 0.2, 0.35)
        mult = np.clip(1 + g.normal(0, spread, (n, F)), 0.55, 1.6)
        mult = mult / (mult @ mix)[:, None]  # mix-weighted mean run time per station = 1/c_i
        rm_f = np.clip(p["rm_share"] * g.uniform(0.85, 1.15, F), 0.25, 0.55)
        state.extras.update({"b": b, "b2": b2, "origin": origin, "cap": cap, "setup": setup, "mix": mix, "rel_price": rel_price,
                             "mult": mult, "rm_f": rm_f, "fam_names": list(fam_names)})
        state.frames["work_centers"] = wc
        cust = pd.DataFrame({
            "customer_id": [f"C{i:04d}" for i in range(1, p["n_customers"] + 1)],
            "name": [rng.company_name("manufacturing") for _ in range(p["n_customers"])],
            "segment": np.where(g.random(p["n_customers"]) < 0.3, "key_account", "standard"),
        })
        state.frames["customers"] = cust

    # ------------------------------------------------------------------ anomalies

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """A1 misidentified constraint (capex request), A2 scrap after b, A3 setup policy, A4 migration."""
        p = state.params
        P = state.periods
        wc = state.frames["work_centers"]
        b, b2, n = state.extras["b"], state.extras["b2"], p["n_stations"]
        nm = wc["name"].tolist()
        capex = np.zeros(n, dtype=np.int64)
        capex[n - 1] = cents(p["revenue_ttm_cents"] * p["capex_request_pct"] * 0.08 / 1000) * 1000
        wc["planned_capex_cents"] = capex
        wc["capex_status"] = np.where(capex > 0, "requested", "")
        state.extras["capex_request_cents"] = int(capex[n - 1])
        kpi_p = P[2]
        return [
            Anomaly(id="A1", kind="misidentified_constraint", pillar="operations_strategy", title="Capex request targets a non-constraint",
                    mechanism=(f"Push release at ~{p['u_b']:.2f}x the rated capacity of {nm[b]} (the constraint, c={p['c_b']:.1f}/h) builds a queue "
                               f"in front of it; the {state.extras['capex_request_cents'] / 100:,.0f} USD capex request targets {nm[-1]}, whose month-end "
                               f"surge queue is what the shipping office sees. The efficiency dashboard (run time only) ranks {nm[b2] if p['difficulty'] != 'foundation' else nm[b]} first."),
                    periods=list(P[4:]), affected_accounts=["1210", "5010", "4110"], affected_tables=["work_centers", "production_log", "work_orders"],
                    magnitude={"migration_threshold_pct": round(p["c_b2"] / p["c_b"] - 1, 6), "release_over_capacity": round(p["u_b"], 6)}, severity="high"),
            Anomaly(id="A2", kind="constraint_waste_scrap", pillar="operations_strategy", title="Scrap detected after the constraint",
                    mechanism=(f"Final inspection is at the end of the line, so {p['scrap_after_b_pct']:.1%} of units are scrapped after consuming "
                               f"{nm[b]} time; {p['defect_origin_upstream_share']:.0%} of the defects originate at {nm[state.extras['origin']]}."),
                    periods=list(P), affected_accounts=["5040", "1210"], affected_tables=["quality_events", "work_orders"],
                    magnitude={"scrap_after_b_pct": round(p["scrap_after_b_pct"], 6), "defect_origin_upstream_share": round(p["defect_origin_upstream_share"], 6)},
                    severity="medium"),
            Anomaly(id="A3", kind="constraint_setup_policy", pillar="operations_strategy", title="Small batches at the constraint",
                    mechanism=(f"From {kpi_p} (local-efficiency KPI, 'be responsive') {nm[b]} splits work orders into small transfer batches: setups "
                               f"of ~{p['setup_min_b']:.0f} min consume {p['setup_share_b']:.1%} of its time (was {p['setup_share_base']:.1%})."),
                    periods=list(P[2:]), affected_accounts=["5010", "1210"], affected_tables=["production_log", "work_orders"],
                    magnitude={"setup_share_b": round(p["setup_share_b"], 6), "setup_min_b": round(p["setup_min_b"], 3)}, severity="high"),
            Anomaly(id="A4", kind="constraint_migration", pillar="operations_strategy", title="Constraint migrates once elevated",
                    mechanism=(f"Elevating {nm[b]} by more than c_b2/c_b - 1 = {p['c_b2'] / p['c_b'] - 1:.1%} moves the constraint to {nm[b2]}; "
                               "throughput gains cap there (quarter simulation)."),
                    periods=[P[-1]], affected_accounts=["4000", "1210"], affected_tables=["work_centers", "production_log"],
                    magnitude={"c_b": round(p["c_b"], 6), "c_b2": round(p["c_b2"], 6)}, severity="medium"),
        ]

    # ------------------------------------------------------------------ simulation of the line

    def _fluid(self, state: WorldState, g: np.random.Generator) -> dict[str, np.ndarray]:
        p = state.params
        n_m = len(state.extras["months"])
        H = p["hours_per_month"]
        c_b = p["c_b"]
        lam = p["lambda_final"] * (0.93 + 0.07 * np.arange(n_m) / (n_m - 1)) * g.lognormal(0, 0.03, n_m)
        R = lam * H
        s = np.where(np.arange(n_m) < p["kpi_month"], p["setup_share_base"], p["setup_share_b"])
        Q0 = 0.15 * c_b * H * (UTIL_TARGET - s[0])
        Q = np.zeros(n_m)
        O = np.zeros(n_m)
        OT = np.zeros(n_m)
        q_prev = Q0
        q_qstart = Q0
        for m in range(n_m):
            if m % 3 == 0:
                q_qstart = q_prev
            cap_reg = c_b * H * (UTIL_TARGET - s[m])
            ot = 0.0
            if m % 3 == 2:
                incr = q_prev + R[m] - cap_reg - q_qstart
                if incr > 0:
                    ot = min(0.35 * H, p["ot_clear"] * incr / (c_b * (1 - s[m])))
            cap = cap_reg + c_b * ot * (1 - s[m])
            O[m] = min(q_prev + R[m], cap)
            Q[m] = q_prev + R[m] - O[m]
            OT[m] = ot
            q_prev = Q[m]
        return {"lam": lam, "R": R, "s": s, "Q0": np.array([Q0]), "Q": Q, "O": O, "OT": OT}

    def _simulate_line(self, ctx: BuildContext, state: WorldState) -> None:
        """Fluid model, work orders and their per-operation timeline."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("line").generator
        months = state.extras["months"]
        n_m = len(months)
        start = pd.Timestamp(cal.start)
        knots = np.array([(pd.Timestamp(ms) - start).total_seconds() / 3600 for ms in months]
                         + [(pd.Timestamp(cal.end) + pd.Timedelta(days=1) - start).total_seconds() / 3600])
        T_end = knots[-1]
        fl = self._fluid(state, g)
        state.extras["fluid"] = fl
        b, n = state.extras["b"], p["n_stations"]
        cap, setup, mix, mult = state.extras["cap"], state.extras["setup"], state.extras["mix"], state.extras["mult"]
        F = len(mix)
        H = p["hours_per_month"]
        # ---- work orders (incl. opening WIP queued at b)
        N = p["n_work_orders"]
        counts = np.maximum(4, np.round(N * fl["R"] / fl["R"].sum())).astype(int)
        k0 = max(3, int(round(counts[0] * fl["Q0"][0] / fl["R"][0])))
        qty_parts, rel_parts, month_parts = [], [], []
        q0 = _split_int(int(round(fl["Q0"][0])), g.gamma(4.0, size=k0))
        qty_parts.append(q0)
        rel_parts.append(np.sort(-g.uniform(24, 21 * 24, k0)))  # released during the 3 weeks before the books open
        month_parts.append(np.full(k0, -1))
        for m in range(n_m):
            k = int(counts[m])
            qty_parts.append(_split_int(int(round(fl["R"][m])), g.gamma(4.0, size=k)))
            rel_parts.append(np.sort(g.uniform(knots[m], knots[m + 1], k)))
            month_parts.append(np.full(k, m))
        qty = np.concatenate(qty_parts).astype(np.int64)
        rel = np.concatenate(rel_parts)
        rel_m = np.concatenate(month_parts)
        W = len(qty)
        fam = g.choice(F, size=W, p=mix)
        prod_k = g.integers(0, 4, W)
        opening = rel_m < 0
        # ---- per-operation times (hours since books open)
        run_min = 60.0 / cap  # minutes per standard unit
        q_in = np.full((W, n), np.nan)
        st = np.full((W, n), np.nan)
        en = np.full((W, n), np.nan)
        setup_min = np.zeros((W, n))
        run_act = np.zeros((W, n))
        setups_n = np.ones((W, n), dtype=np.int64)
        ca, cs = p["cv_arrival"], p["cv_service"]
        month_of = np.clip(np.searchsorted(knots, np.maximum(rel, 0), side="right") - 1, 0, n_m - 1)
        t = rel.copy()
        lam_wo = fl["lam"][month_of]
        for i in range(b):
            dur_run = qty * run_min[i] * mult[i, fam] * g.lognormal(0, 0.06, W)
            su = setup[i] * g.lognormal(0, 0.15, W)
            dur = (dur_run + su) / 60
            te = float(np.mean(dur))
            rho = np.minimum(lam_wo / cap[i], 0.97)
            wq = (rho / (1 - rho)) * ((ca * ca + cs * cs) / 2) * te * g.exponential(1.0, W)
            q_in[:, i] = t
            st[:, i] = t + wq
            en[:, i] = st[:, i] + dur
            setup_min[:, i], run_act[:, i] = su, dur_run
            t = en[:, i] + g.uniform(0.5, 4.0, W)
        # ---- the constraint: FIFO departures from the cumulative fluid output curve
        arr_b = t
        order = np.argsort(arr_b, kind="stable")
        cum = np.cumsum(qty[order].astype(float))
        cumO = np.concatenate([[0.0], np.cumsum(fl["O"])])
        dep = np.where(cum <= cumO[-1], np.interp(cum, cumO, knots), np.inf)
        dep_fluid = np.empty(W)
        dep_fluid[order] = dep
        proc_m = np.clip(np.searchsorted(knots, np.minimum(dep_fluid, T_end - 1e-6), side="right") - 1, 0, n_m - 1)
        s_m = fl["s"][proc_m]
        wo_in_m = np.bincount(proc_m[dep_fluid < T_end], minlength=n_m).astype(float)
        setups_per_wo = s_m * (H + fl["OT"][proc_m]) * 60 / p["setup_min_b"] / np.maximum(1.0, wo_in_m[proc_m])
        # changeovers: campaigns share a setup (0 for most WOs at baseline); the A3 policy splits WOs into small transfer batches
        k_set = (np.floor(setups_per_wo) + (g.random(W) < (setups_per_wo - np.floor(setups_per_wo)))).astype(np.int64)
        su_b = k_set * p["setup_min_b"] * g.lognormal(0, 0.08, W)
        run_b = qty * run_min[b] * mult[b, fam] * g.lognormal(0, 0.04, W)
        dur_b = (su_b + run_b) / 60
        en_b = np.maximum(dep_fluid, arr_b + dur_b)
        q_in[:, b], en[:, b] = arr_b, en_b
        st[:, b] = en_b - dur_b
        setup_min[:, b], run_act[:, b], setups_n[:, b] = su_b, run_b, k_set
        t = en_b + g.uniform(0.5, 4.0, W)
        down_lam = (fl["O"] / H)[proc_m]
        for i in range(b + 1, n):
            dur_run = qty * run_min[i] * mult[i, fam] * g.lognormal(0, 0.06, W)
            su = setup[i] * g.lognormal(0, 0.15, W)
            dur = (dur_run + su) / 60
            te = float(np.mean(dur[np.isfinite(t)])) if np.isfinite(t).any() else float(np.mean(dur))
            rho = np.minimum(down_lam / cap[i], 0.97)
            wq = (rho / (1 - rho)) * ((ca * ca + cs * cs) / 2) * te * g.exponential(1.0, W)
            if i == n - 1:  # month-end surge at finishing (what the shipping office sees)
                tt = np.where(np.isfinite(t), t, 0.0)
                day = pd.to_datetime(start + pd.to_timedelta(tt, unit="h")).day.to_numpy()
                wq = np.where(day >= 25, wq * g.uniform(3, 5, W) + g.uniform(8, 30, W), wq)
            q_in[:, i] = t
            st[:, i] = t + wq
            en[:, i] = st[:, i] + dur
            setup_min[:, i], run_act[:, i] = su, dur_run
            t = en[:, i] + g.uniform(0.5, 4.0, W)
        ship = en[:, n - 1] + g.uniform(1.0, 20.0, W)
        shipped = np.isfinite(ship) & (ship <= T_end)
        scrap = np.where(shipped, g.binomial(qty, p["scrap_after_b_pct"]), 0).astype(np.int64)
        good = qty - scrap
        state.extras.update({"knots": knots, "T_end": T_end, "W": W, "qty": qty, "rel": rel, "rel_m": rel_m, "fam": fam, "prod_k": prod_k,
                             "opening_wo": opening, "q_in": q_in, "st": st, "en": en, "setup_min": setup_min, "run_act": run_act,
                             "setups_n": setups_n, "ship": ship, "shipped": shipped, "scrap": scrap, "good": good})

    def _derive(self, ctx: BuildContext, state: WorldState) -> None:
        """Prices, standard costs, work orders, sales orders, invoices, quality events, labour hours."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        E = state.extras
        start = pd.Timestamp(cal.start)
        n, b = p["n_stations"], E["b"]
        F = len(E["mix"])
        fam, qty, good, scrap, shipped = E["fam"], E["qty"], E["good"], E["scrap"], E["shipped"]
        W = E["W"]
        # ---- price calibration: TTM revenue of good shipped units ~= target
        ship_m = np.where(shipped, np.searchsorted(E["knots"], np.where(shipped, E["ship"], 0), side="right") - 1, -1)
        last12 = ship_m >= len(E["months"]) - 12
        rel_units = float((good * E["rel_price"][fam])[last12].sum()) or 1.0
        base_price = p["revenue_ttm_cents"] / rel_units
        price_f = base_price * E["rel_price"]  # cents per unit
        rm_c = np.round(price_f * E["rm_f"]).astype(np.int64)
        conv = price_f * p["conv_share"]
        frac_up = (b + 1) / n
        cu = np.round(conv * frac_up * 0.6).astype(np.int64)  # conversion applied while in WIP
        cd = np.round(conv * (1 - frac_up) * 0.6).astype(np.int64)  # conversion applied at completion
        price_c = np.round(price_f).astype(np.int64)
        prod_price = np.round(price_c[:, None] * np.array([0.94, 0.98, 1.02, 1.06])[None, :]).astype(np.int64)
        products = pd.DataFrame([
            {"product_id": f"PRD-{f + 1}{k + 1:02d}", "part_number": f"{E['fam_names'][f][:3].upper()}-{1000 + 37 * f + 11 * k}",
             "family": E["fam_names"][f], "rm_cost_cents": int(rm_c[f]), "standard_cost_cents": int(rm_c[f] + cu[f] + cd[f]),
             "list_price_cents": int(prod_price[f, k]), "complexity": ["low", "medium", "high"][min(2, int(E["mult"][b, f] * 2) - 1 if E["mult"][b, f] >= 1 else 0)],
             "annual_volume_plan": 0} for f in range(F) for k in range(4)])
        E.update({"price_c": price_c, "prod_price": prod_price, "rm_c": rm_c, "cu": cu, "cd": cd})
        unit_price = prod_price[fam, E["prod_k"]]
        wip_cost = (rm_c[fam] + cu[fam]).astype(np.int64)
        std_cost = (rm_c[fam] + cu[fam] + cd[fam]).astype(np.int64)
        products["annual_volume_plan"] = [int(qty[(fam == f) & (E["prod_k"] == k)].sum() / 2.25) for f in range(F) for k in range(4)]
        # ---- timestamps
        def ts(h: np.ndarray) -> pd.Series:
            h2 = np.where(np.isfinite(h), h, 0.0)
            return pd.Series(start + pd.to_timedelta(np.round(h2 * 3600), unit="s")).where(np.isfinite(h))

        released = ts(E["rel"])
        ordered = released - pd.to_timedelta(g.uniform(0.2, 4.0, W), unit="D")
        # sales quotes buffer x the median flow time of orders shipped in the previous quarter (re-quoted with a lag)
        n_m = len(E["months"])
        flow_d = (E["ship"] - E["rel"]) / 24
        ship_mi = np.where(shipped, np.searchsorted(E["knots"], np.where(shipped, E["ship"], 0), side="right") - 1, -1)
        med = np.array([np.median(flow_d[shipped & (ship_mi >= m - 3) & (ship_mi < m)]) if (shipped & (ship_mi >= m - 3) & (ship_mi < m)).any() else np.nan
                        for m in range(n_m)])
        first = med[np.isfinite(med)][0] if np.isfinite(med).any() else 14.0
        med = np.where(np.isfinite(med), med, first)
        quoted_m = np.ceil(np.maximum(first, med) * p["quote_buffer"] + 2)
        quoted = quoted_m[np.clip(E["rel_m"], 0, n_m - 1)]
        E["quoted_lead_days"] = float(quoted_m[-1])
        E["quoted_by_month"] = quoted_m
        promised = (ordered + pd.to_timedelta(quoted, unit="D")).dt.normalize() + pd.Timedelta(hours=17)
        ship_ts = ts(np.where(shipped, E["ship"], np.nan))
        late_days = np.where(shipped, np.maximum(0.0, (ship_ts - promised).dt.total_seconds().fillna(0).to_numpy() / 86400), 0.0)
        value = good * unit_price
        weeks_late = np.ceil(late_days / 7.0)
        wo_ids = np.array([f"WO{i:06d}" for i in range(1, W + 1)])
        so_ids = np.array([f"SO{i:06d}" for i in range(1, W + 1)])
        cust = state.frames["customers"]
        cw = 1 + g.pareto(1.3, len(cust))
        cust_idx = g.choice(len(cust), size=W, p=cw / cw.sum())
        clause = (cust["segment"].to_numpy() == "key_account")[cust_idx]  # only key accounts have late-delivery penalty clauses
        penalty = np.where(clause & (late_days > 0), np.minimum(np.round(value * p["late_penalty_pct_week"] * weeks_late), np.round(value * 0.10)), 0).astype(np.int64)
        through_b = np.isfinite(E["en"][:, b]) & (E["en"][:, b] <= E["T_end"])
        status = np.where(shipped, "closed", np.where(through_b, "in_process_downstream", "queued_or_upstream"))
        wo = pd.DataFrame({
            "work_order_id": wo_ids, "product_id": products["product_id"].to_numpy()[fam * 4 + E["prod_k"]],
            "family": np.array(E["fam_names"])[fam], "quantity_planned": qty,
            "quantity_completed": np.where(shipped, good, 0), "quantity_scrapped": scrap,
            "released_at": released, "due_at": promised, "closed_at": ship_ts, "status": status,
            "customer_order_ref": so_ids, "priority": np.where(late_days > 14, "expedite", np.where(g.random(W) < 0.15, "high", "normal")),
            "created_at": released - pd.to_timedelta(g.uniform(0.5, 12, W), unit="h"),
        })
        wo["updated_at"] = wo["closed_at"].fillna(wo["released_at"])
        terms = p["terms_days"]
        inv_date = ship_ts.dt.normalize()
        pay_at = inv_date + pd.to_timedelta(terms + np.maximum(-5, g.normal(4, 8, W)), unit="D") + pd.to_timedelta(g.integers(9, 17, W), unit="h")
        end_ts = start + pd.Timedelta(hours=float(E["T_end"])) - pd.Timedelta(seconds=1)
        paid = shipped & (pay_at <= end_ts).to_numpy()
        so = pd.DataFrame({
            "sales_order_id": so_ids, "customer_id": cust["customer_id"].to_numpy()[cust_idx],
            "customer": cust["name"].to_numpy()[cust_idx], "product_id": wo["product_id"], "work_order_id": wo_ids,
            "quantity": qty, "unit_price_cents": unit_price, "ordered_at": ordered, "promised_at": promised,
            "shipped_at": ship_ts, "shipped_qty": np.where(shipped, good, 0), "invoiced_at": inv_date,
            "invoice_amount_cents": np.where(shipped, value, 0).astype(np.int64), "late_days": np.round(late_days, 2),
            "penalty_cents": penalty, "paid_at": pay_at.where(paid),
        })
        # quality events (one per scrapping work order; detected at final inspection = last station)
        qe_mask = scrap > 0
        origin_up = g.random(W) < p["defect_origin_upstream_share"]
        wc_ids = state.frames["work_centers"]["work_center_id"].to_numpy()
        other_wc = np.where(g.random(W) < 0.6, b, g.integers(b, n, W))
        origin_wc = np.where(origin_up, wc_ids[E["origin"]], wc_ids[other_wc])
        code = np.where(origin_up, np.array(DEFECTS["origin"])[g.integers(0, 3, W)], np.array(DEFECTS["other"])[g.integers(0, 3, W)])
        qe = pd.DataFrame({
            "work_order_id": wo_ids[qe_mask], "work_center_id": wc_ids[n - 1], "origin_work_center_id": origin_wc[qe_mask],
            "detected_at": ship_ts[qe_mask].to_numpy() - pd.to_timedelta(g.uniform(0.5, 1.0, int(qe_mask.sum())), unit="h").to_numpy(),
            "defect_code": code[qe_mask], "quantity": scrap[qe_mask], "disposition": "scrap",
            "cost_estimate_cents": (scrap * std_cost)[qe_mask],
        }).reset_index(drop=True)
        qe.insert(0, "event_id", [f"QE{i:06d}" for i in range(1, len(qe) + 1)])
        # production log (completed operations only)
        rows = []
        for i in range(n):
            ok = np.isfinite(E["en"][:, i]) & (E["en"][:, i] <= E["T_end"])
            if not ok.any():
                continue
            rows.append(pd.DataFrame({
                "_wo": np.flatnonzero(ok), "work_order_id": wo_ids[ok], "work_center_id": wc_ids[i], "op_seq": 10 * (i + 1),
                "queue_in_at": ts(E["q_in"][ok, i]).to_numpy(), "start_at": ts(E["st"][ok, i]).to_numpy(), "end_at": ts(E["en"][ok, i]).to_numpy(),
                "setups": E["setups_n"][ok, i], "setup_minutes_actual": np.round(E["setup_min"][ok, i], 2),
                "run_minutes_actual": np.round(E["run_act"][ok, i], 2), "qty_processed": qty[ok],
                "good_qty": qty[ok], "scrap_qty": np.where(i == n - 1, scrap[ok], 0),
            }))
        log = pd.concat(rows, ignore_index=True)
        # machine time: end - start == setup + run exactly (seconds)
        log["end_at"] = log["start_at"] + pd.to_timedelta(np.round((log["setup_minutes_actual"] + log["run_minutes_actual"]) * 60), unit="s")
        log["good_qty"] = log["good_qty"] - log["scrap_qty"]
        log = log.sort_values(["start_at", "op_seq", "work_order_id"], kind="stable").reset_index(drop=True)
        log.insert(0, "log_id", np.arange(1, len(log) + 1, dtype=np.int64))
        log["shift"] = np.where(log["start_at"].dt.hour.between(6, 13), "A", np.where(log["start_at"].dt.hour.between(14, 21), "B", "C"))
        log["operator_id"] = np.array([f"OP{k:03d}" for k in range(1, 41)])[g.integers(0, 40, len(log))]
        # labour hours by work center and month (regular + overtime at b and finishing in OT months)
        fl = E["fluid"]
        wc = state.frames["work_centers"].copy()
        # conversion budget at plan volume: 40% direct labour (crew x rate x hours), 60% overhead (applied via cu/cd)
        conv_plan = float(np.sum(price_c * E["mix"]) * p["conv_share"]) * p["c_b"] * H_MONTH * (UTIL_TARGET - p["setup_share_base"])
        w_st = 1.0 / wc["capacity_units_per_hour"].to_numpy()
        share = 0.4 * conv_plan * w_st / w_st.sum()
        crew = np.maximum(1, np.round(share / (wc["cost_per_hour_cents"].to_numpy() * H_MONTH))).astype(np.int64)
        wc["crew_size"] = crew
        wc["cost_per_hour_cents"] = np.maximum(1500, np.round(share / (crew * H_MONTH))).astype(np.int64)
        early_ship = shipped & (ship_mi >= 3) & (ship_mi < 9)
        E["revenue_plan_month_cents"] = float(value[early_ship].sum()) / 6
        state.frames["work_centers"] = wc
        E["direct_month_cents"] = int((crew * wc["cost_per_hour_cents"].to_numpy() * H_MONTH).sum())
        E["overhead_month_cents"] = cents(0.6 * conv_plan)
        lh = []
        for m, ms in enumerate(E["months"]):
            for i in range(n):
                ot = fl["OT"][m] if i == b else (0.6 * fl["OT"][m] if i == n - 1 else 0.0)
                lh.append((f"{ms:%Y-%m}", wc_ids[i], float(H_MONTH), round(float(ot), 2), int(wc["crew_size"].iat[i]),
                           int(wc["cost_per_hour_cents"].iat[i])))
        labor = pd.DataFrame(lh, columns=["month", "work_center_id", "regular_hours", "overtime_hours", "crew_size", "rate_cents_per_hour"])
        labor["overtime_premium_cents"] = np.round(labor["overtime_hours"] * labor["crew_size"] * labor["rate_cents_per_hour"] * p["ot_premium"]).astype(np.int64)
        state.frames.update({"products": products, "work_orders": wo, "sales_orders": so, "quality_events": qe, "production_log": log,
                             "labor_hours": labor})
        E.update({"wip_cost": wip_cost, "std_cost": std_cost, "unit_price": unit_price, "value": value, "penalty": penalty,
                  "late_days": late_days, "pay_at": pay_at, "paid": paid, "end_ts": end_ts, "released_ts": released, "ship_ts": ship_ts})

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Simulate the line, derive tables, post the books month by month."""
        state.extras["months"] = state.calendar.month_starts()
        self._simulate_line(ctx, state)
        self._derive(ctx, state)
        # two passes: size SG&A so that P1-P2 operating margin ~= op_margin given the realised gross margin
        E = state.extras
        E["opex_month_cents"] = 0
        self._books(ctx, state)
        P = state.periods
        gp = sum(-sum(v for c, v in state.ledger.activity(x).items() if c[0] in "45") for x in P[:2])
        rev = sum(-sum(state.ledger.activity(x)[c] for c in ("4000", "4010", "4020", "4100", "4110")) for x in P[:2])
        E["opex_month_cents"] = max(cents(rev / 6 * 0.08), cents((gp - state.params["op_margin"] * rev) / 6))
        self._books(ctx, state)

    def _books(self, ctx: BuildContext, state: WorldState) -> None:
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("books").generator
        E = state.extras
        L = self.fresh_ledger(state)
        state.ledger = L
        gran = GL_BATCH[p["difficulty"]]
        months = E["months"]
        n_m = len(months)
        R = p["revenue_ttm_cents"]
        fam = E["fam"]
        qty = E["qty"]
        rm_unit = E["rm_c"][fam]
        cu_unit = E["cu"][fam]
        cd_unit = E["cd"][fam]
        # ---- opening balances
        open_wo = E["opening_wo"]
        wip0 = int((E["wip_cost"] * qty)[open_wo].sum())
        rel_month = np.where(open_wo, -1, E["rel_m"])
        rm_need = np.bincount(rel_month[~open_wo], weights=(rm_unit * qty)[~open_wo], minlength=n_m + 1)
        rm_need[n_m] = rm_need[n_m - 1]
        rm0 = int(round(rm_need[0] * 0.6))
        ar0 = cents(R / 12 * p["terms_days"] / 30.4)
        ap0 = cents(rm_need[0] * p["dpo_days"] / 30.4)
        ppe = cents(R * p["capex_pct"] * 8 * 0.55)
        accdep = cents(ppe * 0.45)
        debt = cents(R * 0.12 / 100_000) * 100_000
        cash0 = cents(R * 0.07)
        net = cash0 + ar0 + rm0 + wip0 + ppe - accdep - ap0 - debt
        cs = cents(0.6 * net) if net > 0 else max(cents(0.25 * (cash0 + ar0 + rm0 + wip0 + ppe - accdep)), 1)
        L.open_books(cal.opening_date, {"1000": cash0, "1100": ar0, "1200": rm0, "1210": wip0, "1500": ppe, "1510": -accdep,
                                        "2000": -ap0, "2610": -debt, "3000": -cs, "3100": -(net - cs)})
        events: list[tuple[dt.date, int, Any]] = []

        def add(d: pd.Timestamp | dt.date, prio: int, fn: Any) -> None:
            events.append((d.date() if isinstance(d, pd.Timestamp) else d, prio, fn))

        # opening AR collected / opening AP paid during the warm-up quarter
        for k, a in enumerate(split_cents(ar0, [0.45, 0.35, 0.20])):
            add(cal.start + dt.timedelta(days=10 + 15 * k), 4, lambda d, a=a: L.collect_cash(d, a, memo="Collection of opening receivables"))
        add(cal.start + dt.timedelta(days=int(p["dpo_days"] * 0.6)), 5, lambda d: L.pay_ap(d, ap0, memo="Opening payables paid"))
        # ---- RM receipts (weekly) and vendor payments
        rec_rows = []
        on_hand = rm0
        for m in range(n_m):
            need = rm_need[m] + 0.6 * rm_need[m + 1] - on_hand
            amt = max(0, int(round(need)))
            parts = split_cents(amt, [1, 1, 1, 1]) if amt > 0 else []
            for k, a in enumerate(parts):
                d = pd.Timestamp(months[m]) + pd.Timedelta(days=7 * k + int(g.integers(0, 3)))
                rec_rows.append((d, a))
            on_hand = on_hand + amt - rm_need[m]
        rec = pd.DataFrame(rec_rows, columns=["received_at", "amount_cents"])
        rec = rec[rec["amount_cents"] > 0].reset_index(drop=True)
        rec["paid_at"] = rec["received_at"] + pd.to_timedelta(np.round(p["dpo_days"] + g.normal(0, 3, len(rec))), unit="D")
        end_d = cal.end
        rec = rec[rec["received_at"].dt.date <= end_d].reset_index(drop=True)
        rec.insert(0, "receipt_id", [f"RCV{i:05d}" for i in range(1, len(rec) + 1)])
        rec["supplier"] = np.array(["Allied Metals", "Precision Stock Co", "Northern Alloys", "Summit Polymers"])[g.integers(0, 4, len(rec))]
        E["rm_receipts"] = rec
        for _, r in rec.iterrows():
            add(r.received_at, 2, lambda d, a=int(r.amount_cents): L.purchase_inventory(d, a, inventory_account="1200", memo="Raw material receipts",
                                                                                     dim={"batch": "rm_receipt"}))
            if r.paid_at.date() <= end_d:
                add(r.paid_at, 5, lambda d, a=int(r.amount_cents): L.pay_ap(d, a, memo="Supplier payments (raw material)"))
        # ---- releases into WIP (batched)
        rel_ts = E["released_ts"]
        nr = ~open_wo
        rel_df = pd.DataFrame({"b": batch_end_dates(rel_ts[nr], gran), "rm": (rm_unit * qty)[nr], "cu": (cu_unit * qty)[nr]})
        for bdt, r in rel_df.groupby("b", sort=True).sum().iterrows():
            lines = [("1210", int(r.rm + r.cu), 0, {"batch": f"release:{bdt.date()}"}), ("1200", 0, int(r.rm), {}), ("5020", 0, int(r.cu), {})]
            add(bdt, 3, lambda d, ln=lines: L.adjust(d, ln, "Work orders released to the floor (RM issued, conversion applied)", "production"))
        # ---- completions, shipments and invoices
        sh = E["shipped"]
        ship_ts = E["ship_ts"]
        comp = pd.DataFrame({"b": batch_end_dates(ship_ts[sh], gran), "stdc": (E["std_cost"] * E["good"])[sh], "scrapc": (E["wip_cost"] * E["scrap"])[sh],
                             "wip": (E["wip_cost"] * qty)[sh], "cd": (cd_unit * E["good"])[sh], "value": E["value"][sh], "pen": E["penalty"][sh],
                             "late": (E["late_days"] > 0)[sh].astype(int), "n": 1})
        comp["freight"] = np.round(comp["value"] * p["freight_pct"] * g.lognormal(0, 0.1, len(comp))).astype(np.int64)
        comp["expedite"] = np.where(comp["late"] > 0, np.round(comp["value"] * g.uniform(0.004, 0.012, len(comp))), 0).astype(np.int64)
        for bdt, r in comp.groupby("b", sort=True).sum().iterrows():
            dim = {"batch": f"shipments:{bdt.date()}", "rows": str(int(r.n))}
            add(bdt, 3, lambda d, r=r, dim=dim: L.adjust(d, [("5000", int(r.stdc), 0, dim), ("5040", int(r.scrapc), 0, dim), ("1210", 0, int(r.wip), dim),
                                                            ("5020", 0, int(r.cd), dim)], "Completed work orders shipped (COGS) and end-of-line scrap", "fulfillment"))
            add(bdt, 3, lambda d, a=int(r.value), dim=dim: L.sell_on_credit(d, a, memo="Shipments invoiced", dim=dim))
            if r.pen:
                add(bdt, 3, lambda d, a=int(r.pen), dim=dim: L.adjust(d, [("4110", a, 0, dim), ("1100", 0, a, dim)], "Late-delivery penalty credit memos", "penalties"))
            add(bdt, 3, lambda d, a=int(r.freight), dim=dim: L.adjust(d, [("5030", a, 0, dim), ("2100", 0, a, dim)], "Outbound freight accrued", "fulfillment"))
            if r.expedite:
                add(bdt, 3, lambda d, a=int(r.expedite), dim=dim: L.adjust(d, [("5030", a, 0, dim), ("2100", 0, a, dim)], "Expedited freight on late orders", "expedite"))
        E["shipment_batches"] = comp
        paid = E["paid"]
        pay = pd.DataFrame({"b": batch_end_dates(E["pay_at"][paid], gran), "amt": (E["value"] - E["penalty"])[paid], "n": 1})
        for bdt, r in pay.groupby("b", sort=True).sum().iterrows():
            add(bdt, 4, lambda d, a=int(r.amt), n_=int(r.n): L.collect_cash(d, a, memo="Customer payments", dim={"rows": str(n_)}))
        # ---- monthly close (crew sizes / overhead come from the cost structure, see _derive)
        labor = state.frames["labor_hours"]
        direct0 = int(E["direct_month_cents"])
        overhead0 = int(E["overhead_month_cents"])
        ot_month = labor.groupby("month", sort=False)["overtime_premium_cents"].sum()
        opex0 = int(E["opex_month_cents"])
        freight_m = comp.assign(mo=pd.to_datetime(comp["b"]).dt.strftime("%Y-%m")).groupby("mo")[["freight", "expedite"]].sum().sum(axis=1)
        nz = self.period_noise(ctx, state)  # review C2: seasonal + idiosyncratic quarter noise
        prev = {"payroll": 0, "accr": 0}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            if m > 0:
                add(ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll"))
                add(ms + dt.timedelta(days=9), 1, lambda d, a=prev["accr"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued expenses and carrier invoices paid", source="opex"))
            if ms.month in (1, 4, 7, 10) and m > 0:
                add(ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"]))
            ot = int(ot_month.get(f"{ms:%Y-%m}", 0))
            infl = (1 + p["wage_inflation_month"]) ** m
            direct, overhead, opex_m = cents(direct0 * infl), cents(overhead0 * infl), cents(opex0 * infl * nz.ox(ms))
            sga_pay = cents(opex_m * 0.55)
            other = opex_m - sga_pay
            add(me, 7, lambda d, a=direct, s=sga_pay: L.payroll(d, {"5010": a, **dict(zip(("6000", "6200"), split_cents(s, [0.4, 0.6]), strict=True))},
                                                               memo="Payroll: direct labour and SG&A"))
            if ot:
                add(me, 7, lambda d, a=ot: L.payroll(d, {"5010": a}, memo="Overtime premium (quarter-end expediting)", source="overtime"))
            add(me, 7, lambda d, a=overhead, o=other: L.accrue_opex(d, {"5020": a, **dict(zip(("6000", "6200", "6400"), split_cents(o, [0.3, 0.4, 0.3]), strict=True))},
                                                                   memo="Plant overhead and operating expenses"))
            add(ms + dt.timedelta(days=14), 6, lambda d: L.capex(d, cents(R * p["capex_pct"] / 12), memo="Maintenance capex"))
            add(me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 96)))
            add(me, 9, lambda d: L.accrue_interest(d, cents((-L.closing_balances()["2610"] - L.closing_balances()["2500"]) * p["interest_rate"] / 12)))
            if ms.month % 3 == 0:
                add(me, 10, lambda d: L.pay_interest(d, -L.closing_balances()["2150"]))

                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))

                add(me, 11, tax)
            prev = {"payroll": direct + sga_pay + ot, "accr": overhead + other + int(freight_m.get(f"{ms:%Y-%m}", 0))}
        events.sort(key=lambda e: (e[0], e[1]))
        floor = cents(R / 12 * 0.12)
        revolver = 0
        k = 0
        for ms in months:
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            while k < len(events) and events[k][0] <= me:
                if events[k][0] <= end_d:
                    events[k][2](events[k][0])
                k += 1
            cash = L.closing_balances()["1000"]
            if cash < floor:
                draw = int(math.ceil((1.5 * floor - cash) / 1_000_000) * 1_000_000)
                L.borrow(me, draw, memo="Revolver draw")
                revolver += draw
            elif cash > 3 * floor and revolver > 0:
                rep = min(revolver, int(math.floor((cash - 2 * floor) / 1_000_000) * 1_000_000))
                if rep > 0:
                    L.repay(me, rep, memo="Revolver repayment")
                    revolver -= rep
        E["revolver_end"] = revolver

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """MES tables + 2/4/6 dirt rules (never on GL-tied money columns)."""
        p = state.params
        E = state.extras
        wc = state.frames["work_centers"]
        products = state.frames["products"]
        n = p["n_stations"]
        fam_idx = {f: i for i, f in enumerate(E["fam_names"])}
        routings = []
        for _, pr in products.iterrows():
            f = fam_idx[pr.family]
            for i in range(n):
                routings.append({"routing_id": f"RT-{pr.product_id}-{10 * (i + 1)}", "product_id": pr.product_id, "op_seq": 10 * (i + 1),
                                 "work_center_id": wc["work_center_id"].iat[i], "setup_minutes": round(float(E["setup"][i]), 1),
                                 "run_minutes_per_unit": round(float(60.0 / E["cap"][i] * E["mult"][i, f]), 4)})
        routings_df = pd.DataFrame(routings)
        log = state.frames["production_log"].drop(columns=["_wo"])
        rec = E["rm_receipts"].copy()
        rec["paid_at"] = rec["paid_at"].where(rec["paid_at"].dt.date <= state.calendar.end)
        dirt_plan = [
            ("sales_orders", "nulls:customer:0.06"), ("quality_events", "case_mixed:defect_code"), ("work_centers", "whitespace:name"),
            ("work_orders", "late_arriving:updated_at"), ("production_log", "dup_keys:0.01"), ("sales_orders", "tz_mixed:ordered_at"),
        ]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[: {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]]:
            dirt.setdefault(t, []).append(rule)
        money = {c: "BIGINT" for c in ("planned_capex_cents", "cost_per_hour_cents", "rm_cost_cents", "standard_cost_cents", "list_price_cents",
                                        "unit_price_cents", "invoice_amount_cents", "penalty_cents", "cost_estimate_cents", "amount_cents",
                                        "overtime_premium_cents", "rate_cents_per_hour")}
        builder.add_frame("work_centers", wc, primary_key=["work_center_id"], foreign_keys=[], column_types=money, dirt=dirt.get("work_centers", ()),
                          descriptions={"capacity_units_per_hour": "rated (nameplate) units per hour at standard mix",
                                        "hours_per_month": "regular available hours per month (two shifts)",
                                        "planned_capex_cents": "capex requested for this work center (board pack)",
                                        "efficiency_target": "local efficiency KPI target (run time / available time)"},
                          description="Work centers of the serial line, in routing order (op_seq).")
        builder.add_frame("products", products, primary_key=["product_id"], foreign_keys=[], column_types=money,
                          descriptions={"rm_cost_cents": "raw material cost per unit (truly variable cost)",
                                        "standard_cost_cents": "absorption standard cost per unit (RM + applied conversion)"})
        builder.add_frame("routings", routings_df, primary_key=["routing_id"],
                          foreign_keys=[("product_id", "products", "product_id"), ("work_center_id", "work_centers", "work_center_id")],
                          descriptions={"setup_minutes": "minutes per setup (changeover)", "run_minutes_per_unit": "standard run minutes per unit"})
        builder.add_frame("work_orders", state.frames["work_orders"], primary_key=["work_order_id"], foreign_keys=[("product_id", "products", "product_id")],
                          column_types={"quantity_planned": "INTEGER", "quantity_completed": "INTEGER", "quantity_scrapped": "INTEGER"},
                          dirt=dirt.get("work_orders", ()),
                          descriptions={"status": "closed | in_process_downstream | queued_or_upstream (not yet through the constraint)",
                                        "released_at": "released to the floor (push: on order receipt)"})
        builder.add_frame("production_log", log, primary_key=["log_id"],
                          foreign_keys=[("work_order_id", "work_orders", "work_order_id"), ("work_center_id", "work_centers", "work_center_id")],
                          column_types={"log_id": "BIGINT", "op_seq": "INTEGER", "setups": "INTEGER", "qty_processed": "INTEGER", "good_qty": "INTEGER",
                                        "scrap_qty": "INTEGER"},
                          dirt=dirt.get("production_log", ()),
                          descriptions={"queue_in_at": "arrived in the queue of this work center", "start_at": "operation start (machine time)",
                                        "end_at": "operation end; end_at - start_at = setup + run minutes",
                                        "setups": "number of setups/changeovers performed for this operation (transfer batches)",
                                        "ingest_id": ">1 marks a re-ingested duplicate of the same log_id"},
                          description="One row per completed operation (work order x work center).")
        builder.add_frame("quality_events", state.frames["quality_events"], primary_key=["event_id"],
                          foreign_keys=[("work_order_id", "work_orders", "work_order_id"), ("work_center_id", "work_centers", "work_center_id")],
                          column_types={**money, "quantity": "INTEGER"}, dirt=dirt.get("quality_events", ()),
                          descriptions={"work_center_id": "where the defect was DETECTED (inspection station)",
                                        "origin_work_center_id": "where the defect was CAUSED (root-cause analysis)"})
        builder.add_frame("sales_orders", state.frames["sales_orders"], primary_key=["sales_order_id"],
                          foreign_keys=[("product_id", "products", "product_id"), ("work_order_id", "work_orders", "work_order_id")],
                          column_types={**money, "quantity": "INTEGER", "shipped_qty": "INTEGER"}, dirt=dirt.get("sales_orders", ()),
                          descriptions={"invoice_amount_cents": "invoiced at shipment (good units x unit price)",
                                        "penalty_cents": "late-delivery credit memo", "paid_at": "NULL while the invoice is open"},
                          description="Customer orders (make-to-order, one work order each) and their invoices (AR sub-ledger).")
        builder.add_frame("labor_hours", state.frames["labor_hours"], primary_key=["month", "work_center_id"],
                          foreign_keys=[("work_center_id", "work_centers", "work_center_id")], column_types=money,
                          descriptions={"overtime_hours": "authorised overtime hours (machine + crew)", "overtime_premium_cents": "posted to GL 5010 (source overtime)"})
        builder.add_frame("purchase_orders", rec.rename(columns={"receipt_id": "po_id"}), primary_key=["po_id"], foreign_keys=[], column_types=money,
                          descriptions={"paid_at": "NULL while unpaid"}, description="Raw-material receipts (AP sub-ledger).")

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, WIP, AP and revenue ties - all hold."""
        cal, E = state.calendar, state.extras
        so = state.frames["sales_orders"]
        rec = E["rm_receipts"]
        ar, wip, ap, rev = {}, {}, {}, {}
        rel = E["released_ts"]
        ship = E["ship_ts"]
        wipv = E["wip_cost"] * E["qty"]
        for period in state.periods:
            s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            s_ts = pd.Timestamp(s)
            inv = so["shipped_at"].notna() & (so["shipped_at"] <= e_ts)
            open_ = so["paid_at"].isna() | (so["paid_at"] > e_ts)
            ar[period] = int((so["invoice_amount_cents"] - so["penalty_cents"])[inv & open_].sum())
            rev[period] = int(so.loc[inv & (so["shipped_at"] >= s_ts), "invoice_amount_cents"].sum())
            open_wo = ((rel <= e_ts) | E["opening_wo"]) & ~(ship.notna() & (ship <= e_ts))
            wip[period] = int(wipv[open_wo.to_numpy()].sum())
            ap[period] = int(rec.loc[(rec["received_at"] <= e_ts) & (rec["paid_at"] > e_ts), "amount_cents"].sum())
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Open invoices net of penalty credit memos == GL 1100"),
            SubledgerTie("wip_open_work_orders", wip, ["1210"], description="Open work orders x standard WIP cost (RM + applied upstream conversion) == GL 1210"),
            SubledgerTie("ap_open_receipts", ap, ["2000"], description="Received, unpaid raw-material receipts == GL 2000"),
            SubledgerTie("revenue_invoices", rev, ["4000"], basis="activity", description="Invoices issued in period == GL 4000 activity"),
        ]

    # ------------------------------------------------------------------ facts

    def _station_stats(self, state: WorldState, periods: list[str]) -> pd.DataFrame:
        """Per work center over ``periods``: available, busy (setup+run), run-only minutes, utilizations, queue/flow times."""
        cal = state.calendar
        log = state.frames["production_log"]
        labor = state.frames["labor_hours"]
        s0 = pd.Timestamp(cal.bounds(periods[0])[0])
        e1 = pd.Timestamp(cal.bounds(periods[-1])[1]) + pd.Timedelta(days=1)
        sel = log[(log["start_at"] >= s0) & (log["start_at"] < e1)]
        months = {f"{m:%Y-%m}" for m in pd.date_range(s0, e1 - pd.Timedelta(days=1), freq="MS")}
        lab = labor[labor["month"].isin(months)].groupby("work_center_id")[["regular_hours", "overtime_hours"]].sum()
        g = sel.assign(busy=sel["setup_minutes_actual"] + sel["run_minutes_actual"],
                       qh=(sel["start_at"] - sel["queue_in_at"]).dt.total_seconds() / 3600,
                       fh=(sel["end_at"] - sel["queue_in_at"]).dt.total_seconds() / 3600).groupby("work_center_id")
        wc = state.frames["work_centers"].set_index("work_center_id")
        out = pd.DataFrame({"op_seq": wc["op_seq"], "name": wc["name"], "capacity": wc["capacity_units_per_hour"]})
        out["avail_min"] = (lab["regular_hours"] + lab["overtime_hours"]).reindex(out.index).fillna(0) * 60
        out["busy_min"] = g["busy"].sum().reindex(out.index).fillna(0)
        out["run_min"] = g["run_minutes_actual"].sum().reindex(out.index).fillna(0)
        out["setup_min"] = g["setup_minutes_actual"].sum().reindex(out.index).fillna(0)
        out["setups"] = g["setups"].sum().reindex(out.index).fillna(0)
        out["units"] = g["qty_processed"].sum().reindex(out.index).fillna(0)
        out["ops"] = g.size().reindex(out.index).fillna(0)
        out["avg_queue_h"] = g["qh"].mean().reindex(out.index).fillna(0)
        out["avg_flow_h"] = g["fh"].mean().reindex(out.index).fillna(0)
        out["rho"] = out["busy_min"] / out["avail_min"]
        out["rho_naive"] = out["run_min"] / out["avail_min"]
        out["window_h"] = (e1 - s0).total_seconds() / 3600
        return out

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        p, cal, E = state.params, state.calendar, state.extras
        P = state.periods
        P8 = P[-1]
        wc = state.frames["work_centers"]
        ids = wc["work_center_id"].tolist()
        b, b2, n = E["b"], E["b2"], p["n_stations"]
        st = self._station_stats(state, P[4:])
        st8 = self._station_stats(state, [P8])
        true_b = str(st["rho"].idxmax())
        naive_b = str(st["rho_naive"].idxmax())
        s8, e8 = cal.bounds(P8)
        s8t, e8t = pd.Timestamp(s8), pd.Timestamp(e8) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        so = state.frames["sales_orders"]
        wo = state.frames["work_orders"]
        shipped8 = so[(so["shipped_at"] >= s8t) & (so["shipped_at"] <= e8t)]
        thr_month = float(shipped8["shipped_qty"].sum()) / 3
        log = state.frames["production_log"]
        lb = log[log["work_center_id"] == ids[b]]
        through_b = set(lb.loc[lb["end_at"] <= e8t, "work_order_id"])
        released = (wo["released_at"] <= e8t) | E["opening_wo"]
        before_b = released & ~wo["work_order_id"].isin(through_b)
        wip_before_b = float(wo.loc[before_b, "quantity_planned"].sum())
        open_all = released & ~(wo["closed_at"].notna() & (wo["closed_at"] <= e8t))
        wip_total = float(wo.loc[open_all, "quantity_planned"].sum())
        done8 = wo[(wo["closed_at"] >= s8t) & (wo["closed_at"] <= e8t)]
        thr_day = float(done8["quantity_planned"].sum()) / cal.days(P8)
        cycle_days = wip_total / thr_day if thr_day else 0.0
        # time-average WIP over P8 (daily snapshots) and average flow time of WOs closed in P8 (Little's Law check)
        days = pd.date_range(s8t, e8t, freq="D") + pd.Timedelta(hours=12)
        rel_t = np.where(E["opening_wo"], -np.inf, wo["released_at"].astype("int64").to_numpy())
        close_t = wo["closed_at"].astype("int64").to_numpy()
        close_t = np.where(wo["closed_at"].isna().to_numpy(), np.iinfo(np.int64).max, close_t)
        snap = np.array([float(wo["quantity_planned"].to_numpy()[(rel_t <= d.value) & (close_t > d.value)].sum()) for d in days])
        flow_days = float((((done8["closed_at"] - done8["released_at"]).dt.total_seconds() / 86400) * done8["quantity_planned"]).sum()
                          / max(1, done8["quantity_planned"].sum()))
        qe = state.frames["quality_events"]
        qe8 = qe[(qe["detected_at"] >= s8t) & (qe["detected_at"] <= e8t)].merge(wo[["work_order_id", "product_id"]], on="work_order_id")
        fam_of = state.frames["products"].set_index("product_id")["family"]
        fam_idx = {f: i for i, f in enumerate(E["fam_names"])}
        run_b_f = np.array([60.0 / E["cap"][b] * E["mult"][b, f] for f in range(len(E["fam_names"]))])
        scrap_min = float((qe8["quantity"] * run_b_f[qe8["product_id"].map(fam_of).map(fam_idx).to_numpy()]).sum()) / 3 if len(qe8) else 0.0
        setup_min8 = float(st8.loc[ids[b], "setup_min"]) / 3
        setup_share8 = float(st8.loc[ids[b], "setup_min"] / st8.loc[ids[b], "avail_min"])
        lb8 = lb[(lb["end_at"] >= s8t) & (lb["end_at"] <= e8t)]
        units_b8 = float(lb8["qty_processed"].sum())
        scrap_share = float(qe8["quantity"].sum()) / units_b8 if units_b8 else 0.0
        ceiling = p["hours_per_month"] * float(wc["capacity_units_per_hour"].iat[b]) * (1 - setup_share8) * (1 - scrap_share)
        prod = state.frames["products"].groupby("family")[["list_price_cents", "rm_cost_cents"]].mean()
        t_per_min = {f: float((prod.loc[f, "list_price_cents"] - prod.loc[f, "rm_cost_cents"]) / run_b_f[fam_idx[f]]) for f in E["fam_names"]}
        ot8 = state.ledger.sum_lines(P8, ["5010"], "debit", "overtime")
        mig = float(wc["capacity_units_per_hour"].iat[b2] / wc["capacity_units_per_hour"].iat[b] - 1)
        late8 = float((shipped8["late_days"] > 0).mean()) if len(shipped8) else 0.0
        pen8 = state.ledger.sum_lines(P8, ["4110"], "debit")
        origin_share = float((qe["origin_work_center_id"] != qe["work_center_id"]).mean()) if len(qe) else 0.0
        up_share = float(qe["origin_work_center_id"].isin(ids[:b]).mean()) if len(qe) else 0.0
        f = {
            "st": st, "st8": st8, "true_b": true_b, "naive_b": naive_b, "true_b_seq": int(st.loc[true_b, "op_seq"]),
            "naive_b_seq": int(st.loc[naive_b, "op_seq"]), "thr_month": thr_month, "wip_before_b": wip_before_b, "wip_total": wip_total,
            "cycle_days": cycle_days, "thr_day": thr_day, "avg_wip_p8": float(snap.mean()), "flow_days_p8": flow_days,
            "scrap_min_month": scrap_min, "setup_min_month": setup_min8, "setup_share8": setup_share8, "scrap_share": scrap_share,
            "ceiling": ceiling, "t_per_min": t_per_min, "ot8": int(ot8), "migration": mig, "late8": late8, "pen8": int(pen8),
            "origin_upstream_share": up_share, "detect_ne_origin_share": origin_share, "units_b8": units_b8,
            "capex_wc": ids[n - 1], "b_id": ids[b], "b2_id": ids[b2],
        }
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Stations (P5-P8 trailing year), queueing at the constraint in P8, throughput economics, KPIs by period."""
        p, cal, E = state.params, state.calendar, state.extras
        f = self._facts(state)
        st = f["st"]
        stations = [Station(name=f"{r.op_seq}:{r.name}", capacity_per_hour=float(r.capacity), utilization=float(r.rho),
                            wip=float(r.avg_flow_h * r.units / r.window_h) if r.window_h else 0.0, cycle_time=float(r.avg_flow_h))
                    for r in st.sort_values("op_seq").itertuples()]
        P8 = state.periods[-1]
        st8 = f["st8"]
        rb = st8.loc[f["b_id"]]
        hours8 = float(rb.window_h)
        lam = float(rb.units) / hours8  # units per calendar hour arriving/processed at b
        W = float(rb.avg_flow_h)
        Wq = float(rb.avg_queue_h)
        mu = float(p["c_b"]) * (1 - f["setup_share8"]) * float(rb.avail_min) / 60 / hours8
        ll = littles_law(lam=lam, W=W)
        theo = mm1(lam, mu)
        # review P2 #23: unit-bearing keys (L / Lq in units of WIP at the constraint, rates per day, times in days; L = lambda x W)
        queueing = {"lambda_units_per_day": lam * 24, "mu_units_per_day": mu * 24, "rho": lam / mu if mu else 0.0,
                    "L_wip_units": ll["L"], "W_days": W / 24, "Lq_wip_units": lam * Wq, "Wq_days": Wq / 24,
                    "kingman_wq_theoretical_days": kingman_wq(lam, mu, p["cv_arrival"], p["cv_service"]) / 24, "mm1_unstable": theo["unstable"],
                    "throughput_units_month": f["thr_month"], "wip_units_total_p8": f["wip_total"], "cycle_time_days_p8": f["cycle_days"]}
        prices = state.frames["products"].groupby("family")[["list_price_cents", "rm_cost_cents", "standard_cost_cents"]].mean()
        inc = {i.period: i for i in state.statements.income}  # type: ignore[union-attr]
        oe = float(sum(state.ledger.activity(P8)[c] for c in ("5010", "5020", "5030", "6000", "6200", "6300", "6400")))
        unit = {"throughput_per_unit_cents": float((prices["list_price_cents"] - prices["rm_cost_cents"]).mean()),
                "operating_expense_quarter_cents": oe, "gross_margin": state.statements.ratios[P8]["gross_margin"],  # type: ignore[union-attr]
                "arpa": float(state.frames["sales_orders"]["invoice_amount_cents"].replace(0, np.nan).mean()), "cac": 0.0, "ltv": 0.0,
                "payback_months": 0.0, "churn_monthly": 0.0}
        rec = E["rm_receipts"]
        supply = {"raw_material": {"suppliers": sorted(rec["supplier"].unique().tolist()), "dpo_days": p["dpo_days"], "cover_months": 0.6},
                  "lead_time_dist": {"quoted_customer_lead_days": E["quoted_lead_days"],
                                     "flow_time_days_p8_mean": f["flow_days_p8"]},
                  "wip_buffer": {"wip_units_before_constraint_p8": f["wip_before_b"], "drum_buffer_rope_buffer_units": float(p["c_b"] * 16 * 5)},
                  "release_policy": "push (release on order receipt)"}
        pricing = {"price_ladder_cents": {fam: int(r.list_price_cents) for fam, r in prices.iterrows()},
                   "throughput_per_constraint_minute_cents": f["t_per_min"], "late_penalty_pct_per_week": p["late_penalty_pct_week"]}
        kpis: dict[str, dict[str, float]] = {}
        so = state.frames["sales_orders"]
        labor = state.frames["labor_hours"]
        for period in state.periods:
            s, e = cal.bounds(period)
            s_t, e_t = pd.Timestamp(s), pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            sh = so[(so["shipped_at"] >= s_t) & (so["shipped_at"] <= e_t)]
            tb = state.ledger.trial_balance(period)
            months = [f"{m:%Y-%m}" for m in pd.date_range(s_t, e_t, freq="MS")]
            kpis[period] = {
                "revenue_cents": float(inc[period].revenue_cents), "units_shipped": float(sh["shipped_qty"].sum()),
                "late_order_share": float((sh["late_days"] > 0).mean()) if len(sh) else 0.0,
                "wip_inventory_cents": float(tb["1210"]), "overtime_hours": float(labor.loc[labor["month"].isin(months), "overtime_hours"].sum()),
                "penalties_cents": float(state.ledger.activity(period)["4110"]), "gross_margin": state.statements.ratios[period]["gross_margin"],  # type: ignore[union-attr]
                "net_income_cents": float(inc[period].net_income_cents),
            }
        return build_operations_state(stations=stations, queueing=queueing, unit_economics=unit, supply_chain=supply, pricing=pricing,
                                      kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 5 key table computed from the generated MES data; reference SQL on the dirty DuckDB."""
        p, cal, E = state.params, state.calendar, state.extras
        f = self._facts(state)
        P = state.periods
        P5, P8 = P[4], P[-1]
        st = f["st"]
        wc = state.frames["work_centers"]
        nm = dict(zip(wc["work_center_id"], wc["name"], strict=True))
        b_id, cap_id = f["b_id"], f["capex_wc"]
        s5 = cal.bounds(P5)[0]
        s8, e8 = cal.bounds(P8)
        metrics = [
            metric("bottleneck_op_seq", f["true_b_seq"], "op_seq",
                   f"argmax over work centers of (setup + run minutes) / available minutes (regular + overtime, labor_hours) for {P5}-{P8}: "
                   f"op {f['true_b_seq']} ({nm[f['true_b']]}), utilization {st.loc[f['true_b'], 'rho']:.3f}.",
                   pillar="operations_strategy", tol_abs=0.0, label="True constraint (op_seq)",
                   hint="Utilization must include setup time; rank by total busy time over available time, trailing four quarters."),
            metric("rho_bottleneck", float(st.loc[b_id, "rho"]), "ratio", f"(setup + run) / available minutes at {nm[b_id]}, {P5}-{P8}.",
                   pillar="operations_strategy", tol_abs=0.03, label="Utilization of the constraint", hint="Busy minutes over available minutes."),
            metric("rho_capex_station", float(st.loc[cap_id, "rho"]), "ratio", f"(setup + run) / available minutes at {nm[cap_id]} (the capex target), {P5}-{P8}.",
                   pillar="operations_strategy", tol_abs=0.03, label="Utilization of the station in the capex request", hint="Same formula, the requested station."),
            metric("throughput_units_month_p8", f["thr_month"], "units", f"Good units shipped in {P8} (sales_orders.shipped_qty) / 3.",
                   pillar="operations_strategy", tol_rel=0.03, label="Throughput, latest quarter (units/month)", hint="Shipped good units per month."),
            metric("throughput_ceiling_units_month", f["ceiling"], "units",
                   f"c_b x hours_per_month x (1 - setup share at b in {P8} = {f['setup_share8']:.3f}) x (1 - scrap after b = {f['scrap_share']:.3f}); "
                   f"c_b = rated capacity of {nm[b_id]}.", pillar="operations_strategy", tol_rel=0.05, label="Throughput ceiling of the constraint",
                   hint="Rated capacity net of setup time and scrapped constraint output."),
            metric("wip_units_before_bottleneck_p8", f["wip_before_b"], "units",
                   f"Units on work orders released by {e8} that had not finished at {nm[b_id]} by then.", pillar="operations_strategy", tol_rel=0.05,
                   label="WIP in front of the constraint (units)", hint="Units of WIP released but not yet through the constraint."),
            metric("cycle_time_days_p8", f["cycle_days"], "days",
                   f"Little's Law W = L / lambda: L = open WIP at {e8} ({f['wip_total']:,.0f} units of WIP, all stations) / lambda = daily "
                   f"completion rate in {P8} ({f['thr_day']:,.1f} units/day) = W in days.",
                   pillar="operations_strategy", tol_abs=1.5, label="Cycle time (Little's Law)",
                   hint="L in units of WIP (open work-order units) / lambda in units per day = W in days."),
            metric("constraint_minutes_wasted_scrap_month", f["scrap_min_month"], "minutes",
                   f"Units scrapped at final inspection in {P8} x standard run minutes per unit at {nm[b_id]} (by family) / 3.",
                   pillar="operations_strategy", tol_rel=0.08, label="Constraint minutes wasted on scrap (per month)",
                   hint="Every unit scrapped after the constraint already consumed constraint time."),
            metric("constraint_minutes_setup_month", f["setup_min_month"], "minutes", f"Sum of setup_minutes_actual at {nm[b_id]} in {P8} / 3.",
                   pillar="operations_strategy", tol_rel=0.05, label="Constraint minutes spent on setups (per month)", hint="Setups at the constraint."),
            metric("migration_threshold_pct", f["migration"], "ratio",
                   f"c_b2 / c_b - 1 with c_b2 = rated capacity of {nm[f['b2_id']]} (the next-tightest station) and c_b of {nm[b_id]}.",
                   pillar="operations_strategy", tol_abs=0.02, label="Elevation before the constraint migrates",
                   hint="How much can the constraint be elevated before another station binds?"),
            metric("overtime_cost_p8_cents", f["ot8"], "cents", f"GL 5010 debits with source 'overtime' in {P8}.",
                   pillar="accounting_finance", tol_rel=0.05, label="Overtime (expediting) cost, latest quarter", hint="Overtime premium in the GL."),
        ]
        for fam, v in sorted(f["t_per_min"].items()):
            metrics.append(metric(f"t_per_constraint_minute_cents_{fam.lower().replace(' ', '_')}", v, "cents",
                                  f"(avg list price - RM cost) / standard run minutes at {nm[b_id]} for family {fam}.",
                                  pillar="accounting_finance", tol_rel=0.05, label=f"Throughput per constraint minute: {fam}",
                                  hint="Throughput accounting: T = price - truly variable cost, per minute of the constraint."))
        dedupe = "SELECT * FROM production_log QUALIFY ROW_NUMBER() OVER (PARTITION BY log_id ORDER BY ingest_id) = 1" if p["difficulty"] == "principal" \
            else "SELECT * FROM production_log"
        sql_util = f"""
WITH log AS ({dedupe}),
avail AS (
  SELECT work_center_id, SUM(regular_hours + overtime_hours) * 60 AS avail_min
  FROM labor_hours WHERE month >= '{s5:%Y-%m}' GROUP BY 1),
busy AS (
  SELECT work_center_id, SUM(setup_minutes_actual + run_minutes_actual) AS busy_min, SUM(run_minutes_actual) AS run_min,
         AVG(date_diff('second', queue_in_at, start_at)) / 3600.0 AS avg_queue_hours
  FROM log WHERE start_at >= TIMESTAMP '{s5} 00:00:00' GROUP BY 1)
SELECT w.op_seq, TRIM(w.name) AS work_center, b.busy_min / a.avail_min AS utilization,
       b.run_min / a.avail_min AS run_only_utilization, b.avg_queue_hours
FROM busy b JOIN avail a USING (work_center_id) JOIN work_centers w USING (work_center_id)
ORDER BY utilization DESC, w.op_seq"""

        def util_checks(df: pd.DataFrame) -> dict[str, Any]:
            return {"top_op_seq": int(df.iloc[0]["op_seq"]), "naive_top_op_seq": int(df.sort_values("run_only_utilization", ascending=False).iloc[0]["op_seq"]),
                    "max_queue_op_seq": int(df.sort_values("avg_queue_hours", ascending=False).iloc[0]["op_seq"])}

        q_util = run_reference_query(con, "utilization_by_work_center", sql_util, signature=["with", "join", "group by", "sum(", "avg("],
                                     metric_keys=["bottleneck_op_seq", "rho_bottleneck", "rho_capex_station"], checks=util_checks)
        sql_capex = """
SELECT TRIM(w.name) AS work_center, w.op_seq, w.planned_capex_cents, w.capacity_units_per_hour,
       SUM(CASE WHEN extract('day' FROM l.queue_in_at) >= 25 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS month_end_arrival_share,
       AVG(date_diff('second', l.queue_in_at, l.start_at)) / 3600.0 AS avg_queue_hours
FROM work_centers w JOIN production_log l USING (work_center_id)
GROUP BY 1, 2, 3, 4 ORDER BY w.op_seq"""
        q_capex = run_reference_query(con, "capex_request_vs_capacity", sql_capex, signature=["join", "group by", "case when", "avg("],
                                      metric_keys=["rho_capex_station", "migration_threshold_pct"],
                                      checks=lambda df: {"capex_op_seq": int(df.loc[df["planned_capex_cents"] > 0, "op_seq"].iloc[0])})
        sql_scrap = """
SELECT o.op_seq AS origin_op_seq, TRIM(o.name) AS caused_at, TRIM(d.name) AS detected_at,
       SUM(q.quantity) AS units_scrapped, COUNT(*) AS events
FROM quality_events q
JOIN work_centers o ON o.work_center_id = q.origin_work_center_id
JOIN work_centers d ON d.work_center_id = q.work_center_id
GROUP BY 1, 2, 3 ORDER BY units_scrapped DESC, 1, 2, 3"""
        q_scrap = run_reference_query(con, "scrap_origin_vs_detection", sql_scrap, signature=["join", "group by", "sum("],
                                      metric_keys=["constraint_minutes_wasted_scrap_month"],
                                      checks=lambda df: {"top_origin_op_seq": int(df.iloc[0]["origin_op_seq"])})
        sql_waste = f"""
WITH r AS (
  SELECT r.product_id, r.run_minutes_per_unit FROM routings r JOIN work_centers w USING (work_center_id)
  WHERE w.work_center_id = '{b_id}')
SELECT SUM(q.quantity) AS units_scrapped, SUM(q.quantity * r.run_minutes_per_unit) / 3.0 AS constraint_minutes_per_month
FROM quality_events q JOIN work_orders o USING (work_order_id) JOIN r USING (product_id)
WHERE q.detected_at >= TIMESTAMP '{s8} 00:00:00' AND q.detected_at < TIMESTAMP '{e8} 00:00:00' + INTERVAL 1 DAY"""
        q_waste = run_reference_query(con, "constraint_minutes_lost_to_scrap_p8", sql_waste, signature=["with", "join", "sum("],
                                      metric_keys=["constraint_minutes_wasted_scrap_month"],
                                      checks=lambda df: {"constraint_minutes_per_month": float(df.iloc[0]["constraint_minutes_per_month"] or 0.0)})
        sql_setup = f"""
WITH log AS ({dedupe})
SELECT period_of(start_at) AS period, SUM(setups) AS setups, SUM(setup_minutes_actual) AS setup_minutes,
       SUM(qty_processed) * 1.0 / SUM(setups) AS units_per_setup
FROM log WHERE work_center_id = '{b_id}' GROUP BY 1 ORDER BY 1"""
        q_setup = run_reference_query(con, "setups_at_constraint_by_quarter", sql_setup, signature=["with", "group by", "sum("],
                                      metric_keys=["constraint_minutes_setup_month"],
                                      checks=lambda df: {"setup_minutes_last": float(df.iloc[-1]["setup_minutes"])})
        sql_batch = f"""
SELECT w.op_seq, TRIM(w.name) AS work_center, SUM(l.setups) AS setups, AVG(l.qty_processed * 1.0 / l.setups) AS avg_transfer_batch,
       SUM(l.setup_minutes_actual) / SUM(l.setup_minutes_actual + l.run_minutes_actual) AS setup_share_of_busy
FROM production_log l JOIN work_centers w USING (work_center_id)
WHERE l.start_at >= TIMESTAMP '{s8} 00:00:00'
GROUP BY 1, 2 ORDER BY setup_share_of_busy DESC, 1"""
        q_batch = run_reference_query(con, "transfer_batch_size_by_work_center_p8", sql_batch, signature=["join", "group by", "sum(", "avg("],
                                      metric_keys=["constraint_minutes_setup_month", "throughput_ceiling_units_month"],
                                      checks=lambda df: {"max_setup_share_op_seq": int(df.iloc[0]["op_seq"])})
        sql_wip = f"""
WITH thru AS (
  SELECT work_order_id, MIN(end_at) AS done_at FROM production_log WHERE work_center_id = '{b_id}' GROUP BY 1),
q AS (SELECT DISTINCT period, end_date FROM period_calendar WHERE is_reporting)
SELECT q.period,
       SUM(CASE WHEN o.released_at <= q.end_date + INTERVAL 1 DAY AND (t.done_at IS NULL OR t.done_at > q.end_date + INTERVAL 1 DAY)
                THEN o.quantity_planned ELSE 0 END) AS wip_units_before_constraint
FROM q CROSS JOIN work_orders o LEFT JOIN thru t USING (work_order_id)
GROUP BY 1 ORDER BY 1"""
        q_wip = run_reference_query(con, "wip_before_constraint_by_quarter", sql_wip, signature=["with", "left join", "group by", "case when"],
                                    metric_keys=["wip_units_before_bottleneck_p8", "cycle_time_days_p8"],
                                    checks=lambda df: {"wip_last": float(df.iloc[-1]["wip_units_before_constraint"]),
                                                       "wip_rising": bool(df.iloc[-1]["wip_units_before_constraint"] > df.iloc[0]["wip_units_before_constraint"])})
        sql_ot = """
SELECT j.period, SUM(CASE WHEN j.source = 'overtime' THEN l.debit_cents ELSE 0 END) AS overtime_cents,
       SUM(CASE WHEN l.account_code = '4110' THEN l.debit_cents ELSE 0 END) AS penalty_cents,
       SUM(CASE WHEN l.account_code = '1210' THEN l.debit_cents - l.credit_cents ELSE 0 END) AS wip_change_cents
FROM gl_lines l JOIN gl_journal j USING (entry_id) WHERE j.period IN (SELECT period FROM period_calendar WHERE is_reporting)
GROUP BY 1 ORDER BY 1"""
        q_ot = run_reference_query(con, "overtime_penalties_wip_by_quarter", sql_ot, signature=["join", "group by", "case when", "sum("],
                                   metric_keys=["overtime_cost_p8_cents"], checks=lambda df: {"overtime_last": float(df.iloc[-1]["overtime_cents"])})
        an = {a.id: a for a in state.anomalies}
        an["A1"] = an["A1"].model_copy(update={
            "reference_sql": [q_util, q_capex],
            "magnitude": {**an["A1"].magnitude, "true_constraint_op_seq": float(f["true_b_seq"]), "naive_constraint_op_seq": float(f["naive_b_seq"]),
                          "rho_bottleneck": round(float(st.loc[b_id, "rho"]), 6), "rho_capex_station": round(float(st.loc[cap_id, "rho"]), 6)},
            "evidence_trail": [f"Shipments flat (~{f['thr_month']:,.0f} units/month) while WIP and overtime rise.",
                               f"Utilization incl. setups: {nm[b_id]} {st.loc[b_id, 'rho']:.2f} vs {nm[cap_id]} {st.loc[cap_id, 'rho']:.2f}.",
                               f"Run-only 'efficiency' ranks {nm[f['naive_b']]} first ({st.loc[f['naive_b'], 'rho_naive']:.2f})."]})
        an["A2"] = an["A2"].model_copy(update={
            "reference_sql": [q_scrap, q_waste],
            "magnitude": {**an["A2"].magnitude, "scrap_share_p8": round(f["scrap_share"], 6), "constraint_minutes_wasted_scrap_month": round(f["scrap_min_month"], 4),
                          "origin_upstream_share": round(f["origin_upstream_share"], 6)},
            "evidence_trail": ["All scrap is detected at the last station (end-of-line gauge).",
                               f"{f['origin_upstream_share']:.0%} of scrapped units were caused upstream of the constraint."]})
        an["A3"] = an["A3"].model_copy(update={
            "reference_sql": [q_setup, q_batch],
            "magnitude": {**an["A3"].magnitude, "setup_share_p8": round(f["setup_share8"], 6), "constraint_minutes_setup_month": round(f["setup_min_month"], 4)},
            "evidence_trail": [f"Setups at {nm[b_id]} jump in {P[2]}; transfer batches shrink.", f"Setup share of available time {f['setup_share8']:.1%} in {P8}."]})
        an["A4"] = an["A4"].model_copy(update={
            "reference_sql": [q_wip, q_ot],
            "magnitude": {**an["A4"].magnitude, "migration_threshold_pct": round(f["migration"], 6)},
            "evidence_trail": [f"{nm[f['b2_id']]} is the next-tightest station: elevation beyond {f['migration']:.1%} moves the constraint there."]})
        herrings_all = [
            f"The {nm[cap_id]} line has the biggest queue at month end - a surge from batched release upstream; its utilization is only "
            f"{st.loc[cap_id, 'rho']:.2f}.",
            "We need more operators everywhere - non-constraints run well below capacity; adding capacity there only raises WIP.",
            f"Scrap ({f['scrap_share']:.1%}) is a quality-department problem within industry norms - its location after the constraint is what costs throughput.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        root = (f"The plant's true constraint is {nm[b_id]} (op {f['true_b_seq']}): with setups included it is busy {st.loc[b_id, 'rho']:.0%} of available time "
                f"while push release keeps loading it, so WIP piles up in front of it ({f['wip_before_b']:,.0f} units at {P8} end) and cycle time "
                f"stretches to ~{f['cycle_days']:.0f} days. Since {P[2]} a local-efficiency KPI pushed small transfer batches, so setups now consume "
                f"{f['setup_share8']:.0%} of the constraint, and end-of-line inspection scraps {f['scrap_share']:.1%} of units after they consumed constraint time "
                f"(most defects are caused upstream at {nm[state.frames['work_centers']['work_center_id'].iat[E['origin']]]}). The capex request targets "
                f"{nm[cap_id]}, a non-constraint ({st.loc[cap_id, 'rho']:.0%} utilized). Batch up at the constraint, move inspection in front of it and "
                f"subordinate release (drum-buffer-rope); elevate the constraint only up to the migration threshold of {f['migration']:.0%} "
                f"before {nm[f['b2_id']]} binds.")
        py = ("import pandas as pd\n"
              "log = pd.read_sql('SELECT * FROM production_log', con).drop_duplicates('log_id')\n"
              "lab = pd.read_sql('SELECT * FROM labor_hours', con)\n"
              "busy = log.assign(m=log.setup_minutes_actual + log.run_minutes_actual).groupby('work_center_id').m.sum()\n"
              "avail = lab.assign(m=(lab.regular_hours + lab.overtime_hours) * 60).groupby('work_center_id').m.sum()\n"
              "print((busy / avail).sort_values())  # true utilization, incl. setups\n"
              "# Little's Law: cycle time (days) = WIP (units) / throughput (units/day); simulate +x% capacity at b capped at c_b2 (migration)\n")
        return GroundTruth(
            root_cause=root, anomalies=[an[k] for k in ("A1", "A2", "A3", "A4")], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q.sql for q in (q_util, q_capex, q_scrap, q_waste, q_setup, q_batch, q_wip, q_ot)], python=[py],
                memo_bullets=[
                    f"The constraint is {nm[b_id]}, not {nm[cap_id]}: rank by busy time including setups, not by month-end queues or run-time efficiency.",
                    "Exploit: larger transfer batches at the constraint (small batches elsewhere), inspect before the constraint.",
                    "Subordinate: release work at the constraint's pace (drum-buffer-rope) to cut WIP and cycle time without losing throughput.",
                    f"Elevate only after exploiting; gains cap at c_b2/c_b - 1 = {f['migration']:.0%} when {nm[f['b2_id']]} becomes the constraint.",
                    "Throughput accounting: a minute lost at the constraint is a minute lost for the plant; rank products by T per constraint minute.",
                    f"Withdraw the {nm[cap_id]} capex request.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal/conceal, four stakeholders, spec timeline (red herrings flagged) and the process-flow diagram."""
        p, E = state.params, state.extras
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        wc = state.frames["work_centers"]
        nm = dict(zip(wc["work_center_id"], wc["name"], strict=True))
        names = rng.pick_names(4)
        cap_name = nm[f["capex_wc"]]
        reveal = [
            f"{f['late8']:.0%} of orders shipped late in {P[-1]}; customer penalty credits reached {f['pen8'] / 100:,.0f} USD in the quarter.",
            f"Overtime premium of {f['ot8'] / 100:,.0f} USD was paid in {P[-1]}.",
            f"The plant manager requests {E['capex_request_cents'] / 100:,.0f} USD of capex for the {cap_name} line.",
            "Plant manager: 'Our people are 95% efficient - every work center is busy.'",
            f"End-of-line scrap runs at about {f['scrap_share']:.1%}.",
        ]
        conceal = [
            "the station utilization table (setup + run over available time)", f"the true constraint is {nm[f['b_id']]}",
            "setup share at the constraint and the small-batch policy", "scrap after the constraint wastes constraint minutes",
            "the scrap origin analysis (defects caused upstream)", f"the migration threshold ({f['migration']:.0%}) before {nm[f['b2_id']]} binds",
            "A1", "A2", "A3", "A4",
        ]
        stakeholders = [
            Stakeholder(name=names[0], role="Plant Manager", agenda=f"Get capex for {cap_name}; measured on local efficiency.", disposition="confident",
                        knows=["every work center hits its efficiency target"], hides=["the efficiency dashboard excludes setup time"]),
            Stakeholder(name=names[1], role="COO", agenda="On-time delivery; suspicious of the capex request.", disposition="skeptical",
                        knows=["penalty clauses are now being enforced"], hides=[]),
            Stakeholder(name=names[2], role="Quality Lead", agenda="Hit the scrap target.", disposition="cooperative",
                        knows=[f"defects originate at {nm[wc['work_center_id'].iat[E['origin']]]}"],
                        hides=["inspection is at the end 'because that's where the gauge is'"]),
            Stakeholder(name=names[3], role="Production Scheduler", agenda="Keep every station busy.", disposition="tired",
                        knows=[f"small batches at {nm[f['b_id']]} are killing its output"], hides=["was told to be 'responsive' and split batches"]),
        ]
        tl = [
            TimelineEvent(period=P[2], headline="Local efficiency KPI introduced", detail="Work centers ranked on utilization; schedulers told to be responsive."),
            TimelineEvent(period=P[4], headline="Overtime authorized", detail="Quarter-end overtime to catch up on late orders."),
            TimelineEvent(period=P[5], headline="Customer penalty clause triggered", detail="Key account enforces late-delivery credits."),
            TimelineEvent(period=P[5], headline=f"{cap_name} queue photo in board deck", detail="Pallets stacked at the end of the line on the last day of the month.",
                          is_red_herring=True),
            TimelineEvent(period=P[7], headline=f"Capex request for {cap_name}", detail="Business case cites the month-end queue."),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[6], headline="Quality initiative: more end-of-line inspection", detail="Second gauge station at final inspection.",
                                    is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[6], headline="Union asks for more operators on every line", detail="Headcount request across all stations.",
                                    is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        flow = ["flowchart LR", '  REL["Order received -> work order released (push)"]']
        prev = "REL"
        for i, r in enumerate(wc.itertuples()):
            node = f"S{i}"
            flag = " (capex requested)" if r.work_center_id == f["capex_wc"] else ""
            flow.append(f'  {node}["{r.op_seq} {r.name}{flag}"]')
            flow.append(f"  {prev} --> {node}")
            prev = node
        flow += ['  QC{"Final inspection (gauge)"}', f"  {prev} --> QC", '  QC -->|"good"| SHIP["Ship & invoice"]', '  QC -->|"scrap"| SCR["Scrap bin"]']
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["capex_request", "efficiency_dashboard", "late_order_list", "floor_photo_description", "process_flow_diagram"],
                           architecture_mermaid="\n".join(flow))

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Five levers; defaults are the P8 status quo."""
        f = self._facts(state)
        wc = state.frames["work_centers"]
        nm = dict(zip(wc["work_center_id"], wc["name"], strict=True))
        return [
            Lever(key="capacity_add_bottleneck_pct", label=f"Add capacity at {nm[f['b_id']]}", min=0, max=40, default=0, unit="%",
                  effect="Throughput rises with constraint capacity until the next-tightest station binds (constraint migrates)."),
            Lever(key="capacity_add_finishing_pct", label=f"Add capacity at {nm[f['capex_wc']]} (capex request)", min=0, max=40, default=0, unit="%",
                  effect="Depreciation and cost; throughput does not move."),
            Lever(key="move_inspection_before_b", label="Inspect before the constraint (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Scrap after the constraint falls ~70%; constraint minutes recovered."),
            Lever(key="batch_size_b_multiplier", label="Transfer batch multiplier at the constraint", min=1, max=4, default=1, unit="x",
                  effect="Setup share at the constraint falls; slightly more WIP downstream."),
            Lever(key="release_policy_dbr", label="Release policy (0 push / 1 drum-buffer-rope)", min=0, max=1, default=0, unit="code",
                  effect="WIP collapses to a buffer, cycle time falls; throughput unchanged."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form transition model (module docstring); migration encoded via ``bounds``."""
        p, E = state.params, state.extras
        f = self._facts(state)
        P = state.periods
        P8 = P[-1]
        inc = {i.period: i for i in state.statements.income}  # type: ignore[union-attr]
        prices = state.frames["products"]
        price = float(prices["list_price_cents"].mean())
        rm = float(prices["rm_cost_cents"].mean())
        H = p["hours_per_month"]
        cap_b_units = p["c_b"] * H * (1 - f["setup_share8"]) * (1 - f["scrap_share"])
        b2_ceiling = p["c_b2"] * H * (1 - f["scrap_share"])
        demand = float(E["fluid"]["R"][-3:].mean())
        upper = min(demand, b2_ceiling)
        thr = f["thr_month"]
        rho_b2 = float(f["st8"].loc[f["b2_id"], "rho"])
        kpis = {
            "throughput_units_month": thr, "revenue_cents": float(inc[P8].revenue_cents), "operating_income_cents": float(inc[P8].operating_income_cents),
            "wip_units": f["wip_total"], "cycle_time_days": f["cycle_days"], "overtime_cost_cents": float(f["ot8"]),
            "late_order_share": f["late8"], "setup_share_b": f["setup_share8"], "scrap_after_b_pct": f["scrap_share"],
            "b2_utilization": rho_b2, "capex_cents": 0.0,
        }
        t_unit = price - rm
        per_pt = cap_b_units / 100.0
        capex_per_pt = E["capex_request_cents"] / 30.0
        rec_scrap = 0.7 * f["scrap_share"] * f["units_b8"] / 3
        rec_setup = f["setup_share8"] / 4 * p["c_b"] * H
        b2_per_unit = 1.0 / (p["c_b2"] * H)
        effects = [
            {"lever": "capacity_add_bottleneck_pct", "kpi": "throughput_units_month", "delta_per_unit": per_pt, "lag_periods": 1},
            {"lever": "capacity_add_bottleneck_pct", "kpi": "revenue_cents", "delta_per_unit": per_pt * price * 3, "lag_periods": 1},
            {"lever": "capacity_add_bottleneck_pct", "kpi": "operating_income_cents", "delta_per_unit": per_pt * t_unit * 3 - capex_per_pt / 32, "lag_periods": 1},
            {"lever": "capacity_add_bottleneck_pct", "kpi": "b2_utilization", "delta_per_unit": per_pt * b2_per_unit, "lag_periods": 1},
            {"lever": "capacity_add_bottleneck_pct", "kpi": "capex_cents", "delta_per_unit": capex_per_pt, "lag_periods": 0},
            {"lever": "capacity_add_bottleneck_pct", "kpi": "wip_units", "delta_per_unit": -per_pt * 0.5, "lag_periods": 1},
            {"lever": "capacity_add_finishing_pct", "kpi": "capex_cents", "delta_per_unit": capex_per_pt, "lag_periods": 0},
            {"lever": "capacity_add_finishing_pct", "kpi": "operating_income_cents", "delta_per_unit": -capex_per_pt / 32, "lag_periods": 1},
            {"lever": "move_inspection_before_b", "kpi": "scrap_after_b_pct", "delta_per_unit": -0.7 * f["scrap_share"], "lag_periods": 0},
            {"lever": "move_inspection_before_b", "kpi": "throughput_units_month", "delta_per_unit": rec_scrap, "lag_periods": 1},
            {"lever": "move_inspection_before_b", "kpi": "revenue_cents", "delta_per_unit": rec_scrap * price * 3, "lag_periods": 1},
            {"lever": "move_inspection_before_b", "kpi": "operating_income_cents", "delta_per_unit": rec_scrap * t_unit * 3, "lag_periods": 1},
            {"lever": "move_inspection_before_b", "kpi": "b2_utilization", "delta_per_unit": rec_scrap * b2_per_unit, "lag_periods": 1},
            {"lever": "batch_size_b_multiplier", "kpi": "setup_share_b", "delta_per_unit": -f["setup_share8"] / 4, "lag_periods": 0},
            {"lever": "batch_size_b_multiplier", "kpi": "throughput_units_month", "delta_per_unit": rec_setup, "lag_periods": 1},
            {"lever": "batch_size_b_multiplier", "kpi": "revenue_cents", "delta_per_unit": rec_setup * price * 3, "lag_periods": 1},
            {"lever": "batch_size_b_multiplier", "kpi": "operating_income_cents", "delta_per_unit": rec_setup * t_unit * 3, "lag_periods": 1},
            {"lever": "batch_size_b_multiplier", "kpi": "b2_utilization", "delta_per_unit": rec_setup * b2_per_unit, "lag_periods": 1},
            {"lever": "batch_size_b_multiplier", "kpi": "wip_units", "delta_per_unit": 0.03 * f["wip_total"], "lag_periods": 0},
            {"lever": "batch_size_b_multiplier", "kpi": "cycle_time_days", "delta_per_unit": 0.03 * f["cycle_days"], "lag_periods": 0},
            {"lever": "release_policy_dbr", "kpi": "wip_units", "delta_per_unit": -(f["wip_total"] - p["c_b"] * 16 * 5), "lag_periods": 1},
            {"lever": "release_policy_dbr", "kpi": "cycle_time_days", "delta_per_unit": -0.6 * f["cycle_days"], "lag_periods": 1},
            {"lever": "release_policy_dbr", "kpi": "overtime_cost_cents", "delta_per_unit": -0.5 * f["ot8"], "lag_periods": 1},
            {"lever": "release_policy_dbr", "kpi": "late_order_share", "delta_per_unit": -0.6 * f["late8"], "lag_periods": 1},
        ]
        n = 3
        st_hist = {per: self._quarter_kpis(state, per) for per in P[4:]}
        drag = {
            "wip_units": (st_hist[P8]["wip"] - st_hist[P[4]]["wip"]) / n,
            "cycle_time_days": (st_hist[P8]["ct"] - st_hist[P[4]]["ct"]) / n,
            "overtime_cost_cents": (f["ot8"] - state.ledger.sum_lines(P[4], ["5010"], "debit", "overtime")) / n,
            "late_order_share": (st_hist[P8]["late"] - st_hist[P[4]]["late"]) / n,
        }
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"move_inspection_before_b": 1.0, "batch_size_b_multiplier": 2.0},
                "bounds": {"throughput_units_month": [0.0, upper], "revenue_cents": [0.0, kpis["revenue_cents"] * upper / max(thr, 1.0)], "b2_utilization": [0.0, 1.0],
                           "wip_units": [0.0, None], "cycle_time_days": [0.5, None], "late_order_share": [0.0, 1.0], "scrap_after_b_pct": [0.0, 1.0],
                           "setup_share_b": [0.0, 1.0], "overtime_cost_cents": [0.0, None]},
                "max_quarters": 4,
                "migration": {"c_b": p["c_b"], "c_b2": p["c_b2"], "threshold_pct": f["migration"], "throughput_ceiling_at_b2_units_month": b2_ceiling}}

    def _quarter_kpis(self, state: WorldState, period: str) -> dict[str, float]:
        cal, E = state.calendar, state.extras
        s, e = cal.bounds(period)
        s_t, e_t = pd.Timestamp(s), pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        wo = state.frames["work_orders"]
        so = state.frames["sales_orders"]
        released = (wo["released_at"] <= e_t) | E["opening_wo"]
        open_all = released & ~(wo["closed_at"].notna() & (wo["closed_at"] <= e_t))
        wip = float(wo.loc[open_all, "quantity_planned"].sum())
        done = wo[(wo["closed_at"] >= s_t) & (wo["closed_at"] <= e_t)]
        thr_day = float(done["quantity_planned"].sum()) / cal.days(period)
        sh = so[(so["shipped_at"] >= s_t) & (so["shipped_at"] <= e_t)]
        return {"wip": wip, "ct": wip / thr_day if thr_day else 0.0, "late": float((sh["late_days"] > 0).mean()) if len(sh) else 0.0}
