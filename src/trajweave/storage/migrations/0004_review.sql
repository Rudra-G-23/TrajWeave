-- TrajWeave schema v4 (Stage 7: Review & Apply).
--
-- Stage 6 proposals remain immutable source material.  These tables record
-- human decisions and rendered policy variants without changing the proposal
-- rows or overwriting the observed repository outside an explicit apply.

CREATE TABLE IF NOT EXISTS policy_reviews (
    id                    TEXT PRIMARY KEY,          -- RV-<proposal id>
    proposal_set_id       TEXT NOT NULL,
    experience_id         TEXT NOT NULL,
    selected_proposal_id  TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'unreviewed' CHECK (status IN (
                              'unreviewed', 'accepted', 'rejected', 'deferred',
                              'test_first', 'applied', 'stale')),
    target_agent          TEXT,
    target_path           TEXT,
    target_scope_type     TEXT,
    target_scope_value    TEXT,
    content_override      TEXT,
    content_revision      INTEGER NOT NULL DEFAULT 0,
    source_fingerprint    TEXT NOT NULL,
    proposal_snapshot_json TEXT NOT NULL,
    latest_preview_id     TEXT,
    stale_reason          TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE(proposal_set_id)
);

CREATE INDEX IF NOT EXISTS idx_policy_reviews_status ON policy_reviews(status);
CREATE INDEX IF NOT EXISTS idx_policy_reviews_experience ON policy_reviews(experience_id);
CREATE INDEX IF NOT EXISTS idx_policy_reviews_proposal ON policy_reviews(selected_proposal_id);

CREATE TABLE IF NOT EXISTS policy_review_actions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id             TEXT NOT NULL REFERENCES policy_reviews(id) ON DELETE CASCADE,
    action                TEXT NOT NULL,
    from_status           TEXT,
    to_status             TEXT,
    proposal_id           TEXT,
    variant_revision      INTEGER,
    payload_json          TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_actions_review ON policy_review_actions(review_id, id);

CREATE TABLE IF NOT EXISTS policy_review_variants (
    review_id             TEXT NOT NULL REFERENCES policy_reviews(id) ON DELETE CASCADE,
    revision              INTEGER NOT NULL,
    proposal_id           TEXT NOT NULL,
    content               TEXT NOT NULL,
    content_hash          TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (review_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_policy_variants_review ON policy_review_variants(review_id, revision);

CREATE TABLE IF NOT EXISTS policy_review_previews (
    id                    TEXT PRIMARY KEY,          -- PV-<review>-<revision>
    review_id             TEXT NOT NULL REFERENCES policy_reviews(id) ON DELETE CASCADE,
    target_path           TEXT NOT NULL,
    target_hash           TEXT,
    output_hash           TEXT NOT NULL,
    proposed_content      TEXT NOT NULL,
    unified_diff          TEXT NOT NULL,
    target_kind           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    consumed_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_policy_previews_review ON policy_review_previews(review_id, created_at);

CREATE TABLE IF NOT EXISTS policy_applications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id             TEXT NOT NULL REFERENCES policy_reviews(id) ON DELETE CASCADE,
    preview_id            TEXT,
    outcome               TEXT NOT NULL,
    target_path           TEXT,
    before_hash           TEXT,
    after_hash            TEXT,
    detail                TEXT,
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_applications_review ON policy_applications(review_id, id);
