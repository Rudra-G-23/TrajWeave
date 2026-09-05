from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB


def file_sha256(path: str | Path) -> str:
    """Streaming SHA-256 of a file's raw bytes (memory-friendly for big JSONL)."""

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def stable_short_id(prefix: str, *parts: str, length: int = 12) -> str:
    """Deterministic short id, e.g. ``tw_proj_9f3a1c2b4d5e``.

    The same inputs always produce the same id, which is what makes
    ``trajweave init`` idempotent regardless of DB state.
    """

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:length]}"
