/**
 * TypeScript mirrors of the Axiom contract (docs/ARCHITECTURE.md §4, §5, §11).
 * Money values are integer cents; percentages are floats in [0, 1].
 * The client only ever receives the public view of a CaseWorld (no ground_truth).
 */

export type Pillar =
  | "accounting_finance"
  | "operations_strategy"
  | "system_design"
  | "sql"
  | "python";

export const PILLARS: readonly Pillar[] = [
  "accounting_finance",
  "operations_strategy",
  "system_design",
  "sql",
  "python",
] as const;

export type Difficulty = "foundation" | "practitioner" | "principal";
export const DIFFICULTIES: readonly Difficulty[] = ["foundation", "practitioner", "principal"];

/** JSON-compatible value used where the contract says `Any`. */
export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };
export type JsonObject = { [k: string]: JsonValue };

// ---------------------------------------------------------------- §4 research tier

export interface Benchmark {
  key: string;
  industry: string;
  metric: string;
  unit: string;
  p10: number;
  p25: number;
  p50: number;
  p75: number;
  p90: number;
  distribution: "lognormal" | "normal" | "beta" | "triangular" | "empirical";
  source: string;
  source_url: string | null;
  as_of: string;
  notes: string;
}

export interface IndustryProfile {
  code: string;
  name: string;
  naics: string | null;
  business_models: string[];
  revenue_range_usd: [number, number];
  benchmarks: Record<string, Benchmark>;
  line_items: string[];
  schema_template: string;
  narrative_hooks: string[];
}

// ---------------------------------------------------------------- §4 finance

export type AccountType =
  | "asset"
  | "liability"
  | "equity"
  | "revenue"
  | "expense"
  | "contra_asset"
  | "contra_revenue";

export interface Account {
  code: string;
  name: string;
  type: AccountType;
  normal_balance: "debit" | "credit";
  statement: "BS" | "IS";
  section: string;
  parent: string | null;
}

export interface JournalLine {
  account: string;
  debit_cents: number;
  credit_cents: number;
  dimension: Record<string, string>;
}

export interface JournalEntry {
  id: string;
  date: string;
  period: string;
  memo: string;
  source: string;
  lines: JournalLine[];
}

export interface StatementLine {
  caption: string;
  account_codes: string[];
  cents: number;
  level: number;
}

export interface IncomeStatement {
  period: string;
  lines: StatementLine[];
  revenue_cents: number;
  gross_profit_cents: number;
  operating_income_cents: number;
  net_income_cents: number;
}

export interface BalanceSheet {
  period: string;
  lines: StatementLine[];
  total_assets_cents: number;
  total_liabilities_cents: number;
  total_equity_cents: number;
}

export interface CashFlowStatement {
  period: string;
  operating_cents: number;
  investing_cents: number;
  financing_cents: number;
  net_change_cents: number;
  beginning_cash_cents: number;
  ending_cash_cents: number;
  lines: StatementLine[];
}

export interface FinancialStatements {
  periods: string[];
  income: IncomeStatement[];
  balance: BalanceSheet[];
  cash_flow: CashFlowStatement[];
  ratios: Record<string, Record<string, number>>;
}

export interface InvariantCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface FinanceState {
  chart_of_accounts: Account[];
  journal: JournalEntry[];
  trial_balance: Record<string, Record<string, number>>;
  statements: FinancialStatements;
  invariants: InvariantCheck[];
}

// ---------------------------------------------------------------- §4 operations

/** §4 names Station/Cohort without fields; these follow the inline comments. */
export interface Station {
  name: string;
  capacity_per_hour: number;
  utilization: number;
  wip: number;
  cycle_time: number;
}

export interface Cohort {
  acquisition_period: string;
  size: number;
  retention: number[];
  revenue: number[];
}

export interface OperationsState {
  stations: Station[];
  queueing: Record<string, number>;
  unit_economics: Record<string, number>;
  cohorts: Cohort[];
  supply_chain: JsonObject;
  pricing: JsonObject;
  kpis_by_period: Record<string, Record<string, number>>;
}

// ---------------------------------------------------------------- §4 database

export interface ColumnSpec {
  name: string;
  type: string;
  nullable: boolean;
  description: string;
}

export type SampleRow = Record<string, JsonValue>;

export interface TableSpec {
  name: string;
  ddl: string;
  columns: ColumnSpec[];
  row_count: number;
  primary_key: string[];
  foreign_keys: [string, string, string][];
  dirt: string[];
  sample_rows: SampleRow[];
}

export interface DatabaseSpec {
  engine: "duckdb";
  path: string;
  tables: TableSpec[];
  erd_mermaid: string;
  total_rows: number;
}

// ---------------------------------------------------------------- §4 narrative + sim

export interface Lever {
  key: string;
  label: string;
  min: number;
  max: number;
  default: number;
  unit: string;
  effect: string;
}

export interface Stakeholder {
  name: string;
  role: string;
  agenda: string;
  disposition: string;
  knows: string[];
  hides: string[];
}

export interface TimelineEvent {
  period: string;
  headline: string;
  detail: string;
  is_red_herring: boolean;
}

/** Public dossier seed: `conceal` is stripped by `CaseWorld.public_view()`. */
export interface DossierSeedPublic {
  reveal: string[];
  stakeholders: Stakeholder[];
  timeline: TimelineEvent[];
  artifacts: string[];
  architecture_mermaid: string | null;
}

export interface SimQuarter {
  period: string;
  decisions: Record<string, number>;
  kpis: Record<string, number>;
  narrative: string | null;
}

export interface SimulationState {
  current_period: string;
  history: SimQuarter[];
  model: JsonObject;
}

export interface CompanyProfile {
  name: string;
  ticker_like: string;
  industry_code: string;
  business_model: string;
  hq: string;
  founded: number;
  fiscal_year_end: string;
  headcount: number;
  stage: string;
  revenue_ttm_cents: number;
  description: string;
}

export interface Ask {
  key: string;
  label: string;
  unit: string;
  hint: string;
}

/** CaseWorld.public_view() + asks (§11). `levers` is an optional extension; see README contract gaps. */
export interface CasePublic {
  case_id: string;
  seed: number;
  archetype: string;
  version: string;
  pillars: Pillar[];
  skill_nodes: string[];
  difficulty: Difficulty;
  company: CompanyProfile;
  industry: IndustryProfile;
  periods: string[];
  finance: FinanceState;
  operations: OperationsState;
  database: DatabaseSpec;
  dossier_seed: DossierSeedPublic;
  simulation: SimulationState;
  generated_at: string;
  asks: Ask[];
  levers?: Lever[];
  status?: CaseStatus;
}

export type CaseStatus = "generated" | "briefed" | "submitted" | "graded";

export interface CaseSummary {
  case_id: string;
  archetype: string;
  company_name: string;
  industry: string;
  difficulty: string;
  pillars: Pillar[];
  created_at: string;
  status: CaseStatus;
}

// ---------------------------------------------------------------- §5 / §11 endpoints

export interface Health {
  status: string;
  version: string;
  llm_configured: boolean;
}

export interface ArchetypeInfo {
  key: string;
  title: string;
  pillars: Pillar[];
  skill_nodes: string[];
  difficulty_range: Difficulty[];
  summary: string;
}

export interface GenerateRequest {
  archetype?: string;
  seed?: number;
  difficulty?: Difficulty;
  target_skills?: string[];
}

export type StakeholderChannel = "email" | "slack" | "memo" | "board_deck";

export interface StakeholderMessage {
  from_name: string;
  role: string;
  channel: StakeholderChannel;
  body: string;
}

export interface Dossier {
  title: string;
  situation: string;
  background_markdown: string;
  stakeholder_messages: StakeholderMessage[];
  the_ask: string;
  deliverables: string[];
  data_room_index: string[];
}

export interface SelfCheck {
  question: string;
  answer: string;
}

export interface TextbookChapter {
  skill_node: string;
  title: string;
  markdown: string;
  self_checks: SelfCheck[];
}

export interface SqlRequest {
  case_id: string;
  sql: string;
  limit?: number;
}

export type Cell = string | number | boolean | null;

export interface SqlResult {
  columns: string[];
  /** Optional DuckDB type names, parallel to `columns` (contract gap: not in §5). */
  column_types?: string[];
  rows: Cell[][];
  row_count: number;
  elapsed_ms: number;
  truncated: boolean;
  error?: string | null;
}

export interface PythonRequest {
  case_id: string;
  code: string;
  timeout_s?: number;
}

export interface PythonResult {
  stdout: string;
  stderr: string;
  result_json?: JsonValue;
  figures: string[];
  elapsed_ms: number;
  error?: string | null;
}

export interface SqlEvidence {
  name: string;
  sql: string;
  result_preview: string;
}

export interface PythonEvidence {
  name: string;
  code: string;
  stdout_tail: string;
  result_json: JsonValue;
}

/** Formula-free financial model: rows = captions, columns = periods. */
export interface FinancialModel {
  periods: string[];
  rows: { caption: string; values: (number | null)[] }[];
  unit: string;
}

export interface Submission {
  case_id: string;
  memo_markdown: string;
  metrics: Record<string, number>;
  sql_evidence: SqlEvidence[];
  python_evidence: PythonEvidence[];
  design_mermaid: string | null;
  financial_model: FinancialModel | null;
}

export interface MetricResult {
  key: string;
  submitted: number | null;
  expected: number;
  unit: string;
  within_tolerance: boolean;
  tolerance_abs: number | null;
  tolerance_rel: number | null;
  derivation: string;
}

export interface QuerySignatureMatch {
  name: string;
  matched: boolean;
  detail: string;
}

export interface DeterministicGrade {
  metric_results: MetricResult[];
  query_signature_matches: QuerySignatureMatch[];
  accuracy: number;
}

export interface RubricDimension {
  score: number;
  evidence: string[];
  gaps: string[];
}

export interface RubricGrade {
  dimensions: Record<string, RubricDimension>;
  overall: number;
  misdiagnosis: boolean;
  strongest_point: string;
  weakest_point: string;
  next_drill_nodes: string[];
}

export interface SkillUpdate {
  node: string;
  before: number;
  after: number;
}

export interface RevealedAnomaly {
  id: string;
  kind: string;
  pillar: Pillar;
  title: string;
  mechanism: string;
  periods: string[];
  severity: "low" | "medium" | "high" | "critical";
  evidence_trail: string[];
}

export interface ReferenceSolution {
  sql?: string;
  python?: string;
  memo_bullets?: string[];
  design_mermaid?: string | null;
}

export interface Revealed {
  root_cause: string;
  anomalies: RevealedAnomaly[];
  reference_solution: ReferenceSolution;
}

export interface GradeReport {
  submission_id: string;
  deterministic: DeterministicGrade;
  rubric: RubricGrade;
  skill_updates: SkillUpdate[];
  revealed: Revealed;
  adversary_opening: string;
}

export interface AdversaryRequest {
  submission_id: string;
  message: string;
}

export interface AdversaryTurn {
  persona: string;
  message: string;
  challenge_type: string;
  cites: string[];
  concedes: boolean;
  turn_index: number;
}

export interface AdvanceRequest {
  submission_id: string;
  decisions: Record<string, number>;
}

export interface SkillNode {
  id: string;
  pillar: Pillar;
  title: string;
  /** p_known in [0,1]. */
  mastery: number;
  prereqs: string[];
  attempts: number;
  /** Optional one-line description (contract gap: not in §5). */
  summary?: string;
}

export interface SkillEdge {
  source: string;
  target: string;
}

export interface SkillGraph {
  nodes: SkillNode[];
  edges: SkillEdge[];
}

export type Provider = "openrouter" | "openai" | "anthropic" | "local";
export const PROVIDERS: readonly Provider[] = ["openrouter", "openai", "anthropic", "local"];

export interface LLMSettings {
  provider: Provider;
  base_url: string;
  api_key: string;
  fast_model: string;
  deep_model: string;
  temperature_fast: number;
  temperature_deep: number;
  max_tokens: number;
}

export interface LLMSettingsPublic {
  provider: Provider;
  base_url: string;
  api_key_masked: string;
  fast_model: string;
  deep_model: string;
  temperature_fast: number;
  temperature_deep: number;
  max_tokens: number;
  configured: boolean;
}

export interface LLMTestResult {
  ok: boolean;
  model: string;
  latency_ms: number;
  error?: string | null;
}

export interface ModelUsage {
  prompt_tokens: number;
  completion_tokens: number;
  calls: number;
  cost_usd_est: number;
}

export interface LLMUsageSummary {
  by_model: Record<string, ModelUsage>;
  total_tokens: number;
  total_cost_usd_est: number;
}

export interface PromptTemplate {
  name: string;
  template: string;
}
