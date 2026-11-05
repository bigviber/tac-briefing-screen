import type {
  Ask,
  BalanceSheet,
  CasePublic,
  CashFlowStatement,
  FinancialStatements,
  IncomeStatement,
  Lever,
} from "../../api/types";
import { DATABASE } from "./schema";

export const CASE_ID = "cs_7f3a91";
export const PERIODS = ["2023Q1", "2023Q2", "2023Q3", "2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4"];

const M = 100_000_000; // $1M in cents
const r = (x: number) => Math.round(x);

/** Builds 8 quarters of statements that tie exactly (A = L + E, CF ties to cash). */
function buildStatements(): FinancialStatements {
  const dsoPath = [44, 46, 49, 53, 58, 64, 71, 78];
  const income: IncomeStatement[] = [];
  const balance: BalanceSheet[] = [];
  const cash_flow: CashFlowStatement[] = [];
  const ratios: FinancialStatements["ratios"] = {};
  let prev = { cash: 14.2 * M, ar: 4.1 * M, prepaid: 0.6 * M, ppe: 2.4 * M, ap: 1.1 * M, accrued: 1.3 * M, dr: 6.2 * M, revolver: 0, re: 3.9 * M };
  const stock = 9.0 * M;
  PERIODS.forEach((period, i) => {
    const rev = r(9.0 * M * Math.pow(1.058, i));
    const cogs = r(rev * (0.24 + i * 0.004));
    const gp = rev - cogs;
    const sm = r(rev * 0.38), rd = r(rev * 0.22), ga = r(rev * 0.12);
    const da = r(0.18 * M);
    const opInc = gp - sm - rd - ga - da;
    const interest = r(prev.revolver * 0.021);
    const tax = Math.max(0, r((opInc - interest) * 0.21));
    const ni = opInc - interest - tax;
    income.push({
      period, revenue_cents: rev, gross_profit_cents: gp, operating_income_cents: opInc, net_income_cents: ni,
      lines: [
        { caption: "Subscription revenue", account_codes: ["4000"], cents: rev, level: 0 },
        { caption: "Cost of revenue", account_codes: ["5000", "5020"], cents: -cogs, level: 0 },
        { caption: "Gross profit", account_codes: [], cents: gp, level: 1 },
        { caption: "Sales and marketing", account_codes: ["6000", "6010"], cents: -sm, level: 0 },
        { caption: "Research and development", account_codes: ["6100"], cents: -rd, level: 0 },
        { caption: "General and administrative", account_codes: ["6200"], cents: -ga, level: 0 },
        { caption: "Depreciation and amortization", account_codes: ["6300", "6310"], cents: -da, level: 0 },
        { caption: "Operating income", account_codes: [], cents: opInc, level: 1 },
        { caption: "Interest expense", account_codes: ["7000"], cents: -interest, level: 0 },
        { caption: "Income tax expense", account_codes: ["8000"], cents: -tax, level: 0 },
        { caption: "Net income", account_codes: [], cents: ni, level: 1 },
      ],
    });
    const ar = r((rev * dsoPath[i]) / 91);
    const prepaid = r(0.6 * M + i * 0.03 * M);
    const capex = r(0.22 * M);
    const ppe = prev.ppe + capex - da;
    const ap = r(cogs * 0.42);
    const accrued = r(1.3 * M + i * 0.05 * M);
    const dr = r(rev * 0.69);
    const re = prev.re + ni;
    let revolver = prev.revolver;
    let cash = ap + accrued + dr + revolver + stock + re - ar - prepaid - ppe;
    const floor = 2.0 * M;
    if (cash < floor) {
      revolver += floor - cash;
      cash = floor;
    }
    const ta = cash + ar + prepaid + ppe;
    const tl = ap + accrued + dr + revolver;
    const te = stock + re;
    balance.push({
      period, total_assets_cents: ta, total_liabilities_cents: tl, total_equity_cents: te,
      lines: [
        { caption: "Cash and cash equivalents", account_codes: ["1000"], cents: cash, level: 0 },
        { caption: "Accounts receivable, net", account_codes: ["1100", "1110"], cents: ar, level: 0 },
        { caption: "Prepaid expenses", account_codes: ["1300"], cents: prepaid, level: 0 },
        { caption: "PP&E and software, net", account_codes: ["1500", "1510", "1600", "1610"], cents: ppe, level: 0 },
        { caption: "Total assets", account_codes: [], cents: ta, level: 1 },
        { caption: "Accounts payable", account_codes: ["2000"], cents: ap, level: 0 },
        { caption: "Accrued expenses", account_codes: ["2100", "2110"], cents: accrued, level: 0 },
        { caption: "Deferred revenue", account_codes: ["2200"], cents: dr, level: 0 },
        { caption: "Revolving line of credit", account_codes: ["2500"], cents: revolver, level: 0 },
        { caption: "Total liabilities", account_codes: [], cents: tl, level: 1 },
        { caption: "Common stock and APIC", account_codes: ["3000"], cents: stock, level: 0 },
        { caption: "Retained earnings", account_codes: ["3100"], cents: re, level: 0 },
        { caption: "Total equity", account_codes: [], cents: te, level: 1 },
      ],
    });
    const dAR = ar - prev.ar;
    const opCf = ni + da - dAR - (prepaid - prev.prepaid) + (ap - prev.ap) + (accrued - prev.accrued) + (dr - prev.dr);
    const finCf = revolver - prev.revolver;
    cash_flow.push({
      period, operating_cents: opCf, investing_cents: -capex, financing_cents: finCf,
      net_change_cents: cash - prev.cash, beginning_cash_cents: prev.cash, ending_cash_cents: cash,
      lines: [
        { caption: "Net income", account_codes: [], cents: ni, level: 0 },
        { caption: "Depreciation and amortization", account_codes: [], cents: da, level: 0 },
        { caption: "Change in accounts receivable", account_codes: ["1100"], cents: -dAR, level: 0 },
        { caption: "Change in deferred revenue", account_codes: ["2200"], cents: dr - prev.dr, level: 0 },
        { caption: "Cash from operations", account_codes: [], cents: opCf, level: 1 },
        { caption: "Capital expenditures", account_codes: ["1500"], cents: -capex, level: 0 },
        { caption: "Revolver draws", account_codes: ["2500"], cents: finCf, level: 0 },
      ],
    });
    ratios[period] = {
      dso: dsoPath[i],
      dpo: Number(((ap / cogs) * 91).toFixed(1)),
      ccc: Number((dsoPath[i] - (ap / cogs) * 91).toFixed(1)),
      gross_margin: Number((gp / rev).toFixed(3)),
      current_ratio: Number(((cash + ar + prepaid) / (ap + accrued + dr + revolver)).toFixed(2)),
      // Not meaningful before the revolver is drawn (zero interest expense): the backend now sends
      // null; older worlds carry the 999.0 sentinel (2023Q4 here). Both render "n/m".
      interest_coverage: interest === 0 ? (period === "2023Q4" ? 999.0 : null) : Number((opInc / interest).toFixed(1)),
    };
    prev = { cash, ar, prepaid, ppe, ap, accrued, dr, revolver, re };
  });
  return { periods: PERIODS, income, balance, cash_flow, ratios };
}

export const ASKS: Ask[] = [
  { key: "dso_2024q4", label: "DSO, 2024Q4", unit: "days", hint: "Ending AR ÷ quarterly revenue × 91. Clean duplicate and dollar-denominated invoices first." },
  { key: "ar_over_90_pct", label: "AR > 90 days past due", unit: "pct", hint: "Share of open AR (by value) more than 90 days past due at 2024-12-31." },
  // Backend asks for money keep the `_cents` key and unit "cents"; the UI takes dollars and converts.
  { key: "cash_trapped_in_ar_cents", label: "Excess cash trapped in AR", unit: "cents", hint: "AR above what a 45-day DSO would imply at 2024Q4 revenue." },
  { key: "ramp_concessions_usd", label: "Ramp concessions, 2024H2", unit: "usd", hint: "Credit memos tagged as ramp concessions issued Jul–Dec 2024." },
];

export const LEVERS: Lever[] = [
  { key: "collections_fte", label: "Collections headcount", min: 0, max: 8, default: 2, unit: "FTE", effect: "Each FTE recovers ~4 DSO days with diminishing returns." },
  { key: "payment_terms_days", label: "Default payment terms", min: 15, max: 90, default: 60, unit: "days", effect: "Shorter terms reduce DSO; enterprise win rate falls below 45 days." },
  { key: "dunning_strictness", label: "Dunning strictness", min: 0, max: 1, default: 0.3, unit: "index", effect: "Stricter dunning lowers 90+ AR and raises logo churn slightly." },
  { key: "early_pay_discount_pct", label: "Early-pay discount", min: 0, max: 0.03, default: 0, unit: "pct", effect: "2/10 net 30 style discount; accelerates cash at a margin cost." },
  { key: "revolver_paydown_cents", label: "Revolver paydown", min: 0, max: 300_000_000, default: 0, unit: "cents", effect: "Repay drawn revolver from cash; lowers interest, tightens liquidity." },
];

const statements = buildStatements();

const kpis: Record<string, Record<string, number>> = {};
PERIODS.forEach((p, i) => {
  kpis[p] = {
    arr_usd: Math.round(36_000_000 * Math.pow(1.058, i)),
    nrr: Number((1.12 - i * 0.006).toFixed(3)),
    gross_churn_monthly: Number((0.009 + i * 0.0006).toFixed(4)),
    collection_rate: Number((0.97 - i * 0.031).toFixed(3)),
    dso: statements.ratios[p].dso ?? 0,
  };
});

export function buildCase(overrides: Partial<CasePublic> = {}): CasePublic {
  return {
    case_id: CASE_ID,
    seed: 41827,
    archetype: "working_capital_crunch",
    version: "0.1.0",
    pillars: ["accounting_finance", "sql", "operations_strategy"],
    skill_nodes: ["af.working_capital", "af.ar_aging", "sql.aging_buckets", "sql.dedup", "os.collections_ops"],
    difficulty: "practitioner",
    company: {
      name: "Halcyon Metrics, Inc.", ticker_like: "HLCY", industry_code: "saas", business_model: "B2B subscription analytics",
      hq: "Denver, CO", founded: 2017, fiscal_year_end: "Dec", headcount: 214, stage: "Series C",
      revenue_ttm_cents: statements.income.slice(-4).reduce((s, x) => s + x.revenue_cents, 0),
      description: "Usage-analytics platform for logistics and healthcare operators; land-and-expand enterprise motion since 2023.",
    },
    industry: {
      code: "saas", name: "B2B SaaS", naics: "511210", business_models: ["subscription", "usage"],
      revenue_range_usd: [10_000_000, 200_000_000], benchmarks: {}, line_items: [], line_items_by_statement: {}, stage_mix: { "Series C": 1 }, schema_template: "saas_billing",
      narrative_hooks: ["enterprise procurement cycles", "annual prepay vs quarterly billing"],
    },
    periods: PERIODS,
    finance: {
      chart_of_accounts: [], journal: [], trial_balance: {}, statements,
      invariants: [
        { name: "balance_sheet_balances", passed: true, detail: "A = L + E for 8/8 periods" },
        { name: "cash_flow_ties", passed: true, detail: "Ending cash = BS cash for 8/8 periods" },
        { name: "ar_subledger_tie", passed: true, detail: "Open invoices tie to BS AR within designed breaks" },
      ],
    },
    operations: {
      // utilization > 1 is physically impossible; kept here so the data room proves it renders such values as-is.
      stations: [
        { name: "Invoice review", capacity_per_hour: 42, utilization: 0.81, wip: 36, cycle_time: 1.1 },
        { name: "Collections follow-up", capacity_per_hour: 18, utilization: 1.07, wip: 412, cycle_time: 22.9 },
      ],
      queueing: { arrival_rate_per_hour: 19.3 }, cohorts: [], supply_chain: {}, pricing: {},
      unit_economics: { cac: 18400, ltv: 96100, payback_months: 17.2, gross_margin: 0.74, arpa: 2580, churn_monthly: 0.013 },
      kpis_by_period: kpis,
    },
    database: DATABASE,
    dossier_seed: {
      reveal: ["Revenue grew every quarter", "The revolver was first drawn in 2024Q3"],
      stakeholders: [
        { name: "Dana Okafor", role: "CFO", agenda: "Avoid a covenant conversation", disposition: "anxious", knows: [], hides: [] },
        { name: "Marcus Hale", role: "VP Sales", agenda: "Protect enterprise deals", disposition: "defensive", knows: [], hides: [] },
      ],
      timeline: [
        { period: "2023Q3", headline: "Enterprise tier launched", detail: "First 60-day terms granted.", is_red_herring: false },
        { period: "2024Q2", headline: "New billing system go-live", detail: "Migration from legacy invoicing.", is_red_herring: true },
        { period: "2024Q3", headline: "Revolver drawn", detail: "$2.4M drawn on the $10M facility.", is_red_herring: false },
      ],
      artifacts: ["board_deck_excerpt", "slack_thread", "ar_aging_export"],
      architecture_mermaid: `flowchart LR
  CRM[(Salesforce)] -->|closed-won| Billing[Billing service]
  Billing -->|invoice.created| Queue{{Event bus}}
  Queue --> Ledger[GL posting worker]
  Processor[Payment processor] -->|webhook| Ingest[Webhook ingest]
  Ingest --> Ledger
  Ledger --> DW[(Warehouse / DuckDB)]
  DW --> BI[Board reporting]`,
    },
    simulation: { current_period: "2024Q4", history: [], model: { dso_elasticity_per_fte: -4.1 } },
    generated_at: "2026-11-04T18:22:09Z",
    asks: ASKS,
    levers: LEVERS,
    status: "briefed",
    ...overrides,
  };
}
