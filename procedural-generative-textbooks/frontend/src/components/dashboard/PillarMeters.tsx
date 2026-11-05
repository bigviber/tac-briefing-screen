import { PILLARS } from "../../api/types";
import { PILLAR_COLOR, PILLAR_SHORT, PILLAR_LABEL } from "../../lib/pillars";
import { pillarMastery, useSkills } from "../../store/skillsStore";
import { Meter } from "../ui/primitives";

/** Five compact mastery meters, one per pillar. */
export function PillarMeters() {
  const graph = useSkills((s) => s.graph);
  const m = pillarMastery(graph);
  return (
    <div className="flex items-center gap-4" aria-label="Mastery by pillar">
      {PILLARS.map((p) => (
        <div key={p} className="w-[92px]" title={`${PILLAR_LABEL[p]}: ${(m[p] * 100).toFixed(0)}% mean p_known`}>
          <div className="mb-1 flex items-baseline justify-between text-2xs">
            <span className="font-medium tracking-wider text-muted">{PILLAR_SHORT[p]}</span>
            <span className="num text-fg">{graph ? (m[p] * 100).toFixed(0) : "–"}</span>
          </div>
          <Meter value={m[p]} color={PILLAR_COLOR[p]} />
        </div>
      ))}
    </div>
  );
}
