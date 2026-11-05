import { create } from "zustand";
import { loadJson, saveJson } from "../lib/storage";

export type BriefTab = "dossier" | "textbook" | "data" | "architecture";
export type SandboxTab = "sql" | "python" | "design" | "model";
export type ToastKind = "info" | "success" | "error";

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

export interface PaneSizes {
  /** Briefing pane width, px. */
  left: number;
  /** Adversary pane width, px. */
  right: number;
  /** Deliverable pane height, px. */
  bottom: number;
}

const LAYOUT_KEY = "axiom:layout:v1";
const DEFAULT_SIZES: PaneSizes = { left: 420, right: 360, bottom: 340 };

interface UiState {
  panes: PaneSizes;
  briefTab: BriefTab;
  sandboxTab: SandboxTab;
  paletteOpen: boolean;
  settingsOpen: boolean;
  settingsTab: "llm" | "usage" | "prompts";
  toasts: Toast[];
  setPane: (key: keyof PaneSizes, px: number) => void;
  resetPanes: () => void;
  setBriefTab: (t: BriefTab) => void;
  setSandboxTab: (t: SandboxTab) => void;
  setPalette: (open: boolean) => void;
  setSettings: (open: boolean, tab?: UiState["settingsTab"]) => void;
  toast: (message: string, kind?: ToastKind) => void;
  dismissToast: (id: number) => void;
}

const LIMITS: Record<keyof PaneSizes, [number, number]> = {
  left: [280, 760],
  right: [280, 640],
  bottom: [160, 720],
};

let toastSeq = 0;

export const useUi = create<UiState>((set, get) => ({
  panes: loadJson(LAYOUT_KEY, DEFAULT_SIZES),
  briefTab: "dossier",
  sandboxTab: "sql",
  paletteOpen: false,
  settingsOpen: false,
  settingsTab: "llm",
  toasts: [],
  setPane: (key, px) => {
    const [lo, hi] = LIMITS[key];
    const panes = { ...get().panes, [key]: Math.round(Math.min(hi, Math.max(lo, px))) };
    set({ panes });
    saveJson(LAYOUT_KEY, panes);
  },
  resetPanes: () => {
    set({ panes: DEFAULT_SIZES });
    saveJson(LAYOUT_KEY, DEFAULT_SIZES);
  },
  setBriefTab: (briefTab) => set({ briefTab }),
  setSandboxTab: (sandboxTab) => set({ sandboxTab }),
  setPalette: (paletteOpen) => set({ paletteOpen }),
  setSettings: (settingsOpen, tab) => set({ settingsOpen, settingsTab: tab ?? get().settingsTab }),
  toast: (message, kind = "info") => {
    const id = ++toastSeq;
    set({ toasts: [...get().toasts, { id, kind, message }] });
    window.setTimeout(() => get().dismissToast(id), kind === "error" ? 6000 : 3200);
  },
  dismissToast: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

/** Shortcut for non-React callers (stores, async actions). */
export const toast = (message: string, kind?: ToastKind) => useUi.getState().toast(message, kind);

let lastLlmPrompt = 0;
/**
 * The backend answered 428 (LLM not configured): open Settings on the LLM tab with a toast.
 * De-duplicated so parallel requests (dossier + chapter) prompt once.
 */
export function requireLlm(action: string): void {
  const now = Date.now();
  if (now - lastLlmPrompt < 2500) return;
  lastLlmPrompt = now;
  const ui = useUi.getState();
  ui.setSettings(true, "llm");
  ui.toast(`${action} needs an LLM. Choose a provider and add an API key (or pick Local).`, "error");
}
