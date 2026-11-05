import { PILLARS, type Pillar, type PillarSummary, type SkillGraph, type SkillNode } from "../../api/types";

/** [id, title, prereqs, summary] — 12 nodes per pillar, 60 total. */
type Spec = [string, string, string[], string];

const SPECS: Record<Pillar, Spec[]> = {
  accounting_finance: [
    ["af.accrual_basics", "Accrual accounting", [], "Revenue and expense recognition independent of cash timing."],
    ["af.double_entry", "Double-entry & the GL", [], "Debits, credits, journal entries and the trial balance."],
    ["af.three_statements", "Three-statement linkage", ["af.accrual_basics", "af.double_entry"], "How IS, BS and CF tie through RE, cash and working capital."],
    ["af.revenue_recognition", "ASC 606 revenue recognition", ["af.accrual_basics"], "Five-step model, performance obligations, contract assets."],
    ["af.working_capital", "Working capital & CCC", ["af.three_statements"], "DSO, DIO, DPO and the cash conversion cycle."],
    ["af.ar_aging", "AR aging & allowance", ["af.working_capital"], "Aging buckets, expected credit loss, write-offs."],
    ["af.cash_flow_indirect", "Indirect cash flow", ["af.three_statements"], "Bridging net income to operating cash flow."],
    ["af.deferred_revenue", "Deferred revenue mechanics", ["af.revenue_recognition"], "Billings vs revenue, current vs non-current."],
    ["af.ratio_analysis", "Ratio analysis", ["af.three_statements"], "Liquidity, leverage, efficiency and profitability ratios."],
    ["af.forensic_signals", "Forensic accounting signals", ["af.ratio_analysis", "af.ar_aging"], "Beneish-style red flags and sub-ledger breaks."],
    ["af.covenants", "Debt covenants & liquidity", ["af.cash_flow_indirect", "af.ratio_analysis"], "Revolver availability, borrowing base, headroom."],
    ["af.dcf_valuation", "DCF valuation", ["af.cash_flow_indirect"], "Unlevered free cash flow, WACC and terminal value."],
  ],
  operations_strategy: [
    ["os.unit_economics", "Unit economics", [], "CAC, LTV, payback and contribution margin."],
    ["os.littles_law", "Little's Law", [], "L = λW applied to queues, WIP and backlog."],
    ["os.cohort_retention", "Cohort retention", ["os.unit_economics"], "Logo vs revenue retention, NRR, GRR."],
    ["os.saas_metrics", "SaaS metric stack", ["os.unit_economics"], "ARR bridges, magic number, burn multiple."],
    ["os.toc", "Theory of constraints", ["os.littles_law"], "Finding and exploiting the bottleneck."],
    ["os.queueing", "Queueing models", ["os.littles_law"], "M/M/1, utilization, waiting time blow-up."],
    ["os.pricing", "Pricing & elasticity", ["os.unit_economics"], "Price ladders, discount leakage and elasticity."],
    ["os.collections_ops", "Collections operations", ["os.saas_metrics", "af.ar_aging"], "Dunning cadence, terms enforcement, escalation."],
    ["os.bullwhip", "Bullwhip effect", ["os.queueing"], "Demand amplification across supply-chain tiers."],
    ["os.scenario_planning", "Scenario planning", ["os.saas_metrics"], "Driver trees, sensitivities and decision levers."],
    ["os.capacity_planning", "Capacity planning", ["os.toc", "os.queueing"], "Headroom, utilization targets, lead times."],
    ["os.strategy_memo", "Executive recommendations", ["os.scenario_planning"], "Structuring a decision memo for a CFO or board."],
  ],
  system_design: [
    ["sd.http_basics", "HTTP & APIs", [], "Requests, status codes, idempotency keys."],
    ["sd.data_modeling", "Data modeling", [], "Entities, keys, normalization trade-offs."],
    ["sd.consistency", "Consistency models", ["sd.data_modeling"], "Strong, eventual, read-your-writes."],
    ["sd.idempotency", "Idempotent handlers", ["sd.http_basics"], "Dedup keys, at-least-once delivery."],
    ["sd.queues", "Queues & async work", ["sd.http_basics"], "Producers, consumers, backpressure, DLQs."],
    ["sd.replication", "Replication & lag", ["sd.consistency"], "Leader/follower, lag symptoms, stale reads."],
    ["sd.outbox", "Transactional outbox", ["sd.queues", "sd.consistency"], "Atomic state change + event publish."],
    ["sd.webhooks", "Webhook ingestion", ["sd.idempotency", "sd.queues"], "Retries, signatures, ordering."],
    ["sd.caching", "Caching strategy", ["sd.consistency"], "TTL, invalidation, stampede protection."],
    ["sd.observability", "Observability", ["sd.queues"], "Metrics, traces, SLOs and alerting."],
    ["sd.billing_systems", "Billing system design", ["sd.webhooks", "sd.outbox"], "Invoices, ledgers, payment state machines."],
    ["sd.reconciliation", "Reconciliation pipelines", ["sd.billing_systems", "sd.replication"], "Matching two sources of truth at scale."],
  ],
  sql: [
    ["sql.select_filter", "SELECT & WHERE", [], "Projection, filtering, NULL semantics."],
    ["sql.joins", "Joins", ["sql.select_filter"], "Inner, left, anti and semi joins."],
    ["sql.aggregation", "GROUP BY & aggregation", ["sql.select_filter"], "Aggregates, HAVING, grouping sets."],
    ["sql.null_handling", "NULLs & dirty data", ["sql.select_filter"], "COALESCE, TRIM, casting mixed formats."],
    ["sql.ctes", "CTEs", ["sql.joins", "sql.aggregation"], "Composing queries with WITH."],
    ["sql.window_basics", "Window functions", ["sql.aggregation"], "ROW_NUMBER, LAG, running totals."],
    ["sql.dates", "Date & time logic", ["sql.null_handling"], "Truncation, time zones, period mapping."],
    ["sql.dedup", "Deduplication", ["sql.window_basics"], "Choosing a survivor row with windows."],
    ["sql.aging_buckets", "Aging buckets", ["sql.dates", "sql.ctes"], "CASE bucketing by days outstanding."],
    ["sql.cohort_queries", "Cohort queries", ["sql.window_basics", "sql.dates"], "Retention matrices in SQL."],
    ["sql.reconciliation", "Reconciliation queries", ["sql.ctes", "sql.dedup"], "Full outer joins and tie-out reports."],
    ["sql.performance", "Query performance", ["sql.ctes"], "Explain plans, predicate pushdown."],
  ],
  python: [
    ["py.basics", "Python fundamentals", [], "Types, comprehensions, functions."],
    ["py.pandas_io", "pandas I/O & selection", ["py.basics"], "Loading frames, loc/iloc, dtypes."],
    ["py.pandas_groupby", "groupby & pivot", ["py.pandas_io"], "Split-apply-combine and pivot tables."],
    ["py.cleaning", "Data cleaning", ["py.pandas_io"], "Parsing dates, currency strings, duplicates."],
    ["py.merge", "Merging frames", ["py.pandas_io"], "merge, indicator, validate=."],
    ["py.timeseries", "Time series", ["py.pandas_groupby"], "Resampling, rolling windows, period index."],
    ["py.plotting", "Plotting", ["py.pandas_groupby"], "matplotlib charts that make a point."],
    ["py.duckdb", "DuckDB from Python", ["py.pandas_io", "sql.select_filter"], "Querying frames and files with SQL."],
    ["py.stats", "Descriptive statistics", ["py.pandas_groupby"], "Distributions, outliers, z-scores."],
    ["py.monte_carlo", "Monte Carlo simulation", ["py.stats"], "Sampling distributions for cash forecasts."],
    ["py.regression", "Regression basics", ["py.stats"], "OLS, elasticity estimation, residuals."],
    ["py.forecasting", "Forecasting", ["py.timeseries", "py.regression"], "Baseline forecasts and error metrics."],
  ],
};

/** Deterministic pseudo-mastery so the graph looks lived-in. */
function masteryFor(i: number, depth: number): number {
  const base = Math.max(0.05, 0.85 - depth * 0.22);
  const wobble = ((i * 37) % 23) / 100;
  return Math.min(0.97, Math.max(0.04, base - 0.1 + wobble));
}

export function buildSkillGraph(): SkillGraph {
  const nodes: SkillNode[] = [];
  const depthOf: Record<string, number> = {};
  let i = 0;
  for (const pillar of Object.keys(SPECS) as Pillar[]) {
    for (const [id, title, prereqs, summary] of SPECS[pillar]) {
      const depth = prereqs.length === 0 ? 0 : 1 + Math.max(...prereqs.map((p) => depthOf[p] ?? 0));
      depthOf[id] = depth;
      nodes.push({
        id,
        pillar,
        title,
        prereqs,
        summary,
        tier: depth >= 3 ? "advanced" : "core",
        mastery: Number(masteryFor(i, depth).toFixed(2)),
        attempts: Math.max(0, 6 - depth * 2 + (i % 3)),
      });
      i += 1;
    }
  }
  const edges = nodes.flatMap((n) => n.prereqs.map((p) => ({ src: p, dst: n.id, source: p, target: n.id })));
  return { nodes, edges, pillars: pillarSummary(nodes) };
}

/** Same shape as the backend's per-pillar `{mean_mastery, nodes, mastered}` (mastered = p_known >= 0.95). */
export function pillarSummary(nodes: SkillNode[]): Partial<Record<Pillar, PillarSummary>> {
  const out: Partial<Record<Pillar, PillarSummary>> = {};
  for (const p of PILLARS) {
    const ns = nodes.filter((n) => n.pillar === p);
    if (ns.length === 0) continue;
    out[p] = {
      mean_mastery: Number((ns.reduce((s, n) => s + n.mastery, 0) / ns.length).toFixed(6)),
      nodes: ns.length,
      mastered: ns.filter((n) => n.mastery >= 0.95).length,
    };
  }
  return out;
}
