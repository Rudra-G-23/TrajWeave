from __future__ import annotations

import pytest

from trajweave.models.enums import CommandKind, EventType
from trajweave.normalization.commands import classify_command, command_event_family


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("pytest tests/test_billing.py", CommandKind.TEST),
        ("cd frontend && npm test", CommandKind.TEST),
        ("go test ./...", CommandKind.TEST),
        ("ruff check .", CommandKind.LINT),
        ("npx eslint src/", CommandKind.LINT),
        ("mypy src", CommandKind.LINT),
        ("npm run build", CommandKind.BUILD),
        ("cargo build --release", CommandKind.BUILD),
        ("make", CommandKind.BUILD),
        ("pip install -e .", CommandKind.BUILD),
        ("git status --short", CommandKind.VCS),
        ("rg -n coupon src/", CommandKind.SEARCH),
        ("ls -la", CommandKind.LIST),
        ("echo hello", CommandKind.OTHER),
        ("", CommandKind.OTHER),
        # Stage 4 regressions: a tool merely mentioned is not a run of it.
        ("which pytest", CommandKind.OTHER),
        ("python3 -m pytest --version", CommandKind.OTHER),
        ('echo "=== python / pytest ==="', CommandKind.OTHER),
        ("ls .pytest_cache/", CommandKind.LIST),
        ('git commit -m "make the build pass"', CommandKind.VCS),
        ("cd app && FOO=1 pytest -q", CommandKind.TEST),
    ],
)
def test_classify_command(cmd, expected):
    assert classify_command(cmd) == expected


def test_parsed_hint_settles_read():
    assert classify_command("sed -n '1,20p' foo.py", "read") == CommandKind.READ
    # ...but test tokens still win over the hint
    assert classify_command("pytest -q", "read") == CommandKind.TEST


def test_command_event_family():
    assert command_event_family(CommandKind.TEST, 0) == EventType.TEST_PASS
    assert command_event_family(CommandKind.TEST, 1) == EventType.TEST_FAIL
    assert command_event_family(CommandKind.LINT, 0) == EventType.LINT_PASS
    assert command_event_family(CommandKind.BUILD, 2) == EventType.BUILD_FAIL
    assert command_event_family(CommandKind.OTHER, 0) is None
    assert command_event_family(CommandKind.TEST, None) is None
