import { useEffect, useMemo, useRef, useState } from "react";
import { CornerDownLeft, Search } from "lucide-react";
import { useUi } from "../store/uiStore";
import { useCatalog } from "../store/catalogStore";
import { useCase } from "../store/caseStore";
import { useCommands, type PaletteCommand } from "./commands";

function score(cmd: PaletteCommand, q: string): number {
  if (!q) return 1;
  const hay = `${cmd.group} ${cmd.label} ${cmd.hint ?? ""}`.toLowerCase();
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.every((w) => hay.includes(w))) return 0;
  return cmd.label.toLowerCase().startsWith(words[0]) ? 2 : 1;
}

export function CommandPalette() {
  const open = useUi((s) => s.paletteOpen);
  const setOpen = useUi((s) => s.setPalette);
  const commands = useCommands();
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const loadArchetypes = useCatalog((s) => s.loadArchetypes);
  const loadCases = useCatalog((s) => s.loadCases);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setIdx(0);
    void loadArchetypes();
    void loadCases();
    window.setTimeout(() => input.current?.focus(), 0);
  }, [open, loadArchetypes, loadCases]);

  const results = useMemo(
    () =>
      commands
        .map((c) => ({ c, s: score(c, q) }))
        .filter((x) => x.s > 0)
        .sort((a, b) => b.s - a.s)
        .map((x) => x.c)
        .slice(0, 40),
    [commands, q],
  );

  if (!open) return null;

  const run = (c: PaletteCommand | undefined) => {
    if (!c) return;
    setOpen(false);
    c.run();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[12vh]" onMouseDown={() => setOpen(false)}>
      <div
        role="dialog"
        aria-label="Command palette"
        className="w-[560px] overflow-hidden rounded border border-line bg-s1 shadow-2xl shadow-black/60"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex h-10 items-center gap-2 border-b border-line px-3">
          <Search size={14} className="text-muted" aria-hidden />
          <input
            ref={input}
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setIdx(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") setIdx((i) => Math.min(results.length - 1, i + 1));
              else if (e.key === "ArrowUp") setIdx((i) => Math.max(0, i - 1));
              else if (e.key === "Enter") run(results[idx]);
              else if (e.key === "Escape") setOpen(false);
              else return;
              e.preventDefault();
            }}
            placeholder="Type a command: generate, settings, sql, dossier…"
            className="h-full flex-1 bg-transparent text-sm text-fg placeholder:text-[#5b6577] focus:outline-none"
            aria-label="Command"
          />
        </div>
        <ul className="max-h-[50vh] overflow-auto py-1" role="listbox">
          {results.length === 0 && <li className="px-3 py-4 text-center text-xs text-muted">No matching commands</li>}
          {results.map((c, i) => {
            const Icon = c.icon;
            return (
              <li
                key={c.id}
                role="option"
                aria-selected={i === idx}
                onMouseEnter={() => setIdx(i)}
                onClick={() => run(c)}
                className={`mx-1 flex h-8 cursor-pointer items-center gap-2.5 rounded px-2 text-sm ${i === idx ? "bg-s3 text-fg" : "text-[#c2cad8]"}`}
              >
                <Icon size={14} className="shrink-0 text-muted" aria-hidden />
                <span className="w-[76px] shrink-0 text-2xs uppercase tracking-wider text-muted">{c.group}</span>
                <span className="truncate">{c.label}</span>
                {c.hint && <span className="ml-auto truncate pl-2 font-mono text-2xs text-muted">{c.hint}</span>}
                {i === idx && <CornerDownLeft size={12} className="shrink-0 text-muted" aria-hidden />}
              </li>
            );
          })}
        </ul>
        <div className="flex h-7 items-center gap-3 border-t border-line px-3 text-2xs text-muted">
          <span>↑↓ navigate</span>
          <span>↵ run</span>
          <span>esc close</span>
          {useCase.getState().caseData && <span className="ml-auto">⌘↵ runs SQL / Python in the sandbox</span>}
        </div>
      </div>
    </div>
  );
}
