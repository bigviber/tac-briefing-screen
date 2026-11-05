import { useState } from "react";
import { Eye, PenLine } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { Markdown } from "../ui/Markdown";

const TEMPLATE = `**To:** CFO · **Re:** Why cash is falling while revenue grows

**Bottom line:**

**Root cause:**

**Evidence:**
-

**Recommendation (Q1):**
1.

**Risks:**
`;

export function MemoEditor() {
  const memo = useCase((s) => s.draft?.memo ?? "");
  const patch = useCase((s) => s.patchDraft);
  const [preview, setPreview] = useState(false);
  const words = memo.trim() ? memo.trim().split(/\s+/).length : 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-7 shrink-0 items-center gap-2 border-b border-line px-3">
        <span className="text-2xs font-medium uppercase tracking-wider text-muted">Executive memo</span>
        <span className="num text-2xs text-muted">{words} words</span>
        {!memo && (
          <button className="text-2xs text-accent hover:underline" onClick={() => patch({ memo: TEMPLATE })}>
            insert template
          </button>
        )}
        <div className="ml-auto flex rounded border border-line" role="radiogroup" aria-label="Memo mode">
          <button role="radio" aria-checked={!preview} onClick={() => setPreview(false)} className={`flex h-5 items-center gap-1 px-1.5 text-2xs ${!preview ? "bg-s3 text-fg" : "text-muted"}`}>
            <PenLine size={10} aria-hidden /> Write
          </button>
          <button role="radio" aria-checked={preview} onClick={() => setPreview(true)} className={`flex h-5 items-center gap-1 px-1.5 text-2xs ${preview ? "bg-s3 text-fg" : "text-muted"}`}>
            <Eye size={10} aria-hidden /> Preview
          </button>
        </div>
      </div>
      {preview ? (
        <div className="min-h-0 flex-1 overflow-auto px-4 py-2">
          {memo ? <Markdown>{memo}</Markdown> : <p className="text-xs text-muted">Nothing to preview.</p>}
        </div>
      ) : (
        <textarea
          value={memo}
          onChange={(e) => patch({ memo: e.target.value })}
          spellCheck
          placeholder="Lead with the answer. One page. Markdown supported."
          aria-label="Executive memo (markdown)"
          className="min-h-0 flex-1 resize-none bg-bg px-4 py-2 font-mono text-xs leading-[19px] text-fg placeholder:text-[#4a5466] focus:outline-none"
        />
      )}
    </div>
  );
}
