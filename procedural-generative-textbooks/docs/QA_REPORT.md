# Axiom QA report: live end-to-end verification

Date: 2026-11-05. Backend `python main.py` on :8000 with `AXIOM_DATA_DIR=<scratchpad>/qa_data` (isolated). Frontend `npm run dev` on :5173 (live API, no mock). No LLM configured, except during step 6. Scripts used: `qa_api.py`, `qa_submit.py`, `qa_ui.js` and `qa_ui2.js` (they are in the session scratchpad and are not committed).

The backend ran for the whole session with no crash and no restart. The concurrent backend edits (`archetypes/window_reconciliation.py`, `datagen.py`, `finance.py`, `operations.py`) did not cause an import error.

## Summary

| Area | Result |
|---|---|
| 1. Health and catalog | PASS. `archetypes_available == 14`. `/archetypes` returns 14, each with pillars and skill nodes. |
| 2. Generation, 14 × 3 at seed 42 | PASS 42/42. Slowest 5.0 s, so none exceeded 15 s. Every invariant passed. A = L + E holds in all 8 periods of every case. There is no `ground_truth`, `hides` or `conceal` anywhere in the public payloads. |
| 3. Sandboxes | PASS with one deviation from the spec. SQL, Python (one valid PNG each time), DDL rejection and the timeout all work. A bad query returns **HTTP 200 with `error`**, not the 400 the spec asks for. That is the documented contract. |
| 4. Session and submit | PASS. A fresh session is empty. Submit returns 428 and the status stays `generated`. Dossier and textbook (plain and `?stream=1`) also return 428. |
| 5. Skill graph and auto-pick | PASS. 65 nodes, all with `summary`, `tier` and `mastery`. Three auto cases were all in the catalog: `pricing_elasticity`, `monte_carlo_cash`, `monte_carlo_cash`. No rationale field is exposed. |
| 6. Settings | PASS. The key is masked as `****1234`. Test returns `ok=false` with a provider error and does not leak the key. 5 prompts are listed. Usage works. |
| 7. Frontend | **FAIL, 1 defect.** The replica_lag Architecture diagram does not render (mermaid parse error). A sweep of all 42 cases found **6 of 9 system-design diagrams broken**. Every other part of the UI flow works. |
| 8. Determinism through the API | **FAIL.** The two `world.json` files differ only in `generated_at`. The API write path does not keep the engine's canonical bytes. |

## Failures (exact request and response)

### F1. Architecture mermaid fails to parse for `replica_lag` (all difficulties) and `idempotent_webhooks` (all difficulties). Severity: high, user-visible.

UI: Briefing → Architecture shows a red parse-error box and no diagram (`docs/screenshots/qa-replica-lag-architecture.png`).

Request: `GET /api/v1/cases/replica_lag-42-practitioner-7fc6db44` returns `dossier_seed.architecture_mermaid`, which begins:
```
flowchart LR
  B[Buyer] --> CDN[Availability cache TTL n/a (browse pages only)]
```
Mermaid 11 parses this as:
```
Parse error on line 2:
...ility cache TTL n/a (browse pages only)]
-----------------------^
Expecting 'SQE', 'DOUBLECIRCLEEND', 'PE', '-)', 'STADIUMEND', 'SUBROUTINEEND', 'PIPE', 'CYLINDEREND', 'DIAMOND_STOP', 'TAGEND', 'TRAPEND', 'INVTRAPEND', 'UNICODE_TEXT', 'TEXT', 'TAGSTART', got 'PS'
```
The cause is unquoted parentheses inside `[...]` node labels:

| Case(s) | Offending label | Source |
|---|---|---|
| replica_lag foundation and practitioner | `CDN[Availability cache TTL n/a (browse pages only)]` | `backend/axiom/engine/archetypes/replica_lag.py:1271` (`ttl = ... "n/a (browse pages only)"`) |
| replica_lag principal | `AN[Nightly Analytics Job (from P7)]` | `replica_lag.py`, the analytics node label |
| idempotent_webhooks, all 3 | `F[Finance Recon job (monthly)]` | `backend/axiom/engine/archetypes/idempotent_webhooks.py:103` |

`outbox_pattern` (3/3) renders. So do the optional diagrams for `toc_bottleneck` and `bullwhip`. Fix: quote the labels (`CDN["..."]`), or drop the parentheses. A backend test that runs each `architecture_mermaid` through a mermaid parser would catch this.

### F2. `world.json` is not byte-identical across regenerations through the API. Severity: medium (breaks the documented contract).

Steps: `POST /api/v1/cases/generate {"archetype":"bullwhip","seed":99,"difficulty":"practitioner"}` returned `bullwhip-99-practitioner-84f865ad`. A second call returned the same id. Deleting `qa_data/cases/bullwhip-99-practitioner-84f865ad/` and regenerating gave HTTP 200.

Result: both files are 483,292 bytes, with sha256 `e927b9ed…5979` and `596011fb…b02`. They are **not equal**. The only differing key is `generated_at` (`2026-11-05T04:43:46+00:00` vs `2026-11-05T04:43:48+00:00`). Everything else is identical, so the engine itself is deterministic.

Cause: `procedural_world.generate_world` writes canonical bytes (`world_json_bytes`, which blanks `generated_at` and uses `indent=1`). The API then overwrites that file: `WorldService._place_files` → `_write_world` (`backend/axiom/services.py:252-257`) dumps the world with `generated_at` stamped and `indent=None`. The README says "Same seed, byte-identical world" and the module docstring says `world.json` has `generated_at=""`. Neither is true for cases made through the API.

### F3 (spec deviation, not a defect). A bad SQL query returns 200, not 400.

Request: `POST /api/v1/sandbox/sql {"case_id":"<each practitioner case>","sql":"SELECT nonexistent_col FROM no_such_table"}`.
Response: HTTP 200 `{"columns":[],"rows":[],"row_count":0,"elapsed_ms":53.59,"truncated":false,"error":"CatalogException: Catalog Error: Table with name no_such_table does not exist!\nDid you mean \"pg_tables\"?\n\nLINE 2: SELECT nonexistent_col FROM no_such_table\n ^^^^^","column_types":[]}`.
The error is readable. This matches the backend and frontend READMEs ("A failing (but allowed) statement is HTTP 200 with `error`"). Only statements the allowlist rejects return 400, and `CREATE TABLE` does return 400 (see below). This was reproduced on all 14 archetypes. The QA spec should be updated, not the code.

### Observations (no action required)
- **Timeout:** `SELECT * FROM range(100000000) a, range(100000000) b` does not time out. The sandbox wraps it as `SELECT * FROM (...) LIMIT 501`, so it streams 501 rows back in 0.89 s. The forcing variant `SELECT count(*) FROM range(100000000) a, range(100000000) b` returned 200 with `"error":"query timed out after 20s and was interrupted"` after 20.1 s wall time, which is under the 25 s limit.
- **Submit schema:** `Submission` requires `case_id` in the body even though it is also in the path. My first attempt (`memo`/`answers` fields, no `case_id`) got 422, which was my mistake. The correct body (`case_id`, `memo_markdown`, `metrics`) gets 428.
- **LLM settings cannot be cleared through the API.** A `PUT` with an empty key keeps the stored key, so once a key is saved there is no way back to "unconfigured" except editing the DB. I deleted the `llm_settings` row in the QA DB after step 6 so that the 428 checks in steps 4 and 7 were valid.
- **Auto-pick:** 2 of 3 auto cases chose `monte_carlo_cash` (seeds were random), all at practitioner. No `rationale` field is exposed in `CasePublic`, so the choice can't be verified from the API.
- **`/settings/llm/test`** error text is `LLM request failed after 3 attempts: ProxyError` (the sandbox has no egress). The key appears in neither the response nor the backend log (grep count 0).
- **Coverage gap:** `checkout_reads` in replica_lag only covers 2022Q3–2023Q3, while the reporting periods are 2021Q4–2023Q3. This looks intentional (the system goes live mid-window), but the case could state it.
- **Benchmark fallbacks:** the backend log has 3 warnings: `ecommerce.cash_pct_revenue`, `any.wacc`, `retail operations.supplier_lead_time_days`.

## 1. Health and archetypes
`GET /health` returns `{"status":"ok","version":"0.1.0","llm_configured":false,"archetypes_available":14}`. `GET /archetypes` returns 14 entries. Every entry has non-empty `pillars` and `skill_nodes` (5 nodes each) and `difficulty_range` = all three.

## 2. Generation matrix (seed 42)
"ms (wall)" is the client round trip of `POST /cases/generate`. `gen_ms`, tables, rows and invariants come from `/world-stats`. Each row also asserts the following on `GET /cases/{id}`:
- no `ground_truth`, `hides` or `conceal` key at any depth;
- `asks`, `levers`, `status` (`generated`), `periods`, `operations` and `database.erd_mermaid` are present;
- `finance.statements` has 8 periods, and "Total assets" == "Total liabilities" + "Total equity" in every period, checked from the public lines;
- for the system-design archetypes, `dossier_seed.architecture_mermaid` is present. (Presence passes. Rendering fails, see F1.)

| # | Archetype | Difficulty | ms (wall) | gen_ms | Tables | Rows | Invariants | asks/levers | Result |
|---|---|---|---:|---:|---:|---:|---|---|---|
| 1 | `working_capital_crunch` | foundation | 765 | 718 | 14 | 8,947 | 91/91 | 9/7 | PASS |
| 2 | `working_capital_crunch` | practitioner | 1,027 | 986 | 14 | 22,381 | 91/91 | 9/7 | PASS |
| 3 | `working_capital_crunch` | principal | 1,953 | 1,656 | 14 | 42,291 | 91/91 | 9/7 | PASS |
| 4 | `abc_cost_distortion` | foundation | 915 | 837 | 18 | 8,563 | 107/107 | 9/5 | PASS |
| 5 | `abc_cost_distortion` | practitioner | 655 | 610 | 18 | 23,392 | 107/107 | 9/5 | PASS |
| 6 | `abc_cost_distortion` | principal | 1,189 | 947 | 18 | 38,493 | 107/107 | 11/5 | PASS |
| 7 | `ma_synergies` | foundation | 438 | 412 | 17 | 6,370 | 83/83 | 10/5 | PASS |
| 8 | `ma_synergies` | practitioner | 559 | 525 | 17 | 13,152 | 83/83 | 10/5 | PASS |
| 9 | `ma_synergies` | principal | 855 | 773 | 17 | 45,179 | 83/83 | 11/5 | PASS |
| 10 | `forensic_revenue` | foundation | 908 | 666 | 15 | 8,657 | 99/99 | 12/5 | PASS |
| 11 | `forensic_revenue` | practitioner | 827 | 778 | 15 | 23,365 | 99/99 | 12/5 | PASS |
| 12 | `forensic_revenue` | principal | 1,581 | 1,393 | 15 | 48,679 | 99/99 | 14/5 | PASS |
| 13 | `toc_bottleneck` | foundation | 615 | 564 | 13 | 10,026 | 91/91 | 13/5 | PASS |
| 14 | `toc_bottleneck` | practitioner | 697 | 640 | 13 | 22,997 | 91/91 | 13/5 | PASS |
| 15 | `toc_bottleneck` | principal | 1,655 | 1,489 | 13 | 47,969 | 91/91 | 14/5 | PASS |
| 16 | `cohort_collapse` | foundation | 1,229 | 1,194 | 13 | 8,417 | 91/91 | 13/5 | PASS |
| 17 | `cohort_collapse` | practitioner | 2,020 | 1,991 | 13 | 21,643 | 91/91 | 13/5 | PASS |
| 18 | `cohort_collapse` | principal | 1,824 | 1,731 | 13 | 41,251 | 91/91 | 13/5 | PASS |
| 19 | `bullwhip` | foundation | 595 | 555 | 13 | 10,407 | 91/91 | 14/5 | PASS |
| 20 | `bullwhip` | practitioner | 749 | 713 | 13 | 21,993 | 91/91 | 14/5 | PASS |
| 21 | `bullwhip` | principal | 836 | 774 | 14 | 37,270 | 91/91 | 14/5 | PASS |
| 22 | `pricing_elasticity` | foundation | 616 | 569 | 12 | 9,177 | 91/91 | 13/5 | PASS |
| 23 | `pricing_elasticity` | practitioner | 1,163 | 1,123 | 12 | 23,160 | 91/91 | 14/5 | PASS |
| 24 | `pricing_elasticity` | principal | 1,519 | 1,349 | 12 | 47,980 | 91/91 | 16/5 | PASS |
| 25 | `idempotent_webhooks` | foundation | 606 | 570 | 14 | 9,552 | 91/91 | 8/6 | PASS |
| 26 | `idempotent_webhooks` | practitioner | 696 | 660 | 15 | 23,474 | 91/91 | 8/6 | PASS |
| 27 | `idempotent_webhooks` | principal | 867 | 804 | 15 | 43,466 | 91/91 | 8/6 | PASS |
| 28 | `outbox_pattern` | foundation | 478 | 451 | 17 | 8,920 | 91/91 | 8/5 | PASS |
| 29 | `outbox_pattern` | practitioner | 708 | 677 | 17 | 21,693 | 91/91 | 8/5 | PASS |
| 30 | `outbox_pattern` | principal | 1,462 | 1,422 | 19 | 40,130 | 91/91 | 10/5 | PASS |
| 31 | `replica_lag` | foundation | 732 | 694 | 20 | 9,412 | 91/91 | 9/6 | PASS |
| 32 | `replica_lag` | practitioner | 963 | 918 | 20 | 26,882 | 91/91 | 9/6 | PASS |
| 33 | `replica_lag` | principal | 1,952 | 1,822 | 21 | 45,662 | 91/91 | 11/6 | PASS |
| 34 | `window_reconciliation` | foundation | 990 | 515 | 10 | 10,588 | 91/91 | 17/4 | PASS |
| 35 | `window_reconciliation` | practitioner | 1,394 | 769 | 10 | 24,417 | 91/91 | 19/4 | PASS |
| 36 | `window_reconciliation` | principal | 2,172 | 1,902 | 11 | 45,925 | 91/91 | 20/4 | PASS |
| 37 | `cohort_matrix` | foundation | 549 | 526 | 9 | 7,673 | 67/67 | 6/4 | PASS |
| 38 | `cohort_matrix` | practitioner | 668 | 641 | 9 | 19,892 | 67/67 | 8/4 | PASS |
| 39 | `cohort_matrix` | principal | 986 | 961 | 9 | 40,007 | 67/67 | 8/4 | PASS |
| 40 | `monte_carlo_cash` | foundation | 2,059 | 2,027 | 16 | 9,137 | 83/83 | 10/5 | PASS |
| 41 | `monte_carlo_cash` | practitioner | 2,643 | 2,589 | 16 | 15,614 | 83/83 | 10/5 | PASS |
| 42 | `monte_carlo_cash` | principal | 5,046 | 4,964 | 17 | 25,940 | 83/83 | 11/5 | PASS |

## 3. Sandboxes (practitioner, seed 42)

The GROUP BY buckets by `year||'Q'||quarter` of the table's first timestamp/date column. The bad query is `SELECT nonexistent_col FROM no_such_table` and the DDL is `CREATE TABLE x AS SELECT 1`, which returns 400 `read-only sandbox: statements must start with SELECT, WITH, EXPLAIN, DESCRIBE, SHOW or PRAGMA table_info`. Python loads the two largest tables, prints their shapes, sets `result` and draws a bar chart. `result_json.rows` matched the schema row_count in all 14 cases.

| Archetype | Largest table (`LIMIT 5`) | GROUP BY table / buckets | Bad query | `CREATE TABLE` | Python stdout | `result_json` | Figures (PNG) | Py ms |
|---|---|---|---|---|---|---|---|---:|
| `working_capital_crunch` | `inventory_movements`: 200, 5 rows × 8 cols | `inventory_movements`: 200, 10 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(5680, 8) (4354, 8)` | `{"rows": 5680}` | 1 (valid PNG) | 1269 |
| `abc_cost_distortion` | `production_log`: 200, 5 rows × 14 cols | `production_log`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(13103, 14) (2614, 10)` | `{"rows": 13103}` | 1 (valid PNG) | 1269 |
| `ma_synergies` | `invoices`: 200, 5 rows × 11 cols | `invoices`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(3786, 11) (3677, 6)` | `{"rows": 3786}` | 1 (valid PNG) | 1520 |
| `forensic_revenue` | `order_items`: 200, 5 rows × 7 cols | `invoices`: 200, 10 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(5491, 7) (2957, 10)` | `{"rows": 5491}` | 1 (valid PNG) | 1269 |
| `toc_bottleneck` | `production_log`: 200, 5 rows × 15 cols | `production_log`: 200, 10 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(12721, 15) (2791, 9)` | `{"rows": 12721}` | 1 (valid PNG) | 1168 |
| `cohort_collapse` | `invoices`: 200, 5 rows × 16 cols | `invoices`: 200, 13 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(13779, 16) (1389, 7)` | `{"rows": 13779}` | 1 (valid PNG) | 1169 |
| `bullwhip` | `orders`: 200, 5 rows × 15 cols | `orders`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(9874, 15) (2574, 6)` | `{"rows": 9874}` | 1 (valid PNG) | 1440 |
| `pricing_elasticity` | `order_items`: 200, 5 rows × 11 cols | `orders`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(12113, 11) (6134, 13)` | `{"rows": 12113}` | 1 (valid PNG) | 1582 |
| `idempotent_webhooks` | `payments_ledger`: 200, 5 rows × 10 cols | `payments_ledger`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(4122, 10) (4012, 7)` | `{"rows": 4122}` | 1 (valid PNG) | 1469 |
| `outbox_pattern` | `usage_events`: 200, 5 rows × 8 cols | `usage_events`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(9708, 8) (5314, 10)` | `{"rows": 9708}` | 1 (valid PNG) | 1523 |
| `replica_lag` | `inventory_events`: 200, 5 rows × 7 cols | `inventory_events`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(5484, 7) (4905, 6)` | `{"rows": 5484}` | 1 (valid PNG) | 1272 |
| `window_reconciliation` | `gl_lines`: 200, 5 rows × 9 cols | `invoices`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(12018, 9) (5639, 15)` | `{"rows": 12018}` | 1 (valid PNG) | 1224 |
| `cohort_matrix` | `subscription_events`: 200, 5 rows × 6 cols | `subscription_events`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(8072, 6) (5604, 14)` | `{"rows": 8072}` | 1 (valid PNG) | 1321 |
| `monte_carlo_cash` | `invoices`: 200, 5 rows × 8 cols | `invoices`: 200, 9 quarter buckets | HTTP 200 + `error` (CatalogException) | HTTP 400 | `(5600, 8) (5274, 8)` | `{"rows": 5600}` | 1 (valid PNG) | 1328 |

Timeout (on `replica_lag` practitioner): the plain cross join returns 200 with 501 rows (truncated) in 0.89 s, because the LIMIT wrapper streams it. `SELECT count(*) FROM range(100000000) a, range(100000000) b` returns 200 with `error: "query timed out after 20s and was interrupted"` after **20.1 s**, which is under 25 s, so it PASSES.

## 4. Session and submit (`cohort_matrix` seed 4242 foundation, fresh)
- `GET /cases/{id}/session` returns `{"submission_id":null,"grade":null,"adversary_turns":[],"sim_history":[],"thread":[],"status":"generated"}`.
- `POST /cases/{id}/submit {"case_id":…,"memo_markdown":"QA memo: cohort collapse.","metrics":{6 asks: 1.0},"sql_evidence":[],"python_evidence":[]}` returns **428** `{"detail":"The LLM is not configured. Open Settings, choose a provider and model, and add an API key (or choose the local provider), then try again."}`.
- After the 428, `GET /cases/{id}` status is `generated`, `/cases` lists it as `generated`, and the session is still empty. The status does not get stuck at `submitted`.
- `POST /dossier`, `POST /dossier?stream=1` and `POST /textbook {"skill_node":…}` each return 428 as JSON (the stream returns 428 before its first chunk, as documented).

## 5. Skill graph
65 nodes and 73 edges. Every node has a non-empty `summary`, plus `tier` and `mastery`. Pillars: accounting_finance 14, operations_strategy 13, system_design 12, sql 13, python 13. Three `POST /cases/generate {}` calls returned `pricing_elasticity`/practitioner, `monte_carlo_cash`/practitioner and `monte_carlo_cash`/practitioner, all in the catalog. Skill nodes are returned, but there is no rationale field.

## 6. Settings
- `PUT /settings/llm {"provider":"openai","base_url":"https://api.openai.com/v1","api_key":"sk-test-1234","fast_model":"gpt-4o-mini","deep_model":"gpt-4o"}` returns 200.
- `GET` returns `api_key":"****1234"`, `api_key_masked":"****1234"`, `api_key_set":true` and `configured":true`. The raw key appears in neither response.
- `POST /settings/llm/test` returns `{"ok":false,"model":"gpt-4o-mini","latency_ms":2150.58,"error":"LLM request failed after 3 attempts: ProxyError"}`, with no key in it.
- `GET /settings/prompts` lists 5 templates: case_synthesizer, textbook_chapter, grader, adversary, quarter_narrator.
- `GET /settings/usage` returns `{"by_model":{},"total_tokens":0,"total_cost_usd_est":0.0}`.

## 7. Frontend (Playwright, Chromium, 1600×1000 @1x)
Flow: dashboard → choose `replica_lag` / practitioner / seed 42 → **Generate case**. This navigates to `#/case/replica_lag-42-practitioner-7fc6db44`. Then Architecture → Data Room ERD → SQL run → Python run → memo and 3 asks → Submit.

| Screenshot | Finding |
|---|---|
| `qa-dashboard-14-archetypes.png` | OK. The archetype select has 14 options plus "Let Axiom pick". The skill map shows 65 nodes and 73 edges in 5 pillar columns, with mastery meters and the learning frontier. The header shows "API · no LLM key". Node titles are truncated with ellipses (expected at this width). The usage strip model name is truncated (`anthropic/claude-sonnet-…`). |
| `qa-replica-lag-architecture.png` | **BROKEN DIAGRAM (F1).** The red parse-error box replaces the diagram. |
| `qa-data-room-erd.png` | OK. The ERD renders (`duckdb · 20 tables · 26.9K rows`) with typed columns, PK/FK markers and relationship labels. It renders at natural size and scrolls. Minor: on first view the top ~400 px of the pane are empty because the diagram is tall and wide, and only `products`/`order_items` are visible without scrolling. |
| `qa-sql-result.png` (extra) | OK. The GROUP BY over `checkout_reads` returns 5 rows in 38 ms. Columns show their types (varchar/bigint) and numbers are right-aligned. |
| `qa-python-figure.png` | OK. stdout `(4905, 6) (3086, 8)`, `result_json {"rows": 4905}`, "FIGURES · 1" with a histogram PNG. The x-axis is in units of 1e8 because the plotted column is `replay_bytes_behind` (bytes), which is correct for that column. Line 7 of the editor is clipped horizontally, which is normal for Monaco. |
| `qa-submit-428.png` | OK. Submit opens the Settings drawer on the LLM-provider tab, and the toast reads "Grading needs an LLM. Choose a provider and add an API key (or pick Local)." The memo is preserved underneath (25 words). |

A browser sweep over all 42 cases rendered the ERD for 42/42. Architecture rendered for outbox_pattern 3/3, toc_bottleneck 3/3 and bullwhip 3/3, and failed for replica_lag 0/3 and idempotent_webhooks 0/3. There were no `pageerror` events.

Browser console: the only errors are the two expected `Failed to load resource: 428` entries, for `POST …/dossier?stream=1` (background load) and `POST …/submit`. There were no JS exceptions.

## 8. Determinism
See F2. The engine output is identical apart from `generated_at`, but the bytes the API writes to `world.json` are not identical across runs.

## Backend log
No tracebacks, no ERROR lines, no crash or restart. HTTP status counts were 428 × 50 (dossier auto-loads in the UI plus the expected tests), 400 × 14 (DDL rejections) and 422 × 1 (my malformed submit body). There were 3 benchmark-fallback WARNINGs, 3 `ProxyError` WARNINGs from the settings test, and an INFO line for each bad-query sandbox error.
