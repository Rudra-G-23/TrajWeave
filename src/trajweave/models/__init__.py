"""The normalized schema every adapter targets.

Small, stable dataclasses (:class:`NormalizedTrajectory`, :class:`NormalizedEvent`,
:class:`FileTouch`) and the closed enums that constrain them. Everything the
storage layer persists is built out of these types.
"""

from __future__ import annotations

from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import (
    DiscoveredSession,
    FileTouch,
    NormalizedTrajectory,
    SourceSessionRef,
)

__all__ = [
    "Agent",
    "EventType",
    "FinalStatus",
    "TaskSource",
    "NormalizedEvent",
    "NormalizedTrajectory",
    "DiscoveredSession",
    "SourceSessionRef",
    "FileTouch",
]
