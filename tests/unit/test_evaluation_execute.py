from __future__ import annotations

import sys

from trajweave.evaluation.execute import run_command, run_verifiers
from trajweave.evaluation.models import VerifierCheck


def test_run_command_captures_timeout_without_raising(tmp_path):
    outcome = run_command(
        [sys.executable, "-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout_seconds=1,
    )
    assert outcome.timed_out is True
    assert outcome.exit_code is None


def test_run_command_truncates_unbounded_output(tmp_path):
    script = "import sys; sys.stdout.write('x' * 100000)"
    outcome = run_command([sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=10)
    assert outcome.exit_code == 0
    assert len(outcome.stdout_excerpt) < 100000
    assert "truncated" in outcome.stdout_excerpt


def test_run_command_reports_missing_executable_without_raising(tmp_path):
    outcome = run_command(["/no/such/executable-xyz"], cwd=tmp_path, timeout_seconds=5)
    assert outcome.exit_code is None
    assert outcome.timed_out is False
    assert "failed to start" in outcome.stderr_excerpt


def test_run_verifiers_marks_timeout_as_not_passed(tmp_path):
    checks = [VerifierCheck("slow", [sys.executable, "-c", "import time; time.sleep(5)"], timeout_seconds=1)]
    results = run_verifiers(checks, cwd=tmp_path)
    assert results[0].passed is False
    assert results[0].timed_out is True
