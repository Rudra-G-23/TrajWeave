from __future__ import annotations

import pytest

from tests.conftest import make_claude_session, make_codex_session
from trajweave.ingest.importer import Importer
from trajweave.projects.registry import ProjectRegistry
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository

pytestmark = pytest.mark.integration


def _setup(tmp_path):
    codex_root = tmp_path / "codex-sessions"
    claude_root = tmp_path / "claude-projects"
    db = Database(tmp_path / "tw.db")
    importer = Importer(
        db, adapter_roots={"codex": codex_root, "claude": claude_root}
    )
    return db, importer, codex_root, claude_root


def test_end_to_end_init_import_reimport(tmp_path, git_repo):
    db, importer, codex_root, claude_root = _setup(tmp_path)
    ProjectRegistry(importer.repo).init(git_repo)

    make_codex_session(codex_root, str(git_repo))
    make_claude_session(claude_root, str(git_repo))

    stats = importer.run()
    assert stats.discovered == {"codex": 1, "claude": 1}
    assert stats.imported == 2
    assert stats.ignored_unregistered == 0
    assert stats.failed == 0

    repo = Repository(db)
    trajs = repo.list_trajectories()
    assert len(trajs) == 2
    agents = {t["agent"] for t in trajs}
    assert agents == {"codex", "claude"}
    for t in trajs:
        assert t["project_name"] == git_repo.name
        assert t["repository_root"] == str(git_repo)
        assert t["event_count"] > 0
        assert t["task"]

    # Re-run: idempotent, zero new trajectories.
    stats2 = importer.run()
    assert stats2.already_imported == 2
    assert stats2.imported == 0
    assert stats2.reimported == 0
    assert len(Repository(db).list_trajectories()) == 2
    assert Repository(db).counts()["trajectories"] == 2


def test_session_in_unregistered_repo_is_ignored(tmp_path, git_repo, other_git_repo):
    db, importer, codex_root, claude_root = _setup(tmp_path)
    ProjectRegistry(importer.repo).init(git_repo)

    make_codex_session(codex_root, str(git_repo), session_id="aaaa1111-0000-0000-0000-000000000001")
    make_codex_session(codex_root, str(other_git_repo), session_id="bbbb2222-0000-0000-0000-000000000002",
                       filename="rollout-2026-09-01T11-00-00-other.jsonl")

    stats = importer.run(["codex"])
    assert stats.discovered["codex"] == 2
    assert stats.imported == 1
    assert stats.ignored_unregistered == 1

    repo = Repository(db)
    trajs = repo.list_trajectories()
    assert len(trajs) == 1
    assert trajs[0]["repository_root"] == str(git_repo)

    ignored = repo.list_source_sessions(status="ignored_unregistered")
    assert len(ignored) == 1
    assert ignored[0]["project_id"] is None


def test_changed_session_is_reimported_in_place(tmp_path, git_repo):
    db, importer, codex_root, _ = _setup(tmp_path)
    ProjectRegistry(importer.repo).init(git_repo)
    path = make_codex_session(codex_root, str(git_repo))

    importer.run(["codex"])
    repo = Repository(db)
    before = repo.list_trajectories()[0]
    assert before["id"] == "TW-000001"

    # Append another user turn -> content hash changes.
    with path.open("a") as fh:
        fh.write(
            '{"timestamp":"2026-09-01T10:00:09.000Z","ordinal":9,"type":"event_msg",'
            '"payload":{"type":"item_completed","item":{"type":"UserMessage","id":"u2",'
            '"content":[{"type":"text","text":"also add docs"}]}}}\n'
        )
    stats = importer.run(["codex"])
    assert stats.reimported == 1
    assert stats.imported == 0
    after = repo.list_trajectories()
    assert len(after) == 1
    assert after[0]["id"] == "TW-000001"  # id preserved
    assert after[0]["event_count"] > before["event_count"]


def test_agent_filter(tmp_path, git_repo):
    db, importer, codex_root, claude_root = _setup(tmp_path)
    ProjectRegistry(importer.repo).init(git_repo)
    make_codex_session(codex_root, str(git_repo))
    make_claude_session(claude_root, str(git_repo))

    stats = importer.run(["claude"])
    assert set(stats.discovered) == {"claude"}
    trajs = Repository(db).list_trajectories()
    assert len(trajs) == 1 and trajs[0]["agent"] == "claude"


def test_project_filter(tmp_path, git_repo, other_git_repo):
    db, importer, codex_root, _ = _setup(tmp_path)
    reg = ProjectRegistry(importer.repo)
    reg.init(git_repo)
    reg.init(other_git_repo)
    make_codex_session(codex_root, str(git_repo), session_id="1111aaaa-0000-0000-0000-000000000001")
    make_codex_session(codex_root, str(other_git_repo), session_id="2222bbbb-0000-0000-0000-000000000002",
                       filename="rollout-2026-09-01T12-00-00-b.jsonl")

    stats = importer.run(["codex"], project_filter=git_repo)
    assert stats.imported == 1
    assert stats.skipped_filtered == 1
    trajs = Repository(db).list_trajectories()
    assert {t["repository_root"] for t in trajs} == {str(git_repo)}


def test_malformed_session_counts_as_failed_not_crash(tmp_path, git_repo, monkeypatch):
    db, importer, codex_root, _ = _setup(tmp_path)
    ProjectRegistry(importer.repo).init(git_repo)
    make_codex_session(codex_root, str(git_repo))

    # Force the adapter to blow up on parse for this one session.
    from trajweave.adapters.codex import CodexAdapter

    def boom(self, session, repo_root=None):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(CodexAdapter, "parse", boom)
    stats = importer.run(["codex"])
    assert stats.failed == 1
    assert stats.failures and "synthetic parse failure" in stats.failures[0][2]
    sessions = Repository(db).list_source_sessions(status="failed")
    assert len(sessions) == 1


def test_dry_run_stores_nothing(tmp_path, git_repo):
    db, importer, codex_root, claude_root = _setup(tmp_path)
    ProjectRegistry(importer.repo).init(git_repo)
    make_codex_session(codex_root, str(git_repo))
    make_claude_session(claude_root, str(git_repo))

    stats = importer.run(dry_run=True)
    assert stats.dry_run == 2
    assert Repository(db).counts()["trajectories"] == 0
