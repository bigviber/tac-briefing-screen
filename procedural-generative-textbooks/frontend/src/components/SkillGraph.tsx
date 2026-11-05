import { memo, useMemo, useRef, useState, type MouseEvent } from "react";
import type { SkillGraph as Graph } from "../api/types";
import { edgePath, layoutDag, LAYOUT, type PlacedNode } from "../lib/dagLayout";
import { PILLAR_COLOR, PILLAR_LABEL } from "../lib/pillars";
import { fmtPct } from "../lib/format";

interface Props {
  graph: Graph;
  selected: string[];
  onToggle: (id: string) => void;
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

/** SVG DAG of the skill taxonomy: pillar columns, layered by prerequisites, filled by mastery. */
export const SkillGraph = memo(function SkillGraph({ graph, selected, onToggle }: Props) {
  const layout = useMemo(() => layoutDag(graph), [graph]);
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  const related = useMemo(() => {
    if (!hover) return null;
    const s = new Set([hover.id]);
    graph.edges.forEach((e) => {
      if (e.source === hover.id) s.add(e.target);
      if (e.target === hover.id) s.add(e.source);
    });
    return s;
  }, [hover, graph.edges]);

  const onEnter = (p: PlacedNode, e: MouseEvent) => {
    const r = wrap.current?.getBoundingClientRect();
    if (!r) return;
    setHover({ id: p.node.id, x: e.clientX - r.left, y: e.clientY - r.top });
  };

  const hovered = hover ? layout.nodes.get(hover.id) : undefined;
  const sel = new Set(selected);

  return (
    <div ref={wrap} className="relative w-full">
      <svg viewBox={`0 0 ${layout.width} ${layout.height}`} className="block w-full" role="img" aria-label="Skill graph">
        {layout.columns.map((c) => (
          <g key={c.pillar}>
            <rect x={c.x} y={4} width={c.width} height={layout.height - 8} rx={4} fill="#0f131a" stroke="#1b212c" />
            <rect x={c.x} y={4} width={c.width} height={2} fill={PILLAR_COLOR[c.pillar]} opacity={0.8} />
            <text x={c.x + 10} y={24} fontSize={10.5} fontWeight={600} fill="#8b95a7" letterSpacing="0.06em">
              {PILLAR_LABEL[c.pillar].toUpperCase()}
            </text>
          </g>
        ))}
        <g fill="none">
          {graph.edges.map((e) => {
            const a = layout.nodes.get(e.source);
            const b = layout.nodes.get(e.target);
            if (!a || !b) return null;
            const cross = a.node.pillar !== b.node.pillar;
            const lit = related ? related.has(e.source) && related.has(e.target) && (e.source === hover?.id || e.target === hover?.id) : false;
            return (
              <path
                key={`${e.source}-${e.target}`}
                d={edgePath(a, b)}
                stroke={lit ? "#d7dde8" : cross ? "#3a4456" : "#2a3242"}
                strokeWidth={lit ? 1.2 : 1}
                strokeDasharray={cross ? "3 3" : undefined}
                opacity={related && !lit ? 0.25 : cross ? 0.55 : 1}
              />
            );
          })}
        </g>
        {[...layout.nodes.values()].map((p) => {
          const color = PILLAR_COLOR[p.node.pillar];
          const isSel = sel.has(p.node.id);
          const dim = related ? !related.has(p.node.id) : false;
          const right = p.x - 10 - p.layer * LAYOUT.indent + LAYOUT.colWidth - 10;
          return (
            <g
              key={p.node.id}
              opacity={dim ? 0.45 : 1}
              className="cursor-pointer"
              onMouseEnter={(e) => onEnter(p, e)}
              onMouseMove={(e) => onEnter(p, e)}
              onMouseLeave={() => setHover(null)}
              onClick={() => onToggle(p.node.id)}
              role="button"
              tabIndex={0}
              aria-pressed={isSel}
              aria-label={`${p.node.title}, mastery ${fmtPct(p.node.mastery, 0)}`}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onToggle(p.node.id);
                }
              }}
            >
              <rect x={p.x - 9} y={p.y - 10} width={right - p.x + 18} height={20} fill={isSel ? "#1a2233" : "transparent"} rx={3} stroke={isSel ? "#5b8def" : "none"} />
              <circle cx={p.x} cy={p.y} r={6} fill="#0b0e14" stroke={color} strokeWidth={1.2} />
              <circle cx={p.x} cy={p.y} r={6} fill={color} opacity={0.12 + 0.88 * p.node.mastery} />
              <text x={p.x + 11} y={p.y + 3.5} fontSize={11} fill={isSel ? "#ffffff" : "#c9d1de"}>
                {truncate(p.node.title, Math.floor((LAYOUT.colWidth - 64 - p.layer * LAYOUT.indent) / 5.8))}
              </text>
              <text x={right} y={p.y + 3.5} fontSize={10} textAnchor="end" fill="#6b7586" fontFamily="JetBrains Mono, monospace">
                {Math.round(p.node.mastery * 100)}
              </text>
            </g>
          );
        })}
      </svg>
      {hover && hovered && (
        <div
          className="pointer-events-none absolute z-30 w-[260px] rounded border border-line bg-s2 p-2.5 text-xs shadow-xl shadow-black/50"
          style={{ left: Math.min(hover.x + 14, (wrap.current?.clientWidth ?? 600) - 270), top: hover.y + 14 }}
        >
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full" style={{ background: PILLAR_COLOR[hovered.node.pillar] }} />
            <span className="font-medium text-fg">{hovered.node.title}</span>
          </div>
          <div className="mt-0.5 font-mono text-2xs text-muted">
            {hovered.node.id}
            {hovered.node.tier && hovered.node.tier !== "core" && <span className="ml-1.5 uppercase text-warn">{hovered.node.tier}</span>}
          </div>
          {hovered.node.summary && <p className="mt-1.5 text-muted">{hovered.node.summary}</p>}
          <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-2xs">
            <div>
              <div className="text-muted">p_known</div>
              <div className="text-fg">{hovered.node.mastery.toFixed(2)}</div>
            </div>
            <div>
              <div className="text-muted">attempts</div>
              <div className="text-fg">{hovered.node.attempts}</div>
            </div>
            <div>
              <div className="text-muted">prereqs</div>
              <div className="text-fg">{hovered.node.prereqs.length}</div>
            </div>
          </div>
          <div className="mt-2 text-2xs text-muted">{sel.has(hovered.node.id) ? "Click to remove from targets" : "Click to target in next case"}</div>
        </div>
      )}
    </div>
  );
});
