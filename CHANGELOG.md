# Changelog

## 0.5.0 - 2026-09-19

### Added

- Built the Stage 0-3 data substrate:
  - Python package layout with a stdlib-only runtime and the `trajweave` CLI.
  - Explicit repository registration with deterministic project ids, on-disk markers, idempotent initialization, and non-destructive handling of missing repositories.
  - Codex and Claude session discovery and parsing, including Claude sub-agent transcripts and a Codex legacy-stream fallback.
  - Tolerant JSONL ingestion that records parse warnings without aborting an entire import.
  - A normalized trajectory model for tasks, events, file touches, statuses, timestamps, command categories, and provenance.
  - Conservative secret redaction, repository-relative paths, injected-context filtering, and evidence-based final-status inference.
  - SQLite storage with versioned migrations for projects, source sessions, trajectories, events, and file touches.
  - Restart-safe, deduplicating historical import with dry-run mode, filters, failure isolation, and re-import in place.
  - Read commands for projects, sessions, trajectories, and detailed trajectory inspection.

- Added the Stage 4 local trajectory explorer:
  - Read-only HTTP server bound to `127.0.0.1` with a vanilla single-page UI.
  - Projects, sessions, trajectory detail, timeline, files, commands, errors, metadata, and import-ledger views.
  - Agent/status/search filters, pagination, sub-agent indicators, empty states, schema error handling, and safe read-only database access.
  - `trajweave ui` with port selection, no-browser mode, and packaged static assets.
  - Corrected command classification so incidental words, paths, help commands, and version checks do not create phantom test, lint, or build failures.

- Added the Stage 5 deterministic experience extractor:
  - Versioned experience schema, occurrence tracking, evidence links, extraction state, and run bookkeeping.
  - Detection for failure-repair-success, meaningful human-correction repair, repeated diagnostic failures, and file-change patterns.
  - Stable grouping, contradiction synthesis, candidate thresholds, review annotations, recency, recurrence, cross-project, and support scoring.
  - Deterministic summaries with an explicit, currently unwired LLM summarization seam.
  - CLI commands for extracting, listing, showing, and reviewing experiences.
  - UI experience list/detail pages with evidence links back to highlighted session ranges.
  - Real-data precision pass that removed false positives and preserves a conservative no-candidate outcome when evidence is insufficient.

- Added the Stage 6 deterministic placement engine:
  - Placement proposals, scope modeling, scoring, ranking, diagnostics, and regeneration from current evidence.
  - Candidate destinations for scoped rules, project rules, global rules, and skills with explicit scope constraints.
  - Storage, CLI, UI views, controlled fixtures, and tests for proposal generation and ranking.

- Added the Stage 7 review-and-apply workflow:
  - Review records, proposal variants, previews, decisions, and apply outcomes with append-only history.
  - Explicit accept, reject, defer, and revise actions with stale-proposal detection.
  - Deterministic rendering for rule, skill, and instruction-file targets.
  - Preview-before-write and explicit-confirmation gates for filesystem changes.
  - Atomic writes, parent-directory creation, backups, conflict protection, path safety, and protection against shared system directories.
  - CLI and UI support for reviewing proposals, inspecting variants, previewing content, and applying approved changes.

- Added the Stage 8 paired evaluation engine:
  - Frozen evaluation specifications and reproducible evaluation runs.
  - Isolated temporary workspaces so evaluations do not modify the active checkout.
  - Baseline-versus-policy comparisons, verifier results, regression checks, efficiency metrics, repeated trials, and provenance.
  - Reuse of the Stage 7 safe-apply layer for controlled policy materialization.
  - CLI and UI access to evaluation specs, runs, and comparisons, including zero-candidate behavior.
  - Controlled fixtures and adversarial validation for path traversal, stale inputs, failed commands, malformed results, and isolation guarantees.

- Added the Stage 9 policy lifecycle learning system:
  - Logical policies with immutable versions, lineage, current-version tracking, and append-only lifecycle history.
  - Evidence aggregation from Stage 8 comparisons with explicit treatment of invalid and incomparable results.
  - Deterministic recommendations for retain, promote, demote, disable, rewrite, merge, and rollback operations.
  - Thresholded heuristics for insufficient evidence, consistent benefit, repeated regression, wrong-scope cost, new-version regression, duplicate policies, and stale targets.
  - Explicit confirmation gates for global promotion and filesystem application.
  - Lifecycle operations that preserve parent policies and prior versions rather than mutating or deleting history.
  - CLI and UI support for lifecycle inspection, recommendations, evidence, and safe application.
  - Full provenance traversal from lifecycle decisions through policy versions, reviews, placement proposals, experiences, trajectories, and source sessions.

### Validation

- Expanded automated coverage from the initial Stage 0-3 suite through the complete Stage 9 feature set, including unit, integration, migration, UI, adversarial, and lifecycle tests.
- Validated Codex and Claude ingestion against real local session stores using throwaway databases and read-only inspection.
- Verified import idempotency, re-import behavior, redaction, project mapping, event normalization, UI traversal, proposal safety, evaluation isolation, and lifecycle recommendation scenarios.

### Known boundaries

- All data remains local by default under `~/.trajweave/`.
- Recommendations, reviews, and evaluations do not silently mutate repositories.
- Lifecycle candidates that require evaluating a brand-new unapplied policy version remain a documented Stage 10 gap.
- The documented reports in `docs/STAGE_*_REPORT.md` contain the detailed implementation evidence and limitations for each stage.
