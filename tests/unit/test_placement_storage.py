from __future__ import annotations

import json

from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def _repo_with_experience(tmp_path) -> Repository:
    repo = Repository(Database(tmp_path / "placement.db"))
    db = repo.db
    with db.transaction():
        db.execute(
            "INSERT INTO projects(id, name, root, created_at, last_seen_at) "
            "VALUES ('p1', 'example', '/private/example', 'now', 'now')"
        )
        db.execute(
            "INSERT INTO source_sessions("
            "agent, source_session_id, source_path, project_id, status, first_seen_at) "
            "VALUES ('codex', 's1', '/sessions/s1.jsonl', 'p1', 'imported', 'now')"
        )
        db.execute(
            "INSERT INTO trajectories("
            "id, seq, source_session_pk, project_id, agent, task_source, final_status, created_at) "
            "VALUES ('TW-000001', 1, 1, 'p1', 'codex', 'user_prompt', 'success', 'now')"
        )
        db.execute(
            "INSERT INTO trajectory_events(trajectory_id, sequence, type, command, redacted) "
            "VALUES ('TW-000001', 2, 'command', 'pytest tests/unit', 0)"
        )
        db.execute(
            "INSERT INTO trajectory_events(trajectory_id, sequence, type, path, redacted) "
            "VALUES ('TW-000001', 1, 'file_edit', 'src/example.py', 0)"
        )
        db.execute(
            "INSERT INTO experience_occurrences("
            "id, trajectory_id, project_id, pattern_type, group_key, start_sequence, "
            "end_sequence, classification, features_json, dedupe_hash, created_at) "
            "VALUES ('O-000001', 'TW-000001', 'p1', 'failure_repair_success', "
            "'fr::test', 1, 3, 'support', '{\"repair\": \"test\"}', 'dedupe', 'now')"
        )
        db.execute(
            "INSERT INTO experiences("
            "id, group_key, title, pattern_type, status, confidence, support_count, "
            "contradiction_count, ambiguous_count, occurrence_count, project_count, "
            "summary_source, review_status, created_at, updated_at) "
            "VALUES ('E-0001', 'fr::test', 'Run tests', 'failure_repair_success', "
            "'candidate', .8, 2, 0, 0, 2, 1, 'deterministic', 'unreviewed', 'now', 'now')"
        )
        db.execute(
            "INSERT INTO experience_evidence(experience_id, occurrence_id, relationship) "
            "VALUES ('E-0001', 'O-000001', 'support')"
        )
    return repo


def _alternatives(*, first: str = "global_rule") -> list[dict]:
    placement_types = [first] + [
        item for item in ("ignore", "global_rule", "project_rule", "scoped_rule", "skill")
        if item != first
    ]
    return [
        {
            "placement_type": placement_type,
            "scope_type": "global" if placement_type == "global_rule" else "project",
            "scope_value": None if placement_type == "global_rule" else "p1",
            "proposed_content": "Run the focused tests before changing implementation.",
            "score": 1 - (rank * .1),
            "rank": rank,
            "feature_values": {"support_count": 2, "rank_input": rank},
            "diagnostics": [{"sign": "+", "feature": "support_count", "value": 2}],
            "diagnostics_text": "+ 2 supporting occurrences",
            "evidence": [{"occurrence_id": "O-000001", "role": "supporting"}],
        }
        for rank, placement_type in enumerate(placement_types, start=1)
    ]


def test_placement_set_replaces_in_place_and_traces_evidence(tmp_path):
    repo = _repo_with_experience(tmp_path)
    run_id = repo.record_placement_run({
        "started_at": "start", "finished_at": "finish", "generator_version": "stage6-v1",
        "eligible_experiences": 1, "proposal_sets_generated": 1, "runtime_seconds": .01,
    })
    set_id = repo.replace_placement_proposal_set(
        experience_id="E-0001", source_fingerprint="fingerprint-a",
        generator_version="stage6-v1", proposals=_alternatives(), run_id=run_id,
    )
    assert set_id == "PS-E-0001"
    assert repo.placement_counts() == {"proposal_sets": 1, "proposals": 5}
    proposal = repo.get_placement_proposals("E-0001")[0]
    assert proposal["placement_type"] == "global_rule"
    assert json.loads(proposal["feature_values_json"])["support_count"] == 2
    evidence = repo.get_placement_proposal_evidence(proposal["id"])
    assert [(row["occurrence_id"], row["role"]) for row in evidence] == [
        ("O-000001", "supporting")
    ]

    # A deterministic re-generation replaces alternatives, rather than appending.
    engine_shaped = _alternatives(first="project_rule")
    for item in engine_shaped:
        item["features"] = item.pop("feature_values")
        item["evidence_occurrence_ids"] = [
            ref["occurrence_id"] for ref in item.pop("evidence")
        ]
    assert repo.replace_placement_proposal_set(
        experience_id="E-0001", source_fingerprint="fingerprint-b",
        generator_version="stage6-v1", proposals=engine_shaped, run_id=run_id,
    ) == set_id
    assert repo.placement_counts() == {"proposal_sets": 1, "proposals": 5}
    assert repo.get_placement_proposals("E-0001")[0]["placement_type"] == "project_rule"
    assert repo.get_placement_proposal_set("E-0001")["source_fingerprint"] == "fingerprint-b"
    # --type searches alternatives, while --recommended is restricted to rank one.
    assert len(repo.list_placement_proposal_sets(placement_type="global_rule")) == 1
    assert repo.list_placement_proposal_sets(recommended="global_rule") == []
    assert repo.latest_placement_run()["id"] == run_id


def test_placement_eligibility_evidence_context_and_invalidation(tmp_path):
    repo = _repo_with_experience(tmp_path)
    assert [row["id"] for row in repo.list_placement_eligible_experiences()] == ["E-0001"]
    evidence = repo.get_placement_evidence("E-0001")
    assert evidence[0]["project_root"] == "/private/example"
    assert evidence[0]["file_paths"] == ["src/example.py"]
    assert evidence[0]["commands"] == ["pytest tests/unit"]

    repo.replace_placement_proposal_set(
        experience_id="E-0001", source_fingerprint="fingerprint-a",
        generator_version="stage6-v1", proposals=_alternatives(),
    )
    assert repo.set_experience_review(
        "E-0001", review_status="false_positive", note="not reusable"
    )
    assert repo.list_placement_eligible_experiences() == []
    assert repo.placement_counts() == {"proposal_sets": 0, "proposals": 0}
