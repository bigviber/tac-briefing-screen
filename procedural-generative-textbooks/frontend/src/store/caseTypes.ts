import type {
  AdversaryTurn,
  CasePublic,
  SessionState,
  FinancialModel,
  PythonEvidence,
  PythonResult,
  SqlEvidence,
  SqlResult,
} from "../api/types";

export interface SqlHistoryItem {
  sql: string;
  at: number;
  rowCount: number;
  ms: number;
  error?: string | null;
}

/** Per-case editor state that is persisted to localStorage. */
export interface PersistedCaseState {
  sql: string;
  python: string;
  design: string;
  model: FinancialModel | null;
  sqlHistory: SqlHistoryItem[];
  memo: string;
  /** Raw input strings keyed by ask key; parsed on submit. */
  metrics: Record<string, string>;
  sqlEvidence: SqlEvidence[];
  pythonEvidence: PythonEvidence[];
  designAttached: boolean;
  modelAttached: boolean;
}

export type ThreadItem =
  | { role: "user"; message: string; at: number }
  | { role: "system"; message: string; at: number }
  | { role: "adversary"; turn: AdversaryTurn; streaming?: boolean };

/** Placeholder adversary turn (streaming, or an opening without a stored record). */
export function blankTurn(patch: Partial<AdversaryTurn> = {}): AdversaryTurn {
  return { persona: "", message: "", challenge_type: "probe", cites: [], concedes: false, turn_index: -1, rung: 1, ...patch };
}

/**
 * Rebuild the chat from `GET /cases/{id}/session`. `thread` is the ordered conversation
 * (user and adversary messages); adversary entries are enriched with the full
 * `adversary_turns` record (challenge type, cites, rung) matched by `turn_index`.
 */
export function threadFromSession(s: SessionState): ThreadItem[] {
  const turns = new Map(s.adversary_turns.map((t) => [t.turn_index, t]));
  const at = (iso: string) => {
    const t = Date.parse(iso);
    return Number.isNaN(t) ? 0 : t;
  };
  return s.thread.map((m): ThreadItem => {
    if (m.role === "adversary") {
      const turn = turns.get(m.turn_index) ?? blankTurn({ persona: m.persona ?? "", message: m.message, turn_index: m.turn_index });
      return { role: "adversary", turn };
    }
    return { role: m.role, message: m.message, at: at(m.created_at) };
  });
}

export const DEFAULT_SQL = `-- Quarterly open AR vs revenue. ⌘↵ runs the query.
WITH inv AS (
  SELECT DISTINCT invoice_id, customer_id, status,
         CASE WHEN amount_cents < 10000 THEN amount_cents * 100 ELSE amount_cents END AS amount_cents,
         TRY_STRPTIME(issue_date, ['%Y-%m-%d', '%m/%d/%Y'])::DATE AS issued
  FROM invoices
)
SELECT 'Q' || quarter(issued) AS period,
       SUM(amount_cents) / 100.0 AS billed_usd,
       SUM(amount_cents) FILTER (WHERE status = 'open') / 100.0 AS open_ar_usd,
       COUNT(*) AS invoice_count
FROM inv
GROUP BY 1
ORDER BY 1;
`;

export const DEFAULT_PYTHON = `# Every case table is preloaded as a pandas DataFrame:
#   tables["invoices"], tables["payments"], tables["credit_memos"], ...
# \`con\` is a read-only DuckDB connection to the case database.
# Assign a JSON-serialisable value to \`result\` to return it as result_json;
# matplotlib figures are captured automatically.
import pandas as pd
import matplotlib.pyplot as plt

inv = tables["invoices"].drop_duplicates("invoice_id")
print(f"invoices: {len(tables['invoices']):,} rows -> {len(inv):,} after dedup")

dso = con.sql("""
    SELECT period, SUM(debit_cents - credit_cents) / 100.0 AS ar_change_usd
    FROM gl_entries WHERE account_code = '1100'
    GROUP BY period ORDER BY period
""").df()
print(dso)

dso.plot(x="period", y="ar_change_usd", kind="bar", legend=False)
plt.title("Change in AR by quarter")

result = {"rows": len(inv), "dup_invoice_ids": int(tables["invoices"].invoice_id.duplicated().sum())}
`;

export const DEFAULT_DESIGN = `flowchart LR
  Billing[Billing service] -->|invoice.created| AR[(AR sub-ledger)]
  AR --> Aging[Daily aging job]
  Aging -->|90+ days| Dunning[Dunning workflow]
  Dunning -->|hold requested| Approval{Finance approval}
  Approval -->|approved| Hold[Pause dunning ≤ 14d]
  Approval -->|rejected| Escalate[Escalate to CFO]
`;

function nextPeriod(p: string): string {
  const m = /^(\d{4})Q([1-4])$/.exec(p);
  if (!m) return `${p}+1`;
  const y = Number(m[1]);
  const q = Number(m[2]);
  return q === 4 ? `${y + 1}Q1` : `${y}Q${q + 1}`;
}

export function defaultModel(c: CasePublic): FinancialModel {
  const actual = c.periods.slice(-2);
  const fc: string[] = [];
  let p = c.periods.at(-1) ?? "2024Q4";
  for (let i = 0; i < 4; i += 1) {
    p = nextPeriod(p);
    fc.push(`${p}E`);
  }
  const periods = [...actual, ...fc];
  const byPeriod = new Map(c.finance.statements.income.map((s) => [s.period, Math.round(s.revenue_cents / 100)]));
  const revenue = periods.map((x) => byPeriod.get(x) ?? null);
  const captions = ["Revenue", "Collections", "Ending AR", "Operating cash burn", "Ending cash"];
  return {
    unit: "usd",
    periods,
    rows: captions.map((caption, i) => ({ caption, values: i === 0 ? revenue : periods.map(() => null) })),
  };
}

/** True for the demo (mock) schema the hand-written DEFAULT_SQL / DEFAULT_PYTHON target. */
function isDemoSchema(c: CasePublic): boolean {
  const names = new Set(c.database.tables.map((t) => t.name));
  const inv = c.database.tables.find((t) => t.name === "invoices");
  return names.has("gl_entries") && !!inv?.columns.some((col) => col.name === "issue_date");
}

/** Starter table: invoices if present, else the largest table. */
function starterTable(c: CasePublic): string | null {
  const tables = c.database.tables;
  if (tables.length === 0) return null;
  return (tables.find((t) => t.name === "invoices") ?? [...tables].sort((a, b) => b.row_count - a.row_count)[0]).name;
}

export function starterSql(c: CasePublic): string {
  if (isDemoSchema(c)) return DEFAULT_SQL;
  const t = starterTable(c);
  const list = c.database.tables.map((x) => `--   ${x.name} (${x.row_count.toLocaleString("en-US")} rows)`).join("\n");
  return `-- ${c.company.name}: ${c.database.tables.length} tables in a read-only DuckDB. ⌘↵ runs the query.\n${list}\n${t ? `SELECT *\nFROM ${t}\nLIMIT 100;\n` : "SHOW TABLES;\n"}`;
}

export function starterPython(c: CasePublic): string {
  if (isDemoSchema(c)) return DEFAULT_PYTHON;
  const t = starterTable(c) ?? "";
  return `# Every case table is available as a lazily loaded pandas DataFrame: tables["${t}"], ...
# \`con\` is a read-only DuckDB connection to the case database.
# Assign a JSON-serialisable value to \`result\`; matplotlib figures are captured automatically.
import pandas as pd

df = tables["${t}"]
print(df.shape)
print(df.head(10).to_string())

result = {"table": "${t}", "rows": int(len(df)), "columns": list(df.columns)}
`;
}

export function emptyPersisted(c: CasePublic): PersistedCaseState {
  return {
    sql: starterSql(c),
    python: starterPython(c),
    design: c.dossier_seed.architecture_mermaid ?? DEFAULT_DESIGN,
    model: defaultModel(c),
    sqlHistory: [],
    memo: "",
    metrics: {},
    sqlEvidence: [],
    pythonEvidence: [],
    designAttached: false,
    modelAttached: false,
  };
}

export interface RunState {
  sqlResult: SqlResult | null;
  sqlRunning: boolean;
  pyResult: PythonResult | null;
  pyRunning: boolean;
}
