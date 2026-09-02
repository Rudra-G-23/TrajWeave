"""HTTP behaviour of the Stage 4 UI server (stdlib only, no real browser)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from trajweave.models.enums import Agent, EventType, FinalStatus
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository
from trajweave.ui.server import make_server


@contextmanager
def running(db_path):
    srv = make_server(db_path, port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, json.loads(r.read())


def get_err(base, path):
    try:
        get(base, path)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    raise AssertionError("expected an HTTP error")


def _seed_db(path):
    db = Database(path)
    repo = Repository(db)
    repo.upsert_project(project_id="p1", name="repo-a", root="/repos/a", git_remote=None)
    pk = repo.record_source_session(
        agent="codex", source_session_id="s1", source_path="/x/s1.jsonl",
        source_hash="h", source_mtime=0.0, size_bytes=1, project_id="p1",
        status="imported", detail=None, cwd="/repos/a", imported=True,
    )
    traj = NormalizedTrajectory(
        agent=Agent.CODEX,
        source=SourceSessionRef(Agent.CODEX, "s1", "/x/s1.jsonl", "h", 0.0, 1),
        task="repair the billing test",
        final_status=FinalStatus.SUCCESS,
    )
    traj.started_at = "2026-09-01T10:00:00+00:00"
    traj.ended_at = "2026-09-01T10:05:00+00:00"
    for e in [
        NormalizedEvent(type=EventType.USER_PROMPT, summary="repair the billing test",
                        timestamp="2026-09-01T10:00:00+00:00"),
        NormalizedEvent(type=EventType.COMMAND, command="pytest -q", exit_code=1,
                        timestamp="2026-09-01T10:01:00+00:00",
                        metadata={"command_kind": "test"}),
        NormalizedEvent(type=EventType.TEST_FAIL, summary="test exit 1",
                        timestamp="2026-09-01T10:01:00+00:00"),
        NormalizedEvent(type=EventType.FILE_EDIT, path="app/billing.py",
                        timestamp="2026-09-01T10:02:00+00:00",
                        metadata={"lines_added": 3, "lines_removed": 1}),
        NormalizedEvent(type=EventType.COMMAND, command="pytest -q", exit_code=0,
                        timestamp="2026-09-01T10:03:00+00:00"),
        NormalizedEvent(type=EventType.TEST_PASS, summary="test exit 0",
                        timestamp="2026-09-01T10:03:00+00:00"),
        NormalizedEvent(type=EventType.COMPLETION, summary="done",
                        timestamp="2026-09-01T10:04:00+00:00"),
    ]:
        traj.add_event(e)
    traj.touch_file("app/billing.py", modified=True)
    tid, _ = repo.persist_trajectory(traj, source_pk=pk, project_id="p1")
    db.close()
    return tid


def test_static_index_served(tmp_path):
    _seed_db(tmp_path / "tw.db")
    with running(tmp_path / "tw.db") as base:
        with urllib.request.urlopen(base + "/") as r:
            assert r.status == 200
            assert b"<title>TrajWeave</title>" in r.read()
        with urllib.request.urlopen(base + "/app.js") as r:
            assert r.status == 200
            assert "javascript" in r.headers["Content-Type"]


def test_meta_projects_sessions_detail(tmp_path):
    tid = _seed_db(tmp_path / "tw.db")
    with running(tmp_path / "tw.db") as base:
        _, meta = get(base, "/api/meta")
        assert meta["db_exists"] is True and meta["schema_ok"] is True
        assert meta["counts"]["trajectories"] == 1
        assert meta["filters"]["agents"] == ["codex"]

        _, projs = get(base, "/api/projects")
        assert projs["projects"][0]["name"] == "repo-a"
        assert projs["projects"][0]["total_sessions"] == 1

        _, one = get(base, "/api/projects/p1")
        assert one["project"]["codex_count"] == 1

        _, sess = get(base, "/api/sessions")
        assert sess["total"] == 1
        assert sess["sessions"][0]["id"] == tid
        assert sess["sessions"][0]["duration_seconds"] == 300.0
        assert "source_path" not in sess["sessions"][0]  # absolute path hidden in list

        _, det = get(base, "/api/trajectories/" + tid)
        assert det["trajectory"]["final_status"] == "success"
        assert [e["type"] for e in det["events"]][:3] == [
            "user_prompt", "command", "test_fail",
        ]
        assert det["events"][1]["metadata"]["command_kind"] == "test"
        assert det["files"][0]["path"] == "app/billing.py"
        assert det["files"][0]["was_modified"] is True


def test_404s(tmp_path):
    _seed_db(tmp_path / "tw.db")
    with running(tmp_path / "tw.db") as base:
        assert get_err(base, "/api/projects/nope")[0] == 404
        assert get_err(base, "/api/trajectories/TW-999999")[0] == 404
        assert get_err(base, "/nonsense")[0] == 404


def test_missing_database_is_empty_not_error(tmp_path):
    with running(tmp_path / "absent.db") as base:
        status, meta = get(base, "/api/meta")
        assert status == 200 and meta["db_exists"] is False
        assert meta["counts"]["trajectories"] == 0

        _, projs = get(base, "/api/projects")
        assert projs == {"projects": []}

        _, sess = get(base, "/api/sessions")
        assert sess["total"] == 0 and sess["sessions"] == []


def test_deleted_project_still_lists_its_sessions(tmp_path):
    tid = _seed_db(tmp_path / "tw.db")
    db = Database(tmp_path / "tw.db")
    db.execute("DELETE FROM projects WHERE id = 'p1'")  # FK SET NULL on trajectories
    db.close()
    with running(tmp_path / "tw.db") as base:
        _, sess = get(base, "/api/sessions")
        assert sess["total"] == 1
        assert sess["sessions"][0]["project_name"] is None
        _, det = get(base, "/api/trajectories/" + tid)
        assert det["trajectory"]["project_name"] is None


def test_unknown_event_type_and_malformed_metadata_survive(tmp_path):
    tid = _seed_db(tmp_path / "tw.db")
    db = Database(tmp_path / "tw.db")
    db.execute(
        "UPDATE trajectory_events SET type = 'brand_new_kind' WHERE sequence = 1 AND trajectory_id = ?",
        (tid,),
    )
    db.execute(
        "UPDATE trajectory_events SET metadata = '{not valid json' WHERE sequence = 2 AND trajectory_id = ?",
        (tid,),
    )
    db.close()
    with running(tmp_path / "tw.db") as base:
        _, det = get(base, "/api/trajectories/" + tid)
        assert det["events"][0]["type"] == "brand_new_kind"
        bad = det["events"][1]
        assert bad["metadata"] is None
        assert bad["metadata_error"] is True
        assert bad["metadata_raw"] == "{not valid json"


def test_incompatible_schema_reports_503(tmp_path):
    db = Database(tmp_path / "tw.db")
    db.execute("DROP TABLE trajectories")
    db.close()
    with running(tmp_path / "tw.db") as base:
        _, meta = get(base, "/api/meta")
        assert meta["db_exists"] is True
        code, body = get_err(base, "/api/sessions")
        assert code == 503 and "schema" in body["error"]


def test_debug_ledger(tmp_path):
    _seed_db(tmp_path / "tw.db")
    with running(tmp_path / "tw.db") as base:
        _, dbg = get(base, "/api/debug/sessions")
        assert dbg["sessions"][0]["agent"] == "codex"
        assert dbg["sessions"][0]["status"] == "imported"
        assert dbg["sessions"][0]["source_path"] == "/x/s1.jsonl"


def test_server_is_read_only(tmp_path):
    _seed_db(tmp_path / "tw.db")
    with running(tmp_path / "tw.db") as base:
        # a GET can't write, but assert the connection itself refuses writes
        from trajweave.ui.server import reader

        with reader(tmp_path / "tw.db") as repo:
            with pytest.raises(Exception):
                repo.db.conn.execute("DELETE FROM projects")
