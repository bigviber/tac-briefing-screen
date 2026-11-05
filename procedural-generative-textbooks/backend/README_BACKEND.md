# Axiom backend

Python 3.12+ (developed on 3.13). The binding interface contract is `../docs/ARCHITECTURE.md`.

## Install

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # or: pip install -e ".[dev]"
```

## Run

```bash
cd backend
./run.sh                      # creates/uses .venv, installs requirements, uvicorn on http://127.0.0.1:8000
AXIOM_PORT=8010 ./run.sh      # other port
AXIOM_RELOAD=1 ./run.sh       # auto-reload while developing
python main.py                # same app without the venv bootstrap (honours AXIOM_PORT)
```

OpenAPI docs are at `/docs`. When `../frontend/dist` exists (run `npm run build` in `frontend/`),
the built app is served at `/` with an SPA fallback, so one process serves everything. For
frontend development run `npm run dev` (port 5173, proxies `/api` to 8000).

Configure the LLM in the app (Settings drawer) or with `PUT /api/v1/settings/llm`. The key is
stored only in `data/axiom.db`, never logged, and every response masks it (`****1234`). A `PUT`
with an empty or masked `api_key` keeps the stored key. Changes apply on the next request.

## Configuration

All settings come from `AXIOM_*` environment variables (see `axiom/config.py`):

| Variable | Default |
|---|---|
| `AXIOM_DATA_DIR` | `backend/data` (holds `axiom.db`, `cases/`, `research_cache/`) |
| `AXIOM_BENCHMARKS_DIR` | `axiom/engine/benchmarks` |
| `AXIOM_SANDBOX_TIMEOUT_S` / `AXIOM_SANDBOX_MEMORY_MB` | `20` / `512` |
| `AXIOM_SQL_ROW_LIMIT` | `500` |
| `AXIOM_CORS_ORIGINS` | `http://localhost:5173` (comma-separated or JSON list) |
| `AXIOM_SEC_USER_AGENT` | `Axiom local research tool contact@example.com` |
| `AXIOM_PORT` | `8000` (`main.py` / `run.sh` only) |
| `AXIOM_LOG_LEVEL` | `INFO` (`main.py` only) |

## Tests

```bash
cd backend
python -m pytest -q
```

Tests use the mini corpus in `tests/fixtures/benchmarks/` and never touch the network
(the EDGAR client is exercised through `httpx.MockTransport`).

## Research-tier CLI

```bash
python -m axiom.engine.market_research report                       # coverage + citations (JSON)
python -m axiom.engine.market_research sample saas.gross_churn_monthly --seed 7 -n 5
python -m axiom.engine.market_research refresh --year 2024          # SEC EDGAR XBRL frames, in memory only
python -m axiom.engine.market_research --dir tests/fixtures/benchmarks report
```

Add `-v` to see loader warnings (missing files, skipped invalid records). `refresh` caches
frames under `data/research_cache/`, stays under 5 requests/s, and never rewrites the
corpus JSON files. If the network fails, the corpus values stay in effect.

## HTTP API (`/api/v1`)

Code: `main.py` (app factory, CORS, lifespan, health, SPA), `axiom/services.py` (`AppServices`,
`WorldService`), `axiom/api/routes_*.py`, `axiom/api/schemas.py` (section 12 response views),
`axiom/api/public_view.py` (leak-free case projection and asks), `axiom/api/streaming.py` (SSE
bridge), `axiom/api/errors.py` (status mapping).

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | | `{status, version, llm_configured, archetypes_available}` (*) |
| GET | `/archetypes` | | `[ArchetypeInfo]` (`[]` while Module 2 is absent) |
| POST | `/cases/generate` | `{archetype?, seed?, difficulty?, target_skills?}` | `CasePublic` + `asks`, `levers`, `status` |
| GET | `/cases` | | `[CaseSummary]` newest first |
| GET | `/cases/{id}` | | `CasePublic` + `asks`, `levers`, `status` |
| GET | `/cases/{id}/schema` | | `DatabaseSpec` (sample-row timestamps ISO 8601, like `/sandbox/sql`) |
| GET | `/cases/{id}/world-stats` | | `{tables, rows, periods, invariants_passed, invariants_total, generation_ms}` |
| POST | `/cases/{id}/dossier[?stream=1]` | `{force?}` (optional) | `Dossier` (cached) |
| POST | `/cases/{id}/textbook[?stream=1]` | `{skill_node}` | `TextbookChapter` (cached; 409 while locked) |
| GET | `/cases/{id}/session` | | `{submission_id, grade, adversary_turns, sim_history, thread, status}` |
| POST | `/cases/{id}/submit` | `Submission` | `GradeReport` (`revealed.anomalies` public fields only) |
| POST | `/cases/{id}/adversary[?stream=1]` | `{submission_id, message}` | `AdversaryTurn` |
| POST | `/cases/{id}/advance` | `{submission_id, decisions}` | `SimQuarter` (simulation written back to `world.json`) |
| POST | `/sandbox/sql` | `{case_id, sql, limit?=500}` | `{columns, column_types, rows, row_count, elapsed_ms, truncated, error}` |
| POST | `/sandbox/python` | `{case_id, code, timeout_s?=20}` | `{stdout, stderr, result_json, figures, elapsed_ms, error}` |
| GET | `/skills/graph` | | `{nodes: [{id, pillar, title, mastery, prereqs, attempts, summary, tier}], edges: [{src, dst, source, target}], pillars}` |
| POST | `/skills/reset` | | `{ok}` |
| GET / PUT | `/settings/llm` | `LLMSettings` (PUT) | masked: `api_key` = `api_key_masked`, `api_key_set`, `configured` |
| POST | `/settings/llm/test` | | `{ok, model, latency_ms, error}` |
| GET | `/settings/usage` | | `{by_model, total_tokens, total_cost_usd_est}` |
| GET | `/settings/prompts` | | `[{name, template}]` |

(*) `archetypes_available` is served by the `/health` handler in `axiom/api/routes_archetypes.py`;
`main.py` still registers its own inline `/health` first, which must be removed for the new field
to appear.

Errors are `{"detail": "..."}`: 400 sandbox rejected the statement/code, 404 unknown case,
submission or skill node, 409 chapter locked / quarter budget exhausted / submission not graded /
unknown decision lever, 428 LLM not configured (open Settings), 502 provider error (provider
message passed through, keys scrubbed), 503 Module 2 not installed. `/adversary` and `/advance`
validate the case, the submission and its state before building the LLM client, so 404/409 are
never masked by 428.

Public case view (`GET /cases/{id}`, `POST /cases/generate`): besides dropping `ground_truth` and
`dossier_seed.conceal`, stakeholders carry only `name`, `role`, `agenda`, `disposition` and the
`knows` items that do not touch a concealed fact (`hides` is never sent); reveal/timeline items
quoting a concealed phrase, an anomaly id or a 5-word root-cause fragment are dropped;
`simulation.model` is reduced to its starting `kpis`; statement ratios / `kpis_by_period` rows
whose value equals an expected metric are removed. `asks[].hint` is `ExpectedMetric.hint`, else
its `label`, else a sentence from the key (`dso_2024q4` -> "Days sales outstanding for 2024Q4");
values and derivations are never included.

Streaming (`?stream=1`): `text/event-stream` with `data: {"delta": "..."}` chunks, then
`data: {"done": true, "payload": {...}}`; a failure after the stream started is
`data: {"error": "..."}` and the stream closes. 404/409/428 that occur before the first chunk
are returned as normal HTTP errors. The adversary is not token-streamed by the pedagogy layer,
so its final message is replayed as deltas. When the client disconnects, the next `on_delta`
call in the worker raises `StreamCancelled`, which aborts the upstream LLM stream.

All blocking work (LLM calls, generation, sandboxes) runs in the threadpool; the event loop is
never blocked. The LLM client is rebuilt from the stored settings for every request.

## Sandboxes (`axiom/engine/sandbox.py`)

- **SQL**: one statement starting with `SELECT`, `WITH`, `EXPLAIN`, `DESCRIBE`, `SHOW` or
  `PRAGMA table_info`; no `;` except trailing; write/DDL/`ATTACH`/`COPY`/`INSTALL`/`LOAD`/`SET`...
  keywords rejected (string literals and comments are ignored by the check). DuckDB is opened
  `read_only` with external file access disabled and the config locked. `SELECT`/`WITH` are
  wrapped as `SELECT * FROM (<sql>) LIMIT limit+1`, so `truncated` is exact. A watchdog thread
  calls `con.interrupt()` after `AXIOM_SANDBOX_TIMEOUT_S`.
- **Python**: `sys.executable -I -B` subprocess in a fresh temp working dir, minimal env,
  `RLIMIT_AS` = interpreter/library baseline + `AXIOM_SANDBOX_MEMORY_MB`, `RLIMIT_CPU`,
  `RLIMIT_FSIZE` 64 MB, sockets disabled, matplotlib `Agg`. Pre-bound names: `pd`, `np`,
  `duckdb`, `scipy`, `con` (read-only case DB), `tables` (lazy `dict` of DataFrames, loaded on
  first access), `CASE_DIR`. A top-level `result` becomes `result_json` (DataFrames as
  `{columns, rows<=500, row_count, truncated}`); open figures become base64 PNGs. Output is
  capped at 20 kB per stream; the process group is killed on timeout. A `MemoryError` is
  reported with the memory budget.
- **Python hardening** (prelude before user code): process creation is blocked, not just
  discouraged: `os.system`/`os.popen`/`os.exec*`/`os.spawn*`/`os.posix_spawn*`/`os.fork` and
  `_posixsubprocess.fork_exec` are replaced with raisers, and a `sys.addaudithook` hook (which
  Python code cannot remove) raises `PermissionError` on `subprocess.Popen`, `os.exec`,
  `os.spawn`, `os.fork` and signals to other processes. `subprocess` stays importable because
  matplotlib imports it. The same hook makes the case folder and the app data dir read-only
  (write-mode `open`, `os.remove`/`rename`/`mkdir`/`chmod`/..., `shutil.rmtree`);
  `duckdb.connect` reopens the case DB read-only and gives new connections no file access, so
  `ATTACH` cannot write it either. The child environment is a fixed allowlist (no API keys, no
  `AXIOM_*`). This is a guard rail for a local single-user tool, not a hostile-code jail.
