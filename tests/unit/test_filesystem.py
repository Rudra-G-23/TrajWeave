from __future__ import annotations

import pytest

from trajweave.utils.filesystem import retry_windows_sharing_violation


class _SharingViolation(OSError):
    winerror = 5


def test_retry_windows_sharing_violation_retries_then_succeeds():
    attempts = []
    delays = []

    def operation():
        attempts.append(None)
        if len(attempts) < 3:
            raise _SharingViolation("access denied")
        return "done"

    assert retry_windows_sharing_violation(operation, is_windows=True, sleep=delays.append) == "done"
    assert len(attempts) == 3
    assert delays == [0.05, 0.1]


def test_retry_windows_sharing_violation_uses_a_bounded_delay():
    attempts = []
    delays = []

    def operation():
        attempts.append(None)
        raise _SharingViolation("access denied")

    with pytest.raises(_SharingViolation):
        retry_windows_sharing_violation(operation, is_windows=True, sleep=delays.append)
    assert len(attempts) == 30
    assert delays[-1] == 1.0


def test_retry_windows_sharing_violation_reraises_non_transient_error():
    with pytest.raises(PermissionError):
        retry_windows_sharing_violation(
            lambda: (_ for _ in ()).throw(PermissionError("not a sharing violation")),
            is_windows=True,
        )


def test_remove_readonly_retries_with_write_permission(monkeypatch, tmp_path):
    import trajweave.utils.filesystem as filesystem

    path = tmp_path / "readonly"
    calls = []
    chmod_calls = []
    error = PermissionError("access denied")

    monkeypatch.setattr(filesystem.os, "name", "nt")
    monkeypatch.setattr(filesystem.os, "chmod", lambda target, mode: chmod_calls.append((target, mode)))

    filesystem._remove_readonly(
        lambda target: calls.append(target),
        path,
        (PermissionError, error, None),
    )

    assert chmod_calls == [(path, filesystem.stat.S_IWRITE)]
    assert calls == [path]
