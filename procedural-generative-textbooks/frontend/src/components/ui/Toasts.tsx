import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { useUi } from "../../store/uiStore";

const ICON = { info: Info, success: CheckCircle2, error: AlertTriangle };
const TONE = { info: "text-accent", success: "text-ok", error: "text-bad" };

export function Toasts() {
  const toasts = useUi((s) => s.toasts);
  const dismiss = useUi((s) => s.dismissToast);
  return (
    <div className="pointer-events-none fixed bottom-3 right-3 z-[60] flex w-[340px] flex-col gap-2" aria-live="polite">
      {toasts.map((t) => {
        const Icon = ICON[t.kind];
        return (
          <div key={t.id} className="pointer-events-auto flex items-start gap-2 rounded border border-line bg-s2 px-3 py-2 text-xs shadow-lg shadow-black/40">
            <Icon size={14} className={`mt-px shrink-0 ${TONE[t.kind]}`} aria-hidden />
            <span className="min-w-0 flex-1 break-words">{t.message}</span>
            <button className="text-muted hover:text-fg" onClick={() => dismiss(t.id)} aria-label="Dismiss notification">
              <X size={12} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
