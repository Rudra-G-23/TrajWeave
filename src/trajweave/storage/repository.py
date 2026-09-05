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

    # ------------------------------------------------------------------
    # Stage 5 - experience extraction
    # ------------------------------------------------------------------
    def list_trajectories_for_extraction(
        self, project_id: str | None = None
    ) -> list[Any]:
        where = " WHERE t.project_id = ?" if project_id else ""
        params = (project_id,) if project_id else ()
        return self.db.query(
            "SELECT t.id, t.project_id, t.agent, t.final_status, "
            "       t.started_at, t.ended_at, ss.source_hash AS source_hash "
            "FROM trajectories t "
            "LEFT JOIN source_sessions ss ON ss.id = t.source_session_pk"
            f"{where} ORDER BY t.seq",
            params,
        )

    def experience_extraction_state(self) -> dict[str, str | None]:
        return {
            r["trajectory_id"]: r["source_hash"]
            for r in self.db.query(
                "SELECT trajectory_id, source_hash FROM experience_extraction_state"
            )
        }

    def set_extraction_state(self, trajectory_id: str, source_hash: str | None) -> None:
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO experience_extraction_state(trajectory_id, source_hash, extracted_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(trajectory_id) DO UPDATE SET source_hash = excluded.source_hash, "
                "extracted_at = excluded.extracted_at",
                (trajectory_id, source_hash, _now()),
            )

    def clear_experience_data(self) -> None:
        """Wipe occurrences + incremental state so ``--rebuild`` reprocesses every
        trajectory. ``experiences`` rows are left for :meth:`rebuild_experiences`
        to replace in place - that preserves human review annotations across a
        rebuild (they are keyed by ``group_key``)."""

        with self.db.transaction():
            for table in (
                "experience_evidence",
                "experience_occurrences",
                "experience_extraction_state",
            ):
                self.db.execute(f"DELETE FROM {table}")

    def _alloc_occurrence_id(self, prefix: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(CAST(SUBSTR(id, ?) AS INTEGER)), 0) AS m "
            "FROM experience_occurrences WHERE id LIKE ?",
            (len(prefix) + 1, f"{prefix}%"),
        )
        return int(row["m"]) + 1

    def replace_trajectory_occurrences(
        self, trajectory_id: str, occurrences: list[Any]
    ) -> None:
        """Delete this trajectory's real occurrences and insert the new set.

        Synthesized contradiction occurrences (prefix ``OC-``) are owned by the
        experience rebuild step, not this one, so they are left alone here.
        """

        with self.db.transaction():
            self.db.execute(
                "DELETE FROM experience_occurrences "
                "WHERE trajectory_id = ? AND classification != 'contradiction'",
                (trajectory_id,),
            )
            if not occurrences:
                return
            seq = self._alloc_occurrence_id("O-")
            now = _now()
            for occ in occurrences:
                occ.id = f"O-{seq:06d}"
                seq += 1
                self.db.execute(
                    "INSERT INTO experience_occurrences("
                    "id, trajectory_id, project_id, pattern_type, group_key, "
                    "start_sequence, end_sequence, failure_family, resolution_family, "
                    "repair_context, error_signature, classification, features_json, "
                    "dedupe_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        occ.id, occ.trajectory_id, occ.project_id, occ.pattern_type,
                        occ.group_key, occ.start_sequence, occ.end_sequence,
                        occ.failure_family, occ.resolution_family, occ.repair_context,
                        occ.error_signature, occ.classification,
                        _json(occ.features or None), occ.dedupe_key(), now,
                    ),
                )

    def load_occurrences(self, *, classifications: tuple[str, ...] | None = None) -> list[Any]:
        where = ""
        params: tuple = ()
        if classifications:
            placeholders = ",".join("?" for _ in classifications)
            where = f" WHERE classification IN ({placeholders})"
            params = classifications
        return self.db.query(
            "SELECT * FROM experience_occurrences" + where + " ORDER BY id", params
        )

    def rebuild_experiences(self, grouped: list[Any], contradictions: list[Any]) -> None:
        """Replace experiences + evidence from a freshly computed grouping.

        ``grouped`` items expose the ``GroupedExperience`` shape;
        ``contradictions`` are the synthesized ``Occurrence`` objects that need
        persisting (their ``.id`` is assigned here).
        """

        now = _now()
        with self.db.transaction():
            id_by_group = {
                r["group_key"]: r["id"]
                for r in self.db.query("SELECT id, group_key FROM experiences")
            }
            review_by_group = {
                r["group_key"]: (r["review_status"], r["reviewed_at"], r["review_note"])
                for r in self.db.query(
                    "SELECT group_key, review_status, reviewed_at, review_note FROM experiences"
                )
            }
            self.db.execute("DELETE FROM experience_evidence")
            self.db.execute("DELETE FROM experiences")
            self.db.execute(
                "DELETE FROM experience_occurrences WHERE classification = 'contradiction'"
            )

            cseq = self._alloc_occurrence_id("OC-")
            contradiction_id: dict[str, str] = {}
            for occ in contradictions:
                occ.id = f"OC-{cseq:06d}"
                cseq += 1
                contradiction_id[f"{occ.trajectory_id}|{occ.group_key}"] = occ.id
                self.db.execute(
                    "INSERT INTO experience_occurrences("
                    "id, trajectory_id, project_id, pattern_type, group_key, "
                    "start_sequence, end_sequence, failure_family, resolution_family, "
                    "repair_context, error_signature, classification, features_json, "
                    "dedupe_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        occ.id, occ.trajectory_id, occ.project_id, occ.pattern_type,
                        occ.group_key, occ.start_sequence, occ.end_sequence,
                        occ.failure_family, occ.resolution_family, occ.repair_context,
                        occ.error_signature, occ.classification,
                        _json(occ.features or None), occ.dedupe_key(), now,
                    ),
                )

            next_e = 1 + max(
                (int(v.split("-")[1]) for v in id_by_group.values() if v.startswith("E-")),
                default=0,
            )
            for exp in grouped:
                eid = id_by_group.get(exp.group_key)
                if eid is None:
                    eid = f"E-{next_e:04d}"
                    next_e += 1
                rev = review_by_group.get(exp.group_key, ("unreviewed", None, None))
                self.db.execute(
                    "INSERT INTO experiences("
                    "id, group_key, title, summary, reusable_lesson, pattern_type, "
                    "context_json, status, confidence, confidence_json, support_count, "
                    "contradiction_count, ambiguous_count, occurrence_count, project_count, "
                    "first_seen_at, last_seen_at, summary_source, review_status, "
                    "reviewed_at, review_note, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        eid, exp.group_key, exp.summary.title, exp.summary.summary,
                        exp.summary.reusable_lesson, exp.pattern_type,
                        _json(exp.summary.context or None), exp.status,
                        exp.confidence.score, _json(exp.confidence.as_dict()),
                        exp.support_count, exp.contradiction_count, exp.ambiguous_count,
                        exp.occurrence_count, exp.project_count,
                        exp.first_seen_at, exp.last_seen_at, exp.summary.source,
                        rev[0], rev[1], rev[2], now, now,
                    ),
                )
                for occ in exp.occurrences:
                    oid = occ.id or contradiction_id.get(
                        f"{occ.trajectory_id}|{occ.group_key}"
                    )
                    if not oid:
                        continue
                    self.db.execute(
                        "INSERT OR IGNORE INTO experience_evidence("
                        "experience_id, occurrence_id, relationship) VALUES (?, ?, ?)",
                        (eid, oid, occ.classification),
                    )

    def record_experience_run(self, stats: dict[str, Any]) -> int:
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO experience_runs("
                "started_at, finished_at, rebuild, project_filter, trajectories_considered, "
                "trajectories_analyzed, occurrences_found, clusters_formed, candidates_created, "
                "needs_more_evidence, llm_used, llm_tokens, runtime_seconds) "
                "VALUES (:started_at, :finished_at, :rebuild, :project_filter, "
                ":trajectories_considered, :trajectories_analyzed, :occurrences_found, "
                ":clusters_formed, :candidates_created, :needs_more_evidence, :llm_used, "
                ":llm_tokens, :runtime_seconds)",
                stats,
            )
            return int(cur.lastrowid)

    def latest_experience_run(self) -> Any:
        return self.db.query_one(
            "SELECT * FROM experience_runs ORDER BY id DESC LIMIT 1"
        )

    # ---- read side (CLI + UI) --------------------------------------
    def list_experiences(
        self, *, status: str | None = None, order: str = "confidence"
    ) -> list[Any]:
        where = " WHERE status = ?" if status else ""
        params = (status,) if status else ()
        order_sql = {
            "confidence": "confidence DESC, occurrence_count DESC, id",
            "recent": "last_seen_at DESC, id",
            "id": "id",
        }.get(order, "confidence DESC, id")
        return self.db.query(
            f"SELECT * FROM experiences{where} ORDER BY {order_sql}", params
        )

    def get_experience(self, experience_id: str) -> Any:
        return self.db.query_one(
            "SELECT * FROM experiences WHERE id = ?", (experience_id,)
        )

    def get_experience_evidence(self, experience_id: str) -> list[Any]:
        return self.db.query(
            "SELECT ev.relationship, o.id AS occurrence_id, o.trajectory_id, "
            "       o.pattern_type, o.start_sequence, o.end_sequence, o.repair_context, "
            "       o.failure_family, o.resolution_family, o.error_signature, "
            "       o.features_json, o.classification, "
            "       t.task AS task, t.agent AS agent, t.final_status AS final_status, "
            "       p.name AS project_name "
            "FROM experience_evidence ev "
            "JOIN experience_occurrences o ON o.id = ev.occurrence_id "
            "LEFT JOIN trajectories t ON t.id = o.trajectory_id "
            "LEFT JOIN projects p ON p.id = o.project_id "
            "WHERE ev.experience_id = ? "
            "ORDER BY CASE ev.relationship WHEN 'support' THEN 0 "
            "         WHEN 'ambiguous' THEN 1 ELSE 2 END, o.trajectory_id",
            (experience_id,),
        )

    def set_experience_review(
        self, experience_id: str, *, review_status: str, note: str | None
    ) -> bool:
        existing = self.get_experience(experience_id)
        if existing is None:
            return False
        with self.db.transaction():
            self.db.execute(
                "UPDATE experiences SET review_status = ?, reviewed_at = ?, "
                "review_note = ?, updated_at = ? WHERE id = ?",
                (review_status, _now(), note, _now(), experience_id),
            )
            # Stage 6 only generates for candidate Experiences that have not
            # been marked false-positive or needing more evidence.  A human
            # triage change to either excluded state invalidates its derived
            # placement set immediately; the FK cascade removes alternatives
            # and their evidence links without touching Stage 5 evidence.
            if review_status in {"false_positive", "needs_more_evidence"}:
                self.db.execute(
                    "DELETE FROM placement_proposal_sets WHERE experience_id = ?",
                    (experience_id,),
                )
        return True

    def experience_counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            row = self.db.query_one(sql)
            return int(row[0]) if row else 0

        return {
            "experiences": one("SELECT COUNT(*) FROM experiences"),
            "candidates": one("SELECT COUNT(*) FROM experiences WHERE status = 'candidate'"),
            "needs_more_evidence": one(
                "SELECT COUNT(*) FROM experiences WHERE status = 'needs_more_evidence'"
            ),
            "occurrences": one("SELECT COUNT(*) FROM experience_occurrences"),
            "false_positives": one(
                "SELECT COUNT(*) FROM experiences WHERE review_status = 'false_positive'"
            ),
        }

    # ------------------------------------------------------------------
    # Stage 6 - deterministic placement proposals
    # ------------------------------------------------------------------
    def list_placement_eligible_experiences(self) -> list[Any]:
        """Return only Stage 6-eligible Stage 5 Experiences.

        The filter intentionally honours Stage 5 human triage.  It does not
        reinterpret confidence or alter Stage 5 thresholds: an Experience is
        eligible exactly when it is a candidate and has not been marked a false
        positive or as needing more evidence.
        """

        return self.db.query(
            "SELECT * FROM experiences "
            "WHERE status = 'candidate' "
            "AND review_status NOT IN ('false_positive', 'needs_more_evidence') "
            "ORDER BY id"
        )

    def get_placement_evidence(self, experience_id: str) -> list[dict[str, Any]]:
        """Return serializable Stage 5 evidence enriched for placement.

        File paths and commands come from the underlying trajectory but are
        scoped to the occurrence's event interval where possible.  The engine
        is responsible for normalising/redacting them before proposal content
        is generated.  Returning evidence as dictionaries keeps the placement
        feature extractor independent of SQLite row objects.
        """

        rows = self.db.query(
            "SELECT ev.relationship, o.id AS occurrence_id, o.trajectory_id, "
            "       o.project_id, o.pattern_type, o.group_key, o.start_sequence, "
            "       o.end_sequence, o.failure_family, o.resolution_family, "
            "       o.repair_context, o.error_signature, o.features_json, "
            "       o.classification, t.task AS task, t.agent AS agent, "
            "       t.final_status AS final_status, t.repository_root, "
            "       p.name AS project_name, p.root AS project_root "
            "FROM experience_evidence ev "
            "JOIN experience_occurrences o ON o.id = ev.occurrence_id "
            "LEFT JOIN trajectories t ON t.id = o.trajectory_id "
            "LEFT JOIN projects p ON p.id = o.project_id "
            "WHERE ev.experience_id = ? "
            "ORDER BY CASE ev.relationship WHEN 'support' THEN 0 "
            "         WHEN 'ambiguous' THEN 1 ELSE 2 END, o.trajectory_id, o.id",
            (experience_id,),
        )
        evidence: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            # Scope evidence must come from the occurrence interval itself.
            # ``trajectory_files`` is a whole-session aggregate and would let
            # an unrelated edit manufacture a directory or extension scope.
            item["file_paths"] = [
                r["path"]
                for r in self.db.query(
                    "SELECT path FROM trajectory_events "
                    "WHERE trajectory_id = ? AND sequence BETWEEN ? AND ? "
                    "AND path IS NOT NULL ORDER BY sequence, path",
                    (row["trajectory_id"], row["start_sequence"], row["end_sequence"]),
                )
            ]
            item["commands"] = [
                r["command"]
                for r in self.db.query(
                    "SELECT command FROM trajectory_events "
                    "WHERE trajectory_id = ? AND sequence BETWEEN ? AND ? "
                    "AND command IS NOT NULL ORDER BY sequence",
                    (row["trajectory_id"], row["start_sequence"], row["end_sequence"]),
                )
            ]
            evidence.append(item)
        return evidence

    def record_placement_run(self, stats: dict[str, Any]) -> int:
        """Persist one Stage 6 generation invocation and return its id."""

        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO placement_runs("
                "started_at, finished_at, generator_version, eligible_experiences, "
                "proposal_sets_generated, runtime_seconds) "
                "VALUES (:started_at, :finished_at, :generator_version, "
                ":eligible_experiences, :proposal_sets_generated, :runtime_seconds)",
                stats,
            )
            return int(cur.lastrowid)

    def latest_placement_run(self) -> Any:
        return self.db.query_one("SELECT * FROM placement_runs ORDER BY id DESC LIMIT 1")

    def replace_placement_proposal_set(
        self,
        *,
        experience_id: str,
        source_fingerprint: str,
        generator_version: str,
        proposals: list[dict[str, Any]],
        run_id: int | None = None,
    ) -> str:
        """Atomically replace the current alternatives for one Experience.

        ``proposals`` is a serializable list with one item per placement type.
        Each item must supply ``placement_type``, ``scope_type``,
        ``proposed_content``, ``score``, ``rank``, ``feature_values``, and
        ``diagnostics``.  Optional ``id`` defaults to the stable deterministic
        ``PP-<experience id>-<placement type>``; ``scope_value``,
        ``diagnostics_text``, and ``evidence`` (occurrence ids or
        ``{occurrence_id, role}`` dictionaries) are also supported.  The
        pure engine's ``features`` / ``evidence_occurrence_ids`` aliases are
        accepted at this boundary as well.

        There is one proposal set per Experience.  Replacing it deletes stale
        alternatives and evidence links in the same transaction, preserving no
        obsolete recommendation while retaining stable set/proposal ids for
        unchanged placement types.
        """

        if self.get_experience(experience_id) is None:
            raise ValueError(f"Unknown experience: {experience_id}")
        if not source_fingerprint:
            raise ValueError("source_fingerprint is required")
        if not generator_version:
            raise ValueError("generator_version is required")
        if not proposals:
            raise ValueError("at least one placement proposal is required")

        set_id = f"PS-{experience_id}"
        allowed_types = {"ignore", "global_rule", "project_rule", "scoped_rule", "skill"}
        required = {
            "placement_type", "scope_type", "proposed_content", "score", "rank",
            "feature_values", "diagnostics",
        }
        seen_types: set[str] = set()
        seen_ranks: set[int] = set()
        seen_ids: set[str] = set()
        prepared: list[dict[str, Any]] = []
        for raw_proposal in proposals:
            # The pure placement engine names these two fields after its own
            # domain objects.  Accept those serializable aliases at the
            # persistence boundary while storing the schema's explicit names.
            proposal = dict(raw_proposal)
            if "feature_values" not in proposal and "features" in proposal:
                proposal["feature_values"] = proposal["features"]
            if "evidence" not in proposal and "evidence_occurrence_ids" in proposal:
                proposal["evidence"] = proposal["evidence_occurrence_ids"]
            missing = required - proposal.keys()
            if missing:
                raise ValueError(f"placement proposal missing fields: {sorted(missing)}")
            placement_type = str(proposal["placement_type"])
            if placement_type not in allowed_types:
                raise ValueError(f"invalid placement_type: {placement_type}")
            rank = int(proposal["rank"])
            if rank < 1:
                raise ValueError("placement proposal rank must be positive")
            if placement_type in seen_types or rank in seen_ranks:
                raise ValueError("placement proposal types and ranks must be unique")
            seen_types.add(placement_type)
            seen_ranks.add(rank)
            proposal_id = str(proposal.get("id") or f"PP-{experience_id}-{placement_type}")
            if proposal_id in seen_ids:
                raise ValueError("placement proposal ids must be unique")
            seen_ids.add(proposal_id)
            evidence = proposal.get("evidence", [])
            prepared.append({
                "id": proposal_id,
                "placement_type": placement_type,
                "scope_type": str(proposal["scope_type"]),
                "scope_value": proposal.get("scope_value"),
                "proposed_content": str(proposal["proposed_content"]),
                "score": float(proposal["score"]),
                "rank": rank,
                "feature_values": proposal["feature_values"],
                "diagnostics": proposal["diagnostics"],
                "diagnostics_text": str(proposal.get("diagnostics_text", "")),
                "evidence": evidence,
            })

        now = _now()
        with self.db.transaction():
            existing = self.db.query_one(
                "SELECT id FROM placement_proposal_sets WHERE experience_id = ?",
                (experience_id,),
            )
            if existing is None:
                self.db.execute(
                    "INSERT INTO placement_proposal_sets("
                    "id, experience_id, source_fingerprint, generator_version, run_id, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (set_id, experience_id, source_fingerprint, generator_version, run_id, now, now),
                )
            else:
                set_id = existing["id"]
                self.db.execute(
                    "UPDATE placement_proposal_sets SET source_fingerprint = ?, "
                    "generator_version = ?, run_id = ?, updated_at = ? WHERE id = ?",
                    (source_fingerprint, generator_version, run_id, now, set_id),
                )
                self.db.execute(
                    "DELETE FROM placement_proposals WHERE proposal_set_id = ?", (set_id,)
                )

            for proposal in prepared:
                self.db.execute(
                    "INSERT INTO placement_proposals("
                    "id, proposal_set_id, placement_type, scope_type, scope_value, "
                    "proposed_content, score, rank, feature_values_json, diagnostics_json, "
                    "diagnostics_text, generator_version, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal["id"], set_id, proposal["placement_type"],
                        proposal["scope_type"], proposal["scope_value"],
                        proposal["proposed_content"], proposal["score"], proposal["rank"],
                        _json(proposal["feature_values"]) or "{}",
                        _json(proposal["diagnostics"]) or "[]",
                        proposal["diagnostics_text"], generator_version, now,
                    ),
                )
                for ref in proposal["evidence"]:
                    if isinstance(ref, str):
                        occurrence_id, role = ref, "evidence"
                    elif isinstance(ref, dict) and ref.get("occurrence_id"):
                        occurrence_id, role = str(ref["occurrence_id"]), str(ref.get("role", "evidence"))
                    else:
                        raise ValueError("proposal evidence must be an occurrence id or mapping")
                    is_experience_evidence = self.db.query_one(
                        "SELECT 1 FROM experience_evidence "
                        "WHERE experience_id = ? AND occurrence_id = ?",
                        (experience_id, occurrence_id),
                    )
                    if is_experience_evidence is None:
                        raise ValueError(
                            "proposal evidence must belong to its experience: "
                            f"{occurrence_id}"
                        )
                    self.db.execute(
                        "INSERT OR IGNORE INTO placement_proposal_evidence("
                        "proposal_id, occurrence_id, role) VALUES (?, ?, ?)",
                        (proposal["id"], occurrence_id, role),
                    )
        return set_id

    def get_placement_proposal_set(self, experience_id: str) -> Any:
        return self.db.query_one(
            "SELECT ps.*, e.title AS experience_title, e.status AS experience_status, "
            "e.review_status AS experience_review_status "
            "FROM placement_proposal_sets ps JOIN experiences e ON e.id = ps.experience_id "
            "WHERE ps.experience_id = ?",
            (experience_id,),
        )

    def get_placement_proposals(self, experience_id: str) -> list[Any]:
        return self.db.query(
            "SELECT pp.*, ps.experience_id, ps.id AS proposal_set_id FROM placement_proposals pp "
            "JOIN placement_proposal_sets ps ON ps.id = pp.proposal_set_id "
            "WHERE ps.experience_id = ? ORDER BY pp.rank, pp.placement_type",
            (experience_id,),
        )

    def get_placement_proposal_evidence(self, proposal_id: str) -> list[Any]:
        return self.db.query(
            "SELECT pe.role, o.id AS occurrence_id, o.trajectory_id, o.project_id, "
            "o.classification, o.pattern_type, o.start_sequence, o.end_sequence, "
            "o.features_json, p.name AS project_name, p.root AS project_root "
            "FROM placement_proposal_evidence pe "
            "JOIN experience_occurrences o ON o.id = pe.occurrence_id "
            "LEFT JOIN projects p ON p.id = o.project_id "
            "WHERE pe.proposal_id = ? ORDER BY o.trajectory_id, o.id",
            (proposal_id,),
        )

    def list_placement_proposal_sets(
        self, *, placement_type: str | None = None, recommended: str | None = None
    ) -> list[Any]:
        """List current sets with their rank-one recommendation for CLI/UI."""

        clauses: list[str] = []
        params: list[Any] = []
        if placement_type:
            clauses.append(
                "EXISTS (SELECT 1 FROM placement_proposals typed "
                "WHERE typed.proposal_set_id = ps.id AND typed.placement_type = ?)"
            )
            params.append(placement_type)
        if recommended:
            clauses.append("recommended.placement_type = ?")
            params.append(recommended)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.db.query(
            "SELECT ps.*, e.title AS experience_title, e.confidence AS experience_confidence, "
            "recommended.id AS recommended_proposal_id, "
            "recommended.placement_type AS recommended_type, "
            "recommended.score AS recommended_score, recommended.scope_type AS recommended_scope_type, "
            "recommended.scope_value AS recommended_scope_value "
            "FROM placement_proposal_sets ps "
            "JOIN experiences e ON e.id = ps.experience_id "
            "JOIN placement_proposals recommended "
            "ON recommended.proposal_set_id = ps.id AND recommended.rank = 1"
            f"{where} ORDER BY recommended.score DESC, ps.experience_id",
            tuple(params),
        )

    def placement_counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            row = self.db.query_one(sql)
            return int(row[0]) if row else 0

        return {
            "proposal_sets": one("SELECT COUNT(*) FROM placement_proposal_sets"),
            "proposals": one("SELECT COUNT(*) FROM placement_proposals"),
        }

    # ------------------------------------------------------------------
    # Stage 7 - review and apply ledger
    # ------------------------------------------------------------------
    def get_policy_proposal_context(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT pp.*, ps.experience_id, ps.source_fingerprint, ps.generator_version, "
            "       ps.id AS proposal_set_id, e.title AS experience_title, "
            "       e.summary AS experience_summary, e.reusable_lesson, "
            "       e.pattern_type AS experience_pattern_type, p.project_root "
            "FROM placement_proposals pp "
            "JOIN placement_proposal_sets ps ON ps.id = pp.proposal_set_id "
            "JOIN experiences e ON e.id = ps.experience_id "
            "LEFT JOIN (SELECT experience_id, MAX(project_root) AS project_root "
            "           FROM (SELECT ev.experience_id, p.root AS project_root "
            "                 FROM experience_evidence ev "
            "                 JOIN experience_occurrences o ON o.id = ev.occurrence_id "
            "                 JOIN projects p ON p.id = o.project_id) GROUP BY experience_id) p "
            "ON p.experience_id = e.id WHERE pp.id = ?",
            (proposal_id,),
        )
        return dict(row) if row else None

    def get_policy_review(self, review_or_proposal_id: str) -> Any:
        return self.db.query_one(
            "SELECT * FROM policy_reviews WHERE id = ? OR selected_proposal_id = ? "
            "OR proposal_set_id = (SELECT proposal_set_id FROM placement_proposals WHERE id = ?) "
            "ORDER BY updated_at DESC LIMIT 1",
            (review_or_proposal_id, review_or_proposal_id, review_or_proposal_id),
        )

    def get_policy_review_by_set(self, proposal_set_id: str) -> Any:
        return self.db.query_one(
            "SELECT * FROM policy_reviews WHERE proposal_set_id = ?", (proposal_set_id,)
        )

    def list_policy_reviews(self, *, status: str | None = None) -> list[Any]:
        where = "WHERE COALESCE(pr.status, 'unreviewed') = ?" if status else ""
        params = (status,) if status else ()
        return self.db.query(
            "SELECT ps.id AS proposal_set_id, ps.experience_id, ps.source_fingerprint, "
            "       ps.updated_at AS proposal_updated_at, e.title AS experience_title, "
            "       recommended.id AS recommended_proposal_id, recommended.placement_type AS recommended_type, "
            "       recommended.score AS recommended_score, recommended.scope_type AS recommended_scope_type, "
            "       recommended.scope_value AS recommended_scope_value, pr.id AS review_id, "
            "       pr.status AS review_status_value, pr.selected_proposal_id, pr.target_agent, "
            "       pr.target_path, pr.source_fingerprint AS review_source_fingerprint, pr.updated_at AS review_updated_at "
            "FROM placement_proposal_sets ps JOIN experiences e ON e.id = ps.experience_id "
            "JOIN placement_proposals recommended ON recommended.proposal_set_id = ps.id AND recommended.rank = 1 "
            "LEFT JOIN policy_reviews pr ON pr.proposal_set_id = ps.id "
            f"{where} ORDER BY COALESCE(pr.updated_at, ps.updated_at) DESC, ps.experience_id",
            params,
        )

    def create_policy_review(self, context: dict[str, Any]) -> str:
        """Create the durable review snapshot for a proposal set if needed."""

        existing = self.get_policy_review_by_set(str(context["proposal_set_id"]))
        if existing:
            return str(existing["id"])
        review_id = f"RV-{context['proposal_set_id']}"
        snapshot = {key: context.get(key) for key in (
            "id", "proposal_set_id", "experience_id", "placement_type", "scope_type",
            "scope_value", "proposed_content", "score", "rank", "feature_values_json",
            "diagnostics_json", "generator_version", "source_fingerprint",
        )}
        now = _now()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO policy_reviews("
                "id, proposal_set_id, experience_id, selected_proposal_id, source_fingerprint, "
                "proposal_snapshot_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, context["proposal_set_id"], context["experience_id"], context["id"],
                 context["source_fingerprint"], _json(snapshot), now, now),
            )
            self.db.execute(
                "INSERT INTO policy_review_actions(review_id, action, from_status, to_status, "
                "proposal_id, payload_json, created_at) VALUES (?, 'created', NULL, 'unreviewed', ?, '{}', ?)",
                (review_id, context["id"], now),
            )
        return review_id

    def update_policy_review(
        self, review_id: str, *, status: str | None = None,
        selected_proposal_id: str | None = None, target_agent: str | None = None,
        target_path: str | None = None, target_scope_type: str | None = None,
        target_scope_value: str | None = None, content_override: str | None = None,
        content_revision: int | None = None, stale_reason: str | None = None,
        clear_content_override: bool = False,
        action: str, payload: dict[str, Any] | None = None,
    ) -> bool:
        existing = self.get_policy_review(review_id)
        if existing is None:
            return False
        now = _now()
        new_status = status or existing["status"]
        with self.db.transaction():
            self.db.execute(
                "UPDATE policy_reviews SET status = ?, selected_proposal_id = COALESCE(?, selected_proposal_id), "
                "target_agent = COALESCE(?, target_agent), target_path = COALESCE(?, target_path), "
                "target_scope_type = COALESCE(?, target_scope_type), target_scope_value = COALESCE(?, target_scope_value), "
                "content_override = CASE WHEN ? THEN ? WHEN ? THEN NULL ELSE content_override END, "
                "content_revision = COALESCE(?, content_revision), stale_reason = ?, updated_at = ? WHERE id = ?",
                (new_status, selected_proposal_id, target_agent, target_path, target_scope_type,
                 target_scope_value, content_override is not None, content_override, clear_content_override, content_revision,
                 stale_reason, now, review_id),
            )
            self.db.execute(
                "INSERT INTO policy_review_actions(review_id, action, from_status, to_status, proposal_id, "
                "variant_revision, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, action, existing["status"], new_status, selected_proposal_id or existing["selected_proposal_id"],
                 content_revision, _json(payload or {}), now),
            )
        return True

    def record_policy_preview(self, review_id: str, preview: dict[str, Any]) -> str:
        preview_id = f"PV-{review_id}-{preview['output_hash'][:12]}"
        now = _now()
        with self.db.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO policy_review_previews("
                "id, review_id, target_path, target_hash, output_hash, proposed_content, unified_diff, "
                "target_kind, created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (preview_id, review_id, preview["target_path"], preview.get("target_hash"),
                 preview["output_hash"], preview["proposed_content"], preview["unified_diff"],
                 preview["target_kind"], now),
            )
            self.db.execute(
                "UPDATE policy_reviews SET latest_preview_id = ?, updated_at = ? WHERE id = ?",
                (preview_id, now, review_id),
            )
        return preview_id

    def record_policy_variant(
        self, review_id: str, *, revision: int, proposal_id: str, content: str, content_hash: str
    ) -> None:
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO policy_review_variants(review_id, revision, proposal_id, content, content_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (review_id, revision, proposal_id, content, content_hash, _now()),
            )

    def get_policy_preview(self, preview_id: str) -> Any:
        return self.db.query_one("SELECT * FROM policy_review_previews WHERE id = ?", (preview_id,))

    def latest_policy_preview(self, review_id: str) -> Any:
        return self.db.query_one(
            "SELECT * FROM policy_review_previews WHERE review_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (review_id,),
        )

    def record_policy_application(self, review_id: str, result: dict[str, Any]) -> int:
        now = _now()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO policy_applications(review_id, preview_id, outcome, target_path, before_hash, "
                "after_hash, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, result.get("preview_id"), result["outcome"], result.get("target_path"),
                 result.get("before_hash"), result.get("after_hash"), result.get("detail"), now),
            )
            self.db.execute(
                "INSERT INTO policy_review_actions(review_id, action, from_status, to_status, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (review_id, "apply_" + result["outcome"], result.get("from_status"), result.get("to_status"),
                 _json(result), now),
            )
            if result["outcome"] in {"applied", "already_applied"}:
                self.db.execute(
                    "UPDATE policy_reviews SET status = 'applied', updated_at = ? WHERE id = ?",
                    (now, review_id),
                )
            return int(cur.lastrowid)

    def pending_policy_application(self, review_id: str) -> Any:
        return self.db.query_one(
            "SELECT * FROM policy_applications WHERE review_id = ? AND outcome = 'pending' "
            "ORDER BY id DESC LIMIT 1", (review_id,)
        )

    def begin_policy_application(self, review_id: str, result: dict[str, Any]) -> int:
        now = _now()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO policy_applications(review_id, preview_id, outcome, target_path, before_hash, "
                "after_hash, detail, created_at) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)",
                (review_id, result.get("preview_id"), result.get("target_path"), result.get("before_hash"),
                 result.get("after_hash"), "atomic apply started", now),
            )
            self.db.execute(
                "INSERT INTO policy_review_actions(review_id, action, from_status, to_status, payload_json, created_at) "
                "VALUES (?, 'apply_started', ?, ?, ?, ?)",
                (review_id, result.get("from_status"), result.get("from_status"), _json(result), now),
            )
            return int(cur.lastrowid)

    def finalize_policy_application(self, application_id: int, review_id: str, result: dict[str, Any]) -> None:
        now = _now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE policy_applications SET outcome = ?, target_path = ?, before_hash = ?, after_hash = ?, detail = ? "
                "WHERE id = ?", (result["outcome"], result.get("target_path"), result.get("before_hash"),
                                  result.get("after_hash"), result.get("detail"), application_id),
            )
            self.db.execute(
                "INSERT INTO policy_review_actions(review_id, action, from_status, to_status, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (review_id, "apply_" + result["outcome"], result.get("from_status"), result.get("to_status"),
                 _json(result), now),
            )
            if result["outcome"] in {"applied", "already_applied"}:
                self.db.execute(
                    "UPDATE policy_reviews SET status = 'applied', updated_at = ? WHERE id = ?",
                    (now, review_id),
                )

    def consume_policy_preview(self, preview_id: str) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE policy_review_previews SET consumed_at = ? WHERE id = ?",
                (_now(), preview_id),
            )

    def policy_review_history(self, review_id: str) -> dict[str, list[Any]]:
        return {
            "actions": self.db.query(
                "SELECT * FROM policy_review_actions WHERE review_id = ? ORDER BY id", (review_id,)
            ),
            "previews": self.db.query(
                "SELECT * FROM policy_review_previews WHERE review_id = ? ORDER BY created_at, id", (review_id,)
            ),
            "variants": self.db.query(
                "SELECT * FROM policy_review_variants WHERE review_id = ? ORDER BY revision", (review_id,)
            ),
            "applications": self.db.query(
                "SELECT * FROM policy_applications WHERE review_id = ? ORDER BY id", (review_id,)
            ),
        }

    # ------------------------------------------------------------------
    # Stage 8 - evaluation ledger
    # ------------------------------------------------------------------
    def create_evaluation_spec(self, spec: dict[str, Any]) -> None:
        now = spec.get("created_at") or _now()
        self.db.execute(
            "INSERT INTO evaluation_specs("
            "id, review_id, experience_id, proposal_id, content_revision, policy_content, "
            "policy_content_hash, placement_type, target_agent, target_override, scope_type, "
            "scope_value, repo_root, repo_commit, task_spec_json, agent_name, agent_version, "
            "model_name, model_version, reasoning_config_json, agent_config_json, "
            "agent_command_json, environment_json, verifier_json, execution_limits_json, "
            "condition_order_mode, seed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                spec["id"], spec["review_id"], spec["experience_id"], spec["proposal_id"],
                spec["content_revision"], spec["policy_content"], spec["policy_content_hash"],
                spec["placement_type"], spec["target_agent"], spec.get("target_override"),
                spec.get("scope_type"), spec.get("scope_value"), spec["repo_root"], spec["repo_commit"],
                _json(spec["task_spec"]), spec.get("agent_name"), spec.get("agent_version"),
                spec.get("model_name"), spec.get("model_version"), _json(spec.get("reasoning_config")),
                _json(spec.get("agent_config")), _json(spec.get("agent_command")),
                _json(spec["environment"]), _json(spec["verifiers"]), _json(spec.get("execution_limits")),
                spec["condition_order_mode"], spec.get("seed"), now,
            ),
        )

    def get_evaluation_spec(self, evaluation_id: str) -> Any:
        return self.db.query_one("SELECT * FROM evaluation_specs WHERE id = ?", (evaluation_id,))

    def list_evaluation_specs(self) -> list[Any]:
        return self.db.query(
            "SELECT es.*, "
            "(SELECT COUNT(*) FROM evaluation_runs r WHERE r.evaluation_id = es.id AND r.condition = 'baseline') AS repetitions, "
            "(SELECT MAX(ec.created_at) FROM evaluation_comparisons ec WHERE ec.evaluation_id = es.id) AS last_compared_at "
            "FROM evaluation_specs es ORDER BY es.created_at DESC"
        )

    def next_evaluation_repetition(self, evaluation_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(repetition_index), 0) AS v FROM evaluation_runs WHERE evaluation_id = ?",
            (evaluation_id,),
        )
        return int(row["v"]) + 1

    def record_evaluation_run(self, run: dict[str, Any]) -> None:
        now = _now()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO evaluation_runs("
                "id, evaluation_id, repetition_index, condition, order_position, status, error_reason, "
                "started_at, ended_at, duration_ms, apply_outcome, agent_exit_code, agent_timed_out, "
                "agent_stdout_excerpt, agent_stderr_excerpt, metrics_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run["id"], run["evaluation_id"], run["repetition_index"], run["condition"],
                    run["order_position"], run["status"], run.get("error_reason"),
                    run["started_at"], run.get("ended_at"), run.get("duration_ms"),
                    run.get("apply_outcome"), run.get("agent_exit_code"),
                    int(bool(run.get("agent_timed_out"))), run.get("agent_stdout_excerpt"),
                    run.get("agent_stderr_excerpt"), _json(run.get("metrics") or {}), now,
                ),
            )
            for result in run.get("verifier_results") or []:
                self.db.execute(
                    "INSERT INTO evaluation_verifier_results("
                    "run_id, checker_name, command_json, exit_code, passed, timed_out, duration_ms, "
                    "stdout_excerpt, stderr_excerpt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run["id"], result["name"], _json(result["command"]), result.get("exit_code"),
                        int(bool(result["passed"])), int(bool(result.get("timed_out"))),
                        result.get("duration_ms"), result.get("stdout_excerpt"),
                        result.get("stderr_excerpt"), now,
                    ),
                )

    def get_evaluation_run(self, run_id: str) -> Any:
        return self.db.query_one("SELECT * FROM evaluation_runs WHERE id = ?", (run_id,))

    def list_evaluation_runs(self, evaluation_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM evaluation_runs WHERE evaluation_id = ? "
            "ORDER BY repetition_index, order_position", (evaluation_id,)
        )

    def list_verifier_results(self, run_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM evaluation_verifier_results WHERE run_id = ? ORDER BY id", (run_id,)
        )

    def record_evaluation_comparison(self, comparison: dict[str, Any]) -> None:
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO evaluation_comparisons("
                "id, evaluation_id, repetition_index, baseline_run_id, candidate_run_id, outcome, "
                "invalid_reason, task_success_delta, regression_count, regression_details_json, "
                "changed_checks_json, metrics_delta_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    comparison["id"], comparison["evaluation_id"], comparison["repetition_index"],
                    comparison.get("baseline_run_id"), comparison.get("candidate_run_id"),
                    comparison["outcome"], comparison.get("invalid_reason"),
                    comparison.get("task_success_delta"), comparison.get("regression_count", 0),
                    _json(comparison.get("regression_details") or []),
                    _json(comparison.get("changed_checks") or []),
                    _json(comparison.get("metrics_delta") or {}), _now(),
                ),
            )

    def list_evaluation_comparisons(self, evaluation_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM evaluation_comparisons WHERE evaluation_id = ? ORDER BY repetition_index",
            (evaluation_id,),
        )

    def evaluation_history(self, evaluation_id: str) -> dict[str, Any] | None:
        spec = self.get_evaluation_spec(evaluation_id)
        if spec is None:
            return None
        runs = [dict(r) for r in self.list_evaluation_runs(evaluation_id)]
        for run in runs:
            run["verifier_results"] = [dict(v) for v in self.list_verifier_results(run["id"])]
        comparisons = [dict(c) for c in self.list_evaluation_comparisons(evaluation_id)]
        return {"spec": dict(spec), "runs": runs, "comparisons": comparisons}


    # ------------------------------------------------------------------
    # Stage 9 - policy lifecycle
    # ------------------------------------------------------------------
    def create_policy(
        self, policy_id: str, *, origin_review_id: str | None, origin_experience_id: str | None,
    ) -> bool:
        """Insert a new logical policy identity. Idempotent by id."""

        if self.get_policy(policy_id) is not None:
            return False
        now = _now()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO policies(id, status, current_version_id, origin_review_id, "
                "origin_experience_id, created_at, updated_at) VALUES (?, 'active', NULL, ?, ?, ?, ?)",
                (policy_id, origin_review_id, origin_experience_id, now, now),
            )
        return True

    def get_policy(self, policy_id: str) -> Any:
        return self.db.query_one("SELECT * FROM policies WHERE id = ?", (policy_id,))

    def get_policy_by_origin_review(self, review_id: str) -> Any:
        return self.db.query_one("SELECT * FROM policies WHERE origin_review_id = ?", (review_id,))

    def list_policies(self, *, status: str | None = None) -> list[Any]:
        where = "WHERE p.status = ?" if status else ""
        params = (status,) if status else ()
        return self.db.query(
            "SELECT p.*, v.version_number AS current_version_number, v.placement_type AS current_placement_type, "
            "       v.scope_type AS current_scope_type, v.scope_value AS current_scope_value, "
            "       v.content_hash AS current_content_hash, v.status AS current_version_status "
            "FROM policies p LEFT JOIN policy_versions v ON v.id = p.current_version_id "
            f"{where} ORDER BY p.updated_at DESC",
            params,
        )

    def set_policy_status(self, policy_id: str, status: str) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE policies SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), policy_id)
            )

    def set_policy_current_version(self, policy_id: str, version_id: str) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE policies SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, _now(), policy_id),
            )

    def next_policy_version_number(self, policy_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(version_number), 0) AS v FROM policy_versions WHERE policy_id = ?",
            (policy_id,),
        )
        return int(row["v"]) + 1

    def create_policy_version(self, version: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO policy_versions("
            "id, policy_id, version_number, content, content_hash, placement_type, target_agent, "
            "target_override, scope_type, scope_value, status, created_via, created_from_version_id, "
            "created_from_review_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version["id"], version["policy_id"], version["version_number"], version["content"],
                version["content_hash"], version["placement_type"], version.get("target_agent"),
                version.get("target_override"), version.get("scope_type"), version.get("scope_value"),
                version.get("status", "active"), version["created_via"], version.get("created_from_version_id"),
                version.get("created_from_review_id"), version.get("reason"), version.get("created_at") or _now(),
            ),
        )

    def get_policy_version(self, version_id: str) -> Any:
        return self.db.query_one("SELECT * FROM policy_versions WHERE id = ?", (version_id,))

    def list_policy_versions(self, policy_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM policy_versions WHERE policy_id = ? ORDER BY version_number", (policy_id,)
        )

    def set_policy_version_status(self, version_id: str, status: str) -> None:
        with self.db.transaction():
            self.db.execute("UPDATE policy_versions SET status = ? WHERE id = ?", (status, version_id))

    def record_lineage(self, *, relation: str, from_version_id: str, to_version_id: str) -> None:
        self.db.execute(
            "INSERT INTO policy_lineage(relation, from_version_id, to_version_id, created_at) VALUES (?, ?, ?, ?)",
            (relation, from_version_id, to_version_id, _now()),
        )

    def lineage_from(self, version_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM policy_lineage WHERE from_version_id = ? ORDER BY id", (version_id,)
        )

    def lineage_to(self, version_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM policy_lineage WHERE to_version_id = ? ORDER BY id", (version_id,)
        )

    def record_lifecycle_action(
        self, *, policy_id: str, version_id: str | None, action: str,
        from_status: str | None, to_status: str | None,
        recommendation_id: str | None = None, payload: dict[str, Any] | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO policy_lifecycle_actions(policy_id, version_id, action, from_status, to_status, "
            "recommendation_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (policy_id, version_id, action, from_status, to_status, recommendation_id, _json(payload or {}), _now()),
        )
        return int(cur.lastrowid)

    def list_lifecycle_actions(self, policy_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM policy_lifecycle_actions WHERE policy_id = ? ORDER BY id", (policy_id,)
        )

    def list_lifecycle_actions_for_version(self, version_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM policy_lifecycle_actions WHERE version_id = ? ORDER BY id", (version_id,)
        )

    def upsert_recommendation(self, rec: dict[str, Any]) -> str:
        """Insert a recommendation if this exact id (a deterministic hash of
        policy/version/operation/evidence) does not already exist. Returns the id."""

        existing = self.get_recommendation(rec["id"])
        if existing is not None:
            return str(existing["id"])
        now = _now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE policy_recommendations SET status = 'superseded', updated_at = ? "
                "WHERE policy_id = ? AND version_id = ? AND status = 'open'",
                (now, rec["policy_id"], rec["version_id"]),
            )
            self.db.execute(
                "INSERT INTO policy_recommendations("
                "id, policy_id, version_id, operation, reason_codes_json, explanation, evidence_json, "
                "counter_evidence_json, strength, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (
                    rec["id"], rec["policy_id"], rec["version_id"], rec["operation"],
                    _json(rec.get("reason_codes") or []), rec["explanation"], _json(rec.get("evidence") or {}),
                    _json(rec.get("counter_evidence") or []), rec.get("strength", "informational"), now, now,
                ),
            )
        return str(rec["id"])

    def get_recommendation(self, recommendation_id: str) -> Any:
        return self.db.query_one("SELECT * FROM policy_recommendations WHERE id = ?", (recommendation_id,))

    def list_recommendations(self, policy_id: str, *, status: str | None = None) -> list[Any]:
        where = "AND status = ?" if status else ""
        params = (policy_id, status) if status else (policy_id,)
        return self.db.query(
            f"SELECT * FROM policy_recommendations WHERE policy_id = ? {where} ORDER BY created_at DESC", params
        )

    def update_recommendation_status(self, recommendation_id: str, status: str) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE policy_recommendations SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), recommendation_id),
            )

    def link_version_evidence(self, version_id: str, comparison_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO policy_version_evidence(policy_version_id, evaluation_comparison_id, linked_at) "
            "VALUES (?, ?, ?)",
            (version_id, comparison_id, _now()),
        )

    def list_version_evidence(self, version_id: str) -> list[Any]:
        return self.db.query(
            "SELECT ec.* FROM policy_version_evidence pve "
            "JOIN evaluation_comparisons ec ON ec.id = pve.evaluation_comparison_id "
            "WHERE pve.policy_version_id = ? ORDER BY ec.created_at",
            (version_id,),
        )

    def comparisons_for_review(self, review_id: str) -> list[Any]:
        return self.db.query(
            "SELECT ec.* FROM evaluation_comparisons ec "
            "JOIN evaluation_specs es ON es.id = ec.evaluation_id "
            "WHERE es.review_id = ? ORDER BY ec.created_at",
            (review_id,),
        )

    def record_lifecycle_preview(self, policy_id: str, version_id: str, preview: dict[str, Any]) -> str:
        preview_id = f"LPV-{version_id}-{preview['output_hash'][:12]}"
        self.db.execute(
            "INSERT OR REPLACE INTO policy_lifecycle_previews("
            "id, policy_id, version_id, target_path, target_hash, output_hash, proposed_content, "
            "unified_diff, target_kind, created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                preview_id, policy_id, version_id, preview["target_path"], preview.get("target_hash"),
                preview["output_hash"], preview["proposed_content"], preview["unified_diff"],
                preview["target_kind"], _now(),
            ),
        )
        return preview_id

    def get_lifecycle_preview(self, preview_id: str) -> Any:
        return self.db.query_one("SELECT * FROM policy_lifecycle_previews WHERE id = ?", (preview_id,))

    def latest_lifecycle_preview(self, version_id: str) -> Any:
        return self.db.query_one(
            "SELECT * FROM policy_lifecycle_previews WHERE version_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (version_id,),
        )

    def consume_lifecycle_preview(self, preview_id: str) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE policy_lifecycle_previews SET consumed_at = ? WHERE id = ?", (_now(), preview_id)
            )

    def record_lifecycle_application(self, policy_id: str, version_id: str, result: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO policy_lifecycle_applications(policy_id, version_id, preview_id, outcome, target_path, "
            "before_hash, after_hash, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                policy_id, version_id, result.get("preview_id"), result["outcome"], result.get("target_path"),
                result.get("before_hash"), result.get("after_hash"), result.get("detail"), _now(),
            ),
        )
        return int(cur.lastrowid)

    def list_lifecycle_applications(self, policy_id: str) -> list[Any]:
        return self.db.query(
            "SELECT * FROM policy_lifecycle_applications WHERE policy_id = ? ORDER BY id", (policy_id,)
        )

    def lifecycle_history(self, policy_id: str) -> dict[str, list[Any]]:
        return {
            "versions": self.list_policy_versions(policy_id),
            "actions": self.list_lifecycle_actions(policy_id),
            "recommendations": self.list_recommendations(policy_id),
            "previews": self.db.query(
                "SELECT * FROM policy_lifecycle_previews WHERE policy_id = ? ORDER BY created_at, id", (policy_id,)
            ),
            "applications": self.list_lifecycle_applications(policy_id),
        }


def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
