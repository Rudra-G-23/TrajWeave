from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own TrajWeave home - never touch the real ~/.trajweave."""

    home = tmp_path / "tw-home"
    monkeypatch.setenv("TRAJWEAVE_HOME", str(home))
    return home


@pytest.fixture
def tw_home(_isolated_home) -> Path:
    return _isolated_home


# ----------------------------------------------------------------------
# git repo helper
# ----------------------------------------------------------------------
def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path) -> Path:
    root = tmp_path / "repo-a"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "app.py").write_text("print('hi')\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def other_git_repo(tmp_path) -> Path:
    root = tmp_path / "repo-b"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# repo b\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


# ----------------------------------------------------------------------
# synthetic session builders (cwd injectable)
# ----------------------------------------------------------------------
def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def make_codex_session(
    root: Path,
    cwd: str,
    *,
    session_id: str = "11111111-2222-3333-4444-555555555555",
    filename: str | None = None,
) -> Path:
    ts = "2026-09-01T10:00:0{}.000Z"
    records = [
        {
            "timestamp": ts.format(0), "ordinal": 0, "type": "session_meta",
            "payload": {
                "session_id": session_id, "id": session_id,
                "timestamp": ts.format(0), "cwd": cwd, "cli_version": "0.149.0",
                "originator": "codex-tui",
                "git": {
                    "commit_hash": "abc123", "branch": "feat/x",
                    "repository_url": "git@github.com:acme/repo-a.git",
                },
            },
        },
        {
            "timestamp": ts.format(1), "ordinal": 1, "type": "turn_context",
            "payload": {"turn_id": "t1", "cwd": cwd, "model": "gpt-5.6-terra"},
        },
        {
            "timestamp": ts.format(1), "ordinal": 2, "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "id": "u1",
                    "content": [{"type": "text", "text": "Add coupon validation endpoint"}],
                },
            },
        },
        {
            "timestamp": ts.format(2), "ordinal": 3, "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "c1",
                    "command": ["/usr/bin/zsh", "-lc", "pytest tests/test_billing.py"],
                    "cwd": f"file://{cwd}",
                    "parsed_cmd": [{"type": "unknown"}],
                    "status": "failed",
                    "exit_code": 1,
                    "aggregated_output": "1 failed in 0.10s",
                    "duration": {"secs": 0, "nanos": 100000000},
                },
            },
        },
        {
            "timestamp": ts.format(3), "ordinal": 4, "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "id": "f1",
                    "changes": {
                        f"{cwd}/app/billing.py": {
                            "type": "update",
                            "unified_diff": "@@ -1,2 +1,4 @@\n-old\n+new1\n+new2\n",
                            "move_path": None,
                        }
                    },
                    "status": "completed",
                },
            },
        },
        {
            "timestamp": ts.format(4), "ordinal": 5, "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "c2",
                    "command": ["/usr/bin/zsh", "-lc", "pytest tests/test_billing.py"],
                    "cwd": f"file://{cwd}",
                    "parsed_cmd": [{"type": "unknown"}],
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "1 passed in 0.10s",
                    "duration": {"secs": 0, "nanos": 90000000},
                },
            },
        },
        {
            "timestamp": ts.format(5), "ordinal": 6, "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}},
            },
        },
        {
            "timestamp": ts.format(6), "ordinal": 7, "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "t1",
                "last_agent_message": "Added coupon validation endpoint and tests pass.",
                "duration_ms": 4200,
            },
        },
    ]
    name = filename or f"rollout-2026-09-01T10-00-00-{session_id}.jsonl"
    return write_jsonl(root / "2026" / "09" / "01" / name, records)


def make_claude_session(
    root: Path,
    cwd: str,
    *,
    session_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    git_branch: str = "feat/cal",
) -> Path:
    ts = "2026-09-02T15:2{}:00.000Z"
    common = {
        "cwd": cwd, "sessionId": session_id, "version": "2.1.251",
        "gitBranch": git_branch, "userType": "external", "entrypoint": "cli",
    }
    records = [
        {"type": "mode", "mode": "normal", "sessionId": session_id},
        {
            "parentUuid": None, "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": "add a readme file how to run that file"},
            "uuid": "u1", "timestamp": ts.format(0), "origin": {"kind": "human"},
            "promptSource": "typed", **common,
        },
        {"type": "ai-title", "aiTitle": "README for running file", "sessionId": session_id},
        {
            "parentUuid": "u1", "isSidechain": False, "type": "assistant",
            "message": {
                "model": "claude-sonnet-5", "role": "assistant", "id": "a1",
                "content": [{"type": "tool_use", "id": "t_read", "name": "Read",
                             "input": {"file_path": f"{cwd}/main.py"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 3, "output_tokens": 50},
            },
            "uuid": "a1", "timestamp": ts.format(1), **common,
        },
        {
            "parentUuid": "a1", "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": [
                {"tool_use_id": "t_read", "type": "tool_result", "content": "print('hi')\n"}
            ]},
            "uuid": "u2", "timestamp": ts.format(1),
            "toolUseResult": {"type": "text", "file": {
                "filePath": f"{cwd}/main.py", "content": "print('hi')\n", "numLines": 1}},
            **common,
        },
        {
            "parentUuid": "u2", "isSidechain": False, "type": "assistant",
            "message": {
                "model": "claude-sonnet-5", "role": "assistant", "id": "a2",
                "content": [{"type": "tool_use", "id": "t_bash", "name": "Bash",
                             "input": {"command": "pytest -q", "description": "run tests"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 40},
            },
            "uuid": "a2", "timestamp": ts.format(2), **common,
        },
        {
            "parentUuid": "a2", "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": [
                {"tool_use_id": "t_bash", "type": "tool_result", "content": "1 passed", "is_error": False}
            ]},
            "uuid": "u3", "timestamp": ts.format(2),
            "toolUseResult": {"stdout": "1 passed in 0.1s", "stderr": "", "interrupted": False,
                              "isImage": False, "noOutputExpected": False},
            **common,
        },
        {
            "parentUuid": "u3", "isSidechain": False, "type": "assistant",
            "message": {
                "model": "claude-sonnet-5", "role": "assistant", "id": "a3",
                "content": [{"type": "tool_use", "id": "t_write", "name": "Write",
                             "input": {"file_path": f"{cwd}/README.md", "content": "# Run\n"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 4, "output_tokens": 30},
            },
            "uuid": "a3", "timestamp": ts.format(3), **common,
        },
        {
            "parentUuid": "a3", "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": [
                {"tool_use_id": "t_write", "type": "tool_result",
                 "content": "The file README.md has been created successfully."}
            ]},
            "uuid": "u4", "timestamp": ts.format(3),
            "toolUseResult": {"type": "create", "filePath": f"{cwd}/README.md",
                              "content": "# Run\n", "structuredPatch": []},
            **common,
        },
        {
            "parentUuid": "u4", "isSidechain": False, "type": "assistant",
            "message": {
                "model": "claude-sonnet-5", "role": "assistant", "id": "a4",
                "content": [{"type": "text", "text": "Added a README with run instructions."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 6, "output_tokens": 12},
            },
            "uuid": "a4", "timestamp": ts.format(4), **common,
        },
        {"type": "last-prompt", "lastPrompt": "add a readme file how to run that file",
         "leafUuid": "a4", "sessionId": session_id},
    ]
    return write_jsonl(root / "-repo-a" / f"{session_id}.jsonl", records)
