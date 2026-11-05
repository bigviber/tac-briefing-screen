import { useEffect } from "react";
import { Network, RotateCcw } from "lucide-react";
import { useSkills } from "../store/skillsStore";
import { SkillGraph } from "../components/SkillGraph";
import { GeneratePanel } from "../components/dashboard/GeneratePanel";
import { RecentCases } from "../components/dashboard/RecentCases";
import { UsageStrip } from "../components/dashboard/UsageStrip";
import { PillarMeters } from "../components/dashboard/PillarMeters";
import { Frontier } from "../components/dashboard/Frontier";
import { ErrorLine, SkeletonBlock } from "../components/ui/primitives";
import { ErrorBoundary } from "../components/ui/ErrorBoundary";

export function Dashboard() {
  const graph = useSkills((s) => s.graph);
  const error = useSkills((s) => s.error);
  const load = useSkills((s) => s.load);
  const reset = useSkills((s) => s.reset);
  const targets = useSkills((s) => s.targets);
  const toggle = useSkills((s) => s.toggleTarget);

  useEffect(() => {
    if (!graph) void load();
  }, [graph, load]);

  const avg = graph ? graph.nodes.reduce((s, n) => s + n.mastery, 0) / graph.nodes.length : 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-14 shrink-0 items-center gap-6 border-b border-line px-4">
        <div>
          <h1 className="text-base font-semibold text-fg">Skill map</h1>
          <div className="text-xs text-muted">
            {graph ? `${graph.nodes.length} nodes · ${graph.edges.length} prerequisite edges · mean p_known ${avg.toFixed(2)}` : "Loading taxonomy…"}
          </div>
        </div>
        <div className="ml-auto">
          <PillarMeters />
        </div>
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_400px] gap-3 p-3">
        <section className="panel flex min-h-0 flex-col" aria-labelledby="graph-title">
          <div className="panel-head">
            <Network size={13} aria-hidden />
            <span id="graph-title">Mastery DAG</span>
            <span className="ml-3 normal-case tracking-normal text-muted">fill = p_known · dashed = cross-pillar prerequisite · click to target</span>
            <button className="btn btn-ghost btn-sm ml-auto normal-case tracking-normal" onClick={() => void reset()}>
              <RotateCcw size={12} aria-hidden /> Reset mastery
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-auto p-2">
            {error && <ErrorLine message={error} onRetry={() => void load()} />}
            {!graph && !error && <SkeletonBlock lines={14} />}
            {graph && (
              <ErrorBoundary label="Skill graph">
                <SkillGraph graph={graph} selected={targets} onToggle={toggle} />
                <Frontier graph={graph} selected={targets} onToggle={toggle} />
              </ErrorBoundary>
            )}
          </div>
        </section>
        <div className="flex min-h-0 flex-col gap-3">
          <GeneratePanel />
          <RecentCases />
          <UsageStrip />
        </div>
      </div>
    </div>
  );
}
