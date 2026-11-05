import type { GradeReport, MetricResult, QuerySignatureMatch, Submission } from "../../api/types";

const EXPECTED: Omit<MetricResult, "submitted" | "within_tolerance">[] = [
  { key: "dso_2024q4", expected: 77.6, unit: "days", tolerance_abs: 2, tolerance_rel: null, derivation: "Deduplicated open AR at 2024-12-31 ($12.20M, legacy dollar rows ×100) ÷ 2024Q4 revenue ($14.30M) × 91.", pillar: "accounting_finance", anomaly_ids: ["A1", "A3"] },
  { key: "ar_over_90_pct", expected: 0.31, unit: "pct", tolerance_abs: 0.03, tolerance_rel: null, derivation: "Open AR with due_date < 2024-10-02 ($3.78M) ÷ total open AR ($12.20M).", pillar: "sql", anomaly_ids: ["A1"] },
  { key: "cash_trapped_usd", expected: 5_130_000, unit: "usd", tolerance_abs: null, tolerance_rel: 0.05, derivation: "$12.20M − (45 ÷ 91 × $14.30M) = $5.13M above a 45-day DSO.", pillar: "accounting_finance", anomaly_ids: ["A1"] },
  { key: "ramp_concessions_usd", expected: 1_920_000, unit: "usd", tolerance_abs: null, tolerance_rel: 0.05, derivation: "SUM(credit_memos.amount_cents) where TRIM(LOWER(reason)) = 'ramp concession' and issued_at in 2024H2.", pillar: "sql", anomaly_ids: ["A2"] },
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
  const hasSql = sub.sql_evidence.length > 0;
  const sig = (name: string, re: RegExp, detail: string, anomaly_id: string): QuerySignatureMatch => ({
    name, detail, anomaly_id, pillar: "sql", evaluated: hasSql, method: "signature",
    matched: sub.sql_evidence.some((e) => re.test(e.sql)),
  });
  const query_signature_matches = [
    sig("dedup_open_ar", /distinct|row_number/i, "Open AR computed on unique invoice_id", "A3"),
    sig("unit_normalization", /\* ?100/, "Legacy dollar rows rescaled to cents", "A3"),
    sig("aging_buckets", /case/i, "Aging buckets derived from due_date", "A1"),
  ];
  const items_total = metric_results.length + query_signature_matches.filter((q) => q.evaluated).length;
  const items_passed = metric_results.filter((m) => m.within_tolerance).length + query_signature_matches.filter((q) => q.evaluated && q.matched).length;
  const det_pass = items_total ? items_passed / items_total : 0;
  // Backend rule: technical_execution may not exceed what the deterministic check supports.
  // Backend te_ceiling(): always a number, 10 = unconstrained.
  const ceiling_te = items_total === 0 || det_pass >= 0.9 ? 10 : det_pass >= 0.6 ? 8 : det_pass >= 0.3 ? 5 : 3;
  const rawTe = Number((5 + accuracy * 4 + evidenceBoost).toFixed(1));
  const te = Math.min(rawTe, ceiling_te);
  const raw_scores = { technical_execution: rawTe, business_judgment: 7.4, communication: memoLen > 400 ? 8.1 : 5.6, risk_awareness: 6.2 };
  const llm_overall = Number((Object.values(raw_scores).reduce((a, b) => a + b, 0) / 4).toFixed(1));
  const overall = Number(((te + raw_scores.business_judgment + raw_scores.communication + raw_scores.risk_awareness) / 4).toFixed(1));
  const counted = n === 1;
  const upd = (node: string, before: number, after: number, score: number) => ({
    node, before, after: counted ? after : before, score,
    outcome: score >= 0.65 ? ("correct" as const) : score >= 0.5 ? ("borderline" as const) : ("incorrect" as const),
  });
  const skill_updates = [
    upd("af.working_capital", 0.46, 0.61, 0.82),
    upd("af.ar_aging", 0.32, 0.49, 0.71),
    upd("sql.aging_buckets", 0.28, 0.44, 0.58),
    upd("sql.dedup", 0.51, 0.58, 0.66),
    upd("os.collections_ops", 0.22, 0.3, 0.41),
  ];
  return {
    submission_id: `sub_${n.toString().padStart(4, "0")}`,
    deterministic: { metric_results, accuracy, query_signature_matches, det_pass, items_passed, items_total },
    observations: skill_updates.map((u) => ({
      node: u.node,
      pillar: u.node.startsWith("af.") ? "accounting_finance" : u.node.startsWith("sql.") ? "sql" : "operations_strategy",
      score: u.score,
      correct: u.outcome === "correct",
      outcome: u.outcome,
      slip_adjust: u.outcome === "borderline" ? 0.1 : 0,
    })),
    counted_for_mastery: counted,
    rubric: {
      overall,
      llm_overall,
      raw_scores,
      ceiling_te,
      clamps: rawTe > ceiling_te ? [`technical_execution ${rawTe} -> ${ceiling_te} (deterministic pass rate ${det_pass.toFixed(2)})`] : [],
      stated_root_cause: memoLen > 0 ? sub.memo_markdown.trim().split("\n")[0].slice(0, 220) : "",
      misdiagnosis: accuracy < 0.25,
      strongest_point: "Separates the data-quality noise from the real change in customer payment behaviour.",
      weakest_point: "Recommendation does not quantify the covenant headroom created per lever.",
      next_drill_nodes: ["af.covenants", "sql.aging_buckets"],
      dimensions: {
        technical_execution: { score: te, evidence: ["\"deduplicated 61 invoice keys before computing open AR\"", "\"rescaled 214 legacy rows from dollars to cents\""], gaps: ["Did not reconcile payments with NULL invoice_id (unapplied cash)."] },
        business_judgment: { score: 7.4, evidence: ["\"the migration explains less than a fifth of the DSO increase\""], gaps: ["No view on which enterprise accounts to escalate first."] },
        communication: { score: memoLen > 400 ? 8.1 : 5.6, evidence: ["Memo leads with the answer and the dollar figure."], gaps: memoLen > 400 ? [] : ["Memo is too short to stand alone for a board pre-read."] },
        risk_awareness: { score: 6.2, evidence: ["Flags the liquidity covenant at threshold."], gaps: ["Ignores churn risk from stricter dunning on renewing accounts."] },
      },
    },
    skill_updates: counted ? skill_updates : [],
    revealed: {
      root_cause:
        "Starting in 2023Q3, Sales granted enterprise customers informal 60–90 day terms and 'ramp concessions' outside contracted terms, and asked Collections to pause dunning on renewing accounts. Open AR for the enterprise segment tripled while contracted terms stayed at 30–60 days. The billing migration (duplicate keys, dollar-denominated legacy rows, mixed date formats) adds visible noise but explains under 20% of the DSO increase.",
      anomalies: [
        { id: "A1", kind: "informal_terms_extension", pillar: "accounting_finance", title: "Informal terms extension for enterprise", mechanism: "due_date − issue_date exceeds customers.payment_terms_days for 38% of enterprise invoices from 2023Q3.", periods: ["2023Q3", "2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4"], severity: "high", evidence_trail: ["DSO rises faster in enterprise", "due dates exceed contracted terms", "collections notes show dunning holds"] },
        { id: "A2", kind: "credit_memo_concessions", pillar: "accounting_finance", title: "Ramp concessions via credit memos", mechanism: "Credit memos with reason 'Ramp concession' reduce invoices after issue; discounts booked to 4110.", periods: ["2024Q3", "2024Q4"], severity: "medium", evidence_trail: ["credit memo spike", "reason text cluster"] },
        { id: "A3", kind: "migration_dirt", pillar: "sql", title: "Billing migration artefacts", mechanism: "dup_keys, unit_mixed:amount_cents, format_mixed:issue_date on invoices.", periods: ["2024Q2"], severity: "low", evidence_trail: ["duplicate invoice ids", "amounts 100× too small"] },
      ],
      reference_solution: {
        sql: ["WITH inv AS (\n  SELECT DISTINCT ON (invoice_id) *,\n    CASE WHEN amount_cents < 10000 THEN amount_cents*100 ELSE amount_cents END AS amt\n  FROM invoices WHERE status='open'\n)\nSELECT SUM(amt)/100.0 AS open_ar_usd,\n  SUM(amt) FILTER (WHERE due_date < DATE '2024-10-02')/SUM(amt) AS over_90_pct\nFROM inv;",
          "SELECT SUM(amount_cents) / 100.0 AS ramp_concessions_usd\nFROM credit_memos\nWHERE TRIM(LOWER(reason)) = 'ramp concession'\n  AND issued_at >= DATE '2024-07-01' AND issued_at < DATE '2025-01-01';",
        ],
        python: [],
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
