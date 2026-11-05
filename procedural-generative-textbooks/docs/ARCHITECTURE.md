# Axiom — Architecture Contract

Axiom is a local-first, procedurally generated case simulator that replaces static
textbooks across five pillars: Accounting & Finance, Operations & Strategy,
System Design, SQL, and Python. This document is the binding contract between the
modules. Every module is written against the interfaces here; if a module needs to
change an interface, it updates this file in the same commit.

Everything lives under `procedural-generative-textbooks/`. Nothing outside that folder
is read or written.

## 1. Three-tier philosophy

| Tier | Responsibility | Never does |
|---|---|---|
| **Research tier** (`engine/market_research.py` + `engine/benchmarks/*.json`) | Supplies real-world, source-cited distributions for every number the generator draws: industry ratios, SaaS/ops benchmarks, 10-K line items, forensic anomaly signatures, standard schema templates. Works fully offline; optional keyless live refresh from SEC EDGAR XBRL frames. | Generate narrative. Call an LLM. |
| **Deterministic tier** (`engine/procedural_world.py` + helpers) | From `(archetype, seed)` builds a mathematically sound company: chart of accounts, double-entry GL, trial balance, IS/BS/CF per period with `Assets = Liabilities + Equity` enforced to the cent, operations state, and a dirty relational DB (1k–50k rows) that mirrors the injected anomalies. Emits a ground-truth key with expected metrics and reference queries. Pure function of its inputs; same seed → byte-identical output. | Call an LLM. Use unseeded randomness. Produce numbers that violate an invariant. |
| **LLM tier** (`engine/llm_client.py`, `engine/pedagogy.py`) | Reads the deterministic world and writes the dossier (without revealing root cause), synthesizes textbook chapters, grades submissions against ground truth, plays the Socratic adversary, narrates quarter advancement. All outputs are structured JSON validated by pydantic. | Compute financial figures. Decide whether a numeric answer is correct (the deterministic grader does that first). |

## 2. Directory layout

```
procedural-generative-textbooks/
  README.md                       # quickstart, screenshots, design notes
  docs/
    ARCHITECTURE.md               # this file
    research/                     # Fable research outputs (cited)
      benchmarks.md
      archetypes.md
      pedagogy.md
  backend/
    pyproject.toml
    requirements.txt
    main.py                       # FastAPI app factory + uvicorn entry
    axiom/
      __init__.py
      config.py                   # paths, env, defaults (pydantic-settings style, no secrets on disk by default)
      models.py                   # ALL shared pydantic models (contract §4)
      persistence.py              # SQLite app DB: skill DAG, mastery, cases, sessions, llm usage
      schema.sql                  # DDL for the app DB
      skill_graph.py              # Skill DAG loader + Bayesian Knowledge Tracing
      skill_dag.json              # the taxonomy (nodes, prerequisites, pillars)
      engine/
        __init__.py
        market_research.py        # BenchmarkPool, IndustryProfile, EDGAR adapter
        benchmarks/
          industries.json         # per-industry ratio distributions (cited)
          saas_metrics.json
          operations.json
          forensic_patterns.json
          schema_templates.json
          line_items.json
        rng.py                    # SeededRNG wrapper (numpy Generator PCG64) + distribution helpers
        finance.py                # chart of accounts, journal engine, statements, invariants
        operations.py             # Little's Law, queueing, cohorts, bullwhip, unit economics
        datagen.py                # relational table builder + dirt injection; writes company.duckdb
        archetypes/
          __init__.py             # ARCHETYPE_REGISTRY
          base.py                 # Archetype ABC: params(), build(), ground_truth()
          working_capital_crunch.py
          abc_cost_distortion.py
          ma_synergies.py
          forensic_revenue.py
          toc_bottleneck.py
          cohort_collapse.py
          bullwhip.py
          pricing_elasticity.py
          idempotent_webhooks.py
          outbox_pattern.py
          replica_lag.py
          window_reconciliation.py
          cohort_matrix.py
          monte_carlo_cash.py
        procedural_world.py       # generate_world(archetype, seed, overrides) -> CaseWorld; CLI
        llm_client.py             # OpenAI-compatible + Anthropic adapters, JSON mode, usage tracking
        pedagogy.py               # prompts, case synthesizer, textbook, grader, adversary, quarter sim
        sandbox.py                # SQL (DuckDB read-only) + Python subprocess sandbox
      api/
        __init__.py
        routes_cases.py
        routes_sandbox.py
        routes_pedagogy.py
        routes_skills.py
        routes_settings.py
    tests/
      test_finance_invariants.py
      test_archetypes.py
      test_market_research.py
      test_skill_graph.py
      test_sandbox.py
      test_pedagogy_offline.py    # uses a fake LLM transport
      test_api.py
  frontend/
    package.json, vite.config.ts, tailwind.config.ts, tsconfig.json, index.html
    src/
      main.tsx, App.tsx
      api/client.ts               # typed fetch wrappers matching §5
      store/                      # zustand stores: session, case, settings, skills
      views/Dashboard.tsx         # skill graph + generate
      views/Workbench.tsx         # 4-pane IDE
      components/
        SkillGraph.tsx            # force/DAG layout, mastery colouring
        panes/Briefing.tsx        # dossier + textbook + mermaid
        panes/Sandbox.tsx         # SQL / Python / Design tabs (Monaco)
        panes/Deliverable.tsx     # memo + evidence linking
        panes/Adversary.tsx       # Socratic chat + quarter sim
        SettingsDrawer.tsx        # LLM config + prompt inspector + usage
        ...
  data/                           # runtime (gitignored): axiom.db, cases/<case_id>/company.duckdb
```

## 3. Runtime data

- **App DB**: `data/axiom.db` (SQLite). Tables: `skill_nodes`, `skill_edges`, `mastery`,
  `cases`, `case_artifacts` (dossier, textbook chapters), `submissions`, `adversary_turns`,
  `sim_quarters`, `llm_settings`, `llm_usage`.
- **Case DB**: `data/cases/<case_id>/company.duckdb`. One DuckDB file per generated case
  holding the company's relational tables. The sandbox opens it **read-only**.
  `world.json` next to it holds the serialized `CaseWorld` (ground truth included; the
  API never returns ground truth to the client until a submission is graded, and only the
  parts relevant to the grade).

## 4. Core models (`axiom/models.py`)

All models are pydantic v2. Money is `Decimal`-safe: internally stored as integer **cents**
(`int`) in the ledger and statements to make `Assets = Liabilities + Equity` exact; the API
serializes cents as integers and the frontend formats. Percentages are floats in `[0,1]`.

```python
Pillar = Literal["accounting_finance","operations_strategy","system_design","sql","python"]

class Benchmark(BaseModel):
    key: str                     # e.g. "saas.gross_churn_monthly"
    industry: str                # "saas" | "ecommerce" | "manufacturing" | "3pl" | "retail" | "any"
    metric: str
    unit: str                    # "ratio" | "pct" | "days" | "usd" | "count" | "x"
    p10: float; p25: float; p50: float; p75: float; p90: float
    distribution: Literal["lognormal","normal","beta","triangular","empirical"] = "empirical"
    source: str                  # citation text
    source_url: str | None
    as_of: str                   # "2024" etc
    notes: str = ""

class IndustryProfile(BaseModel):
    code: str; name: str; naics: str | None
    business_models: list[str]
    revenue_range_usd: tuple[int,int]
    benchmarks: dict[str, Benchmark]       # keyed by Benchmark.key
    line_items: list[str]                  # typical 10-K IS/BS captions
    schema_template: str                   # key into schema_templates.json
    narrative_hooks: list[str]             # things the LLM can use for realism

class Account(BaseModel):
    code: str                   # "1000"
    name: str
    type: Literal["asset","liability","equity","revenue","expense","contra_asset","contra_revenue"]
    normal_balance: Literal["debit","credit"]
    statement: Literal["BS","IS"]
    section: str                # "current_assets", "ppe", "current_liabilities", "cogs", "opex" ...
    parent: str | None = None

class JournalLine(BaseModel):
    account: str; debit_cents: int = 0; credit_cents: int = 0
    dimension: dict[str,str] = {}   # {"customer_id": "...", "sku": "...", "department": "..."}

class JournalEntry(BaseModel):
    id: str; date: date; period: str          # period "2024Q3"
    memo: str; source: str                    # "sales","purchasing","payroll","adjustment","anomaly:<id>"
    lines: list[JournalLine]                  # sum debits == sum credits (enforced)

class StatementLine(BaseModel): caption: str; account_codes: list[str]; cents: int; level: int = 0
class IncomeStatement(BaseModel): period: str; lines: list[StatementLine]; revenue_cents: int; gross_profit_cents: int; operating_income_cents: int; net_income_cents: int
class BalanceSheet(BaseModel): period: str; lines: list[StatementLine]; total_assets_cents: int; total_liabilities_cents: int; total_equity_cents: int
class CashFlowStatement(BaseModel): period: str; operating_cents: int; investing_cents: int; financing_cents: int; net_change_cents: int; beginning_cash_cents: int; ending_cash_cents: int; lines: list[StatementLine]
class FinancialStatements(BaseModel):
    periods: list[str]
    income: list[IncomeStatement]; balance: list[BalanceSheet]; cash_flow: list[CashFlowStatement]
    ratios: dict[str, dict[str,float]]        # period -> {dso, dpo, dio, ccc, gross_margin, current_ratio, ...}

class FinanceState(BaseModel):
    chart_of_accounts: list[Account]
    journal: list[JournalEntry]
    trial_balance: dict[str, dict[str,int]]   # period -> account code -> closing balance cents (signed by normal balance)
    statements: FinancialStatements
    invariants: list[InvariantCheck]          # every check and its result; all must pass

class InvariantCheck(BaseModel): name: str; passed: bool; detail: str

class OperationsState(BaseModel):
    stations: list[Station]                   # ToC: name, capacity_per_hour, utilization, wip, cycle_time
    queueing: dict[str,float]                 # lambda, mu, rho, L, W (Little's Law), Lq, Wq
    unit_economics: dict[str,float]           # cac, ltv, payback_months, gross_margin, arpa, churn_monthly
    cohorts: list[Cohort]                     # acquisition_period, size, retention curve, revenue curve
    supply_chain: dict[str, Any]              # lead_time_dist, order_up_to, safety_stock, bullwhip_ratio by tier
    pricing: dict[str, Any]                   # elasticity by segment, price ladder, discount leakage
    kpis_by_period: dict[str, dict[str,float]]

class TableSpec(BaseModel):
    name: str; ddl: str; columns: list[ColumnSpec]; row_count: int
    primary_key: list[str]; foreign_keys: list[tuple[str,str,str]]  # (col, ref_table, ref_col)
    dirt: list[str]                           # which dirt rules were applied ("nulls:ship_date", "dup_keys", "tz_mismatch", ...)
    sample_rows: list[dict[str, Any]]         # first 5 rows for the schema explorer

class ColumnSpec(BaseModel): name: str; type: str; nullable: bool; description: str

class DatabaseSpec(BaseModel):
    engine: Literal["duckdb"] = "duckdb"
    path: str                                 # relative to data/
    tables: list[TableSpec]
    erd_mermaid: str                          # erDiagram for the schema explorer
    total_rows: int

class Anomaly(BaseModel):
    id: str                                   # "A1"
    kind: str                                 # from forensic_patterns.json keys, e.g. "channel_stuffing"
    pillar: Pillar
    title: str
    mechanism: str                            # how it was injected, in engineering terms
    periods: list[str]
    affected_accounts: list[str]; affected_tables: list[str]
    magnitude: dict[str, float]               # e.g. {"revenue_inflation_pct": 0.11}
    evidence_trail: list[str]                 # what a diligent analyst would notice, in order
    reference_sql: list[ReferenceQuery]       # queries that expose it, with expected result signature
    severity: Literal["low","medium","high","critical"]

class ReferenceQuery(BaseModel): name: str; sql: str; expected: dict[str, Any]; tolerance: float = 0.02

class ExpectedMetric(BaseModel):
    key: str; value: float; unit: str; tolerance_abs: float | None; tolerance_rel: float | None
    derivation: str                           # how it is computed; shown to the user after grading

class GroundTruth(BaseModel):
    root_cause: str                           # one paragraph, never shown before grading
    anomalies: list[Anomaly]
    red_herrings: list[str]                   # plausible-but-wrong explanations the adversary can push
    expected_metrics: list[ExpectedMetric]
    reference_solution: ReferenceSolution     # sql, python, memo bullets, design diagram (mermaid)
    decision_levers: list[Lever]              # what the user can pull in the quarter sim

class Lever(BaseModel): key: str; label: str; min: float; max: float; default: float; unit: str; effect: str

class Stakeholder(BaseModel): name: str; role: str; agenda: str; disposition: str; knows: list[str]; hides: list[str]
class TimelineEvent(BaseModel): period: str; headline: str; detail: str; is_red_herring: bool

class DossierSeed(BaseModel):
    reveal: list[str]                         # facts the LLM MUST state
    conceal: list[str]                        # facts the LLM MUST NOT state (root cause, anomaly ids)
    stakeholders: list[Stakeholder]
    timeline: list[TimelineEvent]
    artifacts: list[str]                      # "board_deck_excerpt", "slack_thread", "architecture_diagram" ...
    architecture_mermaid: str | None          # for system-design archetypes

class SimulationState(BaseModel):
    current_period: str
    history: list[SimQuarter]                 # decisions + resulting kpis
    model: dict[str, Any]                     # deterministic transition parameters

class SimQuarter(BaseModel): period: str; decisions: dict[str,float]; kpis: dict[str,float]; narrative: str | None

class CaseWorld(BaseModel):
    case_id: str; seed: int; archetype: str; version: str
    pillars: list[Pillar]; skill_nodes: list[str]
    difficulty: Literal["foundation","practitioner","principal"]
    company: CompanyProfile
    industry: IndustryProfile
    periods: list[str]
    finance: FinanceState
    operations: OperationsState
    database: DatabaseSpec
    ground_truth: GroundTruth
    dossier_seed: DossierSeed
    simulation: SimulationState
    generated_at: str

class CompanyProfile(BaseModel):
    name: str; ticker_like: str; industry_code: str; business_model: str; hq: str; founded: int
    fiscal_year_end: str; headcount: int; stage: str; revenue_ttm_cents: int; description: str
```

`CaseWorld.public_view()` returns the same object with `ground_truth` removed and
`dossier_seed.conceal` removed. Only the public view ever crosses the API before grading.

## 5. API contract (FastAPI, `/api/v1`)

| Method | Path | Body → Response |
|---|---|---|
| GET | `/health` | → `{status, version, llm_configured: bool}` |
| GET | `/archetypes` | → `[{key, title, pillars, skill_nodes, difficulty_range, summary}]` |
| POST | `/cases/generate` | `{archetype?: str, seed?: int, difficulty?, target_skills?: [str]}` → `CasePublic` (if `archetype` omitted, the skill graph picks the intersection targeting lowest-mastery nodes whose prerequisites are met) |
| GET | `/cases` | → `[CaseSummary]` |
| GET | `/cases/{id}` | → `CasePublic` |
| GET | `/cases/{id}/schema` | → `DatabaseSpec` |
| POST | `/cases/{id}/dossier` | `{force?: bool}` → `Dossier` (LLM; cached in `case_artifacts`) |
| POST | `/cases/{id}/textbook` | `{skill_node: str}` → `TextbookChapter` (LLM; cached) |
| POST | `/sandbox/sql` | `{case_id, sql, limit?: 500}` → `{columns, rows, row_count, elapsed_ms, truncated, error?}` |
| POST | `/sandbox/python` | `{case_id, code, timeout_s?: 20}` → `{stdout, stderr, result_json?, figures: [base64 png], elapsed_ms, error?}` |
| POST | `/cases/{id}/submit` | `Submission` → `GradeReport` (deterministic check + LLM rubric) |
| POST | `/cases/{id}/adversary` | `{submission_id, message}` → `AdversaryTurn` |
| POST | `/cases/{id}/advance` | `{submission_id, decisions: {lever: value}}` → `SimQuarter` |
| GET | `/skills/graph` | → `{nodes: [{id, pillar, title, mastery, prereqs, attempts}], edges}` |
| POST | `/skills/reset` | → `{ok}` |
| GET/PUT | `/settings/llm` | `LLMSettings` (api key stored only in app DB, never logged; GET returns masked key) |
| POST | `/settings/llm/test` | → `{ok, model, latency_ms, error?}` |
| GET | `/settings/usage` | → `{by_model: {...}, total_tokens, total_cost_usd_est}` |
| GET | `/settings/prompts` | → `[{name, template}]` (prompt inspector) |

```python
class Submission(BaseModel):
    case_id: str
    memo_markdown: str                    # executive memo / root cause diagnosis
    metrics: dict[str, float]             # user-asserted key metrics (keys from the case's "asks")
    sql_evidence: list[{name, sql, result_preview}]
    python_evidence: list[{name, code, stdout_tail, result_json}]
    design_mermaid: str | None
    financial_model: dict[str, Any] | None

class GradeReport(BaseModel):
    submission_id: str
    deterministic: DeterministicGrade     # per expected metric: submitted, expected, within_tolerance; reference query signatures matched
    rubric: RubricGrade                   # LLM: dimensions (technical_execution, business_judgment, communication, risk_awareness) each 0-10 with evidence quotes
    skill_updates: list[{node, before, after}]
    revealed: {root_cause, anomalies: [...], reference_solution}   # unlocked after grading
    adversary_opening: str                # first Socratic challenge

class LLMSettings(BaseModel):
    provider: Literal["openrouter","openai","anthropic","local"]
    base_url: str; api_key: str
    fast_model: str; deep_model: str
    temperature_fast: float = 0.7; temperature_deep: float = 0.2
    max_tokens: int = 4096
```

## 6. Determinism rules

1. One `SeededRNG` per world, created from `(archetype, seed, version)`; sub-streams are
   derived with `rng.spawn("finance")`, `rng.spawn("datagen")`, so adding a draw in one
   module never changes another module's output.
2. No wall-clock reads inside generation. `generated_at` is set after generation.
3. All floating money is rounded to cents at the journal line and never re-summed from floats.
4. Every world passes `finance.invariants` before it is written. Failing a check raises
   `WorldInvariantError`; it is a bug, not a case.
5. `generate_world(archetype, seed)` twice → identical `world.json` (test enforced).

## 7. Invariants (`finance.py` must check all)

- Each journal entry: Σdebits == Σcredits.
- Each period: trial balance Σdebits == Σcredits.
- Balance sheet: `total_assets == total_liabilities + total_equity` (exact cents) every period.
- Retained earnings roll-forward: `RE_t = RE_{t-1} + NI_t − dividends_t`.
- Cash flow: `ending_cash_t == BS cash_t`; `beginning_cash_t == ending_cash_{t-1}`; indirect
  method ties NI + non-cash + ΔWC to CFO.
- Sub-ledger ties: Σ open AR in `invoices` table == BS AR (before anomaly-designed breaks, and
  the ground truth states exactly which breaks exist and by how much).
- Inventory roll-forward: `begin + purchases − COGS ± adjustments = end`.

## 8. Dirty-data rules (`datagen.py`)

Each table declares which dirt rules apply. Rules are deterministic and recorded in
`TableSpec.dirt`: `nulls:<col>:<rate>`, `dup_keys:<rate>`, `tz_mixed:<col>` (some rows UTC,
some local with offset string), `format_mixed:<col>` (dates as ISO/US, currency with symbols),
`case_mixed:<col>`, `whitespace:<col>`, `orphan_fk:<col>:<rate>`, `skew:<col>:<pareto_alpha>`,
`late_arriving:<col>` (updated_at < created_at), `unit_mixed:<col>` (cents vs dollars).
Reference queries in ground truth must still produce the expected signature after dirt, so dirt
is applied **before** reference expectations are computed.

## 9. LLM rules

- Transport: OpenAI chat-completions format for `openrouter | openai | local`; native
  Messages API for `anthropic`. JSON output via `response_format` when supported, else a
  strict "return only JSON" instruction + fenced-JSON extractor + one repair retry.
- Every call records `{model, prompt_tokens, completion_tokens, latency_ms, purpose}` in
  `llm_usage`.
- Prompts live in `pedagogy.py` as named templates and are exposed read-only by
  `/settings/prompts`.
- The grader receives the deterministic grade *and* the ground truth; the dossier writer
  receives `dossier_seed` only (reveal/conceal enforced by a post-check that scans the output
  for concealed strings and regenerates once if leaked).
- A `FakeTransport` lets every pedagogy path be unit-tested offline.

## 10. Skill graph

`skill_dag.json` defines ~60 nodes across 5 pillars with prerequisite edges. Mastery is
Bayesian Knowledge Tracing per node: `p_known` updated per graded observation with
`(p_init, p_transit, p_slip, p_guess)` per node; observation = pass/fail of the rubric
dimension(s) mapped to that node. Case selection: candidate archetypes scored by
Σ(1 − p_known) over their nodes, restricted to nodes whose prerequisites have `p_known ≥ 0.6`,
ties broken by the seeded RNG so the same mastery state proposes the same case.

## 11. Additional API response models (authoritative for the frontend)

```python
class CaseSummary(BaseModel): case_id: str; archetype: str; company_name: str; industry: str; difficulty: str; pillars: list[Pillar]; created_at: str; status: Literal["generated","briefed","submitted","graded"]

class Dossier(BaseModel):
    title: str; situation: str; background_markdown: str
    stakeholder_messages: list[{from_name: str, role: str, channel: Literal["email","slack","memo","board_deck"], body: str}]
    the_ask: str; deliverables: list[str]; data_room_index: list[str]

class TextbookChapter(BaseModel): skill_node: str; title: str; markdown: str; self_checks: list[{question: str, answer: str}]

class RubricDimension(BaseModel): score: float; evidence: list[str]; gaps: list[str]
class RubricGrade(BaseModel): dimensions: dict[str, RubricDimension]; overall: float; misdiagnosis: bool; strongest_point: str; weakest_point: str; next_drill_nodes: list[str]
class DeterministicGrade(BaseModel):
    metric_results: list[{key, submitted: float|None, expected: float, unit, within_tolerance: bool, tolerance_abs, tolerance_rel, derivation}]
    query_signature_matches: list[{name, matched: bool, detail}]
    accuracy: float            # fraction of expected metrics within tolerance
class AdversaryTurn(BaseModel): persona: str; message: str; challenge_type: str; cites: list[str]; concedes: bool; turn_index: int
class LLMSettingsPublic(BaseModel): provider; base_url; api_key_masked; fast_model; deep_model; temperature_fast; temperature_deep; max_tokens; configured: bool
class LLMUsageSummary(BaseModel): by_model: dict[str, {prompt_tokens, completion_tokens, calls, cost_usd_est}]; total_tokens: int; total_cost_usd_est: float
```

The case "asks" (which metric keys the user must fill in `Submission.metrics`) are exposed in
`CasePublic.asks: list[{key, label, unit, hint}]`; they are derived from
`GroundTruth.expected_metrics` without values.

Streaming: `/cases/{id}/dossier`, `/cases/{id}/textbook`, `/cases/{id}/adversary` also accept
`?stream=1` and then return `text/event-stream` with `data: {"delta": "..."}` chunks followed by
`data: {"done": true, "payload": <final JSON>}`. Non-stream is the default and always works.
