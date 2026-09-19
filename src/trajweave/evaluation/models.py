"""Pure Stage 8 dataclasses. This module knows nothing about SQLite or the
Stage 7 review ledger - it only describes the shapes that flow between
``workspace.py``, ``execute.py``, ``apply.py``, and ``compare.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VerifierCheck:
    name: str
    command: list[str]
    timeout_seconds: int = 120

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "command": list(self.command), "timeout_seconds": self.timeout_seconds}


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout_excerpt: str
    stderr_excerpt: str


@dataclass(frozen=True)
class VerifierOutcome:
    name: str
    command: list[str]
    exit_code: int | None
    passed: bool
    timed_out: bool
    duration_ms: int | None
    stdout_excerpt: str
    stderr_excerpt: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "passed": self.passed,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
        }
