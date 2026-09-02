from __future__ import annotations

from pathlib import Path

from trajweave.adapters.codex import CodexAdapter
from trajweave.models.enums import EventType, FinalStatus, TaskSource

FIXTURES = Path(__file__).parent.parent / "fixtures" / "codex"


def _parse_fixture(name: str, repo_root="/work/repo-a"):
    adapter = CodexAdapter(root=FIXTURES)
    sessions = {s.path.name: s for s in adapter.discover()}
    return adapter.parse(sessions[name], repo_root=repo_root)


def test_discover_finds_rollout_files():
    adapter = CodexAdapter(root=FIXTURES)
    names = {s.path.name for s in adapter.discover()}
    assert "rollout-2026-09-01T10-00-00-sample.jsonl" in names
    for s in adapter.discover():
        if s.path.name.endswith("sample.jsonl"):
            assert s.cwd == "/work/repo-a"
            assert s.source_session_id == "cafecafe-0000-1111-2222-333344445555"
            assert s.git_remote == "git@github.com:acme/repo-a.git"


def test_parse_sample_session():
    traj = _parse_fixture("rollout-2026-09-01T10-00-00-sample.jsonl")
    assert traj.task == "Add coupon validation endpoint"
    assert traj.task_source == TaskSource.USER_PROMPT
    assert traj.model == "gpt-5.6-terra"
    assert traj.cli_version == "0.149.0"
    assert traj.git_branch == "feat/x"
    assert traj.token_usage and traj.token_usage["total_tokens"] == 120

    types = [e.type for e in traj.events]
    assert EventType.USER_PROMPT in types
    assert EventType.TEST_FAIL in types
    assert EventType.FILE_EDIT in types
    assert EventType.TEST_PASS in types
    assert EventType.COMPLETION in types

    # sequence numbers are 1..N in stream order
    assert [e.sequence for e in traj.events] == list(range(1, len(traj.events) + 1))

    # relative path normalization
    edits = [e for e in traj.events if e.type == EventType.FILE_EDIT]
    assert edits[0].path == "app/billing.py"
    assert traj.files_changed == ["app/billing.py"]

    # test failed then passed then explicit completion -> success
    assert traj.final_status == FinalStatus.SUCCESS


def test_parse_is_relative_to_repo_root_argument(tmp_path):
    traj = _parse_fixture("rollout-2026-09-01T10-00-00-sample.jsonl", repo_root="/somewhere/else")
    edits = [e for e in traj.events if e.type == EventType.FILE_EDIT]
    # path is outside the given root, so it stays absolute (cleaned)
    assert edits[0].path == "/work/repo-a/app/billing.py"


def test_malformed_session_is_tolerated():
    traj = _parse_fixture("rollout-2026-09-01T10-00-00-malformed.jsonl")
    # bad json line + unknown record + unknown item type all recorded, not raised
    assert traj.parse_warnings
    assert any("not valid json" in w or "Expecting" in w for w in traj.parse_warnings)
    assert traj.task == "do the thing"
    # the unknown item type still produces an event
    assert any(e.type == EventType.UNKNOWN for e in traj.events)
    # ruff exit 0 -> lint_pass
    assert any(e.type == EventType.LINT_PASS for e in traj.events)
