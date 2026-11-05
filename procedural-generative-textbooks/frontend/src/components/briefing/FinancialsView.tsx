import { useState } from "react";
import type { StatementLine } from "../../api/types";
import { useCase } from "../../store/caseStore";
import { fmtCents, fmtPct } from "../../lib/format";

type Stmt = "income" | "balance" | "cash_flow" | "ratios";
const LABEL: Record<Stmt, string> = { income: "Income", balance: "Balance", cash_flow: "Cash flow", ratios: "Ratios" };

/** Statements by period from the public CaseWorld (cents → compact dollars). */
export function FinancialsView() {
  const st = useCase((s) => s.caseData?.finance.statements);
  const checks = useCase((s) => s.caseData?.finance.invariants);
  const [stmt, setStmt] = useState<Stmt>("income");
  if (!st) return null;
  const periods = st.periods.slice(-6);

  let rows: { caption: string; level: number; values: string[] }[] = [];
  if (stmt === "ratios") {
    const keys = Object.keys(st.ratios[periods[0]] ?? {});
    rows = keys.map((k) => ({
      caption: k,
      level: 0,
      values: periods.map((p) => {
        const v = st.ratios[p]?.[k];
        if (v === undefined) return "—";
        return /margin|pct/.test(k) ? fmtPct(v) : v.toFixed(1);
      }),
    }));
  } else {
    const list = st[stmt] as { period: string; lines: StatementLine[] }[];
    const byP = new Map(list.map((x) => [x.period, x.lines]));
    const captions = list[0]?.lines ?? [];
    rows = captions.map((l, i) => ({
      caption: l.caption,
      level: l.level,
      values: periods.map((p) => {
        const line = byP.get(p)?.[i];
        return line ? fmtCents(line.cents, { compact: true }) : "—";
      }),
    }));
  }

  return (
    <div className="p-3">
      <div className="mb-2 flex items-center gap-1">
        {(Object.keys(LABEL) as Stmt[]).map((k) => (
          <button key={k} onClick={() => setStmt(k)} className={`chip ${k === stmt ? "border-accent text-fg" : "hover:text-fg"}`}>
            {LABEL[k]}
          </button>
        ))}
      </div>
      <div className="overflow-auto rounded border border-line">
        <table className="tbl whitespace-nowrap">
          <thead>
            <tr>
              <th>caption</th>
              {periods.map((p) => <th key={p} className="text-right font-mono">{p}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.caption} className={r.level > 0 ? "bg-s1 font-medium" : ""}>
                <td className={r.level > 0 ? "text-fg" : "text-[#c2cad8]"}>{r.caption}</td>
                {r.values.map((v, i) => <td key={i} className="num text-right">{v}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {checks && (
        <div className="mt-2 flex flex-wrap gap-1">
          {checks.map((c) => (
            <span key={c.name} className={`chip ${c.passed ? "text-ok" : "text-bad"}`} title={c.detail}>
              {c.passed ? "✓" : "✗"} {c.name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
