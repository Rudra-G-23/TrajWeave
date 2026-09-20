# TrajWeave - Stage 4 Completion Report

Scope delivered: a **local, read-only, professional trajectory explorer**
(`trajweave ui`). No experience extraction, no learned lessons, no rule/skill
generation, no `AGENTS.md` / `CLAUDE.md` changes, no placement/eval engine, no
cloud. Stage 4 is an inspection and Stage-3-validation tool.

---

## Implemented

- `trajweave ui` - starts a local HTTP server bound to `127.0.0.1`, prints the
  URL, opens a browser (unless `--no-browser`), serves a small single-page app
  over a read-only JSON API.
- **Projects** page - every registered project with `total sessions`,
  `Codex` / `Claude` counts, last-activity (relative), repo path, and a
  non-`active` status tag when present.
- **Sessions** page - trajectory list (one row per imported session) with
  `Session / Project / Agent / Task / Status / Started / Duration`, filters for
  agent / status, substring search over task text and `TW-` id, server-side
  pagination (50/page), and a "subagent" tag for Claude sidecar transcripts.
- **Session detail** - header (id, task, status, agent, model, start/end,
  duration, event count, status basis, task source) plus an ordered vertical
  timeline and client-side **Timeline / Files / Commands / Errors / Raw
  Metadata** tabs, all derived from a single event payload.
  - Timeline events are compact: type label + colour-coded left border,
    relative-path or `$ command`, exit-code pill, redaction badge, collapsed
    output tail with "show more", and per-event metadata (`command_kind`,
    `+N/-M` line stats).
  - Failure -> repair sequences (`*_fail` ... `*_pass`) get a note giving the
    step numbers and intervening file-change count, explicitly labelled
    "ordering only, not a proven cause".
- **Import ledger** (`#/debug`, linked from the footer, not the main nav) -
  every discovered source session and why it is / is not in the dataset;
  this is the one view that shows absolute paths.
- Empty states: no DB, no projects, no sessions, no events, no
  files/commands/errors - each renders a helpful message with the next
  command instead of an error.
- Error handling: missing DB (treated as empty, not an error), incompatible
  schema (`503` with guidance), unknown event types (passed through verbatim),
  malformed event metadata (isolated per-event: `metadata_error` flag + raw
  string, the rest of the trajectory still renders), deleted project (sessions
  still listed, `project` shows blank).

---

## UI stack

**stdlib `http.server` + `sqlite3` + a hand-written vanilla-JS SPA. Zero new
dependencies.**

Why it fits TrajWeave:

- The project's defining constraint is *zero third-party runtime
  dependencies* (stdlib only, `pyproject.toml` `dependencies = []`). A Flask /
  FastAPI / npm stack would break that for a read-only local viewer.
- `http.server.ThreadingHTTPServer` + `json` covers the whole API surface.
  Each request opens its **own** short-lived SQLite connection in `mode=ro`
  with `PRAGMA query_only` - the UI process cannot write to the DB, and there
  is no write path in the code at all.
- The frontend is one `index.html` + `app.js` + `app.css` (~1000 lines total),
  no build step, hash routing, `fetch` against the JSON API. It is served as
  package data and is bundled into the wheel (verified).
- The read queries live on the existing `storage/repository.py`
  `Repository` class (six new methods), not a parallel DB layer.

---

## Routes / screens

| Route | Screen |
| --- | --- |
| `#/` | Projects |
| `#/sessions?project=&agent=&status=&q=&offset=` | Sessions list (filtered/paginated) |
| `#/session/TW-000123` | Session detail (Timeline / Files / Commands / Errors / Raw Metadata) |
| `#/debug` | Import ledger (source-session provenance) |

JSON API (read-only): `GET /api/meta`, `/api/projects`, `/api/projects/<id>`,
`/api/sessions`, `/api/trajectories/<TW-id>`, `/api/debug/sessions`.

---

## CLI

```bash
trajweave ui                 # http://127.0.0.1:8765, opens a browser
trajweave ui --port 9000     # explicit port; hard error if it is taken
trajweave ui --no-browser    # do not open a browser
```

- Binds `127.0.0.1` only - there is deliberately no `--host` / `--bind` flag.
- Default port `8765`; if it is busy the next free port in `8765-8784` is
  used and the real URL is printed. An explicit `--port` that is busy is a
  clear failure (exit 2), not a silent reassignment.
- `--home` (global) selects the TrajWeave home / DB.

---

## Tests

```
Passed:  106   (92 unit + 14 integration)
Failed:  0
Skipped: 0
```

New / changed:

- `tests/unit/test_ui_repository.py` - the six new read queries: project
  aggregates, projects with zero trajectories, session filters + pagination,
  `q` search, subagent detection, trajectory-detail provenance, distinct
  filter values.
- `tests/unit/test_ui_http.py` - the server over real HTTP (stdlib
  `urllib`, no browser): static assets, `meta` / `projects` / `sessions` /
  detail payloads, `404`s, **missing DB is empty not error**, **deleted
  project still lists its sessions**, **unknown event type + malformed
  metadata survive**, **incompatible schema -> 503**, ledger, and an
  assertion that the connection refuses writes.
- `tests/unit/test_commands.py` - regression cases for the Stage 3 fix below.
- `tests/integration/test_ui_server.py` - full pipeline (2 git repos, a Codex
  and a Claude session imported for real, plus a synthetic failed trajectory
  and a synthetic fail->pass "mixed" trajectory) read back over HTTP; asserts
  counts, filters, per-project agent breakdown, status filtering, and detail
  payloads.
- `tests/integration/test_cli.py` - `trajweave ui` argument wiring
  (`--no-browser`, `--port`), auto-advance when the default port is busy, and
  hard-error when an explicit port is busy. No real browser is launched.

Line coverage for the new/changed modules: `ui/server.py` 88%,
`storage/repository.py` 88%, `normalization/commands.py` 92%.

---

## Real trajectory validation

Method (identical to Stage 0-3): a **throwaway** `TRAJWEAVE_HOME`, with the 17
repo roots referenced by real sessions **registered directly in the DB** - no
`.trajweave/project.json` marker written into any real repo, no transcript
copied or modified. Script: `scripts`-style harness kept out of the package.

- Discovered: **34 Codex + 257 Claude** session files.
- Imported: **250**; `ignored_unregistered`: **41** (repos with no resolvable
  `.git` and no marker - e.g. deleted worktrees under `~/.herdr`); **0 failed**.
- **10,607** normalized events across 17 projects; status mix
  `success 197 / unknown 34 / aborted 10 / partial 9` (the real local data
  contains **no** `failure` - status inference stays conservative).
- Inspected in depth via the UI and `/api/trajectories/*`:
  `TW-000001` (Codex, `partial`), `TW-000109` (Claude, 68 events, this Stage 4
  session), `TW-000244` (Claude, 76 events, a build_fail -> build_pass repair),
  `TW-000247` (Claude, 89 events), plus a scripted sweep over all 250
  (event-type counts, ordering monotonicity, path shape, task presence,
  duration presence, fail/pass pairing).

Findings from that sweep drove exactly one code fix (next section) and four
recorded-but-not-fixed observations.

---

## Stage 3 issues discovered

### 1. Command classification produced phantom test/lint/build failures  [FIXED]

**Issue.** `normalization/commands.classify_command` matched tool tokens
(`pytest`, `make`, `tsc`, `tox`, ...) as **plain substrings** of a whole
compound shell line. So `which pytest`, `python3 -m pytest --version`,
`echo "=== python / pytest ==="`, and `mkdir -p .next/test-run/...` were all
classified as a test/build **run**, and their non-zero exit produced a
`test_fail` / `build_fail` event - a failure that never happened.

**Evidence.** `TW-000109` events 13-16: two diagnostic one-liners that merely
*mention* `pytest` became `test_fail`. `TW-000244` events 16-21: `mkdir` +
`cd` + `uv venv` scaffolding became `build_fail` then `build_pass`, which the
UI's Errors tab and repair detector then presented as a real repair.

**Fix made** (`normalization/commands.py`, logic only - no schema change):
- single-word tokens now match on a word boundary
  (`(?<![\w./-])make(?![\w-])`), so `mkdir` never matches `make` and a
  `pytest` inside a path never matches;
- quoted spans are blanked before matching (`echo "run pytest"` is inert);
- a segment whose leading word is inspect-only (`echo` `printf` `which`
  `type` `command` `true` `:` ...) is skipped;
- a segment carrying `--version` / `--help` is skipped.

**Effect on the real dataset** (re-import, same 250 sessions): derived
verification events dropped from **462 to 347**. `test_pass` 217->173,
`test_fail` 27->19, `build_pass` 161->97, `build_fail` 11->10, `lint_pass`
56->48. Genuine runs (`python -m pytest -q`, `npm test`, `cargo build`) are
still detected - `TW-000109`'s real `pytest -q` still yields `test_pass`.

**Research implication.** The fail -> edit -> pass triple that Stage 5 wants to
mine was contaminated with phantom failures paired to unrelated later successes.
The Errors tab and the repair-sequence heuristic are now trustworthy enough to
build on.

### 2. `human_correction` fires on bare affirmations  [recorded, not fixed]

**Issue.** A short user turn like `yes` / `c` / `go` after an assistant message
is classified `human_correction`. `TW-000109` has three (`yes` answers to
design questions); `TW-000001` has one (`c`, the user quitting the CLI).

**Evidence.** 310 `human_correction` events in the real data; manual sampling
of ~20 shows a meaningful fraction are approvals or single keystrokes, not
redirections.

**Why not fixed here.** It does not make the *UI* misleading (the event is
shown verbatim and is obviously an approval). It is a Stage 5 input-quality
problem.

**Research implication.** `human_correction` is named in the Stage 0-3 report
as "the strongest local signal" for candidate lessons. Before it is used that
way it needs a filter: drop turns that are pure affirmation / a single token /
shorter than N chars, and keep only turns that redirect. This is a Stage 5
pre-processing step, not a Stage 3 schema change.

### 3. `task` is sometimes a bare path or the entire prompt  [recorded, not fixed]

**Issue.** `TW-000247`'s task is `/home/rudra/.../coupon.md` (the user's first
message was just a file reference). `TW-000109`'s task is the full multi-KB
Stage 4 prompt.

**Why not fixed.** Both are *faithful* - `task_source` is honestly
`user_prompt` in each case. The UI truncates in list views and wraps in the
header, so it is not misleading, only occasionally low-signal.

**Research implication.** Stage 5 clustering on task text should length-cap and
should treat a task that is just a path as "no usable task".

### 4. Absolute paths inside free-text summaries  [recorded, not fixed]

**Issue.** `assistant_message` / `completion` summaries can contain absolute
paths written by the agent in prose, e.g.
`Simplified [codex/README.md](/home/rudra/test/dotfiles/codex/README.md)`.

**Why not fixed.** Redaction is for secrets, not path rewriting, and rewriting
arbitrary prose reliably is out of scope. The structured `path` field on every
event is already repo-relative; absolute paths only ever appear in free-text
bodies and in the deliberately-absolute Raw Metadata / ledger views.

### 5. `file_read` of vendored paths  [recorded, not a defect]

`TW-000244` reads inside `.next/test-run/python/.venv/.../traccia/...`. The
path is correctly repo-relative and the event is faithful; it is just noise.
Stage 5 can ignore reads under `.venv` / `node_modules` / build dirs.

---

## Screens / behavior (finished UI)

- Colour system exactly as briefed: `#5e17eb` only on active nav, links,
  selected tab, timeline "prompt" accent and the repair note. White ground,
  near-black text, light-grey borders, 4px radius, no gradients / shadows /
  animation. Accessible green / red / amber for status, always paired with a
  text label (`SUCCESS` / `FAIL` / `UNKNOWN`), never colour alone.
- Single committed light theme, keyboard-focusable links/buttons/tabs, visible
  focus ring, semantic `<table>` / `<dl>` / `<nav>`.
- Projects -> click a card -> Sessions filtered to that project (breadcrumb +
  "clear filter"). Session id -> detail. Logo -> Projects.

---

## Known limitations

- No JS unit-test runner (would add a dependency) - the frontend is covered by
  an asset-serving smoke test plus the API contract tests and manual review.
- The Sessions list is `seq DESC`; no column sorting.
- Search is a `LIKE` substring over `task` / `id` only (as briefed - no
  full-text, no event-body search).
- Sub-agent trajectories are tagged but not visually linked to their parent
  (no parent/child column exists in Stage 3).
- `pip install` / `uv pip` inside a compound setup line still classifies as
  `build` (pre-existing token choice); a failed dependency install therefore
  shows as `build_fail`. Left as-is - arguably correct, and changing it is a
  separate decision.
- Timeline renders every event for a session in one payload; the largest real
  trajectory (255 events) is fine, but there is no windowing for a
  hypothetical multi-thousand-event session.

---

## Stage 5 readiness

**The normalized trajectory data is reliable enough to begin Experience
Extraction, with one required pre-filter.**

Trustworthy now: event ordering, repo-relative paths, `trajectory_files`
touch flags, `final_status` (conservative - never invents success/failure),
project mapping, timestamps/durations, and - after the fix above -
test/lint/build pass/fail derivation and therefore the fail -> edit -> pass
repair signal.

Required before use: `human_correction` must be filtered (issue 2) - bare
affirmations and single-token turns removed - or Stage 5's "candidate lesson
per correction" step will be dominated by `yes`.

Nice-to-have for Stage 5 (not blocking): length-cap / path-detect on `task`
(issue 3), ignore reads under vendored dirs (issue 5).

No Stage 5 behavior was implemented.

---

## Confirmation

No repository instruction files or skills were modified. Changes are confined
to `src/trajweave/ui/**` (new), `src/trajweave/cli/main.py` (one subcommand),
`src/trajweave/storage/repository.py` (read methods), `src/trajweave/
normalization/commands.py` (the documented fix), `tests/**`, `pyproject.toml`
(a clarifying comment), and `docs/**`.
