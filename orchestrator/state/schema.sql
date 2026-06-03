-- orclaw-engine SQLite schema v1
-- Initialise with: sqlite3 engine.db < schema.sql

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- =====================================================
-- batches: layered partition of issues to implement
-- =====================================================
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL DEFAULT '',               -- 'owner/name'; '' = legacy/single-repo
  layer INTEGER NOT NULL,
  issue_number INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'in_progress', 'merged', 'failed', 'skipped')),
  implementer_run_id TEXT,
  pr_number INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_batches_layer_status ON batches(layer, status);
CREATE INDEX IF NOT EXISTS idx_batches_repo_status ON batches(repo, status);
-- Uniqueness is per (repo, issue_number): two repos may each have issue #1.
CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_repo_issue_active
  ON batches(repo, issue_number) WHERE status != 'failed' AND status != 'skipped';

CREATE TRIGGER IF NOT EXISTS batches_updated_at AFTER UPDATE ON batches
BEGIN
  UPDATE batches SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- =====================================================
-- runs: implementer/reviewer agent invocations
-- =====================================================
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,                        -- uuid
  repo TEXT NOT NULL DEFAULT '',              -- 'owner/name'; '' = legacy/single-repo
  agent TEXT NOT NULL                         -- 'implementer' | 'reviewer' | 'specialist'
    CHECK (agent IN ('implementer', 'reviewer', 'specialist', 'batch_planner')),
  issue_number INTEGER,                       -- nullable for specialist
  pr_number INTEGER,                          -- nullable
  branch TEXT,                                -- branch used
  model TEXT NOT NULL,                        -- 'claude-sonnet-4-6', etc.
  status TEXT NOT NULL                        -- see RunStatus in orclaw_engine/models.py
    CHECK (status IN ('queued', 'running', 'success', 'failed', 'timeout', 'aborted', 'rate_limited')),
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  duration_seconds INTEGER,
  exit_code INTEGER,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_agent_started ON runs(agent, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_issue ON runs(issue_number);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo);

-- =====================================================
-- token_ledger: per-API-call accounting (see docs/token-budget.md)
-- =====================================================
CREATE TABLE IF NOT EXISTS token_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id),
  agent TEXT NOT NULL,
  model TEXT NOT NULL,
  issue_number INTEGER,
  pr_number INTEGER,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  cost_usd_cents INTEGER NOT NULL,            -- integer cents, rounded
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('success', 'error', 'timeout', 'rate_limited'))
);

CREATE INDEX IF NOT EXISTS idx_ledger_started ON token_ledger(started_at);
CREATE INDEX IF NOT EXISTS idx_ledger_agent ON token_ledger(agent, started_at);
CREATE INDEX IF NOT EXISTS idx_ledger_issue ON token_ledger(issue_number);

-- =====================================================
-- reviews: reviewer agent outputs (per PR)
-- =====================================================
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL DEFAULT '',               -- 'owner/name'; '' = legacy/single-repo
  pr_number INTEGER NOT NULL,
  run_id TEXT REFERENCES runs(id),
  verdict TEXT NOT NULL                       -- 'approved' | 'minor_fixes_applied' | 'needs_changes' | 'hard_block'
    CHECK (verdict IN ('approved', 'minor_fixes_applied', 'needs_changes', 'hard_block')),
  blocker_count INTEGER DEFAULT 0,
  major_count INTEGER DEFAULT 0,
  minor_count INTEGER DEFAULT 0,
  fixes_applied INTEGER DEFAULT 0,
  duration_ms INTEGER,
  tokens_used INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reviews_pr ON reviews(pr_number);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at);

-- =====================================================
-- specialist_sessions: conversational state of the CEO/CTO interface
-- =====================================================
CREATE TABLE IF NOT EXISTS specialist_sessions (
  id TEXT PRIMARY KEY,                        -- uuid
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at TEXT,
  user_messages INTEGER DEFAULT 0,
  issues_created TEXT,                        -- JSON array of issue numbers
  notes TEXT
);

-- =====================================================
-- engine_state: misc key/value state (pause flags, cursors)
-- =====================================================
CREATE TABLE IF NOT EXISTS engine_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Initial values
INSERT OR IGNORE INTO engine_state (key, value) VALUES
  ('orchestrator_paused', 'false'),
  ('budget_paused', 'false'),
  ('last_planner_run', '1970-01-01T00:00:00Z');

-- =====================================================
-- monthly_summary: aggregated monthly snapshots
-- =====================================================
CREATE TABLE IF NOT EXISTS monthly_summary (
  yyyymm TEXT PRIMARY KEY,                    -- '2026-05'
  total_cost_usd_cents INTEGER NOT NULL,
  total_runs INTEGER NOT NULL,
  successful_runs INTEGER NOT NULL,
  failed_runs INTEGER NOT NULL,
  issues_closed INTEGER NOT NULL,
  prs_merged INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =====================================================
-- events: structlog tap for in-process incident analysis
--
-- Every log call routed through structlog can optionally be persisted
-- here (opt-in via ORCLAW_LOG_EVENTS_DB env var). The specialist agent
-- queries this table via query_events(); the daily summary samples it
-- for the incident section.
-- =====================================================
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  level TEXT NOT NULL                         -- 'debug' | 'info' | 'warning' | 'error' | 'critical'
    CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
  event TEXT NOT NULL,                        -- structlog event name, e.g. 'implementer_dispatch_completed'
  module TEXT,                                -- python logger name (orclaw_engine.orchestrator.loop, ...)
  attrs TEXT NOT NULL DEFAULT '{}'            -- JSON blob of structured kwargs
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_level_ts ON events(level, ts);
CREATE INDEX IF NOT EXISTS idx_events_event_ts ON events(event, ts);
