import { useEffect, useRef, useState } from "react";
import { Lock, MessageSquareWarning, SendHorizonal } from "lucide-react";
import { useCase } from "../../store/caseStore";
import { humanize } from "../../lib/format";
import { EmptyState } from "../ui/primitives";

const CHALLENGE_TONE: Record<string, string> = {
  opening: "border-bad/40 text-bad",
  alternative_explanation: "border-warn/40 text-warn",
  data_quality: "border-p-sd/40 text-p-sd",
  trade_off: "border-accent/40 text-accent",
  concession: "border-ok/40 text-ok",
};

export function Thread() {
  const thread = useCase((s) => s.thread);
  const grade = useCase((s) => s.grade);
  const busy = useCase((s) => s.adversaryBusy);
  const send = useCase((s) => s.sendAdversary);
  const [msg, setMsg] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [thread]);

  const submit = () => {
    const m = msg.trim();
    if (!m || busy) return;
    setMsg("");
    void send(m);
  };

  return (
    <>
      <div className="min-h-0 flex-1 space-y-2 overflow-auto p-3" aria-live="polite">
        {thread.length === 0 && (
          <EmptyState icon={MessageSquareWarning} title="No challenges yet">
            Submit your deliverable. The adversary will attack your diagnosis with the case's red herrings.
          </EmptyState>
        )}
        {thread.map((t, i) =>
          t.role === "user" ? (
            <div key={i} className="ml-8 whitespace-pre-wrap rounded border border-accent/30 bg-accent/10 px-2.5 py-1.5 text-sm text-fg">{t.message}</div>
          ) : t.role === "system" ? (
            <div key={i} className="px-1 text-center text-2xs italic text-muted">{t.message}</div>
          ) : (
            <div key={i} className="mr-4 rounded border border-line bg-s1 px-2.5 py-2">
              <div className="mb-1 flex flex-wrap items-center gap-1">
                <span className={`chip ${CHALLENGE_TONE[t.turn.challenge_type] ?? "text-muted"}`}>{humanize(t.turn.challenge_type)}</span>
                {t.turn.cites.map((c) => (
                  <span key={c} className="chip text-[#c2cad8]">#{c}</span>
                ))}
                {t.turn.turn_index >= 0 && (
                  <span className="ml-auto font-mono text-2xs text-[#4a5466]" title="Escalation rung of the Socratic ladder">
                    rung {t.turn.rung} · turn {t.turn.turn_index}
                  </span>
                )}
              </div>
              <p className="text-sm leading-relaxed text-fg">
                {t.turn.message || <span className="text-muted">thinking…</span>}
                {t.streaming && <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-accent align-middle" />}
              </p>
            </div>
          ),
        )}
        <div ref={end} />
      </div>
      <div className="shrink-0 border-t border-line p-2">
        <div className="flex items-end gap-1.5">
          <textarea
            value={msg}
            onChange={(e) => setMsg(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            disabled={!grade}
            rows={2}
            placeholder={grade ? "Defend your diagnosis with evidence…  (↵ send, ⇧↵ newline)" : "Locked until you submit a deliverable"}
            aria-label="Reply to adversary"
            className="input h-auto min-h-[44px] flex-1 resize-none py-1.5 disabled:cursor-not-allowed disabled:opacity-60"
          />
          <button className="btn btn-primary h-[44px]" onClick={submit} disabled={!grade || busy || !msg.trim()} aria-label="Send">
            {grade ? <SendHorizonal size={14} /> : <Lock size={14} />}
          </button>
        </div>
      </div>
    </>
  );
}
