-- World Model DB Schema
-- 4 tables: locations, observations, sessions, models
-- See: docs/WorldModel设计.md §七

-- ── Episodic Memory ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS locations (
    id          TEXT PRIMARY KEY,          -- domain::pattern
    run_id      TEXT,
    domain      TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    how_to_reach TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS observations (
    id          SERIAL PRIMARY KEY,
    location_id TEXT REFERENCES locations(id) ON DELETE CASCADE,
    run_id      TEXT,                          -- which run wrote this
    agent_step  INT,
    raw         JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Execution Log ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT,
    direction           TEXT,                     -- briefing direction for this session
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    ended_at            TIMESTAMPTZ,
    outcome             TEXT,                     -- natural_stop / context_exhausted / consecutive_errors / safety_net
    steps_taken         INT,
    trajectory_summary  TEXT
);

-- ── Semantic & Procedural Models ────────────────────────

CREATE TABLE IF NOT EXISTS models (
    domain      TEXT NOT NULL,
    model_type  TEXT NOT NULL,             -- 'semantic' or 'procedural'
    run_id      TEXT NOT NULL,             -- per-run model snapshot
    content     TEXT NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (domain, model_type, run_id)
);

-- ── Indexes ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_locations_domain ON locations(domain);
CREATE INDEX IF NOT EXISTS idx_observations_location ON observations(location_id);
CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_sessions_run ON sessions(run_id);
CREATE INDEX IF NOT EXISTS idx_models_domain ON models(domain);
