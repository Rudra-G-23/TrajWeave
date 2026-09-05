-- TrajWeave schema v7 (Stage 9: Lifecycle Learning).
--
-- Stage 9 separates a *logical policy* (stable identity, `policies`) from its
-- *immutable versions* (`policy_versions`). A lifecycle operation never
-- mutates a historical version in place - it always inserts a new version
-- row and repoints `policies.current_version_id`. `policy_lineage` records
-- explicit rewrite/promote/demote/merge/split/rollback edges between
-- versions (never inferred from text diffing). `policy_version_evidence` is
-- a reference-only bridge to Stage 8's `evaluation_comparisons` - it never
-- copies evaluation content, only links to it. `policy_lifecycle_previews`
-- and `policy_lifecycle_applications` reuse the same accept-then-apply shape
-- as Stage 7's `policy_review_previews`/`policy_applications`, but the
-- actual file write always goes through `trajweave.review.targets`
-- (resolve_target/build_preview/apply_preview) directly, keyed by
-- `policy_id` rather than an experience id, so repeated lifecycle changes to
-- the same logical policy keep updating the same managed block.

CREATE TABLE IF NOT EXISTS policies (
    id                      TEXT PRIMARY KEY,          -- POL-<short hash>
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'pruned')),
    current_version_id      TEXT REFERENCES policy_versions(id) ON DELETE SET NULL,
    origin_review_id        TEXT REFERENCES policy_reviews(id) ON DELETE SET NULL,
    origin_experience_id    TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policies_status ON policies(status);
CREATE INDEX IF NOT EXISTS idx_policies_origin_review ON policies(origin_review_id);

CREATE TABLE IF NOT EXISTS policy_versions (
    id                      TEXT PRIMARY KEY,          -- PVN-<policy id>-<version number>
    policy_id               TEXT NOT NULL REFERENCES policies(id) ON DELETE RESTRICT,
    version_number          INTEGER NOT NULL,
    content                 TEXT NOT NULL,
    content_hash            TEXT NOT NULL,
    placement_type          TEXT NOT NULL CHECK (placement_type IN ('global_rule', 'project_rule', 'scoped_rule', 'skill')),
    target_agent            TEXT,
    target_override         TEXT,
    scope_type              TEXT,
    scope_value             TEXT,
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'rolled_back')),
    created_via             TEXT NOT NULL CHECK (created_via IN ('initial', 'rewrite', 'promote', 'demote', 'merge', 'split', 'rollback')),
    created_from_version_id TEXT REFERENCES policy_versions(id) ON DELETE RESTRICT,
    created_from_review_id  TEXT REFERENCES policy_reviews(id) ON DELETE RESTRICT,
    reason                  TEXT,
    created_at              TEXT NOT NULL,
    UNIQUE (policy_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_policy_versions_policy ON policy_versions(policy_id, version_number);
CREATE INDEX IF NOT EXISTS idx_policy_versions_status ON policy_versions(status);
CREATE INDEX IF NOT EXISTS idx_policy_versions_review ON policy_versions(created_from_review_id);
CREATE INDEX IF NOT EXISTS idx_policy_versions_content_hash ON policy_versions(content_hash);

-- Explicit relational lineage. `from_version_id`/`to_version_id` describe one
-- directed edge; a merge is two rows sharing `to_version_id` with relation
-- 'merge_parent', a split is several rows sharing `from_version_id` with
-- relation 'split_child'.
CREATE TABLE IF NOT EXISTS policy_lineage (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    relation                TEXT NOT NULL CHECK (relation IN ('rewrite', 'promote', 'demote', 'merge_parent', 'split_child', 'rollback_source')),
    from_version_id         TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
    to_version_id           TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_lineage_from ON policy_lineage(from_version_id);
CREATE INDEX IF NOT EXISTS idx_policy_lineage_to ON policy_lineage(to_version_id);

-- Append-only audit log of every lifecycle decision, mirroring
-- policy_review_actions. Never updated or deleted.
CREATE TABLE IF NOT EXISTS policy_lifecycle_actions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id               TEXT NOT NULL REFERENCES policies(id) ON DELETE RESTRICT,
    version_id              TEXT REFERENCES policy_versions(id) ON DELETE RESTRICT,
    action                  TEXT NOT NULL,
    from_status             TEXT,
    to_status               TEXT,
    recommendation_id       TEXT,
    payload_json            TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_actions_policy ON policy_lifecycle_actions(policy_id, id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_actions_version ON policy_lifecycle_actions(version_id);

-- Deterministic, explainable recommendations. `evidence_json` is a small
-- aggregated snapshot (counts, ratios, deltas) - never a copy of raw
-- evaluation content. Re-running `recommend` with unchanged evidence reuses
-- the same deterministic id instead of growing the table unboundedly.
CREATE TABLE IF NOT EXISTS policy_recommendations (
    id                      TEXT PRIMARY KEY,          -- REC-<short hash>
    policy_id               TEXT NOT NULL REFERENCES policies(id) ON DELETE RESTRICT,
    version_id              TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
    operation               TEXT NOT NULL CHECK (operation IN ('retain', 'promote', 'demote', 'rewrite', 'merge', 'split', 'disable', 'prune', 'rollback')),
    reason_codes_json       TEXT NOT NULL DEFAULT '[]',
    explanation             TEXT NOT NULL,
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    counter_evidence_json   TEXT NOT NULL DEFAULT '[]',
    strength                TEXT NOT NULL DEFAULT 'informational' CHECK (strength IN ('informational', 'moderate', 'strong')),
    status                  TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected', 'deferred', 'superseded')),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_recs_policy ON policy_recommendations(policy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_recs_status ON policy_recommendations(status);

-- Reference-only bridge from a policy version to the Stage 8 comparisons
-- that count as evidence for it. Never copies comparison content.
CREATE TABLE IF NOT EXISTS policy_version_evidence (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_version_id           TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
    evaluation_comparison_id    TEXT NOT NULL REFERENCES evaluation_comparisons(id) ON DELETE RESTRICT,
    linked_at                   TEXT NOT NULL,
    UNIQUE (policy_version_id, evaluation_comparison_id)
);

CREATE INDEX IF NOT EXISTS idx_version_evidence_version ON policy_version_evidence(policy_version_id);

CREATE TABLE IF NOT EXISTS policy_lifecycle_previews (
    id                      TEXT PRIMARY KEY,          -- LPV-<version id>-<output hash prefix>
    policy_id               TEXT NOT NULL REFERENCES policies(id) ON DELETE RESTRICT,
    version_id              TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
    target_path             TEXT NOT NULL,
    target_hash             TEXT,
    output_hash             TEXT NOT NULL,
    proposed_content        TEXT NOT NULL,
    unified_diff            TEXT NOT NULL,
    target_kind             TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    consumed_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_previews_version ON policy_lifecycle_previews(version_id, created_at);

CREATE TABLE IF NOT EXISTS policy_lifecycle_applications (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id               TEXT NOT NULL REFERENCES policies(id) ON DELETE RESTRICT,
    version_id              TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
    preview_id              TEXT,
    outcome                 TEXT NOT NULL,
    target_path             TEXT,
    before_hash             TEXT,
    after_hash              TEXT,
    detail                  TEXT,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_applications_version ON policy_lifecycle_applications(version_id, id);

-- Defense in depth: even a direct SQL statement against this database file
-- cannot mutate a version's identity-defining fields or delete append-only
-- ledger rows. The service layer never issues these statements; these
-- triggers exist purely to make the append-only guarantee structural rather
-- than a matter of application-code discipline alone.
CREATE TRIGGER IF NOT EXISTS trg_policy_versions_immutable_content
BEFORE UPDATE OF content, content_hash, placement_type, policy_id, version_number, created_via,
    created_from_version_id, created_from_review_id, created_at ON policy_versions
BEGIN
    SELECT RAISE(ABORT, 'policy_versions: content and identity fields are immutable; insert a new version instead');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_versions_no_delete
BEFORE DELETE ON policy_versions
BEGIN
    SELECT RAISE(ABORT, 'policy_versions rows are append-only and cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_lifecycle_actions_no_update
BEFORE UPDATE ON policy_lifecycle_actions
BEGIN
    SELECT RAISE(ABORT, 'policy_lifecycle_actions is an append-only audit log');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_lifecycle_actions_no_delete
BEFORE DELETE ON policy_lifecycle_actions
BEGIN
    SELECT RAISE(ABORT, 'policy_lifecycle_actions is an append-only audit log');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_lineage_no_update
BEFORE UPDATE ON policy_lineage
BEGIN
    SELECT RAISE(ABORT, 'policy_lineage is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_lineage_no_delete
BEFORE DELETE ON policy_lineage
BEGIN
    SELECT RAISE(ABORT, 'policy_lineage is append-only');
END;
