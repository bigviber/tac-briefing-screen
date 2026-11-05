import { useEffect } from "react";
import { AlertTriangle } from "lucide-react";
import { useCase } from "../store/caseStore";
import { useUi } from "../store/uiStore";
import { navigate } from "../lib/route";
import { ResizeHandle } from "../components/ui/ResizeHandle";
import { ErrorBoundary } from "../components/ui/ErrorBoundary";
import { EmptyState, SkeletonBlock } from "../components/ui/primitives";
import { Briefing } from "../components/panes/Briefing";
import { Sandbox } from "../components/panes/Sandbox";
import { Deliverable } from "../components/panes/Deliverable";
import { Adversary } from "../components/panes/Adversary";

/** 4-pane IDE: Briefing | (Sandbox / Deliverable) | Adversary. Sizes persist in localStorage. */
export function Workbench({ caseId }: { caseId: string }) {
  const open = useCase((s) => s.open);
  const caseData = useCase((s) => s.caseData);
  const loading = useCase((s) => s.caseLoading);
  const error = useCase((s) => s.caseError);
  const panes = useUi((s) => s.panes);
  const setPane = useUi((s) => s.setPane);

  useEffect(() => {
    void open(caseId);
  }, [caseId, open]);

  if (error) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="Could not load case"
        action={
          <div className="flex gap-2">
            <button className="btn btn-sm" onClick={() => { useCase.setState({ caseId: null }); void open(caseId); }}>Retry</button>
            <button className="btn btn-sm" onClick={() => navigate({ name: "dashboard" })}>Dashboard</button>
          </div>
        }
      >
        <span className="font-mono">{error}</span>
      </EmptyState>
    );
  }

  if (loading || !caseData || caseData.case_id !== caseId) {
    return (
      <div className="grid h-full grid-cols-[420px_1fr_360px] gap-px bg-line">
        {[0, 1, 2].map((i) => (
          <div key={i} className="bg-bg">
            <SkeletonBlock lines={10} />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0">
      <section className="flex min-h-0 shrink-0 flex-col" style={{ width: panes.left }} aria-label="Briefing">
        <ErrorBoundary label="Briefing">
          <Briefing />
        </ErrorBoundary>
      </section>
      <ResizeHandle axis="col" size={panes.left} direction={1} onResize={(px) => setPane("left", px)} label="Resize briefing pane" />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <section className="flex min-h-0 flex-1 flex-col" aria-label="Sandbox">
          <ErrorBoundary label="Sandbox">
            <Sandbox />
          </ErrorBoundary>
        </section>
        <ResizeHandle axis="row" size={panes.bottom} direction={-1} onResize={(px) => setPane("bottom", px)} label="Resize deliverable pane" />
        <section className="flex min-h-0 shrink-0 flex-col" style={{ height: panes.bottom }} aria-label="Deliverable">
          <ErrorBoundary label="Deliverable">
            <Deliverable />
          </ErrorBoundary>
        </section>
      </div>
      <ResizeHandle axis="col" size={panes.right} direction={-1} onResize={(px) => setPane("right", px)} label="Resize adversary pane" />
      <section className="flex min-h-0 shrink-0 flex-col" style={{ width: panes.right }} aria-label="Adversary">
        <ErrorBoundary label="Adversary">
          <Adversary />
        </ErrorBoundary>
      </section>
    </div>
  );
}
