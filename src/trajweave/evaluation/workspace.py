"""Isolated baseline/candidate workspaces for Stage 8.

Baseline and candidate must each start from an identical, immutable snapshot
of the developer's repository without ever touching that repository's working
tree, index, or refs. We use ``git clone --local`` (hardlinked objects, same
filesystem, independent ``.git``) rather than ``git worktree add`` because a
worktree registers metadata inside the *source* repo's ``.git/worktrees/`` -
exactly the kind of side effect on the active repository this stage must
never risk, including on a crash mid-evaluation. A plain ``shutil.copytree``
would work too but loses the guarantee that both workspaces are built from
the exact same commit rather than whatever the working tree happened to hold.

Only committed state is ever evaluated: ``git clone`` reads the object
database, not the live working tree, so uncommitted changes in the source
repository are never included in - and can never contaminate - either
sandbox. This is a deliberate, documented limitation, not an oversight.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from trajweave.evaluation.errors import EvaluationError
from trajweave.projects.git import find_repo_root
from trajweave.projects.registry import _is_unsafe_project_root

_GIT_TIMEOUT = 30


class IsolationError(EvaluationError):
    """Building or tearing down an isolated evaluation workspace failed."""


def _run_git(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolationError(f"git command failed to start: {exc}") from exc
    if proc.returncode != 0:
        raise IsolationError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def canonical_repo_root(path: str | Path) -> Path:
    """Resolve ``path`` to the git repository root it belongs to.

    Raises :class:`IsolationError` if ``path`` is not inside a git repository
    or resolves to a shared system directory (the same guard Stage 7's
    project registry uses for registered project roots).
    """

    root = find_repo_root(path)
    if root is None:
        raise IsolationError(
            f"{path} is not inside a git repository; Stage 8 requires a git "
            "history to build a reproducible baseline/candidate snapshot"
        )
    if _is_unsafe_project_root(root):
        raise IsolationError(f"{root} is a shared system directory, not an evaluatable repository")
    return root


def resolve_commit(repo_root: Path, commit: str | None) -> str:
    """Resolve ``commit`` (or HEAD) to a full sha inside ``repo_root``."""

    ref = commit or "HEAD"
    try:
        return _run_git(["-C", str(repo_root), "rev-parse", "--verify", f"{ref}^{{commit}}"])
    except IsolationError as exc:
        raise IsolationError(f"cannot resolve commit {ref!r} in {repo_root}: {exc}") from exc


def has_uncommitted_changes(repo_root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(proc.stdout.strip())


def _clone_at_commit(source: Path, dest: Path, commit: str) -> None:
    _run_git(["clone", "--local", "--no-checkout", "--quiet", str(source), str(dest)])
    _run_git(["-C", str(dest), "checkout", "--quiet", "--detach", commit])
    # Isolation must survive a crash: no isolated clone should ever be able to
    # fetch from or push to the developer's real repository.
    try:
        _run_git(["-C", str(dest), "remote", "remove", "origin"])
    except IsolationError:
        pass


@contextmanager
def isolated_workspaces(repo_root: Path, commit: str) -> Iterator[tuple[Path, Path]]:
    """Yield ``(baseline_dir, candidate_dir)``, two independent clones of
    ``repo_root`` at ``commit``. Both directories - and everything under them
    - are removed on exit, even if the body raises.
    """

    base = Path(tempfile.mkdtemp(prefix="trajweave-eval-"))
    baseline = base / "baseline"
    candidate = base / "candidate"
    try:
        _clone_at_commit(repo_root, baseline, commit)
        _clone_at_commit(repo_root, candidate, commit)
        yield baseline, candidate
    finally:
        shutil.rmtree(base, ignore_errors=True)
