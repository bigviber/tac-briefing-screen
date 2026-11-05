import { useEffect, useState } from "react";
import { FileCode2 } from "lucide-react";
import { useSettings } from "../../store/settingsStore";
import { Editor } from "../ui/LazyEditor";
import { EmptyState, SkeletonBlock } from "../ui/primitives";

/** Read-only view of the backend's named prompt templates. */
export function PromptInspector() {
  const prompts = useSettings((s) => s.prompts);
  const load = useSettings((s) => s.loadPrompts);
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, [load]);

  if (!prompts) return <SkeletonBlock lines={8} />;
  if (prompts.length === 0) return <EmptyState icon={FileCode2} title="No prompt templates exposed" />;
  const current = prompts.find((p) => p.name === active) ?? prompts[0];

  return (
    <div className="flex h-full min-h-0">
      <ul className="w-[180px] shrink-0 border-r border-line py-1">
        {prompts.map((p) => (
          <li key={p.name}>
            <button
              onClick={() => setActive(p.name)}
              className={`w-full truncate px-3 py-1.5 text-left font-mono text-xs ${p.name === current.name ? "bg-s3 text-fg" : "text-muted hover:text-fg"}`}
            >
              {p.name}
            </button>
          </li>
        ))}
      </ul>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-7 shrink-0 items-center border-b border-line px-3 text-2xs text-muted">
          <span className="font-mono text-fg">{current.name}</span>
          <span className="ml-auto">read-only · {current.template.length} chars</span>
        </div>
        <div className="min-h-0 flex-1">
          <Editor value={current.template} language="markdown" readOnly ariaLabel={`Prompt template ${current.name}`} />
        </div>
      </div>
    </div>
  );
}
