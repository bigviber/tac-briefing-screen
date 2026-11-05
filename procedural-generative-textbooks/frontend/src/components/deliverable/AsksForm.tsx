import { useCase } from "../../store/caseStore";

const UNIT_LABEL: Record<string, string> = { usd: "$", cents: "¢", pct: "%", days: "days", months: "mo", ratio: "0–1", bool: "1/0", x: "×", count: "#" };

/** One numeric input per case ask. Percentages accept 31 or 0.31. */
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
          const invalid = raw.trim() !== "" && Number.isNaN(Number(raw.replace(/[$,%\s]/g, "")));
          return (
            <div key={a.key}>
              <label htmlFor={`ask-${a.key}`} className="flex items-baseline justify-between text-xs text-fg">
                <span>{a.label}</span>
                <span className="font-mono text-2xs text-muted">{a.key}</span>
              </label>
              <div className="relative mt-0.5">
                <input
                  id={`ask-${a.key}`}
                  className={`input pr-12 text-right font-mono ${invalid ? "border-bad" : ""}`}
                  inputMode="decimal"
                  value={raw}
                  placeholder="—"
                  onChange={(e) => patch({ metrics: { ...metrics, [a.key]: e.target.value } })}
                  aria-invalid={invalid}
                  aria-describedby={`ask-${a.key}-hint`}
                />
                <span className="pointer-events-none absolute right-2 top-1.5 font-mono text-2xs text-muted">{UNIT_LABEL[a.unit] ?? a.unit}</span>
              </div>
              <p id={`ask-${a.key}-hint`} className="mt-0.5 text-2xs leading-snug text-muted">{a.hint}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
