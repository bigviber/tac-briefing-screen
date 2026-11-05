"""``ma_synergies`` - "Synergy Math" (archetypes.md section 3).

A B2B SaaS company bought a smaller, faster-growing SaaS company at the end of P6, paying a premium
equal to management's claimed synergies. Two quarters later integration is "on track" by milestone
count, but the cost synergies arrive at 55-80% of plan, revenue synergies at 20-45%, overlapping
customers consolidate vendors, and goodwill is exposed to impairment.

Mechanics
---------
* **Acquirer** (``customers``, ``subscriptions``, ``invoices``, ``payments``): customers join over
  time and churn; each subscription is invoiced every 3 months in advance (Dr 1100 / Cr 2200) and
  recognised monthly (Dr 2200 / Cr 4000); net-30 payments.
* **Target** (``target_customers``, ``target_subscriptions``, ``target_invoices`` with ``paid_at``,
  ``target_invoice_lines``): same billing model, ``target_growth`` 20-45% a year. Pre-close the target
  keeps its own books (``target_financials`` quarterly P&L and ``opex_by_dept`` rows with
  ``entity='target'``); from P7 its invoices, recognition, collections and opex post to the acquirer
  GL with ``dimension entity=target``.
* **Deal** (``deal_assumptions``, ``deal_model``, ``purchase_price_allocation``): standalone DCF of the
  target = 5-year FCF (revenue growing from ``target_growth`` linearly down to ``terminal_growth``,
  FCF margin from the target's TTM operating margin to 22% by year 5, after tax) + Gordon terminal
  value at ``wacc``; claimed synergies = cost ``U(12, 20)%`` of target opex + revenue ``U(8, 15)%`` of
  target revenue at 70% contribution, ramp 25/50/75/100% over four quarters, less a planned
  integration cost of ``U(1.0, 1.2)x`` the annual cost run-rate; ``premium = PV(claimed) x U(0.9, 1.1)``,
  ``price = standalone + premium``. Purchase accounting at the last day of P6: Dr 1100 acquired AR,
  Dr 1500 PPE, Dr 1710 customer relationships ``U(25, 35)%`` + developed technology ``U(10, 15)%`` of price,
  Dr 1700 goodwill (plug); Cr 2200 acquired contract liabilities, Cr 1000 cash portion ``U(50, 70)%``,
  Cr 2610 acquisition debt. Intangibles amortise over 7 / 5 years from P7 (6310 / 1720).

Anomalies (the books are right; the break is between the deal model and the realised data)
-------------------------------------------------------------------------------------------
* **A1 synergy shortfall**: target opex in P7-P8 = standalone budget - achievable run-rate x ramp
  (achievable = claimed x ``U(0.55, 0.80)``); integration costs (Dr 6200 / Cr 2100, ``entity=target``)
  run at ``U(1.1, 1.4)x`` the plan and are labelled "non-recurring".
* **A2 overlap churn**: ``customer_overlap_pct`` of target customers share an e-mail domain with an
  acquirer customer (case / whitespace dirt); ``U(15, 30)%`` of that overlap ARR is cancelled in P7-P8
  with ``cancel_reason='consolidated vendor'``; cross-sell ``bundle`` subscriptions appear only at the
  achievable revenue-synergy rate.
* **A3** (principal) **target revenue quality**: ``U(5, 10)%`` of the target ARR in the deal model is
  one-time services booked on "recurring" subscriptions (``target_invoice_lines.line_type='services'``).
* **A4 goodwill exposure**: an ASC 350 one-step test at P8 with achievable synergies gives an implied
  fair value below carrying amount; no impairment has been recorded.

Documented simplifications
* Industry ``saas`` only (acquirer and target); difficulty range is foundation-principal (the spec says
  practitioner-principal; foundation uses the largest gap and three anomalies).
* The target keeps no row-level GL tables (``target_gl_*``) - row budget; its pre-close P&L is the
  quarterly ``target_financials`` table.
* Acquired receivables / contract liabilities flow through the indirect cash-flow lines of the
  close quarter (the CFS is derived from balance-sheet deltas; all ties hold).
* Quarter simulation (reduced form): ``integration_spend_cents`` (multiple of plan, 0-2) adds
  ``0.15 x spend/plan`` of ramp to realised cost synergies; ``retention_program_pct_discount`` cuts
  overlap churn by ``0.6 x d/20`` at a cost of ``d%`` of overlap ARR; ``price_harmonization``
  (-1 / 0 / +1) moves ARPA +-3% with churn elasticity; ``record_impairment`` writes goodwill down to the
  implied fair value (non-cash, equity and covenant headroom down, credibility up);
  ``divest_target_unit_pct`` brings cash at 0.7x standalone and removes synergies.
  ``resolved_if``: impairment recorded and a retention discount of at least 5%.
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

logger = logging.getLogger("axiom.archetypes.ma_synergies")

EOD = pd.Timedelta(hours=23, minutes=59, seconds=59)
DEPTS = ("sales_marketing", "research_development", "general_admin", "customer_success")
DEPT_ACCT = {"sales_marketing": "6000", "research_development": "6100", "general_admin": "6200", "customer_success": "6000"}
DEPT_SPLIT = {"sales_marketing": 0.38, "research_development": 0.32, "general_admin": 0.18, "customer_success": 0.12}
CONTRIBUTION_MARGIN = 0.70
LONG_RUN_FCF_MARGIN = 0.22
RAMP = (0.25, 0.50, 0.75, 1.0)
DOMAINS = ("io", "com", "co", "net", "ai")


def _month_index(ts: pd.Series, start: dt.date) -> np.ndarray:
    t = pd.to_datetime(ts)
    return np.asarray((t.dt.year - start.year) * 12 + (t.dt.month - start.month))


def dcf_value(rev0: float, g1: float, gt: float, m0: float, m_lr: float, tax: float, wacc: float) -> dict[str, Any]:
    """5-year FCF + Gordon terminal value (end-year discounting). Revenue growth declines linearly from ``g1`` to
    ``gt`` (year 5); FCF margin moves linearly from ``m0`` to ``m_lr``; FCF = revenue x margin x (1 - tax)."""
    years = []
    rev = rev0
    pv = 0.0
    for y in range(1, 6):
        g = g1 - (g1 - gt) * (y - 1) / 4
        rev *= 1 + g
        m = m0 + (m_lr - m0) * y / 5
        fcf = rev * m * (1 - tax)
        pv += fcf / (1 + wacc) ** y
        years.append({"year": y, "growth": g, "revenue": rev, "fcf_margin": m, "fcf": fcf})
    tv = years[-1]["fcf"] * (1 + gt) / (wacc - gt)
    pv_tv = tv / (1 + wacc) ** 5
    return {"value": pv + pv_tv, "pv_fcf": pv, "pv_terminal": pv_tv, "years": years}


def synergy_pv(cost_rr: float, rev_rr: float, integration: float, tax: float, wacc: float, gt: float, *, dis_rr: float = 0.0) -> float:
    """PV of synergies: annual cost run-rate + revenue run-rate x contribution margin - dis-synergy, ramped
    25/50/75/100% over the first four quarters (year 1 = 62.5%), after tax, 5 years + Gordon terminal value;
    less the one-time integration cost (after tax, year 1)."""
    annual = (cost_rr + rev_rr * CONTRIBUTION_MARGIN - dis_rr) * (1 - tax)
    pv = 0.0
    for y in range(1, 6):
        pv += annual * (sum(RAMP) / 4 if y == 1 else 1.0) / (1 + wacc) ** y
    pv += annual * (1 + gt) / (wacc - gt) / (1 + wacc) ** 5
    return pv - integration * (1 - tax) / (1 + wacc)


@register
class MaSynergies(Archetype):
    """Synergy Math: the premium paid equals the claimed synergies; the realised ones fall short."""

    key = "ma_synergies"
    title = "Synergy Math"
    pillars = ("accounting_finance", "operations_strategy", "python")
    industries = ("saas",)
    schema_template = "saas_billing"
    summary = ("Two quarters after a headline acquisition the integration office reports milestones on track, but the board "
               "wants a synergy tracker and the CFO is worried about the year-end goodwill test.")
    root_cause_terms = ("achievable synergies", "overlap", "impairment", "premium equals", "dis-synergy")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 3 (difficulty scales the achievable multipliers and anomalies)."""
        ind = industry or "saas"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        R = math.exp(rng.uniform(math.log(40e6), math.log(120e6)))
        # gap to plan: foundation draws the widest gap (lowest achievable share), principal the narrowest (subtle)
        cost_ach = 0.55 + (0.80 - 0.55) - magnitude(rng, difficulty, 0.0, 0.25)
        rev_ach = 0.20 + (0.45 - 0.20) - magnitude(rng, difficulty, 0.0, 0.25)
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": "B2B SaaS platform that acquired a faster-growing point solution",
            "revenue_ttm_cents": int(R * 100),
            "target_revenue_ratio": float(rng.uniform(0.15, 0.40)),
            "acquirer_growth": float(rng.uniform(0.05, 0.15)),
            "target_growth": round(float(rng.uniform(0.20, 0.45)), 6),
            "wacc": round(float(np.clip(bk.sample_between(pool, rng.spawn("bench:wacc"), "any.wacc", "any", 0.45, 0.75), 0.09, 0.12)), 6),
            "terminal_growth": round(float(rng.uniform(0.02, 0.03)), 6),
            "cost_synergy_claimed_pct": float(rng.uniform(0.12, 0.20)),
            "cost_synergy_achievable_mult": float(cost_ach),
            "revenue_synergy_claimed_pct": float(rng.uniform(0.08, 0.15)),
            "revenue_synergy_achievable_mult": float(rev_ach),
            "customer_overlap_pct": float(rng.uniform(0.10, 0.25)),
            "overlap_churn_share": magnitude(rng, difficulty, 0.15, 0.30),
            "premium_factor": float(rng.uniform(0.9, 1.1)),
            "integration_plan_mult": float(rng.uniform(1.0, 1.2)),
            "integration_overrun": float(rng.uniform(1.1, 1.4)),
            "cust_rel_pct": float(rng.uniform(0.25, 0.35)),
            "tech_pct": float(rng.uniform(0.10, 0.15)),
            "cash_portion": float(rng.uniform(0.5, 0.7)),
            "target_margin": float(rng.uniform(-0.12, 0.04)),
            "acquirer_margin": float(rng.uniform(0.08, 0.16)),
            "gm": float(rng.uniform(0.74, 0.82)),
            "tax_rate": round(float(rng.uniform(0.21, 0.25)), 6),  # deal inputs are published with 6 decimals
            "debt_rate": float(rng.uniform(0.07, 0.095)),
            "churn_monthly": float(np.clip(bk.sample_between(pool, rng.spawn("bench:churn"), "saas.gross_churn_monthly_midmarket", "saas", 0.25, 0.6), 0.006, 0.02)),
            "rev_quality_issue": difficulty == "principal",
            "services_mislabelled_pct": float(rng.uniform(0.05, 0.10)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        p["n_acquirer_customers"] = difficulty_pick(difficulty, 230, 560, 2000)
        p["n_target_customers"] = max(40, int(p["n_acquirer_customers"] * difficulty_pick(difficulty, 0.35, 0.35, 0.35)))
        p["description"] = ("B2B SaaS company; acquired a smaller SaaS company at the end of the sixth quarter of the case window, "
                            "financed with cash and acquisition debt.")
        return p

    # ------------------------------------------------------------------ helpers: subscription book

    def _book(self, g: np.random.Generator, n_cust: int, n_m: int, rev_ttm: float, growth: float, churn: float, prefix: str,
              start_share: float = 0.6) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Customers with lifetimes and MRR (scaled to the TTM revenue), and quarterly in-advance invoices."""
        start = np.where(g.random(n_cust) < start_share, 0, g.integers(1, n_m, n_cust))
        # growth: later cohorts slightly larger
        life = g.geometric(churn, n_cust)
        end = start + life
        mrr = g.lognormal(math.log(4000), 0.7, n_cust) * (1 + growth) ** (start / 12)
        # customers already live at the start keep their billing anniversary: a stub invoice in month 0 covers the
        # months up to their next quarterly renewal, so renewals (and deferred revenue) are spread over the quarter
        phase = np.where(start == 0, g.integers(0, 3, n_cust), 0)
        cust = pd.DataFrame({"i": np.arange(n_cust), "start": start, "end": end, "mrr": mrr})
        rows = []
        for i, s0, e0, ph in zip(cust["i"], cust["start"], cust["end"], phase, strict=True):
            if ph:
                rows.append((i, 0, min(int(ph), int(e0))))
            for m in range(int(s0) + int(ph), min(int(e0), n_m), 3):
                rows.append((i, m, min(3, int(e0) - m)))
        inv = pd.DataFrame(rows, columns=["i", "m", "months"])
        # scale MRR so that recognised revenue over the last 12 months ~= rev_ttm
        rec = np.zeros(n_m)
        for k in range(3):
            mm = inv["m"].to_numpy() + k
            ok = (k < inv["months"].to_numpy()) & (mm < n_m)
            np.add.at(rec, mm[ok], mrr[inv["i"].to_numpy()[ok]])
        scale = rev_ttm / max(1.0, rec[-12:].sum())
        cust["mrr"] = cust["mrr"] * scale
        cust["mrr_cents"] = cents_array(cust["mrr"].to_numpy() * 100)
        inv["amount_cents"] = cust["mrr_cents"].to_numpy()[inv["i"].to_numpy()] * inv["months"].to_numpy()
        cust["prefix"] = prefix
        return cust, inv

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Acquirer and target subscription books (no deal effects yet)."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=True)  # base working-capital defaults: vendor AP for part of the accrued-expense run and prepaid expenses
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        state.extras["months"] = months
        R = p["revenue_ttm_cents"] / 100
        # acquirer TTM at close (P3..P6) sized so its revenue keeps growing; the book is scaled on the last 12 months
        a_cust, a_inv = self._book(g, p["n_acquirer_customers"], n_m, R, p["acquirer_growth"], p["churn_monthly"], "A")
        t_rev = R * p["target_revenue_ratio"]
        t_cust, t_inv = self._book(g, p["n_target_customers"], n_m, t_rev, p["target_growth"], p["churn_monthly"] * 0.8, "T", start_share=0.35)
        # after the close the target's sales team is folded into the acquirer's: ~80% of the new logos it would have won never arrive
        close_m = 3 * (len(cal.warmup_periods) + 6)
        lost = (t_cust["start"] >= close_m).to_numpy() & (g.random(len(t_cust)) < 0.8)
        t_cust = t_cust[~lost].reset_index(drop=True)
        t_inv = t_inv[t_inv["i"].isin(np.flatnonzero(~lost))].copy()
        t_inv["i"] = pd.Series(np.cumsum(~lost) - 1)[t_inv["i"].to_numpy()].to_numpy()
        t_cust["i"] = np.arange(len(t_cust))
        a_names = [rng.company_name("saas") for _ in range(len(a_cust))]
        t_names = [rng.company_name("saas") for _ in range(len(t_cust))]
        a_cust["name"] = a_names
        t_cust["name"] = t_names
        a_cust["domain"] = [f"{n.split()[0].lower()}{i}.{DOMAINS[i % len(DOMAINS)]}" for i, n in enumerate(a_names)]
        t_cust["domain"] = [f"{n.split()[0].lower()}{i}x.{DOMAINS[i % len(DOMAINS)]}" for i, n in enumerate(t_names)]
        state.extras.update({"a_cust": a_cust, "a_inv": a_inv, "t_cust": t_cust, "t_inv": t_inv})

    # ------------------------------------------------------------------ anomalies

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Overlap, churn, cross-sell, services mislabelling; the deal (DCF, PPA) is computed in ``post_ledger``."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("deal").generator
        P = state.periods
        months = state.extras["months"]
        n_m = len(months)
        close_m = 3 * (len(cal.warmup_periods) + 6)  # first month of P7
        state.extras["close_m"] = close_m
        a_cust, t_cust, t_inv = state.extras["a_cust"], state.extras["t_cust"], state.extras["t_inv"]
        # overlap: target customers alive at close that share a domain with an acquirer customer alive at close
        t_alive = np.flatnonzero((t_cust["start"] < close_m) & (t_cust["end"] > close_m))
        a_alive = np.flatnonzero((a_cust["start"] < close_m) & (a_cust["end"] > close_m))
        n_ov = max(3, int(round(p["customer_overlap_pct"] * len(t_alive))))
        ov_t = g.choice(t_alive, size=min(n_ov, len(t_alive)), replace=False)
        ov_a = g.choice(a_alive, size=len(ov_t), replace=False)
        t_cust.loc[ov_t, "domain"] = a_cust["domain"].to_numpy()[ov_a]
        t_cust["overlap"] = False
        t_cust.loc[ov_t, "overlap"] = True
        # overlap churn in P7-P8 (dis-synergy): cancel the largest-ARR share until the target share of overlap ARR is reached
        ov_arr = t_cust.loc[ov_t, "mrr"] * 12
        order = ov_arr.sort_values(ascending=False, kind="stable").index.to_numpy()
        order = order[g.permutation(len(order))]
        cum = np.cumsum(t_cust.loc[order, "mrr"].to_numpy() * 12)
        k = max(1, int(np.searchsorted(cum, p["overlap_churn_share"] * ov_arr.sum())) + 1)
        churned = order[:k]
        t_cust["cancel_reason"] = None
        t_cust.loc[churned, "end"] = close_m + g.integers(1, n_m - close_m, len(churned))
        t_cust.loc[churned, "cancel_reason"] = "consolidated vendor"
        natural = (t_cust["end"] < n_m) & t_cust["cancel_reason"].isna()
        t_cust.loc[natural, "cancel_reason"] = np.array(("budget", "switched_competitor", "business_closed", "no_longer_needed"))[g.integers(0, 4, int(natural.sum()))]
        # rebuild target invoices for churned customers (stop at the new end)
        t_inv = t_inv.merge(t_cust[["i", "end"]], on="i")
        t_inv = t_inv[t_inv["m"] < t_inv["end"]].copy()
        t_inv["months"] = np.minimum(t_inv["months"], t_inv["end"] - t_inv["m"])
        t_inv["amount_cents"] = t_cust["mrr_cents"].to_numpy()[t_inv["i"].to_numpy()] * t_inv["months"].to_numpy()
        t_inv = t_inv.drop(columns=["end"]).reset_index(drop=True)
        # services mislabelled as recurring (principal): one-time implementation projects booked on "recurring" plans
        t_cust["services_mislabelled"] = False
        if p["rev_quality_issue"]:
            pre = t_alive[~np.isin(t_alive, churned)]
            arr_close = (t_cust.loc[t_alive, "mrr"] * 12).sum()
            cand = pre[g.permutation(len(pre))]
            cum = np.cumsum(t_cust.loc[cand, "mrr"].to_numpy() * 12)
            k2 = max(1, int(np.searchsorted(cum, p["services_mislabelled_pct"] * arr_close)) + 1)
            t_cust.loc[cand[:k2], "services_mislabelled"] = True
        # cross-sell bundles: target customers alive after close buy the acquirer's bundle at the achievable revenue-synergy rate
        t_rev_annual = float(t_cust.loc[t_alive, "mrr"].sum() * 12)
        claimed_rev_rr = p["revenue_synergy_claimed_pct"] * t_rev_annual
        ach_rev_rr = claimed_rev_rr * p["revenue_synergy_achievable_mult"]
        cand = [i for i in t_alive if i not in set(churned)]
        cand = np.array(cand)[g.permutation(len(cand))]
        n_b = max(2, int(round(len(cand) * 0.15)))
        buyers = cand[:n_b]
        mrr_b = np.full(n_b, ach_rev_rr / 12 / n_b) * g.lognormal(0, 0.2, n_b)
        mrr_b *= ach_rev_rr / 12 / mrr_b.sum()
        start_b = close_m + np.where(g.random(n_b) < 0.4, 0, g.integers(1, max(2, n_m - close_m), n_b))
        bundle = pd.DataFrame({"t_i": buyers, "start": start_b, "mrr_cents": cents_array(mrr_b * 100)})
        state.extras.update({"t_cust": t_cust, "t_inv": t_inv, "churned": churned, "overlap_idx": ov_t, "bundle": bundle,
                             "t_rev_annual_close": t_rev_annual, "claimed_rev_rr": claimed_rev_rr, "ach_rev_rr": ach_rev_rr})
        anomalies = [
            Anomaly(id="A1", kind="synergy_shortfall", pillar="operations_strategy", title="Cost synergies arrive late and short",
                    mechanism=(f"Target opex after close = standalone budget - achievable run-rate x ramp; achievable = "
                               f"{p['cost_synergy_achievable_mult']:.0%} of the claimed {p['cost_synergy_claimed_pct']:.0%} of target opex. Integration "
                               f"costs run at {p['integration_overrun']:.1f}x plan (Dr 6200 / Cr 2100) and are presented as non-recurring."),
                    periods=[P[6], P[7]], affected_accounts=["6000", "6100", "6200"], affected_tables=["opex_by_dept", "deal_model", "gl_lines"],
                    magnitude={"cost_synergy_achievable_mult": round(p["cost_synergy_achievable_mult"], 6),
                               "integration_overrun": round(p["integration_overrun"], 6)}, severity="high"),
            Anomaly(id="A2", kind="customer_overlap_churn", pillar="operations_strategy", title="Overlapping customers consolidate vendors",
                    mechanism=(f"{len(ov_t)} target customers share an e-mail domain with an acquirer customer; {len(churned)} of them (~"
                               f"{p['overlap_churn_share']:.0%} of overlap ARR) cancel after close with cancel_reason='consolidated vendor'. "
                               f"Cross-sell bundles land at {p['revenue_synergy_achievable_mult']:.0%} of the claimed revenue synergy."),
                    periods=[P[6], P[7]], affected_accounts=["4000", "2200", "1100"], affected_tables=["target_subscriptions", "target_customers", "customers", "subscriptions"],
                    magnitude={"overlap_churn_share": round(p["overlap_churn_share"], 6), "revenue_synergy_achievable_mult": round(p["revenue_synergy_achievable_mult"], 6)},
                    severity="high"),
        ]
        if p["rev_quality_issue"]:
            anomalies.append(Anomaly(id="A3", kind="revenue_quality_mislabel", pillar="accounting_finance",
                                     title="Target ARR included one-time services", mechanism=(
                                         "Implementation projects were booked on subscriptions flagged plan='recurring' (target_invoice_lines.line_type="
                                         "'services'); the deal model's ARR counted them as recurring and they stop after a few invoices."),
                                     periods=list(P[2:6]), affected_accounts=["4000"], affected_tables=["target_subscriptions", "target_invoice_lines"],
                                     magnitude={"services_mislabelled_pct": round(p["services_mislabelled_pct"], 6)}, severity="medium"))
        anomalies.append(Anomaly(id=f"A{len(anomalies) + 1}", kind="goodwill_impairment_risk", pillar="accounting_finance",
                                 title="Goodwill exposed: implied fair value below carrying amount",
                                 mechanism=("Goodwill = price - fair value of net assets and identified intangibles; the price included all claimed "
                                            "synergies, so a P8 test on achievable synergies gives an implied fair value below carrying amount. "
                                            "No impairment has been recorded."),
                                 periods=[P[7]], affected_accounts=["1700", "1710", "1720"], affected_tables=["purchase_price_allocation", "deal_model", "gl_lines"],
                                 magnitude={}, severity="critical"))
        return anomalies

    # ------------------------------------------------------------------ deal math

    def _target_financials(self, state: WorldState) -> pd.DataFrame:
        """Target quarterly P&L (standalone through P6; P7-P8 actual post-close): revenue, COGS, opex by dept."""
        if "t_fin" in state.extras:
            return state.extras["t_fin"]
        p, cal = state.params, state.calendar
        months = state.extras["months"]
        n_m = len(months)
        t_cust, t_inv = state.extras["t_cust"], state.extras["t_inv"]
        rec = np.zeros(n_m)
        for k in range(3):
            mm = t_inv["m"].to_numpy() + k
            ok = (k < t_inv["months"].to_numpy()) & (mm < n_m)
            np.add.at(rec, mm[ok], t_cust["mrr_cents"].to_numpy()[t_inv["i"].to_numpy()[ok]])
        rows = []
        for qi, per in enumerate(cal.all_periods):
            rev = float(rec[3 * qi: 3 * qi + 3].sum())
            rows.append({"period": per, "revenue_cents": int(rev)})
        df = pd.DataFrame(rows)
        # standalone cost base follows a smoothed revenue path (budget), not the realised quarter
        smooth = df["revenue_cents"].rolling(2, min_periods=1).mean().to_numpy()
        df["cogs_cents"] = cents_array(smooth * (1 - p["gm"]))
        df["standalone_opex_cents"] = cents_array(smooth * (p["gm"] - p["target_margin"]))
        state.extras["t_fin"] = df
        state.extras["t_rec_monthly"] = rec
        return df

    def _deal(self, state: WorldState) -> dict[str, Any]:
        """Deal model: standalone DCF, claimed / achievable synergy PVs, price, PPA inputs (from the target data)."""
        if "deal" in state.extras:
            return state.extras["deal"]
        p, cal = state.params, state.calendar
        P = state.periods
        tf = self._target_financials(state).set_index("period")
        ttm = [P[2], P[3], P[4], P[5]]
        rev0 = float(tf.loc[ttm, "revenue_cents"].sum())
        opex0 = float(tf.loc[ttm, "standalone_opex_cents"].sum())
        m0 = float((rev0 - tf.loc[ttm, "cogs_cents"].sum() - opex0) / rev0)
        tax, wacc, gt = p["tax_rate"], p["wacc"], p["terminal_growth"]
        standalone = dcf_value(rev0, p["target_growth"], gt, m0, LONG_RUN_FCF_MARGIN, tax, wacc)
        cost_claimed = p["cost_synergy_claimed_pct"] * opex0
        rev_claimed = p["revenue_synergy_claimed_pct"] * rev0
        integ_plan = p["integration_plan_mult"] * cost_claimed
        pv_claimed = synergy_pv(cost_claimed, rev_claimed, integ_plan, tax, wacc, gt)
        pv_claimed_gross = synergy_pv(cost_claimed, rev_claimed, 0.0, tax, wacc, gt)
        premium = pv_claimed * p["premium_factor"]
        price = int(round((standalone["value"] + premium) / 100_000)) * 100_000
        d = {"rev0": rev0, "opex0": opex0, "m0": m0, "standalone": standalone, "cost_claimed": cost_claimed, "rev_claimed": rev_claimed,
             "integ_plan": integ_plan, "pv_claimed": pv_claimed, "pv_claimed_gross": pv_claimed_gross, "premium": premium, "price": price,
             "premium_pct": premium / standalone["value"], "cost_ach": cost_claimed * p["cost_synergy_achievable_mult"],
             "integ_actual": integ_plan * p["integration_overrun"]}
        state.extras["deal"] = d
        return d

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Build invoice/payment frames, run the deal math, then post acquirer books, purchase accounting and the consolidated P7-P8."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("ledger").generator
        L = state.ledger
        gran = GL_BATCH[p["difficulty"]]
        months = state.extras["months"]
        n_m = len(months)
        end_date = cal.end
        end_ts = pd.Timestamp(end_date) + EOD
        close_m = state.extras["close_m"]
        close_date = (pd.Timestamp(months[close_m]) - pd.Timedelta(days=1)).date()
        state.extras["close_date"] = close_date
        mstart = pd.to_datetime(pd.Series(months))
        a_cust, a_inv, t_cust, t_inv, bundle = (state.extras[k] for k in ("a_cust", "a_inv", "t_cust", "t_inv", "bundle"))
        # --- bundle subscriptions -> acquirer customers + invoices
        nb = len(bundle)
        base_a = len(a_cust)
        b_cust = pd.DataFrame({"i": np.arange(base_a, base_a + nb), "start": bundle["start"].to_numpy(), "end": np.full(nb, 10_000),
                               "mrr": bundle["mrr_cents"].to_numpy() / 100, "mrr_cents": bundle["mrr_cents"].to_numpy(), "prefix": "A",
                               "name": t_cust["name"].to_numpy()[bundle["t_i"].to_numpy()], "domain": t_cust["domain"].to_numpy()[bundle["t_i"].to_numpy()],
                               "bundle": True})
        a_cust = pd.concat([a_cust.assign(bundle=False), b_cust], ignore_index=True)
        b_rows = [(i, m, min(3, n_m - m)) for i, s0 in zip(b_cust["i"], b_cust["start"], strict=True) for m in range(int(s0), n_m, 3)]
        b_inv = pd.DataFrame(b_rows, columns=["i", "m", "months"])
        b_inv["amount_cents"] = a_cust["mrr_cents"].to_numpy()[b_inv["i"].to_numpy()] * b_inv["months"].to_numpy()
        a_inv = pd.concat([a_inv, b_inv], ignore_index=True)

        def dated(inv: pd.DataFrame, prefix: str, cust: pd.DataFrame, delay_mu: float) -> pd.DataFrame:
            inv = inv.sort_values(["m", "i"], kind="stable").reset_index(drop=True)
            day = g.integers(0, 5, len(inv))
            inv["invoice_date"] = mstart.to_numpy()[inv["m"].to_numpy()] + pd.to_timedelta(day, unit="D").to_numpy()
            inv["invoice_id"] = [f"{prefix}INV{j:06d}" for j in range(1, len(inv) + 1)]
            delay = np.maximum(-5, np.round(g.normal(delay_mu, 9, len(inv))))
            paid = inv["invoice_date"] + pd.to_timedelta(30 + delay, unit="D") + pd.to_timedelta(g.integers(9 * 3600, 17 * 3600, len(inv)), unit="s")
            inv["paid_at"] = paid.where(paid <= end_ts)
            return inv

        a_inv = dated(a_inv, "A", a_cust, 3.0)
        t_inv = dated(t_inv, "T", t_cust, 6.0)
        state.extras.update({"a_cust": a_cust, "a_inv": a_inv, "t_inv": t_inv})
        deal = self._deal(state)
        tf = self._target_financials(state)
        # --- recognition schedules: amount split over its months (residual to the last)
        def schedule(inv: pd.DataFrame) -> pd.DataFrame:
            parts = [split_cents(int(a), [1.0] * int(k)) for a, k in zip(inv["amount_cents"], inv["months"], strict=True)]
            rows = [(j, int(m0 + q), int(v)) for j, (m0, pr) in enumerate(zip(inv["m"], parts, strict=True)) for q, v in enumerate(pr)]
            return pd.DataFrame(rows, columns=["row", "m", "cents"])

        a_sch = schedule(a_inv)
        t_sch = schedule(t_inv)
        state.extras.update({"a_sch": a_sch, "t_sch": t_sch})
        # --- acquired working capital at close (target invoices issued before the close month)
        pre = t_inv["m"] < close_m
        t_open_close = int(t_inv.loc[pre & (t_inv["paid_at"].isna() | (t_inv["paid_at"] > pd.Timestamp(close_date) + EOD)), "amount_cents"].sum())
        t_def_close = int(t_sch[(t_sch["row"].isin(np.flatnonzero(pre))) & (t_sch["m"] >= close_m)]["cents"].sum())
        ppe_t = cents(deal["rev0"] * 0.04)
        price = deal["price"]
        cust_rel = cents(price * p["cust_rel_pct"])
        tech = cents(price * p["tech_pct"])
        net_tangible = t_open_close + ppe_t - t_def_close
        goodwill = price - cust_rel - tech - net_tangible
        cash_paid = int(round(price * p["cash_portion"] / 100_000)) * 100_000
        acq_debt = price - cash_paid
        state.extras["ppa"] = {"price": price, "ar": t_open_close, "deferred_revenue": t_def_close, "ppe": ppe_t, "customer_relationships": cust_rel,
                               "developed_technology": tech, "goodwill": goodwill, "cash_paid": cash_paid, "acquisition_debt": acq_debt}
        # --- opening balances: acquirer with a war chest for the cash portion
        R = p["revenue_ttm_cents"]
        ppe = cents(R * 0.05)
        accdep = cents(ppe * 0.4)
        cash0 = cents(R * 0.25) + cash_paid
        net = cash0 + ppe - accdep
        cs = cents(0.7 * net)
        L.open_books(cal.opening_date, {"1000": cash0, "1500": ppe, "1510": -accdep, "3000": -cs, "3100": -(net - cs)})
        events: list[tuple[dt.date, int, Any]] = []
        # acquirer invoices + payments, target invoices/payments from the close month
        t_post = t_inv[t_inv["m"] >= close_m]
        for inv, ent in ((a_inv, "acquirer"), (t_post, "target")):
            ib = inv.assign(b=batch_end_dates(inv["invoice_date"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("invoice_id", "size"))
            for b, r in ib.iterrows():
                dim = {"batch": f"billing:{b.date()}", "rows": str(int(r.n)), "entity": ent}
                events.append((b.date(), 2, lambda d, a=int(r.a), dm=dim: L.adjust(d, [("1100", a, 0, dm), ("2200", 0, a, dm)], "Subscription invoices (billed in advance)", "billing")))
        a_paid = a_inv[a_inv["paid_at"].notna()]
        t_paid = t_inv[t_inv["paid_at"].notna() & (t_inv["paid_at"] > pd.Timestamp(close_date) + EOD)]
        for inv, ent in ((a_paid, "acquirer"), (t_paid, "target")):
            pb = inv.assign(b=batch_end_dates(inv["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("invoice_id", "size"))
            for b, r in pb.iterrows():
                dim = {"batch": f"collections:{b.date()}", "rows": str(int(r.n)), "entity": ent}
                events.append((b.date(), 3, lambda d, a=int(r.a), dm=dim: L.collect_cash(d, a, memo="Customer payments (batch)", dim=dm)))
        a_rec_m = a_sch.groupby("m")["cents"].sum().reindex(range(n_m), fill_value=0).to_numpy()
        t_rec_m = t_sch.groupby("m")["cents"].sum().reindex(range(n_m), fill_value=0).to_numpy()
        # integration cost schedule (P7-P8 share of the overrun total) and synergy ramp per month
        integ_total = deal["integ_actual"]
        integ_m = {close_m + k: cents(integ_total * w) for k, w in enumerate((0.16, 0.14, 0.12, 0.10, 0.08, 0.07))}
        prev = {"payroll": 0, "opex": 0}
        amort_cr = cents(cust_rel / 84)
        amort_tech = cents(tech / 60)
        a_opex_pct = p["gm"] - p["acquirer_margin"]
        t_quarter = tf.set_index("period")
        realized_syn: dict[int, int] = {}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            per = cal.labels([ms])[0]
            post = m >= close_m
            rec_a = int(a_rec_m[m])
            rec_t = int(t_rec_m[m]) if post else 0
            events.append((me, 4, lambda d, a=rec_a: L.recognize_deferred(d, a, memo="Subscription revenue recognised", dim={"entity": "acquirer"})))
            if rec_t:
                events.append((me, 4, lambda d, a=rec_t: L.recognize_deferred(d, a, memo="Subscription revenue recognised (target)", dim={"entity": "target"})))
            run = float(a_rec_m[max(0, m - 2): m + 1].mean())
            cogs = cents(run * (1 - p["gm"] - self.period_noise(ctx, state).gm(ms)))  # review C2: quarterly GM / opex noise
            opex = cents(run * a_opex_pct * self.period_noise(ctx, state).ox(ms))
            if post:
                q = (m - close_m) // 3
                tq = t_quarter.loc[per]
                cogs += cents(tq["cogs_cents"] / 3)
                syn = cents(deal["cost_ach"] / 12 * RAMP[min(q, 3)])
                realized_syn[m] = syn
                opex += cents(tq["standalone_opex_cents"] / 3) - syn
            payroll = cents(opex * 0.65)
            other = opex - payroll
            pr = dict(zip(("6000", "6100", "6200"), split_cents(payroll, [0.45, 0.35, 0.20]), strict=True))
            ox = dict(zip(("6000", "6200", "6400"), split_cents(other, [0.45, 0.30, 0.25]), strict=True))
            if m > 0:
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["opex"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex paid", source="opex")))
            if ms.month in (1, 4, 7, 10) and m > 0:
                events.append((ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"])))
            events.append((me, 7, lambda d, a=cogs: L.adjust(d, [("5000", a, 0), ("2100", 0, a)], "Hosting and support costs", "opex")))
            events.append((me, 7, lambda d, s=pr: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox: L.accrue_opex(d, s)))
            integ = integ_m.get(m, 0)
            if integ:
                dim = {"entity": "target", "program": "integration"}
                events.append((me, 7, lambda d, a=integ, dm=dim: L.adjust(d, [("6200", a, 0, dm), ("2100", 0, a, dm)], "Integration costs (presented as non-recurring)", "integration")))
            events.append((ms + dt.timedelta(days=14), 6, lambda d, a=cents(run * 0.02): L.capex(d, a)))
            events.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 48))))
            if post:
                events.append((me, 8, lambda d: L.depreciate(d, amort_cr + amort_tech, expense_account="6310", contra_account="1720", memo="Amortisation of acquired intangibles")))
                events.append((me, 9, lambda d: L.accrue_interest(d, cents(acq_debt * p["debt_rate"] / 12))))
            if ms.month % 3 == 0:
                if post:
                    events.append((me, 10, lambda d: L.pay_interest(d, -L.closing_balances()["2150"])))

                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))

                events.append((me, 11, tax))
            prev = {"payroll": payroll, "opex": other + cogs + integ}
        # purchase accounting on the close date
        pa = state.extras["ppa"]
        dim = {"deal": "target_acquisition"}
        lines = [("1100", pa["ar"], 0, dim), ("1500", pa["ppe"], 0, dim), ("1710", pa["customer_relationships"] + pa["developed_technology"], 0, dim),
                 ("1700", pa["goodwill"], 0, dim), ("2200", 0, pa["deferred_revenue"], dim), ("1000", 0, pa["cash_paid"], dim), ("2610", 0, pa["acquisition_debt"], dim)]
        events.append((close_date, 12, lambda d, ln=lines: L.adjust(d, ln, "Acquisition of target: purchase price allocation (ASC 805)", "acquisition")))
        events.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in events:
            if d <= end_date:
                fn(d)
        state.extras["realized_syn_m"] = realized_syn
        state.extras["integ_m"] = integ_m

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Acquirer and target tables, deal artifacts and opex by department; dirt 2 / 4 / 6."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("tables").generator
        months = state.extras["months"]
        n_m = len(months)
        mstart = pd.to_datetime(pd.Series(months))
        a_cust, a_inv, t_cust, t_inv = (state.extras[k] for k in ("a_cust", "a_inv", "t_cust", "t_inv"))
        deal = self._deal(state)
        P = state.periods
        close_m = state.extras["close_m"]

        def cust_frame(c: pd.DataFrame, prefix: str) -> pd.DataFrame:
            seg = np.where(c["mrr"] > c["mrr"].quantile(0.8), "enterprise", np.where(c["mrr"] > c["mrr"].quantile(0.4), "midmarket", "smb"))
            return pd.DataFrame({"customer_id": [f"{prefix}C{i:05d}" for i in c["i"]], "name": c["name"].to_numpy(), "segment": seg,
                                 "billing_email": ["ap@" + d for d in c["domain"]], "country": "US",
                                 "created_at": mstart.to_numpy()[np.minimum(c["start"].to_numpy(), n_m - 1)] - pd.to_timedelta(g.integers(3, 40, len(c)), unit="D").to_numpy()})

        def sub_frame(c: pd.DataFrame, prefix: str, plan: Any) -> pd.DataFrame:
            ended = c["end"].to_numpy() < n_m
            return pd.DataFrame({"subscription_id": [f"{prefix}S{i:05d}" for i in c["i"]], "customer_id": [f"{prefix}C{i:05d}" for i in c["i"]],
                                 "plan_id": plan, "mrr_cents": c["mrr_cents"].to_numpy(), "billing_interval_months": 3,
                                 "status": np.where(ended, "canceled", "active"),
                                 "start_date": mstart.to_numpy()[np.minimum(c["start"].to_numpy(), n_m - 1)],
                                 "canceled_at": pd.Series(mstart.to_numpy()[np.minimum(c["end"].to_numpy(), n_m - 1)]).where(ended),
                                 "cancel_reason": c["cancel_reason"].to_numpy() if "cancel_reason" in c else np.full(len(c), None, dtype=object)})

        customers = cust_frame(a_cust, "A")
        subs = sub_frame(a_cust, "A", np.where(a_cust["bundle"], "bundle", np.where(a_cust["mrr"] > a_cust["mrr"].median(), "pro", "core")))
        if "cancel_reason" not in a_cust:
            subs["cancel_reason"] = np.where(subs["status"] == "canceled", np.array(("budget", "switched_competitor", "no_longer_needed"))[g.integers(0, 3, len(subs))], None)
        t_customers = cust_frame(t_cust, "T")
        t_subs = sub_frame(t_cust, "T", "recurring")
        t_subs["plan_id"] = np.where(t_cust["mrr"] > t_cust["mrr"].median(), "recurring_plus", "recurring")
        t_subs["recurring_flag"] = True
        invoices = pd.DataFrame({"invoice_id": a_inv["invoice_id"], "customer_id": [f"AC{i:05d}" for i in a_inv["i"]], "subscription_id": [f"AS{i:05d}" for i in a_inv["i"]],
                                 "invoice_date": a_inv["invoice_date"], "due_date": a_inv["invoice_date"] + pd.Timedelta(days=30),
                                 "period_start": mstart.to_numpy()[a_inv["m"].to_numpy()],
                                 "period_end": mstart.to_numpy()[np.minimum(a_inv["m"].to_numpy() + a_inv["months"].to_numpy(), n_m - 1)] - np.timedelta64(1, "D"),
                                 "amount_cents": a_inv["amount_cents"], "currency": "USD", "status": np.where(a_inv["paid_at"].notna(), "paid", "open"),
                                 "created_at": a_inv["invoice_date"] + pd.Timedelta(hours=2)})
        pay = a_inv[a_inv["paid_at"].notna()]
        payments = pd.DataFrame({"payment_id": [f"APY{j:06d}" for j in range(1, len(pay) + 1)], "invoice_id": pay["invoice_id"].to_numpy(),
                                 "customer_id": [f"AC{i:05d}" for i in pay["i"]], "amount_cents": pay["amount_cents"].to_numpy(), "paid_at": pay["paid_at"].to_numpy(),
                                 "method": np.array(("ach", "card", "wire"))[g.integers(0, 3, len(pay))]})
        t_invoices = pd.DataFrame({"invoice_id": t_inv["invoice_id"], "customer_id": [f"TC{i:05d}" for i in t_inv["i"]], "subscription_id": [f"TS{i:05d}" for i in t_inv["i"]],
                                   "invoice_date": t_inv["invoice_date"], "amount_cents": t_inv["amount_cents"], "months_billed": t_inv["months"],
                                   "status": np.where(t_inv["paid_at"].notna(), "paid", "open"), "paid_at": t_inv["paid_at"]})
        # invoice lines: subscription line (services for mislabelled customers) + occasional add-on line split from the total
        mis = t_cust["services_mislabelled"].to_numpy()[t_inv["i"].to_numpy()]
        add = g.random(len(t_inv)) < 0.25
        sub_amt = np.where(add, np.round(t_inv["amount_cents"].to_numpy() * 0.85), t_inv["amount_cents"].to_numpy()).astype(np.int64)
        lines = pd.concat([
            pd.DataFrame({"invoice_id": t_inv["invoice_id"], "description": np.where(mis, "Implementation services", "Platform subscription"),
                          "line_type": np.where(mis, "services", "subscription"), "amount_cents": sub_amt, "quantity": 1}),
            pd.DataFrame({"invoice_id": t_inv["invoice_id"][add], "description": "Additional seats", "line_type": "subscription",
                          "amount_cents": (t_inv["amount_cents"].to_numpy() - sub_amt)[add], "quantity": g.integers(1, 20, int(add.sum()))}),
        ], ignore_index=True).sort_values(["invoice_id", "line_type"], kind="stable").reset_index(drop=True)
        lines.insert(0, "invoice_line_id", [f"TL{j:06d}" for j in range(1, len(lines) + 1)])
        # opex by department: acquirer and target per quarter; target carries budget, synergy plan and actual
        tf = self._target_financials(state).set_index("period")
        rs = state.extras["realized_syn_m"]
        rows = []
        for qi, per in enumerate(cal.all_periods):
            ms_ = range(3 * qi, 3 * qi + 3)
            post = 3 * qi >= close_m
            q = (3 * qi - close_m) // 3 if post else -1
            st_opex = float(tf.loc[per, "standalone_opex_cents"])
            plan_syn = deal["cost_claimed"] / 4 * RAMP[min(q, 3)] if post else 0.0
            act_syn = sum(rs.get(m, 0) for m in ms_)
            integ = sum(state.extras["integ_m"].get(m, 0) for m in ms_)
            for dname, w in DEPT_SPLIT.items():
                rows.append({"period": per, "entity": "target", "department": dname, "standalone_budget_cents": cents(st_opex * w),
                             "synergy_plan_cents": cents(plan_syn * w), "actual_cents": cents((st_opex - act_syn) * w),
                             "integration_cost_cents": cents(integ) if dname == "general_admin" else 0})
        opex = pd.DataFrame(rows)
        # deal artifacts
        sa = deal["standalone"]
        assumptions = pd.DataFrame([
            ("close_date", str(state.extras["close_date"])), ("wacc", f"{p['wacc']:.6f}"), ("terminal_growth", f"{p['terminal_growth']:.6f}"),
            ("tax_rate", f"{p['tax_rate']:.6f}"), ("target_growth_year1", f"{p['target_growth']:.6f}"), ("long_run_fcf_margin", f"{LONG_RUN_FCF_MARGIN:.6f}"),
            ("contribution_margin_on_revenue_synergies", f"{CONTRIBUTION_MARGIN:.6f}"), ("cost_synergy_claimed_pct_of_target_opex", f"{p['cost_synergy_claimed_pct']:.6f}"),
            ("revenue_synergy_claimed_pct_of_target_revenue", f"{p['revenue_synergy_claimed_pct']:.6f}"),
            ("integration_cost_plan_cents", str(int(deal["integ_plan"]))), ("synergy_ramp_quarters", "0.25,0.50,0.75,1.00"),
            ("purchase_price_cents", str(deal["price"])), ("standalone_value_cents", str(int(sa["value"]))),
            ("target_ttm_revenue_at_close_cents", str(int(deal["rev0"]))), ("target_ttm_opex_at_close_cents", str(int(deal["opex0"]))),
            ("target_stated_arr_cents", str(int(round(t_cust.loc[(t_cust["start"] < close_m) & (t_cust["end"] > close_m) | t_cust.index.isin(state.extras["churned"]), "mrr_cents"].sum() * 12)))),
        ], columns=["key", "value"])
        dm_rows = []
        for y in sa["years"]:
            dm_rows.append({"year": y["year"], "target_revenue_cents": int(y["revenue"]), "fcf_margin": round(y["fcf_margin"], 6), "standalone_fcf_cents": int(y["fcf"]),
                            "cost_synergy_plan_cents": int(deal["cost_claimed"] * (sum(RAMP) / 4 if y["year"] == 1 else 1)),
                            "revenue_synergy_plan_cents": int(deal["rev_claimed"] * (sum(RAMP) / 4 if y["year"] == 1 else 1)),
                            "integration_cost_plan_cents": int(deal["integ_plan"]) if y["year"] == 1 else 0})
        deal_model = pd.DataFrame(dm_rows)
        pa = state.extras["ppa"]
        ppa = pd.DataFrame([("purchase_price", pa["price"]), ("acquired_receivables", pa["ar"]), ("acquired_ppe", pa["ppe"]),
                            ("assumed_contract_liabilities", -pa["deferred_revenue"]), ("customer_relationships_7y", pa["customer_relationships"]),
                            ("developed_technology_5y", pa["developed_technology"]), ("goodwill", pa["goodwill"]), ("cash_paid", pa["cash_paid"]),
                            ("acquisition_debt", pa["acquisition_debt"])], columns=["line", "amount_cents"])
        dirt_plan = [
            ("customers", "case_mixed:billing_email"), ("target_customers", "whitespace:billing_email"),
            ("payments", "tz_mixed:paid_at"), ("target_subscriptions", "nulls:plan_id:0.04"),
            ("subscriptions", "case_mixed:plan_id"), ("target_invoices", "nulls:status:0.05"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        money = {c: "BIGINT" for c in ("mrr_cents", "amount_cents", "standalone_budget_cents", "synergy_plan_cents", "actual_cents", "integration_cost_cents",
                                        "target_revenue_cents", "standalone_fcf_cents", "cost_synergy_plan_cents", "revenue_synergy_plan_cents", "integration_cost_plan_cents",
                                        "revenue_cents", "cogs_cents", "standalone_opex_cents")}
        builder.add_frame("customers", customers, primary_key=["customer_id"], foreign_keys=[], dirt=dirt.get("customers", ()), description="Acquirer customers.")
        builder.add_frame("subscriptions", subs, primary_key=["subscription_id"], foreign_keys=[("customer_id", "customers", "customer_id")], column_types=money,
                          dirt=dirt.get("subscriptions", ()), descriptions={"plan_id": "core | pro | bundle (cross-sold to acquired customers)"})
        builder.add_frame("invoices", invoices, primary_key=["invoice_id"], foreign_keys=[("customer_id", "customers", "customer_id"), ("subscription_id", "subscriptions", "subscription_id")],
                          column_types={**money, "invoice_date": "DATE", "due_date": "DATE", "period_start": "DATE", "period_end": "DATE"},
                          description="Acquirer invoices (quarterly in advance).")
        builder.add_frame("payments", payments, primary_key=["payment_id"], foreign_keys=[("invoice_id", "invoices", "invoice_id")], column_types=money, dirt=dirt.get("payments", ()))
        builder.add_frame("target_customers", t_customers, primary_key=["customer_id"], foreign_keys=[], dirt=dirt.get("target_customers", ()), description="Acquired company's customers.")
        builder.add_frame("target_subscriptions", t_subs, primary_key=["subscription_id"], foreign_keys=[("customer_id", "target_customers", "customer_id")], column_types=money,
                          dirt=dirt.get("target_subscriptions", ()), descriptions={"recurring_flag": "counted as recurring (ARR) in the target's metrics"})
        builder.add_frame("target_invoices", t_invoices, primary_key=["invoice_id"], foreign_keys=[("customer_id", "target_customers", "customer_id")],
                          column_types={**money, "invoice_date": "DATE", "months_billed": "INTEGER"}, dirt=dirt.get("target_invoices", ()),
                          descriptions={"paid_at": "payment date (NULL = open)"})
        builder.add_frame("target_invoice_lines", lines, primary_key=["invoice_line_id"], foreign_keys=[("invoice_id", "target_invoices", "invoice_id")], column_types=money,
                          descriptions={"line_type": "subscription | services"})
        builder.add_frame("target_financials", tf.reset_index(), primary_key=["period"], foreign_keys=[], column_types=money,
                          description="Target quarterly P&L from its own books (standalone cost base).")
        builder.add_frame("opex_by_dept", opex, primary_key=["period", "entity", "department"], foreign_keys=[], column_types=money,
                          description="Target opex by department: standalone budget, deal-model synergy plan, actual and integration cost.")
        builder.add_frame("deal_assumptions", assumptions, primary_key=["key"], foreign_keys=[], description="Management's deal model inputs (board pack).")
        builder.add_frame("deal_model", deal_model, primary_key=["year"], foreign_keys=[], column_types=money, description="Deal model: standalone FCF and synergy plan by year.")
        builder.add_frame("purchase_price_allocation", ppa, primary_key=["line"], foreign_keys=[], column_types=money)

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR and deferred revenue (acquirer + target from close) tie to the GL; goodwill to the PPA (all hold)."""
        cal = state.calendar
        a_inv, t_inv, a_sch, t_sch = (state.extras[k] for k in ("a_inv", "t_inv", "a_sch", "t_sch"))
        close_m = state.extras["close_m"]
        close_ts = pd.Timestamp(state.extras["close_date"]) + EOD
        n_w = len(cal.warmup_periods)
        ar, dr, gw = {}, {}, {}
        for i, per in enumerate(state.periods):
            e_ts = pd.Timestamp(cal.bounds(per)[1]) + EOD
            m_end = 3 * (n_w + i + 1)
            a_open = a_inv[(a_inv["invoice_date"] <= e_ts) & (a_inv["paid_at"].isna() | (a_inv["paid_at"] > e_ts))]["amount_cents"].sum()
            if e_ts >= close_ts:
                t_open = t_inv[(t_inv["invoice_date"] <= e_ts) & (t_inv["paid_at"].isna() | (t_inv["paid_at"] > e_ts))]["amount_cents"].sum()
            else:
                t_open = 0
            ar[per] = int(a_open + t_open)
            a_issued = np.flatnonzero(a_inv["m"].to_numpy() < m_end)
            d_a = a_sch[a_sch["row"].isin(a_issued) & (a_sch["m"] >= m_end)]["cents"].sum()
            d_t = 0
            if m_end >= close_m:  # the purchase accounting entry is dated on the last day of P6
                t_issued = np.flatnonzero(t_inv["m"].to_numpy() < m_end)
                d_t = t_sch[t_sch["row"].isin(t_issued) & (t_sch["m"] >= m_end)]["cents"].sum()
            dr[per] = int(d_a + d_t)
            gw[per] = int(state.extras["ppa"]["goodwill"]) if e_ts >= close_ts else 0
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Open acquirer invoices + open target invoices (from close) == GL 1100"),
            SubledgerTie("deferred_revenue_schedule", dr, ["2200"], description="Unrecognised invoice months (acquirer + target from close) == GL 2200"),
            SubledgerTie("goodwill_ppa", gw, ["1700"], description="Goodwill per the purchase price allocation == GL 1700"),
        ]

    # ------------------------------------------------------------------ facts

    def _facts(self, state: WorldState) -> dict[str, Any]:
        """Deal economics and realised post-close data (cached)."""
        if "facts" in state.extras:
            return state.extras["facts"]
        p, cal = state.params, state.calendar
        P = state.periods
        deal = self._deal(state)
        t_cust = state.extras["t_cust"]
        tf = self._target_financials(state).set_index("period")
        tax, wacc, gt = p["tax_rate"], p["wacc"], p["terminal_growth"]
        churned = state.extras["churned"]
        churn_arr = float(t_cust.loc[churned, "mrr_cents"].sum() * 12)
        ach_rev = state.extras["ach_rev_rr"] * 100
        pv_ach = synergy_pv(deal["cost_ach"], ach_rev, deal["integ_actual"], tax, wacc, gt, dis_rr=churn_arr * CONTRIBUTION_MARGIN)
        standalone = deal["standalone"]["value"]
        price = deal["price"]
        # realised cost synergy run-rate at P8 vs plan for P8
        rs = state.extras["realized_syn_m"]
        n_w = len(cal.warmup_periods)
        m8 = range(3 * (n_w + 7), 3 * (n_w + 8))
        plan8 = deal["cost_claimed"] / 4 * RAMP[1]
        act8 = sum(rs.get(m, 0) for m in m8)
        # impairment test at P8: implied fair value = DCF on the P8 TTM revenue (after churn) + PV of achievable synergies (remaining)
        ttm8 = float(tf.loc[P[4:8], "revenue_cents"].sum())
        opex8 = float(tf.loc[P[4:8], "standalone_opex_cents"].sum())
        m8 = float((ttm8 - tf.loc[P[4:8], "cogs_cents"].sum() - opex8) / ttm8)
        h_pre = float(tf.loc[P[4:6], "revenue_cents"].sum())
        h_post = float(tf.loc[P[6:8], "revenue_cents"].sum())
        g_real = float(np.clip((h_post / h_pre) ** 2 - 1, -0.2, p["target_growth"])) if h_pre else 0.0  # post-close half vs pre-close half, annualised
        g_test = float(np.clip(g_real, gt, p["target_growth"]))  # projections re-based on the realised post-close trajectory
        fv_stand = dcf_value(ttm8, g_test, gt, m8, LONG_RUN_FCF_MARGIN, tax, wacc)["value"]
        fv = fv_stand + synergy_pv(deal["cost_ach"], ach_rev, deal["integ_actual"] * 0.4, tax, wacc, gt, dis_rr=churn_arr * CONTRIBUTION_MARGIN)
        tb8 = state.ledger.trial_balance(P[-1])
        pa = state.extras["ppa"]
        carrying = tb8["1700"] + tb8["1710"] + tb8["1720"] + pa["ar"] + pa["ppe"] - pa["deferred_revenue"]
        impairment = float(np.clip(carrying - fv, 0, tb8["1700"]))
        breakeven = (price - standalone + deal["integ_plan"] * (1 - tax) / (1 + wacc)) / deal["pv_claimed_gross"]
        f = {"standalone": standalone, "pv_claimed": deal["pv_claimed"], "pv_ach": pv_ach, "npv_claimed": standalone + deal["pv_claimed"] - price,
             "npv_ach": standalone + pv_ach - price, "realized_pct_plan": act8 / plan8 if plan8 else 0.0, "churn_arr": churn_arr,
             "goodwill": float(tb8["1700"]), "impairment": impairment, "implied_fv": fv, "carrying": float(carrying), "breakeven": breakeven,
             "premium_pct": deal["premium_pct"], "price": price, "act8": act8, "plan8": plan8, "g_real": g_real, "g_test": g_test}
        if p["rev_quality_issue"]:
            close_m = state.extras["close_m"]
            alive = (t_cust["start"] < close_m) & ((t_cust["end"] > close_m) | t_cust.index.isin(churned))
            stated = float(t_cust.loc[alive, "mrr_cents"].sum() * 12)
            f["arr_overstated"] = float(t_cust.loc[alive & t_cust["services_mislabelled"], "mrr_cents"].sum() * 12) / stated
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """SaaS unit economics, cohorts omitted; KPIs by period incl. target ARR and synergy realisation."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        tf = self._target_financials(state).set_index("period")
        kpis = {}
        for per in state.periods:
            inc = next(i for i in state.statements.income if i.period == per)  # type: ignore[union-attr]
            kpis[per] = {"revenue_cents": float(inc.revenue_cents), "operating_income_cents": float(inc.operating_income_cents),
                         "net_income_cents": float(inc.net_income_cents), "target_revenue_cents": float(tf.loc[per, "revenue_cents"]),
                         "cash_cents": float(state.ledger.trial_balance(per)["1000"])}
        a = state.extras["a_cust"]
        unit = {"arpa": float(a["mrr_cents"].mean()), "churn_monthly": p["churn_monthly"], "gross_margin": p["gm"],
                "ltv": float(a["mrr_cents"].mean() * p["gm"] / p["churn_monthly"]), "cac": 0.0, "payback_months": 0.0}
        return build_operations_state(unit_economics=unit, supply_chain={},
                                      pricing={"synergy_ramp": list(RAMP), "contribution_margin": CONTRIBUTION_MARGIN, "realized_cost_synergy_pct_of_plan_p8": f["realized_pct_plan"]},
                                      kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 3 key table (DCF, synergy PVs, realised shortfall, overlap churn, goodwill test) and reference SQL."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        P = state.periods
        metrics = [
            metric("standalone_target_dcf_cents", f["standalone"], "cents",
                   "Target TTM revenue at close (target_financials P3-P6) grown at target_growth_year1 declining linearly to terminal growth by year 5; FCF margin "
                   "from the TTM operating margin to the long-run margin by year 5; FCF after tax; end-year discounting at WACC + Gordon terminal value (deal_assumptions).",
                   pillar="accounting_finance", tol_rel=0.06, label="Standalone DCF of the target"),
            metric("pv_claimed_synergies_cents", f["pv_claimed"], "cents",
                   "Claimed cost run-rate (% of target TTM opex) + revenue run-rate x 70% contribution, ramp 25/50/75/100% (year 1 = 62.5%), after tax, 5 years + terminal; "
                   "less the planned integration cost (after tax, year 1).", pillar="accounting_finance", tol_rel=0.06, label="PV of claimed synergies"),
            metric("pv_achievable_synergies_cents", f["pv_ach"], "cents",
                   "Same with the realised run-rates (cost synergies implied by opex_by_dept, cross-sell bundle ARR) minus the dis-synergy (cancelled overlap ARR x 70%) "
                   "and the actual integration cost.", pillar="accounting_finance", tol_rel=0.08, label="PV of achievable synergies"),
            metric("deal_npv_claimed_cents", f["npv_claimed"], "cents", "Standalone DCF + PV claimed synergies - purchase price.", pillar="accounting_finance", tol_rel=0.10,
                   label="Deal NPV at claimed synergies"),
            metric("deal_npv_achievable_cents", f["npv_ach"], "cents", "Standalone DCF + PV achievable synergies - purchase price.", pillar="accounting_finance", tol_rel=0.10,
                   label="Deal NPV at achievable synergies"),
            metric("realized_cost_synergy_run_rate_p8_pct_of_plan", f["realized_pct_plan"], "ratio",
                   f"{P[-1]}: sum over departments of (standalone_budget - actual) / synergy_plan in opex_by_dept.", pillar="operations_strategy", tol_abs=0.08,
                   label="Realised cost synergies vs plan, latest quarter"),
            metric("overlap_churn_arr_cents", f["churn_arr"], "cents", "Sum of mrr_cents x 12 of target subscriptions cancelled with cancel_reason 'consolidated vendor'.",
                   pillar="sql", tol_rel=0.05, label="ARR lost to overlap churn"),
            metric("goodwill_cents", f["goodwill"], "cents", "GL 1700 = purchase price - fair value of acquired net assets and identified intangibles (purchase_price_allocation).",
                   pillar="accounting_finance", tol_abs=1.0, label="Goodwill"),
            metric("goodwill_impairment_cents", f["impairment"], "cents",
                   f"max(0, carrying amount of the acquired unit at {P[-1]} (goodwill + intangibles net + acquired net tangible assets) - implied fair value), "
                   f"capped at goodwill. Implied fair value = DCF on the target's TTM revenue at {P[-1]} with year-1 growth re-based on the realised "
                   f"post-close run-rate ({P[6]}-{P[7]} vs {P[4]}-{P[5]} revenue, annualised: {f['g_real']:.1%}, floored at terminal growth, capped at the plan's {p['target_growth']:.1%}) "
                   "+ PV of achievable synergies (remaining integration cost).",
                   pillar="accounting_finance", tol_rel=0.15,
                   label="Goodwill impairment exposure"),
            metric("breakeven_synergy_pct", f["breakeven"], "ratio", "Share of the claimed (gross) synergy PV at which the deal NPV is zero, given the planned integration cost.",
                   pillar="accounting_finance", tol_abs=0.02, label="Break-even synergy realisation"),
        ]
        if p["rev_quality_issue"]:
            metrics.append(metric("target_arr_overstated_pct", f["arr_overstated"], "ratio",
                                  "MRR x 12 of target subscriptions whose invoice lines are line_type 'services' / stated ARR at close.", pillar="sql", tol_abs=0.02,
                                  label="Target ARR overstated by services"))
        sql_overlap = """
WITH a AS (SELECT customer_id, lower(trim(split_part(billing_email, '@', 2))) AS domain FROM customers),
t AS (SELECT customer_id, lower(trim(split_part(billing_email, '@', 2))) AS domain FROM target_customers)
SELECT COUNT(DISTINCT t.customer_id) AS overlap_customers, SUM(ts.mrr_cents) * 12 AS overlap_arr_cents,
       SUM(CASE WHEN ts.cancel_reason = 'consolidated vendor' THEN ts.mrr_cents ELSE 0 END) * 12 AS consolidated_arr_cents
FROM t JOIN a USING (domain) JOIN target_subscriptions ts ON ts.customer_id = t.customer_id"""
        q_ov = run_reference_query(con, "overlap_customers_by_domain", sql_overlap, signature=["with", "join", "case when", "distinct", "sum("],
                                   metric_keys=["overlap_churn_arr_cents"], checks=lambda df: {"overlap_customers": int(df["overlap_customers"].iloc[0]),
                                                                                               "consolidated_arr_cents": int(df["consolidated_arr_cents"].iloc[0] or 0)})
        sql_cancel = """
SELECT period_of(canceled_at) AS period, COALESCE(cancel_reason, 'unknown') AS cancel_reason, COUNT(*) AS cancellations, SUM(mrr_cents) * 12 AS arr_cents
FROM target_subscriptions WHERE canceled_at IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 4 DESC"""
        q_cancel = run_reference_query(con, "target_cancellations_by_reason", sql_cancel, signature=["group by", "coalesce(", "count(", "sum("],
                                       metric_keys=["overlap_churn_arr_cents"],
                                       checks=lambda df: {"consolidated_arr_cents": int(df.loc[df["cancel_reason"] == "consolidated vendor", "arr_cents"].sum())})
        sql_syn = """
WITH q AS (
  SELECT period, SUM(standalone_budget_cents) AS budget, SUM(synergy_plan_cents) AS synergy_plan, SUM(actual_cents) AS actual,
         SUM(integration_cost_cents) AS integration
  FROM opex_by_dept WHERE entity = 'target' GROUP BY 1)
SELECT period, budget, synergy_plan, budget - actual AS realized_synergy,
       (budget - actual) * 1.0 / NULLIF(synergy_plan, 0) AS realized_pct_of_plan, integration,
       SUM(integration) OVER (ORDER BY period) AS cumulative_integration
FROM q ORDER BY period"""
        q_syn = run_reference_query(con, "synergy_plan_vs_actual_by_quarter", sql_syn, signature=["over(", "with", "group by", "sum("],
                                    metric_keys=["realized_cost_synergy_run_rate_p8_pct_of_plan", "pv_achievable_synergies_cents"],
                                    checks=lambda df: {"realized_pct_last": float(df["realized_pct_of_plan"].dropna().iloc[-1])})
        sql_integ = """
SELECT j.period, SUM(l.debit_cents) AS integration_cost_cents
FROM gl_lines l JOIN gl_journal j USING (entry_id)
WHERE j.source = 'integration' AND l.account_code = '6200' GROUP BY 1 ORDER BY 1"""
        q_integ = run_reference_query(con, "integration_costs_in_gl", sql_integ, signature=["join", "group by", "sum("], metric_keys=["pv_achievable_synergies_cents"],
                                      checks=lambda df: {"total_integration_cents": int(df["integration_cost_cents"].sum())})
        sql_gw = """
SELECT l.account_code, a.account_name, SUM(l.debit_cents - l.credit_cents) AS balance_cents
FROM gl_lines l JOIN gl_accounts a USING (account_code)
WHERE l.account_code IN ('1700', '1710', '1720') GROUP BY 1, 2 ORDER BY 1"""
        q_gw = run_reference_query(con, "goodwill_and_intangibles_balances", sql_gw, signature=["join", "group by", "sum("], metric_keys=["goodwill_cents", "goodwill_impairment_cents"],
                                   checks=lambda df: {"goodwill_cents": int(df.loc[df["account_code"] == "1700", "balance_cents"].sum())})
        sql_ppa = """
SELECT line, amount_cents, amount_cents * 1.0 / MAX(CASE WHEN line = 'purchase_price' THEN amount_cents END) OVER () AS pct_of_price
FROM purchase_price_allocation ORDER BY amount_cents DESC"""
        q_ppa = run_reference_query(con, "purchase_price_allocation_shares", sql_ppa, signature=["over(", "case when"], metric_keys=["goodwill_cents"],
                                    checks=lambda df: {"goodwill_pct_of_price": float(df.set_index("line").loc["goodwill", "pct_of_price"])})
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_syn, q_integ],
            "magnitude": {**anomalies["A1"].magnitude, "realized_cost_synergy_run_rate_p8_pct_of_plan": round(f["realized_pct_plan"], 6)},
            "evidence_trail": [f"opex_by_dept: realised cost synergies in {P[-1]} are {f['realized_pct_plan']:.0%} of the deal-model plan.",
                               "Integration costs in 6200 exceed the deal model's one-time budget and keep recurring."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_ov, q_cancel],
            "magnitude": {**anomalies["A2"].magnitude, "overlap_churn_arr_cents": float(f["churn_arr"])},
            "evidence_trail": [f"{q_ov.expected['checks']['overlap_customers']} target customers share a normalised e-mail domain with an acquirer customer.",
                               f"Cancellations after close with reason 'consolidated vendor' remove {f['churn_arr'] / 100:,.0f} USD of ARR."]})
        gw_id = next(k for k, a in anomalies.items() if a.kind == "goodwill_impairment_risk")
        anomalies[gw_id] = anomalies[gw_id].model_copy(update={
            "reference_sql": [q_gw, q_ppa],
            "magnitude": {"goodwill_cents": float(f["goodwill"]), "goodwill_impairment_cents": round(f["impairment"], 2), "implied_fair_value_cents": round(f["implied_fv"], 2),
                          "premium_pct_of_standalone": round(f["premium_pct"], 6)},
            "evidence_trail": [f"Goodwill {f['goodwill'] / 100:,.0f} USD is {f['goodwill'] / f['price']:.0%} of the price; premium paid = {f['premium_pct']:.0%} of standalone value.",
                               f"Implied fair value at achievable synergies {f['implied_fv'] / 100:,.0f} USD vs carrying {f['carrying'] / 100:,.0f} USD."]})
        rq_id = next((k for k, a in anomalies.items() if a.kind == "revenue_quality_mislabel"), None)
        if rq_id:
            sql_svc = """
WITH l AS (
  SELECT i.subscription_id, MAX(CASE WHEN tl.line_type = 'services' THEN 1 ELSE 0 END) AS has_services
  FROM target_invoice_lines tl JOIN target_invoices i USING (invoice_id) GROUP BY 1)
SELECT l.has_services, COUNT(*) AS subscriptions, SUM(s.mrr_cents) * 12 AS arr_cents
FROM target_subscriptions s JOIN l USING (subscription_id) GROUP BY 1 ORDER BY 1"""
            q_svc = run_reference_query(con, "target_arr_by_line_type", sql_svc, signature=["with", "join", "case when", "group by"], metric_keys=["target_arr_overstated_pct"],
                                        checks=lambda df: {"services_arr_cents": int(df.loc[df["has_services"] == 1, "arr_cents"].sum())})
            sql_svc2 = """
SELECT line_type, period_of(i.invoice_date) AS period, COUNT(*) AS lines, SUM(tl.amount_cents) AS amount_cents
FROM target_invoice_lines tl JOIN target_invoices i USING (invoice_id) GROUP BY 1, 2 ORDER BY 2, 1"""
            q_svc2 = run_reference_query(con, "target_revenue_by_line_type_and_quarter", sql_svc2, signature=["join", "group by", "count(", "sum("],
                                         metric_keys=["target_arr_overstated_pct"])
            anomalies[rq_id] = anomalies[rq_id].model_copy(update={
                "reference_sql": [q_svc, q_svc2], "magnitude": {**anomalies["A3"].magnitude, "target_arr_overstated_pct": round(f["arr_overstated"], 6)},
                "evidence_trail": ["Some 'recurring' target subscriptions only ever invoice line_type='services' and stop after a few invoices.",
                                   f"They are {f['arr_overstated']:.1%} of the ARR the deal model capitalised."]})
        ordered = [anomalies[k] for k in sorted(anomalies, key=lambda s: int(s[1:]))]
        herrings_all = [
            "Integration costs are one-time; strip them out and we're on plan - they are cash, run above budget, and even excluding them the run-rate synergy is short.",
            "Target growth slowed because of macro - the cancellations carry an explicit 'consolidated vendor' reason and sit in overlapping accounts.",
            "Goodwill is fine because the auditors signed off last quarter - impairment testing is annual or triggered, and the shortfall is a trigger now.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        root = (f"The purchase premium ({f['premium_pct']:.0%} of a {f['standalone'] / 100:,.0f} USD standalone value) is essentially the PV of the claimed "
                f"synergies ({f['pv_claimed'] / 100:,.0f} USD): management paid the seller for synergies it still had to deliver. Realised cost synergies run at "
                f"{f['realized_pct_plan']:.0%} of plan, cross-sell lands at {p['revenue_synergy_achievable_mult']:.0%} of the claim, overlapping customers cancelled "
                f"{f['churn_arr'] / 100:,.0f} USD of ARR (dis-synergy) and integration costs overran. At achievable synergies the PV is {f['pv_ach'] / 100:,.0f} USD "
                f"and deal NPV {f['npv_ach'] / 100:,.0f} USD (vs {f['npv_claimed'] / 100:,.0f} at claimed); the break-even realisation is {f['breakeven']:.0%}. "
                f"Goodwill of {f['goodwill'] / 100:,.0f} USD is exposed to an impairment of ~{f['impairment'] / 100:,.0f} USD that has not been recorded."
                + (f" The target's ARR was overstated by {f['arr_overstated']:.1%} (services counted as recurring)." if p["rev_quality_issue"] else ""))
        metrics = _with_hints(metrics, _HINTS)
        return GroundTruth(
            root_cause=root, anomalies=ordered, red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=list(dict.fromkeys(q.sql for a in ordered for q in a.reference_sql)),
                python=["import numpy as np, pandas as pd\n"
                        "a = dict(pd.read_sql('SELECT key, value FROM deal_assumptions', con).values)\n"
                        "# DCF: 5 years, growth from target_growth_year1 to terminal, margin to long_run_fcf_margin, Gordon terminal at WACC\n"
                        "# sensitivity grid: WACC x synergy realisation; Monte Carlo on achievable % (triangular 0.55/0.68/0.80 cost, 0.20/0.30/0.45 revenue)\n"
                        "# impairment test: carrying (goodwill + intangibles + net assets) vs implied fair value at achievable synergies\n"],
                memo_bullets=[
                    "The premium equals PV(claimed synergies): the buyer paid the seller for value the buyer must create.",
                    f"Realised cost synergies {f['realized_pct_plan']:.0%} of plan; milestone counts are activity, not dollars.",
                    "Dis-synergy is already in the data: overlapping customers consolidate vendors.",
                    f"At achievable synergies the deal NPV is negative; break-even needs {f['breakeven']:.0%} of the claimed synergies.",
                    "A goodwill impairment trigger exists now; record it rather than defer - credibility and covenants depend on it.",
                    "Protect what is left: retention offers for overlap accounts, integration spend where it accelerates the cost ramp.",
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
        names = rng.pick_names(4)
        reveal = [
            f"The acquisition closed at the end of {P[5]} for {f['price'] / 1e8:,.1f}M USD.",
            f"The deal announcement promised cost synergies of {p['cost_synergy_claimed_pct']:.0%} of the target's opex and revenue synergies of "
            f"{p['revenue_synergy_claimed_pct']:.0%} of its revenue.",
            "The integration office reports the program 'on track'.",
            "Acquired intangibles are being amortised from the first post-close quarter.",
            f"Consolidated results for {P[6]} and {P[7]} are in the data room.",
        ]
        conceal = ["achievable synergies multipliers", "customer overlap percentage", "dis-synergy churn", "the goodwill impairment math",
                   "premium equals the claimed synergies", "services mislabelled as recurring", *[a.id for a in state.anomalies]]
        stakeholders = [
            Stakeholder(name=names[0], role="CEO", agenda="Defend the deal thesis.", disposition="confident",
                        knows=["the strategic rationale"], hides=["the premium was set at the full claimed synergy value"]),
            Stakeholder(name=names[1], role="CFO", agenda="Avoid an impairment this year.", disposition="guarded",
                        knows=["the impairment test date"], hides=["the triggering event has occurred"]),
            Stakeholder(name=names[2], role="Head of Integration", agenda="Report milestones.", disposition="upbeat",
                        knows=["the cost ramp is slipping"], hides=["integration costs are over budget"]),
            Stakeholder(name=names[3], role="GM (target's former CEO)", agenda="Earn the earn-out.", disposition="evasive",
                        knows=["overlapping customers are consolidating vendors"], hides=["some recurring revenue was implementation work"] if p["rev_quality_issue"] else []),
        ]
        tl = [
            TimelineEvent(period=P[4], headline="LOI signed", detail="Exclusivity with the target."),
            TimelineEvent(period=P[5], headline=f"Deal closes; goodwill {f['goodwill'] / 1e8:,.1f}M USD", detail="Purchase price allocation completed."),
            TimelineEvent(period=P[6], headline="Integration PMO: 90% of milestones hit", detail="Milestone tracker shared with the board.", is_red_herring=True),
            TimelineEvent(period=P[7], headline="Three overlap customers consolidate vendors", detail="Customer success escalation."),
            TimelineEvent(period=P[7], headline="Board asks for a synergy tracker", detail="Audit committee requests dollars, not milestones."),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[6], headline="Macro softness commentary", detail="Peers cite longer sales cycles.", is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[6], headline="Auditors complete the quarterly review", detail="No issues raised on the acquisition accounting.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["deal_model_summary", "board_announcement", "integration_pmo_dashboard_excerpt", "target_customer_list_export"],
                           architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Spec levers; ``price_harmonization`` is coded -1 / 0 / +1; ``integration_spend_cents`` is a multiple of plan."""
        f = self._facts(state)
        return [
            Lever(key="integration_spend_cents", label="Integration spend (multiple of plan)", min=0, max=2, default=1, unit="x plan",
                  effect="Accelerates the cost-synergy ramp: ramp' = min(1, ramp + 0.15 x spend/plan)."),
            Lever(key="retention_program_pct_discount", label="Retention discount for overlap accounts", min=0, max=20, default=0, unit="%",
                  effect="Cuts overlap churn by 0.6 x d/20; costs d% of overlap ARR."),
            Lever(key="price_harmonization", label="Price harmonisation (-1 down, 0 none, +1 up)", min=-1, max=1, default=0, unit="code",
                  effect="+-3% ARPA; churn responds."),
            Lever(key="record_impairment", label="Record the goodwill impairment (0/1)", min=0, max=1, default=0, unit="bool",
                  effect=f"Non-cash charge of ~{f['impairment'] / 100:,.0f} USD; equity and covenant headroom down; credibility up."),
            Lever(key="divest_target_unit_pct", label="Divest part of the acquired unit", min=0, max=100, default=0, unit="%",
                  effect="Cash at 0.7x standalone value; removes the synergies of the divested share."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model linearised at P8 (module docstring)."""
        p = state.params
        f = self._facts(state)
        deal = self._deal(state)
        P = state.periods
        tb8 = state.ledger.trial_balance(P[-1])
        bs8 = next(b for b in state.statements.balance if b.period == P[-1])  # type: ignore[union-attr]
        inc8 = next(i for i in state.statements.income if i.period == P[-1])  # type: ignore[union-attr]
        t_arr = float(self._target_financials(state).set_index("period").loc[P[-1], "revenue_cents"] * 4)
        ov_arr = float(state.extras["t_cust"].loc[state.extras["overlap_idx"], "mrr_cents"].sum() * 12)
        kpis = {
            "realized_cost_synergy_run_rate_cents": float(f["act8"] * 4), "synergy_realization_pct": float(f["realized_pct_plan"]),
            "overlap_churn_arr_cents": float(f["churn_arr"]), "target_arr_cents": t_arr, "goodwill_cents": float(tb8["1700"]),
            "equity_cents": float(bs8.total_equity_cents), "cash_cents": float(tb8["1000"]), "operating_income_cents": float(inc8.operating_income_cents),
            "credibility_index": 0.5, "covenant_headroom_x": 1.0,
        }
        plan_q = deal["integ_plan"] / 4
        churn_q = f["churn_arr"] / 2
        effects = [
            {"lever": "integration_spend_cents", "kpi": "realized_cost_synergy_run_rate_cents", "delta_per_unit": 0.15 * deal["cost_claimed"], "lag_periods": 1},
            {"lever": "integration_spend_cents", "kpi": "synergy_realization_pct", "delta_per_unit": 0.15, "lag_periods": 1},
            {"lever": "integration_spend_cents", "kpi": "cash_cents", "delta_per_unit": -plan_q, "lag_periods": 0},
            {"lever": "integration_spend_cents", "kpi": "operating_income_cents", "delta_per_unit": -plan_q, "lag_periods": 0},
            {"lever": "retention_program_pct_discount", "kpi": "overlap_churn_arr_cents", "delta_per_unit": -0.6 * churn_q / 20, "lag_periods": 0},
            {"lever": "retention_program_pct_discount", "kpi": "target_arr_cents", "delta_per_unit": 0.6 * churn_q / 20 - ov_arr / 100, "lag_periods": 0},
            {"lever": "retention_program_pct_discount", "kpi": "operating_income_cents", "delta_per_unit": -ov_arr / 100 / 4, "lag_periods": 0},
            {"lever": "price_harmonization", "kpi": "target_arr_cents", "delta_per_unit": 0.03 * t_arr * (1 - 0.8), "lag_periods": 1},
            {"lever": "price_harmonization", "kpi": "overlap_churn_arr_cents", "delta_per_unit": 0.2 * churn_q, "lag_periods": 1},
            {"lever": "record_impairment", "kpi": "goodwill_cents", "delta_per_unit": -f["impairment"], "lag_periods": 0},
            {"lever": "record_impairment", "kpi": "equity_cents", "delta_per_unit": -f["impairment"], "lag_periods": 0},
            {"lever": "record_impairment", "kpi": "operating_income_cents", "delta_per_unit": -f["impairment"], "lag_periods": 0},
            {"lever": "record_impairment", "kpi": "credibility_index", "delta_per_unit": 0.25, "lag_periods": 0},
            {"lever": "record_impairment", "kpi": "covenant_headroom_x", "delta_per_unit": -0.2, "lag_periods": 0},
            {"lever": "divest_target_unit_pct", "kpi": "cash_cents", "delta_per_unit": 0.7 * f["standalone"] / 100, "lag_periods": 0},
            {"lever": "divest_target_unit_pct", "kpi": "target_arr_cents", "delta_per_unit": -t_arr / 100, "lag_periods": 0},
            {"lever": "divest_target_unit_pct", "kpi": "realized_cost_synergy_run_rate_cents", "delta_per_unit": -f["act8"] * 4 / 100, "lag_periods": 0},
            {"lever": "divest_target_unit_pct", "kpi": "goodwill_cents", "delta_per_unit": -float(tb8["1700"]) / 100, "lag_periods": 0},
        ]
        drag = {"overlap_churn_arr_cents": churn_q, "target_arr_cents": -churn_q, "credibility_index": -0.05,
                "synergy_realization_pct": 0.0 if f["realized_pct_plan"] >= 1 else 0.05}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"record_impairment": 1.0, "retention_program_pct_discount": 5.0},
                "bounds": {"synergy_realization_pct": [0.0, 1.5], "credibility_index": [0.0, 1.0], "goodwill_cents": [0.0, None]}}


def _with_hints(metrics: list[Any], hints: dict[str, str]) -> list[Any]:
    """Attach a learner-facing hint to every expected metric (``CasePublic.asks``); unknown keys get a generic hint."""
    out = []
    for m in metrics:
        h = m.hint or hints.get(m.key) or next((v for k, v in hints.items() if k.endswith("*") and m.key.startswith(k[:-1])), "")
        out.append(m.model_copy(update={"hint": h or f"Compute {m.label.lower()} from the case database ({m.unit})."}))
    return out

_HINTS: dict[str, str] = {'standalone_target_dcf_cents': 'Rebuild the DCF from deal_assumptions and target_financials (5 years + terminal value).', 'pv_claimed_synergies_cents': 'PV of the claimed cost and revenue synergies with the ramp, after tax, less planned integration cost.', 'pv_achievable_synergies_cents': 'Same PV using the realised run-rates, overlap churn and actual integration cost.', 'deal_npv_claimed_cents': 'Standalone value + PV of claimed synergies - price.', 'deal_npv_achievable_cents': 'Standalone value + PV of achievable synergies - price.', 'realized_cost_synergy_run_rate_p8_pct_of_plan': 'Latest quarter (budget - actual) / synergy plan in opex_by_dept.', 'overlap_churn_arr_cents': 'Annualise MRR of target subscriptions cancelled for vendor consolidation.', 'goodwill_cents': 'Read goodwill from the purchase price allocation or GL 1700.', 'goodwill_impairment_cents': "Compare the acquired unit's carrying amount with its implied fair value at achievable synergies.", 'breakeven_synergy_pct': 'Share of the claimed synergies at which deal NPV is zero.', 'target_arr_overstated_pct': "ARR on 'recurring' subscriptions whose invoices are services lines / stated ARR."}
