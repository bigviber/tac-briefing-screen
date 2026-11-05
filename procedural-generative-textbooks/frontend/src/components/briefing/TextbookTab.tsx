import { useEffect, useMemo, useState } from "react";
import { BookOpen, ChevronRight, Lock, Settings } from "lucide-react";
import { HTTP_CONFLICT, HTTP_LLM_NOT_CONFIGURED } from "../../api/client";
import { PILLARS, type TextbookSnippet } from "../../api/types";
import { useUi } from "../../store/uiStore";
import { useCase } from "../../store/caseStore";
import { useSkills } from "../../store/skillsStore";
import { PILLAR_COLOR, PILLAR_LABEL } from "../../lib/pillars";
import { Markdown } from "../ui/Markdown";
import { EmptyState, ErrorLine, RedactedBadge, SkeletonBlock } from "../ui/primitives";

function Snippets({ snippets }: { snippets: TextbookSnippet[] }) {
  const usable = snippets.filter((s) => typeof s.code === "string" && s.code.trim());
  if (usable.length === 0) return null;
  return (
    <div className="mt-4">
      <div className="field-label">Syntax patterns · {usable.length}</div>
      <div className="space-y-2">
        {usable.map((s, i) => {
          const lang = String(s.language ?? "").toLowerCase().includes("py") ? "python" : String(s.language ?? "sql").toLowerCase();
          return (
            <div key={i}>
              {s.purpose && <div className="mb-0.5 text-xs text-[#c2cad8]">{String(s.purpose)}</div>}
              <Markdown>{"```" + lang + "\n" + String(s.code) + "\n```"}</Markdown>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function TextbookTab() {
  const c = useCase((s) => s.caseData);
  const chapters = useCase((s) => s.chapters);
  const loadingNode = useCase((s) => s.chapterLoading);
  const stream = useCase((s) => s.chapterStream);
  const error = useCase((s) => s.chapterError);
  const errorStatus = useCase((s) => s.chapterErrorStatus);
  const graded = useCase((s) => s.grade !== null);
  const loadChapter = useCase((s) => s.loadChapter);
  const openSettings = useUi((s) => s.setSettings);
  const graph = useSkills((s) => s.graph);
  const loadGraph = useSkills((s) => s.load);
  const [node, setNode] = useState<string>(c?.skill_nodes[0] ?? "");

  useEffect(() => {
    if (!graph) void loadGraph();
  }, [graph, loadGraph]);

  // Re-run after grading: principal chapters unlock with the first submission (409 before).
  useEffect(() => {
    if (node) void loadChapter(node);
  }, [node, loadChapter, graded]);

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
        {error && !chapter && errorStatus === HTTP_CONFLICT && (
          <EmptyState icon={Lock} title="Chapter locked until your first submission">
            Principal cases use productive failure: attempt the diagnosis first, then the textbook unlocks for every skill node.
          </EmptyState>
        )}
        {error && !chapter && errorStatus === HTTP_LLM_NOT_CONFIGURED && (
          <EmptyState
            icon={Settings}
            title="Chapters need an LLM"
            action={
              <div className="flex gap-1.5">
                <button className="btn btn-sm btn-primary" onClick={() => openSettings(true, "llm")}>Configure LLM</button>
                <button className="btn btn-sm" onClick={() => void loadChapter(node, true)}>Retry</button>
              </div>
            }
          >
            Chapters are synthesized for this case by the configured model and cached.
          </EmptyState>
        )}
        {error && !chapter && errorStatus !== HTTP_CONFLICT && errorStatus !== HTTP_LLM_NOT_CONFIGURED && (
          <ErrorLine message={`Textbook: ${error}`} onRetry={() => void loadChapter(node, true)} />
        )}
        {loadingNode === node && !stream && <SkeletonBlock lines={10} />}
        {loadingNode === node && stream && <Markdown>{stream}</Markdown>}
        {!chapter && !loadingNode && !error && <EmptyState icon={BookOpen} title="Pick a skill node">Chapters are synthesized for this case and cached.</EmptyState>}
        {chapter && (
          <article>
            <div className="mb-2 flex items-start gap-2">
              <h2 className="flex-1 text-[15px] font-semibold text-white">{chapter.title}</h2>
              {chapter.leak_redacted && <RedactedBadge />}
            </div>
            <Markdown>{chapter.markdown}</Markdown>
            <Snippets snippets={chapter.snippets} />
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
