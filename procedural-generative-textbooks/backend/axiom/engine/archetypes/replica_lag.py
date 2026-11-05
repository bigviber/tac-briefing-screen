"""``replica_lag`` - "Read Your Writes" (archetypes.md section 11).

A B2B flash-sale marketplace sells wholesale lots (cases / pallets of overstock) to resellers. Read
replicas were added in P4 "to scale reads"; the checkout service checks ``available_qty`` on the
**replica** and then reserves stock on the primary with an unconditional decrement. Under flash-sale
write bursts the asynchronous replica falls seconds behind, so buyers who arrive just after a lot
sells out still see stock and are sold units that do not exist.

Model
-----
* Normal catalogue SKUs are replenished monthly (order-up-to on available stock) and never sell out;
  their orders read the replica at base lag ``LN(ln 150 ms, 0.4)``. Limited lots exist only in
  flash sales (3-5 lots per sale, P5-P8) and weekly restock drops (one lot, P5-P8).
* Each limited lot is simulated attempt by attempt: Poisson arrivals, ``seen = available on the
  primary at (t - lag(t) - cache_age)``; an order is placed when ``seen >= 1`` and decrements the
  primary unconditionally, so ``available`` goes negative (``inventory_events.qty_after < 0``). The
  lag process is a deterministic function of time: base noise, flash-sale load (ramp to
  ``lag_under_load_s``, per-sale level grows with sale size), short replication hiccups during some
  drops, and (principal) a nightly analytics job on the replica from P7 (``U(60, 300)`` s) plus an
  availability cache with TTL ``U(30, 120)`` s on top of the replica. Arrival rates are calibrated so
  each lot sells out after ``staleness / oversell_share`` seconds, which makes the realised oversell
  share track the sampled parameter. Principal drops run at 02:00 UTC for APAC buyers - inside the
  analytics job - hence a steady night-time oversell on international orders.
* Consequences: ``backorder_cancel_rate`` of oversold orders are cancelled
  (``reason='out_of_stock_after_order'``) and refunded 1-5 days later - the card fee is not returned
  (5050 leakage); the rest are fulfilled late from an expedited purchase (expedite freight in 5030).
  Oversold buyers open tickets; churn uplift is costed with the ``kpi_assumptions`` table.
* GL: card checkout ``Dr 1000 net (Dr 1100 for checkouts of a month's last two days, settled T+2), Dr 5050 fee; Cr 4000`` (weekly batches); shipment ``Dr 5000;
  Cr 1220`` and freight ``Dr 5030; Cr 2100``; receipts ``Dr 1220; Cr 2000`` paid after 30 days;
  cancellations ``Dr 4100; Cr 2450`` then refunds ``Dr 2450; Cr 1000``.
* Ties: physical inventory (receipts - shipments) == 1220, open purchases == 2000, cancellations
  awaiting refund == 2450 all hold. **Designed break** ``orders_gmv_vs_net_revenue``: OMS order value
  placed in the quarter vs GL net revenue (4000 + 4100) differs by exactly the cancellations booked in
  the quarter.

Simulation (reduced form, P8 quarter levels): ``inventory_check_path`` (0 replica / 1 primary read /
2 conditional ``UPDATE ... WHERE available >= qty``) removes 85% / 100% of oversells;
``isolate_olap`` removes night-time oversells and nightly lag; ``cache_ttl_s`` scales cache
staleness; ``rate_limit_checkout_rps`` trims flash lag and conversions; ``upgrade_replica`` costs money
and trims p99 lag by 1/1.3 but leaves the race; ``flash_sale_size_multiplier`` scales revenue and
oversells. ``resolved_if``: conditional update (>= 2) and OLAP isolation.
"""

from __future__ import annotations

import bisect
import datetime as dt
import logging
import math
from typing import Any

import duckdb
import numpy as np
import pandas as pd

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

logger = logging.getLogger("axiom.archetypes.replica_lag")

FEE_RATE = 0.029
FEE_FIXED = 30
CATEGORIES = ("Apparel lots", "Electronics returns", "Home goods", "Toys", "Beauty", "Footwear", "Kitchen", "Sporting goods")
REGIONS_US = ("Northeast", "Southeast", "Midwest", "Southwest", "West")
INTL = ("SG", "AU", "JP", "KR", "HK")
FLAWED_MERMAID = """flowchart LR
  B[Buyer] --> CDN["Availability cache TTL {ttl}"]
  CDN --> U[Checkout Service]
  U -->|"1. read available_qty"| RR[(Read Replica, async)]
  U -->|"2. INSERT order; UPDATE inventory SET reserved = reserved + qty (no condition)"| PR[(Primary)]
  PR -. "async replication (150 ms; seconds under flash load)" .-> RR
  AN["Nightly Analytics Job{olap}"] -->|"heavy OLAP scans"| RR
  classDef bad fill:#3b1d1d,stroke:#ff5555,color:#fff
  class U,AN bad"""
FIXED_MERMAID = """flowchart LR
  B[Buyer] --> U[Checkout Service]
  U -->|"UPDATE inventory SET reserved = reserved + :qty WHERE sku = :sku AND on_hand - reserved >= :qty RETURNING *"| PR[(Primary)]
  U -->|"0 rows -> sold out (no order)"| B
  PR -. async .-> RR[(Read Replica: browse pages only)]
  CAT[Catalogue / browse] --> RR
  PR -. CDC .-> WH[(Analytics warehouse)]
  AN[Nightly Analytics Job] --> WH
  LB["Checkout rate limiter / queue during flash sales"] --> U"""


def _secs(ts: pd.Series | pd.DatetimeIndex | np.ndarray, epoch: pd.Timestamp) -> np.ndarray:
    return (pd.DatetimeIndex(pd.to_datetime(np.asarray(ts))) - epoch).total_seconds().to_numpy()


def _utc(col: str, dirty: bool) -> str:
    """SQL expression normalising a timestamp column that may carry mixed UTC offsets (tz_mixed dirt)."""
    return f"(CAST({col} AS TIMESTAMPTZ) AT TIME ZONE 'UTC')" if dirty else col


class _LagModel:
    """Deterministic replica lag (seconds) as a function of time: base noise + windowed load terms."""

    def __init__(self, base_s: float, g: np.random.Generator) -> None:
        self.base_s = base_s
        self.g = g
        self.windows: list[tuple[float, float, str, float, np.ndarray, float]] = []

    def add(self, t0: float, t1: float, kind: str, level_s: float, ramp_s: float, sigma: float) -> None:
        n = int((t1 - t0) // 10) + 2
        self.windows.append((t0, t1, kind, level_s, self.g.lognormal(-sigma ** 2 / 2, sigma, n), ramp_s))

    def lag(self, t: np.ndarray) -> np.ndarray:
        t = np.asarray(t, dtype=float)
        out = self.base_s * self.g.lognormal(0, 0.4, len(t))
        for t0, t1, _kind, level, noise, ramp in self.windows:
            m = (t >= t0) & (t < t1)
            if m.any():
                x = t[m] - t0
                out[m] += level * np.minimum(1.0, (x + 1.0) / ramp) * noise[(x // 10).astype(int)]
        return out


@register
class ReplicaLag(Archetype):
    """Stale availability reads on an async replica oversell limited stock."""

    key = "replica_lag"
    title = "Read Your Writes"
    pillars = ("system_design", "operations_strategy", "accounting_finance", "sql")
    industries = ("ecommerce", "retail", "3pl")
    schema_template = "ecommerce_oms"
    summary = ("Flash sales are a hit, but a growing share of orders is cancelled for stock that was never there; "
               "the warehouse is blamed, refunds and expedite costs pile up.")
    root_cause_terms = ("read replica", "stale read", "replication lag", "conditional update")
    stage_options = ("seed", "growth")  # the narrative is a growth story

    # ------------------------------------------------------------------ params

    def sample_params(self, rng: SeededRNG, pool: BenchmarkPool, difficulty: Difficulty, industry: str | None = None) -> dict[str, Any]:
        """Parameter table of archetypes.md section 11."""
        ind = industry or "ecommerce"
        if ind not in self.industries:
            raise ValueError(f"{self.key} supports industries {self.industries}, got {ind!r}")
        principal = difficulty == "principal"
        rev_usd = math.exp(rng.uniform(math.log(5e6), math.log(14e6)))
        p: dict[str, Any] = {
            "industry": ind,
            "business_model": {"ecommerce": "B2B flash-sale marketplace for wholesale overstock lots",
                               "retail": "off-price wholesale arm of a retailer selling overstock lots to resellers",
                               "3pl": "liquidation marketplace run by a logistics group, selling returned and overstock lots"}[ind],
            "revenue_ttm_cents": int(rev_usd * 100),
            "growth_qoq": float(rng.uniform(0.03, 0.08)),
            "gm": float(rng.uniform(0.24, 0.36)),
            "freight_pct": float(rng.uniform(0.035, 0.06)),
            "op_margin": float(rng.uniform(0.03, 0.08)),
            "tax_rate": float(rng.uniform(0.18, 0.26)),
            "capex_pct": float(rng.uniform(0.008, 0.02)),
            "replica_lag_base_ms": float(math.exp(rng.normal(math.log(150), 0.4))),
            "lag_under_load_s": magnitude(rng, difficulty, 3.0, 45.0),
            "oversell_share": magnitude(rng, difficulty, 0.005, 0.03),
            "nightly_job": principal,
            "nightly_lag_s": float(rng.uniform(60, 300)) if principal else 0.0,
            "cache_ttl_s": float(rng.uniform(30, 120)) if principal else 0.0,
            "backorder_cancel_rate": float(rng.uniform(0.5, 0.8)),
            "customer_cancel_rate": float(rng.uniform(0.008, 0.02)),
            "ticket_rate_oversold": float(rng.uniform(0.55, 0.85)),
            "ticket_rate_base": float(rng.uniform(0.01, 0.025)),
            "support_cost_cents": int(rng.integers(1800, 4500)),
            "expedite_cost_per_case_cents": int(rng.integers(2500, 9000)),
            "churn_uplift": float(rng.uniform(1.5, 2.5)),
            "base_churn_12m": float(rng.uniform(0.25, 0.4)),
            "hiccup_prob": float(rng.uniform(0.2, 0.35)),
            "alert_threshold_s": 60,
            "n_red_herrings": difficulty_pick(difficulty, 1, 2, 3),
            "difficulty": difficulty,
        }
        p["n_skus"] = difficulty_pick(difficulty, 30, 50, 80)
        p["flash_periods"] = difficulty_pick(difficulty, [5, 7, 8], [5, 6, 7, 8, 8], [5, 6, 7, 7, 8, 8])
        p["lot_stock"] = difficulty_pick(difficulty, (12, 30), (18, 45), (24, 60))
        # telemetry sampling (downsampled outside events) and drop cadence scale with the row budget
        p["flash_sample_s"] = difficulty_pick(difficulty, 30.0, 10.0, 10.0)
        p["drop_sample_s"] = difficulty_pick(difficulty, 300.0, 180.0, 180.0)
        p["base_sample_h"] = difficulty_pick(difficulty, 24.0, 12.0, 12.0)
        p["drop_every_days"] = difficulty_pick(difficulty, 14, 7, 7)
        budget = ROW_BUDGET[difficulty]
        # fixed rows: telemetry + GL + purchase orders + limited-lot orders (4 rows each) + small tables
        fixed = difficulty_pick(difficulty, 6200, 10500, 30500)
        p["n_orders"] = int(max(600, (budget * 0.9 - fixed) / 4.4))
        p["description"] = (f"{cap_first(p['business_model'])}; weekly limited drops and flash sales drive growth. "
                            "Read replicas were added last year to keep the site up under flash-sale traffic.")
        return p

    # ------------------------------------------------------------------ baseline

    def build_baseline(self, ctx: BuildContext, state: WorldState) -> None:
        """Catalogue, customers, normal orders, monthly replenishment and the flash / drop calendar."""
        self.enable_standard_working_capital(ctx, state, vendor_ap=False)  # base working-capital defaults: prepaid expenses (AP already comes from the purchasing / cost sub-ledger)
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("baseline")
        g = rng.generator
        months = cal.month_starts()
        n_m = len(months)
        epoch = pd.Timestamp(cal.start)
        state.extras.update({"months": months, "epoch": epoch})
        S = p["n_skus"]
        price_usd = np.clip(g.lognormal(math.log(420), 0.6, S), 60, 4000)
        gm_sku = np.clip(g.normal(p["gm"], 0.05, S), 0.1, 0.6)
        pop = 1.0 + g.pareto(1.3, S)
        pop /= pop.sum()
        # --- customers (resellers); 15% international
        C = max(200, int(p["n_orders"] * 0.35))
        cw = 1.0 + g.pareto(1.5, C)
        intl = g.random(C) < 0.15
        country = np.where(intl, np.array(INTL)[g.integers(0, len(INTL), C)], "US")
        customers = pd.DataFrame({
            "customer_id": [f"RS{i:05d}" for i in range(1, C + 1)],
            "email": [f"buyer{i}@reseller{i % 97}.example.com" for i in range(1, C + 1)],
            "country": country,
            "region": np.where(intl, "APAC", np.array(REGIONS_US)[g.integers(0, len(REGIONS_US), C)]),
            "acquisition_channel": np.array(("marketplace_ads", "referral", "trade_show", "organic"))[g.integers(0, 4, C)],
            "created_at": epoch - pd.to_timedelta(g.integers(0, 400, C), unit="D") + pd.to_timedelta(g.integers(0, n_m * 30, C) * (g.random(C) < 0.5), unit="D"),
        })
        # --- normal orders (US business hours mostly), 1-2 lines, 1-4 cases
        N = p["n_orders"]
        trend = (1 + p["growth_qoq"]) ** (np.arange(n_m) / 3.0) * g.lognormal(0, 0.05, n_m)
        counts = np.floor(N * trend / trend.sum()).astype(int)
        counts[-1] += N - counts.sum()
        midx = np.repeat(np.arange(n_m), counts)
        mstart = pd.to_datetime(pd.Series(months))
        days_in = mstart.dt.days_in_month.to_numpy()
        hour = np.clip(g.normal(18.5, 3.0, N), 12, 23.99)  # UTC; US daytime
        placed = (mstart.to_numpy()[midx] + pd.to_timedelta((g.random(N) * days_in[midx]).astype(int), unit="D").to_numpy()
                  + pd.to_timedelta(np.round(hour * 3600), unit="s").to_numpy())
        placed = np.sort(placed)
        cust = g.choice(C, size=N, p=cw / cw.sum())
        k = 1 + (g.random(N) < 0.15).astype(int)
        oi = np.repeat(np.arange(N), k)
        sku = g.choice(S, size=len(oi), p=pop)
        qty = 1 + np.minimum(g.poisson(0.8, len(oi)), 3)
        # price scale so TTM revenue ~ target (flash revenue on top is small)
        last12 = (midx >= n_m - 12)[oi]
        raw = float((qty * price_usd[sku])[last12].sum())
        scale = p["revenue_ttm_cents"] * 0.85 / 100 / raw if raw else 1.0
        price_cents = np.maximum(1000, np.round(price_usd * scale * 100 / 100) * 100).astype(np.int64)
        cost_cents = np.round(price_cents * (1 - gm_sku)).astype(np.int64)
        products = pd.DataFrame({
            "product_id": [f"P{i:04d}" for i in range(1, S + 1)],
            "sku": [f"LOT-{1000 + i}" for i in range(1, S + 1)],
            "title": [f"{CATEGORIES[i % len(CATEGORIES)]} case pack {i + 1}" for i in range(S)],
            "category": np.array(CATEGORIES)[np.arange(S) % len(CATEGORIES)],
            "unit_cost_cents": cost_cents, "list_price_cents": price_cents,
            "is_limited": False, "active": True,
            "introduced_at": epoch - pd.to_timedelta(g.integers(30, 900, S), unit="D"),
        })
        normal = pd.DataFrame({"_order": oi, "placed_at": pd.to_datetime(placed[oi]), "_cust": cust[oi], "_sku": sku, "quantity": qty.astype(np.int64),
                               "kind": "regular"})
        state.extras.update({"normal_lines": normal, "cust_weight": cw, "intl": intl, "n_normal_orders": N})
        state.frames.update({"customers": customers, "products": products})
        # --- flash sale calendar (P5-P8) and weekly drops (P5-P8)
        sales = []
        sizes = np.linspace(0.55, 1.0, len(p["flash_periods"]))
        for i, pi in enumerate(p["flash_periods"]):
            s, e = cal.bounds(state.p(pi))
            day = pd.Timestamp(s) + pd.Timedelta(days=int(g.integers(4, (e - s).days - 6)))
            start = day + pd.Timedelta(hours=int(g.integers(16, 19)), minutes=int(g.choice([0, 30])))
            sales.append({"flash_sale_id": f"FS-{i + 1:02d}", "name": f"{['Spring', 'Summer', 'Fall', 'Holiday', 'Clearance', 'Mega'][i % 6]} Lot Drop {i + 1}",
                          "starts_at": start, "ends_at": start + pd.Timedelta(minutes=int(g.integers(60, 121))),
                          "n_lots": int(g.integers(3, 6)), "size": float(sizes[i]), "period": state.p(pi)})
        state.extras["flash_sales"] = sales
        drops = []
        d0 = pd.Timestamp(cal.bounds(state.p(5))[0])
        d_end = pd.Timestamp(cal.end) - pd.Timedelta(days=3)
        p7 = pd.Timestamp(cal.bounds(state.p(7))[0])
        t = d0 + pd.Timedelta(days=int(g.integers(1, 6)))
        while t < d_end:
            apac = p["nightly_job"] and t >= p7
            hh = (2, int(g.integers(0, 30))) if apac else (int(g.integers(14, 20)), int(g.choice([0, 15, 30, 45])))
            st = t.normalize() + pd.Timedelta(hours=hh[0], minutes=hh[1])
            if not any(abs((st - f["starts_at"]).total_seconds()) < 86400 for f in sales):
                drops.append({"start": st, "apac": apac})
            t += pd.Timedelta(days=p["drop_every_days"])
        state.extras["drops"] = drops

    # ------------------------------------------------------------------ anomaly: limited lots under replica lag

    def inject_anomalies(self, ctx: BuildContext, state: WorldState) -> list[Anomaly]:
        """Lag process, batch jobs, and the attempt-level simulation of every limited lot."""
        p, cal = state.params, state.calendar
        rng = ctx.rng.spawn("anomaly")
        g = rng.generator
        epoch = state.extras["epoch"]
        lag = _LagModel(p["replica_lag_base_ms"] / 1000.0, g)
        jobs = []
        if p["nightly_job"]:
            n0 = pd.Timestamp(cal.bounds(state.p(7))[0]) + pd.Timedelta(days=int(g.integers(3, 12)))
            for day in pd.date_range(n0, pd.Timestamp(cal.end), freq="D"):
                st = day + pd.Timedelta(hours=1, minutes=int(g.integers(25, 50)))
                dur = int(g.integers(70, 160))
                lvl = p["nightly_lag_s"] * float(g.lognormal(0, 0.2))
                jobs.append({"job_name": "nightly_dashboard_refresh", "started_at": st, "finished_at": st + pd.Timedelta(minutes=dur), "_lvl": lvl})
                t0 = (st - epoch).total_seconds()
                lag.add(t0, t0 + dur * 60, "batch", lvl, 300.0, 0.25)
        state.extras["batch_jobs"] = jobs
        products = state.frames["products"]
        lots, lines = [], []
        sku_n = len(products)
        ttl = p["cache_ttl_s"]
        price_ref = float(products["list_price_cents"].median())
        lo, hi = p["lot_stock"]

        def simulate(sku_idx: int, t_open: float, t_close: float, stock: int, kind: str, ref: str, *, target: float | None = None,
                     sellout: float | None = None) -> None:
            """Attempt-level simulation of one lot. Flash lots: the arrival rate is calibrated so the lot sells out after
            ``replica staleness / target`` seconds (capped so it sells out inside the sale); an availability cache (principal)
            adds staleness on top, i.e. extra oversells. Drops sell out after ``sellout``."""
            if sellout is None:
                probe = np.linspace(t_open + 60, min(t_close, t_open + 900), 12)
                stale = float(np.mean(lag.lag(probe)))
                sellout = float(np.clip(stale / max(1e-4, float(target)), 120.0, 0.6 * (t_close - t_open)))
            lam = stock / sellout
            n_try = int(g.poisson(lam * (t_close - t_open)))
            times = np.sort(t_open + g.random(n_try) * (t_close - t_open))
            lags = lag.lag(times)
            ages = g.uniform(0, ttl, n_try) if ttl else np.zeros(n_try)
            hist_t, hist_v = [t_open - 3600.0], [stock]
            avail = stock
            for t, lg, age in zip(times.tolist(), lags.tolist(), ages.tolist(), strict=True):
                i = bisect.bisect_right(hist_t, t - lg - age) - 1
                seen = hist_v[i] if i >= 0 else 0
                if seen >= 1:  # checkout saw stock: reserve unconditionally on the primary
                    avail -= 1
                    hist_t.append(t)
                    hist_v.append(avail)
                    lines.append((t, sku_idx, 1, seen, avail, lg, age, kind, ref))

        for f in state.extras["flash_sales"]:
            t0 = (f["starts_at"] - epoch).total_seconds()
            t1 = (f["ends_at"] - epoch).total_seconds()
            lag.add(t0, t1, "flash", p["lag_under_load_s"] * f["size"], 180.0, 0.3)
            for _ in range(f["n_lots"]):
                stock = int(g.integers(lo, hi + 1))
                lots.append({"_sku": sku_n, "stock": stock, "kind": "flash", "ref": f["flash_sale_id"], "receipt_at": f["starts_at"] - pd.Timedelta(hours=20),
                             "price": int(round(price_ref * float(g.uniform(0.5, 0.9)) / 100) * 100), "intl": False})
                simulate(sku_n, t0, t1, stock, "flash", f["flash_sale_id"], target=p["oversell_share"])
                sku_n += 1
        for j, d in enumerate(state.extras["drops"]):
            t0 = (d["start"] - epoch).total_seconds()
            t1 = t0 + 45 * 60
            # drops are thin markets: they sell out over 10-25 minutes (APAC nights: 25-45) whatever the lag
            sellout = float(g.uniform(1500, 2700) if d["apac"] else g.uniform(600, 1500))
            if not d["apac"] and g.random() < p["hiccup_prob"]:
                # the sell-out rush is a write burst: autovacuum / checkpoint hiccups on the replica coincide with it
                h0 = t0 + sellout * float(g.uniform(0.75, 0.95))
                lag.add(h0, h0 + float(g.uniform(300, 600)), "hiccup", float(g.uniform(2, 8)), 30.0, 0.3)
            stock = int(g.integers(max(6, lo // 3), max(10, hi // 3) + 1))
            ref = f"DROP-{j + 1:03d}"
            lots.append({"_sku": sku_n, "stock": stock, "kind": "drop", "ref": ref, "receipt_at": d["start"] - pd.Timedelta(hours=12),
                         "price": int(round(price_ref * float(g.uniform(0.6, 1.0)) / 100) * 100), "intl": d["apac"]})
            simulate(sku_n, t0, t1, stock, "drop", ref, sellout=sellout)
            sku_n += 1
        state.extras["lag_model"] = lag
        lot_df = pd.DataFrame(lots)
        ll = pd.DataFrame(lines, columns=["_t", "_sku", "quantity", "seen", "qty_after", "_lag", "_age", "kind", "ref"])
        # checkout commits are logged at whole seconds; keep each lot's reservations strictly ordered
        ll = ll.sort_values(["_sku", "_t"], kind="stable").reset_index(drop=True)
        t_int = np.round(ll["_t"].to_numpy())
        k = ll.groupby("_sku").cumcount().to_numpy()
        bump = pd.Series(t_int - k).groupby(ll["_sku"]).cummax().to_numpy() + k
        ll["placed_at"] = epoch + pd.to_timedelta(bump, unit="s")
        ll["_oversold"] = ll["qty_after"] < 0
        state.extras.update({"lots": lot_df, "lot_lines": ll})
        n_flash = int((ll["kind"] == "flash").sum())
        n_os_flash = int(((ll["kind"] == "flash") & ll["_oversold"]).sum())
        win = [state.p(i) for i in sorted(set(p["flash_periods"]))]
        anomalies = [
            Anomaly(id="A1", kind="oversell_stale_replica_read", pillar="system_design", title="Oversells from stale availability reads on the replica",
                    mechanism=(f"Checkout reads available_qty from an async read replica, then reserves on the primary with an unconditional "
                               f"decrement. Flash-sale write bursts push replica lag to ~{p['lag_under_load_s']:.0f}s, so buyers keep seeing "
                               f"stock after a lot sells out: {n_os_flash} of {n_flash} flash-sale orders oversold."
                               + (f" An availability cache (TTL {ttl:.0f}s) adds staleness on top." if ttl else "")),
                    periods=win, affected_accounts=["4000", "4100", "2450"], affected_tables=["checkout_reads", "inventory_events", "orders", "replica_lag_samples"],
                    magnitude={"lag_under_load_s": round(p["lag_under_load_s"], 4), "oversell_share_target": round(p["oversell_share"], 6)},
                    severity="high"),
            Anomaly(id="A2", kind="oversell_cost", pillar="accounting_finance", title="Cancellations, refunds, fee leakage and expedites",
                    mechanism=(f"{p['backorder_cancel_rate']:.0%} of oversold orders are cancelled and refunded (card fee not returned); "
                               "the rest ship late from expedited purchases; buyers open tickets and churn faster."),
                    periods=win, affected_accounts=["4100", "2450", "5050", "5030", "6200"], affected_tables=["cancellations", "refunds", "support_tickets", "purchase_orders"],
                    magnitude={"backorder_cancel_rate": round(p["backorder_cancel_rate"], 4)}, severity="high"),
        ]
        if p["nightly_job"]:
            anomalies.append(Anomaly(
                id="A3", kind="olap_on_oltp_replica", pillar="system_design", title="Nightly analytics job on the checkout replica",
                mechanism=(f"From {state.p(7)} the analytics team runs nightly_dashboard_refresh on the same replica (~{p['nightly_lag_s']:.0f}s lag "
                           "while it runs); APAC restock drops at 02:00 UTC fall inside it, so international night orders oversell steadily."),
                periods=state.periods[-2:], affected_accounts=["4100"], affected_tables=["batch_jobs", "replica_lag_samples", "orders"],
                magnitude={"nightly_lag_s": round(p["nightly_lag_s"], 3), "cache_ttl_s": round(ttl, 3)}, severity="medium"))
        return anomalies

    # ------------------------------------------------------------------ derived frames + ledger

    def post_ledger(self, ctx: BuildContext, state: WorldState) -> None:
        """Orders, inventory, reads, cancellations, refunds, expedites, tickets, samples; then the GL."""
        p, cal = state.params, state.calendar
        g = ctx.rng.spawn("derive").generator
        epoch = state.extras["epoch"]
        end_ts = pd.Timestamp(cal.end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        products = state.frames["products"]
        customers = state.frames["customers"]
        lots, ll = state.extras["lots"], state.extras["lot_lines"]
        lag: _LagModel = state.extras["lag_model"]
        S = len(products)
        # --- limited lots become catalogue rows
        if len(lots):
            lp = pd.DataFrame({
                "product_id": [f"P{i + 1:04d}" for i in lots["_sku"]],
                "sku": [f"{'FL' if k == 'flash' else 'DR'}-{r}-{j % 10}" for j, (k, r) in enumerate(zip(lots["kind"], lots["ref"], strict=True))],
                "title": [f"Limited lot ({r})" for r in lots["ref"]],
                "category": np.array(CATEGORIES)[g.integers(0, len(CATEGORIES), len(lots))],
                "list_price_cents": lots["price"].to_numpy().astype(np.int64),
                "is_limited": True, "active": False,
                "introduced_at": (lots["receipt_at"] - pd.Timedelta(days=7)).dt.normalize(),
            })
            lp["unit_cost_cents"] = np.round(lp["list_price_cents"] * (1 - p["gm"] - 0.05)).astype(np.int64)
            products = pd.concat([products, lp[products.columns]], ignore_index=True)
        price = products["list_price_cents"].to_numpy()
        cost = products["unit_cost_cents"].to_numpy()
        # --- buyers for limited lines: international drops go to APAC resellers
        cw = state.extras["cust_weight"]
        intl = state.extras["intl"]
        lot_intl = lots.set_index("_sku")["intl"] if len(lots) else pd.Series(dtype=bool)
        is_intl = ll["_sku"].map(lot_intl).fillna(False).to_numpy().astype(bool)
        pw_int = np.where(intl, cw, 0.0)
        pw_us = np.where(intl, 0.0, cw)
        lc = np.where(is_intl, g.choice(len(cw), size=len(ll), p=pw_int / pw_int.sum()), g.choice(len(cw), size=len(ll), p=pw_us / pw_us.sum()))
        normal = state.extras["normal_lines"]
        lim = pd.DataFrame({"_order": -1, "placed_at": ll["placed_at"], "_cust": lc, "_sku": ll["_sku"], "quantity": ll["quantity"],
                            "kind": ll["kind"], "_seen": ll["seen"], "_after": ll["qty_after"], "_oversold": ll["_oversold"], "_ref": ll["ref"],
                            "_lag": ll["_lag"], "_age": ll["_age"]})
        lines = pd.concat([normal.assign(_seen=np.nan, _after=np.nan, _oversold=False, _ref=None, _lag=np.nan, _age=np.nan), lim], ignore_index=True)
        # orders: normal orders keep their grouping, every limited line is its own order
        key = np.where(lines["_order"].to_numpy() >= 0, lines["_order"].to_numpy(), 10_000_000 + np.arange(len(lines)))
        lines["_okey"] = key
        first = lines.groupby("_okey", sort=False)["placed_at"].transform("min")
        lines["placed_at"] = first
        lines = lines.sort_values(["placed_at", "_okey", "_sku"], kind="stable").reset_index(drop=True)
        okeys = pd.unique(lines["_okey"])
        oid_map = pd.Series([f"SO{i:06d}" for i in range(1, len(okeys) + 1)], index=okeys)
        lines["order_id"] = oid_map.reindex(lines["_okey"]).to_numpy()
        lines["unit_price_cents"] = price[lines["_sku"].to_numpy()]
        lines["unit_cost_cents"] = cost[lines["_sku"].to_numpy()]
        lines["line_total_cents"] = lines["unit_price_cents"] * lines["quantity"]
        lines["order_item_id"] = [f"OI{i:07d}" for i in range(1, len(lines) + 1)]
        orders = lines.groupby("order_id", sort=False).agg(placed_at=("placed_at", "first"), _cust=("_cust", "first"), kind=("kind", "first"),
                                                           total_cents=("line_total_cents", "sum"), oversold=("_oversold", "max"),
                                                           ref=("_ref", "first")).reset_index()
        orders["customer_id"] = customers["customer_id"].to_numpy()[orders["_cust"].to_numpy()]
        orders["fee_cents"] = cents_array(orders["total_cents"].to_numpy() * FEE_RATE) + FEE_FIXED
        n = len(orders)
        # --- cancellations: oversold (most) + customer requests on regular orders
        os_ = orders["oversold"].to_numpy().astype(bool)
        cancel_os = os_ & (g.random(n) < p["backorder_cancel_rate"])
        expedite = os_ & ~cancel_os
        cancel_cust = ~os_ & (orders["kind"].to_numpy() == "regular") & (g.random(n) < p["customer_cancel_rate"])
        placed = orders["placed_at"]
        cancel_at = np.where(cancel_os, placed + pd.to_timedelta(g.uniform(4, 48, n), unit="h"), placed + pd.to_timedelta(g.uniform(0.5, 18, n), unit="h"))
        cancel_at = pd.Series(pd.to_datetime(cancel_at))
        refund_at = cancel_at + pd.to_timedelta(g.uniform(1, 5, n), unit="D")
        cancelled = (cancel_os | cancel_cust) & (cancel_at <= end_ts).to_numpy()
        orders["cancelled"] = cancelled
        orders["cancelled_at"] = cancel_at.where(cancelled)
        orders["refunded_at"] = refund_at.where(cancelled & (refund_at <= end_ts).to_numpy())
        orders["cancel_reason"] = np.where(cancelled, np.where(cancel_os, "out_of_stock_after_order", "customer_request"), None)
        # --- expedited purchases for oversold orders that are fulfilled late
        exp_recv = placed + pd.to_timedelta(g.uniform(3, 8, n), unit="D")
        ship_base = placed + pd.to_timedelta(np.where(orders["kind"] == "regular", g.uniform(0.3, 2.5, n), g.uniform(1, 3, n)), unit="D")
        ship_at = pd.Series(np.where(expedite, exp_recv + pd.to_timedelta(g.uniform(0.5, 1.5, n), unit="D"), ship_base))
        ship_at = pd.to_datetime(ship_at)
        shipped = ~cancelled & ~(cancel_os & ~cancelled) & (ship_at <= end_ts).to_numpy()
        # an oversold order cancelled after the as-of date is still open (not shipped)
        shipped &= ~(os_ & ~expedite & ~cancelled)
        orders["shipped_at"] = ship_at.where(shipped)
        orders["status"] = np.where(cancelled, "cancelled", np.where(shipped, "shipped", "processing"))
        freight = cents_array(orders["total_cents"].to_numpy() * p["freight_pct"] * g.lognormal(0, 0.15, n))
        orders["freight_cents"] = np.where(shipped, freight, 0)
        units_o = lines.groupby("order_id", sort=False)["quantity"].sum().reindex(orders["order_id"]).to_numpy()
        orders["expedite_cents"] = np.where(expedite & shipped, units_o * p["expedite_cost_per_case_cents"], 0)
        orders["is_international"] = intl[orders["_cust"].to_numpy()]
        # --- purchases: monthly replenishment of regular SKUs (order-up-to on available), lot receipts, expedites
        months = state.extras["months"]
        n_m = len(months)
        reg = lines[lines["kind"] == "regular"].merge(orders[["order_id", "cancelled", "cancelled_at"]], on="order_id")
        reg_m = ((reg["placed_at"].dt.year - cal.start.year) * 12 + reg["placed_at"].dt.month - cal.start.month).to_numpy()
        D = np.zeros((S, n_m + 1))
        np.add.at(D, (reg["_sku"].to_numpy(), reg_m), reg["quantity"].to_numpy())
        rec_rows = []
        avail = np.zeros(S)
        D[:, n_m] = np.ceil(D[:, n_m - 1] * (1 + p["growth_qoq"] / 3))  # next month's stock lands on the as-of date
        for m in range(n_m + 1):
            need = np.ceil(D[:, m] * 1.35 + 2)
            q = np.maximum(0, need - avail).astype(np.int64)
            m_start = pd.Timestamp(months[m]) if m < n_m else pd.Timestamp(months[-1]) + pd.offsets.MonthBegin(1)
            ra = m_start + pd.Timedelta(hours=6) - pd.Timedelta(days=1 if m else 0)
            for s in np.flatnonzero(q):
                rec_rows.append((int(s), int(q[s]), ra, "replenishment"))
            # cancellations release reserved stock back inside the month (conservative: ignore here)
            avail = avail + q - D[:, m]
        for sk, stock, ra in zip(lots["_sku"], lots["stock"], lots["receipt_at"], strict=True):
            rec_rows.append((int(sk), int(stock), ra, "limited_lot"))
        exp_o = orders[expedite & shipped]
        if len(exp_o):
            el = lines[lines["order_id"].isin(exp_o["order_id"])]
            er = exp_recv.reindex(exp_o.index)
            erm = pd.Series(er.to_numpy(), index=exp_o["order_id"].to_numpy())
            for sk, q, oid in zip(el["_sku"], el["quantity"], el["order_id"], strict=True):
                rec_rows.append((int(sk), int(q), erm[oid], "expedite"))
        po = pd.DataFrame(rec_rows, columns=["_sku", "quantity", "received_at", "po_type"])
        po["received_at"] = pd.to_datetime(po["received_at"])
        po = po[po["received_at"] <= end_ts].sort_values(["received_at", "_sku"], kind="stable").reset_index(drop=True)
        po["po_id"] = [f"PO{i:06d}" for i in range(1, len(po) + 1)]
        po["product_id"] = products["product_id"].to_numpy()[po["_sku"].to_numpy()]
        po["unit_cost_cents"] = cost[po["_sku"].to_numpy()]
        po["amount_cents"] = po["quantity"] * po["unit_cost_cents"]
        po["ordered_at"] = (po["received_at"] - pd.to_timedelta(np.where(po["po_type"] == "expedite", 2, g.integers(10, 25, len(po))), unit="D")).dt.normalize()
        pay_at = po["received_at"] + pd.Timedelta(days=30)
        po["paid_at"] = pay_at.where(pay_at <= end_ts)
        po["supplier_id"] = np.array([f"V{i:03d}" for i in range(1, 13)])[po["_sku"].to_numpy() % 12]
        # --- inventory events (available = on_hand - reserved) per SKU, in time order
        line_o = lines.merge(orders[["order_id", "cancelled", "cancelled_at"]], on="order_id")
        ev = pd.concat([
            pd.DataFrame({"_sku": po["_sku"], "occurred_at": po["received_at"], "event_type": np.where(po["po_type"] == "expedite", "expedite_receipt", "receipt"),
                          "qty_delta": po["quantity"], "reference_id": po["po_id"]}),
            pd.DataFrame({"_sku": line_o["_sku"], "occurred_at": line_o["placed_at"], "event_type": "order_reserved",
                          "qty_delta": -line_o["quantity"], "reference_id": line_o["order_id"]}),
            pd.DataFrame({"_sku": line_o.loc[line_o["cancelled"], "_sku"], "occurred_at": line_o.loc[line_o["cancelled"], "cancelled_at"],
                          "event_type": "cancel_release", "qty_delta": line_o.loc[line_o["cancelled"], "quantity"],
                          "reference_id": line_o.loc[line_o["cancelled"], "order_id"]}),
        ], ignore_index=True)
        ev["occurred_at"] = pd.to_datetime(ev["occurred_at"])
        ev = ev.sort_values(["_sku", "occurred_at", "event_type"], kind="stable").reset_index(drop=True)
        ev["qty_after"] = ev.groupby("_sku")["qty_delta"].cumsum().astype(np.int64)
        neg_regular = ev[(ev["_sku"] < S) & (ev["qty_after"] < 0)]
        if len(neg_regular):
            raise RuntimeError("replica_lag: a regular SKU went negative - replenishment rule bug")
        ev = ev.sort_values(["occurred_at", "_sku"], kind="stable").reset_index(drop=True)
        ev.insert(0, "event_id", np.arange(1, len(ev) + 1, dtype=np.int64))
        ev["sku"] = products["sku"].to_numpy()[ev["_sku"].to_numpy()]
        # --- checkout reads: logged since replicas went live (P4); seen value for regular SKUs from the lagged primary
        rep_from = pd.Timestamp(cal.bounds(state.p(4))[0])
        rl = lines[lines["placed_at"] >= rep_from].copy()
        rl["read_at"] = rl["placed_at"] - pd.to_timedelta(np.round(g.uniform(0.08, 0.9, len(rl)), 3), unit="s")
        reg_mask = rl["kind"].to_numpy() == "regular"
        if reg_mask.any():
            r = rl[reg_mask]
            lag_s = lag.lag(_secs(r["read_at"], epoch))
            age = g.uniform(0, p["cache_ttl_s"], len(r)) if p["cache_ttl_s"] else np.zeros(len(r))
            stale = (r["read_at"] - pd.to_timedelta(lag_s + age, unit="s")).to_numpy()
            q = pd.DataFrame({"_sku": r["_sku"].to_numpy(), "t": stale, "_i": np.arange(len(r))}).sort_values("t", kind="stable")
            evs = ev[["_sku", "occurred_at", "qty_after"]].sort_values("occurred_at", kind="stable")
            mm = pd.merge_asof(q, evs, left_on="t", right_on="occurred_at", by="_sku", direction="backward").sort_values("_i", kind="stable")
            rl.loc[reg_mask, "_seen"] = mm["qty_after"].fillna(0).to_numpy()
            rl.loc[reg_mask, "_lag"] = lag_s
            rl.loc[reg_mask, "_age"] = age
        reads = pd.DataFrame({"order_id": rl["order_id"], "sku": products["sku"].to_numpy()[rl["_sku"].to_numpy()], "available_qty_seen": rl["_seen"].astype(np.int64),
                              "requested_qty": rl["quantity"], "read_at": rl["read_at"],
                              "read_from": "cache" if p["cache_ttl_s"] else "replica",
                              "cache_age_ms": np.round(rl["_age"].to_numpy() * 1000).astype(np.int64) if p["cache_ttl_s"] else 0})
        reads = reads.sort_values(["read_at", "order_id"], kind="stable").reset_index(drop=True)
        reads.insert(0, "read_id", np.arange(1, len(reads) + 1, dtype=np.int64))
        # --- replica lag samples: 12 h baseline since P4, 10 s in flash windows, 2 min around drops, 5 min in batch jobs
        t_end = (end_ts - epoch).total_seconds()
        grid = [np.arange((rep_from - epoch).total_seconds(), t_end, p["base_sample_h"] * 3600.0)]
        for f in state.extras["flash_sales"]:
            a = (f["starts_at"] - epoch).total_seconds()
            b = (f["ends_at"] - epoch).total_seconds()
            grid.append(np.arange(a - 300, b + 300, p["flash_sample_s"]))
        for d in state.extras["drops"]:
            a = (d["start"] - epoch).total_seconds()
            grid.append(np.arange(a - 300, a + 50 * 60, p["drop_sample_s"]))
        for j in state.extras["batch_jobs"]:
            a = (j["started_at"] - epoch).total_seconds()
            b = (j["finished_at"] - epoch).total_seconds()
            grid.append(np.arange(a - 900, b + 900, 300.0))
        ts = np.unique(np.round(np.concatenate(grid)))
        ts = ts[(ts >= 0) & (ts <= t_end)]
        lag_ms = np.round(lag.lag(ts) * 1000).astype(np.int64)
        in_flash = np.zeros(len(ts), dtype=bool)
        for f in state.extras["flash_sales"]:
            in_flash |= (ts >= (f["starts_at"] - epoch).total_seconds()) & (ts < (f["ends_at"] - epoch).total_seconds())
        qps = np.round(np.where(in_flash, g.uniform(900, 2400, len(ts)), g.uniform(40, 220, len(ts)))).astype(np.int64)
        samples = pd.DataFrame({"sample_id": np.arange(1, len(ts) + 1, dtype=np.int64),
                                "sampled_at": epoch + pd.to_timedelta(ts, unit="s"), "replica": "replica-1", "lag_ms": lag_ms,
                                "primary_write_qps": qps, "replay_bytes_behind": (lag_ms * qps * 2.1).astype(np.int64)})
        # --- support tickets
        tk_os = orders[os_ & (g.random(n) < p["ticket_rate_oversold"])]
        tk_b = orders[~os_ & (g.random(n) < p["ticket_rate_base"])]
        tickets = pd.concat([
            pd.DataFrame({"order_id": tk_os["order_id"], "customer_id": tk_os["customer_id"],
                          "opened_at": tk_os["placed_at"] + pd.to_timedelta(g.uniform(6, 72, len(tk_os)), unit="h"),
                          "category": np.where(tk_os.index.isin(np.flatnonzero(cancel_os)), "order_cancelled", "late_delivery")}),
            pd.DataFrame({"order_id": tk_b["order_id"], "customer_id": tk_b["customer_id"],
                          "opened_at": tk_b["placed_at"] + pd.to_timedelta(g.uniform(2, 240, len(tk_b)), unit="h"),
                          "category": np.array(("damaged_item", "invoice_request", "late_delivery", "account"))[g.integers(0, 4, len(tk_b))]}),
        ], ignore_index=True)
        tickets = tickets[tickets["opened_at"] <= end_ts].sort_values(["opened_at", "order_id"], kind="stable").reset_index(drop=True)
        tickets.insert(0, "ticket_id", [f"TK{i:05d}" for i in range(1, len(tickets) + 1)])
        tickets["status"] = np.where(g.random(len(tickets)) < 0.92, "closed", "open")
        # --- cycle counts (red herring): from P6, counted == physical on-hand on the primary
        phys = pd.concat([pd.DataFrame({"_sku": po["_sku"], "t": po["received_at"], "d": po["quantity"]}),
                          pd.DataFrame({"_sku": lines["_sku"], "t": lines["order_id"].map(orders.set_index("order_id")["shipped_at"]), "d": -lines["quantity"]})],
                         ignore_index=True).dropna(subset=["t"])
        phys["t"] = pd.to_datetime(phys["t"])
        phys = phys.sort_values(["_sku", "t"], kind="stable")
        phys["on_hand"] = phys.groupby("_sku")["d"].cumsum()
        c0 = pd.Timestamp(cal.bounds(state.p(6))[0])
        n_cnt = int(S * 6)
        cq = pd.DataFrame({"_sku": g.integers(0, S, n_cnt), "t": c0 + pd.to_timedelta(g.uniform(0, (end_ts - c0).total_seconds(), n_cnt), unit="s")})
        cq["t"] = cq["t"].dt.floor("h")
        cq["_i"] = np.arange(n_cnt)
        cm = pd.merge_asof(cq.sort_values("t", kind="stable"), phys[["_sku", "t", "on_hand"]].sort_values("t", kind="stable"), on="t", by="_sku",
                           direction="backward").sort_values("_i", kind="stable")
        sys_q = cm["on_hand"].fillna(0).astype(np.int64).to_numpy()
        var = np.where(g.random(n_cnt) < 0.03, g.choice([-1, 1], n_cnt), 0)
        counts = pd.DataFrame({"count_id": [f"CC{i:05d}" for i in range(1, n_cnt + 1)], "sku": products["sku"].to_numpy()[cq["_sku"].to_numpy()],
                               "counted_at": cq["t"].to_numpy(), "system_on_hand": sys_q, "counted_qty": np.maximum(0, sys_q + var),
                               "counter": np.array(("team_a", "team_b", "team_c"))[g.integers(0, 3, n_cnt)]})
        counts = counts.sort_values(["counted_at", "count_id"], kind="stable").reset_index(drop=True)
        state.frames.update({"products": products, "orders": orders, "order_items": lines, "purchase_orders": po, "inventory_events": ev,
                             "checkout_reads": reads, "replica_lag_samples": samples, "support_tickets": tickets, "inventory_counts": counts})
        self._post(ctx, state)

    def _post(self, ctx: BuildContext, state: WorldState) -> None:
        """Opening balances, checkout batches, shipments, receipts, cancellations, refunds and the monthly close."""
        p, cal = state.params, state.calendar
        L = state.ledger
        gran = GL_BATCH[p["difficulty"]]
        months = state.extras["months"]
        orders, lines, po, tk = (state.frames[k] for k in ("orders", "order_items", "purchase_orders", "support_tickets"))
        R = p["revenue_ttm_cents"]
        end_date = cal.end
        ppe = cents(R * p["capex_pct"] * 6 * 0.55)
        accdep = cents(ppe * 0.45)
        cash0 = cents(R * 0.18)
        cs = cents(0.6 * (cash0 + ppe - accdep))
        L.open_books(cal.opening_date, {"1000": cash0, "1500": ppe, "1510": -accdep, "3000": -cs, "3100": -(cash0 + ppe - accdep - cs)})
        events: list[tuple[dt.date, int, Any]] = []
        ob = orders.assign(b=batch_end_dates(orders["placed_at"], gran))
        # in transit at a month end: checkouts of the last two days of the month (keeps GL rows flat at daily batching)
        month_end_batch = ob["b"].dt.day >= ob["b"].dt.days_in_month - 1
        ob["tr"] = np.where(month_end_batch & (ob["placed_at"] >= ob["b"] - pd.Timedelta(days=1)), ob["total_cents"] - ob["fee_cents"], 0)
        sb = ob.groupby("b", sort=True).agg(a=("total_cents", "sum"), f=("fee_cents", "sum"), n=("order_id", "size"), tr=("tr", "sum"))
        for b, r in sb.iterrows():
            dim = {"batch": f"checkout:{b.date()}", "rows": str(int(r.n))}
            # the acquirer settles T+2: checkouts of the batch's last two days are a card settlement receivable (review P0 #3)
            events.append((b.date(), 3, lambda d, a=int(r.a), f=int(r.f), t=int(r.tr), dm=dim: L.entry(
                d, [("1000", a - f - t, 0, dm), ("1100", t, 0, dm), ("5050", f, 0, dm), ("4000", 0, a, dm)], "Card checkout (batch)", "sales")))
            if r.tr:
                events.append(((b + pd.Timedelta(days=2)).date(), 3, lambda d, t=int(r.tr), dm=dim: L.collect_cash(
                    d, t, memo="Card settlement funds received", source="settlement_funds", dim=dm)))
        rb = po.assign(b=batch_end_dates(po["received_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        for b, r in rb.iterrows():
            events.append((b.date(), 2, lambda d, a=int(r.a), dm={"batch": f"receipts:{b.date()}", "rows": str(int(r.n))}: L.purchase_inventory(d, a, memo="Inventory receipts (batch)", dim=dm)))
        pp = po.dropna(subset=["paid_at"])
        pb = pp.assign(b=batch_end_dates(pp["paid_at"], gran)).groupby("b", sort=True).agg(a=("amount_cents", "sum"), n=("po_id", "size"))
        for b, r in pb.iterrows():
            events.append((b.date(), 5, lambda d, a=int(r.a), dm={"batch": f"vendor_payments:{b.date()}", "rows": str(int(r.n))}: L.pay_ap(d, a, memo="Vendor payments (batch)", dim=dm)))
        sh = orders.dropna(subset=["shipped_at"])
        cost_o = lines.assign(c=lines["unit_cost_cents"] * lines["quantity"]).groupby("order_id")["c"].sum()
        sh = sh.assign(cost=sh["order_id"].map(cost_o).to_numpy(), b=batch_end_dates(sh["shipped_at"], gran))
        shb = sh.groupby("b", sort=True).agg(c=("cost", "sum"), fr=("freight_cents", "sum"), ex=("expedite_cents", "sum"), n=("order_id", "size"))
        freight_m: dict[str, int] = {}
        for b, r in shb.iterrows():
            dim = {"batch": f"shipments:{b.date()}", "rows": str(int(r.n))}
            events.append((b.date(), 4, lambda d, a=int(r.c), dm=dim: L.relieve_inventory_cogs(d, a, memo="COGS on shipments (batch)", dim=dm)))
            events.append((b.date(), 4, lambda d, a=int(r.fr), x=int(r.ex), dm=dim: L.adjust(
                d, [("5030", a + x, 0, dm), ("2100", 0, a + x, dm)], "Carrier freight accrued incl. expedites (batch)", "fulfillment")))
            freight_m[f"{b:%Y-%m}"] = freight_m.get(f"{b:%Y-%m}", 0) + int(r.fr) + int(r.ex)
        cx = orders[orders["cancelled"]]
        cb = cx.assign(b=batch_end_dates(cx["cancelled_at"], gran)).groupby("b", sort=True).agg(a=("total_cents", "sum"), n=("order_id", "size"))
        for b, r in cb.iterrows():
            events.append((b.date(), 6, lambda d, a=int(r.a), dm={"batch": f"cancellations:{b.date()}", "rows": str(int(r.n))}: L.adjust(
                d, [("4100", a, 0, dm), ("2450", 0, a, dm)], "Order cancellations (refund owed)", "cancellations")))
        rf = cx.dropna(subset=["refunded_at"])
        rfb = rf.assign(b=batch_end_dates(rf["refunded_at"], gran)).groupby("b", sort=True).agg(a=("total_cents", "sum"), n=("order_id", "size"))
        for b, r in rfb.iterrows():
            events.append((b.date(), 6, lambda d, a=int(r.a), dm={"batch": f"refunds:{b.date()}", "rows": str(int(r.n))}: L.adjust(
                d, [("2450", a, 0, dm), ("1000", 0, a, dm)], "Card refunds issued (fee not returned)", "refunds")))
        rev_m = (orders.groupby(orders["placed_at"].dt.strftime("%Y-%m"))["total_cents"].sum())
        tk_m = tk.groupby(tk["opened_at"].dt.strftime("%Y-%m")).size()
        prev = {"payroll": 0, "opex": 0}
        gm_net = p["gm"] - p["freight_pct"] - 0.03
        for m, ms in enumerate(months):
            me = (pd.Timestamp(ms) + pd.offsets.MonthEnd(0)).date()
            keys = [f"{pd.Timestamp(months[j]):%Y-%m}" for j in range(max(0, m - 2), m + 1)]
            run_rate = float(np.mean([rev_m.get(k, 0) for k in keys])) or R / 12
            opex = cents(run_rate * max(0.08, gm_net - p["op_margin"]) * self.period_noise(ctx, state).ox(ms))  # review C2: opex noise
            payroll = cents(opex * 0.6)
            other = opex - payroll
            support = int(tk_m.get(f"{ms:%Y-%m}", 0)) * p["support_cost_cents"]
            if m > 0:
                events.append((ms + dt.timedelta(days=4), 1, lambda d, a=prev["payroll"]: L.pay_ap(d, a, payable_account="2110", memo="Payroll paid", source="payroll")))
                events.append((ms + dt.timedelta(days=9), 1, lambda d, a=prev["opex"]: L.pay_ap(d, a, payable_account="2100", memo="Accrued opex and carrier invoices paid", source="opex")))
            pr = dict(zip(("6000", "6100", "6200"), split_cents(payroll, [0.35, 0.35, 0.3]), strict=True))
            ox = dict(zip(("6000", "6200", "6400"), split_cents(other, [0.4, 0.3, 0.3]), strict=True))
            ox["6200"] += support
            events.append((me, 7, lambda d, s=pr: L.payroll(d, s)))
            events.append((me, 7, lambda d, s=ox: L.accrue_opex(d, s, memo="Operating expenses incl. support")))
            events.append((ms + dt.timedelta(days=14), 6, lambda d, a=cents(run_rate * p["capex_pct"]): L.capex(d, a)))
            events.append((me, 8, lambda d: L.depreciate(d, cents(L.closing_balances()["1500"] / 72))))
            if ms.month % 3 == 0:
                def tax(d: dt.date) -> None:
                    act = L.activity(L.period_of(d))
                    pretax = -sum(v for c, v in act.items() if c[0] in "4567" and c != "8000")
                    L.provision_tax(d, max(0, cents(pretax * p["tax_rate"])))
                events.append((me, 11, tax))
            if ms.month in (1, 4, 7, 10) and m > 0:
                events.append((ms + dt.timedelta(days=14), 1, lambda d: L.pay_tax(d, -L.closing_balances()["2400"])))
            prev = {"payroll": payroll, "opex": other + support + freight_m.get(f"{ms:%Y-%m}", 0)}
        events.sort(key=lambda e: (e[0], e[1]))
        for d, _prio, fn in events:
            if d <= end_date:
                fn(d)

    # ------------------------------------------------------------------ tables

    def build_tables(self, ctx: BuildContext, state: WorldState, builder: TableBuilder) -> None:
        """OMS, inventory, replica telemetry, finance assumptions + 2/4/6 dirt rules."""
        p, cal = state.params, state.calendar
        orders, lines = state.frames["orders"], state.frames["order_items"]
        products = state.frames["products"]
        o = orders.copy()
        out_orders = pd.DataFrame({"order_id": o["order_id"], "customer_id": o["customer_id"], "placed_at": o["placed_at"],
                                   "channel": np.where(o["kind"] == "regular", "web", np.where(o["kind"] == "flash", "flash_sale", "restock_drop")),
                                   "flash_sale_id": np.where(o["kind"] == "flash", o["ref"], None), "status": o["status"],
                                   "total_cents": o["total_cents"].astype(np.int64), "card_fee_cents": o["fee_cents"].astype(np.int64),
                                   "currency": "USD", "ship_country": np.where(o["is_international"], o["customer_id"].map(
                                       state.frames["customers"].set_index("customer_id")["country"]), "US"),
                                   "shipped_at": o["shipped_at"], "freight_cents": o["freight_cents"].astype(np.int64),
                                   "expedite_freight_cents": o["expedite_cents"].astype(np.int64)})
        items = pd.DataFrame({"order_item_id": lines["order_item_id"], "order_id": lines["order_id"],
                              "product_id": products["product_id"].to_numpy()[lines["_sku"].to_numpy()],
                              "sku": products["sku"].to_numpy()[lines["_sku"].to_numpy()], "quantity": lines["quantity"].astype(np.int64),
                              "unit_price_cents": lines["unit_price_cents"].astype(np.int64), "unit_cost_cents": lines["unit_cost_cents"].astype(np.int64),
                              "line_total_cents": lines["line_total_cents"].astype(np.int64)})
        cx = o[o["cancelled"]]
        cancellations = pd.DataFrame({"cancellation_id": [f"CX{i:05d}" for i in range(1, len(cx) + 1)], "order_id": cx["order_id"].to_numpy(),
                                      "cancelled_at": cx["cancelled_at"].to_numpy(), "reason": cx["cancel_reason"].to_numpy(),
                                      "amount_cents": cx["total_cents"].to_numpy().astype(np.int64), "ingest_id": 1})
        rf = cx.dropna(subset=["refunded_at"])
        refunds = pd.DataFrame({"refund_id": [f"RF{i:05d}" for i in range(1, len(rf) + 1)], "order_id": rf["order_id"].to_numpy(),
                                "refunded_at": rf["refunded_at"].to_numpy(), "amount_cents": rf["total_cents"].to_numpy().astype(np.int64),
                                "card_fee_returned_cents": 0, "method": "card"})
        fs = pd.DataFrame([{k: v for k, v in f.items() if k in ("flash_sale_id", "name", "starts_at", "ends_at", "n_lots")} for f in state.extras["flash_sales"]])
        fs["marketing_channel"] = "email+sms"
        ev = state.frames["inventory_events"][["event_id", "sku", "occurred_at", "event_type", "qty_delta", "qty_after", "reference_id"]]
        po = state.frames["purchase_orders"]
        po_out = po[["po_id", "supplier_id", "product_id", "ordered_at", "received_at", "quantity", "unit_cost_cents", "amount_cents", "po_type", "paid_at"]]
        # current inventory snapshot on the primary
        phys_rec = po.groupby("_sku")["quantity"].sum()
        shipped_lines = lines[lines["order_id"].isin(o.loc[o["shipped_at"].notna(), "order_id"])]
        shipped_q = shipped_lines.groupby("_sku")["quantity"].sum()
        live = lines[lines["order_id"].isin(o.loc[o["status"] == "processing", "order_id"])]
        reserved = live.groupby("_sku")["quantity"].sum()
        idx = np.arange(len(products))
        on_hand = phys_rec.reindex(idx, fill_value=0).to_numpy() - shipped_q.reindex(idx, fill_value=0).to_numpy()
        res = reserved.reindex(idx, fill_value=0).to_numpy()
        last_ev = state.frames["inventory_events"].groupby("sku")["occurred_at"].max()
        inventory = pd.DataFrame({"sku": products["sku"], "warehouse_id": "WH-1", "on_hand": on_hand.astype(np.int64), "reserved": res.astype(np.int64),
                                  "updated_at": products["sku"].map(last_ev)})
        jobs = pd.DataFrame(state.extras["batch_jobs"]) if state.extras["batch_jobs"] else None
        cfg_rows = [("checkout", "inventory_read_path", "replica", pd.Timestamp(cal.bounds(state.p(4))[0]) + pd.Timedelta(days=9)),
                    ("ops", "replica_lag_alert_threshold_s", str(p["alert_threshold_s"]), pd.Timestamp(cal.bounds(state.p(6))[0]) + pd.Timedelta(days=20)),
                    ("checkout", "inventory_decrement", "UPDATE inventory SET reserved = reserved + :qty WHERE sku = :sku", pd.Timestamp(cal.start) - pd.Timedelta(days=200))]
        if p["cache_ttl_s"]:
            cfg_rows.append(("edge", "availability_cache_ttl_s", str(int(round(p["cache_ttl_s"]))), pd.Timestamp(cal.bounds(state.p(5))[0]) + pd.Timedelta(days=12)))
        if p["nightly_job"]:
            cfg_rows.append(("analytics", "dashboard_refresh_dsn", "postgres://replica-1/shop", pd.Timestamp(state.extras["batch_jobs"][0]["started_at"]).normalize()))
        config = pd.DataFrame(cfg_rows, columns=["service", "key", "value", "changed_at"])
        ltv = float(round(o.loc[o["kind"] == "regular", "total_cents"].mean() * 6 * (p["gm"] - p["freight_pct"])))
        # the cost rollup uses exactly the published assumptions
        state.extras.update({"ltv_cents": ltv, "base_churn_pub": round(p["base_churn_12m"], 4), "churn_uplift_pub": round(p["churn_uplift"], 4)})
        assumptions = pd.DataFrame([
            ("support_cost_per_ticket_cents", float(p["support_cost_cents"]), "fully loaded cost of one support ticket"),
            ("customer_ltv_contribution_cents", ltv, "contribution margin of an average reseller over its remaining life"),
            ("base_churn_12m", state.extras["base_churn_pub"], "12-month churn of resellers without a cancelled order"),
            ("churn_uplift_after_cancellation", state.extras["churn_uplift_pub"], "churn multiplier for resellers who had an order cancelled"),
            ("card_fee_rate", FEE_RATE, "processor fee rate; fees are not returned on refunds"),
        ], columns=["key", "value", "note"])
        dirt_plan = [
            ("customers", "case_mixed:region"), ("support_tickets", "case_mixed:category"),
            ("products", "whitespace:title"), ("cancellations", "dup_keys:0.02"),
            ("checkout_reads", "nulls:cache_age_ms:0.03"), ("orders", "tz_mixed:placed_at"),
        ]
        n_rules = {"foundation": 2, "practitioner": 4, "principal": 6}[p["difficulty"]]
        dirt: dict[str, list[str]] = {}
        for t, rule in dirt_plan[:n_rules]:
            dirt.setdefault(t, []).append(rule)
        state.extras["orders_tz_dirty"] = "tz_mixed:placed_at" in dirt.get("orders", [])
        money = {c: "BIGINT" for c in ("total_cents", "card_fee_cents", "freight_cents", "expedite_freight_cents", "unit_price_cents", "unit_cost_cents",
                                         "line_total_cents", "amount_cents", "list_price_cents", "card_fee_returned_cents", "lag_ms", "replay_bytes_behind",
                                         "event_id", "read_id", "sample_id", "cache_age_ms")}
        ints = {c: "INTEGER" for c in ("quantity", "qty_delta", "qty_after", "available_qty_seen", "requested_qty", "on_hand", "reserved",
                                        "system_on_hand", "counted_qty", "primary_write_qps", "n_lots")}
        types = {**money, **ints}
        builder.add_frame("customers", state.frames["customers"], primary_key=["customer_id"], dirt=dirt.get("customers", ()), column_types=types,
                          description="Reseller accounts.")
        builder.add_frame("products", products[["product_id", "sku", "title", "category", "unit_cost_cents", "list_price_cents", "is_limited", "active", "introduced_at"]],
                          primary_key=["product_id"], column_types=types, dirt=dirt.get("products", ()),
                          descriptions={"is_limited": "limited lot sold only in one flash sale or restock drop"})
        builder.add_frame("inventory", inventory, primary_key=["sku"], column_types=types,
                          descriptions={"on_hand": "physical cases in the warehouse (primary)", "reserved": "cases reserved by open orders"},
                          description="Current inventory on the PRIMARY (available = on_hand - reserved).")
        builder.add_frame("inventory_events", ev, primary_key=["event_id"], column_types=types,
                          descriptions={"qty_after": "available (on_hand - reserved) on the primary after the event", "qty_delta": "+receipt / -reservation / +release"},
                          description="Append-only inventory ledger written by checkout and receiving on the primary.")
        builder.add_frame("orders", out_orders, primary_key=["order_id"], foreign_keys=[("customer_id", "customers", "customer_id")], column_types=types,
                          dirt=dirt.get("orders", ()), descriptions={"placed_at": "checkout commit time (UTC)", "card_fee_cents": "processor fee (kept by the processor on refunds)"},
                          description="Orders (paid by card at checkout).")
        builder.add_frame("order_items", items, primary_key=["order_item_id"], foreign_keys=[("order_id", "orders", "order_id"), ("product_id", "products", "product_id")],
                          column_types=types)
        builder.add_frame("checkout_reads", reads := state.frames["checkout_reads"], primary_key=["read_id"], foreign_keys=[("order_id", "orders", "order_id")],
                          column_types=types, dirt=dirt.get("checkout_reads", ()),
                          descriptions={"available_qty_seen": "available quantity checkout saw before reserving", "read_from": "replica | cache (cache in front of the replica)",
                                        "cache_age_ms": "age of the cached availability value"},
                          description="Availability reads logged by the checkout service (since read replicas went live).")
        del reads
        builder.add_frame("replica_lag_samples", state.frames["replica_lag_samples"], primary_key=["sample_id"], column_types=types,
                          descriptions={"lag_ms": "replication lag of replica-1 behind the primary"},
                          description="Replication lag telemetry (10-30 s during flash sales, 3-5 min around restock drops, 5 min during batch jobs, 12-24 h otherwise).")
        builder.add_frame("flash_sales", fs, primary_key=["flash_sale_id"], column_types=types, description="Flash-sale calendar (marketing).")
        if jobs is not None:
            builder.add_frame("batch_jobs", jobs[["job_name", "started_at", "finished_at"]].assign(target="replica-1", owner="analytics")
                              .reset_index(names="job_id").assign(job_id=lambda d: d["job_id"] + 1),
                              primary_key=["job_id"], column_types={"job_id": "BIGINT"}, description="Scheduled analytics jobs.")
        builder.add_frame("purchase_orders", po_out, primary_key=["po_id"], foreign_keys=[("product_id", "products", "product_id")], column_types=types,
                          descriptions={"po_type": "replenishment | limited_lot | expedite", "paid_at": "NULL = unpaid"})
        builder.add_frame("cancellations", cancellations, primary_key=["cancellation_id"], foreign_keys=[("order_id", "orders", "order_id")], column_types=types,
                          dirt=dirt.get("cancellations", ()), descriptions={"ingest_id": ">1 marks a re-ingested copy"})
        builder.add_frame("refunds", refunds, primary_key=["refund_id"], foreign_keys=[("order_id", "orders", "order_id")], column_types=types)
        builder.add_frame("support_tickets", state.frames["support_tickets"], primary_key=["ticket_id"], foreign_keys=[("order_id", "orders", "order_id")],
                          dirt=dirt.get("support_tickets", ()))
        builder.add_frame("inventory_counts", state.frames["inventory_counts"], primary_key=["count_id"], column_types=types,
                          description="Cycle counts (warehouse accuracy program).")
        builder.add_frame("service_config", config, primary_key=["service", "key"], description="Configuration change log.")
        builder.add_frame("kpi_assumptions", assumptions, primary_key=["key"], description="Finance assumptions used in cost rollups.")

    # ------------------------------------------------------------------ ties

    def subledger_ties(self, ctx: BuildContext, state: WorldState) -> list[SubledgerTie]:
        """Inventory, AP and refund liability tie; OMS GMV vs GL net revenue breaks by cancellations (designed)."""
        cal = state.calendar
        orders, lines, po = state.frames["orders"], state.frames["order_items"], state.frames["purchase_orders"]
        cost_o = lines.assign(c=lines["unit_cost_cents"] * lines["quantity"]).groupby("order_id")["c"].sum()
        sh = orders.dropna(subset=["shipped_at"])
        sh_cost = sh["order_id"].map(cost_o)
        inv, ap, liab, gmv, brk = {}, {}, {}, {}, {}
        for period in state.periods:
            s, e = cal.bounds(period)
            s_ts, e_ts = pd.Timestamp(s), pd.Timestamp(e) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            inv[period] = int(po.loc[po["received_at"] <= e_ts, "amount_cents"].sum() - sh_cost[sh["shipped_at"] <= e_ts].sum())
            ap[period] = int(po.loc[(po["received_at"] <= e_ts) & (po["paid_at"].isna() | (po["paid_at"] > e_ts)), "amount_cents"].sum())
            c = orders[orders["cancelled"]]
            liab[period] = int(c.loc[(c["cancelled_at"] <= e_ts) & (c["refunded_at"].isna() | (c["refunded_at"] > e_ts)), "total_cents"].sum())
            gmv[period] = int(orders.loc[(orders["placed_at"] >= s_ts) & (orders["placed_at"] <= e_ts), "total_cents"].sum())
            brk[period] = int(c.loc[(c["cancelled_at"] >= s_ts) & (c["cancelled_at"] <= e_ts), "total_cents"].sum())
        state.extras["gmv_break"] = brk
        return [
            SubledgerTie("inventory_receipts_less_shipments", inv, ["1200", "1210", "1220"], description="Received - shipped at cost == GL inventory"),
            SubledgerTie("ap_open_purchase_orders", ap, ["2000"], description="Received, unpaid POs == GL 2000"),
            SubledgerTie("refund_liability_open_cancellations", liab, ["2450"], description="Cancelled, not yet refunded orders == GL 2450"),
            SubledgerTie("orders_gmv_vs_net_revenue", gmv, ["4000", "4100"], basis="activity", expected_break_cents=brk,
                         description="OMS order value placed vs GL net revenue: breaks by cancellations booked in the quarter (designed)"),
        ]

    # ------------------------------------------------------------------ figures

    def _facts(self, state: WorldState) -> dict[str, Any]:
        if "facts" in state.extras:
            return state.extras["facts"]
        p = state.params
        orders, lines = state.frames["orders"], state.frames["order_items"]
        os_ = orders["oversold"].astype(bool)
        flash = orders["kind"] == "flash"
        jobs = state.extras["batch_jobs"]
        units_os = lines.loc[lines["order_id"].isin(orders.loc[os_, "order_id"]), "quantity"].sum()
        night = np.zeros(len(orders), dtype=bool)
        for j in jobs:
            night |= ((orders["placed_at"] >= j["started_at"]) & (orders["placed_at"] <= j["finished_at"])).to_numpy()
        os_nf = os_ & ~flash
        cx = orders[orders["cancelled"] & (orders["cancel_reason"] == "out_of_stock_after_order")]
        fee_leak = int(cx["fee_cents"].sum())
        tk = state.frames["support_tickets"]
        tk_os = tk[tk["order_id"].isin(orders.loc[os_, "order_id"])]
        support = len(tk_os) * p["support_cost_cents"]
        expedite = int(orders.loc[os_, "expedite_cents"].sum())
        affected = orders.loc[os_, "customer_id"].nunique()
        churn = affected * state.extras["base_churn_pub"] * (state.extras["churn_uplift_pub"] - 1) * state.extras["ltv_cents"]
        f = {
            "oversold_orders": int(os_.sum()), "oversold_units": int(units_os),
            "rate_flash": float(os_[flash].mean()) if flash.any() else 0.0,
            "rate_baseline": float(os_[~flash].mean()),
            "flash_orders": int(flash.sum()), "night_share": float((os_nf & night).sum() / max(1, os_nf.sum())),
            "fee_leak": fee_leak, "support": int(support), "expedite": expedite, "churn": float(churn), "affected": int(affected),
            "n_tickets_os": int(len(tk_os)), "cancel_os": int(len(cx)),
        }
        f["cost"] = fee_leak + support + expedite + churn
        state.extras["facts"] = f
        return f

    # ------------------------------------------------------------------ operations

    def build_operations(self, ctx: BuildContext, state: WorldState) -> OperationsState:
        """Checkout queueing (normal vs flash), inventory policy and KPIs by period."""
        p, cal = state.params, state.calendar
        st = state.statements
        orders = state.frames["orders"]
        per = cal.labels(orders["placed_at"])
        kpis = {}
        for period in state.periods:
            inc = next(i for i in st.income if i.period == period)  # type: ignore[union-attr]
            o = orders[per == period]
            kpis[period] = {"revenue_cents": float(inc.revenue_cents), "gross_margin": st.ratios[period]["gross_margin"],  # type: ignore[union-attr]
                            "net_income_cents": float(inc.net_income_cents), "orders": float(len(o)),
                            "oversold_orders": float(o["oversold"].sum()), "cancelled_orders": float(o["cancelled"].sum()),
                            "cancel_rate": float(o["cancelled"].mean()) if len(o) else 0.0, "cash_cents": float(state.ledger.trial_balance(period)["1000"])}
        lam_n = len(orders) / (len(state.extras["months"]) * 30.4 * 24)
        stations, summary = station_network(lam_n, [{"name": "checkout_api", "capacity_per_hour": lam_n / 0.2, "cs": 0.6},
                                                     {"name": "primary_db_writes", "capacity_per_hour": lam_n / 0.3, "cs": 0.8},
                                                     {"name": "replica_apply", "capacity_per_hour": lam_n / 0.35, "cs": 1.2}])
        flash_rate = p["n_orders"] / 27 / 30.4 / 24 * 60
        q = {**mm1(lam_n, lam_n / 0.3), "replica_lag_base_ms": p["replica_lag_base_ms"], "lag_under_load_s": p["lag_under_load_s"],
             "flash_rho_replica_apply": min(0.98, 0.35 * flash_rate), "throughput": summary["throughput"]}
        inc8 = next(i for i in st.income if i.period == state.periods[-1])  # type: ignore[union-attr]
        unit = {"gross_margin": st.ratios[state.periods[-1]]["gross_margin"], "arpa": float(orders["total_cents"].mean()),  # type: ignore[union-attr]
                "cac": 0.0, "ltv": float(state.extras["ltv_cents"]), "payback_months": 0.0, "churn_monthly": p["base_churn_12m"] / 12,
                "revenue_p8_cents": float(inc8.revenue_cents)}
        return build_operations_state(stations=stations, queueing=q, unit_economics=unit,
                                      supply_chain={"replenishment": "monthly order-up-to (1.35x month demand + 2 cases) for catalogue SKUs",
                                                    "limited_lots": "one receipt per lot, no replenishment", "expedite_lead_days": [3, 8]},
                                      pricing={"card_fee": {"rate": FEE_RATE, "fixed_cents": FEE_FIXED}}, kpis_by_period=kpis)

    # ------------------------------------------------------------------ ground truth

    def build_ground_truth(self, ctx: BuildContext, state: WorldState, con: duckdb.DuckDBPyConnection) -> GroundTruth:
        """Metrics of the section 11 key table (computed by running the reference SQL), queries and design."""
        p = state.params
        f = self._facts(state)

        placed = _utc("o.placed_at", state.extras["orders_tz_dirty"])
        placed_bare = _utc("placed_at", state.extras["orders_tz_dirty"])
        sql_os = f"""
WITH zero AS (
  SELECT sku, MIN(occurred_at) AS zero_at
  FROM inventory_events
  WHERE event_type = 'order_reserved' AND qty_after <= 0
  GROUP BY 1),
o AS (SELECT order_id, channel, {placed} AS placed_utc FROM orders o)
SELECT o.order_id, oi.sku, o.channel, o.placed_utc, z.zero_at,
       date_diff('millisecond', z.zero_at, o.placed_utc) / 1000.0 AS seconds_after_zero,
       cr.available_qty_seen,
       (SELECT quantile_cont(s.lag_ms, 0.95) FROM replica_lag_samples s
         WHERE s.sampled_at BETWEEN o.placed_utc - INTERVAL 1 MINUTE AND o.placed_utc) AS lag_p95_before_ms
FROM o
JOIN order_items oi ON oi.order_id = o.order_id
JOIN zero z ON z.sku = oi.sku
LEFT JOIN checkout_reads cr ON cr.order_id = o.order_id AND cr.sku = oi.sku
WHERE o.placed_utc > z.zero_at
ORDER BY o.placed_utc, o.order_id"""
        q_os = run_reference_query(con, "orders_after_primary_stockout", sql_os, signature=["with", "left join", "join", "group by", "date_diff("],
                                   metric_keys=["oversold_orders_count", "oversold_units"],
                                   checks=lambda df: {"oversold_orders": int(df["order_id"].nunique()),
                                                      "share_with_positive_seen": float((df["available_qty_seen"].fillna(0) > 0).mean())})
        n_os_sql = int(q_os.expected["checks"]["oversold_orders"])
        sql_rate = f"""
WITH zero AS (
  SELECT sku, MIN(occurred_at) AS zero_at FROM inventory_events
  WHERE event_type = 'order_reserved' AND qty_after <= 0 GROUP BY 1),
lines AS (
  SELECT o.order_id, o.channel, {placed} AS placed_utc,
         MAX(CASE WHEN {placed} > z.zero_at THEN 1 ELSE 0 END) AS oversold,
         SUM(CASE WHEN {placed} > z.zero_at THEN oi.quantity ELSE 0 END) AS oversold_units
  FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
  LEFT JOIN zero z ON z.sku = oi.sku
  GROUP BY 1, 2, 3)
SELECT CASE WHEN channel = 'flash_sale' THEN 'flash' ELSE 'baseline' END AS segment,
       COUNT(*) AS orders, SUM(oversold) AS oversold_orders, SUM(oversold_units) AS oversold_units,
       SUM(oversold) * 1.0 / COUNT(*) AS oversell_rate
FROM lines
GROUP BY 1
ORDER BY 1"""
        rate_df = con.execute(sql_rate).fetchdf().set_index("segment")
        q_rate = run_reference_query(con, "oversell_rate_flash_vs_baseline", sql_rate, signature=["with", "left join", "group by", "case when"],
                                     metric_keys=["oversell_rate_flash", "oversell_rate_baseline", "oversold_units"],
                                     checks=lambda df: {"oversold_orders_total": int(df["oversold_orders"].sum())})
        flash_sql = "EXISTS (SELECT 1 FROM flash_sales f WHERE s.sampled_at >= f.starts_at AND s.sampled_at < f.ends_at)"
        night_sql = ("EXISTS (SELECT 1 FROM batch_jobs j WHERE s.sampled_at BETWEEN j.started_at AND j.finished_at)" if p["nightly_job"] else "FALSE")
        sql_lag = f"""
SELECT quantile_cont(lag_ms, 0.50) FILTER (WHERE NOT {flash_sql} AND NOT {night_sql}) AS lag_p50_ms,
       quantile_cont(lag_ms, 0.99) AS lag_p99_ms,
       quantile_cont(lag_ms, 0.99) FILTER (WHERE {flash_sql}) AS lag_p99_flash_ms,
       quantile_cont(lag_ms, 0.99) FILTER (WHERE {night_sql}) AS lag_p99_nightly_ms,
       COUNT(*) AS samples
FROM replica_lag_samples s"""
        lag_row = con.execute(sql_lag).fetchdf().iloc[0]
        q_lag = run_reference_query(con, "replica_lag_percentiles", sql_lag, signature=["filter(", "count("],
                                    metric_keys=["lag_p50_ms", "lag_p99_ms", "lag_p99_flash_ms"],
                                    checks=lambda df: {"lag_p99_flash_s": float(df["lag_p99_flash_ms"].iloc[0]) / 1000.0})
        sql_corr = f"""
WITH zero AS (
  SELECT sku, MIN(occurred_at) AS zero_at FROM inventory_events
  WHERE event_type = 'order_reserved' AND qty_after <= 0 GROUP BY 1),
os AS (
  SELECT time_bucket(INTERVAL 10 MINUTE, {placed}) AS bucket,
         COUNT(DISTINCT o.order_id) AS orders,
         COUNT(DISTINCT CASE WHEN {placed} > z.zero_at THEN o.order_id END) AS oversold
  FROM orders o JOIN order_items oi ON oi.order_id = o.order_id LEFT JOIN zero z ON z.sku = oi.sku
  GROUP BY 1),
lg AS (
  SELECT time_bucket(INTERVAL 10 MINUTE, sampled_at) AS bucket, quantile_cont(lag_ms, 0.95) AS lag_p95
  FROM replica_lag_samples GROUP BY 1),
b AS (  -- 10-minute buckets with checkout activity and lag telemetry
  SELECT lg.bucket, lg.lag_p95, os.oversold
  FROM lg JOIN os ON os.bucket = lg.bucket),
r AS (
  SELECT bucket, lag_p95, oversold,
         AVG(rn_lag) OVER (PARTITION BY lag_p95) AS rank_lag,
         AVG(rn_os) OVER (PARTITION BY oversold) AS rank_os
  FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY lag_p95, bucket) AS rn_lag,
                  ROW_NUMBER() OVER (ORDER BY oversold, bucket) AS rn_os FROM b))
SELECT COUNT(*) AS buckets, SUM(oversold) AS oversold_orders, corr(rank_lag, rank_os) AS spearman_oversell_vs_lag_p95
FROM r"""
        corr_row = con.execute(sql_corr).fetchdf().iloc[0]
        q_corr = run_reference_query(con, "oversell_vs_lag_spearman_10min", sql_corr, signature=["over(", "partition by", "row_number(", "with", "left join"],
                                     metric_keys=["oversell_vs_lag_correlation"],
                                     checks=lambda df: {"spearman": float(df["spearman_oversell_vs_lag_p95"].iloc[0])})
        sql_cx = """
WITH cx AS (
  SELECT * FROM cancellations QUALIFY ROW_NUMBER() OVER (PARTITION BY cancellation_id ORDER BY ingest_id) = 1)
SELECT period_of(cx.cancelled_at) AS period, cx.reason, COUNT(*) AS cancellations,
       SUM(cx.amount_cents) AS refunded_value_cents, SUM(o.card_fee_cents) AS card_fees_lost_cents,
       SUM(o.expedite_freight_cents) AS expedite_cents
FROM cx JOIN orders o ON o.order_id = cx.order_id
GROUP BY 1, 2
ORDER BY 1, 2"""
        q_cx = run_reference_query(con, "cancellations_by_reason_and_quarter", sql_cx, signature=["qualify", "join", "group by", "count("],
                                   metric_keys=["cancellation_cost_cents"],
                                   checks=lambda df: {"oos_cancellations": int(df.loc[df["reason"] == "out_of_stock_after_order", "cancellations"].sum())})
        sql_cost = f"""
WITH zero AS (
  SELECT sku, MIN(occurred_at) AS zero_at FROM inventory_events
  WHERE event_type = 'order_reserved' AND qty_after <= 0 GROUP BY 1),
os AS (
  SELECT DISTINCT o.order_id, o.customer_id, o.card_fee_cents, o.expedite_freight_cents, o.status
  FROM orders o JOIN order_items oi ON oi.order_id = o.order_id JOIN zero z ON z.sku = oi.sku
  WHERE {placed} > z.zero_at),
a AS (SELECT key, value FROM kpi_assumptions)
SELECT
  SUM(CASE WHEN os.status = 'cancelled' THEN os.card_fee_cents ELSE 0 END) AS fee_leakage_cents,
  (SELECT COUNT(*) FROM support_tickets t WHERE t.order_id IN (SELECT order_id FROM os))
     * (SELECT value FROM a WHERE key = 'support_cost_per_ticket_cents') AS support_cents,
  SUM(os.expedite_freight_cents) AS expedite_cents,
  COUNT(DISTINCT os.customer_id) * (SELECT value FROM a WHERE key = 'base_churn_12m')
     * ((SELECT value FROM a WHERE key = 'churn_uplift_after_cancellation') - 1)
     * (SELECT value FROM a WHERE key = 'customer_ltv_contribution_cents') AS churn_ltv_cents
FROM os"""
        cost_row = con.execute(sql_cost).fetchdf().iloc[0]
        cost_sql_total = float(cost_row[["fee_leakage_cents", "support_cents", "expedite_cents", "churn_ltv_cents"]].astype(float).sum())
        q_cost = run_reference_query(con, "oversell_cost_rollup", sql_cost, signature=["with", "join", "case when", "count(", "distinct"],
                                     metric_keys=["cancellation_cost_cents"], checks=lambda df: {"total_cents": cost_sql_total})
        sql_gmv = """
WITH cx AS (
  SELECT * FROM cancellations QUALIFY ROW_NUMBER() OVER (PARTITION BY cancellation_id ORDER BY ingest_id) = 1),
gmv AS (SELECT period_of(placed_at) AS period, SUM(total_cents) AS gmv_cents FROM orders GROUP BY 1),
gl AS (
  SELECT j.period, SUM(l.credit_cents - l.debit_cents) AS net_revenue_cents
  FROM gl_lines l JOIN gl_journal j USING (entry_id)
  WHERE l.account_code IN ('4000', '4100') GROUP BY 1),
c AS (SELECT period_of(cancelled_at) AS period, SUM(amount_cents) AS cancelled_cents FROM cx GROUP BY 1)
SELECT gmv.period, gmv.gmv_cents, gl.net_revenue_cents, gmv.gmv_cents - gl.net_revenue_cents AS gap_cents,
       COALESCE(c.cancelled_cents, 0) AS cancelled_cents
FROM gmv JOIN gl USING (period) LEFT JOIN c USING (period)
ORDER BY 1"""
        if state.extras["orders_tz_dirty"]:
            sql_gmv = sql_gmv.replace("SELECT period_of(placed_at) AS period", f"SELECT period_of({placed_bare}) AS period")
        q_gmv = run_reference_query(con, "oms_gmv_vs_gl_net_revenue", sql_gmv, signature=["qualify", "left join", "join", "group by"],
                                    metric_keys=["cancellation_cost_cents"],
                                    checks=lambda df: {"gap_equals_cancellations": bool((df["gap_cents"] == df["cancelled_cents"]).all())})
        rf, rb = float(rate_df.loc["flash", "oversell_rate"]) if "flash" in rate_df.index else 0.0, float(rate_df.loc["baseline", "oversell_rate"])
        os_units = int(rate_df["oversold_units"].sum())
        metrics = [
            metric("oversold_orders_count", n_os_sql, "count", "Orders containing a SKU ordered after the primary's available quantity first hit zero "
                   "(temporal join of orders to MIN(occurred_at) of inventory_events with qty_after <= 0 on a reservation).",
                   pillar="sql", tol_rel=0.05, label="Oversold orders", hint="Find when each SKU hit zero on the primary, then the orders after it."),
            metric("oversold_units", os_units, "cases", "Cases on those order lines.", pillar="sql", tol_rel=0.05, label="Oversold cases", hint="Count the cases on the oversold order lines."),
            metric("oversell_rate_flash", rf, "ratio", "Oversold flash-sale orders / flash-sale orders (orders.channel = 'flash_sale').",
                   pillar="operations_strategy", tol_abs=0.003, label="Oversell rate in flash sales", hint="Oversold flash-sale orders over all flash-sale orders."),
            metric("oversell_rate_baseline", rb, "ratio", "Oversold non-flash orders / non-flash orders.", pillar="operations_strategy", tol_abs=0.003,
                   label="Oversell rate outside flash sales", hint="Same ratio for orders outside flash sales."),
            metric("lag_p50_ms", float(lag_row["lag_p50_ms"]), "ms", "Median of replica_lag_samples.lag_ms outside flash_sales windows and batch_jobs runs "
                   "(steady state; telemetry is sampled far more densely during events, so an unfiltered median is biased).", pillar="system_design",
                   tol_rel=0.15, label="Replica lag p50 (steady state)", hint="Exclude flash-sale windows and batch-job runs."),
            metric("lag_p99_ms", float(lag_row["lag_p99_ms"]), "ms", "99th percentile of all lag samples (quantile_cont).", pillar="system_design", tol_rel=0.15,
                   label="Replica lag p99", hint="Percentile of the lag telemetry."),
            metric("lag_p99_flash_ms", float(lag_row["lag_p99_flash_ms"]), "ms", "99th percentile of lag samples taken inside flash_sales windows.",
                   pillar="system_design", tol_rel=0.15, label="Replica lag p99 during flash sales", hint="Restrict the lag samples to flash-sale windows."),
            metric("oversell_vs_lag_correlation", float(corr_row["spearman_oversell_vs_lag_p95"]), "rho",
                   f"Spearman rank correlation over the {int(corr_row['buckets'])} 10-minute buckets with both orders and lag samples: oversold orders vs lag p95 "
                   "(average ranks for ties).", pillar="sql", tol_abs=0.1, label="Oversell vs lag correlation", hint="Bucket oversells and lag into 10-minute windows and rank-correlate."),
            metric("cancellation_cost_cents", f["cost"], "cents",
                   f"Card fees kept on {f['cancel_os']} cancelled oversold orders ({f['fee_leak']}) + {f['n_tickets_os']} tickets x support cost "
                   f"({f['support']}) + expedite freight ({f['expedite']}) + churn: {f['affected']} affected resellers x base churn x (uplift - 1) x LTV "
                   f"({f['churn']:.0f}); inputs in kpi_assumptions.", pillar="accounting_finance", tol_rel=0.12, label="Cost of oversells", hint="Fees kept by the processor, tickets, expedites and churn (kpi_assumptions)."),
        ]
        if p["nightly_job"]:
            metrics.append(metric("lag_p99_nightly_ms", float(lag_row["lag_p99_nightly_ms"]), "ms", "99th percentile of lag samples taken while a batch_jobs row was running.",
                                  pillar="system_design", tol_rel=0.15, label="Replica lag p99 during the nightly job", hint="Restrict the lag samples to batch-job runs."))
            metrics.append(metric("nightly_job_lag_attribution_share", f["night_share"], "ratio",
                                  "Oversold non-flash orders placed while nightly_dashboard_refresh ran on the replica / all oversold non-flash orders.",
                                  pillar="system_design", tol_abs=0.1, label="Share of off-sale oversells during the nightly job", hint="Which off-sale oversells happened while a batch job ran on the replica?"))
        anomalies = {a.id: a for a in state.anomalies}
        anomalies["A1"] = anomalies["A1"].model_copy(update={
            "reference_sql": [q_os, q_rate],
            "magnitude": {**anomalies["A1"].magnitude, "oversold_orders": float(n_os_sql), "oversell_rate_flash": round(rf, 6), "oversell_rate_baseline": round(rb, 6)},
            "evidence_trail": [f"{n_os_sql} orders were placed after their SKU's available quantity hit zero on the primary.",
                               "checkout_reads show available_qty_seen > 0 for them: the replica (and cache) was stale.",
                               f"Flash-sale oversell rate {rf:.2%} vs {rb:.2%} outside flash sales; cycle counts match the primary."]})
        anomalies["A2"] = anomalies["A2"].model_copy(update={
            "reference_sql": [q_cx, q_cost, q_gmv],
            "magnitude": {**anomalies["A2"].magnitude, "cancellation_cost_cents": round(f["cost"], 2), "fee_leakage_cents": float(f["fee_leak"]),
                          "expedite_cents": float(f["expedite"]), **{f"gmv_gap_{k}": float(v) for k, v in state.extras["gmv_break"].items()}},
            "evidence_trail": ["Cancellations with reason out_of_stock_after_order cluster in flash-sale windows.",
                               "OMS GMV exceeds GL net revenue by exactly the cancellations booked each quarter.",
                               f"Cost rollup {f['cost'] / 100:,.0f} USD: fees kept by the processor, tickets, expedites, churn."]})
        lag_q = [q_lag, q_corr]
        if p["nightly_job"]:
            sql_night = f"""
WITH j AS (SELECT job_id, started_at, finished_at FROM batch_jobs),
zero AS (
  SELECT sku, MIN(occurred_at) AS zero_at FROM inventory_events
  WHERE event_type = 'order_reserved' AND qty_after <= 0 GROUP BY 1),
os AS (
  SELECT DISTINCT o.order_id, {placed} AS placed_utc, o.ship_country
  FROM orders o JOIN order_items oi ON oi.order_id = o.order_id JOIN zero z ON z.sku = oi.sku
  WHERE {placed} > z.zero_at AND o.channel <> 'flash_sale')
SELECT CASE WHEN EXISTS (SELECT 1 FROM j WHERE os.placed_utc BETWEEN j.started_at AND j.finished_at)
            THEN 'during_nightly_job' ELSE 'other' END AS window_kind,
       COUNT(*) AS oversold_orders,
       COUNT(*) FILTER (WHERE ship_country <> 'US') AS international
FROM os
GROUP BY 1
ORDER BY 1"""
            q_night = run_reference_query(con, "off_sale_oversells_vs_nightly_job", sql_night, signature=["with", "join", "case when", "group by", "filter("],
                                          metric_keys=["nightly_job_lag_attribution_share"],
                                          checks=lambda df: {"during_job": int(df.loc[df["window_kind"] == "during_nightly_job", "oversold_orders"].sum())})
            anomalies["A3"] = anomalies["A3"].model_copy(update={
                "reference_sql": [q_night, q_lag],
                "magnitude": {**anomalies["A3"].magnitude, "nightly_job_lag_attribution_share": round(f["night_share"], 6),
                              "lag_p99_nightly_ms": float(lag_row["lag_p99_nightly_ms"])},
                "evidence_trail": ["Lag spikes every night between ~01:30 and 04:00 UTC exactly while batch_jobs run on replica-1.",
                                   "International restock-drop orders at 02:00 UTC oversell steadily.",
                                   "service_config: availability cache TTL on top of the replica."]})
            lag_q = [q_lag, q_corr]
        a1 = anomalies["A1"]
        anomalies["A1"] = a1.model_copy(update={"reference_sql": [*a1.reference_sql, *lag_q]})
        herrings_all = [
            "The warehouse miscounts - inventory_counts match the primary's on_hand; the oversells happen within seconds of a sell-out.",
            "Bots bought everything - order velocity is high but the buyers are real resellers; the problem is what checkout reads.",
            "The replica is underpowered; upgrade it - that helps p50, but flash write bursts and the nightly job still create minute-long spikes.",
        ]
        herrings = herrings_all[: p["n_red_herrings"]]
        root = ("Checkout decides availability from the asynchronous read replica"
                + (f" (behind an availability cache with a {p['cache_ttl_s']:.0f}s TTL)" if p["cache_ttl_s"] else "")
                + " and then reserves on the primary with an unconditional decrement. During flash sales the replica falls "
                f"{lag_row['lag_p99_flash_ms'] / 1000:.0f}s behind (p99), so buyers keep seeing stock after a lot has sold out: "
                f"{n_os_sql} oversold orders ({rf:.2%} of flash-sale orders vs {rb:.2%} otherwise)"
                + (f"; a nightly analytics job on the same replica adds ~{p['nightly_lag_s']:.0f}s of lag at night, hitting APAC drops" if p["nightly_job"] else "")
                + f". Oversells become cancellations, refunds with the card fee lost, expedites, tickets and churn ({f['cost'] / 100:,.0f} USD). "
                "Fix the read path: reserve with an atomic conditional UPDATE on the primary (read-your-writes for decision reads), "
                "move OLAP off the replica, keep caches for browse data only, and add backpressure for flash sales.")
        python_ref = (
            "import pandas as pd\n"
            "s = tables['replica_lag_samples'].assign(sampled_at=lambda d: pd.to_datetime(d['sampled_at']))\n"
            "ev = tables['inventory_events']\n"
            "zero = ev[(ev.event_type == 'order_reserved') & (ev.qty_after <= 0)].groupby('sku')['occurred_at'].min()\n"
            "o = tables['orders'].merge(tables['order_items'][['order_id', 'sku']], on='order_id')\n"
            "o['placed_at'] = pd.to_datetime(o['placed_at'], utc=True, format='mixed').dt.tz_localize(None)\n"
            "o['oversold'] = o['placed_at'] > o['sku'].map(zero)\n"
            "os10 = o[o.oversold].set_index('placed_at').resample('10min')['order_id'].nunique()\n"
            "lag10 = s.set_index('sampled_at')['lag_ms'].resample('10min').quantile(0.95).dropna()\n"
            "b = pd.concat([lag10.rename('lag_p95'), os10.rename('oversold')], axis=1).reindex(lag10.index).fillna({'oversold': 0})\n"
            "result = {'oversold_orders': int(o.loc[o.oversold, 'order_id'].nunique()),\n"
            "          'spearman': float(b['lag_p95'].rank().corr(b['oversold'].rank()))}\n"
        )
        return GroundTruth(
            root_cause=root, anomalies=[anomalies[k] for k in ("A1", "A2", "A3") if k in anomalies], red_herrings=herrings, expected_metrics=metrics,
            reference_solution=ReferenceSolution(
                sql=[q_os.sql, q_rate.sql, q_lag.sql, q_corr.sql, q_cx.sql, q_cost.sql, q_gmv.sql], python=[python_ref],
                memo_bullets=[
                    "Replicas are fine for reads that tolerate staleness; an availability check before a write is a decision read.",
                    "Reading from the primary is not enough (read-then-write still races): reserve with UPDATE ... WHERE available >= qty and treat 0 rows as sold out.",
                    "Upgrading the replica lowers p50 but flash write bursts and OLAP scans still produce multi-second spikes.",
                    "Move the nightly analytics job to a dedicated replica or the warehouse (CDC); cache browse data, not availability." if p["nightly_job"]
                    else "Keep the lag alert meaningful; raising the threshold hid the problem.",
                    f"Oversells cost {f['cost'] / 100:,.0f} USD so far (fees kept, tickets, expedites, churn) and grow with flash-sale size.",
                    "Rate-limit / queue checkout during flash sales only after the read path is correct.",
                ],
                design_mermaid=FIXED_MERMAID),
            decision_levers=[],
        )

    # ------------------------------------------------------------------ dossier

    def build_dossier_seed(self, ctx: BuildContext, state: WorldState) -> DossierSeed:
        """Reveal / conceal lists, stakeholders, timeline and the flawed architecture."""
        p = state.params
        rng = ctx.rng.spawn("dossier")
        P = state.periods
        f = self._facts(state)
        names = rng.pick_names(4)
        orders = state.frames["orders"]
        last_fs = state.extras["flash_sales"][-1]
        fs_orders = orders[(orders["kind"] == "flash") & (orders["ref"] == last_fs["flash_sale_id"])]
        cx_pct = float(fs_orders["cancelled"].mean()) if len(fs_orders) else 0.0
        reveal = [
            f"The latest flash sale ({last_fs['name']}) set an order record; {cx_pct:.1%} of its orders were later cancelled.",
            "Resellers complain about orders cancelled 'for stock you told us you had'.",
            "Operations launched a warehouse-accuracy (cycle count) initiative.",
            f"DBA note: 'replica lag alerts are noisy - threshold raised to {p['alert_threshold_s']}s'.",
        ]
        if p["nightly_job"]:
            reveal.append("Analytics moved its nightly dashboard refresh 'off the primary' to protect checkout.")
        conceal = [
            "checkout reads availability from the read replica (stale read) before an unconditional decrement",
            "replication lag numbers inside flash-sale windows",
            f"oversold order counts ({f['oversold_orders']}) and the cost rollup",
            "the fix: atomic conditional update / reservation on the primary",
            "A1", "A2",
        ]
        if p["nightly_job"]:
            conceal += ["the nightly analytics job runs on the same replica", f"availability cache TTL {p['cache_ttl_s']:.0f}s", "A3"]
        stakeholders = [
            Stakeholder(name=names[0], role="VP Engineering", agenda="Keep the site up for bigger flash sales.", disposition="proud of the scaling work",
                        knows=["read replicas were added to scale reads"], hides=["checkout's availability read was moved to the replica too"]),
            Stakeholder(name=names[1], role="Head of Fulfilment", agenda="Clear the warehouse of blame.", disposition="defensive",
                        knows=["cycle counts match the system"], hides=[]),
            Stakeholder(name=names[2], role="Analytics Lead", agenda="Fast dashboards every morning.", disposition="unaware",
                        knows=["the nightly refresh is heavy"], hides=["pointed the job at replica-1"] if p["nightly_job"] else []),
            Stakeholder(name=names[3], role="CMO", agenda="Double the size of flash sales.", disposition="impatient",
                        knows=["flash sales drive acquisition"], hides=["promised a bigger sale next quarter"]),
        ]
        tl = [
            TimelineEvent(period=P[3], headline="Read replicas introduced", detail="Checkout and catalogue reads moved to replica-1."),
            TimelineEvent(period=P[4], headline="First flash sale - 'site stayed up'", detail="Record traffic without downtime."),
            TimelineEvent(period=P[5], headline="Lag alert threshold raised to 60s", detail="Alerts during sales were 'noise'."),
            TimelineEvent(period=P[7], headline="Biggest flash sale; cancellations spike", detail=f"{cx_pct:.1%} of the sale's orders cancelled."),
        ]
        if p["n_red_herrings"] >= 2:
            tl.append(TimelineEvent(period=P[5], headline="Cycle-count program launched", detail="Warehouse accuracy initiative.", is_red_herring=True))
        if p["n_red_herrings"] >= 3:
            tl.append(TimelineEvent(period=P[6], headline="Bot-mitigation vendor evaluated", detail="Suspected bots buying out lots.", is_red_herring=True))
        if p["nightly_job"]:
            tl.append(TimelineEvent(period=P[6], headline="Analytics moved nightly job to the replica", detail="'Off the primary' to protect checkout."))
        tl.sort(key=lambda e: e.period)
        ttl = f"{p['cache_ttl_s']:.0f}s" if p["cache_ttl_s"] else "n/a (browse pages only)"
        return DossierSeed(reveal=reveal, conceal=conceal, stakeholders=stakeholders, timeline=tl,
                           artifacts=["architecture_diagram", "flash_sale_press_release", "dba_alert_note", "reseller_complaint_emails", "cycle_count_report"],
                           architecture_mermaid=FLAWED_MERMAID.format(ttl=ttl, olap=" (from P7)" if p["nightly_job"] else " (planned)"))

    # ------------------------------------------------------------------ simulation

    def levers(self, ctx: BuildContext, state: WorldState) -> list[Lever]:
        """The six spec levers; inventory_check_path coded 0 replica / 1 primary / 2 conditional update."""
        p = state.params
        return [
            Lever(key="inventory_check_path", label="Inventory check (0 replica / 1 primary read / 2 conditional update)", min=0, max=2, default=0, unit="code",
                  effect="Conditional update removes oversells; a primary read without it leaves a small race."),
            Lever(key="isolate_olap", label="Move analytics off the checkout replica (0/1)", min=0, max=1, default=0, unit="bool",
                  effect="Nightly lag back to base."),
            Lever(key="cache_ttl_s", label="Availability cache TTL", min=0, max=300, default=round(p["cache_ttl_s"]), unit="s", effect="Staleness on top of lag."),
            Lever(key="rate_limit_checkout_rps", label="Checkout rate limit during flash sales (0 = none)", min=0, max=500, default=0, unit="rps",
                  effect="Less lag under load, fewer conversions."),
            Lever(key="upgrade_replica", label="Bigger replica instance (0/1)", min=0, max=1, default=0, unit="bool", effect="p50 / 2, p99 / 1.3; cost up."),
            Lever(key="flash_sale_size_multiplier", label="Flash-sale size", min=0.5, max=3, default=1, unit="x", effect="Revenue up; oversells scale with it."),
        ]

    def simulation_model(self, ctx: BuildContext, state: WorldState) -> dict[str, Any]:
        """Reduced-form model around P8 (module docstring)."""
        p, cal = state.params, state.calendar
        f = self._facts(state)
        P8 = state.periods[-1]
        orders = state.frames["orders"]
        o8 = orders[cal.labels(orders["placed_at"]) == P8]
        os8 = float(o8["oversold"].sum())
        flash8 = o8[o8["kind"] == "flash"]
        night8 = 0.0
        for j in state.extras["batch_jobs"]:
            night8 += float((o8["oversold"] & (o8["kind"] != "flash") & (o8["placed_at"] >= j["started_at"]) & (o8["placed_at"] <= j["finished_at"])).sum())
        inc8 = next(i for i in state.statements.income if i.period == P8)  # type: ignore[union-attr]
        cost_per_os = f["cost"] / max(1, f["oversold_orders"])
        lag_p99 = float(np.percentile(state.frames["replica_lag_samples"]["lag_ms"], 99))
        flash_rev = float(flash8["total_cents"].sum())
        kpis = {
            "oversold_orders": os8, "oversell_cost_cents": os8 * cost_per_os, "cancelled_orders": float(o8["cancelled"].sum()),
            "lag_p99_ms": lag_p99, "lag_nightly_ms": p["nightly_lag_s"] * 1000, "revenue_cents": float(inc8.revenue_cents),
            "net_income_cents": float(inc8.net_income_cents), "flash_revenue_cents": flash_rev, "conversion_rate": 0.031,
            "infra_cost_cents": 0.012 * float(inc8.revenue_cents),
        }
        stale_share = p["cache_ttl_s"] / 2 / max(1.0, p["cache_ttl_s"] / 2 + p["lag_under_load_s"])
        effects = [
            {"lever": "inventory_check_path", "kpi": "oversold_orders", "delta_per_unit": -0.5 * os8, "lag_periods": 0},
            {"lever": "inventory_check_path", "kpi": "oversell_cost_cents", "delta_per_unit": -0.5 * os8 * cost_per_os, "lag_periods": 0},
            {"lever": "inventory_check_path", "kpi": "cancelled_orders", "delta_per_unit": -0.5 * os8 * p["backorder_cancel_rate"], "lag_periods": 0},
            {"lever": "inventory_check_path", "kpi": "net_income_cents", "delta_per_unit": 0.5 * os8 * cost_per_os * (1 - p["tax_rate"]), "lag_periods": 1},
            {"lever": "isolate_olap", "kpi": "lag_nightly_ms", "delta_per_unit": -(p["nightly_lag_s"] * 1000 - p["replica_lag_base_ms"]) if p["nightly_job"] else 0.0, "lag_periods": 0},
            {"lever": "isolate_olap", "kpi": "oversold_orders", "delta_per_unit": -night8, "lag_periods": 0},
            {"lever": "cache_ttl_s", "kpi": "oversold_orders", "delta_per_unit": stale_share * os8 / max(1.0, p["cache_ttl_s"]) if p["cache_ttl_s"] else 0.02 * os8 / 60, "lag_periods": 0},
            {"lever": "rate_limit_checkout_rps", "kpi": "lag_p99_ms", "delta_per_unit": -lag_p99 * 0.0012, "lag_periods": 0},
            {"lever": "rate_limit_checkout_rps", "kpi": "conversion_rate", "delta_per_unit": -0.031 * 0.0004, "lag_periods": 0},
            {"lever": "rate_limit_checkout_rps", "kpi": "flash_revenue_cents", "delta_per_unit": -flash_rev * 0.0004, "lag_periods": 0},
            {"lever": "upgrade_replica", "kpi": "lag_p99_ms", "delta_per_unit": -lag_p99 * (1 - 1 / 1.3), "lag_periods": 0},
            {"lever": "upgrade_replica", "kpi": "infra_cost_cents", "delta_per_unit": 0.004 * float(inc8.revenue_cents), "lag_periods": 0},
            {"lever": "upgrade_replica", "kpi": "oversold_orders", "delta_per_unit": -0.15 * os8, "lag_periods": 0},
            {"lever": "flash_sale_size_multiplier", "kpi": "flash_revenue_cents", "elasticity": 1.0, "lag_periods": 0},
            {"lever": "flash_sale_size_multiplier", "kpi": "revenue_cents", "delta_per_unit": flash_rev, "lag_periods": 0},
            {"lever": "flash_sale_size_multiplier", "kpi": "oversold_orders", "elasticity": 1.2, "lag_periods": 0},
        ]
        n_win = max(1, len(set(p["flash_periods"])))
        drag = {"oversold_orders": f["oversold_orders"] / n_win * 0.15, "oversell_cost_cents": f["cost"] / n_win * 0.15,
                "net_income_cents": -f["cost"] / n_win * (1 - p["tax_rate"])}
        bounds = {k: [0.0, None] for k in ("oversold_orders", "oversell_cost_cents", "cancelled_orders", "lag_p99_ms", "lag_nightly_ms", "conversion_rate")}
        resolved = {"inventory_check_path": 2.0}
        if p["nightly_job"]:
            resolved["isolate_olap"] = 1.0
        return {"kpis": kpis, "lever_effects": effects, "anomaly_drag": drag, "bounds": bounds, "resolved_if": resolved}
