"""Deterministic confidence score (Stage 5 brief 15/34).

Not an LLM judgement - a documented, reproducible function of the evidence::

    support_ratio  = S / (S + C)                 # ambiguous excluded
    recurrence     = min(1, N / 6)               # N = supporting+ambiguous occ; saturates at 6
    cross_project  = min(1, (P - 1) / 2)         # P = projects with supporting evidence; 1 -> 0, 3+ -> 1
    recency        = 1.0 if last_seen <= 30d
                     0.6 if <= 90d
                     0.3 if <= 180d
                     0.1 otherwise

    confidence = round( 0.50*support_ratio
                      + 0.25*recurrence
                      + 0.15*cross_project
                      + 0.10*recency , 2)

A pattern with no support and no contradiction scores its support_ratio as 0.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trajweave.experience.models import ConfidenceBreakdown
from trajweave.utils.timeparse import parse_timestamp

_W_SUPPORT = 0.50
_W_RECURRENCE = 0.25
_W_CROSS_PROJECT = 0.15
_W_RECENCY = 0.10

_RECURRENCE_SAT = 6.0
_CROSS_PROJECT_SAT = 2.0  # (P-1)/2 -> 3 distinct projects saturates


def _recency_factor(last_seen_at: str | None, *, now: datetime | None = None) -> float:
    dt = parse_timestamp(last_seen_at)
    if dt is None:
        return 0.3  # unknown recency -> neutral-ish
    now = now or datetime.now(timezone.utc)
    days = (now - dt).total_seconds() / 86400.0
    if days <= 30:
        return 1.0
    if days <= 90:
        return 0.6
    if days <= 180:
        return 0.3
    return 0.1


def compute_confidence(
    *,
    support: int,
    contradiction: int,
    ambiguous: int,
    occurrences: int,
    projects: int,
    last_seen_at: str | None,
    now: datetime | None = None,
) -> ConfidenceBreakdown:
    decisive = support + contradiction
    support_ratio = (support / decisive) if decisive else 0.0
    recurrence = min(1.0, occurrences / _RECURRENCE_SAT)
    cross_project = min(1.0, max(0, projects - 1) / _CROSS_PROJECT_SAT)
    recency = _recency_factor(last_seen_at, now=now)

    score = (
        _W_SUPPORT * support_ratio
        + _W_RECURRENCE * recurrence
        + _W_CROSS_PROJECT * cross_project
        + _W_RECENCY * recency
    )
    return ConfidenceBreakdown(
        support=support,
        contradiction=contradiction,
        ambiguous=ambiguous,
        occurrences=occurrences,
        projects=projects,
        support_ratio=support_ratio,
        recurrence=recurrence,
        cross_project=cross_project,
        recency=recency,
        score=round(score, 2),
    )
