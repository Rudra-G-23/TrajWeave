"""Path normalization: store repo-relative paths whenever possible."""

from __future__ import annotations

import os
from pathlib import PurePosixPath, PureWindowsPath


def _to_posix(raw: str) -> str:
    # Session logs may carry file:// URLs or Windows separators.
    if raw.startswith("file://"):
        raw = raw[len("file://") :]
    if "\\" in raw and "/" not in raw:
        try:
            return PureWindowsPath(raw).as_posix()
        except ValueError:  # pragma: no cover - defensive
            return raw.replace("\\", "/")
    return raw


def relativize(raw_path: str | None, repo_root: str | None) -> str | None:
    """Return ``raw_path`` relative to ``repo_root`` when it lives inside it.

    Falls back to the cleaned absolute path if it is outside the repo, and to
    the original string if it cannot be parsed at all. Never raises.
    """

    if not raw_path:
        return None

    cleaned = _to_posix(raw_path.strip())
    if not cleaned:
        return None

    if not repo_root:
        return cleaned

    root = _to_posix(str(repo_root))
    try:
        root_p = PurePosixPath(root)
        path_p = PurePosixPath(cleaned)
    except ValueError:  # pragma: no cover - defensive
        return cleaned

    if not path_p.is_absolute():
        # Already relative - assume it is relative to the repo root.
        return os.path.normpath(cleaned).replace(os.sep, "/")

    try:
        rel = path_p.relative_to(root_p)
        return str(rel) if str(rel) != "." else ""
    except ValueError:
        return cleaned
