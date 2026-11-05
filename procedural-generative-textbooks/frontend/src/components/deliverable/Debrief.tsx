import { ChevronRight } from "lucide-react";
import type { Revealed } from "../../api/types";
import { PILLAR_COLOR } from "../../lib/pillars";
import { Markdown } from "../ui/Markdown";
import { Mermaid } from "../ui/Mermaid";

const SEV: Record<string, string> = { low: "text-muted", medium: "text-warn", high: "text-[#ff8a5b]", critical: "text-bad" };

/** Revealed ground truth: root cause, anomalies, reference solution. Collapsed by default. */
export function Debrief({ revealed }: { revealed: Revealed }) {
  const ref = revealed.reference_solution;
  return (
    <details className="group rounded border border-line bg-s1">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm font-medium text-fg">
        <ChevronRight size={13} className="text-muted transition-transform group-open:rotate-90" aria-hidden />
        Debrief: root cause &amp; reference solution
        <span className="ml-auto text-2xs font-normal text-muted">{revealed.anomalies.length} anomalies revealed</span>
      </summary>
      <div className="space-y-3 border-t border-line p-3">
        <div>
          <div className="field-label">Root cause</div>
          <p className="text-sm leading-relaxed text-fg">{revealed.root_cause}</p>
        </div>
        <div>
          <div className="field-label">Injected anomalies</div>
          <div className="space-y-1.5">
            {revealed.anomalies.map((a) => (
              <div key={a.id} className="rounded border border-line bg-bg p-2 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-muted">{a.id}</span>
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: PILLAR_COLOR[a.pillar] }} />
                  <span className="font-medium text-fg">{a.title}</span>
                  <span className={`ml-auto font-mono uppercase ${SEV[a.severity]}`}>{a.severity}</span>
                </div>
                <p className="mt-1 text-muted">{a.mechanism}</p>
                <div className="mt-1 font-mono text-2xs text-muted">{a.periods.join(" · ")}</div>
              </div>
            ))}
          </div>
        </div>
        {ref.memo_bullets && ref.memo_bullets.length > 0 && (
          <div>
            <div className="field-label">Reference memo</div>
            <Markdown>{ref.memo_bullets.map((b) => `- ${b}`).join("\n")}</Markdown>
          </div>
        )}
        {ref.sql && (
          <div>
            <div className="field-label">Reference SQL</div>
            <Markdown>{"```sql\n" + ref.sql + "\n```"}</Markdown>
          </div>
        )}
        {ref.python && (
          <div>
            <div className="field-label">Reference Python</div>
            <Markdown>{"```python\n" + ref.python + "\n```"}</Markdown>
          </div>
        )}
        {ref.design_mermaid && (
          <div>
            <div className="field-label">Reference design</div>
            <Mermaid code={ref.design_mermaid} />
          </div>
        )}
      </div>
    </details>
  );
}
