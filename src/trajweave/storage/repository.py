"""High-level persistence operations over :class:`Database`.

All writes are idempotent: re-running an import must not create duplicate
projects, sessions, trajectories, or events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from trajweave.models.trajectory import NormalizedTrajectory
from trajweave.storage.database import Database
from trajweave.utils.logging import get_logger

log = get_logger("storage.repository")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str,)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return json.dumps(str(value))


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------
    def upsert_project(
        self,
        *,
        project_id: str,
        name: str,
        root: str,
        git_remote: str | None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        existing = self.db.query_one(
            "SELECT * FROM projects WHERE id = ? OR root = ?", (project_id, root)
        )
        with self.db.transaction():
            if existing is None:
                self.db.execute(
                    "INSERT INTO projects(id, name, root, git_remote, created_at, last_seen_at, enabled, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, 'active')",
                    (project_id, name, root, git_remote, created_at or now, now),
                )
            else:
                self.db.execute(
                    "UPDATE projects SET name = ?, git_remote = COALESCE(?, git_remote), "
                    "last_seen_at = ?, status = CASE WHEN status = 'archived' THEN status ELSE 'active' END "
                    "WHERE id = ?",
                    (name, git_remote, now, existing["id"]),
                )
        return dict(self.get_project(project_id) or self.get_project_by_root(root))

    def get_project(self, project_id: str) -> Any:
        return self.db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def get_project_by_root(self, root: str) -> Any:
        return self.db.query_one("SELECT * FROM projects WHERE root = ?", (root,))

    def list_projects(self) -> list[Any]:
        return self.db.query(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM trajectories t WHERE t.project_id = p.id) AS trajectory_count "
            "FROM projects p ORDER BY p.created_at"
        )

    def touch_project_seen(self, project_id: str) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE projects SET last_seen_at = ? WHERE id = ?", (_now(), project_id)
            )

    def set_project_status(self, project_id: str, status: str) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE projects SET status = ? WHERE id = ?", (status, project_id)
            )

    def refresh_missing_projects(self, path_exists: Any) -> list[str]:
        """Mark projects whose root no longer exists as ``missing`` (non-destructive)."""

        changed: list[str] = []
        for row in self.db.query("SELECT id, root, status FROM projects"):
            exists = path_exists(row["root"])
            if not exists and row["status"] == "active":
                self.set_project_status(row["id"], "missing")
                changed.append(row["id"])
            elif exists and row["status"] == "missing":
                self.set_project_status(row["id"], "active")
                changed.append(row["id"])
        return changed

    # ------------------------------------------------------------------
    # source sessions
    # ------------------------------------------------------------------
    def get_source_session(
        self, agent: str, source_session_id: str, source_path: str
    ) -> Any:
        return self.db.query_one(
            "SELECT * FROM source_sessions WHERE agent = ? AND source_session_id = ? AND source_path = ?",
            (agent, source_session_id, source_path),
        )

    def record_source_session(
        self,
        *,
        agent: str,
        source_session_id: str,
        source_path: str,
        source_hash: str | None,
        source_mtime: float | None,
        size_bytes: int | None,
        project_id: str | None,
        status: str,
        detail: str | None,
        cwd: str | None,
        imported: bool = False,
    ) -> int:
        now = _now()
        existing = self.get_source_session(agent, source_session_id, source_path)
        with self.db.transaction():
            if existing is None:
                cur = self.db.execute(
                    "INSERT INTO source_sessions("
                    "agent, source_session_id, source_path, source_hash, source_mtime, size_bytes, "
                    "project_id, status, detail, cwd, first_seen_at, last_imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        agent, source_session_id, source_path, source_hash, source_mtime,
                        size_bytes, project_id, status, detail, cwd, now,
                        now if imported else None,
                    ),
                )
                return int(cur.lastrowid)
            self.db.execute(
                "UPDATE source_sessions SET source_hash = ?, source_mtime = ?, size_bytes = ?, "
                "project_id = ?, status = ?, detail = ?, cwd = ?, "
                "last_imported_at = CASE WHEN ? THEN ? ELSE last_imported_at END "
                "WHERE id = ?",
                (
                    source_hash, source_mtime, size_bytes, project_id, status, detail, cwd,
                    1 if imported else 0, now, existing["id"],
                ),
            )
            return int(existing["id"])

    def list_source_sessions(
        self, agent: str | None = None, status: str | None = None
    ) -> list[Any]:
        clauses = []
        params: list[Any] = []
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self.db.query(
            f"SELECT * FROM source_sessions{where} ORDER BY first_seen_at DESC", tuple(params)
        )

    # ------------------------------------------------------------------
    # trajectories
    # ------------------------------------------------------------------
    def _allocate_trajectory_id(self, source_pk: int) -> tuple[str, int, bool]:
        existing = self.db.query_one(
            "SELECT id, seq FROM trajectories WHERE source_session_pk = ?", (source_pk,)
        )
        if existing is not None:
            return existing["id"], int(existing["seq"]), True
        row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) AS m FROM trajectories")
        seq = int(row["m"]) + 1
        return f"TW-{seq:06d}", seq, False

    def persist_trajectory(
        self,
        traj: NormalizedTrajectory,
        *,
        source_pk: int,
        project_id: str | None,
    ) -> tuple[str, bool]:
        """Insert (or replace) the trajectory for ``source_pk``.

        Returns ``(trajectory_id, replaced)``.
        """

        traj.finalize()
        with self.db.transaction():
            traj_id, seq, replaced = self._allocate_trajectory_id(source_pk)
            if replaced:
                # CASCADE removes events + files.
                self.db.execute("DELETE FROM trajectories WHERE id = ?", (traj_id,))

            self.db.execute(
                "INSERT INTO trajectories("
                "id, seq, source_session_pk, project_id, agent, task, task_source, "
                "started_at, ended_at, final_status, final_status_reason, "
                "repository_name, repository_root, git_branch, git_commit, git_remote, "
                "model, cli_version, token_usage, event_count, parse_warnings, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    traj_id, seq, source_pk, project_id, str(traj.agent),
                    traj.task, str(traj.task_source),
                    traj.started_at, traj.ended_at,
                    str(traj.final_status), traj.final_status_reason,
                    traj.repository_name, traj.repository_root,
                    traj.git_branch, traj.git_commit, traj.git_remote,
                    traj.model, traj.cli_version, _json(traj.token_usage),
                    traj.event_count, _json(traj.parse_warnings or None), _now(),
                ),
            )

            self.db.conn.executemany(
                "INSERT INTO trajectory_events("
                "trajectory_id, sequence, type, timestamp, path, command, exit_code, "
                "tool_name, summary, metadata, redacted) "
                "VALUES (:trajectory_id, :sequence, :type, :timestamp, :path, :command, "
                ":exit_code, :tool_name, :summary, :metadata, :redacted)",
                [
                    {
                        "trajectory_id": traj_id,
                        **{
                            **row,
                            "metadata": _json(row["metadata"]),
                        },
                    }
                    for row in (e.to_row() for e in traj.events)
                ],
            )

            self.db.conn.executemany(
                "INSERT INTO trajectory_files("
                "trajectory_id, path, was_read, was_created, was_modified, was_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        traj_id, f.path,
                        int(f.was_read), int(f.was_created),
                        int(f.was_modified), int(f.was_deleted),
                    )
                    for f in traj.files.values()
                ],
            )
        return traj_id, replaced

    # ------------------------------------------------------------------
    # read side (CLI)
    # ------------------------------------------------------------------
    def list_trajectories(
        self,
        *,
        project_id: str | None = None,
        agent: str | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        clauses = []
        params: list[Any] = []
        if project_id:
            clauses.append("t.project_id = ?")
            params.append(project_id)
        if agent:
            clauses.append("t.agent = ?")
            params.append(agent)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT t.*, p.name AS project_name FROM trajectories t "
            "LEFT JOIN projects p ON p.id = t.project_id"
            f"{where} ORDER BY t.seq DESC"
        )
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def get_trajectory(self, trajectory_id: str) -> Any:
        return self.db.query_one(
            "SELECT t.*, p.name AS project_name FROM trajectories t "
            "LEFT JOIN projects p ON p.id = t.project_id WHERE t.id = ?",
            (trajectory_id,),
        )

    def get_trajectory_events(self, trajectory_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM trajectory_events WHERE trajectory_id = ? ORDER BY sequence",
            (trajectory_id,),
        )

    def get_trajectory_files(self, trajectory_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM trajectory_files WHERE trajectory_id = ? ORDER BY path",
            (trajectory_id,),
        )

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            row = self.db.query_one(sql)
            return int(row[0]) if row else 0

        return {
            "projects": one("SELECT COUNT(*) FROM projects"),
            "source_sessions": one("SELECT COUNT(*) FROM source_sessions"),
            "trajectories": one("SELECT COUNT(*) FROM trajectories"),
            "events": one("SELECT COUNT(*) FROM trajectory_events"),
        }

    # ------------------------------------------------------------------
    # read side (UI) - aggregates + filtered/paginated listing
    # ------------------------------------------------------------------
    def list_project_summaries(self) -> list[Any]:
        """One row per project with its trajectory aggregates.

        Projects with no imported trajectories are still returned (LEFT JOIN),
        so a freshly ``init``-ed but not-yet-imported repo is visible.
        """

        return self.db.query(
            "SELECT p.id, p.name, p.root, p.git_remote, p.status, p.enabled, "
            "       p.created_at, p.last_seen_at, "
            "       COUNT(t.id)                                        AS total_sessions, "
            "       COALESCE(SUM(t.agent = 'codex'), 0)                AS codex_count, "
            "       COALESCE(SUM(t.agent = 'claude'), 0)               AS claude_count, "
            "       MAX(t.started_at)                                  AS last_activity "
            "FROM projects p "
            "LEFT JOIN trajectories t ON t.project_id = p.id "
            "GROUP BY p.id "
            "ORDER BY p.name COLLATE NOCASE"
        )

    def get_project_summary(self, project_id: str) -> Any:
        return self.db.query_one(
            "SELECT p.id, p.name, p.root, p.git_remote, p.status, p.enabled, "
            "       p.created_at, p.last_seen_at, "
            "       COUNT(t.id)                                        AS total_sessions, "
            "       COALESCE(SUM(t.agent = 'codex'), 0)                AS codex_count, "
            "       COALESCE(SUM(t.agent = 'claude'), 0)               AS claude_count, "
            "       MAX(t.started_at)                                  AS last_activity "
            "FROM projects p "
            "LEFT JOIN trajectories t ON t.project_id = p.id "
            "WHERE p.id = ? "
            "GROUP BY p.id",
            (project_id,),
        )

    _SESSION_COLUMNS = (
        "t.id, t.agent, t.task, t.task_source, t.final_status, t.final_status_reason, "
        "t.started_at, t.ended_at, t.event_count, t.model, t.project_id, "
        "p.name AS project_name, "
        "ss.source_path AS source_path, "
        "CASE WHEN ss.source_path LIKE '%/subagents/%' THEN 1 ELSE 0 END AS is_subagent"
    )

    def _session_where(
        self,
        *,
        project_id: str | None,
        agent: str | None,
        status: str | None,
        query: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("t.project_id = ?")
            params.append(project_id)
        if agent:
            clauses.append("t.agent = ?")
            params.append(agent)
        if status:
            clauses.append("t.final_status = ?")
            params.append(status)
        if query:
            like = f"%{query.strip()}%"
            clauses.append("(t.task LIKE ? OR t.id LIKE ?)")
            params.extend((like, like))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def list_sessions_page(
        self,
        *,
        project_id: str | None = None,
        agent: str | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Any], int]:
        """Return ``(rows, total)`` for the Sessions list, newest first."""

        where, params = self._session_where(
            project_id=project_id, agent=agent, status=status, query=query
        )
        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM trajectories t "
            f"LEFT JOIN source_sessions ss ON ss.id = t.source_session_pk{where}",
            tuple(params),
        )
        total = int(total_row["n"]) if total_row else 0

        rows = self.db.query(
            f"SELECT {self._SESSION_COLUMNS} FROM trajectories t "
            f"LEFT JOIN projects p ON p.id = t.project_id "
            f"LEFT JOIN source_sessions ss ON ss.id = t.source_session_pk"
            f"{where} ORDER BY t.seq DESC LIMIT ? OFFSET ?",
            (*params, max(1, int(limit)), max(0, int(offset))),
        )
        return rows, total

    def get_trajectory_detail(self, trajectory_id: str) -> Any:
        """Full trajectory row plus source-session provenance (for the UI)."""

        return self.db.query_one(
            "SELECT t.*, p.name AS project_name, p.status AS project_status, "
            "       ss.source_path AS source_path, "
            "       ss.source_session_id AS source_session_id, "
            "       ss.source_mtime AS source_mtime, "
            "       ss.cwd AS source_cwd, "
            "       CASE WHEN ss.source_path LIKE '%/subagents/%' THEN 1 ELSE 0 END AS is_subagent "
            "FROM trajectories t "
            "LEFT JOIN projects p ON p.id = t.project_id "
            "LEFT JOIN source_sessions ss ON ss.id = t.source_session_pk "
            "WHERE t.id = ?",
            (trajectory_id,),
        )

    def distinct_filter_values(self) -> dict[str, list[str]]:
        agents = [
            r["agent"]
            for r in self.db.query("SELECT DISTINCT agent FROM trajectories ORDER BY agent")
        ]
        statuses = [
            r["final_status"]
            for r in self.db.query(
                "SELECT DISTINCT final_status FROM trajectories ORDER BY final_status"
            )
        ]
        return {"agents": agents, "statuses": statuses}


def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
