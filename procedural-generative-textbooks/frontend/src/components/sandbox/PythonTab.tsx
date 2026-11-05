import type { ReactNode } from "react";
import { Clock, Paperclip, Terminal } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { Editor } from "../ui/LazyEditor";
import { EmptyState, ErrorLine } from "../ui/primitives";

function Block({ title, tone = "text-muted", children }: { title: string; tone?: string; children: ReactNode }) {
  return (
    <div className="border-b border-line">
      <div className={`px-3 pt-2 text-2xs font-medium uppercase tracking-wider ${tone}`}>{title}</div>
      <div className="px-3 pb-2 pt-1">{children}</div>
    </div>
  );
}

export function PythonTab() {
  const code = useCase((s) => s.draft?.python ?? "");
  const patch = useCase((s) => s.patchDraft);
  const run = useCase((s) => s.runPython);
  const result = useCase((s) => s.pyResult);
  const running = useCase((s) => s.pyRunning);
  const attach = useCase((s) => s.attachPython);

  return (
    <div className="flex h-full min-h-0">
      <div className="min-w-0 flex-[1.15] border-r border-line">
        <Editor value={code} language="python" onChange={(v) => patch({ python: v })} onRun={() => void run()} ariaLabel="Python editor" />
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line bg-s1 px-2 text-xs">
          {result ? (
            <span className="num flex items-center gap-1 text-muted"><Clock size={11} aria-hidden />{result.elapsed_ms}ms</span>
          ) : (
            <span className="text-muted">{running ? "Running in sandbox…" : "Output"}</span>
          )}
          <button className="btn btn-sm ml-auto" onClick={attach} disabled={!result || !!result.error}>
            <Paperclip size={12} aria-hidden /> Attach as evidence
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {!result && (
            <EmptyState icon={Terminal} title="Run a script">
              Tables are preloaded as pandas DataFrames in <code className="font-mono">tables</code>; <code className="font-mono">con</code> is a read-only DuckDB connection. Timeout 20s.
            </EmptyState>
          )}
          {result?.error && <ErrorLine message={result.error} />}
          {result && (
            <>
              {result.stdout && (
                <Block title="stdout">
                  <pre className="whitespace-pre font-mono text-xs leading-[18px] text-fg">{result.stdout}</pre>
                </Block>
              )}
              {result.stderr && (
                <Block title="stderr" tone="text-warn">
                  <pre className="whitespace-pre-wrap font-mono text-xs leading-[18px] text-[#e8c48a]">{result.stderr}</pre>
                </Block>
              )}
              {result.result_json !== undefined && result.result_json !== null && (
                <Block title="result_json" tone="text-accent">
                  <pre className="whitespace-pre font-mono text-xs leading-[18px] text-fg">{JSON.stringify(result.result_json, null, 2)}</pre>
                </Block>
              )}
              {result.figures.length > 0 && (
                <Block title={`figures · ${result.figures.length}`}>
                  <div className="space-y-2">
                    {result.figures.map((b64, i) => (
                      <img key={i} src={`data:image/png;base64,${b64}`} alt={`Figure ${i + 1}`} className="max-w-full rounded border border-line" />
                    ))}
                  </div>
                </Block>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
