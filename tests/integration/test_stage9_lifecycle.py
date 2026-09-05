"""Stage 9: policy lifecycle learning - end to end via the real service,
CLI, and Stage 7 file-apply safety on a real git repository."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from trajweave.config.paths import get_paths
from trajweave.experience import ExperienceExtractor
from trajweave.experience.config import ExperienceConfig
from trajweave.lifecycle.errors import LifecycleError
from trajweave.lifecycle.service import LifecycleService
from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.placement import PlacementGenerator
from trajweave.review.service import ReviewService
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository

pytestmark = pytest.mark.integration


def _seed(home: Path, root: Path) -> None:
    root.mkdir(exist_ok=True)
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
    from trajweave.ui.server import make_server

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
        return json.loads(response.read())


def _post(base: str, path: str, body: dict):
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def home_and_repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "repo"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    _seed(home, root)
    return home, root


def _accepted_review_id(home: Path) -> str:
    db = Database(home / "trajweave.db")
    repo = Repository(db)
    paths = get_paths(str(home))
    reviews = ReviewService(repo, paths).list()
    proposal_id = reviews[0]["recommended_proposal_id"]
    review_id = ReviewService(repo, paths).accept(proposal_id, agent="codex")
    db.close()
    return review_id


def _service(home: Path) -> tuple[Database, LifecycleService]:
    db = Database(home / "trajweave.db")
    return db, LifecycleService(Repository(db), get_paths(str(home)))


def _force_version(service: LifecycleService, policy_id: str, **overrides) -> dict:
    """Test-only helper to seed a specific starting placement/scope for a
    policy. policy_versions rows are immutable (DB-trigger enforced), so this
    inserts a fresh version through the same internal path rewrite()/
    promote()/demote() use, rather than ever UPDATE-ing an existing row."""

    current = service.show(policy_id)["current_version"]
    fields = {
        "placement_type": current["placement_type"], "target_agent": current["target_agent"],
        "target_override": current["target_override"], "scope_type": current["scope_type"],
        "scope_value": current["scope_value"], "content": current["content"],
    }
    fields.update(overrides)
    new_version = service._new_version(
        policy_id, created_via="rewrite", created_from_version_id=current["id"], **fields,
    )
    service.repo.set_policy_version_status(current["id"], "superseded")
    service.repo.set_policy_current_version(policy_id, new_version["id"])
    return new_version


# ----------------------------------------------------------------------
# adopt
# ----------------------------------------------------------------------
def test_adopt_creates_policy_v1(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    payload = service.show(policy_id)
    assert payload["policy"]["status"] == "active"
    assert len(payload["versions"]) == 1
    assert payload["current_version"]["version_number"] == 1
    assert payload["current_version"]["created_via"] == "initial"
    db.close()


def test_adopt_is_idempotent(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    a = service.adopt(review_id)
    b = service.adopt(review_id)
    assert a == b
    assert len(service.list()) == 1
    db.close()


def test_adopt_rejects_unreviewed(home_and_repo):
    home, root = home_and_repo
    db = Database(home / "trajweave.db")
    repo = Repository(db)
    reviews = ReviewService(repo, get_paths(str(home))).list()
    proposal_id = reviews[0]["recommended_proposal_id"]
    db.close()
    db, service = _service(home)
    with pytest.raises(LifecycleError):
        service.adopt(proposal_id)
    db.close()


# ----------------------------------------------------------------------
# rewrite
# ----------------------------------------------------------------------
def test_rewrite_creates_new_version_and_preserves_old(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    v1 = service.show(policy_id)["current_version"]
    v2_id = service.rewrite(policy_id, "when persistent schema changes are made, verify the migration", reason="clarify wording")
    payload = service.show(policy_id)
    assert payload["current_version"]["id"] == v2_id
    assert payload["current_version"]["version_number"] == 2
    v1_after = next(v for v in payload["versions"] if v["id"] == v1["id"])
    assert v1_after["status"] == "superseded"
    assert v1_after["content"] == v1["content"]  # historical row untouched
    assert v1_after["content_hash"] == v1["content_hash"]
    lineage = [row for row in payload["lineage"] if row["relation"] == "rewrite"]
    assert lineage and lineage[0]["from_version_id"] == v1["id"] and lineage[0]["to_version_id"] == v2_id
    db.close()


def test_rewrite_rejects_identical_content_and_empty(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    current = service.show(policy_id)["current_version"]
    with pytest.raises(LifecycleError):
        service.rewrite(policy_id, current["content"])
    with pytest.raises(LifecycleError):
        service.rewrite(policy_id, "   ")
    db.close()


# ----------------------------------------------------------------------
# promote / demote
# ----------------------------------------------------------------------
def test_promote_scoped_to_project_preserves_history(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    # Force a known starting placement for a deterministic promote chain.
    _force_version(service, policy_id, placement_type="scoped_rule", scope_type="directory", scope_value="app")
    v2_id = service.promote(policy_id, scope_type="project", scope_value="p1")
    payload = service.show(policy_id)
    assert payload["current_version"]["placement_type"] == "project_rule"
    assert payload["current_version"]["scope_value"] == "p1"
    versions_by_id = {v["id"]: v for v in payload["versions"]}
    assert versions_by_id[v2_id]["created_via"] == "promote"
    assert any(row["relation"] == "promote" for row in payload["lineage"])
    db.close()


def test_promote_to_global_requires_confirm_global(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    _force_version(service, policy_id, placement_type="project_rule", scope_type="project", scope_value="p1")
    with pytest.raises(LifecycleError):
        service.promote(policy_id)
    version_id = service.promote(policy_id, confirm_global=True)
    assert service.repo.get_policy_version(version_id)["placement_type"] == "global_rule"
    db.close()


def test_promote_invalid_transition_from_global_raises(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    _force_version(service, policy_id, placement_type="global_rule", scope_type=None, scope_value=None)
    with pytest.raises(LifecycleError):
        service.promote(policy_id)
    db.close()


def test_demote_project_to_scoped(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    _force_version(service, policy_id, placement_type="project_rule", scope_type="project", scope_value="p1")
    version_id = service.demote(policy_id, target_placement="scoped_rule", scope_type="directory", scope_value="app")
    current = service.repo.get_policy_version(version_id)
    assert current["placement_type"] == "scoped_rule"
    assert current["scope_value"] == "app"
    db.close()


def test_demote_from_skill_has_no_target(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    _force_version(service, policy_id, placement_type="skill")
    with pytest.raises(LifecycleError):
        service.demote(policy_id)
    db.close()


# ----------------------------------------------------------------------
# merge
# ----------------------------------------------------------------------
def test_merge_preserves_both_parents_and_lineage(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_a = service.adopt(review_id)
    policy_b_id = "POL-SECOND"
    service.repo.create_policy(policy_b_id, origin_review_id=None, origin_experience_id=None)
    service.repo.create_policy_version({
        "id": "PVN-POL-SECOND-1", "policy_id": policy_b_id, "version_number": 1, "content": "verify migrations after model edits",
        "content_hash": "hb", "placement_type": "project_rule", "target_agent": "claude",
        "scope_type": "project", "scope_value": "p1", "created_via": "initial",
    })
    service.repo.set_policy_current_version(policy_b_id, "PVN-POL-SECOND-1")
    _force_version(service, policy_a, placement_type="project_rule", scope_type="project", scope_value="p1")

    merged_id = service.merge(policy_a, policy_b_id, content="unified migration policy")
    merged = service.show(merged_id)
    assert merged["current_version"]["content"] == "unified migration policy"
    parents = {row["from_version_id"] for row in merged["lineage"] if row["relation"] == "merge_parent"}
    a_version = service.show(policy_a)["current_version"]["id"]
    assert parents == {a_version, "PVN-POL-SECOND-1"}
    # Both parents remain independently queryable and untouched.
    assert service.show(policy_a)["policy"]["status"] == "active"
    assert service.show(policy_b_id)["policy"]["status"] == "active"
    db.close()


def test_merge_self_rejected(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    with pytest.raises(LifecycleError):
        service.merge(policy_id, policy_id)
    db.close()


def test_merge_is_idempotent(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_a = service.adopt(review_id)
    policy_b_id = "POL-SECOND"
    service.repo.create_policy(policy_b_id, origin_review_id=None, origin_experience_id=None)
    service.repo.create_policy_version({
        "id": "PVN-POL-SECOND-1", "policy_id": policy_b_id, "version_number": 1, "content": "verify migrations",
        "content_hash": "hb", "placement_type": "scoped_rule", "created_via": "initial",
    })
    service.repo.set_policy_current_version(policy_b_id, "PVN-POL-SECOND-1")
    first = service.merge(policy_a, policy_b_id, content="unified", allow_cross_scope=True)
    second = service.merge(policy_a, policy_b_id, content="unified", allow_cross_scope=True)
    assert first == second
    assert len(service.repo.list_policy_versions(first)) == 1
    db.close()


def test_merge_incompatible_scope_rejected_unless_forced(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_a = service.adopt(review_id)
    policy_b_id = "POL-SECOND"
    service.repo.create_policy(policy_b_id, origin_review_id=None, origin_experience_id=None)
    service.repo.create_policy_version({
        "id": "PVN-POL-SECOND-1", "policy_id": policy_b_id, "version_number": 1, "content": "other policy",
        "content_hash": "hb", "placement_type": "scoped_rule", "scope_type": "directory", "scope_value": "other-dir",
        "created_via": "initial",
    })
    service.repo.set_policy_current_version(policy_b_id, "PVN-POL-SECOND-1")
    with pytest.raises(LifecycleError):
        service.merge(policy_a, policy_b_id)
    merged_id = service.merge(policy_a, policy_b_id, allow_cross_scope=True)
    assert merged_id
    db.close()


# ----------------------------------------------------------------------
# split
# ----------------------------------------------------------------------
def test_split_preserves_source_and_children_start_disabled(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    children = service.split(policy_id, [
        {"content": "child one content", "placement_type": "scoped_rule", "scope_type": "directory", "scope_value": "app/billing"},
        {"content": "child two content", "placement_type": "scoped_rule", "scope_type": "directory", "scope_value": "app/migrations"},
    ])
    assert len(children) == 2
    source_payload = service.show(policy_id)
    assert source_payload["policy"]["status"] == "active"  # source untouched
    for child_id in children:
        child_payload = service.show(child_id)
        assert child_payload["policy"]["status"] == "disabled"  # never silently activated
        assert any(row["relation"] == "split_child" for row in child_payload["lineage"])
    db.close()


def test_split_requires_at_least_one_child(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    with pytest.raises(LifecycleError):
        service.split(policy_id, [])
    db.close()


def test_split_duplicate_children_do_not_corrupt_lineage(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    child_spec = [{"content": "same child content", "placement_type": "scoped_rule", "scope_type": "directory", "scope_value": "app"}]
    first = service.split(policy_id, child_spec)
    second = service.split(policy_id, child_spec)
    assert first == second
    lineage = service.show(policy_id)["lineage"]
    split_edges = [row for row in lineage if row["relation"] == "split_child" and row["to_version_id"] == f"PVN-{first[0]}-1"]
    assert len(split_edges) == 1
    db.close()


# ----------------------------------------------------------------------
# disable / enable / prune
# ----------------------------------------------------------------------
def test_disable_enable_round_trip_preserves_evidence(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    before = service.show(policy_id)
    service.disable(policy_id, reason="too noisy")
    assert service.show(policy_id)["policy"]["status"] == "disabled"
    service.enable(policy_id, reason="revisited")
    after = service.show(policy_id)
    assert after["policy"]["status"] == "active"
    assert after["versions"] == before["versions"]
    db.close()


def test_disable_invalid_transition(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    service.disable(policy_id)
    with pytest.raises(LifecycleError):
        service.disable(policy_id)
    db.close()


def test_prune_preserves_history_but_disallows_reactivation(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    service.prune(policy_id, reason="superseded")
    payload = service.show(policy_id)
    assert payload["policy"]["status"] == "pruned"
    assert len(payload["versions"]) == 1  # history remains
    with pytest.raises(LifecycleError):
        service.enable(policy_id)  # adversarial: no accidental pruned reactivation
    with pytest.raises(LifecycleError):
        service.prune(policy_id)  # already pruned
    db.close()


# ----------------------------------------------------------------------
# rollback
# ----------------------------------------------------------------------
def test_rollback_restores_exact_content_and_records_event(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    v1 = service.show(policy_id)["current_version"]
    service.rewrite(policy_id, "a different, worse policy text")
    service.rewrite(policy_id, "an even worse third version")

    v4_id = service.rollback(policy_id, v1["version_number"])
    payload = service.show(policy_id)
    assert payload["current_version"]["id"] == v4_id
    assert payload["current_version"]["content"] == v1["content"]
    assert payload["current_version"]["placement_type"] == v1["placement_type"]
    assert payload["current_version"]["created_via"] == "rollback"
    versions_by_id = {v["id"]: v for v in payload["versions"]}
    v3_id = [v["id"] for v in payload["versions"] if v["version_number"] == 3][0]
    assert versions_by_id[v3_id]["status"] == "rolled_back"
    assert any(row["relation"] == "rollback_source" and row["from_version_id"] == v1["id"] and row["to_version_id"] == v4_id for row in payload["lineage"])
    db.close()


def test_rollback_to_nonexistent_version_rejected(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    with pytest.raises(LifecycleError):
        service.rollback(policy_id, 999)
    db.close()


def test_rollback_to_current_version_rejected(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    with pytest.raises(LifecycleError):
        service.rollback(policy_id, 1)
    db.close()


# ----------------------------------------------------------------------
# preview / apply - real filesystem writes through Stage 7 safety
# ----------------------------------------------------------------------
def test_preview_apply_writes_and_managed_key_stable_across_rewrites(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    assert not (root / "AGENTS.md").exists()

    preview1 = service.preview(policy_id, project_root=str(root))
    result1 = service.apply(policy_id, project_root=str(root))
    assert result1["outcome"] == "applied"
    text_after_v1 = (root / "AGENTS.md").read_text("utf-8")
    assert "trajweave:managed" in text_after_v1
    assert preview1["proposed_content"].strip() in text_after_v1

    service.rewrite(policy_id, "a clarified rewritten policy body")
    service.preview(policy_id, project_root=str(root))
    result2 = service.apply(policy_id, project_root=str(root))
    assert result2["outcome"] == "applied"
    text_after_v2 = (root / "AGENTS.md").read_text("utf-8")
    assert "a clarified rewritten policy body" in text_after_v2
    # Same managed block key (keyed by policy_id) - one block, not two.
    assert text_after_v2.count("trajweave:managed") == 1
    db.close()


def test_apply_without_preview_rejected(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    with pytest.raises(LifecycleError):
        service.apply(policy_id, project_root=str(root))
    db.close()


def test_apply_dry_run_never_writes(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    result = service.apply(policy_id, project_root=str(root), dry_run=True)
    assert result["outcome"] == "dry_run"
    assert not (root / "AGENTS.md").exists()
    db.close()


def test_apply_global_requires_confirm_global(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    _force_version(service, policy_id, placement_type="global_rule", scope_type=None, scope_value=None)
    service.preview(policy_id)
    with pytest.raises(LifecycleError):
        service.apply(policy_id)
    result = service.apply(policy_id, confirm_global=True)
    assert result["outcome"] == "applied"
    global_dir = get_paths(str(home)).home / "policies" / "codex"
    assert (global_dir / "AGENTS.md").exists()
    db.close()


def test_apply_stale_preview_rejected(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    service.preview(policy_id, project_root=str(root))
    service.rewrite(policy_id, "changed after preview was taken")
    with pytest.raises(LifecycleError):
        service.apply(policy_id, project_root=str(root))
    db.close()


# ----------------------------------------------------------------------
# recommendations
# ----------------------------------------------------------------------
def test_recommend_persists_and_reject_defer(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    rec = service.recommend(policy_id)
    assert rec["status"] == "open"
    assert rec["operation"] == "retain"
    service.reject_recommendation(rec["id"], reason="not now")
    assert service.repo.get_recommendation(rec["id"])["status"] == "rejected"
    with pytest.raises(LifecycleError):
        service.reject_recommendation(rec["id"])
    db.close()


def test_accept_recommendation_dispatches_disable(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    review_row = service.repo.get_policy_review(review_id)
    version = service.show(policy_id)["current_version"]
    spec_id = "EV-LOW"
    service.repo.create_evaluation_spec({
        "id": spec_id, "review_id": review_row["id"], "experience_id": "E-x", "proposal_id": "PP-x",
        "content_revision": 0, "policy_content": version["content"], "policy_content_hash": "hh",
        "placement_type": version["placement_type"], "target_agent": "claude", "repo_root": str(root),
        "repo_commit": "abc", "task_spec": {}, "environment": {}, "verifiers": [], "condition_order_mode": "baseline_first",
    })
    for i in range(6):
        service.repo.record_evaluation_comparison({
            "id": f"EC-LOW-{i}", "evaluation_id": spec_id, "repetition_index": i + 1,
            "outcome": "regressed" if i < 5 else "improved",
        })
    rec = service.recommend(policy_id)
    assert rec["operation"] in {"demote", "disable"}
    service.accept_recommendation(rec["id"])
    assert service.repo.get_recommendation(rec["id"])["status"] == "accepted"
    updated_policy = service.show(policy_id)
    if rec["operation"] == "disable":
        assert updated_policy["policy"]["status"] == "disabled"
    else:
        assert updated_policy["current_version"]["created_via"] == "demote"
    db.close()


# ----------------------------------------------------------------------
# zero-policy state
# ----------------------------------------------------------------------
def test_zero_policy_clean_state(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    code, out = _run(capsys, "lifecycle", "list", "--json")
    assert code == 0
    assert json.loads(out) == []
    code, out = _run(capsys, "lifecycle", "list")
    assert code == 0
    assert "No lifecycle policies yet" in out
    code, out = _run(capsys, "lifecycle", "duplicates", "--json")
    assert code == 0
    assert json.loads(out) == []


# ----------------------------------------------------------------------
# provenance traversal
# ----------------------------------------------------------------------
def test_full_provenance_traversal(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    review_row = service.repo.get_policy_review(review_id)
    version = service.show(policy_id)["current_version"]
    spec_id = "EV-PROV"
    service.repo.create_evaluation_spec({
        "id": spec_id, "review_id": review_row["id"], "experience_id": "E-x", "proposal_id": "PP-x",
        "content_revision": 0, "policy_content": version["content"], "policy_content_hash": "hh",
        "placement_type": version["placement_type"], "target_agent": "claude", "repo_root": str(root),
        "repo_commit": "abc", "task_spec": {}, "environment": {}, "verifiers": [], "condition_order_mode": "baseline_first",
    })
    service.repo.record_evaluation_comparison({"id": "EC-PROV-1", "evaluation_id": spec_id, "repetition_index": 1, "outcome": "improved"})
    rec = service.recommend(policy_id)
    provenance = service.full_provenance(version["id"])
    assert provenance["evaluation_comparisons"]
    assert provenance["stage7_reviews"]
    assert provenance["stage6_placements"]
    assert provenance["stage5_experiences"]
    assert provenance["stage5_experiences"][0]["evidence_occurrences"]
    assert any(r["id"] == rec["id"] for r in provenance["recommendations"])
    db.close()


# ----------------------------------------------------------------------
# append-only enforcement
# ----------------------------------------------------------------------
def test_service_exposes_no_mutation_of_historical_versions(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    v1 = service.show(policy_id)["current_version"]
    service.rewrite(policy_id, "second version text")
    # The only repository call capable of changing a version's row is
    # set_policy_version_status (a status flag), never its content/hash -
    # there is no public API to alter historical content.
    assert not hasattr(service, "update_version_content")
    assert not hasattr(service.repo, "update_policy_version_content")
    unchanged = service.repo.get_policy_version(v1["id"])
    assert unchanged["content"] == v1["content"]
    assert unchanged["content_hash"] == v1["content_hash"]
    db.close()


# ----------------------------------------------------------------------
# CLI smoke test
# ----------------------------------------------------------------------
def test_cli_lifecycle_smoke(home_and_repo, capsys):
    home, root = home_and_repo
    code, out = _run(capsys, "review", "list", "--json")
    proposal_id = json.loads(out)[0]["recommended_proposal_id"]
    assert _run(capsys, "review", "accept", proposal_id, "--agent", "codex")[0] == 0
    review_id = f"RV-{json.loads(_run(capsys, 'review', 'list', '--json')[1])[0]['proposal_set_id']}"

    code, out = _run(capsys, "lifecycle", "adopt", review_id, "--json")
    assert code == 0
    policy_id = json.loads(out)["policy_id"]

    code, out = _run(capsys, "lifecycle", "list", "--json")
    assert code == 0 and json.loads(out)[0]["id"] == policy_id

    code, out = _run(capsys, "lifecycle", "show", policy_id, "--json")
    assert code == 0 and json.loads(out)["policy"]["id"] == policy_id

    code, out = _run(capsys, "lifecycle", "rewrite", policy_id, "--content", "cli rewritten body", "--json")
    assert code == 0

    code, out = _run(capsys, "lifecycle", "recommend", policy_id, "--json")
    assert code == 0
    rec = json.loads(out)
    assert rec["operation"] in {"retain", "promote", "demote", "rollback", "disable", "prune"}

    code, out = _run(capsys, "lifecycle", "preview", policy_id, "--project", str(root), "--json")
    assert code == 0
    code, out = _run(capsys, "lifecycle", "apply", policy_id, "--project", str(root), "--json")
    assert code == 0
    assert (root / "AGENTS.md").exists()

    code, out = _run(capsys, "lifecycle", "disable", policy_id, "--json")
    assert code == 0
    code, out = _run(capsys, "lifecycle", "enable", policy_id, "--json")
    assert code == 0
    code, out = _run(capsys, "lifecycle", "prune", policy_id, "--json")
    assert code == 0


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
def test_ui_lifecycle_zero_state(tmp_path):
    db_path = tmp_path / "home" / "trajweave.db"
    with _server(db_path) as base:
        listed = _get(base, "/api/lifecycle")
        assert listed == {"policies": [], "schema_ok": False}


def test_ui_lifecycle_read_and_actions_keep_apply_explicit(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    db.close()

    db_path = home / "trajweave.db"
    with _server(db_path) as base:
        listed = _get(base, "/api/lifecycle")
        assert listed["schema_ok"] is True
        assert listed["policies"][0]["id"] == policy_id

        shown = _get(base, f"/api/lifecycle/{policy_id}")
        assert shown["policy"]["id"] == policy_id
        assert not (root / "AGENTS.md").exists()  # GET never mutates or applies

        status, body = _post(base, f"/api/lifecycle/{policy_id}/preview", {"project_root": str(root)})
        assert status == 200
        assert not (root / "AGENTS.md").exists()  # preview alone never writes

        status, body = _post(base, f"/api/lifecycle/{policy_id}/apply", {"project_root": str(root), "dry_run": True})
        assert status == 200 and body["outcome"] == "dry_run"
        assert not (root / "AGENTS.md").exists()

        status, body = _post(base, f"/api/lifecycle/{policy_id}/apply", {"project_root": str(root)})
        assert status == 200 and body["outcome"] == "applied"
        assert (root / "AGENTS.md").exists()

        status, body = _post(base, f"/api/lifecycle/{policy_id}/disable", {"reason": "via UI"})
        assert status == 200 and body["status"] == "disabled"
        status, body = _post(base, f"/api/lifecycle/{policy_id}/enable", {})
        assert status == 200 and body["status"] == "active"


# ----------------------------------------------------------------------
# adversarial cases not already covered above
# ----------------------------------------------------------------------
def test_preview_rejects_path_traversal_target_override(home_and_repo):
    from trajweave.review.targets import SafetyError

    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    with pytest.raises(SafetyError):
        service.preview(policy_id, project_root=str(root), target_override="../../etc/escape.md")
    assert not (root.parent.parent / "etc" / "escape.md").exists()
    db.close()


def test_apply_rejects_deleted_repository(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    service.preview(policy_id, project_root=str(root))
    import shutil
    shutil.rmtree(root)
    with pytest.raises(Exception):
        service.apply(policy_id, project_root=str(root))
    db.close()


def test_stale_target_detected_for_missing_project(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    _force_version(service, policy_id, placement_type="project_rule", scope_type="project", scope_value="no-such-project")
    signals = service.stale_signals(policy_id)
    assert "target_project_not_registered" in signals
    db.close()


def test_database_reopen_preserves_lifecycle_state(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    service.rewrite(policy_id, "content that must survive a reopen")
    db.close()

    db2, service2 = _service(home)
    payload = service2.show(policy_id)
    assert payload["current_version"]["content"] == "content that must survive a reopen"
    assert len(payload["versions"]) == 2
    db2.close()


def test_split_child_cannot_be_previewed_while_disabled(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    children = service.split(policy_id, [{"content": "child body", "placement_type": "scoped_rule", "scope_type": "directory", "scope_value": "app"}])
    with pytest.raises(LifecycleError):
        service.preview(children[0], project_root=str(root))
    db.close()


def test_db_level_triggers_block_direct_mutation_of_history(home_and_repo):
    """Adversarial: even a raw SQL statement against the database file cannot
    rewrite historical version content or delete append-only ledger rows -
    this is enforced by triggers, not merely by application-code discipline."""

    import sqlite3

    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    version = service.show(policy_id)["current_version"]

    with pytest.raises(sqlite3.IntegrityError):
        service.repo.db.execute("UPDATE policy_versions SET content = 'tampered' WHERE id = ?", (version["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        service.repo.db.execute("DELETE FROM policy_versions WHERE id = ?", (version["id"],))

    service.rewrite(policy_id, "a second version to generate a lifecycle action")
    action_id = service.repo.list_lifecycle_actions(policy_id)[0]["id"]
    with pytest.raises(sqlite3.IntegrityError):
        service.repo.db.execute("UPDATE policy_lifecycle_actions SET action = 'tampered' WHERE id = ?", (action_id,))
    with pytest.raises(sqlite3.IntegrityError):
        service.repo.db.execute("DELETE FROM policy_lifecycle_actions WHERE id = ?", (action_id,))
    db.close()


def test_ui_lifecycle_unknown_action_rejected(home_and_repo):
    home, root = home_and_repo
    review_id = _accepted_review_id(home)
    db, service = _service(home)
    policy_id = service.adopt(review_id)
    db.close()
    with _server(home / "trajweave.db") as base:
        status, body = _post(base, f"/api/lifecycle/{policy_id}/delete-everything", {})
        assert status == 404
