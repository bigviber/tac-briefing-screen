import { useCase } from "../../store/caseStore";
import { askInDollars, parseAsk } from "../../lib/asks";
import { fmtUsdExact, isCents } from "../../lib/format";

const UNIT_LABEL: Record<string, string> = { usd: "USD", cents: "USD", pct: "%", days: "days", months: "mo", ratio: "0–1", bool: "1/0", x: "×", count: "#" };

/**
 * One numeric input per case ask. Percentages accept 31 or 0.31. Dollar asks (including asks
 * stored in cents, which the backend grades in cents) are typed in dollars: "1.2M", "$850,000",
 * "850k"; the parsed amount is echoed under the field and cents asks are converted on submit.
 */
export function AsksForm() {
  const asks = useCase((s) => s.caseData?.asks ?? []);
  const metrics = useCase((s) => s.draft?.metrics ?? {});
  const patch = useCase((s) => s.patchDraft);

  return (
    <div className="border-b border-line p-3">
      <div className="field-label">Key metrics · asks</div>
      <div className="space-y-2">
        {asks.map((a) => {
          const raw = metrics[a.key] ?? "";
          const value = parseAsk(a, raw);
          const invalid = raw.trim() !== "" && value === null;
          const dollars = askInDollars(a);
          const cents = isCents(a.key, a.unit);
          const echo = dollars && value !== null ? fmtUsdExact(cents ? value / 100 : value) : null;
          return (
            <div key={a.key}>
              <label htmlFor={`ask-${a.key}`} className="flex items-baseline justify-between gap-2 text-xs text-fg">
                <span className="shrink-0">{a.label}</span>
                <span className="min-w-0 truncate font-mono text-2xs text-muted" title={a.key}>{a.key}</span>
              </label>
              <div className="relative mt-0.5">
                <input
                  id={`ask-${a.key}`}
                  className={`input pr-12 text-right font-mono ${invalid ? "border-bad" : ""}`}
                  inputMode={dollars ? "text" : "decimal"}
                  value={raw}
                  placeholder={dollars ? "e.g. 1.2M" : "—"}
                  onChange={(e) => patch({ metrics: { ...metrics, [a.key]: e.target.value } })}
                  aria-invalid={invalid}
                  aria-describedby={`ask-${a.key}-hint`}
                />
                <span className="pointer-events-none absolute right-2 top-1.5 font-mono text-2xs text-muted">{cents ? "USD" : UNIT_LABEL[a.unit] ?? a.unit}</span>
              </div>
              <p id={`ask-${a.key}-hint`} className="mt-0.5 text-2xs leading-snug text-muted">
                {invalid && dollars && <span className="text-bad">Enter a dollar amount such as 1.2M, $850,000 or 850k. </span>}
                {echo && <span className="num text-fg">= {echo}{cents ? " (sent in cents)" : ""}. </span>}
                {a.hint}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
