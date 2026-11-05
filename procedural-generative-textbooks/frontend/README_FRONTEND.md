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

Grades, adversary threads and simulation history are not persisted client-side (see contract gaps).

## Streaming

`api.dossierStream`, `api.textbookStream` and `api.adversaryStream` POST with `?stream=1` and
`Accept: text/event-stream`. Each `data: {"delta": "..."}` chunk is forwarded to the UI, and
`data: {"done": true, "payload": ...}` resolves the call. If the response is not `text/event-stream`,
the body is parsed as the plain JSON payload, so a backend without streaming still works.

## Contract gaps (for the backend)

1. **Levers before grading.** `Lever`s live in `GroundTruth.decision_levers`, which the public view
   strips. The UI reads an optional `CasePublic.levers: Lever[]` and shows an empty state without it.
2. **Fetching a grade again.** There is no `GET /cases/{id}/submissions` or `GET /submissions/{id}`. After a reload
   the workbench cannot restore the `GradeReport`, `submission_id` (which the adversary and advance
   calls need), adversary turns, or sim history. Suggested: `GET /cases/{id}/session` →
   `{latest_submission_id, grade, adversary_turns, sim_history}`.
3. **SQL column types.** `/sandbox/sql` returns no column types. The grid uses an optional
   `column_types: string[]` and otherwise infers types from the values.
4. **Skill node summaries.** `/skills/graph` nodes have no description. The hover card uses an optional
   `summary`.
5. **Case status.** `CasePublic` has no `status`. The UI reads an optional `status`, falling back to
   the `CaseSummary` value.
6. **Revealed anomaly shape.** §5 types `revealed.anomalies` as `[...]`. The UI expects
   `{id, kind, pillar, title, mechanism, periods, severity, evidence_trail}` (the `Anomaly` model
   without `reference_sql`), and `reference_solution` as `{sql?, python?, memo_bullets?, design_mermaid?}`.
7. **Python `result_json` convention.** The UI tells users to assign to a variable named `result`.
   The sandbox should follow the same convention.
8. **Stream errors.** The UI accepts `data: {"error": "..."}` mid-stream and shows it.
9. **Anthropic sampling params.** Current Claude models reject `temperature`. For
   `provider == "anthropic"`, the backend should drop `temperature_fast/deep` from requests.
10. **Station/Cohort fields.** These are not spelled out in §4. `types.ts` assumes the fields named in the
    inline comments.
