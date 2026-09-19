"""Stage 9 file application - reuses Stage 7's safety layer directly.

Deliberately contains no file-writing logic of its own, mirroring
``trajweave.evaluation.apply``. The one structural difference from Stage 7's
``ReviewService`` is that ``managed_key()`` is salted with the logical
``policy_id`` rather than a Stage 6 experience id, so repeated lifecycle
changes to the same logical policy (rewrite, promote, demote, rollback) keep
updating the *same* managed block instead of leaving orphaned ones behind
under the old key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trajweave.review.targets import (
    Preview,
    SafetyError,
    apply_preview,
    build_preview,
    resolve_target,
)

__all__ = ["SafetyError", "resolve_lifecycle_target", "build_lifecycle_preview", "apply_lifecycle_preview"]


def resolve_lifecycle_target(
    *, version: dict[str, Any], project_root: str | Path | None, global_root: str | Path | None,
) -> Any:
    return resolve_target(
        placement_type=str(version["placement_type"]),
        project_root=project_root,
        agent=version.get("target_agent"),
        target=version.get("target_override"),
        global_root=global_root,
        review_id=str(version["id"]),
        scope_type=version.get("scope_type"),
        scope_value=version.get("scope_value"),
    )


def build_lifecycle_preview(*, target: Any, policy_id: str, version: dict[str, Any]) -> Any:
    return build_preview(target=target, experience_id=policy_id, review_id=str(version["id"]), content=str(version["content"]))


def apply_lifecycle_preview(preview: Preview) -> str:
    return apply_preview(preview)
