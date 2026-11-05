# Archetype Common Mechanics — what `finance.py`, `operations.py` and `datagen.py` share

This document fixes the conventions every archetype in `docs/research/archetypes.md` relies on.
Formulas are normative. Where this document and an archetype section disagree, the archetype
section wins for that archetype only.

## 1. Time conventions

| Item | Rule |
|---|---|
| Periods | 8 fiscal quarters, labelled `YYYYQn`. `periods[-1]` is the "current" quarter whose books just closed. |
| Anchor | The anchor (last period end) is derived from the seed so cases don't all share a calendar: `anchor_year = 2023 + (seed % 3)`, `anchor_quarter = 1 + (seed // 3) % 4`. Fiscal year end = Dec unless the industry profile says otherwise (retail archetypes may use Jan/Sep). |
| Table granularity | Monthly. 24 months of transactional rows. `period_of(date)` maps a date to its quarter label. |
| Simulation | `advance_quarter` appends a 9th, 10th… period; the transition model in each archetype defines KPIs and statements for the new period. |
| Dates in tables | Stored as ISO `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS[+offset]` unless a dirt rule (`format_mixed`, `tz_mixed`) applies. Rows are generated in event order inside each month; the datagen RNG stream decides intra-month timestamps. |

## 2. Canonical chart of accounts

All archetypes use this chart. An archetype may leave accounts at zero but never renumbers them.
`normal` is the normal balance; `section` is the statement roll-up key.

| Code | Name | Type | Normal | Stmt | Section |
|---|---|---|---|---|---|
| 1000 | Cash and cash equivalents | asset | debit | BS | current_assets |
| 1100 | Accounts receivable, gross | asset | debit | BS | current_assets |
| 1110 | Allowance for doubtful accounts | contra_asset | credit | BS | current_assets |
| 1150 | Unbilled receivables (contract assets) | asset | debit | BS | current_assets |
| 1200 | Inventory — raw materials | asset | debit | BS | current_assets |
| 1210 | Inventory — work in process | asset | debit | BS | current_assets |
| 1220 | Inventory — finished goods / merchandise | asset | debit | BS | current_assets |
| 1230 | Inventory reserve (obsolescence / shrink) | contra_asset | credit | BS | current_assets |
| 1300 | Prepaid expenses | asset | debit | BS | current_assets |
| 1350 | Deferred contract costs (capitalized commissions) | asset | debit | BS | current_assets |
| 1400 | Other current assets | asset | debit | BS | current_assets |
| 1500 | Property, plant and equipment, gross | asset | debit | BS | ppe |
| 1510 | Accumulated depreciation | contra_asset | credit | BS | ppe |
| 1600 | Capitalized software, gross | asset | debit | BS | intangibles |
| 1610 | Accumulated amortization — software | contra_asset | credit | BS | intangibles |
| 1700 | Goodwill | asset | debit | BS | intangibles |
| 1710 | Acquired intangibles, gross | asset | debit | BS | intangibles |
| 1720 | Accumulated amortization — intangibles | contra_asset | credit | BS | intangibles |
| 1800 | Deposits and other non-current assets | asset | debit | BS | other_noncurrent_assets |
| 2000 | Accounts payable | liability | credit | BS | current_liabilities |
| 2100 | Accrued expenses | liability | credit | BS | current_liabilities |
| 2110 | Accrued payroll and benefits | liability | credit | BS | current_liabilities |
| 2150 | Accrued interest | liability | credit | BS | current_liabilities |
| 2200 | Deferred revenue, current | liability | credit | BS | current_liabilities |
| 2210 | Deferred revenue, non-current | liability | credit | BS | noncurrent_liabilities |
| 2300 | Customer deposits | liability | credit | BS | current_liabilities |
| 2350 | Sales tax payable | liability | credit | BS | current_liabilities |
| 2400 | Income taxes payable | liability | credit | BS | current_liabilities |
| 2450 | Refund liability (returns reserve) | liability | credit | BS | current_liabilities |
| 2500 | Revolving line of credit | liability | credit | BS | current_liabilities |
| 2600 | Term debt — current portion | liability | credit | BS | current_liabilities |
| 2610 | Term debt — non-current | liability | credit | BS | noncurrent_liabilities |
| 2700 | Lease liabilities | liability | credit | BS | noncurrent_liabilities |
| 3000 | Common stock and APIC | equity | credit | BS | equity |
| 3100 | Retained earnings | equity | credit | BS | equity |
| 3200 | Distributions / dividends | equity | debit | BS | equity |
| 4000 | Product / subscription revenue | revenue | credit | IS | revenue |
| 4010 | Services revenue | revenue | credit | IS | revenue |
| 4020 | Usage / transaction revenue | revenue | credit | IS | revenue |
| 4100 | Sales returns and allowances | contra_revenue | debit | IS | revenue |
| 4110 | Discounts and rebates | contra_revenue | debit | IS | revenue |
| 4200 | Other income | revenue | credit | IS | other_income |
| 5000 | COGS — materials / merchandise / hosting | expense | debit | IS | cogs |
| 5010 | COGS — direct labor | expense | debit | IS | cogs |
| 5020 | COGS — overhead (manufacturing / infrastructure) | expense | debit | IS | cogs |
| 5030 | COGS — freight and fulfillment | expense | debit | IS | cogs |
| 5040 | Inventory write-downs and shrink | expense | debit | IS | cogs |
| 5050 | Payment processing fees | expense | debit | IS | cogs |
| 6000 | Sales and marketing | expense | debit | IS | opex |
| 6010 | Commissions expense | expense | debit | IS | opex |
| 6100 | Research and development | expense | debit | IS | opex |
| 6200 | General and administrative | expense | debit | IS | opex |
| 6210 | Bad debt expense | expense | debit | IS | opex |
| 6300 | Depreciation | expense | debit | IS | opex |
| 6310 | Amortization | expense | debit | IS | opex |
| 6400 | Occupancy and rent | expense | debit | IS | opex |
| 6500 | Chargeback, dispute and claim losses | expense | debit | IS | opex |
| 7000 | Interest expense | expense | debit | IS | interest |
| 7100 | Interest income | revenue | credit | IS | interest |
| 7200 | Other non-operating gains / (losses) | expense | debit | IS | other |
| 8000 | Income tax expense | expense | debit | IS | tax |

Signed trial-balance convention: `balance = Σdebits − Σcredits` for every account (so credit-normal
accounts are negative). `trial_balance[period][code]` stores this signed cents value;
presentation flips sign for credit-normal accounts.

## 3. Opening balance sheet construction

The generator starts at `periods[0] − 1` with an opening balance sheet derived from the industry
profile and the archetype's `revenue_ttm`:

```
R      = revenue_ttm_cents (seeded from industry revenue_range, log-uniform)
gm     = draw(<industry>.gross_margin)
dso    = draw(<industry>.dso); dio = draw(<industry>.dio); dpo = draw(<industry>.dpo)
AR0    = R * dso / 365
INV0   = R * (1 - gm) * dio / 365          (split RM/WIP/FG per archetype; non-inventory industries 0)
AP0    = R * (1 - gm) * dpo / 365
DR0    = R * deferred_revenue_days / 365   (SaaS: draw(saas.deferred_revenue_pct_of_arr) * ARR)
PPE0   = R * draw(<industry>.capex_pct_revenue) * ppe_life_years * 0.55  (mid-life assumption)
AccDep0= PPE0 * 0.45
Cash0  = R * draw(<industry>.cash_pct_revenue)   (fallback 0.08–0.20)
Debt0  = EBITDA_ttm * draw(<industry>.debt_to_ebitda); split 20% current / 80% non-current
Equity plug: Common stock = 0.6 * (Assets − Liabilities); Retained earnings = remainder.
```

If `Assets − Liabilities < 0` after the draws (possible for distressed archetypes), the plug goes to
retained earnings as a deficit; common stock is set to `max(0.25 * Assets, 1)`.

The opening balance sheet is posted as a single journal entry `OB-0000` with source `opening`
dated the day before `periods[0]`.

## 4. Journal event families

Every transactional row in the company database that has a money effect produces exactly one
journal entry (or a share of a batched entry for payroll/accruals). The `source` field on the
entry and the `dimension` dict on lines carry the row identifiers so sub-ledger ties can be tested.

Amounts: let `A` be the row amount in cents. Templates list `Dr` then `Cr`.

| Family | Trigger row | Entry |
|---|---|---|
| **sale_credit** | `invoices`/`orders` with terms > 0 | Dr 1100 A; Cr 4000/4010/4020 A (split by line type). Sales tax: Dr 1100 tax; Cr 2350 tax. |
| **sale_cash** | orders paid at checkout | Dr 1000 A; Cr 4000 A; fee: Dr 5050 f·A; Cr 1000 f·A |
| **cogs_relief** | shipment / fulfillment row | Dr 5000 unit_cost·qty; Cr 1220 same. Manufacturing: Dr 5000 ← 1220 FG cost; FG built from WIP: Dr 1220 Cr 1210; WIP from RM+labor+OH: Dr 1210 Cr 1200/1110-labor accrual/5020 absorption. |
| **freight** | shipment row | Dr 5030 A; Cr 2000 A (carrier payable) |
| **collection** | `payments` row | Dr 1000 A; Cr 1100 A |
| **bad_debt_provision** | month-end | Dr 6210 p·AR_gross; Cr 1110 same, where `p = allowance_target − current allowance` to hold allowance at `allowance_pct_of_ar` |
| **write_off** | invoice marked uncollectible | Dr 1110 A; Cr 1100 A |
| **return** | `returns` row | Dr 4100 A; Cr 1100 (if unpaid) or 1000 (refund) A; restock: Dr 1220 cost; Cr 5000 cost |
| **refund_reserve** | month-end (ecommerce/retail) | Dr 4100 r·sales; Cr 2450 r·sales; actual refunds relieve 2450 |
| **purchase_inventory** | `purchase_orders` received | Dr 1200/1220 A; Cr 2000 A |
| **vendor_payment** | AP payment row | Dr 2000 A; Cr 1000 A |
| **opex_accrual** | month-end by department | Dr 6000/6100/6200/6400 A; Cr 2100 A; paid next month: Dr 2100; Cr 1000 |
| **payroll** | month-end | Dr 5010/6000/6100/6200 by department share; Cr 2110; paid: Dr 2110; Cr 1000 |
| **commission** | invoice booked (SaaS) | Dr 1350 c·ACV; Cr 2110 c·ACV; amortize monthly over `commission_life_months`: Dr 6010; Cr 1350 |
| **deferred_revenue** | subscription invoice | Dr 1100 A; Cr 2200 A; monthly recognition: Dr 2200 A/term; Cr 4000 A/term |
| **unbilled_revenue** | usage recognized before invoice | Dr 1150 A; Cr 4020 A; invoice later: Dr 1100 A; Cr 1150 A |
| **capex** | asset purchase | Dr 1500 A; Cr 1000 (or 2000) A |
| **depreciation** | month-end | Dr 6300 PPE_gross/(life·12); Cr 1510 |
| **cap_software** | engineering payroll capitalized | Dr 1600 A; Cr 2110 A (instead of 6100); amortize 36 months: Dr 6310; Cr 1610 |
| **debt_draw / repay** | | Dr 1000; Cr 2500/2610 — repay reversed; reclass current portion quarterly: Dr 2610; Cr 2600 |
| **interest** | month-end | Dr 7000 debt·rate/12; Cr 2150; paid quarterly: Dr 2150; Cr 1000 |
| **tax** | quarter-end | Dr 8000 max(0, pretax)·rate; Cr 2400; paid following quarter |
| **inventory_writedown** | quarter-end | Dr 5040 A; Cr 1230 A |
| **chargeback** | `disputes`/`claims` row lost | Dr 6500 A; Cr 1000 A (processor debits cash) |
| **dividend** | quarter-end if policy | Dr 3200 A; Cr 1000 A |
| **close** | fiscal year end only | Close IS accounts to 3100. Quarterly statements are computed from period activity, not from closing entries. |

Rounding: every line is `round_half_even(amount)` to cents; when a template splits an amount
(e.g. payroll across departments) the last split receives the residual so Σ splits == A exactly.

## 5. Statement construction

### 5.1 Income statement (period activity)

```
revenue           = −(Δ4000 + Δ4010 + Δ4020 + Δ4100 + Δ4110)    (contra accounts are debit-normal, so this nets them)
cogs              = Δ5000 + Δ5010 + Δ5020 + Δ5030 + Δ5040 + Δ5050
gross_profit      = revenue − cogs
opex              = Σ Δ6xxx
operating_income  = gross_profit − opex
other             = −Δ4200 − Δ7100 + Δ7000 + Δ7200
pretax            = operating_income − other
tax               = Δ8000
net_income        = pretax − tax
```
`Δcode` is the period's net posting to the account (debits − credits). Captions: Revenue, Cost of
revenue, Gross profit, Sales & marketing, R&D, G&A, Depreciation & amortization, Operating income,
Interest expense net, Other, Pretax income, Income tax, Net income.

### 5.2 Balance sheet (closing balances)

```
cash                      = 1000
ar_net                    = 1100 + 1110 (1110 negative) + 1150
inventory_net             = 1200 + 1210 + 1220 + 1230
other_current             = 1300 + 1350 + 1400
total_current_assets      = Σ above
ppe_net                   = 1500 + 1510
intangibles_net           = 1600 + 1610 + 1700 + 1710 + 1720
other_noncurrent          = 1800
total_assets              = total_current_assets + ppe_net + intangibles_net + other_noncurrent
current_liabilities       = −(2000+2100+2110+2150+2200+2300+2350+2400+2450+2500+2600)
noncurrent_liabilities    = −(2210+2610+2700)
total_liabilities         = current + noncurrent
equity                    = −(3000 + 3100 + 3200) + (retained earnings for current FY = −Σ IS net postings since last close)
total_equity              = equity
assert total_assets == total_liabilities + total_equity   (exact integer cents)
```
Because the trial balance is produced from balanced entries, this assertion holds by construction;
it is still checked (`InvariantCheck "bs_balances"`) because it catches rounding bugs in splits.

### 5.3 Cash flow statement (indirect)

```
CFO = net_income
    + Δ6300 + Δ6310 + Δ5040 + Δ6210 (non-cash: D&A, write-downs, provisions)
    − Δ(1100 gross) − Δ1150 − Δ(1200+1210+1220) − Δ1300 − Δ1350 − Δ1400       (ΔWC assets)
    + Δ(2000+2100+2110+2150+2200+2210+2300+2350+2400+2450)  (ΔWC liabilities, as positive credits)
    − write_offs (already inside Δ1100 via 1110, handled by using Δ1100 gross and adding 6210)
CFI = −capex − cap_software − acquisitions(1700/1710 additions) + asset_sale_proceeds
CFF = Δ(2500+2600+2610+2700) + Δ3000 − dividends(Δ3200)
net_change = CFO + CFI + CFF
assert beginning_cash + net_change == ending_cash == BS cash
```
The test `cfs_ties` asserts both equalities to the cent. Implementation hint: compute CFO as the
residual `net_change − CFI − CFF` AND independently via the indirect formula; both must agree.
That double computation is the single most effective bug-catcher in this engine.

### 5.4 Ratios per period (`FinancialStatements.ratios[period]`)

`gross_margin, operating_margin, net_margin, dso = AR_net/revenue·days, dio = inventory/cogs·days,
dpo = AP/cogs·days, ccc = dso+dio−dpo, current_ratio, quick_ratio, debt_to_ebitda (TTM), interest_coverage,
accrual_ratio = (NI − CFO)/avg_total_assets, revenue_growth_qoq, revenue_growth_yoy, capex_pct_revenue,
deferred_revenue_to_revenue, allowance_pct_ar, unbilled_pct_ar, returns_pct_sales`. Days = days in quarter.

## 6. Sub-ledger tie rules

Ground truth states which ties hold and which are broken. Default (healthy) ties:

| Tie | Query sketch |
|---|---|
| AR | `SUM(invoices.amount − payments applied − write_offs − credit memos)` for open invoices == BS 1100 |
| Deferred revenue | Σ unrecognized remaining on active subscriptions == 2200+2210 |
| Inventory | Σ `inventory_movements` quantity·cost by SKU == 1200/1220 |
| AP | Σ open `purchase_orders`/`vendor_bills` == 2000 |
| Revenue | Σ `gl_lines` on 4xxx == IS revenue == Σ invoice_lines recognized in period |

The `gl_journal`/`gl_lines` tables mirror `FinanceState.journal` row-for-row, so users can
reconcile the sub-ledger to the GL inside SQL.

## 7. Operations primitives (`operations.py`)

- **Little's Law**: `L = λ·W`. Stations have `capacity_per_hour c_i`, demand `λ`, utilization
  `ρ_i = λ/c_i`. Bottleneck = argmax ρ. Throughput = `min(λ, min_i c_i)`.
- **M/M/1 approximation per station** (used for WIP and cycle time): `Lq = ρ²/(1−ρ)`,
  `Wq = Lq/λ`, `W = Wq + 1/c`. For `ρ ≥ 0.98` clamp ρ to 0.98 and flag "unstable".
  Kingman (G/G/1) variant when CVs are known: `Wq ≈ (ρ/(1−ρ))·((ca²+cs²)/2)·(1/c)`.
- **Cohorts**: retention `r(t) = (1−c_early)·s^(t−1)` for t ≥ 1 with monthly churn `c_early` in month 1
  and steady survival `s`; revenue per cohort `= size·r(t)·arpa·(1+expansion)^t`.
- **Order-up-to with bullwhip**: tier k observes demand `D_k`, forecasts with moving average of `p`
  periods, lead time `L`, orders `O_k = S_k − inventory_position`, `S_k = (L+1)·D̂ + z·σ̂·√(L+1)`.
  Variance amplification (Chen, Drezner, Ryan, Simchi-Levi 2000): `Var(O)/Var(D) ≥ 1 + 2L/p + 2L²/p²`.
- **EOQ**: `Q* = √(2·D·K/h)`; safety stock `= z·σ_L` with `σ_L = σ_d·√L`.
- **Elasticity**: `Q(p) = Q0·(p/p0)^ε` with ε < 0 per segment; revenue-maximizing when ε = −1,
  profit-maximizing markup `(p−c)/p = −1/ε` (Lerner).
- **Unit economics**: `CAC = S&M / new_customers`, `LTV = arpa·gm / churn_monthly`, `payback = CAC/(arpa·gm)`.

## 8. Table generation conventions (`datagen.py`)

- Row-count target per table is `typical_rows_per_month × 24 × scale` where `scale` is derived from
  revenue so that total rows land in 1k–50k (`scale = clamp(revenue_ttm / 50e6, 0.05, 1.0)`).
- Every table has `created_at`; transactional tables have `updated_at`. `late_arriving` dirt sets
  `updated_at < created_at` on a slice.
- Dirt is applied **after** all GL entries are generated and **before** reference query
  expectations are computed. Dirt must never change money totals that the GL depends on; it changes
  representation (nulls on non-key descriptive columns, duplicated rows flagged by a second
  `ingest_id`, mixed formats), and it adds orphans only on descriptive FKs (e.g. `sales_rep_id`),
  never on FKs the GL ties through.
- Duplicate-key dirt is the one exception: duplicated invoice rows DO double-count if the user
  sums naively. That is the pedagogical point; the GL is correct and the `gl_lines` dimension
  references prove which row is canonical (`ingest_id` lowest).

## 9. Difficulty scaling

| Difficulty | Anomaly magnitude | Dirt | Red herrings | Periods affected |
|---|---|---|---|---|
| foundation | high end of range | 2 rules | 1 | last 2 |
| practitioner | mid | 4 rules | 2 | last 3, partially masked |
| principal | low end of range (subtle) | 6 rules incl. dup_keys + tz_mixed | 3, one of which is itself a real but minor issue | last 4, with a decoy anomaly |
