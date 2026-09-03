# TrajWeave - Stage 5 Completion Report

Scope delivered: **deterministic Experience extraction**. TrajWeave now reads
the normalized trajectory store, detects recurring success / failure patterns
by explicit rules (no LLM, no network, no randomness), aggregates them across
trajectories into evidence-backed **candidate** experiences with a documented
confidence score, persists them in SQLite, and exposes them through the CLI
and the read-only UI with every experience traceable back to real session
events.

What Stage 5 deliberately does **not** do (unchanged from the brief's
non-negotiables): it never edits `AGENTS.md` / `CLAUDE.md`, never writes a
skill or a rule into any repository, never mutates project source, never
auto-applies a lesson, never decides final placement (that is Stage 6), and
never requires a paid API or any cloud service. No raw code, transcript text,
or secret leaves the machine. Zero third-party runtime dependencies.

---

## Implemented

- **`experiences` CLI command group** (`src/trajweave/cli/main.py`):
  - `trajweave experiences extract` - run detection + grouping. Flags:
    `--project <path|id>`, `--rebuild`, `--min-occurrences`, `--max-event-gap`,
    `--min-correction-tokens`, `--json`.
  - `trajweave experiences list [--status S] [--json]` - candidates first,
    ordered by confidence.
  - `trajweave experiences show <E-id> [--json]` - summary, reusable lesson,
    confidence breakdown, and the full evidence list (each row links a
    trajectory + sequence range).
  - `trajweave experiences review <E-id> --status <valid|false_positive|
    needs_more_evidence|unreviewed> [--note ...]` - human triage; annotations
    are keyed by `group_key` and survive `--rebuild`.
- **Extraction pipeline** (`src/trajweave/experience/`):
  - `detect.py` - per-trajectory deterministic occurrence detection.
  - `grouping.py` - cross-trajectory aggregation into experiences, including
    *synthesized* contradiction evidence.
  - `confidence.py` - the documented scoring function.
  - `summarize.py` - deterministic template summariser + an unwired LLM seam.
  - `corrections.py` / `signatures.py` / `context.py` - the small, tested
    helper filters (meaningful-correction gate, error-signature normalizer,
    file/command context classifier).
  - `extract.py` - the orchestrator: incremental detection, full-recompute
    grouping, run bookkeeping.
- **Storage** (`storage/migrations/0002_experience.sql`, `SCHEMA_VERSION = 2`):
  five new tables (below). Forward-only migration, applied automatically, and
  the UI endpoint degrades to empty (not `500`) on a pre-Stage-5 database.
- **UI** (`src/trajweave/ui/`): a new **Experiences** page (list with
  confidence pill, status tag, support/contradiction counts) and an
  **Experience detail** page (confidence-component table, reusable lesson,
  evidence table). Evidence rows deep-link to the session timeline with the
  relevant event range highlighted (`#/session/<id>?range=<start>-<end>`).
  Read-only: no review controls in the browser, extraction is CLI-only.
- **Real-data harness** `scripts/stage5_realdata.py` (not packaged) - mirrors
  the Stage 4 method: a throwaway `TRAJWEAVE_HOME`, real repo roots registered
  directly in the DB, no marker written into any real repo, no transcript
  copied or modified.

---

## Human-correction cleanup (before / after)

Stage 3/4 recorded that `human_correction` events fire on bare affirmations
("yes", "ok", "c", "/compact"). Stage 5 does **not** rewrite the stored events
(source data stays intact); it applies a small, defensible **read-time filter**
- `is_meaningful_human_correction()` in `experience/corrections.py` - before a
correction is allowed to seed a pattern.

Rule (documented, deterministic):

1. Empty / whitespace -> not meaningful.
2. Exact match against a frozen set of bare acknowledgements
   (`yes`, `y`, `yep`, `yeah`, `ok`, `okay`, `k`, `c`, `sure`, `do it`,
   `go`, `go ahead`, `continue`, `proceed`, `sounds good`, `lgtm`, `thanks`,
   `perfect`, `great`, ...) -> not meaningful.
3. More than `min_tokens` (default 3) word tokens -> meaningful.
4. Otherwise meaningful **only if** it carries a real signal: a `?`, a
   path-like token, a code-like token, an error-like token, or a directive
   word (`use`, `don't`, `never`, `instead`, `should`, `revert`, `rename`,
   `stop`, `avoid`, `fix`, `rerun`, `install`, `wrong`, `actually`, ...).

Measured on the real local corpus (254 trajectories):

| bucket | count | share |
|---|---|---|
| `human_correction` events, total | 322 | 100% |
| dropped as non-actionable (`yes` x N, `c`, `/compact`, `Implement the plan.`) | 47 | 15% |
| kept as meaningful | 275 | 85% |
| ...of which *directive* (redirect, not just elaboration) | 151 | 47% |

The `human_correction_repair` detector is stricter still (see below): it needs
a *directive* correction **and** a failing check that subsequently turns green.
On real data that leaves **1** supporting occurrence + **1** ambiguous
occurrence across the whole corpus - i.e. the noisy 322 collapse to 2 pieces
of usable evidence, and no `human_correction_repair` experience clears the
candidate bar. That is the intended outcome ("prefer 7 strong experiences over
200 noisy ones").

---

## Experience schema

Migration `0002_experience.sql`, all ids are stable text keys.

| table | purpose | key columns |
|---|---|---|
| `experience_occurrences` | one detected pattern instance in one trajectory | `id` (`O-000001`, or `OC-000001` for a synthesized contradiction), `trajectory_id` -> CASCADE, `project_id` -> SET NULL, `pattern_type`, `group_key`, `start_sequence`, `end_sequence`, `failure_family`, `resolution_family`, `repair_context`, `error_signature`, `classification` (`support` / `contradiction` / `ambiguous`), `features_json`, `dedupe_hash` UNIQUE |
| `experiences` | one aggregated candidate experience | `id` (`E-0001`), `group_key` UNIQUE, `title`, `summary`, `reusable_lesson`, `pattern_type`, `context_json`, `status` (`candidate` / `needs_more_evidence` / `rejected` / `archived`), `confidence`, `confidence_json`, `support_count`, `contradiction_count`, `ambiguous_count`, `occurrence_count`, `project_count`, `first_seen_at`, `last_seen_at`, `summary_source` (`deterministic` / `llm`), `review_status` (`unreviewed` / `valid` / `false_positive` / `needs_more_evidence`), `reviewed_at`, `review_note` |
| `experience_evidence` | experience <-> occurrence link | PK(`experience_id` -> CASCADE, `occurrence_id` -> CASCADE), `relationship` |
| `experience_extraction_state` | incremental-run bookkeeping | `trajectory_id` PK -> CASCADE, `source_hash`, `extracted_at` |
| `experience_runs` | one row per `extract` invocation | `started_at`, `finished_at`, `rebuild`, `project_filter`, `trajectories_considered`, `trajectories_analyzed`, `occurrences_found`, `clusters_formed`, `candidates_created`, `needs_more_evidence`, `llm_used`, `llm_tokens`, `runtime_seconds` |

`review_status` / `reviewed_at` / `review_note` live on `experiences` and are
saved/restored by `group_key` across every `--rebuild`, so human triage is
never lost when the aggregate is recomputed.

---

## Pattern detector

`detect_occurrences(trajectory, events, cfg)` - ordering is authoritative
(transcripts are append-only), timestamps are used only for recency. Four
conservative pattern types ship; the mechanism generalizes to more.

| pattern | fires when | classification |
|---|---|---|
| `failure_repair_success` | `*_fail` -> (>= 1 non-vendored edit **or** a migration command) -> matching `*_pass`, within `max_event_gap` (20) events | `support`, or `ambiguous` if the repair context is `other` / `mixed` |
| `human_correction_repair` | a *meaningful, directive* `human_correction` -> a `*_fail` in-window -> matching `*_pass`, with edits/commands in between | `support`, or `ambiguous` for an `other` repair context |
| `repeated_failure` | a fail/`error` event whose normalized signature `_looks_diagnostic` (names a real error, not "test exit 1"), and is terminal here (never resolved, or the session ended `partial`/`aborted`/`failure`) | `support` if it recurs in-session or the session ended badly; `ambiguous` for a lone hit in a session that still succeeded |
| `file_change_pattern` | a schema-shaped change transition (`model`->`migration`, `model`->`database`, `database`->`migration`) followed by a real `*_pass`, in a `success` session | `support` |

Precision safeguards added during Stage 5 (all driven by the real-data
false-positive pass, below):

- **One occurrence per `(group_key, classification)` per trajectory.** Ten
  test-fix cycles in one chatty session are one piece of evidence, not ten.
- **`_diagnostic_text()`** - the Claude/Codex adapters synthesize the failure
  event summary as `"<kind> exit <code>"` and keep the real output on the
  preceding `command` event; the detector now looks back one/two events to
  recover it before signing a `repeated_failure` signature.
- **`_looks_diagnostic()`** - a `repeated_failure` signature must be >= 12
  chars, not a generic template (`test exit n`, `n passed, n failed`,
  `command failed`, ...), and must contain a diagnostic token (an
  `*error` / `*exception` word, a quoted symbol, or a phrase like
  `command not found`, `could not acquire`, `read-only file system`,
  `module not found`, `no such file`).
- **`file_change_pattern`** was cut from "any A->B context transition" (fired
  on nearly every long successful session) to the three schema transitions
  above, and now requires an actual passing check after the second change.
- **`human_correction_repair`** was cut from "meaningful correction then
  eventual success" (describes almost every multi-turn session) to
  "directive correction then a checked fail->pass".

`error_signature()` normalization (documented in `signatures.py`): pick the
first salient line (prefer a line naming an error) -> lowercase -> mask UUIDs,
Windows paths, hex addresses, line:col markers, POSIX paths -> digit runs to
`N` -> squeeze whitespace -> strip framing punctuation -> cap 200 chars ->
empty if nothing alphabetic remains.

---

## Grouping

`build_experiences(occurrences, profiles, cfg)` - deterministic, interpretable.

- Bucket occurrences by `group_key`:
  - `fr::<repair_context>::<resolution_family>` - `failure_repair_success` and
    `file_change_pattern` share this namespace so a proactive
    "model -> migration -> tests pass" clusters with the reactive
    "model change, tests fail, fix migration, tests pass".
  - `hc::<repair_context>::<resolution_family>` - `human_correction_repair`.
  - `rf::<error_signature>` - `repeated_failure`.
- `positive = support + ambiguous`. A group with **no** `support` occurrence is
  dropped (ambiguous-only is not yet an experience).
- **Synthesized contradictions** (for `fr::` groups only, when
  `len(positive) >= max(2, min_occurrences - 1)`): a trajectory that made the
  same *antecedent* kind of change, reached the same check family cleanly
  (a `*_pass` with no earlier same-family fail), and never touched the repair
  context, becomes a `contradiction` occurrence (`OC-` id, recomputed each
  run). This is how "you must also touch migrations" gets tested against every
  session that changed a model and passed without one.
- A group with `len(positive) < min_occurrences - 1` is dropped (a single
  occurrence is an occurrence, not an experience).
- Status: `candidate` iff `len(positive) >= min_occurrences (3)` **and**
  `project_count >= min_projects (1)`; otherwise `needs_more_evidence`.
- Contradictions never widen `recurrence` or `cross_project` - they only ever
  subtract, through `support_ratio`. `occurrence_count` / `project_count` on
  the row are the totals (incl. contradictions); the confidence inputs are
  support-scoped.

---

## Confidence (exact formula)

`compute_confidence()` in `experience/confidence.py`. Not a judgement - a pure,
reproducible function of the evidence:

```
support_ratio = S / (S + C)                    # S = support, C = contradiction; ambiguous excluded; 0 if S+C = 0
recurrence    = min(1, N / 6)                   # N = supporting + ambiguous occurrences; saturates at 6
cross_project = min(1, (P - 1) / 2)             # P = distinct projects with SUPPORT evidence; 1 -> 0, 3+ -> 1
recency       = 1.0 if last_seen <= 30 days
                0.6 if last_seen <= 90 days
                0.3 if last_seen <= 180 days (or unparseable)
                0.1 otherwise

confidence = round( 0.50 * support_ratio
                  + 0.25 * recurrence
                  + 0.15 * cross_project
                  + 0.10 * recency , 2 )
```

Reference example (from the brief, `test_experience_confidence.py`): S=5, C=1,
N=6, P=2, seen 5 days ago -> support_ratio 0.833, recurrence 1.0,
cross_project 0.5, recency 1.0 -> **0.84**.

---

## LLM usage

**None at runtime.** Detection, grouping, confidence, and the shipped
summaries are 100% deterministic. `summarize.py` defines a `Summarizer`
protocol with two implementations:

- `DeterministicSummarizer` (default, `summary_source = "deterministic"`) -
  template text keyed by `pattern_type`, filled from the aggregated
  context/family/signature. This is what every experience in this report uses.
- `LlmSummarizer` - a seam only. It renders a prompt from the *aggregated,
  already-anonymised* group (never raw transcript text) and, with
  `transport=None` (the only wiring that exists), falls straight back to the
  deterministic summariser. `get_summarizer(cfg)` returns it only when
  `cfg.use_llm_summary` is set, which no code path and no CLI flag sets.

`experience_runs.llm_used` / `llm_tokens` are recorded per run and are `0` /
`false` everywhere. Semantic merging of near-duplicate signatures (see
limitations) is the obvious first place a local model would help; it is left
for Stage 6+ behind this seam.

---

## Tests

`.venv/bin/python -m pytest -q` -> **213 passed, 0 failed, 0 skipped**
(106 before Stage 5, **+107**). Runtime ~10s. No network, no browser, no real
home touched - every test builds its own `tmp_path` SQLite DB.

| file | tests | covers |
|---|---|---|
| `tests/unit/test_experience_corrections.py` | 35 | the meaningful-correction gate: acks, directives, questions, paths, token count |
| `tests/unit/test_experience_context.py` | 29 | file/command context classification, vendored-path detection |
| `tests/unit/test_experience_detect.py` | 9 | each pattern type, the event-gap window, per-trajectory dedup, strict `human_correction_repair`, generic-signature rejection |
| `tests/unit/test_experience_grouping.py` | 6 | bucketing, contradiction synthesis lowering confidence, candidate vs needs-more thresholds |
| `tests/unit/test_experience_storage.py` | 6 | occurrence id allocation, incremental replace, review persistence across rebuild |
| `tests/unit/test_experience_signatures.py` | 5 | error-signature masking + stability |
| `tests/unit/test_experience_confidence.py` | 5 | each component, saturation, the brief's reference example, unparseable-timestamp path |
| `tests/unit/test_ui_experiences.py` | 3 | `/api/experiences` list + detail, `status` filter, `404`, empty DB, pre-Stage-5 schema |
| `tests/integration/test_experience_pipeline.py` | 5 | end-to-end incl. the brief's migration-after-model-change scenario with a contradiction |
| `tests/integration/test_cli_experiences.py` | 4 | `extract` / `list` / `show` / `review`, `--rebuild` reprocessing, idempotent 2nd run, unknown-id exit 2 |
| `tests/unit/test_storage.py` (modified) | - | `schema_version == 2`, the three new core tables exist |

---

## Real dataset results

`scripts/stage5_realdata.py` against the live local Codex + Claude stores.

| metric | value |
|---|---|
| repo roots registered (DB only, no markers) | 17 |
| trajectories imported / events | 254 / 11,094 |
| trajectories with any `*_fail` / `error` event | 25 (10%) |
| trajectories analyzed (`--rebuild`) | 254 |
| pattern occurrences (incl. 6 synthesized contradictions) | 30 |
| clusters formed | 21 |
| **candidate experiences** | **0** |
| `needs_more_evidence` experiences | 3 |
| false positives (post human review) | 0 |
| extraction runtime | ~0.6 s |
| 2nd run (no changes) trajectories analyzed | 0 (idempotent) |

**Why zero candidates is the correct result here.** This corpus is
overwhelmingly *successful exploratory work*: 201 `test_pass` / 99 `build_pass`
/ 49 `lint_pass` events against only 22 `test_fail` / 12 `build_fail` /
11 `lint_fail` / 6 `error`, and only 25 of 254 sessions contain a failing
check at all. The strongest genuinely recurring signal (a read-only-filesystem
lock error, below) appears in exactly 2 projects. `min_occurrences = 3` is the
gate, and it was **not** lowered to manufacture a candidate. The earlier,
looser detector produced 407 "occurrences" / 22 "candidates" on the same data
- essentially all noise (`human_correction_repair` on "yes", `file_change`
on every context transition, `repeated_failure` on "test exit N"). The Stage 5
detector is calibrated for the brief's stated preference.

### group_key distribution (real data)

```
   8  fr::backend::test
   2  rf::error: could not acquire lock ... read-only file system (os error N) at path "<path>"
   2  rf::exit code N (eval): command not found: ruff
   1  rf::error: failed to spawn: `ruff` caused by: no such file or directory (os error N)
   1  rf::[errno N] no such file or directory: '<path>'
   1  rf::zsh: command not found: pytest
   1  rf::zsh: command not found: python
   1  rf::exit code N (eval): command not found: pip ... modulenotfounderror: no module named 'huggingface_hub'
   1  rf::<path>(N,N): error tsN: property 'map' does not exist on type '{}'. ...
   1  rf::error: request failed after N retries ... failed to fetch ...
   1  fr::config::build      1  fr::backend::lint      1  fr::migration::build
   1  hc::config::build      1  hc::other::lint
   (+ 4 more singleton rf:: signatures)
```

---

## Top candidates

No experience reached `candidate` on real data. The three `needs_more_evidence`
clusters (the closest the real corpus produced):

| id | conf | pattern | evidence | reusable lesson (deterministic) |
|---|---|---|---|---|
| `E-0001` | 0.68 | `repeated_failure` | 1 support + 1 ambiguous, 2 projects | *"`error: could not acquire lock ... read-only file system (os error N)` recurs and is not trivially fixed - when it appears, expect real investigation rather than a quick retry."* (a sandbox/permissions problem hitting `uv`/`cargo` in two unrelated repos) |
| `E-0002` | 0.68 | `repeated_failure` | 1 support + 1 ambiguous, 1 project | *"`command not found: ruff` recurs..."* - the linter is invoked before it is installed in the session's environment |
| `E-0003` | 0.38 | `failure_repair_success` | 2 support + **6 synthesized contradictions**, 5 projects | *"Backend change needed before tests passed - check whether backend code also needs updating before relying on tests."* Held well below candidate by contradiction synthesis (see below). |

For a positive end-to-end demonstration (a real `candidate` with confidence,
contradiction, and evidence links) see
`tests/integration/test_experience_pipeline.py::test_migration_candidate_with_contradiction`
- the brief's migration-after-model-change scenario across 4 trajectories /
2 projects yields exactly one `E-0001`, `status = candidate`,
`support_count = 3`, `contradiction_count = 1`, `project_count = 2`,
`0 < confidence <= 1`, evidence ordered `[contradiction, support, support,
support]`. `test_contradiction_lowers_confidence` asserts the same cluster's
score strictly drops when the contradicting trajectory is added.

---

## False-positive analysis (mandatory)

The Stage 5 detector was tuned by running the real-data harness, reading the
output, and tightening the rule that produced each bad row. Findings and the
fix applied:

1. **`human_correction_repair` over-fired (46 occurrences on one cluster).**
   Any longish user turn that happened before eventual success was "evidence".
   *Fix:* require the correction to be **directive** (`_is_directive`), and
   require an actual `*_fail` -> `*_pass` of one family inside the window after
   it. Real-data occurrences: 46 -> 2 (1 support, 1 ambiguous), 0 experiences.

2. **`file_change_pattern` fired on almost every long successful session.**
   "context A then context B" is not a lesson - the first looser version
   produced this pattern as the single largest occurrence source and several
   of the noisy candidates. *Fix:* restrict to three schema-shaped transitions
   and require a passing check after B. Real-data occurrences for this pattern
   now: 0 (the corpus has no model/db/migration change pair that also shows a
   check passing right after).

3. **`repeated_failure` fired on `"test exit 1"` / `"lint exit 2"`.** The
   adapters put the exit-code string on the outcome event and the real error
   text on the previous `command` event, so every failing test run looked like
   the same "recurring failure". *Fix:* `_diagnostic_text()` recovers the real
   output; `_looks_diagnostic()` rejects generic templates and requires a
   named error token. Bad `rf::` clusters (`rf::test exit n`,
   `rf::lint exit n`, `rf::api error message`): 3 candidates -> 0; the `rf::`
   clusters that remain all name a concrete error.

4. **Per-trajectory occurrence inflation.** A chatty session with 10 fix
   cycles counted as 10. *Fix:* one occurrence per `(group_key,
   classification)` per trajectory.

5. **`E-0003` "Backend change needed before tests passed" (still present, as
   `needs_more_evidence`).** `backend` is a broad context (`file_context`
   returns it for most otherwise-unclassified code), so this cluster is
   inherently low-signal. It is **kept, not suppressed**, because it is a
   faithful demonstration that contradiction synthesis works: 2 supporting
   trajectories, 6 synthesized counter-examples (sessions that changed the
   same antecedent and reached test success without a separate backend
   repair), `support_ratio = 2/8 = 0.25`, final confidence **0.38**, status
   `needs_more_evidence`. The pipeline surfaced a weak pattern and then held
   it out of the candidate set on the evidence. A human can additionally mark
   it `review_status = false_positive` (CLI `experiences review`, tested); that
   annotation is shown in `list` / `show`, counted in `experience_counts()`,
   and survives `--rebuild` - it is the durable "reviewed, not useful" signal
   Stage 6 will gate on.

No false positive survives into the `candidate` set on real data (there are no
candidates). The residual imperfections are all in `needs_more_evidence` /
singleton occurrences and are visible for exactly what they are.

---

## Stage 3 / 4 issues discovered (mandatory)

Stage 5 read far more of the normalized event stream than Stage 4 did and
surfaced four upstream issues. **None was fixed in Stage 5** (out of scope -
Stage 5 must not change earlier-stage behaviour); each is worked around
read-only inside `experience/` and recorded here.

1. **Failure-outcome events carry no error body.** `adapters/claude.py:422`
   and `adapters/codex.py:360,464` emit the `test_fail` / `build_fail` /
   `lint_fail` / `error` event with `summary = f"{kind} exit {exit_code}"` and
   drop the command output, which is still present on the *preceding*
   `command` event's `summary`. This starves `repeated_failure` (it needs the
   text to build a signature). *Workaround:* `detect._diagnostic_text()` looks
   back one/two events to the `command` / `tool_call` summary. *Proper fix
   (Stage 3):* propagate the redacted output tail onto the outcome event, or
   add a `detail` field.

2. **Slash-commands and single letters are `human_correction`s.** `/compact`,
   `c`, and bare `yes` land as corrections (confirmed: 47 of 322 on real
   data). Stage 4 already recorded the "bare yes" half. *Workaround:*
   `is_meaningful_human_correction()` filters them at read time. *Proper fix
   (Stage 3):* the Claude adapter should treat a leading `/word` as a client
   command, not user text.

3. **`error_signature` near-duplicates fragment a real pattern.** The same
   read-only-filesystem lock failure produced two `rf::` group keys
   (`rf::error: could not acquire lock ...` and
   `rf::<path> <path> error: could not acquire lock ...`) because the salient
   line differed by a leading path. Merged, that pattern has 3 supporting
   trajectories and **would be a candidate**. This is a Stage 5 limitation,
   not an upstream bug - it is the strongest argument for the LLM
   semantic-merge seam. Similarly `command not found: ruff` /
   `failed to spawn: ruff` / `command not found: pytest` are one
   "tool-not-installed" family split three ways syntactically.

4. **Free-text summaries still contain absolute paths.** e.g.
   `/home/rudra/work/t/traccia-public-ui/.vscode/coupon.md` appears verbatim
   in a task string used as evidence context. Stage 4 recorded this. Stage 5
   only ever displays it in the already-path-revealing evidence table, so it
   is not a new exposure, but the Stage 3 path-rewriter should reach into
   free-text summaries.

No Stage 4 UI defects were found; the new pages reuse the existing router,
tokens, and empty-state conventions.

---

## Known limitations

- **Signature normalization is lexical, not semantic.** Near-duplicate error
  signatures are not merged (finding 3 above). This is the single biggest
  reason the real corpus produced 0 candidates instead of 1-2.
- **Four pattern types only.** `workflow_success` / `workflow_failure` /
  `tool_usage_pattern` from the brief's wider list are not implemented; the
  detection + grouping + confidence machinery is pattern-agnostic and adding
  one is a new `_detector` function plus a `group_key` scheme.
- **`repeated_failure` on a single in-session hit** is allowed (classified
  `ambiguous` unless the session ended badly) so that cross-trajectory
  recurrence can still be detected; a lone ambiguous hit never forms an
  experience by itself.
- **`backend` / `frontend` are coarse contexts.** They are kept as valid
  repair contexts (so real repairs there are not lost) but they produce
  low-signal, truism-shaped lessons (`E-0003`). A reviewer marking them
  `false_positive` is the intended control.
- **Recency uses `last_seen`,** so a strong old pattern decays even if it is
  still true. Deliberate (matches the brief) but worth noting.
- **The deterministic summariser is templated** - readable, but blunt
  ("changes of this kind"). This is the LLM seam's job.
- **No incremental grouping.** Detection is incremental (hash-gated per
  trajectory); grouping is always a full recompute from the occurrence table.
  Cheap here (occurrences are rare, ~0.2 s) and keeps aggregates trivially
  consistent, but it is O(occurrences) every run.

---

## Stage 6 readiness

Stage 6 (placement: deciding *where* a validated lesson should live, and
writing it there) can build directly on this:

- **Stable ids and keys.** `E-####` ids and `group_key`s are stable across
  runs; `review_status` already gives Stage 6 the human-accept signal to gate
  on.
- **Everything is traceable.** `experience_evidence` -> `experience_occurrences`
  -> `trajectory_id` + sequence range -> `trajectory_events`. Every lesson can
  be re-justified from real sessions, and the UI already deep-links it.
- **`context_json` on each experience** records the repair/antecedent contexts
  and families - the raw material for a placement heuristic (project-scoped vs
  global, which file area).
- **`project_count` / per-project support** distinguishes a one-repo habit
  from a cross-project rule - the core placement input.
- **The LLM seam** (`summarize.LlmSummarizer`, `cfg.use_llm_summary`) is where
  semantic signature-merge and natural-language lesson phrasing plug in,
  operating only on aggregated, anonymised group data.
- **Hard boundary intact.** Stage 5 writes nothing outside
  `~/.trajweave/trajweave.db`. Stage 6 is the first stage permitted to
  propose a repo/agent-config edit, and only behind an explicit human step.

---

## Confirmation

- No `AGENTS.md` / `CLAUDE.md` read or written. No skill created. No rule
  written into any repository. No project source modified. No transcript
  copied or modified. No network call. No paid API. No cloud.
- New runtime dependencies: **none** (`pyproject.toml` `dependencies = []`).
- `SCHEMA_VERSION` 1 -> 2 via forward-only `0002_experience.sql`; the UI
  endpoint degrades to empty on a v1 DB.
- `trajweave` version `0.3.0` -> `0.5.0`.
- Full suite: **213 passed, 0 failed, 0 skipped.**
