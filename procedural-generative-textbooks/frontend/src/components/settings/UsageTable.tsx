import { useEffect } from "react";
import { BarChart3, RefreshCw } from "lucide-react";
import { useSettings } from "../../store/settingsStore";
import { EmptyState, SkeletonBlock } from "../ui/primitives";

const n = new Intl.NumberFormat("en-US");

export function UsageTable() {
  const usage = useSettings((s) => s.usage);
  const load = useSettings((s) => s.loadUsage);
  useEffect(() => {
    void load();
  }, [load]);

  if (!usage) return <SkeletonBlock lines={6} />;
  const rows = Object.entries(usage.by_model).sort((a, b) => b[1].cost_usd_est - a[1].cost_usd_est);
  if (rows.length === 0) return <EmptyState icon={BarChart3} title="No LLM calls yet">Usage appears after the first dossier or grade.</EmptyState>;
  const maxCost = Math.max(...rows.map(([, m]) => m.cost_usd_est), 0.0001);

  return (
    <div className="p-4">
      <div className="mb-3 grid grid-cols-3 gap-px overflow-hidden rounded border border-line bg-line">
        <div className="bg-s2 px-3 py-2">
          <div className="text-2xs uppercase tracking-wider text-muted">Total tokens</div>
          <div className="num text-base text-fg">{n.format(usage.total_tokens)}</div>
        </div>
        <div className="bg-s2 px-3 py-2">
          <div className="text-2xs uppercase tracking-wider text-muted">Calls</div>
          <div className="num text-base text-fg">{rows.reduce((s, [, m]) => s + m.calls, 0)}</div>
        </div>
        <div className="bg-s2 px-3 py-2">
          <div className="text-2xs uppercase tracking-wider text-muted">Est. cost</div>
          <div className="num text-base text-ok">${usage.total_cost_usd_est.toFixed(2)}</div>
        </div>
      </div>
      <table className="tbl">
        <thead>
          <tr>
            <th>model</th>
            <th className="text-right">calls</th>
            <th className="text-right">prompt</th>
            <th className="text-right">completion</th>
            <th className="w-[140px] text-right">est. cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([model, m]) => (
            <tr key={model}>
              <td className="font-mono text-fg">{model}</td>
              <td className="num text-right">{m.calls}</td>
              <td className="num text-right">{n.format(m.prompt_tokens)}</td>
              <td className="num text-right">{n.format(m.completion_tokens)}</td>
              <td className="text-right">
                <div className="flex items-center justify-end gap-2">
                  <div className="h-1 w-14 rounded-sm bg-s3">
                    <div className="h-full rounded-sm bg-ok" style={{ width: `${(m.cost_usd_est / maxCost) * 100}%` }} />
                  </div>
                  <span className="num text-fg">${m.cost_usd_est.toFixed(2)}</span>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="btn btn-ghost btn-sm mt-3" onClick={() => void load()}>
        <RefreshCw size={12} aria-hidden /> Refresh
      </button>
      <p className="mt-2 text-2xs text-muted">Costs are estimates from the backend's price table; your provider's invoice is authoritative.</p>
    </div>
  );
}
