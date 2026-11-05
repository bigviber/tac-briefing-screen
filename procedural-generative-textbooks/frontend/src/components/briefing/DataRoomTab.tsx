import { useState } from "react";
import { Key, Link2, Sparkles } from "lucide-react";
import type { TableSpec } from "../../api/types";
import { useCase } from "../../store/caseStore";
import { useUi } from "../../store/uiStore";
import { fmtNum } from "../../lib/format";
import { Mermaid } from "../ui/Mermaid";
import { FinancialsView } from "./FinancialsView";
import { OperationsView } from "./OperationsView";

type View = "tables" | "erd" | "financials" | "operations";

function DirtBadge({ rule }: { rule: string }) {
  const [kind, ...rest] = rule.split(":");
  return (
    <span className="chip border-warn/30 text-warn" title={`Dirt rule: ${rule}`}>
      {kind}
      {rest.length > 0 && <span className="text-muted">:{rest.join(":")}</span>}
    </span>
  );
}

function TableDetail({ t }: { t: TableSpec }) {
  const fks = new Map(t.foreign_keys.map(([col, rt, rc]) => [col, `${rt}.${rc}`]));
  const draft = useCase((s) => s.draft);
  const patch = useCase((s) => s.patchDraft);
  const setTab = useUi((s) => s.setSandboxTab);
  const peek = () => {
    if (!draft) return;
    patch({ sql: `SELECT *\nFROM ${t.name}\nLIMIT 200;\n` });
    setTab("sql");
    void useCase.getState().runSql(`SELECT *\nFROM ${t.name}\nLIMIT 200;\n`);
  };
  return (
    <div className="space-y-2.5 border-t border-line bg-s1/40 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-1">
        {t.dirt.length === 0 ? <span className="chip border-ok/30 text-ok">clean</span> : t.dirt.map((d) => <DirtBadge key={d} rule={d} />)}
        <button className="btn btn-sm ml-auto" onClick={peek}>
          <Sparkles size={11} aria-hidden /> Query in sandbox
        </button>
      </div>
      <table className="tbl">
        <thead>
          <tr><th>column</th><th>type</th><th>notes</th></tr>
        </thead>
        <tbody>
          {t.columns.map((c) => (
            <tr key={c.name}>
              <td className="font-mono text-fg">
                <span className="inline-flex items-center gap-1">
                  {t.primary_key.includes(c.name) && <Key size={10} className="text-warn" aria-label="primary key" />}
                  {fks.has(c.name) && <Link2 size={10} className="text-accent" aria-label="foreign key" />}
                  {c.name}
                </span>
              </td>
              <td className="font-mono text-muted">{c.type}{c.nullable ? "?" : ""}</td>
              <td className="text-muted">
                {c.description}
                {fks.has(c.name) && <span className="ml-1 font-mono text-accent">→ {fks.get(c.name)}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div>
        <div className="field-label">Sample rows</div>
        <div className="max-h-[180px] overflow-auto rounded border border-line">
          <table className="tbl whitespace-nowrap font-mono">
            <thead>
              <tr>{t.columns.map((c) => <th key={c.name}>{c.name}</th>)}</tr>
            </thead>
            <tbody>
              {t.sample_rows.map((r, i) => (
                <tr key={i}>
                  {t.columns.map((c) => {
                    const v = r[c.name];
                    return <td key={c.name} className={v === null ? "italic text-[#5b6577]" : "text-fg"}>{v === null || v === undefined ? "NULL" : String(v)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export function DataRoomTab() {
  const db = useCase((s) => s.caseData?.database);
  const [view, setView] = useState<View>("tables");
  const [open, setOpen] = useState<string | null>(db?.tables[0]?.name ?? null);
  if (!db) return null;

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
        <div className="flex rounded border border-line" role="radiogroup" aria-label="Data room view">
          {(["tables", "erd", "financials", "operations"] as View[]).map((v) => (
            <button key={v} role="radio" aria-checked={view === v} onClick={() => setView(v)} className={`h-6 px-2.5 text-xs capitalize ${view === v ? "bg-s3 text-fg" : "text-muted hover:text-fg"}`}>
              {v === "erd" ? "ERD" : v}
            </button>
          ))}
        </div>
        <span className="ml-auto min-w-0 truncate whitespace-nowrap font-mono text-2xs text-muted">{db.engine} · {db.tables.length} tables · {fmtNum(db.total_rows)} rows</span>
      </div>
      {view === "erd" && (
        // Large schemas (the generator emits ~14 tables) are unreadable when fit to the pane: scroll instead.
        <div className="overflow-auto p-3">
          <Mermaid code={db.erd_mermaid} natural={db.tables.length > 6} />
        </div>
      )}
      {view === "financials" && <FinancialsView />}
      {view === "operations" && <OperationsView />}
      {view === "tables" && (
        <ul>
          {db.tables.map((t) => (
            <li key={t.name} className="border-b border-line">
              <button
                onClick={() => setOpen(open === t.name ? null : t.name)}
                aria-expanded={open === t.name}
                className={`flex h-8 w-full items-center gap-2 px-3 text-left text-sm hover:bg-s2 ${open === t.name ? "bg-s2" : ""}`}
              >
                <span className="font-mono text-fg">{t.name}</span>
                <span className="text-2xs text-muted">{t.columns.length} cols</span>
                {t.dirt.length > 0 && <span className="chip h-4 border-warn/30 text-warn">{t.dirt.length} dirt</span>}
                <span className="num ml-auto text-xs text-muted">{t.row_count.toLocaleString()}</span>
              </button>
              {open === t.name && <TableDetail t={t} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
