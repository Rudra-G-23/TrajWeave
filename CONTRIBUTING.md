# Contributing to TrajWeave

Thanks for your interest in TrajWeave. This document describes how to set up a
development environment, the checks your change needs to pass, and how to get a
pull request merged.

TrajWeave is a **local-first research substrate** for coding-agent trajectory
learning. It observes Codex CLI and Claude Code sessions, and - only for
repositories you have explicitly opted in - normalizes their transcripts into a
common schema stored in a local SQLite database. Please keep that scope and its
privacy guarantees (see [`README.md`](./README.md) and
[`SECURITY.md`](./SECURITY.md)) in mind when proposing changes.

## Ground rules

- Be respectful. This project follows the
  [Contributor Covenant](./CODE_OF_CONDUCT.md).
- **Never commit real transcripts or private data.** Test fixtures under
  `tests/fixtures/` are small, synthetic, and sanitized. Keep it that way.
- Discuss large or behavior-changing proposals in an issue before writing code.
- Report security issues privately - see [`SECURITY.md`](./SECURITY.md), not the
  public issue tracker.

## Prerequisites

- **Python 3.10 or newer** (CI runs 3.10 - 3.13).
- **Git.**
- **[uv](https://docs.astral.sh/uv/)** is recommended for a reproducible
  environment (it reads the committed `uv.lock`). Plain `pip` works too.

TrajWeave has **no third-party runtime dependencies** - the only extra packages
are test and lint tools in the `dev` optional dependency group.

## Set up a development environment

Fork the repository on GitHub, then clone your fork:

```bash
git clone https://github.com/<your-username>/TrajWeave
cd TrajWeave
git remote add upstream https://github.com/Rudra-G-23/TrajWeave
```

Install the package in editable mode with the development extras:

```bash
# with uv (recommended)
uv sync --extra dev

# or with pip on macOS/Linux
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows PowerShell, use:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -e ".[dev]"
```

## Make your change

Create a branch off `main`:

```bash
git fetch upstream
git switch -c feat/short-description upstream/main
```

Branch name prefixes follow the commit convention below (`feat/`, `fix/`,
`docs/`, `chore/`, ...).

## Run the checks

Everything CI enforces, you can run locally.

```bash
# uv: full test suite (unit + integration)
uv run pytest

# uv: unit tests only (skip the ones that build a temp git repo)
uv run pytest -m "not integration"

# uv: lint
uv run ruff check .
```

If you installed with pip instead of uv, activate the virtual environment and
run the same checks without `uv run`:

```bash
python -m pytest
python -m pytest -m "not integration"
python -m ruff check .
```

- Integration tests are marked with `@pytest.mark.integration`; they create a
  throwaway git repository and run the real import pipeline.
- Linting is `ruff check` with the rule set configured in `pyproject.toml`
  (`[tool.ruff.lint]`). Line length (`E501`) is advisory only and not enforced,
  but keep new code close to the 100-column target and match the style of the
  surrounding file. There is no enforced auto-formatter.

### CI diagnostics and test isolation

The CI test job keeps dependency synchronization, collection, and test
execution separate so a slow job has an observable phase:

```bash
uv sync --frozen --extra dev
uv run --no-sync python -m pytest --collect-only -q
uv run --no-sync python -m pytest -vv -ra --durations=30
```

The equivalent commands after a pip-based install are:

```bash
python -m pytest --collect-only -q
python -m pytest -vv -ra --durations=30
```

Each CI test job has a 30-minute limit. The autouse fixture in
`tests/conftest.py` redirects `TRAJWEAVE_CODEX_ROOT` and
`TRAJWEAVE_CLAUDE_ROOT` to empty per-test temporary directories. This prevents
tests that run `trajweave import --all` from scanning a developer's real
`~/.codex` or `~/.claude` session store, while production commands continue to
use those default locations when the environment variables are unset.

The one-line commands corresponding to the complete CI test and lint steps are:

```bash
uv run --frozen --extra dev python -m pytest -q
uv run --no-sync ruff check --output-format=github .
```

After pushing a branch associated with a pull request, inspect the GitHub run
with GitHub CLI:

```bash
git push origin HEAD
gh run list --workflow ci.yml --limit 5
gh run watch <RUN_ID> --exit-status
```

If `gh` is unavailable, open the repository's Actions page and select the
latest `CI` run. The test job reports dependency synchronization, collection,
individual test names, and the slowest 30 tests as separate log sections.

## Add tests

- Unit tests live in `tests/unit/`, integration tests in `tests/integration/`.
- New behavior needs a test. Bug fixes should include a regression test that
  fails before the fix.
- If you need a new fixture session, add a **small synthetic** JSONL file under
  `tests/fixtures/{codex,claude}/`. Never use a real session.
- Schema changes require a new, forward-only migration file in
  `src/trajweave/storage/migrations/` (see the existing `000N_*.sql` files and
  `storage/database.py`), plus a migration test.

## Commit

This repository uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add codex sub-agent event flattening
fix: keep TW- id stable when a session file is re-parsed
docs: clarify repository resolution
chore: bump ruff rule set
```

Keep commits focused and the history readable. Do not add `Co-authored-by`
trailers for automated tooling.

## Open a pull request

Push your branch to your fork and open a PR against `Rudra-G-23/TrajWeave:main`.
The [pull request template](./.github/PULL_REQUEST_TEMPLATE.md) will prompt you
for:

- a summary and the motivation,
- the user-visible changes,
- how you tested it,
- documentation updates,
- compatibility / migration impact,
- a short checklist.

A PR is ready for review when:

- it does one thing,
- `uv run pytest` and `uv run ruff check .` pass,
- new or changed behavior is covered by tests,
- user-facing behavior changes are reflected in `README.md` and any relevant
  `docs/`,
- it introduces no secrets or real transcript data,
- it does not silently break the CLI surface, the stored schema, or the
  persistence and safety guarantees described in `README.md`.

## Documentation

- `README.md` is the entry point and command reference - update it when you
  change the CLI or storage schema.
- The per-stage design write-ups in `docs/` are historical records of how each
  layer was built.

## Reporting issues

- **Bugs and feature requests:** use the
  [issue forms](https://github.com/Rudra-G-23/TrajWeave/issues/new/choose).
- **Security vulnerabilities:** follow [`SECURITY.md`](./SECURITY.md) - do not
  open a public issue.
