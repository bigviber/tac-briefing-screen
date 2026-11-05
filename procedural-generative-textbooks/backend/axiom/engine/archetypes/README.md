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
     `simulation_model` (`{"kpis", "lever_effects", "anomaly_drag", "resolved_if"}`; each effect sets exactly one
     of `elasticity` / `delta_per_unit`, the other key omitted; `resolved_if` values are floats meaning `>=`).
3. Use one child stream per concern (`ctx.rng.spawn("baseline")`, ...). No wall clock, no unseeded randomness,
   no Python `hash()`.
4. Verify: `python -m axiom.engine.procedural_world verify --archetype <key> --seed 7` (byte-identical
   `world.json`, invariant report), `... inspect <case_id>`, then `python -m pytest -q tests/test_archetypes.py`
   (the suite parametrises over the registry automatically).

Conventions to rely on: integer cents everywhere (`cents()` rounds half away from zero, `split_cents` gives the
residual to the last bucket); no closing entries (IS accounts accumulate, BS shows current earnings in retained
earnings); CFS is derived from every non-cash BS account (`CFS_MAP`), so new event families need no CFS code;
`DatabaseSpec.path` is logical (`cases/<case_id>/company.duckdb`), resolve with `procedural_world.resolve_db_path`.
