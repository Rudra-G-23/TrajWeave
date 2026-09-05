from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajweave.models.enums import Agent, FinalStatus, TaskSource
from trajweave.models.events import NormalizedEvent


@dataclass
class DiscoveredSession:
    """A candidate session found on disk, before parsing.

    Cheap to build (a stat + a peek at the first record) so discovery can list
    hundreds of sessions without reading them fully.
    """

    agent: Agent
    source_session_id: str
    path: Path
    mtime: float
    size_bytes: int
    cwd: str | None = None                 # best-effort, for repo routing
    git_remote: str | None = None
    git_branch: str | None = None

    @property
    def path_str(self) -> str:
        return str(self.path)


@dataclass
class SourceSessionRef:
    """Provenance stored alongside a trajectory - never the full transcript."""

    agent: Agent
    source_session_id: str
    source_path: str
    source_hash: str
    source_mtime: float
    size_bytes: int


@dataclass
class FileTouch:
    path: str                              # repo-relative when resolvable
    was_read: bool = False
    was_created: bool = False
    was_modified: bool = False
    was_deleted: bool = False

    def merge(self, other: "FileTouch") -> None:
        self.was_read |= other.was_read
        self.was_created |= other.was_created
        self.was_modified |= other.was_modified
        self.was_deleted |= other.was_deleted


@dataclass
class NormalizedTrajectory:
    """The canonical, agent-independent representation of a session."""

    agent: Agent
    source: SourceSessionRef
    task: str | None = None
    task_source: TaskSource = TaskSource.NONE
    started_at: str | None = None
    ended_at: str | None = None
    final_status: FinalStatus = FinalStatus.UNKNOWN
    final_status_reason: str | None = None

    # Repository context (resolved by the importer, but adapters may pre-fill
    # the raw cwd / git hints).
    cwd: str | None = None
    repository_root: str | None = None
    repository_name: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    git_remote: str | None = None

    model: str | None = None
    cli_version: str | None = None
    token_usage: dict[str, Any] | None = None

    events: list[NormalizedEvent] = field(default_factory=list)
    files: dict[str, FileTouch] = field(default_factory=dict)

    # Non-fatal problems encountered while parsing (bad lines, unknown records).
    parse_warnings: list[str] = field(default_factory=list)

    # ---- construction helpers -------------------------------------------------
    def add_event(self, event: NormalizedEvent) -> NormalizedEvent:
        self.events.append(event)
        return event

    def touch_file(
        self,
        path: str | None,
        *,
        read: bool = False,
        created: bool = False,
        modified: bool = False,
        deleted: bool = False,
    ) -> None:
        if not path:
            return
        entry = self.files.get(path)
        touch = FileTouch(
            path=path,
            was_read=read,
            was_created=created,
            was_modified=modified,
            was_deleted=deleted,
        )
        if entry is None:
            self.files[path] = touch
        else:
            entry.merge(touch)

    def finalize(self) -> "NormalizedTrajectory":
        """Assign sequence numbers and derive start/end timestamps if missing.

        Event order is the adapter's stream order - these transcripts are
        append-only logs, so their on-disk order is authoritative. Timestamps
        can be absent or coarse and are only used to derive session bounds.
        """

        for i, event in enumerate(self.events, start=1):
            event.sequence = i

        stamps = sorted(e.timestamp for e in self.events if e.timestamp)
        if stamps:
            self.started_at = self.started_at or stamps[0]
            self.ended_at = self.ended_at or stamps[-1]
        return self

    # ---- convenience --------------------------------------------------------
    @property
    def files_changed(self) -> list[str]:
        return sorted(
            p
            for p, t in self.files.items()
            if t.was_created or t.was_modified or t.was_deleted
        )

    @property
    def event_count(self) -> int:
        return len(self.events)
