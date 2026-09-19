-- TrajWeave schema v3 (Stage 6: Placement Engine).
--
-- Placement is a derived, read-only research layer over Stage 5 Experiences.
-- A proposal set is the current deterministic generation for one Experience;
-- it deliberately records every competing alternative rather than only the
-- recommendation.  Nothing in these tables is a rendered policy file or a
-- filesystem mutation.

CREATE TABLE IF NOT EXISTS placement_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at                TEXT NOT NULL,
    finished_at               TEXT,
    generator_version         TEXT NOT NULL,
    eligible_experiences      INTEGER NOT NULL DEFAULT 0,
    proposal_sets_generated   INTEGER NOT NULL DEFAULT 0,
    runtime_seconds           REAL
);

CREATE TABLE IF NOT EXISTS placement_proposal_sets (
    id                  TEXT PRIMARY KEY,            -- PS-E-0001
    experience_id       TEXT NOT NULL UNIQUE REFERENCES experiences(id) ON DELETE CASCADE,
    source_fingerprint  TEXT NOT NULL,               -- deterministic input snapshot hash
    generator_version   TEXT NOT NULL,
    run_id              INTEGER REFERENCES placement_runs(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_placement_sets_run ON placement_proposal_sets(run_id);
CREATE INDEX IF NOT EXISTS idx_placement_sets_experience ON placement_proposal_sets(experience_id);

CREATE TABLE IF NOT EXISTS placement_proposals (
    id                  TEXT PRIMARY KEY,            -- PP-E-0001-project_rule
    proposal_set_id     TEXT NOT NULL REFERENCES placement_proposal_sets(id) ON DELETE CASCADE,
    placement_type      TEXT NOT NULL CHECK (placement_type IN (
                            'ignore', 'global_rule', 'project_rule', 'scoped_rule', 'skill')),
    scope_type          TEXT NOT NULL,
    scope_value         TEXT,
    proposed_content    TEXT NOT NULL,
    score               REAL NOT NULL,
    rank                INTEGER NOT NULL CHECK (rank > 0),
    feature_values_json TEXT NOT NULL,
    diagnostics_json    TEXT NOT NULL,
    diagnostics_text    TEXT NOT NULL,
    generator_version   TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE(proposal_set_id, placement_type),
    UNIQUE(proposal_set_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_placement_proposals_set_rank
    ON placement_proposals(proposal_set_id, rank);
CREATE INDEX IF NOT EXISTS idx_placement_proposals_type_score
    ON placement_proposals(placement_type, score DESC);

-- The role is kept separately from Stage 5's experience_evidence relationship:
-- an occurrence may be a contradiction to the Experience while still being
-- specifically cited by an Ignore alternative, for example.
CREATE TABLE IF NOT EXISTS placement_proposal_evidence (
    proposal_id         TEXT NOT NULL REFERENCES placement_proposals(id) ON DELETE CASCADE,
    occurrence_id       TEXT NOT NULL REFERENCES experience_occurrences(id) ON DELETE CASCADE,
    role                TEXT NOT NULL DEFAULT 'evidence',
    PRIMARY KEY (proposal_id, occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_placement_evidence_occurrence
    ON placement_proposal_evidence(occurrence_id);
