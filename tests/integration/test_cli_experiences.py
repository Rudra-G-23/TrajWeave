from __future__ import annotations

import json

import pytest

from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository

pytestmark = pytest.mark.integration


def _run(capsys, *argv) -> tuple[int, str]:
    from trajweave.cli.main import main

    code = main(list(argv))
    return code, capsys.readouterr().out


SPEC = [
    ("user_prompt", None, "add column"),
    ("file_edit", "app/models/user.py", None),
    ("test_fail", None, "E assert x"),
    ("file_create", "migrations/0007.py", None),
    ("test_pass", None, None),
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(h))
    db = Database(h / "trajweave.db")
    repo = Repository(db)
    repo.upsert_project(project_id="p1", name="alpha", root="/r/alpha", git_remote=None)
    for i in range(3):
        pk = repo.record_source_session(
            agent="claude", source_session_id=f"s{i}", source_path=f"/s/{i}.jsonl",
            source_hash=f"h{i}", source_mtime=1.0, size_bytes=1, project_id="p1",
            status="imported", detail=None, cwd="/r", imported=True,
        )
        t = NormalizedTrajectory(
            agent=Agent.CLAUDE,
            source=SourceSessionRef(Agent.CLAUDE, f"s{i}", f"/s/{i}.jsonl", f"h{i}", 1.0, 1),
            task="t", task_source=TaskSource.USER_PROMPT, final_status=FinalStatus.SUCCESS,
        )
        t.started_at = "2026-08-28T00:00:00+00:00"
        t.ended_at = "2026-08-28T00:10:00+00:00"
        for et, p, s in SPEC:
            kw = {}
            if p:
                kw["path"] = p
            if s:
                kw["summary"] = s
            t.add_event(NormalizedEvent(type=EventType(et), **kw))
        repo.persist_trajectory(t, source_pk=pk, project_id="p1")
    db.close()
    return h


def test_extract_list_show_review(home, capsys):
    code, out = _run(capsys, "experiences", "extract")
    assert code == 0
    assert "Experience candidates created: 1" in out
    assert "Pattern occurrences found:" in out

    code, out = _run(capsys, "experiences", "extract", "--json")
    data = json.loads(out)
    assert data["candidates_created"] == 1
    assert data["trajectories_analyzed"] == 0  # nothing changed on the 2nd run

    code, out = _run(capsys, "experiences", "list")
    assert code == 0 and "E-0001" in out

    code, out = _run(capsys, "experiences", "list", "--json")
    rows = json.loads(out)
    assert rows[0]["id"] == "E-0001"
    assert rows[0]["support_count"] == 3

    code, out = _run(capsys, "experiences", "show", "E-0001")
    assert code == 0
    assert "candidate reusable lesson" in out
    assert "confidence parts" in out
    assert "TW-000001" in out

    code, out = _run(capsys, "experiences", "show", "E-0001", "--json")
    payload = json.loads(out)
    assert payload["experience"]["id"] == "E-0001"
    assert len(payload["evidence"]) == 3

    code, out = _run(capsys, "experiences", "review", "E-0001",
                     "--status", "false_positive", "--note", "not useful")
    assert code == 0 and "false_positive" in out

    code, out = _run(capsys, "experiences", "show", "E-0001")
    assert "review: false_positive" in out
    assert "not useful" in out


def test_show_unknown_experience_exits_2(home, capsys):
    code, _ = _run(capsys, "experiences", "extract")
    code, out = _run(capsys, "experiences", "show", "E-9999")
    assert code == 2


def test_rebuild_flag(home, capsys):
    _run(capsys, "experiences", "extract")
    code, out = _run(capsys, "experiences", "extract", "--rebuild", "--json")
    assert json.loads(out)["trajectories_analyzed"] == 3


def test_extract_unknown_project_exits_2(home, capsys):
    code, out = _run(capsys, "experiences", "extract", "--project", "/no/such/repo")
    assert code == 2


def test_placements_generate_list_show_and_invalidate_with_stage5_candidate(home, capsys):
    """Stage 6 consumes a real Stage 5 candidate, never a fake real DB row."""

    assert _run(capsys, "experiences", "extract")[0] == 0

    code, out = _run(capsys, "placements", "generate", "--json")
    generated = json.loads(out)
    assert code == 0
    assert generated["eligible_experiences"] == 1
    assert generated["proposal_sets"] == 1
    assert generated["regenerated"] == 1

    # Same persisted Stage 5 evidence produces no duplicate set or proposals.
    code, out = _run(capsys, "placements", "generate", "--json")
    repeated = json.loads(out)
    assert code == 0
    assert repeated["regenerated"] == 0
    assert repeated["unchanged"] == 1

    code, out = _run(capsys, "placements", "list", "--json")
    listed = json.loads(out)
    assert code == 0
    assert listed[0]["experience_id"] == "E-0001"
    assert listed[0]["recommended_type"] in {
        "ignore", "global_rule", "project_rule", "scoped_rule", "skill",
    }

    code, out = _run(capsys, "placements", "show", "E-0001", "--json")
    shown = json.loads(out)
    assert code == 0
    assert [item["rank"] for item in shown["proposals"]] == [1, 2, 3, 4, 5]
    assert {item["placement_type"] for item in shown["proposals"]} == {
        "ignore", "global_rule", "project_rule", "scoped_rule", "skill",
    }
    # The read contract de-duplicates shared evidence at the set level; the
    # storage contract keeps an explicit link from every proposal to it.
    assert len(shown["evidence"]) == 3

    assert _run(capsys, "experiences", "review", "E-0001", "--status", "false_positive")[0] == 0
    code, out = _run(capsys, "placements", "generate", "--json")
    assert code == 0
    assert json.loads(out)["eligible_experiences"] == 0


def test_placements_zero_candidate_database_is_a_success(tw_home, capsys):
    code, out = _run(capsys, "--home", str(tw_home), "placements", "generate", "--json")
    assert code == 0
    assert json.loads(out)["eligible_experiences"] == 0
    assert json.loads(_run(capsys, "--home", str(tw_home), "placements", "generate", "--json")[1])["proposal_sets"] == 0
