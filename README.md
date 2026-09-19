# TrajWeave

![TrajWeave poster](/assets/trajweave-poster.png)

[![CI](https://github.com/Rudra-G-23/TrajWeave/actions/workflows/ci.yml/badge.svg)](https://github.com/Rudra-G-23/TrajWeave/actions/workflows/ci.yml)

> Every run makes the next one better.

TrajWeave is a local-first learning loop for coding-agent work. It imports Codex and Claude Code sessions, turns them into normalized trajectories, extracts evidence-backed experiences, proposes where they belong, and keeps a human in control of review, evaluation, and policy changes.

Nothing is sent to a cloud service. TrajWeave only processes repositories that you explicitly register with `trajweave init`.

## How it works

The complete stage map, data flow, and boundaries are in [`docs/STAGES.md`](docs/STAGES.md). In brief:

```text
session files -> trajectories -> experiences -> placement proposals
             -> human review -> evaluation -> lifecycle evidence
```

Each step stores its output locally and keeps links to the evidence behind it. No step silently changes a repository policy file.

## Install

Requires Python 3.10+.

```bash
pip install -e .
```

For development:

```bash
pip install -e ".[dev]"
```

## Quick start

```bash
cd ~/work/repo-a
trajweave init
trajweave projects
trajweave import --all
trajweave trajectories
trajweave show TW-000001
```

Import is idempotent and restart-safe. Original session files stay in their existing Codex or Claude directories. TrajWeave stores normalized local data under `~/.trajweave/` or the directory selected by `TRAJWEAVE_HOME`.

## Commands

| Command | Purpose |
| --- | --- |
| `trajweave init [PATH]` | Register a repository for local processing. |
| `trajweave projects` | List registered repositories. |
| `trajweave import --all` | Import discovered Codex and Claude sessions. |
| `trajweave sessions` | List source sessions and import status. |
| `trajweave trajectories` | List normalized trajectories. |
| `trajweave show TW-000001` | Inspect one trajectory and its events. |
| `trajweave experiences ...` | Inspect evidence-backed recurring patterns. |
| `trajweave placement ...` | Generate and inspect deterministic placement proposals. |
| `trajweave review ...` | Review proposals without changing files. |
| `trajweave apply ID --dry-run` | Preview an accepted policy change. |
| `trajweave apply ID` | Apply an explicitly approved preview. |
| `trajweave eval ...` | Compare frozen baseline and candidate runs. |
| `trajweave lifecycle ...` | Inspect evidence-backed policy lifecycle recommendations. |

Run `trajweave --help` or a command's `--help` for current options.

## Safety and privacy

- Repository processing requires explicit opt-in.
- Raw transcripts are not copied into the TrajWeave database.
- Stored event content is normalized and common credentials are redacted.
- Policy changes require review, preview, and explicit apply.
- Existing human-written policy content is preserved.
- Evaluation uses isolated temporary repositories and does not modify the active repository.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run pytest -m "not integration"
uv run ruff check .
```

For Windows PowerShell, pip-based setup, CI-equivalent diagnostics, and GitHub
Actions commands, see
[`CONTRIBUTING.md`](CONTRIBUTING.md).

See [`SECURITY.md`](SECURITY.md) and [`docs/STAGES.md`](docs/STAGES.md) for the
security policy and architecture stages.
