from __future__ import annotations

from pathlib import Path

import pytest

from trajweave.evaluation.workspace import (
    IsolationError,
    canonical_repo_root,
    isolated_workspaces,
    resolve_commit,
)


def test_canonical_repo_root_requires_git(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(IsolationError, match="git repository"):
        canonical_repo_root(plain)


def test_canonical_repo_root_rejects_shared_system_dir(tmp_path, monkeypatch):
    import trajweave.evaluation.workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "find_repo_root", lambda _p: Path("/"))
    with pytest.raises(IsolationError, match="shared system directory"):
        canonical_repo_root(tmp_path)


def test_resolve_commit_rejects_unknown_ref(git_repo):
    with pytest.raises(IsolationError):
        resolve_commit(git_repo, "not-a-real-ref")


def test_resolve_commit_defaults_to_head(git_repo):
    sha = resolve_commit(git_repo, None)
    assert len(sha) == 40


def test_isolated_workspaces_are_independent_and_cleaned_up(git_repo):
    commit = resolve_commit(git_repo, None)
    captured = {}
    with isolated_workspaces(git_repo, commit) as (baseline, candidate):
        assert baseline != candidate
        assert baseline.is_dir() and candidate.is_dir()
        # Both are real, independent git checkouts of the same commit.
        assert (baseline / "app.py").read_text() == (candidate / "app.py").read_text()
        (baseline / "only-baseline.txt").write_text("b")
        (candidate / "only-candidate.txt").write_text("c")
        assert not (candidate / "only-baseline.txt").exists()
        assert not (baseline / "only-candidate.txt").exists()
        captured["base"] = baseline.parent
    # The whole ephemeral parent directory is gone after the context exits.
    assert not captured["base"].exists()


def test_isolated_workspaces_cleaned_up_even_on_exception(git_repo):
    commit = resolve_commit(git_repo, None)
    holder = {}
    with pytest.raises(RuntimeError):
        with isolated_workspaces(git_repo, commit) as (baseline, candidate):
            holder["base"] = baseline.parent
            raise RuntimeError("boom")
    assert not holder["base"].exists()


def test_isolated_workspaces_never_touch_the_source_repo(git_repo):
    before = (git_repo / "app.py").read_text()
    commit = resolve_commit(git_repo, None)
    with isolated_workspaces(git_repo, commit) as (baseline, candidate):
        (baseline / "app.py").write_text("mutated in sandbox\n")
        (candidate / "AGENTS.md").write_text("candidate policy\n")
    assert (git_repo / "app.py").read_text() == before
    assert not (git_repo / "AGENTS.md").exists()
