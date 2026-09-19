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
    assert delays == [0.025, 0.05]


def test_retry_windows_sharing_violation_reraises_non_transient_error():
    with pytest.raises(PermissionError):
        retry_windows_sharing_violation(
            lambda: (_ for _ in ()).throw(PermissionError("not a sharing violation")),
            is_windows=True,
        )
