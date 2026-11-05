import { Swords } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { Thread } from "../adversary/Thread";
import { QuarterSim } from "../adversary/QuarterSim";

export function Adversary() {
  const c = useCase((s) => s.caseData);
  const thread = useCase((s) => s.thread);
  const lastPersona = [...thread].reverse().find((t) => t.role === "adversary" && t.turn.persona);
  const persona = lastPersona?.role === "adversary" ? lastPersona.turn.persona : c?.dossier_seed.stakeholders[1]?.name ?? "Socratic adversary";
  const stakeholder = c?.dossier_seed.stakeholders.find((s) => persona.startsWith(s.name));
  const conceded = thread.some((t) => t.role === "adversary" && t.turn.concedes);

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line bg-s1 px-3">
        <Swords size={13} className="text-bad" aria-hidden />
        <span className="text-xs font-medium uppercase tracking-wider text-muted">Adversary</span>
        {conceded && <span className="chip ml-auto border-ok/40 text-ok">conceded</span>}
      </div>
      <div className="flex shrink-0 items-center gap-2.5 border-b border-line px-3 py-2">
        <div className="flex h-7 w-7 items-center justify-center rounded bg-bad/15 text-xs font-semibold text-bad">
          {persona.split(/\s+/).map((w) => w[0]).join("").slice(0, 2)}
        </div>
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-fg">{persona}</div>
          <div className="truncate text-2xs text-muted">
            {stakeholder ? `${stakeholder.role} · ${stakeholder.disposition} · agenda: ${stakeholder.agenda}` : "Challenges your diagnosis after grading"}
          </div>
        </div>
      </div>
      <div className="flex min-h-0 flex-[1.1] flex-col">
        <Thread />
      </div>
      <div className="flex min-h-0 flex-1 flex-col border-t border-line">
        <QuarterSim />
      </div>
    </div>
  );
}
