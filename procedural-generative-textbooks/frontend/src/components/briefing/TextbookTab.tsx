import { useEffect, useMemo, useState } from "react";
import { BookOpen, ChevronRight } from "lucide-react";
import { PILLARS } from "../../api/types";
import { useCase } from "../../store/caseStore";
import { useSkills } from "../../store/skillsStore";
import { PILLAR_COLOR, PILLAR_LABEL } from "../../lib/pillars";
import { Markdown } from "../ui/Markdown";
import { EmptyState, ErrorLine, SkeletonBlock } from "../ui/primitives";

export function TextbookTab() {
  const c = useCase((s) => s.caseData);
  const chapters = useCase((s) => s.chapters);
  const loadingNode = useCase((s) => s.chapterLoading);
  const stream = useCase((s) => s.chapterStream);
  const error = useCase((s) => s.chapterError);
  const loadChapter = useCase((s) => s.loadChapter);
  const graph = useSkills((s) => s.graph);
  const loadGraph = useSkills((s) => s.load);
  const [node, setNode] = useState<string>(c?.skill_nodes[0] ?? "");

  useEffect(() => {
    if (!graph) void loadGraph();
  }, [graph, loadGraph]);

  useEffect(() => {
    if (node) void loadChapter(node);
  }, [node, loadChapter]);

  const titleOf = useMemo(() => {
    const m = new Map(graph?.nodes.map((n) => [n.id, n]) ?? []);
    return (id: string) => m.get(id)?.title ?? id;
  }, [graph]);

  if (!c) return null;
  const chapter = chapters[node];
  const caseNodes = new Set(c.skill_nodes);

  return (
    <div className="flex min-h-full flex-col">
      <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-bg px-3 py-2">
        <label htmlFor="tb-node" className="text-2xs uppercase tracking-wider text-muted">Skill node</label>
        <select id="tb-node" className="input h-6 flex-1 text-xs" value={node} onChange={(e) => setNode(e.target.value)}>
          <optgroup label="This case">
            {c.skill_nodes.map((id) => (
              <option key={id} value={id}>{titleOf(id)}</option>
            ))}
          </optgroup>
          {graph &&
            PILLARS.map((p) => (
              <optgroup key={p} label={PILLAR_LABEL[p]}>
                {graph.nodes.filter((n) => n.pillar === p && !caseNodes.has(n.id)).map((n) => (
                  <option key={n.id} value={n.id}>{n.title}</option>
                ))}
              </optgroup>
            ))}
        </select>
      </div>
      <div className="flex flex-wrap gap-1 px-3 pt-2">
        {c.skill_nodes.map((id) => {
          const n = graph?.nodes.find((x) => x.id === id);
          return (
            <button
              key={id}
              onClick={() => setNode(id)}
              className={`chip ${id === node ? "border-accent text-fg" : "hover:text-fg"}`}
            >
              {n && <span className="h-1.5 w-1.5 rounded-full" style={{ background: PILLAR_COLOR[n.pillar] }} />}
              {titleOf(id)}
              {chapters[id] && <span className="text-ok">✓</span>}
            </button>
          );
        })}
      </div>
      <div className="flex-1 p-3">
        {error && !chapter && <ErrorLine message={`Textbook: ${error}`} onRetry={() => void loadChapter(node)} />}
        {loadingNode === node && !stream && <SkeletonBlock lines={10} />}
        {loadingNode === node && stream && <Markdown>{stream}</Markdown>}
        {!chapter && !loadingNode && !error && <EmptyState icon={BookOpen} title="Pick a skill node">Chapters are synthesized for this case and cached.</EmptyState>}
        {chapter && (
          <article>
            <h2 className="mb-2 text-[15px] font-semibold text-white">{chapter.title}</h2>
            <Markdown>{chapter.markdown}</Markdown>
            {chapter.self_checks.length > 0 && (
              <div className="mt-4">
                <div className="field-label">Self-checks</div>
                <div className="space-y-1.5">
                  {chapter.self_checks.map((sc, i) => (
                    <details key={i} className="group rounded border border-line bg-s1">
                      <summary className="flex cursor-pointer list-none items-start gap-1.5 px-2.5 py-2 text-sm text-fg">
                        <ChevronRight size={13} className="mt-0.5 shrink-0 text-muted transition-transform group-open:rotate-90" aria-hidden />
                        <span>{sc.question}</span>
                      </summary>
                      <div className="border-t border-[#1a2029] px-2.5 py-2 pl-7 text-sm text-[#c2cad8]">{sc.answer}</div>
                    </details>
                  ))}
                </div>
              </div>
            )}
          </article>
        )}
      </div>
    </div>
  );
}
