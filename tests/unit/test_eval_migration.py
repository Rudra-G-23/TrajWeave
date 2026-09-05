"""Stage 8 migration must apply cleanly on top of a real Stage 7 database
without losing any Stage 7 data, and a brand-new database must still reach
the current head version."""

from __future__ import annotations

import trajweave.storage.database as database_mod
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def test_migration_0006_preserves_existing_stage7_data(tmp_path, monkeypatch):
    path = tmp_path / "tw.db"

    # Build a database as Stage 7 would have left it (migrations 1-5 only).
    stage7_migrations = [m for m in database_mod._MIGRATIONS if m[0] <= 5]
    monkeypatch.setattr(database_mod, "_MIGRATIONS", stage7_migrations)
    monkeypatch.setattr(database_mod, "SCHEMA_VERSION", 5)
    db = Database(path)
    assert db.schema_version == 5
    repo = Repository(db)
    repo.upsert_project(project_id="p1", name="repo", root=str(tmp_path / "repo"), git_remote=None)
    db.execute(
        "INSERT INTO policy_reviews(id, proposal_set_id, experience_id, selected_proposal_id, "
        "status, source_fingerprint, proposal_snapshot_json, created_at, updated_at) "
        "VALUES ('RV-PS-E-0001', 'PS-E-0001', 'E-0001', 'PP-E-0001-project_rule', "
        "'accepted', 'fp1', '{}', datetime('now'), datetime('now'))"
    )
    db.close()

    # Reopen with the real (unpatched) migration list - Stage 8's migration
    # must apply on top of this pre-existing Stage 7 schema.
    monkeypatch.undo()
    db2 = Database(path)
    assert db2.schema_version == database_mod.SCHEMA_VERSION
    repo2 = Repository(db2)
    project = repo2.get_project("p1")
    assert project is not None and project["name"] == "repo"
    review = repo2.get_policy_review("RV-PS-E-0001")
    assert review is not None and review["status"] == "accepted"
    names = {r["name"] for r in db2.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"evaluation_specs", "evaluation_runs", "evaluation_verifier_results", "evaluation_comparisons"} <= names
    db2.close()


def test_fresh_database_reaches_head_version(tmp_path):
    db = Database(tmp_path / "fresh.db")
    assert db.schema_version == database_mod.SCHEMA_VERSION
    db.close()
