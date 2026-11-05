/**
 * In-client mock router. Answers every §5 endpoint with fixture data so the app can be
 * demoed without the backend. State is kept in memory for the page lifetime.
 */
import { ApiError } from "../api/client";
import type { StreamEvent } from "../api/sse";
import type {
  AdvanceRequest,
  AdversaryRequest,
  CasePublic,
  CaseSummary,
  GenerateRequest,
  LLMSettings,
  PythonRequest,
  SimQuarter,
  SqlRequest,
  Submission,
} from "../api/types";
import { ARCHETYPES } from "./fixtures/archetypes";
import { buildCase, CASE_ID } from "./fixtures/case";
import { DOSSIER } from "./fixtures/dossier";
import { chapterFor } from "./fixtures/textbook";
import { gradeSubmission } from "./fixtures/grade";
import { adversaryTurn } from "./fixtures/adversary";
import { buildSkillGraph } from "./fixtures/skills";
import { LLM_SETTINGS, PROMPTS, USAGE } from "./fixtures/settings";
import { runSql } from "./sqlRunner";
import { runPython } from "./pythonRunner";

const sleep = (ms: number) => new Promise<void>((res) => setTimeout(res, ms));

const graph = buildSkillGraph();
const cases = new Map<string, CasePublic>([[CASE_ID, buildCase()]]);
const summaries: CaseSummary[] = [
  summarize(buildCase()),
  { case_id: "cs_2b81c0", archetype: "idempotent_webhooks", company_name: "Ferrous Pay", industry: "fintech", difficulty: "foundation", pillars: ["system_design"], created_at: "2026-11-02T10:11:00Z", status: "graded" },
  { case_id: "cs_93ad14", archetype: "cohort_collapse", company_name: "Lumen Fitness", industry: "consumer_subscription", difficulty: "practitioner", pillars: ["operations_strategy", "sql"], created_at: "2026-10-29T16:40:00Z", status: "submitted" },
  { case_id: "cs_0c4e77", archetype: "toc_bottleneck", company_name: "Arden Fabrication", industry: "manufacturing", difficulty: "foundation", pillars: ["operations_strategy"], created_at: "2026-10-21T08:02:00Z", status: "generated" },
];
let settings = { ...LLM_SETTINGS };
let submissions = 0;
const adversaryIdx = new Map<string, number>();
const simHistory = new Map<string, SimQuarter[]>();

function summarize(c: CasePublic): CaseSummary {
  return { case_id: c.case_id, archetype: c.archetype, company_name: c.company.name, industry: c.industry.name, difficulty: c.difficulty, pillars: c.pillars, created_at: c.generated_at, status: c.status ?? "generated" };
}

function getCase(id: string): CasePublic {
  const c = cases.get(id) ?? (summaries.some((s) => s.case_id === id) ? buildCase({ case_id: id }) : undefined);
  if (!c) throw new ApiError(404, `Case ${id} not found`);
  cases.set(id, c);
  return c;
}

function nextPeriod(p: string): string {
  const y = Number(p.slice(0, 4));
  const q = Number(p.slice(5));
  return q === 4 ? `${y + 1}Q1` : `${y}Q${q + 1}`;
}

function advance(id: string, req: AdvanceRequest): SimQuarter {
  const c = getCase(id);
  const hist = simHistory.get(id) ?? [];
  const last = hist.at(-1);
  const period = nextPeriod(last?.period ?? c.simulation.current_period);
  const d = req.decisions;
  const prevDso = last?.kpis.dso ?? 78;
  const fte = d.collections_fte ?? 2;
  const terms = d.payment_terms_days ?? 60;
  const strict = d.dunning_strictness ?? 0.3;
  const disc = d.early_pay_discount_pct ?? 0;
  const dso = Math.max(32, prevDso - 4.1 * Math.log1p(Math.max(0, fte - 2)) * 2 - (60 - terms) * 0.35 - strict * 6 - disc * 300);
  const rev = (last?.kpis.revenue_usd ?? 14_300_000) * (1.045 - (60 - terms) * 0.0008 - strict * 0.01);
  const ar = (rev * dso) / 91;
  const cash = (last?.kpis.cash_usd ?? 2_000_000) + ((last?.kpis.ar_usd ?? 12_200_000) - ar) * 0.9 - fte * 38_000 - rev * disc * 0.4;
  const q: SimQuarter = {
    period,
    decisions: d,
    kpis: { revenue_usd: Math.round(rev), ar_usd: Math.round(ar), dso: Number(dso.toFixed(1)), cash_usd: Math.round(cash), churn_monthly: Number((0.013 + strict * 0.004).toFixed(4)), covenant_headroom_usd: Math.round(cash - 2_000_000) },
    narrative: dso < prevDso - 5
      ? `Collections tightened in ${period}: DSO fell to ${dso.toFixed(0)} days and roughly $${((cash - (last?.kpis.cash_usd ?? 2e6)) / 1e6).toFixed(1)}M came back onto the balance sheet. Two enterprise renewals pushed back on stricter terms but signed.`
      : `${period} looked like the prior quarter: receivables kept pace with revenue and the revolver stayed drawn. The board asked why the plan did not move cash.`,
  };
  simHistory.set(id, [...hist, q]);
  return q;
}

type Handler = (m: RegExpMatchArray, body: unknown) => unknown;
const routes: [string, RegExp, Handler][] = [
  ["GET", /^\/health$/, () => ({ status: "ok", version: "0.1.0-mock", llm_configured: settings.configured })],
  ["GET", /^\/archetypes$/, () => ARCHETYPES],
  ["POST", /^\/cases\/generate$/, (_m, b) => {
    const req = (b ?? {}) as GenerateRequest;
    const seed = req.seed ?? Math.floor(10000 + Math.random() * 89999);
    const c = buildCase({ case_id: `cs_${seed.toString(16)}`, seed, archetype: req.archetype ?? "working_capital_crunch", difficulty: req.difficulty ?? "practitioner", status: "generated", generated_at: new Date().toISOString() });
    cases.set(c.case_id, c);
    summaries.unshift(summarize(c));
    return c;
  }],
  ["GET", /^\/cases$/, () => summaries],
  ["GET", /^\/cases\/([^/]+)$/, (m) => getCase(m[1])],
  ["GET", /^\/cases\/([^/]+)\/schema$/, (m) => getCase(m[1]).database],
  ["POST", /^\/cases\/([^/]+)\/dossier$/, (m) => (getCase(m[1]), DOSSIER)],
  ["POST", /^\/cases\/([^/]+)\/textbook$/, (_m, b) => {
    const id = (b as { skill_node: string }).skill_node;
    return chapterFor(graph.nodes.find((n) => n.id === id), id);
  }],
  ["POST", /^\/sandbox\/sql$/, (_m, b) => runSql(b as SqlRequest)],
  ["POST", /^\/sandbox\/python$/, (_m, b) => runPython(b as PythonRequest)],
  ["POST", /^\/cases\/([^/]+)\/submit$/, (m, b) => {
    submissions += 1;
    const c = getCase(m[1]);
    c.status = "graded";
    return gradeSubmission(b as Submission, submissions);
  }],
  ["POST", /^\/cases\/([^/]+)\/adversary$/, (_m, b) => {
    const { submission_id } = b as AdversaryRequest;
    // Turn 0 is the grade's adversary_opening; replies start at turn 1.
    const i = adversaryIdx.get(submission_id) ?? 0;
    adversaryIdx.set(submission_id, i + 1);
    return adversaryTurn(i + 1);
  }],
  ["POST", /^\/cases\/([^/]+)\/advance$/, (m, b) => advance(m[1], b as AdvanceRequest)],
  ["GET", /^\/skills\/graph$/, () => graph],
  ["POST", /^\/skills\/reset$/, () => {
    graph.nodes.forEach((n) => { n.mastery = 0.1; n.attempts = 0; });
    return { ok: true };
  }],
  ["GET", /^\/settings\/llm$/, () => settings],
  ["PUT", /^\/settings\/llm$/, (_m, b) => {
    const s = b as LLMSettings;
    const masked = s.api_key ? `${s.api_key.slice(0, 5)}…${s.api_key.slice(-4)}` : settings.api_key_masked;
    settings = { provider: s.provider, base_url: s.base_url, api_key_masked: masked, fast_model: s.fast_model, deep_model: s.deep_model, temperature_fast: s.temperature_fast, temperature_deep: s.temperature_deep, max_tokens: s.max_tokens, configured: masked.length > 0 };
    return settings;
  }],
  ["POST", /^\/settings\/llm\/test$/, () => ({ ok: true, model: settings.fast_model, latency_ms: 412 })],
  ["GET", /^\/settings\/usage$/, () => USAGE],
  ["GET", /^\/settings\/prompts$/, () => PROMPTS],
];

export async function mockRequest(method: string, path: string, body: unknown): Promise<unknown> {
  const slow = /submit|generate|python/.test(path) ? 700 : /sql|dossier|textbook|adversary|advance/.test(path) ? 280 : 90;
  await sleep(slow);
  for (const [m, re, handler] of routes) {
    if (m !== method) continue;
    const match = path.match(re);
    if (match) return structuredClone(handler(match, body));
  }
  throw new ApiError(404, `Mock: no route for ${method} ${path}`);
}

function chunk(text: string, size = 28): string[] {
  const out: string[] = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out;
}

export async function* mockStream(path: string, body: unknown): AsyncGenerator<StreamEvent<unknown>> {
  const payload = await mockRequest("POST", path, body);
  const text =
    path.endsWith("/dossier") ? DOSSIER.background_markdown
    : path.endsWith("/adversary") ? (payload as { message: string }).message
    : (payload as { markdown?: string }).markdown ?? "";
  for (const piece of chunk(text)) {
    await sleep(12);
    yield { delta: piece };
  }
  yield { done: true, payload };
}
