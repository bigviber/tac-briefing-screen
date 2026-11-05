import { FileText, Hash, Mail, Presentation, RefreshCw, Settings, StickyNote } from "lucide-react";
import { HTTP_LLM_NOT_CONFIGURED } from "../../api/client";
import type { StakeholderMessage } from "../../api/types";
import { useUi } from "../../store/uiStore";
import { useCase } from "../../store/caseStore";
import { fmtCents } from "../../lib/format";
import { Markdown } from "../ui/Markdown";
import { EmptyState, ErrorLine, RedactedBadge, SkeletonBlock } from "../ui/primitives";

const CHANNEL: Record<string, { icon: typeof Mail; label: string; tone: string }> = {
  email: { icon: Mail, label: "Email", tone: "border-l-accent" },
  slack: { icon: Hash, label: "Slack", tone: "border-l-p-sd" },
  memo: { icon: StickyNote, label: "Memo", tone: "border-l-warn" },
  board_deck: { icon: Presentation, label: "Board deck", tone: "border-l-ok" },
};

function initials(name: string): string {
  return name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
}

function MessageCard({ m }: { m: StakeholderMessage }) {
  const ch = CHANNEL[m.channel] ?? { ...CHANNEL.memo, label: m.channel || CHANNEL.memo.label };
  const Icon = ch.icon;
  return (
    <article className={`rounded border border-line border-l-2 ${ch.tone} bg-s1`}>
      <header className="flex items-center gap-2 border-b border-[#1a2029] px-2.5 py-1.5">
        <span className="flex h-5 w-5 items-center justify-center rounded-sm bg-s3 text-2xs font-semibold text-fg">{initials(m.from_name)}</span>
        <span className="text-xs font-medium text-fg">{m.from_name}</span>
        <span className="text-xs text-muted">{m.role}</span>
        <span className="ml-auto flex items-center gap-1 text-2xs text-muted">
          <Icon size={11} aria-hidden /> {ch.label}
        </span>
      </header>
      {m.subject && (
        <div className="border-b border-[#1a2029] px-2.5 py-1 text-xs">
          <span className="mr-1.5 text-2xs uppercase tracking-wider text-muted">Subject</span>
          <span className="font-medium text-fg">{m.subject}</span>
        </div>
      )}
      <div className={`px-2.5 py-2 text-sm ${m.channel === "slack" ? "font-normal" : ""}`}>
        <Markdown>{m.body}</Markdown>
      </div>
    </article>
  );
}

function TimelineList({ items }: { items: { period: string; headline: string; detail: string }[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className="field-label">Timeline</div>
      <ol className="space-y-0.5 border-l border-line pl-3">
        {items.map((t, i) => (
          <li key={`${i}-${t.period}`} className="flex gap-2 text-xs leading-snug">
            <span className="num w-[52px] shrink-0 text-muted">{t.period}</span>
            <span className="min-w-0">
              <span className="text-fg">{t.headline}</span>
              {t.detail && <span className="text-muted"> — {t.detail}</span>}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function DossierTab() {
  const c = useCase((s) => s.caseData);
  const dossier = useCase((s) => s.dossier);
  const streamText = useCase((s) => s.dossierStream);
  const loading = useCase((s) => s.dossierLoading);
  const error = useCase((s) => s.dossierError);
  const errorStatus = useCase((s) => s.dossierErrorStatus);
  const reload = useCase((s) => s.loadDossier);
  const openSettings = useUi((s) => s.setSettings);
  if (!c) return null;
  const needsLlm = errorStatus === HTTP_LLM_NOT_CONFIGURED;
  const timeline = dossier?.timeline.length
    ? dossier.timeline.map((t) => ({ period: String(t.period ?? ""), headline: String(t.headline ?? ""), detail: String(t.detail ?? "") }))
    : c.dossier_seed.timeline;

  const facts: [string, string][] = [
    ["Revenue TTM", fmtCents(c.company.revenue_ttm_cents, { compact: true })],
    ["Headcount", String(c.company.headcount)],
    ["Stage", c.company.stage],
    ["HQ", c.company.hq],
  ];

  return (
    <div className="space-y-3 p-3">
      <div className="grid grid-cols-4 gap-px overflow-hidden rounded border border-line bg-line">
        {facts.map(([k, v]) => (
          <div key={k} className="bg-s1 px-2 py-1.5">
            <div className="text-2xs uppercase tracking-wider text-muted">{k}</div>
            <div className="num truncate text-sm text-fg">{v}</div>
          </div>
        ))}
      </div>

      {needsLlm && !loading && (
        <EmptyState
          icon={Settings}
          title="Dossier needs an LLM"
          action={
            <div className="flex gap-1.5">
              <button className="btn btn-sm btn-primary" onClick={() => openSettings(true, "llm")}>Configure LLM</button>
              <button className="btn btn-sm" onClick={() => void reload(false, true)}>Retry</button>
            </div>
          }
        >
          The case world, data room and sandboxes work without one. The narrative dossier, textbook, grading and adversary are written by the configured model.
        </EmptyState>
      )}
      {error && !needsLlm && <ErrorLine message={`Dossier: ${error}`} onRetry={() => void reload(false, true)} />}
      {loading && !streamText && <SkeletonBlock lines={8} />}
      {loading && streamText && (
        <div className="rounded border border-line bg-s1 p-3">
          <div className="mb-1 flex items-center gap-1.5 text-2xs uppercase tracking-wider text-accent">
            <RefreshCw size={10} className="animate-spin" aria-hidden /> Writing dossier…
          </div>
          <Markdown>{streamText}</Markdown>
        </div>
      )}
      {!loading && !dossier && !error && (
        <EmptyState icon={FileText} title="No dossier yet" action={<button className="btn btn-sm" onClick={() => void reload(false, true)}>Generate dossier</button>} />
      )}

      {!dossier && !loading && (
        <>
          <div>
            <div className="field-label">Company</div>
            <p className="text-sm text-[#c2cad8]">
              <span className="font-medium text-fg">{c.company.name}</span> · {c.company.business_model}. {c.company.description}
            </p>
          </div>
          {c.dossier_seed.reveal.length > 0 && (
            <div>
              <div className="field-label">Known facts</div>
              <ul className="space-y-0.5 text-xs text-[#c2cad8]">
                {c.dossier_seed.reveal.map((r) => (
                  <li key={r}>▸ {r}</li>
                ))}
              </ul>
            </div>
          )}
          {c.dossier_seed.stakeholders.length > 0 && (
            <div>
              <div className="field-label">Stakeholders</div>
              <ul className="space-y-1 text-xs">
                {c.dossier_seed.stakeholders.map((sh) => (
                  <li key={sh.name} className="flex gap-2">
                    <span className="w-[130px] shrink-0 truncate text-fg" title={sh.name}>{sh.name}</span>
                    <span className="w-[84px] shrink-0 truncate text-muted" title={sh.role}>{sh.role}</span>
                    <span className="min-w-0 text-muted">{sh.agenda}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <TimelineList items={timeline} />
        </>
      )}

      {dossier && !loading && (
        <>
          <div>
            <div className="flex items-start gap-2">
              <h2 className="flex-1 text-[15px] font-semibold leading-snug text-white">{dossier.title}</h2>
              {dossier.leak_redacted && <RedactedBadge />}
              <button className="btn btn-ghost btn-sm" onClick={() => void reload(true, true)} title="Regenerate dossier (LLM)" aria-label="Regenerate dossier">
                <RefreshCw size={12} />
              </button>
            </div>
            <p className="mt-1.5 text-sm text-[#c2cad8]">{dossier.situation}</p>
          </div>
          <div className="rounded border border-accent/40 bg-accent/5 px-3 py-2">
            <div className="text-2xs font-medium uppercase tracking-wider text-accent">The ask</div>
            <p className="mt-0.5 text-sm text-fg">{dossier.the_ask}</p>
            <ul className="mt-1.5 space-y-0.5 text-xs text-muted">
              {dossier.deliverables.map((d) => (
                <li key={d}>▸ {d}</li>
              ))}
            </ul>
          </div>
          <Markdown>{dossier.background_markdown}</Markdown>
          <div>
            <div className="field-label">Inbox · {dossier.stakeholder_messages.length}</div>
            <div className="space-y-2">
              {dossier.stakeholder_messages.map((m, i) => (
                <MessageCard key={i} m={m} />
              ))}
            </div>
          </div>
          <TimelineList items={timeline} />
          <div>
            <div className="field-label">Data room</div>
            <div className="flex flex-wrap gap-1">
              {dossier.data_room_index.map((d) => (
                <span key={d} className="chip">{d}</span>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
