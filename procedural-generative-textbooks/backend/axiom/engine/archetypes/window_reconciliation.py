"""``window_reconciliation`` - "Tie-Out" (archetypes.md section 12).

A B2B subscription business bills every customer monthly on its billing anniversary. Audit
fieldwork starts and the AR sub-ledger (``invoices`` - ``payments`` - ``credit_memos`` -
``write_offs``) does not tie to GL 1100. The learner must reconcile with window functions
(dedupe with ``QUALIFY ROW_NUMBER()``, roll-forward with ``SUM() OVER``, ``LAG`` by period) and
anti-joins, and produce a reconciling-items table that nets to zero.

Mechanics
---------
* Customers join over time and churn; each active customer gets one invoice a month in advance (``Dr 1100 /
  Cr 4000``; the unearned part of the service month is deferred to 2200 at each quarter end by an
  auto-reversing close entry, source ``revenue_deferral``), net-30; payments arrive with
  a realistic delay mixture (on time, late tail, ~1.5% never - written off after ~6 months against
  the allowance). Credit memos (partial, before payment) reduce the invoice; the customer pays the
  net amount.
* The GL is posted in batches (``GL_BATCH``) but the AR control-account lines are **per document**:
  every ``gl_lines`` row on account 1100 carries the document ids in ``dimensions``
  (``invoice_id=...;payment_id=...;memo_id=...``), the invoice in ``dimension_invoice_id`` and the
  customer in ``dimension_customer_id``, so the sub-ledger can be tied to the GL line by line (the
  reference SQL uses the typed ``dimension_invoice_id``; ``regexp_extract(dimensions, 'invoice_id=([^;]+)', 1)``
  is the equivalent alternative and the only route for ``payment_id`` / ``memo_id`` / ``writeoff_id``).
* Monthly close: hosting COGS, payroll, opex, depreciation, allowance provision (``allowance_pct``
  of GL AR), tax; FX gains/losses to 7200 (principal).

Designed breaks (sub-ledger vs GL; each with an exact signed amount; difficulty picks 3 / 4 / 5)
-----------------------------------------------------------------------------------------------
* ``dup_invoices`` (all): the P7 ingestion rerun re-loaded the invoices of a few days a second time
  (``ingest_id = 2``, ``created_at`` a few seconds later). Naive sums overstate AR; dedupe fixes it.
* ``unapplied_cash`` (all): P7-P8 wires without remittance (``payments.invoice_id IS NULL``); the GL
  credited 1100 with the customer only, the sub-ledger still shows the invoices open.
* GL omission through suspense (all; seeded choice): ``credit_memo_unposted`` - the billing-to-GL
  interface rejected the AR line of some credit memos, posting ``Dr 4100 / Cr 1400 suspense``; or
  ``writeoff_one_sided`` - write-offs posted ``Dr 1110 / Cr 1400 suspense``. Either way GL 1100 is
  not relieved and the suspense balance (account 1400, a *credit* balance) is the visible clue.
* ``cutoff_shift`` (practitioner+): invoices dated in the last 2 days of P7 were posted to the GL on
  the first day of P8 (visible in the invoice-period vs GL-period join), and those dated in the last
  2 days of P8 had not been posted at close (anti-join) - the latter is a P8 reconciling item.
* ``fx_rounding`` (principal): multi-currency invoicing went live in P8 for ``fx_share`` of customers
  (EUR/GBP). Invoices and payments are stored in the invoice currency (``amount_cents`` in minor
  units - a unit trap for naive sums); the sub-ledger converts at the month-average rate of the
  invoice month, the GL at the daily rate of the posting date; open FX invoices differ by a small
  amount (realised differences on payment go to 7200).

Ties declared (``subledger_ties``): naive and deduplicated sub-ledger AR vs GL 1100 (designed breaks
with exact expected amounts per period), suspense items vs GL 1400 (hold), and revenue per invoices
vs GL 4000 activity (designed break: cutoff and FX). Statements always balance.

Documented simplifications
* Industries saas / ecommerce / fintech_marketplace share the same B2B monthly-invoicing model
  (``saas_billing`` schema); revenue account 4000 (saas) or 4020 (others).
* ``reversed_entry_unreversed_subledger`` is not generated; ``invoice_lines`` is omitted (row budget).
* ``gl_lines`` has typed ``dimension_invoice_id`` (shared datagen schema); payment / memo / write-off
  ids live only in ``gl_lines.dimensions``.
* Quarter simulation (reduced form): KPIs are P8 levels; ``post_adjusting_entries`` clears suspense and
  the GL-side items; ``fix_ingestion_idempotency`` stops new duplicates; ``remittance_matching_rule``
  (0 none / 1 amount+customer / 2 ML) cuts unapplied cash 45% per step; ``allowance_true_up_pct`` adds
  allowance on 90+ aging. While unresolved each quarter adds the P7-P8 average new break amounts.
  ``resolved_if``: adjusting entries posted and ingestion made idempotent.
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
    DEFERRAL_SOURCE,
    post_deferred_revenue_trueups,
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
from axiom.engine.finance import SubledgerTie, batch_end_dates, cents, cents_array, split_cents
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state
from axiom.engine.rng import SeededRNG
from axiom.models import (
    Anomaly,
    Difficulty,
    DossierSeed,
    GroundTruth,
    JournalLine,
    Lever,
    OperationsState,
    ReferenceSolution,
    Stakeholder,
    TimelineEvent,
)

logger = logging.getLogger("axiom.archetypes.window_reconciliation")

EOD = pd.Timedelta(hours=23, minutes=59, seconds=59)
PAYROLL_SPLIT = {"6000": 0.35, "6100": 0.40, "6200": 0.25}
OPEX_SPLIT = {"6000": 0.45, "6200": 0.30, "6400": 0.25}
SEGMENTS = ("smb", "midmarket", "enterprise")
COUNTRIES_FX = {"EUR": ("DE", "FR", "NL", "ES"), "GBP": ("GB",)}
BREAK_ORDER = ("dup_invoices", "unapplied_cash", "gl_omission", "cutoff_shift", "fx_rounding")
LABEL = {"dup_invoices": "Duplicate invoice rows from the ingestion rerun", "unapplied_cash": "Unapplied cash (wires without remittance)",
         "credit_memo_unposted": "Credit memos parked in suspense instead of relieving AR",
         "writeoff_one_sided": "Write-offs parked in suspense instead of relieving AR",
         "cutoff_shift": "Invoices posted to the GL in a different quarter", "fx_rounding": "Multi-currency conversion differences"}


def _month_index(ts: pd.Series, start: dt.date) -> np.ndarray:
    t = pd.to_datetime(ts)
    return np.asarray((t.dt.year - start.year) * 12 + (t.dt.month - start.month))


def _ar_line(account: str, debit: int, credit: int, customer: str, **ids: str) -> JournalLine:
    dim = {"customer_id": customer, **{k: v for k, v in ids.items() if v}}
    return JournalLine(account=account, debit_cents=int(debit), credit_cents=int(credit), dimension=dim)


@register
class WindowReconciliation(Archetype):
    """Tie-Out: AR sub-ledger vs GL with duplicates, unapplied cash, suspense postings, cutoff and FX."""

    key = "window_reconciliation"
    title = "Tie-Out"
    pillars = ("sql", "accounting_finance", "python")
    industries = ("saas", "ecommerce", "fintech_marketplace")
    schema_template = "saas_billing"
    summary = ("Audit fieldwork starts and the receivables sub-ledger does not tie to the general ledger; a suspense balance "
               "has appeared and the controller needs an itemised tie-out by Friday.")
    root_cause_terms = ("duplicate", "unapplied cash", "ingest_id", "cutoff", "interface")
    bs_caption_exemptions = {"ecommerce:inventor": "platform-fee business: the merchants own the goods"}

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 12 (difficulty: number of break types, magnitude, dirt, red herrings)."""
        ind = industry or "saas"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")

        def band(metric_key: str, q_lo: float, q_hi: float, ind_: str = ind) -> float:
            return bk.sample_between(pool, rng.spawn(f"bench:{metric_key}"), f"{ind_}.{metric_key}", ind_, q_lo, q_hi)

        omission = str(rng.choice(("credit_memo_unposted", "writeoff_one_sided")))
        breaks = ["dup_invoices", "unapplied_cash", omission]
        if difficulty != "foundation":
            breaks.append("cutoff_shift")
        if difficulty == "principal":
            breaks.append("fx_rounding")
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": {"saas": "B2B SaaS platform billed monthly in advance",
                               "ecommerce": "B2B e-commerce platform billing merchants a monthly platform fee",
                               "fintech_marketplace": "B2B payments platform billing monthly platform and volume fees"}[ind],
            "revenue_ttm_cents": int(math.exp(rng.uniform(math.log(10e6), math.log(40e6))) * 100),
            "revenue_account": "4000" if ind == "saas" else "4020",
            "gm": float(np.clip(band("gross_margin", 0.4, 0.7, "saas"), 0.65, 0.82)),
            "op_margin": float(rng.uniform(0.04, 0.12)),
            "churn_monthly": float(np.clip(band("gross_churn_monthly_midmarket", 0.3, 0.7, "saas"), 0.008, 0.025)),
            "terms_days": 30,
            "allowance_pct": float(np.clip(band("allowance_pct_ar", 0.25, 0.5, "saas"), 0.015, 0.05)),
            "breaks": breaks,
            "omission": omission,
            "break_magnitude": {b: magnitude(rng, difficulty, 0.003, 0.025) for b in BREAK_ORDER},
            "dup_rate": float(rng.uniform(0.005, 0.02)),
            "fx_share": float(rng.uniform(0.15, 0.30)) if difficulty == "principal" else 0.0,
            "late_share": float(rng.uniform(0.08, 0.14)),
            "never_pay_share": float(rng.uniform(0.01, 0.02)),
            "memo_share": float(rng.uniform(0.02, 0.04)),
            "tax_rate": float(rng.uniform(0.15, 0.25)),
            "capex_pct": float(rng.uniform(0.01, 0.03)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        budget = ROW_BUDGET[difficulty]
        gl_overhead = difficulty_pick(difficulty, 1300, 1300, 4500)
        p["n_invoices"] = int((budget * difficulty_pick(difficulty, 0.93, 0.9, 0.87) - gl_overhead - (1700 if difficulty == "principal" else 0)) / 3.25)
        p["description"] = f"{cap_first(p['business_model'])}; customers on net-{p['terms_days']} terms, invoiced on their billing anniversary."
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Customers, subscriptions and the monthly invoice schedule (USD), sized to the row budget."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=True)  # base working-capital defaults: vendor AP for part of the accrued-expense run and prepaid expenses
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        churn = p["churn_monthly"]
        # customers: 55% active at the start, the rest join uniformly; geometric lifetimes
        N = p["n_invoices"]
        exp_life = 1 / churn
        avg_active = 0.55 * min(n_m, exp_life) + 0.45 * min(n_m / 2, exp_life)
        C = max(40, int(N / avg_active * 1.02))
        start = np.where(g.random(C) < 0.55, 0, g.integers(1, n_m, C))
        life = g.geometric(churn, C)
        end = start + life  # exclusive
        seg = np.array(SEGMENTS)[np.minimum(2, (g.random(C) ** 1.8 * 3).astype(int))]
        seg_idx = np.array([SEGMENTS.index(x) for x in seg])
        mrr_usd = g.lognormal(np.log(np.array([1800.0, 7000.0, 26000.0]))[seg_idx], 0.45)
        billing_day = g.integers(1, 32, C)
        cid = np.array([f"CUS{i:05d}" for i in range(1, C + 1)])
        names = [rng.company_name(p["industry"] if p["industry"] in ("saas", "ecommerce") else None) for _ in range(C)]
        # invoice schedule: one per active customer-month
        cm = [(c, m) for c in range(C) for m in range(start[c], min(end[c], n_m))]
        inv = pd.DataFrame(cm, columns=["c", "m"])
        mstart = pd.to_datetime(pd.Series(months))
        dim = mstart.dt.days_in_month.to_numpy()
        inv_day = np.minimum(billing_day[inv["c"].to_numpy()], dim[inv["m"].to_numpy()])
        inv["invoice_date"] = mstart.to_numpy()[inv["m"].to_numpy()] + pd.to_timedelta(inv_day - 1, unit="D").to_numpy()
        usage = g.lognormal(0, 0.06, len(inv))
        inv["usd"] = mrr_usd[inv["c"].to_numpy()] * (1 + 0.01) ** (inv["m"].to_numpy() / 3) * usage
        # scale to the TTM revenue target
        ttm = inv["m"].to_numpy() >= n_m - 12
        scale = (p["revenue_ttm_cents"] / 100) / inv.loc[ttm, "usd"].sum()
        inv["usd"] *= scale
        inv["amount_usd_cents"] = cents_array(inv["usd"].to_numpy() * 100)
        inv = inv.sort_values(["invoice_date", "c"], kind="stable").reset_index(drop=True)
        inv["invoice_id"] = [f"INV-{i:06d}" for i in range(1, len(inv) + 1)]
        customers = pd.DataFrame({
            "customer_id": cid, "name": names, "segment": seg, "country": "US", "currency": "USD",
            "billing_email": [f"ap@{n.split()[0].lower()}{i}.example.com" for i, n in enumerate(names)],
            "created_at": mstart.to_numpy()[np.minimum(start, n_m - 1)] - pd.to_timedelta(g.integers(5, 60, C), unit="D").to_numpy(),
            "acquisition_channel": np.array(("inbound", "outbound", "partner"))[g.integers(0, 3, C)],
        })
        subs = pd.DataFrame({
            "subscription_id": [f"SUB{i:05d}" for i in range(1, C + 1)], "customer_id": cid,
            "plan_id": np.array(("starter", "growth", "scale"))[seg_idx],
            "mrr_cents": cents_array(mrr_usd * scale * 100),
            "billing_day": billing_day, "status": np.where(end >= n_m, "active", "canceled"),
            "start_date": mstart.to_numpy()[np.minimum(start, n_m - 1)],
            "canceled_at": pd.Series(mstart.to_numpy()[np.minimum(end, n_m - 1)]).where(end < n_m),
            "contract_months": np.where(seg == "enterprise", 12, 1),
        })
        state.frames.update({"customers": customers, "subscriptions": subs})
        state.extras["sched"] = inv
        state.extras["fx_customers"] = np.zeros(C, dtype=bool)

    # ------------------------------------------------------------------ anomalies (sub-ledger + GL documents)

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Build documents (invoices, payments, memos, write-offs) with every designed break, then post the GL."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("documents").generator
        P = state.periods
        months = state.extras["months"]
        n_m = len(months)
        end_ts = pd.Timestamp(cal.end) + EOD
        sched = state.extras["sched"]
        customers = state.frames["customers"]
        C = len(customers)
        N = len(sched)
        s7, e7 = (pd.Timestamp(x) for x in cal.bounds(P[6]))
        s8, e8 = (pd.Timestamp(x) for x in cal.bounds(P[7]))
        # ---------------- FX customers (principal): invoices from P8 in EUR / GBP
        fx_rates = None
        currency = np.full(N, "USD", dtype=object)
        amount = sched["amount_usd_cents"].to_numpy().copy()
        sub_usd = amount.copy()
        gl_usd = amount.copy()
        if "fx_rounding" in p["breaks"]:
            fxc = g.random(C) < p["fx_share"]
            cur_c = np.where(g.random(C) < 0.7, "EUR", "GBP")
            state.extras["fx_customers"] = fxc
            days = pd.date_range(s7 - pd.Timedelta(days=92), end_ts.normalize() + pd.Timedelta(days=0), freq="D")
            base = {"EUR": 1.08, "GBP": 1.27}
            rows = []
            for ccy in ("EUR", "GBP"):
                walk = np.exp(np.cumsum(g.normal(0, 0.004, len(days))))
                rows.append(pd.DataFrame({"rate_date": days, "currency": ccy, "usd_per_unit": np.round(base[ccy] * walk, 6)}))
            fx_rates = pd.concat(rows, ignore_index=True)
            rate = fx_rates.set_index(["currency", "rate_date"])["usd_per_unit"]
            mavg = fx_rates.assign(mo=fx_rates["rate_date"].dt.to_period("M")).groupby(["currency", "mo"])["usd_per_unit"].mean()
            fx_inv = fxc[sched["c"].to_numpy()] & (sched["invoice_date"] >= s8).to_numpy()
            ccy = cur_c[sched["c"].to_numpy()]
            currency = np.where(fx_inv, ccy, "USD")
            idx = np.flatnonzero(fx_inv)
            d = pd.to_datetime(sched["invoice_date"].to_numpy()[idx])
            r_day = rate.reindex(list(zip(ccy[idx], d, strict=True))).to_numpy()
            r_avg = mavg.reindex(list(zip(ccy[idx], d.to_period("M"), strict=True))).to_numpy()
            amount[idx] = cents_array(sched["amount_usd_cents"].to_numpy()[idx] / base_rate_for(ccy[idx], base))
            sub_usd[idx] = cents_array(amount[idx] * r_avg)
            gl_usd[idx] = cents_array(amount[idx] * r_day)
            state.extras["fx_rate_lookup"] = rate
            customers = customers.copy()
            customers.loc[fxc, "currency"] = cur_c[fxc]
            customers.loc[fxc, "country"] = [COUNTRIES_FX[c][int(g.integers(0, len(COUNTRIES_FX[c])))] for c in cur_c[fxc]]
            state.frames["customers"] = customers
        inv = pd.DataFrame({
            "invoice_id": sched["invoice_id"], "customer_id": customers["customer_id"].to_numpy()[sched["c"].to_numpy()],
            "subscription_id": state.frames["subscriptions"]["subscription_id"].to_numpy()[sched["c"].to_numpy()],
            "invoice_date": pd.to_datetime(sched["invoice_date"]), "currency": currency, "amount_cents": amount,
            "_sub_usd": sub_usd, "_gl_usd": gl_usd, "_c": sched["c"].to_numpy(),
        })
        inv["due_date"] = inv["invoice_date"] + pd.Timedelta(days=p["terms_days"])
        inv["period_start"] = inv["invoice_date"]
        inv["period_end"] = inv["invoice_date"] + pd.DateOffset(months=1) - pd.Timedelta(days=1)
        inv["created_at"] = inv["invoice_date"] + pd.to_timedelta(g.integers(1 * 3600, 6 * 3600, N), unit="s")
        usd = inv["currency"].to_numpy() == "USD"
        # ---------------- credit memos (partial, before payment) - only on USD invoices
        has_memo = usd & (g.random(N) < p["memo_share"])
        memo_amt = np.where(has_memo, np.maximum(100, np.round(inv["amount_cents"].to_numpy() * g.uniform(0.1, 0.5, N))), 0).astype(np.int64)
        memo_date = inv["invoice_date"] + pd.to_timedelta(g.integers(3, 21, N), unit="D")
        # ---------------- payment behaviour
        u = g.random(N)
        never = u < p["never_pay_share"]
        late = (~never) & (u < p["never_pay_share"] + p["late_share"])
        delay = np.where(late, g.integers(25, 120, N), np.clip(np.round(g.normal(2, 6, N)), -12, 20))
        pay_ts = inv["due_date"] + pd.to_timedelta(delay, unit="D") + pd.to_timedelta(g.integers(8 * 3600, 24 * 3600 - 60, N), unit="s")
        # some wires arrive in the last hour of a quarter (UTC) - local renderings put them in the next quarter
        qend = inv["due_date"] + pd.offsets.QuarterEnd(0)
        near = (~never) & ((qend - pay_ts.dt.normalize()).dt.days.between(0, 2)).to_numpy() & (g.random(N) < 0.5)
        pay_ts = pay_ts.where(~near, qend + pd.Timedelta(hours=23) + pd.to_timedelta(g.integers(0, 3500, N), unit="s"))
        net_due = inv["amount_cents"].to_numpy() - memo_amt
        paid = (~never) & (pay_ts <= end_ts).to_numpy() & (net_due > 0)
        has_memo &= (memo_date <= end_ts).to_numpy()
        memo_amt = np.where(has_memo, memo_amt, 0)
        pay_amt = np.where(paid, inv["amount_cents"].to_numpy() - np.where(has_memo & (memo_date < pay_ts).to_numpy(), memo_amt, 0), 0)
        # ---------------- write-offs: never-payers older than ~170 days
        wo_date = inv["invoice_date"] + pd.to_timedelta(g.integers(160, 200, N), unit="D")
        wo = never & (wo_date <= end_ts).to_numpy()
        wo_amt = np.where(wo, inv["amount_cents"].to_numpy() - memo_amt, 0)
        inv["status"] = np.where(paid, "paid", np.where(wo, "written_off", "open"))
        inv["voided_at"] = pd.NaT
        payments = pd.DataFrame({
            "invoice_id": inv["invoice_id"].to_numpy()[paid], "customer_id": inv["customer_id"].to_numpy()[paid],
            "amount_cents": pay_amt[paid], "currency": inv["currency"].to_numpy()[paid], "paid_at": pay_ts[paid].to_numpy(),
            "method": np.where(g.random(int(paid.sum())) < 0.55, "ach", np.where(g.random(int(paid.sum())) < 0.6, "wire", "card")),
            "_inv_idx": np.flatnonzero(paid),
        }).sort_values(["paid_at", "invoice_id"], kind="stable").reset_index(drop=True)
        payments.insert(0, "payment_id", [f"PAY-{i:06d}" for i in range(1, len(payments) + 1)])
        payments["status"] = "settled"
        memos = pd.DataFrame({
            "invoice_id": inv["invoice_id"].to_numpy()[has_memo], "customer_id": inv["customer_id"].to_numpy()[has_memo],
            "memo_date": memo_date[has_memo].dt.normalize().to_numpy(), "amount_cents": memo_amt[has_memo],
            "reason": np.array(("service_credit", "billing_error", "downgrade_proration", "goodwill"))[g.integers(0, 4, int(has_memo.sum()))],
        }).sort_values(["memo_date", "invoice_id"], kind="stable").reset_index(drop=True)
        memos.insert(0, "memo_id", [f"CM-{i:05d}" for i in range(1, len(memos) + 1)])
        memos["_posted"] = True
        wos = pd.DataFrame({
            "invoice_id": inv["invoice_id"].to_numpy()[wo], "customer_id": inv["customer_id"].to_numpy()[wo],
            "writeoff_date": wo_date[wo].dt.normalize().to_numpy(), "amount_cents": wo_amt[wo],
            "approved_by": np.array(("controller", "cfo"))[g.integers(0, 2, int(wo.sum()))],
        }).sort_values(["writeoff_date", "invoice_id"], kind="stable").reset_index(drop=True)
        wos.insert(0, "writeoff_id", [f"WO-{i:05d}" for i in range(1, len(wos) + 1)])
        wos["_posted"] = True
        inv["_gl_date"] = inv["invoice_date"]
        # ---------------- designed breaks
        ar_close = float(inv.loc[(inv["invoice_date"] >= s8), "_sub_usd"].sum()) / 3 * 1.4  # ~ closing AR estimate
        target = {b: max(1.0, p["break_magnitude"][b] * ar_close) for b in BREAK_ORDER}
        anomalies: list[Anomaly] = []
        aid = 0
        ids: dict[str, str] = {}
        dups = inv.iloc[0:0]
        if "dup_invoices" in p["breaks"]:
            # the rerun re-loaded consecutive days of P7 invoices (USD) until the target is reached (>= 3 rows)
            day0 = s7 + pd.Timedelta(days=int(g.integers(20, 50)))
            cand = inv[usd & (inv["invoice_date"] >= day0) & (inv["invoice_date"] <= e7)]
            cum = cand["amount_cents"].cumsum().to_numpy()
            k = max(3, int(np.searchsorted(cum, target["dup_invoices"])) + 1)
            dups = cand.iloc[:k]
            state.extras["rerun_window"] = (dups["invoice_date"].min(), dups["invoice_date"].max())
            aid += 1
            ids["dup_invoices"] = f"A{aid}"
        unapplied = np.zeros(len(payments), dtype=bool)
        if "unapplied_cash" in p["breaks"]:
            pi = payments["_inv_idx"].to_numpy()
            elig = np.flatnonzero((payments["paid_at"] >= s7).to_numpy() & (payments["method"] == "wire").to_numpy() & usd[pi]
                                  & ~np.isin(inv["invoice_id"].to_numpy()[pi], dups["invoice_id"].to_numpy()))
            elig = elig[g.permutation(len(elig))]
            cum = payments["amount_cents"].to_numpy()[elig].cumsum()
            k = max(2, int(np.searchsorted(cum, target["unapplied_cash"])) + 1)
            unapplied[elig[:k]] = True
            aid += 1
            ids["unapplied_cash"] = f"A{aid}"
        payments["_unapplied"] = unapplied
        om = p["omission"]
        if om == "credit_memo_unposted":
            # interface failures from P6 onwards: memos (USD) whose AR line was parked in suspense
            elig = np.flatnonzero((pd.to_datetime(memos["memo_date"]) >= pd.Timestamp(cal.bounds(P[5])[0])).to_numpy())
            elig = elig[g.permutation(len(elig))]
            cum = memos["amount_cents"].to_numpy()[elig].cumsum()
            k = max(2, int(np.searchsorted(cum, target["gl_omission"])) + 1)
            memos.loc[elig[:k], "_posted"] = False
        else:
            elig = np.flatnonzero((pd.to_datetime(wos["writeoff_date"]) >= pd.Timestamp(cal.bounds(P[5])[0])).to_numpy())
            if len(elig) < 2:  # few recent write-offs: any approved write-off can be mis-posted
                elig = np.arange(len(wos))
            elig = elig[g.permutation(len(elig))]
            cum = wos["amount_cents"].to_numpy()[elig].cumsum()
            k = max(1, min(len(elig), int(np.searchsorted(cum, target["gl_omission"])) + 1))
            wos.loc[elig[:k], "_posted"] = False
        aid += 1
        ids["gl_omission"] = f"A{aid}"
        if "cutoff_shift" in p["breaks"]:
            for (qs, qe, nxt) in ((s7, e7, True), (s8, e8, False)):
                cand = inv[usd & (inv["invoice_date"] >= qe - pd.Timedelta(days=1)) & (inv["invoice_date"] <= qe)
                           & ~inv["invoice_id"].isin(dups["invoice_id"])]
                cand = cand.sample(frac=1.0, random_state=np.random.RandomState(int(g.integers(0, 2**31 - 1))))
                cum = cand["amount_cents"].cumsum().to_numpy()
                k = max(2, int(np.searchsorted(cum, target["cutoff_shift"] * (0.6 if nxt else 1.0))) + 1)
                inv.loc[cand.index[:k], "_gl_date"] = qe + pd.Timedelta(days=1)
            aid += 1
            ids["cutoff_shift"] = f"A{aid}"
        if "fx_rounding" in p["breaks"]:
            aid += 1
            ids["fx_rounding"] = f"A{aid}"
        # duplicated rows (sub-ledger only)
        dup_rows = dups.copy()
        dup_rows["created_at"] = dup_rows["created_at"] + pd.to_timedelta(g.integers(2, 40, len(dup_rows)), unit="s") + pd.Timedelta(days=0)
        inv["ingest_id"] = 1
        dup_rows["ingest_id"] = 2
        inv["_dup"] = False
        dup_rows["_dup"] = True
        state.extras.update({"inv": inv, "dup_rows": dup_rows, "payments": payments, "memos": memos, "wos": wos, "fx_rates": fx_rates,
                             "break_ids": ids, "targets": target})
        # ---------------- anomaly stubs
        mag: dict[str, dict[str, float]] = {}
        texts = {
            "dup_invoices": (f"The {P[6]} ingestion rerun re-loaded {len(dups)} invoices dated {dups['invoice_date'].min():%Y-%m-%d}..{dups['invoice_date'].max():%Y-%m-%d} "
                             "a second time (ingest_id = 2, created_at seconds later); naive sums double count them."),
            "unapplied_cash": (f"{int(unapplied.sum())} wires in {P[6]}-{P[7]} arrived without remittance: payments.invoice_id is NULL, the GL credited 1100 "
                               "for the customer, the sub-ledger still shows the invoices open."),
            "credit_memo_unposted": ("The billing-to-GL interface rejected the AR line of some credit memos from "
                                     f"{P[5]}: the GL posted Dr 4100 / Cr 1400 (suspense) so AR in the GL was never relieved."),
            "writeoff_one_sided": (f"Write-offs approved from {P[5]} were posted Dr 1110 / Cr 1400 (suspense) by the interface instead of Cr 1100, "
                                   "so the GL still carries the receivables."),
            "cutoff_shift": (f"Invoices dated in the last 2 days of {P[6]} were posted to the GL on the first day of {P[7]}; invoices dated in the last "
                             f"2 days of {P[7]} had not been posted at close."),
            "fx_rounding": (f"From {P[7]} ~{p['fx_share']:.0%} of customers are invoiced in EUR/GBP; amounts are stored in invoice currency, the sub-ledger "
                            "converts at the month-average rate, the GL at the daily rate."),
        }
        for b in ("dup_invoices", "unapplied_cash", "gl_omission", "cutoff_shift", "fx_rounding"):
            if b not in ids:
                continue
            kind = om if b == "gl_omission" else b
            gl_side = kind in ("credit_memo_unposted", "writeoff_one_sided")
            anomalies.append(Anomaly(
                id=ids[b], kind=kind, pillar="sql" if b in ("dup_invoices", "unapplied_cash", "cutoff_shift") else "accounting_finance",
                title=LABEL[kind], mechanism=texts[kind],
                periods=[P[6]] if b == "dup_invoices" else ([P[5], P[6], P[7]] if gl_side else ([P[6], P[7]] if b != "fx_rounding" else [P[7]])),
                affected_accounts=["1100", "1400"] + (["4100"] if kind == "credit_memo_unposted" else ["1110"] if kind == "writeoff_one_sided" else []),
                affected_tables={"dup_invoices": ["invoices"], "unapplied_cash": ["payments", "gl_lines"], "credit_memo_unposted": ["credit_memos", "gl_lines", "gl_journal"],
                                 "writeoff_one_sided": ["write_offs", "gl_lines", "gl_journal"], "cutoff_shift": ["invoices", "gl_journal", "gl_lines"],
                                 "fx_rounding": ["invoices", "payments", "fx_rates", "gl_lines"]}[kind],
                magnitude={"break_magnitude_pct_of_closing_ar": round(p["break_magnitude"][b], 6), "target_cents": round(target[b], 2)},
                severity="high" if b in ("dup_invoices", "gl_omission", "unapplied_cash") else "medium"))
        state.extras["anomaly_mag"] = mag
        return anomalies

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Opening balances, per-document AR lines in batches, suspense postings, monthly close."""
        p, cal = state.params, state.calendar
        L = state.ledger
        gran = GL_BATCH[p["difficulty"]]
        months = state.extras["months"]
        end_date = cal.end
        inv, pay, memos, wos = (state.extras[k] for k in ("inv", "payments", "memos", "wos"))
        rate = state.extras.get("fx_rate_lookup")
        rev_acct = p["revenue_account"]
        R = p["revenue_ttm_cents"]
        ppe = cents(R * p["capex_pct"] * 5 * 0.55)
        accdep = cents(ppe * 0.45)
        cash0 = cents(R * 0.25)
        cs = cents(0.6 * (cash0 + ppe - accdep))
        L.open_books(cal.opening_date, {"1000": cash0, "1500": ppe, "1510": -accdep, "3000": -cs, "3100": -(cash0 + ppe - accdep - cs)})
        events: list[tuple[dt.date, int, Any]] = []
        # invoices (GL posting date may differ from invoice date: cutoff)
        posted = inv[inv["_gl_date"] <= pd.Timestamp(end_date)]
        posted = posted.assign(b=batch_end_dates(posted["_gl_date"], gran))
        for b, grp in posted.groupby("b", sort=True):
            lines = [_ar_line("1100", a, 0, c, invoice_id=i) for a, c, i in zip(grp["_gl_usd"], grp["customer_id"], grp["invoice_id"], strict=True)]
            lines.append(JournalLine(account=rev_acct, credit_cents=int(grp["_gl_usd"].sum()), dimension={"batch": f"billing:{b.date()}", "rows": str(len(grp))}))
            events.append((b.date(), 2, lambda d, ln=lines: L.entry(d, ln, "Invoices posted from billing (batch)", "sales")))
        # payments: GL clears the invoice's GL amount; FX difference to 7200
        inv_gl = inv.set_index("invoice_id")["_gl_usd"]
        pay = pay.assign(b=batch_end_dates(pay["paid_at"], gran))
        for b, grp in pay.groupby("b", sort=True):
            lines: list[JournalLine] = []
            cash = fxd = 0
            for amt, ccy, iid, cust, pid, paid_at, unapp in zip(grp["amount_cents"], grp["currency"], grp["invoice_id"], grp["customer_id"],
                                                                 grp["payment_id"], grp["paid_at"], grp["_unapplied"], strict=True):
                ar_amt = int(amt) if ccy == "USD" else int(inv_gl[iid])
                usd_amt = ar_amt if ccy == "USD" else cents(amt * float(rate[(ccy, pd.Timestamp(paid_at).normalize())]))
                cash += usd_amt
                fxd += usd_amt - ar_amt
                lines.append(_ar_line("1100", 0, ar_amt, cust, payment_id=pid, invoice_id=None if unapp else iid))
            lines.insert(0, JournalLine(account="1000", debit_cents=cash, dimension={"batch": f"collections:{b.date()}", "rows": str(len(grp))}))
            if fxd:
                lines.append(JournalLine(account="7200", debit_cents=max(0, -fxd), credit_cents=max(0, fxd), dimension={"batch": f"fx:{b.date()}"}))
            events.append((b.date(), 3, lambda d, ln=lines: L.entry(d, ln, "Customer receipts (batch)", "collections")))
        # credit memos: posted ones relieve AR; interface failures park the AR side in suspense
        memos = memos.assign(b=batch_end_dates(pd.to_datetime(memos["memo_date"]), gran))
        for (b, ok), grp in memos.groupby(["b", "_posted"], sort=True):
            total = int(grp["amount_cents"].sum())
            if ok:
                lines = [JournalLine(account="4100", debit_cents=total, dimension={"batch": f"credit_memos:{b.date()}", "rows": str(len(grp))})]
                lines += [_ar_line("1100", 0, a, c, memo_id=m, invoice_id=i) for a, c, m, i in zip(grp["amount_cents"], grp["customer_id"], grp["memo_id"], grp["invoice_id"], strict=True)]
                events.append((b.date(), 4, lambda d, ln=lines: L.entry(d, ln, "Credit memos (batch)", "credit_memos")))
            else:
                dim = {"batch": f"interface_errors:{b.date()}", "rows": str(len(grp)), "interface": "billing_ar", "status": "unmatched"}
                events.append((b.date(), 4, lambda d, a=total, dm=dim: L.adjust(d, [("4100", a, 0, dm), ("1400", 0, a, dm)],
                                                                               "Billing interface: AR line rejected - parked in suspense", "interface")))
        wos = wos.assign(b=batch_end_dates(pd.to_datetime(wos["writeoff_date"]), gran))
        for (b, ok), grp in wos.groupby(["b", "_posted"], sort=True):
            total = int(grp["amount_cents"].sum())
            if ok:
                lines = [JournalLine(account="1110", debit_cents=total, dimension={"batch": f"write_offs:{b.date()}", "rows": str(len(grp))})]
                lines += [_ar_line("1100", 0, a, c, writeoff_id=w, invoice_id=i) for a, c, w, i in zip(grp["amount_cents"], grp["customer_id"], grp["writeoff_id"], grp["invoice_id"], strict=True)]
                events.append((b.date(), 4, lambda d, ln=lines: L.entry(d, ln, "Receivables written off (batch)", "write_off")))
            else:
                dim = {"batch": f"interface_errors:{b.date()}", "rows": str(len(grp)), "interface": "billing_ar", "status": "unmatched"}
                events.append((b.date(), 4, lambda d, a=total, dm=dim: L.adjust(d, [("1110", a, 0, dm), ("1400", 0, a, dm)],
                                                                               "Billing interface: write-off AR line rejected - parked in suspense", "interface")))
        # monthly close
        rev_m = np.bincount(_month_index(inv["invoice_date"], cal.start), weights=inv["_sub_usd"], minlength=len(months))
        prev = {"payroll": 0, "opex": 0}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            run = float(rev_m[max(0, m - 2): m + 1].mean())
            cogs = cents(run * (1 - p["gm"] - self.period_noise(ctx, state).gm(ms)))  # review C2: quarterly GM / opex noise
            opex = cents(run * (p["gm"] - p["op_margin"] - 0.55 * p["capex_pct"]) * self.period_noise(ctx, state).ox(ms))
            payroll = cents(opex * 0.65)
            other = opex - payroll
            if m > 0:
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["opex"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex and hosting paid", source="opex")))
            if ms.month in (1, 4, 7, 10) and m > 0:
                events.append((ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"])))
            pr = dict(zip(PAYROLL_SPLIT, split_cents(payroll, list(PAYROLL_SPLIT.values())), strict=True))
            ox = dict(zip(OPEX_SPLIT, split_cents(other, list(OPEX_SPLIT.values())), strict=True))
            events.append((me, 7, lambda d, a=cogs: L.adjust(d, [("5000", a, 0), ("2100", 0, a)], "Hosting and support costs accrued", "opex")))
            events.append((me, 7, lambda d, s=pr: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox: L.accrue_opex(d, s)))
            events.append((ms + dt.timedelta(days=14), 6, lambda d, a=cents(run * p["capex_pct"]): L.capex(d, a)))
            events.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 60))))

            def provision(d: dt.date) -> None:
                bal = L.closing_balances()
                L.bad_debt_provision(d, cents(bal["1100"] * p["allowance_pct"]) + bal["1110"])

            events.append((me, 9, provision))
            if ms.month % 3 == 0:
                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))
                events.append((me, 11, tax))
            prev = {"payroll": payroll, "opex": other + cogs}
        events.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in events:
            if d <= end_date:
                fn(d)
        # billed monthly in advance: the unearned part of each invoice is deferred at quarter end (review P0 #3)
        docs = pd.DataFrame({"posted": posted["_gl_date"], "service_start": posted["invoice_date"],
                             "service_end": posted["invoice_date"] + pd.DateOffset(months=1), "amount_cents": posted["_gl_usd"]})
        state.extras["deferral_effect"] = post_deferred_revenue_trueups(state, docs, revenue_account=rev_acct)

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Sub-ledger tables (with the duplicated rerun rows) + the difficulty's dirt rules (2 / 4 / 6)."""
        p = state.params
        inv = pd.concat([state.extras["inv"], state.extras["dup_rows"]], ignore_index=True)
        inv = inv.sort_values(["invoice_date", "invoice_id", "ingest_id"], kind="stable").reset_index(drop=True)
        inv["subtotal_cents"] = inv["amount_cents"]
        inv["tax_cents"] = 0
        inv["updated_at"] = inv["created_at"]
        invoices = inv[["invoice_id", "customer_id", "subscription_id", "invoice_date", "due_date", "period_start", "period_end", "currency",
                        "subtotal_cents", "tax_cents", "amount_cents", "status", "ingest_id", "created_at", "updated_at"]]
        pay = state.extras["payments"].copy()
        pay["invoice_id"] = pay["invoice_id"].where(~pay["_unapplied"], None)
        pay["processor_ref"] = [f"TR{h:010d}" for h in range(7_000_000_001, 7_000_000_001 + len(pay))]
        payments = pay[["payment_id", "invoice_id", "customer_id", "amount_cents", "currency", "method", "status", "paid_at", "processor_ref"]]
        memos = state.extras["memos"].drop(columns=["_posted"])
        wos = state.extras["wos"].drop(columns=["_posted"])
        dirt_plan = [
            ("customers", "case_mixed:segment"), ("customers", "whitespace:name"),
            ("payments", "tz_mixed:paid_at"), ("invoices", "late_arriving:updated_at"),
            ("payments", "nulls:method:0.05"), ("write_offs", "format_mixed:writeoff_date"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        state.extras["dirt"] = dirt
        money = {c: "BIGINT" for c in ("amount_cents", "subtotal_cents", "tax_cents", "mrr_cents")}
        builder.add_frame("customers", state.frames["customers"], primary_key=["customer_id"], foreign_keys=[], dirt=dirt.get("customers", ()),
                          descriptions={"currency": "billing currency (EUR/GBP customers are invoiced in that currency)"})
        builder.add_frame("subscriptions", state.frames["subscriptions"], primary_key=["subscription_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types={**money, "billing_day": "INTEGER", "contract_months": "INTEGER", "start_date": "DATE"})
        builder.add_frame("invoices", invoices, primary_key=["invoice_id"],
                          foreign_keys=[("customer_id", "customers", "customer_id"), ("subscription_id", "subscriptions", "subscription_id")],
                          column_types={**money, "invoice_date": "DATE", "due_date": "DATE", "period_start": "DATE", "period_end": "DATE", "ingest_id": "INTEGER"},
                          dirt=dirt.get("invoices", ()),
                          descriptions={"amount_cents": "invoice total in minor units of `currency`", "ingest_id": "load sequence of the billing export (1 = first load)",
                                        "status": "status at extract time"},
                          description="AR sub-ledger: invoices as loaded from the billing system export.")
        builder.add_frame("payments", payments, primary_key=["payment_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id"), ("customer_id", "customers", "customer_id")],
                          column_types=money, dirt=dirt.get("payments", ()),
                          descriptions={"invoice_id": "invoice the cash was applied to (NULL = no remittance advice)", "amount_cents": "in minor units of `currency`",
                                        "paid_at": "bank value timestamp (UTC unless an offset is shown)"},
                          description="Customer receipts from the bank feed.")
        builder.add_frame("credit_memos", memos, primary_key=["memo_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id")],
                          column_types={**money, "memo_date": "DATE"}, description="Credit memos issued in billing.")
        builder.add_frame("write_offs", wos, primary_key=["writeoff_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id")],
                          column_types={**money, "writeoff_date": "DATE"}, dirt=dirt.get("write_offs", ()), description="Approved bad-debt write-offs.")
        if state.extras.get("fx_rates") is not None:
            builder.add_frame("fx_rates", state.extras["fx_rates"], primary_key=["rate_date", "currency"], foreign_keys=[],
                              column_types={"rate_date": "DATE", "usd_per_unit": "DOUBLE"}, description="Daily reference FX rates (USD per unit of currency).")

    # ------------------------------------------------------------------ ties and break amounts

    def _breaks(self, state: WorldState) -> dict[str, Any]:
        """Signed break amounts (sub-ledger minus GL) per type and period, plus sub-ledger roll-forwards (cached)."""
        if "brk" in state.extras:
            return state.extras["brk"]
        cal = state.calendar
        inv, dup, pay, memos, wos = (state.extras[k] for k in ("inv", "dup_rows", "payments", "memos", "wos"))
        sub_usd = inv.set_index("invoice_id")["_sub_usd"]
        pay = pay.assign(_applied_usd=np.where(pay["currency"] == "USD", pay["amount_cents"], pay["invoice_id"].map(sub_usd).fillna(0)).astype(np.int64))
        cum_applied_prev = 0
        out: dict[str, dict[str, int]] = {k: {} for k in ("dup_invoices", "unapplied_cash", "credit_memo_unposted", "writeoff_one_sided", "cutoff_shift", "fx_rounding")}
        naive, dedup, revenue, susp = {}, {}, {}, {}
        roll: dict[str, dict[str, int]] = {}
        prev_close = 0
        for period in [*cal.warmup_periods, *state.periods]:
            s, e = cal.bounds(period)
            s_ts, e_ts = pd.Timestamp(s), pd.Timestamp(e) + EOD
            issued = inv["invoice_date"] <= e_ts
            applied = pay[(pay["paid_at"] <= e_ts) & ~pay["_unapplied"]]
            applied_usd = int(applied["_applied_usd"].sum())
            m_ = memos[pd.to_datetime(memos["memo_date"]) <= e_ts]
            w_ = wos[pd.to_datetime(wos["writeoff_date"]) <= e_ts]
            clean = int(inv.loc[issued, "_sub_usd"].sum()) - applied_usd - int(m_["amount_cents"].sum()) - int(w_["amount_cents"].sum())
            dup_amt = int(dup.loc[dup["invoice_date"] <= e_ts, "_sub_usd"].sum())
            dedup[period] = clean
            naive[period] = clean + dup_amt
            out["dup_invoices"][period] = dup_amt
            out["unapplied_cash"][period] = int(pay.loc[(pay["paid_at"] <= e_ts) & pay["_unapplied"], "amount_cents"].sum())
            out["credit_memo_unposted"][period] = -int(m_.loc[~m_["_posted"], "amount_cents"].sum())
            out["writeoff_one_sided"][period] = -int(w_.loc[~w_["_posted"], "amount_cents"].sum())
            out["cutoff_shift"][period] = int(inv.loc[issued & (inv["_gl_date"] > e_ts), "_sub_usd"].sum())
            open_fx = issued & (inv["currency"] != "USD")
            paid_ids = set(applied["invoice_id"])
            open_fx &= ~inv["invoice_id"].isin(paid_ids)
            out["fx_rounding"][period] = int((inv.loc[open_fx, "_sub_usd"] - inv.loc[open_fx, "_gl_usd"]).sum())
            in_p = issued & (inv["invoice_date"] >= s_ts)
            revenue[period] = int(inv.loc[in_p, "_sub_usd"].sum())
            susp[period] = -out["credit_memo_unposted"][period] - out["writeoff_one_sided"][period]
            collected = applied_usd - cum_applied_prev
            cum_applied_prev = applied_usd
            roll[period] = {"opening": prev_close, "invoiced": revenue[period],
                            "collected": collected,
                            "credits": int(m_.loc[pd.to_datetime(m_["memo_date"]) >= s_ts, "amount_cents"].sum()),
                            "writeoffs": int(w_.loc[pd.to_datetime(w_["writeoff_date"]) >= s_ts, "amount_cents"].sum()), "closing": clean}
            prev_close = clean
        res = {"items": out, "naive": naive, "dedup": dedup, "revenue": revenue, "suspense": susp, "roll": roll}
        state.extras["brk"] = res
        return res

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """Naive / deduplicated AR vs GL 1100 (designed breaks), suspense (holds), revenue (designed cutoff / FX break)."""
        b = self._breaks(state)
        P = state.periods
        it = b["items"]
        gl_items = ("unapplied_cash", "credit_memo_unposted", "writeoff_one_sided", "cutoff_shift", "fx_rounding")
        dedup_brk = {x: sum(it[k][x] for k in gl_items) for x in P}
        naive_brk = {x: dedup_brk[x] + it["dup_invoices"][x] for x in P}
        inv = state.extras["inv"]
        rev_brk = {}
        for x in P:
            s, e = state.calendar.bounds(x)
            s_ts, e_ts = pd.Timestamp(s), pd.Timestamp(e) + EOD
            in_p = (inv["invoice_date"] >= s_ts) & (inv["invoice_date"] <= e_ts)
            gl_p = (inv["_gl_date"] >= s_ts) & (inv["_gl_date"] <= e_ts)
            rev_brk[x] = int(inv.loc[in_p, "_sub_usd"].sum() - inv.loc[gl_p, "_gl_usd"].sum())
        state.extras["expected_breaks"] = {"naive": naive_brk, "dedup": dedup_brk, "revenue": rev_brk}
        acct = state.params["revenue_account"]
        susp_sub = {x: -b["suspense"][x] for x in P}  # suspense items carry a credit balance on a debit-normal account
        return [
            SubledgerTie("ar_subledger_naive_vs_gl", {x: b["naive"][x] for x in P}, ["1100"], expected_break_cents={k: v for k, v in naive_brk.items() if v},
                         description="Sum of invoices (all loaded rows) - applied cash - credit memos - write-offs vs GL 1100 (designed breaks)"),
            SubledgerTie("ar_subledger_deduped_vs_gl", {x: b["dedup"][x] for x in P}, ["1100"], expected_break_cents={k: v for k, v in dedup_brk.items() if v},
                         description="Same with duplicate loads removed (lowest ingest_id) vs GL 1100 (designed breaks)"),
            SubledgerTie("suspense_items_vs_gl_1400", susp_sub, ["1400"], description="Interface items parked in suspense == GL 1400 (holds)"),
            SubledgerTie("revenue_invoices_vs_gl", {x: b["revenue"][x] for x in P}, [acct], basis="activity",
                         expected_break_cents={k: v for k, v in rev_brk.items() if v}, exclude_sources=(DEFERRAL_SOURCE,),
                         description="Invoices dated in period (USD at month-average) vs GL revenue: cutoff and FX (designed)"),
        ]

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """AR operations KPIs by period (invoices, collections, unapplied, aging) and unit economics."""
        cal = state.calendar
        inv, pay = state.extras["inv"], state.extras["payments"]
        b = self._breaks(state)
        kpis = {}
        for x in state.periods:
            s, e = cal.bounds(x)
            s_ts, e_ts = pd.Timestamp(s), pd.Timestamp(e) + EOD
            inc = next(i for i in state.statements.income if i.period == x)  # type: ignore[union-attr]
            kpis[x] = {"invoices": float(((inv["invoice_date"] >= s_ts) & (inv["invoice_date"] <= e_ts)).sum()),
                       "payments": float(((pay["paid_at"] >= s_ts) & (pay["paid_at"] <= e_ts)).sum()),
                       "revenue_cents": float(inc.revenue_cents), "net_income_cents": float(inc.net_income_cents),
                       "gl_ar_cents": float(state.ledger.trial_balance(x)["1100"]), "subledger_ar_cents": float(b["dedup"][x]),
                       "dso": state.statements.ratios[x]["dso"]}  # type: ignore[union-attr]
        subs = state.frames["subscriptions"]
        unit = {"arpa": float(subs["mrr_cents"].mean()), "churn_monthly": state.params["churn_monthly"],
                "gross_margin": state.params["gm"], "cac": 0.0, "ltv": float(subs["mrr_cents"].mean() * state.params["gm"] / state.params["churn_monthly"]),
                "payback_months": 0.0}
        return build_operations_state(queueing={"invoices_per_day": float(len(inv) / (len(state.extras["months"]) * 30.4))}, unit_economics=unit,
                                      supply_chain={}, pricing={"plans": ["starter", "growth", "scale"], "terms_days": state.params["terms_days"]},
                                      kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def _aging(self, state: WorldState) -> dict[str, int]:
        """Aging of deduplicated open invoices at P8 end (days past due), USD at month-average rate."""
        cal = state.calendar
        inv, pay, memos, wos = (state.extras[k] for k in ("inv", "payments", "memos", "wos"))
        e8 = pd.Timestamp(cal.bounds(state.periods[-1])[1])
        applied = pay[~pay["_unapplied"]].groupby("invoice_id")["amount_cents"].sum()
        sub_rate = inv["_sub_usd"] / inv["amount_cents"]
        open_amt = (inv["amount_cents"] - inv["invoice_id"].map(applied).fillna(0) - inv["invoice_id"].map(memos.groupby("invoice_id")["amount_cents"].sum()).fillna(0)
                    - inv["invoice_id"].map(wos.groupby("invoice_id")["amount_cents"].sum()).fillna(0))
        open_usd = np.round(open_amt * sub_rate)
        past = (e8 - inv["due_date"]).dt.days
        sel = open_usd > 0
        buckets = {"0_30": past <= 30, "31_60": (past > 30) & (past <= 60), "61_90": (past > 60) & (past <= 90), "90_plus": past > 90}
        return {k: int(open_usd[sel & v].sum()) for k, v in buckets.items()}

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 12 key table from the books and the sub-ledger, reference SQL on the dirty DB."""
        p, cal = state.params, state.calendar
        b = self._breaks(state)
        P = state.periods
        P7, P8 = P[6], P[7]
        it = b["items"]
        ids = state.extras["break_ids"]
        tb8 = state.ledger.trial_balance(P8)
        gl_ar = int(tb8["1100"])
        dup = state.extras["dup_rows"]
        aging = self._aging(state)
        roll8 = b["roll"][P8]
        unexplained = gl_ar - b["dedup"][P8]
        om = p["omission"]
        recon_items = {k: -it[k][P8] for k in ("unapplied_cash", om, "cutoff_shift", "fx_rounding") if k in p["breaks"] or k == om}
        metrics = [
            metric("gl_ar_close_p8_cents", gl_ar, "cents", f"Trial balance 1100 at {P8} end: SUM(debit_cents - credit_cents) on account 1100 through {P8}.",
                   pillar="accounting_finance", tol_abs=1.0, label="GL accounts receivable, latest quarter end"),
            metric("subledger_ar_naive_cents", b["naive"][P8], "cents",
                   f"All invoice rows dated <= {cal.bounds(P8)[1]} (USD at the month-average rate) - payments applied to an invoice - credit memos - write-offs.",
                   pillar="sql", tol_rel=0.01, label="Sub-ledger AR, naive sum"),
            metric("subledger_ar_deduped_cents", b["dedup"][P8], "cents",
                   "Same after keeping one row per invoice_id (QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1).",
                   pillar="sql", tol_rel=0.01, label="Sub-ledger AR, deduplicated"),
            metric("unexplained_difference_cents", unexplained, "cents", "GL 1100 minus the deduplicated sub-ledger AR at P8 end; must be fully explained by the reconciling items.",
                   pillar="accounting_finance", tol_abs=1.0, label="Difference to explain (GL - clean sub-ledger)"),
            metric("duplicate_invoice_count", len(dup), "count", f"Invoice rows with ingest_id > 1 for an invoice_id already loaded (rerun of {P7}).",
                   pillar="sql", tol_abs=0.0, label="Duplicate invoice rows"),
            metric("duplicate_invoice_cents", it["dup_invoices"][P8], "cents", "Sum of amount_cents (USD) of those duplicate rows.", pillar="sql", tol_rel=0.02,
                   label="Duplicate invoice amount"),
            metric("unapplied_cash_cents", it["unapplied_cash"][P8], "cents", "SUM(payments.amount_cents) WHERE invoice_id IS NULL (all USD wires).",
                   pillar="sql", tol_rel=0.02, label="Unapplied cash"),
            metric("suspense_balance_p8_cents", int(tb8["1400"]), "cents", f"GL 1400 balance at {P8} end (signed debit-positive; a credit balance is negative).",
                   pillar="accounting_finance", tol_abs=1.0, label="Suspense account balance"),
            metric("ar_rollforward_p8_invoiced_cents", roll8["invoiced"], "cents", f"Deduplicated invoices dated in {P8} (USD at month-average).",
                   pillar="sql", tol_rel=0.01, label="AR roll-forward: invoiced in latest quarter"),
            metric("ar_rollforward_p8_collected_cents", roll8["collected"], "cents", f"Payments applied to invoices with a {P8} value date (UTC), USD at the invoice's rate.",
                   pillar="sql", tol_rel=0.01, label="AR roll-forward: collected in latest quarter"),
            metric("ar_rollforward_p8_closing_cents", roll8["closing"], "cents", "Opening + invoiced - collected - credits - write-offs (deduplicated).",
                   pillar="sql", tol_rel=0.01, label="AR roll-forward: closing"),
        ]
        for k, v in aging.items():
            metrics.append(metric(f"aging_bucket_{k}_p8_cents", v, "cents", f"Open deduplicated invoices at {P8} end, days past due {k.replace('_', '-').replace('-plus', '+')}.",
                                  pillar="sql", tol_rel=0.03, label=f"Aging {k.replace('_', '-').replace('-plus', '+')} days"))
        for k, v in recon_items.items():
            metrics.append(metric(f"reconciling_item_{k}_cents", v, "cents",
                                  f"Effect of {LABEL[k].lower()} on GL minus clean sub-ledger at {P8} end (signed).",
                                  pillar="accounting_finance", tol_rel=0.05, label=f"Reconciling item: {LABEL[k]}"))
        if "cutoff_shift" in p["breaks"]:
            metrics.append(metric("cutoff_items_cents", it["cutoff_shift"][P8], "cents", f"Invoices dated in {P8} with no GL posting by {cal.bounds(P8)[1]} (USD).",
                                  pillar="sql", tol_rel=0.05, label="Cutoff items (invoiced, not yet in the GL)"))
        # ---------------- reference SQL
        tz = "tz_mixed:paid_at" in state.extras["dirt"].get("payments", ())
        paid_date = "CAST(CAST(paid_at AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS DATE)" if tz else "CAST(paid_at AS DATE)"
        fx = "fx_rounding" in p["breaks"]
        wo_date = ("COALESCE(TRY_CAST(writeoff_date AS DATE), CAST(TRY_STRPTIME(CAST(writeoff_date AS VARCHAR), '%m/%d/%Y') AS DATE))"
                   if "format_mixed:writeoff_date" in state.extras["dirt"].get("write_offs", ()) else "CAST(writeoff_date AS DATE)")
        if fx:
            rate_cte = ("rates AS (SELECT currency, date_trunc('month', rate_date) AS mo, AVG(usd_per_unit) AS r FROM fx_rates GROUP BY 1, 2),\n"
                        "inv AS (SELECT i.*, CASE WHEN i.currency = 'USD' THEN i.amount_cents ELSE ROUND(i.amount_cents * r.r) END AS usd_cents\n"
                        "        FROM inv0 i LEFT JOIN rates r ON r.currency = i.currency AND r.mo = date_trunc('month', i.invoice_date)),")
        else:
            rate_cte = "inv AS (SELECT *, amount_cents AS usd_cents FROM inv0),"
        sql_roll = f"""
WITH inv0 AS (
  SELECT * FROM invoices QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1),
{rate_cte}
act AS (
  SELECT period_of(invoice_date) AS period, SUM(usd_cents) AS invoiced, 0 AS collected, 0 AS credits, 0 AS writeoffs FROM inv GROUP BY 1
  UNION ALL
  SELECT period_of({paid_date.replace('paid_at', 'p.paid_at')}), 0,
         SUM(CASE WHEN p.currency = 'USD' THEN p.amount_cents ELSE i.usd_cents END), 0, 0
  FROM payments p JOIN inv i ON i.invoice_id = p.invoice_id GROUP BY 1
  UNION ALL
  SELECT period_of(memo_date), 0, 0, SUM(amount_cents), 0 FROM credit_memos GROUP BY 1
  UNION ALL
  SELECT period_of({wo_date}), 0, 0, 0, SUM(amount_cents) FROM write_offs GROUP BY 1),
roll AS (SELECT period, SUM(invoiced) AS invoiced, SUM(collected) AS collected, SUM(credits) AS credits, SUM(writeoffs) AS writeoffs FROM act GROUP BY 1),
gl AS (
  SELECT j.period, SUM(l.debit_cents - l.credit_cents) AS gl_activity
  FROM gl_lines l JOIN gl_journal j USING (entry_id) WHERE l.account_code = '1100' GROUP BY 1)
SELECT r.period, r.invoiced, r.collected, r.credits, r.writeoffs,
       SUM(r.invoiced - r.collected - r.credits - r.writeoffs) OVER (ORDER BY r.period ROWS UNBOUNDED PRECEDING) AS subledger_ar_close,
       SUM(g.gl_activity) OVER (ORDER BY r.period ROWS UNBOUNDED PRECEDING) AS gl_ar_close,
       SUM(g.gl_activity) OVER (ORDER BY r.period ROWS UNBOUNDED PRECEDING)
         - SUM(r.invoiced - r.collected - r.credits - r.writeoffs) OVER (ORDER BY r.period ROWS UNBOUNDED PRECEDING) AS difference
FROM roll r JOIN gl g USING (period) ORDER BY r.period"""

        def roll_checks(df: pd.DataFrame) -> dict[str, Any]:
            s = df.set_index("period")
            return {"difference_p8": float(s.loc[P8, "difference"]), "subledger_close_p8": float(s.loc[P8, "subledger_ar_close"]),
                    "gl_close_p8": float(s.loc[P8, "gl_ar_close"]), "first_period_with_difference": str(s.index[(s["difference"].abs() > 1)][0]) if (s["difference"].abs() > 1).any() else ""}

        q_roll = run_reference_query(con, "ar_rollforward_vs_gl_running_totals", sql_roll, signature=["over(", "qualify", "union", "join", "group by"],
                                     metric_keys=["ar_rollforward_p8_closing_cents", "unexplained_difference_cents", "gl_ar_close_p8_cents"], checks=roll_checks)
        sql_dup = """
SELECT invoice_id, COUNT(*) AS loads, MIN(ingest_id) AS first_ingest, MAX(ingest_id) AS last_ingest,
       MAX(amount_cents) AS amount_cents, MAX(created_at) - MIN(created_at) AS reload_gap
FROM invoices GROUP BY 1 HAVING COUNT(*) > 1 ORDER BY invoice_id"""
        q_dup = run_reference_query(con, "duplicate_invoice_loads", sql_dup, signature=["group by", "having", "count("], metric_keys=["duplicate_invoice_count", "duplicate_invoice_cents"],
                                    checks=lambda df: {"duplicate_rows": int((df["loads"] - 1).sum()), "duplicate_cents": int(df["amount_cents"].sum())})
        sql_naive = """
WITH ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) AS rn FROM invoices WHERE currency = 'USD')
SELECT SUM(amount_cents) AS naive_invoiced, SUM(amount_cents) FILTER (WHERE rn = 1) AS deduped_invoiced,
       SUM(amount_cents) FILTER (WHERE rn > 1) AS duplicate_cents, COUNT(*) FILTER (WHERE rn > 1) AS duplicate_rows
FROM ranked"""
        q_naive = run_reference_query(con, "naive_vs_deduped_invoice_totals", sql_naive, signature=["over(", "partition by", "row_number(", "with", "filter("],
                                      metric_keys=["subledger_ar_naive_cents", "subledger_ar_deduped_cents"],
                                      checks=lambda df: {"duplicate_cents": int(df["duplicate_cents"].iloc[0] or 0)})
        sql_unapp = f"""
SELECT period_of({paid_date}) AS period, COUNT(*) AS payments, SUM(amount_cents) AS unapplied_cents
FROM payments WHERE invoice_id IS NULL GROUP BY 1 ORDER BY 1"""
        q_unapp = run_reference_query(con, "unapplied_cash_by_quarter", sql_unapp, signature=["group by", "sum("], metric_keys=["unapplied_cash_cents"],
                                      checks=lambda df: {"unapplied_total": int(df["unapplied_cents"].sum())})
        sql_unapp_gl = """
WITH gl AS (
  SELECT regexp_extract(l.dimensions, 'payment_id=([^;]+)', 1) AS payment_id,
         l.dimension_invoice_id AS gl_invoice_id, l.credit_cents
  FROM gl_lines l WHERE l.account_code = '1100' AND l.dimensions LIKE '%payment_id=%')
SELECT p.payment_id, p.customer_id, p.amount_cents, gl.credit_cents AS gl_credit_cents
FROM payments p JOIN gl USING (payment_id)
LEFT JOIN invoices i ON i.invoice_id = p.invoice_id
WHERE p.invoice_id IS NULL AND gl.gl_invoice_id IS NULL ORDER BY p.payment_id"""
        q_unapp_gl = run_reference_query(con, "unapplied_cash_credited_in_gl", sql_unapp_gl, signature=["with", "left join", "join"],
                                         metric_keys=["unapplied_cash_cents"], checks=lambda df: {"gl_credited": int(df["gl_credit_cents"].sum())})
        sql_susp = """
WITH s AS (
  SELECT j.period, j.source, SUM(l.debit_cents - l.credit_cents) AS net
  FROM gl_lines l JOIN gl_journal j USING (entry_id) WHERE l.account_code = '1400' GROUP BY 1, 2)
SELECT period, source, net, SUM(net) OVER (ORDER BY period ROWS UNBOUNDED PRECEDING) AS suspense_balance FROM s ORDER BY period"""
        q_susp = run_reference_query(con, "suspense_running_balance", sql_susp, signature=["over(", "with", "join", "group by"], metric_keys=["suspense_balance_p8_cents"],
                                     checks=lambda df: {"suspense_balance_last": int(df["suspense_balance"].iloc[-1])})
        if om == "credit_memo_unposted":
            sql_om = """
WITH gl AS (SELECT regexp_extract(dimensions, 'memo_id=([^;]+)', 1) AS memo_id FROM gl_lines WHERE account_code = '1100' AND dimensions LIKE '%memo_id=%')
SELECT m.memo_id, m.invoice_id, m.memo_date, m.amount_cents
FROM credit_memos m LEFT JOIN gl ON gl.memo_id = m.memo_id
WHERE gl.memo_id IS NULL ORDER BY m.memo_date, m.memo_id"""
            om_name = "credit_memos_missing_from_gl"
        else:
            sql_om = """
WITH gl AS (SELECT regexp_extract(dimensions, 'writeoff_id=([^;]+)', 1) AS writeoff_id FROM gl_lines WHERE account_code = '1100' AND dimensions LIKE '%writeoff_id=%')
SELECT w.writeoff_id, w.invoice_id, w.writeoff_date, w.amount_cents
FROM write_offs w LEFT JOIN gl ON gl.writeoff_id = w.writeoff_id
WHERE gl.writeoff_id IS NULL ORDER BY w.writeoff_id"""
            om_name = "write_offs_missing_from_gl"
        q_om = run_reference_query(con, om_name, sql_om, signature=["with", "left join"], metric_keys=[f"reconciling_item_{om}_cents", "suspense_balance_p8_cents"],
                                   checks=lambda df: {"items": int(len(df)), "amount_cents": int(df["amount_cents"].sum())})
        queries = {"dup_invoices": [q_dup, q_naive, q_roll], "unapplied_cash": [q_unapp, q_unapp_gl], "gl_omission": [q_om, q_susp]}
        if "cutoff_shift" in p["breaks"]:
            sql_cut = """
WITH inv AS (SELECT * FROM invoices QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1),
gl AS (
  SELECT l.dimension_invoice_id AS invoice_id, j.entry_date, j.period AS gl_period
  FROM gl_lines l JOIN gl_journal j USING (entry_id)
  WHERE l.account_code = '1100' AND l.debit_cents > 0)
SELECT i.invoice_id, i.invoice_date, period_of(i.invoice_date) AS invoice_period, gl.entry_date, gl.gl_period, i.amount_cents
FROM inv i JOIN gl USING (invoice_id)
WHERE date_trunc('quarter', i.invoice_date) <> date_trunc('quarter', gl.entry_date) ORDER BY i.invoice_date, i.invoice_id"""
            q_cut = run_reference_query(con, "invoice_period_vs_gl_period", sql_cut, signature=["qualify", "with", "join", "date_trunc("], metric_keys=["cutoff_items_cents"],
                                        checks=lambda df: {"shifted_invoices": int(len(df))})
            sql_cut2 = """
WITH inv AS (SELECT * FROM invoices QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1),
gl AS (SELECT DISTINCT dimension_invoice_id AS invoice_id FROM gl_lines WHERE account_code = '1100' AND debit_cents > 0)
SELECT period_of(i.invoice_date) AS period, COUNT(*) AS invoices_not_in_gl, SUM(i.amount_cents) AS amount_cents
FROM inv i LEFT JOIN gl USING (invoice_id) WHERE gl.invoice_id IS NULL GROUP BY 1 ORDER BY 1"""
            q_cut2 = run_reference_query(con, "invoices_missing_from_gl", sql_cut2, signature=["qualify", "left join", "group by", "distinct"], metric_keys=["cutoff_items_cents"],
                                         checks=lambda df: {"missing_p8_cents": int(df.set_index("period")["amount_cents"].get(P8, 0))})
            queries["cutoff_shift"] = [q_cut, q_cut2]
        if fx:
            sql_fx = """
WITH rates AS (SELECT currency, date_trunc('month', rate_date) AS mo, AVG(usd_per_unit) AS avg_rate FROM fx_rates GROUP BY 1, 2),
gl AS (SELECT dimension_invoice_id AS invoice_id, debit_cents AS gl_usd_cents
       FROM gl_lines WHERE account_code = '1100' AND debit_cents > 0)
SELECT i.invoice_id, i.currency, i.amount_cents AS amount_minor_units, ROUND(i.amount_cents * r.avg_rate) AS subledger_usd_cents, gl.gl_usd_cents,
       ROUND(i.amount_cents * r.avg_rate) - gl.gl_usd_cents AS difference_cents
FROM invoices i JOIN rates r ON r.currency = i.currency AND r.mo = date_trunc('month', i.invoice_date)
JOIN gl USING (invoice_id) WHERE i.currency <> 'USD' AND i.ingest_id = 1 ORDER BY i.invoice_id"""
            q_fx = run_reference_query(con, "fx_invoice_conversion_differences", sql_fx, signature=["with", "join", "date_trunc(", "avg("], metric_keys=["reconciling_item_fx_rounding_cents"],
                                       checks=lambda df: {"fx_invoices": int(len(df)), "sum_difference_all": int(df["difference_cents"].sum())})
            sql_fx2 = """
SELECT currency, COUNT(*) AS invoices, SUM(amount_cents) AS naive_sum_minor_units FROM invoices
WHERE ingest_id = 1 GROUP BY 1 ORDER BY 1"""
            q_fx2 = run_reference_query(con, "invoice_totals_by_currency", sql_fx2, signature=["group by", "count(", "sum("], metric_keys=["reconciling_item_fx_rounding_cents"],
                                        checks=lambda df: {"currencies": int(len(df))})
            queries["fx_rounding"] = [q_fx, q_fx2]
        anomalies = []
        for a in state.anomalies:
            key = next(k for k, v in ids.items() if v == a.id)
            kind = om if key == "gl_omission" else key
            amt = it[kind][P8]
            trail = {
                "dup_invoices": [f"Naive invoice sums exceed the deduplicated sub-ledger by {it['dup_invoices'][P8] / 100:,.0f} USD.",
                                 f"{len(dup)} invoice_ids were loaded twice (ingest_id 1 and 2, created seconds apart) during the {P7} rerun."],
                "unapplied_cash": [f"{int(state.extras['payments']['_unapplied'].sum())} payments have no invoice_id; the GL credited AR by customer.",
                                   "Those customers' invoices look open (and aging) in the sub-ledger although they paid."],
                "credit_memo_unposted": [f"Suspense (1400) carries a credit balance of {-int(tb8['1400']) / 100:,.0f} USD from billing interface errors.",
                                         "Credit memos with no AR line in the GL (anti-join on memo_id)."],
                "writeoff_one_sided": [f"Suspense (1400) carries a credit balance of {-int(tb8['1400']) / 100:,.0f} USD from billing interface errors.",
                                       "Write-offs with no AR line in the GL (anti-join on writeoff_id)."],
                "cutoff_shift": [f"Invoices dated at the end of {P7} carry a {P8} GL date; {it['cutoff_shift'][P8] / 100:,.0f} USD of {P8} invoices are not in the GL yet."],
                "fx_rounding": ["EUR/GBP invoices are stored in minor units of their currency; month-average vs daily-rate conversion leaves small differences on open items."],
            }[kind]
            anomalies.append(a.model_copy(update={"reference_sql": queries[key], "evidence_trail": trail,
                                                  "magnitude": {**a.magnitude, "break_cents_p8": float(amt),
                                                                **({"duplicate_rows": float(len(dup))} if kind == "dup_invoices" else {})}}))
        fx_share_of_diff = abs(recon_items.get("fx_rounding", 0)) / max(1, sum(abs(v) for v in recon_items.values()))
        herrings_all = [
            "The GL is wrong - the GL is right except for the interface items parked in suspense; most of the gap is sub-ledger data quality.",
            f"FX explains it - conversion differences are {fx_share_of_diff:.1%} of the reconciling items; the structural items dominate.",
            "Just book the difference to suspense - the suspense balance is the symptom the auditor already flagged; itemise it.",
        ]
        herrings = [herrings_all[0], herrings_all[2], herrings_all[1]][: p["n_red_herrings"]]
        parts = []
        if "dup_invoices" in ids:
            parts.append(f"{len(dup)} invoices double-loaded by the {P7} ingestion rerun ({it['dup_invoices'][P8] / 100:,.0f} USD in naive sums)")
        parts.append(f"{it['unapplied_cash'][P8] / 100:,.0f} USD of unapplied cash (wires without remittance credited to AR by customer only)")
        parts.append(f"{-it[om][P8] / 100:,.0f} USD of {'credit memos' if om == 'credit_memo_unposted' else 'write-offs'} whose AR line the billing interface parked in suspense")
        if "cutoff_shift" in p["breaks"]:
            parts.append(f"{it['cutoff_shift'][P8] / 100:,.0f} USD of {P8} invoices not yet posted (cutoff)")
        if fx:
            parts.append(f"{it['fx_rounding'][P8] / 100:,.0f} USD of month-average vs daily-rate FX differences")
        root = (f"GL 1100 is {gl_ar / 100:,.0f} USD at {P8} end; the naive sub-ledger shows {b['naive'][P8] / 100:,.0f} and the deduplicated one "
                f"{b['dedup'][P8] / 100:,.0f}. The difference is fully explained by: " + "; ".join(parts)
                + ". The fixes: dedupe on the lowest ingest_id and make the ingestion idempotent, apply the unapplied cash, post the suspense items "
                "to AR, and post the cutoff invoices in the right period.")
        metrics = _with_hints(metrics, _HINTS)
        return GroundTruth(
            root_cause=root, anomalies=anomalies, red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=list(dict.fromkeys(q.sql for a in anomalies for q in a.reference_sql)),
                python=["import pandas as pd\n"
                        "inv = pd.read_sql('SELECT * FROM invoices', con).sort_values('ingest_id').drop_duplicates('invoice_id')\n"
                        "pay = pd.read_sql(\"SELECT *, CAST(CAST(paid_at AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS DATE) AS value_date FROM payments\", con)\n"
                        "gl = pd.read_sql(\"SELECT l.*, j.entry_date, j.period FROM gl_lines l JOIN gl_journal j USING (entry_id) WHERE account_code = '1100'\", con)\n"
                        "gl['invoice_id'] = gl.dimension_invoice_id  # or gl.dimensions.str.extract(r'invoice_id=([^;]+)')\n"
                        "# reconciling items: duplicates, unapplied cash, suspense items, cutoff, FX -> must net to GL - sub-ledger\n"],
                memo_bullets=[
                    "The sub-ledger to GL tie-out is a roll-forward: opening + invoiced - collected - credits - write-offs = closing, per period.",
                    "Deduplicate on invoice_id keeping the lowest ingest_id (ROW_NUMBER / QUALIFY), not DISTINCT; fix the loader's idempotency.",
                    "Tie GL 1100 lines to documents on gl_lines.dimension_invoice_id (equivalently regexp_extract(dimensions, "
                    "'invoice_id=([^;]+)', 1)); payment / memo / write-off ids live in gl_lines.dimensions.",
                    "Unapplied cash: apply by amount + customer; until then the aging overstates past-due balances.",
                    "Suspense is a symptom: post the interface items to AR, then fix the interface mapping.",
                    "Normalise payment timestamps to UTC before bucketing by quarter; cutoff items belong to the period of the invoice date.",
                    "Reconciling-items table nets the difference to zero; FX is small relative to the structural items.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, four stakeholders and the spec timeline (red herrings flagged)."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        names = rng.pick_names(4)
        tb8 = state.ledger.trial_balance(P[-1])
        reveal = [
            "The external auditor's request list asks for the AR sub-ledger to general-ledger reconciliation as of quarter end.",
            f"A suspense account balance of {abs(int(tb8['1400'])) / 100:,.0f} USD appears in the trial balance.",
            f"The data team reran the billing ingestion job in {P[6]} after an outage.",
        ]
        if "fx_rounding" in p["breaks"]:
            reveal.append(f"Multi-currency (EUR/GBP) invoicing went live in {P[7]}.")
        conceal = ["duplicate rows from the ingestion rerun", "unapplied cash", "ingest_id dedupe key", "cutoff items posted in another quarter",
                   "billing interface errors parked in suspense", "the list of breaks and their amounts", *[a.id for a in state.anomalies]]
        stakeholders = [
            Stakeholder(name=names[0], role="Controller", agenda="Deliver a clean tie-out to the auditor by Friday.", disposition="pressed for time",
                        knows=["the suspense balance appeared recently"], hides=["approved booking prior differences to suspense"]),
            Stakeholder(name=names[1], role="Data Engineer", agenda="Keep the pipelines green.", disposition="defensive",
                        knows=["reran the ingestion job after the outage"], hides=["the loader has no idempotency check"]),
            Stakeholder(name=names[2], role="AR Clerk", agenda="Clear the cash-application queue.", disposition="helpful",
                        knows=["several wires came in without remittance advice"], hides=[]),
            Stakeholder(name=names[3], role="External Auditor", agenda="Test the reconciliation and conclude on AR.", disposition="methodical",
                        knows=["unreconciled AR is a common material-weakness finding"], hides=[]),
        ]
        tl = [
            TimelineEvent(period=P[5], headline="Billing system migration", detail="Invoices moved to the new billing platform.", is_red_herring=True),
            TimelineEvent(period=P[6], headline="Ingestion rerun after outage", detail="The nightly billing export was reloaded."),
            TimelineEvent(period=P[6], headline="New wire remittance process", detail="Customers asked to send remittance to a new mailbox."),
            TimelineEvent(period=P[7], headline="Audit fieldwork begins", detail="PBC list includes the AR reconciliation."),
            TimelineEvent(period=P[7], headline="Suspense balance queried", detail="The auditor asks what the suspense balance contains."),
        ]
        if "fx_rounding" in p["breaks"]:
            tl.append(TimelineEvent(period=P[7], headline="Multi-currency invoicing live", detail="EUR and GBP billing enabled."))
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[6], headline="GL chart-of-accounts cleanup", detail="Finance renamed several GL accounts.", is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[7], headline="FX volatility", detail="EUR moved sharply during the quarter.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["auditor_pbc_list_excerpt", "suspense_gl_detail", "ingestion_job_log_excerpt", "ar_aging_export"],
                           architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Spec levers (categorical levers integer-coded); defaults are the status quo."""
        return [
            Lever(key="post_adjusting_entries", label="Post the adjusting entries (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Clears suspense and the GL-side reconciling items; GL ties to the clean sub-ledger."),
            Lever(key="fix_ingestion_idempotency", label="Make the billing ingestion idempotent (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="No new duplicate loads."),
            Lever(key="remittance_matching_rule", label="Cash application rule (0 none, 1 amount+customer, 2 ML)", min=0, max=2, default=0, unit="code",
                  effect="Unapplied cash falls ~45% per step."),
            Lever(key="allowance_true_up_pct", label="Allowance true-up on 90+ aging", min=0, max=5, default=0, unit="% of AR",
                  effect="Bad-debt expense now; allowance covers the aged items."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model (module docstring)."""
        b = self._breaks(state)
        P = state.periods
        it = b["items"]
        om = state.params["omission"]
        tb8 = state.ledger.trial_balance(P[-1])
        gl_ar = float(tb8["1100"])
        aging = self._aging(state)
        unexplained = gl_ar - b["dedup"][P[-1]]
        gl_side = float(-(it[om][P[-1]]) - it["cutoff_shift"][P[-1]] - it["fx_rounding"][P[-1]])
        inc8 = next(i for i in state.statements.income if i.period == P[-1])  # type: ignore[union-attr]
        kpis = {
            "unexplained_difference_cents": float(unexplained), "suspense_balance_cents": float(-tb8["1400"]),
            "duplicate_rows": float(len(state.extras["dup_rows"])), "unapplied_cash_cents": float(it["unapplied_cash"][P[-1]]),
            "ar_over_90_cents": float(aging["90_plus"]), "allowance_cents": float(-tb8["1110"]), "net_income_cents": float(inc8.net_income_cents),
            "audit_finding_severity": 2.0, "close_days": 9.0,
        }
        per_q_unapp = float(it["unapplied_cash"][P[-1]] - it["unapplied_cash"][P[5]]) / 2
        per_q_susp = float(-(it[om][P[-1]] - it[om][P[4]])) / 3
        effects = [
            {"lever": "post_adjusting_entries", "kpi": "suspense_balance_cents", "delta_per_unit": -kpis["suspense_balance_cents"], "lag_periods": 0},
            {"lever": "post_adjusting_entries", "kpi": "unexplained_difference_cents", "delta_per_unit": -gl_side, "lag_periods": 0},
            {"lever": "post_adjusting_entries", "kpi": "audit_finding_severity", "delta_per_unit": -1.0, "lag_periods": 0},
            {"lever": "post_adjusting_entries", "kpi": "close_days", "delta_per_unit": -2.0, "lag_periods": 1},
            {"lever": "fix_ingestion_idempotency", "kpi": "duplicate_rows", "delta_per_unit": -kpis["duplicate_rows"], "lag_periods": 0},
            {"lever": "fix_ingestion_idempotency", "kpi": "audit_finding_severity", "delta_per_unit": -0.5, "lag_periods": 0},
            {"lever": "remittance_matching_rule", "kpi": "unapplied_cash_cents", "delta_per_unit": -0.45 * kpis["unapplied_cash_cents"], "lag_periods": 0},
            {"lever": "remittance_matching_rule", "kpi": "unexplained_difference_cents", "delta_per_unit": 0.45 * kpis["unapplied_cash_cents"], "lag_periods": 0},
            {"lever": "remittance_matching_rule", "kpi": "ar_over_90_cents", "delta_per_unit": -0.2 * kpis["unapplied_cash_cents"], "lag_periods": 1},
            {"lever": "allowance_true_up_pct", "kpi": "allowance_cents", "delta_per_unit": gl_ar / 100, "lag_periods": 0},
            {"lever": "allowance_true_up_pct", "kpi": "net_income_cents", "delta_per_unit": -gl_ar / 100, "lag_periods": 0},
        ]
        drag = {"unapplied_cash_cents": per_q_unapp, "suspense_balance_cents": per_q_susp, "duplicate_rows": float(len(state.extras["dup_rows"])) / 2,
                "audit_finding_severity": 0.5, "unexplained_difference_cents": -per_q_unapp + per_q_susp}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"post_adjusting_entries": 1.0, "fix_ingestion_idempotency": 1.0},
                "bounds": {"audit_finding_severity": [0.0, 3.0], "duplicate_rows": [0.0, None], "suspense_balance_cents": [0.0, None],
                           "unapplied_cash_cents": [0.0, None], "close_days": [3.0, 20.0]}}


def base_rate_for(ccy: np.ndarray, base: dict[str, float]) -> np.ndarray:
    """Reference rate used to convert a USD price list into the customer's currency."""
    return np.array([base[c] for c in ccy], dtype=float)


def _with_hints(metrics: list[Any], hints: dict[str, str]) -> list[Any]:
    """Attach a learner-facing hint to every expected metric (``CasePublic.asks``); unknown keys get a generic hint."""
    out = []
    for m in metrics:
        h = m.hint or hints.get(m.key) or next((v for k, v in hints.items() if k.endswith("*") and m.key.startswith(k[:-1])), "")
        out.append(m.model_copy(update={"hint": h or f"Compute {m.label.lower()} from the case database ({m.unit})."}))
    return out

_HINTS: dict[str, str] = {'gl_ar_close_p8_cents': 'Sum debit - credit on account 1100 in gl_lines through the latest quarter.', 'subledger_ar_naive_cents': 'Invoices - applied payments - credit memos - write-offs, using every loaded invoice row.', 'subledger_ar_deduped_cents': 'Same roll-forward after keeping one row per invoice_id (lowest ingest_id).', 'unexplained_difference_cents': 'GL 1100 minus the deduplicated sub-ledger balance.', 'duplicate_invoice_count': 'Count invoice rows beyond the first per invoice_id.', 'duplicate_invoice_cents': 'Sum the amounts of those extra rows.', 'unapplied_cash_cents': 'Payments with no invoice reference.', 'suspense_balance_p8_cents': 'Running balance of account 1400 in the GL.', 'ar_rollforward_p8_*': 'Build the AR roll-forward by quarter (opening + invoiced - collected - credits - write-offs = closing); normalise payment timestamps to UTC.', 'aging_bucket_*': 'Age deduplicated open invoices by days past due at quarter end.', 'reconciling_item_*': "Quantify this item's effect on GL minus the clean sub-ledger (signed).", 'cutoff_items_cents': 'Anti-join latest-quarter invoices to their GL lines (invoice_id in gl_lines.dimensions).'}
