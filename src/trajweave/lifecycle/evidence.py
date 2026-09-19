"""Deterministic Stage 8 evidence aggregation for one policy version.

This module never invents statistical significance and never treats an
``invalid``/``incomparable`` comparison as evidence of improvement or
regression - it is simply excluded from the ratios and reported separately
so the negative/uncertain signal stays visible (Stage 9 spec section 17).
"""

from __future__ import annotations

import json
from typing import Any

from trajweave.storage.repository import Repository

_OUTCOMES = ("improved", "unchanged", "regressed", "invalid", "incomparable")


def _decode(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def link_new_evidence(repo: Repository, version: dict[str, Any]) -> None:
    """Link any Stage 8 comparisons for this version's originating review.

    This is an append-only, idempotent bookkeeping step (INSERT OR IGNORE)
    that records *which* comparisons were considered evidence at aggregation
    time - it never copies comparison content.
    """

    review_id = version.get("created_from_review_id")
    if not review_id:
        return
    for comparison in repo.comparisons_for_review(str(review_id)):
        repo.link_version_evidence(str(version["id"]), str(comparison["id"]))


def aggregate_evidence(repo: Repository, version_id: str) -> dict[str, Any]:
    """Return a deterministic evidence summary for one policy version.

    Also links any newly-available Stage 8 comparisons for this version's
    originating review before summarizing, so the summary is always current.
    """

    version = repo.get_policy_version(version_id)
    if version is None:
        raise ValueError(f"no policy version {version_id}")
    version = dict(version)
    link_new_evidence(repo, version)

    comparisons = [dict(row) for row in repo.list_version_evidence(version_id)]
    counts = {outcome: 0 for outcome in _OUTCOMES}
    for row in comparisons:
        outcome = row["outcome"]
        if outcome in counts:
            counts[outcome] += 1

    valid = counts["improved"] + counts["unchanged"] + counts["regressed"]
    total = len(comparisons)
    total_regression_checks = sum(int(row.get("regression_count") or 0) for row in comparisons)

    duration_deltas: list[float] = []
    for row in comparisons:
        delta = (_decode(row.get("metrics_delta_json"), {}) or {}).get("duration_ms", {}).get("delta")
        if delta is not None:
            duration_deltas.append(float(delta))
    avg_duration_delta_ms = sum(duration_deltas) / len(duration_deltas) if duration_deltas else None

    latest = max(comparisons, key=lambda r: r["created_at"]) if comparisons else None

    return {
        "policy_version_id": version_id,
        "total_comparisons": total,
        "valid_comparisons": valid,
        "improved": counts["improved"],
        "unchanged": counts["unchanged"],
        "regressed": counts["regressed"],
        "invalid": counts["invalid"],
        "incomparable": counts["incomparable"],
        "improved_ratio": (counts["improved"] / valid) if valid else None,
        "regressed_ratio": (counts["regressed"] / valid) if valid else None,
        "total_regression_checks": total_regression_checks,
        "avg_duration_delta_ms": avg_duration_delta_ms,
        "policy_bytes": len(str(version["content"]).encode("utf-8")),
        "latest_outcome": latest["outcome"] if latest else None,
        "latest_comparison_id": latest["id"] if latest else None,
    }
