# Axiom — Principal Review of Generated Worlds (read-only)

Scope: 28 worlds (14 archetypes × practitioner seed 42 and principal seed 7), every `world.json`
and `company.duckdb` opened and queried, ground truth, dossier seeds, simulation models, the
26 fallback benchmark keys, the five prompt templates, and the shipped screenshots. Judged as a
CFO, a COO, a staff engineer and a forensic accountant would judge it. Nothing was edited.

Headline: the mechanics are sound. All 28 worlds pass every invariant (67–107 checks each),
regenerate byte-identical, and every reference query runs with a signature. The anomalies are
real, discoverable, and most ground-truth keys are well defined. What is **not** yet credible is
the *company around the anomaly*: several balance sheets are missing lines any analyst would
expect, one archetype produces a 12-person company with $44–77M of revenue, one exposes station
utilizations of 2× to 29×, and the whole batch is stamped "growth stage, FYE 12-31" with
machine-flat margins. Those are the P0/P1 items below; they are fixable in the archetype files
without touching the ledger engine.

## (a) Per-archetype scorecard (1 = unusable, 5 = would pass a partner review)

| Archetype | Realism | Data room | Ground truth | Dossier | Verdict |
|---|:-:|:-:|:-:|:-:|---|
| working_capital_crunch | 4 | 4 | 5 | 4 | The best world. DSO 54→76, DIO 72→92, revolver 99% drawn, CFO −$3.7M vs NI +$0.5M; reference DSO query handles dedup, cutoff and dirt. One live SQL on `invoices.terms_days` (45→54→68) gives the cause away at foundation, which is acceptable there. `resolved_if` cannot express "cut credit terms" (see C3). |
| forensic_revenue | 4 | 5 | 5 | 4 | Beneish M −1.31 at P8 vs −2.52 clean, last-5-day share 21% vs 12%, hold notes, 93-day terms, returns reversing next quarter. Customers table has tax-id whitespace and region case dirt. Baseline DSO 15–18 days is low for wholesale consumer goods; the author knows. CFO reveal "DSO (29 days)" is the `dso_p8_days` answer rounded (tolerance ±3). |
| window_reconciliation | 3 | 4 | 5 | 4 | Reconciling items sum to the unexplained difference to the cent; QUALIFY/LAG/anti-join SQL is what a strong analyst writes. But the company is "billed monthly in advance" with **zero deferred revenue and zero AP** in all 8 quarters, and principal shows $13.1M revenue on 26 heads. 19–20 expected metrics is too many asks. |
| abc_cost_distortion | 3 | 4 | 4 | 4 | ABC vs volume tables and the solved overhead pool are right (distortion −23.6% recovered). But `operations.stations` reports utilization 2.21, 8.83, 13.96, 28.92 (Assembly, Inspection, Painting) — impossible numbers in the public view. Revenue flat 8 quarters while cash grows $4.1M→$9.9M with no dividend, capex or debt change; current ratio 5.8 on a "growth" manufacturer. |
| ma_synergies | 3 | 4 | 4 | 4 | Deal model, PPA, goodwill plug and impairment exposure all recompute. Pre-deal cash $129M on $22M quarterly revenue, AP zero in every period, $8.9M goodwill impairment exposure = 75% of goodwill two quarters after close (plausible only if the deal was egregious; the dossier does not frame it that way). Interest coverage 0.02 shown as a ratio when operating income ≈ 0 is noise. |
| toc_bottleneck | 3 | 5 | 5 | 5 | Constraint is QA Audit (0.99 incl. setups) while run-only efficiency ranks Labeling and the capex request targets Pack-out (0.72): exactly the ToC lesson, with the SQL to prove it. Revenue per head $388k–$515k for a kitting/co-packing operation is 2–3× reality. `queueing.L = 2,434` units is WIP, fine, but unlabeled. |
| cohort_collapse | 3 | 4 | 4 | 4 | Blended churn 1.3%/mo flat while post-cohorts churn 7.8%; sBG fit and commission-asset trace present. Balance sheet: AR $10k–$42k on $21.7M quarterly revenue (DSO 0.18 days) for B2B SaaS "monthly or annual", AP zero. Cash $37M flat across 8 loss-making quarters with no financing line: where does the burn come from? |
| bullwhip | 3 | 4 | 3 | 4 | Amplification is generated endogenously and POS is the unused signal. But `amplification_tier3 = 113×` and `cv_company_po = 1.28` come from counting weeks with no PO as zero per SKU; the company-level weekly PO series has CV 0.41 (≈12× vs POS). A learner using the natural definition misses the ask by an order of magnitude. P6 AP drops $7.0M→$0.7M and back to $6.2M; DIO 11 days for a consumer-goods distributor vs 30–60 benchmark. |
| pricing_elasticity | 4 | 4 | 4 | 3 | Segment elasticities (−3.0 / −1.2 / −0.55), pocket-price waterfall and PVM are right. Reveal list says "Revenue is roughly flat (−9.5%)" (contradiction) and lists units −18%, competitor −5%, sales asking for discount authority: that is the whole mechanism at practitioner. Principal operating margin swings −0.8%→11.1% in 8 quarters. |
| idempotent_webhooks | 3 | 5 | 5 | 5 | Tables look like a Stripe export (`pi_`, `evt_`, `idk_` keys, payload JSON, mixed-tz delivery timestamps); 44 duplicates clustered in 3 incident windows; dedupe keyed on the wrong id. Balance sheet has zero AR, AP, inventory, deferred revenue in every period for a 57-person marketplace; only "Customer deposits" and "Accrued liabilities". Gross margin 84% at practitioner vs 75% at principal with the same business model. |
| outbox_pattern | 4 | 4 | 4 | 4 | Lost events from pod kills during deploys, phantom events at principal, metered-vs-GL break declared per quarter. GM drift −0.8 pts is small for a case headline (spec wanted 1–3). Company "founded 1999" for a Kafka-era usage-API platform with 62 heads. |
| replica_lag | 3 | 5 | 4 | 4 | Per-attempt stock simulation, lag samples, nightly OLAP job attribution 74% at principal: excellent data room. But a "marketplace" that carries $1.0–1.5M inventory and 16–27% gross margin is a reseller; 12–20 heads at $550k revenue/head; `lag_p99_ms = 417,511` (7 minutes) on a 5-second checkout is the kind of number that needs a sentence of context in the reveal. |
| cohort_matrix | 1 | 4 | 4 | 3 | **Headcount 12 with $44.0M (practitioner) and $77.0M (principal) TTM revenue**: $3.7M–$6.4M per employee. Revenue grows 6.2× in 8 quarters (2.2M→13.7M/quarter) from ~150–300 new SMB subscriptions a month at $630–$1,980 MRR, which does not add up to $13.7M/quarter either. Gross margin is exactly 0.700 / 0.691 in all 8 quarters; AR and deferred revenue zero; AP grows in lockstep with revenue; `unit_economics.cac = 0`. The matrix, censoring and reactivation logic are good; the company is a placeholder. |
| monte_carlo_cash | 4 | 4 | 5 | 4 | One simulation source used for calibration, ground truth and the published reference; reference Python reproduces metrics inside the sandbox. Cash $0.4–1.1M on $23M revenue is the point. Gross margin swings 11.5%→25.4%→18.7% between adjacent quarters is wide even for brokerage. |

## (b) Punch list

### P0 — wrong or misleading; fix before this is shown to anyone

1. **cohort_matrix company scale** (`archetypes/cohort_matrix.py`, `build_company`/params). Headcount hits the `max(12, rev/per_head)` floor in `base.py:376` because `per_head` is far too large. Observed: hc=12, revenue_ttm $43,997,940 (seed 42) and $77,028,000 (seed 7); 5,604 subscriptions with MRR $630–$2,970 cannot produce $13.7M in a quarter (max ≈ 5,604 × $2,970 × 3 ≈ $50M only if every customer were on the top plan; the plan mix is 62% at $630). Fix: derive revenue from the subscription table (Σ active MRR × 3 per quarter), set `per_head` to the SaaS benchmark (`saas.revenue_per_employee` or ~$150–250k), and let headcount grow with ARR. Also stop the GM being a constant (see C2).

2. **abc_cost_distortion station utilizations > 1** (`archetypes/abc_cost_distortion.py:822–841`). `operations.stations` shows Cutting 2.21, Machining 1.18, Assembly 8.83, Inspection 13.96, Welding 10.69, Testing 13.41, Painting 28.92, Kitting 28.92 with `capacity_per_hour` 1.0–31 and `wip` 49.0 at every station. These are served in `CasePublic.operations` and will appear in the data room and in any textbook chapter that quotes "relevant public KPIs". Fix: compute λ per work centre from `production_log` minutes over available minutes as `toc_bottleneck` does, cap ρ < 1 for non-constraint stations, and populate `wip`/`cycle_time` from the log rather than a constant.

3. **Balance sheets missing lines the industry must have** (cross-cutting, see C1). Zero AP in every period for ma_synergies (233 heads), cohort_collapse, cohort_matrix, idempotent_webhooks, window_reconciliation; zero deferred revenue for window_reconciliation ("billed monthly in advance"), cohort_matrix and idempotent_webhooks; AR ≈ 0 (DSO 0.14–0.18 days) for cohort_collapse. A CFO opening the balance sheet pane will stop trusting the world in the first ten seconds, before reaching the anomaly.

4. **bullwhip ground-truth definition is an artefact** (`archetypes/bullwhip.py`, metrics `cv_company_po`, `amplification_tier3`). `derivation` says "0 in weeks without a PO" per SKU, which turns a MOQ-driven ordering cadence into variance. Observed: tier-3 amplification 113.1× (practitioner) / 73.6× (principal) while the company-level weekly PO series has CV 0.41 vs POS CV 0.118, i.e. ≈12×. Literature values are 2–10× per tier. Fix: define the metric on the aggregated weekly series per SKU *with the order-cycle smoothing stated*, or on company-level weekly totals, and set the expected value from that definition; keep the "zeros inflate variance" trap as a Socratic point, not as the ask.

### P1 — noticeably artificial

5. **Every company is `stage="growth"` with FYE 12-31** (28/28). `industries.json` carries a `stage_mix` prior that is never sampled. Fix in `base.py:376`: draw stage from `industry.stage_mix`, draw FYE from {12-31: 0.7, 06-30, 09-30, 03-31}, and let stage drive cash/revenue (seed stage: 12–24 months runway; mature: dividends/buybacks).

6. **Revenue per head is 2–3× too high in labour-heavy industries.** toc_bottleneck $388k/$515k (kitting), bullwhip $497k (distributor), replica_lag $550k, window_reconciliation principal $504k with 26 heads. Fix: `per_head` by industry from the corpus (`fin.<ind>.revenue_per_employee` if present, else 3PL/kitting ~$120–180k, distribution ~$300–400k, SaaS ~$180–250k).

7. **Machine-flat series.** cohort_matrix GM exactly 0.700×8; cohort_collapse GM 0.704–0.708; ma_synergies GM 0.780×6; abc revenue flat ±2% for 8 quarters on a "growth" company; abc cash a straight line +$0.9M/quarter. Fix: add seasonal and idiosyncratic noise in `finance` baseline (σ ≈ 0.8–1.5 pts on GM, ±4% on quarterly revenue, a capex or dividend event every 3–5 quarters).

8. **`resolved_if` cannot express a decrease** (all archetypes use floats meaning ≥). Module 3's `simulate_transition` accepts `"<=45"` and `{"op":"<=","value":45}`, but no archetype uses them, so working_capital_crunch resolves only via `inventory_liquidation_pct ≥ 20 AND early_pay_discount ≥ 1`; a learner who cuts `credit_terms_days` from 63 to 45 (the actual root cause) never resolves the anomaly drag. Same shape in pricing_elasticity (`price_cut_shopper_pct ≥ 10` only). Fix: express each archetype's root-cause lever with `<=` where the fix is a reduction, and allow an OR-group (any of N conditions) for alternative valid fixes.

9. **cohort_collapse funding.** 8 loss-making quarters (NI −$0.6M to −$1.6M), cash flat at $37–38M, CFF = 0, no equity line movement. Fix: either an equity raise in the warm-up quarter that explains the cash, or let cash decline with the burn.

10. **ma_synergies impairment magnitude and pre-deal cash.** Goodwill impairment exposure $66.7M = 75% of $89.2M goodwill in the second post-close quarter; pre-deal cash 5.9× quarterly revenue. Fix: scale the premium so the exposure lands at 20–50% of goodwill (still "critical"), and size pre-deal cash to ~2–3 quarters of revenue plus the deal consideration raised partly by the term debt that already appears.

11. **bullwhip P6 payables collapse.** AP $6.98M→$0.73M→$6.24M; "Supplier and carrier payments" $2.76M in Q1 then $1.37M in Q2. The shortage quarter should cut *receipts*, not pay down AP to near zero. Also DIO 11 days vs 30–60 benchmark for a consumer-goods distributor.

12. **replica_lag business model label.** "B2B flash-sale marketplace" with inventory on the balance sheet, COGS and 16–27% gross margin is a reseller/liquidator; call it that (or make the marketplace asset-light with take-rate revenue and no inventory).

13. **idempotent_webhooks margin inconsistency.** Same business model: GM 84% (practitioner) vs 75% (principal); operating margin oscillates 0.5%→8%→1.3% quarter to quarter at principal. Add an AP line (processor fees payable, vendor AP) and a receivable for merchant fees in arrears.

14. **window_reconciliation asks.** 19–20 expected metrics per case; a learner must fill 20 boxes. Keep 6–8 asks (closing GL AR, deduped sub-ledger AR, unexplained difference, duplicates $, unapplied cash $, suspense balance, one aging bucket) and move the rest to `checks` on the reference queries.

15. **Pricing reveal contradiction.** "Revenue is roughly flat (−9.5% 2023Q3 vs 2022Q4)". Also the practitioner reveal already states units −18%, competitor −5%, and sales asking for discount authority; reserve two of those for principal's red-herring timeline instead.

16. **forensic_revenue baseline DSO 15–18 days** for wholesale consumer goods/industrial components (benchmark 35–50). The author chose it so DSRI can move; better: baseline 35, anomaly moves it to 55, and DSRI still clears 1.2.

17. **monte_carlo_cash gross margin volatility.** 11.5%→25.4%→18.7%→16.7% in consecutive quarters for a brokerage; cap quarter-to-quarter GM change at ±3 pts unless an event explains it.

### P2 — polish

18. **Company description casing** (`base.py` / archetype params): "Dtc brand with a fast-growing wholesale / b2b channel" — a `.capitalize()` lowercases the acronyms. Use `description[0].upper() + description[1:]`.
19. **Name pools collide.** Quarry Co.'s customer list contains "Quarry Components"; Halcyon Outfitters has customers "Halcyon Retail". Exclude the company stem from customer/supplier name generation.
20. **`interest_coverage = 999.0` sentinel** in `statements.ratios` (finance.py:814) reaches the public view and will render as "999.0×". Emit `null` when interest expense is 0.
21. **Ask units in cents.** The live screenshot shows asks "Working-capital cash drag ... unit: cents" expecting an input like 852,487,846. Accept dollars in the UI and convert, or declare `unit: "usd"` with a `×100` on the server for `_cents` keys.
22. **Founded years vs business model.** Usage-API platform founded 1999 (outbox), B2B flash-sale marketplace founded 1999 (replica_lag), AI SaaS founded 2001/2002 (cohort). Bound `founded` by business model era.
23. **`unit_economics.cac = 0`, `payback_months = 0`** in cohort_matrix; **`queueing = {"throughput": 1.0}`** in abc; `stations = []` in ma/monte_carlo/window (fine) but `cohorts = []` where the archetype is about cohorts should never happen (cohort_matrix has none in `OperationsState.cohorts`).
24. **cohort_matrix reveal names the promo month** ("A LAUNCH50 promotion ran in 2022-11") while conceal hides "the 2022-11 promo cohort collapses"; at practitioner, reveal only that a promotion ran "last autumn" and let the triangle find the month.
25. **Concealing generic vocabulary.** working_capital_crunch conceals the phrase "cash conversion cycle"; the dossier writer will contort around a term any CFO uses. Conceal the *finding* ("CCC lengthened 14 days") not the term.
26. **`architecture_mermaid` absent for finance archetypes** is by design; a process-flow diagram exists for toc/bullwhip (good). Consider one for working_capital (order-to-cash) and forensic (quote-to-cash with the hold step omitted).
27. **Period labels start at a warm-up quarter in some reference outputs** (`dso_by_quarter_subledger` returns 2021Q3 which is before `world.periods[0]` = 2021Q4) — label it as prior-period or filter.

## (c) Cross-cutting issues in `finance.py` / `datagen.py` / `base.py`

- **C1. Baseline ledger omits AP and deferred revenue for non-inventory templates.** The event families exist (`purchase_inventory`, `accrue_opex`, `pay_ap`, `recognize_deferred`) but SaaS/fintech archetypes book opex straight to cash or to "Accrued liabilities" and bill-in-advance revenue straight to revenue. Add to `base.build_baseline` defaults: vendor AP for 30–45% of opex with DPO drawn from the corpus, deferred revenue from annual/monthly-in-advance billing with the corpus `saas.deferred_revenue_pct_of_arr`, and a processor-fee payable for fintech. Every balance sheet should carry the lines the industry's 10-K captions list in `line_items.json`; add an invariant "no caption in `line_items[balance_sheet]` with p50 share > 2% of assets is zero in every period" so this cannot regress.
- **C2. No noise model in the baseline.** `finance` applies growth paths and ratios deterministically per quarter; archetypes that do not add their own noise produce constant margins. Give `Ledger`/baseline a per-period multiplicative noise draw (seeded sub-stream) on volume, price, COGS ratio and opex, plus a seasonal profile per industry (retail/ecommerce Q4, manufacturing Q1 dip, SaaS Q4 bookings).
- **C3. Simulation contract under-used.** `resolved_if` ≥-only in all 14 archetypes although Module 3 supports `<=`; no archetype uses `shocks` or `bounds`, so second-order effects (bottleneck migration, elasticity response) exist only as linear `delta_per_unit` terms. The toc model, for instance, has `capacity_add_bottleneck_pct → throughput +33.3 units per %` with no cap at the next station's capacity (`b2_utilization` rises 0.0078 per % but nothing stops throughput at ρ=1).
- **C4. Company profile generation** (`base.py:360–380`): `stage` and `fiscal_year_end` are constants, `headcount` floor 12, `founded` unbounded by model, description lowercased. One function, one afternoon.
- **C5. Ratios sentinel and NaN policy** (`finance.py:814 _clip`): clip to 999 is a UI leak; return `None` and let the frontend show "n/m".
- **C6. `datagen.tz_mixed` drops fractional seconds** (noted by the replica_lag author); document or preserve microseconds.
- **C7. `operations.sbg_fit` is unbounded** (noted by the cohort_collapse author); bound log-α, log-β or use L-BFGS-B so the ground truth never carries NaN.
- **C8. Dirt is thin and uniform.** Typical world: 3–5 rules, one per table (`nulls:email:0.08`, `case_mixed:region`, `tz_mixed:order_ts`, `dup_keys:0.02`). Real exports have unit mismatches, orphan FKs, late-arriving rows and format-mixed dates together on the same table. Raise principal to 2–3 rules per transactional table and always include `orphan_fk` and `unit_mixed` somewhere in the reconciliation path.

## (d) Prompt improvements (concrete edits to `pedagogy.py`)

- **case_synthesizer.** Add rule 8: "At practitioner and principal, do not quote the four-quarter trajectory of any metric that is itself an ask or an anomaly symptom; quote the latest value only." (The working-capital reveal already carries the q/q growth path; the pricing reveal carries units −18% and competitor −5%.) Add: "Stakeholder artifacts must disagree with each other on at least one fact that the data room can settle" — this is what makes HBS cases work and nothing currently forces it. Replace "900–1400 words of prose" with a per-section budget so the situation does not eat the stakeholder voices.
- **textbook_chapter.** Good structure. Add: "Derive the formula from a conservation law or a definition, then show one limiting case where it breaks (e.g. Little's Law with non-stationary arrivals; DSO with lumpy billing)." Add: "For SQL snippets, show the wrong version and the right version of one query and name the error (fan-out, timezone, censoring)." Require `self_check` question 3 to be numeric with the case's numbers so the answer is checkable.
- **grader.** The anchors are strong. Two gaps: (1) nothing asks the grader to check that `sql_evidence` actually supports the asserted metrics (a learner can paste any query); add "For each asserted metric, name the evidence item that produces it or mark it 'unsupported'". (2) `misdiagnosis` is binary; add `partial_diagnosis: true` when the area is right but the mechanism is wrong, and have the BKT mapping treat it as borderline instead of a miss.
- **adversary.** Add persona memory: "Do not repeat a challenge the analyst already answered with evidence; reference their evidence by name." Add "At rung 3, cite one number from the public KPIs that superficially contradicts the analyst's thesis" so counter-evidence is concrete. The `MAX RUNG` mechanism is good; make the persona *lose* visibly when conceding ("I was wrong about seasonality") because that is the pedagogical payoff.
- **quarter_narrator.** Fine. Add: "State explicitly which decision had no measurable effect this quarter" — the simulation produces exactly that information (lag_periods) and it is the most instructive part of a quarter advance.
- **All prompts.** "NUMERIC DISCIPLINE" is repeated verbatim five times; fine for the inspector, but add one line to each: "If the data contains both a cents and a dollars figure for the same quantity, use dollars with the unit." The asks and KPIs are in cents and the LLM will otherwise write "852,487,846 USD".

## Counts

P0: 4 · P1: 13 · P2: 10 · cross-cutting: 8 · prompt edits: 6
