from __future__ import annotations

from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def _db(tmp_path) -> Database:
    return Database(tmp_path / "tw.db")


def test_migrations_apply_once(tmp_path):
    path = tmp_path / "tw.db"
    db1 = Database(path)
    assert db1.schema_version == 2
    db1.close()
    db2 = Database(path)  # re-open: no error, still at head
    assert db2.schema_version == 2
    # v2 tables exist
    names = {
        r["name"]
        for r in db2.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"experiences", "experience_occurrences", "experience_evidence"} <= names


def test_upsert_project_idempotent(tmp_path):
    repo = Repository(_db(tmp_path))
    a = repo.upsert_project(project_id="tw_proj_abc", name="repo-a", root="/x/repo-a", git_remote=None)
    b = repo.upsert_project(project_id="tw_proj_abc", name="repo-a", root="/x/repo-a", git_remote="g")
    assert a["id"] == b["id"]
    assert len(repo.list_projects()) == 1
    assert repo.get_project_by_root("/x/repo-a")["git_remote"] == "g"


def _traj(session_id="s1", path="/sessions/s1.jsonl", status=FinalStatus.SUCCESS):
    src = SourceSessionRef(
        agent=Agent.CODEX, source_session_id=session_id, source_path=path,
        source_hash="h", source_mtime=1.0, size_bytes=10,
    )
    t = NormalizedTrajectory(agent=Agent.CODEX, source=src, task="do x",
                             task_source=TaskSource.USER_PROMPT, final_status=status)
    t.add_event(NormalizedEvent(type=EventType.USER_PROMPT, summary="do x"))
    t.add_event(NormalizedEvent(type=EventType.COMMAND, command="pytest", exit_code=0))
    t.add_event(NormalizedEvent(type=EventType.TEST_PASS))
    t.touch_file("app/x.py", modified=True)
    return t


def test_persist_trajectory_and_dedup(tmp_path):
    repo = Repository(_db(tmp_path))
    repo.upsert_project(project_id="tw_proj_abc", name="repo-a", root="/x/repo-a", git_remote=None)
    pk = repo.record_source_session(
        agent="codex", source_session_id="s1", source_path="/sessions/s1.jsonl",
        source_hash="h", source_mtime=1.0, size_bytes=10, project_id="tw_proj_abc",
        status="failed", detail="in progress", cwd="/x/repo-a",
    )
    tid, replaced = repo.persist_trajectory(_traj(), source_pk=pk, project_id="tw_proj_abc")
    assert tid == "TW-000001" and replaced is False
    assert repo.counts()["events"] == 3
    assert repo.counts()["trajectories"] == 1

    # Re-persist for the same source -> same id, replaced, no duplication.
    tid2, replaced2 = repo.persist_trajectory(_traj(), source_pk=pk, project_id="tw_proj_abc")
    assert tid2 == "TW-000001" and replaced2 is True
    assert repo.counts()["trajectories"] == 1
    assert repo.counts()["events"] == 3

    files = repo.get_trajectory_files("TW-000001")
    assert [f["path"] for f in files] == ["app/x.py"]


def test_trajectory_ids_are_sequential(tmp_path):
    repo = Repository(_db(tmp_path))
    for i in range(3):
        pk = repo.record_source_session(
            agent="codex", source_session_id=f"s{i}", source_path=f"/s/{i}.jsonl",
            source_hash="h", source_mtime=1.0, size_bytes=1, project_id=None,
            status="failed", detail=None, cwd=None,
        )
        tid, _ = repo.persist_trajectory(
            _traj(session_id=f"s{i}", path=f"/s/{i}.jsonl"), source_pk=pk, project_id=None
        )
        assert tid == f"TW-{i + 1:06d}"


def test_record_source_session_upsert(tmp_path):
    repo = Repository(_db(tmp_path))
    pk1 = repo.record_source_session(
        agent="claude", source_session_id="x", source_path="/p.jsonl", source_hash="h1",
        source_mtime=1.0, size_bytes=1, project_id=None, status="ignored_unregistered",
        detail="no repo", cwd="/nope",
    )
    pk2 = repo.record_source_session(
        agent="claude", source_session_id="x", source_path="/p.jsonl", source_hash="h2",
        source_mtime=2.0, size_bytes=2, project_id=None, status="imported",
        detail=None, cwd="/nope", imported=True,
    )
    assert pk1 == pk2
    rows = repo.list_source_sessions(agent="claude")
    assert len(rows) == 1
    assert rows[0]["status"] == "imported"
    assert rows[0]["source_hash"] == "h2"
