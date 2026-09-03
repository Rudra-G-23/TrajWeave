from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from trajweave.experience import ExperienceExtractor
from trajweave.experience.config import ExperienceConfig
from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.placement import PlacementGenerator
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository
from trajweave.ui.server import make_server

pytestmark = pytest.mark.integration


def _seed(home: Path, root: Path) -> None:
    root.mkdir()
    db = Database(home / "trajweave.db")
    repo = Repository(db)
    repo.upsert_project(project_id="p1", name="repo", root=str(root), git_remote=None)
    for i in range(3):
        source = SourceSessionRef(Agent.CLAUDE, f"s{i}", f"/s/{i}.jsonl", f"h{i}", 1.0, 1)
        t = NormalizedTrajectory(agent=Agent.CLAUDE, source=source, task="repair", task_source=TaskSource.USER_PROMPT, final_status=FinalStatus.SUCCESS)
        t.started_at = "2026-08-28T00:00:00+00:00"
        t.ended_at = "2026-08-28T00:10:00+00:00"
        for event_type, path, summary in (("user_prompt", None, "repair"), ("file_edit", "app.py", None), ("test_fail", None, "failure"), ("file_create", "tests/test_app.py", None), ("test_pass", None, None)):
            kwargs = {"path": path} if path else {}
            if summary:
                kwargs["summary"] = summary
            t.add_event(NormalizedEvent(type=EventType(event_type), **kwargs))
        pk = repo.record_source_session(agent="claude", source_session_id=f"s{i}", source_path=f"/s/{i}.jsonl", source_hash=f"h{i}", source_mtime=1.0, size_bytes=1, project_id="p1", status="imported", detail=None, cwd=str(root), imported=True)
        repo.persist_trajectory(t, source_pk=pk, project_id="p1")
    ExperienceExtractor(repo, ExperienceConfig()).run()
    PlacementGenerator(repo).run()
    db.close()


def _run(capsys, *argv):
    from trajweave.cli.main import main
    code = main(list(argv))
    return code, capsys.readouterr().out


@contextmanager
def _server(db_path: Path):
    server = make_server(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(base: str, path: str, body: dict):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_review_accept_preview_apply_and_history(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    _seed(home, root)
    code, out = _run(capsys, "review", "list", "--json")
    assert code == 0
    rows = json.loads(out)
    assert rows and rows[0]["review_status"] == "unreviewed"
    proposal_id = rows[0]["recommended_proposal_id"]
    code, out = _run(capsys, "review", "accept", proposal_id, "--agent", "codex")
    assert code == 0 and "Apply remains explicit" in out
    assert not (root / "AGENTS.md").exists()
    code, out = _run(capsys, "apply", proposal_id, "--dry-run")
    assert code == 0 and "Writes: no" in out
    assert not (root / "AGENTS.md").exists()
    code, out = _run(capsys, "apply", proposal_id)
    assert code == 0 and "applied" in out
    assert "trajweave:managed" in (root / "AGENTS.md").read_text("utf-8")
    code, out = _run(capsys, "apply", proposal_id)
    assert code == 0 and "already_applied" in out
    db = Database(home / "trajweave.db")
    history = Repository(db).policy_review_history(f"RV-PS-E-0001")
    assert any(row["action"] == "accept" for row in history["actions"])
    assert len(history["applications"]) == 2
    db.close()


def test_reject_defer_test_first_and_edit_do_not_write(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    _seed(home, root)
    rows = json.loads(_run(capsys, "review", "list", "--json")[1])
    proposal_id = rows[0]["recommended_proposal_id"]
    assert _run(capsys, "review", "defer", proposal_id)[0] == 0
    assert _run(capsys, "apply", proposal_id)[0] == 2
    assert _run(capsys, "review", "test-first", proposal_id)[0] == 0
    assert _run(capsys, "apply", proposal_id)[0] == 2
    assert _run(capsys, "review", "edit", proposal_id, "--content", "Edited policy")[0] == 0
    assert not (root / "AGENTS.md").exists()
    db = Database(home / "trajweave.db")
    assert len(Repository(db).policy_review_history("RV-PS-E-0001")["variants"]) == 1
    db.close()


def test_ui_review_actions_keep_apply_separate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    _seed(home, root)
    db_path = home / "trajweave.db"
    db = Database(db_path)
    rows = Repository(db).list_placement_proposal_sets()
    db.close()
    proposal_id = f"PP-E-0001-{rows[0]['recommended_type']}"
    with _server(db_path) as base:
        with urllib.request.urlopen(base + "/api/reviews", timeout=5) as response:
            listed = json.loads(response.read())
        assert listed["reviews"][0]["review_status"] == "unreviewed"
        _post(base, f"/api/reviews/{proposal_id}/accept", {"agent": "codex"})
        assert not (root / "AGENTS.md").exists()
        _post(base, f"/api/reviews/{proposal_id}/apply", {"dry_run": True})
        assert not (root / "AGENTS.md").exists()
