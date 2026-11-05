import { BookOpen, Database, FileText, Network } from "lucide-react";
import { useUi, type BriefTab } from "../../store/uiStore";
import { useCase } from "../../store/caseStore";
import { Tabs, type TabDef } from "../ui/primitives";
import { DossierTab } from "../briefing/DossierTab";
import { TextbookTab } from "../briefing/TextbookTab";
import { DataRoomTab } from "../briefing/DataRoomTab";
import { Mermaid } from "../ui/Mermaid";
import { EmptyState } from "../ui/primitives";

const TABS: TabDef<BriefTab>[] = [
  { id: "dossier", label: "Dossier", icon: FileText },
  { id: "textbook", label: "Textbook", icon: BookOpen },
  { id: "data", label: "Data Room", icon: Database },
  { id: "architecture", label: "Architecture", icon: Network },
];

function ArchitectureTab() {
  const arch = useCase((s) => s.dossier?.architecture_mermaid || s.caseData?.dossier_seed.architecture_mermaid);
  if (!arch) {
    return (
      <EmptyState icon={Network} title="No architecture diagram">
        This archetype has no system-design component. System-design cases ship a diagram here.
      </EmptyState>
    );
  }
  return (
    <div className="p-3">
      <div className="mb-2 text-xs text-muted">As-is architecture from the data room. Propose changes in the Sandbox → Design tab.</div>
      <div className="overflow-auto rounded border border-line bg-bg p-3">
        <Mermaid code={arch} natural />
      </div>
    </div>
  );
}

export function Briefing() {
  const tab = useUi((s) => s.briefTab);
  const setTab = useUi((s) => s.setBriefTab);
  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <Tabs tabs={TABS} value={tab} onChange={setTab} />
      <div className="min-h-0 flex-1 overflow-auto" role="tabpanel">
        {tab === "dossier" && <DossierTab />}
        {tab === "textbook" && <TextbookTab />}
        {tab === "data" && <DataRoomTab />}
        {tab === "architecture" && <ArchitectureTab />}
      </div>
    </div>
  );
}
