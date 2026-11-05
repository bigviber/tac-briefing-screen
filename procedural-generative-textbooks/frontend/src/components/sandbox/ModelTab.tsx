import { Paperclip, Plus, Trash2 } from "lucide-react";
import type { FinancialModel } from "../../api/types";
import { useCase } from "../../store/caseStore";
import { fmtNum } from "../../lib/format";

const UNITS = ["usd", "usd_thousands", "days", "pct", "count"];

/** Formula-free financial table: captions × periods, editable cells, row totals. */
export function ModelTab() {
  const model = useCase((s) => s.draft?.model ?? null);
  const attached = useCase((s) => s.draft?.modelAttached ?? false);
  const patch = useCase((s) => s.patchDraft);
  if (!model) return null;

  const update = (m: FinancialModel) => patch({ model: m });
  const setCell = (r: number, c: number, raw: string) => {
    const v = raw.trim() === "" ? null : Number(raw.replace(/[$,\s]/g, ""));
    update({ ...model, rows: model.rows.map((row, i) => (i !== r ? row : { ...row, values: row.values.map((x, j) => (j === c ? (Number.isNaN(v) ? x : v) : x)) })) });
  };
  const setCaption = (r: number, caption: string) =>
    update({ ...model, rows: model.rows.map((row, i) => (i === r ? { ...row, caption } : row)) });
  const addRow = () => update({ ...model, rows: [...model.rows, { caption: "New line", values: model.periods.map(() => null) }] });
  const removeRow = (r: number) => update({ ...model, rows: model.rows.filter((_, i) => i !== r) });
  const totals = model.rows.map((r) => r.values.reduce<number>((s, v) => s + (v ?? 0), 0));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line bg-s1 px-3 text-xs">
        <span className="text-2xs uppercase tracking-wider text-muted">financial_model</span>
        <label className="ml-3 flex items-center gap-1.5 text-muted">
          unit
          <select className="input h-6 w-[130px] text-xs" value={model.unit} onChange={(e) => update({ ...model, unit: e.target.value })}>
            {UNITS.map((u) => <option key={u}>{u}</option>)}
          </select>
        </label>
        <span className="min-w-0 truncate text-2xs text-muted" title="No formulas: type the numbers you will defend.">Periods ending in E are forecasts · no formulas</span>
        <button className={`btn btn-sm ml-auto ${attached ? "border-ok/50 text-ok" : ""}`} onClick={() => patch({ modelAttached: !attached })} aria-pressed={attached}>
          <Paperclip size={12} aria-hidden /> {attached ? "Attached" : "Attach to submission"}
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3">
        <table className="tbl">
          <thead>
            <tr>
              <th className="w-[200px]">caption</th>
              {model.periods.map((p) => (
                <th key={p} className={`text-right font-mono ${p.endsWith("E") ? "text-accent" : ""}`}>{p}</th>
              ))}
              <th className="text-right">total</th>
              <th className="w-8" />
            </tr>
          </thead>
          <tbody>
            {model.rows.map((row, r) => (
              <tr key={r}>
                <td className="p-0">
                  <input className="h-7 w-full bg-transparent px-2 text-xs text-fg focus:bg-bg focus:outline-none focus-visible:outline-1 focus-visible:outline-accent" value={row.caption} onChange={(e) => setCaption(r, e.target.value)} aria-label={`Row ${r + 1} caption`} />
                </td>
                {row.values.map((v, c) => (
                  <td key={c} className="p-0">
                    <input
                      className="h-7 w-full min-w-[84px] bg-transparent px-2 text-right font-mono text-xs text-fg focus:bg-bg focus:outline-none focus-visible:outline-1 focus-visible:outline-accent"
                      inputMode="decimal"
                      defaultValue={v === null ? "" : String(v)}
                      key={`${r}-${c}-${v}`}
                      onBlur={(e) => setCell(r, c, e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                      aria-label={`${row.caption} ${model.periods[c]}`}
                      placeholder="·"
                    />
                  </td>
                ))}
                <td className="num text-right text-muted">{fmtNum(totals[r])}</td>
                <td className="p-0 text-center">
                  <button className="p-1 text-muted hover:text-bad" onClick={() => removeRow(r)} aria-label={`Delete ${row.caption}`}>
                    <Trash2 size={12} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="btn btn-ghost btn-sm mt-2" onClick={addRow}>
          <Plus size={12} aria-hidden /> Add row
        </button>
      </div>
    </div>
  );
}
