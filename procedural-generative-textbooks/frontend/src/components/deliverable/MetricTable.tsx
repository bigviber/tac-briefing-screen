import { Fragment, useState } from "react";
import { ChevronRight } from "lucide-react";
import type { MetricResult } from "../../api/types";
import { fmtMetric, isCents } from "../../lib/format";

function tolerance(m: MetricResult): string {
  if (m.tolerance_abs !== null) return `± ${fmtMetric(m.tolerance_abs, m.unit, m.key)}`;
  if (m.tolerance_rel !== null) return `± ${(m.tolerance_rel * 100).toFixed(0)}%`;
  return "exact";
}

/** Raw value tooltip for cents metrics, so the dollar rendering can be checked against the backend. */
function rawTitle(m: MetricResult, v: number | null): string | undefined {
  return v !== null && isCents(m.key, m.unit) ? `${v.toLocaleString("en-US")} cents` : undefined;
}

/** Deterministic grade: submitted vs expected per metric (cents metrics in dollars), with derivation reveal. */
export function MetricTable({ results }: { results: MetricResult[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div>
      <div className="field-label">Deterministic check</div>
      <table className="tbl rounded border border-line">
        <thead>
          <tr>
            <th>metric</th>
            <th className="text-right">submitted</th>
            <th className="text-right">expected</th>
            <th className="text-right">tolerance</th>
            <th className="text-center">result</th>
          </tr>
        </thead>
        <tbody>
          {results.map((m) => {
            const isOpen = open === m.key;
            return (
              <Fragment key={m.key}>
                <tr className="cursor-pointer hover:bg-s2" onClick={() => setOpen(isOpen ? null : m.key)}>
                  <td className="font-mono text-fg">
                    <button className="inline-flex items-center gap-1" aria-expanded={isOpen} aria-label={`Show derivation for ${m.key}`}>
                      <ChevronRight size={11} className={`text-muted transition-transform ${isOpen ? "rotate-90" : ""}`} aria-hidden />
                      {m.key}
                    </button>
                  </td>
                  <td className={`num text-right ${m.submitted === null ? "text-muted" : "text-fg"}`} title={rawTitle(m, m.submitted)}>{fmtMetric(m.submitted, m.unit, m.key)}</td>
                  <td className="num text-right text-fg" title={rawTitle(m, m.expected)}>{fmtMetric(m.expected, m.unit, m.key)}</td>
                  <td className="num text-right text-muted">{tolerance(m)}</td>
                  <td className="text-center">
                    <span className={`chip ${m.within_tolerance ? "border-ok/40 text-ok" : "border-bad/40 text-bad"}`}>
                      {m.within_tolerance ? "PASS" : m.submitted === null ? "MISSING" : "FAIL"}
                    </span>
                  </td>
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={5} className="bg-s1 text-xs text-[#c2cad8]">
                      <span className="mr-1 text-2xs uppercase tracking-wider text-muted">Derivation</span>
                      {m.derivation}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
