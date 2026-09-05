"""SQLite connection management + forward-only migrations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

from trajweave.utils.logging import get_logger

log = get_logger("storage")

SCHEMA_VERSION = 5
_MIGRATIONS_PACKAGE = "trajweave.storage.migrations"


def _load_migration(name: str) -> str:
    return resources.files(_MIGRATIONS_PACKAGE).joinpath(name).read_text("utf-8")


_MIGRATIONS: list[tuple[int, str]] = [
    (1, "0001_initial.sql"),
    (2, "0002_experience.sql"),
    (3, "0003_placement.sql"),
    (4, "0004_review.sql"),
    (5, "0005_review_variants.sql"),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Database:
    """Owns a connection and applies migrations on open."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(self.path)
        self._migrate()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- migrations -------------------------------------------------------
    def _migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        current = int(row["v"])
        for version, filename in _MIGRATIONS:
            if version <= current:
                continue
            log.debug("applying migration %s (%s)", version, filename)
            # NB: sqlite3.executescript() issues its own COMMIT, so it cannot run
            # inside our manual BEGIN/COMMIT block. Each migration file is
            # idempotent (IF NOT EXISTS), which keeps this safe on a crash.
            self.conn.executescript(_load_migration(filename))
            self.conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )

    @property
    def schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        return int(row["v"])

    # -- helpers -------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()
