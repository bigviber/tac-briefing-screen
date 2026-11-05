import { useEffect } from "react";
import { BarChart3, FileCode2, SlidersHorizontal, X } from "lucide-react";
import { useUi } from "../store/uiStore";
import { Tabs, type TabDef } from "./ui/primitives";
import { LlmForm } from "./settings/LlmForm";
import { UsageTable } from "./settings/UsageTable";
import { PromptInspector } from "./settings/PromptInspector";

type STab = "llm" | "usage" | "prompts";
const TABS: TabDef<STab>[] = [
  { id: "llm", label: "LLM provider", icon: SlidersHorizontal },
  { id: "usage", label: "Usage & cost", icon: BarChart3 },
  { id: "prompts", label: "Prompt inspector", icon: FileCode2 },
];

export function SettingsDrawer() {
  const tab = useUi((s) => s.settingsTab);
  const set = useUi((s) => s.setSettings);

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    return () => prev?.focus();
  }, []);

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/40" onMouseDown={() => set(false)}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        className="flex h-full w-[640px] max-w-full flex-col border-l border-line bg-s1 shadow-2xl shadow-black/60"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex h-10 shrink-0 items-center border-b border-line px-3">
          <span className="text-sm font-semibold text-fg">Settings</span>
          <span className="ml-2 text-xs text-muted">stored in the local app DB · API key never leaves this machine except to your provider</span>
          <button className="btn btn-ghost btn-sm ml-auto" onClick={() => set(false)} aria-label="Close settings" autoFocus>
            <X size={14} />
          </button>
        </div>
        <Tabs tabs={TABS} value={tab} onChange={(t) => set(true, t)} />
        <div className="min-h-0 flex-1 overflow-auto">
          {tab === "llm" && <LlmForm />}
          {tab === "usage" && <UsageTable />}
          {tab === "prompts" && <PromptInspector />}
        </div>
      </aside>
    </div>
  );
}
