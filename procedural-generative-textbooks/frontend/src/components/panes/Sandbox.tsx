import { Database, Play, Table2, Terminal, Workflow } from "lucide-react";
import { useUi, type SandboxTab } from "../../store/uiStore";
import { useCase } from "../../store/caseStore";
import { Kbd, Tabs, type TabDef } from "../ui/primitives";
import { SqlTab } from "../sandbox/SqlTab";
import { PythonTab } from "../sandbox/PythonTab";
import { DesignTab } from "../sandbox/DesignTab";
import { ModelTab } from "../sandbox/ModelTab";

const TABS: TabDef<SandboxTab>[] = [
  { id: "sql", label: "SQL", icon: Database },
  { id: "python", label: "Python", icon: Terminal },
  { id: "design", label: "Design", icon: Workflow },
  { id: "model", label: "Model", icon: Table2 },
];

export function Sandbox() {
  const tab = useUi((s) => s.sandboxTab);
  const setTab = useUi((s) => s.setSandboxTab);
  const sqlRunning = useCase((s) => s.sqlRunning);
  const pyRunning = useCase((s) => s.pyRunning);
  const runSql = useCase((s) => s.runSql);
  const runPython = useCase((s) => s.runPython);
  const runnable = tab === "sql" || tab === "python";
  const running = tab === "sql" ? sqlRunning : pyRunning;

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <Tabs
        tabs={TABS}
        value={tab}
        onChange={setTab}
        right={
          runnable && (
            <button
              className="btn btn-primary btn-sm"
              disabled={running}
              onClick={() => void (tab === "sql" ? runSql() : runPython())}
              aria-label={`Run ${tab}`}
            >
              <Play size={11} aria-hidden /> {running ? "Running…" : "Run"}
              <span className="opacity-80"><Kbd>⌘↵</Kbd></span>
            </button>
          )
        }
      />
      <div className="min-h-0 flex-1" role="tabpanel">
        {tab === "sql" && <SqlTab />}
        {tab === "python" && <PythonTab />}
        {tab === "design" && <DesignTab />}
        {tab === "model" && <ModelTab />}
      </div>
    </div>
  );
}
