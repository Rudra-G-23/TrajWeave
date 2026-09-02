# Worklog

Chronological record of what was done, newest section last. The formal
deliverable write-up lives in [`STAGE_0-3_REPORT.md`](./STAGE_0-3_REPORT.md).

---

## Session 1 - Stage 0-3 data substrate (2026-09-02)

### 1. Inspection before coding

- Repo started with only `LICENSE`, `README.md`, `.gitignore` - no existing
  code to reuse.
- Read the real local session stores to design the schema from evidence, not
  assumptions:
  - `~/.codex/sessions/` - 33 real JSONL sessions (Codex CLI 0.149.x). One
    format: tagged `{"timestamp","ordinal","type","payload"}` lines with a
    `session_meta` header and Codex's own `event_msg`/`item_completed` semantic
    item stream (`CommandExecution`, `FileChange`, `UserMessage`,
    `AgentMessage`, `McpToolCall`, `Extension`, `SubAgentActivity`, `Plan`,
    `Reasoning`, `ContextCompaction`).
  - `~/.claude/projects/` - 254 real JSONL session files across CLI versions
    2.1.228 - 2.1.258. Mixed record types; `user` / `assistant` carry the
    work, tool calls and their results are on separate records, sub-agents now
    live in a `<session>/subagents/agent-*.jsonl` sidecar.

### 2. Package skeleton (Stage 0)

- `src/`-layout package `trajweave`, `pyproject.toml` (hatchling), console
  script, **zero third-party runtime dependencies**.
- `config/paths.py` - `~/.trajweave` resolution with `TRAJWEAVE_HOME` override.
- `utils/` - streaming SHA-256, deterministic short ids, tolerant timestamp
  parsing, concise logging.

### 3. Normalized schema (Stage 3)

- `models/` - `NormalizedTrajectory`, `NormalizedEvent`, `FileTouch`, and small
  stable `EventType` / `FinalStatus` / `TaskSource` / `CommandKind` enums.
- `normalization/` - repo-relative path rewriting, conservative regex secret
  redaction (API keys, tokens, private keys, `Bearer`, `KEY=value`, URLs with
  creds), shell-command classification, injected-context stripping
  (`<environment_context>`, `# AGENTS.md`, `[Request interrupted by user]`,
  ...), and evidence-based `final_status` inference (never invents success).

### 4. Storage + project registry (Stage 1)

- `storage/` - versioned SQLite schema (migration `0001_initial.sql`),
  connection/migration manager, and a `Repository` with idempotent upserts.
  Tables: `projects`, `source_sessions`, `trajectories`, `trajectory_events`,
  `trajectory_files` (+ indexes). FKs use `SET NULL` / scoped `CASCADE` so
  deleting a project is non-destructive.
- `projects/` - `.git`-marker walk-up repo-root detection (no `git` binary
  needed), and a `ProjectRegistry`: `init` writes `<repo>/.trajweave/
  project.json` + a DB row, is idempotent (deterministic
  `tw_proj_<sha256(root)[:12]>` id, preserved `created_at`), and
  `resolve_for_path` self-heals a wiped DB from the on-disk marker.

### 5. Adapters (Stage 2)

- `adapters/base.py` - `BaseAdapter` ABC + tolerant `iter_jsonl` streamer.
- `adapters/codex.py` - normalizes off the `item_completed` stream, with a
  `response_item` fallback for older sessions; synthesizes `file_read` from
  `parsed_cmd`; treats a trailing no-work `turn_aborted` as a benign quit.
- `adapters/claude.py` - pairs `tool_use` with its later `tool_result`,
  discovers `subagents/` sidecar transcripts as their own trajectories,
  derives a coarse Bash exit code, treats interrupt-then-clean-end_turn as
  recovered.
- Both emit the identical `NormalizedTrajectory`.

### 6. Import orchestration + CLI

- `ingest/importer.py` - discover -> resolve repo -> opted-in? -> parse ->
  normalize -> dedupe -> store. Idempotent, restart-safe (pessimistic
  `failed` status flipped to `imported` only after a successful transactional
  persist), changed sessions replaced in place keeping their `TW-` id, one bad
  session recorded as `failed` without stopping the run.
- `cli/main.py` - `init`, `projects`, `import`, `sessions`, `trajectories`,
  `show` (all with `--json`); global `--home` / `-v` / `-q` / `--version`.

### 7. Tests

- 82 tests total: 72 unit + 10 integration (`-m integration`), 0 failures,
  81% line coverage.
- Synthetic sanitized fixtures under `tests/fixtures/{codex,claude}/`; a
  `conftest.py` that isolates `TRAJWEAVE_HOME` per test and builds temp Git
  repos + injectable-cwd sessions.
- Integration covers the required flow: temp repo -> `init` -> fixture session
  -> import -> trajectory exists -> re-import -> no duplicate; unregistered
  repo -> ignored; changed session -> re-imported in place; `--agent` /
  `--project` filters; forced parse failure -> counted, run continues;
  `--dry-run` stores nothing; full CLI via `main(argv)`.

### 8. Real-session validation (read-only)

- Ran the full pipeline against the actual `~/.codex` / `~/.claude` stores into
  a throwaway DB, registering the 17 referenced repo roots directly (no marker
  files written into real repos, no transcript touched).
- 33 + 254 discovered; 246 imported, 41 `ignored_unregistered`, **0 failed**;
  second run **0 new / 0 re-imported** (fully idempotent). 10,423 normalized
  events; redaction fired on 235; only 1 trajectory with parse warnings.

### 9. Docs + commit

- Rewrote `README.md` (privacy behaviour, install, commands, schema, layout).
- Added `docs/STAGE_0-3_REPORT.md` (full completion report + deviations).
- `.gitignore`: ignore `.trajweave/`.
- Committed as `feat: TrajWeave Stage 0-3 data substrate` (`8af4d2f`) on
  `feat/stage1`.
- Added this worklog.

### Not done (out of scope, per the prompt)

Experience extraction, candidate lessons, LLM analysis, skill/`AGENTS.md`/
`CLAUDE.md` generation, placement optimization, evaluation engine, UI, SaaS,
auth, billing, cloud storage.

---

## Session 2 - Stage 4 local UI + read-only inspection (2026-09-02)

The formal write-up is [`STAGE_4_REPORT.md`](./STAGE_4_REPORT.md).

### 1. Inspection before UI work

- Re-read the Stage 0-3 substrate (schema, `Repository`, adapters, importer,
  CLI, enums). Ran the suite: 82/82 green.
- Confirmed the defining constraint: zero third-party runtime deps.
- No real `~/.trajweave` DB existed yet, but the real session stores did
  (34 Codex, 257 Claude session files).

### 2. Read layer

- Six new read methods on `storage/repository.py`: `list_project_summaries`,
  `get_project_summary`, `list_sessions_page` (filters + `q` + limit/offset +
  total), `get_trajectory_detail` (adds source provenance + subagent flag),
  `distinct_filter_values`. No parallel DB layer.

### 3. UI server (`src/trajweave/ui/`)

- `server.py`: `ThreadingHTTPServer`, per-request read-only SQLite connection
  (`mode=ro` + `PRAGMA query_only`), six JSON endpoints, static file serving,
  graceful missing-DB / bad-schema / malformed-metadata / unknown-event-type
  handling. `serve()` picks a free port when the default is busy, hard-errors
  on an explicit busy port, opens a browser via stdlib `webbrowser`.
- `static/{index.html,app.js,app.css}` (~1000 lines): vanilla-JS hash-routed
  SPA. Projects / Sessions / Session detail (Timeline + Files/Commands/Errors/
  Raw Metadata tabs) / Import ledger. `#5e17eb` used sparingly per the brief;
  single light theme; status as label+colour; fail->pass shown by ordering
  with an explicit "not a proven cause" note.

### 4. CLI

- `trajweave ui [--port N] [--no-browser]`, binds `127.0.0.1` only.
- Static assets ship as package data (wheel build verified to contain
  `trajweave/ui/static/*`).

### 5. Tests (+24, total 106/106)

- `tests/unit/test_ui_repository.py`, `tests/unit/test_ui_http.py` (server
  over real `urllib`, no browser), `tests/integration/test_ui_server.py`
  (full pipeline -> HTTP), `tests/integration/test_cli.py` (`ui` arg wiring +
  port behaviour), regression cases in `tests/unit/test_commands.py`.

### 6. Real-data validation (read-only, throwaway home)

- 17 repo roots registered directly in a throwaway DB (no markers into real
  repos, no transcript touched). 250 imported, 41 ignored, 0 failed,
  10,607 events. Inspected `TW-000001/109/244/247` + a scripted sweep of all
  250 through the UI and API.

### 7. Stage 3 fix (one, documented)

- `normalization/commands.classify_command` matched tool names as substrings
  of whole compound shell lines, so `which pytest` / `pytest --version` /
  `echo "... pytest ..."` / `mkdir -p .next/test-run` produced phantom
  `test_fail` / `build_fail` events. Fixed with word-boundary matching,
  quoted-literal stripping, inspect-only leading-word skip, and a
  `--version` / `--help` guard. Real-data derived verification events:
  462 -> 347; genuine `pytest -q` / `npm test` runs still detected.
- Four further Stage 3 observations recorded in the report but **not** fixed
  (out of scope / not misleading / not reliably fixable): `human_correction`
  fires on bare "yes"/"c"; `task` sometimes a bare path or the whole prompt;
  absolute paths inside free-text summaries; `file_read` of vendored paths.
- No schema change, no `0002` migration.

### 8. Not done (still out of scope)

Everything from Session 1's out-of-scope list, plus: charts/analytics,
column sorting, full-text search, parent/child linkage for sub-agents.
