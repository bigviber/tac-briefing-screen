import type { Provider } from "../../api/types";

export interface ProviderPreset {
  label: string;
  baseUrl: string;
  fast: string[];
  deep: string[];
  keyHint: string;
}

/** Defaults and model suggestions per provider; every field stays editable. */
export const PROVIDER_PRESETS: Record<Provider, ProviderPreset> = {
  openrouter: {
    label: "OpenRouter",
    baseUrl: "https://openrouter.ai/api/v1",
    fast: ["anthropic/claude-haiku-5.5", "openai/gpt-5-mini", "google/gemini-2.5-flash"],
    deep: ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "openai/gpt-5", "google/gemini-2.5-pro"],
    keyHint: "sk-or-…",
  },
  openai: {
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    fast: ["gpt-5-mini", "gpt-4.1-mini"],
    deep: ["gpt-5", "gpt-4.1"],
    keyHint: "sk-…",
  },
  anthropic: {
    label: "Anthropic",
    baseUrl: "https://api.anthropic.com",
    fast: ["claude-haiku-5-5", "claude-sonnet-5-5"],
    deep: ["claude-opus-5-5", "claude-sonnet-5-5"],
    keyHint: "sk-ant-…",
  },
  local: {
    label: "Local (OpenAI-compatible)",
    baseUrl: "http://localhost:11434/v1",
    fast: ["llama3.1:8b", "qwen2.5:7b-instruct"],
    deep: ["qwen2.5:32b-instruct", "llama3.3:70b"],
    keyHint: "optional",
  },
};
