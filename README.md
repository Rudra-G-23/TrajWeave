# TrajWeave

Local-first research substrate for **coding-agent trajectory learning**.

TrajWeave observes your Codex CLI and Claude Code sessions, figures out which
Git repository each one belongs to, and - **only for repositories you have
explicitly opted in** - parses the agent-specific transcript into a common
normalized schema and stores it in a local SQLite database.

This repository implements **Stage 0-3 only**: the data layer. There is no
learning engine, no skill/rule generation, no modification of `AGENTS.md` /
`CLAUDE.md`, no evaluation engine, and no cloud/SaaS. The goal is a
trustworthy, privacy-conscious, agent-independent trajectory dataset that a
later research algorithm can rely on.

---

## Privacy behaviour (read this first)

- **Explicit opt-in.** TrajWeave never tracks a repository until you run
  `trajweave init` inside it. Sessions belonging to any other repository are
  ignored (and recorded as `ignored_unregistered`, never parsed).
- **Raw transcripts stay put.** The original session files remain in
  `~/.codex/sessions/` and `~/.claude/projects/`. TrajWeave stores a *reference*
  (path + content hash) plus normalized metadata and events - not a copy of the
  transcript.
- **Local only.** Everything is written under `~/.trajweave/` (override with
  `TRAJWEAVE_HOME`). Nothing is sent anywhere.
- **The only repo-side change** is a small opt-in marker:
  `<repo>/.trajweave/project.json`. TrajWeave never touches your source,
  `AGENTS.md`, `CLAUDE.md`, or Git history.
- **Basic secret redaction.** Obvious credentials (API keys, tokens, private
  keys, `Bearer` headers, `KEY=value` secrets, credentials in URLs) are
  replaced with `[REDACTED:...]` before anything is stored, and the affected
  event is flagged.

---

## Installation

Requires Python 3.10+. No third-party runtime dependencies.

```bash
pip install -e .
# or, for development (pytest):
pip install -e ".[dev]"
```

## Quick start

```bash
# 1. Opt a repository in (run from inside the repo)
cd ~/work/repo-a
trajweave init

cd ~/work/repo-c
trajweave init

# 2. See what is registered
trajweave projects

# 3. Import all historical Codex + Claude sessions for registered repos
trajweave import --all

# 4. Inspect
trajweave trajectories
trajweave show TW-000001
```

Running `trajweave import --all` again imports nothing new - it is idempotent
and restart-safe. A session whose file has changed since last import is
re-parsed and its trajectory is replaced **in place** (its `TW-` id is kept).

---

## Commands

| Command | Purpose |
| --- | --- |
| `trajweave init [PATH] [--name NAME]` | Register the repository containing `PATH` (default: cwd). Idempotent. Writes `<repo>/.trajweave/project.json` and a row in the global DB. |
| `trajweave projects [--json]` | List registered projects, their status, and trajectory counts. |
| `trajweave import [--all] [--agent codex\|claude] [--project PATH] [--dry-run] [--verbose] [--json]` | Discover, route, parse, normalize, dedupe and store sessions. |
| `trajweave sessions [--agent ...] [--status ...] [--json]` | List every discovered source session and its import status. |
| `trajweave trajectories [--agent ...] [--project PATH] [--limit N] [--json]` | List stored trajectories. |
| `trajweave show TW-000123 [--events N] [--json]` | Inspect one trajectory: task, status, files changed, event trace. |

Global flags: `--home DIR`, `-v/--verbose`, `-q/--quiet`, `--version`.

---

## What gets stored

SQLite database at `~/.trajweave/trajweave.db` (schema is versioned via a
`schema_migrations` table).

| Table | Contents |
| --- | --- |
| `projects` | project id (`tw_proj_<hash-of-canonical-root>`), name, canonical root, git remote, created/last-seen timestamps, `enabled`, `status` (`active`/`missing`/`archived`). |
| `source_sessions` | one row per discovered session file: agent, source session id, path, content hash, mtime, size, resolved project, import `status`, detail. `UNIQUE(agent, source_session_id, source_path)`. |
| `trajectories` | `TW-000001`-style id, agent, task + `task_source`, start/end, `final_status` + reason, repository name/root, git branch/commit/remote, model, CLI version, token usage, event count. One per source session. |
| `trajectory_events` | ordered normalized events: `sequence`, `type`, timestamp, `path`, `command`, `exit_code`, `tool_name`, `summary`, `metadata` (JSON), `redacted`. |
| `trajectory_files` | per-trajectory file touch summary: relative `path` + `was_read/created/modified/deleted` flags. |

### Normalized event taxonomy

`user_prompt`, `assistant_message`, `file_read`, `file_create`, `file_edit`,
`file_delete`, `command`, `tool_call`, `test_run/pass/fail`,
`lint_run/pass/fail`, `build_run/pass/fail`, `error`, `retry`,
`human_correction`, `completion`, `unknown`.

A shell command becomes a `command` event; when it is recognisably a
test/lint/build invocation, a follow-up `test_pass` / `build_fail` / ... event
is derived from its exit code. Unrecognised record or item types are preserved
as `unknown` events with their raw kind in `metadata` - a malformed line or a
brand-new event type never aborts an import.

### Final status

`success` / `failure` / `partial` / `aborted` / `unknown`. Inferred
conservatively: `success` requires a passing final verification **or** an
explicit completion signal with no failing tail; uncertainty is preserved as
`unknown` rather than guessed.

---

## Repository resolution

For each session, TrajWeave takes the recorded working directory, walks up to
the enclosing Git repository root (a `.git` file or directory - no `git`
executable required), and checks whether that root is a registered project
(by `<repo>/.trajweave/project.json` **or** a DB row - either is sufficient, so
history survives the repo being deleted, and a wiped DB self-heals from the
marker). Paths inside events are stored **relative to the repository root**
(`app/billing.py`, not `/home/you/work/repo-a/app/billing.py`).

---

## Development

```bash
pip install -e ".[dev]"
pytest                       # unit + integration
pytest -m "not integration"  # unit only
```

Test fixtures live in `tests/fixtures/{codex,claude}/` and are small, sanitized,
synthetic sessions - no real private transcripts are committed.

## Layout

```
src/trajweave/
├── cli/            argparse entry point
├── config/         ~/.trajweave path resolution (TRAJWEAVE_HOME)
├── models/         enums, NormalizedEvent, NormalizedTrajectory
├── normalization/  path relativization, redaction, command classification, status inference
├── adapters/       base.py + codex.py + claude.py (all agent-specific logic lives here)
├── projects/       git repo-root detection, project registry / opt-in
├── ingest/         importer: discover -> route -> parse -> normalize -> dedupe -> store
├── storage/        SQLite schema, migrations, repository (persistence)
└── utils/          hashing, timestamp parsing, logging
```
