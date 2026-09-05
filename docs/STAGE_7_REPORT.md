# TrajWeave Stage 7 Report - Review and Apply

## Scope

Stage 7 adds the human-in-the-loop layer between deterministic Stage 6 placement
proposals and repository policy files. Stage 6 remains unchanged and Stage 8
baseline-vs-policy evaluation is not included.

The key invariant is:

```text
Accept != Apply
```

Accept records approval of an exact proposal. It never writes `AGENTS.md`,
`CLAUDE.md`, `skills/`, or any other repository file. Apply requires a prior
Accept, a persisted Preview, a current target hash, and an explicit command or
UI confirmation.

## Architecture

- `trajweave.review.service.ReviewService` owns the state machine and shared
  CLI/UI behavior.
- `trajweave.review.targets` resolves safe targets, renders managed blocks,
  creates unified diffs, validates target content, and performs atomic writes.
- Stage 6 proposal rows remain immutable. Review rows snapshot the selected
  proposal and source fingerprint instead of replacing it.
- Project and Scoped Rules resolve against the registered canonical project
  root. Global Rules use the approved TrajWeave home policy subtree by default.
  Skills use a deterministic repository `skills/<safe-slug>/SKILL.md` target.
- The UI remains loopback-only. GET requests are read-only; review mutations
  use explicit POST action endpoints.

## Schema

Migrations 0004 and 0005 raise the schema to version 5 and add:

- `policy_reviews`: current state, selected alternative, target metadata,
  edited content variant, Stage 6 source fingerprint, and proposal snapshot.
- `policy_review_actions`: append-only review, selection, preview, and apply
  history.
- `policy_review_previews`: exact target hash, output hash, content, and
  unified diff for the latest preview.
- `policy_review_variants`: immutable edited content revisions for review and
  Stage 8 handoff.
- `policy_applications`: applied, no-op, refused, and pending application
  outcomes.

Original Stage 6 proposal IDs and occurrence links remain available. An applied
policy therefore traces from application to review, proposal snapshot,
Experience, occurrence rows, trajectories, and event rows.

## Review states and actions

Current states are `unreviewed`, `accepted`, `rejected`, `deferred`,
`test_first`, `applied`, and `stale`.

Edits are stored as a separate content override with an incrementing revision
and return the review to `unreviewed`. Choosing another placement alternative
also returns the review to `unreviewed`. The edited or newly selected variant
must be explicitly accepted again.

`Test first` persists the reviewed variant and provenance for Stage 8. Stage 7
does not evaluate it or apply it. Ignore is a Stage 6 alternative and has no
filesystem target.

If Stage 6 regenerates a proposal set with a different source fingerprint, the
old review is retained as history and treated as stale. A new review and
preview are required.

## CLI

```text
trajweave review list [--status STATUS]
trajweave review show <proposal-or-review-id>
trajweave review accept <id> [--agent codex|claude] [--target PATH]
trajweave review reject <id>
trajweave review defer <id>
trajweave review test-first <id>
trajweave review edit <id> --content TEXT
trajweave review edit <id> --file PATH
trajweave review choose <id> --placement TYPE
trajweave apply <id> --dry-run
trajweave apply <id>
```

Both proposal IDs and durable review IDs resolve to the same review where
possible. Dry-run creates or refreshes only the database preview ledger and
prints the exact diff with `Writes: no`.

## UI

The local UI adds a Review queue and review detail view. It shows the
Experience, recommendation, alternatives, diagnostics, evidence, editable
content, selected agent and target, review status, and exact diff. Accept,
Reject, Defer, Test first, Edit, Choose, Preview, Dry-run, and Apply controls
are available through explicit actions. Apply is visually separated and
requires a browser confirmation after Preview.

POST endpoints are under `/api/reviews/<id>/` for `accept`, `reject`, `defer`,
`test-first`, `edit`, `choose`, `preview`, and `apply`. A state-changing error
is returned without writing a repository file.

## Rendering and apply behavior

Policy files use a deterministic managed block:

```text
<!-- trajweave:managed key="TW-POLICY-..." -->
approved policy text
<!-- trajweave:end -->
```

Only that exact TrajWeave-owned block is inserted or updated. Existing human
content is preserved. Reserved marker syntax in approved content, malformed
markers, duplicate managed blocks, binary data, and invalid UTF-8 are refused.

Existing selected-agent files are used only when the user selects that agent.
TrajWeave never duplicates a rule into both `AGENTS.md` and `CLAUDE.md`.
Missing text targets are created after preview. Skill files receive minimal
frontmatter and a managed block.

Apply rechecks the target hash. An unchanged exact managed block reports
`already_applied` and performs no rewrite. A changed target requires a new
preview. A human edit inside an existing managed block is not overwritten.

Writes use a temporary file, complete writes with fsync, a directory file
descriptor with no-follow checks, and an atomic same-directory replacement.
Project and Scoped targets must remain below the registered project root.
Global targets must remain below the approved global policy root. Symlinked
targets, path traversal, absolute paths outside the approved root, directories,
and binary targets are rejected.

An application intent is recorded as `pending` before replacement and finalized
afterward. A pending intent blocks subsequent writes until it is reconciled.

## Controlled fixtures and adversarial findings

Temporary repositories and isolated databases cover:

- Project Rule, Scoped Rule, Global Rule, Skill, and Ignore target resolution.
- Reject, Defer, Edit, Test first, alternative selection, and missing targets.
- Existing `AGENTS.md` content preservation and no whole-file regeneration.
- Duplicate Apply and exact no-op behavior.
- Stale preview after human edits.
- Missing projects, malformed markers, binary targets, symlinks, and traversal.
- UI Accept and Dry-run with no repository write.

The adversarial checks found and fixed:

1. Dry-run initially required an existing preview, so it now safely creates a
   database preview without touching the target.
2. Duplicate Apply initially treated a missing target as already applied when
   hashes matched; the check now distinguishes a missing target from an exact
   existing output.
3. Atomic writes now handle partial `os.write` results rather than assuming one
   write completes the temporary file.

No tested path allows viewing, Accept, Reject, Defer, Edit, Test first, or
Ignore to modify a repository target.

## Validation

- Full regression and Stage 7 fixture suite: 241 passed, 0 failed.
- Real database: expected zero eligible experiences and zero proposal sets;
  Stage 7 reviewable proposals and applied policies remain zero.
- No Stage 5 or Stage 6 thresholds were weakened and no production candidates
  were inserted.

## Known limitations

- Global policy configuration is represented by the TrajWeave-owned home
  policy subtree; a separate configuration command is not included.
- The UI is local and loopback-only and has no multi-user authentication.
- Pending application recovery is fail-closed and requires future explicit
  reconciliation support if a process crashes after replacement.
- Stage 8 evaluation is not implemented.

## Stage 8 readiness

Stage 8 can consume the durable review state, selected placement, edited
content revision, target metadata, Stage 6 source fingerprint, proposal
snapshot, and complete evidence links. It can evaluate `test_first` variants
without redesigning the Stage 7 schema or reading an overwritten Stage 6 row.
