"""Stage 9 migration must apply cleanly on top of a real Stage 8 database
without losing any Stage 0-8 data, and a brand-new database must still reach
the current head version."""

from __future__ import annotations

import trajweave.storage.database as database_mod
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def test_migration_0007_preserves_existing_stage8_data(tmp_path, monkeypatch):
    path = tmp_path / "tw.db"

    # Build a database as Stage 8 would have left it (migrations 1-6 only).
    stage8_migrations = [m for m in database_mod._MIGRATIONS if m[0] <= 6]
    monkeypatch.setattr(database_mod, "_MIGRATIONS", stage8_migrations)
    monkeypatch.setattr(database_mod, "SCHEMA_VERSION", 6)
    db = Database(path)
    assert db.schema_version == 6
    repo = Repository(db)
    repo.upsert_project(project_id="p1", name="repo", root=str(tmp_path / "repo"), git_remote=None)
    db.execute(
        "INSERT INTO policy_reviews(id, proposal_set_id, experience_id, selected_proposal_id, "
        "status, source_fingerprint, proposal_snapshot_json, created_at, updated_at) "
        "VALUES ('RV-PS-E-0001', 'PS-E-0001', 'E-0001', 'PP-E-0001-project_rule', "
        "'accepted', 'fp1', '{}', datetime('now'), datetime('now'))"
    )
    db.execute(
        "INSERT INTO evaluation_specs(id, review_id, experience_id, proposal_id, content_revision, "
        "policy_content, policy_content_hash, placement_type, target_agent, repo_root, repo_commit, "
        "task_spec_json, environment_json, verifier_json, created_at) VALUES "
        "('EV-1', 'RV-PS-E-0001', 'E-0001', 'PP-E-0001-project_rule', 0, 'do x', 'hash1', "
        "'project_rule', 'claude', '/tmp/repo', 'deadbeef', '{}', '{}', '[]', datetime('now'))"
    )
    db.execute(
        "INSERT INTO evaluation_comparisons(id, evaluation_id, repetition_index, outcome, created_at) "
        "VALUES ('EC-1', 'EV-1', 1, 'improved', datetime('now'))"
    )
    db.close()

    # Reopen with the real (unpatched) migration list - Stage 9's migration
    # must apply on top of this pre-existing Stage 8 schema.
    monkeypatch.undo()
    db2 = Database(path)
    assert db2.schema_version == database_mod.SCHEMA_VERSION
    repo2 = Repository(db2)
    project = repo2.get_project("p1")
    assert project is not None and project["name"] == "repo"
    review = repo2.get_policy_review("RV-PS-E-0001")
    assert review is not None and review["status"] == "accepted"
    spec = repo2.get_evaluation_spec("EV-1")
    assert spec is not None and spec["policy_content"] == "do x"
    comparisons = repo2.list_evaluation_comparisons("EV-1")
    assert len(comparisons) == 1 and comparisons[0]["outcome"] == "improved"
    names = {r["name"] for r in db2.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "policies", "policy_versions", "policy_lineage", "policy_lifecycle_actions",
        "policy_recommendations", "policy_version_evidence",
        "policy_lifecycle_previews", "policy_lifecycle_applications",
    } <= names

    db2.close()


def test_fresh_database_reaches_head_version(tmp_path):
    db = Database(tmp_path / "fresh.db")
    assert db.schema_version == database_mod.SCHEMA_VERSION
    db.close()
