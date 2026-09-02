from __future__ import annotations

import json

from trajweave.projects.git import find_repo_root, read_git_info
from trajweave.projects.registry import ProjectRegistry, RepoNotFoundError, project_id_for_root
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def _registry(tmp_path) -> ProjectRegistry:
    db = Database(tmp_path / "tw.db")
    return ProjectRegistry(Repository(db))


def test_find_repo_root_from_nested_dir(git_repo):
    nested = git_repo / "app" / "api" / "src"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == git_repo
    assert find_repo_root(nested / "nonexistent_file.py") == git_repo


def test_find_repo_root_none_outside(tmp_path):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    assert find_repo_root(d) is None


def test_read_git_info(git_repo):
    info = read_git_info(git_repo / "app.py")
    assert info.root == git_repo
    assert info.branch in ("main", "master")
    assert info.commit and len(info.commit) == 40


def test_project_id_stable_across_calls(git_repo):
    assert project_id_for_root(git_repo) == project_id_for_root(str(git_repo))


def test_init_creates_marker_and_registers(tmp_path, git_repo):
    reg = _registry(tmp_path)
    project = reg.init(git_repo)
    marker = git_repo / ".trajweave" / "project.json"
    assert marker.is_file()
    data = json.loads(marker.read_text())
    assert data["project_id"] == project.project_id
    assert data["root"] == str(git_repo)
    assert data["enabled"] is True
    rows = reg.list_projects()
    assert len(rows) == 1 and rows[0]["id"] == project.project_id


def test_init_is_idempotent(tmp_path, git_repo):
    reg = _registry(tmp_path)
    p1 = reg.init(git_repo)
    created_at_1 = json.loads((git_repo / ".trajweave" / "project.json").read_text())["created_at"]
    p2 = reg.init(git_repo)
    created_at_2 = json.loads((git_repo / ".trajweave" / "project.json").read_text())["created_at"]
    assert p1.project_id == p2.project_id
    assert created_at_1 == created_at_2
    assert len(reg.list_projects()) == 1


def test_init_from_subdirectory_uses_repo_root(tmp_path, git_repo):
    reg = _registry(tmp_path)
    sub = git_repo / "pkg" / "mod"
    sub.mkdir(parents=True)
    project = reg.init(sub)
    assert project.root == str(git_repo)
    assert (git_repo / ".trajweave" / "project.json").is_file()
    assert not (sub / ".trajweave").exists()


def test_init_outside_repo_raises(tmp_path):
    reg = _registry(tmp_path)
    missing = tmp_path / "ghost.py"
    try:
        reg.init(missing)
    except RepoNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RepoNotFoundError")


def test_resolve_for_path_tracked_and_untracked(tmp_path, git_repo, other_git_repo):
    reg = _registry(tmp_path)
    reg.init(git_repo)

    resolved = reg.resolve_for_path(str(git_repo / "app" / "deep"))
    assert resolved is not None and resolved.root == str(git_repo)

    assert reg.resolve_for_path(str(other_git_repo)) is None
    assert reg.resolve_for_path(None) is None
    assert reg.resolve_for_path(str(tmp_path)) is None


def test_resolve_self_heals_from_marker(tmp_path, git_repo):
    """DB wiped but the on-disk marker remains -> project re-registers."""

    reg1 = _registry(tmp_path)
    reg1.init(git_repo)

    fresh_db = Database(tmp_path / "fresh.db")
    reg2 = ProjectRegistry(Repository(fresh_db))
    assert reg2.list_projects() == []
    resolved = reg2.resolve_for_path(str(git_repo))
    assert resolved is not None
    assert resolved.source == "marker"
    assert len(reg2.list_projects()) == 1


def test_deleted_repo_keeps_project_row(tmp_path, git_repo):
    reg = _registry(tmp_path)
    project = reg.init(git_repo)
    # Simulate the repo going away: registry still lists it.
    rows = reg.list_projects()
    assert rows[0]["id"] == project.project_id
    # resolution now fails (nothing on disk) but the row is untouched.
    import shutil

    shutil.rmtree(git_repo)
    assert reg.resolve_for_path(str(git_repo)) is None
    assert len(reg.list_projects()) == 1
