import { ChevronRight, Command, LayoutDashboard, Settings } from "lucide-react";
import type { Route } from "../lib/route";
import { navigate } from "../lib/route";
import { humanize } from "../lib/format";
import { isMockMode } from "../api/client";
import { useCase } from "../store/caseStore";
import { useCatalog } from "../store/catalogStore";
import { useUi } from "../store/uiStore";
import { Kbd, StatusPill } from "./ui/primitives";

export function TopBar({ route }: { route: Route }) {
  const c = useCase((s) => s.caseData);
  const grade = useCase((s) => s.grade);
  const health = useCatalog((s) => s.health);
  const healthError = useCatalog((s) => s.healthError);
  const setPalette = useUi((s) => s.setPalette);
  const setSettings = useUi((s) => s.setSettings);
  const showCase = route.name === "case" && c && c.case_id === route.id;
  const status = grade ? "graded" : c?.status ?? "generated";

  return (
    <header className="flex h-10 shrink-0 items-center gap-3 border-b border-line bg-s1 px-3">
      <button onClick={() => navigate({ name: "dashboard" })} className="flex items-center gap-2 rounded px-1 font-semibold tracking-wide text-fg" aria-label="Axiom home">
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
          <path d="M3 13 8 3l5 10" stroke="#5b8def" strokeWidth="2" fill="none" />
          <path d="M5.5 9h5" stroke="#d7dde8" strokeWidth="1.5" />
        </svg>
        AXIOM
      </button>
      <nav className="flex min-w-0 items-center gap-1.5 text-xs text-muted" aria-label="Breadcrumb">
        <ChevronRight size={12} aria-hidden />
        <span className={showCase ? "" : "text-fg"}>{route.name === "dashboard" ? "Dashboard" : "Case"}</span>
        {showCase && c && (
          <>
            <ChevronRight size={12} aria-hidden />
            <span className="truncate font-medium text-fg">{c.company.name}</span>
            <span aria-hidden>·</span>
            <span className="truncate">{humanize(c.archetype)}</span>
            <span aria-hidden>·</span>
            <span className="capitalize">{c.difficulty}</span>
            <span aria-hidden>·</span>
            <span className="font-mono">seed {c.seed}</span>
            <span className="ml-1.5">
              <StatusPill status={status} />
            </span>
          </>
        )}
      </nav>
      <div className="ml-auto flex items-center gap-1.5">
        {isMockMode() && <span className="chip border-warn/40 text-warn">MOCK</span>}
        <span
          className="flex items-center gap-1.5 px-1 text-2xs text-muted"
          title={healthError ?? (health ? `API ${health.version}` : "checking backend")}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${health ? (health.llm_configured ? "bg-ok" : "bg-warn") : healthError ? "bg-bad" : "bg-muted"}`} />
          {health ? (health.llm_configured ? "API · LLM ready" : "API · no LLM key") : healthError ? "API offline" : "API…"}
        </span>
        <button className="btn btn-ghost btn-sm" onClick={() => setPalette(true)} aria-label="Open command palette">
          <Command size={12} aria-hidden /> <Kbd>⌘K</Kbd>
        </button>
        <button className="btn btn-ghost btn-sm" onClick={() => navigate({ name: "dashboard" })}>
          <LayoutDashboard size={12} aria-hidden /> Dashboard
        </button>
        <button className="btn btn-ghost btn-sm" onClick={() => setSettings(true)}>
          <Settings size={12} aria-hidden /> Settings
        </button>
      </div>
    </header>
  );
}
