"""Read-layer queries added for the Stage 4 UI."""

from __future__ import annotations

from trajweave.models.enums import Agent, EventType, FinalStatus
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def _src(agent: str, sid: str) -> SourceSessionRef:
    return SourceSessionRef(
        agent=Agent(agent),
        source_session_id=sid,
        source_path=f"/tmp/{agent}/{sid}.jsonl",
        source_hash="deadbeef",
        source_mtime=0.0,
        size_bytes=10,
    )


def _persist(
    repo: Repository,
    *,
    agent: str,
    sid: str,
    project_id: str | None,
    task: str,
    status: FinalStatus,
    events: list[NormalizedEvent],
    source_path: str | None = None,
) -> str:
    src = _src(agent, sid)
    if source_path:
        src.source_path = source_path
    pk = repo.record_source_session(
        agent=agent,
        source_session_id=sid,
        source_path=src.source_path,
        source_hash=src.source_hash,
        source_mtime=0.0,
        size_bytes=10,
        project_id=project_id,
        status="imported",
        detail=None,
        cwd="/tmp",
        imported=True,
    )
    traj = NormalizedTrajectory(agent=Agent(agent), source=src, task=task, final_status=status)
    traj.started_at = "2026-09-01T10:00:00+00:00"
    traj.ended_at = "2026-09-01T10:04:00+00:00"
    for e in events:
        traj.add_event(e)
    tid, _ = repo.persist_trajectory(traj, source_pk=pk, project_id=project_id)
    return tid


def _db(tmp_path) -> Database:
    return Database(tmp_path / "tw.db")


def _seed(repo: Repository) -> None:
    repo.upsert_project(project_id="p_a", name="repo-a", root="/repos/a", git_remote=None)
    repo.upsert_project(project_id="p_b", name="repo-b", root="/repos/b", git_remote=None)
    _persist(
        repo, agent="codex", sid="c1", project_id="p_a", task="add coupon endpoint",
        status=FinalStatus.SUCCESS,
        events=[
            NormalizedEvent(type=EventType.USER_PROMPT, summary="add coupon endpoint"),
            NormalizedEvent(type=EventType.COMMAND, command="pytest", exit_code=0),
            NormalizedEvent(type=EventType.TEST_PASS, summary="test exit 0"),
        ],
    )
    _persist(
        repo, agent="claude", sid="c2", project_id="p_a", task="fix flaky test",
        status=FinalStatus.FAILURE,
        events=[NormalizedEvent(type=EventType.COMMAND, command="pytest", exit_code=1)],
    )
    _persist(
        repo, agent="claude", sid="c3", project_id="p_b", task="write docs",
        status=FinalStatus.SUCCESS,
        events=[NormalizedEvent(type=EventType.FILE_CREATE, path="README.md")],
        source_path="/root/.claude/projects/x/abc/subagents/agent-01.jsonl",
    )


def test_project_summaries_aggregate(tmp_path):
    with _db(tmp_path) as db:
        repo = Repository(db)
        _seed(repo)
        rows = {r["id"]: dict(r) for r in repo.list_project_summaries()}
        assert rows["p_a"]["total_sessions"] == 2
        assert rows["p_a"]["codex_count"] == 1
        assert rows["p_a"]["claude_count"] == 1
        assert rows["p_b"]["total_sessions"] == 1
        assert rows["p_a"]["last_activity"] == "2026-09-01T10:00:00+00:00"


def test_project_with_no_trajectories_still_listed(tmp_path):
    with _db(tmp_path) as db:
        repo = Repository(db)
        repo.upsert_project(project_id="p_empty", name="fresh", root="/repos/fresh", git_remote=None)
        rows = {r["id"]: dict(r) for r in repo.list_project_summaries()}
        assert rows["p_empty"]["total_sessions"] == 0
        assert rows["p_empty"]["last_activity"] is None


def test_sessions_page_filters_and_pagination(tmp_path):
    with _db(tmp_path) as db:
        repo = Repository(db)
        _seed(repo)

        rows, total = repo.list_sessions_page(limit=2, offset=0)
        assert total == 3 and len(rows) == 2

        rows, total = repo.list_sessions_page(agent="claude")
        assert total == 2 and all(r["agent"] == "claude" for r in rows)

        rows, total = repo.list_sessions_page(status="failure")
        assert total == 1 and rows[0]["final_status"] == "failure"

        rows, total = repo.list_sessions_page(project_id="p_b")
        assert total == 1 and rows[0]["is_subagent"] == 1

        rows, total = repo.list_sessions_page(query="coupon")
        assert total == 1 and "coupon" in rows[0]["task"]

        rows, total = repo.list_sessions_page(query="TW-")
        assert total == 3


def test_trajectory_detail_has_provenance(tmp_path):
    with _db(tmp_path) as db:
        repo = Repository(db)
        _seed(repo)
        rows, _ = repo.list_sessions_page(project_id="p_b")
        detail = dict(repo.get_trajectory_detail(rows[0]["id"]))
        assert detail["project_name"] == "repo-b"
        assert detail["source_path"].endswith("subagents/agent-01.jsonl")
        assert detail["is_subagent"] == 1


def test_distinct_filter_values(tmp_path):
    with _db(tmp_path) as db:
        repo = Repository(db)
        _seed(repo)
        vals = repo.distinct_filter_values()
        assert vals["agents"] == ["claude", "codex"]
        assert set(vals["statuses"]) == {"success", "failure"}
