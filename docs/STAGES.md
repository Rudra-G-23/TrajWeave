# TrajWeave stages and data

> Every run makes the next one better.

This is the concise product map. The individual `STAGE_*_REPORT.md` files are historical implementation reports with deeper validation details and known limitations.

## The learning loop

| Stage | What happens | Main data produced |
| --- | --- | --- |
| 0 | Establish the package and CLI foundation. | Project structure and commands. |
| 1 | Register repositories explicitly. | Projects and repository markers. |
| 2 | Discover and parse Codex and Claude session formats. | Source sessions and adapter records. |
| 3 | Normalize events, paths, statuses, and redacted content. | Trajectories, events, and file touches. |
| 4 | Inspect imported work locally. | Read-only CLI and UI views. |
| 5 | Detect recurring patterns from trajectory evidence. | Candidate experiences and evidence links. |
| 6 | Rank possible homes for an experience. | Placement proposal sets and diagnostics. |
| 7 | Let a person review and explicitly apply a proposal. | Reviews, variants, previews, actions, and apply outcomes. |
| 8 | Compare the same task with and without a reviewed policy. | Frozen evaluation specs, runs, checks, and comparisons. |
| 9 | Use accumulated evaluation evidence to recommend policy lifecycle actions. | Lifecycle decisions, evidence, and policy versions. |

## Data flow

```text
Codex / Claude session files
        |
        v
source_sessions
        |
        v
trajectories + trajectory_events + trajectory_files
        |
        v
experiences + experience_occurrences + experience_evidence
        |
        v
placement_proposal_sets + placement_proposals
        |
        v
policy_reviews + policy_review_variants + previews + applications
        |
        v
evaluation_specs + evaluation_runs + comparisons
        |
        v
lifecycle decisions + policy versions
```

## Boundaries

- All data stays local under `~/.trajweave/` unless `TRAJWEAVE_HOME` changes the location.
- A repository must be registered before its sessions are parsed.
- Experiences and placement proposals are recommendations, not repository edits.
- Accepting a proposal records a decision. It does not apply a file.
- Applying a proposal requires a current preview and explicit confirmation.
- Evaluation runs are isolated from the developer's active checkout.
- Lifecycle recommendations are evidence-backed and do not silently rewrite policy.

## Where to look next

- [`STAGE_0-3_REPORT.md`](STAGE_0-3_REPORT.md) - foundation through normalized data.
- [`STAGE_4_REPORT.md`](STAGE_4_REPORT.md) - local inspection UI.
- [`STAGE_5_REPORT.md`](STAGE_5_REPORT.md) - experience extraction.
- [`STAGE_6_REPORT.md`](STAGE_6_REPORT.md) - placement proposals.
- [`STAGE_7_REPORT.md`](STAGE_7_REPORT.md) - review and apply safety model.
- [`STAGE_8_REPORT.md`](STAGE_8_REPORT.md) - paired evaluation.
- [`STAGE_9_REPORT.md`](STAGE_9_REPORT.md) - lifecycle learning.
