import { useEffect, useMemo, useState } from "react";
import { ArrowRight, Dice5, Sparkles, X } from "lucide-react";
import { DIFFICULTIES, PILLARS, type Difficulty } from "../../api/types";
import { useCatalog } from "../../store/catalogStore";
import { useSkills } from "../../store/skillsStore";
import { useCase } from "../../store/caseStore";
import { navigate } from "../../lib/route";
import { PILLAR_COLOR, PILLAR_LABEL } from "../../lib/pillars";
import { Kbd, Skeleton } from "../ui/primitives";
import { humanize } from "../../lib/format";

const AUTO = "__auto__";

export function GeneratePanel() {
  const archetypes = useCatalog((s) => s.archetypes);
  const loadArchetypes = useCatalog((s) => s.loadArchetypes);
  const graph = useSkills((s) => s.graph);
  const targets = useSkills((s) => s.targets);
  const toggleTarget = useSkills((s) => s.toggleTarget);
  const clearTargets = useSkills((s) => s.clearTargets);
  const generating = useCase((s) => s.generating);
  const generate = useCase((s) => s.generate);
  const last = useCase((s) => s.lastGenerated);

  const [archetype, setArchetype] = useState<string>(AUTO);
  const [difficulty, setDifficulty] = useState<Difficulty>("practitioner");
  const [seed, setSeed] = useState("");

  useEffect(() => {
    void loadArchetypes();
  }, [loadArchetypes]);

  const grouped = useMemo(
    () => PILLARS.map((p) => ({ pillar: p, items: (archetypes ?? []).filter((a) => a.pillars[0] === p) })).filter((g) => g.items.length),
    [archetypes],
  );
  const chosen = archetypes?.find((a) => a.key === archetype);
  const titleOf = (id: string) => graph?.nodes.find((n) => n.id === id)?.title ?? id;

  const run = async () => {
    const c = await generate({
      archetype: archetype === AUTO ? undefined : archetype,
      difficulty,
      seed: seed.trim() ? Number(seed) : undefined,
      target_skills: targets.length ? targets : undefined,
    });
    if (c) navigate({ name: "case", id: c.case_id });
  };

  return (
    <section className="panel flex flex-col" aria-labelledby="gen-title">
      <div className="panel-head">
        <Sparkles size={13} className="text-accent" aria-hidden />
        <span id="gen-title">Generate procedural case</span>
      </div>
      <div className="space-y-3 p-3">
        <div>
          <label className="field-label" htmlFor="arch">Archetype</label>
          {archetypes ? (
            <select id="arch" className="input" value={archetype} onChange={(e) => setArchetype(e.target.value)}>
              <option value={AUTO}>Let Axiom pick (lowest mastery, prereqs met)</option>
              {grouped.map((g) => (
                <optgroup key={g.pillar} label={PILLAR_LABEL[g.pillar]}>
                  {g.items.map((a) => (
                    <option key={a.key} value={a.key}>{a.title}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          ) : (
            <Skeleton className="h-7" />
          )}
          <p className="mt-1.5 min-h-[32px] text-xs text-muted">
            {chosen ? chosen.summary : "Axiom scores archetypes by Σ(1 − p_known) over their nodes and picks the best fit for your targets."}
          </p>
          {chosen && (
            <div className="mt-1 flex flex-wrap gap-1">
              {chosen.pillars.map((p) => (
                <span key={p} className="chip" style={{ color: PILLAR_COLOR[p] }}>{PILLAR_LABEL[p]}</span>
              ))}
            </div>
          )}
        </div>

        <div className="grid grid-cols-[1fr_120px] gap-2">
          <div>
            <span className="field-label">Difficulty</span>
            <div className="flex rounded border border-line" role="radiogroup" aria-label="Difficulty">
              {DIFFICULTIES.map((d) => {
                const disabled = chosen ? !chosen.difficulty_range.includes(d) : false;
                return (
                  <button
                    key={d}
                    role="radio"
                    aria-checked={difficulty === d}
                    disabled={disabled}
                    onClick={() => setDifficulty(d)}
                    className={`h-7 flex-1 text-xs capitalize first:rounded-l last:rounded-r disabled:opacity-30 ${
                      difficulty === d ? "bg-s3 text-fg" : "text-muted hover:text-fg"
                    }`}
                  >
                    {d}
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <label className="field-label" htmlFor="seed">Seed</label>
            <div className="relative">
              <input id="seed" className="input pr-7 font-mono" inputMode="numeric" placeholder="random" value={seed} onChange={(e) => setSeed(e.target.value.replace(/\D/g, ""))} />
              <button className="absolute right-1.5 top-1.5 text-muted hover:text-fg" onClick={() => setSeed(String(Math.floor(Math.random() * 1e5)))} aria-label="Random seed">
                <Dice5 size={14} />
              </button>
            </div>
          </div>
        </div>

        <div>
          <div className="field-label flex items-center justify-between">
            <span>Target skills</span>
            {targets.length > 0 && (
              <button className="normal-case tracking-normal text-muted hover:text-fg" onClick={clearTargets}>clear</button>
            )}
          </div>
          <div className="flex min-h-[28px] flex-wrap gap-1 rounded border border-dashed border-line p-1.5">
            {targets.length === 0 && <span className="text-xs text-muted">Click nodes in the graph to target them. Default: Axiom picks.</span>}
            {targets.map((t) => (
              <button key={t} className="chip hover:text-fg" onClick={() => toggleTarget(t)} aria-label={`Remove ${titleOf(t)}`}>
                {titleOf(t)} <X size={10} />
              </button>
            ))}
          </div>
        </div>

        <button className="btn btn-primary w-full justify-center" onClick={run} disabled={generating}>
          <Sparkles size={13} aria-hidden />
          {generating ? "Generating world…" : "Generate case"}
          <span className="ml-auto opacity-70"><Kbd>⌘K</Kbd></span>
        </button>

        {last && (
          <button
            className="block w-full rounded border border-line bg-bg px-2.5 py-2 text-left hover:border-[#2f3848]"
            onClick={() => navigate({ name: "case", id: last.case_id })}
            aria-label={`Open last generated case, ${last.company}`}
          >
            <div className="field-label mb-0.5">Last generated</div>
            <div className="flex items-center gap-2">
              <span className="min-w-0 truncate text-sm text-fg">{last.company}</span>
              <ArrowRight size={12} className="ml-auto shrink-0 text-muted" aria-hidden />
            </div>
            {last.rationale && <p className="mt-0.5 text-xs leading-snug text-[#c2cad8]">{last.rationale}</p>}
            <div className="mt-0.5 truncate font-mono text-2xs text-muted">
              {archetypes?.find((a) => a.key === last.archetype)?.title ?? humanize(last.archetype)} · {last.difficulty} · seed {last.seed}
            </div>
          </button>
        )}
      </div>
    </section>
  );
}
