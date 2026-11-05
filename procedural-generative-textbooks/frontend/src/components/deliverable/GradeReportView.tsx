import { AlertOctagon, CheckCircle2, CircleSlash, Info, XCircle } from "lucide-react";
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
  // The backend always sends a ceiling (10 = unconstrained); only a lower one is worth a note.
  const ceiling = r.ceiling_te !== null && r.ceiling_te < 10 ? r.ceiling_te : null;
  const rawTe = r.raw_scores.technical_execution;
  const capped = ceiling !== null && rawTe !== undefined && rawTe > ceiling;

  return (
    <div className="h-full overflow-auto">
      <div className="grid grid-cols-[repeat(4,minmax(0,1fr))_minmax(0,2.4fr)] gap-px border-b border-line bg-line">
        <Stat label="Metric accuracy" value={fmtPct(d.accuracy, 0)} tone={tone(d.accuracy)} />
        <Stat label="Rubric overall" value={`${r.overall.toFixed(1)}/10`} tone={tone(r.overall / 10)} />
        <Stat
          label="Deterministic items"
          value={d.items_total > 0 ? `${d.items_passed}/${d.items_total}` : `${d.query_signature_matches.filter((q) => q.matched).length}/${d.query_signature_matches.length}`}
          tone={d.items_total > 0 ? tone(d.det_pass) : "text-fg"}
        />
        <Stat label="Submission" value={report.submission_id} tone="text-muted" />
        <div className="flex flex-col justify-center gap-0.5 bg-s1 px-3 py-1.5 text-xs">
          {/* partial_diagnosis rides in raw_scores (RubricGrade has no field for it): right area, wrong mechanism. */}
          {r.misdiagnosis && r.raw_scores.partial_diagnosis === 1 ? (
            <div className="flex items-center gap-1 font-medium text-warn" title="Mastery counts this as a borderline miss (reduced penalty), not a full miss.">
              <AlertOctagon size={12} aria-hidden /> Partial diagnosis: right area, wrong mechanism
            </div>
          ) : r.misdiagnosis ? (
            <div className="flex items-center gap-1 font-medium text-bad">
              <AlertOctagon size={12} aria-hidden /> Misdiagnosis: root cause not identified
            </div>
          ) : null}
          <div className="truncate" title={r.strongest_point}><span className="text-ok">▲ </span>{r.strongest_point}</div>
          <div className="truncate" title={r.weakest_point}><span className="text-bad">▼ </span>{r.weakest_point}</div>
        </div>
      </div>

      {(ceiling !== null || r.clamps.length > 0 || !report.counted_for_mastery) && (
        <div className="space-y-0.5 border-b border-line bg-s1 px-3 py-1.5 text-xs text-[#c2cad8]">
          {ceiling !== null && (
            <div className="flex items-start gap-1.5">
              <Info size={12} className={`mt-0.5 shrink-0 ${capped ? "text-warn" : "text-muted"}`} aria-hidden />
              <span>
                {capped ? "Technical execution capped at " : "Technical execution ceiling "}
                <span className="num text-fg">{ceiling.toFixed(1)}</span> from the deterministic check
                {d.items_total > 0 && <> ({d.items_passed}/{d.items_total} items passed)</>}
                {capped && <>; the model scored it {rawTe.toFixed(1)}</>}.
              </span>
            </div>
          )}
          {r.clamps
            .filter((c) => ceiling === null || !/^technical_execution/i.test(c))
            .map((c) => (
              <div key={c} className="flex items-start gap-1.5">
                <Info size={12} className="mt-0.5 shrink-0 text-muted" aria-hidden />
                <span>{c}</span>
              </div>
            ))}
          {!report.counted_for_mastery && (
            <div className="flex items-start gap-1.5 text-muted">
              <CircleSlash size={12} className="mt-0.5 shrink-0" aria-hidden />
              <span>Resubmission: graded for feedback, not counted toward skill mastery.</span>
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)] gap-3 p-3">
        <div className="min-w-0 space-y-3">
          <MetricTable results={d.metric_results} />
          <div>
            <div className="field-label">Reference-query signatures</div>
            <ul className="space-y-0.5">
              {d.query_signature_matches.map((q) => (
                <li key={q.name} className="flex items-center gap-2 text-xs" title={q.method === "execution" ? "Checked by executing your SQL read-only" : "Checked by query signature"}>
                  {!q.evaluated ? (
                    <CircleSlash size={12} className="shrink-0 text-muted" aria-label="not evaluated" />
                  ) : q.matched ? (
                    <CheckCircle2 size={12} className="shrink-0 text-ok" aria-label="matched" />
                  ) : (
                    <XCircle size={12} className="shrink-0 text-bad" aria-label="not matched" />
                  )}
                  <span className="font-mono text-fg">{q.name}</span>
                  {q.method === "execution" && <span className="chip h-4 px-1 text-2xs">exec</span>}
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
        <Debrief revealed={report.revealed} statedRootCause={r.stated_root_cause} />
      </div>
    </div>
  );
}
