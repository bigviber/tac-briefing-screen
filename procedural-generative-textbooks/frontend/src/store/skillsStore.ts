import { create } from "zustand";
import { api, errorMessage } from "../api/client";
import { PILLARS, type Pillar, type SkillGraph } from "../api/types";
import { toast } from "./uiStore";

interface SkillsState {
  graph: SkillGraph | null;
  loading: boolean;
  error: string | null;
  /** Node ids selected as generation targets. */
  targets: string[];
  load: () => Promise<void>;
  toggleTarget: (id: string) => void;
  clearTargets: () => void;
  reset: () => Promise<void>;
}

export const useSkills = create<SkillsState>((set, get) => ({
  graph: null,
  loading: false,
  error: null,
  targets: [],
  load: async () => {
    set({ loading: true, error: null });
    try {
      set({ graph: await api.skillGraph() });
    } catch (e) {
      set({ error: errorMessage(e) });
    } finally {
      set({ loading: false });
    }
  },
  toggleTarget: (id) => {
    const t = get().targets;
    set({ targets: t.includes(id) ? t.filter((x) => x !== id) : [...t, id] });
  },
  clearTargets: () => set({ targets: [] }),
  reset: async () => {
    try {
      await api.resetSkills();
      toast("Mastery reset", "success");
      await get().load();
    } catch (e) {
      toast(`Reset failed: ${errorMessage(e)}`, "error");
    }
  },
}));

/** Mean p_known per pillar. */
export function pillarMastery(graph: SkillGraph | null): Record<Pillar, number> {
  const out = Object.fromEntries(PILLARS.map((p) => [p, 0])) as Record<Pillar, number>;
  if (!graph) return out;
  for (const p of PILLARS) {
    const nodes = graph.nodes.filter((n) => n.pillar === p);
    out[p] = nodes.length ? nodes.reduce((s, n) => s + n.mastery, 0) / nodes.length : 0;
  }
  return out;
}
