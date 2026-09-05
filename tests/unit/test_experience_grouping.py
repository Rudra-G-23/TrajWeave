from __future__ import annotations

from datetime import datetime, timezone

from trajweave.experience.grouping import TrajectoryProfile, build_experiences
from trajweave.experience.models import (
    PATTERN_FAILURE_REPAIR,
    STATUS_CANDIDATE,
    STATUS_NEEDS_MORE,
    AMBIGUOUS,
    SUPPORT,
    Occurrence,
)

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def _support(tid, project, ante=("model",), group="fr::migration::test"):
    return Occurrence(
        trajectory_id=tid, project_id=project, pattern_type=PATTERN_FAILURE_REPAIR,
        group_key=group, start_sequence=3, end_sequence=5,
        failure_family="test", resolution_family="test", repair_context="migration",
        classification=SUPPORT, features={"antecedent_contexts": list(ante)},
    )


def _profile(tid, project, *, edits, clean_pass=(), status="success"):
    return TrajectoryProfile(
        trajectory_id=tid, project_id=project, final_status=status,
        last_activity="2026-08-28T00:00:00+00:00",
        edit_contexts=set(edits), clean_pass_families=set(clean_pass), max_sequence=9,
    )


def test_three_supports_one_project_is_a_candidate():
    occ = [_support(f"t{i}", "p1") for i in range(3)]
    profiles = {o.trajectory_id: _profile(o.trajectory_id, "p1", edits={"model", "migration"})
                for o in occ}
    exps, _ = build_experiences(occ, profiles, now=NOW)
    assert len(exps) == 1
    e = exps[0]
    assert e.status == STATUS_CANDIDATE
    assert e.support_count == 3 and e.contradiction_count == 0
    assert e.pattern_type == PATTERN_FAILURE_REPAIR


def test_two_supports_is_needs_more_evidence():
    occ = [_support("t0", "p1"), _support("t1", "p1")]
    profiles = {o.trajectory_id: _profile(o.trajectory_id, "p1", edits={"model", "migration"})
                for o in occ}
    exps, _ = build_experiences(occ, profiles, now=NOW)
    assert exps[0].status == STATUS_NEEDS_MORE


def test_single_support_produces_no_experience():
    occ = [_support("t0", "p1")]
    profiles = {"t0": _profile("t0", "p1", edits={"model", "migration"})}
    exps, _ = build_experiences(occ, profiles, now=NOW)
    assert exps == []


def test_contradiction_is_synthesized_and_lowers_confidence():
    occ = [_support(f"t{i}", "p1") for i in range(3)]
    profiles = {o.trajectory_id: _profile(o.trajectory_id, "p1", edits={"model", "migration"})
                for o in occ}
    # a 4th trajectory: changed the model, tests passed cleanly, never touched migration
    profiles["t9"] = _profile("t9", "p2", edits={"model"}, clean_pass={"test"})

    with_contra, all_occ = build_experiences(occ, profiles, now=NOW)
    e = with_contra[0]
    assert e.contradiction_count == 1
    assert e.project_count == 2
    assert any(o.classification == "contradiction" and o.trajectory_id == "t9" for o in all_occ)

    # remove the contradicting profile -> higher confidence
    profiles.pop("t9")
    clean, _ = build_experiences(occ, profiles, now=NOW)
    assert clean[0].confidence.score > e.confidence.score


def test_touching_repair_context_is_not_a_contradiction():
    occ = [_support(f"t{i}", "p1") for i in range(3)]
    profiles = {o.trajectory_id: _profile(o.trajectory_id, "p1", edits={"model", "migration"})
                for o in occ}
    # t9 DID touch migration -> not a contradiction, just not counted
    profiles["t9"] = _profile("t9", "p2", edits={"model", "migration"}, clean_pass={"test"})
    exps, _ = build_experiences(occ, profiles, now=NOW)
    assert exps[0].contradiction_count == 0


def test_only_ambiguous_evidence_is_not_an_experience():
    occ = [
        Occurrence(trajectory_id=f"t{i}", project_id="p1", pattern_type=PATTERN_FAILURE_REPAIR,
                   group_key="fr::other::test", start_sequence=1, end_sequence=2,
                   resolution_family="test", repair_context="other", classification=AMBIGUOUS,
                   features={})
        for i in range(3)
    ]
    profiles = {o.trajectory_id: _profile(o.trajectory_id, "p1", edits={"backend"}) for o in occ}
    exps, _ = build_experiences(occ, profiles, now=NOW)
    assert exps == []
