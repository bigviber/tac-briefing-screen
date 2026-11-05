import { useState } from "react";
import { Clipboard, Clock, Database, History, Paperclip } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { toast } from "../../store/uiStore";
import { toCsv } from "../../lib/csv";
import { relTime } from "../../lib/format";
import { Editor } from "../ui/LazyEditor";
import { EmptyState, ErrorLine } from "../ui/primitives";
import { ResultsGrid } from "./ResultsGrid";

function HistoryList({ onPick }: { onPick: (sql: string) => void }) {
  const history = useCase((s) => s.draft?.sqlHistory ?? []);
  if (history.length === 0) return <div className="p-3 text-xs text-muted">No queries run yet.</div>;
  return (
    <ul className="divide-y divide-[#1a2029]">
      {history.map((h) => (
        <li key={h.at}>
          <button onClick={() => onPick(h.sql)} className="w-full px-3 py-1.5 text-left hover:bg-s2">
            <div className="flex items-center gap-2 text-2xs text-muted">
              <span className={h.error ? "text-bad" : "text-ok"}>{h.error ? "error" : `${h.rowCount} rows`}</span>
              <span className="num">{h.ms}ms</span>
              <span className="ml-auto">{relTime(new Date(h.at).toISOString())}</span>
            </div>
            <div className="truncate font-mono text-xs text-fg">{h.sql.replace(/--[^\n]*\n/g, "").replace(/\s+/g, " ").trim()}</div>
          </button>
        </li>
      ))}
    </ul>
  );
}

export function SqlTab() {
  const sql = useCase((s) => s.draft?.sql ?? "");
  const patch = useCase((s) => s.patchDraft);
  const run = useCase((s) => s.runSql);
  const result = useCase((s) => s.sqlResult);
  const running = useCase((s) => s.sqlRunning);
  const attach = useCase((s) => s.attachSql);
  const [showHistory, setShowHistory] = useState(false);

  const copy = async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(toCsv(result.columns, result.rows));
      toast(`Copied ${result.rows.length} rows as CSV`, "success");
    } catch {
      toast("Clipboard unavailable", "error");
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="h-[42%] min-h-[120px] shrink-0 border-b border-line">
        <Editor value={sql} language="sql" onChange={(v) => patch({ sql: v })} onRun={() => void run()} ariaLabel="SQL editor" />
      </div>
      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line bg-s1 px-2 text-xs">
        {result && !result.error ? (
          <>
            <span className="num text-fg">{result.row_count.toLocaleString()} rows</span>
            <span className="num flex items-center gap-1 text-muted"><Clock size={11} aria-hidden />{result.elapsed_ms}ms</span>
            {result.truncated && <span className="chip border-warn/40 text-warn">truncated at 500</span>}
          </>
        ) : (
          <span className="text-muted">{running ? "Running…" : "Results"}</span>
        )}
        <div className="ml-auto flex items-center gap-1">
          <button className={`btn btn-ghost btn-sm ${showHistory ? "bg-s2 text-fg" : ""}`} onClick={() => setShowHistory((v) => !v)} aria-pressed={showHistory}>
            <History size={12} aria-hidden /> History
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => void copy()} disabled={!result || !!result.error || result.rows.length === 0}>
            <Clipboard size={12} aria-hidden /> Copy CSV
          </button>
          <button className="btn btn-sm" onClick={attach} disabled={!result || !!result.error}>
            <Paperclip size={12} aria-hidden /> Attach as evidence
          </button>
        </div>
      </div>
      <div className="relative flex min-h-0 flex-1">
        <div className="min-w-0 flex-1">
          {result?.error && <ErrorLine message={result.error} />}
          {result && !result.error && <ResultsGrid result={result} />}
          {!result && (
            <EmptyState icon={Database} title="Run a query">
              The case database is DuckDB, opened read-only. Press ⌘↵ in the editor to run.
            </EmptyState>
          )}
        </div>
        {showHistory && (
          <aside className="w-[280px] shrink-0 overflow-auto border-l border-line bg-s1" aria-label="Query history">
            <HistoryList onPick={(q) => { patch({ sql: q }); setShowHistory(false); }} />
          </aside>
        )}
      </div>
    </div>
  );
}
