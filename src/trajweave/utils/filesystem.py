"""Small filesystem helpers with Windows sharing-violation handling."""

from __future__ import annotations

import os
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

_WINDOWS_TRANSIENT_WINERRORS = {5, 32, 33}
_RETRY_ATTEMPTS = 30
_INITIAL_RETRY_DELAY_SECONDS = 0.05
_MAX_RETRY_DELAY_SECONDS = 1.0


def retry_windows_sharing_violation(
    operation: Callable[[], T],
    *,
    is_windows: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run an operation, retrying transient Windows sharing violations."""

    if is_windows is None:
        is_windows = os.name == "nt"
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return operation()
        except OSError as exc:
            if not is_windows or getattr(exc, "winerror", None) not in _WINDOWS_TRANSIENT_WINERRORS:
                raise
            if attempt == _RETRY_ATTEMPTS - 1:
                raise
            sleep(min(_INITIAL_RETRY_DELAY_SECONDS * (2**attempt), _MAX_RETRY_DELAY_SECONDS))
    raise AssertionError("unreachable")


def _remove_readonly(func, path, exc_info) -> None:
    exc = exc_info[1]

    if os.name == "nt" and isinstance(exc, PermissionError):
        os.chmod(path, stat.S_IWRITE)
        func(path)
        return

    raise exc


def remove_tree(path: Path) -> None:
    """Remove a directory tree robustly on Windows."""

    def operation() -> None:
        if not path.exists():
            return

        shutil.rmtree(path, onerror=_remove_readonly)

    retry_windows_sharing_violation(operation)
