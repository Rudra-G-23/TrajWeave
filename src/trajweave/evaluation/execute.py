"""Deterministic, isolated subprocess execution for Stage 8 agent/verifier steps.

Neither function here ever invokes a paid model or network endpoint itself -
``command`` is always an explicit argv the caller supplied (a real coding
agent's CLI, or in tests a small deterministic script). TrajWeave only runs
what it is told to run, inside the isolated workspace, and records the
result.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from trajweave.evaluation.models import CommandOutcome, VerifierCheck, VerifierOutcome

_EXCERPT_LIMIT = 20_000  # guard against unbounded verifier/agent output


def _excerpt(text: str) -> str:
    if len(text) <= _EXCERPT_LIMIT:
        return text
    return text[:_EXCERPT_LIMIT] + f"\n... [truncated {len(text) - _EXCERPT_LIMIT} bytes]"


def run_command(command: list[str], *, cwd: Path, timeout_seconds: int) -> CommandOutcome:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandOutcome(proc.returncode, False, duration_ms, _excerpt(proc.stdout), _excerpt(proc.stderr))
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandOutcome(None, True, duration_ms, _excerpt(out), _excerpt(err))
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandOutcome(None, False, duration_ms, "", _excerpt(f"command failed to start: {exc}"))


def run_verifiers(checks: list[VerifierCheck], *, cwd: Path) -> list[VerifierOutcome]:
    results: list[VerifierOutcome] = []
    for check in checks:
        outcome = run_command(check.command, cwd=cwd, timeout_seconds=check.timeout_seconds)
        passed = outcome.exit_code == 0 and not outcome.timed_out
        results.append(
            VerifierOutcome(
                check.name, check.command, outcome.exit_code, passed, outcome.timed_out,
                outcome.duration_ms, outcome.stdout_excerpt, outcome.stderr_excerpt,
            )
        )
    return results
