import { useEffect } from "react";
import { FolderOpen, History } from "lucide-react";
import { useCatalog } from "../../store/catalogStore";
import { navigate } from "../../lib/route";
import { PILLAR_COLOR } from "../../lib/pillars";
import { humanize, relTime } from "../../lib/format";
import { EmptyState, SkeletonBlock, StatusPill } from "../ui/primitives";

export function RecentCases() {
  const cases = useCatalog((s) => s.cases);
  const load = useCatalog((s) => s.loadCases);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="panel flex min-h-0 flex-1 flex-col" aria-labelledby="recent-title">
      <div className="panel-head">
        <History size={13} aria-hidden />
        <span id="recent-title">Recent cases</span>
        <span className="ml-auto num normal-case tracking-normal">{cases?.length ?? ""}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {!cases && <SkeletonBlock lines={5} />}
        {cases && cases.length === 0 && (
          <EmptyState icon={FolderOpen} title="No cases yet">Generate your first case to start a session.</EmptyState>
        )}
        {cases?.map((c) => (
          <button
            key={c.case_id}
            onClick={() => navigate({ name: "case", id: c.case_id })}
            className="flex w-full items-center gap-2 border-b border-[#1a2029] px-3 py-2 text-left hover:bg-s2 focus-visible:bg-s2"
          >
            <div className="flex shrink-0 gap-0.5">
              {c.pillars.map((p) => (
                <span key={p} className="h-5 w-1 rounded-sm" style={{ background: PILLAR_COLOR[p] }} />
              ))}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm text-fg">{c.company_name}</div>
              <div className="truncate text-xs text-muted">
                {humanize(c.archetype)} · {c.difficulty} · <span className="font-mono">{c.case_id}</span>
              </div>
            </div>
            <div className="flex flex-col items-end gap-1">
              <StatusPill status={c.status} />
              <span className="text-2xs text-muted">{relTime(c.created_at)}</span>
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}
