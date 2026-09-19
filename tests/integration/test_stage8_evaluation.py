"""Stage 8 evaluation: end-to-end scenarios via the CLI, using a real git
repository and deterministic, no-network stand-ins for a coding agent and its
verifiers (see ``eval_repo`` / ``eval_scripts`` in ``tests/conftest.py``).
"""

from __future__ import annotations

import json
import shlex
import sys
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


def _cmd(*parts) -> str:
    return shlex.join([str(p) for p in parts])


def _accept_recommended(capsys, agent: str = "claude") -> str:
    code, out = _run(capsys, "review", "list", "--json")
    assert code == 0
    rows = json.loads(out)
    assert rows, "no reviewable proposals - seeding failed"
    proposal_id = rows[0]["recommended_proposal_id"]
    review_id = rows[0]["review_id"]
    assert _run(capsys, "review", "accept", proposal_id, "--agent", agent)[0] == 0
    assert _run(capsys, "review", "test-first", review_id)[0] == 0
    return review_id


@pytest.fixture
def seeded_review(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    _seed(home, tmp_path / "seed-repo")
    return _accept_recommended(capsys)


# ----------------------------------------------------------------------
# Scenario 1 - improvement
# ----------------------------------------------------------------------
def test_scenario1_improvement(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["agent"]),
        "--target-agent", "claude", "--json",
    )
    assert code == 0
    evaluation_id = json.loads(out)["evaluation_id"]
    code, out = _run(capsys, "eval", "compare", evaluation_id, "--json")
    assert code == 0
    comparisons = json.loads(out)
    assert comparisons[0]["outcome"] == "improved"
    assert comparisons[0]["task_success_delta"] == "fail->pass"
    assert comparisons[0]["regression_count"] == 0


# ----------------------------------------------------------------------
# Scenario 2 / 3 - unchanged success / unchanged failure
# ----------------------------------------------------------------------
def test_scenario2_unchanged_success(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "noop",
        "--verify", _cmd(sys.executable, eval_scripts["always_pass"]),
        "--target-agent", "claude", "--json",
    )
    assert code == 0
    evaluation_id = json.loads(out)["evaluation_id"]
    comparisons = json.loads(_run(capsys, "eval", "compare", evaluation_id, "--json")[1])
    assert comparisons[0]["outcome"] == "unchanged"
    assert comparisons[0]["task_success_delta"] == "pass->pass"


def test_scenario3_unchanged_failure(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "noop",
        "--verify", _cmd(sys.executable, eval_scripts["always_fail"]),
        "--target-agent", "claude", "--json",
    )
    assert code == 0
    evaluation_id = json.loads(out)["evaluation_id"]
    comparisons = json.loads(_run(capsys, "eval", "compare", evaluation_id, "--json")[1])
    assert comparisons[0]["outcome"] == "unchanged"
    assert comparisons[0]["task_success_delta"] == "fail->fail"


# ----------------------------------------------------------------------
# Scenario 4 - regression (candidate makes the primary check worse)
# ----------------------------------------------------------------------
def test_scenario4_regression(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "break something",
        "--verify", _cmd(sys.executable, eval_scripts["verify_app_exists"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["regressor_agent"]),
        "--target-agent", "claude", "--json",
    )
    assert code == 0
    evaluation_id = json.loads(out)["evaluation_id"]
    comparisons = json.loads(_run(capsys, "eval", "compare", evaluation_id, "--json")[1])
    assert comparisons[0]["outcome"] == "regressed"
    assert comparisons[0]["task_success_delta"] == "pass->fail"


# ----------------------------------------------------------------------
# Scenario 5 - improvement with a visible unrelated regression
# ----------------------------------------------------------------------
def test_scenario5_improvement_with_unrelated_regression(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--verify", _cmd(sys.executable, eval_scripts["verify_regression"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["agent"]),
        "--target-agent", "claude", "--json",
    )
    assert code == 0
    evaluation_id = json.loads(out)["evaluation_id"]
    comparisons = json.loads(_run(capsys, "eval", "compare", evaluation_id, "--json")[1])
    comparison = comparisons[0]
    assert comparison["outcome"] == "improved"
    assert comparison["task_success_delta"] == "fail->pass"
    assert comparison["regression_count"] == 1
    assert comparison["regression_details"][0]["checker_name"] == "check_1"


# ----------------------------------------------------------------------
# Scenario 6 - a frozen spec cannot be silently changed on rerun
# ----------------------------------------------------------------------
def test_scenario6_frozen_spec_rejects_changed_conditions(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--target-agent", "claude", "--json",
    )
    evaluation_id = json.loads(out)["evaluation_id"]
    before = json.loads(_run(capsys, "eval", "show", evaluation_id, "--json")[1])
    code, _ = _run(
        capsys, "eval", "run", evaluation_id,
        "--verify", _cmd(sys.executable, eval_scripts["always_fail"]),
    )
    assert code == 2
    after = json.loads(_run(capsys, "eval", "show", evaluation_id, "--json")[1])
    assert after["comparisons"] == before["comparisons"]


# ----------------------------------------------------------------------
# Scenario 7 - candidate application failure; baseline stays auditable
# ----------------------------------------------------------------------
def test_scenario7_candidate_apply_failure_preserves_baseline(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["agent"]),
        "--target-agent", "claude", "--target", "../escape.md", "--json",
    )
    assert code == 0
    evaluation_id = json.loads(out)["evaluation_id"]
    payload = json.loads(_run(capsys, "eval", "show", evaluation_id, "--json")[1])
    runs_by_condition = {r["condition"]: r for r in payload["runs"]}
    assert runs_by_condition["baseline"]["status"] == "completed"
    assert runs_by_condition["candidate"]["status"] == "failed"
    assert "refused" in runs_by_condition["candidate"]["error_reason"]
    comparison = payload["comparisons"][0]
    assert comparison["outcome"] == "invalid"
    assert "candidate" in comparison["invalid_reason"]


# ----------------------------------------------------------------------
# Scenario 8 / 9 - isolation and active-repository protection
# ----------------------------------------------------------------------
def test_scenario8_and_9_isolation_and_active_repo_untouched(capsys, seeded_review, eval_repo, eval_scripts):
    before_legacy = (eval_repo / "legacy.txt").read_text()
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--verify", _cmd(sys.executable, eval_scripts["verify_regression"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["agent"]),
        "--target-agent", "claude", "--json",
    )
    assert code == 0
    # The developer's real repository never sees the candidate policy, the
    # agent's fix.txt, or the regression it introduced in the sandbox.
    assert not (eval_repo / "AGENTS.md").exists()
    assert not (eval_repo / "CLAUDE.md").exists()
    assert not (eval_repo / "fix.txt").exists()
    assert (eval_repo / "legacy.txt").read_text() == before_legacy


# ----------------------------------------------------------------------
# Scenario 10 - repeated trials append, never overwrite
# ----------------------------------------------------------------------
def test_scenario10_repeated_trials_append(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["agent"]),
        "--target-agent", "claude", "--json",
    )
    evaluation_id = json.loads(out)["evaluation_id"]
    assert _run(capsys, "eval", "run", evaluation_id, "--repetitions", "2")[0] == 0
    payload = json.loads(_run(capsys, "eval", "show", evaluation_id, "--json")[1])
    assert [c["repetition_index"] for c in payload["comparisons"]] == [1, 2, 3]
    assert all(c["outcome"] == "improved" for c in payload["comparisons"])


# ----------------------------------------------------------------------
# Scenario 11 - missing telemetry is represented, not fabricated
# ----------------------------------------------------------------------
def test_scenario11_missing_telemetry_is_explicit(capsys, seeded_review, eval_repo, eval_scripts):
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(eval_repo), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["agent"]),
        "--target-agent", "claude", "--json",
    )
    evaluation_id = json.loads(out)["evaluation_id"]
    payload = json.loads(_run(capsys, "eval", "show", evaluation_id, "--json")[1])
    for run in payload["runs"]:
        metrics = json.loads(run["metrics_json"])
        assert metrics["tokens"] is None


# ----------------------------------------------------------------------
# Scenario 12 - zero-candidate database stays a clean empty state
# ----------------------------------------------------------------------
def test_scenario12_zero_candidate_clean_state(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    code, out = _run(capsys, "eval", "list", "--json")
    assert code == 0
    assert json.loads(out) == []
    code, out = _run(capsys, "eval", "list")
    assert code == 0
    assert "No evaluations yet" in out


# ----------------------------------------------------------------------
# Error paths
# ----------------------------------------------------------------------
def test_run_rejects_unreviewed_proposal(tmp_path, monkeypatch, capsys, eval_repo, eval_scripts):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    _seed(home, tmp_path / "seed-repo")
    rows = json.loads(_run(capsys, "review", "list", "--json")[1])
    proposal_id = rows[0]["recommended_proposal_id"]
    code, _ = _run(
        capsys, "eval", "run", proposal_id, "--repo", str(eval_repo), "--task", "x",
        "--verify", _cmd(sys.executable, eval_scripts["always_pass"]),
    )
    assert code == 2


def test_run_requires_repo_when_creating(capsys, seeded_review, eval_scripts):
    code, _ = _run(
        capsys, "eval", "run", seeded_review, "--task", "x",
        "--verify", _cmd(sys.executable, eval_scripts["always_pass"]),
    )
    assert code == 2


def test_run_rejects_non_git_repo(tmp_path, capsys, seeded_review, eval_scripts):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    code, _ = _run(
        capsys, "eval", "run", seeded_review, "--repo", str(not_a_repo), "--task", "x",
        "--verify", _cmd(sys.executable, eval_scripts["always_pass"]),
    )
    assert code == 2


def test_run_rejects_stale_review(tmp_path, monkeypatch, capsys, eval_repo, eval_scripts):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    _seed(home, tmp_path / "seed-repo")
    review_id = _accept_recommended(capsys)
    # Force Stage 6 to regenerate under the accepted review, marking it stale.
    db = Database(home / "trajweave.db")
    repo = Repository(db)
    from trajweave.placement import PlacementGenerator

    PlacementGenerator(repo).run()
    db.execute("UPDATE placement_proposal_sets SET source_fingerprint = 'changed-fp'")
    db.close()
    code, _ = _run(
        capsys, "eval", "run", review_id, "--repo", str(eval_repo), "--task", "x",
        "--verify", _cmd(sys.executable, eval_scripts["always_pass"]),
        "--target-agent", "claude",
    )
    assert code == 2


# ----------------------------------------------------------------------
# Adversarial: the source repository disappears between spec creation and
# a later repetition. Infrastructure failure, not a fabricated task failure.
# ----------------------------------------------------------------------
def test_deleted_repository_produces_invalid_comparison(capsys, seeded_review, eval_repo, eval_scripts, tmp_path):
    movable = tmp_path / "movable-repo"
    eval_repo.rename(movable)
    code, out = _run(
        capsys, "eval", "run", seeded_review,
        "--repo", str(movable), "--task", "fix the bug",
        "--verify", _cmd(sys.executable, eval_scripts["verify_task"]),
        "--agent-cmd", _cmd(sys.executable, eval_scripts["agent"]),
        "--target-agent", "claude", "--json",
    )
    assert code == 0
    evaluation_id = json.loads(out)["evaluation_id"]

    import shutil
    shutil.rmtree(movable)

    code, _ = _run(capsys, "eval", "run", evaluation_id, "--repetitions", "1")
    assert code == 0  # infra failures are recorded, not raised past the CLI
    payload = json.loads(_run(capsys, "eval", "show", evaluation_id, "--json")[1])
    second_rep = [c for c in payload["comparisons"] if c["repetition_index"] == 2][0]
    assert second_rep["outcome"] == "invalid"
    runs = [r for r in payload["runs"] if r["repetition_index"] == 2]
    assert all(r["status"] == "error" for r in runs)
