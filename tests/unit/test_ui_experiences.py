"""HTTP surface for the Stage 5 Experiences UI (stdlib only, no browser)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

from trajweave.experience import ExperienceExtractor
from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
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
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
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


_SPEC = [
    ("user_prompt", None, "add a column"),
    ("file_edit", "app/models/user.py", None),
    ("test_fail", None, "E   assert user.x"),
    ("file_create", "migrations/0007.py", None),
    ("test_pass", None, None),
]


def _seed(path):
    db = Database(path)
    repo = Repository(db)
    repo.upsert_project(project_id="p1", name="alpha", root="/r/a", git_remote=None)
    repo.upsert_project(project_id="p2", name="beta", root="/r/b", git_remote=None)
    for i in range(3):
        proj = "p1" if i < 2 else "p2"
        pk = repo.record_source_session(
            agent="claude", source_session_id=f"s{i}", source_path=f"/s/{i}.jsonl",
            source_hash=f"h{i}", source_mtime=0.0, size_bytes=1, project_id=proj,
            status="imported", detail=None, cwd="/r", imported=True,
        )
        t = NormalizedTrajectory(
            agent=Agent.CLAUDE,
            source=SourceSessionRef(Agent.CLAUDE, f"s{i}", f"/s/{i}.jsonl", f"h{i}", 0.0, 1),
            task="do the thing", task_source=TaskSource.USER_PROMPT,
            final_status=FinalStatus.SUCCESS,
        )
        t.started_at = "2026-08-28T00:00:00+00:00"
        t.ended_at = "2026-08-28T00:10:00+00:00"
        for et, p, s in _SPEC:
            kw = {}
            if p:
                kw["path"] = p
            if s:
                kw["summary"] = s
            t.add_event(NormalizedEvent(type=EventType(et), **kw))
        repo.persist_trajectory(t, source_pk=pk, project_id=proj)
    ExperienceExtractor(repo).run()
    db.close()


def test_experiences_list_and_detail(tmp_path):
    db_path = tmp_path / "tw.db"
    _seed(db_path)
    with running(db_path) as base:
        st, body = get(base, "/api/experiences")
        assert st == 200
        assert body["counts"]["candidates"] == 1
        exp = body["experiences"][0]
        assert exp["id"] == "E-0001"
        assert exp["support_count"] == 3
        assert exp["confidence"] > 0
        assert "confidence_json" not in exp  # not leaked to the list view

        st, body = get(base, "/api/experiences/E-0001")
        assert st == 200
        assert body["experience"]["reusable_lesson"]
        assert body["experience"]["confidence_breakdown"]["components"]["support_ratio"] == 1.0
        assert isinstance(body["experience"]["context"], list)
        rels = sorted(e["relationship"] for e in body["evidence"])
        assert rels == ["support", "support", "support"]
        ev = body["evidence"][0]
        assert ev["trajectory_id"].startswith("TW-")
        assert ev["start_sequence"] and ev["end_sequence"]

        st, body = get(base, "/api/experiences?status=candidate")
        assert len(body["experiences"]) == 1
        st, body = get(base, "/api/experiences?status=rejected")
        assert body["experiences"] == []

        code, body = get_err(base, "/api/experiences/E-9999")
        assert code == 404


def test_experiences_empty_when_no_db(tmp_path):
    with running(tmp_path / "missing.db") as base:
        st, body = get(base, "/api/experiences")
        assert st == 200
        assert body["experiences"] == []


def test_experiences_on_pre_stage5_schema(tmp_path):
    # a v1 database (no experience tables) must not 500 the endpoint
    db_path = tmp_path / "old.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);"
        "INSERT INTO schema_migrations VALUES (1);"
        "CREATE TABLE trajectories(id TEXT);"
    )
    conn.close()
    with running(db_path) as base:
        st, body = get(base, "/api/experiences")
        assert st == 200
        assert body["experiences"] == []
