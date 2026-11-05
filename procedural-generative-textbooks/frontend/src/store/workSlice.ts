import type { StateCreator } from "zustand";
import { api, apiStatus, errorMessage, HTTP_CONFLICT, HTTP_LLM_NOT_CONFIGURED } from "../api/client";
import type { GradeReport, SimQuarter, Submission } from "../api/types";
import { previewTable } from "../lib/csv";
import type { CaseStore } from "./caseStore";
import { blankTurn, threadFromSession, type ThreadItem } from "./caseTypes";
import { requireLlm, toast } from "./uiStore";
import { useSkills } from "./skillsStore";

/** Sandbox execution, evidence, submission/grade, adversary thread and quarter simulation. */
export interface WorkSlice {
  grade: GradeReport | null;
  submitting: boolean;
  thread: ThreadItem[];
  adversaryBusy: boolean;
  sim: SimQuarter[];
  advancing: boolean;
  /** Set when the backend answered 409 to /advance: the case's quarter budget is spent. */
  simExhausted: string | null;
  levers: Record<string, number>;
  runSql: (sqlOverride?: string) => Promise<void>;
  runPython: () => Promise<void>;
  attachSql: () => void;
  attachPython: () => void;
  removeEvidence: (kind: "sql" | "python", index: number) => void;
  submit: () => Promise<void>;
  sendAdversary: (message: string) => Promise<void>;
  setLever: (key: string, value: number) => void;
  advance: () => Promise<void>;
}

export const createWorkSlice: StateCreator<CaseStore, [], [], WorkSlice> = (set, get) => ({
  grade: null,
  submitting: false,
  thread: [],
  adversaryBusy: false,
  sim: [],
  advancing: false,
  simExhausted: null,
  levers: {},

  runSql: async (sqlOverride) => {
    const { caseId, draft } = get();
    if (!caseId || !draft || get().sqlRunning) return;
    const sql = sqlOverride ?? draft.sql;
    set({ sqlRunning: true });
    try {
      const r = await api.runSql({ case_id: caseId, sql, limit: 500 });
      const item = { sql, at: Date.now(), rowCount: r.row_count, ms: r.elapsed_ms, error: r.error ?? null };
      const d = get().draft;
      set({ sqlResult: r, draft: d ? { ...d, sqlHistory: [item, ...d.sqlHistory].slice(0, 30) } : d });
    } catch (e) {
      set({ sqlResult: { columns: [], column_types: [], rows: [], row_count: 0, elapsed_ms: 0, truncated: false, error: errorMessage(e) } });
    } finally {
      set({ sqlRunning: false });
    }
  },

  runPython: async () => {
    const { caseId, draft } = get();
    if (!caseId || !draft || get().pyRunning) return;
    set({ pyRunning: true });
    try {
      set({ pyResult: await api.runPython({ case_id: caseId, code: draft.python, timeout_s: 20 }) });
    } catch (e) {
      set({ pyResult: { stdout: "", stderr: "", result_json: null, figures: [], elapsed_ms: 0, error: errorMessage(e) } });
    } finally {
      set({ pyRunning: false });
    }
  },

  attachSql: () => {
    const { draft, sqlResult } = get();
    if (!draft || !sqlResult || sqlResult.error) return;
    const name = `Query ${draft.sqlEvidence.length + 1}`;
    const ev = { name, sql: draft.sql, result_preview: previewTable(sqlResult.columns, sqlResult.rows) };
    get().patchDraft({ sqlEvidence: [...draft.sqlEvidence, ev] });
    toast(`Attached "${name}" as evidence`, "success");
  },

  attachPython: () => {
    const { draft, pyResult } = get();
    if (!draft || !pyResult) return;
    const name = `Script ${draft.pythonEvidence.length + 1}`;
    const ev = { name, code: draft.python, stdout_tail: pyResult.stdout.split("\n").slice(-20).join("\n"), result_json: pyResult.result_json ?? null };
    get().patchDraft({ pythonEvidence: [...draft.pythonEvidence, ev] });
    toast(`Attached "${name}" as evidence`, "success");
  },

  removeEvidence: (kind, index) => {
    const d = get().draft;
    if (!d) return;
    if (kind === "sql") get().patchDraft({ sqlEvidence: d.sqlEvidence.filter((_, i) => i !== index) });
    else get().patchDraft({ pythonEvidence: d.pythonEvidence.filter((_, i) => i !== index) });
  },

  submit: async () => {
    const { caseId, draft, caseData } = get();
    if (!caseId || !draft || !caseData || get().submitting) return;
    const metrics: Record<string, number> = {};
    for (const ask of caseData.asks) {
      const raw = (draft.metrics[ask.key] ?? "").replace(/[$,%\s]/g, "");
      if (raw === "") continue;
      const n = Number(raw);
      if (!Number.isNaN(n)) metrics[ask.key] = ask.unit === "pct" && n > 1 ? n / 100 : n;
    }
    const submission: Submission = {
      case_id: caseId,
      memo_markdown: draft.memo,
      metrics,
      sql_evidence: draft.sqlEvidence,
      python_evidence: draft.pythonEvidence,
      design_mermaid: draft.designAttached ? draft.design : null,
      financial_model: draft.modelAttached ? draft.model : null,
    };
    set({ submitting: true });
    try {
      const grade = await api.submit(caseId, submission);
      const opening: ThreadItem = {
        role: "adversary",
        turn: blankTurn({ persona: caseData.dossier_seed.stakeholders[1]?.name ?? "Adversary", message: grade.adversary_opening, challenge_type: "opening", turn_index: 0 }),
      };
      set({ grade, thread: grade.adversary_opening ? [opening] : [], sim: [], simExhausted: null, caseData: { ...caseData, status: "graded" } });
      toast(`Graded · accuracy ${(grade.deterministic.accuracy * 100).toFixed(0)}% · rubric ${grade.rubric.overall.toFixed(1)}/10`, "success");
      void useSkills.getState().load();
      // The backend stores the opening as a real adversary turn (persona, challenge type, rung).
      try {
        const s = await api.session(caseId);
        if (get().caseId === caseId && s.submission_id === grade.submission_id && s.thread.length > 0) set({ thread: threadFromSession(s) });
      } catch {
        /* keep the synthesized opening */
      }
    } catch (e) {
      if (apiStatus(e) === HTTP_LLM_NOT_CONFIGURED) requireLlm("Grading");
      else toast(`Submit failed: ${errorMessage(e)}`, "error");
    } finally {
      set({ submitting: false });
    }
  },

  sendAdversary: async (message) => {
    const { caseId, grade } = get();
    if (!caseId || !grade || get().adversaryBusy) return;
    const placeholder: Extract<ThreadItem, { role: "adversary" }> = { role: "adversary", streaming: true, turn: blankTurn({ challenge_type: "…" }) };
    set({ adversaryBusy: true, thread: [...get().thread, { role: "user", message, at: Date.now() }, placeholder] });
    const replaceLast = (item: ThreadItem | null) => {
      const t = get().thread.slice(0, -1);
      set({ thread: item ? [...t, item] : t });
    };
    try {
      const turn = await api.adversaryStream(caseId, { submission_id: grade.submission_id, message }, (_d, acc) =>
        replaceLast({ ...placeholder, turn: { ...placeholder.turn, message: acc } }),
      );
      replaceLast({ role: "adversary", turn });
    } catch (e) {
      replaceLast(null);
      if (apiStatus(e) === HTTP_LLM_NOT_CONFIGURED) requireLlm("The adversary");
      else toast(`Adversary: ${errorMessage(e)}`, "error");
    } finally {
      set({ adversaryBusy: false });
    }
  },

  setLever: (key, value) => set({ levers: { ...get().levers, [key]: value } }),

  advance: async () => {
    const { caseId, grade, levers } = get();
    if (!caseId || !grade || get().advancing || get().simExhausted) return;
    set({ advancing: true });
    try {
      const q = await api.advance(caseId, { submission_id: grade.submission_id, decisions: levers });
      set({ sim: [...get().sim, q] });
    } catch (e) {
      const status = apiStatus(e);
      if (status === HTTP_CONFLICT) {
        set({ simExhausted: errorMessage(e) });
        toast("Quarter budget exhausted for this case's difficulty.", "info");
      } else if (status === HTTP_LLM_NOT_CONFIGURED) requireLlm("Quarter narration");
      else toast(`Advance failed: ${errorMessage(e)}`, "error");
    } finally {
      set({ advancing: false });
    }
  },
});
