"""Application service orchestrating Stage 8 evaluations.

Mirrors the shape of :class:`trajweave.review.service.ReviewService`: one
service class owns the state machine (here: freeze a spec, then run any
number of baseline/candidate repetitions against it) and is the single
read-model shared by the CLI and the UI.
"""

from __future__ import annotations

import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trajweave import __version__
from trajweave.config.paths import TrajWeavePaths
from trajweave.evaluation import compare
from trajweave.evaluation.apply import SafetyError, apply_candidate_policy
from trajweave.evaluation.errors import EvaluationError
from trajweave.evaluation.execute import run_command, run_verifiers
from trajweave.evaluation.models import VerifierCheck
from trajweave.evaluation.workspace import (
    IsolationError,
    canonical_repo_root,
    has_uncommitted_changes,
    isolated_workspaces,
    resolve_commit,
)
from trajweave.review.service import ReviewError, ReviewService
from trajweave.storage.repository import Repository
from trajweave.utils.hashing import stable_short_id, text_sha256

_ELIGIBLE_STATUSES = {"accepted", "test_first", "applied"}
_CONDITIONS = ("baseline", "candidate")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    import json

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _timeout_for(spec: dict[str, Any]) -> int:
    limits = _decode(spec.get("execution_limits_json"), {}) or {}
    try:
        return int(limits.get("timeout_seconds") or 120)
    except (TypeError, ValueError):
        return 120


class EvaluationService:
    def __init__(self, repo: Repository, paths: TrajWeavePaths):
        self.repo = repo
        self.paths = paths

    # ------------------------------------------------------------------
    # spec creation - freezes everything a fair comparison depends on
    # ------------------------------------------------------------------
    def create_spec(
        self,
        ref: str,
        *,
        repo: str,
        commit: str | None,
        task: dict[str, Any],
        verifiers: list[VerifierCheck],
        agent_command: list[str] | None,
        target_agent: str | None,
        target_override: str | None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        reasoning_config: dict[str, Any] | None = None,
        agent_config: dict[str, Any] | None = None,
        execution_limits: dict[str, Any] | None = None,
        condition_order_mode: str = "baseline_first",
        seed: str | None = None,
    ) -> str:
        review_service = ReviewService(self.repo, self.paths)
        try:
            payload = review_service.history(ref)
        except ReviewError as exc:
            raise EvaluationError(str(exc)) from exc
        review = payload.get("review")
        if not review:
            raise EvaluationError(f"{ref} has not been reviewed yet; run 'trajweave review accept' first")
        if review.get("computed_status") == "stale":
            raise EvaluationError(str(review.get("stale_reason") or "review is stale; re-review before evaluating"))
        if review["status"] not in _ELIGIBLE_STATUSES:
            raise EvaluationError(
                f"review {review['id']} has status {review['status']!r}; only "
                f"{sorted(_ELIGIBLE_STATUSES)} are eligible for evaluation"
            )
        proposal = payload["proposal"]
        chosen_agent = target_agent or review.get("target_agent")
        if chosen_agent not in {"codex", "claude"}:
            raise EvaluationError("an explicit --target-agent (codex or claude) is required")
        if not verifiers:
            raise EvaluationError("at least one --verify command is required")
        if not task:
            raise EvaluationError("a --task or --task-file is required")

        try:
            repo_root = canonical_repo_root(repo)
            resolved_commit = resolve_commit(repo_root, commit)
        except IsolationError as exc:
            raise EvaluationError(str(exc)) from exc
        dirty = has_uncommitted_changes(repo_root)

        spec_id = stable_short_id("EV-", str(review["id"]), str(time.time_ns()))
        spec = {
            "id": spec_id,
            "review_id": review["id"],
            "experience_id": proposal["experience_id"],
            "proposal_id": proposal["id"],
            "content_revision": int(review["content_revision"]),
            "policy_content": proposal["effective_content"],
            "policy_content_hash": text_sha256(str(proposal["effective_content"])),
            "placement_type": proposal["placement_type"],
            "target_agent": chosen_agent,
            "target_override": target_override,
            "scope_type": proposal.get("scope_type"),
            "scope_value": proposal.get("scope_value"),
            "repo_root": str(repo_root),
            "repo_commit": resolved_commit,
            "task_spec": task,
            "agent_name": agent_name,
            "agent_version": agent_version,
            "model_name": model_name,
            "model_version": model_version,
            "reasoning_config": reasoning_config,
            "agent_config": agent_config,
            "agent_command": agent_command,
            "environment": {
                "python_version": sys.version.split()[0],
                "platform": platform.platform(),
                "trajweave_version": __version__,
                "snapshot_excludes_uncommitted_changes": True,
                "source_had_uncommitted_changes": dirty,
            },
            "verifiers": [v.as_dict() for v in verifiers],
            "execution_limits": execution_limits,
            "condition_order_mode": condition_order_mode,
            "seed": seed,
        }
        self.repo.create_evaluation_spec(spec)
        return spec_id

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def run_repetition(self, evaluation_id: str, count: int = 1) -> list[str]:
        spec = self.repo.get_evaluation_spec(evaluation_id)
        if spec is None:
            raise EvaluationError(f"no evaluation {evaluation_id}")
        spec = dict(spec)
        repo_root = Path(spec["repo_root"])
        commit = spec["repo_commit"]
        verifiers = [VerifierCheck(v["name"], v["command"], v.get("timeout_seconds", 120))
                     for v in _decode(spec["verifier_json"], [])]
        agent_command = _decode(spec["agent_command_json"], None)

        comparison_ids = []
        for _ in range(max(1, count)):
            repetition_index = self.repo.next_evaluation_repetition(evaluation_id)
            comparison_ids.append(
                self._run_pair(spec, repetition_index, repo_root, commit, verifiers, agent_command)
            )
        return comparison_ids

    def _order_for(self, spec: dict[str, Any], repetition_index: int) -> list[str]:
        mode = spec["condition_order_mode"]
        if mode == "candidate_first":
            return ["candidate", "baseline"]
        if mode == "alternating":
            return ["baseline", "candidate"] if repetition_index % 2 == 1 else ["candidate", "baseline"]
        return ["baseline", "candidate"]

    def _run_pair(
        self, spec: dict[str, Any], repetition_index: int, repo_root: Path, commit: str,
        verifiers: list[VerifierCheck], agent_command: list[str] | None,
    ) -> str:
        order = self._order_for(spec, repetition_index)
        runs: dict[str, dict[str, Any]] = {}
        try:
            with isolated_workspaces(repo_root, commit) as (baseline_dir, candidate_dir):
                dirs = {"baseline": baseline_dir, "candidate": candidate_dir}
                for position, condition in enumerate(order, start=1):
                    runs[condition] = self._run_condition(
                        spec, repetition_index, condition, position, dirs[condition],
                        verifiers, agent_command,
                    )
        except IsolationError as exc:
            now = _now()
            for condition in _CONDITIONS:
                if condition in runs:
                    continue
                run_id = f"ER-{spec['id']}-{repetition_index}-{condition}"
                run = {
                    "id": run_id, "evaluation_id": spec["id"], "repetition_index": repetition_index,
                    "condition": condition, "order_position": order.index(condition) + 1,
                    "status": "error", "error_reason": f"workspace isolation failed: {exc}",
                    "started_at": now, "ended_at": now, "duration_ms": 0,
                    "verifier_results": [],
                }
                self.repo.record_evaluation_run(run)
                runs[condition] = run

        baseline_run, candidate_run = runs["baseline"], runs["candidate"]
        baseline_checks = [dict(r) for r in self.repo.list_verifier_results(baseline_run["id"])]
        candidate_checks = [dict(r) for r in self.repo.list_verifier_results(candidate_run["id"])]
        result = compare.compare_runs(baseline_run, baseline_checks, candidate_run, candidate_checks)
        result["metrics_delta"] = compare.metrics_delta(baseline_run, candidate_run)
        comparison_id = f"EC-{spec['id']}-{repetition_index}"
        self.repo.record_evaluation_comparison({
            "id": comparison_id, "evaluation_id": spec["id"], "repetition_index": repetition_index,
            "baseline_run_id": baseline_run["id"], "candidate_run_id": candidate_run["id"],
            **result,
        })
        return comparison_id

    def _run_condition(
        self, spec: dict[str, Any], repetition_index: int, condition: str, order_position: int,
        workspace: Path, verifiers: list[VerifierCheck], agent_command: list[str] | None,
    ) -> dict[str, Any]:
        run_id = f"ER-{spec['id']}-{repetition_index}-{condition}"
        started = _now()
        start_monotonic = time.monotonic()

        if condition == "candidate":
            try:
                apply_result = apply_candidate_policy(workspace, spec)
            except SafetyError as exc:
                run = {
                    "id": run_id, "evaluation_id": spec["id"], "repetition_index": repetition_index,
                    "condition": condition, "order_position": order_position, "status": "failed",
                    "error_reason": f"candidate application refused: {exc}",
                    "started_at": started, "ended_at": _now(),
                    "duration_ms": int((time.monotonic() - start_monotonic) * 1000),
                    "verifier_results": [],
                }
                self.repo.record_evaluation_run(run)
                return run
            apply_outcome = apply_result["outcome"]
        else:
            apply_outcome = None

        agent_exit_code = agent_stdout = agent_stderr = None
        agent_timed_out = False
        if agent_command:
            outcome = run_command(agent_command, cwd=workspace, timeout_seconds=_timeout_for(spec))
            agent_exit_code = outcome.exit_code
            agent_timed_out = outcome.timed_out
            agent_stdout = outcome.stdout_excerpt
            agent_stderr = outcome.stderr_excerpt

        verifier_results = run_verifiers(verifiers, cwd=workspace)
        ended = _now()
        duration_ms = int((time.monotonic() - start_monotonic) * 1000)
        metrics = {
            "duration_ms": duration_ms,
            "tokens": None,
            "policy_bytes": len(str(spec["policy_content"]).encode("utf-8")) if condition == "candidate" else None,
        }
        run = {
            "id": run_id, "evaluation_id": spec["id"], "repetition_index": repetition_index,
            "condition": condition, "order_position": order_position, "status": "completed",
            "error_reason": None, "started_at": started, "ended_at": ended, "duration_ms": duration_ms,
            "apply_outcome": apply_outcome, "agent_exit_code": agent_exit_code,
            "agent_timed_out": agent_timed_out, "agent_stdout_excerpt": agent_stdout,
            "agent_stderr_excerpt": agent_stderr, "metrics": metrics,
            "verifier_results": [v.as_dict() for v in verifier_results],
        }
        self.repo.record_evaluation_run(run)
        return run

    # ------------------------------------------------------------------
    # read model - shared by the CLI and the UI
    # ------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.repo.list_evaluation_specs()]

    def history(self, evaluation_id: str) -> dict[str, Any]:
        payload = self.repo.evaluation_history(evaluation_id)
        if payload is None:
            raise EvaluationError(f"no evaluation {evaluation_id}")
        for run in payload["runs"]:
            run["metrics"] = _decode(run.get("metrics_json"), {})
            for result in run.get("verifier_results") or []:
                result["command"] = _decode(result.get("command_json"), [])
        for comparison in payload["comparisons"]:
            comparison["regression_details"] = _decode(comparison.get("regression_details_json"), [])
            comparison["changed_checks"] = _decode(comparison.get("changed_checks_json"), [])
            comparison["metrics_delta"] = _decode(comparison.get("metrics_delta_json"), {})
        payload["spec"]["task_spec"] = _decode(payload["spec"].get("task_spec_json"), {})
        payload["spec"]["verifiers"] = _decode(payload["spec"].get("verifier_json"), [])
        payload["spec"]["agent_command"] = _decode(payload["spec"].get("agent_command_json"), None)
        payload["spec"]["environment"] = _decode(payload["spec"].get("environment_json"), {})
        return payload
