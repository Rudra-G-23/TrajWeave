"""Stdlib HTTP server for the TrajWeave trajectory explorer.

Design constraints (see the Stage 4 brief):

* zero third-party dependencies - ``http.server`` + ``sqlite3`` + ``json`` only;
* GET requests are **read-only** - they open their own short-lived SQLite
  connection in ``mode=ro`` with ``PRAGMA query_only``; explicit review actions
  use separate POST requests and never write without an Apply operation;
* binds ``127.0.0.1`` only - a local inspector, never ``0.0.0.0``;
* one bad trajectory (malformed metadata, unknown event type, missing source
  file) must not break the app.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from trajweave import __version__
from trajweave.config.paths import TrajWeavePaths
from trajweave.evaluation import EvaluationError, EvaluationService
from trajweave.lifecycle.errors import LifecycleError
from trajweave.lifecycle.service import LifecycleService
from trajweave.review.service import ReviewError, ReviewService
from trajweave.review.targets import SafetyError
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository
from trajweave.utils.logging import get_logger
from trajweave.utils.timeparse import parse_timestamp

log = get_logger("ui.server")

_STATIC = resources.files("trajweave.ui").joinpath("static")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}

_MAX_PAGE = 200
_DEFAULT_PAGE = 50


class DbUnavailable(Exception):
    """Raised when the DB file is missing or its schema is incompatible."""

    def __init__(self, message: str, *, missing: bool = False):
        super().__init__(message)
        self.missing = missing


# ----------------------------------------------------------------------
# read-only DB access
# ----------------------------------------------------------------------
class _RoDb:
    """Minimal shim exposing the ``.query`` / ``.query_one`` surface that the
    read methods on :class:`Repository` need, backed by a read-only connection.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()


@contextmanager
def reader(db_path: Path) -> Iterator[Repository]:
    """Yield a :class:`Repository` over a fresh read-only connection.

    Raises :class:`DbUnavailable` if the file is absent. Schema problems surface
    later as ``sqlite3.OperationalError`` from individual queries and are handled
    per-endpoint.
    """

    if not db_path.exists():
        raise DbUnavailable(f"no database at {db_path}", missing=True)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield Repository(_RoDb(conn))
    finally:
        conn.close()


def _loads(raw: Any) -> tuple[Any, bool]:
    """Best-effort JSON decode of a stored blob. Returns ``(value, ok)``."""

    if raw is None or isinstance(raw, (dict, list)):
        return raw, True
    if not isinstance(raw, str):
        return raw, True
    try:
        return json.loads(raw), True
    except (TypeError, ValueError):
        return raw, False


def _schema_version(repo: Repository) -> int | None:
    try:
        row = repo.db.query_one(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        )
        return int(row["v"]) if row else 0
    except (AttributeError, sqlite3.OperationalError):
        return None


# ----------------------------------------------------------------------
# payload builders (pure - take a Repository, return JSON-able dicts)
# ----------------------------------------------------------------------
def build_meta(db_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "version": __version__,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "schema_version": None,
        "schema_ok": False,
        "counts": {"projects": 0, "source_sessions": 0, "trajectories": 0, "events": 0},
        "filters": {"agents": [], "statuses": []},
    }
    if not db_path.exists():
        return out
    try:
        with reader(db_path) as repo:
            version = _schema_version(repo)
            out["schema_version"] = version
            out["schema_ok"] = version is not None
            if version is not None:
                out["counts"] = repo.counts()
                out["filters"] = repo.distinct_filter_values()
    except sqlite3.OperationalError as exc:  # pragma: no cover - defensive
        log.warning("meta query failed: %s", exc)
    return out


def build_projects(repo: Repository) -> dict[str, Any]:
    return {"projects": [_project_row(r) for r in repo.list_project_summaries()]}


def build_project(repo: Repository, project_id: str) -> dict[str, Any] | None:
    row = repo.get_project_summary(project_id)
    return {"project": _project_row(row)} if row else None


def _project_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    for k in ("total_sessions", "codex_count", "claude_count"):
        d[k] = int(d.get(k) or 0)
    return d


def build_sessions(repo: Repository, params: dict[str, list[str]]) -> dict[str, Any]:
    def first(name: str) -> str | None:
        vals = params.get(name)
        return vals[0].strip() if vals and vals[0].strip() else None

    try:
        limit = int(first("limit") or _DEFAULT_PAGE)
    except ValueError:
        limit = _DEFAULT_PAGE
    try:
        offset = int(first("offset") or 0)
    except ValueError:
        offset = 0
    limit = max(1, min(_MAX_PAGE, limit))
    offset = max(0, offset)

    rows, total = repo.list_sessions_page(
        project_id=first("project"),
        agent=first("agent"),
        status=first("status"),
        query=first("q"),
        limit=limit,
        offset=offset,
    )
    return {
        "sessions": [_session_row(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _session_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["is_subagent"] = bool(d.get("is_subagent"))
    d["duration_seconds"] = _duration(d.get("started_at"), d.get("ended_at"))
    d.pop("source_path", None)  # absolute path - not for the list view
    return d


def build_trajectory(repo: Repository, trajectory_id: str) -> dict[str, Any] | None:
    row = repo.get_trajectory_detail(trajectory_id)
    if row is None:
        return None
    traj = dict(row)
    traj["is_subagent"] = bool(traj.get("is_subagent"))
    traj["duration_seconds"] = _duration(traj.get("started_at"), traj.get("ended_at"))

    warnings, ok = _loads(traj.get("parse_warnings"))
    traj["parse_warnings"] = warnings if ok else [str(traj.get("parse_warnings"))]
    token_usage, _ = _loads(traj.get("token_usage"))
    traj["token_usage"] = token_usage

    events = []
    for e in repo.get_trajectory_events(trajectory_id):
        ev = dict(e)
        ev["redacted"] = bool(ev.get("redacted"))
        meta, meta_ok = _loads(ev.get("metadata"))
        if meta_ok:
            ev["metadata"] = meta
        else:
            ev["metadata"] = None
            ev["metadata_raw"] = str(e["metadata"])
            ev["metadata_error"] = True
        events.append(ev)

    files = []
    for f in repo.get_trajectory_files(trajectory_id):
        fd = dict(f)
        for k in ("was_read", "was_created", "was_modified", "was_deleted"):
            fd[k] = bool(fd.get(k))
        files.append(fd)

    return {"trajectory": traj, "events": events, "files": files}


def build_experiences(repo: Repository, params: dict[str, list[str]]) -> dict[str, Any]:
    def first(name: str) -> str | None:
        vals = params.get(name)
        return vals[0].strip() if vals and vals[0].strip() else None

    try:
        rows = repo.list_experiences(status=first("status"), order=first("order") or "confidence")
    except (AttributeError, sqlite3.OperationalError):
        # schema v1 DB (pre Stage 5) - present as "no experiences yet".
        return {"experiences": [], "counts": {}, "run": None, "schema_ok": False}
    counts = repo.experience_counts()
    run = repo.latest_experience_run()
    return {
        "experiences": [_experience_row(r) for r in rows],
        "counts": counts,
        "run": dict(run) if run else None,
        "schema_ok": True,
    }


def build_experience(repo: Repository, experience_id: str) -> dict[str, Any] | None:
    try:
        row = repo.get_experience(experience_id)
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    exp = _experience_row(row)
    conf, ok = _loads(row["confidence_json"])
    exp["confidence_breakdown"] = conf if ok else None
    ctx, ok = _loads(row["context_json"])
    exp["context"] = ctx if ok and isinstance(ctx, list) else []

    evidence = []
    for e in repo.get_experience_evidence(experience_id):
        ed = dict(e)
        feats, fok = _loads(ed.pop("features_json", None))
        ed["features"] = feats if fok else None
        evidence.append(ed)
    # Placement is derived data introduced after Stage 5.  An absent proposal
    # is a normal read-only state - for example, before `placements generate`
    # or when there are no eligible experiences - so it must not hide the
    # underlying experience detail.
    return {
        "experience": exp,
        "evidence": evidence,
        "placement": build_placement(repo, experience_id),
    }


def _experience_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in (
        "support_count", "contradiction_count", "ambiguous_count",
        "occurrence_count", "project_count",
    ):
        d[k] = int(d.get(k) or 0)
    d["confidence"] = float(d.get("confidence") or 0.0)
    d.pop("confidence_json", None)
    d.pop("context_json", None)
    return d


def build_placements(repo: Repository, params: dict[str, list[str]]) -> dict[str, Any]:
    """Build a compact, read-only placement list.

    Placement alternatives remain available through an Experience detail, but
    this endpoint makes the current recommendation set discoverable without
    asking the browser to reimplement ranking or scoring.
    """

    def first(name: str) -> str | None:
        vals = params.get(name)
        return vals[0].strip() if vals and vals[0].strip() else None

    placement_type = first("type") or first("recommended")
    try:
        rows = repo.list_placement_proposal_sets(
            placement_type=placement_type,
            recommended=placement_type,
        )
    except (AttributeError, sqlite3.OperationalError):
        # Databases from Stage 5 and earlier remain inspectable in the UI.
        return {"placements": [], "schema_ok": False}
    return {"placements": [_placement_list_row(row) for row in rows], "schema_ok": True}


def build_placement(repo: Repository, experience_id: str) -> dict[str, Any] | None:
    """Build the full proposal/evidence trace for one Experience."""

    try:
        proposal_set = repo.get_placement_proposal_set(experience_id)
    except (AttributeError, sqlite3.OperationalError):
        return None
    if proposal_set is None:
        return None
    proposals = [_placement_row(row) for row in repo.get_placement_proposals(experience_id)]
    evidence_by_occurrence: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        for item in repo.get_placement_proposal_evidence(proposal["id"]):
            row = dict(item)
            # ``classification`` is the Stage 5 support/contradiction relation;
            # the proposal-specific role is separately preserved below.
            row["relationship"] = row.get("classification") or row.get("role")
            row["placement_role"] = row.get("role")
            # A placement reader needs the project label, not its private
            # absolute filesystem root.
            row.pop("project_root", None)
            occurrence_id = str(row.get("occurrence_id") or "")
            if occurrence_id and occurrence_id not in evidence_by_occurrence:
                evidence_by_occurrence[occurrence_id] = row
    evidence = []
    for item in evidence_by_occurrence.values():
        row = dict(item)
        features, valid = _loads(row.pop("features_json", None))
        row["features"] = features if valid else None
        evidence.append(row)
    return {"proposal_set": dict(proposal_set), "proposals": proposals, "evidence": evidence}


def build_reviews(repo: Repository, params: dict[str, list[str]]) -> dict[str, Any]:
    def first(name: str) -> str | None:
        values = params.get(name)
        return values[0].strip() if values and values[0].strip() else None

    try:
        rows = ReviewService(repo, TrajWeavePaths(Path(repo.db.path).parent if hasattr(repo.db, "path") else Path("."))).list(
            status=first("status")
        )
    except (AttributeError, sqlite3.OperationalError):
        return {"reviews": [], "schema_ok": False}
    return {"reviews": rows, "schema_ok": True}


def build_review(repo: Repository, value: str, db_path: Path) -> dict[str, Any] | None:
    try:
        payload = ReviewService(repo, TrajWeavePaths(db_path.parent)).history(value)
    except (ReviewError, AttributeError, sqlite3.OperationalError):
        return None
    return payload if payload.get("proposal") else None


def _placement_list_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    if "score" in d:
        d["score"] = float(d.get("score") or 0.0)
    if "rank" in d:
        d["rank"] = int(d.get("rank") or 0)
    if "recommended_score" in d:
        d["recommended_score"] = float(d.get("recommended_score") or 0.0)
    return d


def _placement_row(row: Any) -> dict[str, Any]:
    d = _placement_list_row(row)
    diagnostics, diagnostics_ok = _loads(d.pop("diagnostics_json", d.get("diagnostics")))
    features, features_ok = _loads(
        d.pop("feature_values_json", d.pop("features_json", d.get("features")))
    )
    d["diagnostics"] = diagnostics if diagnostics_ok and isinstance(diagnostics, list) else []
    d["features"] = features if features_ok and isinstance(features, dict) else {}
    return d


def build_evaluations(repo: Repository) -> dict[str, Any]:
    try:
        rows = repo.list_evaluation_specs()
    except (AttributeError, sqlite3.OperationalError):
        # Databases created before Stage 8 remain inspectable in the UI.
        return {"evaluations": [], "schema_ok": False}
    return {"evaluations": [dict(row) for row in rows], "schema_ok": True}


def build_evaluation(repo: Repository, evaluation_id: str, db_path: Path) -> dict[str, Any] | None:
    try:
        return EvaluationService(repo, TrajWeavePaths(db_path.parent)).history(evaluation_id)
    except (EvaluationError, AttributeError, sqlite3.OperationalError):
        return None


def build_lifecycle_policies(repo: Repository, db_path: Path) -> dict[str, Any]:
    try:
        rows = LifecycleService(repo, TrajWeavePaths(db_path.parent)).list()
    except (AttributeError, sqlite3.OperationalError):
        return {"policies": [], "schema_ok": False}
    return {"policies": rows, "schema_ok": True}


def build_lifecycle_policy(repo: Repository, policy_id: str, db_path: Path) -> dict[str, Any] | None:
    try:
        return LifecycleService(repo, TrajWeavePaths(db_path.parent)).show(policy_id)
    except (LifecycleError, AttributeError, sqlite3.OperationalError):
        return None


def build_debug_sessions(repo: Repository, params: dict[str, list[str]]) -> dict[str, Any]:
    def first(name: str) -> str | None:
        vals = params.get(name)
        return vals[0].strip() if vals and vals[0].strip() else None

    rows = repo.list_source_sessions(first("agent"), first("status"))
    out = []
    for r in rows:
        d = dict(r)
        # keep the absolute source path here (this IS the debugging view) but
        # drop nothing else - source_sessions never holds transcript content.
        out.append(d)
    return {"sessions": out}


def _duration(start: Any, end: Any) -> float | None:
    if not start or not end:
        return None
    a = parse_timestamp(str(start))
    b = parse_timestamp(str(end))
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return round(delta, 3) if delta >= 0 else None


# ----------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------
def _make_handler(db_path: Path, verbose: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"TrajWeaveUI/{__version__}"
        protocol_version = "HTTP/1.1"

        # -- logging --------------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            if verbose:
                log.info("%s - %s", self.address_string(), fmt % args)

        # -- helpers ------------------------------------------------------
        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200) -> None:
            self._send(
                json.dumps(obj, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        # -- routing ----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            try:
                self._route()
            except BrokenPipeError:  # pragma: no cover - client went away
                pass
            except Exception as exc:  # noqa: BLE001 - never 500 the whole server
                log.exception("unhandled request error")
                try:
                    self._error(500, f"internal error: {type(exc).__name__}")
                except OSError:
                    pass

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._post_route()
            except BrokenPipeError:  # pragma: no cover
                pass
            except Exception as exc:  # noqa: BLE001
                log.exception("unhandled POST error")
                self._error(500, f"internal error: {type(exc).__name__}")

        do_HEAD = do_GET

        def _route(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            params = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path in ("/app.js", "/app.css"):
                return self._static(path.lstrip("/"))
            if path == "/favicon.ico":
                return self._send(b"", "image/x-icon", 204)

            if path == "/api/meta":
                return self._json(build_meta(db_path))
            if path.startswith("/api/"):
                return self._api(path, params)

            self._error(404, "not found")

        def _post_route(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path.startswith("/api/reviews/"):
                return self._post_review(path[len("/api/reviews/"):])
            if path.startswith("/api/lifecycle/"):
                return self._post_lifecycle(path[len("/api/lifecycle/"):])
            return self._error(404, "not found")

        def _read_json_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1_000_000:
                    self._error(413, "request body too large")
                    return None
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("request body must be a JSON object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._error(400, str(exc))
                return None
            return body

        def _post_review(self, suffix: str) -> None:
            parts = suffix.split("/")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return self._error(404, "not found")
            value, action = parts
            body = self._read_json_body()
            if body is None:
                return None

            paths = TrajWeavePaths(db_path.parent).ensure()
            try:
                with Database(db_path) as db:
                    service = ReviewService(Repository(db), paths)
                    if action == "accept":
                        rid = service.accept(value, agent=body.get("agent"), target=body.get("target"))
                        result = {"review_id": rid, "status": "accepted"}
                    elif action == "reject":
                        result = {"review_id": service.reject(value), "status": "rejected"}
                    elif action == "defer":
                        result = {"review_id": service.defer(value), "status": "deferred"}
                    elif action in {"test-first", "test_first"}:
                        result = {"review_id": service.test_first(value), "status": "test_first"}
                    elif action == "edit":
                        result = {"review_id": service.edit(value, str(body.get("content") or "")), "status": "unreviewed"}
                    elif action == "choose":
                        result = {"review_id": service.choose(value, str(body.get("placement") or "")), "status": "unreviewed"}
                    elif action == "preview":
                        result = service.preview(value, target=body.get("target"), agent=body.get("agent"))
                    elif action == "apply":
                        result = service.apply(value, dry_run=bool(body.get("dry_run", False)))
                    else:
                        return self._error(404, "unknown review action")
            except ReviewError as exc:
                return self._error(409, str(exc))
            except (ValueError, SafetyError) as exc:
                return self._error(422, str(exc))
            return self._json(result)

        def _post_lifecycle(self, suffix: str) -> None:
            """Fixed whitelist of Stage 9 mutation actions. Every write still
            goes through LifecycleService, never a shortcut in this handler -
            there is no auto-apply from a GET or from page load."""

            parts = suffix.split("/")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return self._error(404, "not found")
            value, action = parts
            body = self._read_json_body()
            if body is None:
                return None

            paths = TrajWeavePaths(db_path.parent).ensure()
            try:
                with Database(db_path) as db:
                    service = LifecycleService(Repository(db), paths)
                    if action == "recommend":
                        result = service.recommend(value)
                    elif action == "rewrite":
                        vid = service.rewrite(value, str(body.get("content") or ""), reason=body.get("reason"))
                        result = {"version_id": vid}
                    elif action == "disable":
                        service.disable(value, reason=body.get("reason"))
                        result = {"policy_id": value, "status": "disabled"}
                    elif action == "enable":
                        service.enable(value, reason=body.get("reason"))
                        result = {"policy_id": value, "status": "active"}
                    elif action == "prune":
                        service.prune(value, reason=body.get("reason"))
                        result = {"policy_id": value, "status": "pruned"}
                    elif action == "rollback":
                        vid = service.rollback(value, int(body.get("version_number")), reason=body.get("reason"))
                        result = {"version_id": vid}
                    elif action == "preview":
                        result = service.preview(
                            value, project_root=body.get("project_root"),
                            target_agent=body.get("target_agent"), target_override=body.get("target"),
                        )
                    elif action == "apply":
                        result = service.apply(
                            value, project_root=body.get("project_root"), dry_run=bool(body.get("dry_run", False)),
                            target_agent=body.get("target_agent"), target_override=body.get("target"),
                            confirm_global=bool(body.get("confirm_global", False)),
                        )
                    elif action == "accept-recommendation":
                        result = {"result": service.accept_recommendation(
                            value, confirm_global=bool(body.get("confirm_global", False)),
                            target_placement=body.get("target_placement"),
                        )}
                    elif action == "reject-recommendation":
                        service.reject_recommendation(value, reason=body.get("reason"))
                        result = {"recommendation_id": value, "status": "rejected"}
                    elif action == "defer-recommendation":
                        service.defer_recommendation(value, reason=body.get("reason"))
                        result = {"recommendation_id": value, "status": "deferred"}
                    else:
                        return self._error(404, "unknown lifecycle action")
            except LifecycleError as exc:
                return self._error(409, str(exc))
            except (ValueError, SafetyError, TypeError) as exc:
                return self._error(422, str(exc))
            return self._json(result)

        def _static(self, name: str) -> None:
            try:
                data = _STATIC.joinpath(name).read_bytes()
            except (FileNotFoundError, OSError):
                return self._error(404, "not found")
            suffix = "." + name.rsplit(".", 1)[-1]
            self._send(data, _CONTENT_TYPES.get(suffix, "application/octet-stream"))

        def _api(self, path: str, params: dict[str, list[str]]) -> None:
            try:
                with reader(db_path) as repo:
                    return self._dispatch(repo, path, params)
            except DbUnavailable as exc:
                # Missing DB is an expected "empty" state for the SPA, not an error.
                if exc.missing:
                    return self._empty_api(path)
                return self._error(503, str(exc))
            except sqlite3.OperationalError as exc:
                return self._error(
                    503,
                    f"database schema is not readable ({exc}); "
                    "re-run 'trajweave import' with the current version",
                )

        def _dispatch(
            self, repo: Repository, path: str, params: dict[str, list[str]]
        ) -> None:
            if path == "/api/projects":
                return self._json(build_projects(repo))
            if path.startswith("/api/projects/"):
                pid = path[len("/api/projects/") :]
                payload = build_project(repo, pid)
                return self._json(payload) if payload else self._error(404, "no such project")
            if path == "/api/sessions":
                return self._json(build_sessions(repo, params))
            if path == "/api/experiences":
                return self._json(build_experiences(repo, params))
            if path.startswith("/api/experiences/"):
                eid = path[len("/api/experiences/") :]
                payload = build_experience(repo, eid)
                return (
                    self._json(payload)
                    if payload
                    else self._error(404, "no such experience")
                )
            if path == "/api/placements":
                return self._json(build_placements(repo, params))
            if path.startswith("/api/placements/"):
                eid = path[len("/api/placements/") :]
                payload = build_placement(repo, eid)
                return (
                    self._json(payload)
                    if payload
                    else self._error(404, "no placement proposal set for this experience")
                )
            if path == "/api/reviews":
                return self._json(build_reviews(repo, params))
            if path.startswith("/api/reviews/"):
                value = path[len("/api/reviews/"):]
                payload = build_review(repo, value, db_path)
                return self._json(payload) if payload else self._error(404, "no such review or proposal")
            if path == "/api/evals":
                return self._json(build_evaluations(repo))
            if path.startswith("/api/evals/"):
                value = path[len("/api/evals/"):]
                payload = build_evaluation(repo, value, db_path)
                return self._json(payload) if payload else self._error(404, "no such evaluation")
            if path == "/api/lifecycle":
                return self._json(build_lifecycle_policies(repo, db_path))
            if path.startswith("/api/lifecycle/"):
                value = path[len("/api/lifecycle/"):]
                payload = build_lifecycle_policy(repo, value, db_path)
                return self._json(payload) if payload else self._error(404, "no such policy")
            if path == "/api/debug/sessions":
                return self._json(build_debug_sessions(repo, params))
            if path.startswith("/api/trajectories/"):
                tid = path[len("/api/trajectories/") :]
                payload = build_trajectory(repo, tid)
                return (
                    self._json(payload)
                    if payload
                    else self._error(404, "no such trajectory")
                )
            self._error(404, "not found")

        def _empty_api(self, path: str) -> None:
            if path == "/api/projects":
                return self._json({"projects": []})
            if path == "/api/sessions":
                return self._json(
                    {"sessions": [], "total": 0, "limit": _DEFAULT_PAGE, "offset": 0}
                )
            if path == "/api/experiences":
                return self._json({"experiences": [], "counts": {}, "run": None})
            if path == "/api/placements":
                return self._json({"placements": [], "schema_ok": False})
            if path == "/api/reviews":
                return self._json({"reviews": [], "schema_ok": False})
            if path == "/api/evals":
                return self._json({"evaluations": [], "schema_ok": False})
            if path == "/api/lifecycle":
                return self._json({"policies": [], "schema_ok": False})
            if path == "/api/debug/sessions":
                return self._json({"sessions": []})
            self._error(404, "no database yet")

    return Handler


# ----------------------------------------------------------------------
# server lifecycle
# ----------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    verbose: bool = False,
) -> _Server:
    """Bind and return a server (does not serve). Raises ``OSError`` if the
    port is unavailable. Used directly by the test-suite."""

    handler = _make_handler(Path(db_path).expanduser().resolve(), verbose)
    return _Server((host, port), handler)


def _try_open_browser(url: str) -> None:
    def _open() -> None:
        time.sleep(0.4)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a headless box must not crash the server
            log.debug("could not open a browser for %s", url)

    threading.Thread(target=_open, daemon=True).start()


def serve(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    explicit_port: bool = False,
    open_browser: bool = True,
    verbose: bool = False,
) -> int:
    """Start the UI server and block until interrupted.

    If ``explicit_port`` is false and ``port`` is taken, the next free port in a
    small window is used. If it is true, an unavailable port is a hard error.
    """

    db_path = Path(db_path).expanduser().resolve()
    candidates = [port] if explicit_port else list(range(port, port + 20))
    httpd: _Server | None = None
    last_err: OSError | None = None
    for candidate in candidates:
        try:
            httpd = make_server(db_path, host=host, port=candidate, verbose=verbose)
            break
        except OSError as exc:
            last_err = exc
    if httpd is None:
        if explicit_port:
            print(
                f"trajweave ui: {host}:{port} is not available ({last_err}). "
                f"Choose another port with --port."
            )
        else:
            print(
                f"trajweave ui: no free port in {port}-{port + 19} on {host} "
                f"({last_err})."
            )
        return 2

    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}"
    print(f"\n  TrajWeave UI running at:\n    {url}\n")
    if not db_path.exists():
        print(
            f"  No database at {db_path} yet.\n"
            "  Run 'trajweave init' inside a repository, then 'trajweave import --all'.\n"
        )
    print("  Press Ctrl+C to stop.\n")

    if open_browser:
        _try_open_browser(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        httpd.server_close()
    return 0
