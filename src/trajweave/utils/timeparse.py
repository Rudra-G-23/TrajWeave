"""Tolerant timestamp parsing.

Codex and Claude both emit ISO-8601 strings (``2026-09-02T08:41:16.123Z``) but
Codex also carries epoch seconds / milliseconds in a few places. Everything is
normalized to timezone-aware UTC ``datetime`` and serialized back as
``...+00:00`` ISO strings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        # Heuristic: treat very large numbers as milliseconds.
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # Last resort: epoch in a string.
            try:
                return parse_timestamp(float(text))
            except ValueError:
                return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    return None


def to_iso(value: Any) -> str | None:
    dt = parse_timestamp(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()
