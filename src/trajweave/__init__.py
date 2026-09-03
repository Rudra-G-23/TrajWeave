"""TrajWeave - local-first coding-agent trajectory data substrate.

Stages 0-5: discovery, project opt-in, agent-specific parsing, normalization
into a common schema, local SQLite storage, a read-only trajectory explorer,
and deterministic Experience extraction (recurring success/failure patterns
aggregated into evidence-backed *candidate* experiences). Nothing here writes
rules into a repository, edits agent config, or auto-applies knowledge -
placement is a later stage.
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__"]
