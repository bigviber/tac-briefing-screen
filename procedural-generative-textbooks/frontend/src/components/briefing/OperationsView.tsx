import { useCase } from "../../store/caseStore";
import { fmtNum, fmtPct, fmtUnit, humanize, unitFromKey } from "../../lib/format";

/** A value outside its physical range (utilization above 100%, a negative count) is shown as-is but flagged. */
function Odd({ text, why }: { text: string; why: string }) {
  return (
    <span className="text-warn" title={why}>
      {text} <span aria-hidden>⚠</span>
      <span className="sr-only"> ({why})</span>
    </span>
  );
}

function kpiCell(key: string, v: number | null | undefined) {
  const unit = unitFromKey(key);
  const text = fmtUnit(v, unit);
  if (typeof v === "number" && Number.isFinite(v)) {
    if (/utilization/.test(key) && v > 1) return <Odd text={text} why="utilization above 100%" />;
    if (unit === "pct" && v < 0) return <Odd text={text} why="negative share" />;
  }
  return text;
}

/**
 * Operations state from the public CaseWorld: stations, queueing, unit economics and KPIs by
 * period. Values are rendered as generated, including impossible ones (ρ > 1), which are flagged
 * rather than clamped so a data problem stays visible.
 */
export function OperationsView() {
  const ops = useCase((s) => s.caseData?.operations);
  if (!ops) return null;
  const periods = Object.keys(ops.kpis_by_period ?? {}).sort().slice(-6);
  const kpiKeys = [...new Set(periods.flatMap((p) => Object.keys(ops.kpis_by_period[p] ?? {})))];
  const flat = (rec: Record<string, number> | undefined) => Object.entries(rec ?? {});

  return (
    <div className="space-y-3 p-3">
      {ops.stations.length > 0 && (
        <section>
          <div className="field-label">Stations</div>
          <div className="overflow-auto rounded border border-line">
            <table className="tbl whitespace-nowrap">
              <thead>
                <tr>
                  <th>station</th>
                  <th className="text-right">capacity / h</th>
                  <th className="text-right">utilization</th>
                  <th className="text-right">WIP</th>
                  <th className="text-right">cycle time</th>
                </tr>
              </thead>
              <tbody>
                {ops.stations.map((st) => (
                  <tr key={st.name}>
                    <td className="text-fg">{st.name}</td>
                    <td className="num text-right">{fmtNum(st.capacity_per_hour)}</td>
                    <td className="num text-right">
                      {Number.isFinite(st.utilization) && st.utilization > 1 ? <Odd text={fmtPct(st.utilization)} why="utilization above 100% (ρ > 1)" /> : fmtPct(st.utilization)}
                    </td>
                    <td className="num text-right">{fmtNum(st.wip)}</td>
                    <td className="num text-right">{fmtNum(st.cycle_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {(flat(ops.unit_economics).length > 0 || flat(ops.queueing).length > 0) && (
        <section className="flex flex-wrap gap-1">
          {[...flat(ops.unit_economics), ...flat(ops.queueing)].map(([k, v]) => (
            <span key={k} className="chip">
              <span className="text-muted">{humanize(k)}</span>
              <span className="num text-fg">{kpiCell(k, v)}</span>
            </span>
          ))}
        </section>
      )}
      {kpiKeys.length > 0 && (
        <section>
          <div className="field-label">KPIs by period</div>
          <div className="overflow-auto rounded border border-line">
            <table className="tbl whitespace-nowrap">
              <thead>
                <tr>
                  <th>kpi</th>
                  {periods.map((p) => <th key={p} className="text-right font-mono">{p}</th>)}
                </tr>
              </thead>
              <tbody>
                {kpiKeys.map((k) => (
                  <tr key={k}>
                    <td className="font-mono text-[#c2cad8]">{k}</td>
                    {periods.map((p) => <td key={p} className="num text-right">{kpiCell(k, ops.kpis_by_period[p]?.[k])}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {ops.stations.length === 0 && kpiKeys.length === 0 && flat(ops.unit_economics).length === 0 && (
        <p className="text-xs text-muted">This case has no operations data.</p>
      )}
    </div>
  );
}
