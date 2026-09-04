"""Candidate policy application for Stage 8.

This module deliberately contains no file-writing logic of its own. It only
adapts a frozen evaluation spec's placement fields into the same plain-path
``resolve_target`` / ``build_preview`` / ``apply_preview`` calls Stage 7's
``ReviewService`` uses, pointed at an isolated workspace directory instead of
a DB-registered project root. Every Stage 7 protection (managed-section
preservation, atomic writes, symlink/path-traversal/binary/malformed-marker
rejection) therefore applies unmodified inside the sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trajweave.review.targets import SafetyError, apply_preview, build_preview, resolve_target

__all__ = ["SafetyError", "apply_candidate_policy"]


def apply_candidate_policy(workspace_root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Render and apply the frozen candidate policy inside ``workspace_root``.

    Raises :class:`SafetyError` (from ``trajweave.review.targets``) on any
    safety violation - the caller is responsible for recording that as a
    failed run rather than letting it propagate past the evaluation loop.
    """

    placement_type = str(spec["placement_type"])
    is_global = placement_type == "global_rule"
    project_root = None if is_global else workspace_root
    # A "global" policy has no home inside a cloned repository; give it an
    # isolated stand-in under the same ephemeral workspace so evaluation
    # never touches the real machine-wide ~/.trajweave/policies directory.
    global_root = (workspace_root / ".trajweave-eval-global" / str(spec["target_agent"])) if is_global else None

    target_spec = resolve_target(
        placement_type=placement_type,
        project_root=project_root,
        agent=str(spec["target_agent"]),
        target=spec.get("target_override"),
        global_root=global_root,
        review_id=str(spec["review_id"]),
        scope_type=spec.get("scope_type"),
        scope_value=spec.get("scope_value"),
    )
    preview = build_preview(
        target=target_spec,
        experience_id=str(spec["experience_id"]),
        review_id=str(spec["review_id"]),
        content=str(spec["policy_content"]),
    )
    outcome = apply_preview(preview)
    return {
        "outcome": outcome,
        "target_path": str(target_spec.path),
        "before_hash": preview.target_hash,
        "after_hash": preview.output_hash,
        "unified_diff": preview.unified_diff,
    }
