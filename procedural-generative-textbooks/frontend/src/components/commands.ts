import { useMemo } from "react";
import {
  BookOpen,
  Database,
  FileText,
  FolderOpen,
  LayoutDashboard,
  Network,
  Play,
  RotateCcw,
  Send,
  Settings,
  Sparkles,
  Table2,
  Terminal,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { useCatalog } from "../store/catalogStore";
import { useCase } from "../store/caseStore";
import { useUi, type BriefTab, type SandboxTab } from "../store/uiStore";
import { useSkills } from "../store/skillsStore";
import { navigate } from "../lib/route";
import { humanize } from "../lib/format";

export interface PaletteCommand {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: LucideIcon;
  run: () => void;
}

async function generateAndOpen(archetype?: string): Promise<void> {
  const targets = useSkills.getState().targets;
  const c = await useCase.getState().generate({ archetype, target_skills: targets.length ? targets : undefined });
  if (c) navigate({ name: "case", id: c.case_id });
}

const BRIEF: [BriefTab, string, LucideIcon][] = [
  ["dossier", "Dossier", FileText],
  ["textbook", "Textbook", BookOpen],
  ["data", "Data Room", Database],
  ["architecture", "Architecture", Network],
];
const SANDBOX: [SandboxTab, string, LucideIcon][] = [
  ["sql", "SQL", Database],
  ["python", "Python", Terminal],
  ["design", "Design", Workflow],
  ["model", "Model", Table2],
];

/** All palette commands, rebuilt when the catalog or open case changes. */
export function useCommands(): PaletteCommand[] {
  const archetypes = useCatalog((s) => s.archetypes);
  const cases = useCatalog((s) => s.cases);
  const caseId = useCase((s) => s.caseData?.case_id);

  return useMemo(() => {
    const ui = useUi.getState();
    const out: PaletteCommand[] = [
      { id: "gen-auto", group: "Generate", label: "Generate case — let Axiom pick", hint: "targets lowest mastery", icon: Sparkles, run: () => void generateAndOpen() },
      ...(archetypes ?? []).map((a) => ({ id: `gen-${a.key}`, group: "Generate", label: `Generate ${a.title}`, hint: a.key, icon: Sparkles, run: () => void generateAndOpen(a.key) })),
      { id: "nav-dash", group: "Navigate", label: "Go to dashboard", icon: LayoutDashboard, run: () => navigate({ name: "dashboard" }) },
      { id: "settings", group: "Settings", label: "Open LLM settings", icon: Settings, run: () => ui.setSettings(true, "llm") },
      { id: "usage", group: "Settings", label: "Show token usage & cost", icon: Settings, run: () => ui.setSettings(true, "usage") },
      { id: "prompts", group: "Settings", label: "Open prompt inspector", icon: Settings, run: () => ui.setSettings(true, "prompts") },
    ];
    if (caseId) {
      const go = (fn: () => void) => () => {
        if (window.location.hash !== `#/case/${caseId}`) navigate({ name: "case", id: caseId });
        fn();
      };
      for (const [id, label, icon] of BRIEF) out.push({ id: `brief-${id}`, group: "Briefing", label: `Show ${label}`, icon, run: go(() => ui.setBriefTab(id)) });
      for (const [id, label, icon] of SANDBOX) out.push({ id: `sb-${id}`, group: "Sandbox", label: `Switch to ${label}`, icon, run: go(() => ui.setSandboxTab(id)) });
      out.push(
        { id: "run-sql", group: "Sandbox", label: "Run SQL", hint: "⌘↵", icon: Play, run: go(() => { ui.setSandboxTab("sql"); void useCase.getState().runSql(); }) },
        { id: "run-py", group: "Sandbox", label: "Run Python", hint: "⌘↵", icon: Play, run: go(() => { ui.setSandboxTab("python"); void useCase.getState().runPython(); }) },
        { id: "submit", group: "Deliverable", label: "Submit deliverable for grading", icon: Send, run: go(() => void useCase.getState().submit()) },
        { id: "redossier", group: "Briefing", label: "Regenerate dossier", icon: FileText, run: go(() => void useCase.getState().loadDossier(true)) },
        { id: "layout", group: "Layout", label: "Reset pane layout", icon: RotateCcw, run: () => ui.resetPanes() },
      );
    }
    for (const c of cases ?? []) {
      out.push({ id: `open-${c.case_id}`, group: "Open", label: `${c.company_name} — ${humanize(c.archetype)}`, hint: c.case_id, icon: FolderOpen, run: () => navigate({ name: "case", id: c.case_id }) });
    }
    return out;
  }, [archetypes, cases, caseId]);
}
