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

  const calls = usage ? Object.values(usage.by_model).reduce((s, m) => s + m.calls, 0) : 0;
  return (
    <button
      onClick={() => openSettings(true, "usage")}
      className="panel flex h-9 w-full shrink-0 items-center gap-3 px-3 text-xs hover:border-[#2f3848]"
      aria-label="LLM usage, open settings"
    >
      <Cpu size={13} className="text-muted" aria-hidden />
      <span className="min-w-0 truncate font-mono text-fg">{settings ? settings.deep_model : "not configured"}</span>
      <span className="ml-auto flex shrink-0 items-center gap-3 whitespace-nowrap font-mono">
        <span><span className="text-muted">calls </span>{calls}</span>
        <span><span className="text-muted">tokens </span>{usage ? fmtNum(usage.total_tokens) : "–"}</span>
        <span><span className="text-muted">est. </span><span className="text-ok">${usage ? usage.total_cost_usd_est.toFixed(2) : "–"}</span></span>
      </span>
    </button>
  );
}
