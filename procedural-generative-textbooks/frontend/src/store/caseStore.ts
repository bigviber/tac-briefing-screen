import { create } from "zustand";
import { api, errorMessage } from "../api/client";
import type { CasePublic, Dossier, GenerateRequest, TextbookChapter } from "../api/types";
import { loadJson, saveJson } from "../lib/storage";
import { emptyPersisted, type PersistedCaseState, type RunState } from "./caseTypes";
import { createWorkSlice, type WorkSlice } from "./workSlice";
import { toast } from "./uiStore";

export interface CoreSlice extends RunState {
  caseId: string | null;
  caseData: CasePublic | null;
  caseLoading: boolean;
  caseError: string | null;
  generating: boolean;
  dossier: Dossier | null;
  dossierStream: string;
  dossierLoading: boolean;
  dossierError: string | null;
  chapters: Record<string, TextbookChapter>;
  chapterStream: string;
  chapterLoading: string | null;
  chapterError: string | null;
  draft: PersistedCaseState | null;
  open: (id: string) => Promise<void>;
  generate: (req: GenerateRequest) => Promise<CasePublic | null>;
  loadDossier: (force?: boolean) => Promise<void>;
  loadChapter: (node: string) => Promise<void>;
  patchDraft: (patch: Partial<PersistedCaseState>) => void;
}

export type CaseStore = CoreSlice & WorkSlice;

const draftKey = (id: string) => `axiom:case:${id}:v1`;

const RESET_WORK = {
  sqlResult: null,
  sqlRunning: false,
  pyResult: null,
  pyRunning: false,
  dossier: null,
  dossierStream: "",
  dossierError: null,
  chapters: {},
  chapterStream: "",
  chapterLoading: null,
  chapterError: null,
  grade: null,
  submitting: false,
  thread: [],
  sim: [],
  advancing: false,
  adversaryBusy: false,
  levers: {},
};

export const useCase = create<CaseStore>()((set, get, store) => ({
  caseId: null,
  caseData: null,
  caseLoading: false,
  caseError: null,
  generating: false,
  dossier: null,
  dossierStream: "",
  dossierLoading: false,
  dossierError: null,
  chapters: {},
  chapterStream: "",
  chapterLoading: null,
  chapterError: null,
  draft: null,
  sqlResult: null,
  sqlRunning: false,
  pyResult: null,
  pyRunning: false,

  open: async (id) => {
    if (get().caseId === id && (get().caseData || get().caseLoading)) return;
    set({ ...RESET_WORK, caseId: id, caseData: null, caseLoading: true, caseError: null, draft: null });
    try {
      const c = await api.getCase(id);
      if (get().caseId !== id) return;
      hydrate(c);
    } catch (e) {
      set({ caseError: errorMessage(e) });
    } finally {
      set({ caseLoading: false });
    }
  },

  generate: async (req) => {
    set({ generating: true });
    try {
      const c = await api.generateCase(req);
      set({ ...RESET_WORK, caseId: c.case_id, caseError: null, caseLoading: false });
      hydrate(c);
      toast(`Generated ${c.company.name} · seed ${c.seed}`, "success");
      return c;
    } catch (e) {
      toast(`Generation failed: ${errorMessage(e)}`, "error");
      return null;
    } finally {
      set({ generating: false });
    }
  },

  loadDossier: async (force = false) => {
    const id = get().caseId;
    if (!id || get().dossierLoading) return;
    set({ dossierLoading: true, dossierError: null, dossierStream: "" });
    try {
      const d = await api.dossierStream(id, force, (_delta, acc) => set({ dossierStream: acc }));
      if (get().caseId === id) set({ dossier: d });
    } catch (e) {
      set({ dossierError: errorMessage(e) });
    } finally {
      set({ dossierLoading: false });
    }
  },

  loadChapter: async (node) => {
    const id = get().caseId;
    if (!id || get().chapters[node]) return;
    set({ chapterLoading: node, chapterError: null, chapterStream: "" });
    try {
      const ch = await api.textbookStream(id, node, (_d, acc) => set({ chapterStream: acc }));
      set({ chapters: { ...get().chapters, [node]: ch } });
    } catch (e) {
      set({ chapterError: errorMessage(e) });
    } finally {
      set({ chapterLoading: null });
    }
  },

  patchDraft: (patch) => {
    const d = get().draft;
    if (!d) return;
    set({ draft: { ...d, ...patch } });
  },

  ...createWorkSlice(set, get, store),
}));

function hydrate(c: CasePublic): void {
  const base = emptyPersisted(c);
  const draft = loadJson<PersistedCaseState>(draftKey(c.case_id), base);
  const levers = Object.fromEntries((c.levers ?? []).map((l) => [l.key, l.default]));
  useCase.setState({ caseData: c, draft, sim: c.simulation.history, levers });
  void useCase.getState().loadDossier();
}

// Persist drafts per case, debounced.
let saveTimer: number | undefined;
useCase.subscribe((s, prev) => {
  if (!s.caseId || !s.draft || s.draft === prev.draft) return;
  const id = s.caseId;
  const draft = s.draft;
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => saveJson(draftKey(id), draft), 400);
});
