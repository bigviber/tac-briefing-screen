import type { ArchetypeInfo } from "../../api/types";

const all: ArchetypeInfo["difficulty_range"] = ["foundation", "practitioner", "principal"];
const upper: ArchetypeInfo["difficulty_range"] = ["practitioner", "principal"];

export const ARCHETYPES: ArchetypeInfo[] = [
  { key: "working_capital_crunch", title: "Working-capital crunch", pillars: ["accounting_finance", "sql"], skill_nodes: ["af.working_capital", "af.ar_aging", "sql.aging_buckets", "os.collections_ops"], difficulty_range: all, summary: "Revenue grows while cash shrinks. Find what is trapping cash in receivables." },
  { key: "abc_cost_distortion", title: "ABC cost distortion", pillars: ["accounting_finance", "operations_strategy"], skill_nodes: ["af.ratio_analysis", "os.unit_economics"], difficulty_range: all, summary: "Peanut-butter overhead allocation hides which products lose money." },
  { key: "ma_synergies", title: "M&A synergy audit", pillars: ["accounting_finance"], skill_nodes: ["af.dcf_valuation", "af.three_statements"], difficulty_range: upper, summary: "A banker's synergy case meets the integrated ledger." },
  { key: "forensic_revenue", title: "Forensic revenue", pillars: ["accounting_finance", "sql"], skill_nodes: ["af.forensic_signals", "af.revenue_recognition", "sql.reconciliation"], difficulty_range: upper, summary: "Quarter-end spikes, side letters and credit-memo reversals." },
  { key: "toc_bottleneck", title: "ToC bottleneck", pillars: ["operations_strategy"], skill_nodes: ["os.toc", "os.littles_law", "os.capacity_planning"], difficulty_range: all, summary: "Throughput stalls; overtime is spent at the wrong station." },
  { key: "cohort_collapse", title: "Cohort collapse", pillars: ["operations_strategy", "sql"], skill_nodes: ["os.cohort_retention", "sql.cohort_queries"], difficulty_range: all, summary: "Topline ARR masks rotting retention in recent cohorts." },
  { key: "bullwhip", title: "Bullwhip amplification", pillars: ["operations_strategy"], skill_nodes: ["os.bullwhip", "os.queueing"], difficulty_range: upper, summary: "Small consumer swings become large factory swings." },
  { key: "pricing_elasticity", title: "Pricing elasticity", pillars: ["operations_strategy", "python"], skill_nodes: ["os.pricing", "py.regression"], difficulty_range: all, summary: "Estimate elasticity by segment and price the next tier." },
  { key: "idempotent_webhooks", title: "Idempotent webhooks", pillars: ["system_design"], skill_nodes: ["sd.idempotency", "sd.webhooks"], difficulty_range: all, summary: "Payment-provider retries double-post invoices." },
  { key: "outbox_pattern", title: "Transactional outbox", pillars: ["system_design"], skill_nodes: ["sd.outbox", "sd.queues"], difficulty_range: upper, summary: "Events are lost between the DB commit and the broker." },
  { key: "replica_lag", title: "Replica lag", pillars: ["system_design", "sql"], skill_nodes: ["sd.replication", "sd.consistency"], difficulty_range: upper, summary: "Reports read stale replicas during close." },
  { key: "window_reconciliation", title: "Window reconciliation", pillars: ["sql"], skill_nodes: ["sql.window_basics", "sql.dedup", "sql.reconciliation"], difficulty_range: all, summary: "Tie the billing ledger to the processor with window functions." },
  { key: "cohort_matrix", title: "Cohort matrix", pillars: ["sql", "python"], skill_nodes: ["sql.cohort_queries", "py.pandas_groupby"], difficulty_range: all, summary: "Build a retention matrix from raw events." },
  { key: "monte_carlo_cash", title: "Monte Carlo cash", pillars: ["python", "accounting_finance"], skill_nodes: ["py.monte_carlo", "af.covenants"], difficulty_range: upper, summary: "Simulate 13-week cash against covenant headroom." },
];
