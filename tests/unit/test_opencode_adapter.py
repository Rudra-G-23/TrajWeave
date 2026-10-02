from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from trajweave.adapters.opencode import OpenCodeAdapter
from trajweave.ingest.importer import Importer
from trajweave.models.enums import Agent, EventType, FinalStatus, TaskSource
from trajweave.projects.registry import ProjectRegistry
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository


def _create_store(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "opencode.db")
    conn.execute(
        "CREATE TABLE session_v2 ("
        "id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT, version TEXT, model TEXT, "
        "tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER, "
        "tokens_cache_read INTEGER, tokens_cache_write INTEGER, time_created INTEGER, "
        "time_updated INTEGER, idle_outcome TEXT)"
    )
    conn.execute(
        "CREATE TABLE session_message ("
        "id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, "
        "time_updated INTEGER, data TEXT)"
    )
    return conn


def _add_session(conn: sqlite3.Connection, session_id: str, cwd: Path, *, offset: int = 0) -> None:
    started = 1_725_200_000_000 + offset
    conn.execute(
        "INSERT INTO session_v2 VALUES (?, 'project', ?, 'Add validation', '2.0.19', "
        "'openai/gpt-5', 100, 25, 10, 5, 2, ?, ?, 'succeeded')",
        (session_id, str(cwd), started, started + 4_000),
    )
    records = [
        ("user", {"text": "Add input validation", "time": {"created": started}}),
        (
            "assistant",
            {
                "model": "openai/gpt-5",
                "content": [
                    {"type": "text", "text": "I will add validation."},
                    {
                        "type": "tool",
                        "name": "read",
                        "state": {
                            "status": "completed",
                            "input": {"path": str(cwd / "src/app.py")},
                        },
                    },
                    {
                        "type": "tool",
                        "name": "edit",
                        "state": {
                            "status": "completed",
                            "input": {
                                "path": str(cwd / "src/app.py"),
                                "oldString": "return value",
                                "newString": "return validate(value)",
                            },
                        },
                    },
                    {
                        "type": "tool",
                        "name": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pytest tests/test_app.py"},
                        },
                    },
                ],
                "time": {"created": started + 1_000},
            },
        ),
    ]
    for seq, (record_type, data) in enumerate(records, start=1):
        conn.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"{session_id}-message-{seq}",
                session_id,
                record_type,
                seq,
                started + seq * 1_000,
                started + seq * 1_000,
                json.dumps(data),
            ),
        )
    conn.commit()


def test_discovers_and_normalizes_opencode_v2_session(tmp_path):
    root = tmp_path / "opencode"
    cwd = tmp_path / "repo"
    conn = _create_store(root)
    _add_session(conn, "ses_one", cwd)
    conn.close()

    adapter = OpenCodeAdapter(root)
    sessions = list(adapter.discover())

    assert len(sessions) == 1
    assert sessions[0].agent == Agent.OPENCODE
    assert sessions[0].source_session_id == "ses_one"
    assert sessions[0].cwd == str(cwd)

    traj = adapter.parse(sessions[0], repo_root=str(cwd))

    assert traj.task == "Add input validation"
    assert traj.task_source == TaskSource.USER_PROMPT
    assert traj.model == "openai/gpt-5"
    assert traj.cli_version == "2.0.19"
    assert traj.token_usage == {
        "input_tokens": 100,
        "output_tokens": 25,
        "reasoning_output_tokens": 10,
        "cached_input_tokens": 5,
        "cache_write_tokens": 2,
    }
    assert traj.final_status == FinalStatus.SUCCESS
    assert EventType.FILE_READ in [event.type for event in traj.events]
    assert EventType.FILE_EDIT in [event.type for event in traj.events]
    assert EventType.TEST_PASS in [event.type for event in traj.events]
    assert traj.files_changed == ["src/app.py"]


def test_session_fingerprint_isolated_from_other_opencode_sessions(tmp_path):
    root = tmp_path / "opencode"
    cwd = tmp_path / "repo"
    conn = _create_store(root)
    _add_session(conn, "ses_one", cwd)
    conn.close()

    adapter = OpenCodeAdapter(root)
    session = next(adapter.discover())
    original = adapter.source_hash(session)

    conn = sqlite3.connect(root / "opencode.db")
    _add_session(conn, "ses_two", cwd, offset=10_000)
    conn.close()

    assert adapter.source_hash(session) == original


def test_import_is_idempotent_per_opencode_session(tmp_path, git_repo):
    root = tmp_path / "opencode"
    conn = _create_store(root)
    _add_session(conn, "ses_one", git_repo)
    conn.close()

    database = Database(tmp_path / "trajweave.db")
    importer = Importer(database, adapter_roots={"opencode": root})
    ProjectRegistry(importer.repo).init(git_repo)

    first = importer.run(["opencode"])
    assert first.discovered == {"opencode": 1}
    assert first.imported == 1

    conn = sqlite3.connect(root / "opencode.db")
    _add_session(conn, "ses_two", git_repo, offset=10_000)
    conn.close()

    second = importer.run(["opencode"])
    assert second.discovered == {"opencode": 2}
    assert second.imported == 1
    assert second.already_imported == 1
    trajectories = Repository(database).list_trajectories()
    assert {row["agent"] for row in trajectories} == {"opencode"}
    database.close()
