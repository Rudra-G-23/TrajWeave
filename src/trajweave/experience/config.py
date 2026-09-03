"""Tunable knobs for Stage 5 extraction.

Deliberately small (Stage 5 brief section 46). Defaults are chosen from the real
local dataset; every value is overridable from the ``trajweave experiences
extract`` CLI. There is intentionally no config *file* yet - that is a separate
concern with its own parser questions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExperienceConfig:
    #: An experience needs at least this many occurrences to become a
    #: ``candidate``. Exactly ``min_occurrences - 1`` -> ``needs_more_evidence``.
    min_occurrences: int = 3

    #: Independent-project floor. Kept at 1 on purpose: repo-specific knowledge
    #: is valuable, so a single-project pattern can still be a candidate.
    min_projects: int = 1

    #: Max number of events between a failure and its resolution for the pair to
    #: count as one "associated repair sequence" (not a proven cause).
    max_event_gap: int = 20

    #: A ``human_correction`` turn with <= this many tokens and no actionable
    #: content is treated as a bare acknowledgement, not a meaningful correction.
    meaningful_correction_min_tokens: int = 3

    #: Optional semantic summarization. Off by default - Stage 5 must produce
    #: useful candidates with zero paid API calls.
    use_llm_summary: bool = False

    def validated(self) -> "ExperienceConfig":
        if self.min_occurrences < 1:
            raise ValueError("min_occurrences must be >= 1")
        if self.min_projects < 1:
            raise ValueError("min_projects must be >= 1")
        if self.max_event_gap < 1:
            raise ValueError("max_event_gap must be >= 1")
        if self.meaningful_correction_min_tokens < 1:
            raise ValueError("meaningful_correction_min_tokens must be >= 1")
        return self
