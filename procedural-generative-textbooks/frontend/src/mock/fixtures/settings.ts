import type { LLMSettingsPublic, LLMUsageSummary, PromptTemplate } from "../../api/types";

export const LLM_SETTINGS: LLMSettingsPublic = {
  provider: "openrouter",
  base_url: "https://openrouter.ai/api/v1",
  api_key_masked: "sk-or-…a91f",
  fast_model: "anthropic/claude-haiku-5.5",
  deep_model: "anthropic/claude-opus-5.5",
  temperature_fast: 0.7,
  temperature_deep: 0.2,
  max_tokens: 4096,
  configured: true,
};

export const USAGE: LLMUsageSummary = {
  by_model: {
    "anthropic/claude-opus-5.5": { prompt_tokens: 184_220, completion_tokens: 41_806, calls: 23, cost_usd_est: 1.18 },
    "anthropic/claude-haiku-5.5": { prompt_tokens: 96_410, completion_tokens: 28_930, calls: 57, cost_usd_est: 0.24 },
    "openai/gpt-4.1-mini": { prompt_tokens: 12_004, completion_tokens: 3_120, calls: 6, cost_usd_est: 0.01 },
  },
  total_tokens: 366_490,
  total_cost_usd_est: 1.43,
};

export const PROMPTS: PromptTemplate[] = [
  { name: "dossier_writer", template: "SYSTEM: You are a case writer for a business school.\nWrite a dossier for {company.name} using ONLY the facts in REVEAL.\nNever state any string in CONCEAL.\n\nREVEAL:\n{reveal}\n\nSTAKEHOLDERS:\n{stakeholders}\n\nReturn JSON matching the Dossier schema." },
  { name: "textbook_chapter", template: "SYSTEM: Write a textbook chapter for skill node {skill_node}.\nGround every example in the case tables: {tables}.\nInclude runnable SQL against DuckDB and pandas code.\nReturn JSON: {title, markdown, self_checks: [{question, answer}]}" },
  { name: "rubric_grader", template: "SYSTEM: You grade an analyst's submission.\nThe deterministic grade is authoritative for numbers:\n{deterministic_grade}\n\nGround truth:\n{ground_truth}\n\nScore technical_execution, business_judgment, communication, risk_awareness 0-10 with verbatim evidence quotes." },
  { name: "socratic_adversary", template: "SYSTEM: You are {persona}. Challenge the analyst's conclusion using red herrings:\n{red_herrings}\nCite metric keys. Concede only when the analyst's evidence addresses your challenge." },
  { name: "quarter_narrator", template: "SYSTEM: Narrate the quarter in 3 sentences given decisions {decisions} and resulting KPIs {kpis}. Do not invent numbers." },
];
