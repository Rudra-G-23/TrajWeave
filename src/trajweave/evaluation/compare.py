"""Deterministic baseline/candidate comparison - Stage 8's primary evidence.

By convention the *first* verifier check in a frozen spec is the
task-specific check; every additional check is a regression-detection check.
A candidate that fixes the task-specific check while breaking another
previously-passing check is never silently reported as a clean "improved" -
``regression_count``/``regression_details`` are always populated alongside
``outcome`` so the regression stays visible.
"""

from __future__ import annotations

from typing import Any

OUTCOMES = {"improved", "unchanged", "regressed", "invalid", "incomparable"}


def _invalid(reason: str) -> dict[str, Any]:
    return {
        "outcome": "invalid", "invalid_reason": reason, "task_success_delta": None,
        "regression_count": 0, "regression_details": [], "changed_checks": [],
    }


def _incomparable(reason: str) -> dict[str, Any]:
    return {
        "outcome": "incomparable", "invalid_reason": reason, "task_success_delta": None,
        "regression_count": 0, "regression_details": [], "changed_checks": [],
    }


def compare_runs(
    baseline_run: dict[str, Any],
    baseline_checks: list[dict[str, Any]],
    candidate_run: dict[str, Any],
    candidate_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    if baseline_run.get("status") != "completed":
        return _invalid(f"baseline run did not complete: {baseline_run.get('error_reason') or baseline_run.get('status')}")
    if candidate_run.get("status") != "completed":
        return _invalid(f"candidate run did not complete: {candidate_run.get('error_reason') or candidate_run.get('status')}")
    if not baseline_checks or not candidate_checks:
        return _invalid("no verifier results recorded for one or both conditions")

    baseline_by_name = {c["checker_name"]: c for c in baseline_checks}
    candidate_by_name = {c["checker_name"]: c for c in candidate_checks}
    if set(baseline_by_name) != set(candidate_by_name):
        return _incomparable("baseline and candidate ran a different set of verifier checks")

    primary_name = baseline_checks[0]["checker_name"]
    baseline_primary = bool(baseline_by_name[primary_name]["passed"])
    candidate_primary = bool(candidate_by_name[primary_name]["passed"])
    task_success_delta = f"{'pass' if baseline_primary else 'fail'}->{'pass' if candidate_primary else 'fail'}"

    changed_checks: list[dict[str, Any]] = []
    regression_count = 0
    for name in sorted(baseline_by_name):
        b = bool(baseline_by_name[name]["passed"])
        c = bool(candidate_by_name[name]["passed"])
        if b != c:
            changed_checks.append({"checker_name": name, "baseline_passed": b, "candidate_passed": c})
            if b and not c:
                regression_count += 1

    if task_success_delta == "pass->fail":
        outcome = "regressed"
    elif task_success_delta == "fail->pass":
        outcome = "improved"
    else:
        outcome = "regressed" if regression_count else "unchanged"

    return {
        "outcome": outcome,
        "invalid_reason": None,
        "task_success_delta": task_success_delta,
        "regression_count": regression_count,
        "regression_details": [c for c in changed_checks if c["baseline_passed"] and not c["candidate_passed"]],
        "changed_checks": changed_checks,
    }


def metrics_delta(baseline_run: dict[str, Any], candidate_run: dict[str, Any]) -> dict[str, Any]:
    b_dur, c_dur = baseline_run.get("duration_ms"), candidate_run.get("duration_ms")
    b_metrics = baseline_run.get("metrics") or {}
    c_metrics = candidate_run.get("metrics") or {}
    b_tokens, c_tokens = b_metrics.get("tokens"), c_metrics.get("tokens")
    return {
        "duration_ms": {
            "baseline": b_dur, "candidate": c_dur,
            "delta": (c_dur - b_dur) if (b_dur is not None and c_dur is not None) else None,
        },
        "tokens": {
            "baseline": b_tokens, "candidate": c_tokens,
            "available": b_tokens is not None and c_tokens is not None,
        },
        "policy_bytes": c_metrics.get("policy_bytes"),
    }
