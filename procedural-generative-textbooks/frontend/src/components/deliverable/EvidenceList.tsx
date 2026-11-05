import { Database, Paperclip, Table2, Terminal, Workflow, X } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { useUi } from "../../store/uiStore";

export function EvidenceList() {
  const draft = useCase((s) => s.draft);
  const remove = useCase((s) => s.removeEvidence);
  const patch = useCase((s) => s.patchDraft);
  const setTab = useUi((s) => s.setSandboxTab);
  if (!draft) return null;

  const items = [
    ...draft.sqlEvidence.map((e, i) => ({ key: `sql-${i}`, icon: Database, name: e.name, detail: e.sql.replace(/--[^\n]*\n/g, "").replace(/\s+/g, " ").trim(), title: e.result_preview, onRemove: () => remove("sql", i) })),
    ...draft.pythonEvidence.map((e, i) => ({ key: `py-${i}`, icon: Terminal, name: e.name, detail: e.stdout_tail.split("\n")[0] ?? "", title: e.stdout_tail, onRemove: () => remove("python", i) })),
    ...(draft.designAttached ? [{ key: "design", icon: Workflow, name: "Design diagram", detail: "design_mermaid", title: draft.design, onRemove: () => patch({ designAttached: false }) }] : []),
    ...(draft.modelAttached ? [{ key: "model", icon: Table2, name: "Financial model", detail: `${draft.model?.rows.length ?? 0} rows × ${draft.model?.periods.length ?? 0} periods`, title: "", onRemove: () => patch({ modelAttached: false }) }] : []),
  ];

  return (
    <div className="p-3">
      <div className="field-label">Attached evidence · {items.length}</div>
      {items.length === 0 ? (
        <div className="rounded border border-dashed border-line p-2.5 text-2xs leading-relaxed text-muted">
          <Paperclip size={11} className="mr-1 inline" aria-hidden />
          Use “Attach as evidence” on a <button className="text-accent hover:underline" onClick={() => setTab("sql")}>SQL</button> or{" "}
          <button className="text-accent hover:underline" onClick={() => setTab("python")}>Python</button> result. Graders check reference-query signatures against attached SQL.
        </div>
      ) : (
        <ul className="space-y-1">
          {items.map((it) => {
            const Icon = it.icon;
            return (
              <li key={it.key} className="group flex items-start gap-2 rounded border border-line bg-s1 px-2 py-1.5" title={it.title}>
                <Icon size={12} className="mt-0.5 shrink-0 text-muted" aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="text-xs text-fg">{it.name}</div>
                  <div className="truncate font-mono text-2xs text-muted">{it.detail}</div>
                </div>
                <button className="text-muted opacity-60 hover:text-bad group-hover:opacity-100" onClick={it.onRemove} aria-label={`Remove ${it.name}`}>
                  <X size={12} />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
