import { FastForward, LineChart as LineIcon } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { fmtCents } from "../../lib/format";
import { SimCard, KpiTrend } from "./SimCharts";

function leverDisplay(v: number, unit: string): string {
  if (unit === "pct") return `${(v * 100).toFixed(1)}%`;
  if (unit === "cents") return fmtCents(v, { compact: true });
  if (unit.startsWith("%")) return `${Number.isInteger(v) ? v : v.toFixed(1)}${unit}`;
  if (unit === "index") return v.toFixed(2);
  return `${Number.isInteger(v) ? v : v.toFixed(1)} ${unit}`;
}

export function QuarterSim() {
  const levers = useCase((s) => s.caseData?.levers ?? []);
  const values = useCase((s) => s.levers);
  const setLever = useCase((s) => s.setLever);
  const sim = useCase((s) => s.sim);
  const grade = useCase((s) => s.grade);
  const advancing = useCase((s) => s.advancing);
  const advance = useCase((s) => s.advance);
  const exhausted = useCase((s) => s.simExhausted);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line bg-s1 px-3">
        <LineIcon size={13} className="text-muted" aria-hidden />
        <span className="text-xs font-medium uppercase tracking-wider text-muted">Quarter simulation</span>
        <button className="btn btn-primary btn-sm ml-auto" onClick={() => void advance()} disabled={!grade || advancing || exhausted !== null} title={exhausted ?? (grade ? "Apply levers and simulate the next quarter" : "Submit first")}>
          <FastForward size={11} aria-hidden /> {advancing ? "Simulating…" : "Advance quarter"}
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="space-y-2 border-b border-line p-3">
          {levers.length === 0 && <div className="text-xs text-muted">This case exposes no decision levers.</div>}
          {levers.map((l) => {
            const v = values[l.key] ?? l.default;
            const step = l.unit === "FTE" || l.unit === "days" ? 1 : (l.max - l.min) / 100;
            return (
              <div key={l.key} title={l.effect}>
                <div className="flex items-baseline justify-between text-xs">
                  <label htmlFor={`lever-${l.key}`} className="text-fg">{l.label}</label>
                  <span className="num text-accent">{leverDisplay(v, l.unit)}</span>
                </div>
                <input
                  id={`lever-${l.key}`}
                  type="range"
                  min={l.min}
                  max={l.max}
                  step={step}
                  value={v}
                  onChange={(e) => setLever(l.key, Number(e.target.value))}
                  className="h-4 w-full accent-[#5b8def]"
                />
                <div className="flex justify-between gap-2 font-mono text-2xs text-[#4a5466]">
                  <span className="shrink-0 whitespace-nowrap">{leverDisplay(l.min, l.unit)}</span>
                  <span className="min-w-0 truncate font-sans text-muted">{l.effect}</span>
                  <span className="shrink-0 whitespace-nowrap">{leverDisplay(l.max, l.unit)}</span>
                </div>
              </div>
            );
          })}
        </div>
        {exhausted && <p className="border-b border-line px-3 py-1.5 text-xs text-warn">Quarter budget exhausted: {exhausted}</p>}
        {sim.length === 0 ? (
          <p className="p-3 text-xs text-muted">{grade ? "Set levers and advance to see the next quarter play out." : "Unlocks after grading. Decisions move KPIs through the case's deterministic transition model."}</p>
        ) : (
          <div className="space-y-2 p-3">
            <KpiTrend history={sim} />
            {[...sim].reverse().map((q) => (
              <SimCard key={q.period} q={q} prev={sim[sim.indexOf(q) - 1]} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

