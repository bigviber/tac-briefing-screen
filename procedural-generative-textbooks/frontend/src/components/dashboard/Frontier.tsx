import { Target } from "lucide-react";
import type { SkillGraph } from "../../api/types";
import { PILLAR_COLOR, PILLAR_SHORT } from "../../lib/pillars";
import { Meter } from "../ui/primitives";

const READY = 0.6;

/** Nodes whose prerequisites are mastered (p_known ≥ 0.6) but which are not yet known: the §10 candidate set. */
export function Frontier({ graph, selected, onToggle }: { graph: SkillGraph; selected: string[]; onToggle: (id: string) => void }) {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const frontier = graph.nodes
    .filter((n) => n.mastery < READY && n.prereqs.every((p) => (byId.get(p)?.mastery ?? 0) >= READY))
    .sort((a, b) => a.mastery - b.mastery)
    .slice(0, 12);
  const blocked = graph.nodes.filter((n) => n.prereqs.some((p) => (byId.get(p)?.mastery ?? 0) < READY)).length;

  return (
    <div className="mt-3 border-t border-line pt-3">
      <div className="mb-2 flex items-center gap-2 px-1 text-xs">
        <Target size={13} className="text-accent" aria-hidden />
        <span className="font-medium uppercase tracking-wider text-muted">Learning frontier</span>
        <span className="text-muted">prereqs ≥ {READY} · lowest p_known first · {blocked} nodes still blocked by prerequisites</span>
      </div>
      {frontier.length === 0 ? (
        <p className="px-1 text-xs text-muted">Nothing on the frontier: every unlocked node is above {READY}.</p>
      ) : (
        <div className="grid grid-cols-3 gap-x-4 gap-y-0.5">
          {frontier.map((n) => {
            const sel = selected.includes(n.id);
            return (
              <button
                key={n.id}
                onClick={() => onToggle(n.id)}
                aria-pressed={sel}
                className={`flex h-7 items-center gap-2 rounded px-1.5 text-left text-xs hover:bg-s2 ${sel ? "bg-s2 ring-1 ring-accent" : ""}`}
              >
                <span className="w-7 shrink-0 font-mono text-2xs" style={{ color: PILLAR_COLOR[n.pillar] }}>{PILLAR_SHORT[n.pillar]}</span>
                <span className="min-w-0 flex-1 truncate text-fg">{n.title}</span>
                <Meter value={n.mastery} color={PILLAR_COLOR[n.pillar]} className="w-12 shrink-0" />
                <span className="num w-7 shrink-0 text-right text-muted">{Math.round(n.mastery * 100)}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
