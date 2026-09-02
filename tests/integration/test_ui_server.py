"""End-to-end: real import pipeline -> UI server reads it back over HTTP."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.conftest import make_claude_session, make_codex_session
from trajweave.cli.main import main
from trajweave.models.enums import Agent, EventType, FinalStatus
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository
from trajweave.ui.server import make_server

pytestmark = pytest.mark.integration


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(path: Path, filename: str) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@e.com")
    _git(path, "config", "user.name", "T")
    (path / filename).write_text("x\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


@contextmanager
def _running(db_path: Path):
    srv = make_server(db_path, port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read())


def _add_failed_and_mixed(db_path: Path, project_id: str) -> tuple[str, str]:
    db = Database(db_path)
    repo = Repository(db)

    def persist(sid, task, status, events):
        pk = repo.record_source_session(
            agent="codex", source_session_id=sid, source_path=f"/synth/{sid}.jsonl",
            source_hash=sid, source_mtime=0.0, size_bytes=1, project_id=project_id,
            status="imported", detail=None, cwd="/synth", imported=True,
        )
        traj = NormalizedTrajectory(
            agent=Agent.CODEX,
            source=SourceSessionRef(Agent.CODEX, sid, f"/synth/{sid}.jsonl", sid, 0.0, 1),
            task=task, final_status=status,
        )
        traj.started_at = "2026-09-01T09:00:00+00:00"
        traj.ended_at = "2026-09-01T09:10:00+00:00"
        for e in events:
            traj.add_event(e)
            if e.type == EventType.FILE_EDIT and e.path:
                traj.touch_file(e.path, modified=True)
        tid, _ = repo.persist_trajectory(traj, source_pk=pk, project_id=project_id)
        return tid

    failed = persist(
        "synthfail", "make the build green", FinalStatus.FAILURE,
        [
            NormalizedEvent(type=EventType.USER_PROMPT, summary="make the build green"),
            NormalizedEvent(type=EventType.COMMAND, command="make", exit_code=2),
            NormalizedEvent(type=EventType.BUILD_FAIL, summary="build exit 2"),
        ],
    )
    mixed = persist(
        "synthmixed", "fix failing billing test", FinalStatus.SUCCESS,
        [
            NormalizedEvent(type=EventType.USER_PROMPT, summary="fix failing billing test"),
            NormalizedEvent(type=EventType.COMMAND, command="pytest", exit_code=1),
            NormalizedEvent(type=EventType.TEST_FAIL, summary="test exit 1"),
            NormalizedEvent(type=EventType.FILE_EDIT, path="app/billing.py"),
            NormalizedEvent(type=EventType.COMMAND, command="pytest", exit_code=0),
            NormalizedEvent(type=EventType.TEST_PASS, summary="test exit 0"),
            NormalizedEvent(type=EventType.COMPLETION, summary="done"),
        ],
    )
    db.close()
    return failed, mixed


def test_ui_reads_a_full_dataset(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))

    repo_a = _repo(tmp_path / "repo-a", "app.py")
    repo_b = _repo(tmp_path / "repo-b", "README.md")

    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    make_codex_session(codex_root, str(repo_a), session_id="11111111-1111-1111-1111-111111111111")
    make_claude_session(claude_root, str(repo_b), session_id="22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("TRAJWEAVE_CODEX_ROOT", str(codex_root))
    monkeypatch.setenv("TRAJWEAVE_CLAUDE_ROOT", str(claude_root))

    assert main(["init", str(repo_a)]) == 0
    assert main(["init", str(repo_b)]) == 0
    assert main(["import", "--all"]) == 0
    capsys.readouterr()

    db_path = home / "trajweave.db"
    with Database(db_path) as db:
        pid_a = Repository(db).get_project_by_root(str(repo_a))["id"]
    failed_id, mixed_id = _add_failed_and_mixed(db_path, pid_a)

    with _running(db_path) as base:
        meta = _get(base, "/api/meta")
        assert meta["counts"]["projects"] == 2
        assert meta["counts"]["trajectories"] == 4
        assert set(meta["filters"]["statuses"]) == {"success", "failure"}

        projects = {p["name"]: p for p in _get(base, "/api/projects")["projects"]}
        assert projects["repo-a"]["codex_count"] == 3  # 1 imported + 2 synthetic
        assert projects["repo-b"]["claude_count"] == 1

        sess = _get(base, "/api/sessions")
        assert sess["total"] == 4
        statuses = {s["id"]: s["final_status"] for s in sess["sessions"]}
        assert statuses[failed_id] == "failure"

        only_fail = _get(base, "/api/sessions?status=failure")
        assert only_fail["total"] == 1 and only_fail["sessions"][0]["id"] == failed_id

        by_repo_b = _get(base, "/api/sessions?project=" + projects["repo-b"]["id"])
        assert by_repo_b["total"] == 1 and by_repo_b["sessions"][0]["agent"] == "claude"

        mixed = _get(base, "/api/trajectories/" + mixed_id)
        kinds = [e["type"] for e in mixed["events"]]
        assert kinds.count("test_fail") == 1 and kinds.count("test_pass") == 1
        assert any(f["path"] == "app/billing.py" for f in mixed["files"])

        codex_detail = _get(base, "/api/trajectories/TW-000001")
        assert codex_detail["trajectory"]["agent"] == "codex"
        assert codex_detail["trajectory"]["task"]
