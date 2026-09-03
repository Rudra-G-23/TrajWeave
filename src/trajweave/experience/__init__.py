"""Stage 5 - Experience Extraction.

Deterministic detection of recurring success/failure patterns across many
normalized trajectories, aggregated into evidence-backed *candidate*
experiences. This layer never edits a repository, an ``AGENTS.md`` /
``CLAUDE.md``, or any source code, and never decides final placement (Stage 6).

Pipeline::

    normalized trajectories
        -> reliable signal extraction        (corrections, signatures, context)
        -> pattern occurrence detection      (detect.py)
        -> cross-trajectory grouping         (grouping.py)
        -> evidence + contradiction analysis (grouping.py)
        -> deterministic / optional-LLM summary (summarize.py)
        -> experience candidate              (extract.py -> storage)
"""

from __future__ import annotations

from trajweave.experience.config import ExperienceConfig
from trajweave.experience.extract import ExperienceExtractor, ExtractionResult

__all__ = ["ExperienceConfig", "ExperienceExtractor", "ExtractionResult"]
