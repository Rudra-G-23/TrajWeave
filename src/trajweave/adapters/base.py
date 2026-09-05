from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from trajweave.models.enums import Agent
from trajweave.models.trajectory import DiscoveredSession, NormalizedTrajectory
from trajweave.utils.logging import get_logger

log = get_logger("adapters")


def iter_jsonl(path: str | Path) -> Iterator[tuple[int, dict | None, str | None]]:
    """Stream a JSONL file line-by-line.

    Yields ``(lineno, obj, error)``. On a bad line ``obj`` is ``None`` and
    ``error`` describes the problem - callers decide whether to skip or abort.
    Never raises for content problems (only for the file being unreadable).
    """

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                yield lineno, None, f"line {lineno}: {exc}"
                continue
            if not isinstance(obj, dict):
                yield lineno, None, f"line {lineno}: top-level value is {type(obj).__name__}, not object"
                continue
            yield lineno, obj, None


def first_jsonl_object(path: str | Path, limit: int = 50) -> dict | None:
    """Cheap peek: return the first well-formed JSON object (scanning ``limit`` lines)."""

    try:
        for i, (_, obj, _) in enumerate(iter_jsonl(path)):
            if obj is not None:
                return obj
            if i >= limit:
                break
    except OSError:
        return None
    return None


class BaseAdapter(ABC):
    """Agent-specific knowledge lives here and nowhere else.

    Everything downstream (importer, storage, CLI) works only with
    :class:`NormalizedTrajectory`.
    """

    agent: Agent
    agent_name: str

    #: Environment variable that can override the discovery root (tests).
    root_env_var: str = ""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        if root is None and self.root_env_var:
            root = os.environ.get(self.root_env_var) or None
        self._root = Path(root).expanduser() if root is not None else self.default_root()

    # -- discovery --------------------------------------------------------
    @classmethod
    @abstractmethod
    def default_root(cls) -> Path:
        """Default on-disk location of this agent's sessions."""

    @property
    def root(self) -> Path:
        return self._root

    @abstractmethod
    def discover(self) -> Iterator[DiscoveredSession]:
        """Yield candidate sessions found under :pyattr:`root` (cheap; no full parse)."""

    # -- parsing --------------------------------------------------------
    def can_parse(self, session: DiscoveredSession) -> bool:
        return session.agent == self.agent and Path(session.path).is_file()

    @abstractmethod
    def parse(
        self, session: DiscoveredSession, repo_root: str | None = None
    ) -> NormalizedTrajectory:
        """Parse + normalize a single session into the canonical schema.

        ``repo_root`` (when known) lets the adapter store repo-relative paths.
        May raise on a fundamentally unreadable session; per-line problems should
        be collected into ``NormalizedTrajectory.parse_warnings`` instead.
        """
