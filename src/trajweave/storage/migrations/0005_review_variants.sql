-- TrajWeave schema v5: immutable reviewed content revisions.

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
