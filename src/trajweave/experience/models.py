"""Plain dataclasses for the Stage 5 layer (decoupled from SQLite rows)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- pattern taxonomy (Stage 5 brief 17; a small starting set) ---------------
PATTERN_FAILURE_REPAIR = "failure_repair_success"
PATTERN_HUMAN_CORRECTION_REPAIR = "human_correction_repair"
PATTERN_REPEATED_FAILURE = "repeated_failure"
PATTERN_FILE_CHANGE = "file_change_pattern"

# Ranked most-informative first - used to pick an experience's headline
# pattern_type when a cluster mixes several.
PATTERN_RANK = (
    PATTERN_FAILURE_REPAIR,
    PATTERN_HUMAN_CORRECTION_REPAIR,
    PATTERN_REPEATED_FAILURE,
    PATTERN_FILE_CHANGE,
)

SUPPORT = "support"
CONTRADICTION = "contradiction"
AMBIGUOUS = "ambiguous"

# experience.status
STATUS_CANDIDATE = "candidate"
STATUS_NEEDS_MORE = "needs_more_evidence"
STATUS_REJECTED = "rejected"
STATUS_ARCHIVED = "archived"


@dataclass
class Occurrence:
    """One detected pattern episode inside a single trajectory."""

    trajectory_id: str
    project_id: str | None
    pattern_type: str
    group_key: str
    start_sequence: int
    end_sequence: int
    failure_family: str | None = None
    resolution_family: str | None = None
    repair_context: str | None = None
    error_signature: str | None = None
    classification: str = SUPPORT
    features: dict[str, Any] = field(default_factory=dict)
    # filled in by the repository on insert
    id: str | None = None

    def dedupe_key(self) -> str:
        return (
            f"{self.trajectory_id}|{self.pattern_type}|{self.classification}"
            f"|{self.group_key}|{self.start_sequence}|{self.end_sequence}"
        )


@dataclass
class ConfidenceBreakdown:
    support: int
    contradiction: int
    ambiguous: int
    occurrences: int
    projects: int
    support_ratio: float
    recurrence: float
    cross_project: float
    recency: float
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "support": self.support,
            "contradiction": self.contradiction,
            "ambiguous": self.ambiguous,
            "occurrences": self.occurrences,
            "projects": self.projects,
            "components": {
                "support_ratio": round(self.support_ratio, 4),
                "recurrence": round(self.recurrence, 4),
                "cross_project": round(self.cross_project, 4),
                "recency": round(self.recency, 4),
            },
            "weights": {
                "support_ratio": 0.50,
                "recurrence": 0.25,
                "cross_project": 0.15,
                "recency": 0.10,
            },
            "score": self.score,
        }
