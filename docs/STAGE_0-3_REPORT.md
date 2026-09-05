# TrajWeave - Stage 0-3 Completion Report

Scope delivered: the **data substrate only** - discovery, explicit repository
opt-in, agent-specific parsing, normalization into a common schema, and local
SQLite storage. No learning, rule/skill generation, instruction-file mutation,
evaluation engine, UI, or cloud.

---

## Implemented

**Stage 0 - skeleton**
- `src/` layout Python package, `pyproject.toml` (hatchling), console script
  `trajweave`. Zero third-party runtime dependencies (stdlib only).
- `pip install -e .` + `trajweave --help` work; `pytest` runs.

**Stage 1 - repository opt-in + registry**
- `trajweave init [PATH]`: resolves the Git repo root (walks up for a `.git`
  file/dir; no `git` binary required), computes a deterministic
  `tw_proj_<sha256(canonical_root)[:12]>` id, writes
  `<repo>/.trajweave/project.json`, registers a row in `~/.trajweave/trajweave.db`.
  Idempotent (stable id + preserved `created_at`; re-run makes no duplicate).
- `trajweave projects` lists registered projects with status + trajectory counts.
- Non-registered repos are ignored. Deleting a repo is non-destructive: the
  `projects` row and its trajectories remain; resolution can later flip a
  project to `missing`. A wiped DB self-heals from the on-disk marker.

**Stage 2 - discovery + adapters**
- `BaseAdapter` (`default_root` / `discover` / `can_parse` / `parse`); all
  agent-specific logic is confined to `adapters/codex.py` and
  `adapters/claude.py`. Discovery is cheap (stat + first-record peek).
- Tolerant JSONL streaming (`iter_jsonl`): a bad line or unknown record/item
  type is recorded in `parse_warnings` and never aborts the session or the run.
- `CodexAdapter`: primary path over Codex's own `event_msg`/`item_completed`
  semantic item stream, with a fallback path over the raw `response_item`
  stream for older sessions.
- `ClaudeAdapter`: buffers `tool_use` and finalizes on the matching
  `tool_result` (they are separate records); also discovers the
  `<session>/subagents/agent-*.jsonl` transcripts newer Claude Code writes.

**Stage 3 - normalized schema**
- `NormalizedTrajectory` + `NormalizedEvent` + `FileTouch`; small stable
  `EventType` / `FinalStatus` / `TaskSource` enums.
- Normalization helpers: repo-relative path rewriting, conservative secret
  redaction, shell-command classification (test/lint/build/vcs/...), and
  evidence-based final-status inference.

**Historical import**
- `trajweave import [--all] [--agent ...] [--project PATH] [--dry-run] [--json]`.
- Pipeline: discover -> resolve repo -> opted-in? -> parse -> normalize ->
  dedupe -> store. Idempotent and restart-safe: unchanged sessions are skipped
  by `(agent, source_session_id, source_path)` + content hash; a changed
  session is re-parsed and its trajectory replaced **in place** (same `TW-` id).
  One failing session is recorded as `failed` and does not stop the run.
- Summary output: discovered / imported / re-imported / already-imported /
  ignored / failed, with per-failure detail.

**Extra CLI**
- `trajweave sessions` (source-session import ledger),
  `trajweave trajectories`, `trajweave show TW-000123` (task, status, files
  changed, event trace). `--json` on every read command.

---

## Architecture

```
src/trajweave/
├── __init__.py  __main__.py
├── cli/            main.py                     argparse entry point + 6 subcommands
├── config/         paths.py                    ~/.trajweave resolution, TRAJWEAVE_HOME override
├── models/         enums.py events.py trajectory.py
├── normalization/  paths.py redaction.py commands.py status.py text.py
├── adapters/       base.py codex.py claude.py  (ALL agent-specific code)
├── projects/       git.py registry.py          repo-root detection + opt-in registry
├── ingest/         importer.py                 orchestration
├── storage/        database.py repository.py migrations/0001_initial.sql
└── utils/          hashing.py timeparse.py logging.py
tests/
├── unit/ (72)   integration/ (10)   fixtures/{codex,claude}/   conftest.py
docs/STAGE_0-3_REPORT.md
```

---

## Commands

```
trajweave --help | --version
trajweave init [PATH] [--name NAME]
trajweave projects [--json]
trajweave import [--all] [--agent codex|claude] [--project PATH] [--dry-run] [--verbose] [--json]
trajweave sessions [--agent ...] [--status ...] [--json]
trajweave trajectories [--agent ...] [--project PATH] [--limit N] [--json]
trajweave show TW-000123 [--events N] [--json]
```

---

## Database (`~/.trajweave/trajweave.db`)

Versioned via `schema_migrations`. Tables:

| Table | Key columns |
| --- | --- |
| `projects` | `id` (`tw_proj_*`), `name`, `root` UNIQUE, `git_remote`, `created_at`, `last_seen_at`, `enabled`, `status` (active/missing/archived) |
| `source_sessions` | `agent`, `source_session_id`, `source_path`, `source_hash`, `source_mtime`, `size_bytes`, `project_id` FK (SET NULL), `status`, `detail`, `cwd`; UNIQUE`(agent, source_session_id, source_path)` |
| `trajectories` | `id` (`TW-000001`), `seq` UNIQUE, `source_session_pk` UNIQUE FK (CASCADE), `project_id` FK (SET NULL), `agent`, `task`, `task_source`, `started_at`, `ended_at`, `final_status`, `final_status_reason`, `repository_name/root`, `git_branch/commit/remote`, `model`, `cli_version`, `token_usage` JSON, `event_count`, `parse_warnings` JSON |
| `trajectory_events` | `trajectory_id` FK (CASCADE), `sequence`, `type`, `timestamp`, `path`, `command`, `exit_code`, `tool_name`, `summary`, `metadata` JSON, `redacted`; UNIQUE`(trajectory_id, sequence)` |
| `trajectory_files` | `trajectory_id` FK (CASCADE), `path`, `was_read/created/modified/deleted`; UNIQUE`(trajectory_id, path)` |

Indexes on: `projects(root)`, `projects(status)`, `source_sessions(agent,status)`,
`source_sessions(project_id)`, `source_sessions(source_hash)`,
`trajectories(project_id)`, `trajectories(agent)`, `trajectories(started_at)`,
`trajectories(final_status)`, `trajectory_events(trajectory_id,sequence)`,
`trajectory_events(type)`, `trajectory_files(trajectory_id)`,
`trajectory_files(path)`.

Deletion is non-destructive: dropping a project row nulls FK references rather
than cascading trajectories away; dropping a source session cascades only its
own trajectory.

---

## Codex - discovered session structure

Location: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<iso>-<uuid>.jsonl`
(33 real local sessions inspected; all one format, Codex CLI 0.149.x).

Every line: `{"timestamp", "ordinal", "type", "payload"}`. Top-level `type`:
`session_meta`, `turn_context`, `event_msg`, `response_item`, `world_state`,
`compacted`, `inter_agent_communication_metadata`.

- **`session_meta.payload`** - `session_id`, `cwd`, `cli_version`, and a
  `git` object (`repository_url`, `branch`, `commit_hash`).
- **`turn_context.payload`** - `model`, `cwd`, `workspace_roots`, effort.
- **`event_msg` / `item_completed`** - Codex's own normalized item stream, the
  primary source. Item types seen: `Reasoning`, `CommandExecution`,
  `AgentMessage`, `UserMessage`, `FileChange`, `Extension` (web search),
  `SubAgentActivity`, `Plan`, `McpToolCall`, `ContextCompaction`.
  - `CommandExecution` carries `command` (argv), `cwd` (`file://`), `status`,
    `exit_code`, `duration`, `aggregated_output`, and a `parsed_cmd` array
    (`read` / `search` / `list_files` / `unknown`) which we use to derive
    `file_read` events and command-kind hints.
  - `FileChange.changes` is `{abs_path: {type: add|update|delete, unified_diff,
    move_path}}`; we store +/- line counts, never the diff body.
- **`event_msg` / `task_complete`** - `last_agent_message`, `duration_ms`
  (explicit completion signal).
- **`event_msg` / `token_count`** - `total_token_usage`.
- **`event_msg` / `turn_aborted`** - abort signal.
- **`response_item`** stream (`message`, `function_call`,
  `custom_tool_call`(+`_output`), `reasoning`) - used only when no
  `item_completed` stream is present (1 old session locally).

Mapped: `UserMessage`->`user_prompt`/`human_correction` (injected
`<environment_context>` / `# AGENTS.md` / `<skills_instructions>` blocks are
stripped); `AgentMessage`/`Plan`->`assistant_message`;
`CommandExecution`->`command` (+ derived `test_*`/`lint_*`/`build_*` and
`file_read`); `FileChange`->`file_create/edit/delete`;
`McpToolCall`/`Extension`/`SubAgentActivity`->`tool_call`.

---

## Claude Code - discovered session structure

Location: `~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl` plus
`~/.claude/projects/<slug>/<uuid>/subagents/agent-<hex>.jsonl`
(254 real local session files inspected across CLI versions 2.1.228 - 2.1.258).

Mixed record types (`mode`, `permission-mode`, `bridge-session`,
`file-history-*`, `user`, `assistant`, `attachment`, `ai-title`, `last-prompt`,
`system`, `queue-operation`, `agent-name`, ...). Relevant ones:

- **`user`** - `message.role == "user"`. Either a typed human prompt
  (`message.content` a string / text blocks; `origin.kind == "human"`,
  `promptSource == "typed"`) **or** tool results (`message.content` is
  `tool_result` blocks, with `is_error`, plus a sibling `toolUseResult` object
  carrying the rich payload). Also `cwd`, `gitBranch`, `version` on nearly
  every record.
- **`assistant`** - `message.content` blocks: `thinking` / `text` / `tool_use`.
  `message.model`, `message.usage`, `message.stop_reason` here.
- **`toolUseResult` shapes**: `Bash` -> `{stdout, stderr, interrupted,
  returnCodeInterpretation?}` (no numeric exit code - derived: 0, or 1 on
  `is_error`, or none if interrupted); `Read` -> `{file:{...}}`; `Edit` ->
  `{structuredPatch, ...}`; `Write` -> `{type: create|update, structuredPatch}`.
- **`ai-title`** (`aiTitle`) and **`last-prompt`** (`lastPrompt`) - task
  fallbacks.
- **`system`** - `subtype` `compact_boundary` / `turn_duration` / ...
- **`isSidechain: true`** - sub-agent activity, inline or in the separate
  `subagents/` file.

Mapped: typed `user` -> `user_prompt`/`human_correction`; `[Request interrupted
by user]` -> abort marker (not a prompt/task); assistant `text` ->
`assistant_message`, `thinking` dropped; `Read` -> `file_read`; `Edit`/
`MultiEdit`/`NotebookEdit` -> `file_edit`; `Write` -> `file_create`/`file_edit`;
`Bash` -> `command` (+ derived verification events); `Grep`/`Glob`/`LS` and
`Task`/`WebFetch`/`Skill`/`mcp__*`/`TodoWrite` -> `tool_call`. A `tool_use`
with no matching result is flushed as an unresolved `tool_call`.

Both adapters emit the identical `NormalizedTrajectory` shape.

---

## Tests

```
total   : 82
passed  : 82
failed  : 0
skipped : 0
```

- 72 unit (config/paths, hashing, timestamps, redaction, command
  classification, path relativization, status inference, git repo-root
  detection, registry init/idempotency/id-stability/self-heal/deleted-repo,
  storage migrations + upsert + persist/dedup + sequential ids, Codex adapter,
  Claude adapter, plus branch coverage for the rich item/record shapes and the
  `response_item` fallback).
- 10 integration (`-m integration`): temp Git repo -> `trajweave init` ->
  fixture session belonging to the repo -> import -> trajectory exists ->
  import again -> no duplicate; session in an uninitialized repo -> ignored;
  changed session -> re-imported in place with id preserved; `--agent` /
  `--project` filters; a synthetically failing parse -> counted `failed`, run
  continues; `--dry-run` stores nothing; full CLI flow via `main(argv)`.
- Line coverage 81% (adapters' rarer branches are additionally exercised by the
  real-session validation below, which is not counted).

---

## Real-session validation

Ran the full pipeline **read-only** against the actual local session stores
(`~/.codex/sessions`, `~/.claude/projects`) into a throwaway DB, registering the
17 real repo roots referenced by those sessions directly in the DB (no marker
files written into real repos, no modification of any transcript).

- Discovered: **33 Codex + 254 Claude** session files.
- First import: 246 imported, 41 `ignored_unregistered`
  (e.g. `~` itself, worktrees under `~/.herdr`), **0 failed**.
- Second import: **0 imported, 0 re-imported, 246 already-imported** - fully
  idempotent.
- 10,423 normalized events; distribution dominated by `command` (3.6k),
  `assistant_message` (2.1k), `file_read` (1.5k), `file_edit` (0.95k),
  `tool_call` (0.8k), then `file_create`, `human_correction`, `user_prompt`,
  `test_pass/fail`, `build_pass/fail`, `lint_pass/fail`, `completion`.
- Redaction fired on 235 events (secrets in shell commands, `Bearer` headers,
  `KEY=value` pairs); manual spot-check found no un-redacted credential and no
  raw diffs/file bodies stored. Redaction errs conservative (it also masks some
  harmless `${VAR:-}` shell expansions).
- Only 1 trajectory carried parse warnings (the single old-format Codex session
  that used the `response_item` fallback).
- Project mapping, task extraction, timestamps, files-changed lists, and
  pass/fail command recognition were spot-checked against `trajweave show` and
  matched the source transcripts.

---

## Deviations from the original plan

1. **Codex sessions are not a flat JSONL of turns** - they are a tagged
   `type`+`payload` stream, and Codex already emits a semantic
   `item_completed` event stream. The adapter normalizes off that stream
   rather than re-deriving events from raw model messages, with a
   `response_item` fallback for older sessions. Kept the taxonomy; added
   `metadata.kind` on `command` events.
2. **Claude tool calls and results are separate records.** The adapter buffers
   `tool_use` and finalizes on the matching `tool_result` (which carries the
   rich `toolUseResult`). Unmatched calls are flushed as unresolved `tool_call`s.
3. **Claude has no numeric exit code for Bash.** Derived a coarse exit code
   (0 / 1-on-`is_error` / none-if-interrupted). `test_*`/`build_*` outcome
   events are only emitted when an exit code is known - we never invent a
   pass/fail.
4. **Claude sub-agents** now live in a separate
   `<session>/subagents/agent-*.jsonl` file (older versions inlined them as
   `isSidechain` records). Discovery picks both up; a stand-alone sub-agent
   file becomes its own trajectory keyed by filename (its `sessionId` points at
   the parent).
5. **`turn_aborted` is usually benign.** Codex appends a `turn_aborted` when
   the user quits the CLI after the task already finished. `final_status` is
   only `aborted` when substantive work happened in the aborted turn;
   otherwise the prior completion/verification wins. Claude: an interruption
   followed by a clean `end_turn` is treated as recovered.
6. **Injected context is not a prompt.** `<environment_context>`,
   `<recommended_plugins>`, `# AGENTS.md instructions`, `<skills_instructions>`,
   `[Request interrupted by user]` etc. are stripped before task/`user_prompt`
   extraction; `task_source` records where the task string actually came from
   (`user_prompt` / `agent_title` / `last_prompt` / `none`).
7. **`file_read` for Codex** is synthesized from `CommandExecution.parsed_cmd`
   entries of type `read` (Codex reads files via `sed`/`cat`, not a Read tool).
8. **Repo resolution does not shell out to `git`** on the hot path - a `.git`
   marker walk-up - so it still works for moved/absent working copies. `git` is
   used opportunistically for remote/branch/commit when the copy still exists.
9. **Schema additions** beyond the prompt's sketch: `task_source`,
   `final_status_reason`, `parse_warnings`, `trajectory_files` flags,
   `source_sessions.detail/cwd`, project `status`, and a `seq` counter backing
   the `TW-` ids.

---

## Known limitations

- Claude Bash exit codes are coarse (no numeric code in the transcript).
- Redaction is regex-based and deliberately conservative - some false
  positives (masked shell-var references), and it is not a guarantee against
  every possible secret shape.
- A handful of real trajectories have `event_count == 0` (session files that
  are metadata-only or interrupted immediately) - they are stored faithfully
  rather than dropped.
- Sub-agent trajectories are stored as independent trajectories; they are
  linked to the same project but there is no explicit parent/child column yet.
- `retry` in the taxonomy is currently unused (no reliable signal in either
  format yet).
- Codex `git.repository_url` was absent (`null`) in the older local sessions;
  newer ones populate it. The importer backfills from the live repo when present.
- Discovery scans the whole session tree each run (fast for thousands of files;
  no incremental mtime index yet).

---

## What Stage 4 should begin with

The dataset now exists (~250 real trajectories, ~10k normalized events across
both agents). Stage 4 (experience extraction / candidate lessons) should start
from:

1. **`human_correction` events** - the strongest local signal. ~295 of them in
   the real data: each is a point where the user redirected the agent, i.e. a
   candidate lesson ("next time, do X"). Cluster these per project and per
   file-path.
2. **fail -> edit -> pass triples** - `test_fail`/`build_fail`/`lint_fail`
   followed by `file_edit`(s) on the same path followed by the matching
   `*_pass`. The diff-stat metadata plus the surrounding commands describe a
   concrete "what fixed it".
3. **repeated commands across trajectories in one project** - recurring
   `command` events (kind `test`/`lint`/`build`) are the project's de-facto
   verification workflow; recurring setup commands are onboarding friction.
4. **`tool_call` / MCP usage patterns** per project - which external knowledge
   sources the agent reaches for.

Concretely: add a read-only `trajweave experiences` analysis pass over
`trajectory_events` (still deterministic, no LLM) that emits candidate lessons
with provenance (trajectory id + event sequence range), then layer LLM
classification (Ignore / Global / Project / Scoped / Skill) on top of those
candidates. The storage layer already supports this - `metadata` blobs and the
`trajectory_files` table carry enough context, and a new `experiences` /
`candidates` table slots in as migration `0002`.
