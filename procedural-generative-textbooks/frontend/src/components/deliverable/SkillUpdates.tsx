import type { SkillUpdate } from "../../api/types";
import { useSkills } from "../../store/skillsStore";
import { PILLAR_COLOR } from "../../lib/pillars";

/** Before → after p_known bars for every node the grade touched. */
export function SkillUpdates({ updates, nextDrill }: { updates: SkillUpdate[]; nextDrill: string[] }) {
  const graph = useSkills((s) => s.graph);
  const nodeOf = (id: string) => graph?.nodes.find((n) => n.id === id);
  return (
    <div>
      <div className="field-label">Skill updates (BKT p_known)</div>
      <div className="space-y-1">
        {updates.map((u) => {
          const n = nodeOf(u.node);
          const color = n ? PILLAR_COLOR[n.pillar] : "#5b8def";
          const up = u.after >= u.before;
          return (
            <div key={u.node} className="flex items-center gap-2 text-xs">
              <span className="w-[150px] shrink-0 truncate text-fg" title={u.node}>{n?.title ?? u.node}</span>
              <div className="relative h-2 flex-1 rounded-sm bg-s3">
                <div className="absolute inset-y-0 left-0 rounded-sm opacity-35" style={{ width: `${u.before * 100}%`, background: color }} />
                <div className="absolute inset-y-0 rounded-sm" style={{ left: `${Math.min(u.before, u.after) * 100}%`, width: `${Math.abs(u.after - u.before) * 100}%`, background: color }} />
              </div>
              {u.outcome && (
                <span
                  className={`w-[62px] shrink-0 text-right font-mono text-2xs ${u.outcome === "correct" ? "text-ok" : u.outcome === "borderline" ? "text-warn" : "text-bad"}`}
                  title={u.score !== null ? `observation score ${u.score.toFixed(2)}` : undefined}
                >
                  {u.outcome}
                </span>
              )}
              <span className="num w-[86px] shrink-0 text-right text-muted">
                {u.before.toFixed(2)} → <span className={up ? "text-ok" : "text-bad"}>{u.after.toFixed(2)}</span>
              </span>
            </div>
          );
        })}
      </div>
      {nextDrill.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1 text-2xs text-muted">
          Next drill:
          {nextDrill.map((id) => (
            <span key={id} className="chip">{nodeOf(id)?.title ?? id}</span>
          ))}
        </div>
      )}
    </div>
  );
}
