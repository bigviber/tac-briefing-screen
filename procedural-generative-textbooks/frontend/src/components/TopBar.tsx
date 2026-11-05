import { useEffect, useState } from "react";
import { ChevronRight, Command, LayoutDashboard, Settings } from "lucide-react";
import type { WorldStats } from "../api/types";
import type { Route } from "../lib/route";
import { navigate } from "../lib/route";
import { fmtNum, humanize } from "../lib/format";
import { api, isMockMode } from "../api/client";
import { useCase } from "../store/caseStore";
import { useCatalog } from "../store/catalogStore";
import { useUi } from "../store/uiStore";
import { Kbd, StatusPill } from "./ui/primitives";

/** `GET /cases/{id}/world-stats`, refetched when the case changes; null while loading or on error. */
function useWorldStats(caseId: string | null): WorldStats | null {
  const [stats, setStats] = useState<{ id: string; data: WorldStats } | null>(null);
  useEffect(() => {
    if (!caseId) return;
    let live = true;
    api.worldStats(caseId).then(
      (data) => { if (live) setStats({ id: caseId, data }); },
      () => { if (live) setStats(null); },
    );
    return () => { live = false; };
  }, [caseId]);
  return stats && stats.id === caseId ? stats.data : null;
}

/** Compact size/health strip for the generated world: tables · rows · invariants n/n · generation ms. */
function WorldStatsStrip({ s }: { s: WorldStats }) {
  const ok = s.invariants_passed === s.invariants_total;
  const ms = s.generation_ms;
  return (
    <span
      className="hidden items-center gap-1.5 whitespace-nowrap font-mono text-2xs text-muted lg:flex"
      title={`World: ${s.tables} tables, ${s.rows.toLocaleString("en-US")} rows, ${s.invariants_passed}/${s.invariants_total} ledger invariants hold${s.periods.length ? `, ${s.periods[0]}–${s.periods[s.periods.length - 1]}` : ""}${ms !== null ? `, generated in ${ms.toLocaleString("en-US")} ms` : ""}`}
      aria-label="World stats"
    >
      <span><span className="text-fg">{s.tables}</span> tables</span>
      <span aria-hidden>·</span>
      <span><span className="text-fg">{fmtNum(s.rows)}</span> rows</span>
      <span aria-hidden>·</span>
      <span className={ok ? "text-ok" : "text-bad"}>{ok ? "✓" : "✗"} {s.invariants_passed}/{s.invariants_total} inv</span>
      {ms !== null && (
        <>
          <span aria-hidden>·</span>
          <span><span className="text-fg">{ms >= 10_000 ? fmtNum(ms) : Math.round(ms)}</span> ms</span>
        </>
      )}
    </span>
  );
}

export function TopBar({ route }: { route: Route }) {
  const c = useCase((s) => s.caseData);
  const grade = useCase((s) => s.grade);
  const health = useCatalog((s) => s.health);
  const healthError = useCatalog((s) => s.healthError);
  const setPalette = useUi((s) => s.setPalette);
  const setSettings = useUi((s) => s.setSettings);
  const showCase = route.name === "case" && c && c.case_id === route.id;
  const status = grade ? "graded" : c?.status ?? "generated";
  const stats = useWorldStats(showCase && c ? c.case_id : null);

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
        {showCase && stats && <WorldStatsStrip s={stats} />}
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
