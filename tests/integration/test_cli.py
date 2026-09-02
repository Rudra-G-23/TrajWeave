from __future__ import annotations

import json

import pytest

from tests.conftest import make_codex_session
from trajweave.cli.main import main

pytestmark = pytest.mark.integration


def _run(capsys, *argv) -> tuple[int, str]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, out


def test_help_and_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    code, out = _run(capsys)  # no command -> help
    assert code == 0
    assert "trajweave" in out


def test_full_cli_flow(tmp_path, git_repo, capsys, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    codex_root = tmp_path / "codex"
    make_codex_session(codex_root, str(git_repo))
    monkeypatch.setenv("TRAJWEAVE_CODEX_ROOT", str(codex_root))

    # init
    code, out = _run(capsys, "init", str(git_repo))
    assert code == 0
    assert "Initialized TrajWeave" in out

    # projects
    code, out = _run(capsys, "projects")
    assert code == 0 and git_repo.name in out

    # import (codex only, since claude root unset -> empty)
    code, out = _run(capsys, "import", "--agent", "codex")
    assert code == 0
    assert "Imported:               1" in out

    # trajectories
    code, out = _run(capsys, "trajectories", "--json")
    rows = json.loads(out)
    assert len(rows) == 1
    tid = rows[0]["id"]
    assert rows[0]["agent"] == "codex"

    # show
    code, out = _run(capsys, "show", tid)
    assert code == 0
    assert "files changed" in out
    assert "app/billing.py" in out

    # sessions
    code, out = _run(capsys, "sessions", "--json")
    sessions = json.loads(out)
    assert sessions[0]["status"] == "imported"

    # re-import: idempotent
    code, out = _run(capsys, "import", "--all")
    assert "Already imported:       1" in out


def test_import_respects_codex_root_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRAJWEAVE_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("TRAJWEAVE_CODEX_ROOT", str(tmp_path / "empty"))
    monkeypatch.setenv("TRAJWEAVE_CLAUDE_ROOT", str(tmp_path / "empty2"))
    code, out = _run(capsys, "import", "--all")
    assert code == 0
    assert "Codex sessions discovered: 0" in out
