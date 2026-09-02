from __future__ import annotations

from pathlib import Path

from trajweave.adapters.claude import ClaudeAdapter
from trajweave.models.enums import EventType, FinalStatus, TaskSource

FIXTURES = Path(__file__).parent.parent / "fixtures" / "claude"


def _parse(name_contains: str, repo_root="/work/repo-a"):
    adapter = ClaudeAdapter(root=FIXTURES)
    sessions = list(adapter.discover())
    match = next(s for s in sessions if name_contains in s.path.name)
    return adapter.parse(match, repo_root=repo_root)


def test_discover():
    adapter = ClaudeAdapter(root=FIXTURES)
    names = {s.path.name for s in adapter.discover()}
    assert any("dddddddd" in n for n in names)


def test_parse_sample_session():
    traj = _parse("dddddddd")
    assert traj.task == "add a readme file how to run that file"
    assert traj.task_source == TaskSource.USER_PROMPT
    assert traj.model == "claude-sonnet-5"
    assert traj.cli_version == "2.1.251"
    assert traj.git_branch == "feat/cal"
    assert traj.token_usage["input_tokens"] == 3 + 5 + 4 + 6

    types = [e.type for e in traj.events]
    assert EventType.USER_PROMPT in types
    assert EventType.FILE_READ in types
    assert EventType.COMMAND in types
    assert EventType.TEST_PASS in types
    assert EventType.FILE_CREATE in types
    assert EventType.ASSISTANT_MESSAGE in types

    assert [e.sequence for e in traj.events] == list(range(1, len(traj.events) + 1))

    reads = [e for e in traj.events if e.type == EventType.FILE_READ]
    assert reads[0].path == "main.py"
    assert "README.md" in traj.files_changed

    # clean end_turn with final text -> success
    assert traj.final_status == FinalStatus.SUCCESS


def test_malformed_session_is_tolerated():
    traj = _parse("session", repo_root="/work/repo-a")  # malformed/session.jsonl
    assert traj.parse_warnings  # broken json line captured
    assert traj.task == "fix the parser"
    # npm test failed -> test_fail, and final text says "needs more work"
    assert any(e.type == EventType.TEST_FAIL for e in traj.events)
    assert traj.final_status in (FinalStatus.PARTIAL, FinalStatus.FAILURE)


def test_bash_error_maps_to_nonzero_exit():
    traj = _parse("session", repo_root="/work/repo-a")
    cmds = [e for e in traj.events if e.type == EventType.COMMAND]
    assert cmds and cmds[0].exit_code == 1
