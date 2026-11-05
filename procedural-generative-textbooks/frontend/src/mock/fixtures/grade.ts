import type { GradeReport, MetricResult, Submission } from "../../api/types";

const EXPECTED: Omit<MetricResult, "submitted" | "within_tolerance">[] = [
  { key: "dso_2024q4", expected: 77.6, unit: "days", tolerance_abs: 2, tolerance_rel: null, derivation: "Deduplicated open AR at 2024-12-31 ($12.20M, legacy dollar rows ×100) ÷ 2024Q4 revenue ($14.30M) × 91." },
  { key: "ar_over_90_pct", expected: 0.31, unit: "pct", tolerance_abs: 0.03, tolerance_rel: null, derivation: "Open AR with due_date < 2024-10-02 ($3.78M) ÷ total open AR ($12.20M)." },
  { key: "cash_trapped_usd", expected: 5_130_000, unit: "usd", tolerance_abs: null, tolerance_rel: 0.05, derivation: "$12.20M − (45 ÷ 91 × $14.30M) = $5.13M above a 45-day DSO." },
  { key: "ramp_concessions_usd", expected: 1_920_000, unit: "usd", tolerance_abs: null, tolerance_rel: 0.05, derivation: "SUM(credit_memos.amount_cents) where TRIM(LOWER(reason)) = 'ramp concession' and issued_at in 2024H2." },
];

function within(m: (typeof EXPECTED)[number], v: number | null): boolean {
  if (v === null || Number.isNaN(v)) return false;
  if (m.tolerance_abs !== null) return Math.abs(v - m.expected) <= m.tolerance_abs;
  return Math.abs(v - m.expected) <= Math.abs(m.expected) * (m.tolerance_rel ?? 0.02);
}

export function gradeSubmission(sub: Submission, n: number): GradeReport {
  const metric_results: MetricResult[] = EXPECTED.map((m) => {
    const v = sub.metrics[m.key];
    const submitted = typeof v === "number" ? v : null;
    return { ...m, submitted, within_tolerance: within(m, submitted) };
  });
  const accuracy = metric_results.filter((m) => m.within_tolerance).length / metric_results.length;
  const evidenceBoost = Math.min(1.5, sub.sql_evidence.length * 0.5 + sub.python_evidence.length * 0.5);
  const memoLen = sub.memo_markdown.trim().length;
  return {
    submission_id: `sub_${n.toString().padStart(4, "0")}`,
    deterministic: {
      metric_results,
      accuracy,
      query_signature_matches: [
        { name: "dedup_open_ar", matched: sub.sql_evidence.some((e) => /distinct|row_number/i.test(e.sql)), detail: "Open AR computed on unique invoice_id" },
        { name: "unit_normalization", matched: sub.sql_evidence.some((e) => /\* ?100/.test(e.sql)), detail: "Legacy dollar rows rescaled to cents" },
        { name: "aging_buckets", matched: sub.sql_evidence.some((e) => /case/i.test(e.sql)), detail: "Aging buckets derived from due_date" },
      ],
    },
    rubric: {
      overall: Number((5.2 + accuracy * 2.5 + evidenceBoost).toFixed(1)),
      misdiagnosis: accuracy < 0.25,
      strongest_point: "Separates the data-quality noise from the real change in customer payment behaviour.",
      weakest_point: "Recommendation does not quantify the covenant headroom created per lever.",
      next_drill_nodes: ["af.covenants", "sql.aging_buckets"],
      dimensions: {
        technical_execution: { score: Number((5 + accuracy * 4).toFixed(1)), evidence: ["\"deduplicated 61 invoice keys before computing open AR\"", "\"rescaled 214 legacy rows from dollars to cents\""], gaps: ["Did not reconcile payments with NULL invoice_id (unapplied cash)."] },
        business_judgment: { score: 7.4, evidence: ["\"the migration explains less than a fifth of the DSO increase\""], gaps: ["No view on which enterprise accounts to escalate first."] },
        communication: { score: memoLen > 400 ? 8.1 : 5.6, evidence: ["Memo leads with the answer and the dollar figure."], gaps: memoLen > 400 ? [] : ["Memo is too short to stand alone for a board pre-read."] },
        risk_awareness: { score: 6.2, evidence: ["Flags the liquidity covenant at threshold."], gaps: ["Ignores churn risk from stricter dunning on renewing accounts."] },
      },
    },
    skill_updates: [
      { node: "af.working_capital", before: 0.46, after: 0.61 },
      { node: "af.ar_aging", before: 0.32, after: 0.49 },
      { node: "sql.aging_buckets", before: 0.28, after: 0.44 },
      { node: "sql.dedup", before: 0.51, after: 0.58 },
      { node: "os.collections_ops", before: 0.22, after: 0.3 },
    ],
    revealed: {
      root_cause:
        "Starting in 2023Q3, Sales granted enterprise customers informal 60–90 day terms and 'ramp concessions' outside contracted terms, and asked Collections to pause dunning on renewing accounts. Open AR for the enterprise segment tripled while contracted terms stayed at 30–60 days. The billing migration (duplicate keys, dollar-denominated legacy rows, mixed date formats) adds visible noise but explains under 20% of the DSO increase.",
      anomalies: [
        { id: "A1", kind: "informal_terms_extension", pillar: "accounting_finance", title: "Informal terms extension for enterprise", mechanism: "due_date − issue_date exceeds customers.payment_terms_days for 38% of enterprise invoices from 2023Q3.", periods: ["2023Q3", "2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4"], severity: "high", evidence_trail: ["DSO rises faster in enterprise", "due dates exceed contracted terms", "collections notes show dunning holds"] },
        { id: "A2", kind: "credit_memo_concessions", pillar: "accounting_finance", title: "Ramp concessions via credit memos", mechanism: "Credit memos with reason 'Ramp concession' reduce invoices after issue; discounts booked to 4110.", periods: ["2024Q3", "2024Q4"], severity: "medium", evidence_trail: ["credit memo spike", "reason text cluster"] },
        { id: "A3", kind: "migration_dirt", pillar: "sql", title: "Billing migration artefacts", mechanism: "dup_keys, unit_mixed:amount_cents, format_mixed:issue_date on invoices.", periods: ["2024Q2"], severity: "low", evidence_trail: ["duplicate invoice ids", "amounts 100× too small"] },
      ],
      reference_solution: {
        sql: "WITH inv AS (\n  SELECT DISTINCT ON (invoice_id) *,\n    CASE WHEN amount_cents < 10000 THEN amount_cents*100 ELSE amount_cents END AS amt\n  FROM invoices WHERE status='open'\n)\nSELECT SUM(amt)/100.0 AS open_ar_usd,\n  SUM(amt) FILTER (WHERE due_date < DATE '2024-10-02')/SUM(amt) AS over_90_pct\nFROM inv;",
        memo_bullets: [
          "Cash is trapped in enterprise receivables, not lost to migration noise: $5.1M above a 45-day DSO.",
          "Root cause: informal 60–90 day terms and paused dunning on renewing enterprise accounts.",
          "Q1 plan: enforce contracted terms on renewals, add 2 collections FTE, offer 1% early-pay to the top 10 accounts.",
        ],
        design_mermaid: null,
      },
    },
    adversary_opening:
      "You attribute most of the DSO increase to informal terms. Marcus will say Q4 enterprise procurement freezes are seasonal. What in the data rules out seasonality?",
  };
}
