-- TrajWeave schema v1 (Stage 0-3 data substrate).
-- Design goals: idempotent import, non-destructive project deletion, useful
-- indexes for later analysis. Raw transcripts are NOT stored here.

CREATE TABLE IF NOT EXISTS projects (
    id                TEXT PRIMARY KEY,              -- tw_proj_<sha256[:12]> of canonical root
    name              TEXT NOT NULL,
    root              TEXT NOT NULL UNIQUE,          -- canonical absolute path
    git_remote        TEXT,
    created_at        TEXT NOT NULL,                 -- ISO-8601 UTC
    last_seen_at      TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'active' -- active | missing | archived
);

CREATE INDEX IF NOT EXISTS idx_projects_root ON projects(root);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

-- One row per session file discovered on disk. Provenance + import bookkeeping.
CREATE TABLE IF NOT EXISTS source_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agent             TEXT NOT NULL,                 -- codex | claude
    source_session_id TEXT NOT NULL,
    source_path       TEXT NOT NULL,
    source_hash       TEXT,                          -- sha256 of file bytes
    source_mtime      REAL,
    size_bytes        INTEGER,
    project_id        TEXT REFERENCES projects(id) ON DELETE SET NULL,
    status            TEXT NOT NULL,                 -- imported | ignored_unregistered | failed | skipped
    detail            TEXT,                          -- error / reason, human readable
    cwd               TEXT,                          -- raw session cwd (for debugging routing)
    first_seen_at     TEXT NOT NULL,
    last_imported_at  TEXT,
    UNIQUE(agent, source_session_id, source_path)
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent_status ON source_sessions(agent, status);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON source_sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_hash ON source_sessions(source_hash);

CREATE TABLE IF NOT EXISTS trajectories (
    id                  TEXT PRIMARY KEY,            -- TW-000001
    seq                 INTEGER NOT NULL UNIQUE,     -- monotonic allocation counter
    source_session_pk   INTEGER NOT NULL UNIQUE REFERENCES source_sessions(id) ON DELETE CASCADE,
    project_id          TEXT REFERENCES projects(id) ON DELETE SET NULL,
    agent               TEXT NOT NULL,
    task                TEXT,
    task_source         TEXT NOT NULL DEFAULT 'none',
    started_at          TEXT,
    ended_at            TEXT,
    final_status        TEXT NOT NULL DEFAULT 'unknown',
    final_status_reason TEXT,
    repository_name     TEXT,
    repository_root     TEXT,
    git_branch          TEXT,
    git_commit          TEXT,
    git_remote          TEXT,
    model               TEXT,
    cli_version         TEXT,
    token_usage         TEXT,                        -- JSON blob
    event_count         INTEGER NOT NULL DEFAULT 0,
    parse_warnings      TEXT,                        -- JSON array
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traj_project ON trajectories(project_id);
CREATE INDEX IF NOT EXISTS idx_traj_agent ON trajectories(agent);
CREATE INDEX IF NOT EXISTS idx_traj_started ON trajectories(started_at);
CREATE INDEX IF NOT EXISTS idx_traj_status ON trajectories(final_status);

CREATE TABLE IF NOT EXISTS trajectory_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id  TEXT NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
    sequence       INTEGER NOT NULL,
    type           TEXT NOT NULL,
    timestamp      TEXT,
    path           TEXT,
    command        TEXT,
    exit_code      INTEGER,
    tool_name      TEXT,
    summary        TEXT,
    metadata       TEXT,                             -- JSON blob
    redacted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(trajectory_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_events_traj ON trajectory_events(trajectory_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_type ON trajectory_events(type);

CREATE TABLE IF NOT EXISTS trajectory_files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id  TEXT NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
    path           TEXT NOT NULL,                    -- repo-relative when resolvable
    was_read       INTEGER NOT NULL DEFAULT 0,
    was_created    INTEGER NOT NULL DEFAULT 0,
    was_modified   INTEGER NOT NULL DEFAULT 0,
    was_deleted    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(trajectory_id, path)
);

CREATE INDEX IF NOT EXISTS idx_files_traj ON trajectory_files(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_files_path ON trajectory_files(path);
