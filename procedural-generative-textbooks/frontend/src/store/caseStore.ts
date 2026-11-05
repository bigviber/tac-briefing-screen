import { create } from "zustand";
import { api, apiStatus, errorMessage, HTTP_LLM_NOT_CONFIGURED, HTTP_UNAVAILABLE } from "../api/client";
import type { CasePublic, Dossier, GenerateRequest, TextbookChapter } from "../api/types";
import { loadJson, saveJson } from "../lib/storage";
import { emptyPersisted, threadFromSession, type PersistedCaseState, type RunState } from "./caseTypes";
import { createWorkSlice, type WorkSlice } from "./workSlice";
import { requireLlm, toast } from "./uiStore";

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
  /** HTTP status of the last dossier failure (428 = LLM not configured). */
  dossierErrorStatus: number | null;
  chapters: Record<string, TextbookChapter>;
  chapterStream: string;
  chapterLoading: string | null;
  chapterError: string | null;
  /** HTTP status of the last chapter failure (409 = locked until first submission, 428 = no LLM). */
  chapterErrorStatus: number | null;
  /** True while `GET /cases/{id}/session` is restoring grade, thread and simulation. */
  sessionLoading: boolean;
  draft: PersistedCaseState | null;
  open: (id: string) => Promise<void>;
  generate: (req: GenerateRequest) => Promise<CasePublic | null>;
  /** `interactive` = user clicked: a 428 opens Settings instead of only showing inline. */
  loadDossier: (force?: boolean, interactive?: boolean) => Promise<void>;
  loadChapter: (node: string, interactive?: boolean) => Promise<void>;
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
  dossierErrorStatus: null,
  chapters: {},
  chapterStream: "",
  chapterLoading: null,
  chapterError: null,
  chapterErrorStatus: null,
  grade: null,
  submitting: false,
  thread: [],
  sim: [],
  advancing: false,
  simExhausted: null,
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
  dossierErrorStatus: null,
  chapters: {},
  chapterStream: "",
  chapterLoading: null,
  chapterError: null,
  chapterErrorStatus: null,
  sessionLoading: false,
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
      void restoreSession(id);
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
      // Case ids are deterministic (archetype-seed-difficulty): re-generating restores prior work.
      if (c.status !== "generated") void restoreSession(c.case_id);
      toast(`Generated ${c.company.name} · seed ${c.seed}`, "success");
      return c;
    } catch (e) {
      const msg = apiStatus(e) === HTTP_UNAVAILABLE ? `World generator unavailable: ${errorMessage(e)}` : `Generation failed: ${errorMessage(e)}`;
      toast(msg, "error");
      return null;
    } finally {
      set({ generating: false });
    }
  },

  loadDossier: async (force = false, interactive = false) => {
    const id = get().caseId;
    if (!id || get().dossierLoading) return;
    set({ dossierLoading: true, dossierError: null, dossierErrorStatus: null, dossierStream: "" });
    try {
      const d = await api.dossierStream(id, force, (_delta, acc) => set({ dossierStream: acc }));
      if (get().caseId === id) set({ dossier: d });
    } catch (e) {
      const status = apiStatus(e);
      set({ dossierError: errorMessage(e), dossierErrorStatus: status });
      if (interactive && status === HTTP_LLM_NOT_CONFIGURED) requireLlm("The dossier");
    } finally {
      set({ dossierLoading: false });
    }
  },

  loadChapter: async (node, interactive = false) => {
    const id = get().caseId;
    if (!id || get().chapters[node] || get().chapterLoading === node) return;
    set({ chapterLoading: node, chapterError: null, chapterErrorStatus: null, chapterStream: "" });
    try {
      const ch = await api.textbookStream(id, node, (_d, acc) => set({ chapterStream: acc }));
      if (get().caseId === id) set({ chapters: { ...get().chapters, [node]: ch } });
    } catch (e) {
      const status = apiStatus(e);
      set({ chapterError: errorMessage(e), chapterErrorStatus: status });
      if (interactive && status === HTTP_LLM_NOT_CONFIGURED) requireLlm("The textbook");
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

/** Restore grade, adversary thread (user + adversary messages) and simulation after a reload. */
async function restoreSession(id: string): Promise<void> {
  useCase.setState({ sessionLoading: true });
  try {
    const s = await api.session(id);
    const st = useCase.getState();
    if (st.caseId !== id || !st.caseData) return;
    useCase.setState({
      grade: s.grade,
      thread: threadFromSession(s),
      sim: s.sim_history.length > 0 ? s.sim_history : st.sim,
      caseData: { ...st.caseData, status: s.status },
    });
  } catch (e) {
    // Older backends without /session: keep the freshly hydrated (ungraded) workbench.
    if (apiStatus(e) !== 404) toast(`Could not restore session: ${errorMessage(e)}`, "error");
  } finally {
    useCase.setState({ sessionLoading: false });
  }
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
