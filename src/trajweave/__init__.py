"""TrajWeave - local-first coding-agent trajectory data substrate.

Stages 0-7: discovery, project opt-in, agent-specific parsing, normalization
into a common schema, local SQLite storage, trajectory exploration,
deterministic Experience extraction, placement proposals, and explicit human
review/apply. Nothing is auto-applied to a repository.
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__"]
