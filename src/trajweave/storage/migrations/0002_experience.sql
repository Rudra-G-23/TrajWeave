-- TrajWeave schema v2 (Stage 5: Experience Extraction).
--
-- Adds a research layer *on top of* the Stage 0-3 trajectory substrate. Nothing
-- here mutates a repository, an AGENTS.md / CLAUDE.md, or any source code - these
-- tables only hold evidence-backed *candidate* experiences derived from the
-- already-normalized trajectories.
--
-- Design:
--   * experience_occurrences  - one detected pattern episode inside one trajectory
--   * experiences             - a cross-trajectory cluster of occurrences
--   * experience_evidence     - occurrence <-> experience link (support/contradiction)
--   * experience_extraction_state - per-trajectory incremental bookkeeping
--   * experience_runs         - per-run metrics (research telemetry)

CREATE TABLE IF NOT EXISTS experience_occurrences (
    id                TEXT PRIMARY KEY,              -- O-000001
    trajectory_id     TEXT NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
    project_id        TEXT REFERENCES projects(id) ON DELETE SET NULL,
    pattern_type      TEXT NOT NULL,                 -- failure_repair_success | ...
    group_key         TEXT NOT NULL,                 -- deterministic cluster key
    start_sequence    INTEGER NOT NULL,
    end_sequence      INTEGER NOT NULL,
    failure_family    TEXT,                          -- test | lint | build | (null)
    resolution_family TEXT,                          -- test | lint | build | (null)
    repair_context    TEXT,                          -- migration | config | tests | ...
    error_signature   TEXT,                          -- normalized, for repeated_failure
    classification    TEXT NOT NULL DEFAULT 'support', -- support | contradiction | ambiguous
    features_json     TEXT,                          -- compact deterministic feature blob
    dedupe_hash       TEXT NOT NULL UNIQUE,          -- sha256(traj, pattern_type, start, end)
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_occ_group ON experience_occurrences(group_key);
CREATE INDEX IF NOT EXISTS idx_occ_traj ON experience_occurrences(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_occ_pattern ON experience_occurrences(pattern_type);
CREATE INDEX IF NOT EXISTS idx_occ_signature ON experience_occurrences(error_signature);

CREATE TABLE IF NOT EXISTS experiences (
    id                  TEXT PRIMARY KEY,            -- E-0001
    group_key           TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    summary             TEXT,
    reusable_lesson     TEXT,
    pattern_type        TEXT NOT NULL,
    context_json        TEXT,                        -- ["database","migration"]
    status              TEXT NOT NULL DEFAULT 'candidate', -- candidate | needs_more_evidence | rejected | archived
    confidence          REAL NOT NULL DEFAULT 0,
    confidence_json     TEXT,                        -- component breakdown (for the UI)
    support_count       INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    ambiguous_count     INTEGER NOT NULL DEFAULT 0,
    occurrence_count    INTEGER NOT NULL DEFAULT 0,
    project_count       INTEGER NOT NULL DEFAULT 0,
    first_seen_at       TEXT,
    last_seen_at        TEXT,
    summary_source      TEXT NOT NULL DEFAULT 'deterministic', -- deterministic | llm
    review_status       TEXT NOT NULL DEFAULT 'unreviewed',    -- unreviewed | valid | false_positive | needs_more_evidence
    reviewed_at         TEXT,
    review_note         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exp_status ON experiences(status);
CREATE INDEX IF NOT EXISTS idx_exp_confidence ON experiences(confidence);
CREATE INDEX IF NOT EXISTS idx_exp_pattern ON experiences(pattern_type);

CREATE TABLE IF NOT EXISTS experience_evidence (
    experience_id   TEXT NOT NULL REFERENCES experiences(id) ON DELETE CASCADE,
    occurrence_id   TEXT NOT NULL REFERENCES experience_occurrences(id) ON DELETE CASCADE,
    relationship    TEXT NOT NULL DEFAULT 'support', -- support | contradiction | ambiguous
    PRIMARY KEY (experience_id, occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_occ ON experience_evidence(occurrence_id);

CREATE TABLE IF NOT EXISTS experience_extraction_state (
    trajectory_id   TEXT PRIMARY KEY REFERENCES trajectories(id) ON DELETE CASCADE,
    source_hash     TEXT,
    extracted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experience_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    rebuild                 INTEGER NOT NULL DEFAULT 0,
    project_filter          TEXT,
    trajectories_considered INTEGER NOT NULL DEFAULT 0,
    trajectories_analyzed   INTEGER NOT NULL DEFAULT 0,
    occurrences_found       INTEGER NOT NULL DEFAULT 0,
    clusters_formed         INTEGER NOT NULL DEFAULT 0,
    candidates_created      INTEGER NOT NULL DEFAULT 0,
    needs_more_evidence     INTEGER NOT NULL DEFAULT 0,
    llm_used                INTEGER NOT NULL DEFAULT 0,
    llm_tokens              INTEGER NOT NULL DEFAULT 0,
    runtime_seconds         REAL
);
