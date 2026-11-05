import { lazy, Suspense, type ComponentProps } from "react";

const CodeEditor = lazy(() => import("./CodeEditor").then((m) => ({ default: m.CodeEditor })));

/** Monaco is ~3MB; load it only when an editor is first shown. */
export function Editor(props: ComponentProps<typeof CodeEditor>) {
  return (
    <Suspense fallback={<div className="p-3 font-mono text-xs text-muted">Loading editor…</div>}>
      <CodeEditor {...props} />
    </Suspense>
  );
}
