"""Small serializable domain objects used by the placement engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PlacementType(str, Enum):
    """The canonical alternatives Stage 7 may later render or apply."""

    IGNORE = "ignore"
    GLOBAL_RULE = "global_rule"
    PROJECT_RULE = "project_rule"
    SCOPED_RULE = "scoped_rule"
    SKILL = "skill"


PLACEMENT_TYPES: tuple[str, ...] = tuple(item.value for item in PlacementType)


@dataclass(frozen=True)
class Scope:
    scope_type: str
    scope_value: str | None
    support_count: int = 0
    concentration: float = 0.0
    specificity: float = 0.0


@dataclass(frozen=True)
class PlacementFeatures:
    """All numeric inputs to the public deterministic scoring function."""

    support_count: int
    contradiction_count: int
    ambiguous_count: int
    trajectory_count: int
    project_count: int
    dominant_project_id: str | None
    support_ratio: float
    recurrence: float
    cross_project_support: float
    project_concentration: float
    context_consistency: float
    actionability: float
    procedural_complexity: float
    confidence: float
    recency: float
    scope_type: str
    scope_value: str | None
    scope_support_count: int
    scope_concentration: float
    scope_specificity: float
    global_eligible: bool

    def as_dict(self) -> dict[str, Any]:
        # Rounded values make stored research snapshots stable and readable.
        result: dict[str, Any] = {
            "support_count": self.support_count,
            "contradiction_count": self.contradiction_count,
            "ambiguous_count": self.ambiguous_count,
            "trajectory_count": self.trajectory_count,
            "project_count": self.project_count,
            "dominant_project_id": self.dominant_project_id,
            "support_ratio": round(self.support_ratio, 4),
            "recurrence": round(self.recurrence, 4),
            "cross_project_support": round(self.cross_project_support, 4),
            "project_concentration": round(self.project_concentration, 4),
            "context_consistency": round(self.context_consistency, 4),
            "actionability": round(self.actionability, 4),
            "procedural_complexity": round(self.procedural_complexity, 4),
            "confidence": round(self.confidence, 4),
            "recency": round(self.recency, 4),
            "scope_type": self.scope_type,
            "scope_value": self.scope_value,
            "scope_support_count": self.scope_support_count,
            "scope_concentration": round(self.scope_concentration, 4),
            "scope_specificity": round(self.scope_specificity, 4),
            "global_eligible": self.global_eligible,
        }
        return result


@dataclass(frozen=True)
class Proposal:
    placement_type: PlacementType
    scope: Scope
    proposed_content: str
    score: float
    rank: int
    diagnostics: tuple[dict[str, Any], ...]
    features: PlacementFeatures
    evidence_occurrence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "placement_type": self.placement_type.value,
            "scope_type": self.scope.scope_type,
            "scope_value": self.scope.scope_value,
            "proposed_content": self.proposed_content,
            "score": self.score,
            "rank": self.rank,
            "diagnostics": list(self.diagnostics),
            "features": self.features.as_dict(),
            "evidence_occurrence_ids": list(self.evidence_occurrence_ids),
        }
