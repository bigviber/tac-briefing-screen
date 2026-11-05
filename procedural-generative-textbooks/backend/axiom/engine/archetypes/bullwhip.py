"""``bullwhip`` - "Whiplash" (archetypes.md section 7).

A distributor (the company) buys SKUs from a supplier and sells them to retailers. Consumer demand
at the shelf (POS) is stable, yet the company's purchase orders swing wildly, the warehouse
alternates between stock-outs and gluts, air freight bills pile up and P8 ends with a write-down.
Nobody uses the ``pos_sales`` feed the retailers send every week.

Mechanics: the amplification is generated endogenously by the ordering policies (weekly loop)
---------------------------------------------------------------------------------------------
* Tier 0 - POS: per retailer-SKU-week ``mu_rs * (1 + 0.03 sin(2 pi t/52)) * (1 + cv_r * N(0,1))`` with
  ``cv_r = cv_d * sqrt(chains carrying the SKU)`` so the SKU-level POS CV is ``cv_d = U(0.10, 0.20)``; the P6 promotion (first 4 weeks of P6, -15% price) lifts POS +10% and
  the following 8 weeks give it back (12-week POS sum within ~2%).
* Tier 1 - retailers: weekly review, moving-average forecast of their own POS over 4 weeks,
  order-up-to ``S = 2 D_hat + z_r sigma_hat sqrt(2)`` (lead time 1 week + review),
  orders ``max(0, S - IP)``. Promo forward-buy multiplies promo-week orders by ``U(1.5, 2.5)``;
  shortage gaming inflates orders by ``U(1.3, 1.8)`` when the company's 4-week fill rate on the SKU
  is below 90%.
* Tier 2 - the company: weekly review, moving average of *retailer orders* over ``p`` weeks
  (8 before P2, then the "responsive" ``p`` in {2,3,4}: 2 at foundation, 2-3 practitioner, 3-4
  principal), order-up-to ``(L+1) D_hat + z sigma_hat sqrt(L+1)`` against on-hand + on-order, rounded
  up to the MOQ (0.5 week of demand before P4, then ``moq_weeks`` in U(2,4): 3.25-4 / 2.5-3.5 / 2-2.5). Retailer orders are filled from stock (proportional allocation),
  the shortfall is lost (stockout revenue loss).
* Tier 3 - the supplier: lead time ``L`` weeks (stable distribution), weekly capacity
  ``cap_mult x`` mean demand per SKU; FIFO backlog; quantities that miss the first shipment are
  shipped later by air (expedited, 4x freight). Principal adds tier 4: the supplier's own
  production orders (MA(4) on our POs).
* The generator **verifies** the realized median SKU variance ratios over P5-P8 (tier 2 =
  Var(retailer orders)/Var(POS), tier 3 = Var(POs)/Var(POS)) against the difficulty band and re-draws
  ``p`` / ``moq_weeks`` up to 5 times (closest attempt kept); the realized values are the ground truth,
  and the company stage is checked against the Chen et al. bound ``1 + 2L/p + 2L^2/p^2``.
* GL: receipts Dr 1220/Cr 2000; shipments Dr 1100/Cr 4000 with promo allowances Dr 4110; COGS Dr
  5000/Cr 1220; inbound freight 5030 (air freight on backorders, source ``expedite``); warehousing
  2%/month of inventory value into 6400; P8 write-down Dr 5040/Cr 1230 on slow movers.
  Ties: AR (open invoices), AP (unpaid receipts), inventory (receipts - shipments at cost), revenue.

Documented simplifications
* ``pos_sales`` is the syndicated feed aggregated per SKU-week (the per-retailer series drive the
  simulation); ``orders`` are retailer-SKU-week order lines.
* Supplier lead time is 2 weeks (benchmark band ~14-25 days): with ``L + 1 = 3`` the Chen bound holds for the
  realized (non-negative, MOQ-rounded) orders at every ``p`` in {2,3,4}; at L = 3 and p = 2 truncation pushes some
  realized ratios below the bound. The generator also checks the bound in its re-draw loop.
* Retailers review weekly (biweekly retailer batching alone would push tier-2 ratios past 50).
* Levers: 5 of the spec's 7 (``allocation_rule`` and ``safety_z`` folded into the model docs).

Simulation (reduced form, KPIs = P8): ``forecast_window_p`` moves company-stage amplification
along the Chen bound (linearised to p=12) with inventory / expedite / fill-rate effects one quarter
later (smoothing lag); ``demand_signal_pos`` collapses tier-2 amplification to ~1 (lag 1);
``moq_weeks`` adds inventory per week of MOQ and unit cost when cut below 2; ``promo_policy_edlp``
removes the forward-buy spike; ``dual_source`` raises unit cost 5% and leaves amplification
unchanged (the trap). ``resolved_if``: POS signal and p >= 8.
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
    chen_bullwhip_bound,
    eoq,
    mm1,
    safety_stock,
    service_z,
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

logger = logging.getLogger("axiom.archetypes.bullwhip")

CATEGORIES = ("Snacks", "Beverages", "Household", "Personal care", "Pet", "Paper goods")
# Verification bands on the median SKU-level weekly variance ratios over P5-P8 (tier 2 = retailer orders / POS,
# tier 3 = company POs / POS). The spec's [2, 6] / [4, 15] assume continuous order quantities; with its own MOQ of
# 2-4 weeks of demand, PO clumping alone gives ~(m - 1) / CV_pos^2, so tier-3 bands are re-derived from the mechanism.
BANDS = {"foundation": ((2.0, 20.0), (40.0, 170.0)), "practitioner": ((2.0, 16.0), (20.0, 110.0)), "principal": ((2.0, 14.0), (8.0, 70.0))}
P_RANGE = {"foundation": (2, 2), "practitioner": (2, 3), "principal": (3, 4)}
MOQ_RANGE = {"foundation": (3.25, 4.0), "practitioner": (2.5, 3.5), "principal": (2.0, 2.5)}
P_OLD = 8
MOQ_OLD = 0.5


def _var_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """Per-row sample-variance ratio ``Var(num)/Var(den)`` (rows = SKUs)."""
    v = np.var(den, axis=1, ddof=1)
    return np.var(num, axis=1, ddof=1) / np.where(v > 0, v, np.nan)


@register
class Bullwhip(Archetype):
    """Whiplash: flat consumer demand, wild purchase orders."""

    key = "bullwhip"
    title = "Whiplash"
    pillars = ("operations_strategy", "accounting_finance", "python")
    industries = ("retail", "ecommerce", "3pl", "manufacturing")
    schema_template = "ecommerce_oms"
    summary = ("Retail sell-through is flat, yet the warehouse swings between stock-outs and gluts, air-freight bills pile up and "
               "the year ends with an inventory write-down. The VP Supply Chain blames an unreliable supplier.")
    root_cause_terms = ("pos stability", "forecast window", "moq", "amplification ratios")

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 7."""
        ind = industry or ("retail" if rng.random() < 0.6 else "ecommerce")
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        lo, hi = 30e6, 200e6
        rev_usd = math.exp(rng.uniform(math.log(lo), math.log(hi)))
        gm_ind = ind if ind in ("retail", "ecommerce") else "retail"
        gm = float(np.clip(bk.sample_between(pool, rng.spawn("bench:gm"), f"{gm_ind}.gross_margin", gm_ind, 0.2, 0.45), 0.18, 0.32))
        lead_days = float(bk.sample_between(pool, rng.spawn("bench:lead"), "operations.supplier_lead_time_days", ind, 0.25, 0.45))
        z = float(np.clip(service_z(bk.sample_between(pool, rng.spawn("bench:sl"), "operations.safety_stock_service_level", ind, 0.4, 0.9)), 1.65, 2.33))
        S = difficulty_pick(difficulty, 10, 22, 32)
        R = difficulty_pick(difficulty, 4, 6, 7)
        p = {
            "industry": ind,
            "business_model": {"retail": "regional consumer-goods distributor supplying retail chains",
                               "ecommerce": "consumer-goods brand selling wholesale to retailers and marketplaces",
                               "3pl": "distribution arm of a logistics group (buy-sell model)",
                               "manufacturing": "consumer-goods manufacturer's distribution business"}[ind],
            "revenue_ttm_cents": int(rev_usd * 100),
            "gm": gm,
            "n_skus": S,
            "n_retailers": R,
            "cv_d": float(rng.uniform(0.10, 0.20)),
            "lead_weeks": 2,  # benchmark band ~14-25 days; 2 weeks keeps the Chen bound valid for p in {2,3,4} (see docstring)
            "lead_days_benchmark": lead_days,
            "z": z,
            "retailer_z": float(rng.uniform(1.0, 1.65)),
            "forecast_window_p": int(rng.integers(P_RANGE[difficulty][0], P_RANGE[difficulty][1] + 1)),
            "moq_weeks": float(rng.uniform(*MOQ_RANGE[difficulty])),
            "promo_discount": 0.15,
            "forward_buy_mult": float(rng.uniform(1.5, 2.5)),
            "gaming_mult": float(rng.uniform(1.3, 1.8)),
            # supplier weekly capacity / mean demand: tighter at foundation (more backorders, louder symptoms)
            "cap_mult": float(rng.uniform(*difficulty_pick(difficulty, (1.25, 1.4), (1.35, 1.55), (1.5, 1.7)))),
            "writedown_pct": magnitude(rng, difficulty, 0.02, 0.04),
            "holding_pct_month": 0.02,
            "air_freight_mult": 4.0,
            "freight_pct": float(rng.uniform(0.012, 0.02)),
            "terms_days": int(rng.choice((30, 45))),
            "supplier_terms_days": int(rng.choice((30, 45))),
            "op_margin": float(rng.uniform(0.03, 0.06)),
            "tax_rate": 0.23,
            "interest_rate": float(rng.uniform(0.07, 0.10)),
            "factory_tier": difficulty == "principal",
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "band_t2": BANDS[difficulty][0],
            "band_t3": BANDS[difficulty][1],
            "difficulty": difficulty,
        }
        p["description"] = (f"{p['business_model'].capitalize()}: {S} SKUs replenished weekly from one key supplier and shipped to "
                            f"{R} retail chains, which share weekly point-of-sale data.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Calendar of weeks, SKUs, retailers and the POS demand (tier 0)."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        start = pd.Timestamp(cal.start)
        first_monday = start + pd.Timedelta(days=(7 - start.weekday()) % 7)
        end = pd.Timestamp(cal.end)
        n_w = int((end - first_monday).days // 7) + 1  # weeks starting on/before the as-of date (the last may be partial)
        weeks = pd.DatetimeIndex([first_monday + pd.Timedelta(weeks=k) for k in range(n_w)])
        S, R = p["n_skus"], p["n_retailers"]
        mu_s = np.clip(20 + 280 * g.pareto(1.6, S) / 2, 20, 900)
        carry = g.random((R, S)) < 0.65
        carry[g.integers(0, R, S), np.arange(S)] = True  # every SKU is carried by at least one chain
        share = g.dirichlet(np.full(R, 3.0))
        mu_rs = mu_s[None, :] * share[:, None] * carry * (R / carry.sum(axis=0))[None, :] * carry.mean()
        mu_rs = np.where(carry, np.maximum(mu_rs, 2.0), 0.0)
        t = np.arange(n_w)
        season = 1 + 0.03 * np.sin(2 * np.pi * (t + g.integers(0, 52)) / 52)  # mild seasonality
        noise = np.clip(g.normal(0, 1, (R, S, n_w)), -2.5, 2.5)
        cv_r = p["cv_d"] * np.sqrt(carry.sum(axis=0))[None, :, None]  # per-chain noise so the SKU-level POS CV ~= cv_d
        pos = mu_rs[:, :, None] * season[None, None, :] * (1 + np.minimum(cv_r, 0.45) * noise)
        p6s = pd.Timestamp(cal.bounds(state.p(6))[0])
        promo_w0 = int(np.searchsorted(weeks, p6s))
        promo = np.zeros(n_w)
        promo[promo_w0: promo_w0 + 4] = 0.10
        promo[promo_w0 + 4: promo_w0 + 12] = -0.05
        pos = np.round(np.maximum(0, pos * (1 + promo)[None, None, :])).astype(np.int64)
        pos = np.where(carry[:, :, None], pos, 0)
        unit_cost = np.round(np.clip(g.lognormal(math.log(900), 0.6, S), 150, 6000)).astype(np.int64)  # cents (rescaled later)
        state.extras.update({"weeks": weeks, "n_w": n_w, "pos": pos, "mu_rs": mu_rs, "mu_s": mu_s, "carry": carry, "promo_w0": promo_w0,
                             "unit_cost_raw": unit_cost, "k_r": np.ones(R, dtype=np.int64), "phase_r": np.zeros(R, dtype=np.int64)})
        state.extras["week_period"] = cal.labels(weeks)
        state.extras["months"] = cal.month_starts()

    # ------------------------------------------------------------------ chain simulation

    def _simulate(self, state: WorldState, g: np.random.Generator, *, p_window: int, moq_weeks: float, signal: str = "retailer_orders",
                  policy_change: bool = True, gaming: bool = True, forward_buy: bool = True) -> dict[str, Any]:
        """Weekly multi-tier simulation (vectorised over retailers x SKUs)."""
        p, E = state.params, state.extras
        pos = E["pos"]
        R, S, n_w = pos.shape
        mu_rs, mu_s = E["mu_rs"], E["mu_rs"].sum(axis=0)
        L = p["lead_weeks"]
        weeks = E["weeks"]
        p2 = int(np.searchsorted(weeks, pd.Timestamp(state.calendar.bounds(state.p(2))[0])))
        p4 = int(np.searchsorted(weeks, pd.Timestamp(state.calendar.bounds(state.p(4))[0])))
        k_r, phase = E["k_r"], E["phase_r"]
        zr = p["retailer_z"]
        promo = np.zeros(n_w, dtype=bool)
        promo[E["promo_w0"]: E["promo_w0"] + 4] = True
        r_cover = (1 + k_r)[:, None]
        r_inv = np.round(mu_rs * (r_cover + zr * 0.3)).astype(float)
        r_pipe = np.zeros((R, S))
        c_inv = np.round(mu_s * (L + 1 + p["z"] * 0.5)).astype(float)
        c_inv0 = c_inv.copy()
        arrivals = np.zeros((S, n_w + L + 30))
        backlog = np.zeros(S)
        on_order = np.zeros(S)
        cap = p["cap_mult"] * mu_s
        orders = np.zeros((R, S, n_w))
        shipped = np.zeros((R, S, n_w))
        po = np.zeros((S, n_w))
        sup_ship_ontime = np.zeros((S, n_w))
        sup_ship_back = np.zeros((S, n_w))
        receipts = np.zeros((S, n_w))
        inv_end = np.zeros((S, n_w))
        onord_end = np.zeros((S, n_w))
        fill_hist = np.ones((S, n_w))
        sig_hist = np.zeros((S, n_w))
        for t in range(n_w):
            # retailer receives last week's shipments, sells POS
            if t > 0:
                r_inv += shipped[:, :, t - 1]
                r_pipe -= shipped[:, :, t - 1]
                r_pipe = np.maximum(r_pipe, 0)
            r_inv -= pos[:, :, t]
            # retailer ordering on review weeks: MA(4) of own POS
            lo = max(0, t - 3)
            hist = pos[:, :, lo: t + 1].astype(float)
            d_hat = hist.mean(axis=2) if t >= 1 else mu_rs
            sd = hist.std(axis=2, ddof=1) if t >= 2 else 0.2 * mu_rs
            review = ((t + phase) % k_r == 0)[:, None]
            s_r = r_cover * d_hat + zr * sd * np.sqrt(r_cover)
            o = np.where(review, np.maximum(0.0, s_r - (r_inv + r_pipe)), 0.0)
            if forward_buy and promo[t]:
                o = np.where(review, np.maximum(o, d_hat * p["forward_buy_mult"]), 0.0)  # forward-buy: ~mult x weekly demand
            if gaming and t >= 4:
                fr = fill_hist[:, max(0, t - 4): t].mean(axis=1)
                o = np.where((fr < 0.9)[None, :], o * p["gaming_mult"], o)
            o = np.round(o) * E["carry"]
            orders[:, :, t] = o
            # company receives supplier shipments, fills retailer orders (proportional allocation)
            c_inv += arrivals[:, t]
            receipts[:, t] = arrivals[:, t]
            on_order = np.maximum(on_order - arrivals[:, t], 0)
            demand = o.sum(axis=0)
            ratio = np.where(demand > 0, np.minimum(1.0, c_inv / np.maximum(demand, 1e-9)), 1.0)
            sh = np.floor(o * ratio[None, :])
            shipped[:, :, t] = sh
            r_pipe += sh
            c_inv -= sh.sum(axis=0)
            fill_hist[:, t] = np.where(demand > 0, sh.sum(axis=0) / np.maximum(demand, 1e-9), 1.0)
            # company forecast + order-up-to (+ MOQ)
            sig = pos[:, :, t].sum(axis=0) if signal == "pos" else demand
            sig_hist[:, t] = sig
            pw = p_window if (t >= p2 or not policy_change) else P_OLD
            mw = moq_weeks if (t >= p4 or not policy_change) else MOQ_OLD
            lo = max(0, t - pw + 1)
            sh_ = sig_hist[:, lo: t + 1]
            dc = sh_.mean(axis=1) if t >= 1 else mu_s
            sdc = sh_.std(axis=1, ddof=1) if t - lo >= 1 else 0.2 * mu_s
            s_c = (L + 1) * dc + p["z"] * sdc * math.sqrt(L + 1)
            q = s_c - (c_inv + on_order)
            moq = np.maximum(1.0, mw * mu_s)
            q = np.where(q > 0, np.ceil(q / moq) * moq, 0.0)
            po[:, t] = q
            on_order += q
            # supplier: FIFO backlog with weekly capacity; first shipment on time, the rest backordered (air)
            backlog += q
            ship_now = np.minimum(backlog, cap)
            ontime = np.minimum(q, ship_now)
            back = ship_now - ontime
            backlog -= ship_now
            sup_ship_ontime[:, t] = ontime
            sup_ship_back[:, t] = back
            arrivals[:, t + L] += ontime
            arrivals[:, t + 1] += back  # backorders are flown in (air freight, 1 week)
            inv_end[:, t] = c_inv
            onord_end[:, t] = on_order
        return {"orders": orders, "shipped": shipped, "po": po, "ontime": sup_ship_ontime, "back": sup_ship_back, "receipts": receipts,
                "inv_end": inv_end, "onord_end": onord_end, "fill": fill_hist, "c_inv0": c_inv0, "arrivals": arrivals, "backlog_end": backlog}

    def _ratios(self, state: WorldState, sim: dict[str, Any], w0: int) -> dict[str, float]:
        E = state.extras
        pos_s = E["pos"].sum(axis=0)[:, w0:].astype(float)
        ro = sim["orders"].sum(axis=0)[:, w0:]
        po = sim["po"][:, w0:]
        t2 = _var_ratio(ro, pos_s)
        t3 = _var_ratio(po, pos_s)
        stage = _var_ratio(po, ro)
        return {"t2": float(np.nanmedian(t2)), "t3": float(np.nanmedian(t3)), "stage": float(np.nanmedian(stage)),
                "t2_by_sku": t2, "t3_by_sku": t3}

    # ------------------------------------------------------------------ anomalies

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Run the chain with the company policy; verify the amplification band (re-draw p / MOQ up to 5 times)."""
        p, E = state.params, state.extras
        rng = ctx.rng.spawn("anomaly")
        P = state.periods
        w5 = int(np.searchsorted(E["weeks"], pd.Timestamp(state.calendar.bounds(P[4])[0])))
        E["w5"] = w5
        lo2, hi2 = p["band_t2"]
        lo3, hi3 = p["band_t3"]
        best = None
        attempts = []
        pw, moq = p["forecast_window_p"], p["moq_weeks"]
        for k in range(5):
            g = rng.spawn(f"sim:{k}").generator
            sim = self._simulate(state, g, p_window=pw, moq_weeks=moq)
            r = self._ratios(state, sim, w5)
            chen = chen_bullwhip_bound(p["lead_weeks"] + 1, pw)
            miss = (max(0.0, lo3 - r["t3"]) / lo3 + max(0.0, r["t3"] - hi3) / hi3 + max(0.0, lo2 - r["t2"]) / lo2 + max(0.0, r["t2"] - hi2) / hi2
                    + max(0.0, chen - r["stage"]) / chen)  # the company stage must respect the Chen et al. lower bound
            attempts.append({"p": pw, "moq_weeks": round(moq, 3), "t2": round(r["t2"], 3), "t3": round(r["t3"], 3), "miss": round(miss, 4)})
            if best is None or miss < best[0]:
                best = (miss, pw, moq, sim, r)
            if miss == 0:
                break
            # re-draw within the difficulty's range, nudged towards the band
            plo, phi = P_RANGE[p["difficulty"]]
            mlo, mhi = MOQ_RANGE[p["difficulty"]]
            step = float(rng.spawn(f"redraw:{k}").uniform(0.15, 0.4))
            if r["t3"] < lo3 or r["stage"] < chen:
                pw, moq = int(max(plo, pw - 1)), float(min(mhi, moq + step))
            else:
                pw, moq = int(min(phi, pw + 1)), float(max(mlo, moq - step))
        assert best is not None
        _, pw, moq, sim, r = best
        p["forecast_window_p"], p["moq_weeks"] = pw, moq
        E["sim"], E["ratios"], E["attempts"] = sim, r, attempts
        L = p["lead_weeks"]
        chen = chen_bullwhip_bound(L + 1, pw)
        E["chen"] = chen
        return [
            Anomaly(id="A1", kind="bullwhip_amplification", pillar="operations_strategy", title="Order variance amplified up the chain",
                    mechanism=(f"From {P[1]} the company forecasts retailer orders (not POS) with a {pw}-week moving average and orders up to "
                               f"(L+1) x forecast + z x sigma (L = {L} weeks); from {P[3]} POs are rounded to a {moq:.1f}-week MOQ. Realized median "
                               f"variance ratios over {P[4]}-{P[7]}: retailer orders / POS {r['t2']:.2f}, POs / POS {r['t3']:.2f} "
                               f"(company stage {r['stage']:.2f} vs Chen bound {chen:.2f})."),
                    periods=list(P[1:]), affected_accounts=["1220", "2000"], affected_tables=["purchase_orders", "orders", "pos_sales", "forecast_config"],
                    magnitude={"amplification_tier2": round(r["t2"], 6), "amplification_tier3": round(r["t3"], 6), "forecast_window_p": float(pw),
                               "moq_weeks": round(moq, 6), "chen_bound": round(chen, 6), "company_stage_ratio": round(r["stage"], 6)}, severity="high"),
            Anomaly(id="A2", kind="promo_forward_buy", pillar="operations_strategy", title="Promotion forward-buy and trough",
                    mechanism=(f"The {P[5]} promotion (-15% for 4 weeks) made retailers forward-buy x{p['forward_buy_mult']:.2f}; POS moved only "
                               "+10% and gave it back over the next 8 weeks; the company chased the spike with its short forecast window."),
                    periods=[P[5], P[6]], affected_accounts=["4110", "1220"], affected_tables=["orders", "pos_sales"],
                    magnitude={"forward_buy_mult": round(p["forward_buy_mult"], 6)}, severity="medium"),
            Anomaly(id="A3", kind="shortage_gaming_supplier_fill", pillar="operations_strategy", title="Rationing game and supplier fill-rate drop",
                    mechanism=(f"When our fill rate on a SKU falls below 90% retailers inflate orders x{p['gaming_mult']:.2f}; the supplier "
                               f"(capacity {p['cap_mult']:.1f}x mean weekly demand) cannot ship the spiky POs on time and backorders the rest "
                               "(its lead-time distribution is unchanged)."),
                    periods=list(P[3:]), affected_accounts=["5030"], affected_tables=["supplier_shipments", "orders"],
                    magnitude={"gaming_mult": round(p["gaming_mult"], 6), "supplier_cap_mult": round(p["cap_mult"], 6)}, severity="medium"),
            Anomaly(id="A4", kind="inventory_cost_consequences", pillar="accounting_finance", title="Air freight, inventory swings and a write-down",
                    mechanism=(f"Backordered quantities are flown in at {p['air_freight_mult']:.0f}x freight (GL 5030, source 'expedite'); inventory "
                               f"oscillates and DIO rises; slow movers are written down by ~{p['writedown_pct']:.1%} of inventory in {P[7]} (GL 5040)."),
                    periods=list(P[5:]), affected_accounts=["5030", "5040", "1230", "1220"], affected_tables=["supplier_shipments", "inventory_snapshots", "gl_lines"],
                    magnitude={"writedown_pct": round(p["writedown_pct"], 6)}, severity="high"),
        ]

    # ------------------------------------------------------------------ frames

    def _derive(self, ctx: BuildContext, state: WorldState) -> None:
        """Prices (calibrated to the revenue target) and every table from the simulated chain."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        E = state.extras
        sim = E["sim"]
        weeks = E["weeks"]
        n_w = E["n_w"]
        R, S = p["n_retailers"], p["n_skus"]
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        promo = np.zeros(n_w, dtype=bool)
        promo[E["promo_w0"]: E["promo_w0"] + 4] = True
        # price calibration: last 52 weeks of shipments at list price less promo allowance ~= target revenue
        ship_s = sim["shipped"].sum(axis=0)
        raw_price = E["unit_cost_raw"] / (1 - p["gm"])
        rev_raw = float((ship_s[:, -52:] * raw_price[:, None] * np.where(promo[-52:], 1 - p["promo_discount"], 1.0)[None, :]).sum())
        k = p["revenue_ttm_cents"] / rev_raw
        unit_cost = np.maximum(10, np.round(E["unit_cost_raw"] * k)).astype(np.int64)
        price = np.round(unit_cost / (1 - p["gm"])).astype(np.int64)
        E["unit_cost"], E["price"] = unit_cost, price
        sku_ids = np.array([f"SKU-{1000 + 7 * i}" for i in range(S)])
        prod_ids = np.array([f"P{i:04d}" for i in range(1, S + 1)])
        cust_ids = np.array([f"R{i:03d}" for i in range(1, R + 1)])
        cat = np.array(CATEGORIES)[g.integers(0, len(CATEGORIES), S)]
        products = pd.DataFrame({"product_id": prod_ids, "sku": sku_ids, "title": [f"{c} item {i + 1}" for i, c in enumerate(cat)], "category": cat,
                                 "unit_cost_cents": unit_cost, "list_price_cents": price, "supplier_id": "V001", "active": True,
                                 "introduced_at": pd.Timestamp(cal.start) - pd.to_timedelta(g.integers(200, 2000, S), unit="D")})
        customers = pd.DataFrame({"customer_id": cust_ids, "name": [f"{ctx.rng.spawn(f'name:{i}').company_name('retail')}" for i in range(R)],
                                  "channel": np.array(("grocery", "mass", "drug", "club", "online", "convenience", "regional"))[np.arange(R) % 7],
                                  "region": np.array(("Northeast", "Southeast", "Midwest", "West", "Southwest", "Canada", "Mountain"))[np.arange(R) % 7],
                                  "payment_terms_days": p["terms_days"], "shares_pos_data": True})
        # retailer order lines (tier 1 -> us)
        o, sh = sim["orders"], sim["shipped"]
        r_i, s_i, t_i = np.nonzero(o)
        qty_o = o[r_i, s_i, t_i].astype(np.int64)
        qty_s = sh[r_i, s_i, t_i].astype(np.int64)
        order_ts = weeks[t_i] + pd.to_timedelta(1, unit="D") + pd.to_timedelta(g.integers(8 * 3600, 17 * 3600, len(t_i)), unit="s")
        ship_ts = weeks[t_i] + pd.to_timedelta(3, unit="D") + pd.to_timedelta(g.integers(7 * 3600, 18 * 3600, len(t_i)), unit="s")
        allow = np.where(promo[t_i], p["promo_discount"], 0.0)
        gross = qty_s * price[s_i]
        allowance = np.round(gross * allow).astype(np.int64)
        net = gross - allowance
        pay_at = pd.Series(ship_ts).dt.normalize() + pd.to_timedelta(p["terms_days"] + np.maximum(-5, g.normal(3, 6, len(t_i))), unit="D") + pd.Timedelta(hours=11)
        shipped_ok = (pd.Series(ship_ts) <= end_ts).to_numpy() & (qty_s > 0)
        paid = shipped_ok & (pay_at <= end_ts).to_numpy()
        orders = pd.DataFrame({
            "order_id": [f"SO{i:06d}" for i in range(1, len(t_i) + 1)], "customer_id": cust_ids[r_i], "product_id": prod_ids[s_i], "sku": sku_ids[s_i],
            "order_date": pd.Series(order_ts), "qty": qty_o, "qty_shipped": np.where(shipped_ok, qty_s, 0), "ship_date": pd.Series(ship_ts).where(shipped_ok),
            "unit_price_cents": price[s_i], "promo_allowance_pct": allow * 100, "gross_cents": np.where(shipped_ok, gross, 0),
            "allowance_cents": np.where(shipped_ok, allowance, 0), "invoice_cents": np.where(shipped_ok, net, 0), "paid_at": pay_at.where(paid),
            "status": np.where(~shipped_ok, "cancelled_stockout", np.where(qty_s < qty_o, "short_shipped", "shipped")),
        })
        orders = orders.sort_values(["order_date", "order_id"], kind="stable").reset_index(drop=True)
        orders["order_id"] = [f"SO{i:06d}" for i in range(1, len(orders) + 1)]
        # purchase orders (us -> supplier)
        po = sim["po"]
        ps_i, pt_i = np.nonzero(po)
        po_ts = weeks[pt_i] + pd.to_timedelta(2, unit="D") + pd.to_timedelta(g.integers(9 * 3600, 16 * 3600, len(pt_i)), unit="s")
        po_ids = np.array([f"PO{i:05d}" for i in range(1, len(pt_i) + 1)])
        pos_df = pd.DataFrame({"po_id": po_ids, "supplier_id": "V001", "product_id": prod_ids[ps_i], "sku": sku_ids[ps_i], "order_date": pd.Series(po_ts),
                               "qty": po[ps_i, pt_i].astype(np.int64), "unit_cost_cents": unit_cost[ps_i],
                               "expected_at": pd.Series(po_ts).dt.normalize() + pd.Timedelta(days=7 * p["lead_weeks"] - 2),
                               "_s": ps_i, "_t": pt_i})
        # supplier shipments: on-time (lead L weeks) and backorders flown in (1 week)
        L = p["lead_weeks"]
        rows = []
        for kind, arr, lag in (("ontime", sim["ontime"], L), ("back", sim["back"], 1)):
            si, ti = np.nonzero(arr)
            rec_w = ti + lag
            rec_ts = np.where(rec_w < n_w, (weeks[np.minimum(rec_w, n_w - 1)] + pd.Timedelta(hours=7)).to_numpy(), np.datetime64("NaT", "ns"))
            # link to the most recent PO of the SKU at or before the shipping week
            po_key = pos_df["_s"].to_numpy() * 100_000 + pos_df["_t"].to_numpy()
            order_po = np.argsort(po_key, kind="stable")
            pos_idx = np.searchsorted(po_key[order_po], si * 100_000 + ti, side="right") - 1
            link = np.where((pos_idx >= 0) & (pos_df["_s"].to_numpy()[order_po][np.maximum(pos_idx, 0)] == si), po_ids[order_po][np.maximum(pos_idx, 0)], None)
            ship_d = weeks[ti] + pd.Timedelta(days=4)
            rows.append(pd.DataFrame({"po_id": link, "product_id": prod_ids[si], "sku": sku_ids[si], "ship_date": ship_d, "received_at": rec_ts,
                                      "qty": arr[si, ti].astype(np.int64), "is_backorder": int(kind == "back"), "expedited": int(kind == "back"),
                                      "mode": "air" if kind == "back" else "truck", "_s": si, "_t": ti}))
        ss = pd.concat(rows, ignore_index=True)
        ss["received_at"] = pd.to_datetime(ss["received_at"])
        ss = ss.sort_values(["ship_date", "sku", "is_backorder"], kind="stable").reset_index(drop=True)
        ss.insert(0, "shipment_id", [f"SS{i:06d}" for i in range(1, len(ss) + 1)])
        value = ss["qty"].to_numpy() * unit_cost[ss["_s"].to_numpy()]
        ss["value_cents"] = value
        ss["freight_cents"] = np.round(value * p["freight_pct"] * np.where(ss["expedited"] == 1, p["air_freight_mult"], 1.0)).astype(np.int64)
        lead_days = (ss["received_at"] - pd.to_datetime(pos_df.set_index("po_id")["order_date"].reindex(ss["po_id"]).to_numpy())).dt.total_seconds() / 86400
        ss["lead_time_days"] = np.round(lead_days.to_numpy(), 2)
        ss["paid_at"] = (ss["received_at"].dt.normalize() + pd.Timedelta(days=p["supplier_terms_days"]) + pd.Timedelta(hours=10))
        ss.loc[ss["paid_at"] > end_ts, "paid_at"] = pd.NaT
        ss = ss[ss["ship_date"] <= end_ts].reset_index(drop=True)
        # POS feed (aggregated per SKU-week) and inventory snapshots
        pos_s = E["pos"].sum(axis=0)
        pos_df_out = pd.DataFrame({"sku": np.repeat(sku_ids, n_w), "product_id": np.repeat(prod_ids, n_w), "sale_date": np.tile(weeks, S),
                                   "qty": pos_s.reshape(-1).astype(np.int64), "stores_reporting": np.repeat(E["carry"].sum(axis=0), n_w) * 25})
        pos_df_out = pos_df_out.sort_values(["sale_date", "sku"], kind="stable").reset_index(drop=True)
        pos_df_out.insert(0, "pos_id", np.arange(1, len(pos_df_out) + 1, dtype=np.int64))
        inv = sim["inv_end"]
        dmd = np.maximum(1.0, pd.DataFrame(sim["orders"].sum(axis=0).T).rolling(4, min_periods=1).mean().to_numpy().T)
        snaps = pd.DataFrame({"sku": np.tile(sku_ids, n_w), "product_id": np.tile(prod_ids, n_w), "snapshot_date": np.repeat(weeks + pd.Timedelta(days=6), S),
                              "on_hand_units": inv.T.reshape(-1).astype(np.int64), "on_order_units": sim["onord_end"].T.reshape(-1).astype(np.int64),
                              "unit_cost_cents": np.tile(unit_cost, n_w), "weeks_of_cover": np.round((inv / dmd).T.reshape(-1), 2)})
        snaps = snaps[snaps["snapshot_date"] <= end_ts].reset_index(drop=True)
        snaps.insert(0, "snapshot_id", np.arange(1, len(snaps) + 1, dtype=np.int64))
        P = state.periods
        cfg = pd.DataFrame([
            {"config_id": 1, "effective_from": pd.Timestamp(cal.start), "demand_signal": "retailer_orders", "forecast_method": "moving_average",
             "forecast_window_weeks": P_OLD, "moq_weeks_of_demand": MOQ_OLD, "safety_z": round(p["z"], 2), "review_period_weeks": 1,
             "note": "legacy planning tool"},
            {"config_id": 2, "effective_from": pd.Timestamp(cal.bounds(P[1])[0]), "demand_signal": "retailer_orders", "forecast_method": "moving_average",
             "forecast_window_weeks": p["forecast_window_p"], "moq_weeks_of_demand": MOQ_OLD, "safety_z": round(p["z"], 2), "review_period_weeks": 1,
             "note": "forecast tool upgrade: 'responsive' window"},
            {"config_id": 3, "effective_from": pd.Timestamp(cal.bounds(P[3])[0]), "demand_signal": "retailer_orders", "forecast_method": "moving_average",
             "forecast_window_weeks": p["forecast_window_p"], "moq_weeks_of_demand": round(p["moq_weeks"], 2), "safety_z": round(p["z"], 2),
             "review_period_weeks": 1, "note": "new supplier contract: MOQ for a lower unit price"},
        ])
        state.frames.update({"products": products, "customers": customers, "orders": orders, "purchase_orders": pos_df, "supplier_shipments": ss,
                             "pos_sales": pos_df_out, "inventory_snapshots": snaps, "forecast_config": cfg})
        if p["factory_tier"]:
            E["factory"] = self._factory_orders(sim["po"], g)
            fs, ft = np.nonzero(E["factory"])
            state.frames["supplier_production_orders"] = pd.DataFrame({
                "production_order_id": [f"MO{i:05d}" for i in range(1, len(fs) + 1)], "sku": sku_ids[fs], "product_id": prod_ids[fs],
                "order_date": weeks[ft] + pd.Timedelta(days=4), "qty": E["factory"][fs, ft].astype(np.int64)}).sort_values(
                ["order_date", "sku"], kind="stable").reset_index(drop=True)

    def _factory_orders(self, po: np.ndarray, g: np.random.Generator) -> np.ndarray:
        """Tier 4 (principal): the supplier's own MA(4) order-up-to on our POs (lead 2 weeks, runs in batches of 2 weeks)."""
        S, n_w = po.shape
        mu = po.mean(axis=1) + 1e-9
        inv = 3 * mu
        pipe = np.zeros((S, n_w + 4))
        out = np.zeros((S, n_w))
        for t in range(n_w):
            inv = inv + pipe[:, t] - po[:, t]
            lo = max(0, t - 3)
            d = po[:, lo: t + 1].mean(axis=1)
            sd = po[:, lo: t + 1].std(axis=1, ddof=1) if t - lo >= 1 else 0.2 * mu
            target = 3 * d + 1.65 * sd * math.sqrt(3)
            q = np.maximum(0.0, target - inv - pipe[:, t + 1: t + 3].sum(axis=1))
            q = np.where(q > 0, np.ceil(q / (2 * mu)) * 2 * mu, 0.0)
            out[:, t] = np.round(q)
            pipe[:, t + 2] += out[:, t]
        return out

    # ------------------------------------------------------------------ ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Derive tables, then post the books month by month."""
        self._derive(ctx, state)
        self._books(ctx, state)

    def _books(self, ctx: BuildContext, state: WorldState) -> None:
        p, cal = state.params, state.calendar
        E = state.extras
        L = Ledger()
        state.ledger = L
        gran = GL_BATCH[p["difficulty"]]
        months = E["months"]
        end_d = cal.end
        R = p["revenue_ttm_cents"]
        uc = E["unit_cost"]
        inv0 = int((E["sim"]["c_inv0"] * uc).sum())
        ar0 = cents(R / 12 * p["terms_days"] / 30.4)
        ap0 = cents(R * (1 - p["gm"]) / 12 * p["supplier_terms_days"] / 30.4)
        ppe = cents(R * 0.04)
        cash0 = cents(R * 0.05)
        debt = cents(R * 0.08 / 100_000) * 100_000
        net = cash0 + ar0 + inv0 + ppe - ap0 - debt
        cs = cents(0.5 * net) if net > 0 else 1
        L.open_books(cal.opening_date, {"1000": cash0, "1100": ar0, "1220": inv0, "1500": ppe, "2000": -ap0, "2610": -debt, "3000": -cs,
                                        "3100": -(net - cs)})
        events: list[tuple[dt.date, int, Any]] = []

        def add(d: Any, prio: int, fn: Any) -> None:
            events.append((d.date() if isinstance(d, pd.Timestamp) else d, prio, fn))

        for k, a in enumerate(split_cents(ar0, [0.5, 0.35, 0.15])):
            add(cal.start + dt.timedelta(days=12 + 14 * k), 4, lambda d, a=a: L.collect_cash(d, a, memo="Collection of opening receivables"))
        for k, a in enumerate(split_cents(ap0, [0.6, 0.4])):
            add(cal.start + dt.timedelta(days=10 + 15 * k), 5, lambda d, a=a: L.pay_ap(d, a, memo="Opening payables paid"))
        ss = state.frames["supplier_shipments"]
        rec = ss[ss["received_at"].notna()]
        rb = rec.assign(b=batch_end_dates(rec["received_at"], gran)).groupby("b", sort=True).agg(v=("value_cents", "sum"), n=("shipment_id", "size"))
        for b, r in rb.iterrows():
            add(b, 2, lambda d, a=int(r.v), n=int(r.n): L.purchase_inventory(d, a, memo="Supplier receipts", dim={"rows": str(n)}))
        for exp, src, memo in ((0, "freight", "Inbound freight (truck)"), (1, "expedite", "Air freight on backordered supplier quantities")):
            sub = rec[rec["expedited"] == exp]
            fb = sub.assign(b=batch_end_dates(sub["received_at"], gran)).groupby("b", sort=True)["freight_cents"].sum()
            for b, a in fb.items():
                add(b, 2, lambda d, a=int(a), s_=src, m_=memo: L.entry(d, [("5030", a, 0, {}), ("2000", 0, a, {})], m_, s_))
        pay = rec[rec["paid_at"].notna()]
        pb = pay.assign(b=batch_end_dates(pay["paid_at"], gran), t=pay["value_cents"] + pay["freight_cents"]).groupby("b", sort=True)["t"].sum()
        for b, a in pb.items():
            add(b, 5, lambda d, a=int(a): L.pay_ap(d, a, memo="Supplier and carrier payments"))
        od = state.frames["orders"]
        sh = od[od["ship_date"].notna()]
        sb = sh.assign(b=batch_end_dates(sh["ship_date"], gran), cogs=sh["qty_shipped"] * uc[sh["product_id"].map(
            {pid: i for i, pid in enumerate(state.frames["products"]["product_id"])}).to_numpy()]).groupby("b", sort=True).agg(
            g=("gross_cents", "sum"), al=("allowance_cents", "sum"), c=("cogs", "sum"), n=("order_id", "size"))
        for b, r in sb.iterrows():
            dim = {"batch": f"shipments:{b.date()}", "rows": str(int(r.n))}
            add(b, 3, lambda d, a=int(r.g), dm=dim: L.sell_on_credit(d, a, memo="Shipments to retailers invoiced", dim=dm))
            if r.al:
                add(b, 3, lambda d, a=int(r.al), dm=dim: L.entry(d, [("4110", a, 0, dm), ("1100", 0, a, dm)], "Promotional allowances", "promotions"))
            add(b, 3, lambda d, a=int(r.c), dm=dim: L.relieve_inventory_cogs(d, a, memo="COGS on shipments", dim=dm))
        pd_ = sh[sh["paid_at"].notna()]
        cb = pd_.assign(b=batch_end_dates(pd_["paid_at"], gran)).groupby("b", sort=True)["invoice_cents"].sum()
        for b, a in cb.items():
            add(b, 4, lambda d, a=int(a): L.collect_cash(d, a, memo="Retailer payments"))
        # warehousing (2%/month of month-end inventory value), opex, payroll, depreciation, interest, tax
        snaps = state.frames["inventory_snapshots"]
        sv = snaps.assign(v=snaps["on_hand_units"] * snaps["unit_cost_cents"], mo=snaps["snapshot_date"].dt.strftime("%Y-%m")).groupby("mo")["v"].mean()
        opex_m = cents(R / 12 * max(0.05, p["gm"] - p["op_margin"] - 0.03))
        P8 = state.periods[-1]
        p8_end = cal.bounds(P8)[1]
        prev = {"pay": 0, "accr": 0}
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            if m > 0:
                add(ms + dt.timedelta(days=4), 1, lambda d, a=prev["pay"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll"))
                add(ms + dt.timedelta(days=9), 1, lambda d, a=prev["accr"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued expenses paid", source="opex"))
            if ms.month in (1, 4, 7, 10) and m > 0:
                add(ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"]))
            hold = cents(float(sv.get(f"{ms:%Y-%m}", sv.iloc[0])) * p["holding_pct_month"])
            payroll = {"6000": cents(opex_m * 0.25), "6200": cents(opex_m * 0.2), "5010": cents(opex_m * 0.15)}
            accr = {"6400": hold + cents(opex_m * 0.15), "6000": cents(opex_m * 0.1), "6200": opex_m - sum(payroll.values()) - cents(opex_m * 0.25)}
            add(me, 7, lambda d, s_=payroll: L.payroll(d, s_, memo="Payroll (sales, G&A, warehouse)"))
            add(me, 7, lambda d, s_=accr: L.accrue_opex(d, s_, memo="Warehousing (inventory carrying), marketing and G&A"))
            add(ms + dt.timedelta(days=14), 6, lambda d: L.capex(d, cents(R * 0.003), memo="Warehouse equipment"))
            add(me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 84)))
            add(me, 9, lambda d: L.accrue_interest(d, cents((-L.closing_balances()["2610"] - L.closing_balances()["2500"]) * p["interest_rate"] / 12)))
            if ms.month % 3 == 0:
                add(me, 10, lambda d: L.pay_interest(d, -L.closing_balances()["2150"]))
                if me == p8_end:
                    add(me, 10, lambda d: L.inventory_writedown(d, self._writedown(state), memo="Write-down of slow-moving inventory (excess weeks of cover)"))

                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))

                add(me, 11, tax)
            prev = {"pay": sum(payroll.values()), "accr": sum(accr.values())}
        events.sort(key=lambda e: (e[0], e[1]))
        floor = cents(R / 12 * 0.08)
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

    def _writedown(self, state: WorldState) -> int:
        """P8 write-down: ``writedown_pct`` of inventory value, taken on the SKUs above the 70th percentile of weeks of cover."""
        E = state.extras
        if "writedown" in E:
            return E["writedown"]
        snaps = state.frames["inventory_snapshots"]
        last = snaps[snaps["snapshot_date"] == snaps["snapshot_date"].max()]
        val = (last["on_hand_units"] * last["unit_cost_cents"]).to_numpy()
        cover = last["weeks_of_cover"].to_numpy()
        slow = cover >= np.quantile(cover, 0.7)
        target = state.params["writedown_pct"] * val.sum()
        amt = int(min(target, val[slow].sum() * 0.5))
        E["writedown"] = amt
        E["writedown_skus"] = last["sku"].to_numpy()[slow].tolist()
        return amt

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """Supply-chain tables + 2/4/6 dirt rules (never on GL-tied money columns)."""
        p = state.params
        F = state.frames
        dirt_plan = [
            ("customers", "case_mixed:channel"), ("supplier_shipments", "nulls:mode:0.05"), ("products", "whitespace:category"),
            ("inventory_snapshots", "nulls:weeks_of_cover:0.03"), ("orders", "dup_keys:0.01"), ("purchase_orders", "tz_mixed:order_date"),
        ]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[: {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]]:
            dirt.setdefault(t, []).append(rule)
        money = {c: "BIGINT" for c in ("unit_cost_cents", "list_price_cents", "unit_price_cents", "gross_cents", "allowance_cents", "invoice_cents",
                                        "value_cents", "freight_cents")}
        ints = {c: "INTEGER" for c in ("qty", "qty_shipped", "on_hand_units", "on_order_units", "is_backorder", "expedited", "forecast_window_weeks",
                                        "review_period_weeks", "payment_terms_days", "stores_reporting")}
        builder.add_frame("products", F["products"], primary_key=["product_id"], foreign_keys=[], column_types=money, dirt=dirt.get("products", ()))
        builder.add_frame("customers", F["customers"], primary_key=["customer_id"], foreign_keys=[], column_types=ints, dirt=dirt.get("customers", ()),
                          description="Retail chains (tier 1) we supply.")
        builder.add_frame("suppliers", pd.DataFrame([{"supplier_id": "V001", "name": ctx.rng.spawn("supplier").company_name("manufacturing"),
                                                       "country": "US", "lead_time_days_quoted": 7 * p["lead_weeks"],
                                                       "payment_terms_days": p["supplier_terms_days"], "deposit_pct": 0.0}]), primary_key=["supplier_id"])
        builder.add_frame("pos_sales", F["pos_sales"], primary_key=["pos_id"], foreign_keys=[("product_id", "products", "product_id")],
                          column_types={**ints, "pos_id": "BIGINT", "sale_date": "DATE"},
                          descriptions={"sale_date": "week (Monday) of consumer sell-through", "qty": "units sold to consumers across all reporting stores"},
                          description="Weekly point-of-sale feed shared by the retail chains (aggregated per SKU).")
        builder.add_frame("orders", F["orders"], primary_key=["order_id"], foreign_keys=[("customer_id", "customers", "customer_id"), ("product_id", "products", "product_id")],
                          column_types={**money, **ints, "promo_allowance_pct": "DOUBLE"}, dirt=dirt.get("orders", ()),
                          descriptions={"qty": "units ordered by the retailer", "qty_shipped": "units we shipped (shortfall is lost)",
                                        "invoice_cents": "invoiced amount net of promotional allowance (AR sub-ledger)", "paid_at": "NULL while open",
                                        "ingest_id": ">1 marks a re-ingested duplicate of the same order_id"},
                          description="Retailer purchase orders to us (one line per retailer x SKU x week).")
        builder.add_frame("purchase_orders", F["purchase_orders"].drop(columns=["_s", "_t"]), primary_key=["po_id"],
                          foreign_keys=[("product_id", "products", "product_id")], column_types={**money, **ints, "expected_at": "DATE"},
                          dirt=dirt.get("purchase_orders", ()), description="Our purchase orders to the supplier.")
        builder.add_frame("supplier_shipments", F["supplier_shipments"].drop(columns=["_s", "_t"]), primary_key=["shipment_id"],
                          foreign_keys=[("po_id", "purchase_orders", "po_id"), ("product_id", "products", "product_id")],
                          column_types={**money, **ints, "lead_time_days": "DOUBLE"}, dirt=dirt.get("supplier_shipments", ()),
                          descriptions={"is_backorder": "1 = quantity the supplier could not ship with the original PO",
                                        "expedited": "1 = flown in (air freight)", "received_at": "NULL while in transit", "paid_at": "NULL while unpaid"},
                          description="Supplier shipments received (AP / inventory sub-ledger).")
        builder.add_frame("inventory_snapshots", F["inventory_snapshots"], primary_key=["snapshot_id"],
                          foreign_keys=[("product_id", "products", "product_id")], column_types={**money, **ints, "snapshot_id": "BIGINT", "snapshot_date": "DATE"},
                          dirt=dirt.get("inventory_snapshots", ()), descriptions={"weeks_of_cover": "on-hand / trailing 4-week retailer orders"})
        builder.add_frame("forecast_config", F["forecast_config"], primary_key=["config_id"], foreign_keys=[], column_types=ints,
                          description="Replenishment planning parameters (history).")
        if "supplier_production_orders" in F:
            builder.add_frame("supplier_production_orders", F["supplier_production_orders"], primary_key=["production_order_id"],
                              foreign_keys=[("product_id", "products", "product_id")], column_types=ints,
                              description="The supplier's production orders to its factory (shared via the supplier portal).")

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """AR, AP, inventory (receipts - shipments at cost) and revenue ties - all hold."""
        cal, E, F = state.calendar, state.extras, state.frames
        od, ss = F["orders"], F["supplier_shipments"]
        uc = E["unit_cost"]
        idx = {pid: i for i, pid in enumerate(F["products"]["product_id"])}
        cost = od["qty_shipped"].to_numpy() * uc[od["product_id"].map(idx).to_numpy()]
        inv0 = int((E["sim"]["c_inv0"] * uc).sum())
        ar, ap, inv, rev = {}, {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            e_ts = pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            shipped = (od["ship_date"].notna() & (od["ship_date"] <= e_ts)).to_numpy()
            open_ = (od["paid_at"].isna() | (od["paid_at"] > e_ts)).to_numpy()
            ar[period] = int(od.loc[shipped & open_, "invoice_cents"].sum())
            rev[period] = int(od.loc[shipped & (od["ship_date"] >= pd.Timestamp(s)).to_numpy(), "gross_cents"].sum())
            rec = (ss["received_at"].notna() & (ss["received_at"] <= e_ts)).to_numpy()
            unpaid = (ss["paid_at"].isna() | (ss["paid_at"] > e_ts)).to_numpy()
            ap[period] = int((ss["value_cents"] + ss["freight_cents"])[rec & unpaid].sum())
            inv[period] = int(inv0 + ss.loc[rec, "value_cents"].sum() - cost[shipped].sum())
        return [
            SubledgerTie("ar_open_invoices", ar, ["1100"], description="Shipped, unpaid retailer invoices (net of allowances) == GL 1100"),
            SubledgerTie("ap_open_receipts", ap, ["2000"], description="Received, unpaid supplier shipments + freight == GL 2000"),
            SubledgerTie("inventory_receipts_less_shipments", inv, ["1220"], description="Opening + receipts - shipments at unit cost == GL 1220"),
            SubledgerTie("revenue_gross_shipments", rev, ["4000"], basis="activity", description="Gross invoiced shipments == GL 4000 activity"),
        ]

    # ------------------------------------------------------------------ facts

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        p, cal, E = state.params, state.calendar, state.extras
        P = state.periods
        sim = E["sim"]
        w5 = E["w5"]
        weeks = E["weeks"]
        pos_s = E["pos"].sum(axis=0).astype(float)
        ro = sim["orders"].sum(axis=0)
        po = sim["po"]

        def cv(x: np.ndarray) -> float:
            m = x.mean(axis=1)
            return float(np.nanmedian(x.std(axis=1, ddof=1) / np.where(m > 0, m, np.nan)))

        f: dict[str, Any] = {"cv_pos": cv(pos_s[:, w5:]), "cv_ro": cv(ro[:, w5:]), "cv_po": cv(po[:, w5:])}
        f.update({"t2": E["ratios"]["t2"], "t3": E["ratios"]["t3"], "stage": E["ratios"]["stage"], "chen": E["chen"]})
        if p["factory_tier"]:
            f["t4"] = float(np.nanmedian(_var_ratio(E["factory"][:, w5:], pos_s[:, w5:])))
        pw0 = E["promo_w0"]
        base = ro[:, pw0 - 8: pw0].sum(axis=0).mean()
        f["forward_buy_multiple"] = float(ro[:, pw0: pw0 + 4].sum(axis=0).mean() / base) if base else 0.0
        around = 0.5 * (pos_s[:, pw0 - 12: pw0].sum() + pos_s[:, pw0 + 12: pw0 + 24].sum())  # surrounding windows cancel the seasonal slope
        f["pos_promo_window_change"] = float(pos_s[:, pw0: pw0 + 12].sum() / around - 1)
        L = state.ledger
        f["expedite_p7_p8"] = int(L.sum_lines(P[6], ["5030"], "debit", "expedite") + L.sum_lines(P[7], ["5030"], "debit", "expedite"))
        f["writedown_p8"] = int(L.activity(P[7])["5040"])
        r = state.statements.ratios  # type: ignore[union-attr]
        f["dio_ratio"] = r[P[7]]["dio"] / r[P[0]]["dio"] if r[P[0]]["dio"] else 0.0
        s8 = int(np.searchsorted(weeks, pd.Timestamp(cal.bounds(P[7])[0])))
        f["fill_p8"] = float(sim["shipped"][:, :, s8:].sum() / max(1.0, sim["orders"][:, :, s8:].sum()))
        f["fill_by_quarter"] = {}
        for i, per in enumerate(P):
            a = int(np.searchsorted(weeks, pd.Timestamp(cal.bounds(per)[0])))
            b = int(np.searchsorted(weeks, pd.Timestamp(cal.bounds(per)[1]) + pd.Timedelta(days=1)))
            f["fill_by_quarter"][per] = float(sim["shipped"][:, :, a:b].sum() / max(1.0, sim["orders"][:, :, a:b].sum()))
        uc = E["unit_cost"]
        f["inv_value_actual_p5_p8"] = float((sim["inv_end"][:, w5:] * uc[:, None]).sum(axis=0).mean())
        cf = self._simulate(state, np.random.default_rng(0), p_window=12, moq_weeks=0.05, signal="pos", gaming=True, forward_buy=True)
        f["inv_value_pos_policy"] = float((cf["inv_end"][:, w5:] * uc[:, None]).sum(axis=0).mean())
        f["fill_pos_policy"] = float(cf["shipped"][:, :, w5:].sum() / max(1.0, cf["orders"][:, :, w5:].sum()))
        cfr = self._ratios(state, cf, w5)
        f["t3_pos_policy"] = cfr["t3"]
        ss = state.frames["supplier_shipments"]
        ont = ss[(ss["is_backorder"] == 0) & ss["lead_time_days"].notna()]
        f["lead_by_quarter"] = {per: float(g_["lead_time_days"].mean()) for per, g_ in ont.groupby(cal.labels(ont["ship_date"]))}
        f["supplier_fill_by_quarter"] = {}
        for per in P:
            a = int(np.searchsorted(weeks, pd.Timestamp(cal.bounds(per)[0])))
            b = int(np.searchsorted(weeks, pd.Timestamp(cal.bounds(per)[1]) + pd.Timedelta(days=1)))
            f["supplier_fill_by_quarter"][per] = float(sim["ontime"][:, a:b].sum() / max(1.0, sim["po"][:, a:b].sum()))
        lost = (sim["orders"] - sim["shipped"])[:, :, w5:].sum(axis=0)
        f["lost_sales_cents_p5_p8"] = float((lost * E["price"][:, None]).sum())
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Supply chain (lead times, policies, per-tier ratios), DC queueing, pricing and KPIs by period."""
        p, cal, E = state.params, state.calendar, state.extras
        f = self._facts(state)
        P = state.periods
        sim = E["sim"]
        mu_s = E["mu_rs"].sum(axis=0)
        L = p["lead_weeks"]
        ss = state.frames["supplier_shipments"]
        ont = ss[(ss["is_backorder"] == 0) & ss["lead_time_days"].notna()]["lead_time_days"]
        top = int(np.argmax(mu_s))
        sd_top = float(sim["orders"].sum(axis=0)[top].std(ddof=1))
        supply = {
            "lead_time_dist": {"supplier_mean_days": float(ont.mean()), "supplier_sd_days": float(ont.std(ddof=1)),
                               "p10_days": float(ont.quantile(0.1)), "p90_days": float(ont.quantile(0.9)), "by_quarter_mean_days": f["lead_by_quarter"],
                               "retailer_lead_weeks": 1, "air_freight_weeks": 1},
            "order_up_to": {"formula": "S = (L+1) * MA_p(retailer orders) + z * sigma * sqrt(L+1)", "L_weeks": L, "p_weeks": p["forecast_window_p"],
                            "z": p["z"], "moq_weeks": p["moq_weeks"], "top_sku_S_units": float((L + 1) * mu_s[top] + p["z"] * sd_top * math.sqrt(L + 1))},
            "safety_stock_top_sku_units": safety_stock(p["z"], sd_top, L + 1),
            "eoq_top_sku_units": eoq(float(mu_s[top]) * 52, 25_000, max(1.0, float(E["unit_cost"][top]) * 0.24)),
            "bullwhip_ratio_by_tier": {"tier1_retailer_orders_vs_pos": f["t2"], "tier2_company_pos_vs_pos": f["t3"], "company_stage_pos_vs_orders": f["stage"],
                                       **({"tier3_supplier_production_vs_pos": f["t4"]} if "t4" in f else {})},
            "chen_lower_bound_company_stage": f["chen"],
            "cv_weekly": {"pos": f["cv_pos"], "retailer_orders": f["cv_ro"], "purchase_orders": f["cv_po"]},
            "fill_rate_by_quarter": f["fill_by_quarter"], "supplier_on_time_fill_by_quarter": f["supplier_fill_by_quarter"],
        }
        od = state.frames["orders"]
        s8, e8 = cal.bounds(P[-1])
        lines8 = od[(od["order_date"] >= pd.Timestamp(s8)) & (od["order_date"] <= pd.Timestamp(e8) + pd.Timedelta(days=1))]
        hours = 13 * 5 * 16.0
        lam = len(lines8) / hours
        mu = lam / 0.82
        q = mm1(lam, mu)
        stations = [Station(name="dc_receiving", capacity_per_hour=len(ss) / (len(E["weeks"]) * 80.0) / 0.6, utilization=0.6,
                            wip=0.6 / (1 - 0.6), cycle_time=(0.6 / (1 - 0.6)) / max(1e-9, len(ss) / (len(E["weeks"]) * 80.0))),
                    Station(name="dc_pick_pack", capacity_per_hour=mu, utilization=q["rho"], wip=q["L"], cycle_time=q["W"])]
        queueing = {"lambda": q["lambda"], "mu": q["mu"], "rho": q["rho"], "L": q["L"], "W": q["W"], "Lq": q["Lq"], "Wq": q["Wq"]}
        prices = state.frames["products"].groupby("category")["list_price_cents"].mean()
        pricing = {"price_ladder_cents": {c: float(v) for c, v in prices.items()}, "promo": {"period": P[5], "discount": p["promo_discount"],
                   "pos_lift": 0.10, "forward_buy_multiple": f["forward_buy_multiple"]},
                   "elasticity_by_segment": {"consumers_short_run": round(0.10 / -p["promo_discount"], 4)},
                   "discount_leakage": {"promo_allowance_cents": float(state.ledger.sum_lines(None, ["4110"], "debit"))}}
        inc = {i.period: i for i in state.statements.income}  # type: ignore[union-attr]
        r = state.statements.ratios  # type: ignore[union-attr]
        kpis = {}
        for per in P:
            tb = state.ledger.trial_balance(per)
            kpis[per] = {"revenue_cents": float(inc[per].revenue_cents), "fill_rate": f["fill_by_quarter"][per],
                         "inventory_cents": float(tb["1220"] + tb["1230"]), "dio_days": r[per]["dio"],
                         "expedite_cost_cents": float(state.ledger.sum_lines(per, ["5030"], "debit", "expedite")),
                         "supplier_on_time_fill": f["supplier_fill_by_quarter"][per], "gross_margin": r[per]["gross_margin"],
                         "net_income_cents": float(inc[per].net_income_cents)}
        E["ops_kpis"] = kpis
        unit = {"gross_margin": r[P[-1]]["gross_margin"], "arpa": float(od["invoice_cents"].mean()), "cac": 0.0, "ltv": 0.0, "payback_months": 0.0,
                "churn_monthly": 0.0, "inventory_turns_annualized": 365.0 / r[P[-1]]["dio"] if r[P[-1]]["dio"] else 0.0}
        return build_operations_state(stations=stations, queueing=queueing, unit_economics=unit, supply_chain=supply, pricing=pricing,
                                      kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Section 7 key table from the simulated chain and the books; reference SQL on the dirty DuckDB."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        P = state.periods
        P5, P6, P7, P8 = P[4], P[5], P[6], P[7]
        s5 = cal.bounds(P5)[0]
        L = p["lead_weeks"]
        pw = p["forecast_window_p"]
        metrics = [
            metric("cv_pos_demand", f["cv_pos"], "ratio", f"Median over SKUs of std/mean of weekly POS units, {P5}-{P8}.", pillar="operations_strategy",
                   tol_abs=0.05, label="CV of weekly POS demand", hint="Consumer demand at the shelf (pos_sales)."),
            metric("cv_retailer_orders", f["cv_ro"], "ratio", f"Median over SKUs of std/mean of weekly retailer order units, {P5}-{P8}.",
                   pillar="operations_strategy", tol_abs=0.05, label="CV of weekly retailer orders", hint="orders.qty by SKU-week."),
            metric("cv_company_po", f["cv_po"], "ratio", f"Median over SKUs of std/mean of weekly purchase-order units (0 in weeks without a PO), {P5}-{P8}.",
                   pillar="operations_strategy", tol_abs=0.05, label="CV of weekly purchase orders", hint="Fill weeks without a PO with zero."),
            metric("amplification_tier2", f["t2"], "x", f"Median over SKUs of var(weekly retailer orders) / var(weekly POS), {P5}-{P8}.",
                   pillar="operations_strategy", tol_rel=0.15, label="Variance amplification: retailer orders vs POS", hint="Bullwhip ratio, tier 1."),
            metric("amplification_tier3", f["t3"], "x", f"Median over SKUs of var(weekly POs) / var(weekly POS), {P5}-{P8} (weeks without POs = 0).",
                   pillar="operations_strategy", tol_rel=0.15, label="Variance amplification: our POs vs POS", hint="Bullwhip ratio, up to our supplier."),
            metric("chen_lower_bound_tier2", f["chen"], "x",
                   f"Chen et al. (2000) bound for our ordering stage: 1 + 2L/p + 2L^2/p^2 with L = lead time + review = {L + 1} weeks, p = {pw} weeks. "
                   f"Realized var(POs)/var(retailer orders) = {f['stage']:.2f} (MOQ and capacity push it above the bound).",
                   pillar="python", tol_rel=0.05, label="Chen lower bound for our ordering stage", hint="Moving-average forecast + order-up-to."),
            metric("forecast_window_p", float(pw), "weeks", f"forecast_config.forecast_window_weeks in force since {P[1]}.", pillar="sql", tol_abs=0.0,
                   label="Forecast window (weeks)", hint="Planning parameters table."),
            metric("moq_weeks_of_demand", float(p["moq_weeks"]), "weeks", f"forecast_config.moq_weeks_of_demand in force since {P[3]}.", pillar="sql",
                   tol_abs=0.3, label="MOQ in weeks of demand", hint="Supplier contract (planning parameters)."),
            metric("promo_forward_buy_multiple", f["forward_buy_multiple"], "x",
                   f"Average weekly retailer order units in the 4 promo weeks of {P6} / average of the 8 weeks before.", pillar="operations_strategy",
                   tol_abs=0.3, label="Promo forward-buy multiple", hint="Compare with POS over the same weeks."),
            metric("expedite_cost_cents_p7_p8", f["expedite_p7_p8"], "cents", f"GL 5030 debits with source 'expedite' in {P7} and {P8}.",
                   pillar="accounting_finance", tol_rel=0.08, label="Air-freight expedite cost, last two quarters", hint="Freight on backordered quantities."),
            metric("writedown_cents_p8", f["writedown_p8"], "cents", f"GL 5040 activity in {P8}.", pillar="accounting_finance", tol_rel=0.05,
                   label="Inventory write-down, latest quarter", hint="Slow movers."),
            metric("dio_p8_vs_p1_ratio", f["dio_ratio"], "x", f"DIO({P8}) / DIO({P[0]}) from the statements.", pillar="accounting_finance", tol_abs=0.15,
                   label="DIO growth", hint="Days inventory outstanding."),
            metric("fill_rate_p8", f["fill_p8"], "ratio", f"Units shipped / units ordered by retailers in {P8}.", pillar="operations_strategy", tol_abs=0.03,
                   label="Fill rate to retailers, latest quarter", hint="orders.qty_shipped / orders.qty."),
            metric("inventory_value_at_pos_based_policy_cents", f["inv_value_pos_policy"], "cents",
                   f"Average weekly inventory value {P5}-{P8} when the same chain is re-simulated with the POS signal, p = 12 and no MOQ "
                   f"(actual policy: {f['inv_value_actual_p5_p8'] / 100:,.0f} USD).", pillar="python", tol_rel=0.15,
                   label="Inventory value under a POS-driven policy", hint="Counterfactual simulation."),
        ]
        dedupe = "SELECT * FROM orders QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY ingest_id) = 1" if p["difficulty"] == "principal" \
            else "SELECT * FROM orders"
        po_date = ("CAST(TRY_CAST(substr(CAST(order_date AS VARCHAR), 1, 19) AS TIMESTAMP) AS DATE)" if p["difficulty"] == "principal"
                   else "CAST(order_date AS DATE)")
        sql_amp = f"""
WITH o AS ({dedupe}),
grid AS (SELECT sku, date_trunc('week', sale_date) AS wk, SUM(qty) AS pos FROM pos_sales WHERE sale_date >= DATE '{s5}' GROUP BY 1, 2),
ro AS (SELECT sku, date_trunc('week', order_date) AS wk, SUM(qty) AS ro FROM o GROUP BY 1, 2),
po AS (SELECT sku, date_trunc('week', {po_date}) AS wk, SUM(qty) AS po FROM purchase_orders GROUP BY 1, 2),
w AS (
  SELECT g.sku, g.wk, g.pos, COALESCE(ro.ro, 0) AS ro, COALESCE(po.po, 0) AS po
  FROM grid g LEFT JOIN ro USING (sku, wk) LEFT JOIN po USING (sku, wk))
SELECT sku, var_samp(ro) / var_samp(pos) AS amp_t2, var_samp(po) / var_samp(pos) AS amp_t3,
       stddev_samp(pos) / AVG(pos) AS cv_pos, stddev_samp(ro) / AVG(ro) AS cv_ro, stddev_samp(po) / AVG(po) AS cv_po
FROM w GROUP BY 1 HAVING COUNT(*) > 40 ORDER BY amp_t3 DESC, sku"""
        q_amp = run_reference_query(con, "variance_amplification_by_sku", sql_amp, signature=["with", "left join", "group by", "having", "coalesce("],
                                    metric_keys=["amplification_tier2", "amplification_tier3", "cv_pos_demand", "cv_retailer_orders", "cv_company_po"],
                                    checks=lambda df: {"median_amp_t2": float(df["amp_t2"].median()), "median_amp_t3": float(df["amp_t3"].median()),
                                                       "median_cv_pos": float(df["cv_pos"].median())})
        sql_cfg = f"""
SELECT c.effective_from, c.demand_signal, c.forecast_window_weeks, c.moq_weeks_of_demand, c.safety_z,
       COUNT(p.po_id) AS purchase_orders, AVG(p.qty) AS avg_po_qty
FROM forecast_config c LEFT JOIN purchase_orders p
  ON {po_date.replace('order_date', 'p.order_date')} >= CAST(c.effective_from AS DATE)
 AND {po_date.replace('order_date', 'p.order_date')} < COALESCE((SELECT MIN(CAST(n.effective_from AS DATE)) FROM forecast_config n WHERE n.effective_from > c.effective_from), DATE '2999-12-31')
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1"""
        q_cfg = run_reference_query(con, "planning_parameters_and_po_lumpiness", sql_cfg, signature=["left join", "group by", "coalesce(", "count(", "avg("],
                                    metric_keys=["forecast_window_p", "moq_weeks_of_demand", "chen_lower_bound_tier2"],
                                    checks=lambda df: {"window_now": int(df.iloc[-1]["forecast_window_weeks"]), "moq_now": float(df.iloc[-1]["moq_weeks_of_demand"])})
        pw0 = state.extras["promo_w0"]
        wk = state.extras["weeks"]
        a, b = wk[max(0, pw0 - 8)].date(), wk[min(len(wk) - 1, pw0 + 12)].date()
        sql_promo = f"""
WITH o AS ({dedupe}),
r AS (SELECT date_trunc('week', order_date) AS wk, SUM(qty) AS retailer_orders FROM o GROUP BY 1),
s AS (SELECT date_trunc('week', sale_date) AS wk, SUM(qty) AS pos_units FROM pos_sales GROUP BY 1)
SELECT s.wk, s.pos_units, COALESCE(r.retailer_orders, 0) AS retailer_orders,
       SUM(s.pos_units) OVER (ORDER BY s.wk ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) AS pos_12wk
FROM s LEFT JOIN r USING (wk)
WHERE s.wk BETWEEN DATE '{a}' AND DATE '{b}' ORDER BY s.wk"""
        q_promo = run_reference_query(con, "promo_forward_buy_vs_pos", sql_promo, signature=["over(", "with", "left join", "group by", "coalesce("],
                                      metric_keys=["promo_forward_buy_multiple"],
                                      checks=lambda df: {"max_orders_to_pos": float((df["retailer_orders"] / df["pos_units"]).max())})
        sql_promo_m = """
SELECT period_of(order_date) AS period, SUM(qty) AS units_ordered, SUM(qty_shipped) AS units_shipped,
       SUM(qty_shipped) * 1.0 / SUM(qty) AS fill_rate, SUM(allowance_cents) AS promo_allowance_cents
FROM orders GROUP BY 1 ORDER BY 1"""
        q_fill = run_reference_query(con, "fill_rate_and_allowances_by_quarter", sql_promo_m, signature=["group by", "sum("],
                                     metric_keys=["fill_rate_p8"], checks=lambda df: {"min_fill": float(df["fill_rate"].min())})
        sql_sup = """
SELECT period_of(ship_date) AS period,
       AVG(CASE WHEN is_backorder = 0 THEN lead_time_days END) AS avg_lead_days_on_time,
       stddev_samp(CASE WHEN is_backorder = 0 THEN lead_time_days END) AS sd_lead_days,
       SUM(CASE WHEN is_backorder = 1 THEN qty ELSE 0 END) * 1.0 / SUM(qty) AS backorder_share,
       SUM(CASE WHEN expedited = 1 THEN freight_cents ELSE 0 END) AS air_freight_cents
FROM supplier_shipments GROUP BY 1 ORDER BY 1"""
        q_sup = run_reference_query(con, "supplier_lead_time_and_backorders", sql_sup, signature=["group by", "case when", "avg(", "sum("],
                                    metric_keys=["expedite_cost_cents_p7_p8"],
                                    checks=lambda df: {"lead_days_range": float(df["avg_lead_days_on_time"].max() - df["avg_lead_days_on_time"].min())})
        sql_gaming = f"""
WITH o AS ({dedupe}),
f AS (
  SELECT sku, date_trunc('week', order_date) AS wk, SUM(qty) AS ordered, SUM(qty_shipped) AS shipped FROM o GROUP BY 1, 2),
l AS (
  SELECT sku, wk, ordered, shipped,
         LAG(shipped * 1.0 / NULLIF(ordered, 0)) OVER (PARTITION BY sku ORDER BY wk) AS prev_fill FROM f)
SELECT CASE WHEN prev_fill < 0.9 THEN 'after_shortage' ELSE 'normal' END AS regime, COUNT(*) AS sku_weeks, AVG(ordered) AS avg_units_ordered
FROM l GROUP BY 1 ORDER BY 1"""
        q_gaming = run_reference_query(con, "orders_after_shortages", sql_gaming, signature=["over(", "partition by", "lag(", "with", "group by"],
                                       metric_keys=["fill_rate_p8"], checks=lambda df: {"regimes": len(df)})
        sql_cost = """
SELECT j.period,
       SUM(CASE WHEN j.source = 'expedite' THEN l.debit_cents ELSE 0 END) AS expedite_cents,
       SUM(CASE WHEN l.account_code = '5040' THEN l.debit_cents ELSE 0 END) AS writedown_cents,
       SUM(CASE WHEN l.account_code = '1220' THEN l.debit_cents - l.credit_cents ELSE 0 END) AS inventory_change_cents
FROM gl_lines l JOIN gl_journal j USING (entry_id) GROUP BY 1 ORDER BY 1"""
        q_cost = run_reference_query(con, "expedite_writedown_inventory_by_quarter", sql_cost, signature=["join", "group by", "case when", "sum("],
                                     metric_keys=["expedite_cost_cents_p7_p8", "writedown_cents_p8", "dio_p8_vs_p1_ratio"],
                                     checks=lambda df: {"writedown_last": float(df.iloc[-1]["writedown_cents"])})
        sql_cover = """
SELECT period_of(snapshot_date) AS period, MIN(weeks_of_cover) AS min_cover, MAX(weeks_of_cover) AS max_cover,
       SUM(on_hand_units * unit_cost_cents) * 1.0 / COUNT(DISTINCT snapshot_date) AS avg_inventory_cents
FROM inventory_snapshots GROUP BY 1 ORDER BY 1"""
        q_cover = run_reference_query(con, "weeks_of_cover_swings", sql_cover, signature=["group by", "distinct", "sum(", "count("],
                                      metric_keys=["inventory_value_at_pos_based_policy_cents", "dio_p8_vs_p1_ratio"],
                                      checks=lambda df: {"max_cover": float(df["max_cover"].max())})
        an = {x.id: x for x in state.anomalies}
        an["A1"] = an["A1"].model_copy(update={"reference_sql": [q_amp, q_cfg], "evidence_trail": [
            f"POS CV {f['cv_pos']:.2f} vs retailer orders {f['cv_ro']:.2f} vs our POs {f['cv_po']:.2f} (weekly, median SKU).",
            f"forecast_config: {pw}-week window since {P[1]}, MOQ {p['moq_weeks']:.1f} weeks since {P[3]}."]})
        an["A2"] = an["A2"].model_copy(update={"reference_sql": [q_promo, q_fill],
                                               "magnitude": {**an["A2"].magnitude, "promo_forward_buy_multiple": round(f["forward_buy_multiple"], 6),
                                                             "pos_12wk_change": round(f["pos_promo_window_change"], 6)},
                                               "evidence_trail": [f"Promo-weeks retailer orders x{f['forward_buy_multiple']:.1f}; 12-week POS change "
                                                                  f"{f['pos_promo_window_change']:+.1%}."]})
        an["A3"] = an["A3"].model_copy(update={"reference_sql": [q_sup, q_gaming], "evidence_trail": [
            "Supplier on-time lead time is stable quarter to quarter; backorder share rises with order spikes.",
            "After weeks with fill < 90% retailers order more (rationing game)."]})
        an["A4"] = an["A4"].model_copy(update={"reference_sql": [q_cost, q_cover],
                                               "magnitude": {**an["A4"].magnitude, "expedite_cost_cents_p7_p8": float(f["expedite_p7_p8"]),
                                                             "writedown_cents_p8": float(f["writedown_p8"]), "dio_p8_vs_p1_ratio": round(f["dio_ratio"], 6)},
                                               "evidence_trail": [f"DIO x{f['dio_ratio']:.2f} from {P[0]} to {P8}; air freight {f['expedite_p7_p8'] / 100:,.0f} USD "
                                                                  f"in {P7}-{P8}; write-down {f['writedown_p8'] / 100:,.0f} USD."]})
        herrings_all = [
            "The supplier is unreliable - its on-time lead time distribution is unchanged; it misses deliveries because our orders became spiky.",
            f"Consumer demand is volatile - POS CV is {f['cv_pos']:.2f}; the volatility is manufactured by ordering policies.",
            f"The promo generated demand - POS over the 12 promo weeks is {f['pos_promo_window_change']:+.1%} vs the surrounding weeks; retailers forward-bought and then stopped ordering.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        root = (f"Shelf demand is stable (POS CV {f['cv_pos']:.2f}) but every tier amplifies it: retailers' orders vary {f['t2']:.1f}x more than POS and "
                f"our purchase orders {f['t3']:.0f}x more. Our planning tool forecasts retailer orders (not the POS feed we receive) with a {pw}-week moving "
                f"average and rounds orders up to a {p['moq_weeks']:.1f}-week MOQ, so our ordering stage alone amplifies variance {f['stage']:.1f}x "
                f"(Chen bound for L={L + 1}, p={pw}: {f['chen']:.1f}). The {P6} promotion added a forward-buy spike and trough, and shortages triggered "
                f"order inflation. The supplier's lead time never changed; its capacity cannot absorb the spikes, so backorders are flown in "
                f"({f['expedite_p7_p8'] / 100:,.0f} USD air freight in {P7}-{P8}), inventory swings (DIO x{f['dio_ratio']:.2f}) and {P8} ends with a "
                f"{f['writedown_p8'] / 100:,.0f} USD write-down. A POS-driven policy with p=12 and no MOQ would hold ~"
                f"{f['inv_value_pos_policy'] / 100:,.0f} USD of inventory instead of {f['inv_value_actual_p5_p8'] / 100:,.0f} USD.")
        py = ("import numpy as np, pandas as pd\n"
              "from axiom.engine.operations import chen_bullwhip_bound\n"
              "w = pd.read_sql('''SELECT sku, date_trunc('week', sale_date) wk, SUM(qty) pos FROM pos_sales GROUP BY 1, 2''', con)\n"
              "# re-simulate: order-up-to with MA(p) on POS vs retailer orders, MOQ on/off; compare inventory, fill rate, var ratios\n"
              f"print(chen_bullwhip_bound({L + 1}, {pw}))\n")
        return GroundTruth(
            root_cause=root, anomalies=[an[k] for k in ("A1", "A2", "A3", "A4")], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q.sql for q in (q_amp, q_cfg, q_promo, q_fill, q_sup, q_gaming, q_cost, q_cover)], python=[py],
                memo_bullets=[
                    "Demand is not volatile; orders are. Quantify amplification by tier (POS -> retailer orders -> our POs).",
                    f"Use the POS feed as the demand signal and lengthen the forecast window (Chen: ratio ~ 1 + 2L/p + 2L^2/p^2; p={pw} -> {f['chen']:.1f}).",
                    "Shrink lot sizes / MOQ: the unit-price saving is dwarfed by air freight and write-downs.",
                    "Move to everyday-low-price or cap forward-buy; allocate on past sales to stop the rationing game.",
                    "Dual-sourcing adds cost and leaves the amplifier in place.",
                ]),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal/conceal, four stakeholders, spec timeline (red herrings flagged) and the flow diagram."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        names = rng.pick_names(4)
        reveal = [
            "The supplier complains about 'impossible' order swings; deliveries were missed in recent quarters.",
            f"Air-freight bills reached {f['expedite_p7_p8'] / 100:,.0f} USD over {P[6]}-{P[7]}.",
            f"An inventory write-down of {f['writedown_p8'] / 100:,.0f} USD was booked in {P[7]}.",
            "Management says 'demand has become volatile'.",
            f"A -15% price promotion ran in {P[5]}.",
            f"Fill rate to retailers dipped to {min(f['fill_by_quarter'].values()):.0%} in the worst quarter.",
        ]
        conceal = ["POS stability (POS CV vs order CV)", f"the forecast window (p = {p['forecast_window_p']} weeks)",
                   "the MOQ rationale and its batching cost", "the amplification ratios by tier", "A1", "A2", "A3", "A4"]
        stakeholders = [
            Stakeholder(name=names[0], role="VP Supply Chain", agenda="Dual-source the 'unreliable' supplier.", disposition="defensive",
                        knows=["the planning tool runs a short forecast window"], hides=["switched to the 2-week 'responsive' window"]),
            Stakeholder(name=names[1], role="Head of Sales", agenda="Repeat the promotion.", disposition="proud",
                        knows=["retailers loaded up during the promo"], hides=[]),
            Stakeholder(name=names[2], role="Supplier account manager", agenda="Keep the account.", disposition="frustrated",
                        knows=["our orders are spiky; their lead time has not changed"], hides=[]),
            Stakeholder(name=names[3], role="CFO", agenda="Explain DIO and the write-down to the board.", disposition="impatient",
                        knows=["inventory and air freight are up"], hides=[]),
        ]
        tl = [
            TimelineEvent(period=P[1], headline="Forecast tool upgraded to a 'responsive' short window", detail="Planning moves to a short moving average."),
            TimelineEvent(period=P[3], headline="New MOQ negotiated for a lower unit price", detail="Supplier contract renewed with minimum order quantities."),
            TimelineEvent(period=P[5], headline="Promotion: -15% for four weeks", detail="Retail chains load up."),
            TimelineEvent(period=P[6], headline="Air freight approved", detail="Expedites authorised to protect fill rate."),
            TimelineEvent(period=P[7], headline="Inventory write-down; dual-sourcing RFP issued", detail="Board asks for an inventory plan."),
            TimelineEvent(period=P[5], headline="Supplier misses deliveries", detail="Backorders on several SKUs.", is_red_herring=True),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[6], headline="Category manager: 'consumers are unpredictable now'", detail="Quote in the sales review.",
                                    is_red_herring=True))
        tl.sort(key=lambda e: e.period)
        mermaid = "\n".join([
            "flowchart RL",
            '  C["Consumers (POS demand)"] -->|"sell-through"| R["Retail chains (MA-4 order-up-to)"]',
            '  R -->|"retailer orders"| D["Our DC (MA-p forecast of retailer orders, MOQ)"]',
            '  D -->|"purchase orders"| S["Supplier (weekly capacity, L weeks)"]',
            *(['  S -->|"production orders"| F["Factory"]'] if p["factory_tier"] else []),
            '  R -. "weekly POS feed (unused)" .-> D',
            '  S -->|"truck / air (backorders)"| D',
            '  D -->|"shipments"| R',
        ])
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["forecast_tool_config_note", "moq_contract_excerpt", "freight_invoice_summary", "retailer_order_chart", "supply_chain_flow_diagram"],
                           architecture_mermaid=mermaid)

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """Five levers; defaults are the P8 status quo."""
        p = state.params
        return [
            Lever(key="forecast_window_p", label="Forecast window (weeks)", min=2, max=16, default=float(p["forecast_window_p"]), unit="weeks",
                  effect="Longer windows damp amplification (Chen) at the cost of responsiveness."),
            Lever(key="demand_signal_pos", label="Forecast on POS instead of retailer orders (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Information sharing removes tier-1 amplification from our signal."),
            Lever(key="moq_weeks", label="MOQ / lot size (weeks of demand)", min=0.5, max=4, default=round(p["moq_weeks"], 2), unit="weeks",
                  effect="Smaller lots cut batching amplification; unit cost +1-3% below 2 weeks."),
            Lever(key="promo_policy_edlp", label="Everyday low price instead of promotions (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Removes the forward-buy spike and trough."),
            Lever(key="dual_source", label="Dual-source the supplier (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Lead-time variability down, unit cost +5%; does not fix the amplification."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model (module docstring)."""
        p, E = state.params, state.extras
        f = self._facts(state)
        P8 = state.periods[-1]
        k = E["ops_kpis"][P8]
        L = p["lead_weeks"]
        pw = p["forecast_window_p"]
        inc8 = next(i for i in state.statements.income if i.period == P8)  # type: ignore[union-attr]
        cogs8 = float(inc8.revenue_cents - inc8.gross_profit_cents)
        kpis = {"amplification_tier2": f["t2"], "amplification_tier3": f["t3"], "inventory_cents": k["inventory_cents"], "fill_rate": f["fill_p8"],
                "expedite_cost_cents": k["expedite_cost_cents"], "dio_days": k["dio_days"], "cogs_cents": cogs8, "revenue_cents": float(inc8.revenue_cents),
                "writedown_risk_cents": float(f["writedown_p8"])}
        chen_now, chen_12 = chen_bullwhip_bound(L + 1, pw), chen_bullwhip_bound(L + 1, 12)
        per_week = (chen_12 - chen_now) / (12 - pw)  # Chen bound slope, linearised from p to 12
        rel = per_week / chen_now  # relative change of the company-stage amplification per week of window
        inv_gap = max(0.0, k["inventory_cents"] - f["inv_value_pos_policy"])
        effects = [
            {"lever": "forecast_window_p", "kpi": "amplification_tier3", "delta_per_unit": f["t3"] * rel, "lag_periods": 0},
            {"lever": "forecast_window_p", "kpi": "inventory_cents", "delta_per_unit": -0.5 * inv_gap / (12 - pw), "lag_periods": 1},
            {"lever": "forecast_window_p", "kpi": "expedite_cost_cents", "delta_per_unit": -0.6 * k["expedite_cost_cents"] / (12 - pw), "lag_periods": 1},
            {"lever": "forecast_window_p", "kpi": "writedown_risk_cents", "delta_per_unit": -0.5 * f["writedown_p8"] / (12 - pw), "lag_periods": 1},
            {"lever": "demand_signal_pos", "kpi": "amplification_tier2", "delta_per_unit": -(f["t2"] - 1.0), "lag_periods": 1},
            {"lever": "demand_signal_pos", "kpi": "amplification_tier3", "delta_per_unit": -0.5 * f["t3"], "lag_periods": 1},
            {"lever": "demand_signal_pos", "kpi": "inventory_cents", "delta_per_unit": -0.5 * inv_gap, "lag_periods": 1},
            {"lever": "demand_signal_pos", "kpi": "fill_rate", "delta_per_unit": 0.5 * (f["fill_pos_policy"] - f["fill_p8"]), "lag_periods": 1},
            {"lever": "moq_weeks", "kpi": "amplification_tier3", "delta_per_unit": 0.25 * f["t3"], "lag_periods": 0},
            {"lever": "moq_weeks", "kpi": "inventory_cents", "delta_per_unit": 0.5 * cogs8 / 13, "lag_periods": 1},
            {"lever": "moq_weeks", "kpi": "cogs_cents", "delta_per_unit": -0.02 * cogs8, "lag_periods": 0},
            {"lever": "promo_policy_edlp", "kpi": "amplification_tier2", "delta_per_unit": -0.4 * (f["t2"] - 1.0), "lag_periods": 0},
            {"lever": "promo_policy_edlp", "kpi": "revenue_cents", "delta_per_unit": -0.01 * float(inc8.revenue_cents), "lag_periods": 0},
            {"lever": "dual_source", "kpi": "cogs_cents", "delta_per_unit": 0.05 * cogs8, "lag_periods": 0},
            {"lever": "dual_source", "kpi": "fill_rate", "delta_per_unit": 0.01, "lag_periods": 1},
        ]
        n = 2
        P = state.periods
        kq = E["ops_kpis"]
        drag = {"inventory_cents": (kq[P8]["inventory_cents"] - kq[P[5]]["inventory_cents"]) / n,
                "expedite_cost_cents": (kq[P8]["expedite_cost_cents"] - kq[P[5]]["expedite_cost_cents"]) / n,
                "dio_days": (kq[P8]["dio_days"] - kq[P[5]]["dio_days"]) / n}
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag,
                "resolved_if": {"demand_signal_pos": 1.0, "forecast_window_p": 8.0},
                "bounds": {"amplification_tier2": [1.0, None], "amplification_tier3": [1.0, None], "fill_rate": [0.0, 1.0], "inventory_cents": [0.0, None],
                           "expedite_cost_cents": [0.0, None], "writedown_risk_cents": [0.0, None]},
                "max_quarters": 4}
