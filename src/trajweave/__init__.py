"""TrajWeave - local-first coding-agent trajectory data substrate.

Stages 0-3 only: discovery, project opt-in, agent-specific parsing, normalization
into a common schema, and local SQLite storage. No learning / rule-generation /
repository mutation happens here.
"""

from __future__ import annotations

__version__ = "0.3.0"

__all__ = ["__version__"]
