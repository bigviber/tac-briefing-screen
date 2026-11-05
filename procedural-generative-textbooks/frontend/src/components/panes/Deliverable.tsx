import { useEffect, useState } from "react";
import { ClipboardCheck, FilePenLine, Send } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { Tabs, type TabDef } from "../ui/primitives";
import { MemoEditor } from "../deliverable/MemoEditor";
import { AsksForm } from "../deliverable/AsksForm";
import { EvidenceList } from "../deliverable/EvidenceList";
import { GradeReportView } from "../deliverable/GradeReportView";

type DTab = "draft" | "grade";

export function Deliverable() {
  const grade = useCase((s) => s.grade);
  const submitting = useCase((s) => s.submitting);
  const submit = useCase((s) => s.submit);
  const draft = useCase((s) => s.draft);
  const asks = useCase((s) => s.caseData?.asks ?? []);
  const [tab, setTab] = useState<DTab>("draft");

  useEffect(() => {
    if (grade) setTab("grade");
  }, [grade]);

  const filled = asks.filter((a) => (draft?.metrics[a.key] ?? "").trim() !== "").length;
  const evidence = (draft?.sqlEvidence.length ?? 0) + (draft?.pythonEvidence.length ?? 0) + (draft?.designAttached ? 1 : 0) + (draft?.modelAttached ? 1 : 0);
  const ready = (draft?.memo.trim().length ?? 0) > 0 || filled > 0;

  const tabs: TabDef<DTab>[] = [
    { id: "draft", label: "Deliverable", icon: FilePenLine },
    {
      id: "grade",
      label: "Grade report",
      icon: ClipboardCheck,
      badge: grade ? <span className="num ml-1 text-ok">{grade.rubric.overall.toFixed(1)}</span> : undefined,
    },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <Tabs
        tabs={tabs}
        value={tab}
        onChange={(t) => (t === "grade" && !grade ? undefined : setTab(t))}
        right={
          <>
            <span className="num text-2xs text-muted">
              asks {filled}/{asks.length} · evidence {evidence}
            </span>
            <button className="btn btn-primary btn-sm" onClick={() => void submit()} disabled={submitting || !ready} title={ready ? "Submit for grading" : "Write a memo or fill an ask first"}>
              <Send size={11} aria-hidden /> {submitting ? "Grading…" : grade ? "Resubmit" : "Submit"}
            </button>
          </>
        }
      />
      <div className="min-h-0 flex-1" role="tabpanel">
        {tab === "draft" || !grade ? (
          <div className="flex h-full min-h-0">
            <div className="min-w-0 flex-1 border-r border-line">
              <MemoEditor />
            </div>
            <div className="flex w-[300px] shrink-0 flex-col overflow-auto">
              <AsksForm />
              <EvidenceList />
            </div>
          </div>
        ) : (
          <GradeReportView report={grade} />
        )}
      </div>
    </div>
  );
}
