"""``outbox_pattern`` - "Dual-Write" (archetypes.md section 10).

A usage-billed platform (metered API / payments API / labelling API, depending on the industry)
moved billing onto an event pipeline at the start of P5. The product service commits each
metering record to ``usage_events`` (Postgres) and *then* publishes it to the broker in a separate
call - a dual write with no transactional outbox. A rating consumer aggregates what reaches the bus
into ``rated_usage`` and the monthly billing job invoices from ``rated_usage``. Infrastructure cost
follows the real usage (``usage_events``) while revenue follows what was rated, so every message
that never reached the bus is unbilled usage with its cost fully booked.

Model
-----
* ``usage_events`` = metering flushes (one row per customer service flush), Pareto customers,
  growing monthly volume, diurnal timing; ``occurred_at`` is the end of the metering interval,
  ``recorded_at`` the commit. Every ``usage-service`` deploy restarts pods; graceful shutdown
  flushes buffered counters (extra rows recorded within 90 s after ``deployed_at``) and the pod is
  killed before it publishes them - the deploy-time loss mode. Deploys are weekly, then daily in the
  anomaly window.
* Before P5 the legacy nightly batch billed straight from ``usage_events`` (exact). From P5 the
  publish fails with ``publish_failure_rate_base`` everywhere, and in the anomaly window (last 2/3/4
  quarters by difficulty) additional losses are drawn - weighted towards broker-partition incidents
  (``broker_incidents`` severity ``sev2``, 12-48 h) and deploy flushes - until each window month has
  lost exactly ``lost_event_share`` of its metered units (weighted sampling without replacement, so
  the realised share equals the sampled parameter to within one event).
* Principal: a P7 change on ``batch-import`` published before commit; ``U(1,3)%`` of those
  transactions rolled back after publishing -> phantom ``bus_messages`` whose ``event_id`` exists
  nowhere in ``usage_events`` -> over-billing -> ``billing_dispute`` tickets and ``credits``
  (Dr 4100 / Cr 1100) for the customers who complained. An ``outbox`` table exists but only the
  customer-lifecycle service uses it.
* GL: month-end invoice batch ``Dr 1100; Cr 4000 platform fees, Cr 4020 usage``; collections
  weekly; hosting ``Dr 5000`` = metered units x unit cost (the cloud price change in P6 is -2%, the
  red herring) and fixed infrastructure ``Dr 5020``, both ``Cr 2000`` and paid on the 10th; payroll,
  opex, capex, depreciation and tax as usual. No unbilled receivable is recorded.
* **Designed sub-ledger break** ``usage_events_vs_billed_usage``: metered value (usage_events x plan
  price, by usage quarter) minus GL 4020 = lost minus phantom dollars per quarter, declared and
  verified to the cent. AR, AP and the usage-revenue tie to ``invoice_lines`` all hold - billing is
  internally consistent downstream, which is why nobody noticed.

Simulation (reduced form): KPIs are P8 quarter levels. ``implement_outbox`` (0/1: outbox table in
the same transaction + polling or CDC relay) removes lost and phantom events and closes the margin
gap with a one-quarter lag on revenue, but makes delivery at-least-once (``duplicate_events`` rise);
``consumer_idempotent`` removes the duplicates; ``backbill`` recovers the in-window amount once
(cash, net income) at a churn cost; ``reconcile_job`` cuts detection time; ``bigger_broker`` costs
money and removes only incident losses (the trap). ``resolved_if``: outbox and idempotent consumer.
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
    WorldState,
    difficulty_pick,
    magnitude,
    metric,
    run_reference_query,
)
from axiom.engine.datagen import GL_BATCH, ROW_BUDGET, TableBuilder
from axiom.engine.finance import SubledgerTie, batch_end_dates, cents, split_cents
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state, mm1, station_network
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

logger = logging.getLogger("axiom.archetypes.outbox_pattern")

PLAN_TIERS = ("starter", "growth", "scale", "enterprise")
PRICE_MULT = {"starter": 1.6, "growth": 1.25, "scale": 1.0, "enterprise": 0.8}
FEE_MULT = {"starter": 0.25, "growth": 0.6, "scale": 1.4, "enterprise": 3.5}
SERVICES = ("api-gateway", "batch-import", "webhooks")
DEPLOY_WINDOW_S = 120
INDUSTRY_STORY = {
    "saas": ("Usage-based API platform billed per 1k API calls", "api_calls_k", "saas"),
    "fintech_marketplace": ("Payments infrastructure API billed per processed transaction", "transactions", "saas"),
    "3pl": ("Shipping-label and tracking API for e-commerce brands, billed per label", "labels", "3pl"),
}
FLAWED_MERMAID = """flowchart LR
  A[Product Service / usage-service] -->|"1. INSERT usage_events (commit)"| DB[(Postgres)]
  A -->|"2. publish(event) - separate call, no outbox"| K[(Kafka)]
  K --> C[Rating Consumer] --> RU[(rated_usage)] --> B[Monthly Billing Job] --> I[Invoices -> GL 4020]
  DB -.->|"never compared"| RU
  D[Deploys: SIGTERM -> flush counters -> SIGKILL] -.-> A
  classDef bad fill:#3b1d1d,stroke:#ff5555,color:#fff
  class A bad"""
FIXED_MERMAID = """flowchart LR
  A[usage-service] -->|"BEGIN; INSERT usage_events; INSERT outbox(event_id, payload); COMMIT"| DB[(Postgres)]
  R[Outbox relay: polling or CDC / Debezium] -->|"read committed outbox rows"| DB
  R -->|"publish (at-least-once), mark published_at"| K[(Kafka)]
  K --> C[Rating Consumer: idempotent, UNIQUE(event_id)] --> RU[(rated_usage)] --> B[Billing Job] --> I[Invoices]
  REC[Daily reconciliation: usage_events vs rated_usage] --> DB
  REC --> RU"""


def _month_index(ts: pd.Series | np.ndarray, start: dt.date) -> np.ndarray:
    t = pd.DatetimeIndex(pd.to_datetime(np.asarray(ts)))
    return ((t.year - start.year) * 12 + (t.month - start.month)).to_numpy()


def _utc(col: str, dirty: bool) -> str:
    """SQL expression normalising a timestamp column that may carry mixed UTC offsets (tz_mixed dirt)."""
    return f"(CAST({col} AS TIMESTAMPTZ) AT TIME ZONE 'UTC')" if dirty else col


@register
class OutboxPattern(Archetype):
    """Dual write without an outbox: lost (and phantom) usage events leak revenue."""

    key = "outbox_pattern"
    title = "Dual-Write"
    pillars = ("system_design", "accounting_finance", "sql")
    industries = ("saas", "fintech_marketplace", "3pl")
    schema_template = "event_platform"
    summary = ("Gross margin slipped after billing moved onto an event pipeline; infrastructure cost tracks real usage "
               "but some metered usage never reaches the invoices.")
    root_cause_terms = ("dual write", "outbox", "lost events", "deploy")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 10 (difficulty scales magnitude, window, dirt and red herrings)."""
        ind = industry or "saas"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        story, unit, _ = INDUSTRY_STORY[ind]
        rev_usd = math.exp(rng.uniform(math.log(8e6), math.log(30e6)))
        window = difficulty_pick(difficulty, 2, 3, 4)
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": story,
            "usage_unit": unit,
            "revenue_ttm_cents": int(rev_usd * 100),
            "growth_qoq": float(rng.uniform(0.04, 0.09)),
            "gm": float(rng.uniform(0.52, 0.64)),
            "fee_share": float(rng.uniform(0.10, 0.20)),
            "base_price_cents": int(rng.integers(12, 31)),
            "op_margin": float(rng.uniform(0.04, 0.12)),
            "tax_rate": float(rng.uniform(0.18, 0.25)),
            "capex_pct": float(rng.uniform(0.01, 0.03)),
            "publish_failure_rate_base": float(rng.uniform(0.0005, 0.003)),
            "incident_failure_rate": float(rng.uniform(0.05, 0.25)),
            "deploy_flush_loss_rate": float(rng.uniform(0.35, 0.7)),
            "n_incidents": int(rng.integers(2, 4)),
            "lost_event_share": magnitude(rng, difficulty, 0.008, 0.035),
            "ordering_fault": difficulty == "principal",
            "phantom_rate": magnitude(rng, difficulty, 0.01, 0.03) if difficulty == "principal" else 0.0,
            "complaint_rate": float(rng.uniform(0.02, 0.06)),
            "cloud_price_change": -0.02,
            "terms_days": 30,
            "window_periods": window,
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        p["n_customers"] = difficulty_pick(difficulty, 24, 42, 80)
        budget = ROW_BUDGET[difficulty]
        # rows: usage (1.0) + bus (~0.55) + dup dirt; fixed: invoices/lines/payments/rated/gl/etc.
        n_c = p["n_customers"]
        fixed = n_c * 27 * 3 + n_c * 12 * 4 + difficulty_pick(difficulty, 1700, 1900, 4200) + 600
        p["n_events"] = int(max(2500, (budget * difficulty_pick(difficulty, 0.9, 0.9, 0.85) - fixed) / 1.6))
        p["description"] = (f"{story}; billing moved onto a Kafka-based event pipeline last year; "
                            f"~{n_c} business customers on usage plans.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Customers, plans, contracts, deploys, incidents and the metered usage stream."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        C = p["n_customers"]
        w = 1.0 + g.pareto(1.16, C)
        order = np.argsort(-w, kind="stable")
        rank = np.empty(C, dtype=int)
        rank[order] = np.arange(C)
        tier = np.where(rank < max(1, C // 10), "enterprise", np.where(rank < C * 0.3, "scale", np.where(rank < C * 0.65, "growth", "starter")))
        # 60% of customers exist before the window, the rest onboard over the first 23 months
        start_m = np.where(g.random(C) < 0.6, 0, g.integers(1, n_m - 3, C))
        start_m[order[:3]] = 0
        created = (pd.to_datetime(pd.Series([months[m] for m in start_m])) + pd.to_timedelta(g.integers(0, 25, C), unit="D")
                   - pd.to_timedelta(np.where(start_m == 0, g.integers(30, 700, C), 0), unit="D"))
        names = []
        seen: set[str] = set()
        while len(names) < C:
            nm = rng.company_name("saas" if p["industry"] != "3pl" else "ecommerce")
            if nm not in seen:
                seen.add(nm)
                names.append(nm)
        cust_ids = np.array([f"CUS-{i:04d}" for i in range(1, C + 1)])
        customers = pd.DataFrame({
            "customer_id": cust_ids,
            "name": names,
            "segment": np.where(tier == "enterprise", "enterprise", np.where(tier == "scale", "midmarket", "smb")),
            "plan_id": np.array([f"PLN-{t.upper()}" for t in tier]),
            "country": np.array(("US", "US", "US", "CA", "GB", "DE", "NL"))[g.integers(0, 7, C)],
            "billing_email": [f"billing@{n.split()[0].lower()}{i}.example.com" for i, n in enumerate(names)],
            "created_at": created,
        })
        state.extras.update({"cust_weight": w, "cust_start_m": start_m, "cust_tier": tier})
        # --- metered events per month (growth x active customer mass), allocated to active customers
        N = p["n_events"]
        trend = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3.0) * g.lognormal(0, 0.04, n_m)
        active = (start_m[None, :] <= np.arange(n_m)[:, None])
        mass = (active * w[None, :]).sum(axis=1)
        wts = trend * mass / mass[-1]
        counts = np.floor(N * wts / wts.sum()).astype(int)
        counts[-1] += N - counts.sum()
        cust = np.empty(N, dtype=np.int64)
        pos = 0
        for m in range(n_m):
            k = counts[m]
            pm = active[m] * w
            cust[pos:pos + k] = g.choice(C, size=k, p=pm / pm.sum())
            pos += k
        midx = np.repeat(np.arange(n_m), counts)
        mstart = pd.to_datetime(pd.Series(months))
        days_in = mstart.dt.days_in_month.to_numpy()
        hour = np.clip(g.normal(14, 4.5, N), 0, 23.999)
        occurred = (mstart.to_numpy()[midx] + pd.to_timedelta((g.random(N) * days_in[midx]).astype(int), unit="D").to_numpy()
                    + pd.to_timedelta(np.round(hour * 3600, 3), unit="s").to_numpy())
        # quantity scale: mean revenue per metered unit ~ base price, usage revenue ~ (1 - fee_share) x R
        last12 = midx >= n_m - 12
        price_mult = np.array([PRICE_MULT[t] for t in tier])
        avg_mult = float((price_mult[cust[last12]]).mean())
        q_mean = p["revenue_ttm_cents"] * (1 - p["fee_share"]) / (last12.sum() * p["base_price_cents"] * avg_mult * 1.04)
        qty = np.maximum(1, np.round(g.lognormal(math.log(q_mean) - 0.18, 0.6, N))).astype(np.int64)
        events = pd.DataFrame({
            "_cust": cust, "occurred_at": pd.to_datetime(occurred),
            "quantity": qty, "source_service": np.array(SERVICES)[np.searchsorted([0.7, 0.9], g.random(N), side="right")],
            "_flush": False,
        })
        events["recorded_at"] = events["occurred_at"] + pd.to_timedelta(np.round(g.uniform(0.15, 3.0, N), 3), unit="s")
        # --- plans & fees
        p0 = p["base_price_cents"]
        n_active_ttm = active[-12:].sum(axis=1).mean()
        fee_unit = p["fee_share"] * p["revenue_ttm_cents"] / 12 / max(1.0, float((active[-1] * np.array([FEE_MULT[t] for t in tier])).sum()) / max(1.0, active[-1].sum()) * n_active_ttm)
        plans = pd.DataFrame({
            "plan_id": [f"PLN-{t.upper()}" for t in PLAN_TIERS],
            "name": [t.capitalize() for t in PLAN_TIERS],
            "billing_interval": "month",
            "metric": p["usage_unit"],
            "price_per_unit_cents": [max(1, int(round(p0 * PRICE_MULT[t]))) for t in PLAN_TIERS],
            "platform_fee_cents": [int(round(fee_unit * FEE_MULT[t] / 100.0)) * 100 for t in PLAN_TIERS],
            "currency": "USD",
            "active": True,
        })
        contracts = pd.DataFrame({
            "contract_id": [f"CTR-{i:04d}" for i in range(1, C + 1)],
            "customer_id": cust_ids,
            "plan_id": customers["plan_id"],
            "signed_at": (created - pd.to_timedelta(g.integers(3, 30, C), unit="D")).dt.normalize(),
            "billing_terms_days": p["terms_days"],
            "backbill_window_days": np.where(tier == "enterprise", 60, np.where(g.random(C) < 0.15, 30, 90)).astype(np.int64),
            "auto_renew": True,
        })
        # --- deploys: weekly (Tue) for usage-service, daily on weekdays in the window; other services sporadic
        w_first = state.periods[-p["window_periods"]]
        daily_from = pd.Timestamp(cal.bounds(max(w_first, state.p(5)))[0])
        state.extras["daily_deploys_from"] = daily_from
        days = pd.date_range(pd.Timestamp(cal.start), pd.Timestamp(cal.end), freq="D")
        is_usage = np.where(days >= daily_from, days.dayofweek < 5, days.dayofweek == 1)
        dep_days = days[is_usage]
        dep_ts = dep_days + pd.to_timedelta(g.integers(15 * 60, 23 * 60, len(dep_days)), unit="m") + pd.to_timedelta(g.integers(0, 60, len(dep_days)), unit="s")
        other_days = days[g.random(len(days)) < 0.18]
        other_ts = other_days + pd.to_timedelta(g.integers(14 * 60, 24 * 60, len(other_days)), unit="m")
        deploys = pd.concat([
            pd.DataFrame({"deployed_at": dep_ts, "service": "usage-service", "pods_restarted": g.integers(3, 7, len(dep_ts))}),
            pd.DataFrame({"deployed_at": other_ts, "service": np.array(("billing-consumer", "web-app", "auth-service", "web-app"))[g.integers(0, 4, len(other_ts))],
                          "pods_restarted": g.integers(2, 5, len(other_ts))}),
        ], ignore_index=True).sort_values("deployed_at", kind="stable").reset_index(drop=True)
        deploys.insert(0, "deploy_id", [f"DEP-{i:05d}" for i in range(1, len(deploys) + 1)])
        deploys["version"] = [f"v{2 + i // 400}.{(i // 20) % 20}.{i % 20}" for i in range(len(deploys))]
        deploys["change_summary"] = np.array(("dependency bumps", "feature flag cleanup", "metering refactor", "perf: connection pool",
                                              "logging", "bugfix: rounding", "schema migration"))[g.integers(0, 7, len(deploys))]
        # graceful-shutdown flushes of buffered counters at usage-service deploys
        dep_u = deploys[deploys["service"] == "usage-service"]
        k = 1 + np.minimum(g.poisson(0.7, len(dep_u)), 3)
        di = np.repeat(np.arange(len(dep_u)), k)
        dts = dep_u["deployed_at"].to_numpy()[di]
        dm = np.minimum(_month_index(dts, cal.start), n_m - 1)
        fc = np.empty(len(di), dtype=np.int64)
        for m in np.unique(dm):
            sel = dm == m
            pm = active[m] * w
            fc[sel] = g.choice(C, size=int(sel.sum()), p=pm / pm.sum())
        rec = pd.to_datetime(dts) + pd.to_timedelta(np.round(g.uniform(1.0, 90.0, len(di)), 3), unit="s")
        flush = pd.DataFrame({"_cust": fc, "recorded_at": rec, "quantity": np.maximum(1, np.round(q_mean * g.uniform(0.4, 1.3, len(di)))).astype(np.int64),
                              "source_service": "api-gateway", "_flush": True})
        flush["occurred_at"] = flush["recorded_at"] - pd.to_timedelta(np.round(g.uniform(0.05, 1.5, len(di)), 3), unit="s")
        ev = pd.concat([events, flush[events.columns]], ignore_index=True)
        ev = ev[ev["recorded_at"] <= pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59)].sort_values(["recorded_at", "_cust"], kind="stable").reset_index(drop=True)
        ev["event_id"] = [f"ue_{i:08d}" for i in range(1, len(ev) + 1)]
        ev["customer_id"] = cust_ids[ev["_cust"].to_numpy()]
        ev["_month"] = _month_index(ev["occurred_at"], cal.start)
        state.frames.update({"customers": customers, "plans": plans, "contracts": contracts})
        state.extras.update({"events": ev, "deploys": deploys, "q_mean": q_mean})
        state.extras["migration_at"] = pd.Timestamp(cal.bounds(state.p(5))[0])

    # ------------------------------------------------------------------ anomalies

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Broker incidents, publish failures (base + targeted window losses) and principal phantoms."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("anomaly")
        g = rng.generator
        ev = state.extras["events"]

        mig = state.extras["migration_at"]
        n_m = len(state.extras["months"])
        win_periods = state.periods[-p["window_periods"]:]
        w0 = n_m - 3 * p["window_periods"]
        # --- broker incidents: sev2 partitions in P6..P8 (inside the window) + sev3 noise incidents
        inc_periods = [x for x in win_periods if x >= state.p(6)] or win_periods[-2:]
        rows = []
        for i in range(p["n_incidents"]):
            per = inc_periods[min(len(inc_periods) - 1, int(i * len(inc_periods) / p["n_incidents"]))]
            s, e = cal.bounds(per)
            start = pd.Timestamp(s) + pd.Timedelta(days=int(g.integers(3, (e - s).days - 4)), hours=int(g.integers(0, 24)), minutes=int(g.integers(0, 60)))
            rows.append((start, start + pd.Timedelta(hours=float(g.uniform(12, 48))), "sev2",
                         str(g.choice(["Kafka partition leader election storm", "Broker disk full on 2 of 6 brokers",
                                       "Under-replicated partitions; producer timeouts"]))))
        for _ in range(int(g.integers(2, 5))):
            per = state.periods[int(g.integers(4, 8))]
            s, e = cal.bounds(per)
            start = pd.Timestamp(s) + pd.Timedelta(days=int(g.integers(1, (e - s).days - 1)), hours=int(g.integers(0, 24)))
            rows.append((start, start + pd.Timedelta(minutes=float(g.uniform(20, 180))), "sev3",
                         str(g.choice(["Consumer group lag alert", "Schema registry slow", "Broker CPU high"]))))
        incidents = pd.DataFrame(rows, columns=["started_at", "ended_at", "severity", "title"]).sort_values("started_at", kind="stable").reset_index(drop=True)
        incidents.insert(0, "incident_id", [f"INC-{i:03d}" for i in range(1, len(incidents) + 1)])
        incidents["affected_component"] = np.where(incidents["severity"] == "sev2", "kafka-cluster-prod", "kafka-consumers")
        state.extras["incidents"] = incidents
        rec = ev["recorded_at"].to_numpy()
        in_inc = np.zeros(len(ev), dtype=bool)
        for a, b in incidents.loc[incidents["severity"] == "sev2", ["started_at", "ended_at"]].itertuples(index=False):
            in_inc |= (rec >= np.datetime64(a)) & (rec <= np.datetime64(b))
        post = (ev["occurred_at"] >= mig).to_numpy()
        # producer timeouts: base rate everywhere after go-live, incident_failure_rate inside sev2 incidents
        lost = post & (g.random(len(ev)) < np.where(in_inc, p["incident_failure_rate"], p["publish_failure_rate_base"]))
        # --- deploy-time losses fill each window month up to exactly lost_event_share of its metered units
        flush = ev["_flush"].to_numpy()
        wgt = np.where(flush, p["deploy_flush_loss_rate"], np.where(in_inc, p["incident_failure_rate"] / 4, 2e-4))
        qty = ev["quantity"].to_numpy()
        month = ev["_month"].to_numpy()
        u = g.random(len(ev))
        for m in range(max(w0, 0), n_m):
            sel = np.flatnonzero((month == m) & post)
            if not len(sel):
                continue
            need = p["lost_event_share"] * qty[sel].sum() - qty[sel][lost[sel]].sum()
            cand = sel[~lost[sel]]
            if need <= 0 or not len(cand):
                continue
            keys = np.log(u[cand]) / wgt[cand]
            order = cand[np.argsort(-keys, kind="stable")]
            cum = np.cumsum(qty[order])
            take = int(np.searchsorted(cum, need, side="left")) + 1
            # stop at whichever of the boundary picks lands closer to the target
            if take > 1 and abs(cum[take - 2] - need) < abs(cum[min(take, len(cum)) - 1] - need):
                take -= 1
            lost[order[:take]] = True
        ev["_lost"] = lost
        ev["_in_incident"] = in_inc
        state.extras["events"] = ev
        # --- phantoms (principal): batch-import publishes before commit from a P7 deploy; some transactions roll back
        phantoms = ev.iloc[0:0][["_cust", "occurred_at", "quantity", "source_service"]].copy()
        fault_at = None
        if p["ordering_fault"]:
            s7 = pd.Timestamp(cal.bounds(state.p(7))[0])
            fault_at = s7 + pd.Timedelta(days=int(g.integers(5, 25)), hours=16)
            base = ev[(ev["source_service"] == "batch-import") & (ev["occurred_at"] >= fault_at)]
            n_ph = max(4, int(round(p["phantom_rate"] * len(base))))
            pick = base.sample(n=min(n_ph, len(base)), random_state=np.random.RandomState(int(g.integers(0, 2**31 - 1))))
            phantoms = pd.DataFrame({"_cust": pick["_cust"].to_numpy(),
                                     "occurred_at": pick["occurred_at"].to_numpy() + pd.to_timedelta(g.integers(5, 600, len(pick)), unit="s").to_numpy(),
                                     "quantity": np.maximum(1, np.round(pick["quantity"].to_numpy() * g.uniform(0.6, 1.4, len(pick)))).astype(np.int64),
                                     "source_service": "batch-import"})
            phantoms = phantoms[phantoms["occurred_at"] <= pd.Timestamp(cal.end) + pd.Timedelta(hours=23)].sort_values("occurred_at", kind="stable").reset_index(drop=True)
        state.extras["phantoms"] = phantoms
        state.extras["fault_at"] = fault_at
        anomalies = [
            Anomaly(id="A1", kind="lost_events_dual_write", pillar="system_design", title="Usage events committed but never published",
                    mechanism=(f"usage-service commits usage_events then publishes in a separate call (no outbox). Base publish failure "
                               f"{p['publish_failure_rate_base']:.2%} from {mig:%Y-%m-%d}; in {', '.join(win_periods)} losses reach "
                               f"{p['lost_event_share']:.2%} of metered units per month, concentrated in deploy-time counter flushes "
                               f"(pod killed after commit, before publish) and {p['n_incidents']} sev2 broker incidents."),
                    periods=win_periods, affected_accounts=["4020", "1100"], affected_tables=["usage_events", "bus_messages", "rated_usage", "deploys", "broker_incidents"],
                    magnitude={"lost_event_share": round(p["lost_event_share"], 6), "incident_failure_rate": round(p["incident_failure_rate"], 4)},
                    severity="high"),
            Anomaly(id="A2", kind="unbilled_usage_margin_drift", pillar="accounting_finance", title="Unbilled usage: revenue short while cost is complete",
                    mechanism=("Revenue (4020) follows rated_usage while hosting cost (5000) follows usage_events, so gross margin drifts down; "
                               "no unbilled receivable (1150) is recorded and the back-billing clause limits what can be recovered."),
                    periods=list(state.periods[4:]), affected_accounts=["4020", "5000", "1150"], affected_tables=["invoice_lines", "invoices", "contracts", "gl_lines"],
                    magnitude={}, severity="high"),
        ]
        if p["ordering_fault"]:
            anomalies.append(Anomaly(
                id="A3", kind="phantom_events", pillar="sql", title="Phantom events published before a rolled-back commit",
                mechanism=(f"From {fault_at:%Y-%m-%d} batch-import publishes before COMMIT; {p['phantom_rate']:.1%} of its transactions roll back "
                           "after publishing, so bus_messages carries event_ids that never existed - customers are over-billed and the ones who "
                           "complain get credits (Dr 4100 / Cr 1100)."),
                periods=state.periods[-2:], affected_accounts=["4020", "4100", "1100"], affected_tables=["bus_messages", "credits", "support_tickets"],
                magnitude={"phantom_rate": round(p["phantom_rate"], 6)}, severity="medium"))
        return anomalies

    # ------------------------------------------------------------------ derived frames + ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Bus messages, rated usage, invoices, credits, payments, infra bills; then the GL."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        ev, ph = state.extras["events"], state.extras["phantoms"]
        customers, plans = state.frames["customers"], state.frames["plans"]
        months = state.extras["months"]
        n_m = len(months)
        mig = state.extras["migration_at"]
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        cust_ids = customers["customer_id"].to_numpy()
        price_by_plan = plans.set_index("plan_id")["price_per_unit_cents"]
        fee_by_plan = plans.set_index("plan_id")["platform_fee_cents"]
        price_c = customers["plan_id"].map(price_by_plan).to_numpy().astype(np.int64)
        fee_c = customers["plan_id"].map(fee_by_plan).to_numpy().astype(np.int64)
        # --- bus messages: published (post-migration, not lost) + phantoms
        pub = ev[(ev["occurred_at"] >= mig) & ~ev["_lost"]]
        bus = pd.DataFrame({"event_id": pub["event_id"].to_numpy(), "_cust": pub["_cust"].to_numpy(), "quantity": pub["quantity"].to_numpy(),
                            "occurred_at": pub["occurred_at"].to_numpy(),
                            "published_at": pub["recorded_at"].to_numpy() + pd.to_timedelta(np.round(g.uniform(0.004, 0.4, len(pub)), 4), unit="s").to_numpy(),
                            "_phantom": False})
        if len(ph):
            n0 = len(ev)
            bus = pd.concat([bus, pd.DataFrame({
                "event_id": [f"ue_{n0 + 1000 + i * 7:08d}" for i in range(len(ph))], "_cust": ph["_cust"].to_numpy(), "quantity": ph["quantity"].to_numpy(),
                "occurred_at": ph["occurred_at"].to_numpy(),
                "published_at": ph["occurred_at"].to_numpy() + pd.to_timedelta(np.round(g.uniform(0.002, 0.05, len(ph)), 4), unit="s").to_numpy(),
                "_phantom": True})], ignore_index=True)
        bus = bus.sort_values(["published_at", "event_id"], kind="stable").reset_index(drop=True)
        bus["occurred_at"] = pd.to_datetime(bus["occurred_at"])
        bus["published_at"] = pd.to_datetime(bus["published_at"])
        bus["customer_id"] = cust_ids[bus["_cust"].to_numpy()]
        bus["partition"] = (bus["_cust"].to_numpy() * 7) % 12
        bus["offset"] = bus.groupby("partition").cumcount().astype(np.int64) + 1000
        bus.insert(0, "message_id", np.arange(1, len(bus) + 1, dtype=np.int64))
        bus["_month"] = _month_index(bus["occurred_at"], cal.start)
        # --- rated usage: per customer x weekly window (never crossing a month end)
        occ_day = bus["occurred_at"].dt.normalize()
        bus["_wend"] = batch_end_dates(bus["occurred_at"], "weekly").to_numpy()
        bus["_wstart"] = (occ_day.dt.to_period("M").dt.to_timestamp()
                          + pd.to_timedelta(np.minimum((occ_day.dt.day.to_numpy() - 1) // 7, 3) * 7, unit="D")).to_numpy()
        ru = bus.groupby(["customer_id", "_wstart", "_wend"], sort=True).agg(quantity=("quantity", "sum"), message_count=("message_id", "size"),
                                                                             last_pub=("published_at", "max")).reset_index()
        rated = pd.DataFrame({"customer_id": ru["customer_id"], "window_start": ru["_wstart"], "window_end": ru["_wend"],
                              "metric": p["usage_unit"], "quantity": ru["quantity"].astype(np.int64), "message_count": ru["message_count"].astype(np.int64),
                              "rated_at": ru["last_pub"] + pd.to_timedelta(g.integers(2, 90, len(ru)), unit="s")})
        rated = rated.sort_values(["window_end", "customer_id"], kind="stable").reset_index(drop=True)
        rated.insert(0, "rating_id", np.arange(1, len(rated) + 1, dtype=np.int64))
        # --- billed units per customer-month: legacy (usage_events) before migration, rated_usage after
        mig_m = int(_month_index(pd.Series([mig]), cal.start)[0])
        C = len(cust_ids)
        metered = np.zeros((C, n_m), dtype=np.int64)
        np.add.at(metered, (ev["_cust"].to_numpy(), np.minimum(ev["_month"].to_numpy(), n_m - 1)), ev["quantity"].to_numpy())
        rated_units = np.zeros((C, n_m), dtype=np.int64)
        np.add.at(rated_units, (bus["_cust"].to_numpy(), np.minimum(bus["_month"].to_numpy(), n_m - 1)), bus["quantity"].to_numpy())
        billed = np.where(np.arange(n_m)[None, :] >= mig_m, rated_units, metered)
        state.extras.update({"metered": metered, "rated_units": rated_units, "billed_units": billed, "price_c": price_c, "fee_c": fee_c, "mig_m": mig_m})
        # --- invoices + lines (one per active customer-month, dated at month end)
        start_m = state.extras["cust_start_m"]
        inv_rows, line_rows = [], []
        k = 0
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0))
            for c in np.flatnonzero(start_m <= m):
                k += 1
                iid = f"INV-{k:06d}"
                usage_amt = int(billed[c, m] * price_c[c])
                inv_rows.append((iid, cust_ids[c], pd.Timestamp(ms), me, me, int(fee_c[c]) + usage_amt, c, m))
                line_rows.append((f"{iid}-1", iid, "platform_fee", 1, int(fee_c[c]), int(fee_c[c])))
                line_rows.append((f"{iid}-2", iid, "usage", int(billed[c, m]), int(price_c[c]), usage_amt))
        inv = pd.DataFrame(inv_rows, columns=["invoice_id", "customer_id", "period_start", "period_end", "invoice_date", "total_cents", "_c", "_m"])
        inv["due_date"] = inv["invoice_date"] + pd.Timedelta(days=p["terms_days"])
        inv["issued_at"] = inv["invoice_date"] + pd.Timedelta(days=1, hours=6)
        lines = pd.DataFrame(line_rows, columns=["invoice_line_id", "invoice_id", "line_type", "quantity", "unit_amount_cents", "amount_cents"])
        lines["description"] = np.where(lines["line_type"] == "usage", f"Metered {p['usage_unit']}", "Platform fee")
        # --- phantom over-billing -> disputes from complainants -> credits applied before payment
        credits = pd.DataFrame(columns=["credit_id", "customer_id", "invoice_id", "amount_cents", "issued_at", "reason"])
        tickets_dispute = pd.DataFrame(columns=["customer_id", "opened_at", "category", "subject"])
        if len(ph):
            phb = bus[bus["_phantom"]]
            over = phb.assign(amt=phb["quantity"].to_numpy() * price_c[phb["_cust"].to_numpy()]).groupby(["_cust", "_month"])["amt"].sum()
            over_cust = np.unique(over.index.get_level_values(0))
            n_comp = max(1, int(round(p["complaint_rate"] * len(over_cust) * 4)))
            # the biggest over-billed accounts notice first
            tot = over.groupby(level=0).sum().sort_values(ascending=False, kind="stable")
            comp = tot.index.to_numpy()[:min(n_comp, len(tot))]
            crow, trow = [], []
            for c in comp:
                for (cc, m), amt in over.loc[[c]].items():
                    sel = inv[(inv["_c"] == cc) & (inv["_m"] == m)]
                    if sel.empty:
                        continue
                    r = sel.iloc[0]
                    opened = r["issued_at"] + pd.Timedelta(days=int(g.integers(3, 12)), hours=int(g.integers(1, 9)))
                    issued = opened + pd.Timedelta(days=int(g.integers(2, 8)))
                    if issued > end_ts or issued >= r["due_date"]:
                        continue
                    trow.append((cust_ids[cc], opened, "billing_dispute", "Usage on our invoice is higher than our own logs"))
                    crow.append((cust_ids[cc], r["invoice_id"], int(amt), issued, "usage_overbilled"))
            if crow:
                credits = pd.DataFrame(crow, columns=["customer_id", "invoice_id", "amount_cents", "issued_at", "reason"])
                credits.insert(0, "credit_id", [f"CRD-{i:04d}" for i in range(1, len(credits) + 1)])
                tickets_dispute = pd.DataFrame(trow, columns=["customer_id", "opened_at", "category", "subject"])
        cred_by_inv = credits.groupby("invoice_id")["amount_cents"].sum() if len(credits) else pd.Series(dtype=np.int64)
        inv["credit_cents"] = inv["invoice_id"].map(cred_by_inv).fillna(0).astype(np.int64)
        delay = np.clip(np.round(g.normal(3, 6, len(inv))), -5, 40)
        paid_at = inv["due_date"] + pd.to_timedelta(delay, unit="D") + pd.to_timedelta(g.integers(8 * 3600, 18 * 3600, len(inv)), unit="s")
        paid = (paid_at <= end_ts).to_numpy() & (inv["total_cents"] > 0).to_numpy()
        inv["status"] = np.where(paid, "paid", "open")
        inv["paid_at"] = paid_at.where(paid)
        pay = inv.loc[paid, ["invoice_id", "customer_id", "paid_at"]].copy()
        pay["amount_cents"] = (inv.loc[paid, "total_cents"] - inv.loc[paid, "credit_cents"]).to_numpy()
        pay = pay.sort_values(["paid_at", "invoice_id"], kind="stable").reset_index(drop=True)
        pay.insert(0, "payment_id", [f"PAY-{i:06d}" for i in range(1, len(pay) + 1)])
        pay["method"] = np.array(("ach", "wire", "card"))[g.integers(0, 3, len(pay))]
        # --- support tickets (baseline) + disputes
        n_t = int(len(inv) * 0.06)
        ti = g.integers(0, len(inv), n_t)
        base_t = pd.DataFrame({"customer_id": inv["customer_id"].to_numpy()[ti],
                               "opened_at": inv["period_start"].to_numpy()[ti] + pd.to_timedelta(g.uniform(0, 28, n_t), unit="D").to_numpy(),
                               "category": np.array(("api_error", "how_to", "latency", "invoice_copy", "feature_request"))[g.integers(0, 5, n_t)]})
        base_t["subject"] = base_t["category"].map({"api_error": "5xx from the API", "how_to": "How do I rotate keys?", "latency": "p99 latency up",
                                                     "invoice_copy": "Need a PDF copy of the invoice", "feature_request": "Feature request"})
        tickets = pd.concat([base_t, tickets_dispute], ignore_index=True)
        tickets["opened_at"] = pd.to_datetime(tickets["opened_at"])
        tickets = tickets[tickets["opened_at"] <= end_ts].sort_values(["opened_at", "customer_id"], kind="stable").reset_index(drop=True)
        tickets.insert(0, "ticket_id", [f"TCK-{i:05d}" for i in range(1, len(tickets) + 1)])
        tickets["status"] = np.where(g.random(len(tickets)) < 0.9, "closed", "open")
        tickets["priority"] = np.where(tickets["category"] == "billing_dispute", "high", np.array(("low", "normal", "normal", "high"))[g.integers(0, 4, len(tickets))])
        # --- infrastructure cost: variable per metered unit (cloud price -2% from P6) + fixed
        units_m = metered.sum(axis=0)
        last12 = slice(n_m - 12, n_m)
        rev_ttm_est = float((billed[:, last12] * price_c[:, None]).sum() + (fee_c[:, None] * (start_m[:, None] <= np.arange(n_m)[None, last12])).sum())
        cu = 0.82 * (1 - p["gm"]) * rev_ttm_est / max(1, units_m[last12].sum())
        p6_m = int(_month_index(pd.Series([pd.Timestamp(cal.bounds(state.p(6))[0])]), cal.start)[0])
        unit_cost = np.where(np.arange(n_m) >= p6_m, cu * (1 + p["cloud_price_change"]), cu)
        fixed = np.array([cents(0.18 * (1 - p["gm"]) * rev_ttm_est / 12 * (1 + p["growth_qoq"]) ** ((m - n_m + 6) / 3.0)) for m in range(n_m)])
        hosting = np.array([cents(units_m[m] * unit_cost[m]) for m in range(n_m)])
        infra = pd.DataFrame({"month": pd.to_datetime(pd.Series(months)), "provider": "cloud", "units_metered": units_m.astype(np.int64),
                              "compute_cost_cents": hosting, "fixed_cost_cents": fixed,
                              "unit_cost_microcents": np.round(unit_cost * 1e6).astype(np.int64)})
        state.frames.update({"bus_messages": bus, "rated_usage": rated, "invoices": inv, "invoice_lines": lines, "payments": pay,
                             "credits": credits, "support_tickets": tickets, "infra_costs": infra})
        state.extras.update({"unit_cost": unit_cost, "p6_m": p6_m})
        self._post(ctx, state)

    def _post(self, ctx: BuildContext, state: WorldState) -> None:
        """Opening balances, month-end invoice batches, collections, credits, infra bills and the monthly close."""
        p, cal = state.params, state.calendar
        L = state.ledger
        gran = GL_BATCH[p["difficulty"]]
        months = state.extras["months"]
        inv, lines, pay, credits, infra = (state.frames[k] for k in ("invoices", "invoice_lines", "payments", "credits", "infra_costs"))
        R = p["revenue_ttm_cents"]
        end_date = cal.end
        ppe = cents(R * p["capex_pct"] * 5 * 0.55)
        accdep = cents(ppe * 0.45)
        cash0 = cents(R * 0.22)
        cs = cents(0.6 * (cash0 + ppe - accdep))
        L.open_books(cal.opening_date, {"1000": cash0, "1500": ppe, "1510": -accdep, "3000": -cs, "3100": -(cash0 + ppe - accdep - cs)})
        events: list[tuple[dt.date, int, Any]] = []
        lt = lines.merge(inv[["invoice_id", "invoice_date"]], on="invoice_id")
        fee_m = lt[lt["line_type"] == "platform_fee"].groupby("invoice_date")["amount_cents"].sum()
        use_m = lt[lt["line_type"] == "usage"].groupby("invoice_date")["amount_cents"].sum()
        n_inv = inv.groupby("invoice_date").size()
        for d in sorted(set(fee_m.index) | set(use_m.index)):
            f, u = int(fee_m.get(d, 0)), int(use_m.get(d, 0))
            dim = {"batch": f"billing_run:{d.date()}", "rows": str(int(n_inv.get(d, 0)))}
            events.append((d.date(), 3, lambda dd, a=f, b=u, dm=dim: L.adjust(dd, [("1100", a + b, 0, dm), ("4000", 0, a, dm), ("4020", 0, b, dm)],
                                                                             "Monthly billing run: platform fees + rated usage", "sales")))
        if len(pay):
            pb = pay.assign(b=batch_end_dates(pay["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("payment_id", "size"))
            for b, r in pb.iterrows():
                events.append((b.date(), 4, lambda dd, a=int(r.a), dm={"batch": f"collections:{b.date()}", "rows": str(int(r.n))}: L.collect_cash(dd, a, memo="Customer payments (batch)", dim=dm)))
        for r in credits.itertuples(index=False):
            events.append((r.issued_at.date(), 4, lambda dd, a=int(r.amount_cents), dm={"credit_id": r.credit_id, "customer_id": r.customer_id}:
                           L.adjust(dd, [("4100", a, 0, dm), ("1100", 0, a, dm)], "Credit memo: usage over-billed", "credits")))
        rev_m = (inv.groupby("_m")["total_cents"].sum()).reindex(range(len(months)), fill_value=0).to_numpy()
        prev = {"payroll": 0, "opex": 0, "infra": 0}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            run_rate = float(rev_m[max(0, m - 2): m + 1].mean()) or R / 12
            opex = cents(run_rate * max(0.15, p["gm"] - p["op_margin"]))
            payroll = cents(opex * 0.65)
            other = opex - payroll
            host, fixed = int(infra["compute_cost_cents"].iloc[m]), int(infra["fixed_cost_cents"].iloc[m])
            if m > 0:
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["opex"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex paid", source="opex")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["infra"]: L.pay_ap(d, a, memo="Cloud provider invoice paid", source="payables")))
            pr = dict(zip(("6000", "6100", "6200"), split_cents(payroll, [0.3, 0.5, 0.2]), strict=True))
            ox = dict(zip(("6000", "6200", "6400"), split_cents(other, [0.4, 0.35, 0.25]), strict=True))
            events.append((me, 5, lambda d, a=host, b=fixed, mm=f"{ms:%Y-%m}": L.adjust(
                d, [("5000", a, 0, {"month": mm}), ("5020", b, 0, {"month": mm}), ("2000", 0, a + b, {"month": mm})], "Cloud hosting: metered compute + fixed infrastructure", "infrastructure")))
            events.append((me, 7, lambda d, s=pr: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox: L.accrue_opex(d, s)))
            events.append((ms + dt.timedelta(days=14), 6, lambda d, a=cents(run_rate * p["capex_pct"]): L.capex(d, a)))
            events.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 60))))
            if ms.month % 3 == 0:
                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))
                events.append((me, 11, tax))
            if ms.month in (1, 4, 7, 10) and m > 0:
                events.append((ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"])))
            prev = {"payroll": payroll, "opex": other, "infra": host + fixed}
        events.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in events:
            if d <= end_date:
                fn(d)

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Source, bus, rating, billing, ops and support tables + 2/4/6 dirt rules."""
        p = state.params
        g = ctx.rng.spawn("tables").generator
        ev = state.extras["events"]
        usage = pd.DataFrame({"event_id": ev["event_id"], "customer_id": ev["customer_id"], "metric": p["usage_unit"],
                              "quantity": ev["quantity"].astype(np.int64), "occurred_at": ev["occurred_at"], "recorded_at": ev["recorded_at"],
                              "source_service": ev["source_service"], "ingest_id": np.ones(len(ev), dtype=np.int64)})
        bus = state.frames["bus_messages"]
        bus_out = bus[["message_id", "event_id", "customer_id", "quantity", "occurred_at", "published_at", "partition", "offset"]].copy()
        bus_out.insert(4, "metric", p["usage_unit"])
        bus_out["topic"] = "usage.metered.v1"
        inv = state.frames["invoices"]
        invoices = inv[["invoice_id", "customer_id", "period_start", "period_end", "invoice_date", "due_date", "issued_at", "total_cents",
                        "credit_cents", "status", "paid_at"]].copy()
        invoices.insert(7, "currency", "USD")
        deploys = state.extras["deploys"][["deploy_id", "service", "version", "deployed_at", "pods_restarted", "change_summary"]]
        incidents = state.extras["incidents"]
        dirt_plan = [
            ("support_tickets", "case_mixed:category"), ("customers", "whitespace:name"),
            ("customers", "nulls:billing_email:0.06"), ("usage_events", "dup_keys:0.01"),
            ("deploys", "case_mixed:service"), ("usage_events", "tz_mixed:recorded_at"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        state.extras["tz_dirty"] = "tz_mixed:recorded_at" in dirt.get("usage_events", [])
        state.extras["deploy_case_dirty"] = "case_mixed:service" in dirt.get("deploys", [])
        money = {c: "BIGINT" for c in ("amount_cents", "total_cents", "credit_cents", "unit_amount_cents", "price_per_unit_cents",
                                         "platform_fee_cents", "compute_cost_cents", "fixed_cost_cents", "unit_cost_microcents",
                                         "units_metered", "quantity", "message_count", "offset", "message_id", "rating_id")}
        builder.add_frame("customers", state.frames["customers"], primary_key=["customer_id"], foreign_keys=[("plan_id", "plans", "plan_id")],
                          dirt=dirt.get("customers", ()), column_types={"created_at": "TIMESTAMP"},
                          descriptions={"plan_id": "current usage plan (price per unit and platform fee)"}, description="Business customers.")
        builder.add_frame("plans", state.frames["plans"], primary_key=["plan_id"], column_types=money,
                          descriptions={"price_per_unit_cents": f"price per metered {p['usage_unit']} unit", "platform_fee_cents": "monthly platform fee"})
        builder.add_frame("contracts", state.frames["contracts"], primary_key=["contract_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types={"signed_at": "DATE", "billing_terms_days": "INTEGER", "backbill_window_days": "INTEGER"},
                          descriptions={"backbill_window_days": "usage may be invoiced retroactively only for invoices issued within this many days"},
                          description="Customer contracts (MSA terms).")
        builder.add_frame("usage_events", usage, primary_key=["event_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types=money, dirt=dirt.get("usage_events", ()),
                          descriptions={"occurred_at": "end of the metering interval (UTC)", "recorded_at": "commit time in Postgres (UTC)",
                                        "quantity": f"metered {p['usage_unit']}", "ingest_id": ">1 marks a re-ingested copy of the same event_id (warehouse load)"},
                          description="Source of truth: metering records committed by usage-service (Postgres).")
        builder.add_frame("bus_messages", bus_out, primary_key=["message_id"], foreign_keys=[("event_id", "usage_events", "event_id")],
                          column_types=money, descriptions={"published_at": "broker ack time", "event_id": "id carried in the message payload"},
                          description="What the Kafka topic actually received (exported from the broker since the event pipeline went live).")
        builder.add_frame("rated_usage", state.frames["rated_usage"], primary_key=["rating_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types={**money, "window_start": "DATE", "window_end": "DATE"},
                          descriptions={"window_end": "rating window end (windows never cross a month end)"},
                          description="Rating consumer output aggregated from bus_messages; the billing job invoices from here.")
        builder.add_frame("invoices", invoices, primary_key=["invoice_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types={**money, "period_start": "DATE", "period_end": "DATE", "invoice_date": "DATE", "due_date": "DATE"},
                          descriptions={"credit_cents": "credit memos applied to this invoice", "total_cents": "fees + usage before credits"},
                          description="Monthly invoices (AR sub-ledger).")
        builder.add_frame("invoice_lines", state.frames["invoice_lines"], primary_key=["invoice_line_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id")],
                          column_types=money, descriptions={"line_type": "platform_fee | usage"})
        builder.add_frame("payments", state.frames["payments"], primary_key=["payment_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id")],
                          column_types=money, description="Customer payments (net of credits).")
        if len(state.frames["credits"]):
            builder.add_frame("credits", state.frames["credits"], primary_key=["credit_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id")],
                              column_types=money, description="Credit memos issued after billing disputes.")
        builder.add_frame("deploys", deploys, primary_key=["deploy_id"], dirt=dirt.get("deploys", ()), column_types={"pods_restarted": "INTEGER"},
                          descriptions={"deployed_at": "rollout start (UTC); pods drain for up to 90 s"}, description="Production deploys (CI/CD log).")
        builder.add_frame("broker_incidents", incidents, primary_key=["incident_id"], description="Kafka incidents from the on-call tool.")
        builder.add_frame("support_tickets", state.frames["support_tickets"], primary_key=["ticket_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          dirt=dirt.get("support_tickets", ()), description="Customer support tickets.")
        builder.add_frame("infra_costs", state.frames["infra_costs"], primary_key=["month"], column_types={**money, "month": "DATE"},
                          descriptions={"unit_cost_microcents": "compute cost per metered unit in 1e-6 cents"},
                          description="Monthly cloud bill (compute scales with metered usage).")
        if p["ordering_fault"]:
            customers = state.frames["customers"]
            n = len(customers)
            ob = pd.DataFrame({
                "outbox_id": np.arange(1, 2 * n + 1, dtype=np.int64),
                "event_id": [f"cust_evt_{i:05d}" for i in range(1, 2 * n + 1)],
                "aggregate_type": "customer", "event_type": np.tile(["customer.created", "plan.assigned"], n),
                "created_at": np.repeat(customers["created_at"].to_numpy(), 2) + pd.to_timedelta(np.tile([0, 5], n), unit="s").to_numpy(),
            })
            ob["published_at"] = ob["created_at"] + pd.to_timedelta(g.integers(200, 2000, len(ob)), unit="ms")
            ob["publish_attempts"] = 1
            ob["relay_worker"] = "outbox-relay-crm"
            builder.add_frame("outbox", ob, primary_key=["outbox_id"], column_types={"publish_attempts": "INTEGER"},
                              description="Transactional outbox of the customer-lifecycle service (crm-service).")

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, AP and usage revenue tie; metered usage vs billed usage breaks by design."""
        cal = state.calendar
        inv, lines, pay, credits = (state.frames[k] for k in ("invoices", "invoice_lines", "payments", "credits"))
        infra = state.frames["infra_costs"]
        ev, bus = state.extras["events"], state.frames["bus_messages"]
        price_c = state.extras["price_c"]
        ev_val = ev["quantity"].to_numpy() * price_c[ev["_cust"].to_numpy()]
        ev_per = cal.labels(ev["occurred_at"])
        lost_val = np.where(ev["_lost"].to_numpy() & (ev["occurred_at"] >= state.extras["migration_at"]).to_numpy(), ev_val, 0)
        bus_per = cal.labels(bus["occurred_at"])
        ph_val = np.where(bus["_phantom"].to_numpy(), bus["quantity"].to_numpy() * price_c[bus["_cust"].to_numpy()], 0)
        lu = lines[lines["line_type"] == "usage"].merge(inv[["invoice_id", "invoice_date"]], on="invoice_id")
        ar, ap, rev, metered, brk = {}, {}, {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            s_ts, e_ts = pd.Timestamp(s), pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            issued = inv["invoice_date"] <= e_ts
            paid_by = pay[pay["paid_at"] <= e_ts].groupby("invoice_id")["amount_cents"].sum()
            cr_by = credits[credits["issued_at"] <= e_ts].groupby("invoice_id")["amount_cents"].sum() if len(credits) else pd.Series(dtype=np.int64)
            sub = inv.loc[issued]
            ar[period] = int((sub["total_cents"] - sub["invoice_id"].map(paid_by).fillna(0) - sub["invoice_id"].map(cr_by).fillna(0)).sum())
            mstart = pd.to_datetime(infra["month"])
            booked = mstart <= e_ts
            # the bill for month m is paid on the 10th of month m+1
            paid_bill = (mstart + pd.offsets.MonthBegin(1) + pd.Timedelta(days=9)) <= e_ts
            ap[period] = int((infra["compute_cost_cents"] + infra["fixed_cost_cents"])[booked & ~paid_bill].sum())
            rev[period] = int(lu.loc[(lu["invoice_date"] >= s_ts) & (lu["invoice_date"] <= e_ts), "amount_cents"].sum())
            metered[period] = int(ev_val[ev_per == period].sum())
            brk[period] = int(lost_val[ev_per == period].sum() - ph_val[bus_per == period].sum())
        state.extras["gap_by_period"] = brk
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Invoices less payments and credits == GL 1100"),
            SubledgerTie("ap_cloud_bills", ap, ["2000"], description="Unpaid monthly cloud bills == GL 2000"),
            SubledgerTie("usage_revenue_invoice_lines", rev, ["4020"], basis="activity", description="Usage invoice lines in period == GL 4020 activity"),
            SubledgerTie("usage_events_vs_billed_usage", metered, ["4020"], basis="activity", expected_break_cents=brk,
                         description="Metered usage (usage_events x plan price) vs billed usage revenue: lost minus phantom dollars (designed break)"),
        ]

    # ------------------------------------------------------------------ figures

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        p, cal = state.params, state.calendar
        ev, bus = state.extras["events"], state.frames["bus_messages"]
        price_c = state.extras["price_c"]
        mig = state.extras["migration_at"]
        lost = ev[ev["_lost"] & (ev["occurred_at"] >= mig)]
        lost_val = lost["quantity"].to_numpy() * price_c[lost["_cust"].to_numpy()]
        win = state.periods[-p["window_periods"]:]
        ev_per = cal.labels(ev["occurred_at"])
        in_win = np.isin(ev_per, win) & (ev["occurred_at"] >= mig).to_numpy()
        lost_win_units = int(ev.loc[in_win & ev["_lost"].to_numpy(), "quantity"].sum())
        win_units = int(ev.loc[in_win, "quantity"].sum())
        ph = bus[bus["_phantom"]]
        ph_val = ph["quantity"].to_numpy() * price_c[ph["_cust"].to_numpy()]
        # back-billing: lost usage whose invoice (month end) was issued within the contract window of the as-of date
        asof = pd.Timestamp(cal.end)
        win_days = state.frames["contracts"].set_index("customer_id")["backbill_window_days"]
        cust_win = win_days.reindex(state.frames["customers"]["customer_id"]).to_numpy()
        inv_date = (lost["occurred_at"] + pd.offsets.MonthEnd(0)).dt.normalize()
        ok = (inv_date >= asof - pd.to_timedelta(cust_win[lost["_cust"].to_numpy()], unit="D")).to_numpy()
        f = {
            "lost_count": int(len(lost)), "lost_units": int(lost["quantity"].sum()), "unbilled": int(lost_val.sum()),
            "lost_share_window": lost_win_units / max(1, win_units), "win_units": win_units,
            "phantom_count": int(len(ph)), "overbilled": int(ph_val.sum()),
            "recoverable": int(lost_val[ok].sum()),
            "credits": int(state.frames["credits"]["amount_cents"].sum()) if len(state.frames["credits"]) else 0,
            "lost_flush": int(lost["_flush"].sum()), "lost_incident": int(lost["_in_incident"].sum()),
        }
        st = state.statements
        inc = {i.period: i for i in st.income}  # type: ignore[union-attr]
        rev = sum(inc[x].revenue_cents for x in win)
        cogs = sum(inc[x].revenue_cents - inc[x].gross_profit_cents for x in win)
        gap = sum(state.extras["gap_by_period"][x] for x in win)
        f["gm_actual_window"] = 1 - cogs / rev
        f["gm_if_billed_window"] = 1 - cogs / (rev + gap)
        f["gm_drift_pts"] = 100 * (f["gm_actual_window"] - f["gm_if_billed_window"])
        f["gap_window"] = gap
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Pipeline queueing, unit economics and KPIs by period."""
        cal = state.calendar
        st = state.statements
        ev, bus = state.extras["events"], state.frames["bus_messages"]
        ev_per = cal.labels(ev["occurred_at"])
        bus_per = cal.labels(bus["occurred_at"])
        kpis = {}
        for period in state.periods:
            inc = next(i for i in st.income if i.period == period)  # type: ignore[union-attr]
            sel = ev_per == period
            kpis[period] = {
                "revenue_cents": float(inc.revenue_cents), "gross_margin": st.ratios[period]["gross_margin"],  # type: ignore[union-attr]
                "net_income_cents": float(inc.net_income_cents), "metered_units": float(ev.loc[sel, "quantity"].sum()),
                "rated_units": float(bus.loc[bus_per == period, "quantity"].sum()), "usage_events": float(sel.sum()),
                "cash_cents": float(state.ledger.trial_balance(period)["1000"]),
            }
        lam = len(ev) / (len(state.extras["months"]) * 30.4 * 86400)
        stations, summary = station_network(lam * 3600, [
            {"name": "usage_service_commit", "capacity_per_hour": lam * 3600 / 0.35, "cs": 0.6},
            {"name": "kafka_publish", "capacity_per_hour": lam * 3600 / 0.2, "cs": 0.8},
            {"name": "rating_consumer", "capacity_per_hour": lam * 3600 / 0.45, "cs": 1.0},
        ])
        q = {**mm1(lam * 3600, lam * 3600 / 0.45), "throughput": summary["throughput"]}
        inc8 = next(i for i in st.income if i.period == state.periods[-1])  # type: ignore[union-attr]
        unit = {"gross_margin": st.ratios[state.periods[-1]]["gross_margin"], "arpa": inc8.revenue_cents / max(1, len(state.frames["customers"])) / 3,  # type: ignore[union-attr]
                "cac": 0.0, "ltv": 0.0, "payback_months": 0.0, "churn_monthly": 0.0,
                "infra_cost_per_unit_cents": float(state.extras["unit_cost"][-1])}
        return build_operations_state(stations=stations, queueing=q, unit_economics=unit,
                                      supply_chain={"event_pipeline": {"delivery": "dual write (commit, then publish)", "broker": "kafka",
                                                                       "topic": "usage.metered.v1", "partitions": 12}},
                                      pricing={"plans": state.frames["plans"][["plan_id", "price_per_unit_cents", "platform_fee_cents"]].to_dict(orient="records")},
                                      kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Metrics of the section 10 key table, reference SQL (run on the dirty DB) and the outbox design."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        P = state.periods
        P8 = P[-1]
        mig = state.extras["migration_at"]
        win = P[-p["window_periods"]:]
        tz = state.extras["tz_dirty"]
        svc = "LOWER(TRIM(service))" if state.extras["deploy_case_dirty"] else "service"
        rec = _utc("recorded_at", tz)
        dedupe = "SELECT * FROM usage_events QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY ingest_id) = 1"
        sql_lost = f"""
WITH ue AS ({dedupe})
SELECT date_trunc('month', u.occurred_at) AS month,
       COUNT(*) AS lost_events,
       SUM(u.quantity) AS lost_units,
       SUM(u.quantity * pl.price_per_unit_cents) AS unbilled_cents
FROM ue u
LEFT JOIN bus_messages b ON b.event_id = u.event_id
JOIN customers c ON c.customer_id = u.customer_id
JOIN plans pl ON pl.plan_id = c.plan_id
WHERE b.event_id IS NULL
  AND u.occurred_at >= TIMESTAMP '{mig:%Y-%m-%d %H:%M:%S}'   -- the event pipeline went live
GROUP BY 1
ORDER BY 1"""
        win_months = [pd.Timestamp(m) for m in state.extras["months"][-3 * p["window_periods"]:]]
        q_lost = run_reference_query(con, "lost_events_by_month", sql_lost, signature=["qualify", "left join", "group by", "date_trunc("],
                                     metric_keys=["lost_event_count", "lost_units", "unbilled_revenue_cents"],
                                     checks=lambda df: {"lost_events": int(df["lost_events"].sum()), "lost_units": int(df["lost_units"].sum()),
                                                        "unbilled_cents": int(df["unbilled_cents"].sum()),
                                                        "lost_units_in_window": int(df.loc[pd.to_datetime(df["month"]) >= win_months[0], "lost_units"].sum())})
        lost_cte = f"""ue AS (
  SELECT event_id, customer_id, occurred_at, quantity, {rec} AS recorded_utc FROM usage_events
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY ingest_id) = 1),
lost AS (
  SELECT ue.* FROM ue LEFT JOIN bus_messages b ON b.event_id = ue.event_id
  WHERE b.event_id IS NULL AND ue.occurred_at >= TIMESTAMP '{mig:%Y-%m-%d %H:%M:%S}')"""
        sql_dep = f"""
WITH {lost_cte},
dep AS (SELECT deploy_id, deployed_at FROM deploys WHERE {svc} = 'usage-service')
SELECT d.deploy_id, d.deployed_at, COUNT(l.event_id) AS lost_near_deploy, SUM(l.quantity) AS lost_units
FROM dep d
JOIN lost l ON l.recorded_utc BETWEEN d.deployed_at - INTERVAL 2 MINUTE AND d.deployed_at + INTERVAL 2 MINUTE
GROUP BY 1, 2
ORDER BY 2, 1"""
        q_dep = run_reference_query(con, "lost_events_near_deploys", sql_dep, signature=["with", "left join", "join", "group by", "count("],
                                    metric_keys=["loss_share_in_incidents_vs_deploys"],
                                    checks=lambda df: {"deploys_with_losses": int(len(df)), "lost_near_deploys": int(df["lost_near_deploy"].sum())})
        sql_cause = f"""
WITH {lost_cte},
tagged AS (
  SELECT l.event_id,
         EXISTS (SELECT 1 FROM broker_incidents i
                 WHERE l.recorded_utc BETWEEN i.started_at AND i.ended_at) AS in_incident,
         EXISTS (SELECT 1 FROM deploys d
                 WHERE {svc.replace('service', 'd.service')} = 'usage-service'
                   AND l.recorded_utc BETWEEN d.deployed_at - INTERVAL 2 MINUTE AND d.deployed_at + INTERVAL 2 MINUTE) AS near_deploy
  FROM lost l)
SELECT CASE WHEN in_incident THEN 'broker_incident'
            WHEN near_deploy THEN 'deploy_restart'
            ELSE 'background' END AS cause,
       COUNT(*) AS lost_events
FROM tagged
GROUP BY 1
ORDER BY 2 DESC, 1"""
        cause_df = con.execute(sql_cause).fetchdf().set_index("cause")["lost_events"]
        n_inc, n_dep = int(cause_df.get("broker_incident", 0)), int(cause_df.get("deploy_restart", 0))
        inc_share = n_inc / max(1, n_inc + n_dep)
        q_cause = run_reference_query(con, "lost_events_by_cause", sql_cause, signature=["with", "case when", "group by", "count("],
                                      metric_keys=["loss_share_in_incidents_vs_deploys"],
                                      checks=lambda df: {"incident_share_of_incident_plus_deploy": inc_share})
        sql_gap = f"""
WITH ue AS ({dedupe}),
metered AS (
  SELECT period_of(u.occurred_at) AS period, SUM(u.quantity * pl.price_per_unit_cents) AS metered_cents
  FROM ue u JOIN customers c USING (customer_id) JOIN plans pl ON pl.plan_id = c.plan_id
  GROUP BY 1),
billed AS (
  SELECT period_of(i.period_end) AS period, SUM(l.amount_cents) AS billed_usage_cents
  FROM invoice_lines l JOIN invoices i USING (invoice_id)
  WHERE l.line_type = 'usage'
  GROUP BY 1)
SELECT period, metered_cents, billed_usage_cents, metered_cents - billed_usage_cents AS gap_cents
FROM metered JOIN billed USING (period)
ORDER BY period"""
        q_gap = run_reference_query(con, "metered_vs_billed_usage_by_quarter", sql_gap, signature=["with", "join", "group by", "sum("],
                                    metric_keys=["usage_vs_billed_gap_cents_p8", "gm_drift_pts"],
                                    checks=lambda df: {"gap_last_quarter": int(df["gap_cents"].iloc[-1]),
                                                       "gap_window": int(df.loc[df["period"].isin(win), "gap_cents"].sum())})
        rev_codes = "('4000', '4010', '4020', '4100', '4110')"
        sql_gm = f"""
SELECT j.period,
       SUM(CASE WHEN l.account_code IN {rev_codes} THEN l.credit_cents - l.debit_cents ELSE 0 END) AS revenue_cents,
       SUM(CASE WHEN l.account_code IN ('5000', '5010', '5020', '5030', '5040', '5050') THEN l.debit_cents - l.credit_cents ELSE 0 END) AS cogs_cents,
       SUM(CASE WHEN l.account_code = '5000' THEN l.debit_cents ELSE 0 END) AS metered_compute_cents,
       1 - SUM(CASE WHEN l.account_code IN ('5000', '5010', '5020', '5030', '5040', '5050') THEN l.debit_cents - l.credit_cents ELSE 0 END) * 1.0
         / SUM(CASE WHEN l.account_code IN {rev_codes} THEN l.credit_cents - l.debit_cents ELSE 0 END) AS gross_margin
FROM gl_lines l JOIN gl_journal j USING (entry_id)
WHERE j.period >= '{P[0]}'
GROUP BY 1
ORDER BY 1"""
        q_gm = run_reference_query(con, "gross_margin_by_quarter_gl", sql_gm, signature=["join", "group by", "case when", "sum("],
                                   metric_keys=["gm_drift_pts"], checks=lambda df: {"gm_last": float(df["gross_margin"].iloc[-1])})
        asof = cal.end
        sql_bb = f"""
WITH {lost_cte}
SELECT k.customer_id, k.backbill_window_days,
       SUM(l.quantity * pl.price_per_unit_cents) AS unbilled_cents,
       COALESCE(SUM(l.quantity * pl.price_per_unit_cents)
                FILTER (WHERE last_day(CAST(l.occurred_at AS DATE)) >= DATE '{asof}' - k.backbill_window_days), 0) AS recoverable_cents
FROM lost l
JOIN contracts k ON k.customer_id = l.customer_id
JOIN customers c ON c.customer_id = l.customer_id
JOIN plans pl ON pl.plan_id = c.plan_id
GROUP BY 1, 2
ORDER BY recoverable_cents DESC, 1"""
        q_bb = run_reference_query(con, "recoverable_backbill_by_customer", sql_bb, signature=["with", "left join", "join", "group by", "filter("],
                                   metric_keys=["recoverable_backbill_cents"],
                                   checks=lambda df: {"recoverable_cents": int(df["recoverable_cents"].sum()), "unbilled_cents": int(df["unbilled_cents"].sum())})
        gap8 = int(state.extras["gap_by_period"][P8])
        metrics = [
            metric("lost_event_count", f["lost_count"], "count",
                   f"usage_events (deduplicated on event_id, lowest ingest_id) since the pipeline went live ({mig:%Y-%m-%d}) with no bus_messages row (anti-join).",
                   pillar="sql", tol_rel=0.03, label="Lost events", hint="Anti-join the source table to what the broker received."),
            metric("lost_units", f["lost_units"], "units", "Sum of quantity over those lost events.", pillar="sql", tol_rel=0.03, label="Lost metered units", hint="Sum the quantity of the events that never reached the bus."),
            metric("unbilled_revenue_cents", f["unbilled"], "cents", "Sum of quantity x plan price_per_unit_cents over the lost events.",
                   pillar="accounting_finance", tol_rel=0.03, label="Unbilled usage revenue", hint="Price each lost unit at the customer's plan price."),
            metric("lost_unit_share_window", f["lost_share_window"], "ratio",
                   f"Lost units / metered units for usage in {win[0]}..{win[-1]} (post-migration).", pillar="sql", tol_abs=0.003,
                   label="Share of metered usage lost (anomaly window)", hint="Divide lost units by all metered units in the recent quarters."),
            metric("loss_share_in_incidents_vs_deploys", inc_share, "ratio",
                   f"Lost events recorded inside broker_incidents windows / (inside incidents + within +/-2 min of a usage-service deploy); "
                   f"{n_inc} vs {n_dep} events - most loss is at deploys, which no broker upgrade fixes.",
                   pillar="system_design", tol_abs=0.08, label="Incident share of attributable losses", hint="Tag each lost event: inside a broker incident, near a deploy, or neither."),
            metric("gm_drift_pts", f["gm_drift_pts"], "pts",
                   f"100 x (GM actual - GM had metered usage been billed) over {win[0]}..{win[-1]}: GM from the income statements, "
                   f"counterfactual revenue = revenue + metered-minus-billed gap ({f['gap_window']} cents).",
                   pillar="accounting_finance", tol_abs=0.15, label="Gross-margin drift caused by unbilled usage", hint="Recompute gross margin as if the metered-but-unbilled usage had been invoiced."),
            metric("usage_vs_billed_gap_cents_p8", gap8, "cents",
                   f"{P8}: metered usage value (usage_events x plan price) minus billed usage invoice lines (= GL 4020).",
                   pillar="sql", tol_rel=0.03, label="Metered vs billed usage gap, latest quarter", hint="Reconcile metered usage value to billed usage lines by quarter."),
            metric("recoverable_backbill_cents", f["recoverable"], "cents",
                   f"Unbilled value of lost usage whose invoice (usage month end) falls within each contract's backbill_window_days of {asof}.",
                   pillar="accounting_finance", tol_rel=0.08, label="Recoverable by back-billing", hint="Contracts limit how far back you may invoice."),
        ]
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_lost, q_dep, q_cause],
            "magnitude": {**anomalies["A1"].magnitude, "lost_event_count": float(f["lost_count"]), "lost_units": float(f["lost_units"]),
                          "lost_unit_share_window": round(f["lost_share_window"], 6), "loss_share_in_incidents_vs_deploys": round(inc_share, 6)},
            "evidence_trail": [f"{f['lost_count']} usage_events never reached the bus ({f['lost_units']} units).",
                               f"{n_dep} lost events sit within 2 minutes of usage-service deploys (counter flush on shutdown), {n_inc} inside sev2 broker incidents.",
                               "Losses start exactly when billing moved to the event pipeline; legacy months tie to the cent."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_gap, q_gm, q_bb],
            "magnitude": {"unbilled_revenue_cents": float(f["unbilled"]), "gm_drift_pts": round(f["gm_drift_pts"], 6),
                          "recoverable_backbill_cents": float(f["recoverable"]),
                          **{f"gap_{k}": float(v) for k, v in state.extras["gap_by_period"].items()}},
            "evidence_trail": ["Compute per metered unit is flat (cloud price even fell 2%), so margin moved through the revenue line.",
                               f"Metered minus billed usage = {gap8} cents in {P8}; zero before the migration.",
                               f"Within the back-billing windows {f['recoverable']} cents are recoverable; the rest is a write-off."]})
        if "A3" in anomalies:
            sql_ph = f"""
WITH ue AS ({dedupe})
SELECT b.customer_id, COUNT(*) AS phantom_events, SUM(b.quantity) AS phantom_units,
       SUM(b.quantity * pl.price_per_unit_cents) AS overbilled_cents
FROM bus_messages b
LEFT JOIN ue u ON u.event_id = b.event_id
JOIN customers c ON c.customer_id = b.customer_id
JOIN plans pl ON pl.plan_id = c.plan_id
WHERE u.event_id IS NULL
GROUP BY 1
ORDER BY overbilled_cents DESC, 1"""
            q_ph = run_reference_query(con, "phantom_events_by_customer", sql_ph, signature=["qualify", "left join", "group by", "count("],
                                       metric_keys=["phantom_event_count", "overbilled_cents"],
                                       checks=lambda df: {"phantom_events": int(df["phantom_events"].sum()), "overbilled_cents": int(df["overbilled_cents"].sum())})
            sql_cr = """
SELECT cr.customer_id, COUNT(DISTINCT t.ticket_id) AS billing_disputes, SUM(cr.amount_cents) AS credited_cents,
       MIN(t.opened_at) AS first_dispute, MAX(cr.issued_at) AS last_credit
FROM credits cr
JOIN support_tickets t ON t.customer_id = cr.customer_id AND LOWER(t.category) = 'billing_dispute'
GROUP BY 1
ORDER BY credited_cents DESC, 1"""
            q_cr = run_reference_query(con, "credits_after_billing_disputes", sql_cr, signature=["join", "group by", "count(", "distinct"],
                                       metric_keys=["overbilled_cents"], checks=lambda df: {"customers_credited": int(len(df))})
            metrics += [
                metric("phantom_event_count", f["phantom_count"], "count", "bus_messages whose event_id does not exist in usage_events (reverse anti-join).",
                       pillar="sql", tol_rel=0.05, label="Phantom events", hint="Anti-join in the other direction: messages with no source row."),
                metric("overbilled_cents", f["overbilled"], "cents", "Sum of quantity x plan price over phantom messages.",
                       pillar="accounting_finance", tol_rel=0.05, label="Over-billed amount", hint="Price the phantom messages at plan prices."),
            ]
            anomalies["A3"] = anomalies["A3"].model_copy(update={
                "reference_sql": [q_ph, q_cr],
                "magnitude": {**anomalies["A3"].magnitude, "phantom_event_count": float(f["phantom_count"]), "overbilled_cents": float(f["overbilled"]),
                              "credits_cents": float(f["credits"])},
                "evidence_trail": [f"{f['phantom_count']} bus messages reference event_ids that were never committed (all from batch-import).",
                                   f"Only the complaining customers were credited ({f['credits']} cents); the rest are still over-billed."]})
        herrings_all = [
            "Customers are using less - usage_events keep growing quarter over quarter; only rated_usage dips.",
            f"Infra cost inflation hurt margin - compute per metered unit is flat and the cloud price changed {p['cloud_price_change']:+.0%} in {P[5]}.",
            "Broker outages lost the messages, buy a bigger Kafka cluster - most losses sit next to usage-service deploys with no broker incident.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        root = (f"Since {mig:%Y-%m-%d} usage-service commits each metering record to Postgres and then publishes it to Kafka in a separate "
                f"call - a dual write with no transactional outbox. Whenever the process dies between the two (deploy-time counter flushes "
                f"killed after commit, producer timeouts during broker incidents) the event is lost: {f['lost_count']} events, "
                f"{f['lost_units']} units, {f['unbilled'] / 100:,.0f} USD never billed ({f['lost_share_window']:.2%} of metered usage in "
                f"{win[0]}-{win[-1]}). Hosting cost follows real usage, so gross margin is {abs(f['gm_drift_pts']):.2f} pts lower than it "
                f"should be; nothing downstream noticed because invoices tie to rated_usage. "
                + (f"A later change on batch-import publishes before commit, so {f['phantom_count']} rolled-back events were billed anyway "
                   f"({f['overbilled'] / 100:,.0f} USD over-billed). " if p["ordering_fault"] else "")
                + f"Fix: write usage_events and an outbox row in one transaction, relay with polling or CDC, make the rating consumer idempotent "
                f"on event_id, reconcile daily, and back-bill the {f['recoverable'] / 100:,.0f} USD still inside contract windows.")
        python_ref = (
            "import pandas as pd\n"
            "ue = (tables['usage_events'].sort_values('ingest_id').drop_duplicates('event_id'))\n"
            "bus = tables['bus_messages']\n"
            f"live = pd.Timestamp('{mig:%Y-%m-%d}')\n"
            "ue = ue[pd.to_datetime(ue['occurred_at']) >= live]\n"
            "lost = ue[~ue['event_id'].isin(bus['event_id'])]\n"
            "price = tables['customers'].merge(tables['plans'], on='plan_id').set_index('customer_id')['price_per_unit_cents']\n"
            "lost = lost.assign(unbilled=lost['quantity'] * lost['customer_id'].map(price))\n"
            "monthly = lost.groupby(pd.to_datetime(lost['occurred_at']).dt.to_period('M'))[['quantity', 'unbilled']].sum()\n"
            "phantoms = bus[~bus['event_id'].isin(tables['usage_events']['event_id'])]\n"
            "result = {'lost_events': len(lost), 'lost_units': int(lost['quantity'].sum()),\n"
            "          'unbilled_cents': int(lost['unbilled'].sum()), 'phantom_events': len(phantoms)}\n"
            "print(monthly)\n"
        )
        return GroundTruth(
            root_cause=root, anomalies=[anomalies[k] for k in ("A1", "A2", "A3") if k in anomalies], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q_lost.sql, q_dep.sql, q_cause.sql, q_gap.sql, q_gm.sql, q_bb.sql] + ([anomalies["A3"].reference_sql[0].sql] if "A3" in anomalies else []),
                python=[python_ref],
                memo_bullets=[
                    "Kafka guarantees delivery only of what was published; the process died between COMMIT and publish (dual write).",
                    "Losses cluster at usage-service deploys (counter flush on shutdown) and broker incidents - a bigger broker fixes neither the deploy losses nor the design.",
                    "Write the usage row and an outbox row in one transaction; a relay (polling or CDC) publishes committed rows only - no lost and no phantom events.",
                    "The relay is at-least-once: make the rating consumer idempotent on event_id (unique key) or it will double-bill.",
                    f"Revenue is short by {f['unbilled'] / 100:,.0f} USD while cost is complete; gross margin drifted {f['gm_drift_pts']:.2f} pts.",
                    f"Back-bill only within contract windows ({f['recoverable'] / 100:,.0f} USD); book an unbilled receivable (Dr 1150 / Cr 4020) for that portion.",
                    "Add a daily reconciliation of usage_events vs rated_usage so drift is caught in a day, not a quarter.",
                ],
                design_mermaid=FIXED_MERMAID),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, stakeholders, timeline (red herrings flagged) and the flawed architecture."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        names = rng.pick_names(4)
        r = state.statements.ratios  # type: ignore[union-attr]
        daily_p = cal.labels([state.extras["daily_deploys_from"]])[0]
        inc = state.extras["incidents"]
        sev2_p = cal.labels([inc.loc[inc["severity"] == "sev2", "started_at"].iloc[0]])[0]
        reveal = [
            f"Gross margin went from {r[P[3]]['gross_margin']:.1%} in {P[3]} to {r[P[-1]]['gross_margin']:.1%} in {P[-1]}.",
            "Finance believes cloud cost inflation is compressing margin.",
            f"There were sev2 Kafka incidents (first one in {sev2_p}); on-call tickets mention producer timeouts.",
            "The billing PM's runbook: invoices are generated from rated_usage, which the rating consumer builds from the usage topic.",
            f"Billing moved to the event-driven pipeline in {P[4]}.",
        ]
        if p["ordering_fault"]:
            reveal.append("A large customer disputed its latest invoice: 'your usage numbers are higher than our own logs'.")
        conceal = [
            "the dual write: usage-service commits then publishes in a separate call, no transactional outbox",
            f"lost events: {f['lost_count']} events / {f['lost_units']} units never published",
            "losses cluster at deploy restarts (counter flush on shutdown) and sev2 broker incidents",
            f"unbilled usage of {f['unbilled']} cents and the gross-margin drift it causes",
            f"the back-billing clause value ({f['recoverable']} cents recoverable)",
            "A1", "A2",
        ]
        if p["ordering_fault"]:
            conceal += [f"phantom events: {f['phantom_count']} rolled-back events were published and billed", "A3"]
        stakeholders = [
            Stakeholder(name=names[0], role="VP Engineering", agenda="Defend the event-driven billing migration.", disposition="confident",
                        knows=["usage-service publishes right after it commits"], hides=["there is no outbox and no reconciliation job"]),
            Stakeholder(name=names[1], role="Billing PM", agenda="Ship invoices on day 1 of the month.", disposition="trusting",
                        knows=["invoices are built from rated_usage"], hides=[]),
            Stakeholder(name=names[2], role="CFO", agenda="Explain the gross-margin miss to the board.", disposition="skeptical",
                        knows=["gross margin is down", "cloud spend is the second-largest cost"], hides=["already promised a cloud cost-cutting plan"]),
            Stakeholder(name=names[3], role="SRE Lead", agenda="Keep daily deploys.", disposition="candid",
                        knows=["deploys restart pods mid-request; usage-service flushes counters on SIGTERM"], hides=["graceful shutdown timeout is shorter than the flush"]),
        ]
        tl = [
            TimelineEvent(period=P[4], headline="Migrated to event-driven billing", detail="Rating consumer + Kafka topic replace the nightly batch."),
            TimelineEvent(period=sev2_p, headline="Broker partition incident", detail="Producer timeouts for several hours; resolved by on-call."),
            TimelineEvent(period=daily_p, headline="Daily deploys introduced", detail="usage-service ships every weekday."),
            TimelineEvent(period=P[7], headline="Gross-margin miss; cost-cutting plan", detail="CFO asks engineering for a 10% cloud saving."),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[5], headline="Cloud provider price change", detail=f"List prices changed {p['cloud_price_change']:+.0%}.", is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[6], headline="Kafka cluster upgrade proposal", detail="Platform team asks for a bigger broker cluster.", is_red_herring=True))
        if p["ordering_fault"]:
            tl.append(TimelineEvent(period=P[6], headline="Customer disputes over-bill", detail="batch-import change shipped earlier in the quarter."))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["architecture_diagram", "rating_job_runbook", "oncall_incident_tickets", "gross_margin_bridge", "cloud_bill_summary"],
                           architecture_mermaid=FLAWED_MERMAID)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Five spec levers (implement_outbox coded 0 none / 1 outbox + relay)."""
        return [
            Lever(key="implement_outbox", label="Transactional outbox + relay (0 none / 1 polling or CDC relay)", min=0, max=1, default=0, unit="code",
                  effect="Lost and phantom events go to ~0; delivery becomes at-least-once."),
            Lever(key="consumer_idempotent", label="Idempotent rating consumer keyed on event_id (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Removes relay duplicates."),
            Lever(key="backbill", label="Back-bill lost usage inside contract windows (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="One-off revenue and cash recovery; some back-billed customers churn."),
            Lever(key="reconcile_job", label="Daily usage_events vs rated_usage reconciliation (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Drift detected in a day instead of a quarter."),
            Lever(key="bigger_broker", label="Bigger Kafka cluster (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Costs money; removes only incident-time losses."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model around the P8 quarter (module docstring)."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        P8 = state.periods[-1]
        ev = state.extras["events"]
        price_c = state.extras["price_c"]
        p8 = cal.labels(ev["occurred_at"]) == P8
        lost8 = ev[p8 & ev["_lost"].to_numpy()]
        lost8_val = float((lost8["quantity"].to_numpy() * price_c[lost8["_cust"].to_numpy()]).sum())
        bus = state.frames["bus_messages"]
        ph8 = bus[(cal.labels(bus["occurred_at"]) == P8) & bus["_phantom"].to_numpy()]
        ph8_val = float((ph8["quantity"].to_numpy() * price_c[ph8["_cust"].to_numpy()]).sum())
        inc8 = next(i for i in state.statements.income if i.period == P8)  # type: ignore[union-attr]
        gm8 = state.statements.ratios[P8]["gross_margin"]  # type: ignore[union-attr]
        rev8 = float(inc8.revenue_cents)
        cogs8 = rev8 * (1 - gm8)
        gm_fixed = 1 - cogs8 / max(1.0, rev8 + lost8_val - ph8_val)
        n8 = float(p8.sum())
        dup_relay = round(0.004 * n8)
        inc_share = float(lost8["_in_incident"].mean()) if len(lost8) else 0.0
        broker_cost = 0.04 * cogs8
        kpis = {
            "lost_events": float(len(lost8)), "unbilled_revenue_cents": lost8_val, "phantom_events": float(len(ph8)), "overbilled_cents": ph8_val,
            "duplicate_events": 0.0, "gross_margin": gm8, "revenue_cents": rev8, "net_income_cents": float(inc8.net_income_cents),
            "cash_cents": float(state.ledger.trial_balance(P8)["1000"]), "drift_detection_days": 90.0, "broker_cost_cents": broker_cost,
            "churned_customers": 0.0,
        }
        rec = float(f["recoverable"])
        effects = [
            {"lever": "implement_outbox", "kpi": "lost_events", "delta_per_unit": -float(len(lost8)), "lag_periods": 0},
            {"lever": "implement_outbox", "kpi": "unbilled_revenue_cents", "delta_per_unit": -lost8_val, "lag_periods": 0},
            {"lever": "implement_outbox", "kpi": "phantom_events", "delta_per_unit": -float(len(ph8)), "lag_periods": 0},
            {"lever": "implement_outbox", "kpi": "overbilled_cents", "delta_per_unit": -ph8_val, "lag_periods": 0},
            {"lever": "implement_outbox", "kpi": "duplicate_events", "delta_per_unit": float(dup_relay), "lag_periods": 0},
            {"lever": "implement_outbox", "kpi": "revenue_cents", "delta_per_unit": lost8_val - ph8_val, "lag_periods": 1},
            {"lever": "implement_outbox", "kpi": "gross_margin", "delta_per_unit": gm_fixed - gm8, "lag_periods": 1},
            {"lever": "consumer_idempotent", "kpi": "duplicate_events", "delta_per_unit": -2.0 * dup_relay, "lag_periods": 0},
            {"lever": "backbill", "kpi": "cash_cents", "delta_per_unit": rec * 0.97, "lag_periods": 0},
            {"lever": "backbill", "kpi": "net_income_cents", "delta_per_unit": rec * (1 - p["tax_rate"]), "lag_periods": 0},
            {"lever": "backbill", "kpi": "churned_customers", "delta_per_unit": max(1.0, round(0.02 * len(state.frames["customers"]))), "lag_periods": 1},
            {"lever": "reconcile_job", "kpi": "drift_detection_days", "delta_per_unit": -89.0, "lag_periods": 0},
            {"lever": "bigger_broker", "kpi": "broker_cost_cents", "delta_per_unit": broker_cost, "lag_periods": 0},
            {"lever": "bigger_broker", "kpi": "lost_events", "delta_per_unit": -inc_share * len(lost8), "lag_periods": 0},
            {"lever": "bigger_broker", "kpi": "net_income_cents", "delta_per_unit": -broker_cost * (1 - p["tax_rate"]), "lag_periods": 0},
        ]
        g = p["growth_qoq"]
        drag = {"lost_events": g * len(lost8), "unbilled_revenue_cents": g * lost8_val, "cash_cents": -lost8_val,
                "gross_margin": -abs(f["gm_drift_pts"]) / 100 / 4}
        bounds = {k: [0.0, None] for k in ("lost_events", "unbilled_revenue_cents", "phantom_events", "overbilled_cents", "duplicate_events",
                                           "drift_detection_days")}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag, "bounds": bounds,
                "resolved_if": {"implement_outbox": 1.0, "consumer_idempotent": 1.0}}
