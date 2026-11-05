import { AlertOctagon, CheckCircle2, XCircle } from "lucide-react";
import type { GradeReport } from "../../api/types";
import { fmtPct } from "../../lib/format";
import { MetricTable } from "./MetricTable";
import { RubricView } from "./RubricView";
import { SkillUpdates } from "./SkillUpdates";
import { Debrief } from "./Debrief";

function Stat({ label, value, tone = "text-fg" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="bg-s1 px-3 py-1.5">
      <div className="text-2xs uppercase tracking-wider text-muted">{label}</div>
      <div className={`num text-base font-medium ${tone}`}>{value}</div>
    </div>
  );
}

export function GradeReportView({ report }: { report: GradeReport }) {
  const d = report.deterministic;
  const r = report.rubric;
  const tone = (x: number) => (x >= 0.75 ? "text-ok" : x >= 0.5 ? "text-warn" : "text-bad");

  return (
    <div className="h-full overflow-auto">
      <div className="grid grid-cols-[repeat(4,minmax(0,1fr))_minmax(0,2.4fr)] gap-px border-b border-line bg-line">
        <Stat label="Metric accuracy" value={fmtPct(d.accuracy, 0)} tone={tone(d.accuracy)} />
        <Stat label="Rubric overall" value={`${r.overall.toFixed(1)}/10`} tone={tone(r.overall / 10)} />
        <Stat
          label="Query signatures"
          value={`${d.query_signature_matches.filter((q) => q.matched).length}/${d.query_signature_matches.length}`}
        />
        <Stat label="Submission" value={report.submission_id} tone="text-muted" />
        <div className="flex flex-col justify-center gap-0.5 bg-s1 px-3 py-1.5 text-xs">
          {r.misdiagnosis && (
            <div className="flex items-center gap-1 font-medium text-bad">
              <AlertOctagon size={12} aria-hidden /> Misdiagnosis: root cause not identified
            </div>
          )}
          <div className="truncate" title={r.strongest_point}><span className="text-ok">▲ </span>{r.strongest_point}</div>
          <div className="truncate" title={r.weakest_point}><span className="text-bad">▼ </span>{r.weakest_point}</div>
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)] gap-3 p-3">
        <div className="min-w-0 space-y-3">
          <MetricTable results={d.metric_results} />
          <div>
            <div className="field-label">Reference-query signatures</div>
            <ul className="space-y-0.5">
              {d.query_signature_matches.map((q) => (
                <li key={q.name} className="flex items-center gap-2 text-xs">
                  {q.matched ? <CheckCircle2 size={12} className="text-ok" aria-label="matched" /> : <XCircle size={12} className="text-bad" aria-label="not matched" />}
                  <span className="font-mono text-fg">{q.name}</span>
                  <span className="truncate text-muted">{q.detail}</span>
                </li>
              ))}
            </ul>
          </div>
          <SkillUpdates updates={report.skill_updates} nextDrill={r.next_drill_nodes} />
        </div>
        <RubricView rubric={r} />
      </div>
      <div className="px-3 pb-3">
        <Debrief revealed={report.revealed} />
      </div>
    </div>
  );
}
