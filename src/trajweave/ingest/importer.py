"""Historical import orchestration.

    discover  ->  determine repository  ->  opted in?
                                              |-- no  -> ignore (recorded)
                                              |-- yes -> parse -> normalize
                                                          -> dedupe -> store

Idempotent and restart-safe: a session already imported with an unchanged
content hash is skipped; a changed session is re-parsed and its trajectory
replaced in place (keeping its ``TW-`` id).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

from trajweave.adapters import ADAPTERS
from trajweave.adapters.base import BaseAdapter
from trajweave.models.trajectory import DiscoveredSession
from trajweave.projects.git import read_git_info
from trajweave.projects.registry import ProjectRegistry
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository
from trajweave.utils.hashing import file_sha256
from trajweave.utils.logging import get_logger

log = get_logger("ingest.importer")


class ImportOutcome(str, Enum):
    IMPORTED = "imported"
    REIMPORTED = "reimported"
    ALREADY_IMPORTED = "already_imported"
    IGNORED_UNREGISTERED = "ignored_unregistered"
    SKIPPED_FILTERED = "skipped_filtered"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass
class ImportStats:
    discovered: dict[str, int] = field(default_factory=dict)
    imported: int = 0
    reimported: int = 0
    already_imported: int = 0
    ignored_unregistered: int = 0
    skipped_filtered: int = 0
    failed: int = 0
    dry_run: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # (agent, path, error)

    def record(self, outcome: ImportOutcome) -> None:
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)

    def as_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "imported": self.imported,
            "reimported": self.reimported,
            "already_imported": self.already_imported,
            "ignored_unregistered": self.ignored_unregistered,
            "skipped_filtered": self.skipped_filtered,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "failures": [
                {"agent": a, "path": p, "error": e} for a, p, e in self.failures
            ],
        }


class Importer:
    def __init__(
        self,
        db: Database,
        *,
        adapter_roots: dict[str, str | Path] | None = None,
    ):
        self.db = db
        self.repo = Repository(db)
        self.registry = ProjectRegistry(self.repo)
        self._adapter_roots = adapter_roots or {}

    def _make_adapter(self, name: str) -> BaseAdapter:
        cls = ADAPTERS[name]
        return cls(self._adapter_roots.get(name))

    # ------------------------------------------------------------------
    def run(
        self,
        agents: Sequence[str] | None = None,
        *,
        project_filter: str | Path | None = None,
        dry_run: bool = False,
    ) -> ImportStats:
        agents = list(agents) if agents else list(ADAPTERS.keys())
        stats = ImportStats()
        filter_root = str(Path(project_filter).expanduser().resolve()) if project_filter else None

        for name in agents:
            if name not in ADAPTERS:
                raise ValueError(f"unknown agent {name!r}; known: {sorted(ADAPTERS)}")
            adapter = self._make_adapter(name)
            sessions = list(adapter.discover())
            stats.discovered[name] = len(sessions)
            log.info("%s: %d session(s) discovered under %s", name, len(sessions), adapter.root)
            for session in sessions:
                outcome = self._ingest_one(adapter, session, filter_root, dry_run, stats)
                log.debug("%s %s -> %s", name, session.path.name, outcome.value)

        return stats

    # ------------------------------------------------------------------
    def _ingest_one(
        self,
        adapter: BaseAdapter,
        session: DiscoveredSession,
        filter_root: str | None,
        dry_run: bool,
        stats: ImportStats,
    ) -> ImportOutcome:
        agent = adapter.agent_name
        path = str(session.path)

        try:
            content_hash = file_sha256(path)
        except OSError as exc:
            stats.failures.append((agent, path, f"unreadable: {exc}"))
            stats.record(ImportOutcome.FAILED)
            return ImportOutcome.FAILED

        existing = self.repo.get_source_session(agent, session.source_session_id, path)
        if (
            existing is not None
            and existing["status"] == "imported"
            and existing["source_hash"] == content_hash
        ):
            stats.record(ImportOutcome.ALREADY_IMPORTED)
            return ImportOutcome.ALREADY_IMPORTED

        resolved = self.registry.resolve_for_path(session.cwd)

        if resolved is None:
            self.repo.record_source_session(
                agent=agent,
                source_session_id=session.source_session_id,
                source_path=path,
                source_hash=content_hash,
                source_mtime=session.mtime,
                size_bytes=session.size_bytes,
                project_id=None,
                status="ignored_unregistered",
                detail=f"cwd not in a registered repo: {session.cwd or '(unknown)'}",
                cwd=session.cwd,
            )
            stats.record(ImportOutcome.IGNORED_UNREGISTERED)
            return ImportOutcome.IGNORED_UNREGISTERED

        if filter_root is not None and str(Path(resolved.root)) != filter_root:
            stats.record(ImportOutcome.SKIPPED_FILTERED)
            return ImportOutcome.SKIPPED_FILTERED

        if dry_run:
            stats.record(ImportOutcome.DRY_RUN)
            return ImportOutcome.DRY_RUN

        changed = existing is not None and existing["source_hash"] not in (None, content_hash)
        try:
            traj = adapter.parse(session, repo_root=resolved.root)
            traj.repository_root = resolved.root
            traj.repository_name = resolved.name
            self._augment_git(traj, resolved.root)

            # Pessimistic: mark failed first, flip to imported only after persist.
            source_pk = self.repo.record_source_session(
                agent=agent,
                source_session_id=session.source_session_id,
                source_path=path,
                source_hash=content_hash,
                source_mtime=session.mtime,
                size_bytes=session.size_bytes,
                project_id=resolved.project_id,
                status="failed",
                detail="import in progress",
                cwd=session.cwd or traj.cwd,
            )
            traj_id, replaced = self.repo.persist_trajectory(
                traj, source_pk=source_pk, project_id=resolved.project_id
            )
            self.repo.record_source_session(
                agent=agent,
                source_session_id=session.source_session_id,
                source_path=path,
                source_hash=content_hash,
                source_mtime=session.mtime,
                size_bytes=session.size_bytes,
                project_id=resolved.project_id,
                status="imported",
                detail=None,
                cwd=session.cwd or traj.cwd,
                imported=True,
            )
        except Exception as exc:  # noqa: BLE001 - one bad session must not stop the run
            log.debug("failed to import %s: %r", path, exc, exc_info=True)
            self.repo.record_source_session(
                agent=agent,
                source_session_id=session.source_session_id,
                source_path=path,
                source_hash=content_hash,
                source_mtime=session.mtime,
                size_bytes=session.size_bytes,
                project_id=resolved.project_id,
                status="failed",
                detail=f"{type(exc).__name__}: {exc}",
                cwd=session.cwd,
            )
            stats.failures.append((agent, path, f"{type(exc).__name__}: {exc}"))
            stats.record(ImportOutcome.FAILED)
            return ImportOutcome.FAILED

        outcome = ImportOutcome.REIMPORTED if (changed or replaced) else ImportOutcome.IMPORTED
        stats.record(outcome)
        return outcome

    def _augment_git(self, traj, repo_root: str) -> None:
        if traj.git_remote and traj.git_branch and traj.git_commit:
            return
        if not Path(repo_root).is_dir():
            return
        info = read_git_info(repo_root)
        traj.git_remote = traj.git_remote or info.remote
        traj.git_branch = traj.git_branch or info.branch
        traj.git_commit = traj.git_commit or info.commit


def import_all(
    db: Database,
    agents: Iterable[str] | None = None,
    **kwargs,
) -> ImportStats:
    return Importer(db).run(list(agents) if agents else None, **kwargs)
