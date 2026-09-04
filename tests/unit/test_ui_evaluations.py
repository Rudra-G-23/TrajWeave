from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from trajweave.config.paths import TrajWeavePaths
from trajweave.evaluation.models import VerifierCheck
from trajweave.evaluation.service import EvaluationService
from trajweave.experience import ExperienceExtractor
from trajweave.experience.config import ExperienceConfig
from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.placement import PlacementGenerator
from trajweave.review.service import ReviewService
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository
from trajweave.ui.server import make_server

pytestmark = pytest.mark.integration


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


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_api_evals_empty_on_fresh_database(tmp_path):
    db_path = tmp_path / "trajweave.db"
    Database(db_path).close()
    with _server(db_path) as base:
        status, body = _get(base, "/api/evals")
        assert status == 200
        assert body == {"evaluations": [], "schema_ok": True}


def test_api_evals_list_and_detail(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    (root / "app.py").write_text("print('hi')\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "initial")

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

    rows = repo.list_placement_proposal_sets()
    proposal_id = f"PP-E-0001-{rows[0]['recommended_type']}"
    paths = TrajWeavePaths(home)
    review_id = ReviewService(repo, paths).accept(proposal_id, agent="claude")
    ReviewService(repo, paths).test_first(review_id)

    import sys as _sys

    evaluation_id = EvaluationService(repo, paths).create_spec(
        review_id, repo=str(root), commit=None, task={"description": "noop"},
        verifiers=[VerifierCheck("task", [_sys.executable, "-c", "import sys; sys.exit(0)"])],
        agent_command=None, target_agent="claude", target_override=None,
    )
    EvaluationService(repo, paths).run_repetition(evaluation_id, count=1)
    db.close()

    with _server(home / "trajweave.db") as base:
        status, body = _get(base, "/api/evals")
        assert status == 200
        assert body["schema_ok"] is True
        assert body["evaluations"][0]["id"] == evaluation_id

        status, detail = _get(base, f"/api/evals/{evaluation_id}")
        assert status == 200
        assert detail["spec"]["id"] == evaluation_id
        assert len(detail["comparisons"]) == 1
        assert detail["comparisons"][0]["outcome"] == "unchanged"

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(base, "/api/evals/EV-does-not-exist")
        assert excinfo.value.code == 404
