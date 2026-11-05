import type { Cell } from "../api/types";

function esc(v: Cell): string {
  if (v === null) return "";
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(columns: string[], rows: Cell[][]): string {
  return [columns.map(esc).join(","), ...rows.map((r) => r.map(esc).join(","))].join("\n");
}

/** A compact plain-text preview used for `sql_evidence.result_preview`. */
export function previewTable(columns: string[], rows: Cell[][], max = 8): string {
  const head = rows.slice(0, max);
  const widths = columns.map((c, i) =>
    Math.min(24, Math.max(c.length, ...head.map((r) => String(r[i] ?? "NULL").length))),
  );
  const line = (vals: string[]) => vals.map((v, i) => v.slice(0, 24).padEnd(widths[i])).join(" | ");
  const out = [
    line(columns),
    widths.map((w) => "-".repeat(w)).join("-+-"),
    ...head.map((r) => line(r.map((v) => String(v ?? "NULL")))),
  ];
  if (rows.length > max) out.push(`… ${rows.length - max} more rows`);
  return out.join("\n");
}
