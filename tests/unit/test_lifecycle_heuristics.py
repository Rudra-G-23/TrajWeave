"""Stage 9 recommendation heuristics locked down against the five documented
evidence scenarios (spec section 37) plus reason-code checks."""

from __future__ import annotations

from trajweave.lifecycle.heuristics import (
    detect_duplicate_policies,
    recommend_for_version,
)
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def _repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "tw.db"))


def _review(repo: Repository, review_id: str, fp: str = "fp1") -> str:
    return repo.create_policy_review({
        "id": f"PP-{review_id}", "proposal_set_id": f"PS-{review_id}", "experience_id": f"E-{review_id}",
        "placement_type": "scoped_rule", "scope_type": "directory", "scope_value": "app/billing",
        "proposed_content": "always do x", "score": 0.9, "rank": 1,
        "feature_values_json": "{}", "diagnostics_json": "[]",
        "generator_version": "1", "source_fingerprint": fp,
    })


def _spec(repo: Repository, review_id: str, spec_id: str) -> None:
    repo.create_evaluation_spec({
        "id": spec_id, "review_id": review_id, "experience_id": "E-x", "proposal_id": "PP-x",
        "content_revision": 0, "policy_content": "always do x", "policy_content_hash": "h1",
        "placement_type": "scoped_rule", "target_agent": "claude", "repo_root": "/tmp/x", "repo_commit": "abc",
        "task_spec": {}, "environment": {}, "verifiers": [], "condition_order_mode": "baseline_first",
    })


def _comparisons(repo: Repository, spec_id: str, outcomes: list[str]) -> None:
    for i, outcome in enumerate(outcomes, start=1):
        repo.record_evaluation_comparison({
            "id": f"EC-{spec_id}-{i}", "evaluation_id": spec_id, "repetition_index": i, "outcome": outcome,
        })


def _policy_with_version(repo: Repository, policy_id: str, review_id: str, *, version_number: int = 1, placement_type: str = "scoped_rule") -> str:
    if repo.get_policy(policy_id) is None:
        repo.create_policy(policy_id, origin_review_id=review_id, origin_experience_id="E-x")
    version_id = f"PVN-{policy_id}-{version_number}"
    repo.create_policy_version({
        "id": version_id, "policy_id": policy_id, "version_number": version_number,
        "content": "always do x", "content_hash": f"h{version_number}", "placement_type": placement_type,
        "target_agent": "claude", "scope_type": "directory", "scope_value": "app/billing",
        "created_via": "initial" if version_number == 1 else "rewrite", "created_from_review_id": review_id,
    })
    repo.set_policy_current_version(policy_id, version_id)
    return version_id


# ----------------------------------------------------------------------
# Scenario A - mostly improved -> promote
# ----------------------------------------------------------------------
def test_scenario_a_mostly_improved_recommends_promote(tmp_path):
    repo = _repo(tmp_path)
    review_id = _review(repo, "A")
    _spec(repo, review_id, "EV-A")
    _comparisons(repo, "EV-A", ["improved"] * 8 + ["unchanged"])
    version_id = _policy_with_version(repo, "POL-A", review_id)

    rec = recommend_for_version(repo, version_id)
    assert rec["operation"] == "promote"
    assert "CONSISTENT_CROSS_SCOPE_BENEFIT" in rec["reason_codes"]
    assert rec["strength"] == "strong"


# ----------------------------------------------------------------------
# Scenario B - mostly regressed -> demote/disable
# ----------------------------------------------------------------------
def test_scenario_b_mostly_regressed_recommends_demote(tmp_path):
    repo = _repo(tmp_path)
    review_id = _review(repo, "B")
    _spec(repo, review_id, "EV-B")
    _comparisons(repo, "EV-B", ["improved"] * 2 + ["regressed"] * 6)
    version_id = _policy_with_version(repo, "POL-B", review_id)

    rec = recommend_for_version(repo, version_id)
    assert rec["operation"] == "demote"
    assert "REPEATED_REGRESSION" in rec["reason_codes"]


def test_scenario_b_at_narrowest_scope_recommends_disable(tmp_path):
    repo = _repo(tmp_path)
    review_id = _review(repo, "B2")
    _spec(repo, review_id, "EV-B2")
    _comparisons(repo, "EV-B2", ["improved"] * 2 + ["regressed"] * 6)
    version_id = _policy_with_version(repo, "POL-B2", review_id, placement_type="skill")

    rec = recommend_for_version(repo, version_id)
    assert rec["operation"] == "disable"
    assert "REPEATED_REGRESSION" in rec["reason_codes"]


# ----------------------------------------------------------------------
# Scenario C - no valid evaluations -> no strong promotion
# ----------------------------------------------------------------------
def test_scenario_c_no_evaluations_does_not_promote(tmp_path):
    repo = _repo(tmp_path)
    review_id = _review(repo, "C")
    version_id = _policy_with_version(repo, "POL-C", review_id)

    rec = recommend_for_version(repo, version_id)
    assert rec["operation"] == "retain"
    assert "INSUFFICIENT_EVIDENCE" in rec["reason_codes"]


# ----------------------------------------------------------------------
# Scenario D - only invalid/incomparable -> not evidence of improvement
# ----------------------------------------------------------------------
def test_scenario_d_only_invalid_incomparable_does_not_promote(tmp_path):
    repo = _repo(tmp_path)
    review_id = _review(repo, "D")
    _spec(repo, review_id, "EV-D")
    _comparisons(repo, "EV-D", ["invalid", "incomparable", "invalid", "incomparable"])
    version_id = _policy_with_version(repo, "POL-D", review_id)

    rec = recommend_for_version(repo, version_id)
    assert rec["operation"] == "retain"
    assert "INSUFFICIENT_EVIDENCE" in rec["reason_codes"]
    assert rec["evidence"]["improved_ratio"] is None


# ----------------------------------------------------------------------
# Scenario E - new version regresses vs a previously-good version -> rollback
# ----------------------------------------------------------------------
def test_scenario_e_new_version_regression_recommends_rollback(tmp_path):
    repo = _repo(tmp_path)
    review_v1 = _review(repo, "E1", fp="fpv1")
    _spec(repo, review_v1, "EV-E1")
    _comparisons(repo, "EV-E1", ["improved"] * 6)
    v1 = _policy_with_version(repo, "POL-E", review_v1, version_number=1)

    review_v2 = _review(repo, "E2", fp="fpv2")
    _spec(repo, review_v2, "EV-E2")
    _comparisons(repo, "EV-E2", ["regressed"] * 5 + ["unchanged"])
    v2 = _policy_with_version(repo, "POL-E", review_v2, version_number=2)

    rec = recommend_for_version(repo, v2)
    assert rec["operation"] == "rollback"
    assert "NEW_VERSION_REGRESSION" in rec["reason_codes"]
    assert rec["evidence"]["rollback_candidate_version_id"] == v1


# ----------------------------------------------------------------------
# Deterministic recommendation ids: unchanged evidence -> same id
# ----------------------------------------------------------------------
def test_recommendation_id_is_deterministic_for_same_evidence(tmp_path):
    repo = _repo(tmp_path)
    review_id = _review(repo, "DET")
    _spec(repo, review_id, "EV-DET")
    _comparisons(repo, "EV-DET", ["improved"] * 8 + ["unchanged"])
    version_id = _policy_with_version(repo, "POL-DET", review_id)

    rec1 = recommend_for_version(repo, version_id)
    rec2 = recommend_for_version(repo, version_id)
    assert rec1["id"] == rec2["id"]


def test_detect_duplicate_policies(tmp_path):
    repo = _repo(tmp_path)
    review_id = _review(repo, "DUP")
    _policy_with_version(repo, "POL-DUP-1", review_id)
    _policy_with_version(repo, "POL-DUP-2", review_id)
    duplicates = detect_duplicate_policies(repo)
    assert len(duplicates) == 1
    assert set(duplicates[0]["policy_ids"]) == {"POL-DUP-1", "POL-DUP-2"}
    assert "DUPLICATE_POLICY" in duplicates[0]["reason_codes"]
