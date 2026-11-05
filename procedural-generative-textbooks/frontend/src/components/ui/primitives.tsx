import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-sm border border-line bg-s2 px-1 font-mono text-2xs leading-4 text-muted">{children}</kbd>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton h-3 ${className}`} />;
}

export function SkeletonBlock({ lines = 6 }: { lines?: number }) {
  return (
    <div className="space-y-2 p-3" aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className={i % 3 === 2 ? "w-2/3" : "w-full"} />
      ))}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
}: {
  icon: LucideIcon;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex h-full min-h-[120px] flex-col items-center justify-center gap-2 p-6 text-center">
      <Icon size={18} className="text-[#4a5466]" aria-hidden />
      <div className="text-sm font-medium text-fg">{title}</div>
      {children && <div className="max-w-[320px] text-xs text-muted">{children}</div>}
      {action}
    </div>
  );
}

export function ErrorLine({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="m-2 flex items-start gap-2 rounded border border-bad/40 bg-bad/10 px-2.5 py-2 text-xs text-[#f3a0aa]" role="alert">
      <span className="min-w-0 flex-1 whitespace-pre-wrap break-words font-mono">{message}</span>
      {onRetry && (
        <button className="btn btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export interface TabDef<T extends string> {
  id: T;
  label: string;
  icon?: LucideIcon;
  badge?: ReactNode;
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  right,
}: {
  tabs: TabDef<T>[];
  value: T;
  onChange: (t: T) => void;
  right?: ReactNode;
}) {
  return (
    <div className="flex h-8 shrink-0 items-stretch border-b border-line bg-s1 pl-1 pr-2" role="tablist">
      {tabs.map((t) => {
        const active = t.id === value;
        const Icon = t.icon;
        return (
          <button
            key={t.id}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(t.id)}
            className={`relative flex items-center gap-1.5 px-2.5 text-xs transition-colors ${
              active ? "text-fg" : "text-muted hover:text-fg"
            }`}
          >
            {Icon && <Icon size={13} aria-hidden />}
            {t.label}
            {t.badge}
            {active && <span className="absolute inset-x-1.5 bottom-0 h-px bg-accent" />}
          </button>
        );
      })}
      <div className="ml-auto flex items-center gap-1.5">{right}</div>
    </div>
  );
}

export function Meter({ value, color, className = "" }: { value: number; color: string; className?: string }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className={`h-1 overflow-hidden rounded-sm bg-s3 ${className}`}>
      <div className="h-full rounded-sm" style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const tone: Record<string, string> = {
    generated: "border-line text-muted",
    briefed: "border-accent/50 text-accent",
    submitted: "border-warn/50 text-warn",
    graded: "border-ok/50 text-ok",
  };
  return (
    <span className={`inline-flex h-5 items-center rounded-sm border px-1.5 font-mono text-2xs uppercase ${tone[status] ?? tone.generated}`}>
      {status}
    </span>
  );
}

/** Small badge shown when the backend's leak filter removed concealed facts from LLM output. */
export function RedactedBadge() {
  return (
    <span
      className="inline-flex h-5 shrink-0 items-center rounded-sm border border-warn/40 px-1.5 font-mono text-2xs uppercase text-warn"
      title="The leak filter removed sentences that would have revealed the hidden root cause."
    >
      redacted
    </span>
  );
}
