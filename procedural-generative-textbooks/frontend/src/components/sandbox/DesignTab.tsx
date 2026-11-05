import { useDeferredValue } from "react";
import { Paperclip } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { Editor } from "../ui/LazyEditor";
import { Mermaid } from "../ui/Mermaid";

export function DesignTab() {
  const code = useCase((s) => s.draft?.design ?? "");
  const attached = useCase((s) => s.draft?.designAttached ?? false);
  const patch = useCase((s) => s.patchDraft);
  const deferred = useDeferredValue(code);

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col border-r border-line">
        <div className="flex h-8 shrink-0 items-center border-b border-line bg-s1 px-3 text-2xs uppercase tracking-wider text-muted">
          mermaid source
        </div>
        <div className="min-h-0 flex-1">
          <Editor value={code} language="markdown" onChange={(v) => patch({ design: v })} ariaLabel="Design diagram editor" />
        </div>
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line bg-s1 px-3 text-2xs uppercase tracking-wider text-muted">
          live preview
          <button
            className={`btn btn-sm ml-auto normal-case tracking-normal ${attached ? "border-ok/50 text-ok" : ""}`}
            onClick={() => patch({ designAttached: !attached })}
            aria-pressed={attached}
          >
            <Paperclip size={12} aria-hidden /> {attached ? "Attached as design_mermaid" : "Attach to submission"}
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-3">
          <Mermaid code={deferred} natural />
        </div>
      </div>
    </div>
  );
}
