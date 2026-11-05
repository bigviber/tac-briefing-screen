# Adding an archetype

1. Create `axiom/engine/archetypes/<key>.py` (the key must be listed in `ARCHETYPE_MODULES` in `__init__.py`,
   all 14 spec keys already are) with a class decorated `@register` that subclasses `Archetype` and sets
   `key, title, pillars, industries, schema_template, summary, root_cause_terms` (terms that must appear in
   `DossierSeed.conceal`). `skill_nodes` come from `skill_dag.json -> archetype_nodes` automatically.
2. Implement the hooks; `Archetype.build()` calls them in this order:
   - `sample_params(rng, pool, difficulty, industry)` -> dict with `"industry"` and `"difficulty"`. Use
     `benchmark_keys.sample_between(pool, rng.spawn("bench:x"), "<ind>.dso", ind, 0.25, 0.5)` for spec draws
     and `magnitude(rng, difficulty, lo, hi)` for `D[p] * U(lo, hi)`. Size volumes from `ROW_BUDGET[difficulty]`.
   - `build_baseline(ctx, state)` -> healthy plan in `state.frames` / `state.extras` (vectorised numpy/pandas).
   - `inject_anomalies(ctx, state)` -> modify the plan, return `Anomaly` stubs (ids `A1..`).
   - `post_ledger(ctx, state)` -> post to `state.ledger` with the `Ledger` family helpers. Batch recurring
     rows with `batch_end_dates(dates, GL_BATCH[difficulty])` (batches never cross a month end). The books
     start at `state.calendar.opening_date` (`open_books`) and include a warm-up quarter P0.
   - `build_tables(ctx, state, builder)` -> `builder.add_frame(name, df, dirt=[...], column_types=...)`; GL tables
     are added by the base. Dirt rules come from common section 9 (2 / 4 / 6 rules); never point value-changing
     rules at GL-tied money columns. Keep money as `*_cents` BIGINT.
   - `subledger_ties(ctx, state)` -> `SubledgerTie`s computed from the clean frames; declare designed breaks with
     `expected_break_cents={period: amount}` so the invariant verifies the exact break.
   - `build_operations`, `build_ground_truth(ctx, state, con)` (metrics computed from the data via `metric(...)`,
     each with `pillar`; reference SQL via `run_reference_query(con, name, sql, signature=[...], metric_keys=[...],
     checks=fn)` - it runs on the dirty DuckDB, so dedupe/trim in the SQL), `build_dossier_seed`, `levers`,
     `simulation_model` (`{"kpis", "lever_effects", "anomaly_drag", "resolved_if"}` plus optional `bounds` / `shocks`;
     each effect sets exactly one of `elasticity` / `delta_per_unit`, the other key omitted). A `resolved_if` value is a
     number (`>=`) or `at_most(x)` (`{"op": "<=", "value": x}`) when the root-cause fix is a reduction; all conditions
     must hold (Module 3 has no OR-groups); the status quo must not resolve and the lever ranges must allow it. The base
     adds name-based default `bounds` (amounts / counts / days >= 0, rates and utilizations in [0, 1], margins in
     [-1, 1]; earnings, flows and cash stay signed); archetype bounds win.
3. Use one child stream per concern (`ctx.rng.spawn("baseline")`, ...). No wall clock, no unseeded randomness,
   no Python `hash()`.
4. Verify: `python -m axiom.engine.procedural_world verify --archetype <key> --seed 7` (byte-identical
   `world.json`, invariant report), `... inspect <case_id>`, then `python -m pytest -q tests/test_archetypes.py`
   (the suite parametrises over the registry automatically).

Conventions to rely on: integer cents everywhere (`cents()` rounds half away from zero, `split_cents` gives the
residual to the last bucket); no closing entries (IS accounts accumulate, BS shows current earnings in retained
earnings); CFS is derived from every non-cash BS account (`CFS_MAP`), so new event families need no CFS code;
`DatabaseSpec.path` is logical (`cases/<case_id>/company.duckdb`), resolve with `procedural_world.resolve_db_path`.

## Realism defaults from the base (review 2026-11)

- **Company profile** (`base.company_profile`): stage from `industry.stage_mix` (restrict with the class attribute
  `stage_options`; > $5M revenue is never `seed`), FYE from {12-31: 0.7, 06-30 / 09-30 / 03-31: 0.1 each} (statements
  stay on calendar quarters), founding year from the stage's age band bounded by `founded_floor(business_model)`,
  headcount = TTM revenue / revenue per employee (SaaS from `saas.headcount_per_1m_arr`, others from
  `REVENUE_PER_EMPLOYEE_USD`; floor 8). Descriptions use `cap_first`, never `str.capitalize` (it lower-cases "B2B").
  The company's name stem is excluded from every `rng.company_name` draw during the build (archetypes with their own
  name pools check `rng.excluded_name_stems()`).
- **Working capital** (`self.enable_standard_working_capital(ctx, state, vendor_ap=..., prepaid=True)` in
  `build_baseline`, before posting): the monthly `pay_ap(payable_account="2100")` settlement is split into vendor AP
  (30-45%, paid on `fin.<ind>.dpo_days` terms), a prepaid draw-down and cash (`finance.WorkingCapitalPolicy`). Do not
  enable `vendor_ap` when an AP sub-ledger ties to 2000. Archetypes that re-create the ledger use
  `self.fresh_ledger(state)` so the policy carries over. Revenue billed in advance but credited to revenue on
  invoicing gets quarter-end auto-reversing deferrals from `post_deferred_revenue_trueups` (source
  `revenue_deferral`); billing-basis revenue ties declare `exclude_sources=(DEFERRAL_SOURCE,)`.
- **Balance-sheet completeness** (required invariant `bs_caption_present[...]`): every `line_items.json` balance-sheet
  caption of the industry with p50 > 2% of assets must be non-zero in some period, unless `BS_CAPTION_RULES` exempts
  it generically (cash, totals, goodwill, leases, debt, lending books, customer deposits) or the archetype declares
  `bs_caption_exemptions = {"[industry:]regex": "reason"}`.
- **Noise** (`self.period_noise(ctx, state)`): seasonal profile by industry x idiosyncratic quarterly noise (`vol(d)`,
  `gm(d)` additive GM shift, `ox(d)` opex multiplier, `bookings(d)`); one `noise` stream per world.
- **Principal dirt**: after the ground truth is built, `add_principal_dirt` tops transactional tables up to 3 rules
  (`unit_mixed`, `orphan_fk`, case / whitespace / nulls) on columns no ground-truth query, reference SQL or reference
  Python names, rewrites the DB and replays every ground-truth query; a table whose extra dirt changed any result
  loses it again.

## Notes and gotchas

- **Row budget per difficulty.** Size volumes from `datagen.ROW_BUDGET` (target: foundation 10,000, practitioner
  25,000, principal 50,000 rows) and `row_scale(difficulty)` (0.2 / 0.5 / 1.0). `TableBuilder.check_budget` enforces
  `ROW_HARD_CAP` (13,000 / 32,000 / 50,000) over **every** materialised table, including `gl_journal`, `gl_lines`,
  `gl_accounts` and the rows `dup_keys` dirt adds; `period_calendar` counts too. GL tables grow with `GL_BATCH`
  (weekly / weekly / daily), so per-document GL lines (e.g. one AR line per invoice) must come out of the budget.
- **`tz_mixed` writes whole-second text.** The rule renders the column as `VARCHAR` `YYYY-MM-DD HH:MM:SS+HH:MM`
  (about half `+00:00`, the rest shifted to a local offset): sub-second precision is dropped. Generate the clean
  timestamps at whole seconds (or floor them before computing expectations) so the clean frame and the dirty DB
  agree; reference SQL normalises with `CAST(col AS TIMESTAMPTZ) AT TIME ZONE 'UTC'` (needs `pytz` in the Python
  environment for DuckDB to return TIMESTAMPTZ values).
- **`gl_lines` dimensions.** `Ledger.entries_frame` emits typed `dimension_customer_id`, `dimension_product_id`,
  `dimension_department`, `dimension_invoice_id` and `dimension_sku` (NULL when absent) plus `dimensions`
  (`key=value;...`, sorted). Prefer the typed columns in reference SQL; other ids (payment, memo, batch) are only in
  `dimensions` (`regexp_extract(dimensions, 'payment_id=([^;]+)', 1)`). Omit empty dimension values rather than
  writing `""`.
- **Simulation `lever_effects`: one key per effect.** Each effect sets exactly one of `elasticity` /
  `delta_per_unit` and **omits** the other key (never `null`): the quarter simulator branches on
  `"elasticity" in effect`, and `validate_simulation_model` rejects effects with both or neither. `lag_periods` is
  an `int`; `lever` / `kpi` must exist in the levers and `kpis`.
- **`sbg_fit`** (`operations.py`) is bounded (`alpha, beta` in `[1e-3, 1e3]`, L-BFGS-B in log space) and warning
  free; near-geometric data returns `geometric_limit=True` with `alpha + beta = 1e3` and the geometric MLE as
  `theta_mean`. No local `warnings` / `np.errstate` wrapper is needed.
- **Testing one archetype.** `tests/test_archetypes.py` generates each `(archetype, difficulty, seed)` lazily and
  caches it per module, so a broken archetype only fails its own tests. Restrict the run with
  `AXIOM_TEST_ARCHETYPES="a,b"` (comma-separated registry keys; unknown keys are a usage error), e.g.
  `AXIOM_TEST_ARCHETYPES=working_capital_crunch python -m pytest -q tests/test_archetypes.py`. The module turns
  NumPy's generic-unit timedelta `DeprecationWarning` into an error: add explicit units (`pd.to_timedelta(x, unit="D")`,
  `np.timedelta64(n, "h")`), never bare integers, to datetime64 values.
