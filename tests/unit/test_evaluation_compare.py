from __future__ import annotations

from trajweave.evaluation.compare import compare_runs, metrics_delta


def _run(status="completed", **extra):
    return {"status": status, "error_reason": None, "duration_ms": 100, "metrics": {}, **extra}


def _check(name, passed):
    return {"checker_name": name, "passed": passed}


def test_improved_when_task_check_flips_fail_to_pass():
    result = compare_runs(_run(), [_check("task", False)], _run(), [_check("task", True)])
    assert result["outcome"] == "improved"
    assert result["task_success_delta"] == "fail->pass"
    assert result["regression_count"] == 0


def test_unchanged_when_both_pass():
    result = compare_runs(_run(), [_check("task", True)], _run(), [_check("task", True)])
    assert result["outcome"] == "unchanged"
    assert result["task_success_delta"] == "pass->pass"


def test_unchanged_when_both_fail():
    result = compare_runs(_run(), [_check("task", False)], _run(), [_check("task", False)])
    assert result["outcome"] == "unchanged"
    assert result["task_success_delta"] == "fail->fail"


def test_regressed_when_task_check_flips_pass_to_fail():
    result = compare_runs(_run(), [_check("task", True)], _run(), [_check("task", False)])
    assert result["outcome"] == "regressed"
    assert result["task_success_delta"] == "pass->fail"


def test_improved_with_visible_unrelated_regression():
    baseline_checks = [_check("task", False), _check("regression_suite", True)]
    candidate_checks = [_check("task", True), _check("regression_suite", False)]
    result = compare_runs(_run(), baseline_checks, _run(), candidate_checks)
    assert result["outcome"] == "improved"
    assert result["regression_count"] == 1
    assert result["regression_details"] == [
        {"checker_name": "regression_suite", "baseline_passed": True, "candidate_passed": False}
    ]


def test_regressed_when_secondary_check_breaks_while_primary_unchanged():
    baseline_checks = [_check("task", True), _check("regression_suite", True)]
    candidate_checks = [_check("task", True), _check("regression_suite", False)]
    result = compare_runs(_run(), baseline_checks, _run(), candidate_checks)
    assert result["outcome"] == "regressed"
    assert result["task_success_delta"] == "pass->pass"
    assert result["regression_count"] == 1


def test_invalid_when_baseline_run_did_not_complete():
    result = compare_runs(_run(status="failed", error_reason="boom"), [], _run(), [_check("task", True)])
    assert result["outcome"] == "invalid"
    assert "baseline" in result["invalid_reason"]


def test_invalid_when_candidate_run_did_not_complete():
    result = compare_runs(_run(), [_check("task", True)], _run(status="error", error_reason="boom"), [])
    assert result["outcome"] == "invalid"
    assert "candidate" in result["invalid_reason"]


def test_incomparable_when_verifier_sets_differ():
    result = compare_runs(
        _run(), [_check("task", True)], _run(), [_check("task", True), _check("extra", True)]
    )
    assert result["outcome"] == "incomparable"


def test_metrics_delta_reports_unavailable_tokens_without_failing():
    delta = metrics_delta(_run(duration_ms=200), _run(duration_ms=150))
    assert delta["duration_ms"] == {"baseline": 200, "candidate": 150, "delta": -50}
    assert delta["tokens"]["available"] is False
    assert delta["tokens"]["baseline"] is None
