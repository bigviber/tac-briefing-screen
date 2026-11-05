# Axiom frontend

React 18 + TypeScript (strict) + Vite 6 + Tailwind 3 + zustand. Monaco (bundled locally, no CDN),
mermaid, recharts, react-markdown/remark-gfm, lucide-react. Dark mode only.

## Scripts

| Command | What it does |
|---|---|
| `npm install` | Install dependencies (Node 22). |
| `npm run dev` | Dev server on http://localhost:5173, proxying `/api` to `http://localhost:8000`. |
| `npm run dev:mock` | Same, with `VITE_MOCK=1` (no backend needed). |
| `npm run build` | `tsc -b` (strict, zero errors) then `vite build` into `dist/`. |
| `npm run typecheck` | Type-check only. |
| `npm run lint` | ESLint (flat config, `--max-warnings 0`). |
| `npm run preview` | Serve the production build. |

## Mock mode

Mock mode answers every §5 endpoint from in-memory fixtures through the same `api` client, so the
whole app can be demoed and screenshotted without the backend. Turn it on with either:

- `VITE_MOCK=1 npm run dev` (build-time), or
- `?mock=1` in the URL, e.g. `http://localhost:5173/?mock=1#/case/cs_7f3a91` (run-time).

The fixtures live in `src/mock/fixtures/`: one SaaS working-capital case (Halcyon Metrics, 8 quarters
whose statements tie: A = L + E, CF to cash), a 9-table DuckDB schema with sample rows and dirt
badges, a 4-message dossier, textbook chapters, asks, levers, a grader, scripted adversary turns and
a 60-node skill DAG across 5 pillars. `src/mock/sqlRunner.ts` returns plausible tables (`GROUP BY`
gives a quarterly DSO table; `CASE` on `due_date` gives aging buckets; `SELECT * FROM <table>` expands
sample rows; DDL/DML returns a read-only error). `pythonRunner.ts` draws a real PNG figure on a canvas.
Dossier, textbook and adversary replies stream through the same SSE consumer as the real API. The
mock module is code-split and loads only in mock mode.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `VITE_MOCK` | unset | `1` serves fixtures instead of calling the backend. |
| `VITE_API_BASE` | `/api/v1` | API prefix. Keep the default with the dev proxy; set a full URL (e.g. `http://localhost:8000/api/v1`) for a static build served elsewhere (backend CORS must allow the origin). |

## Layout

```
src/
  api/        types.ts (§4/§11 mirrors), client.ts (one wrapper per §5 endpoint), sse.ts (stream helper)
  mock/       router.ts (in-client router), sqlRunner.ts, pythonRunner.ts, fixtures/
  store/      uiStore, settingsStore, skillsStore, catalogStore, caseStore (+ workSlice, caseTypes)
  views/      Dashboard.tsx, Workbench.tsx
  components/ SkillGraph, TopBar, CommandPalette, SettingsDrawer, panes/*, briefing/*, sandbox/*,
              deliverable/*, adversary/*, settings/*, dashboard/*, ui/* (primitives, Monaco, mermaid,
              markdown, resize handle, toasts, error boundary)
  lib/        dagLayout (layered layout, no d3), format, csv, highlight, route, storage, pillars
```

Routing is hash-based: `#/` is the dashboard, `#/case/:id` is the workbench.

## Keyboard

| Keys | Action |
|---|---|
| ⌘K / Ctrl+K | Command palette: generate a case (auto or any archetype), open a recent case, switch briefing/sandbox tabs, run SQL/Python, submit, settings, reset layout. |
| ⌘↵ / Ctrl+↵ | Run SQL or Python (in the editor). |
| ⌘, / Ctrl+, | Open settings. |
| Esc | Close the palette or the settings drawer. |
| ↵ / ⇧↵ | Send / newline in the adversary chat. |
| Arrow keys on a focused pane divider | Resize panes (Shift for larger steps). |

## Persistence (localStorage)

- `axiom:layout:v1` stores the pane sizes.
- `axiom:case:<case_id>:v1` stores per-case drafts: SQL/Python/design sources, financial model, SQL
  history, memo, ask inputs, attached evidence. They are restored when the case is reopened.

Grades, the adversary thread (user and adversary messages) and simulation history live in the
backend; opening a case calls `GET /cases/{id}/session` and restores them.

## Streaming

`api.dossierStream`, `api.textbookStream` and `api.adversaryStream` POST with `?stream=1` and
`Accept: text/event-stream`. Each `data: {"delta": "..."}` chunk is forwarded to the UI, and
`data: {"done": true, "payload": ...}` resolves the call. If the response is not `text/event-stream`,
the body is parsed as the plain JSON payload, so a backend without streaming still works.

## Backend contract

`src/api/types.ts` mirrors `backend/axiom/models.py` and the API views in
`backend/axiom/api/schemas.py` (`CasePublicView`, `SqlQueryResultView`, `SkillGraphView`,
`LLMSettingsView`, `GradeReportPublic`, `SessionState`, `ThreadMessage`). The mock fixtures use the
same shapes, so mock mode and the live backend exercise the same components.

Error statuses with a UI meaning (`backend/axiom/api/errors.py`):

| Status | Meaning | UI |
|---|---|---|
| 428 | LLM not configured | User actions (submit, adversary, retry) open Settings → LLM with a toast. Background loads (dossier on open, textbook chapter) show an inline "needs an LLM" state with a Configure button. |
| 409 on `/textbook` | Principal case: chapters locked until the first submission | Locked empty state; retried automatically after grading. |
| 409 on `/advance` | Quarter budget for the difficulty is spent | Advance button disabled with the reason. |
| 503 on `/cases/generate` | World generator unavailable | Toast. |
| 400 on `/sandbox/sql` | Statement rejected by the read-only allowlist | Shown in the results pane. A failing (but allowed) statement is HTTP 200 with `error`. |

Notes:

- `ReferenceSolution.sql` / `.python` are lists; each item renders as its own code block.
- `RubricGrade.ceiling_te` is always sent (10 = unconstrained); the grade report notes it only when
  it is below 10, and says "capped" when the model's raw technical-execution score exceeded it.
- `Dossier.leak_redacted` / `TextbookChapter.leak_redacted` show a "redacted" badge.
- The Python sandbox exposes `tables["name"]` (lazy DataFrames), `con` (read-only DuckDB) and
  returns a top-level `result` as `result_json`.
