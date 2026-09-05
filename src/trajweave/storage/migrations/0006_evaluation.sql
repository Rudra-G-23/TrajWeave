-- TrajWeave schema v6 (Stage 8: Evaluation).
--
-- Stage 8 answers one question per row: for the same frozen coding task under
-- the same frozen execution setup, did adding a reviewed TrajWeave policy
-- change the objective outcome? Nothing here mutates Stage 7's review ledger;
-- an evaluation spec only *references* an already-reviewed variant.
--
-- evaluation_specs is the frozen experiment definition (repo snapshot, task,
-- verifier commands, selected placement, agent/model metadata). It is never
-- updated after insert - every `eval run` on an existing spec only appends
-- evaluation_runs/evaluation_comparisons rows (repetition_index increases).

CREATE TABLE IF NOT EXISTS evaluation_specs (
    id                      TEXT PRIMARY KEY,          -- EV-<short hash>
    review_id               TEXT NOT NULL REFERENCES policy_reviews(id) ON DELETE RESTRICT,
    experience_id           TEXT NOT NULL,
    proposal_id             TEXT NOT NULL,
    content_revision        INTEGER NOT NULL,
    policy_content          TEXT NOT NULL,
    policy_content_hash     TEXT NOT NULL,
    placement_type          TEXT NOT NULL,
    target_agent            TEXT NOT NULL,
    target_override         TEXT,
    scope_type              TEXT,
    scope_value             TEXT,
    repo_root               TEXT NOT NULL,             -- identity only; never written to
    repo_commit             TEXT NOT NULL,
    task_spec_json          TEXT NOT NULL,
    agent_name              TEXT,
    agent_version           TEXT,
    model_name              TEXT,
    model_version           TEXT,
    reasoning_config_json   TEXT,
    agent_config_json       TEXT,
    agent_command_json      TEXT,                      -- argv list, or NULL to skip the agent step
    environment_json        TEXT NOT NULL,
    verifier_json           TEXT NOT NULL,              -- ordered list; [0] is the primary task check
    execution_limits_json   TEXT,
    condition_order_mode    TEXT NOT NULL DEFAULT 'baseline_first'
                            CHECK (condition_order_mode IN ('baseline_first', 'candidate_first', 'alternating')),
    seed                    TEXT,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eval_specs_review ON evaluation_specs(review_id);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id                      TEXT PRIMARY KEY,          -- ER-<spec>-<repetition>-<condition>
    evaluation_id           TEXT NOT NULL REFERENCES evaluation_specs(id) ON DELETE CASCADE,
    repetition_index        INTEGER NOT NULL,
    condition               TEXT NOT NULL CHECK (condition IN ('baseline', 'candidate')),
    order_position          INTEGER NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'error', 'interrupted')),
    error_reason            TEXT,
    started_at              TEXT NOT NULL,
    ended_at                 TEXT,
    duration_ms              INTEGER,
    apply_outcome             TEXT,                     -- candidate-only: applied | already_applied | NULL (baseline)
    agent_exit_code           INTEGER,
    agent_timed_out           INTEGER NOT NULL DEFAULT 0,
    agent_stdout_excerpt       TEXT,
    agent_stderr_excerpt       TEXT,
    metrics_json               TEXT NOT NULL DEFAULT '{}',
    created_at                 TEXT NOT NULL,
    UNIQUE (evaluation_id, repetition_index, condition)
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_eval ON evaluation_runs(evaluation_id, repetition_index);

CREATE TABLE IF NOT EXISTS evaluation_verifier_results (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    checker_name            TEXT NOT NULL,
    command_json            TEXT NOT NULL,
    exit_code               INTEGER,
    passed                  INTEGER NOT NULL,
    timed_out               INTEGER NOT NULL DEFAULT 0,
    duration_ms             INTEGER,
    stdout_excerpt          TEXT,
    stderr_excerpt          TEXT,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eval_verifier_run ON evaluation_verifier_results(run_id);

CREATE TABLE IF NOT EXISTS evaluation_comparisons (
    id                       TEXT PRIMARY KEY,          -- EC-<spec>-<repetition>
    evaluation_id            TEXT NOT NULL REFERENCES evaluation_specs(id) ON DELETE CASCADE,
    repetition_index          INTEGER NOT NULL,
    baseline_run_id            TEXT REFERENCES evaluation_runs(id) ON DELETE SET NULL,
    candidate_run_id           TEXT REFERENCES evaluation_runs(id) ON DELETE SET NULL,
    outcome                    TEXT NOT NULL CHECK (outcome IN ('improved', 'unchanged', 'regressed', 'invalid', 'incomparable')),
    invalid_reason              TEXT,
    task_success_delta          TEXT,
    regression_count             INTEGER NOT NULL DEFAULT 0,
    regression_details_json      TEXT NOT NULL DEFAULT '[]',
    changed_checks_json          TEXT NOT NULL DEFAULT '[]',
    metrics_delta_json           TEXT NOT NULL DEFAULT '{}',
    created_at                   TEXT NOT NULL,
    UNIQUE (evaluation_id, repetition_index)
);

CREATE INDEX IF NOT EXISTS idx_eval_comparisons_eval ON evaluation_comparisons(evaluation_id, repetition_index);
