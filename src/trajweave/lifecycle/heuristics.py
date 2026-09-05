"""Deterministic, explainable Stage 9 recommendation heuristics.

Every threshold here is a named constant, not a magic number, and every
recommendation carries the reason code(s) that produced it. This is
explicitly NOT a statistical-significance test (that is Stage 10's job) -
it is a documented, fixed decision rule over Stage 8's outcome counts.

Only ``improved`` / ``unchanged`` / ``regressed`` comparisons ever count
toward a ratio. ``invalid`` and ``incomparable`` comparisons are always
reported but never counted as evidence of improvement or regression
(Stage 9 spec section 17/38, scenario D).
"""

from __future__ import annotations

from typing import Any

from trajweave.lifecycle.evidence import aggregate_evidence
from trajweave.lifecycle.models import DEMOTE_TARGETS, PROMOTE_TARGETS
from trajweave.storage.repository import Repository
from trajweave.utils.hashing import stable_short_id, text_sha256

# A recommendation needs at least this many valid (improved/unchanged/
# regressed) comparisons before it is treated as a signal at all. Below this,
# Stage 9 explicitly refuses to recommend a change (scenarios C and D).
MIN_VALID_FOR_SIGNAL = 3

# >= this share of valid comparisons improved, with zero regressions,
# recommends promoting to a broader scope (scenario A).
PROMOTE_IMPROVED_RATIO = 0.7

# >= this share of valid comparisons regressed recommends demoting (or
# disabling, if already at the narrowest scope) (scenario B).
DEMOTE_REGRESSED_RATIO = 0.5

# A regression share in [MIXED_REGRESSED_LOW, DEMOTE_REGRESSED_RATIO) alongside
# any real improvement is read as "helps in its lane, costs elsewhere" and
# favors demoting rather than an outright disable.
MIXED_REGRESSED_LOW = 0.2

# Rollback (scenario E) requires both sides to individually clear the signal
# floor: the new version regressing, and the version it would roll back to
# having previously looked good.
ROLLBACK_MIN_VALID = MIN_VALID_FOR_SIGNAL


def _recommendation_id(policy_id: str, version_id: str, operation: str, evidence: dict[str, Any]) -> str:
    fingerprint = text_sha256(
        f"{version_id}|{operation}|{evidence.get('total_comparisons')}|{evidence.get('improved')}|"
        f"{evidence.get('regressed')}|{evidence.get('unchanged')}|{evidence.get('invalid')}|"
        f"{evidence.get('incomparable')}"
    )
    return stable_short_id("REC-", policy_id, version_id, operation, fingerprint, length=16)


def _predecessor(repo: Repository, version: dict[str, Any]) -> dict[str, Any] | None:
    if int(version["version_number"]) <= 1:
        return None
    row = repo.db.query_one(
        "SELECT * FROM policy_versions WHERE policy_id = ? AND version_number = ?",
        (version["policy_id"], int(version["version_number"]) - 1),
    )
    return dict(row) if row else None


def recommend_for_version(repo: Repository, version_id: str) -> dict[str, Any]:
    """Compute (but do not persist) a single deterministic recommendation."""

    version = repo.get_policy_version(version_id)
    if version is None:
        raise ValueError(f"no policy version {version_id}")
    version = dict(version)
    policy_id = str(version["policy_id"])
    placement = str(version["placement_type"])
    evidence = aggregate_evidence(repo, version_id)

    reason_codes: list[str] = []
    counter_evidence: list[str] = []
    valid = evidence["valid_comparisons"]
    improved_ratio = evidence["improved_ratio"] or 0.0
    regressed_ratio = evidence["regressed_ratio"] or 0.0

    if evidence["invalid"] or evidence["incomparable"]:
        counter_evidence.append(
            f"{evidence['invalid']} invalid and {evidence['incomparable']} incomparable comparison(s) "
            "were excluded from the ratios above and are not evidence of improvement or regression."
        )

    # Scenario E: does this exact version regress relative to a prior version
    # that had previously looked good?
    predecessor = _predecessor(repo, version)
    if predecessor is not None and valid >= ROLLBACK_MIN_VALID and regressed_ratio >= DEMOTE_REGRESSED_RATIO:
        predecessor_evidence = aggregate_evidence(repo, str(predecessor["id"]))
        pred_valid = predecessor_evidence["valid_comparisons"]
        pred_improved_ratio = predecessor_evidence["improved_ratio"] or 0.0
        if pred_valid >= ROLLBACK_MIN_VALID and pred_improved_ratio >= PROMOTE_IMPROVED_RATIO:
            reason_codes.append("NEW_VERSION_REGRESSION")
            explanation = (
                f"Version {version['version_number']} regressed in {evidence['regressed']}/{valid} valid "
                f"comparisons, while the prior version {predecessor['version_number']} improved outcomes in "
                f"{predecessor_evidence['improved']}/{pred_valid}. Rolling back to version "
                f"{predecessor['version_number']} is recommended."
            )
            evidence["rollback_candidate_version_id"] = predecessor["id"]
            return _build(policy_id, version_id, "rollback", reason_codes, explanation, evidence, counter_evidence, "strong")

    if valid < MIN_VALID_FOR_SIGNAL:
        reason_codes.append("INSUFFICIENT_EVIDENCE")
        explanation = (
            f"Only {valid} valid (improved/unchanged/regressed) comparison(s) are available; at least "
            f"{MIN_VALID_FOR_SIGNAL} are required before Stage 9 will recommend a lifecycle change."
        )
        return _build(policy_id, version_id, "retain", reason_codes, explanation, evidence, counter_evidence, "informational")

    if regressed_ratio >= DEMOTE_REGRESSED_RATIO:
        reason_codes.append("REPEATED_REGRESSION")
        targets = DEMOTE_TARGETS.get(placement, ())
        if targets:
            explanation = (
                f"{evidence['regressed']}/{valid} valid comparisons regressed. Demoting from {placement} to a "
                f"narrower scope is recommended to limit the blast radius while evidence remains negative."
            )
            return _build(policy_id, version_id, "demote", reason_codes, explanation, evidence, counter_evidence, "strong")
        explanation = (
            f"{evidence['regressed']}/{valid} valid comparisons regressed and {placement} has no narrower "
            "scope to demote to; disabling is recommended."
        )
        return _build(policy_id, version_id, "disable", reason_codes, explanation, evidence, counter_evidence, "strong")

    if MIXED_REGRESSED_LOW <= regressed_ratio and improved_ratio > 0 and placement in DEMOTE_TARGETS and DEMOTE_TARGETS[placement]:
        reason_codes.append("HIGH_WRONG_SCOPE_COST")
        explanation = (
            f"{evidence['improved']}/{valid} valid comparisons improved but {evidence['regressed']}/{valid} "
            f"regressed - evidence suggests {placement} scope is broader than the policy's real benefit. "
            "Demoting to a narrower scope is recommended."
        )
        return _build(policy_id, version_id, "demote", reason_codes, explanation, evidence, counter_evidence, "moderate")

    if improved_ratio >= PROMOTE_IMPROVED_RATIO and evidence["regressed"] == 0:
        reason_codes.append("CONSISTENT_CROSS_SCOPE_BENEFIT")
        targets = PROMOTE_TARGETS.get(placement, ())
        if targets:
            explanation = (
                f"{evidence['improved']}/{valid} valid comparisons improved with zero regressions. Promoting "
                f"from {placement} to a broader scope is recommended."
            )
            return _build(policy_id, version_id, "promote", reason_codes, explanation, evidence, counter_evidence, "strong")
        explanation = (
            f"{evidence['improved']}/{valid} valid comparisons improved with zero regressions, but {placement} "
            "is already the broadest applicable scope; retaining as-is is recommended."
        )
        return _build(policy_id, version_id, "retain", reason_codes, explanation, evidence, counter_evidence, "moderate")

    explanation = (
        f"Evidence is mixed or inconclusive ({evidence['improved']} improved, {evidence['unchanged']} unchanged, "
        f"{evidence['regressed']} regressed of {valid} valid comparisons); no lifecycle change is recommended yet."
    )
    return _build(policy_id, version_id, "retain", reason_codes, explanation, evidence, counter_evidence, "informational")


def _build(
    policy_id: str, version_id: str, operation: str, reason_codes: list[str], explanation: str,
    evidence: dict[str, Any], counter_evidence: list[str], strength: str,
) -> dict[str, Any]:
    rec_id = _recommendation_id(policy_id, version_id, operation, evidence)
    return {
        "id": rec_id, "policy_id": policy_id, "version_id": version_id, "operation": operation,
        "reason_codes": reason_codes, "explanation": explanation, "evidence": evidence,
        "counter_evidence": counter_evidence, "strength": strength,
    }


def detect_stale(repo: Repository, paths: Any, version: dict[str, Any]) -> list[str]:
    """Reviewable staleness signals only - never an automatic prune (spec section 16)."""

    reasons: list[str] = []
    placement = str(version["placement_type"])
    if placement in {"project_rule", "scoped_rule", "skill"}:
        scope_value = version.get("scope_value")
        if scope_value:
            project = repo.get_project(str(scope_value))
            if project is None:
                reasons.append("target_project_not_registered")
            else:
                from pathlib import Path

                if not Path(str(project["root"])).exists():
                    reasons.append("target_repository_missing")
    superseded_by = repo.db.query_one(
        "SELECT to_version_id FROM policy_lineage WHERE from_version_id = ? "
        "AND relation IN ('merge_parent') ORDER BY id DESC LIMIT 1",
        (version["id"],),
    )
    if superseded_by is not None:
        reasons.append("superseded_by_merge")
    return reasons


def detect_duplicate_policies(repo: Repository) -> list[dict[str, Any]]:
    """Cross-policy scan: active policies whose current version content is
    byte-identical are flagged as DUPLICATE_POLICY merge candidates."""

    rows = [dict(r) for r in repo.list_policies(status="active") if r["current_version_id"]]
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_hash.setdefault(str(row["current_content_hash"]), []).append(row)
    duplicates = []
    for content_hash, group in by_hash.items():
        if len(group) < 2:
            continue
        ids = sorted(str(g["id"]) for g in group)
        duplicates.append({
            "reason_codes": ["DUPLICATE_POLICY"],
            "policy_ids": ids,
            "content_hash": content_hash,
            "explanation": f"Policies {', '.join(ids)} currently share identical content and are merge candidates.",
        })
    return duplicates
