import { create } from "zustand";
import { api, errorMessage } from "../api/client";
import type { LLMSettings, LLMSettingsPublic, LLMTestResult, LLMUsageSummary, PromptTemplate } from "../api/types";
import { toast } from "./uiStore";
import { useCatalog } from "./catalogStore";

interface SettingsState {
  settings: LLMSettingsPublic | null;
  usage: LLMUsageSummary | null;
  prompts: PromptTemplate[] | null;
  loading: boolean;
  saving: boolean;
  clearing: boolean;
  testing: boolean;
  testResult: LLMTestResult | null;
  error: string | null;
  load: () => Promise<void>;
  loadUsage: () => Promise<void>;
  loadPrompts: () => Promise<void>;
  save: (s: LLMSettings) => Promise<boolean>;
  /** DELETE /settings/llm: forget the stored key and models (back to unconfigured). */
  clear: () => Promise<boolean>;
  test: () => Promise<void>;
}

export const useSettings = create<SettingsState>((set, get) => ({
  settings: null,
  usage: null,
  prompts: null,
  loading: false,
  saving: false,
  clearing: false,
  testing: false,
  testResult: null,
  error: null,
  load: async () => {
    set({ loading: true, error: null });
    try {
      set({ settings: await api.getLlmSettings() });
    } catch (e) {
      set({ error: errorMessage(e) });
    } finally {
      set({ loading: false });
    }
  },
  loadUsage: async () => {
    try {
      set({ usage: await api.usage() });
    } catch (e) {
      set({ error: errorMessage(e) });
    }
  },
  loadPrompts: async () => {
    if (get().prompts) return;
    try {
      set({ prompts: await api.prompts() });
    } catch (e) {
      toast(`Prompts: ${errorMessage(e)}`, "error");
    }
  },
  save: async (s) => {
    set({ saving: true });
    try {
      set({ settings: await api.putLlmSettings(s), testResult: null });
      toast("LLM settings saved", "success");
      void useCatalog.getState().loadHealth();
      return true;
    } catch (e) {
      toast(`Save failed: ${errorMessage(e)}`, "error");
      return false;
    } finally {
      set({ saving: false });
    }
  },
  clear: async () => {
    set({ clearing: true });
    try {
      set({ settings: await api.clearLlmSettings(), testResult: null });
      toast("Stored API key cleared", "success");
      void useCatalog.getState().loadHealth();
      return true;
    } catch (e) {
      toast(`Clear failed: ${errorMessage(e)}`, "error");
      return false;
    } finally {
      set({ clearing: false });
    }
  },
  test: async () => {
    set({ testing: true, testResult: null });
    try {
      set({ testResult: await api.testLlm() });
    } catch (e) {
      set({ testResult: { ok: false, model: get().settings?.fast_model ?? "", latency_ms: 0, error: errorMessage(e) } });
    } finally {
      set({ testing: false });
    }
  },
}));
