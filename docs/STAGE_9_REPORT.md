# TrajWeave Stage 9 Report - Lifecycle Learning

## Scope

Stage 8 answers "did this candidate help?" for one paired trial. Stage 9
answers a different question: given everything Stage 8 has accumulated about
a reviewed policy over time, what should happen to that policy over its
*lifetime* - should it remain, become more general, become more specific, be
rewritten, be merged with another policy, be split into narrower policies,
be disabled, be pruned, or be rolled back to an earlier version? Stage 9 is a
**deterministic, evidence-backed decision-support and version-management
layer**, not a self-modifying agent: every recommendation is a documented,
fixed decision rule over Stage 8's outcome counts, and every operation that
changes what a policy *is* requires an explicit, separately-invoked human
action before anything is written to a real file. Stage 10's benchmarking
and statistical-significance work and Stage 11's paper work are explicitly
out of scope and were not touched.

## Architecture

- `trajweave.lifecycle.service.LifecycleService` owns the state machine
  (adopt, rewrite, promote, demote, merge, split, disable, enable, prune,
  rollback, recommend, preview, apply) and is the single read-model shared
  by the CLI and the UI - the same shape as `ReviewService` and
  `EvaluationService`.
- `trajweave.lifecycle.models` holds the state machine's constants:
  placement statuses, the promote/demote adjacency table, and the reason
  code vocabulary. Nothing here is stringly-typed inline in the service.
- `trajweave.lifecycle.evidence` aggregates Stage 8 `evaluation_comparisons`
  into a deterministic per-version summary (counts, ratios, deltas) and
  performs the reference-only linking described below. It has no opinion
  about what should happen - only Stage 8 facts.
- `trajweave.lifecycle.heuristics` turns an evidence summary into a
  recommendation: a fixed operation, a list of named reason codes, a
  human-readable explanation, and an explicit strength
  (`informational`/`moderate`/`strong`). All thresholds are named module
  constants, not embedded magic numbers.
- `trajweave.lifecycle.apply` adapts a policy version's placement fields into
  Stage 7's own `resolve_target` / `build_preview` / `apply_preview` calls,
  exactly as `trajweave.evaluation.apply` does for Stage 8. No second
  file-writing implementation exists anywhere in Stage 9.
- Persistence is added to `trajweave.storage.repository.Repository` as a new
  `# Stage 9` section, following the same pattern as Stages 5-8 (one
  God-object `Repository`, not a per-table repository class).

## Domain model: logical policy vs. immutable version

Stage 9 introduces two distinct identities:

- **`policies`** - the durable logical identity (`POL-...`). Its only mutable
  fields are `status` (`active` / `disabled` / `pruned`) and
  `current_version_id` (which version is "the" policy right now).
- **`policy_versions`** - an immutable snapshot (`PVN-<policy>-<n>`):
  content, content hash, placement type, target agent/override, scope, and
  exactly how and from what it was created (`created_via`,
  `created_from_version_id`, `created_from_review_id`). Its only mutable
  field is `status` (`active` / `superseded` / `rolled_back`), which tracks
  supersession, never content.

Splitting the state this way means a status field never has to mean two
things at once: whether a policy is *currently allowed to activate* is
entirely a `policies.status` question; whether a specific version is *the
current one or a historical one* is entirely a `policy_versions.status`
question. A disabled policy's current version still reads `active` - it is
still "the" version, it just is not currently allowed to be applied.

No lifecycle operation ever runs `UPDATE policy_versions SET content = ...`.
Every operation inserts a new version row, inserts explicit
`policy_lineage` edges, and repoints `policies.current_version_id`. This is
enforced by application code and, as defense in depth, by SQLite triggers
(see "Append-only enforcement" below) that reject any direct SQL attempt to
change a version's content/identity fields or to delete a version, lineage
edge, or lifecycle-action row.

## Schema

Migration `0007_lifecycle.sql` raises the schema to version 7 and adds:

- `policies` - logical identity, status, current version pointer.
- `policy_versions` - immutable snapshots, with `UNIQUE(policy_id,
  version_number)`.
- `policy_lineage` - explicit directed edges (`rewrite`, `promote`, `demote`,
  `merge_parent`, `split_child`, `rollback_source`) between version ids.
  Never inferred from text diffing. A merge is two rows sharing one
  `to_version_id`; a split is several rows sharing one `from_version_id`.
- `policy_lifecycle_actions` - append-only audit log, one row per state
  transition or recommendation decision, mirroring
  `policy_review_actions`.
- `policy_recommendations` - one row per computed recommendation
  (`operation`, `reason_codes_json`, `explanation`, `evidence_json`,
  `counter_evidence_json`, `strength`, `status`).
- `policy_version_evidence` - a reference-only bridge table:
  `(policy_version_id, evaluation_comparison_id)`. It never copies a
  comparison's content, only records that it was considered.
- `policy_lifecycle_previews` / `policy_lifecycle_applications` - the same
  preview-then-apply ledger shape as `policy_review_previews` /
  `policy_applications`.

`evaluation_comparisons(id)` is referenced with `ON DELETE RESTRICT` (not
`CASCADE`): Stage 8 comparisons are themselves append-only and never
deleted in practice, and evidence links should never silently vanish if
that ever changed. This mirrors Stage 8's own choice for
`evaluation_specs.review_id`.

Existing Stage 0-8 tables and data are untouched; migration was verified
against a database built with only migrations 1-6 applied plus real Stage 7
review and Stage 8 comparison rows (`tests/unit/test_lifecycle_migration.py`)
- all pre-existing rows remain readable byte-for-byte after the Stage 9
migration runs, and a fresh database reaches the same head version in one
step.

## Linking Stage 8 evidence to a policy version

A policy version created by adopting an applied/accepted Stage 7 review
carries that review's id in `created_from_review_id`. Aggregation walks
`policy_reviews.id -> evaluation_specs.review_id -> evaluation_comparisons`
to find every comparison that evaluated the exact content this version was
adopted from, and records each one (by reference only) in
`policy_version_evidence`. A version created by a Stage 9 operation
(rewrite/promote/demote/merge/split) starts with `created_from_review_id =
NULL` and therefore starts with zero evidence - it has genuinely never been
evaluated yet, and Stage 9 never pretends otherwise (this is exactly
scenario C below). **Rollback is the one deliberate exception**: because a
rollback's content is byte-identical to the version it restores, the new
version inherits that version's `created_from_review_id`, so a policy that
is rolled back to a previously-good version is immediately recognized as
having the evidence that made it good in the first place.

The full provenance chain a caller can traverse from one version is:
version -> lifecycle action -> recommendation -> Stage 8 comparison -> Stage
8 spec's review_id -> Stage 7 review -> Stage 6 placement proposal -> Stage
5 experience -> experience evidence occurrence -> trajectory id. This is
implemented in `LifecycleService.full_provenance()` by composing existing
Repository read methods (`get_evaluation_spec`, `get_policy_review`,
`get_policy_proposal_context`, `get_experience_evidence`) rather than new
raw SQL joins, and is covered by
`test_full_provenance_traversal`.

**Known, deliberate limitation**: Stage 9 does not extend Stage 8 to accept
a brand-new lifecycle candidate (a rewrite/promote/demote/rollback
candidate) as an evaluation target before it is applied. Doing so cleanly
would require either loosening `evaluation_specs.review_id`'s `NOT NULL`
constraint (a structural rebuild of a Stage 8 table) or forcing every
lifecycle candidate through a synthetic Stage 7 review row - and Stage 7's
`ReviewService.preview()` resolves `placement_type` from the *Stage 6
proposal*, not from the review row, so a synthetic review cannot represent
an arbitrary Stage-9-invented placement change without deeper Stage 7
changes. Per the instruction to make "the smallest backwards-compatible
extension necessary" and not redesign Stage 8, this was left as an explicit
gap for Stage 10 rather than forcing either of those two more invasive
changes into this stage. A researcher can still evaluate a lifecycle
candidate today by hand (apply it to an isolated clone and run `trajweave
eval run` style verifiers manually); only the frozen-spec convenience layer
is missing.

## Evidence aggregation

`aggregate_evidence()` computes, for one version: `total_comparisons`,
`valid_comparisons` (`improved + unchanged + regressed`), the five outcome
counts, `improved_ratio` / `regressed_ratio` (both `None`, never `0`, when
there are zero valid comparisons - an absent ratio is not the same claim as
a zero ratio), `total_regression_checks` (sum of Stage 8's per-comparison
`regression_count`), `avg_duration_delta_ms`, `policy_bytes`, and the
outcome of the most recent comparison. `invalid` and `incomparable`
comparisons are always reported but **never** enter the numerator or
denominator of a ratio - they are not silently dropped, and they are never
miscounted as success or failure.

This is a plain, inspectable aggregate. It is not a statistical test, makes
no claim about significance, and Stage 9 never presents it as one.

## Recommendation heuristics

`recommend_for_version()` is a fixed decision tree over the aggregate above,
with every threshold a named constant in `lifecycle/heuristics.py`:

| Constant | Value | Meaning |
|---|---|---|
| `MIN_VALID_FOR_SIGNAL` | 3 | Below this many valid comparisons, no directional recommendation is made at all. |
| `PROMOTE_IMPROVED_RATIO` | 0.7 | At/above this improved share, with zero regressions, promotion is considered. |
| `DEMOTE_REGRESSED_RATIO` | 0.5 | At/above this regressed share, demotion (or disable, if no narrower scope exists) is considered. |
| `MIXED_REGRESSED_LOW` | 0.2 | A regressed share at/above this, alongside real improvement, reads as "helps here, costs elsewhere" and favors demote over disable. |
| `ROLLBACK_MIN_VALID` | 3 | Both the regressing version and its candidate rollback target must individually clear the signal floor. |

Reason codes and what triggers them:

- `CONSISTENT_CROSS_SCOPE_BENEFIT` - improved ratio at/above threshold, zero
  regressions -> `promote` (or `retain` if already at the broadest scope).
- `REPEATED_REGRESSION` - regressed ratio at/above threshold -> `demote` if
  a narrower scope exists, else `disable`.
- `HIGH_WRONG_SCOPE_COST` - a moderate regressed share alongside real
  improvement, at a scope broader than the narrowest -> `demote`.
- `INSUFFICIENT_EVIDENCE` - fewer than `MIN_VALID_FOR_SIGNAL` valid
  comparisons (covers both "zero evaluations" and "only invalid/
  incomparable evaluations") -> `retain`, never a promotion.
- `NEW_VERSION_REGRESSION` - the current version regresses while its direct
  predecessor version's own evidence was strong -> `rollback` to that
  predecessor, with the exact target version id carried in
  `evidence.rollback_candidate_version_id`.
- `DUPLICATE_POLICY` - a separate, policy-set-wide scan
  (`detect_duplicate_policies`), not part of the per-version heuristic: two
  active policies whose current versions are byte-identical are flagged as
  merge candidates.
- `STALE_TARGET` - a separate, non-heuristic signal (`detect_stale`): the
  registered project a version targets no longer exists, or its repository
  root no longer exists on disk. This is always advisory - Stage 9 never
  auto-prunes on staleness, and it is never inferred from age alone.

Recommendation ids are deterministic hashes of `(policy, version, operation,
evidence snapshot)`, so calling `recommend` repeatedly with unchanged
evidence returns the same row rather than growing the table; when the
evidence has genuinely changed, the previous `open` recommendation for that
policy/version is marked `superseded` and a new one is inserted - the
history of what was recommended and when is itself append-only.

The five documented scenarios from the spec are locked down exactly in
`tests/unit/test_lifecycle_heuristics.py`:

- **A** (8 improved, 1 unchanged, 0 regressed) -> `promote`,
  `CONSISTENT_CROSS_SCOPE_BENEFIT`, strength `strong`.
- **B** (2 improved, 6 regressed) -> `demote` (or `disable` at the narrowest
  scope), `REPEATED_REGRESSION`.
- **C** (no valid evaluations) -> `retain`, `INSUFFICIENT_EVIDENCE` - never a
  strong promotion.
- **D** (only invalid/incomparable) -> `retain`, `INSUFFICIENT_EVIDENCE`,
  `evidence.improved_ratio is None` - never treated as improvement evidence.
- **E** (new version regresses; prior version's evidence was strong) ->
  `rollback`, `NEW_VERSION_REGRESSION`, with the exact predecessor version
  id named.

**This is a deterministic recommendation heuristic, not a scientifically
proven optimal lifecycle strategy.** The thresholds are reasonable,
documented starting points chosen to make Stage 9's behavior predictable and
testable; they are not derived from a benchmark, and no claim is made that
they are optimal, statistically significant, or generalize across projects.
Establishing that is explicitly Stage 10's job.

## Lifecycle operations

Every operation below is independently callable, requires an already-`active`
policy (or, for merge, two already-active policies), and is fully covered by
`tests/integration/test_stage9_lifecycle.py`.

- **Rewrite** - new version, same placement/scope, new content. Rejects
  empty content and content identical to the current version (nothing to
  do). Old version becomes `superseded`; never mutated.
- **Promote** / **Demote** - move along the fixed adjacency
  `scoped_rule/skill -> project_rule -> global_rule` (and `skill` as an
  explicit demote target from `project_rule`/`scoped_rule`). Invalid
  transitions (e.g. promoting past `global_rule`, demoting past `skill`)
  raise. **Promoting to `global_rule` requires an explicit
  `confirm_global=True`** on both `promote()` and, independently, `apply()`
  - two separate gates, one at version-creation time and one at the actual
    filesystem-write time, since Stage 7 itself provides no such gate at
    all.
- **Merge** - combines two policies' current versions into a brand-new third
  policy; both parents are recorded as `merge_parent` lineage and are left
  completely untouched (status, current version, and history all
  unchanged) - "merge" only ever creates, it never deletes or auto-disables
  the sources. Self-merge is rejected. Merging with incompatible scopes is
  rejected unless `allow_cross_scope=True`. Calling merge again with the
  same two parents and the same resulting content is idempotent - it
  returns the existing merged policy id rather than creating a duplicate.
- **Split** - creates one or more new sibling policies from a source policy,
  each linked as a `split_child` of the source version. The source policy
  is left untouched. **Every child starts with `policies.status =
  'disabled'`** - splitting can never silently activate a new policy.
  Split requires at least one child; an exact-duplicate child (same
  content hash, placement, and scope as an already-existing child of the
  same source version) is deduplicated rather than creating a corrupt
  second lineage edge.
- **Disable** / **Enable** - a pure `policies.status` toggle
  (`active <-> disabled`). Content, versions, evidence, and lineage are
  completely unaffected; disabling twice or enabling an already-active
  policy raises rather than silently no-op-ing.
- **Prune** - stronger than disable (`active`/`disabled` -> `pruned`).
  Nothing is deleted: the policy row, every version, all evidence links, and
  the full action history remain queryable. Pruning is intentionally
  one-directional in this stage - `enable()` on a pruned policy raises
  (see "Adversarial findings").
- **Rollback** - creates a new version whose content, placement, and target
  metadata are an **exact** copy of a named earlier version (verified
  byte-for-byte in tests). The version being rolled back *from* is marked
  `rolled_back` (not merely `superseded`, so it is distinguishable from an
  ordinary supersession), and an explicit `rollback_source` lineage edge
  records which version was restored. Rolling back to a nonexistent version
  or to the currently-active version both raise.

## Reusing Stage 7's safe-apply layer

`LifecycleService.preview()` / `apply()` call `resolve_target` /
`build_preview` / `apply_preview` from `trajweave.review.targets` directly -
the identical, unmodified module Stage 7 and Stage 8 already use. Every
Stage 7 protection therefore applies to Stage 9 file writes unmodified:
managed-section markers preserve human-authored content, atomic same-
directory replacement, `O_NOFOLLOW` directory-fd walking against path
traversal and symlink escape, binary/non-UTF-8 rejection, and a hash
re-check between preview and apply that refuses a stale or externally
changed target.

The one structural change from Stage 7's own usage: `managed_key()` is
salted with the logical `policy_id` (passed in place of an experience id),
not a Stage 6 experience id. This means a rewrite, promote, or demote all
update the *same* managed block in the target file in place, rather than
leaving an orphaned old block behind under a stale key -
`test_preview_apply_writes_and_managed_key_stable_across_rewrites` asserts
exactly one `trajweave:managed` block remains after two rewrites.

**A real, pre-existing Stage 7 bug was found and fixed while building this
layer** (see "Adversarial findings").

Preview/apply do not attempt to bake `target_agent`/`target_override`
overrides back into the immutable version row - by design, an override is a
one-shot convenience for a single preview/apply call, since a version's
frozen fields cannot be changed after creation without violating
immutability. A stable target should be set correctly on the version itself
(via the `target_agent`/`target_override` parameters already accepted by
`rewrite`/`promote`/`demote`), not patched in at apply time.

## Stale-knowledge detection

`detect_stale()` checks whether a version's target project is still
registered and whether its repository root still exists on disk, and
returns a list of string reasons (e.g. `target_project_not_registered`,
`target_repository_missing`). This is surfaced through
`LifecycleService.stale_signals()` and the `STALE_TARGET` reason code - it
is **always a reviewable signal**, never an automatic prune, and old age
alone is never treated as staleness.

## Human review, explicit apply, and no automatic mutation

Every operation that changes what a policy *is* (rewrite/promote/demote/
merge/split/rollback/disable/enable/prune) is a plain, synchronous method
call a human explicitly invokes - by CLI command, or a POST request they
chose to send from the UI - and it only ever writes to the append-only
lifecycle tables. **No operation ever touches a real file.** Writing to disk
requires a *separate*, explicit `preview()` followed by a separate, explicit
`apply()` call; `apply()` additionally refuses to run if no preview exists,
and refuses to run against a stale preview. There is no code path from "an
evaluation regressed" or "an evaluation improved" to a filesystem write -
`accept_recommendation()` (a human explicitly accepting an open, computed
recommendation) still only creates a new version or toggles a status; it
never previews or applies.

`Recommend` / `accept-recommendation` / `reject-recommendation` /
`defer-recommendation` give a lightweight review layer over recommendations
specifically. Direct operation verbs (`promote`, `demote`, ...) remain the
primary CLI/service surface, since - unlike Stage 7's proposal-centric
review, which exists to gate a *specific pre-generated Stage 6 candidate* -
a human running `trajweave lifecycle promote <policy>` on their own machine
already *is* the explicit human decision the spec requires; the safety
property that matters (no filesystem write without a separate, explicit
apply) holds regardless of which entry point created the version.

## CLI

```text
trajweave lifecycle list [--status STATUS]
trajweave lifecycle show <policy-id>
trajweave lifecycle history <policy-id>
trajweave lifecycle adopt <review-id>
trajweave lifecycle recommend <policy-id>
trajweave lifecycle duplicates
trajweave lifecycle rewrite <policy-id> --content TEXT | --file PATH
trajweave lifecycle promote <policy-id> [--target-placement ...] [--confirm-global]
trajweave lifecycle demote <policy-id> [--target-placement ...]
trajweave lifecycle merge <policy-a> <policy-b> [--content TEXT] [--allow-cross-scope]
trajweave lifecycle split <policy-id> --children-file children.json
trajweave lifecycle disable|enable|prune <policy-id>
trajweave lifecycle rollback <policy-id> <version-number>
trajweave lifecycle preview <policy-id> [--project PATH]
trajweave lifecycle apply <policy-id> [--project PATH] [--dry-run] [--confirm-global]
trajweave lifecycle accept-recommendation|reject-recommendation|defer-recommendation <recommendation-id>
```

Every subcommand supports `--json` for machine-readable output, matching the
`review`/`eval` command conventions exactly (`--home` override, plain-text
vs JSON dual output, `func=` dispatch).

## UI

A minimal read-mostly "Lifecycle" API area was added to the existing local,
loopback-only HTTP server (no new server-rendered page, no third-party
dependency):

- `GET /api/lifecycle` - list, with the same `{"policies": [],
  "schema_ok": false}` clean empty-state shape as `/api/evals`/`/api/reviews`
  for a pre-migration or empty database.
- `GET /api/lifecycle/<policy-id>` - full show payload (versions, lineage,
  evidence, recommendations, action/preview/application history).
- `POST /api/lifecycle/<id>/<action>` - a **fixed whitelist**:
  `recommend`, `rewrite`, `disable`, `enable`, `prune`, `rollback`,
  `preview`, `apply`, `accept-recommendation`, `reject-recommendation`,
  `defer-recommendation`. Any other action returns 404. A GET request can
  never mutate or apply anything; `apply` still requires a prior `preview`
  exactly as the CLI/service do.

## Zero-policy behavior

A real database with zero adopted policies returns a clean state
everywhere: `trajweave lifecycle list` prints "No lifecycle policies yet.",
`trajweave lifecycle duplicates` reports nothing to merge, `/api/lifecycle`
returns `{"policies": [], "schema_ok": true|false}` depending on whether the
schema has migrated yet, and no test fixture data ever leaks into a
non-test database - test fixtures are constructed entirely inside
`tests/`, via either the full trajectory-to-review pipeline or direct
`Repository` calls, never via any production code path.

## Append-only enforcement

Beyond the service layer exposing no update/delete API for historical rows,
migration `0007_lifecycle.sql` adds `BEFORE UPDATE`/`BEFORE DELETE` SQLite
triggers on `policy_versions` (blocking any change to `content`,
`content_hash`, `placement_type`, or the other identity fields, and
blocking any delete), `policy_lifecycle_actions`, and `policy_lineage`
(blocking update and delete on both). These are defense in depth, not a
substitute for the service-layer discipline: the application code never
issues these statements, but a direct SQL statement against the database
file - accidental or adversarial - now cannot silently rewrite lifecycle
history either.
`test_db_level_triggers_block_direct_mutation_of_history` proves this
directly.

## Adversarial findings

Investigated during implementation; one genuine, pre-existing bug was found
and fixed, plus the cases below were proven to fail safely:

1. **Stage 7 (and, before the fix, this stage's own) `apply()` could report
   "already_applied" without ever writing new content.** Root cause: the
   pre-write short-circuit compared the freshly-computed render's
   `output_hash` against the *preview record's own* `output_hash` - which
   are trivially equal whenever nothing has changed between `preview()` and
   `apply()`, regardless of whether the file on disk actually has that
   content yet. Reproduced end-to-end against `ReviewService` directly:
   accept -> preview -> apply (writes v1) -> edit -> accept -> preview ->
   apply reported `already_applied` and left the file at v1's content. Fixed
   in both `review/service.py` and `lifecycle/service.py` by comparing the
   render's `before`/`after` text (what is on disk right now vs. what would
   be written) instead. Locked down by a new regression test in
   `tests/integration/test_stage7_review.py` and by
   `test_preview_apply_writes_and_managed_key_stable_across_rewrites` here.
2. **Version immutability** - a direct `UPDATE`/`DELETE` against
   `policy_versions`/`policy_lifecycle_actions`/`policy_lineage` is rejected
   by DB trigger, not merely discouraged by convention.
3. **Rollback correctness** - restored content/placement/target verified
   byte-for-byte equal to the original version; rollback to a
   nonexistent version number and rollback to the currently-active version
   both raise.
4. **Merge lineage / self-merge / duplicate merge / incompatible scope** -
   self-merge raises; an identical repeat merge returns the same result
   rather than duplicating; a cross-scope merge without
   `allow_cross_scope=True` raises; both parents remain independently
   queryable and unmodified after a merge.
5. **Split lineage / no children / duplicate children** - zero children
   raises; an exact-duplicate child does not create a second lineage edge;
   every child starts `disabled`, proven by attempting (and failing) to
   preview a freshly-split, still-disabled child.
6. **Pruned-policy reactivation** - `enable()` on a `pruned` policy raises;
   pruning an already-pruned policy raises; the policy record, its
   versions, and its full history all remain queryable after pruning.
7. **Cross-project / wrong / deleted repository** - resolving a target
   against a project whose registered root no longer exists on disk raises
   inside Stage 7's own `resolve_target`/`_read_existing`, which Stage 9
   reuses unmodified; deleting the target repository between `preview()`
   and `apply()` is proven to raise rather than write into a stale/absent
   path.
8. **Path traversal / absolute-path escape via `target_override`** - a
   `../../etc/escape.md` override is rejected by Stage 7's own
   `SafetyError`, propagated unmodified through `preview()`.
9. **Concurrent/repeated apply** - a second `apply()` with nothing changed
   correctly reports `already_applied` and performs no write (this is the
   scenario the fix in finding 1 had to preserve, not merely stop breaking).
10. **Invalid/incomparable comparisons as positive evidence** - proven
    directly: with only `invalid`/`incomparable` comparisons present,
    `evidence.improved_ratio` is `None`, never `0` or a positive number, and
    the recommendation is `retain`/`INSUFFICIENT_EVIDENCE`, never a
    promotion.
11. **Database reopen** - lifecycle state (versions, current-version
    pointer, history) is proven identical after closing and reopening the
    database connection.
12. **Stage 8 -> Stage 9 migration / fresh database creation** - both
    covered by `tests/unit/test_lifecycle_migration.py`.

## Validation

- Starting baseline (feat/stage8, `a73d249`): 290 passed, 0 failed.
- Full suite after Stage 9: **349 passed, 0 failed** (290 original + 1
  Stage 7 regression test added for the `already_applied` fix + 58 new
  Stage 9 unit/integration tests). No existing test was removed, skipped, or
  weakened to reach green.
- Schema: fresh database reaches version 7 in one step; a database built
  with only Stage 0-6 migrations plus real Stage 7/8 rows migrates to
  version 7 with all pre-existing rows intact and readable.

## Known limitations

- Evaluating a brand-new lifecycle candidate (rewrite/promote/demote/
  rollback) before applying it is not wired into Stage 8's frozen-spec
  convenience layer - see "Linking Stage 8 evidence" above. Manual
  evaluation is still possible; only the one-command bridge is missing.
- `promote`/`demote`/`merge`/`split` scope fields (`scope_type`,
  `scope_value`) are caller-supplied, not auto-inferred from evidence -
  Stage 9 does not attempt to guess a "correct" narrower/broader scope from
  outcome data; the operator supplies it explicitly, matching the
  instruction to keep the heuristic simple and explainable rather than
  build a scope-inference model.
- The DUPLICATE_POLICY scan is exact byte-identical content matching, not
  semantic similarity - a near-duplicate with different wording is not
  flagged.
- Recommendation thresholds are fixed constants tuned for explainability and
  testability, not fit to any benchmark; see "This is a deterministic
  recommendation heuristic, not a scientifically proven optimal lifecycle
  strategy" above.
- The UI's lifecycle area is API-only (no dedicated static page was added
  beyond the existing SPA shell) - it is exercised directly over HTTP in
  tests, matching how the Stage 8 evaluation API was introduced.

## Explicit confirmation

- Stage 10 benchmark logic (multi-repo suites, no-memory/static-instruction/
  append-all/skills-only/heuristic-placement baselines, statistical
  significance testing, leaderboards) was **not** implemented.
- Stage 11 paper logic (claims, tables, manuscript) was **not** implemented.
- Existing Stage 0-8 behavior remains regression-safe (290/290 original
  tests still pass unchanged, plus one Stage 7 correctness bug found and
  fixed with its own regression test).

## Stage 10 readiness

Stage 9 gives Stage 10 a durable, queryable record of every lifecycle
decision ever made and why - append-only actions, explicit lineage, and
evidence-backed recommendations with named reason codes rather than opaque
scores. Stage 10 can now ask, across many policies and many repositories,
whether Stage 9's fixed thresholds actually correlate with good long-run
outcomes, and can replace or augment `lifecycle/heuristics.py`'s constants
with statistically justified ones without touching the immutable version
model, the lineage schema, or the safe-apply path underneath it - exactly as
Stage 9 was able to build on top of Stage 8's evaluation schema without
touching it.
