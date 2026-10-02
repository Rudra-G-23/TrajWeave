"""OpenCode v2 SQLite session adapter.

OpenCode stores session metadata and message records in ``opencode.db``. The
database is opened read-only for short queries, and each session receives its
own content fingerprint so unrelated sessions remain idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from trajweave.adapters.base import BaseAdapter
from trajweave.models.enums import Agent, EventType, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import (
    DiscoveredSession,
    NormalizedTrajectory,
    SourceSessionRef,
)
from trajweave.normalization.commands import classify_command, command_event_family
from trajweave.normalization.paths import relativize
from trajweave.normalization.redaction import redact
from trajweave.normalization.status import infer_final_status
from trajweave.normalization.text import (
    DEFAULT_COMMAND_LIMIT,
    DEFAULT_TASK_LIMIT,
    bound_text,
    strip_injected_context,
    summarize,
)
from trajweave.utils.logging import get_logger
from trajweave.utils.timeparse import to_iso

log = get_logger("adapters.opencode")

_SESSION_COLUMNS = (
    "id, directory, title, version, model, tokens_input, tokens_output, "
    "tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created, "
    "time_updated, idle_outcome"
)


class OpenCodeAdapter(BaseAdapter):
    """Normalize OpenCode v2's SQLite-backed session records."""

    agent = Agent.OPENCODE
    agent_name = "opencode"
    root_env_var = "TRAJWEAVE_OPENCODE_ROOT"

    @classmethod
    def default_root(cls) -> Path:
        data_home = os.environ.get("XDG_DATA_HOME")
        return Path(data_home) / "opencode" if data_home else Path.home() / ".local/share/opencode"

    @property
    def database_path(self) -> Path:
        return self.root if self.root.suffix == ".db" else self.root / "opencode.db"

    def discover(self) -> Iterator[DiscoveredSession]:
        database = self.database_path
        if not database.is_file():
            return
        try:
            stat = database.stat()
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM session_v2 ORDER BY time_created, id"
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            log.warning("could not inspect OpenCode store %s: %s", database, exc)
            return

        for row in rows:
            yield DiscoveredSession(
                agent=self.agent,
                source_session_id=str(row["id"]),
                path=database,
                mtime=stat.st_mtime,
                size_bytes=stat.st_size,
                cwd=_text(row["directory"]),
            )

    def source_hash(self, session: DiscoveredSession) -> str:
        """Fingerprint one session instead of the mutable database file."""

        with self._connect() as conn:
            row = self._session_row(conn, session.source_session_id)
            if row is None:
                raise ValueError(f"OpenCode session disappeared: {session.source_session_id}")
            messages = conn.execute(
                "SELECT id, type, seq, time_created, time_updated, data "
                "FROM session_message WHERE session_id = ? ORDER BY seq, id",
                (session.source_session_id,),
            ).fetchall()

        digest = hashlib.sha256(b"trajweave-opencode-v2\0")
        digest.update(_canonical_row(row))
        for message in messages:
            digest.update(_canonical_row(message))
        return digest.hexdigest()

    def parse(
        self, session: DiscoveredSession, repo_root: str | None = None
    ) -> NormalizedTrajectory:
        with self._connect() as conn:
            row = self._session_row(conn, session.source_session_id)
            if row is None:
                raise ValueError(f"OpenCode session disappeared: {session.source_session_id}")
            messages = conn.execute(
                "SELECT type, seq, time_created, data FROM session_message "
                "WHERE session_id = ? ORDER BY seq, id",
                (session.source_session_id,),
            ).fetchall()

        source = SourceSessionRef(
            agent=self.agent,
            source_session_id=session.source_session_id,
            source_path=str(self.database_path),
            source_hash=self.source_hash(session),
            source_mtime=session.mtime,
            size_bytes=session.size_bytes,
        )
        traj = NormalizedTrajectory(agent=self.agent, source=source)
        traj.cwd = _text(row["directory"]) or session.cwd
        traj.model = _text(row["model"])
        traj.cli_version = _text(row["version"])
        usage = {
            key: value
            for key, value in {
                "input_tokens": row["tokens_input"],
                "output_tokens": row["tokens_output"],
                "reasoning_output_tokens": row["tokens_reasoning"],
                "cached_input_tokens": row["tokens_cache_read"],
                "cache_write_tokens": row["tokens_cache_write"],
            }.items()
            if isinstance(value, int) and value
        }
        traj.token_usage = usage or None

        state = _ParseState(repo_root)
        for message in messages:
            timestamp = to_iso(message["time_created"])
            try:
                data = json.loads(message["data"])
            except (TypeError, json.JSONDecodeError) as exc:
                traj.parse_warnings.append(f"message {message['seq']}: invalid JSON: {exc}")
                continue
            if not isinstance(data, dict):
                traj.parse_warnings.append(f"message {message['seq']}: data is not an object")
                continue
            self._handle_message(str(message["type"]), data, timestamp, traj, state)

        if traj.task is None and _text(row["title"]):
            clean, _ = redact(_text(row["title"]))
            traj.task = bound_text(clean, DEFAULT_TASK_LIMIT)
            traj.task_source = TaskSource.AGENT_TITLE

        outcome = _text(row["idle_outcome"])
        status, reason = infer_final_status(
            traj.events,
            aborted=outcome == "interrupted",
            explicit_completion=outcome == "succeeded",
            completion_message="OpenCode reported success" if outcome == "succeeded" else None,
        )
        traj.final_status = status
        traj.final_status_reason = reason
        traj.finalize()
        return traj

    def _connect(self) -> sqlite3.Connection:
        database = self.database_path.expanduser().resolve()
        conn = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM session_v2 WHERE id = ?", (session_id,)
        ).fetchone()

    def _handle_message(
        self,
        message_type: str,
        data: dict[str, Any],
        timestamp: str | None,
        traj: NormalizedTrajectory,
        state: _ParseState,
    ) -> None:
        if message_type == "user":
            self._emit_user(_text(data.get("text")), timestamp, traj, state)
        elif message_type == "assistant":
            self._emit_assistant(data, timestamp, traj, state)

    @staticmethod
    def _emit_user(
        text: str | None,
        timestamp: str | None,
        traj: NormalizedTrajectory,
        state: _ParseState,
    ) -> None:
        human = strip_injected_context(text or "")
        if not human:
            return
        clean, was_redacted = redact(human)
        event_type = EventType.HUMAN_CORRECTION if traj.task and state.saw_work else EventType.USER_PROMPT
        if traj.task is None:
            traj.task = bound_text(clean, DEFAULT_TASK_LIMIT)
            traj.task_source = TaskSource.USER_PROMPT
        traj.add_event(NormalizedEvent(
            type=event_type,
            timestamp=timestamp,
            summary=summarize(clean),
            redacted=was_redacted,
        ))
        state.saw_work = False

    def _emit_assistant(
        self,
        data: dict[str, Any],
        timestamp: str | None,
        traj: NormalizedTrajectory,
        state: _ParseState,
    ) -> None:
        if traj.model is None:
            traj.model = _text(data.get("model"))
        content = data.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = _text(item.get("text"))
                if text:
                    clean, was_redacted = redact(text)
                    traj.add_event(NormalizedEvent(
                        type=EventType.ASSISTANT_MESSAGE,
                        timestamp=timestamp,
                        summary=summarize(clean),
                        redacted=was_redacted,
                    ))
                    state.saw_work = True
            elif item.get("type") == "tool":
                self._emit_tool(item, timestamp, traj, state)

    @staticmethod
    def _emit_tool(
        item: dict[str, Any],
        timestamp: str | None,
        traj: NormalizedTrajectory,
        state: _ParseState,
    ) -> None:
        name = _text(item.get("name")) or "unknown"
        tool_state = item.get("state") if isinstance(item.get("state"), dict) else {}
        tool_input = tool_state.get("input") if isinstance(tool_state.get("input"), dict) else {}
        status = _text(tool_state.get("status"))
        exit_code = {"completed": 0, "error": 1}.get(status)

        if name in {"bash", "shell"}:
            command = _text(tool_input.get("command"))
            kind = classify_command(command)
            clean, was_redacted = redact(command or "")
            traj.add_event(NormalizedEvent(
                type=EventType.COMMAND,
                timestamp=timestamp,
                command=bound_text(clean, DEFAULT_COMMAND_LIMIT),
                exit_code=exit_code,
                summary=f"{name} {status or 'started'}",
                redacted=was_redacted,
                metadata={"kind": str(kind), "tool": name, "status": status},
            ))
            outcome = command_event_family(kind, exit_code)
            if outcome is not None:
                traj.add_event(NormalizedEvent(
                    type=outcome,
                    timestamp=timestamp,
                    summary=f"{kind} exit {exit_code}",
                    metadata={"command_kind": str(kind), "exit_code": exit_code},
                ))
        elif name in {"read", "edit", "write"}:
            path = relativize(_text(tool_input.get("path")), state.repo_root)
            event_type = {
                "read": EventType.FILE_READ,
                "edit": EventType.FILE_EDIT,
                "write": EventType.FILE_CREATE,
            }[name]
            if exit_code == 0:
                traj.touch_file(
                    path,
                    read=name == "read",
                    modified=name == "edit",
                    created=name == "write",
                )
            traj.add_event(NormalizedEvent(
                type=event_type,
                timestamp=timestamp,
                path=path,
                summary=f"{name} {path}" if path else name,
                metadata={"tool": name, "status": status},
            ))
        else:
            traj.add_event(NormalizedEvent(
                type=EventType.TOOL_CALL,
                timestamp=timestamp,
                tool_name=name,
                summary=f"{name} {status or 'started'}",
                metadata={"status": status},
            ))
        state.saw_work = True


class _ParseState:
    def __init__(self, repo_root: str | None):
        self.repo_root = repo_root
        self.saw_work = False


def _canonical_row(row: sqlite3.Row) -> bytes:
    return json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8") + b"\0"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
