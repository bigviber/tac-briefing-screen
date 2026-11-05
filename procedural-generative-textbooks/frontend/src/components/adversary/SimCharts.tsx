import { useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { SimQuarter } from "../../api/types";
import { fmtCents, fmtUnit, humanize, isCents, unitFromKey } from "../../lib/format";

/** One quarter's outcome: KPI table with deltas plus the narrative. */
export function SimCard({ q, prev }: { q: SimQuarter; prev?: SimQuarter }) {
  return (
    <article className="rounded border border-line bg-s1">
      <header className="flex items-center gap-2 border-b border-[#1a2029] px-2.5 py-1">
        <span className="num text-sm font-medium text-fg">{q.period}</span>
        <span className="truncate font-mono text-2xs text-muted">
          {Object.entries(q.decisions).map(([k, v]) => `${k}=${isCents(k) ? fmtCents(v, { compact: true }) : Number.isInteger(v) ? v : v.toFixed(2)}`).join(" ")}
        </span>
      </header>
      <table className="w-full text-xs">
        <tbody>
          {Object.entries(q.kpis).map(([k, v]) => {
            const unit = unitFromKey(k);
            const p = prev?.kpis[k];
            const d = p === undefined ? null : v - p;
            return (
              <tr key={k} className="border-b border-[#1a2029] last:border-0">
                <td className="px-2.5 py-0.5 text-muted">{humanize(k)}</td>
                <td className="num px-2 py-0.5 text-right text-fg">{fmtUnit(v, unit)}</td>
                <td className={`num w-[70px] px-2.5 py-0.5 text-right ${d === null || d === 0 ? "text-[#4a5466]" : d > 0 ? "text-ok" : "text-bad"}`}>
                  {d === null ? "" : `${d > 0 ? "+" : "−"}${fmtUnit(Math.abs(d), unit)}`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {q.narrative && <p className="border-t border-[#1a2029] px-2.5 py-1.5 text-xs leading-relaxed text-[#c2cad8]">{q.narrative}</p>}
    </article>
  );
}

/** KPI trend across simulated quarters; one KPI at a time keeps scales honest. */
export function KpiTrend({ history }: { history: SimQuarter[] }) {
  const keys = useMemo(() => Object.keys(history[0]?.kpis ?? {}), [history]);
  const [key, setKey] = useState<string>(keys.includes("dso") ? "dso" : keys[0] ?? "");
  const unit = unitFromKey(key);
  const data = history.map((q) => ({ period: q.period, v: q.kpis[key] }));

  return (
    <div className="rounded border border-line bg-s1 p-2">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-2xs uppercase tracking-wider text-muted">Trend</span>
        <select className="input h-6 w-auto text-xs" value={key} onChange={(e) => setKey(e.target.value)} aria-label="KPI">
          {keys.map((k) => (
            <option key={k} value={k}>{humanize(k)}</option>
          ))}
        </select>
      </div>
      <div className="h-[110px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#1a2029" vertical={false} />
            <XAxis dataKey="period" tick={{ fill: "#8b95a7", fontSize: 10 }} axisLine={{ stroke: "#232a36" }} tickLine={false} />
            <YAxis width={44} tick={{ fill: "#8b95a7", fontSize: 10 }} axisLine={false} tickLine={false} tickFormatter={(v: number) => fmtUnit(v, unit)} domain={["auto", "auto"]} />
            <Tooltip
              contentStyle={{ background: "#161b24", border: "1px solid #232a36", borderRadius: 4, fontSize: 11 }}
              labelStyle={{ color: "#8b95a7" }}
              formatter={(v: number) => [fmtUnit(v, unit), humanize(key)]}
            />
            <Line type="monotone" dataKey="v" stroke="#5b8def" strokeWidth={1.5} dot={{ r: 2.5, fill: "#5b8def" }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
