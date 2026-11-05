import { useEffect } from "react";
import { Cpu } from "lucide-react";
import { useSettings } from "../../store/settingsStore";
import { useUi } from "../../store/uiStore";
import { fmtNum } from "../../lib/format";

/** One-line LLM usage + cost summary that opens the settings drawer. */
export function UsageStrip() {
  const usage = useSettings((s) => s.usage);
  const settings = useSettings((s) => s.settings);
  const loadUsage = useSettings((s) => s.loadUsage);
  const openSettings = useUi((s) => s.setSettings);
  useEffect(() => {
    void loadUsage();
  }, [loadUsage]);

  const model = settings
    ? `Deep model: ${settings.deep_model}\nFast model: ${settings.fast_model}${settings.configured ? "" : "\n(LLM not configured)"}`
    : "LLM settings not loaded";
  // Unconfigured: the stored defaults are not a model anyone chose, so say so (defaults stay in the tooltip).
  const deep = settings?.configured ? settings.deep_model : "";
  const cut = deep.lastIndexOf("/") + 1;
  // "anthropic/claude-sonnet-4.5": the provider prefix gives way first, so the model itself stays legible.
  const prefix = deep.slice(0, cut);
  const base = settings?.configured ? deep.slice(cut) : "LLM not configured";
  const calls = usage ? Object.values(usage.by_model).reduce((s, m) => s + m.calls, 0) : 0;
  return (
    <button
      onClick={() => openSettings(true, "usage")}
      className="panel flex min-h-9 w-full shrink-0 flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 text-left text-xs hover:border-[#2f3848]"
      aria-label="LLM usage, open settings"
    >
      {/* One line while the model name can keep ~9rem; narrower strips wrap the stats to a second line.
          The name truncates with an ellipsis (provider prefix first); full names are in the tooltip. */}
      <span className="flex min-w-0 max-w-full flex-[1_1_9rem] items-center gap-2" title={model}>
        <Cpu size={13} className="shrink-0 text-muted" aria-hidden />
        <span className={`flex min-w-0 font-mono ${settings?.configured ? "text-fg" : "text-warn"}`}>
          {prefix && <span className="min-w-[2ch] shrink-[100] truncate text-muted">{prefix}</span>}
          <span className="min-w-0 shrink-[0.01] truncate">{base}</span>
        </span>
      </span>
      <span className="ml-auto flex shrink-0 items-center gap-3 whitespace-nowrap font-mono">
        <span><span className="text-muted">calls </span>{calls}</span>
        <span><span className="text-muted">tokens </span>{usage ? fmtNum(usage.total_tokens) : "–"}</span>
        <span><span className="text-muted">est. </span><span className="text-ok">${usage ? usage.total_cost_usd_est.toFixed(2) : "–"}</span></span>
      </span>
    </button>
  );
}
