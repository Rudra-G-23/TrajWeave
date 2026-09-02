"""The project registry: explicit repository opt-in.

TrajWeave only ever ingests sessions whose repository was explicitly registered
with ``trajweave init``. Registration writes a tiny ``<repo>/.trajweave/
project.json`` marker *and* a row in the global DB. Either one is enough to keep
a repo "tracked" (so history survives the repo being deleted, and a wiped DB can
self-heal from the marker).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trajweave.config.paths import REPO_CONFIG_NAME, REPO_DIR_NAME
from trajweave.projects.git import find_repo_root, read_git_info
from trajweave.storage.repository import Repository
from trajweave.utils.hashing import stable_short_id
from trajweave.utils.logging import get_logger

log = get_logger("projects.registry")

MARKER_SCHEMA = 1


class RepoNotFoundError(RuntimeError):
    """Raised when ``trajweave init`` is run outside a recognisable repository."""


def project_id_for_root(canonical_root: str | Path) -> str:
    """Deterministic, path-derived project id (makes ``init`` idempotent)."""

    return stable_short_id("tw_proj_", str(Path(canonical_root)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _marker_path(root: Path) -> Path:
    return root / REPO_DIR_NAME / REPO_CONFIG_NAME


def find_marker_dir(start: str | Path) -> Path | None:
    """Walk up from ``start`` looking for a ``.trajweave/project.json`` marker."""

    try:
        current = Path(start).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if _marker_path(candidate).is_file():
            return candidate
    return None


@dataclass
class ResolvedProject:
    project_id: str
    name: str
    root: str
    enabled: bool
    source: str  # "db" | "marker"
    row: dict[str, Any] | None = None


class ProjectRegistry:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ------------------------------------------------------------------
    # init
    # ------------------------------------------------------------------
    def init(self, path: str | Path, name: str | None = None) -> ResolvedProject:
        start = Path(path).expanduser().resolve()
        root = find_repo_root(start)
        if root is None:
            # Allow non-git directories too, but require an explicit target dir.
            if start.is_dir():
                root = start
            else:
                raise RepoNotFoundError(
                    f"{start} is not inside a git repository; run 'trajweave init' "
                    "from a repo root."
                )

        root = root.resolve()
        project_id = project_id_for_root(root)
        resolved_name = name or root.name
        git = read_git_info(root)
        git_remote = git.remote

        marker = _marker_path(root)
        created_at = _now()
        if marker.is_file():
            try:
                existing = json.loads(marker.read_text("utf-8"))
                created_at = existing.get("created_at", created_at)
                if not name:
                    resolved_name = existing.get("name", resolved_name)
            except (json.JSONDecodeError, OSError):
                log.warning("existing marker at %s is unreadable; rewriting", marker)

        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_id": project_id,
            "name": resolved_name,
            "root": str(root),
            "git_remote": git_remote,
            "enabled": True,
            "created_at": created_at,
            "schema": MARKER_SCHEMA,
        }
        marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")

        row = self.repo.upsert_project(
            project_id=project_id,
            name=resolved_name,
            root=str(root),
            git_remote=git_remote,
            created_at=created_at,
        )
        return ResolvedProject(
            project_id=project_id,
            name=resolved_name,
            root=str(root),
            enabled=True,
            source="db",
            row=row,
        )

    # ------------------------------------------------------------------
    # resolution (import hot-path)
    # ------------------------------------------------------------------
    def resolve_for_path(self, cwd: str | None) -> ResolvedProject | None:
        """Return the tracked project a session ``cwd`` belongs to, or ``None``.

        ``None`` means "ignore this session" - the repo was never opted in.
        """

        if not cwd:
            return None

        root = find_repo_root(cwd) or find_marker_dir(cwd)
        if root is None:
            return None
        root = root.resolve()

        project_id = project_id_for_root(root)
        db_row = self.repo.get_project(project_id) or self.repo.get_project_by_root(str(root))
        if db_row is not None:
            if not int(db_row["enabled"]):
                return None
            self.repo.touch_project_seen(db_row["id"])
            return ResolvedProject(
                project_id=db_row["id"],
                name=db_row["name"],
                root=db_row["root"],
                enabled=bool(db_row["enabled"]),
                source="db",
                row=dict(db_row),
            )

        # DB has no row but an on-disk marker exists -> self-heal.
        marker = _marker_path(root)
        if marker.is_file():
            try:
                data = json.loads(marker.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            if data.get("enabled", True) is False:
                return None
            name = data.get("name") or root.name
            git_remote = data.get("git_remote")
            created_at = data.get("created_at")
            row = self.repo.upsert_project(
                project_id=data.get("project_id", project_id),
                name=name,
                root=str(root),
                git_remote=git_remote,
                created_at=created_at,
            )
            return ResolvedProject(
                project_id=row["id"],
                name=name,
                root=str(root),
                enabled=True,
                source="marker",
                row=row,
            )

        return None

    def list_projects(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.repo.list_projects()]
