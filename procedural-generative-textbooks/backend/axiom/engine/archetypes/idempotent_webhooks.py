"""``idempotent_webhooks`` - "At-Least-Once" (archetypes.md section 9).

A B2B procurement marketplace (buyers pay suppliers by corporate card through a payment provider;
the platform keeps a take rate) migrated its webhook consumer to synchronous processing. The
provider delivers ``charge.succeeded`` / ``payment_intent.succeeded`` events at least once with
exponential-backoff retries; the consumer writes ``payments_ledger``, credits the seller and calls
the provider's legacy *capture* endpoint **without an idempotency key** before answering. When the
handler outlives the provider timeout the provider retries a delivery that was already processed,
so the customer is charged twice.

Model (resolves the spec's "or" variants into one consistent story)
------------------------------------------------------------------
* Every processing run of an authorisation event = one ``payments_ledger`` row + one capture
  (``transactions`` row, same ``payment_intent_id``). Before the migration (start of P5) the old
  consumer acked first and processed asynchronously exactly once.
* After the migration a delivery whose handler exceeds ``consumer_timeout_s`` is recorded by the
  provider as ``status='timeout'`` although the work committed; the retry processes again.
  Baseline timeout probability ``p_timeout_base`` (0.3-0.8%), in 2-4 incident windows of 20-180
  minutes (campaign traffic, downstream p99 > timeout) ``p_timeout = U(15, 40)%``.
* Principal: a ``processed_events`` table dedupes on ``event_id`` - but for
  ``payment_intent.succeeded`` the provider re-issues a *new* ``event_id`` on retry, so those
  still duplicate (the correct key is ``payment_intent_id``). Practitioner: ``processed_events`` is
  a non-unique processing log. Foundation: no ``processed_events`` table.
* Consequences: ``dup_to_dispute_rate`` of duplicate captures become disputes (reason
  ``duplicate_processing``, Visa 12.6.1, always lost) opened 10-45 days later; the rest raise a
  ``duplicate_charge`` support ticket and are refunded 2-7 days after the ticket.
* GL (settlement batches): ``Dr 1000 net, Dr 5050 fees; Cr 4020 take x unique volume, Cr 2300
  seller share, Cr 2450 duplicate volume`` - the GL follows provider settlements and parks
  duplicate cash as a refund liability (the Controller's "unreconciled" line). Duplicate refunds
  and lost duplicate disputes relieve 2450; fees on duplicates stay in 5050; dispute fees ($15)
  and the platform share of lost baseline disputes go to 6500.
* **Designed sub-ledger break**: internal ``payments_ledger`` net volume (charges - refunds it
  recorded) vs GL volume (4020 + 2300 credits from sales less baseline refunds) differs by
  ``duplicates - duplicate refunds`` in every period; the tie is declared with that amount and the
  invariant checker verifies it to the cent. ``refund_liability`` (2450), ``seller_payable``
  (2300) and ``revenue`` ties hold exactly.
* Volumes are row-budget constrained (Appendix B.1), so tickets are B2B-sized (tens of $k) and the
  incident windows carry short traffic spikes (campaign launches) so each window holds enough
  deliveries to show the race.

Simulation (reduced form, Appendix B.4): KPIs are P8 levels. ``implement_dedupe`` (0 none / 1
event_id / 2 payment_intent_id) removes half of the duplicates per step; ``ack_before_process``
removes 90% of duplicates and of timeouts; ``raise_consumer_timeout_s`` has elasticities -0.6
(timeouts) and -0.5 (duplicates) - the race narrows but remains; ``backfill_refunds`` pays out
the open duplicate liability now and removes the disputes it would cause; ``rate_limit_retries``
halves duplicates but turns retried timeouts into lost events (the trap); ``dispute_response_effort``
lifts the win rate on baseline disputes from 20% to 45%. While unresolved, each quarter adds the
P6-P8 average duplicate cost and dispute drift. ``resolved_if``: payment_intent dedupe (>= 2) and
ack-before-process (>= 1).
"""

from __future__ import annotations

import datetime as dt
import json
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
from axiom.engine.datagen import GL_BATCH, ROW_BUDGET, TableBuilder
from axiom.engine.finance import SubledgerTie, batch_end_dates, cents, cents_array, split_cents
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

logger = logging.getLogger("axiom.archetypes.idempotent_webhooks")

FEE_RATE = 0.029
FEE_FIXED = 30
DISPUTE_FEE = 1500
CARD_BRANDS = ("visa", "mastercard", "amex", "discover")
FLAWED_MERMAID = """flowchart LR
  P[Payment Provider] -- "POST /webhooks (at-least-once, retries w/ backoff, {timeout}s timeout)" --> R[Webhook Receiver]
  R -->|"sync: validate -> write ledger -> credit seller -> call capture"| DB[(Postgres payments_ledger)]
  R -->|"after work, returns 200 (often > {timeout}s)"| P
  R --> Q[Payout Service]
  DB --> F[Finance Recon job (monthly)]
  classDef bad fill:#3b1d1d,stroke:#ff5555,color:#fff
  class R bad"""
FIXED_MERMAID = """flowchart LR
  P[Payment Provider] -- "POST /webhooks (at-least-once)" --> R[Webhook Receiver]
  R -->|"BEGIN; INSERT INTO processed_events(event_key) ON CONFLICT DO NOTHING; enqueue job; COMMIT"| DB[(Postgres)]
  R -->|"200 immediately"| P
  W[Async worker] -->|"poll inbox"| DB
  W -->|"capture with Idempotency-Key = payment_intent_id"| P
  W -->|"ledger write + seller credit in the same tx as inbox status"| DB
  note["event_key = payment_intent_id || ':' || type (unique constraint)"] --- DB"""


@register
class IdempotentWebhooks(Archetype):
    """At-least-once delivery meets a non-idempotent consumer: double charges, disputes, recon gap."""

    key = "idempotent_webhooks"
    title = "At-Least-Once"
    pillars = ("system_design", "accounting_finance", "sql")
    industries = ("fintech_marketplace", "saas", "ecommerce")
    schema_template = "fintech_ledger"
    summary = ("Customers are being charged twice after a webhook consumer migration; disputes climb toward the card "
               "network threshold and finance cannot reconcile the payments ledger to the provider.")
    root_cause_terms = ("timeout", "idempotency", "dedupe", "incident windows")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 9."""
        ind = industry or "fintech_marketplace"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        marketplace = ind == "fintech_marketplace"
        lo, hi = pool.industry(ind).revenue_range_usd
        rev_usd = math.exp(rng.uniform(math.log(max(lo, 8e6)), math.log(max(min(hi, 40e6), lo * 1.5))))
        if marketplace:
            take = float(np.clip(bk.sample_between(pool, rng.spawn("bench:take"), "ops.fintech.marketplace_take_rate", ind, 0.25, 0.75), 0.06, 0.25))
        else:
            take = 1.0
        window = difficulty_pick(difficulty, 2, 3, 3)
        p: dict[str, Any] = {
            "industry": ind,
            "marketplace": marketplace,
            "business_model": "B2B procurement marketplace with embedded card payments" if marketplace else
                              ("B2B SaaS billing annual plans by card" if ind == "saas" else "B2B e-commerce storefront taking card payments"),
            "revenue_ttm_cents": int(rev_usd * 100),
            "take_rate": take,
            "growth_qoq": float(rng.uniform(0.03, 0.08)),
            "chargeback_rate_base": float(np.clip(bk.sample_between(pool, rng.spawn("bench:cb"), "fintech_marketplace.chargeback_rate", "fintech_marketplace", 0.25, 0.6), 0.001, 0.008)),
            "dispute_win_rate": float(np.clip(bk.sample_between(pool, rng.spawn("bench:win"), "fintech_marketplace.dispute_win_rate", "fintech_marketplace", 0.2, 0.5), 0.1, 0.6)),
            "max_attempts": int(rng.integers(5, 9)),
            "consumer_timeout_s": int(rng.integers(10, 31)),
            "p_timeout_base": float(rng.uniform(0.003, 0.008)),
            "p_timeout_window": magnitude(rng, difficulty, 0.15, 0.40),
            "p_error": 0.002,
            "n_windows": difficulty_pick(difficulty, 2, 3, 4),
            "window_periods": window,
            "dup_charge_share_target": magnitude(rng, difficulty, 0.004, 0.025),
            "dup_to_dispute_rate": float(rng.uniform(0.25, 0.45)),
            "dispute_fee_cents": DISPUTE_FEE,
            "support_cost_per_ticket_cents": int(rng.uniform(600, 1400)),
            "idempotency_key_share": float(rng.uniform(0.55, 0.75)),
            "new_api_share": float(rng.uniform(0.35, 0.6)),
            "dedupe": difficulty_pick(difficulty, "none", "none", "event_id"),
            "processed_events_table": difficulty_pick(difficulty, "absent", "log", "unique_event_id"),
            "refund_rate_base": float(rng.uniform(0.01, 0.025)),
            "ticket_rate_base": float(rng.uniform(0.015, 0.03)),
            "op_margin": float(rng.uniform(0.04, 0.12)),
            "tax_rate": float(np.clip(bk.sample_between(pool, rng.spawn("bench:tax"), "fintech_marketplace.effective_tax_rate", "fintech_marketplace", 0.4, 0.7), 0.1, 0.25)),
            "capex_pct": float(np.clip(bk.sample_between(pool, rng.spawn("bench:capex"), "fintech_marketplace.capex_pct_revenue", "fintech_marketplace", 0.25, 0.5), 0.01, 0.05)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        budget = ROW_BUDGET[difficulty]
        p["n_merchants"] = difficulty_pick(difficulty, 50, 110, 180) if marketplace else 6
        # calibrated rows per payment intent (transactions, events, deliveries, ledger, [processed_events], long tail)
        per_intent = 4.1 if p["processed_events_table"] == "absent" else 5.1
        gl_rows = difficulty_pick(difficulty, 1500, 1500, 5300)
        fixed = (int(p["n_merchants"] * 27 * 0.45) if marketplace else 0) + gl_rows + difficulty_pick(difficulty, 108, 108, 820) + p["n_merchants"] + 70
        p["n_intents"] = int(max(800, (budget * difficulty_pick(difficulty, 0.92, 0.92, 0.86) - fixed) / per_intent))
        p["description"] = (f"{p['business_model'].capitalize()}; card payments run through a third-party provider whose "
                            "webhooks drive the internal payments ledger and seller payouts.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Merchants, payment intents (volume, amounts, event types) and the migration date."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        M = p["n_merchants"]
        mw = 1.0 + g.pareto(1.16, M)
        merchants = pd.DataFrame({
            "merchant_id": [f"M{i:04d}" for i in range(1, M + 1)],
            "name": [rng.company_name("manufacturing") for _ in range(M)],
            "mcc": np.array(("5085", "5065", "5046", "5111", "5169"))[g.integers(0, 5, M)],
            "onboarded_at": pd.Timestamp(cal.start) - pd.to_timedelta(g.integers(30, 900, M), unit="D"),
            "payout_schedule": "monthly",
            "risk_tier": np.array(("low", "medium", "high"))[np.minimum(2, (g.random(M) * 3).astype(int))],
            "country": np.array(("US", "US", "US", "CA", "GB", "DE"))[g.integers(0, 6, M)],
            "status": "active",
        })
        N = p["n_intents"]
        trend = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3.0) * g.lognormal(0, 0.04, n_m)
        counts = np.floor(N * trend / trend.sum()).astype(int)
        counts[-1] += N - counts.sum()
        midx = np.repeat(np.arange(n_m), counts)
        mstart = pd.to_datetime(pd.Series(months))
        days_in = mstart.dt.days_in_month.to_numpy()
        hour = np.clip(g.normal(14, 3.5, N), 0, 23.99)
        ts = (mstart.to_numpy()[midx] + pd.to_timedelta((g.random(N) * days_in[midx]).astype(int), unit="D").to_numpy()
              + pd.to_timedelta(hour * 3600, unit="s").to_numpy())
        ts = np.sort(ts)
        gpv_ttm = p["revenue_ttm_cents"] / p["take_rate"]
        n_ttm = counts[-12:].sum()
        median = gpv_ttm / n_ttm / math.exp(0.8 ** 2 / 2)
        amount = np.maximum(5_000, np.round(g.lognormal(math.log(median), 0.8, N) / 100) * 100).astype(np.int64)
        intents = pd.DataFrame({
            "payment_intent_id": [f"pi_{i:07d}" for i in range(1, N + 1)],
            "created_at": pd.to_datetime(ts),
            "merchant_id": merchants["merchant_id"].to_numpy()[g.choice(M, size=N, p=mw / mw.sum())],
            "buyer_id": np.array([f"B{k:05d}" for k in range(1, max(50, N // 3) + 1)])[g.integers(0, max(50, N // 3), N)],
            "amount_cents": amount,
            "event_type": np.where(g.random(N) < p["new_api_share"], "payment_intent.succeeded", "charge.succeeded"),
            "card_brand": np.array(CARD_BRANDS)[np.minimum(3, (g.random(N) ** 1.6 * 4).astype(int))],
            "idempotency_key": np.where(g.random(N) < p["idempotency_key_share"], [f"idk_{h:012x}" for h in g.integers(0, 2**47, N)], None),
            "flash": np.zeros(N, dtype=bool),
        })
        p5_start = pd.Timestamp(cal.bounds(state.p(5))[0])
        state.extras["migration_at"] = p5_start + pd.Timedelta(days=int(g.integers(2, 20)), hours=10)
        state.frames["merchants"] = merchants
        state.extras["intents"] = intents
        state.extras["merchant_weight"] = mw

    # ------------------------------------------------------------------ anomalies

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Incident windows with campaign traffic; delivery simulation; duplicate captures and their consequences."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("anomaly")
        g = rng.generator
        intents = state.extras["intents"]
        win_periods = state.periods[-p["window_periods"]:]
        windows = []
        per = np.linspace(0, len(win_periods), p["n_windows"], endpoint=False).astype(int)
        for i, k in enumerate(per):
            s, e = cal.bounds(win_periods[min(k, len(win_periods) - 1)])
            day = pd.Timestamp(s) + pd.Timedelta(days=int(g.integers(5, (e - s).days - 5)))
            start = day + pd.Timedelta(hours=int(g.integers(9, 17)), minutes=int(g.integers(0, 60)))
            dur = int(g.integers(20, 181))
            windows.append((start, start + pd.Timedelta(minutes=dur)))
        windows.sort()
        # campaign traffic inside windows sized to the target duplicate share
        months_in = {(w[0].year, w[0].month) for w in windows}
        ts = intents["created_at"]
        base_in_months = int(sum(((ts.dt.year == y) & (ts.dt.month == m)).sum() for y, m in months_in))
        dedupe_factor = p["new_api_share"] if p["dedupe"] == "event_id" else 1.0
        target_dups = p["dup_charge_share_target"] * base_in_months
        per_window = max(20, int(round(target_dups / max(1e-6, p["p_timeout_window"] * dedupe_factor) / len(windows))))
        rows = []
        for (ws, we) in windows:
            span = (we - ws).total_seconds()
            for _ in range(per_window):
                rows.append(ws + pd.Timedelta(seconds=float(g.uniform(0, span))))
        flash = intents.sample(n=len(rows), replace=True, random_state=np.random.RandomState(int(g.integers(0, 2**31 - 1)))).copy()
        flash["created_at"] = rows
        flash["flash"] = True
        allint = pd.concat([intents, flash], ignore_index=True).sort_values("created_at", kind="stable").reset_index(drop=True)
        allint["payment_intent_id"] = [f"pi_{i:07d}" for i in range(1, len(allint) + 1)]
        state.extras["intents"] = allint
        state.extras["windows"] = windows
        self._simulate_deliveries(ctx, state)
        dedupe_txt = {"none": "no dedupe at all", "event_id": "dedupe on event_id only"}[p["dedupe"]]
        anomalies = [
            Anomaly(id="A1", kind="duplicate_payments", pillar="system_design", title="Duplicate processing of retried webhook deliveries",
                    mechanism=(f"After the {state.extras['migration_at']:%Y-%m-%d} migration the consumer processes synchronously ({dedupe_txt}); "
                               f"deliveries whose handler exceeds the {p['consumer_timeout_s']}s provider timeout are retried although already committed, "
                               "so payments_ledger gets a second row and capture is called again without an idempotency key. "
                               f"{len(windows)} incident windows: " + "; ".join(f"{a:%Y-%m-%d %H:%M}-{b:%H:%M}" for a, b in windows) + "."),
                    periods=sorted({cal.labels([w[0]])[0] for w in windows}), affected_accounts=["1000", "2450", "5050"],
                    affected_tables=["webhook_deliveries", "webhook_events", "payments_ledger", "transactions", "processed_events"],
                    magnitude={"incident_window_count": float(len(windows)), "p_timeout_window": round(p["p_timeout_window"], 4)}, severity="critical"),
            Anomaly(id="A2", kind="duplicate_charge_consequences", pillar="accounting_finance", title="Refunds, disputes and support load from double charges",
                    mechanism=(f"{p['dup_to_dispute_rate']:.0%} of duplicate captures become duplicate_processing disputes (12.6.1, lost, $15 fee); "
                               "the rest raise duplicate_charge tickets and are refunded 2-7 days later."),
                    periods=win_periods, affected_accounts=["2450", "6500", "6200", "1000"], affected_tables=["disputes", "refunds", "support_tickets"],
                    magnitude={"dup_to_dispute_rate": round(p["dup_to_dispute_rate"], 4)}, severity="high"),
            Anomaly(id="A3", kind="ledger_reconciliation_gap", pillar="sql", title="Internal payments ledger vs provider-settled GL gap",
                    mechanism=("GL cash follows provider settlements and parks duplicate captures in 2450; the internal payments_ledger books every "
                               "processing run as volume, so the two differ by duplicates not yet refunded."),
                    periods=list(state.periods[4:]), affected_accounts=["2450", "4020", "2300"], affected_tables=["payments_ledger", "settlements", "gl_lines"],
                    magnitude={}, severity="medium"),
        ]
        return anomalies

    def _simulate_deliveries(self, ctx: BuildContext, state: WorldState) -> None:
        """Provider delivery attempts (vectorised per attempt) and consumer processing runs."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("deliveries").generator
        it = state.extras["intents"]
        N = len(it)
        mig = state.extras["migration_at"]
        windows = state.extras["windows"]
        timeout_ms = p["consumer_timeout_s"] * 1000
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)

        def in_window(t: pd.Series) -> np.ndarray:
            v = t.to_numpy()
            out = np.zeros(len(v), dtype=bool)
            for a, b in windows:
                out |= (v >= np.datetime64(a)) & (v <= np.datetime64(b))
            return out

        reissue = (p["dedupe"] == "event_id") & (it["event_type"].to_numpy() == "payment_intent.succeeded")
        event_seq = np.arange(1, N + 1)
        cur_event = np.array([f"evt_{i:08d}" for i in event_seq], dtype=object)
        next_evt = N + 1
        events = [pd.DataFrame({"event_id": cur_event.copy(), "event_type": it["event_type"].to_numpy(),
                                "payment_intent_id": it["payment_intent_id"].to_numpy(),
                                "created_at": it["created_at"] + pd.to_timedelta(g.uniform(0.2, 2.0, N), unit="s"),
                                "reissue_of": None})]
        sent = events[0]["created_at"] + pd.to_timedelta(g.uniform(0.1, 1.5, N), unit="s")
        pending = np.ones(N, dtype=bool)
        deliveries, runs = [], []
        processed_any = np.zeros(N, dtype=bool)
        for attempt in range(1, p["max_attempts"] + 1):
            idx = np.flatnonzero(pending)
            if not len(idx):
                break
            s = sent.iloc[idx].reset_index(drop=True)
            post = (s >= mig).to_numpy()
            inw = in_window(s) & post
            pt = np.where(inw, p["p_timeout_window"], np.where(post, p["p_timeout_base"], 0.0))
            u = g.random(len(idx))
            timeout = u < pt
            error = (~timeout) & (g.random(len(idx)) < p["p_error"])
            ok = ~timeout & ~error
            ms_ok = np.where(post, g.lognormal(math.log(900), 0.5, len(idx)), g.lognormal(math.log(80), 0.4, len(idx)))
            if inw.any():
                ms_ok = np.where(inw, np.minimum(timeout_ms - 1, ms_ok * g.uniform(3, 9, len(idx))), ms_ok)
            ms = np.where(timeout, timeout_ms, np.where(error, g.integers(20, 200, len(idx)), np.minimum(ms_ok, timeout_ms - 1))).astype(np.int64)
            # consumer work committed: ok or timeout after migration (old consumer: ok only)
            processed = ok | (timeout & post)
            # principal dedupe on event_id: a retry of the same event id is skipped
            if p["dedupe"] == "event_id":
                skip = processed_any[idx] & ~reissue[idx] & (attempt > 1)
                processed = processed & ~skip
            evt_ids = cur_event[idx]
            deliveries.append(pd.DataFrame({
                "event_id": evt_ids, "attempt": attempt, "sent_at": s, "response_code": np.where(ok, 200, np.where(error, 500, 0)),
                "response_ms": ms, "status": np.where(ok, "delivered", np.where(timeout, "timeout", "error")),
                "_intent": idx, "_window": inw}))
            pr = idx[processed]
            done_at = s[processed] + pd.to_timedelta(np.where(timeout[processed], timeout_ms + g.integers(500, 9000, int(processed.sum())), ms[processed]), unit="ms")
            runs.append(pd.DataFrame({"_intent": pr, "event_id": evt_ids[processed], "processed_at": done_at.to_numpy(), "attempt": attempt}))
            processed_any[pr] = True
            retry = timeout | error
            pending[:] = False
            ridx = idx[retry]
            pending[ridx] = True
            backoff = 60.0 * 2 ** (attempt - 1) * g.uniform(0.8, 1.2, len(ridx))
            extra = np.where(timeout[retry], p["consumer_timeout_s"], 0.5)
            new_sent = s[retry].to_numpy() + pd.to_timedelta(backoff + extra, unit="s").to_numpy()
            sent = sent.copy()
            sent.iloc[ridx] = new_sent
            # re-issued event ids for the new-API event type (principal trap)
            re = ridx[reissue[ridx]]
            if len(re):
                new_ids = np.array([f"evt_{next_evt + k:08d}" for k in range(len(re))], dtype=object)
                next_evt += len(re)
                events.append(pd.DataFrame({"event_id": new_ids, "event_type": it["event_type"].to_numpy()[re],
                                            "payment_intent_id": it["payment_intent_id"].to_numpy()[re],
                                            "created_at": sent.iloc[re].to_numpy() - np.timedelta64(1, "s"), "reissue_of": cur_event[re]}))
                cur_event[re] = new_ids
        dl = pd.concat(deliveries, ignore_index=True)
        dl = dl[dl["sent_at"] <= end_ts].sort_values(["sent_at", "event_id"], kind="stable").reset_index(drop=True)
        dl.insert(0, "delivery_id", [f"dlv_{i:08d}" for i in range(1, len(dl) + 1)])
        ev = pd.concat(events, ignore_index=True)
        ev = ev[ev["created_at"] <= end_ts].reset_index(drop=True)
        rn = pd.concat(runs, ignore_index=True)
        rn["processed_at"] = pd.to_datetime(rn["processed_at"])
        rn = rn[rn["processed_at"] <= end_ts].sort_values(["processed_at", "_intent"], kind="stable").reset_index(drop=True)
        rn["run_no"] = rn.groupby("_intent").cumcount() + 1
        state.extras.update({"deliveries": dl, "events": ev, "runs": rn})
        state.extras["dups"] = rn[rn["run_no"] > 1]

    # ------------------------------------------------------------------ consequences + ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Captures, refunds, disputes, tickets, payouts; then post the GL."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("consequences").generator
        it = state.extras["intents"]
        rn = state.extras["runs"]
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        take = p["take_rate"]
        # --- provider captures (transactions): one per processing run
        tx = pd.DataFrame({
            "transaction_id": [f"ch_{i:08d}" for i in range(1, len(rn) + 1)],
            "payment_intent_id": it["payment_intent_id"].to_numpy()[rn["_intent"].to_numpy()],
            "merchant_id": it["merchant_id"].to_numpy()[rn["_intent"].to_numpy()],
            "buyer_id": it["buyer_id"].to_numpy()[rn["_intent"].to_numpy()],
            "amount_cents": it["amount_cents"].to_numpy()[rn["_intent"].to_numpy()],
            "currency": "USD",
            "created_at": rn["processed_at"],  # capture returns within the handler run
            "card_brand": it["card_brand"].to_numpy()[rn["_intent"].to_numpy()],
            "idempotency_key": it["idempotency_key"].to_numpy()[rn["_intent"].to_numpy()],
            "source_event_id": rn["event_id"].to_numpy(),
            "_dup": (rn["run_no"] > 1).to_numpy(),
            "_intent": rn["_intent"].to_numpy(),
        })
        tx["captured_at"] = tx["created_at"]
        tx["fee_cents"] = (cents_array(tx["amount_cents"].to_numpy() * FEE_RATE) + FEE_FIXED).astype(np.int64)
        tx["net_cents"] = tx["amount_cents"] - tx["fee_cents"]
        tx["take_cents"] = np.where(tx["_dup"], 0, cents_array(tx["amount_cents"].to_numpy() * take))
        tx["seller_cents"] = np.where(tx["_dup"], 0, tx["amount_cents"] - tx["take_cents"])
        n = len(tx)
        dup = tx["_dup"].to_numpy()
        # --- duplicate consequences
        to_dispute = dup & (g.random(n) < p["dup_to_dispute_rate"])
        to_refund = dup & ~to_dispute
        ticket_at = tx["created_at"] + pd.to_timedelta(g.uniform(0.2, 3.0, n), unit="D")
        refund_at = ticket_at + pd.to_timedelta(g.uniform(2, 7, n), unit="D")
        d_open = tx["created_at"] + pd.to_timedelta(g.integers(10, 46, n), unit="D")
        d_resolve = d_open + pd.to_timedelta(g.integers(20, 61, n), unit="D")
        # --- baseline refunds / disputes / tickets on unique captures
        uniq = ~dup
        b_refund = uniq & (g.random(n) < p["refund_rate_base"])
        b_refund_at = tx["created_at"] + pd.to_timedelta(g.uniform(1, 20, n), unit="D")
        b_dispute = uniq & ~b_refund & (g.random(n) < p["chargeback_rate_base"])
        b_open = tx["created_at"] + pd.to_timedelta(g.integers(10, 61, n), unit="D")
        b_resolve = b_open + pd.to_timedelta(g.integers(30, 76, n), unit="D")
        b_won = g.random(n) < p["dispute_win_rate"]
        b_reason = np.where(g.random(n) < 0.6, "fraudulent", "product_not_received")
        refunds = pd.concat([
            pd.DataFrame({"transaction_id": tx["transaction_id"][to_refund], "payment_intent_id": tx["payment_intent_id"][to_refund],
                          "amount_cents": tx["amount_cents"][to_refund], "created_at": refund_at[to_refund], "reason": "duplicate",
                          "_dup": True, "_seller": 0, "_take": 0, "merchant_id": tx["merchant_id"][to_refund]}),
            pd.DataFrame({"transaction_id": tx["transaction_id"][b_refund], "payment_intent_id": tx["payment_intent_id"][b_refund],
                          "amount_cents": tx["amount_cents"][b_refund], "created_at": b_refund_at[b_refund], "reason": "requested_by_customer",
                          "_dup": False, "_seller": tx["seller_cents"][b_refund], "_take": tx["take_cents"][b_refund],
                          "merchant_id": tx["merchant_id"][b_refund]}),
        ], ignore_index=True)
        refunds = refunds[refunds["created_at"] <= end_ts].sort_values(["created_at", "transaction_id"], kind="stable").reset_index(drop=True)
        refunds.insert(0, "refund_id", [f"re_{i:07d}" for i in range(1, len(refunds) + 1)])
        refunds["status"] = "succeeded"
        dsp = pd.concat([
            pd.DataFrame({"transaction_id": tx["transaction_id"][to_dispute], "opened_at": d_open[to_dispute], "reason": "duplicate_processing",
                          "reason_code": "12.6.1", "amount_cents": tx["amount_cents"][to_dispute], "_resolve": d_resolve[to_dispute], "_won": False,
                          "_dup": True, "_seller": 0, "_take": 0, "merchant_id": tx["merchant_id"][to_dispute]}),
            pd.DataFrame({"transaction_id": tx["transaction_id"][b_dispute], "opened_at": b_open[b_dispute], "reason": b_reason[b_dispute],
                          "reason_code": np.where(b_reason[b_dispute] == "fraudulent", "10.4", "13.1"), "amount_cents": tx["amount_cents"][b_dispute],
                          "_resolve": b_resolve[b_dispute], "_won": b_won[b_dispute], "_dup": False, "_seller": tx["seller_cents"][b_dispute],
                          "_take": tx["take_cents"][b_dispute], "merchant_id": tx["merchant_id"][b_dispute]}),
        ], ignore_index=True)
        dsp = dsp[dsp["opened_at"] <= end_ts].sort_values(["opened_at", "transaction_id"], kind="stable").reset_index(drop=True)
        resolved = dsp["_resolve"] <= end_ts
        dsp["status"] = np.where(~resolved, "under_review", np.where(dsp["_won"], "won", "lost"))
        dsp["resolved_at"] = dsp["_resolve"].where(resolved)
        dsp["evidence_submitted"] = dsp["_won"] | (g.random(len(dsp)) < 0.3)
        dsp.insert(0, "dispute_id", [f"dp_{i:06d}" for i in range(1, len(dsp) + 1)])
        # support tickets: duplicate_charge tickets for refunded dups + baseline
        b_ticket = uniq & (g.random(n) < p["ticket_rate_base"])
        cats = np.array(("shipping", "invoice_question", "account_access", "refund_request"))
        tickets = pd.concat([
            pd.DataFrame({"created_at": ticket_at[to_refund], "category": "duplicate_charge", "transaction_id": tx["transaction_id"][to_refund],
                          "buyer_id": tx["buyer_id"][to_refund]}),
            pd.DataFrame({"created_at": (tx["created_at"] + pd.to_timedelta(g.uniform(0.5, 15, n), unit="D"))[b_ticket],
                          "category": cats[g.integers(0, 4, n)][b_ticket], "transaction_id": tx["transaction_id"][b_ticket],
                          "buyer_id": tx["buyer_id"][b_ticket]}),
        ], ignore_index=True)
        tickets = tickets[tickets["created_at"] <= end_ts].sort_values(["created_at", "transaction_id"], kind="stable").reset_index(drop=True)
        tickets.insert(0, "ticket_id", [f"T{i:06d}" for i in range(1, len(tickets) + 1)])
        tickets["status"] = np.where(g.random(len(tickets)) < 0.9, "closed", "open")
        tickets["first_response_hours"] = np.round(g.lognormal(math.log(6), 0.9, len(tickets)), 1)
        state.extras.update({"tx": tx, "refunds": refunds, "disputes": dsp, "tickets": tickets})
        self._payouts(ctx, state)
        self._post(ctx, state)

    def _payouts(self, ctx: BuildContext, state: WorldState) -> None:
        """Monthly merchant payouts of the seller balance (marketplace only), paid on the 3rd."""
        p, cal = state.params, state.calendar
        months = state.extras["months"]
        tx, rf, dsp = state.extras["tx"], state.extras["refunds"], state.extras["disputes"]
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        if not p["marketplace"]:
            state.extras["payouts"] = pd.DataFrame(columns=["payout_id", "merchant_id", "amount_cents", "currency", "initiated_at", "arrived_at", "status", "method"])
            return
        lost = dsp[(dsp["status"] == "lost") & ~dsp["_dup"]]
        flows = pd.concat([
            pd.DataFrame({"merchant_id": tx["merchant_id"], "t": tx["created_at"], "v": tx["seller_cents"]}),
            pd.DataFrame({"merchant_id": rf["merchant_id"], "t": rf["created_at"], "v": -rf["_seller"].astype(np.int64)}),
            pd.DataFrame({"merchant_id": lost["merchant_id"], "t": lost["resolved_at"], "v": -lost["_seller"].astype(np.int64)}),
        ], ignore_index=True)
        start = cal.start
        flows["m"] = (flows["t"].dt.year - start.year) * 12 + flows["t"].dt.month - start.month
        merchants = state.frames["merchants"]["merchant_id"].to_numpy()
        piv = flows.pivot_table(index="merchant_id", columns="m", values="v", aggfunc="sum").reindex(index=merchants, columns=range(len(months)), fill_value=0).fillna(0)
        bal = np.zeros(len(merchants), dtype=np.int64)
        rows = []
        k = 0
        for m in range(len(months)):
            bal = bal + piv[m].to_numpy().astype(np.int64)
            pay_day = (pd.Timestamp(months[m]) + pd.offsets.MonthBegin(1)) + pd.Timedelta(days=2, hours=9)
            if pay_day > end_ts:
                break
            for j in np.flatnonzero(bal > 0):
                k += 1
                rows.append((f"po_{k:06d}", merchants[j], int(bal[j]), pay_day))
            bal = np.where(bal > 0, 0, bal)
        po = pd.DataFrame(rows, columns=["payout_id", "merchant_id", "amount_cents", "initiated_at"])
        po["currency"] = "USD"
        po["arrived_at"] = po["initiated_at"] + pd.Timedelta(days=1)
        po["status"] = "paid"
        po["method"] = "ach"
        state.extras["payouts"] = po

    def _post(self, ctx: BuildContext, state: WorldState) -> None:
        """Post the general ledger from the sub-ledgers (settlement batches + monthly close)."""
        p, cal = state.params, state.calendar
        L = state.ledger
        gran = GL_BATCH[p["difficulty"]]
        end_date = cal.end
        tx, rf, dsp, po, tk = (state.extras[k] for k in ("tx", "refunds", "disputes", "payouts", "tickets"))
        R = p["revenue_ttm_cents"]
        months = state.extras["months"]
        gm_est = 1 - (FEE_RATE + FEE_FIXED / max(1, tx["amount_cents"].mean())) / p["take_rate"]
        opex_pct = max(0.2, gm_est - p["op_margin"] - 0.02)
        ppe = cents(R * p["capex_pct"] * 5 * 0.55)
        accdep = cents(ppe * 0.45)
        cash0 = cents(R * 0.35)
        cs = cents(0.6 * (cash0 + ppe - accdep))
        L.open_books(cal.opening_date, {"1000": cash0, "1500": ppe, "1510": -accdep, "3000": -cs, "3100": -(cash0 + ppe - accdep - cs)})
        events: list[tuple[dt.date, int, Any]] = []
        # settlement batches
        txb = tx.assign(b=batch_end_dates(tx["created_at"], gran), dupc=np.where(tx["_dup"], tx["amount_cents"], 0))
        sett = txb.groupby("b", sort=True).agg(gross=("amount_cents", "sum"), fees=("fee_cents", "sum"), take_c=("take_cents", "sum"),
                                               seller=("seller_cents", "sum"), dupc=("dupc", "sum"), n=("transaction_id", "size"))
        for b, r in sett.iterrows():
            dim = {"batch": f"settlement:{b.date()}", "settlement_id": f"st_{b:%Y%m%d}", "rows": str(int(r.n))}
            lines = [("1000", int(r.gross - r.fees), 0, dim), ("5050", int(r.fees), 0, dim), ("4020", 0, int(r.take_c), dim),
                     ("2300", 0, int(r.seller), dim), ("2450", 0, int(r.dupc), dim)]
            events.append((b.date(), 2, lambda d, ln=lines: L.adjust(d, ln, "Provider settlement: captures net of fees", "sales")))
        rfb = rf.assign(b=batch_end_dates(rf["created_at"], gran))
        for (b, isdup), grp in rfb.groupby(["b", "_dup"], sort=True):
            dim = {"batch": f"refunds:{b.date()}", "rows": str(len(grp))}
            amt = int(grp["amount_cents"].sum())
            if isdup:
                lines = [("2450", amt, 0, dim), ("1000", 0, amt, dim)]
                memo = "Refunds of duplicate captures"
            else:
                lines = [("4100", int(grp["_take"].sum()), 0, dim), ("2300", int(grp["_seller"].sum()), 0, dim), ("1000", 0, amt, dim)]
                memo = "Customer refunds"
            events.append((b.date(), 3, lambda d, ln=lines, mm=memo: L.adjust(d, ln, mm, "refunds")))
        fee_b = dsp.assign(b=batch_end_dates(dsp["opened_at"], gran)).groupby("b", sort=True).size()
        for b, cnt in fee_b.items():
            events.append((b.date(), 4, lambda d, a=int(cnt) * p["dispute_fee_cents"], c=int(cnt): L.chargeback(
                d, 0, fee_cents=a, memo="Dispute fees", dim={"rows": str(c)})))
        lost = dsp[dsp["status"] == "lost"]
        lb = lost.assign(b=batch_end_dates(lost["resolved_at"], gran))
        for (b, isdup), grp in lb.groupby(["b", "_dup"], sort=True):
            dim = {"batch": f"disputes:{b.date()}", "rows": str(len(grp))}
            amt = int(grp["amount_cents"].sum())
            if isdup:
                lines = [("2450", amt, 0, dim), ("1000", 0, amt, dim)]
            else:
                lines = [("2300", int(grp["_seller"].sum()), 0, dim), ("6500", int(grp["_take"].sum()), 0, dim), ("1000", 0, amt, dim)]
            events.append((b.date(), 4, lambda d, ln=lines: L.adjust(d, ln, "Disputes lost (provider debit)", "disputes")))
        if len(po):
            pob = po.assign(b=batch_end_dates(po["initiated_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("payout_id", "size"))
            for b, r in pob.iterrows():
                events.append((b.date(), 5, lambda d, a=int(r.a), dm={"batch": f"payouts:{b.date()}", "rows": str(int(r.n))}: L.pay_ap(
                    d, a, payable_account="2300", memo="Merchant payouts", source="payouts", dim=dm)))
        # monthly close
        tk_m = tk.groupby((tk["created_at"].dt.year - cal.start.year) * 12 + tk["created_at"].dt.month - cal.start.month).size()
        rev_m = tx[~tx["_dup"]].groupby((tx["created_at"].dt.year - cal.start.year) * 12 + tx["created_at"].dt.month - cal.start.month)["take_cents"].sum()
        prev = {"payroll": 0, "opex": 0}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            run_rate = float(rev_m.reindex(range(max(0, m - 2), m + 1)).fillna(0).mean()) or R / 12
            opex = cents(run_rate * opex_pct)
            payroll = cents(opex * 0.65)
            other = opex - payroll
            support = int(tk_m.get(m, 0)) * p["support_cost_per_ticket_cents"]
            if m > 0:
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["opex"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex paid", source="opex")))
            pr = dict(zip(("6000", "6100", "6200"), split_cents(payroll, [0.3, 0.45, 0.25]), strict=True))
            ox = dict(zip(("6000", "6200", "6400"), split_cents(other, [0.35, 0.35, 0.30]), strict=True))
            ox["6200"] += support
            events.append((me, 7, lambda d, s=pr: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox: L.accrue_opex(d, s, memo="Operating expenses incl. support tickets")))
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
            prev = {"payroll": payroll, "opex": other + support}
        events.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in events:
            if d <= end_date:
                fn(d)
        # settlements frame (one row per settlement batch)
        rf_net = rfb.groupby("b")["amount_cents"].sum()
        lost_net = lb.groupby("b")["amount_cents"].sum()
        fee_net = fee_b * p["dispute_fee_cents"]
        st = sett.copy()
        st["refunds"] = rf_net.reindex(st.index).fillna(0).astype(np.int64)
        st["disputes"] = (lost_net.reindex(st.index).fillna(0) + fee_net.reindex(st.index).fillna(0)).astype(np.int64)
        settlements = pd.DataFrame({
            "settlement_id": [f"st_{b:%Y%m%d}" for b in st.index], "network": "provider", "settlement_date": st.index,
            "gross_cents": st["gross"].to_numpy(), "fees_cents": st["fees"].to_numpy(), "refunds_cents": st["refunds"].to_numpy(),
            "disputes_cents": st["disputes"].to_numpy(), "net_cents": (st["gross"] - st["fees"]).to_numpy(),
            "received_at": st.index + pd.Timedelta(days=2, hours=6),
            "reconciled": (st["dupc"] == 0).to_numpy(), "variance_cents": st["dupc"].to_numpy(),
        })
        settlements = settlements[settlements["settlement_date"] <= pd.Timestamp(end_date)].reset_index(drop=True)
        state.extras["settlements"] = settlements

    # ------------------------------------------------------------------ tables

    def _ledger_rows(self, state: WorldState) -> pd.DataFrame:
        """Internal payments_ledger: one charge row per processing run + refund rows (charge.refunded handler)."""
        if "pl" in state.extras:
            return state.extras["pl"]
        tx, rf, rn = state.extras["tx"], state.extras["refunds"], state.extras["runs"]
        it = state.extras["intents"]
        charges = pd.DataFrame({
            "payment_intent_id": tx["payment_intent_id"].to_numpy(), "event_id": rn["event_id"].to_numpy(),
            "merchant_id": tx["merchant_id"].to_numpy(), "buyer_id": tx["buyer_id"].to_numpy(), "entry_type": "charge",
            "amount_cents": tx["amount_cents"].to_numpy(), "created_at": rn["processed_at"].to_numpy(),
            "event_type": it["event_type"].to_numpy()[rn["_intent"].to_numpy()],
        })
        refunds = pd.DataFrame({
            "payment_intent_id": rf["payment_intent_id"].to_numpy(), "event_id": None, "merchant_id": rf["merchant_id"].to_numpy(),
            "buyer_id": None, "entry_type": "refund", "amount_cents": -rf["amount_cents"].to_numpy(),
            "created_at": rf["created_at"].to_numpy(), "event_type": "charge.refunded",
        })
        pl = pd.concat([charges, refunds], ignore_index=True)
        pl["created_at"] = pd.to_datetime(pl["created_at"])
        pl = pl.sort_values(["created_at", "payment_intent_id"], kind="stable").reset_index(drop=True)
        pl.insert(0, "ledger_entry_id", np.arange(1, len(pl) + 1, dtype=np.int64))
        pl["ingest_id"] = np.ones(len(pl), dtype=np.int64)
        state.extras["pl"] = pl
        return pl

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Provider-side and consumer-side tables + 2/4/6 dirt rules."""
        p = state.params
        g = ctx.rng.spawn("tables").generator
        tx, rf, dsp, po, tk = (state.extras[k] for k in ("tx", "refunds", "disputes", "payouts", "tickets"))
        ev, dl, rn = state.extras["events"], state.extras["deliveries"], state.extras["runs"]
        txs = tx.copy()
        disputed = set(dsp["transaction_id"])
        refunded = set(rf["transaction_id"])
        txs["status"] = np.where(txs["transaction_id"].isin(disputed), "disputed", np.where(txs["transaction_id"].isin(refunded), "refunded", "succeeded"))
        transactions = txs[["transaction_id", "payment_intent_id", "merchant_id", "buyer_id", "amount_cents", "currency", "fee_cents", "net_cents",
                            "status", "created_at", "captured_at", "card_brand", "idempotency_key", "source_event_id"]]
        payload = [json.dumps({"id": e, "type": t, "data": {"object": pi}}, separators=(",", ":")) for e, t, pi in
                   zip(ev["event_id"], ev["event_type"], ev["payment_intent_id"], strict=True)]
        events = pd.DataFrame({"event_id": ev["event_id"], "event_type": ev["event_type"], "payment_intent_id": ev["payment_intent_id"],
                               "created_at": ev["created_at"], "payload_json": payload})
        events = events.sort_values(["created_at", "event_id"], kind="stable").reset_index(drop=True)
        deliveries = dl[["delivery_id", "event_id", "attempt", "sent_at", "response_code", "response_ms", "status"]].copy()
        deliveries["response_code"] = deliveries["response_code"].astype("Int64").mask(deliveries["status"] == "timeout")
        pl = self._ledger_rows(state)
        refunds = rf[["refund_id", "transaction_id", "payment_intent_id", "amount_cents", "reason", "status", "created_at"]]
        disputes = dsp[["dispute_id", "transaction_id", "opened_at", "reason", "reason_code", "amount_cents", "status", "resolved_at", "evidence_submitted"]]
        tickets = tk.copy()
        tickets["subject"] = np.where(tickets["category"] == "duplicate_charge",
                                      np.array(("Charged twice?", "Duplicate charge on corporate card", "Two identical charges", "Please refund the double charge"))[g.integers(0, 4, len(tickets))],
                                      "Question about my order")
        dirt_plan = [
            ("transactions", "nulls:card_brand:0.05"), ("support_tickets", "case_mixed:category"),
            ("webhook_deliveries", "tz_mixed:sent_at"), ("payments_ledger", "dup_keys:0.01"),
            ("merchants", "whitespace:name"), ("disputes", "format_mixed:opened_at"),
        ]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[: {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]]:
            dirt.setdefault(t, []).append(rule)
        money = {c: "BIGINT" for c in ("amount_cents", "fee_cents", "net_cents", "gross_cents", "fees_cents", "refunds_cents", "disputes_cents",
                                        "variance_cents", "response_ms")}
        builder.add_frame("merchants", state.frames["merchants"], dirt=dirt.get("merchants", ()), column_types={"onboarded_at": "DATE"},
                          description="Sellers on the marketplace (payees).")
        builder.add_frame("transactions", transactions, primary_key=["transaction_id"], foreign_keys=[("merchant_id", "merchants", "merchant_id")],
                          column_types=money, dirt=dirt.get("transactions", ()),
                          descriptions={"payment_intent_id": "provider payment intent (one per checkout)", "idempotency_key": "client-supplied key of the checkout request",
                                        "source_event_id": "webhook event whose handler called capture"},
                          description="Provider captures (export of the provider's charge list).")
        builder.add_frame("webhook_events", events, primary_key=["event_id"], foreign_keys=[],
                          descriptions={"payload_json": "event body (abridged)"}, description="Events the provider emitted to our endpoint.")
        builder.add_frame("webhook_deliveries", deliveries, primary_key=["delivery_id"], foreign_keys=[("event_id", "webhook_events", "event_id")],
                          column_types={**money, "attempt": "INTEGER", "response_code": "INTEGER"}, dirt=dirt.get("webhook_deliveries", ()),
                          descriptions={"status": "delivered | timeout | error (provider view)", "response_ms": "capped at the provider timeout"},
                          description="Provider delivery attempts (at-least-once, exponential backoff).")
        if p["processed_events_table"] != "absent":
            pe = rn[["event_id", "processed_at"]].copy()
            pe["handler_version"] = "v2-sync"
            pe["result"] = "ok"
            if p["processed_events_table"] == "unique_event_id":
                pe = pe.drop_duplicates("event_id", keep="first")
                desc = "Consumer dedupe table: one row per event_id (UNIQUE(event_id))."
            else:
                desc = "Consumer processing log (no uniqueness constraint)."
            pe = pe.sort_values(["processed_at", "event_id"], kind="stable").reset_index(drop=True)
            builder.add_frame("processed_events", pe, primary_key=["event_id"] if p["processed_events_table"] == "unique_event_id" else [],
                              foreign_keys=[("event_id", "webhook_events", "event_id")], description=desc)
        builder.add_frame("payments_ledger", pl, primary_key=["ledger_entry_id"], foreign_keys=[("merchant_id", "merchants", "merchant_id")],
                          column_types={**money, "ledger_entry_id": "BIGINT"}, dirt=dirt.get("payments_ledger", ()),
                          descriptions={"entry_type": "charge | refund", "ingest_id": ">1 marks a re-ingested copy of the same ledger_entry_id"},
                          description="Internal payments ledger written by the webhook consumer.")
        builder.add_frame("refunds", refunds, primary_key=["refund_id"], foreign_keys=[("transaction_id", "transactions", "transaction_id")], column_types=money)
        builder.add_frame("disputes", disputes, primary_key=["dispute_id"], foreign_keys=[("transaction_id", "transactions", "transaction_id")],
                          column_types={**money, "opened_at": "DATE", "resolved_at": "DATE"}, dirt=dirt.get("disputes", ()))
        builder.add_frame("support_tickets", tickets, primary_key=["ticket_id"], foreign_keys=[("transaction_id", "transactions", "transaction_id")],
                          dirt=dirt.get("support_tickets", ()), description="Customer support tickets.")
        builder.add_frame("settlements", state.extras["settlements"], primary_key=["settlement_id"], column_types={**money, "settlement_date": "DATE"},
                          descriptions={"variance_cents": "settled gross not matched to an order (finance recon)", "reconciled": "finance recon matched the batch"})
        if len(po):
            builder.add_frame("payouts", po[["payout_id", "merchant_id", "amount_cents", "currency", "initiated_at", "arrived_at", "status", "method"]],
                              column_types=money)

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """Refund liability, seller payable and revenue hold; payments_ledger vs GL volume breaks by design."""
        cal, L = state.calendar, state.ledger
        tx, rf, dsp, po = (state.extras[k] for k in ("tx", "refunds", "disputes", "payouts"))
        pl = self._ledger_rows(state)
        liab, seller, rev, internal, gl_vol, brk = {}, {}, {}, {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            s_ts = pd.Timestamp(s)
            e_ts = pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            dup = tx["_dup"]
            dup_c = int(tx.loc[dup & (tx["created_at"] <= e_ts), "amount_cents"].sum())
            dup_r = int(rf.loc[rf["_dup"] & (rf["created_at"] <= e_ts), "amount_cents"].sum())
            dup_d = int(dsp.loc[dsp["_dup"] & (dsp["status"] == "lost") & (dsp["resolved_at"] <= e_ts), "amount_cents"].sum())
            liab[period] = dup_c - dup_r - dup_d
            lost_b = dsp[~dsp["_dup"] & (dsp["status"] == "lost") & (dsp["resolved_at"] <= e_ts)]
            seller[period] = int(tx.loc[tx["created_at"] <= e_ts, "seller_cents"].sum() - rf.loc[rf["created_at"] <= e_ts, "_seller"].sum()
                                 - lost_b["_seller"].sum() - (po.loc[po["initiated_at"] <= e_ts, "amount_cents"].sum() if len(po) else 0))
            in_p = (tx["created_at"] >= s_ts) & (tx["created_at"] <= e_ts)
            rf_p = (rf["created_at"] >= s_ts) & (rf["created_at"] <= e_ts)
            rev[period] = int(tx.loc[in_p, "take_cents"].sum() - rf.loc[rf_p, "_take"].sum())
            pl_p = (pl["created_at"] >= s_ts) & (pl["created_at"] <= e_ts)
            internal[period] = int(pl.loc[pl_p, "amount_cents"].sum())
            gl_vol[period] = (L.sum_lines(period, ["4020", "2300"], "credit", "sales")
                              - L.sum_lines(period, ["4100", "2300"], "debit", "refunds"))
            brk[period] = int(tx.loc[in_p & dup, "amount_cents"].sum() - rf.loc[rf_p & rf["_dup"], "amount_cents"].sum())
        state.extras["gap_by_period"] = brk
        return [
            SubledgerTie("refund_liability_open_duplicates", liab, ["2450"], description="Duplicate captures not yet refunded or charged back == GL 2450"),
            SubledgerTie("seller_payable", seller, ["2300"], description="Seller credits - refunds - lost disputes - payouts == GL 2300"),
            SubledgerTie("revenue_take", rev, ["4020", "4100"], basis="activity", description="Take on unique captures less refunded take == net revenue"),
            SubledgerTie("payments_ledger_vs_gl_volume", internal, ledger_cents=gl_vol, expected_break_cents=brk,
                         description="Internal ledger net volume vs GL settled volume: breaks by duplicates not yet refunded (designed)"),
        ]

    # ------------------------------------------------------------------ figures

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        cal = state.calendar
        tx, dsp, dl, rf, tk = (state.extras[k] for k in ("tx", "disputes", "deliveries", "refunds", "tickets"))
        P8 = state.periods[-1]
        s8, e8 = cal.bounds(P8)
        s8t, e8t = pd.Timestamp(s8), pd.Timestamp(e8) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        pl = self._ledger_rows(state)
        charges = pl[pl["entry_type"] == "charge"]
        dup_rows = charges[charges.duplicated("payment_intent_id", keep="first")]
        win_mask = dl["_window"].to_numpy()
        tx8 = tx[(tx["created_at"] >= s8t) & (tx["created_at"] <= e8t)]
        d8 = dsp[(dsp["opened_at"] >= s8t) & (dsp["opened_at"] <= e8t)]
        dup_tx = tx[tx["_dup"]]
        dup_disputes = dsp[dsp["_dup"]]
        dup_tickets = tk[tk["category"] == "duplicate_charge"]
        cost = int(dup_tx["fee_cents"].sum()) + len(dup_disputes) * state.params["dispute_fee_cents"] + len(dup_tickets) * state.params["support_cost_per_ticket_cents"]
        f = {
            "dup_count": int(len(dup_rows)), "dup_cents": int(dup_rows["amount_cents"].sum()),
            "timeout_share_windows": float((dl.loc[win_mask, "status"] == "timeout").mean()) if win_mask.any() else 0.0,
            "window_deliveries": int(win_mask.sum()),
            "dispute_rate_p8": len(d8) / max(1, len(tx8)), "disputes_p8": len(d8), "tx_p8": len(tx8),
            "cost": cost, "dup_fees": int(dup_tx["fee_cents"].sum()), "dup_dispute_n": len(dup_disputes), "dup_ticket_n": len(dup_tickets),
            "gap_p8": int(state.extras["gap_by_period"][P8]),
            "dup_in_windows": int(sum(((dup_rows["created_at"] >= a) & (dup_rows["created_at"] <= b + pd.Timedelta(hours=2))).sum() for a, b in state.extras["windows"])),
            "refunds_p8": int(rf[(rf["created_at"] >= s8t) & (rf["created_at"] <= e8t)]["amount_cents"].sum()),
        }
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Consumer queueing (normal vs incident), stations, unit economics and public KPIs."""
        p, cal = state.params, state.calendar
        tx, dsp, tk, dl = (state.extras[k] for k in ("tx", "disputes", "tickets", "deliveries"))
        st = state.statements
        kpis = {}
        for period in state.periods:
            s, e = cal.bounds(period)
            s_t, e_t = pd.Timestamp(s), pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            txp = tx[(tx["created_at"] >= s_t) & (tx["created_at"] <= e_t)]
            dp = dsp[(dsp["opened_at"] >= s_t) & (dsp["opened_at"] <= e_t)]
            tkp = tk[(tk["created_at"] >= s_t) & (tk["created_at"] <= e_t)]
            inc = next(i for i in st.income if i.period == period)  # type: ignore[union-attr]
            kpis[period] = {"transactions": float(len(txp)), "gpv_cents": float(txp["amount_cents"].sum()), "revenue_cents": float(inc.revenue_cents),
                            "net_income_cents": float(inc.net_income_cents), "disputes_opened": float(len(dp)),
                            "dispute_rate": len(dp) / max(1, len(txp)), "support_tickets": float(len(tkp)),
                            "cash_cents": float(state.ledger.trial_balance(period)["1000"])}
        lam = 1.0 / 60.0  # events per second at normal load (per consumer worker)
        normal = mm1(lam, 1.0 / 0.9)
        busy = mm1(0.95 / 0.9 * 1.0, 1.0 / 0.9)
        stations, summary = station_network(1.0, [{"name": "receiver_validate", "capacity_per_hour": 20.0, "cs": 0.5},
                                                  {"name": "ledger_write", "capacity_per_hour": 4.0, "cs": 1.2},
                                                  {"name": "capture_call", "capacity_per_hour": 1.6, "cs": 1.5}])
        q = {"lambda_normal": lam, "W_normal_s": normal["W"], "rho_incident": busy["rho"], "W_incident_s": busy["W"],
             "provider_timeout_s": float(p["consumer_timeout_s"]), "throughput": summary["throughput"]}
        unit = {"take_rate": p["take_rate"], "avg_ticket_cents": float(tx["amount_cents"].mean()), "processing_fee_rate": FEE_RATE,
                "gross_margin": float(np.mean([st.ratios[x]["gross_margin"] for x in state.periods])),  # type: ignore[union-attr]
                "cac": 0.0, "ltv": 0.0, "payback_months": 0.0, "arpa": float(tx["amount_cents"].mean() * p["take_rate"]), "churn_monthly": 0.0}
        return build_operations_state(stations=stations, queueing=q, unit_economics=unit,
                                      supply_chain={"webhook_retry_policy": {"max_attempts": p["max_attempts"], "base_backoff_s": 60, "multiplier": 2,
                                                                             "provider_timeout_s": p["consumer_timeout_s"]}},
                                      pricing={"take_rate": p["take_rate"], "card_fee": {"rate": FEE_RATE, "fixed_cents": FEE_FIXED},
                                               "dispute_fee_cents": p["dispute_fee_cents"]},
                                      kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Metrics of the section 9 key table, reference SQL and reference design."""
        p = state.params
        f = self._facts(state)
        P = state.periods
        P8 = P[-1]
        windows = state.extras["windows"]
        dedupe_code = {"none": 0.0, "event_id": 1.0}[p["dedupe"]]
        metrics = [
            metric("duplicate_charge_count", f["dup_count"], "count",
                   "payments_ledger charge rows beyond the first per payment_intent_id (after removing re-ingested copies with ingest_id > 1).",
                   pillar="sql", tol_rel=0.05, label="Duplicate charges"),
            metric("duplicate_charge_cents", f["dup_cents"], "cents", "Sum of amount_cents of those duplicate charge rows.",
                   pillar="sql", tol_rel=0.05, label="Duplicate charge amount"),
            metric("incident_window_count", len(windows), "count",
                   "Windows where webhook_deliveries timeouts cluster (p99 latency above the provider timeout): "
                   + "; ".join(f"{a:%Y-%m-%d %H:%M}-{b:%H:%M}" for a, b in windows) + ".",
                   pillar="system_design", tol_abs=0.0, label="Incident windows"),
            metric("timeout_share_in_incidents", f["timeout_share_windows"], "ratio",
                   f"Share of delivery attempts sent inside the incident windows with status='timeout' ({f['window_deliveries']} attempts).",
                   pillar="system_design", tol_abs=0.05, label="Timeout share inside incidents"),
            metric("dedupe_present_code", dedupe_code, "code",
                   "0 = no dedupe, 1 = dedupe on the wrong key (event_id), 2 = dedupe on payment_intent_id / idempotency key.",
                   pillar="system_design", tol_abs=0.0, label="Dedupe in place (0 none / 1 wrong key / 2 correct)"),
            metric("ledger_vs_provider_gap_cents_p8", f["gap_p8"], "cents",
                   f"{P8}: payments_ledger net amount (charges - refunds) minus GL settled volume (4020 + 2300 credits less baseline refunds) "
                   "= duplicate captures in the quarter not offset by duplicate refunds.",
                   pillar="accounting_finance", tol_rel=0.05, label="Ledger vs provider gap, latest quarter"),
            metric("dispute_rate_p8", f["dispute_rate_p8"], "ratio", f"Disputes opened in {P8} / provider transactions created in {P8} "
                   f"({f['disputes_p8']} / {f['tx_p8']}).", pillar="accounting_finance", tol_abs=0.001, label="Dispute rate, latest quarter"),
            metric("cost_of_duplicates_cents", f["cost"], "cents",
                   f"Processing fees kept on duplicate captures ({f['dup_fees']}) + $15 x {f['dup_dispute_n']} duplicate disputes + "
                   f"support cost x {f['dup_ticket_n']} duplicate_charge tickets.", pillar="accounting_finance", tol_rel=0.10, label="Cost of duplicates"),
        ]
        sql_dups = """
WITH pl AS (
  SELECT * FROM payments_ledger
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ledger_entry_id ORDER BY ingest_id) = 1)
SELECT payment_intent_id, COUNT(*) AS n, SUM(amount_cents) AS total,
       MIN(created_at) AS first_at, MAX(created_at) AS last_at
FROM pl WHERE entry_type = 'charge'
GROUP BY 1 HAVING COUNT(*) > 1 ORDER BY last_at"""
        q_dups = run_reference_query(con, "duplicate_ledger_entries", sql_dups, signature=["qualify", "group by", "having", "count("],
                                     metric_keys=["duplicate_charge_count", "duplicate_charge_cents"],
                                     checks=lambda df: {"duplicate_rows": int((df["n"] - 1).sum()), "intents_affected": int(len(df))})
        sql_retry = """
SELECT d.event_id, COUNT(*) AS attempts,
       SUM(CASE WHEN d.status = 'timeout' THEN 1 END) AS timeouts,
       MAX(d.response_ms) AS max_ms
FROM webhook_deliveries d GROUP BY 1
HAVING COUNT(*) > 1 AND SUM(CASE WHEN d.status = 'timeout' THEN 1 END) > 0
ORDER BY attempts DESC, d.event_id"""
        q_retry = run_reference_query(con, "retried_deliveries_with_timeouts", sql_retry, signature=["group by", "having", "case when"],
                                      metric_keys=["timeout_share_in_incidents", "incident_window_count"],
                                      checks=lambda df: {"events_retried_after_timeout": int(len(df)), "max_response_ms": int(df["max_ms"].max())})
        sql_gap = """
WITH pl AS (
  SELECT * FROM payments_ledger QUALIFY ROW_NUMBER() OVER (PARTITION BY ledger_entry_id ORDER BY ingest_id) = 1),
internal AS (SELECT period_of(created_at) AS period, SUM(amount_cents) AS internal_cents FROM pl GROUP BY 1),
gl AS (
  SELECT j.period,
         SUM(CASE WHEN j.source = 'sales' AND l.account_code IN ('4020', '2300') THEN l.credit_cents ELSE 0 END)
       - SUM(CASE WHEN j.source = 'refunds' AND l.account_code IN ('4100', '2300') THEN l.debit_cents ELSE 0 END) AS gl_cents
  FROM gl_lines l JOIN gl_journal j USING (entry_id) GROUP BY 1)
SELECT period, internal_cents, gl_cents, internal_cents - gl_cents AS gap_cents
FROM internal JOIN gl USING (period) ORDER BY period"""
        q_gap = run_reference_query(con, "ledger_vs_gl_gap_by_quarter", sql_gap, signature=["join", "group by", "case when", "sum("],
                                    metric_keys=["ledger_vs_provider_gap_cents_p8"],
                                    checks=lambda df: {"gap_last_quarter": int(df["gap_cents"].iloc[-1])})
        sql_disp = """
WITH d AS (
  SELECT reason, COALESCE(TRY_CAST(opened_at AS DATE),
                          CAST(TRY_STRPTIME(CAST(opened_at AS VARCHAR), '%m/%d/%Y') AS DATE)) AS opened
  FROM disputes),
dq AS (
  SELECT period_of(opened) AS period, COUNT(*) AS disputes,
         COUNT(*) FILTER (WHERE reason = 'duplicate_processing') AS duplicate_disputes
  FROM d GROUP BY 1),
tq AS (SELECT period_of(created_at) AS period, COUNT(*) AS transactions FROM transactions GROUP BY 1)
SELECT period, disputes, duplicate_disputes, transactions, disputes * 1.0 / transactions AS dispute_rate
FROM dq JOIN tq USING (period) ORDER BY period"""
        q_disp = run_reference_query(con, "disputes_by_reason_and_quarter", sql_disp, signature=["with", "join", "group by", "count("],
                                     metric_keys=["dispute_rate_p8", "cost_of_duplicates_cents"],
                                     checks=lambda df: {"dispute_rate_last": float(df["dispute_rate"].iloc[-1])})
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_dups, q_retry],
            "magnitude": {**anomalies["A1"].magnitude, "duplicate_charge_count": float(f["dup_count"]), "duplicate_charge_cents": float(f["dup_cents"]),
                          "timeout_share_in_incidents": round(f["timeout_share_windows"], 6), "dedupe_present_code": dedupe_code},
            "evidence_trail": [f"{f['dup_count']} duplicate charge rows ({f['dup_in_windows']} inside or right after incident windows).",
                               "Each duplicate's first delivery attempt has status='timeout' and a retry ~1-2 minutes later.",
                               "processed_events is absent / a plain log / keyed on event_id - not on payment_intent_id."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_disp],
            "magnitude": {**anomalies["A2"].magnitude, "dispute_rate_p8": round(f["dispute_rate_p8"], 6), "cost_of_duplicates_cents": float(f["cost"])},
            "evidence_trail": [f"Dispute rate {f['dispute_rate_p8']:.2%} in {P8}; duplicate_processing (12.6.1) is a growing reason code.",
                               f"{f['dup_ticket_n']} duplicate_charge support tickets."]})
        gap = state.extras["gap_by_period"]
        anomalies["A3"] = anomalies["A3"].model_copy(update={
            "reference_sql": [q_gap],
            "magnitude": {"ledger_vs_provider_gap_cents_p8": float(f["gap_p8"]), **{f"gap_{k}": float(v) for k, v in gap.items()}},
            "evidence_trail": ["settlements.reconciled = false exactly on batches that contain duplicate captures; GL 2450 carries the open amount.",
                               "payments_ledger minus GL volume by quarter equals duplicates not yet refunded."]})
        herrings_all = [
            "Fraud is up - but the disputes are reason duplicate_processing (12.6.1): same card, same amount, minutes apart.",
            "Support is slow and that is why disputes rise - tickets exist because of the duplicates; response time is secondary.",
            "The provider is double-sending maliciously - at-least-once delivery with retries is the documented contract; our consumer is non-idempotent.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        key = "payment_intent_id"
        root = (f"After the webhook consumer moved to synchronous processing in {state.extras['migration_at']:%Y-%m-%d}, handlers that run past the "
                f"provider's {p['consumer_timeout_s']}s timeout are retried even though they already committed. The consumer has "
                + ("no dedupe" if p["dedupe"] == "none" else "dedupe keyed on event_id, but the provider re-issues a new event_id when it retries payment_intent.succeeded")
                + f", so each retry writes a second payments_ledger row and calls capture again without an idempotency key: {f['dup_count']} duplicate charges "
                f"({f['dup_cents'] / 100:,.0f} USD), clustered in {len(windows)} incident windows where p99 latency exceeded the timeout. Duplicates turn into "
                f"refunds, duplicate_processing disputes (rate {f['dispute_rate_p8']:.2%} in {P8}) and support load, and the internal ledger runs ahead of "
                f"the provider-settled GL. The fix is an idempotent receiver keyed on {key} (unique constraint, ack-then-process) plus idempotency keys on capture.")
        return GroundTruth(
            root_cause=root, anomalies=[anomalies[k] for k in ("A1", "A2", "A3")], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q_dups.sql, q_retry.sql, q_gap.sql, q_disp.sql],
                python=["import pandas as pd\n"
                        "pl = pd.read_sql('SELECT * FROM payments_ledger', con).sort_values('ingest_id').drop_duplicates('ledger_entry_id')\n"
                        "ch = pl[pl.entry_type == 'charge'].sort_values('created_at')\n"
                        "dups = ch[ch.duplicated('payment_intent_id')]\n"
                        "print(len(dups), dups.amount_cents.sum())\n"
                        "print(dups.set_index(pd.to_datetime(dups.created_at)).resample('h').size().sort_values().tail())  # incident windows\n"],
                memo_bullets=[
                    "At-least-once delivery is the provider contract; exactly-once effects require an idempotent receiver.",
                    f"Correct idempotency key: {key} (or the provider idempotency key on capture), not event_id.",
                    "Insert into processed_events(event_key) with a UNIQUE constraint in the same transaction as the ledger write; ON CONFLICT DO NOTHING.",
                    "Ack (200) immediately and process asynchronously so handler latency can never exceed the provider timeout.",
                    "Backfill: refund open duplicates proactively before they become disputes; reconcile payments_ledger to settlements.",
                    "Do not suppress retries - that trades duplicates for lost events.",
                ],
                design_mermaid=FIXED_MERMAID),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, stakeholders, timeline and the flawed architecture diagram."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        names = rng.pick_names(4)
        wins = state.extras["windows"]
        reveal = [
            f"The dispute rate reached {f['dispute_rate_p8']:.2%} in {P[-1]} and is rising.",
            "Support tickets mention customers being 'charged twice'.",
            f"The payment provider's status page reported an incident in {state.calendar.labels([wins[0][0]])[0]}.",
            "Finance complains that 'our ledger and the provider don't match'.",
            f"Provider retry policy: up to {p['max_attempts']} attempts with exponential backoff and a {p['consumer_timeout_s']}s timeout.",
        ]
        conceal = [
            "the timeout race between handler latency and the provider timeout",
            "absence of dedupe / dedupe on the wrong key (event_id instead of payment_intent_id) - no idempotency key on capture",
            "the incident windows and their timestamps",
            f"the reconciliation gap amount ({f['gap_p8']} cents in {P[-1]})",
            "A1", "A2", "A3",
        ]
        stakeholders = [
            Stakeholder(name=names[0], role="VP Engineering", agenda="Blame the provider's retries.", disposition="defensive",
                        knows=["the consumer was migrated to synchronous processing"], hides=["the consumer does its work before responding"]),
            Stakeholder(name=names[1], role="Head of Payments Ops", agenda="Stop the manual refund grind.", disposition="exhausted",
                        knows=["refunds are processed by hand from tickets"], hides=[]),
            Stakeholder(name=names[2], role="Controller", agenda="Close the books with a clean reconciliation.", disposition="precise",
                        knows=["a growing 'unreconciled' line in the monthly settlement recon"], hides=[]),
            Stakeholder(name=names[3], role="Risk Lead", agenda="Stay out of the card-network monitoring programs.", disposition="alarmed",
                        knows=["the 0.9% dispute-ratio threshold (VDMP / ECP)"], hides=["has already tightened fraud rules"]),
        ]
        tl = [
            TimelineEvent(period=P[4], headline="Migrated webhook consumer to new service (sync processing)", detail="Old async worker retired."),
            TimelineEvent(period=state.calendar.labels([wins[0][0]])[0], headline="Provider incident (~40 min)", detail="Provider status page shows elevated latency."),
            TimelineEvent(period=P[6], headline="Database migration, p99 up", detail="payments DB moved to a new cluster."),
            TimelineEvent(period=P[6], headline="Support backlog", detail="Ticket queue doubles."),
            TimelineEvent(period=P[7], headline=f"Dispute ratio {f['dispute_rate_p8']:.2%}; network warning letter", detail="Card network sends an early warning."),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[5], headline="Fraud rules tightened", detail="Risk raises the fraud score threshold.", is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[6], headline="Provider changes retry schedule", detail="Release note mentions faster first retry.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["architecture_diagram", "oncall_postmortem_excerpt", "network_warning_letter", "provider_docs_quote"],
                           architecture_mermaid=FLAWED_MERMAID.format(timeout=p["consumer_timeout_s"]))

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Spec levers; categorical levers are integer-coded."""
        p = state.params
        return [
            Lever(key="implement_dedupe", label="Dedupe key (0 none, 1 event_id, 2 payment_intent_id)", min=0, max=2,
                  default={"none": 0.0, "event_id": 1.0}[p["dedupe"]], unit="code", effect="Duplicates go to ~0 only with the payment_intent_id key."),
            Lever(key="ack_before_process", label="Ack before processing (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Respond 200 immediately and process async: timeouts ~0."),
            Lever(key="backfill_refunds", label="Proactively refund open duplicates (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="One-off refund cash out; avoids the disputes those duplicates would cause."),
            Lever(key="raise_consumer_timeout_s", label="Provider timeout / handler budget", min=10, max=120, default=float(p["consumer_timeout_s"]), unit="s",
                  effect="Narrows the race, does not close it."),
            Lever(key="rate_limit_retries", label="Suppress provider retries (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Fewer duplicates but legitimate events are lost."),
            Lever(key="dispute_response_effort", label="Dispute response effort (0 low / 1 high)", min=0, max=1, default=0, unit="code",
                  effect="Win rate 20% -> 45% on contestable disputes."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model (module docstring)."""
        f = self._facts(state)
        P = state.periods
        st = state.statements
        tx, dsp = state.extras["tx"], state.extras["disputes"]
        P8 = P[-1]
        s8, e8 = state.calendar.bounds(P8)
        s8t, e8t = pd.Timestamp(s8), pd.Timestamp(e8) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        dup8 = tx[tx["_dup"] & (tx["created_at"] >= s8t) & (tx["created_at"] <= e8t)]
        dl = state.extras["deliveries"]
        dl8 = dl[(dl["sent_at"] >= s8t) & (dl["sent_at"] <= e8t)]
        timeout_rate = float((dl8["status"] == "timeout").mean()) if len(dl8) else 0.0
        open_dup = -state.ledger.trial_balance(P8)["2450"]
        dup_dispute_contrib = len(dsp[dsp["_dup"] & (dsp["opened_at"] >= s8t)]) / max(1, f["tx_p8"])
        inc8 = next(i for i in st.income if i.period == P8)  # type: ignore[union-attr]
        lost_base = float(dsp[~dsp["_dup"] & (dsp["status"] == "lost")]["_take"].sum()) / max(1, len(P))
        kpis = {
            "duplicate_charges": float(len(dup8)), "duplicate_cents": float(dup8["amount_cents"].sum()),
            "timeout_rate": timeout_rate, "dispute_rate": f["dispute_rate_p8"], "open_duplicate_liability_cents": float(open_dup),
            "support_tickets": 0.0,
            "net_income_cents": float(inc8.net_income_cents), "cash_cents": float(state.ledger.trial_balance(P8)["1000"]),
            "lost_events": 0.0, "network_program_fees_cents": 0.0,
        }
        tk = state.extras["tickets"]
        kpis["support_tickets"] = float(((tk["created_at"] >= s8t) & (tk["created_at"] <= e8t)).sum())
        d = kpis["duplicate_charges"]
        dc = kpis["duplicate_cents"]
        effects = [
            {"lever": "implement_dedupe", "kpi": "duplicate_charges", "delta_per_unit": -d / 2, "lag_periods": 0},
            {"lever": "implement_dedupe", "kpi": "duplicate_cents", "delta_per_unit": -dc / 2, "lag_periods": 0},
            {"lever": "implement_dedupe", "kpi": "dispute_rate", "delta_per_unit": -dup_dispute_contrib / 2, "lag_periods": 1},
            {"lever": "ack_before_process", "kpi": "timeout_rate", "delta_per_unit": -0.9 * timeout_rate, "lag_periods": 0},
            {"lever": "ack_before_process", "kpi": "duplicate_charges", "delta_per_unit": -0.9 * d, "lag_periods": 0},
            {"lever": "ack_before_process", "kpi": "duplicate_cents", "delta_per_unit": -0.9 * dc, "lag_periods": 0},
            {"lever": "ack_before_process", "kpi": "dispute_rate", "delta_per_unit": -0.9 * dup_dispute_contrib, "lag_periods": 1},
            {"lever": "backfill_refunds", "kpi": "cash_cents", "delta_per_unit": -float(open_dup), "lag_periods": 0},
            {"lever": "backfill_refunds", "kpi": "open_duplicate_liability_cents", "delta_per_unit": -float(open_dup), "lag_periods": 0},
            {"lever": "backfill_refunds", "kpi": "dispute_rate", "delta_per_unit": -0.5 * dup_dispute_contrib, "lag_periods": 1},
            {"lever": "raise_consumer_timeout_s", "kpi": "timeout_rate", "elasticity": -0.6, "lag_periods": 0},
            {"lever": "raise_consumer_timeout_s", "kpi": "duplicate_charges", "elasticity": -0.5, "lag_periods": 0},
            {"lever": "rate_limit_retries", "kpi": "duplicate_charges", "delta_per_unit": -0.5 * d, "lag_periods": 0},
            {"lever": "rate_limit_retries", "kpi": "lost_events", "delta_per_unit": float(len(dl8[dl8["status"] != "delivered"])), "lag_periods": 0},
            {"lever": "dispute_response_effort", "kpi": "net_income_cents", "delta_per_unit": 0.25 * lost_base, "lag_periods": 1},
        ]
        n = 3
        rate_p5 = self._dispute_rate(state, P[4])
        drag = {
            "dispute_rate": (f["dispute_rate_p8"] - rate_p5) / n,
            "cash_cents": -float(f["cost"]) / max(1, len(P[4:])),
            "support_tickets": float(f["dup_ticket_n"]) / max(1, len(P[4:])),
            "network_program_fees_cents": 25_000 * 100 * 3 if f["dispute_rate_p8"] >= 0.009 else 0.0,
        }
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"implement_dedupe": 2.0, "ack_before_process": 1.0}}

    def _dispute_rate(self, state: WorldState, period: str) -> float:
        tx, dsp = state.extras["tx"], state.extras["disputes"]
        s, e = state.calendar.bounds(period)
        s_t, e_t = pd.Timestamp(s), pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        n_tx = int(((tx["created_at"] >= s_t) & (tx["created_at"] <= e_t)).sum())
        n_d = int(((dsp["opened_at"] >= s_t) & (dsp["opened_at"] <= e_t)).sum())
        return n_d / max(1, n_tx)
