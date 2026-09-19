"""Stage 9 evidence aggregation, tested directly against the repository
without needing the full trajectory -> experience -> placement pipeline.

A minimal but real Stage 7 review row is enough to hang Stage 8
``evaluation_specs``/``evaluation_comparisons`` off of (their FK only points
at ``policy_reviews.id``); baseline/candidate run rows are optional
(nullable FKs), so comparisons can be constructed directly.
"""

from __future__ import annotations

from trajweave.lifecycle.evidence import aggregate_evidence, link_new_evidence
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def _repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "tw.db"))


def _fake_review(repo: Repository, review_id: str = "RV-FAKE-1") -> str:
    context = {
        "id": "PP-FAKE-1", "proposal_set_id": "PS-FAKE-1", "experience_id": "E-FAKE-1",
        "placement_type": "project_rule", "scope_type": "project", "scope_value": "p1",
        "proposed_content": "always do x", "score": 0.9, "rank": 1,
        "feature_values_json": "{}", "diagnostics_json": "[]",
        "generator_version": "1", "source_fingerprint": "fp1",
    }
    return repo.create_policy_review(context)


def _spec(repo: Repository, review_id: str, spec_id: str) -> None:
    repo.create_evaluation_spec({
        "id": spec_id, "review_id": review_id, "experience_id": "E-FAKE-1", "proposal_id": "PP-FAKE-1",
        "content_revision": 0, "policy_content": "always do x", "policy_content_hash": "h1",
        "placement_type": "project_rule", "target_agent": "claude", "repo_root": "/tmp/x", "repo_commit": "abc",
        "task_spec": {}, "environment": {}, "verifiers": [], "condition_order_mode": "baseline_first",
    })


def _comparison(repo: Repository, spec_id: str, comparison_id: str, outcome: str, *, rep: int = 1, duration_delta=None, regression_count: int = 0) -> None:
    repo.record_evaluation_comparison({
        "id": comparison_id, "evaluation_id": spec_id, "repetition_index": rep, "outcome": outcome,
        "regression_count": regression_count,
        "metrics_delta": {"duration_ms": {"delta": duration_delta}} if duration_delta is not None else {},
    })


def _policy_version(repo: Repository, review_id: str, policy_id: str = "POL-FAKE", version_number: int = 1) -> str:
    repo.create_policy(policy_id, origin_review_id=review_id, origin_experience_id="E-FAKE-1")
    version_id = f"PVN-{policy_id}-{version_number}"
    repo.create_policy_version({
        "id": version_id, "policy_id": policy_id, "version_number": version_number,
        "content": "always do x", "content_hash": "h1", "placement_type": "project_rule",
        "target_agent": "claude", "scope_type": "project", "scope_value": "p1",
        "created_via": "initial", "created_from_review_id": review_id,
    })
    repo.set_policy_current_version(policy_id, version_id)
    return version_id


def test_aggregate_counts_and_ratios(tmp_path):
    repo = _repo(tmp_path)
    review_id = _fake_review(repo)
    _spec(repo, review_id, "EV-1")
    _comparison(repo, "EV-1", "EC-1", "improved", rep=1, duration_delta=-10)
    _comparison(repo, "EV-1", "EC-2", "improved", rep=2, duration_delta=-20)
    _comparison(repo, "EV-1", "EC-3", "unchanged", rep=3)
    _comparison(repo, "EV-1", "EC-4", "regressed", rep=4, regression_count=2)
    _comparison(repo, "EV-1", "EC-5", "invalid", rep=5)
    _comparison(repo, "EV-1", "EC-6", "incomparable", rep=6)
    version_id = _policy_version(repo, review_id)

    evidence = aggregate_evidence(repo, version_id)
    assert evidence["total_comparisons"] == 6
    assert evidence["valid_comparisons"] == 4
    assert evidence["improved"] == 2
    assert evidence["unchanged"] == 1
    assert evidence["regressed"] == 1
    assert evidence["invalid"] == 1
    assert evidence["incomparable"] == 1
    assert evidence["improved_ratio"] == 0.5
    assert evidence["regressed_ratio"] == 0.25
    assert evidence["total_regression_checks"] == 2
    assert evidence["avg_duration_delta_ms"] == -15
    assert evidence["latest_outcome"] == "incomparable"
    assert evidence["policy_bytes"] == len(b"always do x")


def test_aggregate_with_no_comparisons_reports_none_ratios(tmp_path):
    repo = _repo(tmp_path)
    review_id = _fake_review(repo)
    version_id = _policy_version(repo, review_id)
    evidence = aggregate_evidence(repo, version_id)
    assert evidence["total_comparisons"] == 0
    assert evidence["improved_ratio"] is None
    assert evidence["regressed_ratio"] is None
    assert evidence["latest_outcome"] is None


def test_only_invalid_and_incomparable_never_count_as_improvement(tmp_path):
    repo = _repo(tmp_path)
    review_id = _fake_review(repo)
    _spec(repo, review_id, "EV-1")
    _comparison(repo, "EV-1", "EC-1", "invalid", rep=1)
    _comparison(repo, "EV-1", "EC-2", "incomparable", rep=2)
    version_id = _policy_version(repo, review_id)
    evidence = aggregate_evidence(repo, version_id)
    assert evidence["valid_comparisons"] == 0
    assert evidence["improved_ratio"] is None
    assert evidence["invalid"] == 1 and evidence["incomparable"] == 1


def test_linking_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    review_id = _fake_review(repo)
    _spec(repo, review_id, "EV-1")
    _comparison(repo, "EV-1", "EC-1", "improved")
    version_id = _policy_version(repo, review_id)
    version = dict(repo.get_policy_version(version_id))
    link_new_evidence(repo, version)
    link_new_evidence(repo, version)
    assert len(repo.list_version_evidence(version_id)) == 1


def test_linking_is_scoped_to_the_versions_own_review(tmp_path):
    """A version with no created_from_review_id sees no evidence at all -
    lifecycle-created versions never inherit a predecessor's evidence."""

    repo = _repo(tmp_path)
    review_id = _fake_review(repo)
    _spec(repo, review_id, "EV-1")
    _comparison(repo, "EV-1", "EC-1", "improved")
    repo.create_policy("POL-NEW", origin_review_id=None, origin_experience_id=None)
    repo.create_policy_version({
        "id": "PVN-POL-NEW-1", "policy_id": "POL-NEW", "version_number": 1, "content": "x",
        "content_hash": "hx", "placement_type": "project_rule", "created_via": "initial",
        "created_from_review_id": None,
    })
    repo.set_policy_current_version("POL-NEW", "PVN-POL-NEW-1")
    evidence = aggregate_evidence(repo, "PVN-POL-NEW-1")
    assert evidence["total_comparisons"] == 0
