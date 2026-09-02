"""Stage 4: the local, read-only trajectory explorer.

``trajweave ui`` starts a stdlib HTTP server (no third-party dependency) that
serves a small vanilla-JS single-page app plus a read-only JSON API over the
existing SQLite store. Nothing here writes to the database or to any tracked
repository.
"""

from __future__ import annotations

from trajweave.ui.server import serve

__all__ = ["serve"]
