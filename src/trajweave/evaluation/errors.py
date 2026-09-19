"""Domain errors for Stage 8 evaluation requests."""

from __future__ import annotations


class EvaluationError(RuntimeError):
    """An evaluation request is invalid, ineligible, or cannot proceed safely."""
