"""Branch coverage for the less-common record/item shapes in both adapters."""

from __future__ import annotations

import json
from pathlib import Path

from trajweave.adapters.claude import ClaudeAdapter
from trajweave.adapters.codex import CodexAdapter
from trajweave.models.enums import EventType, FinalStatus
from trajweave.models.trajectory import DiscoveredSession


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _codex_session(path: Path) -> DiscoveredSession:
    return DiscoveredSession(
        agent=CodexAdapter.agent, source_session_id="s", path=path,
        mtime=1.0, size_bytes=path.stat().st_size, cwd="/repo",
    )


def test_codex_rich_items(tmp_path):
    recs = [
        {"timestamp": "2026-09-01T10:00:00Z", "type": "session_meta",
         "payload": {"session_id": "s", "cwd": "/repo", "cli_version": "0.1"}},
        {"timestamp": "2026-09-01T10:00:01Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "build the thing"}]}}},
        {"timestamp": "2026-09-01T10:00:02Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {
                "type": "McpToolCall", "server": "notion", "tool": "fetch",
                "status": "completed", "readOnlyHint": True}}},
        {"timestamp": "2026-09-01T10:00:03Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {
                "type": "Extension", "kind": "web.search", "query": "python asyncio",
                "results": [{"x": 1}, {"x": 2}]}}},
        {"timestamp": "2026-09-01T10:00:04Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {
                "type": "SubAgentActivity", "kind": "started", "agent_path": "/root/w"}}},
        {"timestamp": "2026-09-01T10:00:05Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {
                "type": "Plan", "text": "# Plan\nStep one\nStep two"}}},
        {"timestamp": "2026-09-01T10:00:06Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {
                "type": "FileChange", "changes": {
                    "/repo/old.py": {"type": "delete", "unified_diff": "@@ -1 +0,0 @@\n-x\n"},
                    "/repo/new.py": {"type": "add", "unified_diff": "@@ -0,0 +1 @@\n+y\n"}}}}},
        {"timestamp": "2026-09-01T10:00:07Z", "type": "world_state", "payload": {"full": True, "state": {}}},
        {"timestamp": "2026-09-01T10:00:08Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {
                "type": "CommandExecution", "command": ["bash", "-lc", "npm run build"],
                "status": "failed", "exit_code": 2, "parsed_cmd": [{"type": "unknown"}]}}},
        {"timestamp": "2026-09-01T10:00:09Z", "type": "event_msg", "payload": {
            "type": "turn_aborted", "reason": "user"}},
    ]
    path = _write(tmp_path / "rollout-x.jsonl", recs)
    traj = CodexAdapter().parse(_codex_session(path), repo_root="/repo")

    kinds = {e.type for e in traj.events}
    assert EventType.TOOL_CALL in kinds       # mcp + extension + subagent
    assert EventType.ASSISTANT_MESSAGE in kinds  # plan
    assert EventType.FILE_DELETE in kinds
    assert EventType.FILE_CREATE in kinds
    assert EventType.BUILD_FAIL in kinds
    tool_names = {e.tool_name for e in traj.events if e.type == EventType.TOOL_CALL}
    assert "notion.fetch" in tool_names
    assert "extension:web.search" in tool_names
    assert "subagent" in tool_names
    # world_state must NOT produce a parse warning
    assert not any("world_state" in w for w in traj.parse_warnings)
    # real abort (work happened in the turn) -> aborted
    assert traj.final_status == FinalStatus.ABORTED


def test_codex_benign_trailing_abort_is_not_aborted(tmp_path):
    recs = [
        {"timestamp": "2026-09-01T10:00:00Z", "type": "session_meta",
         "payload": {"session_id": "s", "cwd": "/repo"}},
        {"timestamp": "2026-09-01T10:00:01Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {"type": "UserMessage",
            "content": [{"type": "text", "text": "hi"}]}}},
        {"timestamp": "2026-09-01T10:00:02Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {"type": "AgentMessage",
            "content": [{"type": "Text", "text": "Hello!"}], "phase": "final"}}},
        {"timestamp": "2026-09-01T10:00:03Z", "type": "event_msg", "payload": {
            "type": "task_complete", "last_agent_message": "Hello!"}},
        # user types a quit char, immediate abort, no work in this turn
        {"timestamp": "2026-09-01T10:00:04Z", "type": "event_msg", "payload": {
            "type": "item_completed", "item": {"type": "UserMessage",
            "content": [{"type": "text", "text": "c"}]}}},
        {"timestamp": "2026-09-01T10:00:05Z", "type": "event_msg", "payload": {"type": "turn_aborted"}},
    ]
    path = _write(tmp_path / "rollout-y.jsonl", recs)
    traj = CodexAdapter().parse(_codex_session(path), repo_root="/repo")
    assert traj.final_status != FinalStatus.ABORTED


def test_codex_response_item_fallback(tmp_path):
    """Old-format session: no item_completed stream, only response_item."""

    recs = [
        {"timestamp": "2026-08-01T10:00:00Z", "type": "session_meta",
         "payload": {"session_id": "old", "cwd": "/repo", "cli_version": "0.10"}},
        {"timestamp": "2026-08-01T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "run the tests"}]}},
        {"timestamp": "2026-08-01T10:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Running now"}]}},
        {"timestamp": "2026-08-01T10:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "c1",
                     "arguments": json.dumps({"command": ["bash", "-lc", "pytest -q"]})}},
        {"timestamp": "2026-08-01T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": "exit code: 0\nok"}},
    ]
    path = _write(tmp_path / "rollout-old.jsonl", recs)
    traj = CodexAdapter().parse(_codex_session(path), repo_root="/repo")
    assert traj.task == "run the tests"
    assert any("fallback" in w for w in traj.parse_warnings)
    assert any(e.type == EventType.TEST_PASS for e in traj.events)


def test_claude_subagent_discovery_and_sidechain(tmp_path):
    root = tmp_path / "claude"
    sid = "11111111-1111-1111-1111-111111111111"
    # main session
    _write(root / "-repo" / f"{sid}.jsonl", [
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "do it"},
         "uuid": "u1", "timestamp": "2026-09-02T15:00:00Z", "origin": {"kind": "human"},
         "cwd": "/repo", "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
        {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "tool_use", "id": "tg", "name": "Grep", "input": {"pattern": "TODO"}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}},
         "uuid": "a1", "timestamp": "2026-09-02T15:00:01Z", "cwd": "/repo",
         "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
        {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
            {"tool_use_id": "tg", "type": "tool_result", "content": "none"}]},
         "uuid": "u2", "timestamp": "2026-09-02T15:00:02Z", "toolUseResult": {"mode": "content"},
         "cwd": "/repo", "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
        # an assistant tool_use with no matching result -> flushed as unresolved
        {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "tool_use", "id": "dangling", "name": "WebFetch",
                         "input": {"url": "https://x"}}],
            "stop_reason": "tool_use", "usage": {}},
         "uuid": "a2", "timestamp": "2026-09-02T15:00:03Z", "cwd": "/repo",
         "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
    ])
    # subagent transcript
    _write(root / "-repo" / sid / "subagents" / "agent-abc123.jsonl", [
        {"type": "user", "isSidechain": True, "agentId": "agent-abc123",
         "message": {"role": "user", "content": "You are a sub-agent. Do the sub-thing."},
         "uuid": "s1", "timestamp": "2026-09-02T15:00:01Z", "cwd": "/repo",
         "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
        {"type": "assistant", "isSidechain": True, "message": {
            "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "Sub-thing done."}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}},
         "uuid": "s2", "timestamp": "2026-09-02T15:00:02Z", "cwd": "/repo",
         "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
    ])

    adapter = ClaudeAdapter(root=root)
    sessions = list(adapter.discover())
    ids = {s.source_session_id for s in sessions}
    assert sid in ids
    assert "agent-abc123" in ids  # keyed by filename, not parent sessionId

    main_sess = next(s for s in sessions if s.source_session_id == sid)
    traj = adapter.parse(main_sess, repo_root="/repo")
    assert traj.task == "do it"
    types = [e.type for e in traj.events]
    assert EventType.TOOL_CALL in types  # Grep + dangling WebFetch
    assert any(e.metadata.get("unresolved") for e in traj.events if e.type == EventType.TOOL_CALL)

    sub_sess = next(s for s in sessions if s.source_session_id == "agent-abc123")
    sub = adapter.parse(sub_sess, repo_root="/repo")
    assert sub.task == "You are a sub-agent. Do the sub-thing."
    assert sub.final_status == FinalStatus.SUCCESS


def test_claude_interrupt_then_recovery_is_not_aborted(tmp_path):
    root = tmp_path / "claude"
    sid = "22222222-2222-2222-2222-222222222222"
    _write(root / "-repo" / f"{sid}.jsonl", [
        {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "task"},
         "uuid": "u1", "timestamp": "2026-09-02T15:00:00Z", "origin": {"kind": "human"},
         "cwd": "/repo", "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
        {"type": "user", "isSidechain": False,
         "message": {"role": "user", "content": "[Request interrupted by user]"},
         "uuid": "u2", "timestamp": "2026-09-02T15:00:01Z",
         "cwd": "/repo", "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
        {"type": "assistant", "isSidechain": False, "message": {
            "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "Okay, finished the revised task."}],
            "stop_reason": "end_turn", "usage": {}},
         "uuid": "a1", "timestamp": "2026-09-02T15:00:02Z", "cwd": "/repo",
         "sessionId": sid, "version": "2.1.1", "gitBranch": "main"},
    ])
    adapter = ClaudeAdapter(root=root)
    sess = next(iter(adapter.discover()))
    traj = adapter.parse(sess, repo_root="/repo")
    assert traj.task == "task"  # the interrupt marker is not the task
    assert traj.final_status == FinalStatus.SUCCESS
