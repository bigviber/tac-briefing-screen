"""``pricing_elasticity`` - "Pricing Power, or Not" (archetypes.md section 8).

A B2B e-commerce distributor (web store + account reps; or a specialty retailer's trade channel)
raised list prices uniformly in P6 on a consultant's "headroom" study that assumed one elasticity of
-0.5. Revenue is flat, gross margin % is up and management calls it pricing power - but gross-margin
dollars and contribution fell: the price-shopper segment is elastic, its volume fell, and the reps
gave much of the increase back as discounts. The list-price view of the price/volume/mix bridge
overstates the price effect; only pocket prices (the Marn & Rosiello waterfall) tell the truth.

Mechanics (every invariant and sub-ledger tie holds)
----------------------------------------------------
* Weekly demand per segment x SKU: ``Q = Q0 * trend * r_t * competitor * LN(0, 0.1)`` (stochastic rounding to units,
  split into order lines of the segment's typical size), where the
  response ``r_t`` partially adjusts (``r_t = r_{t-1} + 0.5 (r* - r_{t-1})``, 2-4 week lag) to
  ``r* = (pocket_t / pocket_0)^eps_seg``; pocket = list x (1 - discount). Segment elasticities are
  heterogeneous: price_shopper ``-U(2.6, 3.6)`` (62-72% of volume), contract_buyer ``-U(0.8, 1.2)``,
  loyal_account ``-U(0.3, 0.65)`` (+ new_customer ``-U(1.0, 1.5)`` at principal).
* **A1** uniform list increase ``+U(8, 18)%`` (difficulty-scaled) in week 1 of P6 (``price_history``).
* **A2** discount leakage: pocket discount ``U(6, 12)%``; after the increase the price shoppers' rep
  discount ramps up so that leakage eats ``U(35, 60)%`` of the increase (typically to 12-16%; GL 4110 grows).
* **A3** competitor response (seeded, 50%): ``competitor_prices`` -5% in P7 on the price shoppers'
  favourite category; their volume there falls by the cross-elasticity (2.0).
* **A4** (principal) cannibalization: a cheaper substitute SKU gains volume from the hero SKUs
  (cross-elasticity ``U(0.2, 0.5)``).
* The generator re-draws (up to 20x) until, in expectation, aggregate contribution P7 falls >= 5% below
  P5 while the loyal segment's contribution rises (the answer is segmented pricing, not a blanket
  reversal), then verifies the realised order lines (up to 6 regenerations) so P7 contribution < P5.
* Order lines are sampled from the expected units (Poisson line counts, SKU mix by expected units);
  invoices at order date: Dr 1100 list / Cr 4000, discounts Dr 4110 / Cr 1100, COGS Dr 5000 / Cr 1220,
  freight 5030; inventory replenished monthly (Dr 1220 / Cr 2000). Ties: AR, revenue (list), discounts
  (4110), AP.

Documented simplifications
* ``saas`` / ``fintech_marketplace`` are not offered (ecommerce default, retail variant).
* Elasticities are drawn from uniform bands consistent with ``operations.elasticity_*`` (durables/apparel
  p10-p30 for the shoppers) rather than per-category draws, and the gross margin from the upper benchmark band
  (58-66%): with the spec's own ranges (shoppers -1.6..-2.4, ~50% margin) a uniform increase almost never lowers
  aggregate contribution, so the redraw condition could not be met. If 11 redraws fail the competitor responds.
* Ground-truth elasticities are OLS estimates on the generated segment x category x week panel
  (``ln Q ~ ln pocket index + trend + ln competitor category index + category FE``), excluding the
  eight weeks of partial adjustment after the increase.

Simulation (reduced form, KPIs = P8 quarter): ``price_change_shopper_pct`` (signed; a cut is negative) and ``price_change_loyal_pct``
move segment units by their elasticities (half in the quarter, half the next: partial adjustment)
and revenue / contribution accordingly; ``discount_cap_pts`` (tighter rep authority) raises the
pocket price of the shoppers (and costs some volume); ``fence_feature`` enables segmented pricing at a
cost of ~0.75% of revenue; ``match_competitor`` recovers 60% of the competitor-driven volume loss at a
margin cost. ``resolved_if``: fence + a shopper price cut toward the Lerner optimum (``price_change_shopper_pct <= -threshold``).
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
    constant_elasticity_demand,
    mm1,
    optimal_price,
    pocket_price_waterfall,
)
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

logger = logging.getLogger("axiom.archetypes.pricing_elasticity")

SEGMENTS = ("price_shopper", "contract_buyer", "loyal_account", "new_customer")
CATEGORIES = ("Fasteners", "Power tools", "Safety", "Janitorial")
AVG_QTY = {"price_shopper": 2.0, "contract_buyer": 6.0, "loyal_account": 3.5, "new_customer": 2.5}
COMP_CROSS = 2.0
EXCLUDE_WEEKS = 8


@register
class PricingElasticity(Archetype):
    """Pricing Power, or Not: a uniform price increase meets heterogeneous elasticities and discount leakage."""

    key = "pricing_elasticity"
    title = "Pricing Power, or Not"
    pillars = ("operations_strategy", "accounting_finance", "python", "sql")
    industries = ("ecommerce", "retail")
    schema_template = "ecommerce_oms"
    summary = ("After a uniform list-price increase revenue is flat and gross margin % is up - management calls it pricing power. "
               "Is it? Gross-margin dollars fell and the sales team wants more discount authority.")
    root_cause_terms = ("segment elasticities", "leakage magnitude", "optimal prices", "counterfactual")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 8 (re-drawn until the contribution condition holds, see ``inject_anomalies``)."""
        ind = industry or "ecommerce"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        rev_usd = math.exp(rng.uniform(math.log(20e6), math.log(120e6)))
        # upper benchmark band: at these margins the elastic segment sits above its Lerner optimum (margin > 1/|eps|)
        gm = float(np.clip(bk.sample_between(pool, rng.spawn("bench:gm"), f"{ind}.gross_margin", ind, 0.8, 0.95), 0.58, 0.66))
        n_seg = difficulty_pick(difficulty, 3, 3, 4)
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": {"ecommerce": "B2B e-commerce distributor of tools and MRO supplies (web store + account reps)",
                               "retail": "specialty tools retailer with a trade-counter and web channel"}[ind],
            "revenue_ttm_cents": int(rev_usd * 100),
            "gm": gm,
            "segments": list(SEGMENTS[:n_seg]),
            "n_categories": difficulty_pick(difficulty, 3, 3, 4),
            "price_change_pct": magnitude(rng, difficulty, 0.08, 0.18),
            "leak_share": 0.5,
            "lag_weeks": int(rng.integers(2, 5)),
            "adjust_speed": 0.5,
            "competitor_response": bool(rng.random() < 0.5),
            "competitor_cut": 0.05,
            "trend_monthly": float(rng.uniform(0.005, 0.009)),
            "cannibalization": difficulty == "principal",
            "cross_elasticity": float(rng.uniform(0.2, 0.5)),
            "terms_days": 30,
            "supplier_terms_days": 45,
            "freight_pct": float(rng.uniform(0.025, 0.04)),
            "op_margin": float(rng.uniform(0.06, 0.10)),
            "tax_rate": 0.24,
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        self._draw_segments(rng.spawn("segments:0"), p)
        budget = ROW_BUDGET[difficulty]
        gl_rows = difficulty_pick(difficulty, 2600, 2900, 11000)
        p["target_lines"] = int(max(1500, (budget * 0.9 - gl_rows - 2200) / 1.55))
        p["n_skus"] = difficulty_pick(difficulty, 24, 40, 60)
        p["description"] = (f"{cap_first(p['business_model'])}; {len(p['segments'])} customer segments buy through the same price list, "
                            "with account reps authorised to discount.")
        return p

    def _draw_segments(self, rng: SeededRNG, p: dict[str, Any]) -> None:
        """Segment elasticities, volume shares and discounts. The shoppers' post-increase discount is drawn so that leakage eats
        ``leak_share`` = U(35%, 60%) of the list increase in their pocket price (spec: "leakage ate half the increase")."""
        bands = {"price_shopper": (2.6, 3.6), "contract_buyer": (0.8, 1.2), "loyal_account": (0.3, 0.65), "new_customer": (1.0, 1.5)}
        shares = {"price_shopper": rng.uniform(0.62, 0.72), "contract_buyer": rng.uniform(0.12, 0.2), "loyal_account": rng.uniform(0.12, 0.18),
                  "new_customer": rng.uniform(0.06, 0.1)}
        segs = p["segments"]
        w = np.array([shares[s] for s in segs])
        p["eps"] = {s: -float(rng.uniform(*bands[s])) for s in segs}
        p["volume_share"] = {s: float(x) for s, x in zip(segs, w / w.sum(), strict=True)}
        p["disc_base"] = {s: float(rng.uniform(0.06, 0.12)) for s in segs}
        p["leak_share"] = float(rng.uniform(0.35, 0.6))
        self._set_post_discount(p)

    @staticmethod
    def _set_post_discount(p: dict[str, Any]) -> None:
        d0, inc = p["disc_base"]["price_shopper"], p["price_change_pct"]
        p["disc_post_shopper"] = float(1 - (1 - d0) * (1 + inc * (1 - p["leak_share"])) / (1 + inc))

    # ------------------------------------------------------------------ expected-value model (used for the draw condition)

    def _expected_contribution(self, p: dict[str, Any], *, competitor: bool) -> dict[str, float]:
        """Steady-state contribution ratio P7 / P5 per segment (pocket-price elasticity, competitor cut, leakage)."""
        out = {}
        for s in p["segments"]:
            d0 = p["disc_base"][s]
            d1 = p["disc_post_shopper"] if s == "price_shopper" else d0
            pocket0 = 1 - d0
            pocket1 = (1 + p["price_change_pct"]) * (1 - d1)
            cost = pocket0 * (1 - p["gm"])
            q = (pocket1 / pocket0) ** p["eps"][s] * (1 + p["trend_monthly"]) ** 6  # P7 vs P5: six months of underlying growth
            if competitor and s == "price_shopper":
                q *= (1 - p["competitor_cut"]) ** COMP_CROSS * 0.6 + 0.4  # the cut hits the shoppers' favourite category (~60% of their volume)
            out[s] = q * (pocket1 - cost) / (pocket0 - cost)
        tot0 = sum(p["volume_share"][s] * ((1 - p["disc_base"][s]) * p["gm"]) for s in p["segments"])  # P5 contribution weights
        tot1 = sum(p["volume_share"][s] * ((1 - p["disc_base"][s]) * p["gm"]) * out[s] for s in p["segments"])
        out["_total"] = tot1 / tot0
        return out

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Weeks, SKUs (categories, base list prices), customers by segment and segment-category preferences."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=False)  # base working-capital defaults: prepaid expenses (AP already comes from the purchasing / cost sub-ledger)
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        start = pd.Timestamp(cal.start)
        first_monday = start + pd.Timedelta(days=(7 - start.weekday()) % 7)
        n_w = int((pd.Timestamp(cal.end) - first_monday).days // 7) + 1
        weeks = pd.DatetimeIndex([first_monday + pd.Timedelta(weeks=k) for k in range(n_w)])
        S = p["n_skus"]
        K = p["n_categories"]
        cat = np.arange(S) % K
        g.shuffle(cat)
        list0 = np.round(np.clip(g.lognormal(math.log(4500), 0.7, S), 400, 60000)).astype(np.int64)  # cents (rescaled later)
        pop = 1 + g.pareto(1.5, S)
        segs = p["segments"]
        pref = np.ones((len(segs), K))
        pref[segs.index("price_shopper"), 0] = 4.0  # shoppers concentrate on category 0 (where the competitor cuts)
        if "loyal_account" in segs:
            pref[segs.index("loyal_account"), K - 1] = 2.0
        state.extras.update({"weeks": weeks, "n_w": n_w, "cat": cat, "list0_raw": list0, "pop": pop, "pref": pref, "months": cal.month_starts()})
        n_c = {"price_shopper": 900, "contract_buyer": 120, "loyal_account": 260, "new_customer": 300}
        scale = difficulty_pick(p["difficulty"], 0.35, 0.7, 1.0)
        rows = []
        for s in segs:
            for i in range(max(30, int(n_c[s] * scale))):
                rows.append((f"C{len(rows) + 1:05d}", s))
        cust = pd.DataFrame(rows, columns=["customer_id", "segment"])
        cust["region"] = np.array(("Northeast", "Southeast", "Midwest", "West", "Southwest"))[g.integers(0, 5, len(cust))]
        cust["acquisition_channel"] = np.where(cust["segment"] == "price_shopper", "marketplace_search", np.where(cust["segment"] == "contract_buyer", "outbound", "inbound"))
        cust["sales_rep_id"] = np.array([f"REP{k:02d}" for k in range(1, 13)])[g.integers(0, 12, len(cust))]
        cust["email"] = [f"buyer{i:05d}@example.com" for i in range(len(cust))]
        cust["created_at"] = start - pd.to_timedelta(g.integers(30, 1500, len(cust)), unit="D")
        state.frames["customers"] = cust

    # ------------------------------------------------------------------ anomalies

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Enforce the contribution condition (re-draw), then build price/discount schedules and expected demand."""
        p = state.params
        rng = ctx.rng.spawn("anomaly")
        P = state.periods
        for k in range(1, 21):
            e = self._expected_contribution(p, competitor=p["competitor_response"])
            if e["_total"] < 0.95 and e["loyal_account"] > 1.0 and e["price_shopper"] < 1.0:  # margin for sampling noise
                break
            self._draw_segments(rng.spawn(f"segments:{k}"), p)
            if k >= 12:  # last resort: the competitor responds (the spec's A3), which the shoppers feel on their main category
                p["competitor_response"] = True
        # verify on the realised order lines (sampling noise, trend, competitor share): re-draw until P7 contribution < P5
        keys = ("eps", "volume_share", "disc_base", "leak_share", "disc_post_shopper", "competitor_response")
        best: tuple[float, dict[str, Any]] | None = None
        for attempt in range(6):
            p["expected_contribution"] = self._expected_contribution(p, competitor=p["competitor_response"])
            self._schedules(ctx, state)
            self._derive(ctx, state)
            change = self._realised_contribution_change(state)
            p["generation_attempts"] = attempt + 1
            if best is None or change < best[0]:
                best = (change, {k_: (dict(p[k_]) if isinstance(p[k_], dict) else p[k_]) for k_ in keys})
            if change < 0:
                break
            self._draw_segments(rng.spawn(f"realised:{attempt}"), p)
            p["eps"]["price_shopper"] = min(p["eps"]["price_shopper"], -3.0)
            if attempt >= 2:
                p["competitor_response"] = True
        else:  # no attempt met the condition (small principal increases): regenerate the best one
            assert best is not None
            p.update(best[1])
            p["expected_contribution"] = self._expected_contribution(p, competitor=p["competitor_response"])
            self._schedules(ctx, state)
            self._derive(ctx, state)
        comp_txt = "matched with a -5% cut on the shoppers' main category" if p["competitor_response"] else "did not respond"
        an = [
            Anomaly(id="A1", kind="uniform_price_increase", pillar="operations_strategy", title="Uniform list-price increase across heterogeneous segments",
                    mechanism=(f"List prices +{p['price_change_pct']:.1%} on every SKU in week 1 of {P[5]}; volume responds by segment pocket-price "
                               f"elasticity ({', '.join(f'{s} {v:.2f}' for s, v in p['eps'].items())}) with a {p['lag_weeks']}-week lag and 50%/week "
                               "partial adjustment."),
                    periods=list(P[5:]), affected_accounts=["4000", "5000"], affected_tables=["price_history", "order_items", "orders"],
                    magnitude={"price_change_pct": round(p["price_change_pct"], 6), **{f"eps_{s}": round(v, 6) for s, v in p["eps"].items()}}, severity="high"),
            Anomaly(id="A2", kind="discount_leakage", pillar="accounting_finance", title="Reps give the increase back as discounts",
                    mechanism=(f"Price shoppers' average discount rises from {p['disc_base']['price_shopper']:.1%} to {p['disc_post_shopper']:.1%} after the "
                               "increase (rep discretion); pocket price rises far less than list; GL 4110 grows."),
                    periods=list(P[5:]), affected_accounts=["4110"], affected_tables=["order_items"],
                    magnitude={"discount_before": round(p["disc_base"]["price_shopper"], 6), "discount_after": round(p["disc_post_shopper"], 6)},
                    severity="medium"),
            Anomaly(id="A3", kind="competitor_response", pillar="operations_strategy", title="Competitor response",
                    mechanism=f"In {P[6]} the main competitor {comp_txt}.",
                    periods=[P[6], P[7]], affected_accounts=["4000"], affected_tables=["competitor_prices"],
                    magnitude={"competitor_response_flag": 1.0 if p["competitor_response"] else 0.0}, severity="low"),
        ]
        if p["cannibalization"]:
            an.append(Anomaly(id="A4", kind="cannibalization", pillar="operations_strategy", title="Cannibalization by a cheaper substitute SKU",
                              mechanism=f"Hero SKUs lose volume to a cheaper in-house substitute (cross-elasticity {p['cross_elasticity']:.2f}).",
                              periods=list(P[5:]), affected_accounts=["4000", "5000"], affected_tables=["order_items", "products"],
                              magnitude={"cross_elasticity": round(p["cross_elasticity"], 6)}, severity="low"))
        return an

    def _realised_contribution_change(self, state: WorldState) -> float:
        """(Net revenue - COGS) of the generated order lines, P7 minus P5."""
        it = state.frames["order_items"]
        a5, b5 = self._window(state, state.periods[4])
        a7, b7 = self._window(state, state.periods[6])
        c = (it["net_price_cents"] - it["unit_cost_cents"]) * it["quantity"]
        return float(c[(it["_w"] >= a7) & (it["_w"] < b7)].sum() - c[(it["_w"] >= a5) & (it["_w"] < b5)].sum())

    @staticmethod
    def _renorm(shares: dict[str, float], key: str, value: float) -> dict[str, float]:
        rest = 1 - value
        others = {k: v for k, v in shares.items() if k != key}
        tot = sum(others.values())
        out = {k: v / tot * rest for k, v in others.items()}
        out[key] = value
        return out

    def _schedules(self, ctx: BuildContext, state: WorldState) -> None:
        """List prices, segment discounts, competitor index and expected units per segment x SKU x week."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("schedules").generator
        E = state.extras
        weeks, n_w = E["weeks"], E["n_w"]
        S, segs = p["n_skus"], p["segments"]
        G = len(segs)
        inc_w = int(np.searchsorted(weeks, pd.Timestamp(cal.bounds(state.p(6))[0])))
        p7_w = int(np.searchsorted(weeks, pd.Timestamp(cal.bounds(state.p(7))[0])))
        E["inc_w"], E["p7_w"] = inc_w, p7_w
        # list price index per SKU-week: small idiosyncratic changes + the uniform increase
        lidx = np.ones((S, n_w))
        hist = []
        for s in range(S):
            for _ in range(int(g.integers(2, 4))):
                w0 = int(g.integers(4, n_w - 4))
                if abs(w0 - inc_w) < 3:
                    continue
                ch = float(g.choice([-1, 1]) * g.uniform(0.01, 0.04))
                lidx[s, w0:] *= 1 + ch
                hist.append((s, w0, ch))
        lidx[:, inc_w:] *= 1 + p["price_change_pct"]
        hist += [(s, inc_w, p["price_change_pct"]) for s in range(S)]
        E["list_idx"], E["price_hist"] = lidx, hist
        # segment discounts by week (shopper discount ramps over 4 weeks after the increase)
        disc = np.zeros((G, n_w))
        for i, sg in enumerate(segs):
            disc[i] = p["disc_base"][sg]
            if sg == "price_shopper":
                ramp = np.clip((np.arange(n_w) - inc_w + 1) / 4.0, 0, 1)
                disc[i] = p["disc_base"][sg] + ramp * (p["disc_post_shopper"] - p["disc_base"][sg])
        E["disc"] = disc
        # competitor price index per category-week
        K = p["n_categories"]
        comp = np.ones((K, n_w)) * (1 + 0.01 * np.sin(np.arange(n_w)[None, :] / 9.0 + np.arange(K)[:, None]))
        if p["competitor_response"]:
            comp[0, p7_w:] *= 1 - p["competitor_cut"]
        E["comp"] = comp
        # expected units: base x trend x partial-adjustment response x competitor x cannibalization x noise
        cat = E["cat"]
        pocket_rel = lidx[None, :, :] * (1 - disc[:, None, :]) / (1 - disc[:, None, :1])  # pocket_t / pocket_0
        eps = np.array([p["eps"][s] for s in segs])
        target = pocket_rel ** eps[:, None, None]
        r = np.ones((G, S, n_w))
        lag = p["lag_weeks"]
        for t in range(1, n_w):
            tgt = target[:, :, max(0, t - lag)]
            r[:, :, t] = r[:, :, t - 1] + p["adjust_speed"] * (tgt - r[:, :, t - 1])
        trend = (1 + p["trend_monthly"]) ** (np.arange(n_w) / 4.345)
        share = np.array([p["volume_share"][s] for s in segs])
        base = share[:, None] * (E["pop"][None, :] * E["pref"][:, cat]) / (E["pop"][None, :] * E["pref"][:, cat]).sum(axis=1, keepdims=True)
        compf = np.ones((G, S, n_w))
        if p["competitor_response"]:
            i = segs.index("price_shopper")
            compf[i] = (comp[cat] / comp[cat][:, :1]) ** COMP_CROSS
        cann = np.ones((G, S, n_w))
        if p["cannibalization"]:
            hero = np.argsort(-E["pop"], kind="stable")[:3]
            sub = int(np.argsort(E["pop"], kind="stable")[0])
            lift = (lidx[hero, :].mean(axis=0) - 1) * p["cross_elasticity"]
            cann[:, sub, :] = 1 + lift[None, :] * E["pop"][hero].sum() / E["pop"][sub]
            E["cann"] = {"hero": hero.tolist(), "sub": sub}
        noise = g.lognormal(-0.005, 0.1, (G, S, n_w))
        E["expected_units_rel"] = base[:, :, None] * trend[None, None, :] * r * compf * cann * noise
        E["response"] = r

    # ------------------------------------------------------------------ order lines

    def _derive(self, ctx: BuildContext, state: WorldState) -> None:
        """Sample order lines, orders, price history, competitor prices, marketing, purchases."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        E = state.extras
        weeks, n_w = E["weeks"], E["n_w"]
        segs = p["segments"]
        S = p["n_skus"]
        U = E["expected_units_rel"]
        avg_q = np.array([AVG_QTY[s] for s in segs])
        lines_rel = U.sum(axis=1) / avg_q[:, None]  # (G, n_w)
        k = p["target_lines"] / lines_rel.sum()
        # units per segment x SKU x week follow the expected demand (its LN(0, 0.1) noise is the only demand noise); stochastic
        # rounding keeps small cells unbiased; each cell is split into order lines of ~avg_q units
        u_draw = g.random(U.shape)

        def realise(scale_: float) -> np.ndarray:
            raw = U * scale_
            fl_ = np.floor(raw)
            return (fl_ + (u_draw < raw - fl_)).astype(np.int64)

        def n_lines_of(u: np.ndarray) -> int:
            return int(np.where(u > 0, np.maximum(1, np.round(u / avg_q[:, None, None])), 0).sum())

        units = realise(k)
        units = realise(k * p["target_lines"] / max(1, n_lines_of(units)))  # one correction pass on the realised line count
        gi, si, ti = np.nonzero(units)
        cell_u = units[gi, si, ti]
        n_l = np.maximum(1, np.round(cell_u / avg_q[gi]).astype(np.int64))
        rep_idx = np.repeat(np.arange(len(gi)), n_l)
        pos_in = np.arange(len(rep_idx)) - np.repeat(np.cumsum(n_l) - n_l, n_l)
        base_q = cell_u[rep_idx] // n_l[rep_idx]
        qty = base_q + (pos_in < (cell_u[rep_idx] - base_q * n_l[rep_idx])).astype(np.int64)
        sg, sku, wk = gi[rep_idx], si[rep_idx], ti[rep_idx]
        cust = state.frames["customers"]
        L = len(sg)
        disc = np.clip(E["disc"][sg, wk] + g.normal(0, 0.005, L), 0.0, 0.4)  # small rep discretion around the segment's discount
        # price scale: calibrate the base list so TTM net revenue ~= target
        list_rel = E["list0_raw"][sku] * E["list_idx"][sku, wk]
        last52 = wk >= n_w - 52
        net_rel = float((qty * list_rel * (1 - disc))[last52].sum())
        scale = p["revenue_ttm_cents"] / net_rel
        list0 = np.maximum(100, np.round(E["list0_raw"] * scale)).astype(np.int64)
        list_c = np.round(list0[sku] * E["list_idx"][sku, wk]).astype(np.int64)
        net_c = np.round(list_c * (1 - disc)).astype(np.int64)
        base_pocket = list0 * (1 - np.mean(list(p["disc_base"].values())))
        unit_cost = np.round(base_pocket * (1 - p["gm"])).astype(np.int64)
        E["list0"], E["unit_cost"] = list0, unit_cost
        # customers and orders: group lines of the same segment-week into orders of 1-3 lines
        order = np.lexsort((g.random(L), wk, sg))
        sg, wk, sku, qty, disc, list_c, net_c = (x[order] for x in (sg, wk, sku, qty, disc, list_c, net_c))
        new_order = np.ones(L, dtype=bool)
        size_left = 0
        for j in range(L):
            if j > 0 and sg[j] == sg[j - 1] and wk[j] == wk[j - 1] and size_left > 0:
                new_order[j] = False
                size_left -= 1
            else:
                size_left = int(g.integers(0, 3))
        oid = np.cumsum(new_order)
        n_o = int(oid[-1])
        seg_of_order = sg[new_order]
        wk_of_order = wk[new_order]
        cust_by_seg = {i: cust.index[cust["segment"] == s].to_numpy() for i, s in enumerate(segs)}
        cidx = np.array([cust_by_seg[i][int(g.integers(0, len(cust_by_seg[i])))] for i in seg_of_order])
        ots = weeks[wk_of_order] + pd.to_timedelta(g.integers(0, 5, n_o), unit="D") + pd.to_timedelta(g.integers(7 * 3600, 19 * 3600, n_o), unit="s")
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        ots = pd.Series(ots).where(pd.Series(ots) <= end_ts, end_ts - pd.Timedelta(hours=2))
        items = pd.DataFrame({"order_item_id": [f"OI{i:07d}" for i in range(1, L + 1)], "_o": oid - 1, "product_idx": sku, "quantity": qty.astype(np.int64),
                              "list_price_cents": list_c, "discount_pct": np.round(disc * 100, 2), "net_price_cents": net_c,
                              "unit_cost_cents": unit_cost[sku], "_seg": sg, "_w": wk})
        items["line_list_cents"] = items["quantity"] * items["list_price_cents"]
        items["line_net_cents"] = items["quantity"] * items["net_price_cents"]
        items["line_discount_cents"] = items["line_list_cents"] - items["line_net_cents"]
        o_list = np.bincount(oid - 1, weights=items["line_list_cents"].to_numpy(), minlength=n_o).astype(np.int64)
        o_disc = np.bincount(oid - 1, weights=items["line_discount_cents"].to_numpy(), minlength=n_o).astype(np.int64)
        o_cost = np.bincount(oid - 1, weights=(items["quantity"] * items["unit_cost_cents"]).to_numpy(), minlength=n_o).astype(np.int64)
        pay = ots.dt.normalize() + pd.to_timedelta(p["terms_days"] + np.maximum(-10, g.normal(4, 9, n_o)), unit="D") + pd.Timedelta(hours=10)
        paid = (pay <= end_ts).to_numpy()
        orders = pd.DataFrame({"order_id": [f"SO{i:06d}" for i in range(1, n_o + 1)], "customer_id": cust["customer_id"].to_numpy()[cidx],
                               "order_ts": ots, "channel": np.where(seg_of_order == segs.index("price_shopper"), "web", np.where(g.random(n_o) < 0.5, "rep", "web")),
                               "status": "invoiced", "subtotal_cents": o_list, "discount_cents": o_disc, "total_cents": o_list - o_disc,
                               "currency": "USD", "paid_at": pay.where(paid), "_cost": o_cost,
                               "sales_rep_id": cust["sales_rep_id"].to_numpy()[cidx]})
        orders["freight_cents"] = np.round(orders["total_cents"] * p["freight_pct"]).astype(np.int64)
        items["order_id"] = orders["order_id"].to_numpy()[items["_o"].to_numpy()]
        prod = pd.DataFrame({"product_id": [f"P{i:04d}" for i in range(1, S + 1)], "sku": [f"MRO-{3000 + 11 * i}" for i in range(S)],
                             "title": [f"{CATEGORIES[c]} item {i + 1}" for i, c in enumerate(E["cat"])], "category": np.array(CATEGORIES)[E["cat"]],
                             "unit_cost_cents": unit_cost, "list_price_cents": np.round(list0 * E["list_idx"][:, -1]).astype(np.int64),
                             "supplier_id": "V001", "active": True})
        if p["cannibalization"]:
            prod["substitute_for"] = None
            prod.loc[E["cann"]["sub"], "substitute_for"] = ",".join(prod["product_id"].to_numpy()[E["cann"]["hero"]])
        items["product_id"] = prod["product_id"].to_numpy()[items["product_idx"].to_numpy()]
        # price history (SKU, effective_from, list price, change_pct)
        ph = [(prod["product_id"].iat[s], pd.Timestamp(cal.start), int(list0[s]), 0.0) for s in range(S)]
        cur = E["list_idx"]
        for s, w0, ch in E["price_hist"]:
            ph.append((prod["product_id"].iat[s], weeks[w0], int(round(list0[s] * cur[s, w0])), round(ch, 6)))
        price_history = pd.DataFrame(ph, columns=["product_id", "effective_from", "list_price_cents", "change_pct"]).sort_values(
            ["effective_from", "product_id"], kind="stable").reset_index(drop=True)
        price_history.insert(0, "price_id", np.arange(1, len(price_history) + 1, dtype=np.int64))
        # competitor scrape: 3 competitors x category x week (missing weeks)
        comp = E["comp"]
        rows = []
        for c in range(p["n_categories"]):
            ref = float(np.average(list0[E["cat"] == c])) if (E["cat"] == c).any() else float(list0.mean())
            for k_, name in enumerate(("Northline Supply", "ToolDepot Direct", "Hardline Pro")):
                lvl = ref * g.uniform(0.9, 1.05)
                # the main competitor (k_=0) follows the category index (incl. the P7 cut); the others drift slightly
                idx = comp[c] if k_ == 0 else 1 + 0.01 * np.sin(np.arange(n_w) / 7 + k_)
                for t in range(n_w):
                    rows.append((name, CATEGORIES[c], weeks[t] + pd.Timedelta(days=2), int(round(lvl * idx[t]))))
        cp = pd.DataFrame(rows, columns=["competitor", "category", "scraped_at", "price_index_cents"])
        keep = g.random(len(cp)) > 0.06  # scraper misses ~6% of weeks
        cp = cp[keep].sort_values(["scraped_at", "competitor", "category"], kind="stable").reset_index(drop=True)
        cp.insert(0, "scrape_id", np.arange(1, len(cp) + 1, dtype=np.int64))
        mk = pd.DataFrame({"week_start": weeks, "channel": "search_and_catalog",
                           "spend_cents": np.round(p["revenue_ttm_cents"] / 52 * 0.025 * g.lognormal(0, 0.05, n_w)).astype(np.int64)})
        state.frames.update({"orders": orders, "order_items": items, "products": prod, "price_history": price_history, "competitor_prices": cp,
                             "marketing_spend": mk})

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Post the books month by month (order lines were sampled and verified in ``inject_anomalies``)."""
        self._books(ctx, state)

    def _books(self, ctx: BuildContext, state: WorldState) -> None:
        p, cal = state.params, state.calendar
        E, F = state.extras, state.frames
        L = self.fresh_ledger(state)
        state.ledger = L
        gran = GL_BATCH[p["difficulty"]]
        months = E["months"]
        n_m = len(months)
        end_d = cal.end
        R = p["revenue_ttm_cents"]
        od = F["orders"]
        start = pd.Timestamp(cal.start)
        mi = ((od["order_ts"].dt.year - start.year) * 12 + od["order_ts"].dt.month - start.month).to_numpy()
        cogs_m = np.bincount(mi, weights=od["_cost"].to_numpy(), minlength=n_m + 1)
        cogs_m[n_m] = cogs_m[n_m - 1]
        cover = 1.6  # months of COGS on hand
        inv0 = int(round(cogs_m[0] * cover))
        ar0 = cents(R / 12 * p["terms_days"] / 30.4)
        ap0 = cents(cogs_m[0] * p["supplier_terms_days"] / 30.4)
        ppe = cents(R * 0.05)
        cash0 = cents(R * 0.06)
        net = cash0 + ar0 + inv0 + ppe - ap0
        cs = cents(0.6 * net)
        L.open_books(cal.opening_date, {"1000": cash0, "1100": ar0, "1220": inv0, "1500": ppe, "2000": -ap0, "3000": -cs, "3100": -(net - cs)})
        events: list[tuple[dt.date, int, Any]] = []

        def add(d: Any, prio: int, fn: Any) -> None:
            events.append((d.date() if isinstance(d, pd.Timestamp) else d, prio, fn))

        for k, a in enumerate(split_cents(ar0, [0.6, 0.4])):
            add(cal.start + dt.timedelta(days=12 + 14 * k), 4, lambda d, a=a: L.collect_cash(d, a, memo="Collection of opening receivables"))
        add(cal.start + dt.timedelta(days=20), 5, lambda d: L.pay_ap(d, ap0, memo="Opening payables paid"))
        # purchases: monthly receipts keep ~cover months of next month's COGS
        on_hand = inv0
        rec = []
        for m in range(n_m):
            amt = max(0, int(round(cover * cogs_m[m + 1] + cogs_m[m] - on_hand)))  # end the month with `cover` months of next month's COGS
            for j, a in enumerate(split_cents(amt, [1, 1])):
                rec.append((pd.Timestamp(months[m]) + pd.Timedelta(days=1 + 14 * j, hours=8), a))
            on_hand = on_hand + amt - int(cogs_m[m])
        po = pd.DataFrame(rec, columns=["received_at", "amount_cents"])
        po = po[po["amount_cents"] > 0].reset_index(drop=True)
        po.insert(0, "po_id", [f"PO{i:05d}" for i in range(1, len(po) + 1)])
        po["supplier_id"] = "V001"
        po["paid_at"] = po["received_at"].dt.normalize() + pd.Timedelta(days=p["supplier_terms_days"], hours=9)
        po.loc[po["paid_at"].dt.date > end_d, "paid_at"] = pd.NaT
        F["purchase_orders"] = po
        for _, r in po.iterrows():
            add(r.received_at, 2, lambda d, a=int(r.amount_cents): L.purchase_inventory(d, a, memo="Inventory receipts"))
            if pd.notna(r.paid_at):
                add(r.paid_at, 5, lambda d, a=int(r.amount_cents): L.pay_ap(d, a, memo="Supplier payments"))
        ob = od.assign(b=batch_end_dates(od["order_ts"], gran)).groupby("b", sort=True).agg(
            lst=("subtotal_cents", "sum"), dsc=("discount_cents", "sum"), cost=("_cost", "sum"), fr=("freight_cents", "sum"), n=("order_id", "size"))
        for b, r in ob.iterrows():
            dim = {"batch": f"sales:{b.date()}", "rows": str(int(r.n))}
            add(b, 3, lambda d, a=int(r.lst), dm=dim: L.sell_on_credit(d, a, memo="Orders invoiced at list price", dim=dm))
            add(b, 3, lambda d, a=int(r.dsc), dm=dim: L.entry(d, [("4110", a, 0, dm), ("1100", 0, a, dm)], "Discounts granted on invoices", "discounts"))
            add(b, 3, lambda d, a=int(r.cost), dm=dim: L.relieve_inventory_cogs(d, a, memo="COGS on orders", dim=dm))
            add(b, 3, lambda d, a=int(r.fr), dm=dim: L.entry(d, [("5030", a, 0, dm), ("2100", 0, a, dm)], "Outbound freight", "fulfillment"))
        pd_ = od[od["paid_at"].notna()]
        pb = pd_.assign(b=batch_end_dates(pd_["paid_at"], gran)).groupby("b", sort=True)["total_cents"].sum()
        for b, a in pb.items():
            add(b, 4, lambda d, a=int(a): L.collect_cash(d, a, memo="Customer payments"))
        mk = F["marketing_spend"]
        mk_m = mk.groupby(mk["week_start"].dt.strftime("%Y-%m"))["spend_cents"].sum()
        fr_m = od.groupby(od["order_ts"].dt.strftime("%Y-%m"))["freight_cents"].sum()
        opex_base = cents(R / 12 * max(0.05, p["gm"] - p["op_margin"] - p["freight_pct"] - 0.025))
        nz = self.period_noise(ctx, state)  # review C2: seasonal + idiosyncratic quarter noise
        prev = {"pay": 0, "accr": 0}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            if m > 0:
                add(ms + dt.timedelta(days=4), 1, lambda d, a=prev["pay"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll"))
                add(ms + dt.timedelta(days=9), 1, lambda d, a=prev["accr"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued expenses paid", source="opex"))
            if ms.month in (1, 4, 7, 10) and m > 0:
                add(ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"]))
            mkt = int(mk_m.get(f"{ms:%Y-%m}", 0))
            opex_m = cents(opex_base * nz.ox(ms))
            payroll = {"6000": cents(opex_m * 0.35), "6200": cents(opex_m * 0.25), "5010": cents(opex_m * 0.1)}
            accr = {"6000": mkt, "6200": cents(opex_m * 0.15), "6400": opex_m - sum(payroll.values()) - cents(opex_m * 0.15)}
            add(me, 7, lambda d, s_=payroll: L.payroll(d, s_, memo="Payroll (sales reps, G&A, warehouse)"))
            add(me, 7, lambda d, s_=accr: L.accrue_opex(d, s_, memo="Marketing, occupancy and G&A"))
            add(ms + dt.timedelta(days=14), 6, lambda d: L.capex(d, cents(R * 0.004), memo="Capex"))
            add(me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 84)))
            if ms.month % 3 == 0:
                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))
                add(me, 11, tax)
            prev = {"pay": sum(payroll.values()), "accr": sum(accr.values()) + int(fr_m.get(f"{ms:%Y-%m}", 0))}
        events.sort(key=lambda e: (e[0], e[1]))
        floor = cents(R / 12 * 0.1)
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

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Order-management tables + 2/4/6 dirt rules (never on GL-tied money columns)."""
        p, F = state.params, state.frames
        items = F["order_items"][["order_item_id", "order_id", "product_id", "quantity", "list_price_cents", "discount_pct", "net_price_cents",
                                  "unit_cost_cents", "line_list_cents", "line_discount_cents", "line_net_cents"]]
        orders = F["orders"].drop(columns=["_cost"])
        dirt_plan = [
            ("customers", "nulls:email:0.06"), ("competitor_prices", "case_mixed:competitor"), ("customers", "whitespace:region"),
            ("orders", "late_arriving:paid_at"), ("order_items", "dup_keys:0.01"), ("orders", "tz_mixed:order_ts"),
        ]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[: {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]]:
            dirt.setdefault(t, []).append(rule)
        if "late_arriving:paid_at" in dirt.get("orders", []):
            orders["created_at"] = orders["order_ts"]
        money = {c: "BIGINT" for c in ("list_price_cents", "net_price_cents", "unit_cost_cents", "line_list_cents", "line_discount_cents", "line_net_cents",
                                        "subtotal_cents", "discount_cents", "total_cents", "freight_cents", "spend_cents", "amount_cents", "price_index_cents")}
        builder.add_frame("customers", F["customers"], primary_key=["customer_id"], foreign_keys=[], dirt=dirt.get("customers", ()),
                          descriptions={"segment": "price_shopper | contract_buyer | loyal_account | new_customer (CRM segment)"})
        builder.add_frame("products", F["products"], primary_key=["product_id"], foreign_keys=[], column_types=money,
                          descriptions={"list_price_cents": "current list price", "unit_cost_cents": "landed unit cost"})
        builder.add_frame("orders", orders, primary_key=["order_id"], foreign_keys=[("customer_id", "customers", "customer_id")], column_types=money,
                          dirt=dirt.get("orders", ()), descriptions={"subtotal_cents": "at list price", "discount_cents": "discounts granted on the invoice",
                                                                     "total_cents": "invoiced (pocket) amount", "paid_at": "NULL while open"},
                          description="Customer orders, invoiced at order date (AR sub-ledger).")
        builder.add_frame("order_items", items, primary_key=["order_item_id"],
                          foreign_keys=[("order_id", "orders", "order_id"), ("product_id", "products", "product_id")],
                          column_types={**money, "quantity": "INTEGER", "discount_pct": "DOUBLE"}, dirt=dirt.get("order_items", ()),
                          descriptions={"net_price_cents": "pocket price per unit = list x (1 - discount)", "discount_pct": "rep / web discount (%)",
                                        "ingest_id": ">1 marks a re-ingested duplicate of the same order_item_id"})
        builder.add_frame("price_history", F["price_history"], primary_key=["price_id"], foreign_keys=[("product_id", "products", "product_id")],
                          column_types={**money, "price_id": "BIGINT", "effective_from": "DATE"},
                          descriptions={"change_pct": "relative change vs the previous list price"})
        builder.add_frame("competitor_prices", F["competitor_prices"], primary_key=["scrape_id"], foreign_keys=[], column_types={**money, "scrape_id": "BIGINT"},
                          dirt=dirt.get("competitor_prices", ()), description="Weekly competitor price scrape (category basket index; some weeks missing).")
        builder.add_frame("marketing_spend", F["marketing_spend"], primary_key=["week_start", "channel"], foreign_keys=[], column_types={**money, "week_start": "DATE"})
        builder.add_frame("purchase_orders", F["purchase_orders"], primary_key=["po_id"], foreign_keys=[], column_types=money,
                          descriptions={"paid_at": "NULL while unpaid"}, description="Inventory replenishment receipts (AP sub-ledger).")

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, AP, revenue at list and discounts - all hold."""
        cal, F = state.calendar, state.frames
        od, po = F["orders"], F["purchase_orders"]
        ar, ap, rev, dsc = {}, {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            s_ts = pd.Timestamp(s)
            inv = od["order_ts"] <= e_ts
            ar[period] = int(od.loc[inv & (od["paid_at"].isna() | (od["paid_at"] > e_ts)), "total_cents"].sum())
            inp = inv & (od["order_ts"] >= s_ts)
            rev[period] = int(od.loc[inp, "subtotal_cents"].sum())
            dsc[period] = int(od.loc[inp, "discount_cents"].sum())
            ap[period] = int(po.loc[(po["received_at"] <= e_ts) & (po["paid_at"].isna() | (po["paid_at"] > e_ts)), "amount_cents"].sum())
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Invoiced, unpaid orders (pocket amount) == GL 1100"),
            SubledgerTie("ap_open_receipts", ap, ["2000"], description="Received, unpaid replenishment == GL 2000"),
            SubledgerTie("revenue_at_list", rev, ["4000"], basis="activity", description="Orders at list price in period == GL 4000 activity"),
            SubledgerTie("discounts_granted", dsc, ["4110"], basis="activity", description="Invoice discounts in period == GL 4110 activity"),
        ]

    # ------------------------------------------------------------------ facts

    def _window(self, state: WorldState, period: str) -> tuple[int, int]:
        weeks = state.extras["weeks"]
        s, e = state.calendar.bounds(period)
        return int(np.searchsorted(weeks, pd.Timestamp(s))), int(np.searchsorted(weeks, pd.Timestamp(e) + pd.Timedelta(days=1)))

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        p, E, F = state.params, state.extras, state.frames
        P = state.periods
        it = F["order_items"].copy()
        it["cat"] = E["cat"][it["product_idx"].to_numpy()]
        segs = p["segments"]
        inc_w = E["inc_w"]
        # ---- elasticities: OLS on the weekly segment x category panel
        pre = it[it["_w"] < inc_w]
        ref = pre.groupby(["_seg", "product_idx"]).apply(lambda d: (d["net_price_cents"] * d["quantity"]).sum() / d["quantity"].sum(), include_groups=False)
        ref_d = ref.to_dict()
        it["ref_net"] = [ref_d.get((s_, k_), np.nan) for s_, k_ in zip(it["_seg"], it["product_idx"], strict=True)]
        it["rev"] = it["net_price_cents"] * it["quantity"]
        it["rev_ref"] = it["ref_net"] * it["quantity"]
        # SKUs a segment never bought before the increase have no reference pocket price: left out of the price panel
        keep_ = it["ref_net"].notna()
        if "cann" in E:  # the substitute SKU gains volume from the heroes' increase (A4): not a price response, left out
            keep_ &= it["product_idx"] != E["cann"]["sub"]
        panel = it[keep_].groupby(["_seg", "cat", "_w"]).agg(q=("quantity", "sum"), rev=("rev", "sum"), rev_ref=("rev_ref", "sum")).reset_index()
        cp = F["competitor_prices"]
        main = cp[cp["competitor"] == "Northline Supply"].copy()
        main["_w"] = np.searchsorted(E["weeks"], main["scraped_at"] - pd.Timedelta(days=2))
        main["cat"] = main["category"].map({c: i for i, c in enumerate(CATEGORIES)})
        cidx = main.pivot_table(index="_w", columns="cat", values="price_index_cents").reindex(range(E["n_w"])).ffill().bfill()
        cidx = (cidx / cidx.iloc[0]).to_numpy()
        eps_hat = {}
        for i, s in enumerate(segs):
            # segment x category x week panel: the competitor index varies by category (identified from category contrasts),
            # the price step is common to all categories; category fixed effects absorb level differences
            d = panel[(panel["_seg"] == i) & (panel["q"] > 0) & ~panel["_w"].between(inc_w, inc_w + EXCLUDE_WEEKS - 1)]
            y = np.log(d["q"].to_numpy())
            lp = np.log(d["rev"].to_numpy() / d["rev_ref"].to_numpy())
            lc = np.log(cidx[d["_w"].to_numpy(), d["cat"].to_numpy()])
            cats = sorted(d["cat"].unique())
            X = np.column_stack([lp, d["_w"].to_numpy() / 52.0, lc] + [(d["cat"].to_numpy() == c).astype(float) for c in cats])
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            eps_hat[s] = float(beta[0])
        f: dict[str, Any] = {"eps_hat": eps_hat}
        # ---- PVM P5 vs P7 (pocket and list prices) on segment x category cells
        a5, b5 = self._window(state, P[4])
        a7, b7 = self._window(state, P[6])
        it["line_cost"] = it["unit_cost_cents"] * it["quantity"]
        q5 = it[(it["_w"] >= a5) & (it["_w"] < b5)].groupby(["_seg", "cat"]).agg(q=("quantity", "sum"), r=("line_net_cents", "sum"), l=("line_list_cents", "sum"), c=("line_cost", "sum"))
        q7 = it[(it["_w"] >= a7) & (it["_w"] < b7)].groupby(["_seg", "cat"]).agg(q=("quantity", "sum"), r=("line_net_cents", "sum"), l=("line_list_cents", "sum"), c=("line_cost", "sum"))
        j = q5.join(q7, lsuffix="5", rsuffix="7", how="inner")
        p5, p7 = j["r5"] / j["q5"], j["r7"] / j["q7"]
        Q5, Q7 = j["q5"].sum(), j["q7"].sum()
        pbar5 = j["r5"].sum() / Q5
        f["price_var"] = float(((p7 - p5) * j["q7"]).sum())
        f["mix_var"] = float((p5 * j["q7"]).sum() - Q7 * pbar5)
        f["volume_var"] = float((Q7 - Q5) * pbar5)
        f["list_price_var"] = float(((j["l7"] / j["q7"] - j["l5"] / j["q5"]) * j["q7"]).sum())
        f["rev_change"] = float(j["r7"].sum() - j["r5"].sum())
        f["contribution_change"] = float((it.loc[(it["_w"] >= a7) & (it["_w"] < b7), "line_net_cents"].sum() - it.loc[(it["_w"] >= a7) & (it["_w"] < b7), "line_cost"].sum())
                                         - (it.loc[(it["_w"] >= a5) & (it["_w"] < b5), "line_net_cents"].sum() - it.loc[(it["_w"] >= a5) & (it["_w"] < b5), "line_cost"].sum()))
        f["seg_contribution_change"] = {}
        f["pocket_change"], f["units_change"] = {}, {}
        for i, s in enumerate(segs):
            w5_ = it[(it["_seg"] == i) & (it["_w"] >= a5) & (it["_w"] < b5)]
            w7_ = it[(it["_seg"] == i) & (it["_w"] >= a7) & (it["_w"] < b7)]
            f["seg_contribution_change"][s] = float((w7_["line_net_cents"] - w7_["line_cost"]).sum() - (w5_["line_net_cents"] - w5_["line_cost"]).sum())
            f["units_change"][s] = float(w7_["quantity"].sum() / max(1, w5_["quantity"].sum()) - 1)
            n5 = w5_.groupby("product_idx").apply(lambda d: (d["line_net_cents"]).sum() / d["quantity"].sum(), include_groups=False)
            n7 = w7_.groupby("product_idx").apply(lambda d: (d["line_net_cents"]).sum() / d["quantity"].sum(), include_groups=False)
            wq = w5_.groupby("product_idx")["quantity"].sum()
            common = n5.index.intersection(n7.index)
            f["pocket_change"][s] = float((n7[common] * wq[common]).sum() / (n5[common] * wq[common]).sum() - 1)
        sh = segs.index("price_shopper")
        f["disc5"] = float(np.average(it.loc[(it["_seg"] == sh) & (it["_w"] >= a5) & (it["_w"] < b5), "discount_pct"], weights=it.loc[(it["_seg"] == sh) & (it["_w"] >= a5) & (it["_w"] < b5), "line_list_cents"])) / 100
        f["disc7"] = float(np.average(it.loc[(it["_seg"] == sh) & (it["_w"] >= a7) & (it["_w"] < b7), "discount_pct"], weights=it.loc[(it["_seg"] == sh) & (it["_w"] >= a7) & (it["_w"] < b7), "line_list_cents"])) / 100
        f["list_change"] = p["price_change_pct"]
        # ---- Lerner optimum by segment (estimated elasticity, P8 mix)
        a8, b8 = self._window(state, P[7])
        f["optimal_change"], f["pocket8"], f["cost8"], f["units8"] = {}, {}, {}, {}
        for i, s in enumerate(segs):
            w8 = it[(it["_seg"] == i) & (it["_w"] >= a8) & (it["_w"] < b8)]
            pocket = float(w8["line_net_cents"].sum() / max(1, w8["quantity"].sum()))
            cost = float(w8["line_cost"].sum() / max(1, w8["quantity"].sum()))
            f["pocket8"][s], f["cost8"][s], f["units8"][s] = pocket, cost, float(w8["quantity"].sum())
            e = eps_hat[s]
            f["optimal_change"][s] = float(optimal_price(cost, e) / pocket - 1) if e < -1.05 else None
        # ---- counterfactual: P7 with the shoppers back at the pre-increase pocket price (segmented pricing)
        w7s = it[(it["_seg"] == sh) & (it["_w"] >= a7) & (it["_w"] < b7)]
        pocket7 = float(w7s["line_net_cents"].sum() / max(1, w7s["quantity"].sum()))
        pocket_cf = pocket7 / ((1 + p["price_change_pct"]) * (1 - f["disc7"]) / (1 - f["disc5"]))
        units_cf = float(constant_elasticity_demand(float(w7s["quantity"].sum()), pocket7, pocket_cf, eps_hat["price_shopper"]))
        rev7 = float(it.loc[(it["_w"] >= a7) & (it["_w"] < b7), "line_net_cents"].sum())
        f["rev7"] = rev7
        f["rev_segmented"] = rev7 - float(w7s["line_net_cents"].sum()) + units_cf * pocket_cf
        f["competitor"] = 1.0 if p["competitor_response"] else 0.0
        f["units_by_period"] = {}
        for per in P:
            a, b = self._window(state, per)
            f["units_by_period"][per] = {s: float(it.loc[(it["_seg"] == i) & (it["_w"] >= a) & (it["_w"] < b), "quantity"].sum()) for i, s in enumerate(segs)}
        state.extras["facts"] = f
        return f

    def _kpis(self, state: WorldState) -> dict[str, dict[str, float]]:
        """KPIs by period (cached; used by the dossier, operations and the simulation model)."""
        E = state.extras
        if "ops_kpis" in E:
            return E["ops_kpis"]
        f = self._facts(state)
        inc = {i.period: i for i in state.statements.income}  # type: ignore[union-attr]
        r = state.statements.ratios  # type: ignore[union-attr]
        kpis = {}
        for per in state.periods:
            act = state.ledger.activity(per)
            kpis[per] = {"revenue_cents": float(inc[per].revenue_cents), "gross_margin": r[per]["gross_margin"],
                         "gross_profit_cents": float(inc[per].gross_profit_cents), "discounts_cents": float(act["4110"]),
                         "discount_rate": float(act["4110"] / max(1, -act["4000"])), **{f"units_{s}": v for s, v in f["units_by_period"][per].items()}}
        E["ops_kpis"] = kpis
        return kpis

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Pricing (elasticities, ladder, waterfall, Lerner), customer cohorts, order desk queueing, KPIs by period."""
        p, cal, F = state.params, state.calendar, state.frames
        f = self._facts(state)
        P = state.periods
        segs = p["segments"]
        od = F["orders"]
        # customer cohorts by first-order quarter: share ordering again in later quarters
        oq = od.assign(q=cal.labels(od["order_ts"]))
        first = oq.groupby("customer_id")["q"].min()
        cohorts = []
        for per in P[:6]:
            members = first[first == per].index
            if len(members) < 5:
                continue
            act = oq[oq["customer_id"].isin(members)].groupby("q")["customer_id"].nunique()
            later = [x for x in P if x >= per]
            ret = [float(act.get(x, 0) / len(members)) for x in later]
            ret[0] = 1.0
            rev = oq[oq["customer_id"].isin(members)].groupby("q")["total_cents"].sum()
            cohorts.append(cohort_from_curve(per, len(members), ret, float(rev.get(per, 0)) / len(members), 0.0))
        s8, e8 = cal.bounds(P[-1])
        o8 = od[(od["order_ts"] >= pd.Timestamp(s8)) & (od["order_ts"] <= pd.Timestamp(e8) + pd.Timedelta(days=1))]
        lam = len(o8) / (13 * 5 * 9.0)
        q = mm1(lam, lam / 0.75)
        stations = [Station(name="order_desk", capacity_per_hour=q["mu"], utilization=q["rho"], wip=q["L"], cycle_time=q["W"])]
        queueing = {k: q[k] for k in ("lambda", "mu", "rho", "L", "W", "Lq", "Wq")}
        waterfall = pocket_price_waterfall(1.0, {"invoice_discount": f["disc7"], "freight_allowance": p["freight_pct"]})
        pricing = {
            "elasticity_by_segment": {s: {"true": p["eps"][s], "estimated": f["eps_hat"][s]} for s in segs},
            "price_ladder_cents": {c: float(v) for c, v in F["products"].groupby("category")["list_price_cents"].mean().items()},
            "uniform_increase_pct": p["price_change_pct"],
            "discount_leakage": {"shopper_discount_before": f["disc5"], "shopper_discount_after": f["disc7"], "pocket_waterfall_after": waterfall},
            "pocket_price_change_by_segment": f["pocket_change"],
            "lerner_optimal_change_by_segment": {s: v for s, v in f["optimal_change"].items() if v is not None},
            "competitor_response": bool(p["competitor_response"]),
        }
        po = F["purchase_orders"]
        supply = {"lead_time_dist": {"replenishment_days_mean": 14.0}, "inventory_cover_months": 1.6, "receipts_per_month": 2,
                  "replenishment_spend_cents_p8": float(po[po["received_at"] >= pd.Timestamp(s8)]["amount_cents"].sum())}
        kpis = self._kpis(state)
        r = state.statements.ratios  # type: ignore[union-attr]
        unit = {"gross_margin": r[P[-1]]["gross_margin"], "arpa": float(o8["total_cents"].mean()) if len(o8) else 0.0, "cac": 0.0, "ltv": 0.0,
                "payback_months": 0.0, "churn_monthly": 0.0}
        return build_operations_state(stations=stations, queueing=queueing, unit_economics=unit, cohorts=cohorts, supply_chain=supply,
                                      pricing=pricing, kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 8 key table from the generated order lines; reference SQL on the dirty DuckDB."""
        p, F = state.params, state.frames
        f = self._facts(state)
        P = state.periods
        P5, P6, P7, P8 = P[4], P[5], P[6], P[7]
        segs = p["segments"]
        metrics = []
        for s in segs:
            metrics.append(metric(f"elasticity_seg_{s}", f["eps_hat"][s], "x",
                                  f"OLS of ln(weekly units) on ln(pocket-price index vs the segment's pre-increase SKU pocket "
                                  f"prices) + trend + ln(competitor category index) + category effects for segment {s} (segment x category x week panel; "
                                  f"the {EXCLUDE_WEEKS} weeks after the increase excluded: partial adjustment).",
                                  pillar="python", tol_abs=0.3, label=f"Price elasticity: {s}", hint="Log-log regression by segment, pocket prices."))
        metrics += [
            metric("price_variance_cents", f["price_var"], "cents", f"PVM {P5} vs {P7} on segment x category cells, pocket (net) prices: sum (p7 - p5) x q7.",
                   pillar="accounting_finance", tol_rel=0.08, label="Price variance (pocket)", hint="Use net prices, not list."),
            metric("volume_variance_cents", f["volume_var"], "cents", f"(Q7 - Q5) x average {P5} pocket price.", pillar="accounting_finance", tol_rel=0.08,
                   label="Volume variance", hint="Total units change at the old average price."),
            metric("mix_variance_cents", f["mix_var"], "cents", "sum p5 x q7 - Q7 x average P5 pocket price.", pillar="accounting_finance", tol_rel=0.08,
                   label="Mix variance", hint="Shift between cells at old prices."),
            metric("pocket_price_change_pct_seg_price_shopper", f["pocket_change"]["price_shopper"], "ratio",
                   f"Price shoppers: {P7} vs {P5} pocket price per SKU at fixed {P5} SKU mix (list rose {p['price_change_pct']:.1%}).",
                   pillar="operations_strategy", tol_abs=0.02, label="Pocket-price change: price shoppers", hint="Net price, same SKU mix."),
            metric("pocket_price_change_pct_seg_loyal_account", f["pocket_change"]["loyal_account"], "ratio", "Loyal accounts: same measure.",
                   pillar="operations_strategy", tol_abs=0.02, label="Pocket-price change: loyal accounts", hint="Net price, same SKU mix."),
            metric("discount_leakage_delta_pts", f["disc7"] - f["disc5"], "ratio", f"Price shoppers' list-weighted average discount {P7} minus {P5}.",
                   pillar="sql", tol_abs=0.02, label="Discount leakage (shoppers), change", hint="order_items.discount_pct weighted by list value."),
            metric("contribution_change_cents", f["contribution_change"], "cents", f"(Net revenue - COGS) of order lines, {P7} minus {P5}.",
                   pillar="accounting_finance", tol_rel=0.08, label="Contribution change", hint="Gross-margin dollars, not %."),
            metric("revenue_at_segmented_pricing_cents", f["rev_segmented"], "cents",
                   f"{P7} net revenue if price shoppers kept the pre-increase pocket price (units by their estimated elasticity "
                   f"{f['eps_hat']['price_shopper']:.2f}) and the other segments kept the increase.", pillar="python", tol_rel=0.10,
                   label="Revenue under segmented pricing (counterfactual)", hint="Constant-elasticity demand by segment."),
            metric("competitor_response_flag", f["competitor"], "bool", f"1 if the main competitor's category index fell ~5% in {P7}.",
                   pillar="sql", tol_abs=0.0, label="Competitor responded (1/0)", hint="competitor_prices."),
        ]
        for s, v in f["optimal_change"].items():
            if v is not None:
                metrics.append(metric(f"optimal_price_change_seg_{s}_pct", v, "ratio",
                                      f"Lerner: p* = c x eps/(1+eps) with c = {P8} unit cost and eps = estimated {f['eps_hat'][s]:.2f}; reported vs the {P8} pocket price.",
                                      pillar="operations_strategy", tol_abs=0.05, label=f"Profit-maximising price change: {s}", hint="Markup = -1/eps."))
        dd = "SELECT * FROM order_items QUALIFY ROW_NUMBER() OVER (PARTITION BY order_item_id ORDER BY ingest_id) = 1" if p["difficulty"] == "principal" \
            else "SELECT * FROM order_items"
        ots = ("CAST(substr(CAST(o.order_ts AS VARCHAR), 1, 19) AS TIMESTAMP)" if p["difficulty"] == "principal" else "o.order_ts")
        sql_weekly = f"""
WITH oi AS ({dd})
SELECT TRIM(c.segment) AS segment, date_trunc('week', {ots}) AS wk,
       SUM(oi.quantity) AS units, SUM(oi.net_price_cents * oi.quantity) * 1.0 / SUM(oi.quantity) AS pocket_price,
       AVG(oi.list_price_cents) AS list_price,
       CASE WHEN {ots} >= (SELECT MIN(effective_from) FROM price_history WHERE change_pct > 0.05) THEN 'post' ELSE 'pre' END AS phase
FROM oi JOIN orders o USING (order_id) JOIN customers c USING (customer_id)
GROUP BY 1, 2, 6 ORDER BY 1, 2"""

        def weekly_checks(df: pd.DataFrame) -> dict[str, Any]:
            g_ = df.groupby(["segment", "phase"])["units"].mean().unstack()
            return {f"units_post_vs_pre_{s}": float(g_.loc[s, "post"] / g_.loc[s, "pre"] - 1) for s in g_.index if "post" in g_.columns}

        q_weekly = run_reference_query(con, "weekly_units_and_pocket_price_by_segment", sql_weekly, signature=["with", "join", "group by", "case when", "date_trunc("],
                                       metric_keys=[f"elasticity_seg_{s}" for s in segs], checks=weekly_checks, max_rows=30)
        sql_pvm = f"""
WITH oi AS ({dd}),
l AS (
  SELECT TRIM(c.segment) AS segment, pr.category, period_of({ots}) AS period, oi.quantity AS q, oi.net_price_cents * oi.quantity AS rev,
         oi.list_price_cents * oi.quantity AS list_rev
  FROM oi JOIN orders o USING (order_id) JOIN customers c USING (customer_id) JOIN products pr USING (product_id)),
cell AS (
  SELECT segment, category,
         SUM(CASE WHEN period = '{P5}' THEN q END) AS q5, SUM(CASE WHEN period = '{P5}' THEN rev END) AS r5,
         SUM(CASE WHEN period = '{P7}' THEN q END) AS q7, SUM(CASE WHEN period = '{P7}' THEN rev END) AS r7,
         SUM(CASE WHEN period = '{P5}' THEN list_rev END) AS l5, SUM(CASE WHEN period = '{P7}' THEN list_rev END) AS l7
  FROM l GROUP BY 1, 2)
SELECT SUM((r7 / q7 - r5 / q5) * q7) AS price_variance_cents,
       SUM(r5 / q5 * q7) - SUM(q7) * SUM(r5) / SUM(q5) AS mix_variance_cents,
       (SUM(q7) - SUM(q5)) * SUM(r5) / SUM(q5) AS volume_variance_cents,
       SUM((l7 / q7 - l5 / q5) * q7) AS list_price_variance_cents
FROM cell WHERE q5 > 0 AND q7 > 0"""
        q_pvm = run_reference_query(con, "price_volume_mix_p5_vs_p7", sql_pvm, signature=["with", "join", "group by", "case when", "sum("],
                                    metric_keys=["price_variance_cents", "volume_variance_cents", "mix_variance_cents"],
                                    checks=lambda df: {"price_variance": float(df.iloc[0]["price_variance_cents"]),
                                                       "list_price_variance": float(df.iloc[0]["list_price_variance_cents"])})
        sql_disc = f"""
WITH oi AS ({dd})
SELECT TRIM(c.segment) AS segment, period_of({ots}) AS period,
       SUM(oi.discount_pct * oi.list_price_cents * oi.quantity) / SUM(oi.list_price_cents * oi.quantity) / 100.0 AS avg_discount,
       SUM(oi.line_discount_cents) AS discount_cents
FROM oi JOIN orders o USING (order_id) JOIN customers c USING (customer_id)
GROUP BY 1, 2 ORDER BY 1, 2"""
        q_disc = run_reference_query(con, "discount_leakage_by_segment_and_quarter", sql_disc, signature=["with", "join", "group by", "sum("],
                                     metric_keys=["discount_leakage_delta_pts", "pocket_price_change_pct_seg_price_shopper"],
                                     checks=lambda df: {"shopper_discount_last": float(df[df["segment"] == "price_shopper"].iloc[-1]["avg_discount"])}, max_rows=40)
        sql_gl = """
SELECT j.period,
       SUM(CASE WHEN l.account_code = '4000' THEN l.credit_cents - l.debit_cents ELSE 0 END) AS list_revenue_cents,
       SUM(CASE WHEN l.account_code = '4110' THEN l.debit_cents - l.credit_cents ELSE 0 END) AS discounts_cents,
       SUM(CASE WHEN l.account_code LIKE '50%' THEN l.debit_cents - l.credit_cents ELSE 0 END) AS cogs_cents
FROM gl_lines l JOIN gl_journal j USING (entry_id)
WHERE j.period IN (SELECT period FROM period_calendar WHERE is_reporting) GROUP BY 1 ORDER BY 1"""
        q_gl = run_reference_query(con, "contra_revenue_and_margin_by_quarter", sql_gl, signature=["join", "group by", "case when", "sum("],
                                   metric_keys=["contribution_change_cents"], checks=lambda df: {"discounts_last": float(df.iloc[-1]["discounts_cents"])})
        sql_comp = """
SELECT TRIM(LOWER(competitor)) AS competitor, category, period_of(scraped_at) AS period, AVG(price_index_cents) AS avg_price_index
FROM competitor_prices GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""
        q_comp = run_reference_query(con, "competitor_price_index_by_quarter", sql_comp, signature=["group by", "avg("],
                                     metric_keys=["competitor_response_flag"], checks=lambda df: {"rows": len(df)})
        sql_mkt = """
SELECT period_of(week_start) AS period, SUM(spend_cents) AS marketing_cents, COUNT(*) AS weeks
FROM marketing_spend GROUP BY 1 ORDER BY 1"""
        q_mkt = run_reference_query(con, "marketing_spend_by_quarter", sql_mkt, signature=["group by", "sum(", "count("], metric_keys=[],
                                    checks=lambda df: {"max_over_min": float(df["marketing_cents"].max() / df["marketing_cents"].min())})
        an = {a.id: a for a in state.anomalies}
        an["A1"] = an["A1"].model_copy(update={"reference_sql": [q_weekly, q_pvm], "magnitude": {**an["A1"].magnitude, **{f"eps_hat_{s}": round(v, 6) for s, v in f["eps_hat"].items()},
                                                                                            "contribution_change_cents": round(f["contribution_change"], 2)},
                                               "evidence_trail": [f"Units {P7} vs {P5}: " + ", ".join(f"{s} {v:+.0%}" for s, v in f["units_change"].items()) + ".",
                                                                  f"PVM (pocket): price {f['price_var'] / 100:+,.0f}, volume {f['volume_var'] / 100:+,.0f}, mix {f['mix_var'] / 100:+,.0f} USD; "
                                                                  f"the list-price view claims {f['list_price_var'] / 100:+,.0f} USD of price."]})
        an["A2"] = an["A2"].model_copy(update={"reference_sql": [q_disc, q_gl], "magnitude": {**an["A2"].magnitude, "discount_leakage_delta": round(f["disc7"] - f["disc5"], 6),
                                                                                       "pocket_change_shopper": round(f["pocket_change"]["price_shopper"], 6)},
                                               "evidence_trail": [f"Shoppers' discount {f['disc5']:.1%} -> {f['disc7']:.1%}; their pocket price rose only "
                                                                  f"{f['pocket_change']['price_shopper']:.1%} on a {p['price_change_pct']:.1%} list increase."]})
        an["A3"] = an["A3"].model_copy(update={"reference_sql": [q_comp, q_mkt], "evidence_trail": [
            "Competitor index " + ("fell ~5% in " + P7 if p["competitor_response"] else "flat") + "; marketing spend flat."]})
        if "A4" in an:
            hero = F["products"]["product_id"].to_numpy()[state.extras["cann"]["hero"]].tolist()
            sub = F["products"]["product_id"].iat[state.extras["cann"]["sub"]]
            sql_cann = f"""
WITH oi AS ({dd})
SELECT period_of({ots}) AS period,
       SUM(CASE WHEN oi.product_id IN ({', '.join(repr(h) for h in hero)}) THEN oi.quantity ELSE 0 END) AS hero_units,
       SUM(CASE WHEN oi.product_id = '{sub}' THEN oi.quantity ELSE 0 END) AS substitute_units
FROM oi JOIN orders o USING (order_id) GROUP BY 1 ORDER BY 1"""
            q_cann = run_reference_query(con, "hero_vs_substitute_units", sql_cann, signature=["with", "join", "group by", "case when"], metric_keys=[],
                                         checks=lambda df: {"substitute_growth": float(df.iloc[-1]["substitute_units"] / max(1, df.iloc[0]["substitute_units"]))})
            sql_sub = f"""
WITH oi AS ({dd})
SELECT CASE WHEN oi.product_id = '{sub}' THEN 'substitute' ELSE 'hero' END AS role,
       SUM(oi.quantity) AS units, SUM((oi.net_price_cents - oi.unit_cost_cents) * oi.quantity) * 1.0 / SUM(oi.quantity) AS margin_per_unit_cents
FROM oi JOIN products p USING (product_id)
WHERE oi.product_id IN ({', '.join(repr(h) for h in hero + [sub])})
GROUP BY 1 ORDER BY 1"""
            q_sub = run_reference_query(con, "substitute_vs_hero_unit_margin", sql_sub, signature=["with", "join", "group by", "case when", "sum("],
                                        metric_keys=["contribution_change_cents"], checks=lambda df: {"roles": len(df)})
            an["A4"] = an["A4"].model_copy(update={"reference_sql": [q_cann, q_sub]})
        sh_e = f["eps_hat"]["price_shopper"]
        herrings_all = [
            "Macro / consumer weakness - volume fell only in the price-shopper segment and only after the increase; loyal accounts kept buying.",
            "Marketing was cut - weekly marketing spend is flat across all eight quarters.",
            ("A competitor undercut us - " + (f"true in {P7}, but shoppers' volume was already falling in {P6} before the competitor moved."
                                              if p["competitor_response"] else "the competitor price index is flat; there was no response.")),
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        opt = f["optimal_change"].get("price_shopper")
        root = (f"The {p['price_change_pct']:.0%} uniform list increase in {P6} ignored that elasticities differ by segment: price shoppers are elastic "
                f"(estimated {sh_e:.2f}) while loyal accounts are not ({f['eps_hat'].get('loyal_account', float('nan')):.2f}). Reps then gave the shoppers "
                f"{(f['disc7'] - f['disc5']) * 100:.1f} points of extra discount, so their pocket price rose only {f['pocket_change']['price_shopper']:.1%} and their "
                f"volume still fell {f['units_change']['price_shopper']:.0%}. Gross margin % is up but contribution fell by {-f['contribution_change'] / 100:,.0f} USD "
                f"({P7} vs {P5}); the list-price PVM overstates the price effect ({f['list_price_var'] / 100:,.0f} vs {f['price_var'] / 100:,.0f} USD pocket). "
                + (f"The {P7} competitor cut amplified, but did not start, the decline. " if p["competitor_response"] else "")
                + "The answer is segmented pricing with fences: keep the increase for inelastic segments, move shoppers toward the Lerner optimum"
                + (f" ({opt:+.0%})" if opt is not None else "") + " and enforce pocket-price discipline - not a blanket rollback.")
        py = ("import numpy as np, pandas as pd\n"
              "# weekly segment x category panel: ln(units) ~ ln(pocket index) + trend + ln(competitor index) + category FE, by segment\n"
              "# drop the 6 weeks after the increase (partial adjustment); then Lerner p* = c * eps / (1 + eps) for eps < -1\n")
        return GroundTruth(
            root_cause=root, anomalies=[an[k] for k in ("A1", "A2", "A3", "A4") if k in an], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q.sql for q in (q_weekly, q_pvm, q_disc, q_gl, q_comp, q_mkt)], python=[py],
                memo_bullets=[
                    "GM% up, GM$ down: contribution fell - decompose with pocket prices, not list.",
                    f"Elasticity differs by segment (shoppers {sh_e:.2f}); one pooled number hides it.",
                    "Leakage: rep discounts gave back much of the increase in the elastic segment - cap discount authority.",
                    "Segmented pricing with fences beats both the uniform increase and a blanket rollback.",
                    "Timing rules out the competitor and macro stories.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal/conceal, four stakeholders, spec timeline (red herrings flagged)."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        k = self._kpis(state)
        names = rng.pick_names(4)
        units_now = sum(f["units_by_period"][P[7]].values())
        units_before = sum(f["units_by_period"][P[4]].values())
        reveal = [
            f"List prices were raised {p['price_change_pct']:.0%} across the board in {P[5]}.",
            "The CEO describes the increase as proof of 'pricing power'.",
            f"Revenue is roughly flat ({k[P[7]]['revenue_cents'] / k[P[4]]['revenue_cents'] - 1:+.1%} {P[7]} vs {P[4]}) while gross margin % rose "
            f"({k[P[4]]['gross_margin']:.1%} -> {k[P[7]]['gross_margin']:.1%}).",
            "The sales team complains that customers are walking and asks for more discount authority.",
            f"Units are down {units_now / units_before - 1:.0%} ({P[7]} vs {P[4]}).",
        ]
        if p["competitor_response"]:
            reveal.append(f"The main competitor cut prices ~5% in {P[6]}.")
        conceal = ["segment elasticities", "discount leakage magnitude (leakage magnitude by segment)", "optimal prices by segment (Lerner)",
                   "the segmented-pricing counterfactual", "A1", "A2", "A3", "A4"]
        stakeholders = [
            Stakeholder(name=names[0], role="CEO", agenda="Defend the 'pricing power' narrative.", disposition="confident", knows=["margin % is up"], hides=[]),
            Stakeholder(name=names[1], role="VP Sales", agenda="Roll the increase back.", disposition="combative",
                        knows=["reps are discounting to keep shoppers"], hides=["the discounting itself"]),
            Stakeholder(name=names[2], role="Head of Pricing (Finance)", agenda="Defend the business case.", disposition="analytical",
                        knows=["the case assumed elasticity -0.5 for everyone"], hides=[]),
            Stakeholder(name=names[3], role="Category Manager", agenda="Win back the price shoppers.", disposition="cooperative",
                        knows=["the price-shopper segment cross-shops competitors weekly"], hides=[]),
        ]
        tl = [
            TimelineEvent(period=P[3], headline="Pricing consultant: 'headroom'", detail="Study recommends a uniform increase (elasticity -0.5)."),
            TimelineEvent(period=P[4], headline=f"Board approves +{p['price_change_pct']:.0%}", detail="Uniform list increase approved."),
            TimelineEvent(period=P[5], headline="Increase live", detail="New price list published."),
            TimelineEvent(period=P[5], headline="Macro headlines: 'consumer slowdown'", detail="Industry press warns of softer demand.", is_red_herring=True),
            TimelineEvent(period=P[6], headline="Sales asks for discount authority", detail="VP Sales requests a higher rep discount ceiling."),
            TimelineEvent(period=P[7], headline=f"Revenue flat, units {units_now / units_before - 1:.0%}", detail="Board pack shows GM% up."),
        ]
        if p["competitor_response"]:
            tl.append(TimelineEvent(period=P[6], headline="Competitor -5%", detail="Main competitor cuts the shoppers' favourite category."))
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[6], headline="Marketing review: 'did we cut too much?'", detail="Question raised in the ops review.", is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["pricing_business_case", "weekly_revenue_chart", "discount_authority_memo", "competitor_price_scrape"],
                           architecture_mermaid=None)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Five levers; the shopper price lever is a signed change (the fix is a cut, ``<=``); defaults = status quo."""
        return [
            Lever(key="price_change_shopper_pct", label="Price change for price shoppers (fenced)", min=-20, max=10, default=0, unit="%",
                  effect="Shopper units move by their elasticity (half this quarter, half next): a cut raises them."),
            Lever(key="price_change_loyal_pct", label="Price change for loyal accounts", min=-10, max=20, default=0, unit="%",
                  effect="Loyal accounts are inelastic: revenue and contribution rise with price."),
            Lever(key="discount_cap_tightening_pts", label="Tighten rep discount authority", min=0, max=10, default=0, unit="pts",
                  effect="Raises shoppers' pocket price (less leakage); costs some volume."),
            Lever(key="fence_feature", label="Introduce a fence (loyalty tier / contract pricing) (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Enables segmented pricing; costs ~0.75% of revenue."),
            Lever(key="match_competitor", label="Match the competitor on the contested category (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Recovers ~60% of the competitor-driven shopper volume at a margin cost."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form segment demand with elasticities (partial adjustment = half the volume effect lagged one quarter)."""
        p = state.params
        f = self._facts(state)
        P8 = state.periods[-1]
        k8 = self._kpis(state)[P8]
        eps = f["eps_hat"]
        sh, lo = "price_shopper", "loyal_account"
        u_s, u_l = f["units8"][sh], f["units8"].get(lo, 0.0)
        p_s, p_l = f["pocket8"][sh], f["pocket8"].get(lo, 0.0)
        c_s, c_l = f["cost8"][sh], f["cost8"].get(lo, 0.0)
        e_s, e_l = eps[sh], eps.get(lo, -0.5)
        contribution = float(sum((f["pocket8"][s] - f["cost8"][s]) * f["units8"][s] for s in p["segments"]))
        rev = k8["revenue_cents"]
        kpis = {"revenue_cents": rev, "contribution_cents": contribution, "gross_margin": k8["gross_margin"], "units_price_shopper": u_s,
                "units_loyal_account": u_l, "pocket_price_shopper_cents": p_s, "discount_rate_shopper": f["disc7"]}
        effects: list[dict[str, Any]] = []
        for lag in (0, 1):  # partial adjustment: half the volume response this quarter, half next
            effects += [
                {"lever": "price_change_shopper_pct", "kpi": "units_price_shopper", "delta_per_unit": 0.5 * e_s * u_s / 100, "lag_periods": lag},
                {"lever": "price_change_shopper_pct", "kpi": "contribution_cents", "delta_per_unit": 0.5 * e_s * u_s / 100 * (p_s - c_s), "lag_periods": lag},
                {"lever": "price_change_shopper_pct", "kpi": "revenue_cents", "delta_per_unit": 0.5 * e_s * u_s / 100 * p_s, "lag_periods": lag},
                {"lever": "price_change_loyal_pct", "kpi": "units_loyal_account", "delta_per_unit": 0.5 * e_l * u_l / 100, "lag_periods": lag},
                {"lever": "price_change_loyal_pct", "kpi": "contribution_cents", "delta_per_unit": 0.5 * e_l * u_l / 100 * (p_l - c_l), "lag_periods": lag},
                {"lever": "price_change_loyal_pct", "kpi": "revenue_cents", "delta_per_unit": 0.5 * e_l * u_l / 100 * p_l, "lag_periods": lag},
                {"lever": "discount_cap_tightening_pts", "kpi": "units_price_shopper", "delta_per_unit": 0.5 * e_s * u_s / 100, "lag_periods": lag},
            ]
        effects += [  # immediate price effects on the existing volume
            {"lever": "price_change_shopper_pct", "kpi": "pocket_price_shopper_cents", "delta_per_unit": p_s / 100, "lag_periods": 0},
            {"lever": "price_change_shopper_pct", "kpi": "contribution_cents", "delta_per_unit": u_s * p_s / 100, "lag_periods": 0},
            {"lever": "price_change_shopper_pct", "kpi": "revenue_cents", "delta_per_unit": u_s * p_s / 100, "lag_periods": 0},
            {"lever": "price_change_loyal_pct", "kpi": "contribution_cents", "delta_per_unit": u_l * p_l / 100, "lag_periods": 0},
            {"lever": "price_change_loyal_pct", "kpi": "revenue_cents", "delta_per_unit": u_l * p_l / 100, "lag_periods": 0},
            {"lever": "discount_cap_tightening_pts", "kpi": "discount_rate_shopper", "delta_per_unit": -0.01, "lag_periods": 0},
            {"lever": "discount_cap_tightening_pts", "kpi": "pocket_price_shopper_cents", "delta_per_unit": p_s * 0.01, "lag_periods": 0},
            {"lever": "discount_cap_tightening_pts", "kpi": "contribution_cents", "delta_per_unit": u_s * p_s * 0.01, "lag_periods": 0},
            {"lever": "fence_feature", "kpi": "revenue_cents", "delta_per_unit": -0.0075 * rev, "lag_periods": 0},
            {"lever": "fence_feature", "kpi": "contribution_cents", "delta_per_unit": -0.0075 * rev, "lag_periods": 0},
            {"lever": "match_competitor", "kpi": "units_price_shopper", "delta_per_unit": 0.6 * u_s * (1 - (1 - p["competitor_cut"]) ** COMP_CROSS) * 0.5 * f["competitor"], "lag_periods": 0},
            {"lever": "match_competitor", "kpi": "contribution_cents", "delta_per_unit": -0.05 * u_s * p_s * 0.5, "lag_periods": 0},
            {"lever": "match_competitor", "kpi": "gross_margin", "delta_per_unit": -0.01, "lag_periods": 0},
        ]
        opt = f["optimal_change"].get(sh)
        threshold = float(min(10.0, max(3.0, round(-100 * opt * 0.5)))) if opt is not None and opt < 0 else 5.0  # halfway to Lerner, <= 10%
        P = state.periods
        kq = self._kpis(state)
        drag = {"units_price_shopper": (f["units_by_period"][P8][sh] - f["units_by_period"][P[5]][sh]) / 2,
                "contribution_cents": f["contribution_change"] / 2 * 0.5,
                "discount_rate_shopper": max(0.0, (kq[P8]["discount_rate"] - kq[P[5]]["discount_rate"]) / 2)}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"fence_feature": 1.0, "price_change_shopper_pct": at_most(-threshold)},
                "bounds": {"units_price_shopper": [0.0, None], "units_loyal_account": [0.0, None], "discount_rate_shopper": [0.0, 0.6],
                           "gross_margin": [0.0, 1.0]},
                "max_quarters": 4}
