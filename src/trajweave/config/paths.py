"""Resolution of TrajWeave's local-first storage locations.

Everything TrajWeave persists lives under a single global home directory,
``~/.trajweave`` by default. The location can be overridden with the
``TRAJWEAVE_HOME`` environment variable (used heavily by the test-suite so it
never touches a real user's data).

The only thing TrajWeave ever writes *inside a tracked repository* is
``<repo>/.trajweave/project.json`` - created by ``trajweave init``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "TRAJWEAVE_HOME"

#: Name of the per-repository opt-in directory.
REPO_DIR_NAME = ".trajweave"
#: Name of the per-repository opt-in config file.
REPO_CONFIG_NAME = "project.json"


@dataclass(frozen=True)
class TrajWeavePaths:
    """Concrete filesystem locations derived from the global home directory."""

    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / "trajweave.db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    def ensure(self) -> "TrajWeavePaths":
        """Create the home directory tree if it does not yet exist."""

        self.home.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self


def _default_home() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".trajweave"


def get_paths(home: str | os.PathLike[str] | None = None) -> TrajWeavePaths:
    """Return the :class:`TrajWeavePaths` for ``home`` (or the configured default).

    This does **not** create anything on disk; call :meth:`TrajWeavePaths.ensure`.
    """

    if home is not None:
        resolved = Path(home).expanduser().resolve()
    else:
        resolved = _default_home()
    return TrajWeavePaths(home=resolved)
