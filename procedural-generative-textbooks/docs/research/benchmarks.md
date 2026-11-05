# Axiom Benchmark Corpus — Research Report

**Scope.** This report documents the source-cited benchmark corpus that the deterministic world generator draws every number from. The corpus lives in `backend/axiom/engine/benchmarks/` as six JSON files: `industries.json`, `saas_metrics.json`, `operations.json`, `financial_ratios.json`, `line_items.json`, and `schema_templates.json`. It is the "research tier" described in `docs/ARCHITECTURE.md` §1: it never generates narrative and never calls an LLM. Its only job is to make sure that when the generator samples a gross margin, a lead time, a churn rate, or a chargeback ratio, the value lands inside a distribution that a practitioner would recognise.

Generated 2026-11-05. Default vintage 2024; each benchmark carries its own `as_of`.

## 1. Methodology

### 1.1 What a benchmark is

Every benchmark record carries five cross-sectional percentiles (p10, p25, p50, p75, p90), a unit, a recommended sampling family, a citation, a URL, a vintage, and free-text notes. The percentiles describe dispersion **across companies** (or across shipments, plants, or cohorts where the metric is operational), not variation over time within one company. The generator is expected to:

1. Pick a point on the distribution with the seeded RNG (uniform quantile → piecewise-linear interpolation across the five anchors, or the named parametric family fitted to the anchors).
2. Treat the drawn value as the company's **structural** level.
3. Add period-to-period noise and archetype-driven drift on top.

`distribution: "empirical"` (the default) means interpolate across the anchors and clamp the tails. `lognormal` flags right-skewed quantities (lead times, time to second purchase, replica lag, AOV) where the generator should fit a lognormal to the median and the p25/p75 spread. `normal` is used for the Beneish indices. `triangular` is used for parcel transit where the support is small and bounded.

### 1.2 Source hierarchy

Sources were ranked and used in this order:

1. **Primary published datasets with disclosed methodology**: Damodaran's NYU Stern sector tables (margins, working capital, capex, tax rates, interest coverage; January 2026 update), SaaS Capital's annual retention survey (~1,500 private B2B respondents), the KeyBanc/Sapphire private SaaS survey, Sea-Intelligence's Global Liner Performance monthly releases, the Federal Reserve charge-off series, NRF's returns and shrink surveys.
2. **Peer-reviewed research** for structural parameters: Cachon, Randall & Schmidt (2007) and Lee, Padmanabhan & Whang (1997) for the bullwhip effect; Bray & Mendelson (2012) for firm-level bullwhip; Bijmolt, van Heerde & Pieters (2005) and Tellis (1988) for price elasticity; Beneish (1999) and Sloan (1996) for forensic accounting indices.
3. **Operator benchmark panels** with large samples but undisclosed selection: ChartMogul (churn by ARPA band), Recurly Research (churn by B2B/B2C), Benchmarkit (CAC payback by ACV), Bessemer Venture Partners (opex mix by ARR stage), Armstrong & Associates (3PL margins), WERC DC Measures (warehouse KPIs), Little Stream Software (Shopify repeat rates), Loop Returns (apparel return rates), Sift (chargeback rates).
4. **Published price lists and rules** that fix a number by construction: Stripe's 2.9% + $0.30 pricing, Walmart's OTIF supplier targets, Visa's VAMP thresholds, the 21% US federal statutory rate, FMCSA hours-of-service limits.
5. **Estimates**, explicitly labelled `source: "estimate; see notes"`, used only where no public distribution exists (support-ticket volumes, replica lag, warehouse lines per hour, marketplace take rates). 81 of 261 benchmarks are estimates. Every estimate's note explains the reasoning so a reviewer can replace it.

### 1.3 How percentiles were derived when only a median or mean was published

Most practitioner sources publish a median, sometimes a quartile pair, rarely a p10/p90. The rule used:

- If quartiles were published, p10/p90 were extrapolated by extending the interquartile range by roughly 0.8× on each side (a normal-ish assumption) and then sanity-checked against the metric's physical bounds (e.g. retention cannot exceed 1.0).
- If only a median was published, the spread was borrowed from the most similar metric with published quartiles (e.g. SaaS Capital's GRR quartiles were used to set the spread on derived monthly logo churn).
- Where two credible sources disagreed (3PL VAWD net margin 2.9% vs 7%; private SaaS CAC payback 16 vs 18 months depending on report year), the median was set at the midpoint and the disagreement is recorded in `notes`.
- Where a figure is an aggregate dominated by large caps (Damodaran software gross margin 71.72%, public retail inventory turns 13.79), the private-company distribution was shifted down and widened, and the note says so.

### 1.4 Verification caveats

Direct downloads from several primary sites were blocked by the session's network proxy, so figures were verified through search summaries that quote the source pages. Where I could not reach the underlying table, the note says "author's reading" or "author's recollection; verify". The Beneish (1999) group means and the Sloan (1996) decile means fall into this category: the values recorded match the published papers to my knowledge, but the generator author should confirm them against Table 2 of Beneish and Table 1 of Sloan before quoting them in a textbook chapter.

## 2. Sources and what was taken from each

| Source | URL | Used for |
|---|---|---|
| Damodaran, Margins by Sector (US), Jan 2026 | https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/margin.html | Software gross 71.72%, EBITDA 35.93%, net 25.49%; Retail (General) gross 33.18%, pre-tax operating 6.50%, net 5.61%; Retail (Grocery) operating 1.85%, net 1.32%; Retail (Distributors) EBITDA 11.4%. Anchors for `fin.saas.*`, `fin.retail.*`, `fin.manufacturing.*` margins. |
| Damodaran, Margins by Sector, Jan 2023 | same page (archived edition) | Retail (Online): 63 firms, gross 42.78%, net 0.64%. Anchor for `fin.ecommerce.gross_margin` and `operating_margin`. |
| Damodaran, Working Capital by Sector, Jan 2026 | https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/wcdata.html | AR/Sales, Inventory/Sales, AP/Sales ratios (e.g. Aerospace/Defense 23.93%/28.94%/11.12%; Air Transport 4.00%/3.06%/6.64%). DSO/DPO/DIO derived as ratio × 365. |
| Damodaran, Capex, Tax Rates, Ratings/Interest Coverage, Jan 2026 | https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/capex.html, .../taxrate.html, .../ratings.html | Capex % revenue, effective tax rate, interest coverage and debt/EBITDA by sector. |
| SaaS Capital, 2025 B2B SaaS Retention Benchmarks | https://www.saas-capital.com/wp-content/uploads/2025/09/RB32WS1-2025-B2B-SaaS-Retention-Benchmarks.pdf | Median NRR 101%, GRR 91%; GRR 95% for ACV >$250K vs ~91% below; NRR 102% for ACV $25-50K. |
| SaaS Capital, 2023 Retention Benchmarks | https://www.saas-capital.com/wp-content/uploads/2023/05/RB28WS1-2023-B2B-SaaS-Retention-Benchmarks.pdf | GRR ~93% for ACV >$25K vs 90% below. |
| KeyBanc / Sapphire, 2024 Private SaaS Survey | https://info.sapphireventures.com/2024-keybanc-capital-markets-and-sapphire-ventures-saas-survey | Expected ARR growth ~19%; GRR ~90%, NRR ~101%; pivot to efficiency. 2022 median CAC payback ~23 months (press release). |
| Benchmarkit, 2025/2026 SaaS Metrics Benchmarks | https://www.benchmarkit.ai/ | Median CAC payback 18 months (CY2024), 16 months (CY2025); 11 months for ACV <$5K, 22 months for $50-100K; magic number median reported ~1.37 (2025, secondary). |
| ChartMogul, Churn Cheat Sheet / Benchmarks | https://chartmogul.com/resources/churn-cheat-sheet | Monthly gross MRR churn 8.9% (ARPA $0-10) to 2.5% (>$500); customer churn 6.8% to 2.2%; ARPA bands. |
| Recurly Research, Churn Rate Benchmarks | https://recurly.com/research/churn-rate-benchmarks/ | Cross-industry monthly churn 3.27% (2.41% voluntary + 0.86% involuntary); B2C consumer goods ~6.5%; ecommerce subscription annual median 4.25%. |
| Bessemer, Scaling to $100M (updated 2024) | https://www.bvp.com/assets/uploads/2021/09/Scaling-to-100-Million-by-Mary-DOnofrio_updated2024.pdf | S&M 95% of revenue at $1-10M ARR → 50% at $100M; R&D 35%, G&A 20% at $100M; gross margin target 65-70%; good/better/best growth by stage. |
| Chargebee / OpenView pricing surveys (via Monetizely summary) | https://www.getmonetizely.com/faqs/... | Annual prepay discount median 10-20%, modal ~17% ("two months free"); 60-70% of SaaS companies offer annual prepay. |
| Cachon, Randall & Schmidt (2007), M&SOM 9(4) | https://pubsonline.informs.org/doi/10.1287/msom.1060.0149 | Bullwhip definition Var(orders)/Var(sales); present at wholesale, absent at retail and most manufacturing at industry-aggregate level. |
| Lee, Padmanabhan & Whang (1997), Management Science 43(4) | https://doi.org/10.1287/mnsc.43.4.546 | Variance amplification upstream; four causes. |
| Bray & Mendelson (2012), Management Science 58(5) | https://doi.org/10.1287/mnsc.1110.1467 | ~2/3 of firms bullwhip at firm level. |
| Bijmolt, van Heerde & Pieters (2005), JMR 42(2) | https://doi.org/10.1509/jmkr.42.2.141.62296 | Mean brand price elasticity −2.62 across 1,851 estimates. |
| Tellis (1988), JMR 25(4) | https://doi.org/10.1177/002224378802500401 | Mean −1.76; durables more elastic than non-durables. |
| Beneish (1999), Financial Analysts Journal 55(5) | https://doi.org/10.2469/faj.v55.n5.2296 | M-score coefficients, −1.78 cutoff, 76%/17.5% classification rates; index group means (recollection). |
| Sloan (1996), The Accounting Review 71(3) | https://www.jstor.org/stable/248290 | Accrual definition; decile structure (recollection of magnitudes). |
| WERC DC Measures 2024 (Yale summary) and 2018 (Honeywell summary) | https://www.yale.com/globalassets/.../yale-1520_werc-dc-metrics-infographic_072924.pdf; https://sps.honeywell.com/us/en/support/blog/productivity/which-metrics-matter-most-to-dc-operations | Picking accuracy best-in-class >99.85% (2024) / median 99.30% (2018); on-time shipments median 98.20%, BIC ≥99.8%; dock-to-stock BIC <3 h. |
| Sea-Intelligence Global Liner Performance, 2024 monthly | https://sea-intelligence.com/press-room/307-2024-schedule-reliability-largely-within-50-55 | Schedule reliability 50-55%; late-arrival delay 4.74-5.72 days. |
| Flexport transit times; forwarder guides | https://www.flexport.com/rates/ | Shanghai-LA port-to-port 13-17 days; door-to-door 28-38; air 4-7. |
| Mothership LTL guide; C.H. Robinson transit insights | https://www.chrobinson.com/en-us/resources/blog/key-insights-to-improve-ltl-transit-times/ | LTL 1-3 days <500 mi, 5-7 days >2,000 mi; +1 to +3 day buffer for 91% on-time. |
| FMCSA hours-of-service summary | https://www.fmcsa.dot.gov/regulations/hours-service/summary-hours-service-regulations | 11-hour driving limit → ~550 mi/day FTL planning. |
| CSIMarket efficiency tables Q1 2024 (via Netstock / Allianz Trade) | https://csimarket.com/Industry/industry_Efficiency.php | Inventory turns: grocery 18.56, public retail 13.79, apparel 3-4, capital goods 2.44, conglomerates 3.71. |
| Walmart OTIF program (SPS Commerce; RetailWire Feb 2024) | https://www.spscommerce.com/community/articles/walmart-otif-metrics | 98% on-time / 95% in-full historic; 90/95 from 2024; 3% penalty. |
| OEE compilations (Vorne, Evocon, Godlan 2024 study of ~1,470 operations) | https://reliamag.com/guides/oee-benchmarks-by-industry-what-world-class-really-means/ | World-class 85% = 90% × 95% × 99%; typical 55-67%; 3-6% of plants ≥85%. |
| SMED literature (Shingo; vendor summaries) | https://en.wikipedia.org/wiki/Single-minute_exchange_of_die | Changeover baselines by process; <10 min target. |
| Cost-of-poor-quality compilations (FMA, APQC) | https://reliamag.com/topics/articles/cost-of-poor-quality-scrap-rework-returns | Scrap+rework 1.4-2.2% of sales; <5% piece scrap acceptable. |
| Little Stream Software 2024 Shopify analysis (via Eightx) | https://eightx.co/blog/average-ecommerce-repeat-purchase-rate-by-vertical-2026 | Repeat purchase rate avg 25%, median 22.5%, 1.8 orders/customer. |
| DTC 90-day retention guides (TopGrowth, Eightx, Klaviyo) | https://topgrowthmarketing.com/ecommerce-customer-retention-90-day-guide/ | 90-day repeat by category; time to second purchase. |
| Loop Returns via Eightx | https://eightx.co/blog/average-apparel-return-rate-benchmarks | Apparel return rate 23.2% (>4,000 merchants, Jan-Oct 2024). |
| NRF 2024 Consumer Returns; NRF 2023 Retail Security Survey | https://nrf.com/research/2024-consumer-returns-retail-industry; https://nrf.com/research/national-retail-security-survey-2023 | Total returns ~16.9% of sales; shrink 1.6%. |
| Baymard Institute | https://baymard.com/lists/cart-abandonment-rate | Cart abandonment ~70%. |
| Armstrong & Associates 3PL results | https://www.3plogistics.com/ | DTM net-revenue margin 16.6% (2019, 2022), 14.2% (2021); EBIT 8.6% / NI 5.4% of net revenue (2006); VAWD NI 2.9-7%. |
| Sift / Chargeback.io | https://www.chargeback.io/blog/average-e-commerce-chargeback-rate | Chargeback rate 0.26% all-industry (Q3 2025); SaaS 0.66%; Visa VAMP 1.5% (Apr 2026), legacy 0.9%; Mastercard 1.5%. |
| Federal Reserve charge-off rates | https://www.federalreserve.gov/releases/chargeoff/chgallsa.htm | Credit-card charge-offs ~4.0-4.6% (2024). |
| Stripe pricing | https://stripe.com/pricing | 2.9% + $0.30; T+2 payouts. |
| IRS / TCJA | https://www.irs.gov/newsroom/tax-cuts-and-jobs-act-a-comparison-for-businesses | 21% statutory rate. |
| Real-world schemas: Stripe API, Shopify Admin API, SAP PP/CO, Odoo MRP/stock, NetSuite GL, Saleor, Medusa, Debezium outbox, EDI 214 | vendor documentation | Table and column design in `schema_templates.json`. |

## 3. Benchmark key index

Keys are namespaced `saas.*`, `ops.*`, `fin.<industry>.*`, `forensic.*`. Full definitions, percentiles and notes are in the JSON files; this table lists what exists.

| Namespace | Keys (count) |
|---|---|
| `saas.` (38) | gross_logo_churn_monthly.{smb,midmarket,enterprise}; gross_mrr_churn_monthly.{arpa_0_10,arpa_500_plus}; gross_revenue_retention_annual; net_revenue_retention_annual; gross_margin; operating_margin; ebitda_margin; cac_payback_months{,.smb,.enterprise}; ltv_to_cac; sales_marketing_pct_revenue{,.early,.scale}; rnd_pct_revenue; ga_pct_revenue; arpa_monthly_usd.{smb,midmarket,enterprise}; magic_number; rule_of_40; arr_growth_yoy{,.early}; annual_prepay_discount; annual_prepay_share_of_contracts; dso_days; deferred_revenue_pct_revenue; involuntary_churn_share; expansion_share_of_new_arr; trial_to_paid_conversion; burn_multiple; contract_length_months; headcount_per_1m_arr; support_tickets_per_100_customers_month; services_share_of_revenue |
| `ops.lead_time_*` (9) | parcel_ground, ltl, ftl, ocean_transpacific_port_to_port, ocean_door_to_door, air_freight; cv.{parcel,ltl,ocean} |
| `ops.ocean_*` (2) | schedule_reliability, late_arrival_delay_days |
| `ops.warehouse.` (6) | order_picking_accuracy, on_time_shipment, dock_to_stock_hours, lines_picked_per_labor_hour, order_cycle_time_hours, capacity_utilization |
| `ops.inventory_turns.` (6) | grocery, retail_general, apparel, capital_goods, ecommerce_dtc, distribution_wholesale |
| `ops.` supply chain (7) | fill_rate.line, otif.cpg_to_big_box, safety_stock_service_level, bullwhip_ratio.{wholesale,retail,manufacturing_firm_level}, bullwhip_amplification_per_tier |
| `ops.` manufacturing (13) | oee.{discrete_manufacturing,availability,performance,quality}, bottleneck_utilization, non_bottleneck_utilization, scrap_rate, first_pass_yield, changeover_minutes.{stamping,injection_molding,generic}, manufacturing_lead_time_days |
| `ops.price_elasticity.` (5) | overall, grocery_cpg, apparel, durables, software_b2b |
| `ops.retention.` (9) | subscription_b2c.{m1,m3,m6,m12}, subscription_b2b.m12, ecommerce_dtc.repeat_rate_12m, ecommerce_dtc.repeat_90d.{apparel,consumables,home_durables} |
| `ops.ecommerce.` (8) | orders_per_customer_year, time_to_second_purchase_days, return_rate.{apparel,general}, cart_abandonment, conversion_rate, aov_usd, discount_rate_share_of_gross |
| `ops.3pl.` (5) | gross_margin_on_gross_revenue, ebit_margin_on_net_revenue, vawd_net_income_margin, cost_per_order_fulfillment_usd, claims_rate |
| `ops.fintech.` (7) | chargeback_rate, dispute_win_rate, payment_take_rate, marketplace_take_rate, loan_loss_rate_consumer_unsecured, settlement_lag_days, payment_success_rate |
| `ops.system.` (5) | webhook_delivery_success_first_attempt, duplicate_delivery_rate, replica_lag_ms_p50, replica_lag_ms_p99, api_error_rate |
| `fin.saas.` (19), `fin.ecommerce.` (20), `fin.manufacturing.` (20), `fin.3pl.` (18), `fin.retail.` (20), `fin.fintech_marketplace.` (20) | gross/operating/EBITDA margin, DSO, DPO, DIO, current & quick ratio, debt/EBITDA, interest coverage, capex, SG&A, R&D (where relevant), effective tax rate, Sloan accrual ratio, allowance % AR, revenue growth, plus industry-specific items (deferred revenue, return rate, shrink, overhead % COGS, purchased transportation, take rate, customer funds, loan-loss provision, sales per sq ft) |
| `fin.any.` (6) | us_federal_statutory_tax_rate, revenue_growth_yoy.{seed_stage,growth_stage,mature}, dividend_payout_ratio.mature |
| `forensic.` (20) | beneish.{dsri,gmi,aqi,sgi,depi,sgai,lvgi,tata}.{non_manipulator,manipulator}, beneish.m_score.{non_manipulator,manipulator}, sloan.accruals_decile_{low,high} |

Total: **261 benchmarks**, 180 sourced to a named publication or price list, 81 labelled estimates.

## 4. Structural relationships the generator must respect

Sampling each benchmark independently would produce companies that cannot exist. The following constraints are encoded in the `meta.notes` of each file and summarised here so `procedural_world.py` can enforce them.

**SaaS.**
- Churn is a decreasing function of ARPA/ACV. Draw the segment first, then churn from the segment-specific key. Never draw `saas.gross_logo_churn_monthly.enterprise` for a company with SMB ARPA.
- GRR is bounded above by 1.0 and NRR = GRR + expansion. Draw GRR, then draw expansion share, then derive NRR; do not sample NRR independently.
- CAC payback rises with ACV. LTV/CAC is arithmetic: `LTV = ARPA × GM / churn`, `CAC = payback_months × ARPA × GM`. Derive it; the `saas.ltv_to_cac` benchmark exists only as a sanity range.
- Deferred revenue ≈ annual-prepay share × ~0.5 of annual subscription revenue. DSO rises with invoice-based annual billing and falls with card self-serve.
- S&M, R&D and G&A percentages are stage-dependent; use the `.early` / `.scale` variants when the archetype fixes the stage, and make sure gross margin − (S&M + R&D + G&A) reproduces the sampled operating margin within a few points (adjust G&A as the plug).

**Operations.**
- Lead times are right-skewed. Sample lognormal with the median from `ops.lead_time_days.*` and the CV from `ops.lead_time_cv.*`. Ocean lead time = port-to-port + origin/destination handling; door-to-door should not be sampled independently of port-to-port.
- OEE = availability × performance × quality. Sample the three components and multiply; do not sample `ops.oee.discrete_manufacturing` directly except as a check.
- Bullwhip compounds across tiers: retail ratio ≈ 1.0, wholesale 1.2-2.2, manufacturer 1.3-3.0+. Use `ops.bullwhip_amplification_per_tier` to generate tier-by-tier order variance from a retail demand series.
- Inventory turns and DIO are the same quantity: `DIO = 365 / turns` on a COGS basis. Keep `ops.inventory_turns.*` and `fin.<industry>.dio_days` consistent by deriving one from the other.
- First-pass yield ≈ 1 − scrap rate − rework rate; the OEE quality component should match.
- Price elasticity is negative. Elasticities in `ops.price_elasticity.*` are brand-level own-price elasticities; category-level elasticities are roughly a third as large in magnitude.

**Financial statements.**
- Margins nest: gross ≥ operating ≥ net. EBITDA = operating + D&A.
- CCC = DSO + DIO − DPO; compute it, never sample it.
- Current ratio and quick ratio must be consistent with the sampled DSO/DIO/DPO and cash balance; after building the balance sheet, compare the implied ratio to the benchmark range and re-scale cash if it falls outside p10-p90.
- `line_items.json` balance-sheet magnitudes are typical shares, not a closed system: rescale each side to sum to total assets.
- The Sloan accrual ratio and Beneish indices are **outputs** of the generated statements for a clean company and should land near the non-manipulator means; the forensic archetypes then push specific indices toward the manipulator means (DSRI, SGI, TATA above all) and the generator should verify the resulting M-score crosses −1.78 when the archetype intends it to.

**Fintech.**
- Funds held for customers appear as both an asset (restricted cash) and a liability (funds payable) of equal size.
- Chargeback rate × transaction count must be consistent with the row count of the `disputes` table; the dispute reserve on the balance sheet should be chargeback rate × ~60-120 days of volume × (1 − win rate).

## 5. Known gaps and figures not sourced

- **WERC 2024 medians**: only best-in-class values were public for 2024; medians come from the 2018 report. The warehouse KPI spreads are therefore wider than they would be with the current paid report.
- **Bullwhip ratio magnitudes**: Cachon et al. establish presence/absence, not a published percentile distribution of ratios; the p10-p90 values are informed by the paper's framework and the beer-game literature, not lifted from a table.
- **Beneish and Sloan descriptive statistics**: recorded from memory of the papers; flagged in every affected note.
- **Damodaran manufacturing and trucking rows**: the exact January 2026 values for Machinery, Trucking, and Transportation were not retrievable through the proxy; the recorded ranges are the author's reading of recent editions and are labelled as such.
- **3PL cost per order, support ticket rates, replica lag, webhook duplicate rates, marketplace take rates, trial conversion, headcount per $1M ARR**: no public percentile distributions exist; estimates are provided for table sizing and SLO calibration and are labelled.
- **Industry-specific allowance for doubtful accounts**: only vendor-blog ranges and a handful of 10-K examples were available; the corpus uses the vendor ranges with public-company examples as sanity checks.
- **Damodaran "Retail (Online)"** was discontinued after January 2023; the ecommerce margins are anchored on that last edition plus operator benchmark reports.

## 6. Refresh guidance

The research tier is designed to run offline. An optional keyless refresh can pull current sector aggregates from the SEC EDGAR XBRL frames API (`https://data.sec.gov/api/xbrl/frames/us-gaap/<Concept>/USD/CY<year>.json`, which requires a descriptive `User-Agent`) and recompute the `fin.<industry>.*` percentiles from filer-level data, replacing the Damodaran anchors with same-vintage EDGAR values. Everything else in the corpus should be refreshed annually by hand against the sources in §2, bumping `as_of` and `meta.generated`.
