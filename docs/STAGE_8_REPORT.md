# TrajWeave Stage 8 Report - Evaluation

## Scope

Stage 8 answers one question per paired trial: for the same frozen coding
task under the same frozen execution setup, does adding a reviewed Stage 7
policy change the objective outcome? It is an **evaluation engine**, not
Stage 9 lifecycle management (no promote/demote/merge/prune) and not Stage 10
benchmarking (no multi-repo suite, no significance claims, no plots). Stage
7's review/apply architecture is unchanged; Stage 8 only *references* an
already-reviewed variant and re-applies it through Stage 7's own safe writer.

## Architecture

- `trajweave.evaluation.service.EvaluationService` owns the state machine
  (freeze a spec once; run any number of baseline/candidate repetitions
  against it) and is the single read-model shared by the CLI and the UI -
  the same shape as `trajweave.review.service.ReviewService`.
- `trajweave.evaluation.workspace` builds two independent, ephemeral git
  clones of the developer's repository at one pinned commit and guarantees
  their removal even on failure.
- `trajweave.evaluation.apply` adapts a frozen spec's placement fields into
  Stage 7's own `resolve_target` / `build_preview` / `apply_preview` calls,
  pointed at the candidate's isolated workspace instead of a registered
  project root. No second file-writing implementation exists.
- `trajweave.evaluation.execute` runs the (optional) agent command and the
  verifier commands as plain subprocesses with a timeout and an output cap.
  Nothing here ever calls a model or network endpoint itself - `command` is
  always an explicit argv the caller supplied.
- `trajweave.evaluation.compare` is pure and dependency-free: it turns two
  runs' verifier results into an outcome category plus the full underlying
  delta, never hiding a regression behind a label.
- Persistence is added to `trajweave.storage.repository.Repository` as a new
  `# Stage 8` section, following the same pattern as Stages 5-7 (one
  God-object `Repository`, not a per-table repository class).

## Schema

Migration `0006_evaluation.sql` raises the schema to version 6 and adds:

- `evaluation_specs`: the frozen experiment definition - reviewed variant
  reference (`review_id`, `content_revision`, a snapshotted copy of
  `policy_content`/`policy_content_hash`), selected placement (`placement_type`,
  `target_agent`, optional `target_override`, `scope_type`/`scope_value`),
  repository identity (`repo_root`, `repo_commit`), the task specification,
  agent/model/reasoning metadata (nullable - never invented), environment
  metadata, the ordered verifier list (`verifier_json`, first entry is the
  primary task check), execution limits, condition ordering mode, and seed.
  Never updated after insert.
- `evaluation_runs`: one row per condition per repetition (`baseline` |
  `candidate`), append-only, `UNIQUE(evaluation_id, repetition_index,
  condition)`. Records status (`completed | failed | error | interrupted`),
  timing, the candidate's apply outcome, agent exit code/timeout/output
  excerpts, and a `metrics_json` blob (duration, tokens - `null` when
  unavailable, policy bytes).
- `evaluation_verifier_results`: one row per verifier check per run - name,
  command, exit code, pass/fail, timeout, duration, output excerpts.
- `evaluation_comparisons`: one row per repetition,
  `UNIQUE(evaluation_id, repetition_index)`, referencing both run ids.
  Persists `outcome` (`improved | unchanged | regressed | invalid |
  incomparable`), `task_success_delta`, `regression_count`,
  `regression_details_json`, `changed_checks_json`, and `metrics_delta_json` -
  the category never hides the underlying metrics.

The migration is additive only (`CREATE TABLE IF NOT EXISTS`, matching every
prior migration's idempotency convention) and was verified against the real,
pre-existing Stage 7 database on this machine: it reached schema version 6,
gained all four new tables, and its 3 existing trajectories / 1 project / 0
experiences / 0 proposals / 0 reviews were untouched.

## Evaluation spec (frozen)

`EvaluationService.create_spec(review_id, ...)`:

1. Loads the review through `ReviewService.history()` (reuse, not a
   duplicate lookup) and requires status in `{accepted, test_first, applied}`
   and not stale - a review that is `unreviewed`, `rejected`, `deferred`, or
   whose Stage 6 source changed underneath it is rejected with a clear
   `EvaluationError` before anything is created.
2. Snapshots the selected proposal's `effective_content` (the reviewed edit
   if one was recorded, otherwise the Stage 6 proposed content) and its
   sha256, so the frozen spec cannot silently drift if the live review is
   edited again later.
3. Resolves `--repo` to its canonical git root and `--commit` (default HEAD)
   to a full sha via `git rev-parse`, refusing non-git and shared-system
   directories with the same guard Stage 7's project registry uses.
4. Records environment metadata (`python_version`, `platform`,
   `trajweave_version`) and whether the source tree had uncommitted changes
   at freeze time (informational only - see Isolation).
5. Persists the spec once. It is never mutated again; `eval run
   <existing-evaluation-id>` intentionally **refuses** any spec-defining flag
   (`--repo`, `--task`, `--verify`, `--agent-cmd`, `--target*`, `--model`,
   ...) rather than silently accepting a changed condition. This is a
   stronger guarantee than merely flagging the resulting comparison
   "incomparable" after the fact - a changed condition can never reach a
   comparison at all.

Design choice: Stage 8 never reuses a Stage 7 review's *already-resolved*
`target_path`, because that path was resolved against the original
(registered) project root, not the isolated sandbox, and reusing it verbatim
would either silently point outside the sandbox or require re-deriving a
relative path by convention. Instead Stage 8 always renders into the
placement type's own default location inside the sandbox (e.g.
`<workspace>/AGENTS.md`) unless the researcher passes an explicit
`--target <relative-path>` at spec-creation time. This is a deliberate,
documented limitation, not an oversight.

## Isolation

`trajweave.evaluation.workspace.isolated_workspaces(repo_root, commit)`
builds baseline and candidate as two independent `git clone --local
--no-checkout` + `git checkout --detach <commit>` clones under a fresh
`tempfile.mkdtemp()` directory, with `origin` removed from each clone
immediately after checkout. Rationale over the alternatives:

- **Not `git worktree add`**: a worktree registers metadata inside the
  *source* repository's `.git/worktrees/` - exactly the kind of side effect
  on the developer's active repository this stage must never risk,
  including on a crash mid-evaluation.
- **Not a plain `shutil.copytree`**: `git clone --local` guarantees both
  workspaces are built from the exact same commit rather than whatever the
  working tree happens to hold, and is still a plain, dependency-free git
  invocation.

Only committed state is ever evaluated - `git clone` reads git's object
database, not the live working tree, so uncommitted changes in the source
repository can never leak into, or contaminate, either sandbox. This is
recorded as `source_had_uncommitted_changes` in the frozen spec rather than
silently ignored. Cleanup (`shutil.rmtree`) runs in a `finally` block, so
both sandboxes are removed even if the run inside raises.

## Stage 7 safe-apply reuse

`trajweave.evaluation.apply.apply_candidate_policy` calls Stage 7's own
`resolve_target` / `build_preview` / `apply_preview` from
`trajweave.review.targets` with `project_root` pointed at the candidate's
isolated workspace directory instead of a DB-registered project root - no
second file-writing implementation exists. Every Stage 7 protection
therefore applies unmodified inside the sandbox: managed-section
preservation, atomic same-directory replacement, `O_NOFOLLOW` symlink
rejection, path-traversal and absolute-path rejection, binary-target
rejection, malformed/duplicate-marker rejection, and idempotent re-apply
(`already applied`). A `global_rule` placement - which has no natural home
inside a cloned repository - is given an isolated stand-in
(`<workspace>/.trajweave-eval-global/<agent>/`) so evaluation never touches
the real machine-wide `~/.trajweave/policies/` tree. The baseline workspace
never has the candidate policy applied to it.

## Verifier and primary outcome

`--verify CMD` is repeatable; by convention the *first* command is the
primary task-specific check and every additional command is a
regression-detection check. Each check runs as a plain subprocess with a
configurable per-command timeout and a 20 KB output cap (`execute.py`);
timeouts and missing executables are captured as a normal (non-crashing)
outcome, not an exception. `compare.compare_runs` computes:

```text
task_success_delta   "fail->pass" | "pass->pass" | "fail->fail" | "pass->fail"
changed_checks        every check whose pass/fail flipped between conditions
regression_count      checks that were passing on baseline and failing on candidate
outcome                improved | unchanged | regressed | invalid | incomparable
```

`outcome="improved"` never hides a concurrent regression:
`regression_count`/`regression_details` are always populated alongside it,
never folded away (Scenario 5 below).

## Regression detection

A regression is any check that passed on baseline and failed on candidate,
computed across *every* check, not just the primary one - so a candidate
that fixes the task but breaks something else is `improved` with a non-zero,
visible `regression_count`, and a candidate that leaves the primary check
alone but breaks a secondary check is reported as `regressed`, not
`unchanged`.

## Efficiency metrics

Persisted when observable: wall-clock `duration_ms` (always), the
candidate's `apply_outcome`, agent exit code/timeout, and `policy_bytes`
(candidate only). Token telemetry is stored as `tokens: null` - Stage 8's
harness boundary (a plain subprocess) does not expose a token count, and
Stage 8 does not estimate one and present it as observed. `command`/`tool
call`/`retry` counts are not recorded because the current subprocess-based
harness boundary does not expose them either; this is listed under Known
limitations rather than approximated.

## Repeated trials and provenance

`trajweave eval run <evaluation-id> --repetitions N` appends N more
baseline/candidate pairs (`repetition_index` strictly increasing,
`UNIQUE(evaluation_id, repetition_index, condition)`); prior repetitions are
never overwritten. Condition ordering per repetition follows the spec's
frozen `condition_order_mode` (`baseline_first` | `candidate_first` |
`alternating`) and is recorded per run as `order_position`. Provenance is by
reference, not duplication:
`evaluation_specs.review_id -> policy_reviews.id -> policy_reviews
.selected_proposal_id -> placement_proposals -> placement_proposal_sets
.experience_id -> experiences.id -> experience_evidence -> occurrences ->
trajectories`.

## CLI

```text
trajweave eval list
trajweave eval show <evaluation-id>
trajweave eval run <review-or-proposal-id> --repo PATH --task ... --verify CMD [--verify CMD ...]
                    [--agent-cmd CMD] [--target-agent codex|claude] [--target PATH]
                    [--commit SHA] [--repetitions N] [--order MODE] [--timeout SECS] [--json]
trajweave eval run <evaluation-id> [--repetitions N] [--json]
trajweave eval compare <evaluation-id>
```

`--agent-cmd` is never invoked automatically - it must be passed explicitly
(a real coding agent's CLI, or in tests a small deterministic script);
omitting it skips the agent step and only verifies the isolated snapshot.
This is how Stage 8 satisfies "do not silently invoke paid agents."

## UI

A read-only "Evaluations" section was added to the existing local UI:
`/api/evals` (list) and `/api/evals/<id>` (spec, every run with its verifier
results, and every comparison), and matching `#/evals` / `#/eval/<id>` views
following the existing Reviews list/detail pattern. GET only - no run/apply
control was added, consistent with Stage 8 being read-oriented and with
"never silently invoke an agent from a UI click." No Stage 9 lifecycle
controls (promote/demote/merge/split/prune/rollback) exist anywhere in the
UI or CLI.

## Zero-candidate behavior

`trajweave eval list` against the real, unmodified TrajWeave database on
this machine (3 trajectories, 1 project, 0 experiences, 0 proposals, 0
reviews - unchanged since Stage 6/7) returns `[]` / "No evaluations yet."
cleanly. No Stage 5/6 threshold was lowered and no fixture/production data
was inserted to manufacture a non-empty result.

## Controlled fixtures

Tests require no network access and no paid API. A toy repository
(`eval_repo`: a bug represented as a missing `fix.txt`, plus a `legacy.txt`
regression bait file) is paired with small deterministic Python scripts
(`eval_scripts` in `tests/conftest.py`) that stand in for a coding agent and
its verifiers:

- the fake "agent" writes `fix.txt` (and, as an intentional regression,
  deletes `legacy.txt`) **only if it can see the candidate's actual managed
  policy block** in `AGENTS.md`/`CLAUDE.md` - so baseline-vs-candidate
  outcomes are driven by Stage 7's real apply path, not a hardcoded
  condition name;
- a second "regressor" agent/verifier pair proves a candidate can make a
  primary check strictly worse.

This exercises the real orchestration/persistence pathway (isolation, Stage
7 apply, subprocess execution, comparison, persistence) end to end, not a
bypass that only asserts on hardcoded labels.

## Adversarial findings

Investigated during implementation and covered by tests; two were caught and
fixed before commit:

1. **JSON blob shape mismatch** - the CLI's `eval compare`/`eval show --json`
   initially returned raw un-decoded `*_json` columns (`regression_details_json`
   instead of a `regression_details` list), which the UI had separately
   (and inconsistently) decoded. Fixed by moving decoding into
   `EvaluationService.history()` - the single shared read-model CLI and UI
   both call, matching how `ReviewService.history()` already works.
2. **Absolute Stage 7 target reuse** - an early draft considered reusing a
   review's already-resolved `target_path` verbatim inside the sandbox; this
   would have raised `SafetyError: escapes approved location` (or worse,
   silently written outside the sandbox root) for any repo whose absolute
   path differs from the sandbox. Fixed by never reusing a resolved
   absolute target - Stage 8 renders into the default location unless the
   researcher passes an explicit relative `--target`.

Additional cases proven to fail safely rather than corrupt state: workspace
isolation (mutations in one condition are invisible to the other; both
sandboxes vanish even when an exception is raised inside the `with` block);
the developer's real repository is provably untouched after a run;
candidate-apply refusal (path traversal, symlink escape, binary target,
malformed marker) leaves the baseline run `completed` and the failed
candidate run fully auditable rather than aborting the whole evaluation;
the source repository disappearing between spec creation and a later
repetition is recorded as an `error`/`invalid` pair, never as a fabricated
task failure; a stale review is refused at spec-creation time; a frozen
spec's conditions cannot be changed on rerun (rejected outright, not merely
flagged after the fact); subprocess timeouts and unbounded verifier output
are captured and truncated rather than hanging or blowing up memory.

## Validation

- Starting baseline (feat/stage7, `a364409`): 241 passed, 0 failed.
- Full suite after Stage 8: **290 passed, 0 failed** (241 original + 49 new
  Stage 8 unit/integration tests).
- Real database: migrated from schema v5 to v6 in place with zero data loss
  (verified table-by-table); `trajweave eval list` returns a clean empty
  result.

## Known limitations

- Token, tool-call, and retry telemetry are not observable through the
  current subprocess-based harness boundary and are always recorded as
  `null`/absent rather than estimated.
- A Stage 7 review's previously-resolved custom `--target` is not
  automatically reused inside the sandbox (see "Evaluation spec" above);
  the researcher must pass `--target` again explicitly at spec-creation time
  if a non-default location is required.
- Only the last commit is evaluated; uncommitted working-tree changes in the
  source repository are excluded from every snapshot by design.
- `condition_order_mode` controls execution order but nothing in Stage 8
  attempts to correct for ordering effects statistically - that is Stage 10
  territory.
- The UI is local, loopback-only, and read-only; there is no run/compare
  trigger in the browser.

## Explicit confirmation

- Stage 9 lifecycle logic (promote/demote/merge/split/prune/rollback/policy
  optimization loops) was **not** implemented.
- Stage 10 benchmark logic (multi-repo suites, significance claims, paper
  plots) was **not** implemented.
- Existing Stage 0-7 behavior remains regression-safe (241/241 original tests
  still pass unchanged).

## Stage 9 readiness

Stage 8 gives Stage 9 exactly the inputs a lifecycle decision needs without
making any lifecycle decision itself: a durable, queryable history of
paired comparisons per reviewed variant (`improved`/`unchanged`/`regressed`/
`invalid`/`incomparable`, with the full regression and metrics delta behind
each), traceable back through the review to its originating Experience and
evidence. Stage 9 can read this history to decide whether a variant should
be promoted, demoted, merged, or pruned - Stage 8 deliberately stops at
"here is what happened," never "here is what to do about it."
