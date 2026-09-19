"""Errors raised by the Stage 9 lifecycle service."""

from __future__ import annotations


class LifecycleError(RuntimeError):
    """A lifecycle operation is invalid, unsafe, or its target is stale."""
