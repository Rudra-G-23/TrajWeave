"""SQLite persistence: connection + forward-only migrations, and the Repository.

:class:`Database` owns the connection and applies the versioned migrations in
``migrations/``. :class:`Repository` is the only place raw SQL is written; the
rest of the codebase calls its idempotent upsert / query methods.
"""

from __future__ import annotations

from trajweave.storage.database import Database, connect
from trajweave.storage.repository import Repository

__all__ = ["Database", "connect", "Repository"]
