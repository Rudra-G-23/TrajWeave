# TrajWeave - Stage 6 Completion Report

Stage 6 provides a deterministic, evidence-backed Placement Engine. It proposes
where a reusable Stage 5 Experience could live. It does not modify a repository,
write `AGENTS.md` or `CLAUDE.md`, create a skill directory, or apply knowledge.

## Architecture

```text
Stage 5 candidate Experience + occurrences
  -> feature extraction
  -> five competing canonical alternatives
  -> deterministic scores, diagnostics, and ranks
  -> persisted current proposal set with evidence links
  -> CLI and read-only UI
```

`trajweave.placement.build_proposals(experience, evidence, events_by_trajectory)`
is pure. `PlacementGenerator` only rehydrates stored Stage 5 data, fingerprints
the input, and persists its result. This leaves Stage 8 able to reuse the exact
feature and scoring implementation.

## Schema and regeneration

Only `status = candidate` Experiences are eligible. Reviews of `false_positive`
and `needs_more_evidence` exclude and invalidate a set. Stage 5 thresholds and
detectors are unchanged.

Migration `0003_placement.sql` advances SQLite to schema v3. It adds:

- `placement_runs` - generator version and run counts.
- `placement_proposal_sets` - one current, fingerprinted set per Experience.
- `placement_proposals` - every alternative's type, scope, content, score,
  rank, feature values, and diagnostics.
- `placement_proposal_evidence` - proposal-to-occurrence evidence traceability.

Stable IDs are `PS-E-0001` and `PP-E-0001-project_rule`. Matching fingerprint
and version preserve a set unchanged. Experience rebuilds cascade obsolete
derived sets, while Stage 5 review annotations remain intact. Stored data keeps
Experience -> alternatives -> features -> scores -> rank -> recommendation ->
future human/oracle review.

## Features, scope, and content

Persisted inputs include support, contradiction, and ambiguity counts;
trajectory/project breadth; support ratio; recurrence; cross-project support;
dominant-project concentration; context consistency; Stage 5 confidence and
persisted recency; actionability; procedure complexity; scope facts; and the
Global gate.

Scope uses only path, command, context, and ordered-event evidence inside a
supporting occurrence interval. It requires two supports and 75% concentration.
Absolute, home-relative, URI, and upward-traversal paths are rejected. Scope
ties use directory, subsystem, command, framework, language, extension, then
lexical value. Whole-session file aggregates are deliberately not used.

Canonical knowledge remains separate from placement. It is sanitized Stage 5
reusable lesson text, falling back to summary then title, capped at 500
characters. Private absolute paths are replaced by `[repository path omitted]`.
Terminal output is never used.

## Exact scoring and ranking

Scores are clipped to `[0, 1]`, rounded to four decimals. With `r` support
ratio, `c` contradiction rate, `n` recurrence, `x` cross-project support, `p`
project concentration, `s` scope concentration times specificity, `a`
actionability, `q` Stage 5 confidence, `k` context consistency, and `w`
procedure complexity:

```text
Ignore       = .10 + .35(1-r) + .40c + .15(1-a) + .10(1-q) + .10 if support < 2
Global Rule  = .05 + .24r + .22n + .35x + .14a + .10q - .10p - .15s - .20c
Project Rule = .10 + .20r + .20n + .25p + .15a + .10q - .12s - .25x - .40c
Scoped Rule  = .05 + .16r + .10n + .40s + .12a + .07q + .05k - .35c
Skill        = .03 + .15r + .10n + .50w + .12a + .08q - .10c
```

Global is capped at `0.24` unless there are at least two supporting projects,
each has two support occurrences, and no project owns over 75% of support.
Skill requires repeated multi-operation evidence. Exact score ties rank in fixed
safety-first order: Ignore, Global, Project, Scoped, Skill. Diagnostics derive
from the same feature values as scoring.

## CLI and UI

```text
trajweave placements generate [--json]
trajweave placements list [--type TYPE] [--recommended TYPE] [--json]
trajweave placements show E-0007 [--json]
```

The CLI exposes counts, recommendations, alternatives, scopes, diagnostics, and
evidence. There is intentionally no apply command. Experience detail now has a
read-only Placement section. `/api/placements` lists recommendations and
`/api/placements/<experience-id>` returns the full trace. Private roots are
removed from CLI/UI payloads.

## Controlled fixtures and tests

Controlled fixtures demonstrate Global, Project, Scoped, Skill, Ignore,
ambiguous alternatives, and cross-project contamination. They test exact ties,
privacy, malformed context, deterministic rankings, evidence traceability,
migrations, idempotency, regeneration, CLI, UI, and zero-candidate behavior.
The integration fixture creates an actual Stage 5 candidate by running the
Stage 5 extractor over normalized stored trajectories before generating proposals.
It never writes the real database.

Full suite: **228 passed, 0 failed, 0 skipped**.

## Overgeneralization analysis

1. Local evidence cannot become Global without balanced repeated support.
2. One unrelated second-project occurrence fails the Global gate.
3. One-step instructions do not become Skills.
4. Repeated procedures retain a canonical Skill alternative rather than a
   generated policy file.
5. Exact occurrence-interval evidence, not filename coincidence, creates scope.
6. Contradictions raise Ignore and reduce rule scores.
7. No undocumented free-text semantic score exists.
8. Identical stored input produces the same fingerprint, scores, and ranks.
9. Exact ties are deterministic and tested.
10. Absolute/private paths are rejected for scope and scrubbed from content.

## Real-data validation

The actual local TrajWeave database was migrated and then ran:

```text
Eligible experiences: 0
Placement proposal sets: 0
```

This is expected under unchanged Stage 5 thresholds. No proposal row was
created, and no Stage 5 evidence, repository, policy file, or skill directory
was modified.

## Known limitations and Stage 7 readiness

Only the current derived set is retained, not superseded snapshots. Framework
scope awaits explicit stored evidence. Canonical content is deterministic Stage
5 text, not authored prose. These are conservative proposal-only limitations.

Stage 7 can consume stable proposal IDs, all alternatives, feature snapshots,
diagnostics, and occurrence links without a Stage 6 redesign. Stage 7 alone
must own approval, agent-specific rendering, and repository mutation.
