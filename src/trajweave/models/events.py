from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trajweave.models.enums import EventType


@dataclass
class NormalizedEvent:
    """One step in a normalized trajectory.

    Only ``type`` is mandatory. ``sequence`` is assigned by the trajectory when
    the event list is finalized, so adapters may leave it at 0.
    """

    type: EventType
    sequence: int = 0
    timestamp: str | None = None          # ISO-8601 UTC
    summary: str | None = None            # short, redacted, length-bounded
    path: str | None = None               # repo-relative when resolvable
    command: str | None = None            # redacted, length-bounded
    exit_code: int | None = None
    tool_name: str | None = None
    redacted: bool = False                # True if redaction touched this event
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": str(self.type),
            "timestamp": self.timestamp,
            "summary": self.summary,
            "path": self.path,
            "command": self.command,
            "exit_code": self.exit_code,
            "tool_name": self.tool_name,
            "redacted": 1 if self.redacted else 0,
            "metadata": self.metadata or None,
        }
