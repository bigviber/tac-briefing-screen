/**
 * Typed wrappers for every endpoint in docs/ARCHITECTURE.md §5.
 * In mock mode (VITE_MOCK=1 or ?mock=1) requests are answered by the in-client router in src/mock.
 */
import { readStreamOrJson, consumeStream, type DeltaHandler, type StreamEvent } from "./sse";
import type {
  AdvanceRequest,
  AdversaryRequest,
  AdversaryTurn,
  ArchetypeInfo,
  CasePublic,
  CaseSummary,
  DatabaseSpec,
  Dossier,
  GenerateRequest,
  GradeReport,
  Health,
  LLMSettings,
  LLMSettingsPublic,
  LLMTestResult,
  LLMUsageSummary,
  PromptTemplate,
  PythonRequest,
  PythonResult,
  SessionState,
  SimQuarter,
  SkillGraph,
  SqlRequest,
  SqlResult,
  Submission,
  TextbookChapter,
  WorldStats,
} from "./types";

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api/v1";

export function isMockMode(): boolean {
  if (import.meta.env.VITE_MOCK === "1") return true;
  try {
    return new URLSearchParams(window.location.search).get("mock") === "1";
  } catch {
    return false;
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(`${status ? `HTTP ${status}: ` : ""}${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

type Method = "GET" | "POST" | "PUT";

interface MockModule {
  mockRequest: (method: Method, path: string, body: unknown) => Promise<unknown>;
  mockStream: (path: string, body: unknown) => AsyncIterable<StreamEvent<unknown>>;
}

let mockModule: Promise<MockModule> | null = null;
function loadMock(): Promise<MockModule> {
  mockModule ??= import("../mock/router");
  return mockModule;
}

async function errorFrom(res: Response): Promise<ApiError> {
  let detail = res.statusText || "Request failed";
  try {
    // JSON boundary: FastAPI error bodies are {detail: string | object}.
    const body = (await res.json()) as { detail?: unknown };
    if (body.detail !== undefined) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* body was not JSON */
  }
  return new ApiError(res.status, detail);
}

async function request<T>(method: Method, path: string, body?: unknown): Promise<T> {
  if (isMockMode()) {
    const m = await loadMock();
    return (await m.mockRequest(method, path, body)) as T;
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, `Backend unreachable (${e instanceof Error ? e.message : String(e)})`);
  }
  if (!res.ok) throw await errorFrom(res);
  // JSON boundary: response bodies are typed by the endpoint wrapper.
  return (await res.json()) as T;
}

/** POST to a streaming-capable endpoint; falls back to plain JSON when the server does not stream. */
async function streamRequest<T>(path: string, body: unknown, onDelta?: DeltaHandler): Promise<T> {
  if (isMockMode()) {
    const m = await loadMock();
    return (await consumeStream(m.mockStream(path, body), onDelta)) as T;
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}?stream=1`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, `Backend unreachable (${e instanceof Error ? e.message : String(e)})`);
  }
  if (!res.ok) throw await errorFrom(res);
  return readStreamOrJson<T>(res, onDelta);
}

const enc = encodeURIComponent;

export const api = {
  health: () => request<Health>("GET", "/health"),
  archetypes: () => request<ArchetypeInfo[]>("GET", "/archetypes"),

  generateCase: (req: GenerateRequest) => request<CasePublic>("POST", "/cases/generate", req),
  listCases: () => request<CaseSummary[]>("GET", "/cases"),
  getCase: (id: string) => request<CasePublic>("GET", `/cases/${enc(id)}`),
  getSchema: (id: string) => request<DatabaseSpec>("GET", `/cases/${enc(id)}/schema`),
  session: (id: string) => request<SessionState>("GET", `/cases/${enc(id)}/session`),
  worldStats: (id: string) => request<WorldStats>("GET", `/cases/${enc(id)}/world-stats`),

  dossier: (id: string, force = false) => request<Dossier>("POST", `/cases/${enc(id)}/dossier`, { force }),
  dossierStream: (id: string, force: boolean, onDelta?: DeltaHandler) =>
    streamRequest<Dossier>(`/cases/${enc(id)}/dossier`, { force }, onDelta),

  textbook: (id: string, skillNode: string) =>
    request<TextbookChapter>("POST", `/cases/${enc(id)}/textbook`, { skill_node: skillNode }),
  textbookStream: (id: string, skillNode: string, onDelta?: DeltaHandler) =>
    streamRequest<TextbookChapter>(`/cases/${enc(id)}/textbook`, { skill_node: skillNode }, onDelta),

  runSql: (req: SqlRequest) => request<SqlResult>("POST", "/sandbox/sql", req),
  runPython: (req: PythonRequest) => request<PythonResult>("POST", "/sandbox/python", req),

  submit: (id: string, submission: Submission) => request<GradeReport>("POST", `/cases/${enc(id)}/submit`, submission),
  adversary: (id: string, req: AdversaryRequest) => request<AdversaryTurn>("POST", `/cases/${enc(id)}/adversary`, req),
  adversaryStream: (id: string, req: AdversaryRequest, onDelta?: DeltaHandler) =>
    streamRequest<AdversaryTurn>(`/cases/${enc(id)}/adversary`, req, onDelta),
  advance: (id: string, req: AdvanceRequest) => request<SimQuarter>("POST", `/cases/${enc(id)}/advance`, req),

  skillGraph: () => request<SkillGraph>("GET", "/skills/graph"),
  resetSkills: () => request<{ ok: boolean }>("POST", "/skills/reset"),

  getLlmSettings: () => request<LLMSettingsPublic>("GET", "/settings/llm"),
  putLlmSettings: (s: LLMSettings) => request<LLMSettingsPublic>("PUT", "/settings/llm", s),
  testLlm: () => request<LLMTestResult>("POST", "/settings/llm/test"),
  usage: () => request<LLMUsageSummary>("GET", "/settings/usage"),
  prompts: () => request<PromptTemplate[]>("GET", "/settings/prompts"),
};

/** HTTP status of an API error (0 = network failure, null = not an API error). */
export function apiStatus(e: unknown): number | null {
  return e instanceof ApiError ? e.status : null;
}

/**
 * Error statuses with a product meaning (backend/axiom/api/errors.py):
 * 428 LLM not configured, 409 state conflict (chapter locked / quarter budget spent),
 * 503 a backend module (world generator) is unavailable.
 */
export const HTTP_LLM_NOT_CONFIGURED = 428;
export const HTTP_CONFLICT = 409;
export const HTTP_UNAVAILABLE = 503;

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.detail;
  if (e instanceof Error) return e.message;
  return String(e);
}
