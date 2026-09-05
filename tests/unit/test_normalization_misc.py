from __future__ import annotations

from trajweave.models.enums import EventType
from trajweave.models.events import NormalizedEvent
from trajweave.normalization.paths import relativize
from trajweave.normalization.status import infer_final_status
from trajweave.normalization.text import bound_text, strip_injected_context, summarize


def test_relativize_inside_repo():
    assert relativize("/home/u/repo/app/api.py", "/home/u/repo") == "app/api.py"


def test_relativize_outside_repo_returns_clean_abs():
    assert relativize("/etc/passwd", "/home/u/repo") == "/etc/passwd"


def test_relativize_handles_file_url_and_none():
    assert relativize("file:///home/u/repo/x.py", "/home/u/repo") == "x.py"
    assert relativize(None, "/home/u/repo") is None
    assert relativize("relative/path.py", "/home/u/repo") == "relative/path.py"


def test_strip_injected_context():
    assert strip_injected_context("<environment_context>\n<cwd>/x</cwd>\n</environment_context>") is None
    assert strip_injected_context("# AGENTS.md instructions\n\n<INSTRUCTIONS>no</INSTRUCTIONS>") is None
    assert strip_injected_context("real question here") == "real question here"
    assert strip_injected_context("") is None


def test_bound_text_and_summarize():
    assert bound_text("x" * 10, 5).endswith("…")
    assert summarize("line one\n\n   line two ") == "line one line two"
    assert summarize(None) is None


def _cmd(exit_code, etype=EventType.COMMAND):
    return NormalizedEvent(type=etype, exit_code=exit_code)


def test_status_success_on_passing_verification():
    events = [_cmd(1, EventType.TEST_FAIL), _cmd(0, EventType.TEST_PASS)]
    status, _ = infer_final_status(events)
    assert status.value == "success"


def test_status_partial_on_failing_tail_verification():
    events = [_cmd(0, EventType.TEST_PASS), _cmd(1, EventType.TEST_FAIL)]
    status, _ = infer_final_status(events)
    assert status.value == "partial"


def test_status_unknown_without_evidence():
    events = [NormalizedEvent(type=EventType.ASSISTANT_MESSAGE)]
    status, _ = infer_final_status(events)
    assert status.value == "unknown"


def test_status_aborted_wins():
    status, _ = infer_final_status([_cmd(0, EventType.TEST_PASS)], aborted=True)
    assert status.value == "aborted"


def test_status_success_on_explicit_completion():
    events = [NormalizedEvent(type=EventType.COMPLETION, summary="all done")]
    status, _ = infer_final_status(events, explicit_completion=True, completion_message="all done")
    assert status.value == "success"
