import { useEffect, useMemo, useRef, useState, type UIEvent } from "react";
import type { Cell, SqlResult } from "../../api/types";

const ROW_H = 22;
const OVERSCAN = 12;

function inferType(rows: Cell[][], i: number): string {
  for (const r of rows) {
    const v = r[i];
    if (v === null || v === undefined) continue;
    if (typeof v === "number") return Number.isInteger(v) ? "BIGINT" : "DOUBLE";
    if (typeof v === "boolean") return "BOOLEAN";
    return /^\d{4}-\d{2}-\d{2}/.test(v) ? "DATE" : "VARCHAR";
  }
  return "NULL";
}

function fmtCell(v: Cell): string {
  if (v === null) return "NULL";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-US") : v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return String(v);
}

/** Windowed results table: renders only visible rows, so 500+ rows stay smooth. */
export function ResultsGrid({ result }: { result: SqlResult }) {
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(400);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const types = useMemo(
    () => result.columns.map((_, i) => result.column_types?.[i] ?? inferType(result.rows, i)),
    [result],
  );
  const numeric = types.map((t) => /INT|DOUBLE|DECIMAL|FLOAT|NUMERIC|REAL/i.test(t));

  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    setScrollTop(e.currentTarget.scrollTop);
    setViewH(e.currentTarget.clientHeight);
  };

  const total = result.rows.length;
  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const end = Math.min(total, Math.ceil((scrollTop + viewH) / ROW_H) + OVERSCAN);
  const visible = result.rows.slice(start, end);

  if (result.columns.length === 0) return <div className="p-3 text-xs text-muted">Statement returned no columns.</div>;

  return (
    <div ref={box} className="h-full overflow-auto" onScroll={onScroll} tabIndex={0} aria-label="Query results">
      <table className="tbl whitespace-nowrap font-mono">
        <thead>
          <tr>
            <th className="w-10 text-right text-[#4a5466]">#</th>
            {result.columns.map((c, i) => (
              <th key={`${c}-${i}`} className={numeric[i] ? "text-right" : ""}>
                <div className="text-fg">{c}</div>
                <div className="text-2xs font-normal text-[#5b6577]">{types[i].toLowerCase()}</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {start > 0 && <tr style={{ height: start * ROW_H }} aria-hidden />}
          {visible.map((row, ri) => (
            <tr key={start + ri} className="h-[22px] hover:bg-s2">
              <td className="py-0 text-right text-[#4a5466]">{start + ri + 1}</td>
              {row.map((v, ci) => (
                <td key={ci} className={`max-w-[320px] truncate py-0 ${numeric[ci] ? "text-right" : ""} ${v === null ? "italic text-[#5b6577]" : "text-fg"}`}>
                  {fmtCell(v)}
                </td>
              ))}
            </tr>
          ))}
          {end < total && <tr style={{ height: (total - end) * ROW_H }} aria-hidden />}
        </tbody>
      </table>
    </div>
  );
}
