"""``cohort_collapse`` - "Leaky Bucket" (archetypes.md section 6).

A subscription business (B2B SaaS, or a subscription-box e-commerce brand) reports record logo
growth and a "stable ~2%" churn rate while every cohort acquired since P5 leaks out roughly twice as
fast as the older ones. The aggregate metrics hide it: acquisition spend ramps, the installed base
swells the denominator, and much of the new cohorts' churn happens inside the quarter they were
acquired, which the board's quarterly logo-churn KPI never sees.

Mechanics (every invariant and sub-ledger tie holds)
----------------------------------------------------
* Customers are acquired monthly from 60 months before the books open (legacy installed base)
  through P8: ``A_m = A0 * 1.03^(m/3)`` and, from P5, ``* (1 + acquisition_growth)^((m-14)/3)``.
  Segment (smb / midmarket / enterprise at principal), channel, plan (monthly / annual; the annual
  share drops from ~40% to ~15% for P5+ cohorts), discount and start date are drawn per customer.
* Lifetime ``T`` (billing periods paid): with probability ``p_early = early_mult * churn_seg`` the
  customer leaves after its first period, otherwise ``T = 1 + Geometric(theta)``,
  ``theta ~ Beta(mu*kappa, (1-mu)*kappa)`` with ``mu = churn_seg`` (shifted-beta-geometric,
  Fader & Hardie 2007). Annual plans only churn at renewal (``T`` rounded up to 12). A small
  involuntary hazard (failed cards) rises in P7-P8 (processor migration: red herring 3).
* **A1 collapse**: the *collapsed* group's ``mu`` is ``collapse_multiplier`` x the segment churn and
  its early churn ``x U(1.5, 2.0)``. The group depends on the seeded cause: ``channel_shift`` =
  P5+ customers from the new ``paid_social`` channel (share 10% -> ~45%); ``onboarding_regression``
  = every P5+ customer (first-14-day usage halves, onboarding tickets x3); ``discount_cohorts`` =
  P5+ customers on a >= 30% three-month promo (extra churn at promo end).
* **A2 masking**: marketing spend (and logos) ramp from P5; capitalized commissions (1350,
  ASC 340-40) are amortized straight-line over 36 months and never written off on churn, so the
  asset keeps growing although the post-P5 customer life is much shorter.
* **A3 deferred revenue mix**: fewer annual prepayments -> deferred revenue (2200) growth and CFO
  slow before revenue does.
* Billing: anniversary billing in advance (monthly or annual); monthly plans on card auto-pay, annual contracts
  invoiced on net-30 terms (paid 18-48 days later, so AR exists); failed first card attempts
  retried; involuntary churners' final invoice is voided). Revenue is recognized daily over the
  service period (integer cent allocation per invoice), commissions are a sub-ledger, so the AR,
  deferred revenue, revenue and commission-asset ties hold exactly.

Documented simplifications
* ``payments`` holds only failed attempts and their retries (dunning); first-attempt successes are
  recorded on the invoice (``status``, ``paid_at``) to respect the row budget.
* Retention is measured on ``subscriptions`` (``start_date``, ``ended_at`` = end of the last paid
  period); age ``k`` = billing period ``k`` (retention(k) = share with more than ``k`` paid periods).
* ``fintech_marketplace`` is not offered (SaaS default, e-commerce subscription box variant).

Simulation (reduced form): KPIs are P8 levels. ``marketing_spend_change_pct`` moves new logos by 0.7% per point
(diminishing returns) and S&M / cash 1:1. The levers are in their natural units with the status quo as default:
``paid_social_share_pct`` (share of new logos from paid social) and ``promo_depth_pct`` (maximum promotional discount)
are fixed by *cutting* them (``resolved_if`` ``<=`` via ``at_most``), ``onboarding_investment_cents`` by spending
(``>=``). Each costs some logos or cash, but only the lever matching the seeded cause removes the excess new-cohort
churn (half after one quarter, half after two) with a lagged MRR / LTV-CAC effect; ``commission_amortization_months``
(default 36) raises amortization and releases the over-capitalized asset when shortened. ``anomaly_drag`` = P5-P8 drift
of blended churn and the commission asset plus the MRR lost to excess churn; ``resolved_if`` = cause lever ~80% of the
way back to the pre-P5 level (onboarding: half of the budget) and the commission life <= 30 months; the status quo
never resolves. The ``price_change_pct`` lever of the spec is not modelled (5 levers).

Funding (review P1 #9): the books open with a thin cash balance and a preferred equity round closes in the warm-up
quarter P0 (it is the cash pile the case starts with); venture debt is drawn at the start of P5 to fund the acquisition
ramp (interest accrued monthly, paid quarterly), so CFF and the debt / equity lines move with the burn. A further
equity round is raised if cash falls below ~1.5 months of revenue.
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
    at_least,
    at_most,
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
from axiom.engine.operations import (
    build_operations_state,
    cohort_from_curve,
    mm1,
    sbg_fit,
    sbg_survival,
    unit_economics,
)
from axiom.engine.rng import SeededRNG, excluded_name_stems
from axiom.models import (
    Anomaly,
    Cohort,
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

logger = logging.getLogger("axiom.archetypes.cohort_collapse")

CAUSES = ("channel_shift", "onboarding_regression", "discount_cohorts")
CAUSE_CODE = {c: float(i + 1) for i, c in enumerate(CAUSES)}
CHANNELS = ("organic", "outbound", "partner", "paid_search", "paid_social")
CH_CAC = np.array([0.35, 1.3, 0.9, 1.0, 0.7])  # relative CAC by channel
LEGACY_MONTHS = 60
COMMISSION_LIFE = 36
INDUSTRY = {
    "saas": {"segments": ("smb", "midmarket", "enterprise"), "arpa_rel": (1.0, 4.0, 14.0), "share": (0.62, 0.38, 0.10),
             "plans": ("Starter", "Growth", "Scale"), "business_model": "B2B workflow SaaS sold per seat, monthly or annual"},
    "ecommerce": {"segments": ("single", "family", "office"), "arpa_rel": (1.0, 1.7, 3.5), "share": (0.6, 0.4, 0.10),
                  "plans": ("Single box", "Family box", "Office box"), "business_model": "subscription-box e-commerce brand (monthly or annual prepay)"},
}


def _alloc(amount: np.ndarray, cum_frac: np.ndarray) -> np.ndarray:
    """Integer cents allocation: ``round(A*cum_k) - round(A*cum_{k-1})`` per column (sums to A exactly)."""
    c = np.floor(amount[:, None] * cum_frac + 0.5)
    prev = np.concatenate([np.zeros((len(amount), 1)), c[:, :-1]], axis=1)
    return (c - prev).astype(np.int64)


@register
class CohortCollapse(Archetype):
    """Leaky Bucket: post-P5 cohorts collapse while blended churn looks flat."""

    key = "cohort_collapse"
    title = "Leaky Bucket"
    pillars = ("operations_strategy", "accounting_finance", "sql")
    industries = ("saas", "ecommerce")
    schema_template = "saas_billing"
    summary = ("Logo growth is at a record and the board deck says churn is stable at ~2%, yet MRR growth is stalling and the "
               "commission asset keeps growing. What is happening to the customers acquired in the last year?")
    root_cause_terms = ("cohort-level retention", "cause marker", "commission capitalization life", "projection")
    bs_caption_exemptions = {"ecommerce:inventor": "subscription boxes are packed and shipped by a fulfilment partner billed per box "
                                                     "(cost of revenue); the brand holds no stock in this model"}
    stage_options = ("growth",)  # the narrative is a growth story

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 6."""
        ind = industry or "saas"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        cfg = INDUSTRY[ind]
        lo, hi = (15e6, 80e6) if ind == "saas" else (10e6, 60e6)
        rev_usd = math.exp(rng.uniform(math.log(lo), math.log(hi)))
        n_seg = difficulty_pick(difficulty, 2, 2, 3)
        segs = cfg["segments"][:n_seg]
        if ind == "saas":
            churn = [float(np.clip(bk.sample_between(pool, rng.spawn(f"bench:churn:{s}"), f"saas.gross_churn_monthly_{s}", "saas", 0.35, 0.6),
                                   0.004, 0.06)) for s in segs]
            gm = float(np.clip(bk.sample_between(pool, rng.spawn("bench:gm"), "saas.gross_margin", "saas", 0.4, 0.6), 0.65, 0.82))
        else:
            m6 = bk.sample_between(pool, rng.spawn("bench:m6"), "operations.cohort_retention_subscription_m6", ind, 0.5, 0.75)
            base = float(np.clip(1 - m6 ** (1 / 6) * 1.05, 0.03, 0.08))
            churn = [base * f for f in (1.0, 0.8, 0.6)[:n_seg]]
            gm = float(np.clip(bk.sample_between(pool, rng.spawn("bench:gm"), "ecommerce.gross_margin", ind, 0.4, 0.6), 0.38, 0.6))
        payback = float(np.clip(bk.sample_between(pool, rng.spawn("bench:payback"), "saas.cac_payback_months", "saas", 0.3, 0.55), 10, 22))
        cause = str(CAUSES[int(rng.spawn("cause").integers(0, len(CAUSES)))])
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": cfg["business_model"],
            "revenue_ttm_cents": int(rev_usd * 100),
            "segments": list(segs),
            "segment_share": [float(x) for x in np.array(cfg["share"][:n_seg]) / sum(cfg["share"][:n_seg])],
            "arpa_rel": list(cfg["arpa_rel"][:n_seg]),
            "churn_seg": churn,
            "early_mult": float(rng.uniform(1.5, 3.0)),
            "kappa": float(rng.uniform(10, 16)),
            "gm": gm,
            "cac_payback_months": payback,
            "expansion_monthly": float(rng.uniform(0.003, 0.012)),
            "collapse_start_month": 3 + 12,  # first month of P5
            "collapse_multiplier": magnitude(rng, difficulty, 1.6, 2.6),
            "early_collapse": float(rng.uniform(1.5, 2.0)),
            "collapse_cause": cause,
            "paid_social_share_pre": 0.10,
            "paid_social_share_post": float(rng.uniform(0.40, 0.50)),
            "discount_share_post": float(rng.uniform(0.45, 0.55)),
            "promo_end_churn": float(rng.uniform(0.30, 0.45)),
            "acquisition_growth": float(rng.uniform(0.12, 0.25)),
            "annual_share_pre": float(rng.uniform(0.35, 0.45)),
            "annual_share_post": float(rng.uniform(0.12, 0.18)),
            "annual_discount": float(np.clip(bk.sample_between(pool, rng.spawn("bench:annual"), "saas.annual_prepay_discount", "saas", 0.3, 0.6), 0.1, 0.2)),
            "commission_pct": float(rng.uniform(0.08, 0.12)),
            "commission_life_months": COMMISSION_LIFE,
            "fail_rate_base": float(rng.uniform(0.02, 0.04)),
            "fail_rate_migration": float(rng.uniform(0.05, 0.07)),
            "invol_hazard_base": 0.0015,
            "invol_hazard_migration": 0.004,
            "rd_pct": float(rng.uniform(0.18, 0.26)) if ind == "saas" else float(rng.uniform(0.03, 0.06)),
            "ga_pct": float(rng.uniform(0.10, 0.14)),
            "sales_payroll_share": float(rng.uniform(0.35, 0.5)) if ind == "saas" else 0.1,
            "tax_rate": 0.21,
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        budget = ROW_BUDGET[difficulty]
        gl_rows = difficulty_pick(difficulty, 2400, 2700, 10500)
        p["target_active"] = int(max(150, (budget * difficulty_pick(difficulty, 0.86, 0.88, 0.86) - gl_rows - 300) / 31.0))
        p["description"] = (f"{cap_first(p['business_model'])}; growth is funded by a ramp in paid acquisition and sales commissions "
                            "are capitalized under ASC 340-40.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Acquisition plan (legacy installed base + window), customer attributes and healthy lifetimes."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=True)  # base working-capital defaults: vendor AP for part of the accrued-expense run and prepaid expenses
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        churn = np.array(p["churn_seg"])
        share = np.array(p["segment_share"])
        mean_churn = float((share * churn).sum())
        A0 = p["target_active"] * mean_churn * 1.15
        m_idx = np.arange(-LEGACY_MONTHS, n_m)
        cs = p["collapse_start_month"]
        acq = A0 * 1.03 ** (np.minimum(m_idx, cs - 1) / 3.0) * np.where(m_idx >= cs, (1 + p["acquisition_growth"]) ** ((m_idx - cs + 1) / 3.0), 1.0)
        counts = g.poisson(acq * g.lognormal(0, 0.05, len(acq)))
        a = np.repeat(m_idx, counts)  # acquisition month index (0 = first month of P0)
        N = len(a)
        start0 = pd.Timestamp(cal.start)
        m_start = np.array([start0 + pd.DateOffset(months=int(k)) for k in range(-LEGACY_MONTHS, n_m + 1)])
        dim = np.array([(m_start[i + 1] - m_start[i]).days for i in range(len(m_start) - 1)])
        day = (g.random(N) * dim[a + LEGACY_MONTHS]).astype(int)
        signup = pd.Series(pd.to_datetime(m_start[a + LEGACY_MONTHS]) + pd.to_timedelta(day, unit="D") + pd.to_timedelta(g.integers(8 * 3600, 20 * 3600, N), unit="s"))
        seg = g.choice(len(churn), size=N, p=share)
        post = a >= cs
        # channels: paid_social share rises for post cohorts under the channel_shift cause
        ps_share = np.where(post & (p["collapse_cause"] == "channel_shift"), p["paid_social_share_post"], p["paid_social_share_pre"])
        other = np.array([0.30, 0.22, 0.18, 0.30])
        u = g.random(N)
        ch_other = g.choice(4, size=N, p=other / other.sum())
        channel = np.where(u < ps_share, 4, ch_other)
        if p["collapse_cause"] != "channel_shift":  # spend ramp goes to paid search / outbound
            channel = np.where(post & (channel == 4) & (g.random(N) < 0.5), 3, channel)
        annual = g.random(N) < np.where(post, p["annual_share_post"], p["annual_share_pre"])
        disc_promo = post & (p["collapse_cause"] == "discount_cohorts") & (g.random(N) < p["discount_share_post"]) & ~annual
        discount = np.where(disc_promo, np.round(g.uniform(30, 40, N)), np.where(g.random(N) < 0.06, np.round(g.uniform(10, 20, N)), 0.0))
        state.extras.update({"a": a, "N": N, "signup": signup, "seg": seg, "post": post, "channel": channel, "annual": annual,
                             "disc_promo": disc_promo, "discount": discount, "m_start": m_start, "acq_expected": acq})
        state.extras["collapsed"] = np.zeros(N, dtype=bool)

    # ------------------------------------------------------------------ anomalies

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Mark the collapsed group (cause-specific) and draw every lifetime."""
        p = state.params
        E = state.extras
        P = state.periods
        cause = p["collapse_cause"]
        post = E["post"]
        collapsed = {"channel_shift": post & (E["channel"] == 4), "onboarding_regression": post.copy(), "discount_cohorts": E["disc_promo"]}[cause]
        E["collapsed"] = collapsed
        self._lifetimes(ctx, state)
        marker = {"channel_shift": "customers.acquisition_channel = 'paid_social' for cohorts acquired from P5",
                  "onboarding_regression": "first-14-day usage (usage_events) halves and onboarding tickets triple for cohorts acquired from P5",
                  "discount_cohorts": "subscriptions.discount_pct >= 30 (three-month promo) for cohorts acquired from P5"}[cause]
        return [
            Anomaly(id="A1", kind="cohort_retention_collapse", pillar="operations_strategy", title="Post-P5 cohorts churn ~2x faster",
                    mechanism=(f"Cause '{cause}': {marker}. Their steady monthly churn is {p['collapse_multiplier']:.2f}x the segment base and their "
                               f"first-period churn {p['early_collapse']:.2f}x higher; older cohorts are unchanged."),
                    periods=list(P[4:]), affected_accounts=["4000", "2200"], affected_tables=["subscriptions", "customers", "invoices", "usage_events", "support_tickets"],
                    magnitude={"collapse_multiplier": round(p["collapse_multiplier"], 6), "early_collapse": round(p["early_collapse"], 6),
                               "collapsed_customers": float(collapsed.sum())}, severity="high"),
            Anomaly(id="A2", kind="aggregate_masking_commission_asset", pillar="accounting_finance", title="Masking: acquisition ramp and a growing commission asset",
                    mechanism=(f"From P5 new logos grow {p['acquisition_growth']:.0%} per quarter on rising spend; the quarterly logo-churn KPI excludes "
                               "customers acquired and lost within the quarter. Commissions (8-12% of first-year ACV) are capitalized to 1350 and amortized "
                               "over 36 months with no write-off when a customer churns."),
                    periods=list(P[4:]), affected_accounts=["1350", "6010", "6000"], affected_tables=["marketing_spend", "commissions"],
                    magnitude={"acquisition_growth_qoq": round(p["acquisition_growth"], 6), "commission_pct": round(p["commission_pct"], 6)}, severity="medium"),
            Anomaly(id="A3", kind="deferred_revenue_mix_shift", pillar="accounting_finance", title="Annual-prepay share collapses among new cohorts",
                    mechanism=(f"Annual plan share falls from ~{p['annual_share_pre']:.0%} to ~{p['annual_share_post']:.0%} of new customers from P5, "
                               "so deferred revenue growth and operating cash flow slow before revenue does."),
                    periods=list(P[4:]), affected_accounts=["2200", "1000"], affected_tables=["subscriptions", "invoices"],
                    magnitude={"annual_share_pre": round(p["annual_share_pre"], 6), "annual_share_post": round(p["annual_share_post"], 6)}, severity="low"),
        ]

    def _lifetimes(self, ctx: BuildContext, state: WorldState) -> None:
        p, E = state.params, state.extras
        g = ctx.rng.spawn("lifetimes").generator
        N = E["N"]
        churn = np.array(p["churn_seg"])[E["seg"]]
        col = E["collapsed"]
        mu = np.clip(churn * np.where(col, p["collapse_multiplier"], 1.0), 1e-4, 0.5)
        k = p["kappa"]
        theta = g.beta(mu * k, (1 - mu) * k)
        p_early = np.clip(p["early_mult"] * churn * np.where(col, p["early_collapse"], 1.0), 0, 0.6)
        T = np.where(g.random(N) < p_early, 1, 1 + g.geometric(np.clip(theta, 1e-6, 1.0)))
        if p["collapse_cause"] == "discount_cohorts":
            T = np.where(col & (T > 3) & (g.random(N) < p["promo_end_churn"]), 3, T)
        T = np.where(E["annual"], np.ceil(T / 12.0).astype(int) * 12, T)
        # involuntary churn (failed cards): hazard by calendar month, higher during the processor migration (P7-P8)
        n_m = len(E["months"])
        a = E["a"]
        cm = np.arange(-LEGACY_MONTHS, n_m + 60)
        h_by_month = np.where((cm >= 3 + 18) & (cm < 3 + 21), p["invol_hazard_migration"], p["invol_hazard_base"])  # P7 only
        u = g.random((N, 1))
        invol_T = np.full(N, 10_000)
        ages = np.arange(1, 64)
        cal_month = a[:, None] + ages[None, :]
        haz = h_by_month[np.clip(cal_month + LEGACY_MONTHS, 0, len(h_by_month) - 1)]
        surv = np.cumprod(1 - haz, axis=1)
        hit = surv < (1 - u)
        any_hit = hit.any(axis=1)
        invol_T[any_hit] = ages[np.argmax(hit[any_hit], axis=1)]
        invol = (invol_T < T) & ~E["annual"]
        T = np.where(invol, invol_T, T)
        E["T"] = np.minimum(T, 400).astype(np.int64)  # censor absurd geometric draws (theta ~ 0)
        E["invol"] = invol
        E["theta"] = theta

    # ------------------------------------------------------------------ derived tables

    def _derive(self, ctx: BuildContext, state: WorldState) -> None:
        """Pricing scale, subscriptions, invoices (+ recognition), payments, usage, tickets, spend, commissions."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        E = state.extras
        n_m = len(E["months"])
        start0 = pd.Timestamp(cal.start)
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        T, N = E["T"], E["N"]
        signup = E["signup"]
        # keep customers whose service overlaps the window (legacy customers alive at the start, and window acquisitions)
        svc_end = _add_months(signup.to_numpy(), T)
        keep = (svc_end > np.datetime64(start0)) & (signup.to_numpy() <= np.datetime64(end_ts))
        idx = np.flatnonzero(keep)
        E["keep"] = idx
        # base MRR (relative units, scaled to the revenue target below)
        arpa_rel = np.array(p["arpa_rel"])[E["seg"]] * g.lognormal(0, 0.35, N)
        # invoices: monthly plans one per period, annual one per 12 periods
        rows_c, rows_k = [], []
        for i in idx:
            step = 12 if E["annual"][i] else 1
            ks = np.arange(0, int(T[i]), step)
            rows_c.append(np.full(len(ks), i))
            rows_k.append(ks)
        ci = np.concatenate(rows_c)
        kk = np.concatenate(rows_k)
        per_len = np.where(E["annual"][ci], 12, 1)
        ps = _add_months(signup.to_numpy()[ci], kk)
        pe = _add_months(signup.to_numpy()[ci], kk + per_len)
        inwin = (pe > np.datetime64(start0)) & (ps <= np.datetime64(end_ts))
        ci, kk, per_len, ps, pe = ci[inwin], kk[inwin], per_len[inwin], ps[inwin], pe[inwin]
        disc = np.where(E["disc_promo"][ci] & (kk < 3), E["discount"][ci], np.where(E["disc_promo"][ci], 0.0, E["discount"][ci])) / 100.0
        growth = (1 + p["expansion_monthly"]) ** kk
        mrr_rel = arpa_rel[ci] * growth
        gross_rel = np.where(E["annual"][ci], 12 * mrr_rel * (1 - p["annual_discount"]), mrr_rel)
        net_rel = gross_rel * (1 - disc)
        # recognition (calendar-month allocation over the service period), relative units first for scaling
        rec_m0 = _month_index(ps, start0)
        span = 14
        bounds = np.stack([_add_months(_month_floor(ps), np.full(len(ps), j)) for j in range(span + 1)], axis=1)
        lo = np.maximum(bounds[:, :-1], ps[:, None])
        hi = np.minimum(bounds[:, 1:], pe[:, None])
        days = np.maximum(0, (hi - lo).astype("timedelta64[s]").astype(np.int64))
        cum = np.cumsum(days, axis=1) / days.sum(axis=1, keepdims=True)
        month_of = rec_m0[:, None] + np.arange(span)[None, :]
        last12 = (month_of >= n_m - 12) & (month_of < n_m)
        rel_ttm = float((net_rel[:, None] * np.diff(np.concatenate([np.zeros((len(ps), 1)), cum], axis=1), axis=1) * last12).sum())
        scale = p["revenue_ttm_cents"] / rel_ttm
        amount = np.maximum(100, np.round(net_rel * scale)).astype(np.int64)
        subtotal = np.maximum(amount, np.round(gross_rel * scale)).astype(np.int64)
        alloc = _alloc(amount.astype(float), cum)
        E["mrr_base_cents"] = arpa_rel * scale
        # payments / statuses
        inv_date = pd.Series(ps)
        mig = (_month_index(ps, start0) >= 3 + 18) & (_month_index(ps, start0) < 3 + 21)
        fail = g.random(len(ps)) < np.where(mig, p["fail_rate_migration"], p["fail_rate_base"])
        # involuntary churners: an extra final invoice at period T that is voided after 3 failed attempts
        inv_c = np.flatnonzero(E["invol"][idx])
        inv_ci = idx[inv_c]
        v_ps = _add_months(signup.to_numpy()[inv_ci], T[inv_ci])
        v_ok = (v_ps >= np.datetime64(start0)) & (v_ps <= np.datetime64(end_ts))
        inv_ci, v_ps = inv_ci[v_ok], v_ps[v_ok]
        delay = np.where(fail, g.uniform(3, 8, len(ps)), g.uniform(0.0, 0.2, len(ps)))
        # annual contracts are invoiced on net-30 terms (ACH / wire), so receivables exist (review P0 #3); own stream
        terms = ctx.rng.spawn("annual_terms").generator.uniform(18, 48, len(ps))
        delay = np.where((per_len == 12) & ~fail, terms, delay)
        paid_at = inv_date + pd.to_timedelta(delay, unit="D")
        paid = (paid_at <= end_ts).to_numpy()
        inv = pd.DataFrame({
            "_c": ci, "_k": kk, "customer_idx": ci, "invoice_date": inv_date, "period_start": inv_date, "period_end": pd.Series(pe),
            "billing_interval": np.where(per_len == 12, "year", "month"), "subtotal_cents": subtotal, "discount_cents": subtotal - amount,
            "total_cents": amount, "status": np.where(paid, "paid", "open"), "paid_at": paid_at.where(paid), "_fail": fail,
        })
        void = pd.DataFrame({"_c": inv_ci, "_k": T[inv_ci], "customer_idx": inv_ci, "invoice_date": pd.Series(v_ps), "period_start": pd.Series(v_ps),
                             "period_end": pd.Series(_add_months(v_ps, np.ones(len(v_ps), dtype=np.int64))), "billing_interval": "month",
                             "subtotal_cents": np.round(arpa_rel[inv_ci] * scale * (1 + p["expansion_monthly"]) ** T[inv_ci]).astype(np.int64),
                             "discount_cents": 0, "total_cents": np.round(arpa_rel[inv_ci] * scale * (1 + p["expansion_monthly"]) ** T[inv_ci]).astype(np.int64),
                             "status": "void", "paid_at": pd.NaT, "_fail": True})
        allinv = pd.concat([inv, void], ignore_index=True)
        order = np.lexsort((allinv["_c"].to_numpy(), allinv["invoice_date"].to_numpy()))
        allinv = allinv.iloc[order].reset_index(drop=True)
        valid_alloc = np.concatenate([alloc, np.zeros((len(void), span), dtype=np.int64)])[order]
        valid_month = np.concatenate([month_of, np.zeros((len(void), span), dtype=np.int64)])[order]
        allinv.insert(0, "invoice_id", [f"in_{i:07d}" for i in range(1, len(allinv) + 1)])
        E["invoices"] = allinv
        E["alloc"] = valid_alloc
        E["alloc_month"] = valid_month
        E["scale"] = scale
        # payment attempts (failed first attempts + retries; voided invoices: three failures)
        f_inv = allinv[(allinv["_fail"]) & (allinv["status"] != "void")]
        v_inv = allinv[allinv["status"] == "void"]
        codes = np.array(("card_declined", "insufficient_funds", "expired_card", "processing_error"))
        att = [
            pd.DataFrame({"invoice_id": f_inv["invoice_id"], "_c": f_inv["_c"], "amount_cents": f_inv["total_cents"], "status": "failed",
                          "failure_code": codes[g.integers(0, 4, len(f_inv))], "attempted_at": f_inv["invoice_date"] + pd.Timedelta(minutes=5)}),
            pd.DataFrame({"invoice_id": f_inv["invoice_id"], "_c": f_inv["_c"], "amount_cents": f_inv["total_cents"], "status": "succeeded",
                          "failure_code": None, "attempted_at": f_inv["paid_at"]}),
        ]
        for r in range(3):
            att.append(pd.DataFrame({"invoice_id": v_inv["invoice_id"], "_c": v_inv["_c"], "amount_cents": v_inv["total_cents"], "status": "failed",
                                     "failure_code": codes[g.integers(0, 4, len(v_inv))], "attempted_at": v_inv["invoice_date"] + pd.Timedelta(days=3 * r, minutes=5)}))
        pay = pd.concat(att, ignore_index=True)
        pay = pay[pay["attempted_at"].notna() & (pay["attempted_at"] <= end_ts)].sort_values(["attempted_at", "invoice_id"], kind="stable").reset_index(drop=True)
        pay.insert(0, "payment_id", [f"py_{i:06d}" for i in range(1, len(pay) + 1)])
        E["payments"] = pay
        # customers / subscriptions
        cust_ids = np.array([f"cus_{i:06d}" for i in range(1, N + 1)])
        seg_names = np.array(p["segments"])
        plans = INDUSTRY[p["industry"]]["plans"]
        svc_end_ts = pd.Series(svc_end)
        ended = (svc_end_ts <= end_ts).to_numpy()
        cancel_reason = np.where(E["invol"], "payment_failed", np.array(("too_expensive", "missing_features", "switched_service", "low_usage", "other"))[
            np.minimum(4, (g.random(N) ** 1.4 * 5).astype(int))])
        su_idx = pd.DatetimeIndex(signup)
        age_end = (end_ts.year - su_idx.year.to_numpy()) * 12 + (end_ts.month - su_idx.month.to_numpy())
        k_last = np.maximum(0, np.minimum(T - 1, age_end))
        k_last = np.where(E["annual"], (k_last // 12) * 12, k_last)
        sub = pd.DataFrame({
            "subscription_id": [f"sub_{i:06d}" for i in range(1, N + 1)], "customer_id": cust_ids,
            "plan_id": np.array([f"plan_{s}_{'y' if an else 'm'}" for s, an in zip(seg_names[E["seg"]], E["annual"], strict=True)]),
            "seats": np.maximum(1, np.round(np.array(p["arpa_rel"])[E["seg"]] * g.lognormal(1.2, 0.4, N))).astype(np.int64),
            "status": np.where(ended, "canceled", "active"), "start_date": signup.dt.normalize(),
            "ended_at": svc_end_ts.dt.normalize().where(ended), "cancel_reason": np.where(ended, cancel_reason, None),
            "discount_pct": E["discount"], "contract_months": np.where(E["annual"], 12, 1),
            "mrr_cents": np.round(arpa_rel * scale * (1 + p["expansion_monthly"]) ** k_last * np.where(E["annual"], 1 - p["annual_discount"], 1.0)).astype(np.int64),
        })
        sub = sub.iloc[idx].reset_index(drop=True)
        cust = pd.DataFrame({
            "customer_id": cust_ids, "name": [f"{rng_name(g)} {s}" for s in np.array(("Labs", "Group", "Co", "Studio", "Partners", "Works"))[g.integers(0, 6, N)]],
            "segment": seg_names[E["seg"]], "industry": np.array(("software", "healthcare", "retail", "finance", "education", "manufacturing"))[g.integers(0, 6, N)],
            "country": np.array(("US", "US", "US", "CA", "GB", "DE", "AU"))[g.integers(0, 7, N)], "created_at": signup,
            "acquisition_channel": np.array(CHANNELS)[E["channel"]],
            "billing_email": [f"billing@acct{i:06d}.example.com" for i in range(1, N + 1)],
        }).iloc[idx].reset_index(drop=True)
        plan_rows = []
        for si, s in enumerate(p["segments"]):
            unit = int(round(p["arpa_rel"][si] * scale / max(1, np.median(sub.loc[sub["plan_id"].str.contains(f"_{s}_"), "seats"]) if len(sub) else 1)))
            plan_rows.append({"plan_id": f"plan_{s}_m", "name": f"{plans[si]} monthly", "billing_interval": "month", "unit_amount_cents": unit, "tier": s,
                              "currency": "USD", "active": True})
            plan_rows.append({"plan_id": f"plan_{s}_y", "name": f"{plans[si]} annual", "billing_interval": "year",
                              "unit_amount_cents": int(round(unit * 12 * (1 - p["annual_discount"]))), "tier": s, "currency": "USD", "active": True})
        E["plans"] = pd.DataFrame(plan_rows)
        E["cust_ids"] = cust_ids
        inv_out = allinv.copy()
        inv_out["customer_id"] = cust_ids[inv_out["_c"].to_numpy()]
        sub_ids = np.array([f"sub_{i:06d}" for i in range(1, N + 1)])
        inv_out["subscription_id"] = sub_ids[inv_out["_c"].to_numpy()]
        inv_out["due_date"] = inv_out["invoice_date"].dt.normalize()
        inv_out["amount_paid_cents"] = np.where(inv_out["status"] == "paid", inv_out["total_cents"], 0)
        E["invoices_out"] = inv_out
        E["subscriptions"] = sub
        E["customers"] = cust
        # usage in the first 14 days (window acquisitions only)
        newc = idx[E["a"][idx] >= 0]
        onboarding_hit = E["post"][newc] & (p["collapse_cause"] == "onboarding_regression")
        intensity = np.clip(np.log1p(T[newc]) / np.log(13), 0.2, 1.3) * np.where(onboarding_hit, 0.5, 1.0)
        rows = []
        for w in range(2):
            rows.append(pd.DataFrame({"_c": newc, "customer_id": cust_ids[newc], "subscription_id": sub_ids[newc], "metric": "active_days",
                                      "quantity": np.clip(np.round(7 * intensity * g.uniform(0.6, 1.0, len(newc))), 0, 7).astype(np.int64),
                                      "event_ts": signup.to_numpy()[newc] + np.timedelta64(7 * (w + 1), "D")}))
        use = pd.concat(rows, ignore_index=True)
        use = use[use["event_ts"] <= np.datetime64(end_ts)].sort_values(["event_ts", "customer_id"], kind="stable").reset_index(drop=True)
        use.insert(0, "event_id", [f"ue_{i:07d}" for i in range(1, len(use) + 1)])
        use["received_at"] = use["event_ts"] + pd.to_timedelta(g.integers(1, 600, len(use)), unit="s")
        E["usage"] = use
        # support tickets
        tk_rate = np.where(E["post"][idx] & (p["collapse_cause"] == "onboarding_regression"), 0.45, 0.18)
        n_tk = g.poisson(tk_rate)
        tc = np.repeat(idx, n_tk)
        cats = np.array(("onboarding", "billing", "bug", "how_to", "feature_request", "competitor_mention"))
        onb_p = np.where(E["post"][tc] & (p["collapse_cause"] == "onboarding_regression"), 0.55, 0.15)
        cat = np.where(g.random(len(tc)) < onb_p, 0, g.choice([1, 2, 3, 4, 5], size=len(tc), p=[0.25, 0.25, 0.3, 0.12, 0.08]))
        opened = pd.Series(signup.to_numpy()[tc]) + pd.to_timedelta(np.where(cat == 0, g.uniform(1, 25, len(tc)), g.uniform(1, 120, len(tc))), unit="D")
        tk = pd.DataFrame({"customer_id": cust_ids[tc], "opened_at": opened, "category": cats[cat],
                           "priority": np.array(("low", "normal", "high"))[g.choice(3, size=len(tc), p=[0.3, 0.55, 0.15])],
                           "resolution_hours": np.round(g.lognormal(math.log(14), 0.8, len(tc)), 1), "csat": g.integers(1, 6, len(tc))})
        tk = tk[(tk["opened_at"] >= start0) & (tk["opened_at"] <= end_ts)]
        tk = tk.sort_values(["opened_at", "customer_id"], kind="stable").reset_index(drop=True)
        tk["closed_at"] = tk["opened_at"] + pd.to_timedelta(tk["resolution_hours"], unit="h")
        tk["first_response_minutes"] = np.round(g.lognormal(math.log(90), 0.7, len(tk))).astype(np.int64)
        tk.insert(0, "ticket_id", [f"tk_{i:06d}" for i in range(1, len(tk) + 1)])
        E["tickets"] = tk
        # marketing spend by channel-month (window), CAC driven
        gm = p["gm"]
        arpa_cents = float(np.mean(E["mrr_base_cents"][idx]))
        cac_target = p["cac_payback_months"] * arpa_cents * gm
        win = idx[E["a"][idx] >= 0]
        ms_rows = []
        mkt_share = 1 - p["sales_payroll_share"]
        for m in range(n_m):
            sel = win[E["a"][win] == m]
            for c in range(len(CHANNELS)):
                n_new = int((E["channel"][sel] == c).sum())
                spend = cents(cac_target * mkt_share * CH_CAC[c] * max(n_new, 0.3) * g.lognormal(0, 0.08))
                ms_rows.append((f"{E['months'][m]:%Y-%m}", CHANNELS[c], spend, n_new))
        mk = pd.DataFrame(ms_rows, columns=["month", "channel", "spend_cents", "attributed_signups"])
        E["marketing"] = mk
        E["cac_target"] = cac_target
        # commissions (window acquisitions; legacy pool handled as one opening row)
        com_amt = np.round(p["commission_pct"] * 12 * E["mrr_base_cents"][win] * np.where(E["annual"][win], 1 - p["annual_discount"], 1.0)).astype(np.int64)
        com = pd.DataFrame({"customer_id": cust_ids[win], "_c": win, "amount_cents": com_amt, "capitalized_month": E["a"][win],
                            "capitalized_at": (signup.to_numpy()[win]).astype("datetime64[ns]"), "amortization_months": COMMISSION_LIFE,
                            "sales_rep_id": np.array([f"rep_{k:02d}" for k in range(1, 19)])[g.integers(0, 18, len(win))]})
        com = com.sort_values(["capitalized_at", "customer_id"], kind="stable").reset_index(drop=True)
        com.insert(0, "commission_id", [f"cm_{i:06d}" for i in range(1, len(com) + 1)])
        E["commissions"] = com
        E["svc_end"] = svc_end

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Derive the billing sub-ledgers, then post the books month by month."""
        self._derive(ctx, state)
        self._books(ctx, state)

    def _books(self, ctx: BuildContext, state: WorldState) -> None:
        p, cal = state.params, state.calendar
        E = state.extras
        L = self.fresh_ledger(state)
        state.ledger = L
        gran = GL_BATCH[p["difficulty"]]
        months = E["months"]
        n_m = len(months)
        start0 = pd.Timestamp(cal.start)
        end_d = cal.end
        R = p["revenue_ttm_cents"]
        inv = E["invoices"]
        valid = (inv["status"] != "void").to_numpy()
        alloc, amon = E["alloc"], E["alloc_month"]
        pre = (inv["invoice_date"] < start0).to_numpy() & valid
        deferred0 = int((alloc[pre] * (amon[pre] >= 0)).sum())
        # legacy commission pool: unamortized balance of the installed base, run off over 18 months
        legacy_c = E["keep"][E["a"][E["keep"]] < 0]
        pool = cents(p["commission_pct"] * 12 * float(np.sum(E["mrr_base_cents"][legacy_c])) * 0.45)
        E["legacy_commission_pool"] = pool
        # review P1 #9: thin opening cash; the cash pile comes from a preferred round closed in the warm-up quarter P0
        frng = ctx.rng.spawn("funding")
        cash0 = cents(R * float(frng.uniform(0.08, 0.12)))
        series = int(round(R * float(frng.uniform(0.35, 0.45)) / 50_000_000)) * 50_000_000  # to the 0.5M USD
        venture_debt = int(round(R * float(frng.uniform(0.08, 0.12)) / 50_000_000)) * 50_000_000
        debt_rate = float(frng.uniform(0.10, 0.13))
        series_label = "Series C" if R > 3_000_000_000 else "Series B"
        E["funding"] = {"series_label": series_label, "series_cents": series, "venture_debt_cents": venture_debt, "debt_rate": debt_rate}
        ppe = cents(R * 0.03)
        net = cash0 + ppe + pool - deferred0
        cs = cents(0.85 * net) if net > 0 else 1
        L.open_books(cal.opening_date, {"1000": cash0, "1500": ppe, "1350": pool, "2200": -deferred0, "3000": -cs, "3100": -(net - cs)})
        events: list[tuple[dt.date, int, Any]] = []

        def add(d: Any, prio: int, fn: Any) -> None:
            events.append((d.date() if isinstance(d, pd.Timestamp) else d, prio, fn))

        p0_start = pd.Timestamp(cal.opening_date).date()
        add(p0_start + dt.timedelta(days=40), 0, lambda d: L.equity_raise(d, series, memo=f"{series_label} preferred equity financing"))
        p5_start = cal.bounds(state.periods[4])[0]
        add(p5_start + dt.timedelta(days=9), 0, lambda d: L.borrow(d, venture_debt, debt_account="2610",
                                                                  memo="Venture debt drawn to fund the acquisition ramp"))
        # billing (deferred revenue) and collections
        bill = inv[valid & ~pre]
        bb = bill.assign(b=batch_end_dates(bill["invoice_date"], gran)).groupby("b", sort=True).agg(a=("total_cents", "sum"), n=("invoice_id", "size"))
        for b, r in bb.iterrows():
            add(b, 2, lambda d, a=int(r.a), n=int(r.n): L.entry(d, [("1100", a, 0, {"batch": f"billing:{d}", "rows": str(n)}), ("2200", 0, a, {})],
                                                                 "Subscription invoices issued (billed in advance)", "billing"))
        pdx = inv[valid & ~pre & inv["paid_at"].notna()]
        pb = pdx.assign(b=batch_end_dates(pdx["paid_at"], gran)).groupby("b", sort=True).agg(a=("total_cents", "sum"), n=("invoice_id", "size"))
        for b, r in pb.iterrows():
            add(b, 3, lambda d, a=int(r.a), n=int(r.n): L.collect_cash(d, a, memo="Card payments collected", dim={"rows": str(n)}))
        # monthly recognition
        rec_m = np.zeros(n_m, dtype=np.int64)
        vv = valid
        for j in range(alloc.shape[1]):
            mm = amon[vv, j]
            ok = (mm >= 0) & (mm < n_m)
            np.add.at(rec_m, mm[ok], alloc[vv, j][ok])
        E["recognized_by_month"] = rec_m
        # commissions: capitalize at signup (batched), amortize straight-line (company policy, no write-off on churn)
        com = E["commissions"]
        cb = com.assign(b=batch_end_dates(com["capitalized_at"], gran)).groupby("b", sort=True)["amount_cents"].sum()
        for b, a in cb.items():
            add(b, 2, lambda d, a=int(a): L.entry(d, [("1350", a, 0, {"batch": "commissions"}), ("2110", 0, a, {})], "Sales commissions capitalized (ASC 340-40)", "commission"))
        amort = self._commission_amortization(state)
        E["amort_by_month"] = amort
        # marketing and operating costs
        mk = E["marketing"].groupby("month", sort=False)["spend_cents"].sum()
        fee_rate = 0.029
        prev = {"pay": 0, "accr": 0}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            if m > 0:
                add(ms + dt.timedelta(days=4), 1, lambda d, a=prev["pay"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll and commissions paid", source="payroll"))
                add(ms + dt.timedelta(days=9), 1, lambda d, a=prev["accr"]: L.pay_ap(d, a, payable_account="2100", memo="Vendors paid (ads, hosting, opex)", source="opex"))
            if ms.month in (1, 4, 7, 10) and m > 0:
                add(ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"]))
            run = float(rec_m[max(0, m - 2): m + 1].mean()) if m > 0 else float(rec_m[0])
            add(me, 5, lambda d, a=int(rec_m[m]): L.recognize_deferred(d, a, memo="Subscription revenue recognized"))
            add(me, 5, lambda d, a=int(amort[m]): L.entry(d, [("6010", a, 0, {}), ("1350", 0, a, {})], "Amortization of capitalized commissions", "commission"))
            fees = cents(run * fee_rate)
            add(me, 6, lambda d, a=fees: L.entry(d, [("5050", a, 0, {}), ("1000", 0, a, {})], "Card processing fees", "fees"))
            spend = int(mk.get(f"{ms:%Y-%m}", 0))
            hosting = cents(run * max(0.05, 1 - p["gm"] - self.period_noise(ctx, state).gm(ms) - 0.06 - fee_rate))  # review C2: GM noise
            support = cents(run * 0.06)
            rd = cents(R / 12 * p["rd_pct"] * (1.004 ** m) * self.period_noise(ctx, state).ox(ms))
            ga = cents(R / 12 * p["ga_pct"] * (1.004 ** m) * self.period_noise(ctx, state).ox(ms))
            sales = cents(E["cac_target"] * p["sales_payroll_share"] * float(E["acq_expected"][LEGACY_MONTHS + m]))
            payroll = {"5010": support, "6000": sales, "6100": cents(rd * 0.85), "6200": cents(ga * 0.6)}
            accr = {"5000": hosting, "6000": spend, "6100": rd - cents(rd * 0.85), "6200": ga - cents(ga * 0.6), "6400": cents(R / 12 * 0.02)}
            add(me, 7, lambda d, s=payroll: L.payroll(d, s, memo="Payroll (support, sales, R&D, G&A)"))
            add(me, 7, lambda d, s=accr: L.accrue_opex(d, s, memo="Hosting, paid acquisition and operating expenses"))
            add(ms + dt.timedelta(days=14), 6, lambda d: L.capex(d, cents(R * 0.002), memo="Equipment"))
            add(me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 36)))
            add(me, 9, lambda d: L.accrue_interest(d, cents(-L.closing_balances()["2610"] * debt_rate / 12), memo="Venture debt interest"))
            if ms.month % 3 == 0:
                add(me, 10, lambda d: L.pay_interest(d, -L.closing_balances()["2150"], memo="Venture debt interest paid"))

                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))
                add(me, 11, tax)
            com_m = int(com.loc[com["capitalized_month"] == m, "amount_cents"].sum())
            prev = {"pay": sum(payroll.values()) + com_m, "accr": sum(accr.values())}
        events.sort(key=lambda e: (e[0], e[1]))
        floor = cents(R / 12 * 1.5)
        k = 0
        raised = 0
        for ms in months:
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            while k < len(events) and events[k][0] <= me:
                if events[k][0] <= end_d:
                    events[k][2](events[k][0])
                k += 1
            cash = L.closing_balances()["1000"]
            if cash < floor:
                amt = int(math.ceil((4 * floor - cash) / 10_000_000) * 10_000_000)
                L.equity_raise(me, amt, memo="Preferred equity financing round")
                raised += amt
        E["equity_raised"] = raised

    def _commission_amortization(self, state: WorldState) -> np.ndarray:
        """Monthly amortization (company policy: straight-line over the stated life, no write-off on churn)."""
        E = state.extras
        n_m = len(E["months"])
        com = E["commissions"]
        out = np.zeros(n_m, dtype=np.int64)
        life = COMMISSION_LIFE
        A = com["amount_cents"].to_numpy().astype(float)
        m0 = com["capitalized_month"].to_numpy()
        for j in range(life):
            sched = np.floor(A * (j + 1) / life + 0.5) - np.floor(A * j / life + 0.5)
            mm = m0 + j
            ok = mm < n_m
            np.add.at(out, mm[ok], sched[ok].astype(np.int64))
        pool = E.get("legacy_commission_pool", 0)
        for j, a in enumerate(split_cents(pool, [1] * 18) if pool else []):
            if j < n_m:
                out[j] += a
        return out

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Billing tables + 2/4/6 dirt rules."""
        p, E = state.params, state.extras
        inv = E["invoices_out"][["invoice_id", "customer_id", "subscription_id", "invoice_date", "due_date", "period_start", "period_end",
                                 "billing_interval", "subtotal_cents", "discount_cents", "total_cents", "amount_paid_cents", "status", "paid_at"]].copy()
        inv["currency"] = "USD"
        inv["created_at"] = inv["invoice_date"]
        pay = E["payments"].copy()
        pay["customer_id"] = E["cust_ids"][pay["_c"].to_numpy()]
        pay = pay.drop(columns=["_c"])
        pay["method"] = "card"
        com = E["commissions"].drop(columns=["_c", "capitalized_month"]).copy()
        pool = E["legacy_commission_pool"]
        if pool:
            com = pd.concat([pd.DataFrame([{"commission_id": "cm_legacy_pool", "customer_id": None, "amount_cents": pool,
                                            "capitalized_at": pd.Timestamp(state.calendar.opening_date), "amortization_months": 18,
                                            "sales_rep_id": None}]), com], ignore_index=True)
        dirt_plan = [
            ("customers", "nulls:billing_email:0.07"), ("support_tickets", "case_mixed:category"), ("customers", "whitespace:acquisition_channel"),
            ("subscriptions", "format_mixed:start_date"), ("invoices", "dup_keys:0.015"), ("usage_events", "tz_mixed:event_ts"),
        ]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[: {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]]:
            dirt.setdefault(t, []).append(rule)
        money = {c: "BIGINT" for c in ("subtotal_cents", "discount_cents", "total_cents", "amount_paid_cents", "amount_cents", "mrr_cents",
                                        "unit_amount_cents", "spend_cents")}
        builder.add_frame("plans", E["plans"], primary_key=["plan_id"], foreign_keys=[], column_types=money)
        builder.add_frame("customers", E["customers"], primary_key=["customer_id"], foreign_keys=[], dirt=dirt.get("customers", ()),
                          descriptions={"created_at": "signup timestamp (defines the acquisition cohort)", "acquisition_channel": "attributed acquisition channel"})
        builder.add_frame("subscriptions", E["subscriptions"], primary_key=["subscription_id"],
                          foreign_keys=[("customer_id", "customers", "customer_id"), ("plan_id", "plans", "plan_id")],
                          column_types={**money, "start_date": "DATE", "ended_at": "DATE", "seats": "INTEGER", "contract_months": "INTEGER"},
                          dirt=dirt.get("subscriptions", ()),
                          descriptions={"ended_at": "end of the last paid period (NULL = active)", "discount_pct": "promotional / negotiated discount (%)",
                                        "mrr_cents": "current (or final) monthly recurring revenue"})
        builder.add_frame("invoices", inv, primary_key=["invoice_id"],
                          foreign_keys=[("customer_id", "customers", "customer_id"), ("subscription_id", "subscriptions", "subscription_id")],
                          column_types={**money, "invoice_date": "TIMESTAMP", "due_date": "DATE", "period_start": "TIMESTAMP", "period_end": "TIMESTAMP"},
                          dirt=dirt.get("invoices", ()),
                          descriptions={"status": "paid | open | void (void = card failed three times, subscription canceled)",
                                        "ingest_id": ">1 marks a re-ingested duplicate of the same invoice_id"},
                          description="Subscription invoices, billed in advance on the anniversary date (AR / deferred revenue sub-ledger).")
        builder.add_frame("payments", pay, primary_key=["payment_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id")], column_types=money,
                          description="Card payment attempts that failed and their dunning retries (first-attempt successes live on invoices).")
        builder.add_frame("usage_events", E["usage"].drop(columns=["_c"]), primary_key=["event_id"],
                          foreign_keys=[("customer_id", "customers", "customer_id")], column_types={"quantity": "INTEGER"}, dirt=dirt.get("usage_events", ()),
                          descriptions={"metric": "active_days = days with product activity in the onboarding week", "event_ts": "end of the measured week"})
        builder.add_frame("support_tickets", E["tickets"], primary_key=["ticket_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types={"first_response_minutes": "INTEGER", "csat": "INTEGER"}, dirt=dirt.get("support_tickets", ()))
        builder.add_frame("marketing_spend", E["marketing"], primary_key=["month", "channel"], foreign_keys=[], column_types=money,
                          descriptions={"attributed_signups": "signups attributed to the channel in the month (last touch)"})
        builder.add_frame("commissions", com, primary_key=["commission_id"], foreign_keys=[("customer_id", "customers", "customer_id")],
                          column_types={**money, "amortization_months": "INTEGER"},
                          descriptions={"amortization_months": "straight-line amortization period (company policy)"},
                          description="Capitalized sales commissions (ASC 340-40 contract-cost asset sub-ledger).")

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, deferred revenue, revenue and commission-asset ties - all hold."""
        cal, E = state.calendar, state.extras
        inv = E["invoices"]
        valid = (inv["status"] != "void").to_numpy()
        alloc, amon = E["alloc"], E["alloc_month"]
        start0 = pd.Timestamp(cal.start)
        rec_m = E["recognized_by_month"]
        amort = E["amort_by_month"]
        com = E["commissions"]
        pool = E["legacy_commission_pool"]
        ar, dr, rev, ca = {}, {}, {}, {}
        for period in state.periods:
            _s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            me = (pd.Timestamp(e).year - start0.year) * 12 + pd.Timestamp(e).month - start0.month
            ms_ = me - 2
            issued = valid & (inv["invoice_date"] <= e_ts).to_numpy()
            open_ = (inv["paid_at"].isna() | (inv["paid_at"] > e_ts)).to_numpy()
            ar[period] = int(inv.loc[issued & open_ & (inv["invoice_date"] >= start0).to_numpy(), "total_cents"].sum())
            dr[period] = int((alloc[issued] * (amon[issued] > me)).sum())
            rev[period] = int(rec_m[ms_: me + 1].sum())
            ca[period] = int(com.loc[com["capitalized_month"] <= me, "amount_cents"].sum() + pool - amort[: me + 1].sum())
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Issued, unpaid, non-void invoices == GL 1100"),
            SubledgerTie("deferred_revenue_unrecognized", dr, ["2200", "2210"], description="Unrecognized portion of issued invoices == GL 2200"),
            SubledgerTie("revenue_recognized", rev, ["4000"], basis="activity", description="Daily-allocated service revenue in period == GL 4000 activity"),
            SubledgerTie("commission_asset", ca, ["1350"], description="Capitalized commissions less straight-line amortization == GL 1350"),
        ]

    # ------------------------------------------------------------------ facts

    def _active_at(self, state: WorldState, month: int) -> np.ndarray:
        """Customers whose service covers the first day of window month ``month`` (by billing periods)."""
        E = state.extras
        idx = E["keep"]
        t0 = np.datetime64(pd.Timestamp(E["m_start"][LEGACY_MONTHS + month]))
        su = E["signup"].to_numpy()[idx]
        return idx[(su <= t0) & (E["svc_end"][idx] > t0)]

    def _mrr_at(self, state: WorldState, month: int, cust: np.ndarray) -> np.ndarray:
        """Monthly-equivalent recurring revenue (cents) at the start of window month ``month``."""
        p, E = state.params, state.extras
        t0 = pd.Timestamp(E["m_start"][LEGACY_MONTHS + month])
        su = pd.to_datetime(E["signup"].to_numpy()[cust])
        age = np.maximum(0, (t0.year - su.year) * 12 + (t0.month - su.month) - (su.day > t0.day).astype(int))
        k = np.where(E["annual"][cust], (age // 12) * 12, age)
        disc = np.where(E["disc_promo"][cust], np.where(k < 3, E["discount"][cust], 0.0), E["discount"][cust]) / 100
        return E["mrr_base_cents"][cust] * (1 + p["expansion_monthly"]) ** k * np.where(E["annual"][cust], 1 - p["annual_discount"], 1.0) * (1 - disc)

    def _survivors(self, state: WorldState, months: range, horizon: int, mask: np.ndarray | None = None) -> np.ndarray:
        """Pooled survivor counts ``n_0..n_horizon`` of cohorts acquired in window ``months`` (all fully observed)."""
        E = state.extras
        idx = E["keep"]
        sel = idx[np.isin(E["a"][idx], list(months))]
        if mask is not None:
            sel = sel[mask[sel]]
        T = E["T"][sel]
        return np.array([(T > k).sum() for k in range(horizon + 1)], dtype=float)

    def _hazard_mle(self, state: WorldState, m_lo: int, m_hi: int, *, age_min: int = 2, age_max: int = 11,
                    mask: np.ndarray | None = None) -> float:
        """Constant-hazard MLE (events / exposure, right-censored at the as-of date) for monthly-plan customers acquired in
        window months ``m_lo..m_hi``, over billing periods ``age_min <= k <= age_max`` (first-period churn excluded; the
        same age window for every group so sBG duration dependence does not bias comparisons)."""
        E = state.extras
        n_m = len(E["months"])
        idx = E["keep"]
        sel = idx[(E["a"][idx] >= m_lo) & (E["a"][idx] <= m_hi) & ~E["annual"][idx]]
        if mask is not None:
            sel = sel[mask[sel]]
        T = E["T"][sel]
        obs = np.minimum(n_m - 1 - E["a"][sel], age_max)  # last observable period index
        at_risk = T >= age_min
        last = np.minimum(T, obs)
        exposure = np.where(at_risk, np.maximum(0, last - age_min + 1), 0).sum()
        events = (at_risk & (T <= obs)).sum()
        return float(events / exposure) if exposure else 0.0

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        p, cal, E = state.params, state.calendar, state.extras
        P = state.periods
        n_m = len(E["months"])
        cs = p["collapse_start_month"]
        idx = E["keep"]
        a, T = E["a"], E["T"]

        def cohort_ret(m_lo: int, m_hi: int, k: int, mask: np.ndarray | None = None) -> float:
            vals = []
            for m in range(m_lo, m_hi + 1):
                if m + k > n_m - 1:
                    continue
                sel = idx[a[idx] == m]
                if mask is not None:
                    sel = sel[mask[sel]]
                if len(sel):
                    vals.append(float((T[sel] > k).mean()))
            return float(np.mean(vals)) if vals else 0.0

        f: dict[str, Any] = {}
        f["ret_m3_pre"] = cohort_ret(3, cs - 1, 3)
        f["ret_m3_post"] = cohort_ret(cs, n_m - 1, 3)
        f["ret_m6_post"] = cohort_ret(cs, n_m - 1, 6)

        n_pre = self._survivors(state, range(3, cs), 12)
        n_post = self._survivors(state, range(cs, cs + 6), 6)
        f["n_pre"], f["n_post"] = n_pre, n_post
        f["hazard_pre"] = self._hazard_mle(state, 3, cs - 1)
        f["hazard_post"] = self._hazard_mle(state, cs, n_m - 1)
        f["sbg_pre"] = sbg_fit(n_pre)
        f["sbg_post"] = sbg_fit(n_post)
        # blended (lost MRR / opening MRR) monthly churn and the board's quarterly logo-churn KPI
        blended, headline = {}, {}
        for qi, per in enumerate(P):
            m0 = 3 + 3 * qi
            rates = []
            for m in range(m0, m0 + 3):
                act = self._active_at(state, m)
                nxt = set(self._active_at(state, m + 1).tolist()) if m + 1 < n_m else None
                mrr = self._mrr_at(state, m, act)
                if nxt is None:
                    end = np.datetime64(pd.Timestamp(cal.end) + pd.Timedelta(days=1))
                    lost = E["svc_end"][act] <= end
                else:
                    lost = ~np.isin(act, list(nxt))
                rates.append(float(mrr[lost].sum() / mrr.sum()) if mrr.sum() else 0.0)
            blended[per] = float(np.mean(rates))
            # the board KPI: churned established logos (90+ days tenure at quarter start) / logos at quarter END (swelled by new logos)
            open_q = self._active_at(state, m0)
            open_q = open_q[a[open_q] <= m0 - 3]
            end_q = np.datetime64(pd.Timestamp(cal.bounds(per)[1]) + pd.Timedelta(days=1))
            n_end = len(self._active_at(state, m0 + 3)) if m0 + 3 < n_m else int((E["svc_end"][idx] > end_q).sum() - (E["signup"].to_numpy()[idx] >= end_q).sum())
            headline[per] = float((E["svc_end"][open_q] <= end_q).sum() / max(1, n_end) / 3)
        f["blended"], f["headline"] = blended, headline
        # NRR (12 months): MRR at P8 end of customers active at P4 end / their MRR then
        m_p4_end = 3 + 12
        base = self._active_at(state, m_p4_end)
        endm = n_m - 1
        mrr_then = self._mrr_at(state, m_p4_end, base)
        still = E["svc_end"][base] > np.datetime64(pd.Timestamp(cal.end) + pd.Timedelta(days=1))
        mrr_now = self._mrr_at(state, endm, base) * still
        f["nrr_p8"] = float(mrr_now.sum() / mrr_then.sum())
        # unit economics by cohort group
        mk = E["marketing"]
        gm = p["gm"]
        mo = [f"{m:%Y-%m}" for m in E["months"]]
        sales_m = np.array([E["cac_target"] * p["sales_payroll_share"] * float(E["acq_expected"][LEGACY_MONTHS + m]) for m in range(n_m)])

        def group(m_lo: int, m_hi: int) -> dict[str, float]:
            sel = idx[(a[idx] >= m_lo) & (a[idx] <= m_hi)]
            spend = float(mk[mk["month"].isin(mo[m_lo: m_hi + 1])]["spend_cents"].sum()) + float(sales_m[m_lo: m_hi + 1].sum())
            arpa = float(np.mean(E["mrr_base_cents"][sel] * np.where(E["annual"][sel], 1 - p["annual_discount"], 1.0)))
            return {"n": float(len(sel)), "spend": spend, "cac": spend / max(1, len(sel)), "arpa": arpa}

        gpre, gpost = group(3, cs - 1), group(cs, n_m - 1)
        f["gpre"], f["gpost"] = gpre, gpost
        f["ltv_cac_pre"] = (gpre["arpa"] * gm / max(1e-4, f["hazard_pre"])) / gpre["cac"]
        f["ltv_cac_post"] = (gpost["arpa"] * gm / max(1e-4, f["hazard_post"])) / gpost["cac"]
        f["payback_post"] = gpost["cac"] / (gpost["arpa"] * gm)
        # cause marker
        post_idx = idx[a[idx] >= cs]
        pre_idx = idx[(a[idx] >= 3) & (a[idx] < cs)]
        cause = p["collapse_cause"]
        use = E["usage"].groupby("_c")["quantity"].sum()
        if cause == "channel_shift":
            f["marker"] = float((E["channel"][post_idx] == 4).mean())
            f["marker_desc"] = "share of P5+ new customers acquired via paid_social"
        elif cause == "onboarding_regression":
            f["marker"] = float(use.reindex(post_idx).mean() / use.reindex(pre_idx).mean())
            f["marker_desc"] = "average first-14-day active days of P5+ cohorts / P1-P4 cohorts"
        else:
            f["marker"] = float((E["discount"][post_idx] >= 30).mean())
            f["marker_desc"] = "share of P5+ new customers with discount_pct >= 30"
        # marker vs control steady hazard ratio (recovers collapse_multiplier)
        col = E["collapsed"]
        k0 = 4 if cause == "discount_cohorts" else 2
        if cause == "onboarding_regression":
            f["hazard_marker"] = self._hazard_mle(state, cs, n_m - 1, age_min=k0)
            f["hazard_control"] = self._hazard_mle(state, 3, cs - 1, age_min=k0)
        else:
            f["hazard_marker"] = self._hazard_mle(state, cs, n_m - 1, age_min=k0, mask=col)
            f["hazard_control"] = self._hazard_mle(state, cs, n_m - 1, age_min=k0, mask=~col)
        # 12-month MRR projection of the current base with no new acquisition (sBG by group)
        act = self._active_at(state, endm)
        mrr = self._mrr_at(state, endm, act)
        t_end = pd.Timestamp(cal.end) + pd.Timedelta(days=1)
        su = pd.to_datetime(E["signup"].to_numpy()[act])
        age = np.maximum(0, (t_end.year - su.year) * 12 + (t_end.month - su.month))
        proj = 0.0
        for grp, fit in (((a[act] >= cs), f["sbg_post"]), ((a[act] < cs), f["sbg_pre"])):
            S = _sbg_curve(fit, 200)
            ag = np.minimum(age[grp], 180)
            proj += float((mrr[grp] * S[ag + 12] / S[ag]).sum())
        f["mrr_now"] = float(mrr.sum())
        f["mrr_proj_12m"] = proj
        # over-capitalized commissions on post-P5 customers
        com = E["commissions"]
        life_post = int(min(COMMISSION_LIFE, max(3, round(1 / max(1e-3, f["hazard_post"])))))
        cpost = com[com["capitalized_month"] >= cs]
        m_age = (n_m - 1) - cpost["capitalized_month"].to_numpy() + 1
        A = cpost["amount_cents"].to_numpy().astype(float)
        actual = A - np.floor(A * np.minimum(m_age, COMMISSION_LIFE) / COMMISSION_LIFE + 0.5)
        ci = cpost["_c"].to_numpy()
        alive = E["svc_end"][ci] > np.datetime64(t_end)
        correct = np.where(alive, A - np.floor(A * np.minimum(m_age, life_post) / life_post + 0.5), 0.0)
        f["overcap"] = float((actual - correct).sum())
        f["life_post"] = life_post
        f["annual_mix_pre"] = float(E["annual"][pre_idx].mean())
        f["annual_mix_post"] = float(E["annual"][post_idx].mean())
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Cohorts (P1-P8 acquisition quarters), support desk queueing, unit economics, pricing, KPIs."""
        p, cal, E = state.params, state.calendar, state.extras
        f = self._facts(state)
        P = state.periods
        n_m = len(E["months"])
        idx, a, T = E["keep"], E["a"], E["T"]
        cohorts: list[Cohort] = []
        for qi, per in enumerate(P):
            m0 = 3 + 3 * qi
            sel = idx[(a[idx] >= m0) & (a[idx] < m0 + 3)]
            if not len(sel):
                continue
            horizon = max(0, min(12, n_m - 1 - (m0 + 2)))
            ret = [float((T[sel] > k).mean()) for k in range(horizon + 1)]
            arpa = float(np.mean(E["mrr_base_cents"][sel]))
            cohorts.append(cohort_from_curve(per, len(sel), ret, arpa, p["expansion_monthly"]))
        tk = E["tickets"]
        s8, e8 = cal.bounds(P[-1])
        t8 = tk[(tk["opened_at"] >= pd.Timestamp(s8)) & (tk["opened_at"] <= pd.Timestamp(e8) + pd.Timedelta(days=1))]
        hours = cal.days(P[-1]) * 24
        lam = len(t8) / hours
        W = float(t8["resolution_hours"].mean()) if len(t8) else 1.0
        mu = lam + 1 / W
        q = mm1(lam, mu)
        queueing = {"lambda": lam, "mu": mu, "rho": lam / mu, "L": lam * W, "W": W, "Lq": lam * W - lam / mu, "Wq": W - 1 / mu,
                    "desk": 1.0, "mm1_W_check": q["W"]}
        stations = [Station(name="support_desk", capacity_per_hour=mu, utilization=lam / mu, wip=lam * W, cycle_time=W)]
        ue = unit_economics(sm_spend_cents=f["gpost"]["spend"], new_customers=f["gpost"]["n"], arpa_monthly_cents=f["gpost"]["arpa"],
                            gross_margin=p["gm"], churn_monthly=max(1e-4, f["hazard_post"]))
        ue_pre = unit_economics(sm_spend_cents=f["gpre"]["spend"], new_customers=f["gpre"]["n"], arpa_monthly_cents=f["gpre"]["arpa"],
                                gross_margin=p["gm"], churn_monthly=max(1e-4, f["hazard_pre"]))
        unit = {**ue, "ltv_to_cac_pre": ue_pre["ltv_to_cac"], "cac_pre": ue_pre["cac"]}
        plans = E["plans"]
        pricing = {"price_ladder_cents": {r.plan_id: int(r.unit_amount_cents) for r in plans.itertuples()},
                   "annual_prepay_discount": p["annual_discount"], "elasticity_by_segment": {s: -0.8 for s in p["segments"]},
                   "discount_leakage": {"share_discounted_pre": float((E["discount"][idx[(a[idx] >= 3) & (a[idx] < 15)]] > 0).mean()),
                                        "share_discounted_post": float((E["discount"][idx[a[idx] >= 15]] > 0).mean())}}
        supply = {"lead_time_dist": {"onboarding_days_p50": 7, "onboarding_days_p90": 21}, "billing": {"dunning_retry_days": [3, 8], "max_attempts": 3},
                  "acquisition_channels": list(CHANNELS)}
        kpis: dict[str, dict[str, float]] = {}
        inc = {i.period: i for i in state.statements.income}  # type: ignore[union-attr]
        cf = {c.period: c for c in state.statements.cash_flow}  # type: ignore[union-attr]
        for qi, per in enumerate(P):
            m_end = 3 + 3 * qi + 3
            act = self._active_at(state, min(m_end, n_m - 1))
            new = idx[(a[idx] >= m_end - 3) & (a[idx] < m_end)]
            sm = float(sum(state.ledger.activity(per)[c] for c in ("6000", "6010")))
            kpis[per] = {"mrr_cents": float(self._mrr_at(state, min(m_end, n_m - 1), act).sum()), "logos": float(len(act)), "new_logos": float(len(new)),
                         "headline_logo_churn_monthly": f["headline"][per], "blended_mrr_churn_monthly": f["blended"][per],
                         "sm_expense_cents": sm, "cac_cents": sm / max(1, len(new)), "revenue_cents": float(inc[per].revenue_cents),
                         "deferred_revenue_cents": float(-state.ledger.trial_balance(per)["2200"]), "operating_cash_flow_cents": float(cf[per].operating_cents),
                         "commission_asset_cents": float(state.ledger.trial_balance(per)["1350"])}
        E["ops_kpis"] = kpis
        return build_operations_state(stations=stations, queueing=queueing, unit_economics=unit, cohorts=cohorts, supply_chain=supply,
                                      pricing=pricing, kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 6 key table (sBG fits in scipy here only), reference SQL on the dirty DuckDB."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        P = state.periods
        P4, P5, P8 = P[3], P[4], P[-1]
        cause = p["collapse_cause"]
        metrics = [
            metric("retention_m3_pre_cohorts", f["ret_m3_pre"], "ratio",
                   f"Mean over monthly cohorts acquired {P[0]}-{P4} of the share still subscribed after 3 billing periods (subscriptions.ended_at).",
                   pillar="operations_strategy", tol_abs=0.03, label="M3 retention, cohorts before P5", hint="Cohort = signup month; average across cohorts."),
            metric("retention_m3_post_cohorts", f["ret_m3_post"], "ratio", f"Same for cohorts acquired {P5}-{P8} that are at least 3 months old.",
                   pillar="operations_strategy", tol_abs=0.03, label="M3 retention, cohorts from P5", hint="Only cohorts old enough to be observed."),
            metric("retention_m6_post_cohorts", f["ret_m6_post"], "ratio", f"Mean M6 retention of cohorts acquired from {P5} that are at least 6 months old.",
                   pillar="operations_strategy", tol_abs=0.03, label="M6 retention, cohorts from P5", hint="As M3, six periods."),
            metric("blended_churn_p8_monthly", f["blended"][P8], "ratio", f"Average over the three months of {P8} of lost MRR / opening MRR (monthly-equivalent MRR).",
                   pillar="operations_strategy", tol_abs=0.003, label="Blended MRR churn, latest quarter", hint="The aggregate number on the board slide."),
            metric("cohort_churn_post_monthly", f["hazard_post"], "ratio",
                   f"Steady monthly churn of monthly-plan customers acquired from {P5}: churn events / customer-months at risk in billing periods 2-11 "
                   "(right-censored at the as-of date; first-period churn excluded).",
                   pillar="operations_strategy", tol_abs=0.005, label="Steady churn of post-P5 cohorts", hint="Fit after the first-period churn."),
            metric("ltv_cac_pre", f["ltv_cac_pre"], "x", "(ARPA x gross margin / steady churn) / CAC for cohorts acquired P1-P4; CAC = marketing spend + sales payroll / new logos.",
                   pillar="operations_strategy", tol_abs=0.3, label="LTV/CAC, cohorts before P5", hint="Use cohort churn, not blended churn."),
            metric("ltv_cac_post", f["ltv_cac_post"], "x", "Same for cohorts acquired P5-P8 (steady churn from the post-P5 fit).",
                   pillar="operations_strategy", tol_abs=0.3, label="LTV/CAC, cohorts from P5", hint="Same formula, new cohorts."),
            metric("cac_payback_post_months", f["payback_post"], "months", "CAC / (ARPA x gross margin) for cohorts acquired P5-P8.",
                   pillar="operations_strategy", tol_abs=2.0, label="CAC payback, cohorts from P5", hint="Months of gross profit to recover CAC."),
            metric("collapse_cause_code", CAUSE_CODE[cause], "code", "1 = channel_shift (paid_social), 2 = onboarding_regression, 3 = discount_cohorts.",
                   pillar="operations_strategy", tol_abs=0.0, label="Cause of the collapse (1 channel / 2 onboarding / 3 discount)",
                   hint="Segment the new cohorts by channel, onboarding usage and discount."),
            metric("cause_marker_share", f["marker"], "ratio", f"{f['marker_desc']}.", pillar="sql", tol_abs=0.05, label="Cause marker",
                   hint="Quantify the marker that separates collapsing customers."),
            metric("mrr_projected_12m_cents", f["mrr_proj_12m"], "cents",
                   f"MRR of the {P8}-end base in 12 months with no new acquisition: sum of MRR x S(age+12)/S(age) with shifted-beta-geometric fits "
                   f"(pre: a={f['sbg_pre']['alpha']:.2f}, b={f['sbg_pre']['beta']:.2f}; post: a={f['sbg_post']['alpha']:.2f}, b={f['sbg_post']['beta']:.2f}).",
                   pillar="python", tol_rel=0.10, label="12-month MRR runoff projection", hint="Fit sBG (Fader & Hardie) by cohort group; no expansion."),
            metric("overcapitalized_commissions_cents", f["overcap"], "cents",
                   f"1350 balance on post-P5 customers (36-month straight line, no write-off) minus the balance if amortized over the expected life "
                   f"(1/steady churn = {f['life_post']} months) with write-off at churn.", pillar="accounting_finance", tol_rel=0.15,
                   label="Over-capitalized commissions", hint="ASC 340-40: the amortization period must reflect expected customer life."),
            metric("nrr_p8", f["nrr_p8"], "ratio", f"MRR at {P8} end of customers active at {P4} end / their MRR at {P4} end.",
                   pillar="operations_strategy", tol_abs=0.03, label="Net revenue retention (12 months)", hint="Annual NRR."),
        ]
        dedupe = "SELECT * FROM invoices QUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY ingest_id) = 1" if p["difficulty"] == "principal" \
            else "SELECT * FROM invoices"
        start_expr = ("COALESCE(TRY_CAST(start_date AS DATE), CAST(TRY_STRPTIME(CAST(start_date AS VARCHAR), '%m/%d/%Y') AS DATE))"
                      if p["difficulty"] != "foundation" else "CAST(start_date AS DATE)")
        as_of = cal.end
        sql_matrix = f"""
WITH s AS (
  SELECT customer_id, {start_expr} AS start_d, ended_at
  FROM subscriptions),
c AS (SELECT customer_id, start_d, date_trunc('month', start_d) AS cohort, ended_at FROM s WHERE start_d >= DATE '{cal.bounds(P[0])[0]}'),
ages AS (SELECT range AS k FROM range(0, 13))
SELECT c.cohort, ages.k,
       COUNT(*) FILTER (WHERE c.ended_at IS NULL OR c.ended_at > c.start_d + to_months(CAST(ages.k AS INTEGER))) * 1.0 / COUNT(*) AS retention,
       COUNT(*) AS cohort_size
FROM c CROSS JOIN ages
WHERE c.start_d + to_months(CAST(ages.k AS INTEGER)) <= DATE '{as_of}'
GROUP BY 1, 2 ORDER BY 1, 2"""

        def matrix_checks(df: pd.DataFrame) -> dict[str, Any]:
            d = df[df["k"] == 3].copy()
            d["cohort"] = pd.to_datetime(d["cohort"])
            cut = pd.Timestamp(cal.bounds(P5)[0])
            return {"m3_pre_mean": float(d.loc[d["cohort"] < cut, "retention"].mean()), "m3_post_mean": float(d.loc[d["cohort"] >= cut, "retention"].mean())}

        q_matrix = run_reference_query(con, "cohort_retention_matrix", sql_matrix, signature=["with", "group by", "filter(", "count("],
                                       metric_keys=["retention_m3_pre_cohorts", "retention_m3_post_cohorts", "retention_m6_post_cohorts"],
                                       checks=matrix_checks, max_rows=40)
        sql_marker = f"""
WITH c AS (
  SELECT TRIM(c.acquisition_channel) AS channel, s.discount_pct, s.ended_at, s.customer_id,
         CAST(c.created_at AS DATE) AS signup, CASE WHEN c.created_at >= TIMESTAMP '{cal.bounds(P5)[0]} 00:00:00' THEN 'post' ELSE 'pre' END AS grp
  FROM customers c JOIN subscriptions s USING (customer_id)
  WHERE c.created_at >= TIMESTAMP '{cal.bounds(P[0])[0]} 00:00:00'),
u AS (SELECT customer_id, SUM(quantity) AS onboarding_active_days FROM usage_events GROUP BY 1)
SELECT grp, channel, CASE WHEN discount_pct >= 30 THEN 'promo' ELSE 'standard' END AS pricing, COUNT(*) AS customers,
       AVG(u.onboarding_active_days) AS avg_onboarding_days,
       AVG(CASE WHEN ended_at IS NOT NULL AND ended_at <= signup + INTERVAL 3 MONTH THEN 1.0 ELSE 0.0 END) AS churned_within_3m
FROM c LEFT JOIN u USING (customer_id)
WHERE signup + INTERVAL 3 MONTH <= DATE '{as_of}'
GROUP BY 1, 2, 3 ORDER BY 1, churned_within_3m DESC, 2, 3"""
        q_marker = run_reference_query(con, "churn_by_cause_marker", sql_marker, signature=["with", "left join", "group by", "case when", "avg("],
                                       metric_keys=["collapse_cause_code", "cause_marker_share"],
                                       checks=lambda df: {"worst_post_segment": f"{df[df['grp'] == 'post'].iloc[0]['channel']}/{df[df['grp'] == 'post'].iloc[0]['pricing']}"
                                                          if (df["grp"] == "post").any() else ""})
        sql_blended = f"""
WITH inv AS ({dedupe}),
act AS (
  SELECT i.customer_id, date_trunc('month', i.period_start) AS month,
         SUM(CASE WHEN i.billing_interval = 'year' THEN i.total_cents / 12.0 ELSE i.total_cents END) AS mrr
  FROM inv i WHERE i.status <> 'void' GROUP BY 1, 2),
lagged AS (
  SELECT customer_id, month, mrr, LEAD(month) OVER (PARTITION BY customer_id ORDER BY month) AS next_month FROM act)
SELECT period_of(month) AS period, SUM(mrr) AS billed_mrr,
       SUM(CASE WHEN next_month IS NULL OR next_month > month + INTERVAL 12 MONTH THEN mrr ELSE 0 END) AS mrr_not_renewed
FROM lagged GROUP BY 1 ORDER BY 1"""
        q_blended = run_reference_query(con, "mrr_billed_and_not_renewed_by_quarter", sql_blended, signature=["with", "over(", "partition by", "group by", "case when"],
                                        metric_keys=["blended_churn_p8_monthly", "nrr_p8"],
                                        checks=lambda df: {"quarters": len(df)})
        sql_comm = """
SELECT period_of(capitalized_at) AS period, COUNT(*) AS commissions, SUM(amount_cents) AS capitalized_cents,
       SUM(SUM(amount_cents)) OVER (ORDER BY period_of(capitalized_at)) AS cumulative_capitalized_cents
FROM commissions WHERE customer_id IS NOT NULL GROUP BY 1 ORDER BY 1"""
        q_comm = run_reference_query(con, "commissions_capitalized_by_quarter", sql_comm, signature=["over(", "group by", "sum("],
                                     metric_keys=["overcapitalized_commissions_cents"],
                                     checks=lambda df: {"capitalized_last": float(df.iloc[-1]["capitalized_cents"])})
        sql_spend = """
SELECT period_of(CAST(month || '-01' AS DATE)) AS period, SUM(spend_cents) AS marketing_cents, SUM(attributed_signups) AS signups,
       SUM(spend_cents) * 1.0 / NULLIF(SUM(attributed_signups), 0) AS marketing_cac_cents
FROM marketing_spend GROUP BY 1 ORDER BY 1"""
        q_spend = run_reference_query(con, "marketing_spend_and_signups", sql_spend, signature=["group by", "sum("],
                                      metric_keys=["ltv_cac_post", "cac_payback_post_months"],
                                      checks=lambda df: {"signups_last_vs_first": float(df.iloc[-1]["signups"] / max(1, df.iloc[1]["signups"]))})
        sql_annual = f"""
SELECT period_of(CAST(c.created_at AS DATE)) AS cohort_quarter, COUNT(*) AS new_customers,
       AVG(CASE WHEN s.contract_months = 12 THEN 1.0 ELSE 0.0 END) AS annual_share
FROM customers c JOIN subscriptions s USING (customer_id)
WHERE c.created_at >= TIMESTAMP '{cal.bounds(P[0])[0]} 00:00:00'
GROUP BY 1 ORDER BY 1"""
        q_annual = run_reference_query(con, "annual_plan_share_by_cohort_quarter", sql_annual, signature=["join", "group by", "case when", "avg("],
                                       metric_keys=["mrr_projected_12m_cents"],
                                       checks=lambda df: {"annual_share_first": float(df.iloc[0]["annual_share"]), "annual_share_last": float(df.iloc[-1]["annual_share"])})
        sql_dr = """
SELECT j.period, SUM(CASE WHEN l.account_code = '2200' THEN l.credit_cents - l.debit_cents ELSE 0 END) AS deferred_revenue_change_cents,
       SUM(CASE WHEN l.account_code = '1350' THEN l.debit_cents - l.credit_cents ELSE 0 END) AS commission_asset_change_cents
FROM gl_lines l JOIN gl_journal j USING (entry_id) GROUP BY 1 ORDER BY 1"""
        q_dr = run_reference_query(con, "deferred_revenue_and_commission_asset_change", sql_dr, signature=["join", "group by", "case when", "sum("],
                                   metric_keys=["overcapitalized_commissions_cents"],
                                   checks=lambda df: {"commission_asset_growth_last": float(df.iloc[-1]["commission_asset_change_cents"])})
        an = {a.id: a for a in state.anomalies}
        an["A1"] = an["A1"].model_copy(update={
            "reference_sql": [q_matrix, q_marker],
            "magnitude": {**an["A1"].magnitude, "retention_m3_pre": round(f["ret_m3_pre"], 6), "retention_m3_post": round(f["ret_m3_post"], 6),
                          "hazard_marker": round(f["hazard_marker"], 6), "hazard_control": round(f["hazard_control"], 6),
                          "cause_marker_share": round(f["marker"], 6),
                          "sbg_post_alpha": round(f["sbg_post"]["alpha"], 6), "sbg_post_beta": round(f["sbg_post"]["beta"], 6),
                          **{f"post_survival_m{k}": round(float(f["n_post"][k] / f["n_post"][0]), 6) for k in range(1, 7)}},
            "evidence_trail": [f"M3 retention {f['ret_m3_pre']:.0%} (P1-P4 cohorts) vs {f['ret_m3_post']:.0%} (P5+ cohorts).",
                               f"Marker: {f['marker_desc']} = {f['marker']:.2f}."]})
        an["A2"] = an["A2"].model_copy(update={
            "reference_sql": [q_blended, q_comm, q_spend],
            "magnitude": {**an["A2"].magnitude, "blended_churn_p4": round(f["blended"][P4], 6), "blended_churn_p8": round(f["blended"][P8], 6),
                          "headline_churn_p4": round(f["headline"][P4], 6), "headline_churn_p8": round(f["headline"][P8], 6),
                          "overcapitalized_commissions_cents": round(f["overcap"], 2)},
            "evidence_trail": [f"Board KPI (quarterly logo churn) {f['headline'][P4]:.2%} -> {f['headline'][P8]:.2%} per month.",
                               f"Commission asset grows while post-P5 expected life is ~{f['life_post']} months, not 36."]})
        an["A3"] = an["A3"].model_copy(update={
            "reference_sql": [q_annual, q_dr],
            "magnitude": {**an["A3"].magnitude, "annual_mix_pre_realized": round(f["annual_mix_pre"], 6), "annual_mix_post_realized": round(f["annual_mix_post"], 6)},
            "evidence_trail": [f"Annual-prepay share {f['annual_mix_pre']:.0%} -> {f['annual_mix_post']:.0%} of new customers.",
                               "Deferred revenue grows more slowly than revenue; CFO lags."]})
        herrings_all = [
            f"Churn is flat at ~{f['headline'][P8]:.1%} a month - that is the quarterly logo-churn KPI on a swelling installed base; cohort M3 retention fell "
            f"from {f['ret_m3_pre']:.0%} to {f['ret_m3_post']:.0%}.",
            "A competitor launched and is taking our customers - competitor mentions are spread evenly across cohorts; churn is concentrated by the cause marker.",
            "Payment failures rose after the processor migration - true but minor: involuntary churn adds only a few tenths of a point.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        cause_txt = {"channel_shift": "customers acquired through the new paid_social channel",
                     "onboarding_regression": "every cohort since the onboarding redesign (first-14-day activity halved)",
                     "discount_cohorts": "customers acquired on the 30-40% three-month promo, who leave when it ends"}[cause]
        root = (f"Cohorts acquired from {P5} retain far worse than older ones (M3 {f['ret_m3_pre']:.0%} -> {f['ret_m3_post']:.0%}; steady churn "
                f"{f['hazard_pre']:.1%} -> {f['hazard_post']:.1%} a month), driven by {cause_txt}. The board's churn KPI stays near "
                f"{f['headline'][P8]:.1%} because the installed base swells the denominator and new customers lost within their first quarter never enter it; "
                f"spend-driven logo growth hides the leak (LTV/CAC {f['ltv_cac_pre']:.1f}x -> {f['ltv_cac_post']:.1f}x). Commissions on the new cohorts are "
                f"amortized over 36 months although their expected life is ~{f['life_post']} months (over-capitalized by "
                f"{f['overcap'] / 100:,.0f} USD), and the shift away from annual prepay slows deferred revenue and cash. With no new acquisition, "
                f"MRR would run off from {f['mrr_now'] / 100:,.0f} to ~{f['mrr_proj_12m'] / 100:,.0f} USD in 12 months.")
        py = ("import pandas as pd\n"
              "from axiom.engine.operations import sbg_fit, sbg_survival\n"
              "s = pd.read_sql('SELECT customer_id, start_date, ended_at FROM subscriptions', con)\n"
              "# survivor counts n_0..n_6 for cohorts acquired in the first six months of P5; fit sBG; project MRR runoff\n")
        return GroundTruth(
            root_cause=root, anomalies=[an[k] for k in ("A1", "A2", "A3")], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q.sql for q in (q_matrix, q_marker, q_blended, q_comm, q_spend, q_annual, q_dr)], python=[py],
                memo_bullets=[
                    "Blended churn on a growing base is a lagging, diluted metric - manage cohorts.",
                    f"New cohorts churn {f['hazard_post'] / max(1e-4, f['hazard_pre']):.1f}x faster; the cause marker is {f['marker_desc']}.",
                    f"LTV/CAC on new cohorts is {f['ltv_cac_post']:.1f}x with a {f['payback_post']:.0f}-month payback: growth spend is destroying value.",
                    "Fix the acquisition/onboarding cause before cutting spend across the board; model MRR runoff with sBG.",
                    "ASC 340-40: shorten the commission amortization period for the new cohorts and test the asset for impairment.",
                    "Watch annual-prepay mix: deferred revenue and CFO are leading indicators.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal/conceal, four stakeholders, timeline with the seeded cause and red herrings."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        names = rng.pick_names(4)
        cause = p["collapse_cause"]
        reveal = [
            f"The board deck reports churn 'stable at ~{f['headline'][P[-1]]:.1%}' a month.",
            f"New-logo growth is at a record: +{p['acquisition_growth']:.0%} a quarter since {P[4]}.",
            "Sales & marketing spend has been ramped as 'investing in growth'.",
            f"Net revenue retention is 'softening' ({f['nrr_p8']:.0%} in {P[-1]}).",
            f"A competitor launched in {P[4]}.",
            f"The blended LTV/CAC slide (computed on {P[0]}-{P[3]} customers) shows {f['ltv_cac_pre']:.1f}x.",
        ]
        conceal = [
            "cohort-level retention by acquisition month", f"the cause marker ({cause.replace('_', ' ')})",
            "the commission capitalization life vs expected customer life", "the sBG MRR runoff projection", "A1", "A2", "A3",
        ]
        stakeholders = [
            Stakeholder(name=names[0], role="CMO", agenda="Protect the acquisition budget.", disposition="enthusiastic",
                        knows=["paid social converts cheaply but with low intent"], hides=["paid social CAC excludes the sales assist"]),
            Stakeholder(name=names[1], role="VP Product", agenda="Defend the onboarding redesign.", disposition="defensive",
                        knows=["activation dipped after the redesign"], hides=["the activation dashboard was retired"]),
            Stakeholder(name=names[2], role="CFO", agenda="Reduce burn before the next raise.", disposition="worried",
                        knows=["the commission asset is growing oddly fast"], hides=[]),
            Stakeholder(name=names[3], role="Head of Customer Success", agenda="More headcount.", disposition="frustrated",
                        knows=["tickets are up; told it is just 'volume'"], hides=[]),
        ]
        cause_event = {"channel_shift": ("New paid channel launched (paid social)", "Performance agency hired; paid social budget ramps."),
                       "onboarding_regression": ("Onboarding redesign shipped", "Guided setup replaced by a self-serve checklist."),
                       "discount_cohorts": ("Spring promo: 30-40% off for three months", "Promo codes distributed through partners and ads.")}[cause]
        fund = state.extras["funding"]
        tl = [
            TimelineEvent(period=P[0], headline=f"{fund['series_label']} closed (${fund['series_cents'] / 1e8:,.1f}M) the quarter before",
                          detail="Preferred equity round to fund growth; the board expects a paid-acquisition push."),
            TimelineEvent(period=P[3], headline=cause_event[0], detail=cause_event[1]),
            TimelineEvent(period=P[4], headline=f"Venture debt drawn (${fund['venture_debt_cents'] / 1e8:,.1f}M)",
                          detail=f"Term facility at {fund['debt_rate']:.1%} to extend runway through the acquisition ramp."),
            TimelineEvent(period=P[5], headline="Record logo quarter", detail="Sales kick-off celebrates new-logo growth."),
            TimelineEvent(period=P[7], headline="NRR dips; board asks about LTV/CAC", detail="Board requests a unit-economics deep dive."),
            TimelineEvent(period=P[7], headline="Commission asset flagged in close", detail="Auditors ask about the amortization period."),
            TimelineEvent(period=P[4], headline="Competitor launches", detail="A well-funded competitor launches a cheaper plan.", is_red_herring=True),
        ]
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[6], headline="Payment processor migration - some failures", detail="Card failure rate spikes for two weeks.",
                                    is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["ltv_cac_slide_blended", "marketing_channel_mix_chart", "onboarding_funnel_excerpt", "support_ticket_category_counts"],
                           architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def _lever_state(self, state: WorldState) -> dict[str, float]:
        p, E = state.params, state.extras
        idx, a = E["keep"], E["a"]
        post = idx[a[idx] >= p["collapse_start_month"]]
        return {"paid_social_share_pts": float((E["channel"][post] == 4).mean() * 100),
                "max_discount_pct": float(E["discount"][post].max()) if len(post) else 0.0,
                "onboarding_max_cents": float(round(p["revenue_ttm_cents"] * 0.02))}

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Five levers in their natural units; defaults = the P8 status quo. Paid-social share, promo depth and the
        commission life are fixed by *reducing* them (``resolved_if`` uses ``<=``); onboarding is an investment (``>=``)."""
        ls = self._lever_state(state)
        ps_now, promo_now = round(ls["paid_social_share_pts"], 1), round(ls["max_discount_pct"], 1)
        return [
            Lever(key="marketing_spend_change_pct", label="Marketing spend change", min=-60, max=50, default=0, unit="%",
                  effect="New logos ~ spend^0.7 (diminishing returns); S&M moves 1:1."),
            Lever(key="paid_social_share_pct", label="Paid-social share of new logos", min=0, max=round(min(100.0, ps_now + 15), 1), default=ps_now,
                  unit="%", effect="Cutting it buys fewer, more expensive but better-retaining new logos."),
            Lever(key="onboarding_investment_cents", label="Onboarding investment per quarter", min=0, max=ls["onboarding_max_cents"], default=0, unit="cents",
                  effect="New cohorts' early churn returns to base over two quarters (if onboarding is the cause)."),
            Lever(key="promo_depth_pct", label="Maximum promotional discount", min=0, max=max(promo_now, 1.0), default=promo_now, unit="%",
                  effect="A shallower promo brings fewer promo-acquired logos and less churn at promo end (if promos are the cause)."),
            Lever(key="commission_amortization_months", label="Commission amortization life", min=12, max=48, default=COMMISSION_LIFE, unit="months",
                  effect="A life matching the expected customer life raises amortization and releases the over-capitalized asset."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form transition model: only the cause-specific lever removes the excess new-cohort churn."""
        p, E = state.params, state.extras
        f = self._facts(state)
        P = state.periods
        P8 = P[-1]
        kq = E["ops_kpis"]
        kp = kq[P8]
        ls = self._lever_state(state)
        excess = max(0.0, f["hazard_post"] - f["hazard_pre"])
        cause = p["collapse_cause"]
        amort8 = float(state.ledger.activity(P8)["6010"])
        kpis = {
            "mrr_cents": kp["mrr_cents"], "new_logos": kp["new_logos"], "new_cohort_churn_monthly": f["hazard_post"],
            "blended_mrr_churn_monthly": f["blended"][P8], "sm_expense_cents": kp["sm_expense_cents"], "ltv_cac_new": f["ltv_cac_post"],
            "commission_asset_cents": kp["commission_asset_cents"], "commission_amortization_cents": amort8,
            "operating_cash_flow_cents": kp["operating_cash_flow_cents"],
        }
        arpa = f["gpost"]["arpa"]
        mkt_q = float(E["marketing"][E["marketing"]["month"].isin([f"{m:%Y-%m}" for m in E["months"][-3:]])]["spend_cents"].sum())
        nl = kp["new_logos"]
        mrr_gain = excess * kp["mrr_cents"] * 0.5  # churn removed on the newest cohorts (second-order MRR effect, lagged)
        effects: list[dict[str, Any]] = [
            {"lever": "marketing_spend_change_pct", "kpi": "new_logos", "delta_per_unit": 0.007 * nl, "lag_periods": 0},
            {"lever": "marketing_spend_change_pct", "kpi": "sm_expense_cents", "delta_per_unit": 0.01 * mkt_q, "lag_periods": 0},
            {"lever": "marketing_spend_change_pct", "kpi": "operating_cash_flow_cents", "delta_per_unit": -0.01 * mkt_q, "lag_periods": 0},
            {"lever": "marketing_spend_change_pct", "kpi": "mrr_cents", "delta_per_unit": 0.007 * nl * arpa * 0.7, "lag_periods": 1},
            {"lever": "onboarding_investment_cents", "kpi": "operating_cash_flow_cents", "delta_per_unit": -1.0, "lag_periods": 0},
            # per point of paid-social share / promo depth (cutting them costs some new logos)
            {"lever": "paid_social_share_pct", "kpi": "new_logos", "delta_per_unit": 0.003 * nl, "lag_periods": 0},
            {"lever": "promo_depth_pct", "kpi": "new_logos", "delta_per_unit": 0.004 * nl, "lag_periods": 0},
            # per month of amortization life (a shorter life raises amortization and releases the over-capitalized asset)
            {"lever": "commission_amortization_months", "kpi": "commission_amortization_cents", "delta_per_unit": -amort8 / COMMISSION_LIFE, "lag_periods": 0},
            {"lever": "commission_amortization_months", "kpi": "commission_asset_cents", "delta_per_unit": f["overcap"] / 24, "lag_periods": 0},
        ]
        # the cause lever: ``sign`` = -1 for a reduction (paid-social share, promo depth), +1 for the onboarding investment
        if cause == "channel_shift":
            lever, span, sign = "paid_social_share_pct", max(1.0, ls["paid_social_share_pts"] - p["paid_social_share_pre"] * 100), -1.0
            cond = at_most(round(ls["paid_social_share_pts"] - 0.8 * span, 1))  # back to ~the pre-P5 mix
        elif cause == "onboarding_regression":
            lever, span, sign = "onboarding_investment_cents", 0.5 * ls["onboarding_max_cents"], 1.0
            cond = at_least(float(round(span)))  # half of the budget
        else:
            lever, span, sign = "promo_depth_pct", max(1.0, ls["max_discount_pct"] - 10), -1.0
            cond = at_most(round(ls["max_discount_pct"] - 0.8 * span, 1))  # promos back to ~the routine 10-20% range
        u = sign / span  # effect per unit of lever value, oriented so that moving towards the fix helps
        effects += [
            {"lever": lever, "kpi": "new_cohort_churn_monthly", "delta_per_unit": -0.5 * excess * u, "lag_periods": 1},
            {"lever": lever, "kpi": "new_cohort_churn_monthly", "delta_per_unit": -0.5 * excess * u, "lag_periods": 2},
            {"lever": lever, "kpi": "blended_mrr_churn_monthly", "delta_per_unit": -0.4 * excess * u, "lag_periods": 2},
            {"lever": lever, "kpi": "ltv_cac_new", "delta_per_unit": f["ltv_cac_post"] * (excess / max(1e-4, f["hazard_post"])) * u, "lag_periods": 2},
            {"lever": lever, "kpi": "mrr_cents", "delta_per_unit": mrr_gain * u, "lag_periods": 2},
        ]
        n = 3
        drag = {"blended_mrr_churn_monthly": (f["blended"][P8] - f["blended"][P[4]]) / n,
                "commission_asset_cents": (kp["commission_asset_cents"] - kq[P[4]]["commission_asset_cents"]) / n,
                "mrr_cents": -mrr_gain}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {lever: cond, "commission_amortization_months": at_most(COMMISSION_LIFE - 6)},
                "bounds": {"new_cohort_churn_monthly": [f["hazard_pre"] * 0.9, 0.5], "blended_mrr_churn_monthly": [0.0, 0.5], "new_logos": [0.0, None],
                           "ltv_cac_new": [0.0, None], "commission_asset_cents": [0.0, None]},
                "max_quarters": 4, "cause_lever": lever}


def _sbg_curve(fit: dict[str, Any], horizon: int) -> np.ndarray:
    """Survival ``S(0..horizon)``; near-geometric fits (``operations.sbg_fit`` returns ``geometric_limit`` on the
    ``alpha + beta = 1e3`` ray) use the geometric limit ``(1-theta)^t``."""
    a, b = fit["alpha"], fit["beta"]
    if not (np.isfinite(a) and np.isfinite(b)) or a + b > 500:
        theta = a / (a + b) if np.isfinite(a) and np.isfinite(b) and a + b > 0 else 0.02
        return (1 - theta) ** np.arange(horizon + 1, dtype=float)
    return sbg_survival(a, b, horizon)


def rng_name(g: np.random.Generator) -> str:
    stems = ("Acme", "Bluebird", "Cobalt", "Driftwood", "Ember", "Fjord", "Granite", "Harbor", "Ivy", "Juniper", "Kestrel", "Lumen", "Maple",
             "Northwind", "Orchid", "Pioneer", "Quarry", "Redwood", "Summit", "Tidal", "Upland", "Vertex", "Willow", "Yarrow", "Zephyr")
    i = int(g.integers(0, len(stems)))
    banned = excluded_name_stems()  # never the company's own stem (review P2 #19); no extra draw
    while stems[i] in banned:
        i = (i + 1) % len(stems)
    return str(stems[i])


def _month_floor(ts: np.ndarray) -> np.ndarray:
    return np.array(pd.to_datetime(ts).to_period("M").to_timestamp(), dtype="datetime64[ns]")


def _add_months(ts: np.ndarray, k: np.ndarray) -> np.ndarray:
    """Vectorised ``ts + k months`` (day clipped to month end, like ``pd.DateOffset``)."""
    t = pd.DatetimeIndex(pd.to_datetime(ts))
    y = t.year.to_numpy() * 12 + (t.month.to_numpy() - 1) + np.asarray(k, dtype=np.int64)
    yy, mm = y // 12, y % 12 + 1
    first = pd.to_datetime(pd.DataFrame({"year": yy, "month": mm, "day": 1}))
    dim = first.dt.days_in_month.to_numpy()
    day = np.minimum(t.day.to_numpy(), dim)
    tod = (t - t.normalize()).to_numpy()
    return (first.to_numpy() + pd.to_timedelta(day - 1, unit="D").to_numpy() + tod).astype("datetime64[ns]")


def _month_index(ts: np.ndarray, start: pd.Timestamp) -> np.ndarray:
    t = pd.DatetimeIndex(pd.to_datetime(ts))
    return ((t.year.to_numpy() - start.year) * 12 + (t.month.to_numpy() - start.month)).astype(np.int64)
