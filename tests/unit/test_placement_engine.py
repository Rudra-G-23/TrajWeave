"""Controlled Stage 6 fixtures for the pure placement engine.

These records deliberately use the same dictionary contracts returned by the
Stage 5 repository read methods.  They are never inserted into a real database.
"""

from __future__ import annotations

from trajweave.placement import build_proposals
from trajweave.placement import engine
from trajweave.placement.scope import normalize_repository_path


def _experience(**changes):
    result = {
        "id": "E-0007",
        "pattern_type": "failure_repair_success",
        "confidence": 0.90,
        "confidence_json": '{"components": {"recency": 1.0}}',
        "reusable_lesson": "Update the related configuration before relying on the checks.",
    }
    result.update(changes)
    return result


def _evidence(projects, *, relationship="support", context="other"):
    """Make one sequence-bounded supporting episode for every project entry."""

    return [
        {
            "occurrence_id": f"O-{index:06d}",
            "relationship": relationship,
            "project_id": project,
            "trajectory_id": f"TW-{index:06d}",
            "start_sequence": 1,
            "end_sequence": 4,
            "repair_context": context,
        }
        for index, project in enumerate(projects, start=1)
    ]


def _recommended(proposals):
    return proposals[0]["placement_type"]


def test_global_fixture_requires_balanced_repeated_independent_projects():
    evidence = _evidence(["p1", "p1", "p2", "p2", "p3", "p3"])
    proposals = build_proposals(_experience(), evidence, {})

    assert _recommended(proposals) == "global_rule"
    assert proposals[0]["features"]["global_eligible"] is True
    assert proposals[0]["evidence_occurrence_ids"] == [f"O-{i:06d}" for i in range(1, 7)]


def test_project_fixture_beats_global_when_support_is_local():
    proposals = build_proposals(_experience(), _evidence(["p1"] * 4), {})

    assert _recommended(proposals) == "project_rule"
    global_proposal = next(item for item in proposals if item["placement_type"] == "global_rule")
    assert global_proposal["score"] <= 0.24
    assert any(row["code"] == "global_gate" and row["polarity"] == "-" for row in global_proposal["diagnostics"])


def test_scoped_fixture_uses_repeated_exact_directory_evidence():
    evidence = _evidence(["p1"] * 4, context="other")
    events = {
        item["trajectory_id"]: [{"sequence": 2, "type": "write", "path": f"src/migrations/{index}.py"}]
        for index, item in enumerate(evidence)
    }
    proposals = build_proposals(_experience(), evidence, events)

    assert _recommended(proposals) == "scoped_rule"
    assert proposals[0]["scope_type"] == "directory"
    assert proposals[0]["scope_value"] == "src/migrations"


def test_skill_fixture_requires_repeated_multi_operation_workflow():
    evidence = _evidence(["p1"] * 4)
    events = {
        item["trajectory_id"]: [
            {"sequence": 1, "type": "read", "path": "src/a.py"},
            {"sequence": 2, "type": "write", "path": "src/a.py"},
            {"sequence": 3, "type": "command", "command": "pytest"},
        ]
        for item in evidence
    }
    proposals = build_proposals(_experience(), evidence, events)

    assert _recommended(proposals) == "skill"
    assert proposals[0]["features"]["procedural_complexity"] == 1.0
    assert any(row["code"] == "procedure" and row["polarity"] == "+" for row in proposals[0]["diagnostics"])


def test_ignore_fixture_rises_for_contradictory_non_actionable_evidence():
    evidence = _evidence(["p1", "p1"], context="other")
    evidence += _evidence(["p1", "p2", "p3"], relationship="contradiction", context="other")
    # The ids need to remain distinct after concatenating controlled fixture rows.
    for index, item in enumerate(evidence, start=1):
        item["occurrence_id"] = f"O-{index:06d}"
        item["trajectory_id"] = f"TW-{index:06d}"
    proposals = build_proposals(
        _experience(pattern_type="repeated_failure", confidence=0.30), evidence, {}
    )

    assert _recommended(proposals) == "ignore"
    assert proposals[0]["score"] > next(item["score"] for item in proposals if item["placement_type"] == "project_rule")


def test_weak_unrelated_second_project_cannot_turn_project_rule_global():
    proposals = build_proposals(_experience(), _evidence(["p1", "p1", "p1", "p1", "p2"]), {})

    assert _recommended(proposals) == "project_rule"
    global_proposal = next(item for item in proposals if item["placement_type"] == "global_rule")
    assert global_proposal["features"]["global_eligible"] is False
    assert global_proposal["score"] <= 0.24


def test_ambiguous_scopes_keep_all_alternatives_and_deterministic_ranking():
    evidence = _evidence(["p1", "p1", "p2", "p2"])
    events = {
        item["trajectory_id"]: [{"sequence": 1, "path": f"lib/{index}.py", "type": "write"}]
        for index, item in enumerate(evidence)
    }
    first = build_proposals(_experience(), evidence, events)
    second = build_proposals(_experience(), list(reversed(evidence)), events)

    assert [item["placement_type"] for item in first] == [item["placement_type"] for item in second]
    assert [item["score"] for item in first] == [item["score"] for item in second]
    assert [item["rank"] for item in first] == [1, 2, 3, 4, 5]
    assert {item["placement_type"] for item in first} == {
        "ignore", "global_rule", "project_rule", "scoped_rule", "skill",
    }


def test_exact_score_ties_use_the_fixed_safety_first_type_order(monkeypatch):
    monkeypatch.setattr(engine, "_score", lambda _placement_type, _features: 0.5)

    proposals = engine.build_proposals(_experience(), _evidence(["p1", "p1"]), {})

    assert [item["placement_type"] for item in proposals] == [
        "ignore", "global_rule", "project_rule", "scoped_rule", "skill",
    ]


def test_paths_are_repository_relative_and_private_paths_cannot_leak():
    assert normalize_repository_path("src/../src/app.py") == "src/app.py"
    assert normalize_repository_path("/home/alice/private.py") is None
    assert normalize_repository_path(r"C:\\Users\\alice\\private.py") is None
    evidence = _evidence(["p1", "p1"])
    events = {
        item["trajectory_id"]: [
            {"sequence": 1, "path": "/home/alice/private.py", "type": "write"},
            {"sequence": 2, "path": r"C:\\Users\\alice\\secret.py", "type": "write"},
        ]
        for item in evidence
    }
    proposals = build_proposals(
        _experience(reusable_lesson="Edit /home/alice/private.py and /opt/acme/secret.py then rerun checks."), evidence, events
    )

    assert all(item["scope_value"] is None or "alice" not in item["scope_value"] for item in proposals)
    assert all("/home/alice" not in item["proposed_content"] for item in proposals)
    assert all("/opt/acme" not in item["proposed_content"] for item in proposals)
    assert "[repository path omitted]" in proposals[0]["proposed_content"]
