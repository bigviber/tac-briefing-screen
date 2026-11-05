import { create } from "zustand";
import { api, errorMessage } from "../api/client";
import type { ArchetypeInfo, CaseSummary, Health } from "../api/types";

/** Read-mostly lists used by the dashboard: archetypes, recent cases, backend health. */
interface CatalogState {
  archetypes: ArchetypeInfo[] | null;
  cases: CaseSummary[] | null;
  health: Health | null;
  healthError: string | null;
  error: string | null;
  loadArchetypes: () => Promise<void>;
  loadCases: () => Promise<void>;
  loadHealth: () => Promise<void>;
}

export const useCatalog = create<CatalogState>((set, get) => ({
  archetypes: null,
  cases: null,
  health: null,
  healthError: null,
  error: null,
  loadArchetypes: async () => {
    if (get().archetypes) return;
    try {
      set({ archetypes: await api.archetypes() });
    } catch (e) {
      set({ error: errorMessage(e) });
    }
  },
  loadCases: async () => {
    try {
      set({ cases: await api.listCases() });
    } catch (e) {
      set({ error: errorMessage(e) });
    }
  },
  loadHealth: async () => {
    try {
      set({ health: await api.health(), healthError: null });
    } catch (e) {
      set({ health: null, healthError: errorMessage(e) });
    }
  },
}));
