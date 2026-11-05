-- Axiom app DB (SQLite). See docs/ARCHITECTURE.md section 3.
-- All statements are idempotent so init_schema() can run on every startup.

CREATE TABLE IF NOT EXISTS skill_nodes (
    id           TEXT PRIMARY KEY,
    pillar       TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    json         TEXT NOT NULL              -- full SkillNode (BKT params, prereqs)
);

CREATE TABLE IF NOT EXISTS skill_edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);

CREATE TABLE IF NOT EXISTS mastery (
    node_id   TEXT PRIMARY KEY,
    p_known   REAL NOT NULL,
    attempts  INTEGER NOT NULL DEFAULT 0,
    correct   INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS cases (
    case_id          TEXT PRIMARY KEY,
    seed             INTEGER NOT NULL,
    archetype        TEXT NOT NULL,
    difficulty       TEXT NOT NULL,
    company_name     TEXT NOT NULL,
    industry         TEXT NOT NULL,
    pillars_json     TEXT NOT NULL DEFAULT '[]',
    skill_nodes_json TEXT NOT NULL DEFAULT '[]',
    world_path       TEXT NOT NULL,
    db_path          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'generated',
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_created ON cases (created_at);

CREATE TABLE IF NOT EXISTS case_artifacts (
    case_id    TEXT NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,              -- "dossier" | "textbook" | ...
    key        TEXT NOT NULL,              -- "" for singletons, skill node id for chapters
    json       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (case_id, kind, key)
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id  TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    submitted_json TEXT NOT NULL,
    grade_json     TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_submissions_case ON submissions (case_id);

CREATE TABLE IF NOT EXISTS adversary_turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL REFERENCES submissions (submission_id) ON DELETE CASCADE,
    turn_index    INTEGER NOT NULL,
    role          TEXT NOT NULL,           -- "user" | "adversary" | "system"
    json          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_submission ON adversary_turns (submission_id, turn_index);

CREATE TABLE IF NOT EXISTS sim_quarters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id        TEXT NOT NULL REFERENCES cases (case_id) ON DELETE CASCADE,
    submission_id  TEXT,
    period         TEXT NOT NULL,
    decisions_json TEXT NOT NULL DEFAULT '{}',
    kpis_json      TEXT NOT NULL DEFAULT '{}',
    narrative      TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_case ON sim_quarters (case_id, id);

CREATE TABLE IF NOT EXISTS llm_settings (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    model             TEXT NOT NULL,
    purpose           TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        REAL NOT NULL DEFAULT 0,
    cost_usd_est      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_model ON llm_usage (model);
