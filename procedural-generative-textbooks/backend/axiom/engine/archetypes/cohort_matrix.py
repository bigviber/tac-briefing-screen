"""``cohort_matrix`` - "Triangle" (archetypes.md section 13).

A subscription business (B2B SaaS seats, a subscription box, or seller subscriptions on a
marketplace) asks for a cohort retention triangle. The growth team's slide says "retention is
improving" - it compares how much of each cohort is still active *today*, which flatters young
cohorts (right-censoring), counts every subscription row as a customer (re-ingested duplicates,
plan changes and reactivations) and buckets signup months in mixed time zones. Built correctly, the
triangle shows one cohort collapsing: the P5 launch promo (50% off for three months) brought in
customers who churned at twice the normal rate and en masse when the discount ended.

Model
-----
* Contractual setting (all industries; spec "or" resolved to subscriptions for every industry).
  New customers per month grow with ``growth``; segment mix smb / midmarket; customer lifetime in
  paid months ~ shifted-beta-geometric per segment (``theta ~ Beta(a, b)``, ``T ~ Geom(theta)``),
  ``a/(a+b)`` anchored on ``saas.gross_churn_monthly_<segment>``.
* Promo cohort (one month in P5): 2-2.5x acquisition; promo customers draw from ``Beta(2a, b)`` (2x
  churn) and ``promo_cliff_share`` of the survivors leave when the discount ends after 3 months.
* Subscription rows: plan changes (gap 0) and failed-payment re-subscribes (gap 2-20 days) create a
  new row inside the same island; ``reactivation_rate`` of churned customers come back after 35-240
  days (new island, ``previous_subscription_id`` set). ``ended_at`` is the effective end of the last
  paid month; ``cancelled_at`` is when the cancellation was *recorded* - voluntary cancels are recorded
  in the last paid month, involuntary churn is synced 3-25 days late and the latest ones not at all
  (NULL), the "late-arriving cancellations" trap.
* Retention definition (used by the reference SQL, the Python reference and the ground truth): a
  customer's cohort is the UTC month of their first subscription start; the customer is active in
  calendar month M if some subscription covers the end of M (``started_at < first day of M+1`` and
  ``ended_at IS NULL OR ended_at >= first day of M+1``); retention(cohort, k) = active customers in
  month cohort+k / cohort size, for months up to the as-of month only (lower triangle).
* GL: card-paid monthly charges on each billing anniversary - ``Dr 1000 net (Dr 1100 for charges of the
  last two days of the month, settled T+2), Dr 5050 fee, Dr 4110 promo discount; Cr 4000 list price`` - in
  monthly batches; the unearned part of each month billed in advance is deferred to 2200 at quarter end
  (auto-reversing, source ``revenue_deferral``); hosting / fulfilment in 5000; usual close.
  Sub-ledger: charges generated from the subscription intervals == GL net revenue (holds; no designed
  break - the anomaly is analytical).

Simulation (reduced form, monthly cohorts rolled forward a quarter): ``promo_discount_pct`` buys
new customers at lower retention; ``promo_min_commitment_months`` lifts promo retention;
``winback_campaign`` adds reactivations at a cost; ``annual_plan_push_pct`` lowers churn.
``resolved_if``: a minimum commitment of >= 3 months on promos.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from axiom.engine import benchmark_keys as bk
from axiom.engine.archetypes import register
from axiom.engine.archetypes.base import (
    DEFERRAL_SOURCE,
    post_deferred_revenue_trueups,
    Archetype,
    BuildContext,
    WorldState,
    difficulty_pick,
    magnitude,
    metric,
    run_reference_query,
)
from axiom.engine.datagen import ROW_BUDGET, TableBuilder
from axiom.engine.finance import SubledgerTie, cents, cents_array, split_cents
from axiom.engine.market_research import BenchmarkPool
from axiom.engine.operations import build_operations_state, cohort_from_curve, sbg_fit
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

logger = logging.getLogger("axiom.archetypes.cohort_matrix")

FEE_RATE = 0.029
FEE_FIXED = 30
PROMO_CODE = "LAUNCH50"
STORY = {
    "saas": ("B2B SaaS for small and mid-sized teams, billed monthly per workspace", ("smb", "midmarket")),
    "ecommerce": ("Subscription box (monthly curated deliveries), billed on each anniversary", ("standard", "premium")),
    "fintech_marketplace": ("Marketplace selling monthly seller-pro subscriptions", ("starter", "pro")),
}


def _add_months(ts: pd.Series, n: np.ndarray) -> pd.Series:
    """``ts + n calendar months`` (day clipped to the target month's length), vectorised."""
    t = pd.to_datetime(ts)
    y = t.dt.year.to_numpy()
    m = t.dt.month.to_numpy() - 1 + np.asarray(n, dtype=np.int64)
    ty, tm = y + m // 12, m % 12 + 1
    first = pd.to_datetime(pd.DataFrame({"year": ty, "month": tm, "day": 1}))
    dim = first.dt.days_in_month.to_numpy()
    day = np.minimum(t.dt.day.to_numpy(), dim)
    tod = (t - t.dt.normalize()).to_numpy()
    return pd.Series(first.to_numpy() + pd.to_timedelta(day - 1, unit="D").to_numpy() + tod, index=ts.index)


def _midx(ts: pd.Series, start: dt.date) -> np.ndarray:
    t = pd.to_datetime(ts)
    return ((t.dt.year - start.year) * 12 + t.dt.month - start.month).to_numpy()


def _season(d: dt.date) -> str:
    """Northern-hemisphere season of a month (the practitioner / principal reveal names the season, not the month)."""
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(pd.Timestamp(d).month, "autumn")


def _utc(col: str, dirty: bool) -> str:
    """SQL expression normalising a timestamp column that may carry mixed UTC offsets (tz_mixed dirt)."""
    return f"(CAST({col} AS TIMESTAMPTZ) AT TIME ZONE 'UTC')" if dirty else col


@register
class CohortMatrix(Archetype):
    """Build the retention triangle correctly; it reveals a promo cohort the naive slide hides."""

    key = "cohort_matrix"
    title = "Triangle"
    pillars = ("sql", "python", "operations_strategy")
    industries = ("saas", "ecommerce", "fintech_marketplace")
    schema_template = "saas_billing"
    summary = ("The growth team says retention is improving; the board wants a proper cohort view built from the "
               "subscription data - duplicates, reactivations, time zones and all.")
    root_cause_terms = ("promo cohort", "right-censoring", "reactivation", "deduplicat")
    bs_caption_exemptions = {"ecommerce:inventor": "subscription boxes are fulfilled by a partner billed per box; no owned stock is modelled"}
    stage_options = ("growth",)  # a growth story with tens of $M of ARR (never seed stage)

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 13 (difficulty scales the promo effect, dirt and red herrings)."""
        ind = industry or "saas"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        story, segs = STORY[ind]
        churn_smb = float(np.clip(bk.sample_between(pool, rng.spawn("bench:smb"), "saas.gross_churn_monthly_smb", "saas", 0.4, 0.7), 0.02, 0.06))
        churn_mm = float(np.clip(bk.sample_between(pool, rng.spawn("bench:mm"), "saas.gross_churn_monthly_midmarket", "saas", 0.4, 0.7), 0.01, 0.035))
        arpa = float(np.clip(bk.sample_between(pool, rng.spawn("bench:arpa"), "saas.arpa_monthly", "saas", 0.3, 0.6), 60, 900))
        if ind != "saas":  # a subscription box or a seller-pro plan is priced like a consumer / small-seller subscription
            arpa = float(rng.spawn("bench:arpa_b2c").uniform(*{"ecommerce": (35.0, 90.0), "fintech_marketplace": (60.0, 250.0)}[ind]))
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": story,
            "segments": list(segs),
            "churn_monthly": {segs[0]: churn_smb, segs[1]: churn_mm},
            "sbg_concentration": float(rng.uniform(6.0, 14.0)),  # a + b: heterogeneity (lower = more heterogeneous)
            "arpa_usd": arpa,
            "seg_mix": float(rng.uniform(0.6, 0.8)),
            "growth_monthly": float(rng.uniform(0.005, 0.02)),
            "promo_boost": float(rng.uniform(2.0, 2.5)),
            "promo_churn_mult": magnitude(rng, difficulty, 1.6, 2.4),
            "promo_cliff_share": magnitude(rng, difficulty, 0.2, 0.5),
            "promo_discount": 0.5,
            "reactivation_rate": float(rng.uniform(0.03, 0.08)),
            "plan_change_rate": float(rng.uniform(0.05, 0.1)),
            "hiccup_rate": float(rng.uniform(0.03, 0.07)),
            "voluntary_share": float(rng.uniform(0.55, 0.75)),
            "unsynced_days": int(rng.integers(15, 40)),
            "gm": float(rng.uniform(0.68, 0.8)) if ind != "ecommerce" else float(rng.uniform(0.35, 0.45)),
            # sales & marketing is booked per new logo (CAC = ARPA x GM x payback months); R&D / G&A / rent scale with revenue
            "cac_payback_months": float(rng.uniform(10.0, 16.0)) if ind != "ecommerce" else float(rng.uniform(4.0, 7.0)),
            "rd_ga_share": float(rng.uniform(0.27, 0.34)) if ind != "ecommerce" else float(rng.uniform(0.17, 0.22)),
            # months of subscription history before the books open: the product has an installed base on day one
            "history_months": int(rng.integers(18, 31)),
            "tax_rate": float(rng.uniform(0.18, 0.25)),
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        budget = ROW_BUDGET[difficulty]
        # rows per customer: customers 1 + subscriptions ~1.1 (+dups) + events ~1.7 (+ headroom); marketing_spend ~130 rows
        p["n_customers"] = int((budget * 0.9 - 1750) / 4.0)
        years = (p["history_months"] + 27) / 12.0
        p["description"] = (f"{story}; the subscription product launched about {years:.0f} years ago and has acquired "
                            f"~{p['n_customers']:,} customers since.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Acquisition by month, segment, plan and first-subscription lifetimes (sBG)."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=False)  # base working-capital defaults: prepaid expenses (AP already comes from the purchasing / cost sub-ledger)
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        H = p["history_months"]
        window = cal.month_starts()
        m0 = (pd.Timestamp(cal.start) - pd.DateOffset(months=H)).date()
        months = [(pd.Timestamp(m0) + pd.DateOffset(months=k)).date() for k in range(H)] + list(window)
        n_m = len(months)
        # month index 0 = launch month (history); index H = the month the books open
        state.extras.update({"months": months, "m0": m0, "m_open": H})
        # promo month: first or second month of P5
        p5 = pd.Timestamp(cal.bounds(state.p(5))[0])
        promo_m = int(_midx(pd.Series([p5]), m0)[0]) + int(g.integers(0, 2))
        state.extras["promo_m"] = promo_m
        C = p["n_customers"]
        # new logos per month: organic growth x the SaaS Q4 bookings lift (base noise model) x idiosyncratic noise
        noise = self.period_noise(ctx, state)
        w = (1 + p["growth_monthly"]) ** np.arange(n_m) * np.array([noise.bookings(m) for m in months]) * g.lognormal(0, 0.06, n_m)
        w[promo_m] *= p["promo_boost"]
        counts = np.floor(C * w / w.sum()).astype(int)
        counts[-1] += C - counts.sum()
        cm = np.repeat(np.arange(n_m), counts)
        mstart = pd.to_datetime(pd.Series(months))
        days_in = mstart.dt.days_in_month.to_numpy()
        # signups through the day; near-midnight UTC signups are where time-zone bucketing bites
        secs = g.integers(0, 86400, C)
        start = pd.Series(mstart.to_numpy()[cm] + pd.to_timedelta(g.integers(0, days_in[cm]), unit="D").to_numpy()
                          + pd.to_timedelta(secs, unit="s").to_numpy())
        order = np.argsort(start.to_numpy(), kind="stable")
        start = start.iloc[order].reset_index(drop=True)
        cm = cm[order]
        segs = p["segments"]
        seg = np.where(g.random(C) < p["seg_mix"], segs[0], segs[1])
        organic = np.where(cm == promo_m, 1.0 / p["promo_boost"], 1.0)
        promo = (cm == promo_m) & (g.random(C) >= organic)
        conc = p["sbg_concentration"]
        a = np.array([p["churn_monthly"][s] * conc for s in seg])
        b = conc - a
        a = np.where(promo, a * p["promo_churn_mult"], a)
        theta = g.beta(a, b)
        T = np.minimum(g.geometric(np.clip(theta, 1e-9, 1.0)), 60).astype(np.int64)  # lifetimes past 5 years are censored anyway
        cliff = promo & (T > 3) & (g.random(C) < p["promo_cliff_share"])
        T = np.where(cliff, 3, T)
        price_base = {segs[0]: p["arpa_usd"] * 0.7, segs[1]: p["arpa_usd"] * 2.2}
        plans = pd.DataFrame({
            "plan_id": [f"PL-{s.upper()}" for s in segs] + [f"PL-{s.upper()}-PLUS" for s in segs],
            "name": [s.capitalize() for s in segs] + [f"{s.capitalize()} Plus" for s in segs],
            "billing_interval": "month",
            "price_cents": [int(round(price_base[s] / 5) * 500) for s in segs] + [int(round(price_base[s] * 1.5 / 5) * 500) for s in segs],
            "tier": list(segs) * 2, "currency": "USD", "active": True,
        })
        cust = pd.DataFrame({
            "customer_id": [f"C{i:06d}" for i in range(1, C + 1)],
            "segment": seg,
            "country": np.array(("US", "US", "US", "CA", "GB", "DE", "AU", "IN"))[g.integers(0, 8, C)],
            "acquisition_channel": np.where(promo, "promo", np.array(("paid_search", "organic", "referral", "partner"))[g.integers(0, 4, C)]),
            "created_at": start - pd.to_timedelta(g.integers(0, 3600, C), unit="s"),
        })
        state.frames.update({"customers": cust, "plans": plans})
        state.extras["arpa_net_cents"] = float(np.mean([price_base[x] for x in seg]) * 100)
        state.extras.update({"start": start, "T": T, "promo": promo, "seg": seg, "cohort_m": cm, "cliff": cliff})

    # ------------------------------------------------------------------ anomaly + subscription rows

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Expand lifetimes into subscription rows (plan changes, hiccups, reactivations) and events."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("anomaly").generator
        start, T, promo, seg = (state.extras[k] for k in ("start", "T", "promo", "seg"))
        cust_ids = state.frames["customers"]["customer_id"].to_numpy()
        plans = state.frames["plans"].set_index("plan_id")["price_cents"]

        asof = pd.Timestamp(cal.end) + pd.Timedelta(days=1)  # first instant after the as-of date
        C = len(cust_ids)
        # first island: split by plan change / hiccup
        split = np.where(T >= 3, g.random(C), 1.0)
        change = split < p["plan_change_rate"]
        hiccup = (~change) & (split < p["plan_change_rate"] + p["hiccup_rate"])
        j = np.where(T >= 3, np.maximum(1, (g.random(C) * (T - 1)).astype(np.int64)), 1)
        j = np.where(change | hiccup, np.maximum(2, j), j)
        j = np.minimum(j, np.maximum(1, T - 1))
        end1 = _add_months(start, T)
        cut = _add_months(start, j)
        gap = pd.to_timedelta(np.where(hiccup, g.integers(2, 21, C), 0), unit="D")
        base_plan = np.array([f"PL-{s.upper()}" for s in seg], dtype=object)
        plus_plan = np.array([f"PL-{s.upper()}-PLUS" for s in seg], dtype=object)
        rows = []
        # piece 1
        e1 = np.where(change | hiccup, cut, end1)
        rows.append(pd.DataFrame({"_c": np.arange(C), "started_at": start, "ended_at": e1, "plan_id": base_plan, "_island": 1,
                                  "_promo": promo, "_kind": "new", "_months_paid": np.where(change | hiccup, j, T)}))
        sel = np.flatnonzero(change | hiccup)
        s2 = (cut + gap).iloc[sel]
        rows.append(pd.DataFrame({"_c": sel, "started_at": s2.to_numpy(), "ended_at": _add_months(s2, (T - j)[sel]).to_numpy(),
                                  "plan_id": np.where(change[sel], plus_plan[sel], base_plan[sel]), "_island": 1, "_promo": False,
                                  "_kind": np.where(change[sel], "plan_change", "resubscribe"), "_months_paid": (T - j)[sel]}))
        first = pd.concat(rows, ignore_index=True)
        island_end = first.groupby("_c")["ended_at"].max().reindex(np.arange(C)).to_numpy()
        churned = island_end < asof.to_datetime64()
        react = churned & (g.random(C) < p["reactivation_rate"])
        rsel = np.flatnonzero(react)
        r_start = pd.Series(island_end[rsel]) + pd.to_timedelta(g.integers(35, 241, len(rsel)), unit="D") + pd.to_timedelta(g.integers(0, 86400, len(rsel)), unit="s")
        conc = p["sbg_concentration"]
        a = np.array([p["churn_monthly"][seg[c]] * conc for c in rsel])
        theta = g.beta(a, conc - a) if len(rsel) else np.array([])
        T2 = np.minimum(g.geometric(np.clip(theta, 1e-9, 1.0)), 60).astype(np.int64) if len(rsel) else np.array([], dtype=np.int64)
        re = pd.DataFrame({"_c": rsel, "started_at": r_start.to_numpy(), "ended_at": _add_months(r_start, T2).to_numpy(), "plan_id": base_plan[rsel],
                           "_island": 2, "_promo": False, "_kind": "reactivation", "_months_paid": T2})
        re = re[re["started_at"] < asof].reset_index(drop=True)
        subs = pd.concat([first, re], ignore_index=True)
        subs = subs[pd.to_datetime(subs["started_at"]) < asof].reset_index(drop=True)  # a re-subscribe after the as-of date has not happened yet
        subs["started_at"] = pd.to_datetime(subs["started_at"])
        subs["ended_at"] = pd.to_datetime(subs["ended_at"])
        # censor at the as-of date: still active -> ended_at NULL; months paid up to the as-of date
        subs["_active"] = (subs["ended_at"] >= asof).to_numpy()
        subs = subs.sort_values(["started_at", "_c"], kind="stable").reset_index(drop=True)
        subs["subscription_id"] = [f"SUB{i:06d}" for i in range(1, len(subs) + 1)]
        subs["customer_id"] = cust_ids[subs["_c"].to_numpy()]
        prev = subs.sort_values(["_c", "started_at"], kind="stable").groupby("_c")["subscription_id"].shift(1)
        subs["previous_subscription_id"] = prev.reindex(subs.index)
        subs["price_cents"] = subs["plan_id"].map(plans).to_numpy().astype(np.int64)
        # cancellation recording: last piece of a churned island
        last_piece = subs.sort_values(["_c", "started_at"], kind="stable").groupby(["_c", "_island"]).tail(1).index
        is_last = subs.index.isin(last_piece) & ~subs["_active"].to_numpy()
        n = len(subs)
        vol = g.random(n) < p["voluntary_share"]
        rec_vol = subs["ended_at"] - pd.to_timedelta(g.uniform(1, 25, n), unit="D")
        rec_inv = subs["ended_at"] + pd.to_timedelta(g.uniform(3, 25, n), unit="D")
        cancelled_at = pd.Series(np.where(vol, rec_vol, rec_inv)).where(is_last)
        cancelled_at = pd.to_datetime(cancelled_at).where(pd.to_datetime(cancelled_at) < asof)
        subs["cancelled_at"] = cancelled_at
        subs["cancel_type"] = np.where(is_last, np.where(vol, "voluntary", "involuntary"), None)
        subs["status"] = np.where(subs["_active"], "active", "ended")
        subs["promo_code"] = np.where(subs["_promo"], PROMO_CODE, None)
        state.extras["subs"] = subs
        promo_m = state.extras["promo_m"]
        pm_label = f"{pd.Timestamp(state.extras['months'][promo_m]):%Y-%m}"
        state.extras["promo_month_label"] = pm_label
        return [
            Anomaly(id="A1", kind="promo_cohort_collapse", pillar="operations_strategy", title=f"The {pm_label} promo cohort churns twice as fast",
                    mechanism=(f"{PROMO_CODE} (50% off for three months) in {pm_label} brought {int(promo.sum())} promo customers "
                               f"({p['promo_boost']:.1f}x normal acquisition). Their churn hazard is {p['promo_churn_mult']:.1f}x and "
                               f"{p['promo_cliff_share']:.0%} of those still around leave when the discount ends after month 3."),
                    periods=[cal.labels([pd.Timestamp(state.extras['months'][promo_m])])[0]], affected_accounts=["4000", "4110"],
                    affected_tables=["subscriptions", "customers"],
                    magnitude={"promo_churn_mult": round(p["promo_churn_mult"], 4), "promo_cliff_share": round(p["promo_cliff_share"], 4)},
                    severity="high"),
            Anomaly(id="A2", kind="naive_cohort_counting", pillar="sql", title="Naive counting: censoring, duplicates, reactivations, time zones",
                    mechanism=("The 'retention is improving' slide divides each cohort's currently-active customers by subscription rows started "
                               "that month: young cohorts have had less time to churn (right-censoring), re-ingested rows and plan changes / "
                               "reactivations are counted as new customers, and signup months are bucketed in mixed local time zones."),
                    periods=list(state.periods[-2:]), affected_accounts=[], affected_tables=["subscriptions", "subscription_events"],
                    magnitude={"reactivation_rate": round(p["reactivation_rate"], 4)}, severity="medium"),
        ]

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Monthly charges on billing anniversaries, events table, then the GL."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        subs = state.extras["subs"]
        asof = pd.Timestamp(cal.end) + pd.Timedelta(days=1)
        # charges: one per paid month inside the data window
        months_paid = np.where(subs["_active"], 0, subs["_months_paid"].to_numpy())
        # active subscriptions: anniversaries up to the as-of date
        n_active = np.zeros(len(subs), dtype=np.int64)
        act = subs["_active"].to_numpy()
        if act.any():
            st = subs.loc[act, "started_at"]
            n_active[act] = ((asof.year - st.dt.year) * 12 + (asof.month - st.dt.month)).to_numpy() + 1
        n_ch = np.where(act, n_active, months_paid)
        idx = np.repeat(np.arange(len(subs)), n_ch)
        k = np.concatenate([np.arange(c) for c in n_ch]) if len(n_ch) else np.array([], dtype=np.int64)
        bill = _add_months(subs["started_at"].iloc[idx].reset_index(drop=True), k)
        keep = (bill < asof).to_numpy()
        ch = pd.DataFrame({"_s": idx[keep], "_k": k[keep], "billed_at": bill[keep].to_numpy()})
        price = subs["price_cents"].to_numpy()[ch["_s"].to_numpy()]
        disc = np.where(subs["_promo"].to_numpy()[ch["_s"].to_numpy()] & (ch["_k"].to_numpy() < 3), cents_array(price * p["promo_discount"]), 0)
        ch["gross_cents"] = price
        ch["discount_cents"] = disc
        ch["net_cents"] = price - disc
        ch["fee_cents"] = cents_array(ch["net_cents"].to_numpy() * FEE_RATE) + FEE_FIXED
        state.extras["charges"] = ch
        # --- subscription events (late-arriving involuntary churn)
        ev = [pd.DataFrame({"subscription_id": subs["subscription_id"], "customer_id": subs["customer_id"],
                            "event_type": np.where(subs["_kind"] == "reactivation", "reactivated", np.where(subs["_kind"] == "new", "created", subs["_kind"])),
                            "occurred_at": subs["started_at"]})]
        ended = subs[~subs["_active"]]
        ev.append(pd.DataFrame({"subscription_id": ended["subscription_id"], "customer_id": ended["customer_id"], "event_type": "ended",
                                "occurred_at": ended["ended_at"]}))
        canc = subs.dropna(subset=["cancelled_at"])
        ev.append(pd.DataFrame({"subscription_id": canc["subscription_id"], "customer_id": canc["customer_id"],
                                "event_type": np.where(canc["cancel_type"] == "voluntary", "cancel_requested", "payment_failed"),
                                "occurred_at": np.where(canc["cancel_type"] == "voluntary", canc["cancelled_at"], canc["ended_at"] - pd.Timedelta(days=3))}))
        events = pd.concat(ev, ignore_index=True)
        events["occurred_at"] = pd.to_datetime(events["occurred_at"])
        lag_h = np.where(events["event_type"].isin(["payment_failed"]), g.uniform(72, 600, len(events)), g.uniform(0.0, 0.2, len(events)))
        events["recorded_at"] = events["occurred_at"] + pd.to_timedelta(np.round(lag_h * 3600), unit="s")
        events = events[(events["occurred_at"] < asof) & (events["recorded_at"] < asof)]
        events = events.sort_values(["occurred_at", "subscription_id", "event_type"], kind="stable").reset_index(drop=True)
        events.insert(0, "event_id", np.arange(1, len(events) + 1, dtype=np.int64))
        state.frames["subscription_events"] = events
        self._post(ctx, state)

    def _post(self, ctx: BuildContext, state: WorldState) -> None:
        """Opening balances, monthly charge batches, hosting, sales & marketing per new logo, R&D / G&A, capex, depreciation, tax.

        Revenue is the subscription charges themselves (list price less the promo discount, recognised over the service month
        through the quarter-end deferral), so quarterly revenue is Sigma active MRR x 3 plus plan changes and proration - it
        grows only as fast as new logos x ARPA net of churn. Charges billed before the books open (installed-base history)
        are not posted."""
        p, cal = state.params, state.calendar
        L = state.ledger
        months = state.extras["months"]
        m0, H = state.extras["m0"], state.extras["m_open"]
        ch = state.extras["charges"]
        noise = self.period_noise(ctx, state)
        # card charges of the last two days of a month settle in the next month (T+2): settlement receivable at month end
        bat = pd.to_datetime(ch["billed_at"])
        transit = (bat.dt.day > bat.dt.days_in_month - 2).to_numpy()
        ch_t = ch.assign(tr=np.where(transit, ch["net_cents"] - ch["fee_cents"], 0))
        ch_m = ch_t.groupby(_midx(ch["billed_at"], m0)).agg(g=("gross_cents", "sum"), d=("discount_cents", "sum"), f=("fee_cents", "sum"),
                                                            n=("_s", "size"), tr=("tr", "sum"))
        state.extras["charges_by_month"] = ch_m
        rev_ttm = float((ch_m["g"] - ch_m["d"]).iloc[-12:].sum()) or 1.0
        # sales & marketing: CAC per new logo (a promo signup costs ~30%: the discount is the incentive)
        cohort_m, promo = state.extras["cohort_m"], state.extras["promo"]
        new_eq = np.bincount(cohort_m, weights=np.where(promo, 0.3, 1.0), minlength=len(months))
        cac = cents(state.extras["arpa_net_cents"] * p["gm"] * p["cac_payback_months"])
        state.extras["cac_cents"] = cac
        sm_m = {m: cents(new_eq[m] * cac * noise.ox(months[m])) for m in range(H, len(months))}
        state.extras["sm_by_month"] = sm_m
        cash0 = cents(rev_ttm * 0.45) + 25_000_000
        ppe = cents(rev_ttm * 0.04)
        accdep = cents(ppe * 0.4)
        cs = cents(0.6 * (cash0 + ppe - accdep))
        L.open_books(cal.opening_date, {"1000": cash0, "1500": ppe, "1510": -accdep, "3000": -cs, "3100": -(cash0 + ppe - accdep - cs)})
        events: list[tuple[dt.date, int, Any]] = []
        prev = {"payroll": 0, "opex": 0, "cogs": 0}
        for m in range(H, len(months)):
            ms = months[m]
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            r = ch_m.loc[m] if m in ch_m.index else None
            net = int(r.g - r.d) if r is not None else 0
            if r is not None:
                dim = {"batch": f"card_charges:{ms:%Y-%m}", "rows": str(int(r.n))}
                events.append((me, 3, lambda d, a=int(r.g), x=int(r.d), f=int(r.f), t=int(r.tr), dm=dim: L.adjust(
                    d, [("1000", a - x - f - t, 0, dm), ("1100", t, 0, dm), ("5050", f, 0, dm), ("4110", x, 0, dm), ("4000", 0, a, dm)],
                    "Subscription card charges (monthly batch)", "sales")))
                events.append((me + dt.timedelta(days=2), 2, lambda d, t=int(r.tr), dm=dim: L.collect_cash(
                    d, t, memo="Card settlements in transit received", source="settlement_funds", dim=dm)))
            run = float(np.mean([int(ch_m.loc[j].g - ch_m.loc[j].d) if j in ch_m.index else 0 for j in range(max(0, m - 2), m + 1)])) or rev_ttm / 12
            cogs = cents(net * (1 - p["gm"] - noise.gm(ms)))  # review C2: quarterly GM / opex noise
            sm = sm_m[m]
            rdga = cents(run * p["rd_ga_share"] * noise.ox(ms))
            sm_pay, rd_pay = cents(sm * 0.45), cents(rdga * 0.7)
            payroll = sm_pay + rd_pay
            other = (sm - sm_pay) + (rdga - rd_pay)
            if m > H:
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["opex"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex paid", source="opex")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["cogs"]: L.pay_ap(d, a, memo="Hosting / fulfilment vendors paid", source="payables")))
            events.append((me, 5, lambda d, a=cogs: L.adjust(d, [("5000", a, 0), ("2000", 0, a)], "Hosting / fulfilment cost", "cost_of_revenue")))
            rd = split_cents(rd_pay, [0.6, 0.4])
            pr = {"6000": sm_pay, "6100": rd[0], "6200": rd[1]}
            ox = dict(zip(("6100", "6200", "6400"), split_cents(rdga - rd_pay, [0.4, 0.35, 0.25]), strict=True))
            ox["6000"] = sm - sm_pay
            events.append((me, 7, lambda d, s=pr: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox: L.accrue_opex(d, s, memo="Marketing programs and operating expenses")))
            events.append((ms + dt.timedelta(days=14), 6, lambda d, a=cents(run * 0.01): L.capex(d, a)))
            events.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 48))))
            if ms.month % 3 == 0:
                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))
                events.append((me, 11, tax))
            if ms.month in (1, 4, 7, 10) and m > H:
                events.append((ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"])))
            prev = {"payroll": payroll, "opex": other, "cogs": cogs}
        events.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in events:
            if d <= cal.end:
                fn(d)
        # monthly charges are billed in advance for the next month of service: quarter-end deferral (review P0 #3)
        bill = pd.to_datetime(ch["billed_at"])
        posted = (bill >= pd.Timestamp(cal.start)).to_numpy()
        bill = bill[posted]
        docs = pd.DataFrame({"posted": bill.dt.to_period("M").dt.to_timestamp(how="end").dt.normalize(), "service_start": bill,
                             "service_end": bill + pd.DateOffset(months=1), "amount_cents": ch["net_cents"].to_numpy()[posted]})
        state.extras["deferral_effect"] = post_deferred_revenue_trueups(state, docs, revenue_account="4000")

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """customers, plans, promotions, subscriptions, subscription_events + 2/4/6 dirt rules."""
        p, cal = state.params, state.calendar
        subs = state.extras["subs"]
        out = pd.DataFrame({
            "subscription_id": subs["subscription_id"], "customer_id": subs["customer_id"], "plan_id": subs["plan_id"],
            "status": subs["status"], "started_at": subs["started_at"], "ended_at": subs["ended_at"].where(~subs["_active"]),
            "cancelled_at": subs["cancelled_at"], "cancel_type": subs["cancel_type"], "promo_code": subs["promo_code"],
            "previous_subscription_id": subs["previous_subscription_id"], "mrr_cents": subs["price_cents"],
            "created_at": subs["started_at"], "updated_at": subs["cancelled_at"].fillna(subs["started_at"]), "ingest_id": 1,
        })
        pm = pd.Timestamp(state.extras["months"][state.extras["promo_m"]])
        promos = pd.DataFrame([{"promo_code": PROMO_CODE, "description": "50% off the first three months", "discount_pct": 50.0,
                                "duration_months": 3, "starts_at": pm, "ends_at": pm + pd.offsets.MonthEnd(0) + pd.Timedelta(hours=23, minutes=59),
                                "channel": "paid_social"},
                               {"promo_code": "REFER10", "description": "10% off for referred teams (never activated)", "discount_pct": 10.0,
                                "duration_months": 1, "starts_at": pd.Timestamp(cal.start) + pd.Timedelta(days=200),
                                "ends_at": pd.Timestamp(cal.start) + pd.Timedelta(days=230), "channel": "referral"}])
        dirt_plan = [
            ("customers", "case_mixed:acquisition_channel"), ("subscription_events", "case_mixed:event_type"),
            ("subscriptions", "dup_keys:0.02"), ("customers", "nulls:country:0.05"),
            ("subscriptions", "late_arriving:updated_at"), ("subscriptions", "tz_mixed:started_at"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        state.extras["tz_dirty"] = "tz_mixed:started_at" in dirt.get("subscriptions", [])
        builder.add_frame("customers", state.frames["customers"], primary_key=["customer_id"], dirt=dirt.get("customers", ()),
                          description="Customers (one row per customer account).")
        builder.add_frame("plans", state.frames["plans"], primary_key=["plan_id"], column_types={"price_cents": "BIGINT"},
                          descriptions={"price_cents": "monthly list price"})
        builder.add_frame("promotions", promos, primary_key=["promo_code"], column_types={"discount_pct": "DOUBLE", "duration_months": "INTEGER"})
        builder.add_frame("subscriptions", out, primary_key=["subscription_id"],
                          foreign_keys=[("customer_id", "customers", "customer_id"), ("plan_id", "plans", "plan_id")],
                          column_types={"mrr_cents": "BIGINT", "started_at": "TIMESTAMP", "ended_at": "TIMESTAMP", "cancelled_at": "TIMESTAMP"},
                          dirt=dirt.get("subscriptions", ()),
                          descriptions={"started_at": "subscription start (UTC)", "ended_at": "effective end of the last paid month (NULL = active)",
                                        "cancelled_at": "when the cancellation was recorded (involuntary churn syncs late; may be NULL)",
                                        "previous_subscription_id": "the customer's previous subscription (plan change, re-subscribe or reactivation)",
                                        "ingest_id": ">1 marks a re-ingested copy from the billing migration"},
                          description="Subscriptions (one row per subscription; a customer can have several).")
        builder.add_frame("marketing_spend", self._marketing_spend(ctx, state), primary_key=["month", "channel"],
                          column_types={"month": "DATE", "spend_cents": "BIGINT", "new_customers_attributed": "INTEGER"},
                          descriptions={"spend_cents": "program spend (media, partner fees, events); sales & marketing payroll is not included",
                                        "new_customers_attributed": "new customers whose acquisition_channel maps to this channel (last touch)"},
                          description="Monthly marketing program spend by channel since the books opened (GL 6000 also carries S&M payroll).")
        builder.add_frame("subscription_events", state.frames["subscription_events"], primary_key=["event_id"],
                          foreign_keys=[("subscription_id", "subscriptions", "subscription_id")], column_types={"event_id": "BIGINT"},
                          dirt=dirt.get("subscription_events", ()),
                          descriptions={"recorded_at": "when the event reached the warehouse (payment failures sync days late)"},
                          description="Subscription lifecycle events from the billing provider.")

    def _marketing_spend(self, ctx: BuildContext, state: WorldState) -> pd.DataFrame:
        """Program part of the monthly S&M spend (55%; the rest is S&M payroll), split by channel: the promo month's
        paid-social campaign carries the promo signups, the rest follows the organic channel mix."""
        g = ctx.rng.spawn("marketing").generator
        months, H = state.extras["months"], state.extras["m_open"]
        cust = state.frames["customers"]
        cm = state.extras["cohort_m"]
        chan_of = {"paid_search": "paid_search", "partner": "partner", "referral": "partner", "organic": "content_seo", "promo": "paid_social"}
        rows = []
        for m in range(H, len(months)):
            sm = state.extras["sm_by_month"][m]
            prog = sm - cents(sm * 0.45)
            got = cust.loc[cm == m, "acquisition_channel"].map(chan_of).value_counts()
            chans = ["paid_search", "partner", "content_seo", "events"] + (["paid_social"] if got.get("paid_social", 0) else [])
            wts = np.array([1.0 + got.get(c, 0) for c in chans]) * g.uniform(0.8, 1.2, len(chans))
            for c, a in zip(chans, split_cents(prog, list(wts / wts.sum())), strict=True):
                rows.append({"month": pd.Timestamp(months[m]), "channel": c, "spend_cents": int(a), "new_customers_attributed": int(got.get(c, 0))})
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """Charges generated from subscription intervals == GL net subscription revenue (holds)."""
        cal = state.calendar
        ch = state.extras["charges"]
        per = cal.labels(ch["billed_at"])
        rev = {x: int(ch.loc[per == x, "net_cents"].sum()) for x in state.periods}
        return [SubledgerTie("subscription_charges_vs_revenue", rev, ["4000", "4110"], basis="activity", exclude_sources=(DEFERRAL_SOURCE,),
                             description="Monthly charges on billing anniversaries (list - promo discount) == GL net revenue")]

    # ------------------------------------------------------------------ the triangle (ground truth)

    def _triangle(self, state: WorldState) -> dict[str, Any]:
        if "tri" in state.extras:
            return state.extras["tri"]
        subs = state.extras["subs"]
        n_m = len(state.extras["months"])
        m0 = state.extras["m0"]
        s_idx = _midx(subs["started_at"], m0)
        # active at the end of month M  <=>  start month <= M  and  (open  or  end month >= M + 1)
        e_idx = np.where(subs["_active"], n_m, _midx(subs["ended_at"], m0))
        cust = subs["_c"].to_numpy()
        C = int(cust.max()) + 1
        active = np.zeros((C, n_m), dtype=bool)
        for c, a, b in zip(cust, s_idx, e_idx, strict=True):
            if b > a:
                active[c, a:b] = True
        cohort = np.full(C, n_m)
        np.minimum.at(cohort, cust, s_idx)
        size = np.bincount(cohort, minlength=n_m)[:n_m]
        ret = np.full((n_m, n_m), np.nan)
        for co in range(n_m):
            members = cohort == co
            if not size[co]:
                continue
            for k in range(n_m - co):
                ret[co, k] = active[members, co + k].sum() / size[co]
        promo_m = state.extras["promo_m"]

        def wavg(k: int, exclude: int | None = None) -> float:
            rows = [c for c in range(n_m - k) if size[c] and c != exclude]
            return float(sum(ret[c, k] * size[c] for c in rows) / max(1, sum(size[c] for c in rows)))

        # sBG on first-island survival of fully observed (>= 12 months) non-promo cohorts
        i1 = subs["_island"].to_numpy() == 1
        first_end = pd.Series(e_idx[i1]).groupby(cust[i1]).max().reindex(np.arange(C)).to_numpy()
        elig = (cohort <= n_m - 13) & (cohort != promo_m)
        surv = np.array([int(((first_end[elig] - cohort[elig]) > k).sum()) for k in range(13)])
        fit = sbg_fit(surv)
        reactivated = subs.groupby("_c")["_island"].max() >= 2
        out = {"ret": ret, "size": size, "promo_m": promo_m, "m3_promo": float(ret[promo_m, 3]), "m3_base": wavg(3, promo_m),
               "m6_all": wavg(6), "m6_base": wavg(6, promo_m), "reactivation_share": float(reactivated.mean()),
               "sbg": fit, "surv": surv.tolist(), "cohort": cohort}
        state.extras["tri"] = out
        return out

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Monthly cohort curves (from the triangle), unit economics from the books and KPIs by period."""
        p, cal = state.params, state.calendar
        st = state.statements
        tri = self._triangle(state)
        months = state.extras["months"]
        n_m = len(months)
        subs = state.extras["subs"]
        mrr = subs["price_cents"].to_numpy()
        cust_mrr = pd.Series(mrr).groupby(subs["_c"].to_numpy()).first()
        cohorts = []
        for co in range(n_m):
            if not tri["size"][co]:
                continue
            curve = [float(x) for x in tri["ret"][co] if not np.isnan(x)]
            members = np.flatnonzero(tri["cohort"] == co)
            arpa_co = float(cust_mrr.reindex(members).mean())
            cohorts.append(cohort_from_curve(f"{pd.Timestamp(months[co]):%Y-%m}", int(tri["size"][co]), curve, arpa_co))
        kpis = {}
        for period in state.periods:
            inc = next(i for i in st.income if i.period == period)  # type: ignore[union-attr]
            s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + pd.Timedelta(days=1)
            on = (subs["started_at"] < e_ts) & (subs["_active"] | (subs["ended_at"] >= e_ts))
            new = ((subs["started_at"] >= pd.Timestamp(s)) & (subs["started_at"] < e_ts) & (subs["_kind"] == "new")).sum()
            kpis[period] = {"revenue_cents": float(inc.revenue_cents), "net_income_cents": float(inc.net_income_cents),
                            "active_subscriptions": float(on.sum()), "mrr_cents": float(subs.loc[on, "price_cents"].sum()),
                            "new_customers": float(new), "gross_margin": st.ratios[period]["gross_margin"]}  # type: ignore[union-attr]
        ue = self._unit_economics(state)
        return build_operations_state(unit_economics=ue, cohorts=cohorts, kpis_by_period=kpis,
                                      pricing={"promo": {"code": PROMO_CODE, "discount": p["promo_discount"], "months": 3}})

    def _unit_economics(self, state: WorldState) -> dict[str, float]:
        """CAC = sales & marketing booked in the last four quarters (GL 6000) / new logos acquired in them; ARPA = MRR per
        active customer at the as-of date; churn from the censoring-aware month-6 retention of non-promo cohorts;
        LTV = ARPA x GM / churn; payback = CAC / (ARPA x GM)."""
        if "unit_economics" in state.extras:
            return state.extras["unit_economics"]
        cal, st = state.calendar, state.statements
        tri = self._triangle(state)
        subs = state.extras["subs"]
        ttm = state.periods[-4:]
        sm = float(sum(state.ledger.activity(x)["6000"] for x in ttm))
        t0 = pd.Timestamp(cal.bounds(ttm[0])[0])
        first = subs.groupby("_c")["started_at"].min()
        new_ttm = int((first >= t0).sum())
        cac = sm / max(1, new_ttm)
        active = subs[subs["_active"]]
        arpa = float(active["price_cents"].sum() / max(1, active["_c"].nunique()))
        gm = float(np.mean([st.ratios[x]["gross_margin"] for x in ttm]))  # type: ignore[union-attr]
        churn = 1 - tri["m6_base"] ** (1 / 6) if tri["m6_base"] > 0 else 0.0
        ue = {"arpa": arpa, "churn_monthly": churn, "gross_margin": gm, "ltv": arpa * gm / max(churn, 1e-3), "cac": cac,
              "payback_months": cac / max(1.0, arpa * gm), "new_logos_ttm": float(new_ttm), "sales_marketing_ttm_cents": sm}
        ue["ltv_to_cac"] = ue["ltv"] / max(1.0, cac)
        state.extras["unit_economics"] = ue
        return ue

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Triangle metrics, gaps-and-islands / pivot SQL run on the dirty DB, censoring-aware Python reference."""
        p = state.params
        tri = self._triangle(state)
        months = state.extras["months"]

        first_m, last_m = pd.Timestamp(months[0]).date(), pd.Timestamp(months[-1]).date()
        st_expr = _utc("started_at", state.extras["tz_dirty"])
        promo_m = tri["promo_m"]
        pm_label = state.extras["promo_month_label"]
        dedupe = f"""s AS (
  SELECT subscription_id, customer_id, {st_expr} AS started_at, ended_at, promo_code
  FROM subscriptions
  QUALIFY ROW_NUMBER() OVER (PARTITION BY subscription_id ORDER BY ingest_id) = 1)"""
        sql_tri = f"""
WITH {dedupe},
cohort AS (
  SELECT customer_id, CAST(date_trunc('month', MIN(started_at)) AS DATE) AS cohort
  FROM s GROUP BY 1),
months AS (
  SELECT CAST(m AS DATE) AS m FROM generate_series(DATE '{first_m}', DATE '{last_m}', INTERVAL 1 MONTH) t(m)),
active AS (   -- a customer is active in month m if some subscription covers the end of m
  SELECT DISTINCT c.customer_id, c.cohort, mo.m
  FROM cohort c
  JOIN s ON s.customer_id = c.customer_id
  JOIN months mo ON mo.m >= c.cohort
               AND s.started_at < mo.m + INTERVAL 1 MONTH
               AND (s.ended_at IS NULL OR s.ended_at >= mo.m + INTERVAL 1 MONTH)),
sizes AS (SELECT cohort, COUNT(*) AS cohort_size FROM cohort GROUP BY 1)
SELECT a.cohort, date_diff('month', a.cohort, a.m) AS k, sz.cohort_size,
       COUNT(*) AS active_customers, COUNT(*) * 1.0 / sz.cohort_size AS retention
FROM active a JOIN sizes sz USING (cohort)
GROUP BY a.cohort, k, sz.cohort_size
ORDER BY a.cohort, k"""
        tri_df = con.execute(sql_tri).fetchdf()

        def tri_checks(df: pd.DataFrame) -> dict[str, Any]:
            df = df.assign(cohort=pd.to_datetime(df["cohort"]))
            pm = pd.Timestamp(months[promo_m])
            m3 = df[(df["cohort"] == pm) & (df["k"] == 3)]["retention"]
            return {"cells": int(len(df)), "max_k": int(df["k"].max()), "promo_m3": float(m3.iloc[0]) if len(m3) else 0.0,
                    "cohorts": int(df["cohort"].nunique())}

        q_tri = run_reference_query(con, "retention_triangle_long", sql_tri, signature=["qualify", "with", "join", "group by", "date_diff("],
                                    metric_keys=["anomalous_cohort_m3_retention", "weighted_avg_m6_retention", "baseline_m3_retention"],
                                    checks=tri_checks, max_rows=60)
        sql_pivot = f"""
WITH {dedupe},
cohort AS (SELECT customer_id, CAST(date_trunc('month', MIN(started_at)) AS DATE) AS cohort,
                  BOOL_OR(promo_code IS NOT NULL) AS promo FROM s GROUP BY 1),
act AS (
  SELECT c.customer_id, c.cohort, c.promo, k
  FROM cohort c CROSS JOIN (VALUES (1), (3), (6), (12)) v(k)
  WHERE c.cohort + to_months(k) <= DATE '{last_m}'
    AND EXISTS (SELECT 1 FROM s WHERE s.customer_id = c.customer_id
                AND s.started_at < c.cohort + to_months(k + 1)
                AND (s.ended_at IS NULL OR s.ended_at >= c.cohort + to_months(k + 1))))
SELECT c.cohort, COUNT(DISTINCT c.customer_id) AS cohort_size,
       AVG(CASE WHEN c.promo THEN 1.0 ELSE 0.0 END) AS promo_share,
       CASE WHEN c.cohort + to_months(1) <= DATE '{last_m}'   -- NULL, not 0, when the offset is not observed yet (censoring)
            THEN COUNT(DISTINCT a.customer_id) FILTER (WHERE a.k = 1) * 1.0 / COUNT(DISTINCT c.customer_id) END AS m1,
       CASE WHEN c.cohort + to_months(3) <= DATE '{last_m}'
            THEN COUNT(DISTINCT a.customer_id) FILTER (WHERE a.k = 3) * 1.0 / COUNT(DISTINCT c.customer_id) END AS m3,
       CASE WHEN c.cohort + to_months(6) <= DATE '{last_m}'
            THEN COUNT(DISTINCT a.customer_id) FILTER (WHERE a.k = 6) * 1.0 / COUNT(DISTINCT c.customer_id) END AS m6,
       CASE WHEN c.cohort + to_months(12) <= DATE '{last_m}'
            THEN COUNT(DISTINCT a.customer_id) FILTER (WHERE a.k = 12) * 1.0 / COUNT(DISTINCT c.customer_id) END AS m12
FROM cohort c LEFT JOIN act a USING (customer_id)
GROUP BY 1
ORDER BY 1"""
        q_pivot = run_reference_query(con, "cohort_pivot_m1_m3_m6_m12", sql_pivot, signature=["qualify", "left join", "group by", "filter(", "distinct"],
                                      metric_keys=["anomalous_cohort", "anomalous_cohort_m3_retention"],
                                      checks=lambda df: {"worst_m3_cohort": str(pd.Timestamp(df.loc[df["m3"].astype(float).idxmin(), "cohort"]).date())},
                                      max_rows=30)
        sql_isl = f"""
WITH {dedupe},
ord AS (
  SELECT *, MAX(COALESCE(ended_at, TIMESTAMP '9999-12-31')) OVER (
              PARTITION BY customer_id ORDER BY started_at, subscription_id
              ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
  FROM s),
isl AS (
  SELECT *, SUM(CASE WHEN prev_end IS NULL OR started_at > prev_end + INTERVAL 30 DAY THEN 1 ELSE 0 END)
              OVER (PARTITION BY customer_id ORDER BY started_at, subscription_id ROWS UNBOUNDED PRECEDING) AS island
  FROM ord)
SELECT (SELECT COUNT(*) FROM subscriptions) AS raw_subscription_rows,
       COUNT(*) AS distinct_subscriptions,
       COUNT(DISTINCT customer_id) AS customers,
       COUNT(DISTINCT customer_id) FILTER (WHERE island >= 2) AS reactivated_customers,
       COUNT(DISTINCT customer_id) FILTER (WHERE island >= 2) * 1.0 / COUNT(DISTINCT customer_id) AS reactivation_share,
       (SELECT COUNT(*) FROM subscriptions) - COUNT(DISTINCT customer_id) AS naive_vs_correct_delta
FROM isl"""
        isl_row = con.execute(sql_isl).fetchdf().iloc[0]
        q_isl = run_reference_query(con, "gaps_and_islands_reactivations", sql_isl, signature=["over(", "partition by", "with", "case when", "filter("],
                                    metric_keys=["reactivation_share", "naive_vs_correct_customer_count_delta"],
                                    checks=lambda df: {"reactivation_share": float(df["reactivation_share"].iloc[0])})
        sql_slide = """
WITH raw AS (
  SELECT CAST(date_trunc('month', CAST(started_at AS TIMESTAMP)) AS DATE) AS signup_month, subscription_id, status
  FROM subscriptions)   -- the slide: every row is a customer, local time zones, 'active today' share
SELECT signup_month, COUNT(*) AS rows_counted_as_customers,
       AVG(CASE WHEN status = 'active' THEN 1.0 ELSE 0.0 END) AS share_active_today
FROM raw
GROUP BY 1
ORDER BY 1"""
        if state.extras["tz_dirty"]:
            sql_slide = sql_slide.replace("CAST(started_at AS TIMESTAMP)", "CAST(substr(started_at, 1, 19) AS TIMESTAMP)")
        q_slide = run_reference_query(con, "naive_slide_active_today_share", sql_slide, signature=["with", "group by", "case when", "avg("],
                                      metric_keys=["naive_vs_correct_customer_count_delta"],
                                      checks=lambda df: {"last_vs_first_share": float(df["share_active_today"].iloc[-1] - df["share_active_today"].iloc[0])},
                                      max_rows=30)
        sql_late = """
SELECT cancel_type,
       COUNT(*) AS ended_subscriptions,
       COUNT(*) FILTER (WHERE cancelled_at IS NULL) AS cancel_not_recorded,
       AVG(date_diff('day', ended_at, cancelled_at)) AS avg_days_recorded_after_end
FROM subscriptions
WHERE ended_at IS NOT NULL AND cancel_type IS NOT NULL
GROUP BY 1
ORDER BY 1"""
        q_late = run_reference_query(con, "late_arriving_cancellations", sql_late, signature=["group by", "date_diff(", "filter(", "count("],
                                     metric_keys=[], checks=lambda df: {"not_recorded": int(df["cancel_not_recorded"].sum())})
        # metrics from the SQL results (which run on the dirty DB) - asserted equal to the Python triangle in tests
        pm_ts = pd.Timestamp(months[promo_m])
        cells = tri_df.assign(cohort=pd.to_datetime(tri_df["cohort"]))
        sizes = cells.groupby("cohort")["cohort_size"].first()

        def wavg(k: int, exclude: pd.Timestamp | None = None) -> float:
            eligible = [c for c in sizes.index if c + pd.DateOffset(months=k) <= pd.Timestamp(months[-1]) and c != exclude]
            num = sum(float(cells[(cells["cohort"] == c) & (cells["k"] == k)]["active_customers"].sum()) for c in eligible)
            return num / max(1, sum(int(sizes[c]) for c in eligible))

        m3_promo = float(cells[(cells["cohort"] == pm_ts) & (cells["k"] == 3)]["retention"].sum())
        m3_base, m6_all = wavg(3, pm_ts), wavg(6)
        delta = int(isl_row["naive_vs_correct_delta"])
        metrics = [
            metric("anomalous_cohort", float(pm_ts.strftime("%Y%m")), "yyyymm",
                   f"The cohort whose month-3 retention is lowest by far: {pm_label} (promo {PROMO_CODE}).", pillar="sql", tol_abs=0.0,
                   label="Anomalous cohort (YYYYMM)", hint="Compare cohorts at equal month offsets."),
            metric("anomalous_cohort_m3_retention", m3_promo, "ratio", f"Retention of the {pm_label} cohort at k = 3 (active at the end of the 3rd month after the cohort month).",
                   pillar="sql", tol_abs=0.03, label="Anomalous cohort month-3 retention", hint="Active at the end of the third month after the cohort month."),
            metric("baseline_m3_retention", m3_base, "ratio", "Cohort-size-weighted month-3 retention of every other cohort observed for 3 months.",
                   pillar="operations_strategy", tol_abs=0.02, label="Month-3 retention of the other cohorts", hint="Weight each cohort by its size; exclude cohorts not yet observed."),
            metric("weighted_avg_m6_retention", m6_all, "ratio", "Cohort-size-weighted month-6 retention over the cohorts observed for 6 months (censoring-aware: younger cohorts excluded, not averaged).",
                   pillar="operations_strategy", tol_abs=0.02, label="Weighted month-6 retention", hint="Only cohorts observed for six months count (censoring)."),
            metric("reactivation_share", float(isl_row["reactivation_share"]), "ratio",
                   "Customers with a second island (a subscription starting > 30 days after every earlier one ended) / customers.",
                   pillar="sql", tol_abs=0.01, label="Reactivated customer share", hint="Gaps and islands over subscriptions per customer."),
            metric("naive_vs_correct_customer_count_delta", delta, "count",
                   "Raw subscription rows (incl. re-ingested duplicates, plan changes, re-subscribes, reactivations) minus distinct customers.",
                   pillar="sql", tol_abs=0.0, label="Naive minus correct customer count", hint="Raw rows versus distinct customers."),
        ]
        if p["difficulty"] != "foundation":
            fit = tri["sbg"]
            metrics += [
                metric("sbg_alpha", fit["alpha"], "param", f"Shifted-beta-geometric MLE on first-island survivors n_0..n_12 = {tri['surv']} of non-promo cohorts observed >= 12 months.",
                       pillar="python", tol_rel=0.25, label="sBG alpha", hint="Fit a shifted-beta-geometric model to first-island survivor counts."),
                metric("sbg_beta", fit["beta"], "param", "Same fit; beta.", pillar="python", tol_rel=0.25, label="sBG beta", hint="Same fit as alpha."),
            ]
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_tri, q_pivot],
            "magnitude": {**anomalies["A1"].magnitude, "anomalous_cohort_m3_retention": round(m3_promo, 6), "baseline_m3_retention": round(m3_base, 6)},
            "evidence_trail": ["Deduplicate subscriptions on subscription_id, bucket cohorts by the first start in UTC.",
                               f"At equal offsets the {pm_label} cohort retains {m3_promo:.0%} at month 3 vs {m3_base:.0%} for the others.",
                               f"Its customers carry {PROMO_CODE}; churn spikes when the discount ends after month 3."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_isl, q_slide, q_late],
            "magnitude": {**anomalies["A2"].magnitude, "reactivation_share": round(float(isl_row["reactivation_share"]), 6),
                          "naive_vs_correct_customer_count_delta": float(delta)},
            "evidence_trail": [f"Subscription rows exceed customers by {delta}.",
                               "Young cohorts look better 'today' only because they have had less time to churn.",
                               "cancelled_at is late or NULL for involuntary churn; use ended_at."]})
        herrings_all = [
            "Retention improved in recent cohorts - they have simply been observed for fewer months (right-censoring); compare at equal offsets.",
            "Reactivations are new customers - they belong to their original cohort (or a separate reactivation view), never both.",
            "Churn is when cancelled_at is set - involuntary churn is recorded days late or not at all; ended_at is the effective end.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        root = (f"The 'retention is improving' slide is an artefact: it divides currently-active customers by subscription rows per signup month, so "
                f"young cohorts look healthy because they have not had time to churn (right-censoring), and re-ingested rows, plan changes and "
                f"reactivations inflate the counts ({delta} more rows than customers). A deduplicated, UTC-bucketed triangle with reactivations "
                f"attributed to the original cohort shows retention is flat (weighted m6 {m6_all:.0%}) except for the {pm_label} promo cohort: "
                f"{PROMO_CODE} doubled acquisition but those customers churn about twice as fast and leave when the discount ends "
                f"(m3 {m3_promo:.0%} vs {m3_base:.0%}).")
        python_ref = self._python_reference(state)
        return GroundTruth(
            root_cause=root, anomalies=[anomalies["A1"], anomalies["A2"]], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q_tri.sql, q_pivot.sql, q_isl.sql, q_slide.sql, q_late.sql], python=[python_ref],
                memo_bullets=[
                    "Define cohort = UTC month of a customer's first subscription start after deduplicating re-ingested rows.",
                    "Active in a month = covered at month end by any subscription; reactivations stay in the original cohort.",
                    "Compare cohorts only at equal month offsets; average by weighting with cohort size and only over cohorts observed long enough.",
                    f"The {pm_label} promo cohort is the outlier: m3 {m3_promo:.0%} vs {m3_base:.0%}; promo economics must include its churn.",
                    "Churn timing comes from ended_at, not cancelled_at (late and missing for involuntary churn).",
                ]),
            decision_levers=[],
        )

    def _python_reference(self, state: WorldState) -> str:
        months = state.extras["months"]
        first_m, last_m = pd.Timestamp(months[0]), pd.Timestamp(months[-1])
        return (
            "import numpy as np, pandas as pd\n"
            "s = tables['subscriptions'].sort_values('ingest_id').drop_duplicates('subscription_id')\n"
            "s['started_at'] = pd.to_datetime(s['started_at'].astype(str), utc=True, format='mixed').dt.tz_localize(None)\n"
            "s['ended_at'] = pd.to_datetime(s['ended_at'])\n"
            "s['cohort'] = s.groupby('customer_id')['started_at'].transform('min').dt.to_period('M')\n"
            f"months = pd.period_range('{first_m:%Y-%m}', '{last_m:%Y-%m}', freq='M')\n"
            "rows = []\n"
            "for m in months:\n"
            "    end = (m + 1).to_timestamp()\n"
            "    act = s[(s.started_at < end) & (s.ended_at.isna() | (s.ended_at >= end)) & (s.cohort <= m)]\n"
            "    for co, n in act.groupby('cohort')['customer_id'].nunique().items():\n"
            "        rows.append((co, (m - co).n, n))\n"
            "cells = pd.DataFrame(rows, columns=['cohort', 'k', 'active'])\n"
            "size = s.groupby('cohort')['customer_id'].nunique()\n"
            "tri = cells.pivot(index='cohort', columns='k', values='active').reindex(size.index)\n"
            "tri = tri.div(size, axis=0)\n"
            "# censoring: cell (cohort, k) exists only if cohort + k <= last month; fill unobserved-but-zero cells inside the triangle\n"
            "for co in tri.index:\n"
            "    for k in tri.columns:\n"
            "        if co + k <= months[-1] and np.isnan(tri.loc[co, k]):\n"
            "            tri.loc[co, k] = 0.0\n"
            "def wavg(k, exclude=None):\n"
            "    ok = [c for c in tri.index if c + k <= months[-1] and c != exclude]\n"
            "    return float((tri.loc[ok, k] * size[ok]).sum() / size[ok].sum())\n"
            "worst = tri[3].idxmin()\n"
            "result = {'anomalous_cohort': str(worst), 'm3_anomalous': float(tri.loc[worst, 3]), 'm3_others': wavg(3, worst), 'm6_weighted': wavg(6)}\n"
            "try:\n"
            "    import matplotlib\n"
            "    matplotlib.use('Agg')\n"
            "    import matplotlib.pyplot as plt\n"
            "    plt.imshow(tri.values.astype(float), aspect='auto', cmap='viridis'); plt.colorbar(); plt.title('Retention triangle')\n"
            "except ImportError:\n"
            "    pass\n"
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, stakeholders, timeline (red herrings flagged)."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        tri = self._triangle(state)
        names = rng.pick_names(4)
        pm_label = state.extras["promo_month_label"]
        subs = state.extras["subs"]
        recent = subs[subs["started_at"] >= pd.Timestamp(cal.bounds(P[6])[0])]
        old = subs[(subs["started_at"] >= pd.Timestamp(cal.bounds(P[1])[0])) & (subs["started_at"] < pd.Timestamp(cal.bounds(P[2])[0]))]
        reveal = [
            f"Growth's slide: {recent['_active'].mean():.0%} of customers who signed up in {P[6]}-{P[7]} are still active vs {old['_active'].mean():.0%} of the {P[1]} signups - 'retention is improving'.",
            (f"A {PROMO_CODE} promotion ran in {pm_label}." if p["difficulty"] == "foundation"
             else f"A {PROMO_CODE} promotion (50% off the first three months) ran last {_season(state.extras['months'][tri['promo_m']])}."),
            f"Billing was migrated to a new provider in {P[2]}.",
            "The board asked for a proper cohort view before approving next year's acquisition budget.",
        ]
        conceal = [
            f"the correct retention matrix and that the {pm_label} promo cohort collapses after the discount",
            "right-censoring is what makes young cohorts look better",
            "reactivation handling (gaps and islands) and the deduplication of re-ingested subscription rows",
            "time-zone bucketing of signup months", "A1", "A2",
        ]
        stakeholders = [
            Stakeholder(name=names[0], role="Head of Growth", agenda="Defend the promo and the slide.", disposition="enthusiastic",
                        knows=["the promo doubled signups"], hides=["promo customers were never tracked separately"]),
            Stakeholder(name=names[1], role="Senior Analyst", agenda="Rebuild the cohort view properly.", disposition="methodical",
                        knows=["the slide used a quick query"], hides=[]),
            Stakeholder(name=names[2], role="CFO", agenda="An LTV the board can trust.", disposition="skeptical",
                        knows=["CAC per channel"], hides=[]),
            Stakeholder(name=names[3], role="Data Engineer", agenda="Avoid blame for the migration.", disposition="candid",
                        knows=["the billing migration re-ingested some subscriptions", "payment-failure events sync late"], hides=[]),
        ]
        tl = [
            TimelineEvent(period=P[2], headline="Billing migration", detail="Subscriptions re-ingested from the new provider."),
            TimelineEvent(period=cal.labels([pd.Timestamp(state.extras["months"][tri["promo_m"]])])[0], headline=f"{PROMO_CODE} promotion",
                          detail="50% off the first three months on paid social."),
            TimelineEvent(period=P[6], headline="'Retention is improving' slide", detail="Shown at the monthly business review."),
            TimelineEvent(period=P[7], headline="Board asks for a cohort view", detail="Before approving next year's acquisition budget."),
            TimelineEvent(period=P[3], headline="Pricing page redesign", detail="New plan names, prices unchanged.", is_red_herring=True),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[5], headline="Winback emails launched", detail="Churned customers invited back.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["retention_slide", "promo_brief", "billing_migration_note", "board_request_email"], architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Promo design and retention levers."""
        return [
            Lever(key="promo_discount_pct", label="Next promo discount", min=0, max=60, default=50, unit="%", effect="More signups, worse retention."),
            Lever(key="promo_min_commitment_months", label="Minimum commitment on promo signups", min=0, max=12, default=0, unit="months",
                  effect="Fewer promo signups; removes the post-discount cliff."),
            Lever(key="winback_campaign", label="Win-back campaign (0/1)", min=0, max=1, default=0, unit="bool", effect="Reactivations up; costs marketing."),
            Lever(key="annual_plan_push_pct", label="Share of signups steered to annual plans", min=0, max=50, default=0, unit="%", effect="Lower churn, discount cost."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced form: quarterly KPIs around P8 with cohort-level effects."""
        p = state.params
        tri = self._triangle(state)
        P8 = state.periods[-1]
        inc8 = next(i for i in state.statements.income if i.period == P8)  # type: ignore[union-attr]
        size = tri["size"]
        base_new = float(np.mean(size[-6:])) * 3
        promo_new = float(size[tri["promo_m"]]) * (1 - 1 / p["promo_boost"])
        kpis = {
            "new_customers_q": base_new, "promo_customers_q": promo_new, "m3_retention_promo": tri["m3_promo"], "m3_retention_blended": tri["m3_base"],
            "m6_retention": tri["m6_all"], "reactivations_q": float(tri["reactivation_share"] * base_new),
            "revenue_cents": float(inc8.revenue_cents), "net_income_cents": float(inc8.net_income_cents), "marketing_cost_cents": 0.0,
        }
        arpa = float(state.frames["plans"]["price_cents"].mean())
        gap = tri["m3_base"] - tri["m3_promo"]
        effects = [
            {"lever": "promo_discount_pct", "kpi": "promo_customers_q", "delta_per_unit": promo_new / 50.0, "lag_periods": 0},
            {"lever": "promo_discount_pct", "kpi": "m3_retention_promo", "delta_per_unit": -gap / 50.0, "lag_periods": 1},
            {"lever": "promo_discount_pct", "kpi": "revenue_cents", "delta_per_unit": -promo_new * arpa * 3 * 0.01, "lag_periods": 0},
            {"lever": "promo_min_commitment_months", "kpi": "m3_retention_promo", "delta_per_unit": gap / 3.0, "lag_periods": 1},
            {"lever": "promo_min_commitment_months", "kpi": "promo_customers_q", "delta_per_unit": -promo_new * 0.05, "lag_periods": 0},
            {"lever": "promo_min_commitment_months", "kpi": "m3_retention_blended", "delta_per_unit": gap * 0.02, "lag_periods": 1},
            {"lever": "winback_campaign", "kpi": "reactivations_q", "delta_per_unit": 0.5 * kpis["reactivations_q"] + 5, "lag_periods": 0},
            {"lever": "winback_campaign", "kpi": "marketing_cost_cents", "delta_per_unit": 0.02 * float(inc8.revenue_cents), "lag_periods": 0},
            {"lever": "winback_campaign", "kpi": "net_income_cents", "delta_per_unit": -0.015 * float(inc8.revenue_cents), "lag_periods": 0},
            {"lever": "annual_plan_push_pct", "kpi": "m6_retention", "delta_per_unit": 0.002, "lag_periods": 1},
            {"lever": "annual_plan_push_pct", "kpi": "revenue_cents", "delta_per_unit": -0.0008 * float(inc8.revenue_cents), "lag_periods": 0},
        ]
        drag = {"m6_retention": -0.002, "revenue_cents": -promo_new * gap * arpa}
        bounds = {k: [0.0, 1.0] for k in ("m3_retention_promo", "m3_retention_blended", "m6_retention")}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag, "bounds": bounds,
                "resolved_if": {"promo_min_commitment_months": 3.0}, "max_quarters": 4}
