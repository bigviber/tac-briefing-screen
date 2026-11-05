import type { Cell, JsonValue, SqlRequest, SqlResult, TableSpec } from "../api/types";
import { TABLES } from "./fixtures/schema";
import { PERIODS } from "./fixtures/case";

const DSO = [44, 46, 49, 53, 58, 64, 71, 78];

function toCell(v: JsonValue): Cell {
  if (v === null || typeof v === "string" || typeof v === "number" || typeof v === "boolean") return v;
  return JSON.stringify(v);
}

/** Vary a sample value deterministically so expanded rows look plausible. */
function vary(v: JsonValue, row: number, col: number): Cell {
  if (typeof v === "number") {
    if (Number.isInteger(v) && Math.abs(v) > 1000) return Math.round(v * (0.6 + ((row * 7919 + col * 31) % 97) / 100));
    return v;
  }
  if (typeof v === "string") {
    const m = /^([A-Z]+-)(\d+)$/.exec(v);
    if (m) return `${m[1]}${(Number(m[2]) + row).toString().padStart(m[2].length, "0")}`;
    const d = /^(\d{4})-(\d{2})-(\d{2})(.*)$/.exec(v);
    if (d) {
      const day = ((Number(d[3]) + row) % 28) + 1;
      return `${d[1]}-${d[2]}-${String(day).padStart(2, "0")}${d[4]}`;
    }
  }
  return toCell(v);
}

function expandTable(t: TableSpec, n: number): Cell[][] {
  const rows: Cell[][] = [];
  for (let i = 0; i < n; i += 1) {
    const base = t.sample_rows[i % t.sample_rows.length];
    const offset = Math.floor(i / t.sample_rows.length) * t.sample_rows.length;
    rows.push(t.columns.map((c, ci) => (i < t.sample_rows.length ? toCell(base[c.name] ?? null) : vary(base[c.name] ?? null, offset + (i % 5), ci))));
  }
  return rows;
}

function aggregateDso(): Pick<SqlResult, "columns" | "column_types" | "rows"> {
  return {
    columns: ["period", "revenue_usd", "open_ar_usd", "dso_days", "invoice_count"],
    column_types: ["VARCHAR", "DOUBLE", "DOUBLE", "DOUBLE", "BIGINT"],
    rows: PERIODS.map((p, i) => {
      const rev = 9_000_000 * Math.pow(1.058, i);
      return [p, Math.round(rev), Math.round((rev * DSO[i]) / 91), DSO[i], 1500 + i * 210];
    }),
  };
}

function aging(): Pick<SqlResult, "columns" | "column_types" | "rows"> {
  return {
    columns: ["bucket", "segment", "invoices", "open_ar_usd", "share"],
    column_types: ["VARCHAR", "VARCHAR", "BIGINT", "DOUBLE", "DOUBLE"],
    rows: [
      ["current", "enterprise", 212, 3_410_220, 0.28],
      ["1-30", "enterprise", 96, 2_011_870, 0.165],
      ["31-60", "enterprise", 61, 1_604_400, 0.131],
      ["61-90", "enterprise", 44, 1_391_005, 0.114],
      ["90+", "enterprise", 58, 3_120_550, 0.256],
      ["current", "mid_market", 402, 402_310, 0.033],
      ["90+", "mid_market", 37, 259_700, 0.021],
    ],
  };
}

export function runSql(req: SqlRequest): SqlResult {
  const sql = req.sql.trim();
  const started = 18 + (sql.length % 40);
  if (!/\b(select|with)\b/i.test(sql)) {
    const tok = sql.split(/\s+/)[0] ?? "";
    return { columns: [], rows: [], row_count: 0, elapsed_ms: 2, truncated: false, error: `Parser Error: syntax error at or near "${tok}"` };
  }
  if (/\b(insert|update|delete|drop|create|alter)\b/i.test(sql)) {
    return { columns: [], rows: [], row_count: 0, elapsed_ms: 1, truncated: false, error: "Invalid Input Error: Cannot execute statement in read-only mode" };
  }
  const refs = [...sql.matchAll(/\b(?:from|join)\s+([a-z_][a-z0-9_]*)/gi)].map((m) => m[1].toLowerCase());
  const ctes = new Set([...sql.matchAll(/\b([a-z_][a-z0-9_]*)\s+as\s*\(/gi)].map((m) => m[1].toLowerCase()));
  const unknown = refs.find((r) => !ctes.has(r) && !TABLES.some((t) => t.name === r));
  if (unknown) {
    return { columns: [], rows: [], row_count: 0, elapsed_ms: 3, truncated: false, error: `Catalog Error: Table with name ${unknown} does not exist!\nDid you mean "invoices"?` };
  }
  if (/\bcase\b/i.test(sql) && /due_date|aging|bucket/i.test(sql)) {
    const a = aging();
    return { ...a, row_count: a.rows.length, elapsed_ms: started + 11, truncated: false };
  }
  if (/\bgroup\s+by\b/i.test(sql)) {
    const a = aggregateDso();
    return { ...a, row_count: a.rows.length, elapsed_ms: started + 7, truncated: false };
  }
  const t = TABLES.find((x) => x.name === refs[0]) ?? TABLES[3];
  const limitMatch = /\blimit\s+(\d+)/i.exec(sql);
  const cap = req.limit ?? 500;
  const wanted = Math.min(limitMatch ? Number(limitMatch[1]) : t.row_count, t.row_count);
  const n = Math.min(wanted, cap);
  return {
    columns: t.columns.map((c) => c.name),
    column_types: t.columns.map((c) => c.type),
    rows: expandTable(t, n),
    row_count: n,
    elapsed_ms: started + Math.round(n / 25),
    truncated: wanted > cap,
  };
}
