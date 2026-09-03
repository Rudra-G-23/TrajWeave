"""End-to-end Stage 5: seed trajectories -> extract -> assert candidates.

Follows the scenario in the Stage 5 brief section 40 (migration-after-model-change)
plus incremental / idempotency behaviour.
"""

from __future__ import annotations

import pytest

from trajweave.experience import ExperienceConfig, ExperienceExtractor
from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import NormalizedTrajectory, SourceSessionRef
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository

pytestmark = pytest.mark.integration


def _seed(repo: Repository, sid: str, project_id: str, spec, *,
          status=FinalStatus.SUCCESS, source_hash="h"):
    pk = repo.record_source_session(
        agent="claude", source_session_id=sid, source_path=f"/s/{sid}.jsonl",
        source_hash=source_hash, source_mtime=1.0, size_bytes=1, project_id=project_id,
        status="imported", detail=None, cwd="/r", imported=True,
    )
    t = NormalizedTrajectory(
        agent=Agent.CLAUDE,
        source=SourceSessionRef(Agent.CLAUDE, sid, f"/s/{sid}.jsonl", source_hash, 1.0, 1),
        task="task", task_source=TaskSource.USER_PROMPT, final_status=status,
    )
    t.started_at = "2026-08-28T10:00:00+00:00"
    t.ended_at = "2026-08-28T10:20:00+00:00"
    for etype, path, summary in spec:
        kw = {}
        if path:
            kw["path"] = path
        if summary:
            kw["summary"] = summary
        t.add_event(NormalizedEvent(type=EventType(etype), **kw))
    tid, _ = repo.persist_trajectory(t, source_pk=pk, project_id=project_id)
    return tid


def _e(etype, path=None, summary=None):
    return (etype, path, summary)


MODEL_FAIL_MIGRATE_PASS = [
    _e("user_prompt", None, "add a column to the user model"),
    _e("file_edit", "app/models/user.py"),
    _e("test_fail", None, "E   assert user.created_at is not None"),
    _e("file_create", "migrations/0007_user_created_at.py"),
    _e("test_pass"),
]
MODEL_FAIL_MIGRATE_EDIT_PASS = [
    _e("user_prompt", None, "rename order field"),
    _e("file_edit", "app/models/order.py"),
    _e("test_fail", None, "E   assert order.total == expected"),
    _e("file_edit", "migrations/0008_order_total.py"),
    _e("test_pass"),
]
MODEL_MIGRATE_PASS = [  # proactive
    _e("user_prompt", None, "new product model"),
    _e("file_edit", "app/models/product.py"),
    _e("file_create", "migrations/0009_product.py"),
    _e("test_pass"),
]
MODEL_PASS_NO_MIGRATION = [  # contradiction
    _e("user_prompt", None, "tweak cart model docstring"),
    _e("file_edit", "app/models/cart.py"),
    _e("test_pass"),
]


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "tw.db")
    repo = Repository(d)
    repo.upsert_project(project_id="p1", name="alpha", root="/r/alpha", git_remote=None)
    repo.upsert_project(project_id="p2", name="beta", root="/r/beta", git_remote=None)
    return d


def test_migration_candidate_with_contradiction(db):
    repo = Repository(db)
    _seed(repo, "s1", "p1", MODEL_FAIL_MIGRATE_PASS)
    _seed(repo, "s2", "p1", MODEL_FAIL_MIGRATE_EDIT_PASS)
    _seed(repo, "s3", "p2", MODEL_MIGRATE_PASS)
    _seed(repo, "s4", "p2", MODEL_PASS_NO_MIGRATION)

    res = ExperienceExtractor(repo).run()
    assert res.trajectories_analyzed == 4

    exps = [dict(e) for e in repo.list_experiences()]
    mig = [e for e in exps if e["group_key"] == "fr::migration::test"]
    assert len(mig) == 1
    e = mig[0]
    assert e["status"] == "candidate"
    assert e["support_count"] == 3
    assert e["contradiction_count"] == 1
    assert e["project_count"] == 2
    assert 0 < e["confidence"] <= 1

    ev = [dict(x) for x in repo.get_experience_evidence(e["id"])]
    rels = sorted(x["relationship"] for x in ev)
    assert rels == ["contradiction", "support", "support", "support"]
    contra = [x for x in ev if x["relationship"] == "contradiction"][0]
    assert contra["trajectory_id"] == "TW-000004"


def test_contradiction_lowers_confidence(db):
    repo = Repository(db)
    _seed(repo, "s1", "p1", MODEL_FAIL_MIGRATE_PASS)
    _seed(repo, "s2", "p1", MODEL_FAIL_MIGRATE_EDIT_PASS)
    _seed(repo, "s3", "p2", MODEL_MIGRATE_PASS)
    before = dict(ExperienceExtractor(repo).run().top_candidates[0])["confidence"]

    _seed(repo, "s4", "p2", MODEL_PASS_NO_MIGRATION)
    after_exps = [dict(e) for e in Repository(db).list_experiences() if e["group_key"] == "fr::migration::test"]
    ExperienceExtractor(repo).run()
    after = [dict(e) for e in repo.list_experiences() if e["group_key"] == "fr::migration::test"][0]
    assert after["contradiction_count"] == 1
    assert after["confidence"] < before


def test_idempotent_and_incremental(db):
    repo = Repository(db)
    _seed(repo, "s1", "p1", MODEL_FAIL_MIGRATE_PASS)
    _seed(repo, "s2", "p1", MODEL_FAIL_MIGRATE_EDIT_PASS)
    _seed(repo, "s3", "p2", MODEL_MIGRATE_PASS)

    r1 = ExperienceExtractor(repo).run()
    c1 = repo.experience_counts()
    assert r1.trajectories_analyzed == 3

    r2 = ExperienceExtractor(repo).run()
    assert r2.trajectories_analyzed == 0
    assert repo.experience_counts() == c1

    # a new trajectory -> only it is analyzed
    _seed(repo, "s4", "p2", MODEL_PASS_NO_MIGRATION)
    r3 = ExperienceExtractor(repo).run()
    assert r3.trajectories_analyzed == 1

    r4 = ExperienceExtractor(repo).run(rebuild=True)
    assert r4.trajectories_analyzed == 4


def test_bare_affirmation_does_not_create_a_human_correction_experience(db):
    repo = Repository(db)
    spec = [
        _e("user_prompt", None, "build the feature"),
        _e("assistant_message"),
        _e("human_correction", None, "yes"),
        _e("file_edit", "app/api/thing.py"),
        _e("test_pass"),
    ]
    for i in range(4):
        _seed(repo, f"s{i}", "p1", spec, source_hash=f"h{i}")
    ExperienceExtractor(repo).run()
    exps = [dict(e) for e in repo.list_experiences()]
    assert not any(e["group_key"].startswith("hc::") for e in exps)


def test_project_filter(db):
    repo = Repository(db)
    _seed(repo, "s1", "p1", MODEL_FAIL_MIGRATE_PASS)
    _seed(repo, "s2", "p2", MODEL_FAIL_MIGRATE_EDIT_PASS)
    res = ExperienceExtractor(repo).run(project_id="p1")
    assert res.trajectories_considered == 1
    assert res.trajectories_analyzed == 1
