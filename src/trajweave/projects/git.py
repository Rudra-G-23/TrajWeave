"""Repository-root detection and lightweight git metadata reading.

We avoid shelling out to ``git`` for the hot path (repo-root resolution during
import) so it works even when the repo has been moved or ``git`` is absent - a
plain walk up the tree looking for a ``.git`` entry. ``git`` is only used
opportunistically for remote/branch/commit when the working copy still exists.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitInfo:
    root: Path | None
    remote: str | None = None
    branch: str | None = None
    commit: str | None = None


def find_repo_root(start: str | Path) -> Path | None:
    """Walk up from ``start`` until a ``.git`` file or directory is found.

    ``.git`` may be a directory (normal clone) or a file (worktree / submodule).
    Returns the canonical path of the containing directory, or ``None``.
    """

    try:
        current = Path(start).expanduser().resolve()
    except (OSError, RuntimeError):
        return None

    if current.is_file():
        current = current.parent

    for candidate in [current, *current.parents]:
        git_marker = candidate / ".git"
        if git_marker.exists():
            return candidate
    return None


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def read_git_info(start: str | Path) -> GitInfo:
    root = find_repo_root(start)
    if root is None:
        return GitInfo(root=None)

    if not root.exists():  # pragma: no cover - defensive
        return GitInfo(root=root)

    remote = _git(root, "remote", "get-url", "origin")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(root, "rev-parse", "HEAD")
    return GitInfo(root=root, remote=remote, branch=branch, commit=commit)
