import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer } from "recharts";
import type { RubricGrade } from "../../api/types";
import { humanize } from "../../lib/format";

function scoreTone(s: number): string {
  return s >= 7.5 ? "bg-ok" : s >= 5 ? "bg-warn" : "bg-bad";
}

/** Rubric radar + per-dimension bars with evidence quotes and gaps. */
export function RubricView({ rubric }: { rubric: RubricGrade }) {
  const dims = Object.entries(rubric.dimensions);
  const data = dims.map(([k, v]) => ({ dim: humanize(k).replace(" ", "\n"), score: v.score }));

  return (
    <div className="min-w-0">
      <div className="field-label">LLM rubric</div>
      <div className="rounded border border-line bg-s1">
        <div className="h-[170px]">
          <ResponsiveContainer width="100%" height="100%">
            <RadarChart data={data} outerRadius="70%" margin={{ top: 8, right: 24, bottom: 8, left: 24 }}>
              <PolarGrid stroke="#232a36" />
              <PolarAngleAxis dataKey="dim" tick={{ fill: "#8b95a7", fontSize: 10 }} />
              <PolarRadiusAxis domain={[0, 10]} tick={false} axisLine={false} />
              <Radar dataKey="score" stroke="#5b8def" fill="#5b8def" fillOpacity={0.25} isAnimationActive={false} />
            </RadarChart>
          </ResponsiveContainer>
        </div>
        <div className="divide-y divide-[#1a2029] border-t border-line">
          {dims.map(([k, v]) => (
            <details key={k} className="group px-2.5 py-1.5">
              <summary className="flex cursor-pointer list-none items-center gap-2 text-xs">
                <span className="w-[120px] shrink-0 text-fg">{humanize(k)}</span>
                <div className="h-1.5 flex-1 overflow-hidden rounded-sm bg-s3">
                  <div className={`h-full ${scoreTone(v.score)}`} style={{ width: `${v.score * 10}%` }} />
                </div>
                <span className="num w-8 text-right text-fg">{v.score.toFixed(1)}</span>
              </summary>
              <div className="mt-1.5 space-y-1 pl-1 text-xs">
                {v.evidence.map((e, i) => (
                  <p key={`e${i}`} className="border-l-2 border-ok/50 pl-2 italic text-[#c2cad8]">{e}</p>
                ))}
                {v.gaps.map((g, i) => (
                  <p key={`g${i}`} className="border-l-2 border-bad/50 pl-2 text-muted">{g}</p>
                ))}
              </div>
            </details>
          ))}
        </div>
      </div>
    </div>
  );
}
